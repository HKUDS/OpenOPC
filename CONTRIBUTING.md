# Contributing to OpenOPC

New here? Read this once and you can ship a real change. If you remember one thing: **the code is
split into seven numbered layers, and your change goes in the layer that owns the concern** — see
[`docs/architecture.md`](docs/architecture.md) (and its [infographic](landing/infographics/architecture.html))
for the map. Below are copy-paste recipes for the most common contributions.

## Setup (about a minute)

```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e .
uv run python -m playwright install chromium      # for browser tools
uv run opc init                                    # local config; add --no-external-agent-preflight if you lack codex/claude CLIs
```

Run the tests (pytest is **not** a declared dependency — add it with `--with`, and use `uv`, since
bare `python3` lacks the runtime deps):

```bash
uv run --with pytest python -m pytest                 # full suite
uv run --with pytest python -m pytest tests/test_market.py -q   # one file
```

> Heads-up: `main` currently carries ~24 pre-existing failures unrelated to most changes (live-LLM
> integration tests, a few architectural lints). Before blaming your change, diff the failing set
> against a clean checkout — if it's the same set, you're green.

## Recipe 1 — add a new industry org (a "domain pack")

The highest-leverage contribution, and it needs **zero engine changes**. Copy the Physical AI pack:

1. **Preset** — `opc/market/builtin_presets/<your_industry>.yaml` (an `ArchitectureBlueprint`: roles + a work-item DAG). Auto-discovered by `load_architecture_presets_from_yaml`. *Gotcha:* any `prompt_refs` line with a colon must be quoted, or YAML parses it as a mapping.
2. **Talent** — add `category: <industry>` templates to `opc/market/talent_presets.py`.
3. **Role skill(s)** — `skills/core/<domain>.md`; mount on roles via each role's `skill_refs`.
4. **Saved org** — generate it *via the repo's own path* (never hand-write): `apply_architecture_preset_to_config` → `build_org_config_payload_from_config` → `write_org_config_payload`. Add its role count to `tests/test_company_org_config_alignment.py::EXPECTED_ROLE_COUNTS`.
5. **A hub doc + infographic** — see `docs/physical-ai.md` and `landing/infographics/` for the pattern.

## Recipe 2 — add a tool agents can call

1. Write it in `opc/layer4_tools/` as a `ToolDefinition`.
2. Register it in `opc/layer4_tools/registry.py`.
3. Risk-classify it: destructive tools escalate; safe ones are allowlisted (`system_config.yaml → autonomy`).

## Recipe 3 — add a chat channel (Slack-like)

1. New provider in `opc/channels/`, registered in `provider_registry.py`.
2. Add its optional dependency as a `channels-<name>` extra in `pyproject.toml` — and **do not import the SDK at module top level** outside the provider that owns it.
3. Keep inbound **deny-by-default**: `allow_from` opts senders in; an empty list denies all.

## Recipe 4 — add an external-agent adapter

New adapter in `opc/layer3_agent/adapters/`, registered in `adapters/registry.py` (model this on `codex_adapter.py`).

## The non-negotiables (a reviewer will flag these)

- **Docs ship with the change.** A behavior/API/config change updates `CHANGELOG.md`, the README (or `docs/**`), and the agent guide `CLAUDE.md` — in the *same* PR. When a design decision is non-obvious, add a diagram/infographic, don't just describe it.
- **Loguru, not stdlib logging** — `logger.opt(exception=...)`.
- **Tests never touch real state** — use `tests/_temp_paths.py`.
- **Metadata ownership** — before adding a Company-Mode metadata key, check the matrix in `opc/layer2_organization/metadata_ownership.py`; the wrong record fails invariant tests.
- **Verify at the right altitude** — for a runtime change, drive the actual flow (`opc ui` / `opc chat`), not just unit tests.

## Commit & PR

Conventional Commits (`feat(market): …`, `fix(llm): …`, `docs: …`). Branch off `main`, keep the PR focused, and state how you verified it. Full agent-facing rules: [`CLAUDE.md`](CLAUDE.md).
