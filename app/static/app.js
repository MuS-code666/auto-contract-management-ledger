/* ローカル契約書管理 画面スクリプト（外部ライブラリ不使用）
 * 契約書由来の文字列は必ず textContent で表示し、HTMLとして解釈しない。 */
"use strict";

// ------------------------------------------------------------------ 共通
const $app = document.getElementById("app");
let META = null;

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else if (k === "selected") el.selected = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** 子要素を置き換える（配列は展開し、null/undefined/false は無視） */
function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false)
    .map((c) => (c instanceof Node ? c : document.createTextNode(String(c)))));
  return el;
}

class ApiError extends Error {
  constructor(body, status) { super(body.error || "エラー"); this.body = body; this.status = status; }
}

async function api(method, url, body, isForm) {
  const opt = { method, headers: { "X-Ledger-Request": "1" } };
  if (body !== undefined) {
    if (isForm) opt.body = body;
    else { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  }
  let res;
  try { res = await fetch(url, opt); }
  catch (e) { throw new ApiError({ error: "アプリのサーバーに接続できません。", hint: "起動用ウィンドウが閉じられていないか確認し、start.bat で再起動してください。" }, 0); }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : null;
  if (!res.ok) throw new ApiError(data || { error: `エラー（HTTP ${res.status}）` }, res.status);
  return data;
}

function errBox(e) {
  const b = e && e.body ? e.body : { error: String(e && e.message || e) };
  return h("div", { class: "err" }, h("div", {}, b.error), b.hint ? h("div", { class: "hint" }, "対処: " + b.hint) : null);
}
function toast(msg, ms = 3500) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}
function showError(e, where) {
  const box = errBox(e);
  (where || document.getElementById("notice-area")).prepend(box);
  setTimeout(() => box.remove(), 15000);
  box.scrollIntoView({ block: "nearest" });
}
const fmtDate = (s) => s ? s.replaceAll("-", "/") : "";
const catName = (id) => (META.categories.find((c) => c.id === id) || {}).name || "";
function badge(text, cls) { return h("span", { class: "badge " + cls }, text); }
function stateBadge(state) {
  if (!state) return null;
  const cls = { "要確認": "b-check", "記載なし": "b-none", "読取不能": "b-check", "未確認": "b-unconf",
                "修正済み": "b-ok", "確認済み": "b-ok", "手入力": "b-ok" }[state] || "b-none";
  return badge(state, cls);
}
/** 値が空のとき、「記載なし」「要確認」と未入力を区別して表示 */
function cellValue(value, meta, display) {
  const st = meta && meta.state;
  if (value === null || value === undefined || value === "") {
    if (st === "記載なし" || st === "要確認" || st === "読取不能") return stateBadge(st);
    return h("span", { class: "empty-cell" }, "—");
  }
  const out = [display !== undefined ? display : String(value)];
  if (st === "要確認") out.push(" ", stateBadge("要確認"));
  return h("span", {}, out);
}

async function loadMeta() { META = await api("GET", "/api/meta"); }

