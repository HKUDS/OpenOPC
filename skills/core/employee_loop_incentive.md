---
name: employee_loop_incentive
description: "How the Chief AI-Native Officer (CNO) makes every employee discover open loops, propose experiments, and close them — verified by an independent referee and rewarded on-chain for compounding value. Bottom-up loop-closing as a designed game, not a top-down mandate."
domain:
  - culture
  - incentives
  - org-design
  - web3
  - leadership
  - loop-engineering
trigger: "When designing how a company motivates people to find and close capability/workflow gaps — culture, incentives, a contribution/reward system, or a smart-contract payout for verified improvement."
always_on: false
---

# Employee Loop Incentive — the CNO's game

The one idea: **you cannot order people to close loops. You make gaps *visible*, experiments
*cheap and safe*, verification *independent*, and payout *automatic and proportional to verified
compounding value* — then get out of the way.** Top-down sets the *rules of the game* (infra +
smart contract). Bottom-up *plays* it. The CNO's job is not to motivate people; it is to build a
game so obviously fair and rewarding that playing it beats not playing it.

Contrarian anchor: "inspire/encourage" *from the top* is the wrong lever — mandates and pep talks
don't scale and rot with the next reorg. The real levers are **friction** (make closing a loop 10×
cheaper) and **price** (pay the verified close). Fix those two and motivation takes care of itself.

## The kitchen (stay in one picture)

An open kitchen. Every cook sees the **ticket-time board**. The grill backs up — an *open loop* —
and anyone can call it, not just the chef. A cook tries a prep tweak for **one shift, one station**
(bounded, reversible). The **expo** — not the cook who made the change — checks whether ticket time
actually dropped and *holds across shifts*. If it does, the **tip formula on the wall** (not the
chef's mood) pays that cook automatically, more if the fix keeps paying off. The **head chef never
cooks** — they build the kitchen, keep the fridge cold and knives sharp, and write a tip formula
that is fair and can't be gamed.

That kitchen is the company. The CNO is the head chef.

## The five mechanisms (map to your OpenOPC company)

| The ask | Kitchen | The mechanism (technical) |
|---|---|---|
| **Discover the open loop** | the ticket board everyone sees | An auto-surfaced **gap board** — a friction sensor (e.g. WorkflowX) plus your readiness `No`s and high-human-burden workflows. Nobody is told where gaps are; the sensor shows them. |
| **Propose + experiment** | try it one shift, one station | A **bounded closed-loop**: hypothesis + one metric + a rollback. Cheap (infra makes spinning one up a single command); safe (the safety gate is terminal). |
| **Verify** | the expo checks, not the cook | **maker ≠ checker** — an independent evaluator scores the result. You don't need a manager's blessing, you need the metric to move. This is what makes it bottom-up. |
| **Reward** | the tip formula on the wall | An on-chain **smart contract** that pays the *verified close* — never effort or proposals — with two signatures (maker + independent checker), streaming/vesting with the improvement's durability, and a safety-zero gate. |
| **See it compound** | the line gets faster week over week | A public ledger of closed loops + their **compounding curve** (did it lift the whole system / lower *total* human-burden?). Seeing your fix compound is the intrinsic reward; the payout is the extrinsic. Both point the same way. |

## The CNO's three jobs (top-down that *enables* bottom-up)

The CNO must **not** close loops for people — that recreates the bottleneck.

1. **Reliable / scalable / secure infra** — the kitchen: capture, the experiment sandbox, the
   independent referee, on-chain settlement. It must be trustworthy because real money rides on it.
2. **Incentivize culture** — the rules everyone believes are fair *before the contract pays*:
   evidence beats opinion, credit lands where earned, safety is never traded. Culture is what makes
   people *try*.
3. **Code it into a smart contract** — culture made **un-gameable and permanent**, surviving
   politics, turnover, and the founder's bad day.

## The crux — what the contract rewards (get this right, the rest follows)

- **Pay the *verified close*, never effort or proposals.** Effort-based reward pays the loudest.
- **Two signatures on-chain: maker + independent checker.** If the maker can attest their own result
  for money, they grade their own homework — encode `maker != checker` in the contract, not a policy doc.
- **Reward *compounding* — stream/vest the payout.** A fix that keeps lowering human-burden or lifts
  other work earns more, over time; a vanity one-shot earns once. This is the single most important
  knob: it makes people hunt **leverage**, not applause.
- **Safety is a hard zero, not a weighted term.** A safety failure zeroes the payout regardless of
  ROI — same terminal-gate rule as [[physical_ai_operating_loop]].

## The trap (say no to this)

Do not reward activity, proposals, or "innovation points." Reward **verified, compounding, safe
closes — only those.** Never let the maker be the checker on-chain. The scarce, valuable thing is a
verified loop-close that keeps paying. Price *that*, and nothing else.

## Sequence it — culture first, contract second

Put up the open-loop board publicly; let anyone claim a loop; run **one** bounded experiment with an
independent metric; pay the first verified close from a **manual bounty** *before* writing any
Solidity. Prove the game is fun and fair with humans in the loop, then encode the exact payout math
that worked. The contract's only job is to make a proven culture permanent — **never code an
incentive you haven't first run by hand.**

Reference contract sketch (unaudited, illustrative): `contracts/VerifiedClosePayout.sol`. Full map +
self-assessment: `docs/employee-loop-incentive.md`.
