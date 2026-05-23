"""Uniswap v3 subgraph → Parquet cache.

Pulls swap events for a given pool and time range from the Uniswap V3
subgraph (decentralized network gateway), and writes them to a Parquet
cache file matching ``v4sim.data.schema.SWAP_SCHEMA``.

Pagination
----------
The subgraph caps results at 1000 per query. We paginate by
``timestamp_gte`` cursor with client-side dedup on ``Swap.id`` to handle
the case where multiple swaps share a timestamp at the page boundary.
We never use ``skip``, which is capped at 5000 by the gateway.

Usage
-----
As a library::

    from v4sim.data.subgraph import SwapPuller
    df = SwapPuller().fetch_range(
        pool="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
        start_ts=1714521600,
        end_ts=1715126400,
    )

As a CLI::

    GRAPH_API_KEY=... v4sim-pull-swaps --days 7
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import polars as pl
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from v4sim.data.schema import SWAP_SCHEMA, empty_frame

log = logging.getLogger(__name__)

# Uniswap V3 mainnet subgraph on the decentralized network.
DEFAULT_SUBGRAPH_ID = "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"

# ETH / USDC 5bps pool on mainnet. token0 = USDC (6 dec), token1 = WETH (18 dec).
DEFAULT_POOL = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"

PAGE_SIZE = 1000  # subgraph max for `first`

_QUERY = """
query Swaps($pool: String!, $startTs: BigInt!, $endTs: BigInt!, $first: Int!) {
  swaps(
    first: $first
    orderBy: timestamp
    orderDirection: asc
    where: {
      pool: $pool
      timestamp_gte: $startTs
      timestamp_lt: $endTs
    }
  ) {
    id
    timestamp
    logIndex
    transaction { id blockNumber }
    amount0
    amount1
    sqrtPriceX96
    tick
    sender
    recipient
    origin
  }
}
""".strip()


class SubgraphError(RuntimeError):
    """Raised when the subgraph returns an error or malformed payload."""


@dataclass
class PullStats:
    pages: int = 0
    raw_rows: int = 0
    deduped_rows: int = 0
    elapsed_s: float = 0.0


class SwapPuller:
    """Paginated swap puller for the Uniswap V3 subgraph."""

    def __init__(
        self,
        api_key: str | None = None,
        subgraph_id: str = DEFAULT_SUBGRAPH_ID,
        timeout_s: float = 30.0,
    ):
        self.api_key = api_key or os.environ.get("GRAPH_API_KEY")
        if not self.api_key:
            raise SubgraphError(
                "GRAPH_API_KEY not set. Create a key at "
                "https://thegraph.com/studio/apikeys/ and export it."
            )
        self.subgraph_id = subgraph_id
        self.timeout_s = timeout_s
        self.endpoint = (
            f"https://gateway.thegraph.com/api/{self.api_key}"
            f"/subgraphs/id/{self.subgraph_id}"
        )

    # ------------------------------------------------------------------
    # Public API

    def fetch_range(
        self,
        pool: str,
        start_ts: int,
        end_ts: int,
    ) -> tuple[pl.DataFrame, PullStats]:
        """Pull every Swap for `pool` in [start_ts, end_ts) → DataFrame.

        Returns the canonical-schema DataFrame plus a PullStats summary.
        """
        pool = pool.lower()
        stats = PullStats()
        t0 = time.monotonic()

        cursor = start_ts
        seen_ids: set[str] = set()
        # Keep ids from the most recent page only — that's all we need
        # for dedup at the timestamp cursor boundary.
        prev_page_ids: set[str] = set()
        rows: list[dict] = []

        while cursor < end_ts:
            page = self._fetch_page(pool, cursor, end_ts)
            stats.pages += 1
            stats.raw_rows += len(page)

            if not page:
                break

            new_rows = []
            max_ts_in_page = cursor
            page_ids: set[str] = set()
            for s in page:
                sid = s["id"]
                page_ids.add(sid)
                if sid in prev_page_ids:
                    # already kept on previous page (boundary dup)
                    continue
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                new_rows.append(self._normalize_row(s, pool))
                ts = int(s["timestamp"])
                if ts > max_ts_in_page:
                    max_ts_in_page = ts

            rows.extend(new_rows)
            log.info(
                "page %d: %d raw, %d new, cursor %s → %s",
                stats.pages,
                len(page),
                len(new_rows),
                _fmt_ts(cursor),
                _fmt_ts(max_ts_in_page),
            )

            # End of data inside [cursor, end_ts).
            if len(page) < PAGE_SIZE:
                break

            # Advance cursor. If every row on the page shares a single
            # timestamp, we have to break to avoid infinite looping —
            # but PAGE_SIZE worth of swaps in one second on a single pool
            # is extraordinarily rare. Surface it loudly.
            if max_ts_in_page == cursor:
                raise SubgraphError(
                    f"page of {len(page)} swaps all share timestamp "
                    f"{cursor}; cursor pagination can't advance. Either "
                    "tighten the window or implement (timestamp, id) "
                    "pagination."
                )
            cursor = max_ts_in_page
            prev_page_ids = page_ids

        stats.deduped_rows = len(rows)
        stats.elapsed_s = time.monotonic() - t0
        df = self._build_frame(rows)
        return df, stats

    # ------------------------------------------------------------------
    # Internals

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _fetch_page(self, pool: str, start_ts: int, end_ts: int) -> list[dict]:
        payload = {
            "query": _QUERY,
            "variables": {
                "pool": pool,
                "startTs": str(start_ts),
                "endTs": str(end_ts),
                "first": PAGE_SIZE,
            },
        }
        with httpx.Client(timeout=self.timeout_s) as c:
            r = c.post(self.endpoint, json=payload)
        # 4xx is a configuration / auth issue, not a transient network
        # error — surface it immediately instead of looping with backoff.
        if 400 <= r.status_code < 500:
            raise SubgraphError(
                f"HTTP {r.status_code} from gateway: {r.text[:200]!r}"
            )
        r.raise_for_status()
        body = r.json()
        if "errors" in body:
            raise SubgraphError(f"subgraph errors: {body['errors']}")
        data = body.get("data") or {}
        swaps = data.get("swaps")
        if swaps is None:
            raise SubgraphError(f"unexpected payload shape: {body}")
        return swaps

    @staticmethod
    def _normalize_row(s: dict, pool: str) -> dict:
        amount0 = float(s["amount0"])
        amount1 = float(s["amount1"])
        # zeroForOne: trader sent token0 in, pool's token0 balance went up
        # → amount0 positive, amount1 negative.
        if amount0 > 0 and amount1 < 0:
            direction = 0
            amount_in = amount0
        elif amount1 > 0 and amount0 < 0:
            direction = 1
            amount_in = amount1
        else:
            # Degenerate / zero-amount swap; classify by sign of either,
            # default to zeroForOne. Won't matter for replay since it's
            # a no-op.
            direction = 0 if amount0 >= 0 else 1
            amount_in = abs(amount0) if direction == 0 else abs(amount1)

        return {
            "id": s["id"],
            "ts": int(s["timestamp"]),
            "block": int(s["transaction"]["blockNumber"]),
            "log_index": int(s.get("logIndex") or 0),
            "tx_hash": s["transaction"]["id"],
            "pool": pool,
            "dir": direction,
            "amount0": amount0,
            "amount1": amount1,
            "amount_in": amount_in,
            "sqrt_price_x96_post": str(s["sqrtPriceX96"]),
            "tick_post": int(s["tick"]),
            "sender": s.get("sender") or "",
            "recipient": s.get("recipient") or "",
            "origin": s.get("origin") or "",
        }

    @staticmethod
    def _build_frame(rows: list[dict]) -> pl.DataFrame:
        if not rows:
            return empty_frame()
        df = pl.DataFrame(rows, schema=SWAP_SCHEMA)
        # Stable ordering for replay: ascending (block, log_index).
        return df.sort(["block", "log_index"])


def _fmt_ts(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.UTC).isoformat()


# ----------------------------------------------------------------------
# CLI

def _resolve_window(days: int, end_iso: str | None) -> tuple[int, int]:
    """Return (start_ts, end_ts) for ``days`` ending at ``end_iso`` (UTC).

    Default ``end`` is the start of today UTC (so we pull complete days).
    """
    if end_iso:
        end = dt.datetime.fromisoformat(end_iso).replace(tzinfo=dt.UTC)
    else:
        now = dt.datetime.now(tz=dt.UTC)
        end = dt.datetime(now.year, now.month, now.day, tzinfo=dt.UTC)
    start = end - dt.timedelta(days=days)
    return int(start.timestamp()), int(end.timestamp())


def cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="v4sim-pull-swaps",
        description="Pull Uniswap v3 swaps from the subgraph → Parquet cache.",
    )
    p.add_argument("--pool", default=DEFAULT_POOL, help="Pool address (lowercase).")
    p.add_argument("--days", type=int, default=7, help="Days of history to pull.")
    p.add_argument(
        "--end",
        default=None,
        help="ISO end timestamp (UTC); default = start of today UTC.",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Output parquet path. Default = data/cache/swaps_<pool-tag>.parquet",
    )
    p.add_argument(
        "--subgraph-id",
        default=DEFAULT_SUBGRAPH_ID,
        help="Subgraph deployment id on the decentralized network.",
    )
    p.add_argument("-v", "--verbose", action="count", default=0)
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose > 1 else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    start_ts, end_ts = _resolve_window(args.days, args.end)
    out = Path(args.out) if args.out else _default_out_path(args.pool)
    out.parent.mkdir(parents=True, exist_ok=True)

    log.info(
        "pulling swaps  pool=%s  range=%s → %s  out=%s",
        args.pool,
        _fmt_ts(start_ts),
        _fmt_ts(end_ts),
        out,
    )

    try:
        puller = SwapPuller(subgraph_id=args.subgraph_id)
        df, stats = puller.fetch_range(args.pool, start_ts, end_ts)
    except SubgraphError as e:
        log.error("subgraph error: %s", e)
        return 2

    df.write_parquet(out, compression="zstd")

    # Stdout summary the harness uses to gate the next step.
    print()
    print(f"rows           : {df.height}")
    print(f"pages          : {stats.pages}")
    print(f"raw rows seen  : {stats.raw_rows}")
    print(f"elapsed (s)    : {stats.elapsed_s:.2f}")
    print(f"out            : {out}")
    if df.height:
        first = df.select(pl.col("ts").min()).item()
        last = df.select(pl.col("ts").max()).item()
        print(f"ts range       : {_fmt_ts(first)} → {_fmt_ts(last)}")
        print()
        print("head:")
        with pl.Config(tbl_cols=-1, tbl_width_chars=200):
            print(df.head())
    return 0


def _default_out_path(pool: str) -> Path:
    tag = pool.lower()[2:10]  # short tag from address
    return Path("data/cache") / f"swaps_{tag}.parquet"


if __name__ == "__main__":
    sys.exit(cli())
