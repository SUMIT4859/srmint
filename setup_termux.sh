#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# OpenSea Multi-Wallet Mint Bot - Termux Android Auto Installer
# ==============================================================================

set -e

echo "🚀 Starting OpenSea Multi-Wallet Mint Bot setup for Termux..."

# 1. Update Termux packages
echo "📦 Updating Termux package lists..."
pkg update -y

# 2. Install Python, Git, and build essentials for fast cryptography
echo "📦 Installing Python, Git, clang, and dependencies..."
pkg install -y python git clang libffi openssl rust

# 3. Upgrade pip
echo "⬆️ Upgrading pip..."
pip install --upgrade pip

# 4. Install dependencies
echo "📥 Installing bot Python dependencies..."
pip install -r requirements.txt

# 5. Create default configuration if missing
if [ ! -f "config.json" ] && [ ! -f ".env" ]; then
    echo "⚙️ Creating default config.json..."
    cp config.example.json config.json
    echo "💡 Note: Edit config.json or .env with your TELEGRAM_BOT_TOKEN!"
fi

if [ ! -f "wallets.json" ]; then
    echo "💼 Creating empty wallets.json..."
    echo '{"version": 1, "wallets": []}' > wallets.json
fi

echo "=============================================================================="
echo "✅ Setup Complete!"
echo "To start your bot anytime in Termux, run:"
echo "   bash run.sh"
echo "=============================================================================="

