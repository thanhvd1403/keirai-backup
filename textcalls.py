"""Recovery of text-form tool calls (stdlib only).

The model occasionally writes a tool call as TEXT instead of using the tools
API. For example: '<tool_call><function=bash><parameter=command>...'
was delivered raw to chat and nothing executed. recover() parses such markup
back into providers'-shaped tool calls so main's loop can run the round.

NOTE: this source deliberately never contains the literal closing-parameter
tag as one string - a payload containing it gets truncated by the transport
parser. CLOSE is built by concatenation.
"""
import html
import re
import secrets

CLOSE = "<" + "/parameter>"
CALL_RE = re.compile(r"<function[=:](\w+)>(.*?)</function>", re.S)
PARAM_ATTR_RE = re.compile(r"<parameter[=:](\w+)>(.*?)" + CLOSE, re.S)
PARAM_NAME_RE = re.compile(r"<parameter name=[\"']([^\"']+)[\"']>(.*?)"
                           + CLOSE, re.S)


def recover(answer):
    """(calls, rest): calls matches the providers' tool_calls shape
    [{"id","name","arguments"}]; no match returns ([], answer) untouched so
    the old deliver-as-text fallback still applies to unparseable markup."""
    if not answer or "<function" not in answer:
        return [], answer
    calls, spans = [], []
    for m in CALL_RE.finditer(answer):
        body = m.group(2)
        args = {}
        for p in PARAM_ATTR_RE.finditer(body):
            args[p.group(1)] = html.unescape(p.group(2))
        for p in PARAM_NAME_RE.finditer(body):
            args.setdefault(p.group(1), html.unescape(p.group(2)))
        calls.append({"id": "call_" + secrets.token_hex(6),
                      "name": m.group(1), "arguments": args})
        spans.append(m.span())
    if not calls:
        return [], answer
    out = answer
    for a, b in reversed(spans):  # consume the matched spans, keep the rest
        out = out[:a] + out[b:]
    out = out.replace("<tool_call>", "").replace("</tool_call>", "").strip()
    return calls, out
