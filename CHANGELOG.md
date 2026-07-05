# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- **Refactor batch 2: cleared two more `opc/cli/app.py` god-functions + shrank `exec`.** Continuing
  PR #12, extracted (verbatim, behaviour-preserving): `status` 67→**46** lines and `channels_status`
  54→**29** (both now below the god-function threshold — `anyagent analyze` no longer flags them) via
  `_print_status_summary` and `_channel_status_line`; and `_exec_message` 153→**124** by lifting its
  two `stream_json` callbacks into `_make_exec_stream_callbacks`. Verified identical behaviour:
  `tests/test_cli_app.py` **88 passed** before and after; full suite unchanged (1655 passed, 24
  pre-existing failures, zero regressions).

- **Refactor: extracted the default-org builder out of `opc/cli/app.py::init`.** `init()` was a
  263-line god-function (flagged by `anyagent analyze`); ~170 of those lines were a pure, hardcoded
  default "Corporate" org (roles + escalation rules). Extracted verbatim to
  `_apply_default_corporate_org(config)` — a behaviour-preserving move that drops `init()` to **91
  lines** and makes the command read as a clear sequence (resolve config → dirs → skills → memory →
  project → trust → preflight). Verified identical behaviour: `tests/test_cli_app.py` **88 passed**
  before and after; full suite unchanged (1655 passed, 24 pre-existing failures). *(The other
  analyze-flagged CLI god-functions — `_exec_message` 146 lines, `status`, `channels_status` — are
  the same kind of safe follow-up.)*

### Added

- **Architecture, made contributor-legible.** A code-base deep dive (`anyagent analyze` → 75/100,
  documentation the weakest axis at 20%) scoped the highest-ROI "refactor" to docs, not risky engine
  surgery. Added [`landing/infographics/architecture.html`](landing/infographics/architecture.html)
  (the 7-layer stack + brief→delivery flow + a where-to-contribute table),
  [`docs/architecture.md`](docs/architecture.md) (the seven key technical decisions with their *why*,
  plus the conventions that bite), and [`CONTRIBUTING.md`](CONTRIBUTING.md) (copy-paste recipes for
  adding an industry org / tool / channel / external-agent adapter). Revived the README's dormant
  (commented-out) Architecture section as a live two-reader map. `tests/test_docs_architecture.py`
  guards the new docs' relative links.
- **Employee-loop incentive doctrine — bottom-up loop-closing, made runnable (incl. a smart-contract sketch).**
  How to make every employee discover + close loops (not top-down), verified by an independent referee
  and rewarded on-chain for compounding value, while the CNO builds infra + culture:
  - `skills/core/employee_loop_incentive.md` — the CNO's-game doctrine, mounted on the
    `founding_ai_native_lead` (the CNO-equivalent) via `skill_refs`; `roboforce-titan` org regenerated.
  - `docs/employee-loop-incentive.md` — the Inspire→Verify→Reward→Measure map (each pinned to an
    OpenOPC primitive + the WorkflowX friction sensor) + an 8-check **incentive-design self-assessment**
    (L0 top-down → L4 self-incentivizing org).
  - `contracts/VerifiedClosePayout.sol` — a reference (unaudited) Solidity sketch encoding the three
    non-negotiables in code: **maker ≠ checker** (two signatures), **compounding vesting** (streamed
    payout that re-verification extends), and a **safety-zero gate** (a safety fail zeroes the
    unreleased reward). Reward triggers on a *verified close*, never a proposal.
  - `landing/infographics/incentive-design.html` — the doctrine visualized.
  - `tests/test_employee_loop_incentive.py` — hub link-integrity, scorecard completeness, CNO-only
    skill mount, and the contract's three encoded invariants.
- **One map for the physical-AI pack + a readiness self-assessment (compress · simplify · organize · invent).**
  - `docs/physical-ai.md` — the single entry point, structured **Staff · Operate · Measure**, linking
    every physical-AI doc, skill, preset, and infographic from one place. The canonical flywheel is
    now stated once here; the README routes to the hub instead of duplicating cross-links.
  - **Invented:** a **Physical AI Company Readiness self-assessment** — the operating doctrine turned
    into 8 evidence-gated checks (`no evidence ⇒ No`) → a maturity level (L0 demo shop → L4 industrial
    learning system), with safety as a hard prerequisite. Doctrine, made countable.
    `landing/infographics/physical-ai-readiness.html` visualizes it.
  - `tests/test_physical_ai_docs_hub.py` — link-integrity guard (every relative link in the hub must
    resolve) + scorecard completeness, so the doc map can't silently rot.
- **Physical AI operating-loop doctrine — the "AI-Native Physical AI Company" blueprint made runnable.**
  - `skills/core/physical_ai_operating_loop.md` — a mountable role skill encoding the outer loop
    (company learns about the robot), maker≠checker independent referee → capability scorecard,
    safety-as-a-terminal-gate, the 0→5 release ladder, human-burden ratio, and curated capability
    memory. Mounted on all 11 `physical-ai-robotics-company` roles via `skill_refs`.
  - Preset prompt_refs enriched: `ai_infra_reliability_engineer` is now the independent referee
    (emit a scorecard, safety terminal gate, every fix adds a regression asset);
    `founding_ai_native_lead` promotes capabilities up the release ladder on evidence and tracks
    human-minutes per useful robot-hour. `roboforce-titan` saved org regenerated.
  - `docs/physical-ai-operating-loop.md` — eval + gap analysis mapping each blueprint idea to an
    OpenOPC primitive (present / partial / doctrine-only), plus the honest backlog a real robot
    deployment still needs (episode schema, evaluator wiring, burden instrumentation).
  - `landing/infographics/physical-ai-operating-loop.html` — the doctrine visualized.
  - README restructured toward the anyagent pattern: thesis blockquote, inline nav, and a dated
    `## 📰 News` highlight reel.
  - Tests: `tests/test_physical_ai_robotics_org.py` asserts the doctrine skill mounts on every role
    and the referee/ladder prompts are present.
