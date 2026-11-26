use sea_orm::entity::prelude::*;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, DeriveEntityModel, Serialize, Deserialize)]
#[sea_orm(table_name = "site_bundles")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub domain: String,
    pub mode: String,
    pub preset: String,
    pub title: String,
    pub bundle_sha256: String,
    pub bundle_base64: String,
    pub updated_at: DateTime,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
