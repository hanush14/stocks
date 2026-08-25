"""SEC EDGAR: Form 4 insider transactions and Form 13F institutional holdings.

Form 4 is the highest-value feed in the whole system: it is filed within two
business days of the trade, against 30-45 days for a congressional PTR and up
to ~135 days for a 13F. Most of the alpha decay this product measures has not
happened yet when a Form 4 lands.

EDGAR requires a declared User-Agent with a contact address and asks for no more
than ~10 requests/second; `PoliteClient` defaults are far below that.
"""
from __future__ import annotations

import json
import re
from xml.etree import ElementTree as ET

from .base import Source

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"
FILING_INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/index.json"

# Form 4 transaction codes. Only open-market trades reflect a decision; grants,
# exercises and tax withholding are compensation mechanics and must not be
# scored as convictions.
OPEN_MARKET = {"P": "purchase", "S": "sale"}
NON_DISCRETIONARY = {"A", "F", "G", "M", "C", "D", "I", "J"}


class EdgarForm4(Source):
    name = "sec_form4"
    doc_type = "form4"

    def discover(self, *, cik: str | int, limit: int = 200) -> list[dict]:
        cik_i = int(str(cik).lstrip("Cc IK").lstrip("0") or 0)
        raw = self.client.get(SUBMISSIONS.format(cik=cik_i))
        data = json.loads(raw)
        recent = data.get("filings", {}).get("recent", {})
        out = []
        for form, acc, date, doc in zip(recent.get("form", []),
                                        recent.get("accessionNumber", []),
                                        recent.get("filingDate", []),
                                        recent.get("primaryDocument", [])):
            if form not in ("4", "4/A"):
                continue
            out.append({
                "cik": cik_i, "accession": acc, "filed_date": date,
                "is_amendment": form.endswith("/A"),
                "url": ARCHIVE.format(cik=cik_i, acc_nodash=acc.replace("-", ""), doc=doc),
            })
            if len(out) >= limit:
                break
        return out

    def fetch(self, ref: dict) -> bytes:
        return self.client.get(ref["url"])

    def parse(self, raw: bytes, ref: dict) -> list[dict]:
        return self.parse_xml(raw, disclosed_date=ref["filed_date"])

    @staticmethod
    def parse_xml(raw: bytes, *, disclosed_date: str) -> list[dict]:
        """Parse ownershipDocument XML into transactions.

        Derivative rows keep their strike and expiry: booking a long-dated call
        as if it were the underlying stock misstates both the capital at risk
        and the return by an order of magnitude.
        """
        text = raw.decode("utf-8", errors="replace")
        m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", text, re.S)
        if not m:
            return []
        root = ET.fromstring(m.group(0))
        ticker = (root.findtext(".//issuerTradingSymbol") or "").strip().upper() or None
        issuer = (root.findtext(".//issuerName") or "").strip() or None

        rows: list[dict] = []
        for tag, is_deriv in (("nonDerivativeTransaction", False),
                              ("derivativeTransaction", True)):
            for t in root.iter(tag):
                code = (_v(t, ".//transactionCode") or "").strip().upper()
                if code in NON_DISCRETIONARY or code not in OPEN_MARKET:
                    continue
                txn_date = (_v(t, ".//transactionDate") or "").strip()[:10]
                if not txn_date:
                    continue
                shares = _f(_v(t, ".//transactionShares"))
                price = _f(_v(t, ".//transactionPricePerShare"))
                value = (shares * price) if (shares and price) else None
                row = {
                    "txn_date": txn_date,
                    "disclosed_date": disclosed_date,
                    "ticker": ticker,
                    "asset_name": issuer,
                    "asset_type": "option" if is_deriv else "stock",
                    "action": OPEN_MARKET[code],
                    "owner": "self",
                    # Form 4 gives exact figures, unlike a PTR band. Both ends
                    # are set to the same value so downstream code can treat
                    # every amount uniformly as an interval.
                    "amount_low": value,
                    "amount_high": value,
                    "directed": True,
                }
                if is_deriv:
                    row["strike"] = _f(_v(t, ".//conversionOrExercisePrice"))
                    exp = (_v(t, ".//expirationDate") or "").strip()[:10]
                    if exp:
                        row["expiry"] = exp
                    title = (_v(t, ".//securityTitle") or "").lower()
                    row["option_type"] = "put" if "put" in title else "call"
                rows.append(row)
        return rows


class Edgar13F(Source):
    """Quarterly institutional holdings.

    A 13F is a *snapshot*, not a transaction log: positions are inferred by
    diffing consecutive quarters, which cannot see a round trip opened and
    closed inside one quarter. It also covers long US equity only - no shorts,
    no cash, no non-US listings - so a manager's real book may look nothing
    like this.
    """
    name = "sec_13f"
    doc_type = "13f"

    def discover(self, *, cik: str | int, limit: int = 40) -> list[dict]:
        cik_i = int(str(cik).lstrip("Cc IK").lstrip("0") or 0)
        data = json.loads(self.client.get(SUBMISSIONS.format(cik=cik_i)))
        recent = data.get("filings", {}).get("recent", {})
        out = []
        for form, acc, date, rep in zip(recent.get("form", []),
                                        recent.get("accessionNumber", []),
                                        recent.get("filingDate", []),
                                        recent.get("reportDate", [])):
            if not form.startswith("13F-HR"):
                continue
            out.append({"cik": cik_i, "accession": acc, "filed_date": date,
                        "period": rep,
                        "url": FILING_INDEX.format(cik=cik_i,
                                                   acc_nodash=acc.replace("-", ""))})
            if len(out) >= limit:
                break
        return out

    def fetch(self, ref: dict) -> bytes:
        """Resolve the information-table XML from the filing index."""
        index = json.loads(self.client.get(ref["url"]))
        items = index.get("directory", {}).get("item", [])
        table = next((i["name"] for i in items
                      if i["name"].lower().endswith(".xml")
                      and "primary_doc" not in i["name"].lower()), None)
        if not table:
            raise FileNotFoundError(f"no information table in {ref['accession']}")
        base = ref["url"].rsplit("/", 1)[0]
        return self.client.get(f"{base}/{table}")

    @staticmethod
    def parse_holdings(raw: bytes) -> list[dict]:
        """Information-table XML -> holdings. Namespace-agnostic."""
        root = ET.fromstring(raw.decode("utf-8", errors="replace"))
        out = []
        for info in root.iter():
            if not info.tag.endswith("infoTable"):
                continue
            out.append({
                "issuer": _t(info, "nameOfIssuer"),
                "cusip": _t(info, "cusip"),
                "value": _f(_t(info, "value")),
                "shares": _f(_t(info, "sshPrnamt")),
                "put_call": _t(info, "putCall"),
            })
        return out

    def parse(self, raw: bytes, ref: dict) -> list[dict]:
        return self.parse_holdings(raw)


def _v(el, path: str) -> str | None:
    """Form 4 wraps most values in a <value> child."""
    node = el.find(path)
    if node is None:
        return None
    inner = node.find("value")
    return (inner.text if inner is not None else node.text) or None


def _t(el, name: str) -> str | None:
    for child in el.iter():
        if child.tag.endswith(name):
            return (child.text or "").strip() or None
    return None


def _f(s) -> float | None:
    try:
        return float(str(s).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None
