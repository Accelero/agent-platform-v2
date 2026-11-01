import {
  parseDocument,
  stringify,
  isMap,
  isSeq,
  isScalar,
  isAlias,
} from "yaml";
import { findRange, parseDraft, type Draft } from "./model";
export type ConfigurationFormat = "json" | "yaml";
export function parseConfiguration(
  text: string,
  format: ConfigurationFormat = "json",
): ReturnType<typeof parseDraft> {
  if (format === "json") return parseDraft(text);
  if (new TextEncoder().encode(text).length > 65536)
    return { error: "Configuration exceeds 64 KiB.", offset: 0 };
  try {
    const document = parseDocument(text, {
      version: "1.2",
      schema: "core",
      uniqueKeys: true,
      merge: false,
      resolveKnownTags: false,
      prettyErrors: false,
    });
    if (document.errors.length || document.warnings.length) {
      const error = document.errors[0] ?? document.warnings[0];
      return {
        error:
          "Invalid YAML. Use one YAML 1.2 document with unique string keys.",
        offset: error.pos?.[0] ?? 0,
      };
    }
    let invalid: { error: string; offset: number } | undefined;
    const inspect = (node: unknown, depth: number): void => {
      if (!node || typeof node !== "object") return;
      const n = node as { tag?: string; anchor?: string; range?: number[] };
      const fail = (error: string) => {
        invalid ??= { error, offset: n.range?.[0] ?? 0 };
      };
      if (depth > 16) {
        fail("Configuration exceeds the supported nesting limit.");
        return;
      }
      if (isAlias(node) || n.anchor || n.tag) {
        fail("YAML anchors, aliases and explicit tags are not supported.");
        return;
      }
      if (isMap(node))
        for (const pair of node.items) {
          if (
            !isScalar(pair.key) ||
            typeof pair.key.value !== "string" ||
            pair.key.value === "<<"
          )
            fail(
              "YAML keys must be unique strings; merge keys are not supported.",
            );
          inspect(pair.key, depth);
          inspect(pair.value, depth + 1);
        }
      else if (isSeq(node))
        for (const item of node.items) inspect(item, depth + 1);
      else if (
        isScalar(node) &&
        typeof node.value === "number" &&
        (!Number.isFinite(node.value) ||
          (Number.isInteger(node.value) && !Number.isSafeInteger(node.value)))
      )
        fail(
          "Numbers must fit the exact JSON number range supported by the browser.",
        );
    };
    inspect(document.contents, 0);
    if (invalid) return invalid;
    return parseDraft(JSON.stringify(document.toJS({ maxAliasCount: 0 })));
  } catch {
    return { error: "Invalid YAML configuration.", offset: 0 };
  }
}
export function serializeConfiguration(
  value: Draft,
  format: ConfigurationFormat,
) {
  return format === "json"
    ? JSON.stringify(value, null, 2)
    : stringify(value, {
        version: "1.2",
        lineWidth: 0,
        aliasDuplicateObjects: false,
      });
}
function yamlRange(text: string, path: string[]) {
  try {
    const node = parseDocument(text).getIn(path, true) as
      { range?: number[] } | undefined;
    return node?.range ? { from: node.range[0], to: node.range[1] } : undefined;
  } catch {
    return undefined;
  }
}
/**
 * The text a finding points at. A setting that is not in the text has none, so
 * the nearest group that is stands in for it, on its first line; at the very
 * least that is the first line of the document.
 */
export function configurationRange(
  text: string,
  pointer: string,
  format: ConfigurationFormat,
) {
  const path = pointer
    .split("/")
    .slice(1)
    .map((key) => key.replace(/~1/g, "/").replace(/~0/g, "~"));
  const firstLine = (from: number) => {
    const end = text.indexOf("\n", from);
    return { from, to: end < 0 ? text.length : end };
  };
  for (let depth = path.length; depth >= 0; depth--) {
    const found = (format === "json" ? findRange : yamlRange)(
      text,
      path.slice(0, depth),
    );
    if (found) return depth === path.length ? found : firstLine(found.from);
  }
  return firstLine(0);
}
