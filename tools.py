"""Agent tools: files, shell, web search/fetch, browser. Stdlib only.

Tools are OpenAI function-calling specs handed to the provider; execute()
runs them locally and returns text for the model. Safety per project rules:
- bash: destructive-command blacklist + timeout (agent-settable 1..300s)
- long output: over tool_output_max_bytes (default 40 KB = 40k ASCII chars)
  the full text is saved to sessions/<sid>/tool_output/ and the model gets a
  preview + path (OpenCode style); history stores only the path
- file tools: no path sandbox (single trusted operator), size caps only
Handlers never raise: errors come back as strings so the tool loop continues.
"""
import hashlib
import os
import re
import signal
import subprocess
import time

import browser
import config as config_mod
import websearch

# No round cap (user directive): tool rounds continue until the model stops
# asking for tools. /stop interrupts a runaway turn at any round.
SPILL_BYTES = 40_000   # default: outputs over 40 KB go to a file (cfg: tool_output_max_bytes)
READ_CAP = 200_000     # read_file hard cap

# Destructive / reckless commands the model must never run.
BASH_DENY = [
    r"\brm\s+(-[a-z]+\s+)*-[a-z]*[rf][a-z]*\s+(/\s*$|/[^ ]|~|\$HOME|\.)",
    r"\bmkfs",
    r"\bdd\s+[^\n]*\bof=/dev/",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r":\s*\(\s*\)\s*\{.*\|.*&.*\};",          # fork bomb
    r">\s*/dev/sd[a-z]",
    r"\bchmod\s+-R\s+777\s+(/|~)\s*$",
    r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b",  # pipe remote to shell
    r"\bsudo\s+(rm|mkfs|dd)\b",
    r"\bformat\s+[a-z]:",
]


class ToolError(Exception):
    pass


# ---------------------------------------------------------------- specs

def _fn(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required},
    }}


def available_specs(cfg):
    """Tool specs the model may call, based on config + installed extras."""
    if not getattr(cfg, "tools_enabled", True):
        return []
    specs = [
        _fn("read_file",
            "Read a UTF-8 text file and return its contents.",
            {"path": {"type": "string", "description": "File to read"},
             "max_chars": {"type": "integer",
                           "description": "Max chars to return (default 50000, max 200000)"}},
            ["path"]),
        _fn("write_file",
            "Create or overwrite a file with the given content (creates parent directories).",
            {"path": {"type": "string", "description": "File to write"},
             "content": {"type": "string", "description": "Full file content"}},
            ["path", "content"]),
        _fn("edit_file",
            "Replace an exact string in a file. old_string must match exactly once - "
            "include surrounding context if needed. Never rewrites the whole file.",
            {"path": {"type": "string", "description": "File to edit"},
             "old_string": {"type": "string", "description": "Exact text to find (must be unique)"},
             "new_string": {"type": "string", "description": "Replacement text"}},
            ["path", "old_string", "new_string"]),
        _fn("bash",
            "Execute a shell command; returns exit code and output. Destructive commands "
            "are blocked by a safety blacklist. Very long output is written to a file - "
            "use read_file on that path to see the rest.",
            {"command": {"type": "string", "description": "Shell command to run"},
             "timeout_seconds": {"type": "integer",
                                 "description": "Optional timeout 1-300s "
                                                "(default: server config, 30s)"}},
            ["command"]),
        _fn("web_search",
            "Search the live web; returns ranked URLs with LLM-optimized excerpts. "
            "Use for current facts, news, documentation and anything after the "
            "model's training cutoff.",
            {"objective": {"type": "string",
                           "description": "Natural-language description of what to find"},
             "search_queries": {"type": "array", "items": {"type": "string"},
                                "description": "2-3 diverse keyword queries, 3-6 words each"}},
            ["objective", "search_queries"]),
        _fn("web_fetch",
            "Fetch one or more URLs and return clean markdown/excerpts "
            "(handles JS-heavy pages and PDFs). Use after web_search, or when "
            "given a URL to read.",
            {"urls": {"type": "array", "items": {"type": "string"},
                      "description": "1-20 HTTP(S) URLs"},
             "full_content": {"type": "boolean",
                              "description": "Return full page markdown (can be huge); "
                                             "default false = excerpts"}},
            ["urls"]),
    ]
    if browser.find_binary(cfg):
        specs.append(_fn(
            "browse",
            "Render a page in a headless browser (JavaScript executes) and return its "
            "markdown. Use when web_fetch is not enough (heavy JS apps, SPAs).",
            {"url": {"type": "string", "description": "URL to open"},
             "wait_ms": {"type": "integer",
                         "description": "Wait up to N ms for JS rendering (default 1500, max 15000)"}},
            ["url"]))
    return specs


