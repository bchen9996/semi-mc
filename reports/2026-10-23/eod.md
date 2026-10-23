# S4 end-of-day report: 2026-10-23

## Blotter
Proposals today (including rejected):
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- INTC no_trade [accepted]
- TSM no_trade [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- LRCX no_trade [accepted]
- INTC no_trade [accepted]
- TSM no_trade [accepted]
- MU no_trade [accepted]
- NVDA open [accepted]
- LRCX no_trade [accepted]
- INTC no_trade [accepted]
- TSM no_trade [accepted]
- MU open [accepted]
- NVDA hold [accepted]
- LRCX no_trade [accepted]
- INTC no_trade [accepted]
- TSM no_trade [accepted]
- MU add [accepted]
- NVDA hold [accepted]
- LRCX no_trade [accepted]
- INTC no_trade [accepted]
- TSM no_trade [accepted]
- MU hold [accepted]
- NVDA hold [accepted]
- LRCX no_trade [accepted]
- INTC no_trade [accepted]
- TSM no_trade [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- LRCX no_trade [accepted]
- INTC no_trade [accepted]
- TSM no_trade [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- LRCX no_trade [accepted]
- INTC no_trade [accepted]
- NVDA no_trade [accepted]
- INTC no_trade [accepted]
- TSM no_trade [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- INTC no_trade [accepted]
- TSM no_trade [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- INTC no_trade [accepted]
- TSM no_trade [accepted]

Reflections:
- MU (time_stop): gross=-48.48, net=-82.50, invalidation_respected=True
- NVDA (time_stop): gross=-41.89, net=-53.78, invalidation_respected=True

## P&L
- Gross: -90.37
- Net (of modeled cost): -136.28
- Pessimistic (double modeled cost): -182.18

## Attribution
- Sector (SOXX beta-of-1 approximation): 209.44
- Stock-specific (selection + timing, combined): -345.72
- Options: 0.00

The sector component was +$209, so the SOXX rebound helped the two long positions. The stock-specific component was -$346, so both names lagged the sector. MU faded from the breakout and NVDA drifted below VWAP. The options component was 0 because no options were used. Net, the day's loss came from idiosyncratic underperformance against the sector, not from the sector call. The combined net P&L was about -$136 after costs (-$82.5 MU, -$53.8 NVDA).

## Benchmarks for the day
- SOXX buy-and-hold, prior close to close: +0.53%
- SOXX open to close (the session an intraday book can trade): +0.73%
- S0 control sleeve: +0.00%
- Placebo (T6 control): -57.96
- "Do nothing" (the day's entries held untouched to the close): -0.29%

## Closing market report
Semis rebounded after Thursday's Nasdaq selloff (-2.6%, sentiment at Extreme Fear). SOXX gained about 0.53% close-to-close and 0.73% from the open. Breadth was poor: 8 of 11 S&P sectors fell and equal-weight beat cap-weight. MU rose about 3.5% on the day on a semis recovery and an RBC Outperform note, then stalled at the opening-range high. LRCX held above its opening range, and INTC held above its range and VWAP, helped by pre-earnings optimism and an FCC waiver. TSM was up about 1.2% on a Moody's upgrade, but on thin volume (relative volume about 0.6). NVDA finished slightly red, at about -0.3%, against a rising sector. News flow was mixed: AI-capex worries, Korea capitulation signals ahead of SK Hynix earnings, and SK Hynix shelving the Solidigm IPO. Tomorrow's catalysts: SK Hynix earnings, INTC earnings (pre-earnings optimism today), and AAPL earnings on Nov 2. I have no confirmed calendar beyond what the headlines say.

## Process scorecard
- Rejection rate: 0.0%
- New positions: 2/3
- Turnover: 30,016.17 / 74,955.19 cap
- Invalidation adherence: 100.0%
- Incidents today: 15
- Calibration (all-time):
  - [0.25, 0.50]: 75.0% win rate, n=4
  - [0.50, 0.75]: 7.7% win rate, n=13

The rejection rate was 0%. New positions used were 2 of 3. MU used 2 round trips against a cap of 2, and NVDA used 1. Turnover was about $30.0k against a $75.0k cap. Invalidation adherence was 100%: neither stop was hit, and both positions closed on time stops. The calibration buckets (n=4 at 0.25-0.5 with a 75% win rate, n=13 at 0.5-0.75 with a 7.7% win rate) are too small to conclude anything. They are, however, inverted, and are worth watching over time. Both entries were marginal triggers. NVDA entered just under the OR high, and MU entered only $0.6 above it, "extended" by its own flag, with a stop at VWAP. MU was then added to at a thin margin, which concentrated exposure into a marginal breakout. Several incidents were stale or missing quotes (quotes up to 225s old), which are data-quality concerns. Compared with the placebo (-$58) and do-nothing (-0.29%), the sleeve's results are broadly in line. Managing the positions did not clearly help or hurt. The session open equity was $99.94k.

## Monitor cadence
- Checks today: 12
- Average gap between checks: 32.5m
- Longest gap between checks: 60.2m

## Proposed playbook changes
None proposed today.
