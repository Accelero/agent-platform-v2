//! Admin settings (`adminConfig`): Hermes configuration enforced over the
//! user's own. Platform-enforced keys cannot be set by templates or
//! assignments, and settings never carry secret values.

use serde_json::{Map, Value};

use super::{Problem, Problems, pointer_segment};

/// Serialized size limit of one `adminConfig` document.
pub const MAX_ADMIN_CONFIG: usize = 8 * 1024;

/// The managed terminal provider the platform always enforces.
pub const MANAGED_BACKEND: &str = "managed_execution";

/// Deepest nesting of a template's `adminConfig`, counted from its root.
pub const MAX_ADMIN_CONFIG_DEPTH: usize = 16;

/// Most entries of a plugin list in `adminConfig`.
const MAX_PLUGIN_LIST: usize = 64;

/// Longest `terminal.runtime_id`, in characters.
const MAX_RUNTIME_ID: usize = 63;

/// Setting names that mark a credential or personal data, compared in lower
/// case with `_` and `-` removed.
const SENSITIVE_NAMES: [&str; 10] = [
    "apikey",
    "password",
    "token",
    "secret",
    "prompt",
    "identity",
    "memory",
    "profile",
    "credential",
    "authorization",
];

fn sensitive(name: &str) -> bool {
    let name: String = name
        .chars()
        .filter(|c| !matches!(c, '_' | '-'))
        .flat_map(char::to_lowercase)
        .collect();
    SENSITIVE_NAMES.iter().any(|word| name.contains(word))
}

/// `${NAME}`: the value the instance receives from an admin secret delivered
/// as that environment variable, never a secret itself.
fn is_env_reference(value: &Value) -> bool {
    value
        .as_str()
        .and_then(|text| text.strip_prefix("${")?.strip_suffix('}'))
        .is_some_and(|name| {
            name.starts_with(|c: char| c.is_ascii_uppercase() || c == '_')
                && name
                    .chars()
                    .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
        })
}

/// What a template's `adminConfig` may contain beyond the rules every layer
/// obeys: nothing named like a credential or personal data that is not a
/// reference to an admin secret, a bounded depth, and the reviewed limits of
/// the Hermes terminal and plugin settings
/// (`contracts/hermes-manager/schemas/hermes-managed-v1.json`).
pub fn validate_admin_config_content(
    config: &Map<String, Value>,
    at: &str,
    problems: &mut Problems,
) {
    fn walk(value: &Value, at: &str, depth: usize, problems: &mut Problems) {
        if depth > MAX_ADMIN_CONFIG_DEPTH {
            problems.add(at, "depth_limit");
            return;
        }
        let children: Vec<(String, &Value)> = match value {
            Value::Object(map) => map.iter().map(|(k, v)| (k.clone(), v)).collect(),
            Value::Array(items) => items
                .iter()
                .enumerate()
                .map(|(i, v)| (i.to_string(), v))
                .collect(),
            _ => return,
        };
        for (key, child) in children {
            let pointer = format!("{at}/{}", pointer_segment(&key));
            if value.is_object() && sensitive(&key) {
                problems.check(is_env_reference(child), pointer, "sensitive_field");
            } else {
                walk(child, &pointer, depth + 1, problems);
            }
        }
    }
    walk(&Value::Object(config.clone()), at, 0, problems);

    if let Some(runtime) = config
        .get("terminal")
        .and_then(Value::as_object)
        .and_then(|terminal| terminal.get("runtime_id"))
    {
        let pointer = format!("{at}/terminal/runtime_id");
        match runtime {
            Value::Null => {}
            Value::String(id) => {
                problems.check(id.chars().count() <= MAX_RUNTIME_ID, pointer, "too_large")
            }
            _ => problems.add(pointer, "invalid_type"),
        }
    }
    if let Some(plugins) = config.get("plugins").and_then(Value::as_object) {
        for list in ["enabled", "disabled"] {
            let Some(entries) = plugins.get(list) else {
                continue;
            };
            let pointer = format!("{at}/plugins/{list}");
            match entries.as_array() {
                // A disabled list that is not made of strings is platform-enforced.
                None if list == "disabled" => {}
                None => problems.add(pointer, "invalid_type"),
                Some(names) => {
                    problems.check(names.len() <= MAX_PLUGIN_LIST, &pointer, "too_large");
                    if list == "enabled" {
                        for (index, name) in names.iter().enumerate() {
                            problems.check(
                                name.is_string(),
                                format!("{pointer}/{index}"),
                                "invalid_type",
                            );
                        }
                    }
                }
            }
        }
    }
}

