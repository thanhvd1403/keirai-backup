"""Tests for the SQLite session store: schema, session-id backfill, topology.

The backfill runs at every startup, so it must not write when there is
nothing to fix - an unconditional UPDATE there cost a commit+fsync at every
boot (500ms of a 1.8s restart on this disk).
"""
import os
import tempfile
import unittest

import sessions


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "sessions.db")

    def tearDown(self):
        self.dir.cleanup()

    def test_creates_schema(self):
        store = sessions.Store(self.path)
        names = {r[0] for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"sessions", "topics", "session_meta"} <= names)

    def test_backfills_meta_for_preexisting_sessions(self):
        """A database written before session_meta existed still gets ids."""
        store = sessions.Store(self.path)
        store.conn.execute(
            "INSERT INTO sessions (chat_id, thread_id, model, messages, updated_at) "
            "VALUES (1, 0, 'go/m', '[]', 1700000000)")
        store.conn.execute("DELETE FROM session_meta")   # simulate the old schema
        store.conn.commit()
        store.conn.close()

        reopened = sessions.Store(self.path)
        sid = reopened.conn.execute(
            "SELECT session_id FROM session_meta WHERE chat_id=1 AND thread_id=0"
        ).fetchone()
        self.assertIsNotNone(sid)
        self.assertRegex(sid[0], r"^\d{8}-\d{4}-[0-9a-f]{4}$")

    def test_reopen_keeps_the_same_ids(self):
        """Steady state: nothing to insert, ids untouched."""
        store = sessions.Store(self.path)
        store.conn.execute(
            "INSERT INTO sessions (chat_id, thread_id, model, messages, updated_at) "
            "VALUES (2, 7, 'go/m', '[]', 1700000000)")
        store.conn.commit()
        store.conn.close()

        first_open = sessions.Store(self.path)   # backfills the new row
        first = first_open.conn.execute(
            "SELECT session_id FROM session_meta WHERE chat_id=2").fetchone()[0]
        first_open.conn.close()

        reopened = sessions.Store(self.path)     # must not rewrite it
        again = reopened.conn.execute(
            "SELECT session_id FROM session_meta WHERE chat_id=2").fetchone()[0]
        self.assertEqual(first, again)

    def test_reopening_does_not_modify_the_file(self):
        """No write transaction when there is nothing to backfill."""
        sessions.Store(self.path).conn.close()
        before = open(self.path, "rb").read()
        sessions.Store(self.path).conn.close()
        self.assertEqual(before, open(self.path, "rb").read())
