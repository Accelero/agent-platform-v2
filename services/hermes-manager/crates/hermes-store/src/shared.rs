//! Shared-agent storage. Key locks cover absent names; aggregate row locks
//! serialize endpoint, grant and membership commands with deletion.

use crate::{PgStore, PgTx, store_error};
use async_trait::async_trait;
use hermes_core::app::error::StoreError;
use hermes_core::app::queries::{Page, PageRequest};
use hermes_core::app::shared_ports::{SharedQueries, SharedTx};
use hermes_core::domain::identity::Identity;
use hermes_core::domain::shared::{
    AccessGroup, AgentGrants, Endpoint, GrantPrincipal, GrantsView, GroupMember, Principal,
};
use sqlx::{Postgres, Row, Transaction};
use uuid::Uuid;

async fn key_lock(
    tx: &mut Transaction<'static, Postgres>,
    scope: &str,
    name: &str,
) -> Result<(), StoreError> {
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(format!("shared:{scope}:{name}"))
        .execute(&mut **tx)
        .await
        .map_err(store_error)?;
    Ok(())
}

fn endpoint(row: &sqlx::postgres::PgRow) -> Endpoint {
    Endpoint {
        assignment_id: row.get("assignment_id"),
        label: row.get("label"),
        enabled: row.get("enabled"),
        version: row.get("version"),
    }
}
fn group(row: &sqlx::postgres::PgRow) -> AccessGroup {
    AccessGroup {
        id: row.get("id"),
        name: row.get("name"),
        display_name: row.get("display_name"),
        version: row.get("version"),
    }
}

async fn grants(
    tx: &mut Transaction<'static, Postgres>,
    assignment: Uuid,
) -> Result<AgentGrants, StoreError> {
    let version = sqlx::query_scalar(
        "SELECT grants_version FROM manager.assignments WHERE id = $1 AND kind = 'shared'",
    )
    .bind(assignment)
    .fetch_one(&mut **tx)
    .await
    .map_err(store_error)?;
    let rows = sqlx::query("SELECT identity_id, group_id FROM manager.agent_grants WHERE assignment_id = $1 ORDER BY identity_id, group_id")
        .bind(assignment).fetch_all(&mut **tx).await.map_err(store_error)?;
    let mut principals = Vec::new();
    for row in rows {
        principals.push(
            match (
                row.get::<Option<Uuid>, _>("identity_id"),
                row.get::<Option<Uuid>, _>("group_id"),
            ) {
                (Some(id), None) => Principal::Identity(id),
                (None, Some(id)) => Principal::Group(id),
                _ => return Err(StoreError::Unavailable),
            },
        );
    }
    principals.sort();
    Ok(AgentGrants {
        grants: principals,
        grants_version: version,
    })
}

