"""High-performance async EVM RPC client for multi-wallet operations."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
from eth_utils.address import to_checksum_address


@dataclass
class GasFees:
    base_fee_per_gas: int
    max_priority_fee_per_gas: int
    max_fee_per_gas: int


@dataclass
class TransactionReceipt:
    transaction_hash: str
    status: int  # 1 for success, 0 for revert
    block_number: int
    gas_used: int
    effective_gas_price: int


class ChainClient:
    def __init__(self, rpc_url: str, fallback_rpc_url: str = "", timeout_seconds: float = 8.0) -> None:
        self.rpc_url = rpc_url
        self.fallback_rpc_url = fallback_rpc_url
        self.timeout_seconds = timeout_seconds
        self._client: httpx.AsyncClient | None = None
        self._chain_id: int | None = None

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=50, keepalive_expiry=60.0),
                http2=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def rpc_request(self, method: str, params: list[Any], use_fallback: bool = False) -> Any:
        url = self.fallback_rpc_url if (use_fallback and self.fallback_rpc_url) else self.rpc_url
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000) % 1000000,
            "method": method,
            "params": params,
        }
        client = await self.get_client()
        try:
            resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                err = data["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                raise RuntimeError(f"RPC Error [{method}]: {msg}")
            return data.get("result")
        except Exception as e:
            # Try fallback once if available
            if not use_fallback and self.fallback_rpc_url:
                return await self.rpc_request(method, params, use_fallback=True)
            raise e

    async def get_chain_id(self) -> int:
        if self._chain_id is not None:
            return self._chain_id
        res = await self.rpc_request("eth_chainId", [])
        self._chain_id = int(res, 16)
        return self._chain_id

    async def get_block_number(self) -> int:
        res = await self.rpc_request("eth_blockNumber", [])
        return int(res, 16)

    async def get_block_timestamp(self) -> int:
        res = await self.rpc_request("eth_getBlockByNumber", ["latest", False])
        if res and "timestamp" in res:
            return int(res["timestamp"], 16)
        return int(time.time())

    async def get_balance(self, address: str) -> int:
        res = await self.rpc_request("eth_getBalance", [to_checksum_address(address), "latest"])
        return int(res, 16)

    async def get_balances_batch(self, addresses: list[str]) -> dict[str, int]:
        tasks = [self.get_balance(addr) for addr in addresses]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        res_dict = {}
        for addr, r in zip(addresses, results):
            if isinstance(r, int):
                res_dict[addr] = r
            else:
                res_dict[addr] = 0
        return res_dict

    async def get_nonce(self, address: str) -> int:
        res = await self.rpc_request("eth_getTransactionCount", [to_checksum_address(address), "pending"])
        return int(res, 16)

    async def get_nonces_batch(self, addresses: list[str]) -> dict[str, int]:
        tasks = [self.get_nonce(addr) for addr in addresses]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        res_dict = {}
        for addr, r in zip(addresses, results):
            if isinstance(r, int):
                res_dict[addr] = r
            else:
                raise RuntimeError(f"Failed to fetch nonce for {addr}: {r}")
        return res_dict

    async def get_gas_fees(
        self,
        fee_multiplier: float = 1.25,
        priority_fee_gwei: float = 0.002,
    ) -> GasFees:
        # Query latest block and fee history
        block_task = self.rpc_request("eth_getBlockByNumber", ["latest", False])
        priority_task = self.rpc_request("eth_maxPriorityFeePerGas", [])

        block, priority_res = await asyncio.gather(block_task, priority_task, return_exceptions=True)

        base_fee = 20_000_000  # 0.02 Gwei fallback (standard Robinhood Chain base fee)
        if isinstance(block, dict) and "baseFeePerGas" in block and block["baseFeePerGas"]:
            base_fee = int(block["baseFeePerGas"], 16)

        priority_fee_wei = int(priority_fee_gwei * 1e9)
        # On Robinhood Chain, base fee is only ~0.02 Gwei.
        # If user has an old Ethereum-scale priority fee (> base_fee), clamp it so it doesn't inflate fees by 75x!
        if priority_fee_wei > base_fee:
            max_priority = max(100_000, int(base_fee * 0.1))
        else:
            max_priority = max(100_000, priority_fee_wei)

        if isinstance(priority_res, str):
            try:
                rpc_priority = int(priority_res, 16)
                if 0 < rpc_priority <= base_fee:
                    max_priority = max(max_priority, rpc_priority)
            except ValueError:
                pass

        adjusted_base_fee = int(base_fee * fee_multiplier)
        max_fee = adjusted_base_fee + max_priority

        return GasFees(
            base_fee_per_gas=base_fee,
            max_priority_fee_per_gas=max_priority,
            max_fee_per_gas=max_fee,
        )

    async def call(self, to: str, data: str) -> str:
        tx = {"to": to_checksum_address(to), "data": data}
        res = await self.rpc_request("eth_call", [tx, "latest"])
        return res

    async def broadcast_raw_transaction(self, raw_tx: bytes) -> str:
        hex_data = f"0x{raw_tx.hex()}"
        try:
            res = await self.rpc_request("eth_sendRawTransaction", [hex_data])
            return res
        except Exception as e:
            err_str = str(e).lower()
            if "already known" in err_str or "nonce too low" in err_str:
                # Still consider sent
                return f"broadcasted ({e})"
            raise e

    async def broadcast_batch(self, raw_txs: list[bytes]) -> list[str]:
        """Broadcast transactions from multiple wallets concurrently in parallel!"""
        tasks = [self.broadcast_raw_transaction(raw) for raw in raw_txs]
        return await asyncio.gather(*tasks)

    async def wait_for_receipt(
        self,
        tx_hash: str,
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.5,
    ) -> TransactionReceipt | None:
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            try:
                res = await self.rpc_request("eth_getTransactionReceipt", [tx_hash])
                if res and isinstance(res, dict) and res.get("blockNumber") is not None:
                    return TransactionReceipt(
                        transaction_hash=tx_hash,
                        status=int(res.get("status", "0x0"), 16),
                        block_number=int(res.get("blockNumber", "0x0"), 16),
                        gas_used=int(res.get("gasUsed", "0x0"), 16),
                        effective_gas_price=int(res.get("effectiveGasPrice", "0x0"), 16),
                    )
            except Exception:
                pass
            await asyncio.sleep(poll_interval)
        return None

