# C2a: Scorecard, intraday runs, missing-posture alert: design

Date: 2026-10-10. Parent specs:
- `2026-10-09-research-driven-trading-design.md`, section C
- `2026-10-09-c1-research-premarket.md` (the C1 run this builds on)

Status: decided by Claude on the owner's instruction; no separate review.

Not financial advice. This measures and extends the research machinery. The owner still chooses the strategy.

## Goal

1. **Scorecard.** Every weekday after the close, record how each recent pick actually did. Without this, nobody can tell whether the research is any good before real money depends on it. The web app (B) reports from these records.
2. **Intraday runs.** Every 30 minutes during the session, look for new names the morning run did not pick, and allow the day's posture to tighten (never loosen).
3. **Missing-posture alert.** The bot says so when research is on and there is no usable posture by the open, instead of silently standing aside all day.

C2b (later) covers the earnings watch after the close, the weekly watchlist and regime notes, the monthly calibration review, and re-assessing held names.

## Decisions

| Question | Decision | Why |
|---|---|---|
| Runtime | Same research task image and CLI: `traider research run --kind scorecard` and `--kind intraday`. Each kind has its own Scheduler schedule, lock name and budget. | Reuses C1's lock, trail, alerts, scrub, store and infra. |
| Scorecard schedule | `cron(30 16 ? * MON-FRI *)` New York | After the close. Uses daily bars, so an exact time is not needed. |
| Intraday schedule | `cron(0/30 10-15 ? * MON-FRI *)` New York. The runner skips starts after `intraday.last_start` (15:00). | Runs 10:00 to 15:00. Leaves time before the bot's intraday flatten. |
| Schedules start disabled | One toggle per kind: `traider:researchScorecardEnabled` and `traider:researchIntradayEnabled`, default false. | Same rollout as C1. |
| Intraday needs a morning run | An intraday run only proceeds when today already has a usable (`ok`) posture. Otherwise it exits `skipped` and writes nothing. | A failed morning means standing aside all day. Intraday must not rescue the day on code rules alone. |
| Intraday posture | The new level is `stricter(today's latest usable posture, code rules on current metrics)`. There is no model review. The run writes the posture again even when unchanged, so the time-stamped record is continuous. | Lets a VIX spike tighten the day mid-session. It can never loosen the morning call. |
| Intraday picks | Only names not already picked today and not held. Horizon forced to `intraday`, expiring at today's close. At most `intraday.deep_dive_count` (3) dives. If the posture is `stand_aside`: no dives, no picks. | Delta only, and cheap. |
| Intraday model | `dive.intraday_model`, defaulting to the same model as the morning run. A smaller model is the owner's choice, once it has a price entry. | No model is used without a price. |
| Intraday budget | `budget.intraday_run_usd` (default 0.75). It counts toward the shared `budget.day_usd`. | Bounds the per-run spend. |
| Intraday Schwab rate | Its own limiter at 20 requests/min. | The bot is trading live during the session. |
| Scorecard method | Forward returns come from Schwab daily bars. No model calls. | Deterministic and cheap. |
| "Traded" | True when the bot submitted a buy in the symbol (or an option on it) while the pick was live, per the bot's event log. `null` when the log cannot be read. Realised P&L is left to B. | Submission is what the log reliably holds. Fills and P&L come from the broker, which B will read. |
| Missing-posture alert | The bot's engine, at `research.posture_alert_after_open_min` (default 5) after the open on a trading day: if research is on and the view's posture is `stand_aside` because there is no usable posture (not because research chose it), alert once. Event `research_no_posture`. | Visibility. A missing posture is fail-closed but must not be silent. |

## Scorecard

**Which picks:** every pick from `ok` or `partial` runs whose trading day is within the last `scorecard.lookback_days` (30) weekdays. Picks from partial runs are scored too, flagged `run_status`.

