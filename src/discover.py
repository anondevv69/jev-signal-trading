"""Auto-discovery of chats the bots are members of.

Telegram: getUpdates already delivers updates from EVERY chat the bot is
in -- there is no per-chat subscription. Discovery there is just
recording every chat_id seen (ingest_telegram does this inline).

Discord: the bot must enumerate its own guilds and channels --
GET /users/@me/guilds then GET /guilds/{id}/channels -- since polling
is per-channel. Results are cached hourly in ingest_state; channels
that 403 are skipped for 24h to avoid log spam.

Fail-open throughout: discovery problems never break ingestion.
"""

import sys
import time

import requests

API = "https://discord.com/api/v10"
UA = {"User-Agent": "fren-signal-trading/1.0"}

# Discord channel types we read: 0 = text, 5 = announcement.
READABLE_TYPES = {0, 5}
CACHE_TTL = 3600
DENY_TTL = 86400


def record_chat(database, platform, chat_id, title=""):
    """Upsert a chat into known_chats. Returns (is_new, title)."""
    is_new = database.upsert_chat(platform, chat_id, title or "")
    return is_new, title or ""


def _get(auth_headers, path, timeout=20):
    r = requests.get(f"{API}{path}", headers={**auth_headers, **UA},
                     timeout=timeout)
    if r.status_code == 429:
        raise RuntimeError(f"discord rate-limited on {path}")
    r.raise_for_status()
    return r.json()


def discover_discord_channels(auth_headers, database):
    """Return [(channel_id, label)] for all readable text channels in all
    guilds the bot is in, plus any previously known channels. Newly
    found channels are returned in `new`. Fail-open: [] on any error.
    """
    now = int(time.time())
    try:
        cached_at = int(database.get_state("discord:discovered_at", "0") or "0")
    except Exception:
        cached_at = 0
    channels = {}  # channel_id -> label
    new = []

    def _remember(cid, label):
        if cid in channels:
            return
        channels[cid] = label
        is_new, _ = record_chat(database, "discord", cid, label)
        if is_new:
            new.append((cid, label))

    # Previously known channels always stay in the poll set.
    for cid, title in database.list_chats("discord"):
        denied_until = database.get_state(f"discord:denied_until:{cid}", "0") or "0"
        try:
            if int(denied_until) > now:
                continue
        except Exception:
            pass
        channels[cid] = title or cid

    # Fresh guild scan at most hourly.
    if now - cached_at < CACHE_TTL:
        return list(channels.items()), new
    try:
        guilds = _get(auth_headers, "/users/@me/guilds")
        for g in guilds:
            gid, gname = str(g.get("id", "")), g.get("name", "")
            if not gid:
                continue
            try:
                chans = _get(auth_headers, f"/guilds/{gid}/channels")
            except Exception as e:
                print(f"discover: guild {gname} channels failed: {e}",
                      file=sys.stderr)
                continue
            for c in chans:
                if c.get("type") not in READABLE_TYPES:
                    continue
                cid = str(c.get("id", ""))
                if not cid:
                    continue
                _remember(cid, f"{gname} #{c.get('name', '')}")
        database.set_state("discord:discovered_at", str(now))
    except Exception as e:
        print(f"discover: discord guild scan failed (fail-open): {e}",
              file=sys.stderr)
    return list(channels.items()), new


def mark_denied(database, channel_id):
    """Skip a 403 channel for 24h."""
    database.set_state(f"discord:denied_until:{channel_id}",
                       str(int(time.time()) + DENY_TTL))
