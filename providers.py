"""OpenCode Zen / OpenCode Go providers (OpenAI-compatible chat endpoints).

- Zen:  https://opencode.ai/zen/v1
- Go:   https://opencode.ai/zen/go/v1
Both expose GET /models (ids only) and POST /chat/completions.
Model metadata (context window, pricing) comes from models.dev - OpenCode's
own catalog - fetched daily and cached on disk (see models_metadata()).
"""
import json
import os
import time
import urllib.error
import urllib.request

PROVIDER_BASES = {
    "zen": "https://opencode.ai/zen/v1",
    "go": "https://opencode.ai/zen/go/v1",
}
CACHE_TTL = 24 * 60 * 60  # refresh stats daily
CHAT_TIMEOUT = 300

# models.dev (OpenCode's own model catalog) - daily metadata sync
MODELS_DEV_URL = "https://models.dev/api.json"
META_TTL = 24 * 60 * 60
META_PROVIDER_KEYS = {"zen": "opencode", "go": "opencode-go"}
_meta_memo = {}  # cache path -> (fetched_at, meta)


class ProviderError(Exception):
    pass


# ---------------------------------------------------------------- http

def _request(url, api_key, payload=None, timeout=30, session_id=None):
    headers = {"Authorization": "Bearer %s" % api_key, "User-Agent": "keirai/0.1"}
    if session_id:
        # OpenCode Go requires a stable per-conversation session id
        # (https://opencode.ai/docs/go/#where-can-i-use-it); Zen caches on it too
        headers["x-opencode-session"] = session_id
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    else:
        data = None
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise ProviderError("HTTP %d from %s: %s" % (e.code, url, body[:300]))
    except (urllib.error.URLError, OSError) as e:
        # URLError = connect/send failures; raw OSError = timeouts/resets
        # WHILE reading the body (urllib does not wrap those). Everything
        # the caller handles must arrive as ProviderError, or the Go->Zen
        # fallback never fires and the turn dies with a raw exception.
        reason = getattr(e, "reason", None) or e
        raise ProviderError("%s: %s" % (url, reason))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ProviderError("%s: bad response (%s)" % (url, e))


