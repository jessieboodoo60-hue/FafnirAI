/* Fafnir desktop apps. Each app renders into a window from WM (desktop.js) using live data
   from Python (S.data, S.notices, the conversation). Actions go back through `call(...)`. */
"use strict";

const APPS = {
  fafnir:   { name: "Fafnir", icon: "fafnir", keywords: "assistant chat talk orb conversation" },
  mail:     { name: "Mail", icon: "mail", keywords: "email inbox gmail" },
  calendar: { name: "Calendar", icon: "calendar", keywords: "events schedule meetings agenda" },
  tasks:    { name: "Tasks", icon: "tasks", keywords: "todo to-do deadlines business" },
  notices:  { name: "Notices", icon: "notices", keywords: "notifications alerts heartbeat" },
  files:    { name: "Files", icon: "files", keywords: "folders documents explorer" },
  business: { name: "Business", icon: "business", keywords: "youtube social stats channel clients" },
  settings: { name: "Settings", icon: "settings", keywords: "preferences wallpaper about" },
  viewer:   { name: "Viewer", icon: "viewer", keywords: "" },
};
const APP_ORDER = ["fafnir", "|", "mail", "calendar", "tasks", "notices", "files", "business", "|", "settings"];
const PANEL_TO_APP = { today: "calendar", tasks: "tasks", notices: "notices", email: "mail", business: "business", activity: "fafnir" };

/* ---------- helpers ---------- */
const fmtTime = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }); };
const localDay = (s) => { if (/^\d{4}-\d\d-\d\d$/.test(s || "")) return s; const d = new Date(s); if (isNaN(d)) return (s || "").slice(0, 10);
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); };
const fmtDay = (iso) => { const m = /^(\d{4})-(\d\d)-(\d\d)$/.exec(iso || ""); const d = m ? new Date(+m[1], m[2] - 1, +m[3]) : new Date(iso); if (isNaN(d)) return iso; const t = new Date(); const diff = Math.round((new Date(d.toDateString()) - new Date(t.toDateString())) / 864e5);
  return diff === 0 ? "Today" : diff === 1 ? "Tomorrow" : d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "short" }); };
const todayISO = () => { const d = new Date(); return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); };
const senderName = (s) => (s || "").split("<")[0].trim().replace(/^"|"$/g, "") || s || "";
function fileIcon(p) { const e = (p || "").toLowerCase().split(".").pop(); return ["png", "jpg", "jpeg", "gif", "webp", "bmp"].includes(e) ? "image" : ["exe", "lnk", "msi", "bat"].includes(e) ? "app" : "file"; }
function looksLikeFolder(p) { return !/\.[a-z0-9]{1,5}$/i.test(p || ""); }
function openPathSmart(p) { if (!p) return; looksLikeFolder(p) ? openApp("files", { path: p, multi: true }) : call("openPath", p); }
function md(text) {  // small, safe markdown for the Viewer
  let s = esc(text);
  s = s.replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>").replace(/^# (.*)$/gm, "<h1>$1</h1>")
       .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>")
       .replace(/^\s*[-*] (.*)$/gm, "<li>$1</li>").replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, "<ul>$1</ul>");
  return s.replace(/\n{2,}/g, "<br><br>").replace(/\n/g, "<br>");
}

/* ---------- opening apps ---------- */
function openApp(id, opts = {}) {
  const app = APPS[id]; if (!app) return;
  if (!SOLO && S.popped.includes(id) && !opts.multi && !opts.path) { call("soloFocus", id); return; }  // it's on another screen
  const isNew = !WM.wins.has(id) || opts.multi;
  const w = WM.open({ id: opts.multi ? id + ":" + Date.now() : id, app: id, title: opts.title || app.name, multi: !!opts.multi,
    ...(SIZES[id] || {}), flush: ["fafnir", "files"].includes(id),
    render: (w) => { w.state = { path: opts.path }; RENDER[id](w); },
    update: (w) => { if (opts.path && id === "files") { Files.go(w, opts.path); } },
    onClose: (w) => { if (id === "fafnir") Orb.stop(); },
  });
  if (isNew) Dock.bounce(id);
  return w;
}
const SIZES = { fafnir: { w: 620, h: 720 }, mail: { w: 760, h: 560 }, calendar: { w: 640, h: 600 }, tasks: { w: 600, h: 560 },
  notices: { w: 520, h: 600 }, files: { w: 900, h: 580 }, business: { w: 760, h: 560 }, settings: { w: 640, h: 560 } };

function refreshOpen(...ids) { ids.forEach((id) => WM.wins.forEach((w) => { if (w.app === id && RENDER[id] && id !== "fafnir" && id !== "files") RENDER[id](w); })); }

