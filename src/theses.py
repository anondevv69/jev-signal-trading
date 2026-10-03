"""Theses: signed, falsifiable claims on tokens, with author reputation.

Two ingestion paths:
  1. Explicit: a message starting with `/thesis [bull|bear] <text with token>`.
  2. Implicit: a call-intent message with >= THESIS_MIN_CHARS characters is a
     de-facto bull thesis (it carries reasoning); a warning-intent message
     with >= THESIS_MIN_CHARS is a bear thesis.

Each thesis records the token price at write time. score_theses() (run from
the orchestrator) grades theses older than 24h: bull is correct at >= +15%
ret_24h, wrong at <= -15% (mirrored for bear); in-between is "mixed" and does
not count toward accuracy. Author accuracy feeds back into Jev's judgment
input in decide._noul_state.

Spam defense is reputation: anyone can write a thesis, but only authors with
a scored track record move the Noul.
"""
import re
import sys
import time

from . import db as dbmod
from . import extract as extractmod
from . import first_seen as fsmod
from . import intel as intelmod

THESIS_MIN_CHARS = 200
SCORE_AFTER_H = 24
CORRECT_RET = 0.15

THESIS_CMD_RE = re.compile(r"^\s*/thesis\b", re.IGNORECASE)
DIR_WORD_RE = re.compile(r"^\s*/thesis\s+(bull(?:ish)?|bear(?:ish)?)\b", re.IGNORECASE)
BEAR_HINT_RE = re.compile(
    r"\b(bear(?:ish)?|short(?:ing)?|avoid|rug(?:ged|pull)?|dump(?:ing)?|"
    r"sell|scam|ponzi|honeypot|stay away|do not buy|don't buy)\b", re.IGNORECASE)


def parse_command(text):
    """Parse an explicit /thesis command. Returns (direction, body)."""
    m = DIR_WORD_RE.match(text or "")
    direction = None
    body = re.sub(r"^\s*/thesis\b", "", text or "", count=1,
                  flags=re.IGNORECASE).strip()
    if m:
        direction = "bear" if m.group(1).lower().startswith("bear") else "bull"
        body = re.sub(r"^\s*(bull(?:ish)?|bear(?:ish)?)\b[\s,:\-]*", "",
                      body, count=1, flags=re.IGNORECASE).strip()
    if not direction:
        direction = "bear" if BEAR_HINT_RE.search(body) else "bull"
    return direction, body


def detect(text, intent):
    """Implicit thesis detection. Returns direction or None."""
    if not text:
        return None
    if THESIS_CMD_RE.match(text):
        direction, _ = parse_command(text)
        return direction
    if len(text) >= THESIS_MIN_CHARS:
        if intent == "call":
            return "bull"
        if intent == "warning":
            return "bear"
    return None


def _thesis_body(text):
    """The stored text: command prefix stripped for explicit theses."""
    if THESIS_CMD_RE.match(text or ""):
        _, body = parse_command(text)
        return body
    return (text or "").strip()


def ingest_thesis(database, platform, chat_id, user_id, username, text,
                  direction, ts=None, message_id=None):
    """Record a thesis for each token candidate in the text. Returns count.

    Idempotent-ish: skips if this author already has a thesis on the token
    within the last 24h. Captures price_at_thesis via intel when available.
    Never raises.
    """
    ts = int(ts if ts is not None else time.time())
    body = _thesis_body(text)
    if not body:
        return 0
    n = 0
    for cand in extractmod.extract_candidates(body):
        try:
            norm = fsmod.normalize_token_id(cand.get("chain", "evm"),
                                            cand.get("address", ""))
            chain, _, address = norm.partition(":")
            if not address:
                continue
            token = database.get_or_create_token(
                chain, address, symbol=cand.get("symbol", ""), ts=ts,
                chat=str(chat_id or ""))
            dup = database.conn.execute(
                "SELECT id FROM theses WHERE token_id=? AND platform=? "
                "AND user_id=? AND ts > ?",
                (token["id"], platform, user_id, ts - 86400)).fetchone()
            if dup:
                continue
            price = None
            try:
                price = (intelmod.get_intel(chain, address) or {}
                         ).get("price_usd")
            except Exception:
                price = None
            database.conn.execute(
                "INSERT INTO theses(token_id, platform, chat_id, user_id, "
                "username, direction, text, message_id, ts, price_at_thesis) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (token["id"], platform, str(chat_id or ""), user_id,
                 username or "", direction, body[:2000], message_id, ts,
                 price))
            database.conn.execute(
                "INSERT INTO thesis_authors(platform, user_id, username, theses) "
                "VALUES(?, ?, ?, 1) "
                "ON CONFLICT(platform, user_id) DO UPDATE SET "
                "theses = theses + 1, username = excluded.username",
                (platform, user_id, username or ""))
            database.conn.commit()
            n += 1
        except Exception as e:
            print(f"theses: ingest failed ({e})", file=sys.stderr)
    return n


