import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dataRoot = await mkdtemp(path.join(os.tmpdir(), "codex-quota-ui-"));
const socket = net.createServer();
socket.listen(0, "127.0.0.1");
await once(socket, "listening");
const port = socket.address().port;
await new Promise((resolve) => socket.close(resolve));
const base = `http://127.0.0.1:${port}`;
let server;
let browser;

async function state() {
  const response = await fetch(`${base}/api/state`);
  return (await response.json()).state;
}

async function startServer(demo) {
  server = spawn(
    "python3",
    [
      "server.py",
      ...(demo ? ["--demo"] : []),
      "--root",
      dataRoot,
      "--port",
      String(port),
    ],
    { cwd: root, stdio: ["ignore", "pipe", "pipe"] },
  );
  let serverOutput = "";
  server.stdout.on("data", (chunk) => {
    serverOutput += chunk;
  });
  server.stderr.on("data", (chunk) => {
    serverOutput += chunk;
  });
  let ready = false;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (server.exitCode !== null) throw new Error(serverOutput);
    try {
      ready = (await fetch(`${base}/api/health`)).ok;
    } catch {
      /* Wait for the local listener. */
    }
    if (ready) break;
    await delay(100);
  }
  assert.ok(ready, serverOutput || "Local server did not start");
}

try {
  await startServer(true);
  browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1050 },
    timezoneId: "Asia/Shanghai",
  });
  page.setDefaultTimeout(15_000);
  const errors = [];
  const consumes = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (request.url().endsWith("/api/consume")) consumes.push(request);
  });
  await page.goto(base);
  await page.waitForSelector(".connection.connected");
  assert.equal(await page.locator("#demo-banner").isVisible(), true);
  assert.equal(await page.locator("#auth-file").isDisabled(), true);
  assert.equal(await page.locator("#paste-auth").isDisabled(), true);
  assert.equal(await page.locator("#credit-count").innerText(), "2");

  await page.locator("#tab-schedule").click();
  await page.locator("#schedule").click();
  await page.locator("#confirm-submit").click();
  await page.waitForSelector('[data-status="scheduled"]');
  await page.locator('input[name="credit"][value="demo-credit-2"]').check();
  const runAt = await page.evaluate(() => {
    const date = new Date(Date.now() + 7_000);
    const pad = (value) => String(value).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  });
  await page.locator("#schedule-at").fill(runAt);
  await page.locator("#schedule").click();
  await page.locator("#confirm-submit").click();
  await page.waitForFunction(
    () => document.querySelectorAll('[data-status="scheduled"]').length === 2,
  );
  await page
    .locator(".schedule-card")
    .filter({ hasText: "demo-credit-1" })
    .locator("[data-schedule-cancel]")
    .click();
  await page.waitForSelector('[data-status="cancelled"]');
  await page.waitForSelector('[data-status="completed"]', { timeout: 25_000 });
  let snapshot = await state();
  assert.equal(snapshot.operations.length, 1);
  assert.equal(snapshot.operations[0].credit_id, "demo-credit-2");
  assert.equal(snapshot.credits.available_count, 1);

  await page.locator("#tab-now").click();
  for (const change of ["account", "session"]) {
    await page.locator("#consume").click();
    const changed = structuredClone(await state());
    if (change === "account")
      changed.account.account_id = "another-demo-account";
    else changed.csrf = "another-local-session";
    await page.route("**/api/state", (route) =>
      route.fulfill({ json: { ok: true, state: changed } }),
    );
    await page.evaluate(() =>
      document.dispatchEvent(new Event("visibilitychange")),
    );
    await page.waitForFunction(
      () => !document.querySelector("#confirm-dialog").open,
    );
    assert.equal(consumes.length, 0);
    await page.unroute("**/api/state");
    await page.reload();
    await page.waitForSelector(".connection.connected");
  }

  const stale = structuredClone(await state());
  stale.credits.available_count = 99;
  let release;
  let captured;
  const capturedRequest = new Promise((resolve) => {
    captured = resolve;
  });
  await page.route("**/api/state", async (route) => {
    captured();
    await new Promise((resolve) => {
      release = resolve;
    });
    await route.fulfill({ json: { ok: true, state: stale } });
  });
  await page.evaluate(() =>
    document.dispatchEvent(new Event("visibilitychange")),
  );
  await capturedRequest;
  const refreshResponse = page.waitForResponse("**/api/refresh");
  await page.locator("#refresh").click();
  await refreshResponse;
  const staleResponse = page.waitForResponse("**/api/state");
  release();
  await (await staleResponse).finished();
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      ),
  );
  assert.equal(await page.locator("#credit-count").innerText(), "1");
  await page.unroute("**/api/state");

  await page.locator("#consume").click();
  await page.locator("#confirm-submit").click();
  await page.waitForFunction(() =>
    document.querySelector("#operation-list").textContent.includes("无需重置"),
  );
  snapshot = await state();
  assert.equal(snapshot.operations[0].status, "nothing_to_reset");
  assert.equal(snapshot.credits.available_count, 1);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );

  server.kill("SIGTERM");
  await once(server, "exit");
  await startServer(false);
  await page.goto(base);
  await page.waitForSelector(".connection.connected");
  const imports = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/api/auth")) imports.push(request);
  });
  await page.locator("#paste-auth").click();
  assert.equal(
    await page
      .locator("#auth-json")
      .evaluate((el) => el === document.activeElement),
    true,
  );
  assert.equal(await page.locator("#auth-submit").isDisabled(), true);
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
  for (const [text, message] of [
    ['{"access_token":', "JSON 格式错误"],
    ["[]", "JSON 对象"],
    ["null", "JSON 对象"],
    [JSON.stringify({ note: "中".repeat(350_000) }), "内容过大"],
  ]) {
    await page.locator("#auth-json").fill(text);
    await page.locator("#auth-submit").click();
    await page.waitForFunction(
      (expected) =>
        document
          .querySelector("#auth-json-error")
          .textContent.includes(expected),
      message,
    );
    assert.equal(imports.length, 0);
    assert.equal(
      await page.locator("#auth-dialog").evaluate((el) => el.open),
      true,
    );
  }
  await page.locator("#auth-json").fill("{}");
  await page.locator("#auth-submit").click();
  await page.waitForFunction(() =>
    document
      .querySelector("#auth-json-error")
      .textContent.includes("access_token"),
  );
  assert.equal(imports.length, 1);
  assert.equal(await page.locator("#auth-json").inputValue(), "{}");

  const pasted = {
    tokens: {
      access_token: "pasted-test-access",
      refresh_token: "pasted-test-refresh",
      account_id: "pasted-test-account",
    },
  };
  await page.locator("#auth-json").fill(JSON.stringify(pasted, null, 2));
  await page.locator("#auth-submit").click();
  await page.waitForFunction(
    () => !document.querySelector("#auth-dialog").open,
  );
  assert.equal(imports.length, 2);
  assert.equal(await page.locator("#auth-json").inputValue(), "");
  assert.equal((await state()).account.account_id, "pasted-test-account");
  assert.deepEqual(
    JSON.parse(await readFile(path.join(dataRoot, "auth.json"), "utf8")),
    pasted,
  );
  assert.equal(
    (await page.locator("body").innerText()).includes("pasted-test-access"),
    false,
  );
  assert.equal(
    await page.evaluate(() => localStorage.length + sessionStorage.length),
    0,
  );

  await page.locator("#paste-auth").click();
  await page.locator("#auth-json").fill(JSON.stringify(pasted));
  await page.keyboard.press("Escape");
  await page.waitForFunction(
    () => !document.querySelector("#auth-dialog").open,
  );
  assert.equal(await page.locator("#auth-json").inputValue(), "");
  assert.equal(imports.length, 2);
  await page.locator("#paste-auth").click();
  await page.locator("#auth-json").fill(JSON.stringify(pasted));
  await page.locator("#auth-cancel").click();
  assert.equal(await page.locator("#auth-json").inputValue(), "");

  const uploaded = {
    access_token: "uploaded-test-access",
    account_id: "uploaded-test-account",
  };
  await page.locator("#auth-file").setInputFiles({
    name: "auth.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(uploaded)),
  });
  await page.waitForFunction(() =>
    document.querySelector("#account-meta").textContent.includes("uploaded-te"),
  );
  assert.equal((await state()).account.account_id, "uploaded-test-account");
  assert.deepEqual(
    JSON.parse(await readFile(path.join(dataRoot, "auth.json"), "utf8")),
    uploaded,
  );
  assert.equal(imports.length, 3);
  assert.equal(await page.locator("#auth-file").inputValue(), "");
  assert.deepEqual(errors, []);
  console.log(
    "Browser checks passed: pasted and uploaded credentials, validation, scheduling, cancellation, confirmation context, polling order, demo isolation, mobile layout.",
  );
} finally {
  await browser?.close();
  if (server?.exitCode === null) {
    server.kill("SIGTERM");
    await once(server, "exit");
  }
  await rm(dataRoot, { recursive: true, force: true });
}
