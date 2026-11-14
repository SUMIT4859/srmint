"""Configuration manager for OpenSea Multi-Wallet Bot."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


@dataclass
class AppConfig:
    telegram_bot_token: str = ""
    telegram_admin_id: int = 0
    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    fallback_rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    robinhood_sequencer_url: str = "https://sequencer.mainnet.chain.robinhood.com"
    ink_rpc_url: str = "https://rpc-gel.inkonchain.com"
    ink_fallback_rpc_url: str = "https://rpc-qnd.inkonchain.com"
    eth_rpc_url: str = "https://ethereum-rpc.publicnode.com"
    eth_fallback_rpc_url: str = "https://1rpc.io/eth"
    soneium_rpc_url: str = "https://rpc.soneium.org"
    soneium_fallback_rpc_url: str = "https://rpc.nodeflare.app/soneium/public"
    elysium_rpc_url: str = "https://rpc.elysium.kinetiq.xyz"
    elysium_fallback_rpc_url: str = "https://rpc-live.elysp.com"
    gas_limit: int = 250000
    fee_multiplier: float = 1.25
    scheduled_fee_multiplier: float = 2.0
    max_priority_fee_gwei: float = 0.002
    broadcast_lead_ms: int = 80
    wallets_file: str = "wallets.json"
    opensea_timeout_ms: int = 10000
    max_retry_attempts: int = 30
    retry_interval_ms: int = 250
    session_heartbeat_interval_seconds: int = 25
    opensea_api_key: str = ""
    extra_broadcast_rpcs: list[str] = field(default_factory=list)

    def get_rpc_for_chain(self, chain_id: int) -> tuple[str, str]:
        """Return (primary_rpc, fallback_rpc) for the specified chain ID."""
        if chain_id == 99901:
            return (self.elysium_rpc_url, self.elysium_fallback_rpc_url)
        if chain_id == 1868:
            return (self.soneium_rpc_url, self.soneium_fallback_rpc_url)
        if chain_id == 1:
            return (self.eth_rpc_url, self.eth_fallback_rpc_url)
        if chain_id == 57073:
            return (self.ink_rpc_url, self.ink_fallback_rpc_url)
        if chain_id == 4663:
            return (self.rpc_url, self.fallback_rpc_url)
        from .constants import get_chain_info
        info = get_chain_info(chain_id)
        return (info.get("default_rpc", ""), info.get("fallback_rpc", ""))

    @classmethod
    def load(cls, config_path: str = "config.json") -> AppConfig:
        cfg = cls()

        # Load from config.json if exists
        p = Path(config_path)
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in data.items():
                        if hasattr(cfg, k):
                            setattr(cfg, k, v)
            except Exception as e:
                print(f"Warning: Failed to parse {config_path}: {e}")

        # Override with environment variables if present
        cfg.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", cfg.telegram_bot_token)
        admin_id_str = os.getenv("TELEGRAM_ADMIN_ID")
        if admin_id_str and admin_id_str.isdigit():
            cfg.telegram_admin_id = int(admin_id_str)

        cfg.rpc_url = os.getenv("RPC_URL", cfg.rpc_url)
        cfg.fallback_rpc_url = os.getenv("FALLBACK_RPC_URL", cfg.fallback_rpc_url)
        cfg.robinhood_sequencer_url = os.getenv("ROBINHOOD_SEQUENCER_URL", cfg.robinhood_sequencer_url)
        cfg.ink_rpc_url = os.getenv("INK_RPC_URL", cfg.ink_rpc_url)
        cfg.ink_fallback_rpc_url = os.getenv("INK_FALLBACK_RPC_URL", cfg.ink_fallback_rpc_url)
        cfg.eth_rpc_url = os.getenv("ETH_RPC_URL", cfg.eth_rpc_url)
        cfg.eth_fallback_rpc_url = os.getenv("ETH_FALLBACK_RPC_URL", cfg.eth_fallback_rpc_url)
        cfg.soneium_rpc_url = os.getenv("SONEIUM_RPC_URL", cfg.soneium_rpc_url)
        cfg.soneium_fallback_rpc_url = os.getenv("SONEIUM_FALLBACK_RPC_URL", cfg.soneium_fallback_rpc_url)
        cfg.elysium_rpc_url = os.getenv("ELYSIUM_RPC_URL", cfg.elysium_rpc_url)
        cfg.elysium_fallback_rpc_url = os.getenv("ELYSIUM_FALLBACK_RPC_URL", cfg.elysium_fallback_rpc_url)

        if os.getenv("GAS_LIMIT"):
            try:
                cfg.gas_limit = int(os.getenv("GAS_LIMIT"))
            except ValueError:
                pass

        if os.getenv("BROADCAST_LEAD_MS"):
            try:
                cfg.broadcast_lead_ms = int(os.getenv("BROADCAST_LEAD_MS"))
            except ValueError:
                pass

        if os.getenv("WALLETS_FILE"):
            cfg.wallets_file = os.getenv("WALLETS_FILE")

        if os.getenv("OPENSEA_API_KEY"):
            cfg.opensea_api_key = os.getenv("OPENSEA_API_KEY")
        elif os.getenv("opensea_api_key"):
            cfg.opensea_api_key = os.getenv("opensea_api_key")

        if os.getenv("EXTRA_BROADCAST_RPCS"):
            cfg.extra_broadcast_rpcs = [u.strip() for u in os.getenv("EXTRA_BROADCAST_RPCS", "").split(",") if u.strip()]
        elif os.getenv("EXTRA_RPC_URLS"):
            cfg.extra_broadcast_rpcs = [u.strip() for u in os.getenv("EXTRA_RPC_URLS", "").split(",") if u.strip()]

        return cfg

    def save(self, config_path: str = "config.json") -> None:
        data = {
            "telegram_bot_token": self.telegram_bot_token,
            "telegram_admin_id": self.telegram_admin_id,
            "rpc_url": self.rpc_url,
            "fallback_rpc_url": self.fallback_rpc_url,
            "ink_rpc_url": self.ink_rpc_url,
            "ink_fallback_rpc_url": self.ink_fallback_rpc_url,
            "eth_rpc_url": self.eth_rpc_url,
            "eth_fallback_rpc_url": self.eth_fallback_rpc_url,
            "soneium_rpc_url": self.soneium_rpc_url,
            "soneium_fallback_rpc_url": self.soneium_fallback_rpc_url,
            "elysium_rpc_url": self.elysium_rpc_url,
            "elysium_fallback_rpc_url": self.elysium_fallback_rpc_url,
            "gas_limit": self.gas_limit,
            "fee_multiplier": self.fee_multiplier,
            "scheduled_fee_multiplier": self.scheduled_fee_multiplier,
            "max_priority_fee_gwei": self.max_priority_fee_gwei,
            "broadcast_lead_ms": self.broadcast_lead_ms,
            "wallets_file": self.wallets_file,
            "opensea_timeout_ms": self.opensea_timeout_ms,
            "max_retry_attempts": self.max_retry_attempts,
            "retry_interval_ms": self.retry_interval_ms,
            "session_heartbeat_interval_seconds": self.session_heartbeat_interval_seconds,
            "opensea_api_key": self.opensea_api_key,
            "extra_broadcast_rpcs": self.extra_broadcast_rpcs,
        }
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

