"""PDFの保存・移動と、途中失敗からの復旧。

SQLiteの更新とファイル操作は単一の原子的処理にできないため、file_ops テーブルに
処理状態（ジャーナル）を記録し、起動時に recover() で整合させる。

■ 登録（op=register）
  pending → [一時ファイル .part へコピー → ハッシュ検証 → 本名へ rename（上書きしない）]
  → file_done → [DBに契約登録 + completed を同一トランザクションで記録]
  復旧: pending/file_done のまま残っていれば、保存先ファイル（未参照のもの）を削除して rolled_back。
        取込み一時ファイルは残るので、利用者は再度登録できる。

■ 移動（op=move：カテゴリー変更・保存先移行）
  pending → [コピー → 検証 → rename] → file_done → [DB更新 + db_done を同一トランザクション]
  → [移動元を削除] → completed
  復旧: pending/file_done → 移動先（DB未参照）を削除して rolled_back（DBは移動元を指したまま）。
        db_done → 移動元が残っていれば削除して completed。

元PDF（利用者が選択したファイル）はどの段階でも変更・削除しない。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sqlite3
import unicodedata
from pathlib import Path

from .db import now_iso, tx

log = logging.getLogger("ledger.storage")

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_PATH = 240           # Windows の既定 MAX_PATH(260) に余裕を持たせる
MAX_NAME_PART = 60       # 契約書名・相手方名それぞれの最大文字数


class StorageError(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sanitize_component(name: str, max_len: int = MAX_NAME_PART, fallback: str = "名称未設定") -> str:
    """ファイル名・フォルダ名の1要素として安全な文字列にする。"""
    s = unicodedata.normalize("NFC", name or "")
    s = INVALID_CHARS.sub("_", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(". ")  # 末尾のドット・空白は Windows で問題になる
    if s in ("", ".", ".."):
        s = fallback
    if len(s) > max_len:
        s = s[:max_len].rstrip(". ")
    base = s.split(".")[0].upper()
    if base in RESERVED:
        s = "_" + s
    return s


def build_pdf_filename(mgmt_no: str, title: str | None, counterparty: str | None) -> str:
    parts = [mgmt_no, sanitize_component(title or "", fallback="契約書")]
    if counterparty:
        parts.append(sanitize_component(counterparty))
    return "_".join(parts) + ".pdf"


def safe_join(root: Path, *parts: str) -> Path:
    """root 配下であることを保証して結合する。外に出る場合は例外。"""
    root_r = root.resolve()
    p = root_r.joinpath(*parts).resolve()
    try:
        p.relative_to(root_r)
    except ValueError:
        raise StorageError("管理ルート外へのファイル操作は許可されていません。")
    if p == root_r:
        raise StorageError("不正なファイルパスです。")
    return p


def fit_path(root: Path, folder: str, filename: str) -> str:
    """パス長の上限に収まるよう、ファイル名を短縮した相対パスを返す。"""
    rel = f"{folder}/{filename}"
    full = len(str(root.resolve())) + 1 + len(rel)
    if full <= MAX_PATH:
        return rel
    stem, ext = os.path.splitext(filename)
    over = full - MAX_PATH
    head = stem.split("_", 1)[0]  # 管理番号は必ず残す
    keep = max(len(head), len(stem) - over)
    stem = stem[:keep].rstrip("_ .")
    rel = f"{folder}/{stem}{ext}"
    if len(str(root.resolve())) + 1 + len(rel) > MAX_PATH:
        raise StorageError(
            "保存先のパスが長すぎます。",
            "設定画面で、より短いパスの保存ルートを指定してください。",
        )
    return rel


def unique_relpath(root: Path, rel: str) -> str:
    """同名ファイルがあれば _2, _3 … を付ける（上書きしない）。"""
    p = safe_join(root, *rel.split("/"))
    if not p.exists():
        return rel
    folder, name = rel.rsplit("/", 1)
    stem, ext = os.path.splitext(name)
    for i in range(2, 1000):
        cand = f"{folder}/{stem}_{i}{ext}"
        if not safe_join(root, *cand.split("/")).exists():
            return cand
    raise StorageError("同名ファイルが多すぎるため保存できません。")


def check_free_space(directory: Path, needed: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(directory).free
    margin = 200 * 1024 * 1024
    if free < needed + margin:
        raise StorageError(
            "ディスク容量が不足しています。",
            f"保存先ドライブの空き容量を確保してから再実行してください（必要量の目安: {(needed + margin) // (1024 * 1024)} MB）。",
        )


def copy_verified(src: Path, dst: Path, expected_hash: str | None) -> str:
    """dst.part にコピー → ハッシュ検証 → dst へ rename（既存なら失敗）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise StorageError("保存先に同名のファイルが既に存在します（上書きしません）。")
    part = dst.with_name(dst.name + ".part")
    try:
        shutil.copyfile(src, part)
        h = sha256_file(part)
        if expected_hash and h != expected_hash:
            raise StorageError("コピーしたファイルの内容が一致しません。", "再度実行してください。")
        _rename_no_overwrite(part, dst)
        return h
    except BaseException:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        raise


