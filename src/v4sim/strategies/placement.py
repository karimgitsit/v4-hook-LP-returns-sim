"""Shared LP-placement helpers.

Sizing a position to an exact USDC notional and pushing the liquidity on-chain
is needed in two places: the runner's initial world build, and an active
adapter re-centering its band on each rebalance. Factor it here so both use the
same math and there's one definition of "a $X position centered at price P".
"""

from __future__ import annotations

from v4sim.evm.env import V4Env
from v4sim.evm.pool import PoolKey, modify_liquidity
from v4sim.evm.tickmath import get_sqrt_price_at_tick
from v4sim.metrics.accounting import (
    amounts_for_liquidity,
    liquidity_for_amounts,
    price_token1_in_token0,
    usdc_value_of_position,
)
from v4sim.strategies.concentrated import band_ticks
from v4sim.strategies.full_range import MAX_SQRT_PRICE_X96, MIN_SQRT_PRICE_X96, usable_tick_range
from v4sim.strategies.hook_adapter import PositionState


def split_50_50_amounts(
    env: V4Env, sqrt_p_x96: int, notional_usdc: float, *, usdc_is_currency0: bool
) -> tuple[int, int]:
    """Raw (amount0, amount1) for a 50/50-by-USDC-value deposit at ``sqrt_p_x96``."""
    half = notional_usdc / 2.0
    p_raw = price_token1_in_token0(sqrt_p_x96)
    price_t1_per_t0_human = p_raw * (10 ** (env.decimals0 - env.decimals1))
    if usdc_is_currency0:
        amount0_raw = int(half * 10**env.decimals0)
        amount1_raw = int(half * price_t1_per_t0_human * 10**env.decimals1)
    else:
        amount1_raw = int(half * 10**env.decimals1)
        amount0_raw = int(half / price_t1_per_t0_human * 10**env.decimals0)
    return amount0_raw, amount1_raw


def _value(env: V4Env, amount0: int, amount1: int, sqrt_p_x96: int, *, usdc_is_currency0: bool) -> float:
    return usdc_value_of_position(
        amount0, amount1, sqrt_p_x96,
        decimals0=env.decimals0, decimals1=env.decimals1, usdc_is_token0=usdc_is_currency0,
    )


def liquidity_for_notional(
    env: V4Env,
    sqrt_p_x96: int,
    sqrt_a_x96: int,
    sqrt_b_x96: int,
    target_usdc: float,
    *,
    usdc_is_currency0: bool,
) -> int:
    """Liquidity whose value at ``sqrt_p_x96`` equals ``target_usdc``.

    Builds a 50/50 deposit, takes the liquidity that fits, then rescales (value
    is linear in L, so one rescale is exact).
    """
    amount0, amount1 = split_50_50_amounts(
        env, sqrt_p_x96, target_usdc, usdc_is_currency0=usdc_is_currency0
    )
    liquidity = liquidity_for_amounts(sqrt_p_x96, sqrt_a_x96, sqrt_b_x96, amount0, amount1)
    amt0, amt1 = amounts_for_liquidity(sqrt_p_x96, sqrt_a_x96, sqrt_b_x96, liquidity)
    value = _value(env, amt0, amt1, sqrt_p_x96, usdc_is_currency0=usdc_is_currency0)
    if value <= 0:
        raise ValueError(f"position value non-positive ({value})")
    return int(liquidity * target_usdc / value)


def band_bounds(
    center_tick: int, tick_spacing: int, band_pct: float | None
) -> tuple[int, int, int, int]:
    """(tick_lower, tick_upper, sqrt_a, sqrt_b) for a band around ``center_tick``.

    ``band_pct is None`` -> full range; otherwise a ±band_pct concentrated range.
    """
    if band_pct is None:
        lower, upper = usable_tick_range(tick_spacing)
        return lower, upper, MIN_SQRT_PRICE_X96, MAX_SQRT_PRICE_X96
    lower, upper = band_ticks(center_tick, tick_spacing, band_pct)
    return lower, upper, get_sqrt_price_at_tick(lower), get_sqrt_price_at_tick(upper)


def place_position(
    env: V4Env,
    key: PoolKey,
    *,
    center_tick: int,
    center_sqrt_x96: int,
    tick_spacing: int,
    band_pct: float | None,
    target_usdc: float,
    usdc_is_currency0: bool,
    salt: bytes = b"\x00" * 32,
) -> PositionState:
    """Place a band position sized to ``target_usdc`` and return its state.

    Pushes the liquidity on-chain via ``modify_liquidity`` and returns the
    :class:`PositionState` the runner snapshots / an adapter later mutates.
    """
    lower, upper, sqrt_a, sqrt_b = band_bounds(center_tick, tick_spacing, band_pct)
    liquidity = liquidity_for_notional(
        env, center_sqrt_x96, sqrt_a, sqrt_b, target_usdc, usdc_is_currency0=usdc_is_currency0
    )
    modify_liquidity(
        env, key, tick_lower=lower, tick_upper=upper, liquidity_delta=liquidity, salt=salt
    )
    return PositionState(
        tick_lower=lower, tick_upper=upper, sqrt_a_x96=sqrt_a, sqrt_b_x96=sqrt_b,
        liquidity=liquidity, salt=salt,
    )
