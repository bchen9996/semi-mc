"""The pooled panel regression: stock-level signals predicting return net of market and
sector exposure.

Splitting the forecast this way -- CAPM/sector exposure removed first, signals predicting
only what is left -- gives the regression a stationary-ish target instead of asking it to
also relearn beta, and it means a signal's coefficient is interpretable as "return beyond
what this stock's market and sector exposure would predict," which is the quantity the
addendum wants tilted into the drift.

Two leakage rules are enforced here, not left to callers to remember:

**Betas are trailing, not contemporaneous.** The market/sector exposure removed from a
forward return uses a beta estimated on the window *ending at* the origin, never on the
forward window itself -- a beta measured over the same days as the target would leak the
realized covariance into what is supposed to be a residual.

**A training row needs a realized target.** A row's forward window ends at
``target_end_date``; :func:`training_rows` keeps a row only when that date is at or before
the requested origin. This is the addendum's own example: a 63-day forward return starting
today is not knowable for 63 more trading days.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from statsmodels.regression.linear_model import OLS
from statsmodels.tools.tools import add_constant

from ..data.returns import estimate_beta, residualize
from ..signals.build import signal_panel
from ..signals.supply_chain import customer_lead_lag_signal

LOGGER = logging.getLogger(__name__)

SIGNAL_COLUMNS = [
    "momentum",
    "reversal",
    "relative_strength",
    "earnings_drift",
    "customer_lead_lag",
]


def trailing_betas(
    ticker_returns: pd.Series,
    market_returns: pd.Series,
    sector_factor_returns: pd.Series,
    as_of: pd.Timestamp,
    window_days: int = 252,
) -> tuple[float, float]:
    """Market and sector-factor beta on the ``window_days`` ending at ``as_of``.

    ``sector_factor_returns`` should already be the sector benchmark residualized on the
    market (``data.returns.residualize``), so the two betas describe close to orthogonal
    exposures rather than double-counting the market move through both.
    """
    stamp = pd.Timestamp(as_of).normalize()
    window_start = stamp - pd.Timedelta(days=int(window_days * 365.25 / 252))
    ticker_window = ticker_returns.loc[window_start:stamp]
    beta_market = estimate_beta(ticker_window, market_returns.loc[window_start:stamp])
    beta_sector = estimate_beta(ticker_window, sector_factor_returns.loc[window_start:stamp])
    return (
        beta_market if np.isfinite(beta_market) else 1.0,
        beta_sector if np.isfinite(beta_sector) else 0.0,
    )


def forward_cumulative_return(
    series: pd.Series, as_of: pd.Timestamp, horizon_days: int
) -> tuple[float, pd.Timestamp] | None:
    """Cumulative log return over the ``horizon_days`` trading days *after* ``as_of``.

    Returns ``None`` when the series does not extend that far, mirroring
    ``backtest.walkforward._realized_outcome`` -- an incomplete final window is dropped,
    not scored against a truncated horizon.
    """
    stamp = pd.Timestamp(as_of).normalize()
    position = series.index.searchsorted(stamp, side="right") - 1
    if position < 0 or position + horizon_days >= len(series):
        return None
    value = float(series.iloc[position + 1 : position + 1 + horizon_days].sum())
    return value, series.index[position + horizon_days]


def build_regression_row(
    tickers: list[str],
    log_returns: pd.DataFrame,
    market_returns: pd.Series,
    sector_returns: pd.Series,
    calendar,  # noqa: ANN001 - EarningsCalendar, kept untyped to avoid an import cycle
    as_of: pd.Timestamp,
    horizon_days: int,
    supply_chain_links: pd.DataFrame | None = None,
    customer_returns: pd.DataFrame | None = None,
    momentum_lookback_days: int = 252,
    momentum_skip_days: int = 21,
    relative_strength_lookback_days: int = 126,
    earnings_drift_active_days: int = 60,
    beta_window_days: int = 252,
) -> pd.DataFrame:
    """One origin's worth of panel rows: signals, trailing betas, and the (possibly
    unrealized) net-of-exposure forward target.

    Returns
    -------
    pd.DataFrame
        Index: ticker. Columns: the signal columns, ``beta_market``, ``beta_sector``,
        ``date``, ``target``, ``target_end_date``. ``target`` and ``target_end_date`` are
        ``NaN``/``NaT`` when the forward window is not yet realized -- expected for the
        most recent origins, and exactly what :func:`training_rows` filters on.
    """
    sector_factor = residualize(sector_returns, market_returns)
    panel = signal_panel(
        log_returns.loc[:, tickers],
        sector_returns,
        market_returns,
        calendar,
        as_of,
        momentum_lookback_days,
        momentum_skip_days,
        relative_strength_lookback_days,
        earnings_drift_active_days,
    )

    if supply_chain_links is not None and customer_returns is not None:
        panel["customer_lead_lag"] = customer_lead_lag_signal(
            supply_chain_links, customer_returns, tickers, as_of
        )
    else:
        panel["customer_lead_lag"] = np.nan

    targets: dict[str, float] = {}
    ends: dict[str, pd.Timestamp] = {}
    betas_market: dict[str, float] = {}
    betas_sector: dict[str, float] = {}

    for ticker in tickers:
        beta_m, beta_s = trailing_betas(
            log_returns[ticker], market_returns, sector_factor, as_of, beta_window_days
        )
        betas_market[ticker] = beta_m
        betas_sector[ticker] = beta_s

        own = forward_cumulative_return(log_returns[ticker], as_of, horizon_days)
        mkt = forward_cumulative_return(market_returns, as_of, horizon_days)
        sec = forward_cumulative_return(sector_factor, as_of, horizon_days)
        if own is None or mkt is None or sec is None:
            targets[ticker] = np.nan
            ends[ticker] = pd.NaT
            continue
        own_value, own_end = own
        targets[ticker] = own_value - beta_m * mkt[0] - beta_s * sec[0]
        ends[ticker] = own_end

    panel["beta_market"] = pd.Series(betas_market)
    panel["beta_sector"] = pd.Series(betas_sector)
    panel["date"] = pd.Timestamp(as_of).normalize()
    panel["target"] = pd.Series(targets)
    panel["target_end_date"] = pd.Series(ends)
    return panel


def standardize_cross_sectionally(panel: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Z-score each column within each date, so a coefficient means "per standard
    deviation of cross-sectional spread on that date," not "per unit of a signal whose
    scale drifts over the sample."""
    out = panel.copy()
    for column in columns:
        if column not in out.columns:
            continue
        grouped = out.groupby("date")[column]
        mean = grouped.transform("mean")
        std = grouped.transform("std")
        out[column] = np.where(std > 0, (out[column] - mean) / std, np.nan)
    return out


