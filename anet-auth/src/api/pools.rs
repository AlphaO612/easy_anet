use crate::api::api::{load_pool_dto, validate_admin_session};
use crate::api::dto::{
    AdminToken, DeleteNodePoolResponse, GetNodePoolsResponse, GetPoolPreviewResponse,
    PoolPreviewCandidateDto, PoolPreviewDto, SaveNodePoolRequest, SaveNodePoolResponse,
};
use crate::entities::{node_pool_members, node_pools, node_runtime_states, servers};
use chrono::Utc;
use poem_openapi::{param::Query, payload::Json, OpenApi};
use sea_orm::{
    ActiveModelTrait, ColumnTrait, DatabaseConnection, EntityTrait, IntoActiveModel, Set,
    QueryFilter, QueryOrder, TransactionTrait,
};
use std::collections::{HashMap, HashSet};
use uuid::Uuid;

pub struct PoolsApi {
    pub db: DatabaseConnection,
}

fn validate_pool_request(req: &SaveNodePoolRequest) -> std::result::Result<(), String> {
    if req.name.trim().is_empty() {
        return Err("Pool name cannot be empty".to_string());
    }

    if !matches!(
        req.strategy.as_str(),
        "weighted" | "least_connections" | "latency" | "speed"
    ) {
        return Err(
            "Strategy must be weighted, least_connections, latency, or speed".to_string(),
        );
    }

    if req
        .members
        .iter()
        .any(|member| member.weight == 0 || member.weight > 10_000)
    {
        return Err("Member weight must be between 1 and 10000".to_string());
    }
    
    for member in &req.members {
        if let Some(url_str) = &member.port_or_url {
            if member.protocol == crate::entities::ProtocolType::Ws
                || member.protocol == crate::entities::ProtocolType::Ahttp
                || member.protocol == crate::entities::ProtocolType::Wrtc
            {
                if !url_str.trim().is_empty() {
                    if !url_str.is_ascii() {
                        return Err(format!("URL '{}' содержит недопустимые (не латинские) символы", url_str));
                    }
                    let parsed = url_str.parse::<poem::http::Uri>().map_err(|e| format!("Некорректный URL '{}': {}", url_str, e))?;
                    let scheme = parsed.scheme_str().unwrap_or("");
                    if member.protocol == crate::entities::ProtocolType::Ws {
                        if scheme != "ws" && scheme != "wss" {
                            return Err(format!("Неподдерживаемый протокол '{}' для Websocket URL. Ожидается ws:// или wss://", scheme));
                        }
                    } else if member.protocol == crate::entities::ProtocolType::Ahttp {
                        if scheme != "http" && scheme != "https" {
                            return Err(format!("Неподдерживаемый протокол '{}' для AHTTP URL. Ожидается http:// или https://", scheme));
                        }
                    } else if member.protocol == crate::entities::ProtocolType::Wrtc {
                        if scheme != "wrtc" && scheme != "http" && scheme != "https" {
                            return Err(format!("Неподдерживаемый протокол '{}' для WRTC URL. Ожидается wrtc://, https:// или http://", scheme));
                        }
                        if parsed.host().is_none() {
                            return Err(format!("WRTC URL '{}' не содержит хост", url_str));
                        }
                        let path = parsed.path().trim_start_matches('/');
                        if path.is_empty() {
                            return Err(format!("WRTC URL '{}' не содержит имя комнаты (room)", url_str));
                        }
                    }
                }
            }
        }
    }

    let unique: std::collections::HashSet<_> = req
        .members
        .iter()
        .map(|member| (member.server_id, member.protocol))
        .collect();

    if unique.len() != req.members.len() {
        return Err("Pool contains duplicate node and protocol combinations".to_string());
    }

    Ok(())
}

