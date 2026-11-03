"""実行時パスと起動設定。

データディレクトリは以下の優先順で決まる。
1. 環境変数 CONTRACT_LEDGER_DATA
2. <アプリのルート>/data

業務上の設定（カテゴリー、保存ルート、OCR/LLM等）は SQLite の settings テーブルに保存する。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Paths:
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ledger.db"

    @property
    def tmp_dir(self) -> Path:
        # 取込み中PDFの一時領域（元ファイルのコピーを置く）
        return self.data_dir / "imports"

    @property
    def work_dir(self) -> Path:
        # OCR用ページ画像などの短期作業領域
        return self.data_dir / "work"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def default_storage_root(self) -> Path:
        return self.data_dir / "契約書"

    def ensure(self) -> None:
        for p in (self.data_dir, self.tmp_dir, self.work_dir, self.log_dir, self.backup_dir, self.export_dir):
            p.mkdir(parents=True, exist_ok=True)


def resolve_paths(data_dir: str | os.PathLike | None = None) -> Paths:
    if data_dir is None:
        data_dir = os.environ.get("CONTRACT_LEDGER_DATA") or (APP_ROOT / "data")
    p = Paths(Path(data_dir).resolve())
    p.ensure()
    return p


# 既定の業務設定（初回起動時に settings テーブルへ書き込む）
DEFAULT_SETTINGS: dict = {
    "company_names": [],            # 自社名・別名
    "alert_days_expiry": 90,        # 有効期限の事前表示日数
    "alert_days_notice": 60,        # 通知期限の事前表示日数
    "max_pdf_mb": 100,              # 取込み可能なPDFサイズ上限
    "max_pages": 200,               # 取込み可能なページ数上限
    "ocr": {
        "engine": "rapidocr",       # rapidocr / tesseract / none
        "rapidocr_model_dir": "",   # 空ならパッケージ同梱のモデルを使う
        "tesseract_path": "",       # 空なら既定の場所とPATHを探す
        "languages": "jpn+jpn_vert",
        "dpi": 300,
        "page_timeout_sec": 120,
    },
    "llm": {
        "engine": "none",           # openai_compat / none
        "base_url": "http://127.0.0.1:8081/v1",
        "model": "",
        "timeout_sec": 600,
        "chunk_chars": 6000,
        "max_retries": 1,
        "temperature": 0.0,
    },
    "text_min_chars": 20,           # 既存テキストを「利用可能」とみなす最小文字数
}

DEFAULT_CATEGORIES = [
    ("廃棄物処理関連", "廃棄物,産業廃棄物,収集運搬,処分,マニフェスト"),
    ("解体工事関連", "解体,撤去,工事請負"),
    ("設備・車両", "車両,設備,リース,機械,保守"),
    ("不動産", "賃貸借,土地,建物,不動産,駐車場"),
    ("業務委託", "業務委託,委託"),
    ("秘密保持", "秘密保持,機密保持,NDA"),
    ("その他", ""),
]

DOC_TYPES = ["業務委託契約", "売買契約", "秘密保持契約", "請負契約", "賃貸借契約", "覚書", "その他"]