def _rename_no_overwrite(src: Path, dst: Path) -> None:
    if os.name == "nt":
        os.rename(src, dst)  # Windows では既存ファイルがあると FileExistsError
    else:
        os.link(src, dst)  # 既存ならエラー（上書きしない）
        os.unlink(src)


# ---------------------------------------------------------------- journal

def root_path(conn: sqlite3.Connection, root_id: int) -> Path:
    row = conn.execute("SELECT path FROM storage_roots WHERE id=?", (root_id,)).fetchone()
    if row is None:
        raise StorageError("保存ルートが見つかりません。")
    return Path(row["path"])


def default_root(conn: sqlite3.Connection) -> tuple[int, Path]:
    row = conn.execute("SELECT id, path FROM storage_roots WHERE is_default=1").fetchone()
    return row["id"], Path(row["path"])


def contract_pdf_abspath(conn: sqlite3.Connection, root_id: int | None, rel: str | None) -> Path | None:
    if root_id is None or not rel:
        return None
    return safe_join(root_path(conn, root_id), *rel.split("/"))


def journal_start(conn, op: str, *, contract_id=None, import_id=None, src_root_id=None, src_rel=None,
                  dst_root_id=None, dst_rel=None, src_abs=None, file_hash=None) -> int:
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO file_ops(op, contract_id, import_id, src_root_id, src_rel, dst_root_id, dst_rel, src_abs,"
        " file_hash, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (op, contract_id, import_id, src_root_id, src_rel, dst_root_id, dst_rel, src_abs, file_hash, "pending", ts, ts),
    )
    return cur.lastrowid


def journal_set(conn, op_id: int, state: str, error: str | None = None) -> None:
    conn.execute("UPDATE file_ops SET state=?, error=?, updated_at=? WHERE id=?", (state, error, now_iso(), op_id))


def _is_referenced(conn, root_id, rel) -> bool:
    return conn.execute(
        "SELECT 1 FROM contracts WHERE storage_root_id=? AND pdf_relpath=?", (root_id, rel)
    ).fetchone() is not None


def _remove_unreferenced(conn, root_id, rel, expected_hash) -> None:
    if root_id is None or not rel:
        return
    p = safe_join(root_path(conn, root_id), *rel.split("/"))
    part = p.with_name(p.name + ".part")
    if part.exists():
        part.unlink()
    if p.exists() and not _is_referenced(conn, root_id, rel):
        # 自分が作ったファイル（ハッシュ一致）だけを削除する
        if expected_hash is None or sha256_file(p) == expected_hash:
            p.unlink()


def move_contract_pdf(conn, contract_id: str, dst_root_id: int, dst_rel: str, *, fail_after: str | None = None) -> str:
    """契約PDFを移動し、DBを更新する。fail_after はテスト用の故障注入点。"""
    row = conn.execute(
        "SELECT storage_root_id, pdf_relpath, file_hash FROM contracts WHERE id=?", (contract_id,)
    ).fetchone()
    src_root_id, src_rel, fhash = row["storage_root_id"], row["pdf_relpath"], row["file_hash"]
    if src_root_id == dst_root_id and src_rel == dst_rel:
        return src_rel
    src = contract_pdf_abspath(conn, src_root_id, src_rel)
    if src is None or not src.exists():
        raise StorageError("移動元のPDFが見つかりません。", "設定画面の「整合性チェック」で状態を確認してください。")
    dst_root = root_path(conn, dst_root_id)
    dst_rel = unique_relpath(dst_root, dst_rel)
    dst = safe_join(dst_root, *dst_rel.split("/"))
    check_free_space(dst_root, src.stat().st_size)
    with tx(conn):
        op = journal_start(conn, "move", contract_id=contract_id, src_root_id=src_root_id, src_rel=src_rel,
                           dst_root_id=dst_root_id, dst_rel=dst_rel, file_hash=fhash)
    try:
        copy_verified(src, dst, fhash)
        if fail_after == "copy":
            raise OSError("故障注入: copy後")
        with tx(conn):
            journal_set(conn, op, "file_done")
        with tx(conn):
            conn.execute(
                "UPDATE contracts SET storage_root_id=?, pdf_relpath=?, updated_at=? WHERE id=?",
                (dst_root_id, dst_rel, now_iso(), contract_id),
            )
            journal_set(conn, op, "db_done")
        if fail_after == "db":
            raise OSError("故障注入: DB更新後")
    except BaseException as e:
        # DB更新前なら移動先を消して巻き戻す。DB更新後なら残処理（移動元削除）は recover で行う
        state = conn.execute("SELECT state FROM file_ops WHERE id=?", (op,)).fetchone()["state"]
        if state in ("pending", "file_done"):
            try:
                _remove_unreferenced(conn, dst_root_id, dst_rel, fhash)
                with tx(conn):
                    journal_set(conn, op, "rolled_back", str(e)[:200])
            except Exception:
                log.exception("rollback failed op=%s", op)
        raise
    try:
        src.unlink()
        _cleanup_empty_dir(src.parent, root_path(conn, src_root_id))
    except OSError as e:
        log.warning("move: source delete failed op=%s err=%s", op, type(e).__name__)
        with tx(conn):
            journal_set(conn, op, "db_done", "移動元の削除に失敗（次回起動時に再試行）")
        return dst_rel
    with tx(conn):
        journal_set(conn, op, "completed")
    return dst_rel


