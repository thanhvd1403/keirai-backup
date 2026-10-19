"""Recovery of text-form tool calls (stdlib only).

Two dialects reach recover():

1. The plain-text form, `<function=bash><parameter=command>...`
   (also `<function:read_file><parameter name="path">...`) - the model wrote the
   call as TEXT instead of using the tools API.

2. DeepSeek DSML. DeepSeek V3.2/V4/V4.1 write tool calls in their own XML
   envelope; OpenAI-compatible hosts leak it into `content` instead of
   returning structured tool_calls (upstream vLLM DeepSeekDsmlParser and the
   DeepSeek-V4.1-Flash encoding README). V4.1 puts a SPACE inside every tag name:

       <｜DSML｜ calls>
       <｜DSML｜ invoke name="bash">
       <｜DSML｜ parameter name="command" string="true">pwd</｜DSML｜ parameter>
       </｜DSML｜ invoke>
       </｜DSML｜ calls>

   The dialects share one grammar and differ only in the block name: V3.2
   `function_calls`, V4 `tool_calls`, V4.1 ` calls`/` invoke`/` parameter`.
   An ASCII-pipe twin (`<|DSML|invoke ...>`) occurs on the wire too; TOK
   matches the tag-name separator, so all spellings share one regex.

   `string="true"` keeps the value as a raw string; `string="false"` parses
   it as JSON; a missing attribute behaves like `false` (try JSON, fall back
   to the text) - the upstream reference rule.

NOTE: this source deliberately never contains the literal closing-parameter
tag as one string - a payload containing it gets truncated by the transport
parser. CLOSE is built by concatenation, and every
DSML closing tag is assembled the same way.
"""
import html
import json
import re
import secrets

CLOSE = "<" + "/parameter>"

DSML = "｜DSML｜"          # fullwidth-pipe sentinel (V3.2, V4, V4.1)
DSML_ASCII = "|DSML|"      # ASCII-pipe twin seen on the wire
EOS = "<｜end▁of▁sentence｜>"   # DeepSeek end-of-sentence marker
# tag-name separator: V4.1 puts a space inside every tag, V4/V3.2 do not
TOK = r"[ ｜|]+"

CALL_RE = re.compile(r"<function[=:](\w+)>(.*?)</function>", re.S)
PARAM_ATTR_RE = re.compile(r"<parameter[=:](\w+)>(.*?)" + CLOSE, re.S)
PARAM_NAME_RE = re.compile(r"<parameter name=[\"']([^\"']+)[\"']>(.*?)"
                           + CLOSE, re.S)

# block wrapper: <｜DSML｜calls> ... </｜DSML｜calls> (backref pins the name)
DSML_BLOCK_RE = re.compile(
    r"<" + TOK + r"DSML" + TOK + r"(tool_calls|function_calls|calls)>"
    + r"(.*?)</" + TOK + r"DSML" + TOK + r"\1>", re.S)

# one whole invoke: name + body + its own closing tag
DSML_INVOKE_RE = re.compile(
    r"<" + TOK + r"DSML" + TOK + r"invoke\s+name\s*=\s*['\"]?([\w.:-]+)['\"]?"
    + r"(?:\s+[\w-]+\s*=\s*['\"][^'\"]*['\"])*\s*>"
    + r"(.*?)</" + TOK + r"DSML" + TOK + r"invoke\s*>", re.S)

# one parameter inside an invoke body: name, optional string=, value
DSML_PARAM_RE = re.compile(
    r"<" + TOK + r"DSML" + TOK + r"parameter\s+name\s*=\s*['\"]([^'\"]+)['\"]"
    + r"(?:\s+string\s*=\s*['\"]([^'\"]*)['\"])?"
    + r"(?:\s+[\w-]+\s*=\s*['\"][^'\"]*['\"])*\s*>"
    + r"(.*?)</" + TOK + r"DSML" + TOK + r"parameter\s*>", re.S)


# a call opener of any dialect, tolerant about tag spelling - shared by
# looks_like (the gate) and has_markup (the damage check) so both accept the
# spellings the parser itself accepts, without duplicating its grammar
MARKUP_RE = re.compile(
    r"<\s*[ ｜|]*(?:function|invoke|antml\s*:\s*invoke|DSML)", re.I)
# ANY DSML tag: a damaged opener the grammar cannot match still proves the
# model leaked its tool format into the visible channel
DSML_TAG_RE = re.compile(r"</?\s*[ ｜|]*DSML", re.I)
# a bare JSON object filling a whole invoke body (no parameter tags at all)
DIRECT_JSON_RE = re.compile(r"^\s*(\{.*\})\s*$", re.S)
# any DSML invoke/parameter tag, opener or closer - for fragments whose
# partner tag is gone or was never written
FRAGMENT_RE = re.compile(
    r"</?\s*[ ｜|]*DSML\s*[ ｜|]?\s*(?:invoke|parameter)\b[^>]*>", re.I)


def looks_like(text):
    """True when `text` could carry tool-call markup of any known dialect -
    the recover() gate and main's deliver-as-text warning share it, so a
    dialect the parser does not yet handle still never reaches chat raw.

    The check is deliberately looser than the parser: the parser is tolerant
    about tag spelling, so a literal substring gate rejected input the parser
    could have handled - the double-bar form (｜｜DSML｜｜, the most common in
    live V4 SSE) and the spaced ASCII form (< | DSML |>) both died here and
    never reached the grammar."""
    if not text or not isinstance(text, str):
        return False
    return ("<function" in text or "<invoke" in text
            or MARKUP_RE.search(text) is not None)


