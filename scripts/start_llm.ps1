# ローカルLLMサーバー（llama.cpp の llama-server）を 127.0.0.1:8081 で起動する。
# -hf 等のモデル自動ダウンロード機能は使わない。Web UI も無効。
param([int]$Port = 8081, [int]$Context = 16384, [int]$Threads = 0, [string]$Model = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$exe = "tools\llama\llama-server.exe"
if (-not (Test-Path $exe)) { Write-Host "llama-server が見つかりません。先に setup_llm.bat を実行してください。" -ForegroundColor Red; exit 1 }
if (-not $Model) { $Model = (Get-ChildItem models -Filter *.gguf | Select-Object -First 1).FullName }
if (-not $Model) { Write-Host "models フォルダに .gguf モデルがありません。" -ForegroundColor Red; exit 1 }
if ($Threads -le 0) { $Threads = [Math]::Max(1, [Environment]::ProcessorCount - 1) }
Write-Host "LLMサーバーを起動します: $Model（127.0.0.1:$Port, threads=$Threads）"
Write-Host "このウィンドウを閉じるとLLMサーバーは停止します。"
& $exe -m $Model --host 127.0.0.1 --port $Port -c $Context --threads $Threads --no-webui
