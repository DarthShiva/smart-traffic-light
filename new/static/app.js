/*
 * app.js
 * ------
 * Dashboard front end. It consumes exactly one thing: the canonical
 * snapshot from GET /api/data, pushed continuously over GET /api/stream.
 * There is no polling loop for state, no second state model, and no
 * transformation of the server's schema - field names here are the field
 * names in system_state.py.
 *
 * The countdown is interpolated locally from (phase_deadline -
 * last_update), both of which come from the server's clock in the same
 * message. That makes it immune to client/server clock skew, and it stays
 * smooth between the one-second state pushes.
 */
(() => {
  "use strict";

  const FRAME_INTERVAL_MS = 130;   // ~7.5 fps per lane while visible
  const FRAME_RETRY_MS = 1500;
  const LINK_STALE_MS = 6000;

  const el = (id) => document.getElementById(id);
  const cameraGrid = el("camera-grid");
  const countGrid = el("count-grid");
  const manualButtons = el("manual-lane-buttons");
  const template = el("camera-template");

  /** @type {object|null} */ let snapshot = null;
  /** @type {EventSource|null} */ let source = null;
  let builtLanes = null;
  let lastMessageAt = 0;
  let countdownBase = { remaining: null, at: 0 };
  const editing = new Set();
  const cards = new Map();     // lane -> {card, img, timer, ...}
  const frameTimers = new Map();

  // ------------------------------------------------------------- utils --
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));

  const fmtClock = (seconds) => {
    if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
    const s = Math.max(0, Math.round(Number(seconds)));
    return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  };

  const setText = (node, text) => { if (node) node.textContent = text; };

  function showAlert(message) {
    const bar = el("alert-bar");
    if (!bar) return;
    if (!message) { bar.hidden = true; bar.textContent = ""; return; }
    bar.textContent = message;              // textContent, never innerHTML
    bar.hidden = false;
    clearTimeout(showAlert._timer);
    showAlert._timer = setTimeout(() => { bar.hidden = true; }, 6000);
  }

  async function api(url, options = {}) {
    const response = await fetch(url, options);
    let data = null;
    try { data = await response.json(); } catch (_) { /* non-JSON body */ }
    if (!response.ok || (data && data.ok === false)) {
      throw new Error((data && data.error) || `${response.status} ${response.statusText}`);
    }
    return data || {};
  }

  const postJSON = (url, body) => api(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });

  const debounce = (fn, delay) => {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); };
  };

  // -------------------------------------------------------- frame pump --
  function scheduleFrame(lane, delay) {
    clearTimeout(frameTimers.get(lane));
    frameTimers.set(lane, setTimeout(() => pumpFrame(lane), delay));
  }

  function pumpFrame(lane) {
    const entry = cards.get(lane);
    if (!entry) return;
    const laneState = snapshot && snapshot.lanes ? snapshot.lanes[lane] : null;
    const streamable = laneState && laneState.connected && laneState.has_frame;
    if (!streamable || document.visibilityState !== "visible") {
      scheduleFrame(lane, FRAME_RETRY_MS);
      return;
    }
    // Cache-buster: the endpoint is no-store, but a stale bfcache/proxy
    // copy would otherwise freeze the feed.
    entry.img.src = `/api/frame/${encodeURIComponent(lane)}?t=${Date.now()}`;
  }

  // ------------------------------------------------------------- build --
  function buildUI(state) {
    const lanes = state.lane_order || [];
    if (builtLanes && builtLanes.join() === lanes.join()) return;
    builtLanes = lanes.slice();
    cameraGrid.textContent = "";
    countGrid.textContent = "";
    manualButtons.textContent = "";
    cards.clear();

    lanes.forEach((lane) => {
      const laneState = state.lanes[lane] || {};
      const label = laneState.label || lane;

      const frag = template.content.cloneNode(true);
      const card = frag.querySelector(".camera-card");
      card.dataset.lane = lane;
      setText(card.querySelector(".camera-name"), `${label} · ${lane}`.toUpperCase());

      const img = card.querySelector(".camera-video");
      img.alt = `Live feed for ${label} (${lane})`;
      img.addEventListener("load", () => {
        card.classList.remove("missing-video");
        scheduleFrame(lane, FRAME_INTERVAL_MS);
      });
      img.addEventListener("error", () => {
        card.classList.add("missing-video");
        scheduleFrame(lane, FRAME_RETRY_MS);
      });

      const select = card.querySelector(".video-select");
      select.addEventListener("focus", () => editing.add(`video:${lane}`));
      select.addEventListener("blur", () => editing.delete(`video:${lane}`));
      select.addEventListener("change", async () => {
        const value = select.value === "__none__" ? null : select.value;
        select.disabled = true;
        try {
          const result = await postJSON(`/api/lane/${encodeURIComponent(lane)}/video`, { video: value });
          showAlert(result.message || `${label}: video updated`);
        } catch (error) {
          showAlert(`${label}: ${error.message}`);
        } finally {
          select.disabled = false;
          editing.delete(`video:${lane}`);
        }
      });

      card.querySelector(".lane-recalibrate").addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          const result = await postJSON(`/api/lane/${encodeURIComponent(lane)}/recalibrate`);
          showAlert(result.message || `${label}: recalibrated`);
        } catch (error) {
          showAlert(`${label}: ${error.message}`);
        } finally {
          button.disabled = false;
        }
      });

      cameraGrid.appendChild(frag);
      cards.set(lane, { card, img, select });

      // ---- operator count control -------------------------------------
      const countCard = document.createElement("div");
      countCard.className = "count-control";
      countCard.dataset.lane = lane;

      const id = document.createElement("div");
      id.className = "count-id";
      const idName = document.createElement("span");
      idName.textContent = label;
      const idSub = document.createElement("small");
      idSub.className = "count-mode";
      idSub.textContent = "MANUAL DEMAND";
      id.append(idName, idSub);

      const wrap = document.createElement("div");
      wrap.className = "count-input-wrap";
      const input = document.createElement("input");
      input.className = "count-input";
      input.type = "number";
      input.min = "0";
      input.max = "999";
      input.value = "0";
      input.setAttribute("aria-label", `Vehicle count for ${label}`);
      const unit = document.createElement("span");
      unit.textContent = "VEHICLES";
      wrap.append(input, unit);

      const slider = document.createElement("input");
      slider.className = "count-slider";
      slider.type = "range";
      slider.min = "0";
      slider.max = "100";
      slider.value = "0";
      slider.setAttribute("aria-label", `Vehicle slider for ${label}`);

      countCard.append(id, wrap, slider);
      countGrid.appendChild(countCard);

      const apply = (value) => {
        const n = clamp(Math.round(Number(value) || 0), 0, 999);
        input.value = String(n);
        slider.value = String(Math.min(100, n));
        return n;
      };
      [input, slider].forEach((node) => {
        node.addEventListener("focus", () => editing.add(`count:${lane}`));
        node.addEventListener("blur", () => editing.delete(`count:${lane}`));
        node.addEventListener("input", () => pushCount(lane, apply(node.value)));
      });

      // ---- manual override button -------------------------------------
      const manualBtn = document.createElement("button");
      manualBtn.className = "lane-btn";
      manualBtn.dataset.lane = lane;
      manualBtn.textContent = label;
      manualBtn.addEventListener("click", () => setManualLane(lane));
      manualButtons.appendChild(manualBtn);

      scheduleFrame(lane, 100);
    });

    const allRed = document.createElement("button");
    allRed.className = "lane-btn all-red";
    allRed.dataset.lane = "";
    allRed.textContent = "ALL RED";
    allRed.addEventListener("click", () => setManualLane(null));
    manualButtons.appendChild(allRed);
  }

  const pushCount = debounce((lane, value) => {
    postJSON("/api/counts", { [lane]: value }).catch((error) => showAlert(error.message));
  }, 250);

  async function setManualLane(lane) {
    try {
      await postJSON("/api/manual/set_lane", { lane });
      showAlert(lane ? `Manual: ${lane} green` : "Manual: all red");
    } catch (error) {
      showAlert(error.message);
    }
  }

  // ------------------------------------------------------------ render --
  function render(state) {
    snapshot = state;
    buildUI(state);

    const lanes = state.lane_order || [];
    const active = state.active_lane;
    const isGreen = state.phase === "green";

    setText(el("cycle-number"), state.cycle_number ?? "—");
    setText(el("signal-string"), state.signal_string || "RRRR");

    // ---- countdown base ----------------------------------------------
    countdownBase = (state.phase_deadline === null || state.phase_deadline === undefined)
      ? { remaining: null, at: performance.now() }
      : { remaining: Math.max(0, state.phase_deadline - state.last_update), at: performance.now() };
    tickCountdown();

    // ---- engine / decision -------------------------------------------
    const decision = state.decision || {};
    const activeLabel = active ? (state.lanes[active] || {}).label || active : null;
    setText(el("priority-lane"), isGreen && activeLabel ? `${activeLabel} · ${active}` : "ALL RED");
    setText(el("priority-reason"), decision.reason || "Waiting for the first phase…");
    setText(el("priority-duration"),
      state.phase_total_sec === null || state.phase_total_sec === undefined
        ? (state.mode === "manual" ? "HOLD" : "—")
        : `${Number(state.phase_total_sec).toFixed(1)}s`);
    const nextLane = decision.next_lane;
    setText(el("next-lane"), nextLane ? ((state.lanes[nextLane] || {}).label || nextLane) : "—");
    setText(el("decision-rule"), decision.rule
      ? `Rule: ${decision.rule} · all-red clearance ${state.config.all_red_sec}s · ${state.config.protocol}`
      : "");

    const engine = el("engine-state");
    if (engine) {
      engine.textContent = state.mode === "auto" ? "AUTO · RUNNING" : "MANUAL · OPERATOR";
      engine.dataset.state = state.mode === "auto" ? "live" : "manual";
    }

    // ---- mode buttons -------------------------------------------------
    document.querySelectorAll(".mode-btn").forEach((button) => {
      button.classList.toggle("active", button.dataset.mode === state.mode);
      button.setAttribute("aria-pressed", String(button.dataset.mode === state.mode));
    });
    const manual = state.mode === "manual";
    manualButtons.querySelectorAll(".lane-btn").forEach((button) => {
      button.disabled = !manual;
      const target = button.dataset.lane || null;
      button.classList.toggle("active", manual && (target || null) === (active || null));
    });
    const recalcBtn = el("btn-recalculate");
    if (recalcBtn) recalcBtn.disabled = manual;
    setText(el("operator-message"), manual
      ? "Manual mode: the selected lane holds green until you change it. The countdown is disabled by design."
      : "Auto mode: lanes rotate 1 → 2 → 3 → 4; vehicle counts set the green duration only.");

    // ---- per-lane cards ------------------------------------------------
    lanes.forEach((lane) => {
      const laneState = state.lanes[lane] || {};
      const entry = cards.get(lane);
      if (!entry) return;
      const { card } = entry;
      const green = laneState.signal === "G";

      card.classList.toggle("active", green);
      card.classList.toggle("missing-video", !laneState.connected);
      setText(card.querySelector(".feed-count"), laneState.count ?? 0);
      setText(card.querySelector(".feed-source"),
        laneState.count_source === "vision" ? "COUNTED BY DETECTOR" : "MANUAL INPUT");
      setText(card.querySelector(".feed-duration"), `${laneState.green_recommendation_sec}s`);
      setText(card.querySelector(".feed-status"), (laneState.status || "").toUpperCase());
      setText(card.querySelector(".feed-fps"),
        laneState.connected ? `${laneState.fps} fps` : "OFFLINE");
      setText(card.querySelector(".feed-signal"), green ? "GREEN" : "RED");
      card.querySelector(".feed-signal-dot").className = `feed-signal-dot ${green ? "green" : "red"}`;
      setText(card.querySelector(".live-text"), laneState.connected ? "LIVE" : "NO SIGNAL");
      card.querySelector(".live-tag").classList.toggle("offline", !laneState.connected);
      setText(card.querySelector(".missing-detail"),
        laneState.message || (laneState.assigned_video ? "Waiting for frames…" : "No video assigned to this lane."));

      updateVideoSelect(entry.select, lane, state.videos || [], laneState.assigned_video);

      const control = countGrid.querySelector(`.count-control[data-lane="${CSS.escape(lane)}"]`);
      if (control) {
        const input = control.querySelector(".count-input");
        const slider = control.querySelector(".count-slider");
        const visionCounted = laneState.count_source === "vision";
        input.disabled = visionCounted;
        slider.disabled = visionCounted;
        control.classList.toggle("locked", visionCounted);
        setText(control.querySelector(".count-mode"),
          visionCounted ? "DETECTOR — SLIDER LOCKED" : "MANUAL DEMAND");
        if (!editing.has(`count:${lane}`)) {
          const shown = visionCounted ? laneState.count : laneState.manual_count;
          if (document.activeElement !== input) input.value = String(shown ?? 0);
          if (document.activeElement !== slider) slider.value = String(Math.min(100, shown ?? 0));
        }
      }
    });

    // ---- intersection map ---------------------------------------------
    document.querySelectorAll(".signal-node").forEach((node) => node.classList.remove("active"));
    if (active && isGreen) {
      const number = (state.lanes[active] || {}).number;
      const node = document.querySelector(`.node-${number}`);
      if (node) node.classList.add("active");
    }
    setText(el("core-lane"), activeLabel && isGreen ? activeLabel : "—");
    setText(el("core-phase"), isGreen ? "GREEN" : "ALL RED");

    renderDecisionBars(state);
    renderHardware(state);
    renderDetector(state);
    renderComparison(state.comparison);
  }

  function updateVideoSelect(select, lane, videos, assigned) {
    if (editing.has(`video:${lane}`) || select.disabled) return;
    const wanted = ["__none__", ...videos].join("|");
    if (select.dataset.options !== wanted) {
      select.textContent = "";
      const none = document.createElement("option");
      none.value = "__none__";
      none.textContent = "— no video —";
      select.appendChild(none);
      videos.forEach((name) => {
        const option = document.createElement("option");
        option.value = name;                 // set as value/text, never as HTML
        option.textContent = name;
        select.appendChild(option);
      });
      select.dataset.options = wanted;
    }
    const target = assigned || "__none__";
    if (select.value !== target) select.value = target;
  }

  function renderDecisionBars(state) {
    const root = el("decision-bars");
    if (!root) return;
    const lanes = state.lane_order || [];
    const peak = Math.max(1, ...lanes.map((lane) => Number(state.lanes[lane].count) || 0));
    root.textContent = "";
    lanes.forEach((lane) => {
      const laneState = state.lanes[lane];
      const count = Number(laneState.count) || 0;
      const row = document.createElement("div");
      row.className = "dbar";
      const name = document.createElement("span");
      name.textContent = laneState.label;
      const track = document.createElement("div");
      const fill = document.createElement("i");
      fill.style.width = `${Math.round((count / peak) * 100)}%`;
      track.appendChild(fill);
      const value = document.createElement("b");
      value.textContent = `${count} · ${laneState.green_recommendation_sec}s`;
      row.append(name, track, value);
      root.appendChild(row);
    });
  }

  function renderDetector(state) {
    const vision = state.vision || {};
    const label = el("detector-label");
    if (!label) return;
    label.textContent = vision.detector_available
      ? `DETECTOR ${String(vision.detector).toUpperCase()}`
      : `DETECTOR OFF · ${String(vision.detector_status || "unavailable").toUpperCase()}`;
  }

  function renderHardware(state) {
    const serial = state.serial || {};
    const pill = el("hw-pill");
    const text = el("hw-pill-text");
    const label = el("hw-state-label");
    if (serial.connected) {
      pill.dataset.state = "connected";
      setText(text, `ARDUINO · ${serial.port}`);
      setText(label, "CONNECTED");
    } else {
      pill.dataset.state = "simulated";
      setText(text, serial.pyserial_available ? "NO HARDWARE CONNECTED" : "PYSERIAL NOT INSTALLED");
      setText(label, "NO HARDWARE");
    }
    setText(el("hw-last-sent"), serial.last_sent || "—");
    // last_sent_counter can legitimately be 0, so check for null/undefined
    // rather than falsiness - "0" must not be displayed as "—".
    const counter = serial.last_sent_counter;
    setText(el("hw-last-counter"), (counter === null || counter === undefined) ? "—" : String(counter));
    setText(el("hw-last-reply"), serial.last_reply || "—");
  }

  function renderComparison(comparison) {
    const grid = el("comparison-grid");
    const model = el("cmp-model");
    const trace = el("cmp-trace");
    if (!grid) return;
    if (!comparison) {
      grid.hidden = true; model.hidden = true; trace.hidden = true;
      return;
    }
    grid.hidden = false; model.hidden = false; trace.hidden = false;
    setText(el("cmp-fixed"), Math.round(comparison.cumulative_wait_fixed).toLocaleString());
    setText(el("cmp-adaptive"), Math.round(comparison.cumulative_wait_adaptive).toLocaleString());
    setText(el("cmp-pct"), `${comparison.pct_saved}%`);
    setText(model, `${comparison.units} · ${comparison.model}`);

    trace.textContent = "";
    const heading = document.createElement("div");
    heading.className = "trace-head";
    heading.textContent = "Adaptive phase trace (queue at phase start → allocated green)";
    trace.appendChild(heading);
    (comparison.adaptive.trace || []).slice(0, 12).forEach((row) => {
      const line = document.createElement("div");
      line.className = "trace-row";
      const lane = document.createElement("span");
      lane.textContent = `#${row.phase} ${row.lane}`;
      const queue = document.createElement("span");
      queue.textContent = `${row.queue_at_start} queued`;
      const green = document.createElement("b");
      green.textContent = `${row.green_sec}s`;
      line.append(lane, queue, green);
      trace.appendChild(line);
    });
  }

  // --------------------------------------------------------- countdown --
  function tickCountdown() {
    const value = countdownBase.remaining === null
      ? null
      : Math.max(0, countdownBase.remaining - (performance.now() - countdownBase.at) / 1000);
    const text = countdownBase.remaining === null ? "HOLD" : fmtClock(value);
    setText(el("global-countdown"), text);
    setText(el("core-timer"), text);
  }

  // ----------------------------------------------------------- streams --
  function connectStream() {
    if (source && source.readyState !== EventSource.CLOSED) return;
    source = new EventSource("/api/stream");
    source.onmessage = (event) => {
      lastMessageAt = Date.now();
      let payload;
      try { payload = JSON.parse(event.data); } catch (_) { return; }
      setLink(true);
      try { render(payload); } catch (error) {
        console.error("render failed", error);
        showAlert(`Dashboard render error: ${error.message}`);
      }
    };
    source.onerror = () => {
      setLink(false);
      // EventSource reconnects on its own; only rebuild if it gave up.
      if (source && source.readyState === EventSource.CLOSED) {
        source = null;
        setTimeout(connectStream, 2000);
      }
    };
  }

  function setLink(ok) {
    const pill = el("link-pill");
    const text = el("link-pill-text");
    if (!pill) return;
    pill.dataset.state = ok ? "connected" : "lost";
    setText(text, ok ? "LIVE STREAM OK" : "RECONNECTING…");
  }

  // ------------------------------------------------------------- logs --
  async function refreshLogs() {
    let payload;
    try { payload = await api("/api/logs?n=12"); } catch (_) { return; }
    const stats = payload.stats || {};
    setText(el("stat-transitions"), stats.total_transitions ?? 0);
    setText(el("stat-vehicles"), stats.total_vehicles_logged ?? 0);
    setText(el("stat-avg"), `${Number(stats.avg_green_time || 0).toFixed(1)}s`);

    const root = el("history");
    root.textContent = "";
    const rows = (payload.recent || []).slice().reverse().slice(0, 7);
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "history-empty";
      empty.textContent = "No green phases logged yet.";
      root.appendChild(empty);
      return;
    }
    rows.forEach((row) => {
      const laneName = row[1];
      const laneState = snapshot && snapshot.lanes ? snapshot.lanes[laneName] : null;
      const line = document.createElement("div");
      line.className = "history-row";
      const when = document.createElement("span");
      when.textContent = row[0];
      const lane = document.createElement("strong");
      lane.textContent = laneState ? laneState.label : laneName;
      const count = document.createElement("span");
      count.textContent = `${row[2]} vehicles`;
      const duration = document.createElement("b");
      duration.textContent = `${row[3]}s`;
      line.append(when, lane, count, duration);
      root.appendChild(line);
    });
  }

  // ------------------------------------------------------------ serial --
  async function refreshPorts() {
    const select = el("port-select");
    select.textContent = "";
    let data;
    try {
      data = await api("/api/serial/ports");
    } catch (_) {
      const option = document.createElement("option");
      option.textContent = "Server unavailable";
      select.appendChild(option);
      select.disabled = true;
      return;
    }
    const ports = data.ports || [];
    if (!data.pyserial_available || !ports.length) {
      const option = document.createElement("option");
      option.textContent = data.pyserial_available ? "No serial ports detected" : "pyserial not installed";
      select.appendChild(option);
      select.disabled = true;
      return;
    }
    select.disabled = false;
    ports.forEach((port) => {
      const option = document.createElement("option");
      option.value = port.device;
      option.textContent = port.description ? `${port.device} — ${port.description}` : port.device;
      if (port.device === data.port) option.selected = true;
      select.appendChild(option);
    });
  }

  // ------------------------------------------------------------- wiring --
  function wireControls() {
    document.querySelectorAll(".mode-btn").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await postJSON("/api/mode", { mode: button.dataset.mode });
        } catch (error) { showAlert(error.message); }
      });
    });

    el("btn-recalculate").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const result = await postJSON("/api/recalculate");
        showAlert(result.message || "Phase ended");
      } catch (error) { showAlert(error.message); }
      setTimeout(() => { button.disabled = snapshot ? snapshot.mode === "manual" : false; }, 500);
    });

    el("btn-recalibrate-all").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const result = await postJSON("/api/recalibrate");
        const failed = (result.results || []).filter((r) => !r.ok);
        showAlert(failed.length ? failed.map((r) => r.message).join(" · ") : "All feeds recalibrated");
      } catch (error) { showAlert(error.message); }
      button.disabled = false;
    });

    el("btn-refresh-ports").addEventListener("click", refreshPorts);

    el("btn-connect").addEventListener("click", async () => {
      const select = el("port-select");
      const message = el("hw-message");
      setText(message, "Connecting…");
      try {
        const result = await postJSON("/api/serial/connect",
          { port: select.disabled ? null : select.value });
        setText(message, result.message || "Connected");
      } catch (error) {
        setText(message, error.message);
      }
      refreshPorts();
    });

    el("btn-disconnect").addEventListener("click", async () => {
      try {
        const result = await postJSON("/api/serial/disconnect");
        setText(el("hw-message"), result.message || "Disconnected");
      } catch (error) { setText(el("hw-message"), error.message); }
    });

    el("btn-simulate").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const note = el("sim-note");
      button.disabled = true;
      const original = button.textContent;
      button.textContent = "RUNNING…";
      try {
        await postJSON("/api/simulate", {});
        setText(note, "Simulation complete. Figures below come from the offline model, not the live feeds.");
      } catch (error) {
        showAlert(error.message);
      }
      button.textContent = original;
      button.disabled = false;
    });

    el("btn-clear-simulation").addEventListener("click", async () => {
      try { await api("/api/simulate", { method: "DELETE" }); } catch (error) { showAlert(error.message); }
    });

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && builtLanes) {
        builtLanes.forEach((lane) => scheduleFrame(lane, 50));
      }
    });
  }

  // --------------------------------------------------------------- boot --
  async function boot() {
    wireControls();
    try {
      render(await api("/api/data"));
      setLink(true);
    } catch (error) {
      showAlert(`Could not load dashboard state: ${error.message}`);
    }
    connectStream();
    refreshPorts();
    refreshLogs();
    setInterval(refreshLogs, 4000);
    setInterval(tickCountdown, 200);
    setInterval(() => {
      if (lastMessageAt && Date.now() - lastMessageAt > LINK_STALE_MS) setLink(false);
    }, 1000);
  }

  boot();
})();
