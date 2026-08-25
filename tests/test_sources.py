"""Source parser tests, against the real shapes these documents take."""
import pytest

from ledger.sources.edgar import EdgarForm4, Edgar13F
from ledger.sources.house import HousePTR

PTR_TEXT = """
Asset                                    Owner  Type  Date        Notified    Amount
Brava Financial Group (BRVA) [ST]        SP     P     03/12/2024  04/19/2024  $50,001 - $100,000
Ardent Semiconductor (ARDN) [ST]                P     06/04/2024  07/11/2024  $1,001 - $15,000
Novista Therapeutics (NVSTA) Call options       P     08/13/2024  09/09/2024  $250,001 - $500,000
Petrolux Energy (PTRX) [ST]              JT     S     09/17/2024  10/24/2024  $15,001 - $50,000
"""

FORM4_XML = b"""<?xml version="1.0"?>
<ownershipDocument>
  <issuerName>Ardent Semiconductor</issuerName>
  <issuerTradingSymbol>ARDN</issuerTradingSymbol>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-03-18</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>66.90</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-03-20</value></transactionDate>
      <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <derivativeTable>
    <derivativeTransaction>
      <securityTitle><value>Call Option (right to buy)</value></securityTitle>
      <conversionOrExercisePrice><value>75.00</value></conversionOrExercisePrice>
      <transactionDate><value>2024-04-02</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>2000</value></transactionShares>
        <transactionPricePerShare><value>4.25</value></transactionPricePerShare>
      </transactionAmounts>
      <expirationDate><value>2026-01-16</value></expirationDate>
    </derivativeTransaction>
  </derivativeTable>
</ownershipDocument>"""

INFOTABLE_XML = b"""<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>ARDENT SEMICONDUCTOR</nameOfIssuer>
    <cusip>03783100</cusip>
    <value>142500</value>
    <shrsOrPrnAmt><sshPrnamt>1200000</sshPrnamt></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>PETROLUX ENERGY</nameOfIssuer>
    <cusip>03783200</cusip>
    <value>40152</value>
    <shrsOrPrnAmt><sshPrnamt>840000</sshPrnamt></shrsOrPrnAmt>
    <putCall>Put</putCall>
  </infoTable>
</informationTable>"""


class TestHousePTR:
    @pytest.fixture
    def parsed(self):
        return HousePTR.parse_text(PTR_TEXT, disclosed_date="2024-04-19")

    def test_reads_every_transaction_line(self, parsed):
        assert len(parsed) == 4

    def test_carries_owner_codes_through(self, parsed):
        assert [p["owner"] for p in parsed] == ["spouse", "self", "self", "joint"]

    def test_keeps_amounts_as_ranges(self, parsed):
        assert (parsed[0]["amount_low"], parsed[0]["amount_high"]) == (50001.0, 100000.0)

    def test_options_are_not_booked_as_equity(self, parsed):
        opt = parsed[2]
        assert opt["asset_type"] == "option"
        assert opt["option_type"] == "call"
        assert opt["ticker"] == "NVSTA"

    def test_sales_keep_their_action(self, parsed):
        assert parsed[3]["action"] == "sale"

    def test_empty_text_yields_nothing_rather_than_guessing(self):
        assert HousePTR.parse_text("", disclosed_date="2024-04-19") == []

    def test_unreadable_scan_scores_zero_confidence(self):
        assert HousePTR.extraction_confidence("", []) == 0.0

    def test_text_with_no_parsed_rows_is_flagged_for_review(self):
        # The dangerous case: looks ingested, is empty.
        text = "\n".join(f"garbled row $ {i}" for i in range(20))
        assert HousePTR.extraction_confidence(text, []) < 0.2

    def test_clean_filing_scores_high(self):
        parsed = HousePTR.parse_text(PTR_TEXT, disclosed_date="2024-04-19")
        assert HousePTR.extraction_confidence(PTR_TEXT, parsed) > 0.7


class TestForm4:
    @pytest.fixture
    def parsed(self):
        return EdgarForm4.parse_xml(FORM4_XML, disclosed_date="2024-03-20")

    def test_open_market_purchase_is_captured(self, parsed):
        buy = next(p for p in parsed if p["asset_type"] == "stock")
        assert buy["action"] == "purchase"
        assert buy["ticker"] == "ARDN"
        assert buy["txn_date"] == "2024-03-18"

    def test_amount_is_shares_times_price(self, parsed):
        buy = next(p for p in parsed if p["asset_type"] == "stock")
        assert buy["amount_low"] == pytest.approx(669_000.0)

    def test_grants_are_excluded(self, parsed):
        # Code 'A' is a compensation grant, not a decision to buy. Scoring it
        # as conviction would credit skill to a payroll event.
        assert all(p["txn_date"] != "2024-03-20" for p in parsed)
        assert len(parsed) == 2

    def test_derivative_keeps_strike_and_expiry(self, parsed):
        opt = next(p for p in parsed if p["asset_type"] == "option")
        assert opt["option_type"] == "call"
        assert opt["strike"] == 75.0
        assert opt["expiry"] == "2026-01-16"

    def test_option_premium_not_notional(self, parsed):
        # 2000 contracts at $4.25 premium = $8,500 at risk, not $150,000 notional.
        opt = next(p for p in parsed if p["asset_type"] == "option")
        assert opt["amount_low"] == pytest.approx(8_500.0)

    def test_malformed_xml_returns_nothing(self):
        assert EdgarForm4.parse_xml(b"not xml at all", disclosed_date="2024-01-01") == []


class Test13F:
    def test_parses_holdings(self):
        rows = Edgar13F.parse_holdings(INFOTABLE_XML)
        assert len(rows) == 2
        assert rows[0]["issuer"] == "ARDENT SEMICONDUCTOR"
        assert rows[0]["shares"] == 1_200_000.0

    def test_put_call_flag_is_preserved(self):
        # A put is a short-side bet; treating it as a long holding inverts the
        # manager's actual position.
        rows = Edgar13F.parse_holdings(INFOTABLE_XML)
        assert rows[1]["put_call"] == "Put"
