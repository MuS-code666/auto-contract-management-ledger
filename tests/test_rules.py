"""ルール抽出（モデル不要）の検証。文字情報のある架空サンプルPDFを使う。"""
import pypdfium2 as pdfium
import pytest

from app.config import DEFAULT_CATEGORIES
from app.extract.common import Doc, Page
from app.extract.rules import extract_all
from app.pdftext import extract_page_text

from .conftest import SAMPLES

CATS = [{"name": n, "keywords": k} for n, k in DEFAULT_CATEGORIES]


def run(name, company=("架空建設株式会社",), drop_pages=()):
    pdf = pdfium.PdfDocument(str(SAMPLES / name))
    pages = []
    for i in range(len(pdf)):
        t = extract_page_text(pdf, i, 20)
        status = "unprocessed" if (i + 1) in drop_pages else ("ok" if t.usable else "unprocessed")
        pages.append(Page(i + 1, t.text if status == "ok" else "", "text", status))
    pdf.close()
    return extract_all(Doc(pages), CATS, list(company))


def v(r, k):
    return r["fields"][k]["value"]


def test_waste_contract_dates_renewal_and_evidence():
    r = run("01_産業廃棄物処理委託契約書_文字PDF.pdf")
    assert v(r, "title") == "産業廃棄物処理委託契約書"
    assert v(r, "contract_date") == "2024-03-15"  # 締結日
    assert v(r, "start_date") == "2024-04-01"     # 開始日は別に取得
    assert v(r, "expiry_date") == "2025-03-31"
    assert v(r, "auto_renewal") == "あり"
    assert r["fields"]["renewal_terms"]["renewal_period_months"] == 12
    assert r["fields"]["renewal_notice"]["structured"]["value"] == 3
    assert r["fields"]["termination_notice"]["structured"] == {"value": 1, "unit": "月", "base": "解約日", "text": "1か月前までに"}
    assert v(r, "category") == "廃棄物処理関連"
    f = r["fields"]["expiry_date"]
    assert f["page"] == 1 and "2025年3月31日" in f["evidence"] and f["source"] == "text"
    assert r["fields"]["contract_date"]["page"] == 2


def test_parties_and_signers_are_separate():
    r = run("01_産業廃棄物処理委託契約書_文字PDF.pdf")
    assert [(p["label"], p["name"], p["role"]) for p in r["parties"]] == [
        ("甲", "架空建設株式会社", "当社"), ("乙", "東西環境サービス株式会社", "相手方")]
    names = {s["name"] for s in r["signers"]}
    assert names == {"見本 太郎", "試験 次郎"}
    assert all(s["name"] not in [p["name"] for p in r["parties"]] for s in r["signers"])
    assert {s["party_label"] for s in r["signers"]} == {"甲", "乙"}


def test_three_party_nda_and_start_from_contract_date():
    r = run("03_秘密保持契約書_3者.pdf")
    assert [p["label"] for p in r["parties"]] == ["甲", "乙", "丙"]
    assert v(r, "contract_date") == "2024-07-01"  # 和暦（令和6年7月1日）
    assert r["fields"]["contract_date"]["raw"] == "令和6年7月1日"
    assert v(r, "start_date") == "2024-07-01"
    # 「締結日から2年間」→ 計算値は要確認として提示
    assert v(r, "expiry_date") == "2026-06-30" and r["fields"]["expiry_date"]["state"] == "要確認"
    # 更新の記載なし → 「なし」とせず「不明」
    assert v(r, "auto_renewal") == "不明"


def test_condition_end_does_not_invent_date():
    r = run("04_解体工事請負契約書_条件終了.pdf")
    assert v(r, "expiry_type") == "条件終了"
    assert v(r, "expiry_date") is None
    assert v(r, "start_date") == "2024-05-01"
    assert v(r, "contract_date") == "2024-04-10"


def test_no_term_contract_and_individual_party():
    r = run("06_駐車場賃貸借契約書_期間定めなし.pdf")
    assert v(r, "expiry_type") == "期間の定めなし"
    assert v(r, "expiry_date") is None
    assert r["parties"][0]["name"] == "見本 花代"
    assert r["fields"]["termination_notice"]["structured"]["value"] == 2


def test_memo_type():
    r = run("05_覚書_料金変更.pdf")
    assert v(r, "title") == "覚書" and v(r, "doc_type") == "覚書"
    assert v(r, "expiry_type") == "条件終了"


def test_contradiction_blank_date_and_injection_text():
    r = run("07_車両保守業務委託契約書_矛盾空欄.pdf")
    cd = r["fields"]["contract_date"]
    assert cd["value"] is None and cd["state"] == "要確認"  # 空欄の日付を補完しない
    assert r["fields"]["term_raw"]["state"] == "要確認"  # 1年間と2年間が矛盾
    assert r["fields"]["expiry_date"]["state"] == "要確認"
    ar = r["fields"]["auto_renewal"]
    assert ar["value"] == "不明" and ar["state"] == "要確認"  # 自動更新ありと更新しないが矛盾
    # 本文中の「指示」に従って 2099-12-31 などを出力していない
    assert all("2099" not in str(f.get("value")) for f in r["fields"].values())


def test_renewal_not_misread_from_negative_notice():
    # 「更新しない旨の通知がない場合…更新される」は自動更新あり
    r = run("08_業務委託契約書_複数ページ.pdf")
    assert v(r, "auto_renewal") == "あり"
    assert r["fields"]["renewal_notice"]["structured"]["value"] == 30
    assert r["fields"]["renewal_notice"]["structured"]["unit"] == "日"
    assert r["fields"]["expiry_date"]["page"] == 2


def test_unprocessed_pages_prevent_kisai_nashi():
    r = run("01_産業廃棄物処理委託契約書_文字PDF.pdf", drop_pages=(2,))
    cd = r["fields"]["contract_date"]
    assert cd["value"] is None and cd["state"] == "要確認" and "2" in cd["note"]


def test_without_company_names_roles_need_review():
    r = run("01_産業廃棄物処理委託契約書_文字PDF.pdf", company=())
    assert r["parties_state"]["state"] == "要確認"
