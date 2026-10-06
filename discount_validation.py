"""Validation suite for the NAV-discount strategy.

Question it answers: when a backtest disappoints, is it because of
  (ب) the EXECUTION / COST model,
  (ج) the IDEA itself (the signal carries no information), or
  (الف) the PARAMETERS (over-fitting / fragile choice)?

Order matters — each step only makes sense if the previous ones pass:

  1. self_test          the simulator itself: known-answer worlds, accounting identity,
                        causality (no look-ahead) of the signal, baseline and bubble index
  2. audit_trades       gross edge vs spread+fees per trade; P&L split into
                        NAV drift (carry) / discount convergence / spread / fees
  3. info_tests         does the discount carry information? (a) AR(1) slope / half-life of
                        the bubble, (b) forward return after a signal vs the fund's own
                        average, by horizon — p-values from PERMUTATION nulls (see below)
  5. stress             spread ×2/×3, fees +50%, tighter volume cap
  6. param_surface      entry × exit grid; plateau vs spike
  7. walk_forward       fixed parameters per time block + expanding-window re-optimisation
  8. stability          by fund, by month, concentration, leave-top-trades-out
  9. verdict            traffic lights for ب / ج / الف and a one-line diagnosis

Nothing here changes the strategy engine; it only re-runs it.
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
import random
import statistics
import time
from dataclasses import asdict, replace

import discount_backtest as D
from discount_backtest import DiscountParams

CATEGORY_LABELS = D.CATEGORIES


# --------------------------------------------------------------------------- #
#  Small statistics helpers                                                    #
# --------------------------------------------------------------------------- #

def _rank(v: list[float]) -> list[float]:
    order = sorted(range(len(v)), key=lambda i: v[i])
    ranks = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 3:
        return 0.0
    mx, my = sum(x) / n, sum(y) / n
    sx = sum((a - mx) ** 2 for a in x)
    sy = sum((b - my) ** 2 for b in y)
    if sx <= 0 or sy <= 0:
        return 0.0
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sx * sy)


def _spearman(x: list[float], y: list[float]) -> float:
    return _pearson(_rank(x), _rank(y))


def _tstat(vals: list[float], n_eff: float | None = None) -> tuple[float, float]:
    """(mean, t) of a series; ``n_eff`` shrinks the sample for overlapping horizons."""
    n = len(vals)
    if n < 3:
        return (vals[0] if vals else 0.0), 0.0
    m = sum(vals) / n
    sd = statistics.pstdev(vals) * math.sqrt(n / (n - 1))
    ne = max(2.0, n_eff if n_eff is not None else n)
    if sd <= 0:
        return m, 0.0
    return m, m / (sd / math.sqrt(ne))


def _pctile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


# --------------------------------------------------------------------------- #
#  Universe loading (same data the backtest uses)                              #
# --------------------------------------------------------------------------- #

def _decorate(it: dict) -> None:
    rows = it["rows"]
    it["rel"] = [r[4] / r[3] - 1.0 for r in rows]
    it["dates"] = [r[1] for r in rows]
    it["keys"] = [(r[1], r[2]) for r in rows]
    it["fresh_idx"] = [i for i, r in enumerate(rows) if r[5]]


def load_universe(db, cats, symbols, start, end, p: DiscountParams) -> dict:
    funds = D._universe(db, cats, symbols)
    items = []
    for sid, label in funds:
        _raw, rows, day_vol = D._load(db, sid, start, end, p)
        if rows:
            items.append({"label": label, "rows": rows, "day_vol": day_vol})
    if p.entry_mode in ("index", "both") and items:
        idx_all, _curve, _share = D._bubble_index([it["rows"] for it in items], p)
        for it, ix in zip(items, idx_all):
            it["idx"] = ix
    for it in items:
        _decorate(it)
    dates = db.get_nav_intraday_dates(start, end)
    return {"items": items, "dates": dates,
            "d0": dates[0] if dates else (start or 0),
            "d1": dates[-1] if dates else (end or 0)}


def universe_from_rows(items: list[dict]) -> dict:
    """Wrap in-memory rows (used by the self-test) like load_universe does."""
    for it in items:
        _decorate(it)
    dates = sorted({d for it in items for d in it["dates"]})
    return {"items": items, "dates": dates, "d0": dates[0], "d1": dates[-1]}


def _sim_all(U: dict, p: DiscountParams, lo: int | None = None, hi: int | None = None,
             rng: random.Random | None = None) -> list:
    """Simulate every fund (optionally restricted to dates [lo, hi]).

    With ``rng`` given, the fund-level signal (and the index signal) is circularly
    shifted by a random offset — the placebo."""
    trades = []
    for it in U["items"]:
        a, b = 0, len(it["rows"])
        if lo is not None:
            a = bisect.bisect_left(it["dates"], lo)
        if hi is not None:
            b = bisect.bisect_right(it["dates"], hi)
        if b - a < 2:
            continue
        rows = it["rows"][a:b]
        rel = it["rel"][a:b]
        idx = it["idx"][a:b] if it.get("idx") is not None else None
        if rng is not None:
            n = len(rows)
            o = int(n * (0.1 + 0.8 * rng.random()))
            rel = rel[o:] + rel[:o]
            if idx is not None:
                o2 = int(n * (0.1 + 0.8 * rng.random()))
                idx = idx[o2:] + idx[:o2]
        trades.extend(D._simulate(it["label"], rows, it["day_vol"], p, idx, rel))
    return trades


def _portfolio(trades: list, p: DiscountParams, d0: int, d1: int):
    accepted, summary, _curve = D._portfolio_summary(trades, p, d0, d1)
    return accepted, summary


# --------------------------------------------------------------------------- #
#  2) cost audit + P&L attribution                                             #
# --------------------------------------------------------------------------- #

def audit_trades(trades: list[dict], p: DiscountParams) -> dict:
    """Weighted (by cost) split of the average trade into carry / convergence / costs."""
    if not trades:
        return {"n": 0}
    hs = p.half_spread_pct / 100.0
    spread_cost = 1.0 - (1.0 - hs) / (1.0 + hs)
    w_tot = gm_s = nav_s = conv_s = fee_s = net_s = 0.0
    beat = wins_gross = 0
    holds = []
    for t in trades:
        w = t["buy_notional"]
        if w <= 0 or t["entry_price"] <= 0 or t["nav_entry"] <= 0:
            continue
        entry_mid = t["entry_price"] / (1.0 + hs)
        exit_mid = t["exit_price"] / (1.0 - hs)
        gm = exit_mid / entry_mid - 1.0
        nav_chg = t["nav_exit"] / t["nav_entry"] - 1.0
        conv = (1.0 + gm) / (1.0 + nav_chg) - 1.0
        fee = t["fees"] / w
        net = t["net_pnl"] / w
        w_tot += w
        gm_s += w * gm
        nav_s += w * nav_chg
        conv_s += w * conv
        fee_s += w * fee
        net_s += w * net
        beat += 1 if gm > spread_cost + fee else 0
        wins_gross += 1 if gm > 0 else 0
        holds.append(t["hold_days"])
    n = len(holds)
    if not w_tot:
        return {"n": 0}
    gm, nav_c, conv = gm_s / w_tot, nav_s / w_tot, conv_s / w_tot
    fee, net = fee_s / w_tot, net_s / w_tot
    return {
        "n": n,
        "gross_mid_pct": round(gm * 100, 4),           # mid-to-mid price move of the average trade
        "nav_drift_pct": round(nav_c * 100, 4),        # the fund's own NAV change while held (carry)
        "convergence_pct": round(conv * 100, 4),       # change of price/NAV (the actual idea)
        "spread_cost_pct": round(spread_cost * 100, 4),
        "fee_pct": round(fee * 100, 4),
        "net_pct": round(net * 100, 4),
        "residual_pct": round((gm - spread_cost - fee - net) * 100, 4),
        "total_cost_pct": round((spread_cost + fee) * 100, 4),
        "share_gross_positive": round(wins_gross / n * 100, 1),
        "share_beating_costs": round(beat / n * 100, 1),
        "avg_hold_days": round(sum(holds) / n, 2),
        "carry_share_pct": round(nav_c / gm * 100, 1) if abs(gm) > 1e-9 else None,
    }


# --------------------------------------------------------------------------- #
#  3+4) is there information in the discount?  (permutation-null tests)       #
# --------------------------------------------------------------------------- #
#
# Why permutation (surrogate) nulls and not t-tests / placebo trades?  Each of the
# obvious shortcuts was measured on "no-information" worlds (bubble = pure random walk)
# and failed:
#   * signal-shift placebo / random-entry placebo : 13-29% "significant" at the 5% level
#   * same-day-control event study                : t > 2 in 100% of worlds (it conditions
#                                                   on the future rebound)
#   * ratio-type event study vs the fund's average: +0.19% bias (the number of signal rows
#                                                   itself depends on the future path)
# A statistic computed on the REAL data is therefore compared with the same statistic on
# many SURROGATE worlds in which each fund's sequence of bubble changes is randomly
# permuted (price = fair·(1+bubble) is rebuilt; same volatility, no mean reversion).
# Whatever bias the statistic has is then part of the null, so p-values are valid.
# Measured: P(p<=.05) = 4.3% in no-information worlds (nominal 5%); 100% in worlds with
# a mean-reverting bubble.
#
# Decisions are sampled once per day at the FIRST fresh snapshot (causal, cheap):
#   signal day  : bubble at the open <= −entry%  (index modes: the bubble index)
#   forward ret : open price -> end-of-day price h trading days later
# Statistics: (1) pooled within-fund AR(1) slope of the daily bubble (half-life), and
#             (2) event edge(h) = mean forward return after a signal − the fund's own
#                 mean forward return.

HORIZONS = (1, 3, 5, 10)


def _daily_arrays(U: dict) -> list[dict]:
    out = []
    for it in U["items"]:
        rows = it["rows"]
        first: dict[int, int] = {}
        lastr: dict[int, int] = {}
        for i, r in enumerate(rows):
            if r[5]:
                first.setdefault(r[1], i)
                lastr[r[1]] = i
        days = sorted(first)
        if len(days) < 12:
            continue
        z, fair = [], []
        for d in days:
            for i in (first[d], lastr[d]):
                z.append(it["rel"][i])
                fair.append(rows[i][3])
        out.append({"z": z, "fair": fair, "dates": days})
    return out


def _ar_slope(arrs: list[dict], zs: list[list[float]]) -> float:
    """Pooled within-fund slope of Δ(eod bubble) on the previous eod bubble."""
    sxy = sxx = 0.0
    for z in zs:
        eod = z[1::2]
        x = eod[:-1]
        y = [b - a for a, b in zip(eod, eod[1:])]
        n = len(x)
        mx, my = sum(x) / n, sum(y) / n
        for xi, yi in zip(x, y):
            sxy += (xi - mx) * (yi - my)
            sxx += (xi - mx) ** 2
    return sxy / sxx if sxx > 0 else 0.0


def _event_edges(arrs: list[dict], zs: list[list[float]], mode: str, thr_f: float, thr_i: float,
                 need: int) -> dict:
    idx_map = None
    if mode != "fund":
        acc: dict[int, tuple[float, int]] = {}
        for a, z in zip(arrs, zs):
            for j, d in enumerate(a["dates"]):
                s0, c0 = acc.get(d, (0.0, 0))
                acc[d] = (s0 + z[2 * j], c0 + 1)
        idx_map = {d: (s0 / c0 if c0 >= need else None) for d, (s0, c0) in acc.items()}
    tot = {h: [0.0, 0, 0.0, 0.0] for h in HORIZONS}      # [Σ n·edge, Σ n, Σ signal sums, Σ n·baseline]
    for a, z in zip(arrs, zs):
        fair, dates = a["fair"], a["dates"]
        nd = len(dates)
        po = [fair[2 * j] * (1.0 + z[2 * j]) for j in range(nd)]
        pe = [fair[2 * j + 1] * (1.0 + z[2 * j + 1]) for j in range(nd)]
        sig = []
        for j in range(nd):
            fund_ok = z[2 * j] <= thr_f
            ix = idx_map.get(dates[j]) if idx_map is not None else None
            idx_ok = ix is not None and ix <= thr_i
            sig.append(fund_ok if mode == "fund" else (idx_ok if mode == "index" else (fund_ok and idx_ok)))
        for h in HORIZONS:
            ss = bs = 0.0
            sn = bn = 0
            for j in range(nd - h):
                fr = pe[j + h] / po[j] - 1.0
                bs += fr
                bn += 1
                if sig[j]:
                    ss += fr
                    sn += 1
            if sn and bn:
                t = tot[h]
                t[0] += sn * (ss / sn - bs / bn)
                t[1] += sn
                t[2] += ss
                t[3] += sn * bs / bn
    out = {}
    for h, (e, n, ss, bsn) in tot.items():
        out[h] = (e / n, n, ss / n, bsn / n) if n else None
    return out


def info_tests(U: dict, p: DiscountParams, n_iter: int = 300, max_seconds: float = 40.0,
               progress=None, seed: int = 5) -> dict:
    arrs = _daily_arrays(U)
    if not arrs:
        return {"insufficient": True}
    mode = p.entry_mode if p.entry_mode in ("fund", "index", "both") else "fund"
    thr_f = -p.entry_discount_pct / 100.0
    thr_i = -p.index_entry_pct / 100.0
    need = max(1, int(-(-len(arrs) * p.index_min_share // 1)))
    hs = p.half_spread_pct / 100.0
    cost = 2.0 * hs + p.buy_fee + p.sell_fee
    zr = [a["z"] for a in arrs]
    real_b = _ar_slope(arrs, zr)
    real_e = _event_edges(arrs, zr, mode, thr_f, thr_i, need)
    rng = random.Random(seed)
    t0 = time.time()
    null_b: list[float] = []
    null_e: dict[int, list[float]] = {h: [] for h in HORIZONS}
    for it_no in range(n_iter):
        if time.time() - t0 > max_seconds and it_no >= 30:
            break
        zs = []
        for z in zr:
            inc = [b - a for a, b in zip(z, z[1:])]
            rng.shuffle(inc)
            cur, sz = z[0], [z[0]]
            for d in inc:
                cur += d
                sz.append(cur)
            zs.append(sz)
        null_b.append(_ar_slope(arrs, zs))
        ne = _event_edges(arrs, zs, mode, thr_f, thr_i, need)
        for h in HORIZONS:
            if ne[h] is not None:
                null_e[h].append(ne[h][0])
        if progress is not None:
            progress["done"], progress["total"] = it_no + 1, n_iter
    le = sum(1 for x in null_b if x <= real_b)
    hl = (math.log(2) / -math.log(1 + real_b)) if -1 < real_b < 0 else None
    mr = {"beta_per_day": round(real_b, 5), "half_life_days": round(hl, 2) if hl else None,
          "null_mean": round(sum(null_b) / len(null_b), 5),
          "p_value": round((1 + le) / (1 + len(null_b)), 4)}
    events = []
    for h in HORIZONS:
        r = real_e.get(h)
        if r is None or len(null_e[h]) < 20:
            events.append({"h": h, "insufficient": True})
            continue
        edge, n, ms, mb = r
        ge = sum(1 for x in null_e[h] if x >= edge)
        events.append({
            "h": h, "n_signal_days": n,
            "mean_signal_pct": round(ms * 100, 4), "mean_baseline_pct": round(mb * 100, 4),
            "edge_pct": round(edge * 100, 4),
            "null_edge_mean_pct": round(sum(null_e[h]) / len(null_e[h]) * 100, 4),
            "p_value": round((1 + ge) / (1 + len(null_e[h])), 4),
            "net_of_cost_pct": round((ms - cost) * 100, 4),
        })
    return {"mode": mode, "cost_pct": round(cost * 100, 4), "n_iter": len(null_b),
            "n_funds": len(arrs), "n_days": sum(len(a["dates"]) for a in arrs),
            "mean_reversion": mr, "events": events}


# --------------------------------------------------------------------------- #
#  5) stress                                                                   #
# --------------------------------------------------------------------------- #

def stress(U: dict, p: DiscountParams) -> list[dict]:
    scen = [
        ("پایه (فرض‌های فعلی)", p),
        ("اسپرد ×۲", replace(p, half_spread_pct=p.half_spread_pct * 2)),
        ("اسپرد ×۳", replace(p, half_spread_pct=p.half_spread_pct * 3)),
        ("کارمزد +۵۰٪", replace(p, buy_fee=p.buy_fee * 1.5, sell_fee=p.sell_fee * 1.5)),
        ("سقف حجم نصف" if p.participation_pct > 0 else "سقف حجم ۲٫۵٪ روز",
         replace(p, participation_pct=(p.participation_pct / 2 if p.participation_pct > 0 else 2.5))),
        ("اسپرد ×۲ و کارمزد +۵۰٪", replace(p, half_spread_pct=p.half_spread_pct * 2,
                                           buy_fee=p.buy_fee * 1.5, sell_fee=p.sell_fee * 1.5)),
    ]
    out = []
    for name, pp in scen:
        _a, s = _portfolio(_sim_all(U, pp), pp, U["d0"], U["d1"])
        out.append({"name": name, "return_pct": s["portfolio_return_pct"], "final_capital": s["final_capital"],
                    "trades": s["trade_count"], "win_rate": s["win_rate"],
                    "avg_trade_pct": s["avg_net_pct"] if s["trade_count"] else 0.0})
    return out


# --------------------------------------------------------------------------- #
#  6) parameter surface                                                        #
# --------------------------------------------------------------------------- #

def _surface_axes(p: DiscountParams):
    if p.entry_mode == "index":
        return ("index_entry_pct", [0.1, 0.2, 0.3, 0.5, 0.75, 1.0],
                "index_exit_pct", [0.3, 0.15, 0.0, -0.15])
    return ("entry_discount_pct", [0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0],
            "exit_discount_pct", [0.5, 0.25, 0.0, -0.25, -0.5])


def param_surface(U: dict, p: DiscountParams, progress=None) -> dict:
    ek, evals, xk, xvals = _surface_axes(p)
    cur_e, cur_x = getattr(p, ek), getattr(p, xk)
    # make sure the user's own setting is on the grid
    if all(abs(cur_e - v) > 1e-9 for v in evals):
        evals = sorted(evals + [cur_e])
    if all(abs(cur_x - v) > 1e-9 for v in xvals):
        xvals = sorted(xvals + [cur_x], reverse=True)
    cells: dict[tuple[int, int], dict | None] = {}
    total = len(evals) * len(xvals)
    done = 0
    for i, ev in enumerate(evals):
        for j, xv in enumerate(xvals):
            done += 1
            if progress is not None:
                progress["done"], progress["total"] = done, total
            if xv >= ev - 0.02:
                cells[(i, j)] = None
                continue
            pp = replace(p, **{ek: ev, xk: xv})
            _a, s = _portfolio(_sim_all(U, pp), pp, U["d0"], U["d1"])
            cells[(i, j)] = {"ret": s["portfolio_return_pct"], "trades": s["trade_count"], "win": s["win_rate"]}
    valid = {k: v for k, v in cells.items() if v is not None and v["trades"] > 0}
    ci = next(i for i, v in enumerate(evals) if abs(v - cur_e) < 1e-9)
    cj = next(j for j, v in enumerate(xvals) if abs(v - cur_x) < 1e-9)
    cur = cells.get((ci, cj))
    neigh = [valid[(ci + di, cj + dj)]["ret"] for di in (-1, 0, 1) for dj in (-1, 0, 1)
             if (di or dj) and (ci + di, cj + dj) in valid]
    rets = sorted(v["ret"] for v in valid.values())
    out = {
        "entry_key": ek, "exit_key": xk, "entry_vals": evals, "exit_vals": xvals,
        "grid": [[cells.get((i, j)) for j in range(len(xvals))] for i in range(len(evals))],
        "current": {"i": ci, "j": cj},
        "n_cells": len(valid),
    }
    if valid:
        out.update({
            "share_positive_pct": round(sum(1 for v in rets if v > 0) / len(rets) * 100, 1),
            "median_pct": round(statistics.median(rets), 3),
            "best_pct": round(rets[-1], 3), "worst_pct": round(rets[0], 3),
            "current_pct": cur["ret"] if cur and cur["trades"] > 0 else None,
            "neighbors_mean_pct": round(sum(neigh) / len(neigh), 3) if neigh else None,
            "neighbors_positive_pct": round(sum(1 for v in neigh if v > 0) / len(neigh) * 100, 1) if neigh else None,
        })
    return out


# --------------------------------------------------------------------------- #
#  7) walk-forward                                                             #
# --------------------------------------------------------------------------- #

def walk_forward(U: dict, p: DiscountParams, blocks: int = 4, progress=None) -> dict:
    dates = U["dates"]
    if len(dates) < blocks * 5:
        return {"insufficient": True}
    edges = [dates[int(len(dates) * k / blocks)] for k in range(blocks)] + [dates[-1]]
    ek, evals, xk, xvals = _surface_axes(p)
    cur_e, cur_x = getattr(p, ek), getattr(p, xk)
    e_grid = sorted({round(cur_e * f, 3) for f in (0.5, 1.0, 1.5)})
    x_grid = sorted({round(cur_x + dx, 3) for dx in (-0.25, 0.0, 0.25)}, reverse=True)
    rows = []
    for k in range(blocks):
        lo = edges[k]
        hi = edges[k + 1] if k == blocks - 1 else dates[bisect.bisect_left(dates, edges[k + 1]) - 1]
        _a, s = _portfolio(_sim_all(U, p, lo, hi), p, lo, hi)
        row = {"block": k + 1, "from": lo, "to": hi, "trades": s["trade_count"],
               "return_pct": s["portfolio_return_pct"], "win_rate": s["win_rate"],
               "wf_params": None, "wf_oos_return_pct": None, "wf_oos_trades": None}
        if k >= 1:                                   # expanding window: choose on blocks < k, test on k
            train_hi = dates[bisect.bisect_left(dates, edges[k]) - 1]     # last day BEFORE block k
            best, best_ret = None, -1e18
            for ev in e_grid:
                for xv in x_grid:
                    if xv >= ev - 0.02:
                        continue
                    pp = replace(p, **{ek: ev, xk: xv})
                    _a2, st = _portfolio(_sim_all(U, pp, None, train_hi), pp, dates[0], train_hi)
                    if st["trade_count"] >= 5 and st["portfolio_return_pct"] > best_ret:
                        best, best_ret = (ev, xv), st["portfolio_return_pct"]
            if best is not None:
                pp = replace(p, **{ek: best[0], xk: best[1]})
                _a3, so = _portfolio(_sim_all(U, pp, lo, hi), pp, lo, hi)
                row.update({"wf_params": {ek: best[0], xk: best[1]},
                            "wf_train_return_pct": round(best_ret, 3),
                            "wf_oos_return_pct": so["portfolio_return_pct"], "wf_oos_trades": so["trade_count"]})
        rows.append(row)
        if progress is not None:
            progress["done"], progress["total"] = k + 1, blocks
    fixed = [r["return_pct"] for r in rows if r["trades"] > 0]
    oos = [r["wf_oos_return_pct"] for r in rows if r["wf_oos_return_pct"] is not None]
    return {
        "blocks": rows, "entry_key": ek, "exit_key": xk,
        "fixed_mean_pct": round(sum(fixed) / len(fixed), 3) if fixed else None,
        "fixed_positive_blocks": sum(1 for x in fixed if x > 0), "fixed_blocks": len(fixed),
        "oos_mean_pct": round(sum(oos) / len(oos), 3) if oos else None,
        "oos_positive_blocks": sum(1 for x in oos if x > 0), "oos_blocks": len(oos),
    }


# --------------------------------------------------------------------------- #
#  8) stability                                                                #
# --------------------------------------------------------------------------- #

def _month_key(d: int) -> str:
    y, m, dd = d // 10000, d // 100 % 100, d % 100
    try:
        import jdatetime
        j = jdatetime.date.fromgregorian(year=y, month=m, day=dd)
        return f"{j.year}/{j.month:02d}"
    except Exception:                                  # noqa: BLE001
        return f"{y}-{m:02d}"


def stability(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    pnl = [t["net_pnl"] for t in trades]
    total = sum(pnl)
    by_fund: dict[str, float] = {}
    by_month: dict[str, float] = {}
    for t in trades:
        by_fund[t["symbol"]] = by_fund.get(t["symbol"], 0.0) + t["net_pnl"]
        by_month[_month_key(t["exit_date"])] = by_month.get(_month_key(t["exit_date"]), 0.0) + t["net_pnl"]
    funds_sorted = sorted(by_fund.items(), key=lambda kv: -kv[1])
    pos_total = sum(v for v in by_fund.values() if v > 0)
    top3_share = (sum(v for _, v in funds_sorted[:3] if v > 0) / pos_total * 100) if pos_total > 0 else None
    ordered = sorted(trades, key=lambda t: (t["exit_date"], t["exit_time"]))
    half = len(ordered) // 2
    first_half = sum(t["net_pnl"] for t in ordered[:half])
    second_half = sum(t["net_pnl"] for t in ordered[half:])
    best3 = sorted(pnl, reverse=True)[:3]
    months = sorted(by_month.items())
    return {
        "n": len(trades), "total_pnl": round(total, 0),
        "funds": len(by_fund), "funds_profitable": sum(1 for v in by_fund.values() if v > 0),
        "top3_funds_share_pct": round(top3_share, 1) if top3_share is not None else None,
        "top_funds": [[k, round(v, 0)] for k, v in funds_sorted[:5]],
        "worst_funds": [[k, round(v, 0)] for k, v in funds_sorted[-3:]],
        "months": [[k, round(v, 0)] for k, v in months],
        "months_profitable": sum(1 for _, v in months if v > 0),
        "first_half_pnl": round(first_half, 0), "second_half_pnl": round(second_half, 0),
        "pnl_without_best3": round(total - sum(best3), 0),
        "best3_share_pct": round(sum(best3) / total * 100, 1) if total > 0 else None,
    }


# --------------------------------------------------------------------------- #
#  1) self-test of the simulator                                               #
# --------------------------------------------------------------------------- #

def _synthetic_rows(kind: str, n_days: int = 260, per_day: int = 8, seed: int = 3) -> list[tuple]:
    """rows in the engine's format. kind: 'revert' (bubble mean-reverts), 'noise' (random walk)."""
    rng = random.Random(seed)
    rows = []
    rel = 0.0
    nav = 10_000.0
    for di in range(n_days):
        nav *= 1.0004
        d = int((dt.date(2025, 1, 1) + dt.timedelta(days=di)).strftime("%Y%m%d"))
        for k in range(per_day):
            if kind == "revert":
                rel = 0.85 * rel + rng.gauss(0, 0.008)
            else:
                rel += rng.gauss(0, 0.003)               # pure random walk: nothing to revert to
            rows.append((D._ord(d), d, 91500 + k * 3000, nav, nav * (1 + rel), True, nav))
    return rows


