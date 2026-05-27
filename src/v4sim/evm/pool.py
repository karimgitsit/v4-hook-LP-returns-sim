"""High-level v4 pool operations on top of pyrevm.

Wraps the awkward struct-encoded calls (initialize / modifyLiquidity / swap)
behind plain Python signatures and decodes the PoolManager state slot back to
(sqrtPriceX96, tick, protocolFee, lpFee).
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_abi import encode as abi_encode
from eth_utils import keccak

from .env import DEFAULT_GAS_LIMIT, V4Env

ZERO_ADDRESS = "0x" + "00" * 20
ZERO_BYTES = b""

# v4 TickMath bounds: MIN_SQRT_PRICE = 4295128739, MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342
MIN_SQRT_PRICE = 4295128739
MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342
MIN_PRICE_LIMIT = MIN_SQRT_PRICE + 1
MAX_PRICE_LIMIT = MAX_SQRT_PRICE - 1

# PoolManager storage slot for `pools` mapping (see StateLibrary.POOLS_SLOT).
POOLS_SLOT = 6

# Tuple types reused across calls.
POOL_KEY_T = "(address,address,uint24,int24,address)"
MODIFY_LIQ_T = "(int24,int24,int256,bytes32)"
SWAP_PARAMS_T = "(bool,int256,uint160)"
TEST_SETTINGS_T = "(bool,bool)"


@dataclass(frozen=True)
class PoolKey:
    currency0: str
    currency1: str
    fee: int
    tick_spacing: int
    hooks: str = ZERO_ADDRESS

    def as_tuple(self) -> tuple:
        return (self.currency0, self.currency1, self.fee, self.tick_spacing, self.hooks)

    def pool_id(self) -> bytes:
        return keccak(abi_encode([POOL_KEY_T], [self.as_tuple()]))


@dataclass(frozen=True)
class Slot0:
    sqrt_price_x96: int
    tick: int
    protocol_fee: int
    lp_fee: int


def _selector(signature: str) -> bytes:
    return keccak(signature.encode())[:4]


def initialize(env: V4Env, key: PoolKey, sqrt_price_x96: int) -> int:
    """Call PoolManager.initialize(key, sqrtPriceX96). Returns the resulting tick."""
    calldata = _selector(f"initialize({POOL_KEY_T},uint160)") + abi_encode(
        [POOL_KEY_T, "uint160"], [key.as_tuple(), sqrt_price_x96]
    )
    out = env.call(env.manager, calldata)
    # returns int24 — solidity right-pads to 32 bytes
    tick = int.from_bytes(out[-3:], "big", signed=True) if out else 0
    return tick


def modify_liquidity(
    env: V4Env,
    key: PoolKey,
    tick_lower: int,
    tick_upper: int,
    liquidity_delta: int,
    salt: bytes = b"\x00" * 32,
    hook_data: bytes = ZERO_BYTES,
) -> bytes:
    """Call PoolModifyLiquidityTest.modifyLiquidity(key, params, hookData) — the 3-arg overload."""
    if len(salt) != 32:
        raise ValueError("salt must be 32 bytes")
    calldata = _selector(
        f"modifyLiquidity({POOL_KEY_T},{MODIFY_LIQ_T},bytes)"
    ) + abi_encode(
        [POOL_KEY_T, MODIFY_LIQ_T, "bytes"],
        [
            key.as_tuple(),
            (tick_lower, tick_upper, liquidity_delta, salt),
            hook_data,
        ],
    )
    return env.call(env.modify_liquidity_router, calldata)


def swap(
    env: V4Env,
    key: PoolKey,
    zero_for_one: bool,
    amount_specified: int,
    sqrt_price_limit_x96: int | None = None,
    *,
    take_claims: bool = False,
    settle_using_burn: bool = False,
    hook_data: bytes = ZERO_BYTES,
) -> bytes:
    """Call PoolSwapTest.swap(key, params, settings, hookData)."""
    if sqrt_price_limit_x96 is None:
        sqrt_price_limit_x96 = MIN_PRICE_LIMIT if zero_for_one else MAX_PRICE_LIMIT
    calldata = _selector(
        f"swap({POOL_KEY_T},{SWAP_PARAMS_T},{TEST_SETTINGS_T},bytes)"
    ) + abi_encode(
        [POOL_KEY_T, SWAP_PARAMS_T, TEST_SETTINGS_T, "bytes"],
        [
            key.as_tuple(),
            (zero_for_one, amount_specified, sqrt_price_limit_x96),
            (take_claims, settle_using_burn),
            hook_data,
        ],
    )
    return env.call(env.swap_router, calldata)


def read_slot0(env: V4Env, key: PoolKey) -> Slot0:
    """Read PoolManager pool state slot for `key` and decode the packed slot0 word.

    Layout (see v4-core StateLibrary.getSlot0):
        bits   0-159 : uint160 sqrtPriceX96
        bits 160-183 : int24  tick (signed)
        bits 184-207 : uint24 protocolFee
        bits 208-231 : uint24 lpFee
    """
    pool_id = key.pool_id()
    state_slot = keccak(pool_id + POOLS_SLOT.to_bytes(32, "big"))
    raw = env.evm.storage(env.manager, int.from_bytes(state_slot, "big"))
    word = raw if isinstance(raw, int) else int.from_bytes(raw, "big")
    sqrt = word & ((1 << 160) - 1)
    tick_raw = (word >> 160) & ((1 << 24) - 1)
    # sign-extend 24-bit two's complement to Python int
    tick = tick_raw - (1 << 24) if tick_raw & (1 << 23) else tick_raw
    protocol_fee = (word >> 184) & ((1 << 24) - 1)
    lp_fee = (word >> 208) & ((1 << 24) - 1)
    return Slot0(sqrt_price_x96=sqrt, tick=tick, protocol_fee=protocol_fee, lp_fee=lp_fee)
