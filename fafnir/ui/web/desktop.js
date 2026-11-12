/* Fafnir desktop shell: bridge to Python, window manager, dock, menu bar, menus, search,
   control centre, banners, confirmation sheet and desktop icons. Apps live in apps.js. */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const h = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const store = { get(k, d) { try { const v = localStorage.getItem("fafnir." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
                set(k, v) { try { localStorage.setItem("fafnir." + k, JSON.stringify(v)); } catch {} } };

/* A page is either the main desktop or one app popped out onto another screen ("solo"). */
const QS = new URLSearchParams(location.search);
const SOLO = QS.get("solo") || "";
const PAGE = SOLO ? QS.get("id") || "solo" : "main";
const SOLO_ARGS = (() => { try { return JSON.parse(QS.get("args") || "{}"); } catch { return {}; } })();

/* ---------------- spring physics ---------------- */
function spring({ from, to, stiffness = 260, damping = 24, mass = 1, onUpdate, onDone }) {
  let x = from, v = 0, last = performance.now(), stopped = false;
  const step = (now) => {
    if (stopped) return;
    const dt = Math.min(0.032, (now - last) / 1000); last = now;
    const a = (-stiffness * (x - to) - damping * v) / mass;
    v += a * dt; x += v * dt;
    if (Math.abs(v) < 0.002 && Math.abs(x - to) < 0.0005) { onUpdate(to); onDone && onDone(); return; }
    onUpdate(x); requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  return { stop() { stopped = true; } };
}

/* ---------------- state ---------------- */
const S = {
  name: "Fafnir", talkKey: "M5", home: "C:\\Users", voice: false, wake: "",
  data: {}, notices: [], paused: false, briefing: "", nowItems: [],
  conversation: [], activity: [], state: "idle", level: 0, status: "Ready", popped: [],
};
let bridge = null;
const pending = {};  // request id -> callback
let reqN = 0;
function call(method, ...args) { if (bridge && bridge[method]) bridge[method](...args); else console.log("[no bridge]", method, args); }
function request(method, ...args) {
  return new Promise((resolve) => { const id = PAGE + ".r" + (++reqN); pending[id] = resolve; call(method, ...args, id);
    if (!bridge) setTimeout(() => resolve({ ok: false, error: "Not connected to Fafnir." }), 50); });
}

/* ---------------- window manager ---------------- */
const WM = {
  wins: new Map(), z: 100, focused: null,
  area() { return SOLO ? { top: 0, left: 0, right: innerWidth, bottom: innerHeight } : { top: 40, left: 8, right: innerWidth - 8, bottom: innerHeight - 84 }; },

  open(opts) {
    // one window per app unless `multi`; bring an existing one forward
    if (!opts.multi && this.wins.has(opts.id)) { const w = this.wins.get(opts.id); if (w.minimized) this.restore(w); this.focus(w); opts.update && opts.update(w); return w; }
    const a = this.area(), n = this.wins.size;
    const width = Math.min(opts.w || 760, a.right - a.left - 40), height = Math.min(opts.h || 520, a.bottom - a.top - 30);
    const saved = store.get("win." + opts.app, null);
    const x = saved ? saved.x : Math.round((a.left + a.right - width) / 2 + (n % 5) * 34 - 60);
    const y = saved ? saved.y : Math.round(a.top + 40 + (n % 5) * 30);
    const el = h(`<section class="win glass" role="dialog">
      <div class="titlebar"><div class="lights"><button class="l-close" title="Close">${ICONS.x}</button><button class="l-min" title="Minimize">${ICONS.minus}</button><button class="l-max" title="Zoom">${ICONS.plus}</button></div>
      <div class="title">${esc(opts.title)}</div></div><div class="win-body ${opts.flush ? "flush" : ""}"></div>
      ${["n","s","e","w","ne","nw","se","sw"].map((d) => `<div class="resize r-${d}" data-dir="${d}"></div>`).join("")}</section>`);
    const w = { id: opts.id, app: opts.app, title: opts.title, el, body: $(".win-body", el), minimized: false, maximized: false,
                x: clamp(x, a.left, a.right - 200), y: clamp(y, a.top, a.bottom - 120), w: saved ? saved.w : width, h: saved ? saved.h : height, opts };
    if (SOLO) Object.assign(w, { x: 0, y: 0, w: innerWidth, h: innerHeight });
    this.place(w);
    $("#windows").appendChild(el);
    this.wins.set(w.id, w);
    const solo = (action, arg = 0) => call("soloAction", PAGE, action, arg);
    $(".l-close", el).onclick = (e) => { e.stopPropagation(); SOLO ? solo("close") : this.close(w); };
    $(".l-min", el).onclick = (e) => { e.stopPropagation(); SOLO ? solo("minimize") : this.minimize(w); };
    $(".l-max", el).onclick = (e) => { e.stopPropagation(); SOLO ? solo("maximize") : this.toggleMax(w); };
    el.addEventListener("pointerdown", () => this.focus(w), true);
    this.dragging($(".titlebar", el), w);
    $$(".resize", el).forEach((r) => this.resizing(r, w));
    $(".titlebar", el).addEventListener("dblclick", () => (SOLO ? solo("maximize") : this.toggleMax(w)));
    $(".titlebar", el).addEventListener("contextmenu", (e) => { e.preventDefault();
      Menu.show(e.clientX, e.clientY, SOLO
        ? [{ label: `Move back to ${S.name}'s screen`, fn: () => solo("dock") }, { sep: true }, { label: "Close window", k: "Ctrl+W", fn: () => solo("close") }]
        : [{ label: "Minimize", k: "Ctrl+M", fn: () => this.minimize(w) }, { label: w.maximized ? "Restore size" : "Zoom", fn: () => this.toggleMax(w) },
           { label: "Move to another screen", fn: () => this.popOut(w, { screen: "next" }) },
           { sep: true }, { label: "Close window", k: "Ctrl+W", fn: () => this.close(w) }]); });
    opts.render && opts.render(w);
    this.focus(w);
    if (SOLO) { addEventListener("resize", () => { Object.assign(w, { w: innerWidth, h: innerHeight }); this.place(w); w.opts.onResize && w.opts.onResize(w); }); Dock.sync(); return w; }
    // open animation: spring scale + fade
    el.style.opacity = 0;
    spring({ from: 0, to: 1, stiffness: 360, damping: 30, onUpdate: (p) => { el.style.opacity = Math.min(1, p * 1.5); el.style.transform = `scale(${0.96 + 0.04 * p}) translateY(${(1 - p) * 8}px)`; },
             onDone: () => { el.style.transform = ""; } });
    Dock.sync();
    return w;
  },
  place(w) { Object.assign(w.el.style, { left: w.x + "px", top: w.y + "px", width: w.w + "px", height: w.h + "px" }); },
  save(w) { if (!w.maximized && !w.opts.multi) store.set("win." + w.app, { x: w.x, y: w.y, w: w.w, h: w.h }); },
  focus(w) {
    if (this.focused === w) return;
    this.focused = w; w.el.style.zIndex = ++this.z;
    this.wins.forEach((o) => o.el.classList.toggle("inactive", o !== w));
    $("#mb-app").textContent = w ? w.title.split(" — ")[0] : S.name;
  },
  close(w) {
    const el = w.el;
    spring({ from: 1, to: 0, stiffness: 420, damping: 32, onUpdate: (p) => { el.style.opacity = Math.max(0, p); el.style.transform = `scale(${0.94 + 0.06 * p})`; },
             onDone: () => el.remove() });
    this.wins.delete(w.id); w.opts.onClose && w.opts.onClose(w);
    if (this.focused === w) { this.focused = null; const next = [...this.wins.values()].filter((o) => !o.minimized).sort((a, b) => b.el.style.zIndex - a.el.style.zIndex)[0];
      next ? this.focus(next) : ($("#mb-app").textContent = S.name); }
    Dock.sync();
  },
  minimize(w) {
    if (w.minimized) return;
    w.minimized = true;
    const icon = Dock.itemFor(w.app), r = w.el.getBoundingClientRect(), t = icon ? icon.getBoundingClientRect() : { left: innerWidth / 2, top: innerHeight, width: 0, height: 0 };
    const dx = t.left + t.width / 2 - (r.left + r.width / 2), dy = t.top + t.height / 2 - (r.top + r.height / 2);
    spring({ from: 0, to: 1, stiffness: 300, damping: 30, onUpdate: (p) => { w.el.style.transform = `translate(${dx * p}px, ${dy * p}px) scale(${1 - 0.92 * p})`; w.el.style.opacity = 1 - p * 0.9; },
             onDone: () => { w.el.style.visibility = "hidden"; } });
    if (this.focused === w) { this.focused = null; $("#mb-app").textContent = S.name; }
  },
  restore(w) {
    w.minimized = false; w.el.style.visibility = "visible";
    const m = /translate\(([-\d.]+)px, ([-\d.]+)px\)/.exec(w.el.style.transform || ""), dx = m ? +m[1] : 0, dy = m ? +m[2] : 0;
    spring({ from: 1, to: 0, stiffness: 300, damping: 26, onUpdate: (p) => { w.el.style.transform = `translate(${dx * p}px, ${dy * p}px) scale(${1 - 0.92 * Math.max(0, p)})`; w.el.style.opacity = 1 - Math.max(0, p) * 0.9; },
             onDone: () => { w.el.style.transform = ""; w.el.style.opacity = 1; } });
    this.focus(w);
  },
  toggleMax(w) {
    const a = this.area(), from = { x: w.x, y: w.y, w: w.w, h: w.h };
    let to;
    if (w.maximized) { to = w.prev; w.maximized = false; } else { w.prev = from; to = { x: a.left, y: a.top, w: a.right - a.left, h: a.bottom - a.top }; w.maximized = true; }
    w.el.classList.toggle("maximized", w.maximized);
    spring({ from: 0, to: 1, stiffness: 340, damping: 30, onUpdate: (p) => { ["x", "y", "w", "h"].forEach((k) => (w[k] = from[k] + (to[k] - from[k]) * p)); this.place(w); },
             onDone: () => { Object.assign(w, to); this.place(w); this.save(w); } });
  },
  dragging(handle, w) {
    handle.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || e.target.closest(".lights")) return;
      if (SOLO) { call("soloAction", PAGE, "move", 0); return; }
      handle.setPointerCapture(e.pointerId);
      const sx = e.clientX, sy = e.clientY, a = this.area();
      if (w.maximized) { w.maximized = false; w.el.classList.remove("maximized"); Object.assign(w, { w: w.prev.w, h: w.prev.h, x: clamp(sx - w.prev.w / 2, a.left, a.right - w.prev.w) }); this.place(w); }
      const ox = w.x, oy = w.y, gx = sx - ox, gy = sy - oy;
      const hintL = edgeHint("left"), hintR = edgeHint("right");
      const done = () => { handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", up); hintL.classList.remove("show"); hintR.classList.remove("show"); };
      const move = (ev) => {
        // dragged off the edge of this screen: carry on as a real window on the next one
        if (ev.clientX < 0 || ev.clientX >= innerWidth || ev.clientY >= innerHeight) {
          done(); try { handle.releasePointerCapture(e.pointerId); } catch {}
          this.popOut(w, { drag: true, grabX: gx, grabY: gy }); return; }
        hintL.classList.toggle("show", ev.clientX < 14); hintR.classList.toggle("show", ev.clientX > innerWidth - 14);
        w.x = clamp(ox + ev.clientX - sx, a.left - w.w + 120, a.right - 120); w.y = clamp(oy + ev.clientY - sy, a.top, a.bottom - 40); this.place(w); };
      const up = () => { done(); this.save(w); };
      handle.addEventListener("pointermove", move); handle.addEventListener("pointerup", up);
    });
  },
  resizing(handle, w) {
    handle.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      if (SOLO) { const d = handle.dataset.dir; call("soloAction", PAGE, "resize", (d.includes("n") ? 1 : 0) | (d.includes("w") ? 2 : 0) | (d.includes("e") ? 4 : 0) | (d.includes("s") ? 8 : 0)); return; }
      handle.setPointerCapture(e.pointerId);
      const d = handle.dataset.dir, sx = e.clientX, sy = e.clientY, o = { x: w.x, y: w.y, w: w.w, h: w.h }, minW = 320, minH = 220;
      const move = (ev) => { const dx = ev.clientX - sx, dy = ev.clientY - sy;
        if (d.includes("e")) w.w = Math.max(minW, o.w + dx);
        if (d.includes("s")) w.h = Math.max(minH, o.h + dy);
        if (d.includes("w")) { w.w = Math.max(minW, o.w - dx); w.x = o.x + o.w - w.w; }
        if (d.includes("n")) { w.h = Math.max(minH, o.h - dy); w.y = Math.max(this.area().top, o.y + o.h - w.h); }
        this.place(w); w.opts.onResize && w.opts.onResize(w); };
      const up = () => { handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", up); w.maximized = false; this.save(w); };
      handle.addEventListener("pointermove", move); handle.addEventListener("pointerup", up);
    });
  },
  popOut(w, how = {}) {
    // hand this window to Python, which opens it as a real window that can sit on any screen
    const args = { path: w.state && w.state.path, viewer: w.opts.payload };
    call("popOut", w.app, JSON.stringify({ w: Math.round(w.w), h: Math.round(w.h), args, ...how }));
    w.el.remove(); this.wins.delete(w.id); w.opts.onClose && w.opts.onClose(w);
    if (this.focused === w) { this.focused = null; $("#mb-app").textContent = S.name; }
    Dock.sync();
  },
  tile(ids) {
    const list = (ids ? ids.map((i) => this.wins.get(i)).filter(Boolean) : [...this.wins.values()]).filter((w) => !w.minimized);
    if (!list.length) return;
    const a = this.area(), n = list.length, cols = n <= 2 ? n : n <= 4 ? 2 : 3, rows = Math.ceil(n / cols), gap = 12;
    const cw = (a.right - a.left - gap * (cols - 1)) / cols, ch = (a.bottom - a.top - gap * (rows - 1)) / rows;
    list.forEach((w, i) => { const from = { x: w.x, y: w.y, w: w.w, h: w.h }, to = { x: a.left + (i % cols) * (cw + gap), y: a.top + Math.floor(i / cols) * (ch + gap), w: cw, h: ch };
      w.maximized = false; w.el.classList.remove("maximized");
      spring({ from: 0, to: 1, stiffness: 260, damping: 28, onUpdate: (p) => { ["x", "y", "w", "h"].forEach((k) => (w[k] = from[k] + (to[k] - from[k]) * p)); this.place(w); },
               onDone: () => { Object.assign(w, to); this.place(w); w.opts.onResize && w.opts.onResize(w); } }); });
  },
};
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
function edgeHint(side) { let el = $(".edge-hint." + side); if (!el) { el = h(`<div class="edge-hint ${side}"></div>`); document.body.appendChild(el); } return el; }

/* ---------------- dock ---------------- */
const Dock = {
  items: [],
  build() {
    const dock = $("#dock"); dock.innerHTML = "";
    this.items = [];
    APP_ORDER.forEach((id, i) => {
      if (id === "|") { dock.appendChild(h(`<div class="dock-sep"></div>`)); return; }
      const app = APPS[id];
      const el = h(`<button class="dock-item" data-app="${id}"><div class="art">${ICONS[app.icon]}</div><span class="run"></span><span class="tip">${esc(app.name)}</span></button>`);
      el.onclick = () => openApp(id, { fromDock: true });
      el.oncontextmenu = (e) => { e.preventDefault(); const w = WM.wins.get(id);
        Menu.show(e.clientX, e.clientY - 120, [{ label: w ? "Show" : "Open", fn: () => openApp(id) }, ...(w ? [{ label: "Minimize", fn: () => WM.minimize(w) }, { sep: true }, { label: "Quit", fn: () => WM.close(w) }] : [])]); };
      dock.appendChild(el); this.items.push(el);
    });
    // hover magnification: icons near the pointer grow (smoothed by a spring per icon)
    const scales = this.items.map(() => 1), targets = this.items.map(() => 1);
    const anim = () => { let busy = false; this.items.forEach((el, i) => { const d = targets[i] - scales[i]; if (Math.abs(d) > 0.002) { scales[i] += d * 0.28; busy = true; } else scales[i] = targets[i];
        el.style.transform = `scale(${scales[i]})`; el.style.margin = `0 ${(scales[i] - 1) * 16}px`; }); if (busy) requestAnimationFrame(anim); else this.animating = false; };
    const kick = () => { if (!this.animating) { this.animating = true; requestAnimationFrame(anim); } };
    dock.addEventListener("mousemove", (e) => { this.items.forEach((el, i) => { const r = el.getBoundingClientRect(), c = r.left + r.width / 2, dist = Math.abs(e.clientX - c);
        targets[i] = 1 + 0.42 * Math.max(0, Math.cos(Math.min(dist / 150, 1) * Math.PI / 2)); }); kick(); });
    dock.addEventListener("mouseleave", () => { targets.fill(1); kick(); });
  },
  itemFor(app) { return $(`.dock-item[data-app="${app}"]`) || $(`.dock-item[data-app="viewer"]`); },
  sync() { this.items.forEach((el) => el.classList.toggle("running", [...WM.wins.values()].some((w) => w.app === el.dataset.app) || S.popped.includes(el.dataset.app))); },
  bounce(app) { const el = this.itemFor(app); if (!el) return; el.classList.remove("bounce"); void el.offsetWidth; el.classList.add("bounce"); setTimeout(() => el.classList.remove("bounce"), 1900); },
};

/* ---------------- menus ---------------- */
const Menu = {
  el: null,
  show(x, y, items, anchor) {
    this.hide();
    const m = h(`<div class="menu glass"></div>`);
    items.forEach((it) => {
      if (it.sep) { m.appendChild(h(`<div class="sep"></div>`)); return; }
      const b = h(`<button class="mi ${it.disabled ? "disabled" : ""}"><span>${esc(it.label)}</span>${it.k ? `<span class="k">${esc(it.k)}</span>` : ""}</button>`);
      b.onclick = () => { this.hide(); it.fn && it.fn(); };
      m.appendChild(b);
    });
    $("#menu-layer").appendChild(m);
    const r = m.getBoundingClientRect();
    m.style.left = clamp(x, 6, innerWidth - r.width - 6) + "px"; m.style.top = clamp(y, 44, innerHeight - r.height - 6) + "px";
    this.el = m; this.anchor = anchor; anchor && anchor.classList.add("open");
  },
  hide() { if (this.el) { this.el.remove(); this.el = null; } if (this.anchor) { this.anchor.classList.remove("open"); this.anchor = null; } },
};
document.addEventListener("pointerdown", (e) => { if (Menu.el && !e.target.closest(".menu") && !e.target.closest("[data-menu]")) Menu.hide();
  if (!e.target.closest("#control") && !e.target.closest("#mb-control")) Control.hide(); }, true);

function menubarMenus() {
  const focused = WM.focused;
  return {
    fafnir: [{ label: `About ${S.name}`, fn: () => openApp("settings") }, { sep: true }, { label: "Settings…", k: "Ctrl+,", fn: () => openApp("settings") },
             { label: S.paused ? "Resume notices" : "Pause notices", fn: () => call("togglePause") }, { sep: true },
             { label: "Stop talking", k: "Esc", fn: () => call("interrupt") }, { label: "New conversation", fn: () => call("submitText", "/reset") }],
    app: [{ label: `Open ${S.name}`, fn: () => openApp("fafnir") }, { label: "Search…", k: "Ctrl+Space", fn: () => Search.show() }, { label: "Refresh data", fn: () => call("refresh") }],
    file: [{ label: "New Files window", fn: () => openApp("files", { multi: true }) }, { label: "Open Home", fn: () => openApp("files", { path: S.home }) },
           { label: "Open Downloads", fn: () => openApp("files", { path: S.home + "\\Downloads" }) }, { sep: true },
           { label: "Close window", k: "Ctrl+W", disabled: !focused, fn: () => focused && WM.close(focused) }],
    window: [{ label: "Minimize", k: "Ctrl+M", disabled: !focused, fn: () => focused && WM.minimize(focused) }, { label: "Zoom", disabled: !focused, fn: () => focused && WM.toggleMax(focused) },
             { label: "Move to another screen", disabled: !focused, fn: () => focused && WM.popOut(focused, { screen: "next" }) },
             { label: "Tile all windows", fn: () => WM.tile() }, { sep: true },
             ...[...WM.wins.values()].map((w) => ({ label: (w === focused ? "• " : "  ") + w.title, fn: () => (w.minimized ? WM.restore(w) : WM.focus(w)) })),
             { sep: true }, { label: "Close all", fn: () => [...WM.wins.values()].forEach((w) => WM.close(w)) }],
    help: [{ label: `Ask ${S.name}…`, fn: () => { openApp("fafnir"); setTimeout(() => $(".fa-input input")?.focus(), 200); } },
           { label: "Keyboard shortcuts", fn: () => showViewer({ title: "Keyboard shortcuts", kind: "table", columns: ["Keys", "Does"], items: [
             ["Ctrl+Space", "Search"], ["Esc", "Close overlays / stop Fafnir talking"], ["Ctrl+W", "Close window"], ["Ctrl+M", "Minimize window"],
             ["Ctrl+,", "Settings"], ["F11", "Toggle fullscreen"], ["Ctrl+Q", "Quit Fafnir"], [`Hold ${S.talkKey}`, `Talk to ${S.name}`]] }) }],
  };
}

/* ---------------- search ---------------- */
const Search = {
  sel: 0, items: [], req: null,
  show() { Control.hide(); $("#search").classList.remove("hidden"); const i = $("#search-input"); i.value = ""; this.render([]); setTimeout(() => i.focus(), 30); },
  hide() { $("#search").classList.add("hidden"); },
  get open() { return !$("#search").classList.contains("hidden"); },
  query(q) {
    const ql = q.trim().toLowerCase(); this.sections = {};
    if (!ql) { this.render([]); return; }
    this.sections.Apps = Object.entries(APPS).filter(([id, a]) => id !== "viewer" && (a.name.toLowerCase().includes(ql) || (a.keywords || "").includes(ql)))
      .map(([id, a]) => ({ title: a.name, subtitle: "App", icon: a.icon, run: () => openApp(id) }));
    this.sections[`Ask ${S.name}`] = [{ title: q.trim(), subtitle: `Ask ${S.name} this`, icon: "fafnir", run: () => { openApp("fafnir"); call("submitText", q.trim()); } }];
    this.req = PAGE + ".s" + (++reqN); this.render(); if (q.trim().length >= 2) call("search", q.trim(), this.req);
  },
  add(section, items) {
    this.sections[section] = items.map((it) => ({ ...it, icon: section === "Files" ? (looksLikeFolder(it.path) ? "folder" : fileIcon(it.path)) : section === "Memory" ? "fafnir" : section === "Business" ? "business" : "file",
      run: () => section === "Files" || section === "Notes" ? openPathSmart(it.path) : section === "Business" ? openApp("tasks") : openApp("fafnir") }));
    this.render();
  },
  render() {
    const box = $("#search-results"); box.innerHTML = ""; this.items = [];
    const order = ["Apps", `Ask ${S.name}`, "Files", "Notes", "Memory", "Business"];
    order.forEach((sec) => { const list = (this.sections || {})[sec]; if (!list || !list.length) return;
      box.appendChild(h(`<div class="s-section">${esc(sec)}</div>`));
      list.forEach((it) => { const el = h(`<div class="s-item"><div class="art">${ICONS[it.icon] || ICONS.file}</div><div><div class="t">${esc(it.title)}</div><div class="st">${esc(it.subtitle || "")}</div></div></div>`);
        const idx = this.items.length; el.onmouseenter = () => this.select(idx); el.onclick = () => { this.hide(); it.run(); };
        box.appendChild(el); this.items.push({ el, it }); }); });
    this.select(Math.min(this.sel, Math.max(0, this.items.length - 1)));
  },
  select(i) { this.sel = i; this.items.forEach((x, j) => x.el.classList.toggle("active", j === i)); this.items[i]?.el.scrollIntoView({ block: "nearest" }); },
};
$("#search-input").addEventListener("input", (e) => { Search.sel = 0; Search.query(e.target.value); });
$("#search-input").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { Search.select(Math.min(Search.sel + 1, Search.items.length - 1)); e.preventDefault(); }
  else if (e.key === "ArrowUp") { Search.select(Math.max(Search.sel - 1, 0)); e.preventDefault(); }
  else if (e.key === "Enter") { const x = Search.items[Search.sel]; if (x) { Search.hide(); x.it.run(); } }
  else if (e.key === "Escape") Search.hide();
});
$("#search").addEventListener("pointerdown", (e) => { if (e.target.id === "search") Search.hide(); });

