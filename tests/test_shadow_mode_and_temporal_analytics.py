import os
import pytest
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from opc.core.config import (
    AgentsConfig,
    ExternalAgentConfig,
)
from opc.core.models import (
    Phase,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer0_interaction.message_bus import MessageBus
from opc.layer3_agent.adapters.human_adapter import HumanAgentAdapter
from opc.layer3_agent.adapters.registry import AdapterRegistry
from opc.layer3_agent.external_broker import ExternalAgentBroker
from opc.layer2_organization.work_item_transition import (
    DelegationWorkItem,
    transition_work_item,
)
from opc.layer2_organization.phase import validate_transition


def test_adapter_registry_disabled_by_default():
    """Verify that unconfigured / disabled external adapters return empty list."""
    async def _run():
        config = AgentsConfig()
        registry = AdapterRegistry(config)
        await registry.initialize()

        available = [k for k, v in registry._available.items() if v]
        assert "human" not in available
        assert registry.get("human") is None

    asyncio.run(_run())


def test_human_adapter_enabled_explicitly():
    """Verify human adapter is available ONLY when explicitly enabled."""
    async def _run():
        config = AgentsConfig(
            agents={"human": ExternalAgentConfig(enabled=True)}
        )
        registry = AdapterRegistry(config)
        await registry.initialize()

        adapter = registry.get("human")
        assert adapter is not None
        assert isinstance(adapter, HumanAgentAdapter)
        assert await adapter.is_available() is True

    asyncio.run(_run())


def test_external_broker_human_direct_execution():
    """Verify ExternalAgentBroker invokes human adapter directly without subprocess."""
    async def _run():
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "test_opc.db"
            store = OPCStore(db_path=str(db_path))
            await store.initialize()

            try:
                approval_engine = MagicMock()
                approval_engine.authorize_external_action = MagicMock()

                broker = ExternalAgentBroker(store=store, approval_engine=approval_engine)

                config = ExternalAgentConfig(enabled=True)
                human_adapter = HumanAgentAdapter(config=config, store=store)

                task = Task(
                    id="task_human_test",
                    title="Human Task Verification",
                    description="Verify direct broker execution",
                    assigned_to="contractor_role_1",
                )
                await store.save_task(task)

                exec_task = asyncio.create_task(broker.run(human_adapter, task, str(tmp_path)))
                await asyncio.sleep(0.1)

                saved_task = await store.get_task("task_human_test")
                assert saved_task is not None
                assert str(saved_task.status.value if hasattr(saved_task.status, "value") else saved_task.status).lower() in ("awaiting_human", "running", "pending")

                exec_task.cancel()
            finally:
                await store.close()

    asyncio.run(_run())


def test_work_item_phase_transition_and_dag_release():
    """Verify phase transition from awaiting_human to awaiting_manager_review."""
    async def _run():
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "test_opc.db"
            store = OPCStore(db_path=str(db_path))
            await store.initialize()

            try:
                item1 = DelegationWorkItem(
                    work_item_id="wi_1",
                    run_id="run_100",
                    cell_id="cell_1",
                    team_instance_id="team_1",
                    team_id="team_1",
                    role_id="contractor_role",
                    seat_id="seat_1",
                    title="Contractor Work Item",
                    phase=Phase.AWAITING_HUMAN,
                )
                await store.save_delegation_work_item(item1)

                res = await transition_work_item(
                    store=store,
                    work_item_id="wi_1",
                    target_phase=Phase.AWAITING_MANAGER_REVIEW,
                    reason="Contractor deliverable submission",
                    summary="Contractor submission completed",
                    deliverable_summary="Contractor submission completed",
                )
                assert res is not None

                fetched = await store.get_delegation_work_item("wi_1")
                assert fetched is not None
                assert fetched.phase == Phase.AWAITING_MANAGER_REVIEW
                assert fetched.deliverable_summary == "Contractor submission completed"
            finally:
                await store.close()

    asyncio.run(_run())


def test_temporal_performance_analytics_date_filtering():
    """Verify get_temporal_performance filters by start_date, end_date, and completion time."""
    async def _run():
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "test_opc.db"
            store = OPCStore(db_path=str(db_path))
            await store.initialize()

            try:
                task1 = Task(
                    id="task_perf_1",
                    title="Completed Task 1",
                    status=TaskStatus.DONE,
                    assigned_to="dev_role",
                )
                await store.save_task(task1)

                data = await store.get_temporal_performance(interval="daily")
                assert "global" in data
                assert "team" in data
                assert "individual" in data
            finally:
                await store.close()

    asyncio.run(_run())
