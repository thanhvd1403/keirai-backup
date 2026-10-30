"""Exa web search/fetch via the hosted Exa MCP server (streamable HTTP).

The MCP endpoint https://mcp.exa.ai/mcp is free and anonymous (rate limited)
- it exposes exactly the two tools keirai uses: web_search_exa (one query per
call) and web_fetch_exa (batched URLs -> clean markdown). An Exa API key
(exa_api_key / EXA_API_KEY) raises the limits; it rides on the URL the server
itself hands out: https://mcp.exa.ai/mcp?exaApiKey=KEY.

Transport: MCP streamable HTTP = JSON-RPC 2.0 POSTs; the response body is
either plain JSON or text/event-stream (data: lines). Stdlib only.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

MCP_URL = "https://mcp.exa.ai/mcp"
USER_AGENT = "keirai/1.0"  # Cloudflare 1010 blocks the urllib default UA
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = 60
MAX_QUERIES = 3        # one Exa search call per query, at most this many
RESULTS_PER_QUERY = 5  # numResults for web_search_exa
EXCERPT_CHARS = 6000   # web_fetch_exa default is 3000 - keep more context
FULL_CHARS = 60000     # full_content=true: as much of the page as fits
# Exa answers a throttled search with plain text content (not isError)
QUOTA_MARK = "rate limit"
QUOTA_MAX_CHARS = 600  # a throttle notice is short; real results are long


class MCPError(Exception):
    pass


def endpoint(cfg=None):
    """MCP URL, with the Exa API key appended when one is configured."""
    key = getattr(cfg, "exa_api_key", "") if cfg is not None else ""
    if not key:
        return MCP_URL
    sep = "&" if "?" in MCP_URL else "?"
    return "%s%sexaApiKey=%s" % (MCP_URL, sep, urllib.parse.quote(key))


class MCPClient:
    def __init__(self, url=MCP_URL, api_key="", timeout=TIMEOUT):
        self.url = url
        self.api_key = api_key
        self.timeout = timeout

    def _post(self, payload, session=None):
        """One JSON-RPC POST. Returns (message_or_None, session_id)."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            # Cloudflare fronts mcp.exa.ai and answers urllib's default
            # "Python-urllib/3.x" with Error 1010 (blocked browser signature).
            # Any honest UA passes; without this every call 403s.
            "User-Agent": USER_AGENT,
        }
        if session:
            headers["Mcp-Session-Id"] = session
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ctype = resp.headers.get("Content-Type", "")
                sess = resp.headers.get("Mcp-Session-Id") or session
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise MCPError("MCP HTTP %d: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        except urllib.error.URLError as e:
            raise MCPError("MCP unreachable: %s" % e.reason)
        return _parse_body(body, ctype), sess


def _parse_body(body, ctype):
    """Response body -> last JSON-RPC message (JSON or SSE framed)."""
    if not body or not body.strip():
        return None
    if "event-stream" in (ctype or ""):
        last = None
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    last = json.loads(line[5:].strip())
                except ValueError:
                    continue
        return last
    try:
        return json.loads(body)
    except ValueError:
        raise MCPError("unparseable MCP response: %s" % body[:200])


def _text_of(result):
    parts = [b.get("text", "") for b in (result.get("content") or [])
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(parts)


def call_tool(name, arguments, api_key="", url=MCP_URL, timeout=TIMEOUT):
    """initialize -> notifications/initialized -> tools/call. Returns text."""
    client = MCPClient(url, api_key, timeout)
    _msg, session = client._post({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": {"name": "keirai", "version": "0.1"}},
    })
    client._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    out, _session = client._post({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, session)
    if out is None:
        raise MCPError("empty response for tools/call")
    if out.get("error"):
        raise MCPError(str(out["error"].get("message") or out["error"]))
    result = out.get("result") or {}
    text = _text_of(result)
    if result.get("isError"):
        raise MCPError(text or "tool call failed")
    _check_quota(text)
    return text


def _check_quota(text):
    """Exa reports a throttled search as ordinary content, not as an error -
    raise instead of handing the model a rate-limit notice as 'results'.

    Only a short message counts: real results run to thousands of chars and
    routinely *mention* rate limits (any page about APIs does), so a bare
    substring match turned ordinary answers into errors."""
    text = (text or "").strip()
    if len(text) > QUOTA_MAX_CHARS:
        return
    if QUOTA_MARK in text.lower():
        raise MCPError(text)


# ---------------------------------------------------------------- tools

def search(cfg, objective, search_queries):
    """One web_search_exa call per query (Exa takes a single natural-
    language query), results concatenated per query. Queries that fail are
    dropped; only an empty result set raises."""
    url = endpoint(cfg)
    obj = str(objective).strip()[:4000] or "the topic of the search"
    queries = [str(q).strip() for q in (search_queries or []) if str(q).strip()]
    queries = queries[:MAX_QUERIES]
    if not queries:
        raise MCPError("no search queries given")
    out, errs = [], []
    for q in queries:
        try:
            text = call_tool("web_search_exa",
                             {"query": q, "objective": obj,
                              "numResults": RESULTS_PER_QUERY},
                             url=url)
            out.append((q, text))
        except MCPError as e:
            errs.append(str(e))
    if not out:
        raise MCPError(errs[0])
    return "\n\n".join("### %s\n%s" % (q, text) for q, text in out)


def fetch(cfg, urls, full_content=False):
    """One web_fetch_exa call for all URLs (batched), clean markdown back."""
    url = endpoint(cfg)
    args = {"urls": [str(u) for u in urls],
            "maxCharacters": FULL_CHARS if full_content else EXCERPT_CHARS}
    return call_tool("web_fetch_exa", args, url=url)
