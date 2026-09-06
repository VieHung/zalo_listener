from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path

from Crypto.Cipher import AES

from zalo_listener.crypto import decode_ws_event, parse_ws_frame
from zalo_listener.extract import extract_messages
from zalo_listener.privacy import PrivacyFilter
from zalo_listener.store import Store


def _deflate_raw(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


class WebSocketDecoderTests(unittest.TestCase):
    def setUp(self):
        self.key = bytes(range(32))
        self.obj = {
            "error_code": 0,
            "data": {
                "groupMsgs": [
                    {
                        "msgId": "m1",
                        "idTo": "g1",
                        "uidFrom": "self-user",
                        "userId": "self-user",
                        "content": "hello",
                    }
                ]
            },
        }
        self.plain = json.dumps(self.obj).encode()

    def test_parse_binary_frame_header(self):
        payload = {"encrypt": 0, "data": "{}"}
        raw = bytes((1, 9, 2, 0)) + json.dumps(payload).encode()
        frame = parse_ws_frame(base64.b64encode(raw).decode())

        self.assertIsNotNone(frame)
        self.assertEqual(frame.version, 1)
        self.assertEqual(frame.cmd, 521)
        self.assertEqual(frame.sub_cmd, 0)
        self.assertEqual(frame.payload, payload)

    def test_decode_all_encryption_types(self):
        cases = {
            0: json.dumps(self.obj),
            1: base64.b64encode(_deflate_raw(self.plain)).decode(),
            2: self._encrypt_gcm(_deflate_raw(self.plain)),
            3: self._encrypt_gcm(self.plain),
        }
        for encrypt_type, data in cases.items():
            with self.subTest(encrypt_type=encrypt_type):
                decoded = decode_ws_event(
                    {"encrypt": encrypt_type, "data": data}, self.key
                )
                self.assertEqual(decoded, self.obj)

    def _encrypt_gcm(self, plain: bytes) -> str:
        iv = b"i" * 16
        aad = b"a" * 16
        cipher = AES.new(self.key, AES.MODE_GCM, nonce=iv, mac_len=16)
        cipher.update(aad)
        ciphertext, tag = cipher.encrypt_and_digest(plain)
        return base64.b64encode(iv + aad + ciphertext + tag).decode()


class ExtractionAndStorageTests(unittest.TestCase):
    def test_group_context_and_direction(self):
        payload = {
            "groupMsgs": [
                {
                    "msgId": "out",
                    "idTo": "group-1",
                    "uidFrom": "me",
                    "userId": "me",
                    "content": "sent here",
                },
                {
                    "msgId": "in",
                    "idTo": "group-1",
                    "uidFrom": "other",
                    "userId": "me",
                    "content": "sent elsewhere",
                },
            ]
        }

        messages = extract_messages(payload, default_thread_type="group")

        self.assertEqual([m.thread_type for m in messages], ["group", "group"])
        self.assertEqual([m.direction for m in messages], ["outgoing", "incoming"])

    def test_existing_database_is_migrated(self):
        with tempfile.TemporaryDirectory(prefix="zalo-store-test-") as tmp:
            path = Path(tmp) / "messages.sqlite3"
            db = sqlite3.connect(path)
            db.execute(
                """
                CREATE TABLE messages (
                    msg_id TEXT PRIMARY KEY,
                    thread_id TEXT,
                    thread_type TEXT,
                    uid_from TEXT,
                    ts_ms INTEGER,
                    msg_type TEXT,
                    text TEXT,
                    quote_msg_id TEXT,
                    raw_json TEXT,
                    ingested_ms INTEGER
                )
                """
            )
            db.commit()
            db.close()

            store = Store(
                str(path),
                PrivacyFilter({"salt": "test"}),
                store_raw=False,
            )
            columns = {
                row[1] for row in store.db.execute("PRAGMA table_info(messages)")
            }
            store.close()

            self.assertIn("direction", columns)


if __name__ == "__main__":
    unittest.main()
