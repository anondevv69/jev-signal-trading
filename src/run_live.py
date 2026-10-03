"""Live trading loop.

Every run:
  1. Buy pass: tokens with new mentions since the last run are evaluated
     with decide.evaluate_token(live=True) — call-intent + caller
     reputation + >=2 chats + safety veto + Jev buy Noul >= 0.70
     (fail-closed). 'buy' evals go to live.execute_buy, which submits a
     real Bankr swap and records the live position with its tx hash.
  2. Sell pass: open live positions are checked with sell.exit_signal
     (stop-loss -40%, take-profit tiers, sentiment exit, warning kill, time
     exit); triggered exits go to live.execute_sell (real Bankr swap).

Authorized by the operator 2026-10-02: full trading discretion, no kill switch,
no per-trade approval. Mandate:
MANDATE.md (operator-defined)

Prints a single JSON summary to stdout for the cron worker. The cron
reports to the operator ONLY when buys/sells executed (with thesis + tx hash)
or when the run itself failed.

Never raises: per-token and per-position errors are collected into notes.
"""

import json
import time

from . import db as dbmod
from . import config as cfgmod
from . import decide as decidemod
from . import sell as sellmod
from . import live as livemod

LAST_TS_KEY = "live:last_ts"
FIRST_RUN_LOOKBACK_H = 24  # on first run, consider the last 24h of mentions


def _tokens_with_new_mentions(database, since_ts):
    rows = database.conn.execute(
        "SELECT DISTINCT me.token_id FROM mentions me WHERE me.ts >= ?",
        (int(since_ts),),
    ).fetchall()
    out = []
    for r in rows:
        t = database.conn.execute(
            "SELECT * FROM tokens WHERE id = ?", (r["token_id"],)).fetchone()
        if t:
            out.append(dict(t))
    return out


def _publish_trade_alerts(buys, sells):
    """Broadcast executed trades to the agent alpha feed. Never raises."""
    for sig in buys:
        try:
            chain, address = sig["token"].split(":", 1)
        except ValueError:
            chain, address = "", sig.get("token", "")
        livemod.publish_alert({
            "decision": "buy",
            "chain": chain, "address": address,
            "symbol": sig.get("symbol") or "",
            "noul": sig.get("noul"),
            "size_pct": sig.get("size_pct"),
            "price_usd": sig.get("entry_price"),
            "tx_hash": sig.get("tx_hash") or "",
            "thesis": (sig.get("reasoning") or "")[:2000],
            "engine": "fren-signal-trading",
        })
    for sig in sells:
        try:
            chain, address = sig["token"].split(":", 1)
        except ValueError:
            chain, address = "", sig.get("token", "")
        livemod.publish_alert({
            "decision": "sell",
            "chain": chain, "address": address,
            "symbol": sig.get("symbol") or "",
            "price_usd": sig.get("exit_price"),
            "tx_hash": sig.get("tx_hash") or "",
            "thesis": (sig.get("reasoning") or "")[:2000],
            "engine": "fren-signal-trading",
        })


def run():
    cfg = cfgmod.load()
    database = dbmod.Database(cfg.db_path)
    now = int(time.time())
    summary = {"ts": now, "mode": "live", "tokens_evaluated": 0,
               "buys": [], "sells": [], "notes": [],
               "bankroll_usd": None, "open_live_positions": 0}
    try:
        last_ts = database.get_state(LAST_TS_KEY, "")
        since = (int(last_ts) if last_ts.isdigit()
                 else now - FIRST_RUN_LOOKBACK_H * 3600)
        bankroll = livemod.bankroll_usd()
        summary["bankroll_usd"] = bankroll

        # ---- buy pass ----
        tokens = _tokens_with_new_mentions(database, since)
        summary["tokens_evaluated"] = len(tokens)
        for token in tokens[:decidemod.MAX_TOKENS_PER_RUN]:
            try:
                ev = decidemod.evaluate_token(database, token, now=now,
                                              live=True)
            except Exception as e:
                summary["notes"].append(
                    f"live eval failed for token {token['id']}: {e}")
                continue
            if ev["decision"] != "buy":
                continue
            try:
                sig, note = livemod.execute_buy(database, token, ev,
                                                bankroll, now=now)
            except Exception as e:
                sig, note = None, f"live buy crashed: {e}"
            if sig:
                summary["buys"].append(sig)
            if note:
                summary["notes"].append(note)

        # ---- sell pass (runs even if the bankroll query failed) ----
        for pos in livemod.open_live_positions(database):
            try:
                signal = sellmod.exit_signal(database, pos, now=now,
                                             ns="livesell")
            except Exception as e:
                summary["notes"].append(
                    f"live sell check failed for position {pos['id']}: {e}")
                continue
            if not signal:
                continue
            if signal["action"] == "note":
                summary["notes"].append(signal["reasoning"])
                continue
            try:
                sig, note = livemod.execute_sell(database, pos, signal,
                                                 now=now)
            except Exception as e:
                sig, note = None, f"live sell crashed for {pos['id']}: {e}"
            if sig:
                summary["sells"].append(sig)
            if note:
                summary["notes"].append(note)

        summary["open_live_positions"] = len(
            livemod.open_live_positions(database))
        database.set_state(LAST_TS_KEY, str(now))

        # broadcast executed trades to the agent alpha feed
        if summary["buys"] or summary["sells"]:
            try:
                _publish_trade_alerts(summary["buys"], summary["sells"])
            except Exception as e:
                summary["notes"].append(f"alert broadcast failed: {e}")
    except Exception as e:
        summary["notes"].append(f"run_live fatal: {e}")
    finally:
        database.close()
    print(json.dumps(summary, indent=2))
    return summary


def main():
    run()


if __name__ == "__main__":
    main()
