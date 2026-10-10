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
| Runtime | Same research image and CLI: `traider research run --kind scorecard` and `--kind intraday`. Each kind has its own ECS task definition (`{prefix}-research` for premarket, `{prefix}-research-scorecard`, `{prefix}-research-intraday`), its own Scheduler schedule and its own lock name. No schedule overrides the container's command. | Reuses C1's lock, trail, alerts, scrub, store and infra. A schedule can only start its own kind, whatever happens to an override. |
| Scorecard schedule | `cron(30 16 ? * MON-FRI *)` New York | After the close. Uses daily bars, so an exact time is not needed. |
| Intraday schedule | `cron(0/30 10-15 ? * MON-FRI *)` New York. The runner skips starts after `intraday.last_start` (15:00). | Runs 10:00 to 15:00. Leaves time before the bot's intraday flatten. |
| Schedules start disabled | One toggle per kind: `traider:researchScorecardEnabled` and `traider:researchIntradayEnabled`, default false. | Same rollout as C1. |
| Intraday failure | A failed intraday run writes no posture or picks, but does write a failed META and its cost. | The posture and picks in force stay. |
| Intraday needs a morning run | An intraday run only proceeds when today already has an `ok` posture (see "Today's posture" below). Otherwise it exits `skipped` and writes nothing. | A failed morning means standing aside all day. Intraday must not rescue the day on code rules alone. |
| Intraday posture | The new level is `stricter(today's posture as the bot reads it (always with `accept_partial_runs` off), code rules on current metrics)`. There is no model review. The run writes the posture again even when unchanged, so the time-stamped record is continuous. | Lets a VIX spike tighten the day mid-session. It can never loosen the morning call. |
| Intraday picks | Only names not already picked today, not held (ledger), not pinned and without any unexpired pick from any run. Horizon forced to `intraday`, expiring at today's close. At most `intraday.deep_dive_count` (3) dives. If the posture is `stand_aside`: no dives, no picks. | Delta only, and cheap. |
| Intraday model | `dive.intraday_model`, defaulting to the same model as the morning run. A smaller model is the owner's choice, once it has a price entry. | No model is used without a price. |
| Intraday budget | `budget.intraday_run_usd` (default 0.75). It counts toward the shared `budget.day_usd`. | Bounds the per-run spend. |
| Intraday Schwab rate | Its own limiter at 20 requests/min (the other kinds keep 40). | The bot is trading live during the session. |
| Scorecard method | Forward returns come from Schwab daily bars. No model calls. | Deterministic and cheap. |
| "Traded" | True when the bot submitted a buy in the symbol (or an option on it) while the pick was live, per the bot's event log. `null` when the log cannot be read. Realised P&L is left to B. | Submission is what the log reliably holds. Fills and P&L come from the broker, which B will read. |
| Today's posture (bot and intraday, only ever stricter) | The strictest of: the newest `ok` posture (when there is none and `accept_partial_runs` is on, the earliest `partial` one), and every readable posture written at or after it today, whatever its run's status. A newer `ok` premarket run (for example `--force`) is authoritative. An unreadable posture item means no posture. | A partial intraday tightening reaches the bot, and no run can loosen the day. |
| Missing-posture alert | The bot's engine, at `research.posture_alert_after_open_min` (default 5) after the open on a trading day: if research is on and the view's posture is `stand_aside` because there is no usable posture (not because research chose it), alert once. Event `research_no_posture`. | Visibility. A missing posture is fail-closed but must not be silent. |

## Scorecard

**Which picks:** every pick from `ok` or `partial` runs whose trading day is within the last `scorecard.lookback_days` (30) weekdays. Picks from partial runs are scored too, flagged `run_status`.

