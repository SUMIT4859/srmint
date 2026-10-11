"""SeaDrop V1 on-chain calldata encoder and response parser."""

from __future__ import annotations

from dataclasses import dataclass
from eth_abi import decode, encode
from eth_utils.address import to_checksum_address

from .constants import (
    ERC721_PUBLIC_MINT_SELECTOR,
    GET_MINT_STATS_SELECTOR,
    GET_PUBLIC_DROP_SELECTOR,
    OPENSEA_FEE_RECIPIENT,
    OPENSEA_SEADROP_ADDRESS,
)


@dataclass
class OnChainPublicDrop:
    mint_price: int
    start_time: int
    end_time: int
    max_total_mintable_by_wallet: int
    fee_bps: int
    restrict_fee_recipients: bool


@dataclass
class OnChainMintStats:
    minter_num_minted: int
    current_total_supply: int
    max_supply: int


def encode_public_mint(
    nft_contract: str,
    minter_recipient: str,
    quantity: int,
    fee_recipient: str = OPENSEA_FEE_RECIPIENT,
) -> bytes:
    """Encode direct mintPublic(address,address,address,uint256) SeaDrop calldata.
    
    This executes 100% on-chain with zero GraphQL latency.
    """
    args = [
        to_checksum_address(nft_contract),
        to_checksum_address(fee_recipient),
        to_checksum_address(minter_recipient),
        quantity,
    ]
    encoded_args = encode(["address", "address", "address", "uint256"], args)
    return ERC721_PUBLIC_MINT_SELECTOR + encoded_args


def encode_get_public_drop(nft_contract: str) -> str:
    encoded_args = encode(["address"], [to_checksum_address(nft_contract)])
    return "0x" + (GET_PUBLIC_DROP_SELECTOR + encoded_args).hex()


def decode_get_public_drop(raw_hex: str) -> OnChainPublicDrop:
    data = bytes.fromhex(raw_hex.lstrip("0x"))
    decoded = decode(["uint80", "uint48", "uint48", "uint16", "uint16", "bool"], data)
    return OnChainPublicDrop(
        mint_price=decoded[0],
        start_time=decoded[1],
        end_time=decoded[2],
        max_total_mintable_by_wallet=decoded[3],
        fee_bps=decoded[4],
        restrict_fee_recipients=decoded[5],
    )


def encode_get_mint_stats(minter: str) -> str:
    encoded_args = encode(["address"], [to_checksum_address(minter)])
    return "0x" + (GET_MINT_STATS_SELECTOR + encoded_args).hex()


def decode_get_mint_stats(raw_hex: str) -> OnChainMintStats:
    data = bytes.fromhex(raw_hex.lstrip("0x"))
    decoded = decode(["uint256", "uint256", "uint256"], data)
    return OnChainMintStats(
        minter_num_minted=decoded[0],
        current_total_supply=decoded[1],
        max_supply=decoded[2],
    )

