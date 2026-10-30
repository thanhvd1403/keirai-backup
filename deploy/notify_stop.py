#!/usr/bin/env python3
"""systemd ExecStopPost: report keirai.service stopping, in the Telegram chat.

systemd runs this AFTER the main process is gone, so a bot that was OOM-killed,
SIGKILLed or segfaulted can still leave a last word. $SERVICE_RESULT says why.

Deliberately stdlib-only and imports nothing from the repo - the whole point is
to work when the bot's own code is the thing that broke.

Silent on "success" (clean restarts, idle_restart.sh). Rate-limited by a marker
file, so a crash loop pings the chat once instead of every 3 seconds.

Usage:
    notify_stop.py                 # called by systemd; reads $SERVICE_RESULT
    notify_stop.py --dry-run       # print instead of sending
    notify_stop.py --send          # send a real "test" message now
"""

import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # repo root (one up from deploy/)
CONFIG = Path(os.environ.get("KEIRAI_CONFIG") or BASE / "config.toml")
LOGS = BASE / "logs"
MARKER = LOGS / "keirai.notify.marker"
LASTSTOP = LOGS / "keirai.laststop.json"   # how the previous run ended
LOG = LOGS / "keirai.notify.log"

COOLDOWN = 1800        # seconds: repeat deaths within this window go to the log only
SEND_TIMEOUT = 15

# results that are ordinary operation - never worth pinging about
QUIET = {"success"}

# $SERVICE_RESULT -> human sentence
LABELS = {
    "oom-kill": "killed by the memory cap (out of memory)",
    "signal": "killed by a signal",
    "exit-code": "exited with an error",
    "core-dump": "crashed (core dump)",
    "watchdog": "watchdog timeout",
    "timeout": "stop timed out",
    "start-limit-hit": "systemd gave up restarting - it will NOT come back on its own",
    "reload": "reloaded",
    "non-zero-code": "exited with a non-zero code",
}


def stamp(line):
    """Append to the notify log; never raise."""
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), line))
    except OSError:
        pass


def read_config():
    """bot_token + notify chat. Only stdlib, no repo imports, no side effects."""
    try:
        with CONFIG.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        stamp("no config at %s" % CONFIG)
        return "", None
    except (tomllib.TOMLDecodeError, OSError) as exc:
        stamp("config unreadable: %s" % exc)
        return "", None

    token = os.environ.get("KEIRAI_BOT_TOKEN") or data.get("bot_token") or ""

    chat = data.get("notify_chat")
    if chat is None:
        # fall back to the first numeric entry of allowed_users (usernames can't receive)
        allowed = data.get("allowed_users", [])
        if isinstance(allowed, list):
            chat = next((u for u in allowed if isinstance(u, int)), None)
    return token, chat


def uptime_seconds(unit):
    """How long the dying invocation lived. systemd's monotonic stamp and
    time.monotonic() share CLOCK_MONOTONIC, so they subtract cleanly."""
    try:
        import subprocess
        out = subprocess.run(
            ["systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "--value", unit],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        started = int(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    secs = int(time.monotonic() - started / 1_000_000)
    return secs if 0 <= secs < 86400 * 30 else None


def human_uptime(secs):
    if secs is None:
        return "unknown uptime"
    if secs < 60:
        return "up %ds" % secs
    if secs < 3600:
        return "up %dm" % (secs // 60)
    return "up %dh%02dm" % (secs // 3600, (secs % 3600) // 60)


def send(token, chat, text):
    """One sendMessage call. Returns True on success; never raises."""
    body = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % token, data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode() or "{}")
        return bool(payload.get("ok"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        stamp("telegram send failed: %s" % exc)
        return False


def build_message(result, uptime, unit):
    """Return the chat message, or None for a stop nobody needs to hear."""
    if result in QUIET:
        return None
    reason = LABELS.get(result, result or "unknown reason")

    if result == "start-limit-hit":
        icon, tail = "⚫", "manual: sudo systemctl restart %s" % unit
    else:
        icon, tail = "🔴", "systemd is restarting it"

    lines = ["%s %s stopped (%s)" % (icon, unit, result), reason,
             "%s - %s" % (human_uptime(uptime), tail)]
    return "\n".join(lines)


def record_result(result, uptime):
    """Remember how this run ended. notify_start reads it: a clean stop means
    the next start was a deliberate restart (announce it), while a crash
    means it may be a loop (rate-limit it)."""
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        LASTSTOP.write_text(json.dumps({"result": result, "uptime": uptime,
                                        "at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def recently_notified():
    try:
        age = time.time() - MARKER.stat().st_mtime
        return age < COOLDOWN, int(age)
    except OSError:
        return False, None


def touch_marker():
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        MARKER.touch()
    except OSError:
        pass


def main(argv):
    dry_run = "--dry-run" in argv
    test_send = "--send" in argv

    if test_send:
        result, uptime = "test", None
        text = ("✅ keirai stop-notify test\n"
                "ExecStopPost is wired up - future crashes will report here.")
    else:
        # systemd sets SERVICE_RESULT ($UNIT is NOT exported to ExecStopPost on
        # systemd 239, hence the fallback - correct, since only keirai uses this).
        # argv overrides, so this is testable by hand.
        result = next((a for a in argv if not a.startswith("-")), None) \
            or os.environ.get("SERVICE_RESULT") or "unknown"
        unit = os.environ.get("UNIT") or "keirai.service"
        uptime = uptime_seconds(unit)
        text = build_message(result, uptime, unit)

    if not dry_run:  # a dry run must not rewrite how the last real stop ended
        record_result(result, uptime)

    if text is None:
        stamp("quiet stop (result=%s) - no message" % result)
        return 0

    if not dry_run and not test_send:
        blocked, age = recently_notified()
        if blocked:
            # crash loop: log it, keep the chat clean
            stamp("suppressed repeat (result=%s, last notify %ds ago)" % (result, age))
            return 0

    if dry_run:
        print("[dry-run] result=%s uptime=%s\n%s" % (result, uptime, text))
        return 0

    token, chat = read_config()
    if not token or chat is None:
        stamp("no bot_token/notify_chat in %s - message not sent" % CONFIG)
        return 0

    if send(token, chat, text):
        touch_marker()
        stamp("sent (result=%s)" % result)
    else:
        stamp("send failed (result=%s)" % result)
    return 0


if __name__ == "__main__":
    # always exit 0: a failing notifier must never mark the unit failed
    sys.exit(main(sys.argv[1:]))
