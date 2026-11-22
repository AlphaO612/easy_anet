use super::{api::validate_admin_session, dto::AdminToken};
use crate::crypto::DbEncryptor;
use crate::entities::topology_publications as publications;
use crate::entities::{flow_history, flow_reports, profile_credentials, servers, uplink_keys};
use base64::{Engine, engine::general_purpose::STANDARD};
use ed25519_dalek::{Signer, SigningKey};
use poem::{Error, Result, error::InternalServerError, http::StatusCode};
use poem_openapi::param::{Header, Path, Query};
use poem_openapi::{Object, OpenApi, payload::Json};
use sea_orm::ExprTrait;
use sea_orm::QuerySelect;
use sea_orm::sea_query::{Expr, OnConflict};
use sea_orm::{ActiveModelTrait, DatabaseConnection, EntityTrait, QueryOrder, Set};
use sea_orm::{ColumnTrait, ConnectionTrait, DatabaseTransaction, QueryFilter, TransactionTrait};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, net::IpAddr};
use uuid::Uuid;

#[derive(Object)]
struct SignedTopology {
    revision: i64,
    manifest: String,
    signature: String,
}

#[derive(Object)]
struct ProfileCredential {
    profile: String,
    revision: i64,
    config: String,
}

fn credential_cipher() -> Result<DbEncryptor> {
    if std::env::var("DB_ENCRYPTION_KEY").map_or(true, |k| k.len() < 32) {
        return Err(Error::from_status(StatusCode::SERVICE_UNAVAILABLE));
    }
    Ok(DbEncryptor::new())
}

#[derive(Object)]
struct UplinkRequest {
    profile: String,
    path: String,
    revision: i64,
    public_key: String,
}

#[derive(Object)]
struct RegisteredKey {
    public_key: String,
    port: i32,
}

#[derive(Object)]
struct UplinkPeer {
    address: String,
    port: i32,
    public_key: String,
}

#[derive(Object)]
struct FlowReport {
    profile: String,
    revision: i64,
    result: String,
}

#[derive(Object)]
struct NodeReport {
    node_id: Uuid,
    reported_at: String,
    report: FlowReport,
}

pub(super) async fn authenticate_flow_node(
    db: &DatabaseConnection,
    id: Uuid,
    token: &str,
) -> Result<()> {
    use crate::entities::servers;
    let node = servers::Entity::find_by_id(id)
        .one(db)
        .await
        .map_err(InternalServerError)?;
    if !node.is_some_and(|n| {
        n.is_active
            && n.control_token_hash.as_deref() == Some(super::api::hash_node_token(token).as_str())
    }) {
        return Err(Error::from_status(StatusCode::UNAUTHORIZED));
    }
    Ok(())
}

fn for_entry(raw: &str, id: Uuid) -> anyhow::Result<serde_json::Value> {
    let mut t: serde_json::Value = serde_json::from_str(raw)?;
    let entry = id.to_string();
    for k in ["paths", "profiles"] {
        t[k].as_object_mut()
            .ok_or_else(|| anyhow::anyhow!("invalid manifest"))?
            .retain(|_, v| v["entry"].as_str() == Some(entry.as_str()));
    }
    Ok(t)
}

pub struct TopologyApi {
    pub db: DatabaseConnection,
}