def _grade(direction, ret):
    if ret is None:
        return "pending"
    if direction == "bull":
        if ret >= CORRECT_RET:
            return "correct"
        if ret <= -CORRECT_RET:
            return "wrong"
    else:
        if ret <= -CORRECT_RET:
            return "correct"
        if ret >= CORRECT_RET:
            return "wrong"
    return "mixed"


def score_theses(database, now=None, limit=10):
    """Grade pending theses older than SCORE_AFTER_H. Returns graded count.

    Each graded thesis needs one intel price lookup; capped at `limit` per
    run. Never raises.
    """
    now = int(now if now is not None else time.time())
    rows = database.conn.execute(
        "SELECT th.id, th.token_id, th.direction, th.price_at_thesis, "
        "th.platform, th.user_id, t.chain, t.address "
        "FROM theses th JOIN tokens t ON t.id = th.token_id "
        "WHERE th.verdict = 'pending' AND th.ts < ? AND th.price_at_thesis "
        "IS NOT NULL ORDER BY th.ts ASC LIMIT ?",
        (now - SCORE_AFTER_H * 3600, limit)).fetchall()
    graded = 0
    for rid, token_id, direction, price_at, platform, user_id, chain, addr \
            in rows:
        try:
            price = (intelmod.get_intel(chain, addr) or {}).get("price_usd")
            if not price or price_at <= 0:
                continue
            ret = price / price_at - 1.0
            verdict = _grade(direction, ret)
            database.conn.execute(
                "UPDATE theses SET ret_24h = ?, verdict = ? WHERE id = ?",
                (round(ret, 4), verdict, rid))
            if verdict in ("correct", "wrong"):
                col = "correct" if verdict == "correct" else "wrong"
                database.conn.execute(
                    f"UPDATE thesis_authors SET {col} = {col} + 1 "
                    "WHERE platform = ? AND user_id = ?",
                    (platform, user_id))
            database.conn.commit()
            graded += 1
        except Exception as e:
            print(f"theses: score failed thesis {rid} ({e})", file=sys.stderr)
    return graded


def author_accuracy(database, platform, user_id):
    """(accuracy 0-1 or None if unscored, n_scored)."""
    row = database.conn.execute(
        "SELECT correct, wrong FROM thesis_authors "
        "WHERE platform = ? AND user_id = ?",
        (platform, user_id)).fetchone()
    if not row:
        return None, 0
    correct, wrong = row
    n = correct + wrong
    if n == 0:
        return None, 0
    return correct / n, n


def top_theses_for_token(database, token_id, limit=3):
    """Theses on a token, ranked by author accuracy. Returns dicts."""
    rows = database.conn.execute(
        "SELECT th.direction, th.text, th.username, th.user_id, th.platform, "
        "th.ts, th.verdict, COALESCE(ta.correct, 0), COALESCE(ta.wrong, 0) "
        "FROM theses th LEFT JOIN thesis_authors ta "
        "ON ta.platform = th.platform AND ta.user_id = th.user_id "
        "WHERE th.token_id = ? ORDER BY th.ts DESC LIMIT 50",
        (token_id,)).fetchall()
    out = []
    for direction, text, username, uid, platform, ts, verdict, c, w in rows:
        n = c + w
        acc = c / n if n else None
        out.append({
            "direction": direction, "text": text, "username": username,
            "user_id": uid, "platform": platform, "ts": ts,
            "verdict": verdict, "accuracy": acc, "n_scored": n,
        })
    # scored authors with real accuracy first, then the rest (newest first)
    out.sort(key=lambda t: (t["accuracy"] is not None,
                            t["accuracy"] or 0, t["n_scored"]),
             reverse=True)
    return out[:limit]


def stats(database):
    """One-line summary for cron logs."""
    row = database.conn.execute(
        "SELECT COUNT(*), SUM(verdict='correct'), SUM(verdict='wrong'), "
        "SUM(verdict='pending') FROM theses").fetchone()
    total, correct, wrong, pending = (row or (0, 0, 0, 0))
    authors = database.conn.execute(
        "SELECT COUNT(*) FROM thesis_authors").fetchone()[0]
    return (f"theses={total or 0} correct={correct or 0} wrong={wrong or 0} "
            f"pending={pending or 0} authors={authors}")
