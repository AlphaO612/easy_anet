use crate::api::api::{matches_token_hash, node_token_is_valid, sha256_hex, validate_admin_session};
use crate::api::dto::AdminToken;
use crate::crypto::DbEncryptor;
use crate::entities::{
    ingress_routes, node_runtime_states, servers, site_bundles, system_settings,
    telegram_alert_configs, traffic_hourly,
};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use chrono::Utc;
use log::{info, warn};
use poem::{error::InternalServerError, http::StatusCode, Error, Result};
use poem_openapi::{
    param::{Header, Path, Query},
    payload::Json,
    Object, OpenApi,
};
use sea_orm::sea_query::OnConflict;
use sea_orm::{ColumnTrait, DatabaseConnection, EntityTrait, QueryFilter, QueryOrder, Set};
use std::collections::{HashMap, HashSet};
use std::time::{Duration, Instant};

pub struct AlertsAndSitesApi {
    pub db: DatabaseConnection,
}

#[derive(Object, Debug, Clone)]
pub struct TelegramAlertConfigDto {
    pub enabled: bool,
    pub has_bot_token: bool,
    pub chat_id: String,
    pub cooldown_minutes: i32,
    pub proxy_url: String,
    pub api_base_url: String,
    pub updated_at: Option<String>,
}

#[derive(Object, Debug, Clone)]
pub struct SaveTelegramAlertRequest {
    pub enabled: bool,
    /// Оставьте пустым, чтобы сохранить текущий зашифрованный токен.
    pub bot_token: Option<String>,
    pub chat_id: String,
    pub cooldown_minutes: Option<i32>,
    /// HTTP/HTTPS прокси для доставки уведомлений (например http://user:pass@exit:3128)
    pub proxy_url: Option<String>,
    /// Альтернативный адрес Telegram Bot API (например reverse-proxy на зарубежном exit-сервере)
    pub api_base_url: Option<String>,
}

#[derive(Object, Debug, Clone)]
pub struct TelegramTestResponse {
    pub ok: bool,
    pub message: String,
}

#[derive(Object, Debug, Clone)]
pub struct SitePreviewDto {
    pub domain: String,
    pub mode: String,
    pub preset: String,
    pub title: String,
    pub html: String,
}

#[derive(Object, Debug, Clone)]
pub struct SystemSettingsDto {
    /// "balanced" | "low_latency" | "high_throughput" | "failover_only"
    pub operating_mode: String,
    pub multi_exit_enabled: bool,
    pub parallel_transports: bool,
    pub auto_failover: bool,
    pub metrics_enabled: bool,
    pub metrics_retention_days: i32,
    pub probe_interval_seconds: i32,
    pub sync_interval_seconds: i32,
    pub subscription_origin_override: String,
    pub updated_at: Option<String>,
}

#[derive(Object, Debug, Clone)]
pub struct SaveSystemSettingsRequest {
    pub operating_mode: String,
    pub multi_exit_enabled: bool,
    pub parallel_transports: bool,
    pub auto_failover: bool,
    pub metrics_enabled: bool,
    pub metrics_retention_days: i32,
    pub probe_interval_seconds: i32,
    pub sync_interval_seconds: i32,
    pub subscription_origin_override: String,
}

#[derive(Object, Debug, Clone)]
pub struct PruneMetricsResponse {
    pub deleted_rows: u64,
    pub retention_days: i32,
}

#[derive(Object, Debug, Clone)]
pub struct SitePresetDto {
    pub id: String,
    pub name: String,
    pub description: String,
}

#[derive(Object, Debug, Clone)]
pub struct SiteBundleDto {
    pub domain: String,
    pub mode: String,
    pub preset: String,
    pub title: String,
    pub bundle_sha256: String,
    pub size_bytes: i64,
    pub updated_at: Option<String>,
    pub configured: bool,
}

#[derive(Object, Debug, Clone)]
pub struct SitesListResponseDto {
    pub presets: Vec<SitePresetDto>,
    pub sites: Vec<SiteBundleDto>,
}

#[derive(Object, Debug, Clone)]
pub struct SaveSiteBundleRequest {
    /// "preset" | "custom_html" | "custom_zip"
    pub mode: String,
    pub preset: Option<String>,
    pub title: Option<String>,
    /// Исходный HTML (для mode = "custom_html")
    pub html: Option<String>,
    /// Base64 ZIP-архива (для mode = "custom_zip", до 4 МБ)
    pub zip_base64: Option<String>,
}

#[derive(Object, Debug, Clone)]
pub struct GatewaySitePayloadDto {
    pub domain: String,
    pub mode: String,
    pub sha256: String,
    pub bundle_base64: String,
}

const MAX_HTML_BYTES: usize = 1024 * 1024; // 1 MiB
const MAX_ZIP_BYTES: usize = 4 * 1024 * 1024; // 4 MiB