#[async_trait]
impl SharedTx for PgTx {
    async fn shared_name_owner(&mut self, name: &str) -> Result<Option<Uuid>, StoreError> {
        key_lock(&mut self.tx, "agent-name", name).await?;
        sqlx::query_scalar("SELECT id FROM manager.assignments WHERE name = $1 AND kind = 'shared'")
            .bind(name)
            .fetch_optional(&mut *self.tx)
            .await
            .map_err(store_error)
    }
    async fn endpoint(&mut self, assignment: Uuid) -> Result<Option<Endpoint>, StoreError> {
        let row =
            sqlx::query("SELECT * FROM manager.endpoints WHERE assignment_id = $1 FOR UPDATE")
                .bind(assignment)
                .fetch_optional(&mut *self.tx)
                .await
                .map_err(store_error)?;
        Ok(row.as_ref().map(endpoint))
    }
    async fn label_owner(&mut self, label: &str) -> Result<Option<Uuid>, StoreError> {
        key_lock(&mut self.tx, "label", label).await?;
        sqlx::query_scalar("SELECT assignment_id FROM manager.endpoints WHERE label = $1")
            .bind(label)
            .fetch_optional(&mut *self.tx)
            .await
            .map_err(store_error)
    }
    async fn put_endpoint(&mut self, endpoint: &Endpoint) -> Result<(), StoreError> {
        sqlx::query("INSERT INTO manager.endpoints (assignment_id, label, enabled, version) VALUES ($1, $2, $3, $4) ON CONFLICT (assignment_id) DO UPDATE SET label = excluded.label, enabled = excluded.enabled, version = excluded.version")
            .bind(endpoint.assignment_id).bind(&endpoint.label).bind(endpoint.enabled).bind(endpoint.version).execute(&mut *self.tx).await.map_err(store_error)?;
        Ok(())
    }
    async fn agent_grants(&mut self, assignment: Uuid) -> Result<AgentGrants, StoreError> {
        grants(&mut self.tx, assignment).await
    }
    async fn put_agent_grants(
        &mut self,
        assignment: Uuid,
        grants: &[Principal],
        version: i64,
    ) -> Result<(), StoreError> {
        sqlx::query("DELETE FROM manager.agent_grants WHERE assignment_id = $1")
            .bind(assignment)
            .execute(&mut *self.tx)
            .await
            .map_err(store_error)?;
        for principal in grants {
            let (identity, group) = match principal {
                Principal::Identity(id) => (Some(*id), None),
                Principal::Group(id) => (None, Some(*id)),
            };
            sqlx::query("INSERT INTO manager.agent_grants (assignment_id, identity_id, group_id) VALUES ($1, $2, $3)")
                .bind(assignment).bind(identity).bind(group).execute(&mut *self.tx).await.map_err(store_error)?;
        }
        sqlx::query("UPDATE manager.assignments SET grants_version = $2 WHERE id = $1")
            .bind(assignment)
            .bind(version)
            .execute(&mut *self.tx)
            .await
            .map_err(store_error)?;
        Ok(())
    }
    async fn access_group(&mut self, id: Uuid) -> Result<Option<AccessGroup>, StoreError> {
        let row = sqlx::query("SELECT * FROM manager.access_groups WHERE id = $1 FOR UPDATE")
            .bind(id)
            .fetch_optional(&mut *self.tx)
            .await
            .map_err(store_error)?;
        Ok(row.as_ref().map(group))
    }
    async fn group_name_owner(&mut self, name: &str) -> Result<Option<Uuid>, StoreError> {
        key_lock(&mut self.tx, "group-name", name).await?;
        sqlx::query_scalar("SELECT id FROM manager.access_groups WHERE name = $1")
            .bind(name)
            .fetch_optional(&mut *self.tx)
            .await
            .map_err(store_error)
    }
    async fn put_access_group(&mut self, group: &AccessGroup) -> Result<(), StoreError> {
        sqlx::query("INSERT INTO manager.access_groups (id, name, display_name, version) VALUES ($1, $2, $3, $4) ON CONFLICT (id) DO UPDATE SET name = excluded.name, display_name = excluded.display_name, version = excluded.version")
            .bind(group.id).bind(&group.name).bind(&group.display_name).bind(group.version).execute(&mut *self.tx).await.map_err(store_error)?;
        Ok(())
    }
    async fn group_in_use(&mut self, id: Uuid) -> Result<bool, StoreError> {
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM manager.agent_grants WHERE group_id = $1)")
            .bind(id)
            .fetch_one(&mut *self.tx)
            .await
            .map_err(store_error)
    }
    async fn delete_access_group(&mut self, id: Uuid) -> Result<(), StoreError> {
        sqlx::query("DELETE FROM manager.access_groups WHERE id = $1")
            .bind(id)
            .execute(&mut *self.tx)
            .await
            .map_err(store_error)?;
        Ok(())
    }
    async fn put_group_member(
        &mut self,
        group: Uuid,
        identity: Uuid,
        present: bool,
    ) -> Result<(), StoreError> {
        let query = if present {
            "INSERT INTO manager.access_group_members (group_id, identity_id) VALUES ($1, $2) ON CONFLICT DO NOTHING"
        } else {
            "DELETE FROM manager.access_group_members WHERE group_id = $1 AND identity_id = $2"
        };
        sqlx::query(query)
            .bind(group)
            .bind(identity)
            .execute(&mut *self.tx)
            .await
            .map_err(store_error)?;
        Ok(())
    }
}

fn page<T>(mut items: Vec<T>, request: &PageRequest, cursor: impl Fn(&T) -> String) -> Page<T> {
    let next_cursor = if items.len() > usize::from(request.limit) {
        items.truncate(usize::from(request.limit));
        items.last().map(cursor)
    } else {
        None
    };
    Page { items, next_cursor }
}

