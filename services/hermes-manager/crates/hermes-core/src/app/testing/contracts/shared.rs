//! Shared-agent store contracts, instantiated unchanged for memory and PostgreSQL.

use super::super::{Inspect, identity, join, settings};
use super::access::{admin, bootstrap, key};
use super::assignments::{bob_spec, create};
use super::catalog::published_catalog;
use crate::app::assignments::{AssignmentReceipt, CreateSharedAssignment};
use crate::app::controller::InstanceStore;
use crate::app::envelope::CommandEnvelope;
use crate::app::faults::NoFaults;
use crate::app::ports::{AccessReader, AssignmentTx, Store};
use crate::app::routing::{RouteStore, Routing};
use crate::app::shared::*;
use crate::app::shared_ports::SharedTx;
use crate::domain::endpoints::EndpointNames;
use crate::domain::instances::{InstanceStatus, Phase};
use crate::domain::routing::{Decision, Target};
use crate::domain::shared::{GrantPrincipal, NewSharedAgent, Principal};
use std::sync::Arc;
use uuid::Uuid;

fn envelope<S: Store + Clone>(store: &S) -> CommandEnvelope<S> {
    let mut settings = (*settings()).clone();
    settings.endpoint_names = Some(EndpointNames::new("agents.test", &[], &[]).unwrap());
    CommandEnvelope::new(store.clone(), Arc::new(settings), Arc::new(NoFaults))
}

fn shared(name: &str, label: &str) -> CreateSharedAssignment {
    CreateSharedAssignment {
        shared_agent: NewSharedAgent {
            name: name.into(),
            display_name: name.into(),
            label: label.into(),
            enabled: true,
        },
        spec: bob_spec(),
    }
}

async fn setup<S: Store + Inspect + Clone>(store: &S) -> AssignmentReceipt {
    bootstrap(store, "alice").await;
    published_catalog(store).await;
    envelope(store)
        .execute(
            &admin("alice"),
            Uuid::new_v4(),
            &key("shared"),
            &shared("engineering", "engineering"),
        )
        .await
        .unwrap()
}

pub async fn concurrent_shared_creation_commissions_one_endpoint_and_instance<
    S: Store + Inspect + InstanceStore + Clone,
>(
    store: S,
) {
    bootstrap(&store, "alice").await;
    published_catalog(&store).await;
    let env = envelope(&store);
    let command = shared("engineering", "engineering");
    // Own the temporary actor and key across both futures.
    let actor = admin("alice");
    let idempotency = key("same");
    let (a, b) = join(
        env.execute(&actor, Uuid::new_v4(), &idempotency, &command),
        env.execute(&actor, Uuid::new_v4(), &idempotency, &command),
    )
    .await;
    assert_eq!(a.as_ref().unwrap(), b.as_ref().unwrap());
    let receipt = a.unwrap();
    let mut tx = store.begin().await.unwrap();
    assert_eq!(
        tx.endpoint(receipt.assignment_id)
            .await
            .unwrap()
            .unwrap()
            .label,
        "engineering"
    );
    assert_eq!(tx.assignment_of(Uuid::nil()).await.unwrap(), None);
    drop(tx);
    let other = shared("support", "engineering");
    assert_eq!(
        env.execute(&actor, Uuid::new_v4(), &key("other"), &other)
            .await
            .unwrap_err()
            .code,
        "hostname_taken"
    );
    assert_eq!(
        env.execute(&actor, Uuid::new_v4(), &idempotency, &other)
            .await
            .unwrap_err()
            .code,
        "idempotency_conflict"
    );
    let counts = store
        .audit_actions()
        .await
        .into_iter()
        .filter(|a| a == "assignment.create")
        .count();
    assert_eq!(counts, 1);
    assert_eq!(
        store.instance_generations(receipt.instance_id).await,
        (1, 1)
    );
    let first = shared("first", "contended");
    let second = shared("second", "contended");
    let first_key = key("race-first");
    let second_key = key("race-second");
    let (a, b) = join(
        env.execute(&actor, Uuid::new_v4(), &first_key, &first),
        env.execute(&actor, Uuid::new_v4(), &second_key, &second),
    )
    .await;
    let results = [a, b];
    assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
    assert_eq!(
        results.iter().find_map(|r| r.as_ref().err()).unwrap().code,
        "hostname_taken"
    );
    assert_eq!(
        store.instance_ids().await.unwrap().len(),
        2,
        "no orphan instance from the loser"
    );
}

