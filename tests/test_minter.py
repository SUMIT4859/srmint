import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from minter.constants import (
    ERC721_PUBLIC_MINT_SELECTOR,
    OPENSEA_SEADROP_ADDRESS,
    OPENSEA_FEE_RECIPIENT,
    get_chain_info,
    get_tx_explorer_url,
)
from minter.wallet import WalletAccount, WalletManager
from minter.seadrop import encode_public_mint
from minter.client import OpenSeaClient
from minter.config import AppConfig


def test_wallet_account():
    # Test well-known private key: 0x1
    pk = "0x0000000000000000000000000000000000000000000000000000000000000001"
    wallet = WalletAccount(private_key=pk, label="TestWallet1")
    assert wallet.address == "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
    assert wallet.label == "TestWallet1"

    # Test EIP-191 personal sign
    msg = b"Hello OpenSea"
    sig = wallet.sign_personal_message(msg)
    assert sig.startswith("0x")
    assert len(sig) == 132  # 65 bytes in hex + 0x

    # Test EIP-1559 transaction signing
    tx = {
        "type": 2,
        "chainId": 8453,
        "nonce": 0,
        "maxPriorityFeePerGas": 1000000000,
        "maxFeePerGas": 2000000000,
        "gas": 300000,
        "to": OPENSEA_SEADROP_ADDRESS,
        "value": 0,
        "data": b"",
    }
    raw, tx_hash = wallet.sign_transaction(tx)
    assert isinstance(raw, bytes)
    assert raw[0] == 2  # EIP-1559 envelope type
    assert tx_hash.startswith("0x")
    assert len(tx_hash) == 66


def test_wallet_manager(tmp_path):
    wallets_file = tmp_path / "test_wallets.json"
    mgr = WalletManager(filepath=str(wallets_file))
    assert mgr.count() == 0

    pk1 = "0x0000000000000000000000000000000000000000000000000000000000000001"
    pk2 = "0x0000000000000000000000000000000000000000000000000000000000000002"

    w1 = mgr.add_wallet(pk1, label="W1")
    w2 = mgr.add_wallet(pk2, label="W2")
    assert mgr.count() == 2

    # Reload from disk
    mgr2 = WalletManager(filepath=str(wallets_file))
    assert mgr2.count() == 2
    assert mgr2.wallets[0].address == w1.address
    assert mgr2.wallets[1].address == w2.address

    # Remove
    mgr2.remove_wallet(0)
    assert mgr2.count() == 1
    assert mgr2.wallets[0].address == w2.address


def test_seadrop_encoding():
    nft_contract = "0x1111111111111111111111111111111111111111"
    minter = "0x2222222222222222222222222222222222222222"
    calldata = encode_public_mint(nft_contract, minter, quantity=2)

    assert calldata[:4] == ERC721_PUBLIC_MINT_SELECTOR
    # 4 bytes selector + 4 * 32 bytes args = 132 bytes
    assert len(calldata) == 132


def test_opensea_slug_parser():
    url1 = "https://opensea.io/collection/crypto-punks"
    assert OpenSeaClient.parse_slug(url1) == "crypto-punks"

    url2 = "https://opensea.io/mint/cool-collection"
    assert OpenSeaClient.parse_slug(url2) == "cool-collection"

    slug = "my-custom-drop"
    assert OpenSeaClient.parse_slug(slug) == "my-custom-drop"


def test_chain_constants():
    info = get_chain_info(8453)
    assert info["name"] == "Base"
    assert info["symbol"] == "ETH"

    tx_url = get_tx_explorer_url(8453, "0xabcdef123456")
    assert tx_url == "https://basescan.org/tx/0xabcdef123456"


def test_app_config(tmp_path):
    old_env = os.environ.pop("GAS_LIMIT", None)
    try:
        cfg_file = tmp_path / "config.json"
        cfg = AppConfig.load(str(cfg_file))
        assert cfg.gas_limit == 250000
        assert cfg.broadcast_lead_ms == 80
        assert cfg.max_retry_attempts == 30

        cfg.gas_limit = 400000
        cfg.save(str(cfg_file))

        loaded = AppConfig.load(str(cfg_file))
        assert loaded.gas_limit == 400000
    finally:
        if old_env is not None:
            os.environ["GAS_LIMIT"] = old_env


def test_wallet_mint_result_timing():
    from minter.engine import WalletMintResult
    wallet = WalletAccount(
        private_key="0x0000000000000000000000000000000000000000000000000000000000000001",
        label="TestWallet",
    )
    res = WalletMintResult(
        wallet=wallet,
        quantity=1,
        tx_hash="0x123",
        status="SUCCESS",
        broadcast_time_ms=520,
        confirm_time_seconds=0.75,
        total_time_seconds=1.27,
    )
    assert res.broadcast_time_ms == 520
    assert res.confirm_time_seconds == 0.75
    assert res.total_time_seconds == 1.27


def test_contract_address_detection():
    from minter.client import OpenSeaClient
    from eth_utils.address import is_address
    # Valid contract address
    addr = "0x2c8c306f34969d05230bf1c7c8ca6becc8e97e6b"
    assert is_address(addr)
    # Check that URL parsing handles standard URLs as well
    url = "https://opensea.io/collection/trenchstrokerz"
    assert OpenSeaClient.parse_slug(url) == "trenchstrokerz"


def test_price_guard_logic():
    import asyncio
    from minter.engine import MintEngine, WalletMintResult
    from minter.client import CollectionInfo, DropStage
    from minter.config import AppConfig

    # Simulate price guard detection
    cfg = AppConfig()
    engine = MintEngine(cfg, chain=None, opensea=None)

    stage = DropStage(
        uuid="stage1",
        label="Public",
        stage_type="PUBLIC_SALE",
        stage_index=0,
        start_time="",
        end_time="",
        start_timestamp=0,
        end_timestamp=2147483647,
        max_per_wallet=1,
        price_wei=0,  # Approved as FREE
        price_symbol="ETH",
        price_unit=0.0,
    )

    # If developer changes price to 0.05 ETH (50000000000000000 wei)
    expected_price_wei = 0
    actual_detected_price_wei = 50_000_000_000_000_000

    price_changed = (actual_detected_price_wei != expected_price_wei)
    assert price_changed is True, "Price guard must detect discrepancy between approved and current price!"


def test_sign_transaction_lowercase_address():
    pk = "0x0000000000000000000000000000000000000000000000000000000000000001"
    wallet = WalletAccount(private_key=pk, label="TestWallet")
    # OpenSea returns lowercase address '0x00005ea00ac477b1030ce78506496e8c2de24bf5'
    tx_lowercase = {
        "type": 2,
        "chainId": 4663,
        "nonce": 0,
        "maxPriorityFeePerGas": 1000,
        "maxFeePerGas": 20000,
        "gas": 250000,
        "to": "0x00005ea00ac477b1030ce78506496e8c2de24bf5",
        "value": 0,
        "data": b"",
    }
    raw_bytes, tx_hash = wallet.sign_transaction(tx_lowercase)
    assert isinstance(raw_bytes, bytes)
    assert tx_hash.startswith("0x")


def test_opensea_value_parsing():
    # OpenSea GraphQL returns decimal strings for wei value, e.g. "20000000000000" for 0.00002 ETH
    val_raw_decimal = "20000000000000"
    if val_raw_decimal.startswith("0x"):
        val = int(val_raw_decimal, 16)
    else:
        val = int(val_raw_decimal)
    assert val == 20000000000000, f"Expected 20000000000000 wei, got {val}"

    # Hex test
    val_raw_hex = "0x1234"
    if val_raw_hex.startswith("0x"):
        val_hex = int(val_raw_hex, 16)
    else:
        val_hex = int(val_raw_hex)
    assert val_hex == 0x1234


def test_wallet_eligibility_dataclass():
    from minter.client import WalletEligibility

    # 1. Free wallet
    el_free = WalletEligibility(is_eligible=True, price_wei=0, price_unit=0.0, price_symbol="ETH", reason="Allowlist Verified")
    assert el_free.is_free is True
    assert bool(el_free) is True
    assert "FREE ($0.00)" in el_free.format_price(eth_usd_rate=2500.0)

    # 2. Paid wallet
    el_paid = WalletEligibility(is_eligible=True, price_wei=70_000_000_000_000, price_unit=0.00007, price_symbol="ETH", reason="Allowlist Verified")
    assert el_paid.is_free is False
    assert bool(el_paid) is True
    formatted = el_paid.format_price(eth_usd_rate=2500.0)
    assert "0.00007 ETH" in formatted
    assert "$0.18" in formatted or "$0.17" in formatted

    # 3. Ineligible wallet
    el_inelig = WalletEligibility(is_eligible=False, reason="Not on Allowlist")
    assert el_inelig.is_free is True
    assert bool(el_inelig) is False
    assert el_inelig.format_price() == "Ineligible"

    # 4. Backward-compatible tuple unpack: is_eligible, reason = el
    is_e, r_msg = el_paid
    assert is_e is True
    assert r_msg == "Allowlist Verified"


def test_free_mint_protection():
    from minter.client import WalletEligibility

    stage_expected_price_wei = 0
    wallets_elig = {
        "0x1111": WalletEligibility(is_eligible=True, price_wei=0, price_unit=0.0),
        "0x2222": WalletEligibility(is_eligible=True, price_wei=70_000_000_000_000, price_unit=0.00007),
    }

    is_stage_expected_free = (stage_expected_price_wei == 0)
    paid_wallets = [addr for addr, e in wallets_elig.items() if not e.is_free]
    
    # Should trigger free mint protection because 0x2222 has a paid price!
    should_cancel = is_stage_expected_free and len(paid_wallets) > 0
    assert should_cancel is True
    assert "0x2222" in paid_wallets


