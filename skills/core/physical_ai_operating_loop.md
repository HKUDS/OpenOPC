---
name: physical_ai_operating_loop
description: "The operating doctrine of an AI-native Physical AI company — the outer learning loop, the independent referee (maker≠checker), safety-as-a-gate, the capability release ladder, and human-burden ratio. How a robotics role should actually run the work."
domain:
  - physical-ai
  - robotics
  - embodied-ai
  - safety
  - evaluation
  - deployment
trigger: "When a robotics role plans, reviews, or ships a physical capability — deciding whether a policy/deployment is good enough, safe enough, and economically useful enough to promote."
always_on: false
---

# Physical AI Operating Loop

The mental model in one line: **the robot learns inside the task; the company learns about the
robot.** These are two different loops. Never confuse them.

- **Inner loop (the robot):** sense → decide → move → sense. Milliseconds. VLA policy, perception,
  planner, control, safety supervisor.
- **Outer loop (you, the company):** capture episode → evaluate → diagnose → improve → regression-
  test → gate → deploy carefully → remember. Minutes to weeks. **This is the loop this doctrine is about.**

A robot can adapt locally and still make the company worse globally (grip harder → fewer drops →
cracked mugs). So local reward is never enough — the outer loop judges the *full* result.

## The unit of work is an episode

Every capability run produces an **episode**: task request · environment state · plan · action
sequence · outcomes · **independent evaluator verdict** · follow-up decision. If you cannot replay
why a robot failed, you are not running a learning system — you are collecting expensive bloopers.
An episode is the robotics analog of a test case + logs.

## Five questions every episode must answer

1. What did the robot try to do?  2. What actually happened?  3. **Who independently judged it?**
4. What changed because of that judgment?  5. How do we stop a solved failure from returning next
week in a different costume?

## The non-negotiables

- **Maker ≠ checker.** The policy that moved the robot does not get to declare success. An
  independent referee verifies with real evidence (vision in target zone, force within limits,
  collision monitor, trajectory validator, inventory check, timing, human review). Output a
  **capability scorecard**, not a pass/fail label:

  | Dimension | Question |
  |---|---|
  | Task success | Did the job finish correctly? |
  | Safety | Did it stay within constraints? |
  | Robustness | Did it survive variation (lighting, object, clutter)? |
  | Efficiency | Cycle time / path length wasted? |
  | Human burden | Did a person have to rescue it? |
  | Generalization | Does it work on held-out scenes? |

- **Safety is a gate, not a score.** A safety failure is *terminal*: it blocks promotion regardless
  of any other score, and the change rolls back. Non-tradeable conditions: collision with a person,
  force over limit, unsafe motion near a protected zone, loss of e-stop, unauthorized operation,
  sensor confidence below the autonomy minimum, inability to verify task state. On any of these:
  **stop → preserve evidence → diagnose → fix → retest → earn re-entry.** Never "but it was faster."

- **A failure isn't fixed until it's hard to reintroduce.** Every accepted improvement must add a
  durable asset: a regression test, a sim scenario, a replay episode, a safety constraint, a
  human-review trigger, or a monitor. Regression means more than "does the task still work" — vary
  lighting, object position, occlusion, sensor noise, human proximity, and "someone cleaned up."

- **Not every failure is a model update.** The right fix is often better lighting, a different
  gripper, a fixture, a deterministic rule, slowing down, routing to a human, or admitting the robot
  isn't ready for that task. "Train a bigger model" is a large wrench, not a diagnosis.

- **Diagnose with a taxonomy, not "robot failed."** perception / localization / classification /
  grasp-plan / contact-force / path-collision / policy-hesitation / sensor-degradation / novelty /
  human-interruption. You cannot improve what you cannot name.

## Promote on evidence — the release ladder

A capability climbs, it doesn't leap: **0 scripted demo → 1 teleop → 2 assisted → 3 supervised →
4 bounded autonomy (validated conditions = real economic value begins) → 5 generalized.** Promote a
level *only* when the evidence says it is safe, reliable, and economically useful **in its defined
operating domain** — never because a demo looked impressive. Moving boxes in one aisle ≠ carrying
soup in a nursing home.

## The metric that keeps you honest

**Human minutes per useful robot hour** (Human Burden Ratio = human-intervention-minutes ÷
useful-operating-minutes). A robot that runs an hour but needs 20 min teleop + 15 min rescue is
impressive, not autonomous. Goal: move humans *up* the value chain (drive → supervise → handle
exceptions → design the next capability), not eliminate them blindly.

## Compound only proven lessons

Capability memory is curated and versioned — proven operational knowledge, not a vector dump of
logs ("transparent cups fail depth estimation under east-facing morning light"; "grip soft bags at
the reinforced seam"). Compound lessons that came from *evaluated* improvement; random stories are
"superstition with embeddings." Keep learnings out of the referee (no contaminating the judge).

## How this maps to your OpenOPC company

You already have the machinery — use it deliberately: the **reviewer role is the independent
referee** (keep it distinct from the worker); a **review gate** carries the scorecard; an
**approval gate** + risk classification (`max_auto_approve_risk`) is the safety gate; **escalation
to the human owner** is where high-judgment forks go; per-role **experience profiles + playbooks**
are the capability memory; the **work-item dependency DAG** is the fleet of loops / capability
graph. Run the outer loop on purpose, and the robots get less clumsy while the company gets no more
reckless.
