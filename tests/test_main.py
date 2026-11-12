import json
import os
import queue
import tempfile
import threading
import unittest
from unittest import mock

import main
import providers
import config as config_mod


class FakeTG:
    def __init__(self):
        self.sent = []
        self.rich = []       # (chat_id, {"markdown"|"html": src}, thread_id)
        self.edits = []      # (chat_id, message_id, src)
        self.deleted = []    # (chat_id, message_id)
        self.drafts = []     # (chat_id, draft_id, src, thread_id, can_stop)
        self.media = []
        self.file_bytes = b"fakeimage"
        self.topics = {}     # name -> thread_id
        self.renamed = []    # (chat_id, thread_id, name)
        self.deleted_topics = []  # (chat_id, thread_id)
        self._next_tid = 500
        self.me = {"has_topics_enabled": True,
                   "allows_users_to_create_topics": False}
        self.fail_threads = set()  # thread ids whose send raises (dead topic)

    def get_me(self):
        return dict(self.me)

    def send(self, chat_id, text, thread_id=None):
        if thread_id in self.fail_threads:
            raise Exception("Bad Request: message thread not found")
        self.sent.append((chat_id, text, thread_id))
        return {"message_id": len(self.sent)}

    def send_rich(self, chat_id, markdown=None, html=None, thread_id=None):
        src = {"markdown": markdown} if markdown is not None else {"html": html}
        self.rich.append((chat_id, src, thread_id))
        return {"message_id": 1000 + len(self.rich)}

    def send_rich_draft(self, chat_id, draft_id, markdown=None, html=None,
                        thread_id=None, can_stop=False):
        """Bot API 10.1 sendRichMessageDraft: the answer's real-time stream
        (an ephemeral preview in the chat - never the user's composer)."""
        src = {"markdown": markdown} if markdown is not None else {"html": html}
        self.drafts.append((chat_id, draft_id, src, thread_id, can_stop))
        return True

    def edit_rich(self, chat_id, message_id, markdown=None, html=None):
        src = {"markdown": markdown} if markdown is not None else {"html": html}
        self.edits.append((chat_id, message_id, src))
        return True

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

    def typing(self, chat_id, thread_id=None):
        pass

    def get_file(self, file_id):
        return {"file_path": "photos/x.jpg", "file_size": 100}

    def download(self, tg_file, max_bytes=None):
        path = os.path.join(tempfile.gettempdir(), "keirai_test_img.jpg")
        with open(path, "wb") as f:
            f.write(self.file_bytes)
        return path

    def send_media(self, chat_id, kind, path, caption=None, thread_id=None):
        self.media.append((chat_id, kind, caption))

    def create_topic(self, chat_id, name):
        self._next_tid += 1
        self.topics[name] = self._next_tid
        return {"message_thread_id": self._next_tid, "name": name}

    def edit_topic(self, chat_id, thread_id, name):
        self.renamed.append((chat_id, thread_id, name))

    def delete_topic(self, chat_id, thread_id):
        self.deleted_topics.append((chat_id, thread_id))


def make_config(**kw):
    import config as config_mod
    base = {"bot_token": "t", "allowed_users": [42],
            "rich_messages": True, "stream_drafts": False,
            # explicit -> default tests never hit models.dev for thresholds
            "context_limit_tokens": 30_000,
            "providers": {"zen": {"api_key": "zk"}, "go": {"api_key": "gk"}}}
    base.update(kw)
    return config_mod.Config(base)


def msg(text=None, user_id=42, **extra):
    m = {"chat": {"id": 100, "type": "private"}, "from": {"id": user_id, "username": "tester"},
         "text": text or "", "message_thread_id": None}
    m.update(extra)
    return m


