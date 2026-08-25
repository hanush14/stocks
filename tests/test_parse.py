"""Parser tests. Each case is a real shape that appears in filings."""
import pytest

from ledger.parse import ptr


class TestAmounts:
    @pytest.mark.parametrize("text,expected", [
        ("$1,001 - $15,000",       (1001.0, 15000.0)),
        ("$1,001-$15,000",         (1001.0, 15000.0)),
        ("$15,001 - $50,000",      (15001.0, 50000.0)),
        ("$1,000,001 - $5,000,000", (1000001.0, 5000000.0)),
    ])
    def test_bands(self, text, expected):
        assert ptr.parse_amount(text) == expected

    def test_open_ended_top_band_has_no_upper_bound(self):
        # The top band is unbounded. Returning a number here would invent a
        # ceiling that the filing never disclosed.
        assert ptr.parse_amount("$50,000,001 +") == (50000001.0, None)
        assert ptr.parse_amount("Over $50,000,000") == (50000000.0, None)

    def test_single_figure_snaps_to_its_band(self):
        # A lone figure must widen to the band that contains it, never become
        # a point estimate.
        assert ptr.parse_amount("$20,000") == (15001.0, 50000.0)

    def test_missing_amount_is_none_not_zero(self):
        assert ptr.parse_amount("") == (None, None)
        assert ptr.parse_amount("n/a") == (None, None)


class TestOwner:
    @pytest.mark.parametrize("code,expected", [
        ("SP", "spouse"), ("DC", "dependent_child"), ("JT", "joint"),
        ("", "self"), (None, "self"), ("sp", "spouse"),
    ])
    def test_codes(self, code, expected):
        assert ptr.parse_owner(code) == expected


class TestActions:
    @pytest.mark.parametrize("code,expected", [
        ("P", "purchase"), ("S", "sale"), ("S (partial)", "sale_partial"),
        ("E", "exchange"), ("purchase", "purchase"),
    ])
    def test_codes(self, code, expected):
        assert ptr.parse_action(code) == expected

    def test_unknown_action_raises_rather_than_defaulting(self):
        with pytest.raises(ptr.ParseError):
            ptr.parse_action("Z")


class TestTicker:
    def test_extracts_parenthesised_symbol(self):
        assert ptr.extract_ticker("Apple Inc. (AAPL)") == "AAPL"
        assert ptr.extract_ticker("Berkshire Hathaway Inc. Class B (BRK.B)") == "BRK.B"

    def test_returns_none_rather_than_guessing_from_name(self):
        # Guessing a ticker from a company name silently corrupts every return
        # computed from it.
        assert ptr.extract_ticker("Some Private Holding LLC") is None
        assert ptr.extract_ticker("Municipal bond fund (N/A)") is None


class TestOptions:
    def test_detects_option_with_full_terms(self):
        got = ptr.extract_option("Ardent Semi (ARDN) Call options strike $95 expiry 01/17/2025")
        assert got["option_type"] == "call"
        assert got["strike"] == 95.0
        assert got["expiry"] == "2025-01-17"

    def test_flags_option_even_without_terms(self):
        # Knowing it is an option but not its terms is very different from
        # believing it is stock.
        got = ptr.extract_option("Novista Therapeutics (NVSTA) call")
        assert got == {"option_type": "call"}

    def test_plain_equity_is_not_an_option(self):
        assert ptr.extract_option("Apple Inc. (AAPL) common stock") == {}

    def test_row_marks_asset_type_option(self):
        row = ptr.parse_row(
            {"asset": "Ardent (ARDN) Call options", "transaction_type": "P",
             "transaction_date": "03/12/2024", "amount": "$1,001 - $15,000"},
            disclosed_date="2024-04-19")
        assert row["asset_type"] == "option"
        assert row["option_type"] == "call"


class TestDirectedness:
    def test_blind_trust_is_not_directed(self):
        assert ptr.is_directed("Assets held in a qualified blind trust") is False
        assert ptr.is_directed("Vanguard Target Date 2040 fund") is False

    def test_ordinary_holding_is_directed(self):
        assert ptr.is_directed("Apple Inc. (AAPL)") is True


class TestRows:
    def test_full_row(self):
        row = ptr.parse_row({
            "owner": "SP", "asset": "Brava Financial Group (BRVA) [ST]",
            "transaction_type": "P", "transaction_date": "03/12/2024",
            "amount": "$50,001 - $100,000",
        }, disclosed_date="2024-04-19")
        assert row == {
            "txn_date": "2024-03-12", "disclosed_date": "2024-04-19",
            "ticker": "BRVA", "asset_name": "Brava Financial Group (BRVA) [ST]",
            "asset_type": "stock", "action": "purchase", "owner": "spouse",
            "amount_low": 50001.0, "amount_high": 100000.0, "directed": True,
        }

    def test_bad_rows_are_kept_with_their_error(self):
        # A filing we cannot read is a data-quality fact about that filer.
        # Dropping it silently would flatter their record.
        ok, bad = ptr.parse_rows([
            {"asset": "Apple (AAPL)", "transaction_type": "P",
             "transaction_date": "03/12/2024", "amount": "$1,001 - $15,000"},
            {"asset": "Broken", "transaction_type": "P", "transaction_date": "not a date"},
        ], disclosed_date="2024-04-19")
        assert len(ok) == 1 and len(bad) == 1
        assert "unparseable date" in bad[0]["error"]
