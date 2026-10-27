"""Keirai configuration: TOML file + environment variable overrides.

Requires Python 3.11+ (tomllib is in the stdlib).
"""
import os
import re
import threading

try:
    import tomllib
except ImportError:  # pragma: no cover
    raise SystemExit("Keirai requires Python 3.11+ (tomllib). Found an older Python.")

CONFIG_PATHS = ["config.toml"]
PROVIDER_ENV_KEYS = {
    "zen": ["KEIRAI_ZEN_API_KEY", "OPENCODE_API_KEY"],
    "go": ["KEIRAI_GO_API_KEY", "OPENCODE_API_KEY"],
}


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, data, path=None):
        self.path = path
        self.bot_token = os.environ.get("KEIRAI_BOT_TOKEN") or data.get("bot_token") or ""
        allowed = data.get("allowed_users", "all")
        if isinstance(allowed, str) and allowed.strip().lower() == "all":
            self.allowed_users = "all"
        elif isinstance(allowed, list):
            self.allowed_users = [str(u).lstrip("@").lower() for u in allowed]
        elif isinstance(allowed, (str, int)):  # single ID/username, no list
            self.allowed_users = [str(allowed).lstrip("@").lower()]
        else:
            self.allowed_users = []
        self.thinking_default = bool(data.get("thinking_default", True))
        self.rich_messages = bool(data.get("rich_messages", True))
        self.stream_drafts = bool(data.get("stream_drafts", True))
        # auto-compact threshold, in rough tokens (was context_limit_chars -
        # the whole report moved to tokens)
        self.context_limit_tokens = int(data.get("context_limit_tokens", 30_000))
        # explicit only when the user actually set it in config.toml - the
        # default threshold is 100% of the model's context window (models.dev)
        self.context_limit_explicit = "context_limit_tokens" in data
        self.default_model = data.get("default_model", "go/mimo-v2.6-flash")
        self.topic_flow = bool(data.get("topic_flow", False))
        self.max_file_mb = int(data.get("max_file_mb", 20))
        self.tools_enabled = bool(data.get("tools_enabled", True))
        self.bash_timeout = int(data.get("bash_timeout", 30))
        self.tool_output_max_bytes = int(data.get("tool_output_max_bytes", 40_000))
        self.exa_api_key = (os.environ.get("EXA_API_KEY")
                            or data.get("exa_api_key", ""))
        # how the answer is streamed while it is generated:
        #   draft - Bot API 10.1 sendRichMessageDraft (real-time animated
        #           preview in the chat, private chats; edits elsewhere)
        #   edit  - legacy in-place edits of a placeholder message
        mode = str(data.get("stream_mode", "draft")).strip().lower()
        self.stream_mode = mode if mode in ("draft", "edit") else "draft"
        providers = data.get("providers", {}) or {}
        self.providers = {}
        for name, env_keys in PROVIDER_ENV_KEYS.items():
            pconf = providers.get(name, {}) or {}
            key = pconf.get("api_key") or ""
            for env in env_keys:
                if os.environ.get(env):
                    key = os.environ[env]
                    break
            self.providers[name] = {"api_key": key}

    def is_allowed(self, user_id, username=None):
        """True if the user may talk to the bot."""
        if self.allowed_users == "all":
            return True
        uid = str(user_id)
        if uid in self.allowed_users:
            return True
        if username and username.lower() in self.allowed_users:
            return True
        return False

    def provider_key(self, name):
        p = self.providers.get(name)
        return p["api_key"] if p else ""

    def validate(self):
        if not self.bot_token:
            raise ConfigError(
                "bot_token is missing. Set it in config.toml or via the KEIRAI_BOT_TOKEN env var."
            )


def load(path=None):
    """Load config from `path`, the KEIRAI_CONFIG env var, or ./config.toml."""
    candidates = ([path] if path
                  else ([os.environ["KEIRAI_CONFIG"]] if os.environ.get("KEIRAI_CONFIG") else [])
                  + CONFIG_PATHS)
    for cand in candidates:
        if cand and os.path.isfile(cand):
            try:
                with open(cand, "rb") as f:
                    return Config(tomllib.load(f), path=cand)
            except tomllib.TOMLDecodeError as e:
                raise ConfigError("%s is not valid TOML: %s" % (cand, e))
    return Config({}, path=None)


_write_lock = threading.Lock()  # read-modify-write from concurrent workers


def write_value(cfg, key, value):
    """Surgically set a top-level `key = value` in the config file while
    keeping every comment and section (used for `topic_flow` and
    `/model global`). Returns the rendered literal, or None when there is no
    config file (env-only setup -> caller falls back to in-memory)."""
    if not cfg.path or not os.path.isfile(cfg.path):
        return None
    if isinstance(value, bool):
        lit = "true" if value else "false"
    elif isinstance(value, str):
        lit = '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
    else:
        lit = str(value)
    with _write_lock:
        with open(cfg.path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        pattern = re.compile(r"^(\s*)%s\s*=.*$" % re.escape(key))
        for i, line in enumerate(lines):
            m = pattern.match(line)
            if m:
                lines[i] = "%s%s = %s" % (m.group(1), key, lit)
                break
        else:
            # not present -> insert before the first section header (top level)
            at = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")),
                      len(lines))
            lines.insert(at, "%s = %s" % (key, lit))
        with open(cfg.path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return lit


def base_dir(config):
    """Directory of the config file (or cwd) - project root for keirai paths."""
    return os.path.dirname(os.path.abspath(config.path)) if config.path else os.getcwd()


def cache_dir(config):
    """Cache directory next to the config file (or ./cache)."""
    d = os.path.join(base_dir(config), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def sessions_path(config):
    """SQLite session db path next to the config file (or ./sessions.db)."""
    return os.path.join(base_dir(config), "sessions.db")


SYSTEM_FILE = "SYSTEM.md"

# Used when SYSTEM.md is missing or blank - it is gitignored, so a fresh clone
# has none. Kept in code so the bot always has a working prompt.
DEFAULT_SYSTEM_PROMPT = ("You are Keirai, a lightweight AI agent running on a "
                         "small server. Answer using markdown.")


def system_prompt(config):
    """The system prompt for this request: SYSTEM.md next to the config file.

    Read on EVERY call and never cached, deliberately: an edit to SYSTEM.md
    must be visible to the very next user message, not from the next process
    restart. The file is not tracked by git - it holds the local prompt - so a
    missing or blank one falls back to DEFAULT_SYSTEM_PROMPT above.
    """
    try:
        with open(os.path.join(base_dir(config), SYSTEM_FILE),
                  encoding="utf-8") as fh:
            text = fh.read().strip()
        if text:
            return text
    except OSError:
        pass
    return DEFAULT_SYSTEM_PROMPT
