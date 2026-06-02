"""Vanilla v4 full-range LP baseline.

A single position spanning the usable tick range, sized so the
deposit is 50/50 (by USDC value) at the pool's initial price.

`add_full_range_position` is the only entry point; it returns the
liquidity that was added so the caller can snapshot position state
later (no on-chain position-tracking lookup needed since the runner
holds the only position via `PoolModifyLiquidityTest`).
"""

from __future__ import annotations

from v4sim.evm.env import V4Env
from v4sim.evm.pool import MAX_SQRT_PRICE, MIN_SQRT_PRICE, PoolKey, modify_liquidity
from v4sim.metrics.accounting import liquidity_for_amounts

# v4 TickMath bounds (Solidity constants).
MIN_TICK = -887272
MAX_TICK = 887272

# Sqrt-price bounds at the tick boundaries above.
MIN_SQRT_PRICE_X96 = MIN_SQRT_PRICE
MAX_SQRT_PRICE_X96 = MAX_SQRT_PRICE


def usable_tick_range(tick_spacing: int) -> tuple[int, int]:
    """Largest tick range a position can span for the given spacing.

    Positions in v4 must have tickLower/tickUpper aligned to tickSpacing,
    so we take the strictest multiples of `tick_spacing` strictly inside
    [MIN_TICK, MAX_TICK].
    """
    if tick_spacing <= 0:
        raise ValueError(f"tick_spacing must be positive (got {tick_spacing})")
    lower = -(MIN_TICK // -tick_spacing) * tick_spacing  # ceil(MIN_TICK / spacing) * spacing
    upper = (MAX_TICK // tick_spacing) * tick_spacing
    return lower, upper


def add_full_range_position(
    env: V4Env,
    key: PoolKey,
    sqrt_price_x96: int,
    *,
    amount0: int,
    amount1: int,
    tick_spacing: int | None = None,
) -> tuple[int, int, int]:
    """Add a full-range LP position sized by (amount0, amount1) at current price.

    Returns (liquidity_added, tick_lower, tick_upper).

    The (amount0, amount1) ratio should be ≈ pool price for the LP to be
    fully utilised; otherwise the smaller side bounds liquidity (standard
    Uniswap behaviour — the excess of the larger side stays in the wallet).
    """
    ts = tick_spacing if tick_spacing is not None else key.tick_spacing
    lower, upper = usable_tick_range(ts)

    liquidity = liquidity_for_amounts(
        sqrt_p_x96=sqrt_price_x96,
        sqrt_a_x96=MIN_SQRT_PRICE_X96,
        sqrt_b_x96=MAX_SQRT_PRICE_X96,
        amount0=amount0,
        amount1=amount1,
    )
    if liquidity <= 0:
        raise ValueError(f"computed liquidity is non-positive ({liquidity})")

    modify_liquidity(
        env,
        key,
        tick_lower=lower,
        tick_upper=upper,
        liquidity_delta=liquidity,
    )
    return liquidity, lower, upper
