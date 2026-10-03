"""Buy decision engine.

Pipeline per token with new mentions:
  score = (sum over call-intent mentions of caller_weight x recency_decay)
          x chat_independence x safety_factor x deployer_trust
  preconditions: score > 0, >= MIN_DISTINCT_CHATS, safety not flagged,
                 no open position (paper or live, per mode), price available
  TypeSafe buy Noul (fail-CLOSED: None -> skip the token, never buy blind)
  buy rule: noul >= BUY_NOUL_MIN -> size tier by Noul

Modes: evaluate_token(..., live=False) is the paper path (opens paper
positions via decide_buys); live=True is the live-trading path (src/live.py
executes real Bankr swaps). Live trading runs under the written mandate in
MANDATE.md (operator-defined),
authorized by the operator 2026-10-02: full discretion, no kill switch.

Every buy and every skipped/gated decision is logged to decisions_log with
human-readable reasoning.
"""

import time

from . import intel as intelmod
from . import theses as thesesmod
from . import judge as judgemod
from . import scoring as scoringmod
from . import first_seen as fsmod

# ---------------- TUNABLE defaults (starting points; tune in paper trading) ---
BUY_NOUL_MIN = 0.70
MIN_DISTINCT_CHATS = 2
SIZE_TIERS = [(0.90, 0.15), (0.80, 0.10), (0.70, 0.05)]  # (noul_min, size_pct)
MAX_TOKENS_PER_RUN = 10      # cap on TypeSafe Noul calls per run
SAFETY_MAX_TAX_PCT = 10.0    # buy/sell tax above this -> safety_factor = 0
DEPLOYER_TRUST_KNOWN = 1.0   # deployer resolved via blockscout
DEPLOYER_TRUST_UNKNOWN = 0.7  # v1 heuristic; full dossier indexer is pending
# -----------------------------------------------------------------------------


def _call_mentions(database, token_id):
    return database.conn.execute(
        "SELECT me.ts, me.caller_weight, m.platform, m.chat_id, m.user_id,"
        " m.username FROM mentions me JOIN messages m ON m.id = me.message_id"
        " WHERE me.token_id = ? AND me.intent = 'call'",
        (token_id,),
    ).fetchall()


def _open_paper_position(database, token_id):
    return database.conn.execute(
        "SELECT id FROM positions WHERE token_id = ? AND mode = 'paper'"
        " AND status = 'open' LIMIT 1",
        (token_id,),
    ).fetchone()


def _open_live_position(database, token_id):
    return database.conn.execute(
        "SELECT id FROM positions WHERE token_id = ? AND mode = 'live'"
        " AND status = 'open' LIMIT 1",
        (token_id,),
    ).fetchone()


def _size_for_noul(noul):
    for noul_min, size_pct in SIZE_TIERS:
        if noul >= noul_min:
            return size_pct
    return 0.0


def _fmt_pct(x):
    return "n/a" if x is None else f"{x:+.1f}%"


def _noul_state(database, token, intel, weighted_sum, distinct, call_rows):
    caller_bits = []
    for r in call_rows[:6]:
        try:
            w = scoringmod.get_weight(database, r["platform"], r["user_id"],
                                      r["chat_id"])
        except Exception:
            w = 1.0
        caller_bits.append(f"@{r['username'] or r['user_id']} ({w:.2f}x)")
    pc = intel.get("price_change") or {}
    lines = [
        f"Token {intel.get('symbol') or token['address'][:12]} "
        f"({token['chain']}:{token['address'][:18]}...) on "
        f"{intel.get('resolved_chain') or 'unknown chain'}.",
        f"Called by {len(call_rows)} user(s) across {distinct} independent "
        f"chat(s); weighted call score {weighted_sum:.2f}.",
        f"Price ${intel['price_usd']}" if intel.get("price_usd") else "Price unknown",
        f"1h {_fmt_pct(pc.get('h1'))}, 24h {_fmt_pct(pc.get('h24'))}, "
        f"24h volume ${intel.get('volume_24h')}, "
        f"liquidity ${intel.get('liquidity_usd')}.",
    ]
    s = intel.get("safety") or {}
    if s.get("checked"):
        lines.append(
            f"Safety checked: honeypot={s.get('is_honeypot')}, "
            f"buy tax {s.get('buy_tax')}%, sell tax {s.get('sell_tax')}%.")
    else:
        lines.append(f"Safety UNKNOWN: {s.get('note')}. Treat as caution.")
    d = intel.get("deployer") or {}
    if d.get("checked"):
        lines.append(
            f"Deployer {d.get('address')}, wallet txs {d.get('tx_count')}, "
            f"age {d.get('wallet_age_days')}d.")
    else:
        lines.append("Deployer unknown.")
    if caller_bits:
        lines.append("Callers: " + ", ".join(caller_bits) + ".")
    # theses: signed directional claims with author track records
    try:
        for t in thesesmod.top_theses_for_token(database, token["id"], limit=3):
            acc = (f"{t['accuracy']:.0%} over {t['n_scored']} scored"
                   if t["accuracy"] is not None else "no scored record yet")
            lines.append(
                f"Thesis ({t['direction']}) by @{t['username'] or t['user_id']} "
                f"({acc}): {t['text'][:280]}")
    except Exception:
        pass
    return "\n".join(lines)


