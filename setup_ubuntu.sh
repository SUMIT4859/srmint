#!/bin/bash
# ==============================================================================
# OpenSea Multi-Wallet Mint Bot - Ubuntu / PRoot Ubuntu Installer
# ==============================================================================

set -e

echo "🚀 Starting OpenSea Multi-Wallet Mint Bot setup on Ubuntu..."

sudo apt update -y
sudo apt install -y python3 python3-pip python3-venv git curl

# Create virtualenv
if [ ! -d ".venv" ]; then
    echo "📦 Creating Python virtual environment (.venv)..."
    python3 -m venv .venv
fi

source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Create default configuration if missing
if [ ! -f "config.json" ] && [ ! -f ".env" ]; then
    echo "⚙️ Creating default config.json..."
    cp config.example.json config.json
fi

if [ ! -f "wallets.json" ]; then
    echo '{"version": 1, "wallets": []}' > wallets.json
fi

echo "=============================================================================="
echo "✅ Setup Complete!"
echo "To start your bot anytime, run:"
echo "   bash run.sh"
echo "=============================================================================="

