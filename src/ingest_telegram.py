"""Telegram Bot API ingestion (long-polling).

Reads new messages via getUpdates with a persisted offset (restart-safe:
offset lives in the DB's ingest_state table). Stores raw messages, then runs
extract.py on each text and returns the candidate list for downstream
classification.

Auto-discovery: getUpdates delivers updates from EVERY chat the bot is a
member of -- no per-chat subscription exists. Every chat seen (including
`my_chat_member` join events) is recorded in known_chats, so newly joined
groups start flowing with zero config.

Fail-open: network/API errors are logged and return 0 new messages; the
offset is only advanced past successfully processed updates.

Requires: TELEGRAM_BOT_TOKEN env. Watched chats are informational here --
the Bot API only delivers updates for chats the bot is a member of.
"""

import sys
import time

import requests

from . import db as dbmod
from . import discover as discovermod
from . import extract as extractmod

API = "https://api.telegram.org/bot{token}/{method}"
OFFSET_KEY = "telegram:offset"
TIMEOUT = 20  # long-poll seconds


def _msg_text(msg):
    return msg.get("text") or msg.get("caption") or ""


def _parse_update(upd):
    """Return (chat_id, msg_id, user_id, username, text, ts, reply) or None.

    reply is None or a dict with the replied-to message's msg_id, text,
    user_id, username (Telegram embeds the original message in the update).
    """
    msg = (upd.get("message") or upd.get("edited_message")
           or upd.get("channel_post") or upd.get("edited_channel_post"))
    if not msg:
        return None
    text = _msg_text(msg)
    if not text:
        return None
    chat = msg.get("chat", {})
    frm = msg.get("from") or {}
    username = frm.get("username") or ""
    if not username and msg.get("sender_chat"):
        username = msg["sender_chat"].get("title", "")
    reply = None
    rm = msg.get("reply_to_message")
    if isinstance(rm, dict):
        rfrm = rm.get("from") or {}
        rtext = _msg_text(rm)
        if rtext:
            reply = {
                "msg_id": str(rm.get("message_id", "")),
                "text": rtext,
                "user_id": str(rfrm.get("id", "")),
                "username": rfrm.get("username") or "",
            }
    return (
        str(chat.get("id", "")),
        str(msg.get("message_id", "")),
        str(frm.get("id", "")),
        username,
        text,
        int(msg.get("date", time.time())),
        reply,
    )


def _update_chat(upd):
    """(chat_id, title) for any update type we track, or (None, None)."""
    for key in ("message", "edited_message", "channel_post",
                "edited_channel_post", "my_chat_member"):
        inner = upd.get(key)
        if isinstance(inner, dict):
            chat = inner.get("chat", {})
            if chat.get("id") is not None:
                return str(chat["id"]), chat.get("title", "")
    return None, None


def poll_once(cfg, database: dbmod.Database, timeout=TIMEOUT):
    """One getUpdates round. Returns (messages_stored, candidates, new_chats, nominations).

    new_chats is a list of (chat_id, title) first seen this round.

    Auth: prefers the Secure Vault surrogate (custom.telegram-signal-bot);
    falls back to TELEGRAM_BOT_TOKEN env for local testing.
    """
    url = None
    try:
        from . import creds as credsmod
        url = credsmod.telegram_bot_url("https://api.telegram.org/bot{}/getUpdates")
    except Exception as e:
        if cfg.telegram_bot_token:
            url = API.format(token=cfg.telegram_bot_token, method="getUpdates")
        else:
            print(f"telegram: no credential available ({e}), skipping",
                  file=sys.stderr)
            return 0, [], [], []
    offset = int(database.get_state(OFFSET_KEY, "0") or "0")
    try:
        r = requests.get(
            url,
            params={"offset": offset, "timeout": timeout,
                    "allowed_updates": ["message", "edited_message",
                                        "channel_post", "edited_channel_post",
                                        "my_chat_member"]},
            timeout=timeout + 10,
            headers={"User-Agent": "fren-signal-trading/1.0"},
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # fail-open: keep old offset, try again next poll
        print(f"telegram: getUpdates failed (fail-open): {e}", file=sys.stderr)
        return 0, [], [], []
    if not data.get("ok"):
        print(f"telegram: API error (fail-open): {data}", file=sys.stderr)
        return 0, [], [], []

    stored = 0
    candidates = []
    new_chats = []
    nominations = []
    # Seed configured chat IDs so the registry is complete even for
    # quiet chats with no recent updates.
    for cid in (cfg.telegram_chat_ids or []):
        is_new, _ = discovermod.record_chat(database, "telegram", cid, cid)
        if is_new:
            new_chats.append((cid, cid))
    max_id = offset - 1
    for upd in data.get("result", []):
        max_id = max(max_id, upd.get("update_id", 0))
        # Auto-discovery: record every chat the bot appears in, even
        # join events with no message text.
        chat_id, title = _update_chat(upd)
        if chat_id:
            is_new, _ = discovermod.record_chat(database, "telegram",
                                                chat_id, title or chat_id)
            if is_new:
                new_chats.append((chat_id, title or chat_id))
        parsed = _parse_update(upd)
        if not parsed:
            continue
        chat_id, msg_id, user_id, username, text, ts, reply = parsed
        mid = database.insert_message("telegram", chat_id, msg_id,
                                      user_id, username, text, ts)
        stored += 1
        if reply:
            from . import theses as thesesmod
            is_nom, direction = thesesmod.parse_nomination(text)
            if is_nom:
                nominations.append({
                    "platform": "telegram",
                    "chat_id": chat_id,
                    "reply_msg_id": mid,
                    "reply_text": text,
                    "nominator_user_id": user_id,
                    "nominator_username": username,
                    "direction": direction,
                    "orig_text": reply["text"],
                    "orig_user_id": reply["user_id"],
                    "orig_username": reply["username"],
                    "ts": ts,
                })
        for cand in extractmod.extract_candidates(text):
            candidates.append({
                "platform": "telegram",
                "message_id": mid, "chat_id": chat_id,
                "user_id": user_id, "username": username, "ts": ts,
                **cand,
            })
    database.set_state(OFFSET_KEY, str(max_id + 1))
    return stored, candidates, new_chats, nominations


def main():
    from . import config as cfgmod
    cfg = cfgmod.load()
    database = dbmod.Database(cfg.db_path)
    stored, cands, new_chats, noms = poll_once(cfg, database)
    print(f"telegram: stored={stored} candidates={len(cands)} nominations={len(noms)}")
    for cid, title in new_chats:
        print(f"telegram: NEW CHAT discovered: {title} ({cid})")
    database.close()


if __name__ == "__main__":
    main()