# ---------------------------------------------------------------- exec

def execute(cfg, name, args, ctx=None):
    """Run a tool. Returns text for the model; never raises.

    Output over cfg.tool_output_max_bytes (default SPILL_BYTES = 40 KB) is
    written whole to sessions/<sid>/tool_output/ and returned as a head
    preview + path; on success ctx["_spill"] = (path, bytes) tells the
    caller to keep only the path in the session history."""
    args = args if isinstance(args, dict) else {}
    ctx = ctx if isinstance(ctx, dict) else {}
    handler = {"read_file": _read_file, "write_file": _write_file,
               "edit_file": _edit_file, "bash": _bash,
               "web_search": _web_search, "web_fetch": _web_fetch,
               "browse": _browse}.get(name)
    if handler is None:
        return "error: unknown tool %r" % name
    try:
        out = handler(cfg, args, ctx)
    except ToolError as e:
        return "error: %s" % e
    except Exception as e:  # tools must never crash the turn
        return "error: %s: %s" % (type(e).__name__, e)
    if out is None:
        return ""
    limit = int(getattr(cfg, "tool_output_max_bytes", 0) or SPILL_BYTES)
    if len(out.encode("utf-8", "replace")) > limit:
        return _spill(cfg, ctx, name, out, limit)
    return out


def _spill(cfg, ctx, tool, text, limit):
    """Save the full output under sessions/<sid>/tool_output/ and return a
    head preview + the path (OpenCode's truncate.ts style). Content-addressed
    file names, so identical output reuses the same file. Never raises: when
    the disk write fails we fall back to an inline truncation."""
    total = len(text.encode("utf-8", "replace"))
    shown = text[:limit]
    sid = str(ctx.get("spill_session") or ctx.get("session_id") or "misc")
    try:
        d = os.path.join(config_mod.base_dir(cfg), "sessions", sid, "tool_output")
        os.makedirs(d, exist_ok=True)
        digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]
        path = os.path.join(d, "%s-%s.txt"
                            % (re.sub(r"[^A-Za-z0-9_.-]", "_", tool), digest))
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
    except Exception as e:
        return shown + ("\n... [output truncated at %d chars: could not save "
                        "the full output: %s]" % (limit, e))
    ctx["_spill"] = (path, total)
    return (shown
            + "\n... [output truncated: %d bytes total, full output saved to %s"
              " - use read_file to read more]" % (total, path))


def preview(name, args):
    """Short single-line description of a tool call for the progress message."""
    bits = []
    for v in (args or {}).values():
        if isinstance(v, str):
            bits.append(v.replace("\n", " ")[:60])
        elif isinstance(v, (int, float, bool)):
            bits.append(str(v))
        elif isinstance(v, list):
            bits.append(" ".join(str(x) for x in v)[:60])
        if len(bits) >= 2:
            break
    return " ".join(bits)[:100]


# ---------------------------------------------------------------- files

def _read_file(cfg, args, ctx):
    path = os.path.expanduser(str(args.get("path", "")))
    if not path:
        raise ToolError("path is required")
    cap = min(int(args.get("max_chars") or 50_000), READ_CAP)
    if not os.path.isfile(path):
        raise ToolError("no such file: %s" % path)
    size = os.path.getsize(path)
    if size > READ_CAP:
        raise ToolError("file too large (%d bytes); max %d" % (size, READ_CAP))
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = f.read(cap)
    if len(data) == cap:
        data += "\n... [truncated at %d chars; raise max_chars up to %d]" % (cap, READ_CAP)
    return data


def _write_file(cfg, args, ctx):
    path = os.path.expanduser(str(args.get("path", "")))
    content = args.get("content")
    if not path or not isinstance(content, str):
        raise ToolError("path and content are required")
    if len(content) > READ_CAP:
        raise ToolError("content too large (%d chars)" % len(content))
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return "wrote %d chars to %s" % (len(content), path)


