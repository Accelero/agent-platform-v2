import { describe, expect, it } from "vitest";
import { schemas } from "../../schemas/generated/metadata";
import { helpFor, keyAt, noHints, suggestAt } from "./schema-help";
import type { Hints, Schema } from "./schema-help";

const labels = (
  text: string,
  format: "json" | "yaml",
  collection: "templates" | "terminal-backends" = "templates",
  hints: Hints = noHints,
) => {
  const pos = text.indexOf("|");
  const clean = text.replace("|", "");
  const found = suggestAt(collection, clean, pos, format, hints);
  return found?.suggestions.map((s) => s.label);
};

describe("every setting of the contract is described", () => {
  const walk = (
    base: Schema,
    node: Schema,
    path: string,
    seen: string[],
    missing: string[],
  ) => {
    const ref = node.$ref
      ?.slice(2)
      .split("/")
      .reduce<any>((at, k) => at?.[k], base);
    const here: Schema = ref ? { ...ref, ...node } : node;
    if (!here.description && path) missing.push(path);
    if (node.$ref && seen.includes(node.$ref)) return;
    const next = node.$ref ? [...seen, node.$ref] : seen;
    for (const [name, child] of Object.entries(here.properties ?? {}))
      walk(base, child, `${path}/${name}`, next, missing);
    if (typeof here.additionalProperties === "object")
      walk(base, here.additionalProperties, `${path}/*`, next, missing);
    // A list of plain values is described by the list; entries that have
    // settings of their own are described too.
    if (here.items?.$ref || here.items?.properties)
      walk(base, here.items, `${path}/[]`, next, missing);
  };
  for (const [name, schema] of Object.entries(schemas)) {
    it(`${name} has a description on every setting`, () => {
      const missing: string[] = [];
      walk(
        schema as unknown as Schema,
        schema as unknown as Schema,
        "",
        [],
        missing,
      );
      expect(missing).toEqual([]);
    });
  }
});

describe("what a setting is", () => {
  it("explains a setting with its limits, default and example", () => {
    const cpu = helpFor("templates", ["resources", "cpuMillis"])!;
    expect(cpu).toMatchObject({
      name: "cpuMillis",
      type: "whole number",
      required: true,
    });
    expect(cpu.description).toContain("millicores");
    expect(cpu.facts).toContain("Range: 10–64000.");
    const image = helpFor("terminal-backends", ["image"])!;
    expect(image.facts).toContain(
      "Pinned by digest: registry/name@sha256:<64 hex digits>.",
    );
    expect(image.facts.some((fact) => fact.startsWith("Example: "))).toBe(true);
    expect(
      helpFor("templates", ["mounts", "workspace", "readOnly"])!.facts,
    ).toContain("Default: false.");
    expect(helpFor("templates", ["schemaVersion"])!.facts).toContain(
      "Must be 2.",
    );
  });

  it("follows references, named entries and lists", () => {
    expect(helpFor("templates", ["mounts", "workspace"])!.settings).toEqual([
      "mountPath*",
      "readOnly",
    ]);
    expect(
      helpFor("templates", ["mounts", "workspace", "mountPath"])!.description,
    ).toContain("Absolute path");
    expect(helpFor("templates", ["secrets", 0, "env"])!.facts).toContain(
      "Uppercase letters, digits and underscores; does not start with a digit.",
    );
    expect(helpFor("templates", ["home", "source"])!.description).toContain(
      "storage class",
    );
    expect(
      helpFor("terminal-backends", ["mounts", "workspace", "optional"])!
        .description,
    ).toContain("unbound required slot");
  });

  it("describes the reviewed Hermes settings and says so about the others", () => {
    expect(helpFor("templates", ["adminConfig"])!.description).toContain(
      "Hermes settings",
    );
    expect(
      helpFor("templates", ["adminConfig", "terminal", "runtime_id"])!
        .description,
    ).toContain("Pin the terminal backend");
    expect(
      helpFor("templates", ["adminConfig", "plugins", "disabled"])!.description,
    ).toContain("not loaded");
    expect(helpFor("templates", ["adminConfig", "model"])).toMatchObject({
      type: "Hermes setting",
      description: expect.stringContaining("does not review it"),
    });
    expect(helpFor("templates", ["colour"])!.description).toContain(
      "not a setting of this definition",
    );
  });
});

describe("where the cursor is", () => {
  it("finds the key under the pointer, in YAML and JSON", () => {
    const yaml =
      "resources:\n  cpuMillis: 1000\nsecrets:\n  - name: llm\n    env: A\n";
    expect(keyAt(yaml, yaml.indexOf("cpuMillis") + 2, "yaml")).toMatchObject({
      path: ["resources", "cpuMillis"],
    });
    expect(keyAt(yaml, yaml.indexOf("env") + 1, "yaml")?.path).toEqual([
      "secrets",
      0,
      "env",
    ]);
    expect(keyAt(yaml, yaml.indexOf("1000"), "yaml")).toBeUndefined();
    const json =
      '{"home": {"source": "nvme"}, "secrets": [{"name": "a"}, {"name": "b", "file": "f"}]}';
    expect(keyAt(json, json.indexOf("source") + 2, "json")?.path).toEqual([
      "home",
      "source",
    ]);
    expect(keyAt(json, json.indexOf('"file"') + 2, "json")?.path).toEqual([
      "secrets",
      1,
      "file",
    ]);
    expect(keyAt(json, json.indexOf("nvme"), "json")).toBeUndefined();
  });
});

