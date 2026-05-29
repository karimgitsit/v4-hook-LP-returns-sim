"""Step-7a gate: fee-growth storage reads match a modifyLiquidity(0) poke.

The storage path (StateLibrary port) must agree, to the wei, with realizing
fees on-chain — and both must equal 0.05% of the swap volume for a 5bps pool.
Skips if the forge artifacts aren't built.
"""

from __future__ import annotations

import math

import pytest

from v4sim.evm.arb import _decode_balance_delta
from v4sim.evm.artifacts import ArtifactNotFound
from v4sim.evm.env import bootstrap_v4_eth_usdc
from v4sim.evm.pool import PoolKey, initialize, modify_liquidity, read_slot0, swap
from v4sim.evm.state import read_fee_growth_inside, uncollected_fees_raw
from v4sim.strategies.full_range import usable_tick_range


def _env():
    try:
        return bootstrap_v4_eth_usdc()
    except ArtifactNotFound as e:
        pytest.skip(str(e))


def _full_range_pool(env):
    # USDC=token0(6 dec), WETH=token1(18 dec); init at ~3000 USDC/ETH.
    p_raw = (1 / 3000) * 10**12
    sqrtP = int(math.isqrt(int(p_raw * (1 << 192))))
    key = PoolKey(currency0=env.currency0, currency1=env.currency1, fee=500, tick_spacing=10)
    initialize(env, key, sqrtP)
    lower, upper = usable_tick_range(10)
    modify_liquidity(env, key, tick_lower=lower, tick_upper=upper, liquidity_delta=10**18)
    return key, lower, upper


def test_storage_fees_match_poke_and_volume():
    env = _env()
    key, lower, upper = _full_range_pool(env)
    owner = env.modify_liquidity_router

    # 3 swaps of 100k USDC in, 3 of 30 WETH in.
    for i in range(6):
        z4o = i % 2 == 0
        amt = 100_000 * 10**6 if z4o else 30 * 10**18
        swap(env, key, zero_for_one=z4o, amount_specified=-amt)

    cur = read_slot0(env, key).tick
    f0, f1 = uncollected_fees_raw(
        env, key, owner=owner, tick_lower=lower, tick_upper=upper, current_tick=cur
    )

    # 0.05% of 3*100k USDC and 3*30 WETH (allow ±1 wei rounding from the pool).
    assert f0 == pytest.approx(int(0.0005 * 3 * 100_000 * 10**6), abs=2)
    assert f1 == pytest.approx(int(0.0005 * 3 * 30 * 10**18), abs=10**12)

    # Poke modifyLiquidity(0): the realized fee delta must equal the storage read.
    out = modify_liquidity(env, key, tick_lower=lower, tick_upper=upper, liquidity_delta=0)
    d0, d1 = _decode_balance_delta(out)
    assert d0 == f0
    assert d1 == f1


def test_full_range_fee_growth_inside_equals_global():
    """For a full-range position the boundary ticks never cross, so
    feeGrowthInside == feeGrowthGlobal."""
    from v4sim.evm.state import read_fee_growth_globals

    env = _env()
    key, lower, upper = _full_range_pool(env)
    for i in range(4):
        swap(env, key, zero_for_one=i % 2 == 0,
             amount_specified=-(50_000 * 10**6 if i % 2 == 0 else 15 * 10**18))
    cur = read_slot0(env, key).tick
    g0, g1 = read_fee_growth_globals(env, key)
    in0, in1 = read_fee_growth_inside(env, key, lower, upper, cur)
    assert in0 == g0
    assert in1 == g1
