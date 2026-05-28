"""Exact integer TickMath port (v4-core TickMath.getSqrtPriceAtTick).

Used to convert position tick bounds to sqrtPriceX96 for off-chain LP
valuation, so our accounting lines up bit-for-bit with what the pool
computed on deposit. Python's big ints make the Solidity `unchecked`
128-bit fixed-point math exact.
"""

from __future__ import annotations

import math

MIN_TICK = -887272
MAX_TICK = 887272

# Multipliers from v4-core TickMath; each corresponds to a bit of |tick|.
_BIT_MULTIPLIERS = [
    (0x1, 0xFFFCB933BD6FAD37AA2D162D1A594001),
    (0x2, 0xFFF97272373D413259A46990580E213A),
    (0x4, 0xFFF2E50F5F656932EF12357CF3C7FDCC),
    (0x8, 0xFFE5CACA7E10E4E61C3624EAA0941CD0),
    (0x10, 0xFFCB9843D60F6159C9DB58835C926644),
    (0x20, 0xFF973B41FA98C081472E6896DFB254C0),
    (0x40, 0xFF2EA16466C96A3843EC78B326B52861),
    (0x80, 0xFE5DEE046A99A2A811C461F1969C3053),
    (0x100, 0xFCBE86C7900A88AEDCFFC83B479AA3A4),
    (0x200, 0xF987A7253AC413176F2B074CF7815E54),
    (0x400, 0xF3392B0822B70005940C7A398E4B70F3),
    (0x800, 0xE7159475A2C29B7443B29C7FA6E889D9),
    (0x1000, 0xD097F3BDFD2022B8845AD8F792AA5825),
    (0x2000, 0xA9F746462D870FDF8A65DC1F90E061E5),
    (0x4000, 0x70D869A156D2A1B890BB3DF62BAF32F7),
    (0x8000, 0x31BE135F97D08FD981231505542FCFA6),
    (0x10000, 0x9AA508B5B7A84E1C677DE54F3E99BC9),
    (0x20000, 0x5D6AF8DEDB81196699C329225EE604),
    (0x40000, 0x2216E584F5FA1EA926041BEDFE98),
    (0x80000, 0x48A170391F7DC42444E8FA2),
]

_UINT256_MAX = (1 << 256) - 1


def get_sqrt_price_at_tick(tick: int) -> int:
    """Return sqrtPriceX96 for `tick`, matching v4-core TickMath exactly."""
    if not (MIN_TICK <= tick <= MAX_TICK):
        raise ValueError(f"tick {tick} out of range [{MIN_TICK}, {MAX_TICK}]")
    abs_tick = -tick if tick < 0 else tick

    price = 0xFFFCB933BD6FAD37AA2D162D1A594001 if abs_tick & 0x1 else (1 << 128)
    for bit, mult in _BIT_MULTIPLIERS[1:]:
        if abs_tick & bit:
            price = (price * mult) >> 128

    if tick > 0:
        price = _UINT256_MAX // price

    # Q128.128 -> Q128.96, rounding up.
    return (price >> 32) + (0 if price % (1 << 32) == 0 else 1)


def tick_at_price_ratio(base_tick: int, ratio: float) -> int:
    """Tick offset corresponding to multiplying price by `ratio` (e.g. 0.9, 1.1).

    Uses float log — adequate because callers align the result to a tick
    spacing, so sub-tick float error never crosses a boundary.
    """
    return base_tick + int(round(math.log(ratio) / math.log(1.0001)))


def align_tick(tick: int, tick_spacing: int, *, round_up: bool) -> int:
    """Round `tick` to a multiple of `tick_spacing` (up or down)."""
    if round_up:
        return -((-tick) // tick_spacing) * tick_spacing
    return (tick // tick_spacing) * tick_spacing
