# Fast environment setup using uv (much faster than pip)
# PowerShell version for Windows

$ErrorActionPreference = "Stop"

Write-Host "🚀 Setting up environment with uv (fast mode)..." -ForegroundColor Cyan

# Check if uv is installed
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "📦 Installing uv..." -ForegroundColor Yellow
    irm https://astral.sh/uv/install.ps1 | iex
    $env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
}

Write-Host "✅ uv is available" -ForegroundColor Green

# Create venv if it doesn't exist
if (-not (Test-Path ".venv")) {
    Write-Host "📁 Creating virtual environment..." -ForegroundColor Yellow
    uv venv .venv
}

Write-Host "🔧 Activating virtual environment..." -ForegroundColor Yellow
& .\.venv\Scripts\Activate.ps1

# Install dependencies for all components
Write-Host "📥 Installing dependencies (this is MUCH faster with uv)..." -ForegroundColor Cyan

Write-Host "  → Installing signal-bot dependencies..." -ForegroundColor Gray
uv pip install -r signal-bot/requirements.txt

Write-Host "  → Installing signal-ingest dependencies..." -ForegroundColor Gray
uv pip install -r signal-ingest/requirements.txt

Write-Host "  → Installing test dependencies..." -ForegroundColor Gray
uv pip install -r test/requirements.txt

Write-Host ""
Write-Host "✅ Environment setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "To activate the environment, run:" -ForegroundColor Yellow
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host ""
Write-Host "To run tests:" -ForegroundColor Yellow
Write-Host "  pytest test/"
