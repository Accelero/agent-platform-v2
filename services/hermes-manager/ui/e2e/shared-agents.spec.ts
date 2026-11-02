import { test, expect } from "@playwright/test";
import {
  ASSIGNMENT,
  GROUP,
  HOSTILE,
  IDENTITY,
  ISSUER,
  identity,
  mockApi,
} from "./fixtures";

test("agent, group and identity labels render as text", async ({ page }) => {
  await mockApi(page, { shared: true, hostile: true });
  await page.goto(`/assignments/${ASSIGNMENT}`);
  await expect(page.getByRole("heading", { name: HOSTILE })).toBeVisible();
  await page.goto(`/groups/${GROUP}`);
  await expect(page.getByRole("heading", { name: HOSTILE })).toBeVisible();
  await expect(page.getByRole("cell", { name: HOSTILE })).toBeVisible();
  await expect(page.locator("img[src=x]")).toHaveCount(0);
  expect(
    await page.evaluate(
      () => (window as Window & { hostileExecuted?: boolean }).hostileExecuted,
    ),
  ).toBeUndefined();
});

test("create, edit and delete groups through versioned commands", async ({
  page,
}) => {
  const recorded = await mockApi(page);
  await page.goto("/groups");
  await page.getByRole("button", { name: "Create group" }).click();
  let dialog = page.getByRole("dialog");
  await dialog.getByLabel("Name", { exact: true }).fill("engineers");
  await dialog.getByLabel("Display name").fill("Engineers");
  await dialog.getByRole("button", { name: "Confirm change" }).click();
  await expect(
    dialog.getByText("Change accepted.", { exact: false }),
  ).toBeVisible();
  expect(
    recorded.find(
      (item) => item.path === "/api/v1/access-groups" && item.method === "POST",
    )!.body,
  ).toEqual({ name: "engineers", displayName: "Engineers" });
  await page.goto(`/groups/${GROUP}`);
  await page.getByRole("button", { name: "Edit group" }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("Display name").fill("Platform engineers");
  await dialog.getByRole("button", { name: "Confirm change" }).click();
  await expect(
    dialog.getByText("Change accepted.", { exact: false }),
  ).toBeVisible();
  expect(
    recorded.find(
      (item) => item.path.endsWith(GROUP) && item.method === "PATCH",
    )!.headers["if-match"],
  ).toBe('"5"');
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await page.getByRole("button", { name: "Delete group" }).click();
  await expect(
    dialog.getByText("A group with agent grants cannot be deleted.", {
      exact: false,
    }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Confirm change" }).click();
  await expect(
    dialog.getByText("Change accepted.", { exact: false }),
  ).toBeVisible();
  expect(
    recorded.find(
      (item) => item.path.endsWith(GROUP) && item.method === "DELETE",
    )!.headers["if-match"],
  ).toBe('"5"');
});

test("create a shared agent with an explicit endpoint and template", async ({
  page,
}) => {
  const recorded = await mockApi(page);
  await page.goto("/assignments");
  await page.getByRole("button", { name: "Create shared agent" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Name", { exact: true }).fill("engineering");
  await dialog.getByLabel("Display name").fill("Engineering");
  await dialog.getByLabel("Hostname label").fill("engineering");
  await dialog.getByLabel("Hermes template: ID").fill("standard");
  await dialog
    .getByLabel("Hermes template: published version")
    .selectOption("1");
  await dialog.getByRole("button", { name: "Create shared agent" }).click();
  await expect(dialog.getByText("Shared agent created.")).toBeVisible();
  const request = recorded.find(
    (item) => item.path === "/api/v1/assignments" && item.method === "POST",
  )!;
  expect(request.body).toMatchObject({
    sharedAgent: {
      name: "engineering",
      displayName: "Engineering",
      label: "engineering",
      enabled: false,
    },
    spec: { access: "enabled", template: { id: "standard", version: 1 } },
  });
  expect(request.headers["idempotency-key"]).toMatch(/^[0-9a-f-]{36}$/);
});

test("manage a shared endpoint and grants without claiming member privacy", async ({
  page,
}) => {
  const recorded = await mockApi(page, { shared: true });
  await page.goto(`/assignments/${ASSIGNMENT}`);
  await expect(
    page.getByText("Members share this agent and its workspace."),
  ).toBeVisible();
  await expect(
    page.getByText(
      "This status does not verify DNS, TLS or callback registration.",
      { exact: false },
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "Change endpoint" }).click();
  let dialog = page.getByRole("dialog");
  await expect(
    dialog.getByText("Renaming restarts the agent.", { exact: false }),
  ).toBeVisible();
  await dialog.getByLabel("Hostname label").fill("platform");
  await dialog.getByRole("button", { name: "Confirm change" }).click();
  await expect(
    dialog.getByText("Change accepted.", { exact: false }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await page.getByRole("button", { name: "Grant user" }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("Issuer").fill(ISSUER);
  await dialog.getByLabel("Subject").fill("carol");
  await dialog.getByRole("button", { name: "Confirm change" }).click();
  await expect(
    dialog.getByText("Change accepted.", { exact: false }),
  ).toBeVisible();
  const rename = recorded.find(
    (item) => item.path.endsWith("/endpoint") && item.method === "PATCH",
  )!;
  expect(rename.headers["if-match"]).toBe('"2"');
  expect(rename.body).toEqual({ label: "platform", enabled: true });
  const grant = recorded.find(
    (item) => item.path.endsWith("/grants") && item.method === "PUT",
  )!;
  expect(grant.headers["if-match"]).toBe('"4"');
  expect(grant.body).toEqual({
    grants: [{ groupId: GROUP }, { identity: identity("carol") }],
  });
});

test("add and remove a group member with the current group version", async ({
  page,
}) => {
  const recorded = await mockApi(page);
  await page.goto(`/groups/${GROUP}`);
  await expect(
    page.getByText("Removing a member ends their access", { exact: false }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Add member" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Identity: Issuer").fill(ISSUER);
  await dialog.getByLabel("Identity: Subject").fill("carol");
  await dialog.getByRole("button", { name: "Confirm change" }).click();
  await expect(
    dialog.getByText("Change accepted.", { exact: false }),
  ).toBeVisible();
  const add = recorded.find(
    (item) => item.path.endsWith("/members") && item.method === "POST",
  )!;
  expect(add.headers["if-match"]).toBe('"5"');
  expect(add.body).toEqual({ identity: identity("carol") });
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await page.getByRole("button", { name: "Remove member" }).click();
  await expect(
    dialog.getByText("Work inside the agent remains.", { exact: false }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Confirm change" }).click();
  await expect(
    dialog.getByText("Change accepted.", { exact: false }),
  ).toBeVisible();
  const remove = recorded.find(
    (item) =>
      item.path.endsWith(`/members/${IDENTITY}`) && item.method === "DELETE",
  )!;
  expect(remove.headers["if-match"]).toBe('"5"');
});