#[OpenApi]
impl PoolsApi {
    #[oai(path = "/pools", method = "get")]
    async fn get_node_pools(&self, auth: AdminToken) -> GetNodePoolsResponse {
        if let Err(err) = validate_admin_session(&self.db, &auth.0.token).await {
            return GetNodePoolsResponse::Unauthorized(Json(err));
        }
        let pools = match node_pools::Entity::find()
            .order_by_asc(node_pools::Column::Name)
            .all(&self.db)
            .await
        {
            Ok(pools) => pools,
            Err(e) => return GetNodePoolsResponse::Error(Json(e.to_string())),
        };
        let mut result = Vec::with_capacity(pools.len());
        for pool in pools {
            match load_pool_dto(&self.db, pool).await {
                Ok(pool) => result.push(pool),
                Err(e) => return GetNodePoolsResponse::Error(Json(e.to_string())),
            }
        }
        GetNodePoolsResponse::Ok(Json(result))
    }

    #[oai(path = "/pools", method = "post")]
    async fn create_node_pool(
        &self,
        auth: AdminToken,
        req: Json<SaveNodePoolRequest>,
    ) -> SaveNodePoolResponse {
        if let Err(err) = validate_admin_session(&self.db, &auth.0.token).await {
            return SaveNodePoolResponse::Unauthorized(Json(err));
        }
        if let Err(error) = validate_pool_request(&req.0) {
            return SaveNodePoolResponse::BadRequest(Json(error));
        }
        let txn = match self.db.begin().await {
            Ok(txn) => txn,
            Err(e) => return SaveNodePoolResponse::Error(Json(e.to_string())),
        };
        let now = Utc::now().naive_utc();
        let pool = match (node_pools::ActiveModel {
            id: Set(Uuid::new_v4()),
            name: Set(req.0.name.trim().to_string()),
            strategy: Set(req.0.strategy.clone()),
            is_active: Set(req.0.is_active.unwrap_or(true)),
            created_at: Set(now),
            updated_at: Set(now),
        })
            .insert(&txn)
            .await
        {
            Ok(pool) => pool,
            Err(e) => return SaveNodePoolResponse::Error(Json(e.to_string())),
        };
        for member in &req.0.members {
            if let Err(e) = (node_pool_members::ActiveModel {
                pool_id: Set(pool.id),
                server_id: Set(member.server_id),
                protocol: Set(member.protocol),
                port_or_url: Set(member.port_or_url.clone()),
                wrtc_mode: Set(member.wrtc_mode.clone()),
                weight: Set(member.weight),
            })
                .insert(&txn)
                .await
            {
                return SaveNodePoolResponse::Error(Json(e.to_string()));
            }
        }
        if let Err(e) = txn.commit().await {
            return SaveNodePoolResponse::Error(Json(e.to_string()));
        }
        match load_pool_dto(&self.db, pool).await {
            Ok(pool) => SaveNodePoolResponse::Ok(Json(pool)),
            Err(e) => SaveNodePoolResponse::Error(Json(e.to_string())),
        }
    }

    #[oai(path = "/pools/:id", method = "patch")]
    async fn update_node_pool(
        &self,
        auth: AdminToken,
        id: poem_openapi::param::Path<Uuid>,
        req: Json<SaveNodePoolRequest>,
    ) -> SaveNodePoolResponse {
        if let Err(err) = validate_admin_session(&self.db, &auth.0.token).await {
            return SaveNodePoolResponse::Unauthorized(Json(err));
        }
        if let Err(error) = validate_pool_request(&req.0) {
            return SaveNodePoolResponse::BadRequest(Json(error));
        }
        let existing = match node_pools::Entity::find_by_id(id.0).one(&self.db).await {
            Ok(Some(pool)) => pool,
            Ok(None) => return SaveNodePoolResponse::NotFound(Json("Pool not found".to_string())),
            Err(e) => return SaveNodePoolResponse::Error(Json(e.to_string())),
        };
        let txn = match self.db.begin().await {
            Ok(txn) => txn,
            Err(e) => return SaveNodePoolResponse::Error(Json(e.to_string())),
        };
        let mut pool = existing.into_active_model();
        pool.name = Set(req.0.name.trim().to_string());
        pool.strategy = Set(req.0.strategy.clone());
        pool.is_active = Set(req.0.is_active.unwrap_or(true));
        pool.updated_at = Set(Utc::now().naive_utc());
        let saved = match pool.update(&txn).await {
            Ok(pool) => pool,
            Err(e) => return SaveNodePoolResponse::Error(Json(e.to_string())),
        };
        if let Err(e) = node_pool_members::Entity::delete_many()
            .filter(node_pool_members::Column::PoolId.eq(id.0))
            .exec(&txn)
            .await
        {
            return SaveNodePoolResponse::Error(Json(e.to_string()));
        }
        for member in &req.0.members {
            if let Err(e) = (node_pool_members::ActiveModel {
                pool_id: Set(id.0),
                server_id: Set(member.server_id),
                protocol: Set(member.protocol),
                port_or_url: Set(member.port_or_url.clone()),
                wrtc_mode: Set(member.wrtc_mode.clone()),
                weight: Set(member.weight),
            })
                .insert(&txn)
                .await
            {
                return SaveNodePoolResponse::Error(Json(e.to_string()));
            }
        }
        if let Err(e) = txn.commit().await {
            return SaveNodePoolResponse::Error(Json(e.to_string()));
        }
        match load_pool_dto(&self.db, saved).await {
            Ok(pool) => SaveNodePoolResponse::Ok(Json(pool)),
            Err(e) => SaveNodePoolResponse::Error(Json(e.to_string())),
        }
    }

