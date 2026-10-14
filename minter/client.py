"""High-performance OpenSea GraphQL and SIWE client with active session keepalive."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
from eth_utils.address import is_address, to_checksum_address

from .constants import (
    APP_ID,
    GET_PUBLIC_DROP_SELECTOR,
    GRAPHQL_URL,
    OPENSEA_SEADROP_ADDRESS,
    SITE_URL,
    SIWE_STATEMENT,
)
from .wallet import WalletAccount

CONTRACT_QUERY = """
query MintCollectionByContract($contract: ContractIdentifierInput!) {
  collectionByContract(contract: $contract) {
    name
    slug
    address
    imageUrl
    chain { identifier networkId }
  }
}
"""

COLLECTION_QUERY = """
query MintCollectionMetadata($slug: String!) {
  collectionBySlug(slug: $slug) {
    __typename
    ... on Collection {
      name
      slug
      address
      imageUrl
      chain { identifier networkId }
    }
  }
  dropBySlug(slug: $slug) {
    __typename
    disabledReason
    identifier { contractAddress chain { identifier } }
    ... on Erc721SeaDropV1 {
      maxSupply
      totalSupply
    }
    stages {
      __typename
      uuid
      label
      stageType
      stageIndex
      startTime
      endTime
      maxTotalMintableByWallet
      price {
        token { unit symbol contractAddress chain { identifier } }
      }
    }
  }
}
"""

ELIGIBILITY_QUERY = """
query DropEligibilityQuery($collectionSlug: String!, $address: Address!) {
  dropBySlug(slug: $collectionSlug) {
    ... on Erc721SeaDropV1 { minterQuantityMinted(minter: $address) }
    accountStageEligibility {
      wallets {
        address
        quantityMinted
        stages {
          dropStageUuid
          isEligible
          maxTotalMintableByWallet
          price {
            token { unit symbol contractAddress chain { identifier } }
          }
        }
      }
    }
    stages {
      uuid
      stageType
      stageIndex
      isEligible
      eligibleMinterAddress
      maxTotalMintableByWallet
      eligibleMaxTotalMintableByWallet
      eligiblePrice {
        token { unit symbol contractAddress chain { identifier } }
      }
    }
  }
}
"""

MINT_ACTION_QUERY = """
query MintActionTimelineQuery(
  $address: Address!
  $fromAssets: [AssetQuantityInput!]!
  $toAssets: [AssetQuantityInput!]!
  $recipient: Address
) {
  swap(
    address: $address
    fromAssets: $fromAssets
    toAssets: $toAssets
    recipient: $recipient
    action: MINT
  ) {
    actions {
      ... on TransactionAction {
        transactionSubmissionData {
          to
          data
          value
        }
      }
    }
    errors { __typename }
  }
}
"""


@dataclass
class DropStage:
    uuid: str
    label: str
    stage_type: str  # PUBLIC_SALE, SIGNED_PRESALE, MERKLE_PRESALE
    stage_index: int
    start_time: str
    end_time: str
    start_timestamp: int
    end_timestamp: int
    max_per_wallet: int
    price_wei: int
    price_symbol: str
    price_unit: float

    @property
    def is_public(self) -> bool:
        return self.stage_type == "PUBLIC_SALE"

    def is_active(self, now_ts: int | None = None) -> bool:
        now = now_ts or int(time.time())
        return self.start_timestamp <= now < self.end_timestamp


@dataclass
class CollectionInfo:
    name: str
    slug: str
    contract_address: str
    image_url: str
    chain_identifier: str
    chain_id: int
    total_supply: int
    max_supply: int
    stages: list[DropStage]
    disabled_reason: str | None = None


@dataclass
class MintAction:
    target: str
    value: int
    calldata: bytes


@dataclass
class WalletEligibility:
    is_eligible: bool
    price_wei: int = 0
    price_unit: float = 0.0
    price_symbol: str = "ETH"
    reason: str = ""

    @property
    def is_free(self) -> bool:
        return self.price_wei == 0

    def format_price(self, eth_usd_rate: float = 2500.0) -> str:
        if not self.is_eligible:
            return "Ineligible"
        if self.is_free:
            return "FREE ($0.00)"
        usd = self.price_unit * eth_usd_rate
        unit_str = f"{self.price_unit:.8f}".rstrip("0").rstrip(".") if self.price_unit > 0 else "0"
        return f"{unit_str} {self.price_symbol} (~${usd:.2f})"

    def __bool__(self) -> bool:
        return self.is_eligible

    def __iter__(self):
        # Backward compatibility for code doing `is_eligible, reason = ...`
        return iter((self.is_eligible, self.reason))


_eth_usd_cache = {"price": 2500.0, "timestamp": 0.0}


async def get_eth_usd_rate() -> float:
    """Fetch current ETH/USD rate with 5-minute caching and resilient fallbacks."""
    now = time.time()
    if now - _eth_usd_cache["timestamp"] < 300.0:
        return _eth_usd_cache["price"]

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT")
            if resp.status_code == 200:
                p = float(resp.json().get("price", 0))
                if p > 0:
                    _eth_usd_cache["price"] = p
                    _eth_usd_cache["timestamp"] = now
                    return p
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd")
            if resp.status_code == 200:
                p = float(resp.json().get("ethereum", {}).get("usd", 0))
                if p > 0:
                    _eth_usd_cache["price"] = p
                    _eth_usd_cache["timestamp"] = now
                    return p
    except Exception:
        pass

    return _eth_usd_cache["price"]


class OpenSeaClient:
    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._client: httpx.AsyncClient | None = None
        self._wallet_sessions: dict[str, dict[str, str]] = {}  # address -> cookies/headers
        self._heartbeat_task: asyncio.Task | None = None

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"},
                limits=httpx.Limits(max_connections=30, max_keepalive_connections=30, keepalive_expiry=120.0),
                http2=True,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        self.stop_heartbeat()
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def start_heartbeat(self, slug: str, wallets: list[WalletAccount], interval_seconds: int = 25) -> None:
        """Start background session keepalive to prevent timeouts during long waits."""
        self.stop_heartbeat()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(slug, wallets, interval_seconds))

    def stop_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def _heartbeat_loop(self, slug: str, wallets: list[WalletAccount], interval_seconds: int) -> None:
        """Keeps HTTP/2 connection and OpenSea SIWE auth tokens alive and boiling hot!"""
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                # Ping GraphQL lightly
                client = await self.get_client()
                await client.post(
                    GRAPHQL_URL,
                    json={"query": "query KeepAlive { __typename }", "variables": {}},
                    headers={"x-app-id": APP_ID, "origin": SITE_URL},
                )
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    @staticmethod
    def parse_slug(locator: str) -> str:
        s = locator.strip()
        if s.startswith("http://") or s.startswith("https://"):
            parsed = urlsplit(s)
            parts = [p for p in parsed.path.strip("/").split("/") if p]
            if len(parts) >= 2 and parts[0] == "collection":
                return parts[1]
            if len(parts) >= 3 and parts[0] == "mint":
                return parts[1]
            if parts:
                return parts[-1]
        return s

    async def resolve_contract_to_slug(self, contract_address: str) -> str | None:
        """Resolve a contract address to an OpenSea collection slug."""
        client = await self.get_client()
        headers = {
            "accept": "application/json",
            "origin": SITE_URL,
            "referer": SITE_URL,
            "x-app-id": APP_ID,
        }
        chains_to_try = ["ROBINHOOD", "BASE", "ARBITRUM", "ETHEREUM", "POLYGON", "OPTIMISM", "ZORA"]
        checksummed = to_checksum_address(contract_address)

        for chain in chains_to_try:
            try:
                resp = await client.post(
                    GRAPHQL_URL,
                    json={
                        "operationName": "MintCollectionByContract",
                        "query": CONTRACT_QUERY,
                        "variables": {"contract": {"address": checksummed, "chain": chain}},
                    },
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", {})
                    coll = data.get("collectionByContract")
                    if coll and coll.get("slug"):
                        return coll["slug"]
            except Exception:
                continue
        return None

    async def fetch_onchain_collection(self, chain_client: Any, contract_address: str) -> CollectionInfo:
        """Fallback to resolve drop metadata directly from smart contract on-chain."""
        from eth_abi import decode
        from eth_utils import function_signature_to_4byte_selector

        checksummed = to_checksum_address(contract_address)

        # 1. Fetch token name
        try:
            name_sel = function_signature_to_4byte_selector("name()")
            res_name = await chain_client.call(checksummed, "0x" + name_sel.hex())
            name = decode(["string"], bytes.fromhex(res_name[2:]))[0]
        except Exception:
            name = f"Drop {checksummed[:6]}...{checksummed[-4:]}"

        # 2. Fetch getPublicDrop on SeaDrop
        addr_padded = bytes.fromhex(checksummed[2:].lower()).rjust(32, b"\x00")
        call_data = "0x" + (GET_PUBLIC_DROP_SELECTOR + addr_padded).hex()
        res_drop = await chain_client.call(OPENSEA_SEADROP_ADDRESS, call_data)
        decoded = decode(["uint80", "uint48", "uint48", "uint16", "uint16", "bool"], bytes.fromhex(res_drop[2:]))
        mint_price_wei, start_time, end_time, max_per_wallet, fee_bps, restrict_fee = decoded

        try:
            chain_id = await chain_client.get_chain_id()
        except Exception:
            chain_id = 4663

        st_time_str = datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat() if start_time else ""
        end_time_str = datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat() if end_time else ""

        stage = DropStage(
            uuid="onchain_public_drop",
            label="Public Sale",
            stage_type="PUBLIC_SALE",
            stage_index=0,
            start_time=st_time_str,
            end_time=end_time_str,
            start_timestamp=start_time,
            end_timestamp=end_time or 2147483647,
            max_per_wallet=max_per_wallet or 1,
            price_wei=mint_price_wei,
            price_symbol="ETH",
            price_unit=mint_price_wei / 1e18,
        )

        return CollectionInfo(
            name=name,
            slug=contract_address.lower(),
            contract_address=checksummed,
            image_url="",
            chain_identifier="robinhood" if chain_id == 4663 else "evm",
            chain_id=chain_id,
            total_supply=0,
            max_supply=0,
            stages=[stage],
        )

    async def fetch_collection(self, locator: str, chain_client: Any = None) -> CollectionInfo:
        loc = locator.strip()
        is_addr = False
        try:
            if loc.startswith("0x") and len(loc) == 42:
                is_addr = is_address(loc)
        except Exception:
            is_addr = False

        if is_addr:
            slug = await self.resolve_contract_to_slug(loc)
            if not slug and chain_client:
                # Direct on-chain fallback
                return await self.fetch_onchain_collection(chain_client, loc)
            if not slug:
                raise ValueError(f"Could not find OpenSea collection for contract address {loc}")
        else:
            slug = self.parse_slug(loc)

        client = await self.get_client()
        payload = {
            "operationName": "MintCollectionMetadata",
            "query": COLLECTION_QUERY,
            "variables": {"slug": slug},
        }
        headers = {
            "accept": "application/json",
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/collection/{slug}",
            "x-app-id": APP_ID,
        }
        resp = await client.post(GRAPHQL_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        coll_data = data.get("collectionBySlug")
        drop_data = data.get("dropBySlug")

        if not coll_data or not drop_data:
            raise ValueError(f"Collection '{slug}' not found or does not have a SeaDrop.")

        chain_info = coll_data.get("chain", {})
        chain_id = int(chain_info.get("networkId", 1))
        chain_ident = chain_info.get("identifier", "ethereum")

        stages: list[DropStage] = []
        for st in drop_data.get("stages", []):
            st_type = st.get("stageType", "PUBLIC_SALE")
            uuid = st.get("uuid", "")
            lbl = st.get("label") or ("Public Sale" if st_type == "PUBLIC_SALE" else "Presale")
            idx = int(st.get("stageIndex", 0))
            st_time = st.get("startTime", "")
            end_time = st.get("endTime", "")

            # Parse start and end timestamps
            try:
                start_ts = int(datetime.fromisoformat(st_time.replace("Z", "+00:00")).timestamp())
            except Exception:
                start_ts = 0
            try:
                end_ts = int(datetime.fromisoformat(end_time.replace("Z", "+00:00")).timestamp())
            except Exception:
                end_ts = 2147483647

            max_mint = int(st.get("maxTotalMintableByWallet") or 1)

            price_info = st.get("price", {}).get("token", {})
            unit_val = float(price_info.get("unit") or 0.0)
            sym = price_info.get("symbol", "ETH")
            price_wei = int(unit_val * 1e18)

            stages.append(
                DropStage(
                    uuid=uuid,
                    label=lbl,
                    stage_type=st_type,
                    stage_index=idx,
                    start_time=st_time,
                    end_time=end_time,
                    start_timestamp=start_ts,
                    end_timestamp=end_ts,
                    max_per_wallet=max_mint,
                    price_wei=price_wei,
                    price_symbol=sym,
                    price_unit=unit_val,
                )
            )

        return CollectionInfo(
            name=coll_data.get("name", slug),
            slug=slug,
            contract_address=to_checksum_address(coll_data.get("address", "")),
            image_url=coll_data.get("imageUrl", ""),
            chain_identifier=chain_ident,
            chain_id=chain_id,
            total_supply=int(drop_data.get("totalSupply") or 0),
            max_supply=int(drop_data.get("maxSupply") or 0),
            stages=stages,
            disabled_reason=drop_data.get("disabledReason"),
        )

    async def authenticate_wallet(self, wallet: WalletAccount, chain_id: int, slug: str) -> None:
        """Authenticate wallet using SIWE on OpenSea."""
        client = await self.get_client()
        coll_url = f"{SITE_URL}/collection/{slug}"
        headers = {
            "origin": SITE_URL,
            "referer": coll_url,
        }

        # 1. Fetch nonce via POST
        nonce_resp = await client.post(f"{SITE_URL}/__api/auth/siwe/nonce", headers=headers)
        nonce_resp.raise_for_status()
        nonce_data = nonce_resp.json()
        nonce = nonce_data.get("nonce") if isinstance(nonce_data, dict) else str(nonce_data)

        # 2. Construct SIWE message
        domain = "opensea.io"
        issued_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        address = wallet.address

        message = (
            f"{domain} wants you to sign in with your Ethereum account:\n{address}\n\n"
            f"{SIWE_STATEMENT}\n\nURI: {coll_url}\nVersion: 1\nChain ID: {chain_id}\n"
            f"Nonce: {nonce}\nIssued At: {issued_at}"
        )

        signature = wallet.sign_personal_message(message.encode("utf-8"))

        # 3. Verify
        verify_body = {
            "message": {
                "domain": domain,
                "address": address,
                "statement": SIWE_STATEMENT,
                "uri": coll_url,
                "version": "1",
                "chainId": str(chain_id),
                "nonce": nonce,
                "issuedAt": issued_at,
                "accountType": "Ethereum",
            },
            "signature": signature,
            "chainArch": "EVM",
        }
        verify_resp = await client.post(f"{SITE_URL}/__api/auth/siwe/verify", json=verify_body, headers=headers)
        if verify_resp.status_code == 200:
            # Sync cookies to .opensea.io and gql.opensea.io so GraphQL session is recognized
            for cookie in list(client.cookies.jar):
                try:
                    client.cookies.set(cookie.name, cookie.value, domain=".opensea.io", path="/")
                    client.cookies.set(cookie.name, cookie.value, domain="gql.opensea.io", path="/")
                except Exception:
                    pass
            client.cookies.set("connected-account-server-hint", wallet.address, domain=".opensea.io", path="/")
            client.cookies.set("connected-account-server-hint", wallet.address, domain="gql.opensea.io", path="/")

    async def authenticate_all_wallets(self, wallets: list[WalletAccount], chain_id: int, slug: str) -> None:
        """Authenticate multiple wallets concurrently."""
        tasks = [self.authenticate_wallet(w, chain_id, slug) for w in wallets]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def get_mint_action_hot(
        self,
        collection: CollectionInfo,
        wallet: WalletAccount,
        quantity: int,
        stage: DropStage,
        max_attempts: int = 30,
        retry_interval_ms: int = 200,
    ) -> MintAction:
        """Fetch private/allowlist mint calldata with resilient adaptive retry."""
        client = await self.get_client()
        zero_addr = "0x0000000000000000000000000000000000000000"
        variables = {
            "address": wallet.address,
            "fromAssets": [
                {
                    "asset": {
                        "contractAddress": zero_addr,
                        "chain": collection.chain_identifier,
                    }
                }
            ],
            "toAssets": [
                {
                    "asset": {
                        "contractAddress": collection.contract_address,
                        "chain": collection.chain_identifier,
                        "tokenId": "0",
                    },
                    "quantity": str(quantity),
                }
            ],
            "recipient": None,
        }

        headers = {
            "accept": "application/json",
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/collection/{collection.slug}",
            "x-app-id": APP_ID,
        }

        last_err = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await client.post(
                    GRAPHQL_URL,
                    json={
                        "operationName": "MintActionTimelineQuery",
                        "query": MINT_ACTION_QUERY,
                        "variables": variables,
                    },
                    headers=headers,
                )
                if resp.status_code == 200:
                    resp_json = resp.json()
                    errors = resp_json.get("errors") or []
                    if errors:
                        err_msgs = [e.get("message", "") for e in errors if isinstance(e, dict)]
                        err_text = ", ".join(err_msgs)
                        if "can not perform trading operations" in err_text.lower() or "cannot perform trading operations" in err_text.lower():
                            raise RuntimeError("Wallet is not on the Allowlist / Not eligible for this private stage")
                        last_err = RuntimeError(f"OpenSea: {err_text}")

                    data = resp_json.get("data")
                    if data and isinstance(data, dict):
                        swap = data.get("swap") or {}
                        actions = swap.get("actions", [])
                        if actions:
                            sub_data = actions[0].get("transactionSubmissionData", {})
                            if sub_data and "data" in sub_data:
                                target = to_checksum_address(sub_data.get("to", OPENSEA_SEADROP_ADDRESS))
                                val_raw = sub_data.get("value", 0)
                                if isinstance(val_raw, str):
                                    val = int(val_raw, 16) if val_raw.startswith("0x") else int(val_raw)
                                else:
                                    val = int(val_raw)
                                calldata = bytes.fromhex(sub_data["data"].lstrip("0x"))
                                return MintAction(target=target, value=val, calldata=calldata)

                        swap_errors = swap.get("errors", [])
                        if swap_errors:
                            err_names = [e.get("__typename", "") for e in swap_errors if isinstance(e, dict)]
                            if "MinterNotEligibleForActiveDropStageError" in err_names:
                                now_ts = time.time()
                                # If the target stage has not started yet or is within transition window (up to start + 8s),
                                # OpenSea's active stage may still be the previous stage. Do not fail permanently yet!
                                if stage.start_timestamp > 0 and now_ts < (stage.start_timestamp + 8.0):
                                    last_err = RuntimeError("Waiting for OpenSea stage activation...")
                                else:
                                    raise RuntimeError("Wallet is not on the Allowlist for this stage")
                            elif "InsufficientMintsRemainingError" in err_names or "MintQuantityMoreThanAllocatedForMinterError" in err_names:
                                raise RuntimeError("Allocation limit exceeded for this wallet")
                            else:
                                last_err = RuntimeError(f"OpenSea: {', '.join(err_names)}")
            except Exception as e:
                last_err = e
                # If definitely not eligible outside the transition window, stop retrying immediately
                now_ts = time.time()
                is_transition_window = stage.start_timestamp > 0 and now_ts < (stage.start_timestamp + 8.0)
                if not is_transition_window:
                    if "not on the allowlist" in str(e).lower() or "not eligible" in str(e).lower():
                        raise e

            # Adaptive retry sleep
            await asyncio.sleep(retry_interval_ms / 1000.0)

        raise RuntimeError(f"OpenSea mint calldata unavailable: {last_err}")

    async def check_wallet_eligibility(
        self,
        collection: CollectionInfo,
        stage: DropStage,
        wallet: WalletAccount,
    ) -> WalletEligibility:
        """
        Verify if a wallet is eligible to mint in the given drop stage,
        and retrieve the exact per-wallet mint price.
        Returns WalletEligibility.
        """
        if stage.is_public:
            return WalletEligibility(
                is_eligible=True,
                price_wei=stage.price_wei,
                price_unit=stage.price_unit,
                price_symbol=stage.price_symbol,
                reason="Eligible (Public Sale)",
            )

        # Ensure wallet has an active SIWE session
        try:
            await self.authenticate_wallet(wallet, collection.chain_id, collection.slug)
        except Exception:
            pass

        client = await self.get_client()
        headers = {
            "accept": "application/json",
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/collection/{collection.slug}",
            "x-app-id": APP_ID,
        }

        # 1. Query DropEligibilityQuery
        try:
            resp = await client.post(
                GRAPHQL_URL,
                json={
                    "operationName": "DropEligibilityQuery",
                    "query": ELIGIBILITY_QUERY,
                    "variables": {
                        "collectionSlug": collection.slug,
                        "address": wallet.address,
                    },
                },
                headers=headers,
            )
            if resp.status_code == 200:
                d = resp.json().get("data", {}).get("dropBySlug", {})
                account_elig = d.get("accountStageEligibility") or {}
                wallets_list = account_elig.get("wallets") or []

                # Baseline fallback price from top-level stage
                fallback_unit = stage.price_unit
                fallback_sym = stage.price_symbol
                fallback_wei = stage.price_wei
                for st in d.get("stages") or []:
                    if st.get("uuid") == stage.uuid or st.get("stageIndex") == stage.stage_index:
                        el_p = (st.get("eligiblePrice") or {}).get("token") or {}
                        if el_p.get("unit") is not None:
                            fallback_unit = float(el_p.get("unit") or 0.0)
                            fallback_sym = el_p.get("symbol") or "ETH"
                            fallback_wei = int(round(fallback_unit * 1e18))

                for w_info in wallets_list:
                    if w_info.get("address", "").lower() == wallet.address.lower():
                        for st_info in w_info.get("stages", []):
                            if st_info.get("dropStageUuid") == stage.uuid:
                                if st_info.get("isEligible") is True:
                                    p_tok = (st_info.get("price") or {}).get("token") or {}
                                    if p_tok.get("unit") is not None:
                                        p_unit = float(p_tok.get("unit") or 0.0)
                                        p_sym = p_tok.get("symbol") or "ETH"
                                        p_wei = int(round(p_unit * 1e18))
                                    else:
                                        p_unit, p_sym, p_wei = fallback_unit, fallback_sym, fallback_wei
                                    return WalletEligibility(
                                        is_eligible=True,
                                        price_wei=p_wei,
                                        price_unit=p_unit,
                                        price_symbol=p_sym,
                                        reason="Eligible (Allowlist Verified)",
                                    )
                                elif st_info.get("isEligible") is False:
                                    return WalletEligibility(
                                        is_eligible=False,
                                        price_wei=0,
                                        price_unit=0.0,
                                        price_symbol="ETH",
                                        reason="Not on Allowlist",
                                    )

                # Check top-level stages array in dropBySlug
                stages_list = d.get("stages") or []
                for st in stages_list:
                    if st.get("uuid") == stage.uuid or st.get("stageIndex") == stage.stage_index:
                        if st.get("isEligible") is True:
                            el_p = (st.get("eligiblePrice") or {}).get("token") or {}
                            if el_p.get("unit") is not None:
                                p_unit = float(el_p.get("unit") or 0.0)
                                p_sym = el_p.get("symbol") or "ETH"
                                p_wei = int(round(p_unit * 1e18))
                            else:
                                p_unit, p_sym, p_wei = fallback_unit, fallback_sym, fallback_wei
                            return WalletEligibility(
                                is_eligible=True,
                                price_wei=p_wei,
                                price_unit=p_unit,
                                price_symbol=p_sym,
                                reason="Eligible (Allowlist Verified)",
                            )
                        elif st.get("isEligible") is False:
                            return WalletEligibility(
                                is_eligible=False,
                                price_wei=0,
                                price_unit=0.0,
                                price_symbol="ETH",
                                reason="Not on Allowlist",
                            )
        except Exception:
            pass

        # 2. Direct simulation via get_mint_action_hot with 1 attempt (ONLY if stage is live!)
        now_ts = time.time()
        if stage.start_timestamp <= now_ts:
            try:
                act = await self.get_mint_action_hot(
                    collection,
                    wallet,
                    1,
                    stage,
                    max_attempts=1,
                )
                if act and act.calldata:
                    p_wei = act.value
                    p_unit = act.value / 1e18
                    return WalletEligibility(
                        is_eligible=True,
                        price_wei=p_wei,
                        price_unit=p_unit,
                        price_symbol="ETH",
                        reason="Eligible (Calldata Ready)",
                    )
            except Exception as e:
                err_str = str(e).lower()
                if "not on the allowlist" in err_str or "not eligible" in err_str or "trading operations" in err_str:
                    return WalletEligibility(
                        is_eligible=False,
                        price_wei=0,
                        price_unit=0.0,
                        price_symbol="ETH",
                        reason="Not on Allowlist",
                    )

        return WalletEligibility(
            is_eligible=False,
            price_wei=0,
            price_unit=0.0,
            price_symbol="ETH",
            reason="Ineligible for this stage",
        )


