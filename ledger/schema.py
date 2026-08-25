"""Storage schema and domain constants.

The store is **bitemporal**: every fact carries both when it was true in the
world (`txn_date` / `valid_*`) and when we learned it (`recorded_at` /
`superseded_at`). Every analytical query is therefore answerable "as of" a past
date, which is what makes look-ahead bias structurally impossible rather than a
thing we have to remember not to do. Retrofitting this later is a rewrite, so it
is in the first schema.
"""
from __future__ import annotations

# --- Disclosure amount bands -------------------------------------------------
# Periodic Transaction Reports disclose a RANGE, never an amount. Storing a
# midpoint as if it were the value is the single most common way this data gets
# misreported, so the store keeps both ends and the analytics layer propagates
# the interval. Bands are from the Ethics in Government Act schedule.
AMOUNT_BANDS: list[tuple[float, float | None]] = [
    (0.0, 1_000.0),
    (1_001.0, 15_000.0),
    (15_001.0, 50_000.0),
    (50_001.0, 100_000.0),
    (100_001.0, 250_000.0),
    (250_001.0, 500_000.0),
    (500_001.0, 1_000_000.0),
    (1_000_001.0, 5_000_000.0),
    (5_000_001.0, 25_000_000.0),
    (25_000_001.0, 50_000_000.0),
    (50_000_001.0, None),          # open-ended top band
]

# Ownership codes as they appear on House/Senate filings. A spouse who trades
# professionally will dominate a member's apparent skill, so owner is a
# first-class dimension and never silently collapsed into the member.
OWNERS = {
    "":   "self",
    "SP": "spouse",
    "DC": "dependent_child",
    "JT": "joint",
}

ASSET_TYPES = {
    "ST": "stock",
    "OP": "option",
    "MF": "mutual_fund",
    "ETF": "etf",
    "CS": "corporate_bond",
    "GS": "government_security",
    "OT": "other",
    "PS": "stock",           # preferred
    "CT": "cryptocurrency",
}

ACTIONS = {"purchase", "sale", "sale_partial", "sale_full", "exchange"}

# Trades the scoring engine must never treat as a personal skill signal.
NON_DIRECTED = {"blind_trust", "managed_account", "index_fund"}

SCHEMA_VERSION = 1

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Who we track: members of Congress, funds, individual large holders.
CREATE TABLE IF NOT EXISTS filers (
    filer_id     TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,           -- politician | fund | individual
    name         TEXT NOT NULL,
    market       TEXT NOT NULL DEFAULT 'US',
    chamber      TEXT,                    -- rep | sen
    party        TEXT,
    state        TEXT,
    district     TEXT,
    bioguide     TEXT UNIQUE,
    cik          TEXT,
    meta         TEXT                     -- JSON
);
CREATE INDEX IF NOT EXISTS ix_filers_bioguide ON filers(bioguide);

-- Immutable record of every source document ever retrieved. Analytics never
-- reads this table; it exists so every number can be traced to a document.
CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,
    filer_id     TEXT REFERENCES filers(filer_id),
    source       TEXT NOT NULL,           -- house_ptr | senate_efd | sec_form4 | ...
    doc_type     TEXT NOT NULL,
    filed_date   TEXT NOT NULL,
    url          TEXT,
    sha256       TEXT NOT NULL,
    raw_path     TEXT,
    retrieved_at TEXT NOT NULL
);

