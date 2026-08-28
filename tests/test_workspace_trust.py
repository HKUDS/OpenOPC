from __future__ import annotations

import asyncio
import io
import json
import os
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
    save_explicit_workspace_authority_change,
)
from opc.engine import OPCEngine


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


def _trust_project(workspace: Path, config_dir: Path) -> OPCConfig:
    config = OPCConfig.load(config_dir, trusted_source=True)
    WorkspaceTrustStore().trust(workspace, config_dir, config)
    return config


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

    reference = _trust_project(workspace, config_dir)
    loaded = OPCConfig.load(config_dir)

    assert loaded.model_dump() == reference.model_dump()
    assert loaded.system.mcp_servers[0].command == ["python", "project_mcp.py"]
    assert loaded.llm.api_base == "https://llm.example.test/v1"
    assert loaded.llm.api_key_env == "PROJECT_SELECTED_KEY"


def test_explicit_permission_save_refreshes_existing_workspace_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    config_dir = _write_project_config(workspace)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.chdir(workspace)
    _trust_project(workspace, config_dir)
    config = OPCConfig.load(config_dir)

    config.autonomy.native_approval_level = "full-access"
    saved_workspace = save_explicit_workspace_authority_change(
        config,
        config_dir,
    )

    assert saved_workspace == canonical_workspace(workspace)
    assert OPCConfig.load(config_dir).autonomy.native_approval_level == "full-access"


def test_explicit_permission_save_rolls_back_if_trust_store_update_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    config_dir = _write_project_config(workspace)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.chdir(workspace)
    original = _trust_project(workspace, config_dir)
    before = (config_dir / "system_config.yaml").read_bytes()
    config = OPCConfig.load(config_dir)
    config.autonomy.native_approval_level = "full-access"

    with patch.object(WorkspaceTrustStore, "trust", side_effect=OSError("read-only trust store")):
        with pytest.raises(OSError, match="read-only trust store"):
            save_explicit_workspace_authority_change(config, config_dir)

    assert (config_dir / "system_config.yaml").read_bytes() == before
    assert OPCConfig.load(config_dir).model_dump() == original.model_dump()


@pytest.mark.parametrize(
    ("filename", "replacement"),
    [
        (
            "system_config.yaml",
            "system: {}\nmcp_servers:\n"
            "  - name: changed\n"
            "    type: local\n"
            "    command: [python, changed.py]\n",
        ),
        (
            "llm_config.yaml",
            "llm:\n"
            "  default_model: openai/test-model\n"
            "  api_base: https://changed.example.test/v1\n"
            "  api_key_env: CHANGED_KEY\n",
        ),
    ],
)
def test_authority_source_change_fails_before_yaml_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    replacement: str,
) -> None:
    workspace = tmp_path / "project"
    config_dir = _write_project_config(workspace)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    _trust_project(workspace, config_dir)
    (config_dir / filename).write_text(replacement, encoding="utf-8")

    with patch("opc.core.config.yaml.safe_load") as safe_load:
        with pytest.raises(WorkspaceTrustRequired) as raised:
            OPCConfig.load(config_dir)

    safe_load.assert_not_called()
    assert raised.value.reason == "source_changed"


def test_effective_authority_change_fails_before_runtime_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    config_dir = _write_project_config(workspace)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    _trust_project(workspace, config_dir)
    loaded = OPCConfig.load(config_dir)
    loaded.llm.api_base = "https://in-memory-change.example.test/v1"

    with pytest.raises(WorkspaceTrustRequired) as raised:
        WorkspaceTrustStore().require(workspace, config_dir, loaded)

    assert raised.value.reason == "authority_changed"


def test_engine_rechecks_bound_authority_before_initialization_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    config_dir = _write_project_config(workspace)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    _trust_project(workspace, config_dir)
    loaded = OPCConfig.load(config_dir)
    (config_dir / "llm_config.yaml").write_text(
        "llm:\n"
        "  default_model: openai/test-model\n"
        "  api_base: https://changed-before-engine.example.test/v1\n"
        "  api_key_env: CHANGED_KEY\n",
        encoding="utf-8",
    )
    runtime_home = tmp_path / "runtime-home"
    engine = OPCEngine(config=loaded, opc_home=runtime_home)

    with pytest.raises(WorkspaceTrustRequired) as raised:
        asyncio.run(engine.initialize())

    assert raised.value.reason == "source_changed"
    assert not runtime_home.exists()


def test_linked_authority_target_change_invalidates_effective_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    config_dir = _write_project_config(workspace)
    target = tmp_path / "linked-llm.yaml"
    target.write_text((config_dir / "llm_config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (config_dir / "llm_config.yaml").unlink()
    try:
        (config_dir / "llm_config.yaml").symlink_to(target)
    except OSError:
        pytest.skip("file symlinks are unavailable")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    _trust_project(workspace, config_dir)
    target.write_text(
        "llm:\n"
        "  default_model: openai/test-model\n"
        "  api_base: https://changed.example.test/v1\n"
        "  api_key_env: CHANGED_KEY\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceTrustRequired) as raised:
        OPCConfig.load(config_dir)

    assert raised.value.reason == "authority_changed"


def test_legacy_path_only_record_requires_fingerprint_renewal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    config_dir = _write_project_config(workspace)
    trust_path = tmp_path / "user-config" / "openopc" / "trusted_workspaces.json"
    trust_path.parent.mkdir(parents=True)
    trust_path.write_text(
        json.dumps({"version": 1, "trusted_workspaces": [str(canonical_workspace(workspace))]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))

    with pytest.raises(WorkspaceTrustRequired) as raised:
        OPCConfig.load(config_dir)

    assert raised.value.reason == "legacy_record"


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
    config_dir = _write_project_config(workspace)
    config = OPCConfig.load(config_dir, trusted_source=True)
    alias = tmp_path / "workspace-alias"
    try:
        alias.symlink_to(workspace, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    store = WorkspaceTrustStore(tmp_path / "user-config" / "trusted.json")
    store.trust(alias, config_dir, config)

    assert store.is_trusted(workspace)
    assert store.list_trusted() == [canonical_workspace(workspace)]
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert len(payload["trusted_workspaces"]) == 1
    entry = payload["trusted_workspaces"][0]
    assert entry["workspace"] == str(canonical_workspace(workspace))
    assert entry["source_fingerprint"].startswith("sha256:")
    assert entry["authority_fingerprint"].startswith("sha256:")
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
                "version": 2,
                "trusted_workspaces": [
                    {
                        "workspace": str(trusted),
                        "source_fingerprint": "sha256:source",
                        "authority_fingerprint": "sha256:authority",
                    },
                    {
                        "workspace": "relative/workspace",
                        "source_fingerprint": "sha256:source",
                        "authority_fingerprint": "sha256:authority",
                    },
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


def test_trust_cli_add_renews_changed_authority_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    config_dir = _write_project_config(workspace)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    _trust_project(workspace, config_dir)
    (config_dir / "llm_config.yaml").write_text(
        "llm:\n"
        "  default_model: openai/test-model\n"
        "  api_base: https://renewed.example.test/v1\n"
        "  api_key_env: RENEWED_KEY\n",
        encoding="utf-8",
    )
    runner = CliRunner()

    renewed = runner.invoke(app, ["trust", "add", str(workspace)])
    loaded = OPCConfig.load(config_dir)

    assert renewed.exit_code == 0, renewed.output
    assert loaded.llm.api_base == "https://renewed.example.test/v1"
    assert loaded.llm.api_key_env == "RENEWED_KEY"


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
    assert OPCConfig.load(workspace / ".opc" / "config").system.log_level == "INFO"


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
