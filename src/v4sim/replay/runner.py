"""Multi-world v4 replay.

Drives one or more freshly-deployed v4 pools ("worlds") through the same
historical v3 swap stream. After each user swap a perfect arbitrageur
pushes each pool back to the v3 post-swap price (arb-to-truth, step 4);
the arb PnL valued at truth is accumulated as `cum_arb_extracted_usdc`.

Each world is an independent baseline/strategy against the same swaps:

- ``full_range``   — vanilla full-range LP (step 3/4)
- ``concentrated`` — vanilla ±band_pct LP (step 5)
- ``hook``         — hook-equipped pool (step 6; not wired here yet)

`replay_worlds` runs N worlds and returns a long-form snapshot frame keyed
by ``(ts, world)``. `replay_full_range` is a thin single-world wrapper kept
for the step-3/4 API and tests.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from v4sim.evm.arb import arb_to_target
from v4sim.evm.artifacts import creation_code
from v4sim.evm.env import V4Env, bootstrap_v4_eth_usdc
from v4sim.evm.hookmine import deploy_hook
from v4sim.evm.pool import ZERO_ADDRESS, PoolKey, initialize, read_slot0, swap
from v4sim.evm.state import uncollected_fees_raw
from v4sim.metrics.accounting import (
    amounts_for_liquidity,
    usdc_value_of_position,
    volatile_price_in_usdc,
)
from v4sim.metrics.gas import DEFAULT_GAS_MODEL, GasModel
from v4sim.strategies.hook_adapter import HookAdapter, PositionState
from v4sim.strategies.placement import place_position

log = logging.getLogger(__name__)

# ETH/USDC 5bps pool: tickSpacing on Uniswap v3 is 10 and v4 mirrors it.
DEFAULT_FEE = 500  # 5bps in v4 fee units (= bps * 100)
DEFAULT_TICK_SPACING = 10
DEFAULT_SNAPSHOT_PERIOD_S = 3600  # hourly equity snapshots
DEFAULT_BAND_PCT = 0.10

# Source pool (real-world ETH/USDC 5bps): token0 = USDC, token1 = WETH.
SOURCE_TOKEN0_IS_USDC = True


# --------------------------------------------------------------------------
# World configuration + runtime state
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class WorldSpec:
    """Declarative config for one replay world.

    `band_pct` controls LP placement for every kind: None = full-range,
    a fraction = a ±band_pct concentrated position. `kind="hook"` additionally
    deploys the hook from `hook_artifact` at a flag-valid address and attaches
    it to the pool.
    """

    name: str
    kind: str = "full_range"  # "full_range" | "concentrated" | "hook"
    fee: int = DEFAULT_FEE
    tick_spacing: int = DEFAULT_TICK_SPACING
    band_pct: float | None = None
    # kind == "hook" only:
    hook_artifact: dict | None = None  # forge artifact JSON (parsed)
    hook_flags: int = 0  # required v4 permission bits (low 14 of the address)
    hook_constructor_args: bytes = b""  # ABI-encoded ctor args
    # Many hooks (anything extending a BaseHook) take the PoolManager as their
    # first constructor arg. The manager is deployed fresh per world, so its
    # address isn't known when the spec is declared; set this and the runner
    # ABI-encodes env.manager and prepends it to `hook_constructor_args`.
    hook_ctor_manager: bool = False
    adapter: HookAdapter | None = None  # None -> passive (no rebalance)


@dataclass
class _World:
    spec: WorldSpec
    env: V4Env
    key: PoolKey
    position: PositionState
    usdc_is_currency0: bool
    hook_addr: str = ZERO_ADDRESS
    adapter: HookAdapter | None = None
    cum_arb: float = 0.0
    cum_realized_fees: float = 0.0
    arbs: int = 0
    executed: int = 0
    skipped: int = 0
    rebalances: int = 0


@dataclass
class WorldResult:
    name: str
    kind: str
    initial_lp_value_usdc: float
    final_lp_value_usdc: float
    final_lp_amount0_raw: int
    final_lp_amount1_raw: int
    cum_arb_extracted_usdc: float
    swaps_executed: int
    swaps_skipped: int
    arbs_executed: int
    rebalances: int = 0
    hook_addr: str = ZERO_ADDRESS
    # Attribution inputs (step 7a).
    initial_lp_amount0_raw: int = 0
    initial_lp_amount1_raw: int = 0
    final_sqrt_price_x96: int = 0
    fees_token0_raw: int = 0
    fees_token1_raw: int = 0
    fees_usdc: float = 0.0
    gas_usdc: float = 0.0
    decimals0: int = 18
    decimals1: int = 18
    usdc_is_currency0: bool = True
    tick_lower: int = 0
    tick_upper: int = 0
    sqrt_a_x96: int = 0
    sqrt_b_x96: int = 0


@dataclass
class WorldsResult:
    snapshots: pl.DataFrame  # long-form: one row per (ts, world)
    worlds: dict[str, WorldResult] = field(default_factory=dict)


# Kept for the step-3/4 single-world API.
@dataclass
class ReplayResult:
    snapshots: pl.DataFrame
    final_lp_amount0_raw: int
    final_lp_amount1_raw: int
    initial_lp_value_usdc: float
    final_lp_value_usdc: float
    cum_arb_extracted_usdc: float
    swaps_executed: int
    swaps_skipped: int
    arbs_executed: int


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _parse_sqrt_price(value) -> int:
    """Parquet stores sqrtPriceX96 as a decimal string; convert to int."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"unexpected sqrt price type {type(value).__name__}")


