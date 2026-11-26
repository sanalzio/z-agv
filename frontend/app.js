(() => {
  "use strict";

  const MAX_LOG_LINES = 300;
  const SLIDER_THROTTLE_MS = 80;
  const LAST_PORT_KEY = "zagv_last_port";

  const MODE_LABELS = {
    DISCONNECTED: "BAĞLI DEĞİL",
    IDLE: "IDLE // HAZIR",
    LINE: "LINE // OTONOM",
    TURN180: "TURN180 // DÖNÜŞ",
    MANUAL: "MANUAL // SÜRÜŞ",
    ESTOP: "E-STOP KİLİTLİ",
  };

  const els = {
    modePill: document.getElementById("mode-pill"),
    wsLed: document.getElementById("ws-led"),
    linkLed: document.getElementById("link-led"),
    linkText: document.getElementById("link-text"),
    rttVal: document.getElementById("rtt-val"),
    hzVal: document.getElementById("hz-val"),
    portInput: document.getElementById("port-input"),
    portOptions: document.getElementById("port-options"),
    btnRefresh: document.getElementById("btn-refresh"),
    btnConnect: document.getElementById("btn-connect"),
    btnConnectLabel: document.getElementById("btn-connect-label"),
    btnEstop: document.getElementById("btn-estop"),
    btnReset: document.getElementById("btn-reset"),
    alertBanner: document.getElementById("alert-banner"),
    btnLine: document.getElementById("btn-line"),
    btnTurn: document.getElementById("btn-turn"),
    btnStopAuto: document.getElementById("btn-stop-auto"),
    sliderVl: document.getElementById("slider-vl"),
    valVl: document.getElementById("val-vl"),
    sliderVm: document.getElementById("slider-vm"),
    valVm: document.getElementById("val-vm"),
    sliderTrim: document.getElementById("slider-trim"),
    valTrim: document.getElementById("val-trim"),
    btnTrimZero: document.getElementById("btn-trim-zero"),
    errBadge: document.getElementById("err-badge"),
    errNeedle: document.getElementById("err-needle"),
    lpwmVal: document.getElementById("lpwm-val"),
    lpwmBar: document.getElementById("lpwm-bar"),
    rpwmVal: document.getElementById("rpwm-val"),
    rpwmBar: document.getElementById("rpwm-bar"),
    btnCw: document.getElementById("btn-cw"),
    btnCb: document.getElementById("btn-cb"),
    calStatus: document.getElementById("cal-status"),
    chkFilterPing: document.getElementById("chk-filter-ping"),
    btnClearLog: document.getElementById("btn-clear-log"),
    consoleLog: document.getElementById("console-log"),
    consoleForm: document.getElementById("console-form"),
    cmdInput: document.getElementById("cmd-input"),
    cmdGuide: document.getElementById("cmd-guide"),
    sensorBars: Array.from({ length: 8 }, (_, i) => document.querySelector(`[data-s="${i}"]`)),
    sensorVals: Array.from({ length: 8 }, (_, i) => document.querySelector(`[data-sv="${i}"]`)),
    holdButtons: Array.from(document.querySelectorAll("[data-hold]")),
  };

  const state = {
    ws: null,
    wsConnected: false,
    serialConnected: false,
    linkAlive: false,
    mode: "DISCONNECTED",
    reconnectDelay: 1000,
    activeHoldDir: null,
    holdSource: null,
    cmdHistory: [],
    historyIdx: -1,
    logEntries: [],
  };

  const savedPort = localStorage.getItem(LAST_PORT_KEY);
  if (savedPort) els.portInput.value = savedPort;

  function wsUrl() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}/ws`;
  }

  function sendJson(obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  function sendCmd(cmd) {
    const clean = String(cmd || "").trim();
    if (!clean) return false;
    return sendJson({ action: "cmd", data: clean });
  }

  function appendLog(dir, data) {
    const now = new Date();
    const ts = now.toTimeString().slice(0, 8);
    state.logEntries.push({ ts, dir, data });
    if (state.logEntries.length > MAX_LOG_LINES) {
      state.logEntries.shift();
    }
    if (els.chkFilterPing.checked && (data === "P" || data.startsWith("TEL:"))) {
      return;
    }
    renderLogLine({ ts, dir, data });
  }

  function isConsoleNearBottom() {
    return els.consoleLog.scrollHeight - els.consoleLog.scrollTop - els.consoleLog.clientHeight < 28;
  }

  function buildLogRow(entry) {
    const row = document.createElement("div");
    row.className = "log-line";

    const tsSpan = document.createElement("span");
    tsSpan.className = "log-ts";
    tsSpan.textContent = `[${entry.ts}]`;

    const msgSpan = document.createElement("span");
    msgSpan.className = `log-${entry.dir}`;
    msgSpan.textContent = `${entry.dir.toUpperCase()} > ${entry.data}`;

    row.append(tsSpan, msgSpan);
    return row;
  }

  function renderLogLine(entry) {
    const nearBottom = isConsoleNearBottom();
    els.consoleLog.appendChild(buildLogRow(entry));
    while (els.consoleLog.childElementCount > MAX_LOG_LINES) {
      els.consoleLog.firstElementChild.remove();
    }
    if (nearBottom) {
      els.consoleLog.scrollTop = els.consoleLog.scrollHeight;
    }
  }

  function rerenderConsole() {
    const hideTel = els.chkFilterPing.checked;
    const wasNearBottom = isConsoleNearBottom();
    const frag = document.createDocumentFragment();
    for (const entry of state.logEntries) {
      if (hideTel && (entry.data === "P" || entry.data.startsWith("TEL:"))) continue;
      frag.appendChild(buildLogRow(entry));
    }
    els.consoleLog.textContent = "";
    els.consoleLog.appendChild(frag);
    if (wasNearBottom) {
      els.consoleLog.scrollTop = els.consoleLog.scrollHeight;
    }
  }

  function updateStatusUI(status) {
    if (typeof status.connected === "boolean") state.serialConnected = status.connected;
    if (typeof status.link_alive === "boolean") state.linkAlive = status.link_alive;
    if (status.mode) state.mode = state.serialConnected ? status.mode : "DISCONNECTED";
    if (status.port && !els.portInput.value.trim()) els.portInput.value = status.port;

    const pillMode = state.serialConnected ? state.mode : "DISCONNECTED";
    els.modePill.textContent = MODE_LABELS[pillMode] || pillMode;
    els.modePill.className = `mode-pill mode-${pillMode.toLowerCase()}`;

    els.wsLed.className = `led ${state.wsConnected ? "led-ok" : "led-err"}`;

    if (!state.serialConnected) {
      els.linkLed.className = "led led-off";
      els.linkText.textContent = "AGV KAPALI";
      els.rttVal.textContent = "-- ms";
      els.hzVal.textContent = "-- Hz";
    } else if (state.linkAlive) {
      els.linkLed.className = "led led-ok";
      els.linkText.textContent = "AGV ÇEVRİMİÇİ";
    } else {
      els.linkLed.className = "led led-warn";
      els.linkText.textContent = "YANIT YOK";
    }

    if (typeof status.rtt_ms === "number") {
      els.rttVal.textContent = `${status.rtt_ms.toFixed(1)} ms`;
    }
    if (typeof status.hz === "number" && status.hz > 0) {
      els.hzVal.textContent = `${status.hz.toFixed(1)} Hz`;
    }

    els.btnConnectLabel.textContent = state.serialConnected ? "Bağlantıyı Kes" : "Bağlan";
    els.btnReset.disabled = !state.serialConnected;
    els.btnLine.classList.toggle("is-active", state.serialConnected && state.mode === "LINE");
    els.btnTurn.classList.toggle("is-active", state.serialConnected && state.mode === "TURN180");

    updateAlertBanner(status.reason);
  }

  function updateAlertBanner(reason) {
    if (!state.wsConnected) {
      els.alertBanner.textContent = "SUNUCU BAĞLANTISI KOPTU — WebSocket yeniden bağlanıyor...";
      els.alertBanner.className = "alert-banner warn";
      return;
    }
    if (state.serialConnected && state.mode === "ESTOP") {
      els.alertBanner.textContent =
        "ACİL DURDURMA KİLİTLİ (ESTOP) — Hareket komutları engellendi. Devam etmek için RESET butonuna basın.";
      els.alertBanner.className = "alert-banner";
      return;
    }
    if (state.serialConnected && !state.linkAlive) {
      els.alertBanner.textContent =
        reason || "AGV TELEMETRİSİ KESİLDİ — Fiziksel E-Stop kapalı veya Bluetooth bağlantısı zayıf.";
      els.alertBanner.className = "alert-banner warn";
      return;
    }
    els.alertBanner.className = "alert-banner hidden";
  }

  function setMotorMeter(valEl, barEl, pwm) {
    valEl.textContent = `${pwm}`;
    const pct = Math.min(100, Math.abs(pwm) / 255 * 100) / 2;
    if (pwm >= 0) {
      barEl.style.left = "50%";
      barEl.style.width = `${pct}%`;
      barEl.style.backgroundColor = "var(--accent)";
    } else {
      barEl.style.left = `${50 - pct}%`;
      barEl.style.width = `${pct}%`;
      barEl.style.backgroundColor = "var(--amber)";
    }
  }

  function updateTelemetryUI(tel) {
    if (tel.mode && tel.mode !== state.mode) {
      updateStatusUI({
        connected: true,
        link_alive: true,
        mode: tel.mode,
        rtt_ms: tel.rtt_ms,
        hz: tel.hz,
      });
    } else {
      if (typeof tel.rtt_ms === "number") els.rttVal.textContent = `${tel.rtt_ms.toFixed(1)} ms`;
      if (typeof tel.hz === "number" && tel.hz > 0) els.hzVal.textContent = `${tel.hz.toFixed(1)} Hz`;
    }

    const sensors = tel.sensors || [];
    for (let i = 0; i < 8; i++) {
      const val = Number(sensors[i] || 0);
      const bar = els.sensorBars[i];
      const lbl = els.sensorVals[i];
      bar.style.height = `${Math.max(6, Math.round(val / 9 * 100))}%`;
      bar.style.backgroundColor = val >= 6 ? "#f08332" : val >= 3 ? "#c2743b" : "#4a433c";
      lbl.textContent = `${val}`;
    }

    if (tel.error === 999) {
      els.errBadge.textContent = "ÇİZGİ YOK (999)";
      els.errBadge.classList.add("lost");
      els.errNeedle.classList.add("lost");
      els.errNeedle.style.left = "50%";
    } else {
      const clamped = Math.max(-100, Math.min(100, Number(tel.error || 0)));
      els.errBadge.textContent = `HATA: ${clamped > 0 ? "+" : ""}${clamped}`;
      els.errBadge.classList.remove("lost");
      els.errNeedle.classList.remove("lost");
      els.errNeedle.style.left = `${(clamped + 100) / 2}%`;
    }

    setMotorMeter(els.lpwmVal, els.lpwmBar, Number(tel.left_pwm || 0));
    setMotorMeter(els.rpwmVal, els.rpwmBar, Number(tel.right_pwm || 0));
  }

  function handleAckSideEffects(line) {
    if (line === "ACK:CW") els.calStatus.textContent = "Beyaz zemin EEPROM'a kaydedildi";
    else if (line === "ACK:CB") els.calStatus.textContent = "Siyah çizgi EEPROM'a kaydedildi";
    else if (line.startsWith("ACK:VL:")) syncSlider(els.sliderVl, els.valVl, line.slice(7), formatPwm);
    else if (line.startsWith("ACK:VM:")) syncSlider(els.sliderVm, els.valVm, line.slice(7), formatPwm);
    else if (line.startsWith("ACK:T:")) syncSlider(els.sliderTrim, els.valTrim, line.slice(6), (v) => `${v}`);
    else if (line === "ERR:STOP_FIRST") els.calStatus.textContent = "Kalibrasyon için aracı durdurun!";
  }

  function syncSlider(slider, readout, rawVal, formatter) {
    const n = Number(rawVal);
    if (!Number.isFinite(n)) return;
    slider.value = String(n);
    readout.textContent = formatter(n);
  }

  function formatPwm(v) {
    return `${v} PWM (%${Math.round(Number(v) / 255 * 100)})`;
  }

  function connectWs() {
    const ws = new WebSocket(wsUrl());
    state.ws = ws;

    ws.addEventListener("open", () => {
      state.wsConnected = true;
      state.reconnectDelay = 1000;
      appendLog("sys", "WebSocket bağlantısı kuruldu");
      updateStatusUI({ connected: state.serialConnected, link_alive: state.linkAlive, mode: state.mode });
    });

    ws.addEventListener("message", (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }

      if (msg.type === "status") {
        updateStatusUI(msg);
      } else if (msg.type === "ports") {
        els.portOptions.textContent = "";
        for (const p of msg.ports || []) {
          const opt = document.createElement("option");
          opt.value = p.device;
          opt.label = p.description || p.device;
          els.portOptions.appendChild(opt);
        }
        if (!els.portInput.value.trim() && msg.ports && msg.ports.length > 0) {
          els.portInput.value = msg.ports[0].device;
        }
      } else if (msg.type === "telemetry") {
        updateTelemetryUI(msg);
        if (!els.chkFilterPing.checked) {
          const ts = new Date().toTimeString().slice(0, 8);
          const s = (msg.sensors || []).join("");
          renderLogLine({ ts, dir: "rx", data: `TEL:${msg.mode},${msg.left_pwm},${msg.right_pwm},${s},${msg.error}` });
        }
      } else if (msg.type === "log") {
        appendLog(msg.dir || "rx", msg.data || "");
        if (msg.dir === "rx") handleAckSideEffects(msg.data || "");
      }
    });

    ws.addEventListener("close", () => {
      state.wsConnected = false;
      state.serialConnected = false;
      state.linkAlive = false;
      clearManualHold(false);
      updateStatusUI({ connected: false, link_alive: false, mode: "DISCONNECTED" });
      const delay = state.reconnectDelay;
      state.reconnectDelay = Math.min(5000, Math.round(delay * 1.5));
      window.setTimeout(connectWs, delay);
    });
  }

  function bindThrottledSlider(slider, readout, prefix, formatter) {
    let lastSent = 0;
    let pendingTimer = null;

    const dispatch = () => {
      lastSent = Date.now();
      sendCmd(`${prefix}${slider.value}`);
    };

    const schedule = () => {
      readout.textContent = formatter(slider.value);
      const now = Date.now();
      if (now - lastSent >= SLIDER_THROTTLE_MS) {
        if (pendingTimer) clearTimeout(pendingTimer);
        pendingTimer = null;
        dispatch();
      } else if (!pendingTimer) {
        pendingTimer = setTimeout(() => {
          pendingTimer = null;
          dispatch();
        }, SLIDER_THROTTLE_MS);
      }
    };

    const set = (val) => {
      const clamped = Math.max(Number(slider.min), Math.min(Number(slider.max), Math.round(Number(val))));
      slider.value = String(clamped);
      schedule();
    };

    slider.addEventListener("input", schedule);

    slider.addEventListener("change", () => {
      readout.textContent = formatter(slider.value);
      if (pendingTimer) {
        clearTimeout(pendingTimer);
        pendingTimer = null;
      }
      dispatch();
    });

    return {
      nudge: (delta) => set(Number(slider.value) + delta),
      set,
    };
  }

  function highlightHold(dir) {
    for (const btn of els.holdButtons) {
      btn.classList.toggle("is-active", btn.dataset.hold === dir);
    }
  }

  function startManualHold(dir, source) {
    if (!dir) return;
    if (state.activeHoldDir === dir && state.holdSource === source) return;
    state.activeHoldDir = dir;
    state.holdSource = source;
    highlightHold(dir);
    sendCmd(dir);
  }

  function clearManualHold(sendStop = true) {
    const hadHold = state.activeHoldDir !== null || state.mode === "MANUAL";
    state.activeHoldDir = null;
    state.holdSource = null;
    highlightHold(null);
    if (sendStop && hadHold) {
      sendCmd("S");
    }
  }

  // Shared actions for clicks and keyboard shortcuts
  function triggerEstop() {
    clearManualHold(false);
    sendCmd("E");
  }

  function triggerStop() {
    clearManualHold(false);
    sendCmd("S");
  }

  function triggerReset() {
    if (!els.btnReset.disabled) sendCmd("R");
  }

  function triggerConnectToggle() {
    if (state.serialConnected) {
      sendJson({ action: "disconnect" });
      return;
    }
    const port = els.portInput.value.trim();
    if (!port) {
      appendLog("err", "Lütfen geçerli bir seri port yolu girin (ör. /dev/rfcomm0)");
      els.portInput.focus();
      return;
    }
    localStorage.setItem(LAST_PORT_KEY, port);
    sendJson({ action: "connect", port, baud: 9600 });
  }

  function triggerCalibrate(isBlack) {
    els.calStatus.textContent = isBlack ? "Siyah referans okunuyor..." : "Beyaz referans okunuyor...";
    sendCmd(isBlack ? "CB" : "CW");
  }

  function triggerClearLog() {
    state.logEntries = [];
    els.consoleLog.textContent = "";
  }

  function triggerTogglePingFilter() {
    els.chkFilterPing.checked = !els.chkFilterPing.checked;
    rerenderConsole();
  }

  els.btnRefresh.addEventListener("click", () => sendJson({ action: "list_ports" }));
  els.btnConnect.addEventListener("click", triggerConnectToggle);
  els.btnEstop.addEventListener("click", triggerEstop);
  els.btnReset.addEventListener("click", triggerReset);
  els.btnLine.addEventListener("click", () => sendCmd("L"));
  els.btnTurn.addEventListener("click", () => sendCmd("U"));
  els.btnStopAuto.addEventListener("click", triggerStop);
  document.getElementById("btn-stop-manual").addEventListener("click", triggerStop);
  els.btnCw.addEventListener("click", () => triggerCalibrate(false));
  els.btnCb.addEventListener("click", () => triggerCalibrate(true));

  const ctrlVl = bindThrottledSlider(els.sliderVl, els.valVl, "VL:", formatPwm);
  const ctrlVm = bindThrottledSlider(els.sliderVm, els.valVm, "VM:", formatPwm);
  const ctrlTrim = bindThrottledSlider(els.sliderTrim, els.valTrim, "T:", (v) => `${v}`);

  els.btnTrimZero.addEventListener("click", () => ctrlTrim.set(0));

  for (const btn of els.holdButtons) {
    const dir = btn.dataset.hold;
    btn.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      try { btn.setPointerCapture(e.pointerId); } catch {}
      startManualHold(dir, "pointer");
    });
    const endPointer = () => {
      if (state.holdSource === "pointer" && state.activeHoldDir === dir) {
        clearManualHold(true);
      }
    };
    btn.addEventListener("pointerup", endPointer);
    btn.addEventListener("pointercancel", endPointer);
    btn.addEventListener("lostpointercapture", endPointer);
    btn.addEventListener("touchstart", (e) => e.preventDefault(), { passive: false });
    btn.addEventListener("contextmenu", (e) => e.preventDefault());
  }

  const KEY_DIR_MAP = {
    KeyW: "F",
    ArrowUp: "F",
    KeyS: "B",
    ArrowDown: "B",
    KeyA: "A",
    ArrowLeft: "A",
    KeyD: "D",
    ArrowRight: "D",
  };

  const SLIDER_KEYS = {
    Digit1: () => ctrlVl.nudge(-5),
    Digit2: () => ctrlVl.nudge(5),
    Digit3: () => ctrlVm.nudge(-5),
    Digit4: () => ctrlVm.nudge(5),
    Digit5: () => ctrlTrim.nudge(-1),
    Digit6: () => ctrlTrim.nudge(1),
    Digit0: () => ctrlTrim.set(0),
  };

  const CTRL_KEYS = {
    KeyU: () => sendCmd("U"),
    KeyB: () => triggerCalibrate(false),
    KeyS: () => triggerCalibrate(true),
    KeyK: triggerConnectToggle,
    KeyY: () => sendJson({ action: "list_ports" }),
    KeyR: triggerReset,
    KeyP: triggerTogglePingFilter,
    Delete: triggerClearLog,
  };

  window.addEventListener("keydown", (e) => {
    // Global E-STOP shortcut works even when focus is inside a text input
    if (e.ctrlKey && e.shiftKey && !e.altKey && !e.metaKey && e.code === "KeyE") {
      e.preventDefault();
      if (!e.repeat) triggerEstop();
      return;
    }

    if (e.altKey || e.metaKey) return;
    const active = document.activeElement;
    const tag = active ? active.tagName : "";
    const isTextInput =
      tag === "TEXTAREA" ||
      tag === "SELECT" ||
      (tag === "INPUT" && active.type !== "range" && active.type !== "checkbox");
    if (isTextInput) return;

    if (e.ctrlKey && !e.shiftKey) {
      const ctrlAction = CTRL_KEYS[e.code];
      if (ctrlAction) {
        e.preventDefault();
        if (!e.repeat) ctrlAction();
      }
      return;
    }

    if (e.ctrlKey || e.shiftKey) return;

    if (e.code === "KeyL") {
      e.preventDefault();
      if (!e.repeat) sendCmd("L");
      return;
    }

    if (e.code === "Space") {
      e.preventDefault();
      if (!e.repeat) triggerStop();
      return;
    }

    const sliderAction = SLIDER_KEYS[e.code];
    if (sliderAction) {
      e.preventDefault();
      sliderAction();
      return;
    }

    const dir = KEY_DIR_MAP[e.code];
    if (dir) {
      e.preventDefault();
      if (!e.repeat) startManualHold(dir, e.code);
    }
  });

  window.addEventListener("keyup", (e) => {
    if (state.holdSource === e.code) {
      clearManualHold(true);
    }
  });

  window.addEventListener("blur", () => clearManualHold(true));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearManualHold(true);
  });

  els.chkFilterPing.addEventListener("change", rerenderConsole);
  els.btnClearLog.addEventListener("click", triggerClearLog);

  els.consoleForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const raw = els.cmdInput.value.trim();
    if (!raw) return;
    if (sendCmd(raw)) {
      state.cmdHistory.unshift(raw);
      if (state.cmdHistory.length > 50) state.cmdHistory.pop();
      state.historyIdx = -1;
      els.cmdInput.value = "";
    } else {
      appendLog("err", "Komut gönderilemedi (WS bağlı değil)");
    }
  });

  els.cmdInput.addEventListener("keydown", (e) => {
    if (e.key === "ArrowUp") {
      if (state.historyIdx + 1 < state.cmdHistory.length) {
        state.historyIdx += 1;
        els.cmdInput.value = state.cmdHistory[state.historyIdx];
      }
      e.preventDefault();
    } else if (e.key === "ArrowDown") {
      if (state.historyIdx > 0) {
        state.historyIdx -= 1;
        els.cmdInput.value = state.cmdHistory[state.historyIdx];
      } else {
        state.historyIdx = -1;
        els.cmdInput.value = "";
      }
      e.preventDefault();
    }
  });

  els.cmdGuide.addEventListener("click", (e) => {
    const chip = e.target.closest("[data-fill]");
    if (!chip) return;
    els.cmdInput.value = chip.dataset.fill;
    els.cmdInput.focus();
  });

  document.addEventListener("selectstart", (e) => {
    const el = e.target instanceof Element ? e.target : e.target?.parentElement;
    if (!el || !el.closest("#port-input, #cmd-input, .console-log")) {
      e.preventDefault();
    }
  });

  connectWs();
})();
