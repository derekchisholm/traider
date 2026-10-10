# C2a: Scorecard, intraday runs, missing-posture alert — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every weekday after the close, record how each recent pick actually did; every 30 minutes during the session, add new intraday picks and let the day's posture tighten (never loosen); and have the bot say so when research is on and there is no usable posture by the open.

**Architecture:**
- `research/run.py` is split. `RunBase` and `run_locked` hold what every run kind shares: the lock, META, the trail, the cost meter and the day's cost, alerts, failure handling, the time box and the exit codes. `_Run` stays the pre-market kind, with byte-for-byte the same behaviour.
- Two new kinds: `research/scorecard_run.py` (`ScorecardRun(RunBase)`, its maths pure in `research/scorecard.py`) and `research/intraday.py` (`IntradayRun(_Run)`, which reuses the pre-market screen, deep-dives and ranking through three small hooks).
- Research reads the bot's ledger and event log through a read-only `BotState` protocol (`research/botstate.py`), in the namespace `TRAIDER_STATE_NAMESPACE` names.
- The bot gains `ResearchView.posture_missing` and a once-a-day `research_no_posture` event and alert in the engine.
- Pulumi adds two schedules (created disabled, one toggle each) whose targets override the task's command, read-only `Query` on the bot's ledger and log partitions, `BatchGetItem` on the research table, and the state table and namespace in the task's environment.

**Tech Stack:** Python 3.13, uv, pydantic 2, aiohttp, boto3, `anthropic[bedrock]` 1.13, moto, Pulumi (Python) with `pulumi-aws` 7. Nothing new.

**Spec:** `docs/superpowers/specs/2026-10-10-c2a-scorecard-intraday.md` (parents: `2026-10-09-research-driven-trading-design.md` section C, and `2026-10-09-c1-research-premarket.md`). It is committed together with this plan.

**Not financial advice.** This measures and extends the research machinery. The owner chooses the strategy.

**Verified while writing this plan:**
- Every task's code was written and run in a scratch copy of `main` outside the repository (not committed anywhere). At the end: root 2230 passed, 6 skipped; infra 168 passed; ruff, `ruff format --check` and mypy (strict) clean in both.
- The plan was then replayed mechanically in a second fresh copy of `main`: every "Create", "Replace" and "Append" below was applied exactly as written, task by task. Each "see them fail" step failed and each "see them pass" step passed with the counts given, the whole suite and lint were green after every task, and the result was byte-identical to the scratch copy. Finally this Markdown file itself was parsed and its 124 Create, Replace and Append steps applied to a third fresh copy: byte-identical again.
- Each "break on purpose" below (25 in all) was applied in the scratch copy and made the named tests fail.
- One whole-suite run, at the Task 1 state in the second replay, had a single failing test that did not fail again in four reruns of that state; it was not identified (the output was not kept). Every other whole-suite run was green.
- The pre-market run's output (META, picks, posture, cost, lock, trail files, alerts and the printed report, over eight paths: golden, stand aside, Finnhub down, expired sign-in, lock held, already done, dry run, switched off) was fingerprinted before Task 4 and again after Tasks 4, 5 and 6: byte-identical each time.
- Nothing here has met real Schwab, Finnhub, Bedrock or AWS. See "Not verified" in the spec, and Task 9's docs.

**Decisions recorded here (added to the C2a spec in Task 9):**
- **Intraday earnings:** one Finnhub calendar call per intraday run, instead of reading the morning's `snapshot.json` back from S3 (no S3 read permission, no parsing of stored files). A failed or empty calendar makes the run `partial`, as in the morning.
- **`last_start` grace:** starts up to 5 minutes after `research_jobs.intraday.last_start` still run (`START_GRACE_S`): a Fargate task takes a minute or two to start, so the 15:00 run would otherwise usually be skipped. The 15:30 firing of `cron(0/30 10-15 ? * MON-FRI *)` is always skipped.
- **"An ok posture today"** is the newest posture today from an `ok` run of any kind (an earlier intraday run included). An unreadable posture item means none.
- **The ledger is required for intraday:** no state table means the run cannot start (`SetupError`); a ledger read that fails fails the run, so nothing is picked that might be held.
- **Intraday alerts** also go out for a `partial` run, not only for new picks or a tighter posture. A dry run ignores `last_start` (dry runs run forced) but still needs the session open and an ok posture.
- **Scorecard numbers** are percentages (×100), rounded to 4 places; `llm_score` is an integer. "Matured today" means that horizon's bar is today's. The summary covers every pick in the window: newly scored, kept and already final.
- **`final`** needs the 20-day return *and* the expiry day's close: a 20-weekday swing can outlive the 20th bar.
- **The scorecard's window** is pick days up to `lookback_days` weekdays before today, both ends included, so a pick exactly 30 weekdays old gets its final outcome.
- **Unreadable bars** keep a pick's last outcome; a pick with none gets a `pending` one.
- **"Traded"** is an `order_submitted` event with side `BUY` whose symbol (or an option's underlying) is the pick's, between the run's `finished_at` (else `started_at`) and the earlier of the pick's expiry and now.
- **The scorecard's time box** is `research_jobs.max_run_s`; no setting of its own. It needs the same setup as the other kinds (a Finnhub key among them), because the wiring is shared.
- **Alerts:** the scorecard's subject is `Scorecard <day>: <status>`, event `research_scorecard`; the intraday subject is `Research intraday <day>: <status>`; a failure is `research_run_failed` for every kind.
- **Namespace:** research builds its `BotState` only with an explicit `TRAIDER_STATE_NAMESPACE` (`paper` or `live`, a new `Config.state_namespace`); a state table without it is refused.
- **IAM:** the state-table `Query` is limited by `dynamodb:LeadingKeys` to `POS#<mode>` and `LOG#<mode>#*`; `dynamodb:BatchGetItem` is added on the research table for `outcomes()`.
- **Missing posture:** checked on every engine step once `research.posture_alert_after_open_min` has passed, using only a research read made since then; once per trading day per bot process (a restart may repeat it). A partial run's posture counts as missing unless `research.accept_partial_runs` is on.
- **Schedules:** the pre-market schedule keeps the task's own command and is otherwise unchanged; the scorecard and intraday schedules pass `{"containerOverrides": [{"name": "research", "command": [...]}]}` as the target's `input` (not verified on AWS).
- **Tests changed, not only added:** `test_cli_research_run.py`'s `fake_build` takes the new `kind` argument and its "takes only premarket" test becomes "takes the three kinds" (Task 6); in `infra/tests/test_research_jobs.py` four schedule tests are renamed and widened to the three schedules (`test_every_schedule_starts_disabled`, `test_each_schedule_runs_only_once_you_enable_it_and_only_that_one`, `test_enabling_a_schedule_without_the_jobs_is_refused`, `test_every_schedule_runs_on_the_bots_network`), two look the pre-market schedule up by name, the two exact-statement tests gain `ReadBotState` and `dynamodb:BatchGetItem`, and the two environment tests gain the state table and namespace (Task 8). No test in `test_research_run.py` changes.

## Global Constraints

- Python `>=3.13,<3.14`. Run everything with `uv run` from the repo root (bot) or from `infra/` (infra).
- These must stay green: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` (strict), `uv run pytest`, in both the root and `infra/`.
- **Tests never touch real AWS, Schwab, Finnhub or Bedrock.** Use moto, the fake Schwab server (`tests/fakes/schwab_server.py`), the fake Finnhub server, the in-memory stores (research and state), `FakeMarketData`, `FakeEvents` and the scripted model (`tests/fakes/research.py`).
- **Fail closed.** A failed run writes no posture, so the bot stands aside. A partial run is ignored by the bot by default. Missing data means `stand_aside`. An intraday run never rescues a day: without an `ok` posture today it writes nothing. An intraday run that cannot read the bot's ledger picks nothing. An unpriced model is never called.
- **The model only advises, and intraday can only tighten.** An intraday posture is the stricter of today's latest ok posture and the code rules; there is no model review of it. Every pick passes code validation. Research never places an order and only reads the bot's state.
- **Nothing secret** goes in the repo, logs, alerts, the trail, the research table or Pulumi state. Error text is scrubbed before it reaches META, an alert or a log line.
- **Every safety rule gets a deliberate break.** Each task names the breaks: make the change, confirm the named tests FAIL, restore.
- The bot's behaviour does not change, except the new `research_no_posture` event and alert (Task 7). The pre-market run's behaviour does not change at all (Task 4 proves it). Existing tests pass unchanged, except where a task says otherwise.
- Conventional Commits with short subjects. End each commit message with exactly:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Xh7zur22CRYEGaDoTB5nkV
  ```
- Work on branch `feat/research-c2`, branched from `main` (it already holds the commit `docs: C2a scorecard and intraday spec and plan`). Never push to `main`; never push at all unless the owner says so.
- `README.md` and `docs/runbook.md` must stay true. `tests/unit/test_docs.py` checks links, `traider <command>` names, that every backticked `max_*`/`min_*` name is a `RiskLimits` field, that every `research_jobs.…` path exists, and that the runbook's event table lists exactly the events the engine records. Write research settings with their full dotted path (`research_jobs.intraday.max_run_s`, `research.posture_alert_after_open_min`), never bare.

## File structure

| File | Responsibility |
|---|---|
| `src/traider/research/job_settings.py`, `src/traider/config.py` | `IntradaySettings`, `ScorecardSettings`, `dive.intraday_model`, `budget.intraday_run_usd`, `ResearchSettings.posture_alert_after_open_min` (Task 1); `Config.state_namespace` (Task 5) |
| `src/traider/timeutil.py` | `weekdays_from` (Task 2) |
| `src/traider/research/models.py` | `OutcomeStatus`, `PickOutcome`, `outcome_key`, `HorizonStats`, `ScoreBucket`, `ScoreSummary` (Task 2) |
| `src/traider/research/store.py` | `picks_between`, `outcomes`, `put_outcome`, `put_score_summary` on both stores and `ResearchWriter` (Task 2) |
| `src/traider/research/scorecard.py` (new) | the scorecard's pure maths: `score_pick`, `traded_from_logs`, `summarize`, `summary_text` (Task 3) |
| `src/traider/research/run.py` | `RunBase`, `run_locked` and the pre-market `_Run` (Task 4); `RunDeps.state`, `RunOutcome.extra` (Task 5); the intraday hooks (Task 6) |
| `src/traider/research/botstate.py` (new) | `BotState` (read-only protocol), `held_symbols` (Task 5) |
| `src/traider/research/scorecard_run.py` (new) | `run_scorecard`, `ScorecardRun` (Task 5) |
| `src/traider/research/wiring.py`, `src/traider/cli.py` | `bot_state`, `--kind scorecard` (Task 5); `kind`, the 20/min limiter, `--kind intraday` (Task 6) |
| `src/traider/research/dive.py` | `DiveContext.intraday`, `INTRADAY_LINE` (Task 6) |
| `src/traider/research/intraday.py` (new) | `run_intraday`, `IntradayRun`, `latest_ok_posture` (Task 6) |
| `src/traider/research/source.py`, `src/traider/engine.py` | `ResearchView.posture_missing`; `research_no_posture` (Task 7) |
| `infra/settings.py`, `infra/research.py`, `infra/Pulumi.example.yaml` | toggles, schedules, overrides, IAM, environment (Tasks 1 and 8) |
| `README.md`, `docs/runbook.md`, the C2a spec | docs (Tasks 7 and 9) |
| `tests/unit/test_research_scorecard.py`, `test_research_run_base.py`, `test_research_botstate.py`, `test_research_scorecard_run.py`, `test_research_intraday.py`, `test_engine_no_posture.py` (new); `test_research_job_settings.py`, `test_config.py`, `test_timeutil.py`, `test_research_models.py`, `test_research_store.py`, `test_cli_research_run.py`, `infra/tests/test_research_jobs.py` | the tests, task by task |

Each task below shows every change as **Create** (the whole new file), **Replace** (find the first block, which occurs exactly once, and put the second in its place) or **Append** (add at the end of the file, after the usual blank lines). Apply them in the order given: a later replacement in the same file assumes the earlier ones are done. Short Python fragments that are not complete statements are shown as plain `text` blocks, so that formatting this plan never changes them; type them exactly as shown.

---

### Task 1: Settings for the intraday runs, the scorecard and the missing-posture alert

**Files:**
- Modify: `src/traider/research/job_settings.py`, `src/traider/config.py`, `infra/Pulumi.example.yaml`
- Test: `tests/unit/test_research_job_settings.py`, `tests/unit/test_config.py`

**Interfaces:**
- Produces:
  - `traider.research.job_settings.IntradaySettings(enabled: bool = True, last_start: datetime.time = time(15, 0), max_candidates: int = 30 (1..100), deep_dive_count: int = 3 (1..10), max_run_s: float = 600.0 (60..1200))`. `last_start` is naive New York time, strictly between 09:30 and 16:00.
  - `ScorecardSettings(enabled: bool = True, lookback_days: int = 30 (5..60))`.
  - `ResearchJobSettings.intraday: IntradaySettings`, `ResearchJobSettings.scorecard: ScorecardSettings`.
  - `DiveSettings.intraday_model: str = DEFAULT_MODEL` (must have a price in `budget.prices`).
  - `BudgetSettings.intraday_run_usd: Decimal = Decimal("0.75")` (must not exceed `day_usd`).
  - `traider.config.ResearchSettings.posture_alert_after_open_min: int = 5` (1..120).
  - Settings versions written before C2a still load (every new field has a default).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_research_job_settings.py` (1 of 2), replace:

```python
from datetime import date
```

with:

```python
from datetime import date, time
```

Append to the end of `tests/unit/test_research_job_settings.py` (2 of 2), after two blank lines:

```python
# --- C2a: intraday runs and the scorecard ------------------------------------------------


def test_the_c2a_defaults_are_the_specs():
    s = ResearchJobSettings()
    assert s.dive.intraday_model == DEFAULT_MODEL
    assert s.budget.intraday_run_usd == Decimal("0.75")
    i = s.intraday
    assert (i.enabled, i.last_start, i.max_candidates) == (True, time(15, 0), 30)
    assert (i.deep_dive_count, i.max_run_s) == (3, 600.0)
    assert (s.scorecard.enabled, s.scorecard.lookback_days) == (True, 30)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"budget": {"intraday_run_usd": "9"}}, "intraday_run_usd"),
        ({"dive": {"intraday_model": "anthropic.claude-haiku-5"}}, "no price"),
        ({"intraday": {"last_start": "09:30"}}, "inside the session"),
        ({"intraday": {"last_start": "16:00"}}, "inside the session"),
        ({"intraday": {"last_start": "15:00+00:00"}}, "without a timezone"),
        ({"intraday": {"deep_dive_count": 11}}, "deep_dive_count"),
        ({"intraday": {"max_run_s": 1201}}, "max_run_s"),
        ({"scorecard": {"lookback_days": 4}}, "lookback_days"),
        ({"scorecard": {"lookback_days": 61}}, "lookback_days"),
        ({"scorecard": {"surprise": 1}}, "surprise"),
    ],
)
def test_inconsistent_c2a_settings_are_rejected(fields, message):
    with pytest.raises(ValidationError, match=message):
        jobs(**fields)


def test_a_cheaper_intraday_model_needs_its_price():
    haiku = "anthropic.claude-haiku-5"
    s = jobs(
        dive={"intraday_model": haiku},
        budget={
            "prices": {
                haiku: {"in_per_mtok": "1", "out_per_mtok": "5"},
                DEFAULT_MODEL: {"in_per_mtok": "2", "out_per_mtok": "10"},
            }
        },
    )
    assert s.dive.intraday_model == haiku


def test_c2a_settings_round_trip_and_older_versions_still_load():
    settings = Settings(research_jobs={"intraday": {"last_start": "14:30"}})
    again = Settings.model_validate_json(settings.model_dump_json())
    assert again.research_jobs.intraday.last_start == time(14, 30)
    body = Settings().model_dump(mode="json")
    for name in ("intraday", "scorecard"):
        del body["research_jobs"][name]
    del body["research_jobs"]["dive"]["intraday_model"]
    del body["research_jobs"]["budget"]["intraday_run_usd"]
    assert Settings.model_validate(body).research_jobs == ResearchJobSettings()
```

In `tests/unit/test_config.py`, replace:

```python
    assert s.swing_lookback_days == 10
```

with:

```python
    assert s.swing_lookback_days == 10
    assert s.posture_alert_after_open_min == 5


@pytest.mark.parametrize("minutes", [0, 121])
def test_the_missing_posture_alert_delay_is_bounded(minutes):
    with pytest.raises(ConfigError, match="posture_alert_after_open_min"):
        Config.from_env(
            {**BASE, "TRAIDER_RESEARCH": json.dumps({"posture_alert_after_open_min": minutes})}
        )
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_research_job_settings.py tests/unit/test_config.py -q`
Expected: FAIL (8 failed, 66 passed), for example `AttributeError: 'DiveSettings' object has no attribute 'intraday_model'`.

- [ ] **Step 3: Implement**

In `src/traider/research/job_settings.py` (1 of 6), replace:

```python
from datetime import date
```

with:

```python
from datetime import date, time
```

In `src/traider/research/job_settings.py` (2 of 6), replace:

```python
    posture_model: Annotated[str, Field(min_length=1, max_length=200)] = DEFAULT_MODEL
```

with:

```python
    posture_model: Annotated[str, Field(min_length=1, max_length=200)] = DEFAULT_MODEL
    # The intraday runs' deep-dives. A smaller model is the owner's choice, once it has a
    # price in budget.prices.
    intraday_model: Annotated[str, Field(min_length=1, max_length=200)] = DEFAULT_MODEL
```

In `src/traider/research/job_settings.py` (3 of 6), replace:

```python
    day_usd: Usd = Decimal("8.00")
```

with:

```python
    day_usd: Usd = Decimal("8.00")
    # One intraday run. Counts toward day_usd like every other run.
    intraday_run_usd: Usd = Decimal("0.75")
```

In `src/traider/research/job_settings.py` (4 of 6), replace:

```python
            raise ValueError("run_usd cannot exceed day_usd")
        return self
```

with:

```python
            raise ValueError("run_usd cannot exceed day_usd")
        if self.intraday_run_usd > self.day_usd:
            raise ValueError("intraday_run_usd cannot exceed day_usd")
        return self


class IntradaySettings(_Group):
    """The runs every 30 minutes during the session (C2a)."""

    enabled: bool = True
    # New York time. A start later than this (plus a few minutes for the task to start)
    # does nothing, so the last run ends well before the bot's intraday flatten.
    last_start: time = time(15, 0)
    max_candidates: Annotated[int, Field(ge=1, le=100)] = 30
    deep_dive_count: Annotated[int, Field(ge=1, le=10)] = 3
    max_run_s: Annotated[float, Field(ge=60, le=1200)] = 600.0

    @field_validator("last_start")
    @classmethod
    def _in_the_session(cls, value: time) -> time:
        if value.tzinfo is not None:
            raise ValueError("last_start is a New York wall-clock time, without a timezone")
        if not time(9, 30) < value < time(16, 0):
            raise ValueError("last_start must be inside the session, after 09:30 and before 16:00")
        return value


class ScorecardSettings(_Group):
    """The daily scorecard after the close (C2a)."""

    enabled: bool = True
    # Picks from this many weekdays back are scored.
    lookback_days: Annotated[int, Field(ge=5, le=60)] = 30
```

In `src/traider/research/job_settings.py` (5 of 6), replace:

```python
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
```

with:

```python
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    intraday: IntradaySettings = Field(default_factory=IntradaySettings)
    scorecard: ScorecardSettings = Field(default_factory=ScorecardSettings)
```

In `src/traider/research/job_settings.py` (6 of 6), replace:

```python
        for model in (self.dive.model, self.dive.posture_model):
```

with:

```python
        for model in (self.dive.model, self.dive.posture_model, self.dive.intraday_model):
```

In `src/traider/config.py`, replace:

```python
    swing_lookback_days: Annotated[int, Field(ge=1, le=30)] = 10
```

with:

```python
    swing_lookback_days: Annotated[int, Field(ge=1, le=30)] = 10
    # This many minutes after the open, with no usable posture for today, the bot says so
    # once (research_no_posture). It stands aside either way.
    posture_alert_after_open_min: Annotated[int, Field(ge=1, le=120)] = 5
```

The infra test `test_example_configuration_shows_research_off_and_the_real_research_defaults` compares the example's `researchSettings` with `ResearchSettings()`, so the example gains the new field:

In `infra/Pulumi.example.yaml`, replace:

```yaml
  #   swing_lookback_days: 10    # earlier trading days to look back for swing picks that have not expired
```

with:

```yaml
  #   swing_lookback_days: 10    # earlier trading days to look back for swing picks that have not expired
  #   posture_alert_after_open_min: 5  # no usable posture this long after the open: one alert
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `uv run pytest tests/unit/test_research_job_settings.py tests/unit/test_config.py -q`
Expected: PASS (74 passed).

- [ ] **Step 5: Break on purpose (restore after each)**

Make each change, run the named tests, see them FAIL, then undo the change exactly.

- **Break 1.** Let an intraday run's budget exceed the day's. In `src/traider/research/job_settings.py`, replace:

  ```python
          if self.intraday_run_usd > self.day_usd:
              raise ValueError("intraday_run_usd cannot exceed day_usd")
  ```

  with nothing (delete those lines).

  Run: `uv run pytest tests/unit/test_research_job_settings.py -q`. These FAIL: `test_inconsistent_c2a_settings_are_rejected[fields0-intraday_run_usd]`.

- **Break 2.** Let the intraday model go unpriced. In `src/traider/research/job_settings.py`, replace:

  ```python
  for model in (self.dive.model, self.dive.posture_model, self.dive.intraday_model):
  ```

  with:

  ```python
  for model in (self.dive.model, self.dive.posture_model):
  ```

  Run: `uv run pytest tests/unit/test_research_job_settings.py -q`. These FAIL: `test_inconsistent_c2a_settings_are_rejected[fields1-no price]`.

- [ ] **Step 6: Whole suite, lint, types**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: all green: 2093 passed, 5 skipped.

Then from `infra/`: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: all green: 158 passed.

- [ ] **Step 7: Commit**

```bash
git add src/traider/research/job_settings.py src/traider/config.py infra/Pulumi.example.yaml \
  tests/unit/test_research_job_settings.py tests/unit/test_config.py
git commit -m "feat(settings): intraday, scorecard and posture-alert settings

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Xh7zur22CRYEGaDoTB5nkV"
```

---

### Task 2: Pick outcomes, the daily summary, and the store additions

**Files:**
- Modify: `src/traider/timeutil.py`, `src/traider/research/models.py`, `src/traider/research/store.py`
- Test: `tests/unit/test_timeutil.py`, `tests/unit/test_research_models.py`, `tests/unit/test_research_store.py`

**Interfaces:**
- Produces:
  - `traider.timeutil.weekdays_from(start: date, end: date) -> list[date]` (both ends included, oldest first; empty when `end < start`).
  - `traider.research.models`: `OutcomeStatus` (`PENDING`, `PARTIAL`, `FINAL`); `outcome_key(run_id: str, rank: int) -> str` (`PICK#<run_id>#<rank:03d>`); `PickOutcome` (frozen, `extra="forbid"`, every float finite; fields `run_id, rank, symbol, side: PickSide, horizon: Horizon, score, pre_score, llm_score: int | None, pick_day: date, run_status: RunStatus (ok or partial only), entry, price_at_pick, ret_0d, ret_1d, ret_5d, ret_20d, mfe_pct, mae_pct, hit_invalidation: bool | None, expired_return, traded: bool | None, status: OutcomeStatus, updated_at` (aware); property `key`); `HorizonStats(matured, hits, mean_pct)`; `ScoreBucket(low, high, count, mean_ret_1d_pct)`; `ScoreSummary(day, run_id, picks, by_kind, by_side, ret_1d, ret_5d, buckets, updated_at)`.
  - `ResearchWriter` (and both stores) gain: `async day(day: str) -> DayResearch` (already on both stores, now on the protocol), `async picks_between(start: date, end: date) -> list[DayResearch]`, `async outcomes(keys: Sequence[str]) -> dict[str, PickOutcome]` (BatchGetItem, 100 a request, consistent, unprocessed keys asked for again with backoff; unreadable or mis-keyed items absent), `async put_outcome(outcome: PickOutcome) -> None` (`PICK#…` / `OUTCOME`), `async put_score_summary(day: str, summary: ScoreSummary) -> None` (`SCORE#<day>` / `SUMMARY`; refuses a summary of another day).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_timeutil.py` (1 of 2), replace:

```text
    trading_date,
```

with:

```text
    trading_date,
    weekdays_from,
```

Append to the end of `tests/unit/test_timeutil.py` (2 of 2), after two blank lines:

```python
def test_weekdays_from_includes_both_ends_and_skips_weekends():
    friday, tuesday = date(2026, 10, 9), date(2026, 10, 13)
    assert weekdays_from(friday, tuesday) == [friday, date(2026, 10, 12), tuesday]
    assert weekdays_from(date(2026, 10, 10), date(2026, 10, 11)) == []
    assert weekdays_from(tuesday, friday) == []
```

In `tests/unit/test_research_models.py` (1 of 3), replace:

```text
    Pick,
```

with:

```text
    OutcomeStatus,
    Pick,
    PickOutcome,
```

In `tests/unit/test_research_models.py` (2 of 3), replace:

```text
    RunStatus,
```

with:

```text
    RunStatus,
    ScoreSummary,
    outcome_key,
```

Append to the end of `tests/unit/test_research_models.py` (3 of 3), after two blank lines:

```python
# --- C2a: pick outcomes and the scorecard summary -----------------------------------------


def outcome(**overrides) -> PickOutcome:
    fields = {
        "run_id": "premarket-20261009T120000Z-ab12",
        "rank": 3,
        "symbol": "NVDA",
        "side": "long",
        "horizon": "swing",
        "score": 84,
        "pre_score": 89,
        "llm_score": 82,
        "pick_day": "2026-10-09",
        "run_status": "ok",
        "entry": 104.0,
        "ret_1d": 1.25,
        "status": "partial",
        "updated_at": "2026-10-09T20:30:00+00:00",
    }
    return PickOutcome.model_validate(fields | overrides)


def test_an_outcome_parses_and_knows_its_key():
    o = outcome()
    assert (o.status, o.key) == (OutcomeStatus.PARTIAL, "PICK#premarket-20261009T120000Z-ab12#003")
    assert outcome_key("r1", 12) == "PICK#r1#012"
    assert (o.ret_5d, o.traded, o.hit_invalidation) == (None, None, None)


@pytest.mark.parametrize(
    "overrides",
    [
        {"surprise": 1},
        {"ret_1d": float("nan")},
        {"mfe_pct": float("inf")},
        {"entry": 0},
        {"run_status": "failed"},
        {"run_status": "running"},
        {"status": "done"},
        {"updated_at": "2026-10-09T20:30:00"},
        {"symbol": "nvda"},
        {"llm_score": 101},
    ],
)
def test_a_bad_outcome_is_rejected(overrides):
    with pytest.raises(ValidationError):
        outcome(**overrides)


def test_a_summary_parses_with_defaults():
    s = ScoreSummary.model_validate(
        {"day": "2026-10-09", "run_id": "scorecard-x", "picks": 0, "updated_at": T0.isoformat()}
    )
    assert (s.ret_1d.matured, s.ret_1d.mean_pct, s.buckets) == (0, None, ())
    with pytest.raises(ValidationError):
        ScoreSummary.model_validate(s.model_dump() | {"picks": -1})
```

In `tests/unit/test_research_store.py` (1 of 2), replace:

```python
from traider.research.models import Pick, Posture, RunMeta
```

with:

```python
from traider.research.models import Pick, PickOutcome, Posture, RunMeta, ScoreSummary
```

Append to the end of `tests/unit/test_research_store.py` (2 of 2), after two blank lines:

```python
# --- C2a: the scorecard's items ------------------------------------------------------------


def outcome(run_id="r1", rank=1, **overrides) -> PickOutcome:
    fields = {
        "run_id": run_id,
        "rank": rank,
        "symbol": "NVDA",
        "side": "long",
        "horizon": "intraday",
        "score": 80,
        "pre_score": 70,
        "pick_day": DAY,
        "run_status": "ok",
        "status": "pending",
        "updated_at": T1.isoformat(),
    }
    return PickOutcome.model_validate(fields | overrides)


async def test_outcomes_read_back_by_key_and_missing_ones_are_absent(store):
    first, second = outcome(rank=1), outcome(rank=2, status="final", ret_1d=2.5)
    await store.put_outcome(first)
    await store.put_outcome(second)
    found = await store.outcomes([first.key, second.key, "PICK#nobody#001", first.key])
    assert found == {first.key: first, second.key: second}
    assert await store.outcomes([]) == {}


async def test_an_outcome_is_overwritten_not_duplicated(store):
    await store.put_outcome(outcome())
    await store.put_outcome(outcome(status="partial", ret_1d=-1.0))
    (found,) = (await store.outcomes(["PICK#r1#001"])).values()
    assert (found.status.value, found.ret_1d) == ("partial", -1.0)


async def test_an_unreadable_outcome_or_one_under_the_wrong_key_is_absent(store):
    put_raw(store, "PICK#r1#001", "OUTCOME", "{not json")
    put_raw(store, "PICK#r1#002", "OUTCOME", outcome(rank=3).model_dump_json())
    assert await store.outcomes(["PICK#r1#001", "PICK#r1#002"]) == {}


async def test_outcomes_read_more_than_one_batch(store):
    many = [outcome(rank=rank) for rank in range(1, 251)]
    for item in many:
        await store.put_outcome(item)
    found = await store.outcomes([o.key for o in many])
    assert sorted(found) == sorted(o.key for o in many)


async def test_dynamo_asks_again_for_unprocessed_keys(store):
    if isinstance(store, MemoryResearchStore):
        pytest.skip("unprocessed keys are a DynamoDB feature")
    await store.put_outcome(outcome(rank=1))
    await store.put_outcome(outcome(rank=2))
    client = store._table.meta.client
    real = client.batch_get_item
    calls = []

    def flaky(**kwargs):
        calls.append(kwargs)
        response = real(**kwargs)
        if len(calls) == 1:  # the first answer leaves the second key for later
            keys = kwargs["RequestItems"][TABLE]["Keys"]
            response["Responses"][TABLE] = [
                i for i in response["Responses"][TABLE] if i["pk"] == keys[0]["pk"]
            ]
            response["UnprocessedKeys"] = {TABLE: {"Keys": keys[1:], "ConsistentRead": True}}
        return response

    client.batch_get_item = flaky
    found = await store.outcomes(["PICK#r1#001", "PICK#r1#002"])
    assert sorted(found) == ["PICK#r1#001", "PICK#r1#002"]
    assert [len(c["RequestItems"][TABLE]["Keys"]) for c in calls] == [2, 1]
    assert all(c["RequestItems"][TABLE]["ConsistentRead"] for c in calls)


async def test_outcome_items_never_show_up_in_the_bots_day_read(store):
    await store.put_outcome(outcome())
    day = await store.day(DAY)
    assert (day.picks, day.postures, day.invalid) == ((), (), 0)


async def test_picks_between_reads_each_weekday_with_its_runs(store):
    await store.write_run(meta("r1", day="2026-10-08"), [pick(run_id="r1")], posture("trade", "r1"))
    await store.write_run(meta("r2", day=DAY), [pick("AMD", run_id="r2")], None)
    days = await store.picks_between(date(2026, 10, 2), date(2026, 10, 9))
    # Friday 10-02 to Friday 10-09: six weekdays, the weekend left out.
    assert [d.day for d in days] == [
        "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
    ]  # fmt: skip
    by_day = {d.day: d for d in days}
    assert [p.symbol for p in by_day["2026-10-08"].picks] == ["NVDA"]
    assert set(by_day["2026-10-08"].runs) == {"r1"}
    assert [p.symbol for p in by_day[DAY].picks] == ["AMD"]
    assert await store.picks_between(date(2026, 10, 10), date(2026, 10, 11)) == []


async def test_a_summary_is_stored_under_its_day_and_only_there(store):
    summary = ScoreSummary(day=date(2026, 10, 9), run_id="scorecard-x", picks=3, updated_at=T1)
    await store.put_score_summary(DAY, summary)
    if isinstance(store, MemoryResearchStore):
        body = store.raw(f"SCORE#{DAY}", "SUMMARY")["body"]
    else:
        body = store._table.get_item(Key={"pk": f"SCORE#{DAY}", "sk": "SUMMARY"})["Item"]["body"]
    assert ScoreSummary.model_validate_json(body) == summary
    with pytest.raises(ValueError, match="cannot be stored under"):
        await store.put_score_summary("2026-10-08", summary)
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_timeutil.py tests/unit/test_research_models.py tests/unit/test_research_store.py -q`
Expected: FAIL: collection errors (3 errors), `ImportError: cannot import name 'weekdays_from'` and `cannot import name 'OutcomeStatus'`.

- [ ] **Step 3: Implement**

Append to the end of `src/traider/timeutil.py`, after two blank lines:

```python
def weekdays_from(start: date, end: date) -> list[date]:
    """The weekdays from ``start`` to ``end``, both included, oldest first. Holidays are
    not known here."""
    days: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days
