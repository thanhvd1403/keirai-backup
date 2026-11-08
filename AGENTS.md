# Keirai - Lightweight AI Agent

## Goal

Build an extremely lightweight AI agent that runs on a low-resource server (1GB RAM). The agent performs tasks on the server and interacts with users via Telegram Bot API.

## Core Rules

1. **Keep it lightweight** - Every dependency, every feature must justify its memory/CPU cost. If it can be done without a library, do it without a library.
2. **Keep it expandable** - The architecture must allow adding new tools and capabilities without major refactoring.
3. **No unnecessary abstractions** - Use the simplest solution that works.
4. **Prefer built-in modules** - Only add external dependencies when the built-in alternative is truly insufficient.
5. **Ask when unsure** - Ask the user for clarification on everything that is ambiguous or needs a decision; don't guess.

## Tech Stack

- **Language**: Python 3.11+ (tomllib is used from the stdlib for config parsing)
- **Telegram**: raw HTTP calls with stdlib only (python-telegram-bot/aiogram evaluated; both pull heavy asyncio HTTP stacks for little gain - the Bot API is plain JSON over HTTPS)
- **AI Backend**: OpenCode Zen and OpenCode Go via their OpenAI-compatible endpoints (`https://opencode.ai/zen/v1`, `https://opencode.ai/zen/go/v1`)
- **Config**: TOML (`config.toml`, comments allowed) - chosen over JSON (no comments) and YAML (would need PyYAML)
- **Dependencies**: none - everything runs on the Python standard library

## Project Structure

```
keirai/
├── AGENTS.md            # This file - project goals and rules
├── TODO.md              # Planned features
├── LICENSE              # AGPLv3
├── .gitignore           # Ignores config.toml (secrets), sessions.db, sessions/, tools/, cache/, logs/, __pycache__
├── main.py              # Entry point: polling loop, commands, routing, streaming, tool loop, /stop, topic flow, /session, titles
├── config.py            # Config loading (TOML + env overrides)
├── telegram.py          # Telegram Bot API client (raw HTTP, multipart, rich messages, live edits)
├── md2tg.py             # Markdown -> Telegram HTML, message splitting
├── providers.py         # OpenCode Zen/Go clients (blocking + streaming), usage capture, models.dev metadata + pricing cache
├── sessions.py          # SQLite session store (history, models, topics, session meta: ids + cost/token accumulators)
├── tools.py             # LLM tool registry: files, shell (blacklist+timeout), dispatch
├── websearch.py         # Exa MCP client (web_search_exa / web_fetch_exa over streamable HTTP, stdlib)
├── browser.py           # Lightpanda wrapper (optional browse tool, subprocess on demand)
├── cli.py               # Session management CLI (list/delete/rename, no Telegram needed)
├── deploy/keirai.service# systemd unit (keep-alive + autostart + ExecStopPost death notice)
├── deploy/notify_stop.py # ExecStopPost: reports OOM/kill deaths in the chat (stdlib-only, no repo imports, rate-limited)
├── deploy/notify_start.py # ExecStartPost: waits for logs/keirai.ready (written by main.py after its first getUpdates) then says so; detaches
├── deploy/install_lightpanda.sh # installs the browser binary into ./tools/
├── SYSTEM.md            # System prompt - LOCAL, gitignored, re-read on every request (falls back to DEFAULT_SYSTEM_PROMPT)
├── config.example.toml  # Config template (TOML, comments allowed)
└── tests/               # Unit tests (unittest, stdlib)
    ├── test_md2tg.py    #   markdown conversion, splitting, rich split, tables, lists
    ├── test_config.py   #   config loading, access control
    ├── test_providers.py#   provider parsing, usage capture, models.dev metadata, cost math, cache, session header
    ├── test_tools.py     #   tool specs, file ops, shell safety/interrupt
    ├── test_websearch.py #   Exa MCP client: parsing, handshake, search/fetch args, quota guard
    ├── test_browser.py   #   lightpanda discovery + subprocess wrapper
    ├── test_telegram.py  #   Bot API wire format: method names, payloads, allowed_updates
    └── test_main.py      #   routing, commands, sessions, streaming, compaction, CLI,
                          #   tool loop, /stop, reply-to, edit-mode streaming,
                          #   /context, /cost, provider fallback, usage accounting
```

Run tests: `python -m unittest discover -s tests`

## Deploying / restarting