BUY_INSTRUCTIONS = (
    "You are judging whether buying this token NOW is a good trade, given "
    "chat-call intelligence plus market/safety/deployer facts. Answer with the "
    "probability (0-1) that this is a good buy. "
    "Weight heavily: multiple reputable callers across INDEPENDENT chats, "
    "clean safety, real liquidity. Discount: single-chat hype, unknown safety, "
    "warnings, bot-driven mentions. "
    "Example: 3 reputable callers in 3 chats, clean safety, rising price and "
    "volume -> about 0.85. "
    "Example: one chat hyping it, safety unknown, flat price -> about 0.35. "
    "Example: any credible warning or honeypot flag -> below 0.2."
)


def evaluate_token(database, token, now=None, live=False):
    """Full evaluation of one token. Returns an eval dict; runs the TypeSafe
    buy Noul only if deterministic preconditions pass. Never raises.

    live=False checks for open paper positions; live=True checks for open
    live positions instead (used by the live executor)."""
    now = int(now if now is not None else time.time())
    ev = {"token_id": token["id"], "chain": token["chain"],
          "address": token["address"], "symbol": token.get("symbol", ""),
          "score": 0.0, "weighted_sum": 0.0, "distinct_chats": 0,
          "n_calls": 0, "noul": None, "size_pct": 0.0,
          "decision": "skip", "reasons": [], "intel": None}
    try:
        call_rows = _call_mentions(database, token["id"])
        ev["n_calls"] = len(call_rows)
        if not call_rows:
            ev["reasons"].append("no call-intent mentions")
            return ev
        weighted = 0.0
        for r in call_rows:
            w = scoringmod.get_weight(database, r["platform"], r["user_id"],
                                      r["chat_id"])
            age_days = max(0.0, (now - r["ts"]) / 86400.0)
            weighted += w * scoringmod._decay(age_days)
        ev["weighted_sum"] = round(weighted, 3)
        distinct = fsmod.distinct_chats(database, token["id"])
        ev["distinct_chats"] = distinct
        chat_indep = 1 + 0.5 * (distinct - 1)

        intel = intelmod.get_intel(token["chain"], token["address"])
        ev["intel"] = intel
        flagged = intelmod.safety_flagged(intel, SAFETY_MAX_TAX_PCT)
        safety_factor = 0.0 if flagged else 1.0
        if flagged:
            ev["reasons"].append("safety flagged (honeypot or extreme tax)")
        deployer_trust = (DEPLOYER_TRUST_KNOWN
                          if (intel.get("deployer") or {}).get("checked")
                          else DEPLOYER_TRUST_UNKNOWN)
        score = weighted * chat_indep * safety_factor * deployer_trust
        ev["score"] = round(score, 3)
        ev["safety_factor"] = safety_factor
        ev["deployer_trust"] = deployer_trust

        # deterministic preconditions before spending a Noul call
        if distinct < MIN_DISTINCT_CHATS:
            ev["reasons"].append(
                f"only {distinct} chat(s), need {MIN_DISTINCT_CHATS}")
            ev["decision"] = "no_signal"
            return ev
        if flagged:
            ev["decision"] = "no_signal"
            return ev
        already_open = (_open_live_position(database, token["id"])
                        if live else _open_paper_position(database, token["id"]))
        if already_open:
            ev["reasons"].append(
                f"{'live' if live else 'paper'} position already open")
            ev["decision"] = "no_signal"
            return ev
        if intel.get("price_usd") is None:
            ev["reasons"].append("no price available (fail-open: skip)")
            ev["decision"] = "no_signal"
            return ev

        state = _noul_state(database, token, intel, weighted, distinct,
                            call_rows)
        noul = judgemod.ask_noul(state, BUY_INSTRUCTIONS, key="buy")
        ev["noul"] = noul
        if noul is None:
            ev["reasons"].append("buy gate unavailable (fail-closed: skip)")
            ev["decision"] = "skip"
            return ev
        if noul < BUY_NOUL_MIN:
            ev["reasons"].append(f"noul {noul:.2f} < {BUY_NOUL_MIN}")
            ev["decision"] = "no_signal"
            database.log_decision(
                "buy_rejected", token_id=token["id"],
                score_breakdown={"score": ev["score"], "noul": noul,
                                 "distinct_chats": distinct,
                                 "n_calls": len(call_rows)},
                reasoning=f"Buy gate rejected {ev['symbol'] or token['address'][:12]}: "
                          f"noul {noul:.2f} below {BUY_NOUL_MIN}. "
                          f"Score {ev['score']:.2f} from {len(call_rows)} calls "
                          f"across {distinct} chats.",
                ts=now)
            return ev
        ev["size_pct"] = _size_for_noul(noul)
        ev["decision"] = "buy"
        return ev
    except Exception as e:
        ev["reasons"].append(f"evaluation error: {e}")
        ev["decision"] = "skip"
        return ev


