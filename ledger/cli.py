"""Command line entry point.

    python -m ledger status                     what the network allows today
    python -m ledger selftest                   run the whole pipeline offline
    python -m ledger ingest-house --year 2024   House PTRs -> db
    python -m ledger ingest-edgar --cik 320193  SEC Form 4 -> db
    python -m ledger rank --as-of 2026-01-01    score and rank what is in the db
"""
from __future__ import annotations

import argparse
import sys
import urllib.parse
from datetime import date

from . import Store
from .analytics import Performance, signals
from .analytics.ranking import persist, rank_cohort, strong_set
from .sources import Blocked, Edgar13F, EdgarForm4, HousePTR, PoliteClient

USER_AGENT = "LedgerSignal/1.0 (research; contact: set-me@example.com)"

# Probed with a real HTTP request, not a TCP connect: behind an egress proxy a
# socket to :443 succeeds against the proxy itself while the CONNECT tunnel is
# refused, so a connect-only check reports every blocked host as reachable.
PROBES = {
    "House PTR filings": "https://disclosures-clerk.house.gov/robots.txt",
    "Senate EFD filings": "https://efdsearch.senate.gov/robots.txt",
    "SEC EDGAR": "https://data.sec.gov/submissions/CIK0000320193.json",
    "SEC archives": "https://www.sec.gov/robots.txt",
    "Prices (Yahoo)": "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=1d&interval=1d",
    "NSE India": "https://www.nseindia.com/robots.txt",
}


def _client(cache: str | None) -> PoliteClient:
    return PoliteClient(user_agent=USER_AGENT, min_interval=1.5, cache_dir=cache)


# -- commands -----------------------------------------------------------------

def cmd_status(args) -> int:
    """Report which sources this host can actually fetch from."""
    import urllib.error
    import urllib.request

    print(f"{'source':<22} {'host':<32} status")
    print("-" * 72)
    blocked = 0
    for label, url in PROBES.items():
        host = urllib.parse.urlsplit(url).netloc
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                state = f"ok ({r.status})"
        except urllib.error.HTTPError as e:
            # 403 from the egress proxy and 403 from the site itself both mean
            # we cannot ingest; either way it is not a transient failure.
            state = f"BLOCKED ({e.code})"
            blocked += 1
        except urllib.error.URLError as e:
            state = f"BLOCKED ({getattr(e, 'reason', e)})"
            blocked += 1
        except Exception as e:                      # noqa: BLE001 - report, never crash
            state = f"BLOCKED ({type(e).__name__})"
            blocked += 1
        print(f"{label:<22} {host:<32} {str(state)[:34]}")
    print()
    if blocked:
        print(f"{blocked} of {len(PROBES)} sources unreachable. Ingestion cannot run "
              "until the network policy allows them; the pipeline itself is ready "
              "(verify with: python -m ledger selftest).")
    else:
        print("All sources reachable - ingestion can run.")
    return 0


def cmd_selftest(args) -> int:
    """Exercise every stage offline: store, decay, FDR ranking, clustering.

    This verifies the pipeline, not the world. The figures below are generated
    from a deterministic price path defined in this function - they are not a
    claim about any real person.
    """
    from datetime import timedelta

    store = Store(":memory:")
    d0 = date(2024, 1, 1)
    day = lambda i: (d0 + timedelta(days=i)).isoformat()

    store.add_security("TESTCO", "Test Instrument", "Information Technology")
    store.add_prices("TESTCO", [(day(i), 100.0 + i * 0.10, None) for i in range(420)],
                     source="selftest")
    store.add_benchmark("SECTOR:Information Technology",
                        [(day(i), 100.0) for i in range(420)])

    # Two filers, identical trades, different disclosure speed.
    for fid, lag in (("FAST", 2), ("SLOW", 120)):
        store.upsert_filer(filer_id=fid, name=fid, kind="politician")
        store.set_jurisdiction(fid, "HSSY", ["Information Technology"], valid_from=day(0))
        doc = store.add_document(filer_id=fid, source="selftest", doc_type="ptr",
                                 filed_date=day(0), raw=f"{fid}".encode())
        filing = store.add_filing(doc_id=doc, filer_id=fid, filed_date=day(0),
                                  recorded_at="2024-01-01T00:00:00+00:00")
        store.add_transactions(filing, fid, [
            dict(txn_date=day(k * 5), disclosed_date=day(k * 5 + lag), ticker="TESTCO",
                 asset_type="stock", action="purchase", owner="self")
            for k in range(15)
        ], recorded_at="2024-01-01T00:00:00+00:00")

    perf = Performance(store, horizon=180, settle=2)
    as_of = "2026-01-01T00:00:00+00:00"
    ranked = rank_cohort(store, perf, as_of=as_of, min_sample=10)

    print("pipeline self-test - synthetic price path, not real data\n")
    print(f"{'rank':<5}{'filer':<8}{'n':>4}{'total':>9}{'residual':>10}"
          f"{'tradable':>10}{'kept':>7}{'lag':>6}{'q':>8}  significance")
    print("-" * 78)
    for r in ranked:
        kept = f"{r['kept']*100:.0f}%" if r["kept"] is not None else "-"
        q = f"{r['q_value']:.3f}" if r["q_value"] is not None else "-"
        print(f"{str(r['rank'] or '-'):<5}{r['filer_id']:<8}{r['n']:>4}"
              f"{r['total']:>9.2f}{r['residual']:>10.2f}{r['tradable']:>10.2f}"
              f"{kept:>7}{r['median_lag']:>6}{q:>8}  {r['significance']}")

    split = signals.jurisdiction_split(store, perf, "FAST")
    print(f"\njurisdiction  in={split['in_jurisdiction']:.2f}pp (n={split['n_in']})  "
          f"out={split['out_jurisdiction']} (n={split['n_out']})")

    strong = strong_set(ranked)
    print(f"strong set    {sorted(strong) or 'none cleared the bar'}")
    print(f"clusters      {len(signals.scan_clusters(store, strong=strong, as_of=as_of))}")
    persist(store, ranked, as_of=as_of[:10])
    print(f"scores stored {store.db.execute('SELECT COUNT(*) c FROM scores').fetchone()['c']} rows")

    faster = next(r for r in ranked if r["filer_id"] == "FAST")
    slower = next(r for r in ranked if r["filer_id"] == "SLOW")
    ok = faster["tradable"] > slower["tradable"]
    print(f"\nfast discloser retains more tradable alpha: {ok}")
    return 0 if ok else 1


