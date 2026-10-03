"""First-seen registry (Rick-style, cross-chat).

Token identity is "chain:address" (the Nth-mention / FIRST-tag concept).
EVM addresses are lowercased for normalization; Solana base58 is
case-sensitive and kept as-is.

Functions:
    normalize_token_id(chain, address) -> "chain:address"
    register_mention(db, ...) -> (token_row, is_first_seen)
    mention_velocity(db, token_id, window_hours) -> mentions/hour
    distinct_chats(db, token_id, since_ts) -> number of independent chats
"""

import time


def normalize_token_id(chain: str, address: str) -> str:
    chain = (chain or "").strip().lower() or "unknown"
    address = (address or "").strip()
    if chain == "evm" or address.startswith("0x"):
        address = address.lower()
        chain = "evm"
    return f"{chain}:{address}"


def register_mention(database, chain, address, symbol="", chat_id="",
                     ts=None, message_id=None, intent="neutral",
                     caller_weight=1.0):
    """Record a mention; create the token row on first sight.

    Returns (token_row_dict, is_first_seen_bool).
    """
    ts = int(ts if ts is not None else time.time())
    norm_chain, _, norm_addr = normalize_token_id(chain, address).partition(":")
    existed = database.conn.execute(
        "SELECT id FROM tokens WHERE chain=? AND address=?",
        (norm_chain, norm_addr),
    ).fetchone()
    token = database.get_or_create_token(norm_chain, norm_addr,
                                         symbol=symbol, ts=ts, chat=str(chat_id))
    if message_id is not None:
        database.insert_mention(token["id"], message_id, intent=intent,
                                caller_weight=caller_weight, ts=ts)
    return token, existed is None


def mention_velocity(database, token_id, window_hours=1, now=None) -> float:
    """Mentions per hour over a rolling window."""
    now = int(now if now is not None else time.time())
    since = now - int(window_hours * 3600)
    count = database.mention_count(token_id, since_ts=since)
    return count / window_hours if window_hours else 0.0


def distinct_chats(database, token_id, since_ts=None) -> int:
    """How many independent chats mentioned this token (consensus breadth)."""
    if since_ts is None:
        row = database.conn.execute(
            "SELECT COUNT(DISTINCT m.chat_id) c FROM mentions me "
            "JOIN messages m ON m.id = me.message_id WHERE me.token_id=?",
            (token_id,),
        ).fetchone()
    else:
        row = database.conn.execute(
            "SELECT COUNT(DISTINCT m.chat_id) c FROM mentions me "
            "JOIN messages m ON m.id = me.message_id "
            "WHERE me.token_id=? AND me.ts>=?",
            (token_id, int(since_ts)),
        ).fetchone()
    return row["c"]
