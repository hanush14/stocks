"""Conflict-of-interest and convergence signals.

Two findings live here:

* **Jurisdiction split** - a filer's alpha inside the sectors their committee
  legislates over, against everything else. A persistent gap across many trades
  is a far stronger pattern than any single suspicious-looking trade, because it
  is much harder to produce by chance.
* **Conviction clusters** - several independent high-confidence entities buying
  the same security inside a bounded window. The window is the whole point:
  without it, "cluster" degenerates into "several people own this stock".
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import fmean
from typing import Any, Iterable

CLUSTER_WINDOW_DAYS = 180
CLUSTER_MIN_MEMBERS = 3


def _d(s: str | date) -> date:
    return s if isinstance(s, date) else datetime.strptime(s[:10], "%Y-%m-%d").date()


def in_jurisdiction(store, txn: Any) -> bool:
    """Was this trade inside the filer's own legislative jurisdiction?

    Evaluated against the committee assignments in force on the **trade date**,
    not today's - a reassignment must not retroactively reclassify old trades.
    """
    t = dict(txn)
    if not t.get("ticker"):
        return False
    sector = store.sector_of(t["ticker"])
    if not sector:
        return False
    return sector in store.sectors_for(t["filer_id"], t["txn_date"])


def jurisdiction_split(store, perf, filer_id: str, *, as_of: str | None = None,
                       owner: str | None = "self", metric: str = "residual") -> dict:
    """Mean alpha in-jurisdiction vs out, plus the gap between them.

    Each side carries its own n. Per-sector samples are much smaller than the
    overall sample, so a filer can legitimately have a confident overall score
    and too little data on either side of this split.
    """
    txns = store.transactions(as_of=as_of, filer_id=filer_id, owner=owner,
                              directed_only=True)
    inside, outside = [], []
    for t in txns:
        d = perf.decay_for(t)
        if not d:
            continue
        (inside if in_jurisdiction(store, t) else outside).append(d[metric])
    mi = fmean(inside) if inside else None
    mo = fmean(outside) if outside else None
    return {
        "filer_id": filer_id, "metric": metric,
        "in_jurisdiction": mi, "n_in": len(inside),
        "out_jurisdiction": mo, "n_out": len(outside),
        "gap": (mi - mo) if (mi is not None and mo is not None) else None,
    }


def conviction_clusters(store, ticker: str, *, strong: set[str],
                        as_of: str | None = None,
                        window_days: int = CLUSTER_WINDOW_DAYS,
                        min_members: int = CLUSTER_MIN_MEMBERS) -> dict | None:
    """Tightest `window_days` window containing the most distinct high-confidence
    buyers of `ticker`. Returns None when the threshold is not met.

    `strong` is the set of filer ids that cleared the confidence and
    significance gates - clustering weak filers together does not make them
    informative.
    """
    txns = [dict(t) for t in store.transactions(as_of=as_of, ticker=ticker)
            if t["action"] == "purchase" and t["filer_id"] in strong]
    if len(txns) < min_members:
        return None
    # one entry per filer: the earliest disclosure, so a serial buyer counts once
    first: dict[str, dict] = {}
    for t in txns:
        cur = first.get(t["filer_id"])
        if cur is None or _d(t["disclosed_date"]) < _d(cur["disclosed_date"]):
            first[t["filer_id"]] = t
    entries = sorted(first.values(), key=lambda t: _d(t["disclosed_date"]))

    best: dict | None = None
    for i, anchor in enumerate(entries):
        lo = _d(anchor["disclosed_date"])
        win = [e for e in entries[i:]
               if (_d(e["disclosed_date"]) - lo).days <= window_days]
        if best is None or len(win) > best["n"]:
            best = {
                "ticker": ticker, "n": len(win),
                "span_days": (_d(win[-1]["disclosed_date"]) - lo).days,
                "start": win[0]["disclosed_date"], "end": win[-1]["disclosed_date"],
                "members": [e["filer_id"] for e in win],
            }
    if best and best["n"] >= min_members:
        return best
    return None


def scan_clusters(store, *, strong: set[str], as_of: str | None = None,
                  **kw) -> list[dict]:
    """Every security meeting the cluster test, strongest convergence first."""
    tickers = {t["ticker"] for t in store.transactions(as_of=as_of) if t["ticker"]}
    out = []
    for tk in sorted(tickers):
        c = conviction_clusters(store, tk, strong=strong, as_of=as_of, **kw)
        if c:
            out.append(c)
    out.sort(key=lambda c: (-c["n"], c["span_days"]))
    return out


def price_gap_screen(store, perf, cluster_or_txns: Iterable[Any],
                     *, as_of: str | None = None, max_runup: float = 10.0) -> list[dict]:
    """Securities a tracked entity bought that have not yet re-rated.

    This is a **filter, not a signal**. A flat price since a good filer's entry
    can mean the thesis has not played out - or that the market has already
    decided it is wrong. It says nothing on its own and must sit behind the
    entity's own track record.
    """
    as_of = as_of or date.today().isoformat()
    out = []
    for row in cluster_or_txns:
        t = dict(row)
        tk = t.get("ticker")
        if not tk:
            continue
        entry = store.price_on(tk, t["disclosed_date"])
        now = store.price_on(tk, as_of)
        if entry is None or now is None or entry == 0:
            continue
        runup = (now / entry - 1.0) * 100.0
        if runup <= max_runup:
            out.append({"ticker": tk, "filer_id": t["filer_id"],
                        "disclosed": t["disclosed_date"],
                        "price_at_disclosure": entry, "price_now": now,
                        "runup_pct": runup})
    out.sort(key=lambda r: r["runup_pct"])
    return out
