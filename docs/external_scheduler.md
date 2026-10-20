# External scheduler for the trading workflows

**Why.** GitHub delivers this repo's `schedule` events 4-5 hours late. During Stage 1 every
morning job landed in a burst around 17:10-18:20 UTC whatever its cron said: research
(meant for 07:30 CT) ran around 12:30 CT, the opening batch around 13:00 CT, and paper
`decide-and-execute` around 14:00 ET with about 1.5 hours to spare before its 15:30 ET
cutoff. The delay happens before any code runs, so it can't be fixed in code.

**How.** An external scheduler calls GitHub's `workflow_dispatch` API at the exact minute.
GitHub starts a dispatched run within seconds. Each workflow takes a `job` input and runs
only that job. Since 2026-10-08 the intraday `preflight`, `research`, `preopen`,
`open-batch` and `cycle` jobs have **no GitHub cron at all**: cron-job.org is their only
trigger (see "The late cron copies" below for why). Every other job keeps its cron as a
fallback and tolerates a late duplicate: `eod-review` runs once per day, `decide` has its
own cadence guard, and the rest are safe to repeat.

The steps below use [cron-job.org](https://cron-job.org), which is free, runs each job in a
timezone you set (so daylight saving is handled for you, with no twice-yearly edits), and
can send the POST with headers and a body that the API needs. Any scheduler that can send
an authenticated POST at set times works the same way.

---

## Step 0 -- check your GitHub Actions minutes first

The repo is **private**, so Actions minutes are metered: 2,000 a month on GitHub Free,
3,000 on Pro. Past that, runs either stop or are billed at about $0.008 a minute, depending
on your spending limit (**Settings -> Billing and plans -> Plans and usage**).

A rough estimate of what the schedule below uses, per trading day:

| what | minutes/day |
|---|---|
| `cycle`, 12 runs at ~3 min each (checkout, `uv sync`, claude CLI install, run) | ~36 |
| other intraday jobs (preflight, research, preopen, open-batch, carry, eod-review) | ~20 |
| paper `decide-and-execute`, `mark` (x2 sleeves every 30 min), `report` | ~45 |
| **total** | **~100** |

That's about **2,100 minutes a month**: around GitHub Free's allowance, inside Pro's.
(Cycles ran every 20 minutes until 2026-10-06, about 2,500 minutes a month.) The `monitor` loop would add ~385 minutes a day (~8,000 a month), which is why it
has no schedule (owner decision 2026-09-27; see "The monitor loop" at the end). Late
fallback-cron runs add a little on top: fewer since 2026-10-08, when five intraday crons
were removed (about six late copies a trading day).

---

## Step 1 -- create a GitHub token that can only trigger workflows

1. GitHub -> your avatar -> **Settings -> Developer settings -> Personal access tokens ->
   Fine-grained tokens -> Generate new token**.
2. **Token name:** `semi-mc scheduler`. **Expiration:** your choice (a year is the maximum
   GitHub suggests). Put a reminder in your calendar for the week before it expires; an
   expired token fails quietly on the scheduler's side.
3. **Resource owner:** `bchen9996`. **Repository access:** *Only select repositories* ->
   `semi-mc`.
4. **Permissions -> Repository permissions -> Actions: Read and write.** Leave everything
   else at *No access*. (Metadata: Read-only is added automatically and is required.)
5. **Generate token** and copy it. You won't see it again. It goes only into the
   scheduler, never into the repo.

## Step 2 -- test the token with one read-only job

The paper `report` job only reads the ledger, so it's the safe one to test with. From Git
Bash, with your token pasted in place of `YOUR_TOKEN`:

```bash
curl -i -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/bchen9996/semi-mc/actions/workflows/paper_trading.yml/dispatches \
  -d '{"ref":"main","inputs":{"job":"report"}}'
```

Expect `HTTP/2 204` with an empty body. Then open the repo's **Actions** tab. A new
*Paper trading* run should appear within seconds, triggered by `workflow_dispatch`, with
**only** the `report` jobs running and every other job skipped. A 403 or 404 means the
token's repository access or Actions permission is wrong (Step 1, items 3-4).

## Step 3 -- create the scheduler jobs

On cron-job.org: **Sign up -> Cronjobs -> Create cronjob**, once per row below. Every job
uses the same request settings:

- **URL:** `https://api.github.com/repos/bchen9996/semi-mc/actions/workflows/<WORKFLOW>/dispatches`
- **Advanced -> Request method:** `POST`
- **Advanced -> Headers:**
  - `Accept: application/vnd.github+json`
  - `Authorization: Bearer YOUR_TOKEN`
  - `X-GitHub-Api-Version: 2022-11-28`
  - `Content-Type: application/json`
- **Advanced -> Request body:** `{"ref":"main","inputs":{"job":"<JOB>"}}`
- **Advanced -> Timezone:** as listed below. This is what handles daylight saving.
- **Schedule:** *Custom* -> the crontab expression below (minute hour day month weekday).
- **Notifications:** turn on "notify on failure", so an expired token or an API error
  emails you.

**Intraday** -- workflow `intraday_trading.yml`, timezone **America/Chicago**:

| job | time (CT) | crontab | note |
|---|---|---|---|
| `preflight` | 06:45 | `45 6 * * 1-5` | |
| `research` | 07:30 | `30 7 * * 1-5` | runs once per day |
| `preopen` | 08:15 | `15 8 * * 1-5` | |
| `open-batch` | 08:32 | `32 8 * * 1-5` | 2 min after the open, so pretrade sees the market open; runs once per day |
| `cycle` | every 30 min, 09:00-14:30 | `*/30 9-14 * * 1-5` | the open-batch covers 08:30; every 20 min until 2026-10-06 |
| `carry` | 14:45 | `45 14 * * 1-5` | |
| `eod-review` | 15:15 | `15 15 * * 1-5` | one Reviewer call per day |
| `weekly-review` | Fri 15:45 | `45 15 * * 5` | |

**Paper** -- workflow `paper_trading.yml`, timezone **America/New_York**:

| job | time (ET) | crontab | note |
|---|---|---|---|
| `decide-and-execute` | 10:05 | `5 10 * * 1-5` | execution window opens at 10:00 ET |
| `mark` | every 30 min, 10:30-15:30 | `*/30 10-15 * * 1-5` | the 10:00 slot is skipped: nothing to mark before decide runs |
| `report` | 17:30 | `30 17 * * 1-5` | |

The forecast journal (`forecast_journal.yml`) doesn't need an entry. It runs in the
evening, and even a 5-hour delay lands before the next open, which is all it requires.

Market holidays need nothing special. Every job's pretrade check declines on a closed
market, the same as it does today.

Early closes (13:00 ET, 12:00 CT) need nothing special either, since 2026-10-16. The
`carry` at 14:45 CT comes after the bell and does nothing on a closed market, so the
last `cycle` whose successor would land less than 15 minutes before the close (the
11:30 one) runs carry itself instead of calling the Trader (`jobs.py::
early_close_before_next_cycle`, from Alpaca's calendar). The owner also added one-off
`carry` runs at 11:45 CT on 2026-11-27 and 2026-12-24 as a backstop for the real account.

**A `carry` dispatched more than 60 minutes before the close does nothing** (since
2026-10-19; `run_carry`'s `near_close_only`): a warning incident, no orders. On
2026-10-19 one of the 11:45 CT backstops fired on a regular day and closed AVGO more
than three hours early. So a mistimed carry job is now harmless, but it is still worth
fixing, since each mistimed run logs a warning. The placebo's fake calendar always closes
at 16:00 ET, so on an early close it follows the real cycle instead (since 2026-10-20): it
runs its own carry in the cycle where the real one hands over, and declines afterward,
when the real cycle finds the market closed.

## Step 4 -- verify on the first trading day

- **During the session:** the Actions tab should show `workflow_dispatch` runs at the
  table's times, each running one job.
- **After the close:** run the readiness report (`tasks/todo.md` has the exact command). Its
  "first activity (UTC)" column should read about 11:45 (06:45 CT), not 17:29. The monitor
  column should show checks about 30 minutes apart (each cycle records one).
- **On cron-job.org:** each job's execution history should show HTTP 204. Anything else is
  a failed trigger.

## The late cron copies

The original crons fire hours late. They were meant as a harmless fallback, but on
2026-10-07 the 12:00 CT `cycle` dispatch never ran, though cron-job.org shows it was
sent: the likely cause is GitHub's concurrency rule. A group holds at most one *pending*
run, and when another arrives GitHub cancels the older one, silently. The late copies of
the morning jobs land mid-session (18:25-21:07 UTC on every day of the week before), so
one waiting in `intraday-trading-ledger-write` can knock out an on-time dispatch, and a
late `cycle` copy adds an unscheduled Trader call (one opened a trade on 2026-10-07, six
minutes after the on-time cycle had passed).

**Removed 2026-10-08 (owner decision): the crons for `preflight`, `research`, `preopen`,
`open-batch` and `cycle`.** cron-job.org is now their only trigger, so if it fails those
jobs don't run that day: its failure notifications (Step 3) are the alarm. Kept: `carry`,
`eod-review` and `weekly-review`, whose late copies land after the close, when nothing is
pending, and still have work to do if the on-time run failed. The paper workflow keeps
all its crons: it has its own concurrency group, and a late `mark` is harmless.

The same day, a rejected `git push` stopped re-running the job: each job's commit is now
rebased onto the latest main (`.github/scripts/push_ledger.sh`). Paper marks and intraday
cycles fire in the same minutes, so rejections are routine, and re-running a cycle
repeated its Trader call.

## The monitor loop

`monitor` enforces invalidation and time stops every 5 minutes for up to 5.5 hours per run.
`cycle` already runs the same check at the start of every cycle (`intraday/jobs.py::run_cycle`),
so with reliable 30-minute cycles the loop only adds 5-minute granularity between cycles,
and only for **shadow** positions: from Stage 2 on, real stop-loss orders rest at Alpaca and
trigger regardless of any job.

**Chosen 2026-09-27: rely on `cycle`'s check; `monitor` has no schedule.** Shadow stops are
enforced within about 30 minutes. A shadow position can therefore overshoot its stop for up
to ~30 minutes, making Stage 1's stopped-out trades slightly worse than live ones would be:
a conservative bias when deciding on promotion. Zero extra minutes.

The alternatives, if that changes:

- **5-minute loop:** add cron-job.org entries for `monitor` at 08:35 and 14:00 CT (two
  loops cover the session; the loop stops itself at 15:00 CT). About 385 minutes a day.
  **No longer safe as built (2026-10-08):** the loop runs outside the concurrency group,
  and since the grouped jobs rebase instead of re-running, a monitor push landing during
  a cycle now fails that cycle.
- **Single-shot every 5 minutes:** same cadence at roughly 100-150 minutes a day, from one
  scheduler entry (`*/5 8-14 * * 1-5`). This needs a workflow change first: a
  single-check mode for the `monitor` job, and a way to run it that neither collides with
  a cycle's push (outside the group) nor cancels a waiting cycle (inside it, a newer
  pending run replaces the older one).
