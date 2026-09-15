"""Data validation.

The brief's rule is "never silently drop or fill; log every fix". This module therefore
only ever *reports*: it returns a structured list of issues and leaves the decision to the
caller. Nothing here modifies prices.

Checks performed per ticker:

``missing``          requested but absent from the cache (delisted, renamed, or failed)
``short_history``    fewer observations than the fit window needs
``gap``              a run of missing business days longer than ``data.max_gap_days``
``stale_price``      three or more consecutive identical closes
``zero_volume``      a trading day with zero or missing volume
``outlier``          a daily log return beyond ``data.outlier_sigma`` robust sigmas
``duplicate_date``   the same date appearing twice for one ticker
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class ValidationIssue:
    """One finding about one ticker."""

    ticker: str
    kind: str
    severity: str
    detail: str
    date: pd.Timestamp | None = None
    value: float | None = None


@dataclass
class ValidationReport:
    """Collection of findings, with convenience views for the CLI and HTML report."""

    issues: list[ValidationIssue] = field(default_factory=list)
    checked_tickers: list[str] = field(default_factory=list)

    def add(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_frame(self) -> pd.DataFrame:
        """Long frame of every issue, sorted by severity then ticker then date."""
        if not self.issues:
            return pd.DataFrame(columns=["ticker", "kind", "severity", "date", "value", "detail"])
        frame = pd.DataFrame(
            [
                {
                    "ticker": i.ticker,
                    "kind": i.kind,
                    "severity": i.severity,
                    "date": i.date,
                    "value": i.value,
                    "detail": i.detail,
                }
                for i in self.issues
            ]
        )
        frame["_sev"] = frame["severity"].map(SEVERITY_ORDER).fillna(9)
        return (
            frame.sort_values(["_sev", "ticker", "date"], na_position="last")
            .drop(columns="_sev")
            .reset_index(drop=True)
        )

    def summary(self) -> pd.DataFrame:
        """Counts of each issue kind per ticker (wide, tickers as rows)."""
        frame = self.to_frame()
        if frame.empty:
            return pd.DataFrame(index=pd.Index(self.checked_tickers, name="ticker"))
        table = frame.pivot_table(
            index="ticker", columns="kind", values="severity", aggfunc="count", fill_value=0
        )
        return table.reindex(self.checked_tickers, fill_value=0)


def _robust_sigma(values: np.ndarray) -> float:
    """Median-absolute-deviation scale estimate, scaled to a normal standard deviation.

    MAD rather than the sample standard deviation because the very outliers being hunted
    would inflate a classical sigma enough to hide themselves.
    """
    finite = values[np.isfinite(values)]
    if finite.size < 10:
        return float("nan")
    mad = np.median(np.abs(finite - np.median(finite)))
    return float(mad * 1.4826)


def _business_day_gap(previous: pd.Timestamp, current: pd.Timestamp) -> int:
    """Business days strictly between two dates (holidays are not modelled)."""
    return int(np.busday_count(previous.date(), current.date())) - 1


def validate_prices(
    prices: pd.DataFrame,
    requested: list[str],
    volumes: pd.DataFrame | None = None,
    min_observations: int = 750,
    max_gap_days: int = 5,
    outlier_sigma: float = 8.0,
    stale_run_length: int = 3,
) -> ValidationReport:
    """Run every check over a wide price frame.

    Parameters
    ----------
    prices
        Wide adjusted closes. Index: dates. Columns: tickers. ``NaN`` where a ticker did
        not trade.
    requested
        Tickers the caller asked for; any absent from ``prices`` is reported ``missing``.
    volumes
        Optional wide volume frame, same shape as ``prices``.
    min_observations
        Threshold below which a history is reported as short.
    max_gap_days
        Business-day gap above which a break in the series is reported.
    outlier_sigma
        Robust-sigma multiple beyond which a daily return is flagged for review.
    stale_run_length
        Number of consecutive identical closes that counts as a stale run.

    Returns
    -------
    ValidationReport
    """
    report = ValidationReport(checked_tickers=list(requested))

    for ticker in requested:
        if ticker not in prices.columns:
            report.add(
                ValidationIssue(
                    ticker,
                    "missing",
                    "error",
                    "no cached data (delisted, renamed, or the fetch failed)",
                )
            )
            continue

        series = prices[ticker].dropna()
        if series.empty:
            report.add(ValidationIssue(ticker, "missing", "error", "all values are NaN"))
            continue

        if series.index.has_duplicates:
            dupes = series.index[series.index.duplicated()].unique()
            for date in dupes:
                report.add(
                    ValidationIssue(
                        ticker, "duplicate_date", "error", "date appears more than once", date
                    )
                )
            series = series[~series.index.duplicated(keep="first")]

        if len(series) < min_observations:
            report.add(
                ValidationIssue(
                    ticker,
                    "short_history",
                    "warning",
                    f"{len(series)} observations from {series.index.min().date()}; "
                    f"{min_observations} needed for a full fit window",
                    series.index.min(),
                    float(len(series)),
                )
            )

        # Gaps.
        dates = series.index
        for previous, current in zip(dates[:-1], dates[1:], strict=True):
            gap = _business_day_gap(previous, current)
            if gap > max_gap_days:
                report.add(
                    ValidationIssue(
                        ticker,
                        "gap",
                        "warning",
                        f"{gap} missing business days after {previous.date()}",
                        current,
                        float(gap),
                    )
                )

        # Stale prices: runs of identical closes.
        values = series.to_numpy()
        unchanged = np.isclose(values[1:], values[:-1], rtol=0, atol=0)
        run = 0
        for idx, same in enumerate(unchanged):
            if same:
                run += 1
                continue
            if run >= stale_run_length - 1:
                report.add(
                    ValidationIssue(
                        ticker,
                        "stale_price",
                        "warning",
                        f"{run + 1} consecutive identical closes ending {dates[idx].date()}",
                        dates[idx],
                        float(run + 1),
                    )
                )
            run = 0
        if run >= stale_run_length - 1:
            report.add(
                ValidationIssue(
                    ticker,
                    "stale_price",
                    "warning",
                    f"{run + 1} consecutive identical closes ending {dates[-1].date()}",
                    dates[-1],
                    float(run + 1),
                )
            )

        # Outliers, judged against a robust scale.
        rets = np.log(values[1:] / values[:-1])
        sigma = _robust_sigma(rets)
        if np.isfinite(sigma) and sigma > 0:
            centre = float(np.median(rets))
            extreme = np.abs(rets - centre) > outlier_sigma * sigma
            for idx in np.flatnonzero(extreme):
                report.add(
                    ValidationIssue(
                        ticker,
                        "outlier",
                        "info",
                        f"{np.expm1(rets[idx]):+.1%} move, "
                        f"{abs(rets[idx] - centre) / sigma:.1f} robust sigmas "
                        "(review for an unadjusted split or a genuine shock)",
                        dates[idx + 1],
                        float(np.expm1(rets[idx])),
                    )
                )

        # Zero or missing volume on a day the ticker supposedly traded.
        if volumes is not None and ticker in volumes.columns:
            vol = volumes[ticker].reindex(series.index)
            bad = vol.isna() | (vol <= 0)
            for date in vol.index[bad]:
                report.add(
                    ValidationIssue(
                        ticker,
                        "zero_volume",
                        "info",
                        "zero or missing volume on a day with a price",
                        date,
                    )
                )

    return report
