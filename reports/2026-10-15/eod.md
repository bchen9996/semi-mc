# S4 end-of-day report: 2026-10-15

## Blotter
Proposals today (including rejected):
- TSM no_trade [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- TSM no_trade [accepted]
- TXN no_trade [accepted]
- ADI no_trade [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- TSM no_trade [accepted]
- TXN no_trade [accepted]
- ADI no_trade [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- TSM open [accepted]
- NVDA open [accepted]
- ADI no_trade [accepted]
- MU no_trade [accepted]
- TSM hold [accepted]
- NVDA hold [accepted]
- TXN no_trade [accepted]
- ADI no_trade [accepted]
- MU no_trade [accepted]
- TSM add [accepted]
- NVDA add [rejected] -- new_positions_per_day budget (3) already spent today
- ADI no_trade [accepted]
- MU no_trade [accepted]
- TSM hold [accepted]
- NVDA hold [accepted]
- MU no_trade [accepted]
- NVDA hold [accepted]
- TSM hold [accepted]
- NVDA hold [accepted]
- TSM hold [accepted]
- NVDA hold [accepted]
- TSM hold [accepted]
- TSM hold [accepted]
- NVDA hold [accepted]
- ADI no_trade [accepted]
- MU no_trade [accepted]
- TSM no_trade [accepted]
- TXN no_trade [accepted]
- ADI no_trade [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]
- TSM no_trade [accepted]
- TXN no_trade [accepted]
- MU no_trade [accepted]
- NVDA no_trade [accepted]

Reflections:
- NVDA (time_stop): gross=3.34, net=0.56, invalidation_respected=True
- TSM (time_stop): gross=-2.60, net=-6.62, invalidation_respected=True

## P&L
- Gross: 0.74
- Net (of modeled cost): -6.05
- Pessimistic (double modeled cost): -12.85

## Attribution
- Sector (SOXX beta-of-1 approximation): 8.59
- Stock-specific (selection + timing, combined): -14.65
- Options: 0.00

The precomputed split shows sector +8.59, stock-specific -14.65 and options 0.0, a net of about -6. The sector contribution is small and positive, and the stock-specific component is negative. That is consistent with TSM's small net loss (-6.62) and NVDA's near-flat gain (+0.56 net). The day's P&L was essentially cost and noise, with no options exposure.

## Benchmarks for the day
- SOXX buy-and-hold: +0.17%
- S0 control sleeve: +0.00%
- Placebo (T6 control): 1,161.80
- "Do nothing" (hold the open batch flat): not available

## Closing market report
Semis sold off for a roughly eighth straight session. SOXX was about -1% to -2.7% on the day at various points, SK Hynix has lost 31% in 7 days, and memory names are down about $1T since Oct 2. Hot PPI (6.3% YoY) and Fed-hike fears plus AI-financing worries (CoreWeave debt) drove the move, and defensives led the sectors. TSM reported a Q3 beat (record profit) and opened down about 4.7%, then recovered to about 418-421. NVDA held above VWAP and the opening range, finishing near 217. MU bounced about 6% off its low to about 911-921 but was never entered. TXN and ADI stayed inside their opening ranges. LRCX is on a ninth straight losing day. Tomorrow's catalysts: continued Fed-hike and inflation repricing, MSFT earnings ahead, and chip-selloff follow-through or a bounce. No specific earnings for our names.

## Process scorecard
- Rejection rate: 2.1%
  Reasons: new_positions_per_day budget (3) already spent today
- New positions: 3/3
- Turnover: 6,000.00 / 75,000.00 cap
- Invalidation adherence: 100.0%
- Incidents today: 38
- Calibration (all-time):
  - [0.25, 0.50]: 100.0% win rate, n=2
  - [0.50, 0.75]: 25.0% win rate, n=4

The rejection rate was 2.1%, a single rejection: the NVDA add, blocked because the 3 new-position budget was spent. Budget use was 3 of 3 new positions, turnover $6,000 against the $75,000 cap, and round trips of 2 for TSM and 1 for NVDA against a cap of 2. Invalidation adherence was 100%. Calibration is thin: 2 trades in the 0.25-0.5 bin won, and 4 in the 0.5-0.75 bin won 25%, which is too few to conclude anything. Process concerns: many late cycles show missing quotes and stale-data incidents, including errors where the NVDA and TSM invalidation/time-stop was breached with no price to close at. Several later no_trade and hold entries have null confidence and empty evidence. The NVDA 1900 and 1930 theses contain contradictory trigger statements. Using the add on TSM consumed the slot the NVDA add needed. Both positions closed on time stops with essentially zero edge, and the expected_cost_bps field was never supplied.

## Monitor cadence
- Checks today: 13
- Average gap between checks: 29.8m
- Longest gap between checks: 30.1m

## Proposed playbook changes
None proposed today.
