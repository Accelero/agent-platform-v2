import { findNodeAtOffset, parseTree } from "jsonc-parser";
import type { Node } from "jsonc-parser";
import { schemas } from "../../schemas/generated/metadata";
import type { CatalogCollection } from "../../api";
import type { ConfigurationFormat } from "./yaml";

/**
 * Help for the definition editor: what a setting is, what it does and what
 * may be typed next. It reads only the descriptions and annotations of the
 * contract's JSON Schemas; the manager is what validates.
 */
export type Segment = string | number;
export type Schema = {
  $ref?: string;
  description?: string;
  type?: string | string[];
  properties?: Record<string, Schema>;
  required?: string[];
  additionalProperties?: boolean | Schema;
  items?: Schema;
  const?: unknown;
  enum?: unknown[];
  default?: unknown;
  examples?: unknown[];
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  maxItems?: number;
  "x-constraint"?: string;
};

const envelope = (collection: CatalogCollection) =>
  (collection === "terminal-backends"
    ? schemas.backend
    : schemas.template) as unknown as Schema;
const managed = schemas.managed as unknown as Schema;

function deref(base: Schema, node: Schema): Schema {
  let current = node;
  for (let depth = 0; current.$ref && depth < 8; depth++) {
    const target = current.$ref.startsWith("#/")
      ? (current.$ref
          .slice(2)
          .split("/")
          .reduce<unknown>(
            (at, key) => (at as Record<string, unknown> | undefined)?.[key],
            base,
          ) as Schema | undefined)
      : undefined;
    if (!target) break;
    const own = { ...current };
    delete own.$ref;
    current = { ...target, ...own };
  }
  return current;
}

function step(base: Schema, node: Schema, segment: Segment) {
  const here = deref(base, node);
  if (typeof segment === "number")
    return here.items ? deref(base, here.items) : undefined;
  const property = here.properties?.[segment];
  if (property) return deref(base, property);
  const extra = here.additionalProperties;
  return extra && typeof extra === "object" ? deref(base, extra) : undefined;
}

type Resolved = {
  base: Schema;
  schema?: Schema;
  required: boolean;
  /** A Hermes setting the manager keeps as given but has not reviewed. */
  unreviewed: boolean;
};

/**
 * The schema of the value at `path`. For `children` the path names a group
 * whose settings are wanted, so `adminConfig` itself resolves to the reviewed
 * Hermes settings; for `self` it resolves to the template's own `adminConfig`.
 */
function resolve(
  collection: CatalogCollection,
  path: Segment[],
  purpose: "self" | "children",
): Resolved {
  const inAdmin =
    collection === "templates" &&
    path[0] === "adminConfig" &&
    path.length >= (purpose === "children" ? 1 : 2);
  const base = inAdmin ? managed : envelope(collection);
  const walk = inAdmin ? path.slice(1) : path;
  let node: Schema | undefined = deref(base, base);
  let parent: Schema | undefined;
  for (const segment of walk) {
    parent = node;
    node = node && step(base, node, segment);
    if (!node) break;
  }
  const last = walk.at(-1);
  return {
    base,
    schema: node,
    required:
      typeof last === "string" &&
      !!parent &&
      !!deref(base, parent).required?.includes(last),
    unreviewed: inAdmin && !node,
  };
}

export type Help = {
  name: string;
  type: string;
  required: boolean;
  description: string;
  /** What else is worth knowing: limits, allowed values, default, example. */
  facts: string[];
  /** The settings inside a group, required ones starred. */
  settings: string[];
};

function typeLabel(schema: Schema) {
  if (schema.const !== undefined) return "fixed value";
  const types = [schema.type ?? []].flat();
  if (!types.length)
    return schema.properties || schema.additionalProperties ? "group" : "value";
  return types
    .map((type) =>
      type === "integer"
        ? "whole number"
        : type === "string"
          ? "text"
          : type === "boolean"
            ? "true or false"
            : type === "array"
              ? "list"
              : type === "null"
                ? "empty"
                : schema.properties
                  ? "group of settings"
                  : typeof schema.additionalProperties === "object"
                    ? "named entries"
                    : "group of settings",
    )
    .join(" or ");
}

const show = (value: unknown) =>
  typeof value === "string" ? value : JSON.stringify(value);

function factsOf(schema: Schema) {
  const facts: string[] = [];
  if (schema["x-constraint"]) facts.push(schema["x-constraint"]);
  if (schema.const !== undefined) facts.push(`Must be ${show(schema.const)}.`);
  if (schema.enum) facts.push(`One of: ${schema.enum.map(show).join(", ")}.`);
  const { minimum, maximum } = schema;
  if (minimum !== undefined && maximum !== undefined)
    facts.push(`Range: ${minimum}–${maximum}.`);
  else if (minimum !== undefined) facts.push(`At least ${minimum}.`);
  else if (maximum !== undefined) facts.push(`At most ${maximum}.`);
  if (schema.maxLength !== undefined && schema.maxLength <= 128)
    facts.push(`Up to ${schema.maxLength} characters.`);
  if (schema.maxItems !== undefined)
    facts.push(`Up to ${schema.maxItems} entries.`);
  if (schema.default !== undefined)
    facts.push(`Default: ${show(schema.default)}.`);
  if (schema.examples?.length)
    facts.push(`Example: ${show(schema.examples[0])}`);
  return facts;
}

