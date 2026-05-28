# v4-hook-LP-returns-sim

Generic backtesting / simulation harness for Uniswap v4 **hooks**, focused on
LP outcomes.

We replay historical Uniswap v3 swaps against a candidate v4 pool (vanilla
or hook-equipped), then run a perfect arbitrageur after each swap to push
the v4 pool's `sqrtPrice` back to the v3 post-swap price. The arb's PnL is
booked as **value extracted from LPs** — a conservative lower bound on
hook performance.

Each run replays N "worlds" in parallel against the same swap stream:

- **Vanilla full-range** v4 pool
- **Vanilla concentrated** ±10% baseline
- **Hook'd pool** under test (e.g. directional-liquidity)

…and emits equity curves, attribution (fees vs IL vs arb vs gas), and —
for the hook world — a liquidity-placement-over-time view.

## Stack

- Python 3.11+
- [pyrevm](https://github.com/paradigmxyz/pyrevm) — real EVM bytecode
  execution, no Python reimplementation of the AMM math
- [polars](https://pola.rs) for swap data
- [plotly](https://plotly.com/python/) for viz
- The Graph (Uniswap v3 subgraph) for the swap stream
- Foundry artifacts from `v4-core` + `v4-periphery`, vendored as git
  submodules under `contracts/`

## Status

Built step-by-step as a vertical slice. Current step:

- [x] **Step 1** — Subgraph puller for ETH/USDC 5bps, 1 week of swaps → Parquet
- [x] **Step 2** — pyrevm bootstrap: PoolManager + PoolSwapTest + PoolModifyLiquidityTest, hello-world swap
- [x] **Step 3** — Single-world full-range runner: replay 1 week, emit hourly equity snapshots
- [x] **Step 4** — Arb-to-truth: pool tracks v3 √P within 1 tick after each swap; arb PnL booked as value extracted from LPs
- [x] **Step 5** — Concentrated ±10% baseline as a second parallel world; multi-world replay engine
- [x] **Step 6** — Generic hook world: CREATE2 HookMiner, load any hook from a forge artifact JSON, adapter seam + tick-keeper call site
- [ ] Step 7 — Visual report + Streamlit UI (3-line equity, attribution, liquidity placement; paste-a-hook flow)

### Where this is going

This is a **general** simulator for any LP-related v4 hook, not a one-off
for a single hook. The intended end-to-end flow:

1. User compiles their hook with `forge build` and grabs the artifact JSON
   (`out/MyHook.sol/MyHook.json`). A guided UI flow walks them through this.
2. User pastes/uploads the artifact, picks a date range, and (if the hook's
   deposit/withdraw verbs are non-standard) supplies a ~30-line Python
   adapter.
3. The tool simulates the hook against the vanilla baselines over that
   window and renders graphs + tables (Streamlit front-end, self-contained
   HTML export).

Decisions locked for that flow: **forge artifact JSON** as the hook input
format (step 6), **Streamlit** for the UI (step 7b).

## Quickstart

```bash
# install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# pull 1 week of swaps for ETH/USDC 5bps into data/cache/
export GRAPH_API_KEY=<your-decentralized-network-api-key>
v4sim-pull-swaps --days 7

# inspect
python -c "import polars as pl; \
  df = pl.read_parquet('data/cache/swaps_eth_usdc_5bps.parquet'); \
  print(df.shape); print(df.head())"
```

## Bootstrapping the EVM harness (step 2)

The pyrevm harness deploys real v4-core bytecode, so the foundry artifacts
have to exist locally. They're gitignored — build them once after cloning:

```bash
# 1. pull the submodules
git submodule update --init --recursive

# 2. install foundry (forge); see https://book.getfoundry.sh/getting-started/installation
# 3. install solc 0.8.26 (svm-rs or a manual binary)

# 4. compile v4-core into out/
./scripts/build_contracts.sh
```

`scripts/build_contracts.sh` uses the `debug` foundry profile (no via_ir,
`optimizer_runs = 200`) so it finishes in seconds — we don't need a
gas-optimized binary for simulation.

The harness loads JSON artifacts from `contracts/v4-core/out/`. The relevant
tests skip with a clear message if those are missing.

### Getting a Graph API key

The hosted service is deprecated; queries go through the decentralized
network gateway and require an API key. Create one at
<https://thegraph.com/studio/apikeys/> and export it as `GRAPH_API_KEY`.

Free-tier monthly query budgets are plenty for the volumes we pull here
(~50–200k swaps/week for the 5bps pool).

## Repo layout

```
v4-hook-LP-returns-sim/
├── pyproject.toml
├── README.md
├── .gitignore
├── contracts/                 # git submodules: v4-core, v4-periphery
├── src/v4sim/
│   ├── data/
│   │   ├── subgraph.py        # paginated GraphQL → Parquet cache
│   │   └── schema.py          # swap record polars schema
│   ├── evm/                   # pyrevm wiring (step 2+)
│   ├── replay/                # swap-by-swap runner (step 3+)
│   ├── strategies/            # full_range, concentrated, hook_adapter
│   ├── metrics/               # LP value, fees, IL, gas, arb extracted
│   └── viz/                   # equity, attribution, liquidity
├── adapters/                  # hook-specific harness adapters
├── configs/
│   └── eth_usdc_5bps_2024.yaml
└── examples/
```

## Defaults

Driven from `configs/*.yaml`, with sensible defaults baked in:

| Knob                              | Default                                   |
| --------------------------------- | ----------------------------------------- |
| Pool                              | ETH/USDC, v3 source fee 5bps              |
| v4 hook pool fee                  | 5bps (configurable)                       |
| Date range                        | 1 year ending most-recent-full-month      |
| LP notional                       | $1M-equivalent, 50/50 at t=0 price        |
| Concentrated baseline bandwidth   | ±10%                                      |
| Gas price                         | single fixed value per run                |
| Arb                               | perfect (zero gas, zero spread)           |
| MEV / reorgs                      | not modelled in v1                        |

### Replay design choices (step 3)

A handful of decisions ride with every replay; calling them out so they're
discoverable instead of buried in code:

- **Token decimals match real-world** — the harness deploys MockERC20s with
  USDC=6 dec and WETH=18 dec, so swap amounts from the v3 parquet drop in
  unscaled (modulo decimal-to-raw conversion). Catches sign/scale bugs early.
- **Snapshot cadence: hourly** — equity is recorded once per wall-clock hour
  by default. Per-swap snapshots (~28k/week for the 5bps pool) are kept only
  in memory if needed for debugging.
- **Initial price from row 0** — the pool is initialized at row 0's
  `sqrt_price_x96_post`, and the replay loop starts at row 1. Avoids an
  extra subgraph lookback for the prior block's price.

### Arb-to-truth (step 4)

After each replayed swap, a perfect arbitrageur pushes the v4 pool's
sqrtPrice back to the v3 source's post-swap sqrtPrice (the "truth").
Mechanism: a swap call with a huge `amountSpecified` and
`sqrtPriceLimitX96 = target`, so the v4 pool's own swap loop terminates
exactly at the target tick — no Python-side bisection needed.

The arb's signed token delta is valued at the truth price and accumulated
as `cum_arb_extracted_usdc` on each snapshot. Positive = value extracted
from the LP (i.e. LVR). Negative would mean the pool's fee dwarfed the
arb opportunity (unusual in our undersized pool).

Defaults to on. `--no-arb` toggles step-3-style drift mode for
diagnostics.

### Worlds (step 5)

Each run replays the same swap stream against N independent "worlds", one
fresh `PoolManager` per world so a swap in one can't consume liquidity
another was meant to earn on. The two vanilla baselines:

- `full_range` — $1M LP across the whole tick range
- `concentrated` — $1M LP in a ±10% band around the initial price
  (configurable via `--band-pct`); passive, never rebalances

Both are sized to **exactly** $1M of value at t=0 (liquidity is rescaled
post-hoc), so the comparison is fair regardless of band width. Snapshots
are long-form — one row per `(ts, world)` — so adding the hook world is
just another entry in the spec list.

Note: thinner liquidity bleeds more to the arb per swap (LVR ∝ 1/L), so
the full-range world shows much larger `cum_arb_extracted_usdc` than the
concentrated one. Fee income — which is what compensates the concentrated
LP for that — is tracked in step 7's attribution; until then the two
equity curves look similar while price stays inside the band.

### Replay CLI

```bash
# whole parquet (one week of swaps for the 5bps pool), both baselines
v4sim-replay

# pick a window
v4sim-replay --days 2                       # last 2 days
v4sim-replay --start 2026-05-20 --end 2026-05-22

# tune the concentrated band, or run drift diagnostics
v4sim-replay --band-pct 0.05
v4sim-replay --limit 1000 --no-arb          # step-3 drift mode

# add a hook world (step 6)
v4sim-replay --demo-hook                    # transparent no-op hook (MockHooks)
v4sim-replay --hook out/MyHook.sol/MyHook.json --hook-flags 0x40  # afterSwap
```

The output parquet (`data/cache/equity_worlds.parquet` by default) holds
one row per snapshot per world: ts, world, tick, LP underlying amounts, LP
value in USDC at the pool's current price, and cumulative arb extraction.

### Hooks (step 6)

A hook world deploys a v4 hook and attaches it to the pool. Because v4
encodes a hook's permissions in the **low 14 bits of its address**, the
harness includes a Python HookMiner (`evm/hookmine.py`): it deploys a tiny
CREATE2 factory, brute-forces a salt until `address & 0x3FFF == flags`, then
deploys the hook there (running its constructor, so stateful hooks work too).

To plug in your own hook:

1. `forge build` it in your repo and grab `out/MyHook.sol/MyHook.json`.
2. Pass `--hook <path> --hook-flags <bits>` (the bits must match the hook's
   declared `getHookPermissions`, e.g. `0x40` for `afterSwap`).
3. If the hook's LP verbs differ from the standard
   `deposit/withdraw/rebalance`, subclass `HookAdapter`
   (`strategies/hook_adapter.py`) in ~30 lines under `adapters/`.

The tick keeper calls `adapter.rebalance(...)` after each swap+arb at the
truth price; the base (passive) adapter is a no-op, so today a hook world
holds a vanilla position and the active deposit/withdraw/rebalance paths
fill in once a real hook (directional-liquidity) lands.

`--demo-hook` attaches v4-core's `MockHooks` (returns correct selectors,
zero deltas) with only the `afterInitialize` flag — provably transparent, so
the hook world's equity matches the concentrated baseline exactly.

#### Why is `cum_arb_extracted_usdc` so large?

Our default LP is $1M-equivalent against a real-world pool with $100M+
liquidity. Same-size swaps move our small pool ~100× more, and the arb
captures the full impact each time. A week-long run on the 5bps ETH/USDC
parquet currently shows ~$200M cumulative extraction (i.e. catastrophic
LVR) on a stable $1M LP equity — exactly the LVR-vs-fees story you'd
expect when your LP is undersized for the swap volume.