/* ---------------- control centre ---------------- */
const Control = {
  status: null,
  async show() {
    const el = $("#control"); el.classList.remove("hidden"); $("#mb-control").classList.add("open"); this.render();
    const r = await request("controlStatus"); this.status = r.data || null; this.render();
  },
  hide() { $("#control").classList.add("hidden"); $("#mb-control")?.classList.remove("open"); },
  toggle() { $("#control").classList.contains("hidden") ? this.show() : this.hide(); },
  render() {
    const st = this.status, wifi = st?.wifi || {}, bt = st?.bluetooth || {};
    const tog = (key, on, icon, label, sub) => `<button class="cc-toggle ${on ? "on" : ""}" data-k="${key}"><span class="ico">${ICONS[icon]}</span><span><div class="lbl">${label}</div><div class="sub">${esc(sub)}</div></span></button>`;
    $("#control").innerHTML = `
      <div class="cc-tile">${tog("wifi", wifi.on, "wifi", "Wi-Fi", !st ? "Checking…" : !wifi.available ? "No Wi-Fi adapter" : wifi.on ? (wifi.name || "Connected") : "Not connected")}
        ${tog("bt", bt.on, "bluetooth", "Bluetooth", !st ? "Checking…" : bt.on ? "On" : "Off")}</div>
      <div class="cc-tile">${tog("dnd", S.paused, "moon", "Do not disturb", S.paused ? "Notices paused" : "Off")}
        ${tog("voice", st?.voice, "mic", "Voice", !st ? "…" : st.voice ? (st.wake ? `Say “${S.wake || S.name}”` : `Hold ${S.talkKey}`) : "Off")}</div>
      <div class="cc-tile wide"><div class="cc-label">Display <span class="k">${st && st.brightness == null ? "this monitor doesn't allow it" : (st?.brightness ?? "…") + "%"}</span></div>
        <input type="range" class="slider" id="cc-bright" min="0" max="100" value="${st?.brightness ?? 50}" ${st && st.brightness == null ? "disabled" : ""}></div>
      <div class="cc-tile wide"><div class="cc-label">Sound <span class="k">drag to change</span></div>
        <input type="range" class="slider" id="cc-vol" min="0" max="50" value="25">
        <div class="cc-row"><button class="cc-btn" data-a="mute" title="Mute">${ICONS.mute}</button><button class="cc-btn" data-a="previous" title="Previous">${ICONS.prev}</button>
          <button class="cc-btn" data-a="play_pause" title="Play / pause">${ICONS.play}</button><button class="cc-btn" data-a="next" title="Next">${ICONS.next}</button></div></div>`;
    $$(".cc-btn", $("#control")).forEach((b) => (b.onclick = () => call("control", b.dataset.a, 1)));
    const vol = $("#cc-vol"); let last = +vol.value;
    vol.oninput = () => { const v = +vol.value, d = v - last; if (d) call("control", d > 0 ? "volume_up" : "volume_down", Math.abs(d)); last = v; };
    const br = $("#cc-bright"); br.onchange = () => { call("control", "brightness", +br.value); if (this.status) this.status.brightness = +br.value; this.render(); };
    $$(".cc-toggle", $("#control")).forEach((b) => (b.onclick = () => {
      const k = b.dataset.k;
      if (k === "dnd") { call("control", "dnd", 0); S.paused = !S.paused; this.render(); }
      else if (k === "wifi") { if (!st) return; this.hide(); call("control", wifi.on ? "wifi_off" : "wifi_on", 0); }  // asks for your OK first
      else if (k === "bt") { this.hide(); call("control", "bluetooth_settings", 0); }
      else if (k === "voice") { this.hide(); openApp("settings"); }
    }));
  },
};

