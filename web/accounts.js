"use strict";

// Account presentation is separate from request routing and credential parsing.
window.createAccountsView = ({ post, node, iconFrame, shortId, time }) => {
  const $ = (id) => document.getElementById(id);
  let state = null;
  let busy = false;
  let fingerprint = "";
  let action = null;
  let submitting = false;
  let pendingFocus = null;

  function rememberFocus(profileId, kind) {
    pendingFocus = { profileId, kind };
  }

  function remaining(window) {
    if (
      window.used_percent === null ||
      window.used_percent === undefined ||
      !Number.isFinite(Number(window.used_percent))
    )
      return null;
    return (
      Math.round(
        Math.max(0, Math.min(100, 100 - Number(window.used_percent))) * 10,
      ) / 10
    );
  }

  function open(actionName, profile) {
    action = { name: actionName, id: profile.id };
    $("profile-dialog-title").textContent =
      actionName === "rename" ? "重命名账号" : "移除账号";
    $("profile-dialog-description").textContent =
      actionName === "rename"
        ? `为“${profile.name}”设置便于识别的名称。`
        : `移除“${profile.name}”（${shortId(profile.account.account_id)}）及本工具保存的凭证副本和操作记录。原始导入文件不会改动。`;
    $("profile-name-field").hidden = actionName !== "rename";
    $("profile-name").value = profile.name;
    $("profile-submit").textContent =
      actionName === "rename" ? "保存名称" : "确认移除";
    $("profile-dialog-error").hidden = true;
    $("profile-dialog").showModal();
    window.QuotaMotion?.reveal($("profile-dialog"));
    (actionName === "rename" ? $("profile-name") : $("profile-cancel")).focus();
  }

  function controls(value) {
    busy = value;
    $("refresh-all").disabled =
      busy || !state?.profiles?.length || state.refreshing_all;
    $("refresh-all").setAttribute(
      "aria-busy",
      String(Boolean(state?.refreshing_all)),
    );
    $("refresh-all").querySelector("span").textContent = state?.refreshing_all
      ? "正在刷新全部"
      : "刷新全部";
    document.querySelectorAll("[data-profile-action]").forEach((button) => {
      const profile = state?.profiles?.find(
        (item) => item.id === button.dataset.profileId,
      );
      const kind = button.dataset.profileAction;
      button.disabled =
        busy ||
        !profile ||
        (kind === "refresh" && (profile.busy || !profile.account.loaded)) ||
        (kind === "remove" && (!profile.can_remove || state.demo));
    });
    $("profile-submit").disabled = busy || submitting;
    $("profile-cancel").disabled = submitting;
    $("profile-name").disabled = submitting;
    if (!busy && pendingFocus) {
      const active = document.activeElement;
      if (active === document.body || $("profile-list").contains(active)) {
        const { profileId, kind } = pendingFocus;
        const target = [
          ...$("profile-list").querySelectorAll("[data-profile-action]"),
        ].find(
          (button) =>
            button.dataset.profileId === profileId &&
            button.dataset.profileAction === kind,
        );
        (
          target ||
          $("profile-list").querySelector(
            '.profile-select[aria-pressed="true"]',
          ) ||
          $("refresh-all")
        ).focus({ preventScroll: true });
      }
      pendingFocus = null;
    }
  }

  function button(profile, kind, label, icon) {
    const el = node("button", "button button-quiet profile-action", label);
    el.type = "button";
    el.dataset.profileId = profile.id;
    el.dataset.profileAction = kind;
    el.setAttribute("aria-label", `${label} ${profile.name}`);
    if (icon) el.prepend(iconFrame(icon, "profile-action-icon"));
    el.addEventListener("click", () => {
      if (kind === "refresh") {
        rememberFocus(profile.id, kind);
        post(
          "/api/refresh",
          { profile_id: profile.id },
          `已查询“${profile.name}”，请查看账号状态。`,
        );
      } else open(kind, profile);
    });
    if (kind === "remove" && !profile.can_remove)
      el.title = "请先完成当前操作，取消待执行预约并核实未确认记录";
    return el;
  }

  function render(next) {
    state = next;
    const profiles = state.profiles;
    $("accounts").hidden = !Array.isArray(profiles);
    if (!Array.isArray(profiles)) return;
    if (action && !profiles.some((profile) => profile.id === action.id)) {
      $("profile-dialog").close();
      action = null;
    }
    const key = JSON.stringify([
      profiles,
      state.active_profile_id,
      state.demo,
      Math.floor(Date.now() / 60000),
    ]);
    if (key === fingerprint) return;
    fingerprint = key;
    const focused = document.activeElement.closest?.("[data-profile-action]");
    if (focused && $("profile-list").contains(focused))
      rememberFocus(focused.dataset.profileId, focused.dataset.profileAction);
    $("profile-count").textContent = `${profiles.length} 个`;
    const cards = profiles.map((profile) => {
      const selected = profile.id === state.active_profile_id;
      const card = node(
        "article",
        `profile-card${selected ? " selected" : ""}`,
      );
      card.dataset.profileId = profile.id;
      const select = node("button", "profile-select");
      select.type = "button";
      select.dataset.profileId = profile.id;
      select.dataset.profileAction = "select";
      select.setAttribute("aria-pressed", String(selected));
      select.setAttribute("aria-label", `切换到 ${profile.name}`);
      const identity = node("span", "profile-identity");
      identity.append(
        node("strong", "", profile.name),
        node("span", "micro", shortId(profile.account.account_id)),
      );
      select.append(
        iconFrame("account", "profile-avatar"),
        identity,
        node("span", "profile-selection", selected ? "当前操作" : "切换"),
      );
      select.addEventListener("click", () => {
        if (!selected) {
          rememberFocus(profile.id, "select");
          post(
            "/api/accounts/select",
            { profile_id: profile.id },
            `已切换到“${profile.name}”。`,
          );
        }
      });
      const error =
        profile.account.error || profile.usage?.error || profile.credits?.error;
      const expired =
        profile.account.expires_at &&
        new Date(profile.account.expires_at).getTime() <= Date.now();
      const label = profile.busy
        ? "处理中"
        : error
          ? "需要处理"
          : !profile.account.loaded
            ? "待补充凭证"
            : expired
              ? "凭证需刷新"
              : !profile.usage
                ? "待查询"
                : "已查询";
      const status = node("div", "profile-status");
      status.append(node("span", `badge ${error ? "warn" : "neutral"}`, label));
      if (profile.pending_count)
        status.append(node("span", "micro", `${profile.pending_count} 条预约`));
      if (profile.unresolved_count)
        status.append(
          node("span", "micro", `${profile.unresolved_count} 条待核实`),
        );
      const quotas = node("div", "profile-quotas");
      for (const window of profile.usage?.windows || []) {
        const value = remaining(window);
        const row = node("div", "profile-quota-row");
        const labels = node("div", "profile-quota-label");
        labels.append(
          node("span", "", window.name),
          node("strong", "", value === null ? "未知" : `${value}% 剩余`),
        );
        const meter = node("div", "meter");
        meter.setAttribute("role", "meter");
        meter.setAttribute(
          "aria-label",
          `${profile.name} ${window.name}剩余额度`,
        );
        meter.setAttribute("aria-valuemin", "0");
        meter.setAttribute("aria-valuemax", "100");
        if (value !== null) meter.setAttribute("aria-valuenow", String(value));
        const fill = node("span");
        fill.dataset.remaining = String(value ?? 0);
        fill.dataset.motionKey = `profile:${profile.id}:${window.name}`;
        meter.append(fill);
        row.append(labels, meter);
        quotas.append(row);
      }
      if (!quotas.childElementCount)
        quotas.append(node("p", "micro", "刷新后显示额度窗口与恢复状态"));
      const meta = node("div", "profile-meta micro");
      meta.append(
        node(
          "span",
          "",
          `重置机会 ${profile.credits?.available_count ?? "—"} 次`,
        ),
        node(
          "span",
          "",
          profile.usage?.fetched_at
            ? `查询于 ${time(profile.usage.fetched_at)}`
            : "尚未查询",
        ),
      );
      const actions = node("div", "profile-actions");
      actions.append(
        button(profile, "refresh", "刷新", "refresh"),
        button(profile, "rename", "重命名", "edit"),
        button(profile, "remove", "移除", "trash"),
      );
      card.append(select, status, quotas, meta);
      if (error) card.append(node("p", "profile-error micro", error));
      card.append(actions);
      return card;
    });
    $("profile-list").replaceChildren(
      ...(cards.length
        ? cards
        : [
            node(
              "p",
              "history-empty",
              "添加凭证后，可在这里查看和切换多个账号。",
            ),
          ]),
    );
    $("profile-list")
      .querySelectorAll(".meter > span")
      .forEach((fill) => {
        if (window.QuotaMotion)
          window.QuotaMotion.meter(
            fill.dataset.motionKey,
            fill,
            Number(fill.dataset.remaining),
          );
        else fill.style.width = `${fill.dataset.remaining}%`;
      });
    controls(busy);
  }

  $("refresh-all").addEventListener("click", () =>
    post("/api/refresh/all", {}, "全部账号查询已结束。"),
  );
  $("profile-cancel").addEventListener("click", () =>
    $("profile-dialog").close(),
  );
  $("profile-dialog").addEventListener("cancel", (event) => {
    if (submitting) event.preventDefault();
  });
  $("profile-dialog").addEventListener("close", () => {
    action = null;
    $("profile-name").value = "";
  });
  $("profile-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!action || busy || submitting) return;
    const current = action;
    submitting = true;
    controls(busy);
    const ok = await post(
      `/api/accounts/${current.name}`,
      {
        profile_id: current.id,
        ...(current.name === "rename"
          ? { name: $("profile-name").value.trim() }
          : {}),
      },
      current.name === "rename" ? "账号名称已保存。" : "账号已移除。",
    );
    submitting = false;
    controls(busy);
    if (ok) {
      $("profile-dialog").close();
      rememberFocus(current.id, current.name);
      controls(busy);
    } else {
      $("profile-dialog-error").textContent =
        $("notice").dataset.message || $("notice").textContent;
      $("profile-dialog-error").hidden = false;
    }
  });
  return { render, controls };
};
