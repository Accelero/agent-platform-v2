//! Structural validation of a submitted catalog definition.
//!
//! The rules of a definition's shape (required and unknown settings, types,
//! ranges, patterns, names, the one-delivery choice of a secret reference) are
//! the contract's JSON Schemas under `contracts/hermes-manager/schemas/`,
//! embedded at build time and run here, so the manager is the only place that
//! checks them. Each finding is reported at the JSON pointer of the offending
//! value with the stable code the rest of the manager's validation uses.

use std::sync::LazyLock;

use hermes_core::domain::catalog::CatalogKind;
use hermes_core::domain::{Problem, pointer_segment};
use jsonschema::error::ValidationErrorKind;
use jsonschema::{ValidationError, Validator};
use serde_json::Value;

fn compile(text: &str) -> Validator {
    let schema: Value = serde_json::from_str(text).expect("embedded schema is JSON");
    jsonschema::options()
        .build(&schema)
        .expect("embedded schema compiles")
}

static TEMPLATE: LazyLock<Validator> = LazyLock::new(|| {
    compile(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../../contracts/hermes-manager/schemas/template-v2.json"
    )))
});

static BACKEND: LazyLock<Validator> = LazyLock::new(|| {
    compile(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../../contracts/hermes-manager/schemas/terminal-backend-v1.json"
    )))
});

/// Every structural problem of `definition`, in pointer order, or none.
pub fn structural_problems(kind: CatalogKind, definition: &Value) -> Vec<Problem> {
    let validator = match kind {
        CatalogKind::Template => &*TEMPLATE,
        CatalogKind::TerminalBackend => &*BACKEND,
    };
    let mut found: Vec<Problem> = validator
        .iter_errors(definition)
        .flat_map(|error| problems_of(&error))
        .collect();
    found.sort_by(|a, b| (&a.pointer, a.code).cmp(&(&b.pointer, b.code)));
    found.dedup();
    found
}

fn member(at: &str, name: &str) -> String {
    format!("{at}/{}", pointer_segment(name))
}

