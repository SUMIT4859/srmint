"""Multi-wallet management and local signing for EVM minting."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from eth_utils.address import is_address, to_checksum_address


@dataclass
class WalletAccount:
    private_key: str
    label: str = ""
    default_quantity: int = 1
    _account: LocalAccount = field(init=False, repr=False)

    def __post_init__(self) -> None:
        key = self.private_key.strip()
        if key.startswith("0x"):
            key = key[2:]
        if len(key) != 64:
            raise ValueError(f"Invalid private key length: expected 64 hex characters, got {len(key)}")
        try:
            key_bytes = bytes.fromhex(key)
        except ValueError as e:
            raise ValueError("Private key must be valid hex characters") from e
        self._account = Account.from_key(key_bytes)
        if not self.label:
            short_addr = f"{self.address[:6]}...{self.address[-4:]}"
            self.label = f"Wallet {short_addr}"

    @property
    def address(self) -> str:
        return to_checksum_address(self._account.address)

    @property
    def short_address(self) -> str:
        addr = self.address
        return f"{addr[:6]}...{addr[-4:]}"

    def sign_personal_message(self, message: bytes) -> str:
        """Sign EIP-191 personal message (used for OpenSea SIWE authentication)."""
        signed = self._account.sign_message(encode_defunct(primitive=message))
        return f"0x{signed.signature.hex()}"

    def sign_transaction(self, tx_dict: dict[str, Any]) -> tuple[bytes, str]:
        """Sign EIP-1559 or legacy transaction dict. Returns (raw_bytes, tx_hash_hex)."""
        tx = dict(tx_dict)
        if "to" in tx and isinstance(tx["to"], str):
            tx["to"] = to_checksum_address(tx["to"])
        signed = self._account.sign_transaction(tx)
        return bytes(signed.raw_transaction), f"0x{signed.hash.hex()}"


class WalletManager:
    def __init__(self, filepath: str = "wallets.json") -> None:
        self.filepath = Path(filepath)
        self.wallets: list[WalletAccount] = []
        self.load()

    def load(self) -> None:
        self.wallets.clear()
        if not self.filepath.exists():
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            wallets_data = data.get("wallets", [])
            for item in wallets_data:
                pk = item.get("private_key", "")
                lbl = item.get("label", "")
                qty = item.get("quantity", 1)
                if pk:
                    try:
                        self.wallets.append(WalletAccount(private_key=pk, label=lbl, default_quantity=qty))
                    except Exception as e:
                        print(f"Error loading wallet {lbl}: {e}")
        except Exception as e:
            print(f"Failed to read {self.filepath}: {e}")

    def save(self) -> None:
        data = {
            "version": 1,
            "wallets": [
                {
                    "private_key": f"0x{w.private_key[2:] if w.private_key.startswith(('0x', '0X')) else w.private_key}",
                    "label": w.label,
                    "quantity": w.default_quantity,
                    "address": w.address,
                }
                for w in self.wallets
            ],
        }
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def add_wallet(self, private_key: str, label: str = "", quantity: int = 1) -> WalletAccount:
        wallet = WalletAccount(private_key=private_key, label=label, default_quantity=quantity)
        # Avoid duplicate private keys
        for w in self.wallets:
            if w.address.lower() == wallet.address.lower():
                raise ValueError(f"Wallet with address {wallet.address} already exists!")
        self.wallets.append(wallet)
        self.save()
        return wallet

    def remove_wallet(self, index: int) -> WalletAccount:
        if 0 <= index < len(self.wallets):
            removed = self.wallets.pop(index)
            self.save()
            return removed
        raise IndexError(f"Wallet index {index} out of range (0-{len(self.wallets)-1})")

    def count(self) -> int:
        return len(self.wallets)

    def get_all(self) -> list[WalletAccount]:
        return list(self.wallets)
