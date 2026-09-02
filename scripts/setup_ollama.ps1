# scripts/setup_ollama.ps1
# Įdiegia Ollama (jei reikia) ir parsisiunčia modelius, kurių reikia projektui.
# Naudojimas: powershell -ExecutionPolicy Bypass -File scripts\setup_ollama.ps1

param(
    [string]$Model = "qwen2.5-coder:7b-instruct",
    [string]$EmbedModel = "nomic-embed-text"
)

$ErrorActionPreference = "Stop"

function Get-OllamaExe {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $local = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $local) { return $local }
    return $null
}

Write-Host "==> Tikrinama Ollama..." -ForegroundColor Cyan
$ollama = Get-OllamaExe

if (-not $ollama) {
    Write-Host "Ollama nerasta. Bandoma įdiegti per winget..." -ForegroundColor Yellow
    winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
    $ollama = Get-OllamaExe
    if (-not $ollama) {
        Write-Host "Nepavyko rasti ollama.exe. Įdiekite rankiniu būdu: https://ollama.com/download" -ForegroundColor Red
        exit 1
    }
}

Write-Host "Naudojama: $ollama" -ForegroundColor Green
& $ollama --version

Write-Host "==> Parsiunčiamas $Model ..." -ForegroundColor Cyan
& $ollama pull $Model

Write-Host "==> Parsiunčiamas $EmbedModel ..." -ForegroundColor Cyan
& $ollama pull $EmbedModel

Write-Host "==> Modeliai:" -ForegroundColor Cyan
& $ollama list

Write-Host "==> OK. Galima paleisti: python scripts/check_ollama.py" -ForegroundColor Green
