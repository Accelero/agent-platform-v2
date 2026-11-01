//! Administration of shared endpoints, access groups and explicit grants.

use super::auth::Authenticated;
use super::support::{
    Body, NOT_FOUND, idempotency_key, if_match, page, path_uuid, query, request_id, tagged,
    with_status,
};
use super::{AdminState, AdminStore};
use crate::error::ApiError;
use axum::extract::{Path, RawQuery, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Extension, Json, Router};
use hermes_core::app::queries::search;
use hermes_core::app::shared::*;
use hermes_core::domain::identity::Identity;
use hermes_core::domain::shared::{Endpoint, GrantPrincipal};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

pub fn routes<S: AdminStore>() -> Router<Arc<AdminState<S>>> {
    Router::new()
        .route("/api/v1/endpoint-settings", get(settings::<S>))
        .route(
            "/api/v1/assignments/{assignmentId}/endpoint",
            get(endpoint::<S>).patch(update_endpoint::<S>),
        )
        .route(
            "/api/v1/assignments/{assignmentId}/grants",
            get(grants::<S>).put(replace_grants::<S>),
        )
        .route(
            "/api/v1/access-groups",
            get(groups::<S>).post(create_group::<S>),
        )
        .route(
            "/api/v1/access-groups/{groupId}",
            get(group::<S>)
                .patch(update_group::<S>)
                .delete(delete_group::<S>),
        )
        .route(
            "/api/v1/access-groups/{groupId}/members",
            get(members::<S>).post(add_member::<S>),
        )
        .route(
            "/api/v1/access-groups/{groupId}/members/{identityId}",
            axum::routing::delete(remove_member::<S>),
        )
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct EndpointSettings<'a> {
    agent_domain: Option<&'a str>,
    reserved_labels: Vec<&'a str>,
}

