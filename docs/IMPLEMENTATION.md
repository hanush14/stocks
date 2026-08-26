# Implementation spec — from roster to a working track record

**Audience:** a coding agent with working internet access, running on the user's
own machine. **Goal:** deliver the two screens this product exists for.

1. **Track record** — every politician and whale ranked by how good their calls
   actually were, with the statistics to say whether it is skill or luck.
2. **Open ideas** — the trades those proven filers made that *have not yet played
   out*, so there is still something to act on.

Everything needed for both is already built and tested (`ledger/`, 84 tests)
**except price history**. No prices means no returns, no alpha, no ranking, and
no way to tell whether a move has already happened. Phase A is therefore the
whole unlock; nothing downstream works until it lands.

---

## Ground rules — do not break these

These are not style preferences. Each one exists because breaking it produces
numbers that look right and are wrong.

1. **Never invent a value.** If a price, sector or benchmark is missing, the
   result is `None` and the row says so. No midpoints, no interpolation, no
   "reasonable defaults", no sample data.
2. **Never write a point estimate for a disclosure band.** PTRs disclose
   `$1,001–$15,000`. Both ends are stored; the top band's upper bound is `None`
   and stays that way.
3. **All reads go through `as_of`.** `Store.transactions(as_of=...)` exists so a
   backtest cannot see a filing recorded later. Never add a query path that
   bypasses it.
4. **Amendments supersede, never overwrite.** Already handled by
   `Store.add_filing(amends=...)`. Do not "clean up" superseded rows.
5. **Politeness is the contract.** `PoliteClient` honours `robots.txt`, runs one
   request at a time, and treats HTTP 403 as terminal. Do not add retries around
   a 403, do not rotate user agents, do not parallelise to go faster.
6. **`python -m pytest tests/ -q` must stay green.** 84 tests today. Every phase
   adds its own.

Verify each phase with `python -m ledger selftest` plus the phase's own tests
before moving on.

---

## Phase A — price history *(blocks everything else)*

**New module:** `ledger/market/prices.py`

### Source

Primary: **Stooq** — free, no API key, plain CSV, permissive for this use.

```
https://stooq.com/q/d/l/?s={symbol}.us&i=d
-> Date,Open,High,Low,Close,Volume     (already split-adjusted)
```

Fallback: **Yahoo chart API** — its `adjclose` series applies both split and
dividend adjustment, which is exactly what total return needs.

```
https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=10y&interval=1d&events=div%2Csplit
-> JSON: timestamp[], indicators.quote[0].close[], indicators.adjclose[0].adjclose[]
```

> Verify both response shapes before writing the parser — endpoints drift, and a
> silently-changed field name is how a pipeline starts storing nulls.

### Requirements

- Store `close_adj` (adjusted, used by every return) **and** `close_raw`, so an
  adjustment can be audited later. `Store.add_prices()` already takes both.
- Fetch once per ticker per day; `PoliteClient(cache_dir=...)` handles this.
- A ticker that 404s is recorded as unresolvable, not retried in a loop.
- **India:** NSE/BSE bhavcopy is the free equivalent. Defer until US works.

### CLI

```
python -m ledger ingest-prices --from-db          # every ticker with a trade
python -m ledger ingest-prices --ticker AAPL --years 10
```

### Tests

- A known split (AAPL 4:1 on 2020-08-31) — `close_adj` before the split must be
  ~1/4 of `close_raw`. **This is the test that catches the single most common
  silent corruption in this whole domain**: an unadjusted split reads as an 80%
  loss.
- A missing ticker yields no rows and no exception.
- `Store.price_on()` returns the prior close over a weekend, and `None` beyond
  the lookback window.

---

## Phase B — sectors and benchmarks

Alpha is return *minus a benchmark*. Without this, "alpha" is just return, and
anyone overweight tech in a tech bull market looks like a genius.

### Ticker → sector

Free path, no vendor: SEC's submissions API returns a SIC code per company.

```
https://www.sec.gov/files/company_tickers.json      -> ticker -> CIK
https://data.sec.gov/submissions/CIK{cik:010d}.json -> sic, sicDescription
```

Build `ledger/market/sectors.py` with a SIC → GICS-sector map (SIC has ~400
codes; a coarse mapping to the 11 GICS sectors is enough and is what
`app/members.json` already uses on the committee side). Write into `securities`.

Anything unmappable gets sector `None` and falls back to the market benchmark —
**do not guess a sector**, because sector drives the jurisdiction/conflict
signal and a wrong one manufactures a false finding.

### Benchmark series

Sector ETFs as proxies, fetched exactly like prices, stored as
`SECTOR:{name}` in `benchmarks`:

| Sector | ETF | | Sector | ETF |
|---|---|---|---|---|
| Information Technology | XLK | | Consumer Staples | XLP |
| Financials | XLF | | Utilities | XLU |
| Health Care | XLV | | Materials | XLB |
| Energy | XLE | | Real Estate | XLRE |
| Industrials | XLI | | Communication Services | XLC |
| Consumer Discretionary | XLY | | *(market fallback)* | SPY |

`Performance.benchmark_series()` already resolves `SECTOR:{sector}` and falls
back to `MARKET`; store SPY as `MARKET`.

### Tests

- Every ticker with a transaction resolves to a benchmark (sector or fallback).
- `Performance.abnormal_return` returns `None`, not `0.0`, when the benchmark is
  missing — a missing benchmark must never silently become "zero alpha".

