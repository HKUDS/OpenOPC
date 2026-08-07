from __future__ import annotations

import unittest

from opc.plugins.cli_board.state.models import BoardSnapshot, BoardTaskView, TaskDetailView

try:  # pragma: no cover - optional dependency
    from opc.plugins.cli_board.tui.app import CliBoardApp
    from opc.plugins.cli_board.tui.screens.help import HelpScreen
    from opc.plugins.cli_board.tui.screens.palette import CommandPaletteScreen
    from opc.plugins.cli_board.tui.screens.prompt import PromptScreen
except ImportError:  # pragma: no cover - optional dependency
    CliBoardApp = None
    HelpScreen = None
    CommandPaletteScreen = None
    PromptScreen = None


class _StubRepository:
    def __init__(self) -> None:
        self.snapshot = BoardSnapshot(
            project_id="demo",
            tasks=[
                BoardTaskView(
                    task_id="todo-1",
                    title="Todo task",
                    description="Pending task",
                    status="pending",
                    column_id="todo",
                    priority="medium",
                    created_at=1.0,
                    updated_at=1.0,
                ),
                BoardTaskView(
                    task_id="run-1",
                    title="Running task",
                    description="In progress",
                    status="running",
                    column_id="in-progress",
                    priority="high",
                    created_at=2.0,
                    updated_at=2.0,
                ),
                BoardTaskView(
                    task_id="done-1",
                    title="Done task",
                    description="Completed",
                    status="done",
                    column_id="done",
                    priority="low",
                    created_at=3.0,
                    updated_at=3.0,
                ),
            ],
        )

    async def load_snapshot(self) -> BoardSnapshot:
        return self.snapshot

    async def load_task_detail(self, task_id: str):
        task = next((task for task in self.snapshot.tasks if task.task_id == task_id), None)
        return TaskDetailView(task=task) if task else None


@unittest.skipIf(CliBoardApp is None, "textual is not installed")
class CliBoardAppPilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyboard_navigation_view_switching_and_modals(self) -> None:
        app = CliBoardApp(project_id="demo", refresh_interval=60.0, bootstrap_services=False)
        app.repository = _StubRepository()

        async with app.run_test() as pilot:
            app.state.replace_snapshot(await app.repository.load_snapshot())
            await app._load_selected_detail()
            app.status_widget.set_message("Harness ready.")
            self.assertEqual(app.state.selected_task().task_id, "todo-1")

            await pilot.press("right")
            self.assertEqual(app.state.selected_task().task_id, "run-1")

            await pilot.press("2")
            self.assertEqual(app.state.view_mode, "list")

            await pilot.press("3")
            self.assertEqual(app.state.view_mode, "focus")

            app.action_focus_next_pane()
            await pilot.pause()
            self.assertEqual(app.state.pane_focus, "context")

            # Selecting the running task earlier auto-focused the Session
            # context tab (CliBoardApp._load_selected_detail), so cycling
            # right moves session -> activity.
            self.assertEqual(app.state.context_tab, "session")
            await pilot.press("right")
            self.assertEqual(app.state.context_tab, "activity")

            await pilot.press("f")
            self.assertFalse(app.state.show_done)

            await pilot.press("n")
            self.assertIsInstance(app.screen, PromptScreen)
            await pilot.press("escape")

            app.action_open_palette()
            await pilot.pause()
            self.assertIsInstance(app.screen, CommandPaletteScreen)
            await pilot.press("escape")

            await pilot.press("?")
            self.assertIsInstance(app.screen, HelpScreen)
            await pilot.press("escape")

