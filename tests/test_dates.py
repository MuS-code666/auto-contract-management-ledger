from datetime import date

from app import dates as D
from app import deadlines as DL


def test_seireki_variants():
    assert D.parse_date("2024年4月1日").iso == "2024-04-01"
    assert D.parse_date("２０２４年４月１日").iso == "2024-04-01"
    assert D.parse_date("2024/4/1").iso == "2024-04-01"
    assert D.parse_date("2024-04-01").iso == "2024-04-01"
    assert D.parse_date("二〇二四年四月一日").iso == "2024-04-01"


def test_wareki():
    assert D.parse_date("令和6年3月15日").iso == "2024-03-15"
    assert D.parse_date("令和元年5月1日").iso == "2019-05-01"
    assert D.parse_date("令和六年十二月二十五日").iso == "2024-12-25"
    assert D.parse_date("平成31年4月30日").iso == "2019-04-30"
    assert D.parse_date("昭和64年1月7日").iso == "1989-01-07"
    assert D.parse_date("R6.4.1").iso == "2024-04-01"
    p = D.parse_date("令和6年3月15日")
    assert p.raw == "令和6年3月15日"  # 原文を保持


def test_era_out_of_range_is_flagged():
    p = D.parse_date("平成32年3月31日")  # 改元後の旧元号表記
    assert p.status == "era_out_of_range" and p.iso == "2020-03-31"
    p = D.parse_date("令和元年4月1日")  # 令和は5/1から
    assert p.status == "era_out_of_range"


def test_incomplete_and_blank_are_not_completed():
    p = D.parse_date("令和　　年　　月　　日")
    assert p.status == "blank" and p.value is None
    p = D.parse_date("2024年4月")
    assert p.status == "incomplete" and p.value is None
    p = D.parse_date("令和6年　月　日")
    assert p.status == "incomplete" and p.value is None


def test_nonexistent_dates():
    assert D.parse_date("2023年2月29日").status == "invalid"
    assert D.parse_date("2024年2月29日").status == "ok"
    assert D.parse_date("2024年4月31日").status == "invalid"


def test_period_expressions_are_not_dates():
    assert D.find_dates("本契約の有効期間は1年間とする") == []
    assert D.find_dates("毎年4月1日に見直す") == []


def test_add_months_month_end_and_leap():
    assert D.add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert D.add_months(date(2023, 1, 31), 1) == date(2023, 2, 28)
    assert D.add_months(date(2024, 5, 31), -3) == date(2024, 2, 29)
    assert D.add_months(date(2025, 3, 31), -3) == date(2024, 12, 31)
    assert D.add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)


def test_period_end():
    assert D.period_end(date(2024, 4, 1), 12) == date(2025, 3, 31)
    assert D.period_end(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert D.period_end(date(2024, 1, 31), 1) == date(2024, 2, 29)  # 応当日(2/31)なし→月末
    assert D.period_end(date(2024, 3, 31), 1) == date(2024, 4, 30)
    assert D.period_end(date(2024, 7, 1), 24) == date(2026, 6, 30)


def test_subtract_period():
    assert D.subtract_period(date(2025, 3, 31), 30, "日") == date(2025, 3, 1)
    assert D.subtract_period(date(2025, 3, 31), 3, "月") == date(2024, 12, 31)
    assert D.subtract_period(date(2025, 2, 28), 1, "年") == date(2024, 2, 28)


def test_parse_period():
    assert D.parse_period("3か月") == (3, "月")
    assert D.parse_period("３ヶ月") == (3, "月")
    assert D.parse_period("1年間") == (1, "年")
    assert D.parse_period("30日") == (30, "日")
    assert D.parse_period("六箇月") == (6, "月")


def test_notice_candidate():
    c = {"expiry_type": "日付指定", "expiry_date": "2025-03-31", "renewal_notice_json": {"value": 3, "unit": "月"}}
    r = DL.notice_candidate(c)
    assert r["date"] == "2024-12-31" and "応当日" in r["calc"]
    # 管理上の期限（覚書確認後）があればそれを優先し、元の有効期限は変えない
    c2 = dict(c, managed_expiry_date="2026-03-31")
    assert DL.notice_candidate(c2)["date"] == "2025-12-31"
    # 条件終了・期限不明は計算しない
    assert DL.notice_candidate({"expiry_type": "条件終了", "renewal_notice_json": {"value": 3, "unit": "月"}})["date"] is None
    assert DL.notice_candidate({"expiry_type": "日付指定", "expiry_date": "2025-03-31"})["date"] is None


def test_next_term_candidate_is_candidate_only():
    c = {"auto_renewal": "あり", "renewal_period_months": 12, "expiry_date": "2025-03-31"}
    r = DL.next_term_candidate(c)
    assert r["date"] == "2026-03-31" and "確認" in r["calc"]
    assert DL.next_term_candidate({"auto_renewal": "不明", "renewal_period_months": 12, "expiry_date": "2025-03-31"}) is None


def test_classify_includes_undetermined():
    today = date(2025, 1, 15)
    c = {"expiry_type": "記載なし", "review_status": "確認済み", "field_meta": {}, "auto_renewal": "不明"}
    assert "日付未確定" in DL.classify(c, today, 90, 60)
    c = {"expiry_type": "日付指定", "expiry_date": "2025-03-31", "review_status": "確認済み", "field_meta": {},
         "auto_renewal": "あり", "notice_deadline_candidate": "2024-12-31"}
    b = DL.classify(c, today, 90, 60)
    assert "期限が近い" in b and "通知期限経過" in b and "日付未確定" in b
