"""Equity-curve figures from the long-form snapshot frame.

The headline curve is total LP wealth over time — position underlying *plus*
uncollected fees — one trace per world, so worlds are compared on the same
axis. A companion figure plots cumulative LVR (value lost to the arbitrageur),
which lives on a wildly different scale and so gets its own chart.
"""

from __future__ import annotations

import datetime as dt

import plotly.graph_objects as go
import polars as pl

# Consistent per-world colours across every figure in the report.
_PALETTE = [
    "#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2", "#ca8a04",
]


def world_colors(worlds: list[str]) -> dict[str, str]:
    return {w: _PALETTE[i % len(_PALETTE)] for i, w in enumerate(worlds)}


def _timestamps(ts_col: pl.Series) -> list[dt.datetime]:
    return [dt.datetime.fromtimestamp(int(t), tz=dt.UTC) for t in ts_col]


def equity_figure(snapshots: pl.DataFrame, *, colors: dict[str, str] | None = None) -> go.Figure:
    """Total LP wealth (underlying + fees) over time, one trace per world."""
    worlds = list(snapshots["world"].unique(maintain_order=True))
    colors = colors or world_colors(worlds)
    fig = go.Figure()
    for w in worlds:
        sub = snapshots.filter(pl.col("world") == w).sort("ts")
        fig.add_trace(
            go.Scatter(
                x=_timestamps(sub["ts"]),
                y=sub["lp_value_plus_fees_usdc"].to_list(),
                name=w,
                mode="lines",
                line=dict(color=colors[w], width=2),
                hovertemplate="%{x|%Y-%m-%d %H:%M}<br>" + w + ": $%{y:,.0f}<extra></extra>",
            )
        )
    fig.update_layout(
        title="LP wealth over time (underlying + fees)",
        xaxis_title="time (UTC)",
        yaxis_title="LP value (USDC)",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        hovermode="x unified",
    )
    return fig


def lvr_figure(snapshots: pl.DataFrame, *, colors: dict[str, str] | None = None) -> go.Figure:
    """Cumulative LVR (value extracted by the arbitrageur) over time."""
    worlds = list(snapshots["world"].unique(maintain_order=True))
    colors = colors or world_colors(worlds)
    fig = go.Figure()
    for w in worlds:
        sub = snapshots.filter(pl.col("world") == w).sort("ts")
        fig.add_trace(
            go.Scatter(
                x=_timestamps(sub["ts"]),
                y=sub["cum_arb_extracted_usdc"].to_list(),
                name=w,
                mode="lines",
                line=dict(color=colors[w], width=2),
                hovertemplate="%{x|%Y-%m-%d %H:%M}<br>" + w + ": $%{y:,.0f}<extra></extra>",
            )
        )
    fig.update_layout(
        title="Cumulative LVR — value extracted by arbitrage",
        xaxis_title="time (UTC)",
        yaxis_title="cumulative arb PnL (USDC)",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        hovermode="x unified",
    )
    return fig


def fees_figure(snapshots: pl.DataFrame, *, colors: dict[str, str] | None = None) -> go.Figure:
    """Cumulative LP fee income over time, one trace per world."""
    worlds = list(snapshots["world"].unique(maintain_order=True))
    colors = colors or world_colors(worlds)
    fig = go.Figure()
    for w in worlds:
        sub = snapshots.filter(pl.col("world") == w).sort("ts")
        fig.add_trace(
            go.Scatter(
                x=_timestamps(sub["ts"]),
                y=sub["fees_usdc"].to_list(),
                name=w,
                mode="lines",
                line=dict(color=colors[w], width=2),
                hovertemplate="%{x|%Y-%m-%d %H:%M}<br>" + w + ": $%{y:,.0f}<extra></extra>",
            )
        )
    fig.update_layout(
        title="Cumulative LP fee income",
        xaxis_title="time (UTC)",
        yaxis_title="fees earned (USDC)",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        hovermode="x unified",
    )
    return fig
