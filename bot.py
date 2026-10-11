"""Telegram Bot UI for OpenSea Multi-Wallet SeaDrop Minter."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

import httpx

from minter.chain import ChainClient
from minter.client import CollectionInfo, DropStage, OpenSeaClient
from minter.config import AppConfig
from minter.constants import CHAINS, get_chain_info
from minter.engine import MintEngine, WalletMintResult
from minter.wallet import WalletAccount, WalletManager


class TelegramBot:
    def __init__(self, config: AppConfig, wallet_mgr: WalletManager) -> None:
        self.config = config
        self.wallets = wallet_mgr
        self.chain = ChainClient(config.rpc_url, config.fallback_rpc_url)
        self.opensea = OpenSeaClient(timeout_seconds=config.opensea_timeout_ms / 1000.0)
        self.engine = MintEngine(self.config, self.chain, self.opensea)

        self.token = config.telegram_bot_token
        self.api_url = f"https://api.telegram.org/bot{self.token}"
        self.admin_id = config.telegram_admin_id
        self._http: httpx.AsyncClient | None = None
        self._active_sessions: dict[int, dict[str, Any]] = {}  # chat_id -> state

    async def get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=45.0, write=15.0, pool=15.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=60.0),
            )
        return self._http

    async def reset_http(self) -> None:
        if self._http and not self._http.is_closed:
            try:
                await self._http.aclose()
            except Exception:
                pass
        self._http = None

    async def close(self) -> None:
        await self.reset_http()
        await self.chain.close()
        await self.opensea.close()

    async def send_request(self, method: str, data: dict[str, Any]) -> dict[str, Any]:
        client = await self.get_http()
        try:
            resp = await client.post(f"{self.api_url}/{method}", json=data)
            return resp.json()
        except Exception as e:
            return {"ok": False, "description": str(e)}

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        res = await self.send_request("sendMessage", payload)
        if res.get("ok"):
            return res.get("result", {}).get("message_id")
        return None

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self.send_request("editMessageText", payload)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self.send_request("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self.send_request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def is_authorized(self, user_id: int) -> bool:
        if not self.admin_id or self.admin_id == 0:
            # Auto-assign first user as admin
            self.admin_id = user_id
            self.config.telegram_admin_id = user_id
            self.config.save()
            return True
        return user_id == self.admin_id

    # ------------------ Main Handlers ------------------

    async def handle_start(self, chat_id: int) -> None:
        wallet_count = self.wallets.count()
        try:
            chain_id = await self.chain.get_chain_id()
            chain_info = get_chain_info(chain_id)
            chain_text = f"{chain_info['name']} (ID: {chain_id})"
        except Exception:
            chain_text = f"RPC Connected ({self.config.rpc_url})"

        text = (
            "<b>🌊 OpenSea High-Speed Multi-Wallet Minter</b>\n\n"
            f"⚡ <b>Status:</b> Ready\n"
            f"🔗 <b>Default Chain:</b> {chain_text}\n"
            f"💼 <b>Configured Wallets:</b> {wallet_count} wallet(s)\n"
            f"🚀 <b>Lead Broadcast:</b> {self.config.broadcast_lead_ms} ms\n\n"
            "<b>Quick Commands:</b>\n"
            "• <code>/mint &lt;url_or_contract&gt;</code> - Inspect Drop (or paste URL/Contract directly)\n"
            "• <code>/wallets</code> - Manage Wallets & Balances\n"
            "• <code>/addwallet &lt;private_key&gt;</code> - Add New Wallet\n"
            "• <code>/balance</code> - Check All Wallet Balances\n"
            "• <code>/doctor</code> - Run Connectivity & RPC Diagnostic\n"
            "• <code>/cancel</code> - Cancel Active Mint"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "💼 View Wallets", "callback_data": "menu_wallets"},
                    {"text": "💰 Check Balances", "callback_data": "menu_balance"},
                ],
                [
                    {"text": "🩺 Run Doctor", "callback_data": "menu_doctor"},
                    {"text": "⚙️ Settings", "callback_data": "menu_settings"},
                ],
            ]
        }
        await self.send_message(chat_id, text, reply_markup=keyboard)

    async def handle_mint_command(self, chat_id: int, locator: str) -> None:
        msg_id = await self.send_message(chat_id, f"🔍 Inspecting drop: <code>{locator}</code>...")
        if not msg_id:
            return

        try:
            coll = await self.opensea.fetch_collection(locator, chain_client=self.chain)
            initial_stage = coll.stages[0] if coll.stages else None
            self._active_sessions[chat_id] = {
                "collection": coll,
                "selected_stage": initial_stage,
                "quantity": 1,
                "selected_wallets": self.wallets.get_all(),
                "contract_address": coll.contract_address,
                "chain_id": coll.chain_id,
                "expected_price_wei": initial_stage.price_wei if initial_stage else 0,
                "expected_price_unit": initial_stage.price_unit if initial_stage else 0.0,
                "expected_price_symbol": initial_stage.price_symbol if initial_stage else "ETH",
            }

            await self.render_mint_card(chat_id, msg_id)
        except Exception as e:
            await self.edit_message(
                chat_id,
                msg_id,
                f"❌ <b>Failed to load drop:</b> {e}\n\nPlease check the URL or contract address.",
            )

    async def render_mint_card(self, chat_id: int, message_id: int) -> None:
        session = self._active_sessions.get(chat_id)
        if not session:
            return

        coll: CollectionInfo = session["collection"]
        stage: DropStage = session["selected_stage"]
        qty: int = session["quantity"]
        wallets: list[WalletAccount] = session["selected_wallets"]

        chain_info = get_chain_info(coll.chain_id)
        now = int(time.time())
        is_live = stage.is_active(now)
        rem_sec = stage.start_timestamp - now

        if is_live:
            status_text = "🟢 <b>LIVE NOW</b>"
        elif rem_sec > 0:
            status_text = f"⏳ <b>STARTS IN {rem_sec // 60}m {rem_sec % 60}s</b>"
        else:
            status_text = "🔴 <b>ENDED</b>"

        # Prominently format mint price
        if stage.price_wei == 0:
            price_display = "🆓 <b>FREE (0 ETH)</b>"
            guard_label = "FREE (0 ETH)"
        else:
            price_display = f"<b>{stage.price_unit} {stage.price_symbol}</b> (~{stage.price_wei / 1e18:.6f} ETH)"
            guard_label = f"{stage.price_unit} {stage.price_symbol}"

        text = (
            f"🖼️ <b>{coll.name}</b> (<code>{coll.slug}</code>)\n\n"
            f"🔗 <b>Network:</b> {chain_info['name']} (ID: {coll.chain_id})\n"
            f"📜 <b>Contract:</b> <code>{coll.contract_address}</code>\n"
            f"📦 <b>Supply:</b> {coll.total_supply} / {coll.max_supply}\n\n"
            f"🏷️ <b>Selected Stage:</b> {stage.label} ({stage.stage_type})\n"
            f"💰 <b>Mint Price:</b> {price_display}\n"
            f"🛡️ <b>Price Guard:</b> 🔒 Locked to {guard_label} (Auto-cancels if changed!)\n"
            f"⏱️ <b>Status:</b> {status_text}\n"
            f"🔢 <b>Quantity per Wallet:</b> {qty}\n"
            f"👥 <b>Active Wallets:</b> {len(wallets)}/{self.wallets.count()} selected"
        )

        buttons = []

        # Stage selector buttons in rows of 2
        if len(coll.stages) > 1:
            stage_rows = []
            curr_row = []
            for idx, st in enumerate(coll.stages):
                prefix = "✅ " if (stage and st.uuid == stage.uuid) else ""
                curr_row.append({"text": f"{prefix}{st.label[:12]}", "callback_data": f"stage_{idx}"})
                if len(curr_row) == 2:
                    stage_rows.append(curr_row)
                    curr_row = []
            if curr_row:
                stage_rows.append(curr_row)
            buttons.extend(stage_rows)

        # Quantity adjustment
        buttons.append([
            {"text": "➖ 1", "callback_data": "qty_dec"},
            {"text": f"Qty: {qty}", "callback_data": "qty_curr"},
            {"text": "➕ 1", "callback_data": "qty_inc"},
        ])

        # Fire Button
        fire_label = f"🚀 FIRE MINT ({len(wallets)} Wallets)" if is_live else f"⏳ SCHEDULE MINT ({len(wallets)} Wallets)"
        buttons.append([{"text": fire_label, "callback_data": "mint_launch"}])
        buttons.append([{"text": "❌ Cancel", "callback_data": "mint_cancel"}])

        await self.edit_message(chat_id, message_id, text, reply_markup={"inline_keyboard": buttons})

    async def handle_mint_launch(self, chat_id: int, message_id: int) -> None:
        session = self._active_sessions.get(chat_id)
        if not session:
            await self.send_message(chat_id, "⚠️ Mint session expired. Please run /mint again.")
            return

        coll: CollectionInfo = session["collection"]
        stage: DropStage = session["selected_stage"]
        qty: int = session["quantity"]
        wallets: list[WalletAccount] = session["selected_wallets"]
        expected_price_wei: int = session.get("expected_price_wei", stage.price_wei)

        if not wallets:
            await self.send_message(chat_id, "⚠️ No wallets selected! Add a wallet with /addwallet.")
            return

        async def on_progress(status_text: str) -> None:
            await self.send_message(chat_id, status_text)

        await self.edit_message(chat_id, message_id, "🚀 <b>Minting Initiated!</b> Updates will follow below:")

        results: list[WalletMintResult] = await self.engine.execute_mint(
            collection=coll,
            stage=stage,
            wallets=wallets,
            quantity_per_wallet=qty,
            expected_price_wei=expected_price_wei,
            on_progress=on_progress,
        )

        if any(r.status == "CANCELLED_PRICE_CHANGED" for r in results):
            summary_lines = [
                "🛑 <b>MINT OPERATION ABORTED BY PRICE GUARD!</b>\n",
                "⚠️ <b>Reason:</b> Mint price was modified by the developer right before broadcast.",
                "🛡️ <b>Protection:</b> No transactions were broadcasted. Your wallet balances were not touched.",
            ]
            await self.send_message(chat_id, "\n".join(summary_lines))
            return

        # Final Summary
        total_success = sum(1 for r in results if r.status == "SUCCESS")
        max_total_time = max((r.total_time_seconds for r in results if r.total_time_seconds > 0), default=0.0)
        avg_confirm_time = (
            sum(r.confirm_time_seconds for r in results if r.confirm_time_seconds > 0) / max(1, total_success)
            if total_success > 0
            else 0.0
        )
        broadcast_ms = max((r.broadcast_time_ms for r in results), default=0)

        summary_lines = ["<b>📊 Final Mint Results:</b>"]
        if max_total_time > 0:
            summary_lines.append(
                f"⏱️ <b>Total Time Taken:</b> <b>{max_total_time:.2f}s</b> "
                f"(Broadcast: {broadcast_ms}ms | On-chain: {avg_confirm_time:.2f}s)\n"
            )
        else:
            summary_lines.append("\n")

        for r in results:
            icon = "✅" if r.status == "SUCCESS" else ("❌" if r.status == "FAILED" else "⚠️")
            line = f"{icon} <b>{r.wallet.label}</b> ({r.wallet.short_address}): {r.status}"
            if r.tx_hash:
                line += f"\n   Tx: <a href='{r.explorer_url}'>{r.tx_hash[:10]}...{r.tx_hash[-8:]}</a>"
            details = []
            if r.confirm_time_seconds > 0:
                details.append(f"⏱️ {r.confirm_time_seconds:.2f}s")
            if r.gas_used:
                details.append(f"Gas: {r.gas_used}")
            if details:
                line += f" ({' | '.join(details)})"
            if r.error_message:
                line += f"\n   Err: <i>{r.error_message[:60]}</i>"
            summary_lines.append(line)

        await self.send_message(chat_id, "\n".join(summary_lines))

    # ------------------ Wallet Handlers ------------------

    async def handle_wallets_menu(self, chat_id: int) -> None:
        wallets = self.wallets.get_all()
        if not wallets:
            text = (
                "💼 <b>No wallets configured yet.</b>\n\n"
                "Add your first wallet using:\n"
                "<code>/addwallet &lt;private_key&gt; [label]</code>\n\n"
                "<i>Your private keys remain 100% local on your phone/machine.</i>"
            )
            await self.send_message(chat_id, text)
            return

        try:
            chain_id = await self.chain.get_chain_id()
            chain_info = get_chain_info(chain_id)
            symbol = chain_info["symbol"]
            chain_name = chain_info["name"]
        except Exception:
            chain_name = "Connected RPC"
            symbol = "ETH"

        addresses = [w.address for w in wallets]
        balances = await self.chain.get_balances_batch(addresses)

        lines = [f"💼 <b>Configured Wallets ({len(wallets)}):</b>", f"Network: {chain_name}\n"]
        for idx, w in enumerate(wallets):
            bal = balances.get(w.address, 0) / 1e18
            lines.append(
                f"<b>{idx+1}. {w.label}</b>\n"
                f"   Address: <code>{w.address}</code>\n"
                f"   Balance: <b>{bal:.5f} {symbol}</b>\n"
            )

        lines.append("To delete a wallet: <code>/delwallet &lt;number&gt;</code>")
        lines.append("To add a wallet: <code>/addwallet &lt;private_key&gt; [label]</code>")

        keyboard = {
            "inline_keyboard": [
                [{"text": "🔄 Refresh Balances", "callback_data": "menu_balance"}],
            ]
        }
        await self.send_message(chat_id, "\n".join(lines), reply_markup=keyboard)

    async def handle_doctor(self, chat_id: int) -> None:
        msg_id = await self.send_message(chat_id, "🩺 <b>Running diagnostic check...</b>")
        if not msg_id:
            return

        report = ["<b>🩺 Diagnostics Report:</b>\n"]

        # 1. RPC Check
        t0 = time.time()
        try:
            chain_id = await self.chain.get_chain_id()
            block_num = await self.chain.get_block_number()
            block_ts = await self.chain.get_block_timestamp()
            rpc_ms = int((time.time() - t0) * 1000)
            chain_info = get_chain_info(chain_id)
            skew = abs(int(time.time()) - block_ts)
            report.append(
                f"✅ <b>RPC Connected:</b> {chain_info['name']} (ID: {chain_id})\n"
                f"   Latency: <b>{rpc_ms} ms</b> | Latest Block: #{block_num}\n"
                f"   Local Clock Skew: ~{skew}s\n"
            )
        except Exception as e:
            report.append(f"❌ <b>RPC Error:</b> {e}\n")

        # 2. OpenSea Connection Check
        t1 = time.time()
        try:
            client = await self.opensea.get_client()
            resp = await client.post(
                "https://gql.opensea.io/graphql",
                json={
                    "operationName": "DoctorCheck",
                    "query": "query DoctorCheck { __typename }",
                    "variables": {},
                },
                headers={
                    "accept": "application/json",
                    "x-app-id": "os2-web",
                    "origin": "https://opensea.io",
                },
            )
            os_ms = int((time.time() - t1) * 1000)
            if resp.status_code == 200:
                report.append(f"✅ <b>OpenSea GraphQL Connected:</b> {os_ms} ms\n")
            else:
                report.append(f"⚠️ <b>OpenSea Status:</b> HTTP {resp.status_code}\n")
        except Exception as e:
            report.append(f"❌ <b>OpenSea Error:</b> {e}\n")

        # 3. Wallets Check
        w_count = self.wallets.count()
        report.append(f"💼 <b>Wallets Loaded:</b> {w_count} wallet(s)")

        await self.edit_message(chat_id, msg_id, "\n".join(report))

    async def handle_settings(self, chat_id: int) -> None:
        try:
            chain_id = await self.chain.get_chain_id()
            chain_info = get_chain_info(chain_id)
            network_name = f"{chain_info['name']} (ID: {chain_id})"
        except Exception:
            network_name = f"Custom ({self.config.rpc_url})"

        text = (
            "<b>⚙️ Current Bot Settings:</b>\n\n"
            f"• <b>RPC Endpoint:</b> <code>{self.config.rpc_url}</code>\n"
            f"• <b>Network:</b> {network_name}\n"
            f"• <b>Gas Limit:</b> {self.config.gas_limit}\n"
            f"• <b>Active Fee Multiplier:</b> {self.config.fee_multiplier}x\n"
            f"• <b>Scheduled Fee Multiplier:</b> {self.config.scheduled_fee_multiplier}x\n"
            f"• <b>Priority Fee:</b> {self.config.max_priority_fee_gwei} Gwei\n"
            f"• <b>Broadcast Lead:</b> {self.config.broadcast_lead_ms} ms\n"
            f"• <b>Calldata Retry Attempts:</b> {self.config.max_retry_attempts} attempts\n"
            f"• <b>Heartbeat Interval:</b> {self.config.session_heartbeat_interval_seconds}s"
        )
        await self.send_message(chat_id, text)

    # ------------------ Callback Query Dispatcher ------------------

    async def handle_callback(self, callback: dict[str, Any]) -> None:
        cb_id = callback.get("id", "")
        data = callback.get("data", "")
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        msg_id = message.get("message_id")

        if not chat_id or not msg_id:
            return

        await self.answer_callback(cb_id)

        if data == "menu_wallets":
            await self.handle_wallets_menu(chat_id)
        elif data == "menu_balance":
            await self.handle_wallets_menu(chat_id)
        elif data == "menu_doctor":
            await self.handle_doctor(chat_id)
        elif data == "menu_settings":
            await self.handle_settings(chat_id)
        elif data.startswith("stage_"):
            idx = int(data.split("_")[1])
            session = self._active_sessions.get(chat_id)
            if session:
                coll: CollectionInfo = session["collection"]
                if 0 <= idx < len(coll.stages):
                    new_stage = coll.stages[idx]
                    session["selected_stage"] = new_stage
                    session["expected_price_wei"] = new_stage.price_wei
                    session["expected_price_unit"] = new_stage.price_unit
                    session["expected_price_symbol"] = new_stage.price_symbol
                    await self.render_mint_card(chat_id, msg_id)
        elif data == "qty_inc":
            session = self._active_sessions.get(chat_id)
            if session:
                session["quantity"] = min(session["quantity"] + 1, 50)
                await self.render_mint_card(chat_id, msg_id)
        elif data == "qty_dec":
            session = self._active_sessions.get(chat_id)
            if session:
                session["quantity"] = max(session["quantity"] - 1, 1)
                await self.render_mint_card(chat_id, msg_id)
        elif data == "mint_launch":
            await self.handle_mint_launch(chat_id, msg_id)
        elif data == "mint_cancel":
            self.engine.cancel()
            self._active_sessions.pop(chat_id, None)
            await self.edit_message(chat_id, msg_id, "❌ Mint cancelled.")

    # ------------------ Telegram Polling Loop ------------------

    async def run_polling(self) -> None:
        print("[*] OpenSea Telegram Minter Bot is running! Listening for updates...")
        offset = 0

        while True:
            try:
                client = await self.get_http()
                resp = await client.post(
                    f"{self.api_url}/getUpdates",
                    json={"offset": offset, "timeout": 20},
                    timeout=httpx.Timeout(connect=15.0, read=35.0, write=15.0, pool=15.0),
                )
                data = resp.json()
                if not data.get("ok"):
                    desc = data.get("description", "Unknown Telegram error")
                    err_code = data.get("error_code")
                    if err_code == 409:
                        print(
                            f"[!] Telegram Conflict 409: Another bot instance is running with this token! Waiting 5s..."
                        )
                        await asyncio.sleep(5.0)
                    else:
                        print(f"[!] Telegram API error ({err_code}): {desc}")
                        await asyncio.sleep(2.0)
                    continue

                updates = data.get("result", [])
                for update in updates:
                    offset = max(offset, update["update_id"] + 1)

                    # Handle Callback Queries (Buttons)
                    if "callback_query" in update:
                        cb = update["callback_query"]
                        user_id = cb.get("from", {}).get("id")
                        if self.is_authorized(user_id):
                            await self.handle_callback(cb)
                        continue

                    # Handle Messages
                    if "message" in update:
                        msg = update["message"]
                        user_id = msg.get("from", {}).get("id")
                        chat_id = msg.get("chat", {}).get("id")
                        text = msg.get("text", "").strip()
                        msg_id = msg.get("message_id")

                        if not self.is_authorized(user_id):
                            await self.send_message(chat_id, "⛔ Unauthorized user. This bot is private.")
                            continue

                        # Command routing
                        if text == "/start" or text == "/help":
                            await self.handle_start(chat_id)

                        elif text.startswith("/mint"):
                            parts = text.split(maxsplit=1)
                            if len(parts) > 1:
                                await self.handle_mint_command(chat_id, parts[1])
                            else:
                                await self.send_message(
                                    chat_id,
                                    "Usage: <code>/mint &lt;url_or_contract&gt;</code>\n\n"
                                    "<b>Examples:</b>\n"
                                    "• <code>/mint https://opensea.io/collection/trenchstrokerz</code>\n"
                                    "• <code>/mint 0x2c8c306f34969d05230bf1c7c8ca6becc8e97e6b</code>\n\n"
                                    "<i>Tip: You can also paste the URL or Contract Address directly!</i>",
                                )

                        # Direct paste detection for OpenSea URL or 0x contract address
                        elif (
                            text.startswith("http://")
                            or text.startswith("https://")
                            or (text.startswith("0x") and len(text) == 42)
                        ):
                            await self.handle_mint_command(chat_id, text)

                        elif text == "/wallets":
                            await self.handle_wallets_menu(chat_id)

                        elif text.startswith("/addwallet"):
                            # Delete the incoming message with private key immediately for security!
                            await self.delete_message(chat_id, msg_id)
                            parts = text.split()
                            if len(parts) >= 2:
                                pk = parts[1]
                                label = parts[2] if len(parts) >= 3 else ""
                                try:
                                    w = self.wallets.add_wallet(pk, label=label)
                                    await self.send_message(
                                        chat_id,
                                        f"✅ <b>Wallet Added:</b> {w.label}\n"
                                        f"Address: <code>{w.address}</code>\n"
                                        f"<i>(Your message containing the key was auto-deleted for security.)</i>",
                                    )
                                except Exception as e:
                                    await self.send_message(chat_id, f"❌ Failed to add wallet: {e}")
                            else:
                                await self.send_message(
                                    chat_id,
                                    "Usage: <code>/addwallet &lt;private_key&gt; [label]</code>",
                                )

                        elif text.startswith("/delwallet"):
                            parts = text.split()
                            if len(parts) >= 2 and parts[1].isdigit():
                                idx = int(parts[1]) - 1
                                try:
                                    removed = self.wallets.remove_wallet(idx)
                                    await self.send_message(
                                        chat_id,
                                        f"🗑️ Removed wallet <b>{removed.label}</b> (<code>{removed.short_address}</code>)",
                                    )
                                except Exception as e:
                                    await self.send_message(chat_id, f"❌ Error: {e}")
                            else:
                                await self.send_message(
                                    chat_id, "Usage: <code>/delwallet &lt;number&gt;</code>"
                                )

                        elif text == "/balance":
                            await self.handle_wallets_menu(chat_id)

                        elif text == "/doctor":
                            await self.handle_doctor(chat_id)

                        elif text == "/settings" or text == "/config":
                            await self.handle_settings(chat_id)

                        elif text == "/cancel":
                            self.engine.cancel()
                            self._active_sessions.pop(chat_id, None)
                            await self.send_message(chat_id, "🛑 Cancelled active mint operations.")

            except httpx.TimeoutException:
                # Normal long polling timeout (no messages from Telegram during poll interval). Continue smoothly.
                continue
            except (httpx.NetworkError, httpx.RemoteProtocolError) as net_err:
                err_label = type(net_err).__name__
                print(f"[*] Telegram connection refreshed ({err_label}). Reconnecting...")
                await self.reset_http()
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                err_name = type(e).__name__
                err_detail = str(e) or err_name
                print(f"[!] Polling recovered from [{err_name}]: {err_detail}. Reconnecting...")
                await self.reset_http()
                await asyncio.sleep(2.0)


async def main() -> None:
    config = AppConfig.load()
    if not config.telegram_bot_token:
        print("[!] Error: TELEGRAM_BOT_TOKEN is not configured!")
        print("Set it in config.json or in your .env file.")
        sys.exit(1)

    wallets = WalletManager(config.wallets_file)
    bot = TelegramBot(config, wallets)
    try:
        while True:
            try:
                await bot.run_polling()
                break
            except (KeyboardInterrupt, SystemExit):
                print("\n[*] Bot stopped by user.")
                break
            except Exception as e:
                print(f"[!] Bot runner exception: {repr(e)}. Auto-restarting in 3s...")
                await asyncio.sleep(3.0)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
