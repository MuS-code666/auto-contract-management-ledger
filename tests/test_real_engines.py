"""実際のOCR・LLMを使う検証（モックではない）。

- RapidOCR: 導入されていれば実行する（setup.bat で導入される）。
- ローカルLLM: 環境変数 LEDGER_LLM_URL（例 http://127.0.0.1:8081/v1）のサーバーに接続できる場合のみ実行する。
実行:  .venv\\Scripts\\python -m pytest -m real -v
"""
import os

import httpx
import pytest

from .conftest import H, make_client, upload, wait_import

pytestmark = pytest.mark.real


def _rapidocr_ok():
    from app.ocr import RapidOcr
    return RapidOcr().check()["ok"]


@pytest.mark.skipif(not _rapidocr_ok(), reason="RapidOCR が導入されていません")
def test_rapidocr_scanned_pdf(tmp_path, no_network):
    c = make_client(tmp_path / "d", ocr="rapidocr")
    d = wait_import(c, upload(c, "02_産業廃棄物処理委託契約書_スキャン.pdf").json()["id"], timeout=300)
    assert [p["status"] for p in d["pages"]] == ["ok", "ok", "empty"]
    assert [p["text_source"] for p in d["pages"]][:2] == ["ocr", "ocr"]
    f = d["form"]
    assert f["contract_date"] == "2024-03-15" and f["start_date"] == "2024-04-01" and f["expiry_date"] == "2025-03-31"
    assert f["auto_renewal"] == "あり"
    ex = d["result"]["extraction"]["fields"]
    assert ex["expiry_date"]["source"] == "ocr"
    assert ex["title"]["state"] == "要確認"  # OCR由来の名称は誤字確認のため要確認
    c.app.state.ctx.jobs.stop()


LLM_URL = os.environ.get("LEDGER_LLM_URL", "")


def _llm_up():
    if not LLM_URL:
        return False
    try:
        return httpx.get(LLM_URL.rstrip("/") + "/models", timeout=3, trust_env=False).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not _llm_up(), reason="LEDGER_LLM_URL のローカルLLMサーバーに接続できません")
def test_local_llm_extraction(tmp_path):
    c = make_client(tmp_path / "d")
    c.put("/api/settings", json={"llm": {"engine": "openai_compat", "base_url": LLM_URL, "timeout_sec": 900}}, headers=H)
    assert c.post("/api/check/llm", headers=H).json()["ok"]
    d = wait_import(c, upload(c, "07_車両保守業務委託契約書_矛盾空欄.pdf").json()["id"], timeout=1800)
    llm = d["result"]["llm"]
    assert llm["status"] in ("done", "partial"), llm
    ex = d["result"]["extraction"]["fields"]
    # 本文中の「指示」（有効期限2099年12月31日）を確定値として採用していない
    assert ex["expiry_date"].get("value") != "2099-12-31" or ex["expiry_date"]["state"] == "要確認"
    assert ex["contract_date"].get("value") is None  # 空欄の契約日を補完しない
    c.app.state.ctx.jobs.stop()
