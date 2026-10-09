# Research-driven trading: design

Date: 2026-10-09. Status: approved in conversation; written up for the record.

## Goal

Stop maintaining a symbol list. Scheduled research jobs scan the market (movers,
earnings, news, regime), have Bedrock dig into the best candidates, and record ranked
picks plus a day "posture". The bot trades only those picks, through its existing
risk checks, and sits out days the research says are bad. A private web app tunes
every setting and reports on the picks, the trades and how well the ranking works.

Not financial advice: research picks are inputs to a strategy the owner chooses.

## Decisions made

| Question | Decision |
|---|---|
| Research's say over trades | Picks a universe and scores each name. A deterministic strategy and the risk checks decide orders. |
| Holding period | Mix: intraday and swing (days to weeks). The cash split is a setting. |
| Mid-session changes | Intraday scans can add symbols during the session. |
| Bearish picks | Long puts now. Real short selling is a later sub-project. |
| Budget | $100+/month for market data and Bedrock. |
| Research shape | Code screen, then a bounded Bedrock agent per candidate, then code rank and validate. |
| Web app | React + TypeScript SPA, Python JSON API, private network only, login on top. |
| Web app powers | Everything, including the control switch and every limit. Every change is versioned, audited and alerted. |
| No forced trades | No minimum trade count anywhere. A zero-trade day is a normal, reported outcome. |
| After-hours earnings | Watching (research only) now. Trading after hours later. |

## Sub-projects

Each gets its own plan and PR, built in this order.

