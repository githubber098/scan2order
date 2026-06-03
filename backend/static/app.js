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
    // Clear recent searches so the next visitor doesn't see this user's history.
    try { localStorage.removeItem("s2o-recent"); } catch (_) {}
    document.documentElement.setAttribute("data-theme", "fresh");
    await fetch("/api/auth/logout", { method: "POST" }).catch(() => {});
    // Go to the compare page (guest mode) not the login wall, so the app stays
    // usable after signing out.
    window.location.href = "/";
  };

  /* ===================================================================
     Browser-relay connect modal  (restored from main's working version)
     Streams a live Chromium window (CDP screencast over WebSocket, with a
     screenshot-polling fallback) so the user can log in to a store inside the
     app. Clicks send NORMALISED 0-1 coords (what the backend expects); the
     wheel forwards scroll; keystrokes are batched. No fake caret — the page's
     own caret shows in the stream.
     =================================================================== */

  const _STORE_LABEL = { bigbasket: "BigBasket", blinkit: "Blinkit", zepto: "Zepto", instamart: "Instamart" };
  let _browserSessionId = null;
  let _screenshotLoopActive = false;
  let _checkTimer = null;
  let _browserStartToken = 0;       // bumped on close/cancel to abort an in-flight start
  let _browserWs = null;
  let _wsGotFrame = false;
  let _pendingFrame = null;         // newest stream frame awaiting paint (older dropped)
  let _rafScheduled = false;
  let _typeBuffer = "";
  let _typeFlushTimer = null;

  function _flushTypeBuffer() {
    clearTimeout(_typeFlushTimer); _typeFlushTimer = null;
    if (!_typeBuffer || !_browserSessionId) { _typeBuffer = ""; return; }
    const text = _typeBuffer; _typeBuffer = "";
    _sendBrowserEvent({ type: "type", text });
  }

  // Document-level key handler (active only while the modal is open) so the
  // user can type straight into the page without focusing a separate field.
  function _browserKeyHandler(e) {
    if (e.ctrlKey || e.metaKey) return;   // let Ctrl+R, F12, etc. through
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
    // Click → normalised 0-1 coords (the backend maps them onto the 430×700
    // viewport). Dividing by the rendered rect makes this pixel-accurate
    // because the img's aspect-ratio matches the viewport (no letterboxing).
    img.addEventListener("click", (e) => {
      const rect = img.getBoundingClientRect();
      const nx = (e.clientX - rect.left) / rect.width;
      const ny = (e.clientY - rect.top) / rect.height;
      _sendBrowserEvent({ type: "click", nx, ny });
      _showClickRipple(e.clientX, e.clientY);
    });
    // Wheel → forward scroll delta.
    img.addEventListener("wheel", (e) => {
      e.preventDefault();
      _sendBrowserEvent({ type: "scroll", delta_y: e.deltaY });
    }, { passive: false });
  }

  // Brief teal ripple at the tap point so it's obvious the click registered.
  function _showClickRipple(clientX, clientY) {
    const box = document.querySelector(".browser-box");
    if (!box) return;
    const r = box.getBoundingClientRect();
    const dot = document.createElement("div");
    dot.className = "click-ripple";
    dot.style.left = (clientX - r.left) + "px";
    dot.style.top = (clientY - r.top) + "px";
    box.appendChild(dot);
    setTimeout(() => dot.remove(), 520);
  }

  async function _sendBrowserEvent(payload) {
    if (!_browserSessionId) return;
    // Prefer the open WebSocket so a click/keystroke skips the HTTP round-trip.
    if (_browserWs && _browserWs.readyState === 1) {
      try { _browserWs.send(JSON.stringify(payload)); return; } catch (_) {}
    }
    fetch(`/api/auth/browser/event/${_browserSessionId}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).catch(() => {});
  }

  // Paint the newest frame at most once per animation frame (drop stale frames
  // so latency never builds up → smooth, no lag pile-up).
  function _paintFrame() {
    _rafScheduled = false;
    const blob = _pendingFrame; _pendingFrame = null;
    if (!blob || !_browserSessionId) return;
    const img = document.getElementById("browser-screenshot");
    const url = URL.createObjectURL(blob);
    const old = img.src; img.src = url;
    if (old && old.startsWith("blob:")) URL.revokeObjectURL(old);
  }

  function _startScreenshotStream() {
    _wsGotFrame = false;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const sid = _browserSessionId;
    let ws;
    try { ws = new WebSocket(`${proto}//${location.host}/api/auth/browser/ws/${sid}`); }
    catch (e) { _runScreenshotLoop(); return; }
    ws.binaryType = "blob";
    _browserWs = ws;
    ws.onmessage = (ev) => {
      if (!_browserSessionId || _browserWs !== ws) { try { ws.close(); } catch(_){} return; }
      _wsGotFrame = true;
      _pendingFrame = ev.data;
      if (!_rafScheduled) { _rafScheduled = true; requestAnimationFrame(_paintFrame); }
    };
    ws.onclose = () => {
      if (_browserWs === ws) _browserWs = null;
      if (_browserSessionId && sid === _browserSessionId && !_wsGotFrame) _runScreenshotLoop();
    };
    ws.onerror = () => { try { ws.close(); } catch(_){} };
  }

  // Fallback: sequential screenshot polling when the WebSocket never opens.
  async function _runScreenshotLoop() {
    _screenshotLoopActive = true;
    const img = document.getElementById("browser-screenshot");
    while (_screenshotLoopActive && _browserSessionId) {
      const t0 = Date.now();
      try {
        const r = await fetch(`/api/auth/browser/screenshot/${_browserSessionId}?t=${t0}`, { cache: "no-store" });
        if (!_screenshotLoopActive || !_browserSessionId) break;
        if (r.ok) {
          const blob = await r.blob();
          if (!_screenshotLoopActive || !_browserSessionId) break;
          const old = img.src; img.src = URL.createObjectURL(blob);
          if (old && old.startsWith("blob:")) URL.revokeObjectURL(old);
        }
      } catch (_) { if (!_browserSessionId) break; }
      const gap = Date.now() - t0;
      if (gap < 80) await new Promise(r => setTimeout(r, 80 - gap));
    }
    _screenshotLoopActive = false;
  }

  async function _checkBrowserAuth() {
    if (!_browserSessionId) return;
    try {
      const r = await fetch(`/api/auth/browser/check/${_browserSessionId}`);
      const d = await r.json();
      if (d.done) {
        const label = _STORE_LABEL[d.store] || d.store || "Store";
        await _closeBrowserModal();
        toast(label + " connected!", "ok");
        if (typeof refreshStoreRows === "function") refreshStoreRows();
        return;
      }
      const statusEl = document.getElementById("browser-auth-status");
      if (statusEl) {
        if (d.message) statusEl.textContent = d.message;
        else if (d.error) statusEl.textContent = "⚠ " + d.error;
      }
    } catch (_) { /* transient — keep polling */ }
  }

  function _showKeyboardHint() {
    const hint = document.getElementById("browser-kbd-hint");
    if (!hint) return;
    hint.style.opacity = "1";
    hint.style.transform = "translateX(-50%) translateY(0)";
    setTimeout(() => {
      hint.style.opacity = "0";
      hint.style.transform = "translateX(-50%) translateY(4px)";
    }, 3500);
  }

  async function _closeBrowserModal() {
    _browserStartToken++;          // abort any in-flight start (cancel during launch)
    _screenshotLoopActive = false;
    if (_browserWs) { try { _browserWs.close(); } catch (_) {} _browserWs = null; }
    clearInterval(_checkTimer); clearTimeout(_typeFlushTimer);
    _checkTimer = null; _typeFlushTimer = null; _typeBuffer = "";
    _browserSessionId = null;
    document.removeEventListener("keydown", _browserKeyHandler);
    const loading = document.getElementById("browser-loading");
    if (loading) loading.classList.remove("show");
    const modal = document.getElementById("connect-modal");
    if (modal) modal.classList.remove("open");
    const img = document.getElementById("browser-screenshot");
    if (img) { if (img.src && img.src.startsWith("blob:")) URL.revokeObjectURL(img.src); img.src = ""; }
  }

  async function _closeBrowserSession(callBackend) {
    const sid = _browserSessionId;
    if (callBackend && sid) {
      fetch(`/api/auth/browser/session/${sid}`, { method: "DELETE" }).catch(() => {});
    }
    await _closeBrowserModal();
  }

  // PUBLIC: called by Connect/Reconnect buttons across the app.
  window.connectStore = async function (store, btn) {
    const label = _STORE_LABEL[store] || store;
    if (_browserSessionId) await _closeBrowserSession(false);

    // Open the modal IMMEDIATELY with a loading state — launching Chromium +
    // loading the store page takes a few seconds; showing the spinner at once
    // stops the user re-clicking (which used to spawn duplicate sessions).
    const token = ++_browserStartToken;
    const setText = (id, t) => { const e = document.getElementById(id); if (e) e.textContent = t; };
    setText("connect-modal-title", "Connect " + label);
    setText("browser-auth-status", "Starting…");
    setText("browser-loading-text", "Starting " + label + "…");
    const img0 = document.getElementById("browser-screenshot"); if (img0) img0.src = "";
    document.getElementById("browser-loading").classList.add("show");
    document.getElementById("connect-modal").classList.add("open");

    // Forward the user's real GPS so the store's location prompt resolves.
    let geolocation = null;
    if (navigator.geolocation) {
      geolocation = await new Promise((resolve) => {
        navigator.geolocation.getCurrentPosition(
          (p) => resolve({ latitude: p.coords.latitude, longitude: p.coords.longitude }),
          () => resolve(null),
          { timeout: 4000, maximumAge: 60000 }
        );
      });
    }
    if (token !== _browserStartToken) return;   // cancelled while waiting on GPS

    try {
      const r = await fetch(`/api/auth/browser/start/${store}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: window._SERVER_USER_ID, geolocation }),
      });
      const d = await r.json();
      if (token !== _browserStartToken) return;   // cancelled during launch
      if (!d.success) { await _closeBrowserModal(); toast("Connect error: " + (d.error || "unknown"), "err"); return; }
      _browserSessionId = d.session_id;
    } catch (e) {
      if (token !== _browserStartToken) return;
      await _closeBrowserModal();
      toast("Failed to start browser: " + e.message, "err");
      return;
    }

    document.getElementById("browser-loading").classList.remove("show");
    setText("browser-auth-status", "Waiting for login…");
    const img = document.getElementById("browser-screenshot"); if (img) img.focus();
    document.addEventListener("keydown", _browserKeyHandler);
    _startScreenshotStream();
    _checkTimer = setInterval(_checkBrowserAuth, 2000);
    _showKeyboardHint();
  };
  // Back-compat alias (some pages/health notes call openBrowserAuth).
  window.openBrowserAuth = window.connectStore;
  window.closeConnectModal = function () { _closeBrowserSession(true); };

  /* ---- OCR upload ------------------------------------------------------ */
  // Page calls:  document.getElementById('ocr-input').addEventListener('change', s2o.handleOcr)
  // Textarea ID: 'items'   Status el ID: 'ocr-status'   Cancel btn ID: 'ocr-cancel'
  let _ocrAbort = null;

  window.s2o_handleOcr = async function (event) {
    const file = event.target.files[0];
    const statusEl  = document.getElementById("ocr-status");
    const statusText = document.getElementById("ocr-status-text");
    const cancelBtn = document.getElementById("ocr-cancel");
    // Fallback: if the page still uses the old single-element pattern, set textContent directly.
    const status = statusText || statusEl;
    if (!file) return;
    if (status) status.textContent = "Scanning…";
    if (statusEl) statusEl.classList.add("ocr-scanning");
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
      if (statusEl) statusEl.classList.remove("ocr-scanning");
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

  /* ---- toasts ---------------------------------------------------------- */
  // Generic transient notification. type: "" | "ok" | "err" | "greet".
  // Declared (not assigned) so it's hoisted and callable from the relay code above.
  function toast(message, type) {
    const host = document.getElementById("toast-host");
    if (!host) return;
    const el = document.createElement("div");
    el.className = "s2o-toast" + (type && type !== "greet" ? " " + type : "");
    el.textContent = message;
    host.appendChild(el);
    requestAnimationFrame(() => el.classList.add("show"));
    const ttl = type === "greet" ? 3600 : 2800;
    setTimeout(() => {
      el.classList.remove("show");
      setTimeout(() => el.remove(), 320);
    }, ttl);
  }

  /* ---- greeting toast (Shop / Compare) --------------------------------- */
  // Friendly "Hi, <name>" — shown ONLY on the first tab load of a session
  // (sessionStorage flag) so it doesn't pop on every navigation.
  window.s2o_greet = function () {
    try {
      if (sessionStorage.getItem("s2o-greeted")) return;
      sessionStorage.setItem("s2o-greeted", "1");
    } catch (_) {}
    const su = window._SERVER_USER;
    const name = (su && su.name) ? su.name : (isGuest() ? "there" : "");
    const hellos = ["Hi", "Hello", "Hey", "Welcome back"];
    // Vary the greeting without Math.random (unavailable in some sandboxes):
    const pick = hellos[(new Date().getMinutes()) % hellos.length];
    toast(name ? `${pick}, ${name} 👋` : `${pick} 👋`, "greet");
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
    paintIcons, applyTheme, saveTheme, THEMES, toast,
    attachVoice, voiceSupported, isGuest,
    shopCartGet, shopCartSet, shopCartAdd, shopCartCount, updateCartBadge,
    shopCartGetQty, shopCartItems, shopCartSetQty,
  };
})();
