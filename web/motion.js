"use strict";

// Event history is the input. seek(t) is the only animation renderer; rAF is
// only a clock. No integration, CSS transitions, or animation timers.
(() => {
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const effects = new Set();
  const controls = new WeakMap();
  const meters = new Map();
  const clamp = (v, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, v));
  const now = () => performance.now() / 1000;
  const isDisabled = (element) =>
    Boolean(element.disabled || element.control?.disabled);
  const mixColor = (from, to, amount) => {
    const channel = (hex, offset) =>
      parseInt(hex.trim().slice(offset, offset + 2), 16);
    return `rgb(${[1, 3, 5].map((offset) => Math.round(channel(from, offset) + (channel(to, offset) - channel(from, offset)) * amount)).join(" ")})`;
  };
  let frame = null;

  // Unit step response of a damped harmonic oscillator (zero initial velocity).
  function spring(t, frequency = 26, damping = 1) {
    if (t <= 0) return 0;
    if (damping === 1)
      return 1 - (1 + frequency * t) * Math.exp(-frequency * t);
    const q = Math.sqrt(1 - damping * damping);
    const phase = frequency * q * t;
    return (
      1 -
      Math.exp(-damping * frequency * t) *
        (Math.cos(phase) + (damping / q) * Math.sin(phase))
    );
  }

  class Track {
    constructor(initial) {
      this.initial = initial;
      this.events = [];
    }
    set(value, at, frequency = 26, damping = 1) {
      const previous = this.events.at(-1)?.value ?? this.initial;
      if (value !== previous)
        this.events.push({
          at,
          value,
          delta: value - previous,
          frequency,
          damping,
        });
    }
    at(t) {
      let value = this.initial;
      for (const event of this.events) {
        if (event.at > t) break;
        value +=
          event.delta *
          (reduced.matches
            ? 1
            : spring(t - event.at, event.frequency, event.damping));
      }
      return value;
    }
    active(t) {
      return (
        !reduced.matches && t < (this.events.at(-1)?.at ?? -Infinity) + 1.2
      );
    }
  }

  // Content leaves in 90 ms; the next label enters after 110 ms. Even rapid
  // reversals cannot make two labels visible at the same time.
  function contentAt(events, t, key) {
    let opacity = 0;
    let offset = 0;
    events.forEach((event, index) => {
      if (event.key !== key || event.at > t) return;
      const next = events[index + 1];
      if (reduced.matches) {
        if (!next || next.at > t) opacity = 1;
        return;
      }
      const entering = index === 0 ? 1 : clamp(spring(t - event.at - 0.11, 34));
      const age = next ? t - next.at : -1;
      const leaving =
        age <= 0 ? 1 : age >= 0.09 ? 0 : 1 - spring(age, 65) / spring(0.09, 65);
      const contribution = entering * leaving;
      opacity += contribution;
      if (contribution > 0)
        offset = age > 0 ? -3 * (1 - leaving) : 3 * (1 - entering);
    });
    return { opacity: clamp(opacity), offset };
  }

  function paintContent(el, state) {
    el.style.opacity = state.opacity.toFixed(5);
    el.style.filter =
      state.opacity > 0.9999
        ? "none"
        : `blur(${((1 - state.opacity) * 3).toFixed(3)}px)`;
    el.style.transform =
      Math.abs(state.offset) < 0.001
        ? "none"
        : `translateY(${state.offset.toFixed(3)}px)`;
    el.style.visibility = state.opacity < 0.0001 ? "hidden" : "visible";
  }

  function seek(t) {
    if (!Number.isFinite(t)) return;
    for (const effect of effects) effect.seek(t);
  }

  function tick(timestamp) {
    frame = null;
    const t = timestamp / 1000;
    seek(t);
    if ([...effects].some((effect) => effect.active(t)))
      frame = requestAnimationFrame(tick);
  }

  function wake() {
    for (const effect of effects) {
      if (effect.element && !effect.element.isConnected) effects.delete(effect);
    }
    if (frame === null) frame = requestAnimationFrame(tick);
  }

  function control(element) {
    if (controls.has(element)) return controls.get(element);
    const hover = new Track(0);
    const press = new Track(0);
    const effect = {
      element,
      hover,
      press,
      seek(t) {
        const disabled = isDisabled(element);
        const h = disabled ? 0 : clamp(hover.at(t));
        const p = disabled ? 0 : clamp(press.at(t));
        element.style.setProperty("--hover-opacity", (h * 0.06).toFixed(4));
        element.style.transform =
          p < 0.0001
            ? "none"
            : `translateY(${p.toFixed(3)}px) scale(${(1 - p * 0.018).toFixed(5)})`;
      },
      active: (t) => hover.active(t) || press.active(t),
    };
    controls.set(element, effect);
    effects.add(effect);
    return effect;
  }

  const pressed = new Set();
  const selector = ".button, .quick-times button";
  document.addEventListener("pointerover", (event) => {
    const el = event.target.closest(selector);
    if (!el || isDisabled(el) || el.contains(event.relatedTarget)) return;
    control(el).hover.set(1, now());
    wake();
  });
  document.addEventListener("pointerout", (event) => {
    const el = event.target.closest(selector);
    if (!el || el.contains(event.relatedTarget)) return;
    control(el).hover.set(0, now());
    wake();
  });
  function press(element) {
    if (!element || isDisabled(element)) return;
    const effect = control(element);
    effect.press.set(1, now(), 42);
    pressed.add(effect);
    wake();
  }
  function release() {
    for (const effect of pressed) effect.press.set(0, now(), 28, 0.96);
    pressed.clear();
    wake();
  }
  document.addEventListener("pointerdown", (event) => {
    if (event.button === 0) press(event.target.closest(selector));
  });
  document.addEventListener("pointerup", release);
  document.addEventListener("pointercancel", release);
  window.addEventListener("blur", () => {
    release();
    for (const effect of effects) effect.hover?.set(0, now());
  });
  document.addEventListener("keydown", (event) => {
    if (!event.repeat && ["Enter", " "].includes(event.key))
      press(event.target.closest(selector));
  });
  document.addEventListener("keyup", (event) => {
    if (["Enter", " "].includes(event.key)) release();
  });

  function icon(name, spinning = false) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    if (spinning) {
      svg.setAttribute("viewBox", "0 0 24 24");
      const arc = document.createElementNS(svg.namespaceURI, "path");
      arc.setAttribute("d", "M12 3a9 9 0 1 1-9 9");
      svg.append(arc);
    } else {
      const use = document.createElementNS(svg.namespaceURI, "use");
      use.setAttribute("href", `#i-${name}`);
      svg.append(use);
    }
    return svg;
  }

  const refresh = document.getElementById("refresh");
  const phases = {
    ready: {
      label: "刷新用量与重置机会",
      icon: "refresh",
      width: 226,
      tone: 0,
      radius: 14,
    },
    loading: {
      label: "正在查询",
      icon: "refresh",
      width: 148,
      tone: 0,
      radius: 24,
    },
    complete: {
      label: "已更新 · 再次刷新",
      icon: "check",
      width: 210,
      tone: 1,
      radius: 14,
    },
    partial: {
      label: "已查询 · 含异常",
      icon: "alert",
      width: 202,
      tone: 1,
      radius: 14,
    },
    error: {
      label: "查询失败 · 重试",
      icon: "alert",
      width: 198,
      tone: 0,
      radius: 14,
    },
  };
  const phaseEvents = [{ at: -Infinity, key: "ready" }];
  const width = new Track(phases.ready.width);
  const tone = new Track(phases.ready.tone);
  const radius = new Track(phases.ready.radius);
  const layers = new Map();
  refresh.classList.add("morph-button");
  refresh.replaceChildren();
  for (const [key, phase] of Object.entries(phases)) {
    const layer = document.createElement("span");
    layer.className = "refresh-state";
    layer.setAttribute("aria-hidden", "true");
    layer.append(
      icon(phase.icon, key === "loading"),
      document.createTextNode(phase.label),
    );
    layers.set(key, layer);
    refresh.append(layer);
  }
  effects.add({
    element: refresh,
    seek(t) {
      const current = phaseEvents.findLast((event) => event.at <= t);
      const disabled = refresh.disabled && current.key !== "loading";
      const theme = getComputedStyle(document.documentElement);
      const color = (token) => theme.getPropertyValue(token);
      const blend = clamp(tone.at(t));
      refresh.style.width = `${width.at(t).toFixed(3)}px`;
      refresh.style.borderRadius = `${radius.at(t).toFixed(3)}px`;
      refresh.style.backgroundColor = disabled
        ? color("--disabled")
        : mixColor(color("--accent"), color("--accent-soft"), blend);
      refresh.style.color = disabled
        ? color("--disabled-ink")
        : mixColor(color("--accent-ink"), color("--accent"), blend);
      for (const [key, layer] of layers)
        paintContent(layer, contentAt(phaseEvents, t, key));
      const loading = phaseEvents.findLast(
        (event) => event.at <= t && event.key === "loading",
      );
      const angle =
        reduced.matches || !loading ? 0 : ((t - loading.at) * 300) % 360;
      layers.get("loading").firstElementChild.style.transform =
        `rotate(${angle.toFixed(3)}deg)`;
    },
    active(t) {
      return (
        !reduced.matches &&
        (phaseEvents.at(-1).key === "loading" ||
          t < phaseEvents.at(-1).at + 1.2)
      );
    },
  });

  function refreshState(key) {
    if (!phases[key] || phaseEvents.at(-1).key === key) return;
    const at = now();
    phaseEvents.push({ at, key });
    const phase = phases[key];
    width.set(phase.width, at, 27, 0.96);
    radius.set(phase.radius, at);
    tone.set(phase.tone, at);
    refresh.setAttribute("aria-busy", String(key === "loading"));
    refresh.setAttribute(
      "aria-label",
      key === "ready" ? phase.label : `${phase.label}，刷新用量与重置机会`,
    );
    wake();
  }

  const tabList = document.querySelector(".action-tabs");
  const rail = document.createElement("span");
  const indicator = document.createElement("span");
  rail.className = "tab-indicator-rail";
  indicator.className = "tab-indicator";
  rail.setAttribute("aria-hidden", "true");
  rail.append(indicator);
  tabList.prepend(rail);
  tabList.classList.add("motion-tabs");
  const panels = ["now", "schedule"].map((name) =>
    document.getElementById(`panel-${name}`),
  );
  const panelFrame = document.createElement("div");
  panelFrame.className = "action-panels";
  panels[0].before(panelFrame);
  panelFrame.append(...panels);
  panels.forEach((panel, index) => {
    panel.hidden = false;
    panel.inert = index !== 0;
    panel.setAttribute("aria-hidden", String(index !== 0));
  });
  const left = new Track(0);
  const right = new Track(0.5);
  const height = new Track(panels[0].offsetHeight);
  const tabEvents = [{ at: -Infinity, key: "now" }];
  effects.add({
    element: tabList,
    seek(t) {
      const l = clamp(left.at(t));
      const r = clamp(right.at(t));
      indicator.style.left = `${(l * 100).toFixed(4)}%`;
      indicator.style.width = `${(Math.max(0, r - l) * 100).toFixed(4)}%`;
      panelFrame.style.height = `${Math.max(0, height.at(t)).toFixed(3)}px`;
      panels.forEach((panel) =>
        paintContent(panel, contentAt(tabEvents, t, panel.id.slice(6))),
      );
    },
    active: (t) =>
      left.active(t) ||
      right.active(t) ||
      height.active(t) ||
      (!reduced.matches && t < tabEvents.at(-1).at + 1.2),
  });

  function selectTab(key) {
    const index = key === "schedule" ? 1 : 0;
    if (tabEvents.at(-1).key === key) return;
    const at = now();
    tabEvents.push({ at, key });
    // The leading edge responds first; reversing direction swaps the springs.
    left.set(index * 0.5, at, index ? 24 : 38);
    right.set((index + 1) * 0.5, at, index ? 38 : 24);
    height.set(panels[index].offsetHeight, at, 28);
    panels.forEach((panel, i) => {
      panel.inert = i !== index;
      panel.setAttribute("aria-hidden", String(i !== index));
    });
    wake();
  }

  const panelObserver = new ResizeObserver(() => {
    const index = tabEvents.at(-1).key === "schedule" ? 1 : 0;
    height.set(panels[index].offsetHeight, now());
    wake();
  });
  panels.forEach((panel) => panelObserver.observe(panel));

  function meter(key, element, value) {
    let effect = meters.get(key);
    if (!effect) {
      const track = new Track(0);
      effect = {
        element,
        track,
        seek: (t) => {
          effect.element.style.width = `${clamp(track.at(t), 0, 100).toFixed(4)}%`;
        },
        active: (t) => track.active(t),
      };
      meters.set(key, effect);
    }
    effect.element = element;
    effect.track.set(value, now(), 16);
    effects.add(effect);
    wake();
  }

  const reveals = new WeakMap();
  function reveal(element) {
    let effect = reveals.get(element);
    if (!effect) {
      effect = {
        element,
        events: [],
        seek(t) {
          const event = effect.events.findLast((at) => at <= t);
          const value =
            reduced.matches || event === undefined
              ? 1
              : clamp(spring(t - event, 32));
          paintContent(element, { opacity: value, offset: (1 - value) * 5 });
        },
        active: (t) =>
          !reduced.matches && t < (effect.events.at(-1) ?? -Infinity) + 1.2,
      };
      reveals.set(element, effect);
    }
    effect.events.push(now());
    effects.add(effect);
    wake();
  }

  const noticeElement = document.getElementById("notice");
  const messages = [{ at: -Infinity, key: 0, message: "", type: "info" }];
  effects.add({
    element: noticeElement,
    seek(t) {
      let index = messages.findLastIndex((event) => event.at <= t);
      // A single text node is swapped only after its previous content exits.
      if (!reduced.matches && index > 0 && t < messages[index].at + 0.1)
        index -= 1;
      const event = messages[index];
      if (noticeElement.textContent !== event.message)
        noticeElement.textContent = event.message;
      noticeElement.className = `notice ${event.type}`;
      noticeElement.hidden = !event.message;
      paintContent(noticeElement, contentAt(messages, t, event.key));
    },
    active: (t) => !reduced.matches && t < messages.at(-1).at + 1.2,
  });

  function notice(message, type) {
    noticeElement.dataset.message = message;
    messages.push({ at: now(), key: messages.length, message, type });
    wake();
  }

  reduced.addEventListener("change", wake);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && frame !== null) {
      cancelAnimationFrame(frame);
      frame = null;
    } else if (!document.hidden) wake();
  });
  window.QuotaMotion = {
    seek,
    now,
    wake,
    refreshState,
    selectTab,
    meter,
    reveal,
    notice,
  };
  seek(now());
})();