```

Append to the end of `src/traider/research/models.py`, after two blank lines:

```python
# --- the scorecard (C2a) ------------------------------------------------------------------

Percent = Annotated[float, Field(allow_inf_nan=False)]
Price = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class OutcomeStatus(StrEnum):
    PENDING = "pending"  # no entry price yet (the pick day's bar is missing)
    PARTIAL = "partial"  # some returns known, the 20-day one not yet
    FINAL = "final"  # the 20-day close and the expiry are known, or 30 weekdays have passed


def outcome_key(run_id: str, rank: int) -> str:
    """The partition key of a pick's outcome: ``PICK#<run_id>#<rank:03d>``."""
    return f"PICK#{run_id}#{rank:03d}"


class PickOutcome(BaseModel):
    """How one pick did, from Schwab daily bars. Every return is a percentage, signed by
    the pick's side: a long gains when the price rises, a bearish pick when it falls."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=100)
    rank: int = Field(strict=True, ge=1, le=999)
    symbol: str
    side: PickSide
    horizon: Horizon
    score: Score
    pre_score: Score
    llm_score: Score | None = None  # None for a hand-made pick
    pick_day: date
    run_status: RunStatus
    entry: Price | None = None  # the pick day's open
    price_at_pick: Price | None = None
    ret_0d: Percent | None = None  # intraday picks only: entry to the pick day's close
    ret_1d: Percent | None = None
    ret_5d: Percent | None = None
    ret_20d: Percent | None = None
    mfe_pct: Percent | None = None
    mae_pct: Percent | None = None
    hit_invalidation: bool | None = None
    expired_return: Percent | None = None
    traded: bool | None = None  # None: the bot's event log could not be read
    status: OutcomeStatus
    updated_at: datetime

    @property
    def key(self) -> str:
        return outcome_key(self.run_id, self.rank)

    @field_validator("symbol")
    @classmethod
    def _symbol_ok(cls, value: str) -> str:
        return check_symbols((value,))[0]

    @field_validator("run_status")
    @classmethod
    def _finished_run(cls, value: RunStatus) -> RunStatus:
        if value not in (RunStatus.OK, RunStatus.PARTIAL):
            raise ValueError("only picks from ok or partial runs are scored")
        return value

    @field_validator("updated_at")
    @classmethod
    def _updated_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class HorizonStats(BaseModel):
    """Picks whose return over one horizon became known today."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    matured: int = Field(default=0, ge=0)
    hits: int = Field(default=0, ge=0)  # return above zero
    mean_pct: Percent | None = None


class ScoreBucket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    low: Score
    high: Score
    count: int = Field(ge=0)
    mean_ret_1d_pct: Percent | None = None


class ScoreSummary(BaseModel):
    """One scorecard run's summary, item ``SCORE#<day>`` / ``SUMMARY``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    day: date
    run_id: str = Field(min_length=1, max_length=100)
    picks: int = Field(ge=0)
    by_kind: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    by_side: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    ret_1d: HorizonStats = Field(default_factory=HorizonStats)
    ret_5d: HorizonStats = Field(default_factory=HorizonStats)
    buckets: tuple[ScoreBucket, ...] = ()
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def _updated_aware(cls, value: datetime) -> datetime:
        return _aware(value)
```

In `src/traider/research/store.py` (1 of 6), replace:

```text
    LOCK#<name>   / LOCK                       one research run of a kind at a time
```

with:

```text
    LOCK#<name>   / LOCK                       one research run of a kind at a time
    PICK#<run_id>#<rank:03d> / OUTCOME         how that pick did (the scorecard)
    SCORE#<date>  / SUMMARY                    the scorecard's summary for that day
```

In `src/traider/research/store.py` (2 of 6), replace:

```python
from datetime import datetime
```

with:

```python
from datetime import date, datetime
```

In `src/traider/research/store.py` (3 of 6), replace:

```python
from traider.research.models import Pick, Posture, RunKind, RunMeta
```

with:

```python
from traider.research.models import (
    Pick,
    PickOutcome,
    Posture,
    RunKind,
    RunMeta,
    ScoreSummary,
)
from traider.timeutil import weekdays_from
```

In `src/traider/research/store.py` (4 of 6), replace:

```python
    async def release_lock(self, name: str, owner: str) -> None: ...
```

with:

```python
    async def release_lock(self, name: str, owner: str) -> None: ...

    async def day(self, day: str) -> DayResearch: ...

    async def picks_between(self, start: date, end: date) -> list[DayResearch]:
        """Each weekday's research from ``start`` to ``end``, both included, oldest first."""
        ...

    async def outcomes(self, keys: Sequence[str]) -> dict[str, PickOutcome]:
        """The stored outcomes for these ``PICK#...`` keys. Missing or unreadable: absent."""
        ...

    async def put_outcome(self, outcome: PickOutcome) -> None: ...

    async def put_score_summary(self, day: str, summary: ScoreSummary) -> None: ...


BATCH_GET_MAX = 100  # DynamoDB's limit per BatchGetItem request
BATCH_GET_ROUNDS = 5  # unprocessed keys are asked for again at most this often
BATCH_GET_BACKOFF_S = 0.1  # doubled before each new ask


def _outcome_item(outcome: PickOutcome) -> dict[str, Any]:
    return {
        "pk": outcome.key,
        "sk": "OUTCOME",
        "body": json.dumps(outcome.model_dump(mode="json")),
    }


def _summary_item(day: str, summary: ScoreSummary) -> dict[str, Any]:
    if summary.day.isoformat() != day:
        raise ValueError(f"a summary for {summary.day} cannot be stored under {day}")
    return {
        "pk": f"SCORE#{day}",
        "sk": "SUMMARY",
        "body": json.dumps(summary.model_dump(mode="json")),
    }


def _parse_outcomes(items: Sequence[Mapping[str, Any]]) -> dict[str, PickOutcome]:
    found: dict[str, PickOutcome] = {}
    for item in items:
        try:
            outcome = PickOutcome.model_validate(json.loads(str(item["body"])))
        except Exception:
            log.warning("skipping an unreadable pick outcome %s", item.get("pk"))
            continue
        if outcome.key == item.get("pk"):  # an outcome stored under another pick's key: skip
            found[outcome.key] = outcome
    return found
```

In `src/traider/research/store.py` (5 of 6), replace:

```text
        return _parse_day(day, items, metas)

```

with:

```python
        return _parse_day(day, items, metas)

    async def picks_between(self, start: date, end: date) -> list[DayResearch]:
        return [await self.day(d.isoformat()) for d in weekdays_from(start, end)]

    async def outcomes(self, keys: Sequence[str]) -> dict[str, PickOutcome]:
        items = [item for key in dict.fromkeys(keys) if (item := self._items.get((key, "OUTCOME")))]
        return _parse_outcomes(items)

    async def put_outcome(self, outcome: PickOutcome) -> None:
        item = _outcome_item(outcome)
        self._items[(item["pk"], item["sk"])] = item

    async def put_score_summary(self, day: str, summary: ScoreSummary) -> None:
        item = _summary_item(day, summary)
        self._items[(item["pk"], item["sk"])] = item

