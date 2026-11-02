//! Gateway certificate coverage for personal and shared hostnames.

use std::collections::BTreeSet;
use std::time::Duration;

use axum::{Router, routing::get};
use hermes_http::server::{bind, gateway_tls_config, serve_bounded_reloading};
use hermes_testkit::Secrets;
use tokio::process::Command;

#[tokio::test]
async fn gateway_certificate_requires_personal_and_wildcard_coverage() {
    let fixture = Secrets::new().await;
    let ordinary_cert = fixture.read("server.crt");
    let ordinary_key = fixture.read("server.key");
    assert!(gateway_tls_config(&ordinary_cert, &ordinary_key, "https://localhost", None).is_ok());
    assert!(gateway_tls_config(&ordinary_cert, &ordinary_key, "https://127.0.0.1", None).is_ok());
    assert!(
        gateway_tls_config(
            &ordinary_cert,
            &ordinary_key,
            "https://localhost",
            Some("agents.test")
        )
        .is_err()
    );

    let directory = tempfile::tempdir().unwrap();
    let output = tokio::time::timeout(
        Duration::from_secs(30),
        Command::new("openssl")
            .current_dir(directory.path())
            .args([
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                "key.pem",
                "-out",
                "cert.pem",
                "-days",
                "1",
                "-subj",
                "/CN=fixture",
                "-addext",
                "subjectAltName=DNS:home.agents.test,DNS:*.agents.test",
                "-addext",
                "basicConstraints=critical,CA:FALSE",
                "-addext",
                "extendedKeyUsage=serverAuth",
            ])
            .output(),
    )
    .await
    .unwrap()
    .unwrap();
    assert!(output.status.success());
    let cert = tokio::fs::read(directory.path().join("cert.pem"))
        .await
        .unwrap();
    let key = tokio::fs::read(directory.path().join("key.pem"))
        .await
        .unwrap();
    assert!(
        gateway_tls_config(
            &cert,
            &key,
            "https://home.agents.test:8443",
            Some("agents.test")
        )
        .is_ok()
    );
    assert!(gateway_tls_config(&cert, &key, "https://other.test", Some("agents.test")).is_err());
    assert!(
        gateway_tls_config(
            &cert,
            &ordinary_key,
            "https://home.agents.test",
            Some("agents.test")
        )
        .is_err()
    );
    let client_only = tokio::time::timeout(
        Duration::from_secs(30),
        Command::new("openssl")
            .current_dir(directory.path())
            .args([
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                "client.key",
                "-out",
                "client.crt",
                "-days",
                "1",
                "-subj",
                "/CN=fixture",
                "-addext",
                "subjectAltName=DNS:home.agents.test,DNS:*.agents.test",
                "-addext",
                "basicConstraints=critical,CA:FALSE",
                "-addext",
                "extendedKeyUsage=clientAuth",
            ])
            .output(),
    )
    .await
    .unwrap()
    .unwrap();
    assert!(client_only.status.success());
    let client_cert = tokio::fs::read(directory.path().join("client.crt"))
        .await
        .unwrap();
    let client_key = tokio::fs::read(directory.path().join("client.key"))
        .await
        .unwrap();
    assert!(
        gateway_tls_config(
            &client_cert,
            &client_key,
            "https://home.agents.test",
            Some("agents.test")
        )
        .is_err()
    );
}

#[tokio::test]
async fn replacement_certificate_serves_new_connections_without_closing_existing_ones() {
    let first = Secrets::new().await;
    let second = Secrets::new().await;
    let config = |secrets: &Secrets| {
        gateway_tls_config(
            &secrets.read("server.crt"),
            &secrets.read("server.key"),
            "https://localhost",
            None,
        )
        .unwrap()
    };
    let (sender, receiver) = tokio::sync::watch::channel(config(&first));
    let (listener, address) = bind("127.0.0.1:0").await.unwrap();
    let server = tokio::spawn(async move {
        serve_bounded_reloading(
            listener,
            receiver,
            BTreeSet::new(),
            Router::new().route("/", get(|| async { "ok" })),
            std::future::pending(),
            16,
        )
        .await
        .unwrap();
    });
    let client = |secrets: &Secrets| {
        reqwest::Client::builder()
            .no_proxy()
            .http1_only()
            .resolve("localhost", address)
            .tls_certs_only([reqwest::Certificate::from_pem(&secrets.read("ca.crt")).unwrap()])
            .build()
            .unwrap()
    };
    let old = client(&first);
    let url = format!("https://localhost:{}/", address.port());
    let get = |client: &reqwest::Client| client.get(&url).send();
    assert_eq!(get(&old).await.unwrap().text().await.unwrap(), "ok");
    sender.send(config(&second)).unwrap();
    let new = client(&second);
    assert_eq!(get(&new).await.unwrap().text().await.unwrap(), "ok");
    // The old client trusts only the retired certificate's CA. Its second
    // request can succeed only on the already-established connection.
    assert_eq!(get(&old).await.unwrap().text().await.unwrap(), "ok");
    server.abort();
}