def self_test() -> dict:
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    p = DiscountParams(initial_capital=1e10, position_pct=20, participation_pct=0,
                       half_spread_pct=0.02, entry_discount_pct=0.8, exit_discount_pct=0.0,
                       max_hold_days=10, baseline_days=0)
    # --- known-answer worlds ------------------------------------------------
    U = universe_from_rows([{"label": f"W-revert-{i}", "rows": _synthetic_rows("revert", seed=3 + i),
                             "day_vol": {}} for i in range(4)])
    tr = _sim_all(U, p)
    acc, s_ = _portfolio(tr, p, U["d0"], U["d1"])
    add("دنیای «حباب بازگشت‌پذیر»: استراتژی باید سودده باشد", s_["portfolio_return_pct"] > 0,
        f"{s_['trade_count']} معامله، بازده {s_['portfolio_return_pct']}٪")
    net = sum(t.net_pnl for t in acc)
    add("اتحاد حسابداری: سرمایهٔ نهایی − اولیه = جمع سود معامله‌ها",
        abs((s_["final_capital"] - p.initial_capital) - net) < max(50, 1e-6 * p.initial_capital),
        f"اختلاف {round((s_['final_capital'] - p.initial_capital) - net, 1)} ریال (گردکردن)")
    it_ = info_tests(U, p, n_iter=99, max_seconds=15)
    add("آزمون بازگشت حباب: در دنیای بازگشت‌پذیر باید معنادار باشد (p ≤ 0.05)",
        not it_.get("insufficient") and it_["mean_reversion"]["p_value"] <= 0.05,
        f"p = {it_.get('mean_reversion', {}).get('p_value')}")

    # null worlds: random-walk bubble. Over many worlds the tests must NOT be "significant"
    # more often than chance, and the strategy must not systematically make money.
    pv_mr, pv_ev, rets = [], [], []
    for sd in range(24):
        Un = universe_from_rows([{"label": f"W-noise-{i}", "rows": _synthetic_rows("noise", n_days=200,
                                  seed=1000 + sd * 7 + i), "day_vol": {}} for i in range(4)])
        _a, sn = _portfolio(_sim_all(Un, p), p, Un["d0"], Un["d1"])
        rets.append(sn["portfolio_return_pct"])
        r = info_tests(Un, p, n_iter=60, max_seconds=5, seed=sd)
        if r.get("insufficient"):
            continue
        pv_mr.append(r["mean_reversion"]["p_value"])
        e3 = next((e for e in r["events"] if e["h"] == 3 and not e.get("insufficient")), None)
        if e3:
            pv_ev.append(e3["p_value"])
    share_mr = sum(1 for x in pv_mr if x <= 0.10) / max(1, len(pv_mr))
    share_ev = sum(1 for x in pv_ev if x <= 0.10) / max(1, len(pv_ev))
    mean_ret = sum(rets) / len(rets)
    add("دنیای «نویز خالص»: میانگین بازده استراتژی نباید مثبتِ معنادار باشد", mean_ret < 2.0,
        f"میانگین بازده روی {len(rets)} دنیا: {round(mean_ret, 2)}٪")
    add("کالیبراسیون: آزمون بازگشت در دنیای نویز بیش از حدِ تصادف «معنادار» نمی‌گوید (انتظار ≈ ۱۰٪ در سطح ۰٫۱)",
        share_mr <= 0.30, f"{round(share_mr * 100)}٪ از {len(pv_mr)} دنیا")
    add("کالیبراسیون: آزمون مزیت رویداد (افق ۳ روز) هم بیش از حدِ تصادف «معنادار» نمی‌گوید",
        share_ev <= 0.35, f"{round(share_ev * 100)}٪ از {len(pv_ev)} دنیا")

    # --- causality (no look-ahead) -----------------------------------------
    rows = _synthetic_rows("revert", seed=9)
    cut = int(len(rows) * 0.6)
    pc = replace(p, baseline_days=0)
    full = D._simulate("x", rows, {}, pc)
    part = D._simulate("x", rows[:cut], {}, pc)
    key = lambda t: (t.entry_date, t.entry_time, t.entry_price, t.exit_date, t.exit_time, t.exit_reason)   # noqa: E731
    full_keys = {key(t) for t in full}
    ok = all(key(t) in full_keys for t in part if t.exit_reason != "end")
    add("سیگنال علّی است: کوتاه‌کردن دادهٔ آینده معاملات گذشته را عوض نمی‌کند", ok,
        f"{sum(1 for t in part if t.exit_reason != 'end')} معاملهٔ بسته‌شده با دادهٔ ناقص همگی در اجرای کامل هم هست")

    # baseline (permanent bubble) causality: rows built from a prefix of the raw data
    # must equal the prefix of the rows built from all of it
    raw = []
    nav = 10_000.0
    rng = random.Random(5)
    for di in range(120):
        d = int((dt.date(2025, 1, 1) + dt.timedelta(days=di)).strftime("%Y%m%d"))
        nav *= 1.0003
        for k in range(8):
            raw.append((d, 91500 + k * 3000, nav, d, nav * (1 + 0.02 + rng.gauss(0, 0.004)), 1000 * (k + 1)))
    pb = DiscountParams(baseline_days=15, max_nav_age_days=3)
    rows_full, _ = D._prep(raw, pb)
    cut_day = raw[len(raw) * 6 // 10][0]
    rows_part, _ = D._prep([r for r in raw if r[0] <= cut_day], pb)
    same = [r for r in rows_full if r[1] <= cut_day] == rows_part
    add("تعدیل حباب دائمی علّی است: فقط روزهای قبل را می‌بیند", same, f"{len(rows_part)} ردیف یکسان")

    # bubble-index causality + hand check
    mk = lambda rel: [(D._ord(20250101 + i), 20250101 + i, 100000, 100.0, 100.0 * (1 + r), True, 100.0)   # noqa: E731
                      for i, r in enumerate(rel)]
    a, b = mk([0.01, -0.02, 0.0, 0.01]), mk([-0.01, -0.02, 0.02, 0.0])
    idx, _c, _s = D._bubble_index([a, b], DiscountParams(index_min_share=0.5))
    # same timestamp per day -> index = simple mean of the two funds' bubbles that instant
    hand = [(0.01 + -0.01) / 2, (-0.02 + -0.02) / 2, (0.0 + 0.02) / 2, (0.01 + 0.0) / 2]
    ok = all(abs(x - y) < 1e-9 for x, y in zip(idx[0], hand))
    add("شاخص حباب = میانگین ساده حباب صندوق‌ها (حساب دستی)", ok, f"{[round(v, 4) for v in idx[0]]}")
    t1 = D._bubble_index([a[:2], b[:2]], DiscountParams(index_min_share=0.5))[0]
    add("شاخص حباب علّی است", idx[0][:2] == t1[0] and idx[1][:2] == t1[1], "پیشوند یکسان")
    return {"checks": checks, "passed": sum(1 for c in checks if c["ok"]), "total": len(checks)}


# --------------------------------------------------------------------------- #
#  9) verdict                                                                  #
# --------------------------------------------------------------------------- #

def _level(ok: bool | None, warn: bool = False) -> str:
    if ok is None:
        return "na"
    return "ok" if ok else ("warn" if warn else "bad")


def verdict(res: dict, p: DiscountParams) -> dict:
    s = res["summary"]
    aud = res.get("audit", {})
    st = res.get("stress", [])
    sf = res.get("surface", {})
    wf = res.get("walk_forward", {})
    findings = []

    # ---- (ب) execution / cost ------------------------------------------------
    cost_level, cost_txt = "na", "معامله‌ای برای حسابرسی نیست."
    if aud.get("n"):
        g, c = aud["gross_mid_pct"], aud["total_cost_pct"]
        if g <= c:
            cost_level = "bad"
            cost_txt = (f"سود ناخالصِ میانگین معامله ({g}٪) از هزینهٔ رفت‌وبرگشت ({c}٪: اسپرد + کارمزد) کمتر است؛ "
                        "حتی با سیگنال درست، هزینه کل سود را می‌خورد.")
        elif g <= 2 * c:
            cost_level = "warn"
            cost_txt = (f"سود ناخالص ({g}٪) فقط {round(g / c, 1)} برابر هزینه ({c}٪) است؛ نتیجه به فرض اسپرد حساس است.")
        else:
            cost_level = "ok"
            cost_txt = f"سود ناخالص ({g}٪) چند برابر هزینه ({c}٪) است."
    base_r = st[0]["return_pct"] if st else None
    x2 = next((x for x in st if x["name"] == "اسپرد ×۲"), None)
    if x2 is not None and base_r is not None and base_r > 0:
        if x2["return_pct"] <= 0:
            cost_level = "bad" if cost_level != "bad" else cost_level
            cost_txt += f" با اسپرد دو برابر بازده به {x2['return_pct']}٪ می‌رسد (سودِ فعلی به فرض خوشبینانهٔ اسپرد وابسته است)."
        elif x2["return_pct"] < 0.4 * base_r and cost_level == "ok":
            cost_level = "warn"
            cost_txt += f" با اسپرد دو برابر بازده از {base_r}٪ به {x2['return_pct']}٪ می‌افتد."
    findings.append({"area": "ب", "title": "اجرا و هزینه", "level": cost_level, "text": cost_txt})

    # ---- (ج) signal / idea ----------------------------------------------------
    inf = res.get("info", {})
    mr = inf.get("mean_reversion")
    evs = [e for e in inf.get("events", []) if not e.get("insufficient")]
    hold = aud.get("avg_hold_days")
    score_of = {"ok": 0, "warn": 1, "bad": 2}
    levels, parts = [], []
    if mr:
        pv = mr["p_value"]
        lv = "ok" if pv <= 0.05 else ("warn" if pv <= 0.15 else "bad")
        levels.append(lv)
        hl = mr.get("half_life_days")
        parts.append(f"آزمون بازگشت حباب: شیب AR(1) روزانه {mr['beta_per_day']}"
                     + (f"، نیمه‌عمر ≈ {hl} روز" if hl else "") + f"، p = {pv}")
        if hl and hold and hl > 2.5 * max(hold, 1):
            parts.append(f"بازگشت (نیمه‌عمر {hl} روز) بسیار کندتر از مدت نگه‌داری ({hold} روز) است")
            levels.append("warn")
    prim = None
    if evs:
        prim = min(evs, key=lambda e: abs(e["h"] - max(hold or 1, 1)))
        pv = prim["p_value"]
        lv = "ok" if (pv <= 0.05 and prim["edge_pct"] > 0) else ("warn" if pv <= 0.15 and prim["edge_pct"] > 0 else "bad")
        levels.append(lv)
        parts.append(f"مطالعهٔ رویداد (افق {prim['h']} روز، نزدیک‌ترین به نگه‌داری): مزیت نسبت به میانگین صندوق "
                     f"{prim['edge_pct']}٪، p = {pv}")
        if lv != "bad" and prim["net_of_cost_pct"] <= 0:
            parts.append(f"ولی بازده آینده پس از سیگنال ({prim['mean_signal_pct']}٪) از هزینه ({inf['cost_pct']}٪) کمتر است")
    if aud.get("carry_share_pct") is not None and aud["gross_mid_pct"] > 0 and aud["carry_share_pct"] > 60:
        parts.append(f"{aud['carry_share_pct']}٪ سود ناخالص از رشد خودِ NAV است (نه همگرایی تخفیف)")
        levels.append("warn")
    if levels:
        sc = sum(score_of[x] for x in levels) / len(levels)
        sig_level = "ok" if sc < 0.5 else ("warn" if sc < 1.5 else "bad")
    else:
        sig_level = "na"
    if sig_level == "bad":
        parts.append("شواهد کافی نیست که «زیر NAV بودن» اطلاعاتی دربارهٔ بازده آینده بدهد.")
    findings.append({"area": "ج", "title": "ایده / سیگنال", "level": sig_level,
                     "text": "؛ ".join(parts) or "داده برای قضاوت کافی نیست."})

    # ---- (الف) parameters ----------------------------------------------------
    par_level, ptxt = "na", []
    if sf.get("n_cells"):
        sp, npos = sf.get("share_positive_pct"), sf.get("neighbors_positive_pct")
        ptxt.append(f"{sp}٪ از خانه‌های شبکهٔ پارامتر سودده است")
        par_level = "ok" if sp >= 60 else ("warn" if sp >= 35 else "bad")
        cur, nm = sf.get("current_pct"), sf.get("neighbors_mean_pct")
        if cur is not None and nm is not None and cur > 0 and nm < 0.4 * cur:
            par_level = "bad" if par_level != "bad" else par_level
            ptxt.append(f"تنظیم فعلی ({cur}٪) یک «قلهٔ تیز» است؛ میانگین همسایه‌ها {nm}٪")
        elif npos is not None:
            ptxt.append(f"{npos}٪ از همسایه‌های تنظیم فعلی سودده‌اند")
    if wf and not wf.get("insufficient") and wf.get("fixed_blocks"):
        ptxt.append(f"{wf['fixed_positive_blocks']} از {wf['fixed_blocks']} بلوک زمانی با پارامتر ثابت سودده است")
        if wf["fixed_positive_blocks"] / wf["fixed_blocks"] < 0.5 and par_level == "ok":
            par_level = "warn"
        if wf.get("oos_mean_pct") is not None:
            ptxt.append(f"بازده خارج از نمونهٔ walk-forward: میانگین {wf['oos_mean_pct']}٪")
            if wf["oos_mean_pct"] <= 0 and par_level in ("ok", "warn"):
                par_level = "bad" if wf.get("oos_blocks", 0) >= 2 else par_level
    findings.append({"area": "الف", "title": "پارامترها", "level": par_level,
                     "text": "؛ ".join(ptxt) or "داده برای قضاوت کافی نیست."})

    # ---- one-line diagnosis ----------------------------------------------------
    lv = {f["area"]: f["level"] for f in findings}
    net = s.get("portfolio_return_pct", 0)
    if lv["ج"] == "bad":
        head, prim = ("شواهد نشان می‌دهد خودِ ایده/سیگنال اطلاعات کافی ندارد (ج). پارامتر یا اجرا آن را نجات نمی‌دهد؛ "
                      "اول سیگنال را عوض کنید."), "ج"
    elif lv["ب"] == "bad" and lv["ج"] in ("ok", "warn"):
        head, prim = ("سیگنال اطلاعات دارد ولی هزینه/اجرا مزیت را می‌خورد (ب). افق نگه‌داری را بلندتر کنید، "
                      "آستانهٔ ورود را عمیق‌تر کنید یا اسپرد/کارمزد واقعی را بسنجید."), "ب"
    elif lv["الف"] == "bad" and lv["ج"] in ("ok", "warn"):
        head, prim = ("سیگنال معتبر است ولی نتیجه به پارامتر خاصی وابسته است (الف): احتمال بیش‌برازش. "
                      "ناحیهٔ هموار شبکه را انتخاب کنید و walk-forward را ملاک بگیرید."), "الف"
    elif all(v in ("ok", "na") for v in lv.values()) and net > 0:
        head, prim = ("هر سه آزمون قابل‌قبول است: ایده، اجرا و پارامتر پایدار به نظر می‌رسند. "
                      "هنوز فرض اسپرد (بدون اردربوک) حد بالای نتیجه است."), "ok"
    else:
        head, prim = ("نتیجه ترکیبی است؛ موارد زردرنگ را جداگانه بررسی کنید.", "mixed")
    return {"headline": head, "primary": prim, "findings": findings}


# --------------------------------------------------------------------------- #
#  Orchestrator                                                                #
# --------------------------------------------------------------------------- #

def run_validation(db, cats, symbols, start, end, p: DiscountParams, n_perm: int = 300,
                   max_seconds: float = 60.0, progress: dict | None = None) -> dict:
    prog = progress if progress is not None else {}

    def phase(name):
        prog.clear()
        prog.update(phase=name, done=0, total=0)

    phase("بارگذاری داده")
    U = load_universe(db, cats, symbols, start, end, p)
    if not U["items"]:
        return {"error": "برای صندوق‌های انتخاب‌شده داده‌ای در این بازه نیست."}
    phase("بک‌تست پایه")
    raw = _sim_all(U, p)
    accepted, summary = _portfolio(raw, p, U["d0"], U["d1"])
    trades = [asdict(t) for t in accepted]
    res = {"funds": len(U["items"]), "period": [U["d0"], U["d1"]], "summary": summary,
           "params": asdict(p)}
    phase("حسابرسی هزینه و تفکیک سود")
    res["audit"] = audit_trades(trades, p)
    phase("آزمون اطلاعات سیگنال (جایگشت)")
    res["info"] = info_tests(U, p, n_iter=n_perm, max_seconds=max_seconds, progress=prog)
    phase("سخت‌گیری اجرا")
    res["stress"] = stress(U, p)
    phase("سطح پارامتر")
    res["surface"] = param_surface(U, p, prog)
    phase("walk-forward")
    res["walk_forward"] = walk_forward(U, p, 4, prog)
    phase("پایداری")
    res["stability"] = stability(trades)
    res["verdict"] = verdict(res, p)
    phase("پایان")
    return res
