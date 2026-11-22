use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "topology_publications")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub revision: i64,
    pub operator_id: Uuid,
    pub published_at: DateTime,
    pub manifest: String,
}
#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}
impl ActiveModelBehavior for ActiveModel {}
