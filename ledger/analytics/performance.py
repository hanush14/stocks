"""Returns, benchmark-relative alpha, and the alpha-decay profile.

Definitions used throughout (all measured to a **common endpoint** so the three
figures are comparable):

    horizon end   H = trade date + `horizon` days
    total alpha       abnormal return from the trade date to H
    residual alpha    abnormal return from the disclosure date to H
    tradable alpha    abnormal return from disclosure + `settle` days to H

Total alpha is what the filer earned. Residual alpha is what was still on the
table when the filing became public. Tradable alpha is what survived the market
reading it. Ranking on the first produces a leaderboard nobody can act on; this
module exists so the product can rank on the third.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import fmean, pstdev
from typing import Any, Iterable

from ..schema import NON_DIRECTED

BUY_ACTIONS = {"purchase"}
SELL_ACTIONS = {"sale", "sale_partial", "sale_full"}


def _d(s: str | date) -> date:
    return s if isinstance(s, date) else datetime.strptime(s[:10], "%Y-%m-%d").date()


def shift(d: str | date, days: int) -> str:
    return (_d(d) + timedelta(days=days)).isoformat()


def pct_change(a: float, b: float) -> float:
    """Percentage change from a to b, in percentage points."""
    if a == 0:
        raise ZeroDivisionError("zero base price")
    return (b / a - 1.0) * 100.0


def amount_midpoint(low: float | None, high: float | None) -> float | None:
    """Midpoint of a disclosure band, for weighting only.

    Never present this as the trade size: the filing disclosed a range. The
    open-ended top band has no midpoint, so it returns its lower bound and
    callers should treat the result as a lower bound too.
    """
    if low is None:
        return None
    if high is None:
        return low
    return (low + high) / 2.0


class Performance:
    """Computes returns against a Store. Sector benchmarks are looked up by the
    security's sector; a missing price or benchmark yields None rather than a
    silently wrong number."""

    def __init__(self, store, *, horizon: int = 180, settle: int = 2,
                 benchmark_prefix: str = "SECTOR:", fallback_benchmark: str = "MARKET"):
        self.s = store
        self.horizon = horizon
        self.settle = settle
        self.bpfx = benchmark_prefix
        self.fallback = fallback_benchmark

    # -- building blocks ------------------------------------------------------

    def benchmark_series(self, ticker: str) -> str:
        sector = self.s.sector_of(ticker)
        return f"{self.bpfx}{sector}" if sector else self.fallback

    def asset_return(self, ticker: str, d0: str, d1: str) -> float | None:
        p0 = self.s.price_on(ticker, d0)
        p1 = self.s.price_on(ticker, d1)
        if p0 is None or p1 is None or p0 == 0:
            return None
        return pct_change(p0, p1)

    def benchmark_return(self, ticker: str, d0: str, d1: str) -> float | None:
        series = self.benchmark_series(ticker)
        b0 = self.s.benchmark_on(series, d0)
        b1 = self.s.benchmark_on(series, d1)
        if b0 is None or b1 is None:
            if series == self.fallback:
                return None
            b0 = self.s.benchmark_on(self.fallback, d0)
            b1 = self.s.benchmark_on(self.fallback, d1)
        if b0 is None or b1 is None or b0 == 0:
            return None
        return pct_change(b0, b1)

    def abnormal_return(self, ticker: str, d0: str, d1: str) -> float | None:
        """Asset return minus its benchmark over the same window, in pp."""
        a = self.asset_return(ticker, d0, d1)
        b = self.benchmark_return(ticker, d0, d1)
        if a is None or b is None:
            return None
        return a - b

    # -- per-transaction decay ------------------------------------------------

    def decay_for(self, txn: Any) -> dict | None:
        """Three abnormal returns for one purchase, to a common endpoint.

        Sales are skipped: a disposal has no forward return to attribute, and
        scoring one as if it did double-counts the position.
        """
        t = dict(txn)
        if t["action"] not in BUY_ACTIONS or not t.get("ticker"):
            return None
        end = shift(t["txn_date"], self.horizon)
        disc = t["disclosed_date"]
        if _d(disc) >= _d(end):
            # disclosed after the measurement window closed: no tradable leg
            total = self.abnormal_return(t["ticker"], t["txn_date"], end)
            if total is None:
                return None
            return {"txn_id": t["txn_id"], "ticker": t["ticker"],
                    "total": total, "residual": 0.0, "tradable": 0.0,
                    "lag": (_d(disc) - _d(t["txn_date"])).days, "truncated": True}
        total = self.abnormal_return(t["ticker"], t["txn_date"], end)
        residual = self.abnormal_return(t["ticker"], disc, end)
        tradable = self.abnormal_return(t["ticker"], shift(disc, self.settle), end)
        if total is None or residual is None:
            return None
        return {
            "txn_id": t["txn_id"], "ticker": t["ticker"],
            "total": total, "residual": residual,
            "tradable": tradable if tradable is not None else residual,
            "lag": (_d(disc) - _d(t["txn_date"])).days, "truncated": False,
        }

    # -- position book --------------------------------------------------------

    def positions(self, txns: Iterable[Any], *, as_of: str | None = None) -> dict:
        """Split a transaction stream into closed round-trips and open positions.

        Open positions are marked to market and reported separately: blending
        them with realised results lets an entity hide losses by never selling.
        """
        as_of = as_of or date.today().isoformat()
        book: dict[str, list[dict]] = {}
        closed, open_pos = [], []
        for row in txns:
            t = dict(row)
            tk = t.get("ticker")
            if not tk:
                continue
            if t["action"] in BUY_ACTIONS:
                book.setdefault(tk, []).append(t)
            elif t["action"] in SELL_ACTIONS:
                lots = book.get(tk) or []
                if lots:                                  # FIFO
                    lot = lots.pop(0)
                    r = self.abnormal_return(tk, lot["txn_date"], t["txn_date"])
                    closed.append({"ticker": tk, "entry": lot["txn_date"],
                                   "exit": t["txn_date"], "alpha": r,
                                   "owner": lot["owner"]})
        for tk, lots in book.items():
            for lot in lots:
                r = self.abnormal_return(tk, lot["txn_date"], as_of)
                open_pos.append({"ticker": tk, "entry": lot["txn_date"],
                                 "as_of": as_of, "alpha": r, "owner": lot["owner"]})
        return {"closed": closed, "open": open_pos}

    # -- entity roll-up -------------------------------------------------------

    def entity_profile(self, filer_id: str, *, as_of: str | None = None,
                       owner: str | None = "self", sector: str | None = None) -> dict:
        """Aggregate decay figures for one filer.

        `owner='self'` is the default because a professionally-trading spouse
        would otherwise be scored as the member's own skill. Pass None to
        include every owner, or a sector name to score one sector only.
        """
        txns = self.s.transactions(as_of=as_of, filer_id=filer_id,
                                   owner=owner, directed_only=True)
        rows = []
        for t in txns:
            if sector and self.s.sector_of(t["ticker"]) != sector:
                continue
            d = self.decay_for(t)
            if d:
                rows.append(d)
        n = len(rows)
        if not n:
            return {"filer_id": filer_id, "n": 0, "scope": sector or "all",
                    "total": None, "residual": None, "tradable": None,
                    "kept": None, "median_lag": None, "per_trade": []}
        tot = [r["total"] for r in rows]
        res = [r["residual"] for r in rows]
        trd = [r["tradable"] for r in rows]
        lags = sorted(r["lag"] for r in rows)
        mt, mr = fmean(tot), fmean(res)
        return {
            "filer_id": filer_id, "n": n, "scope": sector or "all",
            "total": mt, "residual": mr, "tradable": fmean(trd),
            "kept": (mr / mt) if mt not in (0, None) else None,
            "dispersion": pstdev(res) if n > 1 else 0.0,
            "median_lag": lags[len(lags) // 2],
            "per_trade": rows,
        }