async fn settings<S: AdminStore>(State(state): State<Arc<AdminState<S>>>) -> Response {
    let names = state.settings.endpoint_names.as_ref();
    Json(EndpointSettings {
        agent_domain: names.map(|n| n.domain()),
        reserved_labels: names
            .map(|n| n.reserved_labels().iter().map(String::as_str).collect())
            .unwrap_or_default(),
    })
    .into_response()
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct EndpointView {
    label: String,
    hostname: String,
    url: String,
    gateway_callback_url: String,
    native_callback_url: String,
    enabled: bool,
    version: i64,
}

fn endpoint_view<S: AdminStore>(
    state: &AdminState<S>,
    endpoint: Endpoint,
) -> Result<EndpointView, ApiError> {
    let names = state
        .settings
        .endpoint_names
        .as_ref()
        .ok_or(hermes_core::domain::shared::SharedRejected::EndpointsNotConfigured)
        .map_err(hermes_core::app::error::AppError::from)?;
    let hostname = format!("{}.{}", endpoint.label, names.domain());
    let authority = if state.settings.endpoint_port == 443 {
        hostname.clone()
    } else {
        format!("{hostname}:{}", state.settings.endpoint_port)
    };
    let url = format!("https://{authority}");
    Ok(EndpointView {
        label: endpoint.label,
        hostname,
        gateway_callback_url: format!("{url}/_platform/oidc/callback"),
        native_callback_url: format!("{url}/auth/callback"),
        url,
        enabled: endpoint.enabled,
        version: endpoint.version,
    })
}

async fn endpoint<S: AdminStore>(
    State(state): State<Arc<AdminState<S>>>,
    Path(id): Path<String>,
) -> Result<Response, ApiError> {
    let endpoint = state
        .store
        .assignment_endpoint(path_uuid(&id)?)
        .await?
        .ok_or(NOT_FOUND)?;
    let view = endpoint_view(&state, endpoint)?;
    Ok(tagged(view.version, &view))
}

async fn update_endpoint<S: AdminStore>(
    State(state): State<Arc<AdminState<S>>>,
    Extension(auth): Extension<Authenticated>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Body(changes): Body<EndpointChanges>,
) -> Result<Response, ApiError> {
    let receipt = state
        .envelope
        .execute(
            &auth.actor,
            request_id(),
            &idempotency_key(&headers)?,
            &UpdateEndpoint {
                assignment_id: path_uuid(&id)?,
                expected_version: if_match(&headers)?,
                changes,
            },
        )
        .await?;
    Ok(with_status(StatusCode::ACCEPTED, &receipt))
}

async fn grants<S: AdminStore>(
    State(state): State<Arc<AdminState<S>>>,
    Path(id): Path<String>,
) -> Result<Response, ApiError> {
    let view = state
        .store
        .assignment_grants(path_uuid(&id)?)
        .await?
        .ok_or(NOT_FOUND)?;
    Ok(tagged(view.grants_version, &view))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GrantsBody {
    grants: Vec<GrantPrincipal>,
}

async fn replace_grants<S: AdminStore>(
    State(state): State<Arc<AdminState<S>>>,
    Extension(auth): Extension<Authenticated>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Body(body): Body<GrantsBody>,
) -> Result<Response, ApiError> {
    let receipt = state
        .envelope
        .execute(
            &auth.actor,
            request_id(),
            &idempotency_key(&headers)?,
            &ReplaceAgentGrants {
                assignment_id: path_uuid(&id)?,
                expected_version: if_match(&headers)?,
                grants: body.grants,
            },
        )
        .await?;
    Ok(with_status(StatusCode::ACCEPTED, &receipt))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PageQuery {
    q: Option<String>,
    after: Option<String>,
    limit: Option<u16>,
}

async fn groups<S: AdminStore>(
    State(state): State<Arc<AdminState<S>>>,
    RawQuery(raw): RawQuery,
) -> Result<Response, ApiError> {
    let params: PageQuery = query(raw.as_deref())?;
    Ok(Json(
        state
            .store
            .access_groups(
                search(params.q)?.as_deref(),
                &page(params.after, params.limit)?,
            )
            .await?,
    )
    .into_response())
}

async fn group<S: AdminStore>(
    State(state): State<Arc<AdminState<S>>>,
    Path(id): Path<String>,
) -> Result<Response, ApiError> {
    let view = state
        .store
        .access_group_view(path_uuid(&id)?)
        .await?
        .ok_or(NOT_FOUND)?;
    Ok(tagged(view.version, &view))
}

async fn create_group<S: AdminStore>(
    State(state): State<Arc<AdminState<S>>>,
    Extension(auth): Extension<Authenticated>,
    headers: HeaderMap,
    Body(command): Body<CreateAccessGroup>,
) -> Result<Response, ApiError> {
    let receipt = state
        .envelope
        .execute(
            &auth.actor,
            request_id(),
            &idempotency_key(&headers)?,
            &command,
        )
        .await?;
    Ok(with_status(StatusCode::ACCEPTED, &receipt))
}

async fn update_group<S: AdminStore>(
    State(state): State<Arc<AdminState<S>>>,
    Extension(auth): Extension<Authenticated>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Body(changes): Body<GroupChanges>,
) -> Result<Response, ApiError> {
    let receipt = state
        .envelope
        .execute(
            &auth.actor,
            request_id(),
            &idempotency_key(&headers)?,
            &UpdateAccessGroup {
                group_id: path_uuid(&id)?,
                expected_version: if_match(&headers)?,
                changes,
            },
        )
        .await?;
    Ok(with_status(StatusCode::ACCEPTED, &receipt))
}

async fn delete_group<S: AdminStore>(
    State(state): State<Arc<AdminState<S>>>,
    Extension(auth): Extension<Authenticated>,
    Path(id): Path<String>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    let receipt = state
        .envelope
        .execute(
            &auth.actor,
            request_id(),
            &idempotency_key(&headers)?,
            &DeleteAccessGroup {
                group_id: path_uuid(&id)?,
                expected_version: if_match(&headers)?,
            },
        )
        .await?;
    Ok(with_status(StatusCode::ACCEPTED, &receipt))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MembersQuery {
    after: Option<String>,
    limit: Option<u16>,
}

async fn members<S: AdminStore>(
    State(state): State<Arc<AdminState<S>>>,
    Path(id): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, ApiError> {
    let params: MembersQuery = query(raw.as_deref())?;
    if let Some(after) = &params.after {
        uuid::Uuid::parse_str(after).map_err(|_| hermes_core::app::error::AppError::invalid())?;
    }
    let view = state
        .store
        .access_group_members(path_uuid(&id)?, &page(params.after, params.limit)?)
        .await?
        .ok_or(NOT_FOUND)?;
    Ok(Json(view).into_response())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MemberBody {
    identity: Identity,
}

async fn add_member<S: AdminStore>(
    State(state): State<Arc<AdminState<S>>>,
    Extension(auth): Extension<Authenticated>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Body(body): Body<MemberBody>,
) -> Result<Response, ApiError> {
    let receipt = state
        .envelope
        .execute(
            &auth.actor,
            request_id(),
            &idempotency_key(&headers)?,
            &AddGroupMember {
                group_id: path_uuid(&id)?,
                expected_version: if_match(&headers)?,
                identity: body.identity,
            },
        )
        .await?;
    Ok(with_status(StatusCode::ACCEPTED, &receipt))
}

async fn remove_member<S: AdminStore>(
    State(state): State<Arc<AdminState<S>>>,
    Extension(auth): Extension<Authenticated>,
    Path((id, identity)): Path<(String, String)>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    let receipt = state
        .envelope
        .execute(
            &auth.actor,
            request_id(),
            &idempotency_key(&headers)?,
            &RemoveGroupMember {
                group_id: path_uuid(&id)?,
                expected_version: if_match(&headers)?,
                identity_id: path_uuid(&identity)?,
            },
        )
        .await?;
    Ok(with_status(StatusCode::ACCEPTED, &receipt))
}
