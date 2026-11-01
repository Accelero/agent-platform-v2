//! The resolver's registry read: one statement over the identity, its
//! assignment and its instance, without locks.

use async_trait::async_trait;
use hermes_core::app::error::StoreError;
use hermes_core::app::routing::RouteStore;
use hermes_core::domain::assignments::Access;
use hermes_core::domain::identity::Identity;
use hermes_core::domain::instances::Phase;
use hermes_core::domain::routing::{RouteRow, SharedRouteRow};
use sqlx::Row;

use crate::{PgStore, store_error};

#[async_trait]
impl RouteStore for PgStore {
    async fn route_row(&self, identity: &Identity) -> Result<Option<RouteRow>, StoreError> {
        let Some(row) = sqlx::query(
            "SELECT i.id, i.namespace, i.namespace_uid IS NOT NULL AS bound, a.access, i.phase, \
             i.desired_generation, i.observed_generation, a.version \
             FROM manager.identities d JOIN manager.assignments a ON a.identity_id = d.id \
             JOIN manager.instances i ON i.assignment_id = a.id \
             WHERE d.issuer = $1 AND d.subject = $2",
        )
        .bind(&identity.issuer)
        .bind(&identity.subject)
        .fetch_optional(&self.pool)
        .await
        .map_err(store_error)?
        else {
            return Ok(None);
        };
        Ok(Some(RouteRow {
            instance_id: row.get("id"),
            namespace: row.get("namespace"),
            namespace_bound: row.get("bound"),
            access: Access::parse(row.get("access")).ok_or(StoreError::Unavailable)?,
            phase: Phase::parse(row.get("phase")).ok_or(StoreError::Unavailable)?,
            desired_generation: row.get("desired_generation"),
            observed_generation: row.get("observed_generation"),
            assignment_version: row.get("version"),
        }))
    }
    async fn shared_route_row(
        &self,
        identity: &Identity,
        label: &str,
    ) -> Result<Option<SharedRouteRow>, StoreError> {
        // One statement: endpoint, direct/group authorization and readiness share
        // the same snapshot, including when a grant or membership is revoked.
        let row = sqlx::query(
            "SELECT i.id, i.namespace, i.namespace_uid IS NOT NULL AS bound, a.access, i.phase, \
             i.desired_generation, i.observed_generation, a.version, e.enabled, \
             EXISTS (SELECT 1 FROM manager.identities d WHERE d.issuer = $1 AND d.subject = $2 \
               AND EXISTS (SELECT 1 FROM manager.agent_grants g WHERE g.assignment_id = a.id \
                 AND (g.identity_id = d.id OR EXISTS (SELECT 1 FROM manager.access_group_members m \
                      WHERE m.group_id = g.group_id AND m.identity_id = d.id)))) AS granted \
             FROM manager.endpoints e JOIN manager.assignments a ON a.id = e.assignment_id AND a.kind = 'shared' \
             JOIN manager.instances i ON i.assignment_id = a.id WHERE e.label = $3"
        ).bind(&identity.issuer).bind(&identity.subject).bind(label).fetch_optional(&self.pool).await.map_err(store_error)?;
        let Some(row) = row else {
            return Ok(None);
        };
        Ok(Some(SharedRouteRow {
            endpoint_enabled: row.get("enabled"),
            granted: row.get("granted"),
            route: RouteRow {
                instance_id: row.get("id"),
                namespace: row.get("namespace"),
                namespace_bound: row.get("bound"),
                access: Access::parse(row.get("access")).ok_or(StoreError::Unavailable)?,
                phase: Phase::parse(row.get("phase")).ok_or(StoreError::Unavailable)?,
                desired_generation: row.get("desired_generation"),
                observed_generation: row.get("observed_generation"),
                assignment_version: row.get("version"),
            },
        }))
    }
}