def _harness_zero_for_one(source_dir: int, usdc_is_currency0: bool) -> bool:
    """Translate source-pool swap direction to the harness's currency ordering.

    `source_dir == 0` means "trader sent source.token0 (=USDC)". If the
    harness's currency0 is also USDC, that's a zero-for-one swap; otherwise
    USDC sits at currency1 and it's a one-for-zero swap.
    """
    sending_usdc = source_dir == 0  # source.token0 = USDC
    return sending_usdc == usdc_is_currency0


def _value_position(
    env: V4Env, amount0: int, amount1: int, sqrt_p_x96: int, *, usdc_is_currency0: bool
) -> float:
    return usdc_value_of_position(
        amount0,
        amount1,
        sqrt_p_x96,
        decimals0=env.decimals0,
        decimals1=env.decimals1,
        usdc_is_token0=usdc_is_currency0,
    )


def _world_fees_raw(world: _World, current_tick: int) -> tuple[int, int]:
    """Uncollected (token0, token1) fees for this world's LP position, raw units."""
    pos = world.position
    return uncollected_fees_raw(
        world.env,
        world.key,
        owner=world.env.modify_liquidity_router,
        tick_lower=pos.tick_lower,
        tick_upper=pos.tick_upper,
        current_tick=current_tick,
        salt=pos.salt,
    )


def _snapshot_world(world: _World, ts: int, swap_index: int) -> dict:
    slot0 = read_slot0(world.env, world.key)
    pos = world.position
    amt0, amt1 = amounts_for_liquidity(
        slot0.sqrt_price_x96, pos.sqrt_a_x96, pos.sqrt_b_x96, pos.liquidity
    )
    value = _value_position(
        world.env, amt0, amt1, slot0.sqrt_price_x96, usdc_is_currency0=world.usdc_is_currency0
    )
    fees0, fees1 = _world_fees_raw(world, slot0.tick)
    # Total fees to date = fees already realized at rebalances + still-uncollected.
    uncollected_usdc = _value_position(
        world.env, fees0, fees1, slot0.sqrt_price_x96, usdc_is_currency0=world.usdc_is_currency0
    )
    fees_usdc = world.cum_realized_fees + uncollected_usdc
    # Current band edges in price terms, so the liquidity chart can track a
    # moving band (active worlds re-centre; passive bands are flat).
    edge_a = volatile_price_in_usdc(
        pos.sqrt_a_x96, decimals0=world.env.decimals0, decimals1=world.env.decimals1,
        usdc_is_token0=world.usdc_is_currency0,
    )
    edge_b = volatile_price_in_usdc(
        pos.sqrt_b_x96, decimals0=world.env.decimals0, decimals1=world.env.decimals1,
        usdc_is_token0=world.usdc_is_currency0,
    )
    band_low, band_high = sorted((edge_a, edge_b))
    return {
        "ts": ts,
        "world": world.spec.name,
        "swap_index": swap_index,
        "sqrt_price_x96": str(slot0.sqrt_price_x96),
        "tick": slot0.tick,
        "lp_amount0_raw": str(amt0),
        "lp_amount1_raw": str(amt1),
        "lp_value_usdc": value,
        "fees_token0_raw": str(fees0),
        "fees_token1_raw": str(fees1),
        "fees_usdc": fees_usdc,
        "lp_value_plus_fees_usdc": value + fees_usdc,
        "cum_arb_extracted_usdc": world.cum_arb,
        "band_low_usdc": band_low,
        "band_high_usdc": band_high,
        "rebalances": world.rebalances,
    }


