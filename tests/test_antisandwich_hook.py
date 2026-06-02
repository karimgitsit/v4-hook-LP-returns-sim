"""Step-9 gate tests: a real published hook (OpenZeppelin AntiSandwichHook).

The hook is vendored under contracts/example-hooks/ and built to
contracts/example-hooks/out/AntiSandwichHookHarness.sol/AntiSandwichHookHarness.json
by scripts/build_contracts.sh. These tests cover:

* the PoolManager-address constructor-arg prepend (hook_ctor_manager),
* per-swap block-number advancement (env.set_block), which the hook needs to
  treat each on-chain block as a distinct slot window,
* deploying the real hook at a flag-valid address, and
* the economic thesis: vs the full-range baseline the hook reduces arb-extracted
  value (LVR) and returns it to the LP as fee growth (donations to in-range LPs).
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from v4sim.evm.artifacts import ArtifactNotFound
from v4sim.evm.env import bootstrap_v4_eth_usdc
from v4sim.evm.hookmine import (
    AFTER_SWAP_FLAG,
    AFTER_SWAP_RETURNS_DELTA_FLAG,
    ALL_HOOK_MASK,
    BEFORE_SWAP_FLAG,
)
from v4sim.replay.runner import (
    antisandwich_hook_spec,
    default_baseline_specs,
    replay_worlds,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PARQUET = REPO_ROOT / "data" / "cache" / "swaps_88e6a0c2.parquet"

EXPECTED_FLAGS = BEFORE_SWAP_FLAG | AFTER_SWAP_FLAG | AFTER_SWAP_RETURNS_DELTA_FLAG


def _require_hook_spec():
    """Build the spec, skipping if the vendored artifact hasn't been compiled."""
    try:
        return antisandwich_hook_spec()
    except ArtifactNotFound as e:
        pytest.skip(f"{e} — run scripts/build_contracts.sh")


def _require_parquet():
    if not PARQUET.exists():
        pytest.skip("data/cache/swaps_88e6a0c2.parquet missing")


def test_set_block_advances_block_number():
    env = bootstrap_v4_eth_usdc()
    env.set_block(number=25_175_768, timestamp=1_779_000_000)
    assert env.evm.env.block.number == 25_175_768
    assert env.evm.env.block.timestamp == 1_779_000_000
    env.set_block(number=25_175_800)
    assert env.evm.env.block.number == 25_175_800


def test_antisandwich_spec_flags_and_ctor():
    spec = _require_hook_spec()
    assert spec.kind == "hook"
    assert spec.hook_flags == EXPECTED_FLAGS
    # The hook's BaseHook constructor takes the PoolManager; the runner must
    # prepend it since the manager address is only known at deploy time.
    assert spec.hook_ctor_manager is True
    assert spec.band_pct is None  # full-range, so donations always have a recipient
    # getHookPermissions must be present in the built artifact's ABI.
    assert any(x.get("name") == "getHookPermissions" for x in spec.hook_artifact["abi"])


def test_antisandwich_hook_deploys_at_flag_valid_address():
    _require_parquet()
    spec = _require_hook_spec()
    df = pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(50)
    res = replay_worlds(df, [spec], snapshot_period_s=3600)
    hk = res.worlds[spec.name]
    assert int(hk.hook_addr, 16) & ALL_HOOK_MASK == EXPECTED_FLAGS
    assert hk.swaps_executed > 0


def test_antisandwich_reduces_lvr_and_boosts_lp_fees():
    """The core thesis: the hook recaptures LVR as LP fees vs a vanilla full-range LP."""
    _require_parquet()
    spec = _require_hook_spec()
    df = pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(600)
    specs = default_baseline_specs() + [spec]
    res = replay_worlds(df, specs, snapshot_period_s=3600)

    fr = res.worlds["full_range"]
    hk = res.worlds[spec.name]

    # Hook executed the same swap flow as the baseline (no mass reverts).
    assert hk.swaps_executed == fr.swaps_executed
    # Anti-sandwich constrains the back-running arb, so it extracts less value...
    assert hk.cum_arb_extracted_usdc < fr.cum_arb_extracted_usdc
    # ...which is donated to the in-range LP, showing up as far higher fees.
    assert hk.fees_usdc > fr.fees_usdc * 10


def test_antisandwich_world_in_snapshots():
    _require_parquet()
    spec = _require_hook_spec()
    df = pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(100)
    res = replay_worlds(df, default_baseline_specs() + [spec], snapshot_period_s=600)
    assert spec.name in set(res.snapshots["world"].unique().to_list())
