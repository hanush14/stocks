# Ledger & Signal

Tracks disclosed trades by politicians and large investors, and measures the one
thing that determines whether any of it is useful: **how much of the edge is
still there by the time you're allowed to see it.**

Disclosure is slow — 2 days for an SEC Form 4, 30–45 for a congressional PTR, up
to ~135 for a 13F. Ranking by raw return produces a leaderboard of people you
cannot profitably copy. Everything here is built to rank on what survives the lag
instead.

## Status

| Layer | State |
|---|---|
| Congressional roster + committee jurisdiction | **Live** — 537 sitting members, 49 committees |
| Bitemporal store, parsers, scoring engine | **Built and tested** — 84 tests |
| Trade and price ingestion | **Blocked** — see below |

The disclosure endpoints are unreachable from the current sandbox, so no trade
data is loaded. Rather than fill those columns with estimates, every
trade-derived figure reports its absence. Check for yourself:

```bash
python -m ledger status      # what this host can actually fetch
python -m ledger selftest    # proves the pipeline works, offline
```

`selftest` runs the whole chain on a deterministic price path — two filers with
*identical trades* and different disclosure speed:

```
rank filer  n   total  residual  tradable  kept  lag      q  significance
1    FAST   15  17.40     17.17     16.95   99%    2  0.000  significant
2    SLOW   15  17.40      5.20      5.01   30%  120  0.000  significant
```

Same trades, same returns. The 2-day discloser keeps 99% of the alpha; the
120-day discloser keeps 30%. That difference is the product.

## What it computes

**Alpha decay** — every purchase scored three times against a common endpoint:
from the trade date (what the filer earned), from the disclosure date (what was
left when it went public), and from disclosure + 2 days (what survived the market
reading it). Rankings use the third.

**FDR-corrected significance** — with 537 members ranked at once, a raw 5%
threshold calls ~27 of them skilled by chance alone. Benjamini–Hochberg is
applied across the whole cohort, so `significant` means "survives correction
against everyone ranked". `tests/test_analytics.py` demonstrates this: 535 random
p-values give 20+ raw hits and 0 after correction.

**Jurisdiction split** — alpha inside the sectors a member's committees legislate
over, versus everything else, evaluated against the assignments in force *on the
trade date*. A persistent gap across many trades is a far stronger conflict
signal than any single suspicious trade.

**Conviction clusters** — three or more independent high-confidence filers buying
the same security inside a bounded window. The window is the point; without it
"cluster" just means "several people own this stock".

## Things that silently corrupt this data

Each of these is handled explicitly, and has tests:

- **Amounts are ranges, not values.** PTRs disclose `$1,001–$15,000`, not a
  number. The top band is open-ended. Storing a midpoint as the trade size is the
  most common error in this data.
- **The famous trades are options.** Booking a long-dated call as if it were the
  underlying misstates capital at risk and return by an order of magnitude.
- **Spouse trades are not the member's.** Owner codes (`SP`/`DC`/`JT`) are a
  first-class dimension; scoring defaults to `self`.
- **Filings get amended.** An amendment supersedes rather than overwrites, so
  "what did we know in March" stays answerable.
- **Form 4 grants aren't decisions.** Codes `A`/`M`/`F` are compensation
  mechanics, excluded from scoring.
- **Blind trusts have no agency.** Non-directed holdings are excluded from skill.
- **Look-ahead bias** can't be prevented by discipline, so it's prevented by the
  schema — see below.
- **Survivorship bias.** Delisted tickers must stay in the universe; no free price
  API supplies this, so `delistings` is maintained by hand.

## Why the store is bitemporal

Every fact carries when it was true (`txn_date`) *and* when we learned it
(`recorded_at`). Every read is as-of a date, so a backtest run as of March
physically cannot see a filing ingested in June — the SQL can't return it.

```python
store.transactions(as_of="2024-03-01T00:00:00+00:00")   # what we knew then
store.transactions()                                     # current truth
```

This is why `tests/test_store.py` is the most important file here. If those
tests fail, every published figure is contaminated by hindsight.

## Layout

```
ledger/
  schema.py            DDL, amount bands, owner codes
  store.py             bitemporal store, as-of reads
  parse/ptr.py         ranges, owner codes, options, directedness
  sources/
    base.py            polite client: robots.txt, rate limit, 403 is terminal
    house.py           House PTR index + PDF/OCR extraction
    edgar.py           SEC Form 4 and 13F
  analytics/
    performance.py     returns, benchmark alpha, decay profile
    stats.py           t-test, Benjamini-Hochberg, bootstrap CI
    ranking.py         cohort ranking with FDR
    signals.py         jurisdiction split, clusters, price-gap screen
  cli.py
app/                   jurisdiction index (built from scripts/build_members.py)
scripts/               data extraction and app build
tests/                 84 tests
```

## Running it when data is reachable

```bash
python -m ledger ingest-house  --year 2024 --db ledger.db
python -m ledger ingest-edgar  --cik 320193 --form form4 --db ledger.db
python -m ledger rank --as-of 2026-01-01 --metric tradable --db ledger.db
```

Ingestion is deliberately slow and cached: one request at a time, `robots.txt`
honoured, and a `403` stops the run rather than being routed around. Unblocking
requires egress to `disclosures-clerk.house.gov`, `efdsearch.senate.gov`,
`data.sec.gov`, and a price source. No code changes.

## Scope

This is an analytics product, not an advisory one. It reports what filers did and
how it performed; it does not recommend securities. That distinction is
deliberate — a ranked "who to follow" service raises registration questions under
SEBI's Research Analyst regulations in India and the Investment Advisers Act in
the US.

## Tests

```bash
python -m pytest tests/ -q
```
