"""SQLite session store - sessions, topics and model choice survive restarts.

Stdlib sqlite3. Write-through: main updates state then persists the row.
thread_id 0 is the sentinel for "main chat, no topic".
Thread-safety: session workers run concurrently, so the shared connection
is opened with check_same_thread=False and every access goes through one
re-entrant lock (SQLite stays happy, compound ops stay atomic).
"""
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  chat_id INTEGER NOT NULL,
  thread_id INTEGER NOT NULL,
  model TEXT,
  messages TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (chat_id, thread_id)
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
            self._backfill_meta()
            self.conn.commit()

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
        if not messages:
            self.delete_session(chat_id, thread_id)
            return
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO sessions (chat_id, thread_id, model, messages, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, thread_id, model, json.dumps(messages), time.time()))
            self.conn.commit()

    def delete_session(self, chat_id, thread_id):
        with self.lock:
            row = self.conn.execute(
                "SELECT session_id FROM session_meta WHERE chat_id=? AND thread_id=?",
                (chat_id, thread_id)).fetchone()
            self.conn.execute("DELETE FROM sessions WHERE chat_id=? AND thread_id=?",
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
        """[(chat_id, thread_id, model_or_None, messages, updated_at)]"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT chat_id, thread_id, model, messages, updated_at FROM sessions").fetchall()
        out = []
        for chat_id, thread_id, model, messages, updated in rows:
            try:
                out.append((chat_id, thread_id, model, json.loads(messages), updated))
            except ValueError:
                continue  # corrupt row: skip
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
                "INSERT OR REPLACE INTO sessions (chat_id, thread_id, model, messages, updated_at)"
                " VALUES (?, ?, NULL, '[]', ?)", (chat_id, thread_id, now))
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
