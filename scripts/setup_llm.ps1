# ローカルLLM（任意）の導入：llama.cpp（MIT）と Qwen3-4B-Instruct-2507 の GGUF（Apache-2.0）をダウンロードする。
# 初回のみインターネット接続が必要。導入後の推論は 127.0.0.1 上で完結する。
param(
    [string]$LlamaTag = "b12058",
    [string]$ModelUrl = "https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
    [string]$ModelSha256 = "3605803B982CB64AEAD44F6C1B2AE36E3ACDB41D8E46C8A94C6533BC4C67E597"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force tools\llama, models | Out-Null

if (-not (Test-Path tools\llama\llama-server.exe)) {
    $zip = "tools\llama-$LlamaTag.zip"
    $url = "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaTag/llama-$LlamaTag-bin-win-cpu-x64.zip"
    Write-Host "llama.cpp ($LlamaTag, CPU版) をダウンロードしています: $url"
    curl.exe -fSL -o $zip $url
    if ($LASTEXITCODE -ne 0) { Write-Host "ダウンロードに失敗しました。" -ForegroundColor Red; exit 1 }
    Write-Host ("SHA256: " + (Get-FileHash $zip -Algorithm SHA256).Hash)
    Expand-Archive -Force $zip tools\llama
    Remove-Item $zip
}

$model = "models\" + [IO.Path]::GetFileName(($ModelUrl -split "\?")[0])
if (-not (Test-Path $model)) {
    Write-Host "モデル（約2.5GB）をダウンロードしています: $ModelUrl"
    curl.exe -fSL -o "$model.part" $ModelUrl
    if ($LASTEXITCODE -ne 0) { Write-Host "ダウンロードに失敗しました。" -ForegroundColor Red; exit 1 }
    Move-Item -Force "$model.part" $model
}
$h = (Get-FileHash $model -Algorithm SHA256).Hash
if ($ModelSha256 -and $h -ne $ModelSha256) {
    Write-Host "モデルのハッシュが一致しません（$h）。ファイルを削除して再実行してください。" -ForegroundColor Red
    exit 1
}
Write-Host "モデル SHA256 一致: $h" -ForegroundColor Green
Write-Host "導入しました。start_llm.bat でLLMサーバーを起動し、設定画面で「ローカルLLMサーバー」を選んで動作確認してください。"
