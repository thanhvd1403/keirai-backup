"""Telegram Bot API client - raw HTTPS calls, stdlib only.

Chosen over python-telegram-bot/aiogram: zero dependencies, minimal RAM,
no license questions. The Bot API is plain JSON over HTTPS.
"""
import http.client
import json
import os
import tempfile
import uuid
import urllib.error
import urllib.request

API_BASE = "https://api.telegram.org"
POLL_TIMEOUT = 25
# Bots can download files up to 20 MB.
MAX_DOWNLOAD = 20 * 1024 * 1024
# Bots can upload up to 50 MB; keep a margin.
MAX_UPLOAD = 45 * 1024 * 1024


class TelegramError(Exception):
    pass


def is_not_modified(exc):
    """True for Telegram's 'message is not modified' - the edit we sent was
    byte-identical to what the message already shows, i.e. a no-op, not a
    failure (it happens when a flush fires twice with the same content)."""
    return isinstance(exc, TelegramError) and "message is not modified" in str(exc)


class Telegram:
    def __init__(self, token):
        self.token = token
        self.base = "%s/bot%s" % (API_BASE, token)

    # ------------------------------------------------------------ core

    def call(self, method, payload=None, timeout=40):
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            "%s/%s" % (self.base, method),
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise TelegramError("%s -> HTTP %d: %s" % (method, e.code, body[:300]))
        except (urllib.error.URLError, OSError) as e:
            # URLError covers connect failures; raw OSError covers the
            # socket-level timeouts/resets that surface WHILE reading the
            # body (not wrapped in URLError) - both must become
            # TelegramError, or callers that only catch TelegramError leak
            # and kill their thread (the poll thread dying = bot silent)
            reason = getattr(e, "reason", None) or e
            raise TelegramError("%s -> %s" % (method, reason))
        except http.client.HTTPException as e:
            # truncated/aborted responses (IncompleteRead, RemoteDisconnected
            # on some paths) - same treatment
            raise TelegramError("%s -> %s" % (method, e))
        except ValueError as e:
            # truncated/blank body -> json.loads failure
            raise TelegramError("%s -> bad JSON: %s" % (method, e))
        if not isinstance(out, dict) or not out.get("ok"):
            raise TelegramError("%s -> %s" % (method, out))
        return out["result"]

    # ------------------------------------------------------------ updates

    def get_me(self):
        return self.call("getMe")

    def get_updates(self, offset=None, timeout=POLL_TIMEOUT):
        """Long poll. timeout=0 makes it a plain fetch-and-return - used once
        at startup so readiness is not delayed by the idle wait."""
        payload = {
            "timeout": timeout,
            # stopped_message_generation = the user pressed the native Stop
            # button Telegram shows under a streamed draft (can_stop)
            "allowed_updates": ["message", "stopped_message_generation"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=timeout + 15)

    # ------------------------------------------------------------ sending

    def send(self, chat_id, text, thread_id=None):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if thread_id:
            payload["message_thread_id"] = thread_id
        return self.call("sendMessage", payload)

    def send_rich(self, chat_id, markdown=None, html=None, thread_id=None):
        """Bot API 10.1+ rich message: headings, tables, task lists, details.
        Exactly one of markdown/html must be given. Supports 32768 chars."""
        rich = {"markdown": markdown} if markdown is not None else {"html": html}
        payload = {"chat_id": chat_id, "rich_message": rich}
        if thread_id:
            payload["message_thread_id"] = thread_id
        return self.call("sendRichMessage", payload, timeout=60)

    # NOTE: streaming uses sendRichMessageDraft (Bot API 10.1) - a preview
    # rendered IN THE CHAT. Its predecessor sendMessageDraft (9.3) lands in
    # the USER's composer and blocks them from typing and sending while the
    # bot generates (bugs.telegram.org/c/62189) - never call that one.

    def send_rich_draft(self, chat_id, draft_id, markdown=None, html=None,
                        thread_id=None, can_stop=False):
        """Bot API 10.1 sendRichMessageDraft: stream a partial rich message
        while it is being generated. The draft is an ephemeral ~30s preview
        (re-sending the same draft_id animates it in place) - the final
        message must still be sent with sendRichMessage. Private chats only.
        `can_stop=True` shows Telegram's own Stop button."""
        rich = {"markdown": markdown} if markdown is not None else {"html": html}
        payload = {"chat_id": chat_id, "draft_id": draft_id,
                   "rich_message": rich}
        if thread_id:
            payload["message_thread_id"] = thread_id
        if can_stop:
            payload["can_stop"] = True
        return self.call("sendRichMessageDraft", payload, timeout=30)

    def edit_rich(self, chat_id, message_id, markdown=None, html=None):
        """Bot API 10.1 live edit of a previously sent rich message
        (editMessageText with rich_message). Exactly one of markdown/html."""
        rich = {"markdown": markdown} if markdown is not None else {"html": html}
        payload = {"chat_id": chat_id, "message_id": message_id, "rich_message": rich}
        return self.call("editMessageText", payload, timeout=30)

    def delete_message(self, chat_id, message_id):
        # NOTE: the Bot API method is deleteMessage (camelCase) - a snake
        # case name is a 404 "Not Found", which silently broke every cleanup
        # that relied on it (stale live messages were never deleted)
        return self.call("deleteMessage", {"chat_id": chat_id,
                                           "message_id": message_id})

    def typing(self, chat_id, thread_id=None):
        payload = {"chat_id": chat_id, "action": "typing"}
        if thread_id:
            payload["message_thread_id"] = thread_id
        try:
            return self.call("sendChatAction", payload)
        except TelegramError:
            return None  # non-critical

    # ------------------------------------------------------------ topics
    # Works in forum supergroups and (Bot API 9.3+) in private chats when
    # topic mode is enabled for the bot via the "Threaded mode" setting in the
    # @BotFather Mini App (getMe.has_topics_enabled).

    def create_topic(self, chat_id, name):
        return self.call("createForumTopic", {"chat_id": chat_id, "name": name})

    def edit_topic(self, chat_id, thread_id, name):
        return self.call("editForumTopic", {
            "chat_id": chat_id, "message_thread_id": thread_id, "name": name,
        })

    def delete_topic(self, chat_id, thread_id):
        return self.call("deleteForumTopic", {
            "chat_id": chat_id, "message_thread_id": thread_id,
        })

    # ------------------------------------------------------------ media

    def get_file(self, file_id):
        return self.call("getFile", {"file_id": file_id})

    def download(self, tg_file, max_bytes=MAX_DOWNLOAD):
        """Download a file path from getFile() into a temp file. Returns path."""
        size = tg_file.get("file_size") or 0
        if size > max_bytes:
            raise TelegramError("file too large: %d bytes" % size)
        url = "%s/file/bot%s/%s" % (API_BASE, self.token, tg_file["file_path"])
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read(MAX_DOWNLOAD + 1)
        if len(data) > max_bytes:
            raise TelegramError("file too large")
        suffix = os.path.splitext(tg_file["file_path"])[1]
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="keirai_")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path

    def send_media(self, chat_id, kind, path, caption=None, thread_id=None):
        """Upload a local file as photo/document/audio/video/voice."""
        if kind not in ("photo", "document", "audio", "video", "voice"):
            raise TelegramError("unsupported media kind: %s" % kind)
        size = os.path.getsize(path)
        if size > MAX_UPLOAD:
            raise TelegramError("file too large to upload: %d bytes" % size)
        fields = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption[:1024]
            fields["parse_mode"] = "HTML"
        if thread_id:
            fields["message_thread_id"] = str(thread_id)
        ctype = _content_type(kind, path)
        boundary, body = _multipart(fields, kind, os.path.basename(path), path, ctype)
        req = urllib.request.Request(
            "%s/send%s" % (self.base, kind.capitalize()),
            data=body,
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                out = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise TelegramError("send%s -> HTTP %d: %s" % (kind.capitalize(), e.code, e.read().decode("utf-8", "replace")[:300]))
        if not out.get("ok"):
            raise TelegramError("send%s -> %s" % (kind.capitalize(), out))
        return out["result"]


def _content_type(kind, path):
    ext = os.path.splitext(path)[1].lower()
    table = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp",
        ".pdf": "application/pdf", ".zip": "application/zip",
        ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".oga": "audio/ogg",
        ".mp4": "video/mp4", ".webm": "video/webm",
        ".txt": "text/plain", ".md": "text/markdown", ".json": "application/json",
    }
    return table.get(ext, "application/octet-stream")


def _multipart(fields, file_field, filename, file_path, content_type):
    """Build a multipart/form-data body with one file from disk."""
    boundary = "----keirai" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                 % (boundary, k, v)).encode("utf-8")
    body += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
             'Content-Type: %s\r\n\r\n' % (boundary, file_field, filename, content_type)).encode("utf-8")
    with open(file_path, "rb") as f:
        body += f.read()
    body += ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    return boundary, bytes(body)
