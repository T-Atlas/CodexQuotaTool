import assert from "node:assert/strict";
import { access } from "node:fs/promises";
import path from "node:path";

const card = (page, id) =>
  page.locator(`.profile-card[data-profile-id="${id}"]`);
const selected = (page, id) =>
  page.waitForFunction(
    (id) =>
      document.querySelector(".profile-card.selected")?.dataset.profileId ===
      id,
    id,
  );
const readState = async (base) =>
  (await (await fetch(`${base}/api/state`)).json()).state;

async function rename(page, id, name) {
  await card(page, id).locator('[data-profile-action="rename"]').click();
  await page.locator("#profile-name").fill(name);
  await page.locator("#profile-submit").click();
  await page.waitForFunction(
    () => !document.getElementById("profile-dialog").open,
  );
}

export async function verifyDemoAccounts(page, base) {
  let state = await readState(base);
  assert.equal(state.profiles.length, 2);
  const a = state.profiles.find(
    (p) => p.account.account_id === "offline-demo-account",
  ).id;
  const b = state.profiles.find(
    (p) => p.account.account_id === "offline-demo-team-account",
  ).id;
  assert.equal(await page.locator(".profile-card").count(), 2);
  assert.equal(
    await card(page, a)
      .getByRole("meter")
      .first()
      .getAttribute("aria-valuenow"),
    "4",
  );
  assert.equal(
    await card(page, b)
      .getByRole("meter")
      .first()
      .getAttribute("aria-valuenow"),
    "72",
  );
  await card(page, b).locator(".profile-select").click();
  await selected(page, b);
  await page.waitForFunction(
    (id) =>
      document.activeElement?.dataset.profileId === id &&
      document.activeElement?.dataset.profileAction === "select",
    b,
  );
  assert.equal(
    await page.locator(".usage-number strong").first().innerText(),
    "72",
  );
  await rename(page, b, "<b>团队 & 备用</b>");
  assert.equal(await card(page, b).locator("b").count(), 0);
  await page.reload();
  await page.waitForSelector(".connection.connected");
  await selected(page, b);
  assert.equal(
    await page.locator("#account-label").innerText(),
    "<b>团队 & 备用</b>",
  );

  const refreshed = page.waitForResponse("**/api/refresh");
  await card(page, a).locator('[data-profile-action="refresh"]').click();
  const request = (await refreshed).request().postDataJSON();
  assert.equal(request.profile_id, a);
  assert.equal(request.view_profile_id, b);
  await page.waitForFunction(
    () => !document.getElementById("refresh-all").disabled,
  );
  assert.equal(
    await page
      .locator(".profile-card.selected")
      .getAttribute("data-profile-id"),
    b,
  );
  const all = page.waitForResponse("**/api/refresh/all");
  await page.locator("#refresh-all").click();
  assert.deepEqual((await (await all).json()).refresh_summary, {
    total: 2,
    succeeded: 2,
    failed: 0,
  });
  await page.waitForFunction(
    () => !document.getElementById("refresh-all").disabled,
  );

  const peer = await page.context().newPage();
  try {
    await peer.goto(base);
    await peer.waitForSelector(".connection.connected");
    await card(peer, a).locator(".profile-select").click();
    await selected(peer, a);
    const poll = page.waitForResponse((response) =>
      response.url().includes(`/api/state?profile_id=${b}`),
    );
    await page.evaluate(() =>
      document.dispatchEvent(new Event("visibilitychange")),
    );
    await poll;
    assert.equal(
      await page
        .locator(".profile-card.selected")
        .getAttribute("data-profile-id"),
      b,
      "Another tab changed this tab's operation account",
    );
  } finally {
    await peer.close();
  }
  await rename(page, b, "团队演示账号（模拟数据）");
  await card(page, a).locator(".profile-select").click();
  await selected(page, a);
  state = await readState(base);
  assert.equal(state.active_profile_id, a);
  assert.equal(
    await card(page, a).locator('[data-profile-action="remove"]').isDisabled(),
    true,
  );
  await page.locator(".brand").click();
}

export async function verifySavedAccounts(page, base, dataRoot, pasted) {
  let state = await readState(base);
  const a = state.profiles.find(
    (p) => p.account.account_id === "pasted-test-account",
  ).id;
  const b = state.profiles.find(
    (p) => p.account.account_id === "uploaded-test-account",
  ).id;
  assert.equal(state.profiles.length, 2);
  await card(page, a).locator(".profile-select").click();
  await selected(page, a);
  await rename(page, a, "主账号");
  await page.reload();
  await page.waitForSelector(".connection.connected");
  assert.equal(await page.locator("#account-label").innerText(), "主账号");
  await page.locator("#paste-auth").click();
  await page.locator("#auth-json").fill(
    JSON.stringify({
      ...pasted,
      tokens: { ...pasted.tokens, access_token: "fake-updated-pasted-access" },
    }),
  );
  await page.locator("#auth-submit").click();
  await page.waitForFunction(
    () => !document.getElementById("auth-dialog").open,
  );
  assert.equal(
    (await readState(base)).profiles.length,
    2,
    "Updating a credential created a duplicate account",
  );
  assert.equal(await page.locator("#account-label").innerText(), "主账号");

  await page.locator("#auth-file").setInputFiles([
    {
      name: "third.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({
          access_token: "fake-third-account-access",
          account_id: "third-test-account",
          label: "第三个测试账号",
        }),
      ),
    },
    {
      name: "invalid.json",
      mimeType: "application/json",
      buffer: Buffer.from("{invalid"),
    },
  ]);
  await page.waitForFunction(() =>
    document.getElementById("notice").textContent.includes("1 个失败"),
  );
  state = await readState(base);
  assert.equal(state.profiles.length, 3);
  const c = state.profiles.find(
    (p) => p.account.account_id === "third-test-account",
  ).id;
  assert.equal(state.active_profile_id, c);
  assert.equal(await page.locator(".profile-card").count(), 3);
  for (const key of [c, b, a]) {
    await card(page, key).locator('[data-profile-action="remove"]').click();
    await page.locator("#profile-submit").click();
    await page.waitForFunction(
      () => !document.getElementById("profile-dialog").open,
    );
    await page.waitForFunction(
      (key) =>
        !document.querySelector(`.profile-card[data-profile-id="${key}"]`),
      key,
    );
    await assert.rejects(
      access(path.join(dataRoot, "data", "accounts", key, "auth.json")),
    );
  }
  assert.equal((await readState(base)).profiles.length, 0);
  assert.equal(await page.locator("#refresh-all").isDisabled(), true);
  assert.equal(await page.locator("#paste-auth").isEnabled(), true);
  await page.reload();
  await page.waitForSelector(".connection.connected");
  assert.equal(await page.locator(".profile-card").count(), 0);
  assert.deepEqual(await page.evaluate(() => Object.keys(localStorage)), [
    "codex-quota-theme",
  ]);
}
