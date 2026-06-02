"""Tier-1 mock-injection seam: a hook that consumes an EXTERNAL contract.

`OracleGuardHook` (contracts/example-hooks/src/oracle/) reads a Chainlink-style
price feed on every swap and pauses trading while the price is below a floor.
The feed is an external dependency, so the world only runs if it is deployed in
the sandbox — which is exactly what `WorldSpec.env_setup` /
`WorldSpec.hook_ctor_args_fn` provide. These tests prove:

* the spec wires the seam (env_setup deploys the mock; ctor args reference it),
* with the mock injected the hook runs end-to-end and, breaker off, is
  transparent (tracks the full-range baseline),
* with the floor above the price the hook reads the feed and gates every swap,
* WITHOUT the seam (feed not deployed) the hook reverts — i.e. the dependency is
  real and the seam is necessary.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import polars as pl
import pytest
from eth_abi import encode as abi_encode

from v4sim.evm.artifacts import ArtifactNotFound
from v4sim.evm.hookmine import ALL_HOOK_MASK, BEFORE_SWAP_FLAG
from v4sim.replay.runner import (
    default_baseline_specs,
    oracle_guard_hook_spec,
    replay_worlds,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PARQUET = REPO_ROOT / "data" / "cache" / "swaps_88e6a0c2.parquet"


def _require_spec(**kw):
    try:
        return oracle_guard_hook_spec(**kw)
    except ArtifactNotFound as e:
        pytest.skip(f"{e} — run scripts/build_contracts.sh")


def _require_parquet():
    if not PARQUET.exists():
        pytest.skip("data/cache/swaps_88e6a0c2.parquet missing")


def _swaps(n: int) -> pl.DataFrame:
    return pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(n)


def test_spec_wires_the_mock_injection_seam():
    spec = _require_spec()
    assert spec.kind == "hook"
    assert spec.hook_flags == BEFORE_SWAP_FLAG
    # The seam: a setup that deploys the external dep, and a ctor-args builder
    # that references it. (Not the plain manager-prepend path.)
    assert spec.env_setup is not None
    assert spec.hook_ctor_args_fn is not None
    assert spec.hook_ctor_manager is False


def test_seam_deploys_oracle_and_hook_runs_transparently():
    """Breaker off (price >> floor): hook runs end-to-end and tracks the baseline."""
    _require_parquet()
    spec = _require_spec()
    df = _swaps(300)
    res = replay_worlds(df, default_baseline_specs() + [spec], snapshot_period_s=3600)
    fr = res.worlds["full_range"]
    og = res.worlds[spec.name]

    assert int(og.hook_addr, 16) & ALL_HOOK_MASK == BEFORE_SWAP_FLAG
    assert og.swaps_executed == fr.swaps_executed > 0
    # beforeSwap-only with zero delta -> identical to a vanilla full-range LP.
    assert og.final_lp_value_usdc == pytest.approx(fr.final_lp_value_usdc, rel=1e-9)


def test_oracle_floor_trips_and_pauses_all_swaps():
    """Floor above the oracle price: the hook reads the feed and gates every swap."""
    _require_parquet()
    spec = _require_spec(name="og_trip", floor_usd=3000.0, oracle_price_usd=2500.0)
    df = _swaps(300)
    res = replay_worlds(df, [spec], snapshot_period_s=3600)
    og = res.worlds["og_trip"]
    assert og.swaps_executed == 0
    assert og.swaps_skipped > 0


def test_missing_dependency_reverts_without_the_seam():
    """Point the hook at a feed address that was never deployed (no env_setup).

    Proves the dependency is genuinely exercised at runtime: every swap reverts
    on the empty-address oracle call, so the mock-injection seam is necessary.
    """
    _require_parquet()
    base = _require_spec()
    bogus_feed = "0x" + "de" * 20

    def ctor_args(env, _mocks):  # ignore mocks; point at an undeployed feed
        return abi_encode(
            ["address", "address", "int256"], [env.manager, bogus_feed, 100_000_000]
        )

    spec = dataclasses.replace(
        base, name="og_nodep", env_setup=None, hook_ctor_args_fn=ctor_args
    )
    res = replay_worlds(_swaps(200), [spec], snapshot_period_s=3600)
    w = res.worlds["og_nodep"]
    assert w.swaps_executed == 0  # the external feed isn't there -> beforeSwap reverts