| | Piece | Depends on |
|---|---|---|
| A | Settings and research store; the bot reads both | none |
| B | Web app: settings editor and reports | A |
| C | Research jobs | A |
| D | A strategy that uses picks and scores (the owner's choice) | A, C |

Later, separately: real short selling (margin account, borrow checks, short-sale
restriction, hard stops); trading in extended hours; holding through earnings.

## Architecture

```
EventBridge Scheduler
  ├─ 08:00 ET   pre-market ─┐
  ├─ every N min intraday ──┤  Step Functions: collect → screen → Map(deep-dive) → rank+validate → write
  ├─ Sun        weekly ─────┤                                         (Bedrock tool loop, Lambda)
  ├─ 1st wknd   monthly ────┤
  ├─ after close earnings ──┘
  └─ 16:30 ET   EOD scorecard (forward returns for every pick)
                      │
                      ▼
   DynamoDB research table (runs, picks, posture, regime, outcomes) + S3 (raw inputs, tool trails)
   DynamoDB settings table (versioned, audited)
                      │
         ┌────────────┴─────────────┐
         ▼                          ▼
   ECS bot (existing)          Web app API (Lambda) + SPA (private S3, internal entry point)
   reloads settings, polls picks    settings editor + reports
```

---

## A. Settings and research store; the bot reads both

This is the contract every other piece talks through. It also changes how the bot
chooses what to trade.

Decisions made while building A (plan A2) that the sections below reflect:

* **Research is opt-in per stack** (`traider:research: true`). Until the research jobs
  exist (C), a stack with research on would stand aside every day, so it stays off by
  default. Without it there is no research table, no ledger and none of the research rules.
* **`pinned_symbols` applies live.** The universe changes while the bot runs.
* **Posture applies to pinned symbols.** Pinned symbols are exempt only from `no_pick` and
  `pick_side`.
* **The ledger is used only with research on.** Without research, a held position in a
  pinned symbol is the bot's, as before.
* **Research only blocks or shrinks entries.** Exits never depend on it. The one research
  rule that also blocks sells is `foreign_holding`.

### A.1 Configuration split

`Config` today holds both deploy facts and tunable behaviour. It splits:

* **`Config`** (environment, fixed for the life of the process): trading mode,
  account, region, secret ids, table names, control parameter, alert topic, feed
  type, heartbeat, log level, and the new `settings_table` and `research_table`
  (`research_table` is set only when research is on; it is what switches research on).
* **`Settings`** (DynamoDB, versioned): strategy and its parameters, `risk`
  (`RiskLimits`), order type, limit offset, order timeout, `flatten_before_close_min`,
  `cancel_unknown_orders`, option chain span, `pinned_symbols`, and a new `research`
  block (below).

`trading_mode` stays a deploy fact: it picks the broker (paper or Schwab), which
cannot change under a running process. The web app controls the SSM control switch,
which is what turns live trading on and off in a live stack.

**Without a settings table** (local runs, tests, backtests), `Settings` is built from
the same `TRAIDER_*` variables as today, and the bot behaves as it does now.
`TRAIDER_SYMBOLS` becomes `pinned_symbols` and is no longer required: an empty list
is valid when a research table is configured. Without research, at least one
pinned symbol is still required, both in the environment and in every settings
version: a version with none is rejected (`settings_rejected`) when research is off, and
the bot keeps the settings it had.

**Applying a new version.** The bot reads the newest version every 10 s, like the control
switch. A new version is validated with the same pydantic model.

* Fields that can apply at once: `risk` (except `allow_options`), order parameters,
  `research`, `cancel_unknown_orders`, flatten timing, and **`pinned_symbols`**. A newly
  pinned symbol joins the universe at once. A symbol that is unpinned stays in the
  universe while the bot holds it or has an order working on it (A.5), and the bot raises
  `unpinned_but_held` if it still holds it. They take effect on the next engine step, and
  a `settings_applied` event records the version.
* Fields that need a restart: `strategy`, `strategy_params`, option chain span,
  `risk.allow_options`. The
  bot keeps running on the old values, records `settings_pending_restart` and alerts
  once per version.
* An invalid version (it should not get past the writer, but the bot does not trust
  that) is ignored. The bot keeps the last good one, records `settings_rejected`
  and alerts.
* An unreadable table means the last good version stays in force. If no version has
  ever been read in this process, entries are off (`entries_halted`: "settings not
  loaded").
* **Bootstrap:** if the table has no version yet, the bot writes version 1 from the
  environment with a conditional put (`author: bootstrap`). A deploy therefore
  seeds the settings once and never overwrites them afterwards.

Implementation note: `Config` keeps its tunable fields as the bootstrap source; the
engine, feed and strategy read `Settings` at runtime.

### A.2 Settings table

`{prefix}-settings`, keys `pk`/`sk`, on demand, point-in-time recovery, deletion
protection in live stacks.

| pk | sk | Body |
|---|---|---|
| `SETTINGS` | `V#<000000n>` | Full settings JSON, `version`, `author`, `at`, `note`, `diff` against the previous version. Immutable. |

The current version is the highest-numbered `V#` item. A write is one conditional put
of `V#n+1` (`attribute_not_exists`), so two editors cannot both write version n+1;
the loser gets a conflict and re-reads. Rollback writes a new version whose body is an old one.
Alerts fire on any version that changes `risk` or the control switch (the switch
itself stays in SSM).

### A.3 Research table

`{prefix}-research`, keys `pk`/`sk`, GSI `gsi1` (`gsi1pk`, `gsi1sk`), on demand,
point-in-time recovery. Written by research (C), read by the bot and the web app.

| pk | sk | Holds |
|---|---|---|
| `RUN#<run_id>` | `META` | `kind` (premarket, intraday, weekly, monthly, earnings_watch, scorecard), `status` (running, ok, partial, failed), `started_at`, `finished_at`, `trading_day`, model ids, tokens, `cost_usd`, `s3_prefix`, `error` |
| `DAY#<date>` | `PICK#<run_id>#<rank:03d>` | `Pick` (below). `gsi1pk = SYM#<symbol>`, `gsi1sk = <date>#<run_id>` |
| `DAY#<date>` | `POSTURE#<iso ts>` | `level` (trade, reduced, stand_aside), `reasons` (list of strings), `run_id`, `metrics` |
| `REGIME#weekly` or `REGIME#monthly` | `<iso ts>` | notes and metrics |
| `PICK#<run_id>#<rank:03d>` | `OUTCOME` | forward return at 1, 5 and 20 trading days, `traded`, realised P&L |

`run_id` is `<kind>-<UTC timestamp>-<4 hex>`, which sorts by time.

**Pick** (pydantic, shared by writer and reader):

| Field | Type | Rule |
|---|---|---|
| `run_id` | str | |
| `rank` | int | 1 = best |
| `symbol` | str | same pattern as config symbols |
| `side` | `long` or `bearish` | |
| `horizon` | `intraday` or `swing` | |
| `score` | int 0–100 | final blended score |
| `pre_score` | int 0–100 | code screen score |
| `thesis` | str, ≤ 2000 chars | |
| `invalidation` | Decimal > 0 | price at which the idea is wrong |
| `earnings_date` | date or null | |
| `expires_at` | datetime (UTC) | intraday: today's close; swing: set by research |
| `features` | dict[str, float] | screen features, for reports |

### A.4 The bot's view of research

`ResearchSource` polls every 60 s (`research.poll_s`): today's `DAY#` partition, plus
the `RUN#…/META` of each run that contributed. A pick is **live** when all hold:

* its run's status is `ok` (a `partial` run counts only if
  `research.accept_partial_runs` is on; default off)
* `now < expires_at`
* the pick validates against the model
* `score >= research.min_score`

Swing picks from earlier trading days stay live until they expire: the poll also reads
the previous `research.swing_lookback_days` weekdays (default 10). Intraday picks
from earlier days are ignored. When a symbol has several live picks, the one with the
highest score wins (ties go to the later run, then the better rank).

The **posture** is the latest `POSTURE#` for today from an `ok` run. **No valid posture
for today means `stand_aside`.** So does a day with a `POSTURE#` item the bot cannot
read: it may be the newest one, so the bot refuses the day's posture rather than fall back
to an older one.

If a poll fails (any error counts), the last good snapshot stays in force for
`research.max_stale_s` (default 600). After that the snapshot is empty and the posture is
`stand_aside`. A kept snapshot is **reset when the trading day changes**: a failed poll
never carries yesterday's posture or intraday picks into today. Before the first
successful read the view is empty, so the posture is `stand_aside`. The bot records
`research_stale` and alerts once; `research_restored` when it recovers. Exits never
depend on research.

### A.5 Universe

The set of equity symbols the bot watches, recomputed after every research poll and
settings change, in priority order:

1. symbols with a position the bot owns (ledger, A.6; without research, held symbols
   that are already in the universe)
2. symbols with a working or unresolved order
3. `pinned_symbols`, which apply live (A.1)
4. live picks, by score (research on only)

Capped at 25 equity symbols (`research.max_symbols`, ≤ 25). Symbols in the first two
groups are never dropped to fit the cap. Options on universe symbols are tracked as today
(at most 20 contracts). A holding the bot did not open (A.6) does not keep a symbol in the
universe. The feed always follows the universe, with research on or off.

When the universe changes:

* **Added:** warm-up bars come from price history for that symbol alone, then the
  stream subscription is updated (`SUBS` with the full key list) and polling covers
  it until the stream delivers. The strategy sees warm-up bars marked as such.
* **Dropped:** only once it is flat with nothing working or pending. Until then it
  stays, so exits keep running.
* A `universe_changed` event lists what was added and dropped.

### A.6 Position ledger

The bot can now trade any symbol, so it must know which holdings are its own. **The
ledger is used only with research on.** Without research the bot manages every holding
in a pinned symbol, as it does today, and a holding outside the universe gets
`unmanaged_holding` (an event and an alert) instead of `unknown_holding`.
Stored in the state table: `POS#<ns>` / `<symbol>` with `horizon`, `side`
(long or bearish), `pick_run_id`, `pick_rank`, `opened_at`.

* Written just **before** the first buy into a symbol the bot does not hold (fail
  closed: if the write fails, no order). A fill that arrives for a symbol with no entry
  is booked too, so it is never foreign.
* Deleted when the broker shows the position flat and nothing is working or
  pending.
* Options use the contract symbol, carrying the underlying's pick.
* **Pinned symbols** are always the bot's (today's rule), with horizon `swing`
  unless a live pick says otherwise.
