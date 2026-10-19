"""textcalls.recover: text-form tool calls parsed back into real rounds.

Markup is built by concatenation: a payload containing the literal closing-
parameter tag gets truncated in transit, so the test
source itself must not contain it either.
"""
import unittest

import textcalls

P = "<" + "/parameter>"
OP = "<" + "/function>"


def markup(fn, *params, wrap=True):
    body = "".join("<parameter=%s>%s%s" % (k, v, P) for k, v in params)
    call = "<function=%s>%s%s" % (fn, body, OP)
    if wrap:
        call = "<tool_call>" + call + "</tool_call>"
    return call


class TestRecover(unittest.TestCase):

    def test_text_form_roundtrip(self):
        # the plain-text form, rebuilt without literal closing tags
        ans = ("yields: " + markup("bash", ("command", "go version"))
               + " end")
        calls, rest = textcalls.recover(ans)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")
        self.assertEqual(calls[0]["arguments"], {"command": "go version"})
        self.assertTrue(calls[0]["id"].startswith("call_"))
        self.assertEqual(" ".join(rest.split()), "yields: end")

    def test_colon_style_and_name_attr(self):
        inner = ('<function:read_file><parameter name="path">/etc/hosts'
                 + P + OP)
        calls, rest = textcalls.recover(inner)
        self.assertEqual(calls[0]["name"], "read_file")
        self.assertEqual(calls[0]["arguments"], {"path": "/etc/hosts"})
        self.assertEqual(rest, "")

    def test_two_calls_in_one_answer(self):
        ans = (markup("bash", ("command", "pwd"))
               + " then " + markup("bash", ("command", "ls"), wrap=False))
        calls, rest = textcalls.recover(ans)
        self.assertEqual([c["name"] for c in calls], ["bash", "bash"])
        self.assertEqual(calls[1]["arguments"], {"command": "ls"})
        self.assertEqual(rest, "then")

    def test_entities_unescaped(self):
        calls, _ = textcalls.recover(
            markup("bash", ("command", "a &amp;&amp; b &lt;x&gt;")))
        self.assertEqual(calls[0]["arguments"]["command"], "a && b <x>")

    def test_call_ids_unique_per_call(self):
        ans = markup("bash", ("command", "a")) + markup("bash", ("command", "b"))
        calls, _ = textcalls.recover(ans)
        self.assertNotEqual(calls[0]["id"], calls[1]["id"])

    def test_no_markup_untouched(self):
        self.assertEqual(textcalls.recover("plain answer"),
                         ([], "plain answer"))
        self.assertEqual(textcalls.recover(None), ([], None))
        self.assertEqual(textcalls.recover(""), ([], ""))

    def test_unparseable_markup_falls_back(self):
        # an open tag we cannot match -> old deliver-as-text path still works
        weird = "<function broken stuff"
        self.assertEqual(textcalls.recover(weird), ([], weird))

    def test_prose_and_code_stay_intact(self):
        ans = ("use `ls` for listing. " + markup("bash", ("command", "ls -la"))
               + " Done? </not-a-tag>")
        calls, rest = textcalls.recover(ans)
        self.assertEqual(len(calls), 1)
        self.assertIn("use `ls` for listing.", rest)
        self.assertIn("Done?", rest)


