// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {BeforeSwapDelta, BeforeSwapDeltaLibrary} from "@uniswap/v4-core/src/types/BeforeSwapDelta.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";

import {BaseHook} from "../base/BaseHook.sol";
import {AggregatorV3Interface} from "./AggregatorV3Interface.sol";

/**
 * @dev Example hook that consumes an EXTERNAL price oracle at runtime.
 *
 * Authored for this simulator (not vendored) to exercise the env mock-injection
 * seam — it depends on a Chainlink-style feed that must be deployed in the
 * sandbox, or every swap reverts. The behaviour is a real, recognised pattern: a
 * depeg / crash circuit breaker that pauses swaps while the oracle price sits
 * below a configured floor (and rejects a zero/invalid feed reading outright).
 *
 * It implements only `beforeSwap` and returns a zero delta, so when the breaker
 * is NOT tripped it leaves pool mechanics identical to a vanilla LP — the hook's
 * only effect is gating which swaps execute.
 */
contract OracleGuardHook is BaseHook {
    /// @dev The external price feed this hook reads on every swap.
    AggregatorV3Interface public immutable feed;
    /// @dev Swaps are paused while the feed answer is strictly below this floor
    /// (denominated in the feed's own units, e.g. 8-decimal USD).
    int256 public immutable minAnswer;

    error PriceBelowFloor(int256 answer, int256 floor);
    error InvalidOraclePrice(int256 answer);

    constructor(IPoolManager _poolManager, address _feed, int256 _minAnswer) BaseHook(_poolManager) {
        feed = AggregatorV3Interface(_feed);
        minAnswer = _minAnswer;
    }

    function _beforeSwap(address, PoolKey calldata, SwapParams calldata, bytes calldata)
        internal
        view
        override
        returns (bytes4, BeforeSwapDelta, uint24)
    {
        (, int256 answer,,,) = feed.latestRoundData();
        if (answer <= 0) revert InvalidOraclePrice(answer);
        if (answer < minAnswer) revert PriceBelowFloor(answer, minAnswer);
        return (this.beforeSwap.selector, BeforeSwapDeltaLibrary.ZERO_DELTA, 0);
    }

    function getHookPermissions() public pure override returns (Hooks.Permissions memory permissions) {
        permissions = Hooks.Permissions({
            beforeInitialize: false,
            afterInitialize: false,
            beforeAddLiquidity: false,
            afterAddLiquidity: false,
            beforeRemoveLiquidity: false,
            afterRemoveLiquidity: false,
            beforeSwap: true,
            afterSwap: false,
            beforeDonate: false,
            afterDonate: false,
            beforeSwapReturnDelta: false,
            afterSwapReturnDelta: false,
            afterAddLiquidityReturnDelta: false,
            afterRemoveLiquidityReturnDelta: false
        });
    }
}
