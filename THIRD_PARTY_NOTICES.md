# Third-party notices

Keirai is licensed under AGPLv3 (see [LICENSE](LICENSE)). The components below
are used by, or have parts adapted into, this project. Each keeps its own
license, and the required notice is reproduced here in full.

---

## opencode

- Source: <https://github.com/sst/opencode> (also published as
  <https://github.com/anomalyco/opencode>)
- License: **MIT**
- Used in: `main.py` — the context compaction strategy (`_compact`,
  `_compact_tail_start`, `_compact_keep_tokens`, `COMPACT_*`). This is a
  Python re-implementation, not a copy of their source, but the design and
  the summary template are taken from it:
  - a checkpoint is a summary **plus the newest N tokens of the real
    conversation kept verbatim**, cut on a turn boundary — not a summary of
    everything (`compaction.keep.tokens`, `DEFAULT_TAIL_TURNS`,
    `preserve_recent_tokens` clamped to 2k..15k);
  - a later compaction **updates** the earlier summary as an anchor instead of
    summarizing its own output (`SUMMARY_TEMPLATE` + "preserve still-true,
    remove stale" instruction);
  - the section layout of the summary (`## Goal`, `## Constraints &
    Preferences`, `## Progress`, `## Key Decisions`, `## Next Steps`,
    `## Critical Context`, `## Relevant Files`) and the rule to keep exact
    paths, commands, error strings and identifiers verbatim.
  Not taken: the prune pass (`PRUNE_PROTECT`), background precompute, and the
  trick of sending the conversation unchanged so the summary request reuses
  the prompt cache — keirai builds a text transcript instead.

```
MIT License

Copyright (c) 2025 opencode

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## Lightpanda

- Source: <https://github.com/lightpanda-io/browser>
- License: **AGPLv3** (same as this project)
- Used in: `browser.py` — the optional `browse` tool. Invoked as a separate
  process, never linked into keirai.

## Telegram Bot API

- Hosted service, plain HTTPS calls from our own code (no client library).
  No license implications, no notice required.

## Exa MCP

- Hosted service (`mcp.exa.ai`), REST calls only. No license implications.
