# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

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
