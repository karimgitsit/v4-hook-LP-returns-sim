"""Attribution figures: the HODL-baseline PnL decomposition per world.

Two views:

- :func:`attribution_bar` — grouped bars of the net-PnL components (fees, IL,
  gas) plus the net total, all on one axis. Gas and IL are signed, so losses
  sit below zero.
- :func:`lvr_bar` — LVR per world on its own axis (it dwarfs the net-PnL
  terms, so mixing the two scales would flatten the interesting chart).
"""

from __future__ import annotations

import plotly.graph_objects as go
import polars as pl


def attribution_bar(attribution_df: pl.DataFrame) -> go.Figure:
    """Grouped bars: fees / IL / gas / net LP PnL, per world."""
    names = list(attribution_df["name"])
    fig = go.Figure()
    components = [
        ("fees_usdc", "Fees", "#16a34a"),
        ("il_usdc", "IL (signed)", "#dc2626"),
        ("gas_usdc", "Gas", "#ca8a04"),
        ("net_lp_pnl_usdc", "Net LP PnL", "#2563eb"),
    ]
    for col, label, color in components:
        # Gas is a cost; show it as negative in the bar chart.
        ys = attribution_df[col].to_list()
        if col == "gas_usdc":
            ys = [-y for y in ys]
        fig.add_trace(
            go.Bar(
                x=names,
                y=ys,
                name=label,
                marker_color=color,
                hovertemplate="%{x}<br>" + label + ": $%{y:,.2f}<extra></extra>",
            )
        )
    fig.update_layout(
        title="Return attribution vs HODL (net LP PnL = fees + IL − gas)",
        xaxis_title="world",
        yaxis_title="USDC",
        barmode="group",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    fig.add_hline(y=0, line_width=1, line_color="#444")
    return fig


def lvr_bar(attribution_df: pl.DataFrame) -> go.Figure:
    """LVR (arb-extracted value) per world, on its own scale."""
    names = list(attribution_df["name"])
    fig = go.Figure(
        go.Bar(
            x=names,
            y=attribution_df["lvr_usdc"].to_list(),
            marker_color="#9333ea",
            hovertemplate="%{x}<br>LVR: $%{y:,.0f}<extra></extra>",
        )
    )
    fig.update_layout(
        title="LVR — value lost to arbitrage, per world",
        xaxis_title="world",
        yaxis_title="cumulative arb PnL (USDC)",
        template="plotly_white",
    )
    return fig


def returns_bar(attribution_df: pl.DataFrame) -> go.Figure:
    """Total LP return vs HODL return, per world (percent)."""
    names = list(attribution_df["name"])
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=names, y=attribution_df["hodl_return_pct"].to_list(),
            name="HODL return", marker_color="#94a3b8",
            hovertemplate="%{x}<br>HODL: %{y:.2f}%<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=names, y=attribution_df["total_return_pct"].to_list(),
            name="LP total return", marker_color="#2563eb",
            hovertemplate="%{x}<br>LP: %{y:.2f}%<extra></extra>",
        )
    )
    fig.update_layout(
        title="LP total return vs HODL return",
        xaxis_title="world",
        yaxis_title="return (%)",
        barmode="group",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    fig.add_hline(y=0, line_width=1, line_color="#444")
    return fig
