"""Statistics for ranking many entities at once.

The problem this module exists to solve: with ~535 members of Congress plus
thousands of institutional filers, ranking by alpha is a machine for surfacing
luck. At a 5% threshold roughly 27 members clear significance by chance alone.
Confidence intervals do not fix that; controlling the false discovery rate does.

Implemented in pure Python (no numpy/scipy) so ingestion hosts stay light.
"""
from __future__ import annotations

import math
import random
from statistics import fmean, stdev


# --- incomplete beta, for the Student-t tail ---------------------------------

def _betacf(a: float, b: float, x: float, *, itmax: int = 200, eps: float = 3e-16) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - math.exp(
        lbeta + b * math.log1p(-x) + a * math.log(x)) * _betacf(b, a, 1.0 - x) / b


def t_sf(t: float, df: float) -> float:
    """Two-sided survival function for Student's t."""
    if df <= 0:
        return 1.0
    return betainc(df / 2.0, 0.5, df / (df + t * t))


def ttest_1samp(xs: list[float], popmean: float = 0.0) -> tuple[float, float]:
    """One-sample t-test. Returns (t, two-sided p). p=1.0 when undefined."""
    n = len(xs)
    if n < 2:
        return (0.0, 1.0)
    sd = stdev(xs)
    if sd == 0.0:
        return (0.0, 0.0 if fmean(xs) != popmean else 1.0)
    t = (fmean(xs) - popmean) / (sd / math.sqrt(n))
    return (t, t_sf(t, n - 1))


# --- multiple comparisons ----------------------------------------------------

def fdr_bh(pvalues: list[float]) -> list[float]:
    """Benjamini-Hochberg q-values, returned in the input order.

    A q-value is the smallest FDR at which that hypothesis is called
    significant. Comparing q to 0.05 controls the *expected proportion of false
    discoveries among those called significant*, which is the guarantee a
    leaderboard actually needs.
    """
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [0.0] * m
    prev = 1.0
    # walk from the largest p down, enforcing monotonicity
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        val = min(prev, pvalues[i] * m / rank)
        q[i] = min(1.0, val)
        prev = q[i]
    return q


def bootstrap_ci(xs: list[float], *, iters: int = 2000, alpha: float = 0.05,
                 seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap CI for the mean. Seeded, so a published interval
    is reproducible from the same inputs."""
    n = len(xs)
    if n == 0:
        return (float("nan"), float("nan"))
    if n == 1:
        return (xs[0], xs[0])
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        means.append(fmean([xs[rng.randrange(n)] for _ in range(n)]))
    means.sort()
    lo = means[int((alpha / 2) * iters)]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters))]
    return (lo, hi)


# --- how a rank is allowed to be displayed -----------------------------------

MIN_SAMPLE = 12          # below this, no score is shown at all
Q_THRESHOLD = 0.05


def classify(n: int, q: float | None, mean_alpha: float | None,
             *, min_sample: int = MIN_SAMPLE, q_threshold: float = Q_THRESHOLD) -> str:
    """Label a result. `insufficient` beats everything: too few observations is
    not a weak finding, it is the absence of one."""
    if n < min_sample or q is None or mean_alpha is None:
        return "insufficient"
    if q > q_threshold:
        return "chance"
    return "negative" if mean_alpha < 0 else "significant"


def score_0_100(mean_alpha: float, consistency: float, n: int,
                *, min_sample: int = MIN_SAMPLE) -> int | None:
    """Composite confidence score.

    Deliberately blunt: it rewards magnitude, penalises dispersion, and is
    damped by sample size so a hot streak of 13 trades cannot outrank a long
    steady record. Returns None below the sample floor rather than a number.
    """
    if n < min_sample:
        return None
    # squash alpha (percentage points) into 0..1, +10pp -> ~0.73
    a = math.tanh(mean_alpha / 10.0)
    # consistency: 1 when dispersion is small relative to the effect
    c = 1.0 / (1.0 + consistency)
    # sample damping: 0.5 at the floor, ->1 with a long record
    s = n / (n + min_sample)
    raw = (0.6 * a + 0.4 * (a * c)) * s
    return max(0, min(100, round(50 + 50 * raw)))