```

Append to the end of `src/traider/research/store.py` (6 of 6), after one blank line:

```text
    async def picks_between(self, start: date, end: date) -> list[DayResearch]:
        return [await self.day(d.isoformat()) for d in weekdays_from(start, end)]

    async def outcomes(self, keys: Sequence[str]) -> dict[str, PickOutcome]:
        """BatchGetItem, 100 keys a request, consistent reads. The table's client takes and
        returns plain values. Keys DynamoDB leaves unprocessed are asked for again after a
        growing pause; any still missing after that are absent, so the scorecard scores
        those picks again (an overwrite, never a loss)."""
        unique = list(dict.fromkeys(keys))
        items: list[dict[str, Any]] = []
        client = self._table.meta.client
        name = self._table.name
        for start in range(0, len(unique), BATCH_GET_MAX):
            wanted: list[dict[str, Any]] = [
                {"pk": key, "sk": "OUTCOME"} for key in unique[start : start + BATCH_GET_MAX]
            ]
            for attempt in range(BATCH_GET_ROUNDS):
                if attempt:
                    await asyncio.sleep(BATCH_GET_BACKOFF_S * 2 ** (attempt - 1))
                response = await self._call(
                    client.batch_get_item,
                    RequestItems={name: {"Keys": wanted, "ConsistentRead": True}},
                )
                items.extend(response.get("Responses", {}).get(name, []))
                wanted = response.get("UnprocessedKeys", {}).get(name, {}).get("Keys", [])
                if not wanted:
                    break
            else:
                log.warning(
                    "%d pick outcome(s) stayed unprocessed; scoring them again", len(wanted)
                )
        return _parse_outcomes(items)

    async def put_outcome(self, outcome: PickOutcome) -> None:
        await self._call(self._table.put_item, Item=_outcome_item(outcome))

    async def put_score_summary(self, day: str, summary: ScoreSummary) -> None:
        await self._call(self._table.put_item, Item=_summary_item(day, summary))
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `uv run pytest tests/unit/test_timeutil.py tests/unit/test_research_models.py tests/unit/test_research_store.py -q`
Expected: PASS (137 passed, 6 skipped; the skips are the memory store's half of DynamoDB-only tests).

- [ ] **Step 5: Break on purpose (restore after each)**

Make each change, run the named tests, see them FAIL, then undo the change exactly.

- **Break 1.** Trust an outcome stored under another pick's key. In `src/traider/research/store.py`, replace:

  ```python
          if outcome.key == item.get("pk"):  # an outcome stored under another pick's key: skip
              found[outcome.key] = outcome
  ```

  with:

  ```python
          found[outcome.key] = outcome
  ```

  Run: `uv run pytest tests/unit/test_research_store.py -q`. These FAIL: `test_an_unreadable_outcome_or_one_under_the_wrong_key_is_absent[memory]` and `[dynamo]`.

- **Break 2.** Read outcomes eventually consistently. In `src/traider/research/store.py`, replace:

  ```text
  RequestItems={name: {"Keys": wanted, "ConsistentRead": True}},
  ```

  with:

  ```text
  RequestItems={name: {"Keys": wanted}},
  ```

  Run: `uv run pytest tests/unit/test_research_store.py -q`. These FAIL: `test_dynamo_asks_again_for_unprocessed_keys[dynamo]`.

- [ ] **Step 6: Whole suite, lint, types**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: all green: 2121 passed, 6 skipped.

- [ ] **Step 7: Commit**

```bash
git add src/traider/timeutil.py src/traider/research/models.py src/traider/research/store.py \
  tests/unit/test_timeutil.py tests/unit/test_research_models.py tests/unit/test_research_store.py
git commit -m "feat(research): pick outcomes and score summaries in the store

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Xh7zur22CRYEGaDoTB5nkV"
```

---

### Task 3: The scorecard's maths

**Files:**
- Create: `src/traider/research/scorecard.py`
- Test: `tests/unit/test_research_scorecard.py` (new)

**Interfaces:**
- Consumes: `PickOutcome`, `OutcomeStatus`, `HorizonStats`, `ScoreBucket`, `ScoreSummary` (Task 2); `weekdays_from` (Task 2); `traider.universe.root_symbol`; `traider.research.market.DailyBar`.
- Produces (`traider.research.scorecard`):
  - `HORIZONS = (1, 5, 20)`, `FINAL_AFTER_WEEKDAYS = 30`, `BUCKETS = ((60, 69), (70, 79), (80, 89), (90, 100))`.
  - `Scored(outcome: PickOutcome, matured: frozenset[str] = frozenset())`: `matured` names the horizons (`"ret_1d"`, `"ret_5d"`, `"ret_20d"`) whose bar is today's.
  - `signed_pct(entry: float, price: float, side: PickSide) -> float` (percent, 4 places, positive when it favours the side).
  - `score_pick(pick: Pick, *, pick_day: date, run_status: RunStatus, bars: Sequence[DailyBar], today: date, traded: bool | None, now: datetime) -> Scored`.
  - `traded_from_logs(logs: Mapping[date, Sequence[Mapping[str, Any]] | None], symbol: str, start: datetime, end: datetime) -> bool | None`.
  - `summarize(scored: Sequence[Scored], *, day: date, run_id: str, kinds: Mapping[str, str], now: datetime) -> ScoreSummary`, `summary_text(summary: ScoreSummary) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_research_scorecard.py`:

```python
"""The scorecard's maths, on fixed bar series: returns signed by side, excursions, the
invalidation, status transitions, "traded" from the event log, and the summary."""

from datetime import UTC, date, datetime

import pytest

from traider.research.market import DailyBar
from traider.research.models import OutcomeStatus, Pick, RunStatus
from traider.research.rank import close_of
from traider.research.scorecard import (
    score_pick,
    signed_pct,
    summarize,
    summary_text,
    traded_from_logs,
)
from traider.timeutil import weekdays_after

NOW = datetime(2026, 10, 12, 20, 30, tzinfo=UTC)  # Monday 16:30 New York
MON, TUE, WED, THU, FRI = (date(2026, 10, d) for d in (5, 6, 7, 8, 9))
NEXT_MON = date(2026, 10, 12)


def bar(day, o, h, low, c) -> DailyBar:
    return DailyBar(day=day, open=o, high=h, low=low, close=c, volume=1_000_000)


# Monday 10-05 to Monday 10-12. The pick day is Monday: entry 100.
WEEK = [
    bar(MON, 100, 103, 99, 102),
    bar(TUE, 102, 104, 98, 101),
    bar(WED, 101, 106, 100, 105),
    bar(THU, 105, 107, 103, 104),
    bar(FRI, 104, 105, 94, 96),
    bar(NEXT_MON, 96, 97, 90, 91),  # after the expiry
]


def pick(side="long", horizon="swing", invalidation="95", expires=FRI, **overrides) -> Pick:
    fields = {
        "run_id": "premarket-a",
        "rank": 1,
        "symbol": "NVDA",
        "side": side,
        "horizon": horizon,
        "score": 84,
        "pre_score": 70,
        "thesis": "t",
        "invalidation": invalidation,
        "expires_at": close_of(expires).isoformat(),
        "features": {"llm_score": 82.0, "price_at_pick": 99.5},
    }
    return Pick.model_validate(fields | overrides)


def score(p, *, bars=WEEK, pick_day=MON, today=NEXT_MON, traded=False, status=RunStatus.OK):
    return score_pick(
        p, pick_day=pick_day, run_status=status, bars=bars, today=today, traded=traded, now=NOW
    )


# --- returns, excursions, invalidation --------------------------------------------------


def test_a_long_swing_pick_golden():
    scored = score(pick())
    o = scored.outcome
    assert (o.entry, o.price_at_pick, o.llm_score) == (100.0, 99.5, 82)
    assert (o.ret_0d, o.ret_1d, o.ret_5d, o.ret_20d) == (None, 2.0, -4.0, None)
    assert (o.mfe_pct, o.mae_pct) == (7.0, -6.0)  # high 107, low 94, inside the window
    assert o.hit_invalidation is True  # Friday's low 94 is under 95
    assert o.expired_return == -4.0  # Friday's close
    assert (o.status, o.traded, o.run_status) == (OutcomeStatus.PARTIAL, False, RunStatus.OK)
    assert scored.matured == frozenset()  # nothing matured on Monday the 12th


def test_a_bearish_pick_is_signed_the_other_way():
    o = score(pick(side="bearish", invalidation="108")).outcome
    assert (o.ret_1d, o.ret_5d) == (-2.0, 4.0)
    assert (o.mfe_pct, o.mae_pct) == (6.0, -7.0)  # the fall to 94 is favourable
    assert o.hit_invalidation is False  # the highest high, 107, stayed under 108
    assert o.expired_return == 4.0


def test_a_bearish_invalidation_is_hit_by_a_high_at_or_above_it():
    assert score(pick(side="bearish", invalidation="107")).outcome.hit_invalidation is True


def test_the_excursions_stop_at_the_expiry():
    # Monday the 12th's low of 90 is after Friday's expiry: not counted.
    o = score(pick(expires=THU)).outcome
    assert (o.mfe_pct, o.mae_pct) == (7.0, -2.0)
    assert o.hit_invalidation is False
    assert o.expired_return == 4.0  # Thursday's close, 104


def test_an_intraday_pick_has_a_same_day_return():
    o = score(pick(horizon="intraday", expires=WED), pick_day=WED).outcome
    assert o.entry == 101.0
    assert o.ret_0d == o.ret_1d == pytest.approx(3.9604)
    assert o.ret_5d is None  # Wednesday to Monday is four bars
    assert (o.mfe_pct, o.mae_pct) == (pytest.approx(4.9505), pytest.approx(-0.9901))
    assert o.expired_return == pytest.approx(3.9604)
    assert o.status is OutcomeStatus.PARTIAL


def test_signed_pct_rounds_to_four_places():
    assert signed_pct(3.0, 4.0, pick().side) == 33.3333
    assert signed_pct(3.0, 4.0, pick(side="bearish", invalidation="108").side) == -33.3333


def test_returns_matured_today_are_flagged():
    assert score(pick(), today=MON).matured == {"ret_1d"}
    assert score(pick(), today=FRI).matured == {"ret_5d"}


# --- status ---------------------------------------------------------------------------------


def test_without_the_pick_days_bar_the_outcome_is_pending():
    o = score(pick(), bars=WEEK[1:]).outcome
    assert (o.status, o.entry, o.ret_1d, o.mfe_pct) == (OutcomeStatus.PENDING, None, None, None)
    assert (o.price_at_pick, o.traded) == (99.5, False)


def test_no_bars_at_all_is_pending_too():
    assert score(pick(), bars=[]).outcome.status is OutcomeStatus.PENDING


def trading_bars(start: date, n: int) -> list[DailyBar]:
    days = [start] + [weekdays_after(start, i) for i in range(1, n)]
    return [bar(d, 100, 101, 99, 100 + i) for i, d in enumerate(days)]


def test_the_twenty_day_close_and_the_expiry_make_it_final():
    expires = weekdays_after(MON, 19)  # the 20th bar's day
    bars = trading_bars(MON, 20)
    o = score(pick(expires=expires), bars=bars, today=expires).outcome
    assert o.ret_20d == 19.0
    assert o.status is OutcomeStatus.FINAL


def test_the_twenty_day_close_alone_is_not_final_while_the_pick_is_live():
    expires = weekdays_after(MON, 20)  # a swing of 20 weekdays outlives the 20th bar
    bars = trading_bars(MON, 20)
    o = score(pick(expires=expires), bars=bars, today=bars[-1].day).outcome
    assert (o.ret_20d, o.expired_return) == (19.0, None)
    assert o.status is OutcomeStatus.PARTIAL


def test_a_pick_thirty_weekdays_old_is_final_even_without_bars():
    today = weekdays_after(MON, 30)
    assert score(pick(), bars=[], today=today).outcome.status is OutcomeStatus.FINAL
    almost = weekdays_after(MON, 29)
    assert score(pick(), bars=[], today=almost).outcome.status is OutcomeStatus.PENDING


def test_a_hand_made_pick_without_features_still_scores():
    o = score(pick(features={})).outcome
    assert (o.llm_score, o.price_at_pick, o.ret_1d) == (None, None, 2.0)


# --- traded, from the bot's event log -------------------------------------------------------

START = datetime(2026, 10, 5, 12, 30, tzinfo=UTC)
END = close_of(TUE)


def submitted(symbol, side="BUY", at="2026-10-05T14:00:00+00:00", kind="order_submitted"):
    return {"kind": kind, "at": at, "data": {"symbol": symbol, "side": side}}


@pytest.mark.parametrize(
    "event",
    [
        submitted("NVDA"),
        submitted("NVDA  261016P00100000"),  # a put on it
        submitted("NVDA", at="2026-10-06T19:59:00+00:00"),
    ],
)
def test_a_buy_while_the_pick_was_live_counts(event):
    logs = {MON: [event], TUE: []}
    assert traded_from_logs(logs, "NVDA", START, END) is True


@pytest.mark.parametrize(
    "event",
    [
        submitted("NVDA", side="SELL"),
        submitted("NVDAX"),
        submitted("AMD"),
        submitted("NVDA", kind="order_blocked"),
        submitted("NVDA", at="2026-10-05T12:00:00+00:00"),  # before the pick
        submitted("NVDA", at="2026-10-05T14:00:00"),  # no timezone
        submitted("NVDA", at="soon"),
        {"kind": "order_submitted", "at": "2026-10-05T14:00:00+00:00", "data": "NVDA"},
    ],
)
def test_anything_else_does_not(event):
    assert traded_from_logs({MON: [event], TUE: []}, "NVDA", START, END) is False


def test_an_unreadable_day_makes_it_unknown_unless_a_buy_was_found():
    assert traded_from_logs({MON: [], TUE: None}, "NVDA", START, END) is None
    assert traded_from_logs({MON: []}, "NVDA", START, END) is None  # TUE never read
    assert traded_from_logs({MON: [submitted("NVDA")], TUE: None}, "NVDA", START, END) is True


# --- the summary ----------------------------------------------------------------------------


def test_the_summary_counts_hits_means_and_buckets():
    items = [
        score(pick(rank=1, score=84), today=MON),  # +2.0 matured today
        score(pick(rank=2, side="bearish", invalidation="108", score=72), today=MON),  # -2.0
        score(pick(rank=3, score=91, run_id="intraday-b"), today=MON),  # +2.0
        score(pick(rank=4, score=55), bars=WEEK[1:], today=MON),  # pending
    ]
    summary = summarize(
        items,
        day=MON,
        run_id="scorecard-x",
        kinds={"premarket-a": "premarket", "intraday-b": "intraday"},
        now=NOW,
    )
    assert summary.picks == 4
    assert summary.by_kind == {"intraday": 1, "premarket": 3}
    assert summary.by_side == {"bearish": 1, "long": 3}
    assert (summary.ret_1d.matured, summary.ret_1d.hits) == (3, 2)
    assert summary.ret_1d.mean_pct == pytest.approx(0.6667)
    assert (summary.ret_5d.matured, summary.ret_5d.mean_pct) == (0, None)
    buckets = {(b.low, b.high): (b.count, b.mean_ret_1d_pct) for b in summary.buckets}
    assert buckets == {
        (60, 69): (0, None),
        (70, 79): (1, -2.0),
        (80, 89): (1, 2.0),
        (90, 100): (1, 2.0),
    }
    assert summary_text(summary) == (
        "traider scorecard 2026-10-05: 3 picks matured 1d, hit 2/3, mean +0.7%; "
        "no picks matured 5d; 4 picks in the window"
    )


def test_an_empty_summary_reads_plainly():
    summary = summarize([], day=MON, run_id="scorecard-x", kinds={}, now=NOW)
    assert summary.picks == 0 and all(b.count == 0 for b in summary.buckets)
    assert summary_text(summary) == (
        "traider scorecard 2026-10-05: no picks matured 1d; no picks matured 5d; "
        "0 picks in the window"
    )
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_research_scorecard.py -q`
Expected: FAIL: collection error (1 error), `ModuleNotFoundError: No module named 'traider.research.scorecard'`.

- [ ] **Step 3: Implement**

Create `src/traider/research/scorecard.py`:

```python
"""The scorecard's maths: how a pick actually did, from daily bars. Pure functions.

Every return is a percentage from the entry (the pick day's open), signed by the pick's
side: a long gains when the price rises, a bearish pick (long puts) when it falls.

    ret_<h>d          close of the h-th trading day from the pick day (1 = the pick day)
    ret_0d            intraday picks only: the pick day's close
    mfe_pct, mae_pct  the best and the worst signed move inside the live window
    hit_invalidation  long: a low at or below the invalidation; bearish: a high at or above
    expired_return    the close of the expiry day (the last bar of the window once it closed)

The live window runs from the pick day to the expiry day, capped at today. An outcome is
``pending`` without an entry, ``final`` once the 20-day return and the expiry are both
known (or the pick is 30 weekdays old), and ``partial`` in between.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from traider.research.market import DailyBar
from traider.research.models import (
    Horizon,
    HorizonStats,
    OutcomeStatus,
    Pick,
    PickOutcome,
    PickSide,
    RunStatus,
    ScoreBucket,
    ScoreSummary,
)
from traider.timeutil import trading_date, weekdays_between, weekdays_from
from traider.universe import root_symbol

HORIZONS: Final = (1, 5, 20)
FINAL_AFTER_WEEKDAYS: Final = 30
BUCKETS: Final = ((60, 69), (70, 79), (80, 89), (90, 100))
DIGITS: Final = 4


@dataclass(frozen=True, slots=True)
class Scored:
    outcome: PickOutcome
    # The horizons whose return became known today ("ret_1d", "ret_5d", "ret_20d").
    matured: frozenset[str] = frozenset()


def signed_pct(entry: float, price: float, side: PickSide) -> float:
    """The move from ``entry`` to ``price`` in percent, positive when it favours ``side``."""
    move = (price / entry - 1) * 100
    return round(move if side is PickSide.LONG else -move, DIGITS)


def _positive(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) and value > 0 else None


def _llm_score(pick: Pick) -> int | None:
    value = pick.features.get("llm_score")
    if value is None or not 0 <= value <= 100:
        return None
    return round(value)


def score_pick(
    pick: Pick,
    *,
    pick_day: date,
    run_status: RunStatus,
    bars: Sequence[DailyBar],
    today: date,
    traded: bool | None,
    now: datetime,
) -> Scored:
    """One pick's outcome as of ``today``. ``bars`` are the symbol's daily bars; any
    outside the pick day to today are ignored."""
    series = sorted((b for b in bars if pick_day <= b.day <= today), key=lambda b: b.day)
    old = weekdays_between(pick_day, today) >= FINAL_AFTER_WEEKDAYS
    fields: dict[str, Any] = {
        "run_id": pick.run_id,
        "rank": pick.rank,
        "symbol": pick.symbol,
        "side": pick.side,
        "horizon": pick.horizon,
        "score": pick.score,
        "pre_score": pick.pre_score,
        "llm_score": _llm_score(pick),
        "pick_day": pick_day,
        "run_status": run_status,
        "price_at_pick": _positive(pick.features.get("price_at_pick")),
        "traded": traded,
        "updated_at": now,
    }
    if not series or series[0].day != pick_day:
        # No entry price: nothing can be measured. A pick that old never will be.
        fields["status"] = OutcomeStatus.FINAL if old else OutcomeStatus.PENDING
        return Scored(PickOutcome.model_validate(fields))
    side = pick.side
    entry = series[0].open
    matured: set[str] = set()
    for h in HORIZONS:
        name = f"ret_{h}d"
        if len(series) >= h:
            fields[name] = signed_pct(entry, series[h - 1].close, side)
            if series[h - 1].day == today:
                matured.add(name)
    if pick.horizon is Horizon.INTRADAY:
        fields["ret_0d"] = signed_pct(entry, series[0].close, side)
    expiry = trading_date(pick.expires_at)
    window = [b for b in series if b.day <= expiry]
    moves = [signed_pct(entry, price, side) for b in window for price in (b.high, b.low)]
    invalidation = float(pick.invalidation)
    if side is PickSide.LONG:
        hit = any(b.low <= invalidation for b in window)
    else:
        hit = any(b.high >= invalidation for b in window)
    closed = window[-1].day == expiry or series[-1].day > expiry
    expired = signed_pct(entry, window[-1].close, side) if closed else None
    done = "ret_20d" in fields and expired is not None
    fields |= {
        "entry": entry,
        "mfe_pct": max(moves),
        "mae_pct": min(moves),
        "hit_invalidation": hit,
        "expired_return": expired,
        "status": OutcomeStatus.FINAL if done or old else OutcomeStatus.PARTIAL,
    }
    return Scored(PickOutcome.model_validate(fields), frozenset(matured))


def _bought(event: Mapping[str, Any], symbol: str, start: datetime, end: datetime) -> bool:
    """A buy of ``symbol`` (shares, or an option on it) submitted between start and end."""
    if event.get("kind") != "order_submitted":
        return False
    data = event.get("data")
    if not isinstance(data, Mapping) or data.get("side") != "BUY":
        return False
    try:
        at = datetime.fromisoformat(str(event.get("at")))
        bought = root_symbol(str(data.get("symbol")))
    except ValueError:
        return False
    return at.tzinfo is not None and bought == symbol and start <= at <= end


def traded_from_logs(
    logs: Mapping[date, Sequence[Mapping[str, Any]] | None],
    symbol: str,
    start: datetime,
    end: datetime,
) -> bool | None:
    """Whether the bot submitted a buy in ``symbol`` while the pick was live, from its
    event log, one list per trading day. A day missing from ``logs`` or read as None was
    unreadable: then the answer is None unless a buy was found on another day."""
    unknown = False
    for day in weekdays_from(trading_date(start), trading_date(end)):
        events = logs.get(day)
        if events is None:
            unknown = True
            continue
        if any(_bought(event, symbol, start, end) for event in events):
            return True
    return None if unknown else False


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), DIGITS) if values else None


def _horizon_stats(scored: Sequence[Scored], name: str) -> HorizonStats:
    values = [
        value
        for s in scored
        if name in s.matured and (value := getattr(s.outcome, name)) is not None
    ]
    return HorizonStats(
        matured=len(values), hits=sum(1 for v in values if v > 0), mean_pct=_mean(values)
    )


def summarize(
    scored: Sequence[Scored],
    *,
    day: date,
    run_id: str,
    kinds: Mapping[str, str],
    now: datetime,
) -> ScoreSummary:
    """The day's summary over every pick in the window. ``kinds`` maps a run id to its
    kind (premarket, intraday, ...)."""
    by_kind: dict[str, int] = {}
    by_side: dict[str, int] = {}
    for s in scored:
        kind = kinds.get(s.outcome.run_id, "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_side[s.outcome.side.value] = by_side.get(s.outcome.side.value, 0) + 1
    buckets = []
    for low, high in BUCKETS:
        members = [s.outcome for s in scored if low <= s.outcome.score <= high]
        known = [o.ret_1d for o in members if o.ret_1d is not None]
        buckets.append(
            ScoreBucket(low=low, high=high, count=len(members), mean_ret_1d_pct=_mean(known))
        )
    return ScoreSummary(
        day=day,
        run_id=run_id,
        picks=len(scored),
        by_kind=dict(sorted(by_kind.items())),
        by_side=dict(sorted(by_side.items())),
        ret_1d=_horizon_stats(scored, "ret_1d"),
        ret_5d=_horizon_stats(scored, "ret_5d"),
        buckets=tuple(buckets),
        updated_at=now,
    )


def _stats_text(stats: HorizonStats, horizon: str) -> str:
    if not stats.matured or stats.mean_pct is None:
        return f"no picks matured {horizon}"
    return (
        f"{stats.matured} picks matured {horizon}, hit {stats.hits}/{stats.matured}, "
        f"mean {stats.mean_pct:+.1f}%"
    )


def summary_text(summary: ScoreSummary) -> str:
    """The alert: short and plain. Counts and numbers only, no symbols or model text."""
    return (
        f"traider scorecard {summary.day.isoformat()}: {_stats_text(summary.ret_1d, '1d')}; "
        f"{_stats_text(summary.ret_5d, '5d')}; {summary.picks} picks in the window"
    )
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `uv run pytest tests/unit/test_research_scorecard.py -q`
Expected: PASS (27 passed).

- [ ] **Step 5: Break on purpose (restore after each)**

Make each change, run the named tests, see them FAIL, then undo the change exactly.

- **Break 1.** **Spec break:** ignore the side when signing returns. In `src/traider/research/scorecard.py`, replace:

  ```python
      return round(move if side is PickSide.LONG else -move, DIGITS)
  ```

  with:

  ```python
      return round(move, DIGITS)
  ```

  Run: `uv run pytest tests/unit/test_research_scorecard.py -q`. These FAIL: `test_a_bearish_pick_is_signed_the_other_way`, `test_signed_pct_rounds_to_four_places`, `test_the_summary_counts_hits_means_and_buckets` (and, once Task 5 is in, `test_research_scorecard_run.py::test_a_week_of_picks_is_scored`).

- **Break 2.** Call a pick final on its 20-day close alone. In `src/traider/research/scorecard.py`, replace:

  ```python
      done = "ret_20d" in fields and expired is not None
  ```

  with:

  ```python
      done = "ret_20d" in fields
  ```

  Run: `uv run pytest tests/unit/test_research_scorecard.py -q`. These FAIL: `test_the_twenty_day_close_alone_is_not_final_while_the_pick_is_live`.

- **Break 3.** Count a buy outside the pick's live window. In `src/traider/research/scorecard.py`, replace:

  ```python
      return at.tzinfo is not None and bought == symbol and start <= at <= end
  ```

  with:

  ```python
      return at.tzinfo is not None and bought == symbol
  ```

  Run: `uv run pytest tests/unit/test_research_scorecard.py -q`. These FAIL: `test_anything_else_does_not[event4]` (a buy before the pick).

- [ ] **Step 6: Whole suite, lint, types**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: all green: 2148 passed, 6 skipped.

- [ ] **Step 7: Commit**

```bash
git add src/traider/research/scorecard.py tests/unit/test_research_scorecard.py
git commit -m "feat(research): scorecard maths

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Xh7zur22CRYEGaDoTB5nkV"
```

---

### Task 4: Share the run scaffolding between kinds (refactor; the riskiest task)

The pre-market run must behave byte-for-byte as before, and every existing test in `tests/unit/test_research_run.py` and `tests/unit/test_cli_research_run.py` must pass **unchanged**. Two things make that checkable:

1. The existing tests reach into `run.py`: they patch `run_module._run_deadline`, `run_module._dive_deadline`, `run_module.RUN_BOX_MARGIN_S` and `run_module.run_dive`, build `run_module._Run(deps, NOW, "premarket-x", dry_run=False)` and read its `.box`, use `run_module.LOCK_SPARE_S`, and check that the source of `run.py` never says "broker". So the scaffolding **stays in `run.py`** (a new module would not see those patches), `_Run` keeps its name and constructor, and those module names keep their meaning.
2. A fingerprint script records everything the pre-market run writes, sends and returns over eight paths, before and after. The two outputs must be identical.

**What moves where.** `RunBase` gets `__init__` (now computing `self.max_run_s = self.time_limit()`), `time_limit()`, `models()`, `note`, `meta` (kind and models from the class), `put_trail`, `execute` (the box message uses `self.max_run_s`), `done_today()` (skip-if-done, from the old inline code), `begin(run_usd)` (meter, trail, running META, from the old inline code), `_budget_notes`, `commit(outcome, write, message, *, event=None)` (cost first, then `write`, then the alert, from the old `finish`), `fail`, `_settle_cost`, `alert(status, message, *, event=None)` (subject from `title`). `run_locked(run, *, force)` holds the lock dance from the old `run_premarket`. `_Run(RunBase)` keeps the calendar window, `models()`, `_execute` and every stage, and its `finish` calls `commit`. `_summary` takes the kind. The soft deadline in `_execute` and `dive` reads `self.max_run_s` (equal to `research_jobs.max_run_s` for the pre-market kind).

**Files:**
- Modify: `src/traider/research/run.py` (whole file shown)
- Test: `tests/unit/test_research_run_base.py` (new); `tests/unit/test_research_run.py` and `tests/unit/test_cli_research_run.py` unchanged

**Interfaces:**
- Consumes: nothing new.
- Produces (`traider.research.run`):
  - `new_run_id(now: datetime, kind: str = KIND) -> str`.
  - `class RunBase`: class attributes `kind: ClassVar[RunKind]`, `lock_name: ClassVar[str]`, `title: ClassVar[str]`; `__init__(deps: RunDeps, now: datetime, run_id: str, *, dry_run: bool)`; attributes `deps, now, today, run_id, dry_run, jobs, started, max_run_s, hard_stop, box, stage, notes, partial, counts, meter, trail, written`; methods `time_limit() -> float` (default `research_jobs.max_run_s`), `models() -> tuple[str, ...]` (default `()`), `note(text, *, partial=False)`, `meta(status, *, error="", finished=True) -> RunMeta`, `async put_trail(name, data)`, `async execute(*, force) -> RunOutcome`, `async _execute(*, force) -> RunOutcome` (abstract), `async done_today() -> RunMeta | None`, `async begin(run_usd: Decimal) -> None`, `_budget_notes()`, `async commit(outcome: RunOutcome, write: Callable[[], Awaitable[None]], message: str, *, event: str | None = None) -> RunOutcome`, `async fail(exc) -> RunOutcome`, `async alert(status, message, *, event=None)`.
  - `async run_locked(run: RunBase, *, force: bool) -> RunOutcome`: lock `run.lock_name` for `run.max_run_s + LOCK_SPARE_S`; a dry run takes no lock and runs forced.
  - `class _Run(RunBase)` (pre-market): `kind = "premarket"`, `lock_name = "premarket"`, `title = "Research"`; constructor unchanged.
  - `run_premarket(deps, now, *, dry_run=False, force=False)`: signature and behaviour unchanged.

- [ ] **Step 1: Write the failing tests**

These try the scaffolding on a small made-up kind, `Probe`.

Create `tests/unit/test_research_run_base.py`:

```python
"""The scaffolding every research run kind shares (``RunBase`` and ``run_locked``), tried
on a small made-up kind. The pre-market run's own tests prove it still behaves as before."""

import asyncio
import json
from datetime import date
from decimal import Decimal
from typing import ClassVar

import pytest

import traider.research.run as run_module
from tests.fakes.research import NOW, TODAY, golden_llm, market_day
from tests.unit.test_research_run import Trails
from traider.alerts import LogAlerter
from traider.research.models import RunKind, RunMeta, RunStatus
from traider.research.run import (
    EXIT_FAILED,
    EXIT_LOCKED,
    EXIT_OK,
    LOCK_SPARE_S,
    RUN_BOX_MARGIN_S,
    RunBase,
    RunDeps,
    RunOutcome,
    run_locked,
)
from traider.research.store import MemoryResearchStore
from traider.settings import Settings
from traider.timeutil import ManualClock

DAY = TODAY.isoformat()


class RecordingStore(MemoryResearchStore):
    def __init__(self) -> None:
        super().__init__()
        self.order: list[str] = []

    async def add_day_cost(self, day, usd):
        self.order.append(f"cost {usd}")
        return await super().add_day_cost(day, usd)

    async def put_meta(self, meta):
        self.order.append(f"meta {meta.status.value}")
        await super().put_meta(meta)


class Probe(RunBase):
    """A made-up kind: spends a cent, then writes its META."""

    kind: ClassVar[RunKind] = "manual"
    lock_name: ClassVar[str] = "probe"
    title: ClassVar[str] = "Probe"
    seen_force: bool | None = None
    explode: Exception | None = None

    def time_limit(self) -> float:
        return 100.0

    def models(self) -> tuple[str, ...]:
        return ("model-a",)

    async def _execute(self, *, force: bool) -> RunOutcome:
        self.seen_force = force
        await self.begin(Decimal("0.50"))
        self.stage = "work"
        if self.explode is not None:
            raise self.explode
        self.note("one note")
        meta = self.meta(RunStatus.OK)
        outcome = RunOutcome("ok", EXIT_OK, self.run_id, meta=meta)

        async def write() -> None:
            self.deps.store.order.append("write")
            await self.deps.store.put_meta(meta)

        return await self.commit(outcome, write, "probe done", event="probe_done")


def deps(store=None) -> RunDeps:
    market, events = market_day()
    return RunDeps(
        store=store or RecordingStore(),
        market=market,
        events=events,
        llm=golden_llm(),
        trail=Trails(),
        alerts=LogAlerter(),
        settings=Settings(),
        clock=ManualClock(NOW),
        monotonic=lambda: 0.0,
    )


def probe(d: RunDeps, *, dry_run: bool = False) -> Probe:
    return Probe(d, NOW, "manual-x", dry_run=dry_run)


async def stored_meta(store, run_id="manual-x") -> RunMeta:
    return RunMeta.model_validate(json.loads(store.raw(f"RUN#{run_id}", "META")["body"]))


async def test_a_kind_runs_under_its_own_lock_and_releases_it():
    d = deps()
    taken = []
    real = d.store.acquire_lock

    async def spy(name, owner, ttl_s, now):
        taken.append((name, owner, ttl_s, now))
        return await real(name, owner, ttl_s, now)

    d.store.acquire_lock = spy
    outcome = await run_locked(probe(d), force=False)
    assert (outcome.status, outcome.exit_code) == ("ok", EXIT_OK)
    assert taken == [("probe", "manual-x", 100.0 + LOCK_SPARE_S, NOW)]
    assert ("LOCK#probe", "LOCK") not in d.store.keys


async def test_the_cost_goes_in_before_the_write_and_the_alert_comes_last():
    d = deps()
    run = probe(d)
    await run_locked(run, force=True)
    assert d.store.order == ["meta running", "cost 0.0000", "write", "meta ok"]
    assert d.alerts.sent == [("probe_done", f"Probe {DAY}: ok", "probe done")]
    assert run.seen_force is True
    meta = await stored_meta(d.store)
    assert (meta.kind, meta.models, meta.notes) == ("manual", ("model-a",), ("one note",))


async def test_a_held_lock_exits_2_and_writes_nothing():
    d = deps()
    await d.store.acquire_lock("probe", "someone-else", 3600, NOW)
    outcome = await run_locked(probe(d), force=False)
    assert (outcome.status, outcome.exit_code) == ("locked", EXIT_LOCKED)
    assert d.store.order == [] and d.alerts.sent == []


async def test_another_kinds_lock_does_not_stop_it():
    d = deps()
    await d.store.acquire_lock("premarket", "someone-else", 3600, NOW)
    assert (await run_locked(probe(d), force=False)).status == "ok"


async def test_a_failure_records_meta_failed_alerts_with_the_kind_and_frees_the_lock():
    d = deps()
    run = probe(d)
    run.explode = RuntimeError("boom with token d1c2b3a4e5f6a7b8c9d0e1f2")
    outcome = await run_locked(run, force=False)
    assert (outcome.status, outcome.exit_code) == ("failed", EXIT_FAILED)
    meta = await stored_meta(d.store)
    assert meta.status is RunStatus.FAILED
    assert meta.error.startswith("work: RuntimeError: boom")
    assert "d1c2b3a4e5f6a7b8c9d0e1f2" not in meta.error
    ((event, subject, message),) = d.alerts.sent
    assert (event, subject) == ("research_run_failed", f"Probe {DAY}: failed")
    assert message.startswith(f"traider research manual {DAY} failed: work: RuntimeError")
    assert ("LOCK#probe", "LOCK") not in d.store.keys


async def test_a_dry_run_takes_no_lock_writes_nothing_and_runs_forced():
    d = deps()
    await d.store.acquire_lock("probe", "someone-else", 3600, NOW)
    run = probe(d, dry_run=True)
    outcome = await run_locked(run, force=False)
    assert outcome.status == "ok" and run.seen_force is True
    assert d.store.order == [] and d.alerts.sent == []


async def test_the_time_box_follows_the_kinds_own_limit():
    loop = asyncio.get_running_loop()
    run = probe(deps())
    assert run.max_run_s == 100.0
    box = run.box - loop.time()
    assert 100 + LOCK_SPARE_S - RUN_BOX_MARGIN_S - 1 < box <= 100 + LOCK_SPARE_S - RUN_BOX_MARGIN_S


async def test_a_run_past_its_box_fails_with_its_own_limit_in_the_error(monkeypatch):
    class Slow(Probe):
        async def _execute(self, *, force):
            await self.begin(Decimal("0.50"))
            await asyncio.Event().wait()
            raise AssertionError("never")

    monkeypatch.setattr(
        run_module, "_run_deadline", lambda max_run_s: asyncio.get_running_loop().time() + 0.2
    )
    d = deps()
    slow = Slow(d, NOW, "manual-x", dry_run=False)
    outcome = await asyncio.wait_for(run_locked(slow, force=False), 5)
    assert outcome.status == "failed"
    assert outcome.meta.error == "start: RunDeadline: the run did not finish within 640s"


@pytest.mark.parametrize(
    ("status", "found"),
    [("ok", "manual-b"), ("partial", "manual-b"), ("failed", None), ("running", None)],
)
async def test_done_today_finds_only_a_finished_run_of_this_kind(status, found):
    d = deps()
    for run_id, kind, st in (("manual-b", "manual", status), ("premarket-a", "premarket", "ok")):
        await d.store.put_meta(
            RunMeta(
                run_id=run_id, kind=kind, status=st, started_at=NOW, trading_day=date(2026, 10, 9)
            )
        )
    done = await probe(d).done_today()
    assert (done.run_id if done else None) == found
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_research_run_base.py -q`
Expected: FAIL: collection error (1 error), `ImportError: cannot import name 'RunBase' from 'traider.research.run'`.

- [ ] **Step 3: Fingerprint the pre-market run before touching `run.py`**

Save this script **outside the repository** as `/tmp/premarket_fingerprint.py` (it is a check, not part of the code):

```text
"""Run the pre-market run on the C1 fakes through several paths and print everything it
wrote, sent and returned, as JSON. Run before and after the refactor; the two outputs
must be identical. Usage: uv run python /tmp/premarket_fingerprint.py > out.json"""

import asyncio
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from tests.fakes.research import NOW, golden_llm, market_day  # noqa: E402
from tests.unit.test_research_run import Trails  # noqa: E402
from traider.alerts import LogAlerter  # noqa: E402
from traider.research.run import RunDeps, run_premarket  # noqa: E402
from traider.research.store import MemoryResearchStore  # noqa: E402
from traider.research.trail import to_json  # noqa: E402
from traider.schwab.tokens import AuthUnavailable  # noqa: E402
from traider.settings import Settings  # noqa: E402
from traider.timeutil import ManualClock  # noqa: E402

secrets.token_hex = lambda n: "abcd"  # a fixed run id


def deps(**changes):
    market, events = market_day()
    d = RunDeps(
        store=MemoryResearchStore(),
        market=market,
        events=events,
        llm=golden_llm(),
        trail=Trails(),
        alerts=LogAlerter(),
        settings=Settings(),
        clock=ManualClock(NOW),
        monotonic=lambda: 0.0,
    )
    for name, value in changes.items():
        setattr(d, name, value)
    return d


async def scenario(name, d, **kwargs):
    outcome = await run_premarket(d, NOW, **kwargs)
    items = {f"{pk}|{sk}": d.store.raw(pk, sk) for pk, sk in sorted(d.store.keys)}
    trails = {p: json.loads(to_json(t.files)) for p, t in d.trail.made.items()}
    return {
        "name": name,
        "outcome": {
            "status": outcome.status,
            "exit_code": outcome.exit_code,
            "report": outcome.report(),
        },
        "items": json.loads(json.dumps(items, default=str)),
        "alerts": d.alerts.sent,
        "trails": trails,
    }


async def main():
    out = []
    out.append(await scenario("golden", deps()))
    calm = deps()
    calm.market.quote_map["$VIX"] = calm.market.quote_map["$VIX"].model_copy(update={"last": 40.0})
    out.append(await scenario("stand_aside", calm))
    down = deps()
    down.events.fail_everything()
    out.append(await scenario("finnhub_down", down))
    expired = deps()
    expired.market.failures["quotes"] = AuthUnavailable("sign in again: refresh token expired")
    out.append(await scenario("expired_sign_in", expired))
    held = deps()
    await held.store.acquire_lock("premarket", "someone", 3600, NOW)
    out.append(await scenario("locked", held))
    done = deps()
    await run_premarket(done, NOW)
    out.append(await scenario("skipped", done))
    out.append(await scenario("dry_run", deps(), dry_run=True))
    out.append(await scenario("disabled", deps(settings=Settings(research_jobs={"enabled": False}))))
    print(json.dumps(out, indent=1, sort_keys=True, default=str))


asyncio.run(main())
```

Run: `uv run python /tmp/premarket_fingerprint.py > /tmp/premarket-before.json 2>/dev/null && wc -c /tmp/premarket-before.json`
Expected: exit 0 and a file of about 335,000 bytes (334,822 in the scratch copy). Keep it until Task 6 is done.


- [ ] **Step 4: Replace `run.py`**

The whole new file follows. Compared with the old one: the imports gain `Awaitable`, `ClassVar` and `RunKind`; `new_run_id` takes the kind; `run_premarket` delegates to `run_locked`; `RunBase` is the old `_Run`'s shared half plus `done_today`, `begin` and `commit`; `_Run` is the rest; `_summary` takes the kind. Nothing else changed, line for line.

Replace the whole of `src/traider/research/run.py` with:

```python
"""The pre-market research run, start to finish.

    lock -> market open today? -> already done today? -> META running
      -> collect (Schwab + events) -> posture (code rules, then a model review; stricter wins)
      -> stand_aside? yes -> write the posture, no picks, ok
      -> screen (filters, stale history, features, pre_score, top K)
      -> deep-dives (tool loops, budgets, concurrency, deadline)
      -> earnings check (a symbol-scoped calendar call per swing idea)
      -> rank + validate -> add the cost -> write picks + posture + META ok|partial -> alert
    any exception -> META failed, alert, exit 1 (no posture, so the bot stands aside)

Exit codes: 0 ok, partial, skipped, closed or disabled; 1 failed; 2 the lock is held.
A run is ``partial`` when planned work did not happen: a budget stopped calls, the
deadline passed, the events vendor failed (an empty earnings calendar over a week or
more counts as failed), the model review failed, model calls failed in
half or more of the deep-dives, or a trail file could not be written. The bot
ignores partial runs by default.

Time: the lock lives ``max_run_s + LOCK_SPARE_S`` from the start. The whole run is boxed
to end ``RUN_BOX_MARGIN_S`` before that; hitting the box fails the run (no posture, the
cost recorded, the lock released). The soft deadline is ``max_run_s``: past it before the
screen, the posture is written with no picks; past it during the dives, no new dive starts
(both checked on ``RunDeps.monotonic``) and a hard stop on the event loop's clock cancels
dives still running. A cancelled model call's reservation is charged in full. Ranking gets
whatever time is left in the box.

Every text that reaches META or an alert (errors, notes) goes through ``scrub`` first:
vendor errors and model-written text can carry secrets or injected words.

``RunBase`` holds what every run kind shares: the lock (``run_locked``), META, the trail,
the cost meter and the day's cost, alerts, failure handling, the time box and the exit
codes. ``_Run`` is the pre-market kind; the scorecard and the intraday runs are kinds in
their own modules.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import traceback
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict

from traider.alerts import Alerter
from traider.research.cost import CostMeter
from traider.research.dive import DiveContext, DiveResult, run_dive
from traider.research.events import (
    EarningsEvent,
    EventsData,
    EventsUnavailable,
    NewsItem,
    Profile,
)
from traider.research.llm import LLM
from traider.research.market import DailyBar, MarketData, MarketQuote, QuoteBatch
from traider.research.models import Pick, Posture, PostureLevel, RunKind, RunMeta, RunStatus
from traider.research.posture import (
    REASON_MAX_CHARS,
    PostureDecision,
    decide_posture,
    posture_metrics,
)
from traider.research.rank import (
    RankInput,
    RankResult,
    Rejection,
    blended_score,
    rank_and_validate,
    share_class,
)
from traider.research.screen import (
    DROP_HISTORY,
    DROP_HISTORY_ERROR,
    MIN_BARS,
    ScreenRow,
    build_candidates,
    compute_features,
    drop_counts,
    earnings_candidates,
    earnings_near,
    history_filter,
    quote_filter,
    score_rows,
    top_k,
    with_news,
)
from traider.research.scrub import scrub
from traider.research.store import ResearchWriter
from traider.research.trail import Trail, trail_prefix
from traider.schwab.client import SchwabError
from traider.schwab.parse import ParseError
from traider.settings import Settings
from traider.timeutil import (
    Clock,
    SystemClock,
    previous_weekday,
    trading_date,
    weekdays_after,
    weekdays_between,
)

log = logging.getLogger(__name__)

KIND: Final = "premarket"
LOCK_NAME = "premarket"
LOCK_SPARE_S = 600  # the lock outlives the deadline by this much
RUN_BOX_MARGIN_S = 60  # the whole run ends this long before the lock expires
HISTORY_DAYS = 260
NEWS_DAYS = 3
BARS_CONCURRENCY = 8
SECTOR_ETFS = ("XLK", "XLF", "XLV", "XLE", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC")
CONTEXT_SYMBOLS = ("$VIX", "SPY", "QQQ", "IWM", *SECTOR_ETFS)
MOVER_INDEXES = ("EQUITY_ALL", "NYSE", "NASDAQ")
MOVER_SORTS = ("PERCENT_CHANGE_UP", "PERCENT_CHANGE_DOWN", "VOLUME")
MAX_NOTES = 20
# A name whose last daily bar is more than this many weekdays before today is dropped:
# its features would describe an old market.
MAX_BAR_AGE_WEEKDAYS = 3
DROP_STALE_HISTORY = "stale_history"
# A market-wide earnings calendar with no rows over at least this many weekdays is taken
# as a vendor problem, not a quiet week: it counts as unavailable.
EMPTY_CALENDAR_WEEKDAYS = 5

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_LOCKED = 2


class Snapshot(BaseModel):
    """What the run saw before deciding anything. Stored as ``snapshot.json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    taken_at: datetime
    day: date
    context: dict[str, MarketQuote]
    spy_bars: list[DailyBar]
    movers: dict[str, list[str]]
    earnings_ok: bool
    earnings: list[EarningsEvent]
    news_ok: bool
    market_news: list[NewsItem]
    watchlist: list[str]
    candidates: list[str]
    candidate_sources: dict[str, list[str]]


@dataclass
class RunDeps:
    store: ResearchWriter
    market: MarketData
    events: EventsData
    llm: LLM
    trail: Callable[[str], Trail]  # from the run's prefix
    alerts: Alerter
    settings: Settings  # read once, when the run starts
    clock: Clock = field(default_factory=SystemClock)
    monotonic: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class RunOutcome:
    status: str  # ok, partial, failed, skipped, closed, disabled, locked
    exit_code: int
    run_id: str
    meta: RunMeta | None = None
    posture: Posture | None = None
    picks: tuple[Pick, ...] = ()
    rejected: tuple[Rejection, ...] = ()
    detail: str = ""

    def report(self) -> dict[str, Any]:
        """For printing: what the run decided, as JSON-ready data."""
        return {
            "status": self.status,
            "run_id": self.run_id,
            "detail": self.detail,
            "posture": self.posture.model_dump(mode="json") if self.posture else None,
            "picks": [p.model_dump(mode="json") for p in self.picks],
            "rejected": {r.symbol: r.reason for r in self.rejected},
            "cost_usd": str(self.meta.cost_usd) if self.meta else "0",
            "notes": list(self.meta.notes) if self.meta else [],
            "counts": dict(self.meta.counts) if self.meta else {},
        }


def new_run_id(now: datetime, kind: str = KIND) -> str:
    return f"{kind}-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"


def _describe(exc: BaseException) -> str:
    """An exception as text that is safe for logs, META and alerts."""
    return scrub(f"{type(exc).__name__}: {exc}")


class RunDeadline(Exception):
    """The whole run did not finish inside its time box."""


def _dive_deadline(max_run_s: float) -> float:
    """The hard stop for running deep-dives, on the event loop's clock."""
    return asyncio.get_running_loop().time() + max_run_s


def _run_deadline(max_run_s: float) -> float:
    """The time box for the whole run, on the event loop's clock: it ends a minute before
    the lock expires, so the failure can be recorded while the lock is still held."""
    return asyncio.get_running_loop().time() + max_run_s + LOCK_SPARE_S - RUN_BOX_MARGIN_S


async def run_premarket(
    deps: RunDeps, now: datetime, *, dry_run: bool = False, force: bool = False
) -> RunOutcome:
    """One pre-market run. ``dry_run`` makes every call but writes nothing to the research
    table (no lock, META, picks or cost) and sends no alert. ``force`` ignores an earlier
    ok or partial run today; it never ignores the lock."""
    now = now.astimezone(UTC)
    run_id = new_run_id(now)
    jobs = deps.settings.research_jobs
    if not jobs.enabled:
        log.info("research jobs are switched off (research_jobs.enabled); nothing to do")
        return RunOutcome("disabled", EXIT_OK, run_id, detail="research_jobs.enabled is false")
    return await run_locked(_Run(deps, now, run_id, dry_run=dry_run), force=force)


async def run_locked(run: RunBase, *, force: bool) -> RunOutcome:
    """Run under the kind's lock, which lives ``max_run_s + LOCK_SPARE_S`` from the start
    and is released on every path (it expires by itself if the release fails). A held lock
    exits 2 and touches nothing. A dry run takes no lock and runs with ``force``."""
    if run.dry_run:
        return await run.execute(force=True)
    store = run.deps.store
    try:
        acquired = await store.acquire_lock(
            run.lock_name, run.run_id, run.max_run_s + LOCK_SPARE_S, run.now
        )
    except Exception as exc:
        return await run.fail(exc)
    if not acquired:
        log.warning("another %s run holds the lock; exiting", run.kind)
        return RunOutcome("locked", EXIT_LOCKED, run.run_id, detail="another run holds the lock")
    try:
        return await run.execute(force=force)
    finally:
        try:
            await store.release_lock(run.lock_name, run.run_id)
        except Exception as exc:
            log.error("could not release the research lock (it expires by itself): %s",
                      _describe(exc))  # fmt: skip


class RunBase:
    """What every run kind shares: META, the trail, the cost meter and the day's cost,
    alerts, failure handling and the time box. A kind sets ``kind``, ``lock_name`` and
    ``title``, may change ``time_limit`` and ``models``, and implements ``_execute``."""

    kind: ClassVar[RunKind]
    lock_name: ClassVar[str]
    title: ClassVar[str]  # how each alert's subject starts

    def __init__(self, deps: RunDeps, now: datetime, run_id: str, *, dry_run: bool) -> None:
        self.deps = deps
        self.now = now
        self.today = trading_date(now)
        self.run_id = run_id
        self.dry_run = dry_run
        self.jobs = deps.settings.research_jobs
        self.started = deps.monotonic()
        self.max_run_s = self.time_limit()
        # Both from the same start as the lock's lifetime (the lock is taken right after).
        self.hard_stop = _dive_deadline(self.max_run_s)
        self.box = _run_deadline(self.max_run_s)
        self.stage = "start"
        self.notes: list[str] = []
        self.partial = False
        self.budget_noted = False
        self.counts: dict[str, int] = {}
        self.meter: CostMeter | None = None
        self.trail: Trail | None = None
        self.trail_failures = 0
        self.cost_added = False  # set only once add_day_cost has returned
        self.cost_task: asyncio.Task[Decimal] | None = None
        # Set once write_run has returned: META, picks and posture are in the table and a
        # later failure must not overwrite them.
        self.written: RunOutcome | None = None

    def time_limit(self) -> float:
        """The soft deadline, ``max_run_s``. The lock and the time box follow from it."""
        return self.jobs.max_run_s

    def models(self) -> tuple[str, ...]:
        """The models this kind may call, for META."""
        return ()

    # ------------------------------------------------------------------ helpers

    def note(self, text: str, *, partial: bool = False) -> None:
        clean = scrub(text)  # at most 300 characters, as RunMeta.notes requires
        log.warning("research %s: %s", self.run_id, clean)
        if len(self.notes) < MAX_NOTES:
            self.notes.append(clean)
        self.partial = self.partial or partial

    def meta(self, status: RunStatus, *, error: str = "", finished: bool = True) -> RunMeta:
        meter = self.meter
        return RunMeta(
            run_id=self.run_id,
            kind=self.kind,
            status=status,
            started_at=self.now,
            finished_at=self.deps.clock.now() if finished else None,
            trading_day=self.today,
            models=self.models(),
            cost_usd=meter.spent_usd if meter else Decimal(0),
            # The key prefix only: a bucket name carries the account id.
            s3_prefix=trail_prefix(self.today, self.run_id) if self.trail else "",
            error=error,
            tokens_in=meter.tokens_in if meter else 0,
            tokens_out=meter.tokens_out if meter else 0,
            notes=tuple(self.notes),
            counts=dict(self.counts),
        )

    async def put_trail(self, name: str, data: Any) -> None:
        """A failed trail write does not stop the run, but the audit trail is incomplete,
        so the run is partial. The first failure is noted; all are counted."""
        assert self.trail is not None
        try:
            await self.trail.put(name, data)
        except Exception as exc:
            self.trail_failures += 1
            self.counts["trail_failures"] = self.trail_failures
            if self.trail_failures == 1:
                self.note(f"trail write failed ({name}): {_describe(exc)}", partial=True)
            else:
                log.warning("trail write failed (%s): %s", name, _describe(exc))

    # --------------------------------------------------------------------- flow

    async def execute(self, *, force: bool) -> RunOutcome:
        box = asyncio.timeout_at(self.box)
        try:
            async with box:
                return await self._execute(force=force)
        except TimeoutError as exc:
            if not box.expired():
                return await self.fail(exc)
            limit = self.max_run_s + LOCK_SPARE_S - RUN_BOX_MARGIN_S
            return await self.fail(RunDeadline(f"the run did not finish within {limit:.0f}s"))
        except Exception as exc:
            return await self.fail(exc)

    async def _execute(self, *, force: bool) -> RunOutcome:
        raise NotImplementedError

    async def done_today(self) -> RunMeta | None:
        """The latest ok or partial run of this kind today, for skip-if-done."""
        self.stage = "skip_check"
        done = [
            m
            for m in await self.deps.store.runs_for_day(self.today.isoformat(), self.kind)
            if m.status in (RunStatus.OK, RunStatus.PARTIAL)
        ]
        return done[-1] if done else None

    async def begin(self, run_usd: Decimal) -> None:
        """Start the cost meter (held to ``run_usd`` and to what is left of the day's
        budget) and the trail, and write the running META (not on a dry run)."""
        self.stage = "start"
        budget = self.jobs.budget
        spent_today = await self.deps.store.day_cost(self.today.isoformat())
        self.meter = CostMeter(
            budget.prices, run_usd=run_usd, day_remaining_usd=budget.day_usd - spent_today
        )
        self.trail = self.deps.trail(trail_prefix(self.today, self.run_id))
        if not self.dry_run:
            await self.deps.store.put_meta(self.meta(RunStatus.RUNNING, finished=False))

    # ------------------------------------------------------------------- finish

    def _budget_notes(self) -> None:
        """Any budget stop makes the run partial, however it showed up."""
        assert self.meter is not None
        if self.meter.overrun:
            self.note("budget: a model call cost more than its reservation", partial=True)
        if self.meter.exhausted and not self.budget_noted:
            self.note("budget reached: the cost meter stopped further calls", partial=True)
            self.budget_noted = True

    async def commit(
        self,
        outcome: RunOutcome,
        write: Callable[[], Awaitable[None]],
        message: str,
        *,
        event: str | None = None,
    ) -> RunOutcome:
        """Record a finished run: the day's cost first, then ``write`` (which writes META
        last), then the alert. A dry run does none of it."""
        assert self.meter is not None
        assert outcome.meta is not None
        if not self.dry_run:
            # The cost first: a write that fails afterwards must not hide what was spent.
            # Shielded, so the time box cannot cut the add off halfway; fail() waits for
            # one still under way. Never under-counted: an add that raised is tried again.
            self.cost_task = asyncio.ensure_future(
                self.deps.store.add_day_cost(self.today.isoformat(), self.meter.spent_usd)
            )
            await asyncio.shield(self.cost_task)
            self.cost_added = True
            await write()
            self.written = outcome
            await self.alert(outcome.meta.status, message, event=event)
        return outcome

    async def fail(self, exc: BaseException) -> RunOutcome:
        leaves = _leaves(exc)
        cause = leaves[0]
        error = scrub(f"{self.stage}: {type(cause).__name__}: {cause}")
        # Every failure is logged, scrubbed; the first goes to META. Frames only: the
        # exception's own text may carry vendor words.
        for leaf in leaves:
            frames = "".join(traceback.format_tb(leaf.__traceback__))
            log.error("research run %s failed in %s: %s\n%s", self.run_id, self.stage,
                      _describe(leaf), frames)  # fmt: skip
        if self.written is not None:
            # The result is in the table: META, picks and posture stand as written.
            written = self.written
            log.error("research run %s failed after its result was written as %s; "
                      "it stands", self.run_id, written.status)  # fmt: skip
            await self.alert(
                RunStatus.FAILED,
                f"traider research {self.kind} {self.today.isoformat()}: the run was written as "
                f"{written.status}, then failed: {error}. The written result stands.",
            )
            return replace(written, detail=error)
        meta = self.meta(RunStatus.FAILED, error=error)
        if not self.dry_run:
            await self._settle_cost()
            if self.meter is not None and not self.cost_added and self.meter.spent > 0:
                try:
                    await self.deps.store.add_day_cost(self.today.isoformat(), self.meter.spent_usd)
                except Exception as add_exc:
                    log.error("could not add the failed run's cost to the day: %s",
                              _describe(add_exc))  # fmt: skip
            try:
                await self.deps.store.put_meta(meta)
            except Exception as put_exc:
                log.error("could not record the failed run: %s", _describe(put_exc))
            await self.alert(
                RunStatus.FAILED,
                f"traider research {self.kind} {self.today.isoformat()} failed: {error}",
            )
        return RunOutcome("failed", EXIT_FAILED, self.run_id, meta=meta, detail=error)

    async def _settle_cost(self) -> None:
        """Wait for a cost add the time box interrupted, at most until half the margin
        before the lock expires is gone. If it finished, the cost is in; if it raised or
        is still going, fail() adds it (again): over-counting only makes budgets stricter."""
        task = self.cost_task
        if task is None or self.cost_added:
            return
        if not task.done():
            wait = self.box + RUN_BOX_MARGIN_S / 2 - asyncio.get_running_loop().time()
            await asyncio.wait({task}, timeout=max(wait, 0.0))  # never cancels the add
        if not task.done():
            log.error("the day's cost add did not finish in time; adding it again")
            return
        if task.cancelled():
            return
        if (exc := task.exception()) is not None:
            log.error("could not add the run's cost to the day: %s", _describe(exc))
            return
        self.cost_added = True

    async def alert(self, status: RunStatus, message: str, *, event: str | None = None) -> None:
        subject = f"{self.title} {self.today.isoformat()}: {status.value}"
        try:
            await self.deps.alerts.send(event or f"research_run_{status.value}", subject, message)
        except Exception as exc:
            log.error("could not send the research alert: %s", _describe(exc))


class _Run(RunBase):
    """The pre-market run."""

    kind: ClassVar[RunKind] = KIND
    lock_name: ClassVar[str] = LOCK_NAME
    title: ClassVar[str] = "Research"

    def __init__(self, deps: RunDeps, now: datetime, run_id: str, *, dry_run: bool) -> None:
        super().__init__(deps, now, run_id, dry_run=dry_run)
        # The earnings calendar's window. Nothing is known about earnings after
        # ``calendar_end``, so no swing pick outlives its close.
        self.calendar_start = previous_weekday(self.today)
        self.calendar_end = weekdays_after(self.today, self.jobs.collect.earnings_lookahead_days)

    def models(self) -> tuple[str, ...]:
        dive = self.jobs.dive
        return tuple(dict.fromkeys((dive.posture_model, dive.model)))

    # --------------------------------------------------------------------- flow

    async def _execute(self, *, force: bool) -> RunOutcome:
        deps, day = self.deps, self.today.isoformat()
        self.stage = "market_hours"
        session = await deps.market.market_session(self.today)
        if session.open is None or session.close is None:
            log.info("no regular session on %s; nothing to research", day)
            return RunOutcome("closed", EXIT_OK, self.run_id, detail=f"market closed on {day}")
        if not force:
            done = await self.done_today()
            if done is not None:
                log.info("%s already has a %s run (%s); skipping", day, self.kind, done.run_id)
                return RunOutcome(
                    "skipped", EXIT_OK, self.run_id, detail=f"already done by {done.run_id}"
                )
        await self.begin(self.jobs.budget.run_usd)

        self.stage = "collect"
        snapshot = await self.collect()
        await self.put_trail("snapshot.json", snapshot)

        self.stage = "posture"
        decision = await self.decide(snapshot)
        posture = Posture(
            level=decision.level,
            # Model-written reasons can echo injected news: scrubbed like every other text.
            reasons=tuple(scrub(r, REASON_MAX_CHARS) for r in decision.reasons),
            run_id=self.run_id,
            at=self.now,
            metrics=decision.metrics.as_dict(),
        )
        await self.put_trail(
            "posture.json", {"posture": posture, "notes": [scrub(n) for n in decision.notes]}
        )
        if decision.level is PostureLevel.STAND_ASIDE:
            self.stage = "write"
            return await self.finish(posture, RankResult((), ()), {})
        if deps.monotonic() >= self.started + self.max_run_s:
            self.note("deadline passed before the screen: no deep-dives", partial=True)
            self.stage = "write"
            return await self.finish(posture, RankResult((), ()), {})

        self.stage = "screen"
        top, quotes, bars, profiles = await self.screen(snapshot)

        self.stage = "dive"
        results = await self.dive(
            snapshot, decision=decision, top=top, quotes=quotes, bars=bars, profiles=profiles
        )

        by_symbol = {row.symbol: row for row in top}
        self.stage = "earnings_check"
        earnings, confirmed = await self.confirm_earnings(snapshot, results, by_symbol)

        self.stage = "rank"
        inputs = [
            RankInput(
                symbol=r.symbol,
                assessment=r.assessment,
                pre_score=by_symbol[r.symbol].pre_score,
                features=by_symbol[r.symbol].features.as_dict(),
                atr=by_symbol[r.symbol].atr,
                sector=p.industry if (p := profiles.get(r.symbol)) else None,
                earnings=earnings.get(r.symbol, ()),
                earnings_confirmed=r.symbol in confirmed,
            )
            for r in results
            if r.assessment is not None
        ]
        self.counts["assessed"] = len(inputs)
        assert session.close is not None
        ranked = await rank_and_validate(
            inputs,
            market=deps.market,
            run_id=self.run_id,
            today=self.today,
            close=session.close,
            earnings_ok=snapshot.earnings_ok,
            calendar_end=self.calendar_end,
            settings=self.jobs.rank,
        )
        if ranked.chain_failures:
            self.counts["chain_failures"] = ranked.chain_failures
            self.note(
                f"put chain reads failed for {ranked.chain_failures} name(s); counted as illiquid"
            )
        self.stage = "write"
        assessments = {i.symbol: i.assessment.model_dump(mode="json") for i in inputs}
        return await self.finish(posture, ranked, assessments)

    # ------------------------------------------------------------------ collect

    async def collect(self) -> Snapshot:
        deps, jobs, today = self.deps, self.jobs, self.today
        context = (await deps.market.quotes(CONTEXT_SYMBOLS)).quotes
        spy_bars = await deps.market.daily_bars("SPY", today, HISTORY_DAYS)
        movers: dict[str, list[str]] = {}
        for index in MOVER_INDEXES:
            for sort in MOVER_SORTS:
                movers[f"{index}:{sort}"] = await deps.market.movers(index, sort)
        earnings: list[EarningsEvent] = []
        earnings_ok = True
        try:
            earnings = await deps.events.earnings_calendar(self.calendar_start, self.calendar_end)
        except EventsUnavailable as exc:
            earnings_ok = False
            self.note(f"earnings calendar unavailable, no swing picks: {exc}", partial=True)
        else:
            span = weekdays_between(self.calendar_start, self.calendar_end) + 1
            if not earnings and span >= EMPTY_CALENDAR_WEEKDAYS:
                # No company reporting for a week or more is not believable: an empty
                # reply must not read as "no earnings ahead".
                earnings_ok = False
                self.note(
                    f"earnings calendar returned nothing for {span} weekdays; treated as "
                    "unavailable, no swing picks",
                    partial=True,
                )
        market_news: list[NewsItem] = []
        news_ok = True
        if jobs.collect.market_news_count:
            try:
                market_news = await deps.events.market_news(jobs.collect.market_news_count)
            except EventsUnavailable as exc:
                news_ok = False
                self.note(f"market news unavailable: {exc}", partial=True)
        mover_names = list(dict.fromkeys(name for names in movers.values() for name in names))
        candidates = build_candidates(
            watchlist=jobs.watchlist,
            earnings_names=earnings_candidates(earnings, today),
            movers=mover_names,
            pinned=deps.settings.pinned_symbols,
            cap=jobs.collect.max_candidates,
        )
        self.counts["candidates"] = len(candidates)
        return Snapshot(
            taken_at=self.now,
            day=today,
            context=context,
            spy_bars=spy_bars,
            movers=movers,
            earnings_ok=earnings_ok,
            earnings=earnings,
            news_ok=news_ok,
            market_news=market_news,
            watchlist=list(jobs.watchlist),
            candidates=[c.symbol for c in candidates],
            candidate_sources={c.symbol: list(c.sources) for c in candidates},
        )

    # ------------------------------------------------------------------ posture

    async def decide(self, snapshot: Snapshot) -> PostureDecision:
        assert self.meter is not None
        spy_bars = snapshot.spy_bars
        if spy_bars and _stale(spy_bars, self.today):
            # Old bars would describe an old market: the SPY metrics count as missing, so
            # the code posture stands aside with "missing data" as its reason.
            self.note(f"SPY daily history is stale (last bar {spy_bars[-1].day.isoformat()})")
            spy_bars = []
        decision = await decide_posture(
            self.deps.llm,
            self.meter,
            model=self.jobs.dive.posture_model,
            max_tokens=self.jobs.dive.max_tokens,
            metrics=posture_metrics(snapshot.context, spy_bars),
            today=self.today,
            settings=self.jobs.posture,
            sector_gaps=_sector_gaps(snapshot.context),
            headlines=snapshot.market_news,
        )
        # A failed review is partial: the posture is only the code's (made at least
        # reduced), not what the run planned.
        for text in decision.notes:
            self.note(text, partial=decision.budget_hit or decision.review_failed)
        self.budget_noted = self.budget_noted or decision.budget_hit
        return decision

    # ------------------------------------------------------------------- screen

    async def screen(
        self, snapshot: Snapshot
    ) -> tuple[
        list[ScreenRow], dict[str, MarketQuote], dict[str, list[DailyBar]], dict[str, Profile]
    ]:
        deps, jobs, today = self.deps, self.jobs, self.today
        settings = jobs.screen
        symbols = snapshot.candidates
        batch = await deps.market.quotes(symbols) if symbols else QuoteBatch({})
        if batch.skipped:
            self.counts["unreadable_quotes"] = batch.skipped
        dropped: dict[str, str] = {}
        passing: list[str] = []
        for symbol in symbols:
            reason = quote_filter(symbol, batch.quotes.get(symbol), settings)
            if reason:
                dropped[symbol] = reason
            else:
                passing.append(symbol)
        histories = await self._histories(passing)
        rows: list[ScreenRow] = []
        bars: dict[str, list[DailyBar]] = {}
        for symbol in passing:
            history = histories[symbol]
            q = batch.quotes[symbol]
            if history is None:
                dropped[symbol] = DROP_HISTORY_ERROR
                continue
            if len(history) < MIN_BARS:
                dropped[symbol] = DROP_HISTORY
                continue
            if _stale(history, today):
                dropped[symbol] = DROP_STALE_HISTORY
                continue
            reason = history_filter(q, history, settings)
            if reason:
                dropped[symbol] = reason
                continue
            events = _events_for(snapshot.earnings, symbol)
            features, atr = compute_features(
                q,
                history,
                today=today,
                events=events,
                earnings_ok=snapshot.earnings_ok,
                lookahead=jobs.collect.earnings_lookahead_days,
            )
            assert q.last is not None
            near = snapshot.earnings_ok and earnings_near(events, today)
            rows.append(ScreenRow(symbol, q.last, atr, features, near))
            bars[symbol] = history

        if errors := sum(1 for r in dropped.values() if r == DROP_HISTORY_ERROR):
            # Half or more unreadable looks like an outage, not a few odd names.
            self.note(
                f"daily history unavailable for {errors} of {len(passing)} name(s)",
                partial=2 * errors >= len(passing),
            )

        news_counts = await self._news_counts(
            top_k(score_rows(rows, settings.weights), 2 * settings.deep_dive_count)
        )
        scored = score_rows(
            [with_news(r, news_counts.get(r.symbol, 0)) for r in rows], settings.weights
        )
        top = top_k(scored, settings.deep_dive_count)
        profiles = await self._profiles(top)
        self.counts["screened"] = len(scored)
        for reason, count in drop_counts(dropped).items():
            self.counts[f"drop_{reason}"] = count
        scores = {row.symbol: row for row in scored}
        await self.put_trail(
            "screen.json",
            [
                {
                    "symbol": symbol,
                    "sources": snapshot.candidate_sources.get(symbol, []),
                    "dropped": dropped.get(symbol),
                    "features": scores[symbol].features.as_dict() if symbol in scores else None,
                    "pre_score": scores[symbol].pre_score if symbol in scores else None,
                    "deep_dive": any(r.symbol == symbol for r in top),
                }
                for symbol in symbols
            ],
        )
        return top, batch.quotes, bars, profiles

    async def _histories(self, symbols: Sequence[str]) -> dict[str, list[DailyBar] | None]:
        gate = asyncio.Semaphore(BARS_CONCURRENCY)

        async def one(symbol: str) -> list[DailyBar] | None:
            async with gate:
                try:
                    return await self.deps.market.daily_bars(symbol, self.today, HISTORY_DAYS)
                except (SchwabError, ParseError) as exc:
                    log.warning("no history for %s: %s", symbol, _describe(exc))
                    return None

        # A TaskGroup: an unexpected error cancels the other reads, then fails the run.
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(one(s)) for s in symbols]
        return {s: task.result() for s, task in zip(symbols, tasks, strict=True)}

    async def _news_counts(self, rows: Sequence[ScreenRow]) -> dict[str, int]:
        counts: dict[str, int] = {}
        start = self.today - timedelta(days=NEWS_DAYS)
        for row in rows:
            try:
                items = await self.deps.events.company_news(row.symbol, start, self.today)
            except EventsUnavailable as exc:
                self.note(f"company news unavailable, news counts incomplete: {exc}", partial=True)
                break
            counts[row.symbol] = len(items)
        return counts

    async def _profiles(self, rows: Sequence[ScreenRow]) -> dict[str, Profile]:
        found: dict[str, Profile] = {}
        for row in rows:
            try:
                profile = await self.deps.events.profile(row.symbol)
            except EventsUnavailable as exc:
                self.note(f"company profiles unavailable, sectors unknown: {exc}", partial=True)
                break
            if profile is not None:
                found[row.symbol] = profile
        return found

    # --------------------------------------------------------------------- dive

    async def dive(
        self,
        snapshot: Snapshot,
        *,
        decision: PostureDecision,
        top: Sequence[ScreenRow],
        quotes: Mapping[str, MarketQuote],
        bars: Mapping[str, list[DailyBar]],
        profiles: Mapping[str, Profile],
    ) -> list[DiveResult]:
        deps, jobs = self.deps, self.jobs
        assert self.meter is not None
        meter = self.meter
        gate = asyncio.Semaphore(jobs.dive.dive_concurrency)
        deadline = self.started + self.max_run_s
        context = {
            "posture": decision.level.value,
            "metrics": decision.metrics.as_dict(),
            "sector_etf_gaps_pct": {
                s: round(gap, 2) for s, gap in _sector_gaps(snapshot.context).items()
            },
        }

        async def one(row: ScreenRow) -> DiveResult | None:
            async with gate:
                if deps.monotonic() >= deadline:
                    return None
                ctx = DiveContext(
                    symbol=row.symbol,
                    today=self.today,
                    quote=quotes[row.symbol],
                    bars=tuple(bars[row.symbol]),
                    features=row.features.as_dict(),
                    earnings=_events_for(snapshot.earnings, row.symbol),
                    earnings_ok=snapshot.earnings_ok,
                    profile=profiles.get(row.symbol),
                    market_context=context,
                )
                # Cancelled by the hard stop, run_dive charges the call in flight and
                # re-raises; the task then ends cancelled.
                result = await run_dive(
                    ctx,
                    market=deps.market,
                    events=deps.events,
                    llm=deps.llm,
                    meter=meter,
                    settings=jobs.dive,
                )
                await self.put_trail(f"dives/{row.symbol}.json", result.trail(jobs.dive.model))
                return result

        tasks: list[asyncio.Task[DiveResult | None]] = []
        try:
            async with asyncio.timeout_at(self.hard_stop):
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(one(row)) for row in top]
        except TimeoutError:
            pass  # counted below: the cancelled tasks
        cut = sum(1 for task in tasks if task.cancelled())
        outcomes = [task.result() for task in tasks if not task.cancelled()]
        results = [r for r in outcomes if r is not None]
        self.counts["dived"] = len(results)
        not_started = len(outcomes) - len(results)
        if not_started:
            self.note(f"deadline passed: {not_started} deep-dive(s) not started", partial=True)
        if cut:
            self.note(f"deadline passed: {cut} deep-dive(s) cut short", partial=True)
        stopped = sum(1 for r in results if r.budget_hit)
        if stopped:
            self.note(f"budget reached: {stopped} deep-dive(s) stopped", partial=True)
            self.budget_noted = True
        if any(r.news_failed for r in results):
            self.note("company news unavailable during deep-dives", partial=True)
        ended = Counter(r.outcome for r in results)
        for name, count in sorted(ended.items()):
            if name != "submitted":
                self.counts[f"dive_{name}"] = count
        timed_out = ended["timeout"]
        if failed := ended["llm_error"] + timed_out:
            # Half or more failing or timing out looks like the model being unreachable or
            # stuck, not one bad call.
            self.note(
                f"model calls failed in {failed} of {len(results)} deep-dive(s)"
                + (f" ({timed_out} timed out)" if timed_out else ""),
                partial=2 * failed >= len(results),
            )
        return results

    # --------------------------------------------------------- earnings check

    async def confirm_earnings(
        self,
        snapshot: Snapshot,
        results: Sequence[DiveResult],
        rows: Mapping[str, ScreenRow],
    ) -> tuple[dict[str, tuple[EarningsEvent, ...]], set[str]]:
        """Each name's earnings, and which swing ideas a symbol-scoped calendar call
        confirmed. The market-wide calendar can miss a name; a swing pick needs its own
        check. At most ``rank.max_picks`` calls, best ideas first, one at a time. A failed
        call refuses that name's swing pick only: noted, but not partial on its own."""
        earnings = {r.symbol: _events_for(snapshot.earnings, r.symbol) for r in results}
        confirmed: set[str] = set()
        if not snapshot.earnings_ok:
            return earnings, confirmed  # no swing picks at all
        settings = self.jobs.rank

        def order(r: DiveResult) -> tuple[int, int, str]:
            assert r.assessment is not None
            pre = rows[r.symbol].pre_score
            return (-blended_score(r.assessment.score, pre, settings.llm_weight), -pre, r.symbol)

        swing = sorted(
            (
                r
                for r in results
                if r.assessment is not None
                and r.assessment.side != "pass"
                and r.assessment.horizon == "swing"
                and not share_class(r.symbol)  # refused anyway
            ),
            key=order,
        )[: settings.max_picks]
        failed: list[str] = []
        first_error = ""
        for r in swing:
            try:
                found = await self.deps.events.earnings_calendar(
                    self.calendar_start, self.calendar_end, r.symbol
                )
            except EventsUnavailable as exc:
                failed.append(r.symbol)
                first_error = first_error or str(exc)
                continue
            merged = dict.fromkeys((*earnings[r.symbol], *_events_for(found, r.symbol)))
            earnings[r.symbol] = tuple(sorted(merged, key=lambda e: e.day))
            confirmed.add(r.symbol)
        if swing:
            self.counts["earnings_checks"] = len(swing)
        if failed:
            self.counts["earnings_check_failures"] = len(failed)
            self.note(
                f"earnings check failed for {len(failed)} name(s), no swing pick for them: "
                f"{first_error}"
            )
        return earnings, confirmed

    # ------------------------------------------------------------------- finish

    async def finish(
        self, posture: Posture, ranked: RankResult, assessments: Mapping[str, Any]
    ) -> RunOutcome:
        assert self.meter is not None
        self.counts["picks"] = len(ranked.picks)
        self._budget_notes()
        await self.put_trail(
            "result.json",
            {
                "status": (RunStatus.PARTIAL if self.partial else RunStatus.OK).value,
                "posture": posture,
                "assessments": assessments,
                "rejected": {r.symbol: r.reason for r in ranked.rejected},
                "picks": list(ranked.picks),
                "notes": self.notes,
                "cost_usd": str(self.meter.spent_usd),
                "counts": self.counts,
            },
        )
        # After result.json: a failed write there makes the run partial too.
        status = RunStatus.PARTIAL if self.partial else RunStatus.OK
        meta = self.meta(status)
        outcome = RunOutcome(
            status.value,
            EXIT_OK,
            self.run_id,
            meta=meta,
            posture=posture,
            picks=ranked.picks,
            rejected=ranked.rejected,
        )

        async def write() -> None:
            await self.deps.store.write_run(meta, ranked.picks, posture)

        return await self.commit(
            outcome, write, _summary(self.kind, self.today, posture, ranked.picks, meta)
        )


