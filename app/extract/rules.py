"""ルール（正規表現）による抽出。LLMが無くても動作する。

方針：
- 記載がない情報は推測しない（missing() で「記載なし」または「要確認」）。
- 日付は原文を保持し、欠けた年月日を補完しない。
- 本文中の社名すべてを当事者とせず、「以下「甲」という」等の定義、または署名欄の当事者表示だけを採る。
- 当事者（法人名）と署名者（代表者等）は別に扱う。
- 更新条件が見つからない場合は「不明」とし、「なし」にしない。
- 矛盾する記載は「要確認」にする。
"""
from __future__ import annotations

import re
import unicodedata

from .. import dates as D
from .common import (STATE_CHECK, STATE_FOUND, STATE_NONE, USABLE, Doc, field, missing, norm)

LABELS = "甲乙丙丁戊己"
_N = r"[0-9０-９〇一二三四五六七八九十]+"
_UNIT = r"(?:日|か月|ヵ月|ヶ月|カ月|ケ月|箇月|ヵ年|年)"

TITLE_RE = re.compile(r"(契約書|覚書|合意書|協定書|念書|確認書|約定書|契約)$")


def _src(doc: Doc, page: int | None) -> str | None:
    return doc.source_of(page) if page else None


# ---------------------------------------------------------------- 表題・種類

def extract_title(doc: Doc) -> dict:
    for p in doc.pages[:2]:
        if p.status not in USABLE:
            continue
        lines = [l.strip() for l in p.text.split("\n") if l.strip()]
        for line in lines[:15]:
            compact = re.sub(r"[\s　]+", "", line)
            if 2 <= len(compact) <= 40 and TITLE_RE.search(compact) and not compact.startswith("第"):
                if compact.startswith("本契約") or re.search(r"(以下|において|により|する)", compact):
                    continue
                return field(compact, raw=line, page=p.page_no, evidence=line, source=p.source)
    return missing(doc, "表題が見つかりません") | {"state": STATE_CHECK, "note": "表題が見つかりません。PDFを確認して入力してください"}


TYPE_RULES = [
    ("覚書", ("覚書",)),
    ("秘密保持契約", ("秘密保持", "機密保持", "守秘義務契約")),
    ("賃貸借契約", ("賃貸借",)),
    ("売買契約", ("売買", "売渡", "購入契約")),
    ("請負契約", ("請負", "工事契約")),
    ("業務委託契約", ("委託",)),
]


def classify_type(title: dict) -> dict:
    t = title.get("value") or ""
    if not t:
        return field(None, state=STATE_CHECK, note="表題が不明なため種類を判定できません")
    for typ, kws in TYPE_RULES:
        if any(k in t for k in kws):
            return field(typ, page=title.get("page"), evidence=title.get("evidence"),
                         source=title.get("source"), note="表題から判定")
    return field("その他", page=title.get("page"), evidence=title.get("evidence"),
                 source=title.get("source"), state=STATE_CHECK, note="表題から種類を判定できませんでした")


def suggest_category(doc: Doc, title: dict, categories: list[dict]) -> dict:
    """カテゴリー候補。キーワードの出現数で順位付けする（候補提示のみ）。"""
    t = title.get("value") or ""
    scores = []
    for c in categories:
        kws = [k.strip() for k in (c.get("keywords") or "").split(",") if k.strip()]
        s = 0
        for k in kws:
            s += 5 * t.count(k) + doc.full.count(k)
        if s > 0:
            scores.append((s, c["name"]))
    scores.sort(reverse=True)
    cands = [n for _, n in scores[:3]]
    if not cands:
        return field(None, state=STATE_CHECK, note="カテゴリー候補が見つかりません。選択してください", candidates=[])
    return field(cands[0], state=STATE_CHECK, note="キーワードによる候補です。確認して選択してください",
                 candidates=cands)


# ---------------------------------------------------------------- 当事者・署名者