def _cleanup_empty_dir(d: Path, root: Path) -> None:
    try:
        if d.resolve() != root.resolve() and d.exists() and not any(d.iterdir()):
            d.rmdir()
    except OSError:
        pass


def recover(conn: sqlite3.Connection) -> list[dict]:
    """未完了のファイル操作を整合させる。結果の一覧を返す。"""
    results = []
    rows = conn.execute(
        "SELECT * FROM file_ops WHERE state IN ('pending','file_done','db_done') ORDER BY id"
    ).fetchall()
    for r in rows:
        try:
            if r["op"] == "register":
                if r["contract_id"] and conn.execute(
                    "SELECT 1 FROM contracts WHERE id=?", (r["contract_id"],)
                ).fetchone():
                    with tx(conn):
                        journal_set(conn, r["id"], "completed")
                    results.append({"id": r["id"], "op": "register", "result": "completed"})
                    continue
                _remove_unreferenced(conn, r["dst_root_id"], r["dst_rel"], r["file_hash"])
                with tx(conn):
                    journal_set(conn, r["id"], "rolled_back", "起動時復旧: 登録未完了のため保存先ファイルを削除")
                results.append({"id": r["id"], "op": "register", "result": "rolled_back"})
            elif r["op"] == "move":
                if r["state"] in ("pending", "file_done"):
                    _remove_unreferenced(conn, r["dst_root_id"], r["dst_rel"], r["file_hash"])
                    with tx(conn):
                        journal_set(conn, r["id"], "rolled_back", "起動時復旧: 移動未完了のため移動先を削除")
                    results.append({"id": r["id"], "op": "move", "result": "rolled_back"})
                else:  # db_done
                    src = safe_join(root_path(conn, r["src_root_id"]), *r["src_rel"].split("/"))
                    if src.exists() and not _is_referenced(conn, r["src_root_id"], r["src_rel"]):
                        src.unlink()
                    with tx(conn):
                        journal_set(conn, r["id"], "completed")
                    results.append({"id": r["id"], "op": "move", "result": "completed"})
        except Exception as e:  # 復旧できないものは状態を残して画面に表示する
            log.exception("recover failed op=%s", r["id"])
            with tx(conn):
                conn.execute("UPDATE file_ops SET error=?, updated_at=? WHERE id=?",
                             (f"復旧失敗: {type(e).__name__}", now_iso(), r["id"]))
            results.append({"id": r["id"], "op": r["op"], "result": "failed"})
    return results


def integrity_check(conn: sqlite3.Connection) -> list[dict]:
    """台帳のPDF参照を検証する（存在・ハッシュ）。"""
    problems = []
    for r in conn.execute(
        "SELECT id, mgmt_no, storage_root_id, pdf_relpath, file_hash FROM contracts"
    ).fetchall():
        try:
            p = contract_pdf_abspath(conn, r["storage_root_id"], r["pdf_relpath"])
        except StorageError as e:
            problems.append({"mgmt_no": r["mgmt_no"], "problem": str(e)})
            continue
        if p is None:
            problems.append({"mgmt_no": r["mgmt_no"], "problem": "PDFが登録されていません"})
        elif not p.exists():
            problems.append({"mgmt_no": r["mgmt_no"], "problem": "PDFファイルが見つかりません", "path": r["pdf_relpath"]})
        elif r["file_hash"] and sha256_file(p) != r["file_hash"]:
            problems.append({"mgmt_no": r["mgmt_no"], "problem": "PDFの内容が登録時と異なります", "path": r["pdf_relpath"]})
    return problems
