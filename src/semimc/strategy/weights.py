"""Target-weight primitives shared by every sleeve.

Kept free of any model-fitting or simulation -- those live in ``forecasts.py``. This
module only ever combines a weight vector with a volatility number.
"""

from __future__ import annotations

import pandas as pd


def equal_weight(tickers: list[str]) -> pd.Series:
    """Equal weight across ``tickers``. S0's own definition, and S3's/the vol-twin's
    starting point before either scales it toward a target volatility."""
    return pd.Series(1.0 / len(tickers), index=list(tickers))


def scale_to_target_vol(
    weights: pd.Series,
    forecast_annual_vol: float,
    target_vol: float,
    max_leverage: float = 1.0,
) -> pd.Series:
    """Scale ``weights`` toward ``target_vol``, capped at ``max_leverage``.

    The brief's S3 rule: "no leverage" means the scale factor never exceeds
    ``max_leverage`` (1.0 by default), and the unscaled remainder is implicit cash -- the
    returned weights are not renormalized to sum to 1.

    A non-positive ``forecast_annual_vol`` is degenerate (nothing to size against) and
    returns all-cash rather than dividing by zero or a negative number.
    """
    if forecast_annual_vol <= 0:
        return weights * 0.0
    scale = min(target_vol / forecast_annual_vol, max_leverage)
    return weights * scale
