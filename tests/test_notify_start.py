"""Tests for the ExecStartPost "I'm back" notifier (subprocess/HTTP mocked)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
import notify_start  # noqa: E402


class TestBuildMessage(unittest.TestCase):
    def test_contains_pid_time_and_head(self):
        text = notify_start.build_message(1.2, 4242, "abc1234")
        self.assertIn("is up", text)
        self.assertIn("pid 4242", text)
        self.assertIn("answering after 1.2s", text)
        self.assertIn("abc1234", text)

    def test_survives_missing_pid_and_head(self):
        text = notify_start.build_message(0.4, None, None)
        self.assertIn("answering after 0.4s", text)
        self.assertNotIn("pid", text)


class TestWaitReady(unittest.TestCase):
    def test_returns_elapsed_when_answering(self):
        with mock.patch.object(notify_start, "_run", return_value="active"), \
             mock.patch.object(notify_start, "main_pid", return_value=123), \
             mock.patch.object(notify_start, "pid_alive", return_value=True), \
             mock.patch.object(notify_start, "telegram_answers", return_value=True):
            took = notify_start.wait_ready("tok", timeout=2)
        self.assertIsNotNone(took)
        self.assertLess(took, 2)

    def test_none_when_telegram_never_answers(self):
        """A dead bot must not produce an "I'm back" message."""
        with mock.patch.object(notify_start, "_run", return_value="active"), \
             mock.patch.object(notify_start, "main_pid", return_value=123), \
             mock.patch.object(notify_start, "pid_alive", return_value=True), \
             mock.patch.object(notify_start, "telegram_answers", return_value=False):
            self.assertIsNone(notify_start.wait_ready("tok", timeout=0.6))

    def test_none_when_unit_never_active(self):
        with mock.patch.object(notify_start, "_run", return_value="activating"):
            self.assertIsNone(notify_start.wait_ready("tok", timeout=0.6))


class TestDelivery(unittest.TestCase):
    def test_dry_run_sends_nothing(self):
        with mock.patch.object(notify_start, "wait_ready", return_value=0.5), \
             mock.patch.object(notify_start, "read_config", return_value=("tok", 1)), \
             mock.patch.object(notify_start, "send") as send:
            notify_start.main(["--dry-run"])
        send.assert_not_called()

    def test_sends_once_then_suppressed(self):
        with mock.patch.object(notify_start, "wait_ready", return_value=0.5), \
             mock.patch.object(notify_start, "read_config", return_value=("tok", 1)), \
             mock.patch.object(notify_start, "recently_announced", return_value=False), \
             mock.patch.object(notify_start, "touch_marker"), \
             mock.patch.object(notify_start, "send", return_value=True) as send:
            notify_start.main([])
        send.assert_called_once()

    def test_crash_loop_does_not_spam(self):
        with mock.patch.object(notify_start, "wait_ready", return_value=0.5), \
             mock.patch.object(notify_start, "read_config", return_value=("tok", 1)), \
             mock.patch.object(notify_start, "recently_announced", return_value=True), \
             mock.patch.object(notify_start, "send") as send:
            notify_start.main([])
        send.assert_not_called()

    def test_no_config_is_not_an_error(self):
        with mock.patch.object(notify_start, "wait_ready", return_value=0.5), \
             mock.patch.object(notify_start, "read_config", return_value=("", None)), \
             mock.patch.object(notify_start, "recently_announced", return_value=False), \
             mock.patch.object(notify_start, "send") as send:
            self.assertEqual(notify_start.main([]), 0)
        send.assert_not_called()
