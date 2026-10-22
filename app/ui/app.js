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
  function toast(msg, cls = "") {
    const el = document.createElement("div");
    el.className = `toast ${cls}`; el.textContent = msg;
    $("toasts").appendChild(el);
    setTimeout(() => el.remove(), 4500);
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
      case "tool_result": $("tool-line").textContent += ` → ${typeof m.result === "string" ? m.result : JSON.stringify(m.result)}`; break;
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
      case "info": if (m.stub) { $("conn").textContent = "● stub backend"; $("conn").className = "pill pill-stub"; } break;
    }
  }
  connect();

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
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" || e.key === " ") ringStop(true); });
})();
