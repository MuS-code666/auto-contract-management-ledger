import shutil
import socket
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
H = {"X-Ledger-Request": "1"}


def pytest_configure(config):
    config.addinivalue_line("markers", "real: 実際のOCR・LLMを使う検証（モックではない）")


@pytest.fixture(scope="session", autouse=True)
def samples():
    if not (SAMPLES / "01_産業廃棄物処理委託契約書_文字PDF.pdf").exists():
        import runpy
        runpy.run_path(str(ROOT / "scripts" / "make_samples.py"), run_name="__main__")
    return SAMPLES


def make_client(data_dir: Path, ocr="none", llm="none"):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(str(data_dir))
    c = TestClient(app)
    r = c.put("/api/settings", json={"company_names": ["架空建設株式会社"], "ocr": {"engine": ocr},
                                    "llm": {"engine": llm}}, headers=H)
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def client(tmp_path):
    c = make_client(tmp_path / "data")
    yield c
    c.app.state.ctx.jobs.stop()


def upload(c, name: str):
    with open(SAMPLES / name, "rb") as f:
        return c.post("/api/imports", files={"file": (name, f, "application/pdf")}, headers=H)


def wait_import(c, iid: str, timeout: float = 120) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        d = c.get(f"/api/imports/{iid}").json()
        if d["status"] not in ("queued", "processing"):
            return d
        time.sleep(0.1)
    raise AssertionError("解析が終わりません")


def import_and_register(c, name: str, **form_overrides) -> dict:
    r = upload(c, name)
    assert r.status_code == 200, r.text
    d = wait_import(c, r.json()["id"])
    form = d["form"]
    form.update(form_overrides)
    if not form.get("category_id"):
        form["category_id"] = c.get("/api/meta").json()["categories"][0]["id"]
    r = c.post(f"/api/imports/{d['id']}/register", json={"form": form, "ack_duplicates": True}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def no_network(monkeypatch):
    """ループバック以外への接続を禁止する（インターネット接続なしの動作確認）。"""
    real = socket.socket.connect

    def guarded(self, addr):
        host = addr[0] if isinstance(addr, tuple) else addr
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise OSError(f"外部への接続は禁止されています: {addr}")
        return real(self, addr)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    real_cc = socket.create_connection

    def guarded_cc(address, *a, **k):
        if address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise OSError(f"外部への接続は禁止されています: {address}")
        return real_cc(address, *a, **k)

    monkeypatch.setattr(socket, "create_connection", guarded_cc)
    yield
