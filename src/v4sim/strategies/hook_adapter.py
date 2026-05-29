"""Hook adapter base class — the seam between an arbitrary v4 hook and the
harness's standard LP verbs.

The harness assumes a hook exposes:

    deposit(uint256 amount0Desired, uint256 amount1Desired) -> uint256 shares
    withdraw(uint256 shares) -> (uint256 amount0, uint256 amount1)
    rebalance()

A hook that already speaks that interface needs no adapter. A hook with
different verbs (e.g. directional-liquidity's depositRight / depositLeft /
depositBoth) ships a ~30-line subclass under ``adapters/<hookname>.py`` that
maps its ABI onto these methods.

The passive (no-op) adapter places its position like a vanilla LP and never
rebalances. An *active* adapter (see ``strategies.active_rebalance``) overrides
:meth:`HookAdapter.rebalance` to move liquidity — withdrawing and re-placing
the position — and reports the fees it realized so the runner can book them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from v4sim.evm.env import V4Env
from v4sim.evm.pool import PoolKey


@dataclass
class PositionState:
    """A single LP position the runner snapshots and an adapter may mutate.

    Holds both the tick range and its sqrt-price bounds (cached so the
    off-chain value/fee math never recomputes them). An active adapter mutates
    this *in place* when it rebalances; the runner reads it for every snapshot,
    so there is one source of truth for "where is this world's liquidity now".
    """

    tick_lower: int
    tick_upper: int
    sqrt_a_x96: int
    sqrt_b_x96: int
    liquidity: int
    salt: bytes = field(default=b"\x00" * 32)


@dataclass(frozen=True)
class RebalanceResult:
    """What an adapter did on one tick-keeper call.

    ``realized_fees_usdc`` is the USDC value of fees the adapter collected (and
    removed from the position) during the rebalance — the runner accumulates it
    so total fees = realized + still-uncollected.
    """

    realized_fees_usdc: float = 0.0


class HookAdapter:
    """Default adapter: passive. Subclass and override for active hooks."""

    #: Set on subclasses that drive deposits/rebalances through the hook itself
    #: rather than letting the harness place a vanilla position.
    manages_own_liquidity: bool = False

    def rebalance(
        self, env: V4Env, key: PoolKey, position: PositionState, truth_sqrt_price_x96: int
    ) -> RebalanceResult | None:
        """Tick keeper. Called after each swap+arb at the (post-arb) truth price.

        Return a :class:`RebalanceResult` if a rebalance was performed (the
        runner counts it and books any realized fees), or ``None`` for a no-op.
        The base implementation is passive, so the transparent test hook leaves
        pool mechanics unchanged. An active adapter overrides this to mutate
        ``position`` and move liquidity when it is worthwhile net of gas.
        """
        return None


# Singleton-ish default used when a hook world has no custom adapter.
PASSIVE_ADAPTER = HookAdapter()
