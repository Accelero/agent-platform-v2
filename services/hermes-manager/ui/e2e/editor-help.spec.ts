import { expect, test, type Page } from "@playwright/test";
import { mockApi } from "./fixtures";

async function openEditor(page: Page) {
  await mockApi(page);
  await page.goto("/templates/standard?version=2");
  await page.getByRole("button", { name: "Edit draft" }).click();
  await expect(page.getByLabel("Configuration YAML editor")).toBeVisible();
}

test("hovering a setting says what it does", async ({ page }) => {
  await openEditor(page);
  const tip = page.locator(".cm-tooltip .cm-help");
  await page
    .locator(".cm-line", { hasText: /^image:/ })
    .first()
    .hover({ position: { x: 12, y: 8 } });
  await expect(tip).toContainText("image");
  await expect(tip).toContainText("The Hermes image every instance");
  await expect(tip).toContainText("Pinned by digest");
  await expect(tip).toContainText("required");

  // A Hermes setting the manager does not review says so.
  await page
    .locator(".cm-line", { hasText: /^\s+model:/ })
    .first()
    .hover({ position: { x: 34, y: 8 } });
  await expect(tip).toContainText("does not review it");
});

test("typing suggests the settings, then the values this installation has", async ({
  page,
}) => {
  await openEditor(page);
  await page
    .getByRole("textbox", { name: "Configuration YAML editor" })
    .click();
  await page.keyboard.press("Control+A");
  await page.keyboard.type("home:");
  await page.keyboard.press("Enter");
  await page.keyboard.type("  so");

  const list = page.locator(".cm-tooltip-autocomplete");
  await expect(list).toContainText("source");
  await expect(list).not.toContainText("resources");
  await expect(page.locator(".cm-completionInfo")).toContainText(
    "storage class",
  );
  await page.keyboard.press("Enter");
  await expect(page.locator(".cm-content")).toContainText("source:");

  // The home must be on a storage class, so the static source is not offered.
  await expect(list).toContainText("nvme");
  await expect(list).not.toContainText("host-data");
  await page.keyboard.press("Enter");
  await expect(page.locator(".cm-content")).toContainText("source: nvme");
});

test("a list entry suggests the admin secrets that exist", async ({ page }) => {
  await openEditor(page);
  await page
    .getByRole("textbox", { name: "Configuration YAML editor" })
    .click();
  await page.keyboard.press("Control+A");
  await page.keyboard.type("secrets:");
  await page.keyboard.press("Enter");
  await page.keyboard.type("  - name: ");
  // The installation's secrets load in the background; ask again until they are in.
  await expect(async () => {
    await page.keyboard.press("Control+Space");
    await expect(page.locator(".cm-tooltip-autocomplete")).toContainText(
      "llm-key",
      { timeout: 1000 },
    );
  }).toPass();
});

test("the manager's findings are marked in the editor until the text changes", async ({
  page,
}) => {
  await openEditor(page);
  await expect(page.locator(".cm-lint-marker")).toHaveCount(0);
  await page.getByRole("button", { name: "Validate" }).click();
  await expect(page.getByText("No admin secret has this name.")).toBeVisible();

  // The finding is on the secret's name: marked in the gutter and underlined.
  await expect(page.locator(".cm-lint-marker").first()).toBeVisible();
  await expect(page.locator(".cm-lintRange").first()).toBeVisible();
  await expect(page.locator(".cm-lintRange").first()).toHaveText("llm-key");

  // Once the text changes the answer is stale and its marks go.
  await page
    .getByRole("textbox", { name: "Configuration YAML editor" })
    .click();
  await page.keyboard.press("Control+End");
  await page.keyboard.type("# edited");
  await expect(page.locator(".cm-lint-marker")).toHaveCount(0);
});

test("keys, strings and numbers are coloured apart, in YAML and JSON", async ({
  page,
}) => {
  await openEditor(page);
  const colours = () =>
    page
      .locator(".cm-content span")
      .evaluateAll((spans) => [
        ...new Set(spans.map((span) => getComputedStyle(span).color)),
      ]);
  const key = "rgb(27, 94, 75)";
  const string = "rgb(154, 52, 18)";
  const number = "rgb(29, 78, 216)";
  expect(await colours()).toEqual(expect.arrayContaining([key, number]));
  await page.getByRole("tab", { name: "JSON" }).click();
  await expect(page.getByLabel("Configuration JSON editor")).toBeVisible();
  expect(await colours()).toEqual(
    expect.arrayContaining([key, string, number]),
  );
});