def has_markup(text):
    """True when `text` still CONTAINS tool-call markup - including damaged
    markup recover() could not turn into a call (a missing opener, a mangled
    tag, a bare tag with no wrapper). main uses it to decide that a reply
    producing no call still needs a correction, and to keep that markup out
    of the stored history, where the model would copy it back."""
    if not isinstance(text, str) or not text:
        return False
    return MARKUP_RE.search(text) is not None \
        or DSML_TAG_RE.search(text) is not None


def _new_id():
    return "call_" + secrets.token_hex(6)


def _decode(value, is_str):
    """DSML `string` attribute: true -> raw text, false/absent -> JSON,
    falling back to the text when it is not valid JSON (a path like
    /etc/hosts must never turn into a parse failure)."""
    if is_str == "true":
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def _mk_call(name, body):
    """One invoke -> a providers-shaped call. Arguments come from the
    parameter tags; when there are none the body may be a bare JSON object
    instead ("Format 2" in the vLLM/SGLang detectors). Falling through to {}
    there was worse than a leak: it produced a VALID call with empty
    arguments, so a real tool ran with nothing and failed confusingly."""
    args = {}
    for p in DSML_PARAM_RE.finditer(body):
        args[p.group(1)] = _decode(p.group(3), p.group(2))
    if not args:
        m = DIRECT_JSON_RE.match(body)
        if m:
            args = _decode(m.group(1), "false")
            if not isinstance(args, dict):
                args = {}   # not an argument map - never invent one
    return {"id": _new_id(), "name": name, "arguments": args}


def _strip_spans(answer, spans):
    """Cut every span out once. Overlaps are merged FIRST (a block and the
    invoke inside it are the same markup), then the cut runs RIGHT to LEFT:
    each span's offsets still belong to the original string only until a
    span to its right has been removed - left to right would shift the text
    under the next span and corrupt whatever follows it."""
    merged = []
    for a, b in sorted(set(spans)):
        if merged and a < merged[-1][1]:      # overlaps its predecessor
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = answer
    for a, b in reversed(merged):
        out = out[:a] + out[b:]
    return out


def _dsml_calls(answer):
    """(found, spans): every DSML invoke as (start, call), plus the markup
    to cut.

    Blocks are preferred as the removal unit (one cut takes the wrapper and
    everything in it); bare invokes are also collected so V4.1's wrapper-less
    form works. Spans are non-overlapping: an invoke reached through its
    block is not recorded a second time."""
    blocks = list(DSML_BLOCK_RE.finditer(answer))
    covered = [b.span() for b in blocks]
    invs = [im for im in DSML_INVOKE_RE.finditer(answer)
            if not any(s <= im.start() < e for s, e in covered)]

    found = []  # (start, call) in document order
    for b in blocks:
        base = b.start(2)  # group 2 is inner content -> absolute positions
        for im in DSML_INVOKE_RE.finditer(b.group(2)):
            found.append((base + im.start(), _mk_call(im.group(1),
                                                      im.group(2))))
    for im in invs:
        found.append((im.start(), _mk_call(im.group(1), im.group(2))))
    found.sort(key=lambda t: t[0])
    # caller merges `found` with the legacy form and derives the calls from
    # it, so the two halves never have to agree on ordering by index
    return found, covered + [im.span() for im in invs]


def recover(answer):
    """(calls, rest): calls matches the providers' tool_calls shape
    [{"id","name","arguments"}]; no match returns ([], answer) untouched so
    the old deliver-as-text fallback still applies to unparseable markup.

    Every parsed call is returned, including ones naming a tool the request
    never offered. Markup is parsed from TEXT, and text can quote a tool
    format without meaning to call anything - but deciding what to do about
    that is the CALLER's job: it alone knows the tool list, and it is also
    the only place that can answer the model with a tool result."""
    if not looks_like(answer):
        return [], answer
    found = []  # (start, call) - both dialects, resolved by document order
    spans = []  # markup to remove from `rest`

    for m in CALL_RE.finditer(answer):   # legacy <function=...> form
        body = m.group(2)
        args = {}
        for p in PARAM_ATTR_RE.finditer(body):
            args[p.group(1)] = html.unescape(p.group(2))
        for p in PARAM_NAME_RE.finditer(body):
            args.setdefault(p.group(1), html.unescape(p.group(2)))
        found.append((m.start(), {"id": _new_id(), "name": m.group(1),
                                  "arguments": args}))
        spans.append(m.span())

    dsml_found, dsml_spans = _dsml_calls(answer)  # DeepSeek DSML
    found += dsml_found
    spans += dsml_spans

    if not found and not spans:
        return [], answer

    found.sort(key=lambda t: t[0])
    out = _strip_spans(answer, spans)
    out = out.replace("<tool_call>", "").replace("</tool_call>", "")
    out = out.replace(EOS, "").strip()
    if not found:      # DSML wrapper present but nothing parsed: no raw XML
        return [], out
    return [c for _s, c in found], out


def sanitize(text):
    """Remove tool-call markup from text that produced NO call.

    Used for the reply about to be stored in the session history. Markup
    already turned into a call is not here - recover() consumed it. What
    remains is damage: wrappers and invokes the grammar could not match, and
    fragments whose partner tag is missing. That must not be persisted,
    because the model copies its own degraded output forward - one leak
    poisons every turn after it, which is the failure mode every harness
    handling DSML reports.

    Untouched when there is no markup at all, so ordinary prose comes back
    byte-identical."""
    if not looks_like(text):
        return text
    spans = [m.span() for m in DSML_BLOCK_RE.finditer(text)]
    covered = list(spans)
    spans += [m.span() for m in DSML_INVOKE_RE.finditer(text)
              if not any(s <= m.start() < e for s, e in covered)]
    out = _strip_spans(text, spans)
    out = FRAGMENT_RE.sub("", out)
    return out.replace(EOS, "").strip()
