import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import path from "node:path";

export async function verifyTheme(page, { screenshotDir } = {}) {
  if (screenshotDir) await mkdir(screenshotDir, { recursive: true });
  const preference = () =>
    page.locator("html").getAttribute("data-theme-preference");
  const resolved = () => page.locator("html").getAttribute("data-theme");
  const toggle = page.locator("#theme-toggle");

  // New visitors get light mode even when the operating system is dark.
  await page.emulateMedia({ colorScheme: "dark" });
  assert.equal(await preference(), "light");
  assert.equal(await resolved(), "light");
  await toggle.click();
  assert.equal(await preference(), "dark");
  await page.reload();
  await page.waitForSelector(".connection.connected");
  assert.equal(
    await resolved(),
    "dark",
    "Explicit dark mode did not survive reload",
  );
  await page.emulateMedia({ colorScheme: "light" });
  assert.equal(
    await resolved(),
    "dark",
    "System preference overrode explicit dark mode",
  );

  // Follow-system updates while the page is open, without a reload.
  await toggle.click();
  assert.equal(await preference(), "system");
  assert.equal(await resolved(), "light");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.waitForFunction(
    () => document.documentElement.dataset.theme === "dark",
  );

  for (const scheme of ["dark", "light"]) {
    if (scheme === "light") await toggle.click();
    for (const width of [320, 390, 600, 820, 1024, 1440]) {
      await page.setViewportSize({ width, height: 1050 });
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth,
        ),
        false,
        `${scheme} theme overflows at ${width}px`,
      );
      if (screenshotDir && [390, 1440].includes(width)) {
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.waitForFunction(
          () =>
            Math.abs(
              document.querySelector(".action-panels").clientHeight -
                document.getElementById("panel-now").clientHeight,
            ) < 1,
        );
        await page.screenshot({
          path: path.join(
            screenshotDir,
            `dashboard-${scheme}${width === 390 ? "-mobile" : ""}.png`,
          ),
          fullPage: true,
        });
      }
    }
    const contrast = await page.evaluate(() => {
      const luminance = (rgb) => {
        const [r, g, b] = rgb
          .match(/[\d.]+/g)
          .slice(0, 3)
          .map((value) => {
            const c = Number(value) / 255;
            return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
          });
        return r * 0.2126 + g * 0.7152 + b * 0.0722;
      };
      return [
        ".intro",
        ".credit-help",
        "#consume",
        ".usage-status",
        "#theme-toggle",
      ].map((selector) => {
        const element = document.querySelector(selector);
        let parent = element;
        while (
          parent &&
          ["rgba(0, 0, 0, 0)", "transparent"].includes(
            getComputedStyle(parent).backgroundColor,
          )
        )
          parent = parent.parentElement;
        const a = luminance(getComputedStyle(element).color);
        const b = luminance(
          getComputedStyle(parent || document.body).backgroundColor,
        );
        return {
          selector,
          ratio: (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05),
        };
      });
    });
    for (const item of contrast)
      assert.ok(
        item.ratio >= 4.5,
        `${scheme} ${item.selector} contrast is ${item.ratio.toFixed(2)}`,
      );
  }

  // Preference changes propagate between two windows using the same storage.
  await toggle.click();
  const peer = await page.context().newPage();
  try {
    await peer.goto(new URL(page.url()).origin);
    assert.equal(
      await peer.locator("html").getAttribute("data-theme-preference"),
      "dark",
    );
    await peer.locator("#theme-toggle").click();
    await page.waitForFunction(
      () => document.documentElement.dataset.themePreference === "system",
    );
  } finally {
    await peer.close();
  }
  await toggle.click();
  await page.emulateMedia({ colorScheme: "light" });
  await page.reload();
  await page.waitForSelector(".connection.connected");
  assert.equal(await preference(), "light");
}
