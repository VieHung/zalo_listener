"""
Giải mã payload của Zalo Web.

Cơ chế (đã reverse-engineer bởi cộng đồng, xem README/nguồn):
  - Response/params được mã hoá AES rồi Base64.
  - Key: `zpw_enk` (secret key), Base64, dài 16 byte (AES-128) sau khi decode.
  - Biến thể phổ biến:
      * CBC, PKCS7, IV = 16 byte 0x00        (hay gặp nhất ở web hiện tại)
      * ECB, PKCS7                             (một số endpoint / bản cũ)
  - Có endpoint gói dữ liệu trong {"data": "<b64>"} — tự bóc lớp đó.

Zalo có thể đổi convention. Vì vậy hàm dưới THỬ nhiều tổ hợp và trả về cái
đầu tiên ra JSON hợp lệ. Không có gì bảo đảm 100% — luôn giữ raw để reparse.
"""
from __future__ import annotations

import base64
import binascii
import json
import urllib.parse
import zlib
from dataclasses import dataclass
from typing import Any, Optional

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

ZERO_IV = b"\x00" * 16


@dataclass(frozen=True)
class WSFrame:
    """Một frame protocol của Zalo nằm bên trong WebSocket binary message."""

    version: int
    cmd: int
    sub_cmd: int
    payload: dict


def _b64(s: str) -> Optional[bytes]:
    try:
        return base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        return None


def normalize_key(secret_key_b64: str) -> Optional[bytes]:
    """Chuẩn hoá key về đúng độ dài AES (16/24/32 byte)."""
    if not secret_key_b64:
        return None
    raw = _b64(secret_key_b64.strip())
    if raw is None:
        # có thể key là chuỗi thô (ít gặp)
        raw = secret_key_b64.encode("utf-8", "ignore")
    if len(raw) in (16, 24, 32):
        return raw
    if len(raw) > 32:
        return raw[:32]
    if len(raw) > 24:
        return raw[:24]
    if len(raw) > 16:
        return raw[:16]
    return None


def _try_unpad(data: bytes) -> bytes:
    try:
        return unpad(data, AES.block_size)
    except ValueError:
        return data  # padding không chuẩn -> trả nguyên, để _as_json quyết


def _as_text(data: bytes) -> Optional[str]:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def decrypt(cipher_b64: str, key: bytes) -> Optional[str]:
    """
    Thử CBC(IV=0) rồi ECB. Trả về chuỗi UTF-8 nếu thành công, else None.
    """
    ct = _b64(cipher_b64)
    if not ct or len(ct) < 16 or len(ct) % 16 != 0:
        return None

    # CBC, IV = 0
    try:
        pt = _try_unpad(AES.new(key, AES.MODE_CBC, ZERO_IV).decrypt(ct))
        txt = _as_text(pt)
        if txt is not None and _looks_useful(txt):
            return txt
    except (ValueError, KeyError):
        pass

    # ECB
    try:
        pt = _try_unpad(AES.new(key, AES.MODE_ECB).decrypt(ct))
        txt = _as_text(pt)
        if txt is not None and _looks_useful(txt):
            return txt
    except (ValueError, KeyError):
        pass

    return None


