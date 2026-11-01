//! Shared endpoint and audience ports. Mutating reads lock the named aggregate
//! until commit. Label and name lookups serialize even when the key is absent.

use super::error::StoreError;
use super::queries::{Page, PageRequest};
use crate::domain::shared::{
    AccessGroup, AgentGrants, Endpoint, GrantsView, GroupMember, Principal,
};
use async_trait::async_trait;
use uuid::Uuid;

#[async_trait]
pub trait SharedTx: Send {
    async fn shared_name_owner(&mut self, name: &str) -> Result<Option<Uuid>, StoreError>;
    async fn endpoint(&mut self, assignment: Uuid) -> Result<Option<Endpoint>, StoreError>;
    async fn label_owner(&mut self, label: &str) -> Result<Option<Uuid>, StoreError>;
    async fn put_endpoint(&mut self, endpoint: &Endpoint) -> Result<(), StoreError>;
    async fn agent_grants(&mut self, assignment: Uuid) -> Result<AgentGrants, StoreError>;
    async fn put_agent_grants(
        &mut self,
        assignment: Uuid,
        grants: &[Principal],
        version: i64,
    ) -> Result<(), StoreError>;
    async fn access_group(&mut self, id: Uuid) -> Result<Option<AccessGroup>, StoreError>;
    async fn group_name_owner(&mut self, name: &str) -> Result<Option<Uuid>, StoreError>;
    async fn put_access_group(&mut self, group: &AccessGroup) -> Result<(), StoreError>;
    async fn group_in_use(&mut self, id: Uuid) -> Result<bool, StoreError>;
    async fn delete_access_group(&mut self, id: Uuid) -> Result<(), StoreError>;
    async fn put_group_member(
        &mut self,
        group: Uuid,
        identity: Uuid,
        present: bool,
    ) -> Result<(), StoreError>;
}

#[async_trait]
pub trait SharedQueries: Send + Sync {
    async fn assignment_endpoint(&self, assignment: Uuid) -> Result<Option<Endpoint>, StoreError>;
    async fn assignment_grants(&self, assignment: Uuid) -> Result<Option<GrantsView>, StoreError>;
    async fn access_groups(
        &self,
        q: Option<&str>,
        page: &PageRequest,
    ) -> Result<Page<AccessGroup>, StoreError>;
    async fn access_group_view(&self, group: Uuid) -> Result<Option<AccessGroup>, StoreError>;
    async fn access_group_members(
        &self,
        group: Uuid,
        page: &PageRequest,
    ) -> Result<Option<Page<GroupMember>>, StoreError>;
}