/* ---------- the orb (port of the desktop app's animated core) ---------- */
const Orb = {
  canvas: null, raf: 0, level: 0, color: [176, 196, 232], angle: 0, last: 0,
  COLORS: { idle: [176, 196, 232], listening: [110, 205, 150], thinking: [228, 178, 104], speaking: [130, 168, 245], confirming: [232, 104, 100] },
  start(canvas) { this.canvas = canvas; if (!this.raf) this.raf = requestAnimationFrame((t) => this.frame(t)); },
  stop() { cancelAnimationFrame(this.raf); this.raf = 0; this.canvas = null; },
  frame(now) {
    const c = this.canvas; if (!c || !c.isConnected) { this.raf = 0; return; }
    const dt = Math.min(0.05, (now - (this.last || now)) / 1000); this.last = now;
    const dpr = devicePixelRatio || 1, W = c.clientWidth, H = c.clientHeight;
    if (c.width !== W * dpr) { c.width = W * dpr; c.height = H * dpr; }
    const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
    const target = this.COLORS[S.state] || this.COLORS.idle, k = Math.min(1, dt * 6);
    this.color = this.color.map((v, i) => v + (target[i] - v) * k);
    this.level += (S.level - this.level) * Math.min(1, dt * (S.level > this.level ? 14 : 5));
    const spin = { idle: 0.15, listening: 0.5, thinking: 1.6, speaking: 0.6, confirming: 0.3 }[S.state] || 0.2;
    this.angle = (this.angle + dt * 90 * spin) % 360;
    const [r, gg, b] = this.color.map(Math.round), col = (a) => `rgba(${r},${gg},${b},${a})`;
    const cx = W / 2, cy = H / 2, R = Math.min(W, H) * 0.34, L = this.level, t = now / 1000;
    const breathe = 0.5 + 0.5 * Math.sin(t * (S.state === "thinking" ? 3 : 1.3));
    let grad = g.createRadialGradient(cx, cy, 0, cx, cy, R * 1.6);
    grad.addColorStop(0, col(0.22 + 0.25 * L)); grad.addColorStop(0.45, col(0.08 + 0.1 * L)); grad.addColorStop(1, col(0));
    g.fillStyle = grad; g.beginPath(); g.arc(cx, cy, R * 1.6, 0, Math.PI * 2); g.fill();
    [[0.72, 0.55], [0.88, 0.35], [1.04, 0.2], [1.22, 0.1]].forEach(([rr, a], i) => {
      const rad = R * rr + Math.sin(t * (1.2 + i * 0.4) + i) * R * 0.012 + L * R * (0.06 + i * 0.05);
      g.strokeStyle = col(a * (0.7 + 0.3 * breathe) + 0.2 * L); g.lineWidth = 1.6; g.beginPath(); g.arc(cx, cy, rad, 0, Math.PI * 2); g.stroke(); });
    for (let i = 0; i < 3; i++) {
      const rad = R * (0.96 + i * 0.1), dir = i % 2 ? -1 : 1, s = ((this.angle * (1 + i * 0.6) * dir + i * 120) % 360) * Math.PI / 180;
      g.strokeStyle = col(0.75 - i * 0.2); g.lineWidth = 3.2 - i * 0.7; g.lineCap = "round";
      g.beginPath(); g.arc(cx, cy, rad, s, s + (50 - i * 10) * Math.PI / 180); g.stroke();
      g.beginPath(); g.arc(cx, cy, rad, s + Math.PI, s + Math.PI + (24 - i * 6) * Math.PI / 180); g.stroke(); }
    const cr = R * (0.42 + 0.04 * breathe + 0.22 * L);
    grad = g.createRadialGradient(cx, cy, 0, cx, cy, cr);
    grad.addColorStop(0, "rgba(255,255,255,.92)"); grad.addColorStop(0.35, col(0.95)); grad.addColorStop(0.8, col(0.25)); grad.addColorStop(1, col(0));
    g.fillStyle = grad; g.beginPath(); g.arc(cx, cy, cr, 0, Math.PI * 2); g.fill();
    this.raf = requestAnimationFrame((tt) => this.frame(tt));
  },
};

/* ---------- conversation model ---------- */
const Chat = {
  msgs: [], current: null, caption: "",
  restore(conv) { this.msgs = conv.map((e) => ({ role: e.role === "user" ? "me" : "ai", text: e.text, time: e.time.slice(11, 16), old: true, tools: [] })); },
  add(role, text, extra = {}) { const m = { role, text, time: new Date().toTimeString().slice(0, 5), tools: [], ...extra }; this.msgs.push(m); if (this.msgs.length > 200) this.msgs.shift(); this.render(); return m; },
  render() { WM.wins.forEach((w) => { if (w.app === "fafnir") renderLog(w); }); },
};

function renderLog(w) {
  const log = w.body.querySelector(".fa-log"); if (!log) return;
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  log.innerHTML = Chat.msgs.map((m) => `<div class="msg ${m.role} ${m.old ? "old" : ""}"><div class="who">${m.role === "me" ? (m.spoken ? "You, spoken" : "You") + " · " + m.time : m.role === "ai" ? "<b>" + esc(S.name) + "</b> · " + m.time : m.time}</div>
    <div class="body">${esc(m.text)}</div>${m.tools.map((t) => `<div class="tool ${t.error ? "err" : ""}">${t.error ? "✗" : t.web ? "◌" : "✓"} ${esc(t.name.replace(/_/g, " "))}${t.detail ? " · " + esc(t.detail) : ""}</div>`).join("")}</div>`).join("");
  if (nearBottom) log.scrollTop = log.scrollHeight;
}

