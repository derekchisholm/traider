# Runbook

How to operate the bot once it is deployed. Commands assume a stack called `dev`
and are run from the `infra/` directory unless they say otherwise; resource names
come from `pulumi stack output`.

- [The control switch](#the-control-switch)
- [Signing in](#signing-in)
- [Alerts and what to do](#alerts-and-what-to-do)
- [Seeing what the bot did](#seeing-what-the-bot-did)
- [Restarting, changing settings, tearing down](#restarting-changing-settings-tearing-down)
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
count, loss halt and sales total are kept.

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
| traider started in LIVE mode | A live task started. | Expected after a deploy or the morning start. |
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
| Strategy error | The strategy raised an exception. | Buys are off until the next restart, which the schedule does every morning. Set `close_only` or `halt`, fix the strategy and deploy. |
| Lost the trading lease | This instance is no longer the one allowed to trade. | Check that exactly one task is running. |
| Engine error | An unexpected error in the loop. | The bot keeps running. Read the logs. |

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
| `order_blocked` | A risk check stopped an order. `codes` says which, for example `max_position_usd` or `unsettled_cash`. |
| `order_rejected` | The broker refused an order. |
| `order_unconfirmed`, `order_adopted` | A reply was lost; later, the order was found at the broker. |
| `unknown_order`, `symbol_frozen`, `entries_halted` | See the matching alerts above. |
| `settings_applied` | A new settings version is in force. `diff` lists what changed. |
| `settings_pending_restart` | A new version changes fields that only apply after a restart. `fields` names them. |
| `settings_rejected` | The newest version does not validate. The bot kept the settings it had. |

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

**Change a setting or the code:** edit, then `pulumi up`. Same stop-then-start.

**Stop for a while:** set the switch to `halt`. Scaling the service to zero by hand
does not last with the default schedule, which starts it again at 09:00 New York
time on the next weekday.

**Remove everything:** `pulumi destroy`. The two secrets enter AWS's 30-day recovery
window. A live stack's state table is protected against deletion; lift that first:

```sh
aws dynamodb update-table --table-name "$(pulumi stack output stateTable)" \
  --no-deletion-protection-enabled
```

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
- [ ] **The account holds no shares of the configured symbols that you want to
      keep.** The bot treats the whole position in each configured symbol as its
      own, and will sell it when the strategy says to hold none.
- [ ] Nothing else trades the configured symbols in that account.
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
