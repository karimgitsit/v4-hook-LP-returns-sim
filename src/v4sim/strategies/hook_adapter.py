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

Step 6 wires only the passive (no-op) adapter, which is what the transparent
test hook uses — its position is placed by the harness like a vanilla LP and
it never rebalances. The active deposit/withdraw paths fill in once a real
hook (directional-liquidity) exists.
"""

from __future__ import annotations

from v4sim.evm.env import V4Env
from v4sim.evm.pool import PoolKey


class HookAdapter:
    """Default adapter: passive. Subclass and override for active hooks."""

    #: Set on subclasses that drive deposits/rebalances through the hook itself
    #: rather than letting the harness place a vanilla position.
    manages_own_liquidity: bool = False

    def rebalance(self, env: V4Env, key: PoolKey, truth_sqrt_price_x96: int) -> bool:
        """Tick keeper. Called after each swap+arb at the truth price.

        Return True if a rebalance was performed. The base implementation is a
        no-op (passive LP), so the transparent test hook leaves pool mechanics
        unchanged. An active hook adapter overrides this to call the hook's
        ``rebalance()`` when it is profitable net of the configured gas.
        """
        return False


# Singleton-ish default used when a hook world has no custom adapter.
PASSIVE_ADAPTER = HookAdapter()