def disable_titles(test):
    """Group-19 naming issues one extra chat call per new session; neutralize
    it so tests see exactly one call per turn (and never hit the network)."""
    p = mock.patch.object(main, "_maybe_name_session", lambda *a, **k: None)
    p.start()
    test.addCleanup(p.stop)


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    def test_denied_user_ignored(self):
        main.handle_message(self.cfg, self.tg, msg(user_id=999, text="hi"), self.state)
        self.assertEqual(self.tg.sent, [])

    def test_allowed_user_help(self):
        main.handle_message(self.cfg, self.tg, msg(text="/help"), self.state)
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("Keirai", self.tg.sent[0][1])

    def test_thinking_toggle(self):
        main.handle_message(self.cfg, self.tg, msg(text="/thinking off"), self.state)
        self.assertFalse(self.state.thinking_on(42))
        self.assertIn("off", self.tg.sent[-1][1])
        main.handle_message(self.cfg, self.tg, msg(text="/thinking on"), self.state)
        self.assertTrue(self.state.thinking_on(42))

    def test_unknown_command(self):
        main.handle_message(self.cfg, self.tg, msg(text="/nope"), self.state)
        self.assertIn("unknown command", self.tg.sent[0][1])

    def test_test_md_sends_html(self):
        main.handle_message(self.cfg, self.tg, msg(text="/test_md **hi**"), self.state)
        self.assertEqual(self.tg.sent[0][1], "hi".replace("hi", "<b>hi</b>"))

    @mock.patch.object(providers, "chat", return_value=("answer **x**", "deep thought", None))
    def test_ai_flow_rich_with_thinking(self, _):
        m = msg(text="hello agent")
        main.handle_message(self.cfg, self.tg, m, self.state)
        self.assertEqual(len(self.tg.rich), 2)
        # thinking first, as collapsible details with converted HTML inside
        chat, src, _tid = self.tg.rich[0]
        self.assertIn("html", src)
        self.assertIn("<details><summary>\U0001f914 Thinking</summary>", src["html"])
        self.assertIn("deep thought", src["html"])
        # answer as raw markdown (server parses GFM)
        chat, src, _tid = self.tg.rich[1]
        self.assertEqual(src, {"markdown": "answer **x**"})
        self.assertEqual(self.tg.sent, [])  # no regular messages

    @mock.patch.object(providers, "chat", return_value=("answer", "thoughts", None))
    def test_ai_flow_rich_thinking_off(self, _):
        main.handle_message(self.cfg, self.tg, msg(text="/thinking off"), self.state)
        self.tg.rich.clear()
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertEqual(len(self.tg.rich), 1)
        self.assertEqual(self.tg.rich[0][1], {"markdown": "answer"})

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_ai_flow_rich_fallback_on_error(self, _):
        main.handle_message(self.cfg, self.tg, msg(text="/thinking off"), self.state)
        self.tg.rich.clear()
        self.tg.sent.clear()
        with mock.patch.object(FakeTG, "send_rich", side_effect=main.tg_mod.TelegramError("no rich support")):
            main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertEqual(len(self.tg.sent), 1)  # fell back to regular send
        self.assertEqual(self.tg.sent[0][1], "answer")

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_ai_flow_rich_disabled_in_config(self, _):
        self.cfg = make_config(rich_messages=False)
        main.handle_message(self.cfg, self.tg, msg(text="/thinking off"), self.state)
        self.tg.rich.clear()
        self.tg.sent.clear()
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertEqual(self.tg.rich, [])
        self.assertEqual(len(self.tg.sent), 1)

    def test_test_rich_command(self):
        main.handle_message(self.cfg, self.tg, msg(text="/test_rich"), self.state)
        self.assertEqual(len(self.tg.rich), 1)
        self.assertIn("| Provider | Model |", self.tg.rich[0][1]["markdown"])

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_key_fallback_prefers_go(self, chat_mock):
        self.cfg = make_config(providers={"zen": {"api_key": ""}, "go": {"api_key": "gk"}})
        main.handle_message(self.cfg, self.tg, msg(text="/thinking off"), self.state)
        self.tg.rich.clear()
        self.tg.sent.clear()
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        # default model is zen/... but zen has no key -> routed via go
        self.assertEqual(chat_mock.call_args[0][0], "go")

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_no_keys_at_all_errors(self, chat_mock):
        self.cfg = make_config(providers={"zen": {"api_key": ""}, "go": {"api_key": ""}})
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        chat_mock.assert_not_called()
        self.assertTrue(any("no API key" in t for _, t, _ in self.tg.sent))

    @mock.patch.object(providers, "chat", return_value=("nice pic", "", None))
    def test_photo_goes_to_vision(self, chat_mock):
        m = msg(text=None, photo=[{"file_id": "f1", "file_size": 100}])
        main.handle_message(self.cfg, self.tg, m, self.state)
        sent = chat_mock.call_args[0][3]  # messages arg
        self.assertEqual(sent[-1]["content"][1]["type"], "image_url")
        self.assertTrue(sent[-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    @mock.patch.object(providers, "chat", return_value=("ok", "", None))
    def test_media_test_echo(self, _):
        m = msg(text=None, caption="media_test", photo=[{"file_id": "f1", "file_size": 100}])
        main.handle_message(self.cfg, self.tg, m, self.state)
        self.assertEqual(self.tg.media, [(100, "photo", "round-trip test")])

    @mock.patch.object(providers, "chat", return_value=("ok", "", None))
    def test_history_is_kept(self, chat_mock):
        main.handle_message(self.cfg, self.tg, msg(text="one"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="two"), self.state)
        hist = self.state.get_history(100, None)
        self.assertEqual([m2["role"] for m2 in hist], ["user", "assistant", "user", "assistant"])
        self.assertEqual(hist[-2]["content"], "two")  # last user msg
        self.assertEqual(hist[-1]["content"], "ok")   # last assistant msg


class TestSessions(unittest.TestCase):
    """P4: sessions live in topics while topic flow is ON (groups 19/20/22)."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        self.cfg.topic_flow = True
        disable_titles(self)

    @mock.patch.object(providers, "chat", return_value=("a1", "", None))
    def test_new_creates_topic_and_context_isolated(self, chat_mock):
        main.handle_message(self.cfg, self.tg, msg(text="/new work stuff"), self.state)
        # topic created, welcome sent INTO it, session bound eagerly
        tid = self.tg.topics["work stuff"]
        self.assertEqual(self.tg.sent[-1],
                         (100, "new session started in a topic - type here", tid))
        self.assertTrue(self.state.session_at(100, tid))
        # inside the topic -> own history, isolated from All
        main.handle_message(self.cfg, self.tg,
                            msg(text="hello", message_thread_id=tid), self.state)
        self.assertEqual([m["content"] for m in self.state.get_history(100, tid)],
                         ["hello", "a1"])
        # plain message in All -> hint, no answer, no history (group 20)
        main.handle_message(self.cfg, self.tg, msg(text="main chat"), self.state)
        self.assertEqual(self.state.history.get((100, None)), None)
        self.assertIn("topic flow is on", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat", return_value=("a1", "", None))
    def test_two_topics_isolated_all_hinted(self, chat_mock):
        main.handle_message(self.cfg, self.tg, msg(text="/new apples"), self.state)
        t1 = self.tg.topics["apples"]
        main.handle_message(self.cfg, self.tg, msg(text="/new bolts"), self.state)
        t2 = self.tg.topics["bolts"]
        for tid, text in ((t1, "about apples"), (t2, "about bolts")):
            main.handle_message(self.cfg, self.tg,
                                msg(text=text, message_thread_id=tid), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="in all"), self.state)
        # disjoint histories; All has none
        self.assertEqual([m["content"] for m in self.state.get_history(100, t1)],
                         ["about apples", "a1"])
        self.assertEqual([m["content"] for m in self.state.get_history(100, t2)],
                         ["about bolts", "a1"])
        self.assertEqual(self.state.history.get((100, None)), None)
        self.assertEqual(chat_mock.call_count, 2)
        # every provider call saw ONLY its own session's messages
        for call, own in zip(chat_mock.call_args_list,
                             ("about apples", "about bolts")):
            user_texts = [m["content"] for m in call[0][3]
                          if m.get("role") == "user"]
            self.assertEqual(user_texts, [own])
        # each session sends its OWN hashed x-opencode-session (Go/Zen want a
        # stable id per conversation) - never one shared app-wide bucket
        sid1 = self.state.meta[(100, t1)]["session_id"]
        sid2 = self.state.meta[(100, t2)]["session_id"]
        self.assertNotEqual(sid1, sid2)
        sids = [call[1]["session_id"] for call in chat_mock.call_args_list]
        self.assertEqual(sids, [main._session_header(sid1),
                                main._session_header(sid2)])
        for sent, own in zip(sids, (sid1, sid2)):
            self.assertNotIn(own, sent)      # hashed: raw id never leaves
            self.assertEqual(sent, main._session_header(own))

    @mock.patch.object(providers, "chat")
    def test_manual_topic_rejected_until_bound(self, chat_mock):
        """Hand-made topics are unbound: rejected with a hint until bound
        (group 20) - and they never auto-create sessions anymore (group 9
        behavior superseded)."""
        main.handle_message(self.cfg, self.tg,
                            msg(text="hello from my own topic",
                                message_thread_id=4242), self.state)
        chat_mock.assert_not_called()
        self.assertFalse(self.state.session_at(100, 4242))
        self.assertIn("bound to no session", self.tg.sent[-1][1])
        # session-scoped commands rejected there too...
        main.handle_message(self.cfg, self.tg,
                            msg(text="/context", message_thread_id=4242), self.state)
        self.assertIn("bound to no session", self.tg.sent[-1][1])
        # ...but /session (the binder) runs
        main.handle_message(self.cfg, self.tg,
                            msg(text="/session", message_thread_id=4242), self.state)
        self.assertIn("no other sessions", self.tg.sent[-1][1])
        # not registered as a bot-created topic (/reset-all won't touch it)
        self.assertNotIn(4242, self.state.topics.get(100, []))

    @mock.patch.object(providers, "chat", return_value=("a1", "", None))
    def test_new_placeholder_until_title(self, chat_mock):
        # group 19: placeholder now, agent title after the first message
        main.handle_message(self.cfg, self.tg, msg(text="/new"), self.state)
        self.assertIn("New session", self.tg.topics)
        main.handle_message(self.cfg, self.tg, msg(text="/new named trip"), self.state)
        self.assertIn("named trip", self.tg.topics)

    @mock.patch.object(providers, "chat", return_value=("old answer", "", None))
    def test_new_in_normal_flow_parks_and_starts_blank(self, chat_mock):
        self.cfg.topic_flow = False
        main.handle_message(self.cfg, self.tg, msg(text="old conversation"),
                            self.state)
        old_sid = self.state.ensure_meta(100, None)["session_id"]
        main.handle_message(self.cfg, self.tg, msg(text="/new"), self.state)
        # blank session active in place
        self.assertEqual(self.state.get_history(100, None), [])
        self.assertNotEqual(self.state.meta[(100, None)]["session_id"], old_sid)
        # the old session is parked (kept) and recoverable via /session
        parked = [(k, m) for k, m in self.state.meta.items()
                  if m.get("session_id") == old_sid]
        self.assertEqual(len(parked), 1)
        self.assertIsNotNone(parked[0][0][1])  # no longer in the main slot
        self.assertEqual([m["content"] for m in
                          self.state.history[parked[0][0]]],
                         ["old conversation", "old answer"])
        self.assertIn("recover it with /session", self.tg.sent[-1][1])
        self.assertEqual(self.tg.topics, {})  # no topics in normal flow

    @mock.patch.object(providers, "chat", return_value=("a1", "", None))
    def test_new_registers_topic_for_reset(self, chat_mock):
        main.handle_message(self.cfg, self.tg, msg(text="/new a"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="/new b"), self.state)
        self.assertEqual(len(self.state.topics[100]), 2)

    # ------------------------------------------------ /rename (mirror, group 22)

    def test_rename_mirrors_topic_and_session(self):
        main.handle_message(self.cfg, self.tg, msg(text="/new work"), self.state)
        tid = self.tg.topics["work"]
        main.handle_message(self.cfg, self.tg,
                            msg(text="/rename project x", message_thread_id=tid),
                            self.state)
        self.assertEqual(self.tg.renamed, [(100, tid, "project x")])
        self.assertEqual(self.state.meta[(100, tid)]["name"], "project x")
        self.assertIn("renamed", self.tg.sent[-1][1])

    def test_rename_in_main_chat_without_session_rejected(self):
        self.cfg.topic_flow = False  # in All (flow on) this is gate-rejected
        main.handle_message(self.cfg, self.tg, msg(text="/rename nope"), self.state)
        self.assertEqual(self.tg.renamed, [])
        self.assertIn("no active session", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat", return_value=("hi", "", None))
    def test_rename_session_in_main_chat(self, chat_mock):
        self.cfg.topic_flow = False
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="/rename my trip"),
                            self.state)
        self.assertEqual(self.tg.renamed, [])  # no topic to rename
        self.assertEqual(self.state.meta[(100, None)]["name"], "my trip")
        self.assertIn("renamed", self.tg.sent[-1][1])

    def test_rename_requires_name(self):
        main.handle_message(self.cfg, self.tg, msg(text="/new work"), self.state)
        tid = self.tg.topics["work"]
        main.handle_message(self.cfg, self.tg,
                            msg(text="/rename", message_thread_id=tid), self.state)
        self.assertEqual(self.tg.renamed, [])
        self.assertIn("usage", self.tg.sent[-1][1])


class TestResetAll(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)
        self.state.topics_enabled = True
        self.cfg.topic_flow = True  # /new creates topics in topic flow

    def _initiate(self):
        main.handle_message(self.cfg, self.tg, msg(text="/reset-all"), self.state)
        text = self.tg.sent[-1][1]
        import re
        m = re.search(r"/reset-all ([A-Z0-9]+) ([A-Z0-9]+)", text)
        self.assertIsNotNone(m, text)
        return [m.group(1), m.group(2)]

    def test_not_in_help(self):
        self.assertNotIn("reset-all", main.build_help())

    def test_initiate_then_confirm(self):
        main.handle_message(self.cfg, self.tg, msg(text="/new s1"), self.state)
        tid = self.tg.topics["s1"]
        self.state.get_history(100, tid).append({"role": "user", "content": "x"})
        self.state.model[(100, tid)] = "zen/m"

        challenge = self._initiate()
        self.assertEqual(self.tg.deleted_topics, [])
        main.handle_message(self.cfg, self.tg, msg(text="/reset-all %s %s" % tuple(challenge)), self.state)

        self.assertEqual(self.tg.deleted_topics, [(100, tid)])
        self.assertNotIn(100, self.state.topics)
        self.assertEqual(self.state.history.get((100, tid)), None)
        self.assertNotIn((100, tid), self.state.model)
        self.assertIn("reset done", self.tg.sent[-1][1])
        self.assertNotIn(100, self.state.pending_reset)

    def test_wrong_strings_rechallenge(self):
        challenge = self._initiate()
        main.handle_message(self.cfg, self.tg, msg(text="/reset-all XXXX YYYY"), self.state)
        self.assertEqual(self.tg.deleted_topics, [])
        import re
        text = self.tg.sent[-1][1]
        new_challenge = re.search(r"/reset-all ([A-Z0-9]+) ([A-Z0-9]+)", text).groups()
        self.assertNotEqual(list(new_challenge), challenge)

    def test_nothing_to_delete_still_resets(self):
        challenge = self._initiate()
        main.handle_message(self.cfg, self.tg, msg(text="/reset-all %s %s" % tuple(challenge)), self.state)
        self.assertEqual(self.tg.deleted_topics, [])
        self.assertIn("reset done", self.tg.sent[-1][1])


class TestDynamicHelp(unittest.TestCase):
    def test_help_lists_all_visible_commands(self):
        h = main.build_help()
        for name in ("start", "help", "new", "rename", "delete", "compact",
                     "thinking", "models", "model", "test_md", "test_rich",
                     "stop"):
            self.assertIn("/%s -" % name, h)
        self.assertNotIn("/reset-all", h)

    def test_only_reset_all_hidden(self):
        hidden = [n for n, (_f, _d, h) in main.COMMANDS.items() if h]
        self.assertEqual(hidden, ["reset-all"])

    def test_handlers_share_signature(self):
        import inspect
        for name, (fn, _d, _h) in main.COMMANDS.items():
            params = list(inspect.signature(fn).parameters)
            self.assertEqual(params, ["cfg", "tg", "msg", "thread_id", "state"], msg=name)


class TestDeleteCommand(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    def test_delete_clears_context_keeps_topic(self):
        self.cfg.topic_flow = True
        self.state.create_session(100, 77, name="work")
        hist = self.state.get_history(100, 77)
        hist.append({"role": "user", "content": "x"})
        self.state.model[(100, 77)] = "go/m"
        main.handle_message(self.cfg, self.tg, msg(text="/delete", message_thread_id=77), self.state)
        self.assertEqual(self.state.history.get((100, 77)), None)
        self.assertNotIn((100, 77), self.state.model)
        self.assertIn("session deleted", self.tg.sent[-1][1])
        self.assertIn("delete it manually", self.tg.sent[-1][1])
        # topic untouched, but now unbound (no session lives there)
        self.assertEqual(self.state.topics, {})
        self.assertFalse(self.state.session_at(100, 77))

    def test_delete_without_store_ok(self):
        main.handle_message(self.cfg, self.tg, msg(text="/delete"), self.state)
        self.assertIn("session deleted", self.tg.sent[-1][1])


class TestCompact(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    def _seed(self, n=10, thread=None):
        hist = self.state.get_history(100, thread)
        for i in range(n):
            hist.append({"role": "user" if i % 2 == 0 else "assistant",
                         "content": "message %d " % i + "z" * 100})
        return hist

    @mock.patch.object(providers, "chat", return_value=("THE SUMMARY", "", None))
    def test_manual_compact(self, chat_mock):
        self._seed(10)
        main.handle_message(self.cfg, self.tg, msg(text="/compact"), self.state)
        hist = self.state.get_history(100, None)
        self.assertEqual(len(hist), 6)  # 2 summary marker + last 4
        self.assertIn("[Summary of earlier conversation]", hist[0]["content"])
        self.assertIn("THE SUMMARY", hist[0]["content"])
        self.assertEqual(hist[-1]["content"][:9], "message 9")
        self.assertIn("compacted: 10 -> 6 messages (~", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat", return_value=("THE SUMMARY", "", None))
    def test_compact_transcript_keeps_tool_calls(self, chat_mock):
        """Tool arguments live in `tool_calls`, not `content`: a summary built
        from content alone keeps the results with no command behind them."""
        hist = self.state.get_history(100, None)
        hist.extend([
            {"role": "user", "content": "run the build"},
            {"role": "assistant", "content": "Running it.",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "bash",
                                          "arguments": '{"command": "make -j2"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "exit code 0"},
            {"role": "assistant", "content": "built"},
            {"role": "user", "content": "thanks"},
            {"role": "assistant", "content": "anytime"},
        ])
        main.handle_message(self.cfg, self.tg, msg(text="/compact"), self.state)
        sent = chat_mock.call_args[0][3][0]["content"]
        self.assertIn("bash", sent)
        self.assertIn("make -j2", sent)
        self.assertIn("Running it.", sent)  # prose still there, just shortened

    def test_compact_nothing_to_do(self):
        self._seed(3)
        main.handle_message(self.cfg, self.tg, msg(text="/compact"), self.state)
        self.assertIn("nothing to compact", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat",
                       side_effect=[("SUMMARY", "", None), ("final answer", "", None)])
    def test_auto_compact_on_limit(self, chat_mock):
        self.cfg = make_config(context_limit_tokens=100)
        hist = self._seed(8)
        main.handle_message(self.cfg, self.tg, msg(text="continue"), self.state)
        hist = self.state.get_history(100, None)
        self.assertIn("[Summary of earlier conversation]", hist[0]["content"])
        self.assertEqual(hist[-1]["content"], "final answer")
        self.assertEqual(chat_mock.call_count, 2)


    @mock.patch.object(providers, "price_for")
    @mock.patch.object(providers, "chat")
    def test_auto_compact_fires_at_full_window(self, chat_mock, price_mock):
        """Integration: history past the model's window (default threshold =
        100%) triggers compaction without any explicit config."""
        price_mock.return_value = {"context": 300}  # a 300-token model window
        cfg = make_config()
        cfg.context_limit_explicit = False  # as if the key is absent
        chat_mock.side_effect = [("SUMMARY", "", None),
                                 ("final answer", "", None)]
        hist = self.state.get_history(100, None)
        for i in range(8):
            hist.append({"role": "user" if i % 2 == 0 else "assistant",
                         "content": "msg %d " % i + "z" * 300})
        main.handle_message(cfg, self.tg, msg(text="continue"), self.state)
        hist = self.state.get_history(100, None)
        self.assertIn("[Summary of earlier conversation]", hist[0]["content"])
        self.assertEqual(hist[-1]["content"], "final answer")
        self.assertEqual(chat_mock.call_count, 2)


class TestCompactThreshold(unittest.TestCase):
    """Auto-compact threshold: 100% of the model window by default;
    explicit context_limit_tokens overrides; missing metadata falls back."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        disable_titles(self)

    @mock.patch.object(providers, "price_for")
    def test_default_is_full_model_window(self, price_mock):
        price_mock.return_value = {"context": 1048576}
        cfg = make_config()
        cfg.context_limit_explicit = False  # as if the key is absent
        self.assertEqual(main._compact_limit_tokens(cfg, "go/mimo-v2.6-flash"),
                         1048576)

    @mock.patch.object(providers, "price_for")
    def test_explicit_config_value_wins(self, price_mock):
        cfg = make_config(context_limit_tokens=100)
        self.assertEqual(main._compact_limit_tokens(cfg, "go/mimo-v2.6-flash"),
                         100)
        price_mock.assert_not_called()

    @mock.patch.object(providers, "price_for", return_value=None)
    def test_no_metadata_falls_back_to_config(self, price_mock):
        cfg = make_config()
        cfg.context_limit_explicit = False
        self.assertEqual(main._compact_limit_tokens(cfg, "go/unknown-model"),
                         30_000)


class TestStreaming(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    @mock.patch.object(providers, "chat_stream")
    def test_stream_thinking_edit_in_place_answer_as_draft(self, stream_mock):
        """Private-chat streaming (stream_mode = "draft"): the thinking
        block is ONE message edited in place while the model reasons, and
        the answer streams through sendRichMessageDraft - Telegram's modern
        streaming API (an animated preview in the chat, can_stop button),
        never a message that gets rewritten delta by delta. The draft is
        ephemeral, so the finished answer is sent for real at the end."""
        self.cfg = make_config(stream_drafts=True, stream_mode="draft")
        stream_mock.return_value = iter([
            ("reasoning", "think"),
            ("reasoning", "ing more"),
            ("content", "hello "),
            ("content", "**world**"),
        ])
        # fake clock: every delta sees >1.5s elapsed, so each one flushes
        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 10.0
            return clock["t"]

        with mock.patch.object(main.time, "time", fake_time):
            main.handle_message(self.cfg, self.tg, msg(text="hi"), self.state)
        rich = [s for _c, s, _t in self.tg.rich]
        # exactly TWO messages ever created: ONE thinking block + the final
        # answer (the draft never becomes a message while streaming)
        self.assertEqual(len(rich), 2)
        self.assertEqual(len([s for s in rich if "html" in s]), 1)
        self.assertIn("<details>", rich[0]["html"])
        self.assertEqual(rich[1], {"markdown": "hello **world**"})
        # the thinking streamed by editing ITS message in place...
        self.assertIn("thinking more", self.tg.edits[0][2].get("html", ""))
        self.assertTrue(all(mid == 1000 + 1
                            for _c, mid, _s in self.tg.edits))  # one block
        # ...and the answer streamed as a draft: same draft id (animated),
        # can_stop -> Telegram's native Stop button
        drafts = [s for _c, _d, s, _t, _cs in self.tg.drafts]
        self.assertEqual(drafts[0], {"markdown": "hello "})
        self.assertEqual(drafts[-1], {"markdown": "hello **world**"})
        self.assertEqual(len({d for _c, d, _s, _t, _cs in self.tg.drafts}), 1)
        self.assertTrue(all(cs for _c, _d, _s, _t, cs in self.tg.drafts))
        # final delivery: the thinking block edited in place (full text),
        # the answer persisted with sendRichMessage - no second block
        self.assertIn("thinking more", self.tg.edits[-1][2].get("html", ""))
        # history has exactly one user + one assistant turn
        hist = self.state.get_history(100, None)
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])

    @mock.patch.object(providers, "chat_stream")
    def test_interleaved_reasoning_stays_in_the_thinking_block(self, stream_mock):
        """A model that keeps thinking BETWEEN answer chunks must not leak
        thought text into the answer (it used to read as a second thinking
        block), and the thinking block must not freeze half-finished."""
        stream_mock.return_value = iter([
            ("reasoning", "first thoughts"),
            ("content", "part one "),
            ("reasoning", " ...and second thoughts"),
            ("content", "part two"),
        ])
        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 10.0
            return clock["t"]

        with mock.patch.object(main.time, "time", fake_time):
            main.handle_message(make_config(stream_drafts=True), self.tg,
                                msg(text="hi"), self.state)
        # the answer carries ONLY content deltas
        self.assertEqual([s for _c, _d, s, _t, _cs in self.tg.drafts][-1],
                         {"markdown": "part one part two"})
        self.assertIn({"markdown": "part one part two"},
                      [s for _c, s, _t in self.tg.rich])
        # ONE thinking block, edited in place with the whole reasoning
        htmls = [s["html"] for _c, s, _t in self.tg.rich if "html" in s]
        self.assertEqual(len(htmls), 1)
        self.assertIn("and second thoughts", self.tg.edits[-1][2].get("html", ""))
        # and the persisted history keeps thinking out of the answer
        hist = self.state.get_history(100, None)
        self.assertEqual(hist[-1]["content"], "part one part two")

    @staticmethod
    def _not_modified():
        """Telegram's no-op answer: the edit was identical to the message."""
        return main.tg_mod.TelegramError(
            "editMessageText -> HTTP 400: {\"ok\":false,\"error_code\":400,"
            "\"description\":\"Bad Request: message is not modified: "
            "specified new message content and reply markup are exactly the "
            "same as a current content and reply markup of the message\"}")

    @mock.patch.object(providers, "chat_stream")
    def test_noop_edit_does_not_kill_the_stream(self, stream_mock):
        """'message is not modified' means our flush repeated unchanged
        content - a no-op, not a failure. It used to disable live updates
        for the rest of the turn (stream froze mid-answer)."""
        stream_mock.return_value = iter([
            ("reasoning", "thinking"),   # delta 1 -> block created
            ("content", "hello"),        # delta 2 -> thinking edit = no-op
            ("content", " world"),       # delta 3 -> same
        ])
        tg = FakeTG()
        real_edit = FakeTG.edit_rich

        def noop_on_html(chat_id, message_id, markdown=None, html=None):
            if html is not None:  # every thinking flush repeats the same text
                raise self._not_modified()
            return real_edit(tg, chat_id, message_id, markdown=markdown,
                             html=html)

        tg.edit_rich = noop_on_html
        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 10.0
            return clock["t"]

        with self.assertLogs("keirai", level="INFO") as cm:
            with mock.patch.object(main.time, "time", fake_time):
                main.handle_message(make_config(stream_drafts=True), tg,
                                    msg(text="hi"), self.state)
        self.assertFalse(any("live updates disabled" in line
                             for line in cm.output))
        # the stream kept running: one thinking block, answer delivered
        self.assertEqual(len([s for _c, s, _t in tg.rich if "html" in s]), 1)
        self.assertIn({"markdown": "hello world"},
                      [s for _c, s, _t in tg.rich])
        self.assertTrue(tg.drafts)  # answer streamed after the no-op edits

    @mock.patch.object(providers, "chat_stream")
    def test_noop_thinking_edit_does_not_duplicate_the_block(self, stream_mock):
        """When the round's thinking already equals the final block the edit
        is a no-op - the block is DONE. Falling back to a plain send there
        put a duplicate block next to the streamed one (and leaked raw HTML
        into a normal message)."""
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        with open(path, "w") as f:
            f.write("DATA")
        self.addCleanup(os.unlink, path)
        stream_mock.side_effect = [
            iter([("reasoning", "checking"),
                  ("tool_calls", [{"id": "c1", "name": "read_file",
                                   "arguments": {"path": path}}])]),
            iter([("reasoning", "all done"), ("content", "the answer")]),
        ]
        tg = FakeTG()
        real_edit = FakeTG.edit_rich

        def noop_on_html(chat_id, message_id, markdown=None, html=None):
            if html is not None:
                raise self._not_modified()
            return real_edit(tg, chat_id, message_id, markdown=markdown,
                             html=html)

        tg.edit_rich = noop_on_html
        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 10.0
            return clock["t"]

        with mock.patch.object(main.time, "time", fake_time):
            main.handle_message(make_config(stream_drafts=True), tg,
                                msg(text="read it"), self.state)
        # ONE block per round, nothing deleted, and neither round's
        # reasoning leaked into a plain message (that was the old
        # 'falling back' duplicate-block path)
        self.assertEqual(len([s for _c, s, _t in tg.rich if "html" in s]), 2)
        self.assertFalse(any("checking" in t or "all done" in t
                             for _c, t, _th in tg.sent))
        self.assertEqual(tg.deleted, [])

    @mock.patch.object(providers, "chat_stream")
    def test_noop_final_edit_keeps_the_messages(self, stream_mock):
        """Final delivery with unchanged content must not delete the streamed
        messages and re-send them - the no-op edit IS the delivery."""
        stream_mock.return_value = iter([("reasoning", "hmm"),
                                         ("content", "hello world")])
        tg = FakeTG()
        tg.edit_rich = lambda *a, **k: (_ for _ in ()).throw(
            self._not_modified())
        g = {"id": 100, "type": "supergroup"}
        with self.assertLogs("keirai", level="INFO") as cm:
            with mock.patch.object(main.time, "time", lambda: 1000.0):
                main.handle_message(make_config(stream_drafts=True), tg,
                                    msg(text="hi", chat=g), self.state)
        self.assertEqual(tg.deleted, [])          # kept, not re-created
        self.assertEqual(len(tg.rich), 2)         # nothing sent twice
        self.assertFalse(any("final live edit failed" in line
                             for line in cm.output))

    @mock.patch.object(providers, "chat_stream")
    def test_blank_answer_is_never_sent_rich(self, stream_mock):
        """A whitespace-only flush used to reach Telegram as
        RICH_MESSAGE_EMPTY (HTTP 400) and kill live updates for the turn."""
        stream_mock.return_value = iter([("content", "\n"),
                                         ("content", "ok")])
        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 10.0
            return clock["t"]

        with mock.patch.object(main.time, "time", fake_time):
            main.handle_message(make_config(stream_drafts=True), self.tg,
                                msg(text="hi"), self.state)
        for _c, _d, s, _t, _cs in self.tg.drafts:
            self.assertTrue((s.get("markdown") or "").strip())
        for _c, s, _t in self.tg.rich:
            if "markdown" in s:
                self.assertTrue(s["markdown"].strip())
        self.assertIn({"markdown": "ok"}, [s for _c, s, _t in self.tg.rich])

    @mock.patch.object(providers, "chat_stream")
    def test_group_stream_edits_two_messages(self, stream_mock):
        """Groups stream exactly like private chats: thinking message +
        answer message, both edited in place (no drafts anywhere)."""
        stream_mock.return_value = iter([
            ("reasoning", "hmm"),
            ("content", "hello "),
            ("content", "**world**"),
        ])
        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 10.0
            return clock["t"]

        with mock.patch.object(main.time, "time", fake_time):
            main.handle_message(make_config(stream_drafts=True), self.tg,
                                msg(text="hi", chat={"id": 100, "type": "supergroup"}),
                                self.state)
        # created ONCE (html thinking + markdown answer), then only edited
        html_msgs = [s for _c, s, _t in self.tg.rich if "html" in s]
        self.assertEqual(len(html_msgs), 1)
        self.assertEqual(len(self.tg.rich), 2)
        self.assertIn({"markdown": "hello **world**"},
                      [s for _c, _mid, s in self.tg.edits])

    @mock.patch.object(providers, "chat_stream")
    def test_tool_round_keeps_one_thinking_block_per_round(self, stream_mock):
        """A tool round turns the streamed thinking into THE block: the
        streamed message is edited into its final form (never re-sent, so
        there is no second, half-finished block next to it), and the next
        round streams into fresh messages BELOW the tool lines."""
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        with open(path, "w") as f:
            f.write("DATA")
        self.addCleanup(os.unlink, path)
        stream_mock.side_effect = [
            iter([("reasoning", "checking"),
                  ("tool_calls", [{"id": "c1", "name": "read_file",
                                   "arguments": {"path": path}}])]),
            iter([("reasoning", "all done"),
                  ("content", "the answer")]),
        ]
        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 10.0
            return clock["t"]

        with mock.patch.object(main.time, "time", fake_time):
            main.handle_message(make_config(stream_drafts=True), self.tg,
                                msg(text="read it"), self.state)
        # exactly ONE thinking message per round (2 total), nothing deleted:
        # round 1's streamed block became the permanent block in place
        self.assertEqual(len([s for _c, s, _t in self.tg.rich
                              if "html" in s]), 2)
        self.assertEqual(self.tg.deleted, [])
        edits = [(mid, s) for _c, mid, s in self.tg.edits]
        self.assertTrue(any(mid == 1000 + 1 and "checking" in s.get("html", "")
                            for mid, s in edits))
        # rich sends: round 1 thinking | round 2 thinking | the answer
        # (drafts are ephemeral -> persisted once at the end)
        self.assertEqual(len(self.tg.rich), 3)
        self.assertEqual(self.tg.rich[-1][1], {"markdown": "the answer"})
        self.assertIn("all done", edits[-1][1].get("html", ""))  # r2 block
        self.assertEqual(edits[-1][0], 1000 + 2)
        self.assertTrue(self.tg.drafts)  # round 2's answer streamed live

    @mock.patch.object(providers, "chat_stream")
    def test_tool_round_drops_answer_view_keeps_thinking(self, stream_mock):
        """Edit mode (groups, no drafts): the round's answer view is FINALIZED
        as a real message when the round turns out to call a tool (the prose
        must not vanish when the tool line appears), while its thinking
        message stays - it is that round's permanent block now."""
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        with open(path, "w") as f:
            f.write("DATA")
        self.addCleanup(os.unlink, path)
        stream_mock.side_effect = [
            iter([("reasoning", "checking"),
                  ("content", "on it "),
                  ("tool_calls", [{"id": "c1", "name": "read_file",
                                   "arguments": {"path": path}}])]),
            iter([("reasoning", "all done"),
                  ("content", "the answer")]),
        ]
        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 10.0
            return clock["t"]

        g = {"id": 100, "type": "supergroup"}
        with mock.patch.object(main.time, "time", fake_time):
            main.handle_message(make_config(stream_drafts=True), self.tg,
                                msg(text="read it", chat=g), self.state)
        # nothing was deleted: the round's prose kept its message (finalized
        # to what had streamed) and round 1's thinking block stayed a
        # permanent, fully edited block
        self.assertEqual(self.tg.deleted, [])
        edits = [(mid, s) for _c, mid, s in self.tg.edits]
        self.assertIn((1000 + 2, {"markdown": "on it"}), edits)
        # groups never stream drafts (the Bot API rejects them there)
        self.assertEqual(self.tg.drafts, [])
        edits = [(mid, s) for _c, mid, s in self.tg.edits]
        self.assertTrue(any(mid == 1000 + 1 and "checking" in s.get("html", "")
                            for mid, s in edits))
        # round 2 streamed into fresh messages below the tool lines and its
        # final delivery only edited them - no new send at all
        self.assertEqual(len(self.tg.rich), 4)
        self.assertEqual(edits[-1], (1000 + 4, {"markdown": "the answer"}))

    @mock.patch.object(providers, "chat_stream")
    def test_stream_with_thinking_off_never_shows_reasoning(self, stream_mock):
        """/thinking off gates the LIVE stream too: no reasoning message is
        ever created and the answer takes the single message."""
        stream_mock.return_value = iter([("reasoning", "secret"),
                                         ("content", "hello")])
        self.state.set_thinking(42, False)
        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 10.0
            return clock["t"]

        with mock.patch.object(main.time, "time", fake_time):
            main.handle_message(make_config(stream_drafts=True), self.tg,
                                msg(text="hi"), self.state)
        self.assertEqual([s for _c, s, _t in self.tg.rich],
                         [{"markdown": "hello"}])
        self.assertNotIn("secret", str(self.tg.rich) + str(self.tg.edits))

    @mock.patch.object(providers, "chat")
    @mock.patch.object(providers, "chat_stream", side_effect=providers.ProviderError("nope"))
    def test_stream_failure_falls_back(self, _stream, chat_mock):
        self.cfg = make_config(stream_drafts=True)
        chat_mock.return_value = ("blocking answer", "", None)
        main.handle_message(self.cfg, self.tg, msg(text="hi"), self.state)
        chat_mock.assert_called_once()
        self.assertEqual(self.tg.rich[-1][1], {"markdown": "blocking answer"})
        hist = self.state.get_history(100, None)
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])

    @mock.patch.object(providers, "chat")
    @mock.patch.object(providers, "chat_stream")
    def test_stream_disabled_config(self, stream_mock, chat_mock):
        self.cfg = make_config(stream_drafts=False)
        chat_mock.return_value = ("answer", "", None)
        main.handle_message(self.cfg, self.tg, msg(text="hi"), self.state)
        stream_mock.assert_not_called()
        chat_mock.assert_called_once()


