"""Keirai entry point: Telegram long-polling loop, commands, AI chat.

Stdlib only. Run: python main.py  (config from ./config.toml or KEIRAI_CONFIG)
"""
import base64
import hashlib
import html
import json
import logging
import logging.handlers
import mimetypes
import os
import queue
import re
import secrets
import string
import subprocess
import sys
import threading
import time

import config as config_mod
import md2tg
import providers
import sessions
import telegram as tg_mod
import textcalls
import tools as tools_mod

log = logging.getLogger("keirai")

CHARS_PER_TOKEN = 4  # ordinary ASCII text: this many chars per token
IMAGE_KEEP = 4  # newest image-bearing messages whose pictures are re-sent
COMPACT_FALLBACK_KEEP = 6  # floor of the last-resort trim (see chat_with_history)
CALL_ARG_CHARS = 500  # tool-call arguments kept per call in a compaction summary
CALL_BLOCK_CHARS = 2000  # ... and for all of one message's calls together
REASONING_KEEP_CHARS = 4000  # reasoning kept per assistant message in context
                             # (tail only - conclusions live at the end)
TITLE_WAIT = 10  # seconds the rename may take before the reply starts (derived
                 # title past that - the rename itself always happens first)
SLOT_NONE = object()  # sid_slot(): not found (None means the main-chat slot)
TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".sh", ".bat", ".ps1", ".c", ".h", ".cpp", ".hpp", ".rs",
    ".go", ".java", ".rb", ".xml", ".html", ".css", ".sql", ".log", ".csv",
}
IMAGE_MIMES = ("image/jpeg", "image/png", "image/gif", "image/webp")


def _session_header(session_id):
    """x-opencode-session value for ONE keirai session.

    Go/Zen ask for a stable session id *per conversation* ("so we can
    optimize routing and prompt caching") and pin both to it. One app-wide
    id put every conversation in the same bucket; this derives a distinct
    id per keirai session, stable for its whole life. Hashed so the raw id
    (creation timestamp + entropy) never leaves the machine.
    """
    return hashlib.sha256(
        ("keirai:%s" % (session_id or "")).encode("utf-8")).hexdigest()[:32]


class State:
    """Session state: in-memory + optional SQLite write-through persistence."""

    def __init__(self, thinking_default, store=None):
        self._default = thinking_default
        self.store = store
        self.topics_enabled = False  # bot has topic mode in private chats (getMe)
        self.session_count = {}  # chat_id -> int, for auto session names
        self.topics = {}         # chat_id -> [thread_id] of topics we created
        self.pending_reset = {}  # chat_id -> (s1, s2) confirmation challenge
        self.thinking = {}   # user_id -> bool
        self.model = {}      # (chat_id, thread) -> "provider/model"
        self.history = {}    # (chat_id, thread) -> [messages]
        self.meta = {}       # (chat_id, thread) -> {session_id, name, cost, tokens}
        self.topic_names = {}  # (chat_id, thread_id) -> topic name
        self.prompts = {}    # (chat_id, slot) -> pending typed-choice prompt
        self.page = {}       # (chat_id, slot) -> current /session page
        self.updated = {}    # (chat_id, slot) -> last-active timestamp
        self.allows_user_topics = False  # getMe: "Disallow users to create topics"
        self.bot_id = None  # our own id - Telegram echoes back our own events
        self._turn_lock = threading.Lock()
        self.active_turns = {}   # chat_key -> True while a reply/tool turn runs
        self.interrupts = set()  # chat_keys the user asked to /stop
        self.turn_inbox = {}     # chat_key -> [updates sent mid-turn]
        # slot bookkeeping (/new, /session) runs on session workers that may
        # be concurrent within a chat - guard counters + slot moves
        self._slot_lock = threading.RLock()
        if store:
            self._load()

    # ------------------------------------------------ /stop bookkeeping
    def begin_turn(self, key):
        with self._turn_lock:
            self.active_turns[key] = True
            self.interrupts.discard(key)

    def end_turn(self, key):
        with self._turn_lock:
            self.active_turns.pop(key, None)
            self.interrupts.discard(key)

    def request_interrupt(self, key):
        """Flag a running turn to stop. True if one was running for `key`."""
        with self._turn_lock:
            if key in self.active_turns:
                self.interrupts.add(key)
                return True
            return False

    def interrupted(self, key):
        with self._turn_lock:
            return key in self.interrupts

    # ------------------------------------------- mid-turn message inbox
    # A plain message in a session whose turn is running is parked here and
    # folded into context at the next tool boundary (steering without /stop)
    # instead of waiting for the turn to end.

    def inject(self, key, upd):
        """Park `upd` for the turn running on `key`. True when injected."""
        with self._turn_lock:
            if key in self.active_turns:
                self.turn_inbox.setdefault(key, []).append(upd)
                return True
            return False

    def take_inbox(self, key):
        """Pending mid-turn messages for `key` (thread-safe swap)."""
        with self._turn_lock:
            return self.turn_inbox.pop(key, None) or []

    def _load(self):
        for chat_id, thread_id, model, messages, updated in self.store.load_sessions():
            key = (chat_id, None if thread_id == 0 else thread_id)
            self.history[key] = messages
            self.updated[key] = updated
            if model:
                self.model[key] = model
        for (chat_id, thread_id), m in self.store.load_meta().items():
            if not m.get("session_id"):
                m["session_id"] = sessions.new_session_id()
            self.meta[(chat_id, None if thread_id == 0 else thread_id)] = m
        for key in list(self.history):
            if key not in self.meta:  # sessions predating meta (or meta-less)
                self.ensure_meta(key[0], key[1])
        for chat_id, thread_id, name in self.store.load_topics():
            self.topics.setdefault(chat_id, []).append(thread_id)
            self.topic_names[(chat_id, thread_id)] = name or ""
        for chat_id, tids in self.topics.items():
            self.session_count[chat_id] = len(tids) + 1

    def persist(self, chat_id, thread_id):
        """Write current session (history + model) through to the store.

        Also stamps last-agent-activity: this runs at every tool boundary and
        at turn end (including a turn the user interrupted), so the stamp IS
        "when the agent stopped working" - the timestamp the clock block puts
        in front of the next user message. Stamped whether or not a store is
        attached, because it is session state rather than a side effect of the
        DB write; an empty history means the session was wiped, so the stamp
        goes with it.
        """
        key = self.chat_key(chat_id, thread_id)
        if self.history.get(key):
            self.updated[key] = time.time()
        if not self.store:
            return
        self.store.save_session(chat_id, thread_id or 0, self.model.get(key),
                                self.history.get(key) or [])
        if not (self.history.get(key) or []):
            # history wiped -> the session (and its accumulators) is gone
            self.history.pop(key, None)
            self.updated.pop(key, None)
            self.meta.pop(key, None)

    def thinking_on(self, user_id):
        return self.thinking.get(user_id, self._default)

    def set_thinking(self, user_id, on):
        self.thinking[user_id] = bool(on)

    def chat_key(self, chat_id, thread_id):
        return (chat_id, thread_id)

    def model_for(self, chat_id, thread_id, default_model):
        return self.model.get(self.chat_key(chat_id, thread_id), default_model)

    def ensure_meta(self, chat_id, thread_id):
        """{session_id, name, cost_usd, tokens_*} for this session (created lazily)."""
        key = self.chat_key(chat_id, thread_id)
        m = self.meta.get(key)
        if m is None:
            if self.store:
                m = self.store.ensure_meta(chat_id, thread_id or 0)
                if not m.get("session_id"):
                    m["session_id"] = sessions.new_session_id()
            else:
                m = {"session_id": sessions.new_session_id(), "name": None,
                     "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0,
                     "tokens_cached_read": 0, "tokens_cached_write": 0}
            self.meta[key] = m
        return m

    def add_usage(self, chat_id, thread_id, cost, tokens):
        """Accumulate one provider call's notional cost + token counts."""
        m = self.ensure_meta(chat_id, thread_id)
        m["cost_usd"] = (m.get("cost_usd") or 0.0) + float(cost)
        for k in ("tokens_in", "tokens_out", "tokens_cached_read",
                  "tokens_cached_write"):
            m[k] = (m.get(k) or 0) + int(tokens.get(k) or 0)
        if self.store:
            self.store.record_usage(chat_id, thread_id or 0, cost, tokens)

    # ------------------------------------------------ session slots (P4)
    # A session's slot (thread id) IS its binding: >0 = bound to that topic,
    # None = main chat, <0 = parked (unbound, recoverable via /session).

    def session_at(self, chat_id, thread_id):
        """Is a session bound at this slot? (an empty history still counts)"""
        return self.chat_key(chat_id, thread_id) in self.history

    def sid_slot(self, chat_id, sid):
        """Which slot holds session id `sid`? SLOT_NONE when unknown
        (None is a real slot - the main chat)."""
        for key, m in self.meta.items():
            if key[0] == chat_id and m.get("session_id") == sid:
                return key[1]
        return SLOT_NONE

    def session_name(self, chat_id, thread_id):
        key = self.chat_key(chat_id, thread_id)
        m = self.meta.get(key) or {}
        return m.get("name") or self.topic_names.get(key) or ""

    def free_slot(self, chat_id):
        with self._slot_lock:
            used = {k[1] for k in self.history if k[0] == chat_id}
            used |= {k[1] for k in self.meta if k[0] == chat_id}
            slot = -1
            while slot in used:
                slot -= 1
            return slot

    def move_session(self, chat_id, frm, to):
        """Relocate session data between slots (state + store). Target free."""
        with self._slot_lock:
            src = self.chat_key(chat_id, frm)
            dst = self.chat_key(chat_id, to)
            if src == dst:
                return
            if dst in self.history or dst in self.meta:
                raise ValueError("target slot occupied: %r" % (dst,))
            for attr in ("history", "model", "meta", "updated"):
                d = getattr(self, attr)
                if src in d:
                    d[dst] = d.pop(src)
            if self.store:
                self.store.move_session(chat_id, frm or 0, to or 0)

    def displace(self, chat_id, thread_id):
        """Park whatever session occupies the slot (kept, recoverable)."""
        with self._slot_lock:
            key = self.chat_key(chat_id, thread_id)
            if key not in self.history and key not in self.meta:
                return False
            self.move_session(chat_id, thread_id, self.free_slot(chat_id))
            return True

    def create_session(self, chat_id, thread_id, name=None):
        """Fresh empty session bound at the slot."""
        with self._slot_lock:
            key = self.chat_key(chat_id, thread_id)
            self.history[key] = []
            self.meta[key] = {"session_id": sessions.new_session_id(),
                              "name": name,
                              # provenance: a name from /new <name> is the user's
                              "name_src": ("user" if name else None),
                              "cost_usd": 0.0, "tokens_in": 0,
                              "tokens_out": 0, "tokens_cached_read": 0,
                              "tokens_cached_write": 0}
            # NB: no updated[] stamp here - creating a session is not the agent
            # doing work, and the clock block must read "first message" until
            # a turn actually runs (persist() stamps that)
            if self.store:
                self.store.create_session(chat_id, thread_id or 0, name)

    def remove_topic_ref(self, chat_id, tid):
        """Drop a dead topic from the registry (after a failed ping)."""
        if tid in self.topics.get(chat_id, []):
            self.topics[chat_id].remove(tid)
        self.topic_names.pop((chat_id, tid), None)
        if self.store:
            self.store.remove_topic(chat_id, tid)

    def get_history(self, chat_id, thread_id):
        return self.history.setdefault(self.chat_key(chat_id, thread_id), [])

    def last_activity(self, chat_id, thread_id):
        """Epoch of when this session's agent last did work.

        persist() runs at every tool boundary and at turn end (including an
        interrupted turn), so this is exactly "when it stopped working": the
        last reply, or the last tool call of a run the user cut short. None
        for a session that has never run a turn."""
        return self.updated.get(self.chat_key(chat_id, thread_id)) or None

    def next_session_num(self, chat_id):
        with self._slot_lock:
            n = self.session_count.get(chat_id, 0) + 1
            self.session_count[chat_id] = n
            return n


# ---------------------------------------------------------------- commands
# Command registry: name -> (handler, help_text, hidden)
# handler signature: fn(cfg, tg, msg, thread_id, state)

COMMANDS = {}


def command(name, help_text, hidden=False):
    def deco(fn):
        COMMANDS[name] = (fn, help_text, hidden)
        return fn
    return deco


def build_help():
    lines = ["<b>Keirai</b> - lightweight AI agent", "", "<b>Commands</b>"]
    for name in sorted(COMMANDS):
        fn, desc, hidden = COMMANDS[name]
        if not hidden:
            # the body is HTML, but a description is prose: "<provider/model-id>"
            # is not a tag, and Telegram rejects the whole message (HTTP 400
            # "Unsupported start tag") - which killed /help and /start silently
            lines.append("/%s - %s" % (name, html.escape(desc)))
    lines += [
        "",
        "Sessions live in the plain main chat by default. Run /topic to enter "
        "<b>topic flow</b>, where each topic = one session (Threaded mode must "
        "be on, with \"Disallow users to create topics\" enabled); /session "
        "lists and switches sessions.",
        "",
        "Send a photo to talk about it. Send media with caption <code>media_test</code> "
        "to test the media round-trip.",
        "",
        "The agent can use tools: files, shell, web search/fetch - and a headless "
        "browser once <code>deploy/install_lightpanda.sh</code> has been run.",
        "While a reply or tool is running, /stop interrupts it.",
    ]
    return "\n".join(lines)


@command("start", "this message")
@command("help", "this message")
def cmd_start(cfg, tg, msg, thread_id, state):
    tg.send(msg["chat"]["id"], build_help(), thread_id)

TEST_MD_BODY = """**bold** *italic* ~~strike~~ ||spoiler|| `inline code`
# Header
- list item
[link](https://opencode.ai) and bare https://example.com?a=1&b=2

```python
def hi():
    print("<hello & bye>")
```
> a blockquote
snake_case and file_name.txt stay literal"""


@command("test_md", "run markdown through the regular rendering pipeline (arg optional)")
def cmd_test_md(cfg, tg, msg, thread_id, state):
    src = _arg(msg) or TEST_MD_BODY
    try:
        for part in md2tg.split(src):
            tg.send(msg["chat"]["id"], part, thread_id)
    except Exception as e:
        _err(tg, msg, thread_id, "test_md failed: %s" % e)


TEST_RICH_BODY = """# Rich message test

Native **GFM table**:

| Provider | Model | Context |
|:---------|:-----:|--------:|
| zen | glm-5.3-flash | 1,000,000 |
| go | kimi-k3 | 1,048,576 |

- [ ] task list item
- [x] completed item
- nested
  - sub item

Inline `code`, ==marked text==, ~~strike~~, ||spoiler|| and $$E = mc^2$$

Media block (rich path renders it natively):
![Keirai](https://raw.githubusercontent.com/thanhvd1403/keirai/main/LICENSE)"""


@command("test_rich", "test rich message rendering (tables, task lists, formulas)")
def cmd_test_rich(cfg, tg, msg, thread_id, state):
    src = _arg(msg) or TEST_RICH_BODY
    try:
        for chunk in md2tg.split_rich(src):
            tg.send_rich(msg["chat"]["id"], markdown=chunk, thread_id=thread_id)
    except Exception as e:
        _err(tg, msg, thread_id, "test_rich failed: %s" % e)


@command("thinking", "show/hide AI reasoning: /thinking on|off")
def cmd_thinking(cfg, tg, msg, thread_id, state):
    arg = _arg(msg).strip().lower()
    user_id = msg["from"]["id"]
    if arg in ("on", "off"):
        state.set_thinking(user_id, arg == "on")
    tg.send(msg["chat"]["id"], "thinking is <b>%s</b>" % ("on" if state.thinking_on(user_id) else "off"), thread_id)