def _edit_file(cfg, args, ctx):
    path = os.path.expanduser(str(args.get("path", "")))
    old, new = args.get("old_string"), args.get("new_string")
    if not path or not isinstance(old, str) or not isinstance(new, str):
        raise ToolError("path, old_string and new_string are required")
    if not old:
        raise ToolError("old_string must not be empty")
    if not os.path.isfile(path):
        raise ToolError("no such file: %s" % path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = f.read()
    n = data.count(old)
    if n == 0:
        raise ToolError("old_string not found in %s" % path)
    if n > 1:
        raise ToolError("old_string matches %d times in %s - add more context to make "
                        "it unique" % (n, path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(data.replace(old, new, 1))
    return "edited %s (replaced 1 occurrence)" % path


# ---------------------------------------------------------------- shell

def _deny(cmd):
    for pat in BASH_DENY:
        if re.search(pat, cmd, re.IGNORECASE):
            return pat
    return None


def _kill_and_drain(proc):
    """Kill the command's whole process group and collect output without ever
    blocking forever.

    Why this exists: a command that backgrounds work (`... &`) leaves that
    process holding our stdout pipe, so EOF never arrives. Killing only the
    direct child then makes communicate() wait for an EOF that can never come. start_new_session puts every
    descendant in one group (pgid == pid), so SIGKILL to the group reaches the
    leak. If something outside the group still holds the pipe we close it
    ourselves rather than block: losing a partial tail beats a deadlock.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        out, _ = proc.communicate(timeout=5)
        return out or ""
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""          # whatever we did read is better than nothing
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        for fh in (proc.stdout, proc.stderr):
            if fh is not None and not fh.closed:
                try:
                    fh.close()
                except OSError:
                    pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return partial


def _bash(cfg, args, ctx):
    command = str(args.get("command", "")).strip()
    if not command:
        raise ToolError("command is required")
    denied = _deny(command)
    if denied:
        return ("command blocked by safety blacklist (pattern: %s). "
                "Find a non-destructive alternative." % denied)
    timeout = int(args.get("timeout_seconds") or getattr(cfg, "bash_timeout", 30))
    timeout = max(1, min(timeout, 300))
    interrupt = ctx.get("interrupt") if isinstance(ctx, dict) else None
    # start_new_session: our own process group, so a timeout can kill every
    # descendant (incl. backgrounded ones) instead of just the shell
    proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace",
                            start_new_session=True)
    deadline = time.time() + timeout
    timed_out = interrupted = False
    while True:  # poll so /stop can kill a running command mid-flight
        try:
            out, _ = proc.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if interrupt and interrupt():
                interrupted = True
                out = _kill_and_drain(proc)
                break
            if time.time() >= deadline:
                timed_out = True
                out = _kill_and_drain(proc)
                break
    out = out or ""
    if interrupted:
        header = "INTERRUPTED by the user (command did not finish; output below may be partial)\n"
    elif timed_out:
        header = "TIMED OUT after %ds (output below may be partial)\n" % timeout
    else:
        header = "exit code %d\n" % proc.returncode
    # oversized output is spilled to a file by execute() like every tool
    return header + out


# ---------------------------------------------------------------- web

def _web_search(cfg, args, ctx):
    objective = str(args.get("objective", "")).strip()
    queries = args.get("search_queries") or []
    if not objective or not isinstance(queries, list) or not queries:
        raise ToolError("objective and search_queries are required")
    return websearch.search(cfg, objective, [str(q) for q in queries])


def _web_fetch(cfg, args, ctx):
    urls = args.get("urls") or []
    if not isinstance(urls, list) or not urls:
        raise ToolError("urls is required")
    return websearch.fetch(cfg, [str(u) for u in urls],
                           full_content=bool(args.get("full_content")))


# ---------------------------------------------------------------- browser

def _browse(cfg, args, ctx):
    url = str(args.get("url", "")).strip()
    if not url:
        raise ToolError("url is required")
    wait_ms = max(0, min(int(args.get("wait_ms") or 1500), 15000))
    return browser.browse(cfg, url, wait_ms=wait_ms)
