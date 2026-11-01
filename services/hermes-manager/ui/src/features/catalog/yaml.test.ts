import { expect, it } from "vitest";
import {
  parseConfiguration,
  serializeConfiguration,
  configurationRange,
} from "./yaml";
it("round trips JSON and YAML without losing unknown fields, null or scalar types", () => {
  const value = {
    schemaVersion: 1,
    adminConfig: {
      advanced: [true, null, "yes", "2026-10-22", { nested: 42 }],
    },
  };
  const text = serializeConfiguration(value, "yaml");
  expect(parseConfiguration(text, "yaml").value).toEqual(value);
  expect(
    parseConfiguration(serializeConfiguration(value, "json")).value,
  ).toEqual(value);
});
for (const text of [
  "a: 1\na: 2",
  "a: &x [1]\nb: *x",
  "a: !!timestamp 2026-10-22",
  "? [a, b]\n: value",
  "a: .nan",
  "a: 9007199254740993",
  "a: 1\n---\nb: 2",
  "a: {<<: {b: 1}}",
])
  it(`rejects non-JSON YAML: ${text}`, () =>
    expect(parseConfiguration(text, "yaml").error).toBeTruthy());
it("addresses YAML values with escaped JSON pointers", () => {
  const text = "adminConfig:\n  a/b: false\n";
  const range = configurationRange(text, "/adminConfig/a~1b", "yaml");
  expect(text.slice(range.from, range.to)).toBe("false");
});
it("marks the nearest group that exists for a setting that is missing", () => {
  const yaml = "image: x\nlimits:\n  maxLeaseSeconds: 60\n";
  const group = configurationRange(yaml, "/limits/idleSeconds", "yaml");
  expect(yaml.slice(group.from, group.to)).toBe("maxLeaseSeconds: 60");
  const root = configurationRange(yaml, "/resources", "yaml");
  expect(yaml.slice(root.from, root.to)).toBe("image: x");
  expect(configurationRange(yaml, "/image", "yaml")).toEqual({
    from: 7,
    to: 8,
  });

  const json =
    '{\n  "image": "x",\n  "limits": {\n    "maxLeaseSeconds": 60\n  }\n}';
  const nested = configurationRange(json, "/limits/idleSeconds", "json");
  expect(json.slice(nested.from, nested.to)).toBe("{");
  expect(json.slice(nested.from - 10, nested.from)).toBe('"limits": ');
  const top = configurationRange(json, "/resources", "json");
  expect(json.slice(top.from, top.to)).toBe("{");
  expect(
    json.slice(
      configurationRange(json, "/image", "json").from,
      configurationRange(json, "/image", "json").to,
    ),
  ).toBe('"x"');
});
