"""Live trade executor.

Turns buy evals from decide.py into real Bankr swaps and manages the live
position ledger (mode='live' rows in `positions`). Authorized by the operator
2026-10-02: full trading discretion, no kill switch, no per-trade approval.
Mandate: MANDATE.md (operator-defined)

Safety properties:
- Buys only flow from decide.evaluate_token(live=True): call-intent
  mentions, caller reputation, >= 2 independent chats, honeypot/tax safety
  veto, Jev buy Noul >= 0.70 FAIL-CLOSED (no Noul -> no buy), 5/10/15% size
  tiers from the Noul.
- Only chains where the Bankr wallet holds the input currency (Base,
  Robinhood Chain). Other chains are skipped with a logged note.
- Max 80% of bankroll deployed; max 8 open live positions; no dust
  positions under $5.
- Every executed swap is recorded with its tx hash in decisions_log and on
  the position row. A swap that yields no tx hash opens no position and is
  logged as failed.
- Sells are executed as balance-percentage swaps so partial closes always
  match what the wallet actually holds.

Never raises: execute_buy/execute_sell return (signal, note); exactly one
is non-None.
"""

import json
import os
import re
import subprocess

from . import sell as sellmod

BANKR_CLI = os.path.expanduser("~/workspace/skills/bankr/bin/bankr")

SIGNAL_ALERTS_URL = "https://api-production-8630.up.railway.app/v1/signal-alerts"
SIGNAL_KEY_FILE = os.path.expanduser("~/.signal_ingest_key")

# chains the Bankr wallet can fund (holds native ETH on both)
LIVE_CHAINS = {"base": "Base", "robinhood": "Robinhood Chain"}

MAX_DEPLOYED_PCT = 0.80
MAX_OPEN_POSITIONS = 8
MIN_NOTIONAL_USD = 5.0

TX_RE = re.compile(r"0x[0-9a-fA-F]{64}")


# ---------------- Bankr plumbing ----------------

def _bankr(prompt, timeout=300):
    """Run the Bankr CLI with a natural-language prompt.

    Returns (ok, response_text). Never raises."""
    try:
        proc = subprocess.run(
            [BANKR_CLI, "--prompt", prompt, "--timeout", str(timeout)],
            capture_output=True, text=True, timeout=timeout + 60)
    except Exception as e:
        return False, f"bankr cli failed: {e}"
    out = proc.stdout or ""
    start, end = out.find("{"), out.rfind("}")
    if start < 0 or end < 0:
        return False, f"bankr cli: no JSON in output: {(out + proc.stderr)[-500:]}"
    try:
        data = json.loads(out[start:end + 1])
    except Exception as e:
        return False, f"bankr cli: bad JSON ({e}): {out[start:start + 200]}"
    if not data.get("success"):
        return False, f"bankr job failed: {str(data.get('response'))[:500]}"
    return True, str(data.get("response", ""))


def bankroll_usd():
    """Current total Bankr portfolio value in USD. None if unavailable."""
    ok, resp = _bankr(
        "What is the total USD value of my entire portfolio across all "
        "chains? Reply with ONLY a single number, no words, no dollar sign.",
        timeout=120)
    if not ok:
        return None
    try:
        return float(resp.strip().split()[0].replace(",", ""))
    except Exception:
        return None


# ---------------- live ledger ----------------

def open_live_positions(database):
    rows = database.conn.execute(
        "SELECT p.*, t.chain, t.address, t.symbol FROM positions p "
        "JOIN tokens t ON t.id = p.token_id "
        "WHERE p.mode = 'live' AND p.status = 'open'").fetchall()
    return [dict(r) for r in rows]


def deployed_usd(database):
    row = database.conn.execute(
        "SELECT COALESCE(SUM(notional), 0) s FROM positions "
        "WHERE mode = 'live' AND status = 'open'").fetchone()
    return float(row["s"])


def _log(database, action, token_id, reasoning, breakdown=None, tx_hash=None,
         ts=None):
    database.log_decision(action, token_id=token_id,
                          score_breakdown=breakdown or {},
                          reasoning=reasoning, tx_hash=tx_hash, ts=ts)


# ---------------- broadcast ----------------

def _signal_key():
    try:
        return open(SIGNAL_KEY_FILE).read().strip()
    except Exception:
        return ""


