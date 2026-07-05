// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// ─────────────────────────────────────────────────────────────────────────────
// VerifiedClosePayout — a REFERENCE SKETCH, unaudited, illustrative only.
//
// It encodes the three non-negotiables of the employee-loop-incentive doctrine
// (see docs/employee-loop-incentive.md · skills/core/employee_loop_incentive.md):
//
//   1. maker != checker      — the proposer of a loop-close can never verify or
//                              get paid for their own claim. Two distinct parties.
//   2. compounding vesting    — reward is not paid up front; it STREAMS over time,
//                              and re-verification of durability EXTENDS the stream.
//                              A fix that keeps paying off earns more than a one-shot.
//   3. safety-zero gate       — a safety failure zeroes the UNRELEASED reward and
//                              halts the claim, regardless of any ROI. Not a weighted
//                              term; a terminal gate.
//
// Reward triggers on a VERIFIED CLOSE, never on a proposal or on effort.
// DO NOT DEPLOY AS-IS: no reentrancy hardening, no upgrade path, no oracle for the
// metric, no economic review. It exists to make the doctrine legible in code.
// ─────────────────────────────────────────────────────────────────────────────

contract VerifiedClosePayout {
    enum Status { Proposed, Verified, SafetyZeroed }

    struct Close {
        address maker;         // who proposed + did the work
        address checker;       // the independent referee who attested (0x0 until verified)
        bytes32 evidenceHash;  // hash of the episode/evidence (the trace, not a form)
        uint256 metricDelta;   // the verified improvement (e.g. burden-minutes reduced)
        uint256 totalReward;   // funded at verification, in wei
        uint256 released;      // amount already claimed by the maker
        uint256 startTime;     // vesting clock start (= verification time)
        uint256 vestingEnd;    // extends on each durability re-verification (compounding)
        Status  status;
    }

    address public immutable cno;         // funds the contract, registers referees + guardian
    address public safetyGuardian;        // can pull the safety-zero on any close
    uint256 public baseVestingPeriod = 90 days;
    uint256 public reverifyBonus = 30 days; // each proven-durable re-check extends the stream

    mapping(address => bool) public isChecker; // the independent-referee allowlist
    mapping(uint256 => Close) public closes;
    uint256 public nextId;

    event Proposed(uint256 indexed id, address indexed maker, bytes32 evidenceHash);
    event Verified(uint256 indexed id, address indexed checker, uint256 reward);
    event Reverified(uint256 indexed id, uint256 newVestingEnd);
    event Claimed(uint256 indexed id, address indexed maker, uint256 amount);
    event SafetyZeroed(uint256 indexed id, uint256 forfeited);

    modifier onlyCNO() { require(msg.sender == cno, "not CNO"); _; }

    constructor(address guardian) payable {
        cno = msg.sender;
        safetyGuardian = guardian;
    }

    // ── CNO builds the game: fund it, register independent referees ──────────
    receive() external payable {}
    function registerChecker(address who, bool ok) external onlyCNO { isChecker[who] = ok; }
    function setSafetyGuardian(address who) external onlyCNO { safetyGuardian = who; }

    // ── 1. A maker PROPOSES a close. No money moves — proposals are not paid. ─
    function proposeClose(bytes32 evidenceHash, uint256 metricDelta) external returns (uint256 id) {
        id = nextId++;
        Close storage c = closes[id];
        c.maker = msg.sender;
        c.evidenceHash = evidenceHash;
        c.metricDelta = metricDelta;
        c.status = Status.Proposed;
        emit Proposed(id, msg.sender, evidenceHash);
    }

    // ── 2. An INDEPENDENT checker verifies → funds the vesting stream. ────────
    //     maker != checker is enforced here, in code, not in a policy doc.
    function attestClose(uint256 id, uint256 reward) external {
        Close storage c = closes[id];
        require(c.status == Status.Proposed, "not proposable");
        require(isChecker[msg.sender], "not a referee");
        require(msg.sender != c.maker, "maker != checker"); // the whole point
        require(reward <= address(this).balance, "underfunded");

        c.checker = msg.sender;
        c.totalReward = reward;
        c.startTime = block.timestamp;
        c.vestingEnd = block.timestamp + baseVestingPeriod;
        c.status = Status.Verified;
        emit Verified(id, msg.sender, reward);
    }

    // ── 3. Compounding: a checker re-verifies the fix still holds → extends the
    //     stream, so a durable fix pays MORE than a one-shot. Still maker != checker.
    function reverifyDurable(uint256 id) external {
        Close storage c = closes[id];
        require(c.status == Status.Verified, "not verified");
        require(isChecker[msg.sender] && msg.sender != c.maker, "independent re-check only");
        c.vestingEnd += reverifyBonus;
        emit Reverified(id, c.vestingEnd);
    }

    // ── Safety-zero: a terminal gate. Zeroes the UNRELEASED reward, halts. ────
    function flagSafety(uint256 id) external {
        require(msg.sender == safetyGuardian, "not guardian");
        Close storage c = closes[id];
        require(c.status == Status.Verified, "nothing to zero");
        uint256 forfeited = c.totalReward - c.released; // already-earned stays; future is zeroed
        c.totalReward = c.released;
        c.status = Status.SafetyZeroed;
        emit SafetyZeroed(id, forfeited);
    }

    // ── The maker claims what has vested so far (linear stream). ──────────────
    function vestedAmount(uint256 id) public view returns (uint256) {
        Close storage c = closes[id];
        if (c.status != Status.Verified) return c.released; // zeroed/proposed: nothing new vests
        if (block.timestamp >= c.vestingEnd) return c.totalReward;
        uint256 elapsed = block.timestamp - c.startTime;
        uint256 span = c.vestingEnd - c.startTime;
        return (c.totalReward * elapsed) / span;
    }

    function claim(uint256 id) external {
        Close storage c = closes[id];
        require(msg.sender == c.maker, "only maker claims");
        uint256 amount = vestedAmount(id) - c.released;
        require(amount > 0, "nothing vested");
        c.released += amount;
        emit Claimed(id, msg.sender, amount);
        (bool ok, ) = msg.sender.call{value: amount}("");
        require(ok, "transfer failed");
    }
}