function setNav(name) {
  document.querySelectorAll("header nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
}

// ------------------------------------------------------------------ ルーター
const routes = [
  [/^#\/list$/, viewList, "list"],
  [/^#\/deadlines$/, viewDeadlines, "deadlines"],
  [/^#\/import$/, viewImportStart, "import"],
  [/^#\/import\/([\w-]+)$/, viewImport, "import"],
  [/^#\/new$/, viewNewManual, "import"],
  [/^#\/contract\/([\w-]+)$/, viewContract, "list"],
  [/^#\/settings$/, viewSettings, "settings"],
];
let cleanup = null;
async function route() {
  if (cleanup) { try { cleanup(); } catch (_) {} cleanup = null; }
  const hash = location.hash || "#/list";
  for (const [re, fn, nav] of routes) {
    const m = hash.match(re);
    if (m) {
      setNav(nav);
      $app.className = "";
      fill($app, h("div", { class: "muted" }, "読み込み中…"));
      try { await loadMeta(); await fn(...m.slice(1)); }
      catch (e) { fill($app, errBox(e)); }
      return;
    }
  }
  location.hash = "#/list";
}
window.addEventListener("hashchange", route);
window.addEventListener("load", async () => {
  await route();
  if (META && META.recovery && META.recovery.length) {
    toast(`前回中断したファイル操作 ${META.recovery.length} 件を整合させました（設定 > 整合性チェックで確認できます）。`, 8000);
  }
});

// ------------------------------------------------------------------ 一覧
let listState = { q: "", sort: "mgmt_no", order: "desc", page: 1, per_page: 50 };

function optionList(values, selected, empty = "（すべて）") {
  return [h("option", { value: "" }, empty), ...values.map((v) => {
    const [val, label] = Array.isArray(v) ? v : [v, v];
    return h("option", { value: val, selected: String(selected ?? "") === String(val) }, label);
  })];
}

async function viewList() {
  const s = listState;
  const f = {};
  const inp = (name, attrs = {}) => (f[name] = h("input", { value: s[name] || "", ...attrs }));
  const sel = (name, values) => (f[name] = h("select", {}, optionList(values, s[name])));
  const filters = h("form", { class: "panel filters", onsubmit: (e) => { e.preventDefault(); apply(); } },
    h("div", { class: "wide" }, h("label", {}, "キーワード（契約名・当事者・要旨・管理番号）"), inp("q", { placeholder: "例: 東西環境 委託" })),
    h("div", {}, h("label", {}, "カテゴリー"), sel("category_id", META.categories.map((c) => [c.id, c.name + (c.active ? "" : "（無効）")]))),
    h("div", {}, h("label", {}, "種類"), sel("doc_type", META.doc_types)),
    h("div", {}, h("label", {}, "担当部門・担当者"), inp("owner")),
    h("div", {}, h("label", {}, "確認状態"), sel("review_status", META.review_states)),
    h("div", {}, h("label", {}, "自動更新"), sel("auto_renewal", META.renewal)),
    h("div", {}, h("label", {}, "期限の区分"), sel("expiry_type", META.expiry_types)),
    h("div", {}, h("label", {}, "期間指定の対象"), sel("date_field", [["contract_date", "契約日"], ["start_date", "開始日"], ["expiry_date", "有効期限"], ["notice", "通知期限"], ["next_review_date", "次回確認予定日"], ["created_at", "登録日"]])),
    h("div", {}, h("label", {}, "から"), inp("date_from", { type: "date" })),
    h("div", {}, h("label", {}, "まで"), inp("date_to", { type: "date" })),
    h("div", {}, h("label", {}, "削除済み"), (f.deleted = h("select", {}, h("option", { value: "exclude", selected: s.deleted !== "only" && s.deleted !== "all" }, "含めない"), h("option", { value: "all", selected: s.deleted === "all" }, "含める"), h("option", { value: "only", selected: s.deleted === "only" }, "削除済みのみ")))),
    h("div", { class: "row" }, h("button", { class: "primary", type: "submit" }, "検索"),
      h("button", { type: "button", onclick: () => { listState = { sort: "mgmt_no", order: "desc", page: 1, per_page: 50 }; viewList(); } }, "条件クリア")),
  );
  function apply() {
    for (const k of Object.keys(f)) s[k] = f[k].value;
    s.page = 1; load();
  }
  const tableWrap = h("div", {});
  const exportBar = h("div", { class: "row" },
    h("button", { onclick: () => exportXlsx("filtered") }, "検索結果をExcel出力"),
    h("button", { onclick: () => exportXlsx("all") }, "全件をExcel出力"),
    h("span", { class: "muted small" }, "PDFリンクはこのPC上の保存場所を指します。別のPCでは開けない場合があります。"),
    h("a", { class: "btn", href: "#/new", style: "margin-left:auto" }, "PDFなしで手動登録"),
    h("a", { class: "btn", href: "#/import" }, "PDFを取り込む"));
  fill($app, h("h1", {}, "契約一覧"), filters, exportBar, tableWrap);

  function queryString(extra = {}) {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries({ ...s, ...extra })) if (v !== undefined && v !== null && v !== "") p.set(k, v);
    return p.toString();
  }
  function exportXlsx(scope) { location.href = "/api/export.xlsx?" + queryString({ scope, page: "", per_page: "" }); }

  async function load() {
    fill(tableWrap, h("div", { class: "muted" }, "検索中…"));
    let res;
    try { res = await api("GET", "/api/contracts?" + queryString()); }
    catch (e) { fill(tableWrap, errBox(e)); return; }
    const th = (label, key) => h("th", { class: key ? "sortable" : "", onclick: key ? () => { s.order = s.sort === key && s.order === "asc" ? "desc" : "asc"; s.sort = key; load(); } : null },
      label, key && s.sort === key ? (s.order === "asc" ? " ▲" : " ▼") : "");
    const rows = res.items.map((c) => {
      const m = c.field_meta || {};
      const others = c.parties.filter((p) => p.role !== "当社").map((p) => p.name).join("、");
      const exp = c.managed_expiry_date || c.expiry_date;
      const notice = c.notice_deadline_confirmed
        ? h("span", {}, fmtDate(c.notice_deadline_confirmed))
        : c.notice_deadline_candidate ? h("span", {}, fmtDate(c.notice_deadline_candidate), " ", badge("候補", "b-cand")) : h("span", { class: "empty-cell" }, "—");
      let expCell;
      if (exp) expCell = h("span", {}, fmtDate(exp), c.managed_expiry_date ? " (管理)" : "", m.expiry_date && m.expiry_date.state === "要確認" ? [" ", stateBadge("要確認")] : null);
      else if (c.expiry_type && c.expiry_type !== "日付指定") expCell = badge(c.expiry_type, c.expiry_type === "要確認" ? "b-check" : "b-none");
      else expCell = cellValue(null, m.expiry_date);
      return h("tr", { class: c.deleted_at ? "deleted" : "" },
        h("td", {}, h("a", { href: "#/contract/" + c.id }, c.mgmt_no)),
        h("td", {}, cellValue(c.title, m.title)),
        h("td", {}, cellValue(c.doc_type, m.doc_type)),
        h("td", {}, c.category_name || ""),
        h("td", {}, others ? others : cellValue(null, m.parties)),
        h("td", {}, cellValue(c.contract_date, m.contract_date, fmtDate(c.contract_date))),
        h("td", {}, expCell),
        h("td", {}, notice),
        h("td", {}, c.auto_renewal === "不明" ? badge("不明", "b-unconf") : (c.auto_renewal || "")),
        h("td", {}, stateBadge(c.review_status)),
        h("td", {}, c.pdf_relpath ? h("a", { href: `/api/contracts/${c.id}/pdf`, target: "_blank" }, "PDF") : h("span", { class: "muted small" }, "なし")),
      );
    });
    const pages = Math.max(1, Math.ceil(res.total / res.per_page));
    const pager = h("div", { class: "pager" },
      h("button", { disabled: s.page <= 1, onclick: () => { s.page--; load(); } }, "前へ"),
      h("span", {}, `${res.total} 件中 ${res.total ? (res.page - 1) * res.per_page + 1 : 0}–${Math.min(res.total, res.page * res.per_page)} 件（${res.page}/${pages} ページ）`),
      h("button", { disabled: s.page >= pages, onclick: () => { s.page++; load(); } }, "次へ"),
      h("select", { onchange: (e) => { s.per_page = e.target.value; s.page = 1; load(); } },
        [20, 50, 100, 200].map((n) => h("option", { value: n, selected: Number(s.per_page) === n }, `${n}件表示`))));
    fill(tableWrap, pager,
      h("table", { class: "list" },
        h("thead", {}, h("tr", {}, th("管理番号", "mgmt_no"), th("契約書名", "title"), th("種類"), th("カテゴリー", "category"), th("相手方"),
          th("契約日", "contract_date"), th("有効期限", "expiry_date"), th("通知期限", "notice"), th("自動更新"), th("確認状態"), th("PDF"))),
        h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { colspan: 11, class: "muted" }, "該当する契約はありません。")))),
      h("div", { class: "small muted", style: "margin-top:6px" }, "表示の意味：「—」は未入力、「記載なし」は契約書に記載がない、「要確認」は確認が必要、「候補」はアプリの計算値で未確定。"));
  }
  load();
}

// ------------------------------------------------------------------ 期限
async function viewDeadlines() {
  const d = await api("GET", "/api/deadlines");
  const desc = {
    "期限経過": "有効期限（管理上の期限があればそれ）を過ぎた契約。自動更新の契約は更新状況を確認してください。",
    "期限が近い": `今日から ${d.alert_days_expiry} 日以内に有効期限を迎える契約。`,
    "通知期限経過": "通知期限を過ぎたが有効期限前の契約。",
    "通知期限が近い": `今日から ${d.alert_days_notice} 日以内に通知期限（確定または候補）を迎える契約。`,
    "日付未確定": "期限の区分が記載なし・要確認、自動更新が不明、または自動更新ありで通知期限が未確定の契約。",
    "要確認": "確認状態が「確認済み」でない、または要確認の項目がある契約。",
    "確認予定日": "次回確認予定日が7日以内または過ぎている契約。",
  };
  const panels = Object.entries(d.buckets).map(([k, items]) => h("div", { class: "panel" },
    h("h3", {}, `${k}（${items.length}件）`), h("div", { class: "small muted" }, desc[k] || ""),
    items.length ? h("ul", {}, items.slice(0, 200).map((it) => h("li", {},
      h("a", { href: "#/contract/" + it.id }, it.mgmt_no), " ", it.title || "（契約書名なし）", " ",
      h("span", { class: "small muted" },
        it.expiry ? `期限 ${fmtDate(it.expiry)}` : `期限 ${it.expiry_type || "未入力"}`,
        it.notice ? ` / 通知 ${fmtDate(it.notice)}${it.notice_confirmed ? "" : "（候補）"}` : "",
        it.next_review_date ? ` / 確認予定 ${fmtDate(it.next_review_date)}` : ""),
      " ", stateBadge(it.review_status)))) : h("div", { class: "muted small" }, "該当なし")));
  fill($app, h("h1", {}, `期限・要確認（基準日 ${fmtDate(d.today)}）`),
    h("div", { class: "info small" }, "通知期限の「候補」はアプリの計算値です。確定するには契約詳細で「通知期限（確定）」を入力してください。時刻の経過だけで更新成立や契約継続を確定しません。"),
    h("div", { class: "board" }, panels));
}

// ------------------------------------------------------------------ 取込み開始
async function viewImportStart() {
  const fileInput = h("input", { type: "file", accept: "application/pdf,.pdf", class: "hidden", onchange: (e) => e.target.files[0] && upload(e.target.files[0]) });
  const msg = h("div", {});
  const drop = h("div", { class: "drop" },
    h("div", { style: "font-size:16px;margin-bottom:8px" }, "契約書PDFをここにドラッグ＆ドロップ"),
    h("div", {}, "または ", h("button", { class: "primary", onclick: () => fileInput.click() }, "ファイルを選択")),
    h("div", { class: "small", style: "margin-top:10px" }, "1件ずつ取り込みます。元のファイルは変更・削除しません（アプリの一時領域へコピーして処理します）。"), fileInput);
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault(); drop.classList.remove("over");
    const files = [...e.dataTransfer.files];
    if (files.length > 1) { fill(msg, errBox({ body: { error: "初期版はPDF1件ずつの取込みです。", hint: "1ファイルだけをドロップしてください。" } })); return; }
    if (files[0]) upload(files[0]);
  });
  async function upload(file) {
    fill(msg, h("div", { class: "info" }, h("span", { class: "spinner" }), " アップロード・検査中: ", file.name));
    const fd = new FormData(); fd.append("file", file, file.name);
    try {
      const r = await api("POST", "/api/imports", fd, true);
      if (r.resumed) toast("同じPDFの取込み途中のデータがあるため、そちらを開きます。");
      location.hash = "#/import/" + r.id;
    } catch (e) {
      const box = errBox(e);
      if (e.body && e.body.contract_id) box.append(h("div", {}, h("a", { href: "#/contract/" + e.body.contract_id }, `管理番号 ${e.body.mgmt_no} を開く`)));
      fill(msg, box);
    }
  }
  const drafts = await api("GET", "/api/imports");
  const st = { queued: "待機中", processing: "解析中", extracted: "解析完了", partial: "一部未完了", failed: "失敗", cancelled: "中止", interrupted: "中断" };
  fill($app, h("h1", {}, "契約書の取込み"), drop, msg,
    h("h2", {}, "下書き・取込み途中（再開できます）"),
    drafts.length ? h("table", { class: "list" }, h("thead", {}, h("tr", {}, h("th", {}, "ファイル名"), h("th", {}, "状態"), h("th", {}, "ページ"), h("th", {}, "更新日時"))),
      h("tbody", {}, drafts.map((d) => h("tr", {}, h("td", {}, h("a", { href: "#/import/" + d.id }, d.original_filename)),
        h("td", {}, st[d.status] || d.status), h("td", {}, d.page_count), h("td", {}, (d.updated_at || "").replace("T", " "))))))
      : h("div", { class: "muted" }, "下書きはありません。"),
    h("h2", {}, "抽出機能の状態"), engineStatus());
}

function engineStatus() {
  const box = h("div", { class: "panel small" },
    h("div", {}, "OCR: ", { none: "無効", rapidocr: "RapidOCR", tesseract: "Tesseract" }[META.ocr_engine] || META.ocr_engine, "　／　LLM: ", META.llm_engine === "none" ? "無効（ルール抽出のみ）" : "ローカルLLM（OpenAI互換サーバー）"),
    h("div", { class: "muted" }, "OCRやLLMが未導入でも、PDFを取り込んで手動入力・登録できます。導入方法は README と設定画面を参照してください。"));
  return box;
}

// ------------------------------------------------------------------ フォーム
const FORM_KEYMAP = { category_id: "category", renewal_notice_raw: "renewal_notice", termination_notice_raw: "termination_notice" };

/**
 * 契約入力フォーム。
 * opts.evidence(key) → {state,page,evidence,source,method,note,alternatives,extracted}
 * opts.onPage(page)  → 根拠ページへ移動
 * opts.onChange(key)
 */
function contractForm(form, opts) {
  form = JSON.parse(JSON.stringify(form || {}));
  form.parties = form.parties || []; form.signers = form.signers || []; form.field_states = form.field_states || {};
  const root = h("div", { class: "form" });
  const edited = new Set(opts.edited || []);
  const changed = (key) => { edited.add(key); if (opts.onChange) opts.onChange(key); scheduleCalc(); };

  function evidenceLine(key) {
    const ev = opts.evidence ? opts.evidence(key) : null;
    if (!ev) return null;
    const parts = [];
    if (ev.page || ev.evidence) {
      parts.push(h("div", { class: "ev" },
        ev.page ? h("button", { class: "small", type: "button", onclick: () => opts.onPage && opts.onPage(ev.page) }, `p.${ev.page}へ`) : null, " ",
        ev.source ? badge(ev.source === "ocr" ? "OCR由来" : "既存テキスト由来", "b-none") : null, " ",
        ev.method ? h("span", {}, { rule: "ルール抽出", llm: "LLM抽出", "rule+llm": "ルール＋LLM一致" }[ev.method] || ev.method) : null, " ",
        ev.evidence ? h("q", {}, String(ev.evidence).slice(0, 300)) : null,
        ev.extracted !== undefined && ev.extracted !== null && ev.state === "修正済み" ? h("span", {}, "　抽出値: ", String(ev.extracted).slice(0, 120)) : null));
    }
    if (ev.note) parts.push(h("div", { class: "note" }, ev.note));
    for (const a of ev.alternatives || []) {
      parts.push(h("div", { class: "alt" }, "別の候補", a.label ? `（${a.label}）` : "", ": ", a.value ? String(a.value).slice(0, 200) : "",
        a.page ? [" ", h("button", { class: "small", type: "button", onclick: () => opts.onPage && opts.onPage(a.page) }, `p.${a.page}`)] : null,
        a.evidence && a.evidence !== a.value ? [" ", h("q", {}, String(a.evidence).slice(0, 200))] : null));
    }
    return parts;
  }

  function stateSelect(key, box) {
    const cur = form.field_states[key] || "";
    const sel = h("select", { class: "small", title: "値が空のときの区別（記載なし／要確認／読取不能）", onchange: () => {
      if (sel.value) form.field_states[key] = sel.value; else delete form.field_states[key];
      edited.add("field_states"); changed("field_states"); paint(box, key);
    } }, h("option", { value: "" }, "状態: 通常"), ["記載なし", "要確認", "読取不能"].map((s) => h("option", { value: s, selected: cur === s }, s)));
    return sel;
  }
  function paint(box, key) {
    const ev = opts.evidence ? opts.evidence(key) : null;
    const st = form.field_states[key] || (edited.has(key) ? "修正済み" : ev && ev.state);
    box.classList.toggle("warn", st === "要確認" || st === "読取不能");
    box.classList.toggle("unconf", st === "未確認" || st === "抽出");
    box.classList.toggle("edited", edited.has(key) && !(st === "要確認"));
  }

  function fieldBox(key, label, input, extra) {
    const box = h("div", { class: "f" });
    box.append(...[h("div", { class: "head" }, h("span", { class: "lbl" }, label), stateBadge(stateLabel(key)), h("span", { class: "state" }, stateSelect(key, box))),
      input, extra, evidenceLine(key)].flat(Infinity).filter(Boolean));
    paint(box, key);
    return box;
  }
  function stateLabel(key) {
    const ev = opts.evidence ? opts.evidence(key) : null;
    if (!ev || !ev.state) return null;
    return ev.state === "抽出" ? "未確認" : ev.state;
  }
  function text(key, attrs = {}) {
    return h("input", { type: "text", value: form[key] ?? "", ...attrs, oninput: (e) => { form[key] = e.target.value; clearAutoState(key); changed(key); } });
  }
  function area(key, rows = 3) {
    return h("textarea", { rows, oninput: (e) => { form[key] = e.target.value; clearAutoState(key); changed(key); } }, form[key] ?? "");
  }
  function dateIn(key) {
    return h("input", { type: "date", value: form[key] ?? "", onchange: (e) => { form[key] = e.target.value || null; clearAutoState(key); changed(key); } });
  }
  function select(key, values, empty) {
    return h("select", { onchange: (e) => { const v = e.target.value; form[key] = v === "" ? null : (key === "category_id" ? Number(v) : v); clearAutoState(key); changed(key); } },
      empty !== undefined ? h("option", { value: "" }, empty) : null,
      values.map((v) => { const [val, lab] = Array.isArray(v) ? v : [v, v]; return h("option", { value: val, selected: String(form[key] ?? "") === String(val) }, lab); }));
  }
  // 利用者が値を編集したら、抽出時に自動設定された「要確認」等は外す（利用者が選んだものは残す）
  const autoStates = new Set(Object.keys(form.field_states).filter((k) => !edited.has(k)));
  function clearAutoState(key) {
    if (autoStates.has(key)) { delete form.field_states[key]; autoStates.delete(key); }
  }
  function noticeStruct(key) {
    const st = form[key] || {};
    const v = h("input", { type: "number", min: 0, style: "width:80px", value: st.value ?? "", oninput: upd });
    const u = h("select", { onchange: upd }, h("option", { value: "" }, "単位"), ["日", "月", "年"].map((x) => h("option", { value: x, selected: st.unit === x }, x === "月" ? "か月" : x)));
    function upd() {
      form[key] = v.value === "" || !u.value ? null : { value: Number(v.value), unit: u.value, base: (st.base || (key === "renewal_notice_json" ? "満了日" : "解約日")), text: st.text || null };
      changed(key);
    }
    return h("div", { class: "row small" }, key === "renewal_notice_json" ? "構造化：満了日の" : "構造化：解約日の", v, u, "前まで");
  }

  // 当事者
  function partiesEditor() {
    const wrap = h("div", {});
    function render() {
      fill(wrap, h("table", { class: "sub" }, h("tbody", {}, form.parties.map((p, i) => h("tr", {},
        h("td", { style: "width:46px" }, h("input", { type: "text", value: p.label || "", placeholder: "甲", oninput: (e) => { p.label = e.target.value || null; changed("parties"); } })),
        h("td", {}, h("input", { type: "text", value: p.name, placeholder: "法人名・氏名", oninput: (e) => { p.name = e.target.value; clearAutoState("parties"); changed("parties"); } })),
        h("td", { style: "width:100px" }, h("select", { onchange: (e) => { p.role = e.target.value; changed("parties"); } }, ["当社", "相手方", "その他"].map((r) => h("option", { selected: p.role === r }, r)))),
        h("td", { style: "width:40px" }, h("button", { class: "small danger", type: "button", onclick: () => { form.parties.splice(i, 1); form.signers.forEach((s) => { if (s.party_index === i) s.party_index = null; else if (s.party_index > i) s.party_index--; }); changed("parties"); render(); renderSigners(); } }, "削除")))))),
        h("button", { class: "small", type: "button", onclick: () => { form.parties.push({ name: "", role: "相手方", label: null }); render(); renderSigners(); } }, "＋ 当事者を追加"));
    }
    render();
    return wrap;
  }
  const signersWrap = h("div", {});
  function renderSigners() {
    fill(signersWrap, h("table", { class: "sub" }, h("tbody", {}, form.signers.map((s, i) => h("tr", {},
      h("td", {}, h("input", { type: "text", value: s.name, placeholder: "氏名", oninput: (e) => { s.name = e.target.value; clearAutoState("signers"); changed("signers"); } })),
      h("td", { style: "width:140px" }, h("input", { type: "text", value: s.title || "", placeholder: "役職", oninput: (e) => { s.title = e.target.value; changed("signers"); } })),
      h("td", { style: "width:180px" }, h("select", { onchange: (e) => { s.party_index = e.target.value === "" ? null : Number(e.target.value); changed("signers"); } },
        h("option", { value: "" }, "（所属当事者）"), form.parties.map((p, pi) => h("option", { value: pi, selected: s.party_index === pi }, `${p.label ? p.label + " " : ""}${p.name}`)))),
      h("td", { style: "width:40px" }, h("button", { class: "small danger", type: "button", onclick: () => { form.signers.splice(i, 1); changed("signers"); renderSigners(); } }, "削除")))))),
      h("button", { class: "small", type: "button", onclick: () => { form.signers.push({ name: "", title: "", party_index: null }); renderSigners(); } }, "＋ 署名者を追加"));
  }
  renderSigners();

  // 通知期限の候補（サーバーで計算）
  const calcBox = h("div", { class: "calc" }, "通知期限の候補: 計算中…");
  const nextBox = h("div", { class: "calc hidden" });
  let calcTimer = null;
  function scheduleCalc() { clearTimeout(calcTimer); calcTimer = setTimeout(runCalc, 500); }
  let lastCalc = null;
  async function runCalc() {
    try {
      const r = await api("POST", "/api/calc", { form: payload() });
      lastCalc = r;
      fill(calcBox, h("b", {}, "通知期限の候補（未確定）: "), r.notice.date ? fmtDate(r.notice.date) : "なし", h("div", {}, "計算方法: ", r.notice.calc),
        r.notice.date ? h("button", { class: "small", type: "button", onclick: () => { form.notice_deadline_confirmed = r.notice.date; confirmedIn.value = r.notice.date; changed("notice_deadline_confirmed"); } }, "この候補を確定値に入れる（条文を確認のうえ）") : null);
      if (r.next_term) { nextBox.classList.remove("hidden"); fill(nextBox, h("b", {}, "自動更新された場合の次回満了日（候補）: "), fmtDate(r.next_term.date), h("div", {}, r.next_term.calc)); }
      else nextBox.classList.add("hidden");
    } catch (e) { fill(calcBox, "通知期限の候補: ", e.body ? e.body.error + " " + (e.body.hint || "") : "計算できません"); }
  }
  const confirmedIn = dateIn("notice_deadline_confirmed");

  const cats = META.categories.filter((c) => c.active || c.id === form.category_id).map((c) => [c.id, c.name]);
  const catEv = opts.evidence ? opts.evidence("category_id") : null;
  const catCands = catEv && catEv.candidates && catEv.candidates.length ? h("div", { class: "ev" }, "候補: ", catEv.candidates.join("、")) : null;

  root.append(
    h("fieldset", {}, h("legend", {}, "基本情報"),
      fieldBox("title", "契約書名", text("title")),
      h("div", { class: "two" }, fieldBox("doc_type", "契約書の種類", select("doc_type", META.doc_types, "（選択）")),
        fieldBox("category_id", "カテゴリー（保存フォルダ）", select("category_id", cats, "（選択してください）"), catCands)),
      fieldBox("summary", "要旨（目的・対象業務・主要な権利義務）", area("summary", 4))),
    h("fieldset", {}, h("legend", {}, "当事者・署名者"),
      fieldBox("parties", "契約当事者（当社と相手方を区別）", partiesEditor()),
      fieldBox("signers", "署名者・代表者（当事者とは別）", signersWrap)),
    h("fieldset", {}, h("legend", {}, "日付・期間"),
      h("div", { class: "two" },
        fieldBox("contract_date", "契約日（締結日）", h("div", {}, dateIn("contract_date"), h("div", { class: "small" }, "原文: ", text("contract_date_raw", { placeholder: "例: 令和6年3月15日" })))),
        fieldBox("start_date", "契約開始日", h("div", {}, dateIn("start_date"), h("div", { class: "small" }, "原文: ", text("start_date_raw"))))),
      fieldBox("term_raw", "契約期間の原文", area("term_raw", 2)),
      h("div", { class: "two" },
        fieldBox("expiry_type", "期限の区分", select("expiry_type", META.expiry_types, "（選択）")),
        fieldBox("expiry_date", "有効期限（満了日）", h("div", {}, dateIn("expiry_date"), h("div", { class: "small" }, "原文: ", text("expiry_raw"))))),
      h("div", { class: "f" }, h("div", { class: "head" }, h("span", { class: "lbl" }, "管理上の期限（変更覚書など関連書類を確認して確定した期限）")),
        h("div", { class: "two" }, dateIn("managed_expiry_date"), text("managed_expiry_note", { placeholder: "根拠（例: C000005 覚書で延長）" })),
        h("div", { class: "small muted" }, "元の契約の有効期限は上書きされず、別に保持されます。"))),
    h("fieldset", {}, h("legend", {}, "更新・通知"),
      h("div", { class: "two" },
        fieldBox("auto_renewal", "自動更新", select("auto_renewal", META.renewal)),
        h("div", { class: "f" }, h("div", { class: "head" }, h("span", { class: "lbl" }, "更新期間（か月）")),
          h("input", { type: "number", min: 1, value: form.renewal_period_months ?? "", oninput: (e) => { form.renewal_period_months = e.target.value ? Number(e.target.value) : null; changed("renewal_period_months"); } }))),
      fieldBox("renewal_terms", "更新条件", area("renewal_terms", 2)),
      fieldBox("renewal_notice_raw", "更新拒絶の通知条件（原文）", area("renewal_notice_raw", 2), noticeStruct("renewal_notice_json")),
      h("div", { class: "f" }, h("div", { class: "head" }, h("span", { class: "lbl" }, "通知期限（利用者が確定した日）")), confirmedIn, calcBox, nextBox),
      fieldBox("termination_notice_raw", "中途解約の通知条件（原文）", area("termination_notice_raw", 2), noticeStruct("termination_notice_json"))),
    h("fieldset", {}, h("legend", {}, "社内管理（手動登録）"),
      h("div", { class: "two" },
        h("div", {}, h("label", {}, "社内担当部門"), text("dept")), h("div", {}, h("label", {}, "担当者"), text("owner")),
        h("div", {}, h("label", {}, "紙原本の保管場所"), text("paper_location")), h("div", {}, h("label", {}, "次回確認予定日"), dateIn("next_review_date"))),
      h("div", {}, h("label", {}, "備考"), area("notes", 3)),
      h("div", { class: "row", style: "margin-top:6px" }, h("label", {}, "確認状態"), select("review_status", META.review_states))),
  );
  runCalc();

  function payload() {
    const f = JSON.parse(JSON.stringify(form));
    f.parties = f.parties.filter((p) => p.name && p.name.trim());
    const keep = form.parties.map((p, i) => (p.name && p.name.trim() ? i : null)).filter((x) => x !== null);
    f.signers = f.signers.filter((s) => s.name && s.name.trim()).map((s) => ({ ...s, party_index: s.party_index === null || s.party_index === undefined ? null : (keep.indexOf(s.party_index) >= 0 ? keep.indexOf(s.party_index) : null) }));
    for (const k of ["renewal_period_months"]) if (f[k] === "") f[k] = null;
    return f;
  }
  return { el: root, payload, edited: () => [...edited] };
}

// ------------------------------------------------------------------ 取込み・確認
async function viewImport(iid) {
  $app.className = "full";
  let data = await api("GET", `/api/imports/${iid}`);
  let pdfPage = 1, pdfVer = 0;
  const iframe = h("iframe", { title: "PDF" });
  const pagebar = h("div", { class: "pagebar" });
  const textView = h("div", { class: "textview hidden" });
  const left = h("div", { class: "left" }, pagebar, iframe, textView);
  const right = h("div", { class: "right" });
  fill($app, h("div", { class: "split" }, left, right));
  function gotoPage(p) { pdfPage = p; pdfVer++; iframe.src = `/api/imports/${iid}/pdf?v=${pdfVer}#page=${p}`; }
  gotoPage(1);

  async function showText(p) {
    try {
      const t = await api("GET", `/api/imports/${iid}/pages/${p}`);
      textView.classList.remove("hidden");
      fill(textView, h("div", { class: "row small" }, h("b", {}, `p.${p} の取得文字（${t.text_source === "ocr" ? "OCR" : t.text_source === "text" ? "既存テキスト" : "なし"}）`), t.note || "",
        h("button", { class: "small", style: "margin-left:auto", onclick: () => textView.classList.add("hidden") }, "閉じる")), t.text || "（文字なし）");
    } catch (e) { toast(e.message); }
  }
  function renderPagebar() {
    const pages = data.pages || [];
    fill(pagebar, h("span", {}, "ページ: "), ...pages.map((p) => h("button", {
      class: { ok: p.text_source === "ocr" ? "pg-ocr" : "pg-ok", empty: "pg-empty" }[p.status] || "pg-bad",
      title: `${p.status_ja}${p.note ? "：" + p.note : ""}（${p.chars}文字）`, onclick: () => { gotoPage(p.page_no); showText(p.page_no); } }, String(p.page_no))),
      pages.length ? h("span", { class: "small", style: "margin-left:8px" }, "緑=既存テキスト 青=OCR 赤=未処理・低品質 灰=空ページ") : null);
  }

  let fc = null, dirty = false, saveTimer = null, pollTimer = null, startedPoll = Date.now();
  cleanup = () => { clearTimeout(pollTimer); clearTimeout(saveTimer); if (dirty && fc) saveDraft(true); };

  async function saveDraft(silent) {
    if (!fc) return;
    try { await api("PUT", `/api/imports/${iid}/draft`, { form: fc.payload(), user_edited: fc.edited() }); dirty = false; if (!silent) toast("下書きを保存しました。"); }
    catch (e) { if (!silent) showError(e, right); }
  }

  function evidenceFor(key) {
    const ex = data.result && data.result.extraction;
    if (!ex) return null;
    if (key === "parties" || key === "signers") {
      const st = ex[key + "_state"] || {};
      const first = (ex[key] || [])[0];
      return { state: st.state, note: st.note, method: st.method, page: first && first.page, source: first && first.source,
               evidence: (ex[key] || []).map((p) => p.evidence).filter(Boolean).slice(0, 3).join(" / ") };
    }
    return ex.fields[FORM_KEYMAP[key] || key] || null;
  }

  function render() {
    renderPagebar();
    const active = data.status === "queued" || data.status === "processing";
    const st = { queued: "待機中", processing: "解析中", extracted: "解析完了", partial: "解析完了（一部未完了）", failed: "解析失敗", cancelled: "中止", interrupted: "中断", registered: "登録済み", discarded: "破棄済み" }[data.status] || data.status;
    const statusBar = h("div", { class: "panel" },
      h("div", { class: "status" }, active ? h("span", { class: "spinner" }) : null, h("b", {}, st), data.stage ? h("span", {}, "段階: ", data.stage) : null,
        data.progress ? h("span", {}, data.progress) : null, data.elapsed_sec !== undefined ? h("span", { class: "muted" }, `経過 ${data.elapsed_sec} 秒`) : null,
        h("span", { class: "muted small" }, data.original_filename, ` / ${data.page_count}ページ`),
        h("span", { style: "margin-left:auto" },
          active ? h("button", { class: "danger", onclick: async () => { await api("POST", `/api/imports/${iid}/cancel`); toast("中止を要求しました。"); } }, "中止")
            : data.status !== "registered" && data.status !== "discarded" ? h("button", { onclick: async () => { try { if (fc) await saveDraft(true); await api("POST", `/api/imports/${iid}/analyze`); poll(); } catch (e) { showError(e, right); } } }, "再解析") : null)),
      data.error ? h("div", { class: "err" }, data.error, data.error_hint ? h("div", { class: "hint" }, "対処: " + data.error_hint) : null) : null,
      ...(data.result ? data.result.warnings.map((w) => h("div", { class: "warnbox small" }, w)) : []),
      data.result && data.result.llm ? h("div", { class: "small muted" }, "LLM: ", data.result.llm.message || data.result.llm.status) : null);
    if (data.status === "registered") {
      fill(right, statusBar, h("div", { class: "info" }, "この取込みは登録済みです。 ", h("a", { href: "#/contract/" + data.contract_id }, "契約詳細を開く")));
      return;
    }
    if (active) {
      fill(right, statusBar, h("div", { class: "info" }, "解析が終わると抽出結果が表示されます。解析途中の内容は確定情報として登録しません。",
        h("div", {}, "待たずに入力する場合は「中止」を押すと、手動入力に進めます。")));
      return;
    }
    const startForm = data.form || { auto_renewal: "不明", review_status: "未確認", parties: [], signers: [] };
    fc = contractForm(startForm, { evidence: evidenceFor, onPage: gotoPage, edited: data.user_edited,
      onChange: () => { dirty = true; clearTimeout(saveTimer); saveTimer = setTimeout(() => saveDraft(true), 1500); } });
    const dupBox = h("div", {});
    if (data.duplicates && data.duplicates.length) dupBox.append(dupList(data.duplicates));
    const ack = h("input", { type: "checkbox" });
    const regMsg = h("div", {});
    fill(right, statusBar,
      !data.result ? h("div", { class: "warnbox" }, "抽出結果がありません。手動で入力して登録できます。") :
        h("div", { class: "info small" }, "抽出結果は候補です。PDFと照合し、必要に応じて修正してください。赤枠は要確認、黄色は未確認の抽出値、青は修正済みです。手修正した項目は再解析で上書きされません。"),
      dupBox, fc.el, regMsg,
      h("div", { class: "actions" },
        h("button", { onclick: () => saveDraft(false) }, "下書き保存"),
        h("label", { class: "small" }, ack, " 重複候補を確認した（別の契約として登録する）"),
        h("button", { class: "primary", onclick: register }, "登録を確定"),
        h("button", { class: "danger", style: "margin-left:auto", onclick: async () => { if (!confirm("この取込みを破棄しますか？（元のPDFファイルは削除されません）")) return; await api("DELETE", `/api/imports/${iid}`); location.hash = "#/import"; } }, "破棄")));
    async function register() {
      fill(regMsg, );
      try {
        const r = await api("POST", `/api/imports/${iid}/register`, { form: fc.payload(), ack_duplicates: ack.checked });
        dirty = false;
        toast(`管理番号 ${r.mgmt_no} で登録しました。`);
        location.hash = "#/contract/" + r.id;
      } catch (e) {
        fill(regMsg, errBox(e));
        if (e.body && e.body.duplicates) regMsg.append(dupList(e.body.duplicates));
        regMsg.scrollIntoView({ block: "nearest" });
      }
    }
  }
  async function poll() {
    try { data = await api("GET", `/api/imports/${iid}`); } catch (e) { showError(e, right); return; }
    render();
    if (data.status === "queued" || data.status === "processing") pollTimer = setTimeout(poll, 1000);
  }
  render();
  if (data.status === "queued" || data.status === "processing") poll();
}

function dupList(dups) {
  return h("div", { class: "warnbox" }, h("b", {}, "重複の可能性がある契約（自動では同一扱いしません。変更覚書・別契約の可能性もあります）"),
    h("ul", {}, dups.map((d) => h("li", {}, h("a", { href: "#/contract/" + d.id, target: "_blank" }, d.mgmt_no), " ", d.title || "", " ",
      d.contract_date ? fmtDate(d.contract_date) : "", d.deleted ? "（削除済み）" : "", h("span", { class: "small muted" }, " 理由: " + d.reasons.join("・"))))));
}

// ------------------------------------------------------------------ PDFなし手動登録
async function viewNewManual() {
  const fc = contractForm({ auto_renewal: "不明", review_status: "未確認", parties: [], signers: [] }, {});
  const msg = h("div", {});
  const ack = h("input", { type: "checkbox" });
  fill($app, h("h1", {}, "手動登録（PDFなし）"),
    h("div", { class: "info small" }, "紙のみの契約などを登録します。PDFがある場合は「取込み」から登録してください（PDFの保存とハッシュによる重複検知が行われます）。"),
    h("div", { style: "max-width:900px" }, fc.el, msg,
      h("div", { class: "actions" }, h("label", { class: "small" }, ack, " 重複候補を確認した"),
        h("button", { class: "primary", onclick: async () => {
          fill(msg, );
          try { const r = await api("POST", "/api/contracts", { form: fc.payload(), ack_duplicates: ack.checked }); toast(`管理番号 ${r.mgmt_no} で登録しました。`); location.hash = "#/contract/" + r.id; }
          catch (e) { fill(msg, errBox(e)); if (e.body && e.body.duplicates) msg.append(dupList(e.body.duplicates)); }
        } }, "登録"))));
}

// ------------------------------------------------------------------ 契約詳細
function formFromContract(c) {
  const f = {};
  for (const k of ["title", "doc_type", "category_id", "summary", "contract_date", "contract_date_raw", "start_date", "start_date_raw", "term_raw",
    "expiry_date", "expiry_raw", "expiry_type", "auto_renewal", "renewal_terms", "renewal_period_months", "renewal_notice_raw", "renewal_notice_json",
    "termination_notice_raw", "termination_notice_json", "notice_deadline_confirmed", "managed_expiry_date", "managed_expiry_note", "next_review_date",
    "dept", "owner", "paper_location", "notes", "review_status"]) f[k] = c[k] ?? null;
  f.parties = c.parties.map((p) => ({ name: p.name, role: p.role, label: p.label }));
  f.signers = c.signers.map((s) => ({ name: s.name, title: s.title, party_index: s.party_index }));
  f.field_states = {};
  for (const [k, m] of Object.entries(c.field_meta || {})) if (m && ["記載なし", "要確認", "読取不能"].includes(m.state)) f.field_states[k] = m.state;
  return f;
}

async function viewContract(cid) {
  $app.className = "full";
  const c = await api("GET", `/api/contracts/${cid}`);
  let pdfVer = 0;
  const iframe = h("iframe", { title: "PDF" });
  const gotoPage = (p) => { pdfVer++; iframe.src = `/api/contracts/${cid}/pdf?v=${pdfVer}#page=${p}`; };
  const left = h("div", { class: "left" });
  if (c.pdf_relpath && c.pdf_exists) { left.append(iframe); gotoPage(1); }
  else if (c.pdf_relpath) left.append(h("div", { class: "nopdf" }, "PDFファイルが見つかりません: ", c.pdf_relpath, h("div", {}, "設定画面の「整合性チェック」で確認してください。")));
  else left.append(h("div", { class: "nopdf" }, "PDFは登録されていません（手動登録）。"));
  const right = h("div", { class: "right" });
  fill($app, h("div", { class: "split" }, left, right));
  const meta = c.field_meta || {};
  const evidenceFor = (k) => meta[k] || null;

  const head = h("div", { class: "panel" },
    h("div", { class: "row" }, h("h1", { style: "margin:0" }, c.mgmt_no, " ", c.title || "（契約書名なし）"), stateBadge(c.review_status), c.deleted_at ? badge("削除済み", "b-check") : null),
    h("div", { class: "small muted" }, `カテゴリー: ${c.category_name || ""} ／ 登録 ${c.created_at.replace("T", " ")} ／ 更新 ${c.updated_at.replace("T", " ")}`, c.reviewed_at ? ` ／ 確認 ${c.reviewed_at.replace("T", " ")}` : ""),
    h("div", { class: "small muted" }, "PDF保存先: ", c.pdf_relpath || "なし", c.original_filename ? ` ／ 元ファイル名: ${c.original_filename}` : "", c.file_hash ? ` ／ SHA-256: ${c.file_hash.slice(0, 16)}…` : ""),
    h("div", { class: "row", style: "margin-top:8px" },
      c.pdf_relpath ? h("a", { class: "btn", href: `/api/contracts/${cid}/pdf`, target: "_blank" }, "PDFを別タブで開く") : null,
      ...META.review_states.map((s) => h("button", { disabled: c.review_status === s || !!c.deleted_at, onclick: async () => { await api("POST", `/api/contracts/${cid}/review`, { status: s }); route(); } }, `「${s}」にする`)),
      c.deleted_at ? h("button", { onclick: async () => { await api("POST", `/api/contracts/${cid}/restore`); toast("復元しました。"); route(); } }, "復元")
        : h("button", { class: "danger", style: "margin-left:auto", onclick: async () => { if (!confirm(`${c.mgmt_no} を削除しますか？（論理削除。PDFは削除されず、後で復元できます）`)) return; await api("POST", `/api/contracts/${cid}/delete`); toast("削除しました（復元できます）。"); route(); } }, "削除")));

  const editMsg = h("div", {});
  let fc = null;
  const editArea = h("div", {});
  function showView() {
    const dv = (k, v) => cellValue(v, meta[k]);
    const own = c.parties.filter((p) => p.role === "当社"), oth = c.parties.filter((p) => p.role !== "当社");
    fill(editArea, h("div", { class: "panel" },
      h("div", { class: "row" }, h("h2", { style: "margin:0" }, "登録内容"), h("button", { class: "primary", style: "margin-left:auto", disabled: !!c.deleted_at, onclick: showEdit }, "編集")),
      h("dl", { class: "kv", style: "margin-top:8px" },
        h("dt", {}, "契約書の種類"), h("dd", {}, dv("doc_type", c.doc_type)),
        h("dt", {}, "要旨"), h("dd", {}, dv("summary", c.summary)),
        h("dt", {}, "当社"), h("dd", {}, own.map((p) => `${p.label ? p.label + "：" : ""}${p.name}`).join("\n") || dv("parties", null)),
        h("dt", {}, "相手方"), h("dd", {}, oth.map((p) => `${p.label ? p.label + "：" : ""}${p.name}${p.role === "その他" ? "（その他）" : ""}`).join("\n") || dv("parties", null)),
        h("dt", {}, "署名者・代表者"), h("dd", {}, c.signers.map((s) => `${s.title || ""} ${s.name}${s.party_index !== null && c.parties[s.party_index] ? `（${c.parties[s.party_index].name}）` : ""}`).join("\n") || dv("signers", null)),
        h("dt", {}, "契約日"), h("dd", {}, dv("contract_date", c.contract_date && fmtDate(c.contract_date)), c.contract_date_raw ? `（原文: ${c.contract_date_raw}）` : ""),
        h("dt", {}, "契約開始日"), h("dd", {}, dv("start_date", c.start_date && fmtDate(c.start_date)), c.start_date_raw ? `（原文: ${c.start_date_raw}）` : ""),
        h("dt", {}, "契約期間の原文"), h("dd", {}, dv("term_raw", c.term_raw)),
        h("dt", {}, "期限の区分"), h("dd", {}, dv("expiry_type", c.expiry_type)),
        h("dt", {}, "有効期限"), h("dd", {}, dv("expiry_date", c.expiry_date && fmtDate(c.expiry_date))),
        h("dt", {}, "管理上の期限"), h("dd", {}, c.managed_expiry_date ? `${fmtDate(c.managed_expiry_date)} ${c.managed_expiry_note || ""}` : "—"),
        h("dt", {}, "自動更新"), h("dd", {}, dv("auto_renewal", c.auto_renewal), c.renewal_period_months ? `（更新期間 ${c.renewal_period_months}か月）` : ""),
        h("dt", {}, "更新条件"), h("dd", {}, dv("renewal_terms", c.renewal_terms)),
        h("dt", {}, "更新拒絶の通知条件"), h("dd", {}, dv("renewal_notice_raw", c.renewal_notice_raw), c.renewal_notice_json ? `\n構造化: 満了日の${c.renewal_notice_json.value}${c.renewal_notice_json.unit === "月" ? "か月" : c.renewal_notice_json.unit}前まで` : ""),
        h("dt", {}, "通知期限"), h("dd", {}, c.notice_deadline_confirmed ? [fmtDate(c.notice_deadline_confirmed), " ", badge("確定", "b-ok")] : h("span", { class: "empty-cell" }, "確定値なし"),
          c.notice_deadline_candidate ? h("div", { class: "calc" }, "候補（未確定）: ", fmtDate(c.notice_deadline_candidate), h("div", {}, c.notice_deadline_calc)) : h("div", { class: "small muted" }, c.notice_deadline_calc || "")),
        c.next_term_candidate ? [h("dt", {}, "次回満了日（候補）"), h("dd", {}, h("div", { class: "calc" }, fmtDate(c.next_term_candidate.date), h("div", {}, c.next_term_candidate.calc)))] : null,
        h("dt", {}, "中途解約の通知条件"), h("dd", {}, dv("termination_notice_raw", c.termination_notice_raw)),
        h("dt", {}, "次回確認予定日"), h("dd", {}, c.next_review_date ? fmtDate(c.next_review_date) : "—"),
        h("dt", {}, "社内担当部門・担当者"), h("dd", {}, [c.dept, c.owner].filter(Boolean).join(" ／ ") || "—"),
        h("dt", {}, "紙原本の保管場所"), h("dd", {}, c.paper_location || "—"),
        h("dt", {}, "備考"), h("dd", {}, c.notes || "—")),
      evidencePanel()));
  }
  function evidencePanel() {
    const rows = Object.entries(meta).filter(([, m]) => m && (m.page || m.evidence || m.note));
    if (!rows.length) return null;
    const label = { title: "契約書名", doc_type: "種類", category_id: "カテゴリー", summary: "要旨", contract_date: "契約日", start_date: "開始日", term_raw: "契約期間", expiry_date: "有効期限", expiry_type: "期限の区分", auto_renewal: "自動更新", renewal_terms: "更新条件", renewal_notice_raw: "更新拒絶通知", termination_notice_raw: "中途解約通知", parties: "当事者", signers: "署名者" };
    return h("details", { style: "margin-top:8px" }, h("summary", {}, "抽出時の根拠・状態"),
      h("table", { class: "list small" }, h("tbody", {}, rows.map(([k, m]) => h("tr", {},
        h("td", {}, label[k] || k), h("td", {}, stateBadge(m.state)),
        h("td", {}, m.page ? h("button", { class: "small", onclick: () => gotoPage(m.page) }, `p.${m.page}`) : ""),
        h("td", {}, m.source === "ocr" ? "OCR" : m.source === "text" ? "テキスト" : ""),
        h("td", {}, Array.isArray(m.evidence) ? m.evidence.map((e) => e.evidence).filter(Boolean).join(" / ") : (m.evidence || ""), m.note ? h("div", { class: "note" }, m.note) : null))))));
  }
  function showEdit() {
    fc = contractForm(formFromContract(c), { evidence: evidenceFor, onPage: gotoPage });
    fill(editArea, h("div", { class: "panel" }, h("h2", { style: "margin-top:0" }, "編集"),
      h("div", { class: "small muted" }, "カテゴリーを変更すると、PDFは新しいカテゴリーのフォルダへ移動します（ファイル名は変わりません）。"),
      fc.el, editMsg,
      h("div", { class: "actions" }, h("button", { class: "primary", onclick: save }, "保存"), h("button", { onclick: () => { fill(editMsg, ); showView(); } }, "取消"))));
  }
  async function save() {
    fill(editMsg, );
    try { await api("PUT", `/api/contracts/${cid}`, { form: fc.payload() }); toast("保存しました。"); route(); }
    catch (e) { fill(editMsg, errBox(e)); }
  }
  showView();

  // 関連契約
  const relSel = h("select", {}, META.relation_types.map((t) => h("option", {}, t)));
  const relDir = h("select", {}, h("option", { value: "out" }, "この契約 → 相手"), h("option", { value: "in" }, "相手 → この契約"));
  const relNo = h("input", { type: "text", placeholder: "相手の管理番号（例: C000002）", style: "width:200px" });
  const relNote = h("input", { type: "text", placeholder: "メモ", style: "width:200px" });
  const rel = h("div", { class: "panel" }, h("h2", { style: "margin-top:0" }, "関連契約"),
    h("div", { class: "small muted" }, "例：基本契約→個別契約、原契約→変更覚書。関連付けても元の契約条件は上書きしません。覚書で期限が変わる場合は、確認のうえ「管理上の期限」に入力してください。"),
    c.relations.length ? h("table", { class: "list small" }, h("tbody", {}, c.relations.map((r) => h("tr", {},
      h("td", {}, r.rel_type), h("td", {}, r.direction), h("td", {}, h("a", { href: "#/contract/" + r.other_id }, r.other_mgmt_no), " ", r.other_title || ""),
      h("td", {}, r.other_expiry ? "期限 " + fmtDate(r.other_expiry) : ""), h("td", {}, r.note || ""),
      h("td", {}, h("button", { class: "small danger", onclick: async () => { await api("DELETE", `/api/relations/${r.id}`); route(); } }, "解除")))))) : h("div", { class: "muted small" }, "関連契約はありません。"),
    h("div", { class: "row", style: "margin-top:6px" }, relSel, relDir, relNo, relNote,
      h("button", { onclick: async () => { try { await api("POST", `/api/contracts/${cid}/relations`, { rel_type: relSel.value, direction: relDir.value, other_mgmt_no: relNo.value, note: relNote.value }); route(); } catch (e) { showError(e, rel); } } }, "関連付け")));

  const dups = c.duplicates && c.duplicates.length ? dupList(c.duplicates) : null;
  const histBox = h("div", { class: "panel hist" }, h("h2", { style: "margin-top:0" }, "修正履歴"), h("div", { class: "muted" }, "読み込み中…"));
  fill(right, head, dups || "", editArea, rel, histBox);
  const hist = await api("GET", `/api/contracts/${cid}/history`);
  fill(histBox, h("h2", { style: "margin-top:0" }, "修正履歴"), ...hist.map((x) => h("div", { class: "item" },
    h("b", {}, x.at.replace("T", " "), " ", x.action),
    x.changes.length ? h("ul", {}, x.changes.map((ch) => h("li", {}, ch.label, ": ", fmtVal(ch.old), " → ", fmtVal(ch.new)))) : null)));
}
function fmtVal(v) {
  if (v === null || v === undefined || v === "") return "（空）";
  if (typeof v === "object") { if (Array.isArray(v)) return v.map((x) => x.name || JSON.stringify(x)).join("、") || "（なし）"; return JSON.stringify(v); }
  const s = String(v); return s.length > 80 ? s.slice(0, 80) + "…" : s;
}

// ------------------------------------------------------------------ 設定
async function viewSettings() {
  const st = await api("GET", "/api/settings");
  const msg = h("div", {});
  const ok = (t) => fill(msg, h("div", { class: "info" }, t));
  const fail = (e) => fill(msg, errBox(e));

  // 自社名・期限表示
  const company = h("textarea", { rows: 3 }, (st.company_names || []).join("\n"));
  const de = h("input", { type: "number", value: st.alert_days_expiry, style: "width:90px" });
  const dn = h("input", { type: "number", value: st.alert_days_notice, style: "width:90px" });
  const mmb = h("input", { type: "number", value: st.max_pdf_mb, style: "width:90px" });
  const mpg = h("input", { type: "number", value: st.max_pages, style: "width:90px" });
  const general = h("div", { class: "panel" }, h("h2", { style: "margin-top:0" }, "自社名・期限表示・取込み上限"),
    h("div", { class: "set-grid" },
      h("label", {}, "自社名・別名（1行に1つ）"), company,
      h("label", {}, "有効期限の事前表示日数"), de, h("label", {}, "通知期限の事前表示日数"), dn,
      h("label", {}, "PDFサイズ上限（MB）"), mmb, h("label", {}, "ページ数上限"), mpg),
    h("div", { class: "small muted" }, "上限はメモリ・処理時間の超過を防ぐための制限です。自社名は当事者の「当社／相手方」判別に使います。"),
    h("button", { class: "primary", onclick: async () => { try { await api("PUT", "/api/settings", { company_names: company.value.split("\n"), alert_days_expiry: de.value, alert_days_notice: dn.value, max_pdf_mb: mmb.value, max_pages: mpg.value }); ok("保存しました。"); } catch (e) { fail(e); } } }, "保存"));

  // OCR・LLM
  const ocrEng = h("select", {}, h("option", { value: "rapidocr", selected: st.ocr.engine === "rapidocr" }, "RapidOCR（推奨・同梱モデル）"), h("option", { value: "tesseract", selected: st.ocr.engine === "tesseract" }, "Tesseract"), h("option", { value: "none", selected: st.ocr.engine === "none" }, "使用しない"));
  const ocrPath = h("input", { type: "text", value: st.ocr.tesseract_path, placeholder: "空欄なら既定の場所（C:\\Program Files\\Tesseract-OCR\\tesseract.exe 等）を探します" });
  const ocrLang = h("input", { type: "text", value: st.ocr.languages });
  const ocrDpi = h("input", { type: "number", value: st.ocr.dpi, style: "width:90px" });
  const ocrTo = h("input", { type: "number", value: st.ocr.page_timeout_sec, style: "width:90px" });
  const llmEng = h("select", {}, h("option", { value: "none", selected: st.llm.engine === "none" }, "使用しない（ルール抽出のみ）"), h("option", { value: "openai_compat", selected: st.llm.engine === "openai_compat" }, "ローカルLLMサーバー（OpenAI互換）"));
  const llmUrl = h("input", { type: "text", value: st.llm.base_url });
  const llmModel = h("input", { type: "text", value: st.llm.model, placeholder: "llama-server は空欄可。Ollama はモデル名（例: qwen3:4b）" });
  const llmTo = h("input", { type: "number", value: st.llm.timeout_sec, style: "width:90px" });
  const llmChunk = h("input", { type: "number", value: st.llm.chunk_chars, style: "width:90px" });
  const llmRetry = h("input", { type: "number", value: st.llm.max_retries, style: "width:90px" });
  const chk = h("div", {});
  const showChk = (r) => fill(chk, h("div", { class: r.ok ? "info" : "err" }, r.message, r.hint ? h("div", { class: "hint" }, "対処: " + r.hint) : null,
    r.detail ? h("div", { class: "small" }, JSON.stringify(r.detail)) : null));
  const engines = h("div", { class: "panel" }, h("h2", { style: "margin-top:0" }, "OCR・LLM"),
    h("div", { class: "set-grid" },
      h("label", {}, "OCRエンジン"), ocrEng, h("label", {}, "tesseract.exe の場所"), ocrPath, h("label", {}, "OCR言語（Tesseractのみ）"), ocrLang,
      h("label", {}, "OCR解像度（dpi）"), ocrDpi, h("label", {}, "OCRタイムアウト（秒/ページ）"), ocrTo,
      h("label", {}, "LLM"), llmEng, h("label", {}, "接続先URL（このPC内のみ）"), llmUrl, h("label", {}, "モデル名"), llmModel,
      h("label", {}, "LLMタイムアウト（秒/部分）"), llmTo, h("label", {}, "分割サイズ（文字）"), llmChunk, h("label", {}, "再試行回数"), llmRetry),
    h("div", { class: "small muted" }, "LLMの接続先は 127.0.0.1 / localhost に限られ、外部のサーバーは指定できません。"),
    h("div", { class: "row" },
      h("button", { class: "primary", onclick: async () => { try { await api("PUT", "/api/settings", { ocr: { engine: ocrEng.value, tesseract_path: ocrPath.value, languages: ocrLang.value, dpi: ocrDpi.value, page_timeout_sec: ocrTo.value }, llm: { engine: llmEng.value, base_url: llmUrl.value, model: llmModel.value, timeout_sec: llmTo.value, chunk_chars: llmChunk.value, max_retries: llmRetry.value } }); ok("保存しました。"); } catch (e) { fail(e); } } }, "保存"),
      h("button", { onclick: async () => { fill(chk, "確認中…"); showChk(await api("POST", "/api/check/ocr")); } }, "OCRの動作確認"),
      h("button", { onclick: async () => { fill(chk, "確認中…"); showChk(await api("POST", "/api/check/llm")); } }, "LLMの動作確認")), chk);

  // カテゴリー
  const newCat = h("input", { type: "text", placeholder: "新しいカテゴリー名", style: "width:220px" });
  const newKw = h("input", { type: "text", placeholder: "候補判定キーワード（カンマ区切り）", style: "width:300px" });
  const catMsg = h("div", {});
  const catPanel = h("div", { class: "panel" }, h("h2", { style: "margin-top:0" }, "カテゴリー（保存フォルダ）"),
    h("div", { class: "small muted" }, "カテゴリー名がそのまま保存フォルダ名になります。名称変更すると、該当PDFをフォルダごと移動します（実行前に影響対象を表示します）。無効化したカテゴリーには新規登録できません。"),
    h("table", { class: "list" }, h("thead", {}, h("tr", {}, h("th", {}, "名称"), h("th", {}, "キーワード"), h("th", {}, "件数"), h("th", {}, "状態"), h("th", {}, ""))),
      h("tbody", {}, META.categories.map((c) => {
        const kw = h("input", { type: "text", value: c.keywords });
        return h("tr", {}, h("td", {}, c.name), h("td", {}, kw), h("td", {}, c.n), h("td", {}, c.active ? "有効" : badge("無効", "b-none")),
          h("td", {}, h("div", { class: "row" },
            h("button", { class: "small", onclick: async () => { await api("PUT", `/api/categories/${c.id}`, { keywords: kw.value }); toast("キーワードを保存しました。"); } }, "キーワード保存"),
            h("button", { class: "small", onclick: () => renameCat(c) }, "名称変更"),
            h("button", { class: "small", onclick: async () => { await api("PUT", `/api/categories/${c.id}`, { active: !c.active }); route(); } }, c.active ? "無効化" : "有効化"))));
      }))),
    h("div", { class: "row", style: "margin-top:8px" }, newCat, newKw, h("button", { onclick: async () => { try { await api("POST", "/api/categories", { name: newCat.value, keywords: newKw.value }); route(); } catch (e) { fill(catMsg, errBox(e)); } } }, "追加"),
      h("button", { onclick: async () => { const r = await api("POST", "/api/categories/sync-folders"); fill(catMsg, h("div", { class: r.failed.length ? "err" : "info" }, r.failed.length ? `移動できなかったPDF: ${r.failed.map((f) => f.mgmt_no + " " + f.error).join(" / ")}` : "すべてのPDFがカテゴリーのフォルダに揃っています。")); } }, "フォルダの整合を再実行")), catMsg);
  async function renameCat(c) {
    const name = prompt(`「${c.name}」の新しい名称`, c.name);
    if (!name || name === c.name) return;
    try {
      const pv = await api("POST", `/api/categories/${c.id}/rename-preview`, { name });
      const list = pv.affected.slice(0, 20).map((a) => `  ${a.mgmt_no} ${a.title || ""}`).join("\n");
      if (!confirm(`${pv.message}\n${list}${pv.affected.length > 20 ? "\n  …" : ""}\n\n実行しますか？`)) return;
      const r = await api("POST", `/api/categories/${c.id}/rename`, { name });
      fill(catMsg, h("div", { class: r.failed.length ? "err" : "info" }, `名称を変更しました。移動 ${r.moved} 件。`, r.failed.length ? ` 移動できなかったPDF（旧フォルダのまま参照できます。「フォルダの整合を再実行」で再試行）: ${r.failed.map((f) => f.mgmt_no).join("、")}` : ""));
      await loadMeta();
    } catch (e) { fill(catMsg, errBox(e)); }
  }

  // 保存ルート
  const def = st.storage_roots.find((r) => r.is_default);
  const rootIn = h("input", { type: "text", placeholder: "例: D:\\契約書" });
  const rootMode = h("select", {}, h("option", { value: "new_only" }, "新規保存先のみ変更（既存PDFは今の場所のまま）"), h("option", { value: "migrate" }, "既存PDFも新しい場所へ移行する"));
  const rootMsg = h("div", {});
  const rootPanel = h("div", { class: "panel" }, h("h2", { style: "margin-top:0" }, "PDF保存ルート"),
    h("div", {}, "現在の新規保存先: ", h("b", {}, def ? def.path : "")),
    h("table", { class: "list small" }, h("tbody", {}, st.storage_roots.map((r) => h("tr", {}, h("td", {}, r.path), h("td", {}, r.is_default ? "新規保存先" : ""), h("td", {}, `${r.n} 件`))))),
    h("div", { class: "row", style: "margin-top:8px" }, rootIn, rootMode, h("button", { onclick: async () => {
      if (rootMode.value === "migrate" && !confirm("既存のPDFをすべて新しい保存ルートへ移行します。件数が多い場合は時間がかかります。実行前にバックアップを取ることをお勧めします。実行しますか？")) return;
      try { const r = await api("POST", "/api/storage-roots", { path: rootIn.value, mode: rootMode.value }); fill(rootMsg, h("div", { class: r.failed.length ? "err" : "info" }, `変更しました（${r.path}）。移行 ${r.migrated} 件`, r.failed.length ? `、失敗 ${r.failed.length} 件: ${r.failed.map((f) => f.mgmt_no).join("、")}` : "")); }
      catch (e) { fill(rootMsg, errBox(e)); }
    } }, "変更")), rootMsg);

  // バックアップ
  const bk = await api("GET", "/api/backups");
  const bkMsg = h("div", {});
  const upIn = h("input", { type: "file", accept: ".zip", class: "hidden", onchange: async (e) => { const fd = new FormData(); fd.append("file", e.target.files[0]); try { await api("POST", "/api/backups/upload", fd, true); route(); } catch (er) { fill(bkMsg, errBox(er)); } } });
  const rootOverride = h("input", { type: "text", placeholder: "（任意）PDFの復元先フォルダ。空欄ならバックアップ時の場所" });
  const bkPanel = h("div", { class: "panel" }, h("h2", { style: "margin-top:0" }, "バックアップ・復元"),
    h("div", { class: "small muted" }, "台帳・設定・抽出結果・PDF・下書きを1つのZIPに保存します。保存先: ", bk.dir),
    h("div", { class: "row" }, h("button", { class: "primary", onclick: async () => { fill(bkMsg, h("span", { class: "spinner" }), " 作成中…"); try { const r = await api("POST", "/api/backups"); fill(bkMsg, h("div", { class: r.verify.ok ? "info" : "err" }, `作成しました: ${r.name}（契約 ${r.manifest.counts.contracts} 件 / PDF ${r.manifest.counts.pdfs} 件）検証: ${r.verify.ok ? "OK" : r.verify.problems.join(" / ")}`, r.manifest.missing_pdfs.length ? ` 見つからなかったPDF: ${r.manifest.missing_pdfs.join("、")}` : "")); setTimeout(route, 1500); } catch (e) { fill(bkMsg, errBox(e)); } } }, "今すぐバックアップ"),
      h("button", { onclick: () => upIn.click() }, "バックアップファイルを読み込む"), upIn),
    bkMsg,
    h("table", { class: "list small" }, h("thead", {}, h("tr", {}, h("th", {}, "ファイル"), h("th", {}, "作成日時"), h("th", {}, "種別"), h("th", {}, "契約"), h("th", {}, "PDF"), h("th", {}, ""))),
      h("tbody", {}, bk.items.map((b) => h("tr", {}, h("td", {}, b.name), h("td", {}, (b.created_at || "").replace("T", " ")), h("td", {}, b.label || ""), h("td", {}, b.contracts ?? ""), h("td", {}, b.pdfs ?? ""),
        h("td", {}, h("button", { class: "small", onclick: () => restoreFlow(b.name) }, "復元…")))))),
    h("div", { class: "row", style: "margin-top:6px" }, h("label", { class: "small" }, "復元先の指定"), rootOverride));
  async function restoreFlow(name) {
    try {
      const pv = await api("GET", `/api/backups/${encodeURIComponent(name)}/preview`);
      const txt = [`バックアップ: ${name}`, `作成: ${pv.backup.created_at}（${pv.backup.label || ""}）`, `内容: 契約 ${pv.backup.counts.contracts} 件 / PDF ${pv.backup.counts.pdfs} 件`,
        `現在: 契約 ${pv.current.contracts} 件`, `検証: ${pv.verify.ok ? "OK" : pv.verify.problems.join(" / ")}`, "", "影響:", ...pv.effects.map((e) => "・" + e),
        ...(pv.warnings.length ? ["", "注意:", ...pv.warnings.map((w) => "・" + w)] : [])].join("\n");
      if (!pv.can_restore) { alert(txt + "\n\nこのバックアップは復元できません。"); return; }
      const conf = prompt(txt + "\n\n実行するには、バックアップ名を入力してください:");
      if (conf !== name) { if (conf !== null) alert("名前が一致しないため中止しました。"); return; }
      fill(bkMsg, h("span", { class: "spinner" }), " 復元中…");
      const r = await api("POST", `/api/backups/${encodeURIComponent(name)}/restore`, { confirm: name, root_override: rootOverride.value });
      fill(bkMsg, h("div", { class: r.pdf_problems.length ? "err" : "info" }, `復元しました。契約 ${r.contracts} 件。復元前のデータは ${r.pre_restore_backup} に保存しました。`,
        r.pdf_problems.length ? ` PDF参照の問題: ${r.pdf_problems.map((p) => p.mgmt_no + " " + p.problem).join(" / ")}` : " PDF参照の検証: 問題なし",
        r.renamed_existing.length ? ` 同名で内容の異なるファイル ${r.renamed_existing.length} 件を改名して残しました。` : ""));
    } catch (e) { fill(bkMsg, errBox(e)); }
  }

  // 整合性
  const intMsg = h("div", {});
  const intPanel = h("div", { class: "panel" }, h("h2", { style: "margin-top:0" }, "整合性チェック・復旧"),
    h("div", { class: "small muted" }, "台帳が参照するPDFの存在と内容（ハッシュ）を検証し、途中で中断したファイル操作を確認します。"),
    h("div", { class: "row" }, h("button", { onclick: async () => { const r = await api("GET", "/api/integrity"); fill(intMsg, h("div", { class: r.pdf_problems.length || r.unresolved_file_ops.length ? "err" : "info" },
      r.pdf_problems.length ? r.pdf_problems.map((p) => `${p.mgmt_no}: ${p.problem}${p.path ? "（" + p.path + "）" : ""}`).join("\n") : "PDF参照: 問題なし",
      "\n", r.unresolved_file_ops.length ? `未完了のファイル操作 ${r.unresolved_file_ops.length} 件（「復旧を実行」で整合させます）` : "未完了のファイル操作: なし")); } }, "整合性チェック"),
      h("button", { onclick: async () => { const r = await api("POST", "/api/recover"); fill(intMsg, h("div", { class: "info" }, r.results.length ? r.results.map((x) => `操作${x.id}(${x.op}): ${x.result}`).join(" / ") : "未完了の操作はありません。")); } }, "復旧を実行")), intMsg,
    h("div", { class: "small muted" }, "データフォルダ: ", META.data_dir));

  fill($app, h("h1", {}, "設定"), msg, general, engines, catPanel, rootPanel, bkPanel, intPanel);
}
