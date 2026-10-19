"""Tests for the Exa MCP client (offline, HTTP mocked)."""
import unittest
from unittest import mock

import config as config_mod
import websearch


class TestParseBody(unittest.TestCase):
    def test_plain_json(self):
        msg = websearch._parse_body('{"jsonrpc":"2.0","id":1}', "application/json")
        self.assertEqual(msg["id"], 1)

    def test_sse_framed(self):
        body = ('event: message\n'
                'data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n')
        msg = websearch._parse_body(body, "text/event-stream")
        self.assertEqual(msg["result"], {})

    def test_sse_keeps_last_message(self):
        body = ('data: {"id":1}\n'
                'data: {"id":2}\n')
        msg = websearch._parse_body(body, "text/event-stream")
        self.assertEqual(msg["id"], 2)

    def test_empty_body(self):
        self.assertIsNone(websearch._parse_body("", "application/json"))

    def test_bad_json_raises(self):
        with self.assertRaises(websearch.MCPError):
            websearch._parse_body("not json", "application/json")


class TestCallTool(unittest.TestCase):
    def test_full_handshake(self):
        seq = iter([
            ({"jsonrpc": "2.0", "id": 1, "result": {}}, "S"),
            (None, "S"),
            ({"jsonrpc": "2.0", "id": 2,
              "result": {"content": [{"type": "text", "text": "line1"},
                                     {"type": "text", "text": "line2"}]}}, "S"),
        ])
        seen = []

        def fake_post(self2, payload, session=None):
            seen.append((payload, session))
            return next(seq)

        with mock.patch.object(websearch.MCPClient, "_post", new=fake_post):
            out = websearch.call_tool("web_search", {"objective": "x"},
                                      api_key="k")
        self.assertEqual(out, "line1\nline2")
        methods = [p.get("method") for p, _ in seen]
        self.assertEqual(methods, ["initialize", "notifications/initialized",
                                   "tools/call"])
        # session header propagated to later calls
        self.assertEqual(seen[1][1], "S")
        self.assertEqual(seen[2][1], "S")

    def test_rpc_error_raises(self):
        seq = iter([
            ({"id": 1, "result": {}}, None),
            (None, None),
            ({"error": {"message": "rate limited"}}, None),
        ])
        with mock.patch.object(websearch.MCPClient, "_post",
                               new=lambda s, p, session=None: next(seq)):
            with self.assertRaises(websearch.MCPError) as cm:
                websearch.call_tool("web_search", {})
        self.assertIn("rate limited", str(cm.exception))

    def test_is_error_result_raises(self):
        seq = iter([
            ({"id": 1, "result": {}}, None),
            (None, None),
            ({"result": {"isError": True,
                         "content": [{"type": "text", "text": "boom"}]}}, None),
        ])
        with mock.patch.object(websearch.MCPClient, "_post",
                               new=lambda s, p, session=None: next(seq)):
            with self.assertRaises(websearch.MCPError) as cm:
                websearch.call_tool("web_fetch", {})
        self.assertIn("boom", str(cm.exception))


class TestToolArgs(unittest.TestCase):
    """web_search -> one web_search_exa call per query; web_fetch ->
    one batched web_fetch_exa call. Both go to the Exa endpoint, with the
    API key on the URL when one is configured."""

    def setUp(self):
        self.cfg = config_mod.Config({"exa_api_key": "pk"})

    @mock.patch.object(websearch, "call_tool", return_value="results")
    def test_search_one_call_per_query(self, call_mock):
        out = websearch.search(self.cfg, "find zebras",
                               ["zebra facts", "zebra news"])
        self.assertEqual(out, "### zebra facts\nresults\n\n"
                              "### zebra news\nresults")
        self.assertEqual(call_mock.call_count, 2)  # Exa takes ONE query
        name, args = call_mock.call_args[0][0], call_mock.call_args[0][1]
        self.assertEqual(name, "web_search_exa")
        self.assertEqual(args["objective"], "find zebras")
        self.assertEqual(args["query"], "zebra news")   # last call
        self.assertEqual(args["numResults"], websearch.RESULTS_PER_QUERY)
        self.assertIn("exaApiKey=pk", call_mock.call_args[1]["url"])

    @mock.patch.object(websearch, "call_tool", return_value="results")
    def test_search_caps_queries(self, call_mock):
        websearch.search(self.cfg, "obj", ["a", "b", "c", "d", "e"])
        self.assertEqual(call_mock.call_count, websearch.MAX_QUERIES)

    @mock.patch.object(websearch, "call_tool")
    def test_search_keeps_partial_results(self, call_mock):
        call_mock.side_effect = [websearch.MCPError("boom"), "one result"]
        out = websearch.search(self.cfg, "obj", ["q1", "q2"])
        self.assertEqual(out, "### q2\none result")   # the failure is dropped

    @mock.patch.object(websearch, "call_tool",
                       side_effect=websearch.MCPError("rate limited"))
    def test_search_raises_when_everything_fails(self, call_mock):
        with self.assertRaises(websearch.MCPError):
            websearch.search(self.cfg, "obj", ["q1", "q2"])

    @mock.patch.object(websearch, "call_tool", return_value="md")
    def test_fetch_args(self, call_mock):
        websearch.fetch(self.cfg, ["https://a.example", "https://b.example"],
                        full_content=True)
        name, args = call_mock.call_args[0][0], call_mock.call_args[0][1]
        self.assertEqual(name, "web_fetch_exa")
        self.assertEqual(args["urls"], ["https://a.example",
                                        "https://b.example"])
        self.assertEqual(args["maxCharacters"], websearch.FULL_CHARS)
        self.assertIn("exaApiKey=pk", call_mock.call_args[1]["url"])

    @mock.patch.object(websearch, "call_tool", return_value="md")
    def test_fetch_defaults_to_excerpts(self, call_mock):
        websearch.fetch(self.cfg, ["https://a.example"])
        args = call_mock.call_args[0][1]
        self.assertEqual(args["maxCharacters"], websearch.EXCERPT_CHARS)


class TestEndpoint(unittest.TestCase):
    def test_key_goes_on_the_url(self):
        cfg = config_mod.Config({"exa_api_key": "s3cret key"})
        self.assertEqual(websearch.endpoint(cfg),
                         websearch.MCP_URL + "?exaApiKey=s3cret%20key")

    def test_no_key_plain_endpoint(self):
        self.assertEqual(websearch.endpoint(config_mod.Config({})),
                         websearch.MCP_URL)


class TestQuotaGuard(unittest.TestCase):
    def test_rate_limit_text_raises(self):
        # Exa hands back a throttled search as ordinary content - it must
        # never reach the model as if it were search results
        with self.assertRaises(websearch.MCPError) as cm:
            websearch._check_quota("You've hit Exa's free MCP rate limit. "
                                   "Create your own API key.")
        self.assertIn("rate limit", str(cm.exception))

    def test_normal_results_pass(self):
        websearch._check_quota("# Title\nURL: https://example.com\nbody")


if __name__ == "__main__":
    unittest.main()