/* ---------------- banners ---------------- */
function banner({ title, detail = "", urgency = "info", app = "notices", id }) {
  const el = h(`<div class="banner glass ${urgency === "urgent" ? "urgent" : ""}"><div class="art">${ICONS[APPS[app]?.icon || "notices"]}</div>
    <div><div class="app">${esc(APPS[app]?.name || S.name)}</div><div class="t">${esc(title)}</div>${detail ? `<div class="d">${esc(detail)}</div>` : ""}</div><button class="x">✕</button></div>`);
  $("#banners").prepend(el);
  const remove = () => spring({ from: 0, to: 1, stiffness: 380, damping: 30, onUpdate: (p) => { el.style.transform = `translateX(${p * 400}px)`; el.style.opacity = 1 - p; }, onDone: () => el.remove() });
  spring({ from: 1, to: 0, stiffness: 300, damping: 22, onUpdate: (p) => { el.style.transform = `translateX(${p * 400}px)`; el.style.opacity = 1 - p; } });
  el.onclick = (e) => { if (e.target.closest(".x")) { remove(); return; } remove(); openApp(app); };
  setTimeout(remove, urgency === "info" ? 6000 : 12000);
}

/* ---------------- confirmation sheet ---------------- */
const Confirm = {
  show(action) {
    const [main, cmd] = action.split(" Command: ");
    $("#confirm-text").textContent = main; $("#confirm-detail").textContent = cmd || "";
    $("#confirm-hint").textContent = `Click, or hold ${S.talkKey} and say yes or no. No answer in 60 seconds counts as no.`;
    $("#confirm").classList.remove("hidden"); setTimeout(() => $("#confirm-yes").focus(), 50);
  },
  hide() { $("#confirm").classList.add("hidden"); },
};
$("#confirm-yes").onclick = () => call("answerConfirm", true);
$("#confirm-no").onclick = () => call("answerConfirm", false);

