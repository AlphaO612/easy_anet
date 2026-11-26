use sea_orm_migration::prelude::*;

#[derive(DeriveMigrationName)]
pub struct Migration;

#[async_trait::async_trait]
impl MigrationTrait for Migration {
    async fn up(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        // 1. Add lightweight gateway telemetry columns to ingress_routes
        manager
            .alter_table(
                Table::alter()
                    .table(IngressRoutes::Table)
                    .add_column_if_not_exists(
                        ColumnDef::new(IngressRoutes::LastLatencyMs).double().null(),
                    )
                    .add_column_if_not_exists(
                        ColumnDef::new(IngressRoutes::LastLossPct).double().null(),
                    )
                    .add_column_if_not_exists(
                        ColumnDef::new(IngressRoutes::UplinkUp).boolean().null(),
                    )
                    .to_owned(),
            )
            .await?;

        // 2. Singleton table for Telegram alert configuration
        manager
            .create_table(
                Table::create()
                    .table(TelegramAlertConfigs::Table)
                    .if_not_exists()
                    .col(
                        ColumnDef::new(TelegramAlertConfigs::Id)
                            .integer()
                            .not_null()
                            .primary_key(),
                    )
                    .col(
                        ColumnDef::new(TelegramAlertConfigs::Enabled)
                            .boolean()
                            .not_null()
                            .default(false),
                    )
                    .col(
                        ColumnDef::new(TelegramAlertConfigs::EncryptedBotToken)
                            .text()
                            .null(),
                    )
                    .col(
                        ColumnDef::new(TelegramAlertConfigs::ChatId)
                            .string()
                            .not_null()
                            .default(""),
                    )
                    .col(
                        ColumnDef::new(TelegramAlertConfigs::CooldownMinutes)
                            .integer()
                            .not_null()
                            .default(15),
                    )
                    .col(
                        ColumnDef::new(TelegramAlertConfigs::UpdatedAt)
                            .date_time()
                            .not_null(),
                    )
                    .to_owned(),
            )
            .await?;

        // 3. Per-domain HTTPS camouflage site bundles (presets, custom HTML, custom ZIP)
        manager
            .create_table(
                Table::create()
                    .table(SiteBundles::Table)
                    .if_not_exists()
                    .col(
                        ColumnDef::new(SiteBundles::Domain)
                            .string()
                            .not_null()
                            .primary_key(),
                    )
                    .col(
                        ColumnDef::new(SiteBundles::Mode)
                            .string()
                            .not_null()
                            .default("preset"),
                    )
                    .col(
                        ColumnDef::new(SiteBundles::Preset)
                            .string()
                            .not_null()
                            .default("studio"),
                    )
                    .col(
                        ColumnDef::new(SiteBundles::Title)
                            .string()
                            .not_null()
                            .default(""),
                    )
                    .col(
                        ColumnDef::new(SiteBundles::BundleSha256)
                            .string()
                            .not_null(),
                    )
                    .col(
                        ColumnDef::new(SiteBundles::BundleBase64)
                            .text()
                            .not_null(),
                    )
                    .col(
                        ColumnDef::new(SiteBundles::UpdatedAt)
                            .date_time()
                            .not_null(),
                    )
                    .to_owned(),
            )
            .await?;

        Ok(())
    }

    async fn down(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        manager
            .drop_table(Table::drop().table(SiteBundles::Table).if_exists().to_owned())
            .await?;
        manager
            .drop_table(
                Table::drop()
                    .table(TelegramAlertConfigs::Table)
                    .if_exists()
                    .to_owned(),
            )
            .await?;
        manager
            .alter_table(
                Table::alter()
                    .table(IngressRoutes::Table)
                    .drop_column(IngressRoutes::LastLatencyMs)
                    .drop_column(IngressRoutes::LastLossPct)
                    .drop_column(IngressRoutes::UplinkUp)
                    .to_owned(),
            )
            .await?;
        Ok(())
    }
}

#[derive(DeriveIden)]
enum IngressRoutes {
    Table,
    LastLatencyMs,
    LastLossPct,
    UplinkUp,
}

#[derive(DeriveIden)]
enum TelegramAlertConfigs {
    Table,
    Id,
    Enabled,
    EncryptedBotToken,
    ChatId,
    CooldownMinutes,
    UpdatedAt,
}

#[derive(DeriveIden)]
enum SiteBundles {
    Table,
    Domain,
    Mode,
    Preset,
    Title,
    BundleSha256,
    BundleBase64,
    UpdatedAt,
}