pub async fn shared_endpoint_rename_frees_labels_but_disabled_labels_remain_owned<
    S: Store + Inspect + Clone,
>(
    store: S,
) {
    let first = setup(&store).await;
    let env = envelope(&store);
    let update = UpdateEndpoint {
        assignment_id: first.assignment_id,
        expected_version: 1,
        changes: EndpointChanges {
            label: Some("engineers".into()),
            enabled: Some(false),
        },
    };
    env.execute(&admin("alice"), Uuid::new_v4(), &key("rename"), &update)
        .await
        .unwrap();
    assert_eq!(store.instance_generations(first.instance_id).await, (2, 1));
    let second = env
        .execute(
            &admin("alice"),
            Uuid::new_v4(),
            &key("reuse"),
            &shared("support", "engineering"),
        )
        .await
        .unwrap();
    let collision = UpdateEndpoint {
        assignment_id: second.assignment_id,
        expected_version: 1,
        changes: EndpointChanges {
            label: Some("engineers".into()),
            enabled: None,
        },
    };
    assert_eq!(
        env.execute(&admin("alice"), Uuid::new_v4(), &key("taken"), &collision)
            .await
            .unwrap_err()
            .code,
        "hostname_taken"
    );
    assert_eq!(
        env.execute(&admin("alice"), Uuid::new_v4(), &key("stale"), &update)
            .await
            .unwrap_err()
            .code,
        "stale_version"
    );
    let a = UpdateEndpoint {
        assignment_id: first.assignment_id,
        expected_version: 2,
        changes: EndpointChanges {
            label: Some("contended".into()),
            enabled: None,
        },
    };
    let b = UpdateEndpoint {
        assignment_id: second.assignment_id,
        expected_version: 1,
        changes: a.changes.clone(),
    };
    let actor = admin("alice");
    let a_key = key("rename-race-a");
    let b_key = key("rename-race-b");
    let (a, b) = join(
        env.execute(&actor, Uuid::new_v4(), &a_key, &a),
        env.execute(&actor, Uuid::new_v4(), &b_key, &b),
    )
    .await;
    let results = [a, b];
    assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
    assert_eq!(
        results.iter().find_map(|r| r.as_ref().err()).unwrap().code,
        "hostname_taken"
    );
}

pub async fn shared_access_commands_leave_the_assignment_and_workload_unchanged<
    S: Store + Inspect + InstanceStore + Clone,
