# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

OpenOPC ("One-Person Company") — a Python system that assembles an AI company around a goal: it staffs an org of role-agents (Self-Built), orchestrates them through a work-item state machine (Self-Run), and distills runs into per-role experience and shared playbooks (Self-Grown). The installable package is `opc` (CLI entry point `opc = opc.cli.app:main`); the repo directory name (`physical-ai-native`) is unrelated to the package name.

## Commands

```bash
# Setup (uv recommended; plain pip works too)
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e .
uv run python -m playwright install chromium   # needed for browser tools
uv run opc init                                 # creates .opc/ config/memory/projects
# add --no-external-agent-preflight if codex/claude/cursor CLIs aren't installed

# Tests (pytest, stdlib unittest-style classes; no pytest.ini — defaults apply)
# NOTE: pytest is NOT a declared dependency and bare `python3` lacks the runtime deps.
# Use uv, and add pytest with --with:
uv run --with pytest python -m pytest                # full suite (tests/)
uv run --with pytest python -m pytest tests/test_task_router.py -q          # one file
uv run --with pytest python -m pytest tests/test_cli_app.py::CliInitProjectTests::test_external_agent_preflight_accepts_fake_agent_binaries
uv run --with pytest python -m pytest opc/plugins/office_ui/tests -q         # Office UI backend tests live in the plugin, not tests/
# (test_quantum_harbor_org_config.py fails on a clean tree — it references an org config file absent from this repo.)

# Office UI frontend (React 19 + Phaser + Vite, TypeScript)
cd opc/plugins/office_ui/frontend_src
npm install && npm run typecheck && npm run build   # build output → ../frontend_dist/ (served by opc ui)

# Run
opc ui                                              # browser UI at http://localhost:8765
opc chat -p demo --mode task --agent native "..."   # single-agent Task Mode
opc chat -p demo --mode company --company-profile corporate "..."   # Company Mode
opc exec -p demo --mode task --agent native --json "..."            # non-interactive / CI

# Stuck persisted task state
python scripts/reset_stuck_task.py --project <project> --session <id> --apply
```

CI (`.github/workflows/external-agent-smoke.yml`) runs only the external-agent preflight tests on ubuntu/macos/windows — the full suite is a local responsibility.

## Architecture

`opc/engine.py` is the central orchestrator wiring numbered layers together. The layer split is load-bearing — put new code in the layer that owns the concern:

| Layer | Package | Owns |
|---|---|---|
| 0 Interaction | `opc/layer0_interaction/`, `opc/cli/`, `opc/channels/` | message bus, Typer CLI, messaging providers (Telegram/Slack/Discord/Feishu/… behind `opc/channels/provider_registry.py`) |
| 1 Perception | `opc/layer1_perception/` | context loading/assembly, task routing |
| 2 Organization | `opc/layer2_organization/` | Company Mode: work-item planner + phase state machine (`phase.py`, `work_item_transition.py`), `company_runtime.py` (persistent role sessions), comms, escalation, approval, recruiter, reorg, secretary |
| 3 Agent execution | `opc/layer3_agent/` | native runtime (`runtime_v2/runtime.py`: streaming LLM loop, tool executor, subagents, permissions, git worktrees), external agent adapters (`adapters/`: claude_code, codex, cursor, opencode), `prompt_harness/` (prompt assembly + runtime artifacts) |
| 4 Tools | `opc/layer4_tools/` | shell, file ops, Playwright browser, git, python exec, web search, collaboration RPC; all registered via `registry.py` |
| 5 Memory | `opc/layer5_memory/` | markdown memory, history compaction, skill library/importer, employee evolution, approval allowlist |
| 6 Observability | `opc/layer6_observability/` | cost tracking, loguru-based logging |

Cross-cutting: `opc/core/` (config, pydantic models in `models.py`, events), `opc/llm/` (LiteLLM provider + retry), `opc/database/store.py` (aiosqlite persistence), `opc/market/` (architecture presets, talent templates, `.opcpkg` packages), `opc/plugins/office_ui/` (aiohttp server + WebSocket handler + built frontend).

### Domain packs (org architectures)

A "domain pack" is how OpenOPC targets an industry without engine changes: a built-in architecture preset (structure only) + hireable talent templates + a role skill + a saved org instance. The reference example is the **Physical AI / robotics** pack — the repo is positioned around staffing the "founding AI-native role" for that industry (see `docs/physical-ai-founding-roles.md`):

