"use strict";

// Resolve before stylesheets are painted to avoid a light flash on dark pages.
(() => {
  const key = "codex-quota-theme";
  const modes = ["light", "dark", "system"];
  const names = { light: "浅色", dark: "深色", system: "跟随系统" };
  const icons = { light: "sun", dark: "moon", system: "monitor" };
  const system = matchMedia("(prefers-color-scheme: dark)");
  let preference = "light";
  try {
    const saved = localStorage.getItem(key);
    if (modes.includes(saved)) preference = saved;
  } catch {
    // Appearance remains usable when browser storage is unavailable.
  }

  function apply() {
    const resolved =
      preference === "system"
        ? system.matches
          ? "dark"
          : "light"
        : preference;
    document.documentElement.dataset.theme = resolved;
    document.documentElement.dataset.themePreference = preference;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = resolved === "dark" ? "#141922" : "#f8fafc";
    const button = document.getElementById("theme-toggle");
    if (button) {
      const next = modes[(modes.indexOf(preference) + 1) % modes.length];
      const label = `外观：${names[preference]}，切换为${names[next]}`;
      button.title = label;
      button.setAttribute("aria-label", label);
      button
        .querySelector("use")
        .setAttribute("href", `#i-${icons[preference]}`);
    }
    window.QuotaMotion?.wake();
  }

  apply();
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.getElementById("theme-toggle").addEventListener("click", () => {
      preference = modes[(modes.indexOf(preference) + 1) % modes.length];
      try {
        localStorage.setItem(key, preference);
      } catch {
        // Keep the chosen theme for this page even if it cannot be persisted.
      }
      apply();
    });
  });
  system.addEventListener("change", () => {
    if (preference === "system") apply();
  });
  window.addEventListener("storage", (event) => {
    if (event.key !== key && event.key !== null) return;
    preference = modes.includes(event.newValue) ? event.newValue : "light";
    apply();
  });
})();
