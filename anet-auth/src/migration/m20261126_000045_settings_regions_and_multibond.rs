use sea_orm_migration::prelude::*;

#[derive(DeriveMigrationName)]
pub struct Migration;

#[async_trait::async_trait]
impl MigrationTrait for Migration {
    async fn up(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        // 1. Add region (country code) and tags to servers
        manager
            .alter_table(
                Table::alter()
                    .table(Servers::Table)
                    .add_column_if_not_exists(
                        ColumnDef::new(Servers::Region)
                            .string()
                            .not_null()
                            .default(""),
                    )
                    .add_column_if_not_exists(
                        ColumnDef::new(Servers::Tags)
                            .string()
                            .not_null()
                            .default(""),
                    )
                    .to_owned(),
            )
            .await?;

        // 2. Add proxy_url and api_base_url to telegram_alert_configs
        manager
            .alter_table(
                Table::alter()
                    .table(TelegramAlertConfigs::Table)
                    .add_column_if_not_exists(
                        ColumnDef::new(TelegramAlertConfigs::ProxyUrl)
                            .string()
                            .not_null()
                            .default(""),
                    )
                    .add_column_if_not_exists(
                        ColumnDef::new(TelegramAlertConfigs::ApiBaseUrl)
                            .string()
                            .not_null()
                            .default(""),
                    )
                    .to_owned(),
            )
            .await?;

        // 3. System settings singleton table (operating mode, multi-exit acceleration, metrics retention)
        manager
            .create_table(
                Table::create()
                    .table(SystemSettings::Table)
                    .if_not_exists()
                    .col(
                        ColumnDef::new(SystemSettings::Id)
                            .integer()
                            .not_null()
                            .primary_key(),
                    )
                    .col(
                        ColumnDef::new(SystemSettings::OperatingMode)
                            .string()
                            .not_null()
                            .default("balanced"),
                    )
                    .col(
                        ColumnDef::new(SystemSettings::MultiExitEnabled)
                            .boolean()
                            .not_null()
                            .default(true),
                    )
                    .col(
                        ColumnDef::new(SystemSettings::ParallelTransports)
                            .boolean()
                            .not_null()
                            .default(true),
                    )
                    .col(
                        ColumnDef::new(SystemSettings::AutoFailover)
                            .boolean()
                            .not_null()
                            .default(true),
                    )
                    .col(
                        ColumnDef::new(SystemSettings::MetricsEnabled)
                            .boolean()
                            .not_null()
                            .default(true),
                    )
                    .col(
                        ColumnDef::new(SystemSettings::MetricsRetentionDays)
                            .integer()
                            .not_null()
                            .default(14),
                    )
                    .col(
                        ColumnDef::new(SystemSettings::ProbeIntervalSeconds)
                            .integer()
                            .not_null()
                            .default(15),
                    )
                    .col(
                        ColumnDef::new(SystemSettings::SyncIntervalSeconds)
                            .integer()
                            .not_null()
                            .default(30),
                    )
                    .col(
                        ColumnDef::new(SystemSettings::SubscriptionOriginOverride)
                            .string()
                            .not_null()
                            .default(""),
                    )
                    .col(
                        ColumnDef::new(SystemSettings::UpdatedAt)
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
            .drop_table(Table::drop().table(SystemSettings::Table).if_exists().to_owned())
            .await?;
        manager
            .alter_table(
                Table::alter()
                    .table(TelegramAlertConfigs::Table)
                    .drop_column(TelegramAlertConfigs::ProxyUrl)
                    .drop_column(TelegramAlertConfigs::ApiBaseUrl)
                    .to_owned(),
            )
            .await?;
        manager
            .alter_table(
                Table::alter()
                    .table(Servers::Table)
                    .drop_column(Servers::Region)
                    .drop_column(Servers::Tags)
                    .to_owned(),
            )
            .await?;
        Ok(())
    }
}

#[derive(DeriveIden)]
enum Servers {
    Table,
    Region,
    Tags,
}

#[derive(DeriveIden)]
enum TelegramAlertConfigs {
    Table,
    ProxyUrl,
    ApiBaseUrl,
}

#[derive(DeriveIden)]
enum SystemSettings {
    Table,
    Id,
    OperatingMode,
    MultiExitEnabled,
    ParallelTransports,
    AutoFailover,
    MetricsEnabled,
    MetricsRetentionDays,
    ProbeIntervalSeconds,
    SyncIntervalSeconds,
    SubscriptionOriginOverride,
    UpdatedAt,
}
