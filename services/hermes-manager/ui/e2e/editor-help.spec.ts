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
  await page.keyboard.insertText("secrets:\n  - name: ");
  await page.keyboard.press("Control+Space");
  await expect(page.locator(".cm-tooltip-autocomplete")).toContainText(
    "llm-key",
  );
});
