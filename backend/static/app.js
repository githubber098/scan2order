/* =============================================================================
   scan2order — app.js  (shared client logic for all pages)
   - Inline SVG icon set
   - Theme application + server-persist (POST /api/profile/theme)
   - Bottom-tab / sidebar active state
   - Modal open/close helper
   - Browser-relay connect modal (WebSocket screenshot stream)
   - OCR upload helper (shared; index.html page wires up the result)
   - Logout
   ============================================================================= */
(function () {
  "use strict";

  /* ---- icons ----------------------------------------------------------- */
  const I = {
    scan:    '<path d="M3 8V5a2 2 0 0 1 2-2h3M16 3h3a2 2 0 0 1 2 2v3M21 16v3a2 2 0 0 1-2 2h-3M8 21H5a2 2 0 0 1-2-2v-3"/><path d="M3 12h18"/>',
    camera:  '<rect x="3" y="6" width="18" height="14" rx="2.5"/><circle cx="12" cy="13" r="3.4"/><path d="M8 6l1.5-2.5h5L16 6"/>',
    compare: '<path d="M4 7h11M4 12h16M4 17h8"/><circle cx="19" cy="7" r="1.4"/>',
    history: '<path d="M3 12a9 9 0 1 0 3-6.7M3 4v4h4"/><path d="M12 8v4l3 2"/>',
    user:    '<circle cx="12" cy="8" r="3.6"/><path d="M5 20c1.2-3.6 4-5 7-5s5.8 1.4 7 5"/>',
    cart:    '<circle cx="9" cy="20" r="1.6"/><circle cx="18" cy="20" r="1.6"/><path d="M2 3h3l2.5 13h11l2-9H6"/>',
    check:   '<path d="M4 12.5l5 5 11-11"/>',
    chevron: '<path d="M9 6l6 6-6 6"/>',
    spark:   '<path d="M12 3l2.2 6.3L20 12l-5.8 2.7L12 21l-2.2-6.3L4 12l5.8-2.7z"/>',
    bolt:    '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
    phone:   '<rect x="6" y="2.5" width="12" height="19" rx="3"/><path d="M10 18.5h4"/>',
    mail:    '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M4 7l8 6 8-6"/>',
    plus:    '<path d="M12 5v14M5 12h14"/>',
    trash:   '<path d="M4 7h16M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2M6 7l1 13h10l1-13"/>',
    signout: '<path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4M9 12h11M16 8l4 4-4 4"/>',
    refresh: '<path d="M20 11a8 8 0 1 0-.5 4M20 5v6h-6"/>',
    paint:   '<path d="M4 11V6a2 2 0 0 1 2-2h11a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H8a2 2 0 0 0-2 2v2a2 2 0 0 0 2 2h1"/><rect x="8" y="18" width="5" height="4" rx="1"/>',
    edit:    '<path d="M4 20h4L19 9l-4-4L4 16v4z"/><path d="M14 6l4 4"/>',
    arrow:   '<path d="M5 12h14M13 6l6 6-6 6"/>',
    x:       '<path d="M6 6l12 12M18 6L6 18"/>',
    plug:    '<path d="M9 2v6M15 2v6M7 8h10v3a5 5 0 0 1-10 0V8zM12 16v6"/>',
    spinner: '<path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>',
  };
  function paintIcons(root) {
    (root || document).querySelectorAll("i[data-ico]").forEach((el) => {
      const name = el.getAttribute("data-ico");
      if (!I[name] || el.dataset.done) return;
      const s = el.getAttribute("data-s") || 22, w = el.getAttribute("data-w") || 2;
      el.innerHTML = `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round">${I[name]}</svg>`;
      el.dataset.done = "1"; el.style.display = "inline-flex"; el.style.lineHeight = "0";
    });
  }

  /* ---- theme ----------------------------------------------------------- */
  const THEMES = ["fresh", "night", "aurora", "mono", "light", "brutal"];
  function qp(k) { return new URLSearchParams(location.search).get(k); }

  function currentTheme() {
    // Priority: ?theme= (preview canvas) > server-injected data-theme > localStorage > 'fresh'
    const q = qp("theme");
    if (q && THEMES.includes(q)) return q;
    // Server injects data-theme on <html> from user.theme — no flash, no race.
    const srv = document.documentElement.getAttribute("data-theme");
    if (srv && THEMES.includes(srv) && srv !== "fresh") return srv;
    try { const s = localStorage.getItem("s2o-theme"); if (s && THEMES.includes(s)) return s; } catch (_) {}
    return srv || "fresh";
  }

  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("s2o-theme", t); } catch (_) {}
  }

  function saveTheme(t) {
    applyTheme(t);
    // Persist to server so theme follows the user across devices.
    fetch("/api/profile/theme", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ theme: t }),
    }).catch(() => {});
  }

  /* ---- modal ----------------------------------------------------------- */
  window.openModal  = function (id) { const m = document.getElementById(id); if (m) m.classList.add("open"); };
  window.closeModal = function (id) { const m = document.getElementById(id); if (m) m.classList.remove("open"); };

  /* ---- theme switcher -------------------------------------------------- */
  function wireSwitcher() {
    const cur = currentTheme();
    document.querySelectorAll("[data-theme-pick]").forEach((sw) => {
      if (sw.getAttribute("data-theme-pick") === cur) sw.classList.add("sel");
      sw.addEventListener("click", () => {
        document.querySelectorAll("[data-theme-pick]").forEach((s) => s.classList.remove("sel"));
        sw.classList.add("sel");
        saveTheme(sw.getAttribute("data-theme-pick"));
      });
    });
  }

  /* ---- logout ---------------------------------------------------------- */
  window.logout = async function () {
    await fetch("/api/auth/logout", { method: "POST" }).catch(() => {});
    window.location.href = "/login";
  };

  /* ===================================================================
     Browser-relay connect modal
     Streams a live screenshot of a headless Chromium window so the user
     can log in to a store inside the app.
     =================================================================== */

  let _browserSessionId = null;
  let _screenshotLoopActive = false;
  let _checkTimer = null;
  let _browserStartToken = 0;
  let _browserWs = null;
  let _wsGotFrame = false;
  let _typeBuffer = "";
  let _typeFlushTimer = null;
  let _connectingStore = null;     // store slug being connected
  let _connectBtnEl = null;        // the button that triggered connect (to restore it)

  function _flushTypeBuffer() {
    clearTimeout(_typeFlushTimer); _typeFlushTimer = null;
    if (!_typeBuffer || !_browserSessionId) { _typeBuffer = ""; return; }
    const text = _typeBuffer; _typeBuffer = "";
    _sendBrowserEvent({ type: "type", text });
  }

  function _browserKeyHandler(e) {
    if (e.ctrlKey || e.metaKey) return;
    const ignore = ["Shift","Alt","Meta","Control","CapsLock","Fn","Dead"];
    if (ignore.includes(e.key)) return;
    e.preventDefault();
    if (e.key.length === 1) {
      _typeBuffer += e.key;
      clearTimeout(_typeFlushTimer);
      _typeFlushTimer = setTimeout(_flushTypeBuffer, 30);
    } else {
      _flushTypeBuffer();
      _sendBrowserEvent({ type: "key", key: e.key });
    }
  }

  function _initBrowserModal() {
    const img = document.getElementById("browser-screenshot");
    if (!img) return;
    img.addEventListener("click", (e) => {
      const rect = img.getBoundingClientRect();
      const scaleX = img.naturalWidth  / rect.width;
      const scaleY = img.naturalHeight / rect.height;
      _placeCaret(e.clientX - rect.left, e.clientY - rect.top);
      _sendBrowserEvent({ type:"click", x:Math.round((e.clientX-rect.left)*scaleX), y:Math.round((e.clientY-rect.top)*scaleY) });
    });
  }

  function _placeCaret(cx, cy) {
    const img = document.getElementById("browser-screenshot");
    const caret = document.getElementById("browser-caret");
    if (!img || !caret) return;
    const rect = img.getBoundingClientRect();
    caret.style.left = Math.min(Math.max(cx, 0), rect.width)  + "px";
    caret.style.top  = Math.min(Math.max(cy, 0), rect.height) + "px";
    caret.style.display = "block";
    clearTimeout(caret._t); caret._t = setTimeout(() => caret.style.display="none", 1500);
  }

  async function _sendBrowserEvent(payload) {
    if (!_browserSessionId) return;
    if (_browserWs && _browserWs.readyState === WebSocket.OPEN) {
      _browserWs.send(JSON.stringify(payload)); return;
    }
    fetch(`/api/auth/browser/event/${_browserSessionId}`, {
      method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload),
    }).catch(() => {});
  }

  function _startScreenshotStream() {
    const sid = _browserSessionId;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    let ws;
    try { ws = new WebSocket(`${proto}//${location.host}/api/auth/browser/ws/${sid}`); }
    catch { _runScreenshotLoop(); return; }
    _browserWs = ws; _wsGotFrame = false;
    const img = document.getElementById("browser-screenshot");
    ws.onmessage = (ev) => {
      if (!img || _browserSessionId !== sid) { ws.close(); return; }
      if (typeof ev.data === "string") { try { const d=JSON.parse(ev.data); if(d.done) { _handleConnectDone(); ws.close(); } } catch(_){} return; }
      _wsGotFrame = true;
      const old = img.src; img.src = URL.createObjectURL(ev.data); if(old.startsWith("blob:")) URL.revokeObjectURL(old);
    };
    ws.onerror = () => { if (!_wsGotFrame) _runScreenshotLoop(); };
    ws.onclose = () => { _browserWs = null; if (!_wsGotFrame && _browserSessionId === sid) _runScreenshotLoop(); };
    setTimeout(() => { if (!_wsGotFrame && ws.readyState !== WebSocket.OPEN) _runScreenshotLoop(); }, 2500);
  }

  async function _runScreenshotLoop() {
    const sid = _browserSessionId;
    const img = document.getElementById("browser-screenshot");
    while (_screenshotLoopActive && _browserSessionId === sid) {
      try {
        const r = await fetch(`/api/auth/browser/screenshot/${sid}`, { cache:"no-store" });
        if (!r.ok || _browserSessionId !== sid) break;
        const blob = await r.blob();
        if (img && _browserSessionId === sid) { const old=img.src; img.src=URL.createObjectURL(blob); if(old.startsWith("blob:"))URL.revokeObjectURL(old); }
      } catch (_) {}
      await new Promise(r => setTimeout(r, 800));
    }
  }

  async function _checkBrowserAuth() {
    if (!_browserSessionId) return;
    try {
      const r = await fetch(`/api/auth/browser/check/${_browserSessionId}`);
      const d = await r.json();
      if (d.done) _handleConnectDone();
      else _checkTimer = setTimeout(_checkBrowserAuth, 2000);
    } catch (_) { _checkTimer = setTimeout(_checkBrowserAuth, 2000); }
  }

  function _handleConnectDone() {
    clearTimeout(_checkTimer);
    _closeBrowserModal();
    // Refresh the store status row on profile page if present
    if (typeof refreshStoreRows === "function") refreshStoreRows();
  }

  async function _closeBrowserModal() {
    const sid = _browserSessionId;
    _browserSessionId = null;
    _screenshotLoopActive = false;
    clearTimeout(_checkTimer);
    if (_browserWs) { try { _browserWs.close(); } catch(_){} _browserWs = null; }
    document.removeEventListener("keydown", _browserKeyHandler);
    const modal = document.getElementById("connect-modal");
    if (modal) modal.classList.remove("open");
    if (sid) {
      fetch(`/api/auth/browser/session/${sid}`, { method:"DELETE" }).catch(()=>{});
    }
    // Restore button text
    if (_connectBtnEl) {
      _connectBtnEl.disabled = false;
      _connectBtnEl.innerHTML = '<i data-ico="plug" data-s="15"></i>Connect';
      paintIcons(_connectBtnEl);
      _connectBtnEl = null;
    }
  }

  // PUBLIC: called by Connect buttons across the app.
  window.connectStore = async function (store, btn) {
    _connectingStore = store;
    _connectBtnEl = btn || null;
    if (btn) { btn.disabled = true; btn.textContent = "Opening…"; }

    const token = ++_browserStartToken;
    try {
      const r = await fetch(`/api/auth/browser/start/${store}`, { method:"POST" });
      const d = await r.json();
      if (token !== _browserStartToken) return; // aborted
      if (d.error) { alert(d.error); if (btn) { btn.disabled=false; btn.textContent="Connect"; } return; }

      _browserSessionId = d.session_id;
      _screenshotLoopActive = true;

      // Open the connect modal
      const modal = document.getElementById("connect-modal");
      const title = document.getElementById("connect-modal-title");
      if (title) title.textContent = "Connect " + ({blinkit:"Blinkit",zepto:"Zepto",bigbasket:"BigBasket"}[store]||store);
      if (modal) modal.classList.add("open");

      document.addEventListener("keydown", _browserKeyHandler);
      _startScreenshotStream();
      _checkTimer = setTimeout(_checkBrowserAuth, 3000);
    } catch (e) {
      alert("Could not start browser session: " + e.message);
      if (btn) { btn.disabled=false; btn.textContent="Connect"; }
    }
  };

  window.closeConnectModal = function () { _closeBrowserModal(); };

  /* ---- OCR upload ------------------------------------------------------ */
  // Page calls:  document.getElementById('ocr-input').addEventListener('change', s2o.handleOcr)
  // Textarea ID: 'items'   Status el ID: 'ocr-status'   Cancel btn ID: 'ocr-cancel'
  let _ocrAbort = null;

  window.s2o_handleOcr = async function (event) {
    const file = event.target.files[0];
    const status  = document.getElementById("ocr-status");
    const cancelBtn = document.getElementById("ocr-cancel");
    if (!file) return;
    if (status) status.textContent = "Scanning…";
    if (cancelBtn) cancelBtn.style.display = "inline-flex";
    _ocrAbort = new AbortController();
    const fd = new FormData();
    fd.append("image", file);
    try {
      const r = await fetch("/api/ocr", { method:"POST", body:fd, signal:_ocrAbort.signal });
      const d = await r.json();
      if (d.error) { if (status) status.textContent = "OCR error: " + d.error; return; }
      const items = d.items || [];
      if (!items.length) { if (status) status.textContent = "No items found. Try a clearer, well-lit photo."; return; }
      const ta = document.getElementById("items");
      if (ta) ta.value = items.join("\n");
      if (status) status.textContent = "✓ Found " + items.length + " item" + (items.length===1?"":"s");
    } catch (e) {
      if (e.name === "AbortError") { if (status) status.textContent = "Scan cancelled."; }
      else { if (status) status.textContent = "Error: " + e.message; }
    } finally {
      if (cancelBtn) cancelBtn.style.display = "none";
      _ocrAbort = null;
      event.target.value = "";
    }
  };

  window.s2o_cancelOcr = function () { if (_ocrAbort) _ocrAbort.abort(); };

  /* ---- init ------------------------------------------------------------ */
  document.documentElement.setAttribute("data-theme", currentTheme());
  document.addEventListener("DOMContentLoaded", function () {
    paintIcons(document);
    wireSwitcher();
    _initBrowserModal();

    // Active nav from <body data-page="compare|history|profile">
    const page = document.body.getAttribute("data-page");
    document.querySelectorAll("[data-nav]").forEach((el) => {
      if (el.getAttribute("data-nav") === page) el.classList.add("on");
    });

    // Preserve ?theme= preview across navigation
    const t = qp("theme");
    if (t) document.querySelectorAll("a[data-navlink]").forEach((a) => {
      a.setAttribute("href", a.getAttribute("href").split("?")[0] + "?theme=" + t);
    });

    // Redirect to login if no session
    if (window._SERVER_USER === undefined && document.body.dataset.page !== "login" && document.body.dataset.page !== "onboarding") {
      window.location.href = "/login";
    }
  });

  window.s2o = { paintIcons, applyTheme, saveTheme, THEMES };
})();
