"""LP position math.

Ports the Uniswap v3 LiquidityAmounts library to Python so we can compute
position underlying amounts (and from them, USDC-denominated LP equity)
without round-tripping through the EVM.

All math uses Python's arbitrary-precision ints, so the explicit
``uint128`` clamp from the Solidity original is unnecessary.
"""

from __future__ import annotations

Q96 = 1 << 96
Q192 = 1 << 192


def _ordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def liquidity_for_amount0(sqrt_a_x96: int, sqrt_b_x96: int, amount0: int) -> int:
    """L such that `amount0` of token0 is required when the price is in [a, b]."""
    a, b = _ordered(sqrt_a_x96, sqrt_b_x96)
    intermediate = (a * b) // Q96
    return (amount0 * intermediate) // (b - a)


def liquidity_for_amount1(sqrt_a_x96: int, sqrt_b_x96: int, amount1: int) -> int:
    """L such that `amount1` of token1 is required when the price is in [a, b]."""
    a, b = _ordered(sqrt_a_x96, sqrt_b_x96)
    return (amount1 * Q96) // (b - a)


def liquidity_for_amounts(
    sqrt_p_x96: int,
    sqrt_a_x96: int,
    sqrt_b_x96: int,
    amount0: int,
    amount1: int,
) -> int:
    """Maximum liquidity addable given both token amounts at the current price."""
    a, b = _ordered(sqrt_a_x96, sqrt_b_x96)
    if sqrt_p_x96 <= a:
        return liquidity_for_amount0(a, b, amount0)
    if sqrt_p_x96 < b:
        return min(
            liquidity_for_amount0(sqrt_p_x96, b, amount0),
            liquidity_for_amount1(a, sqrt_p_x96, amount1),
        )
    return liquidity_for_amount1(a, b, amount1)


def amount0_for_liquidity(sqrt_a_x96: int, sqrt_b_x96: int, liquidity: int) -> int:
    a, b = _ordered(sqrt_a_x96, sqrt_b_x96)
    return ((liquidity << 96) * (b - a)) // b // a


def amount1_for_liquidity(sqrt_a_x96: int, sqrt_b_x96: int, liquidity: int) -> int:
    a, b = _ordered(sqrt_a_x96, sqrt_b_x96)
    return (liquidity * (b - a)) // Q96


def amounts_for_liquidity(
    sqrt_p_x96: int,
    sqrt_a_x96: int,
    sqrt_b_x96: int,
    liquidity: int,
) -> tuple[int, int]:
    """Underlying (amount0, amount1) for `liquidity` at the current price."""
    a, b = _ordered(sqrt_a_x96, sqrt_b_x96)
    if sqrt_p_x96 <= a:
        return amount0_for_liquidity(a, b, liquidity), 0
    if sqrt_p_x96 < b:
        return (
            amount0_for_liquidity(sqrt_p_x96, b, liquidity),
            amount1_for_liquidity(a, sqrt_p_x96, liquidity),
        )
    return 0, amount1_for_liquidity(a, b, liquidity)


def price_token1_in_token0(sqrt_p_x96: int) -> float:
    """Raw price = (sqrtP / 2^96)^2 — token1 native units per token0 native unit.

    For ETH/USDC (USDC=token0 dec=6, WETH=token1 dec=18), this is raw WETH per
    raw USDC; multiply by 10^(dec0 - dec1) for the human-readable USDC/ETH.
    """
    return (sqrt_p_x96 / (1 << 96)) ** 2


def usdc_value_of_position(
    amount0_raw: int,
    amount1_raw: int,
    sqrt_p_x96: int,
    *,
    decimals0: int,
    decimals1: int,
    usdc_is_token0: bool,
) -> float:
    """Value a position holding (amount0, amount1) of the pool's currencies in USDC.

    Both raw amounts are valued at the current pool price. Returned in
    human-readable USDC units (e.g. 1_000_000 means $1M).
    """
    p_raw = price_token1_in_token0(sqrt_p_x96)
    # Convert to human-readable units.
    amount0_h = amount0_raw / (10**decimals0)
    amount1_h = amount1_raw / (10**decimals1)
    # Human-readable price: human-token1 per human-token0 = p_raw * 10^(d0 - d1).
    price_t1_per_t0_human = p_raw * (10 ** (decimals0 - decimals1))
    if usdc_is_token0:
        # token0 = USDC, token1 = volatile. Value token1 in USDC via 1/price.
        return amount0_h + amount1_h / price_t1_per_t0_human
    # token1 = USDC, token0 = volatile.
    return amount1_h + amount0_h * price_t1_per_t0_human
