"""Adapter for OpenZeppelin's ReHypothecationHook — a Tier-2 *own-liquidity* hook.

Unlike a vanilla pool, this hook OWNS the liquidity: the LP deposits via
``addReHypothecatedLiquidity(shares)`` and holds the hook's ERC-20 shares, while
the underlying sits in ERC-4626 yield sources and is JIT-injected into the pool
only during swaps. So the harness must not place a router position; instead this
adapter drives the deposit and values the LP's stake.

Valuation maps onto the existing accounting cleanly:

* the LP's shares are treated as full-range liquidity ``L`` (at the first
  deposit the hook itself sets ``liquidity == shares``), so the runner's standard
  amounts/IL math values the *principal*;
* ``previewRedeem(shares)`` returns the LP's *total* claim (principal + collected
  fees + any vault yield), so ``extra_value_usdc`` reports the difference —
  everything on top of principal — in the world's fees column.
"""

from __future__ import annotations

import logging

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak

from v4sim.evm.env import DEFAULT_GAS_LIMIT, V4Env
from v4sim.evm.pool import PoolKey
from v4sim.evm.tickmath import MAX_TICK, MIN_TICK, get_sqrt_price_at_tick
from v4sim.metrics.accounting import amounts_for_liquidity, usdc_value_of_position
from v4sim.strategies.hook_adapter import HookAdapter, PositionState

log = logging.getLogger(__name__)

_MAX_UINT = (1 << 256) - 1
# Linear probe: at the first deposit previewMint is linear in shares, so we mint a
# probe, measure its value, and scale to hit the target notional in one step.
_PROBE_SHARES = 10**18


def _selector(sig: str) -> bytes:
    return keccak(sig.encode())[:4]


def _min_usable_tick(spacing: int) -> int:
    # Mirror v4 TickMath.minUsableTick/maxUsableTick (truncate toward zero).
    return int(MIN_TICK / spacing) * spacing


def _max_usable_tick(spacing: int) -> int:
    return int(MAX_TICK / spacing) * spacing


class ReHypothecationAdapter(HookAdapter):
    """Drives deposits into, and values the LP stake of, a ReHypothecationHook."""

    manages_own_liquidity = True

    def __init__(self, seed_frac: float = 1.0) -> None:
        # ReHypothecation JIT-injects liquidity during a swap and `take`s its
        # owed tokens from the PoolManager in afterSwap — before the swapper has
        # settled their input. The manager therefore needs standing reserves or
        # the take's ERC20 transfer reverts (a limitation OZ's own docs flag,
        # recommending "some permanent pool liquidity"). `seed_frac` places a
        # router-owned full-range backstop sized `seed_frac * notional` to provide
        # those reserves — economically, the co-existing pool liquidity the hook
        # needs. Its fees are NOT counted toward the hook LP (it is not the hook's
        # position), so the hook earns only its proportional share.
        self.seed_frac = seed_frac
        self._hook: str | None = None
        self._shares: int = 0

    # -- low-level calls -------------------------------------------------
    def _read2(self, env: V4Env, sig: str, shares: int) -> tuple[int, int]:
        """Call a ``fn(uint256) -> (uint256, uint256)`` view on the hook."""
        data = _selector(sig) + abi_encode(["uint256"], [shares])
        out = env.evm.message_call(
            caller=env.deployer, to=self._hook, calldata=data, gas=DEFAULT_GAS_LIMIT
        )
        a0, a1 = abi_decode(["uint256", "uint256"], bytes(out))
        return int(a0), int(a1)

    # -- HookAdapter API -------------------------------------------------
    def setup(
        self,
        env: V4Env,
        key: PoolKey,
        *,
        hook_addr: str,
        target_usdc: float,
        usdc_is_currency0: bool,
    ) -> PositionState:
        self._hook = hook_addr

        # Standing reserve backstop so the hook's in-swap `take` has tokens to pull.
        if self.seed_frac > 0:
            from v4sim.evm.pool import read_slot0
            from v4sim.strategies.placement import place_position

            slot0 = read_slot0(env, key)
            place_position(
                env, key,
                center_tick=slot0.tick, center_sqrt_x96=slot0.sqrt_price_x96,
                tick_spacing=key.tick_spacing, band_pct=None,
                target_usdc=self.seed_frac * target_usdc, usdc_is_currency0=usdc_is_currency0,
            )

        # Let the hook pull both currencies from the LP (the deployer).
        for token in (env.currency0, env.currency1):
            env.call(
                token,
                _selector("approve(address,uint256)")
                + abi_encode(["address", "uint256"], [hook_addr, _MAX_UINT]),
            )

        # Size the deposit: value a probe mint at the current price, then scale.
        from v4sim.evm.pool import read_slot0

        sqrt_p = read_slot0(env, key).sqrt_price_x96
        pa0, pa1 = self._read2(env, "previewMint(uint256)", _PROBE_SHARES)
        probe_value = usdc_value_of_position(
            pa0, pa1, sqrt_p,
            decimals0=env.decimals0, decimals1=env.decimals1, usdc_is_token0=usdc_is_currency0,
        )
        if probe_value <= 0:
            raise RuntimeError("rehypothecation: probe mint valued at zero")
        shares = int(_PROBE_SHARES * target_usdc / probe_value)
        if shares <= 0:
            raise RuntimeError("rehypothecation: computed zero shares")

        env.call(
            hook_addr,
            _selector("addReHypothecatedLiquidity(uint256)") + abi_encode(["uint256"], [shares]),
        )
        self._shares = shares

        spacing = key.tick_spacing
        tl, tu = _min_usable_tick(spacing), _max_usable_tick(spacing)
        return PositionState(
            tick_lower=tl,
            tick_upper=tu,
            sqrt_a_x96=get_sqrt_price_at_tick(tl),
            sqrt_b_x96=get_sqrt_price_at_tick(tu),
            liquidity=shares,  # at first deposit the hook sets liquidity == shares
        )

    def extra_value_usdc(
        self,
        env: V4Env,
        key: PoolKey,
        position: PositionState,
        sqrt_price_x96: int,
        *,
        usdc_is_currency0: bool,
    ) -> float:
        """Fees + yield = (redeemable total) − (principal at current price)."""
        if self._shares == 0:
            return 0.0
        t0, t1 = self._read2(env, "previewRedeem(uint256)", self._shares)
        total = usdc_value_of_position(
            t0, t1, sqrt_price_x96,
            decimals0=env.decimals0, decimals1=env.decimals1, usdc_is_token0=usdc_is_currency0,
        )
        p0, p1 = amounts_for_liquidity(
            sqrt_price_x96, position.sqrt_a_x96, position.sqrt_b_x96, position.liquidity
        )
        principal = usdc_value_of_position(
            p0, p1, sqrt_price_x96,
            decimals0=env.decimals0, decimals1=env.decimals1, usdc_is_token0=usdc_is_currency0,
        )
        return total - principal
