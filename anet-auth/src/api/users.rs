use crate::api::api::{
    compiled_routes_for_user, enqueue_disconnect_on_active_servers, matches_token_hash,
    resolve_client_server_entries, url_encode, user_pool_ids, user_route_map_id,
    validate_admin_session,
};
use crate::api::dto::{
    AddRateApiResult, AddRateRequest, AddUserApiResult, AddUserRequest, AddUserResponse,
    AdminToken, DeleteRateApiResult, DeleteUserApiResult, DeleteUserDto, DownloadConfigResponse,
    GetUserApiResult, GetUsersResponse, InternalUserStatusDto, InternalUserStatusResponse,
    PaginatedUsers, QrPageResponse, RateDto, RegenerateUserApiResult, RegenerateUserResponse,
    ResetTrafficApiResult, ResetTrafficDto, UpdateRateApiResult, UpdateRateRequest,
    UpdateUserApiResult, UpdateUserRequest, VpnUserDto,
};
use crate::crypto::DbEncryptor;
use crate::entities::{
    active_sessions, group_node_pools, groups, node_pool_members, node_pools, node_runtime_states,
    rates, servers, traffic_hourly, traffic_totals, user_device_traffic_sources, user_devices,
    user_node_pools, user_servers, user_subscriptions, users, ProtocolType,
};
use crate::route_compiler::toml_string_array;
use chrono::{NaiveDateTime, Utc};
use log::{error, info, warn};
use poem_openapi::{
    param::{Header, Path, Query},
    payload::{Json, PlainText},
    OpenApi,
};
use sea_orm::{
    ActiveModelTrait, ColumnTrait, DatabaseConnection, EntityTrait, IntoActiveModel,
    PaginatorTrait, QueryFilter, QueryOrder, QuerySelect, Set, TransactionTrait,
};
use std::collections::{HashMap, HashSet};
use uuid::Uuid;

pub struct UsersApi {
    pub db: DatabaseConnection,
    pub client_template_path: String,
}

/// Гибкий парсинг даты окончания тарифа: поддерживает `datetime-local` (`YYYY-MM-DDTHH:MM`)
/// и исторический формат `YYYY-MM-DD-HH:MM`.
pub fn parse_rate_date(raw: &str) -> Option<NaiveDateTime> {
    let trimmed = raw.trim();
    const FORMATS: &[&str] = &[
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d-%H:%M",
        "%Y-%m-%d-%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
    ];
    for fmt in FORMATS {
        if let Ok(dt) = NaiveDateTime::parse_from_str(trimmed, fmt) {
            return Some(dt);
        }
    }
    if let Ok(rfc) = chrono::DateTime::parse_from_rfc3339(trimmed) {
        return Some(rfc.naive_utc());
    }
    None
}

async fn validate_config_access(
    db: &DatabaseConnection,
    user_id: Uuid,
    auth_header: Option<&str>,
    query_token: Option<&str>,
) -> bool {
    if let Some(hdr) = auth_header {
        if let Some(jwt) = hdr.strip_prefix("Bearer ").map(str::trim) {
            if !jwt.is_empty() && validate_admin_session(db, jwt).await.is_ok() {
                return true;
            }
        }
    }
    if let Some(raw_token) = query_token.map(str::trim).filter(|t| !t.is_empty()) {
        if let Ok(Some(sub)) = user_subscriptions::Entity::find_by_id(user_id).one(db).await {
            if matches_token_hash(&sub.token_hash, raw_token) {
                return true;
            }
        }
    }
    false
}

