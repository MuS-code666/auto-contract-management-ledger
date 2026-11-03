"""台帳（契約）の登録・更新・検索・履歴・関連・重複候補。"""
from __future__ import annotations

import difflib
import json
import logging
import sqlite3
import uuid
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from . import deadlines as DL
from . import storage as S
from .db import now_iso, tx
from .extract.common import norm

log = logging.getLogger("ledger.repo")

REVIEW_STATES = ["未確認", "要確認", "確認済み"]
EXPIRY_TYPES = ["日付指定", "期間の定めなし", "条件終了", "記載なし", "要確認"]
RENEWAL = ["あり", "なし", "不明"]
FIELD_STATES = ["", "記載なし", "要確認", "読取不能"]
RELATION_TYPES = ["基本契約→個別契約", "原契約→変更覚書", "原契約→更新契約", "関連"]

SCALAR_FIELDS = [
    "title", "doc_type", "category_id", "summary",
    "contract_date", "contract_date_raw", "start_date", "start_date_raw", "term_raw",
    "expiry_date", "expiry_raw", "expiry_type", "auto_renewal", "renewal_terms", "renewal_period_months",
    "renewal_notice_raw", "renewal_notice_json", "termination_notice_raw", "termination_notice_json",
    "notice_deadline_confirmed", "managed_expiry_date", "managed_expiry_note", "next_review_date",
    "dept", "owner", "paper_location", "notes",
]
JSON_FIELDS = ("renewal_notice_json", "termination_notice_json")
# 抽出対象（根拠・状態を field_meta に持つ）項目
META_FIELDS = ["title", "doc_type", "category_id", "summary", "contract_date", "start_date", "term_raw",
               "expiry_date", "expiry_type", "auto_renewal", "renewal_terms", "renewal_notice_raw",
               "termination_notice_raw", "parties", "signers"]
LABELS_JA = {
    "title": "契約書名", "doc_type": "契約書の種類", "category_id": "カテゴリー", "summary": "要旨",
    "contract_date": "契約日", "contract_date_raw": "契約日（原文）", "start_date": "契約開始日",
    "start_date_raw": "開始日（原文）", "term_raw": "契約期間の原文", "expiry_date": "有効期限",
    "expiry_raw": "有効期限（原文）", "expiry_type": "期限の区分", "auto_renewal": "自動更新",
    "renewal_terms": "更新条件", "renewal_period_months": "更新期間（月）",
    "renewal_notice_raw": "更新拒絶の通知条件", "renewal_notice_json": "更新拒絶の通知条件（構造化）",
    "termination_notice_raw": "中途解約の通知条件", "termination_notice_json": "中途解約の通知条件（構造化）",
    "notice_deadline_candidate": "通知期限（候補）", "notice_deadline_confirmed": "通知期限（確定）",
    "managed_expiry_date": "管理上の期限", "managed_expiry_note": "管理上の期限の根拠",
    "next_review_date": "次回確認予定日", "dept": "社内担当部門", "owner": "担当者",
    "paper_location": "紙原本の保管場所", "notes": "備考", "parties": "契約当事者", "signers": "署名者・代表者",
    "review_status": "確認状態", "pdf": "PDF保存先",
}


class RepoError(Exception):
    def __init__(self, message: str, hint: str = "", status: int = 400, data: dict | None = None):
        super().__init__(message)
        self.hint = hint
        self.status = status
        self.data = data or {}


# ---------------------------------------------------------------- 入力スキーマ

class NoticeStruct(BaseModel):
    value: Optional[int] = Field(default=None, ge=0, le=3650)
    unit: Optional[Literal["日", "月", "年"]] = None
    base: Optional[str] = Field(default=None, max_length=20)
    text: Optional[str] = Field(default=None, max_length=200)


class PartyIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role: Literal["当社", "相手方", "その他"] = "相手方"
    label: Optional[str] = Field(default=None, max_length=10)


class SignerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    title: Optional[str] = Field(default=None, max_length=100)
    party_index: Optional[int] = None


def _date_or_none(v):
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v.isoformat()
    try:
        return date.fromisoformat(str(v)).isoformat()
    except ValueError:
        raise ValueError("日付は YYYY-MM-DD 形式で入力してください")


class ContractIn(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)
    doc_type: Optional[str] = Field(default=None, max_length=50)
    category_id: Optional[int] = None
    summary: Optional[str] = Field(default=None, max_length=4000)
    contract_date: Optional[str] = None
    contract_date_raw: Optional[str] = Field(default=None, max_length=100)
    start_date: Optional[str] = None
    start_date_raw: Optional[str] = Field(default=None, max_length=100)
    term_raw: Optional[str] = Field(default=None, max_length=4000)
    expiry_date: Optional[str] = None
    expiry_raw: Optional[str] = Field(default=None, max_length=100)
    expiry_type: Optional[Literal["日付指定", "期間の定めなし", "条件終了", "記載なし", "要確認"]] = None
    auto_renewal: Literal["あり", "なし", "不明"] = "不明"
    renewal_terms: Optional[str] = Field(default=None, max_length=4000)
    renewal_period_months: Optional[int] = Field(default=None, ge=1, le=600)
    renewal_notice_raw: Optional[str] = Field(default=None, max_length=4000)
    renewal_notice_json: Optional[NoticeStruct] = None
    termination_notice_raw: Optional[str] = Field(default=None, max_length=4000)
    termination_notice_json: Optional[NoticeStruct] = None
    notice_deadline_confirmed: Optional[str] = None
    managed_expiry_date: Optional[str] = None
    managed_expiry_note: Optional[str] = Field(default=None, max_length=1000)
    next_review_date: Optional[str] = None
    dept: Optional[str] = Field(default=None, max_length=200)
    owner: Optional[str] = Field(default=None, max_length=200)
    paper_location: Optional[str] = Field(default=None, max_length=500)
    notes: Optional[str] = Field(default=None, max_length=8000)
    review_status: Literal["未確認", "要確認", "確認済み"] = "未確認"
    parties: list[PartyIn] = Field(default_factory=list, max_length=30)
    signers: list[SignerIn] = Field(default_factory=list, max_length=50)
    field_states: dict[str, Literal["", "記載なし", "要確認", "読取不能"]] = Field(default_factory=dict)

    @field_validator("contract_date", "start_date", "expiry_date", "notice_deadline_confirmed",
                     "managed_expiry_date", "next_review_date", mode="before")
    @classmethod
    def _dates(cls, v):
        return _date_or_none(v)

    @field_validator("title", "doc_type", "summary", "term_raw", "renewal_terms", "renewal_notice_raw",
                     "termination_notice_raw", "dept", "owner", "paper_location", "notes",
                     "contract_date_raw", "start_date_raw", "expiry_raw", "managed_expiry_note", mode="before")
    @classmethod
    def _strip(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v


# ---------------------------------------------------------------- 取得

def _row_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in JSON_FIELDS:
        d[k] = json.loads(d[k]) if d.get(k) else None
    d["field_meta"] = json.loads(d.get("field_meta") or "{}")
    return d


def get_contract(conn: sqlite3.Connection, cid: str, with_related: bool = True) -> dict:
    row = conn.execute(
        "SELECT c.*, cat.name AS category_name FROM contracts c LEFT JOIN categories cat ON cat.id=c.category_id"
        " WHERE c.id=?", (cid,)).fetchone()
    if row is None:
        raise RepoError("契約が見つかりません。", status=404)
    d = _row_to_dict(conn, row)
    d["parties"] = [dict(r) for r in conn.execute(
        "SELECT id, name, role, label FROM parties WHERE contract_id=? ORDER BY sort_order", (cid,))]
    pid_index = {p["id"]: i for i, p in enumerate(d["parties"])}
    d["signers"] = []
    for r in conn.execute("SELECT name, title, party_id FROM signers WHERE contract_id=? ORDER BY sort_order", (cid,)):
        s = dict(r)
        s["party_index"] = pid_index.get(s.pop("party_id"))
        d["signers"].append(s)
    if with_related:
        d["relations"] = list_relations(conn, cid)
        d["next_term_candidate"] = DL.next_term_candidate(d)
    return d


# ---------------------------------------------------------------- 採番・保存

def next_mgmt_no(conn: sqlite3.Connection) -> str:
    """管理番号を採番する。採番は即時コミットし、登録が失敗しても番号を再利用しない。"""
    with tx(conn):
        conn.execute("UPDATE counters SET value = value + 1 WHERE name='mgmt_no'")
        n = conn.execute("SELECT value FROM counters WHERE name='mgmt_no'").fetchone()[0]
    return f"C{n:06d}"


def _counterparty(parties: list[PartyIn]) -> str | None:
    for p in parties:
        if p.role == "相手方":
            return p.name
    return parties[0].name if parties else None


def _category(conn, category_id: int | None, require_active: bool = True) -> sqlite3.Row:
    if category_id is None:
        raise RepoError("カテゴリーを選択してください。")
    row = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    if row is None:
        raise RepoError("カテゴリーが見つかりません。")
    if require_active and not row["active"]:
        raise RepoError("無効化されたカテゴリーには新規登録できません。", "別のカテゴリーを選択してください。")
    return row


def _values(data: ContractIn) -> dict:
    v = {}
    for k in SCALAR_FIELDS:
        val = getattr(data, k)
        if k in JSON_FIELDS:
            val = json.dumps(val.model_dump(), ensure_ascii=False) if val and val.value is not None else None
        v[k] = val
    return v


def _with_candidates(v: dict) -> dict:
    c = dict(v)
    for k in JSON_FIELDS:
        c[k] = json.loads(c[k]) if c.get(k) else None
    nc = DL.notice_candidate(c)
    v["notice_deadline_candidate"] = nc["date"]
    v["notice_deadline_calc"] = nc["calc"]
    return v


def build_field_meta(data: ContractIn, extraction: dict | None, categories_by_name: dict) -> dict:
    """抽出値と確定値を比較し、項目ごとの状態を作る。"""
    meta = {}
    ext_fields = (extraction or {}).get("fields", {})
    keymap = {"category_id": "category", "renewal_notice_raw": "renewal_notice",
              "termination_notice_raw": "termination_notice"}
    for f in META_FIELDS:
        user_state = data.field_states.get(f, "")
        final = getattr(data, f) if f not in ("parties", "signers") else [p.model_dump() for p in getattr(data, f)]
        m: dict = {"state": "", "edited": False}
        if f in ("parties", "signers"):
            ex = (extraction or {}).get(f)
            if ex is not None:
                ex_names = [norm(p["name"]) for p in ex]
                fin_names = [norm(p["name"]) for p in final]
                m["extracted"] = [p["name"] for p in ex]
                m["edited"] = ex_names != fin_names
                st = (extraction or {}).get(f + "_state", {})
                m["state"] = "修正済み" if m["edited"] else ("要確認" if st.get("state") == "要確認" else "未確認")
                m["note"] = st.get("note", "")
                m["evidence"] = [{"page": p.get("page"), "evidence": p.get("evidence"), "source": p.get("source")}
                                 for p in ex][:10]
            else:
                m["state"] = "手入力" if final else ""
        else:
            e = ext_fields.get(keymap.get(f, f))
            if e is not None:
                ev = e.get("value")
                if f == "category_id":
                    ev = categories_by_name.get(ev)
                m.update({"extracted": ev, "page": e.get("page"), "evidence": e.get("evidence"),
                          "source": e.get("source"), "method": e.get("method"), "note": e.get("note", ""),
                          "alternatives": e.get("alternatives")})
                m["edited"] = (str(ev) if ev not in (None, "") else "") != (str(final) if final not in (None, "") else "")
                if m["edited"]:
                    m["state"] = "修正済み"
                elif e.get("state") in ("記載なし", "要確認", "読取不能"):
                    m["state"] = e["state"]
                else:
                    m["state"] = "未確認"
            else:
                m["state"] = "手入力" if final not in (None, "") else ""
        if user_state:
            m["state"] = user_state
        meta[f] = m
    return meta


def _write_parties(conn, cid: str, data: ContractIn) -> None:
    conn.execute("DELETE FROM signers WHERE contract_id=?", (cid,))
    conn.execute("DELETE FROM parties WHERE contract_id=?", (cid,))
    ids = []
    for i, p in enumerate(data.parties):
        cur = conn.execute("INSERT INTO parties(contract_id, name, role, label, sort_order) VALUES (?,?,?,?,?)",
                           (cid, p.name.strip(), p.role, p.label, i))
        ids.append(cur.lastrowid)
    for i, s in enumerate(data.signers):
        pid = ids[s.party_index] if s.party_index is not None and 0 <= s.party_index < len(ids) else None
        conn.execute("INSERT INTO signers(contract_id, party_id, name, title, sort_order) VALUES (?,?,?,?,?)",
                     (cid, pid, s.name.strip(), s.title, i))


def add_history(conn, cid: str, action: str, changes: dict | None = None) -> None:
    conn.execute("INSERT INTO history(contract_id, at, action, changes) VALUES (?,?,?,?)",
                 (cid, now_iso(), action, json.dumps(changes or {}, ensure_ascii=False, default=str)))


def categories_by_name(conn) -> dict:
    return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")}


def register_contract(conn: sqlite3.Connection, data: ContractIn, *, pdf_src: Path | None = None,
                      original_filename: str | None = None, file_hash: str | None = None,
                      page_count: int | None = None, import_id: str | None = None,
                      extraction: dict | None = None, fail_after: str | None = None) -> dict:
    """契約を登録する。PDFがあれば管理フォルダへコピーする（ジャーナル付き）。"""
    cat = _category(conn, data.category_id)
    if file_hash:
        dup = conn.execute("SELECT mgmt_no, deleted_at FROM contracts WHERE file_hash=?", (file_hash,)).fetchone()
        if dup:
            raise RepoError(
                f"同じPDFが既に管理番号 {dup['mgmt_no']} で登録されています" + ("（削除済み）" if dup["deleted_at"] else "") + "。",
                "既存の台帳を確認してください。削除済みの場合は復元できます。", status=409,
                data={"mgmt_no": dup["mgmt_no"]})
    mgmt_no = next_mgmt_no(conn)
    cid = str(uuid.uuid4())
    root_id = rel = None
    op = None
    dst = None
    if pdf_src is not None:
        root_id, root = S.default_root(conn)
        S.check_free_space(root, pdf_src.stat().st_size)
        folder = S.sanitize_component(cat["folder_name"])
        name = S.build_pdf_filename(mgmt_no, data.title, _counterparty(data.parties))
        rel = S.unique_relpath(root, S.fit_path(root, folder, name))
        dst = S.safe_join(root, *rel.split("/"))
        with tx(conn):
            op = S.journal_start(conn, "register", contract_id=cid, import_id=import_id,
                                 dst_root_id=root_id, dst_rel=rel, src_abs=str(pdf_src), file_hash=file_hash)
        try:
            S.copy_verified(pdf_src, dst, file_hash)
            with tx(conn):
                S.journal_set(conn, op, "file_done")
            if fail_after == "copy":
                raise OSError("故障注入: copy後")
        except BaseException as e:
            _rollback_register(conn, op, root_id, rel, file_hash, e)
            raise
    v = _with_candidates(_values(data))
    ts = now_iso()
    meta = build_field_meta(data, extraction, categories_by_name(conn))
    try:
        with tx(conn):
            cols = ["id", "mgmt_no", *v.keys(), "storage_root_id", "pdf_relpath", "original_filename", "file_hash",
                    "file_size", "page_count", "review_status", "field_meta", "import_id",
                    "created_at", "updated_at", "reviewed_at"]
            vals = [cid, mgmt_no, *v.values(), root_id, rel, original_filename, file_hash,
                    pdf_src.stat().st_size if pdf_src else None, page_count, data.review_status,
                    json.dumps(meta, ensure_ascii=False), import_id, ts, ts,
                    ts if data.review_status == "確認済み" else None]
            conn.execute(f"INSERT INTO contracts({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            _write_parties(conn, cid, data)
            add_history(conn, cid, "登録", {"mgmt_no": mgmt_no, "pdf": rel})
            if op is not None:
                S.journal_set(conn, op, "completed")
            if fail_after == "db":
                raise sqlite3.OperationalError("故障注入: DB登録中")
    except BaseException as e:
        if op is not None:
            _rollback_register(conn, op, root_id, rel, file_hash, e)
        raise
    log.info("registered contract id=%s mgmt_no=%s", cid, mgmt_no)
    return get_contract(conn, cid)


def _rollback_register(conn, op, root_id, rel, file_hash, err) -> None:
    try:
        S._remove_unreferenced(conn, root_id, rel, file_hash)
        with tx(conn):
            S.journal_set(conn, op, "rolled_back", f"{type(err).__name__}")
    except Exception:
        log.exception("register rollback failed op=%s (起動時に再試行)", op)


def update_contract(conn: sqlite3.Connection, cid: str, data: ContractIn) -> dict:
    before = get_contract(conn, cid, with_related=False)
    if before.get("deleted_at"):
        raise RepoError("削除済みの契約は編集できません。先に復元してください。")
    new_cat = data.category_id
    if new_cat != before["category_id"]:
        _category(conn, new_cat)
    v = _with_candidates(_values(data))
    # 項目状態：抽出時のメタ情報は保持し、値が変わった項目を修正済みにする
    meta = before["field_meta"] or {}
    for f in META_FIELDS:
        m = dict(meta.get(f) or {"state": ""})
        if f in ("parties", "signers"):
            old = [norm(p["name"]) for p in before[f]]
            new = [norm(p.name) for p in getattr(data, f)]
            changed = old != new
        else:
            changed = (before.get(f) or None) != (getattr(data, f) or None)
        if changed:
            m["state"] = "修正済み"
            m["edited"] = True
        us = data.field_states.get(f)
        if us is not None and us != "":
            m["state"] = us
        elif us == "" and m.get("state") in ("記載なし", "要確認", "読取不能") and f in data.field_states:
            m["state"] = "修正済み" if m.get("edited") else "未確認"
        meta[f] = m
    changes = {}
    for k in list(v.keys()):
        old = before.get(k)
        new = v[k]
        if k in JSON_FIELDS:
            old = json.dumps(old, ensure_ascii=False) if old else None
        if (old or None) != (new or None):
            changes[k] = {"old": old, "new": new}
    for f in ("parties", "signers"):
        old = [{kk: p.get(kk) for kk in ("name", "role", "label", "title", "party_index") if kk in p} for p in before[f]]
        new = [p.model_dump(exclude_none=True) for p in getattr(data, f)]
        old_c = [{k: x for k, x in o.items() if x is not None} for o in old]
        if old_c != new:
            changes[f] = {"old": old, "new": new}
    status_changed = data.review_status != before["review_status"]
    if status_changed:
        changes["review_status"] = {"old": before["review_status"], "new": data.review_status}
    with tx(conn):
        sets = ", ".join(f"{k}=?" for k in v.keys())
        conn.execute(f"UPDATE contracts SET {sets}, review_status=?, field_meta=?, updated_at=?"
                     + (", reviewed_at=?" if status_changed and data.review_status == "確認済み" else "")
                     + " WHERE id=?",
                     [*v.values(), data.review_status, json.dumps(meta, ensure_ascii=False), now_iso(),
                      *([now_iso()] if status_changed and data.review_status == "確認済み" else []), cid])
        _write_parties(conn, cid, data)
        if changes:
            add_history(conn, cid, "修正", changes)
    if new_cat != before["category_id"] and before.get("pdf_relpath"):
        move_to_category(conn, cid)
    return get_contract(conn, cid)


def move_to_category(conn, cid: str) -> str | None:
    """契約のPDFを、現在のカテゴリーのフォルダへ移動する（ファイル名は固定）。"""
    c = conn.execute("SELECT c.storage_root_id, c.pdf_relpath, cat.folder_name FROM contracts c "
                     "JOIN categories cat ON cat.id=c.category_id WHERE c.id=?", (cid,)).fetchone()
    if not c or not c["pdf_relpath"]:
        return None
    folder = S.sanitize_component(c["folder_name"])
    cur_folder, fname = c["pdf_relpath"].rsplit("/", 1)
    if cur_folder == folder:
        return c["pdf_relpath"]
    old = c["pdf_relpath"]
    new = S.move_contract_pdf(conn, cid, c["storage_root_id"], f"{folder}/{fname}")
    with tx(conn):
        add_history(conn, cid, "PDF移動", {"pdf": {"old": old, "new": new}})
    return new


def set_review(conn, cid: str, status: str) -> dict:
    if status not in REVIEW_STATES:
        raise RepoError("確認状態が不正です。")
    before = get_contract(conn, cid, with_related=False)
    with tx(conn):
        conn.execute("UPDATE contracts SET review_status=?, updated_at=?, reviewed_at=CASE WHEN ?='確認済み' THEN ? ELSE reviewed_at END WHERE id=?",
                     (status, now_iso(), status, now_iso(), cid))
        add_history(conn, cid, "確認状態変更", {"review_status": {"old": before["review_status"], "new": status}})
    return get_contract(conn, cid)


def soft_delete(conn, cid: str, restore: bool = False) -> dict:
    get_contract(conn, cid, with_related=False)
    with tx(conn):
        conn.execute("UPDATE contracts SET deleted_at=?, updated_at=? WHERE id=?",
                     (None if restore else now_iso(), now_iso(), cid))
        add_history(conn, cid, "復元" if restore else "削除（論理）")
    return get_contract(conn, cid)


def history(conn, cid: str) -> list[dict]:
    out = []
    for r in conn.execute("SELECT at, action, changes FROM history WHERE contract_id=? ORDER BY id DESC", (cid,)):
        ch = json.loads(r["changes"])
        out.append({"at": r["at"], "action": r["action"],
                    "changes": [{"field": k, "label": LABELS_JA.get(k, k),
                                 "old": v.get("old") if isinstance(v, dict) else None,
                                 "new": v.get("new") if isinstance(v, dict) else v}
                                for k, v in ch.items()]})
    return out


# ---------------------------------------------------------------- 関連契約

def add_relation(conn, src: str, dst: str, rel_type: str, note: str | None) -> None:
    if src == dst:
        raise RepoError("同じ契約同士は関連付けできません。")
    if rel_type not in RELATION_TYPES:
        raise RepoError("関連の種類が不正です。")
    get_contract(conn, src, False)
    get_contract(conn, dst, False)
    try:
        with tx(conn):
            conn.execute("INSERT INTO relations(src_id, dst_id, rel_type, note, created_at) VALUES (?,?,?,?,?)",
                         (src, dst, rel_type, note, now_iso()))
            mg = conn.execute("SELECT mgmt_no FROM contracts WHERE id=?", (dst,)).fetchone()[0]
            ms = conn.execute("SELECT mgmt_no FROM contracts WHERE id=?", (src,)).fetchone()[0]
            add_history(conn, src, "関連付け", {"relation": {"new": f"{rel_type}: {mg}"}})
            add_history(conn, dst, "関連付け", {"relation": {"new": f"{rel_type}: {ms}"}})
    except sqlite3.IntegrityError:
        raise RepoError("既に同じ関連が登録されています。")


def delete_relation(conn, rid: int) -> None:
    r = conn.execute("SELECT * FROM relations WHERE id=?", (rid,)).fetchone()
    if not r:
        raise RepoError("関連が見つかりません。", status=404)
    with tx(conn):
        conn.execute("DELETE FROM relations WHERE id=?", (rid,))
        add_history(conn, r["src_id"], "関連解除", {"relation": {"old": r["rel_type"]}})
        add_history(conn, r["dst_id"], "関連解除", {"relation": {"old": r["rel_type"]}})


def list_relations(conn, cid: str) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT r.id, r.rel_type, r.note, r.src_id, r.dst_id, s.mgmt_no AS s_no, s.title AS s_title, "
        "d.mgmt_no AS d_no, d.title AS d_title, s.expiry_date AS s_exp, d.expiry_date AS d_exp "
        "FROM relations r JOIN contracts s ON s.id=r.src_id JOIN contracts d ON d.id=r.dst_id "
        "WHERE r.src_id=? OR r.dst_id=?", (cid, cid)):
        is_src = r["src_id"] == cid
        out.append({"id": r["id"], "rel_type": r["rel_type"], "note": r["note"],
                    "direction": "この契約→相手" if is_src else "相手→この契約",
                    "other_id": r["dst_id"] if is_src else r["src_id"],
                    "other_mgmt_no": r["d_no"] if is_src else r["s_no"],
                    "other_title": r["d_title"] if is_src else r["s_title"],
                    "other_expiry": r["d_exp"] if is_src else r["s_exp"]})
    return out


# ---------------------------------------------------------------- 重複候補

def duplicate_candidates(conn, title: str | None, party_names: list[str], contract_date: str | None,
                         exclude_id: str | None = None) -> list[dict]:
    """再スキャン等で内容が同じ可能性がある契約。自動で同一扱いはしない。"""
    nt = norm(title)
    np_ = {norm(n) for n in party_names if n}
    out = []
    rows = conn.execute("SELECT id, mgmt_no, title, contract_date, deleted_at FROM contracts").fetchall()
    for r in rows:
        if r["id"] == exclude_id:
            continue
        reasons = []
        score = 0
        if nt and r["title"]:
            ratio = difflib.SequenceMatcher(None, nt, norm(r["title"])).ratio()
            if ratio >= 0.85:
                reasons.append("契約書名が類似")
                score += 1
        if contract_date and r["contract_date"] == contract_date:
            reasons.append("契約日が同じ")
            score += 1
        names = {norm(p["name"]) for p in conn.execute("SELECT name FROM parties WHERE contract_id=?", (r["id"],))}
        if np_ and names and len(np_ & names) >= min(2, len(np_)):
            reasons.append("当事者が共通")
            score += 1
        if score >= 2:
            out.append({"id": r["id"], "mgmt_no": r["mgmt_no"], "title": r["title"],
                        "contract_date": r["contract_date"], "deleted": bool(r["deleted_at"]),
                        "reasons": reasons})
    return out


# ---------------------------------------------------------------- 検索

SORTABLE = {"mgmt_no": "c.mgmt_no", "title": "c.title", "contract_date": "c.contract_date",
            "expiry_date": "COALESCE(c.managed_expiry_date, c.expiry_date)",
            "notice": "COALESCE(c.notice_deadline_confirmed, c.notice_deadline_candidate)",
            "updated_at": "c.updated_at", "category": "cat.sort_order", "next_review_date": "c.next_review_date"}
DATE_FILTERS = {"contract_date": "c.contract_date", "start_date": "c.start_date",
                "expiry_date": "COALESCE(c.managed_expiry_date, c.expiry_date)",
                "notice": "COALESCE(c.notice_deadline_confirmed, c.notice_deadline_candidate)",
                "next_review_date": "c.next_review_date", "created_at": "substr(c.created_at,1,10)"}


def search(conn, q: dict, *, paged: bool = True) -> dict:
    where, args = [], []
    deleted = q.get("deleted") or "exclude"
    if deleted == "exclude":
        where.append("c.deleted_at IS NULL")
    elif deleted == "only":
        where.append("c.deleted_at IS NOT NULL")
    if q.get("q"):
        for word in str(q["q"]).split():
            like = f"%{word}%"
            where.append("(c.title LIKE ? OR c.summary LIKE ? OR c.mgmt_no LIKE ? OR c.notes LIKE ? OR "
                         "EXISTS (SELECT 1 FROM parties p WHERE p.contract_id=c.id AND p.name LIKE ?))")
            args += [like] * 5
    for key, col in (("category_id", "c.category_id"), ("doc_type", "c.doc_type"),
                     ("review_status", "c.review_status"), ("auto_renewal", "c.auto_renewal"),
                     ("expiry_type", "c.expiry_type")):
        if q.get(key) not in (None, ""):
            where.append(f"{col} = ?")
            args.append(q[key])
    if q.get("owner"):
        where.append("(c.owner LIKE ? OR c.dept LIKE ?)")
        args += [f"%{q['owner']}%"] * 2
    df = q.get("date_field")
    if df in DATE_FILTERS:
        col = DATE_FILTERS[df]
        if q.get("date_from"):
            where.append(f"{col} >= ?")
            args.append(q["date_from"])
        if q.get("date_to"):
            where.append(f"{col} <= ?")
            args.append(q["date_to"])
        if q.get("date_empty") == "1":
            where.append(f"{col} IS NULL")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    base = f"FROM contracts c LEFT JOIN categories cat ON cat.id=c.category_id{w}"
    total = conn.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
    sort = SORTABLE.get(q.get("sort") or "mgmt_no", "c.mgmt_no")
    order = "ASC" if (q.get("order") or "desc").lower() == "asc" else "DESC"
    sql = f"SELECT c.*, cat.name AS category_name {base} ORDER BY {sort} IS NULL, {sort} {order}, c.mgmt_no {order}"
    page = max(1, int(q.get("page") or 1))
    per = min(200, max(1, int(q.get("per_page") or 50)))
    if paged:
        sql += f" LIMIT {per} OFFSET {(page - 1) * per}"
    items = []
    for r in conn.execute(sql, args):
        d = _row_to_dict(conn, r)
        d["parties"] = [dict(p) for p in conn.execute(
            "SELECT name, role, label FROM parties WHERE contract_id=? ORDER BY sort_order", (d["id"],))]
        items.append(d)
    return {"total": total, "page": page, "per_page": per, "items": items}


def deadline_board(conn, today: date, days_expiry: int, days_notice: int) -> dict:
    buckets = {k: [] for k in ("期限経過", "期限が近い", "通知期限経過", "通知期限が近い", "日付未確定", "要確認", "確認予定日")}
    for r in conn.execute("SELECT c.*, cat.name AS category_name FROM contracts c LEFT JOIN categories cat "
                          "ON cat.id=c.category_id WHERE c.deleted_at IS NULL"):
        d = _row_to_dict(conn, r)
        for b in DL.classify(d, today, days_expiry, days_notice):
            exp, _ = DL.effective_expiry(d)
            nd, conf = DL.effective_notice(d)
            buckets[b].append({"id": d["id"], "mgmt_no": d["mgmt_no"], "title": d["title"],
                               "category_name": d["category_name"], "expiry": exp.isoformat() if exp else None,
                               "expiry_type": d["expiry_type"], "auto_renewal": d["auto_renewal"],
                               "notice": nd.isoformat() if nd else None, "notice_confirmed": conf,
                               "review_status": d["review_status"], "next_review_date": d["next_review_date"]})
    for k in buckets:
        buckets[k].sort(key=lambda x: (x["expiry"] or x["notice"] or "9999"))
    return buckets
