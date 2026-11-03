"""途中失敗からの復旧、バックアップ・復元、Excel出力、オフライン動作。"""
import io
import sqlite3
import zipfile
from pathlib import Path

import pytest
from openpyxl import load_workbook

from app import repo as R
from app import storage as S

from .conftest import H, SAMPLES, import_and_register, make_client, upload, wait_import


def _form(c, **kw):
    cat = c.get("/api/meta").json()["categories"][0]["id"]
    f = {"title": "テスト契約", "category_id": cat, "parties": [{"name": "株式会社A", "role": "相手方"}]}
    f.update(kw)
    return R.ContractIn.model_validate(f)


def _files(root: Path):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_register_failure_after_copy_rolls_back(client):
    ctx = client.app.state.ctx
    conn = ctx.connect()
    pdf = SAMPLES / "05_覚書_料金変更.pdf"
    with pytest.raises(OSError):
        R.register_contract(conn, _form(client), pdf_src=pdf, file_hash=S.sha256_file(pdf), fail_after="copy")
    root = ctx.paths.default_storage_root
    assert _files(root) == []  # 保存先ファイルは巻き戻し
    assert conn.execute("SELECT state FROM file_ops").fetchone()["state"] == "rolled_back"
    # 管理番号は再利用しない
    c = R.register_contract(conn, _form(client), pdf_src=pdf, file_hash=S.sha256_file(pdf))
    assert c["mgmt_no"] == "C000002"
    conn.close()


def test_register_failure_in_db_rolls_back(client):
    ctx = client.app.state.ctx
    conn = ctx.connect()
    pdf = SAMPLES / "05_覚書_料金変更.pdf"
    with pytest.raises(sqlite3.OperationalError):
        R.register_contract(conn, _form(client), pdf_src=pdf, file_hash=S.sha256_file(pdf), fail_after="db")
    assert conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 0
    assert _files(ctx.paths.default_storage_root) == []
    conn.close()


def test_crash_during_register_recovered_on_startup(tmp_path):
    """プロセスが落ちて journal が pending/file_done のまま残った状態を再現し、再起動で復旧する。"""
    c = make_client(tmp_path / "d")
    ctx = c.app.state.ctx
    conn = ctx.connect()
    rid, root = S.default_root(conn)
    pdf = SAMPLES / "05_覚書_料金変更.pdf"
    h = S.sha256_file(pdf)
    from app.db import tx
    with tx(conn):
        op = S.journal_start(conn, "register", contract_id="dead-beef", dst_root_id=rid, dst_rel="その他/C000099_x.pdf", file_hash=h)
    dst = S.safe_join(root, "その他", "C000099_x.pdf")
    S.copy_verified(pdf, dst, h)
    with tx(conn):
        S.journal_set(conn, op, "file_done")
    conn.close()
    c.app.state.ctx.jobs.stop()
    c2 = make_client(tmp_path / "d")  # 再起動
    assert not dst.exists()
    assert any(x["result"] == "rolled_back" for x in c2.app.state.ctx.recovery)
    assert c2.get("/api/integrity").json()["unresolved_file_ops"] == []
    c2.app.state.ctx.jobs.stop()


