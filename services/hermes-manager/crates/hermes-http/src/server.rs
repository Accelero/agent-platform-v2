//! Bounded TLS listeners. A peer's service identity comes only from a verified
//! client certificate, never from a forwarded header.

use std::collections::BTreeSet;
use std::future::Future;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use axum::Router;
use hermes_core::app::error::AppError;
use hyper_util::rt::{TokioExecutor, TokioIo, TokioTimer};
use hyper_util::server::conn::auto::Builder;
use hyper_util::service::TowerToHyperService;
use rustls::pki_types::{CertificateDer, PrivateKeyDer};
use rustls::server::WebPkiClientVerifier;
use rustls::{RootCertStore, ServerConfig};
use tokio::net::TcpListener;
use tokio::sync::Semaphore;
use tokio::task::JoinSet;
use tokio_rustls::TlsAcceptor;
use x509_parser::extensions::GeneralName;
use x509_parser::prelude::{FromDer, X509Certificate};

/// The verified URI identity of the client certificate, if it is one of the
/// listener's allowed peers. Handlers that need a service peer require it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PeerIdentity(pub Option<String>);

/// Concurrent connections of an API listener.
pub const CONNECTIONS: usize = 128;

/// TLS configuration from PEM files; `client_ca` enables mutual TLS.
pub fn tls_config(
    cert_pem: &[u8],
    key_pem: &[u8],
    client_ca: Option<&[u8]>,
) -> Result<Arc<ServerConfig>, AppError> {
    let certificates: Vec<CertificateDer<'static>> = rustls_pemfile::certs(&mut &cert_pem[..])
        .collect::<Result<_, _>>()
        .map_err(|_| AppError::invalid())?;
    let key: PrivateKeyDer<'static> = rustls_pemfile::private_key(&mut &key_pem[..])
        .map_err(|_| AppError::invalid())?
        .ok_or_else(AppError::invalid)?;
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let builder = ServerConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .map_err(|_| AppError::invalid())?;
    let builder = match client_ca {
        Some(ca) => {
            let mut roots = RootCertStore::empty();
            for certificate in rustls_pemfile::certs(&mut &ca[..]) {
                roots
                    .add(certificate.map_err(|_| AppError::invalid())?)
                    .map_err(|_| AppError::invalid())?;
            }
            let verifier = WebPkiClientVerifier::builder_with_provider(Arc::new(roots), provider)
                .build()
                .map_err(|_| AppError::invalid())?;
            builder.with_client_cert_verifier(verifier)
        }
        None => builder.with_no_client_auth(),
    };
    let mut config = builder
        .with_single_cert(certificates, key)
        .map_err(|_| AppError::invalid())?;
    config.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    Ok(Arc::new(config))
}

/// Gateway certificates must cover the personal host and, when shared agents
/// are enabled, the entire one-label agent namespace.
pub fn gateway_tls_config(
    cert_pem: &[u8],
    key_pem: &[u8],
    public_origin: &str,
    agent_domain: Option<&str>,
) -> Result<Arc<ServerConfig>, AppError> {
    let origin = url::Url::parse(public_origin).map_err(|_| AppError::invalid())?;
    let host = origin.host().ok_or_else(AppError::invalid)?;
    let cert = rustls_pemfile::certs(&mut &cert_pem[..])
        .next()
        .transpose()
        .map_err(|_| AppError::invalid())?
        .ok_or_else(AppError::invalid)?;
    let (_, parsed) = X509Certificate::from_der(cert.as_ref()).map_err(|_| AppError::invalid())?;
    if !parsed.validity().is_valid() {
        return Err(AppError::invalid());
    }
    if parsed
        .basic_constraints()
        .map_err(|_| AppError::invalid())?
        .is_some_and(|constraints| constraints.value.ca)
        || parsed
            .key_usage()
            .map_err(|_| AppError::invalid())?
            .is_some_and(|usage| !usage.value.digital_signature())
        || parsed
            .extended_key_usage()
            .map_err(|_| AppError::invalid())?
            .is_some_and(|usage| !usage.value.server_auth && !usage.value.any)
    {
        return Err(AppError::invalid());
    }
    let names = parsed
        .subject_alternative_name()
        .map_err(|_| AppError::invalid())?
        .ok_or_else(AppError::invalid)?;
    let dns: Vec<_> = names
        .value
        .general_names
        .iter()
        .filter_map(|name| match name {
            GeneralName::DNSName(name) => Some(*name),
            _ => None,
        })
        .collect();
    let public_covered = match host {
        url::Host::Domain(host) => dns.iter().any(|name| {
            let name = name.to_ascii_lowercase();
            name == host
                || name.strip_prefix("*.").is_some_and(|suffix| {
                    host.strip_suffix(&format!(".{suffix}"))
                        .is_some_and(|label| !label.is_empty() && !label.contains('.'))
                })
        }),
        url::Host::Ipv4(ip) => names
            .value
            .general_names
            .iter()
            .any(|name| matches!(name, GeneralName::IPAddress(bytes) if *bytes == ip.octets())),
        url::Host::Ipv6(ip) => names
            .value
            .general_names
            .iter()
            .any(|name| matches!(name, GeneralName::IPAddress(bytes) if *bytes == ip.octets())),
    };
    if !public_covered
        || agent_domain.is_some_and(|domain| {
            !dns.iter()
                .any(|name| name.eq_ignore_ascii_case(&format!("*.{domain}")))
        })
    {
        return Err(AppError::invalid());
    }
    tls_config(cert_pem, key_pem, None)
}

