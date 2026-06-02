// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

/// @dev Minimal Chainlink AggregatorV3Interface — the subset an on-chain
/// consumer needs to read a price feed. Authored for this simulator (it mirrors
/// Chainlink's published interface) so example hooks can depend on a price oracle
/// without vendoring the whole Chainlink contracts tree.
interface AggregatorV3Interface {
    function decimals() external view returns (uint8);

    function latestRoundData()
        external
        view
        returns (uint80 roundId, int256 answer, uint256 startedAt, uint256 updatedAt, uint80 answeredInRound);
}