def replay_worlds(
    swaps_df: pl.DataFrame,
    specs: list[WorldSpec],
    *,
    lp_notional_usdc: float = 1_000_000.0,
    snapshot_period_s: int = DEFAULT_SNAPSHOT_PERIOD_S,
    arb_to_truth: bool = True,
    envs: dict[str, V4Env] | None = None,
    gas_model: GasModel = DEFAULT_GAS_MODEL,
) -> WorldsResult:
    """Replay `swaps_df` against every world in `specs`.

    Each world gets its own fresh `V4Env` (a separate PoolManager) unless one
    is supplied for its name in `envs`. Isolation matters: a swap in one
    world must not consume liquidity another world was meant to earn fees on.
    """
    if not specs:
        raise ValueError("need at least one world spec")
    if swaps_df.height < 2:
        raise ValueError("need at least 2 swap rows: row 0 sets the price, row 1+ replays")
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise ValueError(f"world names must be unique, got {names}")

    rows = swaps_df.sort(["ts", "block", "log_index"]).to_dicts()
    init_sqrt_x96 = _parse_sqrt_price(rows[0]["sqrt_price_x96_post"])

    envs = envs or {}
    worlds: list[_World] = []
    for spec in specs:
        env = envs.get(spec.name) or bootstrap_v4_eth_usdc()
        worlds.append(_build_world(spec, env, init_sqrt_x96, lp_notional_usdc))

    first_ts = int(rows[0]["ts"])
    snap_rows: list[dict] = [_snapshot_world(w, first_ts, 0) for w in worlds]
    initial_values = {w.spec.name: snap_rows[i]["lp_value_usdc"] for i, w in enumerate(worlds)}
    initial_amounts = {
        w.spec.name: (int(snap_rows[i]["lp_amount0_raw"]), int(snap_rows[i]["lp_amount1_raw"]))
        for i, w in enumerate(worlds)
    }
    last_snap_ts = first_ts

    for idx, row in enumerate(rows[1:], start=1):
        source_dir = int(row["dir"])
        amount_in = float(row["amount_in"])
        truth_sp = _parse_sqrt_price(row["sqrt_price_x96_post"]) if arb_to_truth else None
        block_number = int(row["block"])
        block_ts = int(row["ts"])
        for w in worlds:
            # Advance each world's block to the swap's real on-chain block so the
            # user swap and its arb back-run share a block (as they did on-chain),
            # which is what lets block-aware hooks act. No-op for hookless worlds.
            w.env.set_block(number=block_number, timestamp=block_ts)
            z4o = _harness_zero_for_one(source_dir, w.usdc_is_currency0)
            input_decimals = w.env.decimals0 if z4o else w.env.decimals1
            amount_in_raw = int(amount_in * 10**input_decimals)
            if amount_in_raw <= 0:
                w.skipped += 1
                continue
            try:
                swap(w.env, w.key, zero_for_one=z4o, amount_specified=-amount_in_raw)
                w.executed += 1
            except Exception as e:  # pyrevm raises a bare RuntimeError on revert
                log.debug("world %s swap %d reverted: %s", w.spec.name, idx, e)
                w.skipped += 1
                continue
            if truth_sp is not None:
                try:
                    arb = arb_to_target(w.env, w.key, truth_sp)
                except Exception as e:
                    log.debug("world %s arb %d reverted: %s", w.spec.name, idx, e)
                    arb = None
                if arb is not None and not arb.skipped:
                    w.arbs += 1
                    w.cum_arb += _value_position(
                        w.env, arb.delta0, arb.delta1, truth_sp,
                        usdc_is_currency0=w.usdc_is_currency0,
                    )

            # Tick keeper: let an active hook rebalance at the (post-arb) truth
            # price. Passive worlds have no adapter; the no-op adapter returns
            # None. The call site is here so real hooks plug in unchanged.
            if w.adapter is not None:
                keeper_price = truth_sp if truth_sp is not None else read_slot0(w.env, w.key).sqrt_price_x96
                rb = w.adapter.rebalance(w.env, w.key, w.position, keeper_price)
                if rb is not None:
                    w.rebalances += 1
                    w.cum_realized_fees += rb.realized_fees_usdc

        ts = int(row["ts"])
        if ts - last_snap_ts >= snapshot_period_s:
            for w in worlds:
                snap_rows.append(_snapshot_world(w, ts, idx))
            last_snap_ts = ts

    last_ts = int(rows[-1]["ts"])
    if last_ts != last_snap_ts:
        for w in worlds:
            snap_rows.append(_snapshot_world(w, last_ts, len(rows) - 1))

    snap_df = pl.DataFrame(snap_rows)

    results: dict[str, WorldResult] = {}
    for w in worlds:
        wrows = snap_df.filter(pl.col("world") == w.spec.name)
        final = wrows.tail(1).to_dicts()[0]
        init0, init1 = initial_amounts[w.spec.name]
        results[w.spec.name] = WorldResult(
            name=w.spec.name,
            kind=w.spec.kind,
            initial_lp_value_usdc=initial_values[w.spec.name],
            final_lp_value_usdc=final["lp_value_usdc"],
            final_lp_amount0_raw=int(final["lp_amount0_raw"]),
            final_lp_amount1_raw=int(final["lp_amount1_raw"]),
            cum_arb_extracted_usdc=w.cum_arb,
            swaps_executed=w.executed,
            swaps_skipped=w.skipped,
            arbs_executed=w.arbs,
            rebalances=w.rebalances,
            hook_addr=w.hook_addr,
            initial_lp_amount0_raw=init0,
            initial_lp_amount1_raw=init1,
            final_sqrt_price_x96=int(final["sqrt_price_x96"]),
            fees_token0_raw=int(final["fees_token0_raw"]),
            fees_token1_raw=int(final["fees_token1_raw"]),
            fees_usdc=final["fees_usdc"],
            gas_usdc=gas_model.cost_usdc(w.rebalances),
            decimals0=w.env.decimals0,
            decimals1=w.env.decimals1,
            usdc_is_currency0=w.usdc_is_currency0,
            tick_lower=w.position.tick_lower,
            tick_upper=w.position.tick_upper,
            sqrt_a_x96=w.position.sqrt_a_x96,
            sqrt_b_x96=w.position.sqrt_b_x96,
        )
    return WorldsResult(snapshots=snap_df, worlds=results)


