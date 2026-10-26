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
    pop() {              // message delivered / light switched: one soft pop
      if (this.muted) return;
      this.tone(520, 0, 0.08, "sine", 0.18); this.tone(780, 0.04, 0.1, "sine", 0.12);
    },
    callTimer: null,
    callRingStart() {    // outgoing call: "ring ring" every 2 s until it connects
      this.callRingStop();
      const burst = () => { if (this.muted) return; for (let i = 0; i < 2; i++) { this.tone(440, i * 0.25, 0.18, "sine", 0.14); this.tone(480, i * 0.25, 0.18, "sine", 0.14); } };
      burst(); this.callTimer = setInterval(burst, 2000);
    },
    callRingStop() { if (this.callTimer) { clearInterval(this.callTimer); this.callTimer = null; } },
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
      if (a.ringing && ringing === null) ringStart(a.id, a.label, "alarm");
    }
  }

  // One full-screen overlay for a ringing alarm or timer: {kind, id} while something rings.
  let ringing = null;
  function ringStart(id, label, kind = "alarm") {
    ringing = { kind, id };
    $("ring-bell").textContent = kind === "timer" ? "⏲" : "⏰";
    $("ring-label").textContent = label || (kind === "timer" ? "Timer done" : "Alarm");
    $("ring-time").textContent = new Date().toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    $("ring-overlay").classList.remove("hidden");
    Sound.alarmStart();
  }
  function ringStop(notify) {
    if (ringing === null) return;
    if (notify) send({ type: ringing.kind === "timer" ? "timer_stop" : "alarm_stop", id: ringing.id });
    ringing = null; Sound.alarmStop();
    $("ring-overlay").classList.add("hidden");
  }
  const isRinging = (kind, id) => ringing !== null && ringing.kind === kind && (id == null || ringing.id === id);
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
      case "interrupted": Sound.listenStop(); break;
      case "transcript":
        if (m.final) Chat.live(null);
        else { Chat.live(m.text); $("tool-line").textContent = ""; }
        break;
      case "assistant_text": break;  // shown through "chat" (one bubble per reply)
      case "chat": Chat.upsert(m.item); break;
      case "chat_history": Chat.reset(m.items || []); break;
      case "levels": Meters.update(m); break;
      case "net": Net.update(m); break;
      case "tool_call": $("tool-line").textContent = `⚙ ${m.name}(${JSON.stringify(m.args || {})})`; Home.busy(m.name, m.args); break;
      case "tool_result": {
        const r = m.result, err = r && typeof r === "object" && r.error;
        $("tool-line").textContent += ` ${err ? "✗ " + err : "✓ " + (typeof r === "string" ? r : JSON.stringify(r))}`.slice(0, 400);
        Home.done(m.name, !err);
        break;
      }
      case "reminders": renderReminders(m.items || []); break;
      case "alarms": renderAlarms(m.items || []); break;
      case "timers": Timers.render(m.items || []); break;
      case "reminder_created": Sound.chime(); toast(`Reminder set: ${m.text} · ${fmtDue(m.due_at)}`); break;
      case "reminder_due": Sound.chime(); Sound.chime(); toast(`Reminder: ${m.text}`, "warn"); break;
      case "alarm_created": Sound.chime(); toast(`Alarm set: ${m.label || "Alarm"} at ${fmt12(m.time)}`); break;
      case "alarm_fired": ringStart(m.id, m.label, "alarm"); break;
      case "alarm_stopped": if (isRinging("alarm", m.id)) ringStop(false); break;
      case "alarm_deleted": if (isRinging("alarm", m.id)) ringStop(false); break;
      case "timer_created": Sound.chime(); toast(`Timer: ${Timers.fmt(m.duration_s)}${m.label ? " · " + m.label : ""}`); break;
      case "timer_fired": ringStart(m.id, m.label ? `${m.label} timer` : `${Timers.fmt(m.duration_s)} timer done`, "timer"); Timers.refresh(); break;
      case "timer_stopped": if (isRinging("timer", m.id)) ringStop(false); Timers.refresh(); break;
      case "timer_cancelled": toast("Timer cancelled"); Timers.refresh(); break;
      case "home": Home.update(m); break;
      case "weather": Home.weather(m); break;
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

  // ---------- chat box (this session's conversation) ----------
  const Chat = {
    els: new Map(), items: new Map(),
    box: $("chat"),
    nearBottom() { const b = this.box; return b.scrollHeight - b.scrollTop - b.clientHeight < 40; },
    scroll(force) { if (force || this.follow) this.box.scrollTop = this.box.scrollHeight; },
    reset(items) {
      for (const el of this.els.values()) el.remove();
      this.els.clear(); this.items.clear();
      for (const it of items) this.upsert(it, true);
      this.empty(); this.scroll(true);
    },
    empty() { $("chat-empty").classList.toggle("hidden", this.items.size > 0 || !$("chat-live").classList.contains("hidden")); },
    upsert(it, quiet) {
      if (!it || it.id == null) return;
      this.follow = this.nearBottom();
      let el = this.els.get(it.id);
      if (!el) {
        el = document.createElement("div");
        el.innerHTML = '<div class="msg-text"></div><div class="msg-meta"></div>';
        this.box.insertBefore(el, $("chat-live"));
        this.els.set(it.id, el);
        if (this.els.size > 300) { const [k, old] = this.els.entries().next().value; old.remove(); this.els.delete(k); this.items.delete(k); }
      }
      this.items.set(it.id, it);
      el.className = `msg msg-${it.role}` + (it.interrupted ? " cut" : "") + (it.role === "tool" && it.ok === false ? " fail" : "");
      const time = it.t ? new Date(it.t * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" }) : "";
      el.querySelector(".msg-text").textContent = it.role === "tool" ? `⚙ ${it.text} ${it.ok === false ? "✗" : "✓"}` : it.text;
      el.querySelector(".msg-meta").textContent = time + (it.interrupted ? " · interrupted" : "");
      if (it.role === "user") this.live(null);
      this.empty(); if (!quiet) this.scroll();
    },
    live(text) {
      const el = $("chat-live");
      this.follow = this.nearBottom();
      if (!text) el.classList.add("hidden");
      else { el.querySelector(".msg-text").textContent = text; el.classList.remove("hidden"); }
      this.empty(); this.scroll();
    },
    text() {
      const who = { user: "You", assistant: "Assistant", tool: "Tool" };
      return [...this.items.values()].map(it => {
        const t = it.t ? new Date(it.t * 1000).toLocaleTimeString() : "";
        return `[${t}] ${who[it.role] || it.role}: ${it.role === "tool" ? it.text + (it.ok === false ? " (failed)" : "") : it.text}${it.interrupted ? " [interrupted]" : ""}`;
      }).join("\n");
    },
  };
  $("chat-clear").onclick = () => { send({ type: "chat_clear" }); Chat.reset([]); };
  $("chat-copy").onclick = async () => {
    try { await navigator.clipboard.writeText(Chat.text()); toast("Conversation copied"); }
    catch { toast("Copy failed (clipboard not allowed here)", "warn"); }
  };

  // ---------- level meters (dBFS, -60..0) ----------
  const Meters = {
    MIN: -60, peaks: { mic: -90, out: -90 }, last: 0,
    pct(db) { return Math.max(0, Math.min(1, (db - this.MIN) / -this.MIN)); },
    set(key, db) {
      const now = performance.now(), dt = (now - (this["t_" + key] || now)) / 1000; this["t_" + key] = now;
      this.peaks[key] = Math.max(db, this.peaks[key] - 12 * dt);  // peak hold falls 12 dB/s
      $(`${key}-cover`).style.transform = `scaleX(${1 - this.pct(db)})`;
      $(`${key}-peak`).style.left = `${this.pct(this.peaks[key]) * 100}%`;
      $(`${key}-db`).textContent = db <= -89 ? "silent" : `${Math.round(db)} dB`;
    },
    update(m) {
      this.last = performance.now();
      this.set("mic", Math.max(m.mic_db, -90));
      this.set("out", Math.max(m.out_db, -90));
    },
    stale() {
      if (performance.now() - this.last < 1500) return;
      for (const k of ["mic", "out"]) { $(`${k}-cover`).style.transform = "scaleX(1)"; $(`${k}-peak`).style.left = "0"; $(`${k}-db`).textContent = "—"; }
    },
  };
  setInterval(() => Meters.stale(), 1000);

  // ---------- internet status ----------
  const Net = {
    online: null,
    RANK: { good: 0, fair: 1, poor: 2, down: 3, unknown: -1 },
    update(m) {
      const rows = m.targets || [];
      const worst = rows.reduce((w, r) => (this.RANK[r.state] > this.RANK[w] ? r.state : w), "unknown");
      const pill = $("net");
      const short = rows.map(r => `${r.name === "llm" ? "LLM" : r.name === "deepgram" ? "DG" : r.name} ${r.state === "down" ? "✗" : r.rtt_ms != null ? Math.round(r.rtt_ms) + " ms" : "?"}`).join(" · ");
      pill.textContent = m.online ? `🌐 ${short}` : "🌐 offline";
      pill.className = `pill net-pill net-${m.online ? worst : "down"}`;
      pill.title = rows.map(r => `${r.label}: ${r.state}${r.rtt_ms != null ? `, round trip ${r.rtt_ms} ms` : ""}${r.error ? ` (${r.error})` : ""}`).join("\n");
      if (this.online === true && !m.online) toast("Internet lost: the cloud providers cannot be reached", "err");
      else if (this.online === false && m.online) toast("Internet back");
      this.online = !!m.online;
      const box = $("net-rows"); box.innerHTML = "";
      for (const r of rows) {
        const d = document.createElement("div"); d.className = `net-row net-${r.state}`;
        const f = (v) => (v == null ? "—" : `${Math.round(v)}`);
        d.innerHTML = `<div class="net-label"><span class="dot"></span><span class="l"></span><span class="s"></span></div>
          <div class="hint">round trip ${f(r.rtt_ms)} ms · DNS ${f(r.dns_ms)} · TCP ${f(r.tcp_ms)} · TLS ${f(r.tls_ms)} ms · ${r.age_s != null ? Math.round(r.age_s) + " s ago" : ""}</div>`;
        d.querySelector(".l").textContent = r.label; d.querySelector(".s").textContent = r.state;
        if (r.error) { const e = document.createElement("div"); e.className = "hint err"; e.textContent = r.error; d.appendChild(e); }
        box.appendChild(d);
      }
      const hs = m.handshakes || [];
      $("net-hs").innerHTML = hs.length ? "<div class=\"hint\"><b>Recent provider connects</b></div>" : "";
      for (const h of hs.slice().reverse()) {
        const d = document.createElement("div"); d.className = "hint" + (h.ms >= 2000 || h.ok === false ? " err" : "");
        d.textContent = `${new Date(h.t * 1000).toLocaleTimeString()} · ${h.provider}${h.reconnect ? " reconnect" : ""} ${h.ok === false ? "failed after" : "in"} ${Math.round(h.ms)} ms`;
        $("net-hs").appendChild(d);
      }
    },
  };
  $("net").onclick = (e) => { e.stopPropagation(); $("net-panel").classList.toggle("hidden"); };
  $("net-panel").onclick = (e) => e.stopPropagation();
  document.addEventListener("click", () => $("net-panel").classList.add("hidden"));
  $("net-check").onclick = () => { send({ type: "net_check" }); $("net").textContent = "🌐 checking…"; };

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

  // ---------- timers (countdown rings; the backend rings them like alarms) ----------
  const Timers = {
    items: [], els: new Map(), R: 26, C: 2 * Math.PI * 26,
    fmt(s) { s = Math.max(0, Math.round(s || 0)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
      return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`; },
    remaining(t) { return t.state === "running" ? Math.max(0, (new Date(t.ends_at) - Date.now()) / 1000) : 0; },
    refresh() { this.render(this.items); },
    render(items) {
      this.items = items.filter(t => t.state === "running" || t.state === "ringing");
      const box = $("timers"); box.innerHTML = ""; this.els.clear();
      $("timer-count").textContent = this.items.length ? `(${this.items.length})` : "";
      if (!this.items.length) { box.innerHTML = '<div class="empty">No timers. Try: "set a timer for 5 minutes".</div>'; return; }
      for (const t of this.items) {
        const el = document.createElement("div");
        el.className = `timer ${t.state}` + (seenIds.has("t" + t.id) ? "" : " new");
        el.innerHTML = `<svg class="tring" viewBox="0 0 60 60"><circle class="tring-track" cx="30" cy="30" r="${this.R}"/><circle class="tring-fill" cx="30" cy="30" r="${this.R}" stroke-dasharray="${this.C}" stroke-dashoffset="0"/></svg>
          <div class="timer-main"><div class="timer-left"></div><div class="timer-sub"></div></div>
          <div class="item-actions">${t.state === "ringing" ? '<button class="btn btn-sm btn-danger" data-act="timer_stop">Stop</button>' : '<button class="btn btn-sm" data-act="timer_cancel">✕</button>'}</div>`;
        el.querySelector(".timer-sub").textContent = t.label || `${this.fmt(t.duration_s)} timer`;
        el.querySelector("[data-act]").onclick = (e) => { send({ type: e.target.dataset.act, id: t.id }); if (e.target.dataset.act === "timer_stop" && isRinging("timer", t.id)) ringStop(false); };
        box.appendChild(el); this.els.set(t.id, el); seenIds.add("t" + t.id);
        if (t.state === "ringing" && ringing === null) ringStart(t.id, t.label ? `${t.label} timer` : `${this.fmt(t.duration_s)} timer done`, "timer");
      }
      this.tick();
    },
    tick() {
      for (const t of this.items) {
        const el = this.els.get(t.id); if (!el) continue;
        const left = this.remaining(t), frac = t.duration_s ? left / t.duration_s : 0;
        el.querySelector(".timer-left").textContent = t.state === "ringing" ? "done" : this.fmt(left);
        el.querySelector(".tring-fill").style.strokeDashoffset = String(this.C * (1 - frac));
        el.classList.toggle("ending", t.state === "running" && left <= 10);
      }
    },
  };
  setInterval(() => Timers.tick(), 500);

  // ---------- the house: simulated devices, one widget each (events: home, weather, tool_call/result) ----------
  const Home = {
    state: null, media: null, mediaT0: 0, mediaPos0: 0, call: null,
    DEV: { control_music: "media", set_light: "lights", set_thermostat: "thermostat", send_message: "phone", make_call: "phone", end_call: "phone",
           get_weather: "weather", create_timer: "timers", cancel_timer: "timers", home_status: "all" },
    LABEL: { control_music: "music", set_light: "lights", set_thermostat: "thermostat", send_message: "sending", make_call: "calling", end_call: "hanging up",
             get_weather: "fetching weather", create_timer: "timer", cancel_timer: "timer", home_status: "reading the house" },
    SWATCHES: [["warm white", "#ffd9a0"], ["white", "#ffffff"], ["red", "#ff5a5a"], ["orange", "#ffa040"], ["yellow", "#ffe866"], ["green", "#5ad67d"], ["blue", "#5a8dff"], ["purple", "#a876ff"], ["pink", "#ff7ad9"]],
    ICON: { sun: "☀️", "cloud-sun": "⛅", cloud: "☁️", fog: "🌫️", drizzle: "🌦️", rain: "🌧️", snow: "❄️", storm: "⛈️" },
    devs(name) { return name === "all" ? [...document.querySelectorAll(".dev-anchor")] : [...document.querySelectorAll(`.dev-anchor[data-dev="${name}"]`)]; },
    // a tool call for a device: the widget shows "executing …" until the result arrives, then flashes ok / fail
    busy(tool, args) {
      const dev = this.DEV[tool]; if (!dev) return;
      let label = this.LABEL[tool] || tool;
      if (tool === "control_music" && args && args.action) label = `${args.action}${args.query ? " " + args.query : ""}`;
      if (tool === "set_light" && args) label = `${args.room || "all"}${args.on === false ? " off" : args.on ? " on" : ""}${args.brightness != null ? " " + args.brightness + "%" : ""}${args.color ? " " + args.color : ""}`;
      if (tool === "set_thermostat" && args) label = `${args.target != null ? args.target + "°" : ""} ${args.mode || ""}`.trim();
      for (const el of this.devs(dev)) { el.classList.remove("ok", "fail"); el.classList.add("busy"); el.querySelector(".dev-busy").textContent = `⟳ ${label}`; }
    },
    done(tool, ok) {
      const dev = this.DEV[tool]; if (!dev) return;
      for (const el of this.devs(dev)) {
        el.classList.remove("busy"); el.classList.add(ok ? "ok" : "fail");
        el.querySelector(".dev-busy").textContent = ok ? "✓ done" : "✗ failed";
        clearTimeout(el._flash); el._flash = setTimeout(() => { el.classList.remove("ok", "fail"); el.querySelector(".dev-busy").textContent = ""; }, 1600);
      }
    },
    update(m) {
      this.state = m;
      const b = m.backends || {};
      $("home-backends").textContent = Object.entries(b).map(([k, v]) => `${k}: ${v}`).join(" · ") || "simulated devices";
      this.lights(m.lights || [], m.transition_ms);
      this.thermostat(m.thermostat);
      this.player(m.media);
      this.phone(m.phone);
      if (m.weather) this.weather(m.weather);
    },
    // ---- lights: one tile per room, the bulb glows with the colour and brightness
    lights(items, transition) {
      const box = $("lights"); const open = box.querySelector(".light.editing");
      box.innerHTML = "";
      for (const l of items) {
        const tile = document.createElement("div");
        tile.className = `light ${l.on ? "on" : "off"}`;
        const alpha = l.on ? Math.max(0.15, l.brightness / 100) : 0;
        this.paintLight(tile, l.color, alpha);
        tile.innerHTML = `<div class="bulb-wrap"><svg class="bulb" viewBox="0 0 48 64" aria-hidden="true">
            <path class="bulb-glass" d="M24 4c-10.5 0-18 7.8-18 17.6 0 6.3 3.2 10.4 6.2 14.1 2.3 2.8 4.2 5.4 4.6 8.8h14.4c.4-3.4 2.3-6 4.6-8.8C38.8 32 42 27.9 42 21.6 42 11.8 34.5 4 24 4z"/>
            <rect class="bulb-base" x="16" y="46" width="16" height="4" rx="1.5"/><rect class="bulb-base" x="16" y="52" width="16" height="4" rx="1.5"/><path class="bulb-base" d="M19 58h10l-2 4h-6z"/>
            <path class="bulb-fil" d="M19 40c0-6 2-9 5-9s5 3 5 9" fill="none"/></svg></div>
          <div class="light-main"><div class="light-room"></div><div class="light-sub"></div>
            <input type="range" class="light-range" min="0" max="100" value="${l.brightness}" title="Brightness">
            <div class="swatches"></div></div>
          <button class="toggle ${l.on ? "on" : ""}" title="Switch"></button>`;
        tile.querySelector(".light-room").textContent = l.room.replace(/\b\w/g, c => c.toUpperCase());
        tile.querySelector(".light-sub").textContent = l.on ? `${l.brightness}% · ${l.color_name}` : "off";
        tile.querySelector(".toggle").onclick = () => send({ type: "light_set", room: l.room, on: !l.on });
        const range = tile.querySelector(".light-range");
        range.onchange = () => send({ type: "light_set", room: l.room, brightness: Number(range.value) });
        range.oninput = () => { tile.querySelector(".light-sub").textContent = `${range.value}%`; this.paintLight(tile, l.color, Math.max(0.15, range.value / 100)); };
        const sw = tile.querySelector(".swatches");
        for (const [name, hex] of this.SWATCHES) {
          const s = document.createElement("button"); s.className = "swatch" + (l.color === hex ? " sel" : ""); s.title = name; s.style.background = hex;
          s.onclick = () => send({ type: "light_set", room: l.room, color: name }); sw.appendChild(s);
        }
        if (transition) tile.querySelector(".bulb-wrap").style.transitionDuration = `${transition}ms`;
        box.appendChild(tile);
      }
      if (!items.length) box.innerHTML = '<div class="empty">No lights configured (LIGHT_ROOMS in .env).</div>';
    },
    // colour maths in JS (no CSS color-mix: the Pi's WebKit may not have it)
    mix(hex, base, t) {
      const h = (x) => [parseInt(x.slice(1, 3), 16), parseInt(x.slice(3, 5), 16), parseInt(x.slice(5, 7), 16)];
      const a = h(hex), b = h(base); return `rgb(${a.map((v, i) => Math.round(b[i] + (v - b[i]) * t)).join(",")})`;
    },
    paintLight(tile, color, alpha) {
      tile.style.setProperty("--glow", alpha ? color : "#000"); tile.style.setProperty("--alpha", alpha);
      tile.style.setProperty("--glass", this.mix(color, "#3a445c", alpha));
      tile.style.setProperty("--edge", this.mix(color, "#273146", 0.35)); tile.style.setProperty("--tint", this.mix(color, "#171e2e", 0.06));
    },
    // ---- thermostat: a 270° dial, the arc goes to the target, the dot shows the room now
    arc(cx, cy, r, a0, a1) {
      const p = (a) => [cx + r * Math.cos((a - 90) * Math.PI / 180), cy + r * Math.sin((a - 90) * Math.PI / 180)];
      const [x0, y0] = p(a0), [x1, y1] = p(a1); const large = a1 - a0 > 180 ? 1 : 0;
      return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
    },
    thermostat(t) {
      if (!t) return;
      const A0 = -135, A1 = 135, R = 74;
      const ang = (v) => A0 + (A1 - A0) * Math.max(0, Math.min(1, (v - t.min) / (t.max - t.min)));
      $("dial-track").setAttribute("d", this.arc(90, 90, R, A0, A1));
      $("dial-value").setAttribute("d", this.arc(90, 90, R, A0, Math.max(A0 + 0.5, ang(t.target))));
      const a = (ang(t.current) - 90) * Math.PI / 180;
      $("dial-now").setAttribute("cx", (90 + R * Math.cos(a)).toFixed(2)); $("dial-now").setAttribute("cy", (90 + R * Math.sin(a)).toFixed(2));
      $("th-target").innerHTML = `${Math.round(t.target)}<span class="th-unit">°${t.unit}</span>`;
      $("th-now").textContent = `now ${t.current.toFixed(1)}°`;
      const act = { heating: "🔥 heating", cooling: "❄ cooling", idle: "✓ at temperature", off: "off" }[t.activity] || t.activity;
      $("th-activity").textContent = act;
      const dev = document.querySelector('.dev[data-dev="thermostat"]');
      dev.className = dev.className.replace(/\bth-\w+/g, "").trim() + ` th-${t.activity} th-mode-${t.mode}`;
      document.querySelectorAll(".mode").forEach(b => b.classList.toggle("sel", b.dataset.mode === t.mode));
      this.thermo = t;
    },
    // ---- music: the disc spins while playing; the bar runs from the snapshot's position
    player(p) {
      if (!p) return;
      this.media = p; this.mediaT0 = performance.now(); this.mediaPos0 = p.position_s || 0;
      const tr = p.track, playing = p.state === "playing";
      const disc = $("disc");
      disc.style.background = tr ? `conic-gradient(from 0deg, ${tr.art[0]}, ${tr.art[1]}, ${tr.art[0]})` : "";
      disc.classList.toggle("spin", playing); disc.classList.toggle("has-art", !!tr);
      $("np-title").textContent = tr ? tr.title : "Nothing playing";
      $("np-artist").textContent = tr ? tr.artist : 'Try: "play some jazz" or "play my focus playlist"';
      const chip = (id, text) => { const el = $(id); el.textContent = text || ""; el.classList.toggle("hidden", !text); };
      chip("np-genre", tr && tr.genre); chip("np-source", p.source && p.queue_length ? `${p.source} · ${p.queue_index + 1}/${p.queue_length}` : "");
      chip("np-state", p.state === "paused" ? "paused" : p.state === "stopped" && tr ? "stopped" : "");
      $("eq").classList.toggle("on", playing);
      $("btn-play").textContent = playing ? "⏸" : "▶"; $("btn-play").dataset.media = playing ? "pause" : "resume";
      $("btn-shuffle").classList.toggle("sel", !!p.shuffle); $("btn-shuffle").dataset.media = p.shuffle ? "shuffle_off" : "shuffle_on";
      $("btn-repeat").classList.toggle("sel", p.repeat !== "off"); $("btn-repeat").textContent = p.repeat === "one" ? "🔂" : "🔁";
      $("btn-repeat").dataset.media = p.repeat === "off" ? "repeat_all" : p.repeat === "all" ? "repeat_one" : "repeat_off";
      $("btn-mute").textContent = p.muted ? "🔇" : p.volume === 0 ? "🔈" : p.volume < 50 ? "🔉" : "🔊"; $("btn-mute").dataset.media = p.muted ? "unmute" : "mute";
      if (document.activeElement !== $("vol")) $("vol").value = p.volume; $("vol-val").textContent = p.muted ? "muted" : p.volume;
      $("np-dur").textContent = tr ? Timers.fmt(tr.duration_s) : "0:00";
      $("up-next").innerHTML = (p.up_next || []).length ? "<span class=\"hint\">Up next:</span> " + p.up_next.map(t => `<span class="next-item"></span>`).join("<span class=\"hint\"> · </span>") : "";
      $("up-next").querySelectorAll(".next-item").forEach((el, i) => el.textContent = `${p.up_next[i].title} — ${p.up_next[i].artist}`);
      this.playerTick();
    },
    playerTick() {
      const p = this.media; if (!p || !p.track) { $("np-fill").style.width = "0%"; $("np-pos").textContent = "0:00"; return; }
      const pos = Math.min(p.track.duration_s, this.mediaPos0 + (p.state === "playing" ? (performance.now() - this.mediaT0) / 1000 : 0));
      $("np-fill").style.width = `${(pos / p.track.duration_s) * 100}%`; $("np-pos").textContent = Timers.fmt(pos);
    },
    // ---- phone: the call card and the message thread
    phone(ph) {
      if (!ph) return;
      const sel = $("contact");
      if (sel.options.length !== (ph.contacts || []).length) { sel.innerHTML = ""; for (const c of ph.contacts || []) { const o = document.createElement("option"); o.value = o.textContent = c; sel.appendChild(o); } }
      const c = ph.call, box = $("call"), active = c && (c.state === "ringing" || c.state === "connected");
      box.className = `call call-${c ? c.state : "none"}`;
      $("call-avatar").textContent = c ? c.contact.split(/\s+/).map(w => w[0]).join("").slice(0, 2).toUpperCase() : "—";
      $("call-name").textContent = c ? c.contact : "No call";
      $("call-hangup").classList.toggle("hidden", !active);
      const wasRinging = this.call && this.call.state === "ringing";
      if (c && c.state === "ringing") { if (!wasRinging) Sound.callRingStart(); }
      else { Sound.callRingStop(); if (wasRinging && c && c.state === "connected") Sound.pop(); }
      this.call = c ? { ...c } : null; this.callTick();
      const list = $("messages"); list.innerHTML = "";
      const msgs = (ph.messages || []).slice(-6);
      if (!msgs.length) list.innerHTML = '<div class="empty">No messages yet. Try: "text Alex that I\'m running late".</div>';
      for (const m of msgs) {
        const el = document.createElement("div"); el.className = `sms ${m.state}` + (seenIds.has("m" + m.id) ? "" : " new");
        el.innerHTML = `<div class="sms-to"></div><div class="sms-text"></div><div class="sms-meta"><span class="sms-time"></span><span class="ticks"></span></div>`;
        el.querySelector(".sms-to").textContent = `To ${m.contact}`; el.querySelector(".sms-text").textContent = m.text;
        el.querySelector(".sms-time").textContent = new Date(m.t * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
        el.querySelector(".ticks").textContent = m.state === "delivered" ? "✓✓ delivered" : "✓ sent";
        if (m.state === "delivered" && this.delivered && !this.delivered.has(m.id) && seenIds.has("m" + m.id)) Sound.pop();
        list.appendChild(el); seenIds.add("m" + m.id);
      }
      this.delivered = new Set(msgs.filter(m => m.state === "delivered").map(m => m.id));
    },
    callTick() {
      const c = this.call; if (!c) { $("call-status").textContent = 'Try: "call Mom"'; return; }
      if (c.state === "ringing") $("call-status").textContent = "Calling…";
      else if (c.state === "connected") $("call-status").textContent = `Connected · ${Timers.fmt(Date.now() / 1000 - c.connected_at)}`;
      else $("call-status").textContent = `Call ended${c.duration_s ? " · " + Timers.fmt(c.duration_s) : ""}`;
    },
    // ---- weather: the last answer from get_weather
    weather(w) {
      if (!w || w.error) return;
      const box = $("weather"); box.classList.remove("weather-empty");
      const n = w.now || {}; const icon = this.ICON[n.icon] || "🌤";
      $("wx-icon").textContent = n.icon === "sun" && n.is_day === false ? "🌙" : icon;
      $("wx-temp").textContent = n.temperature != null ? `${Math.round(n.temperature)}°${w.unit || "C"}` : "--°";
      $("wx-cond").textContent = n.conditions || "";
      $("wx-city").textContent = w.city || "";
      $("wx-details").innerHTML = "";
      for (const [k, v] of [["feels like", n.feels_like != null ? `${Math.round(n.feels_like)}°` : null], ["humidity", n.humidity != null ? `${n.humidity}%` : null], ["wind", n.wind_kmh != null ? `${Math.round(n.wind_kmh)} km/h` : null]]) {
        if (v == null) continue; const d = document.createElement("div"); d.className = "wx-detail"; d.innerHTML = `<span class="hint"></span><b></b>`;
        d.querySelector(".hint").textContent = k; d.querySelector("b").textContent = v; $("wx-details").appendChild(d);
      }
      const days = $("wx-days"); days.innerHTML = "";
      for (const d of w.days || []) {
        const el = document.createElement("div"); el.className = "wx-day" + (w.asked_for && w.asked_for.date === d.date ? " sel" : "");
        el.innerHTML = `<div class="hint"></div><div class="wx-day-icon"></div><div class="wx-day-temps"></div><div class="hint wx-rain"></div>`;
        el.querySelector(".hint").textContent = d.label === "day after tomorrow" ? "in 2 days" : d.label;
        el.querySelector(".wx-day-icon").textContent = this.ICON[d.icon] || "🌤";
        el.querySelector(".wx-day-temps").innerHTML = `<b>${Math.round(d.high)}°</b> <span class="hint">${Math.round(d.low)}°</span>`;
        el.querySelector(".wx-rain").textContent = d.rain_chance != null ? `☂ ${d.rain_chance}%` : "";
        days.appendChild(el);
      }
    },
  };
  setInterval(() => { Home.playerTick(); Home.callTick(); }, 500);
  document.querySelectorAll("[data-media]").forEach(b => b.onclick = () => { Sound.ensure(); send({ type: "media", action: b.dataset.media }); });
  $("vol").oninput = () => { $("vol-val").textContent = $("vol").value; };
  $("vol").onchange = () => send({ type: "media", action: "set_volume", value: Number($("vol").value) });
  $("th-minus").onclick = () => { if (Home.thermo) send({ type: "thermostat_set", target: Math.round(Home.thermo.target) - 1 }); };
  $("th-plus").onclick = () => { if (Home.thermo) send({ type: "thermostat_set", target: Math.round(Home.thermo.target) + 1 }); };
  document.querySelectorAll(".mode").forEach(b => b.onclick = () => send({ type: "thermostat_set", mode: b.dataset.mode }));
  $("btn-call").onclick = () => { Sound.ensure(); send({ type: "phone", action: "call", contact: $("contact").value }); };
  $("call-hangup").onclick = () => send({ type: "phone", action: "hang_up" });
  $("btn-send").onclick = () => { const t = $("msg-text").value.trim(); if (!t) return; send({ type: "phone", action: "message", contact: $("contact").value, text: t }); $("msg-text").value = ""; };
  $("msg-text").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btn-send").onclick(); e.stopPropagation(); });

  // ---------- controls ----------
  $("interrupt").onclick = () => send({ type: "interrupt" });
  $("mute").onclick = () => {
    Sound.muted = !Sound.muted;
    if (Sound.muted) { Sound.alarmStop(); Sound.callRingStop(); } else if (ringing !== null) Sound.alarmStart();
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