/// The certificate's single URI SAN among `allowed`, or none.
fn certificate_identity(
    certificate: &CertificateDer<'_>,
    allowed: &BTreeSet<String>,
) -> Option<String> {
    let (_, parsed) = X509Certificate::from_der(certificate.as_ref()).ok()?;
    let names = parsed.subject_alternative_name().ok()??;
    let matches: BTreeSet<_> = names
        .value
        .general_names
        .iter()
        .filter_map(|name| match name {
            GeneralName::URI(value) if allowed.contains(*value) => Some((*value).to_owned()),
            _ => None,
        })
        .collect();
    let mut matches = matches.into_iter();
    match (matches.next(), matches.next()) {
        (Some(identity), None) => Some(identity),
        _ => None,
    }
}

/// SIGTERM or Ctrl-C. Recovery never depends on this running.
pub async fn shutdown_signal() {
    let mut terminate =
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).ok();
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = async {
            match terminate.as_mut() {
                Some(signal) => { signal.recv().await; }
                None => std::future::pending::<()>().await,
            }
        } => {}
    }
}

pub async fn bind(address: &str) -> Result<(TcpListener, SocketAddr), AppError> {
    let listener = TcpListener::bind(address)
        .await
        .map_err(|_| AppError::unavailable())?;
    let bound = listener.local_addr().map_err(|_| AppError::unavailable())?;
    Ok((listener, bound))
}

/// Serves `router` until `shutdown` completes, with at most [`CONNECTIONS`]
/// connections.
pub async fn serve_until(
    listener: TcpListener,
    config: Arc<ServerConfig>,
    allowed_peers: BTreeSet<String>,
    router: Router,
    shutdown: impl Future<Output = ()> + Send,
) -> Result<(), AppError> {
    serve_bounded(
        listener,
        config,
        allowed_peers,
        router,
        shutdown,
        CONNECTIONS,
    )
    .await
}

/// Serves `router` until `shutdown` completes. Bounded connections, TLS
/// handshake deadline, header sizes and HTTP/2 streams.
pub async fn serve_bounded(
    listener: TcpListener,
    config: Arc<ServerConfig>,
    allowed_peers: BTreeSet<String>,
    router: Router,
    shutdown: impl Future<Output = ()> + Send,
    connections: usize,
) -> Result<(), AppError> {
    let (_sender, receiver) = tokio::sync::watch::channel(config);
    serve_bounded_reloading(
        listener,
        receiver,
        allowed_peers,
        router,
        shutdown,
        connections,
    )
    .await
}

/// New handshakes use the current TLS configuration; existing connections
/// retain the configuration with which they were accepted.
pub async fn serve_bounded_reloading(
    listener: TcpListener,
    configs: tokio::sync::watch::Receiver<Arc<ServerConfig>>,
    allowed_peers: BTreeSet<String>,
    router: Router,
    shutdown: impl Future<Output = ()> + Send,
    connections: usize,
) -> Result<(), AppError> {
    let permits = Arc::new(Semaphore::new(connections));
    let mut connections = JoinSet::new();
    tokio::pin!(shutdown);
    loop {
        tokio::select! {
            _ = &mut shutdown => break,
            Some(_) = connections.join_next(), if !connections.is_empty() => {}
            accepted = listener.accept() => {
                let (socket, _) = accepted.map_err(|_| AppError::unavailable())?;
                let Ok(permit) = permits.clone().try_acquire_owned() else {
                    drop(socket);
                    continue;
                };
                let (acceptor, router, allowed) = (TlsAcceptor::from(configs.borrow().clone()), router.clone(), allowed_peers.clone());
                connections.spawn(async move {
                    let _permit = permit;
                    let Ok(Ok(stream)) = tokio::time::timeout(Duration::from_secs(3), acceptor.accept(socket)).await else {
                        return;
                    };
                    let peer = stream
                        .get_ref()
                        .1
                        .peer_certificates()
                        .and_then(|chain| chain.first())
                        .and_then(|certificate| certificate_identity(certificate, &allowed));
                    let service = router.layer(axum::Extension(PeerIdentity(peer)));
                    let mut builder = Builder::new(TokioExecutor::new());
                    builder
                        .http1()
                        .max_buf_size(16 * 1024)
                        .header_read_timeout(Duration::from_secs(5))
                        .timer(TokioTimer::new());
                    builder.http2().max_header_list_size(16 * 1024).max_concurrent_streams(32);
                    let _ = builder
                        .serve_connection_with_upgrades(TokioIo::new(stream), TowerToHyperService::new(service))
                        .await;
                });
            }
        }
    }
    connections.abort_all();
    while connections.join_next().await.is_some() {}
    Ok(())
}
