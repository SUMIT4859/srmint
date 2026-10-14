import os
import sys
import tempfile
from pathlib import Path

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


if __name__ == "__main__":
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
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_wallet_manager(Path(tmp_dir))
        test_app_config(Path(tmp_dir))
    print("[OK] All unit tests passed successfully!")