class TestPersistence(unittest.TestCase):
    def setUp(self):
        import sessions
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self.store = sessions.Store(os.path.join(self._dir.name, "test.db"))

    def tearDown(self):
        self.store.close()
        self._dir.cleanup()

    def test_roundtrip(self):
        state = main.State(True, store=self.store)
        hist = state.get_history(100, 77)
        hist.extend([{"role": "user", "content": "q"},
                     {"role": "assistant", "content": "a"}])
        state.model[(100, 77)] = "go/m"
        state.persist(100, 77)
        state.topics.setdefault(100, []).append(77)
        self.store.add_topic(100, 77, "work")

        state2 = main.State(True, store=self.store)  # simulated restart
        self.assertEqual(state2.get_history(100, 77), hist)
        self.assertEqual(state2.model_for(100, 77, "x"), "go/m")
        self.assertEqual(state2.topics, {100: [77]})
        self.assertEqual(state2.session_count[100], 2)

    def test_main_chat_uses_zero_sentinel(self):
        state = main.State(True, store=self.store)
        state.get_history(5, None).append({"role": "user", "content": "hi"})
        state.persist(5, None)
        self.assertEqual([r[:2] for r in self.store.list_sessions()], [(5, 0)])
        state2 = main.State(True, store=self.store)
        self.assertEqual(state2.get_history(5, None)[0]["content"], "hi")

    def test_delete_persists(self):
        state = main.State(True, store=self.store)
        state.get_history(9, 3).append({"role": "user", "content": "x"})
        state.persist(9, 3)
        self.store.delete_session(9, 3)
        state2 = main.State(True, store=self.store)
        self.assertEqual(state2.history.get((9, 3)), None)

    def test_delete_chat_returns_topics(self):
        self.store.add_topic(42, 11, "a")
        self.store.add_topic(42, 22, "b")
        self.store.save_session(42, 11, None, [{"role": "user", "content": "x"}])
        got = self.store.delete_chat(42)
        self.assertEqual(sorted(got), [11, 22])
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(self.store.load_topics(), [])

    def test_corrupt_row_skipped(self):
        self.store.conn.execute(
            "INSERT INTO sessions VALUES (1, 0, NULL, 'not-json', 0)")
        self.store.conn.commit()
        self.assertEqual(self.store.load_sessions(), [])

    def test_threaded_writes(self):
        """Session workers run concurrently: the shared connection must
        accept interleaved writes from several threads (lock + no
        check_same_thread)."""
        import threading
        errs = []

        def worker(cid):
            try:
                for i in range(20):
                    self.store.save_session(
                        cid, 0, None, [{"role": "user", "content": "m%d" % i}])
                    self.store.ensure_meta(cid, 0)
                    self.store.record_usage(cid, 0, 0.001,
                                            {"tokens_in": 1, "tokens_out": 2})
            except Exception as e:  # pragma: no cover
                errs.append(e)

        threads = [threading.Thread(target=worker, args=(c,)) for c in (1, 2, 3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errs, [])
        self.assertEqual({r[0] for r in self.store.list_sessions()}, {1, 2, 3})


class TestCLI(unittest.TestCase):
    def setUp(self):
        import sessions
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._dir.name, "cli.db")
        store = sessions.Store(self.db)
        store.save_session(100, 0, "go/m", [{"role": "user", "content": "a"}] * 3)
        store.save_session(100, 77, None, [{"role": "user", "content": "b"}])
        store.add_topic(100, 77, "old name")
        store.close()

    def tearDown(self):
        self._dir.cleanup()

    def _run(self, argv):
        import cli
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with mock.patch.object(cli.config_mod, "load", return_value=make_config()), \
             mock.patch.object(cli.config_mod, "sessions_path", return_value=self.db), \
             redirect_stdout(buf):
            code = cli.run(argv)
        return code, buf.getvalue()

    def test_list(self):
        code, out = self._run(["list"])
        self.assertEqual(code, 0)
        self.assertIn("chat=100 thread=main", out)
        self.assertIn("msgs=3", out)
        self.assertIn("old name", out)

    def test_delete(self):
        code, out = self._run(["delete", "100"])
        self.assertEqual(code, 0)
        self.assertIn("deleted context", out)
        code, out = self._run(["list"])
        self.assertIn("thread=77", out)
        self.assertNotIn("thread=main", out)

    def test_delete_missing(self):
        code, out = self._run(["delete", "999"])
        self.assertEqual(code, 1)
        self.assertIn("no such session", out)

    def test_rename(self):
        code, out = self._run(["rename", "100:77", "new name"])
        self.assertEqual(code, 0)
        import sessions
        store = sessions.Store(self.db)
        names = {(c, t): n for c, t, n in store.load_topics()}
        store.close()
        self.assertEqual(names[(100, 77)], "new name")


