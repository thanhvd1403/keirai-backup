import json
import os
import tempfile
import unittest
from unittest import mock

import providers


class TestNormalize(unittest.TestCase):
    def test_openai_style(self):
        data = {"data": [
            {"id": "glm-5.3-flash", "name": "GLM 5.3 Flash", "context_window": 1000000,
             "max_output": 65536, "cost": {"input": 0.15, "output": 0.5}},
            {"id": "aaa", "context": 4096},
        ]}
        models = providers.normalize_models(data)
        self.assertEqual([m["id"] for m in models], ["aaa", "glm-5.3-flash"])
        glm = models[1]
        self.assertEqual(glm["context"], 1000000)
        self.assertEqual(glm["cost_in"], 0.15)
        self.assertEqual(glm["cost_out"], 0.5)

    def test_models_dev_style(self):
        data = {"models": [{"id": "m1", "limit": {"context": 200000, "output": 8192},
                            "pricing": {"input": 1.0, "output": 4.0}}]}
        m = providers.normalize_models(data)[0]
        self.assertEqual(m["context"], 200000)
        self.assertEqual(m["max_output"], 8192)
        self.assertEqual(m["cost_in"], 1.0)
        self.assertEqual(m["cost_out"], 4.0)

    def test_dict_keyed(self):
        data = {"glm-5": {"name": "GLM 5", "context_length": 131072}}
        m = providers.normalize_models(data)[0]
        self.assertEqual(m["id"], "glm-5")
        self.assertEqual(m["context"], 131072)

    def test_garbage(self):
        self.assertEqual(providers.normalize_models("nope"), [])


class TestChatParsing(unittest.TestCase):
    def _chat(self, message, **kw):
        resp = {"choices": [{"message": message}]}
        with mock.patch.object(providers, "_request", return_value=resp) as req:
            out = providers.chat("zen", "key", "m", [{"role": "user", "content": "hi"}], **kw)
        req.assert_called_once()
        args = req.call_args
        self.assertIn("/chat/completions", args[0][0])
        self.assertEqual(args[1]["payload"]["model"], "m")
        return out, args

    def test_session_header_passed(self):
        _, args = self._chat({"content": "a"}, session_id="keirai-100-77")
        self.assertEqual(args[1]["session_id"], "keirai-100-77")

    def test_reasoning_content(self):
        (content, reasoning, tcs), _ = self._chat({"content": "answer", "reasoning_content": "thinking..."})
        self.assertEqual((content, reasoning, tcs), ("answer", "thinking...", None))

    def test_reasoning_dict(self):
        (content, reasoning, _tcs), _ = self._chat({"content": "a", "reasoning": {"content": "r"}})
        self.assertEqual(reasoning, "r")

    def test_no_reasoning(self):
        (content, reasoning, _tcs), _ = self._chat({"content": "a"})
        self.assertEqual(reasoning, "")


class TestCache(unittest.TestCase):
    def test_fetch_writes_cache_and_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(providers, "fetch_models", return_value=[{"id": "m1"}]) as fm:
                models, from_cache = providers.cached_models("zen", "k", d)
            self.assertFalse(from_cache)
            self.assertEqual(models[0]["id"], "m1")
            self.assertTrue(os.path.isfile(os.path.join(d, "models_zen.json")))

            # API dies -> cached copy served
            with mock.patch.object(providers, "fetch_models", side_effect=providers.ProviderError("down")):
                models, from_cache = providers.cached_models("zen", "k", d)
            self.assertTrue(from_cache)
            self.assertEqual(models[0]["id"], "m1")

    def test_no_key_no_cache_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(providers, "fetch_models", side_effect=providers.ProviderError("down")):
                with self.assertRaises(providers.ProviderError):
                    providers.cached_models("zen", "k", d)

    def test_unknown_provider(self):
        with self.assertRaises(providers.ProviderError):
            providers.fetch_models("nope", "k")