def _sector_gaps(context: Mapping[str, MarketQuote]) -> dict[str, float]:
    return {
        s: gap
        for s in SECTOR_ETFS
        if (q := context.get(s)) is not None and (gap := q.gap_pct) is not None
    }


def _leaves(exc: BaseException) -> list[BaseException]:
    """Every exception inside ``exc``, groups opened (in order), or ``exc`` itself."""
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


def _stale(bars: Sequence[DailyBar], today: date) -> bool:
    return weekdays_between(bars[-1].day, today) > MAX_BAR_AGE_WEEKDAYS


def _events_for(events: Sequence[EarningsEvent], symbol: str) -> tuple[EarningsEvent, ...]:
    return tuple(e for e in events if e.symbol == symbol)


def _summary(kind: str, day: date, posture: Posture, picks: Sequence[Pick], meta: RunMeta) -> str:
    """The alert text. Notes are already scrubbed; posture reasons (model-written) are
    left out on purpose."""
    vix = posture.metrics.get("vix")
    level = posture.level.value + (f" (vix {vix:.1f})" if vix is not None else "")
    listed = ", ".join(
        f"{p.symbol} {'L' if p.side.value == 'long' else 'B'} {p.horizon.value} {p.score}"
        for p in picks
    )
    text = (
        f"traider research {kind} {day.isoformat()}: posture {level}; {len(picks)} picks"
        f"{': ' + listed if listed else ''}; cost ${meta.cost_usd:.2f}"
    )
    if meta.status is RunStatus.PARTIAL and meta.notes:
        text += "; notes: " + "; ".join(meta.notes)
    return text
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `uv run pytest tests/unit/test_research_run_base.py tests/unit/test_research_run.py tests/unit/test_cli_research_run.py -q`
Expected: PASS (104 passed).

Then prove nothing else moved:

Run: `git diff --stat -- tests/unit/test_research_run.py tests/unit/test_cli_research_run.py`
Expected: no output (those tests are unchanged).

Run: `uv run python /tmp/premarket_fingerprint.py > /tmp/premarket-after.json 2>/dev/null && cmp /tmp/premarket-before.json /tmp/premarket-after.json && echo IDENTICAL`
Expected: `IDENTICAL`. If not, `diff` the two files: any difference is a behaviour change to undo, not to accept.

- [ ] **Step 6: Break on purpose (restore after each)**

Make each change, run the named tests, see them FAIL, then undo the change exactly.

- **Break 1.** Write before adding the day's cost. In `src/traider/research/run.py`, replace:

  ```python
              await asyncio.shield(self.cost_task)
              self.cost_added = True
              await write()
  ```

  with:

  ```python
              await write()
              await asyncio.shield(self.cost_task)
              self.cost_added = True
  ```

  Run: `uv run pytest tests/unit/test_research_run_base.py -q`. These FAIL: `test_the_cost_goes_in_before_the_write_and_the_alert_comes_last`.

- **Break 2.** Use one lock for every kind. In `src/traider/research/run.py`, replace:

  ```python
              run.lock_name, run.run_id, run.max_run_s + LOCK_SPARE_S, run.now
  ```

  with:

  ```python
              LOCK_NAME, run.run_id, run.max_run_s + LOCK_SPARE_S, run.now
  ```

  Run: `uv run pytest tests/unit/test_research_run_base.py -q`. These FAIL: `test_a_kind_runs_under_its_own_lock_and_releases_it`, `test_a_held_lock_exits_2_and_writes_nothing`, `test_another_kinds_lock_does_not_stop_it`.

- **Break 3.** Ignore the kind's own time limit. In `src/traider/research/run.py`, replace:

  ```python
          self.max_run_s = self.time_limit()
  ```

  with:

  ```python
          self.max_run_s = self.jobs.max_run_s
  ```

  Run: `uv run pytest tests/unit/test_research_run_base.py -q`. These FAIL: `test_a_kind_runs_under_its_own_lock_and_releases_it`, `test_the_time_box_follows_the_kinds_own_limit`, `test_a_run_past_its_box_fails_with_its_own_limit_in_the_error`.

- [ ] **Step 7: Whole suite, lint, types**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: all green: 2160 passed, 6 skipped.

- [ ] **Step 8: Commit**

```bash
git add src/traider/research/run.py tests/unit/test_research_run_base.py
git commit -m "refactor(research): share run scaffolding between kinds

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Xh7zur22CRYEGaDoTB5nkV"
```

---

### Task 5: The scorecard run, the bot-state reader, and `--kind scorecard`

**Files:**
- Create: `src/traider/research/botstate.py`, `src/traider/research/scorecard_run.py`
- Modify: `src/traider/config.py`, `src/traider/research/run.py`, `src/traider/research/wiring.py`, `src/traider/cli.py`
- Test: `tests/unit/test_research_botstate.py` (new), `tests/unit/test_research_scorecard_run.py` (new), `tests/unit/test_cli_research_run.py` (scorecard tests appended)

**Interfaces:**
- Consumes: `RunBase`, `run_locked`, `new_run_id`, `BARS_CONCURRENCY`, `_describe` (Task 4); `score_pick`, `traded_from_logs`, `summarize`, `summary_text`, `Scored` (Task 3); `picks_between`, `outcomes`, `put_outcome`, `put_score_summary`, `outcome_key`, `PickOutcome`, `OutcomeStatus` (Task 2); `ScorecardSettings` (Task 1); `traider.state.dynamo.DynamoStateStore`.
- Produces:
  - `traider.config.Config.state_namespace: Literal["paper", "live"] | None` (`TRAIDER_STATE_NAMESPACE`).
  - `traider.research.botstate.BotState` (Protocol: `async ledger() -> dict[str, LedgerEntry]`, `async events(day: str) -> list[dict[str, Any]]`; both state stores satisfy it) and `async held_symbols(state: BotState) -> set[str]` (ledger symbols as roots).
  - `RunDeps.state: BotState | None = None`; `RunOutcome.extra: Mapping[str, Any]` (merged last into `report()`; empty for pre-market, so its report is unchanged).
  - `traider.research.wiring.bot_state(config: Config, aws: app.Aws) -> BotState | None` (`SetupError` for a state table without a namespace); `build_deps` sets `RunDeps.state`.
  - `traider.research.scorecard_run`: `KIND = "scorecard"`, `EVENT = "research_scorecard"`, `Entry(day, pick, run)`, `async run_scorecard(deps, now, *, dry_run=False, force=False) -> RunOutcome`, `class ScorecardRun(RunBase)` (`kind = lock_name = "scorecard"`, `title = "Scorecard"`).
  - `traider.cli`: `RESEARCH_KINDS = ("premarket", "scorecard")`, `SCORECARD_DRY_RUN_NOTICE`, `_runner(kind) -> Runner` (looked up at run time, so `cli.run_premarket` can still be patched).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_research_botstate.py`:

```python
"""Research reads the bot's ledger and event log, in the bot's own namespace, and never
writes them."""

from datetime import UTC, datetime

import pytest
from moto import mock_aws

from tests.unit.test_state import TABLE as STATE_TABLE
from tests.unit.test_state import make_table as make_state_table
from traider import app
from traider.config import Config, ConfigError
from traider.research.botstate import BotState, held_symbols
from traider.research.wiring import SetupError, bot_state
from traider.state.base import LedgerEntry
from traider.state.dynamo import DynamoStateStore
from traider.state.memory import MemoryStateStore

T0 = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)


def entry(symbol: str) -> LedgerEntry:
    return LedgerEntry(symbol, horizon="swing", side="long", opened_at=T0)


def test_both_state_stores_satisfy_the_read_protocol():
    memory: BotState = MemoryStateStore()
    dynamo: BotState = DynamoStateStore(None, "paper")
    assert memory is not None and dynamo is not None


async def test_held_symbols_are_the_ledgers_roots():
    state = MemoryStateStore()
    for symbol in ("NVDA", "AMD   261016P00048000", "PLTR"):
        await state.put_ledger(entry(symbol))
    assert await held_symbols(state) == {"NVDA", "AMD", "PLTR"}
    assert await held_symbols(MemoryStateStore()) == set()


def test_the_namespace_comes_from_the_environment_and_is_paper_or_live():
    env = {"TRAIDER_RESEARCH_TABLE": "r", "TRAIDER_STATE_NAMESPACE": "live"}
    assert Config.from_env(env).state_namespace == "live"
    assert Config.from_env({"TRAIDER_RESEARCH_TABLE": "r"}).state_namespace is None
    with pytest.raises(ConfigError, match="state_namespace"):
        Config.from_env({**env, "TRAIDER_STATE_NAMESPACE": "backtest"})


def test_without_a_state_table_there_is_nothing_to_read():
    assert bot_state(Config(research_table="r"), app.Aws("us-west-2")) is None


def test_a_state_table_without_a_namespace_is_refused():
    config = Config(research_table="r", state_table=STATE_TABLE)
    with pytest.raises(SetupError, match="TRAIDER_STATE_NAMESPACE"):
        bot_state(config, app.Aws("us-west-2"))


async def test_research_reads_the_namespace_it_is_told_and_only_that_one():
    with mock_aws():
        table = make_state_table()
        await DynamoStateStore(table, "live").put_ledger(entry("NVDA"))
        await DynamoStateStore(table, "paper").put_ledger(entry("AMD"))
        await DynamoStateStore(table, "live").log_event("order_submitted", {"symbol": "NVDA"}, T0)
        config = Config(
            research_table="r",
            state_table=STATE_TABLE,
            state_namespace="live",
            aws_region="us-west-2",
        )
        state = bot_state(config, app.Aws("us-west-2"))
        assert state is not None
        assert await held_symbols(state) == {"NVDA"}
        (event,) = await state.events("2026-10-09")
        assert (event["kind"], event["data"]) == ("order_submitted", {"symbol": "NVDA"})


def test_research_can_only_read_the_bots_state():
    methods = {name for name in vars(BotState) if not name.startswith("_")}
    assert methods == {"ledger", "events"}
