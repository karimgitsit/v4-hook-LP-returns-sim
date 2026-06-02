"""Streamlit UI for the v4 hook LP-returns simulator (step 7b).

Wraps the replay runner + report builder in a guided, paste-a-hook flow:

  1. (optional) upload a forge artifact JSON for the hook under test,
  2. pick a date window over the cached swap parquet,
  3. (optional, advanced) upload a HookAdapter subclass,
  4. hit Run — the app replays the baselines (+ the hook) and renders the same
     figures as the standalone report, plus a downloadable report.html.

Run with ``v4sim-app`` or ``streamlit run src/v4sim/app/streamlit_app.py``.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import tempfile
import traceback
from pathlib import Path

import polars as pl
import streamlit as st

from v4sim.evm import hookmine
from v4sim.metrics.attribution import SUMMARY_COLUMNS, attribution_table
from v4sim.metrics.gas import GasModel
from v4sim.replay.runner import (
    DEFAULT_BAND_PCT,
    WorldSpec,
    active_recenter_spec,
    default_baseline_specs,
    noop_hook_spec,
    replay_worlds,
)
from v4sim.strategies.hook_adapter import HookAdapter
from v4sim.viz.attribution import attribution_bar, lvr_bar, returns_bar
from v4sim.viz.equity import equity_figure, fees_figure, lvr_figure, world_colors
from v4sim.viz.liquidity import liquidity_figure
from v4sim.viz.report import ReportMeta, build_report_html

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PARQUET = REPO_ROOT / "data" / "cache" / "swaps_88e6a0c2.parquet"

# Named permission flags offered as checkboxes, in address-bit order.
FLAG_OPTIONS: list[tuple[str, int]] = [
    ("beforeInitialize", hookmine.BEFORE_INITIALIZE_FLAG),
    ("afterInitialize", hookmine.AFTER_INITIALIZE_FLAG),
    ("beforeAddLiquidity", hookmine.BEFORE_ADD_LIQUIDITY_FLAG),
    ("afterAddLiquidity", hookmine.AFTER_ADD_LIQUIDITY_FLAG),
    ("beforeRemoveLiquidity", hookmine.BEFORE_REMOVE_LIQUIDITY_FLAG),
    ("afterRemoveLiquidity", hookmine.AFTER_REMOVE_LIQUIDITY_FLAG),
    ("beforeSwap", hookmine.BEFORE_SWAP_FLAG),
    ("afterSwap", hookmine.AFTER_SWAP_FLAG),
    ("beforeDonate", hookmine.BEFORE_DONATE_FLAG),
    ("afterDonate", hookmine.AFTER_DONATE_FLAG),
    ("beforeSwapReturnsDelta", hookmine.BEFORE_SWAP_RETURNS_DELTA_FLAG),
    ("afterSwapReturnsDelta", hookmine.AFTER_SWAP_RETURNS_DELTA_FLAG),
    ("afterAddLiquidityReturnsDelta", hookmine.AFTER_ADD_LIQUIDITY_RETURNS_DELTA_FLAG),
    ("afterRemoveLiquidityReturnsDelta", hookmine.AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA_FLAG),
]


@st.cache_data(show_spinner=False)
def load_swaps(path: str) -> pl.DataFrame:
    return pl.read_parquet(path).sort(["ts", "block", "log_index"])


def load_adapter_from_bytes(src: bytes, filename: str) -> HookAdapter:
    """Import a user .py file and instantiate its `Adapter` (HookAdapter subclass).

    This executes the uploaded module — only use adapters you trust.
    """
    with tempfile.NamedTemporaryFile("wb", suffix=".py", delete=False) as f:
        f.write(src)
        tmp_path = f.name
    mod_name = f"_v4sim_user_adapter_{abs(hash(filename))}"
    spec = importlib.util.spec_from_file_location(mod_name, tmp_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load adapter module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    adapter_cls = getattr(module, "Adapter", None)
    if adapter_cls is None:
        raise RuntimeError("adapter module must define a class named `Adapter`")
    instance = adapter_cls()
    if not isinstance(instance, HookAdapter):
        raise RuntimeError("`Adapter` must subclass v4sim.strategies.hook_adapter.HookAdapter")
    return instance


def _window_dataframe(
    df: pl.DataFrame, start: dt.datetime, end: dt.datetime
) -> pl.DataFrame:
    lo = int(start.replace(tzinfo=dt.UTC).timestamp())
    hi = int(end.replace(tzinfo=dt.UTC).timestamp())
    return df.filter((pl.col("ts") >= lo) & (pl.col("ts") <= hi))


def _sidebar_config() -> dict:
    st.sidebar.header("Configuration")

    band_pct = st.sidebar.slider(
        "Concentrated band (±%)", min_value=1.0, max_value=50.0, value=DEFAULT_BAND_PCT * 100,
        step=1.0,
        help="Half-width of the concentrated baseline's LP range.",
    ) / 100.0
    lp_notional = st.sidebar.number_input(
        "LP notional (USDC)", min_value=10_000.0, value=1_000_000.0, step=100_000.0,
        help="Each world is sized to exactly this USDC value at t0.",
    )
    arb_to_truth = st.sidebar.toggle(
        "Arb-to-truth", value=True,
        help="After each swap, a perfect arb snaps the pool back to the v3 price "
        "(books LVR). Turn off for drift mode.",
    )
    snapshot_period_min = st.sidebar.select_slider(
        "Snapshot period (min)", options=[5, 10, 15, 30, 60, 120], value=60,
    )

    st.sidebar.subheader("Gas model")
    gas_gwei = st.sidebar.number_input("Gas price (gwei)", min_value=0.0, value=10.0, step=1.0)
    gas_per_rebalance = st.sidebar.number_input(
        "Gas per rebalance", min_value=0, value=250_000, step=10_000
    )
    eth_price = st.sidebar.number_input("ETH price (USD)", min_value=1.0, value=3_000.0, step=100.0)

    return {
        "band_pct": band_pct,
        "lp_notional": lp_notional,
        "arb_to_truth": arb_to_truth,
        "snapshot_period_s": int(snapshot_period_min) * 60,
        "gas_model": GasModel(
            gas_price_gwei=gas_gwei, gas_per_rebalance=int(gas_per_rebalance),
            eth_price_usd=eth_price,
        ),
    }


def _hook_section() -> dict:
    st.subheader("1 · Hook under test")
    mode = st.radio(
        "What do you want to simulate?",
        ["Baselines only", "Demo no-op hook (MockHooks)", "Upload a forge artifact"],
        horizontal=True,
    )

    with st.expander("How do I get a forge artifact JSON?"):
        st.markdown(
            "1. In your hook repo, build with the same compiler the harness uses:\n"
            "   ```bash\n   forge build\n   ```\n"
            "2. Find the artifact at `out/MyHook.sol/MyHook.json`.\n"
            "3. Upload that JSON below and tick the permission flags your hook "
            "declares in `getHookPermissions()` (these are encoded in the low 14 "
            "bits of the deployed address; the harness mines a matching salt).",
        )

    artifact = None
    flags = 0
    if mode == "Upload a forge artifact":
        up = st.file_uploader("Hook artifact JSON", type=["json"])
        if up is not None:
            try:
                artifact = json.loads(up.getvalue())
                _ = artifact["bytecode"]["object"]  # validate shape early
                st.success(f"Loaded artifact: {up.name}")
            except Exception as e:  # noqa: BLE001
                st.error(f"Could not parse artifact JSON: {e}")
                artifact = None
        st.markdown("**Hook permission flags**")
        cols = st.columns(2)
        for i, (label, bit) in enumerate(FLAG_OPTIONS):
            if cols[i % 2].checkbox(label, key=f"flag_{label}"):
                flags |= bit
        st.caption(f"Encoded flags: 0x{flags:04x}")

    return {"mode": mode, "artifact": artifact, "flags": flags}


def _adapter_section() -> HookAdapter | None:
    with st.expander("2 · (Advanced) Custom adapter"):
        st.markdown(
            "If your hook uses non-standard verbs (e.g. `depositLeft` / "
            "`rebalance`), upload a Python file defining a class named `Adapter` "
            "that subclasses `HookAdapter`. **The file is executed — only upload "
            "code you trust.**"
        )
        up = st.file_uploader("Adapter .py", type=["py"], key="adapter_upload")
        if up is not None:
            try:
                adapter = load_adapter_from_bytes(up.getvalue(), up.name)
                st.success(f"Loaded adapter `{type(adapter).__name__}` from {up.name}")
                return adapter
            except Exception as e:  # noqa: BLE001
                st.error(f"Adapter load failed: {e}")
    return None


def _active_section() -> dict:
    with st.expander("2b · Auto-recentering active world (no hook needed)"):
        st.markdown(
            "Add a built-in **active** strategy that re-centres a concentrated "
            "band on the price as it drifts — collecting fees and paying gas at "
            "each move. A ready-made example of the active `rebalance()` seam, "
            "useful as a comparison point for your hook."
        )
        enabled = st.checkbox("Add an auto-recentering active world")
        recenter_pct = st.slider(
            "Re-centre when price drifts (±%)", min_value=0.5, max_value=20.0, value=5.0,
            step=0.5, disabled=not enabled,
        ) / 100.0
    return {"enabled": enabled, "recenter_pct": recenter_pct}


def _build_specs(
    cfg: dict, hook: dict, adapter: HookAdapter | None, active: dict
) -> list[WorldSpec]:
    specs = default_baseline_specs(band_pct=cfg["band_pct"])
    if active["enabled"]:
        specs.append(
            active_recenter_spec(band_pct=cfg["band_pct"], recenter_pct=active["recenter_pct"])
        )
    if hook["mode"] == "Demo no-op hook (MockHooks)":
        specs.append(noop_hook_spec(band_pct=cfg["band_pct"]))
    elif hook["mode"] == "Upload a forge artifact" and hook["artifact"] is not None:
        if hook["flags"] == 0:
            st.warning("No permission flags selected — a hook with zero flags is rejected "
                       "by PoolManager.initialize. Select at least one flag.")
        specs.append(
            WorldSpec(
                name="hook", kind="hook", band_pct=cfg["band_pct"],
                hook_artifact=hook["artifact"], hook_flags=hook["flags"],
                adapter=adapter,
            )
        )
    return specs


def _render_results(result, cfg: dict, df: pl.DataFrame) -> None:
    snaps = result.snapshots
    colors = world_colors(list(snaps["world"].unique(maintain_order=True)))
    attribution_df = attribution_table(result)

    st.subheader("Summary")
    display_cols = [c for c, _ in SUMMARY_COLUMNS if c in attribution_df.columns]
    rename = {c: label for c, label in SUMMARY_COLUMNS}
    st.dataframe(
        attribution_df.select(display_cols).rename(rename).to_pandas(),
        width="stretch", hide_index=True,
    )

    st.subheader("Charts")
    st.plotly_chart(equity_figure(snaps, colors=colors), width="stretch")
    c1, c2 = st.columns(2)
    c1.plotly_chart(fees_figure(snaps, colors=colors), width="stretch")
    c2.plotly_chart(attribution_bar(attribution_df), width="stretch")
    c3, c4 = st.columns(2)
    c3.plotly_chart(returns_bar(attribution_df), width="stretch")
    c4.plotly_chart(lvr_bar(attribution_df), width="stretch")
    st.plotly_chart(lvr_figure(snaps, colors=colors), width="stretch")
    for w in result.worlds.values():
        if w.kind in ("concentrated", "hook"):
            st.plotly_chart(liquidity_figure(snaps, w), width="stretch")

    # Downloadable standalone report.
    gm = cfg["gas_model"]
    meta = ReportMeta(
        title="v4 hook LP returns",
        window_start=int(df["ts"].min()),
        window_end=int(df["ts"].max()),
        n_swaps=df.height,
        band_pct=cfg["band_pct"],
        arb_to_truth=cfg["arb_to_truth"],
        gas_note=(
            f"{gm.gas_price_gwei:g} gwei × {gm.gas_per_rebalance:,} gas "
            f"@ ${gm.eth_price_usd:,.0f}/ETH = ${gm.cost_per_rebalance_usdc:,.2f}/rebalance"
        ),
        generated_at=dt.datetime.now(tz=dt.UTC),
    )
    html = build_report_html(result, meta)
    st.download_button(
        "⬇ Download standalone report.html", data=html.encode("utf-8"),
        file_name="report.html", mime="text/html",
    )


def main() -> None:
    st.set_page_config(page_title="v4 hook LP returns", layout="wide")
    st.title("Uniswap v4 hook — LP returns simulator")
    st.caption(
        "Replay historical ETH/USDC swaps against vanilla LP baselines and your "
        "hook, then decompose the LP outcome into fees, IL, gas and LVR."
    )

    cfg = _sidebar_config()

    # Data source + window.
    parquet_path = str(DEFAULT_PARQUET)
    if not Path(parquet_path).exists():
        st.error(
            f"Cached swap parquet not found at `{parquet_path}`.\n\n"
            "Pull it first:\n```bash\nexport GRAPH_API_KEY=...\n"
            "v4sim-pull-swaps --days 7\n```"
        )
        st.stop()

    df_all = load_swaps(parquet_path)
    min_ts, max_ts = int(df_all["ts"].min()), int(df_all["ts"].max())
    min_d = dt.datetime.fromtimestamp(min_ts, tz=dt.UTC).date()
    max_d = dt.datetime.fromtimestamp(max_ts, tz=dt.UTC).date()

    hook = _hook_section()
    adapter = _adapter_section()
    active = _active_section()

    st.subheader("3 · Date window")
    # Default to the last day for a fast first run.
    default_start = max(min_d, max_d - dt.timedelta(days=1))
    picked = st.date_input(
        "Replay window (UTC)", value=(default_start, max_d),
        min_value=min_d, max_value=max_d,
        help="A 1–2 day window finishes in a few seconds; a full week is ~25s.",
    )
    if not (isinstance(picked, tuple) and len(picked) == 2):
        st.info("Pick a start and end date.")
        st.stop()
    start_d, end_d = picked
    start_dt = dt.datetime.combine(start_d, dt.time.min)
    end_dt = dt.datetime.combine(end_d, dt.time.max)
    df = _window_dataframe(df_all, start_dt, end_dt)
    st.caption(f"{df.height:,} swaps in window (of {df_all.height:,} cached).")

    if st.button("▶ Run simulation", type="primary"):
        if df.height < 2:
            st.error("Need at least 2 swaps in the window — widen the date range.")
            st.stop()
        specs = _build_specs(cfg, hook, adapter, active)
        try:
            with st.spinner(f"Replaying {df.height:,} swaps across {len(specs)} worlds…"):
                result = replay_worlds(
                    df, specs,
                    lp_notional_usdc=cfg["lp_notional"],
                    snapshot_period_s=cfg["snapshot_period_s"],
                    arb_to_truth=cfg["arb_to_truth"],
                    gas_model=cfg["gas_model"],
                )
        except Exception as e:  # noqa: BLE001
            st.error(f"Replay failed: {e}")
            st.code(traceback.format_exc())
            st.stop()
        st.success("Done.")
        _render_results(result, cfg, df)


if __name__ == "__main__":
    main()