#[derive(Object)]
struct Revision {
    revision: i64,
    manifest: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Topology {
    paths: BTreeMap<String, TopologyPath>,
    profiles: BTreeMap<String, Profile>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TopologyPath {
    entry: String,
    exit: String,
    transport: String,
    external_ip: IpAddr,
    probe: Option<Probe>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Probe {
    source_node: String,
    target_node: String,
    module: String,
    target: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Profile {
    dns_path: Option<String>,
    credential_id: Option<Uuid>,
    ingress: Option<String>,
    entry: String,
    paths: Vec<String>,
    strategy: String,
    external_ip: Option<IpAddr>,
    user: Option<String>,
    groups: Option<Vec<String>>,
    #[serde(default)]
    rules: Vec<Rule>,
    #[serde(default)]
    require_domain: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Rule {
    id: String,
    allow: Vec<String>,
    user: Option<String>,
    group: Option<String>,
    domain: Option<String>,
    protocol: Option<String>,
    cidr: Option<String>,
}

fn validate(raw: &str) -> anyhow::Result<()> {
    anyhow::ensure!(raw.len() <= 1_000_000, "manifest too large");
    let t: Topology = serde_json::from_str(raw)?;
    for (name, p) in &t.paths {
        anyhow::ensure!(
            !name.is_empty()
                && !p.entry.is_empty()
                && !p.exit.is_empty()
                && !p.transport.is_empty(),
            "empty path field"
        );
        let _ = p.external_ip;
        if let Some(edge) = &p.probe {
            anyhow::ensure!(
                edge.source_node == p.entry
                    && edge.target_node == p.exit
                    && !edge.target.is_empty()
                    && matches!(
                        edge.module.as_str(),
                        "tcp_connect" | "https_ready" | "https_capacity"
                    ),
                "invalid probe binding"
            );
        }
    }
    // Local exits terminate a path. Every other directed edge must form a DAG.
    let mut degree = BTreeMap::new();
    let mut next: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    for path in t.paths.values().filter(|p| p.entry != p.exit) {
        degree.entry(path.entry.as_str()).or_insert(0usize);
        *degree.entry(path.exit.as_str()).or_default() += 1;
        next.entry(&path.entry).or_default().push(&path.exit);
    }
    let mut ready: Vec<_> = degree
        .iter()
        .filter(|(_, n)| **n == 0)
        .map(|(id, _)| *id)
        .collect();
    let mut visited = 0;
    while let Some(node) = ready.pop() {
        visited += 1;
        for target in next.get(node).into_iter().flatten() {
            let n = degree.get_mut(target).unwrap();
            *n -= 1;
            if *n == 0 {
                ready.push(target);
            }
        }
    }
    anyhow::ensure!(visited == degree.len(), "cyclic route");
    for (name, p) in t.profiles {
        anyhow::ensure!(!name.is_empty() && !p.paths.is_empty(), "empty profile");
        anyhow::ensure!(
            matches!(
                p.strategy.as_str(),
                "prefer-route" | "speed" | "auto" | "strict-ip"
            ),
            "unknown strategy"
        );
        anyhow::ensure!(
            p.strategy != "strict-ip" || p.external_ip.is_some(),
            "strict-ip needs external_ip"
        );
        anyhow::ensure!(
            matches!(
                p.ingress.as_deref(),
                None | Some("native" | "wireguard" | "vless")
            ),
            "unknown ingress"
        );
        let _ = (&p.user, &p.groups, p.require_domain, &p.credential_id);
        anyhow::ensure!(
            p.dns_path.as_ref().is_none_or(|id| p.paths.contains(id)),
            "DNS path outside profile"
        );
        let mut unique = std::collections::HashSet::new();
        for id in &p.paths {
            anyhow::ensure!(
                unique.insert(id) && t.paths.get(id).is_some_and(|path| path.entry == p.entry),
                "invalid profile path"
            );
        }
        for r in p.rules {
            anyhow::ensure!(
                !r.id.is_empty() && r.allow.iter().all(|id| p.paths.contains(id)),
                "invalid rule"
            );
            if let Some(cidr) = r.cidr {
                cidr.parse::<ipnet::IpNet>()?;
            }
            let _ = (&r.user, &r.group, &r.domain, &r.protocol);
        }
    }
    Ok(())
}

impl TopologyApi {
    async fn latest(&self) -> Result<Revision> {
        Self::latest_from(&self.db).await
    }

    async fn locked_publication(&self) -> Result<(DatabaseTransaction, Revision)> {
        let tx = self.db.begin().await.map_err(InternalServerError)?;
        // Block publication INSERTs until this acknowledgement or grant commits.
        tx.execute_unprepared("LOCK TABLE topology_publications IN SHARE MODE")
            .await
            .map_err(InternalServerError)?;
        let latest = Self::latest_from(&tx).await?;
        Ok((tx, latest))
    }

    async fn latest_from(db: &impl ConnectionTrait) -> Result<Revision> {
        let row = publications::Entity::find()
            .order_by_desc(publications::Column::Revision)
            .one(db)
            .await
            .map_err(InternalServerError)?;
        Ok(row.map_or_else(
            || Revision {
                revision: 0,
                manifest: r#"{"paths":{},"profiles":{}}"#.into(),
            },
            |row| Revision {
                revision: row.revision,
                manifest: row.manifest,
            },
        ))
    }
}

#[OpenApi]
impl TopologyApi {
    #[oai(path = "/control/nodes/:id/profile-topology", method = "get")]
    async fn node_topology(
        &self,
        id: Path<Uuid>,
        #[oai(name = "X-Node-Token")] token: Header<String>,
    ) -> Result<Json<SignedTopology>> {
        authenticate_flow_node(&self.db, id.0, &token.0).await?;
        let mut published = self.latest().await?;
        published.manifest = for_entry(&published.manifest, id.0)
            .map_err(|_| Error::from_status(StatusCode::INTERNAL_SERVER_ERROR))?
            .to_string();
        let seed = std::env::var("ANET_PROBE_SIGNING_KEY_FILE")
            .ok()
            .and_then(|p| std::fs::read_to_string(p).ok())
            .or_else(|| std::env::var("ANET_PROBE_SIGNING_KEY").ok())
            .and_then(|s| STANDARD.decode(s.trim()).ok())
            .and_then(|v| <[u8; 32]>::try_from(v).ok())
            .ok_or_else(|| Error::from_status(StatusCode::SERVICE_UNAVAILABLE))?;
        let payload = format!(
            "anet/flow/v1\n{}\n{}\n{}",
            id.0, published.revision, published.manifest
        );
        let signature = STANDARD.encode(
            SigningKey::from_bytes(&seed)
                .sign(payload.as_bytes())
                .to_bytes(),
        );
        Ok(Json(SignedTopology {
            revision: published.revision,
            manifest: published.manifest,
            signature,
        }))
    }

    #[oai(path = "/control/nodes/:id/identity", method = "put")]
    async fn register_identity(
        &self,
        id: Path<Uuid>,
        #[oai(name = "X-Node-Token")] token: Header<String>,
        req: Json<RegisteredKey>,
    ) -> Result<()> {
        if STANDARD
            .decode(&req.public_key)
            .map_or(true, |v| v.len() != 32)
            || !(1..=65535).contains(&req.port)
        {
            return Err(Error::from_status(StatusCode::BAD_REQUEST));
        }
        let changed = servers::Entity::update_many()
            .col_expr(
                servers::Column::PublicKey,
                Expr::value(req.public_key.clone()),
            )
            .col_expr(servers::Column::QuicPort, Expr::value(req.port))
            .filter(servers::Column::Id.eq(id.0))
            .filter(servers::Column::IsActive.eq(true))
            .filter(servers::Column::ControlTokenHash.eq(super::api::hash_node_token(&token.0)))
            .filter(
                servers::Column::PublicKey
                    .eq("")
                    .or(servers::Column::PublicKey.eq(&req.public_key)),
            )
            .filter(
                servers::Column::QuicPort
                    .is_null()
                    .or(servers::Column::QuicPort.eq(req.port)),
            )
            .exec(&self.db)
            .await
            .map_err(InternalServerError)?;
        if changed.rows_affected == 0 {
            return Err(Error::from_status(StatusCode::CONFLICT));
        }
        Ok(())
    }

    #[oai(path = "/control/nodes/:id/uplink", method = "put")]
    async fn authorize_uplink(
        &self,
        id: Path<Uuid>,
        #[oai(name = "X-Node-Token")] token: Header<String>,
        req: Json<UplinkRequest>,
    ) -> Result<Json<UplinkPeer>> {
        authenticate_flow_node(&self.db, id.0, &token.0).await?;
        let (tx, latest) = self.locked_publication().await?;
        if latest.revision != req.revision {
            return Err(Error::from_status(StatusCode::CONFLICT));
        }
        let t: Topology = serde_json::from_str(&latest.manifest).map_err(InternalServerError)?;
        let node = id.0.to_string();
        let path = t
            .paths
            .get(&req.path)
            .filter(|p| p.entry == node && p.transport == "quic" && p.entry != p.exit)
            .ok_or_else(|| Error::from_status(StatusCode::FORBIDDEN))?;
        if !t
            .profiles
            .get(&req.profile)
            .is_some_and(|p| p.entry == node && p.paths.contains(&req.path))
        {
            return Err(Error::from_status(StatusCode::FORBIDDEN));
        }
        let exit_id =
            Uuid::parse_str(&path.exit).map_err(|_| Error::from_status(StatusCode::BAD_REQUEST))?;
        let exit = servers::Entity::find_by_id(exit_id)
            .filter(servers::Column::IsActive.eq(true))
            .one(&tx)
            .await
            .map_err(InternalServerError)?
            .ok_or_else(|| Error::from_status(StatusCode::NOT_FOUND))?;
        let port = exit
            .quic_port
            .filter(|p| (1..=65535).contains(p))
            .ok_or_else(|| Error::from_status(StatusCode::BAD_REQUEST))?;
        let key = STANDARD
            .decode(&req.public_key)
            .map_err(|_| Error::from_status(StatusCode::BAD_REQUEST))?;
        if key.len() != 32 {
            return Err(Error::from_status(StatusCode::BAD_REQUEST));
        }
        let fp = STANDARD.encode(&Sha256::digest(&key)[..16]);
        uplink_keys::Entity::insert(uplink_keys::ActiveModel {
            node_id: Set(id.0),
            profile: Set(req.profile.clone()),
            path: Set(req.path.clone()),
            revision: Set(req.revision),
            fingerprint: Set(fp),
            exit_id: Set(exit_id),
        })
        .on_conflict(
            OnConflict::columns([
                uplink_keys::Column::NodeId,
                uplink_keys::Column::Profile,
                uplink_keys::Column::Path,
            ])
            .update_columns([
                uplink_keys::Column::Revision,
                uplink_keys::Column::Fingerprint,
                uplink_keys::Column::ExitId,
            ])
            .to_owned(),
        )
        .exec_without_returning(&tx)
        .await
        .map_err(InternalServerError)?;
        tx.commit().await.map_err(InternalServerError)?;
        Ok(Json(UplinkPeer {
            address: exit.address,
            port,
            public_key: exit.public_key,
        }))
    }

    #[oai(path = "/control/nodes/:id/profile-config", method = "put")]
    async fn store_profile_config(
        &self,
        id: Path<Uuid>,
        #[oai(name = "X-Node-Token")] token: Header<String>,
        input: Json<ProfileCredential>,
    ) -> Result<()> {
        authenticate_flow_node(&self.db, id.0, &token.0).await?;
        let (tx, latest) = self.locked_publication().await?;
        if latest.revision != input.revision {
            return Err(Error::from_status(StatusCode::CONFLICT));
        }
        let manifest = for_entry(&latest.manifest, id.0)
            .map_err(|_| Error::from_status(StatusCode::INTERNAL_SERVER_ERROR))?;
        if manifest["profiles"].get(&input.profile).is_none()
            || input.config.len() > 32768
            || input.config.is_empty()
        {
            return Err(Error::from_status(StatusCode::BAD_REQUEST));
        }
        let encrypted = credential_cipher()?
            .encrypt(&input.config)
            .map_err(|_| Error::from_status(StatusCode::INTERNAL_SERVER_ERROR))?;
        profile_credentials::Entity::insert(profile_credentials::ActiveModel {
            node_id: Set(id.0),
            profile: Set(input.profile.clone()),
            revision: Set(input.revision),
            issued_at: Set(chrono::Utc::now().naive_utc()),
            ciphertext: Set(encrypted),
        })
        .on_conflict(
            OnConflict::columns([
                profile_credentials::Column::NodeId,
                profile_credentials::Column::Profile,
            ])
            .update_columns([
                profile_credentials::Column::Revision,
                profile_credentials::Column::IssuedAt,
                profile_credentials::Column::Ciphertext,
            ])
            .to_owned(),
        )
        .exec_without_returning(&tx)
        .await
        .map_err(InternalServerError)?;
        tx.commit().await.map_err(InternalServerError)?;
        Ok(())
    }

    #[oai(path = "/servers/:id/profile-config", method = "get")]
    async fn get_profile_config(
        &self,
        auth: AdminToken,
        id: Path<Uuid>,
        profile: Query<String>,
    ) -> Result<Json<ProfileCredential>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        let latest = self.latest().await?;
        let manifest = for_entry(&latest.manifest, id.0)
            .map_err(|_| Error::from_status(StatusCode::INTERNAL_SERVER_ERROR))?;
        if manifest["profiles"].get(&profile.0).is_none() {
            return Err(Error::from_status(StatusCode::NOT_FOUND));
        }
        let ack = flow_reports::Entity::find_by_id((id.0, profile.0.clone()))
            .filter(flow_reports::Column::Revision.eq(latest.revision))
            .filter(
                flow_reports::Column::ReportedAt
                    .gte((chrono::Utc::now() - chrono::Duration::seconds(90)).naive_utc()),
            )
            .one(&self.db)
            .await
            .map_err(InternalServerError)?;
        if !ack
            .and_then(|r| serde_json::from_str::<serde_json::Value>(&r.result).ok())
            .is_some_and(|v| v["status"].as_str() == Some("applied"))
        {
            return Err(Error::from_status(StatusCode::CONFLICT));
        }
        let row = profile_credentials::Entity::find_by_id((id.0, profile.0))
            .filter(profile_credentials::Column::Revision.eq(latest.revision))
            .one(&self.db)
            .await
            .map_err(InternalServerError)?
            .ok_or_else(|| Error::from_status(StatusCode::NOT_FOUND))?;
        Ok(Json(ProfileCredential {
            profile: row.profile,
            revision: row.revision,
            config: credential_cipher()?
                .decrypt(&row.ciphertext)
                .map_err(|_| Error::from_status(StatusCode::INTERNAL_SERVER_ERROR))?,
        }))
    }

    #[oai(path = "/control/nodes/:id/profile-result", method = "put")]
    async fn report_flow(
        &self,
        id: Path<Uuid>,
        #[oai(name = "X-Node-Token")] token: Header<String>,
        report: Json<FlowReport>,
    ) -> Result<()> {
        authenticate_flow_node(&self.db, id.0, &token.0).await?;
        let (tx, latest) = self.locked_publication().await?;
        if report.revision != latest.revision {
            return Err(Error::from_status(StatusCode::CONFLICT));
        }
        let manifest = for_entry(&latest.manifest, id.0)
            .map_err(|_| Error::from_status(StatusCode::INTERNAL_SERVER_ERROR))?;
        if manifest["profiles"].get(&report.profile).is_none() || report.result.len() > 65536 {
            return Err(Error::from_status(StatusCode::BAD_REQUEST));
        }
        let before = flow_reports::Entity::find_by_id((id.0, report.profile.clone()))
            .one(&tx)
            .await
            .map_err(InternalServerError)?;
        // Fresh observations refresh the status row. Record history only for a
        // new publication or a change in its routing decisions.
        let decisions = |s: &str| {
            serde_json::from_str::<serde_json::Value>(s)
                .ok()
                .map(|v| (v["decisions"].clone(), v["status"].clone()))
        };
        if before.as_ref().is_none_or(|b| {
            b.revision != report.revision || decisions(&b.result) != decisions(&report.result)
        }) {
            flow_history::Entity::insert(flow_history::ActiveModel {
                node_id: Set(id.0),
                profile: Set(report.profile.clone()),
                revision: Set(report.revision),
                reported_at: Set(chrono::Utc::now().naive_utc()),
                result: Set(report.result.clone()),
                ..Default::default()
            })
            .exec(&tx)
            .await
            .map_err(InternalServerError)?;
        }
        flow_reports::Entity::insert(flow_reports::ActiveModel {
            node_id: Set(id.0),
            profile: Set(report.profile.clone()),
            revision: Set(report.revision),
            reported_at: Set(chrono::Utc::now().naive_utc()),
            result: Set(report.result.clone()),
        })
        .on_conflict(
            OnConflict::columns([flow_reports::Column::NodeId, flow_reports::Column::Profile])
                .update_columns([
                    flow_reports::Column::Revision,
                    flow_reports::Column::ReportedAt,
                    flow_reports::Column::Result,
                ])
                .to_owned(),
        )
        .exec_without_returning(&tx)
        .await
        .map_err(InternalServerError)?;
        tx.commit().await.map_err(InternalServerError)?;
        Ok(())
    }

    #[oai(path = "/servers/:id/profile-history", method = "get")]
    async fn flow_history(
        &self,
        auth: AdminToken,
        id: Path<Uuid>,
        profile: Query<String>,
    ) -> Result<Json<Vec<NodeReport>>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        let rows = flow_history::Entity::find()
            .filter(flow_history::Column::NodeId.eq(id.0))
            .filter(flow_history::Column::Profile.eq(profile.0))
            .order_by_desc(flow_history::Column::Id)
            .limit(50)
            .all(&self.db)
            .await
            .map_err(InternalServerError)?;
        Ok(Json(
            rows.into_iter()
                .map(|row| NodeReport {
                    node_id: row.node_id,
                    reported_at: row.reported_at.and_utc().to_rfc3339(),
                    report: FlowReport {
                        profile: row.profile,
                        revision: row.revision,
                        result: row.result,
                    },
                })
                .collect(),
        ))
    }

    #[oai(path = "/profile-results", method = "get")]
    async fn profile_results(&self, auth: AdminToken) -> Result<Json<Vec<NodeReport>>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        let rows = flow_reports::Entity::find()
            .order_by_desc(flow_reports::Column::ReportedAt)
            .all(&self.db)
            .await
            .map_err(InternalServerError)?;
        Ok(Json(
            rows.into_iter()
                .map(|row| NodeReport {
                    node_id: row.node_id,
                    reported_at: row.reported_at.and_utc().to_rfc3339(),
                    report: FlowReport {
                        profile: row.profile,
                        revision: row.revision,
                        result: row.result,
                    },
                })
                .collect(),
        ))
    }

    #[oai(path = "/profile-topology", method = "get")]
    async fn current(&self, auth: AdminToken) -> Result<Json<Revision>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        Ok(Json(self.latest().await?))
    }
    #[oai(path = "/profile-topology", method = "put")]
    async fn publish_topology(
        &self,
        auth: AdminToken,
        input: Json<Revision>,
    ) -> Result<Json<Revision>> {
        let operator = validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        validate(&input.manifest)
            .map_err(|e| Error::from_string(e.to_string(), StatusCode::BAD_REQUEST))?;
        if self.latest().await?.revision != input.revision {
            return Err(Error::from_status(StatusCode::CONFLICT));
        }
        let revision = input.revision + 1;
        let inserted = publications::ActiveModel {
            revision: Set(revision),
            manifest: Set(input.manifest.clone()),
            published_at: Set(chrono::Utc::now().naive_utc()),
            operator_id: Set(operator),
        }
        .insert(&self.db)
        .await;
        if let Err(err) = inserted {
            if self.latest().await?.revision >= revision {
                return Err(Error::from_status(StatusCode::CONFLICT));
            }
            return Err(InternalServerError(err));
        }
        Ok(Json(Revision {
            revision,
            manifest: input.manifest.clone(),
        }))
    }
}

// The exit authenticates client signatures in the ANet handshake. The grant
// applies only on the registered exit and only to the current publication.
pub(super) async fn uplink_allowed(db: &DatabaseConnection, exit: Uuid, fp: &str) -> Result<bool> {
    let active = servers::Entity::find_by_id(exit)
        .filter(servers::Column::IsActive.eq(true))
        .one(db)
        .await
        .map_err(InternalServerError)?
        .is_some();
    if !active {
        return Ok(false);
    }
    let latest = TopologyApi { db: db.clone() }.latest().await?;
    let grants = uplink_keys::Entity::find()
        .filter(uplink_keys::Column::Revision.eq(latest.revision))
        .filter(uplink_keys::Column::ExitId.eq(exit))
        .filter(uplink_keys::Column::Fingerprint.eq(fp))
        .all(db)
        .await
        .map_err(InternalServerError)?;
    if grants.is_empty() {
        return Ok(false);
    }
    Ok(servers::Entity::find()
        .filter(servers::Column::Id.is_in(grants.into_iter().map(|g| g.node_id)))
        .filter(servers::Column::IsActive.eq(true))
        .filter(servers::Column::ControlTokenHash.is_not_null())
        .one(db)
        .await
        .map_err(InternalServerError)?
        .is_some())
}