/** What the editor tells an administrator about the setting at `path`. */
export function helpFor(
  collection: CatalogCollection,
  path: Segment[],
): Help | undefined {
  const name = path.at(-1);
  if (typeof name !== "string") return undefined;
  const { base, schema, required, unreviewed } = resolve(
    collection,
    path,
    "self",
  );
  if (!schema)
    return {
      name,
      type: unreviewed ? "Hermes setting" : "unknown",
      required: false,
      description: unreviewed
        ? "A Hermes setting. The manager keeps it as given and does not review it, so its name and value are not checked."
        : "This is not a setting of this definition. Validation reports it as unknown.",
      facts: [],
      settings: [],
    };
  const here = deref(base, schema);
  return {
    name,
    type: typeLabel(here),
    required,
    description: here.description ?? "",
    facts: factsOf(here),
    settings: Object.keys(here.properties ?? {}).map((key) =>
      here.required?.includes(key) ? `${key}*` : key,
    ),
  };
}

export type Hints = {
  storageSources: { id: string; dynamic: boolean; detail: string }[];
  secrets: { name: string; kind: string }[];
};
export const noHints: Hints = { storageSources: [], secrets: [] };

export type Suggestion = {
  kind: "key" | "value";
  /** What is shown and matched. */
  label: string;
  detail: string;
  /** For a key: how its value starts. */
  shape: "scalar" | "text" | "object" | "list";
  /** For a value: the text to insert, in the editor's own syntax. */
  text: string;
  help?: Help;
  boost: number;
};

export type Suggestions = {
  from: number;
  to: number;
  /** The column of the key being typed, for the lines a key opens. */
  indent: number;
  /** A value is being typed after its key. */
  value: boolean;
  /** Text follows the cursor that already continues the key. */
  keyContinues: boolean;
  suggestions: Suggestion[];
};

type Context =
  | {
      kind: "key";
      parent: Segment[];
      siblings: Set<string>;
      from: number;
      to: number;
      indent: number;
      keyContinues: boolean;
    }
  | { kind: "value"; path: Segment[]; from: number; to: number };

// ---------------------------------------------------------------- YAML

type Frame = { indent: number; key?: string; index?: number; items: number };
const framePath = (frames: Frame[]): Segment[] =>
  frames.map((frame) => frame.key ?? frame.index!);
const KEY =
  /^("(?:[^"\\]|\\.)*"|'[^']*'|[^\s:#"'\-?[\]{}&*!|>%@`,][^:#]*?)\s*:(?=\s|$)/;
