# Runbook

How to operate the bot once it is deployed. Commands assume a stack called `dev`
and are run from the `infra/` directory unless they say otherwise; resource names
come from `pulumi stack output`.

- [The control switch](#the-control-switch)
- [Signing in](#signing-in)
- [Alerts and what to do](#alerts-and-what-to-do)
- [Seeing what the bot did](#seeing-what-the-bot-did)
- [Restarting, changing settings, tearing down](#restarting-changing-settings-tearing-down)
- [Research jobs](#research-jobs)
- [Seeding research by hand](#seeding-research-by-hand)
- [Going live](#going-live)
- [First-deploy problems](#first-deploy-problems)

## The control switch

One Parameter Store value decides what the running bot may do. The bot re-reads it
every 10 seconds. It can read the value but has no permission to change it.

| Value | Buys | Sells | Notes |
| --- | --- | --- | --- |
| `halt` | no | no | Working orders are cancelled. Positions are left as they are. |
| `close_only` | no | yes | The strategy can still exit; it cannot enter. |
| `paper` | yes | yes | Only in a stack deployed with `tradingMode: paper`. |
| `live` | yes | yes | Only in a stack deployed with `tradingMode: live`. |

`paper` in a live stack, or `live` in a paper stack, trades nothing and raises an
alert. A switch the bot has been unable to read for a minute counts as `halt`.

**AWS refuses a misspelled value** (`Halt`, `stop`) and the switch keeps its old
value, which may be `live`. Read the command's output, then confirm with
`get-parameter`. The AWS CLI must be using the stack's account and region.

```sh
CONTROL=$(pulumi stack output controlParameter)       # /traider-dev/control

aws ssm get-parameter --name "$CONTROL" --query Parameter.Value --output text
aws ssm put-parameter --name "$CONTROL" --value halt --overwrite
aws ssm put-parameter --name "$CONTROL" --value close_only --overwrite
aws ssm put-parameter --name "$CONTROL" --value live --overwrite
```

A deploy never changes the switch. A new paper stack starts at `paper`; a new live
stack starts at `halt`.

The bot acts on a change normally within 10 to 15 seconds, longer if Schwab or AWS
is slow. Cancelling working orders needs a working Schwab sign-in, and an order
whose placement reply was lost cannot be cancelled by the bot at all: after a
`halt`, look at Schwab's order list yourself.

**`halt` does not sell anything.** There is no "sell everything now" command. To get
out of positions in a hurry: set `halt`, then sell at Schwab yourself. Leave the
switch on `halt` until you have decided what happens next, or the strategy may buy
straight back in.

## Signing in

Schwab's sign-in lasts seven days from the moment you log in, and only a person can
renew it. Signing in early is fine and starts a new seven days. A routine that
works: sign in every weekend.

```sh
pulumi stack output reauthUrl --show-secrets
```

Open the link, log in at Schwab, approve access for the account the bot uses. You
land on a page that says *Signed in* and when the sign-in expires. A bot that was
waiting picks the new sign-in up within a minute; one renewing early, within five. You also get a *Schwab sign-in completed* alert: if
you ever receive one you did not cause, set the switch to `halt` and change your
Schwab password.

The link contains a key. Anyone who has it can start the Schwab login page, though
they would still need your Schwab password to finish. Keep it out of shared places.

What happens as expiry approaches:

- About two days before, and each day after that, the watchdog emails you the link.
- With two hours left, the bot stops opening positions. Exits still work.
- When it lapses, the bot has no market data and cannot trade. **It cannot sell what
  it holds.** It alerts you and waits; nothing needs restarting after you sign in.

`traider:flattenBeforeCloseMin` makes the bot try to sell what it holds before each
close, which shortens the time positions sit unattended. It is an attempt, not a
guarantee: it uses ordinary orders, needs everything an ordinary sell needs (quotes,
the lease, a sign-in, a switch that allows sells), and raises no alert if shares are
left. Check positions after the close.

### Nights, weekends and restarts

Nothing is sold when the task stops at 16:30, on a deploy or on a restart. Working
orders are cancelled on the way down if Schwab can be reached; positions stay, with
no bot watching and no stop order at the broker. In the morning the bot reads the
positions from Schwab and waits for the strategy to say what it wants today.

A restart forgets what was only in memory: a frozen symbol, a strategy error,
cooldowns, and which alerts were sent in the last 15 minutes. The day's order
count, loss halt and sales total are kept. With research on, so is the position
ledger (which positions the bot opened), and the bot reads research again at start-up;
until it has, it opens nothing.

With research on, the bot also sells the intraday positions it opened
`research.intraday_flatten_min` minutes before the close (15 by default). That is an
ordinary sell with the same limits as `flattenBeforeCloseMin`: it needs quotes, the lease,
a sign-in and a switch that allows sells. Swing positions carry overnight like any other.
If the ledger cannot be read the bot cannot tell which positions are intraday, and says so
(*Position ledger not loaded*, below).

### If Schwab will not accept the hosted callback

Sources disagree on whether Schwab allows a callback address other than
`https://127.0.0.1` for an individual developer's app. If the developer portal
refuses the stack's `callbackUrl`, register only `https://127.0.0.1` and switch the
stack to paste mode:

```sh
pulumi config set traider:schwabCallbackUrl https://127.0.0.1
pulumi up
```

The same sign-in link now shows a page with a *Sign in with Schwab* button and a
box. After you log in, the browser tries to open a `https://127.0.0.1/...` address
and shows an error. That is expected: copy the whole address from the address bar
and paste it into the box within about 30 seconds.

The command line can do the same thing without the web page, from the repository
root with the `.env` file from the README:

```sh
uv run --env-file .env traider login
```

## Alerts and what to do

Alerts go to the SNS topic, and from there to the address in `traider:alertEmail`
once you have confirmed the subscription. The same alert is not repeated more than
once every 15 minutes.

| Alert | Meaning | What to do |
| --- | --- | --- |
| Schwab app key and secret not set | The app-credentials secret is empty. | Store them (README, step 3). |
| Schwab sign-in needed | Nobody has signed in, or the sign-in lapsed or was refused. | Sign in. |
| Schwab sign-in expires in N hours | From the daily watchdog. | Sign in when convenient. |
| Schwab sign-in has expired | From the daily watchdog. | Sign in. Check positions at Schwab. |
| Schwab sign-in completed | A new sign-in was stored. | Nothing, unless it was not you: `halt`, change your Schwab password. |
| Schwab sign-in restored | The bot is connected again. | Nothing. |
| Cannot reach Schwab sign-in | For five minutes, token requests have failed or the stored key or sign-in could not be read. | Usually Schwab or the network. Check the logs if it persists. |
| Cannot read the Schwab sign-in | From the daily watchdog: the stored sign-in is unreadable. | Sign in again; that replaces it. |
| The bot's task stopped unexpectedly | The container crashed or could not start. Comes from AWS, not from the bot. | ECS restarts it. If it repeats, read the logs; a bad deploy shows up here at the next 09:00 start. |
| traider started in LIVE mode | A live task started. If the settings table differs from the stack's settings, the alert ends with a line naming the settings (not their values); the table is in force and `pulumi up` does not change it. | Expected after a deploy or the morning start. A drift line is normal after `traider settings apply`; otherwise check `traider settings show`. |
| Control does not match deploy | The switch says `paper` in a live stack, or `live` in a paper one. | Set the switch to the deployed mode, or to `halt`. |
| Daily loss limit hit | The whole account's value is down by more than `max_daily_loss_usd` since the day's first reading (around 09:00). Other holdings and withdrawals count. | No new buys today. Positions are untouched: decide yourself whether to exit. |
| A sale could not be valued | The broker reported a fill with no price. | No new buys today. Check the fill at Schwab. |
| Broker rejected an order on X | Schwab refused the order. | Read the reason. The bot retries after a minute while the strategy still wants the trade; `halt` if it keeps happening. |
| Order on X may or may not have gone through | The reply to an order was lost. | Usually resolves itself: the bot looks the order up and does not resend. Check Schwab's order list if you get no follow-up. |
| X frozen: position mismatch | The account does not show what a confirmed fill should have produced. | The bot stops trading X, but only until its next restart, and the schedule restarts it every morning. Set `halt` until you have reconciled the position at Schwab. |
| Open order on X the bot did not place | Somebody else's order is open on a symbol the bot trades. It is only noticed when the bot wants to trade X. | Cancel it or let it finish. The bot stays out of X meanwhile, sells included. With `cancelUnknownOrders` on, the bot cancels such orders itself, yours too. |
| Order on X is not finishing / Cannot read order status for X | An order has been open for five minutes, or its status has been unreadable for about 30 seconds. | Look at the order at Schwab. Cancel it there if needed. |
| X expires today | The account holds an option on its last day. Sent once, when the bot first sees it that day. | Decide whether to leave it to the bot, which tries to sell in the last hour if it is allowed to trade, or to close it yourself. |
| X expires today and is still held | It is the last hour and the option is still in the account. Repeats while that is so. | One alert is normal: the sale is in progress. If it repeats, the bot is not getting it sold (halt, no bid, stale quote, frozen symbol). Sell it at Schwab or tell Schwab not to exercise it: an option that expires in the money becomes 100 shares per contract. |
| Unpinned symbols are still held | Research off only. A settings version unpinned symbols the bot still holds (shares, or options on them). The alert names them. The bot keeps managing them until they are flat or the next restart (every morning on the schedule); after that it does not. (With research on, held pinned positions are booked in the ledger and stay managed across restarts until they are flat, so there is no alert unless the next row applies.) | Sell them, or pin them again. |
| Unpinned symbols are not in the ledger | Research on only. A settings version unpinned symbols the bot holds that are not in its ledger yet. The bot books held pinned positions at each account read, so usually that write has failed (the log says why); a symbol pinned and unpinned between two reads was never tried. Once unpinned, the bot leaves them alone, sells included. | Pin them again (the bot retries the write) or sell them yourself. Check the state table and the task role. |
| X is held but not managed | Research off only (with research on, the next row covers it). The account holds X but X (or, for an option, its underlying) is not pinned, so the bot does not trade it. For an option that also means no expiry alerts and no sale before it expires. Once per symbol each time the bot starts (every morning on the schedule), and again if the position comes back after going flat. | If the bot bought it before it was unpinned, sell it yourself or pin it again. If the bot did not buy it, ignore this: pinning would hand it to the strategy, which may sell it. |
| X is held but the bot did not open it | With research on, the account holds X but the bot's ledger has no record of buying it, and X is not pinned. | The bot leaves X alone, sells included. Sell it yourself, or pin it if the bot should manage it. Pinning books it in the ledger: from then on it is the bot's until it is flat, even if you unpin it again. To hand it back, unpin it, delete its `POS#<mode>` / `<symbol>` item from the state table, and restart the bot. Keep the bot's account to the bot. |
| Position ledger not loaded | With research on, the bot has not been able to read its ledger (the record of the positions it opened) for two minutes. Sent once per outage. | No new positions open until it loads; exits the strategy asks for still work. Intraday positions are not flattened automatically meanwhile, so check them by hand before the close. Check the state table and the task role; the bot reloads the ledger by itself once it is readable. |
| Position ledger not loaded at the close | The intraday flatten window (`research.intraday_flatten_min` before the close) has opened and the ledger still cannot be read, so the bot will not sell intraday positions. Once per outage. | Act now: sell intraday positions yourself at Schwab, or accept holding them overnight. |
| Strategy error | The strategy raised an exception. | Buys are off until the next restart, which the schedule does every morning. Set `close_only` or `halt`, fix the strategy and deploy. |
| Lost the trading lease | This instance is no longer the one allowed to trade. | Check that exactly one task is running. |
| Engine error | An unexpected error in the loop. | The bot keeps running. Read the logs. |
| Settings version N applied | A new version of the settings is in force; the alert lists each change. | Nothing, if you made it. If you did not, set `halt` and look at `traider settings history`. |
| Settings version N needs a restart | The version changes the strategy, its parameters, the option-chain span or whether options are allowed. Those wait for a restart; the rest applies now. | Restart when convenient (see below), or tomorrow's 09:00 start does it. |
| Settings version N rejected | The newest version does not validate, or (with research off) pins no symbols, so the bot would have nothing to trade. If settings had already loaded, the bot keeps the settings it was running with. If none have loaded in this process, no new positions open until a valid version is written; exits still work. | Fix it with `traider settings apply`, start from the last good version: find it with `traider settings history`, print it with `traider settings show --version N`. |
| Settings not loaded | At start-up the bot could not read the settings table. (A version that is stored but invalid gets the "rejected" alert instead.) No new positions open until it can; exits still work. | Check the settings table and the task role (a missing table or a denied read is the usual cause). The bot loads the settings by itself once they are readable. |
| Settings table unreadable | The settings table has been unreadable for five minutes. If settings had loaded, the last good version stays in force and newer versions, tighter limits included, do not apply. If not, no new positions open. Sent once per outage. | Check the table and the task role. If you need tighter limits now, set the control switch to `close_only` or `halt`. |
| Research is stale | The research table has been unreadable for longer than `research.max_stale_s` (10 minutes by default). | No new positions open; exits still work. Check the research table, the task role and the research jobs. |
| Research readable again | It recovered. | Nothing. |
| Research DATE: ok | The pre-market research run finished. The alert gives the posture, each pick (L long, B bearish, its horizon and score) and the cost. | Nothing. `traider research show` before the open shows what the bot will act on. |
| Research DATE: partial | The run finished, but planned work did not happen: a budget stopped Bedrock calls, a deadline passed, Finnhub failed, the Bedrock posture review failed, Bedrock calls failed in half the deep-dives or more, daily history was unreadable for half the names or more, or the trail could not be written. The alert ends with the reasons. By default the bot ignores a partial run, posture included, so it stands aside today. | Read the reasons. Once the cause has passed, run it again (see [Research jobs](#research-jobs)). To trade on partial runs anyway, set `research.accept_partial_runs`; it applies to every partial run. |
| Research DATE: failed | The run stopped at the stage it names (for example `collect`, `posture` or `dive`; a run that took longer than `research_jobs.max_run_s` plus 9 minutes fails too). It wrote no posture, so the bot stands aside today. | An expired Schwab sign-in is the usual cause: sign in, then run it again. Otherwise read the research logs. |
| The research run stopped with an error | From AWS, not from the run: the research task exited with an error (it failed, it could not start, or another run held the lock). | Read the research logs (below). A run that cannot even start (no Finnhub key stored) writes no run record, so this alert is the only sign; it carries only a stop code and reason, and the cause is in the research logs ("cannot start the research run:"). Missing Bedrock model access does not stop the run: it finishes `partial` with notes. If the task could not start, check the image and the roles. |
| Alarm: the scheduler could not start the research run | The scheduler gave up on starting the task (two retries within 10 minutes) and put the request in a dead-letter queue. No task ran, so the alert above cannot fire. The bot stands aside today. | Read the message, then **purge the queue** (below). If you do not, the alarm stays in ALARM and later failures send no new alert. |
| No research alert by about 08:30 on a trading day | (Unless `research_jobs.enabled` is false.) No summary means the run did not finish, or never ran: the schedule is not enabled (`traider:researchScheduleEnabled`, off by default), the start failed, or the run is stuck. A start that AWS refuses with a failure list may not reach the dead-letter queue (not verified). | Look at the research logs and `traider research show`. Until a run is `ok`, the bot stands aside. |

## Seeing what the bot did

**Logs** are JSON lines in CloudWatch:

```sh
aws logs tail "$(pulumi stack output logGroup)" --follow
aws logs tail "$(pulumi stack output logGroup)" --since 1h
```

**The audit log** is the durable record: every target the strategy set, every order
sent, finished or blocked, and why. It lives in the state table, one partition per
mode and trading day:

```sh
aws dynamodb query --table-name "$(pulumi stack output stateTable)" \
  --key-condition-expression 'pk = :pk' \
  --expression-attribute-values '{":pk":{"S":"LOG#paper#2026-10-08"}}' \
  --query 'Items[].[at.S,kind.S,body.S]' --output text
```

| Event | Meaning |
| --- | --- |
| `target` | The strategy changed what it wants to hold. |
| `order_submitted` | An order went to the broker. |
| `order_done` | An order finished: filled, cancelled, rejected or expired, with fill quantity and price. |
| `order_blocked` | A risk check stopped an order. `codes` says which, for example `max_position_usd`, `unsettled_cash`, or with research on `no_pick` (no live pick for the symbol), `posture` (research says stand aside today) and `horizon_budget` (the intraday or swing budget is used up). Others are `pick_side`, `intraday_closing` and `foreign_holding`, which also blocks sells. |
| `order_rejected` | The broker refused an order. |
| `order_unconfirmed`, `order_adopted` | A reply was lost; later, the order was found at the broker. |
| `unknown_order`, `symbol_frozen`, `entries_halted` | See the matching alerts above. `entries_halted` has a `reason`; `settings not loaded` and `position ledger not loaded` are the ones that come from the matching alerts. |
| `settings_applied` | A new settings version is in force. `diff` lists what changed. |
| `settings_pending_restart` | A new version changes fields that only apply after a restart. `fields` names them. |
| `settings_rejected` | The newest version does not validate, or pins no symbols while research is off. The bot kept the settings it had. |
| `settings_unreadable` | The settings table stayed unreadable for five minutes. `since` says when it began, `detail` what went wrong. Recorded once per outage. |
| `unmanaged_holding` | Research off only. The account holds `symbol` (`quantity`) outside the bot's universe, so the bot does not manage it. Recorded with the alert. With research on, `unknown_holding` takes its place. |
| `unknown_holding` | Research on only. The account holds `symbol` (`quantity`), a position the bot did not open (not in its ledger, not pinned). The bot will not trade it. The ledger tracks symbols, not lots, so shares added by hand to a symbol the bot holds are managed (and flattened) as the bot's. |
| `unpinned_but_held` | A settings `version` unpinned `symbols` the bot still holds. With research on, only those not in the ledger (their booking failed). Recorded with the alert. |
| `universe_changed` | The symbols the bot watches changed: `added`, `dropped` and the full `universe`. Held and busy symbols are never dropped. |
| `research_stale` | Research could not be read for longer than `research.max_stale_s`. No live picks and the posture is stand aside until it can be read; exits are not affected. `detail` says what went wrong. |
| `research_restored` | Research is readable again. |

**The paper account** (cash and positions) is kept in the same table so it survives
restarts. To start it over, set `halt`, delete it and restart the bot. If the old
account was worth more than the new starting cash, the day's loss limit trips at
once; that clears the next day.

```sh
aws dynamodb delete-item --table-name "$(pulumi stack output stateTable)" \
  --key '{"pk":{"S":"PAPER#paper"},"sk":{"S":"ACCOUNT"}}'
```

## Restarting, changing settings, tearing down

**Restart** (needed after a frozen symbol or a strategy error):

```sh
aws ecs update-service --cluster "$(pulumi stack output clusterName)" \
  --service "$(pulumi stack output serviceName)" --force-new-deployment
```

The old task is stopped before the new one starts. On the way down the bot cancels
its working orders and hands back the lease; positions are kept. The new task reads
positions from the broker, replays recent bars through the strategy and carries on.

**Change a setting:** settings are versioned in the settings table, and the running
bot picks up a new version within about 10 seconds. With the `localEnv` output
loaded:

```sh
uv run --env-file .env traider settings show > settings.json
# edit settings.json
uv run --env-file .env traider settings apply settings.json --note "why"
uv run --env-file .env traider settings history
```

Limits, order settings, flattening, the research settings and the pinned symbols apply at
once: the bot watches a newly pinned symbol straight away, its feed loads recent bars for
it and subscribes, and the strategy hears it after that warm-up. With research off, a
symbol you unpin while the bot holds it (or options on it) stays managed until it is sold or
the bot next restarts (every morning on the schedule); sell it first or keep it pinned. With
research on, the bot books what it holds on pinned symbols in its position ledger, so an
unpinned holding stays managed, across restarts, until it is flat. With research off, a
version that pins no symbols is rejected. The strategy, its parameters, the
option-chain span and `allow_options` wait for a restart.
`show` prints only the JSON on stdout (the version line goes to stderr), so the file
can be given straight back to `apply`. To go back, print an older version and apply it
as a new version:

```sh
uv run --env-file .env traider settings history
uv run --env-file .env traider settings show --version 3 > old.json
uv run --env-file .env traider settings apply old.json --note "back to 3"
```

Stack settings in Pulumi only seed version 1 when the table is empty; after that the
table wins.
If the newest version is damaged and has no readable version number,
`traider settings apply` refuses and tells you to delete that item from the settings
table first.

**Change the code:** edit, then `pulumi up`. Same stop-then-start.

**Switch research off** (`traider:research` from `true` to `false`, then `pulumi up`): the
bot restarts on the old rules, with no ledger, and manages only holdings in pinned
symbols. A position it opened from a pick is not pinned, so after the restart it gets
*X is held but not managed* (`unmanaged_holding`) and the bot leaves it alone, sells
included. **Sell or pin those positions first.** With research off the stack needs pinned
symbols, and a settings version with none is rejected. On a paper stack `pulumi up` also
deletes the research table, with every pick and posture in it (deletion protection is
live-only). On a live stack the table is protected against deletion, so lift that first
as under *Remove everything* below, with `pulumi stack output researchTable`.

**Stop for a while:** set the switch to `halt`. Scaling the service to zero by hand
does not last with the default schedule, which starts it again at 09:00 New York
time on the next weekday.

**Remove everything:** `pulumi destroy`. The two secrets enter AWS's 30-day recovery
window. A live stack's state and settings tables (and research table, with research on)
are protected against deletion; lift that first:

```sh
aws dynamodb update-table --table-name "$(pulumi stack output stateTable)" \
  --no-deletion-protection-enabled
aws dynamodb update-table --table-name "$(pulumi stack output settingsTable)" \
  --no-deletion-protection-enabled
# With research on:
aws dynamodb update-table --table-name "$(pulumi stack output researchTable)" \
  --no-deletion-protection-enabled
```

## Research jobs

The pre-market research run reads the market at 08:00 New York time on weekdays and
writes the day's posture and ranked picks (README, "The pre-market research run"). Its
picks feed a strategy you choose; this is not financial advice. It is opt-in, and its
schedule is created disabled: nothing runs on its own until step 4. Do these in order.

**1. Bedrock model access.** In the AWS console, in the stack's region, open Amazon
Bedrock and request access to the Claude model in `research_jobs.dive.model`
(`anthropic.claude-sonnet-5-5` by default; whether your account can use it is not
verified). If it cannot, change `research_jobs.dive.model` and
`research_jobs.dive.posture_model` to a model it can use, and add that model's price to
`research_jobs.budget.prices` (a model without a price above zero is never called). Check
the default prices against AWS's Bedrock price list either way: the budgets are only as good
as those numbers. Settings change with `traider settings show` and `traider settings apply`
(see [Restarting, changing settings, tearing down](#restarting-changing-settings-tearing-down)).

**2. Deploy and store the Finnhub key.** Create a free account at finnhub.io and copy the
API key from its dashboard. The secret that holds it is created by the same `pulumi up`
that creates the (disabled) schedule. From `infra/`:

```sh
pulumi config set traider:research true
pulumi config set traider:researchJobs true
pulumi up
read -rs FINNHUB_KEY        # paste the key, press Enter; nothing is shown or saved
printf '{"api_key": "%s"}' "$FINNHUB_KEY" | aws secretsmanager put-secret-value \
  --secret-id "$(pulumi stack output finnhubSecretArn)" --secret-string file:///dev/stdin
unset FINNHUB_KEY
```

The key goes in that command and nowhere else: not in a file, a Pulumi setting, a chat or a
ticket. Piping it in keeps it out of the command line (`printf` is a shell built-in, so it
is not in the process list either); typing it into `--secret-string` directly would show it
there briefly, which is acceptable on a personal machine but worse. If it ever leaks (pasted
anywhere else, even by accident), make a new one at Finnhub and store it the same way. A run
with no key stored does not start (exit 1, no run record); the stopped-with-an-error alert
shows only a stop code and reason, and the research logs say "cannot start the research run:".

**3. A dry run.** With the `localEnv` output loaded (README, step 5;
reload it, it now carries the Finnhub secret) and AWS credentials that can read the secrets
and tables and call Bedrock, and a valid stored Schwab sign-in (sign in first if it has
lapsed), from the repository root:

```sh
uv run --env-file .env traider research run --kind premarket --dry-run
```

It makes every real call, Bedrock included, so it costs about what a real run costs
($1-2, an estimate). It writes nothing to the research table (no picks, posture, cost or
lock) and sends no alert, but like the bot it may save a rotated Schwab sign-in. It prints
the posture and the picks as JSON and leaves the trail in `./research-trail`
(`--trail-dir` to change that). This is the first time the run meets the real Schwab,
Finnhub and Bedrock. What the first dry run may show:

- **Every pick refused as `halted`:** the run counts a missing or non-"Normal"
  `securityStatus` as halted (fail closed), and what Schwab reports before the open is not
  verified.
- **Few or no candidates:** movers or quotes at 08:00 may not reflect pre-market trading,
  or Finnhub's free tier may lack the earnings calendar or answer it empty (then the run
  is `partial`, with no swing picks). Each swing idea also gets its own earnings call for
  its symbol; if that fails, the note "earnings check failed" says which picks were
  refused (`earnings_unknown`).
- **Everything dropped as `stale_history`, or posture `stand_aside` with a note about SPY:**
  daily bars whose last bar is more than 3 weekdays old are dropped, and stale SPY bars mean
  `stand_aside`. Malformed bars are dropped too.
- **A Bedrock problem:** no model access, an id the region does not serve, or tool use the
  endpoint does not support. The run does not stop: it finishes `partial` (a failed posture
  review, or failed model calls in half the deep-dives or more), and the JSON's notes and
  counts say so. The bot would stand aside. Each failed attempt is counted against the
  budget; there are no retries.

**4. Enable the schedule.** Once a dry run looks right, from `infra/`:

```sh
pulumi config set traider:researchScheduleEnabled true
pulumi up
```

From then on the run fires every weekday at 08:00 New York time. Setting it back to false
(and `pulumi up`) pauses the schedule and keeps everything else.

**What the bot does with each outcome**

| Run | Posture and picks |
| --- | --- |
| `ok` | used |
| `partial` | ignored unless `research.accept_partial_runs` is on, posture included: the bot stands aside |
| `failed` | none written: the bot stands aside |
| skipped (`research_jobs.enabled` is false) | none written: the bot stands aside |
| no session that day | nothing runs and nothing is written |

**Time and money limits.** Past `research_jobs.max_run_s` (20 minutes) before the screen,
the run writes the posture and no picks, as `partial`. Past it during the deep-dives, no
new dive starts and unfinished ones are cut short, as `partial`. A run still going 9
minutes after `research_jobs.max_run_s` fails. The cost meter stops all model calls once a call overruns
its reservation or a budget would be exceeded, and estimates include a 1000-token allowance
for tools.

**Running it again.** If the scheduler cannot start the task it retries twice within 10
minutes; a run that failed after starting is not repeated by the schedule (a late
pre-market run is not wanted). A run is skipped if an `ok` or `partial` one already
finished today, and only one runs at a time (a second exits with code 2). After fixing the
cause, run it from your machine with the `localEnv` output loaded; without `--dry-run` it
writes picks for the bot, and its trail stays on your machine.

```sh
uv run --env-file .env traider research run --kind premarket
# after a partial run, to replace it:
uv run --env-file .env traider research run --kind premarket --force
```

**Not while the bot trades.** Research signs in as the same Schwab app as the bot, so
they share Schwab's per-app request quota. Do not start a run without `--dry-run` while a
live bot is trading (and keep dry runs out of market hours too: they make the same calls).
Research holds itself to 40 Schwab requests a minute, so a run spends a few minutes on
Schwab calls alone; the scheduled run at 08:00 finishes well before the open.

**A start that failed.** Read the dead-letter queue, then empty it. The queue is
`<prefix>-research-schedule-dlq`, and the `researchCluster` output is `<prefix>-research`:

```sh
QUEUE="$(aws sqs get-queue-url --queue-name "$(pulumi stack output researchCluster)-schedule-dlq" \
  --query QueueUrl --output text)"
aws sqs receive-message --queue-url "$QUEUE" --max-number-of-messages 10 \
  --attribute-names All --message-attribute-names All
aws sqs purge-queue --queue-url "$QUEUE"
```

The message attributes should say why the scheduler gave up (not verified). Typical causes
are a deleted image, a changed role or a full subnet. Purging is required: the alarm
watches the queue's depth, so a message left in it keeps the alarm in ALARM and later
failures send no alert.

**Where to look.** Logs are in the `researchLogGroup` output's log group. The trail is in
the `researchBucket` output's bucket, one folder per run, `runs/<date>/<run id>/`:
`snapshot.json` (what it saw), `posture.json`, `screen.json` (every candidate, its
features and why it was dropped), `dives/<symbol>.json` (each conversation with the model)
and `result.json` (assessments, why each was refused, the picks).

```sh
aws logs tail "$(pulumi stack output researchLogGroup)" --since 2h
aws s3 ls "s3://$(pulumi stack output researchBucket)/runs/$(date +%F)/" --recursive
```

Error and alert text is scrubbed of keys and tokens. **Never turn on
botocore DEBUG logging** (for example `boto3.set_stream_logger`) where logs are kept: it
prints secret values.

**Good to know.** Earnings-date expiry counts weekdays, not market holidays, so a swing
pick's expiry can land on a holiday.

**Not verified yet:**
- Bedrock Mantle tool use and forced `tool_choice`, the IAM action names, and model access.
- Schwab pre-market movers and quotes, and the `securityStatus` values.
- Whether Schwab hands out a second access token while the bot's is still valid (research
  keeps its own and saves a rotated sign-in the way the bot does).
- Finnhub free-tier coverage.
- The token prices.
- Scheduler, ECS and dead-letter-queue behaviour on real AWS.

**Switching it off:** set `research_jobs.enabled` to false with `traider settings apply`
(the next run exits without a posture, so the bot stands aside), or set
`traider:researchScheduleEnabled false` and `pulumi up` to pause the schedule, or
`traider:researchJobs false` (and `traider:researchScheduleEnabled` unset or false) and
`pulumi up` to remove it. On a live stack,
`pulumi destroy` cannot remove the trail bucket until you empty it:
`aws s3 rm "s3://$(pulumi stack output researchBucket)" --recursive`.

## Seeding research by hand

For paper testing without the research run. It needs a stack with
`traider:research: true`, which creates the research table and is off by default. The
`localEnv` output carries `TRAIDER_RESEARCH_TABLE` **only when `traider:research` is
true**; on a stack with research off the variable is missing and `traider research` prints
`TRAIDER_RESEARCH_TABLE is not set` and exits 2. Load the output as in the README (step 5), then write a file like this:

```json
{
  "posture": {"level": "trade", "reasons": ["quiet macro calendar"]},
  "picks": [
    {"symbol": "NVDA", "side": "long", "horizon": "intraday", "score": 80,
     "thesis": "why", "invalidation": "100"}
  ]
}
```

```sh
uv run --env-file .env traider research seed picks.json
uv run --env-file .env traider research show
```

`seed` checks the whole file and writes nothing if any part is wrong. It writes one run
named `manual-<UTC time>-<4 hex digits>`; picks rank in list order, and `pre_score` defaults to `score`.
Unless you give `expires_at` (an ISO time with a timezone), an intraday pick expires at
today's 16:00 New York time and a swing pick at 16:00 five weekdays later (holidays are
not skipped). `side` is `long` or `bearish`, `horizon` is `intraday` or `swing`,
`level` is `trade`, `reduced` or `stand_aside`.

**The bot trades only on a day that has a posture.** A file with picks and no `posture` adds
the picks but no posture, so unless an earlier seed already wrote one today, the bot stands
aside. The bot picks the new run up within `research.poll_s` (60 seconds by default).

**Seeding again adds to what is there; it does not replace it.** Every seed is a run of its
own, and the bot reads all of today's runs (and the unexpired swing picks of earlier days).
For each symbol the pick with the **highest score** wins, so a later seed can add symbols or
raise a score but cannot remove a pick or lower one. Only the posture is replaced: the
**latest** one written wins, whatever its level. To stop new entries, seed
`{"posture": {"level": "stand_aside"}}`. To take a pick away, delete its item from the
research table (partition key `DAY#<date>`, sort key `PICK#<run id>#<rank>`, for example
`PICK#manual-20261009T134500Z-3f2a#001`), or let it expire.

```sh
# The table is named in TRAIDER_RESEARCH_TABLE in the localEnv output: <prefix>-research.
aws dynamodb delete-item --table-name traider-dev-research \
  --key '{"pk":{"S":"DAY#2026-10-09"},"sk":{"S":"PICK#manual-20261009T134500Z-3f2a#001"}}'
```

**`show` reads the table the way the bot does, but with the research settings from the
`TRAIDER_*` environment** (`TRAIDER_RESEARCH`, which the stack sets from `researchSettings`),
**not the settings table the live bot uses.** It prints the posture and the live picks, and
exits 1 if the table cannot be read. A pick below `research.min_score` is written but not
shown, because the bot ignores it. After a `traider settings apply` that changes
`research.min_score` or `research.accept_partial_runs`, `show` can disagree with the bot
until the stack's `researchSettings` match.

To check it from the bot's side, look for a `universe_changed` event naming the symbols
(see [Seeing what the bot did](#seeing-what-the-bot-did)) and for `order_blocked` events with
`no_pick`, `posture` or `horizon_budget` in `codes`.

## Going live

Use **one stack per Schwab login**. A second sign-in cancels the first, and Schwab
allows one streaming connection, so a paper stack and a live stack on the same
login would keep knocking each other out. Going live means switching the stack you
have.

Before you switch:

- [ ] It has paper traded through several whole sessions, and you have read the
      audit log for those days and agree with every order.
- [ ] You have seen it handle a restart, a `halt`, and a sign-in renewal.
- [ ] `traider check` passes during market hours, and you have read every line of
      it: the account it picked, the cash it may spend, the account type.
- [ ] **The account holds no shares of the pinned symbols that you want to keep.** The
      bot treats the whole position in each pinned symbol as its own, and will sell it
      when the strategy says to hold none.
- [ ] Nothing else trades the pinned symbols in that account.
- [ ] With research on: something writes a posture every morning (the research run with
      `traider:researchJobs` and `traider:researchScheduleEnabled`, finishing `ok`;
      otherwise the bot stands aside every day), and the account holds nothing the bot did not buy.
      A holding it did not open is left alone, but it is also a sign the account is not
      the bot's alone, and the bot cannot tell your shares from its own in a symbol it
      already holds. Check with `traider research show` before the open that the posture
      and picks are what you expect.
- [ ] If options are on: the account has options approval at Schwab, it holds no
      options on the configured symbols that you want to keep through their last
      day, and you have read [Options](../README.md#options).
- [ ] You have chosen the limits on purpose: `max_order_usd`, `max_position_usd`,
      `max_total_exposure_usd`, `max_daily_loss_usd`, `max_orders_per_day`.
- [ ] Alerts reach you, on a device you will have with you.
- [ ] You know how to set `halt` from your phone.

Then, switch first:

```sh
aws ssm put-parameter --name "$(pulumi stack output controlParameter)" --value halt --overwrite
aws ssm get-parameter --name "$(pulumi stack output controlParameter)" --query Parameter.Value --output text
pulumi config set traider:tradingMode live
pulumi config set traider:accountLast4 1234     # last four digits of the account
pulumi up
```

A deploy never touches the switch, so whatever it said before, it says now: that is
why it goes to `halt` first. The bot restarts in live mode and does nothing. When
you are ready:

```sh
aws ssm put-parameter --name "$(pulumi stack output controlParameter)" --value live --overwrite
```

Start with limits small enough that the worst day is an amount you would shrug at.
Watch the first orders at Schwab as they happen.

To go back to paper, set the switch to `halt`, change `tradingMode` back, deploy,
then set the switch to `paper`.

## First-deploy problems

Nobody has run this deployment yet, so expect a few of these.

- **`pulumi up` cannot find or run the Python program.** The project uses Pulumi's
  `uv` toolchain. Update the Pulumi CLI, and check that `uv` is on your PATH.
- **The image build fails with `exec format error`.** Your machine cannot build ARM
  images. Either install emulation (`docker run --privileged --rm tonistiigi/binfmt
  --install arm64`) or `pulumi config set traider:cpuArchitecture X86_64`.
- **AWS rejects a resource argument.** Possible, since only mocks have checked
  them. The error names the resource; the fix is usually one line in `infra/`. Run
  `uv run pytest` in `infra/` afterwards.
- **The task starts and stops in a loop.** Look at the logs for `configuration
  error`. `pulumi preview` checks the settings with the bot's own code, so this
  should not happen; if it does, the two have drifted.
- **No alert emails.** Confirm the SNS subscription from the email AWS sent, and
  check spam.
- **`traider check` fails on sign-in.** The app is probably still *Approved -
  Pending*, or the callback registered with Schwab differs by a character from
  `callbackUrl`.
- **`traider check` fails on quotes with "delayed".** The account or app lacks
  real-time market data. The bot will not trade on delayed quotes.
- **Sign-in page says the link has expired.** Complete the Schwab login within ten
  minutes of opening the link.
