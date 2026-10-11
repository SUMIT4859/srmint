"""High-speed multi-wallet minting engine with pre-signing and parallel broadcast."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from eth_utils.address import to_checksum_address

from .chain import ChainClient, GasFees
from .client import CollectionInfo, DropStage, MintAction, OpenSeaClient
from .config import AppConfig
from .constants import (
    GET_PUBLIC_DROP_SELECTOR,
    OPENSEA_SEADROP_ADDRESS,
    get_chain_info,
    get_tx_explorer_url,
)
from .seadrop import encode_public_mint
from .wallet import WalletAccount


@dataclass
class WalletMintResult:
    wallet: WalletAccount
    quantity: int
    tx_hash: str = ""
    status: str = "PENDING"  # SUCCESS, REVERTED, PENDING, FAILED, CANCELLED_PRICE_CHANGED
    error_message: str = ""
    gas_used: int = 0
    explorer_url: str = ""
    broadcast_time_ms: int = 0
    confirm_time_seconds: float = 0.0
    total_time_seconds: float = 0.0


class MintEngine:
    def __init__(self, config: AppConfig, chain: ChainClient, opensea: OpenSeaClient) -> None:
        self.config = config
        self.chain = chain
        self.opensea = opensea
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    async def get_onchain_seadrop_price(self, chain: ChainClient, contract_address: str) -> int | None:
        """Query SeaDrop smart contract on-chain to read the official public mint price."""
        from eth_abi import decode
        try:
            checksummed = to_checksum_address(contract_address)
            addr_padded = bytes.fromhex(checksummed[2:].lower()).rjust(32, b"\x00")
            call_data = "0x" + (GET_PUBLIC_DROP_SELECTOR + addr_padded).hex()
            res = await chain.call(OPENSEA_SEADROP_ADDRESS, call_data)
            if res and res != "0x":
                raw_bytes = bytes.fromhex(res[2:])
                decoded = decode(["uint80", "uint48", "uint48", "uint16", "uint16", "bool"], raw_bytes)
                return int(decoded[0])
        except Exception:
            pass
        return None

    async def execute_mint(
        self,
        collection: CollectionInfo,
        stage: DropStage,
        wallets: list[WalletAccount],
        quantity_per_wallet: int = 1,
        expected_price_wei: int | None = None,
        on_progress: Callable[[str], asyncio.Future[Any] | Any] | None = None,
    ) -> list[WalletMintResult]:
        self._cancel_requested = False
        results = [WalletMintResult(wallet=w, quantity=quantity_per_wallet) for w in wallets]

        async def notify(msg: str) -> None:
            if on_progress:
                res = on_progress(msg)
                if asyncio.iscoroutine(res):
                    await res

        active_chain = self.chain
        should_close_active_chain = False
        try:
            configured_chain_id = await self.chain.get_chain_id()
        except Exception:
            configured_chain_id = 0

        chain_id = collection.chain_id
        if configured_chain_id != chain_id:
            chain_info = get_chain_info(chain_id)
            default_rpc = chain_info.get("default_rpc")
            if default_rpc:
                await notify(
                    f"🔄 Auto-routing to <b>{chain_info['name']}</b> (Chain ID: {chain_id})\n"
                    f"RPC: <code>{default_rpc}</code>"
                )
                active_chain = ChainClient(default_rpc, timeout_seconds=self.chain.timeout_seconds)
                should_close_active_chain = True

        is_public = stage.is_public
        now = int(time.time())
        is_scheduled = stage.start_timestamp > now

        await notify(
            f"⚡ Initializing {'Public' if is_public else 'Allowlist'} Mint for {len(wallets)} wallet(s)...\n"
            f"Collection: <b>{collection.name}</b>\n"
            f"Stage: <b>{stage.label}</b> | Price: <b>{stage.price_unit} {stage.price_symbol}</b>"
        )

        # 1. OpenSea session warmup & authentication (for private stages)
        if not is_public:
            await notify("🔐 Authenticating wallets on OpenSea SIWE...")
            await self.opensea.authenticate_all_wallets(wallets, chain_id, collection.slug)

        # Start background heartbeat if scheduled wait is required
        if is_scheduled:
            self.opensea.start_heartbeat(
                collection.slug,
                wallets,
                interval_seconds=self.config.session_heartbeat_interval_seconds,
            )

        try:
            # 2. Check wallet balances
            await notify("💰 Checking balances across all wallets...")
            addresses = [w.address for w in wallets]
            balances = await active_chain.get_balances_batch(addresses)

            # Query actual network gas fees to avoid inflated estimates
            try:
                current_gas = await active_chain.get_gas_fees(
                    fee_multiplier=self.config.fee_multiplier,
                    priority_fee_gwei=self.config.max_priority_fee_gwei,
                )
                max_fee = current_gas.max_fee_per_gas
            except Exception:
                max_fee = int(self.config.max_priority_fee_gwei * 1e9) + 500_000_000

            mint_cost = stage.price_wei * quantity_per_wallet
            estimated_gas_cost = min(self.config.gas_limit * max_fee, int(0.0005 * 1e18))
            total_required = mint_cost + estimated_gas_cost

            for res in results:
                bal = balances.get(res.wallet.address, 0)
                # For free mints, only notify if the wallet has strictly 0 balance (cannot pay any gas)
                if mint_cost == 0:
                    if bal == 0:
                        await notify(
                            f"💡 Note: {res.wallet.short_address} has 0.0000 ETH.\n"
                            f"A few cents (~$0.02) of native token is needed for network transaction gas."
                        )
                elif bal < total_required:
                    bal_eth = bal / 1e18
                    cost_eth = total_required / 1e18
                    await notify(
                        f"⚠️ Notice: {res.wallet.short_address} balance is {bal_eth:.5f} ETH "
                        f"(mint total ~{cost_eth:.5f} ETH)."
                    )

            # 3. Scheduled countdown wait with live updates
            target_start_ts = stage.start_timestamp
            now = int(time.time())

            if target_start_ts > now:
                wait_seconds = target_start_ts - now
                await notify(f"⏳ Mint opens at {stage.start_time}.\nStarting countdown ({wait_seconds}s remaining)...")

                while True:
                    if self._cancel_requested:
                        await notify("❌ Minting was cancelled by user.")
                        return results

                    now_ts = time.time()
                    rem_sec = target_start_ts - now_ts
                    if rem_sec <= 10.0:  # T-10 seconds: begin pre-signing!
                        break

                    # Send update at milestones
                    if int(rem_sec) % 30 == 0 or rem_sec in (60, 30, 20, 15):
                        await notify(f"⏳ T-{int(rem_sec)}s until mint starts...")
                    await asyncio.sleep(min(1.0, rem_sec - 10.0))

            if self._cancel_requested:
                await notify("❌ Minting cancelled.")
                return results

            # 4. T-10 Preparation & Pre-Signing Phase
            await notify("🚀 T-10s: Pre-fetching nonces & pre-signing transactions for all wallets...")

            # Concurrently fetch nonces
            nonces = await active_chain.get_nonces_batch(addresses)

            # Fetch gas fees with boost multiplier
            fee_mult = (
                self.config.scheduled_fee_multiplier if is_scheduled else self.config.fee_multiplier
            )
            gas_fees = await active_chain.get_gas_fees(
                fee_multiplier=fee_mult,
                priority_fee_gwei=self.config.max_priority_fee_gwei,
            )

            # Prepare transactions
            prepared_tx_bytes: list[bytes] = []

            if is_public:
                # Direct on-chain SeaDrop mintPublic: zero API delay, instant 100% pre-signed!
                calldata_per_wallet = [
                    encode_public_mint(
                        nft_contract=collection.contract_address,
                        minter_recipient=w.address,
                        quantity=quantity_per_wallet,
                    )
                    for w in wallets
                ]

                for w, cdata in zip(wallets, calldata_per_wallet):
                    nonce = nonces[w.address]
                    val = stage.price_wei * quantity_per_wallet
                    tx_dict = {
                        "type": 2,
                        "chainId": chain_id,
                        "nonce": nonce,
                        "maxPriorityFeePerGas": gas_fees.max_priority_fee_per_gas,
                        "maxFeePerGas": gas_fees.max_fee_per_gas,
                        "gas": self.config.gas_limit,
                        "to": OPENSEA_SEADROP_ADDRESS,
                        "value": val,
                        "data": cdata,
                    }
                    raw_bytes, tx_hash = w.sign_transaction(tx_dict)
                    prepared_tx_bytes.append(raw_bytes)
                    for r in results:
                        if r.wallet.address == w.address:
                            r.tx_hash = tx_hash
                            r.explorer_url = get_tx_explorer_url(chain_id, tx_hash)

            else:
                # Private/Allowlist: fetch mint calldata
                await notify("📡 Requesting allowlist calldata from OpenSea...")
                action_tasks = [
                    self.opensea.get_mint_action_hot(
                        collection,
                        w,
                        quantity_per_wallet,
                        stage,
                        max_attempts=self.config.max_retry_attempts,
                        retry_interval_ms=self.config.retry_interval_ms,
                    )
                    for w in wallets
                ]
                actions = await asyncio.gather(*action_tasks, return_exceptions=True)

                for w, act in zip(wallets, actions):
                    if isinstance(act, Exception):
                        for r in results:
                            if r.wallet.address == w.address:
                                r.status = "FAILED"
                                r.error_message = str(act)
                        continue

                    nonce = nonces[w.address]
                    tx_dict = {
                        "type": 2,
                        "chainId": chain_id,
                        "nonce": nonce,
                        "maxPriorityFeePerGas": gas_fees.max_priority_fee_per_gas,
                        "maxFeePerGas": gas_fees.max_fee_per_gas,
                        "gas": self.config.gas_limit,
                        "to": to_checksum_address(act.target),
                        "value": act.value,
                        "data": act.calldata,
                    }
                    raw_bytes, tx_hash = w.sign_transaction(tx_dict)
                    prepared_tx_bytes.append(raw_bytes)
                    for r in results:
                        if r.wallet.address == w.address:
                            r.tx_hash = tx_hash
                            r.explorer_url = get_tx_explorer_url(chain_id, tx_hash)

            # 5. High-Precision Countdown to T-0 with Lead Offset Compensation
            if target_start_ts > time.time():
                lead_seconds = self.config.broadcast_lead_ms / 1000.0
                target_broadcast_time = target_start_ts - lead_seconds

                rem = target_broadcast_time - time.time()
                if rem > 0:
                    await asyncio.sleep(rem)

            # --- PRE-BROADCAST PRICE GUARD & STAGE INTEGRITY VERIFICATION ---
            target_price = expected_price_wei if expected_price_wei is not None else stage.price_wei
            price_changed = False
            new_price_wei = target_price
            new_price_desc = ""

            # Verification A: Fresh OpenSea Drop Metadata check
            try:
                fresh_coll = await self.opensea.fetch_collection(collection.slug, chain_client=active_chain)
                fresh_stage = next(
                    (s for s in fresh_coll.stages if s.uuid == stage.uuid or s.stage_index == stage.stage_index),
                    None,
                )
                if fresh_stage is None and fresh_coll.stages:
                    fresh_stage = fresh_coll.stages[0]

                if fresh_stage and fresh_stage.price_wei != target_price:
                    price_changed = True
                    new_price_wei = fresh_stage.price_wei
                    new_price_desc = f"{fresh_stage.price_unit} {fresh_stage.price_symbol} (~{fresh_stage.price_wei / 1e18:.6f} ETH)"
            except Exception:
                pass

            # Verification B: Direct On-Chain SeaDrop Smart Contract Check
            if not price_changed and stage.is_public:
                try:
                    onchain_price = await self.get_onchain_seadrop_price(active_chain, collection.contract_address)
                    if onchain_price is not None and onchain_price != target_price:
                        price_changed = True
                        new_price_wei = onchain_price
                        new_price_desc = f"{onchain_price / 1e18:.6f} ETH (On-Chain SeaDrop)"
                except Exception:
                    pass

            # IF PRICE CHANGED: CANCEL TRANSACTION IMMEDIATELY & NEVER BROADCAST!
            if price_changed:
                approved_desc = f"{target_price / 1e18:.6f} ETH" if target_price > 0 else "0 ETH (FREE)"
                alert_text = (
                    "🚨 <b>PRICE CHANGE DETECTED — MINT ABORTED!</b>\n\n"
                    "⚠️ <i>The collection developer modified the mint price!</i>\n"
                    f"• <b>Approved Price:</b> <code>{approved_desc}</code>\n"
                    f"• <b>New Detected Price:</b> <code>{new_price_desc}</code>\n\n"
                    "🛡️ <b>Price Guard Triggered:</b> Transactions were <b>NEVER broadcasted</b>. Your funds are 100% safe!"
                )
                await notify(alert_text)
                for r in results:
                    r.status = "CANCELLED_PRICE_CHANGED"
                    r.error_message = f"Price changed from {approved_desc} to {new_price_desc}"
                return results

            # 6. INSTANT PARALLEL BROADCAST!
            await notify("⚡ T-0: FIRING ALL TRANSACTIONS SIMULTANEOUSLY! 🚀")
            broadcast_start = time.time()

            broadcast_results = await active_chain.broadcast_batch(prepared_tx_bytes)
            elapsed_ms = int((time.time() - broadcast_start) * 1000)

            for r in results:
                r.broadcast_time_ms = elapsed_ms

            await notify(
                f"✅ Broadcasted {len(prepared_tx_bytes)} transactions in <b>{elapsed_ms} ms</b>!\n"
                f"Tracking receipts on-chain..."
            )

            # 7. Concurrently Track Receipts with per-wallet timing
            async def track_receipt(r: WalletMintResult) -> None:
                if not r.tx_hash:
                    return
                t0 = time.time()
                try:
                    rec = await active_chain.wait_for_receipt(r.tx_hash, timeout_seconds=30.0)
                    r.confirm_time_seconds = round(time.time() - t0, 2)
                    r.total_time_seconds = round(time.time() - broadcast_start, 2)
                    if rec and not isinstance(rec, Exception):
                        if rec.status == 1:
                            r.status = "SUCCESS"
                            r.gas_used = rec.gas_used
                        else:
                            r.status = "REVERTED"
                    else:
                        r.status = "UNCONFIRMED"
                except Exception as ex:
                    r.confirm_time_seconds = round(time.time() - t0, 2)
                    r.total_time_seconds = round(time.time() - broadcast_start, 2)
                    r.status = "FAILED"
                    r.error_message = str(ex)

            await asyncio.gather(*[track_receipt(r) for r in results if r.tx_hash])

            total_elapsed = round(time.time() - broadcast_start, 2)
            success_count = sum(1 for r in results if r.status == "SUCCESS")
            await notify(
                f"🏁 Mint Completed: <b>{success_count}/{len(results)} Wallets Succeeded</b> "
                f"in <b>{total_elapsed}s</b>! ⚡"
            )

            return results

        finally:
            self.opensea.stop_heartbeat()
            if should_close_active_chain:
                await active_chain.close()

