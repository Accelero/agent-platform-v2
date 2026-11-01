import { parseTree, getNodeValue, findNodeAtLocation } from "jsonc-parser";
import type { ParseError, Node } from "jsonc-parser";
export { initialDefinition } from "./initial";
export type Draft = Record<string, unknown>;
export function parseDraft(text: string): {
  value?: Draft;
  error?: string;
  offset?: number;
} {
  const errors: ParseError[] = [];
  const tree = parseTree(text, errors, {
    disallowComments: true,
    allowTrailingComma: false,
    allowEmptyContent: false,
  });
  if (errors.length || !tree)
    return {
      error: "Invalid JSON. Correct the syntax before switching to the form.",
      offset: errors[0]?.offset ?? 0,
    };
  let duplicate: Node | undefined;
  let deep = false;
  let imprecise: Node | undefined;
  function visit(node: Node, depth: number) {
    if (depth > 16) {
      deep = true;
      return;
    }
    if (
      node.type === "number" &&
      (!Number.isFinite(node.value) ||
        (Number.isInteger(node.value) && !Number.isSafeInteger(node.value)))
    )
      imprecise = node;
    if (node.type === "object") {
      const keys = new Set<string>();
      for (const property of node.children ?? []) {
        const key = String(property.children?.[0].value);
        if (keys.has(key)) duplicate = property;
        keys.add(key);
      }
    }
    if (node.type === "property") {
      if (node.children?.[1]) visit(node.children[1], depth);
    } else for (const child of node.children ?? []) visit(child, depth + 1);
  }
  visit(tree, 0);
  if (imprecise)
    return {
      error:
        "Numbers must fit the exact integer range supported by the browser.",
      offset: imprecise.offset,
    };
  if (duplicate)
    return {
      error: "Duplicate object keys are not allowed.",
      offset: duplicate.offset,
    };
  if (deep)
    return {
      error: "Configuration exceeds the supported nesting limit.",
      offset: 0,
    };
  if (tree.type !== "object")
    return { error: "The template must be a JSON object.", offset: 0 };
  return { value: getNodeValue(tree) as Draft };
}
/** The text of the value at `path` in JSON, or nothing when it is not there. */
export function findRange(text: string, path: string[]) {
  const tree = parseTree(text);
  if (!tree) return undefined;
  let node: Node | undefined = tree;
  for (const segment of path) {
    node = findNodeAtLocation(node, [
      node.type === "array" ? Number(segment) : segment,
    ]);
    if (!node) return undefined;
  }
  return { from: node.offset, to: node.offset + node.length };
}
const pointerPath = (pointer: string) =>
  pointer
    .split("/")
    .slice(1)
    .map((key) => key.replace(/~1/g, "/").replace(/~0/g, "~"));
export function pointerRange(text: string, pointer: string) {
  return findRange(text, pointerPath(pointer)) ?? { from: 0, to: 0 };
}
export function setPath(
  value: Draft,
  path: string[],
  next: unknown,
  remove = false,
): Draft {
  const result = structuredClone(value);
  let target = result;
  for (const key of path.slice(0, -1)) {
    const child = target[key];
    if (!child || typeof child !== "object" || Array.isArray(child))
      target[key] = {};
    target = target[key] as Draft;
  }
  if (remove) delete target[path.at(-1)!];
  else target[path.at(-1)!] = next;
  return result;
}
export function changedPaths(
  before: unknown,
  after: unknown,
  path = "",
): string[] {
  if (JSON.stringify(before) === JSON.stringify(after)) return [];
  if (
    before &&
    after &&
    typeof before === "object" &&
    typeof after === "object" &&
    !Array.isArray(before) &&
    !Array.isArray(after)
  )
    return Array.from(new Set([...Object.keys(before), ...Object.keys(after)]))
      .sort()
      .flatMap((key) =>
        changedPaths(
          (before as Draft)[key],
          (after as Draft)[key],
          `${path}/${key.replace(/~/g, "~0").replace(/\//g, "~1")}`,
        ),
      );
  return [path || "/"];
}
