"""Git operation tools."""

from __future__ import annotations

import shlex
from typing import Any

from opc.layer4_tools.shell import shell_exec
from opc.layer4_tools.registry import COMPANY_EFFECT_FORBIDDEN, ToolDefinition


async def git_status(working_directory: str = ".") -> dict[str, Any]:
    return await shell_exec("git status --porcelain && echo '---' && git log --oneline -5", working_directory=working_directory)


async def git_commit(message: str, working_directory: str = ".", add_all: bool = True) -> dict[str, Any]:
    cmds = []
    if add_all:
        cmds.append("git add -A")
    # The message flows through ``shell_exec`` -> ``bash -lc "<command>"``, so it is
    # shell-interpolated. Quote it to prevent a crafted message (e.g.
    # ``foo" && rm -rf / #``) from injecting arbitrary commands. Only the literal
    # commit message must reach ``git commit``.
    cmds.append(f"git commit -m {shlex.quote(str(message))}")
    return await shell_exec(" && ".join(cmds), working_directory=working_directory)


async def git_diff(working_directory: str = ".", staged: bool = False) -> dict[str, Any]:
    cmd = "git diff --staged" if staged else "git diff"
    return await shell_exec(cmd, working_directory=working_directory)


async def git_clone(url: str, directory: str = ".") -> dict[str, Any]:
    # Quote the URL: it is interpolated into a ``bash -lc`` command and a value like
    # ``https://x.git; rm -rf /`` or ``$(curl ...)`` would otherwise be executed.
    return await shell_exec(f"git clone {shlex.quote(str(url))}", working_directory=directory, timeout=300)


def create_git_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="git_status",
            description="Show git status and recent commits.",
            parameters={
                "type": "object",
                "properties": {
                    "working_directory": {"type": "string", "description": "Git repo directory", "default": "."},
                },
                "required": [],
            },
            func=git_status,
            category="code",
            company_effect_kind=COMPANY_EFFECT_FORBIDDEN,
            permission_effects=["local_read", "process_execute"],
        ),
        ToolDefinition(
            name="git_commit",
            description="Stage all changes and commit with a message.",
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Commit message"},
                    "working_directory": {"type": "string", "description": "Git repo directory", "default": "."},
                    "add_all": {"type": "boolean", "description": "Stage all changes", "default": True},
                },
                "required": ["message"],
            },
            func=git_commit,
            category="code",
            requires_confirmation=True,
            company_effect_kind=COMPANY_EFFECT_FORBIDDEN,
            permission_effects=["process_execute", "workspace_write"],
        ),
        ToolDefinition(
            name="git_diff",
            description="Show git diff (staged or unstaged).",
            parameters={
                "type": "object",
                "properties": {
                    "working_directory": {"type": "string", "description": "Git repo directory", "default": "."},
                    "staged": {"type": "boolean", "description": "Show staged diff", "default": False},
                },
                "required": [],
            },
            func=git_diff,
            category="code",
            company_effect_kind=COMPANY_EFFECT_FORBIDDEN,
            permission_effects=["local_read", "process_execute"],
        ),
    ]
