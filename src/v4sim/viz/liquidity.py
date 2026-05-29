"""Liquidity-placement-vs-price figure.

Plots the pool price path for one world and shades that world's active LP
range, so you can see how much of the time the price sat inside the position
earning fees. Most useful for a hook world: if a hook actively rebalances, its
band tracks the price; a passive ±band position has a static range the price
can wander out of.

Full-range positions span (almost) the whole price axis, so the shaded band
is omitted for them — there's nothing to show.
"""

from __future__ import annotations

import datetime as dt

import plotly.graph_objects as go
import polars as pl

from v4sim.metrics.accounting import volatile_price_in_usdc
from v4sim.replay.runner import WorldResult


def _timestamps(ts_col: pl.Series) -> list[dt.datetime]:
    return [dt.datetime.fromtimestamp(int(t), tz=dt.UTC) for t in ts_col]


def _price_series(snapshots: pl.DataFrame, world: WorldResult) -> list[float]:
    return [
        volatile_price_in_usdc(
            int(s), decimals0=world.decimals0, decimals1=world.decimals1,
            usdc_is_token0=world.usdc_is_currency0,
        )
        for s in snapshots["sqrt_price_x96"]
    ]


def liquidity_figure(snapshots: pl.DataFrame, world: WorldResult) -> go.Figure:
    """Price path for ``world`` with its LP band shaded.

    The band is drawn per-snapshot, so an active world that re-centres shows a
    band that tracks the price; a passive band is flat. Full-range is omitted
    (the band would span the whole axis).
    """
    sub = snapshots.filter(pl.col("world") == world.name).sort("ts")
    times = _timestamps(sub["ts"])
    prices = _price_series(sub, world)

    fig = go.Figure()

    has_band = world.kind != "full_range" and "band_low_usdc" in sub.columns
    if has_band:
        lows = sub["band_low_usdc"].to_list()
        highs = sub["band_high_usdc"].to_list()
        # Filled band between low/high edges, as a step (band holds until the
        # next rebalance), so re-centres read as discrete jumps.
        fig.add_trace(
            go.Scatter(
                x=times, y=highs, mode="lines", line=dict(width=0, shape="hv"),
                showlegend=False, hoverinfo="skip",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=times, y=lows, mode="lines", line=dict(width=0, shape="hv"),
                fill="tonexty", fillcolor="rgba(22,163,74,0.15)",
                name="LP band", hoverinfo="skip",
            )
        )

    fig.add_trace(
        go.Scatter(
            x=times, y=prices, mode="lines", name="pool price",
            line=dict(color="#2563eb", width=2),
            hovertemplate="%{x|%Y-%m-%d %H:%M}<br>$%{y:,.2f}<extra></extra>",
        )
    )

    if has_band and prices:
        lo = min(min(prices), min(lows)) * 0.98
        hi = max(max(prices), max(highs)) * 1.02
        fig.update_yaxes(range=[lo, hi])

    rebal = f" ({world.rebalances} rebalances)" if world.rebalances else ""
    fig.update_layout(
        title=f"Liquidity placement vs price — {world.name}{rebal}",
        xaxis_title="time (UTC)",
        yaxis_title="price (USDC per volatile token)",
        template="plotly_white",
        hovermode="x unified",
    )
    return fig
