"""
Đồng bộ realtime DB -> Google Sheets.

Luồng:
  1. Đọc tin nhắn MỚI từ SQLite (do listener ghi) cho các thread được chỉ định.
  2. Phân loại đa nhãn theo keyword (classify.Classifier).
  3. Đẩy lên một Google Spreadsheet: MỖI thread = 1 sheet con, tên sheet lấy
     theo tên nhóm/tên người đã map (threads.name), fallback về thread_id.

Chỉ ĐỌC DB (mở read-only) nên chạy song song listener an toàn. Vị trí đã xử lý
được nhớ bằng rowid trong file state -> khởi động lại không ghi trùng.

Chạy:
  zalo_listener/.venv/bin/python -m zalo_listener.sheets_sync -c zalo_listener/config.yaml
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    sys.exit(
        "Thiếu thư viện Google. Cài:\n"
        "  zalo_listener/.venv/bin/pip install gspread google-auth"
    )

from .classify import Classifier

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
# Ký tự Google Sheets không cho trong tên sheet.
_FORBIDDEN_TITLE = re.compile(r"[\[\]\*\?/\\:]")

DEFAULT_COLUMNS = ["ts_local", "direction", "sender", "category", "text", "msg_id"]
COLUMN_HEADERS = {
    "ts_local": "Thời gian",
    "direction": "Chiều",
    "sender": "Người gửi",
    "category": "Phân loại",
    "text": "Nội dung",
    "msg_id": "Mã tin",
    "thread_id": "Thread ID",
    "thread_type": "Loại thread",
    "msg_type": "Kiểu tin",
    "ts_ms": "ts_ms",
}
DIRECTION_VN = {"incoming": "đến", "outgoing": "đi", "unknown": ""}


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"Không thấy config: {path}")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _safe_title(name: str) -> str:
    t = _FORBIDDEN_TITLE.sub(" ", name)
    t = " ".join(t.split()).strip()
    return t[:95] or "thread"


class SheetSync:
    def __init__(self, cfg: dict):
        s = cfg.get("sheets", {})
        if not s.get("enabled", True):
            sys.exit("[sheets] sheets.enabled = false — bật lên trong config để chạy.")

        self.sqlite_path = cfg["storage"]["sqlite_path"]
        self.poll = int(s.get("poll_seconds", 5))
        self.limit = int(s.get("batch_limit", 500))
        self.columns = s.get("columns") or DEFAULT_COLUMNS
        self.header = [COLUMN_HEADERS.get(c, c) for c in self.columns]
        self.overrides = {str(k): str(v) for k, v in (s.get("thread_names") or {}).items()}
        self.tracked = [str(x) for x in (s.get("track_thread_ids") or [])]
        if not self.tracked:
            sys.exit(
                "[sheets] Chưa khai báo sheets.track_thread_ids — liệt kê các "
                "thread_id cần đẩy lên sheet."
            )

        self.classifier = Classifier(cfg.get("classify", {}))

        # State (watermark rowid + map thread_id -> tên sheet hiện tại)
        state_path = s.get("state_path") or (
            str(Path(self.sqlite_path).with_name("sheets_sync.state.json"))
        )
        self.state_path = Path(state_path)
        self.state = self._load_state()

        # DB read-only
        self._db = self._open_db_ro()
        if self.state.get("last_rowid") is None:
            # Lần đầu: mặc định chỉ đồng bộ tin MỚI từ giờ, trừ khi backfill=true.
            if s.get("backfill", False):
                self.state["last_rowid"] = 0
            else:
                row = self._db.execute("SELECT MAX(rowid) FROM messages").fetchone()
                self.state["last_rowid"] = row[0] or 0
            self._save_state()

        # Google Sheets
        cred_path = s.get("credentials_json")
        if not cred_path or not Path(cred_path).exists():
            sys.exit(f"[sheets] Không thấy credentials_json: {cred_path}")
        creds = Credentials.from_service_account_file(cred_path, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.sh = self._open_spreadsheet(s)
        print(f"[sheets] mở spreadsheet: {self.sh.title}")

        self._ws_cache: dict[str, "gspread.Worksheet"] = {}
        self._running = True

    # ── state ────────────────────────────────────────────────────────────────
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"last_rowid": None, "titles": {}}

    def _save_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    @property
    def _titles(self) -> dict:
        return self.state.setdefault("titles", {})

    # ── db ─────────────────────────────────────────────────────────────────────
    def _open_db_ro(self) -> sqlite3.Connection:
        uri = f"file:{Path(self.sqlite_path).as_posix()}?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _thread_names(self) -> dict:
        rows = self._db.execute(
            "SELECT thread_id, name FROM threads WHERE thread_id IN (%s)"
            % ",".join("?" * len(self.tracked)),
            self.tracked,
        ).fetchall()
        return {r["thread_id"]: r["name"] for r in rows}

    def _fetch_new(self):
        placeholders = ",".join("?" * len(self.tracked))
        return self._db.execute(
            f"""
            SELECT m.rowid AS rowid, m.msg_id, m.thread_id, m.thread_type,
                   COALESCE(u.display_name, m.uid_from) AS sender,
                   m.ts_local, m.ts_ms, m.msg_type, m.text, m.direction
            FROM messages m
            LEFT JOIN users u ON m.uid_from = u.uid
            WHERE m.rowid > ? AND m.thread_id IN ({placeholders})
            ORDER BY m.rowid ASC
            LIMIT ?
            """,
            [self.state["last_rowid"], *self.tracked, self.limit],
        ).fetchall()

    # ── spreadsheet / worksheet ───────────────────────────────────────────────
    def _open_spreadsheet(self, s: dict):
        if s.get("spreadsheet_id"):
            return self.gc.open_by_key(s["spreadsheet_id"])
        if s.get("spreadsheet_url"):
            return self.gc.open_by_url(s["spreadsheet_url"])
        sys.exit("[sheets] Cần sheets.spreadsheet_id hoặc sheets.spreadsheet_url.")

    def _desired_title(self, thread_id: str, names: dict) -> str:
        base = (
            self.overrides.get(thread_id)
            or names.get(thread_id)
            or f"thread-{thread_id[:8]}"
        )
        title = _safe_title(base)
        # tránh trùng tên với thread khác
        used_by_other = {
            t for tid, t in self._titles.items() if tid != thread_id
        }
        if title in used_by_other:
            title = _safe_title(f"{base} ({thread_id[:4]})")
        return title

    def _worksheet_for(self, thread_id: str, names: dict):
        desired = self._desired_title(thread_id, names)
        ws = self._ws_cache.get(thread_id)
        if ws is not None:
            if ws.title != desired:                 # tên nhóm vừa đổi -> rename
                try:
                    ws.update_title(desired)
                    self._titles[thread_id] = desired
                    self._save_state()
                except Exception as e:
                    print(f"[sheets] không rename được sheet {ws.title}: {e}")
            return ws

        existing = {w.title: w for w in self.sh.worksheets()}
        prev = self._titles.get(thread_id)
        if prev and prev in existing:
            ws = existing[prev]
            if ws.title != desired:
                try:
                    ws.update_title(desired)
                except Exception:
                    desired = ws.title
        elif desired in existing:
            ws = existing[desired]
        else:
            ws = self.sh.add_worksheet(
                title=desired, rows=1000, cols=max(len(self.columns), 8)
            )
            ws.append_row(self.header, value_input_option="RAW")
            print(f"[sheets] tạo sheet mới: {desired}")

        self._ws_cache[thread_id] = ws
        self._titles[thread_id] = ws.title
        self._save_state()
        return ws

    # ── build row ──────────────────────────────────────────────────────────────
    def _row_values(self, r: sqlite3.Row) -> list:
        text = r["text"] or ""
        out = []
        for c in self.columns:
            if c == "direction":
                out.append(DIRECTION_VN.get(r["direction"] or "unknown", ""))
            elif c == "category":
                out.append(self.classifier.label_str(text))
            elif c == "sender":
                out.append(r["sender"] or "")
            else:
                try:
                    out.append(r[c] if r[c] is not None else "")
                except (IndexError, KeyError):
                    out.append("")
        return out

    @staticmethod
    def _looks_deleted(err: Exception) -> bool:
        """Nhận diện lỗi do sheet đã bị xoá (handle cache treo)."""
        if isinstance(err, gspread.exceptions.WorksheetNotFound):
            return True
        msg = str(err).lower()
        return any(s in msg for s in (
            "unable to parse range",   # range 'Tên sheet'!A1 không còn
            "no grid with id",
            "does not exist",
            "not found",
        ))

    def _invalidate(self, thread_id: str):
        """Quên handle + tên sheet của thread -> lần tới sẽ dò lại / tạo mới."""
        self._ws_cache.pop(thread_id, None)
        self._titles.pop(thread_id, None)
        self._save_state()

    def _push_group(self, thread_id: str, grp: list, names: dict) -> bool:
        """Đẩy một nhóm dòng vào sheet của thread. Tự tạo lại nếu sheet bị xoá."""
        values = [self._row_values(r) for r in grp]
        recreated = False
        for attempt in range(3):
            try:
                ws = self._worksheet_for(thread_id, names)
                ws.append_rows(values, value_input_option="USER_ENTERED")
                return True
            except Exception as e:
                if self._looks_deleted(e) and not recreated:
                    print(f"[sheets] sheet của thread {thread_id} không còn — tạo lại.")
                    self._invalidate(thread_id)
                    recreated = True
                    continue                       # tạo lại ngay, không chờ
                wait = 2 * (attempt + 1)
                print(f"[sheets] lỗi đẩy thread {thread_id}, thử lại sau {wait}s: {e}")
                time.sleep(wait)
        return False

    # ── một vòng đồng bộ ───────────────────────────────────────────────────────
    def sync_once(self) -> int:
        rows = self._fetch_new()
        if not rows:
            return 0
        names = self._thread_names()

        # gom theo thread, giữ thứ tự rowid
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r["thread_id"], []).append(r)

        min_failed_rowid: Optional[int] = None
        pushed = 0
        for thread_id, grp in groups.items():
            if self._push_group(thread_id, grp, names):
                pushed += len(grp)
            else:
                min_failed_rowid = min(min_failed_rowid or grp[0]["rowid"], grp[0]["rowid"])

        max_rowid = rows[-1]["rowid"]
        if min_failed_rowid is None:
            new_wm = max_rowid
        else:
            # Giữ lại từ rowid lỗi trở đi để thử lại vòng sau. Lưu ý: tin của
            # thread khác có rowid lớn hơn mốc này (đã đẩy thành công) có thể bị
            # đẩy lại -> hiếm, chỉ khi một sheet lỗi kéo dài; dedupe được qua msg_id.
            new_wm = max(self.state["last_rowid"], min_failed_rowid - 1)
        self.state["last_rowid"] = new_wm
        self._save_state()
        return pushed

    def run(self):
        print(
            f"[sheets] theo dõi {len(self.tracked)} thread, poll {self.poll}s, "
            f"bắt đầu từ rowid > {self.state['last_rowid']}. Ctrl+C để dừng."
        )
        while self._running:
            try:
                n = self.sync_once()
                if n:
                    print(f"[sheets] +{n} dòng lên sheet "
                          f"(rowid tới {self.state['last_rowid']})")
            except sqlite3.OperationalError as e:
                print(f"[sheets] DB bận, bỏ qua vòng này: {e}")
            except Exception as e:
                print(f"[sheets] lỗi vòng đồng bộ: {e}")
            for _ in range(self.poll):
                if not self._running:
                    break
                time.sleep(1)

    def stop(self):
        self._running = False


def main():
    ap = argparse.ArgumentParser(description="Đồng bộ DB Zalo -> Google Sheets")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="chạy một vòng rồi thoát")
    args = ap.parse_args()

    cfg = load_config(args.config)
    sync = SheetSync(cfg)

    def _sig(*_):
        sync.stop()
    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except ValueError:
        pass

    if args.once:
        n = sync.sync_once()
        print(f"[sheets] xong một vòng: +{n} dòng.")
    else:
        sync.run()
    print("[sheets] dừng.")


if __name__ == "__main__":
    main()
