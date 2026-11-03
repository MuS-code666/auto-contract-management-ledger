"""SQLite 接続とスキーマ移行。

- 接続は処理ごとに開く（スレッド間で共有しない）。
- スキーマ変更は MIGRATIONS に関数を追加して行う。移行前にDBの退避コピーを
  sqlite3 のバックアップAPIで作成する（稼働中ファイルの単純コピーはしない）。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from .config import DEFAULT_CATEGORIES, DEFAULT_SETTINGS


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ～ COMMIT。例外時は ROLLBACK。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------- migrations

def _exec(conn: sqlite3.Connection, script: str) -> None:
    """トランザクション内で複数のDDLを実行する（executescript は暗黙にCOMMITするため使わない）。"""
    for stmt in script.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def _m1(conn: sqlite3.Connection) -> None:
    _exec(
        conn,
        """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
        CREATE TABLE storage_roots (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            folder_name TEXT NOT NULL,
            keywords TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE contracts (
            id TEXT PRIMARY KEY,
            mgmt_no TEXT NOT NULL UNIQUE,
            title TEXT,
            doc_type TEXT,
            category_id INTEGER REFERENCES categories(id),
            summary TEXT,
            contract_date TEXT, contract_date_raw TEXT,
            start_date TEXT, start_date_raw TEXT,
            term_raw TEXT,
            expiry_date TEXT, expiry_raw TEXT,
            expiry_type TEXT,
            auto_renewal TEXT,
            renewal_terms TEXT,
            renewal_period_months INTEGER,
            renewal_notice_raw TEXT, renewal_notice_json TEXT,
            termination_notice_raw TEXT, termination_notice_json TEXT,
            notice_deadline_candidate TEXT, notice_deadline_calc TEXT,
            notice_deadline_confirmed TEXT,
            managed_expiry_date TEXT, managed_expiry_note TEXT,
            next_review_date TEXT,
            dept TEXT, owner TEXT, paper_location TEXT, notes TEXT,
            storage_root_id INTEGER REFERENCES storage_roots(id),
            pdf_relpath TEXT,
            original_filename TEXT, file_hash TEXT, file_size INTEGER, page_count INTEGER,
            review_status TEXT NOT NULL DEFAULT '未確認',
            field_meta TEXT NOT NULL DEFAULT '{}',
            import_id TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            reviewed_at TEXT, deleted_at TEXT
        );
        CREATE INDEX ix_contracts_hash ON contracts(file_hash);
        CREATE INDEX ix_contracts_cat ON contracts(category_id);
        CREATE TABLE parties (
            id INTEGER PRIMARY KEY,
            contract_id TEXT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT '相手方',
            label TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE signers (
            id INTEGER PRIMARY KEY,
            contract_id TEXT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
            party_id INTEGER REFERENCES parties(id) ON DELETE SET NULL,
            name TEXT NOT NULL,
            title TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE relations (
            id INTEGER PRIMARY KEY,
            src_id TEXT NOT NULL REFERENCES contracts(id),
            dst_id TEXT NOT NULL REFERENCES contracts(id),
            rel_type TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (src_id, dst_id, rel_type)
        );
        CREATE TABLE history (
            id INTEGER PRIMARY KEY,
            contract_id TEXT NOT NULL,
            at TEXT NOT NULL,
            action TEXT NOT NULL,
            changes TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX ix_history_contract ON history(contract_id);
        CREATE TABLE imports (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            stage TEXT,
            progress TEXT,
            original_filename TEXT,
            file_hash TEXT,
            file_size INTEGER,
            page_count INTEGER,
            tmp_path TEXT,
            error TEXT,
            error_hint TEXT,
            result_json TEXT,
            form_json TEXT,
            user_edited TEXT NOT NULL DEFAULT '[]',
            contract_id TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        );
        CREATE TABLE import_pages (
            import_id TEXT NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
            page_no INTEGER NOT NULL,
            text_source TEXT NOT NULL,
            status TEXT NOT NULL,
            chars INTEGER NOT NULL DEFAULT 0,
            text TEXT,
            note TEXT,
            PRIMARY KEY (import_id, page_no)
        );
        CREATE TABLE file_ops (
            id INTEGER PRIMARY KEY,
            op TEXT NOT NULL,
            contract_id TEXT,
            import_id TEXT,
            src_root_id INTEGER, src_rel TEXT,
            dst_root_id INTEGER, dst_rel TEXT,
            src_abs TEXT,
            file_hash TEXT,
            state TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_m1]
SCHEMA_VERSION = len(MIGRATIONS)


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(db_path: Path, backup_dir: Path | None = None) -> int:
    """必要な移行を順に適用する。移行前に既存DBを退避する。"""
    conn = connect(db_path)
    try:
        ver = current_version(conn)
        if ver > SCHEMA_VERSION:
            raise RuntimeError(
                f"データベースのスキーマ版({ver})がアプリ({SCHEMA_VERSION})より新しいため起動できません。"
                "新しい版のアプリを使用してください。"
            )
        if ver < SCHEMA_VERSION and ver > 0 and backup_dir is not None:
            backup_dir.mkdir(parents=True, exist_ok=True)
            dest = backup_dir / f"pre_migrate_v{ver}_{datetime.now():%Y%m%d_%H%M%S}.db"
            snapshot_db(conn, dest)
        for i in range(ver, SCHEMA_VERSION):
            conn.execute("BEGIN IMMEDIATE")
            try:
                MIGRATIONS[i](conn)
                conn.execute(f"PRAGMA user_version = {i + 1}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return SCHEMA_VERSION
    finally:
        conn.close()


def snapshot_db(conn: sqlite3.Connection, dest: Path) -> None:
    """稼働中DBの整合したスナップショットをバックアップAPIで作成する。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(str(dest))
    try:
        conn.backup(out)
    finally:
        out.close()


def seed_defaults(conn: sqlite3.Connection, default_root: Path) -> None:
    ts = now_iso()
    with tx(conn):
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (k, json.dumps(v, ensure_ascii=False)),
            )
        conn.execute("INSERT OR IGNORE INTO counters(name, value) VALUES ('mgmt_no', 0)")
        if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            for i, (name, kw) in enumerate(DEFAULT_CATEGORIES):
                conn.execute(
                    "INSERT INTO categories(name, folder_name, keywords, active, sort_order, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (name, name, kw, 1, i, ts, ts),
                )
        if conn.execute("SELECT COUNT(*) FROM storage_roots").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO storage_roots(path, is_default, created_at) VALUES (?, 1, ?)",
                (str(default_root), ts),
            )
    default_root.mkdir(parents=True, exist_ok=True)


def get_setting(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key)
    return json.loads(row["value"])


def all_settings(conn: sqlite3.Connection) -> dict:
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    for r in conn.execute("SELECT key, value FROM settings"):
        out[r["key"]] = json.loads(r["value"])
    # 入れ子の既定値を補完（新しい版で追加された項目）
    for k in ("ocr", "llm"):
        merged = dict(DEFAULT_SETTINGS[k])
        merged.update(out.get(k) or {})
        out[k] = merged
    return out


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
