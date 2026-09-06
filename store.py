"""
Lưu trữ SQLite. Upsert theo msg_id (scroll/realtime hay trùng lặp).
Giữ raw_json để reparse khi Zalo đổi schema.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .extract import Message
from .privacy import PrivacyFilter

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id     TEXT PRIMARY KEY,
    thread_type   TEXT,
    name          TEXT,
    first_seen_ms INTEGER,
    last_seen_ms  INTEGER,
    msg_count     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    uid           TEXT PRIMARY KEY,   -- đã giả danh nếu bật pseudonymize
    display_name  TEXT,               -- NULL nếu không lưu tên
    first_seen_ms INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    msg_id        TEXT PRIMARY KEY,
    thread_id     TEXT,
    thread_type   TEXT,
    uid_from      TEXT,
    ts_ms         INTEGER,
    msg_type      TEXT,
    text          TEXT,
    quote_msg_id  TEXT,
    direction     TEXT,
    raw_json      TEXT,
    ingested_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id, ts_ms);
CREATE INDEX IF NOT EXISTS idx_msg_ts     ON messages(ts_ms);

-- Bắt tất cả sự kiện thô (tuỳ chọn) để dò schema / audit.
CREATE TABLE IF NOT EXISTS raw_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT,
    url           TEXT,
    ts_ms         INTEGER,
    payload       TEXT
);
"""


class Store:
    def __init__(self, path: str, privacy: PrivacyFilter, store_raw: bool = True):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(p))
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()
        self.privacy = privacy
        self.store_raw = store_raw
        self._pending = 0

    def _migrate(self):
        """Thêm cột mới mà không làm mất database đã có."""
        columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(messages)")
        }
        if "direction" not in columns:
            self.db.execute("ALTER TABLE messages ADD COLUMN direction TEXT")

    # ── message ────────────────────────────────────────────────────────────
    def upsert_message(self, m: Message) -> bool:
        """Trả True nếu là message mới (insert), False nếu đã có."""
        now = int(time.time() * 1000)
        uid = self.privacy.uid(m.uid_from)
        text = self.privacy.text(m.text)

        cur = self.db.execute("SELECT 1 FROM messages WHERE msg_id = ?", (m.msg_id,))
        is_new = cur.fetchone() is None

        self.db.execute(
            """
            INSERT INTO messages
                (msg_id, thread_id, thread_type, uid_from, ts_ms, msg_type,
                 text, quote_msg_id, direction, raw_json, ingested_ms)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(msg_id) DO UPDATE SET
                text        = excluded.text,
                msg_type    = excluded.msg_type,
                quote_msg_id= excluded.quote_msg_id,
                direction   = excluded.direction,
                raw_json    = excluded.raw_json
            """,
            (
                m.msg_id, m.thread_id, m.thread_type, uid, m.ts_ms, m.msg_type,
                text, m.quote_msg_id, m.direction,
                json.dumps(m.raw, ensure_ascii=False) if self.store_raw else None,
                now,
            ),
        )

        if uid is not None:
            self.db.execute(
                "INSERT OR IGNORE INTO users (uid, display_name, first_seen_ms) VALUES (?,?,?)",
                (uid, self.privacy.name(m.display_name), now),
            )
            if self.privacy.name(m.display_name):
                self.db.execute(
                    "UPDATE users SET display_name = COALESCE(display_name, ?) WHERE uid = ?",
                    (self.privacy.name(m.display_name), uid),
                )

        if m.thread_id:
            self._touch_thread(m.thread_id, m.thread_type, m.ts_ms or now, is_new)

        self._pending += 1
        if self._pending >= 50:
            self.commit()
        return is_new

    def _touch_thread(self, thread_id: str, ttype: str, ts: int, inc: bool):
        self.db.execute(
            """
            INSERT INTO threads (thread_id, thread_type, first_seen_ms, last_seen_ms, msg_count)
            VALUES (?,?,?,?,?)
            ON CONFLICT(thread_id) DO UPDATE SET
                last_seen_ms = MAX(last_seen_ms, excluded.last_seen_ms),
                thread_type  = CASE WHEN threads.thread_type='unknown'
                                    THEN excluded.thread_type ELSE threads.thread_type END,
                msg_count    = threads.msg_count + ?
            """,
            (thread_id, ttype, ts, ts, 1 if inc else 0, 1 if inc else 0),
        )

    def set_thread_name(self, thread_id: str, name: str):
        self.db.execute(
            """INSERT INTO threads (thread_id, name, first_seen_ms, last_seen_ms)
               VALUES (?,?,?,?)
               ON CONFLICT(thread_id) DO UPDATE SET name = excluded.name""",
            (thread_id, name, int(time.time()*1000), int(time.time()*1000)),
        )

    # ── raw ──────────────────────────────────────────────────────────────────
    def add_raw(self, kind: str, url: str, payload: str):
        if not self.store_raw:
            return
        self.db.execute(
            "INSERT INTO raw_events (kind, url, ts_ms, payload) VALUES (?,?,?,?)",
            (kind, url, int(time.time()*1000), payload),
        )
        self._pending += 1

    # ── tiện ích ──────────────────────────────────────────────────────────────
    def commit(self):
        self.db.commit()
        self._pending = 0

    def stats(self) -> dict:
        c = self.db.cursor()
        msgs = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        threads = c.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return {"messages": msgs, "threads": threads, "users": users}

    def close(self):
        self.commit()
        self.db.close()
