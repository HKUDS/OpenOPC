# The Physical AI Pack — one map: **Staff · Operate · Measure**

This is the single entry point for everything OpenOPC ships for Physical AI. If you read one file,
read this one — it tells you what the pack is, points you to the exact artifact for each job, and
gives you a way to **measure your own company** against the doctrine.

## What it is, in one breath

A Physical AI company is one loop turned fast — the **robot-learning flywheel**:

> **teach** *(teleoperation data)* → **learn** *(a Vision-Language-Action policy)* →
> **test** *(sim2real + evaluation)* → **deploy** *(field pilot)* →
> **observe** *(fleet telemetry)* → teach again.

OpenOPC lets one founder run that whole loop as an AI-native company: it **staffs** the org with
role-agents, has them **operate** by a proven learning-loop doctrine, and gives you a way to
**measure** whether your outer loop is actually getting better. Those three verbs are the map below.
*(This is the canonical statement of the flywheel; every other doc/skill/infographic references it.)*

## 1. Staff — assemble the company

| You want | Artifact |
|---|---|
| The industry-generalized org (11 roles, the flywheel as a work-item DAG) | preset `opc/market/builtin_presets/physical_ai_robotics_company.yaml` → `opc market apply-preset physical-ai-robotics-company` |
| A concrete prototype instance | saved org `.opc/config/company_orgs/org_roboforce-titan_config.yaml` → `opc chat --mode org --org roboforce-titan "…"` |
| Hireable role templates | `opc/market/talent_presets.py` (`category: robotics`) → `opc talent list` |
| The domain vocabulary each role reasons with | skill `skills/core/physical_ai.md` (VLA, teleop/RLDS, sim2real, manipulation, RaaS) |
| The JD research behind the roles | [`physical-ai-founding-roles.md`](physical-ai-founding-roles.md) (RoboForce prototype + industry archetypes) |

Anchor role: RoboForce's real open **Founding AI Native Lead**, generalized across the industry
(Physical Intelligence, Figure, 1X, Apptronik, Agility).

## 2. Operate — run the outer loop

The robot learns inside the task; **the company learns about the robot.** The outer loop is the
product, not the demo.

| You want | Artifact |
|---|---|
| The operating doctrine each role runs by | skill `skills/core/physical_ai_operating_loop.md` (independent referee, safety-as-a-gate, release ladder, human-burden ratio, capability memory) |
| How the doctrine maps to OpenOPC primitives (eval + gap) | [`physical-ai-operating-loop.md`](physical-ai-operating-loop.md) |

The five non-negotiables (full detail in the doctrine skill): **maker ≠ checker** (an independent
referee, never the policy grading its own homework) · **safety is a terminal gate, not a score** ·
a failure isn't fixed until it's **hard to reintroduce** · **not every failure is a model update** ·
**promote on evidence up the 0→5 release ladder**, in a defined operating domain.

## 3. Measure — the Readiness Self-Assessment

Doctrine you can't measure is just adjectives. Score your company's **outer loop** honestly: for
each check, **no evidence ⇒ No** (a form you filled in once is not evidence; a trace/artifact is).

| # | Check — do you actually have this? | "Yes" looks like |
|---|---|---|
| 1 | **Episode capture** — every attempt is replayable | A schema: task · env · plan · actions · outcome · verdict, queryable per capability |
| 2 | **Independent referee** (maker ≠ checker) | A judge separate from the policy, emitting a 6-dimension scorecard on real evidence |
| 3 | **Safety as a terminal gate** | A safety failure blocks promotion regardless of other scores, and rolls back |
| 4 | **Failure taxonomy** | Failures are classed (perception/grasp/collision/…), not "robot failed" |
| 5 | **Regression assets from every fix** | Each accepted change adds a test / sim scenario / constraint / monitor |
| 6 | **Curated capability memory** | Versioned *proven* lessons new hires inherit — not a log dump |
| 7 | **Release ladder promotion on evidence** | Capabilities move 0→5 by measured success in a defined domain, not by demo |
| 8 | **Human-burden ratio tracked** | You measure human-intervention-minutes ÷ useful-robot-minutes and drive it down |

**Score → maturity level** (mirrors the release ladder; Check 3 *safety* is a hard prerequisite —
without it you are capped at Level 1 no matter the count):

| Yes-count | Level | You are… |
|---|---|---|
| 0–1 | **L0 — Demo shop** | shipping trailers; gravity hasn't signed the API agreement |
| 2–3 | **L1 — Instrumented** | you can see what happened, but the judge is still the maker |
| 4–5 | **L2 — Refereed** | an independent judge + a safety gate; improvement is real, not vibes |
| 6–7 | **L3 — Compounding** | fixes leave regression assets and proven memory; the loop turns on its own |
| 8 | **L4 — Industrial learning system** | evidence-gated promotion + burden-ratio discipline; robots improve while you sleep |

The honest read: most "AI robotics" companies self-assess at **L0–L1** — they have a robot and a
model, but no independent referee and no safety gate. The moat isn't the hardware or the model; it
is **the quality of the loop that turns real-world evidence into verified capability.** This
scorecard is that loop, made countable. *(Visual: [readiness infographic](../landing/infographics/physical-ai-readiness.html).)*

## The whole pack at a glance

| Concept | Canonical artifact | Visual |
|---|---|---|
| Flywheel + roles | [`physical-ai-founding-roles.md`](physical-ai-founding-roles.md) | [infographic](../landing/infographics/physical-ai.html) |
| Operating doctrine + eval | [`physical-ai-operating-loop.md`](physical-ai-operating-loop.md) · `skills/core/physical_ai_operating_loop.md` | [infographic](../landing/infographics/physical-ai-operating-loop.html) |
| Readiness self-assessment | this doc (§3) | [infographic](../landing/infographics/physical-ai-readiness.html) |
| Domain vocabulary | `skills/core/physical_ai.md` | — |
| Runnable org | `physical_ai_robotics_company` preset · `roboforce-titan` org | — |
