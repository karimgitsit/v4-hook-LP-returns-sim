"""Step-8 gate: the auto-recentering active adapter drives the rebalance seam."""

from __future__ import annotations

import math
from pathlib import Path

import polars as pl
import pytest

from v4sim.evm.artifacts import ArtifactNotFound
from v4sim.evm.env import bootstrap_v4_eth_usdc
from v4sim.evm.pool import PoolKey, initialize
from v4sim.metrics.gas import GasModel
from v4sim.replay.runner import (
    WorldSpec,
    active_recenter_spec,
    replay_worlds,
)
from v4sim.strategies.active_rebalance import AutoRecenterAdapter, _pct_to_ticks
from v4sim.strategies.placement import place_position

REPO_ROOT = Path(__file__).resolve().parents[1]
PARQUET = REPO_ROOT / "data" / "cache" / "swaps_88e6a0c2.parquet"


def _env():
    try:
        return bootstrap_v4_eth_usdc()
    except ArtifactNotFound as e:
        pytest.skip(str(e))


def _require_parquet():
    if not PARQUET.exists():
        pytest.skip("swap parquet missing")


def _swaps(n: int) -> pl.DataFrame:
    return pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(n)


def test_pct_to_ticks_matches_log_formula():
    # 1 tick ≈ 1bp; a ~1% move is ~99-100 ticks.
    assert _pct_to_ticks(0.01) == round(math.log(1.01) / math.log(1.0001))
    assert _pct_to_ticks(0.05) > _pct_to_ticks(0.01) > 0


def test_place_position_sizes_to_target_value():
    env = _env()
    p_raw = (1 / 3000) * 10**12
    sqrtP = int(math.isqrt(int(p_raw * (1 << 192))))
    key = PoolKey(currency0=env.currency0, currency1=env.currency1, fee=500, tick_spacing=10)
    tick = initialize(env, key, sqrtP)
    pos = place_position(
        env, key, center_tick=tick, center_sqrt_x96=sqrtP, tick_spacing=10,
        band_pct=0.05, target_usdc=500_000.0, usdc_is_currency0=True,
    )
    from v4sim.metrics.accounting import amounts_for_liquidity, usdc_value_of_position

    amt0, amt1 = amounts_for_liquidity(sqrtP, pos.sqrt_a_x96, pos.sqrt_b_x96, pos.liquidity)
    value = usdc_value_of_position(
        amt0, amt1, sqrtP, decimals0=6, decimals1=18, usdc_is_token0=True
    )
    assert value == pytest.approx(500_000.0, rel=1e-6)
    assert pos.tick_lower < tick < pos.tick_upper


def test_active_world_rebalances_and_books_gas():
    _env()
    _require_parquet()
    df = _swaps(1500)
    gas = GasModel(gas_price_gwei=10)
    specs = [
        WorldSpec(name="concentrated", kind="concentrated", band_pct=0.02),
        active_recenter_spec("active", band_pct=0.02, recenter_pct=0.002),
    ]
    res = replay_worlds(df, specs, gas_model=gas, snapshot_period_s=3600)
    passive = res.worlds["concentrated"]
    active = res.worlds["active"]

    assert active.rebalances > 0
    # Gas booked exactly per rebalance; passive pays nothing.
    assert active.gas_usdc == pytest.approx(gas.cost_usdc(active.rebalances))
    assert passive.gas_usdc == 0.0
    # Re-centred liquidity stays near the price, so it earns at least as much fee.
    assert active.fees_usdc >= passive.fees_usdc
    assert active.final_lp_value_usdc > 0


def test_active_reduces_to_passive_when_never_triggered():
    """A huge recenter threshold never fires, so the active world must match the
    passive concentrated baseline exactly (the rebalance seam is transparent)."""
    _env()
    _require_parquet()
    df = _swaps(1500)
    specs = [
        WorldSpec(name="concentrated", kind="concentrated", band_pct=0.02),
        active_recenter_spec("active_never", band_pct=0.02, recenter_pct=0.50),
    ]
    res = replay_worlds(df, specs, snapshot_period_s=3600)
    passive = res.worlds["concentrated"]
    active = res.worlds["active_never"]
    assert active.rebalances == 0
    assert active.final_lp_value_usdc == pytest.approx(passive.final_lp_value_usdc, rel=1e-9)
    assert active.fees_usdc == pytest.approx(passive.fees_usdc, rel=1e-9)
    assert active.cum_arb_extracted_usdc == pytest.approx(passive.cum_arb_extracted_usdc, rel=1e-9)


def test_adapter_no_op_returns_none_far_from_edge():
    """Directly: the adapter returns None when the price sits at the band centre."""
    env = _env()
    p_raw = (1 / 3000) * 10**12
    sqrtP = int(math.isqrt(int(p_raw * (1 << 192))))
    key = PoolKey(currency0=env.currency0, currency1=env.currency1, fee=500, tick_spacing=10)
    tick = initialize(env, key, sqrtP)
    pos = place_position(
        env, key, center_tick=tick, center_sqrt_x96=sqrtP, tick_spacing=10,
        band_pct=0.05, target_usdc=1_000_000.0, usdc_is_currency0=True,
    )
    adapter = AutoRecenterAdapter(band_pct=0.05, recenter_pct=0.03, tick_spacing=10)
    assert adapter.rebalance(env, key, pos, sqrtP) is None