- Preset: `opc/market/builtin_presets/physical_ai_robotics_company.yaml` (YAML `ArchitectureBlueprint`, auto-discovered by `load_architecture_presets_from_yaml`; operational config is *inferred* at install-time by `infer_collaboration_config`, so presets stay pure structure). **Gotcha:** any `prompt_refs` string containing a colon must be quoted, or YAML parses it as a mapping and `RoleConfig` validation fails.
- Talent: `opc/market/talent_presets.py` (`BUILTIN_TALENT_TEMPLATES`, `category: robotics`); skill: `skills/core/physical_ai.md`.
- **Role-level skill mounting**: a role's `skill_refs` scopes which optional skills it is offered at prompt-build time (empty = all, backward-compatible; `always`-on skills are always global). Enforced in `SkillLibrary.build_skills_summary(..., skill_refs=...)` and passed from all three role-prompt paths — `native_agent.py`, `prompt_harness/builder.py` (has a `skill_refs` ctor param), and `engine.py` (via `org_engine.get_role_skill_refs`). If you add a new call site that builds a role's prompt, pass its `skill_refs` too, or that role silently sees the whole library.
- **Operating doctrine as content, not code**: the physical-AI pack's *behavior* (independent referee, safety-as-a-terminal-gate, 0→5 release ladder, human-burden ratio) lives in a mounted role skill (`skills/core/physical_ai_operating_loop.md`) and preset `prompt_refs`, not in engine logic — it rides the existing gate/escalation/memory primitives. The eval/gap map (`docs/physical-ai-operating-loop.md`) marks what's an engine primitive vs doctrine-only; don't mistake the doctrine for a shipped sensor.
- Saved org: `.opc/config/company_orgs/org_roboforce-titan_config.yaml`. **Generate saved orgs from a preset via the repo's own path** — `apply_architecture_preset_to_config` (use `strategy="overwrite"` for unprefixed role ids) → `build_org_config_payload_from_config` → `write_org_config_payload`. Hand-writing risks breaking `test_company_org_config_alignment.py`, which requires every `org_*_config.yaml` to share identical top-level key ordering; new orgs must also be added to that test's `EXPECTED_ROLE_COUNTS`.

### Execution modes

Two conceptual modes: **task** (one agent — native runtime_v2 or an external CLI agent via an adapter) and **company** (an org of roles executing a work-item dependency DAG with manager decompose/assign/review, kanban projection, and human escalation). Some CLI/service commands still accept `--mode org` as a compatibility selector meaning "company mode with a saved org architecture" — don't design new features around `org` as a distinct mode.

### Metadata ownership contract (Company Mode)

`DelegationWorkItem.metadata` owns business/board/review/user-visible facts; runtime `Task.metadata` owns execution infrastructure (sessions, locks, recovery, `runtime_v2` state); a limited set of routing fields may exist on Task as immutable `execution_copy`. The executable owner matrix is `opc/layer2_organization/metadata_ownership.py` (human summary: `docs/company-metadata-ownership.md`); invariants are enforced by `work_item_runtime_invariants.py` and many tests. WorkItem↔Task linkage lives in the `work_item_runtime_links` table, never in metadata. Putting a field on the wrong record will fail invariant tests — check the matrix before adding metadata keys.

## Conventions and gotchas

- **Runtime state vs repo**: `opc init` copies `config/` templates into `.opc/` (or `$OPC_HOME`); agents write deliverables to `../OpenOPC_workplace/<project>/`, with company comms under its `.opc-comms/`. Template changes belong in repo-root `config/` and `skills/core/` — hatch force-includes them into the wheel as `opc/config_templates` and `opc/skills_assets/core`.
- **Tests must not touch real state**: use the helpers in `tests/_temp_paths.py` (workspace temp dirs under `.tmp-test/`) rather than writing to `.opc/` or wall-clock-derived paths.
- **Logging**: loguru, not stdlib logging — use `logger.opt(exception=...)`, not `exc_info=` kwargs.
- **LLM limits**: context window resolves via litellm `max_input_tokens` with a 128k fallback; `max_tokens` is clamped to the model's output limit per call (`opc/llm/provider.py`). Don't hand-roll token limits elsewhere.
- **Channel security**: inbound sender allowlists (`allow_from`) are deny-by-default; keep new providers consistent with that.
- Optional dependencies are per-channel extras (`.[channels-feishu]`, `.[all]`); core code must not import channel SDKs at module top level outside the provider that owns them.
