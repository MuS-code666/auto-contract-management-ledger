# CLAUDE.md

このファイルは、本リポジトリで作業する Claude Code 向けのガイドです。

## プロジェクト概要

**auto-contract-management-ledger**（契約管理台帳の自動化）

契約情報を台帳として一元管理し、登録・更新・期限管理などを自動化するためのプロジェクトです。

スキャンした契約書PDFからローカル環境（OCR・ローカルLLM）で管理項目を抽出し、利用者の確認後に台帳へ登録する Windows 向けローカルWebアプリ。詳細は README.md と docs/設計.md を参照。

## 技術スタック

- 言語 / フレームワーク: Python 3.12、FastAPI + Uvicorn（127.0.0.1 のみ）、画面は素の HTML/CSS/JS（app/static、外部依存なし）
- PDF: pypdfium2 ／ OCR: RapidOCR（既定）・Tesseract ／ LLM: llama.cpp llama-server（OpenAI互換・ループバック限定）
- データ保存先: SQLite（`data/ledger.db`）、PDFは `data/契約書/<カテゴリー>/`（いずれも git 管理外）
- ディレクトリ: `app/`（本体）、`tests/`、`scripts/`（セットアップ・起動・サンプル生成・精度評価）、`samples/`（架空の契約書PDF）、`docs/`

## よく使うコマンド

```bash
# 依存関係のインストール（Windows）
setup.bat                                   # 開発用は: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Dev

# 実行
start.bat                                   # = .venv\Scripts\python -m app.main（http://127.0.0.1:8765/）

# テスト
.venv\Scripts\python -m pytest -q           # 実OCR・実LLMのみ: -m real（実LLMは LEDGER_LLM_URL が必要）
.venv\Scripts\python scripts\evaluate.py --set all --ocr rapidocr   # 抽出精度の評価
```

## Git 運用ルール

- **コードを変更するたびに、コミットして GitHub にプッシュすること。**
  - 1 つのまとまった変更（機能追加・修正・リファクタリングなど）が完了したら、その都度 `git add` → `git commit` → `git push` まで行う。
  - 変更をローカルに溜め込まない。
- コミット前に、テストやビルドが通ることを確認する（手順が整備されている場合）。
- コミットメッセージは変更内容が分かるように簡潔に書く（日本語可）。
  - 例: `契約期限アラート機能を追加`、`台帳CSV出力の文字化けを修正`
- 秘密情報（API キー、パスワード、`.env` ファイル、実際の契約データなど）はコミットしない。必要に応じて `.gitignore` に追加する。
- プッシュに失敗した場合（認証エラー、リモートとの競合など）は、強制プッシュ（`--force`）で解決せず、状況をユーザーに報告する。

## 開発上の注意

- 契約データには個人情報・機密情報が含まれる可能性があるため、テスト用データはダミーを使用する。
- 日付（契約開始日・終了日・更新期限など）の扱いはタイムゾーンと書式を統一する。
