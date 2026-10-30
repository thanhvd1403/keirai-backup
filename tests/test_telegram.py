"""Tests for the Telegram Bot API client: wire format (method names, payloads)
and transport-error mapping.

Method names are the fragile part: the Bot API answers a wrong name with a
404 that `call()` turns into a TelegramError, and every cleanup that swallows
exceptions then fails silently - stale messages stay in the chat. These tests
pin the names and the payloads keirai sends.

Transport errors matter just as much: a raw OSError leaking out of call()
kills whichever thread called it - for the poll thread that means the bot
goes silent while the process keeps running (systemd never restarts it).
"""
import http.client
import unittest
from unittest import mock

import telegram


class WireFormat(unittest.TestCase):
    def setUp(self):
        self.tg = telegram.Telegram("TOKEN")
        self.calls = []

        def fake_call(method, payload=None, timeout=40):
            self.calls.append((method, payload or {}, timeout))
            return True

        self.tg.call = fake_call

    @property
    def last(self):
        return self.calls[-1][0], self.calls[-1][1]

    def test_delete_message_is_camel_case(self):
        # regression: this used to be "delete_message" (a 404), so every
        # live-message cleanup silently did nothing
        self.tg.delete_message(100, 5)
        method, payload = self.last
        self.assertEqual(method, "deleteMessage")
        self.assertEqual(payload, {"chat_id": 100, "message_id": 5})

    def test_send_rich_draft_streams_the_answer(self):
        self.tg.send_rich_draft(100, 42, markdown="partial **answer**",
                                thread_id=7, can_stop=True)
        method, payload = self.last
        self.assertEqual(method, "sendRichMessageDraft")
        self.assertEqual(payload["draft_id"], 42)
        self.assertEqual(payload["rich_message"],
                         {"markdown": "partial **answer**"})
        self.assertEqual(payload["message_thread_id"], 7)
        self.assertTrue(payload["can_stop"])  # Telegram's own Stop button

    def test_send_rich_draft_without_stop_omits_the_flag(self):
        self.tg.send_rich_draft(100, 42, html="<b>x</b>")
        self.assertNotIn("can_stop", self.last[1])
        self.assertNotIn("message_thread_id", self.last[1])
        self.assertEqual(self.last[1]["rich_message"], {"html": "<b>x</b>"})

    def test_send_rich_and_edit_rich_carry_rich_message(self):
        self.tg.send_rich(100, markdown="# hi")
        self.assertEqual(self.last[0], "sendRichMessage")
        self.assertEqual(self.last[1]["rich_message"], {"markdown": "# hi"})
        self.tg.edit_rich(100, 5, html="<details>x</details>")
        method, payload = self.last
        self.assertEqual(method, "editMessageText")
        self.assertEqual(payload["message_id"], 5)
        self.assertEqual(payload["rich_message"],
                         {"html": "<details>x</details>"})

    def test_plain_send_uses_html(self):
        self.tg.send(100, "<b>hi</b>", thread_id=3)
        method, payload = self.last
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(payload["message_thread_id"], 3)

    def test_polls_native_stop_updates(self):
        # the Stop button under a streamed draft arrives as
        # stopped_message_generation - it must be in allowed_updates
        self.tg.get_updates()
        method, payload = self.last
        self.assertEqual(method, "getUpdates")
        self.assertIn("message", payload["allowed_updates"])
        self.assertIn("stopped_message_generation", payload["allowed_updates"])

    def test_polls_with_offset(self):
        self.tg.get_updates(offset=9)
        self.assertEqual(self.last[1]["offset"], 9)