- `deploy/keirai.service` is a **template**: `User=youruser` and the three `/home/youruser` paths must be edited before installing. Never copy it unedited into `/etc/systemd/system/` - that makes every start fail with `217/USER` (placeholder user does not resolve) while the *already-running* process keeps working, so the damage stays invisible until the next restart, when the bot won't come back up. This is easy to do by accident.
- **`diff deploy/keirai.service /etc/systemd/system/keirai.service` before any copy.** Keep real account names and home paths out of commits - the repo must stay portable. If `/etc` was hand-edited in an emergency, copy it back here so the repo stays true.
- `deploy/notify_start.py` (via `ExecStartPost`) announces a successful start, but only after it can confirm one: `main.py` touches `logs/keirai.ready` after its **first successful `getUpdates`** (made with `timeout=0`: a long poll returns only when a message arrives or `POLL_TIMEOUT`=25s expires, so gating readiness on it made "up" as slow as the idle wait - measured 19.3s), and the helper waits for that file to be written *since this start* (unit active alone proves nothing, `getMe` only proves the token works - both were measured firing 0.4s after spawn, i.e. before the harness existed). `ExecStartPost` itself fires before the bot can reply, so the helper polls, then sends and double-forks (systemd does not wait, and the helper is not killed by the stop that follows a restart). Silent when readiness cannot be confirmed; 60s marker so a crash loop announces once; always exits 0.
- `/restart` is debounced: a second one within `RESTART_DEBOUNCE`=30s is refused ("a restart is already in progress") - overlapping restarts queue one transient unit each, and each boot kills the one before it, so none of them ever becomes ready and the user hears nothing. It also clears the start-notify marker so the deliberate restart always announces itself (the 60s cooldown is for crash loops).
- `/restart` (main chat only) first imports the tree in a throwaway process (`_code_imports`: ~120ms, ~24MB, once per command) and refuses to restart if it does not import - a restart into broken code leaves no bot to send `/restart` again, and `Restart=always` + `StartLimitBurst=0` means it loops every 3s until SSH. It then hands `systemctl restart` to a transient `systemd-run --on-active=1` unit: run from inside the service, a plain `systemctl restart` tears down the cgroup that holds the command asking for it. Requires `sudo -n systemd-run` (NOPASSWD). Session history is in sqlite, so the conversation survives the restart.
- `deploy/notify_stop.py` (via `ExecStopPost`) reports unexpected stops in the chat: silent on `success`, 30-min rate limit, always exits 0. Known gap: it inherits `User=`, so a failure *at the USER step* (bad path, unknown account) kills the notifier too - it can't report the very failure that stopped it. systemd 239's `+` prefix does not help (verified empirically); `OnFailure=` pointed at a root-run alert unit does (also verified).
- `deploy/idle_restart.sh` derives the repo root from its own location, so it hardcodes no absolute paths. It restarts only after 5 min of log quiet, because a restart kills the in-flight turn - including this agent's own reply. `sudo systemctl restart keirai` is the manual equivalent.
- **Memory**: the host is small, so keep a swapfile (also listed in
  `/etc/fstab`) and treat a compiler/large build as the heaviest thing
  that can run on it. The bash tool **reports processes left running
  after a call returns** (`_survivors`) - backgrounded work survives its
  tool call by design, so an unnoticed `make -j2` can otherwise keep
  burning memory across later rounds.

## Current Status

