"""HODL-baseline return attribution for a replay world.

Decomposes each world's outcome at the final price into the pieces an LP
actually cares about:

    HODL value      initial deposit amounts, marked at the final price
    LP underlying   the position's token0/token1 at the final price (no fees)
    IL              LP underlying - HODL   (signed; negative = impermanent loss)
    fees            uncollected LP fees, valued at the final price
    gas             modelled rebalance cost (0 for passive worlds)
    LVR             value extracted by the arbitrageur (cum_arb), shown alongside

The headline number is

    net LP PnL = IL + fees - gas

i.e. how the LP did *relative to just holding* the initial basket, plus the
fees they earned, minus gas. LVR is reported separately: it is the value the
pool bled to arbitrage, an upper bound on what a perfectly-LVR-capturing hook
could in principle recover.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import polars as pl

from v4sim.metrics.accounting import usdc_value_of_position
from v4sim.replay.runner import WorldResult, WorldsResult


@dataclass(frozen=True)
class Attribution:
    name: str
    kind: str
    initial_value_usdc: float
    hodl_value_usdc: float
    lp_underlying_usdc: float
    fees_usdc: float
    il_usdc: float  # lp_underlying - hodl (signed; negative under IL)
    gas_usdc: float
    lvr_usdc: float  # cum_arb extracted by the arbitrageur
    net_lp_pnl_usdc: float  # il + fees - gas
    final_lp_wealth_usdc: float  # lp_underlying + fees - gas
    total_return_pct: float  # final wealth vs initial deposit
    hodl_return_pct: float  # HODL vs initial deposit
    rebalances: int

    def as_dict(self) -> dict:
        return asdict(self)


def attribute(world: WorldResult) -> Attribution:
    """Decompose one world's result against the HODL baseline at final price."""
    p = world.final_sqrt_price_x96

    def value(a0: int, a1: int) -> float:
        return usdc_value_of_position(
            a0, a1, p,
            decimals0=world.decimals0,
            decimals1=world.decimals1,
            usdc_is_token0=world.usdc_is_currency0,
        )

    hodl = value(world.initial_lp_amount0_raw, world.initial_lp_amount1_raw)
    lp_underlying = value(world.final_lp_amount0_raw, world.final_lp_amount1_raw)
    fees = world.fees_usdc
    gas = world.gas_usdc
    il = lp_underlying - hodl
    net_pnl = il + fees - gas
    final_wealth = lp_underlying + fees - gas
    initial = world.initial_lp_value_usdc

    return Attribution(
        name=world.name,
        kind=world.kind,
        initial_value_usdc=initial,
        hodl_value_usdc=hodl,
        lp_underlying_usdc=lp_underlying,
        fees_usdc=fees,
        il_usdc=il,
        gas_usdc=gas,
        lvr_usdc=world.cum_arb_extracted_usdc,
        net_lp_pnl_usdc=net_pnl,
        final_lp_wealth_usdc=final_wealth,
        total_return_pct=(final_wealth - initial) / initial * 100.0 if initial else 0.0,
        hodl_return_pct=(hodl - initial) / initial * 100.0 if initial else 0.0,
        rebalances=world.rebalances,
    )


def attribution_table(result: WorldsResult) -> pl.DataFrame:
    """One row of attribution per world, in spec order."""
    rows = [attribute(w).as_dict() for w in result.worlds.values()]
    return pl.DataFrame(rows)


# Columns surfaced in the human-facing summary table, in display order.
SUMMARY_COLUMNS: list[tuple[str, str]] = [
    ("name", "World"),
    ("kind", "Kind"),
    ("initial_value_usdc", "Initial $"),
    ("hodl_value_usdc", "HODL $"),
    ("lp_underlying_usdc", "LP underlying $"),
    ("fees_usdc", "Fees $"),
    ("il_usdc", "IL $"),
    ("gas_usdc", "Gas $"),
    ("net_lp_pnl_usdc", "Net LP PnL $"),
    ("final_lp_wealth_usdc", "Final wealth $"),
    ("total_return_pct", "Return %"),
    ("lvr_usdc", "LVR $"),
    ("rebalances", "Rebalances"),
]
