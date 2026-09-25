"use strict";
/* pytermwm web UI: live screen (keyboard, mouse, paste), window list, console, config / rule / script editors. */
(function () {
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const el = (tag, props, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === "class") e.className = v; else if (k === "text") e.textContent = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v);
    }
    for (const kid of kids) e.append(kid);
    return e;
  };

  // ------------------------------------------------------------------ api
  async function api(method, path, body) {
    const opt = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    let r;
    try { r = await fetch("/api/" + path, opt); } catch (e) { setConn(false); throw new Error("network error"); }
    let j = null;
    try { j = await r.json(); } catch (e) { /* not json */ }
    if (r.status === 401) { setConn(false, "unauthorized"); throw new Error("unauthorized: open the URL printed by `pytermwm web`"); }
    if (!r.ok || (j && j.ok === false)) throw new Error((j && j.error) || ("HTTP " + r.status));
    return j;
  }
  const command = (line) => api("POST", "command", { line });
  let toastTimer = null;
  function toast(msg, bad) {
    const t = $("#toast"); t.textContent = msg; t.className = "show" + (bad ? " bad" : "");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.className = ""), bad ? 6000 : 2500);
  }
  function notifyUser(d) {
    const text = (d.title && d.title !== "pytermwm" ? d.title + ": " : "") + (d.body || "");
    toast(text, d.style === "err");
    try {
      if (window.Notification && Notification.permission === "granted" && document.hidden) new Notification(d.title || "pytermwm", { body: d.body || "" });
    } catch (e) { /* notifications are optional */ }
  }
  const fail = (e) => toast(e.message || String(e), true);
  function setConn(ok, text) { const c = $("#conn"); c.className = "conn " + (ok ? "ok" : "bad"); c.textContent = text || (ok ? "live" : "offline"); }

  // remove the token from the address bar (it is now in a HttpOnly cookie)
  if (location.search.includes("token=")) history.replaceState(null, "", location.pathname);

  // ------------------------------------------------------------------ tabs
  const loaders = {};
  function showTab(name) {
    $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
    location.hash = name;
    if (loaders[name]) loaders[name]();
    if (name === "screen") { fitScreen(); $("#screen").focus(); syncState(); }
  }
  $("#tabs").addEventListener("click", (e) => { const b = e.target.closest("button[data-tab]"); if (b) showTab(b.dataset.tab); });
  $$("[data-cmd]").forEach((b) => b.addEventListener("click", () => { command(b.dataset.cmd).then(() => $("#screen").focus()).catch(fail); }));

  // ------------------------------------------------------------------ screen
  const screenEl = $("#screen"), cursorEl = $("#cursor"), wrapEl = $("#screen-wrap"), measureEl = $("#measure");
  let cw = 8, ch = 17, frame = null, prevRows = [], sizeSent = "", stateCache = null, attachedSize = false;
  const fontInput = $("#font-size"), fitInput = $("#font-fit");
  try {
    const f = localStorage.getItem("ptw-font"); if (f) fontInput.value = f;
    const ff = localStorage.getItem("ptw-font-fit"); if (ff !== null) fitInput.checked = ff === "1";
  } catch (e) { /* storage may be blocked */ }
  fitInput.addEventListener("change", () => {
    try { localStorage.setItem("ptw-font-fit", fitInput.checked ? "1" : "0"); } catch (e) { /* ignore */ }
    fitScreen();
  });

  function applyFont() {
    const px = Math.max(6, Math.min(32, parseInt(fontInput.value, 10) || 14));
    screenEl.style.fontSize = px + "px"; measureEl.style.fontSize = px + "px"; measureEl.style.lineHeight = "1.2";
    try { localStorage.setItem("ptw-font", String(px)); } catch (e) { /* ignore */ }
    const r = measureEl.getBoundingClientRect();
    cw = r.width / 10 || 8; ch = r.height || px * 1.2;
    prevRows = []; if (frame) drawFrame(frame);
    fitScreen();
  }
  fontInput.addEventListener("change", applyFont);

  const cellStyle = (fg, bg, fl) => {
    let s = "";
    const rev = fl & 32;
    let f = fg, b = bg;
    if (rev) { const tf = f || "d8dee9", tb = b || "1e222a"; f = tb; b = tf; }
    if (f) s += "color:#" + f + ";"; if (b) s += "background:#" + b + ";";
    if (fl & 64) s += "visibility:hidden;";
    return s;
  };
  function rowHtml(runs) {
    const row = el("div", { class: "row" });
    for (const [text, fg, bg, fl, cells] of runs) {
      const sp = el("span", { text });
      let cls = "";
      if (fl & 1) cls += " b"; if (fl & 2) cls += " d"; if (fl & 4) cls += " i"; if (fl & 8) cls += " u"; if (fl & 128) cls += " s";
      if (cls) sp.className = cls.trim();
      const st = cellStyle(fg, bg, fl);
      if (st) sp.style.cssText = st;
      sp.style.width = (cells * cw) + "px";
      row.append(sp);
    }
    return row;
  }
  function drawFrame(f) {
    const sizeChanged = frame && (frame.cols !== f.cols || frame.rows !== f.rows);
    frame = f;
    const rows = f.lines;
    const key = (runs) => JSON.stringify(runs);
    if (prevRows.length !== rows.length) { screenEl.textContent = ""; prevRows = []; }
    const kids = screenEl.children;
    rows.forEach((runs, y) => {
      const k = key(runs);
      if (prevRows[y] === k) return;
      prevRows[y] = k;
      const node = rowHtml(runs);
      if (kids[y]) screenEl.replaceChild(node, kids[y]); else screenEl.append(node);
    });
    $("#size-info").textContent = f.cols + "×" + f.rows + (f.title ? "  ·  " + f.title : "");
    if (sizeChanged) fitScreen();               // e.g. an attached terminal was itself resized: re-fit the font
    if (f.title) document.title = f.title + " · pytermwm";
    if (f.cursor) {
      cursorEl.style.display = "block";
      cursorEl.style.left = (6 + f.cursor[0] * cw) + "px"; cursorEl.style.top = (6 + f.cursor[1] * ch) + "px";
      cursorEl.style.width = cw + "px"; cursorEl.style.height = ch + "px";
      cursorEl.className = f.cursor_shape >= 5 ? "bar" : f.cursor_shape >= 3 ? "under" : "";
    } else cursorEl.style.display = "none";
  }

  // When a real terminal is attached, the session follows its size and our resize requests are refused; shrinking the
  // font to make the fixed grid fit the browser window avoids an otherwise unavoidable scrollbar for that content.
  function setFontPx(px) {
    if (String(px) === String(fontInput.value)) return false;
    fontInput.value = px;
    try { localStorage.setItem("ptw-font", String(px)); } catch (e) { /* ignore */ }
    applyFont();               // re-measures cw/ch and redraws at the new size
    return true;
  }
  function fitFontToFrame(availW, availH) {
    if (!frame || !frame.cols || !frame.rows) return;
    let px = Math.max(6, Math.min(32, parseInt(fontInput.value, 10) || 14));
    // an analytic first guess to jump close to the answer in a single step
    const wPx = px * (availW / (frame.cols * cw));
    const hPx = px * (availH / (frame.rows * ch));
    px = Math.max(6, Math.min(32, Math.floor(Math.min(px, wPx, hPx))));
    setFontPx(px);
    // then correct by the actually rendered size: sub-pixel rounding in row/glyph widths means the analytic guess can be
    // a size too big by a hair, which otherwise leaves a scrollbar with nothing but a couple of empty pixels to scroll
    const main = $("main");
    for (let i = 0; i < 16 && px > 6; i++) {
      const overflowY = main.scrollHeight - main.clientHeight;
      const overflowX = wrapEl.scrollWidth - wrapEl.clientWidth;
      if (overflowY <= 0 && overflowX <= 0) break;
      px -= 1;
      setFontPx(px);
    }
  }

  let fitTimer = null;
  function fitScreen() {
    clearTimeout(fitTimer);
    fitTimer = setTimeout(async () => {
      if (!$("#tab-screen").classList.contains("active")) return;
      const main = $("main");
      const availW = main.clientWidth - 40;
      const top = wrapEl.getBoundingClientRect().top;
      const availH = window.innerHeight - top - 70;
      const cols = Math.max(20, Math.floor(availW / cw)), rows = Math.max(6, Math.floor(availH / ch));
      const k = cols + "x" + rows;
      if (k === sizeSent) { if (attachedSize && fitInput.checked) fitFontToFrame(availW, availH); return; }
      sizeSent = k;
      try {
        await api("POST", "resize", { cols, rows });
        attachedSize = false;
      } catch (e) {
        attachedSize = true;
        if (fitInput.checked) fitFontToFrame(availW, availH);
      }
    }, 250);
  }
  window.addEventListener("resize", fitScreen);

  // ---- keyboard -> terminal bytes
  const CURSOR = { ArrowUp: "A", ArrowDown: "B", ArrowRight: "C", ArrowLeft: "D", Home: "H", End: "F" };
  const TILDE = { Insert: 2, Delete: 3, PageUp: 5, PageDown: 6, F5: 15, F6: 17, F7: 18, F8: 19, F9: 20, F10: 21, F11: 23, F12: 24 };
  const SS3 = { F1: "P", F2: "Q", F3: "R", F4: "S" };
  function keyToSeq(e) {
    if (e.metaKey) return null;
    const mod = 1 + (e.shiftKey ? 1 : 0) + (e.altKey ? 2 : 0) + (e.ctrlKey ? 4 : 0);
    const k = e.key;
    if (CURSOR[k]) return mod > 1 ? "\x1b[1;" + mod + CURSOR[k] : "\x1b[" + CURSOR[k];
    if (TILDE[k]) return "\x1b[" + TILDE[k] + (mod > 1 ? ";" + mod : "") + "~";
    if (SS3[k]) return mod > 1 ? "\x1b[1;" + mod + SS3[k] : "\x1bO" + SS3[k];
    let base = null;
    if (k === "Enter") base = "\r"; else if (k === "Backspace") base = e.ctrlKey ? "\x08" : "\x7f";
    else if (k === "Tab") { if (e.shiftKey) return "\x1b[Z"; base = "\t"; } else if (k === "Escape") base = "\x1b";
    if (base !== null) return e.altKey ? "\x1b" + base : base;
    if ([...k].length !== 1) return null;                     // Shift, Control, CapsLock ...
    if (e.ctrlKey) {
      const c = k.toLowerCase();
      if (c >= "a" && c <= "z") return (e.altKey ? "\x1b" : "") + String.fromCharCode(c.charCodeAt(0) - 96);
      const map = { " ": "\x00", "@": "\x00", "[": "\x1b", "\\": "\x1c", "]": "\x1d", "^": "\x1e", "_": "\x1f", "/": "\x1f", "2": "\x00", "3": "\x1b", "4": "\x1c", "5": "\x1d", "6": "\x1e", "7": "\x1f" };
      if (map[k] !== undefined) return map[k];
      return null;
    }
    return (e.altKey ? "\x1b" : "") + k;
  }

  let pending = "", inflight = false, flushTimer = null;
  function sendInput(data) {
    pending += data;
    if (!flushTimer && !inflight) flushTimer = setTimeout(flush, 6);
  }
  async function flush() {
    flushTimer = null;
    if (inflight || !pending) return;
    const data = pending; pending = ""; inflight = true;
    try { await api("POST", "input", { data }); } catch (e) { fail(e); }
    inflight = false;
    if (pending) flushTimer = setTimeout(flush, 0);
  }
  screenEl.addEventListener("keydown", (e) => {
    const s = keyToSeq(e);
    if (s === null) return;
    e.preventDefault(); e.stopPropagation();
    sendInput(s);
  });
  screenEl.addEventListener("paste", (e) => {
    const t = (e.clipboardData || window.clipboardData).getData("text");
    if (t) { e.preventDefault(); sendInput("\x1b[200~" + t.replace(/\x1b/g, "") + "\x1b[201~"); }
  });
  // ---- mouse -> SGR sequences
  let mouseBtn = -1, lastCell = "";
  const cellAt = (e) => {
    const r = screenEl.getBoundingClientRect();
    return [Math.max(1, Math.floor((e.clientX - r.left) / cw) + 1), Math.max(1, Math.floor((e.clientY - r.top) / ch) + 1)];
  };
  const mods = (e) => (e.shiftKey ? 4 : 0) | (e.altKey ? 8 : 0) | (e.ctrlKey ? 16 : 0);
  screenEl.addEventListener("mousedown", (e) => {
    screenEl.focus(); e.preventDefault();
    mouseBtn = e.button; const [x, y] = cellAt(e); lastCell = x + "," + y;
    sendInput("\x1b[<" + (e.button | mods(e)) + ";" + x + ";" + y + "M");
  });
  window.addEventListener("mouseup", (e) => {
    if (mouseBtn < 0) return;
    const [x, y] = cellAt(e); const b = mouseBtn; mouseBtn = -1;
    sendInput("\x1b[<" + (b | mods(e)) + ";" + x + ";" + y + "m");
  });
  window.addEventListener("mousemove", (e) => {
    if (mouseBtn < 0) return;
    const [x, y] = cellAt(e), k = x + "," + y;
    if (k === lastCell) return;
    lastCell = k;
    sendInput("\x1b[<" + ((mouseBtn + 32) | mods(e)) + ";" + x + ";" + y + "M");
  });
  screenEl.addEventListener("wheel", (e) => {
    e.preventDefault();
    const [x, y] = cellAt(e);
    const n = Math.min(5, Math.max(1, Math.round(Math.abs(e.deltaY) / 40)));
    for (let i = 0; i < n; i++) sendInput("\x1b[<" + ((e.deltaY < 0 ? 64 : 65) | mods(e)) + ";" + x + ";" + y + "M");
  }, { passive: false });
  screenEl.addEventListener("contextmenu", (e) => e.preventDefault());

  // ---- toolbar selects
  async function loadThemes() {
    const r = await api("GET", "themes");
    const sel = $("#sel-theme"); sel.textContent = "";
    r.themes.forEach((t) => sel.append(el("option", { text: t, value: t })));
    sel.value = r.current;
  }
  $("#sel-theme").addEventListener("change", (e) => command("theme " + e.target.value).catch(fail));
  $("#sel-layout").addEventListener("change", (e) => command("layout " + e.target.value).catch(fail));
  async function syncState() {
    try {
      const r = await api("GET", "state"); stateCache = r.state;
      $("#session").textContent = "· " + r.state.session;
      const d = r.state.desktops[r.state.current_desktop];
      if (d && document.activeElement !== $("#sel-layout")) $("#sel-layout").value = d.layout;
      if (document.activeElement !== $("#sel-theme")) $("#sel-theme").value = r.state.theme;
      renderDesktops();
    } catch (e) { /* ignore */ }
  }

  // ---- desktops (workspaces): a small taskbar-style switcher plus create/rename/close
  function quoteArg(s) {
    return /[\s"'\\;#]/.test(s) ? '"' + s.replace(/([\\"])/g, "\\$1") + '"' : s;
  }
  function renderDesktops() {
    const bar = $("#desk-bar");
    if (!bar || !stateCache) return;
    bar.textContent = "";
    stateCache.desktops.forEach((d, i) => {
      const n = d.windows.length;
      const btn = el("button", {
        class: "desk-btn" + (i === stateCache.current_desktop ? " active" : ""),
        title: n + " window" + (n === 1 ? "" : "s") + " · " + d.layout + " · double-click to rename",
        onclick: () => { if (i !== stateCache.current_desktop) command("desktop " + (i + 1)).catch(fail); },
        ondblclick: (e) => { e.stopPropagation(); renameDesktop(i, d.name); },
      }, el("span", { text: (i + 1) + ":" + d.name }));
      if (stateCache.desktops.length > 1) {
        btn.append(el("span", {
          class: "desk-close", text: "×", title: "close this desktop and its windows",
          onclick: (e) => { e.stopPropagation(); closeDesktop(i); },
        }));
      }
      bar.append(btn);
    });
  }
  async function renameDesktop(i, current) {
    const name = window.prompt("rename desktop " + (i + 1), current);
    if (!name || name === current) return;
    try {
      if (i !== stateCache.current_desktop) await command("desktop " + (i + 1));
      await command("rename-desktop " + quoteArg(name));
      await syncState();
    } catch (e) { fail(e); }
  }
  async function closeDesktop(i) {
    const d = stateCache.desktops[i];
    if (d.windows.length && !window.confirm("close desktop " + (i + 1) + ":" + d.name + " and its " + d.windows.length + " window(s)?")) return;
    try { await command("close-desktop " + (i + 1)); await syncState(); } catch (e) { fail(e); }
  }
  $("#desk-new").addEventListener("click", async () => {
    const name = window.prompt("new desktop name (optional)", "");
    if (name === null) return;
    try { await command("new-desktop" + (name ? " " + quoteArg(name) : "")); await syncState(); } catch (e) { fail(e); }
  });

  // ---- live stream
  let es = null, evCount = 0;
  const events = [];
  function connect() {
    if (es) es.close();
    es = new EventSource("/api/stream");
    es.onopen = () => setConn(true);
    es.onerror = () => setConn(false);
    es.addEventListener("frame", (m) => { setConn(true); try { drawFrame(JSON.parse(m.data)); } catch (e) { /* bad frame */ } });
    es.addEventListener("event", (m) => {
      const e = JSON.parse(m.data); events.push(e); if (events.length > 300) events.shift();
      if (["desktop_switch", "layout_set", "theme_changed", "config_applied", "layout_changed", "desktop_created", "desktop_closed", "window_moved"].includes(e.event)) syncState();
      if ($("#tab-windows").classList.contains("active") && (/window/.test(e.event) || /desktop/.test(e.event))) loaders.windows();
      if ($("#tab-logs").classList.contains("active")) renderEvents();
      if (e.event === "notify") notifyUser(e.data || {});
      if (e.event === "config_applied" && $("#tab-automation").classList.contains("active")) loaders.automation();
    });
  }

  // ------------------------------------------------------------------ windows
  loaders.windows = async function () {
    if (!stateCache) await syncState();
    const r = await api("GET", "windows");
    const tb = $("#win-table tbody"); tb.textContent = "";
    const deskNames = stateCache ? stateCache.desktops.map((d) => d.name) : [];
    for (const w of r.windows) {
      const acts = el("td", { class: "actions" },
        el("button", { text: "focus", onclick: () => command("focus " + w.id).then(() => showTab("screen")).catch(fail) }),
        el("button", { text: "text", onclick: () => capture(w) }),
        el("button", { text: "close", class: "danger", onclick: () => api("DELETE", "window/" + w.id).then(loaders.windows).catch(fail) }));
      const state = w.exited ? el("span", { class: "pill bad", text: "exited " + (w.exit_code ?? "") }) : el("span", { class: "pill ok", text: w.floating ? "floating" : "running" });
      const sel = el("select", { class: "desk-move", title: "move to another desktop",
        onchange: (e) => moveWindow(w, e.target.value, sel) }, ...deskNames.map((n) => el("option", { text: n, value: n })),
        el("option", { value: "__new__", text: "+ new…" }));
      sel.value = w.desktop || "";
      tb.append(el("tr", {}, el("td", { class: "mono", text: w.id }), el("td", { text: (w.name ? w.name + " · " : "") + w.title }), el("td", { text: w.kind }),
        el("td", { class: "mono", text: (w.viewport || w.size || []).join("×") }), el("td", {}, state), el("td", {}, sel), acts));
    }
  };
  async function moveWindow(w, target, sel) {
    const prev = w.desktop || "";
    if (target === "__new__") {
      const name = window.prompt("new desktop name", "");
      if (!name) { sel.value = prev; return; }
      target = name;
    }
    if (target === prev) return;
    try { await command("send-to-desktop " + quoteArg(target) + " " + w.id); await syncState(); await loaders.windows(); }
    catch (e) { sel.value = prev; fail(e); }
  }
  async function capture(w) {
    try {
      const r = await api("GET", "window/" + w.id + "/text?history=1&lines=500");
      $("#cap-title").hidden = false; $("#cap-title").textContent = "window " + w.id + ": " + w.title;
      $("#capture").hidden = false; $("#capture").textContent = r.text;
    } catch (e) { fail(e); }
  }
  $("#win-refresh").addEventListener("click", () => loaders.windows().catch(fail));
  $("#new-win").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target), spec = {};
    if (f.get("cmd")) spec.cmd = f.get("cmd");
    if (f.get("title")) spec.title = f.get("title");
    if (f.get("kind")) spec.kind = f.get("kind");
    if (f.get("floating")) spec.floating = true;
    try { await api("POST", "op", { op: "create", spec }); await loaders.windows(); } catch (err) { fail(err); }
  });

  // ------------------------------------------------------------------ console
  const hist = []; let hpos = 0;
  loaders.console = async function () {
    if ($("#cmd-list").children.length) { $("#console-in").focus(); return; }
    const r = await api("GET", "commands");
    const dl = $("#cmd-list"), tb = $("#cmd-table tbody");
    for (const c of r.commands) {
      dl.append(el("option", { value: c.name }));
      tb.append(el("tr", {}, el("td", { class: "mono", text: c.name }), el("td", { class: "mono", text: c.usage }), el("td", { text: c.help })));
    }
    $("#console-in").focus();
  };
  function out(text, cls) { const o = $("#console-out"); o.append(el("div", { class: cls || "", text })); o.scrollTop = o.scrollHeight; }
  $("#console-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const inp = $("#console-in"), line = inp.value.trim();
    if (!line) return;
    hist.push(line); hpos = hist.length; inp.value = "";
    out("❯ " + line);
    try {
      const r = await command(line);
      if (r.result !== null && r.result !== undefined) out(typeof r.result === "string" ? r.result : JSON.stringify(r.result, null, 2));
    } catch (err) { out(err.message, "err"); }
  });
  $("#console-in").addEventListener("keydown", (e) => {
    if (e.key === "ArrowUp" && hpos > 0) { hpos--; e.target.value = hist[hpos]; e.preventDefault(); }
    else if (e.key === "ArrowDown") { hpos = Math.min(hist.length, hpos + 1); e.target.value = hist[hpos] || ""; e.preventDefault(); }
  });

  // ------------------------------------------------------------------ config
  let cfgLoaded = "", cfgDefault = "", valTimer = null;
  function showMsgs(box, errors, warnings, okText) {
    box.textContent = "";
    errors.forEach((m) => box.append(el("div", { class: "err", text: "✗ " + m })));
    warnings.forEach((m) => box.append(el("div", { class: "warn", text: "! " + m })));
    if (!errors.length && !warnings.length && okText) box.append(el("div", { class: "ok", text: "✓ " + okText }));
  }
  loaders.config = async function () {
    try {
      const r = await api("GET", "config");
      cfgDefault = r.default || "";
      if ($("#cfg-text").value === "" || $("#cfg-text").value === cfgLoaded) { $("#cfg-text").value = r.text; cfgLoaded = r.text; }
      $("#cfg-path").textContent = r.path ? r.path : "(no file: changes are applied in memory only)";
      const st = $("#cfg-status");
      st.textContent = r.error ? "last reload failed: " + r.error : ""; st.className = "status" + (r.error ? " bad" : "");
      validateCfg();
    } catch (e) { fail(e); }
  };
  async function validateCfg() {
    try {
      const r = await api("POST", "config/validate", { text: $("#cfg-text").value });
      showMsgs($("#cfg-msgs"), r.errors, r.warnings, "configuration is valid");
      $("#cfg-save").disabled = !r.valid;
    } catch (e) { showMsgs($("#cfg-msgs"), [e.message], []); }
  }
  $("#cfg-text").addEventListener("input", () => { clearTimeout(valTimer); valTimer = setTimeout(validateCfg, 400); });
  $("#cfg-text").addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); const t = e.target, s = t.selectionStart; t.setRangeText("  ", s, t.selectionEnd, "end"); }
    if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); $("#cfg-save").click(); }
  });
  $("#cfg-save").addEventListener("click", async () => {
    try {
      const r = await api("PUT", "config", { text: $("#cfg-text").value });
      cfgLoaded = $("#cfg-text").value;
      const st = $("#cfg-status"); st.textContent = "applied" + (r.saved ? " and saved" : " (in memory)"); st.className = "status ok";
      toast("configuration applied");
    } catch (e) { const st = $("#cfg-status"); st.textContent = e.message; st.className = "status bad"; fail(e); }
  });
  $("#cfg-reload").addEventListener("click", () => { $("#cfg-text").value = ""; loaders.config(); });
  $("#cfg-init").addEventListener("click", () => { $("#cfg-text").value = cfgDefault; validateCfg(); });

  // ------------------------------------------------------------------ automation
  loaders.automation = async function () {
    try {
      const r = await api("GET", "rules");
      const tb = $("#rule-table tbody"); tb.textContent = "";
      if (!r.rules.length) tb.append(el("tr", {}, el("td", { colspan: 5, class: "dim", text: "no rules yet: build one below or add `rules:` to the configuration" })));
      for (const ru of r.rules) {
        const acts = el("td", { class: "actions" });
        if (!ru.script) {
          acts.append(el("button", { text: ru.enabled ? "disable" : "enable", onclick: () => command("rule " + (ru.enabled ? "disable " : "enable ") + ru.name).then(loaders.automation).catch(fail) }));
          acts.append(el("button", { text: "fire", onclick: () => command("rule fire " + ru.name).then(loaders.automation).catch(fail) }));
        }
        tb.append(el("tr", {}, el("td", { class: "mono", text: ru.name }),
          el("td", { text: ru.script ? "script" : ru.trigger + (ru.enabled ? "" : "  (disabled)") }),
          el("td", { text: ru.script ? "" : String(ru.fired) }), el("td", { class: ru.error ? "status bad" : "", text: ru.error || "" }), acts));
      }
    } catch (e) { fail(e); }
  };
  function buildRule() {
    const f = new FormData($("#rule-form"));
    const val = (f.get("value") || "").trim(), trig = f.get("trigger");
    const when = {};
    if (trig === "output_matches") when.output_matches = val || ".*";
    else if (trig === "output_changed") when.output_changed = true;
    else if (trig === "idle") when.idle = Number(val) || 30;
    else if (trig === "interval") when.interval = Number(val) || 60;
    else if (trig === "exit") when.exit = val === "" ? true : (val === "error" || val === "ok" ? val : Number(val));
    else if (trig === "event") when.event = val || "window_created";
    else if (trig === "status") { const [k, ...rest] = val.split(/\s+/); when.status = k || "key"; const c = f.get("cond"); const v = rest.join(" "); when[c] = (c === "above" || c === "below") ? Number(v) : v; }
    if (f.get("window") && !["interval", "event", "status"].includes(trig)) when.window = f.get("window");
    const rule = { name: f.get("name"), when, do: (f.get("actions") || "").split("\n").map((s) => s.trim()).filter(Boolean) };
    if (f.get("undo_after")) rule.undo_after = Number(f.get("undo_after"));
    if (f.get("cooldown")) rule.cooldown = Number(f.get("cooldown"));
    return rule;
  }
  $("#rule-form").addEventListener("input", () => { $("#rule-preview").textContent = "  - " + JSON.stringify(buildRule(), null, 1).replace(/\n\s*/g, " "); });
  function addRuleToYaml(text, rule) {
    const item = "  - " + JSON.stringify(rule);
    const lines = text.split("\n");
    const idx = lines.findIndex((l) => /^rules\s*:/.test(l));
    if (idx < 0) return text.replace(/\s*$/, "\n") + "\nrules:\n" + item + "\n";
    const rest = lines[idx].replace(/^rules\s*:/, "").replace(/\s+#.*$/, "").trim();
    if (rest === "[]") lines[idx] = "rules:"; else if (rest !== "") return null;
    let end = idx + 1;
    while (end < lines.length && (lines[end].trim() === "" || /^[\s#-]/.test(lines[end]))) end++;
    let ins = end; while (ins > idx + 1 && lines[ins - 1].trim() === "") ins--;
    lines.splice(ins, 0, item);
    return lines.join("\n");
  }
  $("#rule-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const st = $("#rule-status");
    try {
      const rule = buildRule();
      if (!rule.do.length) throw new Error("add at least one action");
      const cur = await api("GET", "config");
      const next = addRuleToYaml(cur.text, rule);
      if (next === null) throw new Error("`rules:` in your config is written inline; add the rule to the Config tab by hand");
      await api("PUT", "config", { text: next });
      st.textContent = "rule added and applied"; st.className = "status ok";
      cfgLoaded = ""; $("#cfg-text").value = "";
      loaders.automation();
    } catch (err) { st.textContent = err.message; st.className = "status bad"; }
  });

  // ------------------------------------------------------------------ scripts
  const TEMPLATE = "# runs inside pytermwm: `wm` and `api` are available\n\ndef setup(api):\n    api.command(\"hello\", lambda wm, args: \"hello from a script\", help=\"say hello\")\n\n    def on_error(window, line, match):\n        api.message(\"error in window %s: %s\" % (window.id, line), \"err\")\n    api.on_output(r\"ERROR|Traceback\", on_error)\n";
  loaders.scripts = async function (select) {
    try {
      const r = await api("GET", "scripts");
      const sel = $("#script-list"); const cur = select || sel.value; sel.textContent = "";
      r.scripts.forEach((n) => sel.append(el("option", { text: n, value: n })));
      if (r.scripts.length) { sel.value = r.scripts.includes(cur) ? cur : r.scripts[0]; loadScript(); }
      else { $("#script-text").value = ""; }
    } catch (e) { fail(e); }
  };
  async function loadScript() {
    const n = $("#script-list").value; if (!n) return;
    try { const r = await api("GET", "script/" + encodeURIComponent(n)); $("#script-text").value = r.text; $("#script-status").textContent = ""; } catch (e) { fail(e); }
  }
  $("#script-list").addEventListener("change", loadScript);
  $("#script-new").addEventListener("click", () => {
    let n = window.prompt("new script file name (letters, digits, _ - . and .py)", "my_script.py");
    if (!n) return; if (!n.endsWith(".py")) n += ".py";
    const sel = $("#script-list"); if (![...sel.options].some((o) => o.value === n)) sel.append(el("option", { text: n, value: n }));
    sel.value = n; $("#script-text").value = TEMPLATE; $("#script-status").textContent = "not saved yet"; $("#script-status").className = "status";
  });
  $("#script-save").addEventListener("click", async () => {
    const n = $("#script-list").value, st = $("#script-status");
    if (!n) { st.textContent = "create a script first"; return; }
    try { await api("PUT", "script/" + encodeURIComponent(n), { text: $("#script-text").value }); st.textContent = "saved and loaded"; st.className = "status ok"; }
    catch (e) { st.textContent = e.message; st.className = "status bad"; }
  });
  $("#script-text").addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); const t = e.target, s = t.selectionStart; t.setRangeText("    ", s, t.selectionEnd, "end"); }
    if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); $("#script-save").click(); }
  });

  // ------------------------------------------------------------------ plugins
  loaders.plugins = async function () {
    try {
      const r = await api("GET", "plugins");
      const tb = $("#plugin-table tbody"); tb.textContent = "";
      for (const p of r.plugins) {
        const provides = [].concat(p.commands || [], (p.segments || []).map((s) => "segment:" + s), (p.kinds || []).map((k) => "window:" + k)).join(", ");
        const acts = el("td", { class: "actions" });
        const run = (c) => command("plugin " + c + " " + p.name).then(loaders.plugins).catch(fail);
        if (p.loaded) { acts.append(el("button", { text: "reload", onclick: () => run("reload") })); acts.append(el("button", { text: "unload", class: "danger", onclick: () => run("unload") })); }
        else acts.append(el("button", { text: "load", onclick: () => run("load") }));
        tb.append(el("tr", {}, el("td", { class: "mono", text: p.name }),
          el("td", {}, p.loaded ? el("span", { class: "pill ok", text: "loaded" }) : el("span", { class: "pill" + (p.error ? " bad" : ""), text: p.error ? "error" : "available" }), p.error ? el("div", { class: "status bad", text: p.error }) : ""),
          el("td", { class: "mono", text: provides }), acts));
      }
    } catch (e) { fail(e); }
  };

  // ------------------------------------------------------------------ logs
  let logTimer = null;
  loaders.logs = function () { refreshLogs(); renderEvents(); clearInterval(logTimer); logTimer = setInterval(() => { if ($("#tab-logs").classList.contains("active")) refreshLogs(); else clearInterval(logTimer); }, 2000); };
  async function refreshLogs() {
    const q = new URLSearchParams({ n: 300 });
    if ($("#log-level").value) q.set("level", $("#log-level").value);
    if ($("#log-pattern").value) q.set("pattern", $("#log-pattern").value);
    try {
      const r = await api("GET", "logs?" + q.toString());
      const text = r.logs.map((l) => new Date(l.time * 1000).toLocaleTimeString() + " " + l.level.toUpperCase().padEnd(7) + " " + l.name + ": " + l.message).join("\n");
      const o = $("#log-out");
      if (text === o.textContent) return;              // nothing new: leave the scroll position (and DOM) alone
      o.textContent = text;
      if ($("#log-follow").checked) o.scrollTop = o.scrollHeight;
    } catch (e) { /* ignore */ }
  }
  function renderEvents() {
    const o = $("#event-out");
    const text = events.slice(-200).map((e) => new Date(e.time * 1000).toLocaleTimeString() + " " + e.event + " " + JSON.stringify(e.data)).join("\n");
    if (text === o.textContent) return;                 // nothing new: leave the scroll position alone
    o.textContent = text;
    o.scrollTop = o.scrollHeight;
  }
  $("#log-level").addEventListener("change", refreshLogs);
  $("#log-pattern").addEventListener("input", () => { clearTimeout(logTimer); logTimer = setTimeout(loaders.logs, 300); });

  // ------------------------------------------------------------------ boot
  applyFont();
  connect(); syncState(); loadThemes().catch(() => {});
  const start = (location.hash || "#screen").slice(1);
  showTab(document.getElementById("tab-" + start) ? start : "screen");
  api("GET", "state").catch(() => {});
})();