def _build_world(
    spec: WorldSpec, env: V4Env, init_sqrt_x96: int, lp_notional_usdc: float
) -> _World:
    """Build a world, sizing liquidity so its value == lp_notional_usdc.

    Placement is governed by `band_pct` for every kind (None = full-range).
    `kind == "hook"` first deploys the hook at a flag-valid address and
    attaches it to the pool; the no-op test hook leaves mechanics unchanged so
    its world tracks the matching vanilla baseline. A `spec.adapter` (active
    rebalancer) may be attached to any kind — its `rebalance` runs on the tick
    keeper; passive worlds leave it None.
    """
    if spec.kind not in ("full_range", "concentrated", "hook"):
        raise ValueError(f"unknown world kind {spec.kind!r}")

    usdc_is_currency0 = env.decimals0 == 6

    hook_addr = ZERO_ADDRESS
    if spec.kind == "hook":
        if spec.hook_artifact is None:
            raise ValueError("hook world requires hook_artifact")
        ctor_args = spec.hook_constructor_args
        if spec.hook_ctor_manager:
            from eth_abi import encode as abi_encode

            ctor_args = abi_encode(["address"], [env.manager]) + ctor_args
        hook_addr = deploy_hook(
            env,
            creation_code(spec.hook_artifact),
            spec.hook_flags,
            constructor_args=ctor_args,
        )

    key = PoolKey(
        currency0=env.currency0,
        currency1=env.currency1,
        fee=spec.fee,
        tick_spacing=spec.tick_spacing,
        hooks=hook_addr,
    )
    init_tick = initialize(env, key, init_sqrt_x96)
    position = place_position(
        env, key,
        center_tick=init_tick, center_sqrt_x96=init_sqrt_x96,
        tick_spacing=spec.tick_spacing, band_pct=spec.band_pct,
        target_usdc=lp_notional_usdc, usdc_is_currency0=usdc_is_currency0,
    )

    return _World(
        spec=spec,
        env=env,
        key=key,
        position=position,
        usdc_is_currency0=usdc_is_currency0,
        hook_addr=hook_addr,
        adapter=spec.adapter,
    )


