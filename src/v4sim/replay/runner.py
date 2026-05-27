"""Single-world v4 replay: drive a freshly-deployed pool through historical
v3 swaps and emit a per-snapshot equity series.

Step 3 of the project — no arb-to-truth yet (the v4 pool's `sqrtPrice` will
drift from the v3 source after each swap). The shape of the runner is the
one that step 4 (arb) and step 5 (multi-world) will hang off.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from v4sim.evm.env import V4Env, bootstrap_v4_eth_usdc
from v4sim.evm.pool import PoolKey, initialize, read_slot0, swap
from v4sim.metrics.accounting import amounts_for_liquidity, usdc_value_of_position
from v4sim.strategies.full_range import (
    MAX_SQRT_PRICE_X96,
    MIN_SQRT_PRICE_X96,
    add_full_range_position,
)

log = logging.getLogger(__name__)

# ETH/USDC 5bps pool: tickSpacing on Uniswap v3 is 10 and v4 mirrors it.
DEFAULT_FEE = 500  # 5bps in v4 fee units (= bps * 100)
DEFAULT_TICK_SPACING = 10
DEFAULT_SNAPSHOT_PERIOD_S = 3600  # hourly equity snapshots

# Source pool (real-world ETH/USDC 5bps): token0 = USDC, token1 = WETH.
SOURCE_TOKEN0_IS_USDC = True


@dataclass
class Snapshot:
    """One row of the equity curve."""

    ts: int
    swap_index: int
    sqrt_price_x96: int
    tick: int
    lp_amount0_raw: int
    lp_amount1_raw: int
    lp_value_usdc: float


@dataclass
class ReplayResult:
    snapshots: pl.DataFrame
    final_lp_amount0_raw: int
    final_lp_amount1_raw: int
    initial_lp_value_usdc: float
    final_lp_value_usdc: float
    swaps_executed: int
    swaps_skipped: int


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


def _snapshot_now(
    env: V4Env,
    key: PoolKey,
    liquidity: int,
    swap_index: int,
    ts: int,
    *,
    usdc_is_currency0: bool,
) -> Snapshot:
    slot0 = read_slot0(env, key)
    amt0, amt1 = amounts_for_liquidity(
        sqrt_p_x96=slot0.sqrt_price_x96,
        sqrt_a_x96=MIN_SQRT_PRICE_X96,
        sqrt_b_x96=MAX_SQRT_PRICE_X96,
        liquidity=liquidity,
    )
    value = usdc_value_of_position(
        amt0,
        amt1,
        slot0.sqrt_price_x96,
        decimals0=env.decimals0,
        decimals1=env.decimals1,
        usdc_is_token0=usdc_is_currency0,
    )
    return Snapshot(
        ts=ts,
        swap_index=swap_index,
        sqrt_price_x96=slot0.sqrt_price_x96,
        tick=slot0.tick,
        lp_amount0_raw=amt0,
        lp_amount1_raw=amt1,
        lp_value_usdc=value,
    )


def replay_full_range(
    swaps_df: pl.DataFrame,
    *,
    fee: int = DEFAULT_FEE,
    tick_spacing: int = DEFAULT_TICK_SPACING,
    lp_notional_usdc: float = 1_000_000.0,
    snapshot_period_s: int = DEFAULT_SNAPSHOT_PERIOD_S,
    env: V4Env | None = None,
) -> ReplayResult:
    """Replay a swap stream against a fresh v4 full-range pool.

    Parameters
    ----------
    swaps_df :
        Polars frame matching `v4sim.data.schema.SWAP_SCHEMA`.
    fee :
        v4 pool LP fee in hundredths-of-a-bip (500 = 5bps).
    tick_spacing :
        Pool tick spacing (10 for the 5bps ETH/USDC pool).
    lp_notional_usdc :
        Total USDC-equivalent value of the LP deposit at the initial price.
        Split 50/50 across the two sides.
    snapshot_period_s :
        Wall-clock seconds between equity snapshots.

    Returns
    -------
    ReplayResult holding a polars frame of snapshots plus run-level stats.
    """
    if swaps_df.height < 2:
        raise ValueError("need at least 2 swap rows: row 0 sets the price, row 1+ replays")

    env = env or bootstrap_v4_eth_usdc()
    usdc_is_currency0 = env.decimals0 == 6
    key = PoolKey(
        currency0=env.currency0,
        currency1=env.currency1,
        fee=fee,
        tick_spacing=tick_spacing,
    )

    rows = swaps_df.sort(["ts", "block", "log_index"]).to_dicts()

    # Step-3 decision: initial sqrtPrice = row 0's `sqrt_price_x96_post`.
    init_sqrt_x96 = _parse_sqrt_price(rows[0]["sqrt_price_x96_post"])
    initialize(env, key, init_sqrt_x96)

    # 50/50 USDC value at the initial price.
    half_usdc = lp_notional_usdc / 2.0
    # Price of token1 in USDC at init.
    from v4sim.metrics.accounting import price_token1_in_token0

    p_raw = price_token1_in_token0(init_sqrt_x96)
    price_t1_per_t0_human = p_raw * (10 ** (env.decimals0 - env.decimals1))
    if usdc_is_currency0:
        # USDC = token0. Volatile = token1. price_t1_per_t0_human = volatile per USDC.
        amount0_raw = int(half_usdc * 10**env.decimals0)
        amount1_raw = int(half_usdc * price_t1_per_t0_human * 10**env.decimals1)
    else:
        amount1_raw = int(half_usdc * 10**env.decimals1)
        amount0_raw = int(half_usdc / price_t1_per_t0_human * 10**env.decimals0)

    liquidity, _, _ = add_full_range_position(
        env,
        key,
        sqrt_price_x96=init_sqrt_x96,
        amount0=amount0_raw,
        amount1=amount1_raw,
        tick_spacing=tick_spacing,
    )

    snapshots: list[Snapshot] = []
    initial_snap = _snapshot_now(
        env, key, liquidity, swap_index=0, ts=int(rows[0]["ts"]), usdc_is_currency0=usdc_is_currency0
    )
    snapshots.append(initial_snap)
    last_snap_ts = initial_snap.ts

    executed = 0
    skipped = 0
    for idx, row in enumerate(rows[1:], start=1):
        z4o = _harness_zero_for_one(int(row["dir"]), usdc_is_currency0)
        input_decimals = env.decimals0 if z4o else env.decimals1
        amount_in_raw = int(float(row["amount_in"]) * 10**input_decimals)
        if amount_in_raw <= 0:
            skipped += 1
            continue
        try:
            swap(env, key, zero_for_one=z4o, amount_specified=-amount_in_raw)
            executed += 1
        except Exception as e:  # pyrevm raises a bare RuntimeError on revert
            log.debug("swap %d reverted: %s", idx, e)
            skipped += 1
            continue
        ts = int(row["ts"])
        if ts - last_snap_ts >= snapshot_period_s:
            snapshots.append(
                _snapshot_now(
                    env,
                    key,
                    liquidity,
                    swap_index=idx,
                    ts=ts,
                    usdc_is_currency0=usdc_is_currency0,
                )
            )
            last_snap_ts = ts

    final_snap = _snapshot_now(
        env,
        key,
        liquidity,
        swap_index=len(rows) - 1,
        ts=int(rows[-1]["ts"]),
        usdc_is_currency0=usdc_is_currency0,
    )
    if not snapshots or snapshots[-1].ts != final_snap.ts:
        snapshots.append(final_snap)

    snap_df = pl.DataFrame(
        {
            "ts": [s.ts for s in snapshots],
            "swap_index": [s.swap_index for s in snapshots],
            "sqrt_price_x96": [str(s.sqrt_price_x96) for s in snapshots],
            "tick": [s.tick for s in snapshots],
            "lp_amount0_raw": [str(s.lp_amount0_raw) for s in snapshots],
            "lp_amount1_raw": [str(s.lp_amount1_raw) for s in snapshots],
            "lp_value_usdc": [s.lp_value_usdc for s in snapshots],
        }
    )

    return ReplayResult(
        snapshots=snap_df,
        final_lp_amount0_raw=final_snap.lp_amount0_raw,
        final_lp_amount1_raw=final_snap.lp_amount1_raw,
        initial_lp_value_usdc=initial_snap.lp_value_usdc,
        final_lp_value_usdc=final_snap.lp_value_usdc,
        swaps_executed=executed,
        swaps_skipped=skipped,
    )


def cli(argv: list[str] | None = None) -> int:
    """Entry point: replay a parquet of swaps and dump snapshots to disk.

    Note: step 3 has no arb-to-truth, so the pool's internal price drifts
    from the source's after each swap. The equity series is informational
    until step 4 wires arb-to-truth.
    """
    import argparse

    parser = argparse.ArgumentParser(description="v4 full-range replay (step 3)")
    parser.add_argument(
        "--swaps",
        type=Path,
        default=Path("data/cache/swaps_88e6a0c2.parquet"),
        help="path to the step-1 swap parquet",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/cache/equity_fullrange.parquet"),
        help="output path for the equity snapshot parquet",
    )
    parser.add_argument("--limit", type=int, default=0, help="cap on rows (0 = all)")
    parser.add_argument(
        "--snapshot-period-s",
        type=int,
        default=DEFAULT_SNAPSHOT_PERIOD_S,
        help="seconds between equity snapshots",
    )
    parser.add_argument(
        "--lp-notional-usdc", type=float, default=1_000_000.0, help="LP USDC value at t=0"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not args.swaps.exists():
        parser.error(f"swap parquet not found: {args.swaps}")
    df = pl.read_parquet(args.swaps).sort(["ts", "block", "log_index"])
    if args.limit:
        df = df.head(args.limit)
    log.info("loaded %d swap rows from %s", df.height, args.swaps)

    result = replay_full_range(
        df,
        snapshot_period_s=args.snapshot_period_s,
        lp_notional_usdc=args.lp_notional_usdc,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    result.snapshots.write_parquet(args.out)
    log.info(
        "executed=%d skipped=%d snapshots=%d initial_value=%.2f final_value=%.2f -> %s",
        result.swaps_executed,
        result.swaps_skipped,
        result.snapshots.height,
        result.initial_lp_value_usdc,
        result.final_lp_value_usdc,
        args.out,
    )
    return 0