    #[oai(path = "/pools/:id", method = "delete")]
    async fn delete_node_pool(
        &self,
        auth: AdminToken,
        id: poem_openapi::param::Path<Uuid>,
    ) -> DeleteNodePoolResponse {
        if let Err(err) = validate_admin_session(&self.db, &auth.0.token).await {
            return DeleteNodePoolResponse::Unauthorized(Json(err));
        }
        match node_pools::Entity::delete_by_id(id.0).exec(&self.db).await {
            Ok(result) if result.rows_affected > 0 => DeleteNodePoolResponse::Deleted,
            Ok(_) => DeleteNodePoolResponse::NotFound(Json("Pool not found".to_string())),
            Err(e) => DeleteNodePoolResponse::Error(Json(e.to_string())),
        }
    }

    /// Предпросмотр ранжирования узлов пула реальным балансировщиком.
    #[oai(path = "/pools/:id/preview", method = "get")]
    async fn preview_node_pool(
        &self,
        auth: AdminToken,
        id: poem_openapi::param::Path<Uuid>,
        user_id: Query<Option<Uuid>>,
    ) -> GetPoolPreviewResponse {
        if let Err(err) = validate_admin_session(&self.db, &auth.0.token).await {
            return GetPoolPreviewResponse::Unauthorized(Json(err));
        }

        let pool = match node_pools::Entity::find_by_id(id.0).one(&self.db).await {
            Ok(Some(p)) => p,
            Ok(None) => return GetPoolPreviewResponse::NotFound(Json("Pool not found".to_string())),
            Err(e) => return GetPoolPreviewResponse::Error(Json(e.to_string())),
        };

        let members = match node_pool_members::Entity::find()
            .filter(node_pool_members::Column::PoolId.eq(pool.id))
            .all(&self.db)
            .await
        {
            Ok(m) => m,
            Err(e) => return GetPoolPreviewResponse::Error(Json(e.to_string())),
        };

        if members.is_empty() {
            return GetPoolPreviewResponse::Ok(Json(PoolPreviewDto {
                pool_id: pool.id,
                pool_name: pool.name,
                strategy: pool.strategy,
                candidates: Vec::new(),
            }));
        }

        let server_ids: Vec<Uuid> = members
            .iter()
            .map(|m| m.server_id)
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();

        let server_list = match servers::Entity::find()
            .filter(servers::Column::Id.is_in(server_ids.clone()))
            .all(&self.db)
            .await
        {
            Ok(s) => s,
            Err(e) => return GetPoolPreviewResponse::Error(Json(e.to_string())),
        };
        let server_by_id: HashMap<Uuid, servers::Model> =
            server_list.into_iter().map(|s| (s.id, s)).collect();

        let runtime_by_id: HashMap<Uuid, node_runtime_states::Model> =
            match node_runtime_states::Entity::find()
                .filter(node_runtime_states::Column::ServerId.is_in(server_ids))
                .all(&self.db)
                .await
            {
                Ok(list) => list.into_iter().map(|rt| (rt.server_id, rt)).collect(),
                Err(e) => return GetPoolPreviewResponse::Error(Json(e.to_string())),
            };

        let (probe_metrics, _) = crate::api::probes::collect_probe_metrics(&self.db).await;
        let now = Utc::now().naive_utc();
        let effective_user = user_id.0.unwrap_or_else(Uuid::nil);

        let mut active_members_by_server: HashMap<Uuid, Vec<node_pool_members::Model>> =
            HashMap::new();
        let mut inactive_members: Vec<node_pool_members::Model> = Vec::new();

        for member in members {
            if let Some(srv) = server_by_id.get(&member.server_id) {
                if srv.is_active {
                    active_members_by_server
                        .entry(member.server_id)
                        .or_default()
                        .push(member);
                } else {
                    inactive_members.push(member);
                }
            }
        }

        let mut balance_candidates: Vec<crate::balancer::BalanceCandidate> =
            active_members_by_server
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
                    let accepting_connections = rt.map_or(true, |r| r.accepting_connections);
                    let active_connections =
                        rt.map(|r| r.active_connections.max(0) as u64).unwrap_or(0);
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
            effective_user,
            pool.id,
            &mut balance_candidates,
        );