- **Infographics for the essential docs.** `landing/infographics/` — one visual, self-contained,
  theme-aware one-pager per essential doc (shared `ig.css`): How OpenOPC Works (the three loops +
  work-item state machine), Physical AI Founding Roles (the robot-learning flywheel + 10
  archetypes + RoboForce prototype), Company Metadata Ownership (the WorkItem/Task owner matrix),
  Channels (provider matrix + deny-by-default + bridges), and CLI &amp; Slash Commands. Each honors
  the two-reader standard (plain mental model mapped 1-to-1 to the exact file/type/command) and
  cites its source doc. Linked from the landing page nav/CTA and the README; ships with the Vercel
  deploy. Verified with a headless render of all six pages (CSS applied, zero console/network errors).
- **Vercel landing page + 1-click deploy button.** `landing/index.html` is a self-contained,
  responsive, theme-aware marketing/get-started page (hero, the three loops, the Physical AI
  flywheel, quickstart). A "Deploy to Vercel" button at the top of the README one-click-deploys
  it (`root-directory=landing`). The button and its caption are explicit that it deploys the
  *landing page* only — OpenOPC itself is a stateful local daemon (WebSocket + subprocess-driven
  agents + Playwright + SQLite) that Vercel's serverless/static model cannot host, so the app runs
  locally via `opc ui`. Verified with a headless render (content, images, zero console/network errors).
- **Role-level skill mounting (roadmap #1).** `SkillLibrary.build_skills_summary` now accepts
  `skill_refs`: when a role's `skill_refs` is non-empty, the role is offered only those optional
  skills (empty keeps the prior "offer everything" behaviour; `always`-on skills stay global).
  Wired through all three role-prompt paths — native runtime (`native_agent.py`), the default
  prompt harness (`prompt_harness/builder.py`, which gained a `skill_refs` param), and the
  external-agent path (`engine.py`, resolved via `org_engine.get_role_skill_refs`). Previously
  `skill_refs` was stored and surfaced in the UI but ignored at prompt-build time, so every role
  saw every skill. The `physical-ai-robotics-company` preset roles now mount `physical_ai` (plus
  `coding`/`deployment`/`writing`), and the `roboforce-titan` saved org carries them.
  Tests: `tests/test_skill_refs_scoping.py`.

- **Physical AI / robotics domain pack** — repositions OpenOPC to staff the "founding
  AI-native role" for the Physical AI industry, with RoboForce as the prototype:
  - `opc/market/builtin_presets/physical_ai_robotics_company.yaml` — an industry-generalized
    architecture preset (11 roles centered on a **Founding AI Native Lead**; a work-item DAG that
    models the robot-learning flywheel: teleop data → VLA policy training → sim2real eval →
    perception/manipulation/motion integration → field deployment → fleet telemetry → learning
    feedback). Auto-discovered by `load_architecture_presets_from_yaml`.
  - `.opc/config/company_orgs/org_roboforce-titan_config.yaml` — a saved Company-Mode org
    (schema v2), generated from the preset via the repo's own
    `apply_architecture_preset_to_config` → `build_org_config_payload_from_config` →
    `write_org_config_payload` path so its key ordering aligns with the other org configs.
  - `opc/market/talent_presets.py` — 10 hireable robotics talent templates (`category: robotics`):
    Founding AI Native Lead, Robot Learning Lead, Foundation Model Scientist, Data Engine Engineer,
    Sim2Real Engineer, Perception, Manipulation, Motion Planning, Deployment & Field, AI Infra &
    Reliability.
  - `skills/core/physical_ai.md` — role skill grounding agents in the flywheel, VLA/imitation
    learning, teleop data engines, sim2real, and field deployment.
  - `docs/physical-ai-founding-roles.md` — JD research: RoboForce's verified careers taxonomy plus
    cross-industry role archetypes (Physical Intelligence, Figure, 1X, Apptronik, Agility), with
    verified/inferred flags.
  - `tests/test_physical_ai_robotics_org.py` — covers preset load + final-decider inference, preset
    application, saved-org import, and talent-template presence/uniqueness.
- README: new top-of-file **"Built For Physical AI"** section repositioning the repo around the
  physical-AI-native company story, with OpenOPC framed below as the engine that runs it.

### Changed

- `tests/test_company_org_config_alignment.py` — added `roboforce-titan` (11 roles) to
  `EXPECTED_ROLE_COUNTS` so the new saved org is covered by the structural-alignment invariant.

### Notes

- Verified via `uv run --with pytest python -m pytest` on the new + adjacent org/market suites
  (`test_physical_ai_robotics_org`, `test_company_org_config_alignment`, `test_market`,
  `test_org_saved_crud`, `test_org_config_roundtrip`, `test_org_architecture_snapshot`,
  `test_org_config_import_handler`). The pre-existing `test_quantum_harbor_org_config.py` failures
  are unrelated (they reference an org config file not present in this repo).