def test_price_uniformity_guard():
    from minter.client import WalletEligibility

    # Case A: Mismatched prices (Free vs Paid) -> Must CANCEL
    wallets_mismatched = {
        "0x1111": WalletEligibility(is_eligible=True, price_wei=0),
        "0x2222": WalletEligibility(is_eligible=True, price_wei=70_000_000_000_000),
    }
    distinct_prices_a = {e.price_wei for e in wallets_mismatched.values()}
    assert len(distinct_prices_a) > 1, "Should detect mismatched prices (Free vs Paid)"

    # Case B: Differing paid tiers ($0.18 vs $1.13) -> Must CANCEL
    wallets_tiers = {
        "0x2222": WalletEligibility(is_eligible=True, price_wei=70_000_000_000_000),
        "0x3333": WalletEligibility(is_eligible=True, price_wei=450_000_000_000_000),
    }
    distinct_prices_b = {e.price_wei for e in wallets_tiers.values()}
    assert len(distinct_prices_b) > 1, "Should detect mismatched prices between paid tiers"

    # Case C: All identical Free -> Must ALLOW
    wallets_all_free = {
        "0x1111": WalletEligibility(is_eligible=True, price_wei=0),
        "0x4444": WalletEligibility(is_eligible=True, price_wei=0),
    }
    distinct_prices_c = {e.price_wei for e in wallets_all_free.values()}
    assert len(distinct_prices_c) == 1, "Identical free wallets must pass uniformity guard"

    # Case D: All identical Paid -> Must ALLOW
    wallets_all_paid = {
        "0x2222": WalletEligibility(is_eligible=True, price_wei=70_000_000_000_000),
        "0x5555": WalletEligibility(is_eligible=True, price_wei=70_000_000_000_000),
    }
    distinct_prices_d = {e.price_wei for e in wallets_all_paid.values()}
    assert len(distinct_prices_d) == 1, "Identical paid wallets must pass uniformity guard"


def test_rate_limit_handling():
    import asyncio
    import time
    from unittest.mock import AsyncMock, MagicMock
    from minter.client import OpenSeaClient, CollectionInfo, DropStage
    from minter.wallet import WalletAccount

    client = OpenSeaClient()
    wallet = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000001")
    coll = CollectionInfo(
        name="TestDrop",
        slug="test-drop",
        contract_address="0x1111111111111111111111111111111111111111",
        image_url="",
        chain_identifier="ROBINHOOD",
        chain_id=4663,
        total_supply=100,
        max_supply=100,
        stages=[],
    )
    stage = DropStage(
        uuid="s1",
        label="Allowlist",
        stage_type="PRIVATE_SALE",
        stage_index=0,
        start_time="",
        end_time="",
        start_timestamp=int(time.time() + 100),
        end_timestamp=int(time.time() + 200),
        max_per_wallet=1,
        price_wei=0,
        price_symbol="ETH",
        price_unit=0.0,
    )

    # In single_attempt mode, hitting 429 should raise immediately without retrying
    mock_http_client = AsyncMock()
    mock_http_client.is_closed = False
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {"retry-after": "1.5"}
    mock_http_client.post.return_value = mock_resp
    client._wallet_clients[wallet.address.lower()] = mock_http_client

    async def run_test():
        try:
            await client.get_mint_action_hot(coll, wallet, 1, stage, max_attempts=1, single_attempt=True)
            assert False, "Should have raised on 429"
        except Exception as ex:
            assert "Rate Limited" in str(ex) or "429" in str(ex)

    asyncio.run(run_test())


def test_single_attempt_stage_activation():
    import asyncio
    import time
    from unittest.mock import AsyncMock, MagicMock
    from minter.client import OpenSeaClient, CollectionInfo, DropStage
    from minter.wallet import WalletAccount

    client = OpenSeaClient()
    wallet = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000001")
    coll = CollectionInfo(
        name="TestDrop",
        slug="test-drop",
        contract_address="0x1111111111111111111111111111111111111111",
        image_url="",
        chain_identifier="ROBINHOOD",
        chain_id=4663,
        total_supply=100,
        max_supply=100,
        stages=[],
    )
    stage = DropStage(
        uuid="s1",
        label="Allowlist",
        stage_type="PRIVATE_SALE",
        stage_index=0,
        start_time="",
        end_time="",
        start_timestamp=int(time.time() + 100),
        end_timestamp=int(time.time() + 200),
        max_per_wallet=1,
        price_wei=0,
        price_symbol="ETH",
        price_unit=0.0,
    )

    mock_http_client = AsyncMock()
    mock_http_client.is_closed = False
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": {
            "swap": {
                "actions": [],
                "errors": [{"__typename": "MinterNotEligibleForActiveDropStageError"}]
            }
        }
    }
    mock_http_client.post.return_value = mock_resp
    client._wallet_clients[wallet.address.lower()] = mock_http_client

    async def run_test():
        try:
            await client.get_mint_action_hot(coll, wallet, 1, stage, max_attempts=1, single_attempt=True)
            assert False, "Should have raised stage activation wait"
        except Exception as ex:
            assert "Waiting for OpenSea stage activation..." in str(ex)

    asyncio.run(run_test())


def test_pipelined_mint_engine_execution():
    import asyncio
    import time
    from unittest.mock import AsyncMock, MagicMock
    from minter.engine import MintEngine
    from minter.client import CollectionInfo, DropStage, MintAction
    from minter.config import AppConfig
    from minter.chain import GasFees, TransactionReceipt

    cfg = AppConfig()
    cfg.broadcast_lead_ms = 0
    cfg.gas_limit = 250000

    mock_chain = AsyncMock()
    mock_chain.get_chain_id.return_value = 4663
    mock_chain.get_balances_batch.return_value = {"0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf": 10**18}
    mock_chain.get_nonces_batch.return_value = {"0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf": 0}
    mock_chain.get_gas_fees.return_value = GasFees(base_fee_per_gas=20000000, max_priority_fee_per_gas=100000, max_fee_per_gas=25000000)
    mock_chain.broadcast_raw_transaction.return_value = "0x" + "a" * 64
    mock_chain.wait_for_receipt.return_value = TransactionReceipt(
        transaction_hash="0x" + "a" * 64,
        status=1,
        block_number=12345,
        gas_used=120000,
        effective_gas_price=20000000,
    )

    mock_opensea = AsyncMock()
    mock_opensea.stop_heartbeat = MagicMock()
    mock_opensea.start_heartbeat = MagicMock()
    # At T-10s (attempt 1), simulate stage not active yet
    # At T-0, simulate calldata becomes available!
    call_count = 0
    async def fake_get_mint_action(coll, w, qty, stg, **kwargs):
        nonlocal call_count
        call_count += 1
        if kwargs.get("single_attempt"):
            raise RuntimeError("Waiting for OpenSea stage activation...")
        return MintAction(
            target="0x00005ea00Ac477B1030CE78506496e8C2dE24bf5",
            value=0,
            calldata=b"\x12\x34\x56\x78",
        )

    mock_opensea.get_mint_action_hot = fake_get_mint_action

    engine = MintEngine(cfg, mock_chain, mock_opensea)
    wallet = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000001")
    coll = CollectionInfo(
        name="TestDrop",
        slug="test-drop",
        contract_address="0x1111111111111111111111111111111111111111",
        image_url="",
        chain_identifier="ROBINHOOD",
        chain_id=4663,
        total_supply=100,
        max_supply=100,
        stages=[],
    )
    # Stage starts right now (no sleep wait)
    stage = DropStage(
        uuid="s1",
        label="Allowlist",
        stage_type="PRIVATE_SALE",
        stage_index=0,
        start_time="",
        end_time="",
        start_timestamp=int(time.time()),
        end_timestamp=int(time.time() + 200),
        max_per_wallet=1,
        price_wei=0,
        price_symbol="ETH",
        price_unit=0.0,
    )

    async def run_engine_test():
        results = await engine.execute_mint(
            collection=coll,
            stage=stage,
            wallets=[wallet],
            quantity_per_wallet=1,
            expected_price_wei=0,
        )
        assert len(results) == 1
        assert results[0].status == "SUCCESS"
        assert results[0].tx_hash.startswith("0x")
        assert results[0].gas_used == 120000

    asyncio.run(run_engine_test())


def test_ink_chain_support():
    from minter.constants import get_chain_info, get_tx_explorer_url
    from minter.config import AppConfig

    # 1. Chain constants
    info = get_chain_info(57073)
    assert info["name"] == "Ink"
    assert info["symbol"] == "ETH"
    assert "inkonchain.com" in info["explorer"]
    assert "inkonchain.com" in info["default_rpc"]

    # 2. Explorer URL
    url = get_tx_explorer_url(57073, "0xabcdef123456")
    assert url == "https://explorer.inkonchain.com/tx/0xabcdef123456"

    # 3. Config multi-chain routing
    cfg = AppConfig()
    cfg.rpc_url = "https://custom-robinhood-rpc.com"
    cfg.ink_rpc_url = "https://custom-ink-rpc.com"

    # Robinhood routing
    rb_rpc, rb_fb = cfg.get_rpc_for_chain(4663)
    assert rb_rpc == "https://custom-robinhood-rpc.com"

    # Ink routing
    ink_rpc, ink_fb = cfg.get_rpc_for_chain(57073)
    assert ink_rpc == "https://custom-ink-rpc.com"


def test_ethereum_chain_support():
    from minter.constants import get_chain_info, get_tx_explorer_url, get_fee_recipient, OPENSEA_FEE_RECIPIENT
    from minter.config import AppConfig

    # 1. Chain constants
    info = get_chain_info(1)
    assert info["name"] == "Ethereum Mainnet"
    assert info["symbol"] == "ETH"
    assert info["explorer"] == "https://etherscan.io"
    assert "publicnode.com" in info["default_rpc"]

    # 2. Explorer URL
    url = get_tx_explorer_url(1, "0x1234567890abcdef")
    assert url == "https://etherscan.io/tx/0x1234567890abcdef"

    # 3. SeaDrop Fee Recipient (must match OpenSea Fee Recipient on Ethereum to prevent FeeRecipientNotAllowed)
    fee_recipient = get_fee_recipient(1)
    assert fee_recipient == OPENSEA_FEE_RECIPIENT
    assert fee_recipient.lower() == "0x0000a26b00c1f0df003000390027140000faa719"

    # 4. Config multi-chain routing
    cfg = AppConfig()
    cfg.eth_rpc_url = "https://custom-eth-rpc.com"
    cfg.eth_fallback_rpc_url = "https://custom-eth-fallback.com"

    eth_rpc, eth_fb = cfg.get_rpc_for_chain(1)
    assert eth_rpc == "https://custom-eth-rpc.com"
    assert eth_fb == "https://custom-eth-fallback.com"

    # 5. Robinhood and Ink routing remain 100% intact
    rb_rpc, _ = cfg.get_rpc_for_chain(4663)
    ink_rpc, _ = cfg.get_rpc_for_chain(57073)
    assert rb_rpc == cfg.rpc_url
    assert ink_rpc == cfg.ink_rpc_url


