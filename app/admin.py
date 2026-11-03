"""カテゴリー・保存ルートの管理。"""
from __future__ import annotations

import logging
from pathlib import Path

from . import repo as R
from . import storage as S
from .db import now_iso, tx

log = logging.getLogger("ledger.admin")


def list_categories(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT c.*, (SELECT COUNT(*) FROM contracts x WHERE x.category_id=c.id) AS n "
                          "FROM categories c ORDER BY sort_order, id"):
        out.append(dict(r))
    return out


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise R.RepoError("カテゴリー名を入力してください。")
    if len(name) > 40:
        raise R.RepoError("カテゴリー名は40文字以内にしてください。")
    folder = S.sanitize_component(name, max_len=40)
    if folder != name:
        raise R.RepoError("カテゴリー名にフォルダ名として使えない文字（\\ / : * ? \" < > | など）や予約名が含まれています。",
                          f"例えば「{folder}」のように変更してください。")
    return name


def create_category(conn, name: str, keywords: str = "") -> dict:
    name = _validate_name(name)
    if conn.execute("SELECT 1 FROM categories WHERE name=? OR folder_name=?", (name, name)).fetchone():
        raise R.RepoError("同じ名前のカテゴリーが既にあります。")
    ts = now_iso()
    order = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM categories").fetchone()[0]
    with tx(conn):
        cur = conn.execute("INSERT INTO categories(name, folder_name, keywords, active, sort_order, created_at, updated_at)"
                           " VALUES (?,?,?,1,?,?,?)", (name, name, keywords or "", order, ts, ts))
    return dict(conn.execute("SELECT * FROM categories WHERE id=?", (cur.lastrowid,)).fetchone())


def rename_preview(conn, cat_id: int, new_name: str) -> dict:
    new_name = _validate_name(new_name)
    cat = conn.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
    if not cat:
        raise R.RepoError("カテゴリーが見つかりません。", status=404)
    other = conn.execute("SELECT 1 FROM categories WHERE (name=? OR folder_name=?) AND id<>?",
                         (new_name, new_name, cat_id)).fetchone()
    if other:
        raise R.RepoError("同じ名前のカテゴリーが既にあります。")
    affected = [dict(r) for r in conn.execute(
        "SELECT id, mgmt_no, title, pdf_relpath FROM contracts WHERE category_id=? AND pdf_relpath IS NOT NULL", (cat_id,))]
    return {"old_name": cat["name"], "new_name": new_name, "old_folder": cat["folder_name"], "new_folder": new_name,
            "affected": affected,
            "message": f"{len(affected)}件のPDFをフォルダ「{cat['folder_name']}」から「{new_name}」へ移動します。"}


def rename_category(conn, cat_id: int, new_name: str, keywords: str | None = None) -> dict:
    """名称変更とフォルダ移動。移動に失敗した契約は旧フォルダのまま（参照は有効）で一覧を返す。"""
    pv = rename_preview(conn, cat_id, new_name)
    with tx(conn):
        conn.execute("UPDATE categories SET name=?, folder_name=?, keywords=COALESCE(?, keywords), updated_at=? WHERE id=?",
                     (pv["new_name"], pv["new_name"], keywords, now_iso(), cat_id))
    failed = sync_category_folders(conn, cat_id)
    return {"renamed": True, "moved": len(pv["affected"]) - len(failed), "failed": failed}


def sync_category_folders(conn, cat_id: int | None = None) -> list[dict]:
    """契約PDFをカテゴリーのフォルダへ揃える（名称変更の途中失敗からの再実行にも使う）。"""
    failed = []
    sql = "SELECT id, mgmt_no FROM contracts WHERE pdf_relpath IS NOT NULL" + (" AND category_id=?" if cat_id else "")
    for r in conn.execute(sql, (cat_id,) if cat_id else ()).fetchall():
        try:
            R.move_to_category(conn, r["id"])
        except Exception as e:
            log.warning("folder sync failed contract=%s err=%s", r["id"], type(e).__name__)
            failed.append({"mgmt_no": r["mgmt_no"], "error": str(e)})
    return failed


def update_category(conn, cat_id: int, *, active: bool | None = None, keywords: str | None = None,
                    sort_order: int | None = None) -> None:
    with tx(conn):
        if active is not None:
            conn.execute("UPDATE categories SET active=?, updated_at=? WHERE id=?", (1 if active else 0, now_iso(), cat_id))
        if keywords is not None:
            conn.execute("UPDATE categories SET keywords=?, updated_at=? WHERE id=?", (keywords, now_iso(), cat_id))
        if sort_order is not None:
            conn.execute("UPDATE categories SET sort_order=?, updated_at=? WHERE id=?", (sort_order, now_iso(), cat_id))


# ---------------------------------------------------------------- 保存ルート

def list_roots(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM contracts c WHERE c.storage_root_id=r.id) AS n FROM storage_roots r ORDER BY id")]


def _check_root_path(path: str) -> Path:
    p = Path(path or "").expanduser()
    if not p.is_absolute():
        raise R.RepoError("保存ルートは絶対パス（例: D:\\契約書）で指定してください。")
    if len(str(p)) > 150:
        raise R.RepoError("保存ルートのパスが長すぎます（150文字以内）。")
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        raise R.RepoError("指定したフォルダに書き込めません。", "フォルダの場所と権限を確認してください。")
    return p.resolve()


def set_default_root(conn, path: str, mode: str) -> dict:
    """mode=new_only: 新規保存先のみ変更（既存PDFは元の場所のまま参照）
       mode=migrate : 既存PDFも新しい保存ルートへ移行（コピー→検証→台帳更新→旧ファイル削除）"""
    p = _check_root_path(path)
    row = conn.execute("SELECT id FROM storage_roots WHERE path=?", (str(p),)).fetchone()
    with tx(conn):
        if row:
            rid = row["id"]
        else:
            rid = conn.execute("INSERT INTO storage_roots(path, is_default, created_at) VALUES (?,0,?)",
                               (str(p), now_iso())).lastrowid
        conn.execute("UPDATE storage_roots SET is_default = CASE WHEN id=? THEN 1 ELSE 0 END", (rid,))
    result = {"root_id": rid, "path": str(p), "mode": mode, "migrated": 0, "failed": []}
    if mode == "migrate":
        rows = conn.execute("SELECT id, mgmt_no, pdf_relpath FROM contracts WHERE pdf_relpath IS NOT NULL "
                            "AND storage_root_id<>?", (rid,)).fetchall()
        for r in rows:
            try:
                S.move_contract_pdf(conn, r["id"], rid, r["pdf_relpath"])
                with tx(conn):
                    R.add_history(conn, r["id"], "保存先移行", {"pdf": {"new": f"{p}/{r['pdf_relpath']}"}})
                result["migrated"] += 1
            except Exception as e:
                result["failed"].append({"mgmt_no": r["mgmt_no"], "error": str(e)})
    return result
