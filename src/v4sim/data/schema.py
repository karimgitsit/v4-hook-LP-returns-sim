"""Polars schema for the swap-record Parquet cache.

One row per Uniswap v3 Swap event. We store enough to replay each swap
against a v4 pool and to verify the post-swap state.

Convention for `dir`:
    0  =>  zeroForOne  (trader sends token0, receives token1)
    1  =>  oneForZero  (trader sends token1, receives token0)

Conventions for amounts:
    amount0, amount1 are SIGNED, denominated in the token's natural units
    (decimal-adjusted floats, as the Uniswap subgraph reports them). The
    positive side is what the pool received from the trader (gross of
    fee); the negative side is what the pool paid out.

    amount_in is the absolute value of the positive side — i.e. the gross
    input including LP fee. This is what gets passed to PoolSwapTest.

    sqrt_price_x96_post is the X96 fixed-point sqrtPrice AFTER the swap.
    This is the "external truth" the v4 pool gets arb'd to in the
    harness.
"""

from __future__ import annotations

import polars as pl

# Polars schema for swap rows. Order here is the canonical column order in
# the Parquet file.
SWAP_SCHEMA: dict[str, pl.DataType] = {
    "id": pl.Utf8,             # subgraph swap id (tx_hash#logIndex)
    "ts": pl.Int64,            # unix seconds
    "block": pl.Int64,         # block number
    "log_index": pl.Int64,     # log index within the block
    "tx_hash": pl.Utf8,        # for debugging only
    "pool": pl.Utf8,           # pool address, lowercase
    "dir": pl.Int8,            # 0 = zeroForOne, 1 = oneForZero
    "amount0": pl.Float64,     # signed, decimal-adjusted
    "amount1": pl.Float64,     # signed, decimal-adjusted
    "amount_in": pl.Float64,   # abs of positive side, decimal-adjusted
    "sqrt_price_x96_post": pl.Utf8,   # uint160 stored as decimal string
    "tick_post": pl.Int32,
    "sender": pl.Utf8,
    "recipient": pl.Utf8,
    "origin": pl.Utf8,
}

# Columns that are required for replay; the rest are informational.
REPLAY_REQUIRED_COLS = (
    "ts",
    "block",
    "log_index",
    "dir",
    "amount_in",
    "sqrt_price_x96_post",
)


def empty_frame() -> pl.DataFrame:
    """Return an empty DataFrame with the canonical schema."""
    return pl.DataFrame(schema=SWAP_SCHEMA)