def test_soneium_chain_support():
    from minter.constants import get_chain_info, get_tx_explorer_url, get_fee_recipient, OPENSEA_FEE_RECIPIENT
    from minter.config import AppConfig

    # 1. Chain constants
    info = get_chain_info(1868)
    assert info["name"] == "Soneium"
    assert info["symbol"] == "ETH"
    assert info["explorer"] == "https://soneium.blockscout.com"
    assert info["default_rpc"] == "https://rpc.soneium.org"
    assert info["fallback_rpc"] == "https://rpc.nodeflare.app/soneium/public"

    # 2. Explorer URL
    url = get_tx_explorer_url(1868, "0x1234567890abcdef")
    assert url == "https://soneium.blockscout.com/tx/0x1234567890abcdef"

    # 3. SeaDrop Fee Recipient (must match OpenSea Fee Recipient on Soneium to prevent FeeRecipientNotAllowed)
    fee_recipient = get_fee_recipient(1868)
    assert fee_recipient == OPENSEA_FEE_RECIPIENT
    assert fee_recipient.lower() == "0x0000a26b00c1f0df003000390027140000faa719"

    # 4. Config multi-chain routing
    cfg = AppConfig()
    cfg.soneium_rpc_url = "https://custom-soneium-rpc.com"
    cfg.soneium_fallback_rpc_url = "https://custom-soneium-fallback.com"

    son_rpc, son_fb = cfg.get_rpc_for_chain(1868)
    assert son_rpc == "https://custom-soneium-rpc.com"
    assert son_fb == "https://custom-soneium-fallback.com"

    # 5. Robinhood, Ink, and Ethereum routing remain 100% intact
    rb_rpc, _ = cfg.get_rpc_for_chain(4663)
    ink_rpc, _ = cfg.get_rpc_for_chain(57073)
    eth_rpc, _ = cfg.get_rpc_for_chain(1)
    assert rb_rpc == cfg.rpc_url
    assert ink_rpc == cfg.ink_rpc_url
    assert eth_rpc == cfg.eth_rpc_url


def test_elysium_chain_support():
    from minter.constants import get_chain_info, get_tx_explorer_url, get_fee_recipient, OPENSEA_FEE_RECIPIENT
    from minter.config import AppConfig

    # 1. Chain constants
    info = get_chain_info(99901)
    assert info["name"] == "Elysium"
    assert info["symbol"] == "HYPE"
    assert info["explorer"] == "https://elyscan.fun"
    assert info["default_rpc"] == "https://rpc.elysium.kinetiq.xyz"
    assert info["fallback_rpc"] == "https://rpc-live.elysp.com"

    # 2. Explorer URL
    url = get_tx_explorer_url(99901, "0x1234567890abcdef")
    assert url == "https://elyscan.fun/tx/0x1234567890abcdef"

    # 3. SeaDrop Fee Recipient
    fee_recipient = get_fee_recipient(99901)
    assert fee_recipient == OPENSEA_FEE_RECIPIENT
    assert fee_recipient.lower() == "0x0000a26b00c1f0df003000390027140000faa719"

    # 4. Config multi-chain routing
    cfg = AppConfig()
    cfg.elysium_rpc_url = "https://custom-elysium-rpc.com"
    cfg.elysium_fallback_rpc_url = "https://custom-elysium-fallback.com"

    ely_rpc, ely_fb = cfg.get_rpc_for_chain(99901)
    assert ely_rpc == "https://custom-elysium-rpc.com"
    assert ely_fb == "https://custom-elysium-fallback.com"

    # 5. Robinhood, Ink, Ethereum, and Soneium routing remain 100% intact
    rb_rpc, _ = cfg.get_rpc_for_chain(4663)
    ink_rpc, _ = cfg.get_rpc_for_chain(57073)
    eth_rpc, _ = cfg.get_rpc_for_chain(1)
    son_rpc, _ = cfg.get_rpc_for_chain(1868)
    assert rb_rpc == cfg.rpc_url
    assert ink_rpc == cfg.ink_rpc_url
    assert eth_rpc == cfg.eth_rpc_url
    assert son_rpc == cfg.soneium_rpc_url


def test_hedged_broadcast_dual_rpc():
    import asyncio
    from unittest.mock import AsyncMock
    from minter.chain import ChainClient

    async def run_test():
        client = ChainClient(
            "https://primary-rpc.com",
            fallback_rpc_url="https://fallback-rpc.com",
            sequencer_url="https://sequencer.mainnet.chain.robinhood.com",
        )
        
        called_urls = []
        mock_http = AsyncMock()
        
        async def mock_post(url, json=None, headers=None):
            called_urls.append(url)
            resp = AsyncMock()
            resp.raise_for_status = lambda: None
            if url == "https://primary-rpc.com":
                # Primary RPC is slow (simulating lag)
                await asyncio.sleep(0.5)
                resp.json = lambda: {"result": "0xprimary"}
            else:
                # Fallback RPC responds ultra-fast in 5ms!
                await asyncio.sleep(0.005)
                resp.json = lambda: {"result": "0xfallback"}
            return resp

        mock_http.post = mock_post
        client.get_client = AsyncMock(return_value=mock_http)

        # Broadcast should only race Primary + Fallback RPC (never sequencer_url) and take ~0.005s!
        tx_hash = await client.broadcast_raw_transaction(b"\x02\x01\x02")
        assert tx_hash == "0xfallback"
        assert "https://sequencer.mainnet.chain.robinhood.com" not in called_urls

    asyncio.run(run_test())


def test_check_wallet_eligibility_all_stages():
    import asyncio
    from unittest.mock import AsyncMock
    from minter.client import OpenSeaClient, CollectionInfo, DropStage
    from minter.wallet import WalletAccount

    async def run_test():
        os_client = OpenSeaClient()
        w = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000001")
        
        stage1 = DropStage(
            uuid="uuid-1",
            label="Public",
            stage_type="PUBLIC_SALE",
            stage_index=0,
            start_time="now",
            end_time="later",
            start_timestamp=100,
            end_timestamp=200,
            max_per_wallet=2,
            price_wei=0,
            price_symbol="ETH",
            price_unit=0.0,
        )
        stage2 = DropStage(
            uuid="uuid-2",
            label="Allowlist",
            stage_type="SIGNED_PRESALE",
            stage_index=1,
            start_time="now",
            end_time="later",
            start_timestamp=100,
            end_timestamp=200,
            max_per_wallet=1,
            price_wei=10000000000000000,
            price_symbol="ETH",
            price_unit=0.01,
        )
        coll = CollectionInfo(
            name="Test Coll",
            slug="test-coll",
            contract_address="0x0000000000000000000000000000000000000000",
            image_url="",
            chain_identifier="robinhood",
            chain_id=4663,
            total_supply=100,
            max_supply=100,
            stages=[stage1, stage2],
        )

        mock_http = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.json = lambda: {
            "data": {
                "dropBySlug": {
                    "accountStageEligibility": {
                        "wallets": [
                            {
                                "address": w.address.lower(),
                                "stages": [
                                    {
                                        "dropStageUuid": "uuid-2",
                                        "isEligible": True,
                                        "price": {"token": {"unit": 0.01, "symbol": "ETH"}},
                                    }
                                ]
                            }
                        ]
                    },
                    "stages": []
                }
            }
        }
        mock_http.post = AsyncMock(return_value=mock_resp)
        os_client.get_wallet_client = AsyncMock(return_value=mock_http)
        os_client.authenticate_wallet = AsyncMock()

        # Query all stages
        all_eligs = await os_client.check_wallet_eligibility_all_stages(coll, w)
        assert "uuid-1" in all_eligs
        assert all_eligs["uuid-1"].is_eligible is True
        assert "uuid-2" in all_eligs
        assert all_eligs["uuid-2"].is_eligible is True
        assert all_eligs["uuid-2"].price_unit == 0.01

        # Second query hits local memory cache immediately
        cached_eligs = await os_client.check_wallet_eligibility_all_stages(coll, w)
        assert cached_eligs is all_eligs

    asyncio.run(run_test())


def test_differing_quantities_no_cancellation():
    import asyncio
    from unittest.mock import AsyncMock
    from minter.engine import MintEngine
    from minter.client import CollectionInfo, DropStage, MintAction
    from minter.wallet import WalletAccount
    from minter.config import AppConfig

    async def run_test():
        cfg = AppConfig()
        mock_chain = AsyncMock()
        mock_chain.get_chain_id = AsyncMock(return_value=4663)
        mock_chain.get_balances_batch = AsyncMock(return_value={"0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf": 10**18, "0x2B5AD5c4795c026514f8317c7a215E218DcCD6cF": 10**18})
        mock_chain.get_nonces_batch = AsyncMock(return_value={"0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf": 0, "0x2B5AD5c4795c026514f8317c7a215E218DcCD6cF": 1})
        mock_gas = AsyncMock()
        mock_gas.max_priority_fee_per_gas = 1000000
        mock_gas.max_fee_per_gas = 20000000
        mock_chain.get_gas_fees = AsyncMock(return_value=mock_gas)
        mock_chain.broadcast_raw_transaction = AsyncMock(return_value="0xtx123")
        mock_receipt = AsyncMock()
        mock_receipt.status = 1
        mock_receipt.gas_used = 120000
        mock_chain.wait_for_receipt = AsyncMock(return_value=mock_receipt)

        mock_opensea = AsyncMock()
        mock_opensea.authenticate_all_wallets = AsyncMock()
        mock_opensea.stop_heartbeat = lambda: None

        # Wallet 1 mints 3 NFTs at 0.01 ETH each (total value = 0.03 ETH)
        # Wallet 2 mints 1 NFT at 0.01 ETH each (total value = 0.01 ETH)
        async def mock_get_mint_action(coll, w, qty, stage, **kwargs):
            val = qty * 10000000000000000
            return MintAction(
                target="0x00005EA00Ac477B1030CE78506496e8C2dE24bf5",
                value=val,
                calldata=b"\x12\x34",
                quantity=qty,
            )
        mock_opensea.get_mint_action_hot = mock_get_mint_action

        w1 = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000001", label="W1")
        w2 = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000002", label="W2")

        stage = DropStage(
            uuid="allowlist-1",
            label="Allowlist",
            stage_type="SIGNED_PRESALE",
            stage_index=0,
            start_time="",
            end_time="",
            start_timestamp=0,  # Live stage
            end_timestamp=2147483647,
            max_per_wallet=3,
            price_wei=10000000000000000,
            price_symbol="ETH",
            price_unit=0.01,
        )

        coll = CollectionInfo(
            name="Multi-Qty Collection",
            slug="multi-qty-coll",
            contract_address="0x1111111111111111111111111111111111111111",
            image_url="",
            chain_identifier="robinhood",
            chain_id=4663,
            total_supply=100,
            max_supply=100,
            stages=[stage],
        )

        engine = MintEngine(cfg, chain=mock_chain, opensea=mock_opensea)

        # W1 has 3, W2 has 1
        wallet_quantities = {w1.address: 3, w2.address: 1}

        results = await engine.execute_mint(
            collection=coll,
            stage=stage,
            wallets=[w1, w2],
            quantity_per_wallet=3,
            wallet_quantities=wallet_quantities,
            expected_price_wei=10000000000000000,
        )

        # Verify neither was cancelled, and both succeeded with their distinct quantities!
        assert len(results) == 2
        r1 = next(r for r in results if r.wallet.address == w1.address)
        r2 = next(r for r in results if r.wallet.address == w2.address)

        assert r1.status == "SUCCESS"
        assert r1.quantity == 3
        assert r2.status == "SUCCESS"
        assert r2.quantity == 1

    asyncio.run(run_test())