@command("models", "list models from configured providers (with context/cost stats)")
def cmd_models(cfg, tg, msg, thread_id, state):
    lines = []
    try:  # context/pricing stats come from models.dev (the /models API has none)
        meta_all, _ = providers.models_metadata(config_mod.cache_dir(cfg))
    except Exception:
        meta_all = {}
    for name in ("zen", "go"):
        key = cfg.provider_key(name)
        if not key:
            lines.append("<b>%s</b>: no API key configured" % name)
            continue
        try:
            models, from_cache = providers.cached_models(name, key, config_mod.cache_dir(cfg))
            mdata = meta_all.get(name) or {}
            for m in models:
                extra = mdata.get(m["id"])
                if extra:
                    for field in ("context", "max_output", "cost_in", "cost_out"):
                        if m.get(field) is None and extra.get(field) is not None:
                            m[field] = extra[field]
            lines.append("<b>%s</b> (%s, %d models)" % (name, "cached" if from_cache else "fetched", len(models)))
            lines.append(_format_models(models))
        except Exception as e:
            lines.append("<b>%s</b>: error: %s" % (name, _esc(str(e))))
    text = "\n".join(lines) or "no providers configured"
    for part in md2tg.split(text):
        tg.send(msg["chat"]["id"], part, thread_id)


@command("model", "switch model: /model provider/model-id, or /model global provider/model-id")
def cmd_model(cfg, tg, msg, thread_id, state):
    arg = _arg(msg).strip()
    chat_id = msg["chat"]["id"]
    if arg.lower().startswith("global"):  # chat-wide default (group 22)
        val = arg[6:].strip()
        if "/" not in val or val.split("/", 1)[0] not in providers.PROVIDER_BASES:
            tg.send(chat_id, "usage: /model global &lt;provider/model&gt; "
                             "(e.g. /model global go/glm-5.3-flash)", thread_id)
            return
        lit = config_mod.write_value(cfg, "default_model", val)
        cfg.default_model = val
        if lit is None:
            tg.send(chat_id, "default model set to <code>%s</code> (memory only "
                             "- no config file to persist to)" % _esc(val),
                    thread_id)
        else:
            tg.send(chat_id, "default model set to <code>%s</code> (written to "
                             "config.toml)" % _esc(val), thread_id)
        return
    if not arg:
        current = state.model_for(chat_id, thread_id, cfg.default_model)
        tg.send(chat_id, "current model: <code>%s</code>\nusage: /model &lt;provider/model&gt;" % _esc(current), thread_id)
        return
    if "/" not in arg or arg.split("/", 1)[0] not in providers.PROVIDER_BASES:
        tg.send(chat_id, "model must look like <code>zen/&lt;model-id&gt;</code> or <code>go/&lt;model-id&gt;</code>", thread_id)
        return
    state.model[state.chat_key(chat_id, thread_id)] = arg
    state.persist(chat_id, thread_id)
    tg.send(chat_id, "model set to <code>%s</code>" % _esc(arg), thread_id)


@command("new", "start a new session (new topic in topic flow): /new [name]")
def cmd_new(cfg, tg, msg, thread_id, state):
    """Topic flow: new topic + eagerly-bound session (agent title lands after
    the first message). Normal flow: the old session is parked (kept and
    recoverable via /session), a blank one takes its place."""
    chat_id = msg["chat"]["id"]
    name = _arg(msg).strip()
    if cfg.topic_flow:
        placeholder = name or "New session"
        try:
            topic = tg.create_topic(chat_id, placeholder)
        except Exception as e:
            _err(tg, msg, thread_id, "could not create topic: %s" % e)
            return
        tid = topic["message_thread_id"]
        if tid not in state.topics.setdefault(chat_id, []):
            state.topics[chat_id].append(tid)
        state.topic_names[(chat_id, tid)] = placeholder
        if state.store:
            state.store.add_topic(chat_id, tid, placeholder)
        state.create_session(chat_id, tid, name=name or None)
        log.info("session created: chat=%s topic=%s name=%r", chat_id, tid, name)
        tg.send(chat_id, "new session started in a topic - type here", tid)
        return
    if state.session_at(chat_id, thread_id):
        state.displace(chat_id, thread_id)
    state.create_session(chat_id, thread_id, name=name or None)
    tg.send(chat_id, "new session started - the previous one is kept; "
                     "recover it with /session.", thread_id)


@command("topic", "switch between normal chat and topic flow: /topic")
def cmd_topic(cfg, tg, msg, thread_id, state):
    """Typed yes/no confirmation; entry verifies both getMe flags (group 22)."""
    chat_id = msg["chat"]["id"]
    slot = thread_id
    entering = not cfg.topic_flow
    state.prompts[(chat_id, slot)] = {"type": "flow", "enter": entering}
    if entering:
        tg.send(chat_id,
                "<b>enter topic flow?</b> - sessions will live in Telegram "
                "topics (one topic = one session, started with /new). Your "
                "current main-chat session stays untouched.\n\n"
                "type <code>yes</code> or <code>no</code>", thread_id)
    else:
        tg.send(chat_id,
                "<b>leave topic flow?</b> - back to the plain main chat. "
                "Topics and bindings are kept as-is.\n\n"
                "type <code>yes</code> or <code>no</code>", thread_id)


def _set_flow(cfg, value):
    """Write-through the topic_flow flag: in-memory + config.toml (survives
    restarts; env-only setups fall back to memory with a warning)."""
    cfg.topic_flow = bool(value)
    lit = config_mod.write_value(cfg, "topic_flow", cfg.topic_flow)
    if lit is None:
        log.warning("no config file available - topic_flow persists in memory only")


def _enter_topic_flow(cfg, tg, chat_id, slot, state):
    """Both preconditions must hold (group 22): Threaded mode ON and the
    'Disallow users to create topics' toggle ENABLED."""
    try:
        me = tg.get_me()
    except Exception as e:
        tg.send(chat_id, "could not verify the topic settings: %s" % _esc(str(e)),
                slot)
        return
    if not me.get("has_topics_enabled"):
        tg.send(chat_id, "Threaded mode is off - enable it for this bot in the "
                         "@BotFather Mini App (t.me/BotFather?startapp), then "
                         "run /topic again.", slot)
        return
    if me.get("allows_users_to_create_topics"):
        tg.send(chat_id, "please enable <b>Disallow users to create topics</b> "
                         "first - otherwise the app silently creates a topic "
                         "per message. Then run /topic again.", slot)
        return
    state.allows_user_topics = False
    _set_flow(cfg, True)
    log.info("topic flow ON: chat=%s", chat_id)
    tg.send(chat_id, "topic flow is <b>on</b> - run /new to create your first "
                     "topic. The All messages view only accepts commands.", slot)


def _leave_topic_flow(cfg, tg, chat_id, slot, state):
    _set_flow(cfg, False)
    log.info("topic flow OFF: chat=%s", chat_id)
    tg.send(chat_id, "topic flow is <b>off</b> - back to the plain main chat. "
                     "Topics and bindings stay as they are.", slot)


# ---------------------------------------------------------------- /session

SESSIONS_PER_PAGE = 5
_EXCLUDE_NOTHING = object()  # All view (topic flow): no session is active


def _session_preview(history):
    """Last user message, truncated; `[image]` when it carries no text."""
    for m in reversed(history):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c
                         if isinstance(p, dict) and p.get("type") == "text")
        c = (c or "").strip()
        if not c:
            return "[image]"
        return c[:60] + ("…" if len(c) > 60 else "")
    return "(empty)"


def _session_rows(state, cfg, chat_id, view_slot):
    """[{sid, name, updated, preview}] for this chat, last-active first,
    excluding the session active at the viewing slot (group 22)."""
    if not cfg.topic_flow:
        exclude = None          # normal flow: the main-chat session is active
    elif view_slot is not None:
        exclude = view_slot     # topic flow: the session bound here is active
    else:
        exclude = _EXCLUDE_NOTHING  # All: nothing active, full list
    rows = []
    for key, m in state.meta.items():
        if key[0] != chat_id or key[1] == exclude:
            continue
        if key not in state.history:  # meta without a session -> skip
            continue
        name = m.get("name") or state.topic_names.get(key) or ""
        rows.append({
            "sid": m.get("session_id") or "?",
            "name": name or "(unnamed)",
            "updated": state.updated.get(key) or 0.0,
            "preview": _session_preview(state.history.get(key) or []),
        })
    rows.sort(key=lambda r: r["updated"], reverse=True)
    return rows


def _send_session_list(tg, chat_id, slot, rows, page, pkey, state):
    if not rows:
        tg.send(chat_id, "no other sessions yet - /new starts one", slot)
        return
    pages = (len(rows) + SESSIONS_PER_PAGE - 1) // SESSIONS_PER_PAGE
    page = max(1, min(page, pages))
    state.page[pkey] = page
    chunk = rows[(page - 1) * SESSIONS_PER_PAGE:page * SESSIONS_PER_PAGE]
    lines = ["<b>sessions</b> (page %d/%d, most recent first):" % (page, pages)]
    for i, r in enumerate(chunk, 1):
        ts = (time.strftime("%Y-%m-%d %H:%M", time.localtime(r["updated"]))
              if r["updated"] else "-")
        lines.append("%d. <b>%s</b> · <code>%s</code>\n   %s · %s"
                     % (i, _esc(r["name"]), _esc(r["sid"]), ts,
                        _esc(r["preview"])))
    lines.append("switch: /session &lt;N&gt; (this page) or /session &lt;id&gt;"
                 " · next page: /session page %d" % (page + 1 if page < pages else page))
    tg.send(chat_id, "\n".join(lines), slot)


@command("session", "list/switch sessions: /session [page N | N | <id>]")
def cmd_session(cfg, tg, msg, thread_id, state):
    chat_id = msg["chat"]["id"]
    slot = thread_id
    pkey = (chat_id, slot)
    arg = _arg(msg).strip()
    rows = _session_rows(state, cfg, chat_id, slot)

    if not arg:
        _send_session_list(tg, chat_id, slot, rows, 1, pkey, state)
        return
    if arg.lower().startswith("page "):
        n = arg[5:].strip()
        if not n.isdigit() or int(n) < 1:
            tg.send(chat_id, "usage: /session page &lt;N&gt;", slot)
            return
        _send_session_list(tg, chat_id, slot, rows, int(n), pkey, state)
        return
    if arg.isdigit():  # Nth session on the CURRENT page
        page = state.page.get(pkey, 1)
        chunk = rows[(page - 1) * SESSIONS_PER_PAGE:page * SESSIONS_PER_PAGE]
        idx = int(arg) - 1
        if idx < 0 or idx >= len(chunk):
            tg.send(chat_id, "no session #%s on this page - run /session to "
                             "list" % _esc(arg), slot)
            return
        _begin_switch(cfg, tg, chat_id, slot, chunk[idx]["sid"], state)
        return
    # session id (contains dashes/hex - never a bare number)
    if state.sid_slot(chat_id, arg) is SLOT_NONE:
        tg.send(chat_id, "no session with id <code>%s</code>" % _esc(arg), slot)
        return
    _begin_switch(cfg, tg, chat_id, slot, arg, state)


def _begin_switch(cfg, tg, chat_id, slot, sid, state):
    """Resolve where the session lives and start the switch (immediately in
    normal flow; via a typed prompt in topic flow - group 22 matrix)."""
    src = state.sid_slot(chat_id, sid)
    if src is SLOT_NONE:
        tg.send(chat_id, "session not found", slot)
        return
    if src == slot:
        tg.send(chat_id, "that session is already active here", slot)
        return
    name = state.session_name(chat_id, src) or "session"
    if not cfg.topic_flow:  # normal flow: switch immediately
        state.displace(chat_id, slot)
        state.move_session(chat_id, src, slot)
        tg.send(chat_id, "switched to <b>%s</b> · <code>%s</code>"
                % (_esc(name), _esc(sid)), slot)
        return
    bound = src is not None and src > 0
    if slot is None:  # invoked from All
        if not bound:  # unbound -> immediately create its topic
            _switch_new_topic(cfg, tg, chat_id, sid, state)
            return
        state.prompts[(chat_id, None)] = {"type": "switch", "actions": {
            "1": {"act": "ping", "sid": sid},
            "2": {"act": "nothing", "sid": sid}}}
        tg.send(chat_id, "<b>%s</b> is bound to a topic. type:\n"
                "1. ping the topic (locate it)\n2. do nothing" % _esc(name), None)
        return
    # invoked inside a topic
    if not bound:
        state.prompts[(chat_id, slot)] = {"type": "switch", "actions": {
            "1": {"act": "switch_here", "sid": sid},
            "2": {"act": "create_new", "sid": sid},
            "3": {"act": "nothing", "sid": sid}}}
        tg.send(chat_id, "<b>%s</b> has no topic. type:\n"
                "1. switch here (bind it to this topic)\n"
                "2. create a new topic for it\n3. do nothing" % _esc(name), slot)
    else:
        state.prompts[(chat_id, slot)] = {"type": "switch", "actions": {
            "1": {"act": "switch_here", "sid": sid},
            "2": {"act": "ping", "sid": sid},
            "3": {"act": "nothing", "sid": sid}}}
        tg.send(chat_id, "<b>%s</b> is bound to another topic. type:\n"
                "1. switch here (moves it into this topic)\n"
                "2. ping the topic (locate it)\n3. do nothing" % _esc(name), slot)


def _switch_into(cfg, tg, chat_id, target_slot, sid, state, rename_topic=True):
    """Move session `sid` into `target_slot`, parking any occupant."""
    src = state.sid_slot(chat_id, sid)
    if src is SLOT_NONE:
        tg.send(chat_id, "session not found", target_slot)
        return False
    if src == target_slot:
        return True
    state.displace(chat_id, target_slot)
    state.move_session(chat_id, src, target_slot)
    if target_slot is not None and rename_topic:
        # session and topic mirror each other (group 22)
        name = state.session_name(chat_id, target_slot)
        if name:
            try:
                tg.edit_topic(chat_id, target_slot, name)
                state.topic_names[(chat_id, target_slot)] = name
            except Exception as e:
                log.warning("topic rename after switch failed: %s", e)
    return True


def _switch_new_topic(cfg, tg, chat_id, sid, state):
    """Create a topic for the session and move it there (All-unbound switch
    and the 'create new topic' prompt action)."""
    src = state.sid_slot(chat_id, sid)
    if src is SLOT_NONE:
        tg.send(chat_id, "session not found", None)
        return
    name = state.session_name(chat_id, src) or "session"
    try:
        topic = tg.create_topic(chat_id, name)
    except Exception as e:
        tg.send(chat_id, "could not create topic: %s" % _esc(str(e)),
                src if src and src > 0 else None)
        return
    tid = topic["message_thread_id"]
    if tid not in state.topics.setdefault(chat_id, []):
        state.topics[chat_id].append(tid)
    state.topic_names[(chat_id, tid)] = name
    if state.store:
        state.store.add_topic(chat_id, tid, name)
    state.move_session(chat_id, src, tid)
    log.info("session %s moved to new topic %s (chat=%s)", sid, tid, chat_id)
    tg.send(chat_id, "session <b>%s</b> has its own topic now - continue there"
            % _esc(name), tid)


def _is_dead_thread(exc):
    s = str(exc).lower()
    return "thread" in s and "not found" in s