def training_rows(panel: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Rows with a fully realized target at or before ``as_of``.

    The single choke point that enforces the addendum's leakage rule for the panel
    regression: a row survives only if ``date <= as_of`` (the signal itself was knowable)
    **and** ``target_end_date <= as_of`` (the forward return it predicts has actually
    happened). Both conditions are checked -- a stale signal from years ago with a still
    valid target would otherwise slip through the second check alone.
    """
    stamp = pd.Timestamp(as_of).normalize()
    mask = (
        (panel["date"] <= stamp)
        & panel["target_end_date"].notna()
        & (panel["target_end_date"] <= stamp)
    )
    return panel.loc[mask].copy()


class PanelRegressionResult:
    """A fitted pooled panel regression, holding what the addendum's evaluation needs."""

    def __init__(
        self,
        coefficients: pd.Series,
        std_errors: pd.Series,
        t_stats: pd.Series,
        r_squared: float,
        n_observations: int,
        n_dates: int,
        feature_columns: list[str],
    ) -> None:
        self.coefficients = coefficients
        self.std_errors = std_errors
        self.t_stats = t_stats
        self.r_squared = r_squared
        self.n_observations = n_observations
        self.n_dates = n_dates
        self.feature_columns = feature_columns

    def predict(self, features: pd.DataFrame) -> pd.Series:
        design = add_constant(features.loc[:, self.feature_columns], has_constant="add")
        return pd.Series(
            design.to_numpy() @ self.coefficients.reindex(design.columns).to_numpy(),
            index=features.index,
        )

    def annualized_effect_size(self, trading_days_per_year: int, horizon_days: int) -> pd.Series:
        """Each signal's coefficient converted to an annualized return tilt per one
        cross-sectional standard deviation -- the addendum's request to report effect size,
        not just a t-statistic."""
        per_horizon = self.coefficients.drop("const", errors="ignore")
        return per_horizon * (trading_days_per_year / horizon_days)


def fit_driscoll_kraay(
    rows: pd.DataFrame,
    feature_columns: list[str],
    target_column: str = "target",
    date_column: str = "date",
    maxlags: int = 3,
) -> PanelRegressionResult:
    """Pooled OLS with Driscoll-Kraay standard errors (``cov_type="hac-groupsum"``).

    Driscoll-Kraay handles both the cross-sectional correlation semiconductor stocks
    obviously have (they share a sector factor) and the serial correlation induced by
    overlapping forward-return targets, which a plain clustered-by-ticker or clustered-by-
    date standard error would each only get half of.

    Rows with a missing feature or target are dropped; a constant is added, but **no ticker
    or sub-industry fixed effect** -- Section 4B of the addendum forbids it explicitly,
    since a fixed effect would reintroduce each stock's own historical mean return, which
    is exactly the leak the drift policy exists to prevent.
    """
    usable = rows.dropna(subset=[*feature_columns, target_column, date_column])
    if len(usable) < 30:
        raise ValueError(f"only {len(usable)} usable rows for a Driscoll-Kraay fit; need >= 30")

    date_codes = pd.factorize(usable[date_column])[0]
    design = add_constant(usable.loc[:, feature_columns], has_constant="add")
    model = OLS(usable[target_column].to_numpy(), design.to_numpy())
    fitted = model.fit(
        cov_type="hac-groupsum",
        cov_kwds={"time": date_codes, "maxlags": maxlags},
    )

    columns = list(design.columns)
    coefficients = pd.Series(fitted.params, index=columns)
    std_errors = pd.Series(fitted.bse, index=columns)
    t_stats = pd.Series(fitted.tvalues, index=columns)

    return PanelRegressionResult(
        coefficients=coefficients,
        std_errors=std_errors,
        t_stats=t_stats,
        r_squared=float(fitted.rsquared),
        n_observations=int(len(usable)),
        n_dates=int(usable[date_column].nunique()),
        feature_columns=feature_columns,
    )


def newey_west_mean(values: np.ndarray, maxlags: int) -> tuple[float, float]:
    """Sample mean and its Newey-West standard error -- used to test whether a time series
    of per-date Fama-MacBeth coefficients (or monthly rank ICs) is different from zero
    while accounting for their own serial correlation."""
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return float(np.mean(x)) if n else float("nan"), float("nan")
    mean = float(np.mean(x))
    centred = x - mean
    gamma0 = float(np.dot(centred, centred) / n)
    variance = gamma0
    for lag in range(1, min(maxlags, n - 1) + 1):
        weight = 1.0 - lag / (maxlags + 1)
        gamma = float(np.dot(centred[lag:], centred[:-lag]) / n)
        variance += 2.0 * weight * gamma
    se = float(np.sqrt(max(variance, 0.0) / n))
    return mean, se


def fama_macbeth(
    rows: pd.DataFrame,
    feature_columns: list[str],
    target_column: str = "target",
    date_column: str = "date",
    newey_west_lags: int = 3,
    min_cross_section: int = 5,
) -> dict[str, dict[str, float]]:
    """Cross-sectional OLS per date, averaged with a Newey-West standard error.

    A robustness check against :func:`fit_driscoll_kraay`, per the addendum: instead of one
    pooled regression, this fits one regression *per date* and averages the resulting
    coefficients over time, which is robust to a few extreme dates dominating a pooled fit
    and does not assume the same residual variance across the panel.

    Returns
    -------
    dict[str, dict[str, float]]
        One entry per feature: ``mean``, ``newey_west_se``, ``t_stat``, ``n_dates``.
    """
    usable = rows.dropna(subset=[*feature_columns, target_column, date_column])
    per_date: dict[pd.Timestamp, pd.Series] = {}
    for date, group in usable.groupby(date_column):
        if len(group) < min_cross_section:
            continue
        design = add_constant(group.loc[:, feature_columns], has_constant="add")
        try:
            fitted = OLS(group[target_column].to_numpy(), design.to_numpy()).fit()
        except (ValueError, np.linalg.LinAlgError):
            continue
        per_date[date] = pd.Series(fitted.params, index=design.columns)

    if not per_date:
        raise ValueError("no date had enough cross-sectional observations for Fama-MacBeth")

    coefficient_series = pd.DataFrame(per_date).T.sort_index()
    out: dict[str, dict[str, float]] = {}
    for column in coefficient_series.columns:
        mean, se = newey_west_mean(coefficient_series[column].to_numpy(), newey_west_lags)
        out[column] = {
            "mean": mean,
            "newey_west_se": se,
            "t_stat": mean / se if se and np.isfinite(se) and se > 0 else float("nan"),
            "n_dates": int(coefficient_series[column].notna().sum()),
        }
    return out
