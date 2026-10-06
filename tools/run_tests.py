#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Full end-to-end test of the NAV-discount tab on a synthetic world (no real data needed).

    python tools\\run_tests.py            # engine + web + optimizer + static UI checks (~1-3 min)
    python tools\\run_tests.py --ui       # also drives a real browser (needs: pip install playwright
                                          #   and: playwright install chromium)
    python tools\\run_tests.py --json out.json

Exit code 0 = everything passed, 1 = at least one failure.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import discount_fulltest as T  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ui", action="store_true")
    ap.add_argument("--json")
    ap.add_argument("--only", help="show only this group (substring)")
    a = ap.parse_args()
    res = T.run_all(ui=a.ui)
    group = None
    for it in res["items"]:
        if a.only and a.only not in it["group"]:
            continue
        if it["group"] != group:
            group = it["group"]
            print(f"\n== {group}")
        mark = {"pass": "OK  ", "fail": "FAIL", "skip": "SKIP"}[it["status"]]
        print(f"  [{mark}] {it['name']}" + (f"\n         {it['detail']}" if it["status"] != "pass" and it["detail"] else ""))
    s = res["summary"]
    print(f"\n{s['pass']} passed, {s['fail']} failed, {s['skip']} skipped  ({res['seconds']} s)")
    if a.json:
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if s["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
