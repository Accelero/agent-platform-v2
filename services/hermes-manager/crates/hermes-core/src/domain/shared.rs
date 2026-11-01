//! Shared-agent identities, endpoints and audiences. None confers platform administration.

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use super::identity::Identity;
use super::names::validate_name;
use super::{ErrorKind, Rejection};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AssignmentKind {
    Personal,
    Shared,
}

impl AssignmentKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Personal => "personal",
            Self::Shared => "shared",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SharedAgent {
    pub name: String,
    pub display_name: String,
}

impl SharedAgent {
    pub fn validate(&self) -> Result<(), SharedRejected> {
        validate_name(&self.name).map_err(|_| SharedRejected::Invalid)?;
        validate_display_name(&self.display_name)
    }
}

pub fn validate_display_name(value: &str) -> Result<(), SharedRejected> {
    if value.trim().is_empty() || value.len() > 128 || value.chars().any(char::is_control) {
        Err(SharedRejected::Invalid)
    } else {
        Ok(())
    }
}

/// Immutable assignment binding. A shared assignment cannot carry a personal identity.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AssignmentBinding {
    Personal { identity_id: Uuid },
    Shared(SharedAgent),
}

impl AssignmentBinding {
    pub fn kind(&self) -> AssignmentKind {
        match self {
            Self::Personal { .. } => AssignmentKind::Personal,
            Self::Shared(_) => AssignmentKind::Shared,
        }
    }
    pub fn identity_id(&self) -> Option<Uuid> {
        match self {
            Self::Personal { identity_id } => Some(*identity_id),
            Self::Shared(_) => None,
        }
    }
    pub fn shared_agent(&self) -> Option<&SharedAgent> {
        match self {
            Self::Shared(agent) => Some(agent),
            Self::Personal { .. } => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct NewSharedAgent {
    pub name: String,
    pub display_name: String,
    pub label: String,
    pub enabled: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Endpoint {
    pub assignment_id: Uuid,
    pub label: String,
    pub enabled: bool,
    pub version: i64,
}

/// The home binding has no audience or hostname data.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InstanceBinding {
    Personal(Identity),
    Shared { agent_id: Uuid },
}

/// Stored grant keys, with no labels or email participating in authorization.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum Principal {
    Identity(Uuid),
    Group(Uuid),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged, deny_unknown_fields)]
pub enum GrantPrincipal {
    Identity {
        identity: Identity,
    },
    Group {
        #[serde(rename = "groupId")]
        group_id: Uuid,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AgentGrants {
    pub grants: Vec<Principal>,
    pub grants_version: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GrantsView {
    pub grants: Vec<GrantPrincipal>,
    pub grants_version: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AccessGroup {
    pub id: Uuid,
    pub name: String,
    pub display_name: String,
    pub version: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GroupMember {
    pub identity_id: Uuid,
    pub identity: Identity,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SharedRejected {
    Invalid,
    NotFound,
    Stale,
    HostnameTaken,
    NameTaken,
    GroupInUse,
    EndpointsNotConfigured,
    EndpointOnPersonal,
    GrantOnPersonal,
    UnknownGroup,
}

impl Rejection for SharedRejected {
    fn kind(&self) -> ErrorKind {
        match self {
            Self::NotFound => ErrorKind::NotFound,
            Self::Stale => ErrorKind::Precondition,
            Self::HostnameTaken | Self::NameTaken | Self::GroupInUse => ErrorKind::Conflict,
            _ => ErrorKind::Invalid,
        }
    }
    fn code(&self) -> &'static str {
        match self {
            Self::Invalid => "invalid_request",
            Self::NotFound => "not_found",
            Self::Stale => "stale_version",
            Self::HostnameTaken => "hostname_taken",
            Self::NameTaken => "name_taken",
            Self::GroupInUse => "group_in_use",
            Self::EndpointsNotConfigured => "endpoints_not_configured",
            Self::EndpointOnPersonal => "endpoint_on_personal_agent",
            Self::GrantOnPersonal => "grant_on_personal_agent",
            Self::UnknownGroup => "unknown_group",
        }
    }
}
