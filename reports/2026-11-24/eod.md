# S4 end-of-day report: 2026-11-24

## Blotter
Proposals today (including rejected):
- ADI no_trade [accepted]
- MU no_trade [accepted]
- AMD no_trade [accepted]
- TSM no_trade [accepted]
- INTC no_trade [accepted]
- ADI no_trade [accepted]
- MU no_trade [accepted]
- AMD no_trade [accepted]
- TSM no_trade [accepted]
- INTC no_trade [accepted]
- ADI open [accepted]
- INTC open [accepted]
- MU no_trade [accepted]
- AMD no_trade [accepted]
- TSM no_trade [accepted]
- ADI hold [accepted]
- INTC hold [accepted]
- MU no_trade [accepted]
- AMD no_trade [accepted]
- TSM no_trade [accepted]
- ADI hold [accepted]
- INTC hold [accepted]
- MU no_trade [accepted]
- AMD no_trade [accepted]
- TSM no_trade [accepted]
- ADI open [rejected] -- ADI is being closed this cycle; no new entry until that has filled
- INTC open [rejected] -- INTC is being closed this cycle; no new entry until that has filled
- MU no_trade [accepted]
- AMD no_trade [accepted]
- TSM no_trade [accepted]
- ADI open [accepted]
- INTC no_trade [accepted]
- MU no_trade [accepted]
- AMD no_trade [accepted]
- TSM no_trade [accepted]

Reflections:
- ADI (time_stop): gross=46.55, net=40.59, invalidation_respected=True
- INTC (time_stop): gross=59.17, net=49.85, invalidation_respected=True
- ADI (invalidation): gross=-38.66, net=-44.21, invalidation_respected=True

## P&L
- Gross: 67.06
- Net (of modeled cost): 46.23
- Pessimistic (double modeled cost): 25.41

## Attribution
- Sector (SOXX at each name's beta, over each trade's own holding period): 26.50
- Stock-specific (selection + timing, after modeled cost): 19.74
- Options: 0.00

## With the sector removed (a SOXX hedge measured, not traded)
- Today, 3 exits: gross 67.06, hedge -26.50, hedged gross 40.56, hedged net -0.82
- Agent since 2026-11-16: -5.7 bps per exit before costs (standard error 22.2, 12 exits)
- Placebo since 2026-11-16: -21.8 bps per exit before costs (standard error 16.8, 42 exits)

## Execution cost (each fill against the quote midpoint as its order went in)
- Modeled: 8.0 bps a side, charged on top of the fills
- Today, 3 exits: entries +2.8 bps, exits +7.6 bps, +5.2 bps a side
- Agent since Stage 2: +1.1 bps a side (standard error 0.6, 66 exits; entries +0.2 bps, exits +2.0 bps)

## Book risk (measured each cycle; only the net exposure is a limit)
- Cycles measured: 13
- Peak net SOXX exposure: -9.0% of equity (cap 80%)
- Largest simulated loss to the close, at 5% probability with no stop holding: -322.50
- Highest chance of ending past the soft loss limit: 0.0%
- Highest share of book risk from SOXX: 75%
- Largest z-score on a held name: ADI -3.1

## Benchmarks for the day
- SOXX buy-and-hold, prior close to close: -8.26%
- SOXX open to close (the session an intraday book can trade): -3.82%
- S0 control sleeve: +0.00%
- Placebo (T6 control): -267.03
- "Do nothing" (the day's entries held untouched to the close): -0.02%

## Closing market report
No Reviewer narration today: the Reviewer call failed (reviewer call exited 1: Credit balance is too low).

## Process scorecard
- Rejection rate: 5.7%
  Reasons: ADI is being closed this cycle; no new entry until that has filled; INTC is being closed this cycle; no new entry until that has filled
- New positions: 3/6
- Turnover: 17,309.98 / 151,106.36 cap
- Invalidation adherence: 100.0%
- Incidents today: 14
- Calibration (all-time):
  - [0.25, 0.50]: 52.0% win rate, n=25
  - [0.50, 0.75]: 31.1% win rate, n=45

## Monitor cadence
- Checks today: 13
- Average gap between checks: 29.9m
- Longest gap between checks: 30.5m

## Proposed playbook changes
No Reviewer narration today: the Reviewer call failed (reviewer call exited 1: Credit balance is too low).
