//! Shared-agent hostname policy, independent of storage and HTTP transport.

use std::collections::BTreeSet;

use super::names::validate_name;
use super::{ErrorKind, Rejection};

const RESERVED: [&str; 9] = [
    "admin", "api", "auth", "keycloak", "login", "mail", "personal", "sso", "www",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LabelRejected {
    Invalid,
    Reserved,
}

impl Rejection for LabelRejected {
    fn kind(&self) -> ErrorKind {
        ErrorKind::Invalid
    }

    fn code(&self) -> &'static str {
        match self {
            Self::Invalid => "invalid_hostname_label",
            Self::Reserved => "reserved_hostname_label",
        }
    }
}

#[derive(Debug, Clone)]
pub struct EndpointNames {
    domain: String,
    reserved: BTreeSet<String>,
}

impl EndpointNames {
    /// `origins` are the personal origin, administration origin and issuer.
    /// Only a host immediately below the agent domain reserves its label.
    pub fn new(domain: &str, reserved: &[String], origins: &[&str]) -> Result<Self, LabelRejected> {
        let domain_valid = !domain.is_empty()
            && domain.len() <= 251
            && matches!(url::Host::parse(domain), Ok(url::Host::Domain(_)))
            && domain.split('.').all(|label| {
                !label.is_empty()
                    && label.len() <= 63
                    && label.starts_with(|c: char| c.is_ascii_lowercase() || c.is_ascii_digit())
                    && label.ends_with(|c: char| c.is_ascii_lowercase() || c.is_ascii_digit())
                    && label
                        .bytes()
                        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
            });
        if !domain_valid || reserved.iter().any(|label| !valid_label(label)) {
            return Err(LabelRejected::Invalid);
        }
        let mut names = Self {
            domain: domain.to_owned(),
            reserved: RESERVED
                .into_iter()
                .map(str::to_owned)
                .chain(reserved.iter().cloned())
                .collect(),
        };
        let suffix = format!(".{domain}");
        for origin in origins {
            let url = url::Url::parse(origin).map_err(|_| LabelRejected::Invalid)?;
            let host = url.host_str().ok_or(LabelRejected::Invalid)?;
            if let Some(label) = host.strip_suffix(&suffix)
                && !label.contains('.')
            {
                names.reserved.insert(label.to_owned());
            }
        }
        Ok(names)
    }

    pub fn origin(&self, label: &str, personal_origin: &str) -> Result<String, LabelRejected> {
        self.validate_label(label)?;
        let origin = url::Url::parse(personal_origin).map_err(|_| LabelRejected::Invalid)?;
        if origin.scheme() != "https" || origin.origin().ascii_serialization() != personal_origin {
            return Err(LabelRejected::Invalid);
        }
        let port = origin
            .port_or_known_default()
            .ok_or(LabelRejected::Invalid)?;
        let suffix = if port == 443 {
            String::new()
        } else {
            format!(":{port}")
        };
        Ok(format!("https://{label}.{}{suffix}", self.domain))
    }

    pub fn domain(&self) -> &str {
        &self.domain
    }

    pub fn reserved_labels(&self) -> &BTreeSet<String> {
        &self.reserved
    }

    pub fn validate_label(&self, label: &str) -> Result<(), LabelRejected> {
        if !valid_label(label) || label.len() + 1 + self.domain.len() > 253 {
            Err(LabelRejected::Invalid)
        } else if self.reserved.contains(label) {
            Err(LabelRejected::Reserved)
        } else {
            Ok(())
        }
    }
}

fn valid_label(label: &str) -> bool {
    validate_name(label).is_ok() && !label.contains("--")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hostname_labels_and_reserved_names_follow_endpoint_rules() {
        let names = EndpointNames::new(
            "example.test",
            &["ops".into()],
            &[
                "https://home.example.test",
                "https://control.example.test",
                "https://id.example.test/realms/agents",
            ],
        )
        .unwrap();
        for label in ["a", "engineering", "eng-1", &"a".repeat(63)] {
            assert_eq!(names.validate_label(label), Ok(()), "{label}");
        }
        for label in [
            "",
            "1eng",
            "Eng",
            "-eng",
            "eng-",
            "eng--a",
            "xn--test",
            "a.b",
            "a_b",
            "équipe",
            &"a".repeat(64),
        ] {
            assert_eq!(
                names.validate_label(label),
                Err(LabelRejected::Invalid),
                "{label}"
            );
        }
        for label in RESERVED.into_iter().chain(["ops", "home", "control", "id"]) {
            assert_eq!(
                names.validate_label(label),
                Err(LabelRejected::Reserved),
                "{label}"
            );
        }
        assert_eq!(
            names
                .origin("engineering", "https://home.example.test")
                .unwrap(),
            "https://engineering.example.test"
        );
        assert_eq!(
            names
                .origin("engineering", "https://home.example.test:8443")
                .unwrap(),
            "https://engineering.example.test:8443"
        );
        let nested = EndpointNames::new(
            "example.test",
            &[],
            &["https://deep.id.example.test/realms/agents"],
        )
        .unwrap();
        assert_eq!(nested.validate_label("deep"), Ok(()));
    }

    #[test]
    fn agent_domains_are_canonical_bounded_dns_names() {
        for domain in [
            "agents.example.test",
            "localhost",
            "agents.123.example",
            "xn--bcher-kva.test",
        ] {
            assert!(EndpointNames::new(domain, &[], &[]).is_ok(), "{domain}");
        }
        for domain in [
            "",
            "https://agents.test",
            "Agents.test",
            "agents.test.",
            "a..test",
            "*.test",
            "a.test:443",
            "127.0.0.1",
            "[::1]",
            "a_.test",
            "équipe.test",
            &"a".repeat(64),
        ] {
            assert!(EndpointNames::new(domain, &[], &[]).is_err(), "{domain}");
        }
        assert!(EndpointNames::new("agents.test", &["bad--label".into()], &[]).is_err());
        let domain = [
            "a".repeat(63),
            "b".repeat(63),
            "c".repeat(63),
            "d".repeat(59),
        ]
        .join(".");
        let names = EndpointNames::new(&domain, &[], &[]).unwrap();
        assert_eq!(names.validate_label("a"), Ok(()));
        assert_eq!(names.validate_label("aa"), Err(LabelRejected::Invalid));
    }
}