/* ---------------- desktop icons ---------------- */
function buildDesktopIcons() {
  const defs = [{ id: "home", label: "Home", icon: "folder", path: S.home }, { id: "docs", label: "Documents", icon: "folder", path: S.home + "\\OneDrive\\Documents" },
    { id: "down", label: "Downloads", icon: "folder", path: S.home + "\\Downloads" }, { id: "fafnir", label: S.name, icon: "fafnir", app: "fafnir" },
    { id: "files", label: "Files", icon: "files", app: "files" }];
  const wrap = $("#icons"); wrap.innerHTML = "";
  defs.forEach((d, i) => {
    const pos = store.get("icon." + d.id, { x: innerWidth - 130, y: 60 + i * 112 });
    const el = h(`<div class="d-icon" style="left:${pos.x}px;top:${pos.y}px"><div class="art">${ICONS[d.icon]}</div><div class="label">${esc(d.label)}</div></div>`);
    const open = () => (d.app ? openApp(d.app) : openApp("files", { path: d.path, multi: true }));
    el.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return; $$(".d-icon").forEach((x) => x.classList.remove("selected")); el.classList.add("selected");
      el.setPointerCapture(e.pointerId); const sx = e.clientX, sy = e.clientY, ox = parseFloat(el.style.left), oy = parseFloat(el.style.top);
      const move = (ev) => { el.style.left = clamp(ox + ev.clientX - sx, 0, innerWidth - 96) + "px"; el.style.top = clamp(oy + ev.clientY - sy, 44, innerHeight - 190) + "px"; };
      const up = () => { el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up); store.set("icon." + d.id, { x: parseFloat(el.style.left), y: parseFloat(el.style.top) }); };
      el.addEventListener("pointermove", move); el.addEventListener("pointerup", up);
    });
    el.ondblclick = open;
    el.oncontextmenu = (e) => { e.preventDefault(); e.stopPropagation(); Menu.show(e.clientX, e.clientY, [{ label: "Open", fn: open },
      ...(d.path ? [{ label: "Open in Windows Explorer", fn: () => call("openPath", d.path) }] : []), { sep: true },
      { label: `Ask ${S.name} about this`, fn: () => { openApp("fafnir"); call("submitText", `What's in ${d.path || d.label}?`); } }]); };
    wrap.appendChild(el);
  });
}
$("#desktop").addEventListener("pointerdown", (e) => { if (e.target.id === "desktop" || e.target.id === "windows" || e.target.id === "icons") $$(".d-icon").forEach((x) => x.classList.remove("selected")); });
$("#desktop").addEventListener("contextmenu", (e) => {
  if (e.target.closest(".win") || e.target.closest(".d-icon")) return; e.preventDefault();
  Menu.show(e.clientX, e.clientY, [{ label: "Open Files", fn: () => openApp("files") }, { label: `Talk to ${S.name}`, fn: () => openApp("fafnir") },
    { label: "Search…", k: "Ctrl+Space", fn: () => Search.show() }, { sep: true }, { label: "Tile windows", fn: () => WM.tile() },
    { label: "Change wallpaper…", fn: () => openApp("settings") }, { label: "Reset icon positions", fn: () => { Object.keys(localStorage).filter((k) => k.startsWith("fafnir.icon.")).forEach((k) => localStorage.removeItem(k)); buildDesktopIcons(); } },
    { sep: true }, { label: "Refresh", fn: () => call("refresh") }]);
});

