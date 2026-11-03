"""日付の解析と期間計算。

■ 日付解析
- 西暦（2024年4月1日 / 2024/4/1 / 2024-04-01 / 2024.4.1）、和暦（令和・平成・昭和・大正、
  「元年」、略記 R6.4.1 / H31.4.30）、全角数字、漢数字（令和六年四月一日・二〇二四年）に対応。
- 年・月・日のいずれかが欠ける場合や空欄（「令和　年　月　日」）は日付を補完せず、
  status="incomplete"/"blank" として原文のみ保持する。
- 存在しない日付（2月30日など）は status="invalid"。
- 元号の範囲外（平成32年など）は換算値を出しつつ status="era_out_of_range"（要確認扱い）。

■ 期間計算ルール（民法第140条・第143条の考え方に基づく）
- add_months(d, n): 応当日（同じ日）を返す。応当日が存在しない月はその月の末日。
  例）1/31 + 1か月 → 2/28（うるう年は2/29）。5/31 - 3か月 → 2/28(29)。
- period_end(start, months): 開始日を含めて n か月の期間の末日。
  応当日の前日。応当日が存在しない場合はその月の末日。
  例）2024/4/1 から1年 → 2025/3/31。2024/2/29 から1年 → 2025/2/28。
      2024/1/31 から1か月 → 2024/2/29（応当日2/31が無いため2月末日）。
- 「〇日前」は暦日で単純に減算する（営業日・休日の考慮はしない）。
- 解釈が必要な条件（「業務完了まで」等）は計算しない。
"""
from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

ERAS = [
    # 名称, 略号, 開始日, 終了日
    ("令和", "R", date(2019, 5, 1), None),
    ("平成", "H", date(1989, 1, 8), date(2019, 4, 30)),
    ("昭和", "S", date(1926, 12, 25), date(1989, 1, 7)),
    ("大正", "T", date(1912, 7, 30), date(1926, 12, 25)),
]
_ERA_BY_NAME = {e[0]: e for e in ERAS}
_ERA_BY_ABBR = {e[1]: e for e in ERAS}

_KANJI_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_UNIT = {"十": 10, "百": 100, "千": 1000}


def normalize_text(s: str) -> str:
    """全角英数字を半角に、各種空白を半角スペースに。"""
    return unicodedata.normalize("NFKC", s or "")


