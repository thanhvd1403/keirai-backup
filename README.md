# Keirai

> ⚠️ **WARNING: This project is unstable and prone to bugs.** It is developed
> for personal use and changes fast. Expect rough edges, breaking changes and
> the occasional broken session. Use at your own risk — and keep a backup of
> `sessions.db` if you care about your history.

A lightweight AI agent that lives in Telegram. It runs on a small server
(1GB RAM), talks to OpenCode Zen / OpenCode Go models, and can actually *do*
things: read and write files, run shell commands, search the web, and browse
pages.

- **Zero dependencies** — pure Python 3.11+ standard library
- **Telegram Bot API** via raw HTTPS (no framework)
- **Sessions** persisted in SQLite — context survives restarts
- **Agent tools**: files, shell (blacklist + timeout), web search/fetch, optional Lightpanda browser
- **Streaming**: thinking streamed by editing one message in place, the answer through Telegram's streaming API (`sendRichMessageDraft`, native Stop button), rich messages (tables, task lists), markdown fallback
- **Topic flow**: one Telegram topic = one isolated session
- **Context management**: whole history sent, no message cap (newest 4 pictures only) - auto-compaction at 100% of the model's real context window, manual `/compact`
- **Usage accounting**: tokens and notional cost per session (`/cost`)
- **Go-first inference** with automatic fallback to Zen on provider errors

> *A small note from the human:*
> 
> *- Why I built this: I had a 1GB server lying around and it wouldn't run other harnesses without issues. So I built one for myself to use.*
> 
> *- This is 100% AI code. I don't have much experience doing software development apart from uni projects. Take every piece of code here with a grain of salt. Feel free to call it AI slop if you will.*

## Requirements

- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- **IMPORTANT**: An [opencode.ai](https://opencode.ai) API key (Zen and/or Go) (support for other providers to be added, soon)
- (Optional) [Lightpanda](https://lightpanda.io) for the `browse` tool

## Setup

```bash
git clone https://github.com/thanhvd1403/keirai.git
cd keirai
cp config.example.toml config.toml
# edit config.toml: bot_token, api keys, allowed_users
python main.py
```

Config highlights (`config.toml`, comments allowed — see
`config.example.toml` for everything):

| Key | Meaning |
| --- | --- |
| `bot_token` | from @BotFather (env override: `KEIRAI_BOT_TOKEN`) |
| `allowed_users` | list of user IDs/usernames, or `"all"` |
| `default_model` | e.g. `go/mimo-v2.6-flash` (Go tried first, Zen as fallback) |
| `tools_enabled` | master switch for the agent's tools |
| `topic_flow` | sessions live in Telegram topics (or toggle with `/topic`) |
| `rich_messages` / `stream_drafts` | rich rendering / live streaming, both auto-fall back |
| `stream_mode` | `"draft"` (default): the answer streams as a `sendRichMessageDraft` preview in private chats; `"edit"`: in-place edits everywhere |
| `exa_api_key` | optional, raises the free-tier limit of the Exa MCP web tools (env: `EXA_API_KEY`) |

API keys go in `[providers.zen]` / `[providers.go]` (env overrides:
`KEIRAI_ZEN_API_KEY`, `KEIRAI_GO_API_KEY`).

Optional browser tool:

```bash
bash deploy/install_lightpanda.sh   # installs into ./tools/lightpanda
```

## Usage

Talk to the bot; it replies with your session's history. Main commands
(`/help` always shows the live list):

| Command | What it does |
| --- | --- |
| `/new [name]` | start a new session (new topic in topic flow) |
| `/session [page N \| N \| <id>]` | list / switch sessions |
| `/rename <name>` | rename the current session/topic |
| `/delete` | delete this session's context |
| `/model provider/id` | switch model (`/models` to browse with context/cost stats) |
| `/context` | context window usage: used/max tokens + message count, threshold, tokens + cost |
| `/cost` | alias of `/context` - the same report |
| `/compact` | summarize older context now (also runs automatically) |
| `/stop` | interrupt the reply/tool currently running |
| `/restart` | restart keirai now (main chat only; the bot messages you when it is back) |
| `/thinking on\|off` | show/hide AI reasoning |
| `/topic` | switch between normal chat and topic flow |
| `/test_md`, `/test_rich` | check message rendering |

**Topic flow**: each Telegram topic becomes an isolated session. Requires
Threaded mode ON and "Disallow users to create topics" ON in @BotFather; the
bot verifies both before switching. `/session` switches between topics with
typed confirmations.

**Tools**: the model can read/write/edit files, run shell commands (with a
destructive-command blacklist and timeout), search and fetch the web, and
browse pages if Lightpanda is installed. Tool rounds run until the model
stops; `/stop` interrupts a runaway turn. Every round (input + results) is
kept in the session history, so the bot doesn't re-read the same file each
message; outputs over 40 KB spill to `sessions/<id>/tool_output/` - the
model sees a preview + path, history keeps only the path.

**Concurrency & steering**: every session/topic runs on its own worker - a
long turn in one topic never blocks another topic (commands included). A
message sent while your session is mid-turn joins the AI's context at the
next tool step, so you can steer a wrong direction without `/stop`. Each
round's reasoning is shown live as a collapsible block (`/thinking off`).

**Media**: send a photo to talk about it (vision models), or send media with
the caption `media_test` to verify the round-trip.

### CLI

Session admin without Telegram:

```bash
python cli.py list                    # stored sessions
python cli.py delete chat_id[:thread] # drop a session's context
python cli.py rename chat_id:thread "New name"
```

## Tests

```bash
python -m unittest discover -s tests
```

## Deployment

```bash
cd ~/keirai && git pull && sudo systemctl restart keirai
```

A systemd unit is provided in `deploy/keirai.service` (keep-alive +
autostart). Logs go to stderr and `logs/keirai.log` (rotating).

## Project layout

```
main.py        entry point: polling, commands, routing, tool loop, titles
config.py      TOML config + env overrides
telegram.py    raw Bot API client (HTTP, multipart, rich messages, edits)
md2tg.py       markdown -> Telegram HTML, message splitting
providers.py   OpenCode Zen/Go clients (blocking + streaming), pricing
sessions.py    SQLite store (history, models, topics, usage)
tools.py       LLM tool registry: files, shell, dispatch
websearch.py   Exa MCP client (web_search_exa / web_fetch_exa)
browser.py     Lightpanda wrapper (optional browse tool)
cli.py         session management CLI
tests/         unit tests (unittest, stdlib)
```

## License

Keirai is licensed under **AGPLv3** (see [LICENSE](LICENSE)).

Third-party components, and the notices their licenses require, are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Contributing

Contributions are welcome! Open an issue to discuss bigger changes, or send a
pull request for fixes and small features. Keep it lightweight: the project
runs on a 1GB server, so every dependency and every feature must justify its
memory/CPU cost (see `AGENTS.md` for the full rules).