def _ping_topic(cfg, tg, chat_id, sid, state, reply_slot):
    """Locate the bound topic; on a dead topic (400 thread not found) unbind
    it and create a fresh topic for the session (automatic recovery)."""
    src = state.sid_slot(chat_id, sid)
    if src is SLOT_NONE or src is None or src <= 0:
        tg.send(chat_id, "that session has no topic to ping", reply_slot)
        return
    name = state.session_name(chat_id, src) or "session"
    try:
        tg.send(chat_id, "ping - <b>%s</b> lives here" % _esc(name), src)
    except Exception as e:
        if _is_dead_thread(e):
            log.warning("ping failed, topic %s is dead - recovering: %s", src, e)
            state.remove_topic_ref(chat_id, src)
            _switch_new_topic(cfg, tg, chat_id, sid, state)
            return
        tg.send(chat_id, "ping failed: %s" % _esc(str(e)), reply_slot)
        return
    if reply_slot != src:
        tg.send(chat_id, "topic found - the session stays bound there", reply_slot)


def _prompt_reply(cfg, tg, chat_id, slot, prompt, text, state):
    """Consume a typed reply to a pending prompt (the caller already popped
    it). True = handled; False = not a choice -> 'do nothing' (the message
    proceeds through the normal rules)."""
    choice = (text or "").strip().lower()
    if prompt.get("type") == "flow":
        if choice in ("y", "yes"):
            if prompt.get("enter"):
                _enter_topic_flow(cfg, tg, chat_id, slot, state)
            else:
                _leave_topic_flow(cfg, tg, chat_id, slot, state)
            return True
        if choice in ("n", "no"):
            tg.send(chat_id, "cancelled", slot)
            return True
        return False
    if prompt.get("type") == "switch":
        act = (prompt.get("actions") or {}).get(choice)
        if not act:
            return False
        kind, sid = act.get("act"), act.get("sid")
        if kind == "switch_here":
            if _switch_into(cfg, tg, chat_id, slot, sid, state):
                name = state.session_name(chat_id, slot) or "session"
                tg.send(chat_id, "switched here: <b>%s</b> · <code>%s</code>"
                        % (_esc(name), _esc(sid)), slot)
        elif kind == "create_new":
            _switch_new_topic(cfg, tg, chat_id, sid, state)
        elif kind == "ping":
            _ping_topic(cfg, tg, chat_id, sid, state, slot)
        # "nothing": deliberately silent
        return True
    return False


@command("reset-all", "wipe every session and delete the bot's topics", hidden=True)
def cmd_reset_all(cfg, tg, msg, thread_id, state):
    """Wipe everything for this chat: all sessions, their context, and the
    Telegram topics we created. Hidden from /help. Requires typing two
    random confirmation strings."""
    chat_id = msg["chat"]["id"]
    args = _arg(msg).split()
    pending = state.pending_reset.get(chat_id)
    if pending and len(args) >= 2 and args[0] == pending[0] and args[1] == pending[1]:
        deleted, failed = 0, 0
        for tid in state.topics.get(chat_id, []):
            try:
                tg.delete_topic(chat_id, tid)
                deleted += 1
            except Exception as e:
                failed += 1
                log.warning("topic delete failed: chat=%s topic=%s: %s", chat_id, tid, e)
        for key in [k for k in state.history if k[0] == chat_id]:
            del state.history[key]
        for key in [k for k in state.model if k[0] == chat_id]:
            del state.model[key]
        for key in [k for k in state.meta if k[0] == chat_id]:
            del state.meta[key]
        for key in [k for k in state.topic_names if k[0] == chat_id]:
            del state.topic_names[key]
        state.session_count.pop(chat_id, None)
        state.topics.pop(chat_id, None)
        state.pending_reset.pop(chat_id, None)
        if state.store:
            state.store.delete_chat(chat_id)
        log.info("reset-all: chat=%s topics deleted=%d failed=%d", chat_id, deleted, failed)
        tg.send(chat_id, "reset done - <b>%d</b> topic(s) deleted, all sessions cleared." % deleted, thread_id)
        return
    s1, s2 = _confirmation_pair(), _confirmation_pair()
    state.pending_reset[chat_id] = (s1, s2)
    log.info("reset-all initiated: chat=%s", chat_id)
    tg.send(chat_id,
            "<b>careful:</b> this deletes ALL sessions, their context and their "
            "Telegram topics. This cannot be undone.\n\n"
            "To confirm, send exactly:\n"
            "<code>/reset-all %s %s</code>" % (s1, s2), thread_id)


def _confirmation_pair():
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(4))


@command("rename", "rename the current session/topic: /rename <name>")
def cmd_rename(cfg, tg, msg, thread_id, state):
    """Renames session AND topic (they mirror each other, group 22); in the
    main chat only the session name is set."""
    chat_id = msg["chat"]["id"]
    name = _arg(msg).strip()
    if not name:
        tg.send(chat_id, "usage: /rename &lt;name&gt;", thread_id)
        return
    if thread_id is None and not state.session_at(chat_id, thread_id):
        tg.send(chat_id, "no active session yet - send a message or /new first",
                thread_id)
        return
    if thread_id is not None:
        try:
            tg.edit_topic(chat_id, thread_id, name)
        except Exception as e:
            _err(tg, msg, thread_id, "could not rename topic: %s" % e)
            return
        if state.store:
            state.store.set_topic_name(chat_id, thread_id, name)
        state.topic_names[(chat_id, thread_id)] = name
    meta = state.ensure_meta(chat_id, thread_id)
    meta["name"] = name
    meta["name_src"] = "user"  # provenance: auto-titling never overwrites
    if state.store:
        state.store.set_session_name(chat_id, thread_id or 0, name)
    where = "topic &amp; session" if thread_id is not None else "session"
    tg.send(chat_id, "renamed (%s) to <b>%s</b>" % (where, _esc(name)), thread_id)


@command("delete", "delete this session's context (topic stays; delete it manually)")
def cmd_delete(cfg, tg, msg, thread_id, state):
    """Clear the conversation context of the current session. The Telegram
    topic is NOT deleted - only the memory of the conversation."""
    chat_id = msg["chat"]["id"]
    key = state.chat_key(chat_id, thread_id)
    had = bool(state.history.get(key))
    state.history.pop(key, None)
    state.model.pop(key, None)
    state.meta.pop(key, None)  # cost/token accumulators die with the session
    if state.store:
        state.store.delete_session(chat_id, thread_id or 0)
    log.info("session deleted: chat=%s thread=%s (had_history=%s)", chat_id, thread_id, had)
    tg.send(chat_id, "session deleted - conversation context cleared."
            "\nthe topic itself stays; you can delete it manually in Telegram.",
            thread_id)


@command("stop", "interrupt the reply/tool currently running in this chat")
def cmd_stop(cfg, tg, msg, thread_id, state):
    """Reaches the main loop only when no turn is running - the watcher
    consumes /stop during an active turn and interrupts it directly."""
    tg.send(msg["chat"]["id"], "nothing is running right now", thread_id)


SERVICE_UNIT = "keirai.service"  # the unit this process runs under
IMPORT_CHECK_TIMEOUT = 30  # seconds before a hanging import counts as broken
RESTART_DEBOUNCE = 30  # seconds: a second /restart inside this window is a no-op


def _restart_mark(base_dir):
    return os.path.join(base_dir, "logs", "keirai.restart.pending")


def _restart_pending(base_dir):
    """A restart was requested very recently (possibly by the run that is
    about to die). Overlapping restarts queue one transient unit each and
    fight: each boot kills the one before it and none of them ever becomes
    ready, so nobody is told anything."""
    try:
        return (time.time() - os.path.getmtime(_restart_mark(base_dir))) < RESTART_DEBOUNCE
    except OSError:
        return False


def _mark_restart_requested(base_dir):
    """Stamp the request and drop the start-notify marker, so this deliberate
    restart always announces itself - the 60s cooldown exists for crash loops,
    not for a restart a human just asked for."""
    try:
        logs = os.path.join(base_dir, "logs")
        os.makedirs(logs, exist_ok=True)
        open(_restart_mark(base_dir), "w").close()
        try:
            os.remove(os.path.join(logs, "keirai.started.marker"))
        except OSError:
            pass
    except OSError as e:
        log.warning("cannot stamp restart marker: %s", e)


def _code_imports(base_dir):
    """Import the tree in a throwaway process. Returns (ok, error_tail).

    /restart boots whatever is on disk. If that does not import, the bot
    never comes back and there is no bot left to send /restart again - the
    crash loop repeats every RestartSec and only SSH can fix it. So the code
    is imported first, in a child: ~117ms and ~24MB, once, on an explicit
    human command. A traceback is worth paying that for."""
    try:
        proc = subprocess.run([sys.executable, "-c", "import main"], cwd=base_dir,
                              capture_output=True, text=True,
                              timeout=IMPORT_CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "import did not finish within %ds" % IMPORT_CHECK_TIMEOUT
    except (OSError, subprocess.SubprocessError) as e:
        return False, "import check could not run: %s" % e
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr or proc.stdout or "").strip()[-600:]



def _schedule_restart(delay=1):
    """Queue a restart of our own unit from OUTSIDE this service's cgroup.

    `systemctl restart keirai` run from inside the service kills the caller
    mid-job (KillMode=control-group: the restart tears down the cgroup that
    holds the command doing the asking). systemd-run creates a transient
    timer unit - in its own cgroup - which fires the restart `delay` seconds
    later, long enough for the reply to leave. `sudo -n` fails fast instead
    of blocking on a password prompt that nobody can answer."""
    cmd = ["sudo", "-n", "systemd-run", "--collect", "--quiet",
           "--on-active=%d" % delay,
           "--unit=keirai-restart-%d" % int(time.time()),
           "/bin/systemctl", "restart", SERVICE_UNIT]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "systemd-run failed").strip()[:300])


@command("restart", "restart keirai now (main chat only)")
def cmd_restart(cfg, tg, msg, thread_id, state):
    """Restart from Telegram. Main chat only: restarting from inside a topic
    drops you somewhere you did not choose, and a topic-bound session is the
    state most worth not disturbing. History lives in sqlite, so the session
    survives."""
    chat_id = msg["chat"]["id"]
    if thread_id:
        tg.send(chat_id, "restart only works in the main chat - run it in "
                "All messages", thread_id)
        return
    base = config_mod.base_dir(cfg)
    if _restart_pending(base):
        tg.send(chat_id, "a restart is already in progress", thread_id)
        return
    ok, err = _code_imports(base)
    if not ok:
        log.warning("restart refused, code does not import: %s", err[-200:])
        tg.send(chat_id, "not restarting - the code on disk does not import, "
                "and I would not come back:\n<pre>%s</pre>" % html.escape(err),
                thread_id)
        return
    _mark_restart_requested(base)
    try:
        _schedule_restart()
    except Exception as e:  # noqa: BLE001 - reported, never raised at Telegram
        log.warning("restart scheduling failed: %s", e)
        try:
            os.remove(_restart_mark(base))  # do not block the next attempt
        except OSError:
            pass
        tg.send(chat_id, "restart failed: <code>%s</code>" % html.escape(str(e)[:300]),
                thread_id)
        return
    log.info("restart requested from chat=%s thread=%s", chat_id, thread_id)
    # Say it was received - this confirms the command, not the restart. The
    # start notifier does the other half, once the poll loop is actually live.
    tg.send(chat_id, "restarting now - I'll message you when I'm back", thread_id)


@command("compact", "summarize older context now to free space (also runs automatically)")
def cmd_compact(cfg, tg, msg, thread_id, state):
    chat_id = msg["chat"]["id"]
    history = state.get_history(chat_id, thread_id)
    if len(history) < 5:
        tg.send(chat_id, "nothing to compact yet (%d messages)" % len(history), thread_id)
        return
    model_full = state.model_for(chat_id, thread_id, cfg.default_model)
    provider, api_key, model_id = _resolve_provider(cfg, model_full)
    if not api_key:
        _err(tg, msg, thread_id, "no API key for provider '%s'" % provider)
        return
    meta = state.ensure_meta(chat_id, thread_id)
    tg.typing(chat_id, thread_id)
    before_n, before_tok = len(history), _history_tokens(history, cfg)
    cusage = {}
    try:
        folded = _compact(cfg, provider, api_key, model_id, history,
                          _session_header(meta.get("session_id")),
                          usage_out=cusage)
    except Exception as e:
        _err(tg, msg, thread_id, "compaction failed: %s" % e)
        return
    if folded is history:  # nothing older than the checkpoint + the tail
        tg.send(chat_id, "nothing to compact - the checkpoint already covers "
                "everything before the recent turns", thread_id)
        return
    history[:] = folded
    _strip_orphan_tools(history)
    _record_usage(cfg, state, chat_id, thread_id, cusage, provider, model_id)
    state.persist(chat_id, thread_id)
    log.info("manual compact: chat=%s thread=%s %d->%d msgs", chat_id, thread_id,
             before_n, len(history))
    fmt = lambda n: "{:,}".format(int(n))  # noqa: E731 - tiny local helper
    tg.send(chat_id, "compacted: %d -> %d messages (~%s -> ~%s tokens)"
            % (before_n, len(history), fmt(before_tok),
               fmt(_history_tokens(history, cfg))), thread_id)


@command("context", "context window usage of this session (name/id/model, tokens, cost)")
@command("cost", "same report as /context: tokens in/out/cached + total cost")
def cmd_context(cfg, tg, msg, thread_id, state):
    """ONE handler behind BOTH names: /context and /cost describe the same
    session state, so they render the same report (they used to differ - only
    /cost showed the money - which made both feel incomplete)."""
    chat_id = msg["chat"]["id"]
    tg.send(chat_id, _session_report(cfg, state, chat_id, thread_id), thread_id)


def _model_context(cfg, model_full):
    """The model's max context window in tokens (models.dev), or None when
    the cache has no entry for it."""
    provider, _, model_id = (model_full or "").partition("/")
    if not provider or not model_id:
        return None
    try:
        stats = providers.price_for(config_mod.cache_dir(cfg), provider, model_id)
    except Exception:
        return None
    ctx = (stats or {}).get("context")
    try:
        return int(ctx) if ctx else None
    except (TypeError, ValueError):
        return None


def _compact_limit_tokens(cfg, model_full):
    """Auto-compact threshold in tokens.

    Default: 100% of the model's context window (models.dev). An explicit
    context_limit_tokens in config.toml overrides it; without metadata we
    fall back to that value."""
    if cfg.context_limit_explicit:
        return cfg.context_limit_tokens
    ctx = _model_context(cfg, model_full)
    if ctx:
        return ctx
    return cfg.context_limit_tokens


def _session_report(cfg, state, chat_id, thread_id):
    """The single body behind /context and /cost (they are aliases).

    The context line is the model's real window (models.dev, used/max in
    tokens); `used` is the rough estimate of what the next call re-reads -
    system prompt, user text, agent replies and tool use included - and the
    message count covers every stored message (user messages, agent
    responses and tool calls/results alike). Tokens only: no character
    counts and no per-role split - tool use is counted, just not itemised.
    The auto-compact threshold appears only on its own line."""
    key = state.chat_key(chat_id, thread_id)
    history = state.history.get(key) or []
    meta = state.ensure_meta(chat_id, thread_id)
    model = state.model_for(chat_id, thread_id, cfg.default_model)
    name = state.topic_names.get(key) or meta.get("name") or "session"
    fmt = lambda n: "{:,}".format(int(n))  # noqa: E731 - tiny local helper
    ctx_max = _model_context(cfg, model)
    used_tok = _history_tokens(history, cfg)
    if ctx_max:
        pct = min(100, int(round(used_tok * 100.0 / ctx_max)))
        context_line = ("<b>context</b>: ~%s / %s tokens (~%d%%) · %d messages"
                        % (fmt(used_tok), fmt(ctx_max), pct, len(history)))
    else:
        context_line = ("<b>context</b>: ~%s tokens (max unknown - models.dev "
                        "cache empty) · %d messages"
                        % (fmt(used_tok), len(history)))
    limit = _compact_limit_tokens(cfg, model)
    would_compact = used_tok > limit and len(history) > 6
    if cfg.context_limit_explicit:
        at = "%s tokens (context_limit_tokens)" % fmt(limit)
    elif ctx_max:
        at = "100%% of the model window (%s tokens)" % fmt(ctx_max)
    else:
        at = "%s tokens (models.dev cache empty - config fallback)" % fmt(limit)
    lines = [
        "<b>session</b>: %s · <code>%s</code>"
        % (_esc(str(name)), _esc(str(meta.get("session_id") or "?"))),
        "<b>model</b>: <code>%s</code>" % _esc(model),
        context_line,
        "auto-compact: <b>%s</b>" % (
            "would trigger now" if would_compact
            else "not yet - triggers at " + at),
        "<b>tokens</b>: in %s · out %s · cached read %s · cached write %s"
        % tuple("{:,}".format(int(meta.get(k) or 0)) for k in
                ("tokens_in", "tokens_out", "tokens_cached_read",
                 "tokens_cached_write")),
        "<b>cost</b>: $%.6f" % (meta.get("cost_usd") or 0.0),
    ]
    return "\n".join(lines)


