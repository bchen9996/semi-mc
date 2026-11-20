# S4 end-of-day report: 2026-11-20

## Blotter
Proposals today (including rejected):
- AMD no_trade [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- MRVL no_trade [accepted]
- AMD no_trade [accepted]
- MU no_trade [accepted]
- AMAT no_trade [accepted]
- NVDA no_trade [accepted]
- MRVL no_trade [accepted]
- TXN no_trade [accepted]
- AMD open [accepted]
- AMAT open [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- MRVL no_trade [accepted]
- AMD hold [accepted]
- AMAT hold [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- TXN no_trade [accepted]
- MRVL no_trade [accepted]
- AMD hold [accepted]
- AMAT hold [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- MRVL no_trade [accepted]
- TXN no_trade [accepted]
- AMD hold [accepted]
- AMAT hold [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- MRVL no_trade [accepted]
- TXN no_trade [accepted]
- AMD open [rejected] -- AMD is being closed this cycle; no new entry until that has filled
- AMAT open [rejected] -- AMAT is being closed this cycle; no new entry until that has filled
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- TXN no_trade [accepted]
- MRVL no_trade [accepted]
- AMD open [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- MRVL no_trade [accepted]
- TXN no_trade [accepted]
- AMD hold [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- TXN no_trade [accepted]
- MRVL no_trade [accepted]
- AMAT no_trade [accepted]
- AMD hold [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- TXN no_trade [accepted]
- MRVL no_trade [accepted]
- AMD no_trade [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- MRVL no_trade [accepted]
- AMAT no_trade [accepted]
- AMD no_trade [accepted]
- MRVL no_trade [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- TXN no_trade [accepted]
- AMAT no_trade [accepted]
- AMD no_trade [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- MRVL no_trade [accepted]
- TXN no_trade [accepted]

Reflections:
- AMAT (time_stop): gross=100.82, net=93.39, invalidation_respected=True
- AMD (time_stop): gross=153.13, net=146.08, invalidation_respected=True
- AMD (time_stop): gross=61.67, net=56.85, invalidation_respected=True

## P&L
- Gross: 315.62
- Net (of modeled cost): 296.32
- Pessimistic (double modeled cost): 277.02

## Attribution
- Sector (SOXX at each name's beta, over each trade's own holding period): 355.76
- Stock-specific (selection + timing, after modeled cost): -59.44
- Options: 0.00

The three exits in today's attribution netted about +296. Sector exposure explains +355.8, meaning a SOXX hedge at each name's beta would have lost about 356 over those holding periods. Stock-specific was -59.4 after modeled cost, so the shorts gained less than their beta implied. Nothing was unpriced and there were no options. The sector-removed mean was -21.2 bps (stderr 45.9) for today, so the stock-specific edge is indistinguishable from zero. The profit was a short-beta win in a crash day, not evidence of selection skill.

## With the sector removed (a SOXX hedge measured, not traded)
- Today, 3 exits: gross 315.62, hedge -355.76, hedged gross -40.14, hedged net -84.49
- Agent since 2026-11-16: +7.7 bps per exit before costs (standard error 34.5, 5 exits)
- Placebo since 2026-11-16: +2.4 bps per exit before costs (standard error 15.6, 30 exits)

## Execution cost (each fill against the quote midpoint as its order went in)
- Modeled: 8.0 bps a side, charged on top of the fills
- Today, 3 exits: entries +7.3 bps, exits +3.6 bps, +5.5 bps a side
- Agent since Stage 2: +0.8 bps a side (standard error 0.6, 59 exits; entries -0.1 bps, exits +1.7 bps)

## Book risk (measured each cycle; only the net exposure is a limit)
- Cycles measured: 13
- Peak net SOXX exposure: -10.0% of equity (cap 80%)
- Largest simulated loss to the close, at 5% probability with no stop holding: -327.45
- Highest chance of ending past the soft loss limit: 0.0%
- Highest share of book risk from SOXX: 83%
- Largest z-score on a held name: AMAT -1.0

## Benchmarks for the day
- SOXX buy-and-hold, prior close to close: -10.58%
- SOXX open to close (the session an intraday book can trade): -8.68%
- S0 control sleeve: +0.00%
- Placebo (T6 control): -25.82
- "Do nothing" (the day's entries held untouched to the close): +0.47%

## Closing market report
Semis sold off hard. SOXX was -10.6% close to close and -8.7% open to close, so most of the damage came in the overnight gap, which this sleeve never holds. AMD closed about -13%, AMAT about -10.7%, NVDA about -10% from the open, and MRVL about -9%. MU also traded well below its opening range. Breadth was uniformly negative, and TXN was weak but not outperforming SOXX. Catalysts: a Daiwa AMAT target cut (515 to 370) and a Stifel MRVL cut (370 to 280). A short-seller report on AMD alleged firmware supplied to a blacklisted Chinese entity, and law firms opened investigations. NVDA drew congressional and California AG probes over China sales. Jefferies flagged ASML export-policy risk of about 15% of revenue under the MATCH Act. MU has an announced earnings date. Tomorrow's catalysts: watch for rebound or squeeze risk after the washout, and for follow-through on the AMD and NVDA probes. No other earnings are listed in the data.

## Process scorecard
- Rejection rate: 2.8%
  Reasons: AMD is being closed this cycle; no new entry until that has filled; AMAT is being closed this cycle; no new entry until that has filled
- New positions: 3/6
- Turnover: 12,749.22 / 150,908.70 cap
- Invalidation adherence: 100.0%
- Incidents today: 14
- Calibration (all-time):
  - [0.25, 0.50]: 52.0% win rate, n=25
  - [0.50, 0.75]: 31.6% win rate, n=38

Rejection rate was 2.8%. Two same-cycle re-entries (AMD, AMAT) were blocked while closes were pending, which is the control working as intended. Budget use was 3 of 6 new positions and AMD hit its 2 round-trip cap. Turnover was about $12.7k against a $150.9k cap. Invalidation adherence was 100%, and all three exits were time stops, not stop-outs. Peak net beta was about -0.10% against a cap of 0.8, and the highest sector share was 83%, so the book was concentrated in one sector. The calibration curve is not usable yet: 0.5-0.75 confidence shows a 32% win rate (n=38) against 52% for 0.25-0.5 (n=25), which is inverted but too noisy to act on. The agent tried to open AMD at 17:30 while a close was in flight, which suggests it could read the pending-close state better. Several stale-quote warnings for AMAT and TXN were logged. Do-nothing return was +0.47%, and the placebo's own P&L was -25.82. Managing positions via time stops did not obviously help versus holding, though one day is not meaningful.

## Monitor cadence
- Checks today: 13
- Average gap between checks: 29.8m
- Longest gap between checks: 30.4m

## Proposed playbook changes
None proposed today.
