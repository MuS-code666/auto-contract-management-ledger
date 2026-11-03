"""ローカルWebアプリ（FastAPI）。127.0.0.1 だけで待ち受ける。

起動:  python -m app.main [--port 8765] [--data-dir PATH] [--no-browser]
"""
from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import threading
import webbrowser
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import APP_NAME, VERSION
from . import admin as A
from . import backup as B
from . import jobs as J
from . import repo as R
from . import storage as S
from .config import APP_ROOT, DOC_TYPES, Paths, resolve_paths
from .db import all_settings, connect, migrate, seed_defaults, set_setting, tx
from .llm import get_engine as get_llm, validate_local_url
from .ocr import get_engine as get_ocr
from .pdftext import PdfError

log = logging.getLogger("ledger")
STATIC = Path(__file__).resolve().parent / "static"
CSRF_HEADER = "x-ledger-request"


class AppContext:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.app_root = APP_ROOT
        self.storage_lock = threading.RLock()
        self.jobs = J.JobManager(self)
        self.recovery: list[dict] = []

    def connect(self):
        return connect(self.paths.db_path)


def setup_logging(paths: Paths) -> None:
    root = logging.getLogger("ledger")
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    h = RotatingFileHandler(paths.log_dir / "app.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(h)
    root.setLevel(logging.INFO)


def apply_initial_settings(conn) -> None:
    """初回起動時だけ config/settings.json（あれば）の内容を設定に反映する。"""
    f = APP_ROOT / "config" / "settings.json"
    if not f.exists():
        return
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("config/settings.json を読めないため無視しました")
        return
    cur = all_settings(conn)
    with tx(conn):
        for k, v in data.items():
            if k.startswith("_") or k not in cur:
                continue
            if isinstance(v, dict) and isinstance(cur[k], dict):
                v = {**cur[k], **v}
            if k == "llm":
                try:
                    v["base_url"] = validate_local_url(v.get("base_url", ""))
                except ValueError:
                    log.warning("config/settings.json の LLM 接続先がループバックではないため無視しました")
                    continue
            set_setting(conn, k, v)


def init_context(data_dir: str | None = None, start_worker: bool = True) -> AppContext:
    paths = resolve_paths(data_dir)
    setup_logging(paths)
    migrate(paths.db_path, paths.backup_dir)
    ctx = AppContext(paths)
    conn = ctx.connect()
    try:
        first = conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
        seed_defaults(conn, paths.default_storage_root)
        if first:
            apply_initial_settings(conn)
        ctx.recovery = S.recover(conn)
    finally:
        conn.close()
    J.cleanup_stale(ctx)
    if start_worker:
        ctx.jobs.start()
    return ctx


def create_app(data_dir: str | None = None, start_worker: bool = True) -> FastAPI:
    ctx = init_context(data_dir, start_worker)
    # FastAPI 0.16x 内蔵の OpenTelemetry 計測はすべて無効化する（外部エクスポーターも自動設定しない）
    app = FastAPI(title=APP_NAME, version=VERSION, docs_url=None, redoc_url=None, openapi_url=None,
                  telemetry={"tracing": False, "metrics": False, "logs": False, "operation_spans": False,
                             "auto_configure": False})
    app.state.ctx = ctx
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # 他サイトからの書込み要求（CSRF）を防ぐため、更新系APIには独自ヘッダーを必須にする
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"error": "不正な要求です。", "hint": "アプリの画面から操作してください。"}, status_code=403)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if not resp.headers.get("content-type", "").startswith("application/pdf"):  # 内蔵PDFビューアを妨げない
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                "frame-src 'self'; object-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'")
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    def err(msg, hint="", status=400, **data):
        return JSONResponse({"error": msg, "hint": hint, **data}, status_code=status)

    @app.exception_handler(R.RepoError)
    async def _e1(_, e: R.RepoError):
        return err(str(e), e.hint, e.status, **e.data)

    @app.exception_handler(J.ImportError_)
    async def _e2(_, e: J.ImportError_):
        return err(str(e), e.hint, e.status, **e.data)

    @app.exception_handler(S.StorageError)
    async def _e3(_, e: S.StorageError):
        return err(str(e), e.hint, 400)

    @app.exception_handler(B.BackupError)
    async def _e4(_, e: B.BackupError):
        return err(str(e), e.hint, 400)

    @app.exception_handler(PdfError)
    async def _e5(_, e: PdfError):
        return err(str(e), e.hint, 400)

    def _validation(e, skip: int):
        msgs = []
        for x in e.errors()[:5]:
            loc = ".".join(str(l) for l in x.get("loc", [])[skip:])
            label = R.LABELS_JA.get(loc.split(".")[0], loc)
            m = x.get("msg", "")
            if "Value error, " in m:
                m = m.split("Value error, ", 1)[1]
            msgs.append(f"{label}: {m}")
        return err("入力内容に誤りがあります。", " / ".join(msgs), 422)

    @app.exception_handler(RequestValidationError)
    async def _e6(_, e: RequestValidationError):
        return _validation(e, 1)

    @app.exception_handler(ValidationError)
    async def _e6b(_, e: ValidationError):
        return _validation(e, 0)

    @app.exception_handler(Exception)
    async def _e7(_, e: Exception):
        log.exception("unhandled error")
        if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
            return err("ディスク容量が不足しています。", "空き容量を確保してから再実行してください。", 507)
        return err(f"処理中にエラーが発生しました（{type(e).__name__}）。",
                   "操作をやり直してください。繰り返す場合は data/logs/app.log を確認してください。", 500)

    def db():
        conn = ctx.connect()
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------ 画面
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/meta")
    def meta(conn=Depends(db)):
        st = all_settings(conn)
        unresolved = [dict(r) for r in conn.execute(
            "SELECT id, op, state, error, created_at FROM file_ops WHERE state IN ('pending','file_done','db_done')")]
        return {"app": APP_NAME, "version": VERSION, "doc_types": DOC_TYPES,
                "categories": A.list_categories(conn), "review_states": R.REVIEW_STATES,
                "expiry_types": R.EXPIRY_TYPES, "renewal": R.RENEWAL, "relation_types": R.RELATION_TYPES,
                "field_states": R.FIELD_STATES, "company_names": st.get("company_names") or [],
                "ocr_engine": st["ocr"]["engine"], "llm_engine": st["llm"]["engine"],
                "alert_days_expiry": st["alert_days_expiry"], "alert_days_notice": st["alert_days_notice"],
                "recovery": ctx.recovery, "unresolved_file_ops": unresolved, "data_dir": str(ctx.paths.data_dir)}

    # ------------------------------------------------------------ 取込み
    @app.post("/api/imports")
    def upload(file: UploadFile = File(...)):
        name = file.filename or "document.pdf"
        if not name.lower().endswith(".pdf"):
            raise J.ImportError_("PDFファイル（.pdf）を選択してください。")
        return J.create_import(ctx, file.file, name)

    @app.get("/api/imports")
    def drafts(conn=Depends(db)):
        return J.list_drafts(conn)

    @app.get("/api/imports/{iid}")
    def get_import(iid: str, conn=Depends(db)):
        d = J.get_import(conn, iid)
        f = d.get("form") or {}
        d["duplicates"] = R.duplicate_candidates(conn, f.get("title"), [p["name"] for p in f.get("parties") or []],
                                                 f.get("contract_date")) if f else []
        return d

    @app.get("/api/imports/{iid}/pdf")
    def import_pdf(iid: str, conn=Depends(db)):
        r = conn.execute("SELECT tmp_path, original_filename FROM imports WHERE id=?", (iid,)).fetchone()
        if not r or not r["tmp_path"] or not Path(r["tmp_path"]).exists():
            raise J.ImportError_("PDFが見つかりません。", status=404)
        p = Path(r["tmp_path"]).resolve()
        if ctx.paths.tmp_dir.resolve() not in p.parents:
            raise J.ImportError_("不正なファイルパスです。", status=400)
        return _pdf_response(p, r["original_filename"])

    @app.get("/api/imports/{iid}/pages/{page_no}")
    def import_page_text(iid: str, page_no: int, conn=Depends(db)):
        r = conn.execute("SELECT page_no, text_source, status, text, note FROM import_pages WHERE import_id=? AND page_no=?",
                         (iid, page_no)).fetchone()
        if not r:
            raise J.ImportError_("ページの文字情報がありません。", status=404)
        return dict(r)

    @app.put("/api/imports/{iid}/draft")
    async def save_draft(iid: str, request: Request, conn=Depends(db)):
        body = await request.json()
        J.save_draft(conn, iid, body.get("form") or {}, body.get("user_edited") or [])
        return {"ok": True}

    @app.post("/api/imports/{iid}/cancel")
    def cancel(iid: str, conn=Depends(db)):
        J.request_cancel(conn, iid)
        return {"ok": True}

    @app.post("/api/imports/{iid}/analyze")
    def analyze(iid: str, conn=Depends(db)):
        J.reanalyze(ctx, conn, iid)
        return {"ok": True}

    @app.delete("/api/imports/{iid}")
    def discard(iid: str, conn=Depends(db)):
        J.discard(conn, iid)
        return {"ok": True}

    @app.post("/api/imports/{iid}/register")
    async def register(iid: str, request: Request, conn=Depends(db)):
        body = await request.json()
        data = R.ContractIn.model_validate(body.get("form") or {})
        c = J.register(ctx, conn, iid, data, bool(body.get("ack_duplicates")))
        return {"id": c["id"], "mgmt_no": c["mgmt_no"]}

    # ------------------------------------------------------------ 台帳
    @app.get("/api/contracts")
    def list_contracts(request: Request, conn=Depends(db)):
        res = R.search(conn, dict(request.query_params))
        for it in res["items"]:
            it.pop("summary_full", None)
        return res

    @app.post("/api/contracts")
    async def create_manual(request: Request, conn=Depends(db)):
        body = await request.json()
        data = R.ContractIn.model_validate(body.get("form") or {})
        dups = R.duplicate_candidates(conn, data.title, [p.name for p in data.parties], data.contract_date)
        if dups and not body.get("ack_duplicates"):
            raise R.RepoError("重複の可能性がある契約があります。内容を確認してください。",
                              "別の契約であることを確認したら、確認欄にチェックして登録してください。", 409,
                              {"duplicates": dups})
        with ctx.storage_lock:
            c = R.register_contract(conn, data)
        return {"id": c["id"], "mgmt_no": c["mgmt_no"]}

    @app.get("/api/contracts/{cid}")
    def get_contract(cid: str, conn=Depends(db)):
        c = R.get_contract(conn, cid)
        c["duplicates"] = R.duplicate_candidates(conn, c["title"], [p["name"] for p in c["parties"]],
                                                 c["contract_date"], exclude_id=cid)
        c["pdf_exists"] = False
        if c.get("pdf_relpath"):
            p = S.contract_pdf_abspath(conn, c["storage_root_id"], c["pdf_relpath"])
            c["pdf_exists"] = bool(p and p.exists())
            c["pdf_abspath"] = str(p) if p else None
        return c

    @app.put("/api/contracts/{cid}")
    async def update_contract(cid: str, request: Request, conn=Depends(db)):
        body = await request.json()
        data = R.ContractIn.model_validate(body.get("form") or {})
        with ctx.storage_lock:
            c = R.update_contract(conn, cid, data)
        return {"id": c["id"], "mgmt_no": c["mgmt_no"]}

    @app.post("/api/contracts/{cid}/review")
    async def review(cid: str, request: Request, conn=Depends(db)):
        body = await request.json()
        R.set_review(conn, cid, body.get("status"))
        return {"ok": True}

    @app.post("/api/contracts/{cid}/delete")
    def delete(cid: str, conn=Depends(db)):
        R.soft_delete(conn, cid)
        return {"ok": True}

    @app.post("/api/contracts/{cid}/restore")
    def restore_c(cid: str, conn=Depends(db)):
        R.soft_delete(conn, cid, restore=True)
        return {"ok": True}

    @app.get("/api/contracts/{cid}/history")
    def hist(cid: str, conn=Depends(db)):
        return R.history(conn, cid)

    @app.get("/api/contracts/{cid}/pdf")
    def contract_pdf(cid: str, conn=Depends(db)):
        c = conn.execute("SELECT storage_root_id, pdf_relpath, original_filename, mgmt_no FROM contracts WHERE id=?",
                         (cid,)).fetchone()
        if not c or not c["pdf_relpath"]:
            raise R.RepoError("この契約にはPDFが登録されていません。", status=404)
        p = S.contract_pdf_abspath(conn, c["storage_root_id"], c["pdf_relpath"])
        if not p or not p.exists():
            raise R.RepoError("PDFファイルが見つかりません。", "設定画面の「整合性チェック」で確認してください。", 404)
        return _pdf_response(p, Path(c["pdf_relpath"]).name)

    @app.get("/api/contracts/{cid}/pages")
    def contract_pages(cid: str, conn=Depends(db)):
        r = conn.execute("SELECT import_id FROM contracts WHERE id=?", (cid,)).fetchone()
        if not r or not r["import_id"]:
            return []
        return [dict(x) for x in conn.execute(
            "SELECT page_no, text_source, status, chars, note FROM import_pages WHERE import_id=? ORDER BY page_no",
            (r["import_id"],))]

    @app.get("/api/contracts/{cid}/pages/{page_no}")
    def contract_page_text(cid: str, page_no: int, conn=Depends(db)):
        r = conn.execute("SELECT import_id FROM contracts WHERE id=?", (cid,)).fetchone()
        if not r or not r["import_id"]:
            raise R.RepoError("文字情報がありません。", status=404)
        return import_page_text(r["import_id"], page_no, conn)

    @app.post("/api/contracts/{cid}/relations")
    async def add_rel(cid: str, request: Request, conn=Depends(db)):
        b = await request.json()
        other = b.get("other_id")
        if not other and b.get("other_mgmt_no"):
            row = conn.execute("SELECT id FROM contracts WHERE mgmt_no=?", (b["other_mgmt_no"].strip().upper(),)).fetchone()
            if not row:
                raise R.RepoError("指定した管理番号の契約が見つかりません。")
            other = row["id"]
        src, dst = (cid, other) if b.get("direction", "out") == "out" else (other, cid)
        R.add_relation(conn, src, dst, b.get("rel_type"), b.get("note"))
        return {"ok": True}

    @app.delete("/api/relations/{rid}")
    def del_rel(rid: int, conn=Depends(db)):
        R.delete_relation(conn, rid)
        return {"ok": True}

    @app.post("/api/calc")
    async def calc(request: Request):
        """入力中の内容から通知期限・次回満了日の候補を計算する（保存はしない）。"""
        from . import deadlines as DL

        b = await request.json()
        data = R.ContractIn.model_validate(b.get("form") or {})
        c = data.model_dump()
        return {"notice": DL.notice_candidate(c), "next_term": DL.next_term_candidate(c)}

    @app.get("/api/deadlines")
    def deadlines(conn=Depends(db)):
        st = all_settings(conn)
        return {"today": date.today().isoformat(), "alert_days_expiry": st["alert_days_expiry"],
                "alert_days_notice": st["alert_days_notice"],
                "buckets": R.deadline_board(conn, date.today(), int(st["alert_days_expiry"]), int(st["alert_days_notice"]))}

    @app.get("/api/export.xlsx")
    def export(request: Request, conn=Depends(db)):
        from .excel import build_workbook

        q = dict(request.query_params)
        scope = q.pop("scope", "filtered")
        if scope == "all":
            q = {"deleted": "exclude"}
            cond = "全件（削除済みを除く）"
        else:
            cond = "検索・絞込み結果: " + (", ".join(f"{k}={v}" for k, v in q.items() if v and k not in ("page", "per_page")) or "条件なし")
        res = R.search(conn, q, paged=False)
        for it in res["items"]:
            it["parties"] = [dict(p) for p in conn.execute(
                "SELECT name, role FROM parties WHERE contract_id=? ORDER BY sort_order", (it["id"],))]
        roots = {r["id"]: r["path"] for r in conn.execute("SELECT id, path FROM storage_roots")}
        data = build_workbook(res["items"], roots, cond)
        fname = f"契約台帳_{datetime.now():%Y%m%d_%H%M}.xlsx"
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})

    # ------------------------------------------------------------ 設定
    @app.get("/api/settings")
    def get_settings(conn=Depends(db)):
        st = all_settings(conn)
        st["storage_roots"] = A.list_roots(conn)
        return st

    @app.put("/api/settings")
    async def put_settings(request: Request, conn=Depends(db)):
        b = await request.json()
        cur = all_settings(conn)
        updates = {}
        if "company_names" in b:
            names = [str(n).strip() for n in (b["company_names"] or []) if str(n).strip()]
            updates["company_names"] = names[:20]
        for k, lo, hi in (("alert_days_expiry", 0, 3650), ("alert_days_notice", 0, 3650), ("max_pdf_mb", 1, 1000),
                          ("max_pages", 1, 2000), ("text_min_chars", 1, 1000)):
            if k in b:
                try:
                    v = int(b[k])
                except (TypeError, ValueError):
                    raise R.RepoError(f"{k} は数値で入力してください。")
                if not lo <= v <= hi:
                    raise R.RepoError(f"{k} は {lo}〜{hi} の範囲で入力してください。")
                updates[k] = v
        if "ocr" in b:
            o = dict(cur["ocr"])
            o.update({k: b["ocr"][k] for k in ("engine", "tesseract_path", "rapidocr_model_dir", "languages", "dpi",
                                               "page_timeout_sec") if k in b["ocr"]})
            if o["engine"] not in ("rapidocr", "tesseract", "none"):
                raise R.RepoError("OCRエンジンの指定が不正です。")
            if not str(o["languages"]).replace("+", "").replace("_", "").isalnum():
                raise R.RepoError("OCR言語の指定が不正です（例: jpn+jpn_vert）。")
            o["dpi"] = min(600, max(100, int(o["dpi"])))
            o["page_timeout_sec"] = min(1800, max(10, int(o["page_timeout_sec"])))
            updates["ocr"] = o
        if "llm" in b:
            l = dict(cur["llm"])
            l.update({k: b["llm"][k] for k in ("engine", "base_url", "model", "timeout_sec", "chunk_chars",
                                               "max_retries", "temperature") if k in b["llm"]})
            if l["engine"] not in ("openai_compat", "none"):
                raise R.RepoError("LLMエンジンの指定が不正です。")
            try:
                l["base_url"] = validate_local_url(l["base_url"])
            except ValueError as e:
                raise R.RepoError(str(e))
            l["timeout_sec"] = min(7200, max(10, int(l["timeout_sec"])))
            l["chunk_chars"] = min(30000, max(1000, int(l["chunk_chars"])))
            l["max_retries"] = min(5, max(0, int(l["max_retries"])))
            l["temperature"] = min(1.0, max(0.0, float(l["temperature"])))
            updates["llm"] = l
        with tx(conn):
            for k, v in updates.items():
                set_setting(conn, k, v)
        return {"ok": True}

    @app.post("/api/check/ocr")
    def check_ocr(conn=Depends(db)):
        return get_ocr(all_settings(conn)["ocr"], ctx.app_root).check()

    @app.post("/api/check/llm")
    def check_llm(conn=Depends(db)):
        try:
            return get_llm(all_settings(conn)["llm"]).check()
        except ValueError as e:
            return {"ok": False, "message": str(e)}

    @app.post("/api/categories")
    async def add_cat(request: Request, conn=Depends(db)):
        b = await request.json()
        return A.create_category(conn, b.get("name"), b.get("keywords", ""))

    @app.post("/api/categories/{cat_id}/rename-preview")
    async def ren_prev(cat_id: int, request: Request, conn=Depends(db)):
        b = await request.json()
        return A.rename_preview(conn, cat_id, b.get("name"))

    @app.post("/api/categories/{cat_id}/rename")
    async def ren(cat_id: int, request: Request, conn=Depends(db)):
        b = await request.json()
        with ctx.storage_lock:
            return A.rename_category(conn, cat_id, b.get("name"))

    @app.put("/api/categories/{cat_id}")
    async def upd_cat(cat_id: int, request: Request, conn=Depends(db)):
        b = await request.json()
        A.update_category(conn, cat_id, active=b.get("active"), keywords=b.get("keywords"), sort_order=b.get("sort_order"))
        return {"ok": True}

    @app.post("/api/categories/sync-folders")
    def sync_folders(conn=Depends(db)):
        with ctx.storage_lock:
            return {"failed": A.sync_category_folders(conn)}

    @app.post("/api/storage-roots")
    async def set_root(request: Request, conn=Depends(db)):
        b = await request.json()
        mode = b.get("mode")
        if mode not in ("new_only", "migrate"):
            raise R.RepoError("変更方法を選択してください（新規保存先のみ／既存データも移行）。")
        with ctx.storage_lock:
            return A.set_default_root(conn, b.get("path"), mode)

    @app.get("/api/integrity")
    def integrity(conn=Depends(db)):
        unresolved = [dict(r) for r in conn.execute(
            "SELECT id, op, state, error, dst_rel, created_at FROM file_ops WHERE state IN ('pending','file_done','db_done')")]
        return {"pdf_problems": S.integrity_check(conn), "unresolved_file_ops": unresolved}

    @app.post("/api/recover")
    def recover(conn=Depends(db)):
        with ctx.storage_lock:
            return {"results": S.recover(conn)}

    @app.get("/api/backups")
    def backups():
        return {"dir": str(ctx.paths.backup_dir), "items": B.list_backups(ctx)}

    @app.post("/api/backups")
    def make_backup():
        return B.create_backup(ctx)

    @app.post("/api/backups/upload")
    def upload_backup(file: UploadFile = File(...)):
        name = Path(file.filename or "").name
        if not name.startswith("backup_") or not name.endswith(".zip"):
            raise B.BackupError("バックアップファイル（backup_～.zip）を選択してください。")
        dest = ctx.paths.backup_dir / name
        if dest.exists():
            raise B.BackupError("同名のバックアップが既にあります。")
        with open(dest, "wb") as out:
            while chunk := file.file.read(1024 * 1024):
                out.write(chunk)
        return {"name": name}

    @app.get("/api/backups/{name}/preview")
    def preview(name: str):
        return B.restore_preview(ctx, name)

    @app.post("/api/backups/{name}/restore")
    async def do_restore(name: str, request: Request):
        b = await request.json()
        if b.get("confirm") != name:
            raise B.BackupError("確認のため、復元するバックアップ名を入力してください。")
        return B.restore(ctx, name, b.get("root_override") or None)

    def _pdf_response(path: Path, filename: str | None):
        fn = quote(filename or path.name)
        return FileResponse(path, media_type="application/pdf",
                            headers={"Content-Disposition": f"inline; filename*=UTF-8''{fn}",
                                     "Cache-Control": "no-store"})

    return app


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def run() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    if not _port_free(args.port):
        print(f"ポート {args.port} は使用中です。既にアプリが起動していないか確認するか、--port で別の番号を指定してください。")
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{args.port}/")
        sys.exit(1)
    app = create_app(args.data_dir)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"{APP_NAME} {VERSION} を起動しました: {url}")
    print(f"データフォルダ: {app.state.ctx.paths.data_dir}")
    print("終了するには、このウィンドウで Ctrl+C を押すか、ウィンドウを閉じてください。")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    run()
