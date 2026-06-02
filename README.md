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
- [x] **Step 7** — Fee attribution + visual report + Streamlit UI: LP fee tracking from pool storage, HODL-baseline decomposition, self-contained `report.html`, and a guided paste-a-hook web app
- [x] **Step 8** — Active rebalancing end-to-end: a shared mutable `PositionState`, an auto-recentering adapter that withdraws + re-places liquidity on price drift, gas firing per rebalance, and a moving-band liquidity chart
- [x] **Step 9** — A *real published* hook plugged in end-to-end: OpenZeppelin's `AntiSandwichHook`, vendored and compiled against our v4-core, deployed at a mined flag-valid address, run as a world against the baselines — plus per-swap block-number advancement so the hook's per-block slot window is real

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
│   ├── evm/                   # pyrevm wiring (step 2+); state.py = fee-growth reads
│   ├── replay/                # swap-by-swap runner (step 3+)
│   ├── strategies/            # full_range, concentrated, hook_adapter, active_rebalance, placement
│   ├── metrics/               # LP value, fees, IL, gas, attribution
│   ├── viz/                   # equity, attribution, liquidity, report (HTML)
│   ├── app/                   # Streamlit UI (step 7b)
│   └── report.py              # v4sim-report CLI
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
expect when your LP is undersized for the swap volume. Fee income (step 7)
is inflated by the same factor, so read the **relative** comparison between
worlds — not the absolute dollars — as the signal.

### Fee attribution + report (step 7a)

LP fee income is read straight from PoolManager storage — a Python port of
v4-core's `StateLibrary` (`evm/state.py`) that computes `feeGrowthInside`
from the global accumulator and the boundary ticks' `feeGrowthOutside`, then
`fees = liquidity * (insideNow − insideLast) >> 128`. No state perturbation,
and it agrees to the wei with a `modifyLiquidity(0)` poke.

Each world is decomposed against a **HODL baseline** at the final price
(`metrics/attribution.py`):

- **HODL** — the initial deposit basket marked at the final price
- **LP underlying** — the position's token0/token1 at the final price (no fees)
- **IL** = LP underlying − HODL (signed; negative under impermanent loss)
- **Fees** — uncollected LP fees, valued at the final price
- **Gas** — modelled rebalance cost (`metrics/gas.py`: gwei × gas × ETH price
  per rebalance; passive worlds pay $0)
- **LVR** — `cum_arb_extracted_usdc`, reported alongside

with **net LP PnL = IL + fees − gas** as the headline (LVR shown separately as
the value bled to arbitrage — the ceiling an LVR-capturing hook could reclaim).

`v4sim-report` runs the replay and writes a single self-contained
`report.html` (plotly.js inlined, opens offline) with equity / fee / liquidity
charts and a summary table:

```bash
v4sim-report --days 2 --out data/cache/report.html          # baselines only
v4sim-report --days 2 --demo-hook                           # + transparent hook
v4sim-report --hook out/MyHook.sol/MyHook.json --hook-flags 0x40
```

### Active rebalancing (step 8)

The tick-keeper seam from step 6 is now live. An *active* adapter overrides
`HookAdapter.rebalance(env, key, position, truth_price)` to actually move
liquidity; the runner shares a mutable `PositionState` with it (single source
of truth for "where is this world's liquidity now"), books the fees it
realizes, and charges gas per rebalance via the step-7 gas model.

The bundled example, `AutoRecenterAdapter`
(`strategies/active_rebalance.py`), re-centres a ±`band_pct` position whenever
price drifts `recenter_pct` from the band centre: it reads and realizes the
position's fees, withdraws it, and re-places the *same value* in a fresh band
around the current price (value-conserving, so only gas is a cost). Placement
math is shared with the runner's initial build (`strategies/placement.py`).

```bash
v4sim-replay --active --recenter-pct 0.02      # add an active world to the run
v4sim-report --days 3 --active --recenter-pct 0.02
```

