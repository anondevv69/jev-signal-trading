"""Sell-signal engine (mode-agnostic) + paper sell loop.

Exit rules (checked in priority order per position):
  1. warning kill: any new 'warning'-intent mention from a caller with
     weight >= WARNING_MIN_WEIGHT -> close 100% immediately
  2. stop-loss: return <= -STOP_LOSS_PCT -> close 100%
  3. take-profit tier 1: return >= TP1_PCT -> close TP1_CLOSE_FRACTION
  4. take-profit tier 2: return >= TP2_PCT -> close TP2_CLOSE_FRACTION
  5. sentiment exit: TypeSafe Noul on recent chatter ("turned negative?")
     >= SENTIMENT_NOUL_MIN -> close SENTIMENT_CLOSE_FRACTION
  6. time exit: age >= TIME_EXIT_HOURS with no new mentions in that window
     and price below entry -> close 100%

Fail-open on price: if no current price is available the position is skipped
this run -- never sell blind. The buy-side Noul gate is fail-closed; the
sentiment Noul here is fail-open (None -> skip the sentiment check only).

exit_signal() is pure: it computes the signal dict and has NO side effects
(no closes, no TP-flag writes). check_position()/run_sells() apply it to
paper positions; the live executor (src/live.py) applies it to live
positions and writes TP flags only after a real swap confirms.

TP tier hits are tracked in ingest_state (<ns>:tp1:<pid>, <ns>:tp2:<pid>)
so each tier fires once per position; paper uses ns="sell", live uses
ns="livesell".
"""

import time

from . import intel as intelmod
from . import judge as judgemod
from . import scoring as scoringmod

# ---------------- TUNABLE defaults (starting points; tune in paper trading) ---
STOP_LOSS_PCT = 0.40  # mandate: -40% stop (2026-10-02)
TP1_PCT = 1.00
TP1_CLOSE_FRACTION = 0.50
TP2_PCT = 4.00
TP2_CLOSE_FRACTION = 0.25
TIME_EXIT_HOURS = 72
SENTIMENT_NOUL_MIN = 0.70
SENTIMENT_CLOSE_FRACTION = 0.50
SENTIMENT_MIN_MESSAGES = 2   # need at least this many recent messages
SENTIMENT_LOOKBACK_HOURS = 24
WARNING_MIN_WEIGHT = 1.5
# -----------------------------------------------------------------------------

SENTIMENT_INSTRUCTIONS = (
    "You are judging whether sentiment about this token in recent chat "
    "messages has turned materially NEGATIVE (fear, warnings, disappointment, "
    "sell pressure, scam accusations). Answer with the probability (0-1) that "
    "sentiment has turned negative. "
    "Example: messages saying 'dev dumped', 'this is dead', 'getting out' "
    "-> about 0.9. "
    "Example: neutral price discussion and memes, no fear -> about 0.2."
)


def _tp_flag(database, pid, tier, ns="sell"):
    return database.get_state(f"{ns}:tp{tier}:{pid}", "") == "hit"


def _set_tp_flag(database, pid, tier, ns="sell"):
    database.set_state(f"{ns}:tp{tier}:{pid}", "hit")


def mark_tp_hit(database, pid, tier, ns="sell"):
    """Record that a take-profit tier fired for a position. Call only after
    the corresponding close has actually executed (paper close or live swap)."""
    _set_tp_flag(database, pid, tier, ns=ns)


def _recent_texts(database, token_id, since_ts, limit=20):
    rows = database.conn.execute(
        "SELECT m.username, m.text FROM mentions me "
        "JOIN messages m ON m.id = me.message_id "
        "WHERE me.token_id = ? AND me.ts >= ? AND m.text != '' "
        "ORDER BY me.ts DESC LIMIT ?",
        (token_id, int(since_ts), limit),
    ).fetchall()
    return [f"@{r['username'] or '?'}: {r['text'][:300]}" for r in rows]


def _warning_kill(database, token_id, opened_ts, now):
    """New warning-intent mentions from reputable callers since open.

    Returns (username, weight, text_snippet) or None."""
    rows = database.conn.execute(
        "SELECT m.platform, m.chat_id, m.user_id, m.username, m.text "
        "FROM mentions me JOIN messages m ON m.id = me.message_id "
        "WHERE me.token_id = ? AND me.intent = 'warning' AND me.ts >= ?",
        (token_id, int(opened_ts)),
    ).fetchall()
    for r in rows:
        try:
            w = scoringmod.get_weight(database, r["platform"], r["user_id"],
                                      r["chat_id"])
        except Exception:
            w = 1.0
        if w >= WARNING_MIN_WEIGHT:
            return (r["username"] or r["user_id"], w, r["text"][:200])
    return None