/// Собирает готовый `client.toml` для пользователя с использованием реального балансировщика
/// (`crate::balancer::order_candidates`) и сохранением per-protocol метаданных.
pub async fn build_user_client_toml(
    db: &DatabaseConnection,
    client_template_path: &str,
    user_id: Uuid,
    base_auth_url: Option<String>,
) -> Result<(String, String), (poem::http::StatusCode, String)> {
    let (user_opt, assigned_servers) = match users::Entity::find_by_id(user_id)
        .find_with_related(servers::Entity)
        .all(db)
        .await
    {
        Ok(mut list) => {
            if list.is_empty() {
                return Err((poem::http::StatusCode::NOT_FOUND, "Client not found".into()));
            }
            list.remove(0)
        }
        Err(e) => {
            error!("[CONFIG] Failed to fetch user {}: {}", user_id, e);
            return Err((
                poem::http::StatusCode::INTERNAL_SERVER_ERROR,
                "Database error".into(),
            ));
        }
    };

    if !user_opt.is_active {
        return Err((
            poem::http::StatusCode::NOT_FOUND,
            "Client is inactive or banned".into(),
        ));
    }

    let compiled_routes = compiled_routes_for_user(db, user_opt.id)
        .await
        .map_err(|_| {
            (
                poem::http::StatusCode::INTERNAL_SERVER_ERROR,
                "Failed to compile route map".into(),
            )
        })?;

    let encryptor = DbEncryptor::new();
    let decrypted_private_key = match user_opt.private_key {
        Some(ref enc_pk) => encryptor.decrypt(enc_pk).map_err(|_| {
            (
                poem::http::StatusCode::INTERNAL_SERVER_ERROR,
                "Failed to decrypt user credentials".into(),
            )
        })?,
        None => {
            return Err((
                poem::http::StatusCode::INTERNAL_SERVER_ERROR,
                "Private key is missing in DB".into(),
            ));
        }
    };

    let mut group_based_servers_toml: Option<String> = None;
    let mut group_fallback_pub_key: Option<String> = None;

    if let Some(user_group_id) = user_opt.group_id {
        let linked_pool_ids: Vec<Uuid> = group_node_pools::Entity::find()
            .filter(group_node_pools::Column::GroupId.eq(user_group_id))
            .all(db)
            .await
            .unwrap_or_default()
            .into_iter()
            .map(|gp| gp.pool_id)
            .collect();

        if !linked_pool_ids.is_empty() {
            let active_pools = node_pools::Entity::find()
                .filter(node_pools::Column::Id.is_in(linked_pool_ids))
                .filter(node_pools::Column::IsActive.eq(true))
                .order_by_asc(node_pools::Column::Name)
                .all(db)
                .await
                .unwrap_or_default();

            if !active_pools.is_empty() {
                let active_pool_ids: Vec<Uuid> = active_pools.iter().map(|p| p.id).collect();
                let all_members = node_pool_members::Entity::find()
                    .filter(node_pool_members::Column::PoolId.is_in(active_pool_ids))
                    .all(db)
                    .await
                    .unwrap_or_default();

                if !all_members.is_empty() {
                    let imap_member_box_ids: Vec<Uuid> = all_members
                        .iter()
                        .filter(|m| m.protocol == ProtocolType::Imap)
                        .filter_map(|m| m.port_or_url.as_deref())
                        .flat_map(|s| s.split(','))
                        .filter_map(|s| Uuid::parse_str(s.trim()).ok())
                        .collect();

                    let imap_mailbox_map: HashMap<Uuid, crate::entities::imap_mailboxes::Model> =
                        if !imap_member_box_ids.is_empty() {
                            crate::entities::imap_mailboxes::Entity::find()
                                .filter(
                                    crate::entities::imap_mailboxes::Column::Id
                                        .is_in(imap_member_box_ids),
                                )
                                .filter(crate::entities::imap_mailboxes::Column::IsActive.eq(true))
                                .all(db)
                                .await
                                .unwrap_or_default()
                                .into_iter()
                                .map(|m| (m.id, m))
                                .collect()
                        } else {
                            HashMap::new()
                        };

                    let member_server_ids: Vec<Uuid> = all_members
                        .iter()
                        .map(|m| m.server_id)
                        .collect::<HashSet<_>>()
                        .into_iter()
                        .collect();
                    let pool_servers = servers::Entity::find()
                        .filter(servers::Column::Id.is_in(member_server_ids.clone()))
                        .filter(servers::Column::IsActive.eq(true))
                        .all(db)
                        .await
                        .unwrap_or_default();
                    let server_map: HashMap<Uuid, servers::Model> =
                        pool_servers.into_iter().map(|s| (s.id, s)).collect();

                    let runtime_by_id: HashMap<Uuid, node_runtime_states::Model> =
                        node_runtime_states::Entity::find()
                            .filter(node_runtime_states::Column::ServerId.is_in(member_server_ids))
                            .all(db)
                            .await
                            .unwrap_or_default()
                            .into_iter()
                            .map(|rt| (rt.server_id, rt))
                            .collect();
                    let (probe_metrics, _) = crate::api::probes::collect_probe_metrics(db).await;
                    let now = Utc::now().naive_utc();

                    let mut members_by_pool: HashMap<Uuid, Vec<node_pool_members::Model>> =
                        HashMap::new();
                    for m in all_members {
                        members_by_pool.entry(m.pool_id).or_default().push(m);
                    }

                    let mut toml_str = String::new();
                    let mut fallback_key = String::new();
                    let mut emitted: HashSet<(Uuid, Uuid, ProtocolType, String)> = HashSet::new();

                    for pool in &active_pools {
                        let Some(pool_members) = members_by_pool.remove(&pool.id) else {
                            continue;
                        };
                        let mut by_server: HashMap<Uuid, Vec<node_pool_members::Model>> =
                            HashMap::new();
                        for member in pool_members {
                            if server_map.contains_key(&member.server_id) {
                                by_server.entry(member.server_id).or_default().push(member);
                            }
                        }

                        let mut candidates: Vec<crate::balancer::BalanceCandidate> = by_server
                            .iter()
                            .map(|(&server_id, srv_members)| {
                                let weight = srv_members
                                    .iter()
                                    .map(|m| m.weight.max(1) as u32)
                                    .max()
                                    .unwrap_or(1);
                                let rt = runtime_by_id.get(&server_id);
                                let has_runtime = rt.is_some();
                                let is_online = rt.is_some_and(|r| {
                                    now.signed_duration_since(r.last_seen_at).num_seconds() <= 45
                                });
                                let accepting_connections =
                                    rt.map_or(true, |r| r.accepting_connections);
                                let active_connections = rt
                                    .map(|r| r.active_connections.max(0) as u64)
                                    .unwrap_or(0);
                                let metrics = probe_metrics.get(&server_id);
                                crate::balancer::BalanceCandidate {
                                    server_id,
                                    weight,
                                    active_connections,
                                    has_runtime,
                                    is_online,
                                    accepting_connections,
                                    latency_ms: metrics.and_then(|m| m.latency_ms),
                                    loss_pct: metrics.and_then(|m| m.loss_pct),
                                    throughput_mbps: metrics.and_then(|m| m.throughput_mbps),
                                }
                            })
                            .collect();

                        crate::balancer::order_candidates(
                            &pool.strategy,
                            user_opt.id,
                            pool.id,
                            &mut candidates,
                        );

                        for cand in candidates {
                            let Some(server) = server_map.get(&cand.server_id) else {
                                continue;
                            };
                            if server.address.trim().is_empty() {
                                continue;
                            }
                            let Some(mut srv_members) = by_server.remove(&cand.server_id) else {
                                continue;
                            };
                            srv_members.sort_by(|a, b| {
                                b.weight
                                    .cmp(&a.weight)
                                    .then_with(|| a.protocol.as_str().cmp(b.protocol.as_str()))
                            });

                            for member in srv_members {
                                let custom_endpoint = member
                                    .port_or_url
                                    .as_deref()
                                    .map(str::trim)
                                    .filter(|s| !s.is_empty());

                                let (proto_str, port_or_url) = match member.protocol {
                                    ProtocolType::Quic => (
                                        "quic",
                                        custom_endpoint.map(str::to_string).or_else(|| {
                                            server
                                                .quic_port
                                                .filter(|&p| p > 0)
                                                .map(|p| p.to_string())
                                        }),
                                    ),
                                    ProtocolType::Ssh => (
                                        "ssh",
                                        custom_endpoint.map(str::to_string).or_else(|| {
                                            server
                                                .ssh_port
                                                .filter(|&p| p > 0)
                                                .map(|p| p.to_string())
                                        }),
                                    ),
                                    ProtocolType::Vnc => (
                                        "vnc",
                                        custom_endpoint.map(str::to_string).or_else(|| {
                                            server
                                                .vnc_port
                                                .filter(|&p| p > 0)
                                                .map(|p| p.to_string())
                                        }),
                                    ),
                                    ProtocolType::Ws => {
                                        let url = custom_endpoint.map(str::to_string).or_else(
                                            || {
                                                server
                                                    .websocket_url
                                                    .clone()
                                                    .filter(|u| !u.trim().is_empty())
                                            },
                                        );
                                        let proto = match url.as_deref() {
                                            Some(u) if u.starts_with("ws://") => "ws",
                                            _ => "wss",
                                        };
                                        (proto, url)
                                    }
                                    ProtocolType::Ahttp => {
                                        let url = custom_endpoint.map(str::to_string).or_else(
                                            || {
                                                server
                                                    .ahttp_url
                                                    .clone()
                                                    .filter(|u| !u.trim().is_empty())
                                            },
                                        );
                                        let proto = match url.as_deref() {
                                            Some(u) if u.starts_with("http://") => "http",
                                            _ => "https",
                                        };
                                        (proto, url)
                                    }
                                    ProtocolType::Wrtc => (
                                        "wrtc",
                                        custom_endpoint.map(str::to_string).or_else(|| {
                                            server
                                                .wrtc_url
                                                .clone()
                                                .filter(|u| !u.trim().is_empty())
                                        }),
                                    ),
                                    ProtocolType::Imap => {
                                        let dsn = custom_endpoint.and_then(|custom| {
                                            if custom.starts_with("imap://") {
                                                Some(custom.to_string())
                                            } else {
                                                let boxes: Vec<&_> = custom
                                                    .split(',')
                                                    .filter_map(|s| Uuid::parse_str(s.trim()).ok())
                                                    .filter_map(|id| imap_mailbox_map.get(&id))
                                                    .collect();
                                                let first_box = boxes.first()?;
                                                let mut acc_params = Vec::new();
                                                for mb in &boxes {
                                                    if let Ok(pwd) = encryptor
                                                        .decrypt(&mb.client_password_encrypted)
                                                    {
                                                        acc_params.push(format!(
                                                            "acc={}:{}",
                                                            url_encode(&mb.email),
                                                            url_encode(&pwd)
                                                        ));
                                                    }
                                                }
                                                if acc_params.is_empty() {
                                                    None
                                                } else {
                                                    Some(format!(
                                                        "imap://{}:{}?{}&batch_ms=250",
                                                        first_box.host,
                                                        first_box.port,
                                                        acc_params.join("&")
                                                    ))
                                                }
                                            }
                                        });
                                        ("imap", dsn)
                                    }
                                    _ => ("", None),
                                };

                                let Some(port_or_url) = port_or_url else {
                                    continue;
                                };
                                let dsn = if port_or_url.contains("://") {
                                    port_or_url
                                } else {
                                    format!("{}://{}:{}", proto_str, server.address, port_or_url)
                                };
                                let weight = member.weight.max(1);
                                if emitted.insert((pool.id, server.id, member.protocol, dsn.clone()))
                                {
                                    if fallback_key.is_empty() {
                                        fallback_key = server.public_key.clone();
                                    }
                                    let ssh_user = server
                                        .ssh_user
                                        .as_deref()
                                        .map(|u| format!("ssh_user = \"{}\"\n", u))
                                        .unwrap_or_default();
                                    let wrtc_mode_line = if member.protocol == ProtocolType::Wrtc {
                                        let mode = member
                                            .wrtc_mode
                                            .as_deref()
                                            .or_else(|| server.wrtc_mode.as_deref())
                                            .unwrap_or("media_video");
                                        format!("wrtc_mode = \"{}\"\n", mode)
                                    } else {
                                        String::new()
                                    };
                                    let display_name = format!(
                                        "{} [{}]",
                                        server.name.trim(),
                                        member.protocol.as_str().to_uppercase()
                                    );
                                    toml_str.push_str(&format!(
                                        "[[servers]]\nname = \"{}\"\ndsn = \"{}\"\n{}{}timeout_secs = 8\nserver_pub_key = \"{}\"\ngroup_name = \"{}\"\ngroup_id = \"{}\"\nweight = {}\n\n",
                                        display_name, dsn, ssh_user, wrtc_mode_line, server.public_key, pool.name, pool.id, weight
                                    ));
                                }
                            }
                        }
                    }

                    if !toml_str.is_empty() {
                        group_based_servers_toml = Some(toml_str);
                        group_fallback_pub_key = Some(fallback_key);
                    }
                }
            }
        }
    }

    let (servers_toml, fallback_pub_key) = if let (Some(toml), Some(fb_key)) =
        (group_based_servers_toml, group_fallback_pub_key)
    {
        let mut header = String::from(
            "\n# =========================================================================\n# ANET Client: Balanced Group Entry Point Nodes\n# =========================================================================\n",
        );
        header.push_str(&toml);
        (header, fb_key)
    } else {
        let entries = resolve_client_server_entries(db, user_opt.id, assigned_servers)
            .await
            .map_err(|_| {
                (
                    poem::http::StatusCode::INTERNAL_SERVER_ERROR,
                    "Failed to resolve node pools".into(),
                )
            })?;

        if entries.is_empty() {
            return Err((
                poem::http::StatusCode::BAD_REQUEST,
                "No available servers for this user".into(),
            ));
        }

        let mut servers_toml = String::from(
            "\n# =========================================================================\n# ANET Client: Load-balanced Entry Point + Failover Nodes\n# =========================================================================\n",
        );
        let mut fallback_pub_key = String::new();
        let mut emitted: HashSet<(Uuid, String)> = HashSet::new();

        for entry in entries {
            let server = entry.server;
            if !server.is_active || server.address.trim().is_empty() {
                continue;
            }
            if fallback_pub_key.is_empty() {
                fallback_pub_key = server.public_key.clone();
            }
            let ssh_user = server
                .ssh_user
                .as_deref()
                .map(|user| format!("ssh_user = \"{}\"\n", user))
                .unwrap_or_default();

            let mut push_block = |protocol: &str, port_or_url: &str, extra: &str| {
                let dsn = if port_or_url.contains("://") {
                    port_or_url.to_string()
                } else {
                    format!("{}://{}:{}", protocol, server.address, port_or_url)
                };
                if !emitted.insert((server.id, dsn.clone())) {
                    return;
                }
                let display_name = format!("{} [{}]", server.name.trim(), protocol.to_uppercase());
                let pool_meta = match (&entry.pool_name, entry.pool_id) {
                    (Some(name), Some(pid)) => {
                        format!("group_name = \"{}\"\ngroup_id = \"{}\"\nweight = {}\n", name, pid, entry.weight)
                    }
                    _ => String::new(),
                };
                servers_toml.push_str(&format!(
                    "[[servers]]\nname = \"{}\"\ndsn = \"{}\"\n{}{}timeout_secs = 8\nserver_pub_key = \"{}\"\n{}\n",
                    display_name, dsn, ssh_user, extra, server.public_key, pool_meta
                ));
            };

            if let Some(proto) = entry.protocol {
                let custom = entry
                    .port_or_url
                    .as_deref()
                    .map(str::trim)
                    .filter(|s| !s.is_empty());
                match proto {
                    ProtocolType::Quic => {
                        if let Some(p) = custom
                            .map(str::to_string)
                            .or_else(|| server.quic_port.filter(|&p| p > 0).map(|p| p.to_string()))
                        {
                            push_block("quic", &p, "");
                        }
                    }
                    ProtocolType::Ssh => {
                        if let Some(p) = custom
                            .map(str::to_string)
                            .or_else(|| server.ssh_port.filter(|&p| p > 0).map(|p| p.to_string()))
                        {
                            push_block("ssh", &p, "");
                        }
                    }
                    ProtocolType::Vnc => {
                        if let Some(p) = custom
                            .map(str::to_string)
                            .or_else(|| server.vnc_port.filter(|&p| p > 0).map(|p| p.to_string()))
                        {
                            push_block("vnc", &p, "");
                        }
                    }
                    ProtocolType::Ws => {
                        if let Some(u) = custom
                            .map(str::to_string)
                            .or_else(|| server.websocket_url.clone().filter(|u| !u.trim().is_empty()))
                        {
                            let p = if u.starts_with("ws://") { "ws" } else { "wss" };
                            push_block(p, &u, "");
                        }
                    }
                    ProtocolType::Ahttp => {
                        if let Some(u) = custom
                            .map(str::to_string)
                            .or_else(|| server.ahttp_url.clone().filter(|u| !u.trim().is_empty()))
                        {
                            let p = if u.starts_with("http://") { "http" } else { "https" };
                            push_block(p, &u, "");
                        }
                    }
                    ProtocolType::Wrtc => {
                        if let Some(u) = custom
                            .map(str::to_string)
                            .or_else(|| server.wrtc_url.clone().filter(|u| !u.trim().is_empty()))
                        {
                            let mode = entry
                                .wrtc_mode
                                .as_deref()
                                .or_else(|| server.wrtc_mode.as_deref())
                                .unwrap_or("media_video");
                            push_block("wrtc", &u, &format!("wrtc_mode = \"{}\"\n", mode));
                        }
                    }
                    _ => {}
                }
            } else {
                if let Some(ref ahttp) = server.ahttp_url {
                    if !ahttp.trim().is_empty() {
                        let proto = if ahttp.starts_with("http://") { "http" } else { "https" };
                        push_block(proto, ahttp, "");
                    }
                }
                if let Some(ref wrtc) = server.wrtc_url {
                    if !wrtc.trim().is_empty() {
                        let mode = server.wrtc_mode.as_deref().unwrap_or("media_video");
                        push_block("wrtc", wrtc, &format!("wrtc_mode = \"{}\"\n", mode));
                    }
                }
                if let Some(quic) = server.quic_port.filter(|&p| p > 0) {
                    push_block("quic", &quic.to_string(), "");
                }
                if let Some(ref ws) = server.websocket_url {
                    if !ws.trim().is_empty() {
                        let proto = if ws.starts_with("ws://") { "ws" } else { "wss" };
                        push_block(proto, ws, "");
                    }
                }
                if let Some(ssh) = server.ssh_port.filter(|&p| p > 0) {
                    push_block("ssh", &ssh.to_string(), "");
                }
                if let Some(vnc) = server.vnc_port.filter(|&p| p > 0) {
                    push_block("vnc", &vnc.to_string(), "");
                }
            }
        }
        (servers_toml, fallback_pub_key)
    };

    let template_content = tokio::fs::read_to_string(client_template_path)
        .await
        .unwrap_or_else(|_| {
            "[keys]\nprivate_key = \"{{PRIVATE_KEY}}\"\nserver_pub_key = \"{{SERVER_PUB_KEY}}\"\n"
                .to_string()
        });

    let computed_rules_url = base_auth_url
        .as_ref()
        .map(|base| {
            let clean_base = base.trim_end_matches('/');
            if let Some(rm_id) = user_opt.route_map_id {
                format!("{}/api/v1/routing/compiled-rules/{}", clean_base, rm_id)
            } else {
                format!("{}/api/v1/routing/compiled-rules", clean_base)
            }
        })
        .unwrap_or_default();

    let sys_settings = crate::api::alerts_and_sites::load_system_settings(db).await;

    let mut config_output = template_content;
    if !config_output.contains("{{RULES_URL}}") && !config_output.contains("[routing]") {
        config_output.push_str(&format!(
            "\n[routing]\nrules_url = \"{}\"\n",
            computed_rules_url
        ));
    }
    config_output.push_str(&format!(
        "\n[acceleration]\noperating_mode = \"{}\"\nmulti_exit_enabled = {}\nparallel_transports = {}\nauto_failover = {}\n",
        sys_settings.operating_mode,
        sys_settings.multi_exit_enabled,
        sys_settings.parallel_transports,
        sys_settings.auto_failover
    ));
    config_output.push_str(&servers_toml);

    let final_output = config_output
        .replace("{{PRIVATE_KEY}}", &decrypted_private_key)
        .replace("{{SERVER_PUB_KEY}}", &fallback_pub_key)
        .replace("{{RULES_URL}}", &computed_rules_url);
    let final_output = if let Some(routes) = compiled_routes {
        final_output
            .replace("{{ROUTE_FOR}}", &toml_string_array(&routes.route_for))
            .replace(
                "{{EXCLUDE_ROUTE_FOR}}",
                &toml_string_array(&routes.exclude_route_for),
            )
            .replace("{{PER_APP}}", &toml_string_array(&routes.per_app))
            .replace("{{PER_APP_MODE}}", &routes.per_app_mode)
    } else {
        final_output
            .replace("{{ROUTE_FOR}}", "[]")
            .replace("{{EXCLUDE_ROUTE_FOR}}", "[\"192.168.1.0/24\"]")
            .replace("{{PER_APP}}", "[]")
            .replace("{{PER_APP_MODE}}", "all")
    };

    let client_name = user_opt.uid.unwrap_or_else(|| "client".to_string());
    Ok((client_name, final_output))
}

