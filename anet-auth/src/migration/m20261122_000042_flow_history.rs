use sea_orm_migration::prelude::*;

#[derive(DeriveMigrationName)]
pub struct Migration;
#[async_trait::async_trait]
impl MigrationTrait for Migration {
    async fn up(&self, m: &SchemaManager) -> Result<(), DbErr> {
        m.create_table(
            Table::create()
                .table(Alias::new("flow_history"))
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
                .col(
                    ColumnDef::new(Alias::new("id"))
                        .big_integer()
                        .auto_increment()
                        .primary_key(),
                )
                .to_owned(),
        )
        .await
    }
    async fn down(&self, m: &SchemaManager) -> Result<(), DbErr> {
        m.drop_table(Table::drop().table(Alias::new("flow_history")).to_owned())
            .await
    }
}
