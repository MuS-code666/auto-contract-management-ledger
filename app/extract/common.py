"""抽出処理の共通部品。

各抽出項目は次の形の辞書で表す（FieldResult）。
    {
      "value":    正規化値（日付は YYYY-MM-DD、区分は列挙値）または None,
      "raw":      原文上の表記（日付の原文など）,
      "page":     根拠ページ番号（1始まり）,
      "evidence": 根拠となる原文,
      "source":   "text"（PDFの既存テキスト） / "ocr"（OCR結果）,
      "method":   "rule" / "llm" / "rule+llm",
      "state":    "抽出" / "記載なし" / "読取不能" / "要確認",
      "note":     補足（要確認の理由・計算方法など）,
      "alternatives": 別候補のリスト（矛盾時）
    }
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass

EXTRACT_FIELDS = [
    "title", "doc_type", "category", "summary",
    "contract_date", "start_date", "term_raw", "expiry_date", "expiry_type",
    "auto_renewal", "renewal_terms", "renewal_notice", "termination_notice",
]
LIST_FIELDS = ["parties", "signers"]

STATE_FOUND = "抽出"
STATE_NONE = "記載なし"
STATE_UNREADABLE = "読取不能"
STATE_CHECK = "要確認"


def field(value=None, *, raw=None, page=None, evidence=None, source=None, method="rule",
          state=STATE_FOUND, note="", **extra) -> dict:
    d = {"value": value, "raw": raw, "page": page, "evidence": evidence, "source": source,
         "method": method, "state": state, "note": note}
    d.update(extra)
    return d


def missing(doc: "Doc", note_none: str = "", method: str = "rule") -> dict:
    """見つからなかった項目。未処理ページがあれば「記載なし」と断定しない。"""
    if doc.unprocessed_pages:
        pages = ",".join(str(p) for p in doc.unprocessed_pages)
        return field(None, method=method, state=STATE_CHECK,
                     note=f"読み取れないページ（{pages}）があるため、記載なしと断定できません")
    return field(None, method=method, state=STATE_NONE, note=note_none)


def norm(s: str | None) -> str:
    """比較用正規化：NFKC・空白除去。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or ""))


def soft_flatten(text: str) -> str:
    """PDFの折返し改行を除去する。短い行（見出し・署名欄）や句点で終わる行の改行は残す。"""
    lines = [l.strip() for l in text.split("\n")]
    out: list[str] = []
    buf = ""
    for line in lines:
        if not line:
            if buf:
                out.append(buf)
                buf = ""
            continue
        buf += line
        if len(line) < 20 or line.endswith(("。", "）", "」", ")", "：", ":")):
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return "\n".join(out)


@dataclass
class Page:
    page_no: int
    text: str
    source: str  # text / ocr
    status: str  # ok / empty / unprocessed / ocr_failed / low_quality


USABLE = ("ok", "low_quality")  # 低品質OCRのページも本文としては使う（ただし未処理扱いで要確認になる）


class Doc:
    """全ページを連結した文書。位置→ページ番号の対応を保持する。"""

    def __init__(self, pages: list[Page]):
        self.pages = pages
        self.unprocessed_pages = [p.page_no for p in pages if p.status not in ("ok", "empty")]
        parts, self.starts, pos = [], [], 0
        for p in pages:
            flat = soft_flatten(p.text) if p.status in USABLE else ""
            self.starts.append(pos)
            parts.append(flat)
            pos += len(flat) + 1
        self.full = "\n".join(parts)
        self._page_by_no = {p.page_no: p for p in pages}

    def page_at(self, pos: int) -> int:
        i = bisect.bisect_right(self.starts, pos) - 1
        return self.pages[max(i, 0)].page_no

    def source_of(self, page_no: int) -> str | None:
        p = self._page_by_no.get(page_no)
        return p.source if p else None

    def sentences(self) -> list[tuple[int, str]]:
        """(開始位置, 文) のリスト。句点・改行で区切る。"""
        out = []
        for m in re.finditer(r"[^。\n]+。?", self.full):
            s = m.group(0).strip()
            if s:
                out.append((m.start(), s))
        return out

    def find_quote(self, quote: str) -> int | None:
        """引用文が本文中に実在するか（空白・全半角差を無視）。見つかればページ番号。"""
        q = norm(quote)
        if len(q) < 2:
            return None
        for p in self.pages:
            if p.status in USABLE and q in norm(p.text):
                return p.page_no
        # ページ境界をまたぐ引用
        if q in norm(self.full):
            # 先頭部分が含まれるページを返す
            head = q[: max(2, min(10, len(q)))]
            for p in self.pages:
                if head in norm(p.text):
                    return p.page_no
        return None
