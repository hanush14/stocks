"""Analytics tests: decay arithmetic, multiple-comparisons control, clustering."""
import random

import pytest
from conftest import DISC, END, TRADE, add_filing

from ledger.analytics import signals, stats
from ledger.analytics.performance import Performance, shift
from ledger.analytics.ranking import rank_cohort, strong_set


def txn(**kw):
    base = dict(txn_date=TRADE, disclosed_date=DISC, ticker="ARDN",
                asset_type="stock", action="purchase", owner="self")
    base.update(kw)
    return base


class TestDecay:
    """The fixture runs 100 -> 130 before disclosure, 130 -> 140 after."""

    def test_total_alpha_is_the_whole_move(self, seeded, perf):
        add_filing(seeded, txns=[txn()])
        d = perf.decay_for(seeded.transactions()[0])
        assert d["total"] == pytest.approx(40.0, abs=0.01)

    def test_residual_alpha_is_only_what_survived_disclosure(self, seeded, perf):
        add_filing(seeded, txns=[txn()])
        d = perf.decay_for(seeded.transactions()[0])
        assert d["residual"] == pytest.approx(7.692, abs=0.01)

    def test_tradable_alpha_is_smaller_still(self, seeded, perf):
        # Two days for the market to read the filing costs most of the remainder.
        add_filing(seeded, txns=[txn()])
        d = perf.decay_for(seeded.transactions()[0])
        assert d["tradable"] == pytest.approx(3.703, abs=0.01)
        assert d["tradable"] < d["residual"] < d["total"]

    def test_kept_ratio_exposes_how_little_is_followable(self, seeded, perf):
        add_filing(seeded, txns=[txn()])
        prof = perf.entity_profile("F1")
        assert prof["kept"] == pytest.approx(0.192, abs=0.01)

    def test_sales_are_not_scored_as_forward_bets(self, seeded, perf):
        # A disposal has no forward return to attribute; scoring one would
        # double-count the position.
        add_filing(seeded, txns=[txn(action="sale")])
        assert perf.decay_for(seeded.transactions()[0]) is None

    def test_benchmark_is_subtracted_not_ignored(self, seeded, perf):
        # Re-point the sector benchmark so it captures the entire move: alpha
        # must collapse to zero even though the raw return is +40%.
        seeded.add_benchmark("SECTOR:Information Technology",
                             [(TRADE, 100.0), (END, 140.0)])
        add_filing(seeded, txns=[txn()])
        d = perf.decay_for(seeded.transactions()[0])
        assert d["total"] == pytest.approx(0.0, abs=0.01)

    def test_missing_price_yields_none_not_zero(self, seeded, perf):
        seeded.add_security("MISSING", "No Prices", "Information Technology")
        add_filing(seeded, txns=[txn(ticker="MISSING")])
        rows = [t for t in seeded.transactions() if t["ticker"] == "MISSING"]
        assert perf.decay_for(rows[0]) is None


class TestFDR:
    def test_known_benjamini_hochberg_example(self):
        p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.060, 0.074, 0.205]
        q = stats.fdr_bh(p)
        assert q == pytest.approx(
            [0.008, 0.032, 0.0672, 0.0672, 0.0672, 0.080, 0.0846, 0.205], abs=1e-4)

    def test_q_values_are_monotone_and_never_below_p(self):
        rng = random.Random(7)
        p = sorted(rng.random() for _ in range(50))
        q = stats.fdr_bh(p)
        assert all(qi >= pi - 1e-12 for qi, pi in zip(q, p))
        assert q == sorted(q)

    def test_order_is_preserved(self):
        p = [0.9, 0.001, 0.5]
        q = stats.fdr_bh(p)
        assert q[1] < q[2] < q[0]

    def test_correction_kills_the_false_positives_a_raw_threshold_lets_through(self):
        # The whole reason this module exists. 535 filers with no real skill:
        # a raw 0.05 cut calls ~27 of them significant. BH calls ~none.
        rng = random.Random(11)
        p = [rng.random() for _ in range(535)]
        raw = sum(1 for x in p if x < 0.05)
        corrected = sum(1 for x in stats.fdr_bh(p) if x < 0.05)
        assert raw > 15
        assert corrected == 0


class TestClassification:
    def test_below_sample_floor_is_insufficient_not_weak(self):
        # Too few observations is the absence of a finding, not a poor one.
        assert stats.classify(n=4, q=0.001, mean_alpha=25.0) == "insufficient"

    def test_high_q_is_chance_however_large_the_effect(self):
        assert stats.classify(n=50, q=0.4, mean_alpha=25.0) == "chance"

    def test_significant_negative_alpha_is_labelled_separately(self):
        assert stats.classify(n=50, q=0.01, mean_alpha=-8.0) == "negative"

    def test_no_score_below_the_floor(self):
        assert stats.score_0_100(10.0, 0.5, n=4) is None
        assert stats.score_0_100(10.0, 0.5, n=40) is not None


