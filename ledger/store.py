"""Bitemporal SQLite store.

Two rules make this store worth having:

1. Nothing is ever updated in place. A correction (a PTR amendment, a re-parse
   of a scanned filing) writes a new row and stamps `superseded_at` on the old
   one, so "what did we believe on date X" stays answerable.
2. Every read goes through an as-of filter. A backtest run as of the disclosure
   date cannot see an amendment that arrived a month later, because the SQL
   cannot return it.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .schema import DDL

ISO = "%Y-%m-%d"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def as_iso(d: str | date | datetime) -> str:
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    return d


def stable_id(*parts: Any) -> str:
    """Deterministic id, so re-ingesting the same document is idempotent."""
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(DDL)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    # -- writes ---------------------------------------------------------------

    def upsert_filer(self, **f: Any) -> str:
        f.setdefault("market", "US")
        f.setdefault("kind", "politician")
        if "filer_id" not in f:
            f["filer_id"] = f.get("bioguide") or f.get("cik") or stable_id(f["name"], f["kind"])
        if isinstance(f.get("meta"), (dict, list)):
            f["meta"] = json.dumps(f["meta"], separators=(",", ":"))
        cols = ("filer_id", "kind", "name", "market", "chamber", "party",
                "state", "district", "bioguide", "cik", "meta")
        vals = [f.get(c) for c in cols]
        with self.tx() as db:
            db.execute(
                f"INSERT INTO filers ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                "ON CONFLICT(filer_id) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" for c in cols[1:]),
                vals,
            )
        return f["filer_id"]

    def add_document(self, *, filer_id: str, source: str, doc_type: str,
                     filed_date: str, raw: bytes, url: str | None = None,
                     raw_path: str | None = None) -> str:
        """Record a source document. Content-addressed, so the same bytes
        retrieved twice produce one row and every later number can cite it."""
        sha = hashlib.sha256(raw).hexdigest()
        doc_id = stable_id(source, filer_id, filed_date, sha)
        with self.tx() as db:
            db.execute(
                "INSERT OR IGNORE INTO documents "
                "(doc_id,filer_id,source,doc_type,filed_date,url,sha256,raw_path,retrieved_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (doc_id, filer_id, source, doc_type, as_iso(filed_date), url, sha,
                 raw_path, now_utc()),
            )
        return doc_id

    def add_filing(self, *, doc_id: str, filer_id: str, filed_date: str,
                   amends: str | None = None, extract_conf: float = 1.0,
                   recorded_at: str | None = None) -> str:
        """Insert a filing. If it amends an earlier one, the earlier filing and
        all of its transactions are superseded as of this filing's record time
        — not deleted, so an as-of query before that moment still sees them."""
        recorded_at = recorded_at or now_utc()
        filing_id = stable_id(doc_id, filer_id, filed_date, amends)
        with self.tx() as db:
            db.execute(
                "INSERT OR IGNORE INTO filings "
                "(filing_id,doc_id,filer_id,filed_date,amends,extract_conf,recorded_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (filing_id, doc_id, filer_id, as_iso(filed_date), amends,
                 extract_conf, recorded_at),
            )
            if amends:
                db.execute(
                    "UPDATE filings SET superseded_at=? "
                    "WHERE filing_id=? AND superseded_at IS NULL",
                    (recorded_at, amends),
                )
                db.execute(
                    "UPDATE transactions SET superseded_at=? "
                    "WHERE filing_id=? AND superseded_at IS NULL",
                    (recorded_at, amends),
                )
        return filing_id

    def add_transactions(self, filing_id: str, filer_id: str,
                         txns: Iterable[dict], recorded_at: str | None = None) -> list[str]:
        recorded_at = recorded_at or now_utc()
        cols = ("txn_id", "filing_id", "filer_id", "txn_date", "disclosed_date",
                "ticker", "asset_name", "asset_type", "action", "owner",
                "amount_low", "amount_high", "option_type", "strike", "expiry",
                "directed", "recorded_at")
        rows, ids = [], []
        for t in txns:
            tid = stable_id(filing_id, t["txn_date"], t.get("ticker"),
                            t["action"], t.get("owner", "self"), t.get("amount_low"))
            ids.append(tid)
            rows.append((
                tid, filing_id, filer_id, as_iso(t["txn_date"]), as_iso(t["disclosed_date"]),
                t.get("ticker"), t.get("asset_name"), t.get("asset_type", "other"),
                t["action"], t.get("owner", "self"),
                t.get("amount_low"), t.get("amount_high"),
                t.get("option_type"), t.get("strike"),
                as_iso(t["expiry"]) if t.get("expiry") else None,
                int(t.get("directed", 1)), recorded_at,
            ))
        with self.tx() as db:
            db.executemany(
                f"INSERT OR IGNORE INTO transactions ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})", rows)
        return ids

    def add_prices(self, ticker: str, rows: Iterable[tuple[str, float, float | None]],
                   source: str = "manual") -> int:
        data = [(ticker, as_iso(d), adj, raw, source) for d, adj, raw in rows]
        with self.tx() as db:
            db.executemany(
                "INSERT INTO prices (ticker,date,close_adj,close_raw,source) VALUES (?,?,?,?,?) "
                "ON CONFLICT(ticker,date) DO UPDATE SET close_adj=excluded.close_adj,"
                "close_raw=excluded.close_raw,source=excluded.source", data)
        return len(data)

    def add_benchmark(self, series: str, rows: Iterable[tuple[str, float]]) -> int:
        data = [(series, as_iso(d), v) for d, v in rows]
        with self.tx() as db:
            db.executemany(
                "INSERT INTO benchmarks (series,date,value) VALUES (?,?,?) "
                "ON CONFLICT(series,date) DO UPDATE SET value=excluded.value", data)
        return len(data)

    def set_jurisdiction(self, filer_id: str, body_id: str, sectors: Iterable[str],
                         valid_from: str, role: str = "member",
                         valid_to: str | None = None) -> None:
        with self.tx() as db:
            db.executemany(
                "INSERT OR REPLACE INTO jurisdictions "
                "(filer_id,body_id,sector,role,valid_from,valid_to) VALUES (?,?,?,?,?,?)",
                [(filer_id, body_id, s, role, as_iso(valid_from),
                  as_iso(valid_to) if valid_to else None) for s in sectors])

    def add_security(self, ticker: str, name: str | None = None,
                     sector: str | None = None, market: str = "US") -> None:
        with self.tx() as db:
            db.execute(
                "INSERT INTO securities (ticker,name,sector,market) VALUES (?,?,?,?) "
                "ON CONFLICT(ticker) DO UPDATE SET name=COALESCE(excluded.name,name),"
                "sector=COALESCE(excluded.sector,sector)", (ticker, name, sector, market))

    # -- as-of reads ----------------------------------------------------------

    def transactions(self, *, as_of: str | None = None, filer_id: str | None = None,
                     ticker: str | None = None, owner: str | None = None,
                     directed_only: bool = False) -> list[sqlite3.Row]:
        """Transactions we knew about as of `as_of`.

        `as_of` filters on *record* time, not transaction date: a filing we only
        learned about later is invisible, which is exactly what a backtest needs.
        """
        q = ["SELECT t.* FROM transactions t JOIN filings f ON f.filing_id=t.filing_id WHERE 1=1"]
        p: list[Any] = []
        if as_of:
            q.append("AND t.recorded_at <= ? AND (t.superseded_at IS NULL OR t.superseded_at > ?)")
            p += [as_of, as_of]
            # a filing is only public once filed, regardless of when we recorded it
            q.append("AND t.disclosed_date <= ?")
            p.append(as_iso(as_of[:10]))
        else:
            q.append("AND t.superseded_at IS NULL")
        if filer_id:
            q.append("AND t.filer_id = ?"); p.append(filer_id)
        if ticker:
            q.append("AND t.ticker = ?"); p.append(ticker)
        if owner:
            q.append("AND t.owner = ?"); p.append(owner)
        if directed_only:
            q.append("AND t.directed = 1")
        q.append("ORDER BY t.txn_date, t.txn_id")
        return self.db.execute(" ".join(q), p).fetchall()

    def price_on(self, ticker: str, on: str, *, lookback: int = 7) -> float | None:
        """Adjusted close on `on`, or the most recent close within `lookback`
        days before it (markets close at weekends; filings do not)."""
        row = self.db.execute(
            "SELECT close_adj FROM prices WHERE ticker=? AND date<=? AND date>=date(?, ?) "
            "ORDER BY date DESC LIMIT 1",
            (ticker, as_iso(on), as_iso(on), f"-{lookback} day")).fetchone()
        return row["close_adj"] if row else None

    def benchmark_on(self, series: str, on: str, *, lookback: int = 7) -> float | None:
        row = self.db.execute(
            "SELECT value FROM benchmarks WHERE series=? AND date<=? AND date>=date(?, ?) "
            "ORDER BY date DESC LIMIT 1",
            (series, as_iso(on), as_iso(on), f"-{lookback} day")).fetchone()
        return row["value"] if row else None

    def sectors_for(self, filer_id: str, on: str) -> set[str]:
        """Sector jurisdiction in force on a given date — not today's. A
        committee reassignment must not retroactively reclassify old trades."""
        rows = self.db.execute(
            "SELECT DISTINCT sector FROM jurisdictions WHERE filer_id=? "
            "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
            (filer_id, as_iso(on), as_iso(on))).fetchall()
        return {r["sector"] for r in rows}

    def sector_of(self, ticker: str) -> str | None:
        row = self.db.execute("SELECT sector FROM securities WHERE ticker=?", (ticker,)).fetchone()
        return row["sector"] if row else None

    def filers(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return self.db.execute("SELECT * FROM filers WHERE kind=? ORDER BY name", (kind,)).fetchall()
        return self.db.execute("SELECT * FROM filers ORDER BY name").fetchall()

    def save_scores(self, rows: Iterable[dict]) -> int:
        cols = ("filer_id", "as_of", "model_version", "scope", "metric", "value",
                "n", "ci_low", "ci_high", "p_value", "q_value", "significance")
        data = [tuple(r.get(c) for c in cols) for r in rows]
        with self.tx() as db:
            db.executemany(
                f"INSERT OR REPLACE INTO scores ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})", data)
        return len(data)
