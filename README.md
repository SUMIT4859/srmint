# 🌊 OpenSea SeaDrop Multi-Wallet High-Speed Telegram Mint Bot

An ultra-fast, concurrent multi-wallet minting bot for **OpenSea SeaDrop (ERC-721)** collections, supporting **Robinhood (4663)**, **Ink (57073)**, **Ethereum Mainnet (1)**, **Soneium (1868)**, and **Elysium (99901)**, controlled entirely from your private **Telegram Bot** and optimized for **Android phones via Termux**, **Ubuntu (PRoot / VPS)**, Linux, and Windows.

---

## ⚡ Why This Bot is Superior to `osnm-z`

| Feature | Original `osnm-z` (Python) | This Bot (`opensea-mint-bot`) |
| :--- | :--- | :--- |
| **Multi-Wallet Support** | ❌ Single wallet only | ✅ **True Multi-Wallet Concurrent Minting** (parallel broadcast across N wallets at T-0) |
| **Control Interface** | 💻 CLI / Terminal only | 📱 **Private Telegram Bot** (Control, select stages, adjust quantities & fire from your phone) |
| **Countdown Drop Bug** | ⚠️ Fails after waiting (SIWE session expires, auth task cancelled at T-2, retry capped at 15) | ✅ **Fixed!** Active keep-alive heartbeat pings every 25s, pre-signs at T-10, adaptive retry for 30-60s |
| **Public Drop Speed** | Slow GraphQL dependency | ⚡ **Direct On-Chain Calldata (`0x161ac21f`)** pre-signed ahead of time; fires at exact T-0 with zero API lag |
| **Phone / Termux Ready** | ❌ Complex setup | ✅ **1-Click Termux installer** (`bash setup_termux.sh`) |
| **Pre-Signing & Lead Offset**| ❌ No pre-signing | ✅ **Pre-signs raw transactions at T-10s** and broadcasts with customizable millisecond lead offset |

---

## 📱 Installation on Android Phone (Termux)

### Step 1: Install Termux
Install Termux from [F-Droid](https://f-droid.org/en/packages/com.termux/) (avoid Google Play Store version as it is outdated).

### Step 2: Open Termux and Run Setup
Copy and paste this in Termux:
```bash
# Update and install git
pkg update -y && pkg install -y git

# Clone or navigate to the bot directory
cd opensea-mint-bot

# Run the 1-click Termux installer
bash setup_termux.sh
```

### Step 3: Configure Bot Token
Create a bot via [@BotFather](https://t.me/BotFather) on Telegram and copy your token:
```bash
nano config.json
```
Fill in your `telegram_bot_token`:
```json
{
  "telegram_bot_token": "YOUR_BOT_TOKEN_FROM_BOTFATHER",
  "telegram_admin_id": 0,
  "rpc_url": "https://mainnet.base.org",
  "gas_limit": 300000,
  "broadcast_lead_ms": 80
}
```
*(Tip: Press `Ctrl+O` then `Enter` to save, and `Ctrl+X` to exit nano.)*

### Step 4: Run the Bot
```bash
bash run.sh
```
Open Telegram, message your bot `/start`, and you are ready to mint!

---

## 🐧 Installation on Ubuntu / Linux / VPS / PRoot

```bash
# Run the 1-click Ubuntu setup script
bash setup_ubuntu.sh

# Edit your configuration
cp config.example.json config.json
nano config.json

# Start the bot
bash run.sh
```

---

## 🪟 Installation on Windows

```powershell
# Create virtual environment and install dependencies
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# Copy config and edit
copy config.example.json config.json
notepad config.json

# Run the bot
python bot.py
```

---

## 🤖 Telegram Bot Commands & Usage

| Command | Description |
| :--- | :--- |
| `/start` or `/help` | Open main dashboard with interactive inline buttons |
| `/mint <slug_or_url>` | Inspect collection, view live/upcoming stages, select quantity, and fire multi-wallet mint |
| `/wallets` | View all configured wallets, balances, and short addresses |
| `/addwallet <private_key> [label]` | Add a new wallet securely *(the bot automatically deletes your message containing the private key for security)* |
| `/delwallet <number>` | Remove a wallet by its index |
| `/balance` | Check native balances across all wallets simultaneously |
| `/doctor` | Test RPC latency, block time sync, OpenSea API connectivity, and wallet readiness |
| `/settings` | View current gas limit, RPC endpoint, fee multipliers, and broadcast lead time |
| `/mints` or `/active` | View all currently scheduled or executing drops with status and selective cancel buttons |
| `/cancel [all]` | Cancel an active scheduled mint (or `/cancel all` to stop all running drops) |

---

## 🚀 How the Speed Architecture Works

1. **Simultaneous Multi-Drop Minting & Card Isolation**:
   Schedule and fire multiple drops occurring at the exact same second or overlapping times without waiting for previous drops to finish. Each drop card maintains an independent session, and `NonceTracker` automatically assigns sequential, collision-free nonces across concurrent drops sharing wallets.
2. **Direct On-Chain SeaDrop Execution**:
   For Public mint stages, the bot encodes the canonical SeaDrop selector `0x161ac21f` (`mintPublic`) directly on-chain. It does **not** rely on OpenSea's GraphQL server at mint time, completely eliminating server delays.
3. **T-10s Pre-Signing**:
   At 10 seconds before the drop opens, the bot fetches nonces and dynamic gas fees across all wallets in parallel and signs the raw transactions locally.
4. **Simultaneous Parallel Broadcast**:
   At T-0 (with customizable lead time, e.g. 80ms before open to compensate for network latency), the bot blasts all pre-signed transactions to the RPC concurrently using `asyncio.gather`.
5. **Heartbeat Session Keeper**:
   Fixes the timeout issue where watching a countdown caused SIWE sessions or HTTP connections to drop. Background heartbeats keep sessions hot every 25 seconds per collection.