class TestUsageCapture(unittest.TestCase):
    def test_chat_fills_usage_out(self):
        resp = {"choices": [{"message": {"content": "a"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                          "prompt_tokens_details": {"cached_tokens": 4}}}
        with mock.patch.object(providers, "_request", return_value=resp):
            out = {}
            providers.chat("zen", "k", "m", [{"role": "user", "content": "hi"}],
                           usage_out=out)
        self.assertEqual(out["prompt_tokens"], 10)
        self.assertEqual(out["prompt_tokens_details"]["cached_tokens"], 4)

    def test_chat_without_usage_leaves_dict_empty(self):
        resp = {"choices": [{"message": {"content": "a"}}]}
        with mock.patch.object(providers, "_request", return_value=resp):
            out = {}
            providers.chat("zen", "k", "m", [{"role": "user", "content": "hi"}],
                           usage_out=out)
        self.assertEqual(out, {})

    def _run_stream(self, chunks, out):
        """Run chat_stream against fake SSE bytes; returns (payload, events)."""
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter(chunks)

        def fake_urlopen(req, timeout=None):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeResp()

        with mock.patch.object(providers.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            events = list(providers.chat_stream(
                "zen", "k", "m", [{"role": "user", "content": "hi"}],
                usage_out=out))
        return captured.get("payload") or {}, events

    def test_stream_requests_include_usage_and_captures_final_chunk(self):
        chunks = [
            b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":2292,'
            b'"completion_tokens":12}}\n\n',
            b'data: [DONE]\n\n',
        ]
        out = {}
        payload, events = self._run_stream(chunks, out)
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertEqual(events, [("content", "hi")])
        self.assertEqual(out["prompt_tokens"], 2292)

    def test_stream_captures_usage_attached_to_choice_chunk(self):
        # Go attaches usage to a chunk that still carries choices
        chunks = [
            b'data: {"choices":[{"index":0,"finish_reason":"length","delta":{}}],'
            b'"usage":{"prompt_tokens":14,"completion_tokens":12}}\n\n',
            b'data: [DONE]\n\n',
        ]
        out = {}
        _payload, events = self._run_stream(chunks, out)
        self.assertEqual(events, [])
        self.assertEqual(out["completion_tokens"], 12)


class TestMetadata(unittest.TestCase):
    FIXTURE = {
        "opencode": {"models": {
            "glm-5.3-flash": {
                "cost": {"input": 0.15, "output": 0.5, "cache_read": 0.03},
                "limit": {"context": 1000000, "output": 131072},
                "modalities": {"input": ["text", "image"]},
            },
        }},
        "opencode-go": {"models": {
            "mimo-v2.6-flash": {
                "cost": {"input": 0.14, "output": 0.28, "cache_read": 0.0028},
                "limit": {"context": 1048576},
            },
        }},
        "openai": {"models": {"gpt-5.2": {"cost": {"input": 1.75}}}},  # ignored
    }

    def test_parse_metadata(self):
        meta = providers.parse_metadata(self.FIXTURE)
        self.assertEqual(set(meta), {"zen", "go"})
        glm = meta["zen"]["glm-5.3-flash"]
        self.assertEqual(glm["cost_in"], 0.15)
        self.assertEqual(glm["cost_cache_read"], 0.03)
        self.assertEqual(glm["context"], 1000000)
        self.assertTrue(glm["image"])
        mimo = meta["go"]["mimo-v2.6-flash"]
        self.assertEqual(mimo["cost_out"], 0.28)
        self.assertIsNone(mimo["cost_cache_write"])
        self.assertFalse(mimo["image"])  # modalities absent -> no image input

    def test_parse_metadata_garbage(self):
        self.assertEqual(providers.parse_metadata("nope"), {})
        self.assertEqual(providers.parse_metadata({}), {})

    def test_fetch_then_cache_and_price_for(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(providers, "_get_json",
                                   return_value=self.FIXTURE):
                meta, from_cache = providers.models_metadata(d)
            self.assertFalse(from_cache)
            self.assertIn("glm-5.3-flash", meta["zen"])
            self.assertTrue(os.path.isfile(os.path.join(d, "models_meta.json")))

            providers._meta_memo.clear()  # forget the in-memory copy
            with mock.patch.object(providers, "_get_json",
                                   side_effect=providers.ProviderError("down")):
                meta2, from_cache2 = providers.models_metadata(d)
            self.assertTrue(from_cache2)
            self.assertEqual(meta2, meta)

            self.assertEqual(providers.price_for(d, "zen", "glm-5.3-flash")["cost_in"],
                             0.15)
            self.assertIsNone(providers.price_for(d, "zen", "nope"))

    def test_total_failure_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(providers, "_get_json",
                                   side_effect=providers.ProviderError("down")):
                meta, _ = providers.models_metadata(d, force=True)
            self.assertEqual(meta, {})


class TestUsageCost(unittest.TestCase):
    def test_cached_read_uses_cache_rate(self):
        price = {"cost_in": 0.15, "cost_out": 0.5, "cost_cache_read": 0.03}
        usage = {"prompt_tokens": 2292, "completion_tokens": 12,
                 "prompt_tokens_details": {"cached_tokens": 2048}}
        expected = (244 * 0.15 + 2048 * 0.03 + 12 * 0.5) / 1e6
        self.assertAlmostEqual(providers.usage_cost(price, usage), expected)

    def test_no_cache_rate_prices_cached_at_input(self):
        price = {"cost_in": 0.14, "cost_out": 0.28}
        usage = {"prompt_tokens": 100, "completion_tokens": 10,
                 "prompt_tokens_details": {"cached_tokens": 60}}
        self.assertAlmostEqual(providers.usage_cost(price, usage),
                               (100 * 0.14 + 10 * 0.28) / 1e6)

    def test_clamps_and_missing_data(self):
        price = {"cost_in": 1.0, "cost_out": 2.0}
        # cached larger than prompt (shouldn't happen) is clamped
        usage = {"prompt_tokens": 10, "completion_tokens": 1,
                 "prompt_tokens_details": {"cached_tokens": 99}}
        self.assertAlmostEqual(providers.usage_cost(price, usage),
                               (10 * 1.0 + 1 * 2.0) / 1e6)
        self.assertEqual(providers.usage_cost(None, {"prompt_tokens": 5}), 0.0)
        self.assertEqual(providers.usage_cost(price, None), 0.0)
        self.assertEqual(providers.usage_cost(price, {}), 0.0)


class TestMultipart(unittest.TestCase):
    def test_body_structure(self):
        import telegram as tg_mod
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
            f.write(b"\x89PNG fake bytes")
            path = f.name
        try:
            boundary, body = tg_mod._multipart(
                {"chat_id": "1", "caption": "hi"}, "photo", "pic.png", path, "image/png")
            self.assertIn(b'form-data; name="chat_id"', body)
            self.assertIn(b'form-data; name="photo"; filename="pic.png"', body)
            self.assertIn(b"\x89PNG fake bytes", body)
            self.assertTrue(body.endswith(("--%s--\r\n" % boundary).encode()))
        finally:
            os.remove(path)

    def test_size_guard(self):
        import telegram as tg_mod
        with mock.patch.object(os.path, "getsize", return_value=tg_mod.MAX_UPLOAD + 1):
            t = tg_mod.Telegram("t")
            with self.assertRaises(tg_mod.TelegramError):
                t.send_media(1, "document", __file__)


class TestTransportMapping(unittest.TestCase):
    """urlopen only wraps send-phase failures in URLError: a timeout/reset
    while READING the response body escapes as a bare OSError. Everything
    the chat callers catch must arrive as ProviderError - otherwise the
    Go->Zen fallback never fires and the turn dies with a raw exception."""

    def test_read_timeout_becomes_provider_error(self):
        with mock.patch("providers.urllib.request.urlopen",
                        side_effect=TimeoutError("The read operation timed out")):
            with self.assertRaises(providers.ProviderError) as cm:
                providers._request("https://x/v1/chat/completions", "key",
                                   {"a": 1})
        self.assertIn("timed out", str(cm.exception))

    def test_connection_reset_becomes_provider_error(self):
        with mock.patch("providers.urllib.request.urlopen",
                        side_effect=ConnectionResetError(
                            104, "Connection reset by peer")):
            with self.assertRaises(providers.ProviderError):
                providers._request("https://x/v1/chat/completions", "key",
                                   {"a": 1})

    def test_truncated_json_becomes_provider_error(self):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"choices": ['

        with mock.patch("providers.urllib.request.urlopen",
                        return_value=_Resp()):
            with self.assertRaises(providers.ProviderError) as cm:
                providers._request("https://x/v1/chat/completions", "key",
                                   {"a": 1})
        self.assertIn("bad response", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