-- A filing is one interpretation of one document. An amendment supersedes the
-- filing it amends rather than overwriting it, so history stays queryable.
CREATE TABLE IF NOT EXISTS filings (
    filing_id     TEXT PRIMARY KEY,
    doc_id        TEXT NOT NULL REFERENCES documents(doc_id),
    filer_id      TEXT NOT NULL REFERENCES filers(filer_id),
    filed_date    TEXT NOT NULL,
    amends        TEXT REFERENCES filings(filing_id),
    extract_conf  REAL NOT NULL DEFAULT 1.0,   -- <1 routes to human review
    recorded_at   TEXT NOT NULL,
    superseded_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_filings_filer ON filings(filer_id, filed_date);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id         TEXT PRIMARY KEY,
    filing_id      TEXT NOT NULL REFERENCES filings(filing_id),
    filer_id       TEXT NOT NULL REFERENCES filers(filer_id),
    txn_date       TEXT NOT NULL,          -- valid time: when it happened
    disclosed_date TEXT NOT NULL,          -- when it became public
    ticker         TEXT,
    asset_name     TEXT,
    asset_type     TEXT NOT NULL,
    action         TEXT NOT NULL,
    owner          TEXT NOT NULL,          -- self | spouse | dependent_child | joint
    amount_low     REAL,
    amount_high    REAL,                   -- NULL = open-ended top band
    option_type    TEXT,                   -- call | put
    strike         REAL,
    expiry         TEXT,
    directed       INTEGER NOT NULL DEFAULT 1,   -- 0 = blind trust / managed
    recorded_at    TEXT NOT NULL,
    superseded_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_txn_filer ON transactions(filer_id, txn_date);
CREATE INDEX IF NOT EXISTS ix_txn_ticker ON transactions(ticker, txn_date);

-- Split/dividend-adjusted daily closes. close_adj is what every return uses;
-- close_raw is kept so an adjustment can be audited or recomputed.
CREATE TABLE IF NOT EXISTS prices (
    ticker    TEXT NOT NULL,
    date      TEXT NOT NULL,
    close_adj REAL NOT NULL,
    close_raw REAL,
    source    TEXT NOT NULL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    ticker  TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    kind    TEXT NOT NULL,              -- split | dividend | spinoff | merger | delist
    ratio   REAL,
    cash    REAL,
    PRIMARY KEY (ticker, ex_date, kind)
);

-- Delisted tickers must stay in the universe or every backtest inherits an
-- upward survivorship bias. No free price API supplies this; it is maintained.
CREATE TABLE IF NOT EXISTS delistings (
    ticker      TEXT PRIMARY KEY,
    delist_date TEXT NOT NULL,
    reason      TEXT,
    successor   TEXT
);

CREATE TABLE IF NOT EXISTS benchmarks (
    series TEXT NOT NULL,               -- sector or factor series id
    date   TEXT NOT NULL,
    value  REAL NOT NULL,
    PRIMARY KEY (series, date)
);

CREATE TABLE IF NOT EXISTS securities (
    ticker TEXT PRIMARY KEY,
    name   TEXT,
    sector TEXT,
    market TEXT NOT NULL DEFAULT 'US'
);

-- Committee/ministry -> sector jurisdiction, versioned by date so a
-- reassignment never rewrites how past trades were classified.
CREATE TABLE IF NOT EXISTS jurisdictions (
    filer_id   TEXT NOT NULL REFERENCES filers(filer_id),
    body_id    TEXT NOT NULL,           -- committee id
    sector     TEXT NOT NULL,
    role       TEXT,                    -- member | chair | ranking
    valid_from TEXT NOT NULL,
    valid_to   TEXT,
    PRIMARY KEY (filer_id, body_id, sector, valid_from)
);

-- Scores are versioned so a published rank is reproducible and explainable.
CREATE TABLE IF NOT EXISTS scores (
    filer_id      TEXT NOT NULL REFERENCES filers(filer_id),
    as_of         TEXT NOT NULL,
    model_version TEXT NOT NULL,
    scope         TEXT NOT NULL DEFAULT 'all',   -- 'all' | sector name
    metric        TEXT NOT NULL,
    value         REAL,
    n             INTEGER NOT NULL,
    ci_low        REAL,
    ci_high       REAL,
    p_value       REAL,
    q_value       REAL,                          -- FDR-adjusted
    significance  TEXT,                          -- significant | chance | insufficient
    PRIMARY KEY (filer_id, as_of, model_version, scope, metric)
);
"""
