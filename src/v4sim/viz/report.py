"""Assemble the standalone HTML report.

Stitches the equity / attribution / liquidity figures and a summary table into
a single self-contained ``report.html`` — plotly.js is embedded inline, so the
file opens offline with no network. This is the always-on deliverable; the
Streamlit app (7b) reuses the same figure builders for the interactive view.
"""

from __future__ import annotations

import datetime as dt
import html
from dataclasses import dataclass
from pathlib import Path

import plotly.graph_objects as go
import plotly.io as pio
import polars as pl

from v4sim.metrics.attribution import SUMMARY_COLUMNS, attribution_table
from v4sim.replay.runner import WorldsResult
from v4sim.viz.attribution import attribution_bar, lvr_bar, returns_bar
from v4sim.viz.equity import equity_figure, fees_figure, lvr_figure, world_colors
from v4sim.viz.liquidity import liquidity_figure

_CAVEAT = (
    "The simulated v4 pool is intentionally sized to $1M of LP notional, far "
    "smaller than the real $100M+ ETH/USDC pool. Because the same historical "
    "swap volume hits a much thinner book, both fee income and LVR are inflated "
    "by the same factor — treat the <em>relative</em> comparison between worlds "
    "as the signal, not the absolute dollar magnitudes. LVR is the value the "
    "pool bled to the perfect arbitrageur; it is shown alongside net LP PnL as "
    "the ceiling an LVR-capturing hook could in principle reclaim."
)


@dataclass
class ReportMeta:
    title: str = "v4 hook LP returns"
    window_start: int | None = None
    window_end: int | None = None
    n_swaps: int = 0
    band_pct: float | None = None
    arb_to_truth: bool = True
    gas_note: str = ""
    generated_at: dt.datetime | None = None


def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return "—"
    return dt.datetime.fromtimestamp(int(ts), tz=dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_cell(col: str, value) -> str:
    if isinstance(value, float):
        if col.endswith("_pct"):
            return f"{value:,.2f}%"
        return f"{value:,.2f}"
    return html.escape(str(value))


def summary_table_html(attribution_df: pl.DataFrame) -> str:
    """Render the attribution table as a styled HTML <table>."""
    cols = [(c, label) for c, label in SUMMARY_COLUMNS if c in attribution_df.columns]
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in cols)
    body_rows = []
    for row in attribution_df.iter_rows(named=True):
        cells = "".join(f"<td>{_fmt_cell(c, row[c])}</td>" for c, _ in cols)
        body_rows.append(f"<tr>{cells}</tr>")
    body = "".join(body_rows)
    return f"<table class='summary'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _fig_div(fig: go.Figure) -> str:
    return pio.to_html(fig, full_html=False, include_plotlyjs=False, default_height="460px")


def build_report_html(result: WorldsResult, meta: ReportMeta) -> str:
    """Build the full self-contained HTML document string."""
    attribution_df = attribution_table(result)
    snapshots = result.snapshots
    worlds = list(snapshots["world"].unique(maintain_order=True))
    colors = world_colors(worlds)

    figs: list[str] = [
        _fig_div(equity_figure(snapshots, colors=colors)),
        _fig_div(fees_figure(snapshots, colors=colors)),
        _fig_div(attribution_bar(attribution_df)),
        _fig_div(returns_bar(attribution_df)),
        _fig_div(lvr_figure(snapshots, colors=colors)),
        _fig_div(lvr_bar(attribution_df)),
    ]
    # A liquidity-placement chart for each concentrated/hook world.
    for w in result.worlds.values():
        if w.kind in ("concentrated", "hook"):
            figs.append(_fig_div(liquidity_figure(snapshots, w)))

    # Embed the full plotly.js bundle once so the report opens offline.
    from plotly.offline import get_plotlyjs

    plotly_bundle = f"<script type='text/javascript'>{get_plotlyjs()}</script>"

    generated = (meta.generated_at or dt.datetime.now(tz=dt.UTC)).strftime("%Y-%m-%d %H:%M UTC")
    meta_rows = [
        ("Window", f"{_fmt_ts(meta.window_start)} → {_fmt_ts(meta.window_end)}"),
        ("Swaps replayed", f"{meta.n_swaps:,}"),
        ("Concentrated band", f"±{meta.band_pct * 100:.1f}%" if meta.band_pct else "n/a"),
        ("Arb-to-truth", "on" if meta.arb_to_truth else "off (drift)"),
        ("Gas model", html.escape(meta.gas_note) if meta.gas_note else "passive worlds: $0"),
        ("Generated", generated),
    ]
    meta_html = "".join(
        f"<div class='meta-item'><span class='meta-k'>{html.escape(k)}</span>"
        f"<span class='meta-v'>{v}</span></div>"
        for k, v in meta_rows
    )
    figs_html = "".join(f"<section class='card'>{f}</section>" for f in figs)

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(meta.title)}</title>
{plotly_bundle}
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         margin: 0; background: #f8fafc; color: #0f172a; }}
  header {{ background: #0f172a; color: #f8fafc; padding: 28px 32px; }}
  header h1 {{ margin: 0 0 4px; font-size: 24px; }}
  header p {{ margin: 0; color: #94a3b8; font-size: 14px; }}
  .wrap {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 16px 32px; background: #fff; border: 1px solid #e2e8f0;
          border-radius: 12px; padding: 18px 22px; margin: 20px 0; }}
  .meta-item {{ display: flex; flex-direction: column; }}
  .meta-k {{ font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: #64748b; }}
  .meta-v {{ font-size: 15px; font-weight: 600; }}
  .card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 8px 12px;
          margin: 18px 0; box-shadow: 0 1px 2px rgba(15,23,42,.04); }}
  table.summary {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  table.summary th, table.summary td {{ padding: 8px 10px; text-align: right; border-bottom: 1px solid #e2e8f0; }}
  table.summary th:first-child, table.summary td:first-child,
  table.summary th:nth-child(2), table.summary td:nth-child(2) {{ text-align: left; }}
  table.summary thead th {{ background: #f1f5f9; position: sticky; top: 0; }}
  .note {{ font-size: 13px; color: #475569; line-height: 1.55; background: #fffbeb;
          border: 1px solid #fde68a; border-radius: 12px; padding: 14px 18px; margin: 18px 0; }}
  h2.section {{ font-size: 15px; text-transform: uppercase; letter-spacing: .05em; color: #475569;
              margin: 28px 4px 6px; }}
</style></head>
<body>
<header>
  <h1>{html.escape(meta.title)}</h1>
  <p>Uniswap v4 hook LP returns — replayed against historical v3 swaps</p>
</header>
<div class="wrap">
  <div class="meta">{meta_html}</div>
  <div class="note"><strong>Reading this report.</strong> {_CAVEAT}</div>
  <h2 class="section">Summary</h2>
  <section class="card">{summary_table_html(attribution_df)}</section>
  <h2 class="section">Charts</h2>
  {figs_html}
</div>
</body></html>
"""


def write_report(result: WorldsResult, out_path: Path, meta: ReportMeta) -> Path:
    """Write the self-contained report and return the path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_report_html(result, meta), encoding="utf-8")
    return out_path
