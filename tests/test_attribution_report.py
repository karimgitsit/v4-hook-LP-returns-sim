"""Step-7a gate: attribution decomposition + self-contained HTML report."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from v4sim.evm.artifacts import ArtifactNotFound
from v4sim.evm.env import bootstrap_v4_eth_usdc
from v4sim.metrics.attribution import attribute, attribution_table
from v4sim.metrics.gas import GasModel
from v4sim.replay.runner import default_baseline_specs, replay_worlds
from v4sim.viz.report import ReportMeta, build_report_html

REPO_ROOT = Path(__file__).resolve().parents[1]
PARQUET = REPO_ROOT / "data" / "cache" / "swaps_88e6a0c2.parquet"


def _require():
    try:
        bootstrap_v4_eth_usdc()
    except ArtifactNotFound as e:
        pytest.skip(str(e))
    if not PARQUET.exists():
        pytest.skip("swap parquet missing")


def _small_result(gas_model: GasModel | None = None):
    df = pl.read_parquet(PARQUET).sort(["ts", "block", "log_index"]).head(400)
    kwargs = {"snapshot_period_s": 600}
    if gas_model is not None:
        kwargs["gas_model"] = gas_model
    return replay_worlds(df, default_baseline_specs(), **kwargs)


def test_attribution_identities_hold():
    _require()
    res = _small_result()
    for w in res.worlds.values():
        a = attribute(w)
        # IL is signed LP-underlying minus HODL.
        assert a.il_usdc == pytest.approx(a.lp_underlying_usdc - a.hodl_value_usdc, rel=1e-9)
        # Net PnL = IL + fees - gas.
        assert a.net_lp_pnl_usdc == pytest.approx(a.il_usdc + a.fees_usdc - a.gas_usdc, rel=1e-9)
        # Final wealth = LP underlying + fees - gas.
        assert a.final_lp_wealth_usdc == pytest.approx(
            a.lp_underlying_usdc + a.fees_usdc - a.gas_usdc, rel=1e-9
        )
        # Fees are non-negative and LVR is positive (arb-to-truth on).
        assert a.fees_usdc >= 0
        assert a.lvr_usdc > 0


def test_passive_worlds_pay_zero_gas():
    _require()
    res = _small_result(GasModel(gas_price_gwei=50))  # non-trivial price
    for w in res.worlds.values():
        assert w.rebalances == 0
        assert w.gas_usdc == 0.0


def test_attribution_table_has_a_row_per_world():
    _require()
    res = _small_result()
    tbl = attribution_table(res)
    assert set(tbl["name"].to_list()) == set(res.worlds)
    assert "net_lp_pnl_usdc" in tbl.columns


def test_report_html_is_self_contained(tmp_path):
    _require()
    res = _small_result()
    meta = ReportMeta(title="test report", n_swaps=400, band_pct=0.10)
    html = build_report_html(res, meta)
    # plotly.js embedded inline (no CDN/script src needed for our charts).
    assert "plotly.js v" in html
    assert "Plotly.newPlot" in html
    # A figure div per world appears (equity/fees/liquidity reference the names),
    # and the summary table is present.
    assert "class='summary'" in html
    assert "test report" in html
    # Writes a real file.
    out = tmp_path / "report.html"
    out.write_text(html, encoding="utf-8")
    assert out.stat().st_size > 1_000_000  # bundle makes it multi-MB