class TestStop(unittest.TestCase):
    """The /stop command: watcher interception + turn interruption."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config(stream_drafts=True)
        self.q = queue.Queue()
        disable_titles(self)  # titling is its own class (and hits network)

    def test_stop_cmd_matching(self):
        self.assertTrue(main._is_stop_cmd(msg(text="/stop")))
        self.assertTrue(main._is_stop_cmd(msg(text="/stop@vdt_keirai_bot")))
        self.assertFalse(main._is_stop_cmd(msg(text="/stoppers")))
        self.assertFalse(main._is_stop_cmd(msg(text="/go stop")))

    def test_route_queues_when_idle(self):
        m = msg(text="/stop")
        consumed = main._route_update(self.state, self.q, {"message": m})
        self.assertFalse(consumed)
        self.assertEqual(self.q.qsize(), 1)
        self.assertFalse(self.state.interrupted((100, None)))

    def test_route_consumes_during_active_turn(self):
        self.state.begin_turn((100, None))
        consumed = main._route_update(self.state, self.q,
                                      {"message": msg(text="/stop")})
        self.assertTrue(consumed)
        self.assertTrue(self.q.empty())
        self.assertTrue(self.state.interrupted((100, None)))

    def test_native_stop_button_interrupts_the_turn(self):
        """sendRichMessageDraft(can_stop=True) shows Telegram's own Stop
        button; the press arrives as stopped_message_generation and must
        interrupt the very same session turn."""
        self.state.begin_turn((100, None))
        upd = {"stopped_message_generation":
               {"chat": {"id": 100, "type": "private"}, "draft_id": 4242}}
        self.assertTrue(main._route_update(self.state, self.q, upd))
        self.assertTrue(self.q.empty())          # never queued as a message
        self.assertTrue(self.state.interrupted((100, None)))

    def test_native_stop_in_topic_targets_that_slot(self):
        self.state.begin_turn((100, 7))
        upd = {"stopped_message_generation":
               {"chat": {"id": 100, "type": "supergroup"},
                "message_thread_id": 7, "draft_id": 1}}
        self.assertTrue(main._route_update(self.state, self.q, upd))
        self.assertTrue(self.state.interrupted((100, 7)))
        self.assertFalse(self.state.interrupted((100, None)))

    def test_native_stop_with_nothing_running_is_harmless(self):
        upd = {"stopped_message_generation":
               {"chat": {"id": 100, "type": "private"}, "draft_id": 5}}
        self.assertTrue(main._route_update(self.state, self.q, upd))
        self.assertTrue(self.q.empty())          # not a message, no crash
        self.assertFalse(self.state.interrupted((100, None)))

    def test_poll_thread_never_dies(self):
        """THE 'bot stops responding' bug: the poll thread is the only
        producer of updates. If it raises, the process keeps running (so
        systemd's Restart=never fires) while the bot goes silent forever.
        the shape seen in the logs:
            Exception in thread updates: ... TimeoutError: The read
            operation timed out
        A read timeout escaped telegram.call()'s handlers (urlopen only
        wraps send-phase errors in URLError). Both TelegramError and any
        unexpected exception must be caught and retried, forever."""
        calls = {"n": 0}

        class FakeTG:
            def get_updates(self, offset=None, timeout=25):
                calls.setdefault("timeouts", []).append(timeout)
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ValueError("malformed update")     # unexpected bug
                if calls["n"] == 2:
                    raise main.tg_mod.TelegramError("down")  # known path
                raise KeyboardInterrupt  # BaseException -> leaves the loop

        with mock.patch.object(main.time, "sleep", lambda s: None):
            with self.assertRaises(KeyboardInterrupt):
                main._watch_updates(self.cfg, FakeTG(), self.state, self.q)
        self.assertEqual(calls["n"], 3)  # kept polling through both failures
        self.assertEqual(calls["timeouts"][0], 0)  # startup probe is non-blocking
        self.assertTrue(self.q.empty())

    def test_one_bad_update_does_not_wedge_polling(self):
        """A malformed update must not stop the next one from being routed
        (and the offset still advances, so it is not re-delivered forever)."""
        pending = [{"update_id": 1}, {"update_id": 2}]
        seen_offsets = []

        class FakeTG:
            def get_updates(self, offset=None, timeout=25):
                seen_offsets.append(offset)
                if pending:
                    return [pending.pop(0)]
                raise KeyboardInterrupt

        real_route = main._route_update

        def flaky(state, out_q, upd):
            if upd["update_id"] == 1:
                raise KeyError("bad update")
            return real_route(state, out_q, upd)

        with mock.patch.object(main, "_route_update", side_effect=flaky):
            with mock.patch.object(main.time, "sleep", lambda s: None):
                with self.assertRaises(KeyboardInterrupt):
                    main._watch_updates(self.cfg, FakeTG(), self.state, self.q)
        self.assertEqual(seen_offsets[:2], [None, 2])  # offset advanced past #1
        self.assertEqual(self.q.qsize(), 1)        # #2 was routed normally

    def test_stop_replies_when_idle(self):
        main.handle_message(self.cfg, self.tg, msg(text="/stop"), self.state)
        self.assertIn("nothing is running", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat_stream")
    def test_interrupt_mid_stream_records_context(self, stream_mock):
        state = self.state

        def gen():
            yield "reasoning", "thinking hard"
            state.request_interrupt((100, None))  # user sends /stop now
            yield "content", "partial answer will not finish"
            yield "content", " - rest of it"

        stream_mock.return_value = gen()
        main.handle_message(self.cfg, self.tg, msg(text="hi"), state)
        hist = state.get_history(100, None)
        # marker is in context for the model's next turn
        self.assertIn(main.INTERRUPT_NOTE, hist[-1]["content"])
        self.assertIn("partial answer", hist[-1]["content"])
        # user was told; no final full answer was delivered
        self.assertTrue(any("stopped" in t for _c, t, _th in self.tg.sent))
        # edit-mode placeholder was created while thinking, then dropped on /stop
        self.assertTrue(self.tg.rich)
        self.assertTrue(self.tg.deleted)
        # turn bookkeeping cleared
        self.assertNotIn((100, None), state.active_turns)
        self.assertNotIn((100, None), state.interrupts)

    @mock.patch.object(providers, "chat_stream")
    def test_interrupt_drops_live_placeholder(self, stream_mock):
        """Edit-mode placeholder is deleted when the turn is stopped."""
        def gen():
            yield "reasoning", "hmm"
            self.state.request_interrupt((100, None))
            yield "content", "started"

        stream_mock.return_value = gen()
        g = {"id": 100, "type": "supergroup"}
        main.handle_message(self.cfg, self.tg, msg(text="hi", chat=g), self.state)
        self.assertTrue(self.tg.rich)          # placeholder was created
        self.assertTrue(self.tg.deleted)       # ...and cleaned up on /stop
        self.assertTrue(any("stopped" in t for _c, t, _th in self.tg.sent))


class TestToolLoop(unittest.TestCase):
    """LLM tool calls: execute, feed back, then final answer."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config(stream_drafts=False)
        disable_titles(self)

    @mock.patch.object(providers, "chat")
    def test_tool_round_then_answer(self, chat_mock):
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        with open(path, "w") as f:
            f.write("SECRET-CONTENT-42")
        self.addCleanup(os.unlink, path)
        chat_mock.side_effect = [
            ("", "", [{"id": "c1", "name": "read_file",
                       "arguments": {"path": path}}]),
            ("the content is SECRET-CONTENT-42", "", None),
        ]
        main.handle_message(self.cfg, self.tg, msg(text="what's inside?"),
                            self.state)
        # progress line shows the tool being run
        self.assertTrue(any("read_file" in t for _c, t, _th in self.tg.sent))
        # final answer delivered
        self.assertEqual(self.tg.rich[-1][1],
                         {"markdown": "the content is SECRET-CONTENT-42"})
        # history keeps the whole round: user + tool_calls + result + answer
        hist = self.state.get_history(100, None)
        self.assertEqual([m["role"] for m in hist],
                         ["user", "assistant", "tool", "assistant"])
        self.assertIn("SECRET-CONTENT-42", hist[2]["content"])
        self.assertEqual(hist[1]["tool_calls"][0]["function"]["name"],
                         "read_file")
        # second request carried the tool result + tool specs
        second = chat_mock.call_args_list[1]
        msgs = second[0][3]
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("SECRET-CONTENT-42", tool_msgs[0]["content"])
        self.assertEqual(tool_msgs[0]["tool_call_id"], "c1")
        self.assertTrue(second[1]["tools"])

    @mock.patch.object(providers, "chat")
    def test_tools_disabled_no_specs(self, chat_mock):
        self.cfg = make_config(tools_enabled=False, stream_drafts=False)
        chat_mock.return_value = ("plain", "", None)
        main.handle_message(self.cfg, self.tg, msg(text="hi"), self.state)
        self.assertIsNone(chat_mock.call_args[1]["tools"])

    @mock.patch.object(providers, "chat")
    def test_tool_rounds_run_without_cap(self, chat_mock):
        """No round cap (user directive): tool rounds continue until the model
        stops asking for tools - the old cap's tools-off final call is what
        produced text-form tool calls in chat."""
        tc = [{"id": "c1", "name": "read_file", "arguments": {"path": "missing.txt"}}]
        rounds = 9  # well past the old 6-round cap
        chat_mock.side_effect = [("", "", tc) for _ in range(rounds)] + \
            [("final answer", "", None)]
        main.handle_message(self.cfg, self.tg, msg(text="loop me"), self.state)
        self.assertEqual(self.tg.rich[-1][1], {"markdown": "final answer"})
        self.assertEqual(chat_mock.call_count, rounds + 1)
        # every round's tool result was fed back into the conversation
        last = chat_mock.call_args_list[-1][0][3]
        self.assertEqual(len([m for m in last if m.get("role") == "tool"]),
                         rounds)

    @mock.patch.object(providers, "chat")
    def test_text_form_tool_call_recovered(self, chat_mock):
        """The model sometimes writes the call as TEXT instead
        of using the tools API and raw markup reached chat. The loop must
        parse it, run the round, and never show markup (tag built by
        concatenation - literal closing-parameter strings get cut in transit).
        """
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        with open(path, "w") as f:
            f.write("SECRET-CONTENT-42")
        self.addCleanup(os.unlink, path)
        cl = "<" + "/parameter>"
        markup = ("<tool_call><function=read_file><parameter=path>" + path
                  + cl + "<" + "/function></tool_call>")
        chat_mock.side_effect = [
            ("checking " + markup, "", None),
            ("the content is SECRET-CONTENT-42", "", None),
        ]
        main.handle_message(self.cfg, self.tg, msg(text="read it"), self.state)
        # recovered call executed: round 2 carries the tool result
        second = chat_mock.call_args_list[1]
        tools_seen = [m for m in second[0][3] if m.get("role") == "tool"]
        self.assertEqual(len(tools_seen), 1)
        self.assertIn("SECRET-CONTENT-42", tools_seen[0]["content"])
        # history: proper tool_calls round, markup stripped
        hist = self.state.get_history(100, None)
        self.assertEqual([m["role"] for m in hist],
                         ["user", "assistant", "tool", "assistant"])
        self.assertEqual(hist[1]["tool_calls"][0]["function"]["name"],
                         "read_file")
        self.assertNotIn("<tool_call>", hist[1]["content"])
        # final answer delivered, no raw markup anywhere in chat
        self.assertEqual(self.tg.rich[-1][1],
                         {"markdown": "the content is SECRET-CONTENT-42"})
        for _c, t, _th in self.tg.sent:
            self.assertNotIn("<function", t)

    @mock.patch.object(providers, "chat")
    def test_assistant_reasoning_key_always_present(self, chat_mock):
        """go/deepseek-v4.1-flash refuses a history where SOME assistant
        messages carry reasoning_content and others do not: "The
        reasoning_content in the thinking mode must be passed back to the
        API." Every assistant message in the outgoing payload must have the
        key - an empty string is accepted."""
        tc = [{"id": "c1", "name": "read_file", "arguments": {"path": "no.txt"}}]
        chat_mock.side_effect = [("", "", tc),   # round 1: thinking produced
                                 ("final answer", "", None)]
        main.handle_message(self.cfg, self.tg, msg(text="go"), self.state)
        self.assertGreaterEqual(chat_mock.call_count, 2)
        for call in chat_mock.call_args_list:
            for m in call[0][3]:
                if m.get("role") == "assistant":
                    self.assertIn("reasoning_content", m)
                    self.assertIsInstance(m["reasoning_content"], str)

    @mock.patch.object(providers, "chat")
    def test_legacy_history_still_sends(self, chat_mock):
        """A session stored before this rule keeps working: the outgoing
        payload gains reasoning_content: "" rather than 400ing."""
        hist = self.state.get_history(100, None)
        hist.append({"role": "assistant", "content": "old answer"})  # no key
        chat_mock.return_value = ("new answer", "", None)
        main.handle_message(self.cfg, self.tg, msg(text="hi"), self.state)
        assistants = [m for m in chat_mock.call_args[0][3]
                      if m.get("role") == "assistant"]
        self.assertTrue(assistants)
        for m in assistants:
            self.assertEqual(m["reasoning_content"], "")

    @mock.patch.object(providers, "chat")
    def test_assistant_reasoning_key_always_present(self, chat_mock):
        """go/deepseek-v4.1-flash refuses a history where SOME assistant
        messages carry reasoning_content and others do not: "The
        reasoning_content in the thinking mode must be passed back to the
        API." Every assistant message in the outgoing payload must have the
        key - an empty string is accepted."""
        tc = [{"id": "c1", "name": "read_file", "arguments": {"path": "no.txt"}}]
        chat_mock.side_effect = [("", "", tc),   # round 1: nothing thought
                                 ("final answer", "", None)]
        main.handle_message(self.cfg, self.tg, msg(text="go"), self.state)
        self.assertGreaterEqual(chat_mock.call_count, 2)
        for call in chat_mock.call_args_list:
            for m in call[0][3]:
                if m.get("role") == "assistant":
                    self.assertIn("reasoning_content", m)
                    self.assertIsInstance(m["reasoning_content"], str)

    @mock.patch.object(providers, "chat")
    def test_legacy_history_still_sends(self, chat_mock):
        """A session stored before this rule keeps working: the outgoing
        payload gains reasoning_content: "" rather than 400ing."""
        hist = self.state.get_history(100, None)
        hist.append({"role": "assistant", "content": "old answer"})  # no key
        chat_mock.return_value = ("new answer", "", None)
        main.handle_message(self.cfg, self.tg, msg(text="hi"), self.state)
        assistants = [m for m in chat_mock.call_args[0][3]
                      if m.get("role") == "assistant"]
        self.assertTrue(assistants)
        for m in assistants:
            self.assertEqual(m["reasoning_content"], "")

    @mock.patch.object(providers, "chat")
    def test_unparsable_markup_is_delivered_not_retried(self, chat_mock):
        """A reply leaking markup no grammar can parse is NOT re-asked and
        NOT guessed at: it is delivered as plain text and logged, so an
        unseen dialect stays visible in chat and can be supported later
        (user directive)."""
        # an invoke with no closing tag - no grammar matches it, and it is
        # not DSML, so sanitize() leaves it alone on purpose
        broken = ("<function_calls><antml:invoke name=\"read_file\">"
                  "<antml:parameter name=\"path\">SECRET-MARKUP")
        chat_mock.return_value = (broken, "", None)
        main.handle_message(self.cfg, self.tg, msg(text="read it"), self.state)
        # exactly one request - no retry, no correction
        self.assertEqual(chat_mock.call_count, 1)
        # no tool ran, so no tool round in the session
        self.assertEqual([m["role"] for m in self.state.get_history(100, None)],
                         ["user", "assistant"])
        # the reply reached chat in full: an UNSEEN dialect stays visible
        self.assertIn("SECRET-MARKUP", self.tg.rich[-1][1]["markdown"])

    @mock.patch.object(providers, "chat")
    def test_unparsable_leak_never_reaches_history(self, chat_mock):
        """A reply that leaks markup and is never parsed must not be stored:
        the model copies its own degraded output forward, so one leak poisons
        every later turn."""
        damaged = ("here: <｜DSML｜ invoke name=\"bash\">"
                   "<｜DSML｜ parameter name=\"command\" string=\"true\">pwd")
        chat_mock.return_value = (damaged, "", None)
        main.handle_message(self.cfg, self.tg, msg(text="go"), self.state)
        for m in self.state.get_history(100, None):
            if m.get("role") == "assistant":
                self.assertNotIn("DSML", m["content"])
        self.assertNotIn("DSML", self.tg.rich[-1][1]["markdown"])

    @mock.patch.object(providers, "chat")
    def test_unavailable_tool_is_answered_not_executed(self, chat_mock):
        """A call for a tool this request never offered - a hallucinated
        name, one turned off in config, or markup merely QUOTED while
        describing a format - is answered with "tool unavailable" rather
        than skipped: the round stays valid, the model gets to correct
        itself, and nothing runs."""
        quoted = ("<｜DSML｜ invoke name=\"rm\">"
                  "<｜DSML｜ parameter name=\"path\" string=\"true\">/etc"
                  "</｜DSML｜ parameter></｜DSML｜ invoke>")
        chat_mock.side_effect = [("here is the format: " + quoted, "", None),
                                 ("ok, plain answer", "", None)]
        main.handle_message(self.cfg, self.tg, msg(text="explain dsml"),
                            self.state)
        self.assertEqual(chat_mock.call_count, 2)
        # the round WAS answered - one tool result, naming the offered set
        results = [m for m in chat_mock.call_args_list[1][0][3]
                   if m.get("role") == "tool"]
        self.assertEqual(len(results), 1)
        self.assertIn("tool unavailable: rm", results[0]["content"])
        self.assertIn("read_file", results[0]["content"])
        # nothing executed, so no 🔧 line was shown for it
        self.assertFalse(any("rm" in t for _c, t, _th in self.tg.sent))
        # session stays valid: no dangling tool_calls left in history
        hist = self.state.get_history(100, None)
        self.assertEqual([m["role"] for m in hist],
                         ["user", "assistant", "tool", "assistant"])
        self.assertNotIn("DSML", hist[1]["content"])

    @mock.patch.object(providers, "chat")
    def test_dsml_tool_call_executed_end_to_end(self, chat_mock):
        """DeepSeek DSML leaking into `content` (V4.1 - a space inside every
        tag): the loop parses it, runs the round, feeds the result back, and
        no envelope markup ever reaches chat."""
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        with open(path, "w") as f:
            f.write("SECRET-CONTENT-42")
        self.addCleanup(os.unlink, path)
        dsml = ("<｜DSML｜ calls>\n"
                "<｜DSML｜ invoke name=\"read_file\">\n"
                "<｜DSML｜ parameter name=\"path\" string=\"true\">" + path
                + "</｜DSML｜ parameter>\n"
                "</｜DSML｜ invoke>\n"
                "</｜DSML｜ calls>")
        chat_mock.side_effect = [("checking " + dsml, "", None),
                                 ("the content is SECRET-CONTENT-42", "",
                                  None)]
        main.handle_message(self.cfg, self.tg, msg(text="read it"), self.state)
        # round 2 carries the tool result -> the call really executed
        second = chat_mock.call_args_list[1]
        tool_msgs = [m for m in second[0][3] if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("SECRET-CONTENT-42", tool_msgs[0]["content"])
        # ...recorded in history as a proper tool_calls round
        hist = self.state.get_history(100, None)
        self.assertEqual([m["role"] for m in hist],
                         ["user", "assistant", "tool", "assistant"])
        self.assertEqual(hist[1]["tool_calls"][0]["function"]["name"],
                         "read_file")
        # nothing from the envelope reaches the user
        self.assertEqual(self.tg.rich[-1][1],
                         {"markdown": "the content is SECRET-CONTENT-42"})
        for _c, t, _th in self.tg.sent:
            self.assertNotIn("DSML", t)
            self.assertNotIn("<invoke", t)

    @mock.patch.object(providers, "chat")
    def test_round_reasoning_rides_in_context(self, chat_mock):
        """A round's reasoning is stored on its assistant message, so the
        NEXT round re-reads the model's own analysis instead of starting
        cold. Provider accepts reasoning_content next to tool_calls
        (verified live against go/deepseek-v4.1-flash)."""
        tc = [{"id": "c1", "name": "read_file", "arguments": {"path": "no.txt"}}]
        chat_mock.side_effect = [("", "I should read that file.", tc),
                                 ("final", "It was missing.", None)]
        main.handle_message(self.cfg, self.tg, msg(text="go"), self.state)
        # round 2's request carries round 1's reasoning on the assistant msg
        r2 = [m for m in chat_mock.call_args_list[1][0][3]
              if m.get("role") == "assistant" and m.get("tool_calls")]
        self.assertEqual(len(r2), 1)
        self.assertEqual(r2[0]["reasoning_content"], "I should read that file.")
        # the final answer's own reasoning is stored too
        hist = self.state.get_history(100, None)
        self.assertEqual(hist[1]["reasoning_content"], "I should read that file.")
        self.assertEqual(hist[3]["reasoning_content"], "It was missing.")
        # ...and it is COUNTED, or auto-compact fires late
        self.assertGreater(main._history_tokens(hist),
                           main._est_tokens("I should read that file."))

    @mock.patch.object(providers, "chat")
    def test_reasoning_key_empty_when_model_thinks_not(self, chat_mock):
        """No reasoning produced -> the key is still there but EMPTY (the
        DeepSeek provider requires the key; "" is what satisfies it), and no
        thinking text is invented."""
        chat_mock.side_effect = [("", "", [{"id": "c1", "name": "read_file",
                                            "arguments": {"path": "no.txt"}}]),
                                 ("done", "", None)]
        main.handle_message(self.cfg, self.tg, msg(text="go"), self.state)
        for m in self.state.get_history(100, None):
            if m.get("role") == "assistant":
                self.assertEqual(m["reasoning_content"], "")

    @mock.patch.object(providers, "chat")
    def test_long_reasoning_is_tail_capped(self, chat_mock):
        """Display-length reasoning must not be re-billed in full on every
        call: only the last REASONING_KEEP_CHARS go into context."""
        long_thought = ("x" * (main.REASONING_KEEP_CHARS + 5000)) + "CONCLUSION"
        tc = [{"id": "c1", "name": "read_file", "arguments": {"path": "no.txt"}}]
        chat_mock.side_effect = [("", long_thought, tc),
                                 ("final", "", None)]
        main.handle_message(self.cfg, self.tg, msg(text="go"), self.state)
        hist = self.state.get_history(100, None)
        stored = hist[1]["reasoning_content"]
        self.assertEqual(len(stored), main.REASONING_KEEP_CHARS)
        self.assertTrue(stored.endswith("CONCLUSION"))  # the tail survives

    @mock.patch.object(providers, "chat")
    def test_interrupted_turn_keeps_reasoning(self, chat_mock):
        """/stop mid-turn still records what the model was thinking."""
        def side_effect(*a, **k):
            self.state.request_interrupt((100, None))
            return ("partial", "thinking about it", None)
        chat_mock.side_effect = side_effect
        main.handle_message(self.cfg, self.tg, msg(text="go"), self.state)
        hist = self.state.get_history(100, None)
        self.assertIn(main.INTERRUPT_NOTE, hist[-1]["content"])
        self.assertEqual(hist[-1]["reasoning_content"], "thinking about it")

    @mock.patch.object(providers, "chat")
    def test_tool_round_remembered_next_turn(self, chat_mock):
        """The core fix: round N's input+output stay in history, so turn 2's
        request already carries them - no re-reading the same file."""
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        with open(path, "w") as f:
            f.write("SECRET-CONTENT-42")
        self.addCleanup(os.unlink, path)
        chat_mock.side_effect = [
            ("", "", [{"id": "c1", "name": "read_file",
                       "arguments": {"path": path}}]),
            ("first answer", "", None),
            ("second answer", "", None),
        ]
        main.handle_message(self.cfg, self.tg, msg(text="read it"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="and now?"), self.state)
        turn2 = chat_mock.call_args_list[2][0][3]
        tools_seen = [m for m in turn2 if m.get("role") == "tool"]
        self.assertEqual(len(tools_seen), 1)
        self.assertIn("SECRET-CONTENT-42", tools_seen[0]["content"])
        # the assistant tool_calls (the input) ride along too
        self.assertTrue(any(m.get("tool_calls") for m in turn2))

    @mock.patch.object(providers, "chat")
    def test_long_output_spilled_history_keeps_path(self, chat_mock):
        """Over tool_output_max_bytes: the model sees preview + path live
        (OpenCode style); history keeps only the path; the file holds the
        full output."""
        with tempfile.TemporaryDirectory() as td:
            cfg = make_config(tool_output_max_bytes=64)
            cfg.path = os.path.join(td, "config.toml")  # spill dir -> temp
            fd, path = tempfile.mkstemp(suffix=".txt")
            os.close(fd)
            big = "B" * 300
            with open(path, "w") as f:
                f.write(big)
            self.addCleanup(os.unlink, path)
            chat_mock.side_effect = [
                ("", "", [{"id": "c1", "name": "read_file",
                           "arguments": {"path": path}}]),
                ("done reading", "", None)]
            main.handle_message(cfg, self.tg, msg(text="read the big file"),
                                self.state)
            # live: head preview + save hint
            live = [m for m in chat_mock.call_args_list[1][0][3]
                    if m.get("role") == "tool"][0]["content"]
            self.assertTrue(live.startswith("B" * 64))
            self.assertIn("saved to", live)
            # history: path only, payload gone
            hist = [m for m in self.state.get_history(100, None)
                    if m.get("role") == "tool"][0]["content"]
            self.assertTrue(hist.startswith("[full tool output saved to "))
            self.assertNotIn("B" * 64, hist)
            saved = hist.split("saved to ", 1)[1].split(" (", 1)[0]
            parts = saved.replace("\\", "/").split("/")
            self.assertIn("sessions", parts)      # sessions/<sid>/tool_output
            self.assertIn("tool_output", parts)
            self.assertNotIn("keirai", parts)     # never the app-wide constant
            with open(saved, encoding="utf-8") as f:
                self.assertEqual(f.read(), big)

    @mock.patch.object(providers, "chat")
    def test_provider_error_keeps_tool_rounds(self, chat_mock):
        """A failed turn keeps the user message + completed tool rounds
        (a failing tool is itself a result and stays too)."""
        chat_mock.side_effect = [
            ("", "", [{"id": "c1", "name": "read_file",
                       "arguments": {"path": "missing.txt"}}]),
            RuntimeError("provider down")]
        main.handle_message(self.cfg, self.tg, msg(text="hi"), self.state)
        hist = self.state.get_history(100, None)
        self.assertEqual([m["role"] for m in hist],
                         ["user", "assistant", "tool"])
        self.assertIn("no such file", hist[2]["content"])
        self.assertTrue(any("error" in t for _c, t, _t in self.tg.sent))

    @mock.patch.object(providers, "chat")
    def test_interrupt_mid_tool_round_keeps_history(self, chat_mock):
        """/stop during a round: results that ran stay, unanswered calls get
        a placeholder, and the interrupt note closes the turn."""
        state = self.state
        chat_mock.side_effect = [
            ("", "", [{"id": "c1", "name": "read_file",
                       "arguments": {"path": "a"}},
                      {"id": "c2", "name": "read_file",
                       "arguments": {"path": "b"}}]),
            ("never", "", None)]

        def fake_exec(cfg, name, args, ctx):
            state.request_interrupt((100, None))  # /stop lands mid-round
            return "partial result"

        with mock.patch.object(main.tools_mod, "execute", fake_exec):
            main.handle_message(self.cfg, self.tg, msg(text="go"), self.state)
        hist = state.get_history(100, None)
        self.assertEqual([m["role"] for m in hist],
                         ["user", "assistant", "tool", "tool", "assistant"])
        self.assertEqual(hist[2]["content"], "partial result")
        self.assertIn("not executed", hist[3]["content"])
        self.assertIn("interrupted", hist[4]["content"])

    def test_midturn_prose_is_not_swallowed_by_the_tool_round(self):
        """The model's line before acting ("checking the log first") used to
        survive only as a streaming side effect - deleted in edit mode, an
        ephemeral draft in draft mode. 74 of 423 stored tool rounds carry
        such prose, so it has to reach the chat for real."""
        cfg = make_config(stream_drafts=True, stream_mode="draft")
        rounds = [("Checking the log first.", "", [
            {"id": "c1", "name": "read_file", "arguments": {"path": "a"}}])]
        with mock.patch.object(main, "_maybe_name_session", lambda *a, **k: None), \
                mock.patch.object(main, "_stream_answer",
                                  side_effect=lambda *a, **k: rounds.pop(0)), \
                mock.patch.object(main.tools_mod, "execute", lambda *a: "file body"), \
                mock.patch.object(providers, "chat",
                                  return_value=("finished", "", None)):
            main.handle_message(cfg, self.tg, msg(text="go"), self.state)
        marks = [src for _chat, src, _tid in self.tg.rich]
        self.assertIn({"markdown": "Checking the log first."}, marks)
        # the prose keeps its slot in the chat: nothing deleted, and the final
        # answer comes after it
        self.assertEqual(self.tg.deleted, [])
        self.assertEqual(marks[-1], {"markdown": "finished"})
        self.assertLess(marks.index({"markdown": "Checking the log first."}),
                        marks.index({"markdown": "finished"}))

    def test_midturn_prose_adopts_the_edit_mode_message(self):
        """Edit mode already has a real message: finalize it instead of
        sending a duplicate, and detach it so _drop_live leaves it alone."""
        cfg = make_config(rich_messages=True)
        live = main._live_new(True, False)
        live["answer_id"], live["role"]["answer_id"] = 77, "answer"
        main._keep_midturn_answer(self.tg, live, 100, None, "One sec.", cfg)
        self.assertEqual(self.tg.edits, [(100, 77, {"markdown": "One sec."})])
        self.assertEqual(self.tg.rich, [])       # no duplicate message
        self.assertIsNone(live.get("answer_id"))  # _drop_live will not delete it
        main._drop_live(self.tg, live, 100, keep_thinking=True)
        self.assertNotIn((100, 77), self.tg.deleted)

    def test_midturn_prose_empty_is_silent(self):
        cfg = make_config(rich_messages=True)
        main._keep_midturn_answer(self.tg, main._live_new(True, True), 100, None,
                                  "   ", cfg)
        self.assertEqual(self.tg.rich, [])
        self.assertEqual(self.tg.sent, [])

    def test_close_tool_rounds_and_orphan_tools(self):
        h = [{"role": "user", "content": "go"},
             {"role": "assistant", "content": "", "tool_calls": [
                 {"id": "a", "type": "function",
                  "function": {"name": "x", "arguments": "{}"}},
                 {"id": "b", "type": "function",
                  "function": {"name": "y", "arguments": "{}"}}]},
             {"role": "tool", "tool_call_id": "a", "content": "ok"}]
        main._close_tool_rounds(h)
        self.assertEqual(len(h), 4)
        self.assertEqual(h[3]["tool_call_id"], "b")
        main._close_tool_rounds(h)  # idempotent
        self.assertEqual(len(h), 4)
        # leading orphan: a tool result whose call was cut away
        h2 = [{"role": "tool", "tool_call_id": "gone", "content": "orphan"},
              {"role": "user", "content": "u"},
              {"role": "assistant", "content": "a"}]
        main._strip_orphan_tools(h2)
        self.assertEqual([m["role"] for m in h2], ["user", "assistant"])

    @mock.patch.object(providers, "chat")
    def test_no_message_cap_whole_history_is_sent(self, chat_mock):
        """There is no 24-message cap any more: every stored message goes to
        the model and auto-compact alone bounds the size of the context."""
        state = self.state
        hist = state.get_history(100, None)
        for i in range(40):
            hist.append({"role": "user" if i % 2 == 0 else "assistant",
                         "content": "m%d" % i})
        chat_mock.return_value = ("ok", "", None)
        main.handle_message(self.cfg, self.tg, msg(text="hi"), state)
        sent = chat_mock.call_args[0][3]  # messages arg
        # system prompt + all 40 stored messages + the new user message
        self.assertEqual(len(sent), 40 + 1 + 1)
        self.assertEqual(sent[1]["content"], "m0")   # the OLDEST message rides
        self.assertEqual(sent[-1]["content"], "hi")
        self.assertEqual(len(hist), 42)              # 40 + user + the answer

    def test_images_stay_in_the_prompt_for_the_newest_only(self):
        """Without a message cap, base64 images would ride on every call
        forever - so only the newest IMAGE_KEEP image-bearing messages keep
        theirs in the prompt, and that is a prompt-side change only: the
        stored history keeps every picture."""
        state = self.state
        hist = state.get_history(100, None)
        pic = {"type": "image_url",
               "image_url": {"url": "data:image/jpeg;base64,QUJD"}}
        for i in range(main.IMAGE_KEEP + 2):
            hist.append({"role": "user",
                         "content": [{"type": "text", "text": "pic %d" % i},
                                     dict(pic)]})
        sent = main._prompt(hist)
        self.assertEqual(len(sent), len(hist))        # no message dropped
        self.assertEqual([sum(1 for p in m["content"]
                              if isinstance(p, dict) and p.get("type") == "image_url")
                          for m in sent],
                         [0, 0] + [1] * main.IMAGE_KEEP)
        self.assertEqual(sent[0]["content"][0],       # text survives intact
                         {"type": "text", "text": "pic 0"})
        self.assertEqual(sent[0]["content"][1]["type"], "text")
        # the STORED history still has every picture
        self.assertTrue(all(any(isinstance(p, dict) and p.get("type") == "image_url"
                                for p in m["content"]) for m in hist))
        empty = []
        self.assertIs(main._prompt(empty), empty)     # nothing to drop

    @mock.patch.object(providers, "chat")
    def test_mid_turn_message_injected_at_boundary(self, chat_mock):
        """A message sent while the turn runs joins context right after the
        current tool use - steering without /stop - and is persisted."""
        state = self.state
        key = (100, None)
        calls = {"n": 0}

        def fake(name, api_key, model, messages, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return ("", "", [{"id": "c1", "name": "read_file",
                                  "arguments": {"path": "a.txt"}}])
            return ("steered", "", None)  # round 2 must see the injection

        chat_mock.side_effect = fake
        orig = main.tools_mod.execute

        def exec_then_inject(cfg, name, args, ctx):
            out = orig(cfg, name, args, ctx)
            state.inject(key, {"message": msg(text="actually do X instead")})
            return out

        with mock.patch.object(main.tools_mod, "execute", exec_then_inject):
            main.handle_message(self.cfg, self.tg, msg(text="start"), state)
        # round 2's request carried the injected message
        second = chat_mock.call_args_list[1][0][3]
        hits = [m for m in second if m.get("role") == "user"
                and "actually do X" in str(m.get("content"))]
        self.assertEqual(len(hits), 1)
        self.assertIn("sent while you were working",
                      str(hits[0]["content"]))
        # persisted in history, inbox empty afterwards
        self.assertTrue(any("actually do X" in str(m.get("content"))
                            for m in state.get_history(100, None)))
        self.assertEqual(state.take_inbox(key), [])

    @mock.patch.object(providers, "chat")
    def test_dispatch_injects_active_turn_else_queues(self, chat_mock):
        """Dispatcher: plain text during an active turn -> inbox; commands
        and idle-session work -> the session queue."""
        state, cfg, tg = self.state, self.cfg, self.tg
        key = (100, None)
        plain = {"message": msg(text="steer me")}
        cmd = {"message": msg(text="/context")}
        with mock.patch.object(main, "_session_submit") as sub:
            main._dispatch_update(cfg, tg, state, {}, plain)   # idle
            sub.assert_called_once_with(cfg, tg, state, {}, key, plain)
            sub.reset_mock()
            state.begin_turn(key)
            try:
                main._dispatch_update(cfg, tg, state, {}, plain)  # active
                sub.assert_not_called()
                self.assertEqual(len(state.take_inbox(key)), 1)
                main._dispatch_update(cfg, tg, state, {}, cmd)    # cmd waits
                sub.assert_called_once()
            finally:
                state.end_turn(key)

    @mock.patch.object(providers, "chat")
    def test_thinking_block_each_round(self, chat_mock):
        """Every completed round's reasoning is shown as its own block, not
        just the last one after the turn ends."""
        chat_mock.side_effect = [
            ("", "why round one", [{"id": "c1", "name": "read_file",
                                    "arguments": {"path": "x"}}]),
            ("done", "why round two", None)]
        main.handle_message(self.cfg, self.tg, msg(text="go"), self.state)
        htmls = [s.get("html") for _c, s, _t in self.tg.rich if s.get("html")]
        self.assertTrue(any("why round one" in h for h in htmls))  # round 1
        self.assertTrue(any("why round two" in h for h in htmls))  # final

    @mock.patch.object(providers, "chat")
    def test_thinking_blocks_hidden_when_off(self, chat_mock):
        self.state.set_thinking(42, False)
        chat_mock.side_effect = [
            ("", "hidden thought", [{"id": "c1", "name": "read_file",
                                     "arguments": {"path": "x"}}]),
            ("done", "also hidden", None)]
        main.handle_message(self.cfg, self.tg, msg(text="go"), self.state)
        htmls = [s.get("html") for _c, s, _t in self.tg.rich if s.get("html")]
        self.assertFalse(any("Thinking" in h for h in htmls))
        self.assertFalse(any("blockquote expandable" in t
                             for _c, t, _th in self.tg.sent))


class TestReplyTo(unittest.TestCase):
    """Item 17: reply/quote context injected into the prompt."""

    def test_no_reply(self):
        self.assertEqual(main._reply_context(msg(text="hi")), "")

    def test_reply_annotation(self):
        m = msg(text="and this?")
        m["reply_to_message"] = {"from": {"username": "alice"},
                                 "text": "the old answer was42"}
        ctx = main._reply_context(m)
        self.assertIn("user is replying to this message (from @alice)", ctx)
        self.assertIn("42", ctx)

    def test_quote_annotation(self):
        m = msg(text="what does this mean?")
        m["reply_to_message"] = {"from": {"first_name": "Bob"}, "text": "long text"}
        m["quote"] = {"text": "just this bit"}
        ctx = main._reply_context(m)
        self.assertIn("quoting this part", ctx)
        self.assertIn("just this bit", ctx)

    def test_reply_without_text(self):
        m = msg(text="huh?")
        m["reply_to_message"] = {"from": {}, "text": ""}
        ctx = main._reply_context(m)
        self.assertIn("no extractable text", ctx)

    def test_long_reply_capped(self):
        m = msg(text="huh?")
        m["reply_to_message"] = {"from": {}, "text": "x" * 9000}
        ctx = main._reply_context(m)
        self.assertLess(len(ctx), 1700)

    @mock.patch.object(providers, "chat", return_value=("ok", "", None))
    def test_context_reaches_prompt(self, chat_mock):
        tg = FakeTG()
        m = msg(text="what about this?")
        m["reply_to_message"] = {"from": {"username": "alice"},
                                 "text": "remember me"}
        main.handle_message(make_config(stream_drafts=False), tg, m,
                            main.State(True))
        # scan every call: the concurrent naming thread's call may land last
        user_texts = [msg_["content"]
                      for call in chat_mock.call_args_list
                      for msg_ in call[0][3]
                      if msg_["role"] == "user"
                      and isinstance(msg_["content"], str)]
        self.assertTrue(any("user is replying to this message" in t
                            and "remember me" in t for t in user_texts))


class TestEditStreaming(unittest.TestCase):
    """Item 8: non-private chats stream via send + editMessageText(rich)."""

    def setUp(self):
        disable_titles(self)

    @mock.patch.object(providers, "chat_stream")
    def test_group_stream_uses_edits(self, stream_mock):
        stream_mock.return_value = iter([("reasoning", "hmm about that"),
                                         ("content", "hello **world**")])
        tg = FakeTG()
        state = main.State(True)
        g = {"id": 100, "type": "supergroup"}
        main.handle_message(make_config(stream_drafts=True), tg,
                            msg(text="hi", chat=g), state)
        self.assertTrue(tg.rich)              # placeholder message created
        self.assertTrue(tg.edits)             # live edits happened
        # final delivery: full thinking edited into its message, the answer
        # message already carries the full markdown (single-chunk stream)
        self.assertEqual(tg.rich[-1][1], {"markdown": "hello **world**"})
        edited_ids = [mid for _c, mid, _s in tg.edits]
        self.assertIn(1000 + 1, edited_ids)   # first message was edited

    @mock.patch.object(providers, "chat_stream")
    def test_edit_failure_falls_back_to_plain_send(self, stream_mock):
        stream_mock.return_value = iter([("content", "answer text")])
        tg = FakeTG()

        def boom(chat_id, message_id, markdown=None, html=None):
            import telegram as tg_mod
            raise tg_mod.TelegramError("editMessageText not supported")

        tg.edit_rich = boom
        g = {"id": 100, "type": "supergroup"}
        main.handle_message(make_config(stream_drafts=True), tg,
                            msg(text="hi", chat=g), main.State(True))
        # live edits failed -> placeholder dropped, answer sent fresh
        self.assertTrue(tg.deleted)
        self.assertIn({"markdown": "answer text"}, [s for _c, s, _t in tg.rich])


class TestProviderPriority(unittest.TestCase):
    """P3: Go-first default + catalog-checked error fallback (group 25)."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    def test_default_model_is_go(self):
        self.assertEqual(make_config().default_model, "go/mimo-v2.6-flash")

    @mock.patch.object(main, "_alternate_provider", return_value=("zen", "zk"))
    @mock.patch.object(providers, "chat")
    def test_provider_error_falls_back_once(self, chat_mock, alt_mock):
        chat_mock.side_effect = [providers.ProviderError("boom"),
                                 ("recovered", "", None)]
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertEqual(chat_mock.call_args_list[0][0][0], "go")   # go first
        self.assertEqual(chat_mock.call_args_list[1][0][0], "zen")  # then zen
        self.assertTrue(any(s[1].get("markdown") == "recovered"
                            for s in self.tg.rich))

    @mock.patch.object(main, "_alternate_provider", return_value=None)
    @mock.patch.object(providers, "chat",
                       side_effect=providers.ProviderError("boom"))
    def test_error_without_alternate_is_reported(self, chat_mock, alt_mock):
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertTrue(any("error" in t for _c, t, _t in self.tg.sent))

    def test_alternate_requires_key_and_catalog_entry(self):
        # catalog has the model -> usable alternate
        with mock.patch.object(providers, "cached_models",
                               return_value=([{"id": "glm-5.3-flash"}], False)):
            self.assertEqual(main._alternate_provider(self.cfg, "zen", "glm-5.3-flash"),
                             ("go", "gk"))
            self.assertIsNone(main._alternate_provider(self.cfg, "zen", "other"))
        # catalog unreachable -> never blind-retry
        with mock.patch.object(providers, "cached_models",
                               side_effect=providers.ProviderError("down")):
            self.assertIsNone(main._alternate_provider(self.cfg, "zen", "glm-5.3-flash"))
        # no key on the alternate -> no fallback
        nokey = make_config(providers={"zen": {"api_key": "zk"}, "go": {"api_key": ""}})
        self.assertIsNone(main._alternate_provider(nokey, "zen", "glm-5.3-flash"))


class TestUsageAccounting(unittest.TestCase):
    """P3: token/cost accumulation per session (group 21)."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    @mock.patch.object(providers, "price_for")
    @mock.patch.object(providers, "chat")
    def test_usage_and_cost_accumulate(self, chat_mock, price_mock):
        price_mock.return_value = {"cost_in": 0.15, "cost_out": 0.5,
                                   "cost_cache_read": 0.03}

        def fake(*args, **kw):
            kw["usage_out"].update({
                "prompt_tokens": 1000, "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 500}})
            return ("answer", "", None)

        chat_mock.side_effect = fake
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        m = self.state.meta[(100, None)]
        self.assertEqual(m["tokens_in"], 1000)
        self.assertEqual(m["tokens_out"], 100)
        self.assertEqual(m["tokens_cached_read"], 500)
        self.assertEqual(m["tokens_cached_write"], 0)
        self.assertAlmostEqual(m["cost_usd"],
                               (500 * 0.15 + 500 * 0.03 + 100 * 0.5) / 1e6)
        self.assertRegex(m["session_id"], r"^\d{8}-\d{4}-[0-9a-f]{4}$")
        # keeps accumulating across turns
        main.handle_message(self.cfg, self.tg, msg(text="again"), self.state)
        self.assertEqual(self.state.meta[(100, None)]["tokens_in"], 2000)
        self.assertAlmostEqual(self.state.meta[(100, None)]["cost_usd"],
                               2 * (500 * 0.15 + 500 * 0.03 + 100 * 0.5) / 1e6)

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_missing_pricing_counts_tokens_only(self, chat_mock, price_mock):
        def fake(*args, **kw):
            kw["usage_out"].update({"prompt_tokens": 500,
                                    "completion_tokens": 50})
            return ("a", "", None)

        chat_mock.side_effect = fake
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        m = self.state.meta[(100, None)]
        self.assertEqual(m["tokens_in"], 500)   # tokens still counted
        self.assertEqual(m["cost_usd"], 0.0)    # but no price -> no cost


class TestContextCostCommands(unittest.TestCase):
    """P3: /context and /cost (group 21) - ONE shared report (both names show
    the same thing, cost included) plus the user/replies/tools history split."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    @mock.patch.object(providers, "price_for")
    @mock.patch.object(providers, "chat", return_value=("a", "", None))
    def test_context_shows_full_report_including_cost(self, _, price_mock):
        price_mock.return_value = {"context": 1048576}
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.tg.sent.clear()
        main.handle_message(self.cfg, self.tg, msg(text="/context"), self.state)
        text = self.tg.sent[-1][1]
        self.assertIn("<b>session</b>", text)
        self.assertIn("<b>model</b>", text)
        self.assertIn("<b>context</b>", text)
        # used/max against the model's REAL window (models.dev, tokens)
        self.assertIn("/ 1,048,576 tokens", text)
        self.assertNotIn("chars", text)     # tokens only, no char counts
        self.assertIn("messages", text)     # message count kept
        self.assertIn("<b>tokens</b>", text)
        self.assertIn("auto-compact:", text)
        self.assertNotIn("<b>history</b>", text)  # split line dropped
        self.assertIn("<b>cost</b>: $", text)   # same report as /cost
        self.assertRegex(text, r"\d{8}-\d{4}-[0-9a-f]{4}")

    @mock.patch.object(providers, "price_for",
                       return_value={"context": 500000})
    def test_context_and_cost_are_the_same_report(self, _):
        """/context and /cost run through one handler: identical output."""
        main.handle_message(self.cfg, self.tg, msg(text="/context"), self.state)
        by_context = self.tg.sent[-1][1]
        main.handle_message(self.cfg, self.tg, msg(text="/cost"), self.state)
        by_cost = self.tg.sent[-1][1]
        self.assertEqual(by_context, by_cost)
        self.assertIn("<b>cost</b>: $", by_cost)   # cost is never hidden now
        self.assertIn("/ 500,000 tokens", by_cost)  # model window, not config
        self.assertIn("<b>model</b>", by_cost)
        self.assertIn("triggers at", by_cost)  # threshold lives on its own line

    def test_report_counts_tool_use_in_tokens_and_messages(self):
        """Tool rounds ARE context: a call's name + JSON arguments (which
        live on the assistant message, never in its content) and its result
        are all counted - in the token estimate and in the message count."""
        args = '{"command":"ls"}'
        self.state.get_history(100, None).extend([
            {"role": "user", "content": "run it"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "bash", "arguments": args}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "RESULT-OUTPUT"},
            {"role": "assistant", "content": "done"},
        ])
        history = self.state.history[(100, None)]
        # the rule: [A-Za-z0-9] ~4 chars/token, every other character 1 token
        self.assertEqual(main._est_tokens("abcd"), 1)
        self.assertEqual(main._est_tokens("abcde"), 2)
        self.assertEqual(main._est_tokens("a b"), 2)    # spaces cost 1 each
        self.assertEqual(main._est_tokens("日本語"), 3)  # CJK: 1 token each
        # user + assistant(tool_calls) + tool result + assistant answer
        self.assertEqual(main._history_tokens(history),
                         main._est_tokens("run it") + main._est_tokens("bash")
                         + main._est_tokens(args)
                         + main._est_tokens("RESULT-OUTPUT")
                         + main._est_tokens("done"))
        self.assertEqual(main._history_tokens(history), 19)
        # with cfg the system prompt rides along (it is sent on every call) -
        # that is SYSTEM.md now, re-read like the request itself
        import config as config_mod
        self.assertEqual(main._history_tokens(history, self.cfg),
                         19 + main._est_tokens(config_mod.system_prompt(self.cfg)))
        main.handle_message(self.cfg, self.tg, msg(text="/context"), self.state)
        text = self.tg.sent[-1][1]
        self.assertNotIn("<b>history</b>", text)   # the split line is gone
        self.assertIn("4 messages", text)          # user + agent + tool msgs
        self.assertNotIn("chars", text)            # tokens only

    @mock.patch.object(providers, "price_for", return_value=None)
    def test_context_max_unknown_without_metadata(self, _):
        main.handle_message(self.cfg, self.tg, msg(text="/context"), self.state)
        self.assertIn("max unknown", self.tg.sent[-1][1])


class TestModelsStats(unittest.TestCase):
    """P3: /models stats come from models.dev (the API returns ids only)."""

    @mock.patch.object(providers, "models_metadata")
    @mock.patch.object(providers, "cached_models")
    def test_models_merges_metadata(self, cm_mock, mm_mock):
        cm_mock.return_value = ([{"id": "glm-5.3-flash", "context": None,
                                  "cost_in": None, "cost_out": None,
                                  "max_output": None}], False)
        mm_mock.return_value = ({"zen": {"glm-5.3-flash": {
                                    "context": 1000000, "cost_in": 0.15,
                                    "cost_out": 0.5, "max_output": 131072}},
                                 "go": {}}, False)
        tg = FakeTG()
        main.handle_message(make_config(), tg, msg(text="/models"), main.State(True))
        text = tg.sent[0][1]
        self.assertIn("ctx ", text)
        self.assertIn("$0.15/", text)
        self.assertIn("per 1M", text)


class TestSessionMeta(unittest.TestCase):
    """sessions.db session_meta: ids + accumulators + backfill."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._dir.name, "meta.db")

    def tearDown(self):
        self._dir.cleanup()

    def test_new_session_id_format(self):
        import sessions
        self.assertRegex(sessions.new_session_id(1790000000),
                         r"^\d{8}-\d{4}-[0-9a-f]{4}$")

    def test_meta_lifecycle(self):
        import sessions
        store = sessions.Store(self.db)
        meta = store.ensure_meta(100, 0)
        self.assertRegex(meta["session_id"], r"^\d{8}-\d{4}-[0-9a-f]{4}$")
        store.record_usage(100, 0, 0.25, {"tokens_in": 10, "tokens_out": 5,
                                          "tokens_cached_read": 2,
                                          "tokens_cached_write": 0})
        store.record_usage(100, 0, 0.25, {"tokens_in": 1, "tokens_out": 0,
                                          "tokens_cached_read": 0,
                                          "tokens_cached_write": 0})
        got = store.load_meta()[(100, 0)]
        self.assertAlmostEqual(got["cost_usd"], 0.5)
        self.assertEqual(got["tokens_in"], 11)
        self.assertEqual(got["tokens_cached_read"], 2)
        # survives history REPLACE + process restart
        store.save_session(100, 0, None, [{"role": "user", "content": "x"}])
        store.close()
        store2 = sessions.Store(self.db)
        got2 = store2.load_meta()[(100, 0)]
        self.assertEqual(got2["session_id"], meta["session_id"])
        self.assertAlmostEqual(got2["cost_usd"], 0.5)
        # deleting the session clears its meta too
        store2.delete_session(100, 0)
        self.assertEqual(store2.load_meta(), {})
        store2.close()

    def test_backfill_gives_existing_sessions_ids(self):
        import sessions
        store = sessions.Store(self.db)
        store.save_session(7, 0, None, [{"role": "user", "content": "old"}])
        self.assertEqual(store.load_meta(), {})  # pre-meta session: no row yet
        store.close()
        store2 = sessions.Store(self.db)  # reopen -> backfill runs
        meta = store2.load_meta()[(7, 0)]
        self.assertRegex(meta["session_id"], r"^\d{8}-\d{4}-[0-9a-f]{4}$")
        store2.close()

    def test_delete_chat_clears_meta(self):
        import sessions
        store = sessions.Store(self.db)
        store.ensure_meta(5, 0)
        store.ensure_meta(5, 9)
        store.delete_chat(5)
        self.assertEqual(store.load_meta(), {})
        store.close()


class TestTopicFlowMode(unittest.TestCase):
    """P4 group 22: /topic enter/exit with typed confirmations + getMe gates."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    def _confirm(self, answer):
        main.handle_message(self.cfg, self.tg, msg(text="/topic"), self.state)
        self.assertIn("type <code>yes</code>", self.tg.sent[-1][1])
        main.handle_message(self.cfg, self.tg, msg(text=answer), self.state)

    def test_enter_flow_after_confirmation(self):
        self._confirm("yes")
        self.assertTrue(self.cfg.topic_flow)
        self.assertIn("topic flow is <b>on</b>", self.tg.sent[-1][1])
        self.assertNotIn((100, None), self.state.prompts)

    def test_enter_refused_when_users_can_create_topics(self):
        self.tg.me["allows_users_to_create_topics"] = True
        self._confirm("yes")
        self.assertFalse(self.cfg.topic_flow)
        self.assertIn("Disallow users to create topics", self.tg.sent[-1][1])

    def test_enter_refused_when_threaded_mode_off(self):
        self.tg.me["has_topics_enabled"] = False
        self._confirm("yes")
        self.assertFalse(self.cfg.topic_flow)
        self.assertIn("Threaded mode is off", self.tg.sent[-1][1])

    def test_no_cancels(self):
        self._confirm("no")
        self.assertFalse(self.cfg.topic_flow)
        self.assertIn("cancelled", self.tg.sent[-1][1])

    def test_command_cancels_pending_prompt_and_runs(self):
        main.handle_message(self.cfg, self.tg, msg(text="/topic"), self.state)
        self.assertIn((100, None), self.state.prompts)
        main.handle_message(self.cfg, self.tg, msg(text="/help"), self.state)
        self.assertNotIn((100, None), self.state.prompts)
        self.assertIn("Keirai", self.tg.sent[-1][1])
        self.assertFalse(self.cfg.topic_flow)

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_plain_message_cancels_prompt_as_do_nothing(self, chat_mock):
        main.handle_message(self.cfg, self.tg, msg(text="/topic"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="hello instead"), self.state)
        self.assertFalse(self.cfg.topic_flow)  # no mode change
        self.assertNotIn((100, None), self.state.prompts)
        # the message was answered normally (the "do nothing" choice)
        self.assertTrue(any(s[1].get("markdown") == "answer"
                            for s in self.tg.rich))

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_leave_flow(self, chat_mock):
        self.cfg.topic_flow = True
        main.handle_message(self.cfg, self.tg, msg(text="/topic"), self.state)
        self.assertIn("leave topic flow", self.tg.sent[-1][1])
        main.handle_message(self.cfg, self.tg, msg(text="yes"), self.state)
        self.assertFalse(self.cfg.topic_flow)
        self.assertIn("topic flow is <b>off</b>", self.tg.sent[-1][1])


class TestDeliveryRules(unittest.TestCase):
    """P4 group 20: mode gates, per-view hints, command availability."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    @mock.patch.object(providers, "chat")
    def test_flow_off_topics_inert_for_plain_and_commands(self, chat_mock):
        main.handle_message(self.cfg, self.tg,
                            msg(text="hello", message_thread_id=7), self.state)
        main.handle_message(self.cfg, self.tg,
                            msg(text="/help", message_thread_id=7), self.state)
        chat_mock.assert_not_called()
        hints = [t for _c, t, _th in self.tg.sent]
        self.assertEqual(len(hints), 2)
        for t in hints:
            self.assertIn("topic flow is off", t)

    @mock.patch.object(providers, "chat")
    def test_flow_on_all_gates(self, chat_mock):
        self.cfg.topic_flow = True
        # plain message -> hint, no answer
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertIn("topic flow is on", self.tg.sent[-1][1])
        chat_mock.assert_not_called()
        # session-scoped commands -> needs-session hint (incl. /stop)
        for command in ("/compact", "/stop", "/delete", "/context", "/cost"):
            main.handle_message(self.cfg, self.tg, msg(text=command), self.state)
            self.assertIn("needs a session", self.tg.sent[-1][1], command)
        # /model global is chat-wide and allowed in All
        main.handle_message(self.cfg, self.tg,
                            msg(text="/model global go/glm-5.3-flash"), self.state)
        self.assertEqual(self.cfg.default_model, "go/glm-5.3-flash")
        self.assertIn("default model set", self.tg.sent[-1][1])
        # allowed command: /session runs (empty list)
        main.handle_message(self.cfg, self.tg, msg(text="/session"), self.state)
        self.assertIn("no other sessions", self.tg.sent[-1][1])
        chat_mock.assert_not_called()

    @mock.patch.object(providers, "chat")
    def test_flow_on_media_in_all_hinted(self, chat_mock):
        self.cfg.topic_flow = True
        m = msg(text=None, photo=[{"file_id": "f1", "file_size": 100}])
        main.handle_message(self.cfg, self.tg, m, self.state)
        chat_mock.assert_not_called()
        self.assertIn("topic flow is on", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat")
    def test_forum_topic_created_registered_not_hinted(self, chat_mock):
        m = msg(text="", message_thread_id=999)
        m["forum_topic_created"] = {"name": "client topic"}
        main.handle_message(self.cfg, self.tg, m, self.state)
        self.assertIn(999, self.state.topics[100])
        self.assertEqual(self.state.topic_names[(100, 999)], "client topic")
        self.assertEqual(self.tg.sent, [])  # service messages never hinted

    @mock.patch.object(providers, "chat")
    def test_bots_own_events_witnessed_not_allowed_list_miss(self, chat_mock):
        """Telegram echoes back events the bot itself caused (log showed them
        as 'not in allowed_users'): they must be witnessed quietly instead."""
        self.state.bot_id = 8887915792
        m = msg(text="", message_thread_id=7654)
        m["from"] = {"id": 8887915792, "username": "vdt_keirai_bot"}
        m["forum_topic_created"] = {"name": "bot topic"}
        main.handle_message(self.cfg, self.tg, m, self.state)
        self.assertIn(7654, self.state.topics[100])
        self.assertEqual(self.state.topic_names[(100, 7654)], "bot topic")
        self.assertEqual(self.tg.sent, [])            # no reply, no hint
        chat_mock.assert_not_called()
        # the bot's own plain message: quiet no-op, never treated as a user
        m2 = msg(text="my own reply")
        m2["from"] = {"id": 8887915792, "username": "vdt_keirai_bot"}
        main.handle_message(self.cfg, self.tg, m2, self.state)
        self.assertEqual(self.tg.sent, [])
        chat_mock.assert_not_called()


class TestSessionCommands(unittest.TestCase):
    """P4 group 22: /session listing, switching matrix, prompts, ping."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    def _mk(self, slot, text, name=None, updated=0.0):
        """Session at a slot with one exchange, fixed last-active time."""
        self.state.create_session(100, slot, name=name)
        hist = self.state.get_history(100, slot)
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": "ok"})
        self.state.updated[(100, slot)] = updated
        return self.state.meta[(100, slot)]["session_id"]

    # ------------------------------------------------ listing + normal flow

    @mock.patch.object(providers, "chat")
    def test_list_excludes_current_and_formats(self, chat_mock):
        sid_main = self._mk(None, "main conversation", name="main session",
                            updated=300)
        self._mk(-1, "older topic talk", name="older", updated=100)
        sid_a = self._mk(-2, "recent talk about trains", name="recent",
                         updated=200)
        main.handle_message(self.cfg, self.tg, msg(text="/session"), self.state)
        text = self.tg.sent[-1][1]
        self.assertIn("page 1/1", text)
        self.assertIn("<b>recent</b>", text)
        self.assertIn(sid_a, text)
        self.assertIn("older", text)
        self.assertNotIn(sid_main, text)      # current session excluded
        self.assertIn("recent talk about tra", text)  # preview (truncated)
        self.assertIn("1970", text)           # timestamp rendered

    @mock.patch.object(providers, "chat")
    def test_list_paging_and_switch_by_n(self, chat_mock):
        self._mk(None, "current", name="current", updated=999)
        sids = [self._mk(-(i + 1), "talk %d" % (7 - i), name="s%d" % (7 - i),
                         updated=(7 - i) * 100) for i in range(7)]
        # page 1: 5 of 7 rows
        main.handle_message(self.cfg, self.tg, msg(text="/session"), self.state)
        self.assertIn("page 1/2", self.tg.sent[-1][1])
        # page 2: last 2
        main.handle_message(self.cfg, self.tg, msg(text="/session page 2"),
                            self.state)
        text = self.tg.sent[-1][1]
        self.assertIn("page 2/2", text)
        # /session 1 on page 2 -> row with updated=200 (second in sort order)
        main.handle_message(self.cfg, self.tg, msg(text="/session 1"),
                            self.state)
        expected = sids[5]  # sorted desc: 700,600,500,400,300,200,100 -> page2[0]=200
        self.assertEqual(self.state.meta[(100, None)]["session_id"], expected)
        self.assertIn("switched to", self.tg.sent[-1][1])
        # out-of-range N on the page
        main.handle_message(self.cfg, self.tg, msg(text="/session 9"),
                            self.state)
        self.assertIn("no session #9 on this page", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat")
    def test_switch_by_id_and_unknown_id(self, chat_mock):
        self._mk(None, "current", name="current", updated=999)
        sid = self._mk(-1, "other talk", name="other", updated=100)
        main.handle_message(self.cfg, self.tg, msg(text="/session %s" % sid),
                            self.state)
        self.assertEqual(self.state.meta[(100, None)]["session_id"], sid)
        main.handle_message(self.cfg, self.tg,
                            msg(text="/session nope-not-real"), self.state)
        self.assertIn("no session with id", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat")
    def test_already_active_is_noop(self, chat_mock):
        sid = self._mk(None, "current", name="current", updated=999)
        main.handle_message(self.cfg, self.tg, msg(text="/session %s" % sid),
                            self.state)
        self.assertIn("already active", self.tg.sent[-1][1])

    # ------------------------------------------------ topic-flow switching

    @mock.patch.object(providers, "chat")
    def test_unbound_session_from_all_creates_topic_immediately(self, chat_mock):
        self.cfg.topic_flow = True
        sid = self._mk(-1, "parked talk", name="parked", updated=100)
        main.handle_message(self.cfg, self.tg, msg(text="/session"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="/session 1"), self.state)
        self.assertIn("parked", self.tg.topics)           # topic named after it
        tid = self.tg.topics["parked"]
        self.assertEqual(self.state.sid_slot(100, sid), tid)
        self.assertEqual(self.tg.sent[-1][2], tid)         # welcome in the topic

    @mock.patch.object(providers, "chat")
    def test_bound_session_from_all_prompts_ping(self, chat_mock):
        self.cfg.topic_flow = True
        self._mk(77, "bound talk", name="bound", updated=100)
        main.handle_message(self.cfg, self.tg, msg(text="/session"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="/session 1"), self.state)
        self.assertIn((100, None), self.state.prompts)
        self.assertIn("ping", self.tg.sent[-1][1])
        main.handle_message(self.cfg, self.tg, msg(text="1"), self.state)
        self.assertNotIn((100, None), self.state.prompts)
        # ping landed in topic 77, confirmation came back in All
        self.assertIn("ping", self.tg.sent[-2][1])
        self.assertEqual(self.tg.sent[-2][2], 77)
        self.assertIn("topic found", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat")
    def test_ping_on_dead_topic_recovers(self, chat_mock):
        self.cfg.topic_flow = True
        sid = self._mk(88, "dead talk", name="dead", updated=100)
        self.state.topics[100] = [88]
        self.state.topic_names[(100, 88)] = "dead"
        self.tg.fail_threads.add(88)  # sending there -> thread not found
        main.handle_message(self.cfg, self.tg, msg(text="/session"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="/session 1"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="1"), self.state)
        # dead topic unregistered, session relocated into a fresh topic
        self.assertNotIn(88, self.state.topics[100])
        new_tid = self.tg.topics["dead"]
        self.assertNotEqual(new_tid, 88)
        self.assertEqual(self.state.sid_slot(100, sid), new_tid)

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_unbound_session_in_topic_three_option_prompt(self, chat_mock):
        # a main-chat session that becomes unbound once flow is entered
        sid_main = self._mk(None, "main talk", name="main talk", updated=999)
        self.cfg.topic_flow = True
        main.handle_message(self.cfg, self.tg, msg(text="/new here"), self.state)
        tid = self.tg.topics["here"]
        sid_here = self.state.meta[(100, tid)]["session_id"]
        # viewing from inside the topic: list excludes current, shows main
        main.handle_message(self.cfg, self.tg,
                            msg(text="/session", message_thread_id=tid), self.state)
        text = self.tg.sent[-1][1]
        self.assertIn("main talk", text)
        self.assertNotIn(sid_here, text)
        # selecting it -> 3-option prompt (switch here / create new / nothing)
        main.handle_message(self.cfg, self.tg,
                            msg(text="/session 1", message_thread_id=tid),
                            self.state)
        prompt_text = self.tg.sent[-1][1]
        for expected in ("switch here", "create a new topic", "do nothing"):
            self.assertIn(expected, prompt_text)
        # "1" = switch here: main session moves into this topic, occupant parks
        main.handle_message(self.cfg, self.tg,
                            msg(text="1", message_thread_id=tid), self.state)
        self.assertEqual(self.state.sid_slot(100, sid_main), tid)
        # old session parked somewhere negative
        parked = self.state.sid_slot(100, sid_here)
        self.assertIsNotNone(parked)
        self.assertLess(parked, 0)
        self.assertIn("switched here", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_prompt_do_nothing_via_plain_message(self, chat_mock):
        self.cfg.topic_flow = True
        self._mk(77, "bound talk", name="bound", updated=100)
        main.handle_message(self.cfg, self.tg, msg(text="/session"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="/session 1"), self.state)
        self.assertIn((100, None), self.state.prompts)
        # a plain message in All = do nothing + handled normally (hint)
        main.handle_message(self.cfg, self.tg, msg(text="actually hello"),
                            self.state)
        self.assertNotIn((100, None), self.state.prompts)
        self.assertIn("topic flow is on", self.tg.sent[-1][1])
        chat_mock.assert_not_called()

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_prompts_are_per_slot(self, chat_mock):
        self.cfg.topic_flow = True
        main.handle_message(self.cfg, self.tg, msg(text="/new alpha"), self.state)
        t1 = self.tg.topics["alpha"]
        main.handle_message(self.cfg, self.tg, msg(text="/new beta"), self.state)
        t2 = self.tg.topics["beta"]
        # open a prompt in t1 (the only other session is t2's)
        main.handle_message(self.cfg, self.tg,
                            msg(text="/session", message_thread_id=t1), self.state)
        main.handle_message(self.cfg, self.tg,
                            msg(text="/session 1", message_thread_id=t1), self.state)
        self.assertIn((100, t1), self.state.prompts)
        # a command in t2 does NOT cancel t1's prompt
        main.handle_message(self.cfg, self.tg,
                            msg(text="/help", message_thread_id=t2), self.state)
        self.assertIn((100, t1), self.state.prompts)
        # nor does a plain message in t2 (answered normally in t2)
        main.handle_message(self.cfg, self.tg,
                            msg(text="over here", message_thread_id=t2), self.state)
        self.assertIn((100, t1), self.state.prompts)
        self.assertTrue(any(s[1].get("markdown") == "answer"
                            for s in self.tg.rich))
        # ...but a message in t1 cancels ITS prompt
        main.handle_message(self.cfg, self.tg,
                            msg(text="back in t1", message_thread_id=t1), self.state)
        self.assertNotIn((100, t1), self.state.prompts)


class TestSessionTitles(unittest.TestCase):
    """Group 19 two-stage titling: instant derived title + concurrent model
    upgrade (OpenCode prompt / Hermes structure / oh-my-pi reject guard)."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()

    @staticmethod
    def _is_naming(messages):
        return any("title generator" in str(m.get("content") or "")
                   for m in messages if m.get("role") == "system")

    @classmethod
    def _chat(cls, titles=(), answer="the answer", usage=None):
        """Content-based side_effect: naming calls consume `titles`, main
        turns get `answer`. Order-independent, so the concurrent naming
        thread and the reply may interleave freely."""
        pending = list(titles)

        def fake(name, api_key, model, messages, **kw):
            if cls._is_naming(messages):
                if usage and kw.get("usage_out") is not None:
                    kw["usage_out"].update(usage)
                return (pending.pop(0) if pending else "", "", None)
            return (answer, "", None)
        return fake

    @classmethod
    def _naming_calls(cls, chat_mock):
        return [c for c in chat_mock.call_args_list
                if cls._is_naming(c[0][3])]

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_title_after_first_message_then_stops(self, chat_mock, price_mock):
        chat_mock.side_effect = self._chat(["  Trip Planning!  "])
        main.handle_message(self.cfg, self.tg, msg(text="plan my trip"),
                            self.state)
        m = self.state.meta[(100, None)]
        self.assertEqual(m["name"], "Trip Planning")  # punctuation stripped
        self.assertEqual(m["name_src"], "llm")
        self.assertEqual(len(self._naming_calls(chat_mock)), 1)
        # second turn: no more naming calls
        main.handle_message(self.cfg, self.tg, msg(text="and hotels"),
                            self.state)
        self.assertEqual(len(self._naming_calls(chat_mock)), 1)
        self.assertEqual(self.state.meta[(100, None)]["name"], "Trip Planning")

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_model_title_applied_before_the_reply(self, chat_mock, price_mock):
        """THE RENAME COMES FIRST: the model title is the session's name
        while the reply is still being generated - and it stays the one and
        only rename."""
        first = "Fix the checkout total rounding bug"
        seen = {}

        def fake(name, api_key, model, messages, **kw):
            if self._is_naming(messages):
                return ("Model Title", "", None)
            seen["name"] = (self.state.meta.get((100, None)) or {}).get("name")
            return ("answer", "", None)

        chat_mock.side_effect = fake
        main.handle_message(self.cfg, self.tg, msg(text=first), self.state)
        self.assertEqual(seen["name"], "Model Title")  # renamed BEFORE reply
        meta = self.state.meta[(100, None)]
        self.assertEqual(meta["name"], "Model Title")  # exactly one rename
        self.assertEqual(meta["name_src"], "llm")
        # ...and the title call physically preceded the reply call
        first_call = chat_mock.call_args_list[0][0][3]
        self.assertTrue(self._is_naming(first_call))

    @mock.patch.object(providers, "chat")
    def test_rename_is_done_before_the_message_reaches_the_api(self, chat_mock):
        """Ordering contract: the session title runs (and is applied) before
        the message is handed to the model - not after the reply returns."""
        order = []
        chat_mock.side_effect = lambda *a, **k: order.append("api") or ("a", "", None)
        with mock.patch.object(main, "_maybe_name_session",
                               lambda *a, **k: order.append("title-start")
                               or {"handle": 1}), \
             mock.patch.object(main, "_finish_naming",
                               lambda *a, **k: order.append("renamed")):
            main.handle_message(self.cfg, self.tg, msg(text="name me"),
                                self.state)
        self.assertEqual(order, ["title-start", "renamed", "api"])

    def test_slow_title_still_renames_before_the_reply(self):
        """Past TITLE_WAIT the model title is abandoned and the derived one
        is applied instead - still BEFORE the reply starts, never after."""
        handle = {"usage": {}, "provider": "go", "model": "m",
                  "result": [None], "raw": "plan a trip to Da Lat",
                  "images": None}
        hang = threading.Event()
        t = threading.Thread(target=lambda: hang.wait(5), daemon=True)
        handle["thread"] = t
        t.start()
        main._finish_naming(self.cfg, self.tg, self.state, 100, None, handle,
                            timeout=0.01)
        meta = self.state.meta[(100, None)]
        self.assertTrue(meta.get("name"))            # a name exists...
        self.assertEqual(meta["name_src"], "derived")  # ...the fallback one
        self.assertNotIn("_naming", meta)            # attempt is spent
        hang.set()

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_new_with_explicit_name_skips_naming(self, chat_mock, price_mock):
        chat_mock.side_effect = self._chat(["Should Not Apply"])
        main.handle_message(self.cfg, self.tg, msg(text="/new my trip"),
                            self.state)
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertEqual(len(self._naming_calls(chat_mock)), 0)
        self.assertEqual(self.state.meta[(100, None)]["name"], "my trip")
        self.assertEqual(self.state.meta[(100, None)]["name_src"], "user")

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_user_rename_never_overwritten(self, chat_mock, price_mock):
        chat_mock.side_effect = self._chat([""])  # naming yields nothing
        main.handle_message(self.cfg, self.tg, msg(text="hello there"),
                            self.state)
        self.assertEqual(self.state.meta[(100, None)]["name_src"], "derived")
        main.handle_message(self.cfg, self.tg, msg(text="/rename my name"),
                            self.state)
        self.assertEqual(self.state.meta[(100, None)]["name_src"], "user")
        main.handle_message(self.cfg, self.tg, msg(text="and again"),
                            self.state)
        # user names settle it: no further naming calls, name untouched
        self.assertEqual(len(self._naming_calls(chat_mock)), 1)
        self.assertEqual(self.state.meta[(100, None)]["name"], "my name")

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_title_renames_topic_in_flow(self, chat_mock, price_mock):
        self.cfg.topic_flow = True
        chat_mock.side_effect = self._chat(["Cats"])
        main.handle_message(self.cfg, self.tg, msg(text="/new"), self.state)
        tid = self.tg.topics["New session"]
        main.handle_message(self.cfg, self.tg,
                            msg(text="tell me about cats", message_thread_id=tid),
                            self.state)
        self.assertEqual(self.state.meta[(100, tid)]["name"], "Cats")
        self.assertIn((100, tid, "Cats"), self.tg.renamed)  # topic mirrors it

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_media_only_message_named_from_media(self, chat_mock, price_mock):
        chat_mock.side_effect = self._chat(["Sunset Photo"])
        m = msg(text=None, photo=[{"file_id": "f1", "file_size": 100}])
        main.handle_message(self.cfg, self.tg, m, self.state)
        self.assertEqual(self.state.meta[(100, None)]["name"], "Sunset Photo")
        # the naming call got a media descriptor (vision off in this stub)
        naming_msgs = self._naming_calls(chat_mock)[0][0][3]
        self.assertIn("media file", naming_msgs[1]["content"])

    @mock.patch.object(providers, "price_for")
    @mock.patch.object(providers, "chat")
    def test_media_only_message_with_vision_sends_image(self, chat_mock,
                                                        price_mock):
        price_mock.return_value = {"image": True}
        chat_mock.side_effect = self._chat(["Nice Pic"])
        m = msg(text=None, photo=[{"file_id": "f1", "file_size": 100}])
        main.handle_message(self.cfg, self.tg, m, self.state)
        naming_content = self._naming_calls(chat_mock)[0][0][3][1]["content"]
        self.assertIsInstance(naming_content, list)
        self.assertTrue(any(p.get("type") == "image_url"
                            for p in naming_content))
        self.assertEqual(self.state.meta[(100, None)]["name"], "Nice Pic")

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_title_marker_and_quotes_stripped(self, chat_mock, price_mock):
        """Light hygiene only - word choice is left to the prompt."""
        chat_mock.side_effect = self._chat(['Title: "Docker Networking"'])
        main.handle_message(self.cfg, self.tg, msg(text="help me deploy"),
                            self.state)
        self.assertEqual(self.state.meta[(100, None)]["name"],
                         "Docker Networking")

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_json_constrained_output_extracted(self, chat_mock, price_mock):
        for reply, want in (
                ('{"title": "Docker Networking Setup"}',
                 "Docker Networking Setup"),
                ('```json\n{"title": "Kubernetes Ingress TLS"}\n```',
                 "Kubernetes Ingress TLS")):
            chat_mock.side_effect = self._chat([reply])
            state = main.State(True)
            main.handle_message(self.cfg, self.tg, msg(text="fix ingress"), state)
            self.assertEqual(state.meta[(100, None)]["name"], want)

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_answer_shaped_title_rejected_derived_stays(self, chat_mock,
                                                        price_mock):
        """>12 words = the model answered instead of titling: REJECTED (never
        truncated), the derived title stays (oh-my-pi #7306)."""
        answerish = ("I don't have any context about the registration system "
                     "in this conversation so I cannot help with that now")
        chat_mock.side_effect = self._chat([answerish])
        main.handle_message(self.cfg, self.tg,
                            msg(text="sort out the registration form"),
                            self.state)
        meta = self.state.meta[(100, None)]
        self.assertEqual(meta["name"], "sort out the registration form")
        self.assertEqual(meta["name_src"], "derived")  # not the answer

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_tool_call_text_rejected(self, chat_mock, price_mock):
        garbage = '{"title": "<function=bash>pwd</function>"}'
        chat_mock.side_effect = self._chat([garbage])
        main.handle_message(self.cfg, self.tg, msg(text="run a command"),
                            self.state)
        meta = self.state.meta[(100, None)]
        self.assertEqual(meta["name"], "run a command")  # derived kept
        self.assertEqual(meta["name_src"], "derived")

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_long_title_accepted_without_cap(self, chat_mock, price_mock):
        """Legit wordy-but-title-shaped output passes verbatim: 10 words,
        >80 chars - no char cap, no word truncation."""
        long_ok = ("Optimize PostgreSQL database connection pool exhaustion "
                   "in checkout payment microservice latency")
        self.assertGreater(len(long_ok.split()), 7)
        self.assertGreater(len(long_ok), 80)
        chat_mock.side_effect = self._chat([long_ok])
        main.handle_message(self.cfg, self.tg, msg(text="db pool issue"),
                            self.state)
        self.assertEqual(self.state.meta[(100, None)]["name"], long_ok)

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_single_attempt_derived_fallback_sticks(self, chat_mock,
                                                    price_mock):
        """One attempt per session: faulty output -> derived fallback, and
        later messages never rename again (retries would churn the name)."""
        garbage = ("Sorry, as an AI assistant I need much more information "
                   "before I can possibly help you with this request today")
        chat_mock.side_effect = self._chat([garbage] * 10)
        for i in range(4):
            main.handle_message(self.cfg, self.tg, msg(text="message %d" % i),
                                self.state)
        self.assertEqual(len(self._naming_calls(chat_mock)), 1)  # one attempt
        meta = self.state.meta[(100, None)]
        self.assertEqual(meta["name"], "message 0")   # derived, never garbage
        self.assertEqual(meta.get("_naming"), None)   # flag cleared

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_media_fallback_placeholder_on_faulty_output(self, chat_mock,
                                                         price_mock):
        chat_mock.side_effect = self._chat(
            ["I cannot possibly name this media because there is not enough "
             "context available in the conversation for me right now friend"])
        m = msg(text=None, photo=[{"file_id": "f1", "file_size": 100}])
        main.handle_message(self.cfg, self.tg, m, self.state)
        meta = self.state.meta[(100, None)]
        self.assertEqual(meta["name"], "Photo")  # media placeholder fallback
        self.assertEqual(meta["name_src"], "derived")

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_naming_usage_recorded(self, chat_mock, price_mock):
        chat_mock.side_effect = self._chat(
            ["Counted Title"],
            usage={"prompt_tokens": 50, "completion_tokens": 10})
        main.handle_message(self.cfg, self.tg, msg(text="hello world"),
                            self.state)
        meta = self.state.meta[(100, None)]
        self.assertEqual(int(meta["tokens_in"]), 50)
        self.assertEqual(int(meta["tokens_out"]), 10)


class TestConfigPersistence(unittest.TestCase):
    """topic_flow + /model global: surgical config.toml writes (comments kept)."""

    def _cfg(self, d):
        import config as config_mod
        import tomllib
        path = os.path.join(d, "config.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('# my comment\nbot_token = "t"\n'
                    'default_model = "zen/glm-5.3-flash"\n\n'
                    "[providers.zen]\napi_key = \"zk\"\n")
        with open(path, "rb") as f:
            return config_mod.Config(tomllib.load(f), path=path), path

    def test_set_flow_persists_and_survives_reload(self):
        import config as config_mod
        import tomllib
        with tempfile.TemporaryDirectory() as d:
            cfg, path = self._cfg(d)
            main._set_flow(cfg, True)
            self.assertTrue(cfg.topic_flow)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("topic_flow = true", content)
            self.assertIn("# my comment", content)   # comments preserved
            with open(path, "rb") as f:
                reloaded = config_mod.Config(tomllib.load(f), path=path)
            self.assertTrue(reloaded.topic_flow)     # survives a restart

    @mock.patch.object(providers, "chat")
    def test_model_global_writes_config(self, chat_mock):
        import config as config_mod
        import tomllib
        with tempfile.TemporaryDirectory() as d:
            cfg, path = self._cfg(d)
            tg = FakeTG()
            main.handle_message(cfg, tg,
                                msg(text="/model global go/mimo-v2.6-flash"),
                                main.State(True))
            self.assertEqual(cfg.default_model, "go/mimo-v2.6-flash")
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn('default_model = "go/mimo-v2.6-flash"', content)
            self.assertIn("# my comment", content)
            self.assertNotIn("zen/glm-5.3-flash", content)
            self.assertIn("written to", tg.sent[-1][1])
            with open(path, "rb") as f:
                reloaded = config_mod.Config(tomllib.load(f), path=path)
            self.assertEqual(reloaded.default_model, "go/mimo-v2.6-flash")

    def test_write_value_inserts_missing_key_at_top_level(self):
        import config as config_mod
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.toml")
            with open(path, "w", encoding="utf-8") as f:
                f.write('bot_token = "t"\n\n[providers.zen]\napi_key = "k"\n')
            cfg = config_mod.Config({}, path=path)
            config_mod.write_value(cfg, "topic_flow", True)
            with open(path, encoding="utf-8") as f:
                lines = [l for l in f.read().splitlines() if l.strip()]
            self.assertEqual(lines[1], "topic_flow = true")  # before any section
            self.assertEqual(lines[2], "[providers.zen]")


class TestSystemPromptAndClock(unittest.TestCase):
    """SYSTEM.md as the system prompt (live per request) and the [time] block
    that tells the model the current time plus when this session's agent last
    worked, so it can tell a quiet minute from a quiet day."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        disable_titles(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config_mod.Config(
            {"bot_token": "t", "allowed_users": [42], "stream_drafts": False,
             "rich_messages": True, "context_limit_tokens": 30_000,
             "providers": {"zen": {"api_key": "zk"}, "go": {"api_key": "gk"}}},
            path=os.path.join(self.tmp.name, "config.toml"))
        self._write_system("FIRST SYSTEM PROMPT")

    def _write_system(self, text):
        with open(os.path.join(self.tmp.name, config_mod.SYSTEM_FILE), "w",
                  encoding="utf-8") as fh:
            fh.write(text + "\n")

    def _run_turn(self, text):
        """Drive one turn and return the messages sent to the model."""
        seen = {}

        def fake(name, api_key, model, messages, **kw):
            seen["messages"] = messages
            return "ok", "", None

        with mock.patch.object(providers, "chat", side_effect=fake), \
                mock.patch.object(providers, "price_for", return_value=None):
            main.handle_message(self.cfg, self.tg, msg(text=text), self.state)
        return seen.get("messages") or []

    @staticmethod
    def _system(messages):
        return [m for m in messages if m.get("role") == "system"]

    def test_first_message_carries_file_prompt_and_clock(self):
        msgs = self._run_turn("hello")
        sys_msgs = self._system(msgs)
        self.assertTrue(sys_msgs, "no system message was sent")
        content = sys_msgs[0]["content"]
        self.assertIn("FIRST SYSTEM PROMPT", content)   # from SYSTEM.md
        self.assertIn("[time]", content)
        self.assertIn("UTC", content)                   # current time present
        self.assertIn("none (first message in this session)", content)

    def test_system_prompt_edit_is_live_on_the_next_message(self):
        """No restart, no reload: the second turn must already carry the
        rewritten SYSTEM.md."""
        self._run_turn("hello")
        self._write_system("SECOND SYSTEM PROMPT")
        content = self._system(self._run_turn("hello again"))[0]["content"]
        self.assertIn("SECOND SYSTEM PROMPT", content)
        self.assertNotIn("FIRST SYSTEM PROMPT", content)

    def test_clock_reports_the_gap_since_the_previous_turn(self):
        self._run_turn("hello")
        content = self._system(self._run_turn("hello again"))[0]["content"]
        self.assertNotIn("none (first message", content)
        self.assertIn(" ago", content)      # last activity -> elapsed gap
        self.assertIn("UTC", content)

    def test_clock_block_formatting(self):
        self.assertEqual(main._fmt_ago(7), "7s")
        self.assertEqual(main._fmt_ago(75), "1m 15s")
        self.assertEqual(main._fmt_ago(3 * 3600 + 9 * 60), "3h 9m")
        self.assertEqual(main._fmt_ago(3 * 86400 + 6 * 3600), "3d 6h")
        self.assertEqual(main._fmt_ago(-5), "0s")       # clock skew: never negative

    def test_last_activity_follows_the_session_not_the_object(self):
        state = main.State(True)
        self.assertIsNone(state.last_activity(1, None))  # never ran a turn
        history = state.get_history(1, None)
        history.append({"role": "user", "content": "hi"})
        state.persist(1, None)   # what a tool boundary / turn end does
        self.assertIsNotNone(state.last_activity(1, None))
        self.assertIsNone(state.last_activity(2, None))  # other chat untouched


if __name__ == "__main__":
    unittest.main()


class TestRestartCommand(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    def test_restart_in_main_chat_acknowledges(self):
        """One reply: the command was received. The restart itself is
        confirmed later by the start notifier, once the poll loop is live."""
        with mock.patch.object(main, "_restart_pending", return_value=False), \
             mock.patch.object(main, "_mark_restart_requested"), \
             mock.patch.object(main, "_code_imports", return_value=(True, "")), \
             mock.patch.object(main, "_schedule_restart") as sched:
            main.handle_message(self.cfg, self.tg, msg(text="/restart"), self.state)
        self.assertEqual(sched.call_count, 1)
        self.assertIn("restarting now", self.tg.sent[-1][1])
        self.assertEqual(len(self.tg.sent), 1)  # exactly one message

    def test_restart_refused_when_code_does_not_import(self):
        """A restart into broken code leaves no bot to send /restart again:
        the guard must keep the RUNNING one alive and say why."""
        boom = "Traceback (most recent call last):\nSyntaxError: invalid syntax"
        with mock.patch.object(main, "_restart_pending", return_value=False), \
             mock.patch.object(main, "_code_imports", return_value=(False, boom)), \
             mock.patch.object(main, "_schedule_restart") as sched:
            main.handle_message(self.cfg, self.tg, msg(text="/restart"), self.state)
        self.assertEqual(sched.call_count, 0)          # never scheduled
        self.assertIn("does not import", self.tg.sent[-1][1])
        self.assertIn("SyntaxError", self.tg.sent[-1][1])


    def test_concurrent_restart_is_debounced(self):
        """Two /restart in quick succession queue two transient units; each
        boot kills the one before it and nothing ever becomes ready, so the
        user hears nothing at all. The second one says so instead."""
        with mock.patch.object(main, "_restart_pending", return_value=True), \
             mock.patch.object(main, "_code_imports") as guard, \
             mock.patch.object(main, "_schedule_restart") as sched:
            main.handle_message(self.cfg, self.tg, msg(text="/restart"), self.state)
        self.assertEqual(sched.call_count, 0)
        guard.assert_not_called()          # no 120ms import check either
        self.assertIn("already in progress", self.tg.sent[-1][1])

    def test_restart_clears_the_start_cooldown(self):
        """A deliberate restart must announce itself even if a crash
        announced < 60s ago."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            logs = os.path.join(d, "logs")
            os.makedirs(logs)
            marker = os.path.join(logs, "keirai.started.marker")
            open(marker, "w").close()
            main._mark_restart_requested(d)
            self.assertFalse(os.path.exists(marker))                  # cleared
            self.assertTrue(os.path.exists(main._restart_mark(d)))    # stamped
            self.assertTrue(main._restart_pending(d))

    def test_stale_restart_mark_is_not_pending(self):
        import tempfile, time as _t
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "logs"))
            main._mark_restart_requested(d)
            old = _t.time() - (main.RESTART_DEBOUNCE + 5)
            os.utime(main._restart_mark(d), (old, old))
            self.assertFalse(main._restart_pending(d))

    def test_import_guard_catches_a_broken_tree(self):
        """Real check, real subprocess: a package that fails to import."""
        import subprocess, tempfile, textwrap
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "main.py"), "w") as fh:
                fh.write(textwrap.dedent("""
                    import definitely_not_a_real_module_xyz
                """))
            ok, err = main._code_imports(d)
        self.assertFalse(ok)
        self.assertIn("definitely_not_a_real_module_xyz", err)

    def test_import_guard_passes_on_a_sound_tree(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "main.py"), "w") as fh:
                fh.write("value = 1\n")
            ok, err = main._code_imports(d)
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_restart_refused_inside_a_topic(self):
        self.cfg.topic_flow = True  # else the topic message is ignored outright
        with mock.patch.object(main, "_schedule_restart") as sched:
            main.handle_message(self.cfg, self.tg,
                                msg(text="/restart", message_thread_id=77), self.state)
        self.assertEqual(sched.call_count, 0)
        self.assertIn("main chat", self.tg.sent[-1][1])

    def test_restart_reports_scheduling_failure(self):
        with mock.patch.object(main, "_restart_pending", return_value=False), \
             mock.patch.object(main, "_mark_restart_requested"), \
             mock.patch.object(main, "_code_imports", return_value=(True, "")), \
             mock.patch.object(main, "_schedule_restart",
                               side_effect=RuntimeError("sudo: a password is required")):
            main.handle_message(self.cfg, self.tg, msg(text="/restart"), self.state)
        self.assertIn("restart failed", self.tg.sent[-1][1])
        self.assertIn("password", self.tg.sent[-1][1])

    def test_restart_survives_its_own_cgroup(self):
        """The restart must be queued outside this service's cgroup, or the
        kill takes the command that asked for it."""
        with mock.patch.object(main.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            main._schedule_restart()
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["sudo", "-n", "systemd-run"])
        self.assertTrue(any(a.startswith("--on-active=") for a in argv))
        self.assertIn("restart", argv)
        self.assertIn(main.SERVICE_UNIT, argv)


