"""
Lớp quyền riêng tư — áp trước khi ghi vào DB.

Vì đây là tin nhắn của NGƯỜI KHÁC trong nhóm, mặc định:
  - Giả danh uid bằng HMAC-SHA256(salt) -> không lưu uid gốc.
  - Không lưu tên hiển thị (trừ khi bật store_display_name).
  - Che PII trong nội dung: số điện thoại VN, email, dãy số dài (STK/CCCD/thẻ).

Chuẩn hoá Unicode NFC cho tiếng Việt (web hay trả NFD -> hỏng tokenizer).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from typing import Optional

# SĐT VN: 0xxxxxxxxx / +84xxxxxxxxx (9-10 số sau mã)
_PHONE = re.compile(r"(?<!\d)(?:\+?84|0)\d{9,10}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# dãy số dài 9+ chữ số (số tài khoản, CCCD, thẻ) — cho phép ngăn cách . - space
_LONGNUM = re.compile(r"(?<!\d)(?:\d[ .\-]?){9,19}\d(?!\d)")


def normalize_nfc(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return unicodedata.normalize("NFC", text)


def pseudonymize(uid: Optional[str], salt: str) -> Optional[str]:
    if uid is None:
        return None
    digest = hmac.new(salt.encode("utf-8"), str(uid).encode("utf-8"), hashlib.sha256)
    return "u_" + digest.hexdigest()[:16]


def scrub(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    text = _EMAIL.sub("[EMAIL]", text)
    text = _PHONE.sub("[PHONE]", text)
    text = _LONGNUM.sub("[NUM]", text)
    return text


class PrivacyFilter:
    def __init__(self, cfg: dict):
        self.pseudo = cfg.get("pseudonymize_uid", True)
        self.store_name = cfg.get("store_display_name", False)
        self.salt = cfg.get("salt", "change_me")
        self.do_scrub = cfg.get("scrub_pii", True)

    def uid(self, uid: Optional[str]) -> Optional[str]:
        if uid is None:
            return None
        return pseudonymize(uid, self.salt) if self.pseudo else str(uid)

    def name(self, display_name: Optional[str]) -> Optional[str]:
        if not self.store_name:
            return None
        return normalize_nfc(display_name)

    def text(self, text: Optional[str]) -> Optional[str]:
        text = normalize_nfc(text)
        if self.do_scrub:
            text = scrub(text)
        return text
