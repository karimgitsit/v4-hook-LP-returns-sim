"""Vanilla v4 concentrated LP baseline.

A single position spanning ±`band_pct` around the pool's initial price
(default ±10%), sized so the deposit is 50/50 by USDC value at that price.

Like the full-range baseline this is passive — it never rebalances. When
price exits the band the position goes 100% into one token and earns no
fees until price re-enters, which is exactly the risk a concentrated LP
takes and the thing a good hook is supposed to manage.
"""

from __future__ import annotations

from v4sim.evm.env import V4Env
from v4sim.evm.pool import PoolKey, modify_liquidity
from v4sim.evm.tickmath import (
    align_tick,
    get_sqrt_price_at_tick,
    tick_at_price_ratio,
)
from v4sim.metrics.accounting import liquidity_for_amounts

DEFAULT_BAND_PCT = 0.10


def band_ticks(init_tick: int, tick_spacing: int, band_pct: float) -> tuple[int, int]:
    """Aligned [lower, upper] tick bounds for a ±band_pct window around init_tick.

    Lower edge rounds down, upper edge rounds up, so the band is at least as
    wide as requested.
    """
    if not 0 < band_pct < 1:
        raise ValueError(f"band_pct must be in (0, 1), got {band_pct}")
    lower = align_tick(tick_at_price_ratio(init_tick, 1.0 - band_pct), tick_spacing, round_up=False)
    upper = align_tick(tick_at_price_ratio(init_tick, 1.0 + band_pct), tick_spacing, round_up=True)
    return lower, upper


def add_concentrated_position(
    env: V4Env,
    key: PoolKey,
    sqrt_price_x96: int,
    init_tick: int,
    *,
    amount0: int,
    amount1: int,
    band_pct: float = DEFAULT_BAND_PCT,
    tick_spacing: int | None = None,
) -> tuple[int, int, int, int, int]:
    """Add a ±band_pct concentrated position at the current price.

    Returns (liquidity_added, tick_lower, tick_upper, sqrt_a_x96, sqrt_b_x96).
    The sqrt bounds are the exact TickMath values for the aligned ticks, so
    the caller can value the position consistently with the pool.
    """
    ts = tick_spacing if tick_spacing is not None else key.tick_spacing
    lower, upper = band_ticks(init_tick, ts, band_pct)
    sqrt_a = get_sqrt_price_at_tick(lower)
    sqrt_b = get_sqrt_price_at_tick(upper)

    liquidity = liquidity_for_amounts(
        sqrt_p_x96=sqrt_price_x96,
        sqrt_a_x96=sqrt_a,
        sqrt_b_x96=sqrt_b,
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
    return liquidity, lower, upper, sqrt_a, sqrt_b