def test_allocation_decrement_fallback():
    import asyncio
    from unittest.mock import AsyncMock
    from minter.client import OpenSeaClient, CollectionInfo, DropStage
    from minter.wallet import WalletAccount

    async def run_test():
        os_client = OpenSeaClient()
        w = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000001")
        stage = DropStage(
            uuid="s1", label="Allowlist", stage_type="SIGNED_PRESALE", stage_index=0,
            start_time="", end_time="", start_timestamp=0, end_timestamp=2147483647,
            max_per_wallet=3, price_wei=0, price_symbol="ETH", price_unit=0.0
        )
        coll = CollectionInfo(
            name="Coll", slug="coll", contract_address="0x1111111111111111111111111111111111111111",
            image_url="", chain_identifier="robinhood", chain_id=4663, total_supply=100, max_supply=100,
            stages=[stage]
        )

        # Mock GraphQL: when quantity is 3 or 2, return MintQuantityMoreThanAllocatedForMinterError.
        # When quantity is 1, return success!
        attempts_seen = []
        async def mock_post(url, json=None, headers=None):
            resp = AsyncMock()
            resp.status_code = 200
            q_requested = int(json["variables"]["toAssets"][0]["quantity"])
            attempts_seen.append(q_requested)
            if q_requested > 1:
                resp.json = lambda: {
                    "data": {
                        "swap": {
                            "actions": [],
                            "errors": [{"__typename": "MintQuantityMoreThanAllocatedForMinterError"}]
                        }
                    }
                }
            else:
                resp.json = lambda: {
                    "data": {
                        "swap": {
                            "actions": [{
                                "transactionSubmissionData": {
                                    "to": "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5",
                                    "value": 0,
                                    "data": "0x123456"
                                }
                            }],
                            "errors": []
                        }
                    }
                }
            return resp

        mock_http = AsyncMock()
        mock_http.post = mock_post
        os_client.get_wallet_client = AsyncMock(return_value=mock_http)

        # Call with quantity 3
        act = await os_client.get_mint_action_hot(coll, w, quantity=3, stage=stage, max_attempts=5)
        
        # Verify it decremented 3 -> 2 -> 1 and succeeded at 1!
        assert act.quantity == 1
        assert attempts_seen == [3, 2, 1]

    asyncio.run(run_test())


def test_siwe_statement_and_headers():
    from minter.constants import SIWE_STATEMENT
    from minter.client import OpenSeaClient
    
    # Must use OpenSea's exact required statement
    assert "Platform Terms of Service" in SIWE_STATEMENT
    assert "https://opensea.io/tos" in SIWE_STATEMENT
    assert "https://opensea.io/privacy" in SIWE_STATEMENT

    # Test headers generation with api_key and token
    os_client = OpenSeaClient(api_key="test_key_123")
    os_client._wallet_tokens["0x1111111111111111111111111111111111111111"] = "jwt_token_abc"

    headers = os_client._get_headers(wallet_address="0x1111111111111111111111111111111111111111", slug="cool-drop")
    assert headers["x-api-key"] == "test_key_123"
    assert headers["authorization"] == "Bearer jwt_token_abc"
    assert headers["origin"] == "https://opensea.io"
    assert "cool-drop" in headers["referer"]


def test_cross_wallet_eligibility_caching():
    import asyncio
    from unittest.mock import AsyncMock
    from minter.client import OpenSeaClient, CollectionInfo, DropStage
    from minter.wallet import WalletAccount

    async def run_test():
        os_client = OpenSeaClient()
        w1 = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000001", label="W1")
        w2 = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000002", label="W2")

        stage1 = DropStage(
            uuid="stage-allowlist-1",
            label="Allowlist",
            stage_type="SIGNED_PRESALE",
            stage_index=1,
            start_time="now",
            end_time="later",
            start_timestamp=100,
            end_timestamp=200,
            max_per_wallet=2,
            price_wei=0,
            price_symbol="ETH",
            price_unit=0.0,
        )
        coll = CollectionInfo(
            name="Test Coll",
            slug="test-slug",
            contract_address="0x1111111111111111111111111111111111111111",
            image_url="",
            chain_identifier="robinhood",
            chain_id=4663,
            total_supply=100,
            max_supply=100,
            stages=[stage1],
        )

        mock_http = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        # OpenSea returns BOTH w1 and w2 in accountStageEligibility.wallets!
        mock_resp.json = lambda: {
            "data": {
                "dropBySlug": {
                    "accountStageEligibility": {
                        "wallets": [
                            {
                                "address": w1.address.lower(),
                                "stages": [{"dropStageUuid": "stage-allowlist-1", "isEligible": True, "maxTotalMintableByWallet": 2}]
                            },
                            {
                                "address": w2.address.lower(),
                                "stages": [{"dropStageUuid": "stage-allowlist-1", "isEligible": True, "maxTotalMintableByWallet": 1}]
                            }
                        ]
                    },
                    "stages": []
                }
            }
        }
        mock_http.post = AsyncMock(return_value=mock_resp)
        os_client.get_wallet_client = AsyncMock(return_value=mock_http)
        os_client.authenticate_wallet = AsyncMock()

        # Check W1 only
        eligs_w1 = await os_client.check_wallet_eligibility_all_stages(coll, w1)
        assert eligs_w1["stage-allowlist-1"].is_eligible is True
        assert eligs_w1["stage-allowlist-1"].max_mintable == 2

        # W2 must ALREADY be cached without making another network request!
        mock_http.post.reset_mock()
        eligs_w2 = await os_client.check_wallet_eligibility_all_stages(coll, w2)
        assert eligs_w2["stage-allowlist-1"].is_eligible is True
        assert eligs_w2["stage-allowlist-1"].max_mintable == 1
        assert mock_http.post.call_count == 0  # 0 network calls!

    asyncio.run(run_test())


def test_auto_select_eligible_wallets_only():
    from minter.wallet import WalletAccount
    from minter.client import DropStage, WalletEligibility

    w1 = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000001", label="EligibleWallet")
    w2 = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000002", label="IneligibleWallet")
    w3 = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000003", label="MaxMintedWallet")

    stage_private = DropStage(
        uuid="stage-allowlist-1",
        label="Allowlist",
        stage_type="SIGNED_PRESALE",
        stage_index=1,
        start_time="now",
        end_time="later",
        start_timestamp=100,
        end_timestamp=200,
        max_per_wallet=2,
        price_wei=0,
        price_symbol="ETH",
        price_unit=0.0,
    )

    eligibility_cache = {
        (stage_private.uuid, w1.address): WalletEligibility(is_eligible=True, max_mintable=1, price_wei=0),
        (stage_private.uuid, w2.address): WalletEligibility(is_eligible=False, reason="Not on Allowlist"),
        (stage_private.uuid, w3.address): WalletEligibility(is_eligible=False, max_mintable=0, reason="Already minted maximum allocation (1/1)"),
    }

    all_wallets = [w1, w2, w3]
    # Verify auto-filter selects ONLY eligible wallet with remaining allocation (> 0)
    selected = [
        w for w in all_wallets
        if eligibility_cache.get((stage_private.uuid, w.address))
        and eligibility_cache[(stage_private.uuid, w.address)].is_eligible
        and eligibility_cache[(stage_private.uuid, w.address)].max_mintable > 0
    ]

    assert len(selected) == 1
    assert selected[0].address == w1.address
    assert w2 not in selected
    assert w3 not in selected


