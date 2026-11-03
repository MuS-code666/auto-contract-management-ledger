"""期限・通知期限の候補計算と、期限一覧の分類。

原文・構造化条件・計算候補・利用者の確定値を別々に扱う。計算候補は確定値ではなく、
時刻の経過だけで更新成立や契約継続を確定しない。
"""
from __future__ import annotations

from datetime import date, timedelta

from . import dates as D


def effective_expiry(c: dict) -> tuple[date | None, str]:
    """管理上の期限（関連書類確認後に利用者が確定したもの）があれば優先する。"""
    m = D.parse_iso(c.get("managed_expiry_date"))
    if m:
        return m, "管理上の期限（確定）"
    e = D.parse_iso(c.get("expiry_date"))
    if e:
        return e, "有効期限"
    return None, ""


def notice_candidate(c: dict) -> dict:
    """更新拒絶の通知期限の候補日を計算する。計算できない場合は理由を返す。"""
    st = c.get("renewal_notice_json") or None
    exp, label = effective_expiry(c)
    if not st or not st.get("value") or not st.get("unit"):
        return {"date": None, "calc": "通知条件（何日／何か月前か）が構造化されていないため計算できません"}
    if c.get("expiry_type") not in ("日付指定", None, "") and not c.get("managed_expiry_date"):
        return {"date": None, "calc": f"期限の区分が「{c.get('expiry_type')}」のため計算しません"}
    if exp is None:
        return {"date": None, "calc": "有効期限の日付が確定していないため計算できません"}
    try:
        v = int(st["value"])
        d = D.subtract_period(exp, v, st["unit"])
    except (ValueError, TypeError):
        return {"date": None, "calc": "通知条件の値が不正です"}
    unit = {"日": "日", "月": "か月", "年": "年"}[st["unit"]]
    rule = "暦日で減算" if st["unit"] == "日" else "応当日。応当日が無い月はその月の末日"
    calc = f"{label} {D.fmt_jp(exp)} の {v}{unit}前（{rule}）＝ {D.fmt_jp(d)}"
    if v and st["unit"] != "日":
        calc += "。「〜前までに」の解釈（当日を含むか等）は条文を確認してください"
    return {"date": d.isoformat(), "calc": calc}


def next_term_candidate(c: dict) -> dict | None:
    """自動更新された場合の次回満了日の候補（更新成立は未確認）。"""
    if c.get("auto_renewal") != "あり" or not c.get("renewal_period_months"):
        return None
    exp, label = effective_expiry(c)
    if exp is None:
        return None
    months = int(c["renewal_period_months"])
    nxt = D.period_end(exp + timedelta(days=1), months)
    return {"date": nxt.isoformat(),
            "calc": f"{label} {D.fmt_jp(exp)} の翌日から {months}か月（応当日の前日、応当日が無い場合は月末）＝ {D.fmt_jp(nxt)}。"
                    "更新が成立したかどうかは確認してください"}


def effective_notice(c: dict) -> tuple[date | None, bool]:
    conf = D.parse_iso(c.get("notice_deadline_confirmed"))
    if conf:
        return conf, True
    return D.parse_iso(c.get("notice_deadline_candidate")), False


def classify(c: dict, today: date, days_expiry: int, days_notice: int) -> list[str]:
    """期限一覧での分類。1件が複数に該当してよい。"""
    out = []
    exp, _ = effective_expiry(c)
    if exp:
        if exp < today:
            out.append("期限経過")
        elif exp <= today + timedelta(days=days_expiry):
            out.append("期限が近い")
    nd, confirmed = effective_notice(c)
    if nd:
        if nd < today and exp and exp >= today:
            out.append("通知期限経過")
        elif today <= nd <= today + timedelta(days=days_notice):
            out.append("通知期限が近い")
    meta = c.get("field_meta") or {}
    if c.get("review_status") != "確認済み" or any((m or {}).get("state") == "要確認" for m in meta.values()):
        out.append("要確認")
    undetermined = (
        c.get("expiry_type") in (None, "", "記載なし", "要確認")
        or (c.get("expiry_type") == "日付指定" and not c.get("expiry_date") and not c.get("managed_expiry_date"))
        or (c.get("auto_renewal") == "あり" and not c.get("notice_deadline_confirmed"))
        or (c.get("auto_renewal") == "不明" and c.get("expiry_type") == "日付指定")  # 満了日のある契約の更新有無
    )
    if undetermined:
        out.append("日付未確定")
    nr = D.parse_iso(c.get("next_review_date"))
    if nr and nr <= today + timedelta(days=7):
        out.append("確認予定日")
    return out
