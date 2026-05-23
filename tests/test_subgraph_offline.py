"""Offline tests for the subgraph puller (no network).

We exercise normalization + frame-building with synthetic payloads so the
puller's data-handling logic is covered without needing a Graph API key.
"""

from __future__ import annotations

import polars as pl
import pytest

from v4sim.data.schema import SWAP_SCHEMA, empty_frame
from v4sim.data.subgraph import SwapPuller


def _raw_swap(
    sid: str,
    ts: int,
    block: int,
    log_index: int,
    amount0: str,
    amount1: str,
    sqrt_p: str = "1000000000000000000000000",
    tick: int = -200000,
) -> dict:
    return {
        "id": sid,
        "timestamp": str(ts),
        "logIndex": str(log_index),
        "transaction": {"id": f"0x{block:064x}", "blockNumber": str(block)},
        "amount0": amount0,
        "amount1": amount1,
        "sqrtPriceX96": sqrt_p,
        "tick": str(tick),
        "sender": "0xaaa",
        "recipient": "0xbbb",
        "origin": "0xccc",
    }


def test_normalize_direction_and_amount_in():
    pool = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"

    # zeroForOne: trader sends USDC (token0), receives WETH (token1)
    z4o = SwapPuller._normalize_row(
        _raw_swap("a-1", 100, 10, 0, amount0="1000.5", amount1="-0.5"), pool
    )
    assert z4o["dir"] == 0
    assert z4o["amount_in"] == pytest.approx(1000.5)

    # oneForZero
    o4z = SwapPuller._normalize_row(
        _raw_swap("b-1", 100, 11, 0, amount0="-1500.0", amount1="0.7"), pool
    )
    assert o4z["dir"] == 1
    assert o4z["amount_in"] == pytest.approx(0.7)


def test_build_frame_sorts_and_matches_schema():
    pool = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"
    rows = [
        SwapPuller._normalize_row(
            _raw_swap("a-1", 100, 12, 5, "1.0", "-0.5"), pool
        ),
        SwapPuller._normalize_row(
            _raw_swap("a-2", 100, 10, 3, "2.0", "-1.0"), pool
        ),
        SwapPuller._normalize_row(
            _raw_swap("a-3", 100, 12, 1, "-1.0", "0.6"), pool
        ),
    ]
    df = SwapPuller._build_frame(rows)
    # Schema match
    assert list(df.columns) == list(SWAP_SCHEMA.keys())
    # Sorted by (block, log_index)
    assert df["block"].to_list() == [10, 12, 12]
    assert df["log_index"].to_list() == [3, 1, 5]


def test_empty_frame_has_canonical_schema():
    df = empty_frame()
    assert df.height == 0
    assert list(df.columns) == list(SWAP_SCHEMA.keys())
    # column dtypes match
    for name, dtype in SWAP_SCHEMA.items():
        assert df.schema[name] == dtype, name


def test_build_frame_empty_returns_empty():
    df = SwapPuller._build_frame([])
    assert df.height == 0
    assert isinstance(df, pl.DataFrame)
