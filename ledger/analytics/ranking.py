"""Cohort ranking with false-discovery-rate control.

Ranking one filer is a measurement. Ranking a whole cohort is a multiple
comparisons problem, and ignoring that is how a leaderboard fills up with luck.
Every filer is tested individually, then the whole cohort's p-values are passed
through Benjamini-Hochberg together, so `significance` means "survives
correction across everyone ranked", not "beat 0.05 on its own".
"""
from __future__ import annotations

from datetime import date
from statistics import fmean, pstdev

from .stats import MIN_SAMPLE, Q_THRESHOLD, bootstrap_ci, classify, fdr_bh, score_0_100, ttest_1samp

MODEL_VERSION = "1.0.0"

# Which leg of the decay profile a rank is built on. `tradable` is the default
# because it is the only one anyone outside the filing entity could have acted on.
DEFAULT_METRIC = "tradable"


def rank_cohort(store, perf, *, filer_ids=None, as_of: str | None = None,
                metric: str = DEFAULT_METRIC, owner: str | None = "self",
                scope: str | None = None, min_sample: int = MIN_SAMPLE,
                q_threshold: float = Q_THRESHOLD) -> list[dict]:
    """Score and rank filers, FDR-corrected across the cohort.

    Filers below the sample floor are still returned - with `score=None` and
    `significance='insufficient'` - because hiding them would misrepresent how
    thin the underlying disclosure data is.
    """
    as_of = as_of or date.today().isoformat()
    if filer_ids is None:
        filer_ids = [r["filer_id"] for r in store.filers()]

    rows, testable = [], []
    for fid in filer_ids:
        prof = perf.entity_profile(fid, as_of=as_of, owner=owner, sector=scope)
        xs = [r[metric] for r in prof["per_trade"]]
        n = len(xs)
        row = {
            "filer_id": fid, "n": n, "scope": scope or "all", "metric": metric,
            "total": prof["total"], "residual": prof["residual"],
            "tradable": prof["tradable"], "kept": prof["kept"],
            "median_lag": prof["median_lag"],
            "value": fmean(xs) if xs else None,
            "dispersion": pstdev(xs) if n > 1 else 0.0,
            "p_value": None, "q_value": None, "ci_low": None, "ci_high": None,
        }
        if n >= min_sample:
            _, p = ttest_1samp(xs)
            row["p_value"] = p
            lo, hi = bootstrap_ci(xs)
            row["ci_low"], row["ci_high"] = lo, hi
            testable.append(row)
        rows.append(row)

    # FDR across everything actually tested, in one pass
    if testable:
        qs = fdr_bh([r["p_value"] for r in testable])
        for r, q in zip(testable, qs):
            r["q_value"] = q

    for r in rows:
        r["significance"] = classify(r["n"], r["q_value"], r["value"],
                                     min_sample=min_sample, q_threshold=q_threshold)
        if r["significance"] == "insufficient" or r["value"] is None:
            r["score"] = None
        else:
            cv = min(10.0, r["dispersion"] / (abs(r["value"]) + 1e-9))
            r["score"] = score_0_100(r["value"], cv, r["n"], min_sample=min_sample)

    rows.sort(key=lambda r: (r["value"] is None, -(r["value"] or 0.0)))
    for i, r in enumerate(rows, 1):
        r["rank"] = i if r["value"] is not None else None
    return rows


def strong_set(ranked: list[dict], *, min_score: int = 65) -> set[str]:
    """Filers a cluster is allowed to be built from: significant positive alpha
    and a score above the bar. Clustering weak filers proves nothing."""
    return {r["filer_id"] for r in ranked
            if r["significance"] == "significant" and (r["score"] or 0) >= min_score}


def persist(store, ranked: list[dict], *, as_of: str, model_version: str = MODEL_VERSION) -> int:
    """Write a ranking run to the scores table, tagged with the model version so
    a published rank stays reproducible and 'why did I move' is answerable."""
    out = []
    for r in ranked:
        for metric in ("value", "score", "total", "residual", "tradable"):
            if r.get(metric) is None:
                continue
            out.append({
                "filer_id": r["filer_id"], "as_of": as_of,
                "model_version": model_version, "scope": r["scope"],
                "metric": "alpha" if metric == "value" else metric,
                "value": r[metric], "n": r["n"],
                "ci_low": r["ci_low"], "ci_high": r["ci_high"],
                "p_value": r["p_value"], "q_value": r["q_value"],
                "significance": r["significance"],
            })
    return store.save_scores(out)