* **At start-up and on every account snapshot**, a holding that is neither in the
  ledger nor pinned is foreign. The bot records `unknown_holding`, alerts once per
  symbol, and never trades it, sells included. The runbook keeps saying: use an
  account that is the bot's alone. The ledger tracks symbols, not lots, so shares added
  by hand to a symbol the bot holds are managed as the bot's.
* **Leader only.** Only the instance holding the lease writes the ledger, clears
  entries and works out foreign holdings; a standby's copy may be out of date. An
  instance **reloads the ledger when it wins the lease**, because the previous holder may
  have opened or closed positions since it last read it.
* **Outage.** If the ledger cannot be read, entries are off (`entries_halted`: "position
  ledger not loaded"; nothing counts as foreign, so the bot's own exits still work) and
  intraday positions are not flattened, because the bot cannot tell which are intraday.
  It retries every few seconds. After two minutes it alerts (*Position ledger not loaded*),
  once per outage, and if the intraday flatten window opens during the outage it alerts
  again (*Position ledger not loaded at the close*).
* **A1 notices.** `unpinned_but_held` (a settings version unpinned symbols the bot still
  holds) and `unmanaged_holding` (a holding outside the universe) belong to the world
  without a ledger: `unmanaged_holding` is raised only with research off, and the
  "managed until the next restart" warning in `unpinned_but_held` describes research off.
  With research on, a position the bot opened is in its ledger and stays managed.

### A.7 Engine-enforced rules

These are risk checks (new codes), so a buggy strategy cannot get around them. They
apply to entries only. Exits stay exempt, as today.

| Code | Rule |
|---|---|
| `no_pick` | An entry needs a live pick for the symbol (for an option, for its underlying). Pinned symbols are exempt. |
| `pick_side` | A `long` pick allows shares or calls. A `bearish` pick allows puts only. |
| `posture` | `stand_aside` blocks entries. |
| `horizon_budget` | Entry value plus what the bot holds in that horizon must fit `max_total_exposure_usd × research.intraday_share` (intraday) or the rest (swing). |
| `intraday_closing` | An intraday entry inside the last `research.intraday_flatten_min` minutes before the close. |
| `foreign_holding` | The symbol holds a position the bot does not own. Also blocks sells. |

`reduced` posture multiplies `max_order_usd` and `max_position_usd` by
`research.reduced_factor` (default 0.5) for entries. `max_total_exposure_usd` is not
scaled.

**Pinned symbols** are exempt from `no_pick` and `pick_side` only. `posture`,
`horizon_budget` and `intraday_closing` apply to them. An entry's horizon is its ledger
entry's, else its live pick's, else `swing`.

**If the gate cannot be built** for an order (a bug), the bot falls back to a gate with no
pick and a `stand_aside` posture: entries are blocked and exits are not, and the
`foreign_holding` check is kept, so the bot still never sells a holding it did not open.

**Without a research table** none of `no_pick`, `pick_side`, `posture`, `horizon_budget`,
`intraday_closing` or `foreign_holding` applies, there is no ledger, and every position
counts as `swing`: the bot behaves as it does today on its pinned symbols.

**`research` settings block** (all live-applicable):

| Field | Default | Meaning |
|---|---|---|
| `poll_s` | 60 | how often the bot reads the research table |
| `max_stale_s` | 600 | how long a failed read may leave the last snapshot in force |
| `min_score` | 60 | picks below this are ignored |
| `max_symbols` | 25 | universe cap (≤ 25) |
| `accept_partial_runs` | false | use picks from `partial` runs |
| `intraday_share` | 0.5 | share of `max_total_exposure_usd` for intraday positions |
| `reduced_factor` | 0.5 | multiplier on order and position caps on `reduced` days |
| `intraday_flatten_min` | 15 | minutes before the close that intraday positions are sold |
| `swing_lookback_days` | 10 | earlier weekdays read for swing picks that have not expired |

**Intraday flatten:** positions whose ledger horizon is `intraday` get a target of 0
`research.intraday_flatten_min` (default 15) minutes before the close, whatever the
strategy says. Swing positions carry overnight. The existing `flatten_before_close_min`
still flattens everything when set.

### A.8 Strategy API

* `Strategy.__init__(params)` no longer takes a fixed symbol list. `self.symbols`
  stays as the current universe, updated through a new `on_universe(symbols)` hook
  (default: store it). `sma_cross` creates its per-symbol history lazily.
* `StrategyContext` gains `picks: Mapping[str, Pick]` (live picks by symbol) and
  `posture: PostureLevel | None` (`None` when research is off), with `ctx.pick(symbol)`.
  Both are empty without research.
* `Target` is unchanged.
* `sma_cross` gains a `require_pick` parameter, **default false** (it is a strategy
  parameter, so it applies at the next restart): with it on, the strategy targets only
  symbols with a live `long` pick, and asks for 0 of a held symbol with none. That includes
  research being off or stale: a research outage becomes a sale at the next bar. This is the
  strategy's choice. The engine never blocks an exit and never forces one because of
  research. `sma_cross` is still a placeholder that only exercises the plumbing.
* A strategy cannot get around A.7: the rules are risk checks, applied whatever it asks for.

### A.9 Backtest and CLI

* `traider backtest --picks FILE` reads JSON Lines, one row per line: a pick row is the
  seed file's pick fields plus a `day`, and a posture row is `{"day": ..., "posture": ...}`.
  Each day's rows become a research run for that day, read through the same gate as live. A
  day with pick rows and no posture row is a `trade` day; a day with no rows has no
  posture, so nothing opens. Without it, backtests behave as today.
* `traider settings show` prints the current version. `traider settings apply FILE`
  validates a JSON file and writes it as a new version (author `cli`).
  `traider settings history` lists versions.
* `traider research seed FILE` writes a hand-made run, picks and posture for today
  (run kind `manual`). That lets A be tested on paper before C exists.
  Seeding again adds to the day's earlier runs: per symbol the highest score wins, so a
  re-seed cannot remove or lower a pick; only the posture is replaced, by the latest.
* `traider research show` prints today's live picks and posture as the bot sees them. It
  filters with the research settings in the `TRAIDER_*` environment (`TRAIDER_RESEARCH`),
  not the settings table the live bot uses, as `check` and `backtest` also do.

### A.10 Infrastructure

* Two new tables (A.2, A.3). **The research table is opt-in:** `traider:research: true`
  (default false) creates it and gives the bot read access. Pulumi `symbols` becomes
  optional and is renamed `pinnedSymbols`, with `symbols` still accepted as an alias (set
  one of the two, not both). At least one pinned symbol is required unless research is on.
  `traider:researchSettings` sets the `research` block (A.7) and is validated always, used
  only when research is on.
* Task role: read and conditional-write on the settings table, read on the research
  table and its index, and the existing state-table permissions (which cover the
  ledger).
* Environment: `TRAIDER_SETTINGS_TABLE`; `TRAIDER_RESEARCH_TABLE` only when research is
  on; `TRAIDER_RESEARCH` carries `researchSettings` as JSON.
* `localEnv` output gains the settings table name, and the research table name when
  research is on, so `traider settings` and `traider research` work locally. It still
  leaves out `TRAIDER_TRADING_MODE`, `TRAIDER_CONTROL_PARAM` and `TRAIDER_STATE_TABLE`,
  research on or off, so nothing run locally can send a live order.

### A.11 Events, alerts and docs

New engine events, explained in the runbook's event table (the doc test enforces it):
`settings_applied`, `settings_pending_restart`, `settings_rejected`, `research_stale`,
`research_restored`, `universe_changed`, `unknown_holding`, and from A1 `unmanaged_holding`
and `unpinned_but_held`. `entries_halted` gains the reason "position ledger not loaded". New
risk codes are listed under `order_blocked`. The README gains a "Research and the universe"
section, and its "Writing a strategy" section shows `ctx.pick`, `ctx.posture` and
`on_universe`. The runbook gains seeding picks by hand, editing settings from the CLI, and
what the new alerts mean, including the ledger alerts and a going-live check for research.

### A.12 Testing

Everything uses the in-memory stores, moto for DynamoDB, and the engine harness.
Nothing touches real AWS or Schwab.

Each of these must fail when its guard is removed on purpose:

* entry with no live pick; entry on a pick from a failed or expired run
* entry on a `stand_aside` day; entry with no posture for today
* shares on a `bearish` pick; a call on a `bearish` pick; a put on a `long` pick
* intraday budget exceeded; `reduced` caps applied
* research stale past `max_stale_s` blocks entries but not exits
* an invalid settings version is ignored and alerted; a restart-only change is not
  applied live
* concurrent settings writes: exactly one wins
* universe over 25 drops the lowest scores, never a held symbol
* a foreign holding is never traded, sells included
* ledger write failure means no order
* an intraday position is flattened before the close; a swing one is not

---

## B. Web app (outline; full spec when it starts)

* **Frontend:** React, TypeScript, Vite and a chart library. Cognito sign-in (PKCE,
  single user, MFA) with a bearer token to the API. Built assets in a private S3
  bucket.
* **API:** a Python Lambda serving JSON. TypeScript types are generated from the
  pydantic models (A's `Settings`, `Pick`, `Posture` and the report shapes), so the
  page and the API cannot drift.
* **Access:** private only, behind an internal ALB or private API Gateway in the VPC.
  The owner handles network access. The choice between the two is made on cost in
  B's spec.
* **Pages:**
  * **Today:** posture with reasons, control state, positions, P&L, orders, alerts, research run status and cost.
  * **Picks:** tabs per run kind. Ranked table with status (traded, skipped and why, expired) and forward returns.
  * **Pick detail:** features, agent tool trail, thesis, trades, price since the pick against the invalidation line.
  * **Scorecard:** hit rate and return by score bucket, side, horizon, traded vs not, and LLM score vs pre-score. Daily, weekly and monthly. Calibration suggestions.
  * **Regime:** notes and metrics, plus a posture calendar showing each day's posture and P&L.
  * **Trades.**
  * **Costs.**
  * **Settings:** grouped forms, diff before save, history and rollback, typed confirmation for the control switch and for raising limits.
* **Writes:** settings via A's versioned write; control switch via SSM. Both audited and alerted.
* **Tests:** Vitest for components, Playwright (headless Chromium) against a fake API for sign-in, settings save, going live and rollback, and pytest for the API.

## C. Research jobs (outline; full spec when it starts)

* **Runtime:** EventBridge Scheduler triggers Step Functions, with Lambda per stage
  and a Map state for the deep-dives (bounded concurrency).
* **Collect (code):**
  * Schwab: movers, index and sector-ETF quotes, VIX, pre-market gaps.
  * Market-data vendor: earnings calendar, news counts, macro calendar.
  * Carry-overs: held symbols, live swing picks, the weekly watchlist.
  * The vendor (2–3 compared on current pricing) is chosen in C's spec. Its key goes in Secrets Manager.
  * Research reads the Schwab token and never refreshes it. A stale token fails the run.
* **Screen (code):**
  * Filters from settings: price, volume, spread, no OTC, leveraged ETFs on or off.
  * Features: gap, relative volume, ATR %, 20/50-day trend, 52-week distance, days to earnings, news count, sector relative strength, put liquidity.
  * The top K (default 30) go on.
* **Posture:** code metrics and rules (for example a VIX threshold) plus one model call for reasons. The final posture is the stricter of the two.
* **Deep-dive:**
  * A Bedrock Converse tool loop with read-only tools: `price_history`, `news`, `earnings`, `fundamentals`, `sector_context`, `options_liquidity`, `regime`.
  * Capped at about 8 tool calls and a token budget per name.
  * It must finish with `submit_assessment`, against a strict JSON schema.
* **Rank and validate (code):**
  * Schema check.
  * Fresh quote check.
  * The invalidation price must be on the right side of the price and within N × ATR.
  * Bearish picks need a liquid put chain.
  * Blended score, per-sector cap, top N (default 10).
* **Run kinds:** pre-market (full), intraday (delta only), weekly (wider scan and
  watchlist), monthly (regime review and calibration notes, shown as suggestions,
  never applied automatically), earnings watch (after the close), scorecard (EOD).
* **Cost:** per-run and per-day budgets from settings. Hitting one makes the run
  `partial`.
* **Prompt injection:**
  * News text is untrusted data.
  * Tools are read-only fixed APIs.
  * Output is schema-forced and validated by code.
  * Posture can only be made stricter by the model.
* **Models:** a Claude model on Bedrock, and a smaller one for intraday runs. Exact
  model ids are pinned in C's spec after checking availability in the stack's region.
* **Tests:** a fake vendor and fake Bedrock with recorded responses, plus golden
  replays of stored days.

## D. Strategy (owner's choice)

The owner picks the logic. The bot provides `ctx.pick`, `ctx.posture`, bars, quotes
and option chains, and enforces A.7 whatever the strategy does.
