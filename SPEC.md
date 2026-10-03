# Signal Trading System — Build Spec

**Status:** design phase (2026-09-29). Nothing built yet; paper-trade first, live only after validation.
**Owner:** the operator. **Builder:** the engine.
**One-line concept:** Listen to 5 independent group chats (Telegram/Discord/X), detect tokens gaining genuine cross-chat consensus *before* they hit mainstream, score them on caller reputation + deployer intel + contract safety, and auto-trade them — buys and sells — from a dedicated wallet.

Inspired by Rick (first-seen call tracking), taken further: full chat-context understanding, caller reputation weighting, deployer forensics, and autonomous execution.

---

## 1. Architecture overview

```
INGEST (cron, every ~5 min, zero LLM cost)
  └─ poll each chat for new messages → regex scan for CAs/tickers/cashtags
       └─ candidates → EXTRACT

EXTRACT (TypeSafe Choice — intent classification)
  └─ per candidate message: call | scan-request | warning | deployment | neutral | bot-output
       └─ calls/warnings → LINK

LINK (first-seen registry)
  └─ normalize token (chain + contract) → first-seen timestamp, Nth-mention count,
     per-chat mention log, caller attribution
       └─ tracked tokens → INTEL

INTEL (async, per new token)
  ├─ deployer dossier (wallet, track record, socials, skin in the game)
  ├─ contract safety (honeypot, taxes, ownership, verification)
  ├─ liquidity health (depth, lock %, lock duration)
  ├─ holder distribution (concentration, bundlers, growth)
  └─ market momentum (price, volume, buys/sells)

JUDGE (TypeSafe gates — cheap, fast, calibrated)
  ├─ shill-vs-organic Noul per mention cluster
  ├─ warning Noul (does recent chatter flag scam/honeypot?)
  ├─ deployer trust Score (1–5)
  └─ buy Noul → calibrated probability → position size tier

DECIDE (every ~15 min, only tokens with new activity)
  └─ token score = Σ (caller weight × intent × recency × chat independence)
       × deployer trust × safety pass × TypeSafe buy-noul
       └─ above threshold → EXECUTE (paper or live)

EXECUTE (Bankr Wallet API)
  └─ /wallet/swap-quote → /wallet/swap → log receipt (token, size, price, tx, reasoning)

SELL LOOP (every ~15 min per open position)
  └─ stop-loss / take-profit tiers / trailing / time-exit / sentiment Noul
       └─ fire market swap via Bankr (no native stop/limit orders — we own this loop)
```

**Design principles:**
- Filter-first: scripts do polling/regex (free); LLM/TypeSafe only touches candidate messages.
- Consensus over speed: the edge is 3–4 *independent* chats converging, which takes minutes–hours. A 5-min poll delay costs nothing.
- Fail-open: if TypeSafe or an intel API is down, tokens queue for the slower path; nothing blocks, nothing buys blind.
- Every decision logged with reasoning (token, score breakdown, size, tx hash) — morning receipts.

---

## 2. Ingestion layer

### Telegram
- **Primary: Bot API.** Create bot via BotFather, add to each group (admin or privacy-mode-off so it sees all messages). Long-poll `getUpdates` every ~5 min. Simple, no login as the operator.
- **Fallback: Telethon (MTProto user client).** Logs in as the operator's account; sees everything he sees, including groups that won't accept a bot. Needs his login + 2FA; datacenter IPs can get flagged. Use only where the bot can't go.
- **Base code:** `telegram-watcher` (open-source) already does Telethon streaming + Rick-message detection + token extraction + SQLite. Fork candidate — **audit wallet/trading code line-by-line before any funds touch it.**

### Discord
- **Proper bot application** (Developer Portal) invited to each server with read-messages + message-content intent. **Poll** `GET /channels/{id}/messages?after={last_id}` on the cron — no persistent gateway needed.
- Never automate the operator's personal account (self-bot = ToS violation, ban risk). Requires a server admin to add the bot.

### X group chats
- **TBD.** X API access for group DMs is restricted — verify what tier we have. Browser fallback if needed.

### Backfill (day-one bootstrap)
- On joining each chat, read back weeks of history to reconstruct past calls + outcomes → caller reputation leaderboard exists from day one.

---

## 3. Extraction & intent classification

Regex finds the contract/ticker; **TypeSafe Choice** decides what the mention *means*:

| Intent | Meaning | Scoring effect |
|---|---|---|
| `call` | Genuine buy call ("ape in", "this is the one", "just bought") | Buy vote, weighted by caller reputation |
| `scan-request` | "Can someone scan this? Is it safe?" | No buy vote. Token goes on watchlist; clustered scan-requests (5+ people, 3+ chats, 1h) = emerging-interest signal |
| `warning` | "Don't touch this", "honeypot", "dev dumped" | Negative vote; can kill a buy |
| `deployment` | Dev/team announcing their own launch | Feeds deployer intel, not caller score |
| `neutral` | Chart link, info sharing, off-topic | Ignored |
| `bot-output` | Rick's own scan replies etc. | Ignored (not human opinion) |

