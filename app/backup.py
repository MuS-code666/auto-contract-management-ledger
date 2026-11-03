"""バックアップと復元。

対象：台帳DB（契約・当事者・履歴・関連・抽出結果/OCR結果・設定・カテゴリー）、管理PDF、
      取込み途中（下書き）の一時PDF。
整合性：
- DBは sqlite3 のバックアップAPIで整合したスナップショットを取る（稼働中ファイルの単純コピーはしない）。
- PDFのコピー中は storage_lock を保持し、登録・移動処理と競合させない。
- ZIP は .part で書いてから改名し、書込み後にハッシュ・DB整合性を検証する。
復元：
- 実行前に対象と影響をプレビュー表示する。
- 実行時はまず現在のデータを自動バックアップして保全する。
- 復元先に内容の異なる同名PDFがあれば、上書きせず .before_restore_<日時>.pdf に改名して残す。
- 復元後、台帳のPDF参照（存在・ハッシュ）を検証して結果を返す。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import VERSION
from . import storage as S
from .db import SCHEMA_VERSION, connect, migrate, snapshot_db

log = logging.getLogger("ledger.backup")


class BackupError(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def _sha(path: Path) -> str:
    return S.sha256_file(path)


def list_backups(ctx) -> list[dict]:
    out = []
    for f in sorted(ctx.paths.backup_dir.glob("backup_*.zip"), reverse=True):
        info = {"name": f.name, "size": f.stat().st_size,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")}
        try:
            with zipfile.ZipFile(f) as z:
                m = json.loads(z.read("manifest.json"))
            info.update({"created_at": m.get("created_at"), "label": m.get("label"),
                         "contracts": m.get("counts", {}).get("contracts"), "pdfs": len(m.get("files", []))})
        except Exception:
            info["error"] = "manifest を読めません"
        out.append(info)
    return out


def create_backup(ctx, label: str = "手動") -> dict:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    stage = ctx.paths.backup_dir / f".stage_{ts}"
    stage.mkdir(parents=True)
    dest = ctx.paths.backup_dir / f"backup_{ts}.zip"
    part = dest.with_suffix(".zip.part")
    try:
        with ctx.storage_lock:
            live = ctx.connect()
            try:
                total = sum(p.stat().st_size for p in [ctx.paths.db_path] if p.exists())
                S.check_free_space(ctx.paths.backup_dir, total * 2)
                snapshot_db(live, stage / "ledger.db")
            finally:
                live.close()
            snap = sqlite3.connect(str(stage / "ledger.db"))
            snap.row_factory = sqlite3.Row
            try:
                roots = {r["id"]: r["path"] for r in snap.execute("SELECT id, path FROM storage_roots")}
                contracts = snap.execute("SELECT mgmt_no, storage_root_id, pdf_relpath, file_hash FROM contracts "
                                         "WHERE pdf_relpath IS NOT NULL").fetchall()
                n_contracts = snap.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
                drafts = snap.execute("SELECT id, tmp_path FROM imports WHERE status NOT IN ('registered','discarded')"
                                      ).fetchall()
            finally:
                snap.close()
            files, missing = [], []
            with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
                z.write(stage / "ledger.db", "ledger.db")
                for c in contracts:
                    src = Path(roots[c["storage_root_id"]]) / c["pdf_relpath"]
                    arc = f"pdfs/{c['storage_root_id']}/{c['pdf_relpath']}"
                    if not src.exists():
                        missing.append(c["mgmt_no"])
                        continue
                    h = _sha(src)
                    z.write(src, arc)
                    files.append({"arc": arc, "root_id": c["storage_root_id"], "rel": c["pdf_relpath"], "sha256": h,
                                  "mgmt_no": c["mgmt_no"], "hash_matches_ledger": h == c["file_hash"]})
                for d in drafts:
                    if d["tmp_path"] and Path(d["tmp_path"]).exists():
                        z.write(d["tmp_path"], f"imports/{d['id']}.pdf")
                manifest = {"app_version": VERSION, "schema_version": SCHEMA_VERSION,
                            "created_at": datetime.now().isoformat(timespec="seconds"), "label": label,
                            "roots": {str(k): v for k, v in roots.items()},
                            "counts": {"contracts": n_contracts, "pdfs": len(files)},
                            "files": files, "missing_pdfs": missing,
                            "db_sha256": _sha(stage / "ledger.db")}
                z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        os.replace(part, dest)
        verify = verify_backup(dest)
        log.info("backup created name=%s contracts=%s pdfs=%s ok=%s", dest.name, n_contracts, len(files), verify["ok"])
        return {"name": dest.name, "manifest": {k: manifest[k] for k in ("created_at", "counts", "missing_pdfs")},
                "verify": verify}
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if part.exists():
            part.unlink()


def _safe_arc(name: str) -> bool:
    p = PurePosixPath(name)
    return not p.is_absolute() and ".." not in p.parts and ":" not in name and not name.startswith(("/", "\\"))


def verify_backup(path: Path) -> dict:
    problems = []
    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
            if bad:
                problems.append(f"ZIP内のファイルが破損しています: {bad}")
            names = set(z.namelist())
            for n in names:
                if not _safe_arc(n):
                    problems.append(f"不正なパスを含みます: {n}")
            m = json.loads(z.read("manifest.json"))
            for f in m.get("files", []):
                if f["arc"] not in names:
                    problems.append(f"PDFがありません: {f['mgmt_no']}")
                    continue
                h = hashlib.sha256(z.read(f["arc"])).hexdigest()
                if h != f["sha256"]:
                    problems.append(f"PDFの内容が一致しません: {f['mgmt_no']}")
            db = z.read("ledger.db")
            if hashlib.sha256(db).hexdigest() != m.get("db_sha256"):
                problems.append("台帳DBの内容が一致しません")
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as e:
        return {"ok": False, "problems": [f"バックアップファイルを読めません（{type(e).__name__}）"]}
    return {"ok": not problems, "problems": problems}


def _resolve_backup(ctx, name: str) -> Path:
    if not name or "/" in name or "\\" in name or not name.startswith("backup_") or not name.endswith(".zip"):
        raise BackupError("バックアップ名が不正です。")
    p = ctx.paths.backup_dir / name
    if not p.exists():
        raise BackupError("バックアップが見つかりません。")
    return p


def restore_preview(ctx, name: str) -> dict:
    p = _resolve_backup(ctx, name)
    v = verify_backup(p)
    with zipfile.ZipFile(p) as z:
        m = json.loads(z.read("manifest.json"))
    conn = ctx.connect()
    try:
        cur_n = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        cur_roots = [r["path"] for r in conn.execute("SELECT path FROM storage_roots")]
    finally:
        conn.close()
    warnings = []
    if m.get("schema_version", 0) > SCHEMA_VERSION:
        warnings.append("このバックアップは新しい版のアプリで作成されたため復元できません。")
    for rid, path in m.get("roots", {}).items():
        drive = Path(path).anchor
        if drive and not Path(drive).exists():
            warnings.append(f"保存ルート {path} のドライブがありません。復元先フォルダを指定してください。")
    return {"name": name, "verify": v, "backup": {"created_at": m.get("created_at"), "label": m.get("label"),
                                                  "app_version": m.get("app_version"), "counts": m.get("counts"),
                                                  "roots": m.get("roots"), "missing_pdfs": m.get("missing_pdfs")},
            "current": {"contracts": cur_n, "roots": cur_roots},
            "effects": [
                "現在の台帳・設定・下書きは、バックアップ時点の内容に置き換わります。",
                "実行前に現在のデータを自動でバックアップします（復元前自動バックアップ）。",
                "PDFはバックアップ時の保存ルート（または指定した復元先）へ書き戻します。内容の異なる同名ファイルは改名して残します。",
                "バックアップ後に追加されたPDFファイルは削除しません（台帳からは参照されなくなります）。",
            ], "warnings": warnings, "can_restore": v["ok"] and not any("復元できません" in w for w in warnings)}


def restore(ctx, name: str, root_override: str | None = None) -> dict:
    p = _resolve_backup(ctx, name)
    pv = restore_preview(ctx, name)
    if not pv["can_restore"]:
        raise BackupError("このバックアップは復元できません。", " / ".join(pv["verify"]["problems"] + pv["warnings"]))
    if ctx.jobs.busy():
        raise BackupError("解析処理の実行中は復元できません。", "解析の完了を待つか、中止してから実行してください。")
    pre = create_backup(ctx, label="復元前自動バックアップ")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stage = ctx.paths.backup_dir / f".restore_{ts}"
    stage.mkdir()
    renamed = []
    try:
        with ctx.storage_lock:
            with zipfile.ZipFile(p) as z:
                m = json.loads(z.read("manifest.json"))
                for n in z.namelist():
                    if not _safe_arc(n):
                        raise BackupError("バックアップに不正なパスが含まれています。")
                z.extract("ledger.db", stage)
                staged_db = stage / "ledger.db"
                chk = sqlite3.connect(str(staged_db))
                try:
                    if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise BackupError("バックアップ内の台帳DBが破損しています。")
                finally:
                    chk.close()
                migrate(staged_db)
                sconn = connect(staged_db)
                try:
                    roots = {r["id"]: r["path"] for r in sconn.execute("SELECT id, path FROM storage_roots")}
                    if root_override:
                        base = Path(root_override)
                        if not base.is_absolute():
                            raise BackupError("復元先フォルダは絶対パスで指定してください。")
                        new_roots = {rid: str(base if len(roots) == 1 else base / f"root_{rid}") for rid in roots}
                        sconn.execute("BEGIN IMMEDIATE")
                        for rid, path in new_roots.items():
                            sconn.execute("UPDATE storage_roots SET path=? WHERE id=?", (path, rid))
                        sconn.execute("COMMIT")
                        roots = new_roots
                    drafts = {r["id"]: r["tmp_path"] for r in sconn.execute("SELECT id, tmp_path FROM imports")}
                    # 下書きの一時PDFは現在のデータフォルダに置き直す
                    sconn.execute("BEGIN IMMEDIATE")
                    for iid in drafts:
                        sconn.execute("UPDATE imports SET tmp_path=? WHERE id=?", (str(ctx.paths.tmp_dir / f"{iid}.pdf"), iid))
                    sconn.execute("COMMIT")
                finally:
                    sconn.close()
                for f in m.get("files", []):
                    root = Path(roots[f["root_id"]])
                    target = S.safe_join(root, *f["rel"].split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        if _sha(target) == f["sha256"]:
                            continue
                        keep = target.with_name(f"{target.stem}.before_restore_{ts}{target.suffix}")
                        os.replace(target, keep)
                        renamed.append(str(keep))
                    tmp = target.with_name(target.name + ".part")
                    with z.open(f["arc"]) as src, open(tmp, "wb") as out:
                        shutil.copyfileobj(src, out)
                    if _sha(tmp) != f["sha256"]:
                        tmp.unlink()
                        raise BackupError(f"PDFの書き戻しで内容が一致しません: {f['mgmt_no']}")
                    os.replace(tmp, target)
                for n in z.namelist():
                    if n.startswith("imports/") and n.endswith(".pdf"):
                        with z.open(n) as src, open(ctx.paths.tmp_dir / PurePosixPath(n).name, "wb") as out:
                            shutil.copyfileobj(src, out)
            # 台帳DBをバックアップAPIで置き換える
            src_conn = sqlite3.connect(str(stage / "ledger.db"))
            live = sqlite3.connect(str(ctx.paths.db_path), timeout=30)
            try:
                src_conn.backup(live)
            finally:
                live.close()
                src_conn.close()
        conn = ctx.connect()
        try:
            problems = S.integrity_check(conn)
            n = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        finally:
            conn.close()
        log.info("restored backup=%s contracts=%s problems=%s", name, n, len(problems))
        return {"restored": name, "pre_restore_backup": pre["name"], "contracts": n,
                "pdf_problems": problems, "renamed_existing": renamed}
    finally:
        shutil.rmtree(stage, ignore_errors=True)