**Prices** (Schwab daily bars for the pick's symbol, from the pick day to today):

| Name | Meaning |
|---|---|
| `entry` | The pick day's open. If that bar is missing, the outcome stays `pending` (see below). |
| `price_at_pick` | From the pick's features, kept for comparison. |
| `ret_<h>d` (h = 1, 5, 20) | The close of the h-th daily bar, counting the pick day as the 1st (so `ret_1d` is the pick day's close), over `entry`, minus 1; in percent, signed: long +, bearish −. `null` until that bar exists. For an intraday pick, `ret_0d` = the pick day's close over `entry`, minus 1, signed. |
| `mfe_pct`, `mae_pct` | Max favourable and max adverse excursion. Over the pick's live window, from entry, signed by side. |
| `hit_invalidation` | Long: any low ≤ invalidation within the window. Bearish: any high ≥ invalidation. |
| `expired_return` | Signed return from entry to the close of the expiry day (the last bar of the window once the expiry has passed). |

The live window runs from the pick day to the expiry day, capped at today; an expiry before the pick day counts as the pick day. Horizons count bars, so a missing bar must never be bridged: usable data ends at the first bad bar (a non-finite or non-positive price) or after a gap of more than 4 calendar days. Whatever lies beyond stays `null`. A lone missing mid-week bar looks like a holiday and cannot be detected.

**Outcome item:** pk `PICK#<run_id>#<rank:03d>`, sk `OUTCOME`. Pydantic `PickOutcome`: strict, rejects extra fields, every float finite. Fields:
- `run_id`, `rank`, `symbol`, `side`, `horizon`, `score`, `pre_score`, `llm_score`, `pick_day`, `run_status`
- the price fields above
- `traded: bool | None`
- `status`: `pending`, `partial` or `final` (final once the 20-day close and the expiry day's close exist, or the pick is 30 weekdays old)
- `updated_at`

Writes are idempotent overwrites. A pick already marked `final` is skipped. A value an earlier scorecard knew is never overwritten with `null` (an unreadable log day, a bar Schwab no longer returns), and a status never goes backwards.

**Daily summary:** item pk `SCORE#<day>`, sk `SUMMARY`, holding:
- counts per run kind and per side;
- `ret_1d` hit rate (share with return > 0) and the mean for picks that matured today;
- the same for `ret_5d`;
- one row per score bucket (60–69, 70–79, 80–89, 90–100), with count and mean `ret_1d`.

**Alert:** a short `research_scorecard` message: `traider scorecard <day>: 9 picks matured 1d, hit 5/9, mean +0.6%; 5d hit 3/6 …`.

**Failures:**
- A symbol whose bars cannot be read keeps its pick's last outcome (or gets a `pending` one), with a note. If half or more of the symbols fail, the run is `partial`.
- An unreadable event-log day makes `traded` unknown for the picks it covers. If half or more of the days fail, the run is `partial`.
- Anything else fails the run. META is `failed`, but nothing the bot reads depends on the scorecard.

**Store additions:**
- `put_outcome(outcome)`
- `outcomes(pick_keys)`, a batch get (`dynamodb:BatchGetItem` on the research table)
- `picks_between(start_day, end_day)`, which queries `DAY#` partitions per weekday
- `put_score_summary(day, summary)`

## Intraday run

Flow (reuses C1 stages):

```
lock(intraday) → market open now? → after last_start (+5 min grace)? → today has an ok posture? → META running
 → held (the bot's ledger) → live picks of earlier days
 → collect (context quotes, SPY bars, movers, and the Finnhub earnings calendar from
   yesterday to `earnings_lookahead_days` ahead: one call per run, see the decisions below)
 → posture = stricter(today's posture as the bot reads it, code rules) → stand_aside? yes → write posture, 0 picks, ok
 → an unreadable pick item? yes → write posture, 0 picks, partial
 → candidates = movers − picked today − held − pinned − any unexpired pick → screen (same filters, features) → top intraday.deep_dive_count
 → dives (intraday_model, intraday_run_usd) → rank (horizon forced intraday) → write picks + posture + META
 → alert if picks > 0, the posture tightened, or the run is partial
```

**"Held" means:**
- the bot's ledger (state table `POS#`), read-only;
- plus pinned symbols from settings.

This needs `Query` on the state table for `POS#<ns>` and `LOG#<ns>#<day>`, and nothing else. Without the ledger the run fails and picks nothing.

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
- `lookback_days` (30, range 30–60)

## Missing-posture alert (bot)

- **Where:** the engine already refreshes research every `research.poll_s`.
- **Trigger:** once per trading day, after `open + posture_alert_after_open_min`, when all of these hold:
  - research is configured;
  - the session is open;
  - the view has no usable posture for today. The source exposes this distinctly as `ResearchView.posture_missing: bool`, separate from a posture whose level is `stand_aside`.
- **Effect:**
  - Event `research_no_posture`.
  - An alert reading: "No usable research posture for today; the bot is standing aside. Check the pre-market run (alerts, META, the DLQ)."
- Once per bot process per trading day, and only after a research read made since the alert time. A restart may repeat it. A `partial` run's posture counts as missing unless `research.accept_partial_runs` is on. The alert also covers a missed pre-market run.
- **Not** fired when research is stale: `research_stale` already covers that.
- The runbook event table gains the row (the doc test enforces it).

## Infra

Under the existing `traider:researchJobs` flag, no new bucket or secret. Additions:

| Resource | Notes |
|---|---|
| Two more Scheduler schedules, with the same DLQ (shared by all three), retries and maximum event age as C1 | Each starts `DISABLED` unless its toggle is true. Each toggle requires `researchJobs`. |
| Two more task definitions (`{prefix}-research-scorecard`, `{prefix}-research-intraday`), identical to the pre-market one but for the command | Each schedule starts only its own. No container overrides. |
| Task role: `dynamodb:Query` only on the state table, limited by `dynamodb:LeadingKeys` to `POS#<mode>` and `LOG#<mode>#*` | For the ledger and the event log. Check it with `aws iam simulate-principal-policy` before enabling (runbook, step 5). |
| Task env: `TRAIDER_STATE_TABLE` and `TRAIDER_STATE_NAMESPACE`, set together | The namespace is the stack's trading mode; the task has no `TRAIDER_TRADING_MODE`. A table without a namespace fails every kind; intraday needs both; with neither, premarket and scorecard run, and the scorecard reports `traded` as unknown. |
| Scheduler role | May pass the same two roles and run the three task families |
| Dead-letter queue | Shared by the three schedules. It must be purged after every message, or the alarm stays in ALARM and later failures send no alert. Intraday failures can alert up to 12 times a day. |

## CLI

`traider research run --kind {premarket,intraday,scorecard} [--dry-run] [--force]`

- `--dry-run` writes nothing to the table.
- For intraday, `--force` (and a dry run) ignores `last_start` but not the morning-posture requirement.
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
- **Infra:** schedules, toggles, task definitions, state-table read access, env.
- **Breaks on purpose** (each must make a test fail):
  - intraday loosens the posture;
  - intraday runs without a morning posture;
  - the scorecard ignores side when signing returns;
  - the alert fires on a research-chosen `stand_aside`.

## Not verified

- Schwab movers during the session, and whether the day's daily bar is there at 16:30.
- That `dynamodb:LeadingKeys` is enforced as written.
- That the state-table namespace matches the bot's.
- The Scheduler, EventBridge and ECS behaviour, including the shared dead-letter queue.
- Plus everything still unverified from C1.

## Limits of the scorecard

- For an intraday pick the entry is the day's open, and MFE/MAE use the whole day's range, moves before the pick included.
- A lone missing mid-week bar cannot be detected (it looks like a holiday).
- `traded` means a buy order was submitted, not filled; realised profit is not measured.

## Decisions made while planning and building C2a

* **Shared run scaffolding.** `research/run.py` gains `RunBase` (META, trail, cost meter and day cost, alerts, failure handling, time box) and `run_locked` (the lock per kind). The pre-market run is one kind (`_Run`); `scorecard_run.py` and `intraday.py` hold the others. The pre-market run's behaviour does not change.
* **Intraday earnings.** Each intraday run makes one Finnhub calendar call (it does not reuse the morning's snapshot): no S3 read permission and no parsing of stored files. A failed calendar, or an empty one over a span of 5 weekdays or more, makes the run `partial`.
* **`last_start` has 5 minutes' grace** (`START_GRACE_S`): a Fargate task takes a minute or two to start. The 15:30 firing of `cron(0/30 10-15 …)` is always skipped.
* **"An ok posture today"** means today's posture as the bot reads it with `accept_partial_runs` off: the newest `ok` posture made stricter by any later one. An unreadable posture item means none (fail closed). The bot's ledger is required: without a state table the run cannot start, and a failed ledger read fails the run.
* **Unreadable picks.** An intraday run that cannot read a pick item writes its posture and no picks, as `partial`.
* **Swing assessments** are coerced to intraday, with a note.
* **Intraday alert** also when the run is `partial`. A dry run ignores `last_start` but still needs the session open and an `ok` posture.
* **Scorecard numbers** are percentages, rounded to 4 places. `llm_score` is an integer. "Matured today" means that horizon's bar is today's. The summary covers every pick in the window: newly scored, kept and already final.
* **The window** is pick days up to `lookback_days` weekdays before today, both ends included.
* **Unreadable bars** keep a pick's last outcome; a pick with none gets a `pending` one. Known values are never overwritten with `null`, and a status never goes backwards.
* **"Traded"** looks for `order_submitted` events with side `BUY` whose symbol (or an option's underlying) is the pick's, from the run's `finished_at` (else `started_at`) to the earlier of expiry and now.
* **The scorecard's time box** is `research_jobs.max_run_s`. It needs the same setup as the other kinds (a Finnhub key among them), because the wiring is shared.
* **Alerts:** the scorecard's subject is `Scorecard <day>: <status>` and its event `research_scorecard`; the intraday subject is `Research intraday <day>: <status>`; a failure is `research_run_failed`, like the other kinds.
* **Namespace:** research reads the bot's state only with an explicit `TRAIDER_STATE_NAMESPACE` (`paper` or `live`); a state table without it is refused.
* **Missing posture:** checked on every engine step once `posture_alert_after_open_min` has passed, and only on a research read made since then.
* **Schedules:** each kind has its own task definition and its own schedule; there are no container overrides. Not verified on AWS.