```

Create `tests/unit/test_research_scorecard_run.py`:

```python
"""The scorecard run end to end, on fakes: a week of picks, their bars and the bot's event
log, then every way it can go wrong. Nothing here touches Schwab or AWS."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

from tests.fakes.research import FakeEvents, FakeMarketData, ScriptedLLM
from tests.unit.test_research_run import Trails
from traider.alerts import LogAlerter
from traider.research.market import DailyBar
from traider.research.models import (
    OutcomeStatus,
    Pick,
    PickOutcome,
    RunMeta,
    RunStatus,
    ScoreSummary,
)
from traider.research.rank import close_of
from traider.research.run import RunDeps
from traider.research.scorecard_run import run_scorecard
from traider.research.store import MemoryResearchStore
from traider.schwab.client import SchwabUnavailable
from traider.settings import Settings
from traider.state.memory import MemoryStateStore
from traider.timeutil import ManualClock

NOW = datetime(2026, 10, 9, 20, 30, tzinfo=UTC)  # Friday 16:30 New York
TODAY = date(2026, 10, 9)
DAY = TODAY.isoformat()
MON, TUE, WED, THU = (date(2026, 10, d) for d in (5, 6, 7, 8))


def bar(day, o, h, low, c) -> DailyBar:
    return DailyBar(day=day, open=o, high=h, low=low, close=c, volume=1_000_000)


def week(start: float) -> list[DailyBar]:
    """Monday to Friday: up 2% on Monday, then drifting; Friday is today."""
    s = start / 100
    return [
        bar(MON, 100 * s, 103 * s, 99 * s, 102 * s),
        bar(TUE, 102 * s, 104 * s, 98 * s, 101 * s),
        bar(WED, 101 * s, 106 * s, 100 * s, 105 * s),
        bar(THU, 105 * s, 107 * s, 103 * s, 104 * s),
        bar(TODAY, 104 * s, 105 * s, 94 * s, 96 * s),
    ]


def pick(symbol, rank, run_id, *, side="long", horizon="intraday", expires=MON, score=80):
    return Pick(
        run_id=run_id,
        rank=rank,
        symbol=symbol,
        side=side,
        horizon=horizon,
        score=score,
        pre_score=70,
        thesis="t",
        invalidation=Decimal("95") if side == "long" else Decimal("120"),
        expires_at=close_of(expires),
        features={"llm_score": 85.0, "price_at_pick": 100.0},
    )


def run_meta(run_id, day, *, kind="premarket", status="ok") -> RunMeta:
    started = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=12)
    return RunMeta(
        run_id=run_id,
        kind=kind,
        status=status,
        started_at=started,
        finished_at=started.replace(minute=10),
        trading_day=day,
    )


async def a_week(store: MemoryResearchStore) -> None:
    """Monday's pre-market run: NVDA long swing to Friday, AMD bearish intraday. Tuesday's
    partial run: TSLA. Wednesday's failed run: MSFT (never scored). Thursday's intraday
    run: PLTR."""
    await store.write_run(
        run_meta("premarket-a", MON),
        [
            pick("NVDA", 1, "premarket-a", horizon="swing", expires=TODAY, score=84),
            pick("AMD", 2, "premarket-a", side="bearish", score=72),
        ],
        None,
    )
    await store.write_run(
        run_meta("premarket-p", TUE, status="partial"),
        [pick("TSLA", 1, "premarket-p", expires=TUE)],
        None,
    )
    await store.write_run(
        run_meta("premarket-f", WED, status="failed"),
        [pick("MSFT", 1, "premarket-f", expires=WED)],
        None,
    )
    await store.write_run(
        run_meta("intraday-i", THU, kind="intraday"),
        [pick("PLTR", 1, "intraday-i", expires=THU, score=91)],
        None,
    )


def market() -> FakeMarketData:
    m = FakeMarketData()
    m.bars = {"NVDA": week(100), "AMD": week(50), "TSLA": week(200), "PLTR": week(20)}
    return m


async def bot_log() -> MemoryStateStore:
    """The bot bought NVDA shares on Monday and a put on AMD... on Tuesday, after AMD's
    intraday pick had expired."""
    state = MemoryStateStore("paper")
    buy = {"side": "BUY", "quantity": 1, "order_type": "LIMIT"}
    await state.log_event(
        "order_submitted", {**buy, "symbol": "NVDA"}, datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    )
    await state.log_event(
        "order_submitted",
        {**buy, "symbol": "AMD   261016P00048000"},
        datetime(2026, 10, 6, 14, 0, tzinfo=UTC),
    )
    return state


async def deps(*, store=None, m=None, state=None, settings=None) -> RunDeps:
    if store is None:
        store = MemoryResearchStore()
        await a_week(store)
    return RunDeps(
        store=store,
        market=m or market(),
        events=FakeEvents(),
        llm=ScriptedLLM(),
        trail=Trails(),
        alerts=LogAlerter(),
        settings=settings or Settings(),
        clock=ManualClock(NOW),
        monotonic=lambda: 0.0,
        state=state if state is not None else await bot_log(),
    )


def stored(store, key) -> PickOutcome:
    return PickOutcome.model_validate_json(store.raw(key, "OUTCOME")["body"])


def stored_meta(store, run_id) -> RunMeta:
    return RunMeta.model_validate(json.loads(store.raw(f"RUN#{run_id}", "META")["body"]))


# --- the golden week -----------------------------------------------------------------------


async def test_a_week_of_picks_is_scored():
    d = await deps()
    outcome = await run_scorecard(d, NOW)
    assert (outcome.status, outcome.exit_code) == ("ok", 0)
    assert outcome.run_id.startswith("scorecard-20261009T203000Z-")

    nvda = stored(d.store, "PICK#premarket-a#001")
    assert (nvda.entry, nvda.ret_1d, nvda.ret_5d, nvda.ret_20d) == (100.0, 2.0, -4.0, None)
    assert (nvda.mfe_pct, nvda.mae_pct, nvda.hit_invalidation) == (7.0, -6.0, True)
    assert (nvda.expired_return, nvda.traded, nvda.status) == (-4.0, True, OutcomeStatus.PARTIAL)
    assert (nvda.llm_score, nvda.price_at_pick) == (85, 100.0)

    amd = stored(d.store, "PICK#premarket-a#002")
    assert (amd.ret_0d, amd.ret_1d, amd.expired_return) == (-2.0, -2.0, -2.0)
    assert amd.traded is False  # the put was bought after the intraday pick expired

    tsla = stored(d.store, "PICK#premarket-p#001")
    assert (tsla.run_status, tsla.entry, tsla.ret_0d) == (RunStatus.PARTIAL, 204.0, -0.9804)
    assert d.store.raw("PICK#premarket-f#001", "OUTCOME") is None  # a failed run's pick

    summary = ScoreSummary.model_validate_json(d.store.raw(f"SCORE#{DAY}", "SUMMARY")["body"])
    assert summary.picks == 4
    assert summary.by_kind == {"intraday": 1, "premarket": 3}
    assert summary.by_side == {"bearish": 1, "long": 3}
    # Matured today: Monday's 5-day returns (NVDA -4%, AMD bearish +4%); no pick was made
    # today, so no 1-day return matured.
    assert (summary.ret_1d.matured, summary.ret_5d.matured) == (0, 2)
    assert (summary.ret_5d.hits, summary.ret_5d.mean_pct) == (1, 0.0)

    meta = stored_meta(d.store, outcome.run_id)
    assert (meta.kind, meta.status, meta.models, meta.cost_usd) == (
        "scorecard",
        RunStatus.OK,
        (),
        Decimal(0),
    )
    assert meta.counts == {"picks": 4, "final_skipped": 0, "scored": 4, "outcome_partial": 4}
    ((event, subject, message),) = d.alerts.sent
    assert (event, subject) == ("research_scorecard", f"Scorecard {DAY}: ok")
    assert message == (
        "traider scorecard 2026-10-09: no picks matured 1d; 2 picks matured 5d, hit 1/2, "
        "mean +0.0%; 4 picks in the window"
    )
    assert ("LOCK#scorecard", "LOCK") not in d.store.keys
    result = d.trail.only().files["result.json"]
    assert result["status"] == "ok" and len(result["outcomes"]) == 4


async def test_the_bot_never_sees_the_scorecard():
    from traider.config import ResearchSettings
    from traider.research.source import ResearchSource

    d = await deps()
    await run_scorecard(d, NOW)
    source = ResearchSource(d.store, ResearchSettings)
    await source.refresh(NOW)
    assert "scorecard" not in {run.kind for run in (await d.store.day(DAY)).runs.values()}
    assert source.view.posture is None


async def test_final_outcomes_are_skipped_and_still_summarised():
    d = await deps()
    final = stored_outcome_final()
    await d.store.put_outcome(final)
    outcome = await run_scorecard(d, NOW)
    assert stored(d.store, final.key) == final  # not rewritten
    assert outcome.meta.counts["final_skipped"] == 1
    assert "NVDA" not in d.market.called("daily_bars")
    summary = ScoreSummary.model_validate_json(d.store.raw(f"SCORE#{DAY}", "SUMMARY")["body"])
    assert summary.picks == 4


def stored_outcome_final() -> PickOutcome:
    return PickOutcome(
        run_id="premarket-a",
        rank=1,
        symbol="NVDA",
        side="long",
        horizon="swing",
        score=84,
        pre_score=70,
        pick_day=MON,
        run_status="ok",
        status="final",
        updated_at=NOW,
    )


async def test_picks_older_than_the_lookback_are_left_alone():
    store = MemoryResearchStore()
    await a_week(store)
    old_day = date(2026, 9, 25)  # 10 weekdays before today
    await store.write_run(
        run_meta("premarket-o", old_day), [pick("OLD", 1, "premarket-o", expires=old_day)], None
    )
    edge_day = date(2026, 9, 28)  # 9 weekdays before today
    await store.write_run(
        run_meta("premarket-e", edge_day), [pick("EDGE", 1, "premarket-e", expires=edge_day)], None
    )
    d = await deps(
        store=store, settings=Settings(research_jobs={"scorecard": {"lookback_days": 9}})
    )
    await run_scorecard(d, NOW)
    assert store.raw("PICK#premarket-o#001", "OUTCOME") is None
    assert stored(store, "PICK#premarket-e#001").status is OutcomeStatus.PENDING  # no bars


# --- failures ----------------------------------------------------------------------------


class SomeBarsFail(FakeMarketData):
    def __init__(self, failing):
        super().__init__()
        self.failing = set(failing)

    async def daily_bars(self, symbol, before, days):
        if symbol in self.failing:
            self.calls.append(("daily_bars", symbol))
            raise SchwabUnavailable("GET /marketdata/v1/pricehistory: HTTP 503")
        return await super().daily_bars(symbol, before, days)


async def test_a_symbol_without_bars_stays_pending_with_a_note():
    m = SomeBarsFail({"TSLA"})
    m.bars = market().bars
    d = await deps(m=m)
    outcome = await run_scorecard(d, NOW)
    assert outcome.status == "ok"
    assert stored(d.store, "PICK#premarket-p#001").status is OutcomeStatus.PENDING
    assert outcome.meta.counts["bars_failed"] == 1
    assert (
        "daily bars unreadable for 1 of 4 symbol(s); their picks stay pending" in outcome.meta.notes
    )


async def test_unreadable_bars_keep_the_last_outcome():
    m = SomeBarsFail({"NVDA"})
    m.bars = market().bars
    d = await deps(m=m)
    before = stored_outcome_final().model_copy(
        update={"status": OutcomeStatus.PARTIAL, "ret_1d": 2.0}
    )
    await d.store.put_outcome(before)
    await run_scorecard(d, NOW)
    assert stored(d.store, before.key) == before


async def test_half_the_symbols_without_bars_is_partial():
    m = SomeBarsFail({"NVDA", "AMD"})
    m.bars = market().bars
    d = await deps(m=m)
    outcome = await run_scorecard(d, NOW)
    assert outcome.status == "partial"
    assert stored_meta(d.store, outcome.run_id).status is RunStatus.PARTIAL
    ((_, subject, message),) = d.alerts.sent
    assert subject == f"Scorecard {DAY}: partial"
    assert "notes: daily bars unreadable for 2 of 4 symbol(s)" in message


async def test_an_unreadable_event_log_day_makes_traded_unknown():
    class Flaky(MemoryStateStore):
        async def events(self, day):
            if day == "2026-10-06":
                raise RuntimeError("throttled")
            return await super().events(day)

    state = Flaky("paper")
    state._data = (await bot_log())._data
    d = await deps(state=state)
    outcome = await run_scorecard(d, NOW)
    assert outcome.status == "ok"
    assert stored(d.store, "PICK#premarket-a#001").traded is True  # found on Monday
    assert stored(d.store, "PICK#premarket-p#001").traded is None  # Tuesday unreadable
    assert stored(d.store, "PICK#premarket-a#002").traded is False  # Monday only
    assert outcome.meta.counts["event_log_failures"] == 1


async def test_without_a_state_table_traded_is_unknown():
    d = await deps(state=MemoryStateStore())
    d.state = None
    outcome = await run_scorecard(d, NOW)
    assert stored(d.store, "PICK#premarket-a#001").traded is None
    assert "no state table: whether the bot traded a pick is unknown" in outcome.meta.notes


async def test_anything_else_fails_the_run():
    m = market()
    m.failures["daily_bars"] = RuntimeError("bug")
    d = await deps(m=m)
    outcome = await run_scorecard(d, NOW)
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    meta = stored_meta(d.store, outcome.run_id)
    assert (meta.status, meta.error) == (RunStatus.FAILED, "bars: RuntimeError: bug")
    assert d.store.raw(f"SCORE#{DAY}", "SUMMARY") is None
    ((event, _, message),) = d.alerts.sent
    assert event == "research_run_failed"
    assert message == f"traider research scorecard {DAY} failed: bars: RuntimeError: bug"
    assert ("LOCK#scorecard", "LOCK") not in d.store.keys


# --- when it runs ------------------------------------------------------------------------


async def test_a_closed_market_writes_nothing():
    m = market()
    m.open_today = False
    d = await deps(m=m)
    before = set(d.store.keys)
    outcome = await run_scorecard(d, NOW)
    assert outcome.status == "closed" and set(d.store.keys) == before


async def test_once_a_day_unless_forced():
    d = await deps()
    first = await run_scorecard(d, NOW)
    again = await run_scorecard(d, NOW)
    assert again.status == "skipped" and first.run_id in again.detail
    forced = await run_scorecard(d, NOW, force=True)
    assert forced.status == "ok"


async def test_switched_off_means_no_run():
    for jobs in ({"enabled": False}, {"scorecard": {"enabled": False}}):
        d = await deps(settings=Settings(research_jobs=jobs))
        before = set(d.store.keys)
        outcome = await run_scorecard(d, NOW)
        assert (outcome.status, outcome.exit_code) == ("disabled", 0)
        assert set(d.store.keys) == before


async def test_a_held_lock_exits_2():
    d = await deps()
    await d.store.acquire_lock("scorecard", "someone", 3600, NOW)
    outcome = await run_scorecard(d, NOW)
    assert (outcome.status, outcome.exit_code) == ("locked", 2)


async def test_the_premarket_lock_does_not_block_the_scorecard():
    d = await deps()
    await d.store.acquire_lock("premarket", "someone", 3600, NOW)
    assert (await run_scorecard(d, NOW)).status == "ok"


async def test_a_dry_run_reads_everything_and_writes_nothing():
    d = await deps()
    before = set(d.store.keys)
    outcome = await run_scorecard(d, NOW, dry_run=True)
    assert outcome.status == "ok" and set(d.store.keys) == before
    assert d.alerts.sent == []
    report = outcome.report()
    assert report["summary"]["picks"] == 4 and len(report["outcomes"]) == 4


def test_the_scorecard_never_imports_order_code():
    from pathlib import Path

    from traider.research import botstate, scorecard, scorecard_run

    for module in (scorecard, scorecard_run, botstate):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "place_order" not in source and "broker" not in source, module.__name__
```

Append to the end of `tests/unit/test_cli_research_run.py`, after two blank lines:

```python
# --- C2a: the scorecard --------------------------------------------------------------------


async def test_the_scorecard_kind_runs_the_scorecard(tmp_path):
    store = MemoryResearchStore()
    build, _ = fake_build(store)
    out = io.StringIO()
    code = await cli.research_run(
        CONFIG,
        out,
        kind="scorecard",
        dry_run=False,
        force=False,
        trail_dir=str(tmp_path),
        build=build,
        now=NOW,
    )
    assert code == 0
    assert out.getvalue().startswith("research scorecard ok: scorecard-20261009T120000Z-")
    assert ("SCORE#2026-10-09", "SUMMARY") in store.keys


async def test_a_scorecard_dry_run_says_it_calls_no_model(tmp_path):
    build, made = fake_build()
    out = io.StringIO()
    code = await cli.research_run(
        CONFIG,
        out,
        kind="scorecard",
        dry_run=True,
        force=False,
        trail_dir=str(tmp_path),
        build=build,
        now=NOW,
    )
    assert code == 0
    notice, _, printed = out.getvalue().partition("\n\n")
    assert notice.startswith("Dry run: real calls to Schwab (daily bars)")
    assert "no model is called" in notice and "cost real money" not in notice
    assert json.loads(printed)["summary"]["picks"] == 0
    assert made["store"].keys == set()


def test_the_scorecard_kind_parses():
    args = cli._parser().parse_args(["research", "run", "--kind", "scorecard", "--dry-run"])
    assert (args.kind, args.dry_run) == ("scorecard", True)
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_research_botstate.py tests/unit/test_research_scorecard_run.py tests/unit/test_cli_research_run.py -q`
Expected: FAIL: collection errors (2 errors), `ModuleNotFoundError: No module named 'traider.research.botstate'` and `No module named 'traider.research.scorecard_run'`. (The appended CLI tests would fail too: `scorecard` is not a kind yet.)

- [ ] **Step 3: Implement**

`Config` gets the bot's namespace, for research only:

In `src/traider/config.py`, replace:

```python
    state_table: str | None = None
```

with:

```python
    state_table: str | None = None
    # The bot's namespace in the state table (its trading mode). Only research reads it:
    # the research task runs without a trading mode, so it is told which one to read.
    state_namespace: Literal["paper", "live"] | None = None
```

Create `src/traider/research/botstate.py`:

```python
"""What research may read of the bot's own state: its position ledger (``POS#<ns>``) and
its event log (``LOG#<ns>#<day>``). Nothing else, and never a write: the research task's
role may only Query those two kinds of partition in the state table.

``<ns>`` is the bot's trading mode (``paper`` or ``live``). The research task does not run
in that mode, so it is told the namespace explicitly (``TRAIDER_STATE_NAMESPACE``).
"""

from __future__ import annotations

from typing import Any, Protocol

from traider.state.base import LedgerEntry
from traider.universe import root_symbol


class BotState(Protocol):
    """The read-only part of ``StateStore`` research uses. ``DynamoStateStore`` and
    ``MemoryStateStore`` both satisfy it."""

    async def ledger(self) -> dict[str, LedgerEntry]: ...

    async def events(self, day: str) -> list[dict[str, Any]]: ...


async def held_symbols(state: BotState) -> set[str]:
    """The equity symbols the bot holds positions in, by its ledger. An option counts as
    its underlying."""
    return {root_symbol(symbol) for symbol in await state.ledger()}
```

`RunDeps` gets the bot's state and `RunOutcome` a place for extra report data:

In `src/traider/research/run.py` (1 of 4), replace:

```python
from traider.alerts import Alerter
```

with:

```python
from traider.alerts import Alerter
from traider.research.botstate import BotState
```

In `src/traider/research/run.py` (2 of 4), replace:

```python
    monotonic: Callable[[], float] = time.monotonic
```

with:

```python
    monotonic: Callable[[], float] = time.monotonic
    # The bot's ledger and event log, read-only. None without a state table.
    state: BotState | None = None
```

In `src/traider/research/run.py` (3 of 4), replace:

```python
    detail: str = ""
```

with:

```python
    detail: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)  # more JSON-ready data to print
```

In `src/traider/research/run.py` (4 of 4), replace:

```python
            "counts": dict(self.meta.counts) if self.meta else {},
```

with:

```python
            "counts": dict(self.meta.counts) if self.meta else {},
            **self.extra,
```

Create `src/traider/research/scorecard_run.py`:

```python
"""The scorecard run, every weekday after the close: how each recent pick actually did.

    lock(scorecard) -> market open today? -> already done today? -> META running
      -> picks from ok and partial runs over the last ``scorecard.lookback_days`` weekdays
      -> skip those already ``final`` -> daily bars per symbol -> the bot's event log
      -> score each pick -> write outcomes, the day's summary, then META -> alert

No model is called. A symbol whose bars cannot be read keeps its last outcome (or gets a
``pending`` one) and a note; half the symbols or more unreadable makes the run ``partial``.
An unreadable event-log day makes ``traded`` unknown (None) for the picks it covers, with a
note. Anything else fails the run. Nothing the bot reads depends on the scorecard.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar, Final

from traider.research.market import DailyBar
from traider.research.models import (
    OutcomeStatus,
    Pick,
    PickOutcome,
    RunKind,
    RunMeta,
    RunStatus,
    outcome_key,
)
from traider.research.run import (
    BARS_CONCURRENCY,
    EXIT_OK,
    RunBase,
    RunDeps,
    RunOutcome,
    _describe,
    new_run_id,
    run_locked,
)
from traider.research.scorecard import (
    Scored,
    score_pick,
    summarize,
    summary_text,
    traded_from_logs,
)
from traider.schwab.client import SchwabError
from traider.schwab.parse import ParseError
from traider.timeutil import previous_weekday, trading_date, weekdays_between, weekdays_from

log = logging.getLogger(__name__)

KIND: Final = "scorecard"
EVENT: Final = "research_scorecard"
BARS_SPARE_DAYS = 5  # history asked for beyond the oldest pick day, for holidays


@dataclass(frozen=True, slots=True)
class Entry:
    day: date
    pick: Pick
    run: RunMeta

    @property
    def key(self) -> str:
        return outcome_key(self.pick.run_id, self.pick.rank)

    @property
    def live_from(self) -> datetime:
        """When the bot could first see the pick: its run's META is written last."""
        return self.run.finished_at or self.run.started_at


async def run_scorecard(
    deps: RunDeps, now: datetime, *, dry_run: bool = False, force: bool = False
) -> RunOutcome:
    """One scorecard run. ``dry_run`` reads everything but writes nothing to the research
    table and sends no alert; ``force`` ignores an earlier ok or partial scorecard today."""
    now = now.astimezone(UTC)
    run_id = new_run_id(now, KIND)
    jobs = deps.settings.research_jobs
    if not jobs.enabled or not jobs.scorecard.enabled:
        log.info("the scorecard is switched off; nothing to do")
        detail = "research_jobs.enabled or research_jobs.scorecard.enabled is false"
        return RunOutcome("disabled", EXIT_OK, run_id, detail=detail)
    return await run_locked(ScorecardRun(deps, now, run_id, dry_run=dry_run), force=force)


class ScorecardRun(RunBase):
    kind: ClassVar[RunKind] = KIND
    lock_name: ClassVar[str] = KIND
    title: ClassVar[str] = "Scorecard"

    async def _execute(self, *, force: bool) -> RunOutcome:
        deps, day = self.deps, self.today.isoformat()
        self.stage = "market_hours"
        session = await deps.market.market_session(self.today)
        if session.open is None or session.close is None:
            log.info("no regular session on %s; nothing to score", day)
            return RunOutcome("closed", EXIT_OK, self.run_id, detail=f"market closed on {day}")
        if not force:
            done = await self.done_today()
            if done is not None:
                log.info("%s already has a scorecard (%s); skipping", day, done.run_id)
                return RunOutcome(
                    "skipped", EXIT_OK, self.run_id, detail=f"already done by {done.run_id}"
                )
        await self.begin(Decimal(0))  # no model calls

        self.stage = "picks"
        entries = await self.entries()
        existing = await deps.store.outcomes([e.key for e in entries])
        todo = [e for e in entries if _not_final(existing.get(e.key))]
        self.counts["picks"] = len(entries)
        self.counts["final_skipped"] = len(entries) - len(todo)

        self.stage = "bars"
        bars = await self.bars(todo)
        self.stage = "event_log"
        logs = await self.event_logs(todo)

        self.stage = "score"
        fresh: list[Scored] = []
        kept: list[Scored] = []
        for entry in todo:
            history = bars.get(entry.pick.symbol)
            before = existing.get(entry.key)
            if history is None and before is not None:
                kept.append(Scored(before))  # unreadable bars: the last outcome stands
                continue
            end = min(entry.pick.expires_at, self.now)
            traded = (
                traded_from_logs(logs, entry.pick.symbol, entry.live_from, end)
                if logs is not None
                else None
            )
            scored = score_pick(
                entry.pick,
                pick_day=entry.day,
                run_status=entry.run.status,
                bars=history or (),
                today=self.today,
                traded=traded,
                now=self.now,
            )
            if history is None:  # never "final" for want of bars
                pending = scored.outcome.model_copy(update={"status": OutcomeStatus.PENDING})
                scored = Scored(pending)
            fresh.append(scored)
        finals = [Scored(o) for e in entries if (o := existing.get(e.key)) and not _not_final(o)]
        summary = summarize(
            [*fresh, *kept, *finals],
            day=self.today,
            run_id=self.run_id,
            kinds={e.run.run_id: e.run.kind for e in entries},
            now=self.now,
        )
        self.counts["scored"] = len(fresh)
        for kind in OutcomeStatus:
            if count := sum(1 for s in fresh if s.outcome.status is kind):
                self.counts[f"outcome_{kind.value}"] = count

        self.stage = "write"
        await self.put_trail(
            "result.json",
            {
                "status": (RunStatus.PARTIAL if self.partial else RunStatus.OK).value,
                "summary": summary,
                "outcomes": [s.outcome for s in fresh],
                "notes": self.notes,
                "counts": self.counts,
            },
        )
        status = RunStatus.PARTIAL if self.partial else RunStatus.OK
        meta = self.meta(status)
        outcome = RunOutcome(
            status.value,
            EXIT_OK,
            self.run_id,
            meta=meta,
            extra={
                "summary": summary.model_dump(mode="json"),
                "outcomes": [s.outcome.model_dump(mode="json") for s in fresh],
            },
        )

        async def write() -> None:
            for scored in fresh:
                await deps.store.put_outcome(scored.outcome)
            await deps.store.put_score_summary(day, summary)
            await deps.store.put_meta(meta)  # last

        message = summary_text(summary)
        if meta.notes and status is RunStatus.PARTIAL:
            message += "; notes: " + "; ".join(meta.notes)
        return await self.commit(outcome, write, message, event=EVENT)

    async def entries(self) -> list[Entry]:
        """Every pick of an ok or partial run from ``lookback_days`` weekdays ago to today."""
        start = self.today
        for _ in range(self.jobs.scorecard.lookback_days):
            start = previous_weekday(start)
        days = await self.deps.store.picks_between(start, self.today)
        entries: list[Entry] = []
        unreadable = 0
        for found in days:
            unreadable += found.invalid
            for pick in found.picks:
                run = found.runs.get(pick.run_id)
                if run is not None and run.status in (RunStatus.OK, RunStatus.PARTIAL):
                    entries.append(Entry(date.fromisoformat(found.day), pick, run))
        if unreadable:
            self.counts["unreadable_items"] = unreadable
        return entries

    async def bars(self, entries: Sequence[Entry]) -> dict[str, list[DailyBar] | None]:
        """Daily bars per symbol, from the oldest pick day to today. None: unreadable."""
        if not entries:
            return {}
        symbols = sorted({e.pick.symbol for e in entries})
        oldest = min(e.day for e in entries)
        days = weekdays_between(oldest, self.today) + 1 + BARS_SPARE_DAYS
        after_today = self.today + timedelta(days=1)  # today's bar included, if Schwab has it
        gate = asyncio.Semaphore(BARS_CONCURRENCY)

        async def one(symbol: str) -> list[DailyBar] | None:
            async with gate:
                try:
                    return await self.deps.market.daily_bars(symbol, after_today, days)
                except (SchwabError, ParseError) as exc:
                    log.warning("no daily bars for %s: %s", symbol, _describe(exc))
                    return None

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(one(s)) for s in symbols]
        found = {s: task.result() for s, task in zip(symbols, tasks, strict=True)}
        if failed := sum(1 for bars in found.values() if bars is None):
            self.counts["bars_failed"] = failed
            self.note(
                f"daily bars unreadable for {failed} of {len(symbols)} symbol(s); "
                "their picks stay pending",
                partial=2 * failed >= len(symbols),
            )
        return found

    async def event_logs(
        self, entries: Sequence[Entry]
    ) -> Mapping[date, Sequence[Mapping[str, Any]] | None] | None:
        """The bot's event log for every day a pick was live. None without a state table;
        a day that cannot be read is None (``traded`` is then unknown)."""
        state = self.deps.state
        if state is None:
            if entries:
                self.note("no state table: whether the bot traded a pick is unknown")
            return None
        days = sorted(
            {
                day
                for e in entries
                for day in weekdays_from(
                    trading_date(e.live_from), trading_date(min(e.pick.expires_at, self.now))
                )
            }
        )
        logs: dict[date, Sequence[Mapping[str, Any]] | None] = {}
        failed = 0
        first_error = ""
        for day in days:
            try:
                logs[day] = await state.events(day.isoformat())
            except Exception as exc:
                logs[day] = None
                failed += 1
                first_error = first_error or _describe(exc)
        if failed:
            self.counts["event_log_failures"] = failed
            self.note(
                f"the bot's event log was unreadable for {failed} day(s), so traded is "
                f"unknown there: {first_error}"
            )
        return logs


def _not_final(outcome: PickOutcome | None) -> bool:
    return outcome is None or outcome.status is not OutcomeStatus.FINAL
```

The wiring builds the read-only state reader:

In `src/traider/research/wiring.py` (1 of 6), replace:

```python
same newer-wins rule as the bot, and an expired sign-in fails the run.
```

with:

```python
same newer-wins rule as the bot, and an expired sign-in fails the run.

With a state table it reads the bot's ledger and event log (``BotState``), in the
namespace ``TRAIDER_STATE_NAMESPACE`` names; a table without a namespace is refused.
```

In `src/traider/research/wiring.py` (2 of 6), replace:

```python
from traider.config import Config
```

with:

```python
from traider.config import Config
from traider.research.botstate import BotState
```

In `src/traider/research/wiring.py` (3 of 6), replace:

```python
from traider.settings_store import DynamoSettingsStore, SettingsInvalid
```

with:

```python
from traider.settings_store import DynamoSettingsStore, SettingsInvalid
from traider.state.dynamo import DynamoStateStore
```

In `src/traider/research/wiring.py` (4 of 6), replace:

```text
        raise SetupError(str(exc)) from None


```

with:

```python
        raise SetupError(str(exc)) from None


def bot_state(config: Config, aws: app.Aws) -> BotState | None:
    """The bot's state, to read only. None without a state table. The namespace must be
    explicit: reading the wrong one would hide what the bot holds and trades."""
    if not config.state_table:
        return None
    if not config.state_namespace:
        raise SetupError(
            "TRAIDER_STATE_TABLE is set without TRAIDER_STATE_NAMESPACE (paper or live): "
            "research must be told which of the bot's namespaces to read"
        )
    return DynamoStateStore(aws.table(config.state_table), config.state_namespace)


```

In `src/traider/research/wiring.py` (5 of 6), replace:

```python
    settings = await load_settings(config, aws)
```

with:

```python
    settings = await load_settings(config, aws)
    state = bot_state(config, aws)
```

In `src/traider/research/wiring.py` (6 of 6), replace:

```text
        settings=settings,
        clock=clock,
```

with:

```text
        settings=settings,
        clock=clock,
        state=state,
```

The CLI learns the scorecard kind:

In `src/traider/cli.py` (1 of 6), replace:

```python
from traider.research.run import RunDeps, run_premarket
```

with:

```python
from traider.research.run import RunDeps, RunOutcome, run_premarket
from traider.research.scorecard_run import run_scorecard
```

In `src/traider/cli.py` (2 of 6), replace:

```python
RESEARCH_KINDS = ("premarket",)  # the other kinds come in C2
```

with:

```python
RESEARCH_KINDS = ("premarket", "scorecard")
```

In `src/traider/cli.py` (3 of 6), replace:

```python
BuildDeps = Callable[..., Awaitable[RunDeps]]
```

with:

```python
SCORECARD_DRY_RUN_NOTICE = (
    "Dry run: real calls to Schwab (daily bars) and reads of the research table and the "
    "bot's event log; no model is called. Nothing is written to the research table (no "
    "outcomes, summary or lock) and no alert is sent. The trail is written to {where}.\n\n"
)

BuildDeps = Callable[..., Awaitable[RunDeps]]
Runner = Callable[..., Awaitable[RunOutcome]]


def _runner(kind: str) -> Runner:
    """Looked up when the run starts, so tests can replace a runner."""
    if kind == "scorecard":
        return run_scorecard
    return run_premarket
```

In `src/traider/cli.py` (4 of 6), replace:

```python
        out.write(f"unknown research kind {kind!r}; only premarket exists so far\n")
        return 2
    clock = SystemClock()
    if dry_run:
        out.write(DRY_RUN_NOTICE.format(where=trail_dir))
```

with:

```python
        out.write(f"unknown research kind {kind!r}; one of {', '.join(RESEARCH_KINDS)}\n")
        return 2
    clock = SystemClock()
    if dry_run:
        notice = SCORECARD_DRY_RUN_NOTICE if kind == "scorecard" else DRY_RUN_NOTICE
        out.write(notice.format(where=trail_dir))
```

In `src/traider/cli.py` (5 of 6), replace:

```python
            outcome = await run_premarket(deps, started, dry_run=dry_run, force=force)
        except Exception as exc:
            # run_premarket records its own failures; this is a bug, so fail closed.
```

with:

```python
            outcome = await _runner(kind)(deps, started, dry_run=dry_run, force=force)
        except Exception as exc:
            # A run records its own failures; this is a bug, so fail closed.
```

In `src/traider/cli.py` (6 of 6), replace:

```python
        "run", help="run a research job now (what the 08:00 schedule runs)"
```

with:

```python
        "run", help="run a research job now (what its schedule runs)"
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `uv run pytest tests/unit/test_research_botstate.py tests/unit/test_research_scorecard_run.py tests/unit/test_cli_research_run.py -q`
Expected: PASS (52 passed).

Run the fingerprint again (Task 4): `uv run python /tmp/premarket_fingerprint.py > /tmp/premarket-after.json 2>/dev/null && cmp /tmp/premarket-before.json /tmp/premarket-after.json && echo IDENTICAL`
Expected: `IDENTICAL`.

- [ ] **Step 5: Break on purpose (restore after each)**

Make each change, run the named tests, see them FAIL, then undo the change exactly.

- **Break 1.** Score the picks of failed runs too. In `src/traider/research/scorecard_run.py`, replace:

  ```python
                  if run is not None and run.status in (RunStatus.OK, RunStatus.PARTIAL):
  ```

  with:

  ```python
                  if run is not None and run.status is not RunStatus.RUNNING:
  ```

  Run: `uv run pytest tests/unit/test_research_scorecard_run.py -q`. These FAIL: `test_a_week_of_picks_is_scored` and nine more.

- **Break 2.** Score final picks again. In `src/traider/research/scorecard_run.py`, replace:

  ```python
      return outcome is None or outcome.status is not OutcomeStatus.FINAL
  ```

  with:

  ```python
      return True
  ```

  Run: `uv run pytest tests/unit/test_research_scorecard_run.py -q`. These FAIL: `test_final_outcomes_are_skipped_and_still_summarised`.

- **Break 3.** Guess the bot's namespace. In `src/traider/research/wiring.py`, replace:

  ```python
      if not config.state_namespace:
          raise SetupError(
  ```

  with:

  ```python
      if False:
          raise SetupError(
  ```

  Run: `uv run pytest tests/unit/test_research_botstate.py -q`. These FAIL: `test_a_state_table_without_a_namespace_is_refused`.

- [ ] **Step 6: Whole suite, lint, types**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: all green: 2187 passed, 6 skipped.

- [ ] **Step 7: Commit**

```bash
git add src/traider/config.py src/traider/research/botstate.py src/traider/research/run.py \
  src/traider/research/scorecard_run.py src/traider/research/wiring.py src/traider/cli.py \
  tests/unit/test_research_botstate.py tests/unit/test_research_scorecard_run.py tests/unit/test_cli_research_run.py
git commit -m "feat(research): the daily scorecard run

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Xh7zur22CRYEGaDoTB5nkV"
```

---

### Task 6: The intraday run, its own Schwab limiter, and `--kind intraday`

**Files:**
- Create: `src/traider/research/intraday.py`
- Modify: `src/traider/research/dive.py`, `src/traider/research/run.py`, `src/traider/research/wiring.py`, `src/traider/cli.py`
- Test: `tests/unit/test_research_intraday.py` (new); `tests/unit/test_cli_research_run.py`: `fake_build` takes `kind`, `test_the_command_exists_and_takes_only_premarket` becomes `test_the_command_exists_and_takes_the_three_kinds` (it asserted that `--kind intraday` was refused), intraday tests appended

