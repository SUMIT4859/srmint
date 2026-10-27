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
    decode_seadrop_error,
    get_chain_info,
    get_fee_recipient,
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
    calldata_time_ms: int = 0
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
            target_rpc, target_fallback = self.config.get_rpc_for_chain(chain_id)
            if not target_rpc:
                target_rpc = chain_info.get("default_rpc", "")
            if target_rpc:
                await notify(
                    f"🔄 Auto-routing to <b>{chain_info['name']}</b> (Chain ID: {chain_id})\n"
                    f"RPC: <code>{target_rpc}</code>"
                )
                active_chain = ChainClient(
                    target_rpc,
                    fallback_rpc_url=target_fallback,
                    timeout_seconds=self.chain.timeout_seconds,
                )
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
            prepared_tx_map: dict[str, bytes] = {}
            wallets_needing_t0: list[WalletAccount] = []

            if is_public:
                # Direct on-chain SeaDrop mintPublic with chain-specific fee recipient: zero API delay, instant 100% pre-signed!
                fee_recipient = get_fee_recipient(chain_id)
                for w in wallets:
                    cdata = encode_public_mint(
                        nft_contract=collection.contract_address,
                        minter_recipient=w.address,
                        quantity=quantity_per_wallet,
                        fee_recipient=fee_recipient,
                    )
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
                    prepared_tx_map[w.address] = raw_bytes
                    for r in results:
                        if r.wallet.address == w.address:
                            r.tx_hash = tx_hash
                            r.explorer_url = get_tx_explorer_url(chain_id, tx_hash)

            else:
                # Private/Allowlist: zero-spam single-attempt pre-fetch
                await notify("📡 T-10s: Checking for advance allowlist calldata (single attempt)...")
                prefetch_sem = asyncio.Semaphore(3)

                async def prefetch_check(idx: int, w: WalletAccount):
                    await asyncio.sleep(idx * 0.025)
                    async with prefetch_sem:
                        return await self.opensea.get_mint_action_hot(
                            collection,
                            w,
                            quantity_per_wallet,
                            stage,
                            max_attempts=1,
                            single_attempt=True,
                        )

                prefetch_tasks = [prefetch_check(i, w) for i, w in enumerate(wallets)]
                prefetch_results = await asyncio.gather(*prefetch_tasks, return_exceptions=True)

                prefetched_actions: dict[str, MintAction] = {}
                for w, act_res in zip(wallets, prefetch_results):
                    r = next(res for res in results if res.wallet.address == w.address)
                    if isinstance(act_res, MintAction):
                        # Safety Net: Price Guard checks
                        if expected_price_wei == 0 and act_res.value > 0:
                            r.status = "CANCELLED_PAID_ON_FREE"
                            r.error_message = f"Calldata required {act_res.value / 1e18:.6f} ETH on Free stage"
                            continue
                        if expected_price_wei is not None and act_res.value > (expected_price_wei * quantity_per_wallet):
                            r.status = "CANCELLED_PRICE_EXCEEDED"
                            r.error_message = f"Calldata value ({act_res.value}) exceeded expected ({expected_price_wei * quantity_per_wallet})"
                            continue

                        nonce = nonces[w.address]
                        tx_dict = {
                            "type": 2,
                            "chainId": chain_id,
                            "nonce": nonce,
                            "maxPriorityFeePerGas": gas_fees.max_priority_fee_per_gas,
                            "maxFeePerGas": gas_fees.max_fee_per_gas,
                            "gas": self.config.gas_limit,
                            "to": to_checksum_address(act_res.target),
                            "value": act_res.value,
                            "data": act_res.calldata,
                        }
                        raw_bytes, tx_hash = w.sign_transaction(tx_dict)
                        prepared_tx_map[w.address] = raw_bytes
                        r.tx_hash = tx_hash
                        r.explorer_url = get_tx_explorer_url(chain_id, tx_hash)
                        prefetched_actions[w.address] = act_res

                    elif isinstance(act_res, Exception):
                        err_msg = str(act_res)
                        if "stage activation" in err_msg.lower() or "not active" in err_msg.lower():
                            # Stage unlocks at T-0: queue for T-0 pipeline without spamming
                            wallets_needing_t0.append(w)
                        elif "not on the allowlist" in err_msg.lower() or "not eligible" in err_msg.lower() or "allocation limit exceeded" in err_msg.lower():
                            r.status = "FAILED"
                            r.error_message = err_msg
                        else:
                            # Rate limited or temporary network error: queue for T-0 pipeline
                            wallets_needing_t0.append(w)

                # Check if price guard cancelled any pre-signed wallet
                if any(r.status.startswith("CANCELLED") for r in results):
                    first_canc = next(r for r in results if r.status.startswith("CANCELLED"))
                    await notify(f"🚨 <b>PRICE GUARD ABORT:</b> {first_canc.error_message}")
                    return results

                # Price Uniformity Guard on pre-fetched actions
                if len(prefetched_actions) > 1:
                    distinct_vals = {act.value for act in prefetched_actions.values()}
                    if len(distinct_vals) > 1:
                        alert_msg = (
                            "🚨 <b>PRICE GUARD: MISMATCHED PRICES IN CALLEDATA!</b>\n"
                            "Differing prices returned across wallets in pre-fetched allowlist calldata. Mint aborted!"
                        )
                        await notify(alert_msg)
                        for r in results:
                            r.status = "CANCELLED_PRICE_MISMATCH"
                            r.error_message = "Differing prices returned across wallets in allowlist calldata"
                        return results

                ready_count = len(prepared_tx_map)
                if ready_count == len(wallets):
                    await notify(f"⚡ All {ready_count} wallet(s) 100% pre-signed & ready for instant T-0 firing!")
                elif ready_count > 0:
                    await notify(f"⚡ {ready_count} wallet(s) pre-signed! {len(wallets_needing_t0)} queued for pipelined T-0 fetch.")
                else:
                    await notify(f"⏳ Calldata will unlock at T-0. Pipelined workers ready for {len(wallets_needing_t0)} wallet(s).")

            # 5. High-Precision Countdown to T-0 with RPC Pre-Warmup & Lead Offset Compensation
            if target_start_ts > time.time():
                lead_seconds = self.config.broadcast_lead_ms / 1000.0
                target_broadcast_time = target_start_ts - lead_seconds

                rem = target_broadcast_time - time.time()
                # If countdown is long enough, sleep until T-1.5s then pulse warm RPC sockets
                if rem > 1.8:
                    await asyncio.sleep(rem - 1.5)
                    # Warmup both primary and fallback RPC connections so TCP/TLS handshakes are hot for T-0!
                    try:
                        tasks = [active_chain.rpc_request("eth_blockNumber", [])]
                        if active_chain.fallback_rpc_url and active_chain.fallback_rpc_url != active_chain.rpc_url:
                            tasks.append(active_chain.rpc_request("eth_blockNumber", [], use_fallback=True))
                        await asyncio.gather(*tasks, return_exceptions=True)
                    except Exception:
                        pass

                rem_final = target_broadcast_time - time.time()
                if rem_final > 0:
                    await asyncio.sleep(rem_final)

            # 6. INSTANT PIPELINED BROADCAST (Zero synchronous network delay at T-0)
            asyncio.create_task(notify("⚡ T-0: FIRING TRANSACTIONS TO RPC! 🚀"))
            broadcast_start = time.time()

            async def broadcast_presigned(w_addr: str, raw_bytes: bytes) -> None:
                t0 = time.time()
                r = next(res for res in results if res.wallet.address == w_addr)
                try:
                    await active_chain.broadcast_raw_transaction(raw_bytes)
                    r.broadcast_time_ms = int((time.time() - t0) * 1000)
                except Exception as ex:
                    r.status = "FAILED"
                    r.error_message = f"Broadcast failed: {ex}"

            pipeline_sem = asyncio.Semaphore(10)

            async def pipeline_wallet(idx: int, w: WalletAccount) -> None:
                r = next(res for res in results if res.wallet.address == w.address)
                await asyncio.sleep(idx * 0.015)

                t_call_start = time.time()
                async with pipeline_sem:
                    try:
                        act = await self.opensea.get_mint_action_hot(
                            collection,
                            w,
                            quantity_per_wallet,
                            stage,
                            max_attempts=self.config.max_retry_attempts,
                            retry_interval_ms=50,
                        )
                        r.calldata_time_ms = int((time.time() - t_call_start) * 1000)
                    except Exception as ex:
                        r.calldata_time_ms = int((time.time() - t_call_start) * 1000)
                        r.status = "FAILED"
                        r.error_message = str(ex)
                        return

                # Safety Net: Price Guard checks
                if expected_price_wei == 0 and act.value > 0:
                    r.status = "CANCELLED_PAID_ON_FREE"
                    r.error_message = f"Calldata required {act.value / 1e18:.6f} ETH on Free stage"
                    return
                if expected_price_wei is not None and act.value > (expected_price_wei * quantity_per_wallet):
                    r.status = "CANCELLED_PRICE_EXCEEDED"
                    r.error_message = f"Calldata value ({act.value}) exceeded expected ({expected_price_wei * quantity_per_wallet})"
                    return

                # Sign locally
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
                r.tx_hash = tx_hash
                r.explorer_url = get_tx_explorer_url(chain_id, tx_hash)

                # IMMEDIATELY BROADCAST this wallet's transaction!
                t0 = time.time()
                try:
                    await active_chain.broadcast_raw_transaction(raw_bytes)
                    r.broadcast_time_ms = int((time.time() - t0) * 1000)
                except Exception as ex:
                    r.status = "FAILED"
                    r.error_message = f"Broadcast failed: {ex}"

            # Dispatch all tasks concurrently
            dispatch_tasks = []
            for w_addr, raw_bytes in prepared_tx_map.items():
                dispatch_tasks.append(broadcast_presigned(w_addr, raw_bytes))
            for idx, w in enumerate(wallets_needing_t0):
                dispatch_tasks.append(pipeline_wallet(idx, w))

            if dispatch_tasks:
                await asyncio.gather(*dispatch_tasks)

            # Check if any price guard cancellation occurred
            if any(r.status.startswith("CANCELLED") for r in results):
                first_canc = next(r for r in results if r.status.startswith("CANCELLED"))
                await notify(f"🚨 <b>PRICE GUARD ABORT:</b> {first_canc.error_message}")
                return results

            broadcasted = [r for r in results if r.tx_hash and not r.status.startswith("CANCELLED") and r.status != "FAILED"]
            if broadcasted:
                max_bcast_ms = max((r.broadcast_time_ms for r in broadcasted), default=0)
                max_calldata_ms = max((r.calldata_time_ms for r in broadcasted), default=0)
                if max_calldata_ms > 0:
                    total_dispatch_ms = max_calldata_ms + max_bcast_ms
                    summary_time = f"<b>{total_dispatch_ms} ms</b> (OpenSea: {max_calldata_ms}ms | RPC: {max_bcast_ms}ms)"
                else:
                    summary_time = f"<b>{max_bcast_ms} ms</b>"
                asyncio.create_task(
                    notify(f"✅ Dispatched {len(broadcasted)} transaction(s) in {summary_time}! Tracking receipts...")
                )

            # 7. Concurrently Track Receipts with per-wallet timing
            async def track_receipt(r: WalletMintResult) -> None:
                if not r.tx_hash or r.status.startswith("CANCELLED") or r.status == "FAILED":
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
                            r.error_message = "Transaction reverted on-chain"
                            # Attempt to diagnose exact on-chain error
                            try:
                                cdata_hex = "0x" + cdata_map.get(r.wallet.address, b"").hex() if "cdata_map" in locals() else ""
                                if cdata_hex and cdata_hex != "0x":
                                    call_res = await active_chain.call(to=OPENSEA_SEADROP_ADDRESS, data=cdata_hex)
                            except Exception as sim_ex:
                                d_err = decode_seadrop_error(str(sim_ex))
                                if d_err:
                                    r.error_message = f"Reverted: {d_err}" 
                    else:
                        r.status = "UNCONFIRMED"
                        r.error_message = "Timeout waiting for on-chain receipt"
                except Exception as ex:
                    r.confirm_time_seconds = round(time.time() - t0, 2)
                    r.total_time_seconds = round(time.time() - broadcast_start, 2)
                    r.status = "FAILED"
                    r.error_message = str(ex)

            await asyncio.gather(*[track_receipt(r) for r in results])

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