def publish_alert(payload):
    """Publish one alert to the agent broadcast feed.

    Only executed, real-money decisions are published. Returns True on
    success. Never raises."""
    import json
    import urllib.request
    key = _signal_key()
    if not key:
        print("publish_alert: no ingest key; alert not published")
        return False
    try:
        req = urllib.request.Request(
            SIGNAL_ALERTS_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "X-Signal-Key": key,
                     "User-Agent": "fren-signal-trading/1.0"},
            method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = r.status == 201
        if not ok:
            print(f"publish_alert: unexpected status {r.status}")
        return ok
    except Exception as e:
        print(f"publish_alert failed: {e}")
        return False


def tx_url(chain, tx_hash):
    if not tx_hash:
        return ""
    if chain == "base":
        return f"https://basescan.org/tx/{tx_hash}"
    if chain == "robinhood":
        return f"https://robinhoodchain.blockscout.com/tx/{tx_hash}"
    return ""


# ---------------- buys ----------------

def _buy_reasoning(token, ev, notional, bankroll):
    intel = ev["intel"] or {}
    pc = intel.get("price_change") or {}
    return (
        f"LIVE BUY {ev['symbol'] or token['address'][:12]} "
        f"({token['chain']}:{token['address'][:18]}...): "
        f"${notional:.2f} ({ev['size_pct']:.0%} of ${bankroll:.2f} bankroll); "
        f"buy-noul {ev['noul']:.2f} -> {ev['size_pct']:.0%} tier; "
        f"score {ev['score']:.2f} from {ev['n_calls']} call(s) across "
        f"{ev['distinct_chats']} chats (weighted {ev['weighted_sum']:.2f}); "
        f"entry ${intel.get('price_usd')} (1h {pc.get('h1')}, 24h {pc.get('h24')}); "
        f"liquidity ${intel.get('liquidity_usd')}; "
        f"safety {'clean (checked)' if (intel.get('safety') or {}).get('checked') else 'UNKNOWN - caution'}; "
        f"deployer {(intel.get('deployer') or {}).get('address') or 'unknown'}."
    )


def execute_buy(database, token, ev, bankroll, now=None):
    """Execute a live buy for a 'buy' eval. Returns (signal, note)."""
    import time
    now = int(now if now is not None else time.time())
    label = ev["symbol"] or token["address"][:12]
    chain = token["chain"]

    def _skip(reason):
        _log(database, "live_buy_skipped", token_id=token["id"],
             reasoning=f"LIVE BUY SKIP {label}: {reason}", ts=now)
        return None, f"live buy skipped {label}: {reason}"

    if chain not in LIVE_CHAINS:
        return _skip(f"chain '{chain}' not tradable live "
                     f"(no input currency; tradable: {sorted(LIVE_CHAINS)})")
    if not bankroll or bankroll <= 0:
        return _skip("bankroll unavailable (fail-closed: no blind buys)")
    notional = bankroll * ev["size_pct"]
    if notional < MIN_NOTIONAL_USD:
        return _skip(f"notional ${notional:.2f} under ${MIN_NOTIONAL_USD} dust floor")
    deployed = deployed_usd(database)
    if (deployed + notional) / bankroll > MAX_DEPLOYED_PCT:
        return _skip(f"would exceed {MAX_DEPLOYED_PCT:.0%} max deployed "
                     f"(${deployed:.2f} + ${notional:.2f} of ${bankroll:.2f})")
    if len(open_live_positions(database)) >= MAX_OPEN_POSITIONS:
        return _skip(f"{MAX_OPEN_POSITIONS} live positions already open")

    price = (ev["intel"] or {}).get("price_usd")
    reasoning = _buy_reasoning(token, ev, notional, bankroll)
    prompt = (
        f"On {LIVE_CHAINS[chain]}, swap exactly ${notional:.2f} worth of ETH "
        f"for the token with contract address {token['address']} "
        f"(symbol {ev['symbol'] or 'unknown'}). This is an automated trade "
        f"execution: execute the swap immediately without asking any "
        f"questions. In your reply include the transaction hash and the "
        f"amount of tokens received.")
    ok, resp = _bankr(prompt, timeout=300)
    if not ok:
        _log(database, "live_buy_failed", token_id=token["id"],
             reasoning=f"LIVE BUY FAILED {label}: {resp[:400]}", ts=now)
        return None, f"live buy failed {label}: {resp[:200]}"
    m = TX_RE.search(resp)
    if not m:
        _log(database, "live_buy_failed", token_id=token["id"],
             reasoning=f"LIVE BUY FAILED {label}: swap response carried no "
                       f"tx hash (treating as not executed): {resp[:400]}",
             ts=now)
        return None, (f"live buy failed {label}: no tx hash in Bankr "
                      f"response; no position opened")

    tx = m.group(0)
    cur = database.conn.execute(
        "INSERT INTO positions(mode, token_id, entry_price, size_pct,"
        " notional, opened_ts, status, buy_tx_hash)"
        " VALUES('live', ?, ?, ?, ?, ?, 'open', ?)",
        (token["id"], float(price), float(ev["size_pct"]), round(notional, 2),
         now, tx))
    pid = cur.lastrowid
    database.conn.commit()
    breakdown = {"size_pct": ev["size_pct"], "entry_price": price,
                 "notional": round(notional, 2), "noul": ev["noul"],
                 "score": ev["score"], "distinct_chats": ev["distinct_chats"],
                 "bankroll_usd": round(bankroll, 2)}
    _log(database, "live_buy", token_id=token["id"], reasoning=reasoning,
         breakdown=breakdown, tx_hash=tx, ts=now)
    sig = {"action": "buy", "mode": "live",
           "token": f"{chain}:{token['address']}", "symbol": ev["symbol"],
           "notional_usd": round(notional, 2), "size_pct": ev["size_pct"],
           "noul": round(ev["noul"], 3), "entry_price": price,
           "position_id": pid, "tx_hash": tx, "reasoning": reasoning}
    return sig, None