def _format_models(models):
    out = []
    for m in models[:30]:
        stats = []
        if m["context"]:
            stats.append("ctx %s" % _human(m["context"]))
        if m["cost_in"] is not None and m["cost_out"] is not None:
            stats.append("$%s/$%s per 1M" % (_fmt_cost(m["cost_in"]), _fmt_cost(m["cost_out"])))
        out.append("- <code>%s</code>%s" % (_esc(m["id"]), " (%s)" % ", ".join(stats) if stats else ""))
    if len(models) > 30:
        out.append("... and %d more" % (len(models) - 30))
    return "\n".join(out)


# ---------------------------------------------------------------- AI chat

def _resolve_provider(cfg, model_full):
    """(provider_name, api_key, model_id) - falls back to the other provider
    (prefer go) when the configured one has no key; keys are endpoint-bound."""
    provider_name, model_id = model_full.split("/", 1)
    api_key = cfg.provider_key(provider_name)
    if not api_key:
        alt = "go" if provider_name != "go" else "zen"
        alt_key = cfg.provider_key(alt)
        if alt_key:
            log.info("no API key for provider '%s' - routing model '%s' via '%s'",
                     provider_name, model_id, alt)
            return alt, alt_key, model_id
    return provider_name, api_key, model_id


def _alternate_provider(cfg, provider_name, model_id):
    """(other_provider, key) when the OTHER provider has a key AND its catalog
    serves the same model id; None otherwise (never blind-retry a model)."""
    alt = "go" if provider_name != "go" else "zen"
    key = cfg.provider_key(alt)
    if not key:
        return None
    try:
        models, _ = providers.cached_models(alt, key, config_mod.cache_dir(cfg))
    except Exception:
        return None  # unknown catalog -> no fallback (spec: iff it serves it)
    return (alt, key) if any(m.get("id") == model_id for m in models) else None


def _record_usage(cfg, state, chat_id, thread_id, usage, provider, model_id):
    """Accumulate one call's tokens + notional cost into the session (P3)."""
    if not usage:
        return
    try:
        price = providers.price_for(config_mod.cache_dir(cfg), provider, model_id)
        cost = providers.usage_cost(price, usage)
    except Exception:
        cost = 0.0
    details = usage.get("prompt_tokens_details") or {}
    tin = int(usage.get("prompt_tokens") or 0)
    cached = int(details.get("cached_tokens") or 0)
    state.add_usage(chat_id, thread_id, cost, {
        "tokens_in": tin,
        "tokens_out": int(usage.get("completion_tokens") or 0),
        "tokens_cached_read": max(0, min(cached, tin)),
        "tokens_cached_write": 0,  # never reported by the endpoints
    })


# ---------------------------------------------------------------- titles
# Two-stage titling (group 19), blending OpenCode's title prompt, Hermes'
# title_generator, oh-my-pi #7306's reject-don't-truncate guard and Pi's
# cheap background naming:
#   stage 1 - instant derived title from the user's first line, applied
#             inline BEFORE the reply (cannot fail, no model call)
#   stage 2 - model upgrade generated CONCURRENTLY with the reply in a
#             daemon thread (HTTP only; the main thread applies the result)
#   provenance derived < llm < user: /rename and /new <name> win forever.

MAX_TITLE_WORDS = 12  # >12 words = the model ANSWERED instead of titling
_TITLE_EXAMPLES = ("Debugging production 500 errors", "Postgres API connection",
                   "Python key rotation", "Weekend trip planning")
_TITLE_ECHO_REJECT = {e.lower() for e in _TITLE_EXAMPLES} | {"code changes"}
_TITLE_SCAN = re.compile(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"')

TITLE_PROMPT = (
    "You are a title generator for chat sessions. You output ONLY a title. "
    "You never answer the message and you never use tools.\n"
    "Rules:\n"
    "- 3 to 7 words, one line, in the same language as the message.\n"
    "- Name what the user wants done, not that they asked a question.\n"
    "- Keep technical terms, filenames, numbers, and error codes exact.\n"
    "- Drop filler words: the, this, my, a, an.\n"
    "- No trailing punctuation, no quotes, no tool names, no 'Title:' "
    "prefix.\n"
    "Good titles: " + "; ".join(_TITLE_EXAMPLES) + ".\n"
    'Too vague: "Code changes".\n'
    'Too long (this answers the message instead of naming it): "Let me '
    'first check the working directory, then write the test plan".\n'
    'Reply with JSON only: {"title": "..."}'
)


def _clean_title(raw):
    """Light hygiene: drop a Title: marker and stray quotes/punctuation.
    No length cap - validation rejects answer-shaped output instead of
    cutting it (oh-my-pi #7306: truncating a blob still leaves a blob)."""
    t = " ".join((raw or "").split())
    if t.lower().startswith("title:"):
        t = t[6:].strip()
    t = t.strip("\"'“”‘’*`").strip()
    return t.strip(" ,.;:!?-–—")


def _derive_title(text):
    """Stage 1: the first meaningful line trimmed at a word boundary
    (~48 chars). Instant, deterministic, no model - it cannot fail."""
    line = next((ln.strip() for ln in (text or "").splitlines()
                 if ln.strip()), "")
    line = " ".join(line.split())
    if len(line) <= 48:
        return line
    cut = line[:48]
    space = cut.rfind(" ")
    line = cut[:space] if space > 24 else cut
    return line.rstrip(" ,.;:!?-–—") + "…"


def _media_placeholder(images):
    if not images:
        return ""
    return "Photo" if images[0][0].startswith("image/") else "Attachment"


def _json_title(payload):
    try:
        obj = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict) and isinstance(obj.get("title"), str):
        return obj["title"].strip()
    return None


def _extract_title(raw):
    """Model output -> title: fenced body, strict JSON, loose scan, then the
    first prose line (a provider ignoring response_format still titles). A
    truncated structured payload is dropped outright - a fragment must never
    become the title (oh-my-pi #7303)."""
    t = (raw or "").strip()
    if not t:
        return ""
    body = t
    if body.startswith("```"):
        nl = body.find("\n")
        body = body[nl + 1:].rsplit("```", 1)[0].strip() if nl != -1 else ""
    title = _json_title(body)
    if title is None and body != t:
        title = _json_title(t)
    if title is None:
        m = _TITLE_SCAN.search(t)
        if m:
            try:
                title = json.loads('"%s"' % m.group(1))
            except ValueError:
                title = m.group(1)
    if title is not None:
        return _clean_title(title)
    if t.startswith(('{"', '["', '[{')) or t.count("```") % 2:
        return ""  # broken/truncated structured output: never title it
    first = next((ln for ln in t.splitlines() if ln.strip()), "")
    return _clean_title(first)


def _title_reject_reason(title):
    """Why a candidate can't be published ('' = fine). REJECT, never
    truncate: an answer-shaped blob stays a blob (oh-my-pi #7306, ported by
    Hermes) - the derived title stays and the next turn may retry."""
    if not title:
        return "empty"
    low = title.lower()
    if ("<function" in low or "<parameter" in low or "antml:" in low
            or "```" in low):
        return "tool-call/code text"
    words = title.split()
    if len(words) > MAX_TITLE_WORDS:
        return "answer-shaped: %d words > %d" % (len(words), MAX_TITLE_WORDS)
    if low in _TITLE_ECHO_REJECT:
        return "prompt example echo"
    return ""


def _apply_title(cfg, tg, state, chat_id, thread_id, title, source):
    """Set the session name with provenance `source` and mirror it onto the
    topic. Main thread only - all SQLite/state writes stay here."""
    meta = state.ensure_meta(chat_id, thread_id)
    meta["name"] = title
    meta["name_src"] = source
    if state.store:
        state.store.set_session_name(chat_id, thread_id or 0, title)
    if thread_id is not None:  # topic mirrors the session name
        try:
            tg.edit_topic(chat_id, thread_id, title)
            state.topic_names[(chat_id, thread_id)] = title
        except Exception as e:
            log.warning("topic title rename failed: %s", e)
    log.info("session titled (%s): chat=%s thread=%s %r",
             source, chat_id, thread_id, title)


def _naming_content(cfg, raw, images):
    """What the titler looks at: the opening text (bounded), with the image
    attached when the model is vision-capable, else a media descriptor."""
    excerpt = (raw or "").strip()[:1500]
    if images:
        vision = False
        try:
            provider, _key, model_id = _resolve_provider(cfg, cfg.default_model)
            stats = providers.price_for(config_mod.cache_dir(cfg),
                                        provider, model_id)
            vision = bool(stats and stats.get("image"))
        except Exception:
            vision = False
        if vision:
            content = [{"type": "text",
                        "text": excerpt or "Name this conversation."}]
            for mime, b64 in images:
                content.append(providers.image_part(mime, b64))
            return content
        return "the user sent a media file (%s)%s" % (
            images[0][0], " - " + excerpt if excerpt else "")
    return excerpt or None


def _maybe_name_session(cfg, tg, state, chat_id, thread_id, msg, images):
    """Group 19, called at turn START - BEFORE the reply is generated.

    The MODEL title call runs in a daemon thread that performs only the
    HTTP request; ai_reply joins it immediately (_finish_naming), so the
    rename lands before the message ever reaches the model. A successful
    run renames exactly once. The derived title is the fallback when the
    model call exceeds TITLE_WAIT or returns faulty output - and it sticks:
    one attempt per session, no retries (a retry would just rename twice).
    Returns the handle, or None.
    """
    meta = state.ensure_meta(chat_id, thread_id)
    if meta.get("name"):
        return None  # one attempt per session: model title or its fallback
    raw = (msg.get("text") or msg.get("caption") or "").strip()
    content = _naming_content(cfg, raw, images)
    if content is None:
        return None
    provider, api_key, model_id = _resolve_provider(cfg, cfg.default_model)
    if not api_key:
        return None
    meta["_naming"] = True
    handle = {"usage": {}, "provider": provider, "model": model_id,
              "result": [None], "thread": None,
              "raw": raw, "images": images}
    messages = [{"role": "system", "content": TITLE_PROMPT},
                {"role": "user", "content": content}]

    def work():
        """Daemon thread: one HTTP call, writes only its own dict."""
        try:
            out, _r, _t = providers.chat_title(
                provider, api_key, model_id, messages,
                session_id=_session_header(meta.get("session_id")),
                usage_out=handle["usage"])
            handle["result"][0] = out
        except Exception as e:
            log.warning("session naming failed: %s", e)

    thread = threading.Thread(target=work, name="title", daemon=True)
    handle["thread"] = thread
    thread.start()
    return handle


def _finish_naming(cfg, tg, state, chat_id, thread_id, handle,
                   timeout=TITLE_WAIT):
    """Join the naming thread (bounded) and apply its result, BEFORE the
    reply starts. Success = exactly one rename (the model title). Past
    `timeout` - or on faulty output - the derived title is the fallback:
    the session's single attempt is spent, so the visible name never
    churns, but it always exists before the model is called. Abandoning a
    live thread is race-free: it only writes its own dict, and _naming is
    always cleared here."""
    thread = handle.get("thread")
    reason = None
    if thread is not None:
        thread.join(timeout)
        if thread.is_alive():
            reason = "timeout after %.0fs" % timeout
    meta = state.ensure_meta(chat_id, thread_id)
    meta.pop("_naming", None)
    if handle.get("usage"):
        _record_usage(cfg, state, chat_id, thread_id, handle["usage"],
                      handle["provider"], handle["model"])
    out = handle["result"][0]
    title = _extract_title(out) if out else ""
    reason = reason or _title_reject_reason(title)
    if reason:
        log.info("model title not used (%s): chat=%s thread=%s %r",
                 reason, chat_id, thread_id, title)
        if not meta.get("name"):  # stage 1 fallback, applied once
            derived = (_derive_title(handle.get("raw"))
                       or _media_placeholder(handle.get("images")))
            if derived:
                _apply_title(cfg, tg, state, chat_id, thread_id, derived,
                             "derived")
        return False
    _apply_title(cfg, tg, state, chat_id, thread_id, title, "llm")
    return True


