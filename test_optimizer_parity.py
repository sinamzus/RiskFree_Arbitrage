#!/usr/bin/env python3
"""Parity harness for the optimizer fast path (trigger-compressed batch replay).

The optimizer's speed transformation is only admissible if the sparse replay
(_replay_triggers) produces BIT-IDENTICAL trades to the reference replay
(_replay_stream) for every parameter combo.  This harness builds randomized
synthetic multi-day order-book histories that exercise every engine path —
skipped days, missing symbols (final close), one-sided books, thin bids
(partial sells), force_eod, needs_replacement, min_hold across month
boundaries — and asserts exact equality.

Usage
-----
  python test_optimizer_parity.py               # stratified subset (~fast)
  PARITY_FULL=1  python test_optimizer_parity.py  # the entire COARSE_GRID
  PARITY_SEEDS=8 python test_optimizer_parity.py  # more fuzz seeds
"""
from __future__ import annotations

import itertools
import json
import os
import random
import sys
import time
from dataclasses import asdict

from bond_backtest import (
    BondBacktestParams, COARSE_GRID, _OPT_SHARED,
    _build_decision_stream, _replay_stream, _simulate_cache,
    _index_stream_triggers, _replay_triggers, _date_ordinal,
    _combo_params, _opt_group_worker, _c2f_optimize, _result_sort_key,
)
from bonds import price_zero_coupon

FV = 1_000_000.0


# ── Synthetic order-book history ─────────────────────────────────────────────

def _snaps_for_day(rng: random.Random, date_int: int, dtm: int,
                   n_ticks: int) -> list[dict]:
    """One symbol-day of OB snapshots with a z-bias random walk so entry/exit
    thresholds actually get crossed."""
    curve_y = 0.27 + 0.00008 * dtm            # synthetic "true" curve
    bias = rng.uniform(-250.0, 250.0)         # starting z-bias (bps)
    t = 90_000 + rng.randrange(0, 1800)
    snaps = []
    for _ in range(n_ticks):
        bias = max(-400.0, min(400.0, bias + rng.uniform(-60.0, 60.0)))
        mid = price_zero_coupon(curve_y + bias / 10_000.0, FV, dtm)
        s = rng.uniform(0.0004, 0.004)        # half-spread fraction
        snap: dict = {"time": t, "date": date_int}
        r = rng.random()
        has_bid = r > 0.06                    # ~6 % one-sided (no bid)
        has_ask = r < 0.94                    # ~6 % one-sided (no ask)
        if has_bid:
            for i in range(1, rng.choice([3, 4, 5]) + 1):
                snap[f"bid{i}_price"] = round(mid * (1 - s - 0.001 * (i - 1)), 2)
                # Occasional 1-5-unit best bid → partial sells downstream.
                snap[f"bid{i}_vol"] = (rng.randrange(1, 6) if rng.random() < 0.25
                                       else rng.randrange(20, 500))
        if has_ask:
            for i in range(1, rng.choice([3, 4, 5]) + 1):
                snap[f"ask{i}_price"] = round(mid * (1 + s + 0.001 * (i - 1)), 2)
                snap[f"ask{i}_vol"] = rng.randrange(10, 500)
        snaps.append(snap)
        t += rng.randrange(20, 400)
        if t > 123_000:
            break
    return snaps


def build_cache(seed: int):
    """Six days spanning a month boundary, seven symbols with dtm 12..400.

    Crafted stress points:
      - dtm=12 sym is dropped by both min_dtm grids; dtm=22 only by min_dtm=30
      - day 3 has just 3 rows → with min_dtm=30 it can fall under min_pts
      - one symbol disappears after day 4 → exercises the "final" close path
    """
    rng = random.Random(seed)
    dates = [20260628, 20260629, 20260630, 20260701, 20260702, 20260703]
    syms = [("S12", 12), ("S22", 22), ("S60", 60), ("S120", 120),
            ("S200", 200), ("S300", 300), ("S400", 400)]
    cache: dict = {}
    for di, d in enumerate(dates):
        rows = []
        for si, (sym, dtm0) in enumerate(syms):
            if sym == "S300" and di >= 4:
                continue                       # vanishes → final close
            if di == 2 and si > 3:
                continue                       # thin day → skip patterns
            dtm = dtm0 - di                    # natural decay
            snaps = _snaps_for_day(rng, d, dtm, rng.randrange(22, 45))
            if snaps:
                rows.append((sym, FV, dtm, snaps))
        if rows:
            cache[d] = rows
    return dates, cache


# ── Combo enumeration ────────────────────────────────────────────────────────