def parse_ws_frame(frame_b64: str) -> Optional[WSFrame]:
    """
    Bóc framing WebSocket hiện tại của Zalo:

      byte 0      : version
      byte 1..2   : command, uint16 little-endian
      byte 3      : sub-command
      byte 4..end : JSON UTF-8

    `inject.js` chuyển binary frame sang Base64 trước khi gửi về Python.
    """
    raw = _b64(frame_b64)
    if raw is None or len(raw) < 5:
        return None
    try:
        payload = json.loads(raw[4:].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return WSFrame(
        version=raw[0],
        cmd=int.from_bytes(raw[1:3], "little", signed=False),
        sub_cmd=raw[3],
        payload=payload,
    )


def decode_ws_event(payload: dict, cipher_key: Optional[bytes]) -> Optional[Any]:
    """
    Giải mã field `data` của một Zalo WebSocket event.

    `encrypt` có bốn dạng đang gặp:
      0: chuỗi JSON thuần
      1: Base64 + deflate
      2: Base64 + AES-GCM + deflate
      3: Base64 + AES-GCM, không nén

    Với AES-GCM, buffer gồm IV 16 byte, AAD 16 byte, rồi ciphertext + tag.
    """
    data = payload.get("data")
    encrypt_type = payload.get("encrypt")
    if not isinstance(data, str) or not isinstance(encrypt_type, int):
        return None
    if encrypt_type not in (0, 1, 2, 3):
        return None

    if encrypt_type == 0:
        return _safe_json(data)

    encoded = data if encrypt_type == 1 else urllib.parse.unquote(data)
    decoded = _b64(encoded)
    if decoded is None:
        return None

    if encrypt_type != 1:
        if cipher_key is None or len(decoded) < 48:
            return None
        iv = decoded[:16]
        aad = decoded[16:32]
        encrypted = decoded[32:]
        if len(encrypted) < 16:
            return None
        ciphertext, tag = encrypted[:-16], encrypted[-16:]
        try:
            cipher = AES.new(cipher_key, AES.MODE_GCM, nonce=iv, mac_len=16)
            cipher.update(aad)
            decoded = cipher.decrypt_and_verify(ciphertext, tag)
        except (ValueError, KeyError):
            return None

    if encrypt_type != 3:
        decoded = _inflate(decoded)
        if decoded is None:
            return None

    text = _as_text(decoded)
    return _safe_json(text) if text is not None else None


def _inflate(data: bytes) -> Optional[bytes]:
    """Tương thích zlib, raw-deflate và gzip giống `pako.inflate`."""
    for window_bits in (zlib.MAX_WBITS, -zlib.MAX_WBITS, zlib.MAX_WBITS | 16):
        try:
            return zlib.decompress(data, window_bits)
        except zlib.error:
            pass
    return None


def _looks_useful(txt: str) -> bool:
    """Heuristic: bản rõ thường là JSON hoặc chứa nhiều ký tự in được."""
    t = txt.lstrip()
    if t[:1] in ("{", "["):
        return True
    printable = sum(1 for c in txt if c.isprintable() or c in "\n\r\t")
    return len(txt) > 0 and printable / len(txt) > 0.9


def try_decrypt_payload(body: str, key: Optional[bytes]) -> Optional[dict]:
    """
    Nhận body thô của một response. Trả về dict đã parse nếu:
      - body vốn là JSON thường (không mã hoá), hoặc
      - body/field con là ciphertext giải mã được bằng `key`.
    """
    if body is None:
        return None
    body = body.strip()

    # 1) Đã là JSON thường?
    obj = _safe_json(body)
    if obj is not None:
        # Nhiều response bọc ciphertext trong {"data": "..."} hoặc {"error_data": ...}
        if key is not None and isinstance(obj, dict):
            for field in ("data", "error_data", "encryptResp"):
                v = obj.get(field)
                if isinstance(v, str) and len(v) >= 24:
                    dec = decrypt(v, key)
                    if dec:
                        inner = _safe_json(dec)
                        return {"_decrypted_from": field, "data": inner if inner is not None else dec}
        return obj

    # 2) Body nguyên khối là ciphertext?
    if key is not None:
        dec = decrypt(body, key)
        if dec:
            inner = _safe_json(dec)
            return {"_decrypted_from": "body", "data": inner if inner is not None else dec}

    return None


def _safe_json(s: str):
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


def hunt_key_from_body(body: str) -> Optional[str]:
    """
    Một số response (vd getlogininfo/getserverinfo) chứa zpw_enk ở dạng rõ.
    Quét các field khả dĩ và trả về giá trị base64 đầu tiên nhìn giống key.
    """
    obj = _safe_json(body)
    if not isinstance(obj, (dict, list)):
        return None

    hits: list[str] = []

    def walk(node, keyname=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k).lower())
        elif isinstance(node, list):
            for v in node:
                walk(v, keyname)
        elif isinstance(node, str):
            if any(t in keyname for t in ("enk", "secret", "zpw", "key")):
                if 16 <= len(node) <= 64 and normalize_key(node):
                    hits.append(node)

    walk(obj)
    return hits[0] if hits else None
