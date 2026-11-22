use sea_orm_migration::prelude::*;

#[derive(DeriveMigrationName)]
pub struct Migration;
#[async_trait::async_trait]
impl MigrationTrait for Migration {
    async fn up(&self, m: &SchemaManager) -> Result<(), DbErr> {
        m.create_table(
            Table::create()
                .table(Alias::new("flow_reports"))
                .col(
                    ColumnDef::new(Alias::new("revision"))
                        .big_integer()
                        .not_null(),
                )
                .col(ColumnDef::new(Alias::new("node_id")).uuid().not_null())
                .col(
                    ColumnDef::new(Alias::new("reported_at"))
                        .timestamp()
                        .not_null(),
                )
                .col(ColumnDef::new(Alias::new("result")).text().not_null())
                .col(ColumnDef::new(Alias::new("profile")).string().not_null())
                .primary_key(
                    Index::create()
                        .col(Alias::new("node_id"))
                        .col(Alias::new("profile")),
                )
                .to_owned(),
        )
        .await
    }
    async fn down(&self, m: &SchemaManager) -> Result<(), DbErr> {
        m.drop_table(Table::drop().table(Alias::new("flow_reports")).to_owned())
            .await
    }
}