fn escape_html(input: &str) -> String {
    input
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

pub fn built_in_presets() -> Vec<SitePresetDto> {
    vec![
        SitePresetDto {
            id: "studio".into(),
            name: "Графическая студия".into(),
            description: "Минималистичный сайт дизайн-студии в светлой палитре".into(),
        },
        SitePresetDto {
            id: "journal".into(),
            name: "Технический журнал".into(),
            description: "Редакционная страница заметок о графике, верстке и свете".into(),
        },
        SitePresetDto {
            id: "cloud".into(),
            name: "Облачная документация / SaaS".into(),
            description: "Строгая техническая страница облачной инфраструктуры и API".into(),
        },
        SitePresetDto {
            id: "portfolio".into(),
            name: "Портфолио инженера".into(),
            description: "Личный сайт-визитка разработчика с проектами и контактами".into(),
        },
        SitePresetDto {
            id: "status".into(),
            name: "Статус-страница сервиса".into(),
            description: "Спокойная страница состояния веб-сервисов и CDN-узлов".into(),
        },
    ]
}

pub fn render_preset_html(preset: &str, custom_title: &str) -> String {
    let title_override = custom_title.trim();
    match preset {
        "journal" => {
            let title = if title_override.is_empty() { "Бумажные формы" } else { title_override };
            let t = escape_html(title);
            format!(
                r#"<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>{t}</title><style>:root{{--bg:#f6f3eb;--ink:#1f2822;--muted:#526158;--acc:#b4532a}}body{{margin:0;background:var(--bg);color:var(--ink);font:1rem/1.6 system-ui,sans-serif}}main{{max-width:68rem;margin:auto;padding:3rem 1.5rem}}header{{border-bottom:2px solid var(--ink);padding-bottom:.8rem;font-weight:700;letter-spacing:.03em}}h1{{font:400 clamp(2.5rem,7vw,5rem)/1.05 Georgia,serif;margin:2.2rem 0 1rem}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(16rem,1fr));gap:2rem;margin-top:2.5rem;padding-top:2rem;border-top:1px solid #cbd2ca}}.muted{{color:var(--muted)}}</style></head><body><main><header>{t}</header><h1>{t}</h1><p class="muted">Полевые заметки о типографике, композиции и спокойных интерфейсах.</p><section class="grid"><article><h2>Свет и контраст</h2><p>Спокойная сетка и предсказуемый ритм делают длинные тексты читаемыми на любом экране.</p></article><article><h2>Модульная верстка</h2><p>Минимум декоративных элементов, чистая иерархия заголовков и точные отступы.</p></article></section></main></body></html>"#
            )
        }
        "cloud" => {
            let title = if title_override.is_empty() { "Nordic Edge Docs" } else { title_override };
            let t = escape_html(title);
            format!(
                r#"<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>{t}</title><style>body{{margin:0;background:#0f172a;color:#e2e8f0;font:15px/1.65 system-ui,sans-serif}}main{{max-width:64rem;margin:auto;padding:3.2rem 1.5rem}}nav{{display:flex;justify-content:space-between;align-items:center;padding-bottom:1.2rem;border-bottom:1px solid #1e293b}}.badge{{background:#0ea5e922;color:#38bdf8;border:1px solid #0ea5e955;padding:.2rem .65rem;border-radius:999px;font-size:12px}}h1{{font-size:clamp(2rem,5vw,3.3rem);margin:2rem 0 .8rem}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));gap:1rem;margin-top:2rem}}.card{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:1.25rem}}p{{color:#94a3b8}}</style></head><body><main><nav><strong>{t}</strong><span class="badge">API v2.4 · EU</span></nav><h1>Edge Telemetry &amp; Content Delivery</h1><p>Static documentation portal for regional edge caching, health probes, and TLS certificate automation.</p><section class="cards"><div class="card"><h3>Global Anycast</h3><p>Sub-40ms routing across European points of presence with automated failover.</p></div><div class="card"><h3>Zero-Config TLS</h3><p>Automated ACME issuance and seamless key rotation for custom domains.</p></div><div class="card"><h3>Observability</h3><p>Real-time latency histograms and structured JSON audit logs.</p></div></section></main></body></html>"#
            )
        }
        "portfolio" => {
            let title = if title_override.is_empty() { "Alexey K. — Systems Engineer" } else { title_override };
            let t = escape_html(title);
            format!(
                r#"<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>{t}</title><style>body{{margin:0;background:#fafafa;color:#18181b;font:16px/1.65 ui-sans-serif,system-ui,sans-serif}}main{{max-width:46rem;margin:auto;padding:4rem 1.5rem}}h1{{font-size:2.4rem;letter-spacing:-.03em;margin:0 0 .5rem}}.sub{{color:#52525b;margin-bottom:2.5rem}}.item{{padding:1.1rem 0;border-top:1px solid #e4e4e7}}.item h3{{margin:0 0 .25rem}}.item p{{margin:0;color:#52525b}}</style></head><body><main><h1>{t}</h1><p class="sub">Distributed systems, Linux networking, and Rust tooling. Based in Tallinn.</p><div class="item"><h3>netbench-rs</h3><p>Lightweight TCP/QUIC latency benchmarking CLI for multi-region clusters.</p></div><div class="item"><h3>pg-snap</h3><p>Incremental WAL snapshot verifier for PostgreSQL 17 clusters.</p></div><div class="item"><h3>Contact</h3><p>Open to consulting on observability, SRE automation, and high-throughput proxies.</p></div></main></body></html>"#
            )
        }
        "status" => {
            let title = if title_override.is_empty() { "Service Status" } else { title_override };
            let t = escape_html(title);
            format!(
                r#"<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>{t}</title><style>body{{margin:0;background:#f8fafc;color:#0f172a;font:15px/1.6 system-ui,sans-serif}}main{{max-width:48rem;margin:auto;padding:3rem 1.5rem}}.banner{{background:#ecfdf5;border:1px solid #6ee7b7;color:#065f46;padding:1rem 1.25rem;border-radius:10px;font-weight:600;margin:1.5rem 0}}.row{{display:flex;justify-content:space-between;padding:.9rem 1rem;background:#fff;border:1px solid #e2e8f0;margin-top:.6rem;border-radius:8px}}.ok{{color:#059669;font-weight:600}}</style></head><body><main><h1>{t}</h1><div class="banner">All Systems Operational</div><div class="row"><span>Edge Web Gateway</span><span class="ok">Operational</span></div><div class="row"><span>Static Asset CDN</span><span class="ok">Operational</span></div><div class="row"><span>TLS Provisioning</span><span class="ok">Operational</span></div><div class="row"><span>Metrics Collector</span><span class="ok">Operational</span></div></main></body></html>"#
            )
        }
        _ => {
            let title = if title_override.is_empty() { "Этюды формы" } else { title_override };
            let t = escape_html(title);
            format!(
                r#"<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>{t}</title><style>:root{{--paper:#f5f2e9;--ink:#1c2924;--muted:#4a5a50;--accent:#bd572f}}body{{margin:0;background:var(--paper);color:var(--ink);font:1rem/1.55 system-ui,sans-serif}}main{{max-width:74rem;margin:auto;padding:clamp(1.3rem,5vw,4.2rem)}}header{{display:flex;align-items:center;gap:.75rem;padding-bottom:1rem;border-bottom:2px solid;font-weight:650}}header::before{{content:'';width:1rem;height:1rem;background:var(--accent);border-radius:50%}}h1,h2{{font-family:Georgia,serif;font-weight:400;line-height:1.05}}h1{{font-size:clamp(2.8rem,7vw,5.8rem);margin:2rem 0 1.2rem;letter-spacing:-.04em}}.lede{{color:var(--muted);max-width:39rem;font-size:1.2rem}}.notes{{display:grid;grid-template-columns:repeat(auto-fit,minmax(16rem,1fr));gap:2rem;margin-top:3rem;padding-top:2rem;border-top:1px solid var(--ink)}}</style></head><body><main><header>{t}</header><h1>{t}</h1><p class="lede">Круг, плоскость и срез. Небольшая коллекция композиций, построенных на взаимодействии простых фигур.</p><section class="notes"><article><h2>Ритм</h2><p>Дуга удерживает взгляд у левого края. Диагональ справа задаёт второе движение и оставляет между фигурами узкую полосу фона.</p></article><article><h2>Палитра</h2><p>Хвойный зелёный, шалфейный и обожжённая глина. Светлый фон связывает три цвета и сохраняет контраст.</p></article></section></main></body></html>"#
            )
        }
    }
}

pub async fn resolve_site_bundle_for_domain(
    db: &DatabaseConnection,
    domain: &str,
) -> Option<site_bundles::Model> {
    let target = domain.trim().to_lowercase();
    if let Ok(Some(exact)) = site_bundles::Entity::find_by_id(target).one(db).await {
        return Some(exact);
    }
    site_bundles::Entity::find_by_id("*".to_string())
        .one(db)
        .await
        .ok()
        .flatten()
}

pub async fn load_system_settings(db: &DatabaseConnection) -> system_settings::Model {
    if let Ok(Some(row)) = system_settings::Entity::find_by_id(1).one(db).await {
        return row;
    }
    system_settings::Model {
        id: 1,
        operating_mode: "balanced".to_string(),
        multi_exit_enabled: true,
        parallel_transports: true,
        auto_failover: true,
        metrics_enabled: true,
        metrics_retention_days: 30,
        probe_interval_seconds: 30,
        sync_interval_seconds: 30,
        subscription_origin_override: String::new(),
        updated_at: Utc::now().naive_utc(),
    }
}

async fn send_telegram_message(
    bot_token: &str,
    chat_id: &str,
    text: &str,
    proxy_url: &str,
    api_base_url: &str,
) -> anyhow::Result<()> {
    let base = api_base_url.trim().trim_end_matches('/');
    let base_url = if base.is_empty() {
        "https://api.telegram.org"
    } else if base.starts_with("https://") || base.starts_with("http://") {
        base
    } else {
        anyhow::bail!("API Base URL должен начинаться с https:// или http://");
    };
    let url = format!("{}/bot{}/sendMessage", base_url, bot_token.trim());
    let mut builder = reqwest::Client::builder().timeout(Duration::from_secs(14));
    let proxy_clean = proxy_url.trim();
    if !proxy_clean.is_empty() {
        let proxy = reqwest::Proxy::all(proxy_clean)
            .map_err(|e| anyhow::anyhow!("Некорректный proxy_url '{}': {}", proxy_clean, e))?;
        builder = builder.proxy(proxy);
    }
    let client = builder.build()?;
    let resp = client
        .post(&url)
        .json(&serde_json::json!({
            "chat_id": chat_id.trim(),
            "text": text,
            "disable_web_page_preview": true
        }))
        .send()
        .await?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        anyhow::bail!("Telegram API HTTP {}: {}", status, body.chars().take(300).collect::<String>());
    }
    Ok(())
}

