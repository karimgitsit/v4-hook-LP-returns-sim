"""Step-4 gate test: with arb-to-truth on, the v4 pool's tick tracks the
v3 source tick within 1 unit after every swap, and the cumulative arb PnL
is recorded.

Skips when the cached parquet is missing — synthetic data can't really
exercise this gate because we'd need consistent v3-style truth prices.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from v4sim.evm.arb import arb_to_target
from v4sim.evm.artifacts import ArtifactNotFound
from v4sim.evm.env import bootstrap_v4_eth_usdc
from v4sim.evm.pool import (
    PoolKey,
    initialize,
    modify_liquidity,
    read_slot0,
    swap,
)
from v4sim.replay.runner import replay_full_range
from v4sim.strategies.full_range import MAX_SQRT_PRICE_X96, MIN_SQRT_PRICE_X96

REPO_ROOT = Path(__file__).resolve().parents[1]
PARQUET = REPO_ROOT / "data" / "cache" / "swaps_88e6a0c2.parquet"


def _bootstrap_or_skip():
    try:
        return bootstrap_v4_eth_usdc()
    except ArtifactNotFound as e:
        pytest.skip(str(e))


def test_arb_to_target_round_trips_to_within_one_tick():
    """Direct unit test: small user swap then arb back exactly to start."""
    env = _bootstrap_or_skip()
    key = PoolKey(env.currency0, env.currency1, fee=500, tick_spacing=10)
    start_sp = 1715504086711932526946299007354273
    initialize(env, key, start_sp)
    modify_liquidity(env, key, tick_lower=-887270, tick_upper=887270, liquidity_delta=10**16)
    start_tick = read_slot0(env, key).tick

    swap(env, key, zero_for_one=True, amount_specified=-(10**9))
    moved_tick = read_slot0(env, key).tick
    assert moved_tick != start_tick, "user swap should have moved the pool"

    result = arb_to_target(env, key, start_sp)
    assert not result.skipped
    final = read_slot0(env, key)
    assert abs(final.tick - start_tick) <= 1, (start_tick, final.tick)
    # Arb sold WETH-bought-cheap back at truth: PnL signs should differ.
    assert (result.delta0 > 0 and result.delta1 < 0) or (result.delta0 < 0 and result.delta1 > 0)


@pytest.mark.skipif(not PARQUET.exists(), reason="data/cache/swaps_88e6a0c2.parquet missing")
def test_replay_with_arb_tracks_truth_within_one_tick():
    """Replay 100 real swaps; after each arb, pool tick must equal v3 tick ±1."""
    env = _bootstrap_or_skip()
    df = pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(100)

    # Manually walk the loop to assert tick-tracking after every arb. This
    # duplicates a little of replay_full_range but lets us check intermediate
    # state, which the runner doesn't expose.
    from v4sim.replay.runner import (
        _harness_zero_for_one,
        _parse_sqrt_price,
    )

    rows = df.to_dicts()
    key = PoolKey(env.currency0, env.currency1, fee=500, tick_spacing=10)
    initialize(env, key, _parse_sqrt_price(rows[0]["sqrt_price_x96_post"]))
    # Hefty LP so swaps don't blow past the price limit at edges.
    modify_liquidity(env, key, tick_lower=-887270, tick_upper=887270, liquidity_delta=10**17)
    usdc_is_currency0 = env.decimals0 == 6

    worst_tick_err = 0
    for row in rows[1:]:
        z4o = _harness_zero_for_one(int(row["dir"]), usdc_is_currency0)
        amt_dec = env.decimals0 if z4o else env.decimals1
        amt_in = int(float(row["amount_in"]) * 10**amt_dec)
        if amt_in <= 0:
            continue
        swap(env, key, zero_for_one=z4o, amount_specified=-amt_in)
        arb_to_target(env, key, _parse_sqrt_price(row["sqrt_price_x96_post"]))
        post_tick = read_slot0(env, key).tick
        worst_tick_err = max(worst_tick_err, abs(post_tick - int(row["tick_post"])))

    assert worst_tick_err <= 1, f"worst v4-vs-v3 tick mismatch was {worst_tick_err}"


@pytest.mark.skipif(not PARQUET.exists(), reason="data/cache/swaps_88e6a0c2.parquet missing")
def test_replay_full_range_with_arb_accumulates_extraction():
    env = _bootstrap_or_skip()
    df = pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(500)
    result = replay_full_range(
        df,
        snapshot_period_s=3600,
        lp_notional_usdc=1_000_000.0,
        env=env,
        arb_to_truth=True,
    )
    # Real-data arb should have happened and extracted a positive value.
    assert result.arbs_executed >= 1
    assert result.cum_arb_extracted_usdc > 0, result.cum_arb_extracted_usdc
    # With arb on, the pool tracks truth, so LP underlying value at truth
    # should be near initial (small drift OK, but not wild like step-3 mode).
    assert 0.5e6 <= result.final_lp_value_usdc <= 1.5e6, result.final_lp_value_usdc
    # Cumulative extraction appears in every snapshot.
    assert "cum_arb_extracted_usdc" in result.snapshots.columns
    assert result.snapshots["cum_arb_extracted_usdc"][-1] == pytest.approx(
        result.cum_arb_extracted_usdc
    )


def test_no_arb_mode_matches_step3_behaviour():
    """Sanity: arb_to_truth=False reproduces step-3 drift."""
    env = _bootstrap_or_skip()
    if not PARQUET.exists():
        pytest.skip("parquet missing")
    df = pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(200)
    result = replay_full_range(df, env=env, arb_to_truth=False)
    assert result.arbs_executed == 0
    assert result.cum_arb_extracted_usdc == 0.0


# Imports above use these — silence unused warnings without re-exporting.
_ = (MIN_SQRT_PRICE_X96, MAX_SQRT_PRICE_X96)
