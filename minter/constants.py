"""Constants for OpenSea SeaDrop V1 contracts, functions, and EVM chains."""

from eth_utils.address import to_checksum_address

OPENSEA_SEADROP_ADDRESS = to_checksum_address("0x00005EA00Ac477B1030CE78506496e8C2dE24bf5")
OPENSEA_FEE_RECIPIENT = to_checksum_address("0x0000a26b00c1F0DF003000390027140000fAa719")
MULTICALL3_ADDRESS = to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")

# SeaDrop function selectors
# mintPublic(address nftContract, address feeRecipient, address minterIfNotPayer, uint256 quantity)
ERC721_PUBLIC_MINT_SELECTOR = bytes.fromhex("161ac21f")

# getPublicDrop(address nftContract)
GET_PUBLIC_DROP_SELECTOR = bytes.fromhex("bc6a629c")

# getMintStats(address minter) on NFT contract
GET_MINT_STATS_SELECTOR = bytes.fromhex("840e15d4")

# Multicall3 aggregate3 selector
MULTICALL3_AGGREGATE3_SELECTOR = bytes.fromhex("82ad56cb")

# OpenSea Site and API URLs
SITE_URL = "https://opensea.io"
GRAPHQL_URL = "https://gql.opensea.io/graphql"
APP_ID = "os2-web"

SIWE_STATEMENT = (
    "Click to sign in and accept the OpenSea Terms of Service "
    "(https://opensea.io/tos) and Privacy Policy (https://opensea.io/privacy)."
)

# EVM Chains Information
CHAINS = {
    1: {
        "name": "Ethereum Mainnet",
        "symbol": "ETH",
        "explorer": "https://etherscan.io",
        "default_rpc": "https://eth.llamarpc.com",
    },
    8453: {
        "name": "Base",
        "symbol": "ETH",
        "explorer": "https://basescan.org",
        "default_rpc": "https://mainnet.base.org",
    },
    137: {
        "name": "Polygon",
        "symbol": "POL",
        "explorer": "https://polygonscan.com",
        "default_rpc": "https://polygon-rpc.com",
    },
    42161: {
        "name": "Arbitrum One",
        "symbol": "ETH",
        "explorer": "https://arbiscan.io",
        "default_rpc": "https://arb1.arbitrum.io/rpc",
    },
    10: {
        "name": "Optimism",
        "symbol": "ETH",
        "explorer": "https://optimistic.etherscan.io",
        "default_rpc": "https://mainnet.optimism.io",
    },
    7777777: {
        "name": "Zora",
        "symbol": "ETH",
        "explorer": "https://explorer.zora.energy",
        "default_rpc": "https://rpc.zora.energy",
    },
    81457: {
        "name": "Blast",
        "symbol": "ETH",
        "explorer": "https://blastscan.io",
        "default_rpc": "https://rpc.blast.io",
    },
    11155111: {
        "name": "Sepolia Testnet",
        "symbol": "ETH",
        "explorer": "https://sepolia.etherscan.io",
        "default_rpc": "https://rpc.sepolia.org",
    },
    4663: {
        "name": "Robinhood Chain",
        "symbol": "ETH",
        "explorer": "https://robinhoodchain.blockscout.com",
        "default_rpc": "https://rpc.mainnet.chain.robinhood.com",
    },
}

def get_chain_info(chain_id: int) -> dict:
    return CHAINS.get(
        chain_id,
        {
            "name": f"Chain {chain_id}",
            "symbol": "ETH",
            "explorer": "https://etherscan.io",
            "default_rpc": "",
        },
    )

def get_tx_explorer_url(chain_id: int, tx_hash: str) -> str:
    explorer = get_chain_info(chain_id)["explorer"]
    return f"{explorer}/tx/{tx_hash}"