_ROLE_PREFIX = re.compile(
    r"^(?:と|及び|および|並びに|ならびに|は|、|,|委託者|受託者|発注者|受注者|注文者|請負人|売主|買主|貸主|借主|"
    r"賃貸人|賃借人|開示者|受領者|排出事業者|収集運搬業者|処分業者)+[:：]?"
)
PARTY_RE = re.compile(
    r"(?P<name>[^\s、。,，（）()「」『』:：]{1,40}(?:[ 　][^\s、。,，（）()「」『』:：]{1,40}){0,2}?)\s*[（(]\s*以下\s*[、,]?\s*[「『\"]?\s*(?P<label>[甲乙丙丁戊己])\s*[」』\"]?\s*(?:という|と称する|といい|とする|と言う)"
)
ORG_HINT = re.compile(r"(株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|一般財団法人|公益社団法人|公益財団法人|"
                      r"社会福祉法人|医療法人|学校法人|特定非営利活動法人|NPO法人|協同組合|組合|協会|事務所|㈱|㈲|\(株\)|（株）|市|町|村|区|県|都|府)")
REP_RE = re.compile(
    r"(?P<title>代表取締役社長|代表取締役|代表執行役社長|代表執行役|代表理事|代表社員|取締役社長|代表者|理事長|"
    r"社長|所長|支店長|工場長|本部長|部長|市長|町長|村長|区長|知事|氏名|代表)\s*[:：]?\s*"
    r"(?P<name>[一-龥々〆ヶぁ-んァ-ヶーA-Za-zＡ-Ｚａ-ｚ]{1,10}(?:[ 　][一-龥々〆ヶぁ-んァ-ヶーA-Za-zＡ-Ｚａ-ｚ]{1,10})?)"
)
ADDRESS_RE = re.compile(r"(都|道|府|県).{0,8}(市|区|町|村|郡)|^(住所|所在地)")


def _clean_party(name: str) -> str:
    name = unicodedata.normalize("NFKC", name).strip()
    prev = None
    while prev != name:
        prev = name
        name = _ROLE_PREFIX.sub("", name).strip()
    return name


def match_company(name: str, company_names: list[str]) -> bool:
    n = norm(name)
    for c in company_names:
        c = norm(c)
        if c and (c in n or n in c):
            return True
    return False


def extract_parties(doc: Doc, company_names: list[str]) -> tuple[list[dict], dict]:
    """当事者一覧と、項目全体の状態を返す。"""
    parties: dict[str, dict] = {}
    for m in PARTY_RE.finditer(doc.full):
        label = m.group("label")
        name = _clean_party(m.group("name"))
        if len(name) < 2 or label in parties:
            continue
        pos = m.start("name")
        page = doc.page_at(pos)
        parties[label] = {"name": name, "label": label, "page": page,
                          "evidence": m.group(0), "source": _src(doc, page)}
    sig = extract_signature_block(doc)
    for label, blk in sig.items():
        if label not in parties and blk.get("org"):
            parties[label] = {"name": blk["org"], "label": label, "page": blk["page"],
                              "evidence": blk["evidence"], "source": _src(doc, blk["page"]),
                              "note": "署名欄から取得"}
    out = []
    for label in LABELS:
        if label in parties:
            p = parties[label]
            if company_names:
                p["role"] = "当社" if match_company(p["name"], company_names) else "相手方"
            else:
                p["role"] = "相手方"
            out.append(p)
    if not out:
        return [], missing(doc, "当事者の定義（以下「甲」という 等）が見つかりません") | {"state": STATE_CHECK}
    note = ""
    state = STATE_FOUND
    if not company_names:
        state, note = STATE_CHECK, "自社名が未設定のため、当社／相手方を判別できません（設定画面で自社名を登録してください）"
    elif not any(p["role"] == "当社" for p in out):
        state, note = STATE_CHECK, "当事者に自社名が見つかりません。当社／相手方を確認してください"
    return out, field(len(out), state=state, note=note)


