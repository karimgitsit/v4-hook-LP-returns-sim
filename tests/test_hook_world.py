"""Step-6 gate tests: generic hook deployment + transparent hook world.

Covers the CREATE2 HookMiner (address carries the right flag bits), hook
deployment running the constructor, and a no-op hook world that must track
the concentrated baseline bit-for-bit (proving the hook plumbing doesn't
perturb vanilla mechanics).
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from eth_utils import keccak

from v4sim.evm.artifacts import ArtifactNotFound, bytecode
from v4sim.evm.env import bootstrap_v4_eth_usdc
from v4sim.evm.hookmine import (
    AFTER_INITIALIZE_FLAG,
    AFTER_SWAP_FLAG,
    ALL_HOOK_MASK,
    compute_create2_address,
    deploy_hook,
    mine_hook_salt,
)
from v4sim.replay.runner import default_baseline_specs, noop_hook_spec, replay_worlds

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


def test_create2_address_matches_reference_formula():
    factory = "0x" + "11" * 20
    init_code = b"\x60\x00\x60\x00\xf3"
    salt = (42).to_bytes(32, "big")
    expected = "0x" + keccak(
        b"\xff" + bytes.fromhex("11" * 20) + salt + keccak(init_code)
    )[12:].hex()
    assert compute_create2_address(factory, salt, init_code) == expected


def test_create2_address_depends_on_constructor_args():
    factory = "0x" + "22" * 20
    salt = (7).to_bytes(32, "big")
    code = bytes.fromhex("6001600101")
    a = compute_create2_address(factory, salt, code)
    b = compute_create2_address(factory, salt, code + b"\x00" * 32)
    assert a != b


def test_mined_salt_yields_requested_flag_bits():
    factory = "0x" + "ab" * 20
    init_code = bytecode("MockHooks.sol", "MockHooks")
    salt, addr = mine_hook_salt(factory, init_code, AFTER_SWAP_FLAG)
    assert int(addr, 16) & ALL_HOOK_MASK == AFTER_SWAP_FLAG
    # And the salt actually reproduces that address.
    assert compute_create2_address(factory, salt, init_code) == addr


def test_deploy_hook_lands_at_flag_valid_address_with_code():
    _require_artifacts()
    env = bootstrap_v4_eth_usdc()
    hook = deploy_hook(env, bytecode("MockHooks.sol", "MockHooks"), AFTER_INITIALIZE_FLAG)
    assert int(hook, 16) & ALL_HOOK_MASK == AFTER_INITIALIZE_FLAG
    assert env.evm.get_code(hook)


def test_noop_hook_world_matches_concentrated_baseline():
    """A transparent hook must leave pool mechanics identical to vanilla."""
    _require_artifacts()
    _require_parquet()
    df = pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(1000)
    specs = default_baseline_specs() + [noop_hook_spec(band_pct=0.10)]
    res = replay_worlds(df, specs, snapshot_period_s=3600)

    co = res.worlds["concentrated"]
    hk = res.worlds["hook"]
    # Hook deployed at a real flag-valid address.
    assert int(hk.hook_addr, 16) & ALL_HOOK_MASK == AFTER_INITIALIZE_FLAG
    # Same band, transparent hook -> identical outcomes.
    assert hk.final_lp_value_usdc == pytest.approx(co.final_lp_value_usdc, rel=1e-9)
    assert hk.cum_arb_extracted_usdc == pytest.approx(co.cum_arb_extracted_usdc, rel=1e-9)
    # No-op adapter never rebalances.
    assert hk.rebalances == 0


def test_three_worlds_present_in_snapshots():
    _require_artifacts()
    _require_parquet()
    df = pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(200)
    specs = default_baseline_specs() + [noop_hook_spec()]
    res = replay_worlds(df, specs, snapshot_period_s=600)
    assert set(res.snapshots["world"].unique().to_list()) == {
        "full_range",
        "concentrated",
        "hook",
    }
