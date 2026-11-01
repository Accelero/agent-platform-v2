//! Endpoint and audience commands. Access edits never advance workload state.

use super::envelope::{Applied, Command, CommandContext};
use super::error::Failure;
use super::ports::Tx;
use super::shared_ports::SharedTx;
use crate::domain::access::Permission;
use crate::domain::names::validate_name;
use crate::domain::shared::{
    AccessGroup, AssignmentKind, GrantPrincipal, Principal, SharedRejected, validate_display_name,
};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use uuid::Uuid;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct EndpointReceipt {
    pub assignment_id: Uuid,
    pub version: i64,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct EndpointChanges {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub enabled: Option<bool>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateEndpoint {
    pub assignment_id: Uuid,
    pub expected_version: i64,
    pub changes: EndpointChanges,
}

#[async_trait]
impl Command for UpdateEndpoint {
    type Receipt = EndpointReceipt;
    const ACTION: &'static str = "endpoint.update";
    fn permission(&self) -> Permission {
        Permission::Administer
    }
    async fn apply<T: Tx>(
        &self,
        tx: &mut T,
        context: &CommandContext<'_>,
    ) -> Result<Applied<Self::Receipt>, Failure> {
        let assignment = tx
            .assignment(self.assignment_id)
            .await?
            .ok_or(SharedRejected::NotFound)?;
        if assignment.binding.kind() != AssignmentKind::Shared {
            return Err(SharedRejected::EndpointOnPersonal.into());
        }
        let mut endpoint = tx
            .endpoint(self.assignment_id)
            .await?
            .ok_or(SharedRejected::NotFound)?;
        if endpoint.version != self.expected_version {
            return Err(SharedRejected::Stale.into());
        }
        if self.changes.label.is_none() && self.changes.enabled.is_none() {
            return Err(SharedRejected::Invalid.into());
        }
        let mut renamed = false;
        if let Some(label) = &self.changes.label {
            context
                .settings
                .endpoint_names
                .as_ref()
                .ok_or(SharedRejected::EndpointsNotConfigured)?
                .validate_label(label)?;
            if tx
                .label_owner(label)
                .await?
                .is_some_and(|id| id != self.assignment_id)
            {
                return Err(SharedRejected::HostnameTaken.into());
            }
            renamed = endpoint.label != *label;
            endpoint.label = label.clone();
        }
        if let Some(enabled) = self.changes.enabled {
            endpoint.enabled = enabled;
        }
        endpoint.version += 1;
        tx.put_endpoint(&endpoint).await?;
        let receipt = EndpointReceipt {
            assignment_id: self.assignment_id,
            version: endpoint.version,
        };
        let applied = Applied::new(receipt, "endpoint", self.assignment_id, &endpoint);
        if renamed {
            tx.advance_instance(assignment.instance_id, false).await?;
            Ok(applied.notifying(assignment.instance_id))
        } else {
            Ok(applied)
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GrantsReceipt {
    pub assignment_id: Uuid,
    pub grants_version: i64,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ReplaceAgentGrants {
    pub assignment_id: Uuid,
    pub expected_version: i64,
    pub grants: Vec<GrantPrincipal>,
}

#[async_trait]
impl Command for ReplaceAgentGrants {
    type Receipt = GrantsReceipt;
    const ACTION: &'static str = "agent_grants.replace";
    fn permission(&self) -> Permission {
        Permission::Administer
    }
    async fn apply<T: Tx>(
        &self,
        tx: &mut T,
        context: &CommandContext<'_>,
    ) -> Result<Applied<Self::Receipt>, Failure> {
        let assignment = tx
            .assignment(self.assignment_id)
            .await?
            .ok_or(SharedRejected::NotFound)?;
        if assignment.binding.kind() != AssignmentKind::Shared {
            return Err(SharedRejected::GrantOnPersonal.into());
        }
        let current = tx.agent_grants(self.assignment_id).await?;
        if current.grants_version != self.expected_version {
            return Err(SharedRejected::Stale.into());
        }
        let mut next = BTreeSet::new();
        // Lock groups in canonical order, so overlapping audience changes cannot deadlock.
        let groups: BTreeSet<_> = self
            .grants
            .iter()
            .filter_map(|p| match p {
                GrantPrincipal::Group { group_id } => Some(*group_id),
                _ => None,
            })
            .collect();
        for id in groups {
            tx.access_group(id)
                .await?
                .ok_or(SharedRejected::UnknownGroup)?;
        }
        for principal in &self.grants {
            let principal = match principal {
                GrantPrincipal::Identity { identity } => {
                    identity.validate(&context.installation().issuer)?;
                    Principal::Identity(tx.ensure_identity(identity).await?.id)
                }
                GrantPrincipal::Group { group_id } => Principal::Group(*group_id),
            };
            if !next.insert(principal) {
                return Err(SharedRejected::Invalid.into());
            }
        }
        let before: BTreeSet<_> = current.grants.into_iter().collect();
        let added: Vec<_> = next.difference(&before).cloned().collect();
        let removed: Vec<_> = before.difference(&next).cloned().collect();
        let grants: Vec<_> = next.into_iter().collect();
        let version = current.grants_version + 1;
        tx.put_agent_grants(self.assignment_id, &grants, version)
            .await?;
        #[derive(Serialize)]
        struct Change {
            added: Vec<Principal>,
            removed: Vec<Principal>,
            version: i64,
        }
        Ok(Applied::new(
            GrantsReceipt {
                assignment_id: self.assignment_id,
                grants_version: version,
            },
            "agent_grants",
            self.assignment_id,
            &Change {
                added,
                removed,
                version,
            },
        ))
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GroupReceipt {
    pub group_id: Uuid,
    pub version: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CreateAccessGroup {
    pub name: String,
    pub display_name: String,
}

#[async_trait]
impl Command for CreateAccessGroup {
    type Receipt = GroupReceipt;
    const ACTION: &'static str = "access_group.create";
    fn permission(&self) -> Permission {
        Permission::Administer
    }
    async fn apply<T: Tx>(
        &self,
        tx: &mut T,
        _: &CommandContext<'_>,
    ) -> Result<Applied<Self::Receipt>, Failure> {
        validate_name(&self.name).map_err(|_| SharedRejected::Invalid)?;
        validate_display_name(&self.display_name)?;
        if tx.group_name_owner(&self.name).await?.is_some() {
            return Err(SharedRejected::NameTaken.into());
        }
        let group = AccessGroup {
            id: Uuid::new_v4(),
            name: self.name.clone(),
            display_name: self.display_name.clone(),
            version: 1,
        };
        tx.put_access_group(&group).await?;
        Ok(group_applied(&group))
    }
}

fn group_applied(group: &AccessGroup) -> Applied<GroupReceipt> {
    Applied::new(
        GroupReceipt {
            group_id: group.id,
            version: group.version,
        },
        "access_group",
        group.id,
        group,
    )
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct GroupChanges {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateAccessGroup {
    pub group_id: Uuid,
    pub expected_version: i64,
    pub changes: GroupChanges,
}

async fn current_group<T: SharedTx>(
    tx: &mut T,
    id: Uuid,
    version: i64,
) -> Result<AccessGroup, Failure> {
    let group = tx.access_group(id).await?.ok_or(SharedRejected::NotFound)?;
    if group.version != version {
        return Err(SharedRejected::Stale.into());
    }
    Ok(group)
}

#[async_trait]
impl Command for UpdateAccessGroup {
    type Receipt = GroupReceipt;
    const ACTION: &'static str = "access_group.update";
    fn permission(&self) -> Permission {
        Permission::Administer
    }
    async fn apply<T: Tx>(
        &self,
        tx: &mut T,
        _: &CommandContext<'_>,
    ) -> Result<Applied<Self::Receipt>, Failure> {
        let mut group = current_group(tx, self.group_id, self.expected_version).await?;
        if self.changes.name.is_none() && self.changes.display_name.is_none() {
            return Err(SharedRejected::Invalid.into());
        }
        if let Some(name) = &self.changes.name {
            validate_name(name).map_err(|_| SharedRejected::Invalid)?;
            if tx
                .group_name_owner(name)
                .await?
                .is_some_and(|id| id != self.group_id)
            {
                return Err(SharedRejected::NameTaken.into());
            }
            group.name = name.clone();
        }
        if let Some(display_name) = &self.changes.display_name {
            validate_display_name(display_name)?;
            group.display_name = display_name.clone();
        }
        group.version += 1;
        tx.put_access_group(&group).await?;
        Ok(group_applied(&group))
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DeleteAccessGroup {
    pub group_id: Uuid,
    pub expected_version: i64,
}

#[async_trait]
impl Command for DeleteAccessGroup {
    type Receipt = GroupReceipt;
    const ACTION: &'static str = "access_group.delete";
    fn permission(&self) -> Permission {
        Permission::Administer
    }
    async fn apply<T: Tx>(
        &self,
        tx: &mut T,
        _: &CommandContext<'_>,
    ) -> Result<Applied<Self::Receipt>, Failure> {
        let group = current_group(tx, self.group_id, self.expected_version).await?;
        if tx.group_in_use(self.group_id).await? {
            return Err(SharedRejected::GroupInUse.into());
        }
        tx.delete_access_group(self.group_id).await?;
        Ok(group_applied(&group))
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AddGroupMember {
    pub group_id: Uuid,
    pub expected_version: i64,
    pub identity: crate::domain::identity::Identity,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RemoveGroupMember {
    pub group_id: Uuid,
    pub expected_version: i64,
    pub identity_id: Uuid,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct MemberChange {
    version: i64,
    identity_id: Uuid,
}

#[async_trait]
impl Command for AddGroupMember {
    type Receipt = GroupReceipt;
    const ACTION: &'static str = "access_group.member_add";
    fn permission(&self) -> Permission {
        Permission::Administer
    }
    async fn apply<T: Tx>(
        &self,
        tx: &mut T,
        context: &CommandContext<'_>,
    ) -> Result<Applied<Self::Receipt>, Failure> {
        let mut group = current_group(tx, self.group_id, self.expected_version).await?;
        self.identity.validate(&context.installation().issuer)?;
        let identity = tx.ensure_identity(&self.identity).await?;
        tx.put_group_member(group.id, identity.id, true).await?;
        group.version += 1;
        tx.put_access_group(&group).await?;
        Ok(Applied::new(
            GroupReceipt {
                group_id: group.id,
                version: group.version,
            },
            "access_group",
            group.id,
            &MemberChange {
                version: group.version,
                identity_id: identity.id,
            },
        ))
    }
}

#[async_trait]
impl Command for RemoveGroupMember {
    type Receipt = GroupReceipt;
    const ACTION: &'static str = "access_group.member_remove";
    fn permission(&self) -> Permission {
        Permission::Administer
    }
    async fn apply<T: Tx>(
        &self,
        tx: &mut T,
        _: &CommandContext<'_>,
    ) -> Result<Applied<Self::Receipt>, Failure> {
        let mut group = current_group(tx, self.group_id, self.expected_version).await?;
        tx.put_group_member(group.id, self.identity_id, false)
            .await?;
        group.version += 1;
        tx.put_access_group(&group).await?;
        Ok(Applied::new(
            GroupReceipt {
                group_id: group.id,
                version: group.version,
            },
            "access_group",
            group.id,
            &MemberChange {
                version: group.version,
                identity_id: self.identity_id,
            },
        ))
    }
}
