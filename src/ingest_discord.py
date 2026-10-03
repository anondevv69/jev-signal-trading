"""Discord REST ingestion (polling, no gateway needed).

For each watched channel: GET /channels/{id}/messages?limit=100&after={last_id}
with Bot-token auth. Per-channel last_id persists in ingest_state
(restart-safe). Stores raw messages, runs extract.py, returns candidates.

Fail-open: HTTP errors / 429s are logged and the channel is skipped; last_id
only advances past successfully stored messages.

Requires: DISCORD_BOT_TOKEN env, DISCORD_CHANNEL_IDS env (comma-separated).
The bot must be invited to each server with read-message + message-content intent.
"""

import sys
import time
from datetime import datetime, timezone

import requests

from . import db as dbmod
from . import discover as discovermod
from . import extract as extractmod

API = "https://discord.com/api/v10"


def _parse_ts(iso):
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00"))
                   .replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return int(time.time())


def poll_channel(auth_headers, channel_id, database: dbmod.Database):
    """Poll one channel. Returns (stored, candidates, nominations)."""
    key = f"discord:last_id:{channel_id}"
    last_id = database.get_state(key, "0") or "0"
    try:
        r = requests.get(
            f"{API}/channels/{channel_id}/messages",
            headers={**auth_headers, "User-Agent": "fren-signal-trading/1.0"},
            params={"limit": 100, "after": last_id},
            timeout=20,
        )
    except Exception as e:
        print(f"discord:{channel_id}: request failed (fail-open): {e}",
              file=sys.stderr)
        return 0, [], [], []
    if r.status_code == 429:
        retry = r.headers.get("Retry-After", "?")
        print(f"discord:{channel_id}: rate-limited, retry after {retry}s "
              f"(fail-open)", file=sys.stderr)
        return 0, [], [], []
    if r.status_code == 403:
        print(f"discord:{channel_id}: 403 - bot lacks access to this channel "
              f"(fail-open, skipping 24h)", file=sys.stderr)
        try:
            discovermod.mark_denied(database, channel_id)
        except Exception:
            pass
        return 0, [], [], []
    if not r.ok:
        print(f"discord:{channel_id}: HTTP {r.status_code} (fail-open)",
              file=sys.stderr)
        return 0, [], [], []

    stored = 0
    candidates = []
    nominations = []
    max_id = int(last_id)
    for msg in reversed(r.json()):  # oldest first
        mid_int = int(msg["id"])
        max_id = max(max_id, mid_int)
        author = msg.get("author", {})
        text = msg.get("content", "")
        if not text:
            continue
        db_id = database.insert_message(
            "discord", channel_id, msg["id"],
            author.get("id", ""), author.get("username", ""),
            text, _parse_ts(msg.get("timestamp", "")),
        )
        stored += 1
        ref = msg.get("referenced_message")
        if isinstance(ref, dict) and ref.get("content"):
            from . import theses as thesesmod
            is_nom, direction = thesesmod.parse_nomination(text)
            if is_nom:
                rauthor = ref.get("author", {})
                nominations.append({
                    "platform": "discord",
                    "chat_id": channel_id,
                    "reply_msg_id": db_id,
                    "nominator_user_id": author.get("id", ""),
                    "nominator_username": author.get("username", ""),
                    "direction": direction,
                    "orig_text": ref.get("content", ""),
                    "orig_user_id": rauthor.get("id", ""),
                    "orig_username": rauthor.get("username", ""),
                    "ts": _parse_ts(msg.get("timestamp", "")),
                })
        for cand in extractmod.extract_candidates(text):
            candidates.append({
                "platform": "discord",
                "message_id": db_id, "chat_id": channel_id,
                "user_id": author.get("id", ""),
                "username": author.get("username", ""),
                "ts": _parse_ts(msg.get("timestamp", "")),
                **cand,
            })
    if max_id > int(last_id):
        database.set_state(key, str(max_id))
    return stored, candidates, nominations


def poll_once(cfg, database: dbmod.Database):
    """Poll all watched channels. Returns (stored, candidates, new_chats, nominations).

    Channel set = DISCORD_CHANNEL_IDS (explicit) UNION every readable
    text channel in every guild the bot is in (auto-discovered, cached
    hourly). new_chats lists (channel_id, label) first seen this round.

    Auth: prefers the Secure Vault surrogate (custom.discord-signal-bot);
    falls back to DISCORD_BOT_TOKEN env for local testing.
    """
    auth_headers = None
    try:
        from . import creds as credsmod
        auth_headers = credsmod.discord_auth_headers()
    except Exception as e:
        if cfg.discord_bot_token:
            auth_headers = {"Authorization": f"Bot {cfg.discord_bot_token}"}
        else:
            print(f"discord: no credential available ({e}), skipping",
                  file=sys.stderr)
            return 0, [], [], []
    # Auto-discovery: every text channel in every guild the bot is in.
    # Explicit DISCORD_CHANNEL_IDS are kept as well (union).
    channels, new_chats = discovermod.discover_discord_channels(
        auth_headers, database)
    seen = {cid for cid, _ in channels}
    for ch in (cfg.discord_channel_ids or []):
        if ch not in seen:
            is_new, _ = discovermod.record_chat(database, "discord", ch,
                                                f"configured:{ch}")
            channels.append((ch, f"configured:{ch}"))
            if is_new:
                new_chats.append((ch, f"configured:{ch}"))
    if not channels:
        print("discord: no channels (not in any guild / all denied), "
              "skipping", file=sys.stderr)
        return 0, [], [], []
    total_stored, all_cands, all_noms = 0, [], []
    for ch, _label in channels:
        stored, cands, noms = poll_channel(auth_headers, ch, database)
        total_stored += stored
        all_cands.extend(cands)
        all_noms.extend(noms)
    return total_stored, all_cands, new_chats, all_noms


def main():
    from . import config as cfgmod
    cfg = cfgmod.load()
    database = dbmod.Database(cfg.db_path)
    stored, cands, new_chats, noms = poll_once(cfg, database)
    print(f"discord: stored={stored} candidates={len(cands)} nominations={len(noms)}")
    for cid, label in new_chats:
        print(f"discord: NEW CHANNEL discovered: {label} ({cid})")
    database.close()


if __name__ == "__main__":
    main()
