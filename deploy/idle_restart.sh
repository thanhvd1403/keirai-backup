#!/bin/sh
# Restart keirai.service once the chat log has been quiet long enough, so a
# code fix goes live without killing a running turn. Mirrors the original
# deploy watcher pattern (deploy.log: quiet>=1min, 30min budget) with a
# more conservative quiet window. Armed manually; exits after restarting.
#
# Repo root = parent of this script's deploy/ dir - derived, not hardcoded,
# so it works in any clone and carries no account name into the file.
BASE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 1
LOG=$BASE/logs/keirai.log
DEPLOY_LOG=$BASE/logs/deploy.log
QUIET=300   # seconds of log silence = no turn in flight (reasoning gaps run ~150s max)
BUDGET=1800 # give up after 30min of busy chat
start=$(date +%s)
while :; do
    now=$(date +%s)
    last=$(stat -c %Y "$LOG")
    idle=$((now - last))
    if [ "$idle" -ge "$QUIET" ]; then
        echo "$(date -u +%FT%TZ) idle-restart: log quiet ${idle}s -> systemctl restart keirai" >> "$DEPLOY_LOG"
        sudo -n systemctl restart keirai
        exit 0
    fi
    if [ $((now - start)) -ge "$BUDGET" ]; then
        echo "$(date -u +%FT%TZ) idle-restart: budget exhausted (chat busy), giving up" >> "$DEPLOY_LOG"
        exit 1
    fi
    sleep 30
done