/// The top-level `adminConfig` keys without a reviewed schema. They are kept
/// as given and reported, never refused.
pub fn unvalidated_admin_keys(config: &Map<String, Value>, at: &str) -> Vec<Problem> {
    config
        .keys()
        .filter(|key| !matches!(key.as_str(), "terminal" | "plugins"))
        .map(|key| Problem {
            pointer: format!("{at}/{}", pointer_segment(key)),
            code: "unvalidated",
        })
        .collect()
}

/// Checks one admin settings layer. `at` points at the layer.
pub fn validate_admin_config(config: &Map<String, Value>, at: &str, problems: &mut Problems) {
    let size = serde_json::to_vec(config)
        .map(|bytes| bytes.len())
        .unwrap_or(usize::MAX);
    problems.check(size <= MAX_ADMIN_CONFIG, at, "too_large");
    if let Some(terminal) = config.get("terminal") {
        match terminal.as_object() {
            None => problems.add(format!("{at}/terminal"), "invalid_type"),
            Some(terminal) => {
                let backend = terminal.get("backend");
                problems.check(
                    backend.is_none_or(|b| b == MANAGED_BACKEND),
                    format!("{at}/terminal/backend"),
                    "platform_enforced",
                );
                for key in ["manager_url", "token_file", "ca_file", "ssh_key"] {
                    problems.check(
                        !terminal.contains_key(key),
                        format!("{at}/terminal/{key}"),
                        "platform_enforced",
                    );
                }
            }
        }
    }
    if let Some(plugins) = config.get("plugins") {
        match plugins.as_object() {
            None => problems.add(format!("{at}/plugins"), "invalid_type"),
            Some(plugins) => {
                if let Some(disabled) = plugins.get("disabled") {
                    let ok = disabled.as_array().is_some_and(|names| {
                        names
                            .iter()
                            .all(|name| name.is_string() && name != MANAGED_BACKEND)
                    });
                    problems.check(ok, format!("{at}/plugins/disabled"), "platform_enforced");
                }
                let managed = plugins
                    .get("entries")
                    .and_then(Value::as_object)
                    .is_some_and(|entries| entries.contains_key(MANAGED_BACKEND));
                problems.check(
                    !managed,
                    format!("{at}/plugins/entries/{MANAGED_BACKEND}"),
                    "platform_enforced",
                );
            }
        }
    }
}

