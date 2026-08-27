from __future__ import annotations

import unittest

from opc.core.models import OPCEvent
from opc.plugins.cli_board.services.event_bridge import CliBoardEventBridge


class CliBoardEventBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_refreshes_for_current_task_events_only(self) -> None:
        updates: list[dict] = []

        async def sink(update: dict) -> None:
            updates.append(update)

        bridge = CliBoardEventBridge(sink)
        await bridge.handle_event(OPCEvent(event_type="task_created", payload={}))
        await bridge.handle_event(
            OPCEvent(
                event_type="escalation_created",
                payload={"message": "legacy callback event"},
            )
        )

        self.assertEqual(
            updates,
            [{"kind": "refresh", "reason": "task_created"}],
        )