def test_move_failure_keeps_source_and_db(client):
    reg = import_and_register(client, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    ctx = client.app.state.ctx
    conn = ctx.connect()
    before = client.get(f"/api/contracts/{reg['id']}").json()
    rid = before["storage_root_id"]
    with pytest.raises(OSError):
        S.move_contract_pdf(conn, reg["id"], rid, "業務委託/" + before["pdf_relpath"].split("/")[1], fail_after="copy")
    after = client.get(f"/api/contracts/{reg['id']}").json()
    assert after["pdf_relpath"] == before["pdf_relpath"] and after["pdf_exists"]
    assert not (ctx.paths.default_storage_root / "業務委託" / before["pdf_relpath"].split("/")[1]).exists()
    # DB更新後に落ちた場合 → 移動元が残るが、復旧で削除され整合する
    with pytest.raises(OSError):
        S.move_contract_pdf(conn, reg["id"], rid, "業務委託/" + before["pdf_relpath"].split("/")[1], fail_after="db")
    src = ctx.paths.default_storage_root / before["pdf_relpath"]
    assert src.exists()
    res = S.recover(conn)
    assert any(x["result"] == "completed" for x in res) and not src.exists()
    final = client.get(f"/api/contracts/{reg['id']}").json()
    assert final["pdf_relpath"].startswith("業務委託/") and final["pdf_exists"]
    assert S.integrity_check(conn) == []
    conn.close()


def test_integrity_detects_tampered_pdf(client):
    reg = import_and_register(client, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    p = Path(client.get(f"/api/contracts/{reg['id']}").json()["pdf_abspath"])
    p.write_bytes(p.read_bytes() + b"x")
    probs = client.get("/api/integrity").json()["pdf_problems"]
    assert probs and "異なります" in probs[0]["problem"]


def test_backup_and_restore_roundtrip(client, tmp_path):
    reg = import_and_register(client, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    upload(client, "03_秘密保持契約書_3者.pdf")  # 下書きも対象
    b = client.post("/api/backups", headers=H).json()
    assert b["verify"]["ok"] and b["manifest"]["counts"] == {"contracts": 1, "pdfs": 1}
    # バックアップ後にデータを変更・PDFを削除
    import_and_register(client, "04_解体工事請負契約書_条件終了.pdf")
    pdf = Path(client.get(f"/api/contracts/{reg['id']}").json()["pdf_abspath"])
    pdf.unlink()
    pv = client.get(f"/api/backups/{b['name']}/preview").json()
    assert pv["can_restore"] and pv["current"]["contracts"] == 2 and pv["effects"]
    assert client.post(f"/api/backups/{b['name']}/restore", json={"confirm": "違う"}, headers=H).status_code == 400
    r = client.post(f"/api/backups/{b['name']}/restore", json={"confirm": b["name"]}, headers=H).json()
    assert r["contracts"] == 1 and r["pdf_problems"] == []
    assert pdf.exists()
    names = [x["name"] for x in client.get("/api/backups").json()["items"]]
    assert r["pre_restore_backup"] in names  # 復元前のデータを保全
    assert client.get(f"/api/contracts/{reg['id']}/pdf").status_code == 200
    assert len(client.get("/api/imports").json()) == 1
    # 復元前自動バックアップから、変更後の状態にも戻せる
    r2 = client.post(f"/api/backups/{r['pre_restore_backup']}/restore", json={"confirm": r["pre_restore_backup"]}, headers=H).json()
    assert r2["contracts"] == 2


def test_backup_rejects_zip_slip(client):
    ctx = client.app.state.ctx
    p = ctx.paths.backup_dir / "backup_evil.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("manifest.json", '{"files": [], "db_sha256": ""}')
        z.writestr("ledger.db", b"")
        z.writestr("../../evil.txt", "x")
    pv = client.get("/api/backups/backup_evil.zip/preview").json()
    assert not pv["can_restore"]
    assert client.get("/api/backups/..%5Cx.zip/preview").status_code in (400, 404)


def test_excel_export(client):
    cat = client.get("/api/meta").json()["categories"][0]["id"]
    client.post("/api/contracts", json={"ack_duplicates": True, "form": {
        "title": '=HYPERLINK("http://example.com","x")', "category_id": cat, "summary": "+cmd|' /C calc'!A0",
        "expiry_type": "日付指定", "expiry_date": "2025-03-31", "renewal_notice_json": {"value": 3, "unit": "月"},
        "auto_renewal": "あり", "parties": [{"name": "@SUM(1+1)", "role": "相手方"}],
        "field_states": {"contract_date": "記載なし", "start_date": "要確認"}}}, headers=H)
    import_and_register(client, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    r = client.get("/api/export.xlsx", params={"scope": "all"})
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert "<f>" not in sheet  # 数式は含まれない
    wb = load_workbook(io.BytesIO(r.content))
    ws = wb["契約台帳"]
    assert ws.freeze_panes == "C2" and ws.auto_filter.ref.startswith("A1:")
    head = [c.value for c in ws[1]]
    rows = {ws.cell(row=i, column=1).value: [c.value for c in ws[i]] for i in range(2, ws.max_row + 1)}
    r1 = dict(zip(head, rows["C000001"]))
    assert r1["契約書名"].startswith("'=")
    assert r1["契約日"] == "記載なし" and r1["契約開始日"] == "要確認"
    assert r1["通知期限（確定）"] is None and r1["通知期限（候補・未確定）"].startswith("候補: 2024/12/31")
    assert ws.cell(row=2, column=head.index("有効期限") + 1).number_format == "yyyy/mm/dd"
    r2 = dict(zip(head, rows["C000002"]))
    assert r2["PDFを開く"] == "開く" and r2["PDF保存先（管理ルートからの相対パス）"].startswith("廃棄物処理関連/")
    assert "説明" in wb.sheetnames
    # 検索結果だけの出力
    r = client.get("/api/export.xlsx", params={"scope": "filtered", "q": "産業廃棄物"})
    ws = load_workbook(io.BytesIO(r.content))["契約台帳"]
    assert ws.max_row == 2


def test_offline_full_flow(tmp_path, no_network):
    """ループバック以外の通信を遮断した状態で、取込み～登録～出力～バックアップが動作する。"""
    c = make_client(tmp_path / "d", ocr="rapidocr")
    reg = import_and_register(c, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    assert c.get("/api/export.xlsx", params={"scope": "all"}).status_code == 200
    assert c.post("/api/backups", headers=H).json()["verify"]["ok"]
    assert c.get(f"/api/contracts/{reg['id']}/pdf").status_code == 200
    c.app.state.ctx.jobs.stop()


def test_frontend_has_no_external_resources():
    static = Path(__file__).resolve().parent.parent / "app" / "static"
    for f in static.iterdir():
        text = f.read_text(encoding="utf-8")
        for bad in ("<script src=\"http", "href=\"http", "@import", "fonts.googleapis", "cdn."):
            assert bad not in text, f"{f.name}: {bad}"


def test_ocr_unavailable_is_explained(client):
    client.put("/api/settings", json={"ocr": {"engine": "tesseract", "tesseract_path": "C:\\nope\\tesseract.exe"}}, headers=H)
    r = upload(client, "02_産業廃棄物処理委託契約書_スキャン.pdf")
    d = wait_import(client, r.json()["id"])
    assert d["status"] == "partial"
    statuses = [p["status"] for p in d["pages"]]
    assert statuses[:2] == ["unprocessed", "unprocessed"] and statuses[2] == "empty"  # 3ページ目は空ページ
    assert any("OCR" in w and "README" in w or "tesseract" in w.lower() for w in d["result"]["warnings"])
    # ダミー結果を表示しない：値は空で、要確認になっている
    f = d["result"]["extraction"]["fields"]
    assert f["contract_date"]["value"] is None and f["contract_date"]["state"] == "要確認"
