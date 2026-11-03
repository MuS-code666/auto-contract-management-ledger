"""架空サンプル契約書の正解データ（抽出精度の評価用）。

値が None の項目は「契約書に記載がない／日付として特定できない」ことが正解。
"""

EXPECTED = {
    "01_産業廃棄物処理委託契約書_文字PDF.pdf": {
        "title": "産業廃棄物処理委託契約書", "doc_type": "業務委託契約", "category": "廃棄物処理関連",
        "contract_date": "2024-03-15", "start_date": "2024-04-01", "expiry_date": "2025-03-31",
        "expiry_type": "日付指定", "auto_renewal": "あり", "renewal_period_months": 12,
        "renewal_notice": (3, "月"), "termination_notice": (1, "月"),
        "parties": {"甲": "架空建設株式会社", "乙": "東西環境サービス株式会社"},
        "signers": {"見本 太郎", "試験 次郎"},
    },
    "03_秘密保持契約書_3者.pdf": {
        "title": "秘密保持契約書", "doc_type": "秘密保持契約", "category": "秘密保持",
        "contract_date": "2024-07-01", "start_date": "2024-07-01", "expiry_date": "2026-06-30",
        "expiry_type": "日付指定", "auto_renewal": "不明", "renewal_period_months": None,
        "renewal_notice": None, "termination_notice": None,
        "parties": {"甲": "架空建設株式会社", "乙": "株式会社サンプル設計", "丙": "合同会社テスト測量"},
        "signers": {"見本 太郎", "例示 花子", "模擬 三郎"},
    },
    "04_解体工事請負契約書_条件終了.pdf": {
        "title": "解体工事請負契約書", "doc_type": "請負契約", "category": "解体工事関連",
        "contract_date": "2024-04-10", "start_date": "2024-05-01", "expiry_date": None,
        "expiry_type": "条件終了", "auto_renewal": "不明", "renewal_period_months": None,
        "renewal_notice": None, "termination_notice": None,
        "parties": {"甲": "架空建設株式会社", "乙": "株式会社見本解体工業"},
        "signers": {"見本 太郎", "解体 四郎"},
    },
    "05_覚書_料金変更.pdf": {
        "title": "覚書", "doc_type": "覚書", "category": None,
        "contract_date": "2025-03-10", "start_date": "2025-04-01", "expiry_date": None,
        "expiry_type": "条件終了", "auto_renewal": "不明", "renewal_period_months": None,
        "renewal_notice": None, "termination_notice": None,
        "parties": {"甲": "架空建設株式会社", "乙": "東西環境サービス株式会社"},
        "signers": {"見本 太郎", "試験 次郎"},
    },
    "06_駐車場賃貸借契約書_期間定めなし.pdf": {
        "title": "駐車場賃貸借契約書", "doc_type": "賃貸借契約", "category": "不動産",
        "contract_date": "2023-09-20", "start_date": "2023-10-01", "expiry_date": None,
        "expiry_type": "期間の定めなし", "auto_renewal": "不明", "renewal_period_months": None,
        "renewal_notice": None, "termination_notice": (2, "月"),
        "parties": {"甲": "見本 花代", "乙": "架空建設株式会社"},
        "signers": {"見本 花代", "見本 太郎"},
    },
    "07_車両保守業務委託契約書_矛盾空欄.pdf": {
        # 契約日は空欄、契約期間・自動更新は条文が矛盾 → 要確認であることが正解
        "title": "車両保守業務委託契約書", "doc_type": "業務委託契約", "category": "業務委託",
        "contract_date": None, "start_date": "2024-10-01", "expiry_date": "要確認",
        "expiry_type": "要確認", "auto_renewal": "要確認", "renewal_period_months": None,
        "renewal_notice": (1, "月"), "termination_notice": None,
        "parties": {"甲": "架空建設株式会社", "乙": "株式会社テスト自動車整備"},
        "signers": {"見本 太郎", "整備 五郎"},
    },
    "08_業務委託契約書_複数ページ.pdf": {
        "title": "業務委託契約書", "doc_type": "業務委託契約", "category": "業務委託",
        "contract_date": "2024-12-10", "start_date": "2025-01-01", "expiry_date": "2025-12-31",
        "expiry_type": "日付指定", "auto_renewal": "あり", "renewal_period_months": 12,
        "renewal_notice": (30, "日"), "termination_notice": None,
        "parties": {"甲": "架空建設株式会社", "乙": "株式会社モデル清掃"},
        "signers": {"見本 太郎", "清掃 六子"},
    },
}
# 評価専用（ルール調整に使っていない）サンプル。samples/holdout/
HOLDOUT = {
    "holdout/H1_事務所建物賃貸借契約書.pdf": {
        "title": "事務所建物賃貸借契約書", "doc_type": "賃貸借契約", "category": "不動産",
        "contract_date": "2025-03-03", "start_date": "2025-04-01", "expiry_date": "2028-03-31",
        "expiry_type": "日付指定", "auto_renewal": "あり", "renewal_period_months": 24,
        "renewal_notice": (6, "月"), "termination_notice": (3, "月"),
        "parties": {"甲": "有限会社ミホン不動産", "乙": "架空建設株式会社"},
        "signers": {"見本 七海", "見本 太郎"},
    },
    "holdout/H2_設備保守点検業務委託契約書.pdf": {
        "title": "設備保守点検業務委託契約書", "doc_type": "業務委託契約", "category": "設備・車両",
        "contract_date": "2025-06-20", "start_date": "2025-07-01", "expiry_date": "2026-06-30",
        "expiry_type": "日付指定", "auto_renewal": "あり", "renewal_period_months": 12,
        "renewal_notice": (2, "月"), "termination_notice": None,
        "parties": {"甲": "架空建設株式会社", "乙": "テスト設備メンテナンス株式会社"},
        "signers": {"見本 太郎", "点検 八郎"},
    },
    "holdout/H3_資材売買基本契約書.pdf": {
        "title": "資材売買基本契約書", "doc_type": "売買契約", "category": None,
        "contract_date": "2024-12-20", "start_date": "2025-01-01", "expiry_date": "2026-12-31",
        "expiry_type": "日付指定", "auto_renewal": "不明", "renewal_period_months": None,
        "renewal_notice": None, "termination_notice": None,
        "parties": {"甲": "株式会社サンプル資材", "乙": "架空建設株式会社"},
        "signers": {"資材 九男", "見本 太郎"},
    },
}
HOLDOUT["holdout/H4_設備保守点検業務委託契約書_スキャン.pdf"] = HOLDOUT["holdout/H2_設備保守点検業務委託契約書.pdf"]

# 画像だけのスキャン版（OCRで読む）は、元の文字PDFと同じ正解
EXPECTED["02_産業廃棄物処理委託契約書_スキャン.pdf"] = EXPECTED["01_産業廃棄物処理委託契約書_文字PDF.pdf"]
EXPECTED["09_解体工事請負契約書_低品質スキャン.pdf"] = EXPECTED["04_解体工事請負契約書_条件終了.pdf"]