/* ---------------- menu bar ---------------- */
function buildMenubar() {
  $(".logo").innerHTML = ICONS.logo_s; $("#mb-search").innerHTML = ICONS.search_s; $("#mb-control").innerHTML = ICONS.control_s;
  $("#mb-bell").insertAdjacentHTML("afterbegin", ICONS.bell_s);
  $$("[data-menu]").forEach((b) => b.addEventListener("click", (e) => { const r = b.getBoundingClientRect(); if (Menu.anchor === b) { Menu.hide(); return; }
    Menu.show(r.left, r.bottom + 6, menubarMenus()[b.dataset.menu], b); }));
  $$("[data-menu]").forEach((b) => b.addEventListener("mouseenter", () => { if (Menu.el && Menu.anchor && Menu.anchor !== b) b.click(); }));
  $("#mb-search").onclick = () => Search.show();
  $("#mb-control").onclick = () => Control.toggle();
  $("#mb-bell").onclick = () => openApp("notices");
  $("#mb-clock").onclick = () => openApp("calendar");
  const tick = () => { const d = new Date(); $("#mb-clock").textContent = d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }) + "   " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }); };
  tick(); setInterval(tick, 5000);
}

/* ---------------- glass reacts to the mouse ---------------- */
let mouseFrame = 0;
document.addEventListener("mousemove", (e) => {
  if (mouseFrame) return;
  mouseFrame = requestAnimationFrame(() => { mouseFrame = 0;
    $$(".glass").forEach((g) => { const r = g.getBoundingClientRect();
      if (e.clientX < r.left - 300 || e.clientX > r.right + 300 || e.clientY < r.top - 300 || e.clientY > r.bottom + 300) return;
      g.style.setProperty("--mx", e.clientX - r.left + "px"); g.style.setProperty("--my", e.clientY - r.top + "px"); }); });
});

