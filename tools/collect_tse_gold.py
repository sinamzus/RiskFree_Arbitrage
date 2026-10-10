"""Collect tick-level TSE data (every trade + the full order-book event stream) for the gold funds.

Same work as the «📥 دادهٔ TSE» panel of the web app, for long runs from a terminal:

    python tools/collect_tse_gold.py                       # every gold fund, every day of the NAV dump
    python tools/collect_tse_gold.py --start 20251001 --end 20251231 --symbols زر,عیار
    python tools/collect_tse_gold.py --coverage            # only print what is already there

Resumable: a (fund, day, kind) that is stored is never fetched again; Ctrl+C stops cleanly and the next run
continues where this one stopped.  Failed requests are not stored and are retried next time.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tse_gold as G                       # noqa: E402
from database import Database              # noqa: E402


def _print_coverage(db, start, end):
    rows = G.coverage(db, start, end)
    print(f"{'symbol':<12}{'ins_code':<22}{'dump':>6}{'trades':>8}{'book':>7}{'MB':>8}  name")
    for r in rows:
        print(f"{r['symbol']:<12}{(r['ins_code'] or '—'):<22}{r['dump_days']:>6}{r['trades_days']:>8}"
              f"{r['book_days']:>7}{r['bytes'] / 1e6:>8.1f}  {r['tse_name']}")
    print(f"total MB: {sum(r['bytes'] for r in rows) / 1e6:.1f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=int, help="YYYYMMDD (Gregorian); default = first day of the NAV dump")
    ap.add_argument("--end", type=int, help="YYYYMMDD (Gregorian); default = last day of the NAV dump")
    ap.add_argument("--symbols", help="comma-separated tickers (default: every gold fund)")
    ap.add_argument("--kinds", default="trades,book", help="trades,book (default both)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--coverage", action="store_true", help="only print the coverage table")
    a = ap.parse_args()

    db = Database()
    G.ensure_schema(db)
    if a.coverage:
        _print_coverage(db, a.start, a.end)
        return
    funds = G.gold_funds(db)
    ids = None
    if a.symbols:
        want = {s.strip() for s in a.symbols.split(",") if s.strip()}
        ids = [f["symbol_id"] for f in funds if f["symbol"] in want]
        if not ids:
            sys.exit(f"none of {sorted(want)} is a gold fund of the NAV dump")
    kinds = tuple(k for k in a.kinds.split(",") if k in G.KINDS) or G.KINDS

    from data_fetcher import TSETMCFetcher
    prog, stop, lock = {}, threading.Event(), threading.Lock()
    th = threading.Thread(target=lambda: prog.update(result=G.collect(
        db, TSETMCFetcher(), ids, a.start, a.end, kinds, a.workers, prog, stop, lock)), daemon=True)
    th.start()
    try:
        while th.is_alive():
            time.sleep(2)
            with lock:
                d, t = prog.get("done", 0), prog.get("total", 0)
                el = time.time() - prog.get("started", time.time())
                eta = (t - d) * el / d if d else 0
                print(f"\r{d}/{t}  rows {prog.get('rows', 0):,}  failed {prog.get('failed', 0)}  "
                      f"ETA {eta / 60:5.1f} min  {prog.get('current', '')[:40]:<40}", end="", flush=True)
    except KeyboardInterrupt:
        print("\nstopping after the requests in flight ...")
        stop.set()
        th.join()
    print()
    if prog.get("missing_ins_code"):
        print("no TSE code found for:", ", ".join(prog["missing_ins_code"]), "— set it in the web panel")
    _print_coverage(db, a.start, a.end)


if __name__ == "__main__":
    main()
