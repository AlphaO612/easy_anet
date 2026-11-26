use crate::api::api::{
    generate_node_token, matches_token_hash, node_token_is_valid, sha256_hex, url_encode,
    validate_admin_session,
};
use crate::api::dto::AdminToken;
use crate::api::users::build_user_client_toml;
use crate::crypto::DbEncryptor;
use crate::entities::{
    active_sessions, ingress_routes, node_runtime_states, rates, servers, topology_publications,
    traffic_totals, user_device_traffic_sources, user_devices, user_subscriptions, users,
};
use anet_common::probe_revision::ProbeRevision;
use base64::{engine::general_purpose::STANDARD, Engine as _};
use chrono::Utc;
use ipnet::Ipv4Net;
use poem::{error::InternalServerError, http::StatusCode, Error, Result};
use poem_openapi::{
    param::{Header, Path, Query},
    payload::{Json, PlainText},
    Object, OpenApi,
};
use rand::rngs::OsRng;
use sea_orm::sea_query::OnConflict;
use sea_orm::{
    ActiveModelTrait, ColumnTrait, DatabaseConnection, EntityTrait, IntoActiveModel, QueryFilter,
    QueryOrder, Set,
};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::net::Ipv4Addr;
use uuid::Uuid;
use x25519_dalek::{PublicKey as X25519PublicKey, StaticSecret as X25519StaticSecret};