def decide_buys(database, tokens, paper, now=None):
    """Evaluate candidate tokens; open paper positions for buys.

    Returns (signals, notes). signals are dicts for the alerter."""
    now = int(now if now is not None else time.time())
    signals, notes = [], []
    for token in tokens[:MAX_TOKENS_PER_RUN]:
        ev = evaluate_token(database, token, now=now)
        if ev["decision"] != "buy":
            continue
        intel = ev["intel"]
        price = intel["price_usd"]
        reasoning = (
            f"BUY {ev['symbol'] or token['address'][:12]} "
            f"({token['chain']}:{token['address'][:18]}...): "
            f"buy-noul {ev['noul']:.2f} -> {ev['size_pct']:.0%} size tier; "
            f"score {ev['score']:.2f} from {ev['n_calls']} call(s) across "
            f"{ev['distinct_chats']} chats (weighted sum {ev['weighted_sum']:.2f}); "
            f"price ${price} (1h {(intel.get('price_change') or {}).get('h1')}, "
            f"24h {(intel.get('price_change') or {}).get('h24')}); "
            f"liquidity ${intel.get('liquidity_usd')}; "
            f"safety {'clean (checked)' if (intel.get('safety') or {}).get('checked') else 'UNKNOWN - caution'}; "
            f"deployer {(intel.get('deployer') or {}).get('address') or 'unknown'}."
        )
        breakdown = {
            "score": ev["score"], "weighted_sum": ev["weighted_sum"],
            "distinct_chats": ev["distinct_chats"], "n_calls": ev["n_calls"],
            "noul": ev["noul"], "size_pct": ev["size_pct"],
            "entry_price": price,
            "safety_factor": ev.get("safety_factor"),
            "deployer_trust": ev.get("deployer_trust"),
            "unavailable": intel.get("unavailable"),
        }
        try:
            pid = paper.open_position(
                token["id"], ev["size_pct"], price, ts=now,
                reasoning=reasoning, score_breakdown=breakdown)
        except ValueError as e:
            database.log_decision(
                "buy_skipped", token_id=token["id"],
                score_breakdown=breakdown,
                reasoning=f"Buy signal for {ev['symbol'] or token['address'][:12]} "
                          f"blocked by paper rules: {e}", ts=now)
            notes.append(f"buy skipped (paper rules): {e}")
            continue
        signals.append({
            "action": "buy",
            "token": f"{token['chain']}:{token['address']}",
            "symbol": ev["symbol"],
            "size_pct": ev["size_pct"],
            "noul": round(ev["noul"], 3),
            "score": ev["score"],
            "entry_price": price,
            "position_id": pid,
            "reasoning": reasoning,
        })
    return signals, notes