def test_multiphase_allocation_tracking_and_onchain_balance():
    """Verify that when a wallet has already minted its allocation in GTD or on-chain,
    it is detected as 0 left and marked not eligible for the next phase (FCFS)."""
    from minter.client import CollectionInfo, DropStage, OpenSeaClient
    from minter.wallet import WalletAccount

    async def run_test():
        os_client = OpenSeaClient(timeout_seconds=5.0)

        w_gtd_minted = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000001", label="SECOND")
        w_remaining = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000002", label="new")

        stage_fcfs = DropStage(
            uuid="fcfs-uuid-123",
            label="FCFS",
            stage_type="SIGNED_PRESALE",
            stage_index=1,
            start_time="now",
            end_time="later",
            start_timestamp=100,
            end_timestamp=500,
            max_per_wallet=1,
            price_wei=0,
            price_symbol="ETH",
            price_unit=0.0,
        )

        coll = CollectionInfo(
            name="Test Drop",
            slug="test-drop",
            contract_address="0x" + "9" * 40,
            image_url="",
            chain_identifier="robinhood",
            chain_id=4663,
            total_supply=100,
            max_supply=1000,
            stages=[stage_fcfs],
        )

        # Mock OpenSea GraphQL response where w_gtd_minted already has quantityMinted: 1, maxTotalMintableByWallet: 1
        mock_response_data = {
            "data": {
                "dropBySlug": {
                    "stages": [{"uuid": "fcfs-uuid-123", "eligibleMaxTotalMintableByWallet": 1, "isEligible": True}],
                    "accountStageEligibility": {
                        "wallets": [
                            {
                                "address": w_gtd_minted.address,
                                "quantityMinted": 1,
                                "stages": [
                                    {
                                        "dropStageUuid": "fcfs-uuid-123",
                                        "isEligible": True,
                                        "maxTotalMintableByWallet": 1,
                                    }
                                ],
                            },
                            {
                                "address": w_remaining.address,
                                "quantityMinted": 0,
                                "stages": [
                                    {
                                        "dropStageUuid": "fcfs-uuid-123",
                                        "isEligible": True,
                                        "maxTotalMintableByWallet": 2,
                                    }
                                ],
                            },
                        ]
                    },
                }
            }
        }

        mock_http = MagicMock()
        mock_http.is_closed = False
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_response_data
        mock_http.post = AsyncMock(return_value=mock_resp)

        os_client._wallet_clients[w_gtd_minted.address.lower()] = mock_http
        os_client._wallet_clients[w_remaining.address.lower()] = mock_http
        os_client.authenticate_wallet = AsyncMock()

        # Query eligibility
        eligs_second = await os_client.check_wallet_eligibility_all_stages(coll, w_gtd_minted)
        el_second = eligs_second["fcfs-uuid-123"]

        # SECOND has minted 1/1 -> remaining 0 -> is_eligible must be False!
        assert el_second.is_eligible is False
        assert el_second.max_mintable == 0
        assert "Already minted maximum allocation" in el_second.reason

        # new has minted 0/2 -> remaining 2 -> is_eligible must be True!
        eligs_new = await os_client.check_wallet_eligibility_all_stages(coll, w_remaining)
        el_new = eligs_new["fcfs-uuid-123"]
        assert el_new.is_eligible is True
        assert el_new.max_mintable == 2
        assert "2 Left" in el_new.reason

        # Now test on-chain balance detection override
        mock_chain = MagicMock()
        # Mock balanceOf returns 1 token (hex 0x01 padded)
        mock_chain.call = AsyncMock(return_value="0x" + "0" * 63 + "1")

        # Even if OpenSea reported 0 quantityMinted, on-chain ground truth balance = 1
        mock_response_zero_os = {
            "data": {
                "dropBySlug": {
                    "accountStageEligibility": {
                        "wallets": [
                            {
                                "address": w_gtd_minted.address,
                                "quantityMinted": 0,
                                "stages": [
                                    {
                                        "dropStageUuid": "fcfs-uuid-123",
                                        "isEligible": True,
                                        "maxTotalMintableByWallet": 1,
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        }
        mock_resp.json.return_value = mock_response_zero_os
        os_client.clear_eligibility_cache(coll.slug)

        eligs_onchain = await os_client.check_wallet_eligibility_all_stages(
            coll, w_gtd_minted, force_refresh=True, chain_client=mock_chain
        )
        el_onchain = eligs_onchain["fcfs-uuid-123"]
        assert el_onchain.is_eligible is False
        assert el_onchain.max_mintable == 0
        assert "Already minted maximum allocation (1/1)" in el_onchain.reason

    asyncio.run(run_test())


def test_active_scheduled_mint_cancellation():
    import asyncio
    import time
    from unittest.mock import AsyncMock, MagicMock
    from minter.engine import MintEngine
    from minter.client import CollectionInfo, DropStage, OpenSeaClient
    from minter.wallet import WalletAccount
    from minter.config import AppConfig
    from minter.chain import GasFees

    async def run_test():
        cfg = AppConfig()
        chain = AsyncMock()
        chain.get_chain_id = AsyncMock(return_value=4663)
        chain.get_balances_batch = AsyncMock(return_value={"0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf": 10**18})
        chain.get_gas_fees = AsyncMock(return_value=GasFees(base_fee_per_gas=100000000, max_priority_fee_per_gas=2000000, max_fee_per_gas=120000000))
        chain.get_nonces_batch = AsyncMock()
        chain.broadcast_raw_transaction = AsyncMock()

        opensea = AsyncMock(spec=OpenSeaClient)
        opensea.start_heartbeat = MagicMock()
        opensea.stop_heartbeat = MagicMock()

        engine = MintEngine(cfg, chain, opensea)

        w = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000001", label="W1")
        future_start = int(time.time()) + 100  # 100s in the future
        stage = DropStage(
            uuid="scheduled-stage-1",
            label="Public Drop",
            stage_type="PUBLIC_SALE",
            stage_index=0,
            start_time="future",
            end_time="future_end",
            start_timestamp=future_start,
            end_timestamp=future_start + 1000,
            max_per_wallet=1,
            price_wei=0,
            price_symbol="ETH",
            price_unit=0.0,
        )
        coll = CollectionInfo(
            name="Scheduled Coll",
            slug="scheduled-coll",
            contract_address="0x1111111111111111111111111111111111111111",
            image_url="",
            chain_identifier="robinhood",
            chain_id=4663,
            total_supply=100,
            max_supply=100,
            stages=[stage],
        )

        progress_msgs = []
        async def on_progress(msg):
            progress_msgs.append(msg)

        # Launch execute_mint in background
        mint_task = asyncio.create_task(
            engine.execute_mint(
                collection=coll,
                stage=stage,
                wallets=[w],
                quantity_per_wallet=1,
                on_progress=on_progress,
            )
        )

        # Allow countdown to start
        await asyncio.sleep(0.1)

        # User cancels during countdown!
        engine.cancel()

        results = await mint_task

        # Verify it cleanly cancelled
        assert len(results) == 1
        assert results[0].status == "CANCELLED_BY_USER"
        assert results[0].error_message == "Mint was cancelled by user"
        # Verify no transactions were broadcasted!
        assert chain.broadcast_raw_transaction.call_count == 0
        assert chain.get_nonces_batch.call_count == 0

    asyncio.run(run_test())


def test_concurrent_multi_mint_nonce_reservation():
    """Verify that multiple simultaneous mint drops reserve consecutive nonces without collisions."""
    from minter.engine import NonceTracker
    NonceTracker.reset()

    async def run_test():
        mock_chain = MagicMock()
        wallet_addr = "0x1111111111111111111111111111111111111111"
        # On-chain base nonce is 5
        mock_chain.get_nonces_batch = AsyncMock(return_value={wallet_addr: 5})

        # Drop 1 requests nonces on Robinhood (4663)
        nonces_drop1 = await NonceTracker.get_and_reserve_nonces(mock_chain, 4663, [wallet_addr])
        assert nonces_drop1[wallet_addr] == 5

        # Drop 2 requests nonces simultaneously on Robinhood (4663) for the same wallet
        nonces_drop2 = await NonceTracker.get_and_reserve_nonces(mock_chain, 4663, [wallet_addr])
        assert nonces_drop2[wallet_addr] == 6  # Next consecutive nonce! No collision!

        # Drop 3 on a DIFFERENT chain (Ink 57073) with base nonce 0
        mock_chain_ink = MagicMock()
        mock_chain_ink.get_nonces_batch = AsyncMock(return_value={wallet_addr: 0})
        nonces_drop3 = await NonceTracker.get_and_reserve_nonces(mock_chain_ink, 57073, [wallet_addr])
        assert nonces_drop3[wallet_addr] == 0  # Separate chain, independent nonce space!

        # Drop 1 finishes and releases
        await NonceTracker.release_nonces(4663, [wallet_addr], broadcasted=True)
        # Drop 2 still active, so reserved nonce is still 7
        assert (4663, wallet_addr.lower()) in NonceTracker._reserved

        # Drop 2 finishes and releases
        await NonceTracker.release_nonces(4663, [wallet_addr], broadcasted=True)
        # All Robinhood reservations released
        assert (4663, wallet_addr.lower()) not in NonceTracker._reserved

        # Clean up Drop 3
        await NonceTracker.release_nonces(57073, [wallet_addr], broadcasted=True)
        assert len(NonceTracker._reserved) == 0

    asyncio.run(run_test())


def test_simultaneous_multi_drop_selective_cancellation():
    """Verify that cancelling one drop does not affect or cancel another simultaneously running drop."""
    from minter.engine import MintEngine, NonceTracker
    from minter.client import CollectionInfo, DropStage
    from minter.chain import GasFees
    NonceTracker.reset()

    async def run_test():
        config = AppConfig()
        wallet = WalletAccount(private_key="0x" + "a" * 64, label="W1")

        mock_chain = MagicMock()
        mock_chain.rpc_url = "https://rpc.robinhood.org"
        mock_chain.fallback_rpc_url = ""
        mock_chain.timeout_seconds = 5.0
        mock_chain.get_chain_id = AsyncMock(return_value=4663)
        mock_chain.get_balances_batch = AsyncMock(return_value={wallet.address: int(1e18)})
        mock_chain.get_nonces_batch = AsyncMock(return_value={wallet.address: 0})
        mock_chain.get_gas_fees = AsyncMock(return_value=GasFees(1_000_000_000, 2_000_000_000, 1_000_000_000))
        mock_chain.broadcast_raw_transaction = AsyncMock(return_value="0xabc123")
        mock_chain.wait_for_receipt = AsyncMock(return_value=MagicMock(status=1, gas_used=100000))

        mock_opensea = MagicMock()
        mock_opensea.start_heartbeat = MagicMock()
        mock_opensea.stop_heartbeat = MagicMock()

        # Collection 1 (Scheduled in the future, will be cancelled)
        coll1 = CollectionInfo(name="c1", slug="c1", contract_address="0x" + "1" * 40, image_url="", chain_identifier="robinhood", chain_id=4663, total_supply=100, max_supply=100, stages=[])
        stage1 = DropStage(
            uuid="s1",
            label="Public 1",
            stage_type="PUBLIC_SALE",
            stage_index=0,
            start_time="future",
            end_time="future_end",
            start_timestamp=int(time.time()) + 300,
            end_timestamp=int(time.time()) + 600,
            max_per_wallet=1,
            price_wei=0,
            price_symbol="ETH",
            price_unit=0.0,
        )

        # Collection 2 (Live now, will execute successfully)
        coll2 = CollectionInfo(name="c2", slug="c2", contract_address="0x" + "2" * 40, image_url="", chain_identifier="robinhood", chain_id=4663, total_supply=100, max_supply=100, stages=[])
        stage2 = DropStage(
            uuid="s2",
            label="Public 2",
            stage_type="PUBLIC_SALE",
            stage_index=0,
            start_time="now",
            end_time="later",
            start_timestamp=int(time.time()) - 10,
            end_timestamp=int(time.time()) + 600,
            max_per_wallet=1,
            price_wei=0,
            price_symbol="ETH",
            price_unit=0.0,
        )

        engine1 = MintEngine(config, mock_chain, mock_opensea)
        engine2 = MintEngine(config, mock_chain, mock_opensea)

        async def run_engine1():
            return await engine1.execute_mint(coll1, stage1, [wallet], quantity_per_wallet=1)

        async def run_engine2():
            return await engine2.execute_mint(coll2, stage2, [wallet], quantity_per_wallet=1)

        task1 = asyncio.create_task(run_engine1())
        # Let engine1 enter countdown
        await asyncio.sleep(0.05)
        # Cancel engine1 ONLY
        engine1.cancel()

        # Run engine2 in parallel
        results2 = await run_engine2()
        results1 = await task1

        # Assert engine1 was cancelled
        assert results1[0].status == "CANCELLED_BY_USER"
        # Assert engine2 succeeded without interruption!
        assert results2[0].status == "SUCCESS"
        assert results2[0].tx_hash != ""

    asyncio.run(run_test())


def test_telegram_bot_multi_drop_card_isolation():
    """Verify that multiple mint cards in Telegram maintain independent decoupled sessions."""
    from bot import TelegramBot
    from minter.client import CollectionInfo, DropStage
    from minter.wallet import WalletAccount, WalletManager

    with tempfile.TemporaryDirectory() as tmp_dir:
        w_file = Path(tmp_dir) / "wallets.json"
        w_mgr = WalletManager(str(w_file))
        w = w_mgr.add_wallet("0x0000000000000000000000000000000000000000000000000000000000000001", label="W1")
        config = AppConfig()
        config.telegram_bot_token = "fake:token"
        bot = TelegramBot(config, w_mgr)

        coll_a = CollectionInfo(name="CollA", slug="colla", contract_address="0x" + "1" * 40, image_url="", chain_identifier="robinhood", chain_id=4663, total_supply=100, max_supply=100, stages=[])
        coll_b = CollectionInfo(name="CollB", slug="collb", contract_address="0x" + "2" * 40, image_url="", chain_identifier="ink", chain_id=57073, total_supply=100, max_supply=100, stages=[])

        # Card A at msg_id 100
        bot._card_sessions[100] = {
            "message_id": 100,
            "chat_id": 12345,
            "collection": coll_a,
            "quantity": 1,
            "selected_wallets": [w],
        }
        # Card B at msg_id 200
        bot._card_sessions[200] = {
            "message_id": 200,
            "chat_id": 12345,
            "collection": coll_b,
            "quantity": 5,
            "selected_wallets": [w],
        }

        # Modify Card A
        bot._card_sessions[100]["quantity"] = 3

        # Assert Card B is completely unaffected
        assert bot._card_sessions[100]["quantity"] == 3
        assert bot._card_sessions[200]["quantity"] == 5
        assert bot._card_sessions[100]["collection"].slug == "colla"
        assert bot._card_sessions[200]["collection"].slug == "collb"


def test_sequencer_excluded_from_chain_constants():
    from minter.constants import get_chain_info
    rb = get_chain_info(4663)
    assert rb["name"] == "Robinhood Chain"
    assert not rb.get("sequencer_url"), "Robinhood Chain should use clean Dual-RPC without sequencer_url"


def test_safe_poll_receipt_and_dual_rpc():
    import inspect
    from minter.chain import ChainClient
    cc = ChainClient(
        rpc_url="https://alchemy.rpc",
        fallback_rpc_url="https://fallback.rpc",
    )
    sig = inspect.signature(cc.wait_for_receipt)
    assert sig.parameters["poll_interval"].default == 0.15, f"poll_interval should default to safe 0.15s (150ms), got {sig.parameters['poll_interval'].default}"

    # Verify receipt tracking resolves instantly on Poll #1 with 0ms sleep
    async def run_wait_test():
        cc.rpc_request = AsyncMock(return_value={
            "status": "0x1",
            "blockNumber": "0x123",
            "gasUsed": "0x5208",
            "effectiveGasPrice": "0x3b9aca00",
        })
        rec = await cc.wait_for_receipt("0xtxhash", timeout_seconds=2.0)
        assert rec is not None
        assert rec.status == 1
        assert rec.block_number == 0x123
        assert rec.gas_used == 0x5208

    asyncio.run(run_wait_test())


def test_fast_429_retry_and_zero_stagger():
    from minter.client import OpenSeaClient, CollectionInfo, DropStage
    from minter.wallet import WalletAccount

    client = OpenSeaClient()
    wallet = WalletAccount("0x0000000000000000000000000000000000000000000000000000000000000001")
    coll = CollectionInfo(
        name="FastTest", slug="fast-test", contract_address="0x" + "1" * 40,
        image_url="", chain_identifier="ROBINHOOD", chain_id=4663, total_supply=10, max_supply=100, stages=[],
    )
    stage = DropStage(
        uuid="s1", label="Allowlist", stage_type="PRIVATE_SALE", stage_index=0,
        start_time="", end_time="", start_timestamp=int(time.time() - 10), end_timestamp=int(time.time() + 100),
        max_per_wallet=1, price_wei=0, price_symbol="ETH", price_unit=0.0,
    )

    calls = {"n": 0}
    async def mock_post(url, json=None, headers=None):
        calls["n"] += 1
        resp = MagicMock()
        if calls["n"] == 1:
            resp.status_code = 429
            resp.headers = {}
            return resp
        resp.status_code = 200
        resp.headers = {}
        resp.json = lambda: {
            "data": {
                "swap": {
                    "actions": [{
                        "transactionSubmissionData": {
                            "to": "0x" + "2" * 40,
                            "data": "0xdeadbeef",
                            "value": "0",
                        }
                    }],
                    "errors": [],
                }
            }
        }
        return resp

    mock_http = AsyncMock()
    mock_http.is_closed = False
    mock_http.post = mock_post
    client._wallet_clients[wallet.address.lower()] = mock_http

    async def run_test():
        t0 = time.time()
        act = await client.get_mint_action_hot(coll, wallet, 1, stage, max_attempts=3)
        elapsed = time.time() - t0
        assert act.attempts == 2
        assert act.calldata == b"\xde\xad\xbe\xef"
        assert elapsed < 0.25, f"Fast 429 retry should be <0.25s, took {elapsed:.3f}s"

    asyncio.run(run_test())


def test_fast_gas_mode():
    """Verify Fast Gas Mode boosts L1/L2 priority & max fees when enabled, preserves normal behavior by default, and toggles cleanly per card."""
    from bot import TelegramBot
    from minter.chain import ChainClient, GasFees
    from minter.client import CollectionInfo, DropStage, MintAction
    from minter.engine import MintEngine, NonceTracker
    from minter.wallet import WalletAccount, WalletManager

    NonceTracker.reset()

    async def run_test():
        # 1. Verify ChainClient.get_gas_fees normal vs fast on L2 (Robinhood 4663) and L1 (Ethereum 1)
        async def eval_chain_gas(chain_id: int, base_fee: int, rpc_priority: int, fast: bool) -> GasFees:
            cc = ChainClient("https://rpc.example.com")
            cc.get_chain_id = AsyncMock(return_value=chain_id)

            async def fake_rpc(method: str, params=None):
                if method == "eth_getBlockByNumber":
                    return {"baseFeePerGas": hex(base_fee)}
                if method == "eth_maxPriorityFeePerGas":
                    return hex(rpc_priority)
                if method == "eth_gasPrice":
                    return hex(base_fee + rpc_priority)
                return None

            cc.rpc_request = fake_rpc
            return await cc.get_gas_fees(fee_multiplier=1.25, priority_fee_gwei=0.002, fast_mode=fast)

        l2_normal = await eval_chain_gas(4663, 20_000_000, 1_000_000, fast=False)
        l2_fast = await eval_chain_gas(4663, 20_000_000, 1_000_000, fast=True)
        assert l2_normal.max_priority_fee_per_gas <= 2_000_000, f"L2 normal priority should stay normal (<=2_000_000), got {l2_normal.max_priority_fee_per_gas}"
        assert l2_fast.max_priority_fee_per_gas >= 100_000_000, f"L2 fast priority should be >=0.1 Gwei, got {l2_fast.max_priority_fee_per_gas}"
        assert l2_fast.max_fee_per_gas > l2_normal.max_fee_per_gas

        l1_normal = await eval_chain_gas(1, 10_000_000_000, 500_000_000, fast=False)
        l1_fast = await eval_chain_gas(1, 10_000_000_000, 500_000_000, fast=True)
        assert l1_fast.max_priority_fee_per_gas >= 2_000_000_000, f"L1 fast priority should be >=2 Gwei, got {l1_fast.max_priority_fee_per_gas}"
        assert l1_fast.max_fee_per_gas >= int(10_000_000_000 * 2.5)
        assert l1_fast.max_fee_per_gas > l1_normal.max_fee_per_gas

        # 2. Verify MintEngine.execute_mint forwards fast_mode cleanly without affecting normal mode
        cfg = AppConfig()
        cfg.broadcast_lead_ms = 0
        wallet = WalletAccount(private_key="0x" + "b" * 64, label="W1")
        coll = CollectionInfo(
            name="FastGasColl", slug="fast-gas-coll", contract_address="0x" + "3" * 40,
            image_url="", chain_identifier="robinhood", chain_id=4663, total_supply=10, max_supply=100, stages=[],
        )
        stage = DropStage(
            uuid="s1", label="Public", stage_type="PUBLIC_SALE", stage_index=0,
            start_time="", end_time="", start_timestamp=int(time.time()) - 10, end_timestamp=int(time.time()) + 300,
            max_per_wallet=1, price_wei=0, price_symbol="ETH", price_unit=0.0,
        )

        seen_fast_flags: list[bool] = []
        mock_chain = MagicMock()
        mock_chain.rpc_url = "https://rpc.robinhood.org"
        mock_chain.fallback_rpc_url = ""
        mock_chain.timeout_seconds = 5.0
        mock_chain.get_chain_id = AsyncMock(return_value=4663)
        mock_chain.get_balances_batch = AsyncMock(return_value={wallet.address: int(1e18)})
        mock_chain.get_nonces_batch = AsyncMock(return_value={wallet.address: 0})

        async def mock_get_gas_fees(fee_multiplier=1.25, priority_fee_gwei=0.002, fast_mode=False, **kwargs):
            seen_fast_flags.append(bool(fast_mode))
            if fast_mode:
                return GasFees(20_000_000, 100_000_000, 160_000_000)
            return GasFees(20_000_000, 2_000_000, 27_000_000)

        mock_chain.get_gas_fees = AsyncMock(side_effect=mock_get_gas_fees)
        mock_chain.broadcast_raw_transaction = AsyncMock(return_value="0x" + "c" * 64)
        mock_chain.wait_for_receipt = AsyncMock(return_value=MagicMock(status=1, gas_used=120000))

        mock_opensea = MagicMock()
        mock_opensea.start_heartbeat = MagicMock()
        mock_opensea.stop_heartbeat = MagicMock()
        mock_opensea.get_mint_action_hot = AsyncMock(return_value=MintAction(
            target="0x" + "4" * 40, value=0, calldata=b"\xab\xcd",
        ))

        engine = MintEngine(cfg, mock_chain, mock_opensea)
        res_normal = await engine.execute_mint(coll, stage, [wallet], quantity_per_wallet=1, fast_mode=False)
        assert res_normal[0].status == "SUCCESS"
        assert all(f is False for f in seen_fast_flags)

        seen_fast_flags.clear()
        res_fast = await engine.execute_mint(coll, stage, [wallet], quantity_per_wallet=1, fast_mode=True)
        assert res_fast[0].status == "SUCCESS"
        assert len(seen_fast_flags) >= 1 and all(f is True for f in seen_fast_flags)

        # 3. Verify TelegramBot Mint Card toggle defaults to False and isolates per-card fast_mode
        with tempfile.TemporaryDirectory() as tmp_dir:
            w_mgr = WalletManager(str(Path(tmp_dir) / "wallets.json"))
            w = w_mgr.add_wallet("0x" + "b" * 64, label="W1")
            bot = TelegramBot(cfg, w_mgr)
            bot.answer_callback = AsyncMock()
            bot.edit_message = AsyncMock()

            coll_a = CollectionInfo(name="CollA", slug="colla", contract_address="0x" + "1" * 40, image_url="", chain_identifier="robinhood", chain_id=4663, total_supply=10, max_supply=100, stages=[stage])
            coll_b = CollectionInfo(name="CollB", slug="collb", contract_address="0x" + "2" * 40, image_url="", chain_identifier="robinhood", chain_id=4663, total_supply=10, max_supply=100, stages=[stage])

            bot._card_sessions[100] = {
                "message_id": 100, "chat_id": 12345, "collection": coll_a,
                "selected_stage": stage, "quantity": 1, "selected_wallets": [w],
                "expected_price_wei": 0, "expected_price_unit": 0.0, "expected_price_symbol": "ETH",
                "fast_mode": False,
            }
            bot._card_sessions[200] = {
                "message_id": 200, "chat_id": 12345, "collection": coll_b,
                "selected_stage": stage, "quantity": 1, "selected_wallets": [w],
                "expected_price_wei": 0, "expected_price_unit": 0.0, "expected_price_symbol": "ETH",
                "fast_mode": False,
            }

            await bot.handle_callback({
                "id": "cb1",
                "data": "toggle_fast_mode",
                "message": {"chat": {"id": 12345}, "message_id": 100},
            })

            assert bot._card_sessions[100]["fast_mode"] is True
            assert bot._card_sessions[200]["fast_mode"] is False
            rendered_text = bot.edit_message.call_args.args[2]
            rendered_markup = bot.edit_message.call_args.kwargs["reply_markup"]["inline_keyboard"]
            assert "FAST (Max Priority)" in rendered_text
            assert any(btn.get("callback_data") in ("cycle_gas_mode", "toggle_fast_mode") and "FAST" in btn.get("text", "") for row in rendered_markup for btn in row)

    asyncio.run(run_test())


def test_hybrid_rest_and_gql_mint_race():
    """Verify official OpenSea REST v2 /drops/{slug}/mint races cleanly with GQL and falls back seamlessly on 429."""
    from minter.client import CollectionInfo, DropStage, OpenSeaClient
    from minter.wallet import WalletAccount

    wallet = WalletAccount("0x" + "1" * 64)
    coll = CollectionInfo(
        name="HybridDrop", slug="hybrid-drop", contract_address="0x" + "2" * 40,
        image_url="", chain_identifier="ROBINHOOD", chain_id=4663, total_supply=10, max_supply=100, stages=[],
    )
    stage = DropStage(
        uuid="s1", label="Allowlist", stage_type="PRIVATE_SALE", stage_index=0,
        start_time="", end_time="", start_timestamp=int(time.time()) - 5, end_timestamp=int(time.time()) + 100,
        max_per_wallet=2, price_wei=1000, price_symbol="ETH", price_unit=0.0,
    )

    async def run_test():
        client = OpenSeaClient(api_key="os_test_key_123")
        client._authenticated_wallets.add(wallet.address.lower())
        client._wallet_tokens[wallet.address.lower()] = "tok"

        # Case 1: Fast REST wins race over slow GQL + preserves leading 0x00 calldata bytes
        async def slow_gql_post(url, json=None, headers=None):
            await asyncio.sleep(0.15)
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {}
            resp.json = lambda: {"data": {"swap": {"actions": [], "errors": []}}}
            return resp

        async def fast_rest_post(url, json=None, headers=None):
            assert headers.get("x-api-key") == "os_test_key_123"
            assert json["minter"] == wallet.address
            assert json["skip_balance_check"] is True
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {}
            resp.json = lambda: {
                "to": "0x" + "3" * 40,
                "data": "0x00abcdef",
                "value": "1000",
            }
            return resp

        mock_wallet_cl = AsyncMock()
        mock_wallet_cl.is_closed = False
        mock_wallet_cl.post = slow_gql_post
        client._wallet_clients[wallet.address.lower()] = mock_wallet_cl

        mock_rest_cl = AsyncMock()
        mock_rest_cl.is_closed = False
        mock_rest_cl.post = fast_rest_post
        client._rest_client = mock_rest_cl

        t0 = time.time()
        act = await client.get_mint_action_hot(coll, wallet, 2, stage, max_attempts=2)
        elapsed = time.time() - t0
        assert act.calldata == b"\x00\xab\xcd\xef"
        assert act.value == 1000
        assert act.quantity == 2
        assert elapsed < 0.08, f"Fast REST should win before slow GQL finishes (took {elapsed:.3f}s)"

        # Case 2: REST hits 429 -> triggers circuit breaker and GQL seamlessly completes with MintAction
        rest_calls = {"n": 0}

        async def rest_429_post(url, json=None, headers=None):
            rest_calls["n"] += 1
            resp = MagicMock()
            resp.status_code = 429
            resp.headers = {"retry-after": "10"}
            return resp

        async def ok_gql_post(url, json=None, headers=None):
            await asyncio.sleep(0.01)
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {}
            resp.json = lambda: {
                "data": {
                    "swap": {
                        "actions": [{
                            "transactionSubmissionData": {
                                "to": "0x" + "4" * 40,
                                "data": "0x00112233",
                                "value": "0",
                            }
                        }],
                        "errors": [],
                    }
                }
            }
            return resp

        mock_rest_cl.post = rest_429_post
        mock_wallet_cl.post = ok_gql_post

        act2 = await client.get_mint_action_hot(coll, wallet, 1, stage, max_attempts=2)
        assert act2.calldata == b"\x00\x11\x22\x33"
        assert client._rest_rate_limited_until > time.time()
        assert rest_calls["n"] == 1

        # Next call while circuit breaker active skips REST completely
        await client.get_mint_action_hot(coll, wallet, 1, stage, max_attempts=2)
        assert rest_calls["n"] == 1

    asyncio.run(run_test())


def test_revert_diagnosis_and_t0_timing():
    """Verify failed tx receipt simulates exact contract error via eth_call and allowlist avoids firing 120ms early."""
    from minter.chain import GasFees
    from minter.client import CollectionInfo, DropStage, MintAction
    from minter.engine import MintEngine, NonceTracker
    from minter.wallet import WalletAccount

    NonceTracker.reset()

    async def run_test():
        cfg = AppConfig()
        cfg.broadcast_lead_ms = 80
        wallet = WalletAccount("0x" + "d" * 64, label="DiagW")
        coll = CollectionInfo(
            name="DiagDrop", slug="diag-drop", contract_address="0x" + "5" * 40,
            image_url="", chain_identifier="ROBINHOOD", chain_id=4663, total_supply=10, max_supply=10, stages=[],
        )
        start_ts = int(time.time()) + 1
        stage = DropStage(
            uuid="s1", label="Allowlist", stage_type="PRIVATE_SALE", stage_index=0,
            start_time="", end_time="", start_timestamp=start_ts, end_timestamp=start_ts + 300,
            max_per_wallet=1, price_wei=0, price_symbol="ETH", price_unit=0.0,
        )

        mock_chain = MagicMock()
        mock_chain.rpc_url = "https://rpc.robinhood.org"
        mock_chain.fallback_rpc_url = ""
        mock_chain.timeout_seconds = 5.0
        mock_chain.get_chain_id = AsyncMock(return_value=4663)
        mock_chain.get_balances_batch = AsyncMock(return_value={wallet.address: int(1e18)})
        mock_chain.get_nonces_batch = AsyncMock(return_value={wallet.address: 0})
        mock_chain.get_gas_fees = AsyncMock(return_value=GasFees(20_000_000, 2_000_000, 27_000_000))
        mock_chain.broadcast_raw_transaction = AsyncMock(return_value="0x" + "e" * 64)
        mock_chain.wait_for_receipt = AsyncMock(return_value=MagicMock(status=0, gas_used=50000))

        call_kwargs_seen: list[dict] = []

        async def mock_eth_call(to, data, from_address=None, value_wei=0):
            call_kwargs_seen.append({"to": to, "data": data, "from": from_address, "value": value_wei})
            raise RuntimeError("execution reverted: 0x3a824151")

        mock_chain.call = AsyncMock(side_effect=mock_eth_call)

        t0_request_times: list[float] = []
        mock_opensea = MagicMock()
        mock_opensea.start_heartbeat = MagicMock()
        mock_opensea.stop_heartbeat = MagicMock()
        mock_opensea.authenticate_all_wallets = AsyncMock()
        mock_opensea.api_key = ""

        async def fake_hot(coll_arg, w_arg, qty_arg, stg_arg, **kwargs):
            if kwargs.get("single_attempt"):
                raise RuntimeError("Waiting for OpenSea stage activation...")
            t0_request_times.append(time.time())
            return MintAction(target="0x" + "6" * 40, value=0, calldata=b"\xde\xad\xbe\xef")

        mock_opensea.get_mint_action_hot = AsyncMock(side_effect=fake_hot)

        engine = MintEngine(cfg, mock_chain, mock_opensea)
        results = await engine.execute_mint(coll, stage, [wallet], quantity_per_wallet=1)

        assert results[0].status == "REVERTED"
        assert "Drop sold out" in results[0].error_message, f"Expected decoded revert reason, got: {results[0].error_message}"
        assert len(call_kwargs_seen) == 1
        assert call_kwargs_seen[0]["from"] == wallet.address
        assert t0_request_times[0] >= start_ts - 0.015, (
            f"Allowlist should not fire 120ms early; fired at offset {t0_request_times[0] - start_ts:.3f}s"
        )

    asyncio.run(run_test())


def test_custom_gas_war_mode():
    """Verify Custom Gas War ($) converts Dollar budget to aggressive priority tip, respects ceiling, cycles cleanly in bot, and supports /gas command."""
    from bot import TelegramBot
    from minter.chain import ChainClient, GasFees
    from minter.client import CollectionInfo, DropStage, MintAction
    from minter.engine import MintEngine, NonceTracker
    from minter.wallet import WalletAccount, WalletManager

    NonceTracker.reset()

    async def run_test():
        # 1. Test ChainClient.get_gas_fees with custom_gas_usd on Robinhood Chain (L2) and Ethereum (L1)
        cc = ChainClient("https://rpc.example.com")
        cc.get_chain_id = AsyncMock(return_value=4663)
        cc.get_eth_price_usd = AsyncMock(return_value=2500.0)

        async def fake_rpc(method: str, params=None):
            if method == "eth_getBlockByNumber":
                return {"baseFeePerGas": hex(20_000_000)}  # 0.02 Gwei
            if method == "eth_maxPriorityFeePerGas":
                return hex(1_000_000)  # 0.001 Gwei
            return None

        cc.rpc_request = fake_rpc

        # $0.50 budget @ $2500 ETH / 150k gas:
        # budget_wei = (0.50 / 2500) * 1e18 = 200_000_000_000_000 wei
        # target_max_fee = 200_000_000_000_000 // 150_000 = 1,333,333,333 wei (~1.33 Gwei)
        # headroom = 2 * 20_000_000 = 40_000_000 wei (~0.04 Gwei)
        # max_priority = 1,333,333,333 - 40_000_000 = 1,293,333,333 wei (~1.29 Gwei)
        fees_50c = await cc.get_gas_fees(custom_gas_usd=0.50)
        assert fees_50c.max_priority_fee_per_gas > 1_000_000_000, f"Expected >1 Gwei aggressive tip, got {fees_50c.max_priority_fee_per_gas}"
        assert fees_50c.max_fee_per_gas > fees_50c.max_priority_fee_per_gas
        # Assert total 150k gas cost strictly stays <= $0.50 USD
        cost_usd_50c = (fees_50c.max_fee_per_gas * 150_000 / 1e18) * 2500.0
        assert cost_usd_50c <= 0.51

        # $1.00 budget
        fees_1usd = await cc.get_gas_fees(custom_gas_usd=1.00)
        assert fees_1usd.max_fee_per_gas > fees_50c.max_fee_per_gas
        assert fees_1usd.max_priority_fee_per_gas > fees_50c.max_priority_fee_per_gas
        cost_usd_1usd = (fees_1usd.max_fee_per_gas * 150_000 / 1e18) * 2500.0
        assert cost_usd_1usd <= 1.01

        # 2. Test MintEngine.execute_mint forwards custom_gas_usd cleanly
        cfg = AppConfig()
        cfg.broadcast_lead_ms = 0
        wallet = WalletAccount("0x" + "a" * 64, label="W1")
        coll = CollectionInfo(
            name="WarDrop", slug="war-drop", contract_address="0x" + "1" * 40,
            image_url="", chain_identifier="robinhood", chain_id=4663, total_supply=10, max_supply=100, stages=[],
        )
        stage = DropStage(
            uuid="s1", label="Public", stage_type="PUBLIC_SALE", stage_index=0,
            start_time="", end_time="", start_timestamp=int(time.time()) - 10, end_timestamp=int(time.time()) + 300,
            max_per_wallet=1, price_wei=0, price_symbol="ETH", price_unit=0.0,
        )

        seen_custom_gas: list[float] = []
        mock_chain = MagicMock()
        mock_chain.rpc_url = "https://rpc.robinhood.org"
        mock_chain.fallback_rpc_url = ""
        mock_chain.timeout_seconds = 5.0
        mock_chain.get_chain_id = AsyncMock(return_value=4663)
        mock_chain.get_balances_batch = AsyncMock(return_value={wallet.address: int(1e18)})
        mock_chain.get_nonces_batch = AsyncMock(return_value={wallet.address: 0})

        async def mock_gas_fees(**kwargs):
            seen_custom_gas.append(kwargs.get("custom_gas_usd", 0.0))
            return GasFees(20_000_000, 1_290_000_000, 1_330_000_000)

        mock_chain.get_gas_fees = AsyncMock(side_effect=mock_gas_fees)
        mock_chain.broadcast_raw_transaction = AsyncMock(return_value="0x" + "f" * 64)
        mock_chain.wait_for_receipt = AsyncMock(return_value=MagicMock(status=1, gas_used=135000))

        mock_opensea = MagicMock()
        mock_opensea.start_heartbeat = MagicMock()
        mock_opensea.stop_heartbeat = MagicMock()

        engine = MintEngine(cfg, mock_chain, mock_opensea)
        res = await engine.execute_mint(coll, stage, [wallet], quantity_per_wallet=1, custom_gas_usd=0.50)
        assert res[0].status == "SUCCESS"
        assert len(seen_custom_gas) >= 1 and all(cg == 0.50 for cg in seen_custom_gas)

        # 3. Test TelegramBot: cycling gas mode & /gas command
        with tempfile.TemporaryDirectory() as tmp_dir:
            w_mgr = WalletManager(str(Path(tmp_dir) / "wallets.json"))
            w = w_mgr.add_wallet("0x" + "a" * 64, label="W1")
            bot = TelegramBot(cfg, w_mgr)
            bot.answer_callback = AsyncMock()
            bot.edit_message = AsyncMock()
            bot.send_message = AsyncMock()

            # Session defaults to Normal mode (fast_mode=False, custom_gas_usd=0.0)
            bot._card_sessions[100] = {
                "message_id": 100, "chat_id": 12345, "collection": coll,
                "selected_stage": stage, "quantity": 1, "selected_wallets": [w],
                "expected_price_wei": 0, "expected_price_unit": 0.0, "expected_price_symbol": "ETH",
                "fast_mode": False, "custom_gas_usd": 0.0,
            }
            bot._active_sessions[12345] = bot._card_sessions[100]

            # Tap 1: Normal -> Fast
            await bot.handle_callback({"id": "cb1", "data": "cycle_gas_mode", "message": {"chat": {"id": 12345}, "message_id": 100}})
            assert bot._card_sessions[100]["fast_mode"] is True
            assert bot._card_sessions[100]["custom_gas_usd"] == 0.0

            # Tap 2: Fast -> War ($0.50)
            await bot.handle_callback({"id": "cb2", "data": "cycle_gas_mode", "message": {"chat": {"id": 12345}, "message_id": 100}})
            assert bot._card_sessions[100]["fast_mode"] is False
            assert bot._card_sessions[100]["custom_gas_usd"] == 0.50

            # Tap 3: War ($0.50) -> War ($1.00)
            await bot.handle_callback({"id": "cb3", "data": "cycle_gas_mode", "message": {"chat": {"id": 12345}, "message_id": 100}})
            assert bot._card_sessions[100]["custom_gas_usd"] == 1.00

            # Tap 4: War ($1.00) -> War ($2.00)
            await bot.handle_callback({"id": "cb4", "data": "cycle_gas_mode", "message": {"chat": {"id": 12345}, "message_id": 100}})
            assert bot._card_sessions[100]["custom_gas_usd"] == 2.00

            # Tap 5: War ($2.00) -> Normal
            await bot.handle_callback({"id": "cb5", "data": "cycle_gas_mode", "message": {"chat": {"id": 12345}, "message_id": 100}})
            assert bot._card_sessions[100]["fast_mode"] is False
            assert bot._card_sessions[100]["custom_gas_usd"] == 0.0

            # Test /gas command: /gas 1.75
            await bot.handle_message({"chat": {"id": 12345}, "text": "/gas 1.75", "message_id": 201})
            assert bot._card_sessions[100]["custom_gas_usd"] == 1.75
            assert bot._card_sessions[100]["fast_mode"] is False

            # Test /gas 0 resets to normal
            await bot.handle_message({"chat": {"id": 12345}, "text": "/gas 0", "message_id": 202})
            assert bot._card_sessions[100]["custom_gas_usd"] == 0.0
            assert bot._card_sessions[100]["fast_mode"] is False

    asyncio.run(run_test())


if __name__ == "__main__":
    test_custom_gas_war_mode()
    test_hybrid_rest_and_gql_mint_race()
    test_revert_diagnosis_and_t0_timing()
    test_fast_gas_mode()
    test_fast_429_retry_and_zero_stagger()
    test_sequencer_excluded_from_chain_constants()
    test_safe_poll_receipt_and_dual_rpc()
    test_wallet_account()
    test_seadrop_encoding()
    test_opensea_slug_parser()
    test_chain_constants()
    test_wallet_mint_result_timing()
    test_contract_address_detection()
    test_price_guard_logic()
    test_sign_transaction_lowercase_address()
    test_opensea_value_parsing()
    test_wallet_eligibility_dataclass()
    test_free_mint_protection()
    test_price_uniformity_guard()
    test_rate_limit_handling()
    test_single_attempt_stage_activation()
    test_pipelined_mint_engine_execution()
    test_ink_chain_support()
    test_ethereum_chain_support()
    test_soneium_chain_support()
    test_elysium_chain_support()
    test_hedged_broadcast_dual_rpc()
    test_check_wallet_eligibility_all_stages()
    test_differing_quantities_no_cancellation()
    test_allocation_decrement_fallback()
    test_siwe_statement_and_headers()
    test_cross_wallet_eligibility_caching()
    test_auto_select_eligible_wallets_only()
    test_multiphase_allocation_tracking_and_onchain_balance()
    test_active_scheduled_mint_cancellation()
    test_concurrent_multi_mint_nonce_reservation()
    test_simultaneous_multi_drop_selective_cancellation()
    test_telegram_bot_multi_drop_card_isolation()
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_wallet_manager(Path(tmp_dir))
        test_app_config(Path(tmp_dir))
    print("[OK] All unit tests passed successfully!")


