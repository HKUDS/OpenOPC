"""CLI board plugin entrypoint."""

from __future__ import annotations

from typing import Optional


def register_cli(parent_app) -> None:
    """Register the `opc board` command on the parent Typer app."""
    import typer as _typer

    @parent_app.command("board")
    def board(
        project: Optional[str] = _typer.Option(None, "--project", "-p", help="Project ID"),
        session: Optional[str] = _typer.Option(None, "--session", help="Initial session id to inspect"),
        view: str = _typer.Option("kanban", "--view", help="Initial view: kanban, list, focus, pipeline, org, work-item, role, logs"),
        work_item: Optional[str] = _typer.Option(None, "--work-item", help="Initial work item id to inspect"),
        role: Optional[str] = _typer.Option(None, "--role", help="Initial role id to inspect"),
        target: Optional[str] = _typer.Option(None, "--target", help="Initial log/runtime target to inspect"),
        attach: bool = _typer.Option(False, "--attach", help="Run as a temporary inspector attached from opc chat"),
        readonly: bool = _typer.Option(False, "--readonly", help="Disable mutating board actions"),
        refresh_interval: float = _typer.Option(
            2.0,
            "--refresh-interval",
            min=0.5,
            help="Cross-process reconcile interval in seconds",
        ),
    ) -> None:
        """Launch the OpenOPC terminal Kanban board."""
        # The board initializes its engine lazily.  Resolve workspace trust at
        # the CLI boundary first so an untrusted project never reaches the TUI.
        from opc.cli.app import _get_config
        from opc.plugins.cli_board.entry import launch_board

        _get_config()
        launch_board(
            project_id=project,
            refresh_interval=refresh_interval,
            attach=attach,
            readonly=readonly,
            initial_view=view,
            initial_session_id=session,
            initial_work_item_id=work_item,
            initial_role_id=role,
            initial_target=target,
        )