**Interfaces:**
- Consumes: `_Run`, `RunDeps`, `RunOutcome`, `Snapshot`, `run_locked`, `new_run_id`, `CONTEXT_SYMBOLS`, `HISTORY_DAYS`, `MOVER_INDEXES`, `MOVER_SORTS`, `EMPTY_CALENDAR_WEEKDAYS`, `_events_for`, `_stale` (Task 4); `held_symbols`, `RunDeps.state` (Task 5); `IntradaySettings`, `dive.intraday_model`, `budget.intraday_run_usd` (Task 1); `code_posture`, `posture_metrics`, `stricter`, `PostureDecision`, `REASON_MAX_CHARS`; `rank_and_validate`, `RankInput`, `RankResult`; `build_candidates`.
- Produces:
  - `traider.research.dive.INTRADAY_LINE` and `DiveContext.intraday: bool = False` (the first message then says "during the session" and adds the line; the pre-market message is unchanged).
  - `_Run` hooks: `intraday_dives: ClassVar[bool] = False`, `screen_settings() -> ScreenSettings`, `dive_settings() -> DiveSettings`, `alert_wanted(status, posture, picks) -> bool` (all as before for pre-market); `RunBase.commit(..., send: bool = True)`.
  - `traider.research.intraday`: `KIND = "intraday"`, `START_GRACE_S = 300`, `NoBotState`, `latest_ok_posture(research: DayResearch) -> Posture | None`, `async run_intraday(deps, now, *, dry_run=False, force=False) -> RunOutcome`, `class IntradayRun(_Run)` (`kind = lock_name = "intraday"`, `title = "Research intraday"`, `time_limit()` = `research_jobs.intraday.max_run_s`, `models()` = `(dive.intraday_model,)`, `tighten(snapshot, base) -> PostureDecision`, `intraday_only(symbol, assessment) -> Assessment`, `collect_intraday(*, picked, held) -> Snapshot`).
  - `traider.research.wiring.INTRADAY_SCHWAB_MAX_PER_MINUTE = 20`; `build_deps(..., kind: str = "premarket", ...)` (20 a minute for intraday; `SetupError` for intraday without a state table).
  - `traider.cli.RESEARCH_KINDS = ("premarket", "intraday", "scorecard")`, `INTRADAY_DRY_RUN_NOTICE`; `build(...)` is called with `kind=`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_research_intraday.py`:

```python
"""The intraday run end to end, on fakes: a morning posture, the bot's ledger, the movers
at 11:00, then every way it can go wrong. Nothing here touches Schwab, Finnhub, Bedrock
or AWS."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

import traider.research.run as run_module
from tests.fakes.research import (
    PLTR_LONG,
    ScriptedLLM,
    market_day,
    submit,
)
from tests.unit.test_research_run import StepClock, Trails
from traider.alerts import LogAlerter
from traider.config import ResearchSettings
from traider.research.dive import INTRADAY_LINE
from traider.research.intraday import IntradayRun, latest_ok_posture, run_intraday
from traider.research.job_settings import DEFAULT_MODEL
from traider.research.models import Pick, Posture, PostureLevel, RunMeta
from traider.research.rank import close_of
from traider.research.run import LOCK_SPARE_S, RUN_BOX_MARGIN_S, RunDeps
from traider.research.source import ResearchSource
from traider.research.store import MemoryResearchStore
from traider.settings import Settings
from traider.state.base import LedgerEntry
from traider.state.memory import MemoryStateStore
from traider.timeutil import ManualClock

NOW = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)  # Friday 11:00 New York
DAY = "2026-10-09"
MORNING = datetime(2026, 10, 9, 12, 10, tzinfo=UTC)
PLTR_SWING = PLTR_LONG | {"horizon": "swing", "swing_days": 3}


def morning_meta(run_id="premarket-m", status="ok", kind="premarket") -> RunMeta:
    return RunMeta(
        run_id=run_id,
        kind=kind,
        status=status,
        started_at=MORNING,
        finished_at=MORNING,
        trading_day=datetime(2026, 10, 9).date(),
    )


def morning_pick(symbol="NVDA", run_id="premarket-m") -> Pick:
    return Pick(
        run_id=run_id,
        rank=1,
        symbol=symbol,
        side="long",
        horizon="swing",
        score=84,
        pre_score=89,
        thesis="t",
        invalidation=Decimal(101),
        expires_at=close_of(datetime(2026, 10, 16).date()),
    )


async def morning(store, level="trade", status="ok", run_id="premarket-m") -> None:
    """The pre-market run: NVDA picked, and the day's posture."""
    posture = Posture(level=level, reasons=("code: x",), run_id=run_id, at=MORNING)
    await store.write_run(morning_meta(run_id, status), [morning_pick(run_id=run_id)], posture)


async def ledger(*symbols) -> MemoryStateStore:
    state = MemoryStateStore("paper")
    for symbol in symbols:
        await state.put_ledger(LedgerEntry(symbol, horizon="swing", side="long", opened_at=MORNING))
    return state


async def deps(
    *,
    store=None,
    llm=None,
    market=None,
    settings=None,
    state=None,
    monotonic=None,
    level="trade",
) -> RunDeps:
    """By default: the morning said trade and picked NVDA, the bot holds AMD, MSFT is
    pinned. Of the movers, PLTR alone survives the screen."""
    if store is None:
        store = MemoryResearchStore()
        await morning(store, level)
    day_market, events = market_day()
    return RunDeps(
        store=store,
        market=market or day_market,
        events=events,
        llm=llm or ScriptedLLM(dives={"PLTR": [submit(**PLTR_SWING)]}),
        trail=Trails(),
        alerts=LogAlerter(),
        settings=settings or Settings(pinned_symbols=("MSFT",)),
        clock=ManualClock(NOW),
        monotonic=monotonic or (lambda: 0.0),
        state=state if state is not None else await ledger("AMD"),
    )


async def bot_view(store, now=NOW):
    source = ResearchSource(store, ResearchSettings)
    await source.refresh(now)
    return source.view


# --- the golden mid-morning ----------------------------------------------------------------


async def test_a_mid_morning_run_adds_only_new_names_as_intraday_picks():
    d = await deps()
    outcome = await run_intraday(d, NOW)
    assert (outcome.status, outcome.exit_code) == ("ok", 0)
    assert outcome.run_id.startswith("intraday-20261009T150000Z-")
    (pltr,) = outcome.picks
    assert (pltr.symbol, pltr.side.value, pltr.horizon.value) == ("PLTR", "long", "intraday")
    assert pltr.expires_at == datetime(2026, 10, 9, 20, 0, tzinfo=UTC)  # today's close
    assert outcome.posture.level is PostureLevel.TRADE
    assert outcome.posture.reasons == (
        "intraday: at least trade, from premarket-m",
        "code: no rule matched",
    )
    counts = outcome.meta.counts
    assert (counts["excluded_picked"], counts["excluded_held"], counts["excluded_pinned"]) == (
        1,
        1,
        1,
    )
    assert counts["coerced_to_intraday"] == 1
    assert "PLTR: a swing idea was made intraday" in outcome.meta.notes
    snapshot = d.trail.only().files["snapshot.json"]
    assert {"NVDA", "AMD", "MSFT"}.isdisjoint(snapshot["candidates"])
    assert (outcome.meta.kind, outcome.meta.models) == ("intraday", (DEFAULT_MODEL,))
    (request,) = d.llm.requests
    assert request["model"] == DEFAULT_MODEL
    intro = request["messages"][0]["content"]
    assert "during the session" in intro and INTRADAY_LINE in intro
    ((event, subject, message),) = d.alerts.sent
    assert (event, subject) == ("research_run_ok", f"Research intraday {DAY}: ok")
    assert message.startswith(f"traider research intraday {DAY}: posture trade")
    assert ("LOCK#intraday", "LOCK") not in d.store.keys
    view = await bot_view(d.store)
    assert sorted(view.picks) == ["NVDA", "PLTR"]
    assert view.posture.run_id == outcome.run_id  # the newest posture


async def test_it_uses_the_intraday_model_and_budget():
    haiku = "anthropic.claude-haiku-5"
    settings = Settings(
        pinned_symbols=("MSFT",),
        research_jobs={
            "dive": {"intraday_model": haiku},
            "budget": {
                "intraday_run_usd": "0.001",
                "prices": {
                    haiku: {"in_per_mtok": "1", "out_per_mtok": "5"},
                    DEFAULT_MODEL: {"in_per_mtok": "2", "out_per_mtok": "10"},
                },
            },
        },
    )
    d = await deps(settings=settings)
    outcome = await run_intraday(d, NOW)
    assert outcome.meta.models == (haiku,)
    assert d.llm.requests == []  # the budget refused the dive before any call
    assert outcome.status == "partial"
    assert "budget reached: 1 deep-dive(s) stopped" in outcome.meta.notes


# --- it never rescues a day ---------------------------------------------------------------


@pytest.mark.parametrize("status", ["partial", "failed", "running"])
async def test_without_an_ok_posture_today_it_writes_nothing(status):
    store = MemoryResearchStore()
    await morning(store, status=status)
    d = await deps(store=store)
    before = set(store.keys)
    outcome = await run_intraday(d, NOW)
    assert (outcome.status, outcome.exit_code, outcome.detail) == (
        "skipped",
        0,
        "no ok posture today",
    )
    assert set(store.keys) == before
    assert d.llm.requests == [] and d.alerts.sent == []
    assert d.market.called("quotes") == []


async def test_an_empty_morning_is_skipped_too():
    d = await deps(store=MemoryResearchStore())
    assert (await run_intraday(d, NOW)).status == "skipped"


async def test_an_unreadable_posture_item_means_no_posture():
    store = MemoryResearchStore()
    await morning(store)
    store.put_raw(f"DAY#{DAY}", "POSTURE#2026-10-09T13:00:00+00:00", "{not json")
    d = await deps(store=store)
    assert (await run_intraday(d, NOW)).status == "skipped"


async def test_the_latest_ok_posture_is_the_one_it_starts_from():
    store = MemoryResearchStore()
    await morning(store, "trade")
    later = Posture(
        level="reduced",
        run_id="intraday-a",
        at=MORNING.replace(hour=14),
    )
    await store.write_run(morning_meta("intraday-a", kind="intraday"), [], later)
    newest_but_partial = Posture(level="trade", run_id="intraday-b", at=MORNING.replace(hour=15))
    await store.write_run(
        morning_meta("intraday-b", status="partial", kind="intraday"), [], newest_but_partial
    )
    found = latest_ok_posture(await store.day(DAY))
    assert (found.run_id, found.level) == ("intraday-a", PostureLevel.REDUCED)


async def test_force_never_skips_the_morning_posture_check():
    d = await deps(store=MemoryResearchStore())
    assert (await run_intraday(d, NOW, force=True)).status == "skipped"


# --- when it runs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "status"),
    [
        (datetime(2026, 10, 9, 13, 0, tzinfo=UTC), "closed"),  # 09:00, before the open
        (datetime(2026, 10, 9, 20, 0, tzinfo=UTC), "closed"),  # 16:00, the close
        (datetime(2026, 10, 9, 19, 4, tzinfo=UTC), "ok"),  # 15:04, inside the start grace
        (datetime(2026, 10, 9, 19, 6, tzinfo=UTC), "skipped"),  # 15:06, after last_start
    ],
)
async def test_it_runs_only_in_the_session_and_until_last_start(now, status):
    d = await deps()
    d.clock = ManualClock(now)
    assert (await run_intraday(d, now)).status == status


async def test_force_starts_after_last_start():
    late = datetime(2026, 10, 9, 19, 30, tzinfo=UTC)
    d = await deps()
    assert (await run_intraday(d, late, force=True)).status == "ok"


async def test_a_holiday_is_closed():
    d = await deps()
    d.market.open_today = False
    assert (await run_intraday(d, NOW)).status == "closed"


async def test_switched_off_means_no_run():
    for jobs in ({"enabled": False}, {"intraday": {"enabled": False}}):
        d = await deps(settings=Settings(research_jobs=jobs))
        assert (await run_intraday(d, NOW)).status == "disabled"


async def test_its_own_lock_and_only_its_own():
    d = await deps()
    await d.store.acquire_lock("intraday", "someone", 3600, NOW)
    assert (await run_intraday(d, NOW)).exit_code == 2
    d = await deps()
    await d.store.acquire_lock("premarket", "someone", 3600, NOW)
    assert (await run_intraday(d, NOW)).status == "ok"


async def test_its_time_box_follows_intraday_max_run_s():
    d = await deps()
    loop = asyncio.get_running_loop()
    run = IntradayRun(d, NOW, "intraday-x", dry_run=False)
    limit = Settings().research_jobs.intraday.max_run_s
    assert run.max_run_s == limit == 600
    box = run.box - loop.time()
    edge = limit + LOCK_SPARE_S - RUN_BOX_MARGIN_S
    assert edge - 1 < box <= edge


async def test_past_the_deadline_before_the_screen_it_writes_the_posture_only():
    d = await deps(monotonic=StepClock([0.0], then=1e9))
    outcome = await run_intraday(d, NOW)
    assert (outcome.status, outcome.picks) == ("partial", ())
    assert "deadline passed before the screen: no deep-dives" in outcome.meta.notes
    assert d.llm.requests == []


async def test_a_run_past_its_box_fails(monkeypatch):
    class Hanging(ScriptedLLM):
        async def create(self, **request):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        run_module, "_run_deadline", lambda max_run_s: asyncio.get_running_loop().time() + 0.3
    )
    d = await deps(llm=Hanging())
    outcome = await asyncio.wait_for(run_intraday(d, NOW), 5)
    assert outcome.status == "failed"
    assert outcome.meta.error == "dive: RunDeadline: the run did not finish within 1140s"
    assert ("LOCK#intraday", "LOCK") not in d.store.keys


async def test_a_dry_run_writes_nothing():
    d = await deps()
    before = set(d.store.keys)
    outcome = await run_intraday(d, NOW, dry_run=True)
    assert outcome.status == "ok" and [p.symbol for p in outcome.picks] == ["PLTR"]
    assert set(d.store.keys) == before and d.alerts.sent == []


async def test_without_the_bots_ledger_it_fails_closed():
    d = await deps()
    d.state = None
    outcome = await run_intraday(d, NOW)
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    assert outcome.meta.error.startswith("held: NoBotState")
    assert outcome.picks == ()


async def test_an_unreadable_ledger_fails_the_run():
    class Broken(MemoryStateStore):
        async def ledger(self):
            raise RuntimeError("throttled")

    d = await deps(state=Broken())
    assert (await run_intraday(d, NOW)).status == "failed"


# --- the posture only tightens -------------------------------------------------------------


async def test_a_calm_market_never_loosens_a_reduced_morning():
    d = await deps(level="reduced")
    outcome = await run_intraday(d, NOW)
    assert outcome.posture.level is PostureLevel.REDUCED
    assert outcome.posture.reasons[0] == "intraday: at least reduced, from premarket-m"


async def test_a_stand_aside_morning_stays_stand_aside_with_no_dives():
    d = await deps(level="stand_aside")
    outcome = await run_intraday(d, NOW)
    assert (outcome.posture.level, outcome.picks) == (PostureLevel.STAND_ASIDE, ())
    assert d.llm.requests == []
    assert d.alerts.sent == []  # nothing new to say


async def test_a_vix_spike_tightens_the_day_and_says_so():
    d = await deps()
    d.market.quote_map["$VIX"] = d.market.quote_map["$VIX"].model_copy(update={"last": 40.0})
    outcome = await run_intraday(d, NOW)
    assert (outcome.posture.level, outcome.picks) == (PostureLevel.STAND_ASIDE, ())
    assert d.llm.requests == []
    ((_, _, message),) = d.alerts.sent
    assert "posture stand_aside (vix 40.0); 0 picks" in message
    assert (await bot_view(d.store)).level is PostureLevel.STAND_ASIDE


async def test_missing_market_data_means_stand_aside():
    d = await deps()
    del d.market.quote_map["$VIX"]
    outcome = await run_intraday(d, NOW)
    assert outcome.posture.level is PostureLevel.STAND_ASIDE


async def test_no_new_picks_and_no_change_means_no_alert_but_a_posture_all_the_same():
    d = await deps(llm=ScriptedLLM(dives={"PLTR": [submit(**(PLTR_LONG | {"side": "pass"}))]}))
    outcome = await run_intraday(d, NOW)
    assert (outcome.status, outcome.picks) == ("ok", ())
    assert d.alerts.sent == []
    stored = [k for k in d.store.keys if k[1].startswith("POSTURE#2026-10-09T15")]
    assert stored  # written even though unchanged


def test_the_intraday_run_never_imports_order_code():
    from pathlib import Path

    from traider.research import intraday

    source = Path(intraday.__file__).read_text(encoding="utf-8")
    assert "place_order" not in source and "broker" not in source


async def test_rescue_check_reads_only_todays_runs():
    store = MemoryResearchStore()
    yesterday = Posture(level="trade", run_id="premarket-y", at=MORNING.replace(day=8))
    meta = morning_meta("premarket-y").model_copy(
        update={"trading_day": datetime(2026, 10, 8).date()}
    )
    await store.write_run(meta, [], yesterday)
    d = await deps(store=store)
    assert (await run_intraday(d, NOW)).status == "skipped"
```

In `tests/unit/test_cli_research_run.py` (1 of 4), replace:

```python
    async def build(config, http, *, dry_run, trail_dir, clock):
```

with:

```python
    async def build(config, http, *, kind, dry_run, trail_dir, clock):
        made["kind"] = kind
```

In `tests/unit/test_cli_research_run.py` (2 of 4), replace:

```python
def test_the_command_exists_and_takes_only_premarket(capsys):
```

with:

```python
def test_the_command_exists_and_takes_the_three_kinds(capsys):
```

In `tests/unit/test_cli_research_run.py` (3 of 4), replace:

```python
    with pytest.raises(SystemExit) as exit_:
        cli.main(["research", "run", "--kind", "intraday"])
```

with:

```python
    for kind in ("premarket", "intraday", "scorecard"):
        assert cli._parser().parse_args(["research", "run", "--kind", kind]).kind == kind
    with pytest.raises(SystemExit) as exit_:
        cli.main(["research", "run", "--kind", "weekly"])
```

Append to the end of `tests/unit/test_cli_research_run.py` (4 of 4), after two blank lines:

```python
# --- C2a: intraday -------------------------------------------------------------------------


async def test_an_intraday_dry_run_says_what_it_costs_and_tells_the_wiring_its_kind(tmp_path):
    build, made = fake_build()
    out = io.StringIO()
    code = await cli.research_run(
        CONFIG,
        out,
        kind="intraday",
        dry_run=True,
        force=False,
        trail_dir=str(tmp_path),
        build=build,
        now=NOW,
    )
    assert code == 0
    assert made["kind"] == "intraday"
    notice, _, printed = out.getvalue().partition("\n\n")
    assert notice.startswith("Dry run: real calls to Schwab, Finnhub and Claude")
    assert "intraday_run_usd" in notice and "an ok posture today" in notice
    assert json.loads(printed)["status"] == "closed"  # 08:00 New York: before the session


async def test_without_the_bots_state_table_an_intraday_run_cannot_start(aws_stack, tmp_path):
    import aiohttp

    async with aiohttp.ClientSession() as http:
        with pytest.raises(SetupError, match="TRAIDER_STATE_TABLE and TRAIDER_STATE_NAMESPACE"):
            await build_deps(
                stack_config(aws_stack),
                http,
                kind="intraday",
                dry_run=False,
                trail_dir=tmp_path,
                clock=ManualClock(NOW),
                llm=golden_llm(),
            )


async def test_the_intraday_wiring_reads_the_bots_state_at_a_lower_rate(aws_stack, tmp_path):
    import aiohttp

    from tests.unit.test_state import TABLE as STATE_TABLE
    from tests.unit.test_state import make_table as make_state_table
    from traider.research.wiring import INTRADAY_SCHWAB_MAX_PER_MINUTE

    make_state_table()
    config = stack_config(aws_stack, state_table=STATE_TABLE, state_namespace="paper")
    async with aiohttp.ClientSession() as http:
        deps = await build_deps(
            config,
            http,
            kind="intraday",
            dry_run=False,
            trail_dir=tmp_path,
            clock=ManualClock(NOW),
            llm=golden_llm(),
        )
        assert deps.state is not None and await deps.state.ledger() == {}
        assert deps.market._client._limiter._max == INTRADAY_SCHWAB_MAX_PER_MINUTE == 20
        premarket = await build_deps(
            config,
            http,
            dry_run=False,
            trail_dir=tmp_path,
            clock=ManualClock(NOW),
            llm=golden_llm(),
        )
        assert premarket.market._client._limiter._max == RESEARCH_SCHWAB_MAX_PER_MINUTE
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_research_intraday.py tests/unit/test_cli_research_run.py tests/unit/test_research_run.py tests/unit/test_research_dive.py -q`
Expected: FAIL: collection error (1 error), `ImportError: cannot import name 'INTRADAY_LINE' from 'traider.research.dive'`. (The CLI tests would fail too: `fake_build` now needs `kind`, which the CLI does not pass yet.)

- [ ] **Step 3: Implement**

A dive can be told it is intraday:

In `src/traider/research/dive.py` (1 of 2), replace:

```text
    market_context: Mapping[str, Any]

```

with:

```python
    market_context: Mapping[str, Any]
    # An intraday run's dive: during the session, and flat by today's close.
    intraday: bool = False


INTRADAY_LINE = "This is an intraday idea; it must be flat by today's close.\n"
```

In `src/traider/research/dive.py` (2 of 2), replace:

```python
    return (
        f"Symbol: {ctx.symbol}\n"
        f"Today: {ctx.today.isoformat()}, before the open.\n"
        f"Quote: last {q.last}, previous close {q.prev_close}.\n"
        f"Screen features: {features}\n"
        "Study it with the tools, then call submit_assessment."
```

with:

```python
    when = "during the session" if ctx.intraday else "before the open"
    return (
        f"Symbol: {ctx.symbol}\n"
        f"Today: {ctx.today.isoformat()}, {when}.\n"
        f"Quote: last {q.last}, previous close {q.prev_close}.\n"
        f"Screen features: {features}\n"
        + (INTRADAY_LINE if ctx.intraday else "")
        + "Study it with the tools, then call submit_assessment."
```

Three hooks on `_Run` (the pre-market answers are what it did before) and an optional alert in `commit`:

In `src/traider/research/run.py` (1 of 11), replace:

```python
    Profile,
)
```

with:

```python
    Profile,
)
from traider.research.job_settings import DiveSettings, ScreenSettings
```

In `src/traider/research/run.py` (2 of 11), replace:

```python
    ) -> RunOutcome:
        """Record a finished run: the day's cost first, then ``write`` (which writes META
        last), then the alert. A dry run does none of it."""
```

with:

```python
        send: bool = True,
    ) -> RunOutcome:
        """Record a finished run: the day's cost first, then ``write`` (which writes META
        last), then the alert unless ``send`` is false. A dry run does none of it."""
```

In `src/traider/research/run.py` (3 of 11), replace:

```python
            await self.alert(outcome.meta.status, message, event=event)
```

with:

```python
            if send:
                await self.alert(outcome.meta.status, message, event=event)
```

In `src/traider/research/run.py` (4 of 11), replace:

```python
    """The pre-market run."""
```

with:

```python
    """The pre-market run. The intraday run reuses its stages, through the hooks below."""
```

In `src/traider/research/run.py` (5 of 11), replace:

```python
    title: ClassVar[str] = "Research"
```

with:

```python
    title: ClassVar[str] = "Research"
    intraday_dives: ClassVar[bool] = False  # tells the model it is an intraday idea
```

In `src/traider/research/run.py` (6 of 11), replace:

```python
        return tuple(dict.fromkeys((dive.posture_model, dive.model)))
```

with:

```python
        return tuple(dict.fromkeys((dive.posture_model, dive.model)))

    def screen_settings(self) -> ScreenSettings:
        return self.jobs.screen

    def dive_settings(self) -> DiveSettings:
        return self.jobs.dive

    def alert_wanted(self, status: RunStatus, posture: Posture, picks: Sequence[Pick]) -> bool:
        return True
```

In `src/traider/research/run.py` (7 of 11), replace:

```python
        settings = jobs.screen
```

with:

```python
        settings = self.screen_settings()
```

In `src/traider/research/run.py` (8 of 11), replace:

```python
        deps, jobs = self.deps, self.jobs
        assert self.meter is not None
        meter = self.meter
        gate = asyncio.Semaphore(jobs.dive.dive_concurrency)
```

with:

```python
        deps = self.deps
        assert self.meter is not None
        meter = self.meter
        settings = self.dive_settings()
        gate = asyncio.Semaphore(settings.dive_concurrency)
```

In `src/traider/research/run.py` (9 of 11), replace:

```text
                    market_context=context,
```

with:

```text
                    market_context=context,
                    intraday=self.intraday_dives,
```

In `src/traider/research/run.py` (10 of 11), replace:

```python
                    settings=jobs.dive,
                )
                await self.put_trail(f"dives/{row.symbol}.json", result.trail(jobs.dive.model))
```

with:

```python
                    settings=settings,
                )
                await self.put_trail(f"dives/{row.symbol}.json", result.trail(settings.model))
```

In `src/traider/research/run.py` (11 of 11), replace:

```python
            outcome, write, _summary(self.kind, self.today, posture, ranked.picks, meta)
```

with:

```text
            outcome,
            write,
            _summary(self.kind, self.today, posture, ranked.picks, meta),
            send=self.alert_wanted(status, posture, ranked.picks),
```

Create `src/traider/research/intraday.py`:

```python
"""The intraday run, every 30 minutes from 10:00 to 15:00 New York time on weekdays.

    lock(intraday) -> session open now? -> after last_start? -> an ok posture today?
      -> META running -> collect (context quotes, SPY bars, movers, the earnings calendar)
      -> posture = stricter(today's latest ok posture, code rules on current metrics)
      -> stand_aside? yes -> write the posture, no picks, ok
      -> candidates = movers - picked today - held - pinned -> screen -> top K
      -> deep-dives (intraday_model, intraday_run_usd) -> rank (every pick intraday)
      -> write picks + posture + META -> alert if there are picks, the posture tightened,
         or the run is partial

It never rescues a day: without an ok posture today (from the morning run, or an earlier
intraday run) it writes nothing and exits ``skipped``. Its posture can only be stricter
than the one it starts from, and there is no model review of it. It always writes the
posture, changed or not, so the day's record is continuous. A swing idea is made
intraday (with a note): every pick expires at today's close. "Held" is the bot's ledger,
read-only; without it the run fails.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final

from traider.research.botstate import held_symbols
from traider.research.dive import Assessment
from traider.research.events import EarningsEvent, EventsUnavailable
from traider.research.job_settings import DiveSettings, ScreenSettings
from traider.research.models import (
    Pick,
    Posture,
    PostureLevel,
    RunKind,
    RunStatus,
)
from traider.research.posture import (
    REASON_MAX_CHARS,
    PostureDecision,
    code_posture,
    posture_metrics,
    stricter,
)
from traider.research.rank import RankInput, RankResult, rank_and_validate
from traider.research.run import (
    CONTEXT_SYMBOLS,
    EMPTY_CALENDAR_WEEKDAYS,
    EXIT_OK,
    HISTORY_DAYS,
    MOVER_INDEXES,
    MOVER_SORTS,
    RunDeps,
    RunOutcome,
    Snapshot,
    _events_for,
    _Run,
    _stale,
    new_run_id,
    run_locked,
)
from traider.research.screen import build_candidates
from traider.research.scrub import scrub
from traider.research.store import DayResearch
from traider.timeutil import ET, weekdays_between

log = logging.getLogger(__name__)

KIND: Final = "intraday"
# A scheduled start lands a little after its time (the task takes a minute or two to
# start), so last_start allows this much.
START_GRACE_S: Final = 300


class NoBotState(Exception):
    """An intraday run cannot tell what the bot holds."""


async def run_intraday(
    deps: RunDeps, now: datetime, *, dry_run: bool = False, force: bool = False
) -> RunOutcome:
    """One intraday run. ``dry_run`` makes every call but writes nothing to the research
    table and sends no alert. ``force`` (and a dry run) ignores ``last_start``; nothing
    ignores the need for an ok posture today."""
    now = now.astimezone(UTC)
    run_id = new_run_id(now, KIND)
    jobs = deps.settings.research_jobs
    if not jobs.enabled or not jobs.intraday.enabled:
        log.info("intraday research is switched off; nothing to do")
        detail = "research_jobs.enabled or research_jobs.intraday.enabled is false"
        return RunOutcome("disabled", EXIT_OK, run_id, detail=detail)
    return await run_locked(IntradayRun(deps, now, run_id, dry_run=dry_run), force=force)


def latest_ok_posture(research: DayResearch) -> Posture | None:
    """Today's newest posture from an ok run, or None. A day with an unreadable posture
    item has none: that item may be the newest."""
    if research.invalid_postures:
        return None
    usable = [
        p
        for p in research.postures
        if (run := research.runs.get(p.run_id)) is not None and run.status is RunStatus.OK
    ]
    return max(usable, key=lambda p: p.at) if usable else None


class IntradayRun(_Run):
    kind: ClassVar[RunKind] = KIND
    lock_name: ClassVar[str] = KIND
    title: ClassVar[str] = "Research intraday"
    intraday_dives: ClassVar[bool] = True

    def __init__(self, deps: RunDeps, now: datetime, run_id: str, *, dry_run: bool) -> None:
        super().__init__(deps, now, run_id, dry_run=dry_run)
        self.tightened = False

    def time_limit(self) -> float:
        return self.jobs.intraday.max_run_s

    def models(self) -> tuple[str, ...]:
        return (self.jobs.dive.intraday_model,)

    def screen_settings(self) -> ScreenSettings:
        return self.jobs.screen.model_copy(
            update={"deep_dive_count": self.jobs.intraday.deep_dive_count}
        )

    def dive_settings(self) -> DiveSettings:
        return self.jobs.dive.model_copy(update={"model": self.jobs.dive.intraday_model})

    def alert_wanted(self, status: RunStatus, posture: Posture, picks: Sequence[Pick]) -> bool:
        return bool(picks) or self.tightened or status is RunStatus.PARTIAL

    def after_last_start(self) -> bool:
        last = datetime.combine(self.today, self.jobs.intraday.last_start, tzinfo=ET)
        return self.now > last + timedelta(seconds=START_GRACE_S)

    async def _execute(self, *, force: bool) -> RunOutcome:
        deps, day = self.deps, self.today.isoformat()
        self.stage = "market_hours"
        session = await deps.market.market_session(self.today)
        if session.open is None or session.close is None:
            log.info("no regular session on %s; nothing to research", day)
            return RunOutcome("closed", EXIT_OK, self.run_id, detail=f"market closed on {day}")
        if not session.open <= self.now < session.close:
            log.info("the session is not open; nothing to research")
            return RunOutcome("closed", EXIT_OK, self.run_id, detail="the session is not open")
        if not force and self.after_last_start():
            last = self.jobs.intraday.last_start.strftime("%H:%M")
            log.info("after research_jobs.intraday.last_start (%s); skipping", last)
            return RunOutcome(
                "skipped", EXIT_OK, self.run_id, detail=f"after last_start ({last} New York)"
            )
        self.stage = "morning_posture"
        research = await deps.store.day(day)
        base = latest_ok_posture(research)
        if base is None:
            # A failed morning means standing aside all day: code rules alone never
            # rescue it.
            log.info("no ok posture today; an intraday run never rescues the day")
            return RunOutcome("skipped", EXIT_OK, self.run_id, detail="no ok posture today")
        await self.begin(self.jobs.budget.intraday_run_usd)

        self.stage = "held"
        if deps.state is None:
            raise NoBotState("no state table: the run cannot tell what the bot holds")
        held = await held_symbols(deps.state)
        picked = {p.symbol for p in research.picks}

        self.stage = "collect"
        snapshot = await self.collect_intraday(picked=picked, held=held)
        await self.put_trail("snapshot.json", snapshot)

        self.stage = "posture"
        decision = self.tighten(snapshot, base)
        posture = Posture(
            level=decision.level,
            reasons=tuple(scrub(r, REASON_MAX_CHARS) for r in decision.reasons),
            run_id=self.run_id,
            at=self.now,
            metrics=decision.metrics.as_dict(),
        )
        await self.put_trail(
            "posture.json", {"posture": posture, "from": base, "notes": list(decision.notes)}
        )
        if decision.level is PostureLevel.STAND_ASIDE:
            self.stage = "write"
            return await self.finish(posture, RankResult((), ()), {})
        if deps.monotonic() >= self.started + self.max_run_s:
            self.note("deadline passed before the screen: no deep-dives", partial=True)
            self.stage = "write"
            return await self.finish(posture, RankResult((), ()), {})

        self.stage = "screen"
        top, quotes, bars, profiles = await self.screen(snapshot)

        self.stage = "dive"
        results = await self.dive(
            snapshot, decision=decision, top=top, quotes=quotes, bars=bars, profiles=profiles
        )

        self.stage = "rank"
        by_symbol = {row.symbol: row for row in top}
        inputs = [
            RankInput(
                symbol=r.symbol,
                assessment=self.intraday_only(r.symbol, r.assessment),
                pre_score=by_symbol[r.symbol].pre_score,
                features=by_symbol[r.symbol].features.as_dict(),
                atr=by_symbol[r.symbol].atr,
                sector=p.industry if (p := profiles.get(r.symbol)) else None,
                earnings=_events_for(snapshot.earnings, r.symbol),
            )
            for r in results
            if r.assessment is not None
        ]
        self.counts["assessed"] = len(inputs)
        ranked = await rank_and_validate(
            inputs,
            market=deps.market,
            run_id=self.run_id,
            today=self.today,
            close=session.close,
            earnings_ok=snapshot.earnings_ok,
            calendar_end=self.calendar_end,
            settings=self.jobs.rank,
        )
        if ranked.chain_failures:
            self.counts["chain_failures"] = ranked.chain_failures
            self.note(
                f"put chain reads failed for {ranked.chain_failures} name(s); counted as illiquid"
            )
        self.stage = "write"
        assessments = {i.symbol: i.assessment.model_dump(mode="json") for i in inputs}
        return await self.finish(posture, ranked, assessments)

    def intraday_only(self, symbol: str, assessment: Assessment) -> Assessment:
        """Every intraday pick is flat by today's close: a swing idea is made intraday."""
        if assessment.horizon != "swing":
            return assessment
        self.counts["coerced_to_intraday"] = self.counts.get("coerced_to_intraday", 0) + 1
        self.note(f"{symbol}: a swing idea was made intraday")
        return assessment.model_copy(update={"horizon": "intraday", "swing_days": None})

    def tighten(self, snapshot: Snapshot, base: Posture) -> PostureDecision:
        """The stricter of the posture the run starts from and the code rules on the
        market now. No model review, so it can only tighten."""
        spy_bars = snapshot.spy_bars
        notes: list[str] = []
        if spy_bars and _stale(spy_bars, self.today):
            note = f"SPY daily history is stale (last bar {spy_bars[-1].day.isoformat()})"
            self.note(note)
            notes.append(note)
            spy_bars = []
        metrics = posture_metrics(snapshot.context, spy_bars)
        code_level, code_reasons = code_posture(metrics, self.today, self.jobs.posture)
        level = stricter(base.level, code_level)
        self.tightened = level is not base.level
        reasons = [
            f"intraday: at least {base.level.value}, from {base.run_id}",
            *(f"code: {r}" for r in code_reasons),
        ]
        return PostureDecision(level, tuple(reasons), metrics, notes=tuple(notes))

    async def collect_intraday(self, *, picked: set[str], held: set[str]) -> Snapshot:
        deps, jobs, today = self.deps, self.jobs, self.today
        context = (await deps.market.quotes(CONTEXT_SYMBOLS)).quotes
        spy_bars = await deps.market.daily_bars("SPY", today, HISTORY_DAYS)
        movers: dict[str, list[str]] = {}
        for index in MOVER_INDEXES:
            for sort in MOVER_SORTS:
                movers[f"{index}:{sort}"] = await deps.market.movers(index, sort)
        earnings: list[EarningsEvent] = []
        earnings_ok = True
        try:
            earnings = await deps.events.earnings_calendar(self.calendar_start, self.calendar_end)
        except EventsUnavailable as exc:
            earnings_ok = False
            self.note(f"earnings calendar unavailable: {exc}", partial=True)
        else:
            span = weekdays_between(self.calendar_start, self.calendar_end) + 1
            if not earnings and span >= EMPTY_CALENDAR_WEEKDAYS:
                earnings_ok = False
                self.note(
                    f"earnings calendar returned nothing for {span} weekdays; treated as "
                    "unavailable",
                    partial=True,
                )
        names = list(dict.fromkeys(name for found in movers.values() for name in found))
        pinned = set(deps.settings.pinned_symbols)
        for reason, skip in (("picked", picked), ("held", held), ("pinned", pinned)):
            if count := sum(1 for n in names if n in skip):
                self.counts[f"excluded_{reason}"] = count
        candidates = build_candidates(
            watchlist=(),
            earnings_names=(),
            movers=names,
            pinned=picked | held | pinned,
            cap=jobs.intraday.max_candidates,
        )
        self.counts["candidates"] = len(candidates)
        return Snapshot(
            taken_at=self.now,
            day=today,
            context=context,
            spy_bars=spy_bars,
            movers=movers,
            earnings_ok=earnings_ok,
            earnings=earnings,
            news_ok=True,
            market_news=[],
            watchlist=[],
            candidates=[c.symbol for c in candidates],
            candidate_sources={c.symbol: list(c.sources) for c in candidates},
        )
