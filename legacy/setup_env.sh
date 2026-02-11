#!/bin/bash
# Fast environment setup using uv (much faster than pip)

set -e

echo "🚀 Setting up environment with uv (fast mode)..."

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "📦 Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Add uv to PATH
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

echo "✅ uv is available"

# Create venv if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "📁 Creating virtual environment..."
    uv venv .venv
fi

echo "🔧 Activating virtual environment..."
source .venv/bin/activate

# Install dependencies for all components
echo "📥 Installing dependencies (this is MUCH faster with uv)..."

echo "  → Installing signal-bot dependencies..."
uv pip install -r signal-bot/requirements.txt

echo "  → Installing signal-ingest dependencies..."
uv pip install -r signal-ingest/requirements.txt

echo "  → Installing test dependencies..."
uv pip install -r test/requirements.txt

echo ""
echo "✅ Environment setup complete!"
echo ""
echo "To activate the environment, run:"
echo "  source .venv/bin/activate"
echo ""
echo "To run tests:"
echo "  pytest test/"
