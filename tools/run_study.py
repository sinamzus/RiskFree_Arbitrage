#!/usr/bin/env python3
"""Run a (big) optimizer study headless, at low priority, without the browser.

It repeats the LAST optimization started from the app (saved in analysis_results/last_study_request.json: the
same funds, period, dataset, form values and ticked parameters), optionally bigger and longer:

    python tools/run_study.py                                   # same as the last one
    python tools/run_study.py --hours 8 --samples 20000 --universes 200 --push
    python tools/run_study.py --workers 4                       # fewer cores (less memory, cooler laptop)
    python tools/run_study.py --request my_request.json

Every process runs below normal priority, so the computer stays usable; the number of worker processes is also
limited by the free memory (each holds a copy of the data). The result is saved like the app's (and with --push
sent to the analysis-results branch). Close the app first if it uses the same database heavily.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--request", default=str(ROOT / "analysis_results" / "last_study_request.json"))
    ap.add_argument("--hours", type=float, help="time limit (hours)")
    ap.add_argument("--samples", type=int, help="random configurations")
    ap.add_argument("--universes", type=int, help="structural combinations")
    ap.add_argument("--workers", type=int, help="worker processes (0 = automatic)")
    ap.add_argument("--push", action="store_true", help="push the result to the analysis-results branch")
    ap.add_argument("--db", help="database file (default: the app's data/arbitrage.db)")
    a = ap.parse_args()
    req = Path(a.request)
    if not req.exists():
        print(f"{req} not found: start one optimization from the app first (its settings are saved there).")
        return 2
    body = json.loads(req.read_text(encoding="utf-8"))
    if a.hours:
        body["max_seconds"] = int(a.hours * 3600)
    if a.samples:
        body["n_samples"] = a.samples
    if a.universes:
        body["max_universes"] = a.universes
    if a.workers is not None:
        body["workers"] = a.workers
    body["mode"] = "study"
    import discount_study as S
    S._lower_priority()
    from database import Database
    from web_server import create_app
    app = create_app(Database(Path(a.db)) if a.db else Database())
    cl = app.test_client()
    r = cl.post("/api/disc/optimize", json=body)
    if r.status_code != 200:
        print("could not start:", r.get_json())
        return 1
    print(f"study started: up to {body.get('max_seconds', 300) / 60:.0f} min, {body.get('n_samples', 400)} samples, "
          f"{body.get('max_universes', 20)} structural combinations, workers "
          f"{body.get('workers') or 'automatic (limited by free memory)'}")
    t0, last = time.time(), ""
    while True:
        time.sleep(10)
        j = cl.get("/api/disc/optimize/status").get_json() or {}
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
    v = res.get("verdict") or {}
    print("\n" + (v.get("headline") or "done"))
    tm = res.get("timing") or {}
    print(f"workers: {tm.get('workers')} {tm.get('workers_note') or ''} · {tm.get('evals')} evaluations · "
          f"{tm.get('total_s')} s")
    out = ROOT / "analysis_results"
    out.mkdir(exist_ok=True)
    (out / "optimizer_cli.json").write_text(json.dumps(res, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"saved: {out / 'optimizer_cli.json'}")
    if a.push:
        p = cl.post("/api/disc/push_results").get_json() or {}
        print("push:", "ok" if p.get("ok") else p.get("message"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