/// Merges `overlay` into `base` leaf by leaf: objects merge, anything else
/// replaces. Used to layer assignment overrides over template settings.
pub fn merge(base: &mut Map<String, Value>, overlay: &Map<String, Value>) {
    for (key, value) in overlay {
        match (base.get_mut(key), value) {
            (Some(Value::Object(existing)), Value::Object(update)) => merge(existing, update),
            _ => {
                base.insert(key.clone(), value.clone());
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn codes(config: Value) -> Vec<(String, &'static str)> {
        let mut problems = Problems::default();
        validate_admin_config(config.as_object().unwrap(), "/adminConfig", &mut problems);
        problems
            .into_vec()
            .into_iter()
            .map(|p| (p.pointer, p.code))
            .collect()
    }

    #[test]
    fn platform_enforced_keys_cannot_be_overridden() {
        assert!(
            codes(
                json!({"model": {"provider": "custom"}, "terminal": {"runtime_id": "python-tools"}})
            )
            .is_empty()
        );
        assert_eq!(
            codes(json!({"terminal": {"backend": "local"}})),
            [(
                "/adminConfig/terminal/backend".to_owned(),
                "platform_enforced"
            )]
        );
        assert_eq!(
            codes(json!({"plugins": {"disabled": ["managed_execution"]}})),
            [(
                "/adminConfig/plugins/disabled".to_owned(),
                "platform_enforced"
            )]
        );
        assert_eq!(
            codes(json!({"plugins": {"entries": {"managed_execution": {"settings": {}}}}})),
            [(
                "/adminConfig/plugins/entries/managed_execution".to_owned(),
                "platform_enforced"
            )]
        );
        assert_eq!(
            codes(json!({"terminal": "local"})),
            [("/adminConfig/terminal".to_owned(), "invalid_type")]
        );
        assert_eq!(
            codes(json!({"x": "a".repeat(9000)})),
            [("/adminConfig".to_owned(), "too_large")]
        );
    }

    fn content(config: Value) -> Vec<(String, &'static str)> {
        let mut problems = Problems::default();
        validate_admin_config_content(config.as_object().unwrap(), "/adminConfig", &mut problems);
        problems
            .into_vec()
            .into_iter()
            .map(|p| (p.pointer, p.code))
            .collect()
    }

    #[test]
    fn a_template_holds_no_credential_or_personal_data_but_may_reference_a_secret() {
        let flagged = |pointer: &str| [(pointer.to_owned(), "sensitive_field")];
        assert_eq!(
            content(json!({"model": {"api_key": "sk-123"}})),
            flagged("/adminConfig/model/api_key")
        );
        assert_eq!(
            content(json!({"x-API-Key": "k"})),
            flagged("/adminConfig/x-API-Key")
        );
        assert_eq!(
            content(json!({"list": [{"password": "p"}]})),
            flagged("/adminConfig/list/0/password")
        );
        assert_eq!(
            content(json!({"a/b": {"system_prompt": "be brief"}})),
            flagged("/adminConfig/a~1b/system_prompt")
        );
        // The value an admin secret delivers as that environment variable.
        assert!(content(json!({"model": {"api_key": "${OPENAI_API_KEY}"}})).is_empty());
        for not_a_reference in ["${lower}", "x ${A}", "${A} ", "${}", "${1A}", "$A"] {
            assert_eq!(
                content(json!({"model": {"api_key": not_a_reference}})),
                flagged("/adminConfig/model/api_key"),
                "{not_a_reference}"
            );
        }
        assert!(
            content(json!({"model": {"provider": "custom"}, "display": {"compact": true}}))
                .is_empty()
        );
    }

    #[test]
    fn nesting_is_bounded_at_sixteen_levels() {
        let nested = |levels: usize| (0..levels).fold(json!({}), |inner, _| json!({"n": inner}));
        assert!(content(nested(16)).is_empty());
        let too_deep = content(nested(20));
        assert_eq!(too_deep.len(), 1);
        assert_eq!(too_deep[0].1, "depth_limit");
        assert_eq!(too_deep[0].0, format!("/adminConfig{}", "/n".repeat(17)));
    }

    #[test]
    fn the_reviewed_terminal_and_plugin_settings_keep_to_their_limits() {
        let at = |pointer: &str, code| (format!("/adminConfig{pointer}"), code);
        assert!(content(json!({"terminal": {"runtime_id": null}})).is_empty());
        assert!(content(json!({"terminal": {"runtime_id": "x".repeat(63)}})).is_empty());
        assert_eq!(
            content(json!({"terminal": {"runtime_id": "x".repeat(64)}})),
            [at("/terminal/runtime_id", "too_large")]
        );
        assert_eq!(
            content(json!({"terminal": {"runtime_id": 5}})),
            [at("/terminal/runtime_id", "invalid_type")]
        );
        let names = |n: usize| json!((0..n).map(|i| format!("p{i}")).collect::<Vec<_>>());
        assert!(
            content(json!({"plugins": {"enabled": names(64), "disabled": names(64)}})).is_empty()
        );
        assert_eq!(
            content(json!({"plugins": {"enabled": names(65), "disabled": names(65)}})),
            [
                at("/plugins/enabled", "too_large"),
                at("/plugins/disabled", "too_large")
            ]
        );
        assert_eq!(
            content(json!({"plugins": {"enabled": ["a", 1]}})),
            [at("/plugins/enabled/1", "invalid_type")]
        );
        assert_eq!(
            content(json!({"plugins": {"enabled": "a"}})),
            [at("/plugins/enabled", "invalid_type")]
        );
    }

    #[test]
    fn settings_without_a_reviewed_schema_are_reported_not_refused() {
        let config = json!({"model": {}, "terminal": {}, "plugins": {}, "a/b": 1});
        let pointers: Vec<_> = unvalidated_admin_keys(config.as_object().unwrap(), "/adminConfig")
            .into_iter()
            .map(|p| (p.pointer, p.code))
            .collect();
        assert_eq!(
            pointers,
            [
                ("/adminConfig/a~1b".to_owned(), "unvalidated"),
                ("/adminConfig/model".to_owned(), "unvalidated"),
            ]
        );
    }

    #[test]
    fn overrides_merge_leaf_by_leaf() {
        let mut base =
            json!({"model": {"provider": "custom", "default": "a"}, "agent": {"max_turns": 30}});
        let overlay = json!({"model": {"default": "b"}, "agent": 5});
        merge(base.as_object_mut().unwrap(), overlay.as_object().unwrap());
        assert_eq!(
            base,
            json!({"model": {"provider": "custom", "default": "b"}, "agent": 5})
        );
    }
}
