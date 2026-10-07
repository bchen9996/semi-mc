# External scheduler for the trading workflows

**Why.** GitHub delivers this repo's `schedule` events 4-5 hours late. During Stage 1 every
morning job landed in a burst around 17:10-18:20 UTC whatever its cron said: research
(meant for 07:30 CT) ran around 12:30 CT, the opening batch around 13:00 CT, and paper
`decide-and-execute` around 14:00 ET with about 1.5 hours to spare before its 15:30 ET
cutoff. The delay happens before any code runs, so it can't be fixed in code.

**How.** An external scheduler calls GitHub's `workflow_dispatch` API at the exact minute.
GitHub starts a dispatched run within seconds. Each workflow takes a `job` input and runs
only that job. The crons stay in place as a fallback, and the jobs tolerate a late duplicate:
`research`, `open-batch` and `eod-review` run once per day, `decide` has its own cadence
guard, a `cycle` that lands after the close declines quietly on the real market clock (no
Trader call, an info line instead of an error), and the rest are safe to repeat.

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
| `cycle`, 18 runs at ~3 min each (checkout, `uv sync`, claude CLI install, run) | ~55 |
| other intraday jobs (preflight, research, preopen, open-batch, carry, eod-review) | ~20 |
| paper `decide-and-execute`, `mark` (x2 sleeves every 30 min), `report` | ~45 |
| **total** | **~120** |

That's about **2,500 minutes a month**: a few dollars over GitHub Free's allowance, inside
Pro's. The `monitor` loop would add ~385 minutes a day (~8,000 a month), which is why it
has no schedule (owner decision 2026-09-27; see "The monitor loop" at the end). Late
fallback-cron runs add a little on top.

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
| `cycle` | every 20 min, 09:00-14:40 | `*/20 9-14 * * 1-5` | the open-batch covers 08:30 |
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

## Step 4 -- verify on the first trading day

- **During the session:** the Actions tab should show `workflow_dispatch` runs at the
  table's times, each running one job.
- **After the close:** run the readiness report (`tasks/todo.md` has the exact command). Its
  "first activity (UTC)" column should read about 11:45 (06:45 CT), not 17:29. The monitor
  column should show checks about 20 minutes apart (each cycle records one).
- **On cron-job.org:** each job's execution history should show HTTP 204. Anything else is
  a failed trigger.

## The late cron copies

The original crons still fire, hours late. The once-per-day guards make most of those
no-ops. The one kind that still does work is a late `cycle` cron: an extra Trader cycle
mid-afternoon, capped by the daily budget, costing a few minutes of runner time and about
a cent of model spend each. Once the external triggers have run cleanly for about a week,
consider removing the `cycle` cron from `intraday_trading.yml` (its `if:` condition keeps
working for dispatch without it). The single-fire jobs' crons are cheap to keep as a
fallback. `monitor` already has no cron.

## The monitor loop

`monitor` enforces invalidation and time stops every 5 minutes for up to 5.5 hours per run.
`cycle` already runs the same check at the start of every cycle (`intraday/jobs.py::run_cycle`),
so with reliable 20-minute cycles the loop only adds 5-minute granularity between cycles,
and only for **shadow** positions: from Stage 2 on, real stop-loss orders rest at Alpaca and
trigger regardless of any job.

**Chosen 2026-09-27: rely on `cycle`'s check; `monitor` has no schedule.** Shadow stops are
enforced within about 20 minutes. A shadow position can therefore overshoot its stop for up
to ~20 minutes, making Stage 1's stopped-out trades slightly worse than live ones would be:
a conservative bias when deciding on promotion. Zero extra minutes.

The alternatives, if that changes:

- **5-minute loop:** add cron-job.org entries for `monitor` at 08:35 and 14:00 CT (two
  loops cover the session; the loop stops itself at 15:00 CT). About 385 minutes a day.
- **Single-shot every 5 minutes:** same cadence at roughly 100-150 minutes a day, from one
  scheduler entry (`*/5 8-14 * * 1-5`). This needs a small workflow change first: a
  single-check mode for the `monitor` job.