def coarse_combos() -> list[tuple]:
    return [
        (deg, minp, ent, ex, stp, emax, mdtm, mhold, feod, mep, nr, cft, bff)
        for deg, minp, ent, ex, stp, emax, mdtm, mhold, feod, mep, nr, cft, bff
        in itertools.product(
            COARSE_GRID["degree"], COARSE_GRID["min_curve_points"],
            COARSE_GRID["entry_bps"], COARSE_GRID["exit_bps"],
            COARSE_GRID["step_secs"], COARSE_GRID["entry_max_bps"],
            COARSE_GRID["min_dtm"], COARSE_GRID["min_hold_days"],
            COARSE_GRID["force_eod"], COARSE_GRID["min_exit_profit_bps"],
            COARSE_GRID["exit_needs_replacement"],
            COARSE_GRID["entry_confirm_ticks"], COARSE_GRID["entry_best_first"])
        if ex < ent and ent <= emax     # mirrors _c2f_optimize validity filter
    ]


def pick_combos(rng: random.Random, full: bool) -> list[tuple]:
    combos = coarse_combos()
    if full:
        return combos
    sample = rng.sample(combos, min(2400, len(combos)))
    # Force the corners so every grid edge is always covered.
    ents, exs = COARSE_GRID["entry_bps"], COARSE_GRID["exit_bps"]
    for ent in (ents[0], ents[-1]):
        for ex in (exs[0], exs[-1]):
            if ex < ent:
                for cft in (0, COARSE_GRID["entry_confirm_ticks"][-1]):
                    for bff in (False, True):
                        sample.append((1, 3, ent, ex, 0,
                                       COARSE_GRID["entry_max_bps"][-1],
                                       COARSE_GRID["min_dtm"][0],
                                       COARSE_GRID["min_hold_days"][-1],
                                       True, 0.0, True, cft, bff))
    return list(dict.fromkeys(sample))


# ── Old vs new evaluation ────────────────────────────────────────────────────

def _group_tmpl(base: BondBacktestParams, gkey) -> BondBacktestParams:
    degree, minpts, step, mdtm = gkey
    return BondBacktestParams(
        capital=base.capital, degree=int(degree), min_curve_points=int(minpts),
        step_secs=int(step), force_eod=base.force_eod,
        include_matured=base.include_matured,
        buy_fee=base.buy_fee, sell_fee=base.sell_fee,
        signal_price=base.signal_price, curve_trim_bps=base.curve_trim_bps,
        min_dtm=int(mdtm))


def trades_old(dates, cache, base, combos) -> dict:
    streams: dict = {}
    out = {}
    for c in combos:
        gkey = (c[0], c[1], c[4], c[6])
        st = streams.get(gkey)
        if st is None:
            st = _build_decision_stream(dates, cache, _group_tmpl(base, gkey))
            streams[gkey] = st
        p = _combo_params(base, c)
        out[c] = [asdict(t) for t in _replay_stream(st, p)]
    return out


def trades_new(dates, cache, base, combos) -> dict:
    exec_mode = (base.signal_price == "exec")
    groups: dict = {}
    for c in combos:
        groups.setdefault((c[0], c[1], c[4], c[6]), []).append(c)
    out = {}
    for gkey, cs in sorted(groups.items()):
        stream = _build_decision_stream(dates, cache, _group_tmpl(base, gkey))
        gates = {(float(c[2]), float(c[5])) for c in cs}
        exits = {float(c[3]) for c in cs}
        tdays = _index_stream_triggers(stream, gates, exits, exec_mode)
        dord = {td["date"]: _date_ordinal(td["date"])
                for td in tdays if td is not None}
        for c in cs:
            p = _combo_params(base, c)
            out[c] = [asdict(t)
                      for t in _replay_triggers(tdays, stream, p, dord)]
    return out


