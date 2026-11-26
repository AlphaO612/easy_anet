//! Алгоритмы выбора entry point для пула VPN-нод.
//!
//! Resolver панели сортирует узлы перед генерацией `client.toml`: первый узел
//! становится предпочтительным, остальные остаются в списке failover.

use sha2::{Digest, Sha256};
use uuid::Uuid;

#[derive(Debug, Clone, Copy)]
pub struct BalanceCandidate {
    /// Идентификатор зарегистрированной ноды.
    pub server_id: Uuid,
    /// Вес ноды в weighted-пуле; чем он выше, тем чаще нода выбирается первой.
    pub weight: u32,
    /// Число активных подключений из последнего heartbeat.
    pub active_connections: u64,
    /// Есть ли хотя бы одна запись runtime-состояния (heartbeat) от ноды.
    pub has_runtime: bool,
    /// Нода присылала свежий heartbeat (<= 45s).
    pub is_online: bool,
    /// Нода принимает новые подключения (не в режиме drain).
    pub accepting_connections: bool,
    /// Измеренная задержка (ms), если доступна из probe telemetry.
    pub latency_ms: Option<f64>,
    /// Потери пакетов в процентах (0.0..=100.0), если доступны.
    pub loss_pct: Option<f64>,
    /// Пропускная способность (Mbps), если доступна.
    pub throughput_mbps: Option<f64>,
}

/// Уровень здоровья кандидата (меньше — лучше):
/// - `0`: online, принимает подключения и потери < 25%
/// - `1`: online и принимает подключения с высокими потерями, либо новая нода до первого heartbeat
/// - `2`: online, но в режиме drain (`accepting_connections == false`)
/// - `3`: offline (устаревший heartbeat)
pub fn health_tier(candidate: &BalanceCandidate) -> u8 {
    if candidate.is_online && candidate.accepting_connections {
        let loss = candidate
            .loss_pct
            .filter(|v| v.is_finite() && *v >= 0.0)
            .unwrap_or(0.0);
        if loss < 25.0 { 0 } else { 1 }
    } else if !candidate.has_runtime {
        1
    } else if candidate.is_online && !candidate.accepting_connections {
        2
    } else {
        3
    }
}

/// Краткое текстовое пояснение выбранного health_tier для preview UI.
pub fn health_reason(candidate: &BalanceCandidate) -> &'static str {
    if candidate.is_online && candidate.accepting_connections {
        let loss = candidate
            .loss_pct
            .filter(|v| v.is_finite() && *v >= 0.0)
            .unwrap_or(0.0);
        if loss < 25.0 {
            "online_healthy"
        } else {
            "online_high_loss"
        }
    } else if !candidate.has_runtime {
        "no_heartbeat_yet"
    } else if candidate.is_online && !candidate.accepting_connections {
        "draining"
    } else {
        "offline"
    }
}

/// Упорядочивает кандидатов согласно выбранной стратегии пула с учётом health tier.
pub fn order_candidates(
    strategy: &str,
    user_id: Uuid,
    pool_id: Uuid,
    candidates: &mut [BalanceCandidate],
) {
    match strategy {
        // Least-connections использует актуальную нагрузку, health tier, latency bucket
        // и стабильный hash для детерминированного разрешения одинаковых значений.
        "least_connections" => candidates.sort_by_key(|candidate| {
            (
                health_tier(candidate),
                candidate.active_connections,
                latency_bucket_ms(candidate.latency_ms),
                stable_hash(user_id, pool_id, candidate.server_id),
            )
        }),
        // Latency / Speed приоритизируют минимальную задержку, минимальные потери и высокую пропускную способность.
        "latency" | "speed" => candidates.sort_by(|left, right| {
            health_tier(left)
                .cmp(&health_tier(right))
                .then_with(|| {
                    latency_speed_score(strategy, *left)
                        .total_cmp(&latency_speed_score(strategy, *right))
                })
                .then_with(|| left.active_connections.cmp(&right.active_connections))
                .then_with(|| {
                    stable_hash(user_id, pool_id, left.server_id)
                        .cmp(&stable_hash(user_id, pool_id, right.server_id))
                })
        }),
        // Weighted rendezvous сохраняет привязку пользователя к entry point,
        // но распределяет разных пользователей с учётом весов нод, нагрузки и телеметрии.
        _ => candidates.sort_by(|left, right| {
            health_tier(left)
                .cmp(&health_tier(right))
                .then_with(|| {
                    weighted_score(user_id, pool_id, *left)
                        .total_cmp(&weighted_score(user_id, pool_id, *right))
                })
                .then_with(|| {
                    stable_hash(user_id, pool_id, left.server_id)
                        .cmp(&stable_hash(user_id, pool_id, right.server_id))
                })
        }),
    }
}