def extract_signature_block(doc: Doc) -> dict[str, dict]:
    """末尾の署名欄から、当事者表示（甲・乙…）ごとの法人名・代表者を取り出す。"""
    result: dict[str, dict] = {}
    pages = [p for p in doc.pages if p.status in USABLE]
    if not pages:
        return result
    # 最終ページ（短い場合は直前ページも）を対象にする
    target = pages[-2:] if len(pages) >= 2 and len(pages[-1].text) < 400 else pages[-1:]
    current = None
    for p in target:
        lines = [l.strip() for l in p.text.split("\n")]
        for line in lines:
            if not line:
                continue
            m = re.match(r"^[（(【\[]?\s*([甲乙丙丁戊己])\s*[）)】\]]?\s*[:：]?\s*(.*)$", line)
            if m and (len(line) <= 2 or m.group(2) == "" or ADDRESS_RE.search(m.group(2)) or ORG_HINT.search(m.group(2))
                      or REP_RE.search(m.group(2))):
                current = m.group(1)
                result.setdefault(current, {"org": None, "reps": [], "page": p.page_no, "evidence": line})
                rest = m.group(2).strip()
                if not rest:
                    continue
                line = rest
            if current is None:
                continue
            blk = result[current]
            rep = REP_RE.search(line)
            if rep:
                name = unicodedata.normalize("NFKC", re.sub(r"[印㊞]$", "", rep.group("name"))).strip()
                before = line[: rep.start()].strip()
                if before and ORG_HINT.search(before) and not blk["org"] and not ADDRESS_RE.search(before):
                    blk["org"] = _clean_party(before)
                title = rep.group("title")
                blk["reps"].append({"name": name, "title": "" if title == "氏名" else title,
                                    "page": p.page_no, "evidence": line})
                continue
            if ADDRESS_RE.search(line) and not blk["org"]:
                continue
            if not blk["org"] and ORG_HINT.search(line) and len(line) <= 50 and not ADDRESS_RE.search(line):
                blk["org"] = _clean_party(re.sub(r"[印㊞]$", "", line))
    return result


def extract_signers(doc: Doc, parties: list[dict]) -> tuple[list[dict], dict]:
    sig = extract_signature_block(doc)
    out = []
    for label, blk in sig.items():
        party = next((p for p in parties if p["label"] == label), None)
        for r in blk["reps"]:
            out.append({"name": r["name"], "title": r["title"], "party_label": label,
                        "party_name": party["name"] if party else blk.get("org"),
                        "page": r["page"], "evidence": r["evidence"], "source": _src(doc, r["page"])})
    if not out:
        return [], missing(doc, "署名欄に代表者・署名者の記載が見つかりません")
    return out, field(len(out))


# ---------------------------------------------------------------- 日付・期間

SIGN_INTRO = re.compile(r"(記名押印|署名押印|記名捺印|署名捺印|署名又は記名押印|電子署名|各自署名|押印の上|証として)")


def _date_field(doc: Doc, pd: D.ParsedDate, pos: int, evidence: str, note: str = "") -> dict:
    page = doc.page_at(pos)
    if pd.status == "ok":
        return field(pd.iso, raw=pd.raw, page=page, evidence=evidence, source=_src(doc, page), note=note)
    state = STATE_CHECK
    msg = {"blank": "日付欄が空欄です", "incomplete": "年月日の一部が記載されていません（補完しません）",
           "invalid": "存在しない日付です", "era_out_of_range": "元号の期間外の日付です"}.get(pd.status, "")
    return field(pd.iso if pd.status == "era_out_of_range" else None, raw=pd.raw, page=page,
                 evidence=evidence, source=_src(doc, page), state=state, note=(msg + " " + note).strip())