Nuance: the *replier* matters — if someone scans and a respected caller replies "looks clean, I'm in," that's the replier's call.

---

## 4. First-seen registry (Rick-style, cross-chat)

- Global first-seen timestamp per token across all 5 chats ("FIRST" tag concept).
- Nth-mention counter + mention velocity (mentions/hour).
- Per-chat mention log with caller attribution and intent.
- Key metric: **how early** the system caught it + **how fast** independent mentions accumulate.

---

## 5. Caller reputation scoring

**Tracked per user per chat:** every CA/ticker share logged with token price + mcap at call time; token tracked at 1h / 24h / 7d; did it run or rug?

**Stats per user:** total calls, win rate, avg return, rug-call rate, earliness (first-mention ratio).

**Weights:** everyone starts neutral at 1.0x. Proven callers (10+ calls minimum sample, strong hit rate) drift to 2–3x. Serial shillers with rug trails drift toward 0x, optionally negative as a contrarian signal. Recent performance weighted over old. (Bayesian shrinkage toward mean — small samples don't move weight much.)

**Anti-gaming:** only first-mentions per token count fully; rug-calls actively hurt score; score spans all 5 chats (can't fake reputation in one room); track record is expensive to fake.

**Decision input:** token score = Σ over mentions (caller weight × intent × recency × chat-independence). Two 3x callers in two chats outscore ten 0.3x shillers in one chat. Quality of voice beats volume of noise.

---

## 6. Deployer dossier

Separate from chat signals. For each new token's deployer + fee recipient:

**Who are they**
- Deployer wallet + fee recipient addresses — same wallet or different? (Bankr `get_clanker_reward_ownership` for fee side)
- Fresh vs reused wallet (tx count, age); funding source (CEX, bridge, another dev wallet — cluster linkage)
- Social handle → wallet resolution; does that wallet have *any* footprint on the chain? (Rugs usually don't.)

**Track record**
- Deploy count; outcomes per deploy (rugged / alive / multiples / median lifespan)
- Dev behavior: time from deploy to first dev sell; % of supply held vs dumped
- Serial-launcher pattern (5 deploys/week = farm, not founder)

**Real person check**
- Deployment tweet exists? Dev's other tweets — real history vs 3-day-old account
- Follower count, account age, follower quality; Telegram/Discord presence (real members vs bot-filled)

**Skin in the game**
- Deployer wallet balance (public on-chain); did they buy their own token or only hold free allocation?
- Fee extraction behavior — claimed and dumped vs left alone

**Instant kill signals:** fee recipient = deployer only (no ecosystem/multisig split); deployer-funded sniper wallets at genesis; liquidity pulled shortly after adding; prior rug flagged by investigators.

*Note: no single API gives "deploy count + outcomes per deployer" — we build this indexer ourselves (very doable on Robinhood Chain via direct RPC). Bankr's `bankr-token-scam-analysis` skill is the reference playbook ("narrative is noise, on-chain state is signal").*

---

## 7. Token intel (per new token)

- **Contract safety:** honeypot check (can it be sold?), buy/sell taxes, ownership renounced?, mint/freeze authority, upgradeable proxy?, source verified?
- **Liquidity health:** pool depth, % locked/burned, lock duration. (Swap quote shows *our* price impact, not pool health.)
- **Holder distribution:** top-holder concentration, dev/insider wallets, bundler/sniper clusters, holder growth rate.
- **Market momentum:** price, volume, buys vs sells, 5m/1h/24h change (DexScreener free API where chain is indexed).
- **Chain coverage TBD:** verify which intel APIs index our target chains (esp. Robinhood Chain).

---

## 8. TypeSafe judgment layer

| Gate | Type | Purpose |
|---|---|---|
| Intent classification | Choice | call / scan-request / warning / deployment / neutral / bot-output — runs on every candidate message |
| Shill vs organic | Noul | Is this mention cluster coordinated shilling or organic discussion? |
| Warning detection | Noul | Does recent chatter flag scam/honeypot/rug? |
| Deployer trust | Score (1–5) | Dossier synthesis into a graded trust level |
| **Buy gate** | Noul | Given everything: is this a good buy? Calibrated p → size tier |
| Sell sentiment | Noul / Choice | Has sentiment turned negative? hold / take-profit / stop-out |

**Why TypeSafe here:** fractions of a cent per call, fast enough for the poll loop, calibrated probabilities. Auth via existing `custom.typesafe-ai` (cron needs no new secrets — same pattern as the odds-scan semantic gate). **Fail-open:** on outage, tokens queue for the slower LLM path. Heavy reasoning (dossier synthesis, edge cases) stays on full LLM — TypeSafe is the fast filter, not the analyst.

---

## 9. Decision engine (buy)

Every ~15 min, for tokens with new activity:

```
token_score = Σ(caller_weight × intent × recency × chat_independence)
              × deployer_trust(0–1) × safety_pass(0/1)
              × typesafe_buy_noul
```

- `safety_pass` = 0 on any kill signal (honeypot, ownership red flags) → never buys, no matter the hype.
- Buy threshold TBD in paper trading.
- Minimum: signal must span ≥2 independent chats (single-chat hype never buys alone).

---

## 10. Position sizing

Percentage-of-holdings (auto-scales with wins/losses), mapped from TypeSafe buy-noul:

| Conviction | Noul | Size |
|---|---|---|
| Watch / weak | < 0.70 | no buy (watchlist only) |
| Standard | 0.70–0.79 | 5% of holdings |
| Strong | 0.80–0.89 | 10% of holdings |
| High conviction | ≥ 0.90 | 15–20% of holdings |

- Max ~80% of wallet deployed at once (dry powder + gas reserve).
- Cap exposure per narrative/chain (correlated positions aren't diversification).
- Sizes computed on *current* holdings — losers automatically shrink future bets.
- Starting paper-trade at flat 10%; tiers unlock when data earns them.

---

## 11. Sell engine

Bankr has no native stop/limit orders — this is our poll-and-fire loop (every ~15 min per open position):

- **Stop-loss:** fixed % below entry (number TBD — the operator's call).
- **Take-profit tiers:** e.g., sell 50% at 2x, 25% at 5x, let the rest ride (tiers TBD).
- **Trailing:** once up significantly, stop follows price at a set distance.
- **Time exit:** dead token (no momentum, no chatter for N hours) → exit, free the capital.
- **Sentiment exit:** TypeSafe Noul on recent chatter turning negative → reduce/exit.
- **Warning kill:** credible warning (honeypot discovered, dev dump) → immediate full exit.

---

## 12. Execution (Bankr)

- Read: `/wallet/me`, `/wallet/portfolio`, `/wallet/swap-quote` (read-only key OK).
- Write: `/wallet/swap`, `/wallet/transfer` — needs **read-write key + written standing mandate**.
- Every trade logged: timestamp, token, size, entry price, tx hash, score breakdown, reasoning.
- Natural-language fallback: Agent API (`/agent/prompt`) for anything the direct endpoints can't do.

---

## 13. Risk & guardrails

- **Dedicated trading wallet = the cap.** No per-trade or daily caps; the bot has free rein *inside* the wallet. Fund it with only what's comfortable to lose. Trading never touches launch/main funds.
- **Kill-switch phrase** (TBD — the operator provides): halts all buying *and* selling loops instantly, no questions.
- **Safety pass is absolute:** contract kill signals veto any buy regardless of score.
- **Paper-trade first:** identical analysis, fake execution. Go-live only after validation (terms TBD).
- **Morning receipts:** positions, P&L, every decision with reasoning. Full audit trail on demand.
- **Kill switch + mandate** written to the standing mandate file before live trading.

---

## 14. Cost model (rough, steady state)

| Stage | Daily volume | LLM cost |
|---|---|---|
| Polling + regex filter | ~1,400 polls | 0 (scripts) |
| Intent classification (TypeSafe) | 100–300 candidate msgs | fractions of a cent |
| Deployer dossiers | 20–50 new tokens | ~40–60K tokens (verdict synthesis only; RPC/API calls free) |
| Scoring runs | 96/day, activity-filtered | ~50–100K tokens |
| Execution (Bankr API) | per trade | 0 |
| **Total** | | **~150–350K tokens/day + TypeSafe micro-calls** |

Paper-trade costs the same as live (analysis is identical) — no reason not to validate first.

---

## 15. Base code

- **Fork candidate:** `coinspiracynut/telegram-watcher` (Telethon + Rick detection + token extraction + auto-buy + position monitor + SQLite). **Security audit of all wallet/trading code required before funds.**
- Swap its Solana execution (SolanaTracker) → Bankr Wallet API for our chains.
- Add our layers on top: caller reputation, deployer dossier indexer, cross-chat consensus, TypeSafe gates, full sell engine.
- Reference: Bankr `bankr-token-scam-analysis` skill (forensic playbook); `argus-telegram-bot` (cross-group alpha concept); `daemonbot` (call leaderboards, MIT).

---

## 16. Open questions (need the operator)

1. The five chats — invite links + can a bot be added to each?
2. Which wallet funds trading — Bankr account or fresh dedicated wallet?
3. Which chains — Solana, Base, Robinhood Chain, all?
4. Sell numbers — stop-loss %, take-profit tiers.
5. Kill-switch phrase.
6. Paper-trade terms — duration + what result means "go live."
7. Greenlight to audit the telegram-watcher repo.