**Prices** (Schwab daily bars for the pick's symbol, from the pick day to today):

| Name | Meaning |
|---|---|
| `entry` | The pick day's open. If that bar is missing, the outcome stays `pending` (see below). |
| `price_at_pick` | From the pick's features, kept for comparison. |
| `ret_<h>d` (h = 1, 5, 20) | `close of trading day pick_day + h − 1 / entry − 1`, signed: long +, bearish −. `null` until that bar exists. For an intraday pick, `ret_0d` = (close of the pick day / entry − 1), signed. |
| `mfe_pct`, `mae_pct` | Max favourable and max adverse excursion. Over the pick's live window, from entry, signed by side. |
| `hit_invalidation` | Long: any low ≤ invalidation within the window. Bearish: any high ≥ invalidation. |
| `expired_return` | Signed return from entry to the close of the expiry day. |

The live window runs from the pick day to the expiry day, capped at today.

**Outcome item:** pk `PICK#<run_id>#<rank:03d>`, sk `OUTCOME`. Pydantic `PickOutcome`: strict, rejects extra fields, every float finite. Fields:
- `run_id`, `rank`, `symbol`, `side`, `horizon`, `score`, `pre_score`, `llm_score`, `pick_day`, `run_status`
- the price fields above
- `traded: bool | None`
- `status`: `pending`, `partial` or `final` (final once the 20-day close exists, or the pick is 30 weekdays old)
- `updated_at`

Writes are idempotent overwrites. A pick already marked `final` is skipped.

**Daily summary:** item pk `SCORE#<day>`, sk `SUMMARY`, holding:
- counts per run kind and per side;
- `ret_1d` hit rate (share with return > 0) and the mean for picks that matured today;
- the same for `ret_5d`;
- one row per score bucket (60–69, 70–79, 80–89, 90–100), with count and mean `ret_1d`.

**Alert:** a short `research_scorecard` message: `traider scorecard <day>: 9 picks matured 1d, hit 5/9, mean +0.6%; 5d hit 3/6 …`.

**Failures:**
- A symbol whose bars cannot be read stays `pending`, with a note.
- If half or more of the symbols fail, the run is `partial`.
- Anything else fails the run. META is `failed`, but nothing the bot reads depends on the scorecard.

**Store additions:**
- `put_outcome(outcome)`
- `outcomes(pick_keys)`, a batch get
- `picks_between(start_day, end_day)`, which queries `DAY#` partitions per weekday
- `put_score_summary(day, summary)`

## Intraday run

Flow (reuses C1 stages):

```
lock(intraday) → market open now? → after last_start? → today has an ok posture? → META running
 → collect (context quotes, SPY bars, movers; no earnings calendar fetch;
   reuse today's premarket snapshot calendar from the trail if present, else the Finnhub calendar for today ± lookahead)
 → posture = stricter(latest usable posture today, code rules) → stand_aside? yes → write posture, 0 picks, ok
 → candidates = movers − already picked today − held − pinned → screen (same filters, features) → top intraday.deep_dive_count
 → dives (intraday_model, intraday_run_usd) → rank (horizon forced intraday) → write picks + posture + META → alert only if picks > 0 or posture tightened
```

**"Held" means:**
- the bot's ledger (state table `POS#`), read-only;
- plus pinned symbols from settings.

This needs `Query` on the state table for `POS#<ns>` and `LOG#<ns>#<day>`.

**Sizes:**
- `intraday.max_candidates` is 30.
- History reads are bounded the same way as the morning run's.
- `intraday.max_run_s` is 600. The lock and the time box follow C1's rules.

**Assessment:** the dive prompt gains one line: "This is an intraday idea; it must be flat by today's close." Any assessment with `horizon = swing` is coerced to `intraday`, with a note.

**Settings** (`ResearchJobSettings.intraday`):
- `enabled` (true)
- `last_start` (`15:00`)
- `max_candidates` (30)
- `deep_dive_count` (3)
- `max_run_s` (600)

Plus `dive.intraday_model` and `budget.intraday_run_usd`. Validators:
- `intraday_run_usd ≤ day_usd`
- `intraday_model` must be priced

Scorecard settings (`ResearchJobSettings.scorecard`):
- `enabled` (true)
- `lookback_days` (30, range 5–60)

## Missing-posture alert (bot)

- **Where:** the engine already refreshes research every `research.poll_s`.
- **Trigger:** once per trading day, after `open + posture_alert_after_open_min`, when all of these hold:
  - research is configured;
  - the session is open;
  - the view has no usable posture for today. The source exposes this distinctly as `ResearchView.posture_missing: bool`, separate from a posture whose level is `stand_aside`.
- **Effect:**
  - Event `research_no_posture`.
  - An alert reading: "No usable research posture for today; the bot is standing aside. Check the pre-market run (alerts, META, the DLQ)."
- **Not** fired when research is stale: `research_stale` already covers that.
- The runbook event table gains the row (the doc test enforces it).

## Infra

Under the existing `traider:researchJobs` flag, no new bucket or secret. Additions:

| Resource | Notes |
|---|---|
| Two more Scheduler schedules, with the same DLQ, retries and maximum event age as C1 | Each starts `DISABLED` unless its toggle is true. Each toggle requires `researchJobs`. |
| Container overrides on each schedule target: the `command` for its kind | Kinds: `["research", "run", "--kind", "scorecard"]` and `["research", "run", "--kind", "intraday"]` |
| Task role: read-only `dynamodb:Query` on the state table | For the ledger and the event log |
| Task env: `TRAIDER_STATE_TABLE` | Research reads the bot's namespace. That needs the stack's trading mode, so the env gets `TRAIDER_STATE_NAMESPACE` explicitly, not `TRAIDER_TRADING_MODE`. Check the state store's namespace rule in code; if the namespace is derived from the mode, pass the namespace value directly. |
| Scheduler role | May pass the same two roles and run the same task family (it already does) |

## CLI

`traider research run --kind {premarket,intraday,scorecard} [--dry-run] [--force]`

- `--dry-run` writes nothing to the table.
- For intraday, `--force` ignores `last_start` but not the morning-posture requirement.
- `--force` ignores skip-if-done. Skip-if-done applies to premarket and scorecard (once per day). Intraday has no skip-if-done; the lock stops overlaps.

## Testing

All offline, reusing the C1 fakes (`FakeMarketData`, `FakeEvents`, `ScriptedLLM`, the memory stores).

- **Scorecard:**
  - golden returns for long, bearish and intraday picks over a fake bar series;
  - invalidation hit;
  - MFE/MAE;
  - `pending`, `partial` and `final` transitions;
  - `final` picks skipped;
  - `traded` from the event log, and `null` when the log is unreadable;
  - the summary maths;
  - failure handling.
- **Intraday:**
  - skipped without a morning posture;
  - skipped after `last_start`;
  - a posture that only tightens: a break where the code rules loosen must fail a test;
  - delta candidates exclude picked, held and pinned names;
  - swing coerced to intraday;
  - budget and time box;
  - lock.
- **Bot alert:**
  - fires once, after the delay;
  - not before the open;
  - not when research chose `stand_aside`;
  - not when research is stale;
  - not when research is off.
- **Infra:** schedules, toggles, overrides, state-table read access, env.
- **Breaks on purpose** (each must make a test fail):
  - intraday loosens the posture;
  - intraday runs without a morning posture;
  - the scorecard ignores side when signing returns;
  - the alert fires on a research-chosen `stand_aside`.

## Not verified

- Schwab daily bars for the current day before the close: the scorecard runs after the close, so it should not depend on this.
- Schwab movers during the session.
- That the state-table namespace matches the bot's.
- Plus everything still unverified from C1.
