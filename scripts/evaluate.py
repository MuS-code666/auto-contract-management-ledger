"""抽出精度の評価（項目ごとの 正解／誤り／欠落／要確認）。

架空サンプル（samples/）を一時データフォルダで取り込み、tests/expected.py の正解と比較する。
    python scripts/evaluate.py --ocr rapidocr --llm none
    python scripts/evaluate.py --ocr rapidocr --llm openai_compat --llm-url http://127.0.0.1:8081/v1
結果は標準出力と、--out に指定したJSONへ出す。モックではなく、実際のOCR・LLMの結果である。

判定:
  正解   … 値が正解と一致（正解が「記載なし」で、値なしかつ状態が記載なし/要確認の場合も正解）
  要確認 … 値が正解と違う・または無いが、状態が「要確認」になっている（利用者の確認に回る）
  欠落   … 正解があるのに値がなく、要確認にもなっていない
  誤り   … 正解と異なる値が、要確認なしで提示された（最も避けるべき結果）
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from expected import EXPECTED, HOLDOUT  # noqa: E402

ALL = {**EXPECTED, **HOLDOUT}

FIELDS = ["title", "doc_type", "category", "contract_date", "start_date", "expiry_date", "expiry_type",
          "auto_renewal", "renewal_period_months", "renewal_notice", "termination_notice", "parties", "signers"]


def _n(s):
    return "".join(unicodedata.normalize("NFKC", str(s)).split()) if s is not None else None


def judge(field: str, exp, form: dict, ex: dict, cats: dict) -> str:
    f = ex["fields"]
    if field == "parties":
        got = {p.get("label"): _n(p["name"]) for p in form.get("parties") or []}
        want = {k: _n(v) for k, v in exp.items()}
        state = (ex.get("parties_state") or {}).get("state")
        if got == want:
            return "正解"
        return "要確認" if state == "要確認" else ("欠落" if not got else "誤り")
    if field == "signers":
        got = {_n(s["name"]) for s in form.get("signers") or []}
        want = {_n(v) for v in exp}
        if got == want:
            return "正解"
        return "欠落" if not got else ("要確認" if (ex.get("signers_state") or {}).get("state") == "要確認" else "誤り")
    if field in ("renewal_notice", "termination_notice"):
        st = form.get(field + "_json")
        got = (st["value"], st["unit"]) if st else None
        state = (f.get(field) or {}).get("state")
        if got == (tuple(exp) if exp else None):
            return "正解"
        if state == "要確認":
            return "要確認"
        return "欠落" if got is None else "誤り"
    key = {"category": "category", "renewal_period_months": "renewal_terms"}.get(field, field)
    meta = f.get(key) or {}
    state = meta.get("state")
    if field == "category":
        got = next((n for n, i in cats.items() if i == form.get("category_id")), None)
        if exp is None:
            return "正解" if state == "要確認" else "誤り"  # 候補提示のみが正解
        return "正解" if got == exp else ("要確認" if state == "要確認" else "誤り")
    got = form.get({"renewal_period_months": "renewal_period_months"}.get(field, field))
    if exp == "要確認":  # 矛盾等で要確認になるべき項目
        return "正解" if (state == "要確認" or got == "要確認" or (field == "auto_renewal" and got == "不明" and state == "要確認")) else "誤り"
    if _n(got) == _n(exp):
        return "正解"
    if exp is None and got in (None, "", "不明"):
        return "正解"
    if state == "要確認":
        return "要確認"
    return "欠落" if got in (None, "") else "誤り"


def run(ocr: str, llm: str, llm_url: str, model: str, files: list[str] | None = None, which: str = "tuning") -> dict:
    from fastapi.testclient import TestClient

    from app.main import create_app

    H = {"X-Ledger-Request": "1"}
    tmp = tempfile.mkdtemp(prefix="ledger_eval_")
    app = create_app(tmp)
    c = TestClient(app)
    c.put("/api/settings", json={"company_names": ["架空建設株式会社"], "ocr": {"engine": ocr},
                                 "llm": {"engine": llm, "base_url": llm_url, "model": model, "timeout_sec": 900}}, headers=H)
    cats = {x["name"]: x["id"] for x in c.get("/api/meta").json()["categories"]}
    report = {"settings": {"ocr": ocr, "llm": llm, "model": model}, "documents": {}, "totals": {}}
    for name in files or {"tuning": EXPECTED, "holdout": HOLDOUT, "all": ALL}[which]:
        with open(ROOT / "samples" / name, "rb") as fh:
            r = c.post("/api/imports", files={"file": (name, fh, "application/pdf")}, headers=H)
        iid = r.json()["id"]
        t0 = time.time()
        while True:
            d = c.get(f"/api/imports/{iid}").json()
            if d["status"] not in ("queued", "processing"):
                break
            time.sleep(0.3)
        res = d.get("result") or {}
        ex = res.get("extraction") or {"fields": {}, "parties": [], "signers": []}
        form = d.get("form") or {}
        row = {fld: judge(fld, ALL[name][fld], form, ex, cats) for fld in FIELDS}
        report["documents"][name] = {"status": d["status"], "seconds": round(time.time() - t0, 1),
                                     "pages": [p["status"] for p in d["pages"]], "llm": (res.get("llm") or {}).get("status"),
                                     "fields": row}
        for v in row.values():
            report["totals"][v] = report["totals"].get(v, 0) + 1
        c.delete(f"/api/imports/{iid}", headers=H)
    app.state.ctx.jobs.stop()
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ocr", default="rapidocr")
    ap.add_argument("--llm", default="none")
    ap.add_argument("--llm-url", default="http://127.0.0.1:8081/v1")
    ap.add_argument("--model", default="")
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--set", default="tuning", choices=["tuning", "holdout", "all"],
                    help="tuning=ルール調整に使ったサンプル / holdout=評価専用サンプル")
    ap.add_argument("--out")
    a = ap.parse_args()
    rep = run(a.ocr, a.llm, a.llm_url, a.model, a.files, a.set)
    print(f"設定: OCR={a.ocr} LLM={a.llm} {a.model} 対象={a.set}")
    print("文書".ljust(34), " ".join(f[:6].ljust(6) for f in FIELDS), "秒")
    for name, d in rep["documents"].items():
        print(name.replace("holdout/", "")[:32].ljust(34), " ".join(d["fields"][f].ljust(4) for f in FIELDS), d["seconds"], d["status"], d["llm"])
    print("合計:", rep["totals"])
    if a.out:
        Path(a.out).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
