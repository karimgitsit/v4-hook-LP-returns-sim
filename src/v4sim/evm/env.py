"""pyrevm-based v4 harness bootstrap.

`bootstrap_v4()` deploys a fresh PoolManager, two MockERC20 tokens (sorted as
currency0/currency1), and the test routers PoolSwapTest and
PoolModifyLiquidityTest. It also mints a large balance of each token to the
deployer EOA and approves the routers, mirroring v4-core's
Deployers.deployMintAndApprove2Currencies() pattern.

Returns a typed `V4Env` snapshot that downstream code (pool.py, runner.py)
uses without needing to know about pyrevm.
"""

from __future__ import annotations

from dataclasses import dataclass

import pyrevm
from eth_abi import encode as abi_encode

from .artifacts import bytecode

DEPLOYER = "0x000000000000000000000000000000000000abcd"
INITIAL_TOKEN_SUPPLY = 2**200  # plenty; sortable as uint256
DEFAULT_GAS_LIMIT = 30_000_000


@dataclass
class V4Env:
    evm: pyrevm.EVM
    deployer: str
    manager: str
    swap_router: str
    modify_liquidity_router: str
    currency0: str
    currency1: str

    def call(self, to: str, calldata: bytes, caller: str | None = None) -> bytes:
        """Execute a state-changing call; raise on revert."""
        result = self.evm.message_call(
            caller=caller or self.deployer,
            to=to,
            calldata=calldata,
            gas=DEFAULT_GAS_LIMIT,
        )
        return result if isinstance(result, (bytes, bytearray)) else bytes(result)


def _selector(signature: str) -> bytes:
    from eth_utils import keccak

    return keccak(signature.encode())[:4]


def _deploy(evm: pyrevm.EVM, deployer: str, code: bytes) -> str:
    addr = evm.deploy(deployer, code, gas=DEFAULT_GAS_LIMIT)
    if not addr:
        raise RuntimeError("deploy returned empty address")
    return addr


def _deploy_mock_erc20(evm: pyrevm.EVM, deployer: str, name: str, symbol: str) -> str:
    """Deploy mocks/MockERC20 with ctor (name, symbol, decimals=18) and mint to deployer."""
    code = bytecode("mocks/MockERC20.sol", "MockERC20")
    ctor_args = abi_encode(["string", "string", "uint8"], [name, symbol, 18])
    addr = _deploy(evm, deployer, code + ctor_args)
    # mint(address to, uint256 amount)
    calldata = _selector("mint(address,uint256)") + abi_encode(
        ["address", "uint256"], [deployer, INITIAL_TOKEN_SUPPLY]
    )
    evm.message_call(caller=deployer, to=addr, calldata=calldata, gas=DEFAULT_GAS_LIMIT)
    return addr


def _approve(evm: pyrevm.EVM, deployer: str, token: str, spender: str) -> None:
    calldata = _selector("approve(address,uint256)") + abi_encode(
        ["address", "uint256"], [spender, 2**256 - 1]
    )
    evm.message_call(caller=deployer, to=token, calldata=calldata, gas=DEFAULT_GAS_LIMIT)


def bootstrap_v4() -> V4Env:
    """Deploy a fresh v4 environment and return handles."""
    evm = pyrevm.EVM()
    deployer = DEPLOYER
    evm.set_balance(deployer, 10**30)

    # PoolManager(address initialOwner)
    manager_code = bytecode("PoolManager.sol")
    manager_ctor = abi_encode(["address"], [deployer])
    manager = _deploy(evm, deployer, manager_code + manager_ctor)

    # Routers take the manager address in their constructor.
    swap_router_code = bytecode("PoolSwapTest.sol")
    swap_router_ctor = abi_encode(["address"], [manager])
    swap_router = _deploy(evm, deployer, swap_router_code + swap_router_ctor)

    modify_liq_code = bytecode("PoolModifyLiquidityTest.sol")
    modify_liq_ctor = abi_encode(["address"], [manager])
    modify_liquidity_router = _deploy(evm, deployer, modify_liq_code + modify_liq_ctor)

    # Two mock ERC20s, sorted by address so currency0 < currency1.
    token_a = _deploy_mock_erc20(evm, deployer, "TokenA", "TKA")
    token_b = _deploy_mock_erc20(evm, deployer, "TokenB", "TKB")
    currency0, currency1 = sorted([token_a, token_b], key=lambda a: int(a, 16))

    for token in (currency0, currency1):
        _approve(evm, deployer, token, swap_router)
        _approve(evm, deployer, token, modify_liquidity_router)

    return V4Env(
        evm=evm,
        deployer=deployer,
        manager=manager,
        swap_router=swap_router,
        modify_liquidity_router=modify_liquidity_router,
        currency0=currency0,
        currency1=currency1,
    )
