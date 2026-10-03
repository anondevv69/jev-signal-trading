"""v1 orchestrator: poll -> extract -> classify intent -> register -> score.

Runs on cron (every 5 min). For each new candidate from the ingesters:
  1. TypeSafe Choice intent classification, once per message (fail-open -> neutral)
  2. first_seen.register_mention (creates the token row on first sight)
  3. if intent == 'call': scoring.record_call (caller attribution)

Prints a one-line summary for the cron log. Never raises on bad data.
"""

import sys
import time

from . import db as dbmod
from . import config as cfgmod
from . import ingest_telegram, ingest_discord
from . import intents as intentsmod
from . import first_seen as fsmod
from . import scoring as scoringmod
from . import theses as thesesmod


def _message_text(database, message_id):
    row = database.conn.execute(
        "SELECT text FROM messages WHERE id = ?", (message_id,)).fetchone()
    return row["text"] if row else ""


def run():
    cfg = cfgmod.load()
    database = dbmod.Database(cfg.db_path)
    t0 = time.time()
    try:
        t_stored, t_cands, t_new, t_noms = ingest_telegram.poll_once(cfg, database)
        d_stored, d_cands, d_new, d_noms = ingest_discord.poll_once(cfg, database)
        candidates = t_cands + d_cands
        for cid, title in t_new:
            print(f"orchestrate: NEW telegram chat: {title} ({cid})")
        for cid, label in d_new:
            print(f"orchestrate: NEW discord channel: {label} ({cid})")

        # group candidates by message; classify once per message
        by_msg = {}
        for c in candidates:
            by_msg.setdefault(c["message_id"], []).append(c)

        n_calls = n_first_seen = 0
        for mid, cands in by_msg.items():
            first = cands[0]
            text = _message_text(database, mid)
            hint = cands[0].get("address", "")
            try:
                intent = intentsmod.classify(text, token_hint=hint)
            except Exception as e:  # classify is fail-open, but belt-and-braces
                print(f"orchestrate: classify failed ({e}), neutral", file=sys.stderr)
                intent = "neutral"
            # thesis ingestion: explicit /thesis or long call/warning
            thesis_dir = thesesmod.detect(text, intent)
            if thesis_dir:
                try:
                    thesesmod.ingest_thesis(
                        database, first.get("platform", ""),
                        first.get("chat_id", ""), first.get("user_id", ""),
                        first.get("username", ""), text, thesis_dir,
                        ts=first.get("ts"), message_id=mid)
                except Exception as e:
                    print(f"orchestrate: thesis ingest failed ({e})",
                          file=sys.stderr)
            for c in cands:
                try:
                    weight = scoringmod.get_weight(
                        database, c.get("platform", ""),
                        c.get("user_id", ""), c.get("chat_id", ""))
                except Exception:
                    weight = 1.0
                token, is_first = fsmod.register_mention(
                    database, c.get("chain", "evm"), c.get("address", ""),
                    symbol=c.get("symbol", ""), chat_id=c.get("chat_id", ""),
                    ts=c.get("ts"), message_id=mid,
                    intent=intent, caller_weight=weight)
                if is_first:
                    n_first_seen += 1
                if intent == "call":
                    try:
                        scoringmod.record_call(
                            database, c.get("platform", ""),
                            c.get("user_id", ""), c.get("username", ""),
                            c.get("chat_id", ""), token["id"], ts=c.get("ts"))
                        n_calls += 1
                    except Exception as e:
                        print(f"orchestrate: record_call failed ({e})",
                              file=sys.stderr)
        # Self-heal: any message whose text contains an address but has no
        # linked mentions (e.g. a past run stored the message but dropped the
        # candidate) gets processed through the normal path. Cheap: one query.
        n_healed = _backfill_missed(database)
        # Reply nominations: the reply MUST carry the contract address
        # (disambiguates when the original mentions several tokens). The
        # thesis is attributed to the ORIGINAL author; the stored body is
        # the original post.
        n_noms = 0
        for nom in t_noms + d_noms:
            try:
                direction = nom.get("direction")
                if not direction:
                    try:
                        orig_intent = intentsmod.classify(nom["orig_text"])
                    except Exception:
                        orig_intent = "neutral"
                    direction = (thesesmod.detect(nom["orig_text"], orig_intent)
                                 or "bull")
                n_noms += thesesmod.ingest_thesis(
                    database, nom["platform"], nom["chat_id"],
                    nom["orig_user_id"], nom["orig_username"],
                    nom["orig_text"], direction, ts=nom.get("ts"),
                    message_id=None,
                    candidate_text=nom.get("reply_text", ""))
            except Exception as e:
                print(f"orchestrate: nomination failed ({e})", file=sys.stderr)
        # Retroactive thesis scoring (max 10 intel lookups per run).
        try:
            n_scored = thesesmod.score_theses(database)
        except Exception as e:
            print(f"orchestrate: thesis scoring failed ({e})", file=sys.stderr)
            n_scored = 0
        dt = time.time() - t0
        print(f"orchestrate: stored={t_stored + d_stored} "
              f"candidates={len(candidates)} calls={n_calls} "
              f"first_seen={n_first_seen} healed={n_healed} "
              f"nominations={n_noms} "
              f"theses_scored={n_scored} ({dt:.1f}s)")
    finally:
        database.close()


def _backfill_missed(database):
    """Reconcile messages with address-like text but zero linked mentions."""
    from . import extract as extractmod
    healed = 0
    rows = database.conn.execute(
        "SELECT m.id, m.platform, m.text, m.chat_id, m.user_id, m.username, m.ts "
        "FROM messages m LEFT JOIN mentions me ON me.message_id = m.id "
        "WHERE me.id IS NULL").fetchall()
    for mid, platform, text, chat_id, user_id, username, ts in rows:
        cands = extractmod.extract_candidates(text or "")
        if not cands:
            continue
        try:
            intent = intentsmod.classify(text, token_hint=cands[0]["address"])
        except Exception:
            intent = "neutral"
        for c in cands:
            try:
                weight = scoringmod.get_weight(
                    database, platform, user_id, str(chat_id))
            except Exception:
                weight = 1.0
            try:
                token, _ = fsmod.register_mention(
                    database, c.get("chain", "evm"), c.get("address", ""),
                    symbol=c.get("symbol", ""), chat_id=str(chat_id),
                    ts=ts, message_id=mid, intent=intent,
                    caller_weight=weight)
                if intent == "call":
                    try:
                        scoringmod.record_call(
                            database, platform, user_id, username,
                            str(chat_id), token["id"], ts=ts)
                    except Exception:
                        pass
                healed += 1
            except Exception as e:
                print(f"orchestrate: backfill failed msg {mid} ({e})",
                      file=sys.stderr)
    return healed


def main():
    run()


if __name__ == "__main__":
    main()