def cmd_probe(args) -> int:
    """Fetch live filings and print them, writing nothing.

    This exists to answer one question: is the pipeline pulling real records?
    It hits the House Clerk's real index, prints real filer names and document
    ids you can look up yourself, then downloads one real filing and shows the
    transactions parsed out of it.
    """
    src = HousePTR(_client(args.cache))
    print(f"fetching {args.year} House financial disclosure index ...")
    try:
        refs = src.discover(year=args.year)
    except Blocked as e:
        print(f"\nREFUSED: {e}\n"
              "This host cannot reach the source. Run this on a machine with "
              "normal internet access.", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"\nFETCH FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"\n{len(refs)} periodic transaction reports filed in {args.year}\n")
    print(f"{'filer':<30}{'seat':<8}{'filed':<12}{'doc id':<12}")
    print("-" * 62)
    for r in refs[: args.show]:
        name = f"{r['first']} {r['last']}".strip()
        print(f"{name[:29]:<30}{r['state_district']:<8}{r['filed_date']:<12}{r['doc_id']:<12}")

    # Pull one real filing and show what comes out of it.
    print(f"\ndownloading one filing to verify parsing ...")
    for ref in refs[: args.show]:
        try:
            raw = src.fetch(ref)
        except Blocked as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return 2
        except Exception as e:
            print(f"  {ref['doc_id']}: {type(e).__name__}", file=sys.stderr)
            continue
        text = src.extract_text(raw)
        txns = src.parse_text(text, disclosed_date=ref["filed_date"])
        conf = src.extraction_confidence(text, txns)
        print(f"\n  {ref['url']}")
        print(f"  {len(raw):,} bytes, {len(text):,} chars extracted, "
              f"confidence {conf:.2f}")
        if not text.strip():
            print("  no text layer - this is a scanned filing and needs OCR "
                  "(pip install pytesseract pdf2image)")
            continue
        if not txns:
            print("  text extracted but no transactions matched - flagged for review")
            continue
        print(f"\n  {'date':<12}{'ticker':<9}{'action':<10}{'owner':<9}{'amount'}")
        print("  " + "-" * 58)
        for t in txns[: args.rows]:
            amt = (f"${t['amount_low']:,.0f}-" +
                   (f"${t['amount_high']:,.0f}" if t["amount_high"] else "+")
                   ) if t["amount_low"] else "-"
            print(f"  {t['txn_date']:<12}{(t['ticker'] or '-'):<9}"
                  f"{t['action']:<10}{t['owner']:<9}{amt}")
        print(f"\n  {len(txns)} transactions parsed from this filing.")
        return 0
    print("\nno filing could be downloaded", file=sys.stderr)
    return 1


def cmd_ingest_house(args) -> int:
    store = Store(args.db)
    src = HousePTR(_client(args.cache))
    try:
        refs = src.discover(year=args.year)
    except Blocked as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(f"{len(refs)} PTR filings in {args.year}")

    kept = skipped = 0
    for ref in refs[: args.limit] if args.limit else refs:
        filer_id = store.upsert_filer(
            name=f"{ref['first']} {ref['last']}".strip(), kind="politician",
            state=(ref["state_district"] or "")[:2] or None,
            district=(ref["state_district"] or "")[2:] or None)
        try:
            raw = src.fetch(ref)
        except Blocked as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
        except Exception as e:
            print(f"  skip {ref['doc_id']}: {e}", file=sys.stderr)
            skipped += 1
            continue
        txns = src.parse(raw, ref)
        conf = src.extraction_confidence(src.extract_text(raw), txns)
        doc_id = store.add_document(filer_id=filer_id, source=src.name,
                                    doc_type=src.doc_type, filed_date=ref["filed_date"],
                                    raw=raw, url=ref["url"])
        filing = store.add_filing(doc_id=doc_id, filer_id=filer_id,
                                  filed_date=ref["filed_date"], extract_conf=conf)
        store.add_transactions(filing, filer_id, txns)
        kept += len(txns)
        if conf < args.min_confidence:
            print(f"  review {ref['doc_id']} (confidence {conf:.2f})")
    print(f"{kept} transactions ingested, {skipped} documents unreadable")
    return 0


def cmd_ingest_edgar(args) -> int:
    store = Store(args.db)
    client = _client(args.cache)
    src = Edgar13F(client) if args.form == "13f" else EdgarForm4(client)
    try:
        refs = src.discover(cik=args.cik, limit=args.limit or 200)
    except Blocked as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    filer_id = store.upsert_filer(name=args.name or f"CIK{args.cik}",
                                  kind="fund" if args.form == "13f" else "individual",
                                  cik=str(args.cik))
    total = 0
    for ref in refs:
        raw = src.fetch(ref)
        doc_id = store.add_document(filer_id=filer_id, source=src.name,
                                    doc_type=src.doc_type,
                                    filed_date=ref["filed_date"], raw=raw,
                                    url=ref.get("url"))
        filing = store.add_filing(doc_id=doc_id, filer_id=filer_id,
                                  filed_date=ref["filed_date"])
        rows = src.parse(raw, ref)
        if args.form != "13f":
            store.add_transactions(filing, filer_id, rows)
        total += len(rows)
    print(f"{total} rows from {len(refs)} filings")
    return 0


def cmd_rank(args) -> int:
    store = Store(args.db)
    perf = Performance(store, horizon=args.horizon, settle=args.settle)
    as_of = args.as_of or date.today().isoformat()
    ranked = rank_cohort(store, perf, as_of=f"{as_of}T23:59:59+00:00",
                         metric=args.metric, min_sample=args.min_sample)
    if not ranked:
        print("no filers in the database - run an ingest command first")
        return 1
    print(f"{'rank':<5}{'filer':<28}{'n':>5}{'tradable':>10}{'q':>8}  significance")
    print("-" * 68)
    for r in ranked[: args.top]:
        name = store.db.execute("SELECT name FROM filers WHERE filer_id=?",
                                (r["filer_id"],)).fetchone()["name"]
        val = f"{r['tradable']:.2f}" if r["tradable"] is not None else "-"
        q = f"{r['q_value']:.3f}" if r["q_value"] is not None else "-"
        print(f"{str(r['rank'] or '-'):<5}{name[:27]:<28}{r['n']:>5}{val:>10}{q:>8}  "
              f"{r['significance']}")
    persist(store, ranked, as_of=as_of)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ledger", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="which sources this host can reach").set_defaults(fn=cmd_status)
    sub.add_parser("selftest", help="run the pipeline offline").set_defaults(fn=cmd_selftest)

    common = dict(db="ledger.db", cache=".cache")

    pr = sub.add_parser("probe", help="fetch live filings and print them (writes nothing)")
    pr.add_argument("--year", type=int, default=date.today().year - 1)
    pr.add_argument("--cache", default=common["cache"])
    pr.add_argument("--show", type=int, default=10, help="index rows to list")
    pr.add_argument("--rows", type=int, default=15, help="transactions to print")
    pr.set_defaults(fn=cmd_probe)

    h = sub.add_parser("ingest-house", help="ingest House PTR filings")
    h.add_argument("--year", type=int, required=True)
    h.add_argument("--db", default=common["db"])
    h.add_argument("--cache", default=common["cache"])
    h.add_argument("--limit", type=int, default=0)
    h.add_argument("--min-confidence", type=float, default=0.6,
                   help="below this a filing is flagged for human review")
    h.set_defaults(fn=cmd_ingest_house)

    e = sub.add_parser("ingest-edgar", help="ingest SEC filings")
    e.add_argument("--cik", required=True)
    e.add_argument("--form", choices=["form4", "13f"], default="form4")
    e.add_argument("--name")
    e.add_argument("--db", default=common["db"])
    e.add_argument("--cache", default=common["cache"])
    e.add_argument("--limit", type=int, default=0)
    e.set_defaults(fn=cmd_ingest_edgar)

    r = sub.add_parser("rank", help="score and rank filers")
    r.add_argument("--db", default=common["db"])
    r.add_argument("--as-of")
    r.add_argument("--metric", default="tradable",
                   choices=["tradable", "residual", "total"])
    r.add_argument("--horizon", type=int, default=180)
    r.add_argument("--settle", type=int, default=2)
    r.add_argument("--min-sample", type=int, default=12)
    r.add_argument("--top", type=int, default=40)
    r.set_defaults(fn=cmd_rank)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
