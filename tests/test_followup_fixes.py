"""Follow-up tests for the round-2 fixes landed after new16/app13:

- Fix 1 retry: first non-actionable reject spawns Review #N+1 with a
  concrete example hint; only a *second* non-actionable verdict degrades
  to approve-with-warning.
- Fix 2 tightening: ``_role_session_id`` prefers ``task.metadata.
  delegation_team_instance_id`` over an empty cache, so the runtime no
  longer produces ``_no_team::{role}`` sentinel rows when the task knows
  its team. Migration collapses legacy sentinel rows into the canonical
  companion when both exist for a (run_id, role_id).
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from opc.core.models import (
    DelegationWorkItem,
    Phase,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization.company_runtime import (
    _NO_TEAM_SENTINEL,
    canonical_role_session_id,
)


# ── Fix 1 retry path: deleted — runtime no longer does shape-based salvage /
# retry / degrade. See company_mode.py:_finalize_review_work_item for the
# new mechanical-apply flow with verdict-parse-retry as the only fallback.




# ── Fix 5 PR1: role_session_id is role-scoped, no team slot ──────────────


class RoleSessionIdIsRoleScopedTests(unittest.TestCase):
    """Fix 5 PR1: ``_role_session_id`` returns ``role-runtime::{run}::{role}``
    regardless of any team_instance_id cache/metadata. Same role across
    every team context resolves to a single session — that's the whole
    point of PR1."""

    def setUp(self) -> None:
        from opc.layer2_organization.company_runtime import CompanyRuntime

        async def _noop(*_a, **_kw):
            return None

        self.runtime = CompanyRuntime(
            org_engine=None,
            communication=None,
            store=None,
            save_runtime_session=_noop,
            emit_runtime_event=_noop,
        )

    def _task(self, metadata: dict) -> Task:
        return Task(
            id="t",
            title="t", description="",
            assigned_to="cto",
            status=TaskStatus.RUNNING,
            project_id="p", session_id="s",
            metadata=metadata,
        )

    def test_cache_does_not_affect_session_id(self) -> None:
        self.runtime._home_team_instance_by_role["cto"] = "ti-cached"
        task = self._task({"delegation_run_id": "run-1"})
        sid = self.runtime._role_session_id(task, role_id="cto")
        self.assertEqual(sid, "role-runtime::run-1::cto")

    def test_task_metadata_team_instance_does_not_affect_session_id(self) -> None:
        task = self._task({
            "delegation_run_id": "run-1",
            "delegation_team_instance_id": "ti-from-task",
            "team_instance_id": "ti-legacy",
        })
        sid = self.runtime._role_session_id(task, role_id="cto")
        self.assertEqual(sid, "role-runtime::run-1::cto")

    def test_explicit_metadata_override_honoured(self) -> None:
        # When caller has already stamped the session id (e.g. re-used
        # from a prior lookup), we must respect it verbatim.
        task = self._task({
            "delegation_run_id": "run-1",
            "delegation_role_session_id": "role-runtime::run-1::cto",
        })
        sid = self.runtime._role_session_id(task, role_id="cto")
        self.assertEqual(sid, "role-runtime::run-1::cto")


class SentinelLegacyMigrationTests(unittest.IsolatedAsyncioTestCase):
    """Post-Fix-5 PR2: the ``_no_team::{role}`` sentinel shape is just one
    more legacy form. The rich merge migration collapses it into the
    canonical 3-segment row and redirects every reference."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    async def _raw_insert(
        self,
        *,
        role_session_id: str,
        run_id: str,
        role_id: str,
        team_instance_id: str = "",
        updated_at: str | None = None,
    ) -> None:
        ts = updated_at or datetime.now().isoformat()
        for table in ("role_runtime_sessions", "delegation_role_sessions"):
            await self.store._db.execute(
                f"""INSERT INTO {table}
                    (role_session_id, run_id, project_id, team_instance_id, team_id,
                     role_id, seat_id, seat_state_id, employee_id,
                     focused_work_item_id, background_work_item_ids,
                     manager_role_ids, manager_seat_ids, seat_ids,
                     adapter_session_state, inbox_state, memory_slices_by_work_item,
                     resume_state, current_work_item, latest_notification,
                     manager_digest, status, metadata, created_at, updated_at)
                    VALUES (?, ?, 'p', ?, 'team::x',
                            ?, '', '', '',
                            '', '[]', '[]', '[]', '[]',
                            '{{}}', '{{}}', '{{}}', '{{}}', '{{}}', '{{}}',
                            '{{}}', 'idle', '{{}}', ?, ?)""",
                (role_session_id, run_id, team_instance_id, role_id, ts, ts),
            )
        await self.store._db.commit()

    async def test_sentinel_plus_legacy_full_form_merge_to_canonical(self) -> None:
        run_id = "run-merge"
        role_id = "designer"
        canonical = canonical_role_session_id(run_id=run_id, role_id=role_id)
        legacy_full = f"role-runtime::{run_id}::ti-real::{role_id}"
        sentinel = f"role-runtime::{run_id}::{_NO_TEAM_SENTINEL}::{role_id}"
        await self._raw_insert(
            role_session_id=legacy_full,
            run_id=run_id, role_id=role_id, team_instance_id="ti-real",
        )
        await self._raw_insert(
            role_session_id=sentinel,
            run_id=run_id, role_id=role_id,
        )

        # Work item with its claim pointing at the sentinel — the app13
        # scenario where a role's live session diverged from bootstrap.
        wi = DelegationWorkItem(
            work_item_id="wi-claim",
            run_id=run_id, cell_id="c",
            team_instance_id="ti-real",
            team_id="team::x",
            role_id=role_id,
            seat_id="seat::team::x::designer",
            role_runtime_session_id=sentinel,
            claimed_by_role_runtime_session_id=sentinel,
            claimed_by_seat_id="seat::team::x::designer",
            manager_role_id="cmo",
            manager_seat_id="seat::team::x::cmo",
            title="t", phase=Phase.RUNNING,
            metadata={"assigned_role_runtime_id": sentinel},
        )
        await self.store.save_delegation_work_item(wi)

        stats = await self.store._migrate_role_sessions_merge_by_role()
        self.assertEqual(stats["groups"], 1)
        self.assertEqual(stats["canonical_written"], 1)

        # Both legacy rows are gone, canonical survives.
        async with self.store._db.execute(
            "SELECT COUNT(*) FROM role_runtime_sessions WHERE role_session_id=?",
            (sentinel,),
        ) as cursor:
            (count_sentinel,) = await cursor.fetchone()
        self.assertEqual(count_sentinel, 0)
        async with self.store._db.execute(
            "SELECT COUNT(*) FROM role_runtime_sessions WHERE role_session_id=?",
            (legacy_full,),
        ) as cursor:
            (count_full,) = await cursor.fetchone()
        self.assertEqual(count_full, 0)
        async with self.store._db.execute(
            "SELECT COUNT(*) FROM role_runtime_sessions WHERE role_session_id=?",
            (canonical,),
        ) as cursor:
            (count_canonical,) = await cursor.fetchone()
        self.assertEqual(count_canonical, 1)

        # The claim on the work item must now resolve to canonical.
        wi_after = await self.store.get_delegation_work_item("wi-claim")
        self.assertEqual(wi_after.claimed_by_role_runtime_session_id, canonical)
        self.assertEqual(wi_after.role_runtime_session_id, canonical)
        self.assertEqual(wi_after.metadata.get("assigned_role_runtime_id"), canonical)

    async def test_lone_sentinel_renamed_to_canonical(self) -> None:
        """Post-Fix-5: a lone sentinel row IS migrated to canonical (unlike
        the pre-PR2 behavior that left it in place). A runtime lookup
        via ``canonical_role_session_id`` now targets the 3-segment PK,
        so leaving sentinel rows untouched would orphan them."""
        run_id = "run-lone"
        role_id = "qa_analyst"
        sentinel = f"role-runtime::{run_id}::{_NO_TEAM_SENTINEL}::{role_id}"
        canonical = canonical_role_session_id(run_id=run_id, role_id=role_id)
        await self._raw_insert(
            role_session_id=sentinel,
            run_id=run_id, role_id=role_id,
        )

        stats = await self.store._migrate_role_sessions_merge_by_role()
        self.assertEqual(stats["groups"], 1)
        self.assertEqual(stats["canonical_written"], 1)

        async with self.store._db.execute(
            "SELECT COUNT(*) FROM role_runtime_sessions WHERE role_session_id=?",
            (sentinel,),
        ) as cursor:
            (count,) = await cursor.fetchone()
        self.assertEqual(count, 0)
        async with self.store._db.execute(
            "SELECT COUNT(*) FROM role_runtime_sessions WHERE role_session_id=?",
            (canonical,),
        ) as cursor:
            (count_canonical,) = await cursor.fetchone()
        self.assertEqual(count_canonical, 1)

    async def test_migration_idempotent_with_nothing_to_do(self) -> None:
        stats = await self.store._migrate_role_sessions_merge_by_role()
        self.assertEqual(stats["groups"], 0)
        self.assertEqual(stats["canonical_written"], 0)


# ── Fix 3 guard whitelist ────────────────────────────────────────────────


if __name__ == "__main__":
    unittest.main()
