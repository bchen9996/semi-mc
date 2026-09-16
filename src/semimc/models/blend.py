"""Model blending: whole simulated paths drawn from several fitted models.

The addendum's method is specific: blend by drawing **whole paths** from each component in
proportion to its weight, so a path keeps its component's internal structure -- a Merton
path still has that component's jump distribution end to end, a GARCH path still has that
component's volatility clustering. Averaging *distributions* pointwise (mixing densities)
would instead produce a return that no single model could ever generate.

Fitting that idea into :class:`~semimc.models.base.BaseReturnModel` without a special case
turns out to be exact, not approximate. Every component already has the *same* target
annual arithmetic drift ``mu_A`` (the configured policy, identical across models by
construction), so each component's own bias-corrected daily drift is
``mu_A - V_i/2`` where ``V_i`` is that component's :meth:`annualized_variance`. Because
``mu_A`` is linear and shared, the weighted average of those individual drifts is exactly

``sum_i w_i * (mu_A - V_i/2) = mu_A - (sum_i w_i * V_i) / 2``

-- i.e. exactly the drift the base class computes on its own if the blend reports
``sum_i w_i * V_i`` as *its* ``annualized_variance``. So ``BlendModel`` needs only the
three methods every model implements: it draws each path's **shape** from one component's
own ``_simulate_zero_mean`` (unweighted by drift), and lets the shared base-class machinery
add the drift once, uniformly. The result is identical, to floating-point precision, to
computing each component's drift separately and averaging it -- see
``test_blend_drift_matches_component_average`` -- while requiring no change to the
interface every other model already satisfies.

The one place this is a real simplification: a path drawn from a high-variance component
technically "should" carry a slightly different bias correction than the blend average
(the Ito term is component-specific). The difference is second order -- half the *gap*
between that component's variance and the blend's average variance, applied to a single
day -- and is documented rather than hidden.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from scipy import optimize as scipy_optimize

from ..analytics.scoring import crps_from_samples
from .base import BaseReturnModel, FitContext

LOGGER = logging.getLogger(__name__)


class BlendModel(BaseReturnModel):
    """Weighted blend of fitted component models, combined by whole simulated paths."""

    name = "blend"

    def __init__(
        self,
        components: dict[str, BaseReturnModel],
        weights: dict[str, float] | None = None,
        dtype: str = "float64",
    ) -> None:
        if len(components) < 2:
            raise ValueError("a blend needs at least two components")
        super().__init__(dtype=dtype)
        self.components = components
        given = weights or dict.fromkeys(components, 1.0 / len(components))
        missing = set(components) - set(given)
        if missing:
            raise ValueError(f"no weight given for components: {sorted(missing)}")
        total = sum(given[name] for name in components)
        if total <= 0:
            raise ValueError("blend weights must sum to a positive number")
        self.weights = {name: given[name] / total for name in components}
        self.name = "blend(" + "+".join(f"{n}:{self.weights[n]:.2f}" for n in components) + ")"

    # -- fitting --------------------------------------------------------------------

    def _fit_shape(self, log_returns: pd.DataFrame, context: FitContext) -> None:
        for label, component in self.components.items():
            try:
                component.fit(log_returns, context)
            except Exception as exc:  # noqa: BLE001 - reported with the component's name attached
                raise RuntimeError(f"blend component {label!r} failed to fit: {exc}") from exc

    # -- drift input ----------------------------------------------------------------

    def annualized_variance(self, horizon: int) -> pd.Series:
        """Weight-averaged component variance -- see this module's docstring for why that
        makes the blend's drift exactly the weighted average of each component's own drift.
        """
        total = None
        for label, component in self.components.items():
            contribution = self.weights[label] * component.annualized_variance(horizon)
            total = contribution if total is None else total.add(contribution, fill_value=0.0)
        assert total is not None
        return total.reindex(self.tickers)

    # -- simulation -----------------------------------------------------------------

    def _simulate_zero_mean(
        self, n_paths: int, horizon: int, rng: np.random.Generator
    ) -> np.ndarray:
        """Assign each path to one component by weight, draw that component's own shocks.

        Assignment is a single multinomial draw over paths, so the exact path count per
        component is fixed in advance (matches the requested weights as closely as integer
        counts allow) rather than drifting run to run under a per-path coin flip.
        """
        labels = list(self.components)
        probabilities = np.array([self.weights[label] for label in labels])
        counts = rng.multinomial(n_paths, probabilities)

        out = np.empty((n_paths, horizon, len(self.tickers)), dtype=float)
        cursor = 0
        for label, count in zip(labels, counts, strict=True):
            if count == 0:
                continue
            component = self.components[label]
            block = component._simulate_zero_mean(int(count), horizon, rng)  # noqa: SLF001
            out[cursor : cursor + count] = block
            cursor += count
        return out

    def describe(self) -> dict:
        info = super().describe()
        info.update(
            {
                "weights": dict(self.weights),
                "components": {
                    label: component.describe() for label, component in self.components.items()
                },
            }
        )
        return info


# ---------------------------------------------------------------------------------------
# CRPS-optimized weights
# ---------------------------------------------------------------------------------------


def pooled_mixture_crps(
    component_samples: dict[str, np.ndarray],
    realized: np.ndarray,
    weights: dict[str, float],
    n_pool: int,
    rng: np.random.Generator,
) -> float:
    """Mean CRPS of the mixture forecast a real blend would produce, at fixed weights.

    Blending is genuinely nonlinear in its effect on CRPS: mixing two *distributions*, not
    just averaging two numbers, can reduce forecast error even when neither component wins
    on its own -- the calibration literature calls this "diversity" benefit, and it is the
    reason equal weights are often competitive with tuned ones. Minimizing a linear
    combination of each component's own mean CRPS cannot see that effect at all (it always
    puts every weight on the single best component), so weight optimization has to score an
    actual pooled sample the way a live blend would draw one.

    Parameters
    ----------
    component_samples
        ``{name: (n_origins, n_paths) array}`` of simple-return terminal samples, one row
        per forecast origin, same origin order and count for every component.
    realized
        ``(n_origins,)`` realized simple returns, same order.
    weights
        Candidate weights, need not be pre-normalized.
    n_pool
        Paths drawn from the pooled mixture per origin when scoring. Smaller than the raw
        per-component sample count is fine and keeps optimization cheap.
    rng
        Shared across calls by the caller for reproducibility.

    Returns
    -------
    float
        Mean CRPS across origins.
    """
    labels = list(component_samples)
    raw = np.array([max(weights.get(label, 0.0), 0.0) for label in labels])
    total = raw.sum()
    normalized = raw / total if total > 0 else np.full(len(labels), 1.0 / len(labels))

    n_origins = len(realized)
    counts = rng.multinomial(n_pool, normalized)
    scores = np.empty(n_origins)
    for origin in range(n_origins):
        pooled = np.concatenate(
            [
                rng.choice(component_samples[label][origin], size=count, replace=True)
                for label, count in zip(labels, counts, strict=True)
                if count > 0
            ]
        )
        scores[origin] = crps_from_samples(pooled, float(realized[origin]))
    return float(np.mean(scores))


def optimize_crps_weights(
    component_samples: dict[str, np.ndarray],
    realized: np.ndarray,
    n_pool: int = 1000,
    seed: int = 42,
    n_restarts: int = 4,
) -> dict[str, float]:
    """Non-negative, sum-to-one weights minimizing mean pooled-mixture CRPS.

    Optimized by SLSQP over the probability simplex, from several random starting points
    since the mixture-CRPS surface need not be convex in the weights. The addendum's
    instruction to fit this "only from past origins" is enforced by the caller: pass
    ``component_samples`` and ``realized`` restricted to origins strictly before the one
    being forecast.

    Returns
    -------
    dict[str, float]
        Weight per component label, summing to 1.
    """
    labels = list(component_samples)
    n = len(labels)
    if n < 2:
        raise ValueError("need at least two components to optimize blend weights")
    rng = np.random.default_rng(seed)

    def objective(raw_weights: np.ndarray) -> float:
        weights = dict(zip(labels, raw_weights, strict=True))
        return pooled_mixture_crps(component_samples, realized, weights, n_pool, rng)

    constraints = [{"type": "eq", "fun": lambda w: float(np.sum(w) - 1.0)}]
    bounds = [(0.0, 1.0)] * n

    starts = [np.full(n, 1.0 / n)]
    for _ in range(max(0, n_restarts - 1)):
        draw = rng.dirichlet(np.ones(n))
        starts.append(draw)

    best: scipy_optimize.OptimizeResult | None = None
    for start in starts:
        result = scipy_optimize.minimize(
            objective,
            start,
            method="SLSQP",
            bounds=bounds,
            constraints=constraints,
            options={"maxiter": 60, "ftol": 1e-5},
        )
        if best is None or (result.success and result.fun < best.fun):
            best = result

    assert best is not None
    weights = np.clip(best.x, 0.0, None)
    weights = weights / weights.sum() if weights.sum() > 0 else np.full(n, 1.0 / n)
    return dict(zip(labels, (float(w) for w in weights), strict=True))