def exit_signal(database, pos, now=None, ns="sell"):
    """Evaluate one open position and return a signal dict, or None to hold.

    PURE: no side effects — does not close anything, does not write TP
    flags. The caller applies the close (paper ledger or live swap) and then
    calls mark_tp_hit() for signals carrying a tp_tier.

    pos must carry: id, token_id, chain, address, symbol, entry_price,
    opened_ts. ns namespaces the TP-flag keys ("sell" paper, "livesell"
    live). Never raises.
    """
    now = int(now if now is not None else time.time())
    token_id = pos["token_id"]
    label = pos.get("symbol") or pos["address"][:12]
    try:
        intel = intelmod.get_intel(pos["chain"], pos["address"])
    except Exception as e:
        return {"action": "note", "token": f"{pos['chain']}:{pos['address']}",
                "reasoning": f"SELL SKIP {label}: intel failed ({e})"}
    price = intel.get("price_usd")
    if not price:
        return {"action": "note", "token": f"{pos['chain']}:{pos['address']}",
                "reasoning": f"SELL SKIP {label}: no price available "
                             f"(fail-open: position untouched)"}
    entry = pos["entry_price"]
    ret = price / entry - 1 if entry > 0 else 0.0

    def _sig(action, fraction, reason, tp_tier=None):
        return {
            "action": action,  # "sell" | "partial_sell"
            "token": f"{pos['chain']}:{pos['address']}",
            "symbol": pos.get("symbol"), "fraction": fraction,
            "entry_price": entry, "exit_price": price,
            "return_pct": round(ret * 100, 2),
            "position_id": pos["id"], "reasoning": reason,
            "tp_tier": tp_tier,
        }

    # 1. warning kill (highest priority)
    warn = _warning_kill(database, token_id, pos["opened_ts"], now)
    if warn:
        user, w, snippet = warn
        return _sig(
            "sell", 1.0,
            f"WARNING KILL {label}: @{user} (caller weight {w:.2f}x) warned "
            f"after open: \"{snippet}\". Closed 100% at ${price} "
            f"({ret * 100:+.1f}%).")

    # 2. stop-loss
    if ret <= -STOP_LOSS_PCT:
        return _sig(
            "sell", 1.0,
            f"STOP-LOSS {label}: {ret * 100:.1f}% <= -{STOP_LOSS_PCT * 100:.0f}% "
            f"from entry ${entry}. Closed 100% at ${price}.")

    # 3/4. take-profit tiers (each fires once; caller marks the hit)
    if ret >= TP1_PCT and not _tp_flag(database, pos["id"], 1, ns=ns):
        return _sig(
            "partial_sell", TP1_CLOSE_FRACTION,
            f"TAKE-PROFIT T1 {label}: +{ret * 100:.1f}% >= +{TP1_PCT * 100:.0f}%. "
            f"Closed {TP1_CLOSE_FRACTION:.0%} at ${price}; remainder rides.",
            tp_tier=1)
    if (ret >= TP2_PCT and _tp_flag(database, pos["id"], 1, ns=ns)
            and not _tp_flag(database, pos["id"], 2, ns=ns)):
        return _sig(
            "partial_sell", TP2_CLOSE_FRACTION,
            f"TAKE-PROFIT T2 {label}: +{ret * 100:.1f}% >= +{TP2_PCT * 100:.0f}%. "
            f"Closed {TP2_CLOSE_FRACTION:.0%} of remainder at ${price}.",
            tp_tier=2)

    # 5. sentiment exit
    texts = _recent_texts(database, token_id,
                          now - SENTIMENT_LOOKBACK_HOURS * 3600)
    if len(texts) >= SENTIMENT_MIN_MESSAGES:
        state = (f"Token {label}. Recent chat messages:\n"
                 + "\n".join(texts[:10]))
        snoul = judgemod.ask_noul(state, SENTIMENT_INSTRUCTIONS,
                                  key="sentiment")
        if snoul is not None and snoul >= SENTIMENT_NOUL_MIN:
            return _sig(
                "partial_sell", SENTIMENT_CLOSE_FRACTION,
                f"SENTIMENT EXIT {label}: negative-sentiment noul "
                f"{snoul:.2f} >= {SENTIMENT_NOUL_MIN} over "
                f"{len(texts)} recent messages. Closed "
                f"{SENTIMENT_CLOSE_FRACTION:.0%} at ${price} "
                f"({ret * 100:+.1f}%).")

    # 6. time exit: old, silent, underwater
    age_h = (now - pos["opened_ts"]) / 3600.0
    if age_h >= TIME_EXIT_HOURS:
        recent = database.conn.execute(
            "SELECT COUNT(*) c FROM mentions WHERE token_id = ? AND ts >= ?",
            (token_id, now - TIME_EXIT_HOURS * 3600)).fetchone()["c"]
        if recent == 0 and ret < 0:
            return _sig(
                "sell", 1.0,
                f"TIME EXIT {label}: {age_h:.0f}h old, no mentions in "
                f"{TIME_EXIT_HOURS}h, {ret * 100:.1f}% underwater. "
                f"Closed 100% at ${price} to free capital.")

    return None


def check_position(database, paper, pos, now=None):
    """Evaluate one open paper position and apply any exit to the paper
    ledger. Returns a signal dict or None. Never raises."""
    sig = exit_signal(database, pos, now=now, ns="sell")
    if not sig or sig["action"] == "note":
        return sig
    if sig.get("tp_tier"):
        mark_tp_hit(database, sig["position_id"], sig["tp_tier"], ns="sell")
    if sig["action"] == "sell":
        pnl = paper.close_position(sig["position_id"], sig["exit_price"],
                                   ts=now, reason=sig["reasoning"])
    else:
        pnl = paper.close_partial(sig["position_id"], sig["fraction"],
                                  sig["exit_price"], ts=now,
                                  reason=sig["reasoning"])
    sig["pnl"] = round(pnl, 2)
    return sig


def run_sells(database, paper, now=None):
    """Run the sell loop over all open paper positions.

    Returns (signals, notes). Signals include 'note' actions for skips."""
    now = int(now if now is not None else time.time())
    signals, notes = [], []
    for pos in paper.open_positions():
        try:
            sig = check_position(database, paper, pos, now=now)
        except Exception as e:
            notes.append(f"sell check failed for position {pos['id']}: {e}")
            continue
        if sig:
            (signals if sig["action"] != "note" else notes).append(
                sig["reasoning"] if sig["action"] == "note" else sig)
    return signals, notes
