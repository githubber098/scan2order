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
    search:  '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    mic:     '<rect x="9" y="2" width="6" height="11" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v4M8 21h8"/>',
    lock:    '<rect x="4" y="10" width="16" height="11" rx="2.5"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
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
    // ?theme= is used only by the design-preview canvas.
    const q = qp("theme");
    if (q && THEMES.includes(q)) return q;
    // Authenticated pages: the per-user theme the server injects is the single
    // source of truth. It MUST win over any value a previous user left behind,
    // otherwise the theme leaks across logout / account switch. We deliberately
    // do NOT read localStorage here for that reason.
    const su = window._SERVER_USER;
    if (su && su.theme && THEMES.includes(su.theme)) return su.theme;
    if (su) {
      const srv = document.documentElement.getAttribute("data-theme");
      return (srv && THEMES.includes(srv)) ? srv : "fresh";
    }
    // Unauthenticated (login / onboarding) or no server user: default theme.
    return "fresh";
  }

  function applyTheme(t) {
    // Apply for the current page only. Persistence is per-account on the server
    // (saveTheme → POST /api/profile/theme); we intentionally do not mirror to
    // localStorage so a logged-out browser never carries a stale theme.
    document.documentElement.setAttribute("data-theme", t);
  }

  function saveTheme(t) {
    applyTheme(t);
    // Keep the in-page server-user object in sync so currentTheme() stays
    // correct without a reload.
    if (window._SERVER_USER) window._SERVER_USER.theme = t;
    // Persist to the account so the theme follows the user across devices.
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
    // Reset to the default theme on the way out so the login page (and any next
    // account) never inherits this user's look.
    try { localStorage.removeItem("s2o-theme"); } catch (_) {}
    document.documentElement.setAttribute("data-theme", "fresh");
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

  // Ask the browser for GPS so the store's location prompt resolves inside the
  // headless session. Resolves to null (never rejects) so connect proceeds even
  // when permission is denied or unavailable.
  function _getGeolocation() {
    return new Promise((resolve) => {
      if (!navigator.geolocation) return resolve(null);
      navigator.geolocation.getCurrentPosition(
        (pos) => resolve({ latitude: pos.coords.latitude, longitude: pos.coords.longitude }),
        () => resolve(null),
        { timeout: 5000, maximumAge: 600000 }
      );
    });
  }

  // PUBLIC: called by Connect buttons across the app.
  window.connectStore = async function (store, btn) {
    _connectingStore = store;
    _connectBtnEl = btn || null;
    if (btn) { btn.disabled = true; btn.textContent = "Opening…"; }

    const token = ++_browserStartToken;
    try {
      const geolocation = await _getGeolocation();
      if (token !== _browserStartToken) return; // aborted during the GPS prompt
      // Always send a JSON body — the endpoint calls request.json(); an empty
      // POST body would 500 with a plain-text "Internal Server Error".
      const r = await fetch(`/api/auth/browser/start/${store}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ geolocation }),
      });
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

  /* ---- voice input (Web Speech API) ------------------------------------ */
  // Shared voice-to-text used by every input field with a mic button. Falls
  // back gracefully (hides the button) where the browser has no SpeechRecognition
  // (e.g. Firefox). attachVoice(inputEl, micBtnEl, {append, onText}).
  const _SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  function voiceSupported() { return !!_SR; }

  function attachVoice(input, btn, opts) {
    opts = opts || {};
    if (!input || !btn) return;
    if (!_SR) { btn.style.display = "none"; return; }  // unsupported → hide mic
    let rec = null, listening = false;
    const stop = () => { try { rec && rec.stop(); } catch (_) {} };
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      if (listening) { stop(); return; }
      rec = new _SR();
      rec.lang = "en-IN";
      rec.interimResults = true;
      rec.continuous = false;
      let finalText = "";
      rec.onstart = () => { listening = true; btn.classList.add("listening"); };
      rec.onend = () => { listening = false; btn.classList.remove("listening"); };
      rec.onerror = () => { listening = false; btn.classList.remove("listening"); };
      rec.onresult = (ev) => {
        let interim = "";
        finalText = "";
        for (let i = 0; i < ev.results.length; i++) {
          const t = ev.results[i][0].transcript;
          if (ev.results[i].isFinal) finalText += t;
          else interim += t;
        }
        const text = (finalText || interim).trim();
        if (opts.onText) { opts.onText(text, !!finalText); return; }
        if (opts.append && input.value.trim()) {
          input.value = input.value.replace(/\s*$/, "") + "\n" + text;
        } else {
          input.value = text;
        }
        input.dispatchEvent(new Event("input", { bubbles: true }));
      };
      try { rec.start(); } catch (_) {}
    });
  }

  /* ---- guest mode ------------------------------------------------------ */
  // A logged-out visitor: no server user_id. They can browse Shop + Compare
  // (cheapest-only) but Add-to-Cart / full comparison / History are gated.
  function isGuest() {
    return window._SERVER_GUEST === true || !window._SERVER_USER_ID;
  }

  // Non-intrusive bottom-sheet asking the guest to log in. Backed by markup in
  // base.html (#login-prompt). message overrides the default copy.
  window.s2o_loginPrompt = function (message) {
    const sheet = document.getElementById("login-prompt");
    if (!sheet) { if (confirm("Log in to unlock this feature?")) location.href = "/login"; return; }
    const msg = sheet.querySelector("[data-lp-msg]");
    if (msg && message) msg.textContent = message;
    sheet.classList.add("open");
  };
  window.s2o_closeLoginPrompt = function () {
    const sheet = document.getElementById("login-prompt");
    if (sheet) sheet.classList.remove("open");
  };

  /* ---- shop cart (client-side, per user) ------------------------------- */
  // Tracks items added via the Shop tab so the header badge can show a running
  // count across all apps. This reflects what THIS browser added this session,
  // not a live fetch of each store's real cart. Keyed by user so it never
  // leaks across accounts; guests get no cart.
  function _cartKey() {
    const uid = window._SERVER_USER_ID || "anon";
    return "s2o-shopcart-" + uid;
  }
  function shopCartGet() {
    try { return JSON.parse(localStorage.getItem(_cartKey()) || "{}"); }
    catch (_) { return {}; }
  }
  function shopCartSet(c) {
    try { localStorage.setItem(_cartKey(), JSON.stringify(c)); } catch (_) {}
    updateCartBadge();
  }
  function shopCartCount() {
    const c = shopCartGet();
    let n = 0;
    for (const app of Object.keys(c)) {
      for (const pid of Object.keys(c[app] || {})) n += (c[app][pid].count || 1);
    }
    return n;
  }
  // Add/increment one product in the per-app cart; returns the full per-app list.
  function shopCartAdd(product, delta) {
    const c = shopCartGet();
    const app = product.app;
    if (!c[app]) c[app] = {};
    const pid = String(product.product_id);
    const cur = c[app][pid] || { ...product, count: 0 };
    const next = (cur.count || 0) + (delta == null ? 1 : delta);
    if (next <= 0) {
      delete c[app][pid];          // removed (e.g. rolling back a failed add)
    } else {
      cur.count = Math.min(99, next);
      c[app][pid] = cur;
    }
    shopCartSet(c);
    return Object.values(c[app] || {});
  }
  function updateCartBadge() {
    const badges = document.querySelectorAll(".cart-badge");
    if (!badges.length) return;
    const n = isGuest() ? 0 : shopCartCount();
    badges.forEach((badge) => {
      badge.textContent = n;
      badge.style.display = n > 0 ? "flex" : "none";
    });
  }
  // Current quantity of a product in the cart (0 if not present).
  function shopCartGetQty(product) {
    const c = shopCartGet();
    return ((c[product.app] || {})[String(product.product_id)] || {}).count || 0;
  }
  // All items for one app, as an array (for re-syncing the full per-app list).
  function shopCartItems(app) {
    return Object.values(shopCartGet()[app] || {});
  }
  // Set an absolute quantity; qty <= 0 removes the item. Returns the full
  // per-app list so the caller can re-sync the store cart.
  function shopCartSetQty(product, qty) {
    const c = shopCartGet();
    const app = product.app;
    if (!c[app]) c[app] = {};
    const pid = String(product.product_id);
    if (qty <= 0) {
      delete c[app][pid];
    } else {
      const cur = c[app][pid] || { ...product };
      cur.count = Math.min(99, qty);
      c[app][pid] = cur;
    }
    shopCartSet(c);
    return Object.values(c[app] || {});
  }

  /* ---- greeting toast (Shop / Compare) --------------------------------- */
  // Small friendly "Hi, <name>" that slides in on load and auto-dismisses.
  window.s2o_greet = function () {
    const el = document.getElementById("greet-toast");
    if (!el) return;
    const su = window._SERVER_USER;
    const name = (su && su.name) ? su.name : (isGuest() ? "there" : "");
    const hellos = ["Hi", "Hello", "Hey", "Welcome back"];
    // Vary the greeting without Math.random (unavailable in some sandboxes):
    const pick = hellos[(new Date().getMinutes()) % hellos.length];
    el.textContent = name ? `${pick}, ${name} 👋` : `${pick} 👋`;
    requestAnimationFrame(() => el.classList.add("show"));
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove("show"), 3600);
  };

  /* ---- sign-out confirmation ------------------------------------------- */
  window.s2o_confirmSignout = function () {
    const m = document.getElementById("signout-confirm");
    if (m) m.classList.add("open"); else logout();
  };
  window.s2o_closeSignout = function () {
    const m = document.getElementById("signout-confirm");
    if (m) m.classList.remove("open");
  };

  /* ---- init ------------------------------------------------------------ */
  document.documentElement.setAttribute("data-theme", currentTheme());
  document.addEventListener("DOMContentLoaded", function () {
    paintIcons(document);
    wireSwitcher();
    _initBrowserModal();

    // Active nav from <body data-page="compare|shop|history|profile">
    const page = document.body.getAttribute("data-page");
    document.querySelectorAll("[data-nav]").forEach((el) => {
      if (el.getAttribute("data-nav") === page) el.classList.add("on");
    });

    // Guest mode: hide login-gated nav (History) and show the cart count.
    if (isGuest()) {
      document.querySelectorAll("[data-guest-hide]").forEach((el) => {
        el.style.display = "none";
      });
      document.body.classList.add("is-guest");
    }
    updateCartBadge();

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

  window.s2o = {
    paintIcons, applyTheme, saveTheme, THEMES,
    attachVoice, voiceSupported, isGuest,
    shopCartGet, shopCartSet, shopCartAdd, shopCartCount, updateCartBadge,
    shopCartGetQty, shopCartItems, shopCartSetQty,
  };
})();
