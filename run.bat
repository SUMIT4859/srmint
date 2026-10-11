@echo off
echo Starting OpenSea Multi-Wallet Mint Bot...

if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

python bot.py
pause