def extract_contract_date(doc: Doc) -> dict:
    full = doc.full
    m = re.search(r"(契約締結日|締結日|契約日)\s*[:：]?\s*", full)
    if m:
        ds = D.find_dates(full[m.end(): m.end() + 30])
        if ds and ds[0].span[0] == 0:
            pd = ds[0]
            return _date_field(doc, pd, m.end(), full[m.start(): m.end() + pd.span[1]])
    # 署名欄直前の「…記名押印…」の後にある最初の日付
    intros = list(SIGN_INTRO.finditer(full))
    if intros:
        start = intros[-1].end()
        seg = full[start: start + 300]
        for pd in D.find_dates(seg):
            line_start = seg.rfind("\n", 0, pd.span[0]) + 1
            line_end = seg.find("\n", pd.span[1])
            line = seg[line_start: line_end if line_end >= 0 else None].strip()
            return _date_field(doc, pd, start + pd.span[0], line)
    # 最終ページで、日付だけの行
    for p in reversed(doc.pages):
        if p.status not in USABLE:
            continue
        for line in reversed(p.text.split("\n")):
            ln = line.strip()
            pd = D.parse_date(ln)
            if pd and len(norm(ln)) <= len(norm(pd.raw)) + 2:
                pos = doc.starts[doc.pages.index(p)]
                return _date_field(doc, pd, pos, ln, "署名欄付近の日付行から取得")
        break
    return missing(doc, "契約締結日の記載が見つかりません")


TERM_KEY = re.compile(r"(契約期間|有効期間|有効期限|本契約の期間|委託期間|賃貸借期間|貸借期間|本覚書の有効期間|期間は|存続期間|工期)")
COND_END = re.compile(r"(業務(?:の)?(?:完了|終了)(?:する)?(?:日|時)?まで|完了(?:する|した)?(?:日|時)?まで|"
                      r"終了(?:する|した)?(?:日|時)?まで|引渡し?(?:の日|完了)?まで|竣工(?:の日)?まで|履行が完了するまで)")
NO_TERM = re.compile(r"(期間の定め(?:は)?(?:のない|ない|なし|を設けない)|期間を定めない|期限の定めのない)")
FROM_RE = re.compile(r"(から|より)")


