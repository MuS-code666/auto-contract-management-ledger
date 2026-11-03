"""台帳のExcel（.xlsx）出力（openpyxl / MIT）。

- 契約書由来の文字列はすべて「文字列」型で書き込み、Excelの数式として解釈させない。
  さらに先頭が = + - @ の文字列には先頭に ' を付け、編集時の数式化も防ぐ。
- 未確定の日付（通知期限の計算候補）は確定値と別の列に、文字列「候補: …」として出す。
- 空欄と「記載なし」「要確認」を区別して表示する。
"""
from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import dates as D

COLUMNS = [
    # (見出し, 幅, 種別)
    ("管理番号", 11, "text"), ("契約書名", 30, "wrap"), ("契約書の種類", 14, "text"), ("カテゴリー", 14, "text"),
    ("当社", 22, "wrap"), ("相手方", 28, "wrap"), ("要旨", 48, "wrap"),
    ("契約日", 12, "date"), ("契約開始日", 12, "date"), ("有効期限", 12, "date"), ("期限の区分", 12, "text"),
    ("管理上の期限", 12, "date"), ("自動更新", 9, "text"), ("更新条件", 40, "wrap"),
    ("更新拒絶の通知条件", 40, "wrap"), ("通知期限（確定）", 13, "date"), ("通知期限（候補・未確定）", 18, "text"),
    ("中途解約の通知条件", 36, "wrap"), ("次回確認予定日", 13, "date"),
    ("社内担当部門", 14, "text"), ("担当者", 12, "text"), ("紙原本の保管場所", 18, "wrap"),
    ("確認状態", 10, "text"), ("要確認の項目", 22, "wrap"),
    ("PDF保存先（管理ルートからの相対パス）", 40, "wrap"), ("PDFを開く", 10, "link"), ("元ファイル名", 28, "wrap"),
    ("登録日時", 17, "text"), ("更新日時", 17, "text"), ("確認日時", 17, "text"), ("備考", 30, "wrap"), ("削除", 7, "text"),
]
META_LABEL = {"title": "契約書名", "doc_type": "種類", "category_id": "カテゴリー", "summary": "要旨",
              "contract_date": "契約日", "start_date": "開始日", "term_raw": "契約期間", "expiry_date": "有効期限",
              "expiry_type": "期限の区分", "auto_renewal": "自動更新", "renewal_terms": "更新条件",
              "renewal_notice_raw": "更新拒絶通知", "termination_notice_raw": "中途解約通知",
              "parties": "当事者", "signers": "署名者"}


def safe_text(v) -> str | None:
    if v is None:
        return None
    s = ILLEGAL_CHARACTERS_RE.sub("", str(v))
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        s = "'" + s
    return s[:32000]


def _state(c: dict, key: str) -> str:
    return ((c.get("field_meta") or {}).get(key) or {}).get("state", "")


def _blank_label(c: dict, key: str) -> str | None:
    st = _state(c, key)
    return st if st in ("記載なし", "要確認", "読取不能") else None


def row_values(c: dict, root_paths: dict[int, str]) -> list:
    own = "、".join(p["name"] for p in c.get("parties", []) if p.get("role") == "当社")
    other = "、".join(p["name"] for p in c.get("parties", []) if p.get("role") != "当社")

    def dv(key):
        d = D.parse_iso(c.get(key))
        return d if d else _blank_label(c, key)

    def tv(key, val=None):
        val = c.get(key) if val is None else val
        return val if val not in (None, "") else _blank_label(c, key)

    cand = c.get("notice_deadline_candidate")
    cand_txt = f"候補: {cand.replace('-', '/')}（未確定）" if cand and not c.get("notice_deadline_confirmed") else None
    checks = "、".join(META_LABEL.get(k, k) for k, m in (c.get("field_meta") or {}).items()
                      if (m or {}).get("state") == "要確認")
    pdf_abs = None
    if c.get("pdf_relpath") and c.get("storage_root_id") in root_paths:
        pdf_abs = str(Path(root_paths[c["storage_root_id"]]) / c["pdf_relpath"])
    return [
        c["mgmt_no"], tv("title"), tv("doc_type"), c.get("category_name"), own or None, other or tv("parties", other),
        tv("summary"), dv("contract_date"), dv("start_date"), dv("expiry_date"), c.get("expiry_type"),
        D.parse_iso(c.get("managed_expiry_date")), c.get("auto_renewal"), tv("renewal_terms"),
        tv("renewal_notice_raw"), D.parse_iso(c.get("notice_deadline_confirmed")), cand_txt,
        tv("termination_notice_raw"), D.parse_iso(c.get("next_review_date")),
        c.get("dept"), c.get("owner"), c.get("paper_location"), c.get("review_status"), checks or None,
        c.get("pdf_relpath"), pdf_abs, c.get("original_filename"),
        c.get("created_at"), c.get("updated_at"), c.get("reviewed_at"), c.get("notes"),
        "削除済み" if c.get("deleted_at") else None,
    ]


def build_workbook(contracts: list[dict], root_paths: dict[int, str], condition: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "契約台帳"
    head_fill = PatternFill("solid", fgColor="DDE5F0")
    for i, (name, width, _) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=i, value=name)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = width
    warn_font = Font(color="B00020")
    for r, c in enumerate(contracts, start=2):
        for i, (val, (_, _, kind)) in enumerate(zip(row_values(c, root_paths), COLUMNS), start=1):
            cell = ws.cell(row=r, column=i)
            if kind == "link":
                if val:
                    cell.value = "開く"
                    cell.data_type = "s"
                    cell.hyperlink = Path(val).as_uri()
                    cell.font = Font(color="0563C1", underline="single")
                continue
            if isinstance(val, (date, datetime)):
                cell.value = val
                cell.number_format = "yyyy/mm/dd"
            elif val is not None:
                cell.value = safe_text(val)
                cell.data_type = "s"  # 数式として扱わせない
                if val in ("記載なし", "要確認", "読取不能") or (isinstance(val, str) and val.startswith("候補:")):
                    cell.font = warn_font
            cell.alignment = Alignment(wrap_text=(kind == "wrap"), vertical="top")
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(1, len(contracts) + 1)}"
    ws.row_dimensions[1].height = 32

    note = wb.create_sheet("説明")
    lines = [
        ("出力日時", datetime.now().strftime("%Y/%m/%d %H:%M")),
        ("出力条件", condition),
        ("件数", str(len(contracts))),
        ("", ""),
        ("表示の意味", "空欄＝未入力。「記載なし」＝契約書に記載がないと確認された項目。「要確認」＝解釈や読取りの確認が必要な項目。"),
        ("通知期限", "「通知期限（確定）」は利用者が確定した日付。「通知期限（候補・未確定）」はアプリが条文から計算した候補で、確定値ではありません。"),
        ("有効期限", "管理上の期限は、変更覚書など関連書類を確認して利用者が確定した期限です。元の契約の有効期限は別の列に残しています。"),
        ("PDFリンク", "「開く」リンクは出力したPCの保存場所を指しています。別のPCや保存先の変更後は開けない場合があります。"),
        ("注意", "このファイルを編集してもアプリの台帳には反映されません（再取込みは対象外）。"),
    ]
    for r, (k, v) in enumerate(lines, start=1):
        a = note.cell(row=r, column=1, value=k)
        a.font = Font(bold=True)
        b = note.cell(row=r, column=2, value=safe_text(v))
        b.data_type = "s"
        b.alignment = Alignment(wrap_text=True)
    note.column_dimensions["A"].width = 14
    note.column_dimensions["B"].width = 100
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