#[OpenApi]
impl UsersApi {
    /// Получить список всех пользователей (с поиском, фильтром групп и сортировкой)
    #[oai(path = "/users", method = "get")]
    async fn get_users(
        &self,
        auth: AdminToken,
        from: Query<Option<i64>>,
        limit: Query<Option<i64>>,
        search: Query<Option<String>>,
        group_ids: Query<Option<String>>,
        sort_by: Query<Option<String>>,
        descending: Query<Option<bool>>,
    ) -> GetUsersResponse {
        if let Err(deny_reason) = validate_admin_session(&self.db, &auth.0.token).await {
            return GetUsersResponse::Unauthorized(Json(deny_reason));
        }

        let offset = from.0.unwrap_or(0).max(0) as u64;
        let page_size = limit.0.unwrap_or(50).clamp(1, 500) as u64;

        let mut query = users::Entity::find().find_also_related(rates::Entity);

        if let Some(ref s) = search.0 {
            if !s.trim().is_empty() {
                let pattern = format!("%{}%", s.trim());
                query = query.filter(
                    sea_orm::Condition::any()
                        .add(users::Column::Uid.ilike(&pattern))
                        .add(users::Column::Fingerprint.ilike(&pattern)),
                );
            }
        }

        if let Some(ref g_ids) = group_ids.0 {
            if !g_ids.trim().is_empty() {
                let parsed_ids: Vec<Uuid> = g_ids
                    .split(',')
                    .filter_map(|s| Uuid::parse_str(s.trim()).ok())
                    .collect();
                if !parsed_ids.is_empty() {
                    query = query.filter(users::Column::GroupId.is_in(parsed_ids));
                }
            }
        }

        let count = match query.clone().count(&self.db).await {
            Ok(c) => c as i64,
            Err(e) => return GetUsersResponse::Error(Json(e.to_string())),
        };

        let order_col = match sort_by.0.as_deref() {
            Some("uid") => users::Column::Uid,
            Some("id") => users::Column::Id,
            Some("is_active") => users::Column::IsActive,
            _ => users::Column::CreatedAt,
        };

        query = if descending.0.unwrap_or(false) {
            query.order_by_desc(order_col)
        } else {
            query.order_by_asc(order_col)
        };

        let users_list = match query.offset(offset).limit(page_size).all(&self.db).await {
            Ok(list) => list,
            Err(e) => return GetUsersResponse::Error(Json(e.to_string())),
        };

        let dto_list = users_list
            .into_iter()
            .map(|(m, r)| VpnUserDto {
                id: m.id,
                fingerprint: m.fingerprint,
                uid: m.uid,
                is_active: m.is_active,
                created_at: m.created_at.format("%Y-%m-%d %H:%M:%S").to_string(),
                rate: r.map(|rate_model| RateDto {
                    id: rate_model.id,
                    sessions: rate_model.sessions,
                    date_end: rate_model.date_end.format("%Y-%m-%dT%H:%M").to_string(),
                }),
                static_ip: m.static_ip.and_then(|ip| ip.parse().ok()),
                server_ids: Vec::new(),
                pool_ids: Vec::new(),
                route_map_id: m.route_map_id,
                group_id: m.group_id,
            })
            .collect();

        GetUsersResponse::Ok(Json(PaginatedUsers {
            total: count,
            items: dto_list,
        }))
    }