def extract_term(doc: Doc, contract_date: dict) -> dict:
    """契約期間の原文・開始日・満了日・期限区分をまとめて抽出する。"""
    res = {}
    cands = []
    for pos, s in doc.sentences():
        if TERM_KEY.search(s) and (FROM_RE.search(s) or "まで" in s or "間とする" in s or NO_TERM.search(s)):
            if re.search(r"(前まで|前に|予告)", s) and not D.find_dates(s):
                continue  # 通知条件の文は除外
            cands.append((pos, s))
    # 日付の直後に「から／より／まで」が続く文（期間を定める文の典型）を優先する
    def _rank(c):
        t = c[1]
        dated = any(re.match(r"\s*(から|より|まで|迄)", t[d.span[1]: d.span[1] + 3]) for d in D.find_dates(t))
        return (0 if dated else 1 if re.search(rf"{_N}\s*{_UNIT}\s*間", t) or NO_TERM.search(t) else 2, c[0])
    cands.sort(key=_rank)
    if cands and _rank(cands[0])[0] == 2:
        cands = [c for c in cands if _rank(c)[0] < 2] or cands
    if not cands:
        for pos, s in doc.sentences():
            if NO_TERM.search(s) or (COND_END.search(s) and re.search(r"(本契約|効力|有効)", s)):
                cands.append((pos, s))
                break
    if not cands:
        m = missing(doc, "契約期間の記載が見つかりません")
        res["term_raw"] = m
        res["start_date"] = missing(doc, "開始日の記載が見つかりません")
        res["expiry_date"] = missing(doc, "満了日の記載が見つかりません")
        res["expiry_type"] = field("記載なし" if m["state"] == STATE_NONE else "要確認",
                                   state=m["state"], note=m["note"])
        return res

    pos, s = cands[0]
    page = doc.page_at(pos)
    src = _src(doc, page)
    res["term_raw"] = field(s, raw=s, page=page, evidence=s, source=src)
    ds = D.find_dates(s)
    start = end = None
    m_from = FROM_RE.search(s)
    for pd in ds:
        if m_from and pd.span[1] <= m_from.end() + 1 and start is None:
            start = pd
        elif start is not None or (m_from and pd.span[0] > m_from.start()):
            end = pd if end is None else end
    if start is None and len(ds) >= 1 and "まで" not in s[ds[0].span[1]: ds[0].span[1] + 3]:
        start = ds[0] if m_from else None
    if end is None and len(ds) >= 2:
        end = ds[1]
    if end is None and start is None and len(ds) == 1 and "まで" in s[ds[0].span[1]: ds[0].span[1] + 4]:
        end = ds[0]  # 「有効期限は○年○月○日までとする」
    start_pos = pos
    if start is None:
        # 「本契約は○年○月○日から適用する／効力を生じる」など、別の文で開始日を定める場合
        for p2, s2 in doc.sentences():
            if re.search(r"(から|より)\s*(適用|効力を生じ|効力を有|発効|有効と)", s2) and re.search(r"(本契約|本覚書)", s2):
                d2 = [d for d in D.find_dates(s2)]
                if len(d2) == 1:
                    start, start_pos, start_sent = d2[0], p2, s2
                    break

    # 開始日
    if start is not None:
        res["start_date"] = _date_field(doc, start, start_pos + start.span[0], s if start_pos == pos else start_sent)
    elif re.search(r"(締結日|締結の日|本契約締結)(?:から|より)", s):
        cd = contract_date.get("value")
        res["start_date"] = field(cd, raw="締結日", page=page, evidence=s, source=src,
                                  state=STATE_FOUND if cd else STATE_CHECK,
                                  note="条文上、契約締結日を開始日としています")
    else:
        res["start_date"] = field(None, page=page, evidence=s, source=src, state=STATE_CHECK,
                                  note="契約期間の条文に開始日が明記されていません")

    # 満了日・期限区分
    period = None
    mp = re.search(rf"({_N})\s*({_UNIT})\s*間", s)
    if mp:
        period = D.parse_period(mp.group(0))
    if end is not None:
        res["expiry_date"] = _date_field(doc, end, pos + end.span[0], s)
        res["expiry_type"] = field("日付指定" if end.status == "ok" else "要確認", page=page, evidence=s,
                                   source=src, state=STATE_FOUND if end.status == "ok" else STATE_CHECK)
    elif NO_TERM.search(s):
        res["expiry_date"] = field(None, page=page, evidence=s, source=src, state=STATE_NONE,
                                   note="期間の定めなし")
        res["expiry_type"] = field("期間の定めなし", page=page, evidence=s, source=src)
    elif COND_END.search(s):
        res["expiry_date"] = field(None, page=page, evidence=s, source=src, state=STATE_NONE,
                                   note="条件により終了（日付は特定できません）")
        res["expiry_type"] = field("条件終了", page=page, evidence=s, source=src,
                                   note=COND_END.search(s).group(0))
    elif period and res["start_date"].get("value") and period[1] in ("年", "月"):
        st = D.parse_iso(res["start_date"]["value"])
        months = D.months_of(*period)
        exp = D.period_end(st, months)
        res["expiry_date"] = field(exp.isoformat(), raw=mp.group(0), page=page, evidence=s, source=src,
                                   state=STATE_CHECK,
                                   note=f"開始日 {D.fmt_jp(st)} から {mp.group(0)} として算出（初日を含み、応当日の前日。"
                                        f"応当日がない場合は月末）。条文を確認してください")
        res["expiry_type"] = field("日付指定", page=page, evidence=s, source=src, state=STATE_CHECK,
                                   note="算出値")
    else:
        res["expiry_date"] = field(None, page=page, evidence=s, source=src, state=STATE_CHECK,
                                   note="満了日を特定できません")
        res["expiry_type"] = field("要確認", page=page, evidence=s, source=src, state=STATE_CHECK)

    # 契約期間の記載が複数あり内容が異なる → 要確認
    others = []
    for p2, s2 in cands[1:]:
        if norm(s2) == norm(s):
            continue
        d2 = [d.iso for d in D.find_dates(s2) if d.iso]
        d1 = [d.iso for d in ds if d.iso]
        per2 = re.search(rf"({_N})\s*({_UNIT})\s*間", s2)
        if (d2 and set(d2) != set(d1)) or (per2 and mp and D.parse_period(per2.group(0)) != period):
            others.append({"page": doc.page_at(p2), "evidence": s2})
    if others:
        for k in ("term_raw", "expiry_date", "expiry_type"):
            res[k]["state"] = STATE_CHECK
            res[k]["note"] = (res[k].get("note", "") + " 契約期間の記載が複数あり内容が異なります（矛盾の可能性）").strip()
            res[k]["alternatives"] = others
    return res


