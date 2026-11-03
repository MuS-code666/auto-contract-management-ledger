"""台帳API（手動登録・検索・編集・履歴・削除・重複・カテゴリー・関連）。OCR・LLMは無効。"""
from pathlib import Path

from .conftest import H, import_and_register, make_client, upload, wait_import


def manual(c, **kw):
    cat = c.get("/api/meta").json()["categories"][0]["id"]
    form = {"title": "手動契約", "category_id": cat, "auto_renewal": "不明", "review_status": "未確認",
            "parties": [{"name": "架空建設株式会社", "role": "当社"}, {"name": "株式会社テスト", "role": "相手方"}]}
    form.update(kw)
    return c.post("/api/contracts", json={"form": form, "ack_duplicates": True}, headers=H)


def test_security_guards(client):
    assert client.post("/api/contracts", json={}).status_code == 403  # 独自ヘッダーなし
    assert client.get("/api/meta", headers={"host": "evil.example.com"}).status_code == 400
    r = client.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_manual_register_without_pdf_and_search(client):
    r = manual(client, title="駐車場賃貸借契約書", contract_date="2024-01-10", expiry_type="期間の定めなし",
               field_states={"expiry_date": "記載なし"})
    assert r.status_code == 200 and r.json()["mgmt_no"] == "C000001"
    r2 = manual(client, title="別の契約", parties=[{"name": "株式会社別件", "role": "相手方"}])
    assert r2.json()["mgmt_no"] == "C000002"
    res = client.get("/api/contracts", params={"q": "駐車場"}).json()
    assert res["total"] == 1
    res = client.get("/api/contracts", params={"q": "株式会社別件"}).json()
    assert res["total"] == 1 and res["items"][0]["mgmt_no"] == "C000002"
    res = client.get("/api/contracts", params={"date_field": "contract_date", "date_from": "2024-01-01", "date_to": "2024-01-31"}).json()
    assert res["total"] == 1
    d = client.get(f"/api/contracts/{r.json()['id']}").json()
    assert d["field_meta"]["expiry_date"]["state"] == "記載なし"  # 空欄と区別
    res = client.get("/api/contracts", params={"sort": "title", "order": "asc", "per_page": 1, "page": 2}).json()
    assert res["total"] == 2 and len(res["items"]) == 1


def test_invalid_input_message_is_japanese(client):
    r = manual(client, contract_date="2024-02-30")
    assert r.status_code == 422 and "契約日" in r.json()["hint"]