```

The wiring takes the kind, and holds intraday runs to 20 Schwab requests a minute:

In `src/traider/research/wiring.py` (1 of 4), replace:

```python
RESEARCH_SCHWAB_MAX_PER_MINUTE = 40
```

with:

```python
RESEARCH_SCHWAB_MAX_PER_MINUTE = 40
# The intraday runs happen while the bot trades: they take a sixth of the quota.
INTRADAY_SCHWAB_MAX_PER_MINUTE = 20
```

In `src/traider/research/wiring.py` (2 of 4), replace:

```python
    *,
```

with:

```python
    *,
    kind: str = "premarket",
```

In `src/traider/research/wiring.py` (3 of 4), replace:

```python
    state = bot_state(config, aws)
```

with:

```python
    state = bot_state(config, aws)
    if kind == "intraday" and state is None:
        raise SetupError(
            "an intraday run needs the bot's state table to know what it holds: set "
            "TRAIDER_STATE_TABLE and TRAIDER_STATE_NAMESPACE"
        )
```

In `src/traider/research/wiring.py` (4 of 4), replace:

```python
    client = SchwabClient(
        http, tokens, base_url=schwab_base_url, max_per_minute=RESEARCH_SCHWAB_MAX_PER_MINUTE
    )
```

with:

```python
    per_minute = (
        INTRADAY_SCHWAB_MAX_PER_MINUTE if kind == "intraday" else RESEARCH_SCHWAB_MAX_PER_MINUTE
    )
    client = SchwabClient(http, tokens, base_url=schwab_base_url, max_per_minute=per_minute)
```

The CLI learns the intraday kind and passes the kind to the wiring:

In `src/traider/cli.py` (1 of 7), replace:

```python
from traider.models import Bar
```

with:

```python
from traider.models import Bar
from traider.research.intraday import run_intraday
```

In `src/traider/cli.py` (2 of 7), replace:

```python
RESEARCH_KINDS = ("premarket", "scorecard")
```

with:

```python
RESEARCH_KINDS = ("premarket", "intraday", "scorecard")
```

In `src/traider/cli.py` (3 of 7), replace:

```python
    "written to {where}.\n\n"
```

with:

```python
    "written to {where}.\n\n"
)

INTRADAY_DRY_RUN_NOTICE = (
    "Dry run: real calls to Schwab, Finnhub and Claude on Amazon Bedrock, and reads of the "
    "research table and the bot's ledger. The Bedrock calls cost real money (at most "
    "research_jobs.budget.intraday_run_usd, $0.75 by default). It needs the session open "
    "and an ok posture today, and ignores research_jobs.intraday.last_start. Nothing is "
    "written to the research table (no picks, posture, cost or lock) and no alert is sent. "
    "The trail is written to {where}.\n\n"
```

In `src/traider/cli.py` (4 of 7), replace:

```python
        return run_scorecard
```

with:

```python
        return run_scorecard
    if kind == "intraday":
        return run_intraday
```

In `src/traider/cli.py` (5 of 7), replace:

```python
        notice = SCORECARD_DRY_RUN_NOTICE if kind == "scorecard" else DRY_RUN_NOTICE
```

with:

```python
        notices = {"scorecard": SCORECARD_DRY_RUN_NOTICE, "intraday": INTRADAY_DRY_RUN_NOTICE}
        notice = notices.get(kind, DRY_RUN_NOTICE)
```

In `src/traider/cli.py` (6 of 7), replace:

```python
                config, http, dry_run=dry_run, trail_dir=Path(trail_dir), clock=clock
```

with:

```python
                config, http, kind=kind, dry_run=dry_run, trail_dir=Path(trail_dir), clock=clock
```

In `src/traider/cli.py` (7 of 7), replace:

```python
        "--force", action="store_true", help="run even if today's run already finished"
```

with:

```text
        "--force",
        action="store_true",
        help="run even if today's run already finished; for intraday, start after "
        "research_jobs.intraday.last_start",
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `uv run pytest tests/unit/test_research_intraday.py tests/unit/test_cli_research_run.py tests/unit/test_research_run.py tests/unit/test_research_dive.py -q`
Expected: PASS (191 passed).

Run the fingerprint once more (Task 4): `uv run python /tmp/premarket_fingerprint.py > /tmp/premarket-after.json 2>/dev/null && cmp /tmp/premarket-before.json /tmp/premarket-after.json && echo IDENTICAL`
Expected: `IDENTICAL`. The pre-market run's dives still start with "before the open" and carry no intraday line. You may now delete `/tmp/premarket_fingerprint.py` and the two JSON files.

- [ ] **Step 5: Break on purpose (restore after each)**

Make each change, run the named tests, see them FAIL, then undo the change exactly.

- **Break 1.** **Spec break:** let intraday loosen the posture. In `src/traider/research/intraday.py`, replace:

  ```python
          level = stricter(base.level, code_level)
  ```

  with:

  ```python
          level = code_level
  ```

  Run: `uv run pytest tests/unit/test_research_intraday.py -q`. These FAIL: `test_a_calm_market_never_loosens_a_reduced_morning`, `test_a_stand_aside_morning_stays_stand_aside_with_no_dives`.

- **Break 2.** **Spec break:** run without a morning posture. In `src/traider/research/intraday.py`, replace:

  ```python
          if base is None:
              # A failed
  ```

  with:

  ```python
          if base is None:
              base = Posture(level=PostureLevel.TRADE, run_id="none", at=self.now)
          if False:
              # A failed
  ```

  Run: `uv run pytest tests/unit/test_research_intraday.py -q`. These FAIL: `test_without_an_ok_posture_today_it_writes_nothing[partial]`, `[failed]`, `[running]`, `test_an_empty_morning_is_skipped_too`, `test_an_unreadable_posture_item_means_no_posture`, `test_force_never_skips_the_morning_posture_check` and one more.

- **Break 3.** Let held names back in. In `src/traider/research/intraday.py`, replace:

  ```text
              pinned=picked | held | pinned,
  ```

  with:

  ```text
              pinned=picked | pinned,
  ```

  Run: `uv run pytest tests/unit/test_research_intraday.py -q`. These FAIL: `test_a_mid_morning_run_adds_only_new_names_as_intraday_picks` and six more.

- **Break 4.** Keep swing ideas. In `src/traider/research/intraday.py`, replace:

  ```python
          if assessment.horizon != "swing":
  ```

  with:

  ```python
          if True:
  ```

  Run: `uv run pytest tests/unit/test_research_intraday.py -q`. These FAIL: `test_a_mid_morning_run_adds_only_new_names_as_intraday_picks`, `test_a_dry_run_writes_nothing`.

- **Break 5.** Treat a missing ledger as holding nothing. In `src/traider/research/intraday.py`, replace:

  ```python
          if deps.state is None:
              raise NoBotState("no state table: the run cannot tell what the bot holds")
          held = await held_symbols(deps.state)
  ```

  with:

  ```python
          held = await held_symbols(deps.state) if deps.state is not None else set()
  ```

  Run: `uv run pytest tests/unit/test_research_intraday.py -q`. These FAIL: `test_without_the_bots_ledger_it_fails_closed`.

- **Break 6.** Give intraday the full research rate. In `src/traider/research/wiring.py`, replace:

  ```python
          INTRADAY_SCHWAB_MAX_PER_MINUTE if kind == "intraday" else RESEARCH_SCHWAB_MAX_PER_MINUTE
  ```

  with:

  ```python
          RESEARCH_SCHWAB_MAX_PER_MINUTE
  ```

  Run: `uv run pytest tests/unit/test_cli_research_run.py -q`. These FAIL: `test_the_intraday_wiring_reads_the_bots_state_at_a_lower_rate`.

- [ ] **Step 6: Whole suite, lint, types**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: all green: 2220 passed, 6 skipped.

- [ ] **Step 7: Commit**

```bash
git add src/traider/research/dive.py src/traider/research/run.py src/traider/research/intraday.py \
  src/traider/research/wiring.py src/traider/cli.py tests/unit/test_research_intraday.py \
  tests/unit/test_cli_research_run.py
git commit -m "feat(research): intraday runs that only tighten

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Xh7zur22CRYEGaDoTB5nkV"
```

---

### Task 7: The bot says so when there is no posture

**Files:**
- Modify: `src/traider/research/source.py`, `src/traider/engine.py`, `docs/runbook.md` (the event table and the alert table)
- Test: `tests/unit/test_engine_no_posture.py` (new); `tests/unit/test_docs.py` (unchanged; it enforces the event row)

**Interfaces:**
- Consumes: `ResearchSettings.posture_alert_after_open_min` (Task 1); `SessionTracker.view(now)` (`is_open`, `minutes_since_open`).
- Produces:
  - `ResearchView.posture_missing: bool` (property): `as_of is not None and not stale and posture is None`. A posture whose level is `stand_aside` is not missing.
  - `Engine._check_posture(now)`, called at the end of `_housekeeping`; engine attribute `_no_posture_day: date | None`.
  - Event `research_no_posture` with data `{"day": "<YYYY-MM-DD>"}`; alert key `research_no_posture`, subject "No research posture today", message "No usable research posture for today; the bot is standing aside. Check the pre-market run (alerts, META, the DLQ)."

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_engine_no_posture.py`:

```python
"""The bot says so, once a day, when research is on and there is no usable posture a few
minutes after the open. It stands aside either way; this only makes it loud."""

from datetime import UTC, datetime

from tests.unit.engine_harness import Harness
from tests.unit.test_engine_research import write
from traider.research.models import Posture
from traider.research.source import ResearchView
from traider.research.store import MemoryResearchStore

OPEN = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)  # Thursday 09:30 New York
BEFORE_OPEN = datetime(2026, 10, 8, 13, 0, tzinfo=UTC)
MESSAGE = (
    "No usable research posture for today; the bot is standing aside. Check the pre-market "
    "run (alerts, META, the DLQ)."
)


async def bench(tmp_path, *, start=OPEN, level=None, status="ok", store=None, **kwargs) -> Harness:
    """Research on. ``level=None``: no posture written today."""
    store = store if store is not None else MemoryResearchStore()
    h = await Harness.create(
        tmp_path, symbols=(), research_store=store, begin=False, start=start, **kwargs
    )
    if level is not None:
        await write(h, store, (), level, status)
    await h.engine.start()
    await h.engine.step()
    return h


def no_posture_alerts(h) -> list[tuple[str, str, str]]:
    return [a for a in h.alerts.sent if a[0] == "research_no_posture"]


async def test_it_fires_once_after_the_delay(tmp_path):
    h = await bench(tmp_path)
    await h.run_for(4 * 60)  # 09:34: not yet
    assert no_posture_alerts(h) == []
    await h.run_for(2 * 60)  # 09:36: research was read after 09:35
    assert no_posture_alerts(h) == [("research_no_posture", "No research posture today", MESSAGE)]
    (event,) = await h.events("research_no_posture")
    assert event["data"] == {"day": "2026-10-08"}
    await h.run_for(5 * 60)  # once a day
    assert len(no_posture_alerts(h)) == 1
    assert len(await h.events("research_no_posture")) == 1


async def test_it_waits_for_a_read_after_the_delay(tmp_path):
    # The research poll is every 60 s by default; the view read at 09:30 is too old.
    h = await bench(tmp_path, research_settings={"poll_s": 600})
    await h.run_for(6 * 60)
    assert no_posture_alerts(h) == []
    await h.run_for(5 * 60)  # 09:41: the 09:40 read counts
    assert len(no_posture_alerts(h)) == 1


async def test_the_delay_is_a_setting(tmp_path):
    h = await bench(tmp_path, research_settings={"posture_alert_after_open_min": 30})
    await h.run_for(10 * 60)
    assert no_posture_alerts(h) == []
    await h.run_for(22 * 60)
    assert len(no_posture_alerts(h)) == 1


async def test_not_before_the_open(tmp_path):
    h = await bench(tmp_path, start=BEFORE_OPEN)
    await h.run_for(29 * 60)  # 08:59 to 09:28
    assert no_posture_alerts(h) == []


async def test_not_when_research_chose_to_stand_aside(tmp_path):
    h = await bench(tmp_path, level="stand_aside")
    await h.run_for(10 * 60)
    assert no_posture_alerts(h) == []
    assert h.research.view.level.value == "stand_aside"


async def test_a_partial_runs_posture_counts_as_missing(tmp_path):
    h = await bench(tmp_path, level="trade", status="partial")
    await h.run_for(6 * 60)
    assert len(no_posture_alerts(h)) == 1


async def test_not_when_research_is_stale(tmp_path):
    class Broken(MemoryResearchStore):
        async def day(self, day):
            raise RuntimeError("table unreachable")

    h = await bench(tmp_path, store=Broken(), research_settings={"max_stale_s": 60})
    await h.run_for(10 * 60)
    assert no_posture_alerts(h) == []
    assert "research_stale" in h.alert_keys()


async def test_not_when_research_is_off(tmp_path):
    h = await Harness.create(tmp_path, start=OPEN)
    await h.run_for(10 * 60)
    assert no_posture_alerts(h) == []


async def test_a_good_posture_means_no_alert(tmp_path):
    h = await bench(tmp_path, level="trade")
    await h.run_for(10 * 60)
    assert no_posture_alerts(h) == []


def test_the_view_tells_a_missing_posture_from_a_chosen_stand_aside():
    now = OPEN
    assert ResearchView(as_of=now).posture_missing is True
    assert ResearchView().posture_missing is False  # never read
    assert ResearchView(as_of=now, stale=True).posture_missing is False
    chosen = Posture(level="stand_aside", run_id="r1", at=now)
    assert ResearchView(as_of=now, posture=chosen).posture_missing is False
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_engine_no_posture.py tests/unit/test_docs.py -q`
Expected: FAIL (5 failed, 18 passed): `AttributeError: 'ResearchView' object has no attribute 'posture_missing'`, and the alert never comes.

- [ ] **Step 3: Implement**

In `src/traider/research/source.py`, replace:

```python
        return self.posture.level
```

with:

```python
        return self.posture.level

    @property
    def posture_missing(self) -> bool:
        """Research was read and has no usable posture for today: the bot stands aside
        for want of one. Not the same as research choosing ``stand_aside``, and false
        while research is stale or has never been read (``research_stale`` covers those)."""
        return self.as_of is not None and not self.stale and self.posture is None
```

In `src/traider/engine.py` (1 of 3), replace:

```python
        self._research_at: datetime | None = None
```

with:

```python
        self._research_at: datetime | None = None
        self._no_posture_day: date | None = None  # the day research_no_posture was reported
```

In `src/traider/engine.py` (2 of 3), replace:

```python
            await self._refresh_account(now)
```

with:

```python
            await self._refresh_account(now)
        await self._check_posture(now)
```

In `src/traider/engine.py` (3 of 3), replace:

```python
                "and the day's posture.",
            )
```

with:

```python
                "and the day's posture.",
            )

    async def _check_posture(self, now: datetime) -> None:
        """Once per trading day, ``research.posture_alert_after_open_min`` after the open:
        with research on and the session open, say so if research, read since then, has
        no usable posture for today. The bot stands aside either way; this makes it loud.
        A posture research set to ``stand_aside`` is not missing, and staleness has its
        own alert."""
        research = self._research
        today = trading_date(now)
        if research is None or self._no_posture_day == today:
            return
        session = self._session.view(now)
        if not session.is_open or session.minutes_since_open is None:
            return
        delay = self._settings.research.posture_alert_after_open_min
        if session.minutes_since_open < delay:
            return
        view = research.view
        alert_from = now - timedelta(minutes=session.minutes_since_open - delay)
        if view.as_of is None or view.as_of < alert_from or not view.posture_missing:
            return  # not read since the alert time yet, or nothing is missing
        self._no_posture_day = today
        await self._event("research_no_posture", {"day": today.isoformat()}, now)
        await self._alerts.send(
            "research_no_posture",
            "No research posture today",
            "No usable research posture for today; the bot is standing aside. Check the "
            "pre-market run (alerts, META, the DLQ).",
        )
```

The runbook's event table must list the new event (the doc test enforces it), and the alert table gains the alert:

In `docs/runbook.md` (1 of 2), replace:

```markdown
| Research readable again | It recovered. | Nothing. |
```

with:

```markdown
| Research readable again | It recovered. | Nothing. |
| No research posture today | Research is on and, a few minutes after the open (`research.posture_alert_after_open_min`, 5 by default), there is no usable posture for today: the pre-market run failed, did not run, finished `partial` (ignored by default) or was switched off. The bot stands aside all day; the intraday runs do not rescue it. Once a day. | Find out why: the morning's research alert, `traider research show` (what the bot sees), the research logs and the dead-letter queue (see [Research jobs](#research-jobs)). The bot picks up a posture within a minute of a run finishing `ok`; whether to start one by hand during the session is your call (it shares Schwab's request quota with the bot). |
```

In `docs/runbook.md` (2 of 2), replace:

```markdown
| `research_restored` | Research is readable again. |
```

with:

```markdown
| `research_restored` | Research is readable again. |
| `research_no_posture` | Research is on, the session has been open for `research.posture_alert_after_open_min` minutes (5 by default), and research, read since then, has no usable posture for today, so the bot stands aside. Recorded with the alert, once a day (again after a restart). Not recorded when research chose `stand_aside`, or while research is stale (`research_stale` covers that). |
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `uv run pytest tests/unit/test_engine_no_posture.py tests/unit/test_docs.py -q`
Expected: PASS (23 passed). `test_runbook_explains_exactly_the_events_the_engine_records` fails if the runbook row is missing: the engine now records `research_no_posture`.

- [ ] **Step 5: Break on purpose (restore after each)**

Make each change, run the named tests, see them FAIL, then undo the change exactly.

- **Break 1.** **Spec break:** fire on a research-chosen `stand_aside`. In `src/traider/research/source.py`, replace:

  ```python
          return self.as_of is not None and not self.stale and self.posture is None
  ```

  with:

  ```python
          return self.as_of is not None and not self.stale and self.level is PostureLevel.STAND_ASIDE
  ```

  Run: `uv run pytest tests/unit/test_engine_no_posture.py -q`. These FAIL: `test_not_when_research_chose_to_stand_aside`, `test_the_view_tells_a_missing_posture_from_a_chosen_stand_aside`.

- **Break 2.** Count a stale view as missing. In `src/traider/research/source.py`, replace:

  ```python
          return self.as_of is not None and not self.stale and self.posture is None
  ```

  with:

  ```python
          return self.as_of is not None and self.posture is None
  ```

  Run: `uv run pytest tests/unit/test_engine_no_posture.py -q`. These FAIL: `test_the_view_tells_a_missing_posture_from_a_chosen_stand_aside`.

- **Break 3.** Trust a read from before the alert time. In `src/traider/engine.py`, replace:

  ```python
          if view.as_of is None or view.as_of < alert_from or not view.posture_missing:
  ```

  with:

  ```python
          if view.as_of is None or not view.posture_missing:
  ```

  Run: `uv run pytest tests/unit/test_engine_no_posture.py -q`. These FAIL: `test_it_waits_for_a_read_after_the_delay`.

- [ ] **Step 6: Whole suite, lint, types**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: all green: 2230 passed, 6 skipped.

- [ ] **Step 7: Commit**

```bash
git add src/traider/research/source.py src/traider/engine.py docs/runbook.md \
  tests/unit/test_engine_no_posture.py
git commit -m "feat(engine): alert when research has no posture

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Xh7zur22CRYEGaDoTB5nkV"
```

---

### Task 8: Infrastructure: two more schedules, overrides, and read-only state access

**Files:**
- Modify: `infra/settings.py`, `infra/research.py`, `infra/Pulumi.example.yaml`
- Test: `infra/tests/test_research_jobs.py`. There is now one schedule per kind, so the tests that called `jobs.one(SCHEDULE)` either check all three (four are renamed: `test_every_schedule_starts_disabled`, `test_each_schedule_runs_only_once_you_enable_it_and_only_that_one`, `test_enabling_a_schedule_without_the_jobs_is_refused`, `test_every_schedule_runs_on_the_bots_network`) or look the pre-market one up by name (`test_it_runs_weekdays_at_8_…`, `test_a_start_the_scheduler_gives_up_on_…`). The two exact-statement tests gain `ReadBotState` and `dynamodb:BatchGetItem`; the two environment tests gain the state table and namespace. Two tests are new.