# ---------------------------------------------------------------- 更新・通知

RENEW_YES = re.compile(r"(自動(?:的)?に(?:更新|延長)|自動更新(?:され|する|とする)|同一(?:の)?条件で(?:更新|延長|継続)|同条件で(?:更新|延長|継続)|"
                       r"(?:更に|さらに)\s*" + _N + r"\s*" + _UNIT + r"\s*間?(?:延長|更新|継続)|以後も?同様とする)")
RENEW_NO = re.compile(r"((?:更新|延長)(?:は)?(?:しない|行わない|されない|を行わない|せず)(?!旨|との|という)|"
                      r"自動更新(?:は)?(?:しない|ない|なし)(?!旨|との|という))")
NOTICE_NUM = re.compile(rf"(?P<base>期間満了日|期間満了|満了日|満了|期限)(?:日)?の?\s*(?P<n>{_N})\s*(?P<u>{_UNIT})\s*(?:前|以前)")
TERMINATE_KEY = re.compile(r"(中途解約|解約することができる|解約できる|解約を申し入れ|解約の申入れ|解約の申し入れ)")
TERMINATE_NUM = re.compile(rf"(?P<n>{_N})\s*(?P<u>{_UNIT})\s*(?:前|以前)(?:まで)?(?:に)?")


def _struct_notice(m) -> dict | None:
    pr = D.parse_period(m.group("n") + m.group("u"))
    if not pr:
        return None
    return {"value": pr[0], "unit": pr[1], "base": "満了日", "text": m.group(0)}


def extract_renewal(doc: Doc) -> dict:
    res = {}
    yes, no = [], []
    for pos, s in doc.sentences():
        if "更新" not in s and "延長" not in s and "同様" not in s:
            continue
        if RENEW_NO.search(s):
            no.append((pos, s))
        elif RENEW_YES.search(s):
            yes.append((pos, s))
    if yes and no:
        pos, s = yes[0]
        page = doc.page_at(pos)
        res["auto_renewal"] = field("不明", page=page, evidence=s, source=_src(doc, page), state=STATE_CHECK,
                                    note="自動更新に関する記載が矛盾しています",
                                    alternatives=[{"page": doc.page_at(p), "evidence": t} for p, t in no])
        res["renewal_terms"] = field(s, raw=s, page=page, evidence=s, source=_src(doc, page), state=STATE_CHECK)
    elif yes:
        pos, s = yes[0]
        page = doc.page_at(pos)
        res["auto_renewal"] = field("あり", page=page, evidence=s, source=_src(doc, page))
        months = None
        mp = re.search(rf"(?:更に|さらに)\s*({_N})\s*({_UNIT})", s) or             re.search(rf"({_N})\s*({_UNIT})\s*(?:間|ごとに|毎に|ごと)?\s*(?:自動的に)?(?:更新|延長|継続)", s)
        pr = D.parse_period(mp.group(0)) if mp else None
        if pr:
            months = D.months_of(*pr)
        res["renewal_terms"] = field(s, raw=s, page=page, evidence=s, source=_src(doc, page),
                                     renewal_period_months=months,
                                     note=f"更新期間: {months}か月" if months else "更新期間を特定できません")
    elif no:
        pos, s = no[0]
        page = doc.page_at(pos)
        res["auto_renewal"] = field("なし", page=page, evidence=s, source=_src(doc, page))
        res["renewal_terms"] = field(s, raw=s, page=page, evidence=s, source=_src(doc, page))
    else:
        m = missing(doc, "更新に関する記載が見つかりません（「自動更新なし」とは判断しません）")
        res["auto_renewal"] = field("不明", state=m["state"], note=m["note"])
        res["renewal_terms"] = missing(doc, "更新条件の記載が見つかりません")

    # 更新拒絶の通知条件
    notice = None
    for pos, s in doc.sentences():
        if ("更新" in s or "延長" in s or "継続" in s or "終了" in s) and re.search(r"(申し出|申出|通知|意思表示|申し入れ|申入れ)", s):
            m = NOTICE_NUM.search(s)
            if m:
                notice = (pos, s, m)
                break
    if notice:
        pos, s, m = notice
        page = doc.page_at(pos)
        res["renewal_notice"] = field(s, raw=s, page=page, evidence=s, source=_src(doc, page),
                                      structured=_struct_notice(m))
    elif res["auto_renewal"]["value"] == "あり":
        res["renewal_notice"] = field(None, state=STATE_CHECK, note="自動更新の記載はありますが、通知期限の条件が見つかりません")
    else:
        res["renewal_notice"] = missing(doc, "更新拒絶の通知条件の記載が見つかりません")

    # 中途解約の通知条件（更新拒絶とは別）
    term = None
    cands = [(pos, s) for pos, s in doc.sentences() if TERMINATE_KEY.search(s)]
    # 更新拒絶の条文中の「解約の申入れ」より、中途解約を定める条文を優先する
    cands.sort(key=lambda x: (("更新" in x[1]) or ("満了" in x[1]), 0 if re.search(r"(中途解約|解約することができる|解約できる)", x[1]) else 1))
    for pos, s in cands:
        m = TERMINATE_NUM.search(s)
        if term is None or (m and not term[2]):
            term = (pos, s, m)
        if m:
            break
    if term:
        pos, s, m = term
        page = doc.page_at(pos)
        st = None
        if m:
            pr = D.parse_period(m.group("n") + m.group("u"))
            if pr:
                st = {"value": pr[0], "unit": pr[1], "base": "解約日", "text": m.group(0)}
        res["termination_notice"] = field(s, raw=s, page=page, evidence=s, source=_src(doc, page), structured=st,
                                          state=STATE_FOUND if st else STATE_CHECK,
                                          note="" if st else "予告期間を特定できません")
    else:
        res["termination_notice"] = missing(doc, "中途解約の記載が見つかりません")
    return res