    /// Получение профиля пользователя по ID
    #[oai(path = "/user/:id", method = "get")]
    async fn get_user(&self, auth: AdminToken, id: Path<Uuid>) -> GetUserApiResult {
        if let Err(err) = validate_admin_session(&self.db, &auth.0.token).await {
            return GetUserApiResult::Unauthorized(Json(err));
        }

        let (u, r) = match users::Entity::find_by_id(id.0)
            .find_also_related(rates::Entity)
            .one(&self.db)
            .await
        {
            Ok(Some(pair)) => pair,
            Ok(None) => return GetUserApiResult::NotFound(Json("VPN клиент не найден".into())),
            Err(e) => return GetUserApiResult::Error(Json(e.to_string())),
        };

        let s_ids = user_servers::Entity::find()
            .filter(user_servers::Column::UserId.eq(u.id))
            .all(&self.db)
            .await
            .unwrap_or_default()
            .into_iter()
            .map(|us| us.server_id)
            .collect();
        let p_ids = user_pool_ids(&self.db, u.id).await;
        let route_map_id = user_route_map_id(&self.db, u.id).await;

        let rate_dto = r.map(|rate_model| RateDto {
            id: rate_model.id,
            sessions: rate_model.sessions,
            date_end: rate_model.date_end.format("%Y-%m-%dT%H:%M").to_string(),
        });

        GetUserApiResult::Ok(Json(VpnUserDto {
            id: u.id,
            fingerprint: u.fingerprint,
            uid: u.uid,
            is_active: u.is_active,
            created_at: u.created_at.format("%Y-%m-%d %H:%M:%S").to_string(),
            rate: rate_dto,
            static_ip: u.static_ip.and_then(|ip| ip.parse().ok()),
            server_ids: s_ids,
            pool_ids: p_ids,
            route_map_id,
            group_id: u.group_id,
        }))
    }

