from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opc.core.models import SessionMessageRecord, SessionRecord
from opc.database.store import OPCStore


class TestSaveSessionMessageAtomicity(unittest.IsolatedAsyncioTestCase):
    """Regression test for a non-atomic multi-statement write.

    ``OPCStore.save_session_message`` performs two writes (INSERT into
    ``session_messages`` and UPDATE ``sessions``) on the Store's single
    shared, long-lived connection, but neither wraps them in an explicit
    ``BEGIN IMMEDIATE`` / ``try``/``except: rollback`` block. If the second
    statement raises, the first statement's effect is left pending
    (neither committed nor rolled back) on the shared connection. Because
    the connection is long-lived and reused by every other Store method,
    a later, entirely unrelated ``commit()`` call flushes the orphaned
    write to disk -- silently persisting data that the caller believed had
    failed atomically.
    """

    async def _count(self, store: OPCStore, table: str, where: str = "", params: tuple = ()) -> int:
        assert store._db is not None
        query = f"SELECT COUNT(*) FROM {table}"
        if where:
            query += f" WHERE {where}"
        async with store._db.execute(query, params) as cursor:
            row = await cursor.fetchone()
        return int(row[0] or 0)

    @staticmethod
    def _count_from_fresh_connection(db_path: Path, table: str, where: str, params: tuple) -> int:
        """Read via a brand-new connection, so we only see committed data."""
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params)
            row = cursor.fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()

    async def test_failed_second_write_is_not_silently_persisted_later(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tasks.db"
            store = OPCStore(db_path)
            await store.initialize()
            try:
                await store.save_session(
                    SessionRecord(
                        session_id="session-1",
                        project_id="default",
                        title="New Chat",
                    )
                )

                message = SessionMessageRecord(
                    message_id="msg-1",
                    session_id="session-1",
                    role="user",
                )

                real_execute = store._db.execute

                def flaky_execute(sql, params=()):
                    # Let every other statement (BEGIN IMMEDIATE, the INSERT
                    # into session_messages, ...) through untouched, then
                    # simulate a transient failure (e.g. SQLITE_BUSY /
                    # OperationalError) on the second write (UPDATE
                    # sessions), which is the realistic "interrupted between
                    # steps" scenario. Matching on SQL content (rather than
                    # call index) keeps this test valid whether or not the
                    # implementation wraps the writes in an explicit
                    # transaction statement.
                    if isinstance(sql, str) and "UPDATE sessions" in sql:
                        raise RuntimeError("simulated transient DB failure")
                    return real_execute(sql, params)

                with patch.object(store._db, "execute", side_effect=flaky_execute):
                    with self.assertRaises(RuntimeError):
                        await store.save_session_message(message)

                # The overall operation raised, so no committed data should
                # exist yet from ANY other connection's point of view.
                committed_before = self._count_from_fresh_connection(
                    db_path, "session_messages", "message_id = ?", ("msg-1",)
                )
                self.assertEqual(
                    committed_before,
                    0,
                    "the message row must not be durably visible before any "
                    "commit()",
                )

                # Now some *completely unrelated* write happens on the same
                # shared connection and commits, as would happen in normal
                # operation (e.g. another request touching a session).
                await store.touch_session("session-1")

                # BUG: the orphaned INSERT from the failed
                # save_session_message call gets swept into this unrelated
                # commit and silently persisted, even though the caller saw
                # an exception and the paired sessions.updated_at write
                # never happened.
                committed_after = self._count_from_fresh_connection(
                    db_path, "session_messages", "message_id = ?", ("msg-1",)
                )
                self.assertEqual(
                    committed_after,
                    0,
                    "a failed save_session_message must not leak its first "
                    "write into a later, unrelated commit",
                )
            finally:
                await store.close()


if __name__ == "__main__":
    unittest.main()
