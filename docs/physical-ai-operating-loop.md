# The AI-Native Physical AI Company — OpenOPC alignment & gap analysis

## What this is

A manifesto ([“The AI-Native Physical AI Company”](physical-ai-founding-roles.md)) argues that the
winning Physical AI companies are not the ones with the best robot demo — they are the ones with the
best **outer loop**: the system that turns every real-world attempt into a safer, more capable next
attempt. *The robot learns inside the task; the company learns about the robot.*

This doc does two things: (1) **eval** — score OpenOPC honestly against that blueprint, naming what
the engine already gives you versus what is only doctrine; and (2) **align** — pin each blueprint
idea to the exact OpenOPC primitive, so an engineer can *run* the loop instead of re-reading prose.
The doctrine itself is shipped as a mountable role skill: `skills/core/physical_ai_operating_loop.md`
(mounted on the `physical-ai-robotics-company` roles via `skill_refs`).

## The headline

**OpenOPC is already the software substrate the blueprint describes.** Its Self-Built / Self-Run /
Self-Grown loops, review gates, human escalation, work-item DAG, and self-evolution memory map
almost one-to-one onto the blueprint's outer loop. What the physical-AI pack previously *lacked* was
the operating **doctrine** — independent referee → capability scorecard, safety-as-terminal-gate, the
0→5 release ladder, and human-burden ratio. This change encodes that doctrine.

## Eval: blueprint → OpenOPC primitive → status

Legend — ✅ present in the engine · 🟡 partial / needs deliberate use · 📘 now shipped as doctrine.

| Blueprint idea | OpenOPC primitive | Status |
|---|---|---|
| Outer loop: *company learns about the robot* | Self-Grown: `employee_evolution`, promoted playbooks | ✅ |
| Episode = replayable slice of reality | Runtime `Task` + events + transcript + checkpoints | 🟡 (exists as execution records; not yet a robotics-episode schema) |
| **Maker ≠ checker** (independent referee) | Review gate with `reviewer_role` ≠ worker; `gate_harness`; verdict parse | ✅ |
| Capability scorecard (6 dimensions, not pass/fail) | Review verdict / output contract | 🟡 → 📘 (doctrine: referee emits the scorecard) |
| **Safety is a terminal gate**, not a weighted score | Approval gate + risk classification, `autonomy.max_auto_approve_risk` | ✅ mechanism → 📘 (doctrine: safety failure blocks promotion, rolls back) |
| Regression rollback as first-class control | Rework mode + phase transitions; checkpoints/recovery | ✅ |
| A failure isn't fixed until hard to reintroduce | Skill / playbook / test asset from each accepted fix | 🟡 → 📘 (doctrine: every fix adds a durable asset) |
| Diagnosis taxonomy (name the failure class) | — | 📘 (doctrine-only; a robotics failure taxonomy) |
| Not-every-failure-is-a-model-update | Role runtime policy: `auto`/`native`/`external`; route-to-human | ✅ mechanism → 📘 (doctrine) |
| Capability memory (proven, versioned, not a log dump) | `markdown_memory`, `skill_library`, per-role experience profiles | ✅ |
| Keep learnings out of the referee | Reviewer session scoping | 🟡 → 📘 (doctrine) |
| Fleet of loops / capability graph | Work-item dependency DAG (`task_graph`, `org_work_item_planner`) | ✅ |
| Human at the highest-leverage judgment fork | Escalation engine → human owner | ✅ |
| **Release ladder** 0 scripted → 5 generalized | Autonomy config + per-role execution strategy | 🟡 → 📘 (doctrine: promote on evidence, per operating domain) |
| **Human-burden ratio** (human-min / useful-robot-hour) | Cost tracker (tokens/cost today) | 📘 (doctrine-only metric; a real deployment would instrument it) |
| Two-speed org (fast robot loop / slow company loop) | Task Mode (fast, single agent) vs Company Mode (deliberate, gated) | ✅ (the modes literally mirror this) |
| Four flywheels (capability / safety / operations / economic) | Self-Built/Run/Grown compounding | 🟡 (capability + safety flywheels are the shipped roles/gates; operations + economic are org-design) |

**Read of the scorecard:** the *machinery* is largely ✅ — OpenOPC does not need new subsystems to be
this company. The gap was doctrine: making the roles *use* the referee/gate/ladder discipline on
purpose. That is what the mounted skill and the enriched preset gates now do. The remaining honest
📘-only items (episode schema, human-burden instrumentation, failure taxonomy as data) are the next
real build for anyone running actual robots — see “What a real deployment adds” below.

## Align: where each idea lives, concretely

- **Independent referee** → the preset's `ai_infra_reliability_engineer` is the reviewer on the
  `sim2real_eval` and `field_pilot` gates; its prompt now instructs it to emit the six-dimension
  scorecard and to treat safety as terminal (never let the policy grade its own homework).
- **Safety gate** → `capability_delivery` is an `approval` gate owned by the
  `founding_ai_native_lead`; the autonomy layer (`system_config.yaml → autonomy.max_auto_approve_risk`)
  is the risk classifier that escalates dangerous actions to a human.
- **Release ladder** → the `founding_ai_native_lead` prompt now requires promoting a capability up
  the 0→5 ladder on evidence, scoped to a defined operating domain — not on demo polish.
- **Capability memory** → per-role experience profiles + promoted playbooks (`employee_evolution`),
  which new hires inherit.
- **Fleet of loops** → the flywheel work-item DAG already ships in the preset (teleop → policy →
  sim2real → integration → field pilot → telemetry → learning feedback).

## What a real robot deployment adds (the honest 📘 backlog)

OpenOPC coordinates the *company* around the loop; it does not sense torque or run a collision
monitor. A physical deployment would still build: a robotics **episode schema** (sensor/joint/force
capture), the **independent evaluator wiring** (vision/force/collision monitors as real evidence
sources), a **failure taxonomy as data**, and **human-burden-ratio instrumentation**. This doc marks
those explicitly so no one mistakes doctrine for a shipped sensor. The point of the alignment: when
you build them, they slot into primitives OpenOPC already has — gates, memory, escalation, the DAG.
