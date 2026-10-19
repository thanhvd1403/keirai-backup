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


if __name__ == "__main__":
    unittest.main()
