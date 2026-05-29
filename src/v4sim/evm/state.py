"""Read v4 PoolManager fee-growth state straight from storage.

A Python port of the parts of v4-core's ``StateLibrary`` we need for fee
attribution. Everything here is a pure ``extsload``-equivalent storage read
(via ``pyrevm.EVM.storage``) — no transaction is sent, so the pool is never
perturbed. This is the seam that lets us value LP fees without poking
``modifyLiquidity(0)``.

Fee growth is tracked as Q128.128 fixed point and all on-chain arithmetic on
these values is ``unchecked`` (mod 2^256); we mirror that with ``MASK256`` so
the boundary-subtraction in :func:`read_fee_growth_inside` matches the
contract exactly.
"""

from __future__ import annotations

from eth_utils import keccak

from .env import V4Env
from .pool import POOLS_SLOT, PoolKey

# Offsets within Pool.State (see StateLibrary):
#   slot0           @ +0   (read in pool.read_slot0)
#   feeGrowthGlobal0@ +1
#   feeGrowthGlobal1@ +2
#   liquidity       @ +3
#   ticks mapping   @ +4
#   tickBitmap      @ +5
#   positions       @ +6
FEE_GROWTH_GLOBAL0_OFFSET = 1
LIQUIDITY_OFFSET = 3
TICKS_OFFSET = 4
POSITIONS_OFFSET = 6

MASK256 = (1 << 256) - 1
Q128 = 1 << 128


def _read_word(env: V4Env, addr: str, slot: int) -> int:
    """Read one 32-byte storage word as an unsigned int."""
    raw = env.evm.storage(addr, slot & MASK256)
    return raw if isinstance(raw, int) else int.from_bytes(raw, "big")


def _pool_state_slot(key: PoolKey) -> int:
    """keccak(poolId ++ POOLS_SLOT) — the base slot of pools[poolId]."""
    digest = keccak(key.pool_id() + POOLS_SLOT.to_bytes(32, "big"))
    return int.from_bytes(digest, "big")


def _tick_info_slot(key: PoolKey, tick: int) -> int:
    """keccak(int256(tick) ++ (stateSlot + TICKS_OFFSET)) — base slot of ticks[tick]."""
    ticks_mapping_slot = (_pool_state_slot(key) + TICKS_OFFSET) & MASK256
    tick_key = tick.to_bytes(32, "big", signed=True)
    digest = keccak(tick_key + ticks_mapping_slot.to_bytes(32, "big"))
    return int.from_bytes(digest, "big")


def _position_slot(key: PoolKey, owner: str, tick_lower: int, tick_upper: int, salt: bytes) -> int:
    """keccak(positionKey ++ (stateSlot + POSITIONS_OFFSET)).

    positionKey = keccak(owner[20] ++ int24(tickLower) ++ int24(tickUpper) ++ salt[32]).
    """
    owner_bytes = bytes.fromhex(owner[2:] if owner.startswith("0x") else owner)
    packed = (
        owner_bytes
        + tick_lower.to_bytes(3, "big", signed=True)
        + tick_upper.to_bytes(3, "big", signed=True)
        + salt
    )
    position_key = keccak(packed)
    positions_mapping_slot = (_pool_state_slot(key) + POSITIONS_OFFSET) & MASK256
    digest = keccak(position_key + positions_mapping_slot.to_bytes(32, "big"))
    return int.from_bytes(digest, "big")


def read_liquidity(env: V4Env, key: PoolKey) -> int:
    """Pool-wide active liquidity (uint128)."""
    return _read_word(env, env.manager, _pool_state_slot(key) + LIQUIDITY_OFFSET) & ((1 << 128) - 1)


def read_fee_growth_globals(env: V4Env, key: PoolKey) -> tuple[int, int]:
    """(feeGrowthGlobal0X128, feeGrowthGlobal1X128) — pool-wide fees per liquidity."""
    base = _pool_state_slot(key) + FEE_GROWTH_GLOBAL0_OFFSET
    return _read_word(env, env.manager, base), _read_word(env, env.manager, base + 1)


def read_tick_fee_growth_outside(env: V4Env, key: PoolKey, tick: int) -> tuple[int, int]:
    """(feeGrowthOutside0X128, feeGrowthOutside1X128) at ``tick``.

    TickInfo layout: word0 packs liquidityNet/liquidityGross; word+1/+2 hold the
    two feeGrowthOutside values.
    """
    base = _tick_info_slot(key, tick)
    return _read_word(env, env.manager, base + 1), _read_word(env, env.manager, base + 2)


def read_fee_growth_inside(
    env: V4Env, key: PoolKey, tick_lower: int, tick_upper: int, current_tick: int
) -> tuple[int, int]:
    """(feeGrowthInside0X128, feeGrowthInside1X128) for [tick_lower, tick_upper).

    Exact port of StateLibrary.getFeeGrowthInside: subtract the boundary ticks'
    feeGrowthOutside from the global, branching on where the current tick sits.
    All subtraction is mod 2^256 to mirror the contract's unchecked math.
    """
    g0, g1 = read_fee_growth_globals(env, key)
    lo0, lo1 = read_tick_fee_growth_outside(env, key, tick_lower)
    up0, up1 = read_tick_fee_growth_outside(env, key, tick_upper)
    if current_tick < tick_lower:
        inside0 = lo0 - up0
        inside1 = lo1 - up1
    elif current_tick >= tick_upper:
        inside0 = up0 - lo0
        inside1 = up1 - lo1
    else:
        inside0 = g0 - lo0 - up0
        inside1 = g1 - lo1 - up1
    return inside0 & MASK256, inside1 & MASK256


def read_position_fee_growth_inside_last(
    env: V4Env,
    key: PoolKey,
    owner: str,
    tick_lower: int,
    tick_upper: int,
    salt: bytes = b"\x00" * 32,
) -> tuple[int, int, int]:
    """(liquidity, feeGrowthInside0LastX128, feeGrowthInside1LastX128) for a position.

    Position.State layout: word0 = liquidity (uint128), word+1/+2 = the cached
    feeGrowthInsideLast values captured at the last poke.
    """
    base = _position_slot(key, owner, tick_lower, tick_upper, salt)
    liquidity = _read_word(env, env.manager, base) & ((1 << 128) - 1)
    last0 = _read_word(env, env.manager, base + 1)
    last1 = _read_word(env, env.manager, base + 2)
    return liquidity, last0, last1


def uncollected_fees_raw(
    env: V4Env,
    key: PoolKey,
    *,
    owner: str,
    tick_lower: int,
    tick_upper: int,
    current_tick: int,
    salt: bytes = b"\x00" * 32,
) -> tuple[int, int]:
    """Uncollected (token0, token1) fees for an LP position, in raw token units.

    fees = liquidity * (feeGrowthInside_now - feeGrowthInside_last) >> 128, with
    the difference taken mod 2^256 (Uniswap's FullMath semantics). Reads the
    position's own liquidity and cached ``feeGrowthInsideLast`` so this is
    correct even after a rebalance poke (where Last != 0).
    """
    liquidity, last0, last1 = read_position_fee_growth_inside_last(
        env, key, owner, tick_lower, tick_upper, salt
    )
    inside0, inside1 = read_fee_growth_inside(env, key, tick_lower, tick_upper, current_tick)
    fees0 = (liquidity * ((inside0 - last0) & MASK256)) >> 128
    fees1 = (liquidity * ((inside1 - last1) & MASK256)) >> 128
    return fees0, fees1
