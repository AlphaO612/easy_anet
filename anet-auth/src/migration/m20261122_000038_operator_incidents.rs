use sea_orm_migration::prelude::*;

#[derive(DeriveMigrationName)]
pub struct Migration;
#[async_trait::async_trait]
impl MigrationTrait for Migration {
    async fn up(&self, m: &SchemaManager) -> Result<(), DbErr> {
        let mut table = Table::create();
        table.table(Alias::new("operator_incidents"));
        table.col(
            ColumnDef::new(Alias::new("id"))
                .uuid()
                .not_null()
                .primary_key(),
        );
        table.col(ColumnDef::new(Alias::new("operator_id")).uuid().not_null());
        table.col(
            ColumnDef::new(Alias::new("reported_at"))
                .timestamp()
                .not_null(),
        );
        for field in ["observer", "direction", "transport", "evidence"] {
            table.col(ColumnDef::new(Alias::new(field)).text().not_null());
        }
        m.create_table(table.to_owned()).await
    }
    async fn down(&self, m: &SchemaManager) -> Result<(), DbErr> {
        m.drop_table(
            Table::drop()
                .table(Alias::new("operator_incidents"))
                .to_owned(),
        )
        .await
    }
}
