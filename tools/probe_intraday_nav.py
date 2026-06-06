#!/usr/bin/env python3
"""Probe TSE / FIPIRAN for any source of HISTORICAL INTRADAY NAV.

Why this script exists
----------------------
The backtest can only honestly use a NAV-referenced strategy if we can obtain
the fund's NAV *as it was during the trading day, historically*. TSETMC archives
historical PRICE (GetClosingPriceHistory) and historical ORDER BOOK
(BestLimits/{ins}/{date}) — but there is no documented endpoint that archives an
intraday NAV time-series. This tool exhaustively tries every plausible endpoint
so we can prove, empirically, whether such a source exists.

It checks three questions for each fund:
  1. Is there an intraday NAV *time-series* for a past date?      (the holy grail)
  2. Is there a daily NAV *history* (one value per day)?          (FIPIRAN — known)
  3. Does the live NAV endpoint expose anything beyond a single   (sanity check)
     current value?

Run it anywhere TSE/FIPIRAN are reachable (it will NOT work inside a Claude Code
web sandbox whose network policy allow-lists only a few hosts — you'll see
"403 Host not in allowlist"). Usage:

    python tools/probe_intraday_nav.py                # a couple of sample funds
    python tools/probe_intraday_nav.py --all          # every fund in config
    python tools/probe_intraday_nav.py --date 20260603

Output is a compact PASS/FAIL table plus, for any endpoint that returns data, a
preview of the JSON keys so you can judge whether intraday NAV is really there.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

# allow running from repo root or from tools/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import FIXED_INCOME_ETFS, REQUEST_HEADERS  # noqa: E402

TSETMC_CDN  = "https://cdn.tsetmc.com/api"
TSETMC_MAIN = "https://www.tsetmc.com"
FIPIRAN     = "https://fund.fipiran.ir/api/v1"

# A recent trading day to ask history endpoints about (Gregorian YYYYMMDD).
DEFAULT_DATE = 20260603


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(REQUEST_HEADERS)
    s.headers["Referer"] = "https://www.tsetmc.com/"
    s.headers["Origin"]  = "https://www.tsetmc.com"
    return s


def _hit(sess: requests.Session, url: str) -> tuple[str, object]:
    """GET *url*; return (verdict, payload-preview).

    verdict ∈ {DATA, EMPTY, HTML, 4xx/5xx code, ERR}.
    """
    try:
        r = sess.get(url, timeout=12)
    except requests.exceptions.RequestException as e:
        return f"ERR:{type(e).__name__}", str(e)[:80]

    if r.status_code != 200:
        return str(r.status_code), r.text[:80].replace("\n", " ")

    body = r.text.strip()
    if not body:
        return "EMPTY", ""
    if body[0] not in "{[":
        # React SPA shell or HTML — not a data endpoint
        return "HTML", f"{len(body)}B {body[:50]!r}"

    try:
        obj = json.loads(body)
    except ValueError:
        return "BADJSON", body[:80]

    # Summarise structure so we can eyeball whether intraday NAV is present.
    return "DATA", _summarize(obj)


def _summarize(obj) -> str:
    """One-line structural summary of a JSON payload."""
    if isinstance(obj, dict):
        parts = []
        for k, v in list(obj.items())[:6]:
            if isinstance(v, list):
                n = len(v)
                inner = (", ".join(list(v[0].keys())[:8])
                         if n and isinstance(v[0], dict) else "")
                parts.append(f"{k}[{n}]{{{inner}}}")
            elif isinstance(v, dict):
                parts.append(f"{k}{{{','.join(list(v.keys())[:6])}}}")
            else:
                parts.append(f"{k}={str(v)[:18]}")
        return " | ".join(parts)
    if isinstance(obj, list):
        head = obj[0] if obj else None
        keys = ", ".join(list(head.keys())[:8]) if isinstance(head, dict) else ""
        return f"list[{len(obj)}] {{{keys}}}"
    return str(obj)[:80]


def probe_fund(sess: requests.Session, fund: dict, date_int: int) -> None:
    ins = fund["ins_code"]
    sym = fund["symbol"]
    print(f"\n══ {sym}  (ins={ins}) ══")

    # ---- Group A: TSETMC candidate INTRADAY-NAV history endpoints ----------
    # None of these are documented; we try them to prove absence/presence.
    intraday_candidates = [
        f"{TSETMC_CDN}/Fund/GetETFByInsCode/{ins}",
        f"{TSETMC_CDN}/Fund/GetEtfNav/{ins}",
        f"{TSETMC_CDN}/Fund/GetETFNAVHistory/{ins}/{date_int}",
        f"{TSETMC_CDN}/Fund/GetEtfNavHistory/{ins}/{date_int}",
        f"{TSETMC_CDN}/Fund/GetNavHistory/{ins}/{date_int}",
        f"{TSETMC_CDN}/ClosingPrice/GetNavHistory/{ins}/{date_int}",
        f"{TSETMC_CDN}/Instrument/GetNavHistory/{ins}/{date_int}",
        f"{TSETMC_MAIN}/Loader.aspx?ParTree=15131W&i={ins}",   # legacy
    ]
    print("  · TSETMC intraday-NAV candidates:")
    for u in intraday_candidates:
        verdict, prev = _hit(sess, u)
        tag = u.replace(TSETMC_CDN, "").replace(TSETMC_MAIN, "")
        print(f"      [{verdict:>10}] {tag}")
        if verdict == "DATA":
            print(f"                   → {prev}")

    # ---- Group B: FIPIRAN daily-NAV history (known to exist) ---------------
    # FIPIRAN keys funds by regNo, not insCode, so the chart call needs a regNo
    # discovered from the fund list. We surface the list call so you can map it.
    print("  · FIPIRAN daily-NAV history:")
    for u in [
        f"{FIPIRAN}/fund/getfundchart?regno=&insCode={ins}",
        f"{FIPIRAN}/fund/fundnavchart?insCode={ins}",
        f"{FIPIRAN}/fund/getfundnav?insCode={ins}",
    ]:
        verdict, prev = _hit(sess, u)
        print(f"      [{verdict:>10}] {u.replace(FIPIRAN,'')}")
        if verdict == "DATA":
            print(f"                   → {prev}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="probe every fund in config")
    ap.add_argument("--date", type=int, default=DEFAULT_DATE,
                    help="historical date to query (YYYYMMDD)")
    args = ap.parse_args()

    funds = FIXED_INCOME_ETFS if args.all else FIXED_INCOME_ETFS[:2]
    sess = _session()

    print(f"Probing {len(funds)} fund(s) for date {args.date} ...")
    print("Legend: DATA=json returned · HTML=SPA shell (no data) · 403/500=blocked")

    for f in funds:
        probe_fund(sess, f, args.date)

    print("\nDone. Interpretation:")
    print("  • If every 'intraday-NAV candidate' is HTML/4xx → no intraday NAV")
    print("    archive exists; reconstruct it by interpolating daily NAVs.")
    print("  • If a FIPIRAN call returns DATA with a per-day NAV list → use it")
    print("    to back-fill accurate DAILY NAV per historical date.")


if __name__ == "__main__":
    main()
