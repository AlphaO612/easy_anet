use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "flow_history")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i64,
    pub node_id: Uuid,
    pub profile: String,
    pub revision: i64,
    pub reported_at: DateTime,
    pub result: String,
}
#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}
impl ActiveModelBehavior for ActiveModel {}
