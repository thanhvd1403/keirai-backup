"""Markdown -> Telegram HTML conversion and message splitting.

Telegram's HTML parse mode supports a fixed tag set:
  <b> <i> <u> <s> <a href> <code> <pre> <blockquote [expandable]> <tg-spoiler>
We convert a practical markdown subset onto that tag set, and split long
messages at Telegram's 4096 char limit without breaking code fences.
"""
import html as _html
import re

TELEGRAM_LIMIT = 4096
QUOTE_OVERHEAD = len("<blockquote expandable></blockquote>")

_FENCE_RE = re.compile(r"^(```|~~~)\s*([^\s`]*)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^(\s*)[*+-] +(.*)$")  # -, * and + list markers
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_TAG_RE = re.compile(r"(<[^>]+>)")


# ---------------------------------------------------------------- blocks

def split_blocks(md):
    """Split markdown into blocks: (kind, meta, data).
    kinds: "code" (meta=lang, data=body str), "quote" (data=[lines]),
    "text" (data=[lines]), "blank"."""
    blocks = []
    lines = md.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        m = _FENCE_RE.match(line)
        if m:
            closer, lang = m.group(1), m.group(2)
            j = i + 1
            body = []
            while j < n and not lines[j].startswith(closer):
                body.append(lines[j])
                j += 1
            blocks.append(("code", lang, "\n".join(body)))
            i = j + 1  # skip closing fence (or EOF)
            continue
        if line.startswith(">"):
            qlines = []
            while i < n and lines[i].startswith(">"):
                qlines.append(lines[i].lstrip(">").strip())
                i += 1
            blocks.append(("quote", None, qlines))
            continue
        if line.strip() == "":
            blocks.append(("blank", None, None))
            i += 1
            continue
        plines = []
        while (i < n and lines[i].strip() != ""
               and not _FENCE_RE.match(lines[i]) and not lines[i].startswith(">")):
            plines.append(lines[i])
            i += 1
        blocks.append(("text", None, plines))
    return blocks


# ---------------------------------------------------------------- inline

