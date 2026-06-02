// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

// External imports
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
// Internal imports
import {AntiSandwichHook} from "./general/AntiSandwichHook.sol";
import {CurrencySettler} from "./utils/CurrencySettler.sol";
import {BaseHook} from "./base/BaseHook.sol";

/**
 * @dev Concrete, deployable instantiation of OpenZeppelin's abstract
 * {AntiSandwichHook} for the v4-hook-LP-returns-sim harness.
 *
 * This is OpenZeppelin's canonical `AntiSandwichMock` (src/mocks/general/AntiSandwichMock.sol),
 * vendored here as the real hook the simulator deploys. The only changes are the
 * file/contract name and dropping the coverage-only `test()` stub; the economic
 * behaviour is unchanged.
 *
 * The {_afterSwapHandler} donates the anti-sandwich surplus back to the in-range
 * liquidity providers via `poolManager.donate`. In the single-LP simulation that
 * means the surplus accrues to the simulated LP's fee growth — i.e. value the
 * arbitrageur would otherwise have extracted (LVR) is returned to the LP.
 */
contract AntiSandwichHookHarness is AntiSandwichHook {
    using CurrencySettler for Currency;

    constructor(IPoolManager _poolManager) BaseHook(_poolManager) {}

    /**
     * @dev Handles the excess tokens collected during the swap due to the anti-sandwich mechanism.
     * When a swap executes at a worse price than what's currently available in the pool (due to
     * enforcing the beginning-of-block price), the excess tokens are donated back to the pool
     * to benefit all liquidity providers.
     */
    function _afterSwapHandler(
        PoolKey calldata key,
        SwapParams calldata params,
        BalanceDelta,
        uint256,
        uint256 feeAmount
    ) internal override {
        Currency unspecified = (params.amountSpecified < 0 == params.zeroForOne) ? (key.currency1) : (key.currency0);
        (uint256 amount0, uint256 amount1) = unspecified == key.currency0
            ? (uint256(uint128(feeAmount)), uint256(0))
            : (uint256(0), uint256(uint128(feeAmount)));

        // settle and donate excess tokens to the pool
        poolManager.donate(key, amount0, amount1, "");
        unspecified.settle(poolManager, address(this), feeAmount, true);
    }
}
