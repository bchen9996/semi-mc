"""Combining single-signal forecasts: the addendum's "simple to complex" ladder past the
single-signal pooled regression.

The addendum's own ordering (Section "Models, simple to complex"): try the simple average
of the single-signal forecasts first ("often more robust than one regression containing
every signal"), then ridge or lasso. Each of these needs a different fit/predict contract
than a single :func:`~semimc.forecasting.panel.fit_driscoll_kraay` call, so
:func:`evaluate_candidate` is written once against a common ``predict_fn(train, test)``
shape and every rung of the ladder -- including the pre-existing single-signal and
kitchen-sink regressions -- is scored through it identically.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .cv import date_grouped_gapped_splits
from .evaluate import clark_west_statistic, oos_r_squared, rank_information_coefficient
from .panel import PanelRegressionResult, fit_driscoll_kraay

PredictFn = Callable[[pd.DataFrame, pd.DataFrame], np.ndarray]


def evaluate_candidate(
    data: pd.DataFrame,
    predict_fn: PredictFn,
    n_folds: int,
    gap_periods: int,
    min_train_rows: int = 15,
) -> dict | None:
    """Date-grouped, gapped cross-validated OOS R^2, Clark-West t, and rank IC for any
    ``predict_fn(train_fold, test_fold) -> predictions aligned to test_fold.index``.

    The single CV harness every rung of the addendum's ladder goes through. A fold whose
    ``predict_fn`` raises ``ValueError`` (too few usable rows for that fold, e.g. a signal
    with sparse coverage) is skipped rather than failing the whole evaluation, matching how
    the original single-candidate loop handled it.
    """
    splits = date_grouped_gapped_splits(data["date"], n_folds=n_folds, gap_periods=gap_periods)
    actual_folds, pred_folds, date_folds = [], [], []
    for train_dates, test_dates in splits:
        train_fold = data[data["date"].isin(train_dates)]
        test_fold = data[data["date"].isin(test_dates)]
        if len(train_fold) < min_train_rows or test_fold.empty:
            continue
        try:
            predicted = np.asarray(predict_fn(train_fold, test_fold), dtype=float)
        except ValueError:
            continue
        if len(predicted) != len(test_fold):
            continue
        actual_folds.append(test_fold["target"].to_numpy())
        pred_folds.append(predicted)
        date_folds.append(test_fold["date"])
    if not actual_folds:
        return None
    actual = np.concatenate(actual_folds)
    predicted = np.concatenate(pred_folds)
    dates = pd.concat(date_folds, ignore_index=True)
    r2 = oos_r_squared(actual, predicted)
    cw_t, cw_p = clark_west_statistic(actual, np.zeros_like(actual), predicted)
    ic = rank_information_coefficient(pd.Series(predicted), pd.Series(actual), dates)
    return {
        "n_obs": len(data),
        "n_cv_test_obs": len(actual),
        "cv_oos_r2": r2,
        "clark_west_t": cw_t,
        "clark_west_p": cw_p,
        "rank_ic_mean": ic["mean_ic"],
        "rank_ic_t": ic["t_stat"],
        "rank_ic_n_dates": ic["n_dates"],
    }


@dataclass
class CandidateSpec:
    """A model on the ladder, named and reduced to ``fit``/``predict`` so both cross-
    validation and the eventual one-time holdout fit go through the same two calls."""

    label: str
    feature_columns: list[str]
    fit: Callable[[pd.DataFrame], object]
    predict: Callable[[object, pd.DataFrame], np.ndarray]
    describe: Callable[[object], dict] = field(default=lambda model: {})  # noqa: ANN001

    def evaluate(self, data: pd.DataFrame, n_folds: int, gap_periods: int) -> dict | None:
        def _predict_fn(train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
            model = self.fit(train)
            return self.predict(model, test)

        outcome = evaluate_candidate(data, _predict_fn, n_folds, gap_periods)
        if outcome is not None:
            outcome["signal"] = self.label
        return outcome


def _panel_predict(model: PanelRegressionResult, frame: pd.DataFrame) -> np.ndarray:
    return model.predict(frame[model.feature_columns]).to_numpy()


def single_or_combined_spec(label: str, columns: list[str], maxlags: int) -> CandidateSpec:
    """The pre-existing candidates: one Driscoll-Kraay regression on ``columns`` (a single
    signal, or every available signal stacked -- the "kitchen sink" the addendum warns the
    simple average is meant to beat)."""

    def _describe(model: PanelRegressionResult) -> dict:
        return {"coefficients": model.coefficients.to_dict(), "t_stats": model.t_stats.to_dict()}

    return CandidateSpec(
        label=label,
        feature_columns=columns,
        fit=lambda train: fit_driscoll_kraay(train, columns, maxlags=maxlags),
        predict=_panel_predict,
        describe=_describe,
    )


@dataclass
class SimpleAverageModel:
    """Simple average of each signal's own univariate Driscoll-Kraay forecast.

    A row missing one signal still gets a prediction, averaged over whichever component
    models have a usable value for that row, rather than being dropped or having the
    missing signal silently treated as zero -- the point of forecast combination is that a
    row's other signals shouldn't be thrown away because one is missing.
    """

    models: dict[str, PanelRegressionResult]
    feature_columns: list[str]

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        if not self.models:
            return pd.Series(np.nan, index=frame.index)
        components = pd.DataFrame(index=frame.index)
        for column, model in self.models.items():
            mask = frame[column].notna()
            values = pd.Series(np.nan, index=frame.index)
            if mask.any():
                values.loc[mask] = model.predict(frame.loc[mask, [column]]).to_numpy()
            components[column] = values
        return components.mean(axis=1, skipna=True)


def fit_simple_average(
    train: pd.DataFrame, feature_columns: list[str], maxlags: int, min_observations: int = 30
) -> SimpleAverageModel:
    """One univariate Driscoll-Kraay regression per signal, combined by simple average.

    A signal without ``min_observations`` usable training rows on its own is dropped from
    the combination instead of failing the whole fit -- exactly the robustness a forecast
    average is supposed to buy over a kitchen-sink regression, where one sparse or noisy
    column can drag down every prediction.
    """
    models: dict[str, PanelRegressionResult] = {}
    for column in feature_columns:
        sub_train = train.dropna(subset=[column, "target"])
        if len(sub_train) < min_observations:
            continue
        try:
            models[column] = fit_driscoll_kraay(sub_train, [column], maxlags=maxlags)
        except ValueError:
            continue
    return SimpleAverageModel(models=models, feature_columns=feature_columns)


def simple_average_spec(columns: list[str], maxlags: int) -> CandidateSpec:
    def _describe(model: SimpleAverageModel) -> dict:
        return {"components_used": sorted(model.models.keys())}

    return CandidateSpec(
        label="simple_average",
        feature_columns=columns,
        fit=lambda train: fit_simple_average(train, columns, maxlags=maxlags),
        predict=lambda model, frame: model.predict(frame).to_numpy(),
        describe=_describe,
    )


@dataclass
class ShrunkLinearModel:
    """A fitted ridge or lasso model over the cross-sectionally standardized signals.

    Wraps ``sklearn`` so nothing outside this function imports it directly, the same
    one-import-boundary-per-dependency convention ``panel.py`` uses for ``statsmodels``.
    """

    method: str
    alpha: float
    intercept: float
    coefficients: pd.Series
    feature_columns: list[str]

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        design = frame.loc[:, self.feature_columns].fillna(0.0)
        values = self.intercept + design.to_numpy() @ self.coefficients.to_numpy()
        return pd.Series(values, index=frame.index)


def fit_ridge_or_lasso(
    train: pd.DataFrame, feature_columns: list[str], alpha: float, method: str = "ridge"
) -> ShrunkLinearModel:
    """Fit ridge (``method="ridge"``) or lasso (``method="lasso"``) on rows with every
    feature present. Unlike the simple average, a shrinkage regression needs one shared
    design matrix, so a row missing any signal is dropped rather than filled -- the same
    rule :func:`~semimc.forecasting.panel.fit_driscoll_kraay` uses.
    """
    from sklearn.linear_model import Lasso, Ridge

    if method not in ("ridge", "lasso"):
        raise ValueError(f"method must be 'ridge' or 'lasso', got {method!r}")
    usable = train.dropna(subset=[*feature_columns, "target"])
    if len(usable) < 30:
        raise ValueError(f"only {len(usable)} usable rows for a {method} fit; need >= 30")
    estimator = (
        Ridge(alpha=alpha, fit_intercept=True)
        if method == "ridge"
        else Lasso(alpha=alpha, fit_intercept=True, max_iter=10_000)
    )
    estimator.fit(usable.loc[:, feature_columns].to_numpy(), usable["target"].to_numpy())
    return ShrunkLinearModel(
        method=method,
        alpha=alpha,
        intercept=float(estimator.intercept_),
        coefficients=pd.Series(estimator.coef_, index=feature_columns),
        feature_columns=feature_columns,
    )


def shrinkage_spec(columns: list[str], alpha: float, method: str) -> CandidateSpec:
    def _describe(model: ShrunkLinearModel) -> dict:
        return {"alpha": model.alpha, "coefficients": model.coefficients.to_dict()}

    return CandidateSpec(
        label=f"{method}(alpha={alpha:g})",
        feature_columns=columns,
        fit=lambda train: fit_ridge_or_lasso(train, columns, alpha=alpha, method=method),
        predict=lambda model, frame: model.predict(frame).to_numpy(),
        describe=_describe,
    )


def select_alpha_by_cv(
    data: pd.DataFrame,
    feature_columns: list[str],
    method: str,
    alphas: list[float],
    n_folds: int,
    gap_periods: int,
) -> tuple[float, dict | None]:
    """Pick the alpha with the best (least negative) CV OOS R^2 among ``alphas``, using only
    ``data`` -- callers pass pre-holdout rows only, so tuning never touches the holdout, the
    same discipline the addendum requires for the shrinkage lambda.

    Caveat, disclosed rather than engineered around: the same folds used to pick ``alpha``
    are then reported as that alpha's CV performance, which optimistically biases the
    reported ridge/lasso R^2 versus a true nested CV. With only a few hundred pre-holdout
    rows split into a handful of gapped folds, a further nested split would starve each
    inner fold below the >= 30-row floor the fits themselves require. Recorded in
    ``research_log.md``.
    """
    best_alpha, best_outcome, best_score = alphas[0], None, float("-inf")
    for alpha in alphas:
        spec = shrinkage_spec(feature_columns, alpha, method)
        outcome = spec.evaluate(data, n_folds, gap_periods)
        if outcome is None:
            continue
        score = outcome["cv_oos_r2"]
        if np.isfinite(score) and score > best_score:
            best_score, best_alpha, best_outcome = score, alpha, outcome
    return best_alpha, best_outcome


@dataclass
class QuantileRegressionModel:
    quantile: float
    coefficients: pd.Series
    feature_columns: list[str]

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        from statsmodels.tools.tools import add_constant

        design = add_constant(frame.loc[:, self.feature_columns], has_constant="add")
        values = design.to_numpy() @ self.coefficients.reindex(design.columns).to_numpy()
        return pd.Series(values, index=frame.index)


def fit_quantile_regressions(
    train: pd.DataFrame,
    feature_columns: list[str],
    quantiles: tuple[float, ...] = (0.05, 0.25, 0.5, 0.75, 0.95),
) -> dict[float, QuantileRegressionModel]:
    """One ``statsmodels`` ``QuantReg`` fit per quantile level.

    Unlike every candidate above, this predicts the shape of the conditional distribution,
    not just its centre -- the addendum's reason for including it: a signal might widen a
    tail without moving the mean forecast at all, and only a quantile fit shows that. A
    quantile that fails to converge (common at 5%/95% with a small panel) is dropped from
    the returned dict rather than failing every other quantile's fit.
    """
    from statsmodels.regression.quantile_regression import QuantReg
    from statsmodels.tools.tools import add_constant

    usable = train.dropna(subset=[*feature_columns, "target"])
    if len(usable) < 30:
        raise ValueError(f"only {len(usable)} usable rows for quantile regression; need >= 30")
    design = add_constant(usable.loc[:, feature_columns], has_constant="add")
    model = QuantReg(usable["target"].to_numpy(), design.to_numpy())
    out: dict[float, QuantileRegressionModel] = {}
    for q in quantiles:
        try:
            fitted = model.fit(q=q, max_iter=2000)
        except (ValueError, np.linalg.LinAlgError):
            continue
        out[q] = QuantileRegressionModel(
            quantile=q,
            coefficients=pd.Series(fitted.params, index=design.columns),
            feature_columns=feature_columns,
        )
    return out


def predict_quantiles(
    models: dict[float, QuantileRegressionModel], frame: pd.DataFrame
) -> dict[float, np.ndarray]:
    return {q: model.predict(frame).to_numpy() for q, model in models.items()}


def cross_validated_pinball_losses(
    data: pd.DataFrame,
    feature_columns: list[str],
    quantiles: tuple[float, ...],
    n_folds: int,
    gap_periods: int,
    min_train_rows: int = 15,
) -> dict[float, float]:
    """Mean pinball loss per quantile, out of fold, over the same date-grouped gapped
    splits every other candidate is scored on."""
    from .evaluate import pinball_losses

    splits = date_grouped_gapped_splits(data["date"], n_folds=n_folds, gap_periods=gap_periods)
    actual_by_q: dict[float, list[np.ndarray]] = {q: [] for q in quantiles}
    pred_by_q: dict[float, list[np.ndarray]] = {q: [] for q in quantiles}
    for train_dates, test_dates in splits:
        train_fold = data[data["date"].isin(train_dates)]
        test_fold = data[data["date"].isin(test_dates)]
        if len(train_fold) < min_train_rows or test_fold.empty:
            continue
        try:
            models = fit_quantile_regressions(train_fold, feature_columns, quantiles)
        except ValueError:
            continue
        predictions = predict_quantiles(models, test_fold)
        for q, preds in predictions.items():
            actual_by_q[q].append(test_fold["target"].to_numpy())
            pred_by_q[q].append(preds)
    if not any(actual_by_q.values()):
        return {q: float("nan") for q in quantiles}
    combined_predictions = {
        q: (np.concatenate(pred_by_q[q]) if pred_by_q[q] else np.array([])) for q in quantiles
    }
    actual_all = {
        q: (np.concatenate(actual_by_q[q]) if actual_by_q[q] else np.array([])) for q in quantiles
    }
    losses: dict[float, float] = {}
    for q in quantiles:
        if len(actual_all[q]) < 5:
            losses[q] = float("nan")
            continue
        losses[q] = pinball_losses({q: combined_predictions[q]}, actual_all[q])[q]
    return losses
