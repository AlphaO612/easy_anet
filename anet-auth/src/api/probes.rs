use super::api::{hash_node_token, node_token_is_valid, validate_admin_session};
use super::dto::AdminToken;
use crate::entities::{
    probe_publications,
    servers::{self, Column},
};
use anet_common::probe_revision::{ProbeEdge, ProbeRevision};
use base64::{Engine, engine::general_purpose::STANDARD};
use ed25519_dalek::SigningKey;
use poem::{Error, Result, error::InternalServerError, http::StatusCode};
use poem_openapi::{
    Object, OpenApi,
    param::{Header, Path, Query},
    payload::Json,
};
use sea_orm::{ActiveModelTrait, QueryOrder, QuerySelect, Set, TransactionTrait};
use sea_orm::{ColumnTrait, DatabaseConnection, EntityTrait, QueryFilter, sea_query::Expr};
use std::collections::{HashMap, HashSet};
use uuid::Uuid;

pub struct ProbesApi {
    pub db: DatabaseConnection,
}

#[derive(Object)]
struct PublishProbes {
    base_revision: i64,
    edges: Vec<ProbeEdge>,
}
#[derive(Object)]
struct ProbeStatus {
    desired: i64,
    applied: i64,
    error: Option<String>,
    manifest: Option<ProbeRevision>,
}
#[derive(Object)]
struct Publication {
    operator_id: Uuid,
    published_at: String,
    manifest: ProbeRevision,
}
#[derive(Object)]
struct Ack {
    revision: i64,
    error: Option<String>,
}

#[derive(Object)]
struct LinkObservation {
    source_node: String,
    edge: ProbeEdge,
    desired: i64,
    applied: i64,
    last_seen: Option<f64>,
    available: Option<bool>,
    probe_bytes_per_second: Option<f64>,
    loss: Option<f64>,
    latency_ms: Option<f64>,
    mbps: Option<f64>,
    capacity_windows: Vec<Option<f64>>,
}

#[derive(Object)]
struct LinkSnapshot {
    monitoring: String,
    links: Vec<LinkObservation>,
}

#[derive(serde::Deserialize)]
struct QueryResponse {
    data: QueryData,
}

#[derive(serde::Deserialize)]
struct QueryData {
    result: Vec<QuerySample>,
}

#[derive(serde::Deserialize)]
struct QuerySample {
    metric: HashMap<String, String>,
    value: (f64, String),
}

// Request only fixed link metrics from the operator-configured upstream.
async fn query_observations(base: &str) -> anyhow::Result<Vec<QuerySample>> {
    let expr = r#"label_replace(last_over_time(anet:link_last_seen_seconds[1h]), "field", "seen", "", "")
        or label_replace(anet:link_available and present_over_time(probe_success{job="server-link"}[45s]), "field", "state", "", "")
        or label_replace(1 - avg_over_time(probe_success{job="server-link"}[45s]), "field", "loss", "", "")
        or label_replace(1000 * avg_over_time(probe_duration_seconds{job="server-link"}[45s]), "field", "latency", "", "")
        or label_replace(avg_over_time(anet_link_capacity_mbps[15s] offset 30s), "field", "window0", "", "")
        or label_replace(avg_over_time(anet_link_capacity_mbps[15s] offset 15s), "field", "window1", "", "")
        or label_replace(avg_over_time(anet_link_capacity_mbps[15s]), "field", "window2", "", "")
        or label_replace(last_over_time(anet_link_capacity_mbps[45s]), "field", "capacity", "", "")
        or label_replace(sum_over_time(probe_http_uncompressed_body_length{job="server-link",module="https_ready"}[1m]) / 60, "field", "bytes", "", "")"#;
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    let response = client
        .get(format!("{}/api/v1/query", base.trim_end_matches('/')))
        .query(&[("query", expr), ("timeout", "3s")])
        .send()
        .await?
        .error_for_status()?
        .json::<QueryResponse>()
        .await?;
    Ok(response.data.result)
}