def test_import_register_and_pdf_storage(client, tmp_path):
    src = Path(__file__).resolve().parent.parent / "samples" / "01_産業廃棄物処理委託契約書_文字PDF.pdf"
    before = src.read_bytes()
    reg = import_and_register(client, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    c = client.get(f"/api/contracts/{reg['id']}").json()
    assert c["pdf_relpath"] == "廃棄物処理関連/C000001_産業廃棄物処理委託契約書_東西環境サービス株式会社.pdf"
    assert c["pdf_exists"] and src.read_bytes() == before  # 元ファイルは変更しない
    pdf = client.get(f"/api/contracts/{reg['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content == before  # 保存PDFの内容は不変
    assert c["notice_deadline_candidate"] == "2024-12-31"
    assert c["field_meta"]["expiry_date"]["page"] == 1
    assert c["field_meta"]["expiry_date"]["state"] == "未確認"
    pages = client.get(f"/api/contracts/{reg['id']}/pages").json()
    assert [p["text_source"] for p in pages] == ["text", "text"]


def test_same_hash_is_blocked(client):
    reg = import_and_register(client, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    r = upload(client, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    assert r.status_code == 409 and r.json()["mgmt_no"] == reg["mgmt_no"]
    client.post(f"/api/contracts/{reg['id']}/delete", headers=H)
    r = upload(client, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    assert r.status_code == 409 and "削除済み" in r.json()["error"]


def test_rescan_duplicate_candidates_need_ack(client):
    reg = import_and_register(client, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    first = client.get(f"/api/contracts/{reg['id']}").json()
    r = upload(client, "02_産業廃棄物処理委託契約書_スキャン.pdf")  # 内容は同じ・ハッシュは異なる
    assert r.status_code == 200
    d = wait_import(client, r.json()["id"])
    assert d["status"] == "partial"  # OCR無効 → 未処理ページあり
    assert any("読み取れていない" in w for w in d["result"]["warnings"])
    form = d["form"]
    form.update(title=first["title"], contract_date=first["contract_date"], category_id=first["category_id"],
                parties=[{"name": p["name"], "role": p["role"], "label": p["label"]} for p in first["parties"]])
    r = client.post(f"/api/imports/{d['id']}/register", json={"form": form}, headers=H)
    assert r.status_code == 409 and r.json()["duplicates"][0]["mgmt_no"] == reg["mgmt_no"]
    r = client.post(f"/api/imports/{d['id']}/register", json={"form": form, "ack_duplicates": True}, headers=H)
    assert r.status_code == 200 and r.json()["mgmt_no"] == "C000002"  # 自動で同一扱いしない


def test_edit_history_and_soft_delete(client):
    cid = manual(client).json()["id"]
    c = client.get(f"/api/contracts/{cid}").json()
    form = {k: c[k] for k in ("title", "category_id", "auto_renewal", "review_status")}
    form["parties"] = [{"name": p["name"], "role": p["role"]} for p in c["parties"]]
    form.update(title="手動契約（修正）", owner="総務部 山田")
    assert client.put(f"/api/contracts/{cid}", json={"form": form}, headers=H).status_code == 200
    hist = client.get(f"/api/contracts/{cid}/history").json()
    ch = {x["field"]: x for x in hist[0]["changes"]}
    assert ch["title"]["old"] == "手動契約" and ch["title"]["new"] == "手動契約（修正）"
    assert client.get(f"/api/contracts/{cid}").json()["field_meta"]["title"]["state"] == "修正済み"
    client.post(f"/api/contracts/{cid}/delete", headers=H)
    assert client.get("/api/contracts").json()["total"] == 0
    assert client.get("/api/contracts", params={"deleted": "only"}).json()["total"] == 1
    assert client.put(f"/api/contracts/{cid}", json={"form": form}, headers=H).status_code == 400
    client.post(f"/api/contracts/{cid}/restore", headers=H)
    assert client.get("/api/contracts").json()["total"] == 1
    actions = [x["action"] for x in client.get(f"/api/contracts/{cid}/history").json()]
    assert actions[:2] == ["復元", "削除（論理）"]


def test_category_change_moves_pdf_and_keeps_filename(client):
    reg = import_and_register(client, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    ctx = client.app.state.ctx
    c = client.get(f"/api/contracts/{reg['id']}").json()
    old_abs = Path(c["pdf_abspath"])
    other = [x for x in client.get("/api/meta").json()["categories"] if x["name"] == "業務委託"][0]
    form = {k: c[k] for k in ("title", "auto_renewal", "review_status", "expiry_date", "expiry_type")}
    form["category_id"] = other["id"]
    form["title"] = "名称を修正"  # 契約名を修正してもファイル名は変えない
    assert client.put(f"/api/contracts/{reg['id']}", json={"form": form}, headers=H).status_code == 200
    c2 = client.get(f"/api/contracts/{reg['id']}").json()
    assert c2["pdf_relpath"] == "業務委託/" + c["pdf_relpath"].split("/")[1]
    assert c2["pdf_exists"] and not old_abs.exists()


def test_category_rename_preview_and_move(client):
    reg = import_and_register(client, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    cat = client.get(f"/api/contracts/{reg['id']}").json()["category_id"]
    pv = client.post(f"/api/categories/{cat}/rename-preview", json={"name": "産廃関連"}, headers=H).json()
    assert len(pv["affected"]) == 1
    bad = client.post(f"/api/categories/{cat}/rename-preview", json={"name": "a/b"}, headers=H)
    assert bad.status_code == 400
    r = client.post(f"/api/categories/{cat}/rename", json={"name": "産廃関連"}, headers=H).json()
    assert r["moved"] == 1 and not r["failed"]
    c = client.get(f"/api/contracts/{reg['id']}").json()
    assert c["pdf_relpath"].startswith("産廃関連/") and c["pdf_exists"]


def test_inactive_category_rejects_new(client):
    cat = client.get("/api/meta").json()["categories"][0]["id"]
    client.put(f"/api/categories/{cat}", json={"active": False}, headers=H)
    r = manual(client, category_id=cat)
    assert r.status_code == 400 and "無効" in r.json()["error"]


def test_relations_do_not_overwrite_original(client):
    a = manual(client, title="基本契約", expiry_type="日付指定", expiry_date="2025-03-31").json()
    b = manual(client, title="変更覚書", parties=[{"name": "株式会社X", "role": "相手方"}]).json()
    r = client.post(f"/api/contracts/{a['id']}/relations", json={"rel_type": "原契約→変更覚書", "other_mgmt_no": b["mgmt_no"]}, headers=H)
    assert r.status_code == 200
    ca = client.get(f"/api/contracts/{a['id']}").json()
    assert ca["relations"][0]["other_mgmt_no"] == b["mgmt_no"] and ca["expiry_date"] == "2025-03-31"
    assert client.get(f"/api/contracts/{b['id']}").json()["relations"][0]["direction"] == "相手→この契約"


def test_storage_root_new_only_vs_migrate(client, tmp_path):
    reg = import_and_register(client, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    r = client.post("/api/storage-roots", json={"path": str(tmp_path / "newroot"), "mode": "new_only"}, headers=H).json()
    assert r["migrated"] == 0
    c = client.get(f"/api/contracts/{reg['id']}").json()
    assert c["pdf_exists"] and "newroot" not in c["pdf_abspath"]  # 既存は元の場所
    r = client.post("/api/storage-roots", json={"path": str(tmp_path / "root3"), "mode": "migrate"}, headers=H).json()
    assert r["migrated"] == 1 and not r["failed"]
    c = client.get(f"/api/contracts/{reg['id']}").json()
    assert c["pdf_exists"] and "root3" in c["pdf_abspath"]
    assert client.get("/api/integrity").json()["pdf_problems"] == []
    rel = client.post("/api/storage-roots", json={"path": "relative\\path", "mode": "new_only"}, headers=H)
    assert rel.status_code == 400


def test_corrupt_and_limits(client):
    r = upload(client, "90_破損PDF.pdf")
    assert r.status_code == 400 and "読み込めません" in r.json()["error"] and r.json()["hint"]
    client.put("/api/settings", json={"max_pages": 1}, headers=H)
    r = upload(client, "08_業務委託契約書_複数ページ.pdf")
    assert r.status_code == 400 and "上限" in r.json()["error"]


def test_encrypted_pdf(client, tmp_path):
    from reportlab.lib import pdfencrypt
    from reportlab.pdfgen import canvas

    p = tmp_path / "enc.pdf"
    cv = canvas.Canvas(str(p), encrypt=pdfencrypt.StandardEncryption("secret", canPrint=0))
    cv.drawString(100, 700, "secret")
    cv.save()
    with open(p, "rb") as f:
        r = client.post("/api/imports", files={"file": ("enc.pdf", f, "application/pdf")}, headers=H)
    assert r.status_code == 400 and "パスワード" in r.json()["error"]


def test_draft_resume_and_user_edits_survive_reanalysis(client):
    r = upload(client, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    iid = r.json()["id"]
    d = wait_import(client, iid)
    form = d["form"]
    form["title"] = "利用者が修正した名称"
    client.put(f"/api/imports/{iid}/draft", json={"form": form, "user_edited": ["title"]}, headers=H)
    # 同じPDFを再度選ぶと、下書きを再開する
    assert upload(client, "01_産業廃棄物処理委託契約書_文字PDF.pdf").json() == {"id": iid, "resumed": True}
    assert client.post(f"/api/imports/{iid}/analyze", headers=H).status_code == 200
    d = wait_import(client, iid)
    assert d["form"]["title"] == "利用者が修正した名称"  # 再解析で上書きしない
    assert d["form"]["expiry_date"] == "2025-03-31"
    assert d["result"]["extraction"]["fields"]["title"]["value"] == "産業廃棄物処理委託契約書"  # 抽出値は別に保持


def test_register_blocked_during_analysis(client):
    ctx = client.app.state.ctx
    r = upload(client, "03_秘密保持契約書_3者.pdf")
    iid = r.json()["id"]
    wait_import(client, iid)
    conn = ctx.connect()
    conn.execute("UPDATE imports SET status='processing' WHERE id=?", (iid,))  # 解析中の状態を再現
    conn.close()
    cat = client.get("/api/meta").json()["categories"][0]["id"]
    r = client.post(f"/api/imports/{iid}/register", json={"form": {"title": "x", "category_id": cat}}, headers=H)
    assert r.status_code == 400 and "解析中" in r.json()["error"]


def test_restart_keeps_ledger_and_pdf(tmp_path):
    c = make_client(tmp_path / "d")
    reg = import_and_register(c, "01_産業廃棄物処理委託契約書_文字PDF.pdf")
    c.app.state.ctx.jobs.stop()
    c2 = make_client(tmp_path / "d")
    d = c2.get(f"/api/contracts/{reg['id']}").json()
    assert d["mgmt_no"] == "C000001" and d["pdf_exists"]
    assert c2.get(f"/api/contracts/{reg['id']}/pdf").status_code == 200
    c2.app.state.ctx.jobs.stop()


def test_deadlines_board(client):
    manual(client, title="期限切れ", expiry_type="日付指定", expiry_date="2020-01-01", review_status="確認済み")
    manual(client, title="期限不明", expiry_type="記載なし", parties=[{"name": "B社", "role": "相手方"}])
    b = client.get("/api/deadlines").json()["buckets"]
    assert any(x["title"] == "期限切れ" for x in b["期限経過"])
    assert any(x["title"] == "期限不明" for x in b["日付未確定"])
    assert any(x["title"] == "期限不明" for x in b["要確認"])
