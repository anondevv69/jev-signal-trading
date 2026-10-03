# Jev Signal Trading

An autonomous crypto signal-trading engine for AI agents. It watches Telegram
and Discord chats for token mentions, scores them with caller reputation +
market/safety/deployer intel, asks [Jev](https://typesafe.ai) (TypeSafe's
calibrated judgment model) whether each is a good buy, and — in live mode —
executes through a [Bankr](https://bankr.bot) wallet with full position
management.

Part of the open agent alpha network: every executed trade is published to a
public broadcast feed so other agents can judge and follow.

```
alerts  ->  chat intel (Telegram/Discord)
judge   ->  Jev buy Noul + safety vetoes + caller reputation + theses
execute ->  Bankr wallet swaps, stop-loss / take-profit / sentiment exits
publish ->  POST /v1/signal-alerts (agent broadcast feed)
```

## Layout

```
src/
  config.py          env-based config. No secrets in code, ever.
  db.py              SQLite schema + helpers (all queries parameterized)
  extract.py         regex extraction: EVM addrs, Solana mints, cashtags, DEX URLs
  discover.py        chat auto-discovery (bot records every chat it's added to)
  ingest_telegram.py Bot API long-polling -> messages + candidates
  ingest_discord.py  REST polling per channel -> messages + candidates
  orchestrate.py     ingest loop: poll -> extract -> classify -> register -> score
  intents.py         TypeSafe Choice gate: call|warning|deployment|neutral|...
                     FAIL-OPEN: any error returns "neutral"
  first_seen.py      "chain:address" registry, mention velocity, cross-chat breadth
  scoring.py         caller reputation: record_call / update_outcome / weights
  theses.py          signed token theses (/thesis command + implicit), retroactive
                     scoring, author accuracy
  intel.py           per-token intel: DexScreener market data, honeypot.is safety,
                     Robinhood blockscout deployer lookup. All fail-open.
  judge.py           shared TypeSafe Noul gate. FAIL-CLOSED: None on any error,
                     callers must skip the action (never act blind).
  decide.py          buy engine: score -> preconditions -> Noul gate -> signal
  sell.py            pure exit_signal(): stop-loss, take-profit tiers, warning
                     kill, sentiment exit, time exit (shared by paper + live)
  paper.py           paper portfolio: open/close/partial-close, P&L
  live.py            live executor: Bankr natural-language swaps, live ledger,
                     publish_alert() to the broadcast feed
  run_live.py        live loop: buy pass + sell pass -> JSON summary
  run_decide.py      paper loop (control track): buy pass + sell pass -> JSON
  creds.py           Secure Vault (authd surrogate) access for bot tokens
```

## Setup

```bash
export TELEGRAM_BOT_TOKEN=<redacted>
export DISCORD_BOT_TOKEN=<redacted>
export TELEGRAM_CHAT_IDS="-100123,-100456"   # or leave empty: auto-discovery
export DISCORD_CHANNEL_IDS="123,456"
export POLL_INTERVAL_SEC=300
export DB_PATH=./data/signal.db
export PAPER_START_BALANCE=10000
# live trading only:
export BANKR_API_KEY=...          # Bankr Agent API key (wallet with funds)
export LIVE_MAX_DEPLOYED_PCT=80
export LIVE_MAX_POSITIONS=8
```

TypeSafe (Jev) needs `TYPESAFE_API_KEY` in the environment, or the
`custom.typesafe-ai` credential when running inside a Muse sandbox.

## Running

```bash
python3 -m src.orchestrate   # ingest loop (5-min cron): poll -> classify -> register -> score theses
python3 -m src.run_decide    # paper loop (15-min cron): buy pass + sell pass -> JSON
python3 -m src.run_live      # LIVE loop (15-min cron): real Bankr swaps + broadcast
python3 src/extract.py       # regex self-test
```

Each loop prints one JSON summary. Wire the cron to report only on executed
trades (with thesis + tx hash) or on failure — silence is the healthy state.

## Theses

Anyone in a monitored chat can post a thesis:

```
/thesis bull 0xabc... team is doxxed, LP locked, chart breaking out
```

Long call-intent messages (200+ chars) are also ingested as implicit bull
theses; long warnings as bear theses. **Reply nominations**: reply to any
post with the contract address — `/thesis 0xabc...` — and the *original*
post becomes a thesis on that token, attributed to its author. The reply
must carry the CA (this also disambiguates when the original mentions
several tokens). Every thesis is graded retroactively
(24h, ±15% decides correct/wrong) and author accuracy feeds back into Jev's
judgment input. Spam defense is reputation: anyone can write, only scored
authors move the Noul.

## Broadcast

`live.publish_alert()` POSTs every executed trade to the agent alpha feed:

- `GET https://api-production-8630.up.railway.app/v1/signal-alerts` (public)
- Agents subscribe for push via musemaxxing webhooks (`signal_alert` events)

Only real-money executions are published — never paper calls.

## Safety

- Buy judgment is fail-closed: if Jev is unreachable, nothing is bought.
- Safety veto: honeypot / extreme-tax tokens are never bought, regardless of Noul.
- Live mode requires explicit operator authorization; the wallet is the risk cap.
- No private keys in code — Bankr holds custody, the engine only prompts.

## License

MIT
