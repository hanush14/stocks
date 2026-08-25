#!/usr/bin/env python3
"""Build the member roster for the app from the unitedstates/congress-legislators dataset.

Source (public domain / CC0):
    https://github.com/unitedstates/congress-legislators
      legislators-current.yaml          - every sitting member of Congress
      committees-current.yaml           - every standing/select/joint committee
      committee-membership-current.yaml - committee rosters, keyed by bioguide id

Everything written to the output file is taken directly from those files. The one
piece of added judgement is COMMITTEE_SECTORS, which maps a committee's real
legislative jurisdiction onto GICS sectors so trades can later be classified as
in- or out-of-jurisdiction.

Usage:  python3 scripts/build_members.py <path-to-congress-legislators> app/members.json
"""
import json
import sys
from pathlib import Path

import yaml

# Committee -> GICS sectors the committee's jurisdiction actually covers.
# Committees with no meaningful market jurisdiction (Budget, Rules, Ethics,
# Foreign Affairs, ...) are deliberately absent rather than mapped to something
# vague: a wrong mapping would manufacture false conflict-of-interest signals.
COMMITTEE_SECTORS = {
    "HSAG": ["Consumer Staples", "Materials"],
    "SSAF": ["Consumer Staples", "Materials"],
    "HSAS": ["Industrials"],
    "SSAS": ["Industrials"],
    "HSBA": ["Financials", "Real Estate"],
    "SSBK": ["Financials", "Real Estate"],
    "HSWM": ["Financials", "Health Care"],
    "SSFI": ["Financials", "Health Care"],
    "JSTX": ["Financials"],
    "HSIF": ["Energy", "Utilities", "Health Care",
             "Communication Services", "Consumer Discretionary"],
    "HSII": ["Energy", "Materials", "Utilities"],
    "SSEG": ["Energy", "Materials", "Utilities"],
    "SSEV": ["Utilities", "Materials", "Industrials"],
    "HSSY": ["Information Technology"],
    "SSCM": ["Industrials", "Communication Services", "Information Technology"],
    "HSPW": ["Industrials"],
    "SSHR": ["Health Care"],
    "SPAG": ["Health Care"],
    "HSVR": ["Health Care"],
    "SSVA": ["Health Care"],
    "HSHM": ["Industrials", "Information Technology"],
    "SSGA": ["Industrials", "Information Technology"],
    "HLIG": ["Industrials", "Information Technology"],
    "SLIN": ["Industrials", "Information Technology"],
    "HSJU": ["Information Technology", "Communication Services"],
    "SSJU": ["Information Technology", "Communication Services"],
    "HSED": ["Consumer Discretionary"],
}

# Committees whose leadership confers real agenda-setting power over the sectors above.
LEAD_TITLES = ("Chair", "Chairman", "Chairwoman", "Ranking Member", "Vice Chair")


def load(base: Path):
    read = lambda n: yaml.safe_load((base / n).read_text())
    return (read("legislators-current.yaml"),
            read("committees-current.yaml"),
            read("committee-membership-current.yaml"))


def build(base: Path):
    legislators, committees, membership = load(base)

    cmte_by_id = {c["thomas_id"]: c for c in committees}

    # bioguide -> [committee assignment, ...]
    assignments: dict[str, list] = {}
    for key, members in membership.items():
        # Subcommittee keys are the parent id plus a numeric suffix; roll them
        # up to the parent so a member is credited with the committee itself.
        parent = key if key in cmte_by_id else key[:4]
        cmte = cmte_by_id.get(parent)
        if not cmte:
            continue
        for m in members:
            bid = m.get("bioguide")
            if not bid:
                continue
            slot = assignments.setdefault(bid, {})
            title = m.get("title") or ""
            prev = slot.get(parent)
            # keep the most senior title seen across committee + subcommittees
            if prev is None or (title.startswith(LEAD_TITLES) and not prev["title"]):
                slot[parent] = {"id": parent, "name": cmte["name"], "title": title}

    out = []
    for p in legislators:
        term = p["terms"][-1]
        bid = p["id"]["bioguide"]
        cmtes = list(assignments.get(bid, {}).values())

        sectors = sorted({s for c in cmtes for s in COMMITTEE_SECTORS.get(c["id"], [])})
        leads = [c for c in cmtes if c["title"].startswith(LEAD_TITLES)]

        name = p["name"].get("official_full") or f"{p['name']['first']} {p['name']['last']}"
        out.append({
            "id": bid,
            "name": name,
            "chamber": term["type"],                       # rep | sen
            "party": term.get("party", ""),
            "state": term["state"],
            "district": term.get("district"),
            "since": min(t["start"][:4] for t in p["terms"]),
            "committees": sorted(cmtes, key=lambda c: c["name"]),
            "sectors": sectors,
            "leadership": [{"id": c["id"], "name": c["name"], "title": c["title"]} for c in leads],
        })

    out.sort(key=lambda m: m["name"].split()[-1])
    cmte_out = [{
        "id": c["thomas_id"], "name": c["name"], "type": c.get("type", ""),
        "sectors": COMMITTEE_SECTORS.get(c["thomas_id"], []),
    } for c in sorted(committees, key=lambda c: c["name"])]

    return {"members": out, "committees": cmte_out}


if __name__ == "__main__":
    base = Path(sys.argv[1])
    dest = Path(sys.argv[2])
    data = build(base)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, separators=(",", ":")))

    m, c = data["members"], data["committees"]
    print(f"members     {len(m)}")
    print(f"committees  {len(c)}  ({sum(1 for x in c if x['sectors'])} sector-mapped)")
    print(f"on a cmte   {sum(1 for x in m if x['committees'])}")
    print(f"w/ sectors  {sum(1 for x in m if x['sectors'])}")
    print(f"leadership  {sum(1 for x in m if x['leadership'])}")
    print(f"bytes       {dest.stat().st_size:,}")
