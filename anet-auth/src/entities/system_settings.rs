use sea_orm::entity::prelude::*;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, DeriveEntityModel, Serialize, Deserialize)]
#[sea_orm(table_name = "system_settings")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub id: i32,
    pub operating_mode: String,
    pub multi_exit_enabled: bool,
    pub parallel_transports: bool,
    pub auto_failover: bool,
    pub metrics_enabled: bool,
    pub metrics_retention_days: i32,
    pub probe_interval_seconds: i32,
    pub sync_interval_seconds: i32,
    pub subscription_origin_override: String,
    pub updated_at: DateTime,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
