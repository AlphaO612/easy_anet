use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "uplink_keys")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub node_id: Uuid,
    #[sea_orm(primary_key, auto_increment = false)]
    pub profile: String,
    pub revision: i64,
    #[sea_orm(primary_key, auto_increment = false)]
    pub path: String,
    pub exit_id: Uuid,
    pub fingerprint: String,
}
#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}
impl ActiveModelBehavior for ActiveModel {}
