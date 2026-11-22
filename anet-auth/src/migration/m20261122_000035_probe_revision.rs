use sea_orm_migration::prelude::*;

#[derive(DeriveMigrationName)]
pub struct Migration;

#[async_trait::async_trait]
impl MigrationTrait for Migration {
    async fn up(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        manager
            .alter_table(
                Table::alter()
                    .table(Alias::new("servers"))
                    .add_column(
                        ColumnDef::new(Alias::new("probe_revision"))
                            .big_integer()
                            .not_null()
                            .default(0),
                    )
                    .add_column(ColumnDef::new(Alias::new("probe_manifest")).text().null())
                    .add_column(
                        ColumnDef::new(Alias::new("probe_applied"))
                            .big_integer()
                            .not_null()
                            .default(0),
                    )
                    .add_column(ColumnDef::new(Alias::new("probe_error")).text().null())
                    .to_owned(),
            )
            .await
    }
    async fn down(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        manager
            .alter_table(
                Table::alter()
                    .table(Alias::new("servers"))
                    .drop_column(Alias::new("probe_revision"))
                    .drop_column(Alias::new("probe_manifest"))
                    .drop_column(Alias::new("probe_applied"))
                    .drop_column(Alias::new("probe_error"))
                    .to_owned(),
            )
            .await
    }
}
