/* Original icons for Fafnir's desktop (inline SVG, no external assets). */
(function () {
  const tile = (id, c1, c2, glyph) => `<svg viewBox="0 0 64 64"><defs>
    <linearGradient id="g${id}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${c1}"/><stop offset="1" stop-color="${c2}"/></linearGradient>
    <linearGradient id="s${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".2"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/></linearGradient></defs>
    <rect x="3" y="3" width="58" height="58" rx="15" fill="url(#g${id})"/>
    <rect x="3" y="3" width="58" height="58" rx="15" fill="url(#s${id})"/>
    <rect x="3.5" y="3.5" width="57" height="57" rx="14.5" fill="none" stroke="#fff" stroke-opacity=".16"/>
    <g fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round">${glyph}</g></svg>`;

  const line = (d, extra = "") => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" ${extra}>${d}</svg>`;

  window.ICONS = {
    fafnir: `<svg viewBox="0 0 64 64"><defs><radialGradient id="fo" cx=".42" cy=".38" r=".7"><stop offset="0" stop-color="#f3f5fa"/><stop offset=".55" stop-color="#b9c4dc"/><stop offset="1" stop-color="#6b7896"/></radialGradient>
      <linearGradient id="fb" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3a3e4a"/><stop offset="1" stop-color="#23262e"/></linearGradient></defs>
      <rect x="3" y="3" width="58" height="58" rx="14" fill="url(#fb)"/><circle cx="32" cy="32" r="19.5" fill="none" stroke="#c9d1e4" stroke-opacity=".28" stroke-width="1.4"/>
      <circle cx="32" cy="32" r="12.5" fill="url(#fo)"/><rect x="3.5" y="3.5" width="57" height="57" rx="13.5" fill="none" stroke="#fff" stroke-opacity=".1"/></svg>`,
    mail: tile("m", "#4fb0ff", "#2457e6", `<rect x="15" y="20" width="34" height="24" rx="4"/><path d="M16 22l16 12 16-12"/>`),
    calendar: tile("c", "#ff8a7a", "#e8455f", `<rect x="16" y="18" width="32" height="30" rx="5"/><path d="M16 27h32M24 14v8M40 14v8"/><circle cx="26" cy="36" r=".6"/><circle cx="32" cy="36" r=".6"/><circle cx="38" cy="36" r=".6"/>`),
    tasks: tile("t", "#5ee0a0", "#16a36a", `<path d="M20 23l4 4 7-7M20 39l4 4 7-7M37 25h9M37 41h9"/>`),
    notices: tile("n", "#ffcd6b", "#ff8c2b", `<path d="M22 40V30a10 10 0 0120 0v10l3 4H19z"/><path d="M29 48a3.5 3.5 0 006 0"/>`),
    files: tile("f", "#7ad1ff", "#3c82f6", `<path d="M14 22a3 3 0 013-3h9l4 4h17a3 3 0 013 3v17a3 3 0 01-3 3H17a3 3 0 01-3-3z"/><path d="M14 29h36"/>`),
    business: tile("b", "#c08bff", "#7a3cf0", `<rect x="15" y="24" width="34" height="22" rx="4"/><path d="M26 24v-4a3 3 0 013-3h6a3 3 0 013 3v4M15 33h34"/>`),
    youtube: tile("y", "#ff6b6b", "#c9184a", `<rect x="14" y="20" width="36" height="24" rx="7"/><path d="M29 27v10l9-5z" fill="#fff"/>`),
    settings: tile("s", "#a7b4c8", "#56637a", `<circle cx="32" cy="32" r="6"/><path d="M32 15v5M32 44v5M15 32h5M44 32h5M20 20l3.5 3.5M40.5 40.5L44 44M44 20l-3.5 3.5M23.5 40.5L20 44"/>`),
    viewer: tile("v", "#6ef0ff", "#2b9bd6", `<rect x="15" y="18" width="34" height="24" rx="4"/><path d="M26 48h12M32 42v6"/>`),
    search: tile("q", "#8aa0ff", "#4a5cff", `<circle cx="29" cy="29" r="9"/><path d="M36 36l9 9"/>`),
    folder: `<svg viewBox="0 0 64 64"><defs><linearGradient id="fd" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#8fd8ff"/><stop offset="1" stop-color="#3f8ff0"/></linearGradient></defs>
      <path d="M6 16a4 4 0 014-4h14l5 5h25a4 4 0 014 4v29a4 4 0 01-4 4H10a4 4 0 01-4-4z" fill="#2f74d0"/>
      <path d="M6 24a4 4 0 014-4h44a4 4 0 014 4v26a4 4 0 01-4 4H10a4 4 0 01-4-4z" fill="url(#fd)"/>
      <path d="M6.5 24a3.5 3.5 0 013.5-3.5h44" stroke="#fff" stroke-opacity=".5" fill="none"/></svg>`,
    file: `<svg viewBox="0 0 64 64"><path d="M16 6h22l12 12v38a3 3 0 01-3 3H16a3 3 0 01-3-3V9a3 3 0 013-3z" fill="#eef4ff" fill-opacity=".92"/>
      <path d="M38 6v10a2 2 0 002 2h10" fill="#c7d4ea"/><path d="M21 30h22M21 37h22M21 44h14" stroke="#8aa0c2" stroke-width="2.4" stroke-linecap="round"/></svg>`,
    image: `<svg viewBox="0 0 64 64"><rect x="8" y="12" width="48" height="40" rx="6" fill="#eef4ff" fill-opacity=".92"/><circle cx="23" cy="25" r="5" fill="#ffc46b"/><path d="M12 48l14-14 10 10 6-6 10 10z" fill="#5fb3ff"/></svg>`,
    app: `<svg viewBox="0 0 64 64"><rect x="10" y="10" width="44" height="44" rx="11" fill="#c7d4ea"/><path d="M24 32h16M32 24v16" stroke="#56637a" stroke-width="4" stroke-linecap="round"/></svg>`,
    home: line(`<path d="M3 11l9-7 9 7v9a1 1 0 01-1 1h-5v-6h-6v6H4a1 1 0 01-1-1z"/>`),
    desktop: line(`<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>`),
    docs: line(`<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>`),
    download: line(`<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>`),
    pictures: line(`<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-8 8"/>`),
    music: line(`<path d="M9 18V6l11-2v12"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>`),
    video: line(`<rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10l5-3v10l-5-3"/>`),
    drive: line(`<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 12h.01M11 12h.01"/>`),
    search_s: line(`<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>`),
    control_s: line(`<rect x="3" y="5" width="18" height="5" rx="2.5"/><circle cx="8" cy="7.5" r="1.3" fill="currentColor"/><rect x="3" y="14" width="18" height="5" rx="2.5"/><circle cx="16" cy="16.5" r="1.3" fill="currentColor"/>`),
    bell_s: line(`<path d="M6 16V11a6 6 0 0112 0v5l2 2H4z"/><path d="M10 20a2 2 0 004 0"/>`),
    logo_s: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5.5" fill="#e4e8f2"/><circle cx="12" cy="12" r="9.5" fill="none" stroke="#e4e8f2" stroke-opacity=".45" stroke-width="1.3"/></svg>`,
    wifi: line(`<path d="M2 9a15 15 0 0120 0M5 12.5a10 10 0 0114 0M8.5 16a5 5 0 017 0"/><circle cx="12" cy="19" r="1" fill="currentColor"/>`),
    bluetooth: line(`<path d="M7 7l10 10-5 4V3l5 4L7 17"/>`),
    moon: line(`<path d="M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z"/>`),
    mic: line(`<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0014 0M12 18v3"/>`),
    sun: line(`<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M19 5l-1.5 1.5M6.5 17.5L5 19"/>`),
    sound: line(`<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M17 9a4 4 0 010 6M19.5 6.5a8 8 0 010 11"/>`),
    mute: line(`<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M17 9l5 6M22 9l-5 6"/>`),
    play: line(`<path d="M7 5v14l11-7z"/><path d="M19 5v14" stroke-opacity="0"/>`),
    next: line(`<path d="M5 5v14l9-7zM18 5v14"/>`),
    prev: line(`<path d="M19 5v14l-9-7zM6 5v14"/>`),
    back: line(`<path d="M15 5l-7 7 7 7"/>`),
    fwd: line(`<path d="M9 5l7 7-7 7"/>`),
    up: line(`<path d="M5 15l7-7 7 7"/>`),
    refresh: line(`<path d="M20 12a8 8 0 11-2.3-5.7M20 4v5h-5"/>`),
    x: line(`<path d="M6 6l12 12M18 6L6 18"/>`, 'stroke-width="3"'),
    minus: line(`<path d="M5 12h14"/>`, 'stroke-width="3"'),
    plus: line(`<path d="M12 5v14M5 12h14"/>`, 'stroke-width="3"'),
  };
})();