P0 + P1 + P2 + P3 + P4 complete and tested (293 tests). The bot does:
- Long polling with access control (allow-list by user ID/username, or allow all)
- Config in TOML (`config.toml`, comments allowed) with env overrides
- System prompt lives in `SYSTEM.md` (gitignored - it is the local prompt; fallback `DEFAULT_SYSTEM_PROMPT` in config.py, which is also what a fresh clone gets) and is **re-read on every request**, so an edit reaches the very next user message with no restart; every request also carries a `[time]` block - current time plus when this session's agent last worked (last reply, or last tool call of a run the user interrupted) - so the model can tell a quiet minute from a quiet day
- AI chat via OpenCode Zen / OpenCode Go (OpenAI-compatible), history persisted per session in SQLite
- Sessions via Telegram topics (Bot API 9.3+ private-chat topics - enable "Threaded mode" in the @BotFather Mini App): `/new [name]`, `/rename <name>`, per-topic context isolation; `/reset-all` wipes everything (2-string confirmation, deletes tracked topics)
- AI answers sent as Bot API 10.1 Rich Messages: markdown passthrough (native tables, task lists, headings, formulas, 32k chars), thinking as collapsible `<details>`; `rich_messages` config toggle + automatic fallback to regular messages
- Live streaming: the **thinking block streams by editing ONE message in place** (created on the first reasoning delta, edited until the round ends - reasoning that arrives between answer chunks keeps editing the same block, and a tool round turns it into the permanent block instead of sending a second one); the **answer streams through `sendRichMessageDraft`** (Bot API 10.1: animated preview in the chat, native Stop button -> `stopped_message_generation` -> same interrupt as /stop) in private chats, and through in-place edits of a message of its own below the thinking where drafts are not allowed (groups) or with `stream_mode = "edit"`; `stream_drafts` on/off, automatic fallback to a blocking call on any error. The draft is ephemeral, so the finished answer is persisted with `sendRichMessage` at the end. It is `sendRichMessageDraft`, never `sendMessageDraft` - the latter lands in the *user's* composer and blocks them from typing/sending (bugs.telegram.org/c/62189)
- Session persistence in SQLite (`sessions.db`): history, per-session model, topic registry - survives restarts; `/delete` clears one session's context
- Context window management: `/context` shows used/max against the model's **real context window** (models.dev tokens) plus the message count; auto-compact fires at **100% of that window** by default (`context_limit_tokens` in config.toml overrides); `/compact` manual (summarize older messages, keep last 4)
- **No message cap**: the whole session history is sent on every call (`_prompt()`) - auto-compact alone bounds size (send everything, summarize when it gets big); the one exception is base64 images: only the newest `IMAGE_KEEP` (4) image-bearing messages re-send theirs, older pictures become a marker in the *prompt* (stored history keeps them). Compaction is hardened to be that sole bound: the summary transcript is capped at ~1/8 of the window (newest older messages first), and if summarizing still fails while the window is genuinely full, the oldest are dropped to ~half the window (`COMPACT_FALLBACK_KEEP` = 6 floor) - a last resort, never normal operation
- CLI: `python cli.py list|delete|rename` for session admin without Telegram
- Agent tools (OpenAI function-calling, `tools.py`): `read_file`/`write_file`/`edit_file`, `bash` (destructive-command blacklist, agent-settable 1-300s timeout; runs in its own session and timeout/`/stop` SIGKILLs the **whole process group** - a `&` child inherits our stdout pipe, so killing only the shell left `communicate()` waiting for an EOF that never comes; see `_kill_and_drain`), `web_search`/`web_fetch` (**Exa MCP** - one `web_search_exa` call per query, batched `web_fetch_exa`; free anonymous tier, `exa_api_key` raises the limit), optional `browse` (Lightpanda, install via `deploy/install_lightpanda.sh` -> `./tools/lightpanda`)
- Tool rounds (input + results) persist in session history - the next turn carries them, so no re-reading the same file every message; failed/interrupted rounds stay too (unanswered calls get a "[not executed...]" placeholder). Each round's **reasoning is kept in context** on the assistant message (`reasoning_content`, tail-capped at `REASONING_KEEP_CHARS` = 4000 - reasoning is display-length and every stored message is re-sent on each call), so the model re-reads its own analysis on the next round instead of starting cold; it is counted in `_history_tokens` like any other content. Output over `tool_output_max_bytes` (default 40 KB) spills to `sessions/<session-id>/tool_output/` - model sees preview + path, history keeps only the path; text-form tool calls in an answer (model bypassed the tools API) are parsed by `textcalls.recover()` and executed as a normal round instead of being delivered raw to chat. `recover()` speaks two dialects: the `<function=bash><parameter=command>` plain-text form, and **DeepSeek DSML** (V3.2 `function_calls`, V4 `tool_calls`, V4.1 ` calls`/` invoke`/` parameter` - V4.1 puts a space inside every tag - plus the `<|DSML|>` ASCII twin). In DSML `string="true"` keeps the value as a raw string and `string="false"` parses it as JSON (a missing `string` behaves like `false`, falling back to the text); a bare JSON object filling an invoke body is parsed as the arguments; wrapper-less invokes are accepted. Overlapping spans are merged and stripped right-to-left, so no shared markup corrupts the prose around it. Markup that no grammar can parse is delivered as **plain text and logged** - never re-asked, never guessed at, so an unseen dialect stays visible in chat and can be supported later. A call that DOES parse but names a tool this request never offered (a hallucinated name, one turned off in config, or markup merely quoted while describing a format) is answered with a `[tool unavailable: <name> - this session offers only: ...]` tool result: the round stays valid, the model corrects itself, and nothing runs. Whatever produced no call is stripped by `textcalls.sanitize()` before it is stored - markup left in history is copied back by the model, so one leak poisons every later turn
- Tool rounds run in-turn **uncapped** (`/stop` interrupts a runaway turn); each turn logs `tool round N: X call(s)` and `turn done: ... tool_rounds=...`
- `/stop` interrupts a running reply/tool: an update-watcher thread owns getUpdates, intercepts /stop mid-turn, kills running `bash`, and records a neutral "[This run was interrupted by the user...]" marker in context
- Concurrent sessions: a dispatcher gives each session (chat + topic) its own worker - a turn in one topic never blocks another topic's messages or commands; a plain message sent while your own session is mid-turn is folded into context at the next tool boundary (marked "[sent while you were working...]") so you can steer without `/stop`; every round's reasoning is shown live as its own collapsible block (`/thinking off` hides)
- Reply-to/quote context: replies and quoted parts are injected into the prompt as annotations (item 17)
- Dynamic `/help` built from the command registry; commands registered with `setMyCommands`; per-session `x-opencode-session` header (docs: "a stable session ID ... for each conversation" - value is `sha256("keirai:<session id>")[:32]` from `main._session_header`, stable for the session's life, distinct per session, hashed so neither chat ids nor the raw session id leave the machine) + `keirai/0.1` user agent
- Regular-message fallback path: Markdown -> Telegram HTML rendering with 4096-char splitting (never breaks code blocks; tables as aligned `<pre>`, nested list bullets)
- `/test_md` (regular pipeline) and `/test_rich` (rich pipeline) for live rendering verification
- `/models` lists models with context/cost stats from **models.dev** (OpenCode's own catalog, daily sync to `cache/models_meta.json`); `/model provider/id` switches models
- **Go-first inference**: default model `go/mimo-v2.6-flash`; on a provider error the call retries once on the other provider when it has a key AND its catalog serves the model
- **Usage accounting**: each call's `usage` (blocking + streaming) accumulates per session in `sessions.db` (`session_meta`: id `yyyymmdd-hhmm-4hex`, tokens in/out/cached, notional cost with cached-read discount)
- `/context` and `/cost` are two names for ONE report (same handler): session name/id/model, rough tokens used vs the model's **real context window** (models.dev), message count (every stored message - user, agent and tool call/result), the auto-compact threshold in tokens, token totals (in/out/cached) and the session's notional cost - **no character counts anywhere**: `[A-Za-z0-9]` ~4 chars/token, every other character (space/punctuation/CJK/emoji) 1 token each, system prompt included, tool calls+results and kept reasoning counted like any other message
- **Topic flow** (`/topic`, persisted in `config.toml`): entry needs Threaded mode ON **and** "Disallow users to create topics" ON (checked live via getMe, typed confirmation); while ON, All takes commands only (session-scoped rejected with a hint, `/model global` allowed) and every topic message needs a bound session (unbound -> rejection hint)
- `/session [page N | N | <id>]`: lists sessions (name/id/timestamp/preview, last-active first, current excluded) and switches them; topic-flow switches go through typed per-slot prompts (switch-here / create-new / ping / do-nothing) with dead-topic ping recovery
- Sessions move between **slots** (`thread_id` >0 = bound to that topic, main chat = None, negative = parked/unbound); `/new` creates a topic+bound session in topic flow, or parks the old session and starts blank in normal flow
- **Agent titles**: the rename happens **before the message reaches the model** - the JSON-constrained title call (OpenCode-style prompt) is joined first (`TITLE_WAIT` = 10s), then the reply is generated under that name; **one rename per session, no retries**; past the wait or on fault a derived fallback (first line ~48 chars, media placeholder) sticks - still applied before the reply; faulty output (tool-call text, >12-word answers) is rejected, never truncated; provenance `derived < llm < user`; media-only messages named from the media; `/rename` mirrors session+topic
- Photos -> vision models; media round-trip test via caption `media_test`
- Logging: stderr + `logs/keirai.log` (rotating)

Not implemented yet: P5 (custom OpenAI-compatible providers, interactive setup script, then the question tool) - see TODO.md. Topic flow is unit-tested; the live walkthrough on the dev/production bot is still pending.

## External Components & Licenses

- Keirai code: AGPLv3 (LICENSE)
- Lightpanda browser (optional tool): **AGPLv3** - same license, compatible; invoked as a separate process, never linked
- **Exa MCP** (mcp.exa.ai): hosted service with a free anonymous tier (rate limited; an `exa_api_key` raises it); we only make REST calls (no license implications)
- Telegram Bot API (sendRichMessageDraft / sendRichMessage / editMessageText): plain HTTPS calls from our own code, no client library (no license implications)

## License

Keirai is licensed under **AGPLv3** (see LICENSE). Keep this in mind when adding dependencies: all project code must remain AGPLv3-compatible. Library licenses must be checked before implementing (see TODO.md).

## Future Development

See **TODO.md** for planned features. All code should be written with awareness of what's coming - don't paint yourself into corners.

## Memory Budget

Target: Agent must run comfortably within 256MB RAM, leaving room for the OS on a 1GB server.