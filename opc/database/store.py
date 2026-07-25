"""SQLite-based persistent storage for tasks, collaboration state, and observability."""

from __future__ import annotations

import json
import inspect
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
import sqlite3
from typing import Any

from loguru import logger

from opc.core.models import (
    AgentCompactionRecord,
    AgentMemorySnapshotRecord,
    AgentMessage,
    ApprovalDecision,
    ArtifactRecord,
    CommsSemanticType,
    CommsState,
    CommsTransportKind,
    CostEvent,
    DelegationCell,
    DelegationEvent,
    DelegationRoleSession,
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    ExternalSession,
    Goal,
    GoalLevel,
    GoalStatus,
    HandoffRecord,
    MeetingRoom,
    MeetingStatus,
    MessageUrgency,
    MessageStatus,
    OrgAgent,
    OrgSnapshot,
    Organization,
    OPCEvent,
    ReorgEventKind,
    ReorgEventRecord,
    ReorgProposal,
    ReorgProposalStatus,
    ReorgRiskLevel,
    ReorgScope,
    RoleMemoryRecord,
    RoleRuntimeSession,
    SeatState,
    SessionCompactionRecord,
    SessionMemorySnapshotRecord,
    SessionLinkRecord,
    SessionMessageRecord,
    SessionPartRecord,
    SessionRecord,
    TeamInstance,
    Task,
    TaskStatus,
    WorkItemDecisionRecord,
    normalize_role_runtime_status,
)
from opc.core.models import Phase
from opc.core.transcript_visibility import (
    normalize_transcript_detail_level,
    transcript_visibility_sql,
)
from opc.layer2_organization.phase import (
    DONE_PHASES,
    IN_PROGRESS_PHASES,
    IN_REVIEW_PHASES,
    InvalidPhaseTransition,
    TODO_PHASES,
    coerce_phase,
    is_stale_claim_releasable,
    is_terminal,
    kanban_column,
    on_phase_transition,
    validate_transition,
)
from opc.layer2_organization.work_item_identity import (
    WORK_ITEM_PROJECTION_ID_KEY,
    WORK_ITEM_TURN_TYPE_KEY,
    migrate_work_item_projection_metadata,
    projection_id_for_work_item,
)
from opc.layer2_organization.work_item_links import (
    linked_work_item_id_for_task,
    set_linked_work_item_id,
)
from opc.layer2_organization.work_item_runtime import (
    is_work_item_runtime_metadata,
    migrate_work_item_runtime_metadata,
)
from opc.layer2_organization.work_item_runtime_invariants import (
    validate_work_item_runtime_projection,
)


def _json_dumps(value: Any) -> str:
    def _default(obj: Any) -> Any:
        if is_dataclass(obj):
            return asdict(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Enum):
            return obj.value
        raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

    return json.dumps(value, ensure_ascii=False, default=_default)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        # JSON columns can hold corrupt/partial values after a crash or manual edit.
        # Raising here would abort store.initialize() (e.g. via _sweep_stale_claims) and
        # prevent the store from ever opening, so fall back to the default instead.
        return default


class _SQLiteCursorAdapter:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    async def __aenter__(self) -> "_SQLiteCursorAdapter":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._cursor.close()
        return False

    async def fetchone(self) -> Any:
        return self._cursor.fetchone()

    async def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    @property
    def description(self) -> Any:
        return self._cursor.description

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount


class _SQLiteExecuteResult:
    def __init__(self, connection: sqlite3.Connection, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> None:
        self._connection = connection
        self._sql = sql
        self._params = tuple(params)
        self._cursor: sqlite3.Cursor | None = None

    def __await__(self):
        async def _run() -> _SQLiteCursorAdapter:
            cursor = self._connection.cursor()
            cursor.execute(self._sql, self._params)
            return _SQLiteCursorAdapter(cursor)

        return _run().__await__()

    async def __aenter__(self) -> _SQLiteCursorAdapter:
        cursor = self._connection.cursor()
        cursor.execute(self._sql, self._params)
        self._cursor = cursor
        return _SQLiteCursorAdapter(cursor)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._cursor is not None:
            self._cursor.close()
        return False


class _SQLiteConnectionAdapter:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")

    def execute(self, sql: str, parameters: tuple[Any, ...] | list[Any] = ()) -> _SQLiteExecuteResult:
        return _SQLiteExecuteResult(self._conn, sql, parameters)

    async def executescript(self, script: str) -> None:
        self._conn.executescript(script)

    async def commit(self) -> None:
        self._conn.commit()

    async def rollback(self) -> None:
        self._conn.rollback()

    async def close(self) -> None:
        self._conn.close()


class OPCStore:
    """Async SQLite store for OPC data (WAL mode for concurrency)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self.project_id = self._infer_project_id_from_db_path(db_path)
        self._db: _SQLiteConnectionAdapter | None = None
        # Fix 5 PR3 feature flag mirrored onto the store so phase hooks
        # (which receive ``store`` but not the top-level OPCConfig) can
        # consult it cheaply. Engine sets this during init from
        # ``OPCConfig.org.role_serial_queue_enabled``. Tests can flip it
        # directly to exercise both branches.
        self.role_serial_queue_enabled: bool = True

    @staticmethod
    def _infer_project_id_from_db_path(db_path: str | Path) -> str | None:
        path = Path(db_path)
        try:
            if path.name == "tasks.db" and path.parent.parent.name == "projects":
                return path.parent.name or None
        except Exception:
            return None
        return None

    def _assert_project_write_scope(
        self,
        value: str | None,
        *,
        operation: str,
        entity: str,
    ) -> None:
        store_project_id = str(self.project_id or "").strip()
        entity_project_id = str(value or "").strip()
        if not store_project_id or not entity_project_id:
            return
        if entity_project_id != store_project_id:
            raise RuntimeError(
                f"{operation} rejected cross-project {entity} write: "
                f"store_project={store_project_id!r} entity_project={entity_project_id!r} "
                f"db_path={self.db_path!r}"
            )

    @property
    def is_ready(self) -> bool:
        """Whether the SQLite connection has been initialized."""
        return self._db is not None

    def _require_db(self) -> aiosqlite.Connection:
        """Return the active DB connection or raise a descriptive error."""
        if self._db is None:
            raise RuntimeError(
                f"OPCStore database not initialized (db_path={self.db_path!r}). "
                "Call await store.initialize() before using store methods."
            )
        return self._db

    async def ensure_ready(self) -> None:
        """Initialize the store if not already connected."""
        if self._db is None:
            await self.initialize()

    async def initialize(self, *, run_startup_maintenance: bool = True) -> None:
        """Open the store.

        ``run_startup_maintenance`` is reserved for the owning OpenOPC runtime
        process. Lightweight collaboration clients such as ``opc-collab`` open
        the already-initialized project DB to service one tool call; they must
        not run schema migrations or stale-claim sweeps as a side effect of a
        read-only command.
        """
        if run_startup_maintenance:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        elif not Path(self.db_path).exists():
            raise FileNotFoundError(f"OPCStore database does not exist: {self.db_path}")
        self._db = _SQLiteConnectionAdapter(self.db_path)
        if not run_startup_maintenance:
            return
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()
        await self._ensure_schema()
        await self._sweep_stale_claims()
        await self._migrate_drop_runtime_topology_version_columns()
        # Fix 5 PR2: merge every (run_id, role_id) group into a single
        # canonical row with PK = ``role-runtime::{run_id}::{role_id}``.
        # Combines inbox, memory_slices, list fields, and picks the most
        # recent state for scalar fields — so historical data survives
        # the collapse. Subsumes the old Fix-2 duplicate-collapse and
        # _no_team-sentinel migrations (the 3-segment canonical form
        # eliminates both problems at the source).
        await self._migrate_role_sessions_merge_by_role()
        await self._migrate_work_item_runtime_metadata()
        await self._migrate_work_item_projection_metadata()
        await self._purge_cross_project_runtime_rows()
        await self._validate_work_item_runtime_links()
        await self._ensure_indexes()

    async def _table_columns(self, table: str) -> list[str]:
        assert self._db is not None
        if not await self._table_exists(table):
            return []
        async with self._db.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
        return [str(row[1]) for row in rows]

    async def _purge_cross_project_runtime_rows(self) -> dict[str, int]:
        """Remove rows that were written into the wrong project-scoped DB.

        Project databases under ``.opc/projects/<project_id>/tasks.db`` are
        single-project stores. A row whose explicit ``project_id`` names a
        different project is corruption from a prior mutable-store race; keeping
        it can break canonical WorkItem validation and project switching.
        """
        if self._db is None:
            return {}
        project_id = str(self.project_id or "").strip()
        if not project_id:
            return {}
        deleted: dict[str, int] = {}
        for table in ("external_sessions", "runtime_sessions", "tasks"):
            if not await self._table_exists(table):
                continue
            columns = set(await self._table_columns(table))
            if "project_id" not in columns:
                continue
            cursor = await self._db.execute(
                f"""DELETE FROM {table}
                    WHERE project_id IS NOT NULL
                      AND TRIM(project_id) != ''
                      AND project_id != ?""",
                (project_id,),
            )
            count = int(getattr(cursor, "rowcount", 0) or 0)
            if count:
                deleted[table] = count
        if deleted:
            await self._db.commit()
            logger.warning(
                "Purged cross-project runtime rows from project store: "
                f"project_id={project_id} db_path={self.db_path} deleted={deleted}"
            )
        return deleted

    async def _migrate_drop_runtime_topology_version_columns(self) -> None:
        # runtime_topology_version is no longer tracked: reorg never increments it,
        # snapshots never read it, and gates aren't keyed off it. Drop the
        # vestigial columns from legacy DBs (idempotent).
        assert self._db
        for table, column in (
            ("reorg_proposals", "old_runtime_topology_version"),
            ("reorg_proposals", "new_runtime_topology_version"),
            ("org_snapshots", "runtime_topology_version"),
        ):
            async with self._db.execute(f"PRAGMA table_info({table})") as cursor:
                rows = await cursor.fetchall()
            cols = {row[1] for row in rows}
            if column in cols:
                try:
                    await self._db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                except Exception as exc:
                    logger.warning(f"Failed to drop {table}.{column}: {exc}")
        await self._db.commit()

    async def _migrate_work_item_runtime_metadata(self) -> dict[str, int]:
        """Normalize canonical company work-item runtime metadata."""
        if self._db is None:
            return {}

        targets = (
            ("tasks", "id"),
            ("delegation_runs", "run_id"),
            ("delegation_cells", "cell_id"),
            ("delegation_work_items", "work_item_id"),
            ("team_instances", "team_instance_id"),
            ("seat_states", "seat_state_id"),
            ("role_runtime_sessions", "role_session_id"),
            ("delegation_role_sessions", "role_session_id"),
        )
        stats: dict[str, int] = {}
        for table, key_column in targets:
            stats[table] = await self._migrate_work_item_runtime_metadata_table(
                table=table,
                key_column=key_column,
            )
        changed = sum(stats.values())
        if changed:
            await self._db.commit()
            logger.info(f"work-item runtime metadata migration: updated {changed} rows")
        return stats

    async def _migrate_work_item_runtime_metadata_table(
        self,
        *,
        table: str,
        key_column: str,
    ) -> int:
        assert self._db is not None
        if not await self._table_exists(table):
            return 0
        async with self._db.execute(f"PRAGMA table_info({table})") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if key_column not in columns or "metadata" not in columns:
            return 0

        async with self._db.execute(
            f"""SELECT {key_column}, metadata
                FROM {table}
                WHERE metadata LIKE '%work_item_runtime%'"""
        ) as cursor:
            rows = await cursor.fetchall()

        updated = 0
        for row_id, metadata_json in rows:
            try:
                metadata = _json_loads(metadata_json, {})
            except Exception as exc:
                logger.warning(
                    f"work-item runtime metadata migration: skipping invalid "
                    f"{table}.{key_column}={row_id}: {exc}"
                )
                continue
            if not isinstance(metadata, dict):
                continue
            migrated, changed = migrate_work_item_runtime_metadata(metadata)
            if not changed:
                continue
            await self._db.execute(
                f"UPDATE {table} SET metadata=? WHERE {key_column}=?",
                (_json_dumps(migrated), row_id),
            )
            updated += 1
        return updated

    async def _migrate_work_item_projection_metadata(self) -> dict[str, int]:
        """Normalize canonical work-item projection identity metadata."""
        if self._db is None:
            return {}
        stats = {
            "tasks": await self._migrate_task_projection_metadata(),
            "delegation_work_items": await self._migrate_delegation_work_item_projection_metadata(),
        }
        changed = sum(stats.values())
        if changed:
            await self._db.commit()
            logger.info(f"work-item projection metadata migration: updated {changed} rows")
        return stats

    async def _migrate_task_projection_metadata(self) -> int:
        assert self._db is not None
        if not await self._table_exists("tasks"):
            return 0
        async with self._db.execute("PRAGMA table_info(tasks)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "id" not in columns or "metadata" not in columns:
            return 0
        async with self._db.execute(
            """SELECT id, metadata
               FROM tasks
               WHERE metadata LIKE '%work_item_projection_id%'
                  OR metadata LIKE '%work_item_turn_type%'"""
        ) as cursor:
            rows = await cursor.fetchall()
        updated = 0
        for task_id, metadata_json in rows:
            try:
                metadata = _json_loads(metadata_json, {})
            except Exception as exc:
                logger.warning(
                    f"work-item projection metadata migration: skipping invalid task {task_id}: {exc}"
                )
                continue
            if not isinstance(metadata, dict):
                continue
            migrated, changed = migrate_work_item_projection_metadata(metadata)
            if not changed:
                continue
            await self._db.execute(
                "UPDATE tasks SET metadata=? WHERE id=?",
                (_json_dumps(migrated), task_id),
            )
            updated += 1
        return updated

    async def _migrate_delegation_work_item_projection_metadata(self) -> int:
        assert self._db is not None
        if not await self._table_exists("delegation_work_items"):
            return 0
        async with self._db.execute("PRAGMA table_info(delegation_work_items)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        required = {"work_item_id", "projection_id", "kind", "metadata"}
        if not required.issubset(columns):
            return 0
        async with self._db.execute(
            """SELECT work_item_id, projection_id, kind, metadata
               FROM delegation_work_items
               WHERE COALESCE(projection_id, '') != ''
                  OR COALESCE(kind, '') != ''
                  OR metadata LIKE '%work_item_projection_id%'
                  OR metadata LIKE '%work_item_turn_type%'"""
        ) as cursor:
            rows = await cursor.fetchall()
        updated = 0
        for work_item_id, projection_id, kind, metadata_json in rows:
            try:
                metadata = _json_loads(metadata_json, {})
            except Exception as exc:
                logger.warning(
                    f"work-item projection metadata migration: skipping invalid work item {work_item_id}: {exc}"
                )
                continue
            if not isinstance(metadata, dict):
                continue
            migrated, changed = migrate_work_item_projection_metadata(
                metadata,
                projection_id_fallback=str(projection_id or work_item_id or "").strip(),
                turn_type_fallback=str(kind or "execute").strip(),
            )
            if not changed:
                continue
            await self._db.execute(
                "UPDATE delegation_work_items SET metadata=? WHERE work_item_id=?",
                (_json_dumps(migrated), work_item_id),
            )
            updated += 1
        return updated

    async def _task_exists(self, task_id: str) -> bool:
        assert self._db is not None
        tid = str(task_id or "").strip()
        if not tid:
            return False
        async with self._db.execute("SELECT 1 FROM tasks WHERE id=? LIMIT 1", (tid,)) as cursor:
            return await cursor.fetchone() is not None

    async def _work_item_exists(self, work_item_id: str) -> bool:
        assert self._db is not None
        wid = str(work_item_id or "").strip()
        if not wid:
            return False
        async with self._db.execute(
            "SELECT 1 FROM delegation_work_items WHERE work_item_id=? LIMIT 1",
            (wid,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _runtime_link_task_id_for_work_item(self, work_item_id: str) -> str:
        assert self._db is not None
        wid = str(work_item_id or "").strip()
        if not wid:
            return ""
        async with self._db.execute(
            "SELECT runtime_task_id FROM work_item_runtime_links WHERE work_item_id=?",
            (wid,),
        ) as cursor:
            row = await cursor.fetchone()
        return str(row[0] or "").strip() if row else ""

    async def _runtime_link_work_item_id_for_task(self, task_id: str) -> str:
        assert self._db is not None
        tid = str(task_id or "").strip()
        if not tid:
            return ""
        async with self._db.execute(
            "SELECT work_item_id FROM work_item_runtime_links WHERE runtime_task_id=?",
            (tid,),
        ) as cursor:
            row = await cursor.fetchone()
        return str(row[0] or "").strip() if row else ""

    async def _get_task_unhydrated(self, task_id: str) -> Task | None:
        assert self._db is not None
        tid = str(task_id or "").strip()
        if not tid:
            return None
        async with self._db.execute("SELECT * FROM tasks WHERE id = ?", (tid,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_task(row, cursor.description)

    async def _task_status_for_id(self, task_id: str) -> str:
        assert self._db is not None
        tid = str(task_id or "").strip()
        if not tid:
            return ""
        async with self._db.execute(
            "SELECT status FROM tasks WHERE id=? LIMIT 1",
            (tid,),
        ) as cursor:
            row = await cursor.fetchone()
        return str(row[0] or "").strip().lower() if row else ""

    @staticmethod
    def _terminal_task_statuses() -> set[str]:
        return {
            TaskStatus.DONE.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
        }

    async def _runtime_task_link_is_replaceable(self, task_id: str) -> bool:
        status = await self._task_status_for_id(task_id)
        return not status or status in self._terminal_task_statuses()

    @staticmethod
    def _runtime_task_matches_work_item(
        task: Task,
        item: DelegationWorkItem,
        *,
        preferred_task_id: str = "",
    ) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        wid = str(getattr(item, "work_item_id", "") or "").strip()
        if not wid:
            return False

        preferred = str(preferred_task_id or "").strip()
        if not is_work_item_runtime_metadata(metadata) and str(getattr(task, "id", "") or "").strip() != preferred:
            return False

        projection_id = projection_id_for_work_item(item)
        task_projection_id = str(metadata.get(WORK_ITEM_PROJECTION_ID_KEY, "") or "").strip()
        if projection_id and task_projection_id != projection_id:
            return False

        item_run_id = str(getattr(item, "run_id", "") or "").strip()
        task_run_id = str(metadata.get("delegation_run_id", "") or "").strip()
        if item_run_id and task_run_id != item_run_id:
            return False

        item_role_id = str(getattr(item, "role_id", "") or "").strip()
        task_role_id = str(
            metadata.get("work_item_role_id", "")
            or metadata.get("role_id", "")
            or getattr(task, "assigned_to", "")
            or ""
        ).strip()
        if item_role_id and task_role_id and task_role_id != item_role_id:
            return False

        item_seat_id = str(
            getattr(item, "seat_id", "")
            or dict(getattr(item, "metadata", {}) or {}).get("seat_id", "")
            or ""
        ).strip()
        task_seat_id = str(
            metadata.get("delegation_seat_id", "")
            or metadata.get("seat_id", "")
            or ""
        ).strip()
        if item_seat_id and task_seat_id and task_seat_id != item_seat_id:
            return False

        return True

    async def _write_work_item_runtime_link(
        self,
        work_item_id: str,
        runtime_task_id: str,
        *,
        link_kind: str = "primary",
        commit: bool = True,
    ) -> bool:
        assert self._db is not None
        wid = str(work_item_id or "").strip()
        tid = str(runtime_task_id or "").strip()
        if not wid or not tid:
            return False
        now = datetime.now().isoformat()
        # runtime_task_id is UNIQUE. If stale legacy data points this Task at
        # another WorkItem, the explicit WorkItem link wins.
        await self._db.execute(
            "DELETE FROM work_item_runtime_links WHERE runtime_task_id=? AND work_item_id != ?",
            (tid, wid),
        )
        await self._db.execute(
            """INSERT INTO work_item_runtime_links
               (work_item_id, runtime_task_id, link_kind, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(work_item_id) DO UPDATE SET
                   runtime_task_id=excluded.runtime_task_id,
                   link_kind=excluded.link_kind,
                   updated_at=excluded.updated_at""",
            (wid, tid, str(link_kind or "primary").strip() or "primary", now, now),
        )
        if commit:
            await self._db.commit()
        return True

    async def _validate_work_item_runtime_links(self) -> dict[str, int]:
        """Validate canonical WorkItem/Task links.

        WorkItem is the business source of truth; runtime Tasks are connected
        only through ``work_item_runtime_links``.
        """
        if self._db is None:
            return {}
        required_tables = ("tasks", "delegation_work_items", "work_item_runtime_links")
        for table in required_tables:
            if not await self._table_exists(table):
                return {"existing": 0, "missing": 0}

        stats = {"existing": 0, "missing": 0}
        diagnostics: list[str] = []
        linked_task_ids: set[str] = set()

        async with self._db.execute(
            "SELECT work_item_id, runtime_task_id FROM work_item_runtime_links"
        ) as cursor:
            for work_item_id, task_id in await cursor.fetchall():
                wid = str(work_item_id or "").strip()
                tid = str(task_id or "").strip()
                if not wid or not tid:
                    continue
                stats["existing"] += 1
                linked_task_ids.add(tid)
                if not await self._work_item_exists(wid):
                    diagnostics.append(f"runtime link points to missing WorkItem: work_item={wid} task={tid}")
                if not await self._task_exists(tid):
                    diagnostics.append(f"runtime link points to missing Task: work_item={wid} task={tid}")

        async with self._db.execute(
            """SELECT id, metadata
               FROM tasks
               WHERE metadata LIKE '%work_item_runtime%'
                 AND metadata LIKE '%work_item_projection_id%'"""
        ) as cursor:
            projection_rows = await cursor.fetchall()
        for task_id, metadata_json in projection_rows:
            tid = str(task_id or "").strip()
            try:
                metadata = _json_loads(metadata_json, {})
            except Exception as exc:
                diagnostics.append(f"invalid company runtime projection metadata: task={tid} error={exc}")
                continue
            if not isinstance(metadata, dict) or not is_work_item_runtime_metadata(metadata):
                continue
            projection_id = str(metadata.get(WORK_ITEM_PROJECTION_ID_KEY, "") or "").strip()
            if projection_id and tid not in linked_task_ids:
                stats["missing"] += 1
                diagnostics.append(
                    "company runtime projection missing canonical link: "
                    f"task={tid} projection={projection_id}"
                )

        if diagnostics:
            sample = "; ".join(diagnostics[:12])
            suffix = "" if len(diagnostics) <= 12 else f"; ... {len(diagnostics) - 12} more"
            raise RuntimeError(
                "canonical WorkItem runtime link validation failed "
                f"(db_path={self.db_path}): {sample}{suffix}"
            )

        return stats

    async def _create_tables(self) -> None:
        assert self._db
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                parent_session_id TEXT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                assigned_to TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                priority INTEGER DEFAULT 5,
                dependencies TEXT DEFAULT '[]',
                execution_lock INTEGER DEFAULT 0,
                context_snapshot TEXT DEFAULT '{}',
                assigned_external_agent TEXT,
                created_at TEXT NOT NULL,
                deadline TEXT,
                result TEXT,
                parent_id TEXT,
                project_id TEXT DEFAULT 'default',
                tags TEXT DEFAULT '[]',
                comments TEXT DEFAULT '[]',
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                metadata TEXT DEFAULT '{}',
                org_id TEXT,
                goal_id TEXT,
                checkout_run_id TEXT,
                execution_locked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_messages (
                msg_id TEXT PRIMARY KEY,
                msg_type TEXT NOT NULL,
                from_agent TEXT NOT NULL,
                to_agents TEXT NOT NULL,
                subject TEXT DEFAULT '',
                body TEXT DEFAULT '',
                context_ref TEXT,
                urgency TEXT DEFAULT 'normal',
                reply_needed INTEGER DEFAULT 0,
                requires_ack INTEGER DEFAULT 0,
                timeout_action TEXT,
                reply_to_msg_id TEXT,
                task_id TEXT,
                status TEXT DEFAULT 'sent',
                timestamp TEXT NOT NULL,
                processed_at TEXT,
                metadata TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS meetings (
                room_id TEXT PRIMARY KEY,
                task_id TEXT,
                topic TEXT NOT NULL,
                participants TEXT NOT NULL,
                shared_context TEXT DEFAULT '',
                agenda TEXT DEFAULT '[]',
                max_rounds INTEGER DEFAULT 5,
                decision_owner TEXT DEFAULT 'coordinator',
                status TEXT DEFAULT 'open',
                decision_method TEXT DEFAULT '',
                current_round INTEGER DEFAULT 0,
                pending_participants TEXT DEFAULT '[]',
                consensus TEXT DEFAULT '{}',
                outcome TEXT,
                transcript TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                deadline_at TEXT
            );

            CREATE TABLE IF NOT EXISTS work_item_decisions (
                decision_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                task_id TEXT,
                role_id TEXT DEFAULT '',
                projection_id TEXT DEFAULT '',
                category TEXT DEFAULT 'general',
                summary TEXT DEFAULT '',
                details TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artifact_records (
                artifact_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                task_id TEXT,
                projection_id TEXT DEFAULT '',
                role_id TEXT DEFAULT '',
                name TEXT DEFAULT '',
                artifact_type TEXT DEFAULT 'generic',
                location TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                details TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_memory (
                memory_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                role_id TEXT NOT NULL,
                scope TEXT DEFAULT 'project',
                summary TEXT DEFAULT '',
                details TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS handoff_records (
                handoff_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT,
                task_id TEXT,
                from_role TEXT DEFAULT '',
                to_role TEXT DEFAULT '',
                source_projection_id TEXT DEFAULT '',
                target_projection_id TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                payload TEXT DEFAULT '{}',
                requires_ack INTEGER DEFAULT 0,
                status TEXT DEFAULT 'sent',
                received_at TEXT,
                acked_at TEXT,
                accepted_at TEXT,
                rejected_at TEXT,
                response_summary TEXT DEFAULT '',
                ack_message_id TEXT,
                response_message_id TEXT,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS delegation_runs (
                run_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT NOT NULL,
                company_profile TEXT DEFAULT 'corporate',
                execution_model TEXT DEFAULT 'recursive_delegation',
                final_decider_role_id TEXT DEFAULT '',
                top_level_role_ids TEXT DEFAULT '[]',
                status TEXT DEFAULT 'pending',
                lifecycle_status TEXT DEFAULT 'active',
                current_revision INTEGER DEFAULT 1,
                latest_deliverable_summary TEXT DEFAULT '',
                recovery_pointer TEXT DEFAULT '{}',
                project_dossier TEXT DEFAULT '{}',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS delegation_cells (
                cell_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                manager_role_id TEXT DEFAULT '',
                member_role_ids TEXT DEFAULT '[]',
                status TEXT DEFAULT 'idle',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS delegation_work_items (
                work_item_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                cell_id TEXT NOT NULL,
                team_instance_id TEXT DEFAULT '',
                team_id TEXT DEFAULT '',
                role_id TEXT DEFAULT '',
                seat_id TEXT DEFAULT '',
                seat_state_id TEXT DEFAULT '',
                role_runtime_session_id TEXT DEFAULT '',
                parent_work_item_id TEXT,
                source_role_id TEXT,
                source_seat_id TEXT,
                title TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                kind TEXT DEFAULT 'execute',
                projection_id TEXT DEFAULT '',
                phase TEXT NOT NULL DEFAULT 'ready',
                batch_id TEXT DEFAULT '',
                batch_index INTEGER DEFAULT 0,
                deliverable_summary TEXT DEFAULT '',
                blocked_reason TEXT DEFAULT '',
                handoff_status TEXT DEFAULT 'pending',
                continuation_source TEXT DEFAULT '',
                manager_role_id TEXT DEFAULT '',
                manager_seat_id TEXT DEFAULT '',
                claimed_by_role_runtime_session_id TEXT DEFAULT '',
                claimed_by_seat_id TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS work_item_runtime_links (
                work_item_id TEXT PRIMARY KEY,
                runtime_task_id TEXT NOT NULL UNIQUE,
                link_kind TEXT DEFAULT 'primary',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(work_item_id) REFERENCES delegation_work_items(work_item_id) ON DELETE CASCADE,
                FOREIGN KEY(runtime_task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS delegation_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                work_item_id TEXT,
                cell_id TEXT,
                role_id TEXT,
                event_type TEXT NOT NULL,
                payload TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS delegation_role_sessions (
                role_session_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                team_instance_id TEXT DEFAULT '',
                team_id TEXT DEFAULT '',
                role_id TEXT NOT NULL,
                seat_id TEXT DEFAULT '',
                seat_state_id TEXT DEFAULT '',
                employee_id TEXT DEFAULT '',
                focused_work_item_id TEXT DEFAULT '',
                background_work_item_ids TEXT DEFAULT '[]',
                manager_role_ids TEXT DEFAULT '[]',
                manager_seat_ids TEXT DEFAULT '[]',
                seat_ids TEXT DEFAULT '[]',
                adapter_session_state TEXT DEFAULT '{}',
                inbox_state TEXT DEFAULT '{}',
                memory_slices_by_work_item TEXT DEFAULT '{}',
                resume_state TEXT DEFAULT '{}',
                current_work_item TEXT DEFAULT '{}',
                latest_notification TEXT DEFAULT '{}',
                manager_digest TEXT DEFAULT '{}',
                status TEXT DEFAULT 'idle',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_instances (
                team_instance_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                team_id TEXT NOT NULL,
                session_id TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                seat_ids TEXT DEFAULT '[]',
                role_ids TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS seat_states (
                seat_state_id TEXT PRIMARY KEY,
                team_instance_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                team_id TEXT NOT NULL,
                seat_id TEXT NOT NULL,
                role_id TEXT DEFAULT '',
                employee_id TEXT DEFAULT '',
                member_session_id TEXT DEFAULT '',
                role_runtime_session_id TEXT DEFAULT '',
                status TEXT DEFAULT 'idle',
                resident_status TEXT DEFAULT 'idle',
                current_task_id TEXT DEFAULT '',
                current_work_item_id TEXT DEFAULT '',
                manager_role_id TEXT DEFAULT '',
                manager_seat_id TEXT DEFAULT '',
                manager_role_ids TEXT DEFAULT '[]',
                manager_seat_ids TEXT DEFAULT '[]',
                inbox_state TEXT DEFAULT '{}',
                resume_state TEXT DEFAULT '{}',
                current_work_item TEXT DEFAULT '{}',
                latest_notification TEXT DEFAULT '{}',
                manager_digest TEXT DEFAULT '{}',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_runtime_sessions (
                role_session_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                team_instance_id TEXT DEFAULT '',
                team_id TEXT DEFAULT '',
                role_id TEXT NOT NULL,
                seat_id TEXT DEFAULT '',
                seat_state_id TEXT DEFAULT '',
                employee_id TEXT DEFAULT '',
                focused_work_item_id TEXT DEFAULT '',
                background_work_item_ids TEXT DEFAULT '[]',
                manager_role_ids TEXT DEFAULT '[]',
                manager_seat_ids TEXT DEFAULT '[]',
                seat_ids TEXT DEFAULT '[]',
                adapter_session_state TEXT DEFAULT '{}',
                inbox_state TEXT DEFAULT '{}',
                memory_slices_by_work_item TEXT DEFAULT '{}',
                resume_state TEXT DEFAULT '{}',
                current_work_item TEXT DEFAULT '{}',
                latest_notification TEXT DEFAULT '{}',
                manager_digest TEXT DEFAULT '{}',
                status TEXT DEFAULT 'idle',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reorg_proposals (
                proposal_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT,
                task_id TEXT,
                initiated_by TEXT DEFAULT 'owner',
                source_role_id TEXT DEFAULT '',
                scope TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                rationale TEXT DEFAULT '',
                user_confirmation_required INTEGER DEFAULT 1,
                old_org_version INTEGER DEFAULT 1,
                new_org_version INTEGER DEFAULT 1,
                changeset TEXT DEFAULT '{}',
                migration_plan TEXT DEFAULT '{}',
                impact_summary TEXT DEFAULT '{}',
                approval_notes TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS org_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                org_version INTEGER DEFAULT 1,
                company_name TEXT DEFAULT '',
                topology TEXT DEFAULT '',
                roles TEXT DEFAULT '[]',
                company_profile TEXT DEFAULT 'corporate',
                active_tasks TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reorg_events (
                event_id TEXT PRIMARY KEY,
                proposal_id TEXT DEFAULT '',
                project_id TEXT DEFAULT 'default',
                event_kind TEXT NOT NULL,
                summary TEXT DEFAULT '',
                details TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload TEXT DEFAULT '{}',
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cost_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                agent_id TEXT,
                model TEXT,
                tokens_in INTEGER DEFAULT 0,
                tokens_out INTEGER DEFAULT 0,
                cost REAL DEFAULT 0.0,
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS approval_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                project_id TEXT DEFAULT 'default',
                action_kind TEXT NOT NULL,
                action_name TEXT NOT NULL,
                target_agent TEXT DEFAULT '',
                decision_action TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                confidence REAL DEFAULT 0.0,
                rationale TEXT DEFAULT '',
                policy_source TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS external_sessions (
                session_key TEXT PRIMARY KEY,
                agent_type TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                session_id TEXT NOT NULL,
                opc_session_id TEXT,
                task_id TEXT,
                workspace_path TEXT DEFAULT '',
                run_mode TEXT DEFAULT 'batch',
                status TEXT DEFAULT 'unknown',
                metadata TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS execution_checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT,
                checkpoint_type TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                task_id TEXT,
                payload TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_sessions (
                runtime_session_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT,
                task_id TEXT,
                status TEXT DEFAULT 'running',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_events (
                event_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_transcript_entries (
                entry_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                task_id TEXT,
                session_id TEXT,
                message_id TEXT DEFAULT '',
                role TEXT DEFAULT 'assistant',
                entry_type TEXT DEFAULT 'message',
                content TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_tool_calls (
                call_record_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                task_id TEXT,
                session_id TEXT,
                message_id TEXT DEFAULT '',
                tool_call_id TEXT NOT NULL,
                tool_name TEXT DEFAULT '',
                arguments TEXT DEFAULT '{}',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_tool_results (
                result_record_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                task_id TEXT,
                session_id TEXT,
                message_id TEXT DEFAULT '',
                tool_call_id TEXT DEFAULT '',
                tool_name TEXT DEFAULT '',
                payload TEXT DEFAULT '{}',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_permission_grants (
                grant_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                scope TEXT DEFAULT 'once',
                tool_name TEXT DEFAULT '',
                candidate TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_subagent_runs (
                subagent_run_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                task_id TEXT,
                agent_id TEXT NOT NULL,
                profile TEXT DEFAULT 'general',
                status TEXT DEFAULT 'running',
                worktree_path TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_worktree_sessions (
                worktree_session_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                task_id TEXT,
                path TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_compaction_boundaries (
                boundary_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                task_id TEXT,
                summary TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                parent_session_id TEXT,
                title TEXT DEFAULT '',
                mode TEXT DEFAULT 'primary',
                status TEXT DEFAULT 'active',
                summary TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_messages (
                message_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                task_id TEXT,
                agent_id TEXT,
                parent_message_id TEXT,
                summary_flag INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_parts (
                part_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                part_type TEXT NOT NULL,
                payload TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_compactions (
                compaction_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                compaction_message_id TEXT NOT NULL,
                source_boundary_message_id TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_memory_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT NOT NULL,
                summary_message_id TEXT NOT NULL,
                source_boundary_message_id TEXT NOT NULL,
                summary_text TEXT DEFAULT '',
                memory_text TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_compactions (
                compaction_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT NOT NULL,
                employee_id TEXT NOT NULL,
                role_id TEXT DEFAULT '',
                compaction_message_id TEXT NOT NULL,
                source_boundary_message_id TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_memory_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT NOT NULL,
                employee_id TEXT NOT NULL,
                role_id TEXT DEFAULT '',
                memory_scope TEXT DEFAULT 'session',
                memory_kind TEXT DEFAULT 'process',
                summary_message_id TEXT NOT NULL,
                source_boundary_message_id TEXT NOT NULL,
                summary_text TEXT DEFAULT '',
                memory_text TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_links (
                link_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT NOT NULL,
                linked_session_id TEXT,
                task_id TEXT,
                link_type TEXT DEFAULT 'child_session',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS organizations (
                org_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                company_profile TEXT DEFAULT 'corporate',
                budget_monthly_cents INTEGER DEFAULT 0,
                spent_monthly_cents INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS goals (
                goal_id TEXT PRIMARY KEY,
                org_id TEXT NOT NULL,
                parent_id TEXT,
                owner_agent_id TEXT,
                level TEXT DEFAULT 'task',
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                priority INTEGER DEFAULT 5,
                deadline TEXT,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS org_agents (
                agent_id TEXT PRIMARY KEY,
                org_id TEXT NOT NULL,
                role_id TEXT NOT NULL,
                name TEXT DEFAULT '',
                reports_to TEXT,
                budget_monthly_cents INTEGER DEFAULT 0,
                spent_monthly_cents INTEGER DEFAULT 0,
                heartbeat_enabled INTEGER DEFAULT 0,
                heartbeat_interval_sec INTEGER DEFAULT 300,
                last_heartbeat_at TEXT,
                status TEXT DEFAULT 'idle',
                capabilities TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cost_events (
                event_id TEXT PRIMARY KEY,
                org_id TEXT,
                agent_id TEXT,
                task_id TEXT,
                model TEXT DEFAULT '',
                tokens_in INTEGER DEFAULT 0,
                tokens_out INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0,
                timestamp TEXT NOT NULL
            );
        """)
        await self._db.commit()

    async def _ensure_schema(self) -> None:
        assert self._db
        await self._ensure_external_session_layout()
        await self._ensure_columns(
            "tasks",
            {
                "session_id": "TEXT",
                "parent_session_id": "TEXT",
                "description": "TEXT DEFAULT ''",
                "assigned_to": "TEXT DEFAULT ''",
                "status": "TEXT DEFAULT 'pending'",
                "priority": "INTEGER DEFAULT 5",
                "dependencies": "TEXT DEFAULT '[]'",
                "execution_lock": "INTEGER DEFAULT 0",
                "context_snapshot": "TEXT DEFAULT '{}'",
                "assigned_external_agent": "TEXT",
                "deadline": "TEXT",
                "result": "TEXT",
                "parent_id": "TEXT",
                "project_id": "TEXT DEFAULT 'default'",
                "tags": "TEXT DEFAULT '[]'",
                "comments": "TEXT DEFAULT '[]'",
                "retry_count": "INTEGER DEFAULT 0",
                "max_retries": "INTEGER DEFAULT 3",
                "metadata": "TEXT DEFAULT '{}'",
                "org_id": "TEXT",
                "goal_id": "TEXT",
                "checkout_run_id": "TEXT",
                "execution_locked_at": "TEXT",
            },
        )
        await self._ensure_columns(
            "agent_messages",
            {
                "requires_ack": "INTEGER DEFAULT 0",
                "reply_to_msg_id": "TEXT",
                "task_id": "TEXT",
                "status": "TEXT DEFAULT 'sent'",
                "processed_at": "TEXT",
                "metadata": "TEXT DEFAULT '{}'",
            },
        )
        await self._ensure_columns(
            "meetings",
            {
                "task_id": "TEXT",
                "status": "TEXT DEFAULT 'open'",
                "decision_method": "TEXT DEFAULT ''",
                "current_round": "INTEGER DEFAULT 0",
                "pending_participants": "TEXT DEFAULT '[]'",
                "consensus": "TEXT DEFAULT '{}'",
                "metadata": "TEXT DEFAULT '{}'",
                "updated_at": "TEXT",
                "last_activity_at": "TEXT",
                "deadline_at": "TEXT",
            },
        )
        await self._ensure_columns(
            "handoff_records",
            {
                "session_id": "TEXT",
                "source_work_item_id": "TEXT DEFAULT ''",
                "target_work_item_id": "TEXT DEFAULT ''",
                "requires_ack": "INTEGER DEFAULT 0",
                "status": "TEXT DEFAULT 'sent'",
                "received_at": "TEXT",
                "acked_at": "TEXT",
                "accepted_at": "TEXT",
                "rejected_at": "TEXT",
                "response_summary": "TEXT DEFAULT ''",
                "ack_message_id": "TEXT",
                "response_message_id": "TEXT",
                "metadata": "TEXT DEFAULT '{}'",
            },
        )
        await self._ensure_columns(
            "external_sessions",
            {
                "session_key": "TEXT",
                "opc_session_id": "TEXT",
            },
        )
        await self._ensure_columns(
            "execution_checkpoints",
            {
                "session_id": "TEXT",
            },
        )
        await self._ensure_columns(
            "agent_memory_snapshots",
            {
                "memory_scope": "TEXT DEFAULT 'session'",
            },
        )
        await self._ensure_columns(
            "delegation_runs",
            {
                "company_profile": "TEXT DEFAULT 'corporate'",
                "lifecycle_status": "TEXT DEFAULT 'active'",
                "current_revision": "INTEGER DEFAULT 1",
                "latest_deliverable_summary": "TEXT DEFAULT ''",
                "recovery_pointer": "TEXT DEFAULT '{}'",
                "project_dossier": "TEXT DEFAULT '{}'",
            },
        )
        await self._ensure_columns(
            "delegation_work_items",
            {
                "team_instance_id": "TEXT DEFAULT ''",
                "team_id": "TEXT DEFAULT ''",
                "role_id": "TEXT DEFAULT ''",
                "seat_id": "TEXT DEFAULT ''",
                "seat_state_id": "TEXT DEFAULT ''",
                "role_runtime_session_id": "TEXT DEFAULT ''",
                "parent_work_item_id": "TEXT",
                "source_role_id": "TEXT",
                "source_seat_id": "TEXT",
                "title": "TEXT DEFAULT ''",
                "summary": "TEXT DEFAULT ''",
                "kind": "TEXT DEFAULT 'execute'",
                "projection_id": "TEXT DEFAULT ''",
                "batch_id": "TEXT DEFAULT ''",
                "batch_index": "INTEGER DEFAULT 0",
                "deliverable_summary": "TEXT DEFAULT ''",
                "blocked_reason": "TEXT DEFAULT ''",
                "handoff_status": "TEXT DEFAULT 'pending'",
                "continuation_source": "TEXT DEFAULT ''",
                "manager_role_id": "TEXT DEFAULT ''",
                "manager_seat_id": "TEXT DEFAULT ''",
                "claimed_by_role_runtime_session_id": "TEXT DEFAULT ''",
                "claimed_by_seat_id": "TEXT DEFAULT ''",
                "metadata": "TEXT DEFAULT '{}'",
                # Added during the Phase unification refactor. SQLite
                # ALTER TABLE ADD COLUMN cannot enforce NOT NULL on a
                # populated table, so we use 'ready' as the default and
                # rely on writes to fill in the canonical value.
                "phase": "TEXT DEFAULT 'ready'",
            },
        )
        await self._drop_mismatched_delegation_work_item_indexes()
        await self._ensure_columns(
            "delegation_role_sessions",
            {
                "project_id": "TEXT DEFAULT 'default'",
                "team_instance_id": "TEXT DEFAULT ''",
                "team_id": "TEXT DEFAULT ''",
                "seat_id": "TEXT DEFAULT ''",
                "seat_state_id": "TEXT DEFAULT ''",
                "manager_seat_ids": "TEXT DEFAULT '[]'",
                "seat_ids": "TEXT DEFAULT '[]'",
                "current_work_item": "TEXT DEFAULT '{}'",
                "latest_notification": "TEXT DEFAULT '{}'",
                "manager_digest": "TEXT DEFAULT '{}'",
                # Fix 5 PR3: per-role serial task queue.
                "pending_work_item_ids": "TEXT DEFAULT '[]'",
            },
        )
        await self._ensure_columns(
            "role_runtime_sessions",
            {
                "current_work_item": "TEXT DEFAULT '{}'",
                "latest_notification": "TEXT DEFAULT '{}'",
                "manager_digest": "TEXT DEFAULT '{}'",
                "pending_work_item_ids": "TEXT DEFAULT '[]'",
            },
        )
        await self._ensure_columns(
            "seat_states",
            {
                "current_work_item": "TEXT DEFAULT '{}'",
                "latest_notification": "TEXT DEFAULT '{}'",
                "manager_digest": "TEXT DEFAULT '{}'",
            },
        )

    async def _ensure_indexes(self) -> None:
        assert self._db
        # These indexes depend on columns that may be added by migrations for
        # older databases, so create them only after _ensure_schema() runs.
        await self._db.executescript("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_project_status_created ON tasks(project_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_project_priority_created ON tasks(project_id, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
            CREATE INDEX IF NOT EXISTS idx_messages_task ON agent_messages(task_id);
            CREATE INDEX IF NOT EXISTS idx_messages_status ON agent_messages(status);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON agent_messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_meetings_task ON meetings(task_id);
            CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
            CREATE INDEX IF NOT EXISTS idx_cost_task ON cost_records(task_id);
            CREATE INDEX IF NOT EXISTS idx_approval_project ON approval_records(project_id);
            CREATE INDEX IF NOT EXISTS idx_approval_name ON approval_records(action_name);
            CREATE INDEX IF NOT EXISTS idx_decisions_project ON work_item_decisions(project_id, projection_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifact_records(project_id, projection_id);
            CREATE INDEX IF NOT EXISTS idx_role_memory_project ON role_memory(project_id, role_id);
            CREATE INDEX IF NOT EXISTS idx_handoff_project ON handoff_records(project_id, target_projection_id);
            CREATE INDEX IF NOT EXISTS idx_handoff_status ON handoff_records(project_id, status, target_projection_id);
            CREATE INDEX IF NOT EXISTS idx_delegation_runs_session ON delegation_runs(session_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_delegation_runs_project_lifecycle ON delegation_runs(project_id, lifecycle_status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_delegation_cells_run ON delegation_cells(run_id, manager_role_id);
            CREATE INDEX IF NOT EXISTS idx_delegation_work_items_run ON delegation_work_items(run_id, phase, role_id);
            CREATE INDEX IF NOT EXISTS idx_delegation_work_items_team ON delegation_work_items(team_instance_id, team_id, seat_id, phase);
            CREATE INDEX IF NOT EXISTS idx_delegation_work_items_batch ON delegation_work_items(run_id, batch_id, batch_index, phase);
            CREATE INDEX IF NOT EXISTS idx_delegation_work_items_manager_board ON delegation_work_items(run_id, manager_seat_id, parent_work_item_id, phase);
            CREATE INDEX IF NOT EXISTS idx_work_item_runtime_links_task ON work_item_runtime_links(runtime_task_id);
            CREATE INDEX IF NOT EXISTS idx_work_item_runtime_links_kind ON work_item_runtime_links(link_kind, updated_at);
            CREATE INDEX IF NOT EXISTS idx_delegation_events_run ON delegation_events(run_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_delegation_role_sessions_run ON delegation_role_sessions(run_id, role_id, status);
            CREATE INDEX IF NOT EXISTS idx_delegation_role_sessions_team ON delegation_role_sessions(team_instance_id, team_id, seat_id, role_id, status);
            CREATE INDEX IF NOT EXISTS idx_team_instances_run ON team_instances(run_id, team_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_seat_states_team ON seat_states(team_instance_id, team_id, seat_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_role_runtime_sessions_team ON role_runtime_sessions(team_instance_id, team_id, seat_id, role_id, status);
            CREATE INDEX IF NOT EXISTS idx_reorg_proposals_project ON reorg_proposals(project_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_org_snapshots_project ON org_snapshots(project_id, org_version, created_at);
            CREATE INDEX IF NOT EXISTS idx_reorg_events_project ON reorg_events(project_id, proposal_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_checkpoint_project_status ON execution_checkpoints(project_id, status);
            CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_session_messages_session ON session_messages(session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_session_parts_session ON session_parts(session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_session_compactions_session ON session_compactions(session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_session_memory_snapshots_session ON session_memory_snapshots(session_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_agent_compactions_scope ON agent_compactions(project_id, session_id, employee_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_memory_snapshots_scope ON agent_memory_snapshots(project_id, session_id, employee_id, memory_kind, updated_at);
            CREATE INDEX IF NOT EXISTS idx_agent_memory_snapshots_scope_v2 ON agent_memory_snapshots(project_id, employee_id, memory_scope, memory_kind, updated_at);
            CREATE INDEX IF NOT EXISTS idx_session_links_session ON session_links(session_id, link_type);
            CREATE INDEX IF NOT EXISTS idx_external_sessions_agent_project ON external_sessions(agent_type, project_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_sessions_task ON runtime_sessions(task_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_events_session ON runtime_events(runtime_session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_transcript_session ON runtime_transcript_entries(runtime_session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_tool_calls_session ON runtime_tool_calls(runtime_session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_tool_results_session ON runtime_tool_results(runtime_session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_subagent_runs_session ON runtime_subagent_runs(runtime_session_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_worktrees_session ON runtime_worktree_sessions(runtime_session_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_organizations_status ON organizations(status);
            CREATE INDEX IF NOT EXISTS idx_goals_org ON goals(org_id, status);
            CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals(parent_id);
            CREATE INDEX IF NOT EXISTS idx_org_agents_org ON org_agents(org_id, status);
            CREATE INDEX IF NOT EXISTS idx_org_agents_reports_to ON org_agents(reports_to);
            CREATE INDEX IF NOT EXISTS idx_cost_events_org ON cost_events(org_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_cost_events_agent ON cost_events(agent_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_cost_events_task ON cost_events(task_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_org ON tasks(org_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_goal ON tasks(goal_id);
        """)
        await self._db.commit()

    async def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        assert self._db
        async with self._db.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
        existing = {row[1] for row in rows}
        for name, ddl in columns.items():
            if name in existing:
                continue
            await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        await self._db.commit()

    async def _drop_mismatched_delegation_work_item_indexes(self) -> None:
        assert self._db
        expected = {
            "idx_delegation_work_items_run": ["run_id", "phase", "role_id"],
            "idx_delegation_work_items_team": ["team_instance_id", "team_id", "seat_id", "phase"],
            "idx_delegation_work_items_batch": ["run_id", "batch_id", "batch_index", "phase"],
            "idx_delegation_work_items_manager_board": ["run_id", "manager_seat_id", "parent_work_item_id", "phase"],
        }
        async with self._db.execute("PRAGMA index_list(delegation_work_items)") as cursor:
            rows = await cursor.fetchall()
        existing = {str(row[1]) for row in rows}
        for index_name, expected_columns in expected.items():
            if index_name not in existing:
                continue
            async with self._db.execute(f"PRAGMA index_info({index_name})") as cursor:
                info = await cursor.fetchall()
            columns = [str(row[2]) for row in info]
            if columns != expected_columns:
                await self._db.execute(f"DROP INDEX IF EXISTS {index_name}")
        await self._db.commit()

    async def _ensure_external_session_layout(self) -> None:
        assert self._db
        async with self._db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'external_sessions'"
        ) as cursor:
            row = await cursor.fetchone()
        create_sql = row[0] if row else ""
        if create_sql and "PRIMARY KEY (agent_type, project_id)" not in create_sql:
            return
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS external_sessions_v2 (
                session_key TEXT PRIMARY KEY,
                agent_type TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                session_id TEXT NOT NULL,
                opc_session_id TEXT,
                task_id TEXT,
                workspace_path TEXT DEFAULT '',
                run_mode TEXT DEFAULT 'batch',
                status TEXT DEFAULT 'unknown',
                metadata TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL
            );
            INSERT OR REPLACE INTO external_sessions_v2
            (session_key, agent_type, project_id, session_id, opc_session_id, task_id, workspace_path, run_mode, status, metadata, updated_at)
            SELECT
                printf('%s|%s|%s|%s|%s', agent_type, project_id, ifnull(opc_session_id, ''), ifnull(task_id, ''), session_id),
                agent_type,
                project_id,
                session_id,
                opc_session_id,
                task_id,
                workspace_path,
                run_mode,
                status,
                metadata,
                updated_at
            FROM external_sessions;
            DROP TABLE external_sessions;
            ALTER TABLE external_sessions_v2 RENAME TO external_sessions;
            CREATE INDEX IF NOT EXISTS idx_external_sessions_agent_project ON external_sessions(agent_type, project_id, updated_at);
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _table_exists(self, table: str) -> bool:
        assert self._db
        async with self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ) as cursor:
            return await cursor.fetchone() is not None

    @staticmethod
    def _clean_text_ids(values: Any) -> set[str]:
        return {
            str(value or "").strip()
            for value in values
            if str(value or "").strip()
        }

    @staticmethod
    def _chunked_ids(values: set[str] | list[str], size: int = 400) -> list[list[str]]:
        ids = sorted(OPCStore._clean_text_ids(values))
        return [ids[index : index + size] for index in range(0, len(ids), size)]

    async def _fetch_text_column(
        self,
        query: str,
        params: tuple[Any, ...] | list[Any] = (),
    ) -> set[str]:
        assert self._db
        async with self._db.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()
        return self._clean_text_ids(row[0] for row in rows)

    async def _fetch_text_column_where_in(
        self,
        table: str,
        select_column: str,
        where_column: str,
        values: set[str] | list[str],
        *,
        extra_where: str = "",
    ) -> set[str]:
        assert self._db
        results: set[str] = set()
        for chunk in self._chunked_ids(values):
            placeholders = ", ".join("?" for _ in chunk)
            query = f"SELECT {select_column} FROM {table} WHERE {where_column} IN ({placeholders})"
            if extra_where:
                query += f" AND {extra_where}"
            async with self._db.execute(query, tuple(chunk)) as cursor:
                rows = await cursor.fetchall()
            results.update(self._clean_text_ids(row[0] for row in rows))
        return results

    async def _delete_where_in(
        self,
        table: str,
        column: str,
        values: set[str] | list[str],
    ) -> None:
        assert self._db
        for chunk in self._chunked_ids(values):
            placeholders = ", ".join("?" for _ in chunk)
            await self._db.execute(
                f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
                tuple(chunk),
            )

    async def _delete_events_by_payload_ids(
        self,
        json_path: str,
        values: set[str] | list[str],
    ) -> None:
        assert self._db
        for chunk in self._chunked_ids(values):
            placeholders = ", ".join("?" for _ in chunk)
            await self._db.execute(
                f"""
                DELETE FROM events
                WHERE json_valid(payload)
                  AND json_extract(payload, ?) IN ({placeholders})
                """,
                tuple([json_path, *chunk]),
            )

    async def _fetch_text_column_where_text_contains(
        self,
        table: str,
        select_column: str,
        text_column: str,
        values: set[str] | list[str],
    ) -> set[str]:
        assert self._db
        results: set[str] = set()
        for chunk in self._chunked_ids(values):
            conditions = " OR ".join(f"{text_column} LIKE ?" for _ in chunk)
            async with self._db.execute(
                f"SELECT {select_column} FROM {table} WHERE {conditions}",
                tuple(f"%{value}%" for value in chunk),
            ) as cursor:
                rows = await cursor.fetchall()
            results.update(self._clean_text_ids(row[0] for row in rows))
        return results

    async def _delete_where_text_contains(
        self,
        table: str,
        text_column: str,
        values: set[str] | list[str],
    ) -> None:
        assert self._db
        for chunk in self._chunked_ids(values):
            conditions = " OR ".join(f"{text_column} LIKE ?" for _ in chunk)
            await self._db.execute(
                f"DELETE FROM {table} WHERE {conditions}",
                tuple(f"%{value}%" for value in chunk),
            )

    async def _delete_by_json_path_or_text_ids(
        self,
        table: str,
        json_column: str,
        json_paths: tuple[str, ...],
        values: set[str] | list[str],
    ) -> None:
        assert self._db
        clean_values = self._clean_text_ids(values)
        if not clean_values:
            return
        for path in json_paths:
            for chunk in self._chunked_ids(clean_values):
                placeholders = ", ".join("?" for _ in chunk)
                await self._db.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE json_valid({json_column})
                      AND json_extract({json_column}, ?) IN ({placeholders})
                    """,
                    tuple([path, *chunk]),
                )
        await self._delete_where_text_contains(table, json_column, clean_values)

    async def _delete_company_runtime_artifacts_for_task(
        self,
        task_id: str,
        session_id: str | None,
        *,
        shared_session: bool,
    ) -> None:
        """Delete company-mode and runtime rows tied to one task/session.

        Top-level chat deletion removes the full delegation run for that
        session. Child work-item deletion removes only the linked work item and
        its runtime traces so a sibling/parent run is not destroyed.
        """
        if not self._db:
            return

        clean_task_id = str(task_id or "").strip()
        clean_session_id = str(session_id or "").strip()
        if not clean_task_id and not clean_session_id:
            return

        task_row: dict[str, Any] = {}
        if clean_task_id:
            async with self._db.execute(
                "SELECT id, session_id, parent_session_id, project_id, metadata FROM tasks WHERE id = ?",
                (clean_task_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    cols = [description[0] for description in cursor.description]
                    task_row = dict(zip(cols, row))

        parent_session_id = str(task_row.get("parent_session_id") or "").strip()
        if not clean_session_id and task_row:
            clean_session_id = str(task_row.get("session_id") or parent_session_id or "").strip()
        is_primary_session_task = bool(clean_session_id and not shared_session and not parent_session_id)

        task_ids: set[str] = set()
        if clean_task_id:
            task_ids.add(clean_task_id)
        if clean_session_id and not shared_session:
            task_ids.update(
                await self._fetch_text_column(
                    "SELECT id FROM tasks WHERE session_id = ? OR parent_session_id = ?",
                    (clean_session_id, clean_session_id),
                )
            )
        session_ids = {clean_session_id} if clean_session_id and not shared_session else set()
        full_run_ids: set[str] = set()
        work_item_ids: set[str] = set()
        role_runtime_session_ids: set[str] = set()
        runtime_session_ids: set[str] = set()

        if is_primary_session_task:
            full_run_ids.update(
                await self._fetch_text_column(
                    "SELECT run_id FROM delegation_runs WHERE session_id = ?",
                    (clean_session_id,),
                )
            )

        if task_ids:
            work_item_ids.update(
                await self._fetch_text_column_where_in(
                    "work_item_runtime_links",
                    "work_item_id",
                    "runtime_task_id",
                    task_ids,
                )
            )

        metadata_paths = (
            "$.task_id",
            "$.runtime_task_id",
            "$.execution_task_id",
            "$.origin_task_id",
            "$.worker_task_id",
        )
        if task_ids:
            for path in metadata_paths:
                for chunk in self._chunked_ids(task_ids):
                    placeholders = ", ".join("?" for _ in chunk)
                    work_item_ids.update(
                        await self._fetch_text_column(
                            f"""
                            SELECT work_item_id FROM delegation_work_items
                            WHERE json_valid(metadata)
                              AND json_extract(metadata, ?) IN ({placeholders})
                            """,
                            tuple([path, *chunk]),
                        )
                    )

        if session_ids:
            for chunk in self._chunked_ids(session_ids):
                placeholders = ", ".join("?" for _ in chunk)
                work_item_ids.update(
                    await self._fetch_text_column(
                        f"""
                        SELECT work_item_id FROM delegation_work_items
                        WHERE json_valid(metadata)
                          AND (
                            json_extract(metadata, '$.session_id') IN ({placeholders})
                            OR json_extract(metadata, '$.parent_session_id') IN ({placeholders})
                            OR json_extract(metadata, '$.session_scope_id') IN ({placeholders})
                          )
                        """,
                        tuple([*chunk, *chunk, *chunk]),
                    )
                )

        if full_run_ids:
            work_item_ids.update(
                await self._fetch_text_column_where_in(
                    "delegation_work_items",
                    "work_item_id",
                    "run_id",
                    full_run_ids,
                )
            )

        if work_item_ids:
            role_runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "delegation_work_items",
                    "role_runtime_session_id",
                    "work_item_id",
                    work_item_ids,
                    extra_where="COALESCE(role_runtime_session_id, '') != ''",
                )
            )
            role_runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "delegation_work_items",
                    "claimed_by_role_runtime_session_id",
                    "work_item_id",
                    work_item_ids,
                    extra_where="COALESCE(claimed_by_role_runtime_session_id, '') != ''",
                )
            )

        if full_run_ids:
            role_runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "role_runtime_sessions",
                    "role_session_id",
                    "run_id",
                    full_run_ids,
                )
            )
            role_runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "delegation_role_sessions",
                    "role_session_id",
                    "run_id",
                    full_run_ids,
                )
            )
            role_runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "seat_states",
                    "role_runtime_session_id",
                    "run_id",
                    full_run_ids,
                    extra_where="COALESCE(role_runtime_session_id, '') != ''",
                )
            )
            role_runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "seat_states",
                    "member_session_id",
                    "run_id",
                    full_run_ids,
                    extra_where="COALESCE(member_session_id, '') != ''",
                )
            )

        if task_ids:
            runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "runtime_sessions",
                    "runtime_session_id",
                    "task_id",
                    task_ids,
                )
            )
        if session_ids:
            runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "runtime_sessions",
                    "runtime_session_id",
                    "session_id",
                    session_ids,
                )
            )
        if full_run_ids:
            runtime_session_ids.update(role_runtime_session_ids)
            # Some company runtime events are scoped directly to the run id
            # rather than a role/runtime session id.
            runtime_session_ids.update(full_run_ids)
        runtime_lookup_ids = task_ids | session_ids | full_run_ids | work_item_ids
        if full_run_ids:
            runtime_lookup_ids.update(role_runtime_session_ids)
        runtime_session_ids.update(
            await self._fetch_text_column_where_text_contains(
                "runtime_sessions",
                "runtime_session_id",
                "runtime_session_id",
                runtime_lookup_ids,
            )
        )
        runtime_session_ids.update(
            await self._fetch_text_column_where_text_contains(
                "runtime_sessions",
                "runtime_session_id",
                "metadata",
                runtime_lookup_ids,
            )
        )

        task_tables = (
            "runtime_transcript_entries",
            "runtime_tool_calls",
            "runtime_tool_results",
            "runtime_subagent_runs",
            "runtime_worktree_sessions",
            "runtime_compaction_boundaries",
        )
        for table in task_tables:
            await self._delete_where_in(table, "task_id", task_ids)
            if session_ids and table in {
                "runtime_transcript_entries",
                "runtime_tool_calls",
                "runtime_tool_results",
            }:
                await self._delete_where_in(table, "session_id", session_ids)

        if runtime_session_ids:
            for table in (
                "runtime_events",
                "runtime_transcript_entries",
                "runtime_tool_calls",
                "runtime_tool_results",
                "runtime_permission_grants",
                "runtime_subagent_runs",
                "runtime_worktree_sessions",
                "runtime_compaction_boundaries",
                "runtime_sessions",
            ):
                await self._delete_where_in(table, "runtime_session_id", runtime_session_ids)

        await self._delete_where_in("execution_checkpoints", "task_id", task_ids)
        await self._delete_where_in("execution_checkpoints", "session_id", session_ids)
        await self._delete_by_json_path_or_text_ids(
            "execution_checkpoints",
            "payload",
            ("$.task_id", "$.session_id", "$.run_id", "$.work_item_id"),
            task_ids | session_ids | full_run_ids | work_item_ids,
        )
        await self._delete_where_in("external_sessions", "task_id", task_ids)
        await self._delete_where_in("external_sessions", "opc_session_id", session_ids)
        await self._delete_where_in("external_sessions", "session_id", session_ids)
        await self._delete_where_text_contains(
            "external_sessions",
            "metadata",
            task_ids | session_ids | full_run_ids | work_item_ids | runtime_session_ids,
        )
        if await self._table_exists("external_sessions_v2"):
            await self._delete_where_in("external_sessions_v2", "task_id", task_ids)
            await self._delete_where_in("external_sessions_v2", "opc_session_id", session_ids)
            await self._delete_where_in("external_sessions_v2", "session_id", session_ids)
            await self._delete_where_text_contains(
                "external_sessions_v2",
                "metadata",
                task_ids | session_ids | full_run_ids | work_item_ids | runtime_session_ids,
            )

        if clean_task_id:
            await self._db.execute(
                """
                DELETE FROM events
                WHERE json_valid(payload)
                  AND (
                    json_extract(payload, '$.task_id') = ?
                    OR json_extract(payload, '$.runtime_task_id') = ?
                    OR json_extract(payload, '$.execution_task_id') = ?
                    OR json_extract(payload, '$.escalation_id') LIKE ?
                  )
                """,
                (clean_task_id, clean_task_id, clean_task_id, f"esc_{clean_task_id}_%"),
            )
        for path in (
            "$.task_id",
            "$.runtime_task_id",
            "$.execution_task_id",
            "$.origin_task_id",
            "$.worker_task_id",
        ):
            await self._delete_events_by_payload_ids(path, task_ids)
        for path in ("$.session_id", "$.parent_session_id", "$.opc_session_id"):
            await self._delete_events_by_payload_ids(path, session_ids)
        await self._delete_events_by_payload_ids("$.run_id", full_run_ids)
        await self._delete_events_by_payload_ids("$.work_item_id", work_item_ids)
        await self._delete_events_by_payload_ids("$.runtime_session_id", runtime_session_ids)
        role_event_session_ids = set(runtime_session_ids)
        if full_run_ids:
            role_event_session_ids.update(role_runtime_session_ids)
        await self._delete_events_by_payload_ids("$.role_runtime_session_id", role_event_session_ids)
        await self._delete_events_by_payload_ids("$.member_session_id", role_event_session_ids)
        event_scope_ids = task_ids | session_ids | full_run_ids | work_item_ids | runtime_session_ids
        if full_run_ids:
            event_scope_ids.update(role_runtime_session_ids)
        await self._delete_where_text_contains(
            "events",
            "payload",
            event_scope_ids,
        )

        if full_run_ids:
            await self._delete_where_in("work_item_runtime_links", "work_item_id", work_item_ids)
            await self._delete_where_in("delegation_events", "run_id", full_run_ids)
            await self._delete_where_in("delegation_work_items", "run_id", full_run_ids)
            await self._delete_where_in("delegation_cells", "run_id", full_run_ids)
            await self._delete_where_in("delegation_role_sessions", "run_id", full_run_ids)
            await self._delete_where_in("role_runtime_sessions", "run_id", full_run_ids)
            await self._delete_where_in("seat_states", "run_id", full_run_ids)
            await self._delete_where_in("team_instances", "run_id", full_run_ids)
            await self._delete_where_in("delegation_runs", "run_id", full_run_ids)
        elif work_item_ids:
            await self._delete_where_in("work_item_runtime_links", "work_item_id", work_item_ids)
            await self._delete_where_in("delegation_events", "work_item_id", work_item_ids)
            await self._delete_where_in("delegation_work_items", "work_item_id", work_item_ids)
            if await self._table_exists("external_sessions_v2"):
                await self._delete_where_text_contains(
                    "external_sessions_v2",
                    "metadata",
                    task_ids | session_ids | full_run_ids | work_item_ids | runtime_session_ids,
                )
            await self._delete_where_in("work_item_runtime_links", "runtime_task_id", task_ids)
            for chunk in self._chunked_ids(work_item_ids):
                placeholders = ", ".join("?" for _ in chunk)
                await self._db.execute(
                    f"""
                    UPDATE seat_states
                    SET current_work_item_id = '',
                        current_task_id = ''
                    WHERE current_work_item_id IN ({placeholders})
                    """,
                    tuple(chunk),
                )
                await self._db.execute(
                    f"""
                    UPDATE role_runtime_sessions
                    SET focused_work_item_id = ''
                    WHERE focused_work_item_id IN ({placeholders})
                    """,
                    tuple(chunk),
                )

    async def delete_company_runtime_artifacts_for_session(
        self,
        session_id: str,
    ) -> None:
        """Delete company runtime rows tied to a chat session id.

        This is intentionally session-driven rather than task-driven so a hard
        chat delete can clean orphan company runtime rows even if the task row
        was already removed by an earlier or partial delete path.
        """
        if not self._db:
            return
        clean_session_id = str(session_id or "").strip()
        if not clean_session_id:
            return
        await self._delete_company_runtime_artifacts_for_task(
            "",
            clean_session_id,
            shared_session=False,
        )
        await self._db.commit()

    async def delete_session_data(self, task_id: str, session_id: str | None = None) -> None:
        """Delete all data associated with a session/task.

        Cleans: agent_messages, session_messages, session_parts,
        session_compactions, execution_checkpoints, external_sessions.
        The task row itself is NOT deleted (caller marks it CANCELLED).
        """
        if not self._db:
            return
        await self._db.execute("DELETE FROM agent_messages WHERE task_id = ?", (task_id,))
        shared_session = False
        if session_id:
            async with self._db.execute(
                "SELECT 1 FROM tasks WHERE session_id = ? AND id != ? LIMIT 1",
                (session_id, task_id),
            ) as cursor:
                shared_session = await cursor.fetchone() is not None
        if session_id:
            if not shared_session:
                await self._db.execute("DELETE FROM session_parts WHERE session_id = ?", (session_id,))
                await self._db.execute("DELETE FROM session_compactions WHERE session_id = ?", (session_id,))
                await self._db.execute("DELETE FROM session_memory_snapshots WHERE session_id = ?", (session_id,))
                await self._db.execute("DELETE FROM agent_compactions WHERE session_id = ?", (session_id,))
                await self._db.execute("DELETE FROM agent_memory_snapshots WHERE session_id = ?", (session_id,))
                await self._db.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
        else:
            await self._db.execute("DELETE FROM session_messages WHERE task_id = ?", (task_id,))
        if session_id and not shared_session:
            await self._db.execute(
                "DELETE FROM execution_checkpoints WHERE task_id = ? OR session_id = ?",
                (task_id, session_id),
            )
            await self._db.execute(
                "DELETE FROM external_sessions WHERE task_id = ? OR opc_session_id = ?",
                (task_id, session_id),
            )
            if await self._table_exists("external_sessions_v2"):
                await self._db.execute(
                    "DELETE FROM external_sessions_v2 WHERE task_id = ? OR opc_session_id = ?",
                    (task_id, session_id),
                )
        else:
            await self._db.execute("DELETE FROM execution_checkpoints WHERE task_id = ?", (task_id,))
            await self._db.execute("DELETE FROM external_sessions WHERE task_id = ?", (task_id,))
            if await self._table_exists("external_sessions_v2"):
                await self._db.execute("DELETE FROM external_sessions_v2 WHERE task_id = ?", (task_id,))
        await self._db.commit()

    async def hard_delete_task(self, task_id: str, session_id: str | None = None) -> None:
        """Permanently delete a task row and all persisted lifecycle data."""
        if not self._db:
            return
        shared_session = False
        if session_id:
            async with self._db.execute(
                "SELECT 1 FROM tasks WHERE session_id = ? AND id != ? LIMIT 1",
                (session_id, task_id),
            ) as cursor:
                shared_session = await cursor.fetchone() is not None
        await self._delete_company_runtime_artifacts_for_task(
            task_id,
            session_id,
            shared_session=shared_session,
        )
        await self.delete_session_data(task_id, session_id)
        await self._db.execute("DELETE FROM meetings WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM work_item_decisions WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM artifact_records WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM cost_records WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM cost_events WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM approval_records WHERE task_id = ?", (task_id,))
        if session_id and not shared_session:
            await self._db.execute(
                "DELETE FROM handoff_records WHERE task_id = ? OR session_id = ?",
                (task_id, session_id),
            )
            await self._db.execute(
                "DELETE FROM reorg_proposals WHERE task_id = ? OR session_id = ?",
                (task_id, session_id),
            )
            await self._db.execute(
                "DELETE FROM session_links WHERE task_id = ? OR session_id = ? OR linked_session_id = ?",
                (task_id, session_id, session_id),
            )
            await self._db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        else:
            await self._db.execute("DELETE FROM handoff_records WHERE task_id = ?", (task_id,))
            await self._db.execute("DELETE FROM reorg_proposals WHERE task_id = ?", (task_id,))
            await self._db.execute("DELETE FROM session_links WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await self._db.commit()

    # --- Tasks ---

    @staticmethod
    def _metadata_has_work_item_projection_identity(metadata: dict[str, Any]) -> bool:
        return any(
            str(metadata.get(key, "") or "").strip()
            for key in (
                WORK_ITEM_PROJECTION_ID_KEY,
                WORK_ITEM_TURN_TYPE_KEY,
            )
        )

    async def _save_task_row(self, task: Task, *, commit: bool = True) -> None:
        assert self._db
        self._assert_project_write_scope(
            getattr(task, "project_id", None),
            operation="save_task",
            entity=f"task {getattr(task, 'id', '')!r}",
        )
        task.metadata = dict(task.metadata or {})
        if self._metadata_has_work_item_projection_identity(task.metadata):
            task.metadata, _ = migrate_work_item_projection_metadata(
                task.metadata,
                turn_type_fallback="",
            )
        await self._db.execute(
            """INSERT INTO tasks
            (id, session_id, parent_session_id, title, description, assigned_to, status, priority, dependencies,
             execution_lock, context_snapshot, assigned_external_agent, created_at,
             deadline, result, parent_id, project_id, tags, comments,
             retry_count, max_retries, metadata,
             org_id, goal_id, checkout_run_id, execution_locked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                session_id=excluded.session_id,
                parent_session_id=excluded.parent_session_id,
                title=excluded.title,
                description=excluded.description,
                assigned_to=excluded.assigned_to,
                status=excluded.status,
                priority=excluded.priority,
                dependencies=excluded.dependencies,
                execution_lock=excluded.execution_lock,
                context_snapshot=excluded.context_snapshot,
                assigned_external_agent=excluded.assigned_external_agent,
                created_at=excluded.created_at,
                deadline=excluded.deadline,
                result=excluded.result,
                parent_id=excluded.parent_id,
                project_id=excluded.project_id,
                tags=excluded.tags,
                comments=excluded.comments,
                retry_count=excluded.retry_count,
                max_retries=excluded.max_retries,
                metadata=excluded.metadata,
                org_id=excluded.org_id,
                goal_id=excluded.goal_id,
                checkout_run_id=excluded.checkout_run_id,
                execution_locked_at=excluded.execution_locked_at""",
            (
                task.id,
                task.session_id,
                task.parent_session_id,
                task.title,
                task.description,
                task.assigned_to,
                task.status.value,
                task.priority,
                _json_dumps(task.dependencies),
                int(task.execution_lock),
                _json_dumps(task.context_snapshot),
                task.assigned_external_agent,
                task.created_at.isoformat(),
                task.deadline.isoformat() if task.deadline else None,
                _json_dumps(task.result) if task.result else None,
                task.parent_id,
                task.project_id,
                _json_dumps(task.tags),
                _json_dumps(task.comments),
                task.retry_count,
                task.max_retries,
                _json_dumps(task.metadata),
                task.org_id,
                task.goal_id,
                task.checkout_run_id,
                task.execution_locked_at.isoformat() if task.execution_locked_at else None,
            ),
        )
        if commit:
            await self._db.commit()

    async def save_task(self, task: Task) -> None:
        await self._save_task_row(task, commit=True)

    async def _find_existing_runtime_task_for_work_item(
        self,
        work_item: DelegationWorkItem,
        candidate_task: Task,
    ) -> Task | None:
        assert self._db is not None
        session_id = str(getattr(candidate_task, "session_id", "") or "").strip()
        project_id = str(getattr(candidate_task, "project_id", "") or "").strip()
        projection_id = projection_id_for_work_item(work_item)
        run_id = str(getattr(work_item, "run_id", "") or "").strip()
        if not session_id or not project_id or not projection_id or not run_id:
            return None
        async with self._db.execute(
            """SELECT * FROM tasks
               WHERE session_id=?
                 AND project_id=?
                 AND metadata LIKE ?
                 AND metadata LIKE ?
               ORDER BY created_at ASC, id ASC""",
            (
                session_id,
                project_id,
                "%work_item_runtime%",
                f"%{projection_id}%",
            ),
        ) as cursor:
            rows = await cursor.fetchall()
            description = cursor.description

        candidates: list[Task] = []
        for row in rows:
            task = self._row_to_task(row, description)
            metadata = dict(task.metadata or {})
            if str(metadata.get("delegation_run_id", "") or "").strip() != run_id:
                continue
            if str(metadata.get(WORK_ITEM_PROJECTION_ID_KEY, "") or "").strip() != projection_id:
                continue
            if not self._runtime_task_matches_work_item(task, work_item):
                continue
            candidates.append(task)
        if not candidates:
            return None

        def _candidate_sort_key(task: Task) -> tuple[int, int, datetime, str]:
            metadata = dict(task.metadata or {})
            status = str(task.status.value if hasattr(task.status, "value") else task.status or "").strip().lower()
            terminal = 1 if status in self._terminal_task_statuses() else 0
            duplicate = 1 if str(metadata.get("duplicate_runtime_task_of", "") or "").strip() else 0
            return (duplicate, terminal, task.created_at, str(task.id or ""))

        candidates.sort(key=_candidate_sort_key)
        if len(candidates) > 1:
            logger.warning(
                "ensure_runtime_task_for_work_item: multiple exact runtime Tasks "
                f"for work_item={work_item.work_item_id}; using task={candidates[0].id} "
                f"candidates={[task.id for task in candidates[:5]]}"
            )
        return candidates[0]

    async def ensure_runtime_task_for_work_item(
        self,
        work_item: DelegationWorkItem,
        task_factory: Any,
        *,
        replace_policy: str = "never_active",
    ) -> Task:
        """Return the authoritative runtime Task for a WorkItem.

        This is the only hot-path creation/reuse entry point for company-mode
        runtime Tasks. It writes the structured link in the same transaction as
        task creation, and ordinary read paths do not repair or rescore links.
        """
        if self._db is None:
            raise RuntimeError("store is not initialized")
        wid = str(getattr(work_item, "work_item_id", "") or "").strip()
        if not wid:
            raise ValueError("work_item_id is required")
        policy = str(replace_policy or "never_active").strip() or "never_active"
        if policy != "never_active":
            raise ValueError(f"unsupported runtime link replace_policy: {replace_policy}")

        await self._db.execute("BEGIN IMMEDIATE")
        try:
            if not await self._work_item_exists(wid):
                raise ValueError(f"WorkItem does not exist: {wid}")

            linked_task_id = await self._runtime_link_task_id_for_work_item(wid)
            if linked_task_id:
                linked_task = await self._get_task_unhydrated(linked_task_id)
                if linked_task is not None:
                    set_linked_work_item_id(linked_task, wid)
                    issues = [
                        issue for issue in validate_work_item_runtime_projection(linked_task, work_item)
                        if issue.severity == "error"
                    ]
                    if issues:
                        raise RuntimeError(
                            "work-item runtime invariant failed for linked Task "
                            f"{linked_task.id}: "
                            + "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
                        )
                    await self._db.commit()
                    return linked_task
                await self._db.execute(
                    "DELETE FROM work_item_runtime_links WHERE work_item_id=?",
                    (wid,),
                )

            candidate = task_factory() if callable(task_factory) else task_factory
            if inspect.isawaitable(candidate):
                candidate = await candidate
            if not isinstance(candidate, Task):
                raise TypeError("task_factory must return a Task")
            candidate.metadata = dict(candidate.metadata or {})

            task = await self._find_existing_runtime_task_for_work_item(work_item, candidate)
            if task is None:
                task = candidate
                await self._save_task_row(task, commit=False)

            existing_owner_id = await self._runtime_link_work_item_id_for_task(task.id)
            if existing_owner_id and existing_owner_id != wid:
                raise RuntimeError(
                    "runtime Task is already linked to another WorkItem: "
                    f"task={task.id} owner={existing_owner_id} requested={wid}"
                )

            linked = await self._write_work_item_runtime_link(
                wid,
                task.id,
                link_kind="primary",
                commit=False,
            )
            if not linked:
                raise RuntimeError(
                    f"failed to link runtime Task {task.id} for WorkItem {wid}"
                )
            set_linked_work_item_id(task, wid)
            issues = [
                issue for issue in validate_work_item_runtime_projection(task, work_item)
                if issue.severity == "error"
            ]
            if issues:
                raise RuntimeError(
                    "work-item runtime invariant failed for Task "
                    f"{task.id}: "
                    + "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
                )
            await self._db.commit()
            return task
        except Exception:
            await self._db.rollback()
            raise

    async def link_work_item_runtime_task(
        self,
        work_item_id: str,
        runtime_task_id: str,
        *,
        link_kind: str = "primary",
        allow_replace: bool = False,
    ) -> bool:
        """Persist the authoritative WorkItem -> runtime Task link."""
        if self._db is None:
            return False
        wid = str(work_item_id or "").strip()
        tid = str(runtime_task_id or "").strip()
        if not wid or not tid:
            return False
        if not await self._work_item_exists(wid) or not await self._task_exists(tid):
            return False
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            existing_task_id = await self._runtime_link_task_id_for_work_item(wid)
            existing_owner_id = await self._runtime_link_work_item_id_for_task(tid)
            if existing_task_id and existing_task_id != tid and not allow_replace:
                if not await self._runtime_task_link_is_replaceable(existing_task_id):
                    logger.warning(
                        "work-item runtime link refused to overwrite active link: "
                        f"work_item={wid} existing_task={existing_task_id} requested_task={tid}"
                    )
                    await self._db.rollback()
                    return False
            if existing_owner_id and existing_owner_id != wid and not allow_replace:
                logger.warning(
                    "work-item runtime link refused to steal task from another work item: "
                    f"task={tid} existing_work_item={existing_owner_id} requested_work_item={wid}"
                )
                await self._db.rollback()
                return False
            wrote = await self._write_work_item_runtime_link(
                wid,
                tid,
                link_kind=link_kind,
                commit=False,
            )
            await self._db.commit()
            return wrote
        except Exception:
            await self._db.rollback()
            raise

    async def hydrate_task_work_item_links(self, tasks: list[Task]) -> list[Task]:
        """Attach non-persisted WorkItem link ids from the link table only."""
        if self._db is None or not tasks:
            return tasks
        task_by_id = {
            str(task.id or "").strip(): task
            for task in tasks
            if str(getattr(task, "id", "") or "").strip()
        }
        if not task_by_id:
            return tasks
        placeholders = ", ".join("?" for _ in task_by_id)
        async with self._db.execute(
            f"""SELECT work_item_id, runtime_task_id
                FROM work_item_runtime_links
                WHERE runtime_task_id IN ({placeholders})""",
            list(task_by_id),
        ) as cursor:
            rows = await cursor.fetchall()
        linked_task_ids: set[str] = set()
        for work_item_id, task_id in rows:
            tid = str(task_id or "").strip()
            wid = str(work_item_id or "").strip()
            task = task_by_id.get(tid)
            if task is None or not wid:
                continue
            set_linked_work_item_id(task, wid)
            linked_task_ids.add(tid)

        for tid, task in task_by_id.items():
            if tid not in linked_task_ids:
                set_linked_work_item_id(task, "")
        return tasks

    async def get_runtime_links_for_work_items(self, work_item_ids: list[str]) -> dict[str, str]:
        if self._db is None:
            return {}
        ids = [
            str(work_item_id or "").strip()
            for work_item_id in work_item_ids
            if str(work_item_id or "").strip()
        ]
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        async with self._db.execute(
            f"""SELECT work_item_id, runtime_task_id
                FROM work_item_runtime_links
                WHERE work_item_id IN ({placeholders})""",
            ids,
        ) as cursor:
            rows = await cursor.fetchall()
        return {
            str(work_item_id or "").strip(): str(task_id or "").strip()
            for work_item_id, task_id in rows
            if str(work_item_id or "").strip() and str(task_id or "").strip()
        }

    async def get_runtime_task_for_work_item(self, work_item_id: str) -> Task | None:
        if self._db is None:
            return None
        wid = str(work_item_id or "").strip()
        if not wid:
            return None
        task_id = await self._runtime_link_task_id_for_work_item(wid)
        if not task_id:
            return None
        task = await self._get_task_unhydrated(task_id)
        if task is None:
            return None
        set_linked_work_item_id(task, wid)
        return task

    async def get_work_item_for_runtime_task(self, task_id: str) -> DelegationWorkItem | None:
        if self._db is None:
            return None
        tid = str(task_id or "").strip()
        if not tid:
            return None
        work_item_id = await self._runtime_link_work_item_id_for_task(tid)
        if not work_item_id:
            return None
        return await self.get_delegation_work_item(work_item_id)

    async def get_task(self, task_id: str) -> Task | None:
        assert self._db
        task = await self._get_task_unhydrated(task_id)
        if task is None:
            return None
        await self.hydrate_task_work_item_links([task])
        return task

    async def get_tasks(
        self,
        project_id: str | None = None,
        status: TaskStatus | None = None,
        parent_id: str | None = None,
    ) -> list[Task]:
        assert self._db
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if status:
            query += " AND status = ?"
            params.append(status.value)
        if parent_id is not None:
            query += " AND parent_id = ?"
            params.append(parent_id)
        query += " ORDER BY priority ASC, created_at ASC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            tasks = [self._row_to_task(row, cursor.description) for row in rows]
        await self.hydrate_task_work_item_links(tasks)
        return tasks

    async def get_tasks_by_session_id(
        self,
        session_id: str,
        project_id: str | None = None,
    ) -> list[Task]:
        assert self._db
        query = "SELECT * FROM tasks WHERE session_id = ?"
        params: list[Any] = [session_id]
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        query += " ORDER BY priority ASC, created_at ASC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            tasks = [self._row_to_task(row, cursor.description) for row in rows]
        await self.hydrate_task_work_item_links(tasks)
        return tasks

    async def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        assert self._db
        await self._db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status.value, task_id))
        await self._db.commit()

    async def append_task_comment(self, task_id: str, comment: dict[str, Any]) -> None:
        task = await self.get_task(task_id)
        if not task:
            return
        task.comments = list(task.comments)
        task.comments.append(comment)
        await self.save_task(task)

    async def acquire_task_lock(self, task_id: str, *, lease_seconds: int | None = None) -> bool:
        """Atomically acquire the execution lock on a task.

        When ``lease_seconds`` is provided, a stale lock (one whose
        ``execution_locked_at`` timestamp is older than ``now - lease_seconds``)
        can be stolen. This lets a new process claim an execution slot whose
        original holder died without releasing — the common crash-recovery case.

        Always refreshes ``execution_locked_at`` to ``now()`` on successful
        acquire, so the returned lease starts fresh.
        """
        assert self._db
        now_iso = datetime.now().isoformat()
        if lease_seconds is not None and lease_seconds > 0:
            cutoff_iso = (datetime.now() - timedelta(seconds=int(lease_seconds))).isoformat()
            query = (
                "UPDATE tasks SET execution_lock = 1, execution_locked_at = ? "
                "WHERE id = ? AND ("
                "execution_lock = 0 "
                "OR execution_locked_at IS NULL "
                "OR execution_locked_at < ?"
                ")"
            )
            params: tuple[Any, ...] = (now_iso, task_id, cutoff_iso)
        else:
            query = (
                "UPDATE tasks SET execution_lock = 1, execution_locked_at = ? "
                "WHERE id = ? AND execution_lock = 0"
            )
            params = (now_iso, task_id)
        async with self._db.execute(query, params) as cursor:
            await self._db.commit()
            return cursor.rowcount > 0

    async def release_task_lock(self, task_id: str) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE tasks SET execution_lock = 0, execution_locked_at = NULL WHERE id = ?",
            (task_id,),
        )
        await self._db.commit()

    def _row_to_task(self, row: Any, description: Any) -> Task:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return Task(
            id=data["id"],
            session_id=data.get("session_id"),
            parent_session_id=data.get("parent_session_id"),
            title=data["title"],
            description=data["description"],
            assigned_to=data["assigned_to"],
            status=TaskStatus(data["status"]),
            priority=data["priority"],
            dependencies=_json_loads(data["dependencies"], []),
            execution_lock=bool(data["execution_lock"]),
            context_snapshot=_json_loads(data["context_snapshot"], {}),
            assigned_external_agent=data["assigned_external_agent"],
            created_at=datetime.fromisoformat(data["created_at"]),
            deadline=datetime.fromisoformat(data["deadline"]) if data["deadline"] else None,
            result=_json_loads(data["result"], None),
            parent_id=data["parent_id"],
            project_id=data["project_id"],
            tags=_json_loads(data["tags"], []),
            comments=_json_loads(data["comments"], []),
            retry_count=data["retry_count"],
            max_retries=data["max_retries"],
            metadata=_json_loads(data["metadata"], {}),
            org_id=data.get("org_id"),
            goal_id=data.get("goal_id"),
            checkout_run_id=data.get("checkout_run_id"),
            execution_locked_at=(
                datetime.fromisoformat(data["execution_locked_at"])
                if data.get("execution_locked_at")
                else None
            ),
        )

    # --- Agent Messages ---

    async def save_message(self, msg: AgentMessage) -> None:
        assert self._db
        metadata = {
            **dict(msg.metadata or {}),
            "transport_kind": getattr(getattr(msg, "transport_kind", None), "value", getattr(msg, "transport_kind", "")) or "",
            "semantic_type": getattr(getattr(msg, "semantic_type", None), "value", getattr(msg, "semantic_type", "")) or "",
            "comms_state": getattr(getattr(msg, "comms_state", None), "value", getattr(msg, "comms_state", "")) or "",
            "correlation_id": str(getattr(msg, "correlation_id", "") or "").strip(),
            "refs": dict(getattr(msg, "refs", {}) or {}),
        }
        await self._db.execute(
            """INSERT OR REPLACE INTO agent_messages
            (msg_id, msg_type, from_agent, to_agents, subject, body, context_ref, urgency,
             reply_needed, requires_ack, timeout_action, reply_to_msg_id, task_id, status,
             timestamp, processed_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg.msg_id,
                msg.msg_type,
                msg.from_agent,
                _json_dumps(msg.to_agents),
                msg.subject,
                msg.body,
                msg.context_ref,
                msg.urgency.value,
                int(msg.reply_needed),
                int(msg.requires_ack),
                msg.timeout_action,
                msg.reply_to_msg_id,
                msg.task_id,
                msg.status.value,
                msg.timestamp.isoformat(),
                msg.processed_at.isoformat() if msg.processed_at else None,
                _json_dumps(metadata),
            ),
        )
        await self._db.commit()

    def _row_to_message(self, row: Any, description: Any) -> AgentMessage:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        metadata = _json_loads(data.get("metadata"), {})
        return AgentMessage(
            msg_id=data["msg_id"],
            msg_type=data["msg_type"],
            from_agent=data["from_agent"],
            to_agents=_json_loads(data["to_agents"], []),
            subject=data["subject"],
            body=data["body"],
            context_ref=data["context_ref"],
            urgency=MessageUrgency(data["urgency"]),
            reply_needed=bool(data["reply_needed"]),
            requires_ack=bool(data.get("requires_ack", 0)),
            timeout_action=data["timeout_action"],
            reply_to_msg_id=data.get("reply_to_msg_id"),
            task_id=data.get("task_id"),
            status=MessageStatus(data.get("status") or MessageStatus.SENT.value),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            processed_at=datetime.fromisoformat(data["processed_at"]) if data.get("processed_at") else None,
            transport_kind=CommsTransportKind(str(metadata.get("transport_kind") or CommsTransportKind.DM.value)),
            semantic_type=CommsSemanticType(str(metadata.get("semantic_type") or CommsSemanticType.WORK_UPDATE.value)),
            comms_state=CommsState(str(metadata.get("comms_state") or CommsState.OPEN.value)),
            correlation_id=str(metadata.get("correlation_id", "") or "").strip(),
            refs=dict(metadata.get("refs", {}) or {}),
            metadata=metadata,
        )

    async def get_message(self, msg_id: str) -> AgentMessage | None:
        assert self._db
        async with self._db.execute("SELECT * FROM agent_messages WHERE msg_id = ?", (msg_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_message(row, cursor.description)

    async def update_message_status(
        self,
        msg_id: str,
        status: MessageStatus,
        processed_at: datetime | None = None,
    ) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE agent_messages SET status = ?, processed_at = ? WHERE msg_id = ?",
            (status.value, processed_at.isoformat() if processed_at else None, msg_id),
        )
        await self._db.commit()

    async def get_messages_for_agent(
        self,
        agent_id: str,
        limit: int = 20,
        unread_only: bool = False,
        task_id: str | None = None,
        task_ids: list[str] | None = None,
    ) -> list[AgentMessage]:
        assert self._db
        query = """SELECT * FROM agent_messages
        WHERE to_agents LIKE ?"""
        params: list[Any] = [f'%"{agent_id}"%']
        if unread_only:
            query += " AND status IN (?, ?)"
            params.extend([MessageStatus.SENT.value, MessageStatus.DELIVERED.value])
        scope_ids = [str(item).strip() for item in list(task_ids or []) if str(item).strip()]
        if not scope_ids and task_id:
            scope_ids = [str(task_id).strip()]
        if scope_ids:
            scope_clauses: list[str] = []
            for scope_id in scope_ids:
                scope_clauses.append("(task_id = ? OR context_ref = ?)")
                params.extend([scope_id, scope_id])
            query += " AND (" + " OR ".join(scope_clauses) + ")"
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_message(row, cursor.description) for row in rows]

    async def get_outbox_for_agent(
        self,
        agent_id: str,
        limit: int = 20,
        task_id: str | None = None,
        task_ids: list[str] | None = None,
    ) -> list[AgentMessage]:
        assert self._db
        query = "SELECT * FROM agent_messages WHERE from_agent = ?"
        params: list[Any] = [agent_id]
        scope_ids = [str(item).strip() for item in list(task_ids or []) if str(item).strip()]
        if not scope_ids and task_id:
            scope_ids = [str(task_id).strip()]
        if scope_ids:
            scope_clauses: list[str] = []
            for scope_id in scope_ids:
                scope_clauses.append("(task_id = ? OR context_ref = ?)")
                params.extend([scope_id, scope_id])
            query += " AND (" + " OR ".join(scope_clauses) + ")"
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_message(row, cursor.description) for row in rows]

    async def list_agent_messages_for_tasks(
        self,
        task_ids: list[str],
        limit: int = 50,
    ) -> list[AgentMessage]:
        assert self._db
        clean_ids = [str(item).strip() for item in list(task_ids or []) if str(item).strip()]
        if not clean_ids:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        for task_id in clean_ids:
            clauses.append("(task_id = ? OR context_ref = ?)")
            params.extend([task_id, task_id])
        query = "SELECT * FROM agent_messages WHERE " + " OR ".join(clauses)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(max(1, int(limit or 50)))
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_message(row, cursor.description) for row in rows]

    async def get_replies_for_message(self, msg_id: str) -> list[AgentMessage]:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM agent_messages WHERE reply_to_msg_id = ? ORDER BY timestamp ASC",
            (msg_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_message(row, cursor.description) for row in rows]

    async def get_latest_reply(self, msg_id: str) -> AgentMessage | None:
        replies = await self.get_replies_for_message(msg_id)
        return replies[-1] if replies else None

    async def get_unprocessed_messages(self, limit: int = 200) -> list[AgentMessage]:
        """Return messages with status SENT or DELIVERED (not yet read/replied/timed out)."""
        assert self._db
        query = """SELECT * FROM agent_messages
        WHERE status IN (?, ?)
        ORDER BY timestamp ASC LIMIT ?"""
        params = [MessageStatus.SENT.value, MessageStatus.DELIVERED.value, limit]
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_message(row, cursor.description) for row in rows]

    # --- Meetings ---

    async def save_meeting(self, meeting: MeetingRoom) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO meetings
            (room_id, task_id, topic, participants, shared_context, agenda, max_rounds,
             decision_owner, status, decision_method, current_round, pending_participants,
             consensus, outcome, transcript, metadata, created_at, updated_at,
             last_activity_at, deadline_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                meeting.room_id,
                meeting.task_id,
                meeting.topic,
                _json_dumps(meeting.participants),
                meeting.shared_context,
                _json_dumps(meeting.agenda),
                meeting.max_rounds,
                meeting.decision_owner,
                meeting.status.value,
                meeting.decision_method,
                int(meeting.current_round or 0),
                _json_dumps(meeting.pending_participants),
                _json_dumps(meeting.consensus),
                _json_dumps(meeting.outcome) if meeting.outcome is not None else None,
                _json_dumps(meeting.transcript),
                _json_dumps(meeting.metadata),
                meeting.created_at.isoformat(),
                meeting.updated_at.isoformat(),
                meeting.last_activity_at.isoformat(),
                meeting.deadline_at.isoformat() if meeting.deadline_at else None,
            ),
        )
        await self._db.commit()

    async def get_meeting(self, room_id: str) -> MeetingRoom | None:
        assert self._db
        async with self._db.execute("SELECT * FROM meetings WHERE room_id = ?", (room_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_meeting(row, cursor.description)

    async def get_meetings_for_task(self, task_id: str) -> list[MeetingRoom]:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM meetings WHERE task_id = ? ORDER BY updated_at DESC",
            (task_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_meeting(row, cursor.description) for row in rows]

    def _row_to_meeting(self, row: Any, description: Any) -> MeetingRoom:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return MeetingRoom(
            room_id=data["room_id"],
            task_id=data.get("task_id"),
            topic=data["topic"],
            participants=_json_loads(data["participants"], []),
            shared_context=data["shared_context"],
            agenda=_json_loads(data["agenda"], []),
            max_rounds=data["max_rounds"],
            decision_owner=data["decision_owner"],
            status=MeetingStatus(data.get("status") or MeetingStatus.OPEN.value),
            decision_method=str(data.get("decision_method", "") or ""),
            current_round=int(data.get("current_round") or 0),
            pending_participants=_json_loads(data.get("pending_participants"), []),
            consensus=_json_loads(data.get("consensus"), {}),
            outcome=_json_loads(data["outcome"], None),
            transcript=_json_loads(data["transcript"], []),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            last_activity_at=datetime.fromisoformat(data.get("last_activity_at") or data["updated_at"]),
            deadline_at=datetime.fromisoformat(data["deadline_at"]) if data.get("deadline_at") else None,
        )

    # --- Work-item decisions, artifacts, role memory, handoffs ---

    async def record_work_item_decision(self, record: WorkItemDecisionRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO work_item_decisions
            (decision_id, project_id, task_id, role_id, projection_id, category, summary, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.decision_id,
                record.project_id,
                record.task_id,
                record.role_id,
                record.projection_id,
                record.category,
                record.summary,
                _json_dumps(record.details),
                record.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_work_item_decisions(
        self,
        project_id: str,
        projection_id: str | None = None,
        limit: int = 20,
    ) -> list[WorkItemDecisionRecord]:
        assert self._db
        query = "SELECT * FROM work_item_decisions WHERE project_id = ?"
        params: list[Any] = [project_id]
        if projection_id:
            query += " AND projection_id = ?"
            params.append(projection_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                WorkItemDecisionRecord(
                    decision_id=data["decision_id"],
                    project_id=data["project_id"],
                    task_id=data["task_id"],
                    role_id=data["role_id"],
                    projection_id=data["projection_id"],
                    category=data["category"],
                    summary=data["summary"],
                    details=_json_loads(data["details"], {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
                for data in (dict(zip(cols, row)) for row in rows)
            ]

    async def record_artifact(self, record: ArtifactRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO artifact_records
            (artifact_id, project_id, task_id, projection_id, role_id, name, artifact_type, location, status, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.artifact_id,
                record.project_id,
                record.task_id,
                record.projection_id,
                record.role_id,
                record.name,
                record.artifact_type,
                record.location,
                record.status,
                _json_dumps(record.details),
                record.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_artifacts(
        self,
        project_id: str,
        projection_id: str | None = None,
        limit: int = 50,
    ) -> list[ArtifactRecord]:
        assert self._db
        query = "SELECT * FROM artifact_records WHERE project_id = ?"
        params: list[Any] = [project_id]
        if projection_id:
            query += " AND projection_id = ?"
            params.append(projection_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                ArtifactRecord(
                    artifact_id=data["artifact_id"],
                    project_id=data["project_id"],
                    task_id=data["task_id"],
                    projection_id=data["projection_id"],
                    role_id=data["role_id"],
                    name=data["name"],
                    artifact_type=data["artifact_type"],
                    location=data["location"],
                    status=data["status"],
                    details=_json_loads(data["details"], {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
                for data in (dict(zip(cols, row)) for row in rows)
            ]

    async def record_role_memory(self, record: RoleMemoryRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO role_memory
            (memory_id, project_id, role_id, scope, summary, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                record.memory_id,
                record.project_id,
                record.role_id,
                record.scope,
                record.summary,
                _json_dumps(record.details),
                record.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_role_memory(
        self,
        project_id: str,
        role_id: str,
        limit: int = 10,
    ) -> list[RoleMemoryRecord]:
        assert self._db
        async with self._db.execute(
            """SELECT * FROM role_memory
            WHERE project_id = ? AND role_id = ?
            ORDER BY created_at DESC LIMIT ?""",
            (project_id, role_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                RoleMemoryRecord(
                    memory_id=data["memory_id"],
                    project_id=data["project_id"],
                    role_id=data["role_id"],
                    scope=data["scope"],
                    summary=data["summary"],
                    details=_json_loads(data["details"], {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
                for data in (dict(zip(cols, row)) for row in rows)
            ]

    async def save_handoff_record(self, record: HandoffRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO handoff_records
            (handoff_id, project_id, session_id, task_id, from_role, to_role, source_projection_id, target_projection_id,
             source_work_item_id, target_work_item_id, summary, payload, requires_ack, status, received_at, acked_at, accepted_at, rejected_at,
             response_summary, ack_message_id, response_message_id, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.handoff_id,
                record.project_id,
                record.session_id,
                record.task_id,
                record.from_role,
                record.to_role,
                record.source_projection_id,
                record.target_projection_id,
                record.source_work_item_id,
                record.target_work_item_id,
                record.summary,
                _json_dumps(record.payload),
                int(record.requires_ack),
                record.status,
                record.received_at.isoformat() if record.received_at else None,
                record.acked_at.isoformat() if record.acked_at else None,
                record.accepted_at.isoformat() if record.accepted_at else None,
                record.rejected_at.isoformat() if record.rejected_at else None,
                record.response_summary,
                record.ack_message_id,
                record.response_message_id,
                _json_dumps(record.metadata),
                record.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    def _row_to_handoff_record(self, row: Any, description: Any) -> HandoffRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return HandoffRecord(
            handoff_id=data["handoff_id"],
            project_id=data["project_id"],
            session_id=data.get("session_id"),
            task_id=data["task_id"],
            from_role=data["from_role"],
            to_role=data["to_role"],
            source_projection_id=data["source_projection_id"],
            target_projection_id=data["target_projection_id"],
            source_work_item_id=data.get("source_work_item_id") or "",
            target_work_item_id=data.get("target_work_item_id") or "",
            summary=data["summary"],
            payload=_json_loads(data["payload"], {}),
            requires_ack=bool(data.get("requires_ack", 0)),
            status=data.get("status") or "sent",
            received_at=datetime.fromisoformat(data["received_at"]) if data.get("received_at") else None,
            acked_at=datetime.fromisoformat(data["acked_at"]) if data.get("acked_at") else None,
            accepted_at=datetime.fromisoformat(data["accepted_at"]) if data.get("accepted_at") else None,
            rejected_at=datetime.fromisoformat(data["rejected_at"]) if data.get("rejected_at") else None,
            response_summary=data.get("response_summary") or "",
            ack_message_id=data.get("ack_message_id"),
            response_message_id=data.get("response_message_id"),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def get_handoff_record(self, handoff_id: str) -> HandoffRecord | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM handoff_records WHERE handoff_id = ?",
            (handoff_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_handoff_record(row, cursor.description)

    async def get_handoff_records(
        self,
        project_id: str,
        target_projection_id: str | None = None,
        target_work_item_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[HandoffRecord]:
        assert self._db
        query = "SELECT * FROM handoff_records WHERE project_id = ?"
        params: list[Any] = [project_id]
        if target_projection_id:
            query += " AND target_projection_id = ?"
            params.append(target_projection_id)
        if target_work_item_id:
            query += " AND target_work_item_id = ?"
            params.append(target_work_item_id)
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_handoff_record(row, cursor.description) for row in rows]

    async def update_handoff_record(
        self,
        handoff_id: str,
        *,
        status: str | None = None,
        received_at: datetime | None = None,
        acked_at: datetime | None = None,
        accepted_at: datetime | None = None,
        rejected_at: datetime | None = None,
        response_summary: str | None = None,
        ack_message_id: str | None = None,
        response_message_id: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> HandoffRecord | None:
        record = await self.get_handoff_record(handoff_id)
        if record is None:
            return None
        if status is not None:
            record.status = str(status).strip() or record.status
        if received_at is not None:
            record.received_at = received_at
        if acked_at is not None:
            record.acked_at = acked_at
        if accepted_at is not None:
            record.accepted_at = accepted_at
        if rejected_at is not None:
            record.rejected_at = rejected_at
        if response_summary is not None:
            record.response_summary = str(response_summary)
        if ack_message_id is not None:
            record.ack_message_id = ack_message_id
        if response_message_id is not None:
            record.response_message_id = response_message_id
        if metadata_updates:
            record.metadata = {**dict(record.metadata or {}), **dict(metadata_updates)}
        await self.save_handoff_record(record)
        return record

    def _row_to_role_runtime_session(self, row: Any, description: Any) -> RoleRuntimeSession:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        focused_work_item_id = data.get("focused_work_item_id") or ""
        status = normalize_role_runtime_status(data.get("status"), focused_work_item_id)
        if status == "idle":
            focused_work_item_id = ""
        return RoleRuntimeSession(
            role_session_id=data["role_session_id"],
            run_id=data["run_id"],
            project_id=data.get("project_id") or "default",
            team_instance_id=data.get("team_instance_id") or "",
            team_id=data.get("team_id") or "",
            role_id=data.get("role_id") or "",
            seat_id=data.get("seat_id") or "",
            seat_state_id=data.get("seat_state_id") or "",
            employee_id=data.get("employee_id") or "",
            focused_work_item_id=focused_work_item_id,
            background_work_item_ids=_json_loads(data.get("background_work_item_ids"), []),
            manager_role_ids=_json_loads(data.get("manager_role_ids"), []),
            manager_seat_ids=_json_loads(data.get("manager_seat_ids"), []),
            seat_ids=_json_loads(data.get("seat_ids"), []),
            adapter_session_state=_json_loads(data.get("adapter_session_state"), {}),
            inbox_state=_json_loads(data.get("inbox_state"), {}),
            memory_slices_by_work_item=_json_loads(data.get("memory_slices_by_work_item"), {}),
            resume_state=_json_loads(data.get("resume_state"), {}),
            current_work_item=_json_loads(data.get("current_work_item"), {}),
            latest_notification=_json_loads(data.get("latest_notification"), {}),
            manager_digest=_json_loads(data.get("manager_digest"), {}),
            status=status,
            pending_work_item_ids=_json_loads(data.get("pending_work_item_ids"), []),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def _save_role_runtime_session_row(self, session: RoleRuntimeSession, *, table: str) -> None:
        db = self._require_db()
        session.focused_work_item_id = str(session.focused_work_item_id or "").strip()
        session.status = normalize_role_runtime_status(
            session.status,
            session.focused_work_item_id,
        )
        if session.status == "idle":
            session.focused_work_item_id = ""
        await db.execute(
            f"""INSERT OR REPLACE INTO {table}
            (role_session_id, run_id, project_id, team_instance_id, team_id, role_id, seat_id, seat_state_id,
             employee_id, focused_work_item_id, background_work_item_ids, manager_role_ids, manager_seat_ids,
             seat_ids, adapter_session_state, inbox_state, memory_slices_by_work_item, resume_state,
             current_work_item, latest_notification, manager_digest, status, pending_work_item_ids,
             metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.role_session_id,
                session.run_id,
                session.project_id,
                session.team_instance_id,
                session.team_id,
                session.role_id,
                session.seat_id,
                session.seat_state_id,
                session.employee_id,
                session.focused_work_item_id,
                _json_dumps(session.background_work_item_ids),
                _json_dumps(session.manager_role_ids),
                _json_dumps(session.manager_seat_ids),
                _json_dumps(session.seat_ids),
                _json_dumps(session.adapter_session_state),
                _json_dumps(session.inbox_state),
                _json_dumps(session.memory_slices_by_work_item),
                _json_dumps(session.resume_state),
                _json_dumps(session.current_work_item),
                _json_dumps(session.latest_notification),
                _json_dumps(session.manager_digest),
                session.status,
                _json_dumps(list(getattr(session, "pending_work_item_ids", []) or [])),
                _json_dumps(session.metadata),
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
            ),
        )
        await db.commit()

    async def save_role_runtime_session(self, session: RoleRuntimeSession) -> None:
        await self._save_role_runtime_session_row(session, table="role_runtime_sessions")
        await self._save_role_runtime_session_row(session, table="delegation_role_sessions")

    async def save_delegation_role_session(self, session: RoleRuntimeSession) -> None:
        await self.save_role_runtime_session(session)

    async def get_role_runtime_session(self, role_session_id: str) -> RoleRuntimeSession | None:
        db = self._require_db()
        for table in ("role_runtime_sessions", "delegation_role_sessions"):
            async with db.execute(
                f"SELECT * FROM {table} WHERE role_session_id = ?",
                (role_session_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row is not None:
                    return self._row_to_role_runtime_session(row, cursor.description)
        return None

    async def get_delegation_role_session(self, role_session_id: str) -> RoleRuntimeSession | None:
        return await self.get_role_runtime_session(role_session_id)

    async def get_delegation_role_session_for_role(
        self,
        run_id: str,
        role_id: str,
        *,
        team_id: str | None = None,
        seat_id: str | None = None,
    ) -> RoleRuntimeSession | None:
        db = self._require_db()
        query = "SELECT * FROM delegation_role_sessions WHERE run_id = ? AND role_id = ?"
        params: list[Any] = [run_id, role_id]
        if team_id:
            query += " AND team_id = ?"
            params.append(team_id)
        if seat_id:
            query += " AND seat_id = ?"
            params.append(seat_id)
        query += " ORDER BY created_at ASC LIMIT 1"
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if row is not None:
                return self._row_to_role_runtime_session(row, cursor.description)
        return None

    async def get_role_runtime_session_for_role(
        self,
        run_id: str,
        role_id: str,
        *,
        team_id: str | None = None,
        seat_id: str | None = None,
    ) -> RoleRuntimeSession | None:
        return await self.get_delegation_role_session_for_role(
            run_id,
            role_id,
            team_id=team_id,
            seat_id=seat_id,
        )

    async def list_role_runtime_sessions(
        self,
        run_id: str,
        *,
        team_id: str | None = None,
        seat_id: str | None = None,
        role_id: str | None = None,
        status: str | None = None,
    ) -> list[RoleRuntimeSession]:
        db = self._require_db()
        query = "SELECT * FROM role_runtime_sessions WHERE run_id = ?"
        params: list[Any] = [run_id]
        if team_id:
            query += " AND team_id = ?"
            params.append(team_id)
        if seat_id:
            query += " AND seat_id = ?"
            params.append(seat_id)
        if role_id:
            query += " AND role_id = ?"
            params.append(role_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_role_runtime_session(row, cursor.description) for row in rows]

    async def list_delegation_role_sessions(
        self,
        run_id: str,
        *,
        team_id: str | None = None,
        seat_id: str | None = None,
        role_id: str | None = None,
        status: str | None = None,
    ) -> list[RoleRuntimeSession]:
        db = self._require_db()
        query = "SELECT * FROM delegation_role_sessions WHERE run_id = ?"
        params: list[Any] = [run_id]
        if team_id:
            query += " AND team_id = ?"
            params.append(team_id)
        if seat_id:
            query += " AND seat_id = ?"
            params.append(seat_id)
        if role_id:
            query += " AND role_id = ?"
            params.append(role_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_role_runtime_session(row, cursor.description) for row in rows]

    async def update_delegation_role_session(
        self,
        role_session_id: str,
        *,
        team_instance_id: str | None = None,
        team_id: str | None = None,
        seat_id: str | None = None,
        seat_state_id: str | None = None,
        focused_work_item_id: str | None = None,
        background_work_item_ids: list[str] | None = None,
        manager_role_ids: list[str] | None = None,
        manager_seat_ids: list[str] | None = None,
        seat_ids: list[str] | None = None,
        adapter_session_state: dict[str, Any] | None = None,
        inbox_state: dict[str, Any] | None = None,
        memory_slices_by_work_item: dict[str, list[str]] | None = None,
        resume_state: dict[str, Any] | None = None,
        current_work_item: dict[str, Any] | None = None,
        latest_notification: dict[str, Any] | None = None,
        manager_digest: dict[str, Any] | None = None,
        status: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> RoleRuntimeSession | None:
        session = await self.get_delegation_role_session(role_session_id)
        if session is None:
            return None
        if team_instance_id is not None:
            session.team_instance_id = str(team_instance_id or "").strip()
        if team_id is not None:
            session.team_id = str(team_id or "").strip()
        if seat_id is not None:
            session.seat_id = str(seat_id or "").strip()
        if seat_state_id is not None:
            session.seat_state_id = str(seat_state_id or "").strip()
        if focused_work_item_id is not None:
            session.focused_work_item_id = str(focused_work_item_id or "").strip()
        if background_work_item_ids is not None:
            session.background_work_item_ids = [
                str(item).strip() for item in background_work_item_ids if str(item).strip()
            ]
        if manager_role_ids is not None:
            session.manager_role_ids = [str(item).strip() for item in manager_role_ids if str(item).strip()]
        if manager_seat_ids is not None:
            session.manager_seat_ids = [str(item).strip() for item in manager_seat_ids if str(item).strip()]
        if seat_ids is not None:
            session.seat_ids = [str(item).strip() for item in seat_ids if str(item).strip()]
        if adapter_session_state is not None:
            session.adapter_session_state = dict(adapter_session_state)
        if inbox_state is not None:
            session.inbox_state = dict(inbox_state)
        if memory_slices_by_work_item is not None:
            session.memory_slices_by_work_item = {
                str(key).strip(): [str(item).strip() for item in list(value or []) if str(item).strip()]
                for key, value in dict(memory_slices_by_work_item or {}).items()
                if str(key).strip()
            }
        if resume_state is not None:
            session.resume_state = dict(resume_state)
        if current_work_item is not None:
            session.current_work_item = dict(current_work_item)
        if latest_notification is not None:
            session.latest_notification = dict(latest_notification)
        if manager_digest is not None:
            session.manager_digest = dict(manager_digest)
        if status is not None:
            session.status = normalize_role_runtime_status(
                status,
                session.focused_work_item_id,
            )
        if metadata_updates:
            session.metadata = {**dict(session.metadata or {}), **dict(metadata_updates)}
        session.status = normalize_role_runtime_status(
            session.status,
            session.focused_work_item_id,
        )
        if session.status == "idle":
            session.focused_work_item_id = ""
        session.updated_at = datetime.now()
        await self.save_delegation_role_session(session)
        return session

    async def update_role_runtime_session(
        self,
        role_session_id: str,
        **kwargs: Any,
    ) -> RoleRuntimeSession | None:
        return await self.update_delegation_role_session(role_session_id, **kwargs)

    # ── Fix 5 PR3: pending queue atomic helpers ────────────────────────

    async def enqueue_pending_work_item(
        self,
        role_session_id: str,
        work_item_id: str,
    ) -> bool:
        """Append ``work_item_id`` to the session's pending queue.

        Returns ``True`` if the item was enqueued, ``False`` if the session
        was not found or the item was already present. The write uses the
        SQL row as the source of truth (read → append → write in one
        transaction) so two concurrent dispatcher ticks cannot clobber
        each other's append. Idempotent on duplicate work_item_id: the
        queue is a set-in-FIFO-order, not a bag.
        """
        wid = str(work_item_id or "").strip()
        sid = str(role_session_id or "").strip()
        if not wid or not sid:
            return False
        db = self._require_db()
        # Atomic read-modify-write inside a transaction so concurrent
        # enqueues don't race each other. SQLite's default journal_mode
        # (WAL) serializes writes; the BEGIN IMMEDIATE here forces the
        # write lock upfront to avoid a lock upgrade halfway through.
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                """SELECT pending_work_item_ids, updated_at
                   FROM role_runtime_sessions
                   WHERE role_session_id = ?""",
                (sid,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.execute("ROLLBACK")
                return False
            pending_json, _updated_at = row
            pending = _json_loads(pending_json, [])
            if not isinstance(pending, list):
                pending = []
            if wid in pending:
                await db.execute("ROLLBACK")
                return False
            pending.append(wid)
            now_iso = datetime.now().isoformat()
            for table in ("role_runtime_sessions", "delegation_role_sessions"):
                await db.execute(
                    f"""UPDATE {table}
                        SET pending_work_item_ids = ?,
                            updated_at = ?
                        WHERE role_session_id = ?""",
                    (_json_dumps(pending), now_iso, sid),
                )
            await db.commit()
            return True
        except Exception:
            await db.execute("ROLLBACK")
            raise

    async def dequeue_pending_work_item(
        self,
        role_session_id: str,
    ) -> str | None:
        """Pop the FIFO head of the session's pending queue.

        Returns the dequeued ``work_item_id`` or ``None`` if the queue is
        empty / session missing. Atomic read-modify-write so the pop is
        safe against concurrent enqueues.
        """
        sid = str(role_session_id or "").strip()
        if not sid:
            return None
        db = self._require_db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                """SELECT pending_work_item_ids
                   FROM role_runtime_sessions
                   WHERE role_session_id = ?""",
                (sid,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.execute("ROLLBACK")
                return None
            pending = _json_loads(row[0], [])
            if not isinstance(pending, list) or not pending:
                await db.execute("ROLLBACK")
                return None
            head = str(pending[0])
            remaining = pending[1:]
            now_iso = datetime.now().isoformat()
            for table in ("role_runtime_sessions", "delegation_role_sessions"):
                await db.execute(
                    f"""UPDATE {table}
                        SET pending_work_item_ids = ?,
                            updated_at = ?
                        WHERE role_session_id = ?""",
                    (_json_dumps(remaining), now_iso, sid),
                )
            await db.commit()
            return head
        except Exception:
            await db.execute("ROLLBACK")
            raise

    async def role_session_is_busy(self, role_session_id: str) -> bool:
        """Return ``True`` when the session is already focused on a work
        item (i.e. a new runnable work item should be queued, not claimed).

        A session is busy when ``focused_work_item_id`` is non-empty. The
        status column is intentionally not consulted: a session marked
        ``idle`` but still carrying a focus stamp is in the "claim
        completing" gap and should still hold new work back.
        """
        sid = str(role_session_id or "").strip()
        if not sid:
            return False
        db = self._require_db()
        async with db.execute(
            """SELECT focused_work_item_id FROM role_runtime_sessions
               WHERE role_session_id = ?""",
            (sid,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return False
        return bool(str(row[0] or "").strip())

    # ── Fix 5 PR6: role-level adapter resume tokens ────────────────────

    async def update_role_session_adapter_state(
        self,
        role_session_id: str,
        agent_type: str,
        token_record: dict[str, Any] | None,
    ) -> bool:
        """Merge a single-agent entry into ``adapter_session_state``.

        PR6: the resume token for each external agent (codex,
        claude_code, opencode) lives under ``adapter_session_state[agent_type]``
        on the ROLE session — not per-task. Consecutive tasks for the
        same role resume the same external session (same codex thread,
        same claude-code session, same opencode session). A single role
        can hold independent tokens for different adapters simultaneously
        (keyed by ``agent_type``) so switching executor types is safe.

        ``token_record`` shape:
            {
                "resume_session_id": str,
                "provider_session_id": str,
                "updated_at": iso string,
                "last_task_id": str,
                "last_project_id": str,
            }

        Passing ``token_record=None`` clears the entry for ``agent_type``
        (used when the adapter signals the session is no longer resumable).
        Returns True on success, False when the role session is missing.

        Atomic read-modify-write inside ``BEGIN IMMEDIATE`` so concurrent
        broker writes (parallel roles, or two executors per role during
        rollover) don't clobber each other.
        """
        sid = str(role_session_id or "").strip()
        agent = str(agent_type or "").strip()
        if not sid or not agent:
            return False
        db = self._require_db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                """SELECT adapter_session_state FROM role_runtime_sessions
                   WHERE role_session_id = ?""",
                (sid,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.execute("ROLLBACK")
                return False
            current = _json_loads(row[0], {})
            if not isinstance(current, dict):
                current = {}
            if token_record is None:
                current.pop(agent, None)
                legacy_agent = str(
                    current.get("external_resume_agent_type")
                    or (
                        current.get("selected_execution_agent")
                        if current.get("external_resume_session_id")
                        else ""
                    )
                    or ""
                ).strip()
                if legacy_agent == agent:
                    for key in (
                        "external_resume_session_id",
                        "external_resume_session_scope_id",
                        "external_resume_agent_type",
                        "resume_source_session",
                    ):
                        current.pop(key, None)
            else:
                current[agent] = {
                    str(k): v for k, v in dict(token_record).items()
                }
            now_iso = datetime.now().isoformat()
            serialized = _json_dumps(current)
            for table in ("role_runtime_sessions", "delegation_role_sessions"):
                await db.execute(
                    f"""UPDATE {table}
                        SET adapter_session_state = ?,
                            updated_at = ?
                        WHERE role_session_id = ?""",
                    (serialized, now_iso, sid),
                )
            await db.commit()
            return True
        except Exception:
            await db.execute("ROLLBACK")
            raise

    async def get_role_session_adapter_state(
        self,
        role_session_id: str,
        agent_type: str,
    ) -> dict[str, Any] | None:
        """Read the per-agent token entry from ``adapter_session_state``.

        Returns the stored dict (``{"resume_session_id": ..., ...}``) or
        ``None`` when the role session, the dict, or the agent's entry
        is missing. Never raises on malformed JSON — returns None.
        """
        sid = str(role_session_id or "").strip()
        agent = str(agent_type or "").strip()
        if not sid or not agent:
            return None
        db = self._require_db()
        async with db.execute(
            """SELECT adapter_session_state FROM role_runtime_sessions
               WHERE role_session_id = ?""",
            (sid,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        state = _json_loads(row[0], {})
        if not isinstance(state, dict):
            return None
        entry = state.get(agent)
        if not isinstance(entry, dict):
            return None
        return entry

    def _row_to_team_instance(self, row: Any, description: Any) -> TeamInstance:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return TeamInstance(
            team_instance_id=data["team_instance_id"],
            run_id=data["run_id"],
            project_id=data.get("project_id") or "default",
            team_id=data["team_id"],
            session_id=data.get("session_id") or "",
            status=data.get("status") or "pending",
            seat_ids=_json_loads(data.get("seat_ids"), []),
            role_ids=_json_loads(data.get("role_ids"), []),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def save_team_instance(self, team: TeamInstance) -> None:
        db = self._require_db()
        await db.execute(
            """INSERT OR REPLACE INTO team_instances
            (team_instance_id, run_id, project_id, team_id, session_id, status, seat_ids, role_ids, metadata,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                team.team_instance_id,
                team.run_id,
                team.project_id,
                team.team_id,
                team.session_id,
                team.status,
                _json_dumps(team.seat_ids),
                _json_dumps(team.role_ids),
                _json_dumps(team.metadata),
                team.created_at.isoformat(),
                team.updated_at.isoformat(),
            ),
        )
        await db.commit()

    async def get_team_instance(self, team_instance_id: str) -> TeamInstance | None:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM team_instances WHERE team_instance_id = ?",
            (team_instance_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_team_instance(row, cursor.description)

    async def list_team_instances(
        self,
        *,
        run_id: str | None = None,
        team_id: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
    ) -> list[TeamInstance]:
        db = self._require_db()
        query = "SELECT * FROM team_instances WHERE 1=1"
        params: list[Any] = []
        if run_id:
            query += " AND run_id = ?"
            params.append(run_id)
        if team_id:
            query += " AND team_id = ?"
            params.append(team_id)
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_team_instance(row, cursor.description) for row in rows]

    async def update_team_instance(
        self,
        team_instance_id: str,
        *,
        status: str | None = None,
        seat_ids: list[str] | None = None,
        role_ids: list[str] | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> TeamInstance | None:
        team = await self.get_team_instance(team_instance_id)
        if team is None:
            return None
        if status is not None:
            team.status = str(status).strip() or team.status
        if seat_ids is not None:
            team.seat_ids = [str(item).strip() for item in seat_ids if str(item).strip()]
        if role_ids is not None:
            team.role_ids = [str(item).strip() for item in role_ids if str(item).strip()]
        if metadata_updates:
            team.metadata = {**dict(team.metadata or {}), **dict(metadata_updates)}
        team.updated_at = datetime.now()
        await self.save_team_instance(team)
        return team

    def _row_to_seat_state(self, row: Any, description: Any) -> SeatState:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        current_work_item_id = data.get("current_work_item_id") or ""
        status = normalize_role_runtime_status(data.get("status"), current_work_item_id)
        if status == "idle":
            current_work_item_id = ""
        return SeatState(
            seat_state_id=data["seat_state_id"],
            team_instance_id=data["team_instance_id"],
            run_id=data["run_id"],
            project_id=data.get("project_id") or "default",
            team_id=data["team_id"],
            seat_id=data["seat_id"],
            role_id=data.get("role_id") or "",
            employee_id=data.get("employee_id") or "",
            member_session_id=data.get("member_session_id") or "",
            role_runtime_session_id=data.get("role_runtime_session_id") or "",
            status=status,
            resident_status=status,
            current_task_id=data.get("current_task_id") or "",
            current_work_item_id=current_work_item_id,
            manager_role_id=data.get("manager_role_id") or "",
            manager_seat_id=data.get("manager_seat_id") or "",
            manager_role_ids=_json_loads(data.get("manager_role_ids"), []),
            manager_seat_ids=_json_loads(data.get("manager_seat_ids"), []),
            inbox_state=_json_loads(data.get("inbox_state"), {}),
            resume_state=_json_loads(data.get("resume_state"), {}),
            current_work_item=_json_loads(data.get("current_work_item"), {}),
            latest_notification=_json_loads(data.get("latest_notification"), {}),
            manager_digest=_json_loads(data.get("manager_digest"), {}),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def save_seat_state(self, seat: SeatState) -> None:
        db = self._require_db()
        seat.current_work_item_id = str(seat.current_work_item_id or "").strip()
        seat.status = normalize_role_runtime_status(
            seat.status,
            seat.current_work_item_id,
        )
        if seat.status == "idle":
            seat.current_work_item_id = ""
        seat.resident_status = seat.status
        await db.execute(
            """INSERT OR REPLACE INTO seat_states
            (seat_state_id, team_instance_id, run_id, project_id, team_id, seat_id, role_id, employee_id,
             member_session_id, role_runtime_session_id, status, resident_status, current_task_id,
             current_work_item_id, manager_role_id, manager_seat_id, manager_role_ids, manager_seat_ids,
             inbox_state, resume_state, current_work_item, latest_notification, manager_digest, metadata,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                seat.seat_state_id,
                seat.team_instance_id,
                seat.run_id,
                seat.project_id,
                seat.team_id,
                seat.seat_id,
                seat.role_id,
                seat.employee_id,
                seat.member_session_id,
                seat.role_runtime_session_id,
                seat.status,
                seat.resident_status,
                seat.current_task_id,
                seat.current_work_item_id,
                seat.manager_role_id,
                seat.manager_seat_id,
                _json_dumps(seat.manager_role_ids),
                _json_dumps(seat.manager_seat_ids),
                _json_dumps(seat.inbox_state),
                _json_dumps(seat.resume_state),
                _json_dumps(seat.current_work_item),
                _json_dumps(seat.latest_notification),
                _json_dumps(seat.manager_digest),
                _json_dumps(seat.metadata),
                seat.created_at.isoformat(),
                seat.updated_at.isoformat(),
            ),
        )
        await db.commit()

    async def get_seat_state(self, seat_state_id: str) -> SeatState | None:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM seat_states WHERE seat_state_id = ?",
            (seat_state_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_seat_state(row, cursor.description)

    async def get_seat_state_for_seat(
        self,
        team_instance_id: str,
        seat_id: str,
    ) -> SeatState | None:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM seat_states WHERE team_instance_id = ? AND seat_id = ? ORDER BY created_at ASC LIMIT 1",
            (team_instance_id, seat_id),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_seat_state(row, cursor.description)

    async def list_seat_states(
        self,
        *,
        team_instance_id: str | None = None,
        team_id: str | None = None,
        seat_id: str | None = None,
        run_id: str | None = None,
    ) -> list[SeatState]:
        db = self._require_db()
        query = "SELECT * FROM seat_states WHERE 1=1"
        params: list[Any] = []
        if team_instance_id:
            query += " AND team_instance_id = ?"
            params.append(team_instance_id)
        if team_id:
            query += " AND team_id = ?"
            params.append(team_id)
        if seat_id:
            query += " AND seat_id = ?"
            params.append(seat_id)
        if run_id:
            query += " AND run_id = ?"
            params.append(run_id)
        query += " ORDER BY created_at ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_seat_state(row, cursor.description) for row in rows]

    async def update_seat_state(
        self,
        seat_state_id: str,
        *,
        status: str | None = None,
        resident_status: str | None = None,
        current_task_id: str | None = None,
        current_work_item_id: str | None = None,
        role_runtime_session_id: str | None = None,
        inbox_state: dict[str, Any] | None = None,
        resume_state: dict[str, Any] | None = None,
        current_work_item: dict[str, Any] | None = None,
        latest_notification: dict[str, Any] | None = None,
        manager_digest: dict[str, Any] | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> SeatState | None:
        seat = await self.get_seat_state(seat_state_id)
        if seat is None:
            return None
        if status is not None:
            seat.status = normalize_role_runtime_status(
                status,
                seat.current_work_item_id,
            )
        if resident_status is not None:
            seat.resident_status = normalize_role_runtime_status(
                resident_status,
                seat.current_work_item_id,
            )
        if current_task_id is not None:
            seat.current_task_id = str(current_task_id or "").strip()
        if current_work_item_id is not None:
            seat.current_work_item_id = str(current_work_item_id or "").strip()
        seat.status = normalize_role_runtime_status(
            seat.status,
            seat.current_work_item_id,
        )
        if seat.status == "idle":
            seat.current_work_item_id = ""
        seat.resident_status = seat.status
        if role_runtime_session_id is not None:
            seat.role_runtime_session_id = str(role_runtime_session_id or "").strip()
        if inbox_state is not None:
            seat.inbox_state = dict(inbox_state)
        if resume_state is not None:
            seat.resume_state = dict(resume_state)
        if current_work_item is not None:
            seat.current_work_item = dict(current_work_item)
        if latest_notification is not None:
            seat.latest_notification = dict(latest_notification)
        if manager_digest is not None:
            seat.manager_digest = dict(manager_digest)
        if metadata_updates:
            seat.metadata = {**dict(seat.metadata or {}), **dict(metadata_updates)}
        seat.updated_at = datetime.now()
        await self.save_seat_state(seat)
        return seat

    async def save_delegation_seat_state(self, seat: SeatState) -> None:
        await self.save_seat_state(seat)

    async def get_delegation_seat_state(self, seat_state_id: str) -> SeatState | None:
        return await self.get_seat_state(seat_state_id)

    async def list_delegation_seat_states(
        self,
        run_id: str,
        *,
        team_id: str | None = None,
        seat_id: str | None = None,
    ) -> list[SeatState]:
        return await self.list_seat_states(
            run_id=run_id,
            team_id=team_id,
            seat_id=seat_id,
        )

    async def update_delegation_seat_state(
        self,
        seat_state_id: str,
        **kwargs: Any,
    ) -> SeatState | None:
        return await self.update_seat_state(seat_state_id, **kwargs)

    def _row_to_delegation_run(self, row: Any, description: Any) -> DelegationRun:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return DelegationRun(
            run_id=data["run_id"],
            project_id=data["project_id"],
            session_id=data["session_id"],
            company_profile=data.get("company_profile") or "corporate",
            execution_model=data.get("execution_model") or "recursive_delegation",
            final_decider_role_id=data.get("final_decider_role_id") or "",
            top_level_role_ids=_json_loads(data.get("top_level_role_ids"), []),
            status=data.get("status") or "pending",
            lifecycle_status=data.get("lifecycle_status") or "active",
            current_revision=int(data.get("current_revision") or 1),
            latest_deliverable_summary=data.get("latest_deliverable_summary") or "",
            recovery_pointer=_json_loads(data.get("recovery_pointer"), {}),
            project_dossier=_json_loads(data.get("project_dossier"), {}),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def save_delegation_run(self, run: DelegationRun) -> None:
        db = self._require_db()
        await db.execute(
            """INSERT OR REPLACE INTO delegation_runs
            (run_id, project_id, session_id, company_profile, execution_model, final_decider_role_id,
             top_level_role_ids, status, lifecycle_status, current_revision, latest_deliverable_summary,
             recovery_pointer, project_dossier, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.run_id,
                run.project_id,
                run.session_id,
                run.company_profile,
                run.execution_model,
                run.final_decider_role_id,
                _json_dumps(run.top_level_role_ids),
                run.status,
                run.lifecycle_status,
                int(run.current_revision or 1),
                run.latest_deliverable_summary,
                _json_dumps(run.recovery_pointer),
                _json_dumps(run.project_dossier),
                _json_dumps(run.metadata),
                run.created_at.isoformat(),
                run.updated_at.isoformat(),
            ),
        )
        await db.commit()

    async def get_delegation_run(self, run_id: str) -> DelegationRun | None:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM delegation_runs WHERE run_id = ?",
            (run_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_delegation_run(row, cursor.description)

    async def list_delegation_runs(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        lifecycle_status: str | None = None,
        session_id: str | None = None,
    ) -> list[DelegationRun]:
        db = self._require_db()
        query = "SELECT * FROM delegation_runs WHERE 1=1"
        params: list[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        if lifecycle_status:
            query += " AND lifecycle_status = ?"
            params.append(lifecycle_status)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        query += " ORDER BY updated_at DESC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_delegation_run(row, cursor.description) for row in rows]

    async def list_open_delegation_runs(
        self,
        *,
        project_id: str | None = None,
    ) -> list[DelegationRun]:
        db = self._require_db()
        open_states = ("active", "paused", "blocked", "awaiting_owner", "deliverable")
        query = (
            "SELECT * FROM delegation_runs WHERE lifecycle_status IN (?, ?, ?, ?, ?)"
        )
        params: list[Any] = list(open_states)
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        query += " ORDER BY updated_at DESC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_delegation_run(row, cursor.description) for row in rows]

    async def get_latest_delegation_run(
        self,
        project_id: str,
        *,
        include_session_id: str | None = None,
    ) -> DelegationRun | None:
        runs = await self.list_delegation_runs(project_id=project_id)
        for run in runs:
            if include_session_id and run.session_id == include_session_id:
                continue
            return run
        return None

    def _row_to_delegation_cell(self, row: Any, description: Any) -> DelegationCell:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return DelegationCell(
            cell_id=data["cell_id"],
            run_id=data["run_id"],
            manager_role_id=data.get("manager_role_id") or "",
            member_role_ids=_json_loads(data.get("member_role_ids"), []),
            status=data.get("status") or "idle",
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def save_delegation_cell(self, cell: DelegationCell) -> None:
        db = self._require_db()
        await db.execute(
            """INSERT OR REPLACE INTO delegation_cells
            (cell_id, run_id, manager_role_id, member_role_ids, status, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cell.cell_id,
                cell.run_id,
                cell.manager_role_id,
                _json_dumps(cell.member_role_ids),
                cell.status,
                _json_dumps(cell.metadata),
                cell.created_at.isoformat(),
                cell.updated_at.isoformat(),
            ),
        )
        await db.commit()

    async def list_delegation_cells(self, run_id: str) -> list[DelegationCell]:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM delegation_cells WHERE run_id = ? ORDER BY created_at ASC",
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_delegation_cell(row, cursor.description) for row in rows]

    def _row_to_delegation_work_item(self, row: Any, description: Any) -> DelegationWorkItem:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return DelegationWorkItem(
            work_item_id=data["work_item_id"],
            run_id=data["run_id"],
            cell_id=data["cell_id"],
            team_instance_id=data.get("team_instance_id") or "",
            team_id=data.get("team_id") or "",
            role_id=data.get("role_id") or "",
            seat_id=data.get("seat_id") or "",
            seat_state_id=data.get("seat_state_id") or "",
            role_runtime_session_id=data.get("role_runtime_session_id") or "",
            parent_work_item_id=data.get("parent_work_item_id"),
            source_role_id=data.get("source_role_id"),
            source_seat_id=data.get("source_seat_id"),
            title=data.get("title") or "",
            summary=data.get("summary") or "",
            kind=data.get("kind") or "execute",
            projection_id=data.get("projection_id") or "",
            phase=coerce_phase(data.get("phase") or "ready"),
            batch_id=data.get("batch_id") or "",
            batch_index=int(data.get("batch_index") or 0),
            deliverable_summary=data.get("deliverable_summary") or "",
            blocked_reason=data.get("blocked_reason") or "",
            handoff_status=data.get("handoff_status") or "pending",
            continuation_source=data.get("continuation_source") or "",
            manager_role_id=data.get("manager_role_id") or "",
            manager_seat_id=data.get("manager_seat_id") or "",
            claimed_by_role_runtime_session_id=data.get("claimed_by_role_runtime_session_id") or "",
            claimed_by_seat_id=data.get("claimed_by_seat_id") or "",
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def _sweep_stale_claims(self) -> int:
        """Crash-recovery sweep: clear claim metadata on every in-flight
        work item at startup.

        Process restart drops every in-memory runtime session, but the
        persistent ``claimed_by_*`` fields on the work items still point
        to those dead session IDs. Without this sweep, the dispatcher
        would treat those cards as "actively claimed" forever — they
        would become permanent zombies (Bug C).

        We only touch in-flight phases (RUNNING / WAITING_FOR_* /
        PAUSED / NEEDS_ATTENTION / AWAITING_*) so we never disturb
        terminal cards. The phase itself is left alone; the sweep only
        clears the claim. Active execution can subsequently be re-picked;
        passive AWAITING_* parents remain non-dispatchable while their
        report/review chain (or human) advances them.
        """
        if self._db is None:
            return 0
        async with self._db.execute(
            """SELECT work_item_id, phase, metadata
               FROM delegation_work_items
               WHERE claimed_by_role_runtime_session_id != ''
                  OR claimed_by_seat_id != ''"""
        ) as cursor:
            rows = await cursor.fetchall()
        cleared = 0
        for work_item_id, phase_str, metadata_json in rows:
            try:
                phase = coerce_phase(phase_str)
            except (TypeError, ValueError):
                continue
            if not is_stale_claim_releasable(phase):
                continue
            metadata = _json_loads(metadata_json, {})
            metadata["claimed_by_role_session_id"] = ""
            metadata["claimed_task_id"] = ""
            metadata["claim_swept_at"] = datetime.now().isoformat()
            await self._db.execute(
                """UPDATE delegation_work_items
                   SET claimed_by_role_runtime_session_id='',
                       claimed_by_seat_id='',
                       metadata=?,
                       updated_at=?
                   WHERE work_item_id=?""",
                (_json_dumps(metadata), datetime.now().isoformat(), work_item_id),
            )
            cleared += 1
        if cleared:
            await self._db.commit()
            logger.info(
                f"stale-claim sweep: released {cleared} in-flight work item claims on startup"
            )
        return cleared

    # ── Fix 5 PR2: rich field-level merge by (run_id, role_id) ──────────

    async def _migrate_role_sessions_merge_by_role(self) -> dict[str, int]:
        """Collapse every ``(run_id, role_id)`` group into a single canonical
        row, merging state field-by-field so inbox / memory / adapter
        session tokens survive the collapse.

        Design (see Fix 5 PR2 plan):

        Target PK       ``role-runtime::{run_id}::{role_id}`` (3-segment).
        Groups          every (run_id, role_id) with ≥1 row. A size-1 group
                        whose PK already equals the canonical form is
                        skipped (noop — common case after migration).
        Merge rules     inbox_state, memory_slices_by_work_item, and the
                        list-of-ids columns (background_work_item_ids,
                        manager_role_ids, manager_seat_ids, seat_ids) are
                        UNIONED across all rows. Scalar state columns
                        (focused_work_item_id, status, resume_state,
                        current_work_item, latest_notification,
                        manager_digest, adapter_session_state, team_*,
                        seat_id, seat_state_id) are taken from the
                        "active" row — the one with a populated focus and
                        the highest status priority, tiebroken by
                        updated_at. ``adapter_session_state`` is the
                        externally-observable LLM / codex session token:
                        losing rows' states are retained under
                        ``metadata.adapter_session_state_audit`` so the
                        old codex sessions can be recovered for debugging.
        References      foreign references in delegation_work_items
                        (role_runtime_session_id,
                        claimed_by_role_runtime_session_id), seat_states,
                        and JSON metadata (tasks.metadata.delegation_role_
                        session_id, delegation_work_items.metadata.
                        assigned_role_runtime_id) are all redirected to
                        the canonical PK before losers are deleted.
        Idempotent      re-running the migration is a noop (every row is
                        already canonical).

        Returns counters for observability / tests.
        """
        # Deferred import — the company_runtime module transitively imports
        # layer2 phase machinery, which we also use; importing at module
        # scope would create a cycle during initial bootstrap.
        from opc.layer2_organization.company_runtime import canonical_role_session_id

        if self._db is None:
            return {
                "groups": 0,
                "canonical_written": 0,
                "deleted": 0,
                "refs_updated": 0,
            }

        stats = {
            "groups": 0,
            "canonical_written": 0,
            "deleted": 0,
            "refs_updated": 0,
        }

        # Collect every (run_id, role_id) with any rows — unlike the old
        # "duplicate" migration, this pass also catches single legacy rows
        # whose PK is not the 3-segment canonical form.
        async with self._db.execute(
            """SELECT DISTINCT run_id, role_id
               FROM role_runtime_sessions
               WHERE run_id != '' AND role_id != ''"""
        ) as cursor:
            groups = await cursor.fetchall()

        if not groups:
            return stats

        for run_id, role_id in groups:
            try:
                canonical_id = canonical_role_session_id(
                    run_id=str(run_id), role_id=str(role_id)
                )
            except ValueError:
                logger.warning(
                    f"role-session merge: cannot build canonical ID for "
                    f"run={run_id} role={role_id}; skipping"
                )
                continue

            async with self._db.execute(
                """SELECT * FROM role_runtime_sessions
                   WHERE run_id=? AND role_id=?
                   ORDER BY updated_at DESC""",
                (str(run_id), str(role_id)),
            ) as cursor:
                rows = await cursor.fetchall()
                columns = [desc[0] for desc in cursor.description]

            if not rows:
                continue

            row_dicts = [dict(zip(columns, row)) for row in rows]

            # Fast path: single row already at canonical PK → nothing to do.
            if len(row_dicts) == 1 and row_dicts[0]["role_session_id"] == canonical_id:
                continue

            stats["groups"] += 1

            merged = self._merge_role_session_rows(
                rows=row_dicts, canonical_id=canonical_id
            )

            # Upsert the merged row under the canonical PK in both tables.
            for table in ("role_runtime_sessions", "delegation_role_sessions"):
                await self._upsert_role_session_row(table=table, row=merged)
            stats["canonical_written"] += 1

            # Redirect every foreign reference from any non-canonical PK
            # in the group to the canonical PK. We do this before deleting
            # the rows so a crash mid-migration leaves references resolvable.
            losers = [
                rd["role_session_id"]
                for rd in row_dicts
                if rd["role_session_id"] != canonical_id
            ]
            for loser_id in losers:
                refs = await self._redirect_role_session_references(
                    source_id=loser_id, target_id=canonical_id
                )
                stats["refs_updated"] += refs

            # Delete the orphaned rows from both tables.
            for loser_id in losers:
                for table in ("role_runtime_sessions", "delegation_role_sessions"):
                    await self._db.execute(
                        f"DELETE FROM {table} WHERE role_session_id=?",
                        (loser_id,),
                    )
                stats["deleted"] += 1

        await self._db.commit()
        if stats["groups"]:
            logger.info(
                "role-session merge: "
                f"groups={stats['groups']} "
                f"canonical_written={stats['canonical_written']} "
                f"deleted={stats['deleted']} "
                f"refs_updated={stats['refs_updated']}"
            )
        return stats

    @staticmethod
    def _status_priority(status: str) -> int:
        """Higher priority wins when merging scalar state."""
        return {
            "running": 3,
            "reserved": 2,
            "blocked": 2,
            "idle": 1,
            "cold": 0,
        }.get((status or "").strip().lower(), 1)

    @classmethod
    def _merge_role_session_rows(
        cls, *, rows: list[dict[str, Any]], canonical_id: str
    ) -> dict[str, Any]:
        """Field-level merge of N role_runtime_sessions rows → single canonical
        row. See ``_migrate_role_sessions_merge_by_role`` for the policy.
        Rows must all belong to the same (run_id, role_id)."""
        # Pick the "active" row for scalar fields — the one most likely to
        # reflect the live state of the role. Ordering: has_focus desc,
        # status_priority desc, updated_at desc.
        active = max(
            rows,
            key=lambda r: (
                1 if (r.get("focused_work_item_id") or "").strip() else 0,
                cls._status_priority(r.get("status") or ""),
                str(r.get("updated_at") or ""),
            ),
        )

        # Team instance: prefer any non-empty value across the group. Old
        # short-form rows had it blank; the long-form row carries the truth.
        team_instance_id = str(active.get("team_instance_id") or "").strip()
        if not team_instance_id:
            for r in rows:
                candidate = str(r.get("team_instance_id") or "").strip()
                if candidate:
                    team_instance_id = candidate
                    break

        # Inbox: union + de-dup by message id, then sort by timestamp.
        inbox_messages: list[dict[str, Any]] = []
        seen_msg_ids: set[str] = set()
        for r in rows:
            state = _json_loads(r.get("inbox_state") or "{}", {})
            for msg in list(state.get("messages", []) or []):
                if not isinstance(msg, dict):
                    continue
                mid = str(msg.get("message_id") or msg.get("id") or "").strip()
                # Preserve order for messages that have no ID (rare) — use
                # a synthetic marker so they don't all collide on "".
                key = mid or f"__noid__::{len(inbox_messages)}"
                if key in seen_msg_ids:
                    continue
                seen_msg_ids.add(key)
                inbox_messages.append(dict(msg))
        inbox_messages.sort(key=lambda m: str(m.get("timestamp") or m.get("created_at") or ""))
        # Preserve any non-messages keys from the active row's inbox_state.
        active_inbox = _json_loads(active.get("inbox_state") or "{}", {})
        active_inbox["messages"] = inbox_messages
        merged_inbox_state = active_inbox

        # Memory slices: union dict[work_item_id → list], merging lists per key.
        # Iterate oldest-first so older notes appear before newer ones in the
        # merged list (natural reading order; rows come in DESC so reverse).
        merged_memory: dict[str, list[Any]] = {}
        for r in reversed(rows):
            slices = _json_loads(r.get("memory_slices_by_work_item") or "{}", {})
            if not isinstance(slices, dict):
                continue
            for wid, items in slices.items():
                if not isinstance(items, list):
                    continue
                merged_memory.setdefault(str(wid), []).extend(items)
        # De-dup identical entries per work item (keep order).
        for wid, items in list(merged_memory.items()):
            seen: set[str] = set()
            deduped: list[Any] = []
            for entry in items:
                sig = _json_dumps(entry) if not isinstance(entry, str) else entry
                if sig in seen:
                    continue
                seen.add(sig)
                deduped.append(entry)
            merged_memory[wid] = deduped

        # List unions (order: active row's entries first, others appended).
        def _union_list(field: str) -> list[str]:
            merged: list[str] = []
            seen: set[str] = set()
            for r in [active] + [r for r in rows if r is not active]:
                raw = _json_loads(r.get(field) or "[]", [])
                for item in raw if isinstance(raw, list) else []:
                    normalized = str(item)
                    if normalized in seen:
                        continue
                    seen.add(normalized)
                    merged.append(normalized)
            return merged

        background_ids = _union_list("background_work_item_ids")
        manager_role_ids = _union_list("manager_role_ids")
        manager_seat_ids = _union_list("manager_seat_ids")
        seat_ids = _union_list("seat_ids")
        # Pending queue: preserve FIFO — active row's queue first (these
        # are the ones already committed to this role's runtime), then any
        # extras from siblings. De-dup but keep earliest occurrence.
        pending_ids = _union_list("pending_work_item_ids")

        # Adapter session state (codex / LLM resume token): active row
        # wins; every other row's state is retained as audit breadcrumbs.
        active_adapter = _json_loads(active.get("adapter_session_state") or "{}", {})
        adapter_audit: list[dict[str, Any]] = []
        for r in rows:
            if r is active:
                continue
            raw_state = r.get("adapter_session_state") or ""
            if not raw_state or raw_state in ("{}", "null"):
                continue
            adapter_audit.append(
                {
                    "source_role_session_id": str(r.get("role_session_id") or ""),
                    "updated_at": str(r.get("updated_at") or ""),
                    "adapter_session_state": _json_loads(raw_state, {}),
                }
            )

        # Base metadata: start from active row's metadata, then append the
        # adapter audit list (append, don't overwrite — a role that has
        # been merged multiple times retains its full trail).
        merged_metadata = _json_loads(active.get("metadata") or "{}", {})
        if not isinstance(merged_metadata, dict):
            merged_metadata = {}
        if adapter_audit:
            existing_audit = list(merged_metadata.get("adapter_session_state_audit", []) or [])
            existing_audit.extend(adapter_audit)
            merged_metadata["adapter_session_state_audit"] = existing_audit

        # team_instance_id history — diagnostic trail of which team contexts
        # this role has been seen in. Useful when debugging cross-team flows.
        prior_team_instances = [
            str(r.get("team_instance_id") or "").strip()
            for r in rows
            if str(r.get("team_instance_id") or "").strip()
        ]
        if prior_team_instances:
            existing_history = list(merged_metadata.get("team_instance_history", []) or [])
            existing_history.extend(prior_team_instances)
            # De-dup preserving order.
            dedup_history: list[str] = []
            seen_tid: set[str] = set()
            for tid in existing_history:
                if tid in seen_tid:
                    continue
                seen_tid.add(tid)
                dedup_history.append(tid)
            merged_metadata["team_instance_history"] = dedup_history

        # Preserve the oldest created_at across the group (role has been
        # around since the earliest row was written), use the most recent
        # updated_at (merged row reflects the latest activity).
        created_at = min(str(r.get("created_at") or "") for r in rows if r.get("created_at"))
        updated_at = max(str(r.get("updated_at") or "") for r in rows if r.get("updated_at"))
        if not updated_at:
            updated_at = datetime.now().isoformat()

        return {
            "role_session_id": canonical_id,
            "run_id": str(active.get("run_id") or ""),
            "project_id": str(active.get("project_id") or "default"),
            "team_instance_id": team_instance_id,
            "team_id": str(active.get("team_id") or ""),
            "role_id": str(active.get("role_id") or ""),
            "seat_id": str(active.get("seat_id") or ""),
            "seat_state_id": str(active.get("seat_state_id") or ""),
            "employee_id": str(active.get("employee_id") or ""),
            "focused_work_item_id": str(active.get("focused_work_item_id") or ""),
            "background_work_item_ids": _json_dumps(background_ids),
            "manager_role_ids": _json_dumps(manager_role_ids),
            "manager_seat_ids": _json_dumps(manager_seat_ids),
            "seat_ids": _json_dumps(seat_ids),
            "adapter_session_state": _json_dumps(active_adapter),
            "inbox_state": _json_dumps(merged_inbox_state),
            "memory_slices_by_work_item": _json_dumps(merged_memory),
            "resume_state": active.get("resume_state") or "{}",
            "current_work_item": active.get("current_work_item") or "{}",
            "latest_notification": active.get("latest_notification") or "{}",
            "manager_digest": active.get("manager_digest") or "{}",
            "status": normalize_role_runtime_status(
                active.get("status"),
                active.get("focused_work_item_id"),
            ),
            "pending_work_item_ids": _json_dumps(pending_ids),
            "metadata": _json_dumps(merged_metadata),
            "created_at": created_at or updated_at,
            "updated_at": updated_at,
        }

    async def _upsert_role_session_row(self, *, table: str, row: dict[str, Any]) -> None:
        """Write the merged row under its canonical PK. ``INSERT OR REPLACE``
        so an earlier migration pass (or a pre-existing canonical row) is
        overwritten with the merged state."""
        assert self._db is not None
        columns = [
            "role_session_id", "run_id", "project_id", "team_instance_id",
            "team_id", "role_id", "seat_id", "seat_state_id", "employee_id",
            "focused_work_item_id", "background_work_item_ids",
            "manager_role_ids", "manager_seat_ids", "seat_ids",
            "adapter_session_state", "inbox_state", "memory_slices_by_work_item",
            "resume_state", "current_work_item", "latest_notification",
            "manager_digest", "status", "pending_work_item_ids",
            "metadata", "created_at", "updated_at",
        ]
        placeholders = ", ".join("?" for _ in columns)
        await self._db.execute(
            f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            tuple(row.get(c) for c in columns),
        )

    async def _redirect_role_session_references(
        self,
        *,
        source_id: str,
        target_id: str,
    ) -> int:
        """Rewrite every table/column that references ``source_id`` to
        ``target_id``. Returns the number of rows modified (for observability).

        Scope is intentionally explicit: we know which columns hold
        role_session_id references and update exactly those. JSON metadata
        columns (tasks.metadata, delegation_work_items.metadata) are
        rewritten with a targeted JSON-level replace that only touches the
        specific keys we control — we never blindly string-replace the
        JSON blob.
        """
        assert self._db is not None
        total = 0

        # delegation_work_items: two direct columns.
        cursor = await self._db.execute(
            """UPDATE delegation_work_items
               SET role_runtime_session_id=?,
                   updated_at=?
               WHERE role_runtime_session_id=?""",
            (target_id, datetime.now().isoformat(), source_id),
        )
        total += getattr(cursor, "rowcount", 0) or 0
        cursor = await self._db.execute(
            """UPDATE delegation_work_items
               SET claimed_by_role_runtime_session_id=?,
                   updated_at=?
               WHERE claimed_by_role_runtime_session_id=?""",
            (target_id, datetime.now().isoformat(), source_id),
        )
        total += getattr(cursor, "rowcount", 0) or 0

        # seat_states: single column.
        cursor = await self._db.execute(
            """UPDATE seat_states
               SET role_runtime_session_id=?,
                   updated_at=?
               WHERE role_runtime_session_id=?""",
            (target_id, datetime.now().isoformat(), source_id),
        )
        total += getattr(cursor, "rowcount", 0) or 0

        # JSON metadata references: load, rewrite keys, store.
        # delegation_work_items.metadata.assigned_role_runtime_id
        async with self._db.execute(
            """SELECT work_item_id, metadata FROM delegation_work_items
               WHERE metadata LIKE ?""",
            (f'%"{source_id}"%',),
        ) as cursor:
            wi_rows = await cursor.fetchall()
        for work_item_id, metadata_json in wi_rows:
            meta = _json_loads(metadata_json, {})
            mutated = False
            if str(meta.get("assigned_role_runtime_id", "")) == source_id:
                meta["assigned_role_runtime_id"] = target_id
                mutated = True
            if mutated:
                await self._db.execute(
                    """UPDATE delegation_work_items
                       SET metadata=?, updated_at=?
                       WHERE work_item_id=?""",
                    (_json_dumps(meta), datetime.now().isoformat(), work_item_id),
                )
                total += 1

        # tasks.metadata.delegation_role_session_id
        async with self._db.execute(
            """SELECT id, metadata FROM tasks
               WHERE metadata LIKE ?""",
            (f'%"{source_id}"%',),
        ) as cursor:
            task_rows = await cursor.fetchall()
        for task_id, metadata_json in task_rows:
            meta = _json_loads(metadata_json, {})
            mutated = False
            if str(meta.get("delegation_role_session_id", "")) == source_id:
                meta["delegation_role_session_id"] = target_id
                mutated = True
            if mutated:
                await self._db.execute(
                    """UPDATE tasks SET metadata=? WHERE id=?""",
                    (_json_dumps(meta), task_id),
                )
                total += 1

        return total

    async def save_delegation_work_item(
        self,
        item: DelegationWorkItem,
    ) -> None:
        await self._write_delegation_work_item(item, if_absent=False)

    async def _write_delegation_work_item(
        self,
        item: DelegationWorkItem,
        *,
        if_absent: bool,
    ) -> bool:
        # Single-source-of-truth gate: every write — whether it goes through
        # update_delegation_work_item or directly mutates `item.phase` and
        # then calls save — passes through validate_transition. Skipping the
        # validation requires a separate code path; there is no way to write
        # an invalid phase by accident.
        existing = await self.get_delegation_work_item(item.work_item_id)
        if if_absent and existing is not None:
            return False
        previous_phase = existing.phase if existing is not None else None
        validate_transition(previous_phase, item.phase)
        item.metadata = dict(item.metadata or {})
        if (
            self._metadata_has_work_item_projection_identity(item.metadata)
            or str(item.projection_id or "").strip()
            or str(item.kind or "").strip()
        ):
            item.metadata, _ = migrate_work_item_projection_metadata(
                item.metadata,
                projection_id_fallback=str(item.projection_id or item.work_item_id or "").strip(),
                turn_type_fallback=str(item.kind or "").strip(),
            )
        # Capture pre-write state we need for hook context (the persisted
        # row's previous claim, etc.) — hooks fire on the saved item but
        # may want to know "what changed".
        target_phase = item.phase
        db = self._require_db()
        conflict_action = (
            "DO NOTHING"
            if if_absent
            else """DO UPDATE SET
                run_id=excluded.run_id,
                cell_id=excluded.cell_id,
                team_instance_id=excluded.team_instance_id,
                team_id=excluded.team_id,
                role_id=excluded.role_id,
                seat_id=excluded.seat_id,
                seat_state_id=excluded.seat_state_id,
                role_runtime_session_id=excluded.role_runtime_session_id,
                parent_work_item_id=excluded.parent_work_item_id,
                source_role_id=excluded.source_role_id,
                source_seat_id=excluded.source_seat_id,
                title=excluded.title,
                summary=excluded.summary,
                kind=excluded.kind,
                projection_id=excluded.projection_id,
                phase=excluded.phase,
                batch_id=excluded.batch_id,
                batch_index=excluded.batch_index,
                deliverable_summary=excluded.deliverable_summary,
                blocked_reason=excluded.blocked_reason,
                handoff_status=excluded.handoff_status,
                continuation_source=excluded.continuation_source,
                manager_role_id=excluded.manager_role_id,
                manager_seat_id=excluded.manager_seat_id,
                claimed_by_role_runtime_session_id=excluded.claimed_by_role_runtime_session_id,
                claimed_by_seat_id=excluded.claimed_by_seat_id,
                metadata=excluded.metadata,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at"""
        )
        cursor = await db.execute(
            f"""INSERT INTO delegation_work_items
            (work_item_id, run_id, cell_id, team_instance_id, team_id, role_id, seat_id, seat_state_id,
             role_runtime_session_id, parent_work_item_id, source_role_id, source_seat_id, title, summary,
             kind, projection_id, phase, batch_id, batch_index,
             deliverable_summary, blocked_reason, handoff_status, continuation_source, manager_role_id,
             manager_seat_id, claimed_by_role_runtime_session_id, claimed_by_seat_id, metadata,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_item_id) {conflict_action}""",
            (
                item.work_item_id,
                item.run_id,
                item.cell_id,
                item.team_instance_id,
                item.team_id,
                item.role_id,
                item.seat_id,
                item.seat_state_id,
                item.role_runtime_session_id,
                item.parent_work_item_id,
                item.source_role_id,
                item.source_seat_id,
                item.title,
                item.summary,
                item.kind,
                item.projection_id,
                item.phase.value,
                item.batch_id,
                int(item.batch_index or 0),
                item.deliverable_summary,
                item.blocked_reason,
                item.handoff_status,
                item.continuation_source,
                item.manager_role_id,
                item.manager_seat_id,
                item.claimed_by_role_runtime_session_id,
                item.claimed_by_seat_id,
                _json_dumps(item.metadata),
                item.created_at.isoformat(),
                item.updated_at.isoformat(),
            ),
        )
        await db.commit()
        if if_absent and not (getattr(cursor, "rowcount", 0) or 0):
            return False
        # D2 hook fire — propagate phase change to dependent layers
        # (task.status, role_session.status, dispatcher wake, etc.). All
        # writes to delegation_work_items pass through here, so this is
        # the single chokepoint where hooks fire.
        try:
            await on_phase_transition(previous_phase, target_phase, item, store=self)
        except Exception:  # never let hook failures break the write
            logger.opt(exception=True).debug("on_phase_transition raised at top level")
        return True

    async def insert_delegation_work_item_if_absent(
        self,
        item: DelegationWorkItem,
    ) -> bool:
        """Atomically create a WorkItem without overwriting a concurrent claim.

        Auxiliary report/review IDs are deterministic.  A read-before-write
        check is insufficient because another dispatcher can create and claim
        the same card between those operations; the conflict decision must be
        made by SQLite in the insert statement itself.
        """
        return await self._write_delegation_work_item(item, if_absent=True)

    async def list_delegation_work_items(
        self,
        run_id: str,
        *,
        team_instance_id: str | None = None,
        team_id: str | None = None,
        seat_id: str | None = None,
        role_runtime_session_id: str | None = None,
        role_id: str | None = None,
        batch_id: str | None = None,
    ) -> list[DelegationWorkItem]:
        db = self._require_db()
        query = "SELECT * FROM delegation_work_items WHERE run_id = ?"
        params: list[Any] = [run_id]
        if team_instance_id:
            query += " AND team_instance_id = ?"
            params.append(team_instance_id)
        if team_id:
            query += " AND team_id = ?"
            params.append(team_id)
        if seat_id:
            query += " AND seat_id = ?"
            params.append(seat_id)
        if role_runtime_session_id:
            query += " AND role_runtime_session_id = ?"
            params.append(role_runtime_session_id)
        if role_id:
            query += " AND role_id = ?"
            params.append(role_id)
        if batch_id:
            query += " AND batch_id = ?"
            params.append(batch_id)
        query += " ORDER BY created_at ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_delegation_work_item(row, cursor.description) for row in rows]

    async def list_manager_board(
        self,
        run_id: str,
        *,
        manager_seat_id: str,
        parent_work_item_id: str | None = None,
    ) -> list[DelegationWorkItem]:
        db = self._require_db()
        query = "SELECT * FROM delegation_work_items WHERE run_id = ? AND manager_seat_id = ?"
        params: list[Any] = [run_id, str(manager_seat_id or "").strip()]
        normalized_parent = str(parent_work_item_id or "").strip()
        if normalized_parent:
            query += " AND parent_work_item_id = ?"
            params.append(normalized_parent)
        query += " ORDER BY batch_index ASC, created_at ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_delegation_work_item(row, cursor.description) for row in rows]

    async def summarize_parent_status(
        self,
        run_id: str,
        *,
        manager_seat_id: str,
        parent_work_item_id: str,
    ) -> dict[str, Any]:
        parent_id = str(parent_work_item_id or "").strip()
        manager_id = str(manager_seat_id or "").strip()
        if not run_id or not parent_id or not manager_id:
            return {
                "run_id": str(run_id or "").strip(),
                "manager_seat_id": manager_id,
                "parent_work_item_id": parent_id,
                "total_children": 0,
                "phase_counts": {},
                "column_counts": {},
                "releasable_work_item_ids": [],
                "blocked_reasons": [],
                "blocker_count": 0,
                "rework_count": 0,
                "upstream_summary": [],
                "derived_parent_column": "todo",
            }
        children = await self.list_manager_board(
            run_id,
            manager_seat_id=manager_id,
            parent_work_item_id=parent_id,
        )
        phase_counts: dict[str, int] = {}
        column_counts: dict[str, int] = {}
        blocked_reasons: list[str] = []
        releasable_work_item_ids: list[str] = []
        upstream_summary: list[dict[str, Any]] = []
        blocker_count = 0
        rework_count = 0
        done_children = 0
        active_children = 0
        review_children = 0
        todo_children = 0
        visible_children = 0
        for item in children:
            phase = item.phase
            metadata = dict(item.metadata or {})
            column_value = kanban_column(phase)
            phase_counts[phase.value] = phase_counts.get(phase.value, 0) + 1
            column_counts[column_value] = column_counts.get(column_value, 0) + 1
            if item.blocked_reason:
                blocked_reasons.append(str(item.blocked_reason).strip())
            if item.blocked_reason or phase in {
                Phase.WAITING_FOR_PEER,
                Phase.WAITING_FOR_CHILDREN,
                Phase.NEEDS_ATTENTION,
                Phase.WAITING_DEPENDENCIES,
            }:
                blocker_count += 1
            if str(metadata.get("rework_feedback", "") or "").strip():
                rework_count += 1
            if phase == Phase.QUEUED:
                releasable_work_item_ids.append(str(item.work_item_id))
            if metadata.get("hidden_from_company_kanban"):
                continue
            visible_children += 1
            if phase in DONE_PHASES:
                done_children += 1
            elif phase in IN_PROGRESS_PHASES:
                active_children += 1
            elif phase in IN_REVIEW_PHASES:
                review_children += 1
            else:
                todo_children += 1
            visibility = str(metadata.get("upstream_visibility", "summary_only") or "summary_only").strip().lower()
            if visibility != "hidden":
                payload = {
                    "work_item_id": str(item.work_item_id),
                    "title": str(item.title or "").strip(),
                    "role_id": str(item.role_id or "").strip(),
                    "phase": phase.value,
                    "kanban_column": column_value,
                    "deliverable_summary": str(item.deliverable_summary or "").strip(),
                    "blocked_reason": str(item.blocked_reason or "").strip(),
                    "completion_report": str(metadata.get("completion_report", "") or "").strip(),
                    "review_owner_role_id": str(
                        metadata.get("review_owner_role_id")
                        or item.manager_role_id
                        or ""
                    ).strip(),
                    "review_owner_seat_id": str(
                        metadata.get("review_owner_seat_id")
                        or item.manager_seat_id
                        or ""
                    ).strip(),
                    "review_evidence": dict(metadata.get("review_evidence", {}) or {}),
                }
                if visibility == "debug":
                    payload["summary"] = str(item.summary or "").strip()
                    payload["dependency_work_item_ids"] = [
                        str(dep).strip()
                        for dep in list(metadata.get("dependency_work_item_ids", []) or [])
                        if str(dep).strip()
                    ]
                upstream_summary.append(payload)
        derived_parent_column = "todo"
        if visible_children and done_children == visible_children:
            derived_parent_column = "done"
        elif review_children:
            derived_parent_column = "in_review"
        elif active_children:
            derived_parent_column = "in_progress"
        return {
            "run_id": str(run_id or "").strip(),
            "manager_seat_id": manager_id,
            "parent_work_item_id": parent_id,
            "total_children": visible_children,
            "phase_counts": phase_counts,
            "column_counts": column_counts,
            "releasable_work_item_ids": releasable_work_item_ids,
            "blocked_reasons": list(dict.fromkeys(item for item in blocked_reasons if item)),
            "blocker_count": blocker_count,
            "rework_count": rework_count,
            "upstream_summary": upstream_summary[:12],
            "derived_parent_column": derived_parent_column,
        }

    async def get_delegation_work_item(self, work_item_id: str) -> DelegationWorkItem | None:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM delegation_work_items WHERE work_item_id = ?",
            (work_item_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_delegation_work_item(row, cursor.description)

    async def update_delegation_work_item(
        self,
        work_item_id: str,
        *,
        team_instance_id: str | None = None,
        team_id: str | None = None,
        seat_id: str | None = None,
        seat_state_id: str | None = None,
        role_runtime_session_id: str | None = None,
        phase: Phase | str | None = None,
        summary: str | None = None,
        batch_id: str | None = None,
        batch_index: int | None = None,
        deliverable_summary: str | None = None,
        blocked_reason: str | None = None,
        handoff_status: str | None = None,
        continuation_source: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
        metadata_unset: list[str] | tuple[str, ...] | None = None,
        manager_role_id: str | None = None,
        manager_seat_id: str | None = None,
        claimed_by_role_runtime_session_id: str | None = None,
        claimed_by_seat_id: str | None = None,
    ) -> DelegationWorkItem | None:
        item = await self.get_delegation_work_item(work_item_id)
        if item is None:
            return None
        previous_phase = item.phase
        if team_instance_id is not None:
            item.team_instance_id = str(team_instance_id or "").strip()
        if team_id is not None:
            item.team_id = str(team_id or "").strip()
        if seat_id is not None:
            item.seat_id = str(seat_id or "").strip()
        if seat_state_id is not None:
            item.seat_state_id = str(seat_state_id or "").strip()
        if role_runtime_session_id is not None:
            item.role_runtime_session_id = str(role_runtime_session_id or "").strip()
        if phase is not None:
            target_phase = coerce_phase(phase)
            validate_transition(previous_phase, target_phase)
            item.phase = target_phase
        if summary is not None:
            item.summary = summary
        if batch_id is not None:
            item.batch_id = str(batch_id or "").strip()
        if batch_index is not None:
            item.batch_index = int(batch_index)
        if deliverable_summary is not None:
            item.deliverable_summary = str(deliverable_summary or "").strip()
        if blocked_reason is not None:
            item.blocked_reason = str(blocked_reason or "").strip()
        if handoff_status is not None:
            item.handoff_status = str(handoff_status or "").strip()
        if continuation_source is not None:
            item.continuation_source = str(continuation_source or "").strip()
        if manager_role_id is not None:
            item.manager_role_id = str(manager_role_id or "").strip()
        if manager_seat_id is not None:
            item.manager_seat_id = str(manager_seat_id or "").strip()
        if claimed_by_role_runtime_session_id is not None:
            item.claimed_by_role_runtime_session_id = str(claimed_by_role_runtime_session_id or "").strip()
        if claimed_by_seat_id is not None:
            item.claimed_by_seat_id = str(claimed_by_seat_id or "").strip()
        if metadata_unset or metadata_updates:
            metadata = dict(item.metadata or {})
            for key in list(metadata_unset or []):
                metadata.pop(str(key), None)
            if metadata_updates:
                metadata.update(dict(metadata_updates))
            item.metadata = metadata
        item.updated_at = datetime.now()
        await self.save_delegation_work_item(item)
        return item

    async def claim_delegation_work_item_if_dispatchable(
        self,
        work_item_id: str,
        *,
        expected_phase: Phase | str,
        role_runtime_session_id: str,
        seat_id: str,
        task_id: str,
        work_item_revision: int = 0,
    ) -> DelegationWorkItem | None:
        """Atomically claim an unheld, unowned dispatchable WorkItem.

        The dispatcher may be operating on a snapshot loaded before a Stop or
        shutdown transition.  Keeping the phase, claim, queue, and durable
        hold predicates in the same UPDATE prevents that stale snapshot from
        resurrecting a suspended WorkItem.
        """

        phase = coerce_phase(expected_phase)
        dispatchable_phases = {
            Phase.READY,
            Phase.READY_FOR_REWORK,
            *IN_PROGRESS_PHASES,
        }
        if phase not in dispatchable_phases:
            return None
        if phase != Phase.RUNNING:
            validate_transition(phase, Phase.RUNNING)
        role_session_id = str(role_runtime_session_id or "").strip()
        claimed_task_id = str(task_id or "").strip()
        if not work_item_id or not role_session_id or not claimed_task_id:
            return None

        updated_at = datetime.now()
        db = self._require_db()
        cursor = await db.execute(
            """UPDATE delegation_work_items
               SET phase = ?,
                   role_runtime_session_id = ?,
                   claimed_by_role_runtime_session_id = ?,
                   claimed_by_seat_id = ?,
                   metadata = json_set(
                       COALESCE(NULLIF(metadata, ''), '{}'),
                       '$.claimed_by_role_session_id', ?,
                       '$.claimed_task_id', ?,
                       '$.claimed_work_item_revision', ?,
                       '$.attempt_seq', COALESCE(
                           CAST(json_extract(metadata, '$.attempt_seq') AS INTEGER),
                           0
                       ) + 1,
                       '$.attempt_settled', json('false'),
                       '$.attempt_outcome', '',
                       '$.attempt_started_at', ?
                   ),
                   updated_at = ?
               WHERE work_item_id = ?
                 AND phase = ?
                 AND COALESCE(claimed_by_role_runtime_session_id, '') = ''
                 AND COALESCE(claimed_by_seat_id, '') = ''
                 AND COALESCE(json_extract(metadata, '$.claimed_by_role_session_id'), '') = ''
                 AND COALESCE(json_extract(metadata, '$.claimed_task_id'), '') = ''
                 AND COALESCE(json_extract(metadata, '$.dispatch_hold'), '') = ''
                 AND COALESCE(json_extract(metadata, '$.queued_behind_session'), '') = ''
                 AND COALESCE(
                       CAST(json_extract(metadata, '$.manager_mutation_revision') AS INTEGER),
                       0
                     ) = ?""",
            (
                Phase.RUNNING.value,
                role_session_id,
                role_session_id,
                str(seat_id or "").strip(),
                role_session_id,
                claimed_task_id,
                int(work_item_revision or 0),
                updated_at.isoformat(),
                updated_at.isoformat(),
                str(work_item_id or "").strip(),
                phase.value,
                int(work_item_revision or 0),
            ),
        )
        await db.commit()
        if not (getattr(cursor, "rowcount", 0) or 0):
            return None
        persisted = await self.get_delegation_work_item(work_item_id)
        if persisted is None:
            return None
        persisted_metadata = dict(persisted.metadata or {})
        if (
            persisted.phase != Phase.RUNNING
            or str(persisted.claimed_by_role_runtime_session_id or "").strip()
            != role_session_id
            or str(persisted_metadata.get("claimed_by_role_session_id", "") or "").strip()
            != role_session_id
            or str(persisted_metadata.get("claimed_task_id", "") or "").strip()
            != claimed_task_id
            or str(persisted_metadata.get("dispatch_hold", "") or "").strip()
            or str(persisted_metadata.get("queued_behind_session", "") or "").strip()
        ):
            # Stop/shutdown may have won immediately after the CAS commit and
            # cleared this claim while adding a durable hold.  The committed
            # UPDATE is not permission to spawn once that newer state exists.
            return None
        # Do not fire the generic phase hooks here.  The executor's first
        # idempotent RUNNING transition performs projection.  Deferring it
        # avoids a post-commit hook racing after Stop and projecting the Task
        # back to RUNNING from an already-held WorkItem.
        return persisted

    async def apply_delegation_review_resolution(
        self,
        work_item_id: str,
        *,
        source_report_work_item_id: str,
        target_phase: Phase | str,
        blocked_reason: str,
        metadata_updates: dict[str, Any],
    ) -> DelegationWorkItem | None:
        """Atomically apply a manager verdict to its exact report generation.

        The phase predicate and latest-applied-report predicate live in the
        same SQLite UPDATE as the child phase + applied-stamp write. This
        prevents a late manager turn from crossing an AWAITING_HUMAN
        transition or approving an older report after a newer report landed.
        ``updated_at`` provides optimistic metadata concurrency; a few retries
        preserve unrelated same-phase updates without weakening either guard.
        """
        target = coerce_phase(target_phase)
        validate_transition(Phase.AWAITING_MANAGER_REVIEW, target)
        expected_source = str(source_report_work_item_id or "").strip()
        db = self._require_db()

        for _attempt in range(3):
            item = await self.get_delegation_work_item(work_item_id)
            if item is None or item.phase != Phase.AWAITING_MANAGER_REVIEW:
                return None
            metadata = dict(item.metadata or {})
            metadata.update(dict(metadata_updates or {}))
            if (
                self._metadata_has_work_item_projection_identity(metadata)
                or str(item.projection_id or "").strip()
                or str(item.kind or "").strip()
            ):
                metadata, _ = migrate_work_item_projection_metadata(
                    metadata,
                    projection_id_fallback=str(
                        item.projection_id or item.work_item_id or ""
                    ).strip(),
                    turn_type_fallback=str(item.kind or "").strip(),
                )
            previous_updated_at = item.updated_at.isoformat()
            updated_at = datetime.now()
            cursor = await db.execute(
                """UPDATE delegation_work_items
                   SET phase = ?, blocked_reason = ?, metadata = ?, updated_at = ?
                   WHERE work_item_id = ?
                     AND phase = ?
                     AND updated_at = ?
                     AND COALESCE((
                         SELECT report.work_item_id
                         FROM delegation_work_items AS report
                         WHERE report.parent_work_item_id = ?
                           AND report.kind = 'report'
                           AND json_extract(
                               report.metadata,
                               '$.report_target_work_item_id'
                           ) = ?
                           AND json_extract(
                               report.metadata,
                               '$.report_card_outcome'
                           ) = 'applied'
                         ORDER BY report.batch_index DESC,
                                  report.created_at DESC,
                                  report.work_item_id DESC
                         LIMIT 1
                     ), '') = ?""",
                (
                    target.value,
                    str(blocked_reason or ""),
                    _json_dumps(metadata),
                    updated_at.isoformat(),
                    work_item_id,
                    Phase.AWAITING_MANAGER_REVIEW.value,
                    previous_updated_at,
                    work_item_id,
                    work_item_id,
                    expected_source,
                ),
            )
            await db.commit()
            if not (getattr(cursor, "rowcount", 0) or 0):
                continue
            persisted = await self.get_delegation_work_item(work_item_id)
            if persisted is None:
                return None
            try:
                await on_phase_transition(
                    Phase.AWAITING_MANAGER_REVIEW,
                    target,
                    persisted,
                    store=self,
                )
            except Exception:
                logger.opt(exception=True).debug(
                    "on_phase_transition raised after review-resolution CAS"
                )
            return persisted
        return None

    async def reopen_approved_delegation_work_item_for_rework(
        self,
        work_item_id: str,
        *,
        target_phase: Phase | str = Phase.READY_FOR_REWORK,
        summary: str | None = None,
        deliverable_summary: str | None = "",
        blocked_reason: str | None = "",
        metadata_updates: dict[str, Any] | None = None,
        metadata_unset: list[str] | tuple[str, ...] | None = None,
        release_claim: bool = True,
    ) -> DelegationWorkItem | None:
        """Named bypass for invalidating approved work after manager/user feedback."""
        item = await self.get_delegation_work_item(work_item_id)
        if item is None:
            return None
        previous_phase = item.phase
        target = coerce_phase(target_phase)
        if previous_phase != Phase.APPROVED:
            raise InvalidPhaseTransition(
                f"executive rework can only reopen approved work items, got {previous_phase.value}"
            )
        if target not in {Phase.READY_FOR_REWORK, Phase.READY}:
            raise InvalidPhaseTransition(
                f"executive rework target must be ready_for_rework or ready, got {target.value}"
            )

        item.phase = target
        if summary is not None:
            item.summary = str(summary or "").strip()
        if deliverable_summary is not None:
            item.deliverable_summary = str(deliverable_summary or "").strip()
        if blocked_reason is not None:
            item.blocked_reason = str(blocked_reason or "").strip()
        if release_claim:
            item.claimed_by_role_runtime_session_id = ""
            item.claimed_by_seat_id = ""

        metadata = dict(item.metadata or {})
        for key in list(metadata_unset or []):
            metadata.pop(str(key), None)
        if metadata_updates:
            metadata.update(dict(metadata_updates))
        if (
            self._metadata_has_work_item_projection_identity(metadata)
            or str(item.projection_id or "").strip()
            or str(item.kind or "").strip()
        ):
            metadata, _ = migrate_work_item_projection_metadata(
                metadata,
                projection_id_fallback=str(item.projection_id or item.work_item_id or "").strip(),
                turn_type_fallback=str(item.kind or "").strip(),
            )
        item.metadata = metadata
        item.updated_at = datetime.now()

        db = self._require_db()
        await db.execute(
            """UPDATE delegation_work_items
               SET phase=?,
                   summary=?,
                   deliverable_summary=?,
                   blocked_reason=?,
                   claimed_by_role_runtime_session_id=?,
                   claimed_by_seat_id=?,
                   metadata=?,
                   updated_at=?
               WHERE work_item_id=?""",
            (
                item.phase.value,
                item.summary,
                item.deliverable_summary,
                item.blocked_reason,
                item.claimed_by_role_runtime_session_id,
                item.claimed_by_seat_id,
                _json_dumps(item.metadata),
                item.updated_at.isoformat(),
                item.work_item_id,
            ),
        )
        await db.commit()
        try:
            await on_phase_transition(previous_phase, target, item, store=self)
        except Exception:
            logger.opt(exception=True).debug("on_phase_transition raised during approved work-item reopen")
        return item

    async def amend_delegation_work_item(
        self,
        work_item_id: str,
        *,
        title: str | None = None,
        summary: str | None = None,
        kind: str | None = None,
        role_id: str | None = None,
        seat_id: str | None = None,
        team_instance_id: str | None = None,
        team_id: str | None = None,
        role_runtime_session_id: str | None = None,
        claimed_by_role_runtime_session_id: str | None = None,
        claimed_by_seat_id: str | None = None,
        dependency_work_item_ids: list[str] | None = None,
        phase: Phase | str | None = None,
        metadata_set: dict[str, Any] | None = None,
        metadata_unset: list[str] | None = None,
    ) -> DelegationWorkItem | None:
        item = await self.get_delegation_work_item(work_item_id)
        if item is None:
            return None
        previous_phase = item.phase
        if title is not None:
            item.title = str(title or "").strip()
        if summary is not None:
            item.summary = str(summary or "").strip()
        if kind is not None:
            item.kind = str(kind or "").strip() or item.kind
        if role_id is not None:
            item.role_id = str(role_id or "").strip()
        if seat_id is not None:
            item.seat_id = str(seat_id or "").strip()
        if team_instance_id is not None:
            item.team_instance_id = str(team_instance_id or "").strip()
        if team_id is not None:
            item.team_id = str(team_id or "").strip()
        if role_runtime_session_id is not None:
            item.role_runtime_session_id = str(role_runtime_session_id or "").strip()
        if claimed_by_role_runtime_session_id is not None:
            item.claimed_by_role_runtime_session_id = str(claimed_by_role_runtime_session_id or "").strip()
        if claimed_by_seat_id is not None:
            item.claimed_by_seat_id = str(claimed_by_seat_id or "").strip()
        if phase is not None:
            target_phase = coerce_phase(phase)
            validate_transition(previous_phase, target_phase)
            item.phase = target_phase

        metadata = dict(item.metadata or {})
        if dependency_work_item_ids is not None:
            raw_dependencies = (
                [dependency_work_item_ids]
                if isinstance(dependency_work_item_ids, str)
                else list(dependency_work_item_ids or [])
            )
            metadata["dependency_work_item_ids"] = [
                str(dep).strip()
                for dep in raw_dependencies
                if str(dep).strip()
            ]
        if metadata_set:
            metadata.update(dict(metadata_set))
        for key in list(metadata_unset or []):
            metadata.pop(str(key), None)
        item.metadata = metadata
        item.updated_at = datetime.now()
        await self.save_delegation_work_item(item)
        return item

    async def replace_work_item_dependency(
        self,
        run_id: str,
        old_work_item_id: str,
        new_work_item_ids: list[str],
    ) -> list[DelegationWorkItem]:
        rid = str(run_id or "").strip()
        old_id = str(old_work_item_id or "").strip()
        raw_replacements = (
            [new_work_item_ids]
            if isinstance(new_work_item_ids, str)
            else list(new_work_item_ids or [])
        )
        replacements = [
            str(item).strip()
            for item in raw_replacements
            if str(item).strip()
        ]
        if not rid or not old_id:
            return []
        updated: list[DelegationWorkItem] = []
        for item in await self.list_delegation_work_items(rid):
            metadata = dict(item.metadata or {})
            dependency_ids = [
                str(dep).strip()
                for dep in list(metadata.get("dependency_work_item_ids", []) or [])
                if str(dep).strip()
            ]
            waiting_ids = [
                str(dep).strip()
                for dep in list(metadata.get("waiting_on_work_item_ids", []) or [])
                if str(dep).strip()
            ]
            if old_id not in dependency_ids and old_id not in waiting_ids:
                continue
            rewritten: list[str] = []
            for dep in dependency_ids:
                if dep == old_id:
                    rewritten.extend(replacements)
                else:
                    rewritten.append(dep)
            deduped = list(dict.fromkeys(dep for dep in rewritten if dep))
            rewritten_waiting: list[str] = []
            for dep in waiting_ids:
                if dep == old_id:
                    rewritten_waiting.extend(replacements)
                else:
                    rewritten_waiting.append(dep)
            item.metadata = {
                **metadata,
                "dependency_work_item_ids": deduped,
                "waiting_on_work_item_ids": list(
                    dict.fromkeys(dep for dep in rewritten_waiting if dep)
                ),
                "dependency_rewritten_from_work_item_id": old_id,
                "dependency_rewritten_at": datetime.now().isoformat(),
            }
            item.updated_at = datetime.now()
            await self.save_delegation_work_item(item)
            updated.append(item)
        return updated

    async def release_manager_work_items(
        self,
        run_id: str,
        *,
        manager_seat_id: str,
        work_item_ids: list[str] | None = None,
        parent_work_item_id: str | None = None,
        release_note: str = "",
        released_by_message_id: str = "",
        action_hint: str = "",
    ) -> list[DelegationWorkItem]:
        target_ids = {
            str(item).strip()
            for item in list(work_item_ids or [])
            if str(item).strip()
        }
        candidates = await self.list_manager_board(
            run_id,
            manager_seat_id=manager_seat_id,
            parent_work_item_id=parent_work_item_id,
        )
        released: list[DelegationWorkItem] = []
        for item in candidates:
            if target_ids and item.work_item_id not in target_ids:
                continue
            metadata_updates = {
                "last_release_note": str(release_note or "").strip(),
                "last_released_by_message_id": str(released_by_message_id or "").strip(),
                "last_release_action_hint": str(action_hint or "").strip(),
                "released_at": datetime.now().isoformat(),
            }
            target_phase = Phase.READY if item.phase == Phase.QUEUED else item.phase
            updated = await self.update_delegation_work_item(
                item.work_item_id,
                phase=target_phase,
                metadata_updates=metadata_updates,
            )
            if updated is not None:
                released.append(updated)
        return released

    async def rollup_manager_board(
        self,
        run_id: str,
        *,
        manager_seat_id: str,
        parent_work_item_id: str,
        summary: str = "",
        phase: Phase | str | None = None,
        blocked_reason: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rollup = await self.summarize_parent_status(
            run_id,
            manager_seat_id=manager_seat_id,
            parent_work_item_id=parent_work_item_id,
        )
        parent = await self.get_delegation_work_item(parent_work_item_id)
        if parent is not None:
            merged_updates = {
                "manager_board_rollup": dict(rollup),
                "manager_board_rollup_updated_at": datetime.now().isoformat(),
            }
            if summary:
                merged_updates["manager_board_rollup_summary"] = str(summary).strip()
            if metadata_updates:
                merged_updates.update(dict(metadata_updates))
            await self.update_delegation_work_item(
                parent_work_item_id,
                phase=phase,
                deliverable_summary=summary or None,
                blocked_reason=blocked_reason,
                metadata_updates=merged_updates,
            )
            refreshed_parent = await self.get_delegation_work_item(parent_work_item_id)
            if refreshed_parent is not None:
                rollup["parent_phase"] = refreshed_parent.phase.value
                rollup["parent_column"] = kanban_column(refreshed_parent.phase)
                rollup["parent_deliverable_summary"] = str(getattr(refreshed_parent, "deliverable_summary", "") or "").strip()
                rollup["parent_blocked_reason"] = str(getattr(refreshed_parent, "blocked_reason", "") or "").strip()
        return rollup

    async def save_delegation_event(self, event: DelegationEvent) -> None:
        db = self._require_db()
        await db.execute(
            """INSERT OR REPLACE INTO delegation_events
            (event_id, run_id, work_item_id, cell_id, role_id, event_type, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.run_id,
                event.work_item_id,
                event.cell_id,
                event.role_id,
                event.event_type,
                _json_dumps(event.payload),
                event.created_at.isoformat(),
            ),
        )
        await db.commit()

    async def list_delegation_events(self, run_id: str) -> list[DelegationEvent]:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM delegation_events WHERE run_id = ? ORDER BY created_at ASC",
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        return [
            DelegationEvent(
                event_id=data["event_id"],
                run_id=data["run_id"],
                work_item_id=data.get("work_item_id"),
                cell_id=data.get("cell_id"),
                role_id=data.get("role_id"),
                event_type=data["event_type"],
                payload=_json_loads(data.get("payload"), {}),
                created_at=datetime.fromisoformat(data["created_at"]),
            )
            for data in (dict(zip(cols, row)) for row in rows)
        ]

    # --- Dynamic reorg persistence ---

    async def save_reorg_proposal(self, proposal: ReorgProposal) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO reorg_proposals
            (proposal_id, project_id, session_id, task_id, initiated_by, source_role_id, scope, risk_level,
             status, title, summary, rationale, user_confirmation_required, old_org_version, new_org_version,
             changeset, migration_plan, impact_summary,
             approval_notes, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                proposal.proposal_id,
                proposal.project_id,
                proposal.session_id,
                proposal.task_id,
                proposal.initiated_by,
                proposal.source_role_id,
                proposal.scope.value,
                proposal.risk_level.value,
                proposal.status.value,
                proposal.title,
                proposal.summary,
                proposal.rationale,
                int(proposal.user_confirmation_required),
                proposal.old_org_version,
                proposal.new_org_version,
                _json_dumps(proposal.changeset.__dict__),
                _json_dumps(proposal.migration_plan.__dict__),
                _json_dumps(proposal.impact_summary),
                proposal.approval_notes,
                _json_dumps(proposal.metadata),
                proposal.created_at.isoformat(),
                proposal.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_reorg_proposal(self, proposal_id: str) -> ReorgProposal | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM reorg_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_reorg_proposal(row, cursor.description)

    async def list_reorg_proposals(
        self,
        project_id: str,
        status: ReorgProposalStatus | None = None,
        limit: int = 20,
    ) -> list[ReorgProposal]:
        assert self._db
        query = "SELECT * FROM reorg_proposals WHERE project_id = ?"
        params: list[Any] = [project_id]
        if status:
            query += " AND status = ?"
            params.append(status.value)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_reorg_proposal(row, cursor.description) for row in rows]

    def _row_to_reorg_proposal(self, row: Any, description: Any) -> ReorgProposal:
        from dataclasses import fields as _dc_fields

        from opc.core.models import (
            ReorgChangeSet,
            ReorgMigrationPlan,
            ReorgRoleChange,
            ReorgTaskAdjustment,
        )

        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        changeset_data = _json_loads(data.get("changeset"), {})
        migration_plan_data = _json_loads(data.get("migration_plan"), {})
        migration_plan_data = {
            k: v
            for k, v in migration_plan_data.items()
            if k in {f.name for f in _dc_fields(ReorgMigrationPlan)}
        }
        return ReorgProposal(
            proposal_id=data["proposal_id"],
            project_id=data["project_id"],
            session_id=data.get("session_id"),
            task_id=data.get("task_id"),
            initiated_by=data.get("initiated_by") or "owner",
            source_role_id=data.get("source_role_id") or "",
            scope=ReorgScope(data["scope"]),
            risk_level=ReorgRiskLevel(data["risk_level"]),
            status=ReorgProposalStatus(data["status"]),
            title=data.get("title") or "",
            summary=data.get("summary") or "",
            rationale=data.get("rationale") or "",
            user_confirmation_required=bool(data.get("user_confirmation_required", 1)),
            old_org_version=int(data.get("old_org_version") or 1),
            new_org_version=int(data.get("new_org_version") or 1),
            old_runtime_topology_version=int(data.get("old_runtime_topology_version") or 1),
            new_runtime_topology_version=int(data.get("new_runtime_topology_version") or 1),
            changeset=ReorgChangeSet(
                role_changes=[ReorgRoleChange(**item) for item in changeset_data.get("role_changes", [])],
                task_adjustments=[ReorgTaskAdjustment(**item) for item in changeset_data.get("task_adjustments", [])],
                metadata=dict(changeset_data.get("metadata", {})),
            ),
            migration_plan=ReorgMigrationPlan(**migration_plan_data),
            impact_summary=_json_loads(data.get("impact_summary"), {}),
            approval_notes=data.get("approval_notes") or "",
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def save_org_snapshot(self, snapshot: OrgSnapshot) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO org_snapshots
            (snapshot_id, project_id, org_version, company_name, topology, roles,
             company_profile, active_tasks, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot.snapshot_id,
                snapshot.project_id,
                snapshot.org_version,
                snapshot.company_name,
                snapshot.topology,
                _json_dumps(snapshot.roles),
                snapshot.company_profile,
                _json_dumps(snapshot.active_tasks),
                _json_dumps(snapshot.metadata),
                snapshot.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_org_snapshot(self, snapshot_id: str) -> OrgSnapshot | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM org_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_org_snapshot(row, cursor.description)

    async def get_latest_org_snapshot(self, project_id: str) -> OrgSnapshot | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM org_snapshots WHERE project_id = ? ORDER BY org_version DESC, created_at DESC LIMIT 1",
            (project_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_org_snapshot(row, cursor.description)

    def _row_to_org_snapshot(self, row: Any, description: Any) -> OrgSnapshot:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return OrgSnapshot(
            snapshot_id=data["snapshot_id"],
            project_id=data["project_id"],
            org_version=int(data.get("org_version") or 1),
            runtime_topology_version=int(data.get("runtime_topology_version") or 1),
            company_name=data.get("company_name") or "",
            topology=data.get("topology") or "",
            roles=_json_loads(data.get("roles"), []),
            company_profile=data.get("company_profile") or "corporate",
            active_tasks=_json_loads(data.get("active_tasks"), []),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def record_reorg_event(self, event: ReorgEventRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO reorg_events
            (event_id, proposal_id, project_id, event_kind, summary, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.proposal_id,
                event.project_id,
                event.event_kind.value,
                event.summary,
                _json_dumps(event.details),
                event.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def list_reorg_events(
        self,
        project_id: str,
        proposal_id: str | None = None,
        limit: int = 50,
    ) -> list[ReorgEventRecord]:
        assert self._db
        query = "SELECT * FROM reorg_events WHERE project_id = ?"
        params: list[Any] = [project_id]
        if proposal_id:
            query += " AND proposal_id = ?"
            params.append(proposal_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                ReorgEventRecord(
                    event_id=data["event_id"],
                    proposal_id=data.get("proposal_id") or "",
                    project_id=data["project_id"],
                    event_kind=ReorgEventKind(data["event_kind"]),
                    summary=data.get("summary") or "",
                    details=_json_loads(data.get("details"), {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
                for data in (dict(zip(cols, row)) for row in rows)
            ]

    async def get_tasks_by_ids(self, task_ids: list[str]) -> list[Task]:
        if not task_ids:
            return []
        assert self._db
        placeholders = ", ".join("?" for _ in task_ids)
        query = f"SELECT * FROM tasks WHERE id IN ({placeholders}) ORDER BY priority ASC, created_at ASC"
        async with self._db.execute(query, task_ids) as cursor:
            rows = await cursor.fetchall()
            tasks = [self._row_to_task(row, cursor.description) for row in rows]
        await self.hydrate_task_work_item_links(tasks)
        return tasks

    # --- Session memory ---

    async def save_session(self, session: SessionRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO sessions
            (session_id, project_id, parent_session_id, title, mode, status, summary, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.session_id,
                session.project_id,
                session.parent_session_id,
                session.title,
                session.mode,
                session.status,
                session.summary,
                _json_dumps(session.metadata),
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    def _row_to_session(self, row: Any, description: Any) -> SessionRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return SessionRecord(
            session_id=data["session_id"],
            project_id=data["project_id"],
            parent_session_id=data["parent_session_id"],
            title=data["title"],
            mode=data["mode"],
            status=data["status"],
            summary=data.get("summary") or "",
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def get_session(self, session_id: str) -> SessionRecord | None:
        assert self._db
        async with self._db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_session(row, cursor.description)

    async def list_sessions(
        self,
        project_id: str = "default",
        parent_session_id: str | None = None,
        limit: int = 50,
    ) -> list[SessionRecord]:
        assert self._db
        query = "SELECT * FROM sessions WHERE project_id = ?"
        params: list[Any] = [project_id]
        if parent_session_id is None:
            query += " AND parent_session_id IS NULL"
        else:
            query += " AND parent_session_id = ?"
            params.append(parent_session_id)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_session(row, cursor.description) for row in rows]

    async def touch_session(self, session_id: str) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (datetime.now().isoformat(), session_id),
        )
        await self._db.commit()

    async def save_session_message(self, message: SessionMessageRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO session_messages
            (message_id, session_id, role, task_id, agent_id, parent_message_id, summary_flag, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message.message_id,
                message.session_id,
                message.role,
                message.task_id,
                message.agent_id,
                message.parent_message_id,
                int(message.summary_flag),
                _json_dumps(message.metadata),
                message.created_at.isoformat(),
            ),
        )
        await self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (message.created_at.isoformat(), message.session_id),
        )
        await self._db.commit()

    def _row_to_session_message(self, row: Any, description: Any) -> SessionMessageRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return SessionMessageRecord(
            message_id=data["message_id"],
            session_id=data["session_id"],
            role=data["role"],
            task_id=data["task_id"],
            agent_id=data["agent_id"],
            parent_message_id=data["parent_message_id"],
            summary_flag=bool(data["summary_flag"]),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def get_session_message(self, message_id: str) -> SessionMessageRecord | None:
        assert self._db
        async with self._db.execute("SELECT * FROM session_messages WHERE message_id = ?", (message_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_session_message(row, cursor.description)

    async def list_session_messages(self, session_id: str) -> list[SessionMessageRecord]:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM session_messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_session_message(row, cursor.description) for row in rows]

    async def get_session_transcript_page(
        self,
        session_id: str,
        *,
        limit: int = 200,
        before_created_at: datetime | None = None,
        before_message_id: str | None = None,
        detail_level: str = "summary",
    ) -> dict[str, Any]:
        assert self._db
        normalized_limit = max(1, min(int(limit), 500))
        normalized_detail_level = normalize_transcript_detail_level(detail_level)
        visibility_sql, visibility_params = transcript_visibility_sql(
            detail_level=normalized_detail_level,
        )
        query = (
            "SELECT * FROM session_messages "
            "WHERE session_id = ? AND summary_flag = 0 "
        )
        params: list[Any] = [session_id]
        query += visibility_sql
        params.extend(visibility_params)
        normalized_before_id = str(before_message_id or "").strip()
        if before_created_at is not None:
            before_iso = before_created_at.isoformat()
            if normalized_before_id:
                query += " AND (created_at < ? OR (created_at = ? AND message_id < ?))"
                params.extend([before_iso, before_iso, normalized_before_id])
            else:
                query += " AND created_at < ?"
                params.append(before_iso)
        query += " ORDER BY created_at DESC, message_id DESC LIMIT ?"
        params.append(normalized_limit + 1)

        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            description = cursor.description

        has_more = len(rows) > normalized_limit
        visible_rows = rows[:normalized_limit]
        messages_desc = [self._row_to_session_message(row, description) for row in visible_rows]
        messages = list(reversed(messages_desc))
        parts_by_message: dict[str, list[SessionPartRecord]] = {}
        if messages:
            placeholders = ",".join("?" for _ in messages)
            part_params = [session_id, *[message.message_id for message in messages]]
            async with self._db.execute(
                f"SELECT * FROM session_parts WHERE session_id = ? AND message_id IN ({placeholders}) "
                "ORDER BY created_at ASC",
                part_params,
            ) as cursor:
                part_rows = await cursor.fetchall()
                part_description = cursor.description
            for row in part_rows:
                part = self._row_to_session_part(row, part_description)
                parts_by_message.setdefault(part.message_id, []).append(part)

        count_query = (
            "SELECT COUNT(*) FROM session_messages "
            "WHERE session_id = ? AND summary_flag = 0 "
        )
        count_params: list[Any] = [session_id]
        count_query += visibility_sql
        count_params.extend(visibility_params)
        async with self._db.execute(count_query, count_params) as cursor:
            row = await cursor.fetchone()
        total_count = int(row[0] or 0) if row else 0

        return {
            "messages": [
                {
                    "message": message,
                    "parts": parts_by_message.get(message.message_id, []),
                }
                for message in messages
            ],
            "has_more": has_more,
            "total_count": total_count,
        }

    async def save_session_part(self, part: SessionPartRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO session_parts
            (part_id, message_id, session_id, part_type, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                part.part_id,
                part.message_id,
                part.session_id,
                part.part_type,
                _json_dumps(part.payload),
                part.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    def _row_to_session_part(self, row: Any, description: Any) -> SessionPartRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return SessionPartRecord(
            part_id=data["part_id"],
            message_id=data["message_id"],
            session_id=data["session_id"],
            part_type=data["part_type"],
            payload=_json_loads(data.get("payload"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def list_session_parts(self, session_id: str, message_id: str | None = None) -> list[SessionPartRecord]:
        assert self._db
        query = "SELECT * FROM session_parts WHERE session_id = ?"
        params: list[Any] = [session_id]
        if message_id:
            query += " AND message_id = ?"
            params.append(message_id)
        query += " ORDER BY created_at ASC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_session_part(row, cursor.description) for row in rows]

    async def get_session_transcript(self, session_id: str) -> list[dict[str, Any]]:
        messages = await self.list_session_messages(session_id)
        parts = await self.list_session_parts(session_id)
        parts_by_message: dict[str, list[SessionPartRecord]] = {}
        for part in parts:
            parts_by_message.setdefault(part.message_id, []).append(part)
        return [
            {
                "message": message,
                "parts": parts_by_message.get(message.message_id, []),
            }
            for message in messages
        ]

    async def save_session_compaction(self, record: SessionCompactionRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO session_compactions
            (compaction_id, session_id, compaction_message_id, source_boundary_message_id, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                record.compaction_id,
                record.session_id,
                record.compaction_message_id,
                record.source_boundary_message_id,
                _json_dumps(record.metadata),
                record.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    def _row_to_session_compaction(self, row: Any, description: Any) -> SessionCompactionRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return SessionCompactionRecord(
            compaction_id=data["compaction_id"],
            session_id=data["session_id"],
            compaction_message_id=data["compaction_message_id"],
            source_boundary_message_id=data["source_boundary_message_id"],
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def get_latest_session_compaction(self, session_id: str) -> SessionCompactionRecord | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM session_compactions WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_session_compaction(row, cursor.description)

    async def save_session_memory_snapshot(self, record: SessionMemorySnapshotRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO session_memory_snapshots
            (snapshot_id, project_id, session_id, summary_message_id, source_boundary_message_id,
             summary_text, memory_text, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.snapshot_id,
                record.project_id,
                record.session_id,
                record.summary_message_id,
                record.source_boundary_message_id,
                record.summary_text,
                record.memory_text,
                _json_dumps(record.metadata),
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    def _row_to_session_memory_snapshot(self, row: Any, description: Any) -> SessionMemorySnapshotRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return SessionMemorySnapshotRecord(
            snapshot_id=data["snapshot_id"],
            project_id=data["project_id"],
            session_id=data["session_id"],
            summary_message_id=data["summary_message_id"],
            source_boundary_message_id=data["source_boundary_message_id"],
            summary_text=data.get("summary_text") or "",
            memory_text=data.get("memory_text") or "",
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def get_latest_session_memory_snapshot(self, session_id: str) -> SessionMemorySnapshotRecord | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM session_memory_snapshots WHERE session_id = ? ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_session_memory_snapshot(row, cursor.description)

    async def save_agent_compaction(self, record: AgentCompactionRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO agent_compactions
            (compaction_id, project_id, session_id, employee_id, role_id, compaction_message_id,
             source_boundary_message_id, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.compaction_id,
                record.project_id,
                record.session_id,
                record.employee_id,
                record.role_id,
                record.compaction_message_id,
                record.source_boundary_message_id,
                _json_dumps(record.metadata),
                record.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    def _row_to_agent_compaction(self, row: Any, description: Any) -> AgentCompactionRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return AgentCompactionRecord(
            compaction_id=data["compaction_id"],
            project_id=data["project_id"],
            session_id=data["session_id"],
            employee_id=data["employee_id"],
            role_id=data.get("role_id") or "",
            compaction_message_id=data["compaction_message_id"],
            source_boundary_message_id=data["source_boundary_message_id"],
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def get_latest_agent_compaction(
        self,
        *,
        project_id: str,
        session_id: str,
        employee_id: str,
    ) -> AgentCompactionRecord | None:
        assert self._db
        async with self._db.execute(
            """SELECT * FROM agent_compactions
            WHERE project_id = ? AND session_id = ? AND employee_id = ?
            ORDER BY created_at DESC LIMIT 1""",
            (project_id, session_id, employee_id),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_agent_compaction(row, cursor.description)

    async def save_agent_memory_snapshot(self, record: AgentMemorySnapshotRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO agent_memory_snapshots
            (snapshot_id, project_id, session_id, employee_id, role_id, memory_scope, memory_kind,
             summary_message_id, source_boundary_message_id, summary_text, memory_text,
             metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.snapshot_id,
                record.project_id,
                record.session_id,
                record.employee_id,
                record.role_id,
                record.memory_scope,
                record.memory_kind,
                record.summary_message_id,
                record.source_boundary_message_id,
                record.summary_text,
                record.memory_text,
                _json_dumps(record.metadata),
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    def _row_to_agent_memory_snapshot(self, row: Any, description: Any) -> AgentMemorySnapshotRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return AgentMemorySnapshotRecord(
            snapshot_id=data["snapshot_id"],
            project_id=data["project_id"],
            session_id=data["session_id"],
            employee_id=data["employee_id"],
            role_id=data.get("role_id") or "",
            memory_scope=data.get("memory_scope") or "session",
            memory_kind=data.get("memory_kind") or "process",
            summary_message_id=data["summary_message_id"],
            source_boundary_message_id=data["source_boundary_message_id"],
            summary_text=data.get("summary_text") or "",
            memory_text=data.get("memory_text") or "",
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def get_agent_memory_snapshot(
        self,
        *,
        project_id: str,
        session_id: str | None = None,
        employee_id: str,
        memory_kind: str | None = None,
        memory_scope: str | None = None,
    ) -> AgentMemorySnapshotRecord | None:
        assert self._db
        query = (
            "SELECT * FROM agent_memory_snapshots "
            "WHERE project_id = ? AND employee_id = ?"
        )
        params: list[Any] = [project_id, employee_id]
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)
        if memory_scope:
            query += " AND memory_scope = ?"
            params.append(memory_scope)
        if memory_kind:
            query += " AND memory_kind = ?"
            params.append(memory_kind)
        query += " ORDER BY updated_at DESC LIMIT 1"
        async with self._db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_agent_memory_snapshot(row, cursor.description)

    async def list_agent_memory_snapshots(
        self,
        *,
        project_id: str,
        session_id: str | None = None,
        employee_id: str,
        memory_kind: str | None = None,
        memory_scope: str | None = None,
    ) -> list[AgentMemorySnapshotRecord]:
        assert self._db
        query = (
            "SELECT * FROM agent_memory_snapshots "
            "WHERE project_id = ? AND employee_id = ?"
        )
        params: list[Any] = [project_id, employee_id]
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)
        if memory_scope:
            query += " AND memory_scope = ?"
            params.append(memory_scope)
        if memory_kind:
            query += " AND memory_kind = ?"
            params.append(memory_kind)
        query += " ORDER BY updated_at DESC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_agent_memory_snapshot(row, cursor.description) for row in rows]

    async def delete_agent_memory_snapshots(
        self,
        *,
        project_id: str,
        session_id: str | None = None,
        employee_id: str,
        memory_kind: str | None = None,
        memory_scope: str | None = None,
    ) -> None:
        assert self._db
        query = (
            "DELETE FROM agent_memory_snapshots "
            "WHERE project_id = ? AND employee_id = ?"
        )
        params: list[Any] = [project_id, employee_id]
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)
        if memory_scope:
            query += " AND memory_scope = ?"
            params.append(memory_scope)
        if memory_kind:
            query += " AND memory_kind = ?"
            params.append(memory_kind)
        await self._db.execute(query, params)
        await self._db.commit()

    async def save_session_link(self, link: SessionLinkRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO session_links
            (link_id, project_id, session_id, linked_session_id, task_id, link_type, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                link.link_id,
                link.project_id,
                link.session_id,
                link.linked_session_id,
                link.task_id,
                link.link_type,
                _json_dumps(link.metadata),
                link.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_session_links(
        self,
        session_id: str,
        link_type: str | None = None,
        limit: int = 50,
    ) -> list[SessionLinkRecord]:
        assert self._db
        query = "SELECT * FROM session_links WHERE session_id = ?"
        params: list[Any] = [session_id]
        if link_type:
            query += " AND link_type = ?"
            params.append(link_type)
        query += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                SessionLinkRecord(
                    link_id=data["link_id"],
                    project_id=data["project_id"],
                    session_id=data["session_id"],
                    linked_session_id=data["linked_session_id"],
                    task_id=data["task_id"],
                    link_type=data["link_type"],
                    metadata=_json_loads(data.get("metadata"), {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
                for data in (dict(zip(cols, row)) for row in rows)
            ]

    # --- Events ---

    async def save_event(self, event: OPCEvent) -> None:
        assert self._db
        await self._db.execute(
            "INSERT INTO events (event_id, event_type, payload, timestamp) VALUES (?, ?, ?, ?)",
            (event.event_id, event.event_type, _json_dumps(event.payload), event.timestamp.isoformat()),
        )
        await self._db.commit()

    async def get_events(self, event_type: str | None = None, limit: int = 50) -> list[dict]:
        assert self._db
        query = "SELECT * FROM events"
        params: list[Any] = []
        if event_type:
            query += " WHERE event_type = ?"
            params.append(event_type)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in rows]

    # --- Costs ---

    async def record_cost(
        self,
        task_id: str | None,
        agent_id: str | None,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost: float,
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT INTO cost_records (task_id, agent_id, model, tokens_in, tokens_out, cost, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, agent_id, model, tokens_in, tokens_out, cost, datetime.now().isoformat()),
        )
        await self._db.commit()

    async def get_total_cost(self, project_id: str | None = None) -> dict:
        assert self._db
        if project_id:
            query = """SELECT SUM(tokens_in), SUM(tokens_out), SUM(cost), COUNT(*)
                      FROM cost_records cr JOIN tasks t ON cr.task_id = t.id
                      WHERE t.project_id = ?"""
            params: tuple[Any, ...] = (project_id,)
        else:
            query = "SELECT SUM(tokens_in), SUM(tokens_out), SUM(cost), COUNT(*) FROM cost_records"
            params = ()
        async with self._db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return {
                "total_tokens_in": row[0] or 0,
                "total_tokens_out": row[1] or 0,
                "total_cost": row[2] or 0.0,
                "total_calls": row[3] or 0,
            }

    # --- Approvals and autonomy ---

    async def record_approval(
        self,
        decision: ApprovalDecision,
        task_id: str | None,
        project_id: str,
        action_kind: str,
        action_name: str,
        target_agent: str = "",
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT INTO approval_records
            (task_id, project_id, action_kind, action_name, target_agent, decision_action,
             risk_level, confidence, rationale, policy_source, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                project_id or "default",
                action_kind,
                action_name,
                target_agent,
                decision.action.value,
                decision.risk_level.value,
                decision.confidence,
                decision.rationale,
                decision.policy_source,
                _json_dumps(decision.metadata),
                datetime.now().isoformat(),
            ),
        )
        await self._db.commit()

    async def get_recent_approvals(
        self,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        assert self._db
        query = "SELECT * FROM approval_records"
        params: list[Any] = []
        if project_id:
            query += " WHERE project_id = ?"
            params.append(project_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in rows]

    async def get_autonomy_stats(self, project_id: str | None = None) -> dict[str, Any]:
        assert self._db
        query = "SELECT decision_action, COUNT(*) FROM approval_records"
        params: list[Any] = []
        if project_id:
            query += " WHERE project_id = ?"
            params.append(project_id)
        query += " GROUP BY decision_action"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        counts = {row[0]: row[1] for row in rows}
        total = sum(counts.values())
        auto = counts.get("auto_approve", 0)
        escalate = counts.get("escalate", 0)
        reject = counts.get("reject", 0)
        return {
            "total": total,
            "auto_approved": auto,
            "escalated": escalate,
            "rejected": reject,
            "auto_approval_rate": (auto / total) if total else 0.0,
        }

    # --- External sessions ---

    async def save_external_session(self, session: ExternalSession) -> None:
        assert self._db
        self._assert_project_write_scope(
            getattr(session, "project_id", None),
            operation="save_external_session",
            entity=f"external session task={getattr(session, 'task_id', '')!r}",
        )
        session_key = self._external_session_key(session)
        await self._db.execute(
            """INSERT OR REPLACE INTO external_sessions
            (session_key, agent_type, project_id, session_id, opc_session_id, task_id, workspace_path, run_mode, status, metadata, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_key,
                session.agent_type,
                session.project_id,
                session.session_id,
                session.opc_session_id,
                session.task_id,
                session.workspace_path,
                session.run_mode,
                session.status,
                _json_dumps(session.metadata),
                session.updated_at.isoformat(),
            ),
        )
        await self._close_replaced_external_session_rows(session, session_key=session_key)
        await self._db.commit()

    async def _close_replaced_external_session_rows(
        self,
        session: ExternalSession,
        *,
        session_key: str,
    ) -> None:
        """Close stale placeholder rows once the real provider session finishes."""
        assert self._db
        status = str(session.status or "").strip().lower()
        if status not in {
            "done",
            "completed",
            "complete",
            "finished",
            "failed",
            "cancelled",
            "canceled",
            "suspended",
            "hard_timeout",
            "idle_timeout",
            "startup_timeout",
            "denied",
            "rejected",
        }:
            return
        task_id = str(session.task_id or "").strip()
        if not task_id:
            return
        await self._db.execute(
            """
            UPDATE external_sessions
            SET status = ?, updated_at = ?
            WHERE project_id = ?
              AND agent_type = ?
              AND task_id = ?
              AND COALESCE(opc_session_id, '') = ?
              AND session_key != ?
              AND status IN ('starting', 'running', 'working')
            """,
            (
                session.status,
                session.updated_at.isoformat(),
                session.project_id,
                session.agent_type,
                task_id,
                str(session.opc_session_id or "").strip(),
                session_key,
            ),
        )

    async def get_external_session(
        self,
        agent_type: str,
        project_id: str = "default",
        *,
        opc_session_id: str | None = None,
        task_id: str | None = None,
    ) -> ExternalSession | None:
        assert self._db
        query = "SELECT * FROM external_sessions WHERE agent_type = ? AND project_id = ?"
        params: list[Any] = [agent_type, project_id]
        if opc_session_id:
            query += " AND opc_session_id = ?"
            params.append(opc_session_id)
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        query += " ORDER BY updated_at DESC LIMIT 1"
        async with self._db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            data = dict(zip(cols, row))
            return ExternalSession(
                agent_type=data["agent_type"],
                project_id=data["project_id"],
                session_id=data["session_id"],
                opc_session_id=data.get("opc_session_id"),
                task_id=data["task_id"],
                workspace_path=data["workspace_path"],
                run_mode=data["run_mode"],
                status=data["status"],
                metadata=_json_loads(data["metadata"], {}),
                updated_at=datetime.fromisoformat(data["updated_at"]),
            )

    async def get_latest_external_session_for_task(
        self,
        project_id: str,
        task_id: str,
    ) -> ExternalSession | None:
        assert self._db
        async with self._db.execute(
            """
            SELECT * FROM external_sessions
            WHERE project_id = ? AND task_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (project_id, task_id),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            data = dict(zip(cols, row))
            return ExternalSession(
                agent_type=data["agent_type"],
                project_id=data["project_id"],
                session_id=data["session_id"],
                opc_session_id=data.get("opc_session_id"),
                task_id=data["task_id"],
                workspace_path=data["workspace_path"],
                run_mode=data["run_mode"],
                status=data["status"],
                metadata=_json_loads(data["metadata"], {}),
                updated_at=datetime.fromisoformat(data["updated_at"]),
            )

    async def list_external_sessions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        task_id: str | None = None,
        opc_session_id: str | None = None,
        limit: int = 50,
    ) -> list[ExternalSession]:
        assert self._db
        query = "SELECT * FROM external_sessions WHERE 1=1"
        params: list[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if opc_session_id:
            query += " AND opc_session_id = ?"
            params.append(opc_session_id)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit or 50)))
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        return [
            ExternalSession(
                agent_type=data["agent_type"],
                project_id=data["project_id"],
                session_id=data["session_id"],
                opc_session_id=data.get("opc_session_id"),
                task_id=data["task_id"],
                workspace_path=data["workspace_path"],
                run_mode=data["run_mode"],
                status=data["status"],
                metadata=_json_loads(data["metadata"], {}),
                updated_at=datetime.fromisoformat(data["updated_at"]),
            )
            for data in (dict(zip(cols, row)) for row in rows)
        ]

    def _external_session_key(self, session: ExternalSession) -> str:
        return "|".join(
            [
                session.agent_type,
                session.project_id or "default",
                session.opc_session_id or "",
                session.task_id or "",
                session.session_id,
            ]
        )

    # --- Execution checkpoints ---

    async def save_execution_checkpoint(self, checkpoint: ExecutionCheckpoint) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO execution_checkpoints
            (checkpoint_id, project_id, session_id, checkpoint_type, status, task_id, payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                checkpoint.checkpoint_id,
                checkpoint.project_id,
                checkpoint.session_id,
                checkpoint.checkpoint_type,
                checkpoint.status,
                checkpoint.task_id,
                _json_dumps(checkpoint.payload),
                checkpoint.created_at.isoformat(),
                checkpoint.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_or_create_active_execution_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        checkpoint_types: list[str] | tuple[str, ...] | set[str],
        create_if_missing: bool = True,
    ) -> tuple[ExecutionCheckpoint | None, bool]:
        """Atomically reuse or create one active checkpoint for a session scope.

        ``BEGIN IMMEDIATE`` serializes checkpoint creation across independent
        controller/store connections.  It also repairs historical duplicate
        active rows in the same transaction, so concurrent startup/shutdown
        reconcilers can never supersede each other's newly-created checkpoint
        and leave the scope without a durable recovery point.
        """

        assert self._db
        clean_types = sorted(
            {
                str(item).strip()
                for item in checkpoint_types
                if str(item).strip()
            }
        )
        project_id = str(checkpoint.project_id or "default").strip() or "default"
        session_id = str(checkpoint.session_id or "").strip()
        checkpoint_type = str(checkpoint.checkpoint_type or "").strip()
        if not session_id:
            raise ValueError("active execution checkpoint requires session_id")
        if checkpoint_type not in clean_types:
            raise ValueError(
                "checkpoint_type must be included in the active checkpoint scope"
            )

        placeholders = ", ".join("?" for _ in clean_types)
        try:
            await self._db.execute("BEGIN IMMEDIATE")
            async with self._db.execute(
                f"""SELECT * FROM execution_checkpoints
                WHERE project_id = ?
                  AND session_id = ?
                  AND checkpoint_type IN ({placeholders})
                  AND status IN ('pending', 'resuming')
                ORDER BY updated_at DESC, created_at DESC, checkpoint_id DESC""",
                (project_id, session_id, *clean_types),
            ) as cursor:
                rows = await cursor.fetchall()
                cols = [description[0] for description in cursor.description]

            if rows:
                decoded = [dict(zip(cols, row)) for row in rows]
                winner_data = decoded[0]
                winner_id = str(winner_data["checkpoint_id"])
                now = datetime.now().isoformat()
                for duplicate in decoded[1:]:
                    duplicate_id = str(duplicate.get("checkpoint_id", "") or "").strip()
                    if not duplicate_id:
                        continue
                    payload = _json_loads(duplicate.get("payload"), {})
                    payload["superseded_at"] = now
                    payload["superseded_by_checkpoint_id"] = winner_id
                    await self._db.execute(
                        """UPDATE execution_checkpoints
                        SET status = 'superseded', payload = ?, updated_at = ?
                        WHERE checkpoint_id = ? AND status IN ('pending', 'resuming')""",
                        (_json_dumps(payload), now, duplicate_id),
                    )
                await self._db.commit()
                return (
                    ExecutionCheckpoint(
                        checkpoint_id=winner_id,
                        project_id=str(winner_data["project_id"]),
                        session_id=winner_data.get("session_id"),
                        checkpoint_type=str(winner_data["checkpoint_type"]),
                        status=str(winner_data["status"]),
                        task_id=winner_data.get("task_id"),
                        payload=_json_loads(winner_data.get("payload"), {}),
                        created_at=datetime.fromisoformat(str(winner_data["created_at"])),
                        updated_at=datetime.fromisoformat(str(winner_data["updated_at"])),
                    ),
                    False,
                )

            if not create_if_missing:
                await self._db.commit()
                return None, False

            await self._db.execute(
                """INSERT INTO execution_checkpoints
                (checkpoint_id, project_id, session_id, checkpoint_type, status,
                 task_id, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint.checkpoint_id,
                    project_id,
                    session_id,
                    checkpoint_type,
                    checkpoint.status,
                    checkpoint.task_id,
                    _json_dumps(checkpoint.payload),
                    checkpoint.created_at.isoformat(),
                    checkpoint.updated_at.isoformat(),
                ),
            )
            await self._db.commit()
            return checkpoint, True
        except Exception:
            await self._db.rollback()
            raise

    async def normalize_active_execution_checkpoints(
        self,
        *,
        project_id: str,
        session_id: str,
        checkpoint_types: list[str] | tuple[str, ...] | set[str],
    ) -> ExecutionCheckpoint | None:
        """Return one active scope owner while superseding duplicate rows.

        Unlike creation, startup reconciliation must not manufacture a
        checkpoint merely because none exists.  This wrapper reuses the same
        serialized transaction and duplicate winner rule with insertion
        explicitly disabled.
        """

        clean_types = sorted(
            {
                str(item).strip()
                for item in checkpoint_types
                if str(item).strip()
            }
        )
        if not clean_types or not str(session_id or "").strip():
            return None
        candidate = ExecutionCheckpoint(
            project_id=str(project_id or "default").strip() or "default",
            session_id=str(session_id or "").strip(),
            checkpoint_type=clean_types[0],
        )
        checkpoint, _created = await self.get_or_create_active_execution_checkpoint(
            candidate,
            checkpoint_types=clean_types,
            create_if_missing=False,
        )
        return checkpoint

    async def compare_and_set_execution_checkpoint(
        self,
        checkpoint_id: str,
        *,
        expected_statuses: list[str] | tuple[str, ...] | set[str],
        status: str,
        payload: dict[str, Any],
        updated_at: datetime | None = None,
    ) -> bool:
        """Atomically transition a checkpoint only from an expected state.

        The conditional UPDATE is the cross-controller guard for checkpoint
        consumption.  In-memory scope locks serialize one Office controller;
        this CAS prevents a standalone CLI/controller from claiming the same
        pending runtime concurrently.
        """

        assert self._db
        expected = [
            str(item).strip()
            for item in expected_statuses
            if str(item).strip()
        ]
        if not checkpoint_id or not expected:
            return False
        placeholders = ", ".join("?" for _ in expected)
        cursor = await self._db.execute(
            f"""UPDATE execution_checkpoints
            SET status = ?, payload = ?, updated_at = ?
            WHERE checkpoint_id = ? AND status IN ({placeholders})""",
            (
                str(status or "").strip(),
                _json_dumps(dict(payload or {})),
                (updated_at or datetime.now()).isoformat(),
                checkpoint_id,
                *expected,
            ),
        )
        await self._db.commit()
        return cursor.rowcount == 1

    async def complete_execution_checkpoint_and_reopen_ui_anchor(
        self,
        checkpoint_id: str,
        *,
        project_id: str,
        session_id: str,
        expected_status: str,
        status: str,
        payload: dict[str, Any],
        ui_anchor_task_id: str = "",
        updated_at: datetime | None = None,
    ) -> bool:
        """Atomically complete a runtime handoff and reopen its UI anchor.

        The anchor must never become chat-runnable if a concurrent Stop already
        took checkpoint ownership back.  Keeping both writes in one SQLite
        transaction makes the checkpoint CAS the gate for the UI projection.
        """

        assert self._db
        checkpoint_id = str(checkpoint_id or "").strip()
        project_id = str(project_id or "default").strip() or "default"
        session_id = str(session_id or "").strip()
        expected_status = str(expected_status or "").strip()
        status = str(status or "").strip()
        ui_anchor_task_id = str(ui_anchor_task_id or "").strip()
        if not checkpoint_id or not session_id or not expected_status or not status:
            return False
        now = updated_at or datetime.now()
        try:
            await self._db.execute("BEGIN IMMEDIATE")
            cursor = await self._db.execute(
                """UPDATE execution_checkpoints
                SET status = ?, payload = ?, updated_at = ?
                WHERE checkpoint_id = ?
                  AND project_id = ?
                  AND session_id = ?
                  AND status = ?""",
                (
                    status,
                    _json_dumps(dict(payload or {})),
                    now.isoformat(),
                    checkpoint_id,
                    project_id,
                    session_id,
                    expected_status,
                ),
            )
            if cursor.rowcount != 1:
                await self._db.rollback()
                return False
            if ui_anchor_task_id:
                await self._db.execute(
                    """UPDATE tasks
                    SET status = ?, execution_lock = 0, execution_locked_at = NULL
                    WHERE id = ? AND project_id = ? AND status = ?""",
                    (
                        TaskStatus.IDLE.value,
                        ui_anchor_task_id,
                        project_id,
                        TaskStatus.CANCELLED.value,
                    ),
                )
            await self._db.commit()
            return True
        except Exception:
            await self._db.rollback()
            raise

    async def get_execution_checkpoints(
        self,
        project_id: str = "default",
        session_id: str | None = None,
        checkpoint_types: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> list[ExecutionCheckpoint]:
        assert self._db
        query = "SELECT * FROM execution_checkpoints WHERE project_id = ?"
        params: list[Any] = [project_id]
        clean_statuses = [str(status).strip() for status in list(statuses or []) if str(status).strip()]
        if clean_statuses:
            placeholders = ", ".join("?" for _ in clean_statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(clean_statuses)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if checkpoint_types:
            placeholders = ", ".join("?" for _ in checkpoint_types)
            query += f" AND checkpoint_type IN ({placeholders})"
            params.extend(checkpoint_types)
        query += " ORDER BY updated_at DESC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                ExecutionCheckpoint(
                    checkpoint_id=data["checkpoint_id"],
                    project_id=data["project_id"],
                    session_id=data.get("session_id"),
                    checkpoint_type=data["checkpoint_type"],
                    status=data["status"],
                    task_id=data["task_id"],
                    payload=_json_loads(data["payload"], {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                    updated_at=datetime.fromisoformat(data["updated_at"]),
                )
                for data in (dict(zip(cols, row)) for row in rows)
            ]

    async def get_pending_checkpoints(
        self,
        project_id: str = "default",
        session_id: str | None = None,
        checkpoint_types: list[str] | None = None,
    ) -> list[ExecutionCheckpoint]:
        return await self.get_execution_checkpoints(
            project_id=project_id,
            session_id=session_id,
            checkpoint_types=checkpoint_types,
            statuses=["pending"],
        )

    async def get_latest_pending_checkpoint(
        self,
        project_id: str = "default",
        session_id: str | None = None,
    ) -> ExecutionCheckpoint | None:
        checkpoints = await self.get_pending_checkpoints(project_id=project_id, session_id=session_id)
        return checkpoints[0] if checkpoints else None

    async def resolve_execution_checkpoint(self, checkpoint_id: str, status: str = "resolved") -> None:
        assert self._db
        await self._db.execute(
            "UPDATE execution_checkpoints SET status = ?, updated_at = ? WHERE checkpoint_id = ?",
            (status, datetime.now().isoformat(), checkpoint_id),
        )
        await self._db.commit()

    async def supersede_pending_checkpoints(
        self,
        *,
        project_id: str = "default",
        task_id: str | None = None,
        session_id: str | None = None,
        checkpoint_types: list[str] | None = None,
        basis_hash: str | None = None,
        exclude_checkpoint_id: str | None = None,
    ) -> list[str]:
        assert self._db
        query = "SELECT * FROM execution_checkpoints WHERE project_id = ? AND status = 'pending'"
        params: list[Any] = [project_id]
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if checkpoint_types:
            placeholders = ", ".join("?" for _ in checkpoint_types)
            query += f" AND checkpoint_type IN ({placeholders})"
            params.extend(checkpoint_types)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]

        superseded_ids: list[str] = []
        now = datetime.now().isoformat()
        for data in (dict(zip(cols, row)) for row in rows):
            checkpoint_id = str(data.get("checkpoint_id", "") or "").strip()
            if not checkpoint_id or checkpoint_id == str(exclude_checkpoint_id or "").strip():
                continue
            payload = _json_loads(data.get("payload"), {})
            existing_basis_hash = str(payload.get("basis_hash", "") or "").strip()
            if basis_hash and existing_basis_hash and existing_basis_hash == basis_hash:
                continue
            payload["superseded_at"] = now
            if exclude_checkpoint_id:
                payload["superseded_by_checkpoint_id"] = str(exclude_checkpoint_id)
            await self._db.execute(
                "UPDATE execution_checkpoints SET status = ?, payload = ?, updated_at = ? WHERE checkpoint_id = ?",
                ("superseded", _json_dumps(payload), now, checkpoint_id),
            )
            superseded_ids.append(checkpoint_id)
        if superseded_ids:
            await self._db.commit()
        return superseded_ids

    # --- Runtime V2 persistence ---

    async def save_runtime_session(
        self,
        *,
        runtime_session_id: str,
        project_id: str = "default",
        session_id: str | None = None,
        task_id: str | None = None,
        status: str = "running",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        self._assert_project_write_scope(
            project_id,
            operation="save_runtime_session",
            entity=f"runtime session {runtime_session_id!r}",
        )
        now = datetime.now().isoformat()
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_sessions
            (runtime_session_id, project_id, session_id, task_id, status, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM runtime_sessions WHERE runtime_session_id = ?), ?), ?)""",
            (
                runtime_session_id,
                project_id,
                session_id,
                task_id,
                status,
                _json_dumps(metadata or {}),
                runtime_session_id,
                now,
                now,
            ),
        )
        await self._db.commit()

    async def get_runtime_session(self, runtime_session_id: str) -> dict[str, Any] | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM runtime_sessions WHERE runtime_session_id = ?",
            (runtime_session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cursor.description]
        data = dict(zip(cols, row))
        data["metadata"] = _json_loads(data.get("metadata"), {})
        return data

    async def list_runtime_sessions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        assert self._db
        query = "SELECT * FROM runtime_sessions WHERE 1=1"
        params: list[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit or 50)))
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        sessions: list[dict[str, Any]] = []
        for row in rows:
            data = dict(zip(cols, row))
            data["metadata"] = _json_loads(data.get("metadata"), {})
            sessions.append(data)
        return sessions

    async def save_runtime_event(
        self,
        runtime_session_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT INTO runtime_events
            (event_id, runtime_session_id, event_type, payload, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                runtime_session_id,
                event_type,
                _json_dumps(payload or {}),
                datetime.now().isoformat(),
            ),
        )
        await self._db.commit()

    async def list_runtime_events(
        self,
        runtime_session_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        assert self._db
        async with self._db.execute(
            """
            SELECT * FROM (
                SELECT * FROM runtime_events
                WHERE runtime_session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            )
            ORDER BY created_at ASC
            """,
            (runtime_session_id, max(1, int(limit or 100))),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        results: list[dict[str, Any]] = []
        for row in rows:
            data = dict(zip(cols, row))
            data["payload"] = _json_loads(data.get("payload"), {})
            results.append(data)
        return results

    async def save_runtime_transcript_entry(
        self,
        *,
        runtime_session_id: str,
        task_id: str | None = None,
        session_id: str | None = None,
        message_id: str = "",
        role: str = "assistant",
        entry_type: str = "message",
        content: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_transcript_entries
            (entry_id, runtime_session_id, task_id, session_id, message_id, role, entry_type, content, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                runtime_session_id,
                task_id,
                session_id,
                message_id,
                role,
                entry_type,
                content,
                _json_dumps(metadata or {}),
                datetime.now().isoformat(),
            ),
        )
        await self._db.commit()

    async def save_runtime_tool_call(
        self,
        *,
        runtime_session_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        message_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_tool_calls
            (call_record_id, runtime_session_id, task_id, session_id, message_id, tool_call_id, tool_name, arguments, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"{runtime_session_id}|{tool_call_id or uuid.uuid4().hex}",
                runtime_session_id,
                task_id,
                session_id,
                message_id,
                tool_call_id,
                tool_name,
                _json_dumps(arguments or {}),
                _json_dumps(metadata or {}),
                datetime.now().isoformat(),
            ),
        )
        await self._db.commit()

    async def save_runtime_tool_result(
        self,
        *,
        runtime_session_id: str,
        tool_name: str,
        payload: dict[str, Any] | None = None,
        tool_call_id: str = "",
        task_id: str | None = None,
        session_id: str | None = None,
        message_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_tool_results
            (result_record_id, runtime_session_id, task_id, session_id, message_id, tool_call_id, tool_name, payload, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"{runtime_session_id}|{tool_call_id or tool_name}|{uuid.uuid4().hex}",
                runtime_session_id,
                task_id,
                session_id,
                message_id,
                tool_call_id,
                tool_name,
                _json_dumps(payload or {}),
                _json_dumps(metadata or {}),
                datetime.now().isoformat(),
            ),
        )
        await self._db.commit()

    async def list_runtime_transcript_entries(
        self,
        runtime_session_id: str,
    ) -> list[dict[str, Any]]:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM runtime_transcript_entries WHERE runtime_session_id = ? ORDER BY created_at ASC",
            (runtime_session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        return [
            {**dict(zip(cols, row)), "metadata": _json_loads(dict(zip(cols, row)).get("metadata"), {})}
            for row in rows
        ]

    async def list_runtime_tool_calls(
        self,
        runtime_session_id: str,
    ) -> list[dict[str, Any]]:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM runtime_tool_calls WHERE runtime_session_id = ? ORDER BY created_at ASC",
            (runtime_session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        results: list[dict[str, Any]] = []
        for row in rows:
            data = dict(zip(cols, row))
            data["arguments"] = _json_loads(data.get("arguments"), {})
            data["metadata"] = _json_loads(data.get("metadata"), {})
            results.append(data)
        return results

    async def list_runtime_tool_results(
        self,
        runtime_session_id: str,
    ) -> list[dict[str, Any]]:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM runtime_tool_results WHERE runtime_session_id = ? ORDER BY created_at ASC",
            (runtime_session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        results: list[dict[str, Any]] = []
        for row in rows:
            data = dict(zip(cols, row))
            data["payload"] = _json_loads(data.get("payload"), {})
            data["metadata"] = _json_loads(data.get("metadata"), {})
            results.append(data)
        return results

    async def save_runtime_permission_grant(
        self,
        *,
        runtime_session_id: str,
        project_id: str = "default",
        scope: str,
        tool_name: str,
        candidate: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_permission_grants
            (grant_id, runtime_session_id, project_id, scope, tool_name, candidate, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"{runtime_session_id}|{scope}|{tool_name}|{candidate}",
                runtime_session_id,
                project_id,
                scope,
                tool_name,
                candidate,
                _json_dumps(metadata or {}),
                datetime.now().isoformat(),
            ),
        )
        await self._db.commit()

    async def list_runtime_permission_grants(
        self,
        *,
        runtime_session_id: str | None = None,
        project_id: str | None = None,
        scopes: list[str] | None = None,
        tool_name: str | None = None,
    ) -> list[dict[str, Any]]:
        assert self._db
        query = "SELECT * FROM runtime_permission_grants WHERE 1=1"
        params: list[Any] = []
        if runtime_session_id:
            query += " AND runtime_session_id = ?"
            params.append(runtime_session_id)
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if scopes:
            placeholders = ",".join("?" for _ in scopes)
            query += f" AND scope IN ({placeholders})"
            params.extend(scopes)
        if tool_name:
            query += " AND tool_name = ?"
            params.append(tool_name)
        query += " ORDER BY created_at ASC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        results: list[dict[str, Any]] = []
        for row in rows:
            data = dict(zip(cols, row))
            data["metadata"] = _json_loads(data.get("metadata"), {})
            results.append(data)
        return results

    async def save_runtime_subagent_run(
        self,
        *,
        subagent_run_id: str,
        runtime_session_id: str,
        agent_id: str,
        profile: str,
        status: str,
        task_id: str | None = None,
        worktree_path: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        now = datetime.now().isoformat()
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_subagent_runs
            (subagent_run_id, runtime_session_id, task_id, agent_id, profile, status, worktree_path, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM runtime_subagent_runs WHERE subagent_run_id = ?), ?), ?)""",
            (
                subagent_run_id,
                runtime_session_id,
                task_id,
                agent_id,
                profile,
                status,
                worktree_path,
                _json_dumps(metadata or {}),
                subagent_run_id,
                now,
                now,
            ),
        )
        await self._db.commit()

    async def save_runtime_worktree_session(
        self,
        *,
        worktree_session_id: str,
        runtime_session_id: str,
        path: str,
        status: str,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        now = datetime.now().isoformat()
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_worktree_sessions
            (worktree_session_id, runtime_session_id, task_id, path, status, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM runtime_worktree_sessions WHERE worktree_session_id = ?), ?), ?)""",
            (
                worktree_session_id,
                runtime_session_id,
                task_id,
                path,
                status,
                _json_dumps(metadata or {}),
                worktree_session_id,
                now,
                now,
            ),
        )
        await self._db.commit()

    async def save_runtime_compaction_boundary(
        self,
        *,
        boundary_id: str,
        runtime_session_id: str,
        summary: str,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_compaction_boundaries
            (boundary_id, runtime_session_id, task_id, summary, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                boundary_id,
                runtime_session_id,
                task_id,
                summary,
                _json_dumps(metadata or {}),
                datetime.now().isoformat(),
            ),
        )
        await self._db.commit()

    # --- Organizations ---

    async def save_organization(self, org: Organization) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO organizations
            (org_id, name, description, status, company_profile,
             budget_monthly_cents, spent_monthly_cents, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                org.org_id,
                org.name,
                org.description,
                org.status,
                org.company_profile,
                org.budget_monthly_cents,
                org.spent_monthly_cents,
                _json_dumps(org.metadata),
                org.created_at.isoformat(),
                org.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_organization(self, org_id: str) -> Organization | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM organizations WHERE org_id = ?", (org_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            data = dict(zip(cols, row))
            return Organization(
                org_id=data["org_id"],
                name=data["name"],
                description=data.get("description") or "",
                status=data.get("status") or "active",
                company_profile=data.get("company_profile") or "corporate",
                budget_monthly_cents=int(data.get("budget_monthly_cents") or 0),
                spent_monthly_cents=int(data.get("spent_monthly_cents") or 0),
                metadata=_json_loads(data.get("metadata"), {}),
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
            )

    async def list_organizations(self, status: str | None = None) -> list[Organization]:
        assert self._db
        query = "SELECT * FROM organizations"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            results: list[Organization] = []
            for row in rows:
                data = dict(zip(cols, row))
                results.append(Organization(
                    org_id=data["org_id"],
                    name=data["name"],
                    description=data.get("description") or "",
                    status=data.get("status") or "active",
                    company_profile=data.get("company_profile") or "corporate",
                    budget_monthly_cents=int(data.get("budget_monthly_cents") or 0),
                    spent_monthly_cents=int(data.get("spent_monthly_cents") or 0),
                    metadata=_json_loads(data.get("metadata"), {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                    updated_at=datetime.fromisoformat(data["updated_at"]),
                ))
            return results

    async def update_organization(self, org_id: str, **kwargs: Any) -> None:
        assert self._db
        allowed = {"name", "description", "status", "company_profile",
                    "budget_monthly_cents", "spent_monthly_cents", "metadata"}
        sets: list[str] = []
        params: list[Any] = []
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            if key == "metadata":
                value = _json_dumps(value)
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(org_id)
        await self._db.execute(
            f"UPDATE organizations SET {', '.join(sets)} WHERE org_id = ?", params
        )
        await self._db.commit()

    # --- Goals ---

    async def save_goal(self, goal: Goal) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO goals
            (goal_id, org_id, parent_id, owner_agent_id, level, title, description,
             status, priority, deadline, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                goal.goal_id,
                goal.org_id,
                goal.parent_id,
                goal.owner_agent_id,
                goal.level.value if isinstance(goal.level, GoalLevel) else goal.level,
                goal.title,
                goal.description,
                goal.status.value if isinstance(goal.status, GoalStatus) else goal.status,
                goal.priority,
                goal.deadline.isoformat() if goal.deadline else None,
                _json_dumps(goal.metadata),
                goal.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_goal(self, goal_id: str) -> Goal | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM goals WHERE goal_id = ?", (goal_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_goal(row, cursor.description)

    async def list_goals(
        self,
        org_id: str,
        status: str | None = None,
        parent_id: str | None = "__unset__",
    ) -> list[Goal]:
        assert self._db
        query = "SELECT * FROM goals WHERE org_id = ?"
        params: list[Any] = [org_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        if parent_id != "__unset__":
            if parent_id is None:
                query += " AND parent_id IS NULL"
            else:
                query += " AND parent_id = ?"
                params.append(parent_id)
        query += " ORDER BY priority ASC, created_at ASC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_goal(row, cursor.description) for row in rows]

    async def get_goal_tree(self, org_id: str) -> list[Goal]:
        return await self.list_goals(org_id, parent_id="__unset__")

    def _row_to_goal(self, row: Any, description: Any) -> Goal:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return Goal(
            goal_id=data["goal_id"],
            org_id=data["org_id"],
            parent_id=data.get("parent_id"),
            owner_agent_id=data.get("owner_agent_id"),
            level=GoalLevel(data.get("level") or "task"),
            title=data["title"],
            description=data.get("description") or "",
            status=GoalStatus(data.get("status") or "active"),
            priority=int(data.get("priority") or 5),
            deadline=datetime.fromisoformat(data["deadline"]) if data.get("deadline") else None,
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    # --- Org Agents ---

    async def save_org_agent(self, agent: OrgAgent) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO org_agents
            (agent_id, org_id, role_id, name, reports_to,
             budget_monthly_cents, spent_monthly_cents,
             heartbeat_enabled, heartbeat_interval_sec, last_heartbeat_at,
             status, capabilities, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent.agent_id,
                agent.org_id,
                agent.role_id,
                agent.name,
                agent.reports_to,
                agent.budget_monthly_cents,
                agent.spent_monthly_cents,
                int(agent.heartbeat_enabled),
                agent.heartbeat_interval_sec,
                agent.last_heartbeat_at.isoformat() if agent.last_heartbeat_at else None,
                agent.status,
                agent.capabilities,
                _json_dumps(agent.metadata),
                agent.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_org_agent(self, agent_id: str) -> OrgAgent | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM org_agents WHERE agent_id = ?", (agent_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_org_agent(row, cursor.description)

    async def list_org_agents(
        self,
        org_id: str,
        status: str | None = None,
    ) -> list[OrgAgent]:
        assert self._db
        query = "SELECT * FROM org_agents WHERE org_id = ?"
        params: list[Any] = [org_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_org_agent(row, cursor.description) for row in rows]

    def _row_to_org_agent(self, row: Any, description: Any) -> OrgAgent:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return OrgAgent(
            agent_id=data["agent_id"],
            org_id=data["org_id"],
            role_id=data["role_id"],
            name=data.get("name") or "",
            reports_to=data.get("reports_to"),
            budget_monthly_cents=int(data.get("budget_monthly_cents") or 0),
            spent_monthly_cents=int(data.get("spent_monthly_cents") or 0),
            heartbeat_enabled=bool(data.get("heartbeat_enabled", 0)),
            heartbeat_interval_sec=int(data.get("heartbeat_interval_sec") or 300),
            last_heartbeat_at=(
                datetime.fromisoformat(data["last_heartbeat_at"])
                if data.get("last_heartbeat_at")
                else None
            ),
            status=data.get("status") or "idle",
            capabilities=data.get("capabilities") or "",
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    # --- Atomic task checkout / release ---

    async def checkout_task(self, task_id: str, agent_id: str, run_id: str | None = None) -> bool:
        """Atomically claim a task for execution. Returns True on success."""
        assert self._db
        import uuid as _uuid
        run_id = run_id or str(_uuid.uuid4())
        now = datetime.now().isoformat()
        async with self._db.execute(
            """UPDATE tasks
               SET status = 'running',
                   assigned_to = ?,
                   checkout_run_id = ?,
                   execution_locked_at = ?
               WHERE id = ?
                 AND status IN ('pending', 'todo')
                 AND (assigned_to IS NULL OR assigned_to = '' OR assigned_to = ?)""",
            (agent_id, run_id, now, task_id, agent_id),
        ) as cursor:
            await self._db.commit()
            return cursor.rowcount > 0

    async def release_task(self, task_id: str, agent_id: str) -> bool:
        """Release a checked-out task back to pending."""
        assert self._db
        async with self._db.execute(
            """UPDATE tasks
               SET status = 'pending',
                   checkout_run_id = NULL,
                   execution_locked_at = NULL
               WHERE id = ?
                 AND assigned_to = ?
                 AND status = 'running'""",
            (task_id, agent_id),
        ) as cursor:
            await self._db.commit()
            return cursor.rowcount > 0

    # --- Cost events ---

    async def record_cost_event(self, event: CostEvent) -> None:
        assert self._db
        await self._db.execute(
            """INSERT INTO cost_events
            (event_id, org_id, agent_id, task_id, model, tokens_in, tokens_out, cost_usd, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.org_id,
                event.agent_id,
                event.task_id,
                event.model,
                event.tokens_in,
                event.tokens_out,
                event.cost_usd,
                event.timestamp.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_agent_spend(self, agent_id: str) -> dict[str, Any]:
        assert self._db
        async with self._db.execute(
            "SELECT SUM(tokens_in), SUM(tokens_out), SUM(cost_usd), COUNT(*) FROM cost_events WHERE agent_id = ?",
            (agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return {
                "tokens_in": row[0] or 0,
                "tokens_out": row[1] or 0,
                "cost_usd": row[2] or 0.0,
                "calls": row[3] or 0,
            }

    async def get_org_spend(self, org_id: str) -> dict[str, Any]:
        assert self._db
        async with self._db.execute(
            "SELECT SUM(tokens_in), SUM(tokens_out), SUM(cost_usd), COUNT(*) FROM cost_events WHERE org_id = ?",
            (org_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return {
                "tokens_in": row[0] or 0,
                "tokens_out": row[1] or 0,
                "cost_usd": row[2] or 0.0,
                "calls": row[3] or 0,
            }