class TestDSML(unittest.TestCase):
    """DeepSeek DSML envelope (V3.2 / V4 / V4.1) leaking into `content`."""

    # V4.1 markup: a space inside every tag
    def test_v41_spaced_dialect(self):
        ans = ("look:\n"
               "<｜DSML｜ calls>\n"
               "<｜DSML｜ invoke name=\"bash\">\n"
               "<｜DSML｜ parameter name=\"command\" string=\"true\">"
               "ls -la /etc/hosts</｜DSML｜ parameter>\n"
               "</｜DSML｜ invoke>\n"
               "</｜DSML｜ calls>")
        calls, rest = textcalls.recover(ans)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")
        self.assertEqual(calls[0]["arguments"], {"command": "ls -la /etc/hosts"})
        self.assertTrue(calls[0]["id"].startswith("call_"))
        self.assertEqual(rest.strip(), "look:")

    def test_v4_block(self):
        ans = ("<｜DSML｜tool_calls>\n"
               "<｜DSML｜invoke name=\"get_weather\">\n"
               "<｜DSML｜parameter name=\"location\" string=\"true\">"
               "San Francisco, CA</｜DSML｜parameter>\n"
               "</｜DSML｜invoke>\n"
               "</｜DSML｜tool_calls><｜end▁of▁sentence｜>")
        calls, rest = textcalls.recover(ans)
        self.assertEqual(calls[0]["name"], "get_weather")
        self.assertEqual(calls[0]["arguments"],
                         {"location": "San Francisco, CA"})
        self.assertEqual(rest, "")            # eos marker gone with the tag

    def test_v32_function_calls_block(self):
        ans = ("<｜DSML｜function_calls>"
               "<｜DSML｜invoke name=\"search\">"
               "<｜DSML｜parameter name=\"query\" string=\"true\">golang</｜DSML｜parameter>"
               "</｜DSML｜invoke>"
               "</｜DSML｜function_calls>")
        calls, rest = textcalls.recover(ans)
        self.assertEqual(calls[0]["name"], "search")
        self.assertEqual(calls[0]["arguments"], {"query": "golang"})
        self.assertEqual(rest, "")

    def test_ascii_pipe_variant(self):
        ans = ("<|DSML| invoke name=\"read_file\">"
               "<|DSML| parameter name=\"path\" string=\"true\">/etc/hosts</|DSML| parameter>"
               "</|DSML| invoke>")
        calls, rest = textcalls.recover(ans)
        self.assertEqual(calls[0]["name"], "read_file")
        self.assertEqual(calls[0]["arguments"], {"path": "/etc/hosts"})
        self.assertEqual(rest, "")

    def test_string_true_is_raw_string(self):
        # a shell command with quotes and operators must arrive byte-exact
        cmd = 'grep -E "WARNING|ERROR" logs/keirai.log'
        ans = ("<｜DSML｜ invoke name=\"bash\">"
               "<｜DSML｜ parameter name=\"command\" string=\"true\">" + cmd
               + "</｜DSML｜ parameter>"
               "</｜DSML｜ invoke>")
        calls, _ = textcalls.recover(ans)
        self.assertEqual(calls[0]["arguments"], {"command": cmd})

    def test_string_false_parses_json(self):
        ans = ("<｜DSML｜ invoke name=\"read_file\">"
               "<｜DSML｜ parameter name=\"max_chars\" string=\"false\">5000"
               "</｜DSML｜ parameter>"
               "<｜DSML｜ parameter name=\"deep\" string=\"false\">{\"a\": 1}"
               "</｜DSML｜ parameter>"
               "</｜DSML｜ invoke>")
        calls, _ = textcalls.recover(ans)
        self.assertEqual(calls[0]["arguments"],
                         {"max_chars": 5000, "deep": {"a": 1}})

    def test_missing_string_attr_defaults_to_json_then_text(self):
        # upstream reference rule: absent string= behaves like false
        ans = ("<｜DSML｜ invoke name=\"read_file\">"
               "<｜DSML｜ parameter name=\"max_chars\">5000</｜DSML｜ parameter>"
               "<｜DSML｜ parameter name=\"path\">/etc/hosts</｜DSML｜ parameter>"
               "</｜DSML｜ invoke>")
        calls, _ = textcalls.recover(ans)
        self.assertEqual(calls[0]["arguments"],
                         {"max_chars": 5000, "path": "/etc/hosts"})

    def test_wrapper_less_invokes(self):
        # V4.1 emits invokes with no enclosing block
        ans = ("<｜DSML｜ invoke name=\"bash\">"
               "<｜DSML｜ parameter name=\"command\" string=\"true\">pwd"
               "</｜DSML｜ parameter>"
               "</｜DSML｜ invoke>")
        calls, rest = textcalls.recover(ans)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"], {"command": "pwd"})
        self.assertNotIn("DSML", rest)

    def test_two_invokes_parallel(self):
        ans = ("<｜DSML｜ calls>"
               "<｜DSML｜ invoke name=\"bash\">"
               "<｜DSML｜ parameter name=\"command\" string=\"true\">a"
               "</｜DSML｜ parameter></｜DSML｜ invoke>"
               "<｜DSML｜ invoke name=\"bash\">"
               "<｜DSML｜ parameter name=\"command\" string=\"true\">b"
               "</｜DSML｜ parameter></｜DSML｜ invoke>"
               "</｜DSML｜ calls>")
        calls, rest = textcalls.recover(ans)
        self.assertEqual([c["arguments"]["command"] for c in calls], ["a", "b"])
        self.assertEqual(rest, "")

    def test_overlapping_spans_cut_once(self):
        # block + nested invoke overlap: cutting both against shifted text
        # would corrupt whatever follows
        ans = ("PRE " + "<｜DSML｜ calls>"
               "<｜DSML｜ invoke name=\"bash\">"
               "<｜DSML｜ parameter name=\"command\" string=\"true\">x"
               "</｜DSML｜ parameter></｜DSML｜ invoke>"
               "</｜DSML｜ calls>" + " POST")
        calls, rest = textcalls.recover(ans)
        self.assertEqual(len(calls), 1)
        self.assertEqual(" ".join(rest.split()), "PRE POST")

    def test_dsml_and_legacy_coexist_in_order(self):
        ans = ("<｜DSML｜ invoke name=\"bash\">"
               "<｜DSML｜ parameter name=\"command\" string=\"true\">1"
               "</｜DSML｜ parameter></｜DSML｜ invoke>"
               " mid "
               + markup("read_file", ("path", "/etc/hosts"), wrap=False))
        calls, rest = textcalls.recover(ans)
        self.assertEqual([c["name"] for c in calls], ["bash", "read_file"])
        self.assertEqual(rest.strip(), "mid")

    def test_unparsed_block_is_stripped_not_delivered(self):
        # a DSML wrapper with nothing parseable: no raw XML in chat
        ans = "here: <｜DSML｜ calls> nothing known </｜DSML｜ calls>"
        calls, rest = textcalls.recover(ans)
        self.assertEqual(calls, [])
        self.assertNotIn("DSML", rest)

    def test_looks_like_gate(self):
        self.assertTrue(textcalls.looks_like("<｜DSML｜ calls>"))
        self.assertTrue(textcalls.looks_like("<|DSML| invoke>"))
        self.assertTrue(textcalls.looks_like("<invoke name=\"bash\">"))
        self.assertTrue(textcalls.looks_like("<function=bash>"))
        self.assertFalse(textcalls.looks_like("plain answer"))
        self.assertFalse(textcalls.looks_like(None))
        self.assertFalse(textcalls.looks_like(""))


