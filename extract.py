"""
Trích message ra khỏi JSON đã giải mã — theo kiểu heuristic, không phụ thuộc
vào một schema cố định (vì schema Zalo Web thay đổi theo thời gian).

Ý tưởng: đi đệ quy khắp cấu trúc, tìm các "dict trông giống message" — có một
khoá định danh (msgId/cliMsgId), một khoá người gửi (uidFrom), và một khoá
nội dung (content/message). Map linh hoạt nhiều biến thể tên khoá.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# Các biến thể tên khoá đã gặp ở Zalo Web.
ID_KEYS = ("msgId", "msgid", "message_id", "globalMsgId", "cliMsgId", "cli_msg_id")
FROM_KEYS = ("uidFrom", "uid_from", "fromUid", "senderId", "from_id", "srcId")
TO_KEYS = ("idTo", "toid", "to_id", "dName_to", "conversationId", "convId")
TS_KEYS = ("ts", "sendDttm", "timestamp", "time", "st", "at")
NAME_KEYS = ("dName", "displayName", "fromDisplayName", "senderName", "name")
CONTENT_KEYS = ("content", "message", "msg", "text", "body")
TYPE_KEYS = ("msgType", "type", "cmd", "contentType")
QUOTE_KEYS = ("quote", "quoteMsg", "reply", "refMsg")
SELF_KEYS = ("userId", "selfUid", "self_uid", "ownerId")


@dataclass
class Message:
    msg_id: str
    thread_id: Optional[str]
    thread_type: str            # "group" | "user" | "unknown"
    uid_from: Optional[str]
    display_name: Optional[str]
    ts_ms: Optional[int]
    msg_type: Optional[str]
    text: Optional[str]
    quote_msg_id: Optional[str]
    direction: str = "unknown"  # "incoming" | "outgoing" | "unknown"
    raw: dict = field(repr=False, default_factory=dict)


def _first(d: dict, keys: Iterable[str]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    # so khớp không phân biệt hoa thường
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, ""):
            return v
    return None


def _coerce_text(val: Any) -> Optional[str]:
    """content có thể là str, hoặc dict {title/text/...} với tin nhắn giàu."""
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        for k in ("title", "text", "description", "caption", "href"):
            if isinstance(val.get(k), str) and val[k]:
                return val[k]
        return None
    return str(val)


def _coerce_ts(val: Any) -> Optional[int]:
    try:
        n = int(str(val))
    except (ValueError, TypeError):
        return None
    # chuẩn hoá về mili-giây
    if n > 10_000_000_000_000:      # micro giây
        return n // 1000
    if n < 10_000_000_000:          # giây
        return n * 1000
    return n


def _looks_like_message(d: dict) -> bool:
    has_id = _first(d, ID_KEYS) is not None
    has_body = _first(d, CONTENT_KEYS) is not None
    has_from = _first(d, FROM_KEYS) is not None
    return has_id and (has_body or has_from)


def _thread_of(d: dict) -> tuple[Optional[str], str]:
    """Đoán thread_id + loại. group thường có 'groupId'/'grid' hoặc idTo dài."""
    for gk in ("groupId", "grid", "group_id", "gid"):
        v = d.get(gk)
        if v:
            return str(v), "group"
    to = _first(d, TO_KEYS)
    if to:
        return str(to), "unknown"
    return None, "unknown"


def extract_messages(obj: Any, default_thread_type: str = "unknown") -> list[Message]:
    """Đệ quy toàn bộ cấu trúc, trả về danh sách Message tìm được."""
    out: list[Message] = []
    _walk(obj, out, ctx_thread=None, ctx_type=default_thread_type)
    # khử trùng theo msg_id trong cùng một payload
    seen = set()
    uniq = []
    for m in out:
        if m.msg_id in seen:
            continue
        seen.add(m.msg_id)
        uniq.append(m)
    return uniq


def _walk(node: Any, out: list, ctx_thread: Optional[str], ctx_type: str) -> None:
    if isinstance(node, dict):
        # thu ngữ cảnh thread nếu node cha có
        t_id, t_type = _thread_of(node)
        if t_id:
            ctx_thread = t_id
            # idTo cho biết thread ID nhưng không tự cho biết user/group.
            # Giữ loại đã suy ra từ command WebSocket của node cha.
            if t_type != "unknown":
                ctx_type = t_type

        if _looks_like_message(node):
            out.append(_build(node, ctx_thread, ctx_type))
            # message có thể lồng quote là message khác -> vẫn duyệt tiếp con
        for v in node.values():
            if isinstance(v, (dict, list)):
                _walk(v, out, ctx_thread, ctx_type)

    elif isinstance(node, list):
        for v in node:
            _walk(v, out, ctx_thread, ctx_type)


def _build(d: dict, ctx_thread: Optional[str], ctx_type: str) -> Message:
    msg_id = str(_first(d, ID_KEYS))
    t_id, t_type = _thread_of(d)
    thread_id = t_id or ctx_thread
    thread_type = t_type if t_type != "unknown" else ctx_type

    quote = _first(d, QUOTE_KEYS)
    quote_id = None
    if isinstance(quote, dict):
        q = _first(quote, ID_KEYS)
        quote_id = str(q) if q is not None else None

    uid_from_value = _first(d, FROM_KEYS)
    self_uid_value = _first(d, SELF_KEYS)
    direction = "unknown"
    if uid_from_value is not None and self_uid_value is not None:
        direction = (
            "outgoing"
            if str(uid_from_value) == str(self_uid_value)
            else "incoming"
        )

    return Message(
        msg_id=msg_id,
        thread_id=thread_id,
        thread_type=thread_type,
        uid_from=(str(uid_from_value) if uid_from_value is not None else None),
        display_name=_coerce_text(_first(d, NAME_KEYS)),
        ts_ms=_coerce_ts(_first(d, TS_KEYS)),
        msg_type=(str(_first(d, TYPE_KEYS)) if _first(d, TYPE_KEYS) is not None else None),
        text=_coerce_text(_first(d, CONTENT_KEYS)),
        quote_msg_id=quote_id,
        direction=direction,
        raw=d,
    )