>(
    store: S,
) {
    let receipt = setup(&store).await;
    let env = envelope(&store);
    let cluster = super::super::fakes::FakeCluster::default();
    let controller = super::controller::controller(&store, &cluster, Arc::new(NoFaults));
    let epoch = store.advance_epoch().await.unwrap();
    super::controller::until(&controller, epoch, receipt.instance_id, Phase::Ready).await;
    let materials = store.materials(receipt.instance_id).await.unwrap();
    let before = store
        .desired_instance(receipt.instance_id)
        .await
        .unwrap()
        .unwrap();
    let group = env
        .execute(
            &admin("alice"),
            Uuid::new_v4(),
            &key("group"),
            &CreateAccessGroup {
                name: "team".into(),
                display_name: "Team".into(),
            },
        )
        .await
        .unwrap();
    env.execute(
        &admin("alice"),
        Uuid::new_v4(),
        &key("member"),
        &AddGroupMember {
            group_id: group.group_id,
            expected_version: 1,
            identity: identity("bob"),
        },
    )
    .await
    .unwrap();
    env.execute(
        &admin("alice"),
        Uuid::new_v4(),
        &key("grant"),
        &ReplaceAgentGrants {
            assignment_id: receipt.assignment_id,
            expected_version: 1,
            grants: vec![
                GrantPrincipal::Identity {
                    identity: identity("before-login"),
                },
                GrantPrincipal::Group {
                    group_id: group.group_id,
                },
            ],
        },
    )
    .await
    .unwrap();
    env.execute(
        &admin("alice"),
        Uuid::new_v4(),
        &key("disable"),
        &UpdateEndpoint {
            assignment_id: receipt.assignment_id,
            expected_version: 1,
            changes: EndpointChanges {
                label: None,
                enabled: Some(false),
            },
        },
    )
    .await
    .unwrap();
    assert_eq!(
        store
            .desired_instance(receipt.instance_id)
            .await
            .unwrap()
            .unwrap(),
        before
    );
    assert_eq!(
        store.instance_generations(receipt.instance_id).await,
        (1, 1)
    );
    super::controller::until(&controller, epoch, receipt.instance_id, Phase::Ready).await;
    assert_eq!(
        store.materials(receipt.instance_id).await.unwrap(),
        materials
    );
    let mut tx = store.begin().await.unwrap();
    assert_eq!(
        tx.assignment(receipt.assignment_id)
            .await
            .unwrap()
            .unwrap()
            .version,
        1
    );
    let grants = tx.agent_grants(receipt.assignment_id).await.unwrap();
    assert_eq!(grants.grants_version, 2);
    assert!(grants.grants.contains(&Principal::Group(group.group_id)));
    drop(tx);
    assert_eq!(
        env.execute(
            &admin("alice"),
            Uuid::new_v4(),
            &key("delete"),
            &DeleteAccessGroup {
                group_id: group.group_id,
                expected_version: 2
            }
        )
        .await
        .unwrap_err()
        .code,
        "group_in_use"
    );
}

pub async fn shared_kind_boundaries_and_endpoint_configuration_are_enforced<
    S: Store + Inspect + Clone,
>(
    store: S,
) {
    let shared_receipt = setup(&store).await;
    let personal = create(&store, "bob", bob_spec()).await.unwrap();
    let env = envelope(&store);
    assert_eq!(
        env.execute(
            &admin("alice"),
            Uuid::new_v4(),
            &key("personal-endpoint"),
            &UpdateEndpoint {
                assignment_id: personal.assignment_id,
                expected_version: 1,
                changes: EndpointChanges {
                    label: None,
                    enabled: Some(true)
                }
            }
        )
        .await
        .unwrap_err()
        .code,
        "endpoint_on_personal_agent"
    );
    assert_eq!(
        env.execute(
            &admin("alice"),
            Uuid::new_v4(),
            &key("personal-grant"),
            &ReplaceAgentGrants {
                assignment_id: personal.assignment_id,
                expected_version: 1,
                grants: vec![]
            }
        )
        .await
        .unwrap_err()
        .code,
        "grant_on_personal_agent"
    );
    assert_eq!(
        super::access::envelope(&store)
            .execute(
                &admin("alice"),
                Uuid::new_v4(),
                &key("unconfigured"),
                &shared("support", "support")
            )
            .await
            .unwrap_err()
            .code,
        "endpoints_not_configured"
    );
    assert_eq!(
        env.execute(
            &admin("bob"),
            Uuid::new_v4(),
            &key("unauthorized"),
            &ReplaceAgentGrants {
                assignment_id: shared_receipt.assignment_id,
                expected_version: 1,
                grants: vec![]
            }
        )
        .await
        .unwrap_err()
        .code,
        "access_denied"
    );
}

pub async fn shared_routes_read_current_explicit_audiences_without_provisioning<
    S: Store + Inspect + AccessReader + InstanceStore + RouteStore + Clone,
