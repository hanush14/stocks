"""Bitemporal store tests.

These are the tests that make the backtest trustworthy. If any of them fail,
every alpha figure the product publishes is contaminated by hindsight.
"""
from conftest import DISC, TRADE, add_filing


def txn(**kw):
    base = dict(txn_date=TRADE, disclosed_date=DISC, ticker="ARDN",
                asset_type="stock", action="purchase", owner="self",
                amount_low=1001.0, amount_high=15000.0)
    base.update(kw)
    return base


class TestAsOf:
    def test_query_without_as_of_returns_current_truth(self, seeded):
        add_filing(seeded, txns=[txn()])
        assert len(seeded.transactions()) == 1

    def test_filing_recorded_later_is_invisible_to_an_earlier_as_of(self, seeded):
        # The core look-ahead guard: a backtest run as of March cannot see a
        # filing we only ingested in June.
        add_filing(seeded, txns=[txn()], recorded_at="2024-06-01T00:00:00+00:00")
        assert seeded.transactions(as_of="2024-03-01T00:00:00+00:00") == []
        assert len(seeded.transactions(as_of="2024-07-01T00:00:00+00:00")) == 1

    def test_undisclosed_trade_is_invisible_even_if_already_recorded(self, seeded):
        # Ingesting early must not make a trade public early.
        add_filing(seeded, txns=[txn()], recorded_at="2024-01-02T00:00:00+00:00")
        assert seeded.transactions(as_of="2024-01-15T00:00:00+00:00") == []
        assert len(seeded.transactions(as_of="2024-02-02T00:00:00+00:00")) == 1


class TestAmendments:
    def test_amendment_supersedes_original_without_destroying_it(self, seeded):
        original = add_filing(seeded, txns=[txn(amount_low=1001.0, amount_high=15000.0)],
                              recorded_at="2024-02-01T00:00:00+00:00")
        add_filing(seeded, txns=[txn(amount_low=250001.0, amount_high=500000.0)],
                   amends=original, recorded_at="2024-05-01T00:00:00+00:00")

        # current truth is the amended figure, and only that
        now = seeded.transactions()
        assert len(now) == 1
        assert now[0]["amount_low"] == 250001.0

        # but the original is still what we believed in March
        before = seeded.transactions(as_of="2024-03-01T00:00:00+00:00")
        assert len(before) == 1
        assert before[0]["amount_low"] == 1001.0

    def test_amendment_history_is_retained_for_audit(self, seeded):
        original = add_filing(seeded, txns=[txn()], recorded_at="2024-02-01T00:00:00+00:00")
        add_filing(seeded, txns=[txn(amount_low=250001.0)], amends=original,
                   recorded_at="2024-05-01T00:00:00+00:00")
        rows = seeded.db.execute("SELECT superseded_at FROM filings ORDER BY recorded_at").fetchall()
        assert rows[0]["superseded_at"] is not None   # original marked, not deleted
        assert rows[1]["superseded_at"] is None


class TestFilters:
    def test_owner_filter_separates_spouse_from_member(self, seeded):
        # A professionally-trading spouse must not be scored as the member.
        add_filing(seeded, txns=[txn(owner="self"), txn(owner="spouse", amount_low=15001.0)])
        assert len(seeded.transactions(owner="self")) == 1
        assert len(seeded.transactions(owner="spouse")) == 1
        assert len(seeded.transactions()) == 2

    def test_directed_only_excludes_blind_trust_holdings(self, seeded):
        add_filing(seeded, txns=[txn(), txn(directed=0, amount_low=15001.0)])
        assert len(seeded.transactions(directed_only=True)) == 1


class TestPricesAndJurisdiction:
    def test_price_lookup_falls_back_to_last_close(self, seeded):
        # Filings land on weekends; markets do not trade then.
        assert seeded.price_on("ARDN", "2024-01-06") == 100.0

    def test_price_lookup_gives_up_beyond_lookback(self, seeded):
        assert seeded.price_on("ARDN", "2023-12-01") is None

    def test_jurisdiction_is_evaluated_at_the_trade_date(self, seeded):
        # A committee reassignment must not retroactively reclassify old trades.
        seeded.set_jurisdiction("F1", "HSBA", ["Financials"],
                                valid_from="2023-01-03", valid_to="2024-01-03")
        seeded.set_jurisdiction("F1", "HSSY", ["Information Technology"],
                                valid_from="2024-01-03")
        assert seeded.sectors_for("F1", "2023-06-01") == {"Financials"}
        assert seeded.sectors_for("F1", "2024-06-01") == {"Information Technology"}


class TestProvenance:
    def test_identical_bytes_produce_one_document(self, seeded):
        a = seeded.add_document(filer_id="F1", source="test", doc_type="ptr",
                                filed_date=DISC, raw=b"same")
        b = seeded.add_document(filer_id="F1", source="test", doc_type="ptr",
                                filed_date=DISC, raw=b"same")
        assert a == b
        n = seeded.db.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
        assert n == 1

    def test_every_document_keeps_its_hash(self, seeded):
        seeded.add_document(filer_id="F1", source="test", doc_type="ptr",
                            filed_date=DISC, raw=b"payload")
        row = seeded.db.execute("SELECT sha256 FROM documents").fetchone()
        assert len(row["sha256"]) == 64