/// Фоновый мониторинг узлов и маршрутов с отправкой алертов в Telegram при падении и восстановлении
/// + автоматическая ротация старых метрик по расписанию.
pub fn spawn_alert_monitor(db: DatabaseConnection) {
    tokio::spawn(async move {
        let mut unhealthy_since: HashMap<String, Instant> = HashMap::new();
        let mut last_notified: HashMap<String, Instant> = HashMap::new();
        let mut last_prune = Instant::now();
        // Прогрев 60с после старта, чтобы шлюзы успели прислать первый синк
        tokio::time::sleep(Duration::from_secs(60)).await;
        info!("Telegram alert monitor & metrics retention worker started");

        loop {
            // Автоматическая ротация старых метрик раз в 6 часов
            if last_prune.elapsed() >= Duration::from_secs(6 * 3600) {
                let settings = load_system_settings(&db).await;
                let days = settings.metrics_retention_days.clamp(1, 365);
                let cutoff = Utc::now().naive_utc() - chrono::Duration::days(days as i64);
                if let Ok(res) = traffic_hourly::Entity::delete_many()
                    .filter(traffic_hourly::Column::BucketStart.lt(cutoff))
                    .exec(&db)
                    .await
                {
                    if res.rows_affected > 0 {
                        info!(
                            "Metrics retention pruned {} traffic_hourly rows older than {} days",
                            res.rows_affected, days
                        );
                    }
                }
                last_prune = Instant::now();
            }

            if let Ok(Some(cfg)) = telegram_alert_configs::Entity::find_by_id(1).one(&db).await {
                if cfg.enabled && !cfg.chat_id.trim().is_empty() {
                    let encryptor = DbEncryptor::new();
                    let bot_token = cfg
                        .encrypted_bot_token
                        .as_deref()
                        .and_then(|ct| encryptor.decrypt(ct).ok())
                        .unwrap_or_default();
                    if !bot_token.trim().is_empty() {
                        let cooldown = Duration::from_secs((cfg.cooldown_minutes.max(1) as u64) * 60);
                        let now_dt = Utc::now().naive_utc();
                        let now_inst = Instant::now();
                        let mut current_issues: HashMap<String, String> = HashMap::new();

                        // 1. Проверяем маршруты VLESS / WireGuard
                        if let Ok(routes) = ingress_routes::Entity::find().all(&db).await {
                            for r in routes {
                                let key = format!("route:{}", r.id);
                                if r.uplink_up == Some(false) {
                                    current_issues.insert(
                                        key,
                                        format!(
                                            "🔴 Маршрут {} ({} {}:{} → {}): аплинк-туннель DOWN",
                                            r.id, r.protocol.to_uppercase(), r.entry_host, r.entry_port, r.exit_host
                                        ),
                                    );
                                } else if r.last_loss_pct.is_some_and(|l| l >= 50.0) {
                                    current_issues.insert(
                                        key,
                                        format!(
                                            "🟠 Маршрут {} ({} → {}): высокие потери {:.0}%",
                                            r.id, r.entry_host, r.exit_host, r.last_loss_pct.unwrap_or(100.0)
                                        ),
                                    );
                                } else if let Some(ts) = r.last_sync_at {
                                    let age = now_dt.signed_duration_since(ts).num_seconds();
                                    if age > 240 {
                                        current_issues.insert(
                                            key,
                                            format!(
                                                "🔴 Шлюз {} (маршрут {}): нет синхронизации {} сек",
                                                r.entry_host, r.id, age
                                            ),
                                        );
                                    }
                                }
                            }
                        }

                        // 2. Проверяем серверы, которые уже хотя бы раз слали heartbeat
                        if let (Ok(srvs), Ok(rts)) = (
                            servers::Entity::find().all(&db).await,
                            node_runtime_states::Entity::find().all(&db).await,
                        ) {
                            let rt_map: HashMap<_, _> =
                                rts.into_iter().map(|r| (r.server_id, r)).collect();
                            for s in srvs {
                                if !s.is_active {
                                    continue;
                                }
                                if let Some(rt) = rt_map.get(&s.id) {
                                    let age = now_dt.signed_duration_since(rt.last_seen_at).num_seconds();
                                    if age > 180 {
                                        let key = format!("node:{}", s.id);
                                        current_issues.insert(
                                            key,
                                            format!(
                                                "🔴 Нода «{}» ({}): OFFLINE (нет heartbeat {} сек)",
                                                s.name, s.address, age
                                            ),
                                        );
                                    }
                                }
                            }
                        }

                        // Отправляем алерты о новых или повторяющихся проблемах
                        for (key, msg) in &current_issues {
                            unhealthy_since.entry(key.clone()).or_insert(now_inst);
                            let should_send = last_notified
                                .get(key)
                                .map_or(true, |prev| now_inst.duration_since(*prev) >= cooldown);
                            if should_send {
                                match send_telegram_message(
                                    &bot_token,
                                    &cfg.chat_id,
                                    msg,
                                    &cfg.proxy_url,
                                    &cfg.api_base_url,
                                )
                                .await
                                {
                                    Ok(()) => {
                                        last_notified.insert(key.clone(), now_inst);
                                    }
                                    Err(e) => warn!("Failed to send Telegram alert: {}", e),
                                }
                            }
                        }

                        // Отправляем уведомления о восстановлении
                        let recovered: Vec<String> = unhealthy_since
                            .keys()
                            .filter(|k| !current_issues.contains_key(*k))
                            .cloned()
                            .collect();
                        for key in recovered {
                            unhealthy_since.remove(&key);
                            if last_notified.remove(&key).is_some() {
                                let msg = format!("🟢 Восстановлено: {}", key);
                                if let Err(e) = send_telegram_message(
                                    &bot_token,
                                    &cfg.chat_id,
                                    &msg,
                                    &cfg.proxy_url,
                                    &cfg.api_base_url,
                                )
                                .await
                                {
                                    warn!("Failed to send Telegram recovery alert: {}", e);
                                }
                            }
                        }
                    }
                }
            }

            tokio::time::sleep(Duration::from_secs(60)).await;
        }
    });
}