        let mut out = Vec::new();
        let mut rank: i32 = 1;

        for cand in balance_candidates {
            let Some(srv) = server_by_id.get(&cand.server_id) else {
                continue;
            };
            let tier = match crate::balancer::health_tier(&cand) {
                0 => "healthy".to_string(),
                1 => "degraded".to_string(),
                2 => "draining".to_string(),
                _ => "offline".to_string(),
            };
            let reason = crate::balancer::health_reason(&cand).to_string();
            let status = if !cand.has_runtime {
                "unknown".to_string()
            } else if cand.is_online {
                "online".to_string()
            } else {
                "offline".to_string()
            };

            if let Some(mut srv_members) = active_members_by_server.remove(&cand.server_id) {
                srv_members.sort_by(|a, b| {
                    b.weight
                        .cmp(&a.weight)
                        .then_with(|| a.protocol.as_str().cmp(b.protocol.as_str()))
                });
                for member in srv_members {
                    out.push(PoolPreviewCandidateDto {
                        rank,
                        server_id: srv.id,
                        server_name: srv.name.clone(),
                        address: srv.address.clone(),
                        protocol: member.protocol.as_str().to_string(),
                        port_or_url: member.port_or_url,
                        weight: member.weight.max(1),
                        status: status.clone(),
                        accepting_connections: cand.accepting_connections,
                        active_connections: cand.active_connections as i64,
                        latency_ms: cand.latency_ms,
                        loss_pct: cand.loss_pct,
                        throughput_mbps: cand.throughput_mbps,
                        health_tier: tier.clone(),
                        reason: reason.clone(),
                    });
                    rank += 1;
                }
            }
        }

        for member in inactive_members {
            let Some(srv) = server_by_id.get(&member.server_id) else {
                continue;
            };
            out.push(PoolPreviewCandidateDto {
                rank,
                server_id: srv.id,
                server_name: srv.name.clone(),
                address: srv.address.clone(),
                protocol: member.protocol.as_str().to_string(),
                port_or_url: member.port_or_url,
                weight: member.weight.max(1),
                status: "inactive".to_string(),
                accepting_connections: false,
                active_connections: 0,
                latency_ms: None,
                loss_pct: None,
                throughput_mbps: None,
                health_tier: "inactive".to_string(),
                reason: "server_inactive".to_string(),
            });
            rank += 1;
        }

        GetPoolPreviewResponse::Ok(Json(PoolPreviewDto {
            pool_id: pool.id,
            pool_name: pool.name,
            strategy: pool.strategy,
            candidates: out,
        }))
    }
}
