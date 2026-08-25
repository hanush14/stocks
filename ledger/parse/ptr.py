"""Periodic Transaction Report parsing.

Each function here exists because a naive reading of a PTR produces a
confidently wrong number:

* Amounts are **ranges**, not values. Storing a midpoint as the trade size is
  the most common error in this data.
* Several of the highest-profile congressional trades are long-dated options.
  Booking a $1M-notional call as a $1M stock purchase misstates both the capital
  at risk and the return by an order of magnitude.
* Owner codes (SP / DC / JT) decide whether a trade is even the member's own.
* Filings are amended, and the amendment is often the interesting part.
"""
from __future__ import annotations

import re
from datetime import datetime

from ..schema import AMOUNT_BANDS, ASSET_TYPES, OWNERS

# "$1,001 - $15,000", "$1,001-$15,000", "$50,000,001 +", "Over $50,000,000"
_AMT = re.compile(
    r"\$?\s*([\d,]+)\s*(?:-|–|—|to)?\s*(?:\$\s*([\d,]+))?\s*(\+)?", re.I)
_TICKER = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,9})\)")
_OPTION = re.compile(
    r"\b(call|put)s?\b(?:.*?\bstrike\s*\$?([\d,.]+))?(?:.*?\b(?:exp(?:iry|ires|iration)?)\s*"
    r"(\d{1,2}/\d{1,2}/\d{2,4}))?", re.I)
_BRACKET_TYPE = re.compile(r"\[([A-Z]{2,3})\]")

ACTION_CODES = {
    "P": "purchase", "PURCHASE": "purchase",
    "S": "sale", "SALE": "sale",
    "S (PARTIAL)": "sale_partial", "S(PARTIAL)": "sale_partial", "SP": "sale_partial",
    "S (FULL)": "sale_full", "S(FULL)": "sale_full",
    "E": "exchange", "EXCHANGE": "exchange",
}

# Phrases that mean the filer did not personally direct the trade. Scoring these
# as skill misattributes agency to someone who had none.
_NON_DIRECTED = re.compile(
    r"\b(blind trust|qualified blind trust|managed account|"
    r"index fund|target date|excepted investment fund)\b", re.I)


class ParseError(ValueError):
    pass


def parse_amount(text: str) -> tuple[float | None, float | None]:
    """Parse a disclosure band into (low, high). `high` is None for the
    open-ended top band, and callers must keep treating it as open-ended."""
    if not text:
        return (None, None)
    s = text.strip()
    if re.search(r"\bover\b", s, re.I) or s.endswith("+"):
        m = re.search(r"([\d,]+)", s)
        return (float(m.group(1).replace(",", "")), None) if m else (None, None)
    m = _AMT.search(s)
    if not m:
        return (None, None)
    lo = float(m.group(1).replace(",", ""))
    hi = float(m.group(2).replace(",", "")) if m.group(2) else None
    if m.group(3) and hi is None:
        return (lo, None)
    if hi is None:
        # a single figure: snap to the band containing it, never invent a point
        for blo, bhi in AMOUNT_BANDS:
            if blo <= lo and (bhi is None or lo <= bhi):
                return (blo, bhi)
        return (lo, lo)
    return (lo, hi)


def parse_owner(code: str | None) -> str:
    if not code:
        return "self"
    c = code.strip().upper().replace(".", "")
    return OWNERS.get(c, "self")


def parse_action(code: str | None) -> str:
    if not code:
        raise ParseError("missing transaction type")
    c = code.strip().upper()
    if c in ACTION_CODES:
        return ACTION_CODES[c]
    for k, v in ACTION_CODES.items():
        if c.startswith(k):
            return v
    raise ParseError(f"unrecognised transaction type: {code!r}")


def parse_date(text: str) -> str:
    t = (text or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d %b %Y", "%b %d, %Y"):
        try:
            d = datetime.strptime(t, fmt).date()
            if fmt.endswith("%y") and d.year > datetime.now().year + 1:
                d = d.replace(year=d.year - 100)
            return d.isoformat()
        except ValueError:
            continue
    raise ParseError(f"unparseable date: {text!r}")


def extract_ticker(text: str) -> str | None:
    """Ticker from a parenthesised symbol. Returns None rather than guessing
    from the company name - a wrong ticker silently corrupts every return."""
    m = _TICKER.search(text or "")
    if not m:
        return None
    tk = m.group(1).strip(".")
    if tk in {"NA", "N/A", "NONE"} or len(tk) > 10:
        return None
    return tk


def extract_option(text: str) -> dict:
    """Option leg, if the description names one.

    Returns {} for ordinary equity. When a filing says 'call' but gives no
    strike or expiry, the option flag is still set: knowing it is an option and
    not knowing its terms is very different from believing it is stock.
    """
    m = _OPTION.search(text or "")
    if not m:
        return {}
    out: dict = {"option_type": m.group(1).lower()}
    if m.group(2):
        try:
            out["strike"] = float(m.group(2).replace(",", ""))
        except ValueError:
            pass
    if m.group(3):
        try:
            out["expiry"] = parse_date(m.group(3))
        except ParseError:
            pass
    return out


def asset_type_of(text: str, explicit: str | None = None) -> str:
    if explicit and explicit.upper() in ASSET_TYPES:
        return ASSET_TYPES[explicit.upper()]
    m = _BRACKET_TYPE.search(text or "")
    if m and m.group(1) in ASSET_TYPES:
        return ASSET_TYPES[m.group(1)]
    if _OPTION.search(text or ""):
        return "option"
    return "other"


def is_directed(text: str) -> bool:
    """False when the description indicates the filer did not choose the trade."""
    return not bool(_NON_DIRECTED.search(text or ""))


def parse_row(row: dict, *, disclosed_date: str) -> dict:
    """Normalise one PTR line into a transaction record.

    Expected keys (all optional except asset/type/date): owner, asset, ticker,
    asset_type, transaction_type, transaction_date, amount.
    """
    asset = (row.get("asset") or "").strip()
    amount_low, amount_high = parse_amount(row.get("amount", ""))
    txn = {
        "txn_date": parse_date(row.get("transaction_date") or row.get("date") or ""),
        "disclosed_date": disclosed_date,
        "ticker": row.get("ticker") or extract_ticker(asset),
        "asset_name": asset or None,
        "asset_type": asset_type_of(asset, row.get("asset_type")),
        "action": parse_action(row.get("transaction_type") or row.get("type")),
        "owner": parse_owner(row.get("owner")),
        "amount_low": amount_low,
        "amount_high": amount_high,
        "directed": is_directed(asset),
    }
    txn.update(extract_option(asset))
    if txn.get("option_type"):
        txn["asset_type"] = "option"
    return txn


def parse_rows(rows, *, disclosed_date: str) -> tuple[list[dict], list[dict]]:
    """Parse many rows. Returns (parsed, rejected).

    Rejected rows are kept with their error rather than dropped: a filing we
    could not read is a data-quality fact about that filer, and silently
    discarding it would flatter their record.
    """
    ok, bad = [], []
    for i, r in enumerate(rows):
        try:
            ok.append(parse_row(r, disclosed_date=disclosed_date))
        except (ParseError, ValueError) as e:
            bad.append({"index": i, "row": r, "error": str(e)})
    return ok, bad
