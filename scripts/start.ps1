# ローカル契約書管理を起動する（127.0.0.1 のみで待ち受け。インターネット接続は不要）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$vpy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    Write-Host "セットアップが済んでいません。先に setup.bat を実行してください。" -ForegroundColor Red
    exit 1
}
# テレメトリ・外部送信の無効化（念のため環境変数でも明示）
$env:FASTAPI_OTEL_AUTO_CONFIGURE = "false"
$env:OTEL_SDK_DISABLED = "true"
$env:DO_NOT_TRACK = "1"
$env:HF_HUB_OFFLINE = "1"
$env:PYTHONUTF8 = "1"
& $vpy -m app.main @args
