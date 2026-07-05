# The Employee Loop Incentive — one map + a scorecard

How do you get **every employee** (bottom-up, not top-down) to discover open loops, propose and
experiment to close them, provide visibility, and evaluate compounding effects — while the **Chief
AI-Native Officer (CNO)** builds reliable/scalable/secure infra, designs an incentivizing culture,
and codes it into a web3 smart contract?

The short answer: **you don't order it — you design a game.** Make gaps visible, experiments cheap
and safe, verification independent, and payout automatic and proportional to *verified compounding
value*. This doc is the map (Inspire → Verify → Reward → Measure), each piece pinned to an OpenOPC
primitive, plus a scorecard to grade your own incentive system. The doctrine ships as a mountable
CNO skill: `skills/core/employee_loop_incentive.md` (mounted on the `founding_ai_native_lead`).

## The one picture — an open kitchen

Every cook sees the ticket-time board (visible gaps). Anyone can call a backed-up station and try a
prep tweak for one shift (bounded experiment). The **expo** — not the cook — checks whether ticket
time actually dropped (independent referee). The **tip formula on the wall** pays automatically,
more if the fix keeps paying off (on-chain, compounding). The **head chef never cooks** — they build
the kitchen and write the formula (the CNO). Order people around and you get compliance; build this
kitchen and you get a company that hunts its own gaps.

## Inspire → Verify → Reward → Measure (mapped to OpenOPC)

| The ask | Mechanism | OpenOPC primitive |
|---|---|---|
| **Discover the open loop** | an auto-surfaced gap board | a friction sensor (WorkflowX) + your [readiness](physical-ai-operating-loop.md) `No`s + high-human-burden workflows |
| **Propose + experiment** | a bounded closed-loop (hypothesis + one metric + rollback) | the work-item DAG spins one up; the safety gate keeps it from touching production |
| **Provide visibility** | append-only, public by default | the shared event ledger / kanban; on-chain for anything that pays out |
| **Verify (maker ≠ checker)** | an independent evaluator, not the maker | review gate with `reviewer_role` ≠ worker (the independent referee) |
| **Reward** | on-chain payout for the *verified close* | the smart contract (`contracts/VerifiedClosePayout.sol`) — two sigs, compounding vesting, safety-zero |
| **Eval compounding effects** | a public compounding curve | per-role experience + promoted playbooks; did the fix lift the whole system / lower *total* human-burden? |
| **Escalate only the hard forks** | humans at the highest-leverage judgment | the escalation engine → human owner |

The CNO owns the three things that make bottom-up possible: **reliable/scalable/secure infra** (the
kitchen), **culture** (the fair rules people believe before the contract even pays), and the
**smart contract** (culture made un-gameable and permanent).

## The Incentive-Design Readiness Self-Assessment

Grade your company's incentive *system*, honestly — for each check, **no evidence ⇒ No** (a values
poster is not evidence; a working mechanism is):

| # | Check — do you actually have this? | "Yes" looks like |
|---|---|---|
| 1 | **Visible gap board** | Open loops are surfaced automatically and anyone can see + claim one |
| 2 | **Cheap, bounded experiments** | Spinning up a rollback-safe experiment is ~one command, not a committee |
| 3 | **Independent referee** (maker ≠ checker) | A verifier separate from the proposer decides if the metric moved |
| 4 | **Safety is a hard zero** | An unsafe "improvement" earns nothing and rolls back, regardless of ROI |
| 5 | **Pay the verified close, not effort** | Reward triggers on a moved metric, never on proposals, activity, or hours |
| 6 | **Compounding payout** | The reward streams/vests with the fix's durability — leverage earns more than a one-shot |
| 7 | **On-chain / immutable settlement** | The payout math is transparent and can't be overridden by a manager's mood |
| 8 | **Public compounding ledger** | Everyone can see closed loops and whether they lifted the whole system |

**Score → maturity level** (Check 4 *safety* and Check 3 *independent referee* are hard
prerequisites — without either you are capped at L1, because you're paying for unverified or unsafe
"wins"):

| Yes-count | Level | You are… |
|---|---|---|
| 0–1 | **L0 — Top-down** | gaps are found (if at all) by managers; improvement is assigned, not hunted |
| 2–3 | **L1 — Suggestion box** | people can propose, but nothing is verified or reliably rewarded |
| 4–5 | **L2 — Refereed bounties** | an independent judge + a safety zero; verified closes get paid (even by hand) |
| 6–7 | **L3 — Compounding culture** | payout rewards leverage, visibility is public; people hunt gaps unprompted |
| 8 | **L4 — Self-incentivizing org** | the game runs on-chain and un-gameable; the company closes its own loops while leadership designs the next one |

Most companies self-assess at **L0–L1**: they *say* "we value initiative," but there's no visible
gap board, no independent referee, and reward flows through a manager's judgment. The moat isn't a
mission statement — it's **a game where closing a loop is visible, safe, fair, and automatically
paid for what it compounds.** *(Visual: [incentive-design infographic](../landing/infographics/incentive-design.html).)*

## Sequence it — culture first, contract second

Run the manual version before the on-chain one: put up the board, let people claim loops, pay the
first verified close from a hand-managed bounty. Only encode the payout math **after** it has proven
fun and fair with humans in the loop. **Never code an incentive you haven't first run by hand** —
the contract's only job is to make a proven culture permanent. Reference sketch (unaudited):
[`contracts/VerifiedClosePayout.sol`](../contracts/VerifiedClosePayout.sol).
