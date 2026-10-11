#!/bin/bash
# ==============================================================================
# OpenSea Multi-Wallet Mint Bot - Startup Script (Linux / Termux / Ubuntu / Windows)
# ==============================================================================

# Activate virtualenv if present
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
elif [ -f ".venv/Scripts/activate" ]; then
    source .venv/Scripts/activate
fi

# Run the Telegram Bot using the best python available
if [ -f ".venv/bin/python" ]; then
    .venv/bin/python bot.py
elif [ -f ".venv/Scripts/python.exe" ]; then
    .venv/Scripts/python.exe bot.py
elif command -v python3 &>/dev/null; then
    python3 bot.py
else
    python bot.py
fi