class TestRanking:
    @pytest.fixture
    def cohort(self, store):
        """Three filers: one with real edge, one with none, one with too few trades."""
        store.add_security("AAA", "Alpha Co", "Information Technology")
        store.add_benchmark("SECTOR:Information Technology",
                            [(shift("2024-01-01", i), 100.0) for i in range(0, 400)])
        # price grinds steadily upward after every disclosure date
        store.add_prices("AAA", [(shift("2024-01-01", i), 100.0 + i * 0.10, None)
                                 for i in range(0, 400)], source="test")
        for fid, n in (("EDGE", 15), ("NOISE", 15), ("THIN", 3)):
            store.upsert_filer(filer_id=fid, name=fid, kind="politician")
            txns = []
            for k in range(n):
                t0 = shift("2024-01-01", k * 5)
                # EDGE discloses fast, so most of the drift is still ahead of it
                lag = 2 if fid == "EDGE" else 120
                txns.append(dict(txn_date=t0, disclosed_date=shift(t0, lag),
                                 ticker="AAA", asset_type="stock",
                                 action="purchase", owner="self"))
            # record time must precede the as_of used below, or the store
            # correctly hides these filings from the backtest
            add_filing(store, filer_id=fid, filed="2024-01-01", txns=txns,
                       recorded_at="2024-08-01T00:00:00+00:00")
        return store

    def test_fast_discloser_outranks_slow_one(self, cohort):
        perf = Performance(cohort, horizon=180, settle=2)
        ranked = rank_cohort(cohort, perf, as_of="2026-01-01T00:00:00+00:00",
                             min_sample=10)
        by_id = {r["filer_id"]: r for r in ranked}
        assert by_id["EDGE"]["value"] > by_id["NOISE"]["value"]
        assert by_id["EDGE"]["rank"] < by_id["NOISE"]["rank"]

    def test_thin_record_gets_no_score_and_no_rank(self, cohort):
        perf = Performance(cohort, horizon=180, settle=2)
        ranked = rank_cohort(cohort, perf, as_of="2026-01-01T00:00:00+00:00",
                             min_sample=10)
        thin = next(r for r in ranked if r["filer_id"] == "THIN")
        assert thin["significance"] == "insufficient"
        assert thin["score"] is None
        assert thin["p_value"] is None

    def test_every_tested_row_carries_a_q_value(self, cohort):
        perf = Performance(cohort, horizon=180, settle=2)
        ranked = rank_cohort(cohort, perf, as_of="2026-01-01T00:00:00+00:00",
                             min_sample=10)
        for r in ranked:
            assert (r["q_value"] is None) == (r["p_value"] is None)

    def test_strong_set_excludes_unproven_filers(self, cohort):
        perf = Performance(cohort, horizon=180, settle=2)
        ranked = rank_cohort(cohort, perf, as_of="2026-01-01T00:00:00+00:00",
                             min_sample=10)
        assert "THIN" not in strong_set(ranked)


class TestClusters:
    @pytest.fixture
    def multi(self, seeded):
        for fid in ("A", "B", "C", "D"):
            seeded.upsert_filer(filer_id=fid, name=fid, kind="politician")
        # A, B, C converge inside 100 days; D arrives a year later
        for fid, disc in (("A", "2024-02-01"), ("B", "2024-03-15"),
                          ("C", "2024-05-10"), ("D", "2025-06-01")):
            add_filing(seeded, filer_id=fid, filed=disc,
                       txns=[txn(disclosed_date=disc)])
        return seeded

    def test_convergence_inside_the_window_is_a_cluster(self, multi):
        c = signals.conviction_clusters(multi, "ARDN", strong={"A", "B", "C", "D"})
        assert c["n"] == 3
        assert c["span_days"] <= 180
        assert set(c["members"]) == {"A", "B", "C"}

    def test_late_arrival_is_excluded_not_counted(self, multi):
        # Without a bounded window "cluster" just means "several people own it".
        c = signals.conviction_clusters(multi, "ARDN", strong={"A", "B", "C", "D"})
        assert "D" not in c["members"]

    def test_weak_filers_do_not_form_a_cluster(self, multi):
        assert signals.conviction_clusters(multi, "ARDN", strong={"A"}) is None

    def test_threshold_is_respected(self, multi):
        assert signals.conviction_clusters(
            multi, "ARDN", strong={"A", "B", "C"}, min_members=4) is None


class TestJurisdiction:
    def test_trade_inside_committee_sector_is_flagged(self, seeded):
        seeded.set_jurisdiction("F1", "HSSY", ["Information Technology"],
                                valid_from="2023-01-01")
        add_filing(seeded, txns=[txn()])
        assert signals.in_jurisdiction(seeded, seeded.transactions()[0]) is True

    def test_trade_outside_committee_sector_is_not(self, seeded):
        seeded.set_jurisdiction("F1", "HSBA", ["Financials"], valid_from="2023-01-01")
        add_filing(seeded, txns=[txn()])
        assert signals.in_jurisdiction(seeded, seeded.transactions()[0]) is False

    def test_split_reports_both_sides_with_their_own_n(self, seeded, perf):
        seeded.set_jurisdiction("F1", "HSSY", ["Information Technology"],
                                valid_from="2023-01-01")
        add_filing(seeded, txns=[txn()])
        s = signals.jurisdiction_split(seeded, perf, "F1")
        assert s["n_in"] == 1 and s["n_out"] == 0
        assert s["in_jurisdiction"] == pytest.approx(7.692, abs=0.01)
        assert s["gap"] is None      # nothing to compare against yet