This makes the LP tradeoff legible: re-centred liquidity stays near the price
so it **earns more fees** and bleeds **less to arbitrage**, but it **realizes
IL** at each re-centre (selling the falling asset, buying the rising one) and
**pays gas**. The decomposition and the moving-band liquidity chart show all
four moving at once. A real hook with the same intent subclasses `HookAdapter`
identically and calls its own `rebalance()` instead of the harness's
`modify_liquidity`. With a large `recenter_pct` (never triggers) the active
world collapses exactly onto the passive concentrated baseline.

### Real published hook: AntiSandwichHook (step 9)

Steps 1–8 build the machine; step 9 proves it on a real, third-party hook —
OpenZeppelin's audited [`AntiSandwichHook`](https://github.com/OpenZeppelin/uniswap-hooks)
(the umbra-research sandwich-resistant AMM design). It is **vendored verbatim**
under `contracts/hooks/` (see `contracts/hooks/NOTICE` for provenance + MIT
attribution) and compiled by `scripts/build_contracts.sh` *against our own
v4-core submodule*, so the deployed bytecode matches the PoolManager the harness
runs. No code in the hook is modified; we only add a thin deployable wrapper
(`AntiSandwichHookHarness.sol`, OpenZeppelin's own `AntiSandwichMock` renamed).

```bash
v4sim-replay --antisandwich --limit 3000     # add the real hook as a world
v4sim-report --days 2 --antisandwich         # full report incl. the hook
```

What the hook does: for `!zeroForOne` swaps it pins execution to the
beginning-of-block price (so a back-run can't profit from an in-block price
move) and **donates the surplus to in-range LPs**. The world is placed
**full-range** so the sole simulated LP is always in range to receive those
donations (otherwise `donate` reverts with `NoLiquidityToReceiveDonation`).

Two integration pieces made this work, both reusable by any future hook:

1. **Block-number advancement.** The replay now sets each world's
   `block.number`/`timestamp` from the swap's real on-chain block
   (`V4Env.set_block`). Block-aware hooks (anti-sandwich, JIT penalties, TWAP)
   are no-ops without it; with it, the user swap and its back-running arb share
   a block exactly as they did on-chain. Harmless for hookless worlds.
2. **Constructor manager injection.** Any `BaseHook` takes the PoolManager as
   its first ctor arg, but that address is only known once the per-world env is
   bootstrapped. `WorldSpec(hook_ctor_manager=True)` makes the runner
   ABI-encode `env.manager` and prepend it to the hook's constructor args.

The result needs **no new attribution line**: the hook's benefit flows straight
through the existing metrics. Donations raise the LP's fee growth (the **Fees**
line jumps), and constrained arbs extract far less (**LVR** collapses, even
going negative — arbing becomes unprofitable, exactly as OZ's docs warn). On a
3 000-swap window the anti-sandwich world's LVR fell from **+$9.7M**
(full-range baseline) to **−$0.8M**, with the recaptured value reappearing as
LP fees.

> ⚠️ **Read this as a relative result.** The magnitude is inflated by two
> things stacked together: the [undersized pool](#why-is-cum_arb_extracted_usdc-so-large)
> (already makes LVR/fees huge), and the modelling choice that the back-running
> arb shares the price-moving swap's block. In reality a front-run is small and
> the arb captures only a sliver; here the arb tries to undo the *entire* user
> swap within the same block, so the anti-sandwich rule attributes the full
> in-block price move to it. The hook's *direction* (kills LVR, returns it to
> LPs) is faithful; the absolute dollars are not a forecast.

### Streamlit app (step 7b)

A guided web UI wraps the runner + report. Install the `ui` extra and launch:

```bash
pip install -e ".[ui]"
v4sim-app            # == streamlit run src/v4sim/app/streamlit_app.py
```

The app walks you through `forge build` → upload `out/MyHook.json`, ticking
the permission flags your hook declares; pick a date window over the cached
parquet (defaults to the last day for a fast run — a full week is ~25s);
optionally upload a `HookAdapter` subclass for non-standard verbs; then **Run**
to see the same charts inline plus a downloadable `report.html`.
