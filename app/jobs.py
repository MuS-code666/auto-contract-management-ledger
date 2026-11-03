"""PDF取込みと解析ジョブ。

- 解析は専用ワーカースレッドで1件ずつ実行し、画面操作（HTTP処理）から分離する。
- 各段階で中止要求を確認する。OCRは1ページごと、LLMはチャンクごとにタイムアウトを持つ。
- アプリ終了などで中断された解析は、次回起動時に「中断」状態にし、再解析できるようにする。
- 通常ログには契約本文・当事者名を出さない（IDと段階のみ）。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from pathlib import Path
from typing import BinaryIO

from . import pdftext as P
from . import repo as R
from . import storage as S
from .db import all_settings, now_iso, tx
from .extract import llm_extract as LX
from .extract import rules
from .extract.common import Doc, Page
from .llm import LlmCancelled, LlmUnavailable, get_engine as get_llm
from .ocr import OcrFailed, OcrUnavailable, get_engine as get_ocr

log = logging.getLogger("ledger.jobs")

ACTIVE = ("queued", "processing")
OPEN = ("queued", "processing", "extracted", "partial", "failed", "cancelled", "interrupted")


class Cancelled(Exception):
    pass


class ImportError_(Exception):
    def __init__(self, message: str, hint: str = "", status: int = 400, data: dict | None = None):
        super().__init__(message)
        self.hint = hint
        self.status = status
        self.data = data or {}


class JobManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.q: queue.Queue[str] = queue.Queue()
        self.current: str | None = None
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="analysis-worker", daemon=True)

    def start(self):
        conn = self.ctx.connect()
        try:
            with tx(conn):
                conn.execute("UPDATE imports SET status='interrupted', stage='中断', error=?, error_hint=?, updated_at=? "
                             "WHERE status IN ('queued','processing')",
                             ("前回の解析が完了前に中断されました。", "「再解析」を押してやり直すか、手動で入力してください。", now_iso()))
        finally:
            conn.close()
        self.thread.start()

    def stop(self):
        self._stop.set()
        self.q.put("")

    def submit(self, import_id: str):
        self.q.put(import_id)

    def busy(self) -> bool:
        return self.current is not None or not self.q.empty()

    def _loop(self):
        while not self._stop.is_set():
            iid = self.q.get()
            if not iid:
                continue
            self.current = iid
            try:
                process(self.ctx, iid)
            except Exception:
                log.exception("analysis crashed import=%s", iid)
            finally:
                self.current = None


# ---------------------------------------------------------------- 取込み

def create_import(ctx, stream: BinaryIO, filename: str) -> dict:
    """アップロードされたPDFを一時領域へ保存し、検査する。"""
    conn = ctx.connect()
    try:
        st = all_settings(conn)
        iid = str(uuid.uuid4())
        tmp = ctx.paths.tmp_dir / f"{iid}.pdf"
        limit = int(st["max_pdf_mb"]) * 1024 * 1024
        S.check_free_space(ctx.paths.tmp_dir, 0)
        size = 0
        try:
            with open(tmp, "wb") as out:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise P.PdfError(f"PDFのサイズが上限（{st['max_pdf_mb']} MB）を超えています。",
                                         "PDFを分割するか、設定画面で上限を見直してください。", "too_large")
                    out.write(chunk)
            pages = P.inspect(tmp, int(st["max_pdf_mb"]), int(st["max_pages"]))
        except P.PdfError as e:
            tmp.unlink(missing_ok=True)
            raise ImportError_(str(e), e.hint, data={"code": e.code})
        except OSError as e:
            tmp.unlink(missing_ok=True)
            if getattr(e, "errno", None) == 28:
                raise ImportError_("ディスク容量が不足しています。", "空き容量を確保してから再度取り込んでください。")
            raise
        fhash = S.sha256_file(tmp)
        dup = conn.execute("SELECT id, mgmt_no, deleted_at FROM contracts WHERE file_hash=?", (fhash,)).fetchone()
        if dup:
            tmp.unlink(missing_ok=True)
            raise ImportError_(
                f"同じPDFが既に管理番号 {dup['mgmt_no']} で登録されています" + ("（削除済み）" if dup["deleted_at"] else "") + "。",
                "同一ファイルのため新規登録できません。既存の台帳を開いて確認してください。",
                status=409, data={"contract_id": dup["id"], "mgmt_no": dup["mgmt_no"]})
        same = conn.execute(f"SELECT id FROM imports WHERE file_hash=? AND status IN ({','.join('?' * len(OPEN))})",
                            (fhash, *OPEN)).fetchone()
        if same:
            tmp.unlink(missing_ok=True)
            return {"id": same["id"], "resumed": True}
        ts = now_iso()
        with tx(conn):
            conn.execute("INSERT INTO imports(id, status, stage, original_filename, file_hash, file_size, page_count,"
                         " tmp_path, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (iid, "queued", "待機中", Path(filename or "document.pdf").name[:200], fhash, size, pages,
                          str(tmp), ts, ts))
        log.info("import created id=%s pages=%s size=%s", iid, pages, size)
    finally:
        conn.close()
    ctx.jobs.submit(iid)
    return {"id": iid, "resumed": False}


def _set(conn, iid: str, **kw):
    kw["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in kw)
    with tx(conn):
        conn.execute(f"UPDATE imports SET {cols} WHERE id=?", (*kw.values(), iid))


def _save_page(conn, iid, page_no, source, status, text, note=""):
    chars = len("".join((text or "").split()))
    with tx(conn):
        conn.execute("INSERT OR REPLACE INTO import_pages(import_id, page_no, text_source, status, chars, text, note)"
                     " VALUES (?,?,?,?,?,?,?)", (iid, page_no, source, status, chars, text, note))


PAGE_STATUS_JA = {"ok": "読取済み", "empty": "空ページ", "unprocessed": "未処理", "ocr_failed": "OCR失敗",
                  "low_quality": "低品質（要確認）"}


def process(ctx, iid: str) -> None:
    conn = ctx.connect()
    work_files: list[Path] = []
    try:
        row = conn.execute("SELECT * FROM imports WHERE id=?", (iid,)).fetchone()
        if row is None or row["status"] not in ("queued",):
            return
        st = all_settings(conn)
        _set(conn, iid, status="processing", stage="開始", progress="", started_at=now_iso(), finished_at=None,
             error=None, error_hint=None, cancel_requested=0)
        with tx(conn):
            conn.execute("DELETE FROM import_pages WHERE import_id=?", (iid,))

        def cancelled() -> bool:
            r = conn.execute("SELECT cancel_requested FROM imports WHERE id=?", (iid,)).fetchone()
            return bool(r and r[0])

        def check():
            if cancelled():
                raise Cancelled()

        tmp = Path(row["tmp_path"])
        warnings: list[str] = []
        min_chars = int(st.get("text_min_chars", 20))
        ocr_cfg = st["ocr"]
        ocr = get_ocr(ocr_cfg, ctx.app_root)
        ocr_state = None  # None=未確認, dict=check結果
        need_ocr: list[int] = []
        with P.open_pdf(tmp) as pdf:
            n = len(pdf)
            for i in range(n):
                check()
                _set(conn, iid, stage="文字情報の確認", progress=f"{i + 1}/{n} ページ")
                pt = P.extract_page_text(pdf, i, min_chars)
                if pt.usable:
                    _save_page(conn, iid, i + 1, "text", "ok", pt.text)
                else:
                    need_ocr.append(i)
            for k, i in enumerate(need_ocr):
                check()
                _set(conn, iid, stage="OCR", progress=f"{k + 1}/{len(need_ocr)}（{i + 1}ページ目）")
                png = ctx.paths.work_dir / f"{iid}_p{i + 1}.png"
                work_files.append(png)
                try:
                    P.render_page_png(pdf, i, int(ocr_cfg.get("dpi", 300)), png)
                    if P.is_blank_page(png):
                        _save_page(conn, iid, i + 1, "none", "empty", "", "空ページ")
                        continue
                    if ocr_state is None:
                        ocr_state = ocr.check()
                    if not ocr_state.get("ok"):
                        _save_page(conn, iid, i + 1, "none", "unprocessed", "",
                                   f"文字情報がなくOCRが利用できません: {ocr_state.get('message')}")
                        continue
                    text = ocr.recognize(png, float(ocr_cfg.get("page_timeout_sec", 120)), cancelled)
                    ok, note, _ = P.judge_usable(text, 5)
                    _save_page(conn, iid, i + 1, "ocr", "ok" if ok and len(text) >= 40 else "low_quality", text,
                               "" if ok and len(text) >= 40 else (note or "読み取れた文字が少ない"))
                except OcrUnavailable as e:
                    ocr_state = {"ok": False, "message": str(e), "hint": e.hint}
                    _save_page(conn, iid, i + 1, "none", "unprocessed", "", f"OCRが利用できません: {e}")
                except OcrFailed as e:
                    if cancelled():
                        raise Cancelled()
                    _save_page(conn, iid, i + 1, "none", "ocr_failed", "", str(e))
                finally:
                    png.unlink(missing_ok=True)
        if ocr_state is not None and not ocr_state.get("ok"):
            warnings.append(f"OCR: {ocr_state.get('message')} {ocr_state.get('hint', '')}".strip())

        pages = [Page(r["page_no"], r["text"] or "", r["text_source"], r["status"]) for r in conn.execute(
            "SELECT * FROM import_pages WHERE import_id=? ORDER BY page_no", (iid,))]
        doc = Doc(pages)
        check()
        _set(conn, iid, stage="項目抽出（ルール）", progress="")
        cats = [dict(r) for r in conn.execute("SELECT name, keywords FROM categories WHERE active=1")]
        rule_res = rules.extract_all(doc, cats, st.get("company_names") or [])

        llm_info = {"status": "disabled", "message": "LLMは無効（ルール抽出のみ）", "chunks": []}
        llm_res = None
        llm_cfg = st["llm"]
        if llm_cfg.get("engine") != "none" and any(p.status in ("ok", "low_quality") for p in pages):
            try:
                eng = get_llm(llm_cfg)
                chk = eng.check()
                if not chk.get("ok"):
                    llm_info = {"status": "unavailable", "message": chk["message"], "hint": chk.get("hint"), "chunks": []}
                    warnings.append(f"LLM: {chk['message']} {chk.get('hint', '')}".strip())
                else:
                    out = LX.run_llm(eng, doc, int(llm_cfg.get("chunk_chars", 6000)), float(llm_cfg.get("timeout_sec", 600)),
                                     int(llm_cfg.get("max_retries", 1)), cancelled,
                                     lambda msg: _set(conn, iid, stage="項目抽出（LLM）", progress=msg))
                    llm_res = out["merged"]
                    llm_info = {"status": "done" if out["complete"] else "partial", "chunks": out["chunks"],
                                "message": "LLM解析完了" if out["complete"] else "LLM解析が一部完了していません"}
                    if not out["complete"]:
                        bad = [c for c in out["chunks"] if c["status"] != "done"]
                        warnings.append("LLM: 未完了の部分があります（" + "、".join(
                            f"ページ{c['pages'][0]}〜{c['pages'][-1]}: {c['error']}" for c in bad) + "）")
            except LlmCancelled:
                raise Cancelled()
            except (LlmUnavailable, ValueError) as e:
                llm_info = {"status": "unavailable", "message": str(e), "hint": getattr(e, "hint", ""), "chunks": []}
                warnings.append(f"LLM: {e}")
        combined = LX.combine(rule_res, llm_res)
        flag_ocr_names(combined)

        page_summary = [{"page_no": p.page_no, "source": p.source, "status": p.status,
                         "status_ja": PAGE_STATUS_JA.get(p.status, p.status)} for p in pages]
        unproc = [p.page_no for p in pages if p.status not in ("ok", "empty")]
        if unproc:
            warnings.insert(0, f"読み取れていないページがあります: {', '.join(map(str, unproc))} ページ")
        if all(p.status == "empty" for p in pages):
            warnings.insert(0, "すべてのページが空白です。")
        complete = not unproc and llm_info["status"] in ("disabled", "done")
        result = {"extraction": combined, "pages": page_summary, "llm": llm_info, "warnings": warnings,
                  "complete": complete, "finished_at": now_iso()}
        form_old = json.loads(row["form_json"]) if row["form_json"] else None
        edited = set(json.loads(row["user_edited"] or "[]"))
        cats_by_name = R.categories_by_name(conn)
        form = build_form(combined, cats_by_name, form_old, edited)
        _set(conn, iid, status="extracted" if complete else "partial", stage="完了" if complete else "一部未完了",
             progress="", result_json=json.dumps(result, ensure_ascii=False), form_json=json.dumps(form, ensure_ascii=False),
             finished_at=now_iso())
        log.info("analysis finished import=%s complete=%s pages=%s unprocessed=%s llm=%s",
                 iid, complete, len(pages), len(unproc), llm_info["status"])
    except Cancelled:
        _set(conn, iid, status="cancelled", stage="中止", progress="", finished_at=now_iso(),
             error="解析を中止しました。", error_hint="「再解析」でやり直すか、手動で入力して登録できます。")
        log.info("analysis cancelled import=%s", iid)
    except P.PdfError as e:
        _set(conn, iid, status="failed", stage="失敗", finished_at=now_iso(), error=str(e), error_hint=e.hint)
    except Exception as e:
        log.exception("analysis failed import=%s", iid)
        hint = "手動で入力して登録できます。繰り返し発生する場合はログ（data/logs）を確認してください。"
        msg = f"解析中にエラーが発生しました（{type(e).__name__}）。"
        if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
            msg, hint = "ディスク容量が不足しています。", "空き容量を確保してから再解析してください。"
        _set(conn, iid, status="failed", stage="失敗", finished_at=now_iso(), error=msg, error_hint=hint)
    finally:
        for f in work_files:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        conn.close()


OCR_NOTE = "OCRで読み取った文字のため、誤字（似た字形・旧字体など）がないか確認してください"


def flag_ocr_names(ex: dict) -> None:
    """OCR由来の契約書名・当事者名・署名者名は誤字の影響が大きいため要確認にする。"""
    t = ex["fields"].get("title") or {}
    if t.get("source") == "ocr" and t.get("value"):
        t["state"] = "要確認"
        t["note"] = (t.get("note", "") + " " + OCR_NOTE).strip()
    for key in ("parties", "signers"):
        if any(p.get("source") == "ocr" for p in ex.get(key) or []):
            st = dict(ex.get(key + "_state") or {})
            st["state"] = "要確認"
            st["note"] = (st.get("note", "") + " " + OCR_NOTE).strip()
            ex[key + "_state"] = st


def build_form(ex: dict, cats_by_name: dict, old: dict | None, edited: set[str]) -> dict:
    """抽出結果から入力フォームの初期値を作る。利用者が編集した項目は上書きしない。"""
    f = ex["fields"]

    def v(k):
        return (f.get(k) or {}).get("value")

    form = {
        "title": v("title"), "doc_type": v("doc_type"), "category_id": cats_by_name.get(v("category")),
        "summary": v("summary"),
        "contract_date": v("contract_date"), "contract_date_raw": (f.get("contract_date") or {}).get("raw"),
        "start_date": v("start_date"), "start_date_raw": (f.get("start_date") or {}).get("raw"),
        "term_raw": v("term_raw"),
        "expiry_date": v("expiry_date"), "expiry_raw": (f.get("expiry_date") or {}).get("raw"),
        "expiry_type": v("expiry_type"), "auto_renewal": v("auto_renewal") or "不明",
        "renewal_terms": v("renewal_terms"),
        "renewal_period_months": (f.get("renewal_terms") or {}).get("renewal_period_months"),
        "renewal_notice_raw": v("renewal_notice"),
        "renewal_notice_json": (f.get("renewal_notice") or {}).get("structured"),
        "termination_notice_raw": v("termination_notice"),
        "termination_notice_json": (f.get("termination_notice") or {}).get("structured"),
        "parties": [{"name": p["name"], "role": p.get("role", "相手方"), "label": p.get("label")} for p in ex["parties"]],
    }
    labels = [p.get("label") for p in ex["parties"]]
    form["signers"] = [{"name": s["name"], "title": s.get("title"),
                        "party_index": labels.index(s["party_label"]) if s.get("party_label") in labels else None}
                       for s in ex["signers"]]
    states = {}
    keymap = {"category_id": "category", "renewal_notice_raw": "renewal_notice", "termination_notice_raw": "termination_notice"}
    for k in R.META_FIELDS:
        if k in ("parties", "signers"):
            s = (ex.get(k + "_state") or {}).get("state")
        else:
            s = (f.get(keymap.get(k, k)) or {}).get("state")
        if s in ("記載なし", "要確認", "読取不能"):
            states[k] = s
    form["field_states"] = states
    form["review_status"] = "要確認" if "要確認" in states.values() else "未確認"
    if old:
        for k in edited:
            if k in old:
                form[k] = old[k]
        keep_states = (old.get("field_states") or {})
        for k in edited:
            if k in keep_states:
                form["field_states"][k] = keep_states[k]
            else:
                form["field_states"].pop(k, None)
        for k in ("dept", "owner", "paper_location", "notes", "next_review_date", "notice_deadline_confirmed",
                  "managed_expiry_date", "managed_expiry_note"):
            if old.get(k):
                form[k] = old[k]
    return form


def get_import(conn, iid: str) -> dict:
    r = conn.execute("SELECT * FROM imports WHERE id=?", (iid,)).fetchone()
    if r is None:
        raise ImportError_("取込みデータが見つかりません。", status=404)
    d = dict(r)
    d.pop("tmp_path", None)
    d["result"] = json.loads(d.pop("result_json") or "null")
    d["form"] = json.loads(d.pop("form_json") or "null")
    d["user_edited"] = json.loads(d["user_edited"] or "[]")
    d["pages"] = [dict(p) for p in conn.execute(
        "SELECT page_no, text_source, status, chars, note FROM import_pages WHERE import_id=? ORDER BY page_no", (iid,))]
    for p in d["pages"]:
        p["status_ja"] = PAGE_STATUS_JA.get(p["status"], p["status"])
    if d["started_at"]:
        end = d["finished_at"] or now_iso()
        from datetime import datetime
        d["elapsed_sec"] = int((datetime.fromisoformat(end) - datetime.fromisoformat(d["started_at"])).total_seconds())
    return d


def list_drafts(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"SELECT id, status, stage, original_filename, page_count, created_at, updated_at FROM imports "
        f"WHERE status IN ({','.join('?' * len(OPEN))}) ORDER BY updated_at DESC", OPEN)]


def save_draft(conn, iid: str, form: dict, user_edited: list[str]) -> None:
    r = conn.execute("SELECT status FROM imports WHERE id=?", (iid,)).fetchone()
    if r is None:
        raise ImportError_("取込みデータが見つかりません。", status=404)
    if r["status"] in ("registered", "discarded"):
        raise ImportError_("この取込みは既に登録済みまたは破棄済みです。")
    _set(conn, iid, form_json=json.dumps(form, ensure_ascii=False), user_edited=json.dumps(sorted(set(user_edited))))


def request_cancel(conn, iid: str) -> None:
    _set(conn, iid, cancel_requested=1)
    r = conn.execute("SELECT status FROM imports WHERE id=?", (iid,)).fetchone()
    if r and r["status"] == "queued":
        _set(conn, iid, status="cancelled", stage="中止", error="解析を中止しました。",
             error_hint="「再解析」でやり直すか、手動で入力して登録できます。")


def reanalyze(ctx, conn, iid: str) -> None:
    r = conn.execute("SELECT status FROM imports WHERE id=?", (iid,)).fetchone()
    if r is None:
        raise ImportError_("取込みデータが見つかりません。", status=404)
    if r["status"] in ACTIVE:
        raise ImportError_("解析中です。完了または中止してから再解析してください。")
    if r["status"] in ("registered", "discarded"):
        raise ImportError_("登録済み・破棄済みの取込みは再解析できません。")
    _set(conn, iid, status="queued", stage="待機中", cancel_requested=0, error=None, error_hint=None)
    ctx.jobs.submit(iid)


def discard(conn, iid: str) -> None:
    r = conn.execute("SELECT status, tmp_path FROM imports WHERE id=?", (iid,)).fetchone()
    if r is None:
        raise ImportError_("取込みデータが見つかりません。", status=404)
    if r["status"] in ACTIVE:
        raise ImportError_("解析中は破棄できません。先に中止してください。")
    if r["status"] == "registered":
        raise ImportError_("登録済みの取込みは破棄できません。")
    _set(conn, iid, status="discarded", stage="破棄")
    if r["tmp_path"]:
        Path(r["tmp_path"]).unlink(missing_ok=True)


def register(ctx, conn, iid: str, data: R.ContractIn, ack_duplicates: bool, fail_after: str | None = None) -> dict:
    r = conn.execute("SELECT * FROM imports WHERE id=?", (iid,)).fetchone()
    if r is None:
        raise ImportError_("取込みデータが見つかりません。", status=404)
    if r["status"] in ACTIVE:
        raise ImportError_("解析中のため登録できません。完了を待つか、中止してから登録してください。",
                           "解析途中の内容は確定情報として登録しません。")
    if r["status"] in ("registered", "discarded"):
        raise ImportError_("この取込みは既に登録済みまたは破棄済みです。")
    dups = R.duplicate_candidates(conn, data.title, [p.name for p in data.parties], data.contract_date)
    if dups and not ack_duplicates:
        raise ImportError_("重複の可能性がある契約があります。内容を確認してください。",
                           "同じ契約の再スキャンでない（変更覚書・別契約など）ことを確認したら、確認欄にチェックして登録してください。",
                           status=409, data={"duplicates": dups})
    result = json.loads(r["result_json"]) if r["result_json"] else None
    tmp = Path(r["tmp_path"])
    if not tmp.exists():
        raise ImportError_("取込み時の一時ファイルが見つかりません。", "PDFをもう一度取り込んでください。")
    with ctx.storage_lock:
        c = R.register_contract(conn, data, pdf_src=tmp, original_filename=r["original_filename"],
                                file_hash=r["file_hash"], page_count=r["page_count"], import_id=iid,
                                extraction=(result or {}).get("extraction"), fail_after=fail_after)
    _set(conn, iid, status="registered", stage="登録済み", contract_id=c["id"],
         form_json=json.dumps(data.model_dump(), ensure_ascii=False, default=str))
    try:
        tmp.unlink(missing_ok=True)  # 管理フォルダへのコピー完了後に一時ファイルを削除（元ファイルではない）
    except OSError:
        pass
    return c


def cleanup_stale(ctx) -> None:
    """破棄・登録済みの一時ファイル、作業ファイルの残骸を掃除する。"""
    for f in ctx.paths.work_dir.glob("*.png"):
        f.unlink(missing_ok=True)
    conn = ctx.connect()
    try:
        keep = {Path(r["tmp_path"]).name for r in conn.execute(
            f"SELECT tmp_path FROM imports WHERE status IN ({','.join('?' * len(OPEN))})", OPEN) if r["tmp_path"]}
    finally:
        conn.close()
    for f in ctx.paths.tmp_dir.glob("*.pdf"):
        if f.name not in keep:
            f.unlink(missing_ok=True)
