"""
Phân loại tin nhắn theo keyword — đa nhãn, cấu hình bằng config.

Ý tưởng: mỗi category là một danh sách keyword. So khớp trên nội dung đã
chuẩn hoá: bỏ dấu tiếng Việt + lowercase, nên "chuyen khoan" khớp cả
"Chuyển khoản". Một tin có thể mang nhiều nhãn (multi-label).

Config mẫu (trong config.yaml):

    classify:
      categories:
        "Cần xử lý":  ["gấp", "khẩn", "lỗi", "không vào được"]
        "Giao dịch":  ["chuyển khoản", "thanh toán", "hoá đơn"]
        "Khiếu nại":  ["phàn nàn", "khiếu nại", "kém"]
      default_label: ""        # nhãn khi không khớp gì (để rỗng = không gắn)
      accent_insensitive: true # bỏ dấu khi so khớp
"""
from __future__ import annotations

import unicodedata
from typing import Iterable, Optional


def strip_accents(text: str) -> str:
    """Bỏ dấu tiếng Việt: NFD tách dấu rồi loại các ký tự combining. đ/Đ -> d."""
    text = text.replace("đ", "d").replace("Đ", "D")
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if not unicodedata.combining(c))


def _norm(text: str, accent_insensitive: bool) -> str:
    text = unicodedata.normalize("NFC", text).lower()
    return strip_accents(text) if accent_insensitive else text


class Classifier:
    def __init__(self, cfg: Optional[dict] = None):
        cfg = cfg or {}
        self.accent_insensitive = cfg.get("accent_insensitive", True)
        self.default_label = cfg.get("default_label", "") or ""
        # Giữ thứ tự khai báo -> nhãn xuất ra ổn định.
        self.categories: list[tuple[str, list[str]]] = []
        for name, keywords in (cfg.get("categories") or {}).items():
            norm_kw = [
                self._norm(str(k))
                for k in (keywords or [])
                if str(k).strip()
            ]
            if norm_kw:
                self.categories.append((str(name), norm_kw))

    def _norm(self, text: str) -> str:
        return _norm(text, self.accent_insensitive)

    def classify(self, text: Optional[str]) -> list[str]:
        """Trả về danh sách nhãn khớp (có thể rỗng, hoặc [default_label])."""
        if not text:
            return [self.default_label] if self.default_label else []
        hay = self._norm(text)
        labels = [name for name, kws in self.categories if any(k in hay for k in kws)]
        if not labels and self.default_label:
            return [self.default_label]
        return labels

    def label_str(self, text: Optional[str], sep: str = ", ") -> str:
        """Gộp các nhãn thành một chuỗi để ghi vào ô 'Phân loại'."""
        return sep.join(self.classify(text))