def replay_full_range(
    swaps_df: pl.DataFrame,
    *,
    fee: int = DEFAULT_FEE,
    tick_spacing: int = DEFAULT_TICK_SPACING,
    lp_notional_usdc: float = 1_000_000.0,
    snapshot_period_s: int = DEFAULT_SNAPSHOT_PERIOD_S,
    env: V4Env | None = None,
    arb_to_truth: bool = True,
) -> ReplayResult:
    """Single full-range world. Thin wrapper over `replay_worlds` (step 3/4 API)."""
    spec = WorldSpec(name="full_range", kind="full_range", fee=fee, tick_spacing=tick_spacing)
    res = replay_worlds(
        swaps_df,
        [spec],
        lp_notional_usdc=lp_notional_usdc,
        snapshot_period_s=snapshot_period_s,
        arb_to_truth=arb_to_truth,
        envs={"full_range": env} if env is not None else None,
    )
    w = res.worlds["full_range"]
    return ReplayResult(
        snapshots=res.snapshots,
        final_lp_amount0_raw=w.final_lp_amount0_raw,
        final_lp_amount1_raw=w.final_lp_amount1_raw,
        initial_lp_value_usdc=w.initial_lp_value_usdc,
        final_lp_value_usdc=w.final_lp_value_usdc,
        cum_arb_extracted_usdc=w.cum_arb_extracted_usdc,
        swaps_executed=w.swaps_executed,
        swaps_skipped=w.swaps_skipped,
        arbs_executed=w.arbs_executed,
    )


def default_baseline_specs(band_pct: float = DEFAULT_BAND_PCT) -> list[WorldSpec]:
    """The two vanilla baselines every hook gets compared against."""
    return [
        WorldSpec(name="full_range", kind="full_range"),
        WorldSpec(name="concentrated", kind="concentrated", band_pct=band_pct),
    ]


def active_recenter_spec(
    name: str = "active_recenter",
    *,
    band_pct: float = DEFAULT_BAND_PCT,
    recenter_pct: float = 0.05,
    tick_spacing: int = DEFAULT_TICK_SPACING,
) -> WorldSpec:
    """A concentrated world driven by the auto-recentering active adapter.

    Demonstrates the active path end-to-end: the harness places the initial
    ±band_pct position, then the adapter re-centres it whenever price drifts
    `recenter_pct` from the band centre (booking realized fees and gas).
    """
    from v4sim.strategies.active_rebalance import AutoRecenterAdapter

    return WorldSpec(
        name=name,
        kind="concentrated",
        band_pct=band_pct,
        tick_spacing=tick_spacing,
        adapter=AutoRecenterAdapter(
            band_pct=band_pct, recenter_pct=recenter_pct, tick_spacing=tick_spacing
        ),
    )


