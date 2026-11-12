"""SQLite session store - sessions, topics and model choice survive restarts.

Stdlib sqlite3. Write-through: main updates state then persists the row.
thread_id 0 is the sentinel for "main chat, no topic".
Thread-safety: session workers run concurrently, so the shared connection
is opened with check_same_thread=False and every access goes through one
re-entrant lock (SQLite stays happy, compound ops stay atomic).
"""
import json
import logging
import os
import secrets
import shutil
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

SCHEMA = """
-- A conversation is stored APPEND-ONLY in session_messages: one row per
-- message, written once, never rewritten. This table only says which slice
-- of those rows is visible right now:
--   visible = seq in [base_seq, base_seq + n_msgs)
-- Compaction lowers n_msgs (and moves base_seq forward) instead of deleting,
-- so the raw conversation survives every fold - the same property opencode
-- gets from its message table. `messages` is the pre-append-only blob and is
-- emptied by _migrate() on first boot; it stays in the schema so an old
-- database still opens.
CREATE TABLE IF NOT EXISTS sessions (
  chat_id INTEGER NOT NULL,
  thread_id INTEGER NOT NULL,
  model TEXT,
  messages TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL,
  base_seq INTEGER NOT NULL DEFAULT 1,
  n_msgs INTEGER NOT NULL DEFAULT 0,
  next_seq INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (chat_id, thread_id)
);
CREATE TABLE IF NOT EXISTS session_messages (
  chat_id INTEGER NOT NULL,
  thread_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (chat_id, thread_id, seq)
);
CREATE TABLE IF NOT EXISTS topics (
  chat_id INTEGER NOT NULL,
  thread_id INTEGER NOT NULL,
  name TEXT,
  PRIMARY KEY (chat_id, thread_id)
);
CREATE TABLE IF NOT EXISTS session_meta (
  chat_id INTEGER NOT NULL,
  thread_id INTEGER NOT NULL,
  session_id TEXT,
  name TEXT,
  cost_usd REAL NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  tokens_cached_read INTEGER NOT NULL DEFAULT 0,
  tokens_cached_write INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (chat_id, thread_id)
);
"""


def new_session_id(ts=None):
    """Stable session id: yyyymmdd-hhmm-4hex, assigned at creation."""
    return time.strftime("%Y%m%d-%H%M", time.localtime(ts or time.time())) \
        + "-" + secrets.token_hex(2)


