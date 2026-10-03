"""Caller reputation scoring.

Each time a user shares a token with `call` intent, record_call() logs it
with price/mcap at call time. update_outcome() records 1h/24h/7d returns.
Weight follows the spec:

  - everyone starts at 1.0x (neutral)
  - minimum 10-call sample before weight moves meaningfully
    (Bayesian shrinkage toward the mean for small samples)
  - recency weighting: recent calls count more (30-day half-life)
  - rug-calls actively penalize
  - only the caller's FIRST mention per token counts (anti-gaming:
    spamming the same token can't inflate the record)

A "win" is defined as >= +50% at 24h (WIN_RETURN_THRESHOLD).

Pure functions (compute_weight) are separated from DB I/O so the math is
unit-testable without a database.
"""

import math
import time

MIN_CALLS_FOR_FULL_WEIGHT = 10   # sample size before weight fully moves
WIN_RETURN_THRESHOLD = 0.50      # +50% at 24h counts as a win
RECENCY_HALFLIFE_DAYS = 30.0
MAX_WEIGHT = 3.0
MIN_WEIGHT = 0.1


def compute_weight(calls: int, win_rate: float, avg_return: float,
                   rug_rate: float) -> float:
    """Pure weight math. All inputs are recency-weighted aggregates.

    calls      -- effective (decay-weighted) number of scored calls
    win_rate   -- decay-weighted fraction of calls hitting WIN_RETURN_THRESHOLD
    avg_return -- decay-weighted mean 24h return (1.0 = +100%)
    rug_rate   -- decay-weighted fraction of calls that rugged
    """
    perf = (win_rate - 0.30) * 3.0          # 60% win -> +0.9 ; 30% -> 0 ; 10% -> -0.6
    ret_term = max(-1.0, min(3.0, avg_return)) * 0.3
    rug_term = -rug_rate * 2.0              # 50% rugs -> -1.0
    raw = 1.0 + perf + ret_term + rug_term
    raw = max(MIN_WEIGHT, min(MAX_WEIGHT, raw))
    confidence = min(1.0, calls / MIN_CALLS_FOR_FULL_WEIGHT)  # shrinkage
    return 1.0 + (raw - 1.0) * confidence


def _decay(age_days: float) -> float:
    return 0.5 ** (age_days / RECENCY_HALFLIFE_DAYS)


def record_call(database, platform, user_id, username, chat_id,
                token_id, price_at_call=None, mcap_at_call=None, ts=None):
    """Log a call. Returns the call row id.

    is_first_for_token=0 if this caller already called this token (dupes
    don't inflate the record -- anti-gaming).
    """
    ts = int(ts if ts is not None else time.time())
    caller = database.get_or_create_caller(platform, user_id,
                                           username=username, chat_id=chat_id)
    dup = database.conn.execute(
        "SELECT id FROM calls WHERE caller_id=? AND token_id=?",
        (caller["id"], token_id),
    ).fetchone()
    database.conn.execute(
        "INSERT INTO calls(caller_id, token_id, is_first_for_token,"
        " price_at_call, mcap_at_call, ts) VALUES(?, ?, ?, ?, ?, ?)",
        (caller["id"], token_id, 0 if dup else 1,
         price_at_call, mcap_at_call, ts),
    )
    database.conn.execute(
        "UPDATE callers SET calls = calls + 1 WHERE id=?", (caller["id"],))
    database.conn.commit()
    return database.conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]


def update_outcome(database, call_id, horizon, ret, rugged=False, now=None):
    """Record a horizon outcome. horizon in {'1h','24h','7d'}; ret as
    decimal (0.5 = +50%). Triggers caller aggregate recompute."""
    if horizon not in ("1h", "24h", "7d"):
        raise ValueError(f"bad horizon: {horizon}")
    col = {"1h": "ret_1h", "24h": "ret_24h", "7d": "ret_7d"}[horizon]
    database.conn.execute(
        f"UPDATE calls SET {col}=?, rugged=? WHERE id=?",
        (float(ret), 1 if rugged else 0, call_id),
    )
    database.conn.commit()
    row = database.conn.execute(
        "SELECT caller_id FROM calls WHERE id=?", (call_id,)).fetchone()
    if row:
        recompute_caller(database, row["caller_id"], now=now)


def recompute_caller(database, caller_id, now=None):
    """Recompute wins/avg_return/rug_calls/weight from scored calls.

    Only first-per-token calls with a 24h outcome count; contributions are
    time-decayed (30-day half-life).
    """
    now = int(now if now is not None else time.time())
    rows = database.conn.execute(
        "SELECT ret_24h, rugged, ts FROM calls "
        "WHERE caller_id=? AND is_first_for_token=1 AND ret_24h IS NOT NULL",
        (caller_id,),
    ).fetchall()
    w_sum = w_wins = w_ret = w_rugs = 0.0
    for r in rows:
        age_days = max(0.0, (now - r["ts"]) / 86400.0)
        w = _decay(age_days)
        w_sum += w
        if r["ret_24h"] >= WIN_RETURN_THRESHOLD:
            w_wins += w
        w_ret += w * r["ret_24h"]
        if r["rugged"]:
            w_rugs += w
    if w_sum > 0:
        weight = compute_weight(w_sum, w_wins / w_sum,
                                w_ret / w_sum, w_rugs / w_sum)
        avg_return = w_ret / w_sum
        rug_calls = int(round(w_rugs))
    else:
        weight, avg_return, rug_calls = 1.0, 0.0, 0
        w_wins = 0.0
    database.conn.execute(
        "UPDATE callers SET wins=?, avg_return=?, rug_calls=?, weight=? WHERE id=?",
        (int(round(w_wins)), avg_return, rug_calls, weight, caller_id),
    )
    database.conn.commit()
    return {"weight": weight, "effective_calls": w_sum,
            "avg_return": avg_return}


def get_weight(database, platform, user_id, chat_id="") -> float:
    """Current weight for a caller; 1.0 for unknown callers."""
    row = database.conn.execute(
        "SELECT weight FROM callers WHERE platform=? AND user_id=? AND chat_id=?",
        (platform, str(user_id), str(chat_id)),
    ).fetchone()
    return row["weight"] if row else 1.0