def noop_hook_spec(name: str = "hook", band_pct: float = DEFAULT_BAND_PCT) -> WorldSpec:
    """A transparent hook world built on v4-core's MockHooks.

    MockHooks returns the correct selector and zero delta from every callback,
    so attaching it (with only the afterInitialize flag) leaves pool mechanics
    identical to a vanilla concentrated LP. Used to prove the hook plumbing is
    transparent before a real hook is plugged in.
    """
    from v4sim.evm.artifacts import get
    from v4sim.evm.hookmine import AFTER_INITIALIZE_FLAG

    return WorldSpec(
        name=name,
        kind="hook",
        band_pct=band_pct,
        hook_artifact=get("MockHooks.sol", "MockHooks"),
        hook_flags=AFTER_INITIALIZE_FLAG,
    )


# Path to the vendored example-hook artifacts (built by scripts/build_contracts.sh).
_HOOKS_OUT = Path(__file__).resolve().parents[3] / "contracts" / "example-hooks" / "out"


def antisandwich_hook_spec(name: str = "antisandwich", band_pct: float | None = None) -> WorldSpec:
    """A world running OpenZeppelin's real AntiSandwichHook (vendored, see contracts/example-hooks/).

    The hook pins a beginning-of-block execution price for !zeroForOne swaps and
    donates the resulting surplus back to in-range LPs. In this single-LP replay
    that means value the arbitrageur would extract (LVR) is instead returned to
    the LP as fee growth — so the hook's effect shows up directly in the existing
    fees and LVR metrics, with no attribution change needed.

    Defaults to FULL-RANGE placement (``band_pct=None``): the hook donates to
    in-range liquidity, so a position that is always in range avoids the
    ``NoLiquidityToReceiveDonation`` revert and gives a clean comparison against
    the full-range baseline.

    Requires block-number advancement (the runner does this per swap) and the
    artifact built at contracts/example-hooks/out/ — run scripts/build_contracts.sh.
    """
    from v4sim.evm.artifacts import load_artifact_file
    from v4sim.evm.hookmine import (
        AFTER_SWAP_FLAG,
        AFTER_SWAP_RETURNS_DELTA_FLAG,
        BEFORE_SWAP_FLAG,
    )

    artifact_path = _HOOKS_OUT / "AntiSandwichHookHarness.sol" / "AntiSandwichHookHarness.json"
    return WorldSpec(
        name=name,
        kind="hook",
        band_pct=band_pct,
        hook_artifact=load_artifact_file(artifact_path),
        hook_flags=BEFORE_SWAP_FLAG | AFTER_SWAP_FLAG | AFTER_SWAP_RETURNS_DELTA_FLAG,
        hook_ctor_manager=True,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _parse_when(value: str) -> int:
    """Accept either a unix ts ('1779000000') or an ISO date/datetime."""
    s = value.strip()
    if s.isdigit():
        return int(s)
    parsed = dt.datetime.fromisoformat(s)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return int(parsed.timestamp())


def _apply_window(
    df: pl.DataFrame, *, start: int | None, end: int | None, days: int | None
) -> pl.DataFrame:
    """Filter `df` by a [start, end) ts window, or to the most-recent `days`."""
    if days is not None and (start is not None or end is not None):
        raise ValueError("--days is mutually exclusive with --start/--end")
    if days is not None:
        if df.height == 0:
            return df
        max_ts = int(df["ts"].max())
        start = max_ts - days * 86_400
        end = max_ts + 1
    if start is not None:
        df = df.filter(pl.col("ts") >= start)
    if end is not None:
        df = df.filter(pl.col("ts") < end)
    return df


def cli(argv: list[str] | None = None) -> int:
    """Replay the baselines (full-range + concentrated) and dump long-form snapshots."""
    import argparse

    parser = argparse.ArgumentParser(description="v4 multi-world replay with arb-to-truth")
    parser.add_argument(
        "--swaps", type=Path, default=Path("data/cache/swaps_88e6a0c2.parquet"),
        help="path to the step-1 swap parquet",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("data/cache/equity_worlds.parquet"),
        help="output path for the long-form equity snapshot parquet",
    )
    parser.add_argument("--start", type=str, default=None, help="window start (ISO or unix ts)")
    parser.add_argument("--end", type=str, default=None, help="window end (exclusive)")
    parser.add_argument("--days", type=int, default=None, help="take the last N days of the parquet")
    parser.add_argument("--limit", type=int, default=0, help="cap on rows after windowing (0 = all)")
    parser.add_argument(
        "--band-pct", type=float, default=DEFAULT_BAND_PCT,
        help="concentrated baseline half-bandwidth (0.10 = ±10%%)",
    )
    parser.add_argument(
        "--snapshot-period-s", type=int, default=DEFAULT_SNAPSHOT_PERIOD_S,
        help="seconds between equity snapshots",
    )
    parser.add_argument(
        "--lp-notional-usdc", type=float, default=1_000_000.0, help="LP USDC value at t=0"
    )
    parser.add_argument(
        "--no-arb", action="store_true", help="disable arb-to-truth (drift mode)"
    )
    parser.add_argument(
        "--hook", type=Path, default=None,
        help="forge artifact JSON for a hook to add as a third world",
    )
    parser.add_argument(
        "--hook-flags", type=lambda s: int(s, 0), default=None,
        help="hook permission flag bits (low 14 of the address), e.g. 0x40 for afterSwap",
    )
    parser.add_argument(
        "--demo-hook", action="store_true",
        help="add a transparent no-op hook world (v4-core MockHooks) for plumbing demos",
    )
    parser.add_argument(
        "--antisandwich", action="store_true",
        help="add a world running the real vendored OpenZeppelin AntiSandwichHook (full-range)",
    )
    parser.add_argument(
        "--active", action="store_true",
        help="add an auto-recentering active world (re-centres the band on price drift)",
    )
    parser.add_argument(
        "--recenter-pct", type=float, default=0.05,
        help="active world: re-centre when price drifts this fraction from band centre",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not args.swaps.exists():
        parser.error(f"swap parquet not found: {args.swaps}")
    df = pl.read_parquet(args.swaps).sort(["ts", "block", "log_index"])

    start_ts = _parse_when(args.start) if args.start else None
    end_ts = _parse_when(args.end) if args.end else None
    df = _apply_window(df, start=start_ts, end=end_ts, days=args.days)
    if args.limit:
        df = df.head(args.limit)
    if df.height < 2:
        parser.error(f"after windowing, fewer than 2 rows remain ({df.height})")
    log.info(
        "loaded %d rows (ts %s → %s) from %s",
        df.height,
        dt.datetime.fromtimestamp(int(df["ts"].min()), tz=dt.UTC).isoformat(),
        dt.datetime.fromtimestamp(int(df["ts"].max()), tz=dt.UTC).isoformat(),
        args.swaps,
    )

    specs = default_baseline_specs(band_pct=args.band_pct)
    if args.active:
        specs.append(active_recenter_spec(band_pct=args.band_pct, recenter_pct=args.recenter_pct))
    if args.demo_hook:
        specs.append(noop_hook_spec(band_pct=args.band_pct))
    if args.antisandwich:
        specs.append(antisandwich_hook_spec())
    if args.hook is not None:
        if args.hook_flags is None:
            parser.error("--hook requires --hook-flags")
        from v4sim.evm.artifacts import load_artifact_file

        specs.append(
            WorldSpec(
                name="hook",
                kind="hook",
                band_pct=args.band_pct,
                hook_artifact=load_artifact_file(args.hook),
                hook_flags=args.hook_flags,
            )
        )

    res = replay_worlds(
        df,
        specs,
        snapshot_period_s=args.snapshot_period_s,
        lp_notional_usdc=args.lp_notional_usdc,
        arb_to_truth=not args.no_arb,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    res.snapshots.write_parquet(args.out)
    for name, w in res.worlds.items():
        log.info(
            "[%s] executed=%d skipped=%d arbs=%d initial=%.2f final=%.2f cum_arb=%.2f",
            name, w.swaps_executed, w.swaps_skipped, w.arbs_executed,
            w.initial_lp_value_usdc, w.final_lp_value_usdc, w.cum_arb_extracted_usdc,
        )
    log.info("wrote %d snapshot rows -> %s", res.snapshots.height, args.out)
    return 0