class Store:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._add_missing_columns()
            self._migrate()
            self._backfill_meta()
            self.conn.commit()

    def _add_missing_columns(self):
        """ALTER for databases created before append-only messages.

        SQLite has no `ADD COLUMN IF NOT EXISTS`; PRAGMA tells us what is
        there. Steady state this writes nothing (no DDL, no transaction)."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(sessions)")}
        for name, decl in (("base_seq", "INTEGER NOT NULL DEFAULT 1"),
                           ("n_msgs", "INTEGER NOT NULL DEFAULT 0"),
                           ("next_seq", "INTEGER NOT NULL DEFAULT 1")):
            if name not in have:
                self.conn.execute("ALTER TABLE sessions ADD COLUMN %s %s"
                                  % (name, decl))

    def _migrate(self):
        """Move history blobs into session_messages, once.

        A row is unconverted while it still carries a JSON blob and claims no
        visible messages; nothing else qualifies, so this is a no-op from the
        second boot on. seq is 1-based and preserved across the conversion so
        the archive below the first fold starts exactly where the blob did."""
        rows = self.conn.execute(
            "SELECT chat_id, thread_id, messages FROM sessions"
            " WHERE n_msgs = 0 AND messages != ''").fetchall()
        for chat_id, thread_id, blob in rows:
            try:
                messages = json.loads(blob)
            except ValueError:
                log.warning("migration: unreadable history blob for"
                            " chat=%s thread=%s - left as is", chat_id, thread_id)
                continue
            self.conn.execute(
                "DELETE FROM session_messages WHERE chat_id=? AND thread_id=?",
                (chat_id, thread_id))
            self.conn.executemany(
                "INSERT INTO session_messages (chat_id, thread_id, seq, payload)"
                " VALUES (?, ?, ?, ?)",
                [(chat_id, thread_id, i + 1, json.dumps(m, ensure_ascii=False))
                 for i, m in enumerate(messages)])
            self.conn.execute(
                "UPDATE sessions SET messages='', base_seq=1, n_msgs=?, next_seq=?"
                " WHERE chat_id=? AND thread_id=?",
                (len(messages), len(messages) + 1, chat_id, thread_id))

    def _backfill_meta(self):
        """Give pre-existing sessions a session id; fill any NULL ids.

        Steady state this writes NOTHING: an unconditional UPDATE opened a
        write transaction at every startup and cost a commit+fsync on a
        database that already had its ids (measured 500ms of a 1.8s boot on
        this disk). The queries below only insert what is actually missing."""
        rows = self.conn.execute(
            "SELECT s.chat_id, s.thread_id, s.updated_at FROM sessions s "
            "LEFT JOIN session_meta m ON m.chat_id = s.chat_id "
            "AND m.thread_id = s.thread_id WHERE m.session_id IS NULL").fetchall()
        for chat_id, thread_id, updated in rows:
            self.conn.execute(
                "INSERT OR IGNORE INTO session_meta (chat_id, thread_id, session_id) "
                "VALUES (?, ?, ?)", (chat_id, thread_id, new_session_id(updated)))
        nulls = self.conn.execute(
            "SELECT COUNT(*) FROM session_meta WHERE session_id IS NULL").fetchone()[0]
        if nulls:
            self.conn.execute(
                "UPDATE session_meta SET session_id=? WHERE session_id IS NULL",
                (new_session_id(),))

    # ------------------------------------------------ sessions

    def save_session(self, chat_id, thread_id, model, messages):
        """Write the session through, appending only what is new.

        O(new messages) instead of re-serializing the whole history at every
        tool boundary. A shorter list means compaction: the window moves on
        and the rows already written stay behind as the archive, so a fold
        can no longer destroy the raw conversation."""
        if not messages:
            self.delete_session(chat_id, thread_id)
            return
        with self.lock:
            row = self.conn.execute(
                "SELECT base_seq, n_msgs, next_seq FROM sessions"
                " WHERE chat_id=? AND thread_id=?", (chat_id, thread_id)).fetchone()
            base, visible, nxt = row if row else (1, 0, 1)
            if visible and (len(messages) < visible or not self._same_message(
                    chat_id, thread_id, base + visible - 1, messages[visible - 1])):
                # not an append: a new generation starts above the old rows
                base, visible = nxt, 0
            fresh = [(chat_id, thread_id, nxt + i, json.dumps(m, ensure_ascii=False))
                     for i, m in enumerate(messages[visible:])]
            if fresh:
                self.conn.executemany(
                    "INSERT INTO session_messages (chat_id, thread_id, seq, payload)"
                    " VALUES (?, ?, ?, ?)", fresh)
                nxt += len(fresh)
            self.conn.execute(
                "INSERT INTO sessions (chat_id, thread_id, model, messages, updated_at,"
                " base_seq, n_msgs, next_seq) VALUES (?, ?, ?, '', ?, ?, ?, ?)"
                " ON CONFLICT(chat_id, thread_id) DO UPDATE SET"
                " model=excluded.model, updated_at=excluded.updated_at,"
                " base_seq=excluded.base_seq, n_msgs=excluded.n_msgs,"
                " next_seq=excluded.next_seq",
                (chat_id, thread_id, model, time.time(), base, len(messages), nxt))
            self.conn.commit()

    def _same_message(self, chat_id, thread_id, seq, message):
        """True when the row already stored at `seq` is this exact message.
        A cheap append/diverge test: only the last visible message is checked,
        which is what distinguishes "one more round" from "history rewritten"."""
        row = self.conn.execute(
            "SELECT payload FROM session_messages"
            " WHERE chat_id=? AND thread_id=? AND seq=?",
            (chat_id, thread_id, seq)).fetchone()
        return bool(row) and row[0] == json.dumps(message, ensure_ascii=False)

    def delete_session(self, chat_id, thread_id):
        with self.lock:
            row = self.conn.execute(
                "SELECT session_id FROM session_meta WHERE chat_id=? AND thread_id=?",
                (chat_id, thread_id)).fetchone()
            self.conn.execute("DELETE FROM sessions WHERE chat_id=? AND thread_id=?",
                              (chat_id, thread_id))
            self.conn.execute(
                "DELETE FROM session_messages WHERE chat_id=? AND thread_id=?",
                (chat_id, thread_id))
            self.conn.execute("DELETE FROM session_meta WHERE chat_id=? AND thread_id=?",
                              (chat_id, thread_id))
            self.conn.commit()
        self._drop_spill(row[0] if row else None)

    def _drop_spill(self, session_id):
        """Remove the session's tool_output spill files (deleted session =
        deleted artifacts; they can be large). Best effort."""
        if not session_id:
            return
        root = os.path.join(os.path.dirname(os.path.abspath(self.path)),
                            "sessions", str(session_id))
        shutil.rmtree(root, ignore_errors=True)

    def load_sessions(self):
        """[(chat_id, thread_id, model_or_None, messages, updated_at)]

        Only the VISIBLE slice comes back. Everything below base_seq is the
        archive: still on disk, never sent, recoverable with load_archived().
        """
        with self.lock:
            rows = self.conn.execute(
                "SELECT s.chat_id, s.thread_id, s.model, s.updated_at, m.payload"
                " FROM sessions s LEFT JOIN session_messages m"
                " ON m.chat_id = s.chat_id AND m.thread_id = s.thread_id"
                " AND m.seq >= s.base_seq AND m.seq < s.base_seq + s.n_msgs"
                " ORDER BY s.chat_id, s.thread_id, m.seq").fetchall()
        out, seen = [], None
        for chat_id, thread_id, model, updated, payload in rows:
            key = (chat_id, thread_id)
            if key != seen:
                out.append((chat_id, thread_id, model, [], updated))
                seen = key
            if payload is None:
                continue
            try:
                out[-1][3].append(json.loads(payload))
            except ValueError:
                continue  # corrupt row: skip
        return out

    def archive_count(self, chat_id, thread_id):
        """How many folded-away messages are still on disk for this session."""
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM session_messages m JOIN sessions s"
                " ON m.chat_id = s.chat_id AND m.thread_id = s.thread_id"
                " WHERE m.chat_id=? AND m.thread_id=? AND m.seq < s.base_seq",
                (chat_id, thread_id)).fetchone()[0]

    def load_archived(self, chat_id, thread_id):
        """The folded-away messages, oldest first - what a summary replaced."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT m.payload FROM session_messages m JOIN sessions s"
                " ON m.chat_id = s.chat_id AND m.thread_id = s.thread_id"
                " WHERE m.chat_id=? AND m.thread_id=? AND m.seq < s.base_seq"
                " ORDER BY m.seq", (chat_id, thread_id)).fetchall()
        out = []
        for (payload,) in rows:
            try:
                out.append(json.loads(payload))
            except ValueError:
                continue
        return out

    def list_sessions(self):
        return self.load_sessions()

    def delete_chat(self, chat_id):
        """Remove all sessions + topic rows for a chat. Returns topic thread ids."""
        with self.lock:
            topics = [r[0] for r in self.conn.execute(
                "SELECT thread_id FROM topics WHERE chat_id=?", (chat_id,)).fetchall()]
            sids = [r[0] for r in self.conn.execute(
                "SELECT session_id FROM session_meta WHERE chat_id=?",
                (chat_id,)).fetchall()]
            self.conn.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
            self.conn.execute("DELETE FROM session_messages WHERE chat_id=?", (chat_id,))
            self.conn.execute("DELETE FROM topics WHERE chat_id=?", (chat_id,))
            self.conn.execute("DELETE FROM session_meta WHERE chat_id=?", (chat_id,))
            self.conn.commit()
        for sid in sids:
            self._drop_spill(sid)
        return topics

    # ------------------------------------------------ session meta
    # session id + name + cost/token accumulators, stored separately from the
    # history row so save_session's INSERT OR REPLACE can never wipe them.
    # A session's SLOT (thread_id) is its binding: >0 = bound to that topic,
    # 0 = main chat, <0 = parked (unbound, recoverable via /session).

    def create_session(self, chat_id, thread_id, name=None):
        """Eagerly create an empty session at a slot (binds it there)."""
        now = time.time()
        with self.lock:
            self.conn.execute(
                "DELETE FROM session_messages WHERE chat_id=? AND thread_id=?",
                (chat_id, thread_id))
            self.conn.execute(
                "INSERT OR REPLACE INTO sessions (chat_id, thread_id, model, messages,"
                " updated_at, base_seq, n_msgs, next_seq)"
                " VALUES (?, ?, NULL, '', ?, 1, 0, 1)", (chat_id, thread_id, now))
            self.conn.execute(
                "DELETE FROM session_meta WHERE chat_id=? AND thread_id=?",
                (chat_id, thread_id))
            self.conn.execute(
                "INSERT INTO session_meta (chat_id, thread_id, session_id, name)"
                " VALUES (?, ?, ?, ?)",
                (chat_id, thread_id, new_session_id(), name))
            self.conn.commit()

    def move_session(self, chat_id, frm, to):
        """Relocate a session (history + meta) from one slot to another.
        The target slot must be free - the caller displaces any occupant."""
        with self.lock:
            if self.conn.execute(
                    "SELECT 1 FROM sessions WHERE chat_id=? AND thread_id=?",
                    (chat_id, to)).fetchone():
                raise ValueError("target slot %s/%s is occupied" % (chat_id, to))
            self.conn.execute(
                "UPDATE sessions SET thread_id=? WHERE chat_id=? AND thread_id=?",
                (to, chat_id, frm))
            self.conn.execute(
                "UPDATE session_messages SET thread_id=? WHERE chat_id=? AND thread_id=?",
                (to, chat_id, frm))
            self.conn.execute(
                "UPDATE session_meta SET thread_id=? WHERE chat_id=? AND thread_id=?",
                (to, chat_id, frm))
            self.conn.commit()

    def set_session_name(self, chat_id, thread_id, name):
        with self.lock:
            self.ensure_meta(chat_id, thread_id)
            self.conn.execute(
                "UPDATE session_meta SET name=? WHERE chat_id=? AND thread_id=?",
                (name, chat_id, thread_id))
            self.conn.commit()

    def load_meta(self):
        """{(chat_id, thread_id): {session_id, name, cost_usd, tokens_*}}"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT chat_id, thread_id, session_id, name, cost_usd, tokens_in,"
                " tokens_out, tokens_cached_read, tokens_cached_write"
                " FROM session_meta").fetchall()
        return {(r[0], r[1]): {"session_id": r[2], "name": r[3], "cost_usd": r[4],
                               "tokens_in": r[5], "tokens_out": r[6],
                               "tokens_cached_read": r[7],
                               "tokens_cached_write": r[8]} for r in rows}

    def ensure_meta(self, chat_id, thread_id):
        """Meta dict for (chat, thread), creating the row if missing."""
        with self.lock:
            row = self.conn.execute(
                "SELECT session_id, name, cost_usd, tokens_in, tokens_out,"
                " tokens_cached_read, tokens_cached_write FROM session_meta"
                " WHERE chat_id=? AND thread_id=?", (chat_id, thread_id)).fetchone()
            if row:
                return {"session_id": row[0], "name": row[1], "cost_usd": row[2],
                        "tokens_in": row[3], "tokens_out": row[4],
                        "tokens_cached_read": row[5], "tokens_cached_write": row[6]}
            meta = {"session_id": new_session_id(), "name": None, "cost_usd": 0.0,
                    "tokens_in": 0, "tokens_out": 0, "tokens_cached_read": 0,
                    "tokens_cached_write": 0}
            self.conn.execute(
                "INSERT INTO session_meta (chat_id, thread_id, session_id, name)"
                " VALUES (?, ?, ?, NULL)",
                (chat_id, thread_id, meta["session_id"]))
            self.conn.commit()
            return meta

    def record_usage(self, chat_id, thread_id, cost, tokens):
        """Add one call's notional cost + token counts to the accumulators."""
        with self.lock:
            self.ensure_meta(chat_id, thread_id)
            self.conn.execute(
                "UPDATE session_meta SET cost_usd = cost_usd + ?,"
                " tokens_in = tokens_in + ?, tokens_out = tokens_out + ?,"
                " tokens_cached_read = tokens_cached_read + ?,"
                " tokens_cached_write = tokens_cached_write + ?"
                " WHERE chat_id=? AND thread_id=?",
                (float(cost), int(tokens.get("tokens_in") or 0),
                 int(tokens.get("tokens_out") or 0),
                 int(tokens.get("tokens_cached_read") or 0),
                 int(tokens.get("tokens_cached_write") or 0),
                 chat_id, thread_id))
            self.conn.commit()

    # ------------------------------------------------ topics

    def add_topic(self, chat_id, thread_id, name=None):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO topics (chat_id, thread_id, name) "
                              "VALUES (?, ?, ?)", (chat_id, thread_id, name))
            self.conn.commit()

    def set_topic_name(self, chat_id, thread_id, name):
        with self.lock:
            self.conn.execute("UPDATE topics SET name=? WHERE chat_id=? AND thread_id=?",
                              (name, chat_id, thread_id))
            self.conn.commit()

    def load_topics(self):
        """[(chat_id, thread_id, name)]"""
        with self.lock:
            return self.conn.execute(
                "SELECT chat_id, thread_id, name FROM topics").fetchall()

    def remove_topic(self, chat_id, thread_id):
        """Drop a dead topic from the registry (lazy cleanup after a failed ping)."""
        with self.lock:
            self.conn.execute("DELETE FROM topics WHERE chat_id=? AND thread_id=?",
                              (chat_id, thread_id))
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()
