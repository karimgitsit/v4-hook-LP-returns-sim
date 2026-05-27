"""Perfect arbitrageur: push the v4 pool's sqrtPrice back to a target.

The arb is "perfect" in the sense of zero gas + zero spread relative to
the truth price — the only spread it actually pays is the v4 pool's own
LP fee. That fee accrues to the LP (good for them); the rest of the
move is the arb's profit, which we book as "value extracted from LPs".

Mechanism
---------
We pass an arbitrarily-large `amountSpecified` plus a
`sqrtPriceLimitX96 = target`, so the v4 pool's own swap loop stops
exactly at the target tick. No iterative bisection in Python — the
pool's math is the bisection.
"""

from __future__ import annotations

from dataclasses import dataclass

from v4sim.evm.env import V4Env
from v4sim.evm.pool import PoolKey, read_slot0, swap

# Big enough that the price limit always binds before the amount runs out,
# but well within int128 to avoid pool-internal overflow.
HUGE_AMOUNT_SPECIFIED = (1 << 120) - 1


@dataclass(frozen=True)
class ArbResult:
    """Outcome of one arb-to-truth step."""

    delta0: int  # signed int128 — swapper's net token0 change (positive = received)
    delta1: int  # signed int128 — swapper's net token1 change
    pre_sqrt_price_x96: int
    post_sqrt_price_x96: int
    target_sqrt_price_x96: int
    skipped: bool = False  # true if the pool was already at the target

    @property
    def tick_error(self) -> int:
        """Useful in tests: difference in raw-sqrt steps (NOT actual tick units)."""
        return self.post_sqrt_price_x96 - self.target_sqrt_price_x96


def _decode_balance_delta(swap_return: bytes) -> tuple[int, int]:
    """Decode v4's packed (int128, int128) BalanceDelta from a 32-byte return word.

    Layout (see v4-core BalanceDelta.sol):
        bits 128-255 : amount0 (int128, sign-extended via arithmetic shift)
        bits   0-127 : amount1 (int128, sign-extended from byte 15)
    """
    if len(swap_return) < 32:
        raise ValueError(f"expected ≥32 bytes BalanceDelta, got {len(swap_return)}")
    delta = int.from_bytes(swap_return[:32], "big", signed=True)
    amount0 = delta >> 128  # Python >> is arithmetic for signed ints
    amount1 = delta & ((1 << 128) - 1)
    if amount1 & (1 << 127):
        amount1 -= 1 << 128
    return amount0, amount1


def arb_to_target(env: V4Env, key: PoolKey, target_sqrt_price_x96: int) -> ArbResult:
    """Drive the pool's sqrtPriceX96 to `target_sqrt_price_x96` via PoolSwapTest.

    Returns the arb's signed (delta0, delta1) and the resulting slot0
    sqrtPrice for accounting / verification. No-op (and zero-delta result)
    when the pool is already at target.
    """
    pre = read_slot0(env, key)
    if pre.sqrt_price_x96 == target_sqrt_price_x96:
        return ArbResult(
            delta0=0,
            delta1=0,
            pre_sqrt_price_x96=pre.sqrt_price_x96,
            post_sqrt_price_x96=pre.sqrt_price_x96,
            target_sqrt_price_x96=target_sqrt_price_x96,
            skipped=True,
        )
    zero_for_one = target_sqrt_price_x96 < pre.sqrt_price_x96
    out = swap(
        env,
        key,
        zero_for_one=zero_for_one,
        amount_specified=-HUGE_AMOUNT_SPECIFIED,
        sqrt_price_limit_x96=target_sqrt_price_x96,
    )
    d0, d1 = _decode_balance_delta(out)
    post = read_slot0(env, key)
    return ArbResult(
        delta0=d0,
        delta1=d1,
        pre_sqrt_price_x96=pre.sqrt_price_x96,
        post_sqrt_price_x96=post.sqrt_price_x96,
        target_sqrt_price_x96=target_sqrt_price_x96,
        skipped=False,
    )
