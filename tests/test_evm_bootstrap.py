"""Step-2 gate test: bootstrap a fresh v4 PoolManager + routers in pyrevm,
initialize a pool, add liquidity, run a swap, and assert state moved.

Requires `forge build` to have been run in contracts/v4-core/ so artifacts
exist under contracts/v4-core/out/. The test skips with a clear message if
they're missing — keeps CI green on machines without solc.
"""

from __future__ import annotations

import pytest

from v4sim.evm.artifacts import ArtifactNotFound
from v4sim.evm.env import bootstrap_v4
from v4sim.evm.pool import PoolKey, initialize, modify_liquidity, read_slot0, swap

# Constants from v4-core test/Constants.sol
SQRT_PRICE_1_1 = 79228162514264337593543950336  # 1 << 96


def _bootstrap_or_skip():
    try:
        return bootstrap_v4()
    except ArtifactNotFound as e:
        pytest.skip(str(e))


def test_bootstrap_deploys_manager_and_routers():
    env = _bootstrap_or_skip()
    for label, addr in [
        ("manager", env.manager),
        ("swap_router", env.swap_router),
        ("modify_liquidity_router", env.modify_liquidity_router),
        ("currency0", env.currency0),
        ("currency1", env.currency1),
    ]:
        code = env.evm.get_code(addr)
        assert code and len(code) > 0, f"{label} at {addr} has no code"
    # Sorted invariant.
    assert int(env.currency0, 16) < int(env.currency1, 16)


def test_hello_world_swap_moves_pool_state():
    env = _bootstrap_or_skip()
    key = PoolKey(
        currency0=env.currency0,
        currency1=env.currency1,
        fee=3000,
        tick_spacing=60,
    )

    initialize(env, key, SQRT_PRICE_1_1)
    before = read_slot0(env, key)
    assert before.sqrt_price_x96 == SQRT_PRICE_1_1
    assert before.tick == 0
    assert before.lp_fee == 3000

    # Same range and size that v4-core's Deployers.LIQUIDITY_PARAMS uses.
    modify_liquidity(env, key, tick_lower=-120, tick_upper=120, liquidity_delta=10**18)

    # Exact-input zero-for-one swap: amountSpecified is negative.
    swap(env, key, zero_for_one=True, amount_specified=-100)

    after = read_slot0(env, key)
    # Selling token0 into the pool must push sqrtPrice down (and tick negative).
    assert after.sqrt_price_x96 < before.sqrt_price_x96, (before, after)
    assert after.tick < 0, after
    assert after.lp_fee == 3000  # unchanged for static-fee pool