    /// Создание нового VPN-Клиента
    #[oai(path = "/add", method = "post")]
    async fn add_user(&self, auth: AdminToken, req: Json<AddUserRequest>) -> AddUserApiResult {
        if let Err(err) = validate_admin_session(&self.db, &auth.0.token).await {
            return AddUserApiResult::Unauthorized(Json(err));
        }

        let trimmed_uid = req.0.uid.trim().to_string();
        if trimmed_uid.is_empty() || trimmed_uid.chars().count() > 64 {
            return AddUserApiResult::BadRequest(Json(
                "Имя пользователя должно содержать от 1 до 64 символов".into(),
            ));
        }

        match users::Entity::find()
            .filter(users::Column::Uid.eq(&trimmed_uid))
            .one(&self.db)
            .await
        {
            Ok(Some(_)) => {
                return AddUserApiResult::Conflict(Json(
                    "Пользователь с таким именем уже существует".into(),
                ));
            }
            Ok(None) => {}
            Err(e) => return AddUserApiResult::Error(Json(e.to_string())),
        }

        let identity = crate::keygen::generate_identity();
        let user_id = Uuid::new_v4();
        let encryptor = DbEncryptor::new();
        let now = Utc::now().naive_utc();

        let encrypted_private_key = match encryptor.encrypt(&identity.private_key) {
            Ok(k) => k,
            Err(e) => return AddUserApiResult::Error(Json(e.to_string())),
        };
        let encrypted_public_key = match encryptor.encrypt(&identity.public_key) {
            Ok(k) => k,
            Err(e) => return AddUserApiResult::Error(Json(e.to_string())),
        };

        let new_user = users::ActiveModel {
            id: Set(user_id),
            fingerprint: Set(identity.fingerprint.clone()),
            uid: Set(Some(trimmed_uid.clone())),
            is_active: Set(true),
            created_at: Set(now),
            updated_at: Set(now),
            static_ip: Set(None),
            private_key: Set(Some(encrypted_private_key)),
            public_key: Set(Some(encrypted_public_key)),
            route_map_id: Set(req.0.route_map_id),
            group_id: Set(req.0.group_id),
        };

        if let Err(e) = new_user.insert(&self.db).await {
            error!("Failed to create user: {}", e);
            return AddUserApiResult::Error(Json("Ошибка записи пользователя в БД".into()));
        }

        let mut created_rate: Option<RateDto> = None;

        if let Some(group_id) = req.0.group_id {
            if let Ok(Some(group)) = groups::Entity::find_by_id(group_id).one(&self.db).await {
                // duration_days <= 0 означает безлимитный срок действия — не протухаем мгновенно!
                let date_end = if group.duration_days <= 0 {
                    now + chrono::Duration::days(36500)
                } else {
                    now + chrono::Duration::days(group.duration_days as i64)
                };
                let rate_id = Uuid::new_v4();
                let new_rate = rates::ActiveModel {
                    id: Set(rate_id),
                    user_id: Set(user_id),
                    sessions: Set(group.sessions_limit),
                    traffic_limit: Set(group.traffic_limit),
                    speed_limit: Set(group.speed_limit),
                    date_end: Set(date_end),
                    created_at: Set(now),
                    updated_at: Set(now),
                };
                if let Ok(saved_rate) = new_rate.insert(&self.db).await {
                    created_rate = Some(RateDto {
                        id: saved_rate.id,
                        sessions: saved_rate.sessions,
                        date_end: saved_rate.date_end.format("%Y-%m-%dT%H:%M").to_string(),
                    });
                }
            }
        } else if let Some(ref rate_req) = req.0.rate {
            if let Some(date_end) = parse_rate_date(&rate_req.date_end) {
                let new_rate = rates::ActiveModel {
                    id: Set(Uuid::new_v4()),
                    user_id: Set(user_id),
                    sessions: Set(rate_req.sessions),
                    traffic_limit: Set(0),
                    speed_limit: Set(0),
                    date_end: Set(date_end),
                    created_at: Set(now),
                    updated_at: Set(now),
                };
                if let Ok(saved_rate) = new_rate.insert(&self.db).await {
                    created_rate = Some(RateDto {
                        id: saved_rate.id,
                        sessions: saved_rate.sessions,
                        date_end: saved_rate.date_end.format("%Y-%m-%dT%H:%M").to_string(),
                    });
                }
            }
        }

        if let Some(ids) = &req.0.server_ids {
            for sid in ids {
                let _ = (user_servers::ActiveModel {
                    user_id: Set(user_id),
                    server_id: Set(*sid),
                })
                .insert(&self.db)
                .await;
            }
        }
        if let Some(ids) = &req.0.pool_ids {
            for pool_id in ids {
                let _ = (user_node_pools::ActiveModel {
                    user_id: Set(user_id),
                    pool_id: Set(*pool_id),
                })
                .insert(&self.db)
                .await;
            }
        }

        AddUserApiResult::Ok(Json(AddUserResponse {
            id: user_id,
            uid: trimmed_uid,
            fingerprint: identity.fingerprint,
            private_key: identity.private_key,
            public_key: identity.public_key,
            rate: created_rate,
        }))
    }

    /// Редактировать настройки профиля (PATCH)
    #[oai(path = "/user/:id", method = "patch")]
    async fn update_user(
        &self,
        auth: AdminToken,
        id: Path<Uuid>,
        req: Json<UpdateUserRequest>,
    ) -> UpdateUserApiResult {
        if let Err(err) = validate_admin_session(&self.db, &auth.0.token).await {
            return UpdateUserApiResult::Unauthorized(Json(err));
        }

        let (user_model, rate_model) = match users::Entity::find_by_id(id.0)
            .find_also_related(rates::Entity)
            .one(&self.db)
            .await
        {
            Ok(Some(pair)) => pair,
            Ok(None) => return UpdateUserApiResult::NotFound(Json("VPN клиент не найден".into())),
            Err(e) => return UpdateUserApiResult::Error(Json(e.to_string())),
        };

        let was_active = user_model.is_active;
        let user_fingerprint = user_model.fingerprint.clone();
        let current_uid = user_model.uid.clone();

        let rate_dto = rate_model.map(|r_model| RateDto {
            id: r_model.id,
            sessions: r_model.sessions,
            date_end: r_model.date_end.format("%Y-%m-%dT%H:%M").to_string(),
        });

        let mut editable_user = user_model.into_active_model();
        let mut something_changed = false;
        let mut deactivated_now = false;

        if let Some(new_uid) = req.0.uid {
            let trimmed = new_uid.trim().to_string();
            if trimmed.is_empty() || trimmed.chars().count() > 64 {
                return UpdateUserApiResult::BadRequest(Json(
                    "Имя пользователя должно содержать от 1 до 64 символов".into(),
                ));
            }
            if current_uid.as_deref() != Some(trimmed.as_str()) {
                if let Ok(Some(_)) = users::Entity::find()
                    .filter(users::Column::Uid.eq(&trimmed))
                    .filter(users::Column::Id.ne(id.0))
                    .one(&self.db)
                    .await
                {
                    return UpdateUserApiResult::BadRequest(Json(
                        "Пользователь с таким именем уже существует".into(),
                    ));
                }
                editable_user.uid = Set(Some(trimmed));
                something_changed = true;
            }
        }
        if let Some(activation_flag) = req.0.is_active {
            if was_active && !activation_flag {
                deactivated_now = true;
            }
            editable_user.is_active = Set(activation_flag);
            something_changed = true;
        }
        if req.0.clear_static_ip.unwrap_or(false) {
            editable_user.static_ip = Set(None);
            something_changed = true;
        } else if let Some(static_ip) = req.0.static_ip {
            let trimmed = static_ip.trim().to_string();
            editable_user.static_ip = Set(if trimmed.is_empty() { None } else { Some(trimmed) });
            something_changed = true;
        }

        if req.0.clear_group.unwrap_or(false) {
            editable_user.group_id = Set(None);
            something_changed = true;
        } else if let Some(group_id) = req.0.group_id {
            editable_user.group_id = Set(Some(group_id));
            something_changed = true;
        }

        if req.0.clear_route_map.unwrap_or(false) {
            editable_user.route_map_id = Set(None);
            something_changed = true;
        } else if let Some(route_map_id) = req.0.route_map_id {
            editable_user.route_map_id = Set(Some(route_map_id));
            something_changed = true;
        }

        if let Some(ref ids) = req.0.server_ids {
            let _ = user_servers::Entity::delete_many()
                .filter(user_servers::Column::UserId.eq(id.0))
                .exec(&self.db)
                .await;
            for sid in ids {
                let _ = (user_servers::ActiveModel {
                    user_id: Set(id.0),
                    server_id: Set(*sid),
                })
                .insert(&self.db)
                .await;
            }
            something_changed = true;
        }
        if let Some(ref ids) = req.0.pool_ids {
            let _ = user_node_pools::Entity::delete_many()
                .filter(user_node_pools::Column::UserId.eq(id.0))
                .exec(&self.db)
                .await;
            for pool_id in ids {
                let _ = (user_node_pools::ActiveModel {
                    user_id: Set(id.0),
                    pool_id: Set(*pool_id),
                })
                .insert(&self.db)
                .await;
            }
            something_changed = true;
        }

        let saved_user = if something_changed {
            editable_user.updated_at = Set(Utc::now().naive_utc());
            match editable_user.update(&self.db).await {
                Ok(updated) => updated,
                Err(e) => {
                    error!("[DB CRASH] Editing user payload: {}", e);
                    return UpdateUserApiResult::Error(Json("DB Update failed".into()));
                }
            }
        } else {
            match users::Entity::find_by_id(id.0).one(&self.db).await {
                Ok(Some(u)) => u,
                Ok(None) => return UpdateUserApiResult::NotFound(Json("VPN клиент не найден".into())),
                Err(e) => return UpdateUserApiResult::Error(Json(e.to_string())),
            }
        };

        if deactivated_now {
            enqueue_disconnect_on_active_servers(&self.db, &user_fingerprint).await;
        }

        let s_ids = user_servers::Entity::find()
            .filter(user_servers::Column::UserId.eq(saved_user.id))
            .all(&self.db)
            .await
            .unwrap_or_default()
            .into_iter()
            .map(|us| us.server_id)
            .collect();
        let p_ids = user_pool_ids(&self.db, saved_user.id).await;
        let route_map_id = user_route_map_id(&self.db, saved_user.id).await;

        UpdateUserApiResult::Ok(Json(VpnUserDto {
            id: saved_user.id,
            fingerprint: saved_user.fingerprint,
            uid: saved_user.uid,
            is_active: saved_user.is_active,
            created_at: saved_user.created_at.format("%Y-%m-%d %H:%M:%S").to_string(),
            rate: rate_dto,
            static_ip: saved_user.static_ip.and_then(|ip| ip.parse().ok()),
            server_ids: s_ids,
            pool_ids: p_ids,
            route_map_id,
            group_id: saved_user.group_id,
        }))
    }

