"""An auto-recentering active LP adapter.

Models the simplest worthwhile active hook: a concentrated position that, when
the price drifts past a threshold from its band centre, withdraws and re-places
a fresh ±band position centred on the current (truth) price. Fees collected at
each withdrawal are reported so the runner books them; the band move costs gas.

This drives every seam the earlier steps built — the tick-keeper call site, the
gas model, the mutable :class:`PositionState` — without needing an external
hook repo. A real hook with the same intent (re-centering liquidity) would
subclass :class:`~v4sim.strategies.hook_adapter.HookAdapter` the same way and
call its own ``rebalance()`` instead of the harness's ``modify_liquidity``.
"""

from __future__ import annotations

import logging
import math

from v4sim.evm.env import V4Env
from v4sim.evm.pool import PoolKey, modify_liquidity, read_slot0
from v4sim.evm.state import uncollected_fees_raw
from v4sim.metrics.accounting import amounts_for_liquidity, usdc_value_of_position
from v4sim.strategies.hook_adapter import HookAdapter, PositionState, RebalanceResult
from v4sim.strategies.placement import place_position

log = logging.getLogger(__name__)

_LOG_1_0001 = math.log(1.0001)


def _pct_to_ticks(pct: float) -> int:
    """Convert a fractional price move to a tick distance (1.0001^tick = ratio)."""
    return max(1, round(math.log(1.0 + pct) / _LOG_1_0001))


class AutoRecenterAdapter(HookAdapter):
    """Re-centre a ±``band_pct`` position when price drifts ``recenter_pct`` from centre.

    The harness places the initial position (so this is a drop-in over a
    concentrated world); from then on the adapter manages the band.
    """

    def __init__(
        self, *, band_pct: float = 0.05, recenter_pct: float = 0.03, tick_spacing: int = 10
    ) -> None:
        self.band_pct = band_pct
        self.recenter_pct = recenter_pct
        self.tick_spacing = tick_spacing
        self._trigger_ticks = _pct_to_ticks(recenter_pct)

    def rebalance(
        self, env: V4Env, key: PoolKey, position: PositionState, truth_sqrt_price_x96: int
    ) -> RebalanceResult | None:
        slot0 = read_slot0(env, key)
        cur_tick = slot0.tick
        cur_sqrt = slot0.sqrt_price_x96
        centre = (position.tick_lower + position.tick_upper) // 2
        if abs(cur_tick - centre) < self._trigger_ticks:
            return None  # still well inside the band — nothing to do

        usdc_is_currency0 = env.decimals0 == 6

        def value(a0: int, a1: int) -> float:
            return usdc_value_of_position(
                a0, a1, cur_sqrt,
                decimals0=env.decimals0, decimals1=env.decimals1,
                usdc_is_token0=usdc_is_currency0,
            )

        # Realize fees and the principal value before tearing the position down.
        fees0, fees1 = uncollected_fees_raw(
            env, key, owner=env.modify_liquidity_router,
            tick_lower=position.tick_lower, tick_upper=position.tick_upper,
            current_tick=cur_tick, salt=position.salt,
        )
        realized_fees_usdc = value(fees0, fees1)
        amt0, amt1 = amounts_for_liquidity(
            cur_sqrt, position.sqrt_a_x96, position.sqrt_b_x96, position.liquidity
        )
        principal_usdc = value(amt0, amt1)

        # Withdraw the old position, then re-place the principal centred on price.
        modify_liquidity(
            env, key, tick_lower=position.tick_lower, tick_upper=position.tick_upper,
            liquidity_delta=-position.liquidity, salt=position.salt,
        )
        new = place_position(
            env, key,
            center_tick=cur_tick, center_sqrt_x96=cur_sqrt,
            tick_spacing=self.tick_spacing, band_pct=self.band_pct,
            target_usdc=principal_usdc, usdc_is_currency0=usdc_is_currency0,
            salt=position.salt,
        )
        # Mutate the shared position in place so the runner snapshots the new band.
        position.tick_lower = new.tick_lower
        position.tick_upper = new.tick_upper
        position.sqrt_a_x96 = new.sqrt_a_x96
        position.sqrt_b_x96 = new.sqrt_b_x96
        position.liquidity = new.liquidity
        log.debug(
            "recentre @tick %d: band [%d,%d] -> [%d,%d], realized fees $%.2f",
            cur_tick, centre - (position.tick_upper - position.tick_lower) // 2,
            centre, new.tick_lower, new.tick_upper, realized_fees_usdc,
        )
        return RebalanceResult(realized_fees_usdc=realized_fees_usdc)
