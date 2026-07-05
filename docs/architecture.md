# OpenOPC Architecture — the deep dive

## What it is, in one breath

OpenOPC is a **coordination runtime**, not just an agent launcher. Picture a company with
departments: a front desk that talks to you, a planning team, workers, a supply closet of tools, a
memory, and a floor manager watching it all. OpenOPC splits its code the same way — into **seven
numbered layers** — and one file, [`opc/engine.py`](../opc/engine.py), wires them together.

Why numbered layers? So there is exactly one right place for every kind of code. When you add a
feature, its number tells you where it goes; when you debug, the number tells you where to look.
*(Visual: [architecture infographic](../landing/infographics/architecture.html).)*

## The seven layers

| # | Name | Owns | Where | Size |
|---|---|---|---|---|
| 0 | **Interaction** | how you reach it — CLI, Office UI, message bus, external chat channels | `opc/cli/`, `opc/channels/`, `opc/layer0_interaction/` | 2 + 16 files |
| 1 | **Perception & Context** | reads the brief, routes it, assembles the context an agent needs | `opc/layer1_perception/` | 4 files |
| 2 | **Organization** | Company Mode's brain — the work-item DAG, review/approval gates, comms, escalation, recruitment | `opc/layer2_organization/` | 37 files |
| 3 | **Agent Execution** | the native runtime loop + external-agent adapters, permissions, subagents | `opc/layer3_agent/` | 28 files |
| 4 | **Tools** | what agents can *do* — shell, files, browser, web search, Python, git | `opc/layer4_tools/` | 16 files |
| 5 | **Memory & Evolution** | what the company remembers — markdown memory, compaction, skill library, per-employee experience | `opc/layer5_memory/` | 11 files |
| 6 | **Observability** | who watches — event bus, cost tracking, structured logs, snapshots | `opc/layer6_observability/` | 3 files |

Cross-cutting (used by every layer): `opc/core/` (pydantic models + config), `opc/llm/` (LiteLLM
provider + retry), `opc/database/` (aiosqlite), `opc/market/` (presets/talent/`.opcpkg`),
`opc/plugins/office_ui/` (the React+Phaser web app + its aiohttp/WebSocket server).

## What happens when you send a brief

```
brief (L0) → route: task or company? (L1) → plan a work-item DAG (L2)
  → execute with agents + tools (L3–L4) → gates: review · safety · human (L2)
  → deliver + remember (L5–L6)
```

- **Task Mode** — one agent does the job (the native `runtime_v2` loop, or an external CLI agent via an adapter). Fast, single-player.
- **Company Mode** — an org of role-agents runs the DAG; a manager decomposes, assigns, and reviews across five modes (execute, delegate, review, integrate, rework); blockers escalate to a human. Same engine, more players.

## Key technical decisions — and *why*

These are the choices a contributor most needs to understand before changing anything.

1. **Two modes, one runtime — not two codebases.** Task and Company share the engine, the tools,
   the memory. *Why:* a single agent is just the degenerate case of a one-role company; forking them
   would double the maintenance. `--mode org` is a compatibility alias for "Company Mode with a saved
   org," not a third mode.

2. **Domain packs are *content*, not code.** Targeting a new industry (the Physical AI pack is the
   reference) is a **preset** (`opc/market/builtin_presets/*.yaml`) + **talent templates** + a
   **role skill** + a saved org — *zero engine changes*. *Why:* the engine should be industry-blind;
   verticals compound as data, so anyone can add one without touching runtime code.

3. **Behavior lives in skills + prompts, riding existing primitives.** The physical-AI operating
   doctrine (independent referee, safety-as-a-gate, release ladder) is a mounted role skill, not new
   engine logic — it rides the review gate, the approval gate, and the escalation engine that already
   exist. *Why:* doctrine changes weekly; runtime shouldn't.

4. **Company Mode keeps two records, and the split is load-bearing.** `DelegationWorkItem.metadata`
   owns business/board/review facts; runtime `Task.metadata` owns execution infrastructure
   (sessions, locks, recovery). The executable contract is `layer2_organization/metadata_ownership.py`;
   invariants are enforced by tests. *Why:* mixing them causes cross-layer state desync — a whole
   class of bugs the invariant tests exist to prevent. **Check the matrix before adding a metadata key.**

5. **`skill_refs` scopes skills per role.** A role is offered only the skills its `skill_refs` lists
   (empty = all, backward-compatible; `always`-on skills stay global), enforced in
   `SkillLibrary.build_skills_summary` across all three prompt paths. *Why:* a marketing role
   shouldn't be told about a manipulation skill.

6. **Bring-your-own-agent.** Task/Company can run the native runtime *or* an external CLI agent
   (Codex, Claude Code, Cursor, OpenCode) behind an adapter (`layer3_agent/adapters/`). *Why:* the
   coordination value is independent of which agent does the concrete work.

7. **Runtime state lives outside the repo.** `opc init` copies `config/` + `skills/core/` templates
   into `.opc/` (or `$OPC_HOME`); agents write deliverables to `../OpenOPC_workplace/`. *Why:* the
   repo stays a clean, shareable package; your data and secrets stay yours.

## Conventions that will bite you if you miss them

- **Logging is loguru, not stdlib** — `logger.opt(exception=...)`, never `exc_info=`.
- **Tests must not touch real state** — use `tests/_temp_paths.py`, never write to `.opc/`.
- **LLM token limits are centralized** in `opc/llm/provider.py` (context via litellm `max_input_tokens`, 128k fallback; `max_tokens` clamped to the model's output cap). Don't hand-roll them elsewhere.
- **Channel inbound is deny-by-default** — `allow_from` opts senders in; an empty list denies all.

## Where to go next

- **Contribute:** [`CONTRIBUTING.md`](../CONTRIBUTING.md) — recipes for adding a domain pack, a tool, a channel, or an adapter.
- **The Physical AI pack** (the reference domain pack): [`docs/physical-ai.md`](physical-ai.md).
- **The agent guide** (rules for AI contributors): [`CLAUDE.md`](../CLAUDE.md).
