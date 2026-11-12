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

class AppendOnlyCase(unittest.TestCase):
    """History is append-only: one row per message, written once. Compaction
    moves the visible window (base_seq/n_msgs) instead of deleting, so the
    raw conversation survives every fold."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "sessions.db")
        self.store = sessions.Store(self.path)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _msgs(self, n, prefix="m"):
        return [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": "%s%d" % (prefix, i)} for i in range(n)]

    def _rows(self):
        return self.store.conn.execute(
            "SELECT COUNT(*) FROM session_messages").fetchone()[0]

    def _visible(self):
        return self.store.load_sessions()[0][3]

    def test_appending_writes_only_the_new_messages(self):
        self.store.save_session(1, 0, None, self._msgs(4))
        self.assertEqual(self._rows(), 4)
        self.store.save_session(1, 0, None, self._msgs(6))
        self.assertEqual(self._rows(), 6)          # not 10: nothing rewritten
        self.assertEqual([m["content"] for m in self._visible()],
                         ["m0", "m1", "m2", "m3", "m4", "m5"])

    def test_shrinking_keeps_the_raw_conversation(self):
        """Compaction is a window move, not a delete."""
        self.store.save_session(1, 0, None, self._msgs(6))
        self.store.save_session(1, 0, None, self._msgs(2, "new"))
        self.assertEqual([m["content"] for m in self._visible()], ["new0", "new1"])
        self.assertEqual(self.store.archive_count(1, 0), 6)
        self.assertEqual(len(self.store.load_archived(1, 0)), 6)
        self.assertEqual(self.store.load_archived(1, 0)[0]["content"], "m0")

    def test_same_length_rewrite_starts_a_new_generation(self):
        self.store.save_session(1, 0, None, self._msgs(3))
        changed = self._msgs(3)
        changed[-1] = {"role": "assistant", "content": "rewritten"}
        self.store.save_session(1, 0, None, changed)
        self.assertEqual(self._visible()[-1]["content"], "rewritten")
        self.assertEqual(self.store.archive_count(1, 0), 3)

    def test_delete_takes_the_archive_with_it(self):
        self.store.save_session(1, 0, None, self._msgs(6))
        self.store.save_session(1, 0, None, self._msgs(2, "new"))
        self.store.delete_session(1, 0)
        self.assertEqual(self._rows(), 0)
        self.assertEqual(self.store.load_archived(1, 0), [])

    def test_move_carries_the_message_rows(self):
        self.store.save_session(1, 5, None, self._msgs(3))
        self.store.move_session(1, 5, 7)
        self.assertEqual([m["content"] for m in self.store.load_sessions()[0][3]],
                         ["m0", "m1", "m2"])
        self.assertEqual(self.store.archive_count(1, 5), 0)

    def test_migrates_a_blob_database_once(self):
        """Databases written by the old INSERT OR REPLACE path convert on the
        first boot, and only the first: the second boot must not duplicate."""
        import json
        self.store.conn.execute(
            "INSERT INTO sessions (chat_id, thread_id, model, messages, updated_at)"
            " VALUES (9, 0, 'go/m', ?, 1700000000)",
            (json.dumps(self._msgs(3)),))
        self.store.conn.commit()
        self.store.close()

        self.store = sessions.Store(self.path)
        self.assertEqual([m["content"] for m in self._visible()],
                         ["m0", "m1", "m2"])
        self.assertEqual(self._rows(), 3)
        self.assertEqual(
            self.store.conn.execute(
                "SELECT messages FROM sessions WHERE chat_id=9").fetchone()[0], "")
        self.store.close()

        self.store = sessions.Store(self.path)   # idempotent
        self.assertEqual(self._rows(), 3)
        self.assertEqual(len(self._visible()), 3)
