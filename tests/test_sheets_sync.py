from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from zalo_listener import sheets_sync as ss
from zalo_listener.classify import Classifier
from zalo_listener.extract import Message, extract_name_hints
from zalo_listener.privacy import PrivacyFilter
from zalo_listener.store import Store


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.c = Classifier({
            "categories": {
                "Giao dịch": ["chuyển khoản", "ck"],
                "Cần xử lý": ["gấp", "lỗi"],
            },
            "default_label": "Khác",
        })

    def test_accent_insensitive_match(self):
        # gõ không dấu vẫn khớp keyword có dấu
        self.assertEqual(self.c.classify("anh oi chuyen khoan giup em"), ["Giao dịch"])

    def test_multi_label(self):
        self.assertEqual(
            set(self.c.classify("Lỗi rồi, chuyển khoản gấp")),
            {"Giao dịch", "Cần xử lý"},
        )

    def test_default_when_no_match(self):
        self.assertEqual(self.c.classify("xin chào"), ["Khác"])

    def test_empty_text(self):
        self.assertEqual(self.c.classify(""), ["Khác"])
        self.assertEqual(Classifier({}).classify("bất kỳ"), [])

    def test_label_order_stable(self):
        # thứ tự nhãn theo thứ tự khai báo category
        self.assertEqual(self.c.label_str("ck bị lỗi"), "Giao dịch, Cần xử lý")


class NameHintTests(unittest.TestCase):
    def test_group_and_user_names(self):
        payload = {
            "groups": [{"groupId": "999", "name": "Nhóm CSKH"}],
            "msgs": [{"msgId": "m1", "uidFrom": "555", "dName": "Nguyễn Văn A",
                      "content": "hi", "idTo": "999"}],
        }
        hints = {(h.kind, h.raw_id, h.name) for h in extract_name_hints(payload)}
        self.assertIn(("group", "999", "Nhóm CSKH"), hints)
        self.assertIn(("user", "555", "Nguyễn Văn A"), hints)

    def test_numeric_name_rejected(self):
        # tên toàn số / trùng id -> không nhận (dễ là id)
        hints = extract_name_hints({"groupId": "999", "name": "999"})
        self.assertEqual(hints, [])


class StoreNameTests(unittest.TestCase):
    def _store(self):
        d = tempfile.mkdtemp()
        return Store(
            str(Path(d) / "t.sqlite3"),
            PrivacyFilter({"pseudonymize_uid": False, "store_display_name": False}),
            store_raw=False,
        )

    def test_group_name_idempotent(self):
        s = self._store()
        s.upsert_message(Message("m1", "999", "group", "5", "A", 1, "1", "hi", None, "incoming", {}))
        self.assertTrue(s.note_group_name("999", "Nhóm CSKH"))
        self.assertFalse(s.note_group_name("999", "Nhóm CSKH"))
        name = s.db.execute("SELECT name FROM threads WHERE thread_id='999'").fetchone()[0]
        self.assertEqual(name, "Nhóm CSKH")
        s.close()

    def test_user_name_only_for_existing_user_thread(self):
        s = self._store()
        s.upsert_message(Message("m2", "555", "user", "555", "A", 1, "1", "hi", None, "incoming", {}))
        self.assertTrue(s.note_user_name("555", "Nguyễn Văn A"))
        self.assertFalse(s.note_user_name("404", "Không tồn tại"))  # không tạo rác
        rows = dict(s.db.execute("SELECT thread_id, name FROM threads").fetchall())
        self.assertEqual(rows["555"], "Nguyễn Văn A")
        self.assertNotIn("404", rows)
        s.close()


class SheetHelperTests(unittest.TestCase):
    def test_safe_title(self):
        self.assertEqual(ss._safe_title("Nhom/CSKH: [test]*"), "Nhom CSKH test")
        self.assertEqual(ss._safe_title("   "), "thread")
        self.assertEqual(len(ss._safe_title("x" * 200)), 95)

    def test_row_values(self):
        obj = object.__new__(ss.SheetSync)
        obj.columns = ["ts_local", "direction", "sender", "category", "text", "msg_id"]
        obj.classifier = Classifier({"categories": {"Giao dịch": ["ck"]}})
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        r = db.execute(
            "SELECT '09:02 08/09/2026' AS ts_local, 'incoming' AS direction, "
            "'u_abc' AS sender, 'CK gap' AS text, 'm1' AS msg_id"
        ).fetchone()
        self.assertEqual(
            obj._row_values(r),
            ["09:02 08/09/2026", "đến", "u_abc", "Giao dịch", "CK gap", "m1"],
        )


if __name__ == "__main__":
    unittest.main()