pub struct DevicesApi {
    pub db: DatabaseConnection,
    pub client_template_path: String,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct IngressRouteDto {
    pub id: String,
    pub protocol: String,
    pub name: String,
    pub entry_host: String,
    pub entry_port: i32,
    pub exit_host: String,
    pub entry_server_id: Option<Uuid>,
    pub exit_server_id: Option<Uuid>,
    pub wg_server_public_key: Option<String>,
    pub wg_subnet: Option<String>,
    pub sni: Option<String>,
    pub sync_mode: String,
    pub last_sync_at: Option<String>,
    pub created_at: String,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct IngressRoutesResponseDto {
    pub origin: String,
    pub routes: Vec<IngressRouteDto>,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct CreateIngressRouteRequest {
    pub id: Option<String>,
    pub protocol: String,
    pub name: String,
    pub entry_host: String,
    pub entry_port: i32,
    pub exit_host: String,
    pub entry_server_id: Option<Uuid>,
    pub exit_server_id: Option<Uuid>,
    pub wg_server_public_key: Option<String>,
    pub wg_subnet: Option<String>,
    pub sni: Option<String>,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct CreateIngressRouteResponseDto {
    pub route: IngressRouteDto,
    pub sync_token: String,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct UserDeviceDto {
    pub id: Uuid,
    pub user_id: Uuid,
    pub protocol: String,
    pub name: String,
    pub route_id: Option<String>,
    pub entry_host: Option<String>,
    pub entry_port: Option<i32>,
    pub exit_host: Option<String>,
    pub status: String,
    pub sync_mode: Option<String>,
    pub synced_at: Option<String>,
    pub config_uri: Option<String>,
    pub config_text: Option<String>,
    pub assigned_ip: Option<String>,
    pub upload_bytes: i64,
    pub download_bytes: i64,
    pub last_traffic_at: Option<String>,
    pub traffic_state: String,
    pub created_at: String,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct UserDevicesResponseDto {
    pub user_id: Uuid,
    pub subscription_active: bool,
    pub subscription_origin: String,
    pub devices: Vec<UserDeviceDto>,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct CreateDeviceRequestDto {
    pub protocol: String,
    pub name: String,
    pub route_id: Option<String>,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct SubscriptionResponseDto {
    pub path: String,
    pub url: String,
    pub token: String,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct IngressSyncRequest {
    pub protocol: String,
    pub source: String,
    pub wg_server_public_key: Option<String>,
    pub totals: HashMap<String, Vec<i64>>,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct ActiveVlessClientDto {
    pub uuid: String,
    pub email: String,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct ActiveWgPeerDto {
    pub public_key: String,
    pub allowed_ips: String,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct IngressSyncResponse {
    pub route_id: String,
    pub protocol: String,
    pub active_vless_clients: Vec<ActiveVlessClientDto>,
    pub active_wg_peers: Vec<ActiveWgPeerDto>,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct GraphNodeDto {
    pub id: String,
    pub name: String,
    pub role: String,
    pub address: String,
    pub is_active: bool,
    pub status: String,
    pub heartbeat_age_sec: Option<i64>,
    pub uptime_seconds: Option<i64>,
    pub active_connections: i64,
    pub accepting_connections: bool,
    pub version: Option<String>,
    pub transports: Vec<String>,
    pub rx_bytes: i64,
    pub tx_bytes: i64,
    pub device_count: i64,
    pub probe_revision: i64,
    pub probe_applied: i64,
    pub probe_error: Option<String>,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct GraphEdgeDto {
    pub id: String,
    pub source: String,
    pub target: String,
    pub transport: String,
    pub route_id: Option<String>,
    pub status: String,
    pub latency_ms: Option<f64>,
    pub loss_pct: Option<f64>,
    pub throughput_mbps: Option<f64>,
    pub rx_bytes: i64,
    pub tx_bytes: i64,
    pub last_measured_at: Option<String>,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct ProtocolCountsDto {
    pub vless: i64,
    pub wireguard: i64,
    pub anet: i64,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct ClientsSummaryDto {
    pub total_users: i64,
    pub active_users: i64,
    pub active_sessions: i64,
    pub by_protocol: ProtocolCountsDto,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct UserGraphPathDto {
    pub user_id: Uuid,
    pub uid: String,
    pub is_active: bool,
    pub protocols: Vec<String>,
    pub node_ids: Vec<String>,
    pub edge_ids: Vec<String>,
    pub rx_bytes: i64,
    pub tx_bytes: i64,
}

#[derive(Object, Debug, Clone, Serialize, Deserialize)]
pub struct TopologyGraphDto {
    pub generated_at: String,
    pub nodes: Vec<GraphNodeDto>,
    pub edges: Vec<GraphEdgeDto>,
    pub clients_summary: ClientsSummaryDto,
    pub user_paths: Vec<UserGraphPathDto>,
}

#[derive(Deserialize)]
struct WizardVlessCatalog {
    #[serde(default)]
    origin: String,
    #[serde(default)]
    routes: HashMap<String, WizardVlessRoute>,
}

#[derive(Deserialize)]
struct WizardVlessRoute {
    entry: String,
    exit: String,
    port: i32,
    #[serde(default)]
    devices: Vec<String>,
    #[serde(default)]
    usage: Option<WizardVlessUsage>,
}

#[derive(Deserialize)]
struct WizardVlessUsage {
    #[serde(default)]
    token_hash: String,
}

#[derive(Deserialize)]
struct WizardStarterProfile {
    id: String,
    protocol: String,
    entry: String,
    exit: String,
    port: i32,
}

fn catalog_paths() -> (String, String, String) {
    let vless_path = std::env::var("ANET_VLESS_CATALOG_PATH")
        .unwrap_or_else(|_| "/srv/anet-vless-catalog/index.json".into());
    let origin_path = std::env::var("ANET_SUBSCRIPTION_ORIGIN_PATH")
        .unwrap_or_else(|_| "/srv/anet-vless-catalog/subscription-origin".into());
    let starter_path = std::env::var("ANET_STARTER_PROFILES_PATH")
        .unwrap_or_else(|_| "/srv/anet-profiles/index.json".into());
    (vless_path, origin_path, starter_path)
}

fn read_vless_catalog() -> Option<WizardVlessCatalog> {
    let (vless_path, _, _) = catalog_paths();
    let raw = std::fs::read_to_string(vless_path).ok()?;
    serde_json::from_str(&raw).ok()
}

fn resolve_subscription_origin() -> String {
    let (_, origin_path, _) = catalog_paths();
    if let Ok(explicit) = std::fs::read_to_string(origin_path) {
        let trimmed = explicit.trim();
        if !trimmed.is_empty() {
            return trimmed.trim_end_matches('/').to_string();
        }
    }
    if let Some(cat) = read_vless_catalog() {
        let trimmed = cat.origin.trim();
        if !trimmed.is_empty() {
            return trimmed.trim_end_matches('/').to_string();
        }
    }
    std::env::var("ANET_PUBLIC_URL")
        .ok()
        .map(|u| u.trim().trim_end_matches('/').to_string())
        .unwrap_or_default()
}

/// Автоматически импортирует маршруты из смонтированных каталогов мастера в `ingress_routes`
/// (идемпотентно, не затирая уже настроенные оператором поля и live-режим).
async fn sync_wizard_routes(db: &DatabaseConnection) {
    let now = Utc::now().naive_utc();
    let all_servers = servers::Entity::find().all(db).await.unwrap_or_default();
    let find_server_id = |host: &str| -> Option<Uuid> {
        let target = host.trim().to_lowercase();
        all_servers
            .iter()
            .find(|s| s.address.trim().to_lowercase() == target)
            .map(|s| s.id)
    };

    if let Some(catalog) = read_vless_catalog() {
        for (route_id, route) in catalog.routes {
            let existing = ingress_routes::Entity::find_by_id(route_id.clone())
                .one(db)
                .await
                .ok()
                .flatten();
            let usage_hash = route
                .usage
                .as_ref()
                .map(|u| u.token_hash.trim().to_string())
                .filter(|s| !s.is_empty());

            if let Some(model) = existing {
                if model.sync_token_hash.is_none() && usage_hash.is_some() {
                    let mut active = model.into_active_model();
                    active.sync_token_hash = Set(usage_hash);
                    let _ = active.update(db).await;
                }
            } else {
                let model = ingress_routes::ActiveModel {
                    id: Set(route_id.clone()),
                    protocol: Set("vless".into()),
                    name: Set(format!("VLESS {}", route_id)),
                    entry_host: Set(route.entry.clone()),
                    entry_port: Set(route.port),
                    exit_host: Set(route.exit.clone()),
                    entry_server_id: Set(find_server_id(&route.entry)),
                    exit_server_id: Set(find_server_id(&route.exit)),
                    wg_server_public_key: Set(None),
                    wg_subnet: Set(None),
                    sni: Set(Some(route.entry.clone())),
                    sync_token_hash: Set(usage_hash),
                    sync_mode: Set("legacy_pool".into()),
                    last_sync_at: Set(None),
                    created_at: Set(now),
                };
                let _ = model.insert(db).await;
            }
        }
    }

    let (_, _, starter_path) = catalog_paths();
    if let Ok(raw) = std::fs::read_to_string(starter_path) {
        if let Ok(items) = serde_json::from_str::<Vec<WizardStarterProfile>>(&raw) {
            for item in items {
                if item.protocol != "wireguard" && item.protocol != "vless" {
                    continue;
                }
                let exists = ingress_routes::Entity::find_by_id(item.id.clone())
                    .one(db)
                    .await
                    .ok()
                    .flatten()
                    .is_some();
                if !exists {
                    let model = ingress_routes::ActiveModel {
                        id: Set(item.id.clone()),
                        protocol: Set(item.protocol.clone()),
                        name: Set(format!("{} {}", item.protocol.to_uppercase(), item.id)),
                        entry_host: Set(item.entry.clone()),
                        entry_port: Set(item.port),
                        exit_host: Set(item.exit.clone()),
                        entry_server_id: Set(find_server_id(&item.entry)),
                        exit_server_id: Set(find_server_id(&item.exit)),
                        wg_server_public_key: Set(None),
                        wg_subnet: Set(if item.protocol == "wireguard" {
                            Some("10.79.0.0/24".into())
                        } else {
                            None
                        }),
                        sni: Set(if item.protocol == "vless" {
                            Some(item.entry.clone())
                        } else {
                            None
                        }),
                        sync_token_hash: Set(None),
                        sync_mode: Set("live".into()),
                        last_sync_at: Set(None),
                        created_at: Set(now),
                    };
                    let _ = model.insert(db).await;
                }
            }
        }
    }
}

fn route_to_dto(m: ingress_routes::Model) -> IngressRouteDto {
    IngressRouteDto {
        id: m.id,
        protocol: m.protocol,
        name: m.name,
        entry_host: m.entry_host,
        entry_port: m.entry_port,
        exit_host: m.exit_host,
        entry_server_id: m.entry_server_id,
        exit_server_id: m.exit_server_id,
        wg_server_public_key: m.wg_server_public_key,
        wg_subnet: m.wg_subnet,
        sni: m.sni,
        sync_mode: m.sync_mode,
        last_sync_at: m.last_sync_at.map(|t| t.and_utc().to_rfc3339()),
        created_at: m.created_at.and_utc().to_rfc3339(),
    }
}

fn generate_wg_keypair() -> (String, String) {
    let secret = X25519StaticSecret::random_from_rng(OsRng);
    let public = X25519PublicKey::from(&secret);
    (
        STANDARD.encode(secret.to_bytes()),
        STANDARD.encode(public.as_bytes()),
    )
}

fn allocate_wg_ip(subnet_str: &str, used_ips: &HashSet<String>) -> Option<String> {
    let net: Ipv4Net = subnet_str.parse().unwrap_or_else(|_| {
        "10.79.0.0/24"
            .parse()
            .expect("default WG subnet is valid")
    });
    let network_addr = u32::from(net.network());
    let broadcast_addr = u32::from(net.broadcast());
    // Пропускаем .0 (network), .1 (server gateway) и broadcast
    for ip_u32 in (network_addr + 2)..broadcast_addr {
        let candidate = format!("{}/32", Ipv4Addr::from(ip_u32));
        if !used_ips.contains(&candidate) {
            return Some(candidate);
        }
    }
    None
}

fn render_vless_uri(uuid: &str, route: &ingress_routes::Model, device_name: &str) -> String {
    let sni = route
        .sni
        .as_deref()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or(&route.entry_host);
    format!(
        "vless://{}@{}:{}?encryption=none&security=tls&type=tcp&sni={}#{}",
        uuid,
        route.entry_host,
        route.entry_port,
        sni,
        url_encode(device_name)
    )
}

fn render_wg_conf(
    client_private_key: &str,
    assigned_ip: &str,
    route: &ingress_routes::Model,
) -> String {
    let server_pub = route
        .wg_server_public_key
        .as_deref()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or("<SERVER_PUBLIC_KEY_PENDING_SYNC>");
    format!(
        "[Interface]\nPrivateKey = {}\nAddress = {}\nDNS = 1.1.1.1\nMTU = 1280\n\n[Peer]\nPublicKey = {}\nAllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = {}:{}\nPersistentKeepalive = 25\n",
        client_private_key, assigned_ip, server_pub, route.entry_host, route.entry_port
    )
}

async fn device_to_dto(
    db: &DatabaseConnection,
    client_template_path: &str,
    dev: user_devices::Model,
    route: Option<&ingress_routes::Model>,
    encryptor: &DbEncryptor,
) -> UserDeviceDto {
    let now = Utc::now().naive_utc();
    let traffic_state = match dev.last_traffic_at {
        None => "no_reports".to_string(),
        Some(ts) if now.signed_duration_since(ts).num_seconds() > 180 => "stale".to_string(),
        Some(_) => "live".to_string(),
    };

    let decrypted = dev
        .encrypted_secret
        .as_deref()
        .and_then(|ct| encryptor.decrypt(ct).ok());

    let (config_uri, config_text) = match dev.protocol.as_str() {
        "vless" => {
            if let Some(r) = route {
                let uuid = decrypted.as_deref().unwrap_or(&dev.credential_id);
                let uri = render_vless_uri(uuid, r, &dev.name);
                (Some(uri.clone()), Some(uri))
            } else {
                (None, None)
            }
        }
        "wireguard" => {
            if let (Some(r), Some(priv_key), Some(ip)) =
                (route, decrypted.as_deref(), dev.assigned_ip.as_deref())
            {
                let conf = render_wg_conf(priv_key, ip, r);
                (None, Some(conf))
            } else {
                (None, None)
            }
        }
        "anet" => {
            let origin = resolve_subscription_origin();
            let base = if origin.is_empty() { None } else { Some(origin) };
            match build_user_client_toml(db, client_template_path, dev.user_id, base).await {
                Ok((_, toml)) => (None, Some(toml)),
                Err(_) => (None, None),
            }
        }
        _ => (None, None),
    };

    UserDeviceDto {
        id: dev.id,
        user_id: dev.user_id,
        protocol: dev.protocol,
        name: dev.name,
        route_id: dev.route_id,
        entry_host: route.map(|r| r.entry_host.clone()),
        entry_port: route.map(|r| r.entry_port),
        exit_host: route.map(|r| r.exit_host.clone()),
        status: dev.status,
        sync_mode: route.map(|r| r.sync_mode.clone()),
        synced_at: dev.synced_at.map(|t| t.and_utc().to_rfc3339()),
        config_uri,
        config_text,
        assigned_ip: dev.assigned_ip,
        upload_bytes: dev.upload_bytes,
        download_bytes: dev.download_bytes,
        last_traffic_at: dev.last_traffic_at.map(|t| t.and_utc().to_rfc3339()),
        traffic_state,
        created_at: dev.created_at.and_utc().to_rfc3339(),
    }
}

#[OpenApi]
impl DevicesApi {
    /// Список всех входных точек VLESS / WireGuard (включая авто-обнаруженные из каталога мастера).
    #[oai(path = "/ingress-routes", method = "get")]
    async fn list_ingress_routes(&self, auth: AdminToken) -> Result<Json<IngressRoutesResponseDto>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        sync_wizard_routes(&self.db).await;

        let rows = ingress_routes::Entity::find()
            .order_by_asc(ingress_routes::Column::Name)
            .all(&self.db)
            .await
            .map_err(InternalServerError)?;

        Ok(Json(IngressRoutesResponseDto {
            origin: resolve_subscription_origin(),
            routes: rows.into_iter().map(route_to_dto).collect(),
        }))
    }

    /// Создать входную точку VLESS или WireGuard и получить одноразовый sync_token.
    #[oai(path = "/ingress-routes", method = "post")]
    async fn create_ingress_route(
        &self,
        auth: AdminToken,
        req: Json<CreateIngressRouteRequest>,
    ) -> Result<Json<CreateIngressRouteResponseDto>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;

        let proto = req.0.protocol.trim().to_lowercase();
        if proto != "vless" && proto != "wireguard" {
            return Err(Error::from_string(
                "Протокол должен быть vless или wireguard",
                StatusCode::BAD_REQUEST,
            ));
        }
        let name = req.0.name.trim().to_string();
        let entry_host = req.0.entry_host.trim().to_string();
        let exit_host = req.0.exit_host.trim().to_string();
        if name.is_empty() || entry_host.is_empty() || !(1..=65535).contains(&req.0.entry_port) {
            return Err(Error::from_string(
                "Укажите название, адрес входа и корректный порт (1..65535)",
                StatusCode::BAD_REQUEST,
            ));
        }

        let route_id = req
            .0
            .id
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| format!("{}-{}", proto, &Uuid::new_v4().simple().to_string()[..8]));

        let sync_token = generate_node_token();
        let token_hash = sha256_hex(&sync_token);
        let now = Utc::now().naive_utc();

        let wg_subnet = if proto == "wireguard" {
            Some(
                req.0
                    .wg_subnet
                    .as_deref()
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .unwrap_or("10.79.0.0/24")
                    .to_string(),
            )
        } else {
            None
        };

        let model = ingress_routes::ActiveModel {
            id: Set(route_id),
            protocol: Set(proto.clone()),
            name: Set(name),
            entry_host: Set(entry_host.clone()),
            entry_port: Set(req.0.entry_port),
            exit_host: Set(if exit_host.is_empty() {
                entry_host.clone()
            } else {
                exit_host
            }),
            entry_server_id: Set(req.0.entry_server_id),
            exit_server_id: Set(req.0.exit_server_id),
            wg_server_public_key: Set(req
                .0
                .wg_server_public_key
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())),
            wg_subnet: Set(wg_subnet),
            sni: Set(req
                .0
                .sni
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .or_else(|| if proto == "vless" { Some(entry_host) } else { None })),
            sync_token_hash: Set(Some(token_hash)),
            sync_mode: Set("live".into()),
            last_sync_at: Set(None),
            created_at: Set(now),
        };

        let saved = model.insert(&self.db).await.map_err(InternalServerError)?;
        Ok(Json(CreateIngressRouteResponseDto {
            route: route_to_dto(saved),
            sync_token,
        }))
    }

    /// Удалить входную точку.
    #[oai(path = "/ingress-routes/:id", method = "delete")]
    async fn delete_ingress_route(&self, auth: AdminToken, id: Path<String>) -> Result<()> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        let res = ingress_routes::Entity::delete_by_id(id.0)
            .exec(&self.db)
            .await
            .map_err(InternalServerError)?;
        if res.rows_affected == 0 {
            return Err(Error::from_status(StatusCode::NOT_FOUND));
        }
        Ok(())
    }

    /// Получить все устройства пользователя (VLESS / WireGuard / ANet) и статус подписки.
    #[oai(path = "/users/:id/devices", method = "get")]
    async fn list_user_devices(
        &self,
        auth: AdminToken,
        id: Path<Uuid>,
    ) -> Result<Json<UserDevicesResponseDto>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        sync_wizard_routes(&self.db).await;

        let _user = users::Entity::find_by_id(id.0)
            .one(&self.db)
            .await
            .map_err(InternalServerError)?
            .ok_or_else(|| Error::from_string("Пользователь не найден", StatusCode::NOT_FOUND))?;

        let routes: HashMap<String, ingress_routes::Model> = ingress_routes::Entity::find()
            .all(&self.db)
            .await
            .map_err(InternalServerError)?
            .into_iter()
            .map(|r| (r.id.clone(), r))
            .collect();

        let devices = user_devices::Entity::find()
            .filter(user_devices::Column::UserId.eq(id.0))
            .order_by_desc(user_devices::Column::CreatedAt)
            .all(&self.db)
            .await
            .map_err(InternalServerError)?;

        let sub_active = user_subscriptions::Entity::find_by_id(id.0)
            .one(&self.db)
            .await
            .map_err(InternalServerError)?
            .is_some();

        let encryptor = DbEncryptor::new();
        let mut dtos = Vec::with_capacity(devices.len());
        for dev in devices {
            let route = dev.route_id.as_deref().and_then(|rid| routes.get(rid));
            dtos.push(
                device_to_dto(&self.db, &self.client_template_path, dev, route, &encryptor).await,
            );
        }

        Ok(Json(UserDevicesResponseDto {
            user_id: id.0,
            subscription_active: sub_active,
            subscription_origin: resolve_subscription_origin(),
            devices: dtos,
        }))
    }

    /// Выдать новый конфиг / устройство (VLESS, WireGuard или ANet) пользователю.
    #[oai(path = "/users/:id/devices", method = "post")]
    async fn create_user_device(
        &self,
        auth: AdminToken,
        id: Path<Uuid>,
        req: Json<CreateDeviceRequestDto>,
    ) -> Result<Json<UserDeviceDto>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        sync_wizard_routes(&self.db).await;

        let user = users::Entity::find_by_id(id.0)
            .one(&self.db)
            .await
            .map_err(InternalServerError)?
            .ok_or_else(|| Error::from_string("Пользователь не найден", StatusCode::NOT_FOUND))?;

        let proto = req.0.protocol.trim().to_lowercase();
        if !matches!(proto.as_str(), "vless" | "wireguard" | "anet") {
            return Err(Error::from_string(
                "Неподдерживаемый протокол. Выберите vless, wireguard или anet",
                StatusCode::BAD_REQUEST,
            ));
        }
        let name = req.0.name.trim().to_string();
        if name.is_empty() || name.chars().count() > 80 {
            return Err(Error::from_string(
                "Введите название устройства длиной от 1 до 80 символов",
                StatusCode::BAD_REQUEST,
            ));
        }

        let route = if proto == "anet" {
            None
        } else {
            let rid = req
                .0
                .route_id
                .as_deref()
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    Error::from_string(
                        "Выберите входную точку (маршрут) для этого протокола",
                        StatusCode::BAD_REQUEST,
                    )
                })?;
            let found = ingress_routes::Entity::find_by_id(rid.to_string())
                .one(&self.db)
                .await
                .map_err(InternalServerError)?
                .ok_or_else(|| {
                    Error::from_string("Выбранная входная точка не найдена", StatusCode::NOT_FOUND)
                })?;
            if found.protocol != proto {
                return Err(Error::from_string(
                    "Протокол устройства не совпадает с протоколом выбранного входа",
                    StatusCode::BAD_REQUEST,
                ));
            }
            Some(found)
        };

        let encryptor = DbEncryptor::new();
        let device_id = Uuid::new_v4();
        let now = Utc::now().naive_utc();

        let (credential_id, assigned_ip, encrypted_secret) = match proto.as_str() {
            "vless" => {
                let r = route.as_ref().unwrap();
                // Если вход в legacy_pool-режиме и на диске есть слоты каталога, пробуем взять свободный слот
                let mut picked_uuid: Option<String> = None;
                if r.sync_mode == "legacy_pool" {
                    if let Some(cat) = read_vless_catalog() {
                        if let Some(wroute) = cat.routes.get(&r.id) {
                            let used: HashSet<String> = user_devices::Entity::find()
                                .filter(user_devices::Column::RouteId.eq(&r.id))
                                .filter(user_devices::Column::Status.eq("active"))
                                .all(&self.db)
                                .await
                                .unwrap_or_default()
                                .into_iter()
                                .map(|d| d.credential_id)
                                .collect();
                            picked_uuid = wroute
                                .devices
                                .iter()
                                .find(|u| !used.contains(*u))
                                .cloned();
                        }
                    }
                }
                let vless_uuid = picked_uuid.unwrap_or_else(|| Uuid::new_v4().to_string());
                let enc = encryptor
                    .encrypt(&vless_uuid)
                    .map_err(|e| Error::from_string(e.to_string(), StatusCode::INTERNAL_SERVER_ERROR))?;
                (vless_uuid, None, Some(enc))
            }
            "wireguard" => {
                let r = route.as_ref().unwrap();
                let (priv_b64, pub_b64) = generate_wg_keypair();
                let used_ips: HashSet<String> = user_devices::Entity::find()
                    .filter(user_devices::Column::RouteId.eq(&r.id))
                    .all(&self.db)
                    .await
                    .map_err(InternalServerError)?
                    .into_iter()
                    .filter_map(|d| d.assigned_ip)
                    .collect();
                let subnet = r.wg_subnet.as_deref().unwrap_or("10.79.0.0/24");
                let ip = allocate_wg_ip(subnet, &used_ips).ok_or_else(|| {
                    Error::from_string(
                        "В подсети WireGuard этого входа закончились свободные IP-адреса",
                        StatusCode::CONFLICT,
                    )
                })?;
                let enc = encryptor
                    .encrypt(&priv_b64)
                    .map_err(|e| Error::from_string(e.to_string(), StatusCode::INTERNAL_SERVER_ERROR))?;
                (pub_b64, Some(ip), Some(enc))
            }
            _ => (user.fingerprint.clone(), None, None),
        };

        let model = user_devices::ActiveModel {
            id: Set(device_id),
            user_id: Set(user.id),
            protocol: Set(proto),
            name: Set(name),
            route_id: Set(route.as_ref().map(|r| r.id.clone())),
            credential_id: Set(credential_id),
            assigned_ip: Set(assigned_ip),
            encrypted_secret: Set(encrypted_secret),
            status: Set("active".into()),
            upload_bytes: Set(0),
            download_bytes: Set(0),
            last_traffic_at: Set(None),
            synced_at: Set(None),
            created_at: Set(now),
        };

        let saved = model.insert(&self.db).await.map_err(InternalServerError)?;
        let dto = device_to_dto(
            &self.db,
            &self.client_template_path,
            saved,
            route.as_ref(),
            &encryptor,
        )
        .await;
        Ok(Json(dto))
    }

    /// Отозвать устройство (будет удалено с Xray / WireGuard при следующей синхронизации).
    #[oai(path = "/users/:id/devices/:device_id/revoke", method = "post")]
    async fn revoke_user_device(
        &self,
        auth: AdminToken,
        id: Path<Uuid>,
        device_id: Path<Uuid>,
    ) -> Result<()> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;

        let dev = user_devices::Entity::find_by_id(device_id.0)
            .filter(user_devices::Column::UserId.eq(id.0))
            .one(&self.db)
            .await
            .map_err(InternalServerError)?
            .ok_or_else(|| Error::from_status(StatusCode::NOT_FOUND))?;

        let mut active = dev.into_active_model();
        active.status = Set("revoked".into());
        active.update(&self.db).await.map_err(InternalServerError)?;
        Ok(())
    }

    /// Удалить устройство окончательно (освобождает IP WireGuard и исключает из синка).
    #[oai(path = "/users/:id/devices/:device_id", method = "delete")]
    async fn delete_user_device(
        &self,
        auth: AdminToken,
        id: Path<Uuid>,
        device_id: Path<Uuid>,
    ) -> Result<()> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;

        let dev = user_devices::Entity::find_by_id(device_id.0)
            .filter(user_devices::Column::UserId.eq(id.0))
            .one(&self.db)
            .await
            .map_err(InternalServerError)?
            .ok_or_else(|| Error::from_status(StatusCode::NOT_FOUND))?;

        let _ = user_device_traffic_sources::Entity::delete_many()
            .filter(user_device_traffic_sources::Column::DeviceId.eq(dev.id))
            .exec(&self.db)
            .await;
        user_devices::Entity::delete_by_id(dev.id)
            .exec(&self.db)
            .await
            .map_err(InternalServerError)?;
        Ok(())
    }

    /// Создать или перевыпустить единую HTTPS-ссылку подписки пользователя.
    #[oai(path = "/users/:id/subscription", method = "post")]
    async fn create_subscription(
        &self,
        auth: AdminToken,
        id: Path<Uuid>,
    ) -> Result<Json<SubscriptionResponseDto>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;

        let _user = users::Entity::find_by_id(id.0)
            .one(&self.db)
            .await
            .map_err(InternalServerError)?
            .ok_or_else(|| Error::from_string("Пользователь не найден", StatusCode::NOT_FOUND))?;

        let token = generate_node_token();
        let hash = sha256_hex(&token);
        let now = Utc::now().naive_utc();

        user_subscriptions::Entity::insert(user_subscriptions::ActiveModel {
            user_id: Set(id.0),
            token_hash: Set(hash),
            created_at: Set(now),
            updated_at: Set(now),
        })
        .on_conflict(
            OnConflict::column(user_subscriptions::Column::UserId)
                .update_columns([
                    user_subscriptions::Column::TokenHash,
                    user_subscriptions::Column::UpdatedAt,
                ])
                .to_owned(),
        )
        .exec_without_returning(&self.db)
        .await
        .map_err(InternalServerError)?;

        let path = format!("/api/v1/subscriptions/{}", token);
        let origin = resolve_subscription_origin();
        let url = if origin.is_empty() {
            path.clone()
        } else {
            format!("{}{}", origin, path)
        };

        Ok(Json(SubscriptionResponseDto { path, url, token }))
    }

    /// Отозвать HTTPS-ссылку подписки пользователя.
    #[oai(path = "/users/:id/subscription", method = "delete")]
    async fn revoke_subscription(&self, auth: AdminToken, id: Path<Uuid>) -> Result<()> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        let _ = user_subscriptions::Entity::delete_by_id(id.0)
            .exec(&self.db)
            .await
            .map_err(InternalServerError)?;
        Ok(())
    }

    /// Публичный эндпоинт персональной подписки (возвращает base64-список активных VLESS URI).
    #[oai(path = "/subscriptions/:token", method = "get")]
    async fn fetch_subscription(
        &self,
        token: Path<String>,
        format: Query<Option<String>>,
    ) -> Result<PlainText<String>> {
        let raw_token = token.0.trim();
        if raw_token.len() < 32 || raw_token.len() > 128 {
            return Err(Error::from_status(StatusCode::NOT_FOUND));
        }

        let hex_hash = sha256_hex(raw_token);
        let sub = user_subscriptions::Entity::find()
            .filter(user_subscriptions::Column::TokenHash.eq(&hex_hash))
            .one(&self.db)
            .await
            .map_err(InternalServerError)?
            .ok_or_else(|| Error::from_status(StatusCode::NOT_FOUND))?;

        let (user, rate_opt) = users::Entity::find_by_id(sub.user_id)
            .find_also_related(rates::Entity)
            .one(&self.db)
            .await
            .map_err(InternalServerError)?
            .ok_or_else(|| Error::from_status(StatusCode::NOT_FOUND))?;

        let now = Utc::now().naive_utc();
        if !user.is_active || rate_opt.is_some_and(|r| r.date_end < now) {
            return Err(Error::from_status(StatusCode::NOT_FOUND));
        }

        sync_wizard_routes(&self.db).await;
        let routes: HashMap<String, ingress_routes::Model> = ingress_routes::Entity::find()
            .all(&self.db)
            .await
            .map_err(InternalServerError)?
            .into_iter()
            .map(|r| (r.id.clone(), r))
            .collect();

        let devices = user_devices::Entity::find()
            .filter(user_devices::Column::UserId.eq(user.id))
            .filter(user_devices::Column::Protocol.eq("vless"))
            .filter(user_devices::Column::Status.eq("active"))
            .order_by_asc(user_devices::Column::Name)
            .all(&self.db)
            .await
            .map_err(InternalServerError)?;

        let encryptor = DbEncryptor::new();
        let mut uris = Vec::new();
        for dev in devices {
            if let Some(route) = dev.route_id.as_deref().and_then(|rid| routes.get(rid)) {
                let uuid = dev
                    .encrypted_secret
                    .as_deref()
                    .and_then(|ct| encryptor.decrypt(ct).ok())
                    .unwrap_or(dev.credential_id);
                uris.push(render_vless_uri(&uuid, route, &dev.name));
            }
        }

        if uris.is_empty() {
            return Err(Error::from_string(
                "Нет активных VLESS-устройств для подписки",
                StatusCode::NOT_FOUND,
            ));
        }

        let payload = format!("{}\n", uris.join("\n"));
        if format.0.as_deref() == Some("raw") {
            Ok(PlainText(payload))
        } else {
            Ok(PlainText(STANDARD.encode(payload.as_bytes())))
        }
    }

    /// Двусторонняя синхронизация входной VPS (VLESS / WireGuard):
    /// принимает счётчики трафика и возвращает актуальный список активных клиентов/пиров.
    #[oai(path = "/ingress-sync/:route_id", method = "post")]
    async fn ingress_sync(
        &self,
        route_id: Path<String>,
        #[oai(name = "X-Usage-Token")] usage_token: Header<Option<String>>,
        #[oai(name = "X-Node-Token")] node_token: Header<Option<String>>,
        req: Json<IngressSyncRequest>,
    ) -> Result<Json<IngressSyncResponse>> {
        sync_wizard_routes(&self.db).await;

        let route = ingress_routes::Entity::find_by_id(route_id.0.clone())
            .one(&self.db)
            .await
            .map_err(InternalServerError)?
            .ok_or_else(|| Error::from_status(StatusCode::NOT_FOUND))?;

        let mut authorized = false;
        if let (Some(expected_hash), Some(tok)) = (
            route.sync_token_hash.as_deref(),
            usage_token.0.as_deref().map(str::trim),
        ) {
            if !tok.is_empty() && matches_token_hash(expected_hash, tok) {
                authorized = true;
            }
        }
        if !authorized {
            if let (Some(srv_id), Some(tok)) = (
                route.entry_server_id,
                node_token.0.as_deref().map(str::trim),
            ) {
                if !tok.is_empty()
                    && node_token_is_valid(&self.db, srv_id, tok)
                        .await
                        .unwrap_or(false)
                {
                    authorized = true;
                }
            }
        }
        if !authorized {
            return Err(Error::from_status(StatusCode::UNAUTHORIZED));
        }

        let now = Utc::now().naive_utc();
        let mut route_active = route.clone().into_active_model();
        route_active.sync_mode = Set("live".into());
        route_active.last_sync_at = Set(Some(now));
        if let Some(ref wg_pub) = req.0.wg_server_public_key {
            let trimmed = wg_pub.trim();
            if !trimmed.is_empty() {
                route_active.wg_server_public_key = Set(Some(trimmed.to_string()));
            }
        }
        let _ = route_active.update(&self.db).await;

        let route_devices = user_devices::Entity::find()
            .filter(user_devices::Column::RouteId.eq(&route.id))
            .all(&self.db)
            .await
            .map_err(InternalServerError)?;

        let mut dev_by_key: HashMap<String, Uuid> = HashMap::new();
        for d in &route_devices {
            dev_by_key.insert(d.credential_id.clone(), d.id);
            dev_by_key.insert(d.id.to_string(), d.id);
            dev_by_key.insert(sha256_hex(&d.credential_id), d.id);
        }

        let source = req.0.source.trim().to_string();
        let mut updated_device_ids: HashSet<Uuid> = HashSet::new();
        if !source.is_empty() {
            for (key, pair) in &req.0.totals {
                if pair.len() != 2 {
                    continue;
                }
                let up = pair[0].max(0);
                let down = pair[1].max(0);
                let Some(&dev_id) = dev_by_key.get(key) else {
                    continue;
                };
                let existing = user_device_traffic_sources::Entity::find_by_id((
                    dev_id,
                    source.clone(),
                ))
                .one(&self.db)
                .await
                .map_err(InternalServerError)?;

                if let Some(prev) = existing {
                    let next_up = prev.upload_bytes.max(up);
                    let next_down = prev.download_bytes.max(down);
                    let mut active = prev.into_active_model();
                    active.upload_bytes = Set(next_up);
                    active.download_bytes = Set(next_down);
                    let _ = active.update(&self.db).await;
                } else {
                    let _ = (user_device_traffic_sources::ActiveModel {
                        device_id: Set(dev_id),
                        source: Set(source.clone()),
                        upload_bytes: Set(up),
                        download_bytes: Set(down),
                    })
                    .insert(&self.db)
                    .await;
                }
                updated_device_ids.insert(dev_id);
            }
        }

        // Обновляем суммарные счётчики по всем устройствам маршрута (включая 0 Б — чтобы было видно, что отчёт пришёл)
        for dev in &route_devices {
            let sources = user_device_traffic_sources::Entity::find()
                .filter(user_device_traffic_sources::Column::DeviceId.eq(dev.id))
                .all(&self.db)
                .await
                .unwrap_or_default();
            let total_up: i64 = sources.iter().map(|s| s.upload_bytes.max(0)).sum();
            let total_down: i64 = sources.iter().map(|s| s.download_bytes.max(0)).sum();
            let mut active = dev.clone().into_active_model();
            if updated_device_ids.contains(&dev.id) || dev.last_traffic_at.is_none() {
                active.upload_bytes = Set(total_up);
                active.download_bytes = Set(total_down);
                active.last_traffic_at = Set(Some(now));
            }
            active.synced_at = Set(Some(now));
            let _ = active.update(&self.db).await;
        }

        // Собираем только активные устройства активных и не просроченных пользователей
        let user_ids: Vec<Uuid> = route_devices
            .iter()
            .map(|d| d.user_id)
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();
        let active_user_ids: HashSet<Uuid> = if user_ids.is_empty() {
            HashSet::new()
        } else {
            users::Entity::find()
                .filter(users::Column::Id.is_in(user_ids))
                .find_also_related(rates::Entity)
                .all(&self.db)
                .await
                .unwrap_or_default()
                .into_iter()
                .filter(|(u, r)| u.is_active && !r.as_ref().is_some_and(|rate| rate.date_end < now))
                .map(|(u, _)| u.id)
                .collect()
        };

        let mut active_vless_clients = Vec::new();
        let mut active_wg_peers = Vec::new();

        for dev in route_devices {
            if dev.status != "active" || !active_user_ids.contains(&dev.user_id) {
                continue;
            }
            if dev.protocol == "vless" {
                active_vless_clients.push(ActiveVlessClientDto {
                    uuid: dev.credential_id.clone(),
                    email: format!("anet-dev-{}@anet.invalid", dev.id),
                });
            } else if dev.protocol == "wireguard" {
                if let Some(ip) = dev.assigned_ip {
                    active_wg_peers.push(ActiveWgPeerDto {
                        public_key: dev.credential_id.clone(),
                        allowed_ips: ip,
                    });
                }
            }
        }

        Ok(Json(IngressSyncResponse {
            route_id: route.id,
            protocol: route.protocol,
            active_vless_clients,
            active_wg_peers,
        }))
    }

    /// Единый агрегированный снимок живой топологии для интерактивного SVG-графа в WebUI.
    #[oai(path = "/topology/graph", method = "get")]
    async fn topology_graph(&self, auth: AdminToken) -> Result<Json<TopologyGraphDto>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        sync_wizard_routes(&self.db).await;

        let now = Utc::now().naive_utc();
        let all_servers = servers::Entity::find()
            .order_by_asc(servers::Column::Name)
            .all(&self.db)
            .await
            .map_err(InternalServerError)?;
        let runtimes: HashMap<Uuid, node_runtime_states::Model> =
            node_runtime_states::Entity::find()
                .all(&self.db)
                .await
                .unwrap_or_default()
                .into_iter()
                .map(|rt| (rt.server_id, rt))
                .collect();
        let all_routes = ingress_routes::Entity::find()
            .all(&self.db)
            .await
            .unwrap_or_default();
        let all_devices = user_devices::Entity::find()
            .all(&self.db)
            .await
            .unwrap_or_default();
        let all_users = users::Entity::find().all(&self.db).await.unwrap_or_default();
        let all_totals = traffic_totals::Entity::find()
            .all(&self.db)
            .await
            .unwrap_or_default();
        let all_sessions = active_sessions::Entity::find()
            .all(&self.db)
            .await
            .unwrap_or_default();
        let (_, edge_probes) = crate::api::probes::collect_probe_metrics(&self.db).await;

        // Агрегируем трафик по нодам и пользователям
        let mut node_rx: HashMap<Uuid, i64> = HashMap::new();
        let mut node_tx: HashMap<Uuid, i64> = HashMap::new();
        let mut user_rx: HashMap<String, i64> = HashMap::new();
        let mut user_tx: HashMap<String, i64> = HashMap::new();
        for t in &all_totals {
            *node_rx.entry(t.server_id).or_default() += t.rx_bytes.max(0);
            *node_tx.entry(t.server_id).or_default() += t.tx_bytes.max(0);
            *user_rx.entry(t.fingerprint.clone()).or_default() += t.rx_bytes.max(0);
            *user_tx.entry(t.fingerprint.clone()).or_default() += t.tx_bytes.max(0);
        }

        let mut route_traffic: HashMap<String, (i64, i64, i64)> = HashMap::new();
        let mut proto_vless: i64 = 0;
        let mut proto_wg: i64 = 0;
        let mut proto_anet: i64 = 0;
        for d in &all_devices {
            if d.status != "active" {
                continue;
            }
            match d.protocol.as_str() {
                "vless" => proto_vless += 1,
                "wireguard" => proto_wg += 1,
                "anet" => proto_anet += 1,
                _ => {}
            }
            if let Some(ref rid) = d.route_id {
                let entry = route_traffic.entry(rid.clone()).or_insert((0, 0, 0));
                entry.0 += d.upload_bytes.max(0);
                entry.1 += d.download_bytes.max(0);
                entry.2 += 1;
            }
        }

        // Роли нод из ingress_routes, probe_manifest и topology_publications
        let mut is_entry: HashSet<String> = HashSet::new();
        let mut is_exit: HashSet<String> = HashSet::new();
        let mut server_id_by_host: HashMap<String, String> = HashMap::new();
        for s in &all_servers {
            server_id_by_host.insert(s.address.trim().to_lowercase(), s.id.to_string());
        }

        let mut edges: Vec<GraphEdgeDto> = Vec::new();
        let mut seen_edges: HashSet<(String, String, String)> = HashSet::new();
        let mut extra_transports: HashMap<String, Vec<String>> = HashMap::new();
        let mut device_counts_by_node: HashMap<String, i64> = HashMap::new();
        let mut virtual_nodes: HashMap<String, (String, String, String)> = HashMap::new(); // id -> (name, role, host)

        for route in &all_routes {
            let src_id = route
                .entry_server_id
                .map(|id| id.to_string())
                .or_else(|| server_id_by_host.get(&route.entry_host.trim().to_lowercase()).cloned())
                .unwrap_or_else(|| {
                    let vid = format!("entry-{}", route.entry_host.trim().to_lowercase());
                    virtual_nodes
                        .entry(vid.clone())
                        .or_insert_with(|| (route.entry_host.clone(), "entry".into(), route.entry_host.clone()));
                    vid
                });
            let dst_id = route
                .exit_server_id
                .map(|id| id.to_string())
                .or_else(|| server_id_by_host.get(&route.exit_host.trim().to_lowercase()).cloned())
                .unwrap_or_else(|| {
                    let vid = format!("exit-{}", route.exit_host.trim().to_lowercase());
                    virtual_nodes
                        .entry(vid.clone())
                        .or_insert_with(|| (route.exit_host.clone(), "exit".into(), route.exit_host.clone()));
                    vid
                });

            is_entry.insert(src_id.clone());
            is_exit.insert(dst_id.clone());
            extra_transports
                .entry(src_id.clone())
                .or_default()
                .push(format!("{}:{}", route.protocol, route.entry_port));

            let (rx, tx, cnt) = route_traffic.get(&route.id).copied().unwrap_or((0, 0, 0));
            *device_counts_by_node.entry(src_id.clone()).or_default() += cnt;

            if src_id != dst_id && seen_edges.insert((src_id.clone(), dst_id.clone(), route.protocol.clone())) {
                let probe = edge_probes.get(&(src_id.clone(), dst_id.clone()));
                let status = match probe.and_then(|p| p.available) {
                    Some(true) if probe.and_then(|p| p.loss_pct).unwrap_or(0.0) >= 15.0 => "degraded",
                    Some(true) => "up",
                    Some(false) => "down",
                    None => if route.last_sync_at.is_some() { "up" } else { "unknown" },
                };
                edges.push(GraphEdgeDto {
                    id: format!("route-{}", route.id),
                    source: src_id,
                    target: dst_id,
                    transport: route.protocol.clone(),
                    route_id: Some(route.id.clone()),
                    status: status.to_string(),
                    latency_ms: probe.and_then(|p| p.latency_ms),
                    loss_pct: probe.and_then(|p| p.loss_pct),
                    throughput_mbps: probe.and_then(|p| p.throughput_mbps),
                    rx_bytes: rx,
                    tx_bytes: tx,
                    last_measured_at: route.last_sync_at.map(|t| t.and_utc().to_rfc3339()),
                });
            }
        }

        // Добавляем рёбра из опубликованных probe_manifest серверов
        for s in &all_servers {
            if let Some(ref raw_manifest) = s.probe_manifest {
                if let Ok(manifest) = ProbeRevision::decode(raw_manifest) {
                    let src_id = s.id.to_string();
                    for edge in manifest.edges {
                        let dst_id = edge.target_node.clone();
                        is_entry.insert(src_id.clone());
                        is_exit.insert(dst_id.clone());
                        if src_id != dst_id && seen_edges.insert((src_id.clone(), dst_id.clone(), "quic".into())) {
                            let probe = edge_probes.get(&(src_id.clone(), dst_id.clone()));
                            let status = match probe.and_then(|p| p.available) {
                                Some(true) if probe.and_then(|p| p.loss_pct).unwrap_or(0.0) >= 15.0 => "degraded",
                                Some(true) => "up",
                                Some(false) => "down",
                                None => "unknown",
                            };
                            edges.push(GraphEdgeDto {
                                id: format!("probe-{}-{}", src_id, dst_id),
                                source: src_id.clone(),
                                target: dst_id,
                                transport: "quic".into(),
                                route_id: None,
                                status: status.into(),
                                latency_ms: probe.and_then(|p| p.latency_ms),
                                loss_pct: probe.and_then(|p| p.loss_pct),
                                throughput_mbps: probe.and_then(|p| p.throughput_mbps),
                                rx_bytes: 0,
                                tx_bytes: 0,
                                last_measured_at: None,
                            });
                        }
                    }
                }
            }
        }

        // Добавляем рёбра из последней topology_publications (если опубликована)
        if let Ok(Some(pub_row)) = topology_publications::Entity::find()
            .order_by_desc(topology_publications::Column::Revision)
            .one(&self.db)
            .await
        {
            if let Ok(val) = serde_json::from_str::<serde_json::Value>(&pub_row.manifest) {
                if let Some(paths_obj) = val.get("paths").and_then(|p| p.as_object()) {
                    for (path_name, path_val) in paths_obj {
                        let Some(entry_id) = path_val.get("entry").and_then(|v| v.as_str()) else { continue };
                        let Some(exit_id) = path_val.get("exit").and_then(|v| v.as_str()) else { continue };
                        let transport = path_val
                            .get("transport")
                            .and_then(|v| v.as_str())
                            .unwrap_or("quic")
                            .to_string();
                        is_entry.insert(entry_id.to_string());
                        is_exit.insert(exit_id.to_string());
                        if entry_id != exit_id
                            && seen_edges.insert((entry_id.to_string(), exit_id.to_string(), transport.clone()))
                        {
                            let probe = edge_probes.get(&(entry_id.to_string(), exit_id.to_string()));
                            let status = match probe.and_then(|p| p.available) {
                                Some(true) => "up",
                                Some(false) => "down",
                                None => "unknown",
                            };
                            edges.push(GraphEdgeDto {
                                id: format!("topo-{}", path_name),
                                source: entry_id.to_string(),
                                target: exit_id.to_string(),
                                transport,
                                route_id: Some(path_name.clone()),
                                status: status.into(),
                                latency_ms: probe.and_then(|p| p.latency_ms),
                                loss_pct: probe.and_then(|p| p.loss_pct),
                                throughput_mbps: probe.and_then(|p| p.throughput_mbps),
                                rx_bytes: 0,
                                tx_bytes: 0,
                                last_measured_at: None,
                            });
                        }
                    }
                }
            }
        }

        let mut nodes: Vec<GraphNodeDto> = Vec::new();
        for s in &all_servers {
            let sid = s.id.to_string();
            let rt = runtimes.get(&s.id);
            let heartbeat_age_sec = rt.map(|r| now.signed_duration_since(r.last_seen_at).num_seconds().max(0));
            let is_online = heartbeat_age_sec.is_some_and(|age| age <= 45);
            let accepting = rt.map_or(true, |r| r.accepting_connections);
            let status = if rt.is_none() {
                "unknown"
            } else if !is_online {
                "offline"
            } else if !accepting {
                "draining"
            } else {
                "online"
            };

            let role = match (is_entry.contains(&sid), is_exit.contains(&sid)) {
                (true, true) => "both",
                (true, false) => "entry",
                (false, true) => "exit",
                (false, false) => "both",
            };

            let mut transports = Vec::new();
            if let Some(p) = s.quic_port.filter(|&p| p > 0) {
                transports.push(format!("quic:{}", p));
            }
            if s.websocket_url.as_deref().is_some_and(|u| !u.trim().is_empty()) {
                transports.push("ws".into());
            }
            if s.ahttp_url.as_deref().is_some_and(|u| !u.trim().is_empty()) {
                transports.push("ahttp".into());
            }
            if s.wrtc_url.as_deref().is_some_and(|u| !u.trim().is_empty()) {
                transports.push("wrtc".into());
            }
            if let Some(p) = s.ssh_port.filter(|&p| p > 0) {
                transports.push(format!("ssh:{}", p));
            }
            if let Some(extra) = extra_transports.get(&sid) {
                for item in extra {
                    if !transports.contains(item) {
                        transports.push(item.clone());
                    }
                }
            }

            nodes.push(GraphNodeDto {
                id: sid.clone(),
                name: s.name.clone(),
                role: role.into(),
                address: s.address.clone(),
                is_active: s.is_active,
                status: status.into(),
                heartbeat_age_sec,
                uptime_seconds: rt.map(|r| r.uptime_seconds.max(0)),
                active_connections: rt.map(|r| r.active_connections.max(0)).unwrap_or(0),
                accepting_connections: accepting,
                version: rt.map(|r| r.version.clone()),
                transports,
                rx_bytes: node_rx.get(&s.id).copied().unwrap_or(0),
                tx_bytes: node_tx.get(&s.id).copied().unwrap_or(0),
                device_count: device_counts_by_node.get(&sid).copied().unwrap_or(0),
                probe_revision: s.probe_revision,
                probe_applied: s.probe_applied,
                probe_error: s.probe_error.clone(),
            });
        }

        // Добавляем виртуальные узлы (входы/выходы из ingress_routes без записи в servers)
        for (vid, (vname, vrole, vhost)) in virtual_nodes {
            nodes.push(GraphNodeDto {
                id: vid.clone(),
                name: vname,
                role: vrole,
                address: vhost,
                is_active: true,
                status: "unknown".into(),
                heartbeat_age_sec: None,
                uptime_seconds: None,
                active_connections: 0,
                accepting_connections: true,
                version: None,
                transports: extra_transports.remove(&vid).unwrap_or_default(),
                rx_bytes: 0,
                tx_bytes: 0,
                device_count: device_counts_by_node.get(&vid).copied().unwrap_or(0),
                probe_revision: 0,
                probe_applied: 0,
                probe_error: None,
            });
        }

        // Формируем пути пользователей для подсветки на графе
        let edge_by_route: HashMap<String, &GraphEdgeDto> = edges
            .iter()
            .filter_map(|e| e.route_id.as_ref().map(|rid| (rid.clone(), e)))
            .collect();
        let mut devices_by_user: HashMap<Uuid, Vec<&user_devices::Model>> = HashMap::new();
        for d in &all_devices {
            if d.status == "active" {
                devices_by_user.entry(d.user_id).or_default().push(d);
            }
        }

        let mut user_paths = Vec::with_capacity(all_users.len());
        let mut active_users_cnt = 0_i64;
        for u in &all_users {
            if u.is_active {
                active_users_cnt += 1;
            }
            let mut protocols_set: HashSet<String> = HashSet::new();
            let mut node_ids_set: HashSet<String> = HashSet::new();
            let mut edge_ids_set: HashSet<String> = HashSet::new();
            let mut rx = user_rx.get(&u.fingerprint).copied().unwrap_or(0);
            let mut tx = user_tx.get(&u.fingerprint).copied().unwrap_or(0);

            if let Some(devs) = devices_by_user.get(&u.id) {
                for d in devs {
                    protocols_set.insert(d.protocol.clone());
                    rx += d.upload_bytes.max(0);
                    tx += d.download_bytes.max(0);
                    if let Some(ref rid) = d.route_id {
                        if let Some(edge) = edge_by_route.get(rid) {
                            edge_ids_set.insert(edge.id.clone());
                            node_ids_set.insert(edge.source.clone());
                            node_ids_set.insert(edge.target.clone());
                        }
                    }
                }
            }
            if protocols_set.is_empty() {
                protocols_set.insert("anet".into());
            }

            user_paths.push(UserGraphPathDto {
                user_id: u.id,
                uid: u.uid.clone().unwrap_or_else(|| u.id.to_string()),
                is_active: u.is_active,
                protocols: protocols_set.into_iter().collect(),
                node_ids: node_ids_set.into_iter().collect(),
                edge_ids: edge_ids_set.into_iter().collect(),
                rx_bytes: rx,
                tx_bytes: tx,
            });
        }

        let total_active_sessions: i64 = all_sessions.iter().map(|s| s.sessions.max(0) as i64).sum();

        Ok(Json(TopologyGraphDto {
            generated_at: now.and_utc().to_rfc3339(),
            nodes,
            edges,
            clients_summary: ClientsSummaryDto {
                total_users: all_users.len() as i64,
                active_users: active_users_cnt,
                active_sessions: total_active_sessions,
                by_protocol: ProtocolCountsDto {
                    vless: proto_vless,
                    wireguard: proto_wg,
                    anet: proto_anet.max(active_users_cnt),
                },
            },
            user_paths,
        }))
    }
}