/* ---------- renderers ---------- */
const RENDER = {
  fafnir(w) {
    w.body.innerHTML = `<div class="fa"><div class="fa-top"><canvas class="fa-orb"></canvas><div class="fa-state"><div class="s1"></div><div class="s2"></div><div class="fa-caption"></div></div></div>
      <div class="fa-log"></div><div class="fa-input"><input placeholder="Type to ${esc(S.name)}, or hold ${esc(S.talkKey)}${S.wake ? ` or say “${esc(S.wake)}”` : ""}"></div></div>`;
    Orb.start(w.body.querySelector(".fa-orb"));
    const inp = w.body.querySelector("input");
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter" && inp.value.trim()) { const t = inp.value.trim(); inp.value = "";
      if (!$("#confirm").classList.contains("hidden")) { call("submitText", t); return; } Chat.add("me", t); call("submitText", t); } });
    renderLog(w); RENDER.fafnirState(w);
    setTimeout(() => { const l = w.body.querySelector(".fa-log"); l.scrollTop = l.scrollHeight; }, 50);
  },
  fafnirState(w) {
    const s1 = w.body.querySelector(".s1"), s2 = w.body.querySelector(".s2"); if (!s1) return;
    const st = S.state;
    s1.textContent = { idle: "Ready", listening: "Listening…", thinking: "Thinking…", speaking: "Speaking", confirming: "Needs your OK" }[st] || "Ready";
    s2.textContent = st === "idle" ? (S.voice ? (S.wake ? `Say “${S.wake}” or hold ${S.talkKey}` : `Hold ${S.talkKey} to talk`) : "Type below (voice is off)") : "Press Esc to stop";
    w.body.querySelector(".fa-caption").textContent = Chat.caption;
  },

  mail(w) {
    const d = S.data, mails = d.emails || [];
    const unread = mails.filter((m) => m.unread).length;
    w.body.innerHTML = d.google_missing ? `<div class="empty">Google isn't connected yet. Ask ${esc(S.name)} “what's on my calendar?” to connect.</div>` :
      `<div class="h">Inbox</div><div class="sub">${mails.length} recent · ${unread} unread</div>
       <div class="list">${mails.map((m, i) => `<div class="row ${m.unread ? "unread" : ""}" data-i="${i}"><div class="main"><div class="t">${esc(senderName(m.from))} — ${esc(m.subject || "(no subject)")}</div>
         <div class="st">${esc(m.snippet || "")}</div></div><div class="meta">${esc(shortDate(m.date))}</div></div>`).join("") || `<div class="empty">No email in the last week.</div>`}</div>`;
    w.body.querySelectorAll(".row").forEach((r) => { const m = mails[+r.dataset.i];
      const gmail = () => call("openUrl", "https://mail.google.com/mail/u/0/#inbox/" + m.id);
      r.ondblclick = gmail;
      r.oncontextmenu = (e) => { e.preventDefault(); Menu.show(e.clientX, e.clientY, [{ label: "Open in Gmail", fn: gmail },
        { label: `Ask ${S.name} to summarise`, fn: () => askFafnir(`Summarise the email from ${senderName(m.from)} about "${m.subject}" (id ${m.id}).`) },
        { label: `Ask ${S.name} to draft a reply`, fn: () => askFafnir(`Draft a reply to the email from ${senderName(m.from)} about "${m.subject}" (id ${m.id}). Save it as a draft, don't send.`) }]); };
      r.onclick = () => { w.body.querySelectorAll(".row").forEach((x) => x.classList.remove("sel")); r.classList.add("sel"); }; });
  },

  calendar(w) {
    const d = S.data, evs = d.events || [], now = new Date();
    const groups = {}; evs.forEach((e) => { const k = localDay(e.start); (groups[k] = groups[k] || []).push(e); });
    w.body.innerHTML = `<div class="h">${now.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })}</div>
      <div class="sub">${evs.length ? `${evs.length} event${evs.length === 1 ? "" : "s"} this week` : d.google_missing ? "Google isn't connected yet." : "Nothing scheduled this week."}</div>
      ${Object.keys(groups).sort().map((k) => `<div class="sec">${esc(fmtDay(k))}</div><div class="list">${groups[k].map((e) => {
        const past = !e.all_day && new Date(e.end || e.start) < now;
        return `<div class="row" style="${past ? "opacity:.5" : ""}"><div class="main"><div class="t">${esc(e.title)}</div><div class="st">${esc(e.location || "")}</div></div>
          <div class="meta">${e.all_day ? "All day" : esc(fmtTime(e.start)) + " – " + esc(fmtTime(e.end))}</div></div>`; }).join("")}</div>`).join("")}
      <div class="cc-row" style="margin-top:16px"><button class="btn" id="cal-open">Open Google Calendar</button><button class="btn" id="cal-ask">Ask ${esc(S.name)} to add an event</button></div>`;
    w.body.querySelector("#cal-open").onclick = () => call("openUrl", "https://calendar.google.com/");
    w.body.querySelector("#cal-ask").onclick = () => { openApp("fafnir"); setTimeout(() => { const i = $(".fa-input input"); if (i) { i.value = "Add an event: "; i.focus(); } }, 150); };
  },

  tasks(w) {
    const d = S.data, t = todayISO();
    const items = [...(d.tasks || []).map((x) => ({ ...x, src: "Business" })), ...(d.gtasks || []).filter((x) => !x.done).map((x) => ({ ...x, src: "Google Tasks" }))];
    items.sort((a, b) => (a.due || "9999").localeCompare(b.due || "9999"));
    w.body.innerHTML = `<div class="h">Tasks</div><div class="sub">${items.length} open</div>
      <input class="field" id="task-add" placeholder="Add a task… (e.g. “Send the Acme invoice by Friday”)">
      <div class="list" style="margin-top:12px">${items.map((x) => { const due = (x.due || "").slice(0, 10), cls = due && due < t ? "urgent" : due === t ? "important" : "";
        return `<div class="row"><div class="main"><div class="t ${cls}">${esc(x.title)}</div><div class="st">${esc(x.src)}${x.kind ? " · " + esc(x.kind) : ""}${x.client ? " · " + esc(x.client) : ""}</div></div>
          <div class="meta ${cls}">${due ? (due < t ? "overdue · " : "") + esc(fmtDay(due)) : "no date"}</div></div>`; }).join("") || `<div class="empty">Nothing open. Nice.</div>`}</div>`;
    const inp = w.body.querySelector("#task-add");
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter" && inp.value.trim()) { askFafnir(`Add this to my business tasks: ${inp.value.trim()}`); inp.value = ""; } });
    w.body.querySelectorAll(".row").forEach((r, i) => r.oncontextmenu = (e) => { e.preventDefault(); const x = items[i];
      Menu.show(e.clientX, e.clientY, [{ label: "Mark done", fn: () => askFafnir(`Mark the task "${x.title}" (${x.src}${x.id ? ", id " + x.id : ""}) as done.`) },
        { label: "Move to tomorrow", fn: () => askFafnir(`Move the task "${x.title}" (${x.src}${x.id ? ", id " + x.id : ""}) to tomorrow.`) }]); });
  },

  notices(w) {
    const list = S.notices;
    w.body.innerHTML = `<div class="h">Notices</div><div class="sub">${S.paused ? "Paused · nothing will interrupt you" : `${list.length} waiting`}</div>
      <div class="cc-row" style="margin:0 0 12px"><button class="btn" id="n-pause">${S.paused ? "Resume notices" : "Pause notices"}</button>${list.length > 1 ? `<button class="btn" id="n-clear">Clear all</button>` : ""}</div>
      <div class="list">${list.map((n) => `<div class="row"><div class="main"><div class="t ${n.urgency === "urgent" ? "urgent" : n.urgency === "important" ? "important" : ""}">${esc(n.title)}</div>
        <div class="st">${esc(n.time)}${n.detail ? " · " + esc(n.detail) : ""}</div></div><button class="x" data-id="${esc(n.id)}" title="Dismiss">✕</button></div>`).join("") || `<div class="empty">All clear.</div>`}</div>
      ${S.activity.length ? `<div class="sec">Recent activity</div><div class="list">${S.activity.slice(0, 8).map((a) => `<div class="row"><div class="main"><div class="t ${a.error ? "urgent" : ""}">${a.error ? "✗" : "✓"} ${esc(a.name.replace(/_/g, " "))}</div></div><div class="meta">${esc(a.time)}</div></div>`).join("")}</div>` : ""}`;
    w.body.querySelector("#n-pause").onclick = () => call("togglePause");
    w.body.querySelector("#n-clear")?.addEventListener("click", () => call("dismissNotice", "all"));
    w.body.querySelectorAll(".x").forEach((b) => (b.onclick = () => call("dismissNotice", b.dataset.id)));
  },

  business(w) {
    const d = S.data, yt = d.youtube, vids = d.videos || [], sys = d.system || {};
    const biz = (d.tasks || []).filter((x) => ["client", "deadline", "note"].includes(x.kind));
    const num = (v) => (v == null || v === "" ? "—" : Number(v).toLocaleString());
    w.body.innerHTML = `<div class="h">Business</div><div class="sub">Your channel, clients and this PC</div>
      <div class="cards">${yt ? `<div class="card"><div class="st">YouTube · ${esc(yt.title)}</div><div class="big">${num(yt.subscribers)}</div><div class="st">subscribers</div></div>
        <div class="card"><div class="st">Total views</div><div class="big">${num(yt.views)}</div><div class="st">${num(yt.videos)} videos</div></div>` : `<div class="card"><div class="t">YouTube</div><div class="st">${d.google_missing ? "Google isn't connected yet." : "No channel found."}</div></div>`}
        ${(sys.disks || []).map((k) => `<div class="card"><div class="st">Drive ${esc(k.drive)}</div><div class="big">${esc(k.free_gb)} GB</div><div class="st">free of ${esc(k.total_gb)} GB</div></div>`).join("")}</div>
      ${vids.length ? `<div class="sec">Latest videos</div><div class="list">${vids.map((v) => `<div class="row" data-id="${esc(v.id)}"><div class="main"><div class="t">${esc(v.title)}</div>
        <div class="st">${num(v.views)} views · ${num(v.likes)} likes · ${num(v.comments)} comments</div></div><div class="meta">${esc(v.published)}</div></div>`).join("")}</div>` : ""}
      <div class="sec">Clients, deadlines & notes</div><div class="list">${biz.map((x) => `<div class="row"><div class="main"><div class="t">${esc(x.title)}</div><div class="st">${esc(x.kind)}${x.due ? " · due " + esc(x.due) : ""}</div></div></div>`).join("") || `<div class="empty">Nothing tracked yet. Ask ${esc(S.name)} to add a client.</div>`}</div>`;
    w.body.querySelectorAll("[data-id]").forEach((r) => (r.ondblclick = () => call("openUrl", "https://www.youtube.com/watch?v=" + r.dataset.id)));
  },

  async settings(w) {
    if (!w.state.cfg) {
      w.body.innerHTML = `<div class="h">Settings</div><div class="sub">Loading…</div>`;
      const r = await request("getSettings");
      w.state.cfg = r.ok ? r.data : { values: {}, when: {}, error: r.error };
    }
    const C = w.state.cfg, v = C.values || {};
    const wp = store.get("wallpaper", "") || "aurora";
    const sw = [["aurora", "linear-gradient(180deg,#39405a,#8a6f78 55%,#d49a76 75%,#262a3b 76%)", "Dusk"], ["dusk", "linear-gradient(180deg,#0b0f1c,#253152 60%,#3d4a6e 75%,#141a30 76%)", "Night"],
                ["ocean", "linear-gradient(180deg,#43514e,#7d8b83 55%,#b6b9a6 75%,#333d3a 76%)", "Mist"], ["graphite", "linear-gradient(180deg,#1b1c20,#2a2b30)", "Graphite"]];
    const note = (k) => ({ restart: "after restart", conversation: "after a restart or /reset" }[C.when?.[k]] || "");
    const seg = (k, opts) => `<div class="seg" data-k="${k}">${opts.map(([val, label]) => `<button class="${v[k] === val ? "on" : ""}" data-v='${JSON.stringify(val)}'>${esc(label)}</button>`).join("")}</div>`;
    const sw_ = (k) => `<button class="switch ${v[k] ? "on" : ""}" data-k="${k}"></button>`;
    const row = (label, ctl, k) => `<div class="set-row"><span>${label}${k && note(k) ? `<div class="set-note">Applies ${note(k)}</div>` : ""}</span>${ctl}</div>`;
    w.body.innerHTML = `<div class="h">Settings</div><div class="sub">Changes save straight away${C.error ? " · " + esc(C.error) : ""}</div>
      ${C.restartNeeded ? `<div class="restart-bar"><span>Some changes apply after a restart.</span><button class="btn primary" id="set-restart">Restart ${esc(S.name)}</button></div>` : ""}
      <div class="sec">Brain</div><div class="set-group">
        ${row(`Where ${esc(S.name)} thinks<div class="set-note">Local = Qwen on this PC (private, free). Cloud = Claude.</div>`, seg("model.provider", [["ollama", "Local (Qwen)"], ["claude_code", "Cloud (Claude)"]]), "model.provider")}
        ${v["model.provider"] === "ollama" ? row(`Hand-offs to the cloud<div class="set-note">For pictures, web search and long jobs the local brain can't do.</div>`, seg("model.local.cloud_fallback", [["ask", "Ask me each time"], ["never", "Never"]]), "model.local.cloud_fallback") : ""}
        ${row(`Performance profile<div class="set-note">Auto frees the graphics card while a game runs.</div>`, seg("performance.profile", [["auto", "Auto"], ["gaming", "Gaming"], ["balanced", "Balanced"], ["ai_performance", "AI Performance"]]))}
        ${v["model.provider"] === "ollama" ? row("Requests while in Gaming", seg("performance.gaming_brain", [["ask", "Ask, then cloud"], ["cloud", "Cloud, no asking"], ["off", "Decline"]]), "performance.gaming_brain") : ""}
        ${row(v["model.provider"] === "ollama" ? "Cloud model (for hand-offs)" : "Model", seg("model.name", [["claude-sonnet-5-5", "Sonnet 5.5"], ["claude-opus-5-5", "Opus 5.5"], ["claude-haiku-5-5", "Haiku 5.5"]]), "model.name")}
        ${row("Thinking effort", seg("model.effort", [["low", "Low"], ["medium", "Medium"], ["high", "High"], ["xhigh", "Extra high"]]), "model.effort")}</div>
      <div class="sec">Voice</div><div class="set-group">
        ${row(`Speech recognition<div class="set-note">Local = Whisper on this PC (private, about half a second slower). Cloud = Deepgram.</div>`, seg("voice.stt.engine", [["local", "Local (Whisper)"], ["deepgram", "Cloud (Deepgram)"]]), "voice.stt.engine")}
        ${row(`On headphones, just start talking to cut ${esc(S.name)} off`, sw_("voice.barge_in"), "voice.barge_in")}
        ${row(`Wake word “${esc(S.name)}”`, sw_("voice.wake.enabled"), "voice.wake.enabled")}
        ${row(`Keep listening after ${esc(S.name)} speaks <span class="k" id="fu-val">${v["voice.wake.follow_up_seconds"] ?? 6}s</span>`, `<input type="range" class="slider set-slider" data-k="voice.wake.follow_up_seconds" min="0" max="30" step="1" value="${v["voice.wake.follow_up_seconds"] ?? 6}">`)}
        ${row(`Wake word sensitivity <span class="k" id="sens-val">${v["voice.wake.sensitivity"] ?? 0.4}</span>`, `<input type="range" class="slider set-slider" data-k="voice.wake.sensitivity" min="0.1" max="0.9" step="0.05" value="${v["voice.wake.sensitivity"] ?? 0.4}">`, "voice.wake.sensitivity")}
        ${row(`Say “${esc(S.name)}” to cut it off mid-sentence`, sw_("voice.wake.while_speaking"))}
        ${row("Beeps when listening starts and stops", sw_("voice.cue_sounds"))}
        ${row("Talk key", `<span class="k">Hold ${esc(S.talkKey)}</span>`)}</div>
      <div class="sec">Screen</div><div class="set-group">
        ${row("Wallpaper", `<div class="swatches">${sw.map(([k, g, n]) => `<button class="swatch ${wp === k ? "on" : ""}" data-wp="${k}" style="background:${g}" title="${n}"></button>`).join("")}</div>`)}
        ${row("Your own picture", `<input class="field set-text" data-k="app.background" placeholder="C:/Users/you/Pictures/wall.jpg" value="${esc(v["app.background"] || "")}" style="max-width:320px">`)}
        ${row("Monitor", seg("app.screen", [["right", "Right"], ["left", "Left"], ["above", "Above"], ["primary", "Main"]]), "app.screen")}</div>
      <div class="sec">Privacy · sent off this PC in the last 24 hours</div><div class="set-group">
        ${Object.keys(C.outbound || {}).length ? Object.entries(C.outbound).map(([to, d]) => row(`${esc(to)}<div class="set-note">${esc((d.why || []).join(", "))}</div>`, `<span class="k">${d.requests} request${d.requests === 1 ? "" : "s"}${d.kb ? " · " + d.kb + " KB" : ""}</span>`)).join("") : row("Nothing left this PC", `<span class="k">All local</span>`)}</div>
      <div class="sec">Notifications</div><div class="set-group">${row("Pause notices (do not disturb)", `<button class="switch ${S.paused ? "on" : ""}" id="set-dnd"></button>`)}</div>
      <div class="sec">About</div><div class="set-group">${row(esc(S.name), `<span class="k">Your assistant · runs on this PC</span>`)}
        ${row("Shortcuts", `<button class="btn" id="set-keys">Show</button>`)}</div>`;
    const save = async (k, val) => {
      const r = await request("saveSetting", k, JSON.stringify(val));
      if (!r.ok) { banner({ title: "Couldn't save that setting", detail: r.error, urgency: "important", app: "settings" }); return; }
      v[k] = r.data.value; C.restartNeeded = r.data.restartNeeded;
      RENDER.settings(w);
    };
    w.body.querySelectorAll(".seg button").forEach((b) => (b.onclick = () => save(b.parentElement.dataset.k, JSON.parse(b.dataset.v))));
    w.body.querySelectorAll(".switch[data-k]").forEach((b) => (b.onclick = () => save(b.dataset.k, !v[b.dataset.k])));
    w.body.querySelectorAll(".set-slider").forEach((s) => {
      const lbl = s.dataset.k.endsWith("seconds") ? "#fu-val" : "#sens-val";
      s.oninput = () => (w.body.querySelector(lbl).textContent = s.dataset.k.endsWith("seconds") ? s.value + "s" : s.value);
      s.onchange = () => save(s.dataset.k, +s.value); });
    w.body.querySelectorAll(".set-text").forEach((t) => t.addEventListener("keydown", (e) => { if (e.key === "Enter") save(t.dataset.k, t.value.trim()); }));
    w.body.querySelectorAll(".swatch").forEach((b) => (b.onclick = () => { setWallpaper(b.dataset.wp); RENDER.settings(w); }));
    w.body.querySelector("#set-restart")?.addEventListener("click", () => call("restartApp"));
    w.body.querySelector("#set-dnd").onclick = () => call("togglePause");
    w.body.querySelector("#set-keys").onclick = () => menubarMenus().help[1].fn();
  },

  files(w) { Files.render(w); },
};
function shortDate(s) { const d = new Date(s); if (isNaN(d)) return ""; const now = new Date();
  return d.toDateString() === now.toDateString() ? d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) : d.toLocaleDateString(undefined, { day: "numeric", month: "short" }); }
function askFafnir(text) { openApp("fafnir"); Chat.add("me", text); call("submitText", text); }

/* ---------- Files ---------- */
const Files = {
  places() { const H = S.home, p = S.places || {};
    return [["home", "Home", H], ["desktop", "Desktop", p.desktop || H + "\\Desktop"], ["docs", "Documents", p.documents || H + "\\Documents"], ["download", "Downloads", H + "\\Downloads"],
            ["pictures", "Pictures", p.pictures || H + "\\Pictures"], ["music", "Music", p.music || H + "\\Music"], ["video", "Videos", p.videos || H + "\\Videos"]]; },
  render(w) {
    w.state.hist = w.state.hist || []; w.state.fwd = [];
    w.body.innerHTML = `<div class="files"><aside class="f-side"><div class="sec">Places</div>${this.places().map(([ic, n, p]) => `<button data-p="${esc(p)}">${ICONS[ic]}${esc(n)}</button>`).join("")}
      <div class="sec">Drives</div>${(S.data.system?.disks || [{ drive: "C:\\" }]).map((d) => `<button data-p="${esc(d.drive)}">${ICONS.drive}${esc(d.drive)}</button>`).join("")}</aside>
      <div class="f-main"><div class="f-bar"><button class="nav" data-a="back">${ICONS.back}</button><button class="nav" data-a="fwd">${ICONS.fwd}</button><button class="nav" data-a="up">${ICONS.up}</button>
        <div class="f-path"></div><button class="nav" data-a="refresh">${ICONS.refresh}</button></div><div class="f-grid"></div></div></div>`;
    w.body.querySelectorAll(".f-side button").forEach((b) => (b.onclick = () => this.go(w, b.dataset.p)));
    w.body.querySelectorAll(".f-bar .nav").forEach((b) => (b.onclick = () => {
      const a = b.dataset.a, cur = w.state.path;
      if (a === "back" && w.state.hist.length) { w.state.fwd.push(cur); this.go(w, w.state.hist.pop(), true); }
      else if (a === "fwd" && w.state.fwd.length) { w.state.hist.push(cur); this.go(w, w.state.fwd.pop(), true); }
      else if (a === "up") { const up = cur.replace(/[\\/][^\\/]+[\\/]?$/, ""); if (up && up !== cur) this.go(w, /^[A-Za-z]:$/.test(up) ? up + "\\" : up); }
      else if (a === "refresh") this.go(w, cur, true); }));
    w.body.querySelector(".f-grid").addEventListener("contextmenu", (e) => { if (e.target.closest(".f-item")) return; e.preventDefault();
      Menu.show(e.clientX, e.clientY, [{ label: "Refresh", fn: () => this.go(w, w.state.path, true) }, { label: "Open in Windows Explorer", fn: () => call("openPath", w.state.path) },
        { label: `Ask ${S.name} to tidy this folder`, fn: () => askFafnir(`Look at ${w.state.path} and suggest how to tidy it. Don't move or delete anything yet.`) }]); });
    this.go(w, w.state.path || S.home, true);
  },
  async go(w, path, noHist) {
    if (!noHist && w.state.path && w.state.path !== path) { w.state.hist.push(w.state.path); w.state.fwd = []; }
    w.state.path = path; w.body.querySelector(".f-path").textContent = path;
    w.body.querySelectorAll(".f-side button").forEach((b) => b.classList.toggle("on", b.dataset.p === path));
    const grid = w.body.querySelector(".f-grid"); grid.innerHTML = `<div class="empty">Loading…</div>`;
    w.el.querySelector(".title").textContent = path.split(/[\\/]/).filter(Boolean).pop() || path;
    const r = await request("listFolder", path);
    if (w.state.path !== path) return;
    if (!r.ok) { grid.innerHTML = `<div class="empty">${esc(r.error)}</div>`; return; }
    const entries = r.data.entries || [];
    grid.innerHTML = entries.map((e, i) => `<div class="f-item" data-i="${i}"><div class="art">${ICONS[e.type === "folder" ? "folder" : fileIcon(e.name)]}</div><div class="n">${esc(e.name)}</div></div>`).join("")
      || `<div class="empty">This folder is empty.</div>`;
    if (r.data.total > entries.length) grid.insertAdjacentHTML("beforeend", `<div class="empty">Showing ${entries.length} of ${r.data.total} items.</div>`);
    grid.querySelectorAll(".f-item").forEach((el) => { const e = entries[+el.dataset.i], full = path.replace(/[\\/]$/, "") + "\\" + e.name;
      el.onclick = () => { grid.querySelectorAll(".f-item").forEach((x) => x.classList.remove("sel")); el.classList.add("sel"); };
      el.ondblclick = () => (e.type === "folder" ? this.go(w, full) : call("openPath", full));
      el.oncontextmenu = (ev) => { ev.preventDefault(); el.onclick();
        Menu.show(ev.clientX, ev.clientY, [{ label: "Open", fn: el.ondblclick },
          ...(e.type !== "folder" ? [{ label: "Quick look", fn: () => this.preview(w, full) }] : []),
          { label: `Ask ${S.name} about this`, fn: () => askFafnir(`Tell me about ${full}.`) },
          { label: "Show in Windows Explorer", fn: () => call("openPath", path) }, { sep: true },
          { label: "Move to Recycle Bin…", fn: async () => { const res = await request("deletePath", full); if (res.ok) this.go(w, path, true); } }]); };
    });
  },
  async preview(w, full) {
    const p = h(`<div class="f-preview"><div class="cc-row" style="margin:0 0 10px;justify-content:space-between"><b>${esc(full.split("\\").pop())}</b><button class="btn">Close</button></div><pre>Loading…</pre></div>`);
    w.el.appendChild(p); p.querySelector("button").onclick = () => p.remove();
    const r = await request("readFile", full);
    p.querySelector("pre").textContent = r.ok ? (r.data.content + (r.data.truncated ? "\n\n… (truncated)" : "")) : r.error;
  },
};