class TestHelpRendering(unittest.TestCase):
    def test_descriptions_are_html_escaped(self):
        """The help body is HTML, but a description is prose. An unescaped
        "<provider/model-id>" made Telegram reject the whole message with
        HTTP 400 ("Unsupported start tag") - /help and /start died silently."""
        import html as _html
        body = main.build_help()
        for name, (fn, desc, hidden) in main.COMMANDS.items():
            if hidden or "<" not in desc:
                continue
            self.assertIn(_html.escape(desc), body, "description of /%s is not escaped" % name)
        # and no stray tag-shaped prose survives: every <...> is an allowed tag
        import re
        bad = re.findall(r"<(?!/?(?:b|i|u|s|code|pre|a|tg-spoiler)\b)[^>]{0,40}>", body)
        self.assertEqual(bad, [])

class TestSessionHeader(unittest.TestCase):
    """x-opencode-session: one stable, hashed id per keirai session."""

    def test_stable_per_session(self):
        h = main._session_header("20261108-0003-ab12")
        self.assertEqual(h, main._session_header("20261108-0003-ab12"))
        self.assertEqual(len(h), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_distinct_across_sessions(self):
        ids = ["20261108-0003-ab12", "20261108-0003-ab13", "20261108-0004-ab12"]
        self.assertEqual(len({main._session_header(i) for i in ids}), 3)
        # ... and it is not the old app-wide constant that merged every
        # conversation into a single routing/cache bucket
        self.assertNotIn("keirai", {main._session_header(i) for i in ids})

    def test_raw_id_never_leaks(self):
        for raw in ("20261108-0003-ab12", "", None):
            out = main._session_header(raw)
            self.assertNotIn("20261108", out)
            self.assertRegex(out, r"^[0-9a-f]{32}$")