#[OpenApi]
impl AlertsAndSitesApi {
    /// Получить настройки Telegram-уведомлений.
    #[oai(path = "/alerts/telegram", method = "get")]
    async fn get_telegram_config(&self, auth: AdminToken) -> Result<Json<TelegramAlertConfigDto>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;

        let row = telegram_alert_configs::Entity::find_by_id(1)
            .one(&self.db)
            .await
            .map_err(InternalServerError)?;

        Ok(Json(match row {
            Some(m) => TelegramAlertConfigDto {
                enabled: m.enabled,
                has_bot_token: m.encrypted_bot_token.as_deref().is_some_and(|s| !s.is_empty()),
                chat_id: m.chat_id,
                cooldown_minutes: m.cooldown_minutes,
                proxy_url: m.proxy_url,
                api_base_url: m.api_base_url,
                updated_at: Some(m.updated_at.and_utc().to_rfc3339()),
            },
            None => TelegramAlertConfigDto {
                enabled: false,
                has_bot_token: false,
                chat_id: String::new(),
                cooldown_minutes: 15,
                proxy_url: String::new(),
                api_base_url: String::new(),
                updated_at: None,
            },
        }))
    }

    /// Сохранить настройки Telegram-уведомлений (включая прокси / альтернативный API-маршрут).
    #[oai(path = "/alerts/telegram", method = "put")]
    async fn save_telegram_config(
        &self,
        auth: AdminToken,
        req: Json<SaveTelegramAlertRequest>,
    ) -> Result<Json<TelegramAlertConfigDto>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;

        let existing = telegram_alert_configs::Entity::find_by_id(1)
            .one(&self.db)
            .await
            .map_err(InternalServerError)?;

        let encryptor = DbEncryptor::new();
        let encrypted_token = match req.0.bot_token.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
            Some(raw) => Some(
                encryptor
                    .encrypt(raw)
                    .map_err(|e| Error::from_string(e.to_string(), StatusCode::INTERNAL_SERVER_ERROR))?,
            ),
            None => existing.as_ref().and_then(|m| m.encrypted_bot_token.clone()),
        };

        let cooldown = req.0.cooldown_minutes.unwrap_or(15).clamp(1, 1440);
        let chat_id = req.0.chat_id.trim().to_string();
        let proxy_url = req
            .0
            .proxy_url
            .map(|s| s.trim().to_string())
            .unwrap_or_else(|| existing.as_ref().map(|m| m.proxy_url.clone()).unwrap_or_default());
        let api_base_url = req
            .0
            .api_base_url
            .map(|s| s.trim().trim_end_matches('/').to_string())
            .unwrap_or_else(|| existing.as_ref().map(|m| m.api_base_url.clone()).unwrap_or_default());
        if !api_base_url.is_empty()
            && !api_base_url.starts_with("https://")
            && !api_base_url.starts_with("http://")
        {
            return Err(Error::from_string(
                "API Base URL должен начинаться с https:// или http://",
                StatusCode::BAD_REQUEST,
            ));
        }
        let now = Utc::now().naive_utc();

        telegram_alert_configs::Entity::insert(telegram_alert_configs::ActiveModel {
            id: Set(1),
            enabled: Set(req.0.enabled),
            encrypted_bot_token: Set(encrypted_token.clone()),
            chat_id: Set(chat_id.clone()),
            cooldown_minutes: Set(cooldown),
            proxy_url: Set(proxy_url.clone()),
            api_base_url: Set(api_base_url.clone()),
            updated_at: Set(now),
        })
        .on_conflict(
            OnConflict::column(telegram_alert_configs::Column::Id)
                .update_columns([
                    telegram_alert_configs::Column::Enabled,
                    telegram_alert_configs::Column::EncryptedBotToken,
                    telegram_alert_configs::Column::ChatId,
                    telegram_alert_configs::Column::CooldownMinutes,
                    telegram_alert_configs::Column::ProxyUrl,
                    telegram_alert_configs::Column::ApiBaseUrl,
                    telegram_alert_configs::Column::UpdatedAt,
                ])
                .to_owned(),
        )
        .exec_without_returning(&self.db)
        .await
        .map_err(InternalServerError)?;

        Ok(Json(TelegramAlertConfigDto {
            enabled: req.0.enabled,
            has_bot_token: encrypted_token.is_some(),
            chat_id,
            cooldown_minutes: cooldown,
            proxy_url,
            api_base_url,
            updated_at: Some(now.and_utc().to_rfc3339()),
        }))
    }

    /// Отправить тестовое уведомление в Telegram.
    #[oai(path = "/alerts/telegram/test", method = "post")]
    async fn test_telegram(&self, auth: AdminToken) -> Result<Json<TelegramTestResponse>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;

        let cfg = telegram_alert_configs::Entity::find_by_id(1)
            .one(&self.db)
            .await
            .map_err(InternalServerError)?
            .ok_or_else(|| Error::from_string("Сначала сохраните Bot Token и Chat ID", StatusCode::BAD_REQUEST))?;

        if cfg.chat_id.trim().is_empty() {
            return Err(Error::from_string("Не указан Chat ID", StatusCode::BAD_REQUEST));
        }
        let encryptor = DbEncryptor::new();
        let token = cfg
            .encrypted_bot_token
            .as_deref()
            .and_then(|ct| encryptor.decrypt(ct).ok())
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| Error::from_string("Не указан Bot Token", StatusCode::BAD_REQUEST))?;

        let route_note = if !cfg.api_base_url.trim().is_empty() {
            format!(" (через {})", cfg.api_base_url.trim())
        } else if !cfg.proxy_url.trim().is_empty() {
            " (через HTTP-прокси)".to_string()
        } else {
            " (напрямую)".to_string()
        };
        let msg = format!(
            "✅ ANet Easy: тестовое уведомление успешно доставлено{}. Мониторинг нод и шлюзов активен.",
            route_note
        );
        match send_telegram_message(&token, &cfg.chat_id, &msg, &cfg.proxy_url, &cfg.api_base_url).await {
            Ok(()) => Ok(Json(TelegramTestResponse {
                ok: true,
                message: format!("Сообщение отправлено в Telegram{}", route_note),
            })),
            Err(e) => Err(Error::from_string(e.to_string(), StatusCode::BAD_GATEWAY)),
        }
    }

    /// Предпросмотр встроенного пресета сайта маскировки (возвращает готовый HTML).
    #[oai(path = "/sites/preview", method = "get")]
    async fn preview_site_preset(
        &self,
        auth: AdminToken,
        preset: Query<Option<String>>,
        title: Query<Option<String>>,
    ) -> Result<Json<SitePreviewDto>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;

        let preset_id = preset
            .0
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .unwrap_or("studio")
            .to_lowercase();
        let custom_title = title.0.unwrap_or_default();
        let html = render_preset_html(&preset_id, &custom_title);
        Ok(Json(SitePreviewDto {
            domain: "*".into(),
            mode: "preset".into(),
            preset: preset_id,
            title: custom_title,
            html,
        }))
    }

    /// Получить сохранённый HTML сайта маскировки для домена (для live-предпросмотра в WebUI).
    #[oai(path = "/sites/:domain/html", method = "get")]
    async fn get_site_html(
        &self,
        auth: AdminToken,
        domain: Path<String>,
    ) -> Result<Json<SitePreviewDto>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;

        let dom = domain.0.trim().to_lowercase();
        if let Some(bundle) = resolve_site_bundle_for_domain(&self.db, &dom).await {
            let html = if bundle.mode == "custom_zip" {
                format!(
                    r#"<!doctype html><html lang="ru"><head><meta charset="utf-8"><style>body{{margin:0;background:#0f172a;color:#e2e8f0;font:15px/1.6 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh}}.card{{max-width:34rem;padding:2rem;background:#1e293b;border:1px solid #334155;border-radius:14px}}h2{{margin:0 0 .5rem}}code{{color:#38bdf8}}</style></head><body><div class="card"><h2>ZIP-архив для {}</h2><p>Загружен многофайловый статический бандл (SHA-256: <code>{}</code>). Для проверки откройте живой домен в новой вкладке.</p></div></body></html>"#,
                    escape_html(&dom),
                    escape_html(&bundle.bundle_sha256.chars().take(16).collect::<String>())
                )
            } else {
                STANDARD
                    .decode(&bundle.bundle_base64)
                    .ok()
                    .and_then(|b| String::from_utf8(b).ok())
                    .unwrap_or_else(|| render_preset_html(&bundle.preset, &bundle.title))
            };
            Ok(Json(SitePreviewDto {
                domain: dom,
                mode: bundle.mode,
                preset: bundle.preset,
                title: bundle.title,
                html,
            }))
        } else {
            Ok(Json(SitePreviewDto {
                domain: dom,
                mode: "preset".into(),
                preset: "studio".into(),
                title: "Этюды формы".into(),
                html: render_preset_html("studio", ""),
            }))
        }
    }

    /// Получить глобальные системные настройки (режим работы, ускорение, метрики и их ротация).
    #[oai(path = "/settings", method = "get")]
    async fn get_system_settings(&self, auth: AdminToken) -> Result<Json<SystemSettingsDto>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;

        let m = load_system_settings(&self.db).await;
        Ok(Json(SystemSettingsDto {
            operating_mode: m.operating_mode,
            multi_exit_enabled: m.multi_exit_enabled,
            parallel_transports: m.parallel_transports,
            auto_failover: m.auto_failover,
            metrics_enabled: m.metrics_enabled,
            metrics_retention_days: m.metrics_retention_days,
            probe_interval_seconds: m.probe_interval_seconds,
            sync_interval_seconds: m.sync_interval_seconds,
            subscription_origin_override: m.subscription_origin_override,
            updated_at: Some(m.updated_at.and_utc().to_rfc3339()),
        }))
    }

    /// Сохранить глобальные системные настройки.
    #[oai(path = "/settings", method = "put")]
    async fn save_system_settings(
        &self,
        auth: AdminToken,
        req: Json<SaveSystemSettingsRequest>,
    ) -> Result<Json<SystemSettingsDto>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;

        let mode = req.0.operating_mode.trim().to_lowercase();
        if !matches!(
            mode.as_str(),
            "balanced" | "low_latency" | "high_throughput" | "failover_only"
        ) {
            return Err(Error::from_string(
                "Режим работы должен быть balanced, low_latency, high_throughput или failover_only",
                StatusCode::BAD_REQUEST,
            ));
        }

        let retention = req.0.metrics_retention_days.clamp(1, 365);
        let probe_int = req.0.probe_interval_seconds.clamp(10, 600);
        let sync_int = req.0.sync_interval_seconds.clamp(10, 300);
        let origin = req
            .0
            .subscription_origin_override
            .trim()
            .trim_end_matches('/')
            .to_string();
        if !origin.is_empty() && !origin.starts_with("https://") && !origin.starts_with("http://") {
            return Err(Error::from_string(
                "Адрес подписки должен начинаться с https:// или http://",
                StatusCode::BAD_REQUEST,
            ));
        }
        let now = Utc::now().naive_utc();

        system_settings::Entity::insert(system_settings::ActiveModel {
            id: Set(1),
            operating_mode: Set(mode.clone()),
            multi_exit_enabled: Set(req.0.multi_exit_enabled),
            parallel_transports: Set(req.0.parallel_transports),
            auto_failover: Set(req.0.auto_failover),
            metrics_enabled: Set(req.0.metrics_enabled),
            metrics_retention_days: Set(retention),
            probe_interval_seconds: Set(probe_int),
            sync_interval_seconds: Set(sync_int),
            subscription_origin_override: Set(origin.clone()),
            updated_at: Set(now),
        })
        .on_conflict(
            OnConflict::column(system_settings::Column::Id)
                .update_columns([
                    system_settings::Column::OperatingMode,
                    system_settings::Column::MultiExitEnabled,
                    system_settings::Column::ParallelTransports,
                    system_settings::Column::AutoFailover,
                    system_settings::Column::MetricsEnabled,
                    system_settings::Column::MetricsRetentionDays,
                    system_settings::Column::ProbeIntervalSeconds,
                    system_settings::Column::SyncIntervalSeconds,
                    system_settings::Column::SubscriptionOriginOverride,
                    system_settings::Column::UpdatedAt,
                ])
                .to_owned(),
        )
        .exec_without_returning(&self.db)
        .await
        .map_err(InternalServerError)?;

        Ok(Json(SystemSettingsDto {
            operating_mode: mode,
            multi_exit_enabled: req.0.multi_exit_enabled,
            parallel_transports: req.0.parallel_transports,
            auto_failover: req.0.auto_failover,
            metrics_enabled: req.0.metrics_enabled,
            metrics_retention_days: retention,
            probe_interval_seconds: probe_int,
            sync_interval_seconds: sync_int,
            subscription_origin_override: origin,
            updated_at: Some(now.and_utc().to_rfc3339()),
        }))
    }

    /// Ручная ротация/очистка старых записей истории трафика старше заданного срока хранения.
    #[oai(path = "/settings/prune-metrics", method = "post")]
    async fn prune_metrics(
        &self,
        auth: AdminToken,
        days: Query<Option<i32>>,
    ) -> Result<Json<PruneMetricsResponse>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;

        let settings = load_system_settings(&self.db).await;
        let retention_days = days.0.unwrap_or(settings.metrics_retention_days).clamp(1, 365);
        let cutoff = Utc::now().naive_utc() - chrono::Duration::days(retention_days as i64);
        let res = traffic_hourly::Entity::delete_many()
            .filter(traffic_hourly::Column::BucketStart.lt(cutoff))
            .exec(&self.db)
            .await
            .map_err(InternalServerError)?;

        Ok(Json(PruneMetricsResponse {
            deleted_rows: res.rows_affected,
            retention_days,
        }))
    }

    /// Список сайтов маскировки HTTPS (пресеты и загруженные HTML/ZIP бандлы по доменам).
    #[oai(path = "/sites", method = "get")]
    async fn list_sites(&self, auth: AdminToken) -> Result<Json<SitesListResponseDto>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;

        let saved = site_bundles::Entity::find()
            .order_by_asc(site_bundles::Column::Domain)
            .all(&self.db)
            .await
            .map_err(InternalServerError)?;
        let routes = ingress_routes::Entity::find()
            .all(&self.db)
            .await
            .unwrap_or_default();

        let mut seen: HashSet<String> = HashSet::new();
        let mut sites: Vec<SiteBundleDto> = Vec::new();
        for m in saved {
            seen.insert(m.domain.to_lowercase());
            let decoded_len = STANDARD.decode(&m.bundle_base64).map(|b| b.len() as i64).unwrap_or(0);
            sites.push(SiteBundleDto {
                domain: m.domain,
                mode: m.mode,
                preset: m.preset,
                title: m.title,
                bundle_sha256: m.bundle_sha256,
                size_bytes: decoded_len,
                updated_at: Some(m.updated_at.and_utc().to_rfc3339()),
                configured: true,
            });
        }

        // Добавляем домены VLESS-шлюзов, для которых ещё не задан кастомный бандл
        for r in routes {
            if r.protocol != "vless" {
                continue;
            }
            let dom = r.sni.unwrap_or(r.entry_host).trim().to_lowercase();
            if !dom.is_empty() && seen.insert(dom.clone()) {
                sites.push(SiteBundleDto {
                    domain: dom,
                    mode: "preset".into(),
                    preset: "studio".into(),
                    title: "Этюды формы".into(),
                    bundle_sha256: String::new(),
                    size_bytes: 0,
                    updated_at: None,
                    configured: false,
                });
            }
        }
        sites.sort_by(|a, b| a.domain.cmp(&b.domain));

        Ok(Json(SitesListResponseDto {
            presets: built_in_presets(),
            sites,
        }))
    }

    /// Сохранить пресет, свой HTML или ZIP-бандл маскировки для домена (или `*` по умолчанию).
    #[oai(path = "/sites/:domain", method = "put")]
    async fn save_site(
        &self,
        auth: AdminToken,
        domain: Path<String>,
        req: Json<SaveSiteBundleRequest>,
    ) -> Result<Json<SiteBundleDto>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;

        let dom = domain.0.trim().to_lowercase();
        if dom.is_empty()
            || dom.len() > 253
            || (dom != "*"
                && !dom
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '-'))
        {
            return Err(Error::from_string("Некорректное доменное имя", StatusCode::BAD_REQUEST));
        }

        let mode = req.0.mode.trim().to_lowercase();
        let preset = req
            .0
            .preset
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .unwrap_or("studio")
            .to_lowercase();
        let title = req.0.title.unwrap_or_default().trim().to_string();

        let (raw_bytes, stored_mode): (Vec<u8>, String) = match mode.as_str() {
            "preset" => {
                let valid = ["studio", "journal", "cloud", "portfolio", "status"];
                if !valid.contains(&preset.as_str()) {
                    return Err(Error::from_string("Неизвестный пресет сайта", StatusCode::BAD_REQUEST));
                }
                let html = render_preset_html(&preset, &title);
                (html.into_bytes(), "preset".into())
            }
            "custom_html" => {
                let html = req
                    .0
                    .html
                    .as_deref()
                    .ok_or_else(|| Error::from_string("Передайте HTML-содержимое", StatusCode::BAD_REQUEST))?;
                if html.trim().is_empty() {
                    return Err(Error::from_string("HTML не может быть пустым", StatusCode::BAD_REQUEST));
                }
                if html.len() > MAX_HTML_BYTES {
                    return Err(Error::from_string("HTML превышает лимит 1 МБ", StatusCode::BAD_REQUEST));
                }
                (html.as_bytes().to_vec(), "custom_html".into())
            }
            "custom_zip" => {
                let b64 = req
                    .0
                    .zip_base64
                    .as_deref()
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .ok_or_else(|| Error::from_string("Передайте ZIP-архив в base64", StatusCode::BAD_REQUEST))?;
                let bytes = STANDARD
                    .decode(b64)
                    .map_err(|_| Error::from_string("Некорректный base64 ZIP-архива", StatusCode::BAD_REQUEST))?;
                if bytes.len() < 22 || !bytes.starts_with(b"PK\x03\x04") {
                    return Err(Error::from_string("Файл не является корректным ZIP-архивом", StatusCode::BAD_REQUEST));
                }
                if bytes.len() > MAX_ZIP_BYTES {
                    return Err(Error::from_string("ZIP-бандл превышает лимит 4 МБ", StatusCode::BAD_REQUEST));
                }
                (bytes, "custom_zip".into())
            }
            _ => {
                return Err(Error::from_string(
                    "Режим должен быть preset, custom_html или custom_zip",
                    StatusCode::BAD_REQUEST,
                ));
            }
        };

        let digest = sha256_hex(&String::from_utf8_lossy(&raw_bytes));
        // Используем точный SHA-256 по сырым байтам
        let exact_sha = {
            use sha2::{Digest, Sha256};
            let mut h = Sha256::new();
            h.update(&raw_bytes);
            format!("{:x}", h.finalize())
        };
        let _ = digest;
        let encoded = STANDARD.encode(&raw_bytes);
        let now = Utc::now().naive_utc();
        let size_bytes = raw_bytes.len() as i64;

        site_bundles::Entity::insert(site_bundles::ActiveModel {
            domain: Set(dom.clone()),
            mode: Set(stored_mode.clone()),
            preset: Set(preset.clone()),
            title: Set(title.clone()),
            bundle_sha256: Set(exact_sha.clone()),
            bundle_base64: Set(encoded),
            updated_at: Set(now),
        })
        .on_conflict(
            OnConflict::column(site_bundles::Column::Domain)
                .update_columns([
                    site_bundles::Column::Mode,
                    site_bundles::Column::Preset,
                    site_bundles::Column::Title,
                    site_bundles::Column::BundleSha256,
                    site_bundles::Column::BundleBase64,
                    site_bundles::Column::UpdatedAt,
                ])
                .to_owned(),
        )
        .exec_without_returning(&self.db)
        .await
        .map_err(InternalServerError)?;

        Ok(Json(SiteBundleDto {
            domain: dom,
            mode: stored_mode,
            preset,
            title,
            bundle_sha256: exact_sha,
            size_bytes,
            updated_at: Some(now.and_utc().to_rfc3339()),
            configured: true,
        }))
    }

    /// Удалить кастомный сайт маскировки для домена (вернуть к дефолтному пресету).
    #[oai(path = "/sites/:domain", method = "delete")]
    async fn delete_site(&self, auth: AdminToken, domain: Path<String>) -> Result<()> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        let dom = domain.0.trim().to_lowercase();
        let _ = site_bundles::Entity::delete_by_id(dom)
            .exec(&self.db)
            .await
            .map_err(InternalServerError)?;
        Ok(())
    }

    /// Авторизованная выгрузка актуального HTML/ZIP бандла маскировки для входного шлюза.
    #[oai(path = "/ingress-sync/:route_id/site", method = "get")]
    async fn fetch_gateway_site(
        &self,
        route_id: Path<String>,
        #[oai(name = "X-Usage-Token")] usage_token: Header<Option<String>>,
        #[oai(name = "X-Node-Token")] node_token: Header<Option<String>>,
    ) -> Result<Json<GatewaySitePayloadDto>> {
        let route = ingress_routes::Entity::find_by_id(route_id.0)
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

        let target_domain = route.sni.unwrap_or(route.entry_host);
        let bundle = resolve_site_bundle_for_domain(&self.db, &target_domain)
            .await
            .ok_or_else(|| Error::from_status(StatusCode::NOT_FOUND))?;

        Ok(Json(GatewaySitePayloadDto {
            domain: bundle.domain,
            mode: bundle.mode,
            sha256: bundle.bundle_sha256,
            bundle_base64: bundle.bundle_base64,
        }))
    }
}