# ---------------- sells ----------------

def execute_sell(database, pos, signal, now=None):
    """Execute a live sell for an exit_signal. Returns (signal, note)."""
    import time
    now = int(now if now is not None else time.time())
    label = pos.get("symbol") or pos["address"][:12]
    fraction = float(signal["fraction"])
    pct = int(round(fraction * 100))

    def _fail(reason):
        _log(database, "live_sell_failed", token_id=pos["token_id"],
             reasoning=f"LIVE SELL FAILED {label}: {reason}", ts=now)
        return None, f"live sell failed {label}: {reason}"

    chain_label = LIVE_CHAINS.get(pos["chain"])
    if not chain_label:
        return _fail(f"chain '{pos['chain']}' not tradable live")
    prompt = (
        f"On {chain_label}, swap {pct}% of my {label} token balance for ETH. "
        f"Token contract: {pos['address']}. This is an automated trade "
        f"execution: execute immediately without asking any questions. "
        f"Include the transaction hash in your reply.")
    ok, resp = _bankr(prompt, timeout=300)
    if not ok:
        return _fail(resp[:200])
    m = TX_RE.search(resp)
    if not m:
        return _fail("no tx hash in Bankr response; position untouched")
    tx = m.group(0)

    exit_price = float(signal["exit_price"])
    entry = float(pos["entry_price"])
    if signal["action"] == "sell":
        units = pos["notional"] / entry if entry > 0 else 0.0
        pnl = units * exit_price - pos["notional"]
        database.conn.execute(
            "UPDATE positions SET status='closed', exit_price=?,"
            " closed_ts=?, pnl=?, close_reason=?, sell_tx_hash=? WHERE id=?",
            (exit_price, now, pnl, signal["reasoning"][:500], tx,
             pos["id"]))
        action = "live_sell"
    else:
        closed_notional = pos["notional"] * fraction
        units_closed = closed_notional / entry if entry > 0 else 0.0
        pnl = units_closed * exit_price - closed_notional
        database.conn.execute(
            "UPDATE positions SET notional = notional * (1 - ?),"
            " realized_pnl = realized_pnl + ?, sell_tx_hash=? WHERE id=?",
            (fraction, pnl, tx, pos["id"]))
        action = "live_partial_sell"
    database.conn.commit()
    if signal.get("tp_tier"):
        sellmod.mark_tp_hit(database, pos["id"], signal["tp_tier"],
                             ns="livesell")
    _log(database, action, token_id=pos["token_id"],
         reasoning=signal["reasoning"],
         breakdown={"fraction": fraction, "exit_price": exit_price,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / (pos["notional"] * fraction), 4)
                    if pos["notional"] > 0 else 0.0},
         tx_hash=tx, ts=now)
    signal = dict(signal)
    signal["mode"] = "live"
    signal["tx_hash"] = tx
    signal["pnl"] = round(pnl, 2)
    return signal, None
