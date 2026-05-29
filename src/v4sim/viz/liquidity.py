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
    """Price path for ``world`` with its LP range shaded."""
    sub = snapshots.filter(pl.col("world") == world.name).sort("ts")
    times = _timestamps(sub["ts"])
    prices = _price_series(sub, world)

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=times, y=prices, mode="lines", name="pool price",
            line=dict(color="#2563eb", width=2),
            hovertemplate="%{x|%Y-%m-%d %H:%M}<br>$%{y:,.2f}<extra></extra>",
        )
    )

    # Shade the active LP band, unless full-range (band would span the axis).
    if world.sqrt_a_x96 and world.sqrt_b_x96 and world.kind != "full_range":
        edges = sorted(
            volatile_price_in_usdc(
                s, decimals0=world.decimals0, decimals1=world.decimals1,
                usdc_is_token0=world.usdc_is_currency0,
            )
            for s in (world.sqrt_a_x96, world.sqrt_b_x96)
        )
        band_low, band_high = edges
        # Clip the shaded band to a sensible view around the price path.
        if prices:
            lo = min(min(prices), band_low) * 0.98
            hi = max(max(prices), band_high) * 1.02
            fig.update_yaxes(range=[lo, hi])
        fig.add_hrect(
            y0=band_low, y1=band_high, fillcolor="#16a34a", opacity=0.12,
            line_width=0, annotation_text="LP range", annotation_position="top left",
        )
        fig.add_hline(y=band_low, line_dash="dash", line_color="#16a34a", line_width=1)
        fig.add_hline(y=band_high, line_dash="dash", line_color="#16a34a", line_width=1)

    fig.update_layout(
        title=f"Liquidity placement vs price — {world.name}",
        xaxis_title="time (UTC)",
        yaxis_title="price (USDC per volatile token)",
        template="plotly_white",
        hovermode="x unified",
    )
    return fig