**Interfaces:**
- Consumes: `Data.table` (the bot's state table), `Settings.trading_mode`.
- Produces:
  - `infra/settings.py`: `Settings.research_scorecard_enabled`, `Settings.research_intraday_enabled` (stack config `traider:researchScorecardEnabled`, `traider:researchIntradayEnabled`, default false, each refused without `traider:researchJobs`).
  - `infra/research.py`: `SCORECARD_SCHEDULE = "cron(30 16 ? * MON-FRI *)"`, `INTRADAY_SCHEDULE = "cron(0/30 10-15 ? * MON-FRI *)"`; schedules `research-premarket` (unchanged), `research-scorecard`, `research-intraday`, the last two with target `input` `{"containerOverrides": [{"name": "research", "command": ["research", "run", "--kind", <kind>]}]}`; task-role statement `ReadBotState` (`dynamodb:Query` on the state table, `ForAllValues:StringLike` `dynamodb:LeadingKeys` `["POS#<mode>", "LOG#<mode>#*"]`); `dynamodb:BatchGetItem` in the `Research` statement; task env `TRAIDER_STATE_TABLE` and `TRAIDER_STATE_NAMESPACE` (the stack's trading mode).

- [ ] **Step 1: Write the failing tests**

In `infra/tests/test_research_jobs.py` (1 of 10), replace:

```python
def test_the_schedule_starts_disabled(jobs):
    assert jobs.one(SCHEDULE).inputs["state"] == "DISABLED"


def test_the_schedule_runs_only_once_you_enable_it():
    enabled = deploy({"research": True, "researchJobs": True, "researchScheduleEnabled": True})
    assert enabled.one(SCHEDULE).inputs["state"] == "ENABLED"
    disabled = deploy({"research": True, "researchJobs": True, "researchScheduleEnabled": False})
    assert disabled.one(SCHEDULE).inputs["state"] == "DISABLED"


@pytest.mark.parametrize("config", [{}, {"research": True}], ids=["default", "research-only"])
def test_enabling_the_schedule_without_the_jobs_is_refused(config):
    with pytest.raises(Exception, match="researchScheduleEnabled needs traider:researchJobs"):
        deploy({**config, "researchScheduleEnabled": True})
```

with:

```python
KINDS = ("premarket", "scorecard", "intraday")
TOGGLES = {
    "premarket": "researchScheduleEnabled",
    "scorecard": "researchScorecardEnabled",
    "intraday": "researchIntradayEnabled",
}


def schedule(deployment, kind="premarket"):
    return deployment.one(SCHEDULE, f"research-{kind}")


def test_every_schedule_starts_disabled(jobs):
    assert {s.name: s.inputs["state"] for s in jobs.of(SCHEDULE)} == {
        f"research-{kind}": "DISABLED" for kind in KINDS
    }


@pytest.mark.parametrize("kind", KINDS)
def test_each_schedule_runs_only_once_you_enable_it_and_only_that_one(kind):
    enabled = deploy({"research": True, "researchJobs": True, TOGGLES[kind]: True})
    states = {s.name: s.inputs["state"] for s in enabled.of(SCHEDULE)}
    assert states == {
        f"research-{other}": "ENABLED" if other == kind else "DISABLED" for other in KINDS
    }
    disabled = deploy({"research": True, "researchJobs": True, TOGGLES[kind]: False})
    assert schedule(disabled, kind).inputs["state"] == "DISABLED"


@pytest.mark.parametrize("toggle", TOGGLES.values())
@pytest.mark.parametrize("config", [{}, {"research": True}], ids=["default", "research-only"])
def test_enabling_a_schedule_without_the_jobs_is_refused(config, toggle):
    with pytest.raises(Exception, match=f"{toggle} needs traider:researchJobs"):
        deploy({**config, toggle: True})
```

In `infra/tests/test_research_jobs.py` (2 of 10), replace:

```python
    config = Config.from_env(env)
    assert (config.trading_mode, config.finnhub_api_key) == ("paper", None)
```

with:

```python
    # The bot's state, to read: the namespace is the stack's trading mode, given apart.
    assert env["TRAIDER_STATE_TABLE"] == jobs.one(TABLE, "state").inputs["name"]
    assert env["TRAIDER_STATE_NAMESPACE"] == "paper"
    config = Config.from_env(env)
    assert (config.trading_mode, config.finnhub_api_key) == ("paper", None)
    assert (config.state_table, config.state_namespace) == (
        jobs.one(TABLE, "state").inputs["name"],
        "paper",
    )
```

In `infra/tests/test_research_jobs.py` (3 of 10), replace:

```python
    assert Config.from_env(environment(live)).research_table == "traider-prod-research"
```

with:

```python
    config = Config.from_env(environment(live))
    assert config.research_table == "traider-prod-research"
    assert (config.trading_mode, config.state_namespace) == ("paper", "live")
```

In `infra/tests/test_research_jobs.py` (4 of 10), replace:

```python
        "ReadSettings": jobs.one(TABLE, "settings").arn,
```

with:

```python
        "ReadSettings": jobs.one(TABLE, "settings").arn,
        "ReadBotState": jobs.one(TABLE, "state").arn,
```

In `infra/tests/test_research_jobs.py` (5 of 10), replace:

```text
            "dynamodb:GetItem",
```

with:

```text
            "dynamodb:GetItem",
            "dynamodb:BatchGetItem",
```

In `infra/tests/test_research_jobs.py` (6 of 10), replace:

```python
        "ReadSettings": ["dynamodb:Query", "dynamodb:GetItem"],
```

with:

```python
        "ReadSettings": ["dynamodb:Query", "dynamodb:GetItem"],
        "ReadBotState": "dynamodb:Query",
```

In `infra/tests/test_research_jobs.py` (7 of 10), replace:

```python
    schedule = jobs.one(SCHEDULE).inputs
    assert schedule["scheduleExpression"] == "cron(0 8 ? * MON-FRI *)"
    assert schedule["scheduleExpressionTimezone"] == "America/New_York"
    assert schedule["flexibleTimeWindow"] == {"mode": "OFF"}
    target = schedule["target"]
```

with:

```python
    found = schedule(jobs).inputs
    assert found["scheduleExpression"] == "cron(0 8 ? * MON-FRI *)"
    assert found["scheduleExpressionTimezone"] == "America/New_York"
    assert found["flexibleTimeWindow"] == {"mode": "OFF"}
    target = found["target"]
```

In `infra/tests/test_research_jobs.py` (8 of 10), replace:

```text


def test_it_runs_on_the_bots_network(jobs):
    service = jobs.one("aws:ecs/service:Service").inputs["networkConfiguration"]
    network = jobs.one(SCHEDULE).inputs["target"]["ecsParameters"]["networkConfiguration"]
    assert network["subnets"] == service["subnets"]
    assert network["securityGroups"] == service["securityGroups"]
    assert network["assignPublicIp"] is True
```

with:

```python
    assert "input" not in target  # the task's own command: premarket


@pytest.mark.parametrize(
    ("kind", "expression"),
    [("scorecard", "cron(30 16 ? * MON-FRI *)"), ("intraday", "cron(0/30 10-15 ? * MON-FRI *)")],
)
def test_the_other_kinds_run_on_their_own_times_with_their_own_command(jobs, kind, expression):
    found = schedule(jobs, kind).inputs
    assert found["name"] == f"traider-dev-research-{kind}"
    assert found["scheduleExpression"] == expression
    assert found["scheduleExpressionTimezone"] == "America/New_York"
    assert found["flexibleTimeWindow"] == {"mode": "OFF"}
    target = found["target"]
    assert json.loads(target["input"]) == {
        "containerOverrides": [{"name": "research", "command": ["research", "run", "--kind", kind]}]
    }
    assert container(jobs)["name"] == "research"  # the override names the real container
    premarket = schedule(jobs).inputs["target"]
    # Everything else is the pre-market schedule's: cluster, role, task, network, retries
    # and the dead-letter queue.
    assert {k: v for k, v in target.items() if k != "input"} == premarket


def test_every_schedule_runs_on_the_bots_network(jobs):
    service = jobs.one("aws:ecs/service:Service").inputs["networkConfiguration"]
    for kind in KINDS:
        network = schedule(jobs, kind).inputs["target"]["ecsParameters"]["networkConfiguration"]
        assert network["subnets"] == service["subnets"]
        assert network["securityGroups"] == service["securityGroups"]
        assert network["assignPublicIp"] is True
```

In `infra/tests/test_research_jobs.py` (9 of 10), replace:

```python
    target = jobs.one(SCHEDULE).inputs["target"]
    assert target["deadLetterConfig"] == {"arn": queue.arn}
```

with:

```python
    for kind in KINDS:
        target = schedule(jobs, kind).inputs["target"]
        assert target["deadLetterConfig"] == {"arn": queue.arn}
```

Append to the end of `infra/tests/test_research_jobs.py` (10 of 10), after two blank lines:

```python
# --- C2a: reading the bot's state ------------------------------------------------------------


@pytest.mark.parametrize(("mode", "stack"), [("paper", "dev"), ("live", "prod")])
def test_research_may_only_query_the_bots_ledger_and_event_log(mode, stack):
    config = {"research": True, "researchJobs": True, "alertEmail": "ops@example.test"}
    if mode == "live":
        config |= {"tradingMode": "live", "accountLast4": "5678"}
    deployment = deploy(config, stack=stack)
    read = statements(deployment, "research-task")["ReadBotState"]
    assert (read["Action"], read["Resource"]) == (
        "dynamodb:Query",
        deployment.one(TABLE, "state").arn,
    )
    assert read["Condition"] == {
        "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"POS#{mode}", f"LOG#{mode}#*"]}
    }
    assert environment(deployment)["TRAIDER_STATE_NAMESPACE"] == mode
    # Nothing else in the research role touches the state table.
    others = [s for s in deployment.policy("research-task") if s["Sid"] != "ReadBotState"]
    assert deployment.one(TABLE, "state").arn not in json.dumps(others)
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_research_jobs.py -q` (from `infra/`)
Expected: FAIL (18 failed, 29 passed): there is one schedule, not three, the role has no `ReadBotState` statement, and the task's environment has no `TRAIDER_STATE_TABLE`.

- [ ] **Step 3: Implement**

In `infra/settings.py` (1 of 3), replace:

```python
    research_schedule_enabled: bool  # the schedule fires; needs research_jobs
```

with:

```python
    research_schedule_enabled: bool  # the schedule fires; needs research_jobs
    research_scorecard_enabled: bool  # the scorecard's schedule fires; needs research_jobs
    research_intraday_enabled: bool  # the intraday schedule fires; needs research_jobs
```

In `infra/settings.py` (2 of 3), replace:

```python
    research_schedule_enabled = bool(config.get_bool("researchScheduleEnabled"))
    if research_schedule_enabled and not research_jobs:
        raise ValueError(
            "traider:researchScheduleEnabled needs traider:researchJobs: true: there is no "
            "research schedule to enable without the research jobs"
        )
```

with:

```python
    toggles = {
        key: bool(config.get_bool(key))
        for key in (
            "researchScheduleEnabled",
            "researchScorecardEnabled",
            "researchIntradayEnabled",
        )
    }
    for key, on in toggles.items():
        if on and not research_jobs:
            raise ValueError(
                f"traider:{key} needs traider:researchJobs: true: there is no research "
                "schedule to enable without the research jobs"
            )
```

In `infra/settings.py` (3 of 3), replace:

```text
        research_schedule_enabled=research_schedule_enabled,
```

with:

```text
        research_schedule_enabled=toggles["researchScheduleEnabled"],
        research_scorecard_enabled=toggles["researchScorecardEnabled"],
        research_intraday_enabled=toggles["researchIntradayEnabled"],
```

In `infra/research.py` (1 of 7), replace:

```python
"""The scheduled research run (opt-in: ``traider:researchJobs``, which needs research on).

Every weekday at 08:00 New York time EventBridge Scheduler starts one Fargate task from
the bot's image: ``traider research run --kind premarket``. It reads the market, sets the
day's posture, has Claude on Bedrock study the best candidates and writes ranked picks to
the research table. Its trail goes to a private S3 bucket. A task that exits non-zero
raises an alert, and so does a run that cannot even be started: what the scheduler
fails to deliver lands in a dead-letter queue, which alarms.
```

with:

```python
"""The scheduled research runs (opt-in: ``traider:researchJobs``, which needs research on).

EventBridge Scheduler starts Fargate tasks from the bot's image, one schedule per kind,
each created disabled until its own toggle is on:

* premarket, weekdays 08:00 New York: ``traider research run --kind premarket``. It reads
  the market, sets the day's posture, has Claude on Bedrock study the best candidates and
  writes ranked picks to the research table (``traider:researchScheduleEnabled``);
* scorecard, weekdays 16:30: how each recent pick did (``traider:researchScorecardEnabled``);
* intraday, every 30 minutes from 10:00 to 15:30 (the run itself skips starts after
  ``research_jobs.intraday.last_start``, 15:00 by default): new names, and a posture that
  can only tighten (``traider:researchIntradayEnabled``).

The scorecard and intraday schedules override the container's command. The trail goes to
a private S3 bucket. A task that exits non-zero raises an alert, and so does a run that
cannot even be started: what the scheduler fails to deliver lands in a dead-letter queue,
which alarms.
```

In `infra/research.py` (2 of 7), replace:

```text
from __future__ import annotations

```

with:

```python
from __future__ import annotations

import json
```

In `infra/research.py` (3 of 7), replace:

```python
SCHEDULE = "cron(0 8 ? * MON-FRI *)"
```

with:

```python
SCHEDULE = "cron(0 8 ? * MON-FRI *)"
SCORECARD_SCHEDULE = "cron(30 16 ? * MON-FRI *)"
INTRADAY_SCHEDULE = "cron(0/30 10-15 ? * MON-FRI *)"
```

In `infra/research.py` (4 of 7), replace:

```text
                "dynamodb:GetItem",
```

with:

```text
                "dynamodb:GetItem",
                "dynamodb:BatchGetItem",
```

In `infra/research.py` (5 of 7), replace:

```python
            "Resource": data.settings_table.arn,
```

with:

```python
            "Resource": data.settings_table.arn,
        },
        {
            # The bot's ledger and event log, in its own namespace: Query only, and only
            # those partitions (the scorecard's "traded", intraday's "held").
            "Sid": "ReadBotState",
            "Effect": "Allow",
            "Action": "dynamodb:Query",
            "Resource": data.table.arn,
            "Condition": {
                "ForAllValues:StringLike": {
                    "dynamodb:LeadingKeys": [
                        f"POS#{settings.trading_mode}",
                        f"LOG#{settings.trading_mode}#*",
                    ]
                }
            },
```

In `infra/research.py` (6 of 7), replace:

```python
        "TRAIDER_RESEARCH_TABLE": data.research_table.name,
```

with:

```python
        "TRAIDER_RESEARCH_TABLE": data.research_table.name,
        # Read-only (the role above). The namespace is the bot's trading mode, given
        # explicitly because the research task has no trading mode of its own.
        "TRAIDER_STATE_TABLE": data.table.name,
        "TRAIDER_STATE_NAMESPACE": settings.trading_mode,
```

In `infra/research.py` (7 of 7), replace:

```python
    aws.scheduler.Schedule(
        "research-premarket",
        name=f"{prefix}-research-premarket",
        description="traider: the pre-market research run",
        schedule_expression=SCHEDULE,
        schedule_expression_timezone=TIMEZONE,
        # Created disabled: nothing fires until you have stored the Finnhub key, enabled
        # Bedrock access and done a dry run, then set traider:researchScheduleEnabled.
        state="ENABLED" if settings.research_schedule_enabled else "DISABLED",
        # Exactly on time. A start that fails is retried briefly (the lock and
        # skip-if-done make a duplicate harmless); one that still fails goes to the
        # dead-letter queue.
        flexible_time_window=aws.scheduler.ScheduleFlexibleTimeWindowArgs(mode="OFF"),
        target=aws.scheduler.ScheduleTargetArgs(
            arn=cluster.arn,
            role_arn=scheduler_role.arn,
            ecs_parameters=aws.scheduler.ScheduleTargetEcsParametersArgs(
                task_definition_arn=task.arn,
                launch_type="FARGATE",
                task_count=1,
                network_configuration=aws.scheduler.ScheduleTargetEcsParametersNetworkConfigurationArgs(
                    subnets=network.subnet_ids,
                    security_groups=[network.security_group_id],
                    assign_public_ip=True,
                ),
            ),
            retry_policy=aws.scheduler.ScheduleTargetRetryPolicyArgs(
                maximum_retry_attempts=START_RETRIES,
                maximum_event_age_in_seconds=START_MAX_AGE_S,
            ),
            dead_letter_config=aws.scheduler.ScheduleTargetDeadLetterConfigArgs(
                arn=dead_letters.arn
            ),
        ),
        opts=pulumi.ResourceOptions(depends_on=[scheduler_policy]),
```

with:

```python

    def schedule(
        kind: str, expression: str, enabled: bool, description: str
    ) -> aws.scheduler.Schedule:
        # Each is created disabled: nothing fires until you have stored the Finnhub key,
        # enabled Bedrock access and done a dry run, then set its toggle. Exactly on time.
        # A start that fails is retried briefly (the lock, and skip-if-done for the
        # once-a-day kinds, make a duplicate harmless); one that still fails goes to the
        # dead-letter queue. Kinds other than premarket override the container's command.
        overrides = None
        if kind != "premarket":
            overrides = json.dumps(
                {
                    "containerOverrides": [
                        {"name": "research", "command": ["research", "run", "--kind", kind]}
                    ]
                }
            )
        return aws.scheduler.Schedule(
            f"research-{kind}",
            name=f"{prefix}-research-{kind}",
            description=f"traider: {description}",
            schedule_expression=expression,
            schedule_expression_timezone=TIMEZONE,
            state="ENABLED" if enabled else "DISABLED",
            flexible_time_window=aws.scheduler.ScheduleFlexibleTimeWindowArgs(mode="OFF"),
            target=aws.scheduler.ScheduleTargetArgs(
                arn=cluster.arn,
                role_arn=scheduler_role.arn,
                input=overrides,
                ecs_parameters=aws.scheduler.ScheduleTargetEcsParametersArgs(
                    task_definition_arn=task.arn,
                    launch_type="FARGATE",
                    task_count=1,
                    network_configuration=aws.scheduler.ScheduleTargetEcsParametersNetworkConfigurationArgs(
                        subnets=network.subnet_ids,
                        security_groups=[network.security_group_id],
                        assign_public_ip=True,
                    ),
                ),
                retry_policy=aws.scheduler.ScheduleTargetRetryPolicyArgs(
                    maximum_retry_attempts=START_RETRIES,
                    maximum_event_age_in_seconds=START_MAX_AGE_S,
                ),
                dead_letter_config=aws.scheduler.ScheduleTargetDeadLetterConfigArgs(
                    arn=dead_letters.arn
                ),
            ),
            opts=pulumi.ResourceOptions(depends_on=[scheduler_policy]),
        )

    schedule(
        "premarket",
        SCHEDULE,
        settings.research_schedule_enabled,
        "the pre-market research run",
    )
    schedule(
        "scorecard",
        SCORECARD_SCHEDULE,
        settings.research_scorecard_enabled,
        "the scorecard after the close",
    )
    schedule(
        "intraday",
        INTRADAY_SCHEDULE,
        settings.research_intraday_enabled,
        "the intraday research runs",
```

The example lists every setting the docs name (an infra test checks it); keep the comment lines from starting with `traider:`, because the test uncomments those:

In `infra/Pulumi.example.yaml`, replace:

```yaml
  # traider:researchScheduleEnabled: false
```

with:

```yaml
  # traider:researchScheduleEnabled: false
  # Two more schedules, each created disabled with its own switch; both need the
  # research jobs on. The scorecard, weekdays at 16:30 New York, records how
  # each recent pick did; it calls no model. The intraday runs, every 30 minutes from
  # 10:00 to 15:00, add new names and can only tighten the day's posture; each costs at
  # most research_jobs.budget.intraday_run_usd. See docs/runbook.md, "Research jobs".
  # traider:researchScorecardEnabled: false
  # traider:researchIntradayEnabled: false
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `uv run pytest tests/test_research_jobs.py -q` (from `infra/`)
Expected: PASS (47 passed).

- [ ] **Step 5: Break on purpose (restore after each)**

Make each change, run the named tests, see them FAIL, then undo the change exactly.

- **Break 1.** Drop the partition limit on the state table. In `infra/research.py`, replace:

  ```python
              "Condition": {
                  "ForAllValues:StringLike": {
  ```

  with:

  ```python
              "XCondition": {
                  "ForAllValues:StringLike": {
  ```

  Run from `infra/`: `uv run pytest tests/test_research_jobs.py -q`. These FAIL: `test_research_may_only_query_the_bots_ledger_and_event_log[paper-dev]` and `[live-prod]`.

- **Break 2.** Create every schedule enabled. In `infra/research.py`, replace:

  ```text
              state="ENABLED" if enabled else "DISABLED",
  ```

  with:

  ```text
              state="ENABLED",
  ```

  Run from `infra/`: `uv run pytest tests/test_research_jobs.py -q`. These FAIL: `test_every_schedule_starts_disabled` and the three `test_each_schedule_runs_only_once_you_enable_it_and_only_that_one` cases.

- **Break 3.** Drop the command override. In `infra/research.py`, replace:

  ```python
          if kind != "premarket":
  ```

  with:

  ```python
          if False:
  ```

  Run from `infra/`: `uv run pytest tests/test_research_jobs.py -q`. These FAIL: both `test_the_other_kinds_run_on_their_own_times_with_their_own_command` cases.

- [ ] **Step 6: Whole suite, lint, types**

From `infra/`: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: all green: 168 passed.

Then from the root (the example file and the docs are read by both): `uv run pytest tests/unit/test_docs.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add infra/settings.py infra/research.py infra/Pulumi.example.yaml \
  infra/tests/test_research_jobs.py
git commit -m "feat(infra): scorecard and intraday schedules

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Xh7zur22CRYEGaDoTB5nkV"
```

---

### Task 9: Docs and spec

**Files:**
- Modify: `README.md`, `docs/runbook.md`, `docs/superpowers/specs/2026-10-10-c2a-scorecard-intraday.md`
- Test: `tests/unit/test_docs.py`, `infra/tests/test_stack.py` (both unchanged; they check what the docs name)

**Interfaces:** none. The docs must name only real commands, settings (full dotted paths), stack outputs and stack settings.

- [ ] **Step 1: Update the docs and the spec**

The README gains a section on the two new kinds, the cost note and the "not verified" items:

In `README.md` (1 of 5), replace:

```markdown
- **The pre-market research run has never called a real service.** It was tested end to
  end against a fake Schwab, a fake Finnhub, a scripted model and moto. Not verified:
```

with:

```markdown
- **The pre-market research run has never called a real service,** and neither have the
  scorecard and the intraday runs. They were tested end to end against a fake Schwab, a
  fake Finnhub, a scripted model and moto. Not verified:
```

In `README.md` (2 of 5), replace:

```markdown
  - the Scheduler, ECS task and dead-letter queue behaviour on real AWS.
```

with:

```markdown
  - the Scheduler, ECS task and dead-letter queue behaviour on real AWS, including the
    command override the scorecard and intraday schedules pass to the task;
  - Schwab movers during the session, and whether the day's daily bar is there at 16:30;
  - that the state table's namespace the research task is given matches the bot's.
```

In `README.md` (3 of 5), replace:

```markdown
[runbook](docs/runbook.md#research-jobs) has the steps and what each alert means.

```

with:

```markdown
[runbook](docs/runbook.md#research-jobs) has the steps and what each alert means.

### The scorecard and the intraday runs

Two more schedules run from the same task, each created **disabled** with its own switch
(both need `traider:researchJobs: true`):

- **The scorecard** (`traider:researchScorecardEnabled`), weekdays at 16:30 New York time:
  `traider research run --kind scorecard`. For every pick of an `ok` or `partial` run in the
  last `research_jobs.scorecard.lookback_days` weekdays (30) it records how the pick did,
  from Schwab daily bars: the 1-, 5- and 20-day returns from the pick day's open (signed by
  side, so a bearish pick gains when the price falls), the best and worst move while it was
  live, whether the price reached its invalidation, the return at expiry, and whether the
  bot bought it while it was live (from the bot's event log). It calls no model. Each
  pick's outcome is final once its 20-day return and its expiry are known. A daily summary
  (the hit rate and mean of the 1- and 5-day returns that became known that day, and the
  mean 1-day return per score bucket) goes to the research table and to a short alert. The web app (B) will report from these
  records; nothing the bot does depends on them.
- **The intraday runs** (`traider:researchIntradayEnabled`), every 30 minutes from 10:00 to
  15:00: `traider research run --kind intraday`. Each one only proceeds when today already
  has a posture from an `ok` run (it never rescues a day the morning run lost), and looks
  at the day's movers that are not already picked, held by the bot or pinned. It may make
  the day's posture stricter on the code rules (a VIX spike, say), never looser, and it
  has no model review. It dives into at most `research_jobs.intraday.deep_dive_count` (3)
  names with `research_jobs.dive.intraday_model`, within `research_jobs.budget.intraday_run_usd`
  ($0.75) a run, and every pick it makes is intraday: flat by today's close. It holds
  itself to 20 Schwab requests a minute, because the bot is trading at the time. It reads
  the bot's ledger (to know what is held), read-only.

| Setting | Default | Meaning |
| --- | --- | --- |
| `research_jobs.scorecard.enabled` | true | false: no scorecard |
| `research_jobs.scorecard.lookback_days` | 30 | weekdays of picks to score (5 to 60) |
| `research_jobs.intraday.enabled` | true | false: no intraday runs |
| `research_jobs.intraday.last_start` | 15:00 | New York time; a later start does nothing (5 minutes' grace for the task to start) |
| `research_jobs.intraday.deep_dive_count` | 3 | names studied per run |
| `research_jobs.intraday.max_candidates` | 30 | movers screened per run |
| `research_jobs.dive.intraday_model` | `anthropic.claude-sonnet-5-5` | the intraday dives' model; it needs a price in `research_jobs.budget.prices` |
| `research_jobs.budget.intraday_run_usd` | 0.75 | Bedrock spend per intraday run, within `day_usd` |

**Missing posture.** With research on, if a few minutes after the open
(`research.posture_alert_after_open_min`, 5) there is still no usable posture for today,
the bot records `research_no_posture` and alerts once: it is standing aside all day and
someone should look at the morning run.

```

In `README.md` (4 of 5), replace:

```markdown
well under a dollar of Fargate and S3. Finnhub's free tier costs nothing.
```

with:

```markdown
well under a dollar of Fargate and S3. Finnhub's free tier costs nothing. The intraday
runs, when switched on, add up to `research_jobs.budget.intraday_run_usd` ($0.75) each, 11
a day, all inside the same `day_usd` cap, so they mostly use what the morning run left. The
scorecard calls no model.
```

In `README.md` (5 of 5), replace:

```markdown
  research/             picks, posture, the research table, the bot's view of it, and
                        the pre-market research run (run.py) and its parts
```

with:

```markdown
  research/             picks, posture, the research table, the bot's view of it, the
                        research runs (run.py: pre-market and what every run shares;
                        intraday.py; scorecard_run.py) and their parts
```

The runbook's research section gains step 5, the alert rows, where the scorecard's records are, what is not verified and how to switch each part off:

In `docs/runbook.md` (1 of 5), replace:

```markdown
| Research DATE: failed | The run stopped at the stage it names (for example `collect`, `posture` or `dive`; a run that took longer than `research_jobs.max_run_s` plus 9 minutes fails too). It wrote no posture, so the bot stands aside today. If the message says "was written as ok" (or partial) "then failed", the result was already in the table when something went wrong (for example the time box ran out while the alert was sent): it stands, and the bot uses it as usual. | An expired Schwab sign-in is the usual cause: sign in, then run it again. Otherwise read the research logs. After "was written as ... then failed", nothing to redo; read the research logs. |
```

with:

```markdown
| Research DATE: failed | The run stopped at the stage it names (for example `collect`, `posture` or `dive`; a run that took longer than `research_jobs.max_run_s` plus 9 minutes fails too). It wrote no posture, so the bot stands aside today. If the message says "was written as ok" (or partial) "then failed", the result was already in the table when something went wrong (for example the time box ran out while the alert was sent): it stands, and the bot uses it as usual. | An expired Schwab sign-in is the usual cause: sign in, then run it again. Otherwise read the research logs. After "was written as ... then failed", nothing to redo; read the research logs. |
| Scorecard DATE: ok | The scorecard after the close: how many picks' 1-day and 5-day returns became known today, how many of those were above zero, and their mean. | Nothing. |
| Scorecard DATE: partial | As above, but daily bars were unreadable for half the symbols or more; their picks stay pending and are scored again tomorrow. | Nothing, unless it repeats (check the research logs). Nothing the bot does depends on the scorecard. |
| Research intraday DATE: ok | An intraday run added picks or made the posture stricter (the alert gives both). Quiet runs send nothing. | Nothing. |
| Research intraday DATE: partial | An intraday run did not finish what it planned (budget, deadline, Finnhub, model calls). By default the bot ignores a partial run's picks and posture; the earlier posture stays in force. | Read the notes; nothing to redo, the next run is 30 minutes later. |
| Research intraday DATE: failed, Scorecard DATE: failed | The run stopped at the stage it names. A failed intraday run writes nothing the bot reads; the posture in force stays. An intraday run that cannot read the bot's ledger fails (`held`): it will not pick what it cannot tell is held. | Read the research logs. For `held`, check the state table and the research task role. |
```

In `docs/runbook.md` (2 of 5), replace:

```markdown
(and `pulumi up`) pauses the schedule and keeps everything else.

```

with:

````markdown
(and `pulumi up`) pauses the schedule and keeps everything else.

**5. The scorecard and the intraday runs (optional).** Each has its own schedule, created
disabled. Dry-run each first, from the repository root with the `localEnv` output loaded.
The scorecard calls no model and costs nothing but Schwab requests; run it after the close.
An intraday dry run needs the session open and an `ok` posture for today, costs up to
`research_jobs.budget.intraday_run_usd` ($0.75) in Bedrock tokens, and shares Schwab's
request quota with the bot (it holds itself to 20 requests a minute). It reads the bot's
ledger, so it needs the state table and the bot's namespace (its trading mode), which
`localEnv` leaves out on purpose; give them for that one command:

```sh
uv run --env-file .env traider research run --kind scorecard --dry-run
TRAIDER_STATE_TABLE="$(cd infra && pulumi stack output stateTable)" TRAIDER_STATE_NAMESPACE=paper \
  uv run --env-file .env traider research run --kind intraday --dry-run
```

(`TRAIDER_STATE_NAMESPACE=live` on a live stack.) Then, from `infra/`:

```sh
pulumi config set traider:researchScorecardEnabled true
pulumi config set traider:researchIntradayEnabled true
pulumi up
```

The scorecard fires at 16:30 on weekdays; the intraday runs at 10:00, 10:30 and so on to
15:30, and each start after `research_jobs.intraday.last_start` (15:00, with 5 minutes'
grace for the task to start) exits at once. Neither schedule is needed for trading.

**What the intraday runs may do.** Only add intraday picks (flat by the close) for movers
that are not picked already, held or pinned, and only make the day's posture stricter. A
day without an `ok` posture by then stays a stand-aside day: an intraday run without one
exits `skipped` and writes nothing. Its alert comes only when it adds picks, tightens the
posture or finishes `partial`; a quiet run still writes the posture and its META.

````

In `docs/runbook.md` (3 of 5), replace:

```markdown
and `result.json` (assessments, why each was refused, the picks).
```

with:

```markdown
and `result.json` (assessments, why each was refused, the picks; for the scorecard, the
outcomes and the summary). The scorecard's records are in the research table: one
`PICK#<run id>#<rank>` / `OUTCOME` item per pick and one `SCORE#<date>` / `SUMMARY` item
per day.
```

In `docs/runbook.md` (4 of 5), replace:

```markdown
- Scheduler, ECS and dead-letter-queue behaviour on real AWS.
```

with:

```markdown
- Scheduler, ECS and dead-letter-queue behaviour on real AWS, and the command override
  the scorecard and intraday schedules pass to the task (if it were ignored, those
  schedules would start the pre-market run, which skips a day already done: check that the
  first scorecard's log says `research scorecard`).
- Schwab movers during the session, and whether the day's daily bar is there at 16:30
  (the scorecard runs after the close so that it should be).
- That the namespace given to the research task (`TRAIDER_STATE_NAMESPACE`, the stack's
  trading mode) matches the bot's: a wrong one would make the intraday run think the bot
  holds nothing, and the scorecard think it traded nothing.
```

In `docs/runbook.md` (5 of 5), replace:

```markdown
`traider:researchJobs false` (and `traider:researchScheduleEnabled` unset or false) and
`pulumi up` to remove it. On a live stack,
```

with:

```markdown
`traider:researchJobs false` (and `traider:researchScheduleEnabled`,
`traider:researchScorecardEnabled` and `traider:researchIntradayEnabled` unset or false)
and `pulumi up` to remove it. The scorecard and the intraday runs each have their own
switch too: `research_jobs.scorecard.enabled` and `research_jobs.intraday.enabled` in the
settings, or `traider:researchScorecardEnabled` and `traider:researchIntradayEnabled` for
their schedules. On a live stack,
```

The spec records what this plan decided (the same list as at the top of this plan) and fixes the three lines that changed:

In `docs/superpowers/specs/2026-10-10-c2a-scorecard-intraday.md` (1 of 4), replace:

```markdown
- `status`: `pending`, `partial` or `final` (final once the 20-day close exists, or the pick is 30 weekdays old)
```

with:

```markdown
- `status`: `pending`, `partial` or `final` (final once the 20-day close and the expiry day's close exist, or the pick is 30 weekdays old)
```

In `docs/superpowers/specs/2026-10-10-c2a-scorecard-intraday.md` (2 of 4), replace:

```markdown
 → collect (context quotes, SPY bars, movers; no earnings calendar fetch;
   reuse today's premarket snapshot calendar from the trail if present, else the Finnhub calendar for today ± lookahead)
```

with:

```markdown
 → collect (context quotes, SPY bars, movers, and the Finnhub earnings calendar from
   yesterday to `earnings_lookahead_days` ahead: one call per run, see the decisions below)
```

In `docs/superpowers/specs/2026-10-10-c2a-scorecard-intraday.md` (3 of 4), replace:

```markdown
| Task role: read-only `dynamodb:Query` on the state table | For the ledger and the event log |
```

with:

```markdown
| Task role: read-only `dynamodb:Query` on the state table, limited by `dynamodb:LeadingKeys` to `POS#<mode>` and `LOG#<mode>#*` | For the ledger and the event log |
| Task role: `dynamodb:BatchGetItem` on the research table | `outcomes(pick_keys)` |
```

Append to the end of `docs/superpowers/specs/2026-10-10-c2a-scorecard-intraday.md` (4 of 4), after one blank line:

```markdown
## Decisions made while planning C2a

Recorded from the implementation plan (`docs/superpowers/plans/2026-10-10-c2a-scorecard-intraday.md`).

* **Shared run scaffolding.** `research/run.py` gains `RunBase` (META, trail, cost meter
  and day cost, alerts, failure handling, time box) and `run_locked` (the lock per kind).
  The pre-market run is one kind (`_Run`); `scorecard_run.py` and `intraday.py` hold the
  others. The pre-market run's output is byte-for-byte what it was.
* **Intraday earnings.** Each intraday run makes one Finnhub calendar call instead of
  reading the morning's `snapshot.json` back from S3: no S3 read permission and no
  parsing of stored files. A failed or empty calendar makes the run `partial`, as in the
  morning (the bot ignores partial runs by default).
* **`last_start` has 5 minutes' grace** (`START_GRACE_S`): a Fargate task takes a minute
  or two to start, so the 15:00 run would otherwise usually be skipped. The 15:30 firing
  of `cron(0/30 10-15 …)` is always skipped.
* **"An ok posture today"** is the newest posture today from an `ok` run of any kind (an
  earlier intraday run included). An unreadable posture item means none (fail closed).
  The bot's ledger is required: without a state table an intraday run cannot start, and
  a run whose ledger read fails fails (no picks).
* **Intraday alert** also when the run is `partial`, not only for new picks or a tighter
  posture. A dry run ignores `last_start` (dry runs run forced) but still needs the session
  open and an ok posture.
* **Scorecard numbers** are percentages (×100), rounded to 4 places. `llm_score` is an
  integer. "Matured today" means that horizon's bar is today's. The summary covers every
  pick in the window: newly scored, kept and already final.
* **`final`** needs the 20-day return *and* the expiry day's close: a 20-weekday swing can
  outlive the 20th bar.
* **The window** is pick days up to `lookback_days` weekdays before today, both ends
  included, so a pick exactly 30 weekdays old gets its final outcome.
* **Unreadable bars** keep a pick's last outcome; a pick with none gets a `pending` one.
* **"Traded"** looks for `order_submitted` events with side `BUY` whose symbol (or an
  option's underlying) is the pick's, from the run's `finished_at` (else `started_at`) to
  the earlier of expiry and now.
* **The scorecard's time box** is `research_jobs.max_run_s`; it has no setting of its own.
  It needs the same setup as the other kinds (a Finnhub key among them), because the
  wiring is shared.
* **Alerts:** the scorecard's subject is `Scorecard <day>: <status>` and its event
  `research_scorecard`; a failure is `research_run_failed`, like the other kinds. The
  intraday subject is `Research intraday <day>: <status>`.
* **Namespace:** research reads the bot's state only with an explicit
  `TRAIDER_STATE_NAMESPACE` (`paper` or `live`); a state table without it is refused.
* **Missing posture:** checked on every engine step once `posture_alert_after_open_min`
  has passed, and only on a research read made since then; once per trading day per bot
  process (a restart may repeat it). A partial run's posture counts as missing unless
  `research.accept_partial_runs` is on.
* **Schedules:** the pre-market schedule keeps the task's own command (no override); the
  scorecard and intraday schedules pass `{"containerOverrides": [{"name": "research",
  "command": [...]}]}` as the target's `input`. Not verified on AWS.
```

- [ ] **Step 2: Run the tests to see them pass**

Run: `uv run pytest tests/unit/test_docs.py -q`
Expected: PASS (13 passed). From `infra/`, `uv run pytest tests/test_stack.py -q` must pass too: it checks every `traider:<setting>` and `pulumi stack output <name>` the docs mention.

- [ ] **Step 3: Whole suite, lint, types**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: all green: 2230 passed, 6 skipped.

Then from `infra/`: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: all green: 168 passed.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/runbook.md docs/superpowers/specs/2026-10-10-c2a-scorecard-intraday.md
git commit -m "docs: scorecard, intraday runs and the posture alert

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Xh7zur22CRYEGaDoTB5nkV"
```

---

## Self-review (done while writing)

**Spec coverage.**

| Spec item | Task |
|---|---|
| Scorecard: which picks (ok and partial runs, lookback, `run_status`) | 5 (`ScorecardRun.entries`), 2 (`picks_between`) |
| Prices: entry, `price_at_pick`, `ret_1d/5d/20d`, `ret_0d`, MFE/MAE, invalidation, expired return, live window | 3 (`score_pick`) |
| `PickOutcome` (strict, finite, fields, status rules, `updated_at`), idempotent overwrite, finals skipped | 2, 3, 5 |
| Daily summary (counts by kind and side, 1d and 5d hit rate and mean, buckets) and its alert | 3 (`summarize`, `summary_text`), 5 |
| Scorecard failures (pending with a note, half → partial, else failed) | 5 |
| Store additions | 2 |
| `traded` from the event log, `null` when unreadable | 3 (`traded_from_logs`), 5 (`event_logs`) |
| Intraday flow: lock, session open, `last_start`, ok posture, META, collect, posture, stand aside, delta candidates, screen, dives, rank, write, alert rule | 6 |
| "Held" = ledger (read-only) + pinned; `Query` on `POS#` and `LOG#` | 5 (`BotState`), 6, 8 |
| Intraday sizes (`max_candidates`, history bounds, `max_run_s` 600, lock and time box) | 1, 4, 6 |
| Dive prompt line; swing coerced with a note | 6 |
| Settings and validators (`intraday`, `scorecard`, `intraday_model`, `intraday_run_usd`) | 1 |
| Intraday Schwab limiter 20/min | 6 |
| Missing-posture alert (where, trigger, effect, not when stale) and the runbook row | 7 |
| Infra: two schedules with toggles, overrides, state-table read, `TRAIDER_STATE_TABLE` and `TRAIDER_STATE_NAMESPACE`, scheduler role unchanged | 8 |
| CLI: `--kind {premarket,intraday,scorecard}`, `--dry-run`, `--force` meanings | 5, 6 |
| Testing list, including the four spec breaks | 3 (side), 6 (loosen, no morning posture), 7 (chosen stand aside) |
| Not verified | 9 (README, runbook, spec) |

**Placeholders:** none. Every code step shows the code; every command shows its expected result, taken from the replay.

**Type consistency:** names used across tasks were checked against the code that ran: `RunBase`, `run_locked`, `_Run`, `RunDeps.state`, `RunOutcome.extra`, `BotState`, `held_symbols`, `bot_state`, `outcome_key`, `PickOutcome.key`, `Scored.matured`, `latest_ok_posture`, `INTRADAY_LINE`, `INTRADAY_SCHWAB_MAX_PER_MINUTE`, `RESEARCH_KINDS`, `posture_missing`, `posture_alert_after_open_min`.

**Risks the implementer should know:**
- Task 4 is a pure refactor. If the fingerprint differs, stop and find out why; do not edit the existing tests to make them pass.
- `infra/Pulumi.example.yaml` is parsed by an infra test that uncomments every line starting with `  # traider:`; a wrapped comment line must not start with a setting name.
- Run the intraday CLI and wiring tests with the fake servers only; nothing in this plan needs network access.
