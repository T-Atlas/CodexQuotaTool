import assert from "node:assert/strict";

// These checks exercise actual input and the rendered result. They also seek
// backwards to detect accidental frame integration or overlapping content.
export async function verifyMotion(page) {
  await page.evaluate(() => document.fonts.ready);
  assert.equal(
    await page.evaluate(() =>
      [...document.fonts].some(
        (face) => face.family === "Geist" && face.status === "loaded",
      ),
    ),
    true,
  );
  const disabledImport = page.locator('label[for="auth-file"]');
  const disabledBounds = await disabledImport.boundingBox();
  await page.mouse.move(disabledBounds.x + 12, disabledBounds.y + 12);
  await page.mouse.down();
  const disabledState = await page.evaluate(() => {
    window.QuotaMotion.seek(window.QuotaMotion.now() + 0.2);
    const label = document.querySelector('label[for="auth-file"]');
    return {
      transform: getComputedStyle(label).transform,
      hover: Number(label.style.getPropertyValue("--hover-opacity")),
    };
  });
  await page.mouse.up();
  assert.deepEqual(
    disabledState,
    { transform: "none", hover: 0 },
    "Disabled file import reacts as an enabled button",
  );
  const button = await page.locator("#refresh").elementHandle();
  let release;
  let capture;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  const captured = new Promise((resolve) => {
    capture = resolve;
  });
  await page.route("**/api/refresh", async (route) => {
    capture();
    await held;
    await route.continue();
  });
  const response = page.waitForResponse("**/api/refresh");
  await page.locator("#refresh").click();
  await captured;
  assert.equal(
    await page.locator("#refresh").getAttribute("aria-busy"),
    "true",
  );
  const sample = await page.evaluate(() => {
    const motion = window.QuotaMotion;
    const t = motion.now() + 0.4;
    const snapshot = (time) => {
      motion.seek(time);
      return [
        ...document.querySelectorAll(
          "#refresh, .refresh-state, .refresh-state .icon, .meter > span",
        ),
      ].map((el) => el.getAttribute("style"));
    };
    const first = snapshot(t);
    const size = document.getElementById("refresh").offsetWidth;
    snapshot(t + 0.5);
    snapshot(t - 0.3);
    return { first, replay: snapshot(t), size };
  });
  assert.deepEqual(
    sample.replay,
    sample.first,
    "seek(t) depends on prior frames",
  );
  assert.ok(
    sample.size >= 145 && sample.size <= 152,
    "Loading button did not morph",
  );
  release();
  await response;
  await page.waitForFunction(
    () =>
      document.getElementById("refresh").getAttribute("aria-busy") === "false",
  );
  assert.equal(
    await button.evaluate((el) => el === document.getElementById("refresh")),
    true,
    "Refresh replaced its outer element",
  );
  assert.match(
    await page.locator("#refresh").getAttribute("aria-label"),
    /已更新/,
  );
  await page.unroute("**/api/refresh");

  await page.route("**/api/refresh", (route) =>
    route.fulfill({ status: 503, json: { ok: false, error: "模拟查询失败" } }),
  );
  await page.locator("#refresh").click();
  await page.waitForFunction(() =>
    document
      .getElementById("refresh")
      .getAttribute("aria-label")
      .includes("查询失败"),
  );
  assert.equal(
    await page.locator("#refresh").getAttribute("aria-busy"),
    "false",
  );
  await page.unroute("**/api/refresh");

  // Releasing the pointer outside a control must not leave it visually pressed.
  const bounds = await page.locator("#refresh").boundingBox();
  await page.mouse.move(
    bounds.x + bounds.width / 2,
    bounds.y + bounds.height / 2,
  );
  await page.mouse.down();
  const pressed = await page.evaluate(() => {
    window.QuotaMotion.seek(window.QuotaMotion.now() + 0.1);
    return document.getElementById("refresh").style.transform;
  });
  assert.notEqual(pressed, "none");
  await page.mouse.move(12, 100);
  await page.mouse.up();
  assert.equal(
    await page.evaluate(() => {
      window.QuotaMotion.seek(window.QuotaMotion.now() + 2);
      return document.getElementById("refresh").style.transform;
    }),
    "none",
  );

  await page.locator("#tab-schedule").click();
  const start = await page.evaluate(() => window.QuotaMotion.now());
  const stretched = await page.evaluate((t) => {
    window.QuotaMotion.seek(t + 0.07);
    return (
      document.querySelector(".tab-indicator").offsetWidth /
      document.querySelector(".tab-indicator-rail").offsetWidth
    );
  }, start);
  assert.ok(
    stretched > 0.53,
    "Tab leading edge did not stretch ahead of its trailing edge",
  );
  for (const name of ["now", "schedule", "now", "schedule"])
    await page.locator(`#tab-${name}`).click();
  const result = await page.evaluate((startTime) => {
    const motion = window.QuotaMotion;
    const end = motion.now() + 1.5;
    let overlap = false;
    let invalidSize = false;
    for (let t = startTime - 0.1; t < end; t += 0.008) {
      motion.seek(t);
      for (const selector of [
        ".refresh-state",
        ".action-panels > [role=tabpanel]",
      ]) {
        if (
          [...document.querySelectorAll(selector)].filter(
            (el) => Number(el.style.opacity) > 0.001,
          ).length > 1
        )
          overlap = true;
      }
      if (parseFloat(document.querySelector(".tab-indicator").style.width) < 0)
        invalidSize = true;
    }
    const capture = () =>
      [
        ...document.querySelectorAll(
          ".tab-indicator, .action-panels, [role=tabpanel]",
        ),
      ].map((el) => el.getAttribute("style"));
    motion.seek(startTime + 0.07);
    const first = capture();
    motion.seek(end);
    motion.seek(startTime + 0.07);
    const replay = capture();
    motion.seek(end);
    return {
      overlap,
      invalidSize,
      first,
      replay,
      active: document.getElementById("panel-schedule").style.opacity,
    };
  }, start);
  assert.equal(result.overlap, false, "Two content states overlap");
  assert.equal(result.invalidSize, false);
  assert.deepEqual(result.first, result.replay);
  assert.equal(Number(result.active), 1);

  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.locator("#tab-now").click();
  assert.equal(
    await page.evaluate(() => {
      window.QuotaMotion.seek(window.QuotaMotion.now());
      return document.getElementById("panel-now").style.opacity;
    }),
    "1",
  );
  assert.equal(
    await page.locator("#panel-schedule").evaluate((el) => el.inert),
    true,
  );
  assert.equal(
    await page.evaluate(() =>
      [...document.querySelectorAll("*")].some(
        (el) => getComputedStyle(el).willChange !== "auto",
      ),
    ),
    false,
  );
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.reload();
  await page.waitForSelector(".connection.connected");
}