    /// Удалить пользователя, каскадно очистить устройства/подписки/тарифы и отключить активные сессии.
    #[oai(path = "/user/:id", method = "delete")]
    async fn delete_user(&self, auth: AdminToken, id: Path<Uuid>) -> DeleteUserApiResult {
        if let Err(err) = validate_admin_session(&self.db, &auth.0.token).await {
            return DeleteUserApiResult::Unauthorized(Json(err));
        }

        let user = match users::Entity::find_by_id(id.0).one(&self.db).await {
            Ok(Some(u)) => u,
            Ok(None) => return DeleteUserApiResult::NotFound(Json("VPN клиент не найден".into())),
            Err(e) => return DeleteUserApiResult::Error(Json(e.to_string())),
        };

        enqueue_disconnect_on_active_servers(&self.db, &user.fingerprint).await;

        let txn = match self.db.begin().await {
            Ok(t) => t,
            Err(e) => return DeleteUserApiResult::Error(Json(e.to_string())),
        };

        let res: Result<(), sea_orm::DbErr> = async {
            let device_ids: Vec<Uuid> = user_devices::Entity::find()
                .filter(user_devices::Column::UserId.eq(id.0))
                .all(&txn)
                .await?
                .into_iter()
                .map(|d| d.id)
                .collect();
            if !device_ids.is_empty() {
                user_device_traffic_sources::Entity::delete_many()
                    .filter(user_device_traffic_sources::Column::DeviceId.is_in(device_ids))
                    .exec(&txn)
                    .await?;
            }
            user_devices::Entity::delete_many()
                .filter(user_devices::Column::UserId.eq(id.0))
                .exec(&txn)
                .await?;
            user_subscriptions::Entity::delete_many()
                .filter(user_subscriptions::Column::UserId.eq(id.0))
                .exec(&txn)
                .await?;
            rates::Entity::delete_many()
                .filter(rates::Column::UserId.eq(id.0))
                .exec(&txn)
                .await?;
            active_sessions::Entity::delete_many()
                .filter(active_sessions::Column::UserId.eq(id.0))
                .exec(&txn)
                .await?;
            user_servers::Entity::delete_many()
                .filter(user_servers::Column::UserId.eq(id.0))
                .exec(&txn)
                .await?;
            user_node_pools::Entity::delete_many()
                .filter(user_node_pools::Column::UserId.eq(id.0))
                .exec(&txn)
                .await?;
            traffic_totals::Entity::delete_many()
                .filter(
                    sea_orm::Condition::any()
                        .add(traffic_totals::Column::UserId.eq(id.0))
                        .add(traffic_totals::Column::Fingerprint.eq(&user.fingerprint)),
                )
                .exec(&txn)
                .await?;
            traffic_hourly::Entity::delete_many()
                .filter(
                    sea_orm::Condition::any()
                        .add(traffic_hourly::Column::UserId.eq(id.0))
                        .add(traffic_hourly::Column::Fingerprint.eq(&user.fingerprint)),
                )
                .exec(&txn)
                .await?;
            users::Entity::delete_by_id(id.0).exec(&txn).await?;
            Ok(())
        }
        .await;

        if let Err(e) = res {
            return DeleteUserApiResult::Error(Json(e.to_string()));
        }
        if let Err(e) = txn.commit().await {
            return DeleteUserApiResult::Error(Json(e.to_string()));
        }

        DeleteUserApiResult::Ok(Json(DeleteUserDto {
            deleted: true,
            id: id.0,
        }))
    }

    /// Сбросить счётчики трафика пользователя (ANet + VLESS + WireGuard).
    #[oai(path = "/users/:id/reset-traffic", method = "post")]
    async fn reset_user_traffic(
        &self,
        auth: AdminToken,
        id: Path<Uuid>,
    ) -> ResetTrafficApiResult {
        if let Err(err) = validate_admin_session(&self.db, &auth.0.token).await {
            return ResetTrafficApiResult::Unauthorized(Json(err));
        }

        let user = match users::Entity::find_by_id(id.0).one(&self.db).await {
            Ok(Some(u)) => u,
            Ok(None) => return ResetTrafficApiResult::NotFound(Json("VPN клиент не найден".into())),
            Err(e) => return ResetTrafficApiResult::Error(Json(e.to_string())),
        };

        let txn = match self.db.begin().await {
            Ok(t) => t,
            Err(e) => return ResetTrafficApiResult::Error(Json(e.to_string())),
        };

        let res: Result<(), sea_orm::DbErr> = async {
            traffic_totals::Entity::delete_many()
                .filter(
                    sea_orm::Condition::any()
                        .add(traffic_totals::Column::UserId.eq(id.0))
                        .add(traffic_totals::Column::Fingerprint.eq(&user.fingerprint)),
                )
                .exec(&txn)
                .await?;
            traffic_hourly::Entity::delete_many()
                .filter(
                    sea_orm::Condition::any()
                        .add(traffic_hourly::Column::UserId.eq(id.0))
                        .add(traffic_hourly::Column::Fingerprint.eq(&user.fingerprint)),
                )
                .exec(&txn)
                .await?;
            let device_ids: Vec<Uuid> = user_devices::Entity::find()
                .filter(user_devices::Column::UserId.eq(id.0))
                .all(&txn)
                .await?
                .into_iter()
                .map(|d| d.id)
                .collect();
            if !device_ids.is_empty() {
                user_device_traffic_sources::Entity::delete_many()
                    .filter(user_device_traffic_sources::Column::DeviceId.is_in(device_ids))
                    .exec(&txn)
                    .await?;
                user_devices::Entity::update_many()
                    .col_expr(
                        user_devices::Column::UploadBytes,
                        sea_orm::sea_query::Expr::value(0_i64),
                    )
                    .col_expr(
                        user_devices::Column::DownloadBytes,
                        sea_orm::sea_query::Expr::value(0_i64),
                    )
                    .filter(user_devices::Column::UserId.eq(id.0))
                    .exec(&txn)
                    .await?;
            }
            Ok(())
        }
        .await;

        if let Err(e) = res {
            return ResetTrafficApiResult::Error(Json(e.to_string()));
        }
        if let Err(e) = txn.commit().await {
            return ResetTrafficApiResult::Error(Json(e.to_string()));
        }

        ResetTrafficApiResult::Ok(Json(ResetTrafficDto { reset: true }))
    }

