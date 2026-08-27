# Contributing to OpenOPC

Thanks for your interest in improving OpenOPC! This guide covers how to set up, make changes, and submit them.

## Ways to contribute
- **Report bugs** and **request features** via the [issue templates](.github/ISSUE_TEMPLATE).
- **Improve docs**, fix typos, add examples.
- **Submit code** — bug fixes, new features, talent templates, channel providers.
- **Discuss** in the Feishu / WeChat community groups linked in the [README](README.md).

## Development setup
OpenOPC requires **Python >= 3.10** (3.12 recommended). [`uv`](https://docs.astral.sh/uv/) is the recommended tool.

```bash
git clone https://github.com/<your-username>/OpenOPC
cd OpenOPC
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
uv run opc init
```

Run the UI with `opc ui` (needs Node.js >= 18 for the Office UI) or the CLI with `opc chat`.

## Making changes
1. **Fork** the repo and create a branch: `git checkout -b my-change`.
2. Keep changes **focused** — one logical change per PR.
3. Match the existing code style; add type hints where the surrounding code uses them.
4. Update docs/README when you change user-facing behavior.

## Testing
```bash
python -m pytest
```
The `external-agent-smoke` GitHub workflow also runs on pull requests.

## Submitting a pull request
1. Push your branch to your fork.
2. Open a PR against `main` using the pull request template.
3. Link the related issue (e.g. `Closes #123`).
4. Make sure CI passes and respond to review feedback.

## Code of Conduct
By participating, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).
