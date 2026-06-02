// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import {AggregatorV3Interface} from "../oracle/AggregatorV3Interface.sol";

/// @dev Minimal mock of a Chainlink price feed, deployed into the sandbox by the
/// simulator's env mock-injection seam so a hook that consumes an external oracle
/// has something to read. `updateAnswer` lets a driver move the price during a
/// replay. Authored for this simulator (a trimmed version of Chainlink's own
/// MockV3Aggregator test contract).
contract MockV3Aggregator is AggregatorV3Interface {
    uint8 public immutable override decimals;
    int256 public answer;
    uint256 public updatedAt;
    uint80 public roundId;

    constructor(uint8 _decimals, int256 _initialAnswer) {
        decimals = _decimals;
        answer = _initialAnswer;
        updatedAt = block.timestamp;
        roundId = 1;
    }

    /// @dev Set a new answer (and bump round/timestamp), as a real feed would.
    function updateAnswer(int256 _answer) external {
        answer = _answer;
        updatedAt = block.timestamp;
        roundId += 1;
    }

    function latestRoundData()
        external
        view
        override
        returns (uint80, int256, uint256, uint256, uint80)
    {
        return (roundId, answer, updatedAt, updatedAt, roundId);
    }
}
