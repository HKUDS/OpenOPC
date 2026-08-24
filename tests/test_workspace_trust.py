from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from opc.cli.app import _get_config, app
from opc.core.config import OPCConfig
from opc.core.workspace_trust import (
    WorkspaceTrustRequired,
    WorkspaceTrustStore,
    canonical_workspace,
)


def _write_project_config(workspace: Path) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "pyproject.toml").write_text(
        "[project]\nname = 'trust-probe'\nversion = '0'\n",
        encoding="utf-8",
    )
    config_dir = workspace / ".opc" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "system_config.yaml").write_text(
        "system:\n"
        "  log_level: DEBUG\n"
        "mcp_servers:\n"
        "  - name: local-probe\n"
        "    type: local\n"
        "    command: [python, project_mcp.py]\n"
        "    enabled: true\n"
        "autonomy: {}\n"
        "capabilities: {}\n",
        encoding="utf-8",
    )
    (config_dir / "llm_config.yaml").write_text(
        "llm:\n"
        "  default_model: openai/test-model\n"
        "  api_base: https://llm.example.test/v1\n"
        "  api_key_env: PROJECT_SELECTED_KEY\n",
        encoding="utf-8",
    )
    return config_dir


def test_untrusted_project_config_stops_before_parse_or_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    config_dir = _write_project_config(workspace)
    agent_config = config_dir / "agent_config.yaml"
    agent_config.write_text(
        "external_agents:\n  codex:\n    approval_mode: delegate\n",
        encoding="utf-8",
    )
    original = agent_config.read_bytes()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))

    with patch("opc.core.config.yaml.safe_load") as safe_load:
        with pytest.raises(WorkspaceTrustRequired) as raised:
            OPCConfig.load(config_dir)

    safe_load.assert_not_called()
    assert raised.value.workspace == canonical_workspace(workspace)
    assert agent_config.read_bytes() == original
    assert not (tmp_path / "user-config" / "openopc" / "trusted_workspaces.json").exists()


def test_trusted_project_loads_without_changing_effective_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    config_dir = _write_project_config(workspace)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))

    reference = OPCConfig.load(config_dir, trusted_source=True)
    WorkspaceTrustStore().trust(workspace)
    loaded = OPCConfig.load(config_dir)

    assert loaded.model_dump() == reference.model_dump()
    assert loaded.system.mcp_servers[0].command == ["python", "project_mcp.py"]
    assert loaded.llm.api_base == "https://llm.example.test/v1"
    assert loaded.llm.api_key_env == "PROJECT_SELECTED_KEY"


def test_non_project_user_dot_opc_is_not_misclassified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "private-home" / ".opc" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "llm_config.yaml").write_text(
        "llm:\n  default_model: private/model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))

    loaded = OPCConfig.load(config_dir)

    assert loaded.llm.default_model == "private/model"


def test_trust_store_canonicalizes_paths_and_uses_private_permissions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "workspace-alias"
    try:
        alias.symlink_to(workspace, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    store = WorkspaceTrustStore(tmp_path / "user-config" / "trusted.json")
    store.trust(alias)

    assert store.is_trusted(workspace)
    assert store.list_trusted() == [canonical_workspace(workspace)]
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["trusted_workspaces"] == [str(canonical_workspace(workspace))]
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o777 == 0o600


def test_trust_store_ignores_relative_and_malformed_entries(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    store = WorkspaceTrustStore(tmp_path / "user-config" / "trusted.json")
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "trusted_workspaces": [
                    str(trusted),
                    "relative/workspace",
                    "",
                    None,
                ],
            }
        ),
        encoding="utf-8",
    )

    assert store.list_trusted() == [canonical_workspace(trusted)]


def test_cli_prompt_grants_trust_then_loads_unchanged_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    config_dir = _write_project_config(workspace)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    output = io.StringIO()

    with patch("opc.cli.app.get_opc_home", return_value=config_dir.parent), patch(
        "opc.cli.app.sys.stdin.isatty",
        return_value=True,
    ), patch("opc.cli.app.typer.confirm", return_value=True), patch(
        "opc.cli.app.console",
        Console(file=output, force_terminal=False),
    ):
        loaded = _get_config()

    assert WorkspaceTrustStore().is_trusted(workspace)
    assert loaded.system.mcp_servers[0].name == "local-probe"
    assert "Trusted workspace" in output.getvalue()


def test_cli_noninteractive_load_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    config_dir = _write_project_config(workspace)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))

    with patch("opc.cli.app.get_opc_home", return_value=config_dir.parent), patch(
        "opc.cli.app.sys.stdin.isatty",
        return_value=False,
    ):
        with pytest.raises(typer.Exit) as raised:
            _get_config()

    assert raised.value.exit_code == 2
    assert not WorkspaceTrustStore().is_trusted(workspace)


def test_trust_cli_add_list_and_remove(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    _write_project_config(workspace)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    runner = CliRunner()

    added = runner.invoke(app, ["trust", "add", str(workspace)])
    listed = runner.invoke(app, ["trust", "list"])
    trusted_after_add = WorkspaceTrustStore().is_trusted(workspace)
    removed = runner.invoke(app, ["trust", "remove", str(workspace)])

    assert added.exit_code == 0, added.output
    assert trusted_after_add
    assert str(canonical_workspace(workspace)) in listed.output.replace("\n", "")
    assert removed.exit_code == 0, removed.output
    assert not WorkspaceTrustStore().is_trusted(workspace)


def test_init_auto_trusts_only_newly_created_project_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "new-project"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        "[project]\nname = 'new-project'\nversion = '0'\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["init", "--no-external-agent-preflight", "--no-trust-external-agents"],
    )

    assert result.exit_code == 0, result.output
    assert (workspace / ".opc" / "config" / "system_config.yaml").is_file()
    assert WorkspaceTrustStore().is_trusted(workspace)


def test_init_does_not_auto_trust_preexisting_empty_config_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "existing-project"
    config_dir = workspace / ".opc" / "config"
    config_dir.mkdir(parents=True)
    (workspace / "pyproject.toml").write_text(
        "[project]\nname = 'existing-project'\nversion = '0'\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["init", "--no-external-agent-preflight", "--no-trust-external-agents"],
    )

    assert result.exit_code == 0, result.output
    assert (config_dir / "system_config.yaml").is_file()
    assert not WorkspaceTrustStore().is_trusted(workspace)
