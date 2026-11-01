//! Authority classification happens per request, before session lookup. TLS SNI
//! and forwarded headers never select an agent.

use axum::http::{HeaderMap, Uri, Version, header, uri::Authority};
use hermes_core::domain::endpoints::EndpointNames;
use hermes_identity::AuthError;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostClass {
    Personal,
    Agent { label: String },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Misdirected;

pub struct Hosts {
    personal: String,
    port: u16,
    agents: Option<EndpointNames>,
}

impl Hosts {
    pub fn new(origin: &str, agents: Option<EndpointNames>) -> Result<Self, AuthError> {
        let url = url::Url::parse(origin).map_err(|_| AuthError::Configuration)?;
        if url.scheme() != "https"
            || url.origin().ascii_serialization() != origin
            || url.host_str().is_none_or(|host| host.ends_with('.'))
        {
            return Err(AuthError::Configuration);
        }
        Ok(Self {
            personal: url.host_str().ok_or(AuthError::Configuration)?.to_owned(),
            port: url
                .port_or_known_default()
                .ok_or(AuthError::Configuration)?,
            agents,
        })
    }

    pub fn classify(
        &self,
        version: Version,
        uri: &Uri,
        headers: &HeaderMap,
    ) -> Result<HostClass, Misdirected> {
        let mut hosts = headers.get_all(header::HOST).iter();
        let host = hosts
            .next()
            .map(|value| value.to_str().map_err(|_| Misdirected))
            .transpose()?;
        if hosts.next().is_some() {
            return Err(Misdirected);
        }
        let authority = match (uri.authority(), host) {
            (Some(authority), host) => {
                if uri.scheme_str() != Some("https") {
                    return Err(Misdirected);
                }
                let target = authority_parts(authority.as_str())?;
                if let Some(host) = host {
                    if target != authority_parts(host)? {
                        return Err(Misdirected);
                    }
                } else if version != Version::HTTP_2 {
                    return Err(Misdirected);
                }
                target
            }
            (None, Some(host)) if version != Version::HTTP_2 => authority_parts(host)?,
            _ => return Err(Misdirected),
        };
        let (host, port) = authority;
        if port != self.port {
            return Err(Misdirected);
        }
        if host == self.personal {
            return Ok(HostClass::Personal);
        }
        if let Some(agents) = &self.agents
            && let Some(label) = host.strip_suffix(&format!(".{}", agents.domain()))
            && agents.validate_label(label).is_ok()
        {
            return Ok(HostClass::Agent {
                label: label.to_owned(),
            });
        }
        Err(Misdirected)
    }
}

fn authority_parts(raw: &str) -> Result<(String, u16), Misdirected> {
    if !raw.is_ascii() || raw.len() > 260 || raw.contains('@') || raw.ends_with(':') {
        return Err(Misdirected);
    }
    let authority: Authority = raw.parse().map_err(|_| Misdirected)?;
    let host = authority.host().to_ascii_lowercase();
    if host.is_empty() || host.ends_with('.') {
        return Err(Misdirected);
    }
    let port = match authority.port() {
        Some(port) => port.as_str().parse().map_err(|_| Misdirected)?,
        None => 443,
    };
    Ok((host, port))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn authorities_classify_identically_for_http1_and_http2() {
        let names = EndpointNames::new(
            "agents.test",
            &["ops".into()],
            &[
                "https://home.agents.test",
                "https://control.agents.test",
                "https://id.agents.test/realms/agents",
            ],
        )
        .unwrap();
        let policy = Hosts::new("https://home.agents.test", Some(names)).unwrap();
        for (authority, expected) in [
            ("home.agents.test", Ok(HostClass::Personal)),
            ("HOME.AGENTS.TEST:443", Ok(HostClass::Personal)),
            (
                "engineering.agents.test",
                Ok(HostClass::Agent {
                    label: "engineering".into(),
                }),
            ),
            (
                "ENG-1.AGENTS.TEST:443",
                Ok(HostClass::Agent {
                    label: "eng-1".into(),
                }),
            ),
            ("home.agents.test:444", Err(Misdirected)),
            ("home.agents.test.", Err(Misdirected)),
            ("user@home.agents.test", Err(Misdirected)),
            ("home.agents.test:", Err(Misdirected)),
            ("other.test", Err(Misdirected)),
            ("a.b.agents.test", Err(Misdirected)),
            ("1eng.agents.test", Err(Misdirected)),
            ("eng--1.agents.test", Err(Misdirected)),
            ("xn--eng.agents.test", Err(Misdirected)),
            ("ops.agents.test", Err(Misdirected)),
            ("admin.agents.test", Err(Misdirected)),
            ("control.agents.test", Err(Misdirected)),
            ("id.agents.test", Err(Misdirected)),
        ] {
            for version in [Version::HTTP_11, Version::HTTP_2] {
                let mut headers = HeaderMap::new();
                headers.insert(header::HOST, authority.parse().unwrap());
                let uri = if version == Version::HTTP_2 {
                    format!("https://{authority}/").parse::<Uri>()
                } else {
                    "/".parse::<Uri>()
                };
                if let Ok(uri) = uri {
                    assert_eq!(
                        policy.classify(version, &uri, &headers),
                        expected,
                        "{version:?} {authority}"
                    );
                } else {
                    assert_eq!(expected, Err(Misdirected));
                }
            }
        }
        let policy = Hosts::new("https://home.agents.test:8443", None).unwrap();
        let uri = "/".parse().unwrap();
        let mut headers = HeaderMap::new();
        headers.insert(header::HOST, "home.agents.test:8443".parse().unwrap());
        assert_eq!(
            policy.classify(Version::HTTP_11, &uri, &headers),
            Ok(HostClass::Personal)
        );
        headers.insert(header::HOST, "home.agents.test".parse().unwrap());
        assert_eq!(
            policy.classify(Version::HTTP_11, &uri, &headers),
            Err(Misdirected)
        );
    }

    #[test]
    fn duplicate_conflicting_and_missing_authorities_are_refused() {
        assert!(Hosts::new("https://hermes.test.", None).is_err());
        let policy = Hosts::new("https://hermes.test", None).unwrap();
        let uri = "/".parse().unwrap();
        let mut headers = HeaderMap::new();
        assert_eq!(
            policy.classify(Version::HTTP_11, &uri, &headers),
            Err(Misdirected)
        );
        headers.append(header::HOST, "hermes.test".parse().unwrap());
        headers.append(header::HOST, "hermes.test".parse().unwrap());
        assert_eq!(
            policy.classify(Version::HTTP_11, &uri, &headers),
            Err(Misdirected)
        );
        headers.remove(header::HOST);
        let h2 = "https://hermes.test/".parse().unwrap();
        assert_eq!(
            policy.classify(Version::HTTP_2, &h2, &headers),
            Ok(HostClass::Personal)
        );
        headers.insert(header::HOST, "foreign.test".parse().unwrap());
        for version in [Version::HTTP_11, Version::HTTP_2] {
            assert_eq!(policy.classify(version, &h2, &headers), Err(Misdirected));
        }
        headers.insert(header::HOST, "hermes.test".parse().unwrap());
        let absolute = "https://foreign.test/".parse().unwrap();
        assert_eq!(
            policy.classify(Version::HTTP_11, &absolute, &headers),
            Err(Misdirected)
        );
    }
}
