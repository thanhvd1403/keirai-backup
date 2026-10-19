# Keirai - TODO

> Features planned for future implementation. Write code with these in mind.
> Priorities: P0 = highest. **P0-P2 are complete.** Outstanding work is split
> into implementable phases:
> - **P3** - provider foundation: usage capture, models.dev metadata, Go-first priority, `/context` + `/cost`
> - **P4** - topic flow & session UX: `/topic` mode, `/session`, delivery rules, titles
> - **P5** - custom OpenAI-compatible providers + setup wizard, then the question tool (last)

## P0 - Telegram Bot with Markdown & Reasoning Display (complete)

### 1. Library research & decision
- [x] Compare options: python-telegram-bot, aiogram, raw HTTP calls
  - Evaluate: memory footprint, effort saved, maintenance status
- [x] Check license of chosen library before implementing (must be compatible)
  - Decision: raw HTTP with stdlib only - zero dependencies, zero license risk, minimal RAM

### 2. Bot skeleton & access control
- [x] Config system (TOML `config.toml`, comments allowed; env var overrides; Python 3.11+ via stdlib tomllib)
- [x] Config for allowed users (list of user IDs/usernames), with an option to allow all users
- [x] Ignore/reject messages from users not on the allow list
- [x] Minimal bot: receive message → send it back (proof of life)
- [x] Decide long polling vs webhook (polling first: simpler, no TLS needed)

### 3. Markdown processing
- [x] Implement full markdown → Telegram conversion (Telegram MarkdownV2 or HTML parse mode)
  - Handle: bold, italic, code, code blocks, links, lists, headers
  - Escape special characters correctly (Telegram MarkdownV2 is strict)
  - Decision: HTML parse mode (robust, supports expandable blockquotes)