def kanji_to_int(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "元":
        return 1
    if all(ch in _KANJI_DIGIT for ch in s):  # 二〇二四 形式
        return int("".join(str(_KANJI_DIGIT[ch]) for ch in s))
    total, cur = 0, 0
    for ch in s:
        if ch in _KANJI_DIGIT:
            cur = _KANJI_DIGIT[ch]
        elif ch in _KANJI_UNIT:
            total += (cur or 1) * _KANJI_UNIT[ch]
            cur = 0
        else:
            return None
    return total + cur


_NUM = r"[0-9０-９〇零一二三四五六七八九十百千]+|元"
_SP = r"[\s　]*"

# 年月日（年・月・日の数字は欠けていてもよい → 不完全として検出する）
DATE_RE = re.compile(
    rf"(?P<era>令和|平成|昭和|大正|西暦)?{_SP}(?P<y>{_NUM})?{_SP}年{_SP}(?P<m>{_NUM})?{_SP}月{_SP}(?:(?P<d>{_NUM})?{_SP}日)?"
)
SLASH_RE = re.compile(r"(?<![0-9])(?P<y>(?:19|20)\d{2})\s*[/\-.]\s*(?P<m>\d{1,2})\s*[/\-.]\s*(?P<d>\d{1,2})(?![0-9])")
ABBR_RE = re.compile(r"(?<![A-Za-z])(?P<era>[RHST])\s*(?P<y>\d{1,2})\s*[./]\s*(?P<m>\d{1,2})\s*[./]\s*(?P<d>\d{1,2})(?![0-9])")


@dataclass
class ParsedDate:
    raw: str
    value: date | None
    status: str  # ok / incomplete / blank / invalid / era_out_of_range
    note: str = ""
    span: tuple[int, int] = field(default=(0, 0))

    @property
    def iso(self) -> str | None:
        return self.value.isoformat() if self.value else None


def _era_year(era: str | None, y: int) -> tuple[int, str]:
    if not era or era == "西暦":
        return y, ""
    e = _ERA_BY_NAME.get(era) or _ERA_BY_ABBR.get(era)
    return e[2].year + y - 1, e[0]


def _check_era(era_name: str, d: date) -> bool:
    e = _ERA_BY_NAME[era_name]
    if d < e[2]:
        return False
    if e[3] is not None and d > e[3]:
        return False
    return True


def _build(raw: str, era: str | None, ys, ms, ds, span) -> ParsedDate:
    y = kanji_to_int(ys) if ys else None
    m = kanji_to_int(ms) if ms else None
    d = kanji_to_int(ds) if ds else None
    if y is None and m is None and d is None:
        return ParsedDate(raw, None, "blank", "日付欄が空欄です", span)
    if y is None or m is None or d is None:
        missing = [n for n, v in (("年", y), ("月", m), ("日", d)) if v is None]
        return ParsedDate(raw, None, "incomplete", f"{''.join(missing)}の記載がありません", span)
    if era and era != "西暦" and y > 99:
        return ParsedDate(raw, None, "invalid", "和暦の年が不正です", span)
    gy, era_name = _era_year(era, y)
    try:
        val = date(gy, m, d)
    except ValueError:
        return ParsedDate(raw, None, "invalid", "存在しない日付です", span)
    if era_name and not _check_era(era_name, val):
        return ParsedDate(raw, val, "era_out_of_range", f"{era_name}の期間外の日付です（換算値は参考）", span)
    return ParsedDate(raw, val, "ok", "", span)


def find_dates(text: str) -> list[ParsedDate]:
    """文中の日付表現をすべて抽出する（位置順）。"""
    out: list[ParsedDate] = []
    taken: list[tuple[int, int]] = []

    def overlaps(a, b):
        return any(not (b <= s or a >= e) for s, e in taken)

    for m in DATE_RE.finditer(text):
        # 「年」単独（例：1年間）を日付として拾わないよう、月の記載を必須とする。
        # 「年　月　日」のような空欄の書式欄は、元号または「日」まであるものだけ対象にする。
        g = m.group(0)
        if "月" not in g:
            continue
        if not (m.group("m") or m.group("era") or g.rstrip().endswith("日")):
            continue
        if m.group("y") is None and not m.group("era") and m.start() > 0 and text[m.start() - 1] in "毎翌同前各本当昨来":
            continue  # 「毎年4月1日」等は特定の日付ではない
        tail = text[m.end(): m.end() + 2]
        if m.group("d") is None and tail.startswith(("間", "分")):
            continue
        # 「1年間」「3か月」等の期間表現を除外
        after_year = text[m.start("y") if m.group("y") else m.start(): m.end()]
        if re.match(rf"(?:{_NUM}){_SP}年{_SP}間", after_year):
            continue
        out.append(_build(m.group(0), m.group("era"), m.group("y"), m.group("m"), m.group("d"), m.span()))
        taken.append(m.span())
    for rx in (SLASH_RE, ABBR_RE):
        for m in rx.finditer(text):
            if overlaps(*m.span()):
                continue
            era = m.groupdict().get("era")
            out.append(_build(m.group(0), era, m.group("y"), m.group("m"), m.group("d"), m.span()))
            taken.append(m.span())
    out.sort(key=lambda p: p.span[0])
    return out


def parse_date(text: str) -> ParsedDate | None:
    """文字列から最初の日付表現を解析する。"""
    ds = find_dates(text or "")
    return ds[0] if ds else None


def parse_iso(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- 期間計算

def days_in_month(y: int, m: int) -> int:
    return calendar.monthrange(y, m)[1]


def _shift_ym(y: int, m: int, months: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + months
    return idx // 12, idx % 12 + 1


def add_months(d: date, months: int) -> date:
    """応当日を返す。応当日が無い場合はその月の末日。"""
    y, m = _shift_ym(d.year, d.month, months)
    return date(y, m, min(d.day, days_in_month(y, m)))


def period_end(start: date, months: int) -> date:
    """開始日を初日として含む months か月の期間の末日。"""
    y, m = _shift_ym(start.year, start.month, months)
    if start.day <= days_in_month(y, m):
        return date(y, m, start.day) - timedelta(days=1)
    return date(y, m, days_in_month(y, m))


def subtract_period(d: date, value: int, unit: str) -> date:
    if unit == "日":
        return d - timedelta(days=value)
    if unit == "月":
        return add_months(d, -value)
    if unit == "年":
        return add_months(d, -12 * value)
    raise ValueError(f"未対応の単位: {unit}")


# 期間表現（1年間、6か月、3ヶ月、30日 等）
_PERIOD_RE = re.compile(
    rf"(?P<n>{_NUM}){_SP}(?P<u>年|か月|ヵ月|ヶ月|カ月|ケ月|箇月|ケ月|月|日)"
)


def parse_period(text: str) -> tuple[int, str] | None:
    """「1年間」「6か月」「30日」→ (値, 単位: 年/月/日)。"""
    m = _PERIOD_RE.search(normalize_text(text) if text else "")
    if not m:
        return None
    n = kanji_to_int(m.group("n"))
    if n is None:
        return None
    u = m.group("u")
    unit = "年" if u == "年" else ("日" if u == "日" else "月")
    return n, unit


def months_of(value: int, unit: str) -> int | None:
    if unit == "年":
        return value * 12
    if unit == "月":
        return value
    return None


def fmt_jp(d: date | None) -> str:
    return f"{d.year}年{d.month}月{d.day}日" if d else ""