    /// Обнулить конфиг: перегенерация ключей ANet с авто-киком старого fingerprint на всех нодах.
    #[oai(path = "/regenerate/:id", method = "post")]
    async fn regenerate_keys(&self, auth: AdminToken, id: Path<Uuid>) -> RegenerateUserApiResult {
        if let Err(err) = validate_admin_session(&self.db, &auth.0.token).await {
            return RegenerateUserApiResult::Unauthorized(Json(err));
        }

        let user_model = match users::Entity::find_by_id(id.0).one(&self.db).await {
            Ok(Some(u)) => u,
            Ok(None) => {
                return RegenerateUserApiResult::NotFound(Json(
                    "Клиент с таким ID не обнаружен.".into(),
                ));
            }
            Err(e) => {
                error!("[REGEN FAIL] {}", e);
                return RegenerateUserApiResult::Error(Json("Ошибка поиска".into()));
            }
        };

        let old_fingerprint = user_model.fingerprint.clone();
        let new_crypto_core = crate::keygen::generate_identity();
        let encryptor = DbEncryptor::new();

        let encrypted_private_key = match encryptor.encrypt(&new_crypto_core.private_key) {
            Ok(k) => k,
            Err(_) => return RegenerateUserApiResult::Error(Json("Encryption error".into())),
        };
        let encrypted_public_key = match encryptor.encrypt(&new_crypto_core.public_key) {
            Ok(k) => k,
            Err(_) => return RegenerateUserApiResult::Error(Json("Encryption error".into())),
        };

        let mut updated_usr = user_model.into_active_model();
        updated_usr.fingerprint = Set(new_crypto_core.fingerprint.clone());
        updated_usr.private_key = Set(Some(encrypted_private_key));
        updated_usr.public_key = Set(Some(encrypted_public_key));
        updated_usr.updated_at = Set(Utc::now().naive_utc());

        let final_model = match updated_usr.update(&self.db).await {
            Ok(saved) => saved,
            Err(e) => {
                error!("[REGEN DB ERROR]: {}", e);
                return RegenerateUserApiResult::Error(Json(
                    "Ошибка базы данных, operation aborted.".into(),
                ));
            }
        };

        enqueue_disconnect_on_active_servers(&self.db, &old_fingerprint).await;

        RegenerateUserApiResult::Ok(Json(RegenerateUserResponse {
            id: final_model.id,
            uid: final_model.uid,
            fingerprint: new_crypto_core.fingerprint,
            private_key: new_crypto_core.private_key,
            public_key: new_crypto_core.public_key,
        }))
    }

    /// Настройки тарифа: Обновление количества сессий и даты окончания.
    #[oai(path = "/rate/:id", method = "patch")]
    async fn update_rate(
        &self,
        auth: AdminToken,
        id: Path<Uuid>,
        req: Json<UpdateRateRequest>,
    ) -> UpdateRateApiResult {
        if let Err(err) = validate_admin_session(&self.db, &auth.0.token).await {
            return UpdateRateApiResult::Unauthorized(Json(err));
        }

        let rate_model = match rates::Entity::find_by_id(id.0).one(&self.db).await {
            Ok(Some(r)) => r,
            Ok(None) => return UpdateRateApiResult::NotFound(Json("Тариф не найден в базе!".into())),
            Err(e) => return UpdateRateApiResult::Error(Json(e.to_string())),
        };

        let orig_id = rate_model.id;
        let orig_sessions = rate_model.sessions;
        let orig_date_end = rate_model.date_end;
        let mut editable_rate = rate_model.into_active_model();
        let mut something_changed = false;

        if let Some(new_sessions) = req.0.sessions {
            editable_rate.sessions = Set(new_sessions);
            something_changed = true;
        }
        if let Some(new_date_str) = &req.0.date_end {
            let Some(date_parsed) = parse_rate_date(new_date_str) else {
                return UpdateRateApiResult::BadRequest(Json(
                    "Неверный формат даты. Ожидается YYYY-MM-DDTHH:MM или YYYY-MM-DD-HH:MM".into(),
                ));
            };
            editable_rate.date_end = Set(date_parsed);
            something_changed = true;
        }

        if something_changed {
            editable_rate.updated_at = Set(Utc::now().naive_utc());
            match editable_rate.update(&self.db).await {
                Ok(updated_data) => UpdateRateApiResult::Ok(Json(RateDto {
                    id: updated_data.id,
                    sessions: updated_data.sessions,
                    date_end: updated_data.date_end.format("%Y-%m-%dT%H:%M").to_string(),
                })),
                Err(e) => UpdateRateApiResult::Error(Json(e.to_string())),
            }
        } else {
            UpdateRateApiResult::Ok(Json(RateDto {
                id: orig_id,
                sessions: orig_sessions,
                date_end: orig_date_end.format("%Y-%m-%dT%H:%M").to_string(),
            }))
        }
    }

    /// Удаление тарифа по ID
    #[oai(path = "/rate/:id", method = "delete")]
    async fn delete_rate(&self, auth: AdminToken, id: Path<Uuid>) -> DeleteRateApiResult {
        if let Err(err) = validate_admin_session(&self.db, &auth.0.token).await {
            return DeleteRateApiResult::Unauthorized(Json(err));
        }

        match rates::Entity::delete_by_id(id.0).exec(&self.db).await {
            Ok(res) if res.rows_affected > 0 => DeleteRateApiResult::Deleted,
            Ok(_) => DeleteRateApiResult::NotFound(Json("Тариф не найден".into())),
            Err(e) => DeleteRateApiResult::Error(Json(e.to_string())),
        }
    }

    /// Добавление (или замена существующего) тарифа пользователя
    #[oai(path = "/addrate", method = "post")]
    async fn add_rate(
        &self,
        auth: AdminToken,
        user_id: Query<Uuid>,
        req: Json<AddRateRequest>,
    ) -> AddRateApiResult {
        if let Err(err) = validate_admin_session(&self.db, &auth.0.token).await {
            return AddRateApiResult::Unauthorized(Json(err));
        }

        match users::Entity::find_by_id(user_id.0).one(&self.db).await {
            Ok(Some(_)) => {}
            Ok(None) => return AddRateApiResult::BadRequest(Json("Пользователь не найден".into())),
            Err(e) => return AddRateApiResult::Error(Json(e.to_string())),
        }

        let Some(new_date_str) = &req.0.date_end else {
            return AddRateApiResult::BadRequest(Json("Нет даты".into()));
        };
        let Some(date_parsed) = parse_rate_date(new_date_str) else {
            return AddRateApiResult::BadRequest(Json(
                "Неверный формат даты. Ожидается YYYY-MM-DDTHH:MM или YYYY-MM-DD-HH:MM".into(),
            ));
        };

        let now = Utc::now().naive_utc();
        let new_sessions = req.0.sessions.unwrap_or(0);

        // Не плодим дубликаты: если у пользователя уже есть тариф, обновляем его
        if let Ok(Some(existing)) = rates::Entity::find()
            .filter(rates::Column::UserId.eq(user_id.0))
            .one(&self.db)
            .await
        {
            let mut active = existing.into_active_model();
            active.sessions = Set(new_sessions);
            active.date_end = Set(date_parsed);
            if let Some(tl) = req.0.traffic_limit {
                active.traffic_limit = Set(tl);
            }
            if let Some(sl) = req.0.speed_limit {
                active.speed_limit = Set(sl);
            }
            active.updated_at = Set(now);
            return match active.update(&self.db).await {
                Ok(saved) => AddRateApiResult::Ok(Json(RateDto {
                    id: saved.id,
                    sessions: saved.sessions,
                    date_end: saved.date_end.format("%Y-%m-%dT%H:%M").to_string(),
                })),
                Err(e) => AddRateApiResult::Error(Json(e.to_string())),
            };
        }

        let new_rate = rates::ActiveModel {
            id: Set(Uuid::new_v4()),
            user_id: Set(user_id.0),
            sessions: Set(new_sessions),
            date_end: Set(date_parsed),
            traffic_limit: Set(req.0.traffic_limit.unwrap_or(0)),
            speed_limit: Set(req.0.speed_limit.unwrap_or(0)),
            created_at: Set(now),
            updated_at: Set(now),
        };

        match new_rate.insert(&self.db).await {
            Ok(added_data) => AddRateApiResult::Ok(Json(RateDto {
                id: added_data.id,
                sessions: added_data.sessions,
                date_end: added_data.date_end.format("%Y-%m-%dT%H:%M").to_string(),
            })),
            Err(e) => AddRateApiResult::Error(Json(e.to_string())),
        }
    }

