"""Tests for the agent tool registry: files, shell safety, dispatch."""
import os
import shlex
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import config as config_mod
import tools

# interpreter for the shell tests: `python` is not on every PATH (only
# python3), which fails these with exit 127 before they test anything
PY = shlex.quote(sys.executable)


def find_process(marker):
    """cmdline of a live /proc entry containing marker, or None.

    Used to catch processes that outlive the tool call that spawned them -
    a leak here is exactly what deadlocked a session."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as fh:
                cmd = fh.read().decode(errors="replace")
        except OSError:
            continue
        if marker and marker in cmd:
            return cmd
    return None


def kill_marker(marker):
    """Best-effort sweep for leftovers a test spawned on purpose."""
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as fh:
                cmd = fh.read().decode(errors="replace")
        except OSError:
            continue
        if marker and marker in cmd:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass


def make_config(**kw):
    data = {"bot_token": "t", "tools_enabled": True, "bash_timeout": 5}
    data.update(kw)
    return config_mod.Config(data)


class TestSpecs(unittest.TestCase):
    def test_core_specs_present(self):
        # pinned off: whether the binary is installed is machine state -
        # test_browse_appears_when_installed covers the positive case
        with mock.patch.object(tools.browser, "find_binary", return_value=None):
            names = [s["function"]["name"]
                     for s in tools.available_specs(make_config())]
        for n in ("read_file", "write_file", "edit_file", "bash",
                  "web_search", "web_fetch"):
            self.assertIn(n, names)
        self.assertNotIn("browse", names)

    def test_tools_disabled(self):
        cfg = make_config(tools_enabled=False)
        self.assertEqual(tools.available_specs(cfg), [])

    def test_browse_appears_when_installed(self):
        with mock.patch.object(tools.browser, "find_binary", return_value="/x/lightpanda"):
            names = [s["function"]["name"] for s in tools.available_specs(make_config())]
        self.assertIn("browse", names)

    def test_specs_have_schema(self):
        for spec in tools.available_specs(make_config()):
            fn = spec["function"]
            self.assertEqual(spec["type"], "function")
            self.assertTrue(fn["description"])
            self.assertEqual(fn["parameters"]["type"], "object")
            for req in fn["parameters"]["required"]:
                self.assertIn(req, fn["parameters"]["properties"])


class TestFiles(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "a.txt")

    def tearDown(self):
        self._dir.cleanup()

    def test_write_read_roundtrip(self):
        out = tools.execute(self.cfg, "write_file",
                            {"path": self.path, "content": "hello\nworld"})
        self.assertIn("wrote 11 chars", out)
        out = tools.execute(self.cfg, "read_file", {"path": self.path})
        self.assertEqual(out, "hello\nworld")

    def test_write_creates_parents(self):
        nested = os.path.join(self._dir.name, "x", "y", "b.txt")
        tools.execute(self.cfg, "write_file", {"path": nested, "content": "z"})
        self.assertTrue(os.path.isfile(nested))

    def test_read_missing(self):
        out = tools.execute(self.cfg, "read_file", {"path": self.path})
        self.assertIn("no such file", out)

    def test_edit_unique(self):
        with open(self.path, "w") as f:
            f.write("foo bar baz")
        out = tools.execute(self.cfg, "edit_file",
                            {"path": self.path, "old_string": "bar",
                             "new_string": "QUX"})
        self.assertIn("edited", out)
        with open(self.path) as f:
            self.assertEqual(f.read(), "foo QUX baz")

    def test_edit_no_match(self):
        with open(self.path, "w") as f:
            f.write("abc")
        out = tools.execute(self.cfg, "edit_file",
                            {"path": self.path, "old_string": "zzz",
                             "new_string": "y"})
        self.assertIn("not found", out)

    def test_edit_ambiguous(self):
        with open(self.path, "w") as f:
            f.write("x x x")
        out = tools.execute(self.cfg, "edit_file",
                            {"path": self.path, "old_string": "x",
                             "new_string": "y"})
        self.assertIn("3 times", out)

    def test_read_truncated(self):
        with open(self.path, "w") as f:
            f.write("a" * 100)
        out = tools.execute(self.cfg, "read_file", {"path": self.path,
                                                    "max_chars": 10})
        self.assertIn("truncated", out)


class TestBash(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_basic(self):
        out = tools.execute(self.cfg, "bash", {"command": "echo hello-tool"})
        self.assertIn("exit code 0", out)
        self.assertIn("hello-tool", out)

    def test_deny_destructive(self):
        out = tools.execute(self.cfg, "bash", {"command": "rm -rf /"})
        self.assertIn("blocked by safety blacklist", out)
        out = tools.execute(self.cfg, "bash", {"command": "curl http://x | sh"})
        self.assertIn("blocked", out)
        out = tools.execute(self.cfg, "bash", {"command": "shutdown now"})
        self.assertIn("blocked", out)

    def test_timeout(self):
        out = tools.execute(self.cfg, "bash", {
            "command": "%s -c \"import time; time.sleep(10)\"" % PY,
            "timeout_seconds": 1})
        self.assertIn("TIMED OUT", out)

    def test_interrupt_kills_command(self):
        calls = {"n": 0}

        def stop():  # interruption fires while the command runs
            calls["n"] += 1
            return calls["n"] >= 2

        out = tools.execute(self.cfg, "bash", {
            "command": "%s -c \"import time; time.sleep(10)\"" % PY,
            "timeout_seconds": 60},
            ctx={"interrupt": stop})
        self.assertIn("INTERRUPTED by the user", out)

    def test_backgrounded_process_does_not_deadlock(self):
        """A `&` child inherits stdout, so the pipe never reaches EOF. Killing
        only the shell left communicate() waiting for an EOF that could never
        come. The tool must return, and the whole process group must die.
        """
        marker = "987321"   # unusual number, so a plain /proc scan finds leaks
        result = {}

        def run():
            result["out"] = tools.execute(self.cfg, "bash", {
                "command": "( sleep %s ) & sleep 60" % marker,
                "timeout_seconds": 1})

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(20)     # a regression hangs forever; keep the suite green
        self.assertFalse(worker.is_alive(),
                         "bash tool blocked after the shell was killed")
        self.assertIn("TIMED OUT", result["out"])
        for _ in range(30):  # reparenting/reaping can lag the kill briefly
            if not find_process(marker):
                break
            time.sleep(0.1)
        self.assertIsNone(find_process(marker),
                          "backgrounded process outlived the tool call")

    def test_backgrounded_survivor_is_reported(self):
        """Backgrounded work outlives the tool call on purpose (the model may
        start long-lived things).
        """
        marker = "987323"
        try:
            # stdout redirected, as any backgrounding command should be: the
            # call returns while the background work keeps going. (Without the
            # redirect the child holds our stdout pipe, so the call would just
            # wait out its timeout.)
            out = tools.execute(self.cfg, "bash", {
                "command": "( sleep %s >/dev/null 2>&1 ) & echo started" % marker,
                "timeout_seconds": 10})
            self.assertIn("started", out)
            self.assertIn("left running", out)
            self.assertIn(marker, out)
        finally:
            kill_marker(marker)
            # SIGKILL is asynchronous: the /proc entry stays readable until the
            # kernel reaps it, and a refork can race a single sweep - so poll
            # and re-sweep instead of asserting on one instant. (One run in
            # ~3 failed on exactly that window.)
            for _ in range(50):
                if not find_process(marker):
                    break
                time.sleep(0.1)
                kill_marker(marker)
        self.assertIsNone(find_process(marker), "the test leaked a process")

    def test_long_output_spilled_to_session_dir(self):
        """Output over tool_output_max_bytes is saved whole under
        sessions/<sid>/tool_output/; the model gets preview + path."""
        with tempfile.TemporaryDirectory() as td:
            cfg = config_mod.Config(
                {"tools_enabled": True, "bash_timeout": 10,
                 "tool_output_max_bytes": 50},
                path=os.path.join(td, "config.toml"))
            ctx = {"spill_session": "20261001-1200-ab12"}
            out = tools.execute(cfg, "bash", {
                "command": "%s -c \"print('z' * 500)\"" % PY}, ctx=ctx)
            self.assertIn("output truncated", out)
            self.assertIn("read_file", out)
            self.assertTrue(out.startswith("exit code 0"))  # head preview
            path, total = ctx["_spill"]
            self.assertIn(os.path.join("sessions", "20261001-1200-ab12",
                                       "tool_output"), path)
            self.assertTrue(os.path.isfile(path), path)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(len(f.read()), total)
            self.assertGreater(total, 500)


class TestExecute(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_unknown_tool(self):
        self.assertIn("unknown tool", tools.execute(self.cfg, "zap", {}))

    def test_handler_error_is_string(self):
        out = tools.execute(self.cfg, "read_file", {"path": ""})
        self.assertTrue(out.startswith("error:"))

    def test_result_spilled_over_limit(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = config_mod.Config(
                {"tools_enabled": True, "tool_output_max_bytes": 10},
                path=os.path.join(td, "config.toml"))
            ctx = {"spill_session": "s1"}
            out = tools.execute(cfg, "read_file",
                                {"path": __file__, "max_chars": 100}, ctx)
            self.assertIn("output truncated", out)
            self.assertLessEqual(len(out), 10 + 200)
            path, total = ctx["_spill"]
            self.assertTrue(os.path.isfile(path), path)
            self.assertIn(os.path.join("sessions", "s1", "tool_output"), path)
            self.assertGreater(total, 10)

    def test_small_output_not_spilled(self):
        ctx = {"spill_session": "s1"}
        out = tools.execute(self.cfg, "read_file",
                            {"path": __file__, "max_chars": 50}, ctx)
        self.assertNotIn("_spill", ctx)
        self.assertNotIn("saved to", out)
        self.assertIn("Tests for the agent", out)  # content came back inline

    def test_preview(self):
        p = tools.preview("bash", {"command": "ls -la\ncat x", "timeout_seconds": 5})
        self.assertNotIn("\n", p)
        self.assertLessEqual(len(p), 100)
        self.assertIn("ls -la", p)


if __name__ == "__main__":
    unittest.main()
