"""Textual application for the OpenOPC CLI board."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header

from opc.plugins.cli_board.state.store import BoardStateStore
from opc.plugins.cli_board.tui.screens.help import HelpScreen
from opc.plugins.cli_board.tui.screens.palette import CommandPaletteScreen, PaletteCommand
from opc.plugins.cli_board.tui.screens.prompt import PromptField, PromptScreen
from opc.plugins.cli_board.widgets.activity_pane import ActivityPaneWidget
from opc.plugins.cli_board.widgets.context_tabs import ContextTabsWidget
from opc.plugins.cli_board.widgets.detail_pane import DetailPaneWidget
from opc.plugins.cli_board.widgets.focus_view import FocusTaskWidget
from opc.plugins.cli_board.widgets.kanban_board import KanbanBoardWidget
from opc.plugins.cli_board.widgets.metrics_bar import MetricsBarWidget
from opc.plugins.cli_board.widgets.org_viewer import OrgViewerWidget
from opc.plugins.cli_board.widgets.pipeline_view import PipelineViewWidget
from opc.plugins.cli_board.widgets.session_pane import SessionPaneWidget
from opc.plugins.cli_board.widgets.session_sidebar import SessionSidebarWidget
from opc.plugins.cli_board.widgets.status_bar import StatusBarWidget
from opc.plugins.cli_board.widgets.task_list import TaskListWidget
from opc.plugins.office_ui.services.factory import OfficeServiceFactory

if TYPE_CHECKING:
    from opc.plugins.cli_board.services.actions import BoardActions
    from opc.plugins.cli_board.services.board_repository import BoardRepository
    from opc.plugins.cli_board.services.engine_facade import EngineFacade
    from opc.plugins.cli_board.services.event_bridge import CliBoardEventBridge
    from opc.plugins.cli_board.services.reconcile import ReconcileLoop


class CliBoardApp(App[None]):
    """Terminal command center for OpenOPC."""

    CSS_PATH = "board.tcss"

    BINDINGS = [
        Binding("left", "move_left", "Left", show=False),
        Binding("h", "move_left", "Left", show=False),
        Binding("right", "move_right", "Right", show=False),
        Binding("l", "move_right", "Right", show=False),
        Binding("up", "move_up", "Up", show=False),
        Binding("k", "move_up", "Up", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("j", "move_down", "Down", show=False),
        Binding("tab", "focus_next_pane", "Next Pane", priority=True),
        Binding("shift+tab", "focus_prev_pane", "Prev Pane", show=False, priority=True),
        Binding("enter", "activate_selection", "Open"),
        Binding("space", "toggle_density", "Density"),
        Binding("1", "view_kanban", "Kanban"),
        Binding("2", "view_list", "List"),
        Binding("3", "view_focus", "Focus"),
        Binding("4", "view_pipeline", "Pipeline"),
        Binding("5", "view_org", "Org"),
        Binding("ctrl+k", "open_palette", "Palette", priority=True),
        Binding(":", "open_palette", "Palette", show=False, priority=True),
        Binding("n", "new_task", "New"),
        Binding("g", "run_selected", "Run"),
        Binding("s", "reply", "Reply"),
        Binding("m", "move_task", "Move"),
        Binding("a", "approve_selected", "Approve"),
        Binding("d", "deny_selected", "Deny"),
        Binding("c", "mark_done", "Done"),
        Binding("x", "cancel_task", "Cancel"),
        Binding("t", "retry_selected", "Retry"),
        Binding("e", "checkpoint_feedback", "Feedback"),
        Binding("R", "rename_session", "Rename", show=False),
        Binding("D", "delete_session", "Delete", show=False),
        Binding("E", "switch_mode", "Mode", show=False),
        Binding("f", "toggle_done", "Toggle Done"),
        Binding("/", "search", "Search"),
        Binding("r", "refresh_board", "Refresh"),
        Binding("?", "show_help", "Help"),
        Binding("q", "quit_board", "Quit"),
        Binding("ctrl+q", "quit_board", "Return", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        project_id: str | None = None,
        refresh_interval: float = 2.0,
        bootstrap_services: bool = True,
        attach: bool = False,
        readonly: bool = False,
        initial_view: str = "kanban",
        initial_session_id: str | None = None,
        initial_work_item_id: str | None = None,
        initial_role_id: str | None = None,
        initial_target: str | None = None,
    ) -> None:
        super().__init__()
        self.project_id = project_id
        self.refresh_interval = refresh_interval
        self.attach = attach
        self.readonly = readonly
        self.initial_view = initial_view
        self.initial_session_id = initial_session_id
        self.initial_work_item_id = initial_work_item_id
        self.initial_role_id = initial_role_id
        self.initial_target = initial_target
        self.state = BoardStateStore()
        self.facade: EngineFacade | None = None
        self.repository: BoardRepository | None = None
        self.actions: BoardActions | None = None
        self.event_bridge: CliBoardEventBridge | None = None
        self.reconcile_loop: ReconcileLoop | None = None
        self.exec_mode = "task"
        self.company_profile = "corporate"
        self._refresh_lock = asyncio.Lock()

        self.metrics_widget = MetricsBarWidget(self.state, exec_mode=self.exec_mode, company_profile=self.company_profile)
        self.session_sidebar = SessionSidebarWidget(self.state)
        self.board_widget = KanbanBoardWidget(self.state)
        self.list_widget = TaskListWidget(self.state)
        self.focus_widget = FocusTaskWidget(self.state)
        self.pipeline_widget = PipelineViewWidget(self.state)
        self.org_widget = OrgViewerWidget(self.state)
        self.context_tabs_widget = ContextTabsWidget(self.state)
        self.detail_widget = DetailPaneWidget(self.state)
        self.session_widget = SessionPaneWidget(self.state)
        self.activity_widget = ActivityPaneWidget(self.state)
        self.status_widget = StatusBarWidget(self.state)

        if bootstrap_services:
            self._bootstrap_services()

    def _bootstrap_services(self) -> None:
        from opc.plugins.cli_board.services.actions import BoardActions
        from opc.plugins.cli_board.services.board_repository import BoardRepository
        from opc.plugins.cli_board.services.engine_facade import EngineFacade
        from opc.plugins.cli_board.services.event_bridge import CliBoardEventBridge

        self.facade = EngineFacade(project_id=self.project_id)
        self.repository = BoardRepository(self.facade, project_id=self.project_id)
        self.actions = BoardActions(self.facade, project_id=self.project_id)
        self.event_bridge = CliBoardEventBridge(self._handle_board_event)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield self.metrics_widget
        with Horizontal(id="body"):
            with Vertical(id="session-shell"):
                with VerticalScroll(id="session-scroll"):
                    yield self.session_sidebar
            with Vertical(id="main-shell"):
                with VerticalScroll(id="main-scroll"):
                    yield self.board_widget
                    yield self.list_widget
                    yield self.focus_widget
                    yield self.pipeline_widget
                    yield self.org_widget
            with Vertical(id="context-shell"):
                yield self.context_tabs_widget
                with VerticalScroll(id="context-scroll"):
                    yield self.detail_widget
                    yield self.session_widget
                    yield self.activity_widget
        yield self.status_widget
        yield Footer()

    async def on_mount(self) -> None:
        self._sync_layout_state()
        if not self.facade or not self.repository or not self.actions or not self.event_bridge:
            self.status_widget.set_message("Standalone mode.")
            self._refresh_all_widgets()
            return
        self.status_widget.set_message("Initializing engine...")
        self.facade.configure_callbacks(
            progress_callback=self.event_bridge.handle_progress,
            event_callback=self.event_bridge.handle_event,
        )
        self._infer_default_mode()
        await self._refresh_snapshot(reason="startup")
        await self._apply_initial_inspector_target()
        from opc.plugins.cli_board.services.reconcile import ReconcileLoop

        self.reconcile_loop = ReconcileLoop(self.refresh_interval, self._refresh_from_reconcile)
        self.run_worker(self.reconcile_loop.run(), group="reconcile", exclusive=True)

    async def on_unmount(self) -> None:
        if self.reconcile_loop is not None:
            self.reconcile_loop.stop()
        if self.facade is not None:
            await self.facade.shutdown()

    def action_quit_board(self) -> None:
        if self.reconcile_loop is not None:
            self.reconcile_loop.stop()
        self.exit()

    def _infer_default_mode(self) -> None:
        """Auto-detect exec_mode from org config.

        If roles + employees are configured → company mode.
        Otherwise → task mode.  Avoids needing to sync with UI state.
        """
        if self.facade is None or self.facade.engine is None:
            return
        org = getattr(self.facade.engine, "org_engine", None)
        if org is None:
            return
        try:
            roles = org.list_agents()
            employees = org.list_employees()
            profile = org.get_company_profile()
            if profile == "custom":
                has_org = len(roles) > 0 and len(employees) > 0
            else:
                has_org = len(roles) > 1 and len(employees) > 0
        except Exception:
            has_org = False
        if has_org:
            self.exec_mode = "company"
            self.company_profile = profile
        # else keep default "task" / "corporate"

    def _require_actions(self) -> BoardActions | None:
        if self.readonly:
            self.status_widget.set_message("Read-only inspector: return to chat to run mutating commands.")
            return None
        if self.actions is None:
            self.status_widget.set_message("Task actions are unavailable.")
            return None
        return self.actions

    def _readonly_guard(self) -> bool:
        if not self.readonly:
            return False
        self.status_widget.set_message("Read-only inspector: q/Ctrl-Q returns to chat.")
        return True

    async def _run_office_service(self, operation: Any) -> Any:
        if self.facade is None:
            raise RuntimeError("Office services are unavailable.")
        engine = await self.facade.ensure_ready()
        async with OfficeServiceFactory(
            config=getattr(engine, "config", None),
            project_id=self.project_id or "default",
            on_progress=self.event_bridge.handle_progress if self.event_bridge else None,
            on_runtime_event=self.event_bridge.handle_event if self.event_bridge else None,
        ) as services:
            return await operation(services)

    def action_move_left(self) -> None:
        if self.state.pane_focus == "context":
            self.state.cycle_context_tab(-1)
            self.status_widget.set_message(f"Context tab: {self.state.context_tab}.")
            self._refresh_all_widgets()
            return
        if self.state.pane_focus == "session-rail":
            self.state.set_pane_focus("main")
            self.status_widget.set_message("Focused main viewport.")
            self._refresh_all_widgets()
            return
        if self.state.view_mode == "kanban":
            self.state.move_selection(column_delta=-1)
            self._selection_changed()
            return
        self.state.cycle_view_mode(-1)
        self.status_widget.set_message(f"Switched to {self.state.view_mode} view.")
        self._refresh_all_widgets()

    def action_move_right(self) -> None:
        if self.state.pane_focus == "context":
            self.state.cycle_context_tab(1)
            self.status_widget.set_message(f"Context tab: {self.state.context_tab}.")
            self._refresh_all_widgets()
            return
        if self.state.pane_focus == "session-rail":
            self.state.set_pane_focus("main")
            self.status_widget.set_message("Focused main viewport.")
            self._refresh_all_widgets()
            return
        if self.state.view_mode == "kanban":
            self.state.move_selection(column_delta=1)
            self._selection_changed()
            return
        self.state.cycle_view_mode(1)
        self.status_widget.set_message(f"Switched to {self.state.view_mode} view.")
        self._refresh_all_widgets()

    def action_move_up(self) -> None:
        if self.state.pane_focus == "session-rail":
            self.state.move_session_selection(-1)
        elif self.state.pane_focus == "main":
            if self.state.view_mode == "kanban":
                self.state.move_selection(row_delta=-1)
            else:
                self.state.move_linear_selection(-1)
        else:
            self.state.cycle_context_tab(-1)
            self.status_widget.set_message(f"Context tab: {self.state.context_tab}.")
            self._refresh_all_widgets()
            return
        self._selection_changed()

    def action_move_down(self) -> None:
        if self.state.pane_focus == "session-rail":
            self.state.move_session_selection(1)
        elif self.state.pane_focus == "main":
            if self.state.view_mode == "kanban":
                self.state.move_selection(row_delta=1)
            else:
                self.state.move_linear_selection(1)
        else:
            self.state.cycle_context_tab(1)
            self.status_widget.set_message(f"Context tab: {self.state.context_tab}.")
            self._refresh_all_widgets()
            return
        self._selection_changed()

    def action_focus_next_pane(self) -> None:
        focus = self.state.cycle_pane_focus(1)
        self.status_widget.set_message(f"Focused {focus}.")
        self._refresh_all_widgets()

    def action_focus_prev_pane(self) -> None:
        focus = self.state.cycle_pane_focus(-1)
        self.status_widget.set_message(f"Focused {focus}.")
        self._refresh_all_widgets()

    def action_activate_selection(self) -> None:
        if self.state.pane_focus == "session-rail":
            self.state.set_view_mode("focus")
            self.state.set_pane_focus("main")
            self.status_widget.set_message("Opened focus view.")
            self._refresh_all_widgets()
            return
        if self.state.pane_focus == "main":
            if self.state.view_mode != "focus":
                self.state.set_view_mode("focus")
                self.status_widget.set_message("Opened focus view.")
            else:
                self.state.set_pane_focus("context")
                self.status_widget.set_message("Focused context dock.")
            self._refresh_all_widgets()
            return
        self.state.cycle_context_tab(1)
        self.status_widget.set_message(f"Context tab: {self.state.context_tab}.")
        self._refresh_all_widgets()

    def action_toggle_density(self) -> None:
        density = self.state.toggle_density()
        self.status_widget.set_message(f"Density: {density}.")
        self._refresh_all_widgets()

    def action_view_kanban(self) -> None:
        self.state.set_view_mode("kanban")
        self.status_widget.set_message("Kanban view enabled.")
        self._refresh_all_widgets()

    def action_view_list(self) -> None:
        self.state.set_view_mode("list")
        self.status_widget.set_message("List view enabled.")
        self._refresh_all_widgets()

    def action_view_focus(self) -> None:
        self.state.set_view_mode("focus")
        self.status_widget.set_message("Focus view enabled.")
        self._refresh_all_widgets()

    def action_view_pipeline(self) -> None:
        self.state.set_view_mode("pipeline")
        self.status_widget.set_message("Projection view enabled.")
        self.run_worker(self._load_pipeline_for_selected(), group="pipeline", exclusive=True)

    def action_view_org(self) -> None:
        self.state.set_view_mode("org")
        self.status_widget.set_message("Organisation view enabled.")
        self.run_worker(self._load_org(), group="org", exclusive=True)

    def action_open_palette(self) -> None:
        self._action_open_palette()

    @work(group="modal", exclusive=True)
    async def _action_open_palette(self) -> None:
        result = await self.push_screen_wait(
            CommandPaletteScreen(
                title="CLI Board Commands",
                commands=self._palette_commands(),
            )
        )
        if not result:
            return
        await self._dispatch_palette_command(result)

    def action_new_task(self) -> None:
        if self._readonly_guard():
            return
        self._action_new_task()

    # Textual requires an active worker context for push_screen_wait.
    @work(group="modal", exclusive=True)
    async def _action_new_task(self) -> None:
        result = await self.push_screen_wait(
            PromptScreen(
                title="Create Task",
                fields=[
                    PromptField("title", "Title", placeholder="Short task title"),
                    PromptField("description", "Description", placeholder="Optional description"),
                    PromptField("message", "Initial message", placeholder="Optional first prompt to run immediately"),
                ],
                help_text="If you provide an initial message, the task will start immediately.",
                confirm_label="Create",
            )
        )
        if not result:
            return

        actions = self._require_actions()
        if actions is None:
            return
        title = result.get("title", "")
        description = result.get("description", "")
        initial_message = result.get("message", "").strip()
        await self._launch_action(
            actions.create_task(
                title=title,
                description=description,
                auto_run=bool(initial_message),
                initial_message=initial_message or None,
                mode=self.exec_mode,
                company_profile=self.company_profile,
            ),
            success_message="Task created.",
        )

    async def action_run_selected(self) -> None:
        task = self.state.selected_task()
        if task is None:
            self.status_widget.set_message("No task selected.")
            return
        actions = self._require_actions()
        if actions is None:
            return
        self._launch_background(
            actions.run_task(task.task_id, mode=self.exec_mode, company_profile=self.company_profile),
            success_message=f"Triggered {task.title}.",
        )

    def action_reply(self) -> None:
        if self._readonly_guard():
            return
        self._action_reply()

    @work(group="modal", exclusive=True)
    async def _action_reply(self) -> None:
        task = self.state.selected_task()
        if task is None:
            self.status_widget.set_message("No task selected.")
            return
        result = await self.push_screen_wait(
            PromptScreen(
                title=f"Reply to {task.title}",
                fields=[PromptField("message", "Message", placeholder="Continue the session")],
                confirm_label="Send",
            )
        )
        if not result or not result.get("message", "").strip():
            return
        actions = self._require_actions()
        if actions is None:
            return
        self._launch_background(
            actions.send_session_message(
                task.task_id,
                result["message"],
                mode=self.exec_mode,
                company_profile=self.company_profile,
            ),
            success_message=f"Sent a reply to {task.title}.",
        )

    def action_move_task(self) -> None:
        if self._readonly_guard():
            return
        self._action_move_task()

    @work(group="modal", exclusive=True)
    async def _action_move_task(self) -> None:
        task = self.state.selected_task()
        if task is None:
            self.status_widget.set_message("No task selected.")
            return
        result = await self.push_screen_wait(
            PromptScreen(
                title=f"Move {task.title}",
                fields=[PromptField("column", "Target column", value=task.column_id, placeholder="todo / in-progress / done")],
                confirm_label="Move",
            )
        )
        if not result:
            return
        actions = self._require_actions()
        if actions is None:
            return
        self._launch_background(
            actions.move_task(task.task_id, result.get("column", task.column_id)),
            success_message=f"Moved {task.title}.",
        )

    def action_approve_selected(self) -> None:
        task = self.state.selected_task()
        if task is None:
            self.status_widget.set_message("No task selected.")
            return
        if (
            task.pending_checkpoint is not None
            and task.pending_checkpoint.checkpoint_type
            in {"tool_permission", "action_permission"}
        ):
            self.status_widget.set_message(
                "Exact tool/action approval is disabled in the CLI board; "
                "review the complete payload in Office UI. Deny remains available."
            )
            return
        actions = self._require_actions()
        if actions is None:
            return
        self._launch_background(
            actions.approve_checkpoint(task.task_id, approved=True),
            success_message=f"Approved checkpoint for {task.title}.",
        )

    def action_deny_selected(self) -> None:
        task = self.state.selected_task()
        if task is None:
            self.status_widget.set_message("No task selected.")
            return
        actions = self._require_actions()
        if actions is None:
            return
        self._launch_background(
            actions.approve_checkpoint(task.task_id, approved=False),
            success_message=f"Denied checkpoint for {task.title}.",
        )

    def action_mark_done(self) -> None:
        task = self.state.selected_task()
        if task is None:
            self.status_widget.set_message("No task selected.")
            return
        actions = self._require_actions()
        if actions is None:
            return
        self._launch_background(
            actions.complete_task(task.task_id),
            success_message=f"Marked {task.title} done.",
        )

    def action_cancel_task(self) -> None:
        task = self.state.selected_task()
        if task is None:
            self.status_widget.set_message("No task selected.")
            return
        actions = self._require_actions()
        if actions is None:
            return
        self._launch_background(
            actions.cancel_task(task.task_id),
            success_message=f"Cancelled {task.title}.",
        )

    def action_retry_selected(self) -> None:
        task = self.state.selected_task()
        if task is None:
            self.status_widget.set_message("No task selected.")
            return
        actions = self._require_actions()
        if actions is None:
            return
        self._launch_background(
            actions.retry_task(task.task_id, mode=self.exec_mode, company_profile=self.company_profile),
            success_message=f"Reran {task.title}.",
        )

    def action_switch_mode(self) -> None:
        if self._readonly_guard():
            return
        self._action_switch_mode()

    @work(group="modal", exclusive=True)
    async def _action_switch_mode(self) -> None:
        result = await self.push_screen_wait(
            PromptScreen(
                title="Switch Execution Mode",
                fields=[
                    PromptField("mode", "Mode", value=self.exec_mode, placeholder="task / company / custom"),
                    PromptField("profile", "Company Profile", value=self.company_profile, placeholder="corporate / custom"),
                ],
                help_text="Mode determines how tasks are orchestrated. Profile selects the company runtime variant.",
                confirm_label="Apply",
            )
        )
        if not result:
            return
        new_mode = result.get("mode", "").strip().lower()
        new_profile = result.get("profile", "").strip().lower()
        if new_mode in {"task", "company", "custom"}:
            self.exec_mode = new_mode
        if new_profile:
            self.company_profile = new_profile
        self.status_widget.set_message(f"Mode: {self.exec_mode}/{self.company_profile}.")
        self._refresh_all_widgets()

    def action_checkpoint_feedback(self) -> None:
        if self._readonly_guard():
            return
        self._action_checkpoint_feedback()

    @work(group="modal", exclusive=True)
    async def _action_checkpoint_feedback(self) -> None:
        task = self.state.selected_task()
        if task is None:
            self.status_widget.set_message("No task selected.")
            return
        if not task.pending_checkpoint:
            self.status_widget.set_message("No pending checkpoint on this task.")
            return
        if task.pending_checkpoint.checkpoint_type in {
            "tool_permission",
            "action_permission",
        }:
            self.status_widget.set_message(
                "Exact tool/action approval with feedback is disabled in the "
                "CLI board; deny it here or review the complete payload in Office UI."
            )
            return
        result = await self.push_screen_wait(
            PromptScreen(
                title=f"Checkpoint Feedback: {task.pending_checkpoint.short_label}",
                fields=[
                    PromptField("feedback", "Your feedback", placeholder="Provide guidance or constraints...", multiline=True),
                    PromptField("action", "Action", value="approve", placeholder="approve / deny"),
                ],
                help_text="The feedback text will be sent along with your approve/deny decision.",
                confirm_label="Send",
            )
        )
        if not result:
            return
        feedback_text = result.get("feedback", "").strip()
        action_input = result.get("action", "approve").strip().lower()
        actions = self._require_actions()
        if actions is None:
            return
        decision = action_input if action_input in {"approve", "deny"} else "approve"
        reply = f"{decision}: {feedback_text}" if feedback_text else decision
        label = "Approved" if decision == "approve" else "Denied"
        suffix = " with feedback" if feedback_text else ""
        self._launch_background(
            actions.approve_checkpoint(task.task_id, reply=reply),
            success_message=f"{label} checkpoint for {task.title}{suffix}.",
        )

    def action_rename_session(self) -> None:
        if self._readonly_guard():
            return
        self._action_rename_session()

    @work(group="modal", exclusive=True)
    async def _action_rename_session(self) -> None:
        task = self.state.selected_task()
        if task is None:
            self.status_widget.set_message("No task selected.")
            return
        result = await self.push_screen_wait(
            PromptScreen(
                title="Rename Session",
                fields=[PromptField("title", "New title", value=task.title)],
                confirm_label="Rename",
            )
        )
        if not result:
            return
        new_title = result.get("title", "").strip()
        if not new_title:
            self.status_widget.set_message("Title cannot be empty.")
            return
        actions = self._require_actions()
        if actions is None:
            return
        try:
            engine = await self.facade.ensure_ready()
            if engine.store:
                t = await engine.store.get_task(task.task_id)
                if t:
                    t.title = new_title
                    await engine.store.save_task(t)
                    await self._refresh_snapshot(reason="rename", silent=True)
                    self.status_widget.set_message(f"Renamed to \"{new_title}\".")
        except Exception as exc:
            self.status_widget.set_message(f"Rename failed: {exc}")

    def action_delete_session(self) -> None:
        if self._readonly_guard():
            return
        self._action_delete_session()

    @work(group="modal", exclusive=True)
    async def _action_delete_session(self) -> None:
        task = self.state.selected_task()
        if task is None:
            self.status_widget.set_message("No task selected.")
            return
        result = await self.push_screen_wait(
            PromptScreen(
                title=f"Delete \"{task.title}\"?",
                fields=[PromptField("confirm", "Type 'yes' to confirm", placeholder="yes")],
                help_text="This will cancel the task. This action cannot be undone.",
                confirm_label="Delete",
            )
        )
        if not result or result.get("confirm", "").strip().lower() != "yes":
            self.status_widget.set_message("Cancelled.")
            return
        try:
            engine = await self.facade.ensure_ready()
            if engine.store:
                await engine.store.hard_delete_task(task.task_id, task.session_id)
            await self._refresh_snapshot(reason="delete", silent=True)
            self.status_widget.set_message(f"Deleted {task.title}.")
        except Exception as exc:
            self.status_widget.set_message(f"Delete failed: {exc}")

    def action_purge_cancelled(self) -> None:
        if self._readonly_guard():
            return
        self._action_purge_cancelled()

    @work(group="modal", exclusive=True)
    async def _action_purge_cancelled(self) -> None:
        engine = await self.facade.ensure_ready() if self.facade else None
        if not engine or not engine.store:
            self.status_widget.set_message("Store unavailable.")
            return

        project_id = self.project_id or "default"
        from opc.core.models import TaskStatus
        all_tasks = await engine.store.get_tasks(project_id=project_id)
        purgeable = [
            t for t in all_tasks
            if t.status in (TaskStatus.CANCELLED, TaskStatus.FAILED)
        ]

        if not purgeable:
            self.status_widget.set_message("No cancelled/failed tasks to purge.")
            return

        result = await self.push_screen_wait(
            PromptScreen(
                title=f"Purge {len(purgeable)} cancelled/failed tasks?",
                fields=[PromptField("confirm", "Type 'yes' to confirm", placeholder="yes")],
                help_text=f"This permanently deletes {len(purgeable)} tasks and all their data from the database.",
                confirm_label="Purge",
            )
        )
        if not result or result.get("confirm", "").strip().lower() != "yes":
            self.status_widget.set_message("Cancelled.")
            return

        deleted = 0
        for task in purgeable:
            try:
                await engine.store.hard_delete_task(task.id, task.session_id)
                deleted += 1
            except Exception:
                pass
        await self._refresh_snapshot(reason="purge", silent=True)
        self.status_widget.set_message(f"Purged {deleted} tasks.")

    def action_switch_project(self) -> None:
        if self._readonly_guard():
            return
        self._action_switch_project()

    @work(group="modal", exclusive=True)
    async def _action_switch_project(self) -> None:
        # Discover available projects from .opc/projects/
        project_ids: list[str] = []
        if self.facade:
            projects_dir = self.facade.opc_home / "projects"
            if projects_dir.is_dir():
                project_ids = sorted(
                    d.name for d in projects_dir.iterdir() if d.is_dir()
                )
        hint = ", ".join(project_ids[:8]) if project_ids else "no projects found"
        result = await self.push_screen_wait(
            PromptScreen(
                title="Switch Project",
                fields=[
                    PromptField("project", "Project ID", value=self.project_id or "default",
                                placeholder=hint),
                ],
                help_text=f"Available: {hint}",
                confirm_label="Switch",
            )
        )
        if not result:
            return
        new_id = result.get("project", "").strip()
        if not new_id or new_id == (self.project_id or "default"):
            self.status_widget.set_message("Project unchanged.")
            return

        # Shutdown current engine and reinitialize
        self.status_widget.set_message(f"Switching to {new_id}...")
        if self.reconcile_loop:
            self.reconcile_loop.stop()
        if self.facade:
            await self.facade.shutdown()

        self.project_id = new_id
        self._bootstrap_services()
        if self.facade and self.event_bridge:
            self.facade.configure_callbacks(
                progress_callback=self.event_bridge.handle_progress,
                event_callback=self.event_bridge.handle_event,
            )
        self._infer_default_mode()
        await self._refresh_snapshot(reason="project_switch")

        from opc.plugins.cli_board.services.reconcile import ReconcileLoop
        self.reconcile_loop = ReconcileLoop(self.refresh_interval, self._refresh_from_reconcile)
        self.run_worker(self.reconcile_loop.run(), group="reconcile", exclusive=True)
        self.status_widget.set_message(f"Switched to project: {new_id}.")

    def action_project_create(self) -> None:
        if self._readonly_guard():
            return
        self._action_project_create()

    @work(group="modal", exclusive=True)
    async def _action_project_create(self) -> None:
        result = await self.push_screen_wait(
            PromptScreen(
                title="Create Project",
                fields=[PromptField("project", "Project ID", placeholder="letters, numbers, hyphens, underscores")],
                confirm_label="Create",
            )
        )
        if not result:
            return
        project_id = result.get("project", "").strip()
        if not project_id:
            self.status_widget.set_message("Project ID cannot be empty.")
            return
        try:
            await self._run_office_service(lambda svc: svc.project.create(project_id, active_project_id=self.project_id or "default"))
            self.status_widget.set_message(f"Created project: {project_id}.")
        except Exception as exc:
            self.status_widget.set_message(f"Project create failed: {exc}")

    def action_project_delete(self) -> None:
        if self._readonly_guard():
            return
        self._action_project_delete()

    @work(group="modal", exclusive=True)
    async def _action_project_delete(self) -> None:
        result = await self.push_screen_wait(
            PromptScreen(
                title="Delete Project",
                fields=[
                    PromptField("project", "Project ID", value=self.project_id or "default"),
                    PromptField("confirm", "Type 'yes' to confirm", placeholder="yes"),
                ],
                help_text="The default project cannot be deleted.",
                confirm_label="Delete",
            )
        )
        if not result or result.get("confirm", "").strip().lower() != "yes":
            self.status_widget.set_message("Cancelled.")
            return
        project_id = result.get("project", "").strip()
        try:
            await self._run_office_service(lambda svc: svc.project.delete(project_id))
            self.status_widget.set_message(f"Deleted project: {project_id}.")
            if project_id == (self.project_id or "default"):
                self.project_id = "default"
                if self.facade:
                    await self.facade.shutdown()
                self._bootstrap_services()
                if self.facade and self.event_bridge:
                    self.facade.configure_callbacks(
                        progress_callback=self.event_bridge.handle_progress,
                        event_callback=self.event_bridge.handle_event,
                    )
                await self._refresh_snapshot(reason="project_delete", silent=True)
        except Exception as exc:
            self.status_widget.set_message(f"Project delete failed: {exc}")

    def action_session_config(self) -> None:
        if self._readonly_guard():
            return
        self._action_session_config()

    @work(group="modal", exclusive=True)
    async def _action_session_config(self) -> None:
        task = self.state.selected_task()
        if task is None:
            self.status_widget.set_message("No session selected.")
            return
        metadata = dict(task.metadata or {})
        result = await self.push_screen_wait(
            PromptScreen(
                title="Session Config",
                fields=[
                    PromptField("mode", "Mode", value=str(metadata.get("exec_mode") or self.exec_mode), placeholder="task / company / org"),
                    PromptField("profile", "Company Profile", value=str(metadata.get("company_profile") or self.company_profile), placeholder="corporate / custom"),
                    PromptField("agent", "Preferred Agent", value=str(metadata.get("preferred_agent") or ""), placeholder="native / codex / jiuwen (JiuwenSwarm-single) / jiuwenswarm (JiuwenSwarm-team)"),
                    PromptField("org", "Org ID", value=str(metadata.get("org_id") or metadata.get("organization_id") or ""), placeholder="saved org id"),
                ],
                confirm_label="Apply",
            )
        )
        if not result:
            return
        try:
            await self._run_office_service(
                lambda svc: svc.session.update_config(
                    project_id=self.project_id or "default",
                    task_id=task.task_id,
                    exec_mode=result.get("mode") or None,
                    company_profile=result.get("profile") or None,
                    preferred_agent=result.get("agent") or None,
                    org_id=result.get("org") or None,
                )
            )
            await self._refresh_snapshot(reason="session_config", silent=True)
            self.status_widget.set_message("Session config updated.")
        except Exception as exc:
            self.status_widget.set_message(f"Session config failed: {exc}")

    def action_org_add_role(self) -> None:
        if self._readonly_guard():
            return
        self._action_org_add_role()

    @work(group="modal", exclusive=True)
    async def _action_org_add_role(self) -> None:
        result = await self.push_screen_wait(
            PromptScreen(
                title="Add Org Role",
                fields=[
                    PromptField("role_id", "Role ID", placeholder="qa_lead"),
                    PromptField("name", "Name", placeholder="QA Lead"),
                    PromptField("responsibility", "Responsibility", placeholder="Owns quality and review"),
                    PromptField("reports_to", "Reports To", value="owner"),
                ],
                confirm_label="Add",
            )
        )
        if not result or not result.get("role_id", "").strip():
            return
        try:
            await self._run_office_service(lambda svc: svc.org.add_role({
                "role_id": result.get("role_id", "").strip(),
                "name": result.get("name", "").strip() or result.get("role_id", "").strip(),
                "responsibility": result.get("responsibility", "").strip(),
                "reports_to": result.get("reports_to", "").strip() or "owner",
            }))
            await self._load_org()
            self.status_widget.set_message("Role added.")
        except Exception as exc:
            self.status_widget.set_message(f"Add role failed: {exc}")

    def action_talent_scan(self) -> None:
        self.run_worker(self._action_talent_scan(), group="panel", exclusive=True)

    async def _action_talent_scan(self) -> None:
        try:
            result = await self._run_office_service(lambda svc: svc.talent.scan())
            count = len(result.payload.get("templates", []) or [])
            self.status_widget.set_message(f"Talent scan found {count} template(s).")
        except Exception as exc:
            self.status_widget.set_message(f"Talent scan failed: {exc}")

    def action_market_browse(self) -> None:
        self.run_worker(self._action_market_browse(), group="panel", exclusive=True)

    async def _action_market_browse(self) -> None:
        try:
            result = await self._run_office_service(lambda svc: svc.market.browse())
            count = len(result.payload.get("presets", []) or [])
            self.status_widget.set_message(f"Market has {count} architecture preset(s).")
        except Exception as exc:
            self.status_widget.set_message(f"Market browse failed: {exc}")

    def action_agent_list(self) -> None:
        self.run_worker(self._action_agent_list(), group="panel", exclusive=True)

    async def _action_agent_list(self) -> None:
        try:
            result = await self._run_office_service(lambda svc: svc.agent.list())
            count = len(result.payload.get("agents", []) or [])
            self.status_widget.set_message(f"Agents: {count}.")
        except Exception as exc:
            self.status_widget.set_message(f"Agent list failed: {exc}")

    def action_role_logs(self) -> None:
        self.run_worker(self._action_role_logs(), group="panel", exclusive=True)

    async def _action_role_logs(self) -> None:
        task = self.state.selected_task()
        role_id = task.assigned_to if task is not None else ""
        if not role_id:
            self.status_widget.set_message("No role selected.")
            return
        try:
            result = await self._run_office_service(lambda svc: svc.work_item.logs(project_id=self.project_id or "default", role_id=role_id, limit=50))
            count = len(result.payload.get("events", []) or [])
            self.status_widget.set_message(f"{role_id} logs: {count} event(s).")
        except Exception as exc:
            self.status_widget.set_message(f"Role logs failed: {exc}")

    def action_toggle_done(self) -> None:
        showing = self.state.toggle_show_done()
        self.status_widget.set_message("Done tasks visible." if showing else "Done tasks hidden.")
        self._selection_changed(load_detail=True)

    def action_search(self) -> None:
        self._action_search()

    @work(group="modal", exclusive=True)
    async def _action_search(self) -> None:
        result = await self.push_screen_wait(
            PromptScreen(
                title="Search tasks",
                fields=[PromptField("query", "Filter text", value=self.state.search_query)],
                help_text="Matches title, description, tags, assignee, and status.",
                confirm_label="Apply",
            )
        )
        if result is None:
            return
        self.state.set_search_query(result.get("query", ""))
        self.status_widget.set_message("Updated search filter.")
        self._selection_changed(load_detail=True)

    def action_refresh_board(self) -> None:
        self.run_worker(self._refresh_snapshot(reason="manual"), group="refresh", exclusive=True)

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    async def _refresh_from_reconcile(self) -> None:
        await self._refresh_snapshot(reason="reconcile", silent=True)

    async def _refresh_snapshot(self, *, reason: str, silent: bool = False) -> None:
        if self.repository is None:
            return
        async with self._refresh_lock:
            snapshot = await self.repository.load_snapshot()
            self.state.replace_snapshot(snapshot)
            await self._load_selected_detail()
            if not silent:
                self.status_widget.set_message(f"Board refreshed ({reason}).")

    async def _apply_initial_inspector_target(self) -> None:
        view = str(self.initial_view or "kanban").strip().lower().replace("_", "-")
        if view == "work-item":
            view = "focus"
        elif view == "logs":
            view = "focus"
            self.state.set_context_tab("activity")
        elif view == "role":
            view = "list"
            if self.initial_role_id:
                self.state.set_search_query(self.initial_role_id)
        if view not in {"kanban", "list", "focus", "pipeline", "org"}:
            view = "kanban"

        target_ids = {
            str(self.initial_work_item_id or "").strip(),
            str(self.initial_target or "").strip(),
        }
        if self.initial_session_id:
            target_ids.add(str(self.initial_session_id).strip())
        target_ids.discard("")
        selected_id = ""
        if target_ids:
            for task in self.state.all_tasks():
                candidates = {
                    str(task.task_id or ""),
                    str(task.session_id or ""),
                    str(task.work_item_id or ""),
                    str(task.runtime_task_id or ""),
                    str(task.execution_turn_id or ""),
                }
                if target_ids.intersection(candidates):
                    selected_id = task.task_id
                    break
        if not selected_id and self.initial_role_id:
            role_id = str(self.initial_role_id or "").strip()
            for task in self.state.all_tasks():
                if role_id and role_id in {str(task.assigned_to or ""), str(task.metadata.get("role_id", "") or "")}:
                    selected_id = task.task_id
                    break
        if selected_id:
            self.state.select_task(selected_id)
        if self.attach:
            self._sync_inspector_mode_from_selected_task()
        self.state.set_view_mode(view)  # type: ignore[arg-type]
        if self.attach:
            mode = "read-only " if self.readonly else ""
            self.status_widget.set_message(f"Attached {mode}inspector. q/Ctrl-Q returns to chat; r refreshes.")
        if view == "pipeline":
            await self._load_pipeline_for_selected()
        elif view == "org":
            await self._load_org()
        else:
            await self._load_selected_detail()

    def _sync_inspector_mode_from_selected_task(self) -> None:
        """Attached inspectors display the selected session identity, not global mode."""
        task = self.state.selected_task()
        if task is None:
            return
        metadata = dict(getattr(task, "metadata", {}) or {})
        raw_mode = str(
            metadata.get("exec_mode")
            or metadata.get("mode")
            or metadata.get("execution_mode")
            or ""
        ).strip().lower()
        raw_profile = str(metadata.get("company_profile") or "").strip().lower()
        raw_org = str(metadata.get("org_id") or metadata.get("organization_id") or "").strip()

        if raw_mode in {"org", "custom"} or raw_profile == "custom" or raw_org:
            self.exec_mode = "org"
            self.company_profile = "custom"
        elif raw_mode in {"company", "company_mode"} or raw_profile == "corporate":
            self.exec_mode = "company"
            self.company_profile = "corporate"
        elif raw_mode in {"task", "task_mode", "project", "project_mode", "single"}:
            self.exec_mode = "task"
            self.company_profile = "corporate"

    async def _load_org(self) -> None:
        org = None
        if self.repository is not None:
            org = await self.repository.load_org_snapshot()
        self.org_widget.set_org(org)
        self._refresh_all_widgets()

    async def _load_pipeline_for_selected(self) -> None:
        pipeline = None
        if self.repository is not None:
            selected = self.state.selected_task()
            if selected:
                pipeline = await self.repository.load_pipeline_state(
                    selected.task_id,
                    runtime_lookup=self.state.runtime_by_task,
                )
        self.pipeline_widget.set_pipeline(pipeline)
        self._refresh_all_widgets()

    async def _load_selected_detail(self) -> None:
        detail = None
        if self.repository is not None:
            selected = self.state.selected_task()
            detail = await self.repository.load_task_detail(selected.task_id) if selected else None
        self.detail_widget.set_detail(detail)
        self.session_widget.set_detail(detail)
        self.focus_widget.set_detail(detail)
        self.activity_widget.set_detail(detail)
        # Auto-focus Session tab when task has conversation or is running
        if detail and self.state.context_tab == "detail":
            has_conversation = bool(detail.transcript)
            is_active = detail.task.status in {"running", "idle", "blocked", "awaiting_review"}
            runtime = self.state.runtime_for(detail.task.task_id)
            has_runtime = runtime is not None and runtime.status not in {"idle", ""}
            if has_conversation or is_active or has_runtime:
                self.state.set_context_tab("session")
        self._refresh_all_widgets()

    def _selection_changed(self, *, load_detail: bool = True) -> None:
        if load_detail:
            self.run_worker(self._load_selected_detail(), group="detail", exclusive=True)
        else:
            self._refresh_all_widgets()

    async def _handle_board_event(self, payload: dict[str, Any]) -> None:
        kind = payload.get("kind")
        if kind == "task_status":
            self.state.apply_task_status(
                str(payload.get("task_id", "") or ""),
                str(payload.get("status", "") or ""),
                column_id=str(payload.get("column_id", "") or ""),
            )
            self._selection_changed(load_detail=False)
            return

        if kind == "runtime":
            self.state.apply_runtime_update(
                str(payload.get("task_id", "") or ""),
                status=str(payload.get("status", "") or "idle"),
                current_tool=payload.get("current_tool"),
                iteration=payload.get("iteration"),
                tool_elapsed_ms=payload.get("tool_elapsed_ms"),
                last_tool_summary=payload.get("last_tool_summary"),
                context_tokens=payload.get("context_tokens"),
                context_window=payload.get("context_window"),
                context_remaining_pct=payload.get("context_remaining_pct"),
                turn_cost_usd=payload.get("turn_cost_usd"),
                session_cost_usd=payload.get("session_cost_usd"),
                pending_permission_count=payload.get("pending_permission_count"),
                drain_mode=payload.get("drain_mode"),
            )
            self._refresh_all_widgets()
            return

        if kind == "progress":
            task_id = str(payload.get("task_id", "") or "")
            if task_id:
                self.state.append_progress(task_id, str(payload.get("text", "") or ""))
                if payload.get("current_tool"):
                    self.state.apply_runtime_update(
                        task_id,
                        status="tool_active",
                        current_tool=str(payload.get("current_tool") or ""),
                    )
            self._refresh_all_widgets()
            return

        if kind == "refresh":
            self.run_worker(
                self._refresh_snapshot(reason=str(payload.get("reason", "event")), silent=True),
                group="refresh",
                exclusive=True,
            )

    async def _launch_action(self, coro: Any, *, success_message: str) -> None:
        try:
            await coro
            await self._refresh_snapshot(reason="action", silent=True)
            self.status_widget.set_message(success_message)
        except Exception as exc:
            self.status_widget.set_message(f"Action failed: {exc}")

    def _launch_background(self, coro: Any, *, success_message: str) -> None:
        self.status_widget.set_message("Working...")
        self.run_worker(self._launch_action(coro, success_message=success_message), exclusive=False)

    def _refresh_all_widgets(self) -> None:
        self._sync_layout_state()
        self.metrics_widget.exec_mode = self.exec_mode
        self.metrics_widget.company_profile = self.company_profile
        self.status_widget.exec_mode = self.exec_mode
        self.status_widget.company_profile = self.company_profile
        if self.pipeline_widget.pipeline:
            self.metrics_widget.pipeline_done = self.pipeline_widget.pipeline.done_count
            self.metrics_widget.pipeline_total = self.pipeline_widget.pipeline.total_count
        else:
            self.metrics_widget.pipeline_done = 0
            self.metrics_widget.pipeline_total = 0
        for widget in [
            self.metrics_widget,
            self.session_sidebar,
            self.board_widget,
            self.list_widget,
            self.focus_widget,
            self.pipeline_widget,
            self.org_widget,
            self.context_tabs_widget,
            self.detail_widget,
            self.session_widget,
            self.activity_widget,
            self.status_widget,
        ]:
            widget.refresh()

    def _sync_layout_state(self) -> None:
        self._set_hidden(self.board_widget, self.state.view_mode != "kanban")
        self._set_hidden(self.list_widget, self.state.view_mode != "list")
        self._set_hidden(self.focus_widget, self.state.view_mode != "focus")
        self._set_hidden(self.pipeline_widget, self.state.view_mode != "pipeline")
        self._set_hidden(self.org_widget, self.state.view_mode != "org")
        self._set_hidden(self.detail_widget, self.state.context_tab != "detail")
        self._set_hidden(self.session_widget, self.state.context_tab != "session")
        self._set_hidden(self.activity_widget, self.state.context_tab != "activity")
        self._set_hidden(self.query_one("#session-shell"), self.state.view_mode in {"focus", "pipeline", "org"})
        self._set_focus_class(self.query_one("#session-shell"), self.state.pane_focus == "session-rail")
        self._set_focus_class(self.query_one("#main-shell"), self.state.pane_focus == "main")
        self._set_focus_class(self.query_one("#context-shell"), self.state.pane_focus == "context")

    @staticmethod
    def _set_hidden(node: Any, hidden: bool) -> None:
        if hidden:
            node.add_class("hidden")
        else:
            node.remove_class("hidden")

    @staticmethod
    def _set_focus_class(node: Any, focused: bool) -> None:
        if focused:
            node.add_class("pane-focused")
        else:
            node.remove_class("pane-focused")

    def _palette_commands(self) -> list[PaletteCommand]:
        commands = [
            PaletteCommand("new_task", "Create Task", "Create a new task and optional first prompt.", "n"),
            PaletteCommand("run_selected", "Run Selected Task", "Start the selected task in the current mode.", "g"),
            PaletteCommand("reply", "Reply in Session", "Send a follow-up message to the selected session.", "s"),
            PaletteCommand("move_task", "Move Task", "Move the selected task to another lane.", "m"),
            PaletteCommand("approve_selected", "Approve Checkpoint", "Approve the pending checkpoint.", "a"),
            PaletteCommand("deny_selected", "Deny Checkpoint", "Reject the pending checkpoint.", "d"),
            PaletteCommand("mark_done", "Mark Done", "Mark the selected task as done.", "c"),
            PaletteCommand("cancel_task", "Cancel Task", "Cancel the selected task.", "x"),
            PaletteCommand("retry_selected", "Retry Task", "Retry the selected task.", "t"),
            PaletteCommand("checkpoint_feedback", "Checkpoint Feedback", "Approve or deny with custom feedback.", "e"),
            PaletteCommand("switch_mode", "Switch Execution Mode", "Change between task, company, and custom modes.", "E"),
            PaletteCommand("search", "Search / Filter", "Filter tasks and sessions by text.", "/"),
            PaletteCommand("refresh_board", "Refresh Board", "Reload the board snapshot from storage.", "r"),
            PaletteCommand("project_create", "Create Project", "Create a project through the shared Office service.", ""),
            PaletteCommand("project_delete", "Delete Project", "Delete a project through the shared Office service.", ""),
            PaletteCommand("session_config", "Configure Session", "Edit selected session mode, profile, agent, and org.", ""),
            PaletteCommand("org_add_role", "Add Org Role", "Add a role through the shared Office service.", ""),
            PaletteCommand("talent_scan", "Talent Scan", "Scan local recruitable talent templates.", ""),
            PaletteCommand("market_browse", "Market Browse", "Browse architecture presets.", ""),
            PaletteCommand("agent_list", "Agent List", "Refresh visual office agent count.", ""),
            PaletteCommand("role_logs", "Role Logs", "Aggregate selected role work-item events.", ""),
            PaletteCommand("view_kanban", "Switch to Kanban", "Show the command-center Kanban view.", "1"),
            PaletteCommand("view_list", "Switch to List", "Show a dense linear task list.", "2"),
            PaletteCommand("view_focus", "Switch to Focus", "Zoom into the selected task.", "3"),
            PaletteCommand("view_pipeline", "Switch to Projection", "Show the read-only work-item projection for the selected company run.", "4"),
            PaletteCommand("view_org", "Switch to Organisation", "Show read-only org structure.", "5"),
            PaletteCommand("rename_session", "Rename Session", "Change the title of the selected task.", "R"),
            PaletteCommand("delete_session", "Delete Session", "Cancel and remove the selected task.", "D"),
            PaletteCommand("purge_cancelled", "Purge Cancelled Tasks", "Permanently delete all cancelled/failed tasks.", ""),
            PaletteCommand("switch_project", "Switch Project", "Change the active project.", ""),
            PaletteCommand("toggle_done", "Toggle Done Visibility", "Hide or show done tasks.", "f"),
            PaletteCommand("toggle_density", "Toggle Density", "Switch between compact and comfortable density.", "space"),
            PaletteCommand("focus_next_pane", "Focus Next Pane", "Cycle focus between session rail, viewport, and dock.", "tab"),
            PaletteCommand("show_help", "Open Help", "Display grouped keyboard shortcuts.", "?"),
        ]
        if not self.readonly:
            return commands
        mutating = {
            "new_task",
            "run_selected",
            "reply",
            "move_task",
            "approve_selected",
            "deny_selected",
            "mark_done",
            "cancel_task",
            "retry_selected",
            "checkpoint_feedback",
            "switch_mode",
            "project_create",
            "project_delete",
            "session_config",
            "org_add_role",
            "rename_session",
            "delete_session",
            "purge_cancelled",
            "switch_project",
        }
        return [command for command in commands if command.command_id not in mutating]

    async def _dispatch_palette_command(self, command_id: str) -> None:
        if command_id == "run_selected":
            await self.action_run_selected()
            return
        action = getattr(self, f"action_{command_id}", None)
        if action is None:
            self.status_widget.set_message(f"Unknown palette command: {command_id}")
            return
        result = action()
        if asyncio.iscoroutine(result):
            await result