    /// Внутренняя проверка активности пользователя (для profile-service без QR-хака).
    #[oai(path = "/internal/users/:id/status", method = "get")]
    async fn internal_user_status(
        &self,
        id: Path<Uuid>,
        #[oai(name = "Authorization")] auth_hdr: Header<Option<String>>,
        #[oai(name = "X-Internal-Service")] internal_svc: Header<Option<String>>,
        #[oai(name = "X-Auth-Key")] auth_key: Header<Option<String>>,
    ) -> InternalUserStatusResponse {
        let mut authorized = internal_svc
            .0
            .as_deref()
            .is_some_and(|v| !v.trim().is_empty());
        if !authorized {
            if let (Ok(expected), Some(actual)) =
                (std::env::var("AUTH_BACKEND_KEY"), auth_key.0.as_deref())
            {
                if actual == expected {
                    authorized = true;
                }
            }
        }
        if !authorized {
            if let Some(jwt) = auth_hdr
                .0
                .as_deref()
                .and_then(|h| h.strip_prefix("Bearer "))
            {
                if validate_admin_session(&self.db, jwt.trim()).await.is_ok() {
                    authorized = true;
                }
            }
        }
        if !authorized {
            return InternalUserStatusResponse::Unauthorized(Json("Unauthorized".into()));
        }

        let (user, rate_opt) = match users::Entity::find_by_id(id.0)
            .find_also_related(rates::Entity)
            .one(&self.db)
            .await
        {
            Ok(Some(pair)) => pair,
            Ok(None) => return InternalUserStatusResponse::NotFound(Json("User not found".into())),
            Err(e) => return InternalUserStatusResponse::Error(Json(e.to_string())),
        };

        let now = Utc::now().naive_utc();
        let expired = rate_opt.as_ref().is_some_and(|r| r.date_end < now);
        let active = user.is_active && !expired;

        InternalUserStatusResponse::Ok(Json(InternalUserStatusDto {
            active,
            id: user.id,
            uid: user.uid,
            is_active: user.is_active,
            expired,
            expires_at: rate_opt.map(|r| r.date_end.and_utc().to_rfc3339()),
        }))
    }

    /// ЗАЩИЩЁННЫЙ ЭНДПОИНТ: Скачать готовый client.toml (требует Admin JWT или ?token= подписки)
    #[oai(path = "/config/:id", method = "get")]
    async fn download_config(
        &self,
        id: Path<Uuid>,
        #[oai(name = "Authorization")] auth_hdr: Header<Option<String>>,
        token: Query<Option<String>>,
        #[oai(name = "Host")] host: Header<Option<String>>,
        #[oai(name = "X-Forwarded-Proto")] forwarded_proto: Header<Option<String>>,
        #[oai(name = "X-Forwarded-Host")] forwarded_host: Header<Option<String>>,
    ) -> DownloadConfigResponse {
        if !validate_config_access(
            &self.db,
            id.0,
            auth_hdr.0.as_deref(),
            token.0.as_deref(),
        )
        .await
        {
            warn!("[CONFIG] Unauthorized config download attempt for {}", id.0);
            return DownloadConfigResponse::Unauthorized(Json(
                "Требуется авторизация администратора или персональный токен подписки".into(),
            ));
        }

        let effective_host = forwarded_host
            .0
            .filter(|h| !h.trim().is_empty())
            .or_else(|| host.0.filter(|h| !h.trim().is_empty()));
        let scheme = forwarded_proto
            .0
            .filter(|p| !p.trim().is_empty())
            .unwrap_or_else(|| "https".to_string());
        let base_auth_url = std::env::var("ANET_PUBLIC_URL")
            .ok()
            .filter(|u| !u.trim().is_empty())
            .or_else(|| effective_host.map(|h| format!("{}://{}", scheme, h.trim())));

        match build_user_client_toml(&self.db, &self.client_template_path, id.0, base_auth_url)
            .await
        {
            Ok((client_name, final_output)) => {
                let filename_header = format!("attachment; filename=\"{}.toml\"", client_name);
                info!("[CONFIG] Served client.toml for user: {}", client_name);
                DownloadConfigResponse::Ok(PlainText(final_output), filename_header)
            }
            Err((poem::http::StatusCode::NOT_FOUND, msg)) => {
                DownloadConfigResponse::NotFound(Json(msg))
            }
            Err((_, msg)) => DownloadConfigResponse::Error(Json(msg)),
        }
    }

    /// Защищённая страница конфигурации (без утечки секретов на внешний api.qrserver.com)
    #[oai(path = "/config/qr/:id", method = "get")]
    async fn download_config_qr(
        &self,
        id: Path<Uuid>,
        #[oai(name = "Authorization")] auth_hdr: Header<Option<String>>,
        #[oai(name = "X-Internal-Service")] internal_svc: Header<Option<String>>,
        token: Query<Option<String>>,
        #[oai(name = "Host")] host: Header<Option<String>>,
        #[oai(name = "X-Forwarded-Proto")] forwarded_proto: Header<Option<String>>,
    ) -> QrPageResponse {
        let is_internal = internal_svc
            .0
            .as_deref()
            .is_some_and(|v| !v.trim().is_empty());
        if !is_internal
            && !validate_config_access(
                &self.db,
                id.0,
                auth_hdr.0.as_deref(),
                token.0.as_deref(),
            )
            .await
        {
            return QrPageResponse::Unauthorized(Json(
                "Требуется авторизация администратора или токен подписки".into(),
            ));
        }

        let user_opt = match users::Entity::find_by_id(id.0).one(&self.db).await {
            Ok(Some(u)) => u,
            Ok(None) => return QrPageResponse::NotFound(Json("Client not found".into())),
            Err(e) => {
                error!("[QR] DB error: {}", e);
                return QrPageResponse::Error(Json("Database error".into()));
            }
        };

        if !user_opt.is_active {
            return QrPageResponse::NotFound(Json("Client is inactive or banned".into()));
        }

        let scheme = forwarded_proto
            .0
            .filter(|p| !p.trim().is_empty())
            .unwrap_or_else(|| "https".to_string());
        let host_str = host.0.unwrap_or_else(|| "127.0.0.1:8088".to_string());
        let suffix = token
            .0
            .as_deref()
            .map(str::trim)
            .filter(|t| !t.is_empty())
            .map(|t| format!("?token={}", url_encode(t)))
            .unwrap_or_default();
        let config_url = format!("{}://{}/api/v1/config/{}{}", scheme, host_str, id.0, suffix);

        let html_page = crate::api::api::get_qr_html_page(
            &config_url,
            user_opt.uid.as_deref().unwrap_or("client"),
        );

        QrPageResponse::Ok(PlainText(html_page))
    }
}