#[async_trait]
impl SharedQueries for PgStore {
    async fn assignment_endpoint(&self, assignment: Uuid) -> Result<Option<Endpoint>, StoreError> {
        let row = sqlx::query("SELECT * FROM manager.endpoints WHERE assignment_id = $1")
            .bind(assignment)
            .fetch_optional(&self.pool)
            .await
            .map_err(store_error)?;
        Ok(row.as_ref().map(endpoint))
    }
    async fn assignment_grants(&self, assignment: Uuid) -> Result<Option<GrantsView>, StoreError> {
        let mut tx = self.pool.begin().await.map_err(store_error)?;
        sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            .execute(&mut *tx)
            .await
            .map_err(store_error)?;
        let exists: bool = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM manager.assignments WHERE id = $1 AND kind = 'shared')",
        )
        .bind(assignment)
        .fetch_one(&mut *tx)
        .await
        .map_err(store_error)?;
        let result = if exists {
            let current = grants(&mut tx, assignment).await?;
            let rows = sqlx::query("SELECT g.group_id, i.issuer, i.subject FROM manager.agent_grants g LEFT JOIN manager.identities i ON i.id = g.identity_id WHERE g.assignment_id = $1 ORDER BY g.identity_id, g.group_id")
                .bind(assignment).fetch_all(&mut *tx).await.map_err(store_error)?;
            Some(GrantsView {
                grants_version: current.grants_version,
                grants: rows
                    .iter()
                    .map(|r| match r.get::<Option<Uuid>, _>("group_id") {
                        Some(group_id) => GrantPrincipal::Group { group_id },
                        None => GrantPrincipal::Identity {
                            identity: Identity {
                                issuer: r.get("issuer"),
                                subject: r.get("subject"),
                            },
                        },
                    })
                    .collect(),
            })
        } else {
            None
        };
        tx.commit().await.map_err(store_error)?;
        Ok(result)
    }
    async fn access_groups(
        &self,
        q: Option<&str>,
        request: &PageRequest,
    ) -> Result<Page<AccessGroup>, StoreError> {
        let rows = sqlx::query("SELECT * FROM manager.access_groups WHERE ($1::text IS NULL OR strpos(lower(name), lower($1)) > 0 OR strpos(lower(display_name), lower($1)) > 0) AND ($2::text IS NULL OR name > $2) ORDER BY name LIMIT $3")
            .bind(q).bind(&request.after).bind(i64::from(request.limit) + 1).fetch_all(&self.pool).await.map_err(store_error)?;
        Ok(page(rows.iter().map(group).collect(), request, |g| {
            g.name.clone()
        }))
    }
    async fn access_group_view(&self, id: Uuid) -> Result<Option<AccessGroup>, StoreError> {
        let row = sqlx::query("SELECT * FROM manager.access_groups WHERE id = $1")
            .bind(id)
            .fetch_optional(&self.pool)
            .await
            .map_err(store_error)?;
        Ok(row.as_ref().map(group))
    }
    async fn access_group_members(
        &self,
        group: Uuid,
        request: &PageRequest,
    ) -> Result<Option<Page<GroupMember>>, StoreError> {
        let after = request
            .after
            .as_deref()
            .map(Uuid::parse_str)
            .transpose()
            .map_err(|_| StoreError::Unavailable)?;
        let mut tx = self.pool.begin().await.map_err(store_error)?;
        sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            .execute(&mut *tx)
            .await
            .map_err(store_error)?;
        let exists: bool =
            sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM manager.access_groups WHERE id = $1)")
                .bind(group)
                .fetch_one(&mut *tx)
                .await
                .map_err(store_error)?;
        if !exists {
            return Ok(None);
        }
        let rows = sqlx::query("SELECT i.id, i.issuer, i.subject FROM manager.access_group_members m JOIN manager.identities i ON i.id = m.identity_id WHERE m.group_id = $1 AND ($2::uuid IS NULL OR i.id > $2) ORDER BY i.id LIMIT $3")
            .bind(group).bind(after).bind(i64::from(request.limit) + 1).fetch_all(&mut *tx).await.map_err(store_error)?;
        tx.commit().await.map_err(store_error)?;
        Ok(Some(page(
            rows.iter()
                .map(|r| GroupMember {
                    identity_id: r.get("id"),
                    identity: Identity {
                        issuer: r.get("issuer"),
                        subject: r.get("subject"),
                    },
                })
                .collect(),
            request,
            |m| m.identity_id.to_string(),
        )))
    }
}