def check_seed(seed: int, base: BondBacktestParams, full: bool) -> tuple[int, int]:
    rng = random.Random(seed * 7919)
    dates, cache = build_cache(seed)
    combos = pick_combos(rng, full)
    old = trades_old(dates, cache, base, combos)
    new = trades_new(dates, cache, base, combos)
    n_trades = mismatches = 0
    for c in combos:
        n_trades += len(old[c])
        if old[c] != new[c]:
            mismatches += 1
            if mismatches == 1:
                print(f"  ✗ MISMATCH combo={c}")
                for i, (a, b) in enumerate(zip(old[c], new[c])):
                    if a != b:
                        print(f"    trade #{i}:")
                        for k in a:
                            if a[k] != b[k]:
                                print(f"      {k}: old={a[k]!r} new={b[k]!r}")
                        break
                if len(old[c]) != len(new[c]):
                    print(f"    trade count: old={len(old[c])} new={len(new[c])}")
    # Third engine: _simulate_cache (the real-backtest path) vs _replay_stream
    # on a subsample — guards the hand-mirrored confirm / best-first blocks in
    # _simulate_bond_day against drift from the stream engines.
    sim_sample = combos[::max(1, len(combos) // 48)]
    for c in sim_sample:
        p = _combo_params(base, c)
        sim = [asdict(t) for t in _simulate_cache(dates, cache, p)[0]]
        if sim != old[c]:
            mismatches += 1
            print(f"  ✗ SIM-vs-STREAM MISMATCH combo={c} "
                  f"(sim {len(sim)} vs stream {len(old[c])} trades)")
    return len(combos), n_trades, mismatches


def check_worker_and_pool(seed: int, base: BondBacktestParams) -> None:
    """End-to-end: _c2f_optimize with n_jobs=1 vs n_jobs=2 on a reduced grid."""
    import bond_backtest as bb
    dates, cache = build_cache(seed)
    saved = bb.COARSE_GRID
    bb.COARSE_GRID = {
        "degree": [1, 2], "min_curve_points": [3],
        "entry_bps": [30, 65, 110], "exit_bps": [-10, 10],
        "step_secs": [0], "entry_max_bps": [120, 150],
        "min_dtm": [15, 30], "min_hold_days": [0, 1],
        "force_eod": [False, True], "min_exit_profit_bps": [-1.0],
        "exit_needs_replacement": [False, True],
        "entry_confirm_ticks": [0, 2], "entry_best_first": [False, True],
    }
    try:
        r1 = _c2f_optimize(dates, cache, base, "sharpe", 3, top_k=5, n_jobs=1)
        r2 = _c2f_optimize(dates, cache, base, "sharpe", 3, top_k=5, n_jobs=2)
        # Force the spawn path (what Windows uses) even where fork exists.
        os.environ["BOND_OPT_START_METHOD"] = "spawn"
        try:
            r3 = _c2f_optimize(dates, cache, base, "sharpe", 3, top_k=5, n_jobs=2)
        finally:
            del os.environ["BOND_OPT_START_METHOD"]
    finally:
        bb.COARSE_GRID = saved
    j1 = json.dumps([(r["params"], r["score"], r["summary"]) for r in r1],
                    sort_keys=True, ensure_ascii=False)
    j2 = json.dumps([(r["params"], r["score"], r["summary"]) for r in r2],
                    sort_keys=True, ensure_ascii=False)
    j3 = json.dumps([(r["params"], r["score"], r["summary"]) for r in r3],
                    sort_keys=True, ensure_ascii=False)
    assert j1 == j2, "n_jobs=1 vs n_jobs=2 (fork) results differ!"
    assert j1 == j3, "sequential vs spawn-pool results differ!"
    print(f"  ✓ pool parity: {len(r1)} combos identical across "
          f"sequential / fork / spawn")


def main() -> int:
    full = os.environ.get("PARITY_FULL") == "1"
    n_seeds = int(os.environ.get("PARITY_SEEDS", "3"))
    bases = [
        BondBacktestParams(signal_price="exec", total_capital=10_000_000_000.0,
                           max_position_pct=0.5, curve_trim_bps=150.0),
        BondBacktestParams(signal_price="mid", total_capital=10_000_000_000.0,
                           max_position_pct=1.0, curve_trim_bps=0.0),
        BondBacktestParams(signal_price="exec", total_capital=0.0,
                           capital=2_000_000_000.0, curve_trim_bps=150.0),
    ]
    t0 = time.time()
    total_combos = total_trades = total_mm = 0
    for seed in range(1, n_seeds + 1):
        base = bases[(seed - 1) % len(bases)]
        nc, nt, mm = check_seed(seed, base, full)
        total_combos += nc
        total_trades += nt
        total_mm += mm
        tag = "exec" if base.signal_price == "exec" else "mid "
        cap = "pool" if base.total_capital > 0 else "leg "
        print(f"  seed {seed} [{tag}/{cap}]: {nc} combos, {nt} trades, "
              f"{mm} mismatches")
    check_worker_and_pool(99, bases[0])
    dt = time.time() - t0
    print(f"\n{'='*60}")
    if total_mm == 0:
        print(f"PARITY OK — {total_combos} combos, {total_trades} trades, "
              f"0 mismatches  ({dt:.1f}s)")
        return 0
    print(f"PARITY FAILED — {total_mm} mismatching combos out of {total_combos}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
