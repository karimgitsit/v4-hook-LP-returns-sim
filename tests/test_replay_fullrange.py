"""Step-3 gate test: replay swaps against a fresh full-range v4 pool and
emit an equity curve.

Runs against the cached parquet from step 1 when it's present (the real
gate); falls back to a small synthetic stream so the test stays useful
on machines without the data pull.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from v4sim.data.schema import SWAP_SCHEMA
from v4sim.evm.artifacts import ArtifactNotFound
from v4sim.evm.env import bootstrap_v4_eth_usdc
from v4sim.replay.runner import replay_full_range

REPO_ROOT = Path(__file__).resolve().parents[1]
PARQUET = REPO_ROOT / "data" / "cache" / "swaps_88e6a0c2.parquet"

# A realistic ETH/USDC mid sqrtPriceX96 — corresponds to ~$2100/ETH with
# USDC=token0 (6 dec), WETH=token1 (18 dec).
SYNTHETIC_SQRT_X96 = "1727415587102554548530060000000000"


def _bootstrap_or_skip():
    try:
        return bootstrap_v4_eth_usdc()
    except ArtifactNotFound as e:
        pytest.skip(str(e))


def _synthetic_swaps(n: int = 20) -> pl.DataFrame:
    """Tiny alternating-direction stream — enough to exercise the loop."""
    rows = []
    base_ts = 1_700_000_000
    for i in range(n):
        direction = i % 2
        rows.append(
            {
                "id": f"0x{i:064x}",
                "ts": base_ts + i * 60,
                "block": 20_000_000 + i,
                "log_index": 0,
                "tx_hash": f"0x{i:064x}",
                "pool": "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
                "dir": direction,
                # source.token0 = USDC (6 dec, ~$100), source.token1 = WETH (18 dec, ~0.05 ETH)
                "amount0": 100.0 if direction == 0 else -100.0,
                "amount1": -0.05 if direction == 0 else 0.05,
                "amount_in": 100.0 if direction == 0 else 0.05,
                "sqrt_price_x96_post": SYNTHETIC_SQRT_X96,
                "tick_post": 199_000,
                "sender": "0x" + "00" * 20,
                "recipient": "0x" + "00" * 20,
                "origin": "0x" + "00" * 20,
            }
        )
    return pl.DataFrame(rows, schema=SWAP_SCHEMA)


def test_replay_runner_produces_equity_curve_synthetic():
    env = _bootstrap_or_skip()
    swaps = _synthetic_swaps(20)
    result = replay_full_range(
        swaps,
        snapshot_period_s=120,  # snapshot every 2 minutes so we get multiple rows
        lp_notional_usdc=1_000_000.0,
        env=env,
    )

    # At least one snapshot beyond the initial.
    assert result.snapshots.height >= 2
    # All values finite and positive (no NaN/inf).
    values = result.snapshots["lp_value_usdc"].to_list()
    assert all(v > 0 and v == v for v in values)  # NaN != NaN

    # Initial LP value at price = ~$1M (within reasonable rounding).
    assert 0.95e6 <= result.initial_lp_value_usdc <= 1.05e6, result.initial_lp_value_usdc

    # The pool should have actually moved some swaps.
    assert result.swaps_executed > 0


@pytest.mark.skipif(not PARQUET.exists(), reason="data/cache/swaps_88e6a0c2.parquet missing")
def test_replay_runner_against_cached_parquet_slice():
    env = _bootstrap_or_skip()
    df = pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(200)
    result = replay_full_range(
        df,
        snapshot_period_s=3600,
        lp_notional_usdc=1_000_000.0,
        env=env,
    )
    assert result.snapshots.height >= 2
    assert result.swaps_executed >= 1
    # Initial value close to target.
    assert 0.95e6 <= result.initial_lp_value_usdc <= 1.05e6, result.initial_lp_value_usdc
    # All snapshot values finite.
    vals = result.snapshots["lp_value_usdc"].to_list()
    assert all(v == v and v > 0 for v in vals)
