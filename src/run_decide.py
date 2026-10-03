"""Decision orchestrator (v2). PAPER ADVISORY ONLY.

Runs the buy pass over tokens with new mentions since the last run, then the
sell loop over open paper positions. Prints a single JSON summary to stdout
for the cron worker / alerter. A `decide:last_ts` watermark in ingest_state
prevents reprocessing.

Intended cadence: every ~15 minutes (separate cron from the 5-min ingest).

Never raises: per-token and per-position errors are collected into notes.
"""

import json
import sys
import time

from . import db as dbmod
from . import config as cfgmod
from . import paper as papermod
from . import decide as decidemod
from . import sell as sellmod

LAST_TS_KEY = "decide:last_ts"
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


def run():
    cfg = cfgmod.load()
    database = dbmod.Database(cfg.db_path)
    now = int(time.time())
    summary = {"ts": now, "tokens_evaluated": 0, "buys": [], "sells": [],
               "notes": []}
    try:
        last_ts = database.get_state(LAST_TS_KEY, "")
        since = int(last_ts) if last_ts.isdigit() else now - FIRST_RUN_LOOKBACK_H * 3600
        paper = papermod.PaperPortfolio(database, cfg.paper_start_balance)

        tokens = _tokens_with_new_mentions(database, since)
        summary["tokens_evaluated"] = len(tokens)
        try:
            buy_signals, buy_notes = decidemod.decide_buys(
                database, tokens, paper, now=now)
        except Exception as e:
            buy_signals, buy_notes = [], [f"buy pass failed: {e}"]
        summary["buys"] = buy_signals
        summary["notes"].extend(buy_notes)

        try:
            sell_signals, sell_notes = sellmod.run_sells(database, paper, now=now)
        except Exception as e:
            sell_signals, sell_notes = [], [f"sell pass failed: {e}"]
        summary["sells"] = sell_signals
        summary["notes"].extend(sell_notes)

        summary["open_positions"] = len(paper.open_positions())
        summary["paper_cash"] = round(paper.cash(), 2)
        database.set_state(LAST_TS_KEY, str(now))
    except Exception as e:
        summary["notes"].append(f"run_decide fatal: {e}")
    finally:
        database.close()
    print(json.dumps(summary, indent=2))
    return summary


def main():
    run()


if __name__ == "__main__":
    main()
