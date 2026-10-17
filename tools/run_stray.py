#!/usr/bin/env python3
"""Run the stray-order study headless (no browser), in parallel and at low priority, and optionally push it.

    python tools/run_stray.py --push                       # the app's default settings (gold funds, 1404/7/1-1405/7/1)
    python tools/run_stray.py --workers 4 --push           # fewer cores
    python tools/run_stray.py --start 20251001 --end 20260923

Every fund is saved on disk as soon as it is done (data/stray_cache): if the run is interrupted, running the same
command again resumes with the funds already done. Close the app first if it uses the same database heavily.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cats", default="gold", help="fund categories (comma separated)")
    ap.add_argument("--symbols", default="", help="explicit tickers instead of categories (comma separated)")
    ap.add_argument("--start", default="20250923", help="first day (YYYYMMDD, Gregorian)")
    ap.add_argument("--end", default="20260923", help="last day (YYYYMMDD, Gregorian)")
    ap.add_argument("--workers", type=int, default=0, help="worker processes (0 = every core but one)")
    ap.add_argument("--push", action="store_true", help="push the result to the analysis-results branch")
    ap.add_argument("--db", help="database file (default: the app's data/arbitrage.db)")
    a = ap.parse_args()
    import discount_study as S
    S._lower_priority()
    from database import Database
    from web_server import create_app
    app = create_app(Database(Path(a.db)) if a.db else Database())
    cl = app.test_client()
    body = {"cats": a.cats, "symbols": a.symbols, "start": a.start, "end": a.end, "workers": a.workers}
    r = cl.post("/api/disc/stray", json=body)
    if r.status_code != 200:
        print("could not start:", r.get_json())
        return 1
    print("stray-order study started (funds already done in an earlier run are reused)")
    t0, last = time.time(), ""
    while True:
        time.sleep(5)
        j = cl.get("/api/disc/stray/status").get_json() or {}
        pr = j.get("progress") or {}
        line = f"  {pr.get('phase', '')} {pr.get('done', 0)}/{pr.get('total', 0)}  ({(time.time() - t0) / 60:.0f} min)"
        if line != last:
            print(line, flush=True)
            last = line
        if not j.get("running"):
            res = j.get("result") or {}
            break
    if res.get("error"):
        print("error:", res["error"])
        return 1
    print("\n" + ((res.get("verdict") or {}).get("headline") or "done"))
    st = res.get("setup") or {}
    print(f"{st.get('funds')} funds, {st.get('days')} days, {st.get('workers')} workers, "
          f"{st.get('funds_from_cache')} from cache, {st.get('seconds')} s")
    if a.push:
        p = cl.post("/api/disc/push_results").get_json() or {}
        print("push:", "ok" if p.get("ok") else p.get("message"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
