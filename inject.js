// ─────────────────────────────────────────────────────────────────────────
// inject.js  — chạy TRONG page context của tab Zalo (add_init_script).
//
// Nhiệm vụ: chỉ QUAN SÁT. Không gửi đi tin nhắn, không gọi API ghi.
//   - Wrap fetch / XMLHttpRequest / WebSocket để bắt mọi traffic.
//   - Đẩy sự kiện ra Python qua binding window.__ZL_SINK (expose_binding).
//   - Cố lấy secret key (zpw_enk) từ localStorage để Python giải mã.
//
// __ZL_CFG được Python chèn vào phía trên khi add_init_script.
// ─────────────────────────────────────────────────────────────────────────
(() => {
  if (window.__ZL_HOOKED) return;      // tránh hook 2 lần khi HMR / reload
  window.__ZL_HOOKED = true;

  const CFG = window.__ZL_CFG || {};
  const URL_FILTER = (CFG.url_filter || "zalo").toLowerCase();
  const CAP_HTTP = CFG.http !== false;
  const CAP_WS = CFG.websocket !== false;
  const CAP_DOM = CFG.dom === true;
  const DOM_SELECTOR = CFG.dom_selector || '[class*="message"]';

  // Hàng đợi phòng khi binding chưa sẵn sàng lúc trang vừa load.
  const queue = [];
  let sinkReady = typeof window.__ZL_SINK === "function";

  function emit(kind, payload) {
    const evt = { kind, ts: Date.now(), ...payload };
    if (sinkReady) {
      try { window.__ZL_SINK(JSON.stringify(evt)); return; } catch (e) { /* fall through */ }
    }
    queue.push(evt);
    if (queue.length > 5000) queue.splice(0, queue.length - 5000);
  }

  // Poll cho tới khi Python expose binding xong, rồi flush hàng đợi.
  const readyTimer = setInterval(() => {
    if (typeof window.__ZL_SINK === "function") {
      sinkReady = true;
      clearInterval(readyTimer);
      while (queue.length) {
        try { window.__ZL_SINK(JSON.stringify(queue.shift())); }
        catch (e) { break; }
      }
    }
  }, 300);

  const wanted = (url) =>
    typeof url === "string" && url.toLowerCase().includes(URL_FILTER);

  // ── Cố lấy secret key mỗi vài giây (key có sau khi login xong) ──────────
  function scanSecretKey() {
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (!k) continue;
        const kl = k.toLowerCase();
        if (kl.includes("enk") || kl.includes("secret") || kl.includes("zpw")) {
          const v = localStorage.getItem(k);
          if (v && v.length >= 16 && v.length <= 128) {
            emit("secret_hint", { source: "localStorage", key: k, value: v });
          }
        }
      }
    } catch (e) { /* localStorage có thể bị chặn */ }
  }
  scanSecretKey();
  const keyTimer = setInterval(scanSecretKey, 5000);
  setTimeout(() => clearInterval(keyTimer), 60000);

  // ── Hook fetch ─────────────────────────────────────────────────────────
  if (CAP_HTTP && window.fetch) {
    const _fetch = window.fetch;
    window.fetch = function (input, init) {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      return _fetch.apply(this, arguments).then((res) => {
        if (wanted(url)) {
          try {
            res.clone().text().then((body) => {
              emit("http", {
                url,
                method: (init && init.method) || "GET",
                status: res.status,
                body: body.length > 2_000_000 ? body.slice(0, 2_000_000) : body,
              });
            }).catch(() => {});
          } catch (e) { /* ignore */ }
        }
        return res;
      });
    };
  }

  // ── Hook XMLHttpRequest ─────────────────────────────────────────────────
  if (CAP_HTTP && window.XMLHttpRequest) {
    const XHR = window.XMLHttpRequest;
    const _open = XHR.prototype.open;
    const _send = XHR.prototype.send;
    XHR.prototype.open = function (method, url) {
      this.__zl_url = url;
      this.__zl_method = method;
      return _open.apply(this, arguments);
    };
    XHR.prototype.send = function () {
      const xhr = this;
      this.addEventListener("load", function () {
        try {
          if (wanted(xhr.__zl_url)) {
            let body = "";
            const rt = xhr.responseType;
            if (rt === "" || rt === "text") body = xhr.responseText;
            else if (rt === "json") body = JSON.stringify(xhr.response);
            emit("http", {
              url: xhr.__zl_url,
              method: xhr.__zl_method || "GET",
              status: xhr.status,
              body: body.length > 2_000_000 ? body.slice(0, 2_000_000) : body,
            });
          }
        } catch (e) { /* ignore */ }
      });
      return _send.apply(this, arguments);
    };
  }

  // ── Hook WebSocket (kênh chính của tin nhắn realtime) ───────────────────
  if (CAP_WS && window.WebSocket) {
    const _WS = window.WebSocket;
    const Wrapped = function (url, protocols) {
      const ws = protocols !== undefined ? new _WS(url, protocols) : new _WS(url);
      try { emit("ws_open", { url: String(url) }); } catch (e) {}

      ws.addEventListener("message", (ev) => {
        try {
          const d = ev.data;
          if (typeof d === "string") {
            emit("ws", { url: String(url), text: d });
          } else if (d instanceof ArrayBuffer) {
            emit("ws", { url: String(url), b64: abToB64(d), binary: true });
          } else if (d instanceof Blob) {
            d.arrayBuffer().then((buf) => {
              emit("ws", { url: String(url), b64: abToB64(buf), binary: true });
            }).catch(() => {});
          }
        } catch (e) { /* ignore */ }
      });

      // Bắt cả chiều client -> server. Tin tự gửi thường vẫn được server echo
      // về chiều incoming, và msg_id trong SQLite sẽ khử trùng hai bản.
      const _send = ws.send;
      ws.send = function (data) {
        try {
          if (typeof data === "string") {
            emit("ws_send", { url: String(url), text: data });
          } else if (data instanceof ArrayBuffer) {
            emit("ws_send", { url: String(url), b64: abToB64(data), binary: true });
          } else if (ArrayBuffer.isView(data)) {
            const buf = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
            emit("ws_send", { url: String(url), b64: abToB64(buf), binary: true });
          } else if (data instanceof Blob) {
            data.arrayBuffer().then((buf) => {
              emit("ws_send", { url: String(url), b64: abToB64(buf), binary: true });
            }).catch(() => {});
          }
        } catch (e) { /* ignore */ }
        return _send.apply(this, arguments);
      };
      return ws;
    };
    Wrapped.prototype = _WS.prototype;
    Wrapped.CONNECTING = _WS.CONNECTING;
    Wrapped.OPEN = _WS.OPEN;
    Wrapped.CLOSING = _WS.CLOSING;
    Wrapped.CLOSED = _WS.CLOSED;
    window.WebSocket = Wrapped;
  }

  function abToB64(buf) {
    let binary = "";
    const bytes = new Uint8Array(buf);
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(binary);
  }

  // ── DOM fallback (tuỳ chọn) — chỉ bắt hội thoại đang mở ─────────────────
  if (CAP_DOM) {
    const seen = new WeakSet();
    const obs = new MutationObserver((muts) => {
      for (const m of muts) {
        for (const node of m.addedNodes) {
          if (node.nodeType !== 1) continue;
          const els = node.matches && node.matches(DOM_SELECTOR)
            ? [node]
            : (node.querySelectorAll ? node.querySelectorAll(DOM_SELECTOR) : []);
          els.forEach((el) => {
            if (seen.has(el)) return;
            seen.add(el);
            const text = (el.innerText || "").trim();
            if (text) emit("dom", { text, html: el.outerHTML.slice(0, 8000) });
          });
        }
      }
    });
    const start = () => {
      if (document.body) obs.observe(document.body, { childList: true, subtree: true });
      else setTimeout(start, 500);
    };
    start();
  }

  emit("hook_ready", { url: location.href });
})();
