"""Keirai session management CLI - works without (and alongside) the bot.

  python cli.py list                          # all stored sessions
  python cli.py delete <chat>[:<thread|main>] # clear one session's context
  python cli.py rename <chat>[:<thread|main>] <name>
  python cli.py archive <chat>[:<thread|main>]  # what compaction folded away
  python cli.py archive <chat>[:<thread|main>] --json  # ... as JSON

<chat> is the Telegram chat id, <thread> the topic id (omit or 'main' for
the main chat). Renames the stored session/topic name.
"""
import argparse
import datetime
import sys

import config as config_mod
from sessions import Store


def parse_key(key):
    """'123' / '123:456' / '123:main' -> (chat_id, thread_id_or_None)."""
    if ":" in key:
        chat, thread = key.split(":", 1)
        thread = None if thread.lower() in ("main", "0", "") else int(thread)
        return int(chat), thread
    return int(key), None


def _names(store):
    return {(chat_id, thread_id): name
            for chat_id, thread_id, name in store.load_topics()}


def cmd_list(store, args):
    rows = sorted(store.list_sessions(), key=lambda r: -r[4])
    if not rows:
        print("no sessions stored")
        return 0
    names = _names(store)
    for chat_id, thread_id, model, messages, updated in rows:
        key = (chat_id, thread_id)
        when = datetime.datetime.fromtimestamp(updated).strftime("%Y-%m-%d %H:%M")
        # arch= is everything compaction folded away and kept on disk: still
        # there, never sent to the model, readable with `cli.py archive`.
        print("chat=%s thread=%s name=%-20s msgs=%-3d arch=%-4d model=%-24s last=%s"
              % (chat_id, "main" if thread_id == 0 else thread_id,
                 names.get(key) or "-", len(messages),
                 store.archive_count(chat_id, thread_id),
                 model or "-", when))
    return 0


def cmd_delete(store, args):
    chat_id, thread_id = parse_key(args.key)
    rows = [r for r in store.list_sessions()
            if r[0] == chat_id and r[1] == (thread_id or 0)]
    if not rows:
        print("no such session: %s" % args.key)
        return 1
    store.delete_session(chat_id, thread_id or 0)
    print("deleted context of chat=%s thread=%s"
          % (chat_id, thread_id or "main"))
    print("note: the Telegram topic (if any) is not touched - delete it in Telegram")
    return 0


def cmd_archive(store, args):
    """Print what a compaction replaced - the raw history under the window."""
    import json
    chat_id, thread_id = parse_key(args.key)
    rows = store.load_archived(chat_id, thread_id or 0)
    if not rows:
        print("nothing archived for %s (no compaction has run, or it was "
              "written before append-only storage)" % args.key)
        return 0
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    for i, m in enumerate(rows):
        text = m.get("content")
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        print("%4d %-9s %s" % (i, m.get("role", "?"),
                               (text or "").replace("\n", " ")[:160]))
    print("-- %d archived messages (raw; the model never sees these again "
          "unless the session is rebuilt from them)" % len(rows))
    return 0


def cmd_rename(store, args):
    chat_id, thread_id = parse_key(args.key)
    if thread_id is None:
        print("main chat has no topic to rename; use a topic key like 123:456")
        return 1
    store.set_topic_name(chat_id, thread_id, args.name)
    print("renamed session chat=%s thread=%s -> %r" % (chat_id, thread_id, args.name))
    return 0


def run(argv=None):
    parser = argparse.ArgumentParser(prog="keirai-cli", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list stored sessions")
    p_del = sub.add_parser("delete", help="delete one session's context")
    p_del.add_argument("key", help="chat_id[:thread_id|main]")
    p_arc = sub.add_parser("archive", help="show what compaction folded away")
    p_arc.add_argument("key", help="chat_id[:thread_id|main]")
    p_arc.add_argument("--json", action="store_true", help="dump as JSON")
    p_ren = sub.add_parser("rename", help="rename a session/topic")
    p_ren.add_argument("key", help="chat_id:thread_id")
    p_ren.add_argument("name")
    args = parser.parse_args(argv)

    cfg = config_mod.load()
    store = Store(config_mod.sessions_path(cfg))
    try:
        handler = {"list": cmd_list, "delete": cmd_delete,
                   "rename": cmd_rename, "archive": cmd_archive}[args.cmd]
        return handler(store, args)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(run())
