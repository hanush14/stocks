"""House of Representatives Periodic Transaction Reports.

The Clerk publishes one ZIP per year containing a tab-delimited index of every
financial disclosure filed that year, plus a PDF per filing:

    https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip
    https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{docid}.pdf

The index is structured and cheap; the transaction detail lives in the PDFs.
Many of those PDFs are scans, so `parse` reports an extraction confidence and
anything below the threshold is meant to go to a review queue rather than
straight into scoring.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime

from ..parse import ptr
from .base import Source

BASE = "https://disclosures-clerk.house.gov/public_disc"
INDEX_URL = BASE + "/financial-pdfs/{year}FD.zip"
PTR_PDF_URL = BASE + "/ptr-pdfs/{year}/{doc_id}.pdf"

# FilingType codes in the index. 'P' is the periodic transaction report; the
# annual report ('O'/'A') is a holdings snapshot, not transactions.
PTR_TYPES = {"P"}

INDEX_FIELDS = ["Prefix", "Last", "First", "Suffix", "FilingType",
                "StateDst", "Year", "FilingDate", "DocID"]

# One PTR line as it survives text extraction, e.g.
#   SP  Apple Inc. (AAPL) [ST]  P  03/12/2024  04/19/2024  $1,001 - $15,000
#
# The owner code sits before the asset in some layouts and after it in others,
# depending on how the PDF's columns survive text extraction. Both positions are
# accepted; assuming one silently attributes a spouse's trade to the member.
_LINE = re.compile(
    r"^\s*(?P<owner_pre>SP|DC|JT)?\s*"
    r"(?P<asset>.+?)\s+"
    r"(?:(?P<owner_post>SP|DC|JT)\s+)?"
    r"(?P<type>P|S(?:\s*\((?:partial|full)\))?|E)\s+"
    r"(?P<txn_date>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<notified>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<amount>\$[\d,]+(?:\s*-\s*\$[\d,]+)?\s*\+?)",
    re.I | re.M)


class HousePTR(Source):
    name = "house_ptr"
    doc_type = "ptr"

    def discover(self, *, year: int) -> list[dict]:
        """Every PTR filed in `year`, from the annual index ZIP."""
        raw = self.client.get(INDEX_URL.format(year=year))
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".txt"))
            text = z.read(name).decode("utf-8", errors="replace")

        out = []
        for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
            if (row.get("FilingType") or "").strip().upper() not in PTR_TYPES:
                continue
            doc_id = (row.get("DocID") or "").strip()
            if not doc_id:
                continue
            out.append({
                "doc_id": doc_id,
                "year": int(row.get("Year") or year),
                "last": (row.get("Last") or "").strip(),
                "first": (row.get("First") or "").strip(),
                "state_district": (row.get("StateDst") or "").strip(),
                "filed_date": _iso(row.get("FilingDate")),
                "url": PTR_PDF_URL.format(year=year, doc_id=doc_id),
            })
        return out

    def fetch(self, ref: dict) -> bytes:
        return self.client.get(ref["url"])

    def parse(self, raw: bytes, ref: dict) -> list[dict]:
        """Extract transactions from a filing.

        `raw` is a PDF. Text extraction is delegated to `extract_text` so the
        OCR backend is swappable; a scanned filing that yields no text returns
        no transactions rather than a guess.
        """
        text = self.extract_text(raw)
        return self.parse_text(text, disclosed_date=ref["filed_date"])

    # -- text handling --------------------------------------------------------

    @staticmethod
    def extract_text(raw: bytes) -> str:
        """PDF -> text. Tries pdfminer, then a scanned-page OCR path.

        Both are optional dependencies: the pipeline degrades to "could not
        read this filing", which is recorded as a data-quality fact about the
        filer rather than silently dropped.
        """
        try:
            from pdfminer.high_level import extract_text as _pdfminer  # type: ignore
            return _pdfminer(io.BytesIO(raw)) or ""
        except ImportError:
            pass
        except Exception:
            return ""
        try:                                    # scanned filings
            import pytesseract  # type: ignore
            from pdf2image import convert_from_bytes  # type: ignore
            return "\n".join(pytesseract.image_to_string(p)
                             for p in convert_from_bytes(raw))
        except Exception:
            return ""

    @staticmethod
    def parse_text(text: str, *, disclosed_date: str) -> list[dict]:
        rows = []
        for m in _LINE.finditer(text or ""):
            rows.append({
                "owner": m.group("owner_pre") or m.group("owner_post") or "",
                "asset": m.group("asset").strip(),
                "transaction_type": m.group("type").strip(),
                "transaction_date": m.group("txn_date"),
                "amount": m.group("amount"),
            })
        parsed, rejected = ptr.parse_rows(rows, disclosed_date=disclosed_date)
        for p in parsed:
            p["_rejected"] = len(rejected)
        return parsed

    @staticmethod
    def extraction_confidence(text: str, parsed: list[dict]) -> float:
        """Rough confidence that this filing was read correctly.

        A filing with plenty of text but no parsed rows is the dangerous case:
        it looks ingested and is empty. That scores low so it lands in review.
        """
        if not text.strip():
            return 0.0
        if not parsed:
            return 0.1
        dollar_lines = sum(1 for ln in text.splitlines() if "$" in ln)
        if dollar_lines == 0:
            return 0.5
        return max(0.0, min(1.0, len(parsed) / dollar_lines))


def _iso(s: str | None) -> str:
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unparseable filing date: {s!r}")