class TransportErrorsBecomeTelegramError(unittest.TestCase):
    """Every transport failure must surface as TelegramError.

    Observed shape:
        Exception in thread updates: ... TimeoutError: The read operation
        timed out   (ssl.py -> socket.py -> urllib/request.py:489)
    urlopen only wraps send-phase errors in URLError, so a timeout while
    READING the long-poll body escaped as a bare TimeoutError, killed the
    poll thread and the bot stopped responding while still running."""

    class _Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def setUp(self):
        self.tg = telegram.Telegram("TOKEN")

    def test_read_timeout_is_mapped(self):
        # a read timeout on the long poll
        with mock.patch("telegram.urllib.request.urlopen",
                        side_effect=TimeoutError(
                            "The read operation timed out")):
            with self.assertRaises(telegram.TelegramError) as cm:
                self.tg.get_updates()
        self.assertIn("getUpdates", str(cm.exception))

    def test_connection_reset_is_mapped(self):
        with mock.patch("telegram.urllib.request.urlopen",
                        side_effect=ConnectionResetError(
                            104, "Connection reset by peer")):
            with self.assertRaises(telegram.TelegramError):
                self.tg.send(1, "hi")

    def test_truncated_body_is_mapped(self):
        with mock.patch("telegram.urllib.request.urlopen",
                        side_effect=http.client.IncompleteRead(b"partial")):
            with self.assertRaises(telegram.TelegramError):
                self.tg.get_updates()

    def test_invalid_json_is_mapped(self):
        with mock.patch("telegram.urllib.request.urlopen",
                        return_value=self._Resp(b'{"ok": tr')):
            with self.assertRaises(telegram.TelegramError) as cm:
                self.tg.get_updates()
        self.assertIn("JSON", str(cm.exception))

    def test_non_dict_body_is_mapped(self):
        with mock.patch("telegram.urllib.request.urlopen",
                        return_value=self._Resp(b'"surprise"')):
            with self.assertRaises(telegram.TelegramError):
                self.tg.get_updates()

    def test_http_error_still_carries_the_body(self):
        err = urllib_error_for_test()
        with mock.patch("telegram.urllib.request.urlopen", side_effect=err):
            with self.assertRaises(telegram.TelegramError) as cm:
                self.tg.get_updates()
        self.assertIn("HTTP 400", str(cm.exception))


def urllib_error_for_test():
    import io
    import urllib.error
    return urllib.error.HTTPError("https://api.telegram.org", 400, "Bad",
                                  {}, io.BytesIO(b'{"description":"nope"}'))


class TestNotModified(unittest.TestCase):
    """'message is not modified' = our edit was byte-identical to what the
    message already shows: a no-op, never a failure. Treating it as one used
    to disable live streaming mid-turn (and made the thinking block fall
    back to a duplicate send)."""

    def test_detected(self):
        err = telegram.TelegramError(
            "editMessageText -> HTTP 400: {\"ok\":false,\"error_code\":400,"
            "\"description\":\"Bad Request: message is not modified: "
            "specified new message content and reply markup are exactly the "
            "same as a current content and reply markup of the message\"}")
        self.assertTrue(telegram.is_not_modified(err))

    def test_other_errors_are_not(self):
        self.assertFalse(telegram.is_not_modified(
            telegram.TelegramError("editMessageText -> HTTP 400: nope")))
        self.assertFalse(telegram.is_not_modified(
            ValueError("message is not modified")))
        self.assertFalse(telegram.is_not_modified(None))


if __name__ == "__main__":
    unittest.main()


class TestPollTimeout(unittest.TestCase):
    def test_default_is_a_long_poll(self):
        tg = telegram.Telegram("tok")
        with mock.patch.object(tg, "call", return_value=[]) as call:
            tg.get_updates(None)
        self.assertEqual(call.call_args[0][1]["timeout"], telegram.POLL_TIMEOUT)

    def test_zero_makes_it_non_blocking(self):
        """Startup readiness must not wait for the idle timeout to expire."""
        tg = telegram.Telegram("tok")
        with mock.patch.object(tg, "call", return_value=[]) as call:
            tg.get_updates(None, timeout=0)
        self.assertEqual(call.call_args[0][1]["timeout"], 0)
        self.assertEqual(call.call_args[1]["timeout"], 15)  # 0 + 15 socket slack
