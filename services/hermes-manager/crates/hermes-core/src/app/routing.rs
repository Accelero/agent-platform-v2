//! Route resolution for the gateway (`router.openapi.yaml`). Every call reads
//! the registry; nothing here caches a decision.

use async_trait::async_trait;

use super::error::{AppError, StoreError};
use crate::domain::identity::{Identity, IdentityRejected};
use crate::domain::installation::Installation;
use crate::domain::routing::{
    Decision, RouteRow, SharedRouteRow, Target, UnavailableReason, decide, decide_shared,
};

#[async_trait]
pub trait RouteStore: Send + Sync {
    /// The registry row of the identity's assignment and instance; `None`
    /// when the identity or its assignment is unknown.
    async fn route_row(&self, identity: &Identity) -> Result<Option<RouteRow>, StoreError>;
    /// Endpoint, access, readiness, direct grants and group membership from one
    /// statement's snapshot. Reads never create identities or assignments.
    async fn shared_route_row(
        &self,
        identity: &Identity,
        label: &str,
    ) -> Result<Option<SharedRouteRow>, StoreError>;
}

pub struct Routing<S> {
    store: S,
    installation: Installation,
}

impl<S: RouteStore> Routing<S> {
    pub fn new(store: S, installation: Installation) -> Self {
        Self {
            store,
            installation,
        }
    }

    /// A malformed identity is an invalid request. An identity of another
    /// issuer has no assignment here and is denied like any unknown one.
    pub async fn resolve(&self, identity: &Identity) -> Result<Decision, AppError> {
        self.resolve_target(identity, &Target::Personal {}).await
    }

    pub async fn resolve_target(
        &self,
        identity: &Identity,
        target: &Target,
    ) -> Result<Decision, AppError> {
        match identity.validate(&self.installation.issuer) {
            Ok(()) => {}
            Err(IdentityRejected::ForeignIssuer) => return Ok(Decision::Denied),
            Err(IdentityRejected::Malformed) => return Err(AppError::invalid()),
        }
        target.validate().map_err(|_| AppError::invalid())?;
        let decision = match target {
            Target::Personal {} => self
                .store
                .route_row(identity)
                .await
                .map(|row| decide(&self.installation, row.as_ref())),
            Target::Shared { label } => self
                .store
                .shared_route_row(identity, label)
                .await
                .map(|row| decide_shared(&self.installation, row.as_ref())),
        };
        Ok(match decision {
            Ok(decision) => decision,
            Err(_) => Decision::Unavailable(UnavailableReason::DependencyUnavailable),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::testing::{identity, installation};

    struct Failing;

    #[async_trait]
    impl RouteStore for Failing {
        async fn route_row(&self, _: &Identity) -> Result<Option<RouteRow>, StoreError> {
            Err(StoreError::Unavailable)
        }
        async fn shared_route_row(
            &self,
            _: &Identity,
            _: &str,
        ) -> Result<Option<SharedRouteRow>, StoreError> {
            Err(StoreError::Unavailable)
        }
    }

    #[tokio::test]
    async fn a_store_failure_is_unavailable_never_a_route() {
        let routing = Routing::new(Failing, installation());
        assert_eq!(
            routing.resolve(&identity("bob")).await,
            Ok(Decision::Unavailable(
                UnavailableReason::DependencyUnavailable
            ))
        );
        assert_eq!(
            routing
                .resolve_target(
                    &identity("bob"),
                    &Target::Shared {
                        label: "engineering".into()
                    }
                )
                .await,
            Ok(Decision::Unavailable(
                UnavailableReason::DependencyUnavailable
            ))
        );
    }
}