def extract_summary(doc: Doc) -> dict:
    """第1条（目的）等の原文を要旨の候補として引用する（原文にない内容は加えない）。"""
    m = re.search(r"第\s*[1１一]\s*条\s*(?:[（(][^）)\n]{0,20}[）)])?", doc.full)
    if m:
        body = doc.full[m.end(): m.end() + 400]
        body = re.split(r"第\s*[2２二]\s*条", body)[0]
        sents = re.findall(r"[^。]+。", body.replace("\n", ""))
        text = "".join(sents[:2]).strip() or body.strip()[:200]
        if text:
            page = doc.page_at(m.start())
            return field(text[:300], raw=text, page=page, evidence=text[:300], source=_src(doc, page), state=STATE_CHECK,
                         note="第1条の原文を引用した候補です。要旨として適切か確認してください")
    return missing(doc, "目的条項が見つかりません") | {"state": STATE_CHECK}


# ---------------------------------------------------------------- まとめ

def extract_all(doc: Doc, categories: list[dict], company_names: list[str]) -> dict:
    fields: dict = {}
    title = extract_title(doc)
    fields["title"] = title
    fields["doc_type"] = classify_type(title)
    fields["category"] = suggest_category(doc, title, categories)
    fields["summary"] = extract_summary(doc)
    cd = extract_contract_date(doc)
    fields["contract_date"] = cd
    fields.update(extract_term(doc, cd))
    fields.update(extract_renewal(doc))
    parties, pstate = extract_parties(doc, company_names)
    signers, sstate = extract_signers(doc, parties)
    # 契約日と開始日の取り違え防止：同じ根拠位置から両方を採っていないか
    if cd.get("value") and fields["start_date"].get("value") == cd.get("value") and cd.get("evidence") == fields["start_date"].get("evidence"):
        fields["start_date"]["state"] = STATE_CHECK
        fields["start_date"]["note"] += " 契約日と同じ記載から取得しています。確認してください"
    return {"fields": fields, "parties": parties, "parties_state": pstate,
            "signers": signers, "signers_state": sstate}