/// The problems of one schema error. Where the rest of the manager's
/// validation has the same rule, the code is the same.
fn problems_of(error: &ValidationError<'_>) -> Vec<Problem> {
    let at = error.instance_path().to_string();
    let schema_path = error.schema_path().to_string();
    let last = at.rsplit('/').next().unwrap_or_default();
    let one = |pointer: String, code: &'static str| vec![Problem { pointer, code }];
    // A bad key of a map is reported once, at the key.
    if let ValidationErrorKind::PropertyNames { error: inner } = error.kind() {
        let key = inner.instance().as_str().unwrap_or_default();
        return one(member(&at, key), "invalid_name");
    }
    // Every other value the schemas call a name is checked by the one `name` rule.
    if schema_path.starts_with("/$defs/name/") {
        return one(at, "invalid_name");
    }
    match error.kind() {
        ValidationErrorKind::Required { property } => one(
            member(&at, property.as_str().unwrap_or_default()),
            "missing_setting",
        ),
        ValidationErrorKind::AdditionalProperties { unexpected } => unexpected
            .iter()
            .map(|name| Problem {
                pointer: member(&at, name),
                code: "unknown_setting",
            })
            .collect(),
        ValidationErrorKind::OneOfNotValid { .. }
        | ValidationErrorKind::OneOfMultipleValid { .. } => one(at, "one_delivery_required"),
        ValidationErrorKind::Type { .. } => one(at, "invalid_type"),
        ValidationErrorKind::Minimum { .. }
        | ValidationErrorKind::Maximum { .. }
        | ValidationErrorKind::ExclusiveMinimum { .. }
        | ValidationErrorKind::ExclusiveMaximum { .. } => one(at, "out_of_range"),
        ValidationErrorKind::MaxLength { .. }
        | ValidationErrorKind::MaxItems { .. }
        | ValidationErrorKind::MaxProperties { .. } => one(at, "too_large"),
        ValidationErrorKind::MinLength { .. } => one(at, "too_short"),
        ValidationErrorKind::Constant { .. } => {
            let code = match last {
                "schemaVersion" => "unsupported_schema",
                "sshContract" => "unsupported_contract",
                _ => "unsupported_value",
            };
            one(at, code)
        }
        ValidationErrorKind::Pattern { .. } => {
            let code = match last {
                "image" => "invalid_image",
                "mountPath" | "path" => "invalid_path",
                _ => "invalid_format",
            };
            one(at, code)
        }
        _ => one(at, "invalid_format"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(name: &str) -> Value {
        let path = format!(
            "{}/../../../../contracts/hermes-manager/schemas/fixtures/{name}",
            env!("CARGO_MANIFEST_DIR")
        );
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
    }

    fn found(kind: CatalogKind, value: &Value) -> Vec<(String, &'static str)> {
        structural_problems(kind, value)
            .into_iter()
            .map(|problem| (problem.pointer, problem.code))
            .collect()
    }

    fn pairs(list: &[(&str, &'static str)]) -> Vec<(String, &'static str)> {
        list.iter().map(|(p, c)| ((*p).to_owned(), *c)).collect()
    }

    #[test]
    fn the_shared_fixtures_are_judged_at_their_place() {
        use CatalogKind::{Template, TerminalBackend};
        for (name, kind, expected) in [
            ("valid/template.json", Template, vec![]),
            ("valid/terminal-backend.json", TerminalBackend, vec![]),
            (
                "invalid/template-cpu-bounds.json",
                Template,
                vec![("/resources/cpuMillis", "out_of_range")],
            ),
            (
                "invalid/template-schema-version.json",
                Template,
                vec![("/schemaVersion", "unsupported_schema")],
            ),
            (
                "invalid/template-two-deliveries.json",
                Template,
                vec![("/secrets/0", "one_delivery_required")],
            ),
            (
                "invalid/template-unknown-field.json",
                Template,
                vec![("/storageClass", "unknown_setting")],
            ),
            (
                "invalid/template-unpinned-image.json",
                Template,
                vec![("/image", "invalid_image")],
            ),
            (
                "invalid/terminal-backend-contract.json",
                TerminalBackend,
                vec![("/sshContract", "unsupported_contract")],
            ),
            (
                "invalid/terminal-backend-missing-limits.json",
                TerminalBackend,
                vec![("/limits", "missing_setting")],
            ),
            (
                "invalid/terminal-backend-root.json",
                TerminalBackend,
                vec![("/uid", "out_of_range")],
            ),
        ] {
            assert_eq!(found(kind, &fixture(name)), pairs(&expected), "{name}");
        }
    }

    #[test]
    fn every_problem_is_reported_once_with_the_place_it_is_at() {
        let mut backend = fixture("valid/terminal-backend.json");
        backend["uid"] = "1000".into();
        backend["colour"] = "red".into();
        backend["image"] = "".into();
        backend["limits"]["idleSeconds"] = 5.into();
        backend["mounts"] = serde_json::json!({
            "Bad Name": {"mountPath": "relative"},
            "workspace": {"mountPath": "/"},
        });
        backend.as_object_mut().unwrap().remove("resources");
        assert_eq!(
            found(CatalogKind::TerminalBackend, &backend),
            pairs(&[
                ("/colour", "unknown_setting"),
                ("/image", "invalid_image"),
                ("/limits/idleSeconds", "out_of_range"),
                ("/mounts/Bad Name", "invalid_name"),
                ("/mounts/Bad Name/mountPath", "invalid_path"),
                ("/mounts/workspace/mountPath", "too_short"),
                ("/resources", "missing_setting"),
                ("/uid", "invalid_type"),
            ])
        );
    }

    #[test]
    fn secret_references_are_checked_by_the_same_rules() {
        let mut template = fixture("valid/template.json");
        template["secrets"] = serde_json::json!([
            {"name": "a", "env": "lower"},
            {"name": "b", "file": ""},
            {"name": "c"},
        ]);
        assert_eq!(
            found(CatalogKind::Template, &template),
            pairs(&[
                ("/secrets/0/env", "invalid_format"),
                ("/secrets/1/file", "invalid_name"),
                ("/secrets/2", "one_delivery_required"),
            ])
        );
    }
}
