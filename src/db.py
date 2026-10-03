"""SQLite storage for the signal-trading scaffold.

Tables:
    messages      raw ingested chat messages (idempotent on platform/chat/msg)
    tokens        normalized token registry (chain:address)
    mentions      token mention links (message -> token, with intent + caller weight)
    callers       per-user reputation aggregates
    calls         per-call records for outcome tracking (1h/24h/7d) -- feeds callers
    positions     paper positions; there is no real-money mode in this package
    decisions_log every decision with score breakdown + reasoning (audit trail)
    ingest_state  key/value store for poll offsets (telegram update_id, discord last_id)
    known_chats   every chat/channel the bots have been seen in (auto-discovery)

All queries are parameterized. Timestamps are epoch seconds (int).
"""

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    platform  TEXT NOT NULL,
    chat_id   TEXT NOT NULL,
    msg_id    TEXT NOT NULL,
    user_id   TEXT NOT NULL DEFAULT '',
    username  TEXT NOT NULL DEFAULT '',
    text      TEXT NOT NULL DEFAULT '',
    ts        INTEGER NOT NULL,
    UNIQUE (platform, chat_id, msg_id)
);
CREATE TABLE IF NOT EXISTS tokens (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chain          TEXT NOT NULL,
    address        TEXT NOT NULL,
    symbol         TEXT NOT NULL DEFAULT '',
    first_seen_ts  INTEGER NOT NULL,
    first_seen_chat TEXT NOT NULL DEFAULT '',
    UNIQUE (chain, address)
);
CREATE TABLE IF NOT EXISTS mentions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id      INTEGER NOT NULL REFERENCES tokens(id),
    message_id    INTEGER NOT NULL REFERENCES messages(id),
    intent        TEXT NOT NULL DEFAULT 'neutral',
    caller_weight REAL NOT NULL DEFAULT 1.0,
    ts            INTEGER NOT NULL,
    UNIQUE (token_id, message_id)
);
CREATE TABLE IF NOT EXISTS callers (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    platform  TEXT NOT NULL,
    user_id   TEXT NOT NULL,
    username  TEXT NOT NULL DEFAULT '',
    chat_id   TEXT NOT NULL DEFAULT '',
    calls     INTEGER NOT NULL DEFAULT 0,
    wins      INTEGER NOT NULL DEFAULT 0,
    avg_return REAL NOT NULL DEFAULT 0.0,
    rug_calls INTEGER NOT NULL DEFAULT 0,
    weight    REAL NOT NULL DEFAULT 1.0,
    UNIQUE (platform, user_id, chat_id)
);
CREATE TABLE IF NOT EXISTS calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id      INTEGER NOT NULL REFERENCES callers(id),
    token_id       INTEGER NOT NULL REFERENCES tokens(id),
    is_first_for_token INTEGER NOT NULL DEFAULT 1,
    price_at_call  REAL,
    mcap_at_call   REAL,
    ts             INTEGER NOT NULL,
    ret_1h         REAL,
    ret_24h        REAL,
    ret_7d         REAL,
    rugged         INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mode        TEXT NOT NULL DEFAULT 'paper',
    token_id    INTEGER NOT NULL REFERENCES tokens(id),
    entry_price REAL NOT NULL,
    size_pct    REAL NOT NULL,
    notional    REAL NOT NULL,
    opened_ts   INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    exit_price  REAL,
    closed_ts   INTEGER,
    pnl         REAL,
    close_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS decisions_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    token_id        INTEGER REFERENCES tokens(id),
    action          TEXT NOT NULL,
    score_breakdown TEXT NOT NULL DEFAULT '{}',
    reasoning       TEXT NOT NULL DEFAULT '',
    tx_hash         TEXT
);
CREATE TABLE IF NOT EXISTS ingest_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS known_chats (
    platform   TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    UNIQUE (platform, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_mentions_token_ts ON mentions(token_id, ts);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);
CREATE TABLE IF NOT EXISTS theses (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id  INTEGER NOT NULL REFERENCES tokens(id),
    platform  TEXT NOT NULL,
    chat_id   TEXT NOT NULL DEFAULT '',
    user_id   TEXT NOT NULL DEFAULT '',
    username  TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT 'bull',
    text      TEXT NOT NULL,
    message_id INTEGER,
    ts        INTEGER NOT NULL,
    price_at_thesis REAL,
    ret_24h   REAL,
    verdict   TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_theses_token ON theses(token_id);
CREATE INDEX IF NOT EXISTS idx_theses_verdict ON theses(verdict, ts);
CREATE TABLE IF NOT EXISTS thesis_authors (
    platform TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    theses   INTEGER NOT NULL DEFAULT 0,
    correct  INTEGER NOT NULL DEFAULT 0,
    wrong    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (platform, user_id)
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)  # cron-safe: create data dir
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        # lightweight migration: partial-close realized P&L (v2)
        cols = [r["name"]
                for r in self.conn.execute("PRAGMA table_info(positions)")]
        if "realized_pnl" not in cols:
            self.conn.execute(
                "ALTER TABLE positions ADD COLUMN realized_pnl "
                "REAL NOT NULL DEFAULT 0")
        # lightweight migration: live-trade tx hashes (v3, 2026-10-02)
        if "buy_tx_hash" not in cols:
            self.conn.execute(
                "ALTER TABLE positions ADD COLUMN buy_tx_hash TEXT "
                "NOT NULL DEFAULT ''")
        if "sell_tx_hash" not in cols:
            self.conn.execute(
                "ALTER TABLE positions ADD COLUMN sell_tx_hash TEXT "
                "NOT NULL DEFAULT ''")
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- ingest_state (poll offsets) ----
    def get_state(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM ingest_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO ingest_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ---- known_chats (auto-discovery) ----
    def upsert_chat(self, platform, chat_id, title=""):
        """Record a chat the bot was seen in. Returns True if newly discovered."""
        now = int(time.time())
        row = self.conn.execute(
            "SELECT title FROM known_chats WHERE platform=? AND chat_id=?",
            (platform, str(chat_id)),
        ).fetchone()
        if row:
            # refresh title if we learned a better one
            if title and title != row["title"]:
                self.conn.execute(
                    "UPDATE known_chats SET title=?, last_seen=? "
                    "WHERE platform=? AND chat_id=?",
                    (title, now, platform, str(chat_id)),
                )
            else:
                self.conn.execute(
                    "UPDATE known_chats SET last_seen=? "
                    "WHERE platform=? AND chat_id=?",
                    (now, platform, str(chat_id)),
                )
            self.conn.commit()
            return False
        self.conn.execute(
            "INSERT INTO known_chats(platform, chat_id, title, first_seen, last_seen)"
            " VALUES(?, ?, ?, ?, ?)",
            (platform, str(chat_id), title or "", now, now),
        )
        self.conn.commit()
        return True

    def list_chats(self, platform):
        rows = self.conn.execute(
            "SELECT chat_id, title FROM known_chats WHERE platform=? "
            "ORDER BY first_seen",
            (platform,),
        ).fetchall()
        return [(r["chat_id"], r["title"]) for r in rows]

    # ---- messages ----
    def insert_message(self, platform, chat_id, msg_id, user_id, username, text, ts):
        """Idempotent insert; returns message row id (existing or new)."""
        self.conn.execute(
            "INSERT OR IGNORE INTO messages(platform, chat_id, msg_id, user_id, username, text, ts)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            (platform, str(chat_id), str(msg_id), str(user_id), username or "",
             text or "", int(ts)),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM messages WHERE platform=? AND chat_id=? AND msg_id=?",
            (platform, str(chat_id), str(msg_id)),
        ).fetchone()
        return row["id"]

    # ---- tokens ----
    def get_or_create_token(self, chain, address, symbol="", ts=None, chat=""):
        ts = int(ts if ts is not None else time.time())
        self.conn.execute(
            "INSERT OR IGNORE INTO tokens(chain, address, symbol, first_seen_ts, first_seen_chat)"
            " VALUES(?, ?, ?, ?, ?)",
            (chain, address, symbol or "", ts, str(chat)),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM tokens WHERE chain=? AND address=?", (chain, address)
        ).fetchone()
        return dict(row)

    # ---- mentions ----
    def insert_mention(self, token_id, message_id, intent="neutral", caller_weight=1.0, ts=None):
        ts = int(ts if ts is not None else time.time())
        self.conn.execute(
            "INSERT OR IGNORE INTO mentions(token_id, message_id, intent, caller_weight, ts)"
            " VALUES(?, ?, ?, ?, ?)",
            (token_id, message_id, intent, float(caller_weight), ts),
        )
        self.conn.commit()

    def mention_count(self, token_id, since_ts=None):
        if since_ts is None:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM mentions WHERE token_id=?", (token_id,)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM mentions WHERE token_id=? AND ts>=?",
                (token_id, int(since_ts)),
            ).fetchone()
        return row["c"]

    # ---- callers ----
    def get_or_create_caller(self, platform, user_id, username="", chat_id=""):
        self.conn.execute(
            "INSERT OR IGNORE INTO callers(platform, user_id, username, chat_id)"
            " VALUES(?, ?, ?, ?)",
            (platform, str(user_id), username or "", str(chat_id)),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM callers WHERE platform=? AND user_id=? AND chat_id=?",
            (platform, str(user_id), str(chat_id)),
        ).fetchone()
        return dict(row)

    # ---- decisions ----
    def log_decision(self, action, token_id=None, score_breakdown=None,
                     reasoning="", tx_hash=None, ts=None):
        ts = int(ts if ts is not None else time.time())
        self.conn.execute(
            "INSERT INTO decisions_log(ts, token_id, action, score_breakdown, reasoning, tx_hash)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (ts, token_id, action,
             json.dumps(score_breakdown or {}), reasoning, tx_hash),
        )
        self.conn.commit()
