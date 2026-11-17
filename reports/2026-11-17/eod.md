# S4 end-of-day report: 2026-11-17

## Blotter
Proposals today (including rejected):
- MRVL no_trade [accepted]
- LRCX no_trade [accepted]
- AMD no_trade [accepted]
- MU no_trade [accepted]
- AMD open [accepted]
- MU open [accepted]
- MRVL no_trade [accepted]
- LRCX no_trade [accepted]
- KLAC no_trade [accepted]
- AMD hold [accepted]
- MU hold [accepted]
- MRVL no_trade [accepted]
- LRCX no_trade [accepted]
- KLAC no_trade [accepted]
- AMD hold [accepted]
- MU hold [accepted]
- MRVL no_trade [accepted]
- LRCX no_trade [accepted]
- KLAC no_trade [accepted]
- AMD hold [accepted]
- MU hold [accepted]
- MRVL no_trade [accepted]
- LRCX no_trade [accepted]
- KLAC no_trade [accepted]
- MRVL no_trade [accepted]
- LRCX no_trade [accepted]
- KLAC no_trade [accepted]
- AMD no_trade [accepted]
- MU no_trade [accepted]
- MRVL no_trade [accepted]
- LRCX no_trade [accepted]
- AMD no_trade [accepted]
- MU no_trade [accepted]
- LRCX no_trade [accepted]
- AMD no_trade [accepted]
- MU no_trade [accepted]
- MRVL no_trade [accepted]
- LRCX no_trade [accepted]
- AMD no_trade [accepted]
- MU no_trade [accepted]
- KLAC no_trade [accepted]
- LRCX no_trade [accepted]
- AMD no_trade [accepted]
- MU no_trade [accepted]
- MRVL no_trade [accepted]
- LRCX no_trade [accepted]
- AMD no_trade [accepted]
- MU no_trade [accepted]
- MRVL no_trade [accepted]
- LRCX no_trade [accepted]
- AMD no_trade [accepted]
- MU no_trade [accepted]
- KLAC no_trade [accepted]
- MRVL no_trade [accepted]
- LRCX no_trade [accepted]
- AMD no_trade [accepted]
- MU no_trade [accepted]
- KLAC no_trade [accepted]

Reflections:
- AMD (time_stop): gross=424.06, net=404.31, invalidation_respected=True
- MU (time_stop): gross=97.90, net=90.11, invalidation_respected=True

## P&L
- Gross: 521.96
- Net (of modeled cost): 494.42
- Pessimistic (double modeled cost): 466.87

## Attribution
- Sector (SOXX at each name's beta, over each trade's own holding period): 394.87
- Stock-specific (selection + timing, after modeled cost): 99.55
- Options: 0.00

Net P&L was about +494 across 2 exits (AMD, MU). The sector component was +394.9, meaning a SOXX hedge at each name's beta would have lost that amount over the holding periods. Both trades were shorts, so most of the gain was just sector beta from the semis selloff. Stock-specific P&L was +99.6 after modeled cost, with no unpriced exits and no options. The sector-removed mean was +51 bps with a stderr of about 50 bps, so it is statistically indistinguishable from zero. The placebo's sector-removed mean was -18 bps over 12 exits, and its day P&L was -8.37. Do-nothing, meaning the same entries held untouched to the close, returned -0.098%. That return is not directly comparable with the sector-removed figures, so I'm not drawing a verdict from it.

## With the sector removed (a SOXX hedge measured, not traded)
- Today, 2 exits: gross 521.96, hedge -394.87, hedged gross 127.09, hedged net 62.34
- Agent since 2026-11-16: +51.0 bps per exit before costs (standard error 49.7, 2 exits)
- Placebo since 2026-11-16: -18.3 bps per exit before costs (standard error 19.1, 12 exits)

## Execution cost (each fill against the quote midpoint as its order went in)
- Modeled: 8.0 bps a side, charged on top of the fills
- Today, 2 exits: entries -0.4 bps, exits +3.8 bps, +1.7 bps a side
- Agent since Stage 2: +0.6 bps a side (standard error 0.6, 56 exits; entries -0.5 bps, exits +1.6 bps)

## Book risk (measured each cycle; only the net exposure is a limit)
- Cycles measured: 13
- Peak net SOXX exposure: -20.5% of equity (cap 80%)
- Largest simulated loss to the close, at 5% probability with no stop holding: -704.04
- Highest chance of ending past the soft loss limit: 0.2%
- Highest share of book risk from SOXX: 77%
- Largest z-score on a held name: MU -1.7

## Benchmarks for the day
- SOXX buy-and-hold, prior close to close: +0.16%
- SOXX open to close (the session an intraday book can trade): -1.01%
- S0 control sleeve: +0.00%
- Placebo (T6 control): -8.37
- "Do nothing" (the day's entries held untouched to the close): -0.10%

## Closing market report
Semis reversed over the session. SOXX was +0.16% vs the prior close at the open, then ended -1.0% open-to-close. SMH was weaker than SOXX, around -1.4% to -1.9% intraday. Equipment names were red: AMAT about -1.7%, LRCX about -1.5%, KLAC about -1.6%. MRVL was about -0.4% to -1.2% and sat below its opening-range low and VWAP all day. AMD gapped up, then faded and broke the opening-range low, ending about 4-5% below its open. The headline cited export restrictions on AI chips, but another AMD headline was positive and the data conflicted with the pre-market gap. MU fell about 3-4.7% on the Situational Awareness exit filing, which may be stale, and on sector weakness. Breadth was negative among the 14 watchlist names. Tomorrow's catalysts: follow-through on export-control headlines, the Nvidia leadership transition and NVDA's new export-control committee, and the Broadcom/Anthropic IPO news. No earnings are listed in the context.

## Process scorecard
- Rejection rate: 0.0%
- New positions: 2/6
- Turnover: 18,090.98 / 150,126.32 cap
- Invalidation adherence: 100.0%
- Incidents today: 14
- Calibration (all-time):
  - [0.25, 0.50]: 50.0% win rate, n=24
  - [0.50, 0.75]: 27.8% win rate, n=36

The rejection rate was 0%. New positions were 2 of 6, round trips were 1 per symbol against a cap of 2, and turnover was about $18.1k against a $150k cap. Invalidation adherence was 100%, and both exits were time stops at about 120 minutes. Calibration is thin: the 0.25-0.5 bucket has n=24 and a 50% win rate, and the 0.5-0.75 bucket has n=36 and a 28% win rate. That second bucket is overconfident, but the sample is small and today did not add to it. There were several quote-age and no-quote warnings, mostly on KLAC and MRVL and at the open. These did not affect the trades. Process concerns: the AMD entry carried low relative volume (0.44) and a headline/data conflict. The MU thesis relied on a possibly stale filing and on sector green, and that premise broke within minutes, so MU would have been better held to a tighter standard. Later no_trade decisions on chasing were disciplined. The agent stayed flat after the time stops, so it did not re-enter the extended AMD move.

## Monitor cadence
- Checks today: 13
- Average gap between checks: 29.8m
- Longest gap between checks: 30.3m

## Proposed playbook changes
None proposed today.
