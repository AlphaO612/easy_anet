use sea_orm_migration::prelude::*;

#[derive(DeriveMigrationName)]
pub struct Migration;

#[async_trait::async_trait]
impl MigrationTrait for Migration {
    async fn up(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        manager.alter_table(
            Table::alter()
                .table(Alias::new("servers"))
                .add_column(ColumnDef::new(Alias::new("enrollment_token_hash")).text().null())
                .add_column(ColumnDef::new(Alias::new("enrollment_expires_at")).timestamp().null())
                .to_owned(),
        ).await
    }

    async fn down(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        manager.alter_table(
            Table::alter()
                .table(Alias::new("servers"))
                .drop_column(Alias::new("enrollment_token_hash"))
                .drop_column(Alias::new("enrollment_expires_at"))
                .to_owned(),
        ).await
    }
}