def _get_json(url, timeout=60):
    """Anonymous GET -> parsed JSON. Raises ProviderError on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "keirai/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise ProviderError("HTTP %d from %s: %s" % (e.code, url, body[:200]))
    except urllib.error.URLError as e:
        raise ProviderError("%s: %s" % (url, e.reason))


# ---------------------------------------------------------------- models

def fetch_models(name, api_key):
    """GET {base}/models, normalized. Raises ProviderError on failure."""
    base = PROVIDER_BASES.get(name)
    if not base:
        raise ProviderError("unknown provider: %s" % name)
    data = _request(base + "/models", api_key)
    return normalize_models(data)


def normalize_models(data):
    """Best-effort normalization of the /models response.

    Accepts {"data": [...]}, {"models": [...]}, or {id: {...}} dicts.
    Returns [{id, name, context, max_output, cost_in, cost_out}].
    """
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
        if not items and not any(k in data for k in ("data", "models")):
            items = [{"id": k, **(v if isinstance(v, dict) else {})} for k, v in data.items()]
    elif isinstance(data, list):
        items = data
    else:
        items = []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = item.get("id") or item.get("model") or item.get("slug")
        if not mid:
            continue
        limit = item.get("limit") or {}
        cost = item.get("cost") or item.get("pricing") or item.get("price") or {}
        out.append({
            "id": mid,
            "name": item.get("name") or item.get("display_name") or mid,
            "context": _first_int(item, limit, ("context_window", "context_length", "context", "max_context")),
            "max_output": _first_int(item, limit, ("max_output", "output", "max_tokens")),
            "cost_in": _first_num(item, cost, ("input", "in", "prompt", "input_cost")),
            "cost_out": _first_num(item, cost, ("output", "out", "completion", "output_cost")),
        })
    out.sort(key=lambda m: m["id"])
    return out


def _first_int(item, nested, keys):
    for k in keys:
        v = item.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    for k in keys:
        v = nested.get(k) if isinstance(nested, dict) else None
        if isinstance(v, (int, float)):
            return int(v)
    return None


def _first_num(item, nested, keys):
    for k in keys:
        v = item.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    for k in keys:
        v = nested.get(k) if isinstance(nested, dict) else None
        if isinstance(v, (int, float)):
            return float(v)
    return None


# ---------------------------------------------------------------- cache

def cached_models(name, api_key, cache_dir, force_refresh=False):
    """Return (models, from_cache). Falls back to cache when the API is unreachable."""
    path = os.path.join(cache_dir, "models_%s.json" % name)
    if not force_refresh and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
            if time.time() - blob.get("fetched_at", 0) < CACHE_TTL:
                return blob.get("models", []), True
        except (ValueError, OSError):
            pass
    try:
        models = fetch_models(name, api_key)
    except ProviderError:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
            return blob.get("models", []), True
        raise
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "models": models}, f)
    except OSError:
        pass
    return models, False


# -------------------------------------------------- models.dev metadata
# The live /models endpoint returns ids only, so context/pricing/capability
# data comes from models.dev (OpenCode's own catalog, used by opencode itself).

def parse_metadata(blob):
    """models.dev api.json -> {provider: {model_id: stats}} for OUR providers.

    stats: {context, max_output, cost_in, cost_out, cost_cache_read,
    cost_cache_write, image} - costs are USD per 1M tokens, None when absent.
    """
    out = {}
    if not isinstance(blob, dict):
        return out
    for ours, theirs in META_PROVIDER_KEYS.items():
        entry = blob.get(theirs)
        if not isinstance(entry, dict):
            continue
        rows = {}
        for mid, m in (entry.get("models") or {}).items():
            if not isinstance(m, dict):
                continue
            cost = m.get("cost") if isinstance(m.get("cost"), dict) else {}
            limit = m.get("limit") if isinstance(m.get("limit"), dict) else {}
            modal = m.get("modalities") if isinstance(m.get("modalities"), dict) else {}
            rows[mid] = {
                "context": limit.get("context"),
                "max_output": limit.get("output"),
                "cost_in": cost.get("input"),
                "cost_out": cost.get("output"),
                "cost_cache_read": cost.get("cache_read"),
                "cost_cache_write": cost.get("cache_write"),
                "image": "image" in (modal.get("input") or []),
            }
        out[ours] = rows
    return out


def models_metadata(cache_dir, force=False):
    """(meta, from_cache) - daily models.dev sync into a compact derived cache.

    One ~4.9 MB fetch per day, reduced to just our two providers (a few KB)
    and written to cache/models_meta.json; returns ({}, ...) on total failure
    so callers degrade instead of raising.
    """
    path = os.path.join(cache_dir, "models_meta.json")
    now = time.time()

    def _read_cache():
        try:
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
            return blob.get("fetched_at", 0), blob.get("meta") or {}
        except (ValueError, OSError):
            return 0, {}

    memo = _meta_memo.get(path)
    if memo and not force and now - memo[0] < META_TTL:
        return memo[1], True
    if not force and os.path.isfile(path):
        fetched_at, meta = _read_cache()
        if now - fetched_at < META_TTL:
            _meta_memo[path] = (fetched_at, meta)
            return meta, True
    try:
        meta = parse_metadata(_get_json(MODELS_DEV_URL))
    except ProviderError:
        fetched_at, meta = _read_cache()  # stale beats nothing
        if meta:
            _meta_memo[path] = (fetched_at, meta)
        return meta, True
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": now, "meta": meta}, f)
    except OSError:
        pass
    _meta_memo[path] = (now, meta)
    return meta, False


def price_for(cache_dir, provider, model_id):
    """Per-1M pricing stats for one model, or None when unknown."""
    meta, _ = models_metadata(cache_dir)
    return (meta.get(provider) or {}).get(model_id)


def usage_cost(price, usage):
    """Notional USD for one call's `usage` at models.dev per-1M rates.

    Cached reads use the cheaper cache_read rate when available; cache-write
    tokens are not reported by the API and stay inside prompt_tokens at the
    input rate. Returns 0.0 when pricing or usage is missing.
    """
    if not price or not isinstance(usage, dict):
        return 0.0
    tin = int(usage.get("prompt_tokens") or 0)
    tout = int(usage.get("completion_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    cached = max(0, min(cached, tin))
    rate_in = float(price.get("cost_in") or 0.0)
    rate_cached = float(price.get("cost_cache_read") or price.get("cost_in") or 0.0)
    rate_out = float(price.get("cost_out") or 0.0)
    return ((tin - cached) * rate_in + cached * rate_cached + tout * rate_out) / 1_000_000.0


# ---------------------------------------------------------------- chat

def chat(name, api_key, model, messages, extra=None, session_id=None, tools=None,
         usage_out=None):
    """One chat completion. Returns (content, reasoning, tool_calls).

    tool_calls: [{"id", "name", "arguments"(dict)}] or None.
    session_id: stable id sent as x-opencode-session (Go requires it; Zen
    pins its cache-affine instance with it).
    usage_out: optional dict - filled with the response `usage` (token counts)
    for cost accounting.
    """
    base = PROVIDER_BASES.get(name)
    if not base:
        raise ProviderError("unknown provider: %s" % name)
    payload = {"model": model, "messages": messages}
    if tools:
        payload["tools"] = tools
    if extra:
        payload.update(extra)
    resp = _request(base + "/chat/completions", api_key, payload=payload,
                    timeout=CHAT_TIMEOUT, session_id=session_id)
    if usage_out is not None and isinstance(resp.get("usage"), dict):
        usage_out.update(resp["usage"])
    try:
        msg = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise ProviderError("unexpected chat response: %s" % json.dumps(resp)[:300])
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    if isinstance(reasoning, dict):
        reasoning = reasoning.get("content") or ""
    return content, reasoning, parse_tool_calls(msg.get("tool_calls"))


def parse_tool_calls(raw):
    """OpenAI tool_calls -> [{"id","name","arguments"(dict)}] or None."""
    if not raw or not isinstance(raw, list):
        return None
    out = []
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                args = {"_raw": args}
        elif not isinstance(args, dict):
            args = {}
        out.append({"id": tc.get("id") or "", "name": fn.get("name") or "",
                    "arguments": args})
    return out or None


# Session-title call: a single constrained field, with headroom for a fenced
# or reasoning reply - a model that honours the contract stops after ~15
# tokens, so the ceiling only costs on replies that were garbage anyway.
TITLE_MAX_TOKENS = 512
TITLE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "session_title",
        "strict": True,
        "schema": {"type": "object",
                   "properties": {"title": {"type": "string"}},
                   "required": ["title"],
                   "additionalProperties": False},
    },
}


def chat_title(name, api_key, model, messages, session_id=None, usage_out=None):
    """Session-title completion: JSON-constrained output. Providers that
    ignore response_format degrade to plain text - the caller extracts and
    validates either way (strict JSON -> loose scan -> prose first line)."""
    return chat(name, api_key, model, messages, session_id=session_id,
                usage_out=usage_out,
                extra={"response_format": TITLE_RESPONSE_FORMAT,
                       "max_tokens": TITLE_MAX_TOKENS})


def chat_stream(name, api_key, model, messages, session_id=None, tools=None,
                usage_out=None):
    """Streaming chat completion.

    Yields ("reasoning"|"content", delta_text) while generating and, when the
    model called tools, a final ("tool_calls", [...]) before stopping.
    OpenAI-compatible SSE (stream: true). Raises ProviderError on failure.
    usage_out (optional dict) receives the final `usage` token counts - sent
    with stream_options.include_usage and captured from any chunk carrying it.
    """
    base = PROVIDER_BASES.get(name)
    if not base:
        raise ProviderError("unknown provider: %s" % name)
    payload = {"model": model, "messages": messages, "stream": True,
               "stream_options": {"include_usage": True}}
    if tools:
        payload["tools"] = tools
    headers = {"Authorization": "Bearer %s" % api_key, "User-Agent": "keirai/0.1",
               "Content-Type": "application/json", "Accept": "text/event-stream"}
    if session_id:
        headers["x-opencode-session"] = session_id
    req = urllib.request.Request(base + "/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=CHAT_TIMEOUT)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise ProviderError("HTTP %d from %s: %s" % (e.code, base, body[:300]))
    except urllib.error.URLError as e:
        raise ProviderError("%s: %s" % (base, e.reason))
    tool_acc = {}  # index -> {"id","name","args"}
    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            u = obj.get("usage")
            if usage_out is not None and isinstance(u, dict):
                usage_out.update(u)
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, dict):
                reasoning = reasoning.get("content")
            if reasoning:
                yield "reasoning", reasoning
            content = delta.get("content")
            if content:
                yield "content", content
            for i, tc in enumerate(delta.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                slot = tool_acc.setdefault(tc.get("index", i), {"id": "", "name": "",
                                                                "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
    if tool_acc:
        calls = []
        for key in sorted(tool_acc):
            slot = tool_acc[key]
            if not slot["name"]:
                continue
            args = slot["args"]
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                args = {"_raw": args}
            if not isinstance(args, dict):
                args = {"_raw": str(args)}
            calls.append({"id": slot["id"], "name": slot["name"], "arguments": args})
        if calls:
            yield "tool_calls", calls


def image_part(mime, data_b64):
    return {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, data_b64)}}
