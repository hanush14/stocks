import pytest

from ledger import Store
from ledger.analytics.performance import Performance, shift

TRADE = "2024-01-01"
DISC = "2024-02-01"
HORIZON = 180
END = shift(TRADE, HORIZON)


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def seeded(store):
    """A store with one security whose price path makes decay unambiguous.

    ARDN runs 100 -> 130 before the filing becomes public, then 130 -> 140 after.
    The sector benchmark is flat, so abnormal return equals raw return:

        total alpha    = 40.0 pp   (what the filer earned)
        residual alpha = 7.69 pp   (what was left at disclosure)
        tradable alpha = 3.70 pp   (what was left two days later)
    """
    store.upsert_filer(filer_id="F1", name="Test Filer", kind="politician",
                       bioguide="F000001")
    store.add_security("ARDN", "Ardent Semiconductor", "Information Technology")
    store.add_prices("ARDN", [
        (TRADE, 100.0, 100.0),
        (DISC, 130.0, 130.0),
        (shift(DISC, 2), 135.0, 135.0),
        (END, 140.0, 140.0),
    ], source="test")
    store.add_benchmark("SECTOR:Information Technology", [
        (TRADE, 100.0), (DISC, 100.0), (shift(DISC, 2), 100.0), (END, 100.0)])
    return store


@pytest.fixture
def perf(seeded):
    return Performance(seeded, horizon=HORIZON, settle=2)


def add_filing(store, *, filer_id="F1", filed=DISC, txns=None, amends=None,
               recorded_at=None, doc=b"raw"):
    """Convenience: document -> filing -> transactions in one call."""
    doc_id = store.add_document(filer_id=filer_id, source="test", doc_type="ptr",
                                filed_date=filed, raw=doc)
    fid = store.add_filing(doc_id=doc_id, filer_id=filer_id, filed_date=filed,
                           amends=amends, recorded_at=recorded_at)
    store.add_transactions(fid, filer_id, txns or [], recorded_at=recorded_at)
    return fid
