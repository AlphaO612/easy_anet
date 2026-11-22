use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "flow_reports")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub node_id: Uuid,
    #[sea_orm(primary_key, auto_increment = false)]
    pub profile: String,
    pub revision: i64,
    pub reported_at: DateTime,
    pub result: String,
}
#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}
impl ActiveModelBehavior for ActiveModel {}
