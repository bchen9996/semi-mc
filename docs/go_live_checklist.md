# Go-live checklist

The last gate before flipping any `paper.sleeves.<sleeve>.trading_enabled` from its
default `false`, in the same pre-registration spirit as `THESES.md`: a fixed list decided
*before* the moment of flipping the switch, so that moment is a checklist item ticked off,
not a judgment call made under time pressure. Phase 8 (`tasks/todo.md`) built and verified
items 1-5 below; item 6 -- actually enabling a sleeve -- is a separate, deliberate action
the owner takes when ready, never bundled into finishing this checklist.

## 1. `.env` holds paper keys, not live keys

Confirmed since Phase 4 (`AlpacaPaperBroker.from_env`'s own docstring, `docs/
alpaca_notes.md`). Alpaca paper and live accounts each have their own key pair; there is no
live code path anywhere in this project (`.env.example`'s own comment says so) -- restated
here as a standing check to glance at before going further, not re-verified each time.

## 2. `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` are set as GitHub repo secrets

`Settings -> Secrets and variables -> Actions` on the repo, matching the names
`.github/workflows/paper_trading.yml`'s `execute`/`mark` jobs already reference
(`${{ secrets.ALPACA_API_KEY }}` / `${{ secrets.ALPACA_SECRET_KEY }}`). Without these, every
scheduled `execute`/`mark` run fails immediately trying to construct
`AlpacaPaperBroker.from_env()` -- which, per Phase 7, now correctly fails the job and
triggers GitHub's failure email, but it's still worth confirming ahead of time rather than
learning it from the first failure. This session has no repo access to check it (`gh auth
status` fails locally) -- owner-verified.

## 3. A `workflow_dispatch` dry run of `paper_trading.yml` completed clean

Triggered manually (GitHub's Actions tab, or `gh workflow run paper_trading.yml` once `gh`
is authenticated) after pushing. All three jobs (`decide-and-execute`, `mark`, `report`)
should complete successfully with every sleeve declining harmlessly at pretrade (`sleeve
's0'/'s3' is not trading_enabled`) -- the first real proof the workflow runs on GitHub's
infrastructure at all, not just against `FakeBroker` or the real API called directly from a
local shell. Record the run URL and date here once done:

- Run: _(pending -- owner to trigger and confirm)_

## 4. GitHub's Actions failure-notification email is enabled for this repo

`github.com` -> account Settings -> Notifications -> Actions. This is what Phase 7's whole
alerting design depends on (no new dependency was added specifically because this already
exists) -- confirmed present as a setting, but whether it's *on* for this repo is the
owner's account, not something this repo's code can inspect or guarantee.

## 5. The kill switch procedure is understood

`paper.kill_switch_path` (`runs/paper/KILL_SWITCH`) is a plain sentinel file
(`execution/pretrade.py::check_kill_switch`): its mere existence halts every sleeve's
`execute` on its next run, checked fresh each run, no redeploy needed. To engage it against
the real, deployed workflow: commit an empty file at that path (directly on GitHub, or
`touch` + commit + push locally) -- the next scheduled or dispatched run will decline at
pretrade for every sleeve. To resume, delete the file and commit that. Deliberately not
automated (e.g. auto-engaging on repeated incidents) -- see `tasks/todo.md`'s Phase 7 entry
for why that's a Phase 8+ rollout-policy decision, not alerting plumbing.

## 6. Enable a sleeve -- the owner's explicit action, not automatic

Two edits to `config/paper.yaml`, committed together as one deliberate, reviewable diff
(never a default -- the file's own header comment already says so):

- `paper.registered_accounts`: add the real paper account's own number (from
  `broker.get_account().account_number` against the account `.env`'s keys point at --
  **never the API key itself**, which must never appear in a committed file or this
  document). This is the guard `execution/pretrade.py::check_pretrade` checks before any
  order can be submitted at all; nothing trades against an account not listed here.
- `paper.sleeves.<sleeve>.trading_enabled: true`, for exactly the sleeve(s) named at
  go-live time -- `s0` and/or `s3`, decided then, not pre-committed to both simultaneously
  by this checklist.

Before doing this, re-read `THESES.md`'s `review_months: [3, 6, 12]` cadence: no verdict
from `semimc paper evaluate` should be read as final before the first (3-month) checkpoint,
however the numbers look in week one.
