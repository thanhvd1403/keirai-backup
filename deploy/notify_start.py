#!/usr/bin/env python3
"""systemd ExecStartPost: say "I'm back" in the Telegram chat, once the bot
actually answers.

ExecStartPost fires the moment the unit starts - before the process has
imported anything, before Telegram long-polling is up. A message sent then
would be lying, and "the process exists" / "getMe works" are both lies of
that kind: the first says nothing about this bot, the second only proves the
token is valid.

Readiness is therefore taken from the harness itself: main.py touches
logs/keirai.ready after its first successful getUpdates. This waits for that
file, freshly written since this start, and only then sends. Silent when it
cannot confirm: an unconfirmed "I'm back" is worse than none.

Detached (`--detach`, what the unit uses): the helper double-forks, so
systemd does not wait on a 25s poll and the process is not killed by the
next stop. Notify machinery that dies with the thing it reports on is
useless.

Deliberately stdlib-only and importing nothing from the repo: the point is
to work when the bot's own code is what broke. Shares the sender and config
reader with notify_stop.py, which has the same constraint.

Usage:
    notify_start.py --detach      # called by systemd (returns immediately)
    notify_start.py --dry-run     # print what would be sent, send nothing
    notify_start.py --send        # send a real "test" message now
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from notify_stop import BASE, LOGS, read_config, send, stamp  # noqa: E402
from notify_stop import LASTSTOP, QUIET  # noqa: E402

UNIT = os.environ.get("UNIT") or "keirai.service"
READY_TIMEOUT = 25      # seconds to wait for the harness to report ready
POLL = 0.4
COOLDOWN = 60           # seconds: a crash loop announces once, not every 3s
MARKER = LOGS / "keirai.started.marker"
READY = LOGS / "keirai.ready"   # touched by main.py once polling works


def _run(args, timeout=10):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def main_pid(unit):
    out = _run(["systemctl", "show", "-p", "MainPID", "--value", unit])
    try:
        pid = int(out)
    except ValueError:
        return None
    return pid or None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ready_since(moment):
    """True when the harness has written its ready file since `moment`."""
    try:
        return READY.stat().st_mtime >= moment - 1.0
    except OSError:
        return False


def wait_ready(timeout=READY_TIMEOUT):
    """Seconds it took for the harness to report itself ready, or None."""
    started = time.time()
    while time.time() - started < timeout:
        if _run(["systemctl", "is-active", UNIT]) == "active" and ready_since(started):
            return time.time() - started
        time.sleep(POLL)
    return None


def head_sha():
    out = _run(["git", "-C", str(BASE), "rev-parse", "--short", "HEAD"], timeout=5)
    return out if len(out) == 7 and " " not in out else None


def build_message(ready_in, pid, head):
    lines = ["✅ %s is up" % UNIT]
    bits = []
    if pid:
        bits.append("pid %d" % pid)
    bits.append("receiving messages after %.1fs" % ready_in)
    if head:
        bits.append(head)
    lines.append(" · ".join(bits))
    return "\n".join(lines)


def last_stop_was_clean():
    """True when the previous run was stopped on purpose (systemctl restart,
    a clean exit). Such a start is a deliberate restart and must always be
    announced - the cooldown exists for crash loops, and a manual restart was
    being swallowed by it."""
    try:
        data = json.loads(LASTSTOP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True   # no history: assume a real start, not a loop
    return data.get("result") in QUIET


def recently_announced():
    try:
        return (time.time() - MARKER.stat().st_mtime) < COOLDOWN
    except OSError:
        return False


def touch_marker():
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        MARKER.touch()
    except OSError:
        pass


def detach():
    """Double fork: systemd's ExecStartPost returns at once, and the helper
    survives the unit being stopped underneath it."""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)


def main(argv):
    dry_run = "--dry-run" in argv
    test_send = "--send" in argv

    if "--detach" in argv and not dry_run:
        detach()

    token, chat = read_config()

    if test_send:
        ready_in, pid, head = 0.0, main_pid(UNIT), head_sha()
        text = "✅ keirai start-notify test\n" + build_message(ready_in, pid, head).split("\n", 1)[1]
        return _deliver(token, chat, text, dry_run=False, marker=False)

    ready_in = wait_ready()
    if ready_in is None:
        stamp("harness not ready within %ds (no fresh %s) - no start message"
              % (READY_TIMEOUT, READY.name))
        return 0

    text = build_message(ready_in, main_pid(UNIT), head_sha())
    if dry_run:
        print("[dry-run] ready in %.1fs\n%s" % (ready_in, text))
        return 0
    return _deliver(token, chat, text, dry_run=False, marker=True)


def _deliver(token, chat, text, dry_run, marker):
    if dry_run:
        print(text)
        return 0
    if marker and recently_announced() and not last_stop_was_clean():
        stamp("start message suppressed (announced < %ds ago)" % COOLDOWN)
        return 0
    if not token or chat is None:
        stamp("no bot_token/notify_chat - start message not sent")
        return 0
    if send(token, chat, text):
        if marker:
            touch_marker()
        stamp("start message sent")
    else:
        stamp("start message failed")
    return 0


if __name__ == "__main__":
    # never fail the unit: a broken notifier must not stop the bot starting
    sys.exit(main(sys.argv[1:]))