- [x] Message splitting at Telegram's 4096 char limit
  - Split without breaking markdown (e.g. don't cut an open code block in half)
- [x] Test interface: command (e.g. `/test_md <message>`) that runs the markdown pipeline and sends the result to Telegram
  - Lets us verify rendering without a live AI backend

### 4. Thinking / reasoning display
- [x] Render "thinking" / reasoning content separately from the answer (e.g. collapsible blockquote or spoiler)
  - Done via expandable blockquote
- [x] Config option: default on/off for showing thinking
- [x] Bot commands to toggle at runtime (e.g. `/thinking on`, `/thinking off`)
- [x] Persist the user's toggle choice (in-memory first, storage later)

### 5. AI providers: OpenCode Go & OpenCode Zen
- [x] Support OpenCode Go as a model provider
- [x] Support OpenCode Zen as a model provider
  - OpenAI-compatible: POST {base}/chat/completions, reasoning from `reasoning_content`/`reasoning`
- [x] List available models from each provider (slash command, e.g. `/models`)
- [x] Auto-retrieve model stats: max context window, costs, ...
  - GET {base}/models, normalized + cached to disk (24h TTL), falls back to cache when offline
  - ⚠️ **Live-probed 2026-09-25: the real `/models` endpoint returns NO stats** (every entry = `{id, object, created, owned_by}` only) -> the `limit`/`cost` normalization never populates in production; stats move to **models.dev** (see group 25, P3)

### 6. Images & media files
- [x] Receive photos and media files from users (download via Telegram file API)
- [x] Send images/media files back to users (correct type handling)
- [x] Pass images to AI providers (vision/multimodal models, where supported)
- [x] Size limits + memory-safe handling (don't load big files fully into RAM)
  - Download cap 20 MB (Bot API limit), upload cap 45 MB, config `max_file_mb`

### 7. Logging & ops
- [x] Log to file: `logs/keirai.log` (rotating 2MB x 3, plus stderr for journald)
  - Stream live: `tail -f ~/keirai/logs/keirai.log` (or `less +F`, `journalctl -u keirai -f`)
- [x] `/reset-all`: wipes everything for the chat - all sessions/context and deletes the Telegram topics the bot created
  - Confirmation: bot generates two random strings, user must type them back (e.g. `/reset-all KX3P QW9Z`)
  - Hidden from /help (tracked topics only - topics created before this feature can't be listed via Bot API)

### 8. Rich Messages (Bot API 10.1)
- [x] Send ALL AI answers via `sendRichMessage` - markdown passed through, Telegram's server parses GFM: native tables, task lists, headings, `$$formulas$$`, 32768 char limit
  - `rich_messages` config kill-switch (default on); automatic fallback to regular messages on TelegramError
- [x] Thinking display as native collapsible `<details><summary>` block (rich HTML path, converted via our md2tg)
- [x] `/test_rich` command + regular-message fallback kept for old paths (`/test_md`)
- [x] Streaming: the thinking block is streamed by **editing ONE message in place**, the answer through Telegram's modern streaming API
  - Thinking: created on the first reasoning delta (before any content) and edited in place until the round ends - reasoning arriving between answer chunks keeps editing the SAME block; a tool round edits that block into its final form instead of sending a second one, then releases the slot (`_drop_live(keep_thinking=True)`) so the next round streams below the tool lines
  - Answer: `sendRichMessageDraft` (Bot API 10.1) in private chats - animated preview in the chat, ephemeral ~30s, `can_stop` -> the user's native Stop button -> `stopped_message_generation` -> the same interrupt as `/stop`; the finished answer is then persisted with `sendRichMessage`. Groups (drafts are private-only) and `stream_mode = "edit"` fall back to in-place edits of a message of its own below the thinking
  - NOTE: `sendRichMessageDraft` is NOT `sendMessageDraft` (9.3) - the latter lands in the USER's composer and blocks them from typing/sending (bugs.telegram.org/c/62189)
  - First flush immediate, then every 1.5s; `stream_drafts` is the streaming master switch, `stream_mode = "draft"|"edit"` picks the mechanism; automatic fallback to a blocking call on any error, draft errors fall back to edits
  - Final delivery matches the live messages by ROLE (thinking/answer), so one can never overwrite the other's message; leftovers are deleted, not left stale
- [x] Streaming bugs fixed
  - `Telegram.delete_message` called the API method `delete_message` -> HTTP 404 on every call, so every cleanup silently did nothing and half-streamed messages piled up in the chat; now `deleteMessage`
  - reasoning deltas that arrived after the first content token were appended to the ANSWER -> thinking text leaked into the answer and read like a second thinking block; reasoning now always accumulates as reasoning
  - the tool round sent a SECOND thinking block next to the half-streamed live one; it now edits the streamed message (one block per round)
- [x] `==marked==`, footnotes, `$$formulas$$`, inline HTML - work automatically via GFM markdown passthrough
- [x] Media blocks in answers: `![alt](url)` passes through natively on the rich path; the regular path degrades it to a link (Telegram HTML has no `<img>`)
- [x] `editMessageText(rich_message=...)` for live edits after send
  - Non-private chats (and `stream_mode = "edit"`) stream via send placeholder + rich edits; the final answer edits the streamed answer's message in place
  - Any edit failure deletes the placeholder and sends fresh

## P1 - Session Management & Context Control (complete)

### 9. Sessions via Telegram topics
- [x] Use Telegram's forum topic API: each topic = 1 chat session with its own context
  - Bot API 9.3 (Dec 2025) brought topics to private chats with bots - enable via the **"Threaded mode" setting in the @BotFather Mini App** (t.me/BotFather?startapp, NOT the /mybots chat menu); `getMe.has_topics_enabled` confirms it - verified live (2026-09: createForumTopic -> "chat is not a forum" until enabled)
  - Works in forum supergroups the same way (message_thread_id routing)
  - Fallback when topics are disabled: `/new` clears the current session in place
- [x] Create sessions: `/new [name]` creates a new topic + fresh session (auto-names "Session N" without arg)
- [x] Rename sessions: `/rename <name>` renames the current topic/session
- [x] Keep context strictly isolated per topic (history + model choice keyed by (chat, thread))
- [x] Storage for session context: SQLite `sessions.db` (stdlib sqlite3) - history, model choice and topic registry
  - Write-through after every turn, loaded on startup - sessions survive restarts and are CLI-manageable
  - ⚠️ **Superseded by groups 20/22**: with topic flow OFF, messages in topics are inert + hinted (no per-topic sessions); the per-topic-isolation tests get rewritten around the switchable mode

### 10. /delete command
- [x] `/delete` clears the current session's context (the conversation memory) and its db row
- [x] Do NOT delete the Telegram topic - send a confirmation that the session is deleted and note the user can delete the topic manually

### 11. Context window management
- [x] Configurable limit for context size: `context_limit_tokens` (rough tokens; default 30000 - no tokenizer dependency; was `context_limit_chars`, renamed when the report went tokens-only)
- [x] `/compact` slash command: manually compact the current session's context (reports before/after sizes)
- [x] Auto-compact when context exceeds the configured limit (last resort only: if summarizing fails AND the window is full, drop oldest down to ~half the window, floor 6 messages)
- [x] Compaction strategy: summarize older messages via the provider into a `[Summary of earlier conversation]` marker message, keep the last 4 messages
- [x] **No message cap**: the whole session history is sent every call (`_prompt()`) - auto-compact alone bounds size; was: newest 24 only (`HISTORY_LIMIT` + `_trim`, both removed)
- [x] Images without a message cap: only the newest `IMAGE_KEEP` (4) image-bearing messages keep their base64 pictures in the prompt (older ones become a marker, stored history untouched) - otherwise every call re-sends every picture ever received: provider image/size caps plus image tokens billed each turn
- [x] Compaction hardened so it can be the sole bound: the summary transcript is capped at ~1/8 of the model window (newest older messages first) so the summary request itself can never overflow

### 12. CLI fallback for session management
- [x] `python cli.py list|delete|rename` - works without Telegram, operates directly on sessions.db
- [x] List sessions (chat, thread, name, message count, model, last active)
- [x] Rename sessions (stored session/topic name)
- [x] Delete sessions (context only; prints a hint that the Telegram topic is untouched)

### 13. /help command listing
- [x] `/help` lists all available commands dynamically - built from the COMMANDS registry, hidden commands excluded
- [x] Register commands with `setMyCommands` at startup so they show up in the bot's UI menu

## P2 - Core Tools & Message Context (complete)

> First real tools for the agent. Built on OpenAI function-calling specs
> (`tools.py` registry: `available_specs(cfg)` + `execute(cfg, name, args, ctx)`);
> tool rounds persist in history (input + results; oversized output spills to a file - group 27).

### 14. File & shell tools
- [x] `read_file` - read file contents (size caps: 200 KB hard, default 50 KB, `max_chars` arg)
- [x] `write_file` - create/overwrite files (creates parent directories)
- [x] `edit_file` - targeted edit (exact match, must be unique - errors otherwise, no full rewrites)
- [x] `bash` - execute shell commands
  - Timeout: config `bash_timeout` (default 30s), agent-settable per call (`timeout_seconds`, clamped 1-300)
  - Safety per agreed decision: destructive-command blacklist (rm -rf on /, mkfs, dd to /dev, shutdown/reboot, fork bombs, curl|sh, ...); NO path sandbox (single trusted operator)
  - Output too long -> saved whole to `sessions/<session-id>/tool_output/` (over `tool_output_max_bytes`, 40 KB), model gets preview + path (group 27)
  - Interruptible: polls the /stop flag and kills the process mid-run

### 15. Web search & fetch (Exa MCP)
- [x] `web_search` via the hosted **Exa MCP** (https://mcp.exa.ai/mcp) - `web_search_exa`, ONE call per query (the tool's 2-3 diverse queries -> 2-3 calls, capped at `MAX_QUERIES`), results concatenated per query; queries that fail are dropped, only "all failed" raises
- [x] `web_fetch` via `web_fetch_exa` (all URLs in ONE batched call -> clean markdown); `full_content` raises `maxCharacters` (`FULL_CHARS`), otherwise `EXCERPT_CHARS`. Exa has no per-fetch objective, so that argument left the tool spec
- [x] API key via config: optional `exa_api_key` / `EXA_API_KEY`, appended as `?exaApiKey=` (the exact form the server hands back); works anonymously too, rate limited
- [x] Quota guard: Exa reports a throttled search as ordinary content (not `isError`) - `websearch._check_quota` raises instead of feeding a rate-limit notice to the model as if it were results
- [x] Replaces the **Parallel Search MCP** (`parallel_api_key`/`PARALLEL_API_KEY` removed): the free endpoint started answering HTTP 429 for this server, and Exa's fetch already worked keyless
- [x] Stdlib streamable-HTTP MCP client (`websearch.py`): initialize -> notifications/initialized -> tools/call, JSON or SSE bodies

### 16. Browser (Lightpanda)
- [x] License checked: Lightpanda is **AGPLv3** - same as Keirai, fully compatible (separate process, never linked)
- [x] Drive it from Python: `lightpanda fetch --dump markdown <url>` via subprocess - renders JS, prints markdown; no CDP/WebSocket client needed (CDP reserved for future interactive needs like clicks/forms)
- [x] Start/stop on demand: one subprocess per call, never resident; telemetry disabled (`LIGHTPANDA_DISABLE_TELEMETRY=true`)
- [x] Install script: `bash deploy/install_lightpanda.sh` puts the binary in `keirai/tools/lightpanda`; the `browse` tool only appears to the model after install (or when `lightpanda` is on PATH)

### 17. Reply-to-message context
- [x] Detect replies via `reply_to_message` and inject the referenced message as an annotation ("user is replying to this message (from @x): ...")
- [x] Detect quoted text (`quote` object) - "user is quoting this part of a message: ..."
- [x] Handle replies to the bot's own messages and older messages (content arrives in the reply object; capped at 1500 chars to respect context limits; no-text replies noted as media/rich message)

### 18. /stop - interrupt running work
- [x] `/stop` interrupts the reply/tool turn running in the current chat+thread
  - The watcher thread owns getUpdates and feeds a queue; it intercepts /stop during the session's active turn (other messages are queued, never lost). Other sessions run on their own workers meanwhile (group 28)
  - Interruption points: every streaming delta, every tool round, before/after each tool, and mid-`bash` (process killed)
  - Context marker written for the next turn: "[This run was interrupted by the user before it finished - any running command/tool did not complete.]" (says the user interrupted, without referencing the /stop command)
  - Partial live message (edit mode) is deleted; user gets "stopped - the running reply/tool was interrupted"
  - Idle `/stop` replies "nothing is running right now"

### 27. Tool rounds in history + spill files
- [x] Tool input + output persist in session history (`sessions.db`) - the next turn carries them, so the model stops re-reading the same file on every message
- [x] Failed/interrupted rounds stay in context too: a failing tool's error is a result; a provider error or `/stop` keeps the round and closes unanswered tool_calls with a "[not executed...]" placeholder (providers reject dangling calls)
- [x] Output over `tool_output_max_bytes` (config, default **40 KB = 40k ASCII chars**) is written whole to `sessions/<session-id>/tool_output/<tool>-<sha1>.txt`
  - Live: head preview + "full output saved to <path>" (OpenCode `truncate.ts` style); history: only the path (decision)
  - Content-addressed names (identical output reuses one file); `/delete` and `/reset-all` remove the session's spill dir
- [x] Front-trims/compaction strip orphaned leading tool results (a cut can separate a result from its call); auto-compact counts tool output toward the threshold

## P3 - Provider Foundation: Metadata, Priority & Cost (complete)

> Everything providers.py / config / sessions.db level. No Telegram UX beyond
> two new read-only commands. Specs live in groups 25 + 21.

**Implementation points (in order):**
1. [x] `providers.py`: capture **`usage`** from every call - blocking responses (`usage` object, previously discarded) and streaming (`stream_options: {"include_usage": true}` + the usage-carrying chunk, whether `"choices": []` on Zen or attached to a choice chunk on Go) - via a `usage_out` dict handed to callers
2. [x] Swap the `x-opencode-session` header value to the **anonymized app-wide constant `keirai`** (`main.SESSION_ID`; no chat ids leave the machine, also passed to tool ctx - per-prefix cache hits unaffected)
3. [x] **models.dev daily sync** (group 25): one `api.json` fetch/24h -> extract `opencode` + `opencode-go` -> compact `cache/models_meta.json`; `/models` stats display now merges from it (the dead `/models` normalization stays as fallback)
4. [x] **Go-first priority** (group 25): built-in `default_model` -> `go/mimo-v2.6-flash`; `_alternate_provider()` + `pcall()` in `_ai_reply` - on `ProviderError` retry once via the other provider iff it has a key AND its catalog serves the model id
5. [x] sessions.db: new **`session_meta` table** (separate from `sessions` so the history REPLACE can't wipe it): `session_id` (`yyyymmdd-hhmm-4hex`, assigned at creation, **backfilled** for pre-existing rows), `name`, **`cost_usd`, `tokens_in`, `tokens_out`, `tokens_cached_read`, `tokens_cached_write`** (cleared together with the session by `/delete`/`/reset-all`)
6. [x] **`/context`** command (group 21): rough tokens vs limit, message count, auto-compact trigger + session name/id/model + token totals
7. [x] **`/cost`** command (group 21): the context block + notional cost (cached-read discount applied) + token totals
8. [x] Tests for each (24 new: usage capture both paths, metadata parse/cache, cost math, fallback routing, command output, meta lifecycle/backfill) + docs (`config.example.toml`, AGENTS.md)

### 25. Model metadata (models.dev) & Go-first priority
*(moved here from P0 group 5 - was outstanding inside a completed phase)*

- [x] **Model metadata sync from models.dev** (the price/context source; decision 2026-09-25)
  - models.dev (`https://models.dev/api.json`) is OpenCode's own database (GitHub `anomalyco/models.dev` - "we use it internally in opencode") and is what OpenCode itself uses for pricing + context limits (V2 docs: "OpenCode includes a provider and model catalog from models.dev")
  - Verified live: providers **`opencode`** (= zen, `https://opencode.ai/zen/v1`, 113 models) and **`opencode-go`** (41 models) with `cost {input, output, cache_read}` per 1M, `limit {context, output}` and `modalities` (image input = vision detection) - e.g. `glm-5.3-flash`: $0.15/$0.50, ctx 1M, exactly matching the docs pricing table
  - Fetch: **daily** (24h TTL, same pattern as the models cache) - one `GET https://models.dev/api.json` (~4.9 MB; no per-provider endpoint exists, SPA swallows path guesses, repo per-model TOMLs would need 154 file fetches > unauth GitHub rate limit) -> extract just the two providers into a **compact derived cache** (few KB), discard the payload; transient parse spike of tens of MB, within the memory budget
  - Feeds: `/cost` price table (group 21), `/models` stats display (fixes the dead normalization), vision-capability detection (group 19 media naming)
  - Checked per request: Hermes Agent (`NousResearch/hermes-agent`) resolves **context length** via a multi-source chain ending at models.dev and only picks up pricing opportunistically from `/models` payloads - models.dev gives us both cost AND context in one place
- [x] **Go-first inference priority** (decisions 2026-09-25): infer on the **Go** key first, use **Zen** as a fallback
  - **Option (a) chosen**: built-in `default_model` flips to **`go/mimo-v2.6-flash`**; PLUS runtime error-fallback - on `ProviderError` from the primary provider, retry once on the other provider's endpoint **iff** it has a key **AND its catalog serves the same model id** (checked against the cached model list - never blind-retry `zen/gpt-5.2` on go); an explicit `/model zen/...` still starts on zen (and falls back to go on error)
  - Replaces current behavior: `_resolve_provider` routes by model prefix only; key-missing falls back to the other provider (prefer go); built-in default was `zen/glm-5.3-flash` -> with both keys present the default landed on **zen**

### 21. /context and /cost commands
- [x] `/context` shows the current context window usage of the session: rough tokens used vs `context_limit_tokens`, message count (user + agent + tool messages), whether an auto-compact would trigger - plus **session name, session id, active model**
- [x] Report is **tokens-only**: no character counts and no `user · replies · tools` split (tool calls/results still counted, just not itemised); estimator = `[A-Za-z0-9]` ~4 chars/token + 1 token per other character, system prompt included; the same estimate drives the auto-compact trigger
- [x] `/cost` (new): same context block (used vs limit + the auto-compact trigger point) **+ total cost of this session**, plus session name, session id, active model
  - Cost research - **live-probed 2026-09-25** (service-account key; zen blocking + streaming, go, V2 inference): **no cost field anywhere** - not in the JSON body, not in response headers -> **sum locally**: capture `usage` after every API call, multiply by price
    - Blocking: standard `usage` object (returned today, currently discarded); Go additionally reports `prompt_tokens_details.cached_tokens` + `completion_tokens_details.reasoning_tokens`, zen's `prompt_tokens_details` comes back empty on cache misses
    - Streaming: `stream_options: {"include_usage": true}` is accepted; the final chunk before `[DONE]` has `"choices": []` + `usage` (chunks without `choices` are skipped today - must be captured)
    - Go requires the `x-opencode-session` header (else `400 MissingSessionID`); Zen **caches only when the header is sent** - value: anonymized app-wide constant `keirai` (decision)
    - ⚠️ **`GET /models` returns NO pricing/context metadata** (all 43 entries = `{id, object, created, owned_by}`; per-model GET 404, console pricing endpoints 404) -> the existing `/models` stats code (expects `limit`/`cost` keys) **never populates in production**
    - **Price source RESOLVED: models.dev** - daily sync spec lives in group 25 (extracts `opencode` + `opencode-go` cost/limit entries into a compact cache; also fixes the `/models` stats display)
    - ⚠️ **Free models cannot be called from the bot**: `403 FreeTierError - "OpenCode's free tier can only be used from within OpenCode"` (never point config at a `-free` model)
    - Console Usage API (`GET /console/api/v1/usage/export`, CSV) returns ground-truth `cost_micro_cents` + full token/cache/reasoning breakdown - but **no session attribution** and batch ranges only (24h/7d/30d) -> unusable for per-session `/cost`; fine as a manual cross-check (observed: Go subscription rows carry `cost_micro_cents = 0`)
    - Cache handling (verified live 2026-09-25): **cached READ is priceable on BOTH providers** - `prompt_tokens_details.cached_tokens` reports it; Zen only caches when the **`x-opencode-session` header** is sent (2 identical calls: no header -> `0`; with header -> `cached_tokens: 2048/2292` on glm-5.3-flash, `1152/1263` on kimi-k3); the header pins the routing **instance**, while the cache is keyed by **content prefix** -> one constant id keeps all traffic on one instance where every prefix warms (per-topic hits unaffected); note: Go's cache behavior is unproven (reported `cached_tokens: 0` even with a stable header); formula: `(prompt - cached) x cost_in + cached x cost_cache_read` (rates from models.dev `cost.cache_read`; model without a cache rate -> price all at input); missing/empty `prompt_tokens_details` -> cached = 0 (safe overestimate)
    - Cached WRITE still NOT priceable: chat/completions usage has no cache-creation field; the Anthropic-native `/v1/messages` endpoint reports `cache_creation_input_tokens`/`cache_read_input_tokens` natively BUT the gateway ignores explicit `cache_control` (all-zero counters probed) and **claude models are unreachable via chat/completions** (`503 Endpoint is unavailable` - yet still listed by `/models`, worth a listing filter) -> write tokens stay inside `prompt_tokens` at input price; keep models.dev `cost.cache_write` rates in the cache for future use (Console usage-export CSV `cache_write_*` columns remain provider-side ground truth: batch, no session attribution)
    - Tiered models (`context_over_200k` + `tiers` on some entries): v1 uses the base input/output rate; tier-boundary math only if a tiered model gets heavy use
  - Accumulated per session in sessions.db: **`cost_usd` (notional tokens x price) + `tokens_in`, `tokens_out`, `tokens_cached_read`, `tokens_cached_write`** (cleared together with the session by `/delete`)
  - Display decisions: `/cost` shows the **notional tokens x price** (Go's dollar limits are denominated that way even though the subscription bills $0); **`/context` and `/cost` both show the session totals: tokens in / out / cached read / cached write** (both providers report cached reads once the session header lands a cache hit; cached write stays 0 until an endpoint reports it)
- [x] Both commands are session-scoped (work in topics, rejected in the All topic - enforcement ships with P4's group 20 delivery rules ✓)

## P4 - Topic Flow & Session UX (complete)

> The switchable `/topic` mode with All-topic rules, `/session` switching,
> per-session prompts and agent titles. Specs live in groups 19, 20, 22.

**Implementation points (in order):**
1. [x] **`/session` command** (group 22): list (name/id/timestamp/preview, last-active first, `[image]` placeholder), `page N`, `N` against current page, `<id>` immediate switch; never lists the current session
2. [x] **Prompt machinery**: typed-choice prompts with **per-session slots** (one per topic + one for All), command-cancels-own-slot, wait forever, plain message = "do nothing" + normal handling (reuses `/reset-all` pending-confirm pattern)
3. [x] **Topic-flow mode** (group 22): `topic_flow` flag write-through in config.toml; `/topic` enter (typed confirm, BOTH `getMe` preconditions with hints, no auto-created topic, hint `/new`) & exit (typed confirm, persist flag)
4. [x] **Delivery rules** (group 20): All = commands only (+ hint every plain message), unbound-topic rejection hint, flow-off = topics fully inert + hint every message; rejected/allowed command lists in All
5. [x] **Bindings & switching** (group 22): topic<->session binding table, switching matrix (normal / unbound / bound x All / in-topic), per-topic prompts, ping liveness + dead-topic recovery, `/stop` topic-only
6. [x] **Titles** (group 19): extra naming completion after the first user message (default model, joined BEFORE the reply starts, media-aware via models.dev vision info); `/new` in All/topic creates topic + first-message title
7. [x] **Mirror commands**: `/rename` renames session AND topic; `/model global <id>` surgical config.toml write (keep comments)
8. [x] Rewrite the group-9 per-topic isolation tests around flow on/off (213 tests green: mode gates, hints, prompt matrix, ping recovery, titles, config writes) - **unit-tested; live walkthrough with you still pending** (drafts-in-topics + getMe flags already proven live)

### 19. Agent-generated session titles
- [x] New sessions get an agent-generated **short** title (replaces "Session N"; `/new <name>` skips generation)
  - **Single rename, and it comes FIRST**: the naming call runs with the first user message and is joined **before the message reaches the model** (`ai_reply` -> `_maybe_name_session` + `_finish_naming`, bounded by `TITLE_WAIT` = 10s) - the reply is generated under the name the chat already shows; success = renamed exactly once (model, config default - new sessions start there)
  - **JSON-constrained** (`response_format: json_schema`, `max_tokens` 512), OpenCode-style prompt with good/vague/too-long examples
  - **Guards reject, never truncate** (oh-my-pi #7306): tool-call text, >12-word answer-shaped output, prompt-example echoes; no length cap on valid titles
  - **Fallback**: on timeout/fault the derived title (first line, word-boundary ~48 chars; media placeholder) is applied and sticks - **one attempt per session, no retries**
  - **Provenance** `derived < llm < user`: `/rename` and `/new <name>` are never overwritten
  - Both flows; the topic is renamed to match (mirrors `/rename`, group 22)
  - **Media-only first message**: named from the media - image attached when the model is vision-capable, else a type/filename descriptor

### 20. Topic-only delivery & All-topic rules
- [x] All AI messages must be delivered inside a topic - **ignore every message sent to the "All" topic**
  - [x] Verified live (2026-09 dev bot): in topic flow, All-topic messages arrive with **NO `message_thread_id`** -> `thread_id is None` = All, `thread_id` set = topic session
  - ⚠️ When `allows_users_to_create_topics` (BotFather "allow user to create topics") is ON, sending from the All view makes the client **implicitly create a topic per message** (`is_name_implicit`, auto-named after the text - e.g. a topic literally named "/new"). This option must be OFF (see group 22)
  - [x] Plain message in All during topic flow: ignore + send a **hint EVERY time** ("topic flow is on - /new to start a topic, /session to switch")
- [x] Message lands in a topic that is **bound to no session** (residual/manual topic) during topic flow: **reject with a hint** - "this topic is bound to no session, use /session to bind a session"
- [x] The "All" topic only accepts suitable commands, e.g.:
  - `/new` **in All** creates a new topic, then the topic title is set (very short, agent-generated, group 19) after the first user message sent in it
  - `/new` **inside an individual topic** causes the same behaviour (creates a new topic + first-message title), and the `/new` itself is **ignored in the history of the current session** (commands are never recorded)
- [x] Session-scoped commands are unavailable in the All topic: `/context`, `/cost`, `/stop`, `/compact`, `/delete`, `/rename`, `/model` - reject with a short hint to use `/new` or open a topic
  - Includes the new `/context` and `/cost` commands (see group 21)

### 22. Topic flow - switchable mode (via /topic)

> Two per-chat modes. Normal = plain chat; Topic = sessions live in topics.
> Group 22 spec COMPLETE (2026-09-25) - no open questions remain.

**Normal flow (default):**
- [x] Message like normal (no thread id)
- [x] `/new` clears the current context and moves to a blank session
  - Old sessions are still saved in the DB so the user can recover them with `/session` (list + switch - same command as in topic flow)

**Entering topic flow - `/topic` command:**
- [x] First ask the user to confirm the switch (typed reply) - explained: sessions are started afterwards with `/new`
  - The **active main-chat session is left intact, NOT cleared/deleted** - the main chat becomes the "All messages" view; the user can recover that session later via `/session` from All or from a topic
- [x] Preconditions - **BOTH must hold**, else refuse with a hint:
  - Threaded mode ON (`has_topics_enabled` via getMe) - hint: enable Threaded mode in the @BotFather Mini App
  - **"Disallow users to create topics" toggle ENABLED** (`allows_users_to_create_topics == false` via getMe) - hint: enable that toggle (otherwise the client implicitly creates topics per message - group 20)
  - Topic flow only works when both Threaded mode and "Disallow users to create topics" are enabled (decision)
- [x] Persist the mode flag **in config.toml** (write-through: update file + in-memory) so it survives restarts (decision)
- [x] **No topic auto-created at entry** (decision) - just hint the user to run `/new` to create the first topic
- [x] **No residual-topic check at entry** (decision) - liveness is only verified when a ping is called (see switching logic)
  - Constraints (verified live): the Bot API has **no "list topics" method**, and **topic deletions are NOT broadcast** to the bot (no `forum_topic_deleted` event); sending into a dead topic fails with `400 message thread not found` - the ping is the liveness probe
  - The `topics` table in sessions.db remains the registry of bot-created topics (persists across restarts); `forum_topic_created` events are registered as they stream past
- [x] Each topic binds to a session in the database (sessions.db)

**The `/session` command (all flows):**
- [x] `/session` (no args): list the **first 5 sessions** - each row shows **name, session id**, **timestamp** + **preview of the last user message**; **ordered by last-active (most recent first)**; if the last user message has no text, the preview is the placeholder **`[image]`**
- [x] `/session page N` - show the Nth page (5 sessions per page)
- [x] `/session N` - switch to the Nth session **on the current page** (remember the current page per chat so N resolves against it)
- [x] `/session <id>` - switch immediately to that specific session
  - **Session id format: `yyyymmdd-hhmm-4hex`**, assigned when the session is created (unambiguous vs the bare-number page selector); stored in sessions.db
- [x] The list **never includes the current active session**: not the session bound to the topic you're in, and not the main-chat session when running in normal flow; when `/session` is run in **All** (topic flow) no session is active there, so the full list is allowed

**Prompt mechanics (every typed-choice prompt in this flow):**
- [x] Choices are made via **typed replies** (reuses the `/reset-all` pending-confirm machinery - no inline keyboards / callback_query)
- [x] Running **any command** while a prompt is pending **cancels** that session's prompt (the command runs normally; prompts in other sessions are untouched)
- [x] Prompt slots are **per session**: one per topic **plus one for the All view** - multiple prompts can be outstanding at the same time across different topics
- [x] A prompt **waits forever** (no timeout); a new prompt in the same session replaces that session's pending one
- [x] Sending a plain new message while a prompt is pending in the same session = the "do nothing" choice + the message is handled normally (per switching logic / delivery rules)

**Switching logic:**
- [x] **Normal flow**: switch immediately to the selected session
- [x] **Topic flow, session has no bound topic:**
  - `/session` triggered **in All**: immediately create a topic, rename it to the session name, continue from there
  - `/session` triggered **inside a topic**: ask the user **3 options** - **1. switch here** (bind session to this topic) / **2. create new topic** (as above) / **3. do nothing**
    - **Any new message in this same topic while the prompt is open counts as "do nothing"**: the prompt is cancelled and the agent answers the new message normally
- [x] **Topic flow, session has a bound topic**:
  - `/session` triggered **in All**: ask the user **2 options** - **1. ping** the bound topic (locate it) / **2. do nothing**
  - `/session` triggered **inside a topic**: ask the user **3 options** - **1. switch here** (rebind the session to this topic) / **2. ping** / **3. do nothing** (new message = do nothing, as above)
  - **If ping fails** (`400 message thread not found` - topic was deleted): in both cases immediately **unbind the dead topic id and create a new topic for that session** (automatic recovery; ping doubles as the liveness probe)
  - **"switch here" unbinds the session's previous topic** (old topic stays, just unbound)
- [x] **Leaving topic flow (`/topic` off)**: keep all binding ids - topics are NOT deleted when leaving topic flow
- [x] `/unbound`: **dropped by decision** - unbinding now happens implicitly (switching elsewhere / dead-topic recovery)
- [x] `/rename` inside topic flow renames **both the session and the topic** (they mirror each other)
- [x] `/model` is **session-scoped** (sets the model of the current session); **`/model global <provider/model-id>`** (e.g. `/model global go/glm-5.3-flash`) sets the **default model in config.toml** (persisted by writing the file at runtime; implementation note: surgical line edit to keep config comments)
- [x] Delivery rules of group 20 apply (AI answers only inside topics; All accepts suitable commands only, incl. `/new` and `/session`)

**Command availability in All (decision):**
- [x] **Rejected in All** (session-scoped): `/context`, `/cost`, `/compact`, `/delete`, `/rename`, `/model` (except `/model global`), **`/stop`** - `/stop must be run inside a topic** (no global interrupt)
- [x] **Allowed in All**: `/new`, `/session`, `/topic`, `/help`, `/start`, `/models`, `/thinking`, `/test_md`, `/test_rich`, `/reset-all` (chat-wide)

**Leaving topic flow - running `/topic` again:**
- [x] User must confirm switching back to the normal flow (typed reply, same mechanics)
- [x] Persist the mode flag back to config.toml (as at entry)
- [x] After the switch: all bot messages are sent **without** a thread ID; **EVERY message that lands inside a topic is ignored - plain messages AND commands alike - and answered with a hint EVERY time** (topics are fully inert while flow is off; session recovery then works via `/session` from All only - `/session` from a topic applies while topic flow is ON)

### 28. Concurrent sessions, mid-turn steering & live thinking
- [x] **One worker per session key** (chat + topic): a turn streaming in one topic no longer blocks other topics/sessions - messages AND commands elsewhere run immediately; within a session everything stays ordered (the watcher still owns getUpdates and /stop)
- [x] **Mid-turn injection**: a plain text message sent while its session's turn runs is parked in a thread-safe inbox and folded into context/history at the next tool boundary, marked "[sent while you were working...]" - steer the work without /stop; messages arriving after the last boundary are handled as the next turn
- [x] **Every thinking block shown**: each completed round's reasoning is sent as its own collapsible block before that round's tool progress (the final round still ships inline with the answer); `/thinking off` hides them
- [x] Thread-safety: SQLite store on one locked connection (`check_same_thread=False` + RLock), `config.write_value` locked, session slot/counter ops locked; cosmetic sends never break a turn
- [x] Decisions: same-session commands wait for the turn (`/stop` excepted); only text injects (media/commands queue)

## P5 - Custom Providers, Setup Script & Question Tool (last)

**Implementation points:**
1. [ ] Custom provider config + `/models` integration (group 23), using the group 25 metadata pipeline for stats where available
2. [ ] Interactive `setup.py` wizard (group 24)
3. [ ] Question tool with inline-button answers (group 26)

### 23. Custom providers
- [ ] Support custom OpenAI-compatible providers via config (base URL + API key)
- [ ] List available models per provider (`/models` should include them)
- [ ] Auto-retrieve model stats where the provider supports it (max context window, costs, ...)
- [ ] Per-provider/per-model overrides in config (e.g. manual context window, cost values for providers that don't report stats)

### 24. Interactive setup script
- [ ] `python setup.py` wizard that:
  - Prompts for bot token (validate live via `getMe` before saving)
  - Prompts for provider API keys (validate by fetching `/models`)
  - Asks for allowed users (IDs/usernames) or allow-all
  - Writes `config.toml` in the project dir
- [ ] Optional keep-alive service install, per user choice at the end:
  - Detect systemd; if present, offer to install the unit with correct User/paths, then `daemon-reload` + `enable --now`
  - Skip gracefully (with a printed hint) when systemd is unavailable
- [ ] Rerunnable: detect existing config and offer to edit values instead of overwriting

### 26. Interactive question tool (inline buttons)
- [ ] New agent tool (e.g. `ask_user`) the model calls when it needs the user to decide something
  - Sends the question with **inline keyboard buttons**, one per option (`reply_markup: inline_keyboard`)
  - Tapping a button answers: the chosen option is returned to the model as the tool result and the turn continues
  - Last button always **"Write my own answer"**: tapping it makes the script **wait for the user's next message** - that message text becomes the tool result
- [ ] Introduces `callback_query` handling - new territory: group 22 prompts are typed replies by decision, no inline keyboards
- [ ] One answer per question: edit the buttons off after answering; a second tap is ignored
- [ ] Pending question is per session (same slot idea as the typed prompts); a command or a new plain message while a question is pending = cancel + handle normally (mirror group 22 rules)
- [ ] Open: does the tool block the turn until an answer arrives, and does it wait forever (like typed prompts) or expire after a timeout?
