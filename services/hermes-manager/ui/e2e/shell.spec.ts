import { expect, test } from "@playwright/test";
import { mockApi } from "./fixtures";

const background = (page: import("@playwright/test").Page) =>
  page.evaluate(() => getComputedStyle(document.body).backgroundColor);
const LIGHT = "rgb(255, 255, 255)";
const DARK = "rgb(10, 10, 10)";

test("the theme follows the system until one is chosen, and remembers the choice", async ({
  page,
}) => {
  await mockApi(page);
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/");
  expect(await background(page)).toBe(DARK);
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", /.+/);

  const theme = page.getByRole("group", { name: "Colour theme" });
  await expect(theme.getByRole("button", { name: "System" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await theme.getByRole("button", { name: "Light" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  expect(await background(page)).toBe(LIGHT);

  // The choice survives a reload, and beats the system setting.
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  expect(await background(page)).toBe(LIGHT);

  await page
    .getByRole("group", { name: "Colour theme" })
    .getByRole("button", { name: "System" })
    .click();
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", /.+/);
  expect(await background(page)).toBe(DARK);
  await page.emulateMedia({ colorScheme: "light" });
  expect(await background(page)).toBe(LIGHT);
});

test("the sidebar collapses to an icon rail that keeps every link reachable", async ({
  page,
}) => {
  await mockApi(page);
  await page.goto("/");
  const toggle = page.getByRole("button", { name: "Collapse sidebar" });
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  const width = () =>
    page.locator(".sidebar").evaluate((el) => el.getBoundingClientRect().width);
  expect(await width()).toBeGreaterThan(200);
  await toggle.click();
  await expect(
    page.getByRole("button", { name: "Expand sidebar" }),
  ).toHaveAttribute("aria-expanded", "false");
  expect(await width()).toBeLessThan(80);
  // Collapsed links keep their names for screen readers and show them on hover.
  const users = page
    .getByRole("navigation")
    .getByRole("link", { name: "Users" });
  await expect(users).toHaveAttribute("title", "Users");
  await users.click();
  await expect(page).toHaveURL(/\/users(\?|$)/);

  await page.reload();
  expect(await width()).toBeLessThan(80);
  await page.getByRole("button", { name: "Expand sidebar" }).click();
  expect(await width()).toBeGreaterThan(200);
});

test("on a phone the navigation is a drawer that closes when a page is chosen", async ({
  page,
}) => {
  await mockApi(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  const open = page.getByRole("button", { name: "Open navigation" });
  await expect(open).toHaveAttribute("aria-expanded", "false");
  await expect(
    page.getByRole("navigation").getByRole("link", { name: "Users" }),
  ).not.toBeInViewport();
  await open.click();
  const users = page
    .getByRole("navigation")
    .getByRole("link", { name: "Users" });
  await expect(users).toBeInViewport();
  await users.click();
  await expect(page).toHaveURL(/\/users(\?|$)/);
  await expect(
    page.getByRole("button", { name: "Open navigation" }),
  ).toBeVisible();
  await expect(users).not.toBeInViewport();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("the account lives at the top right, and the page carries no advertising", async ({
  page,
}) => {
  await mockApi(page);
  await page.goto("/");
  const header = page.locator("header");
  const account = header.getByRole("button", { name: "Account" });
  const box = await account.boundingBox();
  const viewport = page.viewportSize()!;
  expect(box!.y).toBeLessThan(60);
  expect(box!.x).toBeGreaterThan(viewport.width * 0.7);
  await account.click();
  await expect(page.getByText("Signed in as")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();

  const text = await page.locator("body").innerText();
  for (const gone of [
    "Private instances. Explicit permissions.",
    "A place for every agent.",
    "Infrastructure, with identity.",
    "Personal agent infrastructure",
    "SIGNED IN WITH KEYCLOAK",
  ])
    expect(text).not.toContain(gone);
});

test("the landing page shows the installation's real figures", async ({
  page,
}) => {
  await mockApi(page);
  await page.goto("/");
  const status = page.getByRole("region", { name: "Status" });
  await expect(status.getByText("Instances")).toBeVisible();
  await expect(status.getByText("1 ready")).toBeVisible();
  await expect(
    status
      .getByRole("listitem")
      .filter({ hasText: "Hermes templates" })
      .getByText("1 published"),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Recent activity" }),
  ).toBeVisible();
});
