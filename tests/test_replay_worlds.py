"""Step-5 gate tests: concentrated baseline + multi-world replay.

The real-data tests use the cached step-1 parquet and skip if it (or the
forge artifacts) is missing.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from v4sim.evm.artifacts import ArtifactNotFound
from v4sim.evm.env import bootstrap_v4_eth_usdc
from v4sim.replay.runner import (
    WorldSpec,
    default_baseline_specs,
    replay_worlds,
)
from v4sim.strategies.concentrated import band_ticks

REPO_ROOT = Path(__file__).resolve().parents[1]
PARQUET = REPO_ROOT / "data" / "cache" / "swaps_88e6a0c2.parquet"


def _require_artifacts():
    try:
        bootstrap_v4_eth_usdc()
    except ArtifactNotFound as e:
        pytest.skip(str(e))


def _require_parquet():
    if not PARQUET.exists():
        pytest.skip("data/cache/swaps_88e6a0c2.parquet missing")


def test_band_ticks_brackets_init_and_aligns():
    lower, upper = band_ticks(199670, 10, 0.10)
    assert lower % 10 == 0 and upper % 10 == 0
    assert lower < 199670 < upper
    # ±10% ≈ ±953 ticks; allow rounding slack.
    assert 1800 <= (upper - lower) <= 2100, (lower, upper)


def test_both_worlds_initialize_at_notional():
    _require_artifacts()
    _require_parquet()
    df = pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(50)
    res = replay_worlds(df, default_baseline_specs(), snapshot_period_s=3600)
    assert set(res.worlds) == {"full_range", "concentrated"}
    for w in res.worlds.values():
        # Rescaled to exactly the $1M notional within float rounding.
        assert w.initial_lp_value_usdc == pytest.approx(1_000_000.0, rel=1e-6), w


def test_snapshots_are_long_form_with_world_column():
    _require_artifacts()
    _require_parquet()
    df = pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(300)
    res = replay_worlds(df, default_baseline_specs(), snapshot_period_s=600)
    snaps = res.snapshots
    assert "world" in snaps.columns
    assert set(snaps["world"].unique().to_list()) == {"full_range", "concentrated"}
    # Equal number of snapshots per world (snapshotted in lockstep).
    counts = snaps.group_by("world").len()["len"].to_list()
    assert len(set(counts)) == 1, counts


def test_thin_fullrange_suffers_more_lvr_than_concentrated():
    """Same $1M, but full-range is far thinner at the current price, so it
    bleeds more to the arb per swap (LVR ∝ 1/L)."""
    _require_artifacts()
    _require_parquet()
    df = pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(1000)
    res = replay_worlds(df, default_baseline_specs(), snapshot_period_s=3600)
    fr = res.worlds["full_range"]
    co = res.worlds["concentrated"]
    assert fr.cum_arb_extracted_usdc > co.cum_arb_extracted_usdc > 0, (fr, co)


def test_concentrated_band_exit_does_not_crash():
    """Force the price out of a tight band and confirm the world still
    produces finite, positive LP values (position goes ~100% one token)."""
    _require_artifacts()
    _require_parquet()
    df = pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(800)
    specs = [WorldSpec(name="tight", kind="concentrated", band_pct=0.002)]  # ±0.2% ≈ ±20 ticks
    res = replay_worlds(df, specs, snapshot_period_s=600)
    w = res.worlds["tight"]
    assert w.swaps_executed > 0
    vals = res.snapshots["lp_value_usdc"].to_list()
    assert all(v == v and v > 0 for v in vals), vals  # finite + positive