fn stable_hash(user_id: Uuid, pool_id: Uuid, server_id: Uuid) -> u64 {
    let mut hasher = Sha256::new();
    hasher.update(user_id.as_bytes());
    hasher.update(pool_id.as_bytes());
    hasher.update(server_id.as_bytes());
    let digest = hasher.finalize();
    u64::from_be_bytes(digest[..8].try_into().expect("SHA-256 prefix"))
}

fn latency_bucket_ms(latency_ms: Option<f64>) -> u64 {
    latency_ms
        .filter(|v| v.is_finite() && *v >= 0.0)
        .map(|ms| (ms / 10.0).round() as u64)
        .unwrap_or(u64::MAX / 2)
}

fn latency_speed_score(strategy: &str, candidate: BalanceCandidate) -> f64 {
    let lat = candidate
        .latency_ms
        .filter(|v| v.is_finite() && *v >= 0.0)
        .unwrap_or(250.0);
    let loss_penalty = candidate
        .loss_pct
        .filter(|v| v.is_finite() && *v >= 0.0)
        .unwrap_or(0.0)
        * 15.0;
    let throughput = candidate
        .throughput_mbps
        .filter(|v| v.is_finite() && *v > 0.0)
        .unwrap_or(0.0);

    if strategy == "speed" {
        (lat + loss_penalty) / (1.0 + throughput / 50.0) + (candidate.active_connections as f64 * 0.5)
    } else {
        (lat + loss_penalty - (throughput.min(500.0) * 0.02)).max(0.0)
    }
}

