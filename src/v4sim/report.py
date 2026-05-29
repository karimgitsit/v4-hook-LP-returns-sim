"""`v4sim-report` — run the replay and emit the standalone HTML report.

Thin orchestration layer: window the cached swaps (same flags as
``v4sim-replay``), run the worlds, then hand the result to
:mod:`v4sim.viz.report`. Optionally adds a hook world from a forge artifact.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
from pathlib import Path

import polars as pl

from v4sim.metrics.gas import GasModel
from v4sim.replay.runner import (
    DEFAULT_BAND_PCT,
    DEFAULT_SNAPSHOT_PERIOD_S,
    WorldSpec,
    _apply_window,
    _parse_when,
    default_baseline_specs,
    noop_hook_spec,
    replay_worlds,
)
from v4sim.viz.report import ReportMeta, write_report

log = logging.getLogger(__name__)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the v4 replay and emit report.html")
    parser.add_argument(
        "--swaps", type=Path, default=Path("data/cache/swaps_88e6a0c2.parquet"),
        help="path to the step-1 swap parquet",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("data/cache/report.html"),
        help="output path for the self-contained HTML report",
    )
    parser.add_argument("--start", type=str, default=None, help="window start (ISO or unix ts)")
    parser.add_argument("--end", type=str, default=None, help="window end (exclusive)")
    parser.add_argument(
        "--days", type=int, default=2,
        help="take the last N days of the parquet (default 2; a full week ~25s)",
    )
    parser.add_argument("--limit", type=int, default=0, help="cap rows after windowing (0 = all)")
    parser.add_argument("--band-pct", type=float, default=DEFAULT_BAND_PCT)
    parser.add_argument("--snapshot-period-s", type=int, default=DEFAULT_SNAPSHOT_PERIOD_S)
    parser.add_argument("--lp-notional-usdc", type=float, default=1_000_000.0)
    parser.add_argument("--no-arb", action="store_true", help="disable arb-to-truth (drift mode)")
    parser.add_argument("--demo-hook", action="store_true", help="add a no-op MockHooks world")
    parser.add_argument("--hook", type=Path, default=None, help="forge artifact JSON for a hook world")
    parser.add_argument(
        "--hook-flags", type=lambda s: int(s, 0), default=None, help="hook permission flag bits"
    )
    parser.add_argument("--title", type=str, default="v4 hook LP returns")
    parser.add_argument("--gas-price-gwei", type=float, default=None, help="override gas price (gwei)")
    parser.add_argument("--gas-per-rebalance", type=int, default=None, help="gas units per rebalance")
    parser.add_argument("--eth-price-usd", type=float, default=None, help="ETH price for gas valuation")
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

    specs = default_baseline_specs(band_pct=args.band_pct)
    if args.demo_hook:
        specs.append(noop_hook_spec(band_pct=args.band_pct))
    if args.hook is not None:
        if args.hook_flags is None:
            parser.error("--hook requires --hook-flags")
        from v4sim.evm.artifacts import load_artifact_file

        specs.append(
            WorldSpec(
                name="hook", kind="hook", band_pct=args.band_pct,
                hook_artifact=load_artifact_file(args.hook), hook_flags=args.hook_flags,
            )
        )

    gas_overrides = {
        k: v
        for k, v in {
            "gas_price_gwei": args.gas_price_gwei,
            "gas_per_rebalance": args.gas_per_rebalance,
            "eth_price_usd": args.eth_price_usd,
        }.items()
        if v is not None
    }
    gas_model = GasModel(**gas_overrides)

    log.info("replaying %d swaps across %d worlds", df.height, len(specs))
    result = replay_worlds(
        df, specs,
        lp_notional_usdc=args.lp_notional_usdc,
        snapshot_period_s=args.snapshot_period_s,
        arb_to_truth=not args.no_arb,
        gas_model=gas_model,
    )

    meta = ReportMeta(
        title=args.title,
        window_start=int(df["ts"].min()),
        window_end=int(df["ts"].max()),
        n_swaps=df.height,
        band_pct=args.band_pct,
        arb_to_truth=not args.no_arb,
        gas_note=(
            f"{gas_model.gas_price_gwei:g} gwei × {gas_model.gas_per_rebalance:,} gas "
            f"@ ${gas_model.eth_price_usd:,.0f}/ETH = ${gas_model.cost_per_rebalance_usdc:,.2f}/rebalance"
        ),
        generated_at=dt.datetime.now(tz=dt.UTC),
    )
    out = write_report(result, args.out, meta)
    for name, w in result.worlds.items():
        log.info(
            "[%s] fees=%.0f net_lp_pnl(IL+fees-gas) lvr=%.0f rebalances=%d",
            name, w.fees_usdc, w.cum_arb_extracted_usdc, w.rebalances,
        )
    log.info("wrote report -> %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