class TestGuardAndSanitize(unittest.TestCase):
    """Gate, direct-JSON bodies, sanitize()."""

    def test_double_bar_and_spaced_ascii_now_parse(self):
        # the survey calls the double-bar shape the most common in live V4
        # SSE, and the spaced ASCII form turns up through trans-coding
        # proxies. The old literal gate rejected BOTH before the parser
        # ever saw them.
        for prefix in ("｜｜DSML｜｜", " | DSML | "):
            ans = ("<" + prefix + "invoke name=\"bash\"><" + prefix
                   + "parameter name=\"command\" string=\"true\">pwd"
                   + "</" + prefix + "parameter></" + prefix + "invoke>")
            calls, rest = textcalls.recover(ans)
            self.assertEqual(len(calls), 1, prefix)
            self.assertEqual(calls[0]["arguments"], {"command": "pwd"})
            self.assertEqual(rest, "")

    def test_direct_json_body_is_arguments(self):
        # "Format 2" in the vLLM/SGLang detectors: a bare JSON object filling
        # the invoke. Returning {} there was worse than leaking - a valid call
        # with no arguments, so a real tool ran and failed confusingly.
        ans = ("<｜DSML｜ invoke name=\"bash\">{\"command\": \"ls\"}"
               "</｜DSML｜ invoke>")
        calls, rest = textcalls.recover(ans)
        self.assertEqual(calls[0]["arguments"], {"command": "ls"})
        self.assertEqual(rest, "")

    def test_non_object_json_body_yields_no_args(self):
        # never invent an argument map for something that is not one
        ans = "<｜DSML｜ invoke name=\"bash\">[1, 2]</｜DSML｜ invoke>"
        calls, _ = textcalls.recover(ans)
        self.assertEqual(calls[0]["arguments"], {})

    def test_recover_returns_calls_it_cannot_vet(self):
        # the parser cannot know the tool list, so it returns every parsed
        # call. main is the one that answers an unavailable tool - the parser
        # must not silently decide a call is fake.
        ans = ("Example markup only: <｜DSML｜ invoke name=\"rm\">"
               "<｜DSML｜ parameter name=\"path\" string=\"true\">/etc"
               "</｜DSML｜ parameter></｜DSML｜ invoke> is the shape.")
        calls, rest = textcalls.recover(ans)
        self.assertEqual(calls[0]["name"], "rm")
        self.assertNotIn("DSML", rest)   # markup still leaves chat
        self.assertTrue(rest.startswith("Example"))

    def test_sanitize_strips_damage_keeps_prose(self):
        damaged = ("ran: <｜DSML｜ invoke name=\"bash\"><｜DSML｜ parameter"
                   " name=\"command\" string=\"true\">pwd ... then stopped")
        self.assertEqual(textcalls.sanitize(damaged),
                         "ran: pwd ... then stopped")
        self.assertEqual(textcalls.sanitize("plain answer about LS"),
                         "plain answer about LS")
        # pass-through, like recover(): nothing to strip, nothing invented
        self.assertIsNone(textcalls.sanitize(None))

    def test_has_markup_sees_damaged_markup(self):
        self.assertTrue(textcalls.has_markup(
            "<｜DSML｜ invoke name=\"bash\">"))       # bare opener
        self.assertTrue(textcalls.has_markup(
            "<function_calls><antml:invoke name=\"bash\">"))
        self.assertFalse(textcalls.has_markup("just text"))
        self.assertFalse(textcalls.has_markup(""))
        self.assertFalse(textcalls.has_markup(None))


if __name__ == "__main__":
    unittest.main()