---

## Phase C — finish the sources

### C1. Senate EFD — `ledger/sources/senate.py`

The Senate site requires accepting an agreement before searching:

1. `GET https://efdsearch.senate.gov/search/home/` — collect the CSRF token and
   session cookie.
2. `POST /search/home/` with `prohibition_agreement=1`.
3. `POST /search/report/data/` with the CSRF token, `report_types=[11]`
   (periodic transaction), date range, paging.
4. Results are HTML tables; parse rows into the same dicts
   `ledger.parse.ptr.parse_row` already consumes.

`urllib` needs an explicit `HTTPCookieProcessor` opener for this. Reuse
`PoliteClient`'s rate limiting — do not open a second unthrottled client.

### C2. 13F position diffing — `ledger/analytics/holdings.py`

A 13F is a **snapshot, not a transaction log**. Derive trades by diffing
consecutive quarters per CIK:

- share count up → inferred purchase; down → inferred sale; new → open; gone → close
- Store with a flag distinguishing **inferred** from **disclosed** trades. Add
  an `inferred INTEGER DEFAULT 0` column to `transactions`.
- Ranking must default to disclosed-only. An inferred trade has no real
  transaction date — only "sometime in that quarter" — so scoring it at the
  quarter boundary invents precision the filing never had.
- Document the blind spot in the UI: a position opened *and closed* inside one
  quarter is invisible to 13F entirely.

### C3. Delistings

Populate `delistings` from SEC Form 25 filings. Without it every backtest
inherits an upward survivorship bias, because the losers quietly vanish from the
universe. There is no free API for this; a maintained table is the honest answer.

---

## Phase D — the two screens

### D1. Track record

`rank_cohort()` already produces this. It needs only real data plus a report:

```
python -m ledger rank --as-of 2026-01-01 --metric tradable --top 50
```

Ship as JSON for the UI: name, chamber, party, seat, n, total α, residual α,
tradable α, kept %, median lag, q-value, significance, score 0–100.

**Ranked by `tradable`, never by raw return.** Ranking on total return produces
a leaderboard of people nobody can copy; that is the whole thesis.

### D2. Open ideas — "trades still worth making"

**New:** `ledger/analytics/ideas.py`. This is the screen the user has been asking
for since the beginning, so get the gating right.

For each transaction, include it only if **all** hold:

1. Filer's `significance == "significant"` and `score >= 65` — an unproven filer's
   trade is not an idea.
2. Filer's `tradable` alpha > 0 — they must have edge *after* the lag, not just
   overall.
3. Action is `purchase`, `directed == 1`, owner is `self`.
4. Disclosed within the last `--max-age` days (default 120).
5. Price move since disclosure is below `--max-runup` (default 10%) —
   `signals.price_gap_screen()` already does this.

Output per row: filer, score, historical residual α *at their own median lag*,
ticker, sector, in/out of jurisdiction, disclosed date, days since, price at
disclosure, price now, gap %, and **how many other qualifying filers also hold
it** (conviction, via `signals.scan_clusters`).

```
python -m ledger ideas --max-age 120 --max-runup 10 --min-score 65
```

**Required framing in the output and the UI** — the product's posture is
analytics, not advice:

> These are disclosed purchases by filers with a statistically significant
> post-disclosure record, whose price has not yet moved. It is an observation
> about disclosure and price, not a recommendation. `n` is small and the filer's
> edge is historical.

If the screen returns nothing, say so plainly. An empty result is a real finding
about the current window — never pad it by loosening the gates.

### Tests

- A filer with `significance != "significant"` never appears, regardless of alpha.
- A trade whose price already ran past `max-runup` is excluded.
- Empty input yields an empty list, not an exception.

---

## Phase E — wire the UI

`scripts/build_app.py` currently inlines a static roster. Change it to read
`ledger.db` and emit both new datasets alongside the existing jurisdiction index.

Two new tabs in `app/template.html`:

- **Track record** — sortable leaderboard. Show `n` and significance on every
  row; render "not distinguishable from chance" plainly rather than hiding it.
  The decay sparkline component already exists in git history (`e0ca5d4`) —
  reuse it, now with real values.
- **Open ideas** — D2's output as cards. Each carries the filer's score, the
  gap, days since disclosure, and the conviction count.

Keep the existing "Not ingested" empty states. If a section has no real data it
says so; that behaviour is deliberate and should survive this change.

---

## Order of work

| Phase | Delivers | Blocked by |
|---|---|---|
| A. Prices | any return figure at all | — |
| B. Sectors & benchmarks | alpha instead of raw return | A |
| C1. Senate | 100 more filers | — (parallel) |
| C2. 13F diffing | whales, with caveats | A, B |
| D1. Track record | **screen 1** | A, B |
| D2. Open ideas | **screen 2** | D1 |
| E. UI | both, visible | D |

A → B → D1 → D2 is the critical path. C can run in parallel; E is last.

## Definition of done

- `python -m ledger rank --top 50` prints real named filers with real alpha and
  q-values.
- `python -m ledger ideas` prints real tickers, or states plainly that nothing
  currently qualifies.
- `python -m pytest tests/ -q` green, with new tests per phase.
- No fabricated value anywhere. Every figure traces to a stored document
  (`documents.sha256`) or is absent.
