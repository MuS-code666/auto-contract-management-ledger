# ローカル契約書管理 セットアップ（初回のみ。依存パッケージのダウンロードにインターネット接続が必要）
param([switch]$Dev)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "== ローカル契約書管理 セットアップ ==" -ForegroundColor Cyan

# Python を探す（3.12 推奨。3.11〜3.13 を許可）
$py = $null
foreach ($v in @("3.12", "3.13", "3.11")) {
    try { & py "-$v" -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $py = @("py", "-$v"); break } } catch {}
}
if (-not $py) {
    try { & python -c "import sys; assert (3,11) <= sys.version_info[:2] <= (3,13)" 2>$null; if ($LASTEXITCODE -eq 0) { $py = @("python") } } catch {}
}
if (-not $py) {
    Write-Host "Python 3.11〜3.13（64bit）が見つかりません。" -ForegroundColor Red
    Write-Host "https://www.python.org/ から Python 3.12 をインストールし（「Add python.exe to PATH」にチェック）、再実行してください。"
    exit 1
}
$exe = $py[0]; $pyArgs = @(); if ($py.Count -gt 1) { $pyArgs = $py[1..($py.Count - 1)] }
& $exe @pyArgs -c "import struct,sys; print('Python', sys.version.split()[0], struct.calcsize('P')*8, 'bit')"
& $exe @pyArgs -c "import struct; assert struct.calcsize('P') == 8" 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "64bit版の Python が必要です。" -ForegroundColor Red; exit 1 }

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "仮想環境（.venv）を作成しています…"
    & $exe @pyArgs -m venv .venv
}
$vpy = Join-Path $root ".venv\Scripts\python.exe"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
Write-Host "依存パッケージを導入しています（数分かかります）…"
& $vpy -m pip install --upgrade pip | Out-Null
$req = "requirements.txt"; if ($Dev) { $req = "requirements-dev.txt" }
& $vpy -m pip install -r $req
if ($LASTEXITCODE -ne 0) { Write-Host "パッケージの導入に失敗しました。インターネット接続・プロキシ設定を確認して再実行してください。" -ForegroundColor Red; exit 1 }

Write-Host "動作確認をしています…"
& $vpy -c "import fastapi, uvicorn, pypdfium2, openpyxl, rapidocr, onnxruntime; from app.ocr import RapidOcr; r = RapidOcr().check(); print('OCR:', r['message'])"
if ($LASTEXITCODE -ne 0) { Write-Host "動作確認に失敗しました。" -ForegroundColor Red; exit 1 }

if ($Dev) {
    & $vpy scripts\make_samples.py
    & $vpy scripts\make_holdout.py
}

Write-Host ""
Write-Host "セットアップが完了しました。" -ForegroundColor Green
Write-Host "  起動:            start.bat"
Write-Host "  ローカルLLM（任意）: setup_llm.bat で導入 → start_llm.bat で起動 → 設定画面で有効化"
Write-Host "以後の通常利用ではインターネット接続は不要です。"
