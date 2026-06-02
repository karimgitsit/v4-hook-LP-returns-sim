"""Tier-2 own-liquidity seam: a hook that OWNS the LP's liquidity.

OpenZeppelin's `ReHypothecationHook` (vendored under contracts/example-hooks/)
parks the LP's tokens in ERC-4626 vaults and JIT-injects them as pool liquidity
during swaps; the LP holds the hook's ERC-20 shares, not a router position. These
tests prove the seam:

* `manages_own_liquidity` routes `_build_world` through the adapter's `setup`
  (deposit via the hook) instead of placing a router position,
* the LP's stake is valued via the hook's own share accounting
  (`previewRedeem`), with principal driving IL and fees/yield reported on top,
* the world runs end-to-end (with the permanent backstop the hook requires).
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from v4sim.evm.artifacts import ArtifactNotFound
from v4sim.evm.hookmine import (
    AFTER_SWAP_FLAG,
    ALL_HOOK_MASK,
    BEFORE_INITIALIZE_FLAG,
    BEFORE_SWAP_FLAG,
)
from v4sim.replay.runner import (
    default_baseline_specs,
    rehypothecation_hook_spec,
    replay_worlds,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PARQUET = REPO_ROOT / "data" / "cache" / "swaps_88e6a0c2.parquet"

EXPECTED_FLAGS = BEFORE_INITIALIZE_FLAG | BEFORE_SWAP_FLAG | AFTER_SWAP_FLAG


def _require_spec(**kw):
    try:
        return rehypothecation_hook_spec(**kw)
    except ArtifactNotFound as e:
        pytest.skip(f"{e} — run scripts/build_contracts.sh")


def _require_parquet():
    if not PARQUET.exists():
        pytest.skip("data/cache/swaps_88e6a0c2.parquet missing")


def _swaps(n: int) -> pl.DataFrame:
    return pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(n)


def test_spec_is_own_liquidity_with_seam_wired():
    spec = _require_spec()
    assert spec.kind == "hook"
    assert spec.hook_flags == EXPECTED_FLAGS
    assert spec.env_setup is not None  # deploys the two ERC-4626 vaults
    assert spec.hook_ctor_args_fn is not None  # wires vault addresses into the ctor
    assert spec.adapter is not None and spec.adapter.manages_own_liquidity is True


def test_deposit_values_at_notional_via_share_accounting():
    """`setup` deposits through the hook; the LP's value comes from previewRedeem."""
    _require_parquet()
    spec = _require_spec()
    df = _swaps(50)
    res = replay_worlds(df, [spec], snapshot_period_s=3600, lp_notional_usdc=1_000_000.0)
    w = res.worlds[spec.name]
    # Deployed at the mined flag-valid address with the hook's permission bits.
    assert int(w.hook_addr, 16) & ALL_HOOK_MASK == EXPECTED_FLAGS
    # The own-liquidity valuation (shares -> previewRedeem) starts at the notional.
    assert w.initial_lp_value_usdc == pytest.approx(1_000_000.0, rel=1e-3)


def test_runs_end_to_end_and_tracks_principal():
    """With the backstop the hook needs, swaps execute and IL tracks a full-range LP."""
    _require_parquet()
    spec = _require_spec()  # default seed_frac=1.0
    df = _swaps(300)
    res = replay_worlds(df, default_baseline_specs() + [spec], snapshot_period_s=3600)
    fr = res.worlds["full_range"]
    rh = res.worlds[spec.name]

    assert rh.swaps_executed == fr.swaps_executed > 0  # all swaps run with the seed
    # The hook holds a full-range position too, so its principal (IL) matches the
    # full-range baseline's underlying at the end.
    assert rh.final_lp_value_usdc == pytest.approx(fr.final_lp_value_usdc, rel=1e-6)
    # Fees/yield are reported via the adapter (previewRedeem - principal), >= 0
    # and growing as the JIT position collects fees.
    assert rh.fees_usdc > 0.0


def test_seedless_cannot_serve_takes():
    """Without the backstop, the hook's in-swap take has no reserves -> swaps revert.

    Documents *why* the seam needs the seed; not a defect in the seam itself.
    """
    _require_parquet()
    spec = _require_spec()
    spec.adapter.seed_frac = 0.0
    res = replay_worlds(_swaps(100), [spec], snapshot_period_s=3600)
    assert res.worlds[spec.name].swaps_executed == 0
