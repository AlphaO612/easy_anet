use super::{api::validate_admin_session, dto::AdminToken};
use crate::entities::operator_incidents as reports;
use poem::{Error, Result, error::InternalServerError, http::StatusCode};
use poem_openapi::{Object, OpenApi, param::Path, payload::Json};
use sea_orm::{ActiveModelTrait, DatabaseConnection, EntityTrait, QueryOrder, QuerySelect, Set};
use uuid::Uuid;

#[derive(Object)]
struct DeleteIncidentResult {
    deleted: bool,
    id: Uuid,
}

pub struct IncidentsApi {
    pub db: DatabaseConnection,
}

#[derive(Object)]
struct Observation {
    observer: String,
    direction: String,
    transport: String,
    evidence: String,
}

impl Observation {
    fn valid(&self) -> bool {
        [
            (&self.observer, 256),
            (&self.direction, 512),
            (&self.transport, 128),
            (&self.evidence, 8192),
        ]
        .into_iter()
        .all(|(value, limit)| !value.trim().is_empty() && value.len() <= limit)
    }
}

#[derive(Object)]
struct Report {
    id: Uuid,
    operator_id: Uuid,
    reported_at: String,
    observation: Observation,
}

impl From<reports::Model> for Report {
    fn from(m: reports::Model) -> Self {
        Self {
            id: m.id,
            operator_id: m.operator_id,
            reported_at: m.reported_at.and_utc().to_rfc3339(),
            observation: Observation {
                observer: m.observer,
                direction: m.direction,
                transport: m.transport,
                evidence: m.evidence,
            },
        }
    }
}

#[OpenApi]
impl IncidentsApi {
    #[oai(path = "/operator-incidents", method = "get")]
    async fn recent_reports(&self, auth: AdminToken) -> Result<Json<Vec<Report>>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        let rows = reports::Entity::find()
            .order_by_desc(reports::Column::ReportedAt)
            .limit(200)
            .all(&self.db)
            .await
            .map_err(InternalServerError)?;
        Ok(Json(rows.into_iter().map(Report::from).collect()))
    }

    #[oai(path = "/operator-incidents", method = "post")]
    async fn file_report(
        &self,
        auth: AdminToken,
        input: Json<Observation>,
    ) -> Result<Json<Report>> {
        let operator = validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        if !input.valid() {
            return Err(Error::from_string(
                "Missing or oversized observation field",
                StatusCode::BAD_REQUEST,
            ));
        }
        let input = input.0;
        let row = reports::ActiveModel {
            id: Set(Uuid::new_v4()),
            operator_id: Set(operator),
            reported_at: Set(chrono::Utc::now().naive_utc()),
            observer: Set(input.observer),
            direction: Set(input.direction),
            transport: Set(input.transport),
            evidence: Set(input.evidence),
        }
        .insert(&self.db)
        .await
        .map_err(InternalServerError)?;
        Ok(Json(row.into()))
    }

    #[oai(path = "/operator-incidents/:id", method = "delete")]
    async fn delete_report(
        &self,
        auth: AdminToken,
        id: Path<Uuid>,
    ) -> Result<Json<DeleteIncidentResult>> {
        validate_admin_session(&self.db, &auth.0.token)
            .await
            .map_err(|e| Error::from_string(e, StatusCode::UNAUTHORIZED))?;
        let deleted = reports::Entity::delete_by_id(id.0)
            .exec(&self.db)
            .await
            .map_err(InternalServerError)?;
        if deleted.rows_affected == 0 {
            return Err(Error::from_status(StatusCode::NOT_FOUND));
        }
        Ok(Json(DeleteIncidentResult {
            deleted: true,
            id: id.0,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn report_requires_observation_and_evidence() {
        let mut r = Observation {
            observer: "iOS".into(),
            direction: "entry to exit".into(),
            transport: "WG".into(),
            evidence: "timeout".into(),
        };
        assert!(r.valid());
        r.observer = " ".into();
        assert!(!r.valid());
        r.observer = "router".into();
        r.evidence = "a".repeat(8193);
        assert!(!r.valid());
    }
}
