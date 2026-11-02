//! Platform sessions as the gateway uses them. [`BrowserAuth`] with the
//! gateway purpose implements the port; tests substitute fakes.

use async_trait::async_trait;
use hermes_core::domain::identity::Identity;
use hermes_identity::AuthError;
use hermes_identity::browser::BrowserAuth;

/// A valid session: its verified identity, an opaque handle that identifies
/// it without the cookie value, and its CSRF token.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SignedIn {
    pub identity: Identity,
    pub handle: String,
    pub csrf: String,
}

#[async_trait]
pub trait Sessions: Send + Sync {
    /// Authenticates a request from its two platform cookies.
    async fn authenticate(&self, session: &str, browser: &str) -> Result<SignedIn, AuthError>;
    /// Re-authenticates an established connection by its handle.
    async fn recheck(&self, handle: &str) -> Result<Identity, AuthError>;
    async fn logout(&self, session: &str, browser: &str, csrf: &str) -> Result<(), AuthError>;

    /// Implementations that have no host support must fail closed for agents.
    async fn authenticate_on_host(
        &self,
        session: &str,
        browser: &str,
        host: Option<&str>,
    ) -> Result<SignedIn, AuthError> {
        if host.is_some() {
            return Err(AuthError::Evidence);
        }
        self.authenticate(session, browser).await
    }
    async fn recheck_on_host(
        &self,
        handle: &str,
        host: Option<&str>,
    ) -> Result<Identity, AuthError> {
        if host.is_some() {
            return Err(AuthError::Evidence);
        }
        self.recheck(handle).await
    }
    async fn logout_on_host(
        &self,
        session: &str,
        browser: &str,
        csrf: &str,
        host: Option<&str>,
    ) -> Result<(), AuthError> {
        if host.is_some() {
            return Err(AuthError::Evidence);
        }
        self.logout(session, browser, csrf).await
    }
}

#[async_trait]
impl Sessions for BrowserAuth {
    async fn authenticate_on_host(
        &self,
        session: &str,
        browser: &str,
        host: Option<&str>,
    ) -> Result<SignedIn, AuthError> {
        let signed = BrowserAuth::authenticate_on_host(self, session, browser, host).await?;
        Ok(SignedIn {
            identity: signed.identity,
            handle: signed.session_handle,
            csrf: signed.csrf_token,
        })
    }

    async fn recheck_on_host(
        &self,
        handle: &str,
        host: Option<&str>,
    ) -> Result<Identity, AuthError> {
        Ok(self
            .authenticate_handle_on_host(handle, host)
            .await?
            .identity)
    }

    async fn logout_on_host(
        &self,
        session: &str,
        browser: &str,
        csrf: &str,
        host: Option<&str>,
    ) -> Result<(), AuthError> {
        BrowserAuth::logout_on_host(self, session, browser, csrf, host).await
    }
    async fn authenticate(&self, session: &str, browser: &str) -> Result<SignedIn, AuthError> {
        let signed = BrowserAuth::authenticate(self, session, browser).await?;
        Ok(SignedIn {
            identity: signed.identity,
            handle: signed.session_handle,
            csrf: signed.csrf_token,
        })
    }

    async fn recheck(&self, handle: &str) -> Result<Identity, AuthError> {
        Ok(self.authenticate_handle(handle).await?.identity)
    }

    async fn logout(&self, session: &str, browser: &str, csrf: &str) -> Result<(), AuthError> {
        BrowserAuth::logout(self, session, browser, csrf).await
    }
}