describe("suggestions while typing", () => {
  it("offers the settings of the group being typed, without the ones already there", () => {
    expect(labels("resources:\n  cpuMillis: 1000\n  m|", "yaml")).toEqual([
      "memoryMib",
    ]);
    expect(labels("|", "yaml", "terminal-backends")).toEqual(
      expect.arrayContaining([
        "image",
        "sshContract",
        "uid",
        "gid",
        "resources",
        "limits",
        "mounts",
        "registrySecret",
      ]),
    );
    expect(labels('{"resources": {"cpuMillis": 1, "m|"}}', "json")).toEqual([
      "memoryMib",
    ]);
    expect(labels('{\n  "|"\n}', "json", "terminal-backends")).toContain(
      "sshContract",
    );
    expect(
      labels('{"image": "x", "|', "json", "terminal-backends"),
    ).not.toContain("image");
  });

  it("reaches inside named entries and list items", () => {
    expect(labels("mounts:\n  workspace:\n    |", "yaml")).toEqual([
      "mountPath",
      "readOnly",
    ]);
    expect(labels("secrets:\n  - name: llm\n    |", "yaml")).toEqual([
      "env",
      "file",
    ]);
    expect(labels("secrets:\n  - name: a\n    env: A\n  - |", "yaml")).toEqual([
      "name",
      "env",
      "file",
    ]);
    expect(labels('{"mounts": {"workspace": {"|"}}}', "json")).toEqual([
      "mountPath",
      "readOnly",
    ]);
  });

  it("puts the required settings first and offers the reviewed Hermes ones under adminConfig", () => {
    const found = suggestAt("templates", "", 0, "yaml")!.suggestions;
    const rank = (s: { boost: number }) => s.boost;
    expect(found.filter((s) => rank(s) > 0).map((s) => s.label)).toEqual(
      expect.arrayContaining(["schemaVersion", "image", "resources", "home"]),
    );
    expect(labels("adminConfig:\n  |", "yaml")).toEqual([
      "terminal",
      "plugins",
    ]);
    expect(labels("adminConfig:\n  terminal:\n    |", "yaml")).toEqual([
      "backend",
      "runtime_id",
    ]);
    expect(labels("adminConfig:\n  model:\n    |", "yaml")).toEqual([]);
  });

  it("suggests the values a setting can take", () => {
    expect(labels("sshContract: |", "yaml", "terminal-backends")).toEqual([
      "managed-ssh-v2",
    ]);
    expect(labels("schemaVersion: |", "yaml")).toEqual(["2"]);
    expect(labels("mounts:\n  workspace:\n    readOnly: |", "yaml")).toEqual([
      "false",
      "true",
    ]);
    expect(
      labels('{"sshContract": "man|"}', "json", "terminal-backends"),
    ).toEqual(["managed-ssh-v2"]);
    const found = suggestAt(
      "terminal-backends",
      '{"sshContract": "man"}',
      20,
      "json",
    )!;
    expect(found.suggestions[0].text).toBe('"managed-ssh-v2"');
    expect([found.from, found.to]).toEqual([16, 21]);
  });

  it("suggests the storage sources and secrets this installation has", () => {
    const hints: Hints = {
      storageSources: [
        { id: "nvme", dynamic: true, detail: "storage class" },
        { id: "host-data", dynamic: false, detail: "local path" },
      ],
      secrets: [
        { name: "llm-key", kind: "env" },
        { name: "pull-key", kind: "registry" },
      ],
    };
    expect(labels("home:\n  source: |", "yaml", "templates", hints)).toEqual([
      "nvme",
    ]);
    expect(
      labels(
        "defaultVolumes:\n  workspace:\n    source: |",
        "yaml",
        "templates",
        hints,
      ),
    ).toEqual(["nvme", "host-data"]);
    expect(labels("secrets:\n  - name: |", "yaml", "templates", hints)).toEqual(
      ["llm-key"],
    );
    expect(
      labels("registrySecret: |", "yaml", "terminal-backends", hints),
    ).toEqual(["pull-key"]);
  });

  it("stays quiet where nothing can be suggested", () => {
    expect(labels("# a comment |", "yaml")).toBeUndefined();
    expect(labels("resources:\n  cpuMillis: 1000 # |", "yaml")).toBeDefined();
    expect(labels('{"plugins": ["|"]}', "json")).toBeUndefined();
  });
});
