"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "本地时区";
  const ui = {
    state: null,
    posting: false,
    selected: "",
    confirm: null,
    connection: false,
    fingerprints: {},
    inputTouched: false,
    requestEpoch: 0,
  };
  const pendingStatuses = new Set(["scheduled", "running"]);
  const statusLabels = {
    scheduled: "已预约",
    pending: "执行中",
    running: "执行中",
    completed: "已完成",
    skipped: "已跳过",
    cancelled: "已取消",
    uncertain: "结果待核实",
    failed: "执行失败",
    succeeded: "已执行",
    no_credit: "无可用机会",
    nothing_to_reset: "无需重置",
    not_sent: "未提交",
  };

  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = String(text);
    return el;
  }

  function parseDate(value) {
    if (value === null || value === undefined || value === "") return null;
    const timestamp =
      typeof value === "number" ? (value < 1e12 ? value * 1000 : value) : value;
    const date = new Date(timestamp);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function time(value, seconds = false) {
    const date = parseDate(value);
    if (!date) return "未提供";
    const options = {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    };
    if (seconds) options.second = "2-digit";
    if (date.getFullYear() !== new Date().getFullYear())
      options.year = "numeric";
    return date.toLocaleString("zh-CN", options);
  }

  function fullTime(value) {
    const date = parseDate(value);
    return date
      ? date.toLocaleString("zh-CN", {
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        })
      : "未提供";
  }

  function relative(value) {
    const date = parseDate(value);
    if (!date) return "时间未知";
    const minutes = Math.ceil((date.getTime() - Date.now()) / 60000);
    if (minutes <= 0) return "已到时间";
    if (minutes < 60) return `${minutes} 分钟后`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24)
      return `${hours} 小时${minutes % 60 ? ` ${minutes % 60} 分` : ""}后`;
    return `${Math.floor(hours / 24)} 天${hours % 24 ? ` ${hours % 24} 小时` : ""}后`;
  }

  function shortId(value) {
    const id = String(value || "");
    return id.length > 22
      ? `${id.slice(0, 11)}…${id.slice(-7)}`
      : id || "未提供 ID";
  }

  function usable(credit) {
    const expiry = parseDate(credit.expires_at);
    return (
      credit.status === "available" &&
      credit.id &&
      !credit._expiry_invalid &&
      (!expiry || expiry.getTime() > Date.now())
    );
  }

  function credits() {
    return [...(ui.state?.credits?.items || [])].sort(
      (a, b) =>
        (parseDate(a.expires_at)?.getTime() || Infinity) -
        (parseDate(b.expires_at)?.getTime() || Infinity),
    );
  }

  function selectedCredit() {
    return credits().find(
      (credit) => credit.id === ui.selected && usable(credit),
    );
  }

  function activeSchedule(creditId = null) {
    return (ui.state?.schedules || []).some(
      (job) =>
        pendingStatuses.has(job.status) &&
        (!creditId || job.credit_id === creditId),
    );
  }

  function ambiguousOperation() {
    return (ui.state?.operations || []).some((operation) =>
      ["pending", "uncertain"].includes(operation.status),
    );
  }

  function notification(message, type = "info") {
    $("notice").textContent = message;
    $("notice").className = `notice ${type}`;
    $("notice").hidden = !message;
  }

  function connection(connected) {
    ui.connection = connected;
    $("connection").className =
      `connection ${connected ? "connected" : "disconnected"}`;
    $("connection").replaceChildren(
      node("span"),
      document.createTextNode(
        connected
          ? ui.state?.demo
            ? "离线演示已连接"
            : "本地服务已连接"
          : "本地服务未连接",
      ),
    );
    updateControls();
  }

  async function api(path, payload) {
    const epoch = payload === undefined ? ui.requestEpoch : ++ui.requestEpoch;
    const options = { credentials: "same-origin", cache: "no-store" };
    if (payload !== undefined) {
      if (!ui.state?.csrf)
        throw new Error("尚未连接本地服务，请等待连接后再操作。");
      options.method = "POST";
      options.headers = {
        "Content-Type": "application/json",
        "X-Local-Token": ui.state.csrf,
      };
      options.body = JSON.stringify(payload);
    }
    const response = await fetch(path, options);
    let result;
    try {
      result = await response.json();
    } catch {
      throw new Error("本地服务返回了无法读取的响应，请检查启动终端。");
    }
    if (payload === undefined && epoch !== ui.requestEpoch) return result;
    if (result.state) render(result.state);
    if (!response.ok || result.ok === false)
      throw new Error(result.error || `请求失败（HTTP ${response.status}）`);
    connection(true);
    return result;
  }

  let polling = false;
  async function poll() {
    if (polling || ui.posting) return;
    polling = true;
    try {
      await api("/api/state");
    } catch {
      connection(false);
    } finally {
      polling = false;
    }
  }

  async function post(path, payload, message) {
    if (ui.posting) return;
    ui.posting = true;
    updateControls();
    notification("正在处理，请稍候……");
    try {
      await api(path, payload);
      const operation = [
        "/api/consume",
        "/api/retry",
        "/api/reconcile",
      ].includes(path)
        ? (ui.state?.operations || []).find((item) =>
            payload.operation_id
              ? item.id === payload.operation_id
              : item.credit_id === payload.credit_id,
          )
        : null;
      if (operation?.status === "uncertain") {
        notification(
          "请求结果待核实。请在操作记录中先核实结果；如需重试，将沿用原请求 ID。",
          "info",
        );
      } else if (
        operation?.status === "succeeded" &&
        operation.verified === false
      ) {
        notification(
          "重置已执行，最新状态仍待核实。请使用刷新或只读核实更新结果。",
          "info",
        );
      } else if (
        ["no_credit", "nothing_to_reset"].includes(operation?.status)
      ) {
        notification(operation.message || "本次没有消耗重置机会。", "info");
      } else {
        notification(message, "");
      }
      return true;
    } catch (error) {
      notification(error.message || "操作失败，请检查本地服务。", "error");
      await poll();
      return false;
    } finally {
      ui.posting = false;
      updateControls();
    }
  }

  function updateControls() {
    const busy = ui.posting || Boolean(ui.state?.busy) || !ui.connection;
    const loaded = Boolean(ui.state?.account?.loaded);
    const selected = selectedCredit();
    const blocked = ambiguousOperation();
    $("refresh").disabled = busy || !loaded;
    $("auth-file").disabled = busy || Boolean(ui.state?.demo);
    $("reload-auth").disabled = busy || Boolean(ui.state?.demo);
    $("consume").disabled =
      busy || !selected || blocked || activeSchedule(selected?.id);
    $("schedule").disabled =
      busy ||
      !selected ||
      blocked ||
      activeSchedule(selected?.id) ||
      !parseDate(selected.expires_at);
    document.querySelectorAll("[data-schedule-cancel]").forEach((button) => {
      button.disabled = busy;
    });
    document.querySelectorAll("[data-before]").forEach((button) => {
      const date = parseDate(selected?.expires_at);
      button.disabled =
        !date ||
        date.getTime() - Number(button.dataset.before) * 60000 <= Date.now();
    });
    document.querySelectorAll("[data-operation-action]").forEach((button) => {
      button.disabled = busy;
    });
  }

  function renderAccount(state) {
    const account = state.account || {};
    $("account-heading").textContent = account.loaded
      ? "Codex 凭证已就绪"
      : "连接你的 Codex 凭证";
    $("account-badge").textContent = account.loaded ? "已导入" : "未导入";
    $("account-badge").className = account.loaded ? "badge" : "badge neutral";
    $("account-label").textContent = account.loaded
      ? account.label || "已载入 auth.json"
      : "将 auth.json 放进工具文件夹，或在这里导入。";
    const meta = [];
    if (account.account_id) meta.push(`账号 ${shortId(account.account_id)}`);
    if (account.expires_at)
      meta.push(`访问令牌到期 ${fullTime(account.expires_at)}`);
    $("account-meta").textContent =
      account.error ||
      (meta.length
        ? meta.join(" · ")
        : "凭证由本机服务读取；页面不会展示令牌。");
    $("demo-banner").hidden = !state.demo;
  }

  function windowLabel(window) {
    return `${window.name}${window.name.includes("额度") ? "" : "额度"}`;
  }

  function renderUsage(state) {
    const usage = state.usage;
    $("last-refresh").textContent = usage?.fetched_at
      ? `最近查询 ${time(usage.fetched_at, true)}`
      : "导入后点击刷新";
    $("usage-error").hidden = !usage?.error;
    $("usage-error").textContent = usage?.error
      ? `用量查询：${usage.error}`
      : "";
    if (!usage?.windows?.length) {
      const card = node("article", "card usage-card placeholder");
      const top = node("div", "card-top");
      top.append(node("h3", "", "用量窗口"), node("span", "window-icon", "◷"));
      const number = node("div", "usage-number", "—");
      number.append(node("span", "", "剩余"));
      const meter = node("div", "meter");
      meter.append(node("span"));
      card.append(
        top,
        number,
        meter,
        node(
          "p",
          "muted",
          usage
            ? "尚未取得可识别的用量窗口，请查看查询提示后重试。"
            : "导入凭证并刷新后，显示用量和自然恢复时间。",
        ),
      );
      $("usage-cards").replaceChildren(card);
      return;
    }
    const cards = usage.windows.map((window) => {
      const card = node("article", "card usage-card");
      const top = node("div", "card-top");
      top.append(
        node("h3", "", windowLabel(window)),
        node("span", "window-icon", "◷"),
      );
      const used =
        window.used_percent !== null &&
        window.used_percent !== undefined &&
        Number.isFinite(Number(window.used_percent))
          ? Math.max(0, Math.min(100, Number(window.used_percent)))
          : null;
      const remaining =
        used === null ? null : Math.round((100 - used) * 10) / 10;
      const number = node(
        "div",
        "usage-number",
        remaining === null ? "—" : `${remaining}%`,
      );
      number.append(node("span", "", "剩余"));
      const meter = node(
        "div",
        `meter${remaining !== null && remaining <= 10 ? " low" : remaining !== null && remaining <= 25 ? " warn" : ""}`,
      );
      const fill = node("span");
      fill.style.width = `${remaining ?? 0}%`;
      meter.append(fill);
      meter.setAttribute("role", "meter");
      meter.setAttribute("aria-label", `${windowLabel(window)}剩余额度`);
      meter.setAttribute("aria-valuemin", "0");
      meter.setAttribute("aria-valuemax", "100");
      if (remaining !== null)
        meter.setAttribute("aria-valuenow", String(remaining));
      const bottom = node("div", "usage-bottom");
      bottom.append(
        node(
          "span",
          "",
          used === null ? "用量未知" : `已使用 ${Math.round(used * 10) / 10}%`,
        ),
        node("span", "", "总量 100%"),
      );
      const reset = node("div", "usage-reset");
      const value = node("div", "reset-value", time(window.reset_at));
      value.append(
        node(
          "small",
          "",
          parseDate(window.reset_at)
            ? `${relative(window.reset_at)} · ${zone}`
            : "上游未提供恢复时间",
        ),
      );
      reset.append(node("span", "", "自然恢复"), value);
      card.append(top, number, meter, bottom, reset);
      return card;
    });
    $("usage-cards").replaceChildren(...cards);
  }

  function localInput(date) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function chooseCredit(id, resetTime = true) {
    ui.selected = id || "";
    const credit = selectedCredit();
    $("selected-credit").textContent = credit
      ? `重置机会 ${shortId(credit.id)}`
      : "尚未选择重置机会";
    $("selected-expiry").textContent = credit
      ? `到期 ${fullTime(credit.expires_at)} · ${zone}`
      : "查询后选择一条可用机会";
    if (resetTime && !ui.inputTouched) {
      const expiry = parseDate(credit?.expires_at);
      if (expiry) {
        const defaultTime = new Date(
          Math.max(
            expiry.getTime() - 3600000,
            Math.ceil((Date.now() + 60000) / 60000) * 60000,
          ),
        );
        $("schedule-at").value =
          defaultTime < expiry ? localInput(defaultTime) : "";
      } else $("schedule-at").value = "";
    }
    document.querySelectorAll('input[name="credit"]').forEach((radio) => {
      radio.checked = radio.value === ui.selected;
    });
    updateControls();
  }

  function renderCredits(state) {
    const data = state.credits;
    $("credit-count").textContent = data?.available_count ?? "—";
    const total = data?.available_count;
    const applicable = data?.applicable_available_count;
    $("credit-count-note").textContent =
      Number.isFinite(applicable) && total !== applicable
        ? `当前适用次数 ${applicable}（上游返回）`
        : data?.fetched_at
          ? `最近查询 ${time(data.fetched_at)}`
          : "查询后查看每次机会的到期时间";
    $("credits-error").hidden = !data?.error;
    $("credits-error").textContent = data?.error
      ? `机会查询：${data.error}`
      : "";
    const items = credits();
    if (!items.some((credit) => credit.id === ui.selected && usable(credit))) {
      ui.selected = items.find(usable)?.id || "";
      ui.inputTouched = false;
    }
    if (!items.length) {
      const empty = node("div", "empty-state");
      empty.append(
        node("span", "empty-icon", "◇"),
        node("p", "", data ? "没有可显示的重置机会" : "重置机会会显示在这里"),
        node(
          "span",
          "",
          data && Number(total) > 0
            ? "次数已返回，但未取得可操作的机会明细"
            : "查询不会消耗次数",
        ),
      );
      $("credit-list").replaceChildren(empty);
    } else {
      $("credit-list").replaceChildren(
        ...items.map((credit) => {
          const label = node("label", "credit-option");
          const radio = node("input");
          radio.type = "radio";
          radio.name = "credit";
          radio.value = credit.id || "";
          radio.disabled = !usable(credit);
          radio.checked = credit.id === ui.selected;
          radio.addEventListener("change", () => {
            ui.inputTouched = false;
            chooseCredit(credit.id);
          });
          const content = node("div", "credit-content");
          const row = node("div", "credit-row");
          const expiry = parseDate(credit.expires_at);
          const available = usable(credit);
          const remaining = expiry
            ? relative(credit.expires_at)
            : "到期时间未知";
          const status = available
            ? expiry
              ? `${remaining}到期`
              : remaining
            : credit.status === "available"
              ? "已过期"
              : { consumed: "已使用", redeemed: "已使用", expired: "已过期" }[
                  credit.status
                ] ||
                credit.status ||
                "状态未知";
          row.append(
            node("span", "credit-id", shortId(credit.id)),
            node(
              "span",
              `credit-countdown${expiry && expiry - Date.now() < 86400000 ? " urgent" : ""}`,
              status,
            ),
          );
          content.append(
            row,
            node("p", "credit-expiry", `到期 ${fullTime(credit.expires_at)}`),
          );
          label.append(radio, content);
          return label;
        }),
      );
    }
    chooseCredit(ui.selected);
  }

  function badgeClass(status) {
    return ["uncertain", "pending", "scheduled", "running", "skipped"].includes(
      status,
    )
      ? "warn"
      : status === "failed"
        ? "error"
        : ["cancelled", "no_credit", "nothing_to_reset"].includes(status)
          ? "neutral"
          : "";
  }

  function renderSchedule(state) {
    const jobs = state.schedules || [];
    $("schedules-section").hidden = jobs.length === 0;
    $("schedules-count").textContent =
      `${jobs.filter((job) => pendingStatuses.has(job.status)).length} 条待执行 / 共 ${jobs.length} 条`;
    const sorted = [...jobs].sort(
      (a, b) =>
        Number(pendingStatuses.has(b.status)) -
          Number(pendingStatuses.has(a.status)) ||
        (parseDate(a.run_at)?.getTime() || 0) -
          (parseDate(b.run_at)?.getTime() || 0),
    );
    $("schedule-list").replaceChildren(
      ...sorted.map((job) => {
        const row = node("article", "schedule-card");
        row.dataset.scheduleId = job.id;
        row.dataset.status = job.status;
        const content = node("div", "schedule-content");
        const heading = node("div", "account-heading-row");
        heading.append(
          node("h3", "", `重置机会 ${shortId(job.credit_id)}`),
          node(
            "span",
            `badge ${badgeClass(job.status)}`,
            statusLabels[job.status] || job.status,
          ),
        );
        content.append(
          heading,
          node("strong", "", `${fullTime(job.run_at)} · ${zone}`),
          node("p", "micro", `到期 ${fullTime(job.expires_at)}`),
          node(
            "p",
            "micro",
            job.message || "等待执行。请保持后台服务运行，并让 Mac 保持唤醒。",
          ),
        );
        row.append(node("span", "schedule-symbol", "◷"), content);
        if (job.status === "scheduled") {
          const cancel = node(
            "button",
            "button button-secondary",
            "取消此预约",
          );
          cancel.dataset.scheduleCancel = job.id;
          cancel.addEventListener("click", () =>
            post(
              "/api/schedule/cancel",
              { schedule_id: job.id },
              "所选预约已取消，其他预约不受影响。",
            ),
          );
          row.append(cancel);
        }
        return row;
      }),
    );
  }

  function renderOperations(state) {
    if (!state.operations?.length) {
      $("operation-list").replaceChildren(
        node(
          "div",
          "history-empty",
          "还没有重置操作。查询用量不会产生消耗记录。",
        ),
      );
      return;
    }
    const operations = [...state.operations].sort(
      (a, b) =>
        (parseDate(b.created_at)?.getTime() || 0) -
        (parseDate(a.created_at)?.getTime() || 0),
    );
    $("operation-list").replaceChildren(
      ...operations.map((operation) => {
        const row = node("article", "operation");
        const cls = badgeClass(operation.status);
        row.append(
          node(
            "span",
            `op-indicator ${cls}`,
            operation.status === "succeeded"
              ? "✓"
              : operation.status === "failed"
                ? "×"
                : "·",
          ),
        );
        const content = node("div", "op-content");
        const top = node("div", "op-top");
        const label =
          operation.status === "succeeded" && operation.verified === false
            ? "已执行，待核实"
            : statusLabels[operation.status] || operation.status;
        top.append(
          node("span", "op-title", `重置机会 ${shortId(operation.credit_id)}`),
          node("span", `badge ${cls}`, label),
        );
        content.append(
          top,
          node(
            "p",
            "op-message",
            operation.message || operation.code || "正在更新操作状态",
          ),
          node("p", "micro", `请求 ${shortId(operation.id)}`),
        );
        if (Number.isInteger(operation.windows_reset))
          content.append(
            node("p", "micro", `已重置 ${operation.windows_reset} 个额度窗口`),
          );
        if (
          operation.status === "uncertain" ||
          (operation.status === "succeeded" && operation.verified === false)
        ) {
          const actions = node("div", "op-actions");
          const reconcile = node(
            "button",
            "button button-secondary",
            "核实结果（只读）",
          );
          reconcile.dataset.operationAction = "reconcile";
          reconcile.addEventListener("click", () =>
            post(
              "/api/reconcile",
              { operation_id: operation.id },
              "已完成只读核实，请查看最新操作状态。",
            ),
          );
          actions.append(reconcile);
          if (operation.status === "uncertain") {
            const retry = node(
              "button",
              "button button-secondary",
              "使用相同请求重试",
            );
            retry.dataset.operationAction = "retry";
            retry.addEventListener("click", () => confirm("retry", operation));
            actions.append(retry);
          }
          content.append(actions);
        }
        row.append(
          content,
          node("time", "op-date", time(operation.created_at, true)),
        );
        return row;
      }),
    );
  }

  function render(state) {
    const previousAccount = ui.state?.account?.account_id;
    const changedSession = ui.state && ui.state.csrf !== state.csrf;
    const changedAccount =
      ui.state && previousAccount !== state.account.account_id;
    ui.state = state;
    if (changedAccount || changedSession) {
      if ($("confirm-dialog").open) {
        $("confirm-dialog").close();
        ui.confirm = null;
        notification(
          changedAccount
            ? "账号已更新，请重新选择重置机会。"
            : "本地服务已重启，请重新确认操作。",
        );
      }
      ui.selected = "";
      ui.inputTouched = false;
      ui.fingerprints = {};
    }
    if (ui.confirm && ui.confirm.path !== "/api/retry") {
      const credit = (state.credits?.items || []).find(
        (item) => item.id === ui.confirm.payload.credit_id,
      );
      if (!credit || !usable(credit)) {
        $("confirm-dialog").close();
        ui.confirm = null;
        notification("所选重置机会已更新，请重新查询并选择。");
      }
    }
    renderAccount(state);
    const minute = Math.floor(Date.now() / 60000);
    for (const [key, renderFn] of [
      ["usage", renderUsage],
      ["credits", renderCredits],
      ["schedules", renderSchedule],
      ["operations", renderOperations],
    ]) {
      const fingerprint =
        JSON.stringify(state[key]) +
        (key === "usage" || key === "credits" ? minute : "");
      if (fingerprint !== ui.fingerprints[key]) {
        renderFn(state);
        ui.fingerprints[key] = fingerprint;
      }
    }
    updateControls();
  }

  function showTab(which) {
    for (const name of ["now", "schedule"]) {
      const active = name === which;
      $(`tab-${name}`).classList.toggle("active", active);
      $(`tab-${name}`).setAttribute("aria-selected", String(active));
      $(`tab-${name}`).tabIndex = active ? 0 : -1;
      $(`panel-${name}`).hidden = !active;
    }
  }

  function confirm(kind, operation) {
    const credit = selectedCredit();
    if (ui.posting || !ui.connection) return;
    if (kind !== "retry" && !credit) {
      notification("请先选择一条可用重置机会。", "error");
      return;
    }
    let runAt;
    if (kind === "schedule") {
      runAt = parseDate($("schedule-at").value);
      const expires = parseDate(credit.expires_at);
      if (!runAt || runAt.getTime() <= Date.now()) {
        notification("请选择未来的执行时间。", "error");
        $("schedule-at").focus();
        return;
      }
      if (!expires || runAt >= expires) {
        notification("执行时间必须早于这条重置机会的到期时间。", "error");
        $("schedule-at").focus();
        return;
      }
    }
    const details = [];
    if (kind === "retry") {
      details.push(
        ["请求 ID", operation.id],
        ["机会", shortId(operation.credit_id)],
      );
      $("confirm-title").textContent = "使用相同请求重试";
      $("confirm-description").textContent =
        "沿用原请求 ID，重新确认这一次重置。服务不会创建新的重置请求 ID。";
      $("confirm-warning").textContent =
        "请先使用“核实结果”检查状态。仅在原请求结果不确定时重试。";
      $("confirm-submit").textContent = "确认重试原请求";
      ui.confirm = {
        path: "/api/retry",
        payload: { operation_id: operation.id },
        message: "原请求处理已结束，请以操作记录中的状态为准。",
      };
    } else {
      details.push(
        [
          "账号",
          ui.state.account?.label || shortId(ui.state.account?.account_id),
        ],
        ["所选机会", shortId(credit.id)],
        ["到期时间", fullTime(credit.expires_at)],
        ["时区", zone],
      );
      $("confirm-title").textContent =
        kind === "schedule" ? "确认预约一次重置" : "确认消耗 1 次重置机会";
      $("confirm-description").textContent =
        kind === "schedule"
          ? "在指定时间检查账号状态，并尝试消耗一次现有重置机会。此预约只执行一次。"
          : "本次操作将使用当前账号的一次现有重置机会。执行后会查询用量与剩余次数。";
      if (kind === "schedule")
        details.splice(2, 0, ["执行时间", fullTime(runAt)]);
      $("confirm-warning").textContent =
        kind === "schedule"
          ? "后台服务需运行，Mac 需保持唤醒。迟到超过 15 分钟、所选机会已过期或不满足执行条件时会跳过；只尝试使用这一次指定机会。"
          : "此操作成功后无法撤销。只尝试使用这一次指定机会；执行前会重新核实可用状态。";
      $("confirm-submit").textContent =
        kind === "schedule" ? "确认预约，消耗 1 次" : "确认消耗 1 次";
      ui.confirm =
        kind === "schedule"
          ? {
              path: "/api/schedule",
              payload: { credit_id: credit.id, run_at: runAt.toISOString() },
              message:
                "单次预约已保存。请保持后台服务运行，并让 Mac 保持唤醒。",
            }
          : {
              path: "/api/consume",
              payload: { credit_id: credit.id },
              message: "本次请求处理已结束，请以操作记录中的状态为准。",
            };
    }
    $("confirm-details").replaceChildren(
      ...details.flatMap(([label, value]) => [
        node("dt", "", label),
        node("dd", "", value),
      ]),
    );
    $("confirm-dialog").showModal();
  }

  async function importFile(file) {
    if (!file || ui.posting || $("auth-file").disabled) return;
    if (file.size > 1000 * 1024) {
      notification("文件过大，请选择有效的 auth.json（小于 1 MB）。", "error");
      return;
    }
    let auth;
    try {
      auth = JSON.parse(await file.text());
      if (!auth || typeof auth !== "object" || Array.isArray(auth))
        throw new Error("not an object");
    } catch {
      notification("无法解析 JSON，请选择有效的 auth.json。", "error");
      return;
    }
    const imported = await post(
      "/api/auth",
      { auth },
      "凭证已导入。点击“刷新用量与重置机会”获取最新状态。",
    );
    auth = null;
    if (imported) {
      ui.inputTouched = false;
      renderCredits(ui.state);
    }
    $("auth-file").value = "";
  }

  $("timezone").textContent = zone;
  $("refresh").addEventListener("click", () =>
    post("/api/refresh", {}, "查询已结束，请查看最新用量与机会明细。"),
  );
  $("consume").addEventListener("click", () => confirm("consume"));
  $("schedule").addEventListener("click", () => confirm("schedule"));
  $("reload-auth").addEventListener("click", () =>
    post(
      "/api/auth/reload",
      {},
      "已重新读取工具文件夹中的 auth.json。点击刷新查询最新用量。",
    ),
  );
  $("schedule-at").addEventListener("input", () => {
    ui.inputTouched = true;
  });
  $("auth-file").addEventListener("change", (event) =>
    importFile(event.target.files[0]),
  );
  $("confirm-submit").addEventListener("click", async () => {
    if (!ui.confirm || ui.posting) return;
    const action = ui.confirm;
    ui.confirm = null;
    $("confirm-dialog").close();
    await post(action.path, action.payload, action.message);
  });
  $("confirm-dialog").addEventListener("close", () => {
    ui.confirm = null;
  });
  for (const name of ["now", "schedule"]) {
    $(`tab-${name}`).addEventListener("click", () => showTab(name));
    $(`tab-${name}`).addEventListener("keydown", (event) => {
      if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const target =
          event.key === "Home"
            ? "now"
            : event.key === "End"
              ? "schedule"
              : name === "now"
                ? "schedule"
                : "now";
        showTab(target);
        $(`tab-${target}`).focus();
      }
    });
  }
  document.querySelectorAll("[data-before]").forEach((button) =>
    button.addEventListener("click", () => {
      const expiry = parseDate(selectedCredit()?.expires_at);
      if (!expiry) return;
      const date = new Date(
        expiry.getTime() - Number(button.dataset.before) * 60000,
      );
      if (date.getTime() <= Date.now()) {
        notification("这个快捷时间已经过去，请选择未来的执行时间。", "error");
        return;
      }
      $("schedule-at").value = localInput(date);
      ui.inputTouched = true;
    }),
  );
  const dropZone = $("account-card");
  ["dragenter", "dragover"].forEach((type) =>
    dropZone.addEventListener(type, (event) => {
      event.preventDefault();
      if (!$("auth-file").disabled) dropZone.classList.add("dragging");
    }),
  );
  ["dragleave", "drop"].forEach((type) =>
    dropZone.addEventListener(type, (event) => {
      event.preventDefault();
      dropZone.classList.remove("dragging");
    }),
  );
  dropZone.addEventListener("drop", (event) => {
    if (event.dataTransfer?.files.length)
      importFile(event.dataTransfer.files[0]);
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) poll();
  });
  poll();
  setInterval(() => {
    if (!document.hidden) poll();
  }, 3000);
})();
