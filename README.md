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
- [ ] Step 2 — pyrevm bootstrap (PoolManager + PoolSwapTest + PoolModifyLiquidityTest)
- [ ] Step 3 — Single-world runner (vanilla full-range)
- [ ] Step 4 — Arb-to-truth step
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
