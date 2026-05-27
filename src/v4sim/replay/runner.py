"""Single-world v4 replay: drive a freshly-deployed pool through historical
v3 swaps, optionally arb the pool back to the v3 post-swap price between
each step, and emit a per-snapshot equity series.

With `arb_to_truth=True` (the default, step-4 behaviour), the v4 pool's
sqrtPrice tracks the source within 1 tick after every swap. The arb's
PnL — valued at the v3 truth price — is accumulated as
`cum_arb_extracted_usdc` and reported per-snapshot. With
`arb_to_truth=False` (step-3 mode), the pool drifts and the equity
series reflects the pool's internal price only.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from v4sim.evm.arb import arb_to_target
from v4sim.evm.env import V4Env, bootstrap_v4_eth_usdc
from v4sim.evm.pool import PoolKey, initialize, read_slot0, swap
from v4sim.metrics.accounting import (
    amounts_for_liquidity,
    price_token1_in_token0,
    usdc_value_of_position,
)
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
    cum_arb_extracted_usdc: float


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
    cum_arb_extracted_usdc: float,
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
        cum_arb_extracted_usdc=cum_arb_extracted_usdc,
    )


def _arb_pnl_usdc(
    env: V4Env, delta0: int, delta1: int, truth_sqrt_x96: int, *, usdc_is_currency0: bool
) -> float:
    """Value the arb's (delta0, delta1) at the v3 truth sqrtPrice, in USDC.

    Positive return = arb captured value from LPs at the truth price.
    """
    return usdc_value_of_position(
        delta0,
        delta1,
        truth_sqrt_x96,
        decimals0=env.decimals0,
        decimals1=env.decimals1,
        usdc_is_token0=usdc_is_currency0,
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
    arb_to_truth :
        If True (default, step 4), after each user swap a perfect arb pushes
        the pool's sqrtPrice back to the row's `sqrt_price_x96_post`. If
        False (step 3 diagnostic mode), no arb runs and the pool drifts.

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

    # Initial sqrtPrice = row 0's `sqrt_price_x96_post` (project decision).
    init_sqrt_x96 = _parse_sqrt_price(rows[0]["sqrt_price_x96_post"])
    initialize(env, key, init_sqrt_x96)

    # 50/50 USDC value at the initial price.
    half_usdc = lp_notional_usdc / 2.0
    p_raw = price_token1_in_token0(init_sqrt_x96)
    price_t1_per_t0_human = p_raw * (10 ** (env.decimals0 - env.decimals1))
    if usdc_is_currency0:
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
    cum_arb = 0.0
    initial_snap = _snapshot_now(
        env,
        key,
        liquidity,
        swap_index=0,
        ts=int(rows[0]["ts"]),
        cum_arb_extracted_usdc=cum_arb,
        usdc_is_currency0=usdc_is_currency0,
    )
    snapshots.append(initial_snap)
    last_snap_ts = initial_snap.ts

    executed = 0
    skipped = 0
    arbs = 0
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

        if arb_to_truth:
            truth_sp = _parse_sqrt_price(row["sqrt_price_x96_post"])
            arb = arb_to_target(env, key, truth_sp)
            if not arb.skipped:
                arbs += 1
                cum_arb += _arb_pnl_usdc(
                    env,
                    arb.delta0,
                    arb.delta1,
                    truth_sp,
                    usdc_is_currency0=usdc_is_currency0,
                )

        ts = int(row["ts"])
        if ts - last_snap_ts >= snapshot_period_s:
            snapshots.append(
                _snapshot_now(
                    env,
                    key,
                    liquidity,
                    swap_index=idx,
                    ts=ts,
                    cum_arb_extracted_usdc=cum_arb,
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
        cum_arb_extracted_usdc=cum_arb,
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
            "cum_arb_extracted_usdc": [s.cum_arb_extracted_usdc for s in snapshots],
        }
    )

    return ReplayResult(
        snapshots=snap_df,
        final_lp_amount0_raw=final_snap.lp_amount0_raw,
        final_lp_amount1_raw=final_snap.lp_amount1_raw,
        initial_lp_value_usdc=initial_snap.lp_value_usdc,
        final_lp_value_usdc=final_snap.lp_value_usdc,
        cum_arb_extracted_usdc=cum_arb,
        swaps_executed=executed,
        swaps_skipped=skipped,
        arbs_executed=arbs,
    )


def _parse_when(value: str) -> int:
    """Accept either a unix ts ('1779000000') or an ISO date/datetime."""
    s = value.strip()
    if s.isdigit():
        return int(s)
    # date-only is fine, datetime.fromisoformat handles 'YYYY-MM-DD' on 3.11+.
    parsed = dt.datetime.fromisoformat(s)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return int(parsed.timestamp())


def _apply_window(df: pl.DataFrame, *, start: int | None, end: int | None, days: int | None) -> pl.DataFrame:
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
    """Replay a parquet of swaps, arb back to truth after each, and dump snapshots."""
    import argparse

    parser = argparse.ArgumentParser(description="v4 full-range replay with arb-to-truth (step 4)")
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
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="window start (ISO datetime, ISO date, or unix ts); inclusive",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="window end (same formats as --start); exclusive",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="alternative to --start/--end: take the last N days of the parquet",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="cap on rows after windowing (0 = all)",
    )
    parser.add_argument(
        "--snapshot-period-s",
        type=int,
        default=DEFAULT_SNAPSHOT_PERIOD_S,
        help="seconds between equity snapshots",
    )
    parser.add_argument(
        "--lp-notional-usdc", type=float, default=1_000_000.0, help="LP USDC value at t=0"
    )
    parser.add_argument(
        "--no-arb",
        action="store_true",
        help="disable arb-to-truth (step-3 diagnostic mode)",
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

    result = replay_full_range(
        df,
        snapshot_period_s=args.snapshot_period_s,
        lp_notional_usdc=args.lp_notional_usdc,
        arb_to_truth=not args.no_arb,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    result.snapshots.write_parquet(args.out)
    log.info(
        "executed=%d skipped=%d arbs=%d snapshots=%d initial=%.2f final=%.2f cum_arb=%.2f -> %s",
        result.swaps_executed,
        result.swaps_skipped,
        result.arbs_executed,
        result.snapshots.height,
        result.initial_lp_value_usdc,
        result.final_lp_value_usdc,
        result.cum_arb_extracted_usdc,
        args.out,
    )
    return 0