#[OpenApi]
impl ProbesApi {
    #[oai(path = "/probe-links", method = "get")]
    async fn links(&self, auth: AdminToken) -> Result<Json<LinkSnapshot>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        self.link_snapshot(None).await
    }

    #[oai(path = "/control/nodes/:id/probe-links", method = "get")]
    async fn node_links(
        &self,
        id: Path<Uuid>,
        #[oai(name = "X-Node-Token")] token: Header<String>,
    ) -> Result<Json<LinkSnapshot>> {
        super::topology::authenticate_flow_node(&self.db, id.0, &token.0).await?;
        self.link_snapshot(Some(id.0)).await
    }

    #[oai(path = "/servers/:id/probe-revision", method = "get")]
    async fn status(&self, auth: AdminToken, id: Path<Uuid>) -> Result<Json<ProbeStatus>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        let node = servers::Entity::find_by_id(id.0)
            .one(&self.db)
            .await
            .map_err(InternalServerError)?
            .ok_or_else(|| Error::from_status(StatusCode::NOT_FOUND))?;
        Ok(Json(ProbeStatus {
            desired: node.probe_revision,
            applied: node.probe_applied,
            error: node.probe_error,
            manifest: node
                .probe_manifest
                .as_deref()
                .map(ProbeRevision::decode)
                .transpose()
                .map_err(|_| Error::from_status(StatusCode::INTERNAL_SERVER_ERROR))?,
        }))
    }

    #[oai(path = "/servers/:id/probe-revision", method = "put")]
    async fn publish(
        &self,
        auth: AdminToken,
        id: Path<Uuid>,
        request: Json<PublishProbes>,
    ) -> Result<Json<ProbeRevision>> {
        let operator = validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        let signing_seed = match std::env::var("ANET_PROBE_SIGNING_KEY_FILE") {
            Ok(path) => std::fs::read_to_string(path).ok(),
            Err(_) => std::env::var("ANET_PROBE_SIGNING_KEY").ok(),
        };
        let key = signing_seed
            .and_then(|key| STANDARD.decode(key.trim()).ok())
            .and_then(|bytes| <[u8; 32]>::try_from(bytes).ok())
            .ok_or_else(|| {
                Error::from_string(
                    "Probe signing key unavailable",
                    StatusCode::SERVICE_UNAVAILABLE,
                )
            })?;
        let base = request.0.base_revision;
        let revision = base
            .checked_add(1)
            .ok_or_else(|| Error::from_status(StatusCode::BAD_REQUEST))?;
        let mut manifest = ProbeRevision {
            node_id: id.0.to_string(),
            revision,
            edges: request.0.edges,
            signature: String::new(),
        };
        manifest
            .sign(&SigningKey::from_bytes(&key))
            .map_err(|e| Error::from_string(e.to_string(), StatusCode::BAD_REQUEST))?;
        let targets: HashSet<Uuid> = manifest
            .edges
            .iter()
            .map(|edge| Uuid::parse_str(&edge.target_node))
            .collect::<std::result::Result<_, _>>()
            .map_err(|_| {
                Error::from_string(
                    "Expected a registered target node ID",
                    StatusCode::BAD_REQUEST,
                )
            })?;
        if !targets.is_empty() {
            let known = servers::Entity::find()
                .filter(Column::Id.is_in(targets.iter().copied()))
                .all(&self.db)
                .await
                .map_err(InternalServerError)?;
            if known.len() != targets.len() {
                return Err(Error::from_string(
                    "Unknown target node",
                    StatusCode::BAD_REQUEST,
                ));
            }
        }
        let payload = manifest
            .encode()
            .map_err(|_| Error::from_status(StatusCode::INTERNAL_SERVER_ERROR))?;
        let txn = self.db.begin().await.map_err(InternalServerError)?;
        let changed = servers::Entity::update_many()
            .col_expr(Column::ProbeRevision, Expr::value(revision))
            .col_expr(Column::ProbeManifest, Expr::value(payload.clone()))
            .col_expr(Column::ProbeError, Expr::value(Option::<String>::None))
            .filter(Column::Id.eq(id.0))
            .filter(Column::ProbeRevision.eq(base))
            .exec(&txn)
            .await
            .map_err(InternalServerError)?;
        if changed.rows_affected == 0 {
            return Err(Error::from_status(StatusCode::CONFLICT));
        }
        probe_publications::ActiveModel {
            node_id: Set(id.0),
            revision: Set(revision),
            operator_id: Set(operator),
            published_at: Set(chrono::Utc::now().naive_utc()),
            manifest: Set(payload),
        }
        .insert(&txn)
        .await
        .map_err(InternalServerError)?;
        txn.commit().await.map_err(InternalServerError)?;
        Ok(Json(manifest))
    }

    /// Page through immutable publication history, newest first.
    #[oai(path = "/servers/:id/probe-history", method = "get")]
    async fn history(
        &self,
        auth: AdminToken,
        id: Path<Uuid>,
        before: Query<Option<i64>>,
    ) -> Result<Json<Vec<Publication>>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        let mut query =
            probe_publications::Entity::find().filter(probe_publications::Column::NodeId.eq(id.0));
        if let Some(cursor) = before.0 {
            query = query.filter(probe_publications::Column::Revision.lt(cursor));
        }
        let rows = query
            .order_by_desc(probe_publications::Column::Revision)
            .limit(50)
            .all(&self.db)
            .await
            .map_err(InternalServerError)?;
        let records = rows
            .into_iter()
            .map(|row| {
                Ok(Publication {
                    operator_id: row.operator_id,
                    published_at: row.published_at.and_utc().to_rfc3339(),
                    manifest: ProbeRevision::decode(&row.manifest)
                        .map_err(|_| Error::from_status(StatusCode::INTERNAL_SERVER_ERROR))?,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        Ok(Json(records))
    }

    #[oai(path = "/control/nodes/:id/probe-revision", method = "get")]
    async fn fetch(
        &self,
        id: Path<Uuid>,
        #[oai(name = "X-Node-Token")] token: Header<String>,
    ) -> Result<Json<ProbeRevision>> {
        if !node_token_is_valid(&self.db, id.0, &token.0)
            .await
            .map_err(InternalServerError)?
        {
            return Err(Error::from_status(StatusCode::UNAUTHORIZED));
        }
        let manifest = servers::Entity::find_by_id(id.0)
            .one(&self.db)
            .await
            .map_err(InternalServerError)?
            .and_then(|node| node.probe_manifest)
            .ok_or_else(|| Error::from_status(StatusCode::NOT_FOUND))?;
        Ok(Json(ProbeRevision::decode(&manifest).map_err(|_| {
            Error::from_status(StatusCode::INTERNAL_SERVER_ERROR)
        })?))
    }

    #[oai(path = "/control/nodes/:id/probe-revision/result", method = "post")]
    async fn acknowledge(
        &self,
        id: Path<Uuid>,
        #[oai(name = "X-Node-Token")] token: Header<String>,
        request: Json<Ack>,
    ) -> Result<()> {
        let report = request.0;
        if report.revision < 1 || report.error.as_ref().is_some_and(|e| e.len() > 512) {
            return Err(Error::from_status(StatusCode::BAD_REQUEST));
        }
        let mut update = servers::Entity::update_many();
        if report.error.is_none() {
            update = update.col_expr(Column::ProbeApplied, Expr::value(report.revision));
        }
        let changed = update
            .col_expr(Column::ProbeError, Expr::value(report.error))
            .filter(Column::Id.eq(id.0))
            .filter(Column::ControlTokenHash.eq(hash_node_token(&token.0)))
            .filter(Column::ProbeRevision.eq(report.revision))
            .exec(&self.db)
            .await
            .map_err(InternalServerError)?;
        if changed.rows_affected == 0 {
            return Err(Error::from_status(StatusCode::CONFLICT));
        }
        Ok(())
    }
}

impl ProbesApi {
    async fn link_snapshot(&self, source: Option<Uuid>) -> Result<Json<LinkSnapshot>> {
        let mut query = servers::Entity::find().filter(Column::ProbeManifest.is_not_null());
        if let Some(id) = source {
            query = query.filter(Column::Id.eq(id));
        }
        let nodes = query.all(&self.db).await.map_err(InternalServerError)?;
        let mut links = Vec::new();
        let mut indices = HashMap::new();
        for node in nodes {
            let Some(raw) = node.probe_manifest else {
                continue;
            };
            let manifest = ProbeRevision::decode(&raw)
                .map_err(|_| Error::from_status(StatusCode::INTERNAL_SERVER_ERROR))?;
            for edge in manifest.edges {
                let source_node = node.id.to_string();
                indices.insert(
                    (
                        source_node.clone(),
                        edge.target_node.clone(),
                        edge.module.clone(),
                        edge.target.clone(),
                    ),
                    links.len(),
                );
                links.push(LinkObservation {
                    source_node,
                    edge,
                    desired: node.probe_revision,
                    applied: node.probe_applied,
                    last_seen: None,
                    available: None,
                    probe_bytes_per_second: None,
                    loss: None,
                    latency_ms: None,
                    mbps: None,
                    capacity_windows: vec![None; 3],
                });
            }
        }
        let base = std::env::var("ANET_PROMETHEUS_URL").unwrap_or_default();
        let monitoring = if base.is_empty() {
            "disabled"
        } else if links.is_empty() {
            "configured"
        } else if let Ok(samples) = query_observations(&base).await {
            for sample in samples {
                let mut metric = sample.metric;
                let [src, dst, module, instance] =
                    ["source_node", "target_node", "module", "instance"]
                        .map(|k| metric.remove(k).unwrap_or_default());
                let Some(i) = indices.get(&(src, dst, module, instance)) else {
                    continue;
                };
                let Ok(value) = sample.value.1.parse::<f64>() else {
                    continue;
                };
                if !value.is_finite() || value < 0.0 {
                    continue;
                }
                let link = &mut links[*i];
                match metric.get("field").map(String::as_str) {
                    Some("seen") => link.last_seen = Some(value),
                    Some("state") if value == 0.0 || value == 1.0 => {
                        link.available = Some(value == 1.0)
                    }
                    Some("loss") if value <= 1.0 => link.loss = Some(value),
                    Some("latency") => link.latency_ms = Some(value),
                    Some("window0") => link.capacity_windows[0] = Some(value),
                    Some("window1") => link.capacity_windows[1] = Some(value),
                    Some("window2") => link.capacity_windows[2] = Some(value),
                    Some("capacity") => link.mbps = Some(value),
                    Some("bytes") => link.probe_bytes_per_second = Some(value),
                    _ => (),
                }
            }
            "configured"
        } else {
            "unreachable"
        };
        Ok(Json(LinkSnapshot {
            monitoring: monitoring.into(),
            links,
        }))
    }
}

#[derive(Debug, Clone, Default)]
pub struct NodeProbeMetrics {
    pub latency_ms: Option<f64>,
    pub loss_pct: Option<f64>,
    pub throughput_mbps: Option<f64>,
    pub available: Option<bool>,
}

#[derive(Debug, Clone, Default)]
pub struct EdgeProbeMetrics {
    pub latency_ms: Option<f64>,
    pub loss_pct: Option<f64>,
    pub throughput_mbps: Option<f64>,
    pub available: Option<bool>,
}

/// Собирает актуальные probe-метрики по нодам и рёбрам (best-effort; никогда не падает,
/// если Prometheus не настроен или недоступен).
pub async fn collect_probe_metrics(
    db: &DatabaseConnection,
) -> (
    HashMap<Uuid, NodeProbeMetrics>,
    HashMap<(String, String), EdgeProbeMetrics>,
) {
    let mut node_metrics: HashMap<Uuid, NodeProbeMetrics> = HashMap::new();
    let mut edge_metrics: HashMap<(String, String), EdgeProbeMetrics> = HashMap::new();

    let base = std::env::var("ANET_PROMETHEUS_URL").unwrap_or_default();
    if base.trim().is_empty() {
        return (node_metrics, edge_metrics);
    }

    // Проверяем, есть ли вообще ноды с probe_manifest или публикации probe_publications
    let has_manifests = servers::Entity::find()
        .filter(Column::ProbeManifest.is_not_null())
        .one(db)
        .await
        .ok()
        .flatten()
        .is_some();
    if !has_manifests {
        let has_pubs = probe_publications::Entity::find()
            .one(db)
            .await
            .ok()
            .flatten()
            .is_some();
        if !has_pubs {
            return (node_metrics, edge_metrics);
        }
    }

    let Ok(samples) = query_observations(&base).await else {
        return (node_metrics, edge_metrics);
    };

    let mut lat_acc: HashMap<Uuid, (f64, usize)> = HashMap::new();
    let mut edge_lat_acc: HashMap<(String, String), (f64, usize)> = HashMap::new();

    for sample in samples {
        let mut metric = sample.metric;
        let src = metric.remove("source_node").unwrap_or_default();
        let dst = metric.remove("target_node").unwrap_or_default();
        let field = metric.get("field").map(String::as_str);
        let Ok(value) = sample.value.1.parse::<f64>() else {
            continue;
        };
        if !value.is_finite() || value < 0.0 {
            continue;
        }

        if !src.is_empty() && !dst.is_empty() {
            let edge_key = (src.clone(), dst.clone());
            let edge = edge_metrics.entry(edge_key.clone()).or_default();
            match field {
                Some("state") if value == 0.0 || value == 1.0 => {
                    let avail = value == 1.0;
                    edge.available = Some(edge.available.map_or(avail, |prev| prev || avail));
                }
                Some("loss") if value <= 1.0 => {
                    let pct = value * 100.0;
                    edge.loss_pct = Some(edge.loss_pct.map_or(pct, |prev| prev.max(pct)));
                }
                Some("latency") => {
                    let acc = edge_lat_acc.entry(edge_key).or_insert((0.0, 0));
                    acc.0 += value;
                    acc.1 += 1;
                }
                Some("capacity") => {
                    edge.throughput_mbps = Some(
                        edge.throughput_mbps
                            .map_or(value, |prev| prev.max(value)),
                    );
                }
                _ => {}
            }
        }

        // Агрегируем телеметрию на source_node (и на target_node, если у него нет исходящих проб)
        for node_str in [&src, &dst] {
            let Ok(node_uuid) = Uuid::parse_str(node_str) else {
                continue;
            };
            let entry = node_metrics.entry(node_uuid).or_default();
            match field {
                Some("state") if value == 0.0 || value == 1.0 => {
                    let avail = value == 1.0;
                    entry.available = Some(entry.available.map_or(avail, |prev| prev || avail));
                }
                Some("loss") if value <= 1.0 => {
                    let pct = value * 100.0;
                    entry.loss_pct = Some(entry.loss_pct.map_or(pct, |prev| prev.max(pct)));
                }
                Some("latency") => {
                    let acc = lat_acc.entry(node_uuid).or_insert((0.0, 0));
                    acc.0 += value;
                    acc.1 += 1;
                }
                Some("capacity") => {
                    entry.throughput_mbps = Some(
                        entry
                            .throughput_mbps
                            .map_or(value, |prev| prev.max(value)),
                    );
                }
                _ => {}
            }
        }
    }

    for (node_uuid, (sum, count)) in lat_acc {
        if count > 0 {
            if let Some(entry) = node_metrics.get_mut(&node_uuid) {
                entry.latency_ms = Some(sum / count as f64);
            }
        }
    }
    for (edge_key, (sum, count)) in edge_lat_acc {
        if count > 0 {
            if let Some(edge) = edge_metrics.get_mut(&edge_key) {
                edge.latency_ms = Some(sum / count as f64);
            }
        }
    }

    // Дополняем метрики узлов и рёбер телеметрией от живых шлюзов (ingress_routes),
    // если по конкретному направлению ещё нет данных Prometheus
    if let Ok(routes) = crate::entities::ingress_routes::Entity::find().all(db).await {
        for r in routes {
            if let Some(exit_id) = r.exit_server_id {
                let entry = node_metrics.entry(exit_id).or_default();
                if entry.latency_ms.is_none() {
                    entry.latency_ms = r.last_latency_ms;
                }
                if entry.loss_pct.is_none() {
                    entry.loss_pct = r.last_loss_pct;
                }
                if entry.available.is_none() {
                    entry.available = r.uplink_up;
                }
            }
        }
    }

    (node_metrics, edge_metrics)
}
