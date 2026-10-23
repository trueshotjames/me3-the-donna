/* Voice Assistant UI. Talks to the backend over a WebSocket (see ../UI_EVENTS.md). */
(() => {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const WS_URL = params.get("wsurl") || `ws://127.0.0.1:${params.get("ws") || 8765}`;

  // ---------- sounds (synthesised with WebAudio, no files needed) ----------
  const Sound = {
    ctx: null,
    muted: false,
    alarmTimer: null,
    ensure() {
      if (!this.ctx) this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (this.ctx.state === "suspended") this.ctx.resume();
      return this.ctx;
    },
    tone(freq, start, dur, type = "sine", gain = 0.25) {
      const ctx = this.ensure();
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = type; o.frequency.value = freq;
      g.gain.setValueAtTime(0, ctx.currentTime + start);
      g.gain.linearRampToValueAtTime(gain, ctx.currentTime + start + 0.01);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + start + dur);
      o.connect(g).connect(ctx.destination);
      o.start(ctx.currentTime + start); o.stop(ctx.currentTime + start + dur + 0.05);
    },
    chime() {            // reminder created / due: two rising notes
      if (this.muted) return;
      this.tone(880, 0, 0.25); this.tone(1175, 0.18, 0.45);
    },
    tick() {             // state change: short soft blip
      if (this.muted) return;
      this.tone(1500, 0, 0.05, "triangle", 0.08);
    },
    listenStart() { if (!this.muted) { this.tone(660, 0, 0.09, "sine", 0.15); this.tone(990, 0.09, 0.12, "sine", 0.15); } },
    listenStop()  { if (!this.muted) { this.tone(990, 0, 0.09, "sine", 0.15); this.tone(660, 0.09, 0.12, "sine", 0.15); } },
    alarmStart() {       // repeating ring until stopped
      this.alarmStop();
      const burst = () => {
        if (this.muted) return;
        for (let i = 0; i < 4; i++) { this.tone(1046, i * 0.14, 0.09, "square", 0.18); this.tone(1318, i * 0.14 + 0.05, 0.09, "square", 0.18); }
      };
      burst();
      this.alarmTimer = setInterval(burst, 1000);
    },
    alarmStop() { if (this.alarmTimer) { clearInterval(this.alarmTimer); this.alarmTimer = null; } },
  };

  // ---------- toasts ----------
  // Each toast stays 10 s (or until its x is clicked). The same message arriving again while it is
  // still shown restarts its timer and counts up instead of stacking copies (error floods).
  const TOAST_MS = 10000;
  function toast(msg, cls = "") {
    const box = $("toasts");
    for (const old of box.children) {
      if (old.dataset.msg === msg && old.dataset.cls === cls) {
        const n = Number(old.dataset.count || 1) + 1;
        old.dataset.count = n; old.querySelector(".toast-count").textContent = ` ×${n}`;
        clearTimeout(old._timer); old._timer = setTimeout(() => old.remove(), TOAST_MS);
        return;
      }
    }
    const el = document.createElement("div");
    el.className = `toast ${cls}`; el.dataset.msg = msg; el.dataset.cls = cls;
    const text = document.createElement("span"); text.className = "toast-text"; text.textContent = msg;
    const count = document.createElement("span"); count.className = "toast-count";
    text.appendChild(count);
    const close = document.createElement("button");
    close.className = "toast-close"; close.type = "button"; close.title = "Close"; close.setAttribute("aria-label", "Close notification"); close.textContent = "×";
    close.onclick = () => { clearTimeout(el._timer); el.remove(); };
    el.append(text, close);
    box.appendChild(el);
    el._timer = setTimeout(() => el.remove(), TOAST_MS);
  }

  // ---------- clock ----------
  function tickClock() {
    const d = new Date();
    const hh = d.getHours() % 12 || 12, mm = String(d.getMinutes()).padStart(2, "0");
    $("clock").textContent = `${hh}:${mm}`;
    $("clock-sec").textContent = `${String(d.getSeconds()).padStart(2, "0")} s · ${d.getHours() < 12 ? "AM" : "PM"}`;
    $("date").textContent = d.toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" });
  }
  tickClock(); setInterval(tickClock, 1000);

  // ---------- state ----------
  const LABEL = { starting: "Starting…", idle: "Idle", listening: "Listening…", thinking: "Thinking…", speaking: "Speaking" };
  let current = "idle";
  function setState(s) {
    if (!LABEL[s] || s === current) return;
    if (s === "listening") Sound.listenStart();
    else if (current === "listening") Sound.listenStop();
    else if (s !== "starting") Sound.tick();
    current = s;
    $("orb").className = `orb state-${s}`;
    $("state-label").textContent = LABEL[s];
    $("interrupt").style.visibility = s === "speaking" ? "visible" : "hidden";
  }
  $("interrupt").style.visibility = "hidden";

  // ---------- reminders / alarms ----------
  const fmtDue = (iso) => {
    if (!iso) return "no time";
    const d = new Date(iso); if (isNaN(d)) return iso;
    const today = new Date().toDateString() === d.toDateString();
    return (today ? "" : d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " ") +
      d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  };
  const fmt12 = (hhmm) => { const [h, m] = (hhmm || "").split(":").map(Number); if (isNaN(h)) return hhmm; return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`; };
  const seenIds = new Set();

  function renderReminders(items) {
    const ul = $("reminders"); ul.innerHTML = "";
    const open = items.filter(r => !r.done);
    $("rem-count").textContent = open.length ? `(${open.length})` : "";
    if (!items.length) { ul.innerHTML = '<li class="empty">No reminders yet. Try: "remind me to … in 10 minutes".</li>'; return; }
    items.sort((a, b) => (a.done - b.done) || String(a.due_at || "z").localeCompare(String(b.due_at || "z")));
    for (const r of items) {
      const li = document.createElement("li");
      li.className = (r.done ? "done " : "") + (seenIds.has(r.id) ? "" : "new");
      li.innerHTML = `<div class="item-main"><div class="item-text"></div><div class="item-sub">${fmtDue(r.due_at)}</div></div>
        <div class="item-actions">${r.done ? "" : '<button class="btn btn-sm" data-act="reminder_done">Done</button>'}<button class="btn btn-sm" data-act="reminder_delete">✕</button></div>`;
      li.querySelector(".item-text").textContent = r.text;
      li.querySelectorAll("button").forEach(b => b.onclick = () => send({ type: b.dataset.act, id: r.id }));
      ul.appendChild(li); seenIds.add(r.id);
    }
  }

  function renderAlarms(items) {
    const ul = $("alarms"); ul.innerHTML = "";
    const on = items.filter(a => a.enabled !== false);
    $("alarm-count").textContent = on.length ? `(${on.length})` : "";
    if (!items.length) { ul.innerHTML = '<li class="empty">No alarms. Try: "set an alarm for 6:30".</li>'; return; }
    items.sort((a, b) => (Number(b.enabled !== false) - Number(a.enabled !== false)) || String(a.time).localeCompare(String(b.time)));
    for (const a of items) {
      const li = document.createElement("li");
      li.className = (a.enabled === false ? "off " : "") + (seenIds.has(a.id) ? "" : "new");
      const sub = a.ringing ? "ringing" : a.enabled === false ? "rang" : a.repeat_daily ? "daily" : (a.repeat || "once");
      li.innerHTML = `<div class="item-time">${fmt12(a.time)}</div><div class="item-main"><div class="item-text"></div><div class="item-sub">${sub}</div></div>
        <div class="item-actions"><button class="btn btn-sm" data-act="alarm_delete">✕</button></div>`;
      li.querySelector(".item-text").textContent = a.label || "Alarm";
      li.querySelector("[data-act]").onclick = () => send({ type: "alarm_delete", id: a.id });
      ul.appendChild(li); seenIds.add(a.id);
      if (a.ringing && ringing === null) ringStart(a.id, a.label);
    }
  }

  let ringing = null;
  function ringStart(id, label) {
    ringing = id;
    $("ring-label").textContent = label || "Alarm";
    $("ring-time").textContent = new Date().toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    $("ring-overlay").classList.remove("hidden");
    Sound.alarmStart();
  }
  function ringStop(notify) {
    if (ringing === null) return;
    if (notify) send({ type: "alarm_stop", id: ringing });
    ringing = null; Sound.alarmStop();
    $("ring-overlay").classList.add("hidden");
  }
  $("ring-stop").onclick = () => ringStop(true);

  // ---------- websocket ----------
  let ws = null, retry = 1000;
  function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ ts: Date.now(), ...obj })); }
  function connect() {
    ws = new WebSocket(WS_URL);
    ws.onopen = () => { retry = 1000; $("conn").textContent = "● connected"; $("conn").className = "pill pill-on"; };
    ws.onclose = () => { $("conn").textContent = "● reconnecting"; $("conn").className = "pill pill-off"; setState("idle"); setTimeout(connect, retry); retry = Math.min(retry * 2, 8000); };
    ws.onerror = () => ws.close();
    ws.onmessage = (ev) => { let m; try { m = JSON.parse(ev.data); } catch { return; } handle(m); };
  }
  function handle(m) {
    switch (m.type) {
      case "hello": $("conn").textContent = "● connected"; $("conn").className = "pill pill-on"; break;
      case "state": setState(m.state); break;
      case "interrupted": Sound.listenStop(); $("assistant-text").textContent += " —"; break;
      case "transcript": {
        const t = $("transcript"); t.textContent = m.text || ""; t.className = "text" + (m.final ? "" : " partial");
        if (!m.final) { $("assistant-text").textContent = "—"; $("assistant-text").className = "text muted"; $("tool-line").textContent = ""; }
        break;
      }
      case "assistant_text": { const t = $("assistant-text"); t.textContent = m.text || ""; t.className = "text" + (m.final ? "" : " partial"); break; }
      case "tool_call": $("tool-line").textContent = `⚙ ${m.name}(${JSON.stringify(m.args || {})})`; break;
      case "tool_result": {
        const r = m.result, err = r && typeof r === "object" && r.error;
        $("tool-line").textContent += ` ${err ? "✗ " + err : "✓ " + (typeof r === "string" ? r : JSON.stringify(r))}`.slice(0, 400);
        break;
      }
      case "reminders": renderReminders(m.items || []); break;
      case "alarms": renderAlarms(m.items || []); break;
      case "reminder_created": Sound.chime(); toast(`Reminder set: ${m.text} · ${fmtDue(m.due_at)}`); break;
      case "reminder_due": Sound.chime(); Sound.chime(); toast(`Reminder: ${m.text}`, "warn"); break;
      case "alarm_created": Sound.chime(); toast(`Alarm set: ${m.label || "Alarm"} at ${fmt12(m.time)}`); break;
      case "alarm_fired": ringStart(m.id, m.label); break;
      case "alarm_stopped": if (ringing === m.id || m.id == null) ringStop(false); break;
      case "alarm_deleted": if (ringing === m.id) ringStop(false); break;
      case "time_told": toast(`🕒 ${m.spoken || m.iso}`); $("clock").classList.add("flash"); setTimeout(() => $("clock").classList.remove("flash"), 1500); break;
      case "muted": setMic(!!m.muted); break;
      case "latency": if (m.stage === "voice_to_voice") $("latency").textContent = `voice-to-voice ${Math.round(m.ms)} ms`; break;
      case "latency_breakdown": if (m.total_ms != null) $("latency").textContent = `voice-to-voice ${Math.round(m.total_ms)} ms`; break;
      case "error": toast(m.message || "error", "err"); if (m.fatal) setState("idle"); break;
      case "warning": toast(m.message || "warning", "warn"); break;
      case "info":
        if (m.stub) { $("conn").textContent = "● stub backend"; $("conn").className = "pill pill-stub"; }
        if (m.message) toast(m.message);
        break;
      case "llm_wait": showWait(m.seconds, m.remaining_tokens, m.limit_tokens); break;
      case "config": Settings.config(m); break;
      case "summary": Settings.summary(m); break;
      case "calibrate": Settings.calibrate(m); break;
    }
  }
  connect();

  // ---------- rate-limit wait (Groq 429) ----------
  let waitTimer = null;
  function showWait(seconds, left, limit) {
    const el = $("wait"); let s = Math.ceil(seconds || 0);
    const tokens = left != null && limit ? ` · ${Math.round(left)}/${Math.round(limit)} tokens left this minute` : "";
    toast(`Groq rate limit: reply delayed ${s} s${tokens}`, "warn");
    if (waitTimer) clearInterval(waitTimer);
    const tick = () => { if (s <= 0) { el.classList.add("hidden"); clearInterval(waitTimer); waitTimer = null; return; } el.textContent = `⏳ rate limited · ${s--} s`; };
    el.classList.remove("hidden"); tick(); waitTimer = setInterval(tick, 1000);
  }

  // ---------- settings panel ----------
  const Settings = {
    open() { $("settings").classList.remove("hidden"); send({ type: "config" }); },
    close() { $("settings").classList.add("hidden"); },
    tab(name) {
      document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
      document.querySelectorAll(".tab-body").forEach(b => b.classList.toggle("hidden", b.id !== `tab-${name}`));
    },
    config(c) {
      const dl = $("cfg"); dl.innerHTML = "";
      const rows = [["LLM model", c.model], ["Reasoning effort", c.reasoning_effort || "(unset)"], ["History re-sent per call", `${c.history_messages} messages`],
        ["VAD confidence", c.vad_confidence], ["VAD min loudness", c.vad_min_volume], ["VAD start / stop", `${c.vad_start_secs} s / ${c.vad_stop_secs} s`],
        ["Turn start", c.turn_start === "asr" ? "Flux (asr)" : "local VAD"], ["Eager end of turn", c.eager_end_of_turn ? "on" : "off"], ["TTS voice", c.tts_voice], ["Turn log", c.turns_log]];
      for (const [k, v] of rows) { const dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = k; dd.textContent = v ?? "—"; dl.append(dt, dd); }
      this.current = { confidence: c.vad_confidence, min_volume: c.vad_min_volume };
      if (c.calibrating) { $("cal-progress").classList.remove("hidden"); $("cal-cancel").classList.remove("hidden"); }
    },
    summary(m) {
      const info = $("sum-info"), wrap = $("sum-table");
      if (m.error) { info.textContent = m.error; wrap.innerHTML = ""; return; }
      info.textContent = `${m.turns} turns (${m.from} to ${m.to}), ${m.replied} with a full reply` +
        (m.rate_limit_waits ? ` · rate limited ${m.rate_limit_waits}× (${m.rate_limit_wait_s} s waited)` : "");
      const t = document.createElement("table"); t.className = "sum";
      t.innerHTML = "<thead><tr><th>metric</th><th>n</th><th>median</th><th>p90</th><th>min</th><th>max</th></tr></thead>";
      const tb = document.createElement("tbody"); let group = "";
      for (const r of m.rows || []) {
        const g = r.metric.split(".")[0];
        if (g !== group) { group = g; const tr = document.createElement("tr"); tr.className = "group"; tr.innerHTML = `<td colspan="6">${g}</td>`; tb.appendChild(tr); }
        const tr = document.createElement("tr");
        const bad = r.metric === "e2e.total_ms" && r.median > 1000 ? " class=\"bad\"" : "";
        tr.innerHTML = `<td>${r.metric}</td><td>${r.n}</td><td${bad}>${r.median}</td><td${bad}>${r.p90}</td><td>${r.min}</td><td>${r.max}</td>`;
        tb.appendChild(tr);
      }
      t.appendChild(tb); wrap.innerHTML = ""; wrap.appendChild(t);
    },
    calTimer: null,
    calibrate(m) {
      const prog = $("cal-progress"), phase = $("cal-phase"), bar = $("cal-bar"), count = $("cal-count"), res = $("cal-result");
      const stopTimer = () => { if (this.calTimer) { clearInterval(this.calTimer); this.calTimer = null; } };
      if (m.phase === "quiet" || m.phase === "talk") {
        prog.classList.remove("hidden"); res.classList.add("hidden"); $("cal-cancel").classList.remove("hidden"); $("cal-start").disabled = true;
        phase.textContent = (m.phase === "quiet" ? "🤫 " : "🗣 ") + m.message;
        bar.style.background = m.phase === "quiet" ? "var(--accent)" : "var(--ok)";
        if (m.phase === "quiet") Sound.listenStart(); else Sound.chime();
        stopTimer(); const t0 = Date.now(), total = (m.secs || 10) * 1000;
        const tick = () => { const p = Math.min(1, (Date.now() - t0) / total); bar.style.width = `${p * 100}%`; count.textContent = `${Math.max(0, Math.ceil((total - (Date.now() - t0)) / 1000))} s left`; if (p >= 1) stopTimer(); };
        tick(); this.calTimer = setInterval(tick, 200);
        return;
      }
      stopTimer(); prog.classList.add("hidden"); $("cal-cancel").classList.add("hidden"); $("cal-start").disabled = false;
      if (m.phase === "done") {
        const r = m.result, cur = r.current || this.current || {};
        Sound.listenStop();
        res.innerHTML = `
          <div class="pair">
            <div><div class="hint">Suggested VAD_CONFIDENCE</div><div class="big">${r.confidence}</div><div class="hint">now ${cur.confidence}</div></div>
            <div><div class="hint">Suggested VAD_MIN_VOLUME</div><div class="big">${r.min_volume}</div><div class="hint">now ${cur.min_volume}</div></div>
            <div><div class="hint">Speech frames passing</div><div class="big">${r.speech_pass_pct}%</div><div class="hint">${r.speech_frames} speech frames of ${r.talk_frames}</div></div>
            <div><div class="hint">Quiet-room frames passing</div><div class="big">${r.quiet_pass_pct}%</div><div class="hint">${r.quiet_frames} frames</div></div>
          </div>
          <div class="hint">Quiet room: confidence p50/p90/p99 ${r.quiet.confidence["50"]} / ${r.quiet.confidence["90"]} / ${r.quiet.confidence["99"]}, loudness ${r.quiet.loudness["50"]} / ${r.quiet.loudness["90"]} / ${r.quiet.loudness["99"]}.
            Speech: confidence p5/p50/p95 ${r.speech.confidence["5"]} / ${r.speech.confidence["50"]} / ${r.speech.confidence["95"]}, loudness ${r.speech.loudness["5"]} / ${r.speech.loudness["50"]} / ${r.speech.loudness["95"]}.</div>
          ${r.too_quiet ? '<div class="warn">Your speech is barely louder than the room; move closer to the mic and run it again.</div>' : ""}
          <div class="row"><button id="cal-apply" class="btn">Apply to the running assistant and .env</button><span class="hint">Takes effect at once; .env keeps it for the next start.</span></div>`;
        res.classList.remove("hidden");
        $("cal-apply").onclick = () => { send({ type: "vad_apply", confidence: r.confidence, min_volume: r.min_volume }); $("cal-apply").disabled = true; $("cal-apply").textContent = "Applied"; };
      } else if (m.phase === "error") { toast(`Calibration: ${m.message}`, "err"); }
      else if (m.phase === "cancelled") { toast("Calibration cancelled"); }
    },
  };
  $("settings-btn").onclick = () => Settings.open();
  $("settings-close").onclick = () => Settings.close();
  $("settings").addEventListener("click", (e) => { if (e.target === $("settings")) Settings.close(); });
  document.querySelectorAll(".tab").forEach(t => t.onclick = () => Settings.tab(t.dataset.tab));
  $("sum-run").onclick = () => { $("sum-info").textContent = "running…"; send({ type: "summarize", last: Number($("sum-last").value) }); };
  $("cal-start").onclick = () => { Sound.ensure(); $("cal-result").classList.add("hidden"); send({ type: "vad_calibrate", secs: Number($("cal-secs").value) }); };
  $("cal-cancel").onclick = () => send({ type: "vad_calibrate_cancel" });

  // ---------- controls ----------
  $("interrupt").onclick = () => send({ type: "interrupt" });
  $("mute").onclick = () => {
    Sound.muted = !Sound.muted;
    if (Sound.muted) Sound.alarmStop(); else if (ringing !== null) Sound.alarmStart();
    $("mute").textContent = Sound.muted ? "🔇 Sounds off" : "🔊 Sounds on";
  };
  let micMuted = false;
  function setMic(m) { micMuted = m; $("mic").textContent = m ? "🎙 Mic muted" : "🎙 Mic on"; $("mic").classList.toggle("off", m); }
  $("mic").onclick = () => { setMic(!micMuted); send({ type: "mute", muted: micMuted }); };
  $("test-sounds").onclick = () => { Sound.ensure(); Sound.chime(); setTimeout(() => { Sound.alarmStart(); setTimeout(() => Sound.alarmStop(), 2200); }, 900); };
  // browsers need one user gesture before audio can play
  document.addEventListener("pointerdown", () => Sound.ensure(), { once: true });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("settings").classList.contains("hidden")) { Settings.close(); return; }
    if (e.key === "Escape" || (e.key === " " && ringing !== null)) ringStop(true);
  });
})();