const unquote = (key: string) =>
  /^["']/.test(key) ? key.slice(1, -1) : key.trim();

type YamlLine = {
  /** The start of what the line says, after indentation and `- ` marks. */
  content: number;
  parent: Segment[];
  key?: { name: string; from: number; to: number; colon: number };
};

/**
 * Reads the block structure of YAML text by indentation. Unfinished text is
 * the normal case while typing, so this never fails: it reports where each
 * line sits and, for the line at `pos`, which setting group it belongs to.
 */
function scanYaml(text: string, pos: number) {
  const stack: Frame[] = [];
  const rootCounter = { items: 0 } as Frame;
  const siblings = new Map<string, { name: string; line: number }[]>();
  let cursor: YamlLine | undefined;
  let cursorLine = -1;
  const lines = text.split("\n");
  let offset = 0;
  const found: (YamlLine | undefined)[] = [];
  lines.forEach((line, number) => {
    const start = offset;
    offset += line.length + 1;
    let column = line.length - line.trimStart().length;
    if (line.trimStart().startsWith("#")) return void found.push(undefined);
    const dashes = () => /^(-)( +|$)/.exec(line.slice(column));
    for (let dash = dashes(); dash; dash = dashes()) {
      while (stack.length && stack.at(-1)!.indent >= column) stack.pop();
      const owner = stack.at(-1) ?? rootCounter;
      stack.push({ indent: column, index: owner.items++, items: 0 });
      column += 1 + dash[2].length;
    }
    const isCursor = pos >= start && pos <= start + line.length;
    const rest = line.slice(column);
    const key = KEY.exec(rest);
    while (stack.length && stack.at(-1)!.indent >= column) stack.pop();
    const parent = framePath(stack);
    const entry: YamlLine = { content: start + column, parent };
    if (key) {
      const name = unquote(key[1]);
      entry.key = {
        name,
        from: start + column,
        to: start + column + key[1].length,
        colon: start + column + key[0].length - 1,
      };
      const id = JSON.stringify(parent);
      siblings.set(id, [...(siblings.get(id) ?? []), { name, line: number }]);
      stack.push({ indent: column, key: name, items: 0 });
    }
    found.push(entry);
    if (isCursor) {
      cursor = entry;
      cursorLine = number;
    }
  });
  return { cursor, cursorLine, siblings };
}

function yamlContext(text: string, pos: number): Context | undefined {
  const { cursor, cursorLine, siblings } = scanYaml(text, pos);
  if (!cursor) return undefined;
  if (cursor.key && pos > cursor.key.colon) {
    const after = text.slice(cursor.key.colon + 1, pos);
    const spaces = after.length - after.trimStart().length;
    return {
      kind: "value",
      path: [...cursor.parent, cursor.key.name],
      from: cursor.key.colon + 1 + spaces,
      to: pos,
    };
  }
  if (pos < cursor.content) return undefined;
  const lineEnd = text.indexOf("\n", pos);
  const tail = text.slice(pos, lineEnd < 0 ? undefined : lineEnd);
  const column =
    cursor.content - (text.lastIndexOf("\n", cursor.content - 1) + 1);
  return {
    kind: "key",
    parent: cursor.parent,
    siblings: new Set(
      (siblings.get(JSON.stringify(cursor.parent)) ?? [])
        .filter((entry) => entry.line !== cursorLine)
        .map((entry) => entry.name),
    ),
    from: cursor.content,
    to: pos,
    indent: column,
    keyContinues: /^[\w-]*\s*:/.test(tail),
  };
}

function yamlKeyAt(text: string, pos: number) {
  const { cursor } = scanYaml(text, pos);
  const key = cursor?.key;
  if (!cursor || !key || pos < key.from || pos > key.to) return undefined;
  return { path: [...cursor.parent, key.name], from: key.from, to: key.to };
}

// ---------------------------------------------------------------- JSON

function nodePath(node: Node | undefined): Segment[] {
  const path: Segment[] = [];
  for (let at = node; at?.parent; at = at.parent) {
    const parent = at.parent;
    if (parent.type === "array") path.unshift(parent.children!.indexOf(at));
    else if (parent.type === "property" && parent.children![1] === at)
      path.unshift(String(parent.children![0].value));
  }
  return path;
}

function jsonKeyAt(text: string, pos: number) {
  const tree = parseTree(text);
  const node = tree && findNodeAtOffset(tree, pos, true);
  const property = node?.parent;
  if (
    !node ||
    node.type !== "string" ||
    property?.type !== "property" ||
    property.children![0] !== node
  )
    return undefined;
  return {
    path: [...nodePath(property.parent), String(node.value)],
    from: node.offset,
    to: node.offset + node.length,
  };
}

function jsonContext(text: string, pos: number): Context | undefined {
  const before = text.slice(0, pos);
  const typed = /"?[\w-]*$/.exec(before)![0];
  const from = pos - typed.length;
  // A closing quote that is already there belongs to what is being typed.
  const to = typed.startsWith('"') && text[pos] === '"' ? pos + 1 : pos;
  const previous = before.slice(0, from).trimEnd().at(-1);
  const patched = (replacement: string) =>
    text.slice(0, from) + replacement + text.slice(to);
  if (previous === ":") {
    const tree = parseTree(patched("0"));
    const node = tree && findNodeAtOffset(tree, from, true);
    if (!node || node.parent?.type !== "property") return undefined;
    return { kind: "value", path: nodePath(node), from, to };
  }
  if (previous !== "{" && previous !== ",") return undefined;
  const placeholder = '"__": 0';
  const tree = parseTree(patched(placeholder));
  const node = tree && findNodeAtOffset(tree, from + 1, true);
  const property = node?.parent;
  if (
    !node ||
    property?.type !== "property" ||
    property.children![0] !== node ||
    property.parent?.type !== "object"
  )
    return undefined;
  const object = property.parent;
  const column = from - (text.lastIndexOf("\n", from - 1) + 1);
  return {
    kind: "key",
    parent: nodePath(object),
    siblings: new Set(
      object
        .children!.filter((child) => child !== property)
        .map((child) => String(child.children![0].value)),
    ),
    from,
    to,
    indent: column,
    keyContinues: /^\s*:/.test(text.slice(to)),
  };
}

// ----------------------------------------------------------- suggestions

/** The key at `pos`, if the position is on one, for the hover. */
export function keyAt(text: string, pos: number, format: ConfigurationFormat) {
  return format === "json" ? jsonKeyAt(text, pos) : yamlKeyAt(text, pos);
}

const plain = /^[A-Za-z0-9_./@][\w./@ -]*$/;
const ambiguous = /^(true|false|null|yes|no|on|off|~|[-+]?[\d.]+)$/i;
function written(value: unknown, format: ConfigurationFormat) {
  if (format === "json") return JSON.stringify(value);
  return typeof value === "string" &&
    plain.test(value) &&
    !ambiguous.test(value)
    ? value
    : JSON.stringify(value);
}

function hintedValues(
  collection: CatalogCollection,
  path: Segment[],
  hints: Hints,
): { name: string; detail: string }[] {
  const shape = path.map((segment) =>
    typeof segment === "number" ? "*" : segment,
  );
  const at = (...expected: string[]) =>
    shape.length === expected.length &&
    expected.every((name, index) => name === "*" || name === shape[index]);
  const sources = (all: boolean) =>
    hints.storageSources
      .filter((source) => all || source.dynamic)
      .map((source) => ({ name: source.id, detail: source.detail }));
  const secrets = (kinds: string[]) =>
    hints.secrets
      .filter((secret) => kinds.includes(secret.kind))
      .map((secret) => ({
        name: secret.name,
        detail: `${secret.kind} secret`,
      }));
  if (collection === "templates") {
    if (at("home", "source")) return sources(false);
    if (shape[0] === "defaultVolumes" && at("defaultVolumes", "*", "source"))
      return sources(true);
    if (at("secrets", "*", "name")) return secrets(["env", "file"]);
  } else if (at("registrySecret")) return secrets(["registry"]);
  return [];
}

function keySuggestions(
  collection: CatalogCollection,
  context: Extract<Context, { kind: "key" }>,
): Suggestion[] {
  const { base, schema } = resolve(collection, context.parent, "children");
  const group = schema && deref(base, schema);
  const names = Object.keys(group?.properties ?? {}).filter(
    (name) => !context.siblings.has(name),
  );
  return names.map((name) => {
    const property = deref(base, group!.properties![name]);
    const required = !!group!.required?.includes(name);
    const types = [property.type ?? []].flat();
    const isList = types.includes("array");
    const isGroup =
      !isList &&
      (types.includes("object") ||
        !!property.properties ||
        typeof property.additionalProperties === "object" ||
        (context.parent.length === 0 && name === "adminConfig"));
    return {
      kind: "key" as const,
      label: name,
      detail: `${typeLabel(property)}${required ? ", required" : ""}`,
      shape: isList
        ? ("list" as const)
        : isGroup
          ? ("object" as const)
          : types.includes("string") || typeof property.const === "string"
            ? ("text" as const)
            : ("scalar" as const),
      text: name,
      help: helpFor(collection, [...context.parent, name]),
      boost: required ? 10 : 0,
    };
  });
}

function valueSuggestions(
  collection: CatalogCollection,
  path: Segment[],
  format: ConfigurationFormat,
  hints: Hints,
): Suggestion[] {
  const { base, schema } = resolve(collection, path, "self");
  const own = schema ? deref(base, schema) : undefined;
  const found: { value: unknown; detail: string; boost: number }[] = [];
  if (own) {
    if (own.const !== undefined)
      found.push({ value: own.const, detail: "the only value", boost: 9 });
    for (const value of own.enum ?? [])
      found.push({ value, detail: "allowed value", boost: 9 });
    if ([own.type ?? []].flat().includes("boolean"))
      for (const value of [false, true])
        found.push({ value, detail: "true or false", boost: 5 });
    for (const value of own.examples ?? [])
      found.push({ value, detail: "example", boost: -1 });
  }
  for (const hint of hintedValues(collection, path, hints))
    found.push({ value: hint.name, detail: hint.detail, boost: 8 });
  const seen = new Set<string>();
  return found
    .filter(({ value }) => !seen.has(show(value)) && !!seen.add(show(value)))
    .map(({ value, detail, boost }) => ({
      kind: "value" as const,
      label: show(value),
      detail,
      shape: "scalar" as const,
      text: written(value, format),
      boost,
    }));
}

/** What may be typed at `pos`, or nothing when the position takes no suggestion. */
export function suggestAt(
  collection: CatalogCollection,
  text: string,
  pos: number,
  format: ConfigurationFormat,
  hints: Hints = noHints,
): Suggestions | undefined {
  const context =
    format === "json" ? jsonContext(text, pos) : yamlContext(text, pos);
  if (!context) return undefined;
  if (context.kind === "value")
    return {
      from: context.from,
      to: context.to,
      indent: 0,
      value: true,
      keyContinues: false,
      suggestions: valueSuggestions(collection, context.path, format, hints),
    };
  return {
    from: context.from,
    to: context.to,
    indent: context.indent,
    value: false,
    keyContinues: context.keyContinues,
    suggestions: keySuggestions(collection, context),
  };
}