/* ---------- Viewer (what Fafnir puts on screen) ---------- */
function showViewer(p) {
  const id = "viewer:" + (p.title || "On screen");
  const existing = WM.wins.get(id); if (existing) WM.close(existing);
  return WM.open({ id, app: "viewer", title: p.title || "On screen", multi: false, w: p.kind === "cards" ? 860 : 760, h: 560, payload: p,
    render: (w) => { w.body.innerHTML = `<div class="h">${esc(p.title || "")}</div>${p.subtitle ? `<div class="sub">${esc(p.subtitle)}</div>` : `<div class="sub"></div>`}<div class="vbody"></div>`;
      const b = w.body.querySelector(".vbody"), items = p.items || [], tagc = (t) => (t === "urgent" ? "urgent" : t === "important" ? "important" : t === "good" ? "good" : "");
      if (p.kind === "text") b.innerHTML = `<div class="md">${md(p.text || "")}</div>`;
      else if (p.kind === "cards") b.innerHTML = `<div class="cards">${items.map((it) => `<div class="card"><div class="t ${tagc(it.tag)}">${esc(it.title)}</div>${it.meta ? `<div class="st">${esc(it.meta)}</div>` : ""}${it.subtitle ? `<div class="st">${esc(it.subtitle)}</div>` : ""}${it.body ? `<div style="margin-top:6px">${esc(it.body)}</div>` : ""}</div>`).join("")}</div>`;
      else if (p.kind === "table") b.innerHTML = `<table class="tbl"><thead><tr>${(p.columns || []).map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${items.map((r) => `<tr>${(Array.isArray(r) ? r : [r.title, r.subtitle, r.meta]).map((c) => `<td>${esc(c ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
      else b.innerHTML = `<div class="list">${items.map((it) => `<div class="row"><div class="main"><div class="t ${tagc(it.tag)}">${esc(it.title)}</div>${it.subtitle || it.body ? `<div class="st">${esc(it.subtitle || it.body)}</div>` : ""}</div>${it.meta ? `<div class="meta">${esc(it.meta)}</div>` : ""}</div>`).join("")}</div>`; } });
}

/* ---------- glue called from desktop.js ---------- */
const Apps = {
  init() {
    Chat.restore(S.conversation);
    if (SOLO) {  // a popped-out window: just this one app, filling the real window
      if (!WM.wins.size) SOLO === "viewer" ? showViewer(SOLO_ARGS.viewer || {}) : openApp(SOLO, SOLO_ARGS.path ? { path: SOLO_ARGS.path } : {});
      else Chat.render();
      return;
    }
    if (!WM.wins.has("fafnir") && !S.popped.includes("fafnir")) openApp("fafnir"); else Chat.render();
    Dock.sync();
  },
  state() { WM.wins.forEach((w) => { if (w.app === "fafnir") RENDER.fafnirState(w); }); },
  data() { refreshOpen("mail", "calendar", "tasks", "business"); },
  notices() { refreshOpen("notices"); },
  event(m) {
    switch (m.type) {
      case "heard": Chat.add("me", m.text, { spoken: true }); Chat.caption = `“${m.text}”`; this.state(); break;
      case "reply_start": Chat.current = Chat.add("ai", ""); break;
      case "reply_text": if (!Chat.current) Chat.current = Chat.add("ai", ""); Chat.current.text += m.text; Chat.render(); break;
      case "tool": if (!Chat.current) Chat.current = Chat.add("ai", ""); Chat.current.tools.push(m); Chat.render();
        S.activity.unshift({ name: m.name, time: new Date().toTimeString().slice(0, 5), error: m.error }); break;
      case "reply_end": if (Chat.current) { Chat.current.text = Chat.current.text.trim() + (m.interrupted ? " [interrupted]" : ""); Chat.current = null; Chat.render(); }
        Chat.caption = ""; this.state(); break;
      case "caption": Chat.caption = m.text; this.state(); setTimeout(() => { if (Chat.caption === m.text) { Chat.caption = ""; this.state(); } }, 6000); break;
      case "error": Chat.add("sys", m.text); break;
      case "confirm": Chat.add("sys", "Waiting for your OK: " + m.action.split(" Command: ")[0]); break;
      case "confirm_done": Chat.add("sys", m.approved ? "Approved." : m.timed_out ? "No answer, so I didn't do it." : "Declined."); break;
      case "info": Chat.add("sys", m.text); break;
    }
  },
  screen(action, p) {
    if (action === "show") showViewer(p);
    else if (action === "clear") [...WM.wins.values()].filter((w) => w.app === "viewer").forEach((w) => WM.close(w));
    else if (action === "open") { const id = PANEL_TO_APP[p.app] || p.app; openApp(id, p.path ? { path: p.path, multi: true } : {}); }
    else if (action === "layout") {
      (p.hidden || []).forEach((k) => { const w = WM.wins.get(PANEL_TO_APP[k] || k); if (w) WM.close(w); });
      if (p.conversation === "hide") { const w = WM.wins.get("fafnir"); if (w) WM.minimize(w); }
      const ids = (p.order || []).map((k) => PANEL_TO_APP[k] || k).filter((id) => APPS[id]);
      ids.forEach((id) => openApp(id));
      if (ids.length) setTimeout(() => WM.tile(ids), 120);
    }
  },
};
