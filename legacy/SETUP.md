# Fast Environment Setup

This project uses **uv** for ultra-fast dependency installation (10-100x faster than pip).

## Quick Start

### Linux/WSL/macOS
```bash
chmod +x setup_env.sh
./setup_env.sh
source .venv/bin/activate
```

### Windows PowerShell
```powershell
.\setup_env.ps1
```

## What does this do?

1. **Installs uv** (if not already installed) - A blazingly fast Python package installer
2. **Creates virtual environment** in `.venv/`
3. **Installs all dependencies** from:
   - `signal-bot/requirements.txt`
   - `signal-ingest/requirements.txt`
   - `test/requirements.txt`

## Speed Comparison

Traditional pip installation: **2-5 minutes**  
With uv: **10-30 seconds** ⚡

## Manual Setup (if you prefer)

If you want to use uv manually:

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create venv
uv venv

# Install dependencies
source .venv/bin/activate  # or .\.venv\Scripts\Activate.ps1 on Windows
uv pip install -r signal-bot/requirements.txt
uv pip install -r signal-ingest/requirements.txt
uv pip install -r test/requirements.txt
```

## Traditional Method (slower)

If you prefer using standard pip:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r signal-bot/requirements.txt
pip install -r signal-ingest/requirements.txt
pip install -r test/requirements.txt
```

## Running Tests

After setup:
```bash
pytest test/
```

## About uv

uv is a modern Python package installer written in Rust by Astral (creators of Ruff). It's:
- 10-100x faster than pip
- Drop-in replacement (same commands)
- More reliable dependency resolution
- Better caching

Learn more: https://github.com/astral-sh/uv