/* ---------------- keyboard ---------------- */
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.code === "Space") { e.preventDefault(); Search.open ? Search.hide() : Search.show(); }
  else if (e.key === "Escape") { if (Search.open) Search.hide(); else if (Menu.el) Menu.hide(); else if (!$("#control").classList.contains("hidden")) Control.hide(); else call("interrupt"); }
  else if (e.ctrlKey && e.key.toLowerCase() === "w" && WM.focused) { e.preventDefault(); WM.close(WM.focused); }
  else if (e.ctrlKey && e.key.toLowerCase() === "m" && WM.focused) { e.preventDefault(); WM.minimize(WM.focused); }
  else if (e.ctrlKey && e.key === ",") { e.preventDefault(); openApp("settings"); }
});

/* ---------------- Right-now widget ---------------- */
function renderNow() {
  $("#now-briefing").textContent = S.briefing || "All clear.";
  $("#now-items").innerHTML = S.nowItems.slice(0, 5).map((i) => `<li class="tag-${esc(i.tag)}"><span class="ic">${esc(i.icon)}</span><span class="t">${esc(i.title)}</span><span class="d">${esc(i.detail)}</span></li>`).join("");
}

/* ---------------- messages from Python ---------------- */
function onEvent(raw) {
  const m = typeof raw === "string" ? JSON.parse(raw) : raw;
  if (m.to && m.to !== PAGE) return;
  if (SOLO && ["banner", "briefing", "screen", "confirm", "popped"].includes(m.type)) return;
  switch (m.type) {
    case "init":
      Object.assign(S, { name: m.name, talkKey: m.talkKey, voice: m.voice, wake: m.wake, home: m.home, conversation: m.conversation || [], activity: m.activity || [] });
      document.title = m.name; $("#mb-app").textContent = m.name;
      if (m.wallpaper && !store.get("wallpaper", "")) setWallpaper("image", m.wallpaper);
      if (!SOLO) buildDesktopIcons();
      Apps.init(); break;
    case "popped": S.popped = m.apps || []; Dock.sync(); break;
    case "tick":
      if (m.state !== S.state) { S.state = m.state; $("#mb-status").dataset.state = m.state; $("#mb-status-text").textContent = m.status; Apps.state(); }
      S.level = m.level; break;
    case "data": S.data = m; Apps.data(); break;
    case "briefing": S.briefing = m.text; S.nowItems = m.items || []; renderNow(); break;
    case "notices":
      S.notices = m.items; S.paused = m.paused;
      const b = $("#mb-badge"), n = m.items.filter((x) => x.urgency !== "info").length || m.items.length;
      b.textContent = n; b.classList.toggle("show", m.items.length > 0); Apps.notices(); break;
    case "wallpaper": setWallpaper(m.url ? "image" : "aurora", m.url); break;
    case "banner": banner({ title: m.title, detail: m.detail, urgency: m.urgency, app: "notices" }); break;
    case "confirm": Confirm.show(m.action); Apps.event(m); break;
    case "confirm_done": Confirm.hide(); Apps.event(m); break;
    case "result": if (pending[m.id]) { pending[m.id](m); delete pending[m.id]; } break;
    case "search": if (m.id === Search.req) Search.add(m.section, m.items); break;
    case "screen": Apps.screen(m.action, m.payload || {}); break;
    case "error": banner({ title: "Something went wrong", detail: m.text, urgency: "important", app: "fafnir" }); Apps.event(m); break;
    default: Apps.event(m);
  }
}