def _inline_text(text):
    """Convert inline markdown (no code spans) in raw text to Telegram HTML."""
    t = _html.escape(text, quote=False)
    # Telegram HTML cannot embed images: ![alt](url) -> plain link
    t = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", r"[\1](\2)", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', t)
    t = re.sub(r'(?<!["\'>])\b(https?://[^\s<]+)', r'<a href="\1">\1</a>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"(?<!\w)__([^_]+?)__(?!\w)", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", t)
    t = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", t)
    t = re.sub(r"~~(.+?)~~", r"<s>\1</s>", t, flags=re.S)
    t = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", t, flags=re.S)
    return t


def _inline(seg):
    """Inline conversion with `code` spans protected from formatting."""
    out = []
    pos = 0
    for m in re.finditer(r"`([^`\n]+)`", seg):
        out.append(_inline_text(seg[pos:m.start()]))
        out.append("<code>" + _html.escape(m.group(1), quote=False) + "</code>")
        pos = m.end()
    out.append(_inline_text(seg[pos:]))
    return _balance("".join(out))


def _balance(h):
    """Fix tag nesting so Telegram never sees interleaved tags.

    Sequential regex conversion can produce <i>..<b>..</i>..</b> which
    Telegram rejects (HTTP 400 'unmatched end tag'). This closes tags in
    LIFO order and reopens them, and drops stray closers.
    """
    out = []
    stack = []
    for tok in _TAG_RE.split(h):
        if not tok:
            continue
        if tok.startswith("</"):
            name = tok[2:].rstrip(">")
            if name in stack:
                closed = []
                while stack and stack[-1] != name:
                    closed.append(stack.pop())
                    out.append("</%s>" % closed[-1])
                stack.pop()
                out.append("</%s>" % name)
                for t in reversed(closed):  # reopen what we had to close early
                    out.append("<%s>" % t)
                    stack.append(t)
            # stray closer without opener: drop
        elif tok.startswith("<"):
            stack.append(tok[1:].split()[0].rstrip(">").rstrip("/"))
            out.append(tok)
        else:
            out.append(tok)
    while stack:
        out.append("</%s>" % stack.pop())
    return "".join(out)


def _render_list_item(m):
    """List item -> bullet with indentation. Level 0 = white bullet,
    deeper levels = '-'. Indent 2 spaces per level."""
    indent, content = m.group(1), m.group(2)
    level = len(indent) // 2
    bullet = "\u2022" if level == 0 else "-"
    return "%s%s %s" % ("  " * level, bullet, _inline(content))


def _row_cells(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_table_start(lines, i):
    return (i + 1 < len(lines) and "|" in lines[i]
            and _TABLE_SEP_RE.match(lines[i + 1]) is not None)


def _read_table(lines, i):
    rows = [_row_cells(lines[i])]
    j = i + 2  # skip header + separator
    while j < len(lines) and "|" in lines[j] and lines[j].strip():
        rows.append(_row_cells(lines[j]))
        j += 1
    return rows, j


def _render_table(rows):
    """Telegram has no table markup -> render as an aligned <pre> block."""
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    widths = [max(len(r[c]) for r in rows) for c in range(ncols)]
    header = "  ".join(c.ljust(widths[k]) for k, c in enumerate(rows[0])).rstrip()
    body = ["  ".join(c.ljust(widths[k]) for k, c in enumerate(r)).rstrip()
            for r in rows[1:]]
    out = [header, "-" * len(header)] + body
    return "<pre>" + _html.escape("\n".join(out), quote=False) + "</pre>"


def _convert_lines(lines, headings=True):
    parts = []
    i = 0
    while i < len(lines):
        if "|" in lines[i] and _is_table_start(lines, i):
            rows, i = _read_table(lines, i)
            parts.append(_render_table(rows))
            continue
        m = _LIST_RE.match(lines[i])
        if m:
            parts.append(_render_list_item(m))
            i += 1
            continue
        if headings:
            m = _HEADING_RE.match(lines[i])
            if m:
                parts.append("<b>" + _inline(m.group(2)) + "</b>")
                i += 1
                continue
        parts.append(_inline(lines[i]))
        i += 1
    return "\n".join(parts)


# ---------------------------------------------------------------- convert

def convert(md):
    """Convert a complete markdown document to Telegram HTML."""
    out = []
    for kind, meta, data in split_blocks(md):
        if kind == "code":
            inner = _html.escape(data, quote=False)
            out.append(_code_tags(meta, inner))
        elif kind == "quote":
            out.append("<blockquote>" + _convert_lines(data, headings=False) + "</blockquote>")
        elif kind == "text":
            out.append(_convert_lines(data))
    return "\n".join(x for x in out if x)


# ---------------------------------------------------------------- split

def _block_source(block):
    kind, meta, data = block
    if kind == "code":
        return "```%s\n%s\n```" % (meta or "", data)
    if kind == "quote":
        return "\n".join("> " + q for q in data)
    if kind == "blank":
        return ""
    return "\n".join(data)


def convert_block(block):
    return convert(_block_source(block))


def _inline_chunk(source):
    return "\n".join(_inline(l) for l in source.split("\n"))


def _max_prefix(line, limit):
    """Largest k such that _inline(line[:k]) fits in limit."""
    lo, hi = 1, len(line)
    if len(_inline(line[:hi])) <= limit:
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(_inline(line[:mid])) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _code_tags(lang, inner):
    if lang:
        return '<pre><code class="language-%s">%s</code></pre>' % (lang, inner)
    return "<pre><code>%s</code></pre>" % inner


def _hard_split_code(lang, body, limit):
    """Split an oversized code block into fenced pieces that fit.

    Works in escaped space: each line is pre-escaped, so measured
    lengths match the final HTML exactly.
    """
    lines = [_html.escape(l, quote=False) for l in body.split("\n")]
    overhead = len(_code_tags(lang, ""))
    max_inner = max(limit - overhead, 32)
    pieces, cur, cur_len = [], [], 0
    for line in lines:
        while len(line) + 1 > max_inner:  # single monster line
            if cur:
                pieces.append(cur)
                cur, cur_len = [], 0
            pieces.append(line[:max_inner - 1])
            line = line[max_inner - 1:]
        if cur and cur_len + len(line) + 1 > max_inner:
            pieces.append(cur)
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        pieces.append(cur)
    return [_code_tags(lang, "\n".join(p)) for p in pieces]


def _hard_split_text(lines, limit):
    """Split oversized text into source chunks whose conversion fits."""
    out, cur = [], ""
    for line in lines:
        cand = (cur + "\n" + line) if cur else line
        if len(_inline_chunk(cand)) <= limit:
            cur = cand
            continue
        if cur:
            out.append(cur)
            cur = ""
        while len(_inline_chunk(line)) > limit:
            k = _max_prefix(line, limit)
            out.append(line[:k])
            line = line[k:]
        cur = line
    if cur:
        out.append(cur)
    return [_inline_chunk(c) for c in out]


def split(md, limit=TELEGRAM_LIMIT):
    """Split markdown into HTML chunks, each <= limit chars."""
    chunks = []
    cur, cur_len = [], 0
    for block in split_blocks(md):
        if block[0] == "blank":
            continue
        html_text = convert_block(block)
        need = len(html_text) + (1 if cur else 0)
        if cur_len + need <= limit:
            cur.append(html_text)
            cur_len += need
            continue
        if cur:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        if len(html_text) <= limit:
            chunks.append(html_text)
        elif block[0] == "code":
            chunks.extend(_hard_split_code(block[1], block[2], limit))
        else:
            chunks.extend(_hard_split_text(block[2], limit))
    if cur:
        chunks.append("\n".join(cur))
    return chunks if chunks else [""]


def send_parts(answer_md, thinking_md=None, limit=TELEGRAM_LIMIT):
    """Message list for an answer with optional thinking display.

    Thinking goes first, wrapped in expandable blockquotes.
    """
    parts = []
    if thinking_md and thinking_md.strip():
        for c in split(thinking_md, limit - QUOTE_OVERHEAD):
            parts.append("<blockquote expandable>%s</blockquote>" % c)
    parts.extend(split(answer_md, limit))
    return parts


# ------------------------------------------------- rich messages (Bot API 10.1+)

RICH_LIMIT = 30000  # Telegram allows 32768; keep a margin


def _rich_code_chunks(lang, body, limit):
    """Split an oversized code block into fenced source pieces."""
    fence_len = len("```%s\n\n```" % (lang or ""))
    max_inner = max(limit - fence_len, 32)
    pieces, cur, cur_len = [], [], 0
    for line in body.split("\n"):
        while len(line) + 1 > max_inner:
            if cur:
                pieces.append("\n".join(cur))
                cur, cur_len = [], 0
            pieces.append(line[:max_inner - 1])
            line = line[max_inner - 1:]
        if cur and cur_len + len(line) + 1 > max_inner:
            pieces.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        pieces.append("\n".join(cur))
    return ["```%s\n%s\n```" % (lang or "", p) for p in pieces]


# Rich Markdown pairs two "$" into an inline LaTeX span. A money range like
# "$0.1 - $0.2" therefore renders as italic math "0.1 - " with the second
# amount's sign eaten - the failure is silent and looks like a typo. GFM
# honours backslash escapes, so a bare "$" is sent as "\$".
_DOLLAR_URL_STOP = " \t\r\n<>()\"'"


def escape_rich_dollars(md):
    r"""Escape a bare `$` so Rich Markdown does not read it as LaTeX.

    Left alone: inline code and fenced blocks (already literal), bare URLs
    (an escape inside a link would change the address), an existing `\$`
    (so a second pass is a no-op), and `$$...$$` display math, which keeps
    working on purpose - a real equation can be written that way.
    """
    if "$" not in md:
        return md
    out, i, n = [], 0, len(md)
    while i < n:
        ch = md[i]
        if ch == "\\" and i + 1 < n:  # an escape of its own: keep both chars
            out.append(md[i:i + 2])
            i += 2
            continue
        if ch == "`":  # inline code span, or a whole fenced block
            j = i
            while j < n and md[j] == "`":
                j += 1
            end = md.find(md[i:j], j)
            if end < 0:  # unmatched run: copy it and carry on
                out.append(md[i:j])
                i = j
                continue
            end += j - i
            out.append(md[i:end])
            i = end
            continue
        if ch == "~" and (i == 0 or md[i - 1] == "\n"):  # ~~~ fenced block
            j = i
            while j < n and md[j] == "~":
                j += 1
            if j - i >= 3:
                k = md.find("\n" + md[i:j], j)
                if k < 0:
                    out.append(md[i:j])
                    i = j
                    continue
                end = md.find("\n", k + 1)
                end = n if end < 0 else end
                out.append(md[i:end])
                i = end
                continue
        if md.startswith("$$", i):  # display math stays math
            end = md.find("$$", i + 2)
            if end >= 0:
                out.append(md[i:end + 2])
                i = end + 2
                continue
        if md.startswith("http://", i) or md.startswith("https://", i):
            j = i
            while j < n and md[j] not in _DOLLAR_URL_STOP:
                j += 1
            out.append(md[i:j])
            i = j
            continue
        out.append("\\$" if ch == "$" else ch)
        i += 1
    return "".join(out)


def split_rich(md, limit=RICH_LIMIT):
    """Split raw markdown into source chunks for sendRichMessage (the
    server parses GFM, so we only need source-level block splitting).

    `$` is escaped first, so the limit is measured on what is actually sent
    and a chunk can never end up one backslash over.
    """
    md = escape_rich_dollars(md)
    chunks, cur, cur_len = [], [], 0
    for block in split_blocks(md):
        if block[0] == "blank":
            continue
        src = _block_source(block)
        need = len(src) + (2 if cur else 0)
        if cur_len + need <= limit:
            cur.append(src)
            cur_len += need
            continue
        if cur:
            chunks.append("\n\n".join(cur))
            cur, cur_len = [], 0
        if len(src) <= limit:
            chunks.append(src)
        elif block[0] == "code":
            chunks.extend(_rich_code_chunks(block[1], block[2], limit))
        else:
            lines = block[2] if isinstance(block[2], list) else [block[2]]
            piece, piece_len = "", 0
            for line in lines:
                cand = (piece + "\n" + line) if piece else line
                if len(cand) <= limit:
                    piece = cand
                    piece_len = len(cand)
                    continue
                if piece:
                    chunks.append(piece)
                while len(line) > limit:
                    chunks.append(line[:limit])
                    line = line[limit:]
                piece = line
                piece_len = len(line)
            if piece:
                chunks.append(piece)
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks or [md]
