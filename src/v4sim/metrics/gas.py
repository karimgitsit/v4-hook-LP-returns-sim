"""Gas cost model for active-hook rebalancing.

A deliberately simple, config-driven model: a fixed gas price times a
per-operation gas estimate, valued in USDC at a fixed ETH price. Passive
worlds never rebalance, so they pay $0; an active hook adapter that fires N
rebalances over a run pays ``N * cost_per_rebalance_usdc``.

These are knobs, not measurements — the harness can't observe real mainnet
gas, so we expose the assumptions explicitly and let the report state them.
"""

from __future__ import annotations

from dataclasses import dataclass

# Defaults: 10 gwei (matches configs/*.yaml `gas.fixed_gwei`), a ~250k-gas
# rebalance, and a $3k ETH for USD valuation.
DEFAULT_GAS_PRICE_GWEI = 10.0
DEFAULT_GAS_PER_REBALANCE = 250_000
DEFAULT_ETH_PRICE_USD = 3_000.0


@dataclass(frozen=True)
class GasModel:
    gas_price_gwei: float = DEFAULT_GAS_PRICE_GWEI
    gas_per_rebalance: int = DEFAULT_GAS_PER_REBALANCE
    eth_price_usd: float = DEFAULT_ETH_PRICE_USD

    @property
    def cost_per_rebalance_usdc(self) -> float:
        """USDC cost of one rebalance = gwei * 1e-9 * gas * ETH price."""
        return self.gas_price_gwei * 1e-9 * self.gas_per_rebalance * self.eth_price_usd

    def cost_usdc(self, rebalances: int) -> float:
        """Total gas cost in USDC for ``rebalances`` operations."""
        return self.cost_per_rebalance_usdc * max(rebalances, 0)


#: Default model used when a run doesn't supply one.
DEFAULT_GAS_MODEL = GasModel()
