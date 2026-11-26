use sea_orm::entity::prelude::*;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, DeriveEntityModel, Serialize, Deserialize)]
#[sea_orm(table_name = "ingress_routes")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
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
    pub sync_token_hash: Option<String>,
    pub sync_mode: String,
    pub last_sync_at: Option<DateTime>,
    pub created_at: DateTime,
    pub last_latency_ms: Option<f64>,
    pub last_loss_pct: Option<f64>,
    pub uplink_up: Option<bool>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