def _est_tokens(text):
    """Rough token count of a string - no tokenizer dependency.

    [A-Za-z0-9] runs at the usual ~4 chars/token; every other character
    (spaces, punctuation, newlines, CJK, emoji, control chars) counts as
    1 token each, which is roughly what byte-level BPE charges for it.
    Deliberately errs high: auto-compact then fires early, never late.
    The message framing (role keys, JSON envelope) is not counted."""
    alnum = sum(1 for ch in text if ch.isascii() and ch.isalnum())
    return (alnum + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN + (len(text) - alnum)


def _msg_tokens(m):
    """Tokens a single history message contributes to the prompt."""
    n = 0
    c = m.get("content")
    if isinstance(c, str):
        n += _est_tokens(c)
    elif isinstance(c, list):
        for part in c:
            if isinstance(part, dict) and part.get("type") == "text":
                n += _est_tokens(part.get("text", ""))
    for call in m.get("tool_calls") or []:  # ride on the assistant message
        f = call.get("function") or {}
        n += _est_tokens(str(f.get("name") or ""))
        n += _est_tokens(str(f.get("arguments") or ""))
    # reasoning rides in context too (assistant messages): count it, or
    # auto-compact fires late and the request overruns the real window
    r = m.get("reasoning_content")
    if isinstance(r, str):
        n += _est_tokens(r)
    return n


def _history_tokens(history, cfg=None):
    """Everything the model re-reads from this history, in rough tokens.

    Tool use is context too and used to be invisible: a call's name + JSON
    arguments ride on the assistant message (never in `content`) and the
    result is its own role:"tool" message - both are sent to the model, so
    both are counted here, as is `reasoning_content` when a round's reasoning
    is kept in context. With cfg, the system prompt is counted as well:
    it rides on every call."""
    n = sum(_msg_tokens(m) for m in history)
    if cfg is not None:
        # SYSTEM.md - re-read like the request itself, so /context and the
        # auto-compact threshold count what will actually be sent
        n += _est_tokens(config_mod.system_prompt(cfg))
    return n


SUMMARY_LABEL = "[Summary of earlier conversation]"

# opencode's compaction strategy, ported (MIT - see THIRD_PARTY_NOTICES.md):
# a checkpoint is "summary + the newest N tokens of the real conversation",
# never a summary of everything. A second compaction updates the first
# summary as an anchor instead of summarizing its own output. Nothing here
# deletes: the store is append-only, so a fold only moves the visible window
# (sessions.Store.save_session) and the raw stays below it.
COMPACT_KEEP_TOKENS = 15_000  # recent conversation kept verbatim beside the summary

COMPACT_SECTIONS = """\
Structure the summary with these EXACT sections, in this order:

## Goal
One sentence: what the user is ultimately trying to accomplish.

## Constraints & Preferences
Rules, constraints and preferences the user stated - style, naming,
approaches to avoid, and anything about how the work must be done.

## Progress
### Done
### In Progress
### Blocked

## Key Decisions
Decisions made, and why.

## Next Steps
What comes next, in order.

## Critical Context
Facts needed to continue: versions, ids, error strings, measured numbers.

## Relevant Files
`path` - one line on why it matters.

Rules:
- Keep every section, even when it is empty.
- Terse bullets, never prose paragraphs.
- Preserve exact file paths, commands, error strings, numbers and identifiers
  verbatim whenever they are known.
- Do not mention this summary or that context was compacted.
- Output only the summary."""

COMPACT_FRESH = ("Write a detailed summary of the conversation below. It "
                 "replaces that conversation, so the next turn has to be able "
                 "to continue from it alone.\n\n" + COMPACT_SECTIONS)

COMPACT_UPDATE = ("Update the anchored summary below with the conversation that "
                  "follows it. Keep every fact that is still true, drop what is "
                  "now stale or wrong, and merge in what is new: one summary, "
                  "not two.\n\n<anchored_summary>\n%s\n</anchored_summary>\n\n"
                  + COMPACT_SECTIONS)


def _is_summary_marker(m):
    c = m.get("content") if isinstance(m, dict) else None
    return isinstance(c, str) and c.startswith(SUMMARY_LABEL)


def _compact_keep_tokens(cfg, provider, model_id):
    """Recent conversation kept verbatim, in tokens. Bounded: the tail has to
    leave the summary and the next turn room, so it cannot be a fixed share
    of a window that keeps growing (opencode v1 clamps its quarter-of-window
    rule to 2k..15k for the same reason)."""
    ctx = _model_context(cfg, "%s/%s" % (provider, model_id)) or cfg.context_limit_tokens
    return max(2_000, min(COMPACT_KEEP_TOKENS, int(ctx * 0.10)))


def _compact_tail_start(history, budget):
    """Index where the verbatim tail begins.

    A turn opens at a user message; the tail is the newest whole turns whose
    messages fit `budget` tokens. The newest turn is kept whatever its size -
    a summary with no recent turns beside it is the thing this replaces."""
    starts = [i for i, m in enumerate(history) if m.get("role") == "user"]
    if not starts:
        return len(history)  # no turn boundary to cut on: fold nothing
    total, start = 0, len(history)
    for s in reversed(starts):
        cost = sum(_msg_tokens(m) for m in history[s:start])
        if start < len(history) and total + cost > budget:
            break
        total += cost
        start = s
    return start


def _compact(cfg, provider, api_key, model_id, history, session_id,
             usage_out=None):
    """Summarize the older part of history, keep the newest turns verbatim.

    Two properties matter, and both were missing when `keep` was a message
    count and every compaction summarized the whole history:
    - the tail is a TOKEN budget cut on a turn boundary (COMPACT_KEEP_TOKENS),
      so recent detail survives verbatim and no round is half-kept;
    - a second compaction UPDATES the existing summary as an anchor instead of
      summarizing its own summary, so detail does not decay per generation
      (a summary of a summary of a summary keeps the shape and loses the facts).
    Returns the new message list; the same list when there is nothing to fold."""
    start = _compact_tail_start(history, _compact_keep_tokens(cfg, provider, model_id))
    if start >= len(history):
        return history  # no turn boundary to cut on: nothing to fold
    if start == 0:
        # The budget swallows every turn (a small session, or a small window):
        # keep the newest turn verbatim and fold the rest - a compaction that
        # makes no progress would fire again on the very next turn.
        starts = [i for i, m in enumerate(history) if m.get("role") == "user"]
        if len(starts) < 2:
            return history
        start = starts[-1]
    tail = list(history[start:])
    head = history[:start]
    anchored = ""
    if head and _is_summary_marker(head[0]):
        anchored = head[0]["content"][len(SUMMARY_LABEL):].strip()
        # the checkpoint's own "Understood." follows the summary
        head = head[1:]
        if head and head[0].get("role") == "assistant":
            head = head[1:]
    if not head:
        return history
    # The summary request is built from a text transcript (never raw
    # messages), but a session with no message cap could still make that
    # transcript too big for the window - keep the newest messages first.
    ctx = _model_context(cfg, "%s/%s" % (provider, model_id)) or cfg.context_limit_tokens
    budget = max(8_000, ctx * CHARS_PER_TOKEN // 8)
    transcript, size = [], 0
    for m in reversed(head):
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict)
                         and p.get("type") == "text")
        c = c or ""
        # Tool calls ride on the assistant message, never in `content`.
        # Left out, the summary keeps results with no command behind them:
        # "tool: exit code 0" and no idea what ran. Content is halved when
        # calls are present so the two fit the same 4000-char line cap.
        calls = "; ".join(
            "%s %s" % ((f := tc.get("function") or {}).get("name") or "?",
                       (f.get("arguments") or "")[:CALL_ARG_CHARS])
            for tc in m.get("tool_calls") or [])
        text = ("%s\n[calls: %s]" % (c[:2000], calls[:CALL_BLOCK_CHARS])
                if calls else c)
        line = "%s: %s" % (m.get("role", "?"), text[:4000])
        if transcript and size + len(line) > budget:
            break
        transcript.append(line)
        size += len(line)
    transcript.reverse()
    prompt = (COMPACT_UPDATE % anchored) if anchored else COMPACT_FRESH
    prompt += "\n\n<conversation>\n" + "\n".join(transcript) + "\n</conversation>"
    summary, _reasoning, _tcs = providers.chat(provider, api_key, model_id,
                                               [{"role": "user", "content": prompt}],
                                               session_id=session_id,
                                               usage_out=usage_out)
    # this fold deletes nothing: the raw conversation stays in
    # session_messages below base_seq (sessions.Store.save_session)
    marker = [
        {"role": "user", "content": SUMMARY_LABEL + "\n" + summary},
        {"role": "assistant", "content": "Understood. Continuing with that context."},
    ]
    return marker + tail


def _close_tool_rounds(history):
    """Answer any tool_calls left unanswered (turn ended mid-round: /stop,
    provider error). Providers reject a history whose tool_calls have no
    matching tool result, so unanswered calls get a placeholder result -
    the round stays in context either way (user directive)."""
    answered = {m.get("tool_call_id") for m in history if m.get("role") == "tool"}
    missing = []
    for m in history:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                if tc.get("id") not in answered:
                    missing.append(tc.get("id"))
    for tid in missing:
        history.append({"role": "tool", "tool_call_id": tid,
                        "content": "[not executed: the turn ended before this "
                                   "call ran]"})


def _strip_orphan_tools(history):
    """Front-trimming can cut an assistant/tool_calls pair in half, leaving
    leading tool messages whose call was trimmed away - the API rejects
    those. Drop them (their assistant is already summarized/gone)."""
    while history and history[0].get("role") == "tool":
        history.pop(0)


def _prompt(history):
    """History as handed to the API: ALL of it - there is no message cap any
    more, auto-compact alone bounds size (send the whole session, summarize
    when it gets big). One exception: base64 images. Re-sending every picture
    ever received would push each request past provider image/size caps and
    re-bill image tokens on every call, so only the newest IMAGE_KEEP
    image-bearing messages keep theirs; older pictures become a marker in the
    PROMPT only - stored history is untouched. Returns `history` itself when
    there is nothing to drop (the common case)."""
    marker = ("[image omitted - only the newest %d stay in context]"
              % IMAGE_KEEP)
    keep, out = IMAGE_KEEP, None
    for i in range(len(history) - 1, -1, -1):
        parts = history[i].get("content")
        if not isinstance(parts, list) or not any(
                isinstance(p, dict) and p.get("type") == "image_url"
                for p in parts):
            continue
        if keep:
            keep -= 1
            continue
        if out is None:
            out = list(history)  # shallow: stored messages keep their images
        fresh, marked = [], False
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "image_url":
                if not marked:
                    fresh.append({"type": "text", "text": marker})
                    marked = True
                continue
            fresh.append(p
            )
        msg = dict(history[i])
        msg["content"] = fresh
        out[i] = msg
    return history if out is None else out


def _store_reasoning(msg, reasoning):
    """Attach a round's reasoning to an assistant message that goes into
    context. Only the TAIL is kept: reasoning is display-length (thousands
    of characters) and every stored message is re-sent on every call, so an
    unbounded copy would re-bill the same tokens on each request forever.
    The tail is where the conclusions are.

    The key is ALWAYS written - as "" when there is no reasoning - because
    the DeepSeek provider rejects a history where the key is present on some
    assistant messages and absent on others (see _ensure_reasoning)."""
    r = (reasoning or "").strip()
    msg["reasoning_content"] = r[-REASONING_KEEP_CHARS:] if r else ""
    return msg


def _unavailable(name, declared):
    """What the model is told when it calls a tool this request never
    offered - a hallucinated name, one turned off in config, or markup it
    merely QUOTED while describing a format. Answering rather than silently
    skipping keeps the round valid (one tool result per call, or the API
    rejects the history) and lets the model correct itself."""
    if declared:
        return ("[tool unavailable: %s - this session offers only: %s]"
                % (name, ", ".join(sorted(declared))))
    return ("[tool unavailable: %s - no tools are enabled in this session]"
            % name)


def _ensure_reasoning(messages):
    """Every assistant message must CARRY a `reasoning_content` key for the
    DeepSeek provider (opencode-go), or the request is refused:
    "The `reasoning_content` in the thinking mode must be passed back to the
    API." - it rejects a history where some assistant messages have the key
    and others do not. An empty string satisfies it, so a message that never
    thought carries "" (measured on go/deepseek-v4.1-flash; mimo-v2.6-flash
    accepts both shapes). Mutates in place - called on the outgoing list,
    which shares dicts with stored history, so a session written before this
    rule is repaired on its next request rather than rewritten."""
    for m in messages:
        if m.get("role") == "assistant":
            m.setdefault("reasoning_content", "")


def _thinking_html(text):
    return ("<details><summary>\U0001f914 Thinking</summary>%s</details>"
            % text)


def _live_new(show, draft, draft_id=0):
    """Live progress state for one turn:
      msg_id    the thinking block - ONE message per round, edited in place
                from the first reasoning delta until the round ends
      answer_id the streamed answer's own message (edit mode), right below
                the thinking so the thinking stays readable
      role      slot -> "thinking" | "answer": what that message holds, so
                the final delivery can never overwrite one with the other
      draft     the answer streams through sendRichMessageDraft instead
                (no answer message exists until the final send)
    """
    return {"msg_id": None, "answer_id": None, "role": dict(),
            "thinking": bool(show), "on": True, "draft": bool(draft),
            "draft_id": draft_id}


def _edit_or_send(tg, live, slot, chat_id, thread_id, role=None, **src):
    """Edit a live slot's message in place, sending it on the first flush.
    `role` is recorded when the message is created (see _live_new)."""
    if live.get(slot):
        tg.edit_rich(chat_id, live[slot], **src)
    else:
        live[slot] = tg.send_rich(chat_id, thread_id=thread_id, **src)["message_id"]
        if role:
            live.setdefault("role", {})[slot] = role


def _drop_live(tg, live, chat_id, keep_thinking=False):
    """Forget the round's live messages and clean up the progress views.

    keep_thinking: the round's thinking block was just made permanent (it is
    the SAME message the thinking streamed into), so only the slot is
    cleared and the message stays - the next round streams into fresh
    messages BELOW the tool lines. Interrupts pass keep_thinking=False:
    every partial progress view goes away."""
    if not live:
        return
    roles = live.setdefault("role", {})
    for slot in ("msg_id", "answer_id"):
        mid = live.pop(slot, None)
        role = roles.pop(slot, None)
        if not mid:
            continue
        if keep_thinking and role == "thinking":
            continue  # permanent block - stays in the chat
        try:
            tg.delete_message(chat_id, mid)
        except Exception:
            pass


def _keep_midturn_answer(tg, live, chat_id, thread_id, answer, cfg):
    """Publish a round's prose when the round turns out to call tools.

    The model often writes a line of narration before acting ("I'll check the
    log first"), and that text used to reach the chat only as a streaming
    side effect: in draft mode it was an ephemeral preview, and in edit mode
    _drop_live DELETED the message when the tool line appeared - so the user
    saw the sentence flash and disappear. Measured 2026-11-08: 74 of the 423
    tool-calling rounds in the store carry such prose.

    Kept in place when the round already streamed it into a real message
    (final text written, slot detached so _drop_live leaves it alone);
    published for real in draft mode, where the preview is all there was."""
    text = textcalls.sanitize((answer or "").strip())
    if not text:
        return
    live = live or {}
    roles = live.setdefault("role", {})
    for slot in ("answer_id", "msg_id"):
        if roles.get(slot) == "answer" and live.get(slot):
            mid = live.pop(slot)
            roles.pop(slot, None)
            try:
                if len(text) <= md2tg.RICH_LIMIT:
                    tg.edit_rich(chat_id, mid, markdown=text)
                else:  # too long for one rich message: re-send as parts
                    tg.delete_message(chat_id, mid)
                    for part in md2tg.send_parts(text):
                        tg.send(chat_id, part, thread_id)
            except tg_mod.TelegramError as e:
                log.warning("could not finalize mid-turn message: %s", e)
            return
    if cfg.rich_messages:
        try:
            _send_rich_answer(tg, chat_id, thread_id, text, None)
            return
        except tg_mod.TelegramError as e:
            log.warning("mid-turn rich send failed, using plain messages: %s", e)
    for part in md2tg.send_parts(text):
        tg.send(chat_id, part, thread_id)