>(
    store: S,
) {
    let receipt = setup(&store).await;
    let env = envelope(&store);
    let target = Target::Shared {
        label: "engineering".into(),
    };
    let routing = Routing::new(store.clone(), super::super::installation());
    let member = identity("before-login");
    let epoch = store.advance_epoch().await.unwrap();
    store
        .bind_namespace(receipt.instance_id, "shared-namespace")
        .await
        .unwrap();
    store
        .record_status(
            epoch,
            receipt.instance_id,
            1,
            &InstanceStatus {
                phase: Phase::Ready,
                blocked: None,
                conditions: vec![],
                converged: true,
            },
        )
        .await
        .unwrap();
    let untouched = store.instance_ids().await.unwrap();
    for subject in ["alice", "before-login", "unknown"] {
        assert_eq!(
            routing
                .resolve_target(&identity(subject), &target)
                .await
                .unwrap(),
            Decision::Denied
        );
    }
    let group = env
        .execute(
            &admin("alice"),
            Uuid::new_v4(),
            &key("group"),
            &CreateAccessGroup {
                name: "team".into(),
                display_name: "Team".into(),
            },
        )
        .await
        .unwrap();
    let added = env
        .execute(
            &admin("alice"),
            Uuid::new_v4(),
            &key("join"),
            &AddGroupMember {
                group_id: group.group_id,
                expected_version: 1,
                identity: member.clone(),
            },
        )
        .await
        .unwrap();
    assert_eq!(
        routing.resolve_target(&member, &target).await.unwrap(),
        Decision::Denied,
        "membership alone grants nothing"
    );
    env.execute(
        &admin("alice"),
        Uuid::new_v4(),
        &key("grant-group"),
        &ReplaceAgentGrants {
            assignment_id: receipt.assignment_id,
            expected_version: 1,
            grants: vec![GrantPrincipal::Group {
                group_id: group.group_id,
            }],
        },
    )
    .await
    .unwrap();
    store.record_login(&member).await.unwrap();
    let route = routing.resolve_target(&member, &target).await.unwrap();
    let Decision::Ready(route) = route else {
        panic!("explicit member must reach the ready shared instance")
    };
    assert_eq!(route.instance_id, receipt.instance_id);
    assert_eq!(
        route.destination.namespace,
        store.instance_namespace(receipt.instance_id).await
    );
    assert_eq!(
        routing.resolve(&member).await.unwrap(),
        Decision::Denied,
        "a shared grant creates no personal assignment"
    );
    let mut tx = store.begin().await.unwrap();
    let member_id = crate::app::ports::AccessTx::identity(&mut tx, &member)
        .await
        .unwrap()
        .unwrap()
        .id;
    drop(tx);
    let revoke = RemoveGroupMember {
        group_id: group.group_id,
        expected_version: added.version,
        identity_id: member_id,
    };
    let actor = admin("alice");
    let revoke_key = key("revoke-member");
    let (observed, removed) = join(
        routing.resolve_target(&member, &target),
        env.execute(&actor, Uuid::new_v4(), &revoke_key, &revoke),
    )
    .await;
    assert!(matches!(
        observed.unwrap(),
        Decision::Ready(_) | Decision::Denied
    ));
    removed.unwrap();
    for _ in 0..8 {
        assert_eq!(
            routing.resolve_target(&member, &target).await.unwrap(),
            Decision::Denied
        );
    }
    env.execute(
        &actor,
        Uuid::new_v4(),
        &key("direct"),
        &ReplaceAgentGrants {
            assignment_id: receipt.assignment_id,
            expected_version: 2,
            grants: vec![GrantPrincipal::Identity {
                identity: member.clone(),
            }],
        },
    )
    .await
    .unwrap();
    assert_eq!(
        routing.resolve_target(&member, &target).await.unwrap(),
        Decision::Ready(route.clone())
    );
    let clear = ReplaceAgentGrants {
        assignment_id: receipt.assignment_id,
        expected_version: 3,
        grants: vec![],
    };
    let clear_key = key("revoke-direct");
    let (observed, cleared) = join(
        routing.resolve_target(&member, &target),
        env.execute(&actor, Uuid::new_v4(), &clear_key, &clear),
    )
    .await;
    assert!(matches!(
        observed.unwrap(),
        Decision::Ready(_) | Decision::Denied
    ));
    cleared.unwrap();
    for _ in 0..8 {
        assert_eq!(
            routing.resolve_target(&member, &target).await.unwrap(),
            Decision::Denied
        );
    }
    assert_eq!(store.instance_ids().await.unwrap(), untouched);
}
