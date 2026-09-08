"""
Runner chính — read-only Zalo Web listener.

Luồng:
  1. connect_over_cdp tới Brave đã đăng nhập (KHÔNG login programmatic).
  2. add_init_script(inject.js) + reload tab -> hook fetch/XHR/WebSocket.
  3. expose_binding __ZL_SINK: mọi sự kiện từ page chảy về đây.
  4. Giải mã -> extract message -> áp privacy -> ghi SQLite.

Chế độ:
  (mặc định)     lắng nghe & ghi DB.
  --discover     ngoài ghi DB, dump toàn bộ sự kiện thô ra discover/*.jsonl
                 để bạn dò schema / tìm secret key.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

import yaml
from playwright.async_api import async_playwright

from . import crypto
from .extract import extract_messages, extract_name_hints
from .privacy import PrivacyFilter
from .store import Store

ROOT = Path(__file__).parent.parent
INJECT_JS = (Path(__file__).parent / "inject.js").read_text(encoding="utf-8")


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"Không thấy config: {path} (copy config.example.yaml -> config.yaml)")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


class InstanceLock:
    """Ngăn nhiều listener cùng hook một tab và ghi chung một database."""

    def __init__(self, sqlite_path: str):
        lock_path = Path(sqlite_path).with_suffix(Path(sqlite_path).suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fp.close()
            sys.exit(
                f"[lock] Đã có listener khác dùng database {sqlite_path}. "
                "Hãy dừng tiến trình cũ trước."
            )
        self.fp.seek(0)
        self.fp.truncate()
        self.fp.write(str(os.getpid()))
        self.fp.flush()

    def close(self):
        if self.fp.closed:
            return
        fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
        self.fp.close()


class Listener:
    def __init__(self, cfg: dict, discover: bool):
        self.cfg = cfg
        self.discover = discover
        self.privacy = PrivacyFilter(cfg.get("privacy", {}))
        self._lock = InstanceLock(cfg["storage"]["sqlite_path"])
        self.store = Store(
            cfg["storage"]["sqlite_path"],
            self.privacy,
            store_raw=cfg.get("capture", {}).get("store_raw_events", True),
        )
        self.key = crypto.normalize_key(cfg.get("crypto", {}).get("secret_key_b64", ""))
        self._key_source = "config" if self.key else None
        self.ws_key = self.key
        self._ws_key_source = "config" if self.ws_key else None
        self.filter = cfg.get("filter", {})
        self.counters = {"events": 0, "http": 0, "ws": 0, "dom": 0,
                         "ws_send": 0, "http_parsed": 0, "ws_decoded": 0,
                         "decode_failed": 0,
                         "new_msgs": 0, "dup_msgs": 0, "names": 0}
        self._running = True
        self._disc_fp = None
        if discover:
            d = ROOT / "discover"
            d.mkdir(exist_ok=True)
            self._disc_fp = open(d / f"events_{int(time.time())}.jsonl", "a", encoding="utf-8")

    # ── xử lý một sự kiện từ page ────────────────────────────────────────────
    def on_event(self, raw_json: str):
        try:
            evt = json.loads(raw_json)
        except json.JSONDecodeError:
            return
        self.counters["events"] += 1
        kind = evt.get("kind")

        if self._disc_fp:
            self._disc_fp.write(raw_json + "\n")

        if kind == "secret_hint":
            self._maybe_set_key(evt.get("value"), f"localStorage:{evt.get('key')}")
            return
        if kind == "hook_ready":
            print(f"[hook] sẵn sàng trên {evt.get('url')}")
            return
        if kind == "ws_open":
            return

        if kind == "http":
            self.counters["http"] += 1
            self._handle_payload(evt.get("body"), evt.get("url", ""), "http")
        elif kind == "ws":
            self.counters["ws"] += 1
            body = evt.get("text")
            if body is None and evt.get("b64"):
                # WS nhị phân: thử coi b64 là ciphertext luôn
                body = evt["b64"]
            self._handle_ws_payload(body, evt.get("url", ""), "ws")
        elif kind == "ws_send":
            self.counters["ws_send"] += 1
            body = evt.get("text")
            if body is None and evt.get("b64"):
                body = evt["b64"]
            self._handle_ws_payload(body, evt.get("url", ""), "ws_send")
        elif kind == "dom":
            self.counters["dom"] += 1
            # DOM chỉ có text -> lưu thô để tham khảo, không phải nguồn chính
            self.store.add_raw("dom", evt.get("url", ""), raw_json)

    def _maybe_set_key(self, candidate: str, source: str):
        if self.key or not candidate:
            return
        k = crypto.normalize_key(candidate)
        if k:
            self.key = k
            self._key_source = source
            print(f"[key] tìm thấy secret key từ {source}")

    def _handle_payload(self, body, url: str, kind: str):
        if not body:
            return

        # cơ hội moi key từ getlogininfo nếu chưa có
        if not self.key and ("logininfo" in url.lower() or "serverinfo" in url.lower()):
            hint = crypto.hunt_key_from_body(body if isinstance(body, str) else "")
            if hint:
                self._maybe_set_key(hint, f"response:{url[:60]}")

        if self.cfg.get("capture", {}).get("store_raw_events", True):
            self.store.add_raw(kind, url, body if isinstance(body, str) else json.dumps(body))

        parsed = crypto.try_decrypt_payload(body if isinstance(body, str) else json.dumps(body), self.key)
        if parsed is None:
            return
        self.counters["http_parsed"] += 1

        data = parsed.get("data", parsed) if isinstance(parsed, dict) else parsed
        self._store_messages(data)

    def _handle_ws_payload(self, body, url: str, kind: str):
        if not body:
            return

        frame = crypto.parse_ws_frame(body)
        if frame is None:
            # Một số phiên bản có thể gửi text frame JSON không có header.
            self._handle_payload(body, url, kind)
            return

        if self.cfg.get("capture", {}).get("store_raw_events", True):
            self.store.add_raw(kind, url, body)

        # Server gửi key phiên trong frame 1/1/1. Đây là key AES-GCM cho socket,
        # không phải các key cl-enk/cl-fenk trong localStorage.
        if (frame.version, frame.cmd, frame.sub_cmd) == (1, 1, 1):
            candidate = frame.payload.get("key")
            key = crypto.normalize_key(candidate) if isinstance(candidate, str) else None
            if key:
                self.ws_key = key
                self._ws_key_source = "websocket-handshake"
                print("[key] nhận cipher key cho WebSocket handshake")
            return

        decoded = crypto.decode_ws_event(frame.payload, self.ws_key)
        if decoded is None:
            # Ping/ack có thể không mang field data nên không tính là lỗi decode.
            if isinstance(frame.payload.get("data"), str):
                self.counters["decode_failed"] += 1
            return

        self.counters["ws_decoded"] += 1
        data = decoded.get("data", decoded) if isinstance(decoded, dict) else decoded
        if frame.cmd in (511, 521):
            thread_type = "group"
        elif frame.cmd in (501, 510):
            thread_type = "user"
        else:
            thread_type = "unknown"
        self._store_messages(data, default_thread_type=thread_type)

    def _store_messages(self, data, default_thread_type: str = "unknown"):
        for m in extract_messages(data, default_thread_type=default_thread_type):
            if not self._passes_filter(m):
                continue
            if self.store.upsert_message(m):
                self.counters["new_msgs"] += 1
            else:
                self.counters["dup_msgs"] += 1
        # Bắt tên nhóm / tên người 1-1 để đặt tên sheet ở module đồng bộ.
        for h in extract_name_hints(data):
            changed = (
                self.store.note_group_name(h.raw_id, h.name)
                if h.kind == "group"
                else self.store.note_user_name(h.raw_id, h.name)
            )
            if changed:
                self.counters["names"] += 1

    def _passes_filter(self, m) -> bool:
        f = self.filter
        if f.get("group_only") and m.thread_type != "group":
            return False
        only = f.get("only_thread_ids") or []
        if only and m.thread_id not in {str(x) for x in only}:
            return False
        skip = f.get("skip_thread_ids") or []
        if m.thread_id in {str(x) for x in skip}:
            return False
        return True

    # ── vòng chạy ────────────────────────────────────────────────────────────
    async def run(self):
        cdp = self.cfg["cdp"]
        async with async_playwright() as pw:
            print(f"[cdp] nối tới {cdp['endpoint']} ...")
            try:
                browser = await pw.chromium.connect_over_cdp(cdp["endpoint"])
            except Exception as e:
                sys.exit(
                    f"[cdp] Không nối được: {e}\n"
                    "→ Đã chạy `python launch_brave.py` và đăng nhập Zalo chưa?"
                )

            context = browser.contexts[0] if browser.contexts else await browser.new_context()

            # binding nhận sự kiện từ page
            await context.expose_binding(
                "__ZL_SINK",
                lambda source, payload: self.on_event(payload),
            )

            # chèn cấu hình + hook cho MỌI trang tương lai
            cap = self.cfg.get("capture", {})
            init = (
                "window.__ZL_CFG = " + json.dumps({
                    "url_filter": cap.get("url_filter", "zalo"),
                    "http": cap.get("http", True),
                    "websocket": cap.get("websocket", True),
                    "dom": cap.get("dom", False),
                    "dom_selector": cap.get("dom_selector", '[class*="message"]'),
                }) + ";\n" + INJECT_JS
            )
            await context.add_init_script(init)

            # tìm tab Zalo
            page = None
            for p in context.pages:
                if cdp.get("target_url_contains", "chat.zalo.me") in p.url:
                    page = p
                    break
            if page is None:
                if context.pages:
                    page = context.pages[0]
                    await page.goto("https://chat.zalo.me/")
                else:
                    page = await context.new_page()
                    await page.goto("https://chat.zalo.me/")

            # init script chỉ áp khi trang load -> reload để hook có hiệu lực
            if cdp.get("reload_on_attach", True):
                print("[cdp] reload tab Zalo để kích hoạt hook (giữ nguyên đăng nhập)...")
                await page.reload(wait_until="domcontentloaded")

            print("[run] Đang lắng nghe. Mở/đọc các nhóm để tin nhắn mới chảy về.")
            print("[run] Ctrl+C để dừng.\n")

            # in thống kê định kỳ
            interval = self.cfg.get("runtime", {}).get("stats_every_seconds", 60)
            while self._running:
                await asyncio.sleep(interval)
                self.store.commit()
                s = self.store.stats()
                c = self.counters
                http_key = self._key_source or "chưa có"
                ws_key = self._ws_key_source or "chưa có"
                print(
                    f"[stats] db: {s['messages']} msg / {s['threads']} thread "
                    f"| phiên: +{c['new_msgs']} mới, {c['dup_msgs']} trùng, "
                    f"{c['names']} tên nhóm/người, "
                    f"{c['ws_decoded']} WS giải mã, {c['decode_failed']} lỗi giải mã, "
                    f"{c['http_parsed']} HTTP JSON, "
                    f"{c['events']} sự kiện "
                    f"(http {c['http']}, ws-in {c['ws']}, ws-out {c['ws_send']}) "
                    f"| key: http={http_key}, ws={ws_key}"
                )

    def stop(self):
        self._running = False

    def close(self):
        if self._disc_fp:
            self._disc_fp.close()
        s = self.store.stats()
        self.store.close()
        self._lock.close()
        print(f"\n[done] Tổng trong DB: {s['messages']} tin nhắn, "
              f"{s['threads']} nhóm/hội thoại, {s['users']} người.")


def main():
    ap = argparse.ArgumentParser(description="Zalo Web read-only listener → SQLite")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--discover", action="store_true",
                    help="dump sự kiện thô ra discover/*.jsonl để dò schema/key")
    args = ap.parse_args()

    cfg = load_config(args.config)
    listener = Listener(cfg, discover=args.discover)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _sig(*_):
        listener.stop()
    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except ValueError:
        pass  # không phải main thread

    try:
        loop.run_until_complete(listener.run())
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()


if __name__ == "__main__":
    main()
