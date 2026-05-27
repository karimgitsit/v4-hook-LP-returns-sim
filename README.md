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
- [ ] Step 4 — Arb-to-truth step
- [ ] Step 4 — Arb-to-truth step (without this, the pool drifts and equity is at *pool* price, not market — see "Step 3 caveat" below)
- [ ] Step 5 — Concentrated baseline (second parallel world)
- [ ] Step 6 — Hook world + adapter
- [ ] Step 7 — Equity + attribution plots

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

#### Step 3 caveat

Step 3 has no arb-to-truth, so each swap pushes the v4 pool further from
the v3 source price (our pool's liquidity is much smaller than the
mainnet 5bps pool, so impact compounds). The hourly snapshots are
*correct* in that they reflect the pool's actual state after each swap,
but `lp_value_usdc` is denominated in the *pool's* internal price — not
market. Step 4 will fix this by arbing the pool back to v3's post-swap
price between each step, at which point equity is a clean LP-perf
number.

Try it: `v4sim-replay-fullrange --limit 1000` writes
`data/cache/equity_fullrange.parquet`.
