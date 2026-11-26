use sea_orm::entity::prelude::*;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, DeriveEntityModel, Serialize, Deserialize)]
#[sea_orm(table_name = "user_device_traffic_sources")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub device_id: Uuid,
    #[sea_orm(primary_key, auto_increment = false)]
    pub source: String,
    pub upload_bytes: i64,
    pub download_bytes: i64,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {
    #[sea_orm(
        belongs_to = "super::user_devices::Entity",
        from = "Column::DeviceId",
        to = "super::user_devices::Column::Id",
        on_delete = "Cascade"
    )]
    Device,
}

impl Related<super::user_devices::Entity> for Entity {
    fn to() -> RelationDef {
        Relation::Device.def()
    }
}

impl ActiveModelBehavior for ActiveModel {}
