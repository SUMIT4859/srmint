"""High-performance OpenSea GraphQL and SIWE client with active session keepalive."""

from __future__ import annotations

import asyncio
import random
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
    OPENSEA_API_V2_URL,
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
    quantity: int = 1
    attempts: int = 1


@dataclass
class WalletEligibility:
    is_eligible: bool
    price_wei: int = 0
    price_unit: float = 0.0
    price_symbol: str = "ETH"
    reason: str = ""
    max_mintable: int = 1

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
    def __init__(self, timeout_seconds: float = 10.0, api_key: str = "") -> None:
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None
        self._rest_client: httpx.AsyncClient | None = None
        self._rest_rate_limited_until: float = 0.0
        self._wallet_clients: dict[str, httpx.AsyncClient] = {}  # address -> isolated httpx.AsyncClient
        self._wallet_sessions: dict[str, dict[str, str]] = {}  # address -> cookies/headers
        self._wallet_tokens: dict[str, str] = {}  # address -> access_token
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}  # slug -> heartbeat task
        self._authenticated_wallets: set[str] = set()  # address -> authenticated
        self._wallet_stage_elig_cache: dict[tuple[str, str], dict[str, WalletEligibility]] = {}

    def _get_headers(self, wallet_address: str | None = None, slug: str | None = None) -> dict[str, str]:
        """Construct standard OpenSea API headers including auth token and API key if available."""
        headers = {
            "accept": "application/json",
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/collection/{slug}" if slug else f"{SITE_URL}/",
            "x-app-id": APP_ID,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        if wallet_address:
            token = self._wallet_tokens.get(wallet_address.lower())
            if token:
                headers["authorization"] = f"Bearer {token}"
        return headers

    async def get_client(self) -> httpx.AsyncClient:
        """General unauthenticated client for public collection metadata."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"},
                limits=httpx.Limits(max_connections=30, max_keepalive_connections=30, keepalive_expiry=120.0),
                http2=True,
                follow_redirects=True,
            )
        return self._client

    async def get_wallet_client(self, wallet_address: str) -> httpx.AsyncClient:
        """Dedicated client with isolated cookie jar for a specific wallet."""
        addr = wallet_address.lower()
        cl = self._wallet_clients.get(addr)
        if cl is None or cl.is_closed:
            cl = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"},
                limits=httpx.Limits(max_connections=15, max_keepalive_connections=15, keepalive_expiry=120.0),
                http2=True,
                follow_redirects=True,
            )
            self._wallet_clients[addr] = cl
        return cl

    async def get_rest_client(self) -> httpx.AsyncClient:
        """Dedicated HTTP/2 pool for OpenSea Developer REST API v2 (api.opensea.io)."""
        if self._rest_client is None or self._rest_client.is_closed:
            self._rest_client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers={"accept": "application/json", "user-agent": "OpenSeaMintBot/2.0"},
                limits=httpx.Limits(max_connections=25, max_keepalive_connections=25, keepalive_expiry=120.0),
                http2=True,
                follow_redirects=True,
            )
        return self._rest_client

    async def warmup_rest(self) -> None:
        """Pre-warm TLS/HTTP2 socket to api.opensea.io without consuming rate-limit quota."""
        if not self.api_key:
            return
        try:
            rest_cl = await self.get_rest_client()
            await rest_cl.head(
                f"{OPENSEA_API_V2_URL}/",
                headers={"x-api-key": self.api_key, "accept": "application/json"},
            )
        except Exception:
            pass

    async def close(self) -> None:
        self.stop_heartbeat()
        self._authenticated_wallets.clear()
        self._wallet_tokens.clear()
        self._wallet_stage_elig_cache.clear()
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        if self._rest_client and not self._rest_client.is_closed:
            await self._rest_client.aclose()
        for cl in self._wallet_clients.values():
            if not cl.is_closed:
                await cl.aclose()
        self._wallet_clients.clear()

    def clear_eligibility_cache(self, slug: str | None = None) -> None:
        """Clear cached stage eligibility so subsequent stages query fresh real-time minted counts."""
        if slug:
            self._wallet_stage_elig_cache = {
                k: v for k, v in self._wallet_stage_elig_cache.items() if k[0] != slug
            }
        else:
            self._wallet_stage_elig_cache.clear()

    def start_heartbeat(self, slug: str, wallets: list[WalletAccount], interval_seconds: int = 25) -> None:
        """Start background session keepalive to prevent timeouts during long waits."""
        self.stop_heartbeat(slug)
        task = asyncio.create_task(self._heartbeat_loop(slug, wallets, interval_seconds))
        self._heartbeat_tasks[slug] = task
        self._heartbeat_task = task

    def stop_heartbeat(self, slug: str | None = None) -> None:
        if slug is not None:
            task = self._heartbeat_tasks.pop(slug, None)
            if task and not task.done():
                task.cancel()
            if self._heartbeat_task == task:
                self._heartbeat_task = None
        else:
            for task in self._heartbeat_tasks.values():
                if not task.done():
                    task.cancel()
            self._heartbeat_tasks.clear()
            if self._heartbeat_task and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def _heartbeat_loop(self, slug: str, wallets: list[WalletAccount], interval_seconds: int) -> None:
        """Keeps HTTP/2 connection and OpenSea SIWE auth tokens alive and boiling hot!"""
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                # Ping GraphQL lightly on general client
                client = await self.get_client()
                tasks = [
                    client.post(
                        GRAPHQL_URL,
                        json={"query": "query KeepAlive { __typename }", "variables": {}},
                        headers=self._get_headers(slug=slug),
                    )
                ]
                # Also pulse each wallet client to keep their HTTP/2 multiplexed streams alive!
                for w in wallets:
                    w_cl = await self.get_wallet_client(w.address)
                    tasks.append(
                        w_cl.post(
                            GRAPHQL_URL,
                            json={"query": "query KeepAlive { __typename }", "variables": {}},
                            headers=self._get_headers(w.address, slug=slug),
                        )
                    )
                if self.api_key:
                    tasks.append(self.warmup_rest())
                await asyncio.gather(*tasks, return_exceptions=True)
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
        headers = self._get_headers()
        chains_to_try = ["ROBINHOOD", "INK", "SONEIUM", "ELYSIUM", "BASE", "ARBITRUM", "ETHEREUM", "POLYGON", "OPTIMISM", "ZORA"]
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
            price_symbol="HYPE" if chain_id == 99901 else "ETH",
            price_unit=mint_price_wei / 1e18,
        )

        return CollectionInfo(
            name=name,
            slug=contract_address.lower(),
            contract_address=checksummed,
            image_url="",
            chain_identifier="robinhood" if chain_id == 4663 else ("ink" if chain_id == 57073 else ("soneium" if chain_id == 1868 else ("elysium" if chain_id == 99901 else ("ethereum" if chain_id == 1 else "evm")))),
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
        headers = self._get_headers(slug=slug)
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
        """Authenticate wallet using SIWE on OpenSea with its isolated client."""
        addr_lower = wallet.address.lower()
        if addr_lower in self._authenticated_wallets and addr_lower in self._wallet_tokens:
            return

        client = await self.get_wallet_client(wallet.address)
        headers = {
            "accept": "application/json",
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/collection/{slug}",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key

        # 1. Fetch nonce via POST
        nonce_resp = await client.post(f"{SITE_URL}/__api/auth/siwe/nonce", headers=headers)
        nonce_resp.raise_for_status()
        nonce_data = nonce_resp.json()
        nonce = nonce_data.get("nonce") if isinstance(nonce_data, dict) else str(nonce_data)

        # 2. Construct SIWE message strictly per OpenSea requirements
        domain = "opensea.io"
        issued_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        address = wallet.address

        message = (
            f"{domain} wants you to sign in with your Ethereum account:\n{address}\n\n"
            f"{SIWE_STATEMENT}\n\nURI: {SITE_URL}\nVersion: 1\nChain ID: {chain_id}\n"
            f"Nonce: {nonce}\nIssued At: {issued_at}"
        )

        signature = wallet.sign_personal_message(message.encode("utf-8"))

        # 3. Verify
        verify_body = {
            "message": {
                "domain": domain,
                "address": address,
                "statement": SIWE_STATEMENT,
                "uri": SITE_URL,
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
            self._authenticated_wallets.add(addr_lower)
            for cookie in client.cookies.jar:
                if cookie.name == "access_token":
                    self._wallet_tokens[addr_lower] = cookie.value
                    break

    async def authenticate_all_wallets(self, wallets: list[WalletAccount], chain_id: int, slug: str) -> None:
        """Authenticate multiple wallets concurrently."""
        tasks = [self.authenticate_wallet(w, chain_id, slug) for w in wallets]
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _build_mint_action(
        to_raw: str | None,
        data_raw: str,
        value_raw: Any,
        quantity: int,
        attempt: int,
    ) -> MintAction | None:
        if not isinstance(data_raw, str):
            return None
        hex_body = data_raw[2:] if data_raw.startswith(("0x", "0X")) else data_raw
        if not hex_body or (len(hex_body) % 2 != 0):
            return None
        try:
            calldata = bytes.fromhex(hex_body)
        except ValueError:
            return None

        target = to_checksum_address(to_raw or OPENSEA_SEADROP_ADDRESS)
        if isinstance(value_raw, str):
            val = int(value_raw, 16) if value_raw.startswith(("0x", "0X")) else int(value_raw or "0")
        else:
            val = int(value_raw or 0)
        return MintAction(target=target, value=val, calldata=calldata, quantity=quantity, attempts=attempt)

    async def _query_rest_mint_action_once(
        self,
        collection: CollectionInfo,
        wallet: WalletAccount,
        stage: DropStage,
        current_q: int,
        attempt: int,
    ) -> tuple[str, Any, float]:
        """Try official OpenSea API v2 drop mint endpoint; never blocks GQL fallback."""
        try:
            rest_cl = await self.get_rest_client()
            resp = await rest_cl.post(
                f"{OPENSEA_API_V2_URL}/drops/{collection.slug}/mint",
                json={
                    "minter": wallet.address,
                    "quantity": current_q,
                    "skip_balance_check": True,
                },
                headers={
                    "accept": "application/json",
                    "content-type": "application/json",
                    "x-api-key": self.api_key,
                },
            )
            if resp.status_code == 200:
                payload = resp.json()
                tx_obj = payload.get("transaction") if isinstance(payload.get("transaction"), dict) else payload
                if isinstance(tx_obj, dict):
                    act = self._build_mint_action(
                        tx_obj.get("to"),
                        tx_obj.get("data", ""),
                        tx_obj.get("value", 0),
                        current_q,
                        attempt,
                    )
                    if act is not None:
                        return ("ok", act, 0.0)
                return ("unavailable", RuntimeError("Empty calldata from OpenSea REST API"), 0.0)

            if resp.status_code == 429:
                retry_hdr = resp.headers.get("retry-after")
                cooldown = 15.0
                if retry_hdr:
                    try:
                        cooldown = min(60.0, max(2.0, float(retry_hdr)))
                    except ValueError:
                        cooldown = 15.0
                self._rest_rate_limited_until = time.time() + cooldown
                return ("unavailable", RuntimeError("OpenSea REST API 429 rate limited"), 0.0)

            if resp.status_code in (401, 403):
                self._rest_rate_limited_until = time.time() + 300.0
                return ("unavailable", RuntimeError(f"OpenSea REST API HTTP {resp.status_code}"), 0.0)

            if resp.status_code == 422:
                body_lower = resp.text.lower()
                if any(k in body_lower for k in ("exceed", "allocation", "quantity", "maximum", "supply")):
                    if current_q > 1:
                        return ("decrement", RuntimeError("REST allocation exceeded"), 0.0)
                return ("unavailable", RuntimeError("REST 422"), 0.0)

            return ("unavailable", RuntimeError(f"OpenSea REST HTTP {resp.status_code}"), 0.0)
        except Exception as ex:
            return ("unavailable", ex, 0.0)

    async def _query_gql_mint_action_once(
        self,
        client: httpx.AsyncClient,
        stage: DropStage,
        variables: dict[str, Any],
        headers: dict[str, str],
        attempt: int,
    ) -> tuple[str, Any, float]:
        """Execute one attempt against OpenSea GraphQL (os2-web) MintActionTimelineQuery."""
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

            if resp.status_code == 429:
                retry_after_hdr = resp.headers.get("retry-after")
                if retry_after_hdr:
                    try:
                        delay = min(1.0, max(0.05, float(retry_after_hdr)))
                    except ValueError:
                        delay = 0.08 + random.uniform(0.01, 0.02)
                else:
                    delay = min(1.0, 0.06 * (1.4 ** attempt) + random.uniform(0.01, 0.02))
                return ("retry", RuntimeError(f"OpenSea: Rate Limited (HTTP 429), backing off {delay:.2f}s"), delay)

            if resp.status_code != 200:
                delay = min(1.0, 0.06 * (1.3 ** attempt) + random.uniform(0.01, 0.02))
                return ("retry", RuntimeError(f"OpenSea HTTP {resp.status_code}"), delay)

            resp_json = resp.json()
            errors = resp_json.get("errors") or []
            last_err: Exception | None = None
            if errors:
                err_msgs = [e.get("message", "") for e in errors if isinstance(e, dict)]
                err_text = ", ".join(err_msgs)

                if "too many requests" in err_text.lower() or "rate limit" in err_text.lower() or "throttled" in err_text.lower():
                    delay = min(1.0, 0.06 * (1.4 ** attempt) + random.uniform(0.01, 0.02))
                    return ("retry", RuntimeError(f"OpenSea: Rate Limited ({err_text}), backing off {delay:.2f}s"), delay)

                if "can not perform trading operations" in err_text.lower() or "cannot perform trading operations" in err_text.lower():
                    return ("fatal_elig", RuntimeError("Wallet is not on the Allowlist / Not eligible for this private stage"), 0.0)

                last_err = RuntimeError(f"OpenSea: {err_text}")

            data = resp_json.get("data")
            if data and isinstance(data, dict):
                swap = data.get("swap") or {}
                actions = swap.get("actions", [])
                if actions:
                    sub_data = actions[0].get("transactionSubmissionData", {})
                    if sub_data and "data" in sub_data:
                        actual_qty = int(variables["toAssets"][0]["quantity"])
                        act = self._build_mint_action(
                            sub_data.get("to"),
                            sub_data["data"],
                            sub_data.get("value", 0),
                            actual_qty,
                            attempt,
                        )
                        if act is not None:
                            return ("ok", act, 0.0)

                swap_errors = swap.get("errors", [])
                if swap_errors:
                    err_names = [e.get("__typename", "") for e in swap_errors if isinstance(e, dict)]
                    is_not_started = (
                        "MinterNotEligibleForActiveDropStageError" in err_names
                        or "DropStageNotStartedError" in err_names
                        or "DropStageInactiveError" in err_names
                        or "DropNotStartedError" in err_names
                        or "StageNotActiveError" in err_names
                    )
                    if is_not_started:
                        now_ts = time.time()
                        if stage.start_timestamp > 0 and now_ts < (stage.start_timestamp + 8.0):
                            # Instant retry if clock has reached start_timestamp, minimal sleep otherwise
                            if now_ts >= stage.start_timestamp:
                                poll_delay = 0.001
                            else:
                                poll_delay = min(0.010, max(0.001, stage.start_timestamp - now_ts))
                            return ("retry", RuntimeError("Waiting for OpenSea stage activation..."), poll_delay)
                        return ("fatal", RuntimeError("Wallet is not on the Allowlist for this stage"), 0.0)

                    if "InsufficientMintsRemainingError" in err_names or "MintQuantityMoreThanAllocatedForMinterError" in err_names:
                        current_q = int(variables["toAssets"][0]["quantity"])
                        if current_q > 1:
                            return ("decrement", RuntimeError("Quantity exceeds allocation"), 0.0)
                        now_ts = time.time()
                        is_transition_window = stage.start_timestamp > 0 and now_ts < (stage.start_timestamp + 8.0)
                        if is_transition_window and attempt < 5:
                            return ("retry", RuntimeError("Waiting for OpenSea stage allocation unlock..."), 0.015)
                        return ("fatal", RuntimeError("Stage supply pool exhausted / allocation limit reached on OpenSea"), 0.0)

                    last_err = RuntimeError(f"OpenSea: {', '.join(err_names)}")

            return ("fallback_retry", last_err or RuntimeError("Empty OpenSea GraphQL response"), 0.0)
        except Exception as e:
            return ("exception", e, 0.0)

    async def get_mint_action_hot(
        self,
        collection: CollectionInfo,
        wallet: WalletAccount,
        quantity: int,
        stage: DropStage,
        max_attempts: int = 30,
        retry_interval_ms: int = 200,
        single_attempt: bool = False,
    ) -> MintAction:
        """Fetch private/allowlist mint calldata using Hybrid REST v2 + GraphQL race with adaptive retry."""
        client = await self.get_wallet_client(wallet.address)
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

        headers = self._get_headers(wallet.address, collection.slug)

        last_err: Exception | None = None
        attempts = 1 if single_attempt else max_attempts

        async def _query_once(att: int) -> tuple[str, Any, float]:
            cur_q = int(variables["toAssets"][0]["quantity"])
            use_rest = bool(self.api_key) and time.time() >= self._rest_rate_limited_until

            if use_rest:
                gql_task = asyncio.create_task(
                    self._query_gql_mint_action_once(client, stage, variables, headers, att)
                )
                rest_task = asyncio.create_task(
                    self._query_rest_mint_action_once(collection, wallet, stage, cur_q, att)
                )
                done, pending = await asyncio.wait({gql_task, rest_task}, return_when=asyncio.FIRST_COMPLETED)

                gql_res: tuple[str, Any, float] | None = None
                rest_res: tuple[str, Any, float] | None = None

                for t in done:
                    res = t.result()
                    if res[0] == "ok":
                        for pt in pending:
                            pt.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                        return res
                    if t is gql_task:
                        gql_res = res
                    else:
                        rest_res = res

                if pending:
                    rem_results = await asyncio.gather(*pending, return_exceptions=True)
                    for pt, res in zip(pending, rem_results):
                        if isinstance(res, tuple) and res[0] == "ok":
                            return res
                        if pt is gql_task and isinstance(res, tuple):
                            gql_res = res
                        elif pt is rest_task and isinstance(res, tuple):
                            rest_res = res

                status, payload, delay = gql_res or ("fallback_retry", RuntimeError("OpenSea unavailable"), 0.0)
                if status != "decrement" and rest_res and rest_res[0] == "decrement" and cur_q > 1:
                    status = "decrement"
                return (status, payload, delay)
            else:
                return await self._query_gql_mint_action_once(
                    client, stage, variables, headers, att
                )

        attempt = 1
        while attempt <= attempts:
            current_q = int(variables["toAssets"][0]["quantity"])
            now_ts = time.time()
            # Speculative Staggered Hedging at T-0:
            # If attempt 1 right around drop activation takes >55ms, launch attempt 2 concurrently!
            is_at_t0_window = (
                not single_attempt
                and attempt == 1
                and attempts >= 2
                and stage.start_timestamp > 0
                and now_ts < (stage.start_timestamp + 1.5)
            )

            if is_at_t0_window:
                task1 = asyncio.create_task(_query_once(1))
                done, _ = await asyncio.wait({task1}, timeout=0.055)
                if task1 in done:
                    status, payload, delay = task1.result()
                else:
                    # Task1 still in-flight after 55ms -> fire concurrent hedged probe Task2!
                    task2 = asyncio.create_task(_query_once(2))
                    d2, p2 = await asyncio.wait({task1, task2}, return_when=asyncio.FIRST_COMPLETED)
                    first_res = None
                    for t in d2:
                        res = t.result()
                        if res[0] == "ok":
                            for pt in p2:
                                pt.cancel()
                            if p2:
                                await asyncio.gather(*p2, return_exceptions=True)
                            return res[1]
                        first_res = res
                    # If neither was "ok", wait remaining
                    if p2:
                        rem_res = await asyncio.gather(*p2, return_exceptions=True)
                        for r_item in rem_res:
                            if isinstance(r_item, tuple) and r_item[0] == "ok":
                                return r_item[1]
                            if isinstance(r_item, tuple):
                                first_res = r_item
                    status, payload, delay = first_res or ("fallback_retry", RuntimeError("OpenSea unavailable"), 0.0)
                    attempt = 2  # accounted for both tasks
            else:
                status, payload, delay = await _query_once(attempt)

            if status == "ok":
                return payload

            last_err = payload if isinstance(payload, Exception) else RuntimeError(str(payload))
            if single_attempt:
                raise last_err

            if status == "decrement":
                if current_q > 1:
                    variables["toAssets"][0]["quantity"] = str(current_q - 1)
                    attempt += 1
                    continue
                raise last_err

            if status == "fatal":
                raise last_err

            if status == "fatal_elig":
                now_ts = time.time()
                is_transition_window = stage.start_timestamp > 0 and now_ts < (stage.start_timestamp + 8.0)
                if not is_transition_window:
                    raise last_err

            if status == "exception":
                now_ts = time.time()
                is_transition_window = stage.start_timestamp > 0 and now_ts < (stage.start_timestamp + 8.0)
                err_str = str(last_err).lower()
                if "supply pool exhausted" in err_str or "allocation limit reached" in err_str:
                    if not is_transition_window or attempt >= 5:
                        raise last_err
                if not is_transition_window:
                    if "not on the allowlist" in err_str or "not eligible" in err_str:
                        raise last_err

            if status == "retry" and delay > 0:
                await asyncio.sleep(delay)
                attempt += 1
                continue

            if attempt < attempts:
                await asyncio.sleep(retry_interval_ms / 1000.0)
            attempt += 1

        raise RuntimeError(f"OpenSea mint calldata unavailable: {last_err}")

    async def check_wallet_eligibility_all_stages(
        self,
        collection: CollectionInfo,
        wallet: WalletAccount,
        force_refresh: bool = False,
        chain_client: Any = None,
    ) -> dict[str, WalletEligibility]:
        """
        Verify wallet eligibility across ALL drop stages in a single query.
        Returns a dict mapping stage.uuid -> WalletEligibility.
        """
        cache_key = (collection.slug, wallet.address.lower())
        if not force_refresh and cache_key in self._wallet_stage_elig_cache:
            return self._wallet_stage_elig_cache[cache_key]

        results: dict[str, WalletEligibility] = {}

        # Check on-chain ground truth balance for this wallet on this collection contract
        on_chain_balance = 0
        if chain_client and getattr(collection, "contract_address", ""):
            try:
                addr_padded = wallet.address[2:].lower().rjust(64, "0")
                call_data = "0x70a08231" + addr_padded
                bal_hex = await chain_client.call(collection.contract_address, call_data)
                if bal_hex and bal_hex != "0x":
                    on_chain_balance = int(bal_hex, 16)
            except Exception:
                on_chain_balance = 0

        # 1. Pre-fill any public stages
        for st in collection.stages:
            if st.is_public:
                max_w = st.max_per_wallet or 1
                rem = max(0, max_w - on_chain_balance) if on_chain_balance > 0 else max_w
                results[st.uuid] = WalletEligibility(
                    is_eligible=(rem > 0),
                    price_wei=st.price_wei,
                    price_unit=st.price_unit,
                    price_symbol=st.price_symbol,
                    reason=f"Eligible ({rem} Left)" if rem > 0 else f"Already minted maximum allocation ({on_chain_balance}/{max_w})",
                    max_mintable=rem,
                )

        # Ensure wallet has an active SIWE session (cached, runs at most once per session)
        try:
            await self.authenticate_wallet(wallet, collection.chain_id, collection.slug)
        except Exception:
            pass

        client = await self.get_wallet_client(wallet.address)
        headers = self._get_headers(wallet.address, collection.slug)

        # Query DropEligibilityQuery
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
                stages_list = d.get("stages") or []
                minter_qty_minted = int(d.get("minterQuantityMinted") or 0)

                # Build fallback price mapping per stage uuid or index
                top_stage_prices: dict[str, tuple[float, str, int]] = {}
                for st in stages_list:
                    u = st.get("uuid", "")
                    el_p = (st.get("eligiblePrice") or {}).get("token") or {}
                    if el_p.get("unit") is not None:
                        unit = float(el_p.get("unit") or 0.0)
                        sym = el_p.get("symbol") or "ETH"
                        wei = int(round(unit * 1e18))
                        top_stage_prices[u] = (unit, sym, wei)

                # Process and cross-populate cache for ALL wallets returned by OpenSea in this query!
                for w_info in wallets_list:
                    w_addr_lower = w_info.get("address", "").lower()
                    w_qty_minted = max(int(w_info.get("quantityMinted") or 0), minter_qty_minted)
                    if w_addr_lower == wallet.address.lower() and on_chain_balance > 0:
                        w_qty_minted = max(w_qty_minted, on_chain_balance)

                    w_stage_results: dict[str, WalletEligibility] = {}

                    for st in collection.stages:
                        if st.is_public:
                            max_w = st.max_per_wallet or 1
                            rem = max(0, max_w - w_qty_minted) if w_qty_minted > 0 else max_w
                            w_stage_results[st.uuid] = WalletEligibility(
                                is_eligible=(rem > 0),
                                price_wei=st.price_wei,
                                price_unit=st.price_unit,
                                price_symbol=st.price_symbol,
                                reason=f"Eligible ({rem} Left)" if rem > 0 else f"Already minted maximum allocation ({w_qty_minted}/{max_w})",
                                max_mintable=rem,
                            )

                    for st_info in w_info.get("stages", []):
                        u = st_info.get("dropStageUuid", "")
                        matching_stage = next((s for s in collection.stages if s.uuid == u), None)
                        fb_unit = matching_stage.price_unit if matching_stage else 0.0
                        fb_sym = matching_stage.price_symbol if matching_stage else "ETH"
                        fb_wei = matching_stage.price_wei if matching_stage else 0
                        if u in top_stage_prices:
                            fb_unit, fb_sym, fb_wei = top_stage_prices[u]

                        max_total = st_info.get("maxTotalMintableByWallet")
                        if max_total is None:
                            top_st = next((s for s in stages_list if s.get("uuid") == u), None)
                            if top_st:
                                max_total = top_st.get("eligibleMaxTotalMintableByWallet") or top_st.get("maxTotalMintableByWallet")
                        if max_total is None and matching_stage:
                            max_total = matching_stage.max_per_wallet
                        max_total_int = int(max_total or 1)
                        remaining_mintable = max(0, max_total_int - w_qty_minted)

                        if st_info.get("isEligible") is True:
                            if remaining_mintable <= 0:
                                w_stage_results[u] = WalletEligibility(
                                    is_eligible=False,
                                    price_wei=0,
                                    price_unit=0.0,
                                    price_symbol="ETH",
                                    reason=f"Already minted maximum allocation ({w_qty_minted}/{max_total_int})",
                                    max_mintable=0,
                                )
                                continue

                            p_tok = (st_info.get("price") or {}).get("token") or {}
                            if p_tok.get("unit") is not None:
                                p_unit = float(p_tok.get("unit") or 0.0)
                                p_sym = p_tok.get("symbol") or "ETH"
                                p_wei = int(round(p_unit * 1e18))
                            else:
                                p_unit, p_sym, p_wei = fb_unit, fb_sym, fb_wei
                            w_stage_results[u] = WalletEligibility(
                                is_eligible=True,
                                price_wei=p_wei,
                                price_unit=p_unit,
                                price_symbol=p_sym,
                                reason=f"Eligible ({remaining_mintable} Left)",
                                max_mintable=remaining_mintable,
                            )
                        elif st_info.get("isEligible") is False:
                            w_stage_results[u] = WalletEligibility(
                                is_eligible=False,
                                price_wei=0,
                                price_unit=0.0,
                                price_symbol="ETH",
                                reason="Not on Allowlist",
                                max_mintable=0,
                            )

                    for st in collection.stages:
                        if st.uuid not in w_stage_results:
                            w_stage_results[st.uuid] = WalletEligibility(
                                is_eligible=False,
                                price_wei=0,
                                price_unit=0.0,
                                price_symbol="ETH",
                                reason="Not on Allowlist",
                            )

                    if w_addr_lower:
                        self._wallet_stage_elig_cache[(collection.slug, w_addr_lower)] = w_stage_results

                if cache_key in self._wallet_stage_elig_cache:
                    results = self._wallet_stage_elig_cache[cache_key]

                # Fallback to top-level stages array if any stage was not in wallets_list
                for st in stages_list:
                    u = st.get("uuid", "")
                    if u not in results:
                        el_minter = (st.get("eligibleMinterAddress") or "").lower()
                        matching_stage = next((s for s in collection.stages if s.uuid == u), None)
                        fb_unit = matching_stage.price_unit if matching_stage else 0.0
                        fb_sym = matching_stage.price_symbol if matching_stage else "ETH"
                        fb_wei = matching_stage.price_wei if matching_stage else 0
                        top_max = st.get("eligibleMaxTotalMintableByWallet") or st.get("maxTotalMintableByWallet") or (matching_stage.max_per_wallet if matching_stage else 1)
                        top_max_int = int(top_max or 1)
                        rem = max(0, top_max_int - on_chain_balance) if on_chain_balance > 0 else top_max_int

                        if (not el_minter or el_minter == wallet.address.lower()) and st.get("isEligible") is True:
                            if rem <= 0:
                                results[u] = WalletEligibility(
                                    is_eligible=False,
                                    price_wei=0,
                                    price_unit=0.0,
                                    price_symbol="ETH",
                                    reason=f"Already minted maximum allocation ({on_chain_balance}/{top_max_int})",
                                    max_mintable=0,
                                )
                                continue

                            el_p = (st.get("eligiblePrice") or {}).get("token") or {}
                            if el_p.get("unit") is not None:
                                p_unit = float(el_p.get("unit") or 0.0)
                                p_sym = el_p.get("symbol") or "ETH"
                                p_wei = int(round(p_unit * 1e18))
                            else:
                                p_unit, p_sym, p_wei = fb_unit, fb_sym, fb_wei
                            results[u] = WalletEligibility(
                                is_eligible=True,
                                price_wei=p_wei,
                                price_unit=p_unit,
                                price_symbol=p_sym,
                                reason=f"Eligible ({rem} Left)",
                                max_mintable=rem,
                            )
                        elif not el_minter and st.get("isEligible") is True and not wallets_list:
                            if rem <= 0:
                                results[u] = WalletEligibility(
                                    is_eligible=False,
                                    price_wei=0,
                                    price_unit=0.0,
                                    price_symbol="ETH",
                                    reason=f"Already minted maximum allocation ({on_chain_balance}/{top_max_int})",
                                    max_mintable=0,
                                )
                                continue

                            el_p = (st.get("eligiblePrice") or {}).get("token") or {}
                            if el_p.get("unit") is not None:
                                p_unit = float(el_p.get("unit") or 0.0)
                                p_sym = el_p.get("symbol") or "ETH"
                                p_wei = int(round(p_unit * 1e18))
                            else:
                                p_unit, p_sym, p_wei = fb_unit, fb_sym, fb_wei
                            results[u] = WalletEligibility(
                                is_eligible=True,
                                price_wei=p_wei,
                                price_unit=p_unit,
                                price_symbol=p_sym,
                                reason=f"Eligible ({rem} Left)",
                                max_mintable=rem,
                            )
        except Exception:
            pass

        # Fill any remaining stages as not eligible
        for st in collection.stages:
            if st.uuid not in results:
                results[st.uuid] = WalletEligibility(
                    is_eligible=False,
                    price_wei=0,
                    price_unit=0.0,
                    price_symbol="ETH",
                    reason="Not on Allowlist",
                )

        self._wallet_stage_elig_cache[cache_key] = results
        return results

    async def check_wallet_eligibility(
        self,
        collection: CollectionInfo,
        stage: DropStage,
        wallet: WalletAccount,
        force_refresh: bool = False,
        chain_client: Any = None,
    ) -> WalletEligibility:
        """
        Verify if a wallet is eligible to mint in the given drop stage,
        and retrieve the exact per-wallet mint price.
        Returns WalletEligibility.
        """
        if stage.is_public:
            on_chain_balance = 0
            if chain_client and getattr(collection, "contract_address", ""):
                try:
                    addr_padded = wallet.address[2:].lower().rjust(64, "0")
                    call_data = "0x70a08231" + addr_padded
                    bal_hex = await chain_client.call(collection.contract_address, call_data)
                    if bal_hex and bal_hex != "0x":
                        on_chain_balance = int(bal_hex, 16)
                except Exception:
                    on_chain_balance = 0
            max_w = stage.max_per_wallet or 1
            rem = max(0, max_w - on_chain_balance) if on_chain_balance > 0 else max_w
            return WalletEligibility(
                is_eligible=(rem > 0),
                price_wei=stage.price_wei,
                price_unit=stage.price_unit,
                price_symbol=stage.price_symbol,
                reason=f"Eligible ({rem} Left)" if rem > 0 else f"Already minted maximum allocation ({on_chain_balance}/{max_w})",
                max_mintable=rem,
            )

        all_eligs = await self.check_wallet_eligibility_all_stages(
            collection, wallet, force_refresh=force_refresh, chain_client=chain_client
        )
        if stage.uuid in all_eligs:
            return all_eligs[stage.uuid]

        # Direct simulation fallback (ONLY if stage is live!)
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
                    el = WalletEligibility(
                        is_eligible=True,
                        price_wei=p_wei,
                        price_unit=p_unit,
                        price_symbol="ETH",
                        reason="Eligible (Calldata Ready)",
                    )
                    all_eligs[stage.uuid] = el
                    return el
            except Exception as e:
                err_str = str(e).lower()
                if "not on the allowlist" in err_str or "not eligible" in err_str or "trading operations" in err_str:
                    el = WalletEligibility(
                        is_eligible=False,
                        price_wei=0,
                        price_unit=0.0,
                        price_symbol="ETH",
                        reason="Not on Allowlist",
                    )
                    all_eligs[stage.uuid] = el
                    return el

        return WalletEligibility(
            is_eligible=False,
            price_wei=0,
            price_unit=0.0,
            price_symbol="ETH",
            reason="Not on Allowlist",
        )