/* ---------------- wallpaper ---------------- */
function setWallpaper(kind, url) {
  document.body.classList.remove("wp-dusk", "wp-ocean", "wp-graphite");
  const wp = $("#wallpaper"); wp.classList.remove("image"); wp.style.backgroundImage = "";
  if (kind === "image" && url) { wp.classList.add("image"); wp.style.backgroundImage = `linear-gradient(rgba(4,8,20,.25), rgba(4,8,20,.25)), url("${url}")`; }
  else if (kind && kind !== "aurora") document.body.classList.add("wp-" + kind);
  store.set("wallpaper", kind === "image" ? "" : kind);
}

/* ---------------- start ---------------- */
function start() {
  if (SOLO) document.body.classList.add("solo");
  else { buildMenubar(); Dock.build(); buildDesktopIcons(); renderNow(); }
  const wp = store.get("wallpaper", ""); if (wp) setWallpaper(wp);
  if (window.qt && window.QWebChannel) {
    new QWebChannel(qt.webChannelTransport, (ch) => { bridge = ch.objects.fafnir; bridge.event.connect(onEvent);
      SOLO ? bridge.soloReady(PAGE) : bridge.ready(); });
  } else {
    // opened in a normal browser (design preview): show sample content
    onEvent({ type: "init", name: "Fafnir", talkKey: "M5", voice: true, wake: "Fafnir", home: "C:\\Users\\you", conversation: [] });
    window.PREVIEW && window.PREVIEW(onEvent);
  }
}
addEventListener("DOMContentLoaded", start);