fn weighted_score(user_id: Uuid, pool_id: Uuid, candidate: BalanceCandidate) -> f64 {
    let hash = stable_hash(user_id, pool_id, candidate.server_id);
    let uniform = (hash as f64 + 1.0) / (u64::MAX as f64 + 2.0);
    let weight = candidate.weight.max(1) as f64;
    let base = -uniform.ln() / weight;

    let load_factor = 1.0 + (candidate.active_connections as f64 / (weight * 25.0));
    let latency_factor = candidate
        .latency_ms
        .filter(|v| v.is_finite() && *v >= 0.0)
        .map(|ms| 1.0 + (ms / 500.0).min(2.0))
        .unwrap_or(1.0);
    let loss_factor = 1.0
        + (candidate
            .loss_pct
            .filter(|v| v.is_finite() && *v >= 0.0)
            .unwrap_or(0.0)
            / 25.0);

    base * load_factor * latency_factor * loss_factor
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mk_candidate(
        server_id: Uuid,
        weight: u32,
        active_connections: u64,
        has_runtime: bool,
        is_online: bool,
        accepting_connections: bool,
    ) -> BalanceCandidate {
        BalanceCandidate {
            server_id,
            weight,
            active_connections,
            has_runtime,
            is_online,
            accepting_connections,
            latency_ms: None,
            loss_pct: None,
            throughput_mbps: None,
        }
    }

    #[test]
    fn health_tiers_order_online_above_unknown_draining_and_offline() {
        let user = Uuid::new_v4();
        let pool = Uuid::new_v4();
        let healthy = Uuid::new_v4();
        let high_loss = Uuid::new_v4();
        let unknown = Uuid::new_v4();
        let draining = Uuid::new_v4();
        let offline = Uuid::new_v4();

        let mut high_loss_c = mk_candidate(high_loss, 100, 0, true, true, true);
        high_loss_c.loss_pct = Some(30.0);

        let mut candidates = vec![
            mk_candidate(offline, 100, 0, true, false, true),
            mk_candidate(draining, 100, 0, true, true, false),
            mk_candidate(unknown, 100, 0, false, false, true),
            high_loss_c,
            mk_candidate(healthy, 1, 50, true, true, true),
        ];

        order_candidates("least_connections", user, pool, &mut candidates);
        assert_eq!(candidates[0].server_id, healthy);
        assert_eq!(health_tier(&candidates[1]), 1);
        assert_eq!(health_tier(&candidates[2]), 1);
        assert_eq!(candidates[3].server_id, draining);
        assert_eq!(candidates[4].server_id, offline);
    }

    #[test]
    fn least_connections_puts_less_loaded_node_first_and_uses_latency_tiebreaker() {
        let user = Uuid::new_v4();
        let pool = Uuid::new_v4();
        let light_fast = Uuid::new_v4();
        let light_slow = Uuid::new_v4();
        let heavy = Uuid::new_v4();

        let mut c_fast = mk_candidate(light_fast, 1, 2, true, true, true);
        c_fast.latency_ms = Some(18.0);
        let mut c_slow = mk_candidate(light_slow, 1, 2, true, true, true);
        c_slow.latency_ms = Some(95.0);
        let c_heavy = mk_candidate(heavy, 5, 20, true, true, true);

        let mut candidates = vec![c_heavy, c_slow, c_fast];
        order_candidates("least_connections", user, pool, &mut candidates);
        assert_eq!(candidates[0].server_id, light_fast);
        assert_eq!(candidates[1].server_id, light_slow);
        assert_eq!(candidates[2].server_id, heavy);
    }

    #[test]
    fn latency_and_speed_strategies_prefer_low_latency_and_high_throughput() {
        let user = Uuid::new_v4();
        let pool = Uuid::new_v4();
        let fast_low_lat = Uuid::new_v4();
        let slow_high_lat = Uuid::new_v4();
        let high_bw = Uuid::new_v4();

        let mut c1 = mk_candidate(fast_low_lat, 1, 5, true, true, true);
        c1.latency_ms = Some(15.0);
        c1.throughput_mbps = Some(100.0);

        let mut c2 = mk_candidate(slow_high_lat, 1, 0, true, true, true);
        c2.latency_ms = Some(180.0);
        c2.throughput_mbps = Some(50.0);

        let mut c3 = mk_candidate(high_bw, 1, 0, true, true, true);
        c3.latency_ms = Some(30.0);
        c3.throughput_mbps = Some(900.0);

        let mut by_lat = vec![c2, c3, c1];
        order_candidates("latency", user, pool, &mut by_lat);
        assert_eq!(by_lat[0].server_id, fast_low_lat);
        assert_eq!(by_lat[2].server_id, slow_high_lat);

        let mut by_speed = vec![c2, c1, c3];
        order_candidates("speed", user, pool, &mut by_speed);
        assert_eq!(by_speed[0].server_id, high_bw);
        assert_eq!(by_speed[2].server_id, slow_high_lat);
    }

    #[test]
    fn weighted_rendezvous_favours_larger_weight() {
        let pool = Uuid::new_v4();
        let light = Uuid::new_v4();
        let heavy = Uuid::new_v4();
        let mut light_wins = 0;
        let mut heavy_wins = 0;
        for _ in 0..1_000 {
            let mut candidates = vec![
                mk_candidate(light, 1, 0, true, true, true),
                mk_candidate(heavy, 5, 0, true, true, true),
            ];
            order_candidates("weighted", Uuid::new_v4(), pool, &mut candidates);
            if candidates[0].server_id == heavy {
                heavy_wins += 1;
            } else {
                light_wins += 1;
            }
        }
        assert!(
            heavy_wins > light_wins * 3,
            "heavy={heavy_wins}, light={light_wins}"
        );
    }

    #[test]
    fn tie_breaking_is_deterministic_for_same_user_and_pool() {
        let user = Uuid::new_v4();
        let pool = Uuid::new_v4();
        let a = Uuid::new_v4();
        let b = Uuid::new_v4();

        let mut first = vec![
            mk_candidate(a, 1, 3, true, true, true),
            mk_candidate(b, 1, 3, true, true, true),
        ];
        let mut second = vec![
            mk_candidate(b, 1, 3, true, true, true),
            mk_candidate(a, 1, 3, true, true, true),
        ];
        order_candidates("least_connections", user, pool, &mut first);
        order_candidates("least_connections", user, pool, &mut second);
        assert_eq!(first[0].server_id, second[0].server_id);
        assert_eq!(first[1].server_id, second[1].server_id);
    }
}