class _ToolHeartbeat:
    """One chat message that says a long tool call is still alive.

    Silent until the call has run a minute (tools.HEARTBEAT_S), then it posts
    "\u23f3 bash still running - 1m 00s" and EDITS that same message every
    minute after, so a 200-second submission reads as work in progress
    instead of a dead bot. When the call returns, the message is finalized
    with how long it took - "crashed" and "slow" must never look alike.

    Purely cosmetic: every Telegram error is swallowed, and a call that
    finishes in less than a minute sends nothing at all.
    """

    def __init__(self, tg, chat_id, thread_id, name, preview):
        self.tg, self.chat_id, self.thread_id = tg, chat_id, thread_id
        self.name, self.preview = name, preview
        self.msg_id = None
        self.started = time.time()

    @staticmethod
    def _fmt(secs):
        secs = int(secs)
        return ("%dm %02ds" % (secs // 60, secs % 60)) if secs >= 60 else ("%ds" % secs)

    def _text(self, secs, done=False):
        if done:
            return "\u2705 <code>%s</code> finished in %s" % (
                _esc(self.name), self._fmt(secs))
        return ("\u23f3 <code>%s</code> still running - %s\n<code>%s</code>"
                % (_esc(self.name), self._fmt(secs), _esc(self.preview[:160])))

    def tick(self, elapsed=None):
        """First beat creates the message; later beats edit it in place."""
        # the tool's own clock when it gives one (it starts before we do)
        secs = float(elapsed) if elapsed is not None else time.time() - self.started
        self.elapsed = secs
        try:
            if self.msg_id is None:
                self.msg_id = self.tg.send(self.chat_id, self._text(secs),
                                           self.thread_id)["message_id"]
            else:
                self.tg.edit_text(self.chat_id, self.msg_id, self._text(secs))
        except Exception:  # cosmetic only - never fail a tool call over it
            pass

    def done(self):
        if self.msg_id is None:  # fast call - nothing was ever shown
            return
        secs = max(getattr(self, "elapsed", 0), time.time() - self.started)
        try:
            self.tg.edit_text(self.chat_id, self.msg_id,
                              self._text(secs, done=True))
        except Exception:
            pass


def _stream_draft(tg, live, chat_id, thread_id, answer):
    """Modern streaming API: sendRichMessageDraft (Bot API 10.1) streams the
    answer as an animated preview in the chat - real-time, no message to
    churn. Re-sending the same draft_id animates it; Telegram's own Stop
    button (can_stop) arrives as a stopped_message_generation update. The
    preview is ephemeral (~30s), so the final answer is sent for real by
    _send_rich_answer. Any error -> back to in-place edits."""
    try:
        tg.send_rich_draft(chat_id, live["draft_id"], markdown=answer,
                           thread_id=thread_id, can_stop=True)
    except tg_mod.TelegramError as e:
        log.warning("draft stream failed (%s) - falling back to edits", e)
        live["draft"] = False


def _flush_live(tg, live, chat_id, thread_id, reasoning, answer, show_reasoning):
    """Push current partial state to Telegram.

    Thinking: exactly ONE block per round - created on the first reasoning
    delta (before any content) and edited in place for the rest of the turn,
    including reasoning that arrives between answer chunks (interleaved
    thinking), so the block never duplicates or freezes half-finished.

    Answer: draft in private chats (sendRichMessageDraft), otherwise the
    answer's own message edited in place below the thinking (nothing to
    think about -> the answer takes the first slot)."""
    roles = live.setdefault("role", {})
    if reasoning and live.get("thinking") and (
            roles.get("msg_id") == "thinking"
            or (show_reasoning and not live.get("msg_id"))):
        try:
            _edit_or_send(tg, live, "msg_id", chat_id, thread_id, role="thinking",
                          html=_thinking_html(_esc(reasoning[-2000:])))
        except tg_mod.TelegramError as e:
            if not tg_mod.is_not_modified(e):
                raise  # real failure -> _stream_answer disables live updates
            # unchanged content = the block already shows this exact text
    if answer and answer.strip() and len(answer) <= md2tg.RICH_LIMIT:
        if live.get("draft"):
            _stream_draft(tg, live, chat_id, thread_id, answer)
        else:
            slot = "answer_id" if live.get("msg_id") else "msg_id"
            try:
                _edit_or_send(tg, live, slot, chat_id, thread_id, role="answer",
                              markdown=answer)
            except tg_mod.TelegramError as e:
                if not tg_mod.is_not_modified(e):
                    raise
                # same reasoning as above: a no-op is not a failure, and it
                # must not stop the thinking slot from flushing either


def _stream_answer(tg, msg, live, stop, provider, api_key, model_id, messages,
                   session_id, tools=None, thread_id=None, usage_out=None):
    """Stream one provider round with live progress.
    Returns (answer, reasoning, tool_calls). Raises on provider failure.
    `stop()` is checked every delta so /stop cuts generation short."""
    chat_id = msg["chat"]["id"]
    answer, reasoning = "", ""
    show_reasoning = True  # until first content token arrives
    last_flush = 0.0
    for kind, delta in providers.chat_stream(provider, api_key, model_id, messages,
                                             session_id=session_id, tools=tools,
                                             usage_out=usage_out):
        if kind == "tool_calls":
            return answer, reasoning, delta
        if kind == "reasoning":
            # reasoning ALWAYS accumulates as reasoning: a model that keeps
            # thinking between answer chunks must not leak thought text into
            # the answer (that used to look like a second thinking block)
            reasoning += delta
        else:
            show_reasoning = False  # first content token ends the thinking
            answer += delta
        if stop and stop():
            log.info("stream cut short by /stop")
            break
        now = time.time()
        if live.get("on") and now - last_flush > 1.5:
            last_flush = now
            try:
                _flush_live(tg, live, chat_id, thread_id, reasoning, answer,
                            show_reasoning)
            except tg_mod.TelegramError as e:
                if tg_mod.is_not_modified(e):
                    continue  # same content as last flush - not a failure
                log.warning("live updates disabled: %s", e)
                live["on"] = False
    return answer, reasoning, None


def _fmt_ts(epoch):
    """UTC stamp for the clock block: 2026-10-22 10:14:03 UTC."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch)) + " UTC"


def _fmt_ago(seconds):
    """Elapsed time, compact: 7s / 12m 3s / 4h 9m / 3d 6h."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm %ds" % (seconds // 60, seconds % 60)
    if seconds < 86400:
        return "%dh %dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd %dh" % (seconds // 86400, (seconds % 86400) // 3600)


def _clock_block(state, chat_id, thread_id, now=None):
    """The [time] block appended to the system prompt of every request.

    The model has no clock and no awareness across turns: it cannot tell a
    message that arrived seconds after its last reply from one that arrived a
    day later. "now" plus "when you last worked on this session" (the last
    reply, or the last tool call of a run the user cut short) lets it reason
    about the gap - whether something it started has had time to finish, why
    context may be stale, why the user is pinging.

    Built once per turn, before this turn's own first persist(), so it always
    describes the END OF THE PREVIOUS TURN rather than mid-turn work.
    """
    now = time.time() if now is None else now
    last = state.last_activity(chat_id, thread_id)
    if last:
        when = "%s - %s ago" % (_fmt_ts(last), _fmt_ago(now - last))
    else:
        when = "none (first message in this session)"
    return ("[time]\n"
            "now: %s\n"
            "your last activity in this session: %s\n"
            "(the gap above is how long this session sat idle before this "
            "message)" % (_fmt_ts(now), when))


CLOCK_NOTE = ("above is a machine-stamped clock, not a message to act on: it is "
              "refreshed on every request, so it is the moment THIS request was "
              "sent")

def _clock_msg(state, chat_id, thread_id):
    """The clock as a TRAILING user message, rebuilt on every round.

    Where it sits is a cache decision. Measured 2026-11-12 on
    go/deepseek-v4.1-flash, 3 reps of a 1297-token prompt, comparing two
    independent requests (a turn boundary): a one-second change in the SYSTEM
    prompt cached 0/1297 tokens, the same change at the tail cached 1152/1297
    (88%). Rounds within one turn match either way - a turn shares one system
    prompt - so the system prompt keeps byte-identical for the life of the
    config and the clock rides at the end. Rebuilding it per round is a
    separate fix: a 56-round run must not reason about a clock frozen at turn
    start."""
    return {"role": "user",
            "content": "%s\n(%s)" % (_clock_block(state, chat_id, thread_id),
                                      CLOCK_NOTE)}


INTERRUPT_NOTE = ("[This run was interrupted by the user before it finished - "
                  "any running command/tool did not complete.]")


def _injected_user_msg(upd):
    """A plain message sent while a turn was running: folded into context at
    the next tool boundary so the user can steer the work without /stop."""
    m = upd["message"]
    text = m.get("text") or ""
    reply_ctx = _reply_context(m)
    if reply_ctx:
        text = reply_ctx + "\n\n" + text
    return {"role": "user",
            "content": "[sent while you were working on this turn - take it "
                       "into account and continue]\n" + text}


def _send_thinking_block(tg, chat_id, thread_id, reasoning_md, cfg, live=None):
    """Persist ONE round's reasoning as its own collapsible block. Every
    completed round gets one, so the user can watch the model think round by
    round (and steer mid-turn) instead of seeing only the last block after
    the turn ends.

    When the thinking streamed live, the SAME message is edited into its
    final form - never a second block next to the half-streamed one. Only
    chunks past RICH_LIMIT (or no live block at all) need extra messages.
    Cosmetic: never lets a send failure break the turn."""
    roles = (live or {}).get("role") or {}
    live_mid = ((live or {}).get("msg_id")
                if roles.get("msg_id") == "thinking" else None)
    chunks = [c for c in md2tg.split(reasoning_md, md2tg.RICH_LIMIT)
              if c.strip()]
    if not chunks:  # nothing worth showing (caller usually guards already)
        return
    sent = False
    if cfg.rich_messages:
        try:
            if live_mid:
                tg.edit_rich(chat_id, live_mid,
                             html=_thinking_html(chunks[0]))
                # the streamed message IS the block now - release the slot
                live["msg_id"] = None
                roles.pop("msg_id", None)
                for chunk in chunks[1:]:
                    tg.send_rich(chat_id, html=_thinking_html(chunk),
                                 thread_id=thread_id)
                sent = True
            else:
                for chunk in chunks:
                    tg.send_rich(chat_id, html=_thinking_html(chunk),
                                 thread_id=thread_id)
                sent = True
        except tg_mod.TelegramError as e:
            if tg_mod.is_not_modified(e) and live_mid:
                # the streamed text already matches the final block - the
                # no-op edit IS the finished block, nothing to fall back to
                live["msg_id"] = None
                roles.pop("msg_id", None)
                sent = True
            else:
                log.warning("rich thinking block failed (%s) - falling back", e)
        except Exception as e:
            log.warning("rich thinking block failed (%s) - falling back", e)
    if not sent:
        try:
            for part in md2tg.send_parts("", reasoning_md):
                if part.strip():
                    tg.send(chat_id, part, thread_id)
        except Exception as e:
            log.warning("thinking block failed: %s", e)


def ai_reply(cfg, tg, msg, thread_id, state, text, images=None):
    chat_id = msg["chat"]["id"]
    key = state.chat_key(chat_id, thread_id)
    state.begin_turn(key)
    try:
        # THE RENAME COMES FIRST: the session is titled (model title with a
        # bounded wait, derived title as the fallback) BEFORE the message is
        # handed to the model, so the reply is generated under the name the
        # chat already shows - not renamed afterwards.
        naming = _maybe_name_session(cfg, tg, state, chat_id, thread_id, msg,
                                     images)
        if naming is not None:
            _finish_naming(cfg, tg, state, chat_id, thread_id, naming)
        _ai_reply(cfg, tg, msg, thread_id, state, text, images)
    finally:
        state.end_turn(key)


def _ai_reply(cfg, tg, msg, thread_id, state, text, images=None):
    chat_id = msg["chat"]["id"]
    key = state.chat_key(chat_id, thread_id)

    def stop():
        return state.interrupted(key)

    model_full = state.model_for(chat_id, thread_id, cfg.default_model)
    provider_name, api_key, model_id = _resolve_provider(cfg, model_full)
    if not api_key:
        _err(tg, msg, thread_id, "no API key for provider '%s' (or fallback 'go'/'zen')"
             % provider_name)
        return
    # one hashed id per keirai session: provider routing and the prompt
    # cache pin to it, and a new session must not reuse another's bucket
    session_id = _session_header(
        state.ensure_meta(chat_id, thread_id).get("session_id"))
    switched = {"on": False}

    def pcall(messages, tools=None):
        """Provider call with Go-first error fallback (group 25): on
        ProviderError, retry once via the other provider when it has a key
        AND its catalog serves this model id. Usage is recorded on success."""
        nonlocal provider_name, api_key
        while True:
            usage = {}
            try:
                result = providers.chat(provider_name, api_key, model_id,
                                        messages, session_id=session_id,
                                        tools=tools, usage_out=usage)
            except providers.ProviderError as e:
                alt = (None if switched["on"]
                       else _alternate_provider(cfg, provider_name, model_id))
                if alt is None:
                    raise
                switched["on"] = True
                log.warning("provider '%s' failed (%s) - retrying via '%s'",
                            provider_name, e, alt[0])
                provider_name, api_key = alt
                continue
            _record_usage(cfg, state, chat_id, thread_id, usage,
                          provider_name, model_id)
            return result

    content = [{"type": "text", "text": text or "Describe the image."}]
    for mime, b64 in images or []:
        content.append(providers.image_part(mime, b64))
    user_msg = {"role": "user", "content": content if images else text}

    history = state.get_history(chat_id, thread_id)
    history.append(user_msg)

    # auto-compact at the threshold (default: 100% of the model's window)
    if not stop() and len(history) > 6 and _history_tokens(history, cfg) \
            > _compact_limit_tokens(cfg, model_full):
        before = len(history)
        cusage = {}
        try:
            history[:] = _compact(cfg, provider_name, api_key, model_id, history,
                                  session_id, usage_out=cusage)
            _strip_orphan_tools(history)
            _record_usage(cfg, state, chat_id, thread_id, cusage,
                          provider_name, model_id)
            log.info("auto-compact: chat=%s thread=%s %d -> %d msgs",
                     chat_id, thread_id, before, len(history))
        except Exception as e:
            log.warning("auto-compact failed (%s)", e)
            ctx = _model_context(cfg, model_full)
            if ctx and _history_tokens(history, cfg) > ctx:
                # Summarizing failed AND the window is really full: such a
                # session could never be sent again. Drop the oldest until it
                # fits with room to spare. Last resort only - normal operation
                # has no message cap at all (auto-compact bounds size).
                half = ctx // 2
                while len(history) > COMPACT_FALLBACK_KEEP and \
                        _history_tokens(history, cfg) > half:
                    del history[:max(1, len(history) // 2)]
                _strip_orphan_tools(history)
                log.warning("session trimmed to its newest %d messages (no "
                            "room in the %d-token window)", len(history), ctx)

    _close_tool_rounds(history)  # safety net: no dangling tool_calls ever
    # the WHOLE session goes to the model - no message cap (auto-compact bounds size)
    # system prompt = SYSTEM.md, re-read on every request so an edit lands on
    # the very next message. It must stay byte-identical across turns: a clock
    # in here changes at every turn boundary, so the cross-turn prefix never
    # matches (measured: 0 of 1297 tokens cached when the timestamp moved 1s).
    # Rounds INSIDE a turn still hit - which is why the old placement looked
    # healthy at a 90% cache rate while every turn started cold, and why this
    # is easy to "fix" back by accident. The [time] block rides at the END of
    # each request instead (_clock_msg), where a 1s change costs ~31 tokens.
    sys_text = config_mod.system_prompt(cfg)
    messages = ([{"role": "system", "content": sys_text}]
                if sys_text else []) + _prompt(history)
    tool_specs = tools_mod.available_specs(cfg)
    # the tool names THIS request offers - the guard for calls parsed out of
    # text, which cannot distinguish a real call from markup the model merely
    # quoted while describing a format
    declared = {s.get("function", {}).get("name") for s in (tool_specs or [])}
    # session_id is the hashed per-session provider header (see
    # _session_header); spill_session is this session's own id -> spill files land in
    # sessions/<session-id>/tool_output/
    ctx = {"session_id": session_id, "interrupt": stop,
           "spill_session": state.ensure_meta(chat_id, thread_id).get("session_id")}
    show = state.thinking_on(msg["from"]["id"])  # stable: /thinking queues
    live = None
    if cfg.rich_messages and cfg.stream_drafts:
        # Live streaming: the thinking block is streamed edit-in-place; the
        # answer streams as a sendRichMessageDraft preview in private chats
        # (stream_mode = "draft") or through edits of a message of its own
        # below it (groups, or stream_mode = "edit"). Drafts never touch the
        # user's composer, so they keep typing and sending mid-turn (folded
        # in at the next tool boundary) and between turns.
        draft_ok = (cfg.stream_mode == "draft"
                    and (msg.get("chat") or {}).get("type") == "private")
        live = _live_new(show, draft_ok,
                         draft_id=secrets.randbelow(2 ** 31 - 1) + 1)

    reasoning, answer = None, None
    interrupted = False
    rounds = 0
    while True:  # unbounded: runs until the model stops calling tools
        # fold in messages that arrived mid-turn: they were sent after the
        # previous tool use finished, so the model sees them right now and
        # can be steered without interrupting the run
        for extra in state.take_inbox(key):
            injected = _injected_user_msg(extra)
            history.append(injected)
            messages.append(injected)
            log.info("mid-turn message folded into context: chat=%s thread=%s",
                     chat_id, thread_id)
        if stop():
            interrupted = True
            break
        # fresh clock on every round: a 56-round run must not believe it is
        # still the moment the turn began (the system prompt stays untouched,
        # so the conversation prefix keeps caching)
        prompt = messages + [_clock_msg(state, chat_id, thread_id)]
        _ensure_reasoning(prompt)  # DeepSeek refuses a history missing the key
        tcs, got = None, False
        if live:
            susage = {}
            try:
                answer, reasoning, tcs = _stream_answer(
                    tg, msg, live, stop, provider_name, api_key, model_id,
                    prompt, session_id, tools=tool_specs or None,
                    thread_id=thread_id, usage_out=susage)
                got = True
            except Exception as e:
                log.warning("streaming failed (%s) - falling back to blocking call", e)
            _record_usage(cfg, state, chat_id, thread_id, susage,
                          provider_name, model_id)
        if not got and not stop():
            tg.typing(chat_id, thread_id)
            try:
                answer, reasoning, tcs = pcall(prompt, tools=tool_specs or None)
            except Exception as e:
                # keep the turn (user message + any tool rounds) per policy;
                # just close unanswered calls so the next request is valid
                _close_tool_rounds(history)
                state.persist(chat_id, thread_id)
                _err(tg, msg, thread_id, "%s error: %s" % (provider_name, e))
                return
        if stop():
            interrupted = True
            break
        if not tcs:
            # the model sometimes writes the call as TEXT instead of using
            # the tools API - recover it and run the round; raw markup must
            # never reach the chat
            tcs, answer = textcalls.recover(answer)
            if not tcs:
                if textcalls.has_markup(answer):
                    # MARKUP, but nothing parseable. Deliberately NOT a retry
                    # and NOT a guess at the damage: the answer goes out as
                    # plain text so an unseen dialect stays VISIBLE and can
                    # be supported later (user directive), with the log
                    # flagging that markup was seen.
                    log.warning("unparsable tool-call markup - delivered as "
                                "text: chat=%s thread=%s", chat_id, thread_id)
                break  # the model produced its final text
            log.warning("recovered text-form tool call(s) - model bypassed "
                        "the tools API: chat=%s thread=%s", chat_id, thread_id)
        rounds += 1
        log.info("tool round %d: %d call(s)", rounds, len(tcs))
        # this round's reasoning becomes a permanent block BEFORE the tools
        # run - every block is shown as it happens, not just the last one.
        # It edits the block the thinking streamed into (one message per
        # round - never a second, half-streamed one next to it)
        if show and (reasoning or "").strip():
            _send_thinking_block(tg, chat_id, thread_id, reasoning, cfg,
                                 live=live)
        # a round that ends in tool calls must not swallow the prose it
        # streamed: keep it as a real message above the tool lines
        _keep_midturn_answer(tg, live, chat_id, thread_id, answer, cfg)
        # the round's live view is now superseded (the thinking block above
        # is permanent, tool lines appear below it): release it so the NEXT
        # round streams into fresh messages underneath instead of editing
        # messages that sit above the tool lines. The thinking message is
        # kept; only the answer's progress view is cleaned up.
        _drop_live(tg, live, chat_id, keep_thinking=True)
        # run the tools the model asked for - the round (input + results) is
        # recorded in history too, so the next turn remembers what happened
        # instead of re-reading the same file over and over
        tc_msg = {"role": "assistant", "content": answer or "",
                  "tool_calls": [
                      {"id": t["id"], "type": "function",
                       "function": {"name": t["name"],
                                    "arguments": json.dumps(t["arguments"])}}
                      for t in tcs]}
        # the round's reasoning rides in context too: on the next round the
        # model re-reads its own analysis instead of starting cold (the
        # provider accepts reasoning_content on an assistant message that
        # also carries tool_calls - verified against go/deepseek-v4.1-flash)
        _store_reasoning(tc_msg, reasoning)
        messages.append(tc_msg)
        history.append(tc_msg)
        for t in tcs:
            if stop():
                interrupted = True
                break
            if t["name"] not in declared:
                # a tool this request never offered. Answer it rather than
                # skipping it: the API requires one result per call, and the
                # model gets to correct itself instead of stalling.
                result = _unavailable(t["name"], declared)
                log.warning("tool call for unavailable tool: %s (chat=%s "
                            "thread=%s)", t["name"], chat_id, thread_id)
                messages.append({"role": "tool", "tool_call_id": t["id"],
                                 "content": result})
                history.append({"role": "tool", "tool_call_id": t["id"],
                                "content": result})
                state.persist(chat_id, thread_id)
                continue
            preview = tools_mod.preview(t["name"], t["arguments"])
            try:
                tg.send(chat_id, "\U0001f527 <code>%s</code> %s"
                        % (_esc(t["name"]), _esc(preview)), thread_id)
            except tg_mod.TelegramError:
                pass
            ctx.pop("_spill", None)
            beat = _ToolHeartbeat(tg, chat_id, thread_id, t["name"], preview)
            ctx["progress"] = beat.tick
            try:
                result = tools_mod.execute(cfg, t["name"], t["arguments"], ctx)
            finally:
                ctx.pop("progress", None)
                beat.done()
            spill = ctx.pop("_spill", None)
            messages.append({"role": "tool", "tool_call_id": t["id"],
                             "content": result})
            if spill:  # over the byte limit: history keeps only the path
                path, total = spill
                stored = ("[full tool output saved to %s (%d bytes) - use "
                          "read_file to view it]" % (path, total))
            else:
                stored = result
            history.append({"role": "tool", "tool_call_id": t["id"],
                            "content": stored})
            # write through at every tool boundary: a 56-round run lives in
            # RAM otherwise, and a crash 20 minutes in takes the whole turn
            # with it (that is what the docstring always promised)
            state.persist(chat_id, thread_id)
            log.info("tool %s -> %d chars%s", t["name"], len(result),
                     ", spilled" if spill else "")
            if stop():
                interrupted = True
                break
        if interrupted:
            break
        answer = None  # next round must produce the final text

    if interrupted or stop():
        _deliver_interrupted(tg, state, msg, thread_id, answer, live, reasoning,
                             cfg)
        return

    if not (answer or "").strip():
        # no text and no tool calls either: one retry with tools off
        tg.typing(chat_id, thread_id)
        try:
            answer, reasoning, _ = pcall(messages + [_clock_msg(state, chat_id, thread_id)])
        except Exception as e:
            _close_tool_rounds(history)
            state.persist(chat_id, thread_id)
            _err(tg, msg, thread_id, "%s error: %s" % (provider_name, e))
            return

    if textcalls.looks_like(answer):
        log.warning("answer contains text-form tool calls (model bypassed the "
                    "tools API) - delivering as text: chat=%s thread=%s",
                    chat_id, thread_id)
    log.info("turn done: chat=%s thread=%s tool_rounds=%d answer=%d chars",
             chat_id, thread_id, rounds, len(answer or ""))

    # a leak that never became a call must not be persisted: the model copies
    # its own degraded output forward, so one broken reply poisons every turn
    # after it. Sanitizing here also fixes what chat is about to show.
    answer = textcalls.sanitize(answer)
    hist_msg = {"role": "assistant", "content": answer}
    _store_reasoning(hist_msg, reasoning)  # last round's reasoning stays in context
    history.append(hist_msg)
    _strip_orphan_tools(history)
    state.persist(chat_id, thread_id)

    reasoning_md = reasoning if (show and reasoning and reasoning.strip()) else None
    sent = False
    if cfg.rich_messages:
        try:
            _send_rich_answer(tg, chat_id, thread_id, answer, reasoning_md, live=live)
            sent = True
        except tg_mod.TelegramError as e:
            log.warning("rich send failed, falling back to regular messages: %s", e)
    if not sent:
        parts = md2tg.send_parts(answer, reasoning_md)
        for part in parts:
            tg.send(chat_id, part, thread_id)


def _deliver_interrupted(tg, state, msg, thread_id, partial, live, reasoning=None,
                         cfg=None):
    """Record the /stop interruption in context and tell the user.

    What the user could already see stays visible: the round's prose is
    finalized as a real message (same rule as a tool round - see
    _keep_midturn_answer) and the current round's thinking block is kept
    instead of deleted, so an interrupted turn looks like any other round
    that happened rather than one that was erased."""
    chat_id = msg["chat"]["id"]
    history = state.get_history(chat_id, thread_id)
    _close_tool_rounds(history)  # keep the partial round; close its calls
    partial = textcalls.sanitize(partial or "")  # never persist a leak
    body = (((partial or "").strip() + "\n\n") if (partial or "").strip() else "")
    hist_msg = {"role": "assistant", "content": body + INTERRUPT_NOTE}
    _store_reasoning(hist_msg, reasoning)  # what it was thinking when stopped
    history.append(hist_msg)
    _strip_orphan_tools(history)
    state.persist(chat_id, thread_id)
    if cfg is not None:
        # the partial prose is real output the user already watched stream -
        # keep it above the stop notice rather than deleting it with the view
        _keep_midturn_answer(tg, live, chat_id, thread_id, partial, cfg)
    _drop_live(tg, live, chat_id, keep_thinking=True)  # no stale progress views
    log.info("turn interrupted by user: chat=%s thread=%s", chat_id, thread_id)
    try:
        tg.send(chat_id, "\u23f9 stopped - the running reply/tool was interrupted.",
                thread_id)
    except tg_mod.TelegramError:
        pass


def _send_rich_answer(tg, chat_id, thread_id, answer_md, thinking_md, live=None):
    """Bot API 10.1 rich messages: thinking as a collapsible <details> first,
    then the answer as GFM markdown chunks (32k each). The live slots edit in
    place instead of sending: the streamed thinking block keeps ITS message,
    and so does the streamed answer (edit mode) - a fully streamed turn only
    needs the drafts' final send. Slots are matched by ROLE, so thinking can
    never overwrite the answer's message (or vice versa); a live message
    nothing lands in is deleted rather than left behind as a stale progress
    view."""
    items = []  # (kind, src), kind: "html" | "markdown"
    if thinking_md:
        for chunk in md2tg.split(thinking_md, md2tg.RICH_LIMIT):
            if chunk.strip():
                items.append(("html", _thinking_html(chunk)))
    for chunk in md2tg.split_rich(answer_md):
        if chunk.strip():  # never send an empty rich message (400)
            items.append(("markdown", chunk))
    slots = []
    if live:
        roles = live.get("role") or {}
        if live.get("msg_id") and thinking_md \
                and roles.get("msg_id") == "thinking":
            slots.append(("msg_id", "html"))  # the block that streamed
        if live.get("answer_id"):
            slots.append(("answer_id", "markdown"))
        elif live.get("msg_id") and not thinking_md \
                and roles.get("msg_id") == "answer":
            slots.append(("msg_id", "markdown"))  # the answer streamed here
        # ...and when the answer holds the ONLY message while thinking_md is
        # due, the slot is left over on purpose: it is deleted below and both
        # are sent fresh, so the thinking still lands above the answer.
    for slot, kind in slots:
        mid = live.get(slot)
        hit = next((i for i, (k, _s) in enumerate(items) if k == kind), None)
        if not mid or hit is None:
            continue
        live[slot] = None  # consumed (cleared before the edit so a failed
        try:               # edit cannot leave the slot looking stale)
            k, src = items[hit]
            tg.edit_rich(chat_id, mid, **{k: src})
            items.pop(hit)
        except tg_mod.TelegramError as e:
            if tg_mod.is_not_modified(e):
                items.pop(hit)  # identical content - the edit is a no-op
                continue        # success, not a failure
            log.warning("final live edit failed, sending fresh: %s", e)
            try:
                tg.delete_message(chat_id, mid)
            except Exception:
                pass
    for slot in ("msg_id", "answer_id"):  # leftovers: stale progress views
        mid = live.get(slot) if live else None
        if mid:
            live[slot] = None
            try:
                tg.delete_message(chat_id, mid)
            except Exception:
                pass
    for kind, src in items:
        if kind == "html":
            tg.send_rich(chat_id, html=src, thread_id=thread_id)
        else:
            tg.send_rich(chat_id, markdown=src, thread_id=thread_id)


# ---------------------------------------------------------------- media

def handle_media(cfg, tg, msg, thread_id, state):
    user_id = msg["from"]["id"]
    caption = msg.get("caption") or ""
    if caption.strip().lstrip("/").lower() == "media_test":
        _media_echo(cfg, tg, msg, thread_id)
        return

    limit = cfg.max_file_mb * 1024 * 1024
    images = []
    doc_note = None

    if msg.get("photo"):
        tg_file = msg["photo"][-1]  # largest size
    elif msg.get("document"):
        doc = msg["document"]
        tg_file = doc
        mime = doc.get("mime_type", "")
        if mime.startswith("text/") or os.path.splitext(doc.get("file_name", ""))[1].lower() in TEXT_EXTS:
            pass  # handled below
        elif mime not in IMAGE_MIMES:
            name = doc.get("file_name", "file")
            size = doc.get("file_size", 0)
            tg.send(msg["chat"]["id"], "received <code>%s</code> (%s) - this type is not processed yet" % (_esc(name), _human(size)), thread_id)
            return
    elif msg.get("video"):
        tg_file = msg["video"]
    elif msg.get("voice"):
        tg_file = msg["voice"]
    elif msg.get("audio"):
        tg_file = msg["audio"]
    else:
        return

    size = tg_file.get("file_size") or 0
    if size > limit:
        tg.send(msg["chat"]["id"], "file too large (%s, limit %d MB)" % (_human(size), cfg.max_file_mb), thread_id)
        return
    try:
        f = tg.get_file(tg_file["file_id"])
        path = tg.download(f)
    except Exception as e:
        _err(tg, msg, thread_id, "download failed: %s" % e)
        return

    mime = tg_file.get("mime_type") or mimetypes.guess_type(path)[0] or "application/octet-stream"
    try:
        if mime in IMAGE_MIMES:
            with open(path, "rb") as fh:
                images.append((mime, base64.b64encode(fh.read()).decode("ascii")))
        elif mime.startswith("text/") or os.path.splitext(path)[1].lower() in TEXT_EXTS or mime == "application/json":
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read(200_000)
            doc_note = "attached file %s:\n```\n%s\n```" % (tg_file.get("file_name", path), data)
        else:
            doc_note = "user sent a %s file" % mime
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    if not images and not doc_note:
        return
    prompt = caption or ""
    if doc_note:
        prompt = (prompt + "\n\n" if prompt else "") + doc_note
    ai_reply(cfg, tg, msg, thread_id, state, prompt or None, images or None)


def _media_echo(cfg, tg, msg, thread_id):
    """Send the user's own media back - tests the upload path."""
    try:
        if msg.get("photo"):
            f = tg.get_file(msg["photo"][-1]["file_id"])
            path = tg.download(f)
            tg.send_media(msg["chat"]["id"], "photo", path, "round-trip test", thread_id)
            os.remove(path)
            return
        kind = "document" if msg.get("document") else ("video" if msg.get("video") else ("voice" if msg.get("voice") else ("audio" if msg.get("audio") else None)))
        if not kind:
            tg.send(msg["chat"]["id"], "send media with caption media_test", thread_id)
            return
        f = tg.get_file(msg[kind]["file_id"])
        path = tg.download(f)
        name = msg[kind].get("file_name") or os.path.basename(path)
        tmp = os.path.join(os.path.dirname(path), name) if kind == "document" else path
        if tmp != path:
            os.rename(path, tmp)
        tg.send_media(msg["chat"]["id"], kind, tmp, "round-trip test", thread_id)
        if tmp != path:
            os.remove(tmp)
        else:
            os.remove(path)
    except Exception as e:
        _err(tg, msg, thread_id, "media echo failed: %s" % e)


# ---------------------------------------------------------------- routing
# Delivery rules (groups 20/22): topic flow OFF = topics fully inert (hint
# every message); topic flow ON = All takes commands only, topics need a
# bound session; typed prompts live per slot (one per topic + one for All).

SESSION_SCOPED = {"context", "cost", "compact", "delete", "rename", "model",
                  "stop"}

HINT_TOPIC_FLOW_OFF = (
    "topic flow is off - messages inside topics are ignored. "
    "Talk to me in All messages, or run /topic there to switch modes.")
HINT_ALL_PLAIN = (
    "topic flow is on - messages in All are ignored. "
    "/new starts a topic, /session switches sessions.")
HINT_ALL_NEEDS_SESSION = (
    "that command needs a session - start one with /new or open a topic. "
    "(<code>/model global &lt;provider/model&gt;</code> works anywhere.)")
HINT_UNBOUND_TOPIC = (
    "this topic is bound to no session - use /session to bind a session, "
    "or /new to start a new one.")

TOPIC_SERVICE_EVENTS = (
    "forum_topic_edited", "forum_topic_deleted", "forum_topic_closed",
    "forum_topic_reopened", "forum_topic_unrestricted")


def _session_scoped(name, rest):
    """/model global is chat-wide (allowed everywhere - group 22)."""
    if name == "model" and rest.strip().lower().startswith("global"):
        return False
    return name in SESSION_SCOPED


def _dispatch_command(cfg, tg, msg, text, thread_id, state):
    cmd, _, rest = text[1:].partition(" ")
    msg["_arg"] = rest
    entry = COMMANDS.get(cmd.split("@", 1)[0])  # tolerate /cmd@botname
    if entry is None:
        tg.send(msg["chat"]["id"], "unknown command, try /help", thread_id)
    else:
        entry[0](cfg, tg, msg, thread_id, state)


def _witness_topic_event(chat_id, thread_id, msg, state):
    """Record topic lifecycle events in the registry (for /reset-all and
    name mirroring). No-op for any other kind of message."""
    if not thread_id:
        return
    created = msg.get("forum_topic_created")
    if isinstance(created, dict):
        name = created.get("name") or ""
        if thread_id not in state.topics.setdefault(chat_id, []):
            state.topics[chat_id].append(thread_id)
        state.topic_names[(chat_id, thread_id)] = name
        if state.store:
            state.store.add_topic(chat_id, thread_id, name)
        return
    edited = msg.get("forum_topic_edited")
    if isinstance(edited, dict) and edited.get("name"):
        state.topic_names[(chat_id, thread_id)] = edited["name"]
        if state.store:
            state.store.set_topic_name(chat_id, thread_id, edited["name"])


def handle_message(cfg, tg, msg, state):
    chat_id = msg["chat"]["id"]
    thread_id = msg.get("message_thread_id")
    user = msg.get("from") or {}

    # Telegram echoes back events the bot itself caused (topic created/
    # edited service messages, its own replies): witness the topic events,
    # never treat them as a user message or an allow-list miss.
    if state.bot_id is not None and user.get("id") == state.bot_id:
        _witness_topic_event(chat_id, thread_id, msg, state)
        log.info("bot's own message/event - ignored: chat=%s thread=%s",
                 chat_id, thread_id)
        return

    if not cfg.is_allowed(user.get("id"), user.get("username")):
        log.info("ignored message from user %s (@%s) - not in allowed_users",
                 user.get("id"), user.get("username"))
        return  # silently ignore non-allowed users
    log.info("message from user %s (@%s) chat=%s thread=%s",
             user.get("id"), user.get("username"), chat_id, thread_id)

    # topic lifecycle: witness creations/renames, never hinted
    if "forum_topic_created" in msg or "forum_topic_edited" in msg:
        _witness_topic_event(chat_id, thread_id, msg, state)
        return
    if any(k in msg for k in TOPIC_SERVICE_EVENTS):
        return

    text = msg.get("text") or msg.get("caption") or ""
    is_cmd = text.startswith("/")
    slot = thread_id
    flow = cfg.topic_flow
    in_topic = thread_id is not None

    if is_cmd:
        # any command cancels a pending prompt in its own slot, then runs
        state.prompts.pop((chat_id, slot), None)
    else:
        prompt = state.prompts.get((chat_id, slot))
        if prompt is not None:
            state.prompts.pop((chat_id, slot), None)
            if _prompt_reply(cfg, tg, chat_id, slot, prompt, text, state):
                return
            # not a choice -> "do nothing": the message proceeds normally

    # --- mode gates -------------------------------------------------------
    if in_topic and not flow:
        # topics fully inert while flow is off: plain AND commands
        tg.send(chat_id, HINT_TOPIC_FLOW_OFF, thread_id)
        return
    if not in_topic and flow:
        if is_cmd:
            name, _, rest = text[1:].partition(" ")
            name = name.split("@", 1)[0]
            if _session_scoped(name, rest):
                tg.send(chat_id, HINT_ALL_NEEDS_SESSION, None)
                return
            _dispatch_command(cfg, tg, msg, text, thread_id, state)
            return
        tg.send(chat_id, HINT_ALL_PLAIN, None)  # hint every plain message/media
        return

    # --- normal path: main chat (flow off) or a topic (flow on) -----------
    if is_cmd:
        name, _, rest = text[1:].partition(" ")
        name = name.split("@", 1)[0]
        if in_topic and not state.session_at(chat_id, thread_id) \
                and _session_scoped(name, rest):
            tg.send(chat_id, HINT_UNBOUND_TOPIC, thread_id)
            return
        _dispatch_command(cfg, tg, msg, text, thread_id, state)
        return

    if in_topic and not state.session_at(chat_id, thread_id):
        tg.send(chat_id, HINT_UNBOUND_TOPIC, thread_id)
        return

    if msg.get("photo") or msg.get("document") or msg.get("video") \
            or msg.get("voice") or msg.get("audio"):
        handle_media(cfg, tg, msg, thread_id, state)
        return

    if text.strip():
        reply_ctx = _reply_context(msg)
        if reply_ctx:
            text = reply_ctx + "\n\n" + text
        ai_reply(cfg, tg, msg, thread_id, state, text)


def _reply_context(msg):
    """Annotation for replies/quotes so the model sees the referenced text
    (item 17). The reply object carries the full referenced message."""
    reply = msg.get("reply_to_message")
    if not reply:
        return ""
    sender = reply.get("from") or {}
    who = sender.get("username") or sender.get("first_name") or "someone"
    body = reply.get("text") or reply.get("caption") or ""
    quote = msg.get("quote")  # user quoted a part of the message
    if quote is not None:
        q = (quote.get("text") or "") if isinstance(quote, dict) else str(quote)
        return "user is quoting this part of a message (from @%s):\n%s" % (
            who, (q[:1200] or "(empty quote)"))
    if not body:
        body = ("(the referenced message has no extractable text - "
                "it may be media or a rich message)")
    return "user is replying to this message (from @%s):\n%s" % (who, body[:1500])


# ------------------------------------------------------- /stop + update watcher
# Concurrency model: a watcher thread owns getUpdates (and intercepts /stop
# for the session's running turn via a thread-safe flag). A single dispatcher
# routes every other update: a plain message in a session with an active turn
# is injected into that turn (folded into context at the next tool boundary);
# everything else lands on the session's own queue. One worker thread per
# session key runs that queue - messages inside a session stay ordered,
# sessions/topics run in parallel and never block each other.

def _msg_key(msg):
    return (msg["chat"]["id"], msg.get("message_thread_id"))


def _is_stop_cmd(msg):
    text = (msg.get("text") or "").strip()
    if not text.startswith("/stop"):
        return False
    rest = text[len("/stop"):]
    return rest == "" or rest[0] in " @"  # /stop, /stop@botname


def _route_update(state, out_q, upd):
    """Watcher decision: consume /stop for a running turn, else queue.
    Returns True when the update was consumed (not queued).

    Two ways to stop a turn: the /stop command, and Telegram's own Stop
    button under a streamed draft (sendRichMessageDraft can_stop=True) -
    the latter arrives as stopped_message_generation and interrupts the
    very same turn."""
    native = upd.get("stopped_message_generation")
    if native:
        chat = native.get("chat") or {}
        key = state.chat_key(chat.get("id"), native.get("message_thread_id"))
        hit = state.request_interrupt(key)
        log.info("native stop %s: chat=%s thread=%s draft=%s",
                 "requested" if hit else "ignored (nothing running)",
                 key[0], key[1], native.get("draft_id"))
        return True  # not a message: never queue it
    msg = upd.get("message")
    if msg and _is_stop_cmd(msg):
        if state.request_interrupt(_msg_key(msg)):
            log.info("stop requested: chat=%s thread=%s",
                     msg["chat"]["id"], msg.get("message_thread_id"))
            return True
    out_q.put(upd)
    return False


def _mark_ready(cfg):
    """Write logs/keirai.ready once polling actually works.

    deploy/notify_start.py waits for this file before saying "I'm back".
    A spawned process proves nothing, and Telegram answering getMe proves
    only that the *token* is good - not that this bot is consuming updates.
    The first successful getUpdates is the earliest honest "I am back", and
    it is the harness saying so about itself."""
    path = os.path.join(config_mod.base_dir(cfg), "logs", "keirai.ready")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("%d\n" % os.getpid())
    except OSError as e:
        log.warning("cannot write ready file %s: %s", path, e)


def _watch_updates(cfg, tg, state, out_q):
    """Long-poll getUpdates forever. Sole owner of the offset.

    This thread dying = the bot goes silent forever (everything else runs
    off its queue), so it never raises: the Telegram call maps transport
    errors to TelegramError, and anything else a malformed update or an
    unexpected bug could throw is caught, logged and retried here."""
    offset = None
    backoff = 1
    ready = False
    while True:
        try:
            # the very first poll is non-blocking: a long poll returns only
            # when a message arrives OR the idle timeout fires (25s), so
            # gating readiness on it made "up" as slow as the idle wait.
            updates = tg.get_updates(offset, timeout=0 if not ready
                                     else tg_mod.POLL_TIMEOUT)
            backoff = 1
            if not ready:
                _mark_ready(cfg)  # first honest "I can receive messages"
                ready = True
        except tg_mod.TelegramError as e:
            log.error("poll error: %s", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        except Exception:
            log.exception("unexpected poll failure - retrying in %ds", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        for upd in updates:
            try:
                offset = upd["update_id"] + 1  # advance even if routing fails
                _route_update(state, out_q, upd)
            except Exception:
                log.exception("failed to route update - skipping it")


def _dispatch_update(cfg, tg, state, workers, upd):
    """Route one update (runs on the single dispatcher thread).

    Plain text in a NORMAL-path session whose turn is active -> inject into
    the running turn (steering without /stop). Everything else (commands,
    media, other sessions, gated paths) -> that session's queue, so a long
    turn only ever delays its OWN session."""
    msg = upd.get("message")
    if not msg:
        return
    text = (msg.get("text") or "").strip()
    media = any(k in msg for k in ("photo", "document", "video",
                                   "voice", "audio"))
    thread_id = msg.get("message_thread_id")
    flow = cfg.topic_flow
    normal_path = bool(thread_id is not None) == bool(flow)
    user = msg.get("from") or {}
    mine = state.bot_id is None or user.get("id") != state.bot_id
    if (text and not text.startswith("/") and not media and normal_path
            and mine and cfg.is_allowed(user.get("id"), user.get("username"))
            and state.inject(_msg_key(msg), upd)):
        log.info("mid-turn message injected: chat=%s thread=%s",
                 msg["chat"]["id"], thread_id)
        return
    _session_submit(cfg, tg, state, workers, _msg_key(msg), upd)


def _session_submit(cfg, tg, state, workers, key, upd):
    """Queue `upd` on the session's own worker (created on first use)."""
    q = workers.get(key)
    if q is None:
        q = queue.Queue()
        workers[key] = q
        threading.Thread(target=_session_worker, args=(cfg, tg, state, key, q),
                         daemon=True, name="s-%s-%s" % key).start()
    q.put(upd)


def _session_worker(cfg, tg, state, key, q):
    """Process one session's updates in arrival order, forever."""
    while True:
        _run_update(cfg, tg, state, q.get())
        # mid-turn messages that never reached a boundary (the turn ended
        # first) are handled now, before anything later queued
        for extra in state.take_inbox(key):
            _run_update(cfg, tg, state, extra)


def _run_update(cfg, tg, state, upd):
    msg = upd.get("message")
    if not msg:
        return
    try:
        handle_message(cfg, tg, msg, state)
    except Exception as e:
        log.exception("handler error")
        try:
            tg.send(msg["chat"]["id"], "error: %s" % _esc(str(e)),
                    msg.get("message_thread_id"))
        except Exception:
            pass


# ---------------------------------------------------------------- main

def setup_logging(base_dir=None):
    """Log to stderr AND to <base>/logs/keirai.log (rotating 2MB x 3)."""
    base = base_dir or os.path.dirname(os.path.abspath(__file__))
    logdir = os.path.join(base, "logs")
    os.makedirs(logdir, exist_ok=True)
    root = logging.getLogger("keirai")
    if root.handlers:
        return root
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(logdir, "keirai.log"), maxBytes=2_000_000, backupCount=2,
        encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    return root


def main():
    setup_logging()
    try:
        cfg = config_mod.load()
        cfg.validate()
    except config_mod.ConfigError as e:
        log.error("config error: %s", e)
        log.error("copy config.example.toml to config.toml and set bot_token")
        sys.exit(1)

    store = sessions.Store(config_mod.sessions_path(cfg))
    tg = tg_mod.Telegram(cfg.bot_token)
    state = State(cfg.thinking_default, store=store)
    try:
        me = tg.get_me()
        state.topics_enabled = bool(me.get("has_topics_enabled"))
        state.allows_user_topics = bool(me.get("allows_users_to_create_topics"))
        state.bot_id = me.get("id")
        log.info("bot @%s ready (private-chat topics: %s, disallow-user-topics: %s)",
                 me.get("username", "?"),
                 "on" if state.topics_enabled else
                 "off - enable Threaded mode in the @BotFather Mini App",
                 "on" if state.allows_user_topics else "off")
    except tg_mod.TelegramError as e:
        log.error("getMe failed: %s (continuing without topic support)", e)
    def _publish_commands():
        """The bot menu is not needed to receive messages - an RTT spent on
        cosmetics delayed every restart by ~0.5s, so it runs alongside the
        poller instead of in front of it."""
        try:
            tg.call("setMyCommands", {"commands": [
                {"command": name, "description": desc}
                for name, (fn, desc, hidden) in sorted(COMMANDS.items()) if not hidden
            ]})
        except tg_mod.TelegramError as e:
            log.warning("setMyCommands failed: %s", e)

    threading.Thread(target=_publish_commands, daemon=True, name="commands").start()
    log.info("polling... (default model: %s)", cfg.default_model)
    out_q = queue.Queue()
    threading.Thread(target=_watch_updates, args=(cfg, tg, state, out_q),
                     daemon=True, name="updates").start()
    workers = {}  # session key -> queue; a worker thread per active session
    while True:
        # the dispatcher must survive a bad update: exiting here would take
        # the whole bot down (systemd restarts it, but in-flight state is
        # lost), so log and keep pumping the queue
        try:
            _dispatch_update(cfg, tg, state, workers, out_q.get())
        except Exception:
            log.exception("dispatcher error - continuing")


# ---------------------------------------------------------------- helpers

def _arg(msg):
    return msg.get("_arg", "")


def _esc(s):
    return html.escape(s, quote=False)


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0


def _fmt_cost(x):
    if x == 0:
        return "0"
    return ("%g" % x)


def _err(tg, msg, thread_id, text):
    log.error(text)
    try:
        tg.send(msg["chat"]["id"], "error: %s" % _esc(text), thread_id)
    except Exception:
        pass


if __name__ == "__main__":
    main()
