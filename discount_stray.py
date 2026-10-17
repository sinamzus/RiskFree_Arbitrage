"""Stray orders: how often does the order book show a fund far cheaper (or dearer) than it should be, how long does
such an order stay, and would catching it pay?

The backtest looks at the market only on its time grid (every 10 minutes by default), so an order that appears at
09:03 and is gone at 09:04 never reaches it. The collected TSE data is not sampled, though: it holds every change of
the top-5 book and every trade, to the second. This study replays that stream second by second.

Reference ("fair") price of a fund at an instant: the NAV known then (from the NAV feed, only when current — its
``nav_time`` at most ``max_nav_age_min`` old) times one plus the fund's own norm (median daily bubble of its previous
``norm_days`` trading days; 0 = no norm, the NAV itself). A fund that always trades 1 % under its NAV is not a
bargain at −1 %.

An episode, per side and threshold x %:
  * cheap ask — the best ask is at least x % under fair (a chance to BUY cheap);
  * rich bid  — the best bid is at least x % over fair (a chance to SELL dear, if the fund is held).
It opens at the first second the condition holds and closes at the first second it no longer does (or at the end of
continuous trading). Evaluated once per second, after every book change of that second (no half-applied updates).

Per episode: lifetime; how it ended (a trade at or through the threshold = someone took it; otherwise cancelled);
and, for each reaction delay L (seconds), whether it was still there at start + L and what could be taken then —
the levels beyond the threshold in the book AT THAT SECOND, up to ``cap_rial``. Its value:
  * paper edge: the gap to fair, minus both fees (buy now / sell later, or the reverse);
  * realised: what really happened afterwards — the fund's bubble 1 hour and 1 trading day later at the price one
    could close at (the best bid after buying a cheap ask, the best ask after selling a rich bid), minus both fees.
The realised figure is the honest one: a "stray" order may be informed (the NAV is about to move).

Limits: only the top 5 levels are visible (a stray order is at the top by definition); who took an order is unknown,
so a capture at delay L assumes the order was still there for us — the delays are the safeguard: an order that
survives 10 s was not taken by faster participants in those 10 s.
"""
from __future__ import annotations

import bisect
import math
import statistics
import time

import discount_backtest as D
import nav_feed as NF
import tse_dataset as TD
import tse_gold as G

THRESHOLDS = (0.5, 1.0, 2.0, 3.0)
LATENCIES = (0, 2, 5, 10, 30, 60)
LIFE_BUCKETS = ((0, 0, "۰ ثانیه"), (1, 4, "۱–۴ ثانیه"), (5, 9, "۵–۹ ثانیه"), (10, 29, "۱۰–۲۹ ثانیه"),
                (30, 59, "۳۰–۵۹ ثانیه"), (60, 299, "۱–۵ دقیقه"), (300, 10 ** 9, "بیش از ۵ دقیقه"))
SIDES = (("ask", "فروشندهٔ ارزان (فرصت خرید)"), ("bid", "خریدار گران (فرصت فروش)"))


def _sec(t: int) -> int:
    return (t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100


def _hms(s: int) -> int:
    return (s // 3600) * 10000 + (s // 60 % 60) * 100 + s % 60


def _tstat(v: list[float]):
    if len(v) < 3:
        return None
    sd = statistics.pstdev(v)
    return statistics.mean(v) / sd * math.sqrt(len(v)) if sd > 0 else None


def _take(levels: list, cap_rial: float) -> tuple[float, float]:
    """Walk the given levels (best first) up to cap_rial: (units, average price)."""
    units, cost = 0.0, 0.0
    for p, v in levels:
        if cost >= cap_rial:
            break
        q = min(float(v), (cap_rial - cost) / p)
        units += q
        cost += q * p
    return units, (cost / units if units > 0 else 0.0)


STRAY_VERSION = 2          # bump when an episode's fields or meaning change (invalidates the per-fund cache)


class _Nav:
    """The NAV known at an instant, only when current (its nav_time at most max_age old)."""

    def __init__(self, db, sid: int, max_age_s: int):
        ver = NF.version(db, sid)
        self.ok = ver is not None
        self.rep, self.best = (NF._load(db, sid, ver)[1:] if self.ok else ([], []))
        self.nts = [NF._ts(nd, nt) if nd else None for _n, nd, nt in self.best]
        self.max_age = max_age_s

    def at(self, d: int, s: int):
        ts = NF._ts(d, _hms(s))
        j = bisect.bisect_right(self.rep, ts) - 1
        if j < 0 or self.nts[j] is None or ts - self.nts[j] > self.max_age:
            return None
        return self.best[j][0]

    def day(self, d: int):
        """A forward-only reader for one day (seconds must not decrease): no search per second."""
        base = NF._ord(d) * 86400
        rep, nts, best, age = self.rep, self.nts, self.best, self.max_age
        st = [bisect.bisect_right(rep, base) - 1]

        def at(s: int):
            ts = base + s
            j = st[0]
            while j + 1 < len(rep) and rep[j + 1] <= ts:
                j += 1
            st[0] = j
            if j < 0 or nts[j] is None or ts - nts[j] > age:
                return None
            return best[j][0]
        return at


def _fund_study(db, sid: int, lab: str, start, end, cfg: dict) -> dict | None:
    """Every episode of one fund (compact, JSON-safe), a sample of its relative half spreads and its days."""
    thresholds, latencies = cfg["thresholds"], cfg["latencies"]
    ref_latency, cap, fees, norm_days = cfg["ref_latency"], cfg["cap"], cfg["fees"], cfg["norm_days"]
    x_min = min(thresholds)
    days = TD.tse_days(db, sid, start, end)
    nv = _Nav(db, sid, cfg["max_age"])
    if not days or not nv.ok:
        return None
    spreads, used_days, series = [], [], {}
    trades = {d: sorted((_sec(r[1]), float(r[2]), float(r[3])) for r in (G.load_day(db, sid, d, "trades") or [])
                        if not r[4] and r[2] > 0 and r[3] > 0) for d in days}
    # the fund's own norm: median of its daily median bubble (trades at a current NAV) over the previous days
    med = {}
    for d in days:
        at = nv.day(d)
        bs = [px / n - 1.0 for s, px, _v in trades[d] for n in (at(s),) if n]
        if bs:
            med[d] = statistics.median(bs)
    mdays = sorted(med)
    norm = {}
    for j, d in enumerate(mdays):
        if norm_days <= 0:
            norm[d] = 0.0
            continue
        w = [med[x] for x in mdays[max(0, j - norm_days):j]]
        if len(w) >= max(3, norm_days // 3):
            norm[d] = statistics.median(w)
    fund_eps = []
    for d in days:
        if d not in norm or not trades[d]:
            continue
        win = TD.market_window(db, d)
        if not win:
            continue
        used_days.append(d)
        w0, w1 = win
        tr_t = [x[0] for x in trades[d]]
        ev = sorted(G.load_day(db, sid, d, "book") or [], key=lambda r: (r[0], r[2]))
        levels: dict[int, tuple] = {}
        open_ = {}                              # (side, x) -> episode being built
        nav_at = nv.day(d)
        nd_ = norm[d]
        last_s = None
        mins: dict[int, list] = {}

        def close(key, s_end, how=None):
            e = open_.pop(key)
            e["end"] = s_end
            e["life"] = s_end - e["start"]
            side = key[0]
            lo, hi = bisect.bisect_left(tr_t, e["start"]), bisect.bisect_right(tr_t, s_end)
            thr = e["thr"]
            got = sum(v for _s, px, v in trades[d][lo:hi] if (px <= thr if side == "ask" else px >= thr))
            e["taken_units"] = got
            e["ended"] = how or ("taken" if got > 0 else "cancelled")
            fund_eps.append(e)

        def evaluate(s):
            # fast path: the best quotes alone decide whether anything is near a threshold
            ba = bb = 0.0
            for bp, bv, ap, av in levels.values():
                if ap > 0 and av > 0 and (ba == 0.0 or ap < ba):
                    ba = ap
                if bp > 0 and bv > 0 and bp > bb:
                    bb = bp
            if ba <= 0 or bb <= 0 or ba <= bb:
                return
            nav = nav_at(s)
            if not nav:
                return
            if s % 60 == 0 and len(spreads) < 20000:
                spreads.append((ba - bb) / (ba + bb))
            fair = nav * (1.0 + nd_)
            # per minute: the cheapest ask and the dearest bid against fair, and the last mid bubble
            a_dev, b_dev, mid_b = ba / fair - 1.0, bb / fair - 1.0, (ba + bb) / 2.0 / nav - 1.0
            mv = mins.get(s // 60)
            if mv is None:
                mins[s // 60] = [a_dev, b_dev, mid_b, a_dev, b_dev, mid_b, mid_b]
            else:
                mv[0] = min(mv[0], a_dev)
                mv[1] = max(mv[1], b_dev)
                mv[2], mv[3], mv[4] = mid_b, a_dev, b_dev
                mv[5] = min(mv[5], mid_b)
                mv[6] = max(mv[6], mid_b)
            if not open_ and ba > fair * (1 - x_min / 100.0) and bb < fair * (1 + x_min / 100.0):
                return
            bids = sorted(([bp, bv] for bp, bv, _a, _b in levels.values() if bp > 0 and bv > 0), key=lambda z: -z[0])
            asks = sorted(([ap, av] for _bp, _bv, ap, av in levels.values() if ap > 0 and av > 0), key=lambda z: z[0])
            for x in thresholds:
                for side in ("ask", "bid"):
                    key = (side, x)
                    thr = fair * (1 - x / 100.0) if side == "ask" else fair * (1 + x / 100.0)
                    book = asks if side == "ask" else bids
                    on = book[0][0] <= thr if side == "ask" else book[0][0] >= thr
                    if on:
                        beyond = [lv for lv in book if (lv[0] <= thr if side == "ask" else lv[0] >= thr)]
                        e = open_.get(key)
                        if e is None:
                            e = open_[key] = {"fund": lab, "sid": sid, "date": d, "side": side, "x": x,
                                              "start": s, "thr": thr, "fair": fair, "nav": nav, "norm": nd_,
                                              "states": [], "best_dev": 0.0,
                                              "dev0": (book[0][0] / fair - 1.0) * 100}
                        dev = (book[0][0] / fair - 1.0) * 100
                        if abs(dev) > abs(e["best_dev"]):
                            e["best_dev"] = dev
                        if not e["states"] or e["states"][-1][1] != beyond:
                            e["states"].append((s, [list(lv) for lv in beyond], nav))
                    elif key in open_:
                        close(key, s)

        i = 0
        while i < len(ev):
            s = _sec(ev[i][1])
            while i < len(ev) and _sec(ev[i][1]) == s:
                r = ev[i]
                if 1 <= r[2] <= 5:
                    levels[r[2]] = (r[3], r[4], r[6], r[7])
                i += 1
            if s < w0:
                continue
            if s > w1:
                break
            evaluate(s)
            last_s = s
        for key in list(open_):
            close(key, max(w1, last_s or w1), "close")
        series[str(d)] = [[m] + [round(v, 5) for v in vals] for m, vals in sorted(mins.items())]
    # what happened afterwards (1 hour, 1 trading day), at the price the position could be closed at
    _outcomes(db, sid, days, fund_eps, nv, latencies, ref_latency, cap, fees)
    eps = [{"fund": e["fund"], "date": e["date"], "side": e["side"], "x": e["x"], "start": e["start"],
            "life": e["life"], "ended": e["ended"], "best_dev": round(e["best_dev"], 4),
            "depth_rial": round(sum(p_ * v for p_, v in e["states"][0][1])) if e["states"] else 0,
            "cap": {str(L): {k: (round(v, 6) if k == "edge" else round(v, 2)) for k, v in c.items()}
                    for L, c in e["cap"].items()},
            "real_1h": e.get("real_1h"), "real_1d": e.get("real_1d"), "dev0": round(e["dev0"], 4),
            "s_in": e["start"] + ref_latency, "exits": e.get("exits", {})} for e in fund_eps]
    return {"fund": lab, "episodes": eps, "spreads": spreads, "days": used_days, "series": series}


def _cache_path(db, sid: int, start, end, cfg: dict):
    import hashlib
    import json
    from pathlib import Path
    root = getattr(db, "path", None)
    if root is None:
        return None
    key = json.dumps([STRAY_VERSION, sid, start, end, cfg, list(TD._collected_version(db, sid)),
                      list(NF.version(db, sid) or [])], sort_keys=True, default=str)
    return Path(root).parent / "stray_cache" / f"{sid}_{hashlib.sha1(key.encode()).hexdigest()[:16]}.json"


def _cached_fund_study(db, sid, lab, start, end, cfg):
    """_fund_study, kept per fund on disk: an interrupted study resumes with the funds already done."""
    import json
    path = _cache_path(db, sid, start, end, cfg)
    if path is not None and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")), True
        except Exception:
            pass
    res = _fund_study(db, sid, lab, start, end, cfg)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass
    return res, False


def _worker_init():
    try:
        import discount_study as S
        S._lower_priority()
    except Exception:
        pass


def _worker(task):
    from pathlib import Path
    from database import Database
    db_path, sid, lab, start, end, cfg = task
    return sid, _cached_fund_study(Database(Path(db_path)), sid, lab, start, end, cfg)


def run_stray_study(db, cats, symbols, start, end, p: D.DiscountParams, progress: dict | None = None,
                    thresholds=THRESHOLDS, latencies=LATENCIES, ref_latency: int = 10, norm_days: int = 20,
                    cap_rial: float | None = None, max_examples: int = 20, workers: int = 0,
                    max_dev_pct: float = 10.0) -> dict:
    t0 = time.time()

    def prog(**kw):
        if progress is not None:
            progress.update(kw)

    funds = D._universe(db, cats, symbols)
    if not funds:
        raise ValueError("صندوقی انتخاب نشده است.")
    thresholds = sorted({float(x) for x in thresholds if float(x) > 0})
    latencies = sorted({int(x) for x in latencies if int(x) >= 0})
    if ref_latency not in latencies:
        latencies = sorted(latencies + [ref_latency])
    cap = float(cap_rial if cap_rial else p.initial_capital * p.position_pct / 100.0)
    fees = p.buy_fee + p.sell_fee
    max_age = max(1, int(p.max_nav_age_min or 5)) * 60
    cfg = {"thresholds": thresholds, "latencies": latencies, "ref_latency": ref_latency, "cap": cap, "fees": fees,
           "max_age": max_age, "norm_days": norm_days}
    results, n_cached = {}, 0
    # funds already done (an earlier, possibly interrupted, run with the same settings and data)
    todo = []
    for sid, lab in funds:
        path = _cache_path(db, sid, start, end, cfg)
        if path is not None and path.exists():
            r, hit = _cached_fund_study(db, sid, lab, start, end, cfg)
            results[sid] = r
            n_cached += hit
        else:
            todo.append((sid, lab))
    prog(phase=f"بازپخش صف‌ها ({n_cached} صندوق از قبل آماده)", done=len(results), total=len(funds))
    if workers <= 0:
        try:
            import discount_study as S
            workers = S._auto_workers()
        except Exception:
            workers = 1
    workers = max(1, min(workers, len(todo)))
    db_path = getattr(db, "path", None)
    if workers > 1 and db_path is not None:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"),
                                 initializer=_worker_init) as ex:
            futs = [ex.submit(_worker, (str(db_path), sid, lab, start, end, cfg)) for sid, lab in todo]
            for f in as_completed(futs):
                sid, (r, _hit) = f.result()
                results[sid] = r
                prog(phase=f"بازپخش صف‌ها ({workers} هسته)", done=len(results), total=len(funds))
    else:
        for sid, lab in todo:
            prog(phase=f"بازپخش صف‌ها: {lab}", done=len(results), total=len(funds))
            results[sid] = _cached_fund_study(db, sid, lab, start, end, cfg)[0]
    episodes, spreads, n_days_all, used_funds, skipped = [], [], set(), [], []
    for sid, lab in funds:
        r = results.get(sid)
        if not r:
            skipped.append(lab)
            continue
        used_funds.append(lab)
        for e in r["episodes"]:
            e["cap"] = {int(L): c for L, c in e["cap"].items()}
            episodes.append(e)
        spreads.extend(r["spreads"])
        n_days_all.update(r["days"])
    if not used_funds:
        raise ValueError("برای صندوق‌های انتخابی دادهٔ TSE (صف و معاملات) یا NAV ریزدانه (feed) وجود ندارد.")
    g_at = _group_series([results[sid] for sid, _l in funds if results.get(sid)])
    _relative(episodes, g_at)
    suspect = [e for e in episodes if abs(e["best_dev"]) > max_dev_pct]
    episodes = [e for e in episodes if abs(e["best_dev"]) <= max_dev_pct]
    n_days = len(n_days_all)
    half_spread = statistics.median(spreads) if spreads else 0.0
    out_sides = []
    for side, slab in SIDES:
        rows = []
        for x in thresholds:
            E = [e for e in episodes if e["side"] == side and e["x"] == x]
            row = _summarise(E, x, latencies, ref_latency, cap, fees, half_spread, n_days, p.initial_capital)
            # fund-specific: cheaper / dearer than the whole group by x too (not a market move the NAV lags behind)
            Ei = [e for e in E if e.get("rel_dev") is not None and
                  (e["rel_dev"] <= -x if side == "ask" else e["rel_dev"] >= x)]
            row["idio"] = _summarise(Ei, x, latencies, ref_latency, cap, fees, half_spread, n_days, p.initial_capital)
            row["idio_pct"] = round(len(Ei) / len(E) * 100, 1) if E else None
            rows.append(row)
        out_sides.append({"side": side, "label": slab, "rows": rows})
    x_ref = 1.0 if 1.0 in thresholds else thresholds[0]
    per_fund = []
    for lab in used_funds:
        E = [e for e in episodes if e["fund"] == lab and e["side"] == "ask" and e["x"] == x_ref]
        cap_e = [e for e in E if e["cap"].get(ref_latency)]
        n_idio = sum(1 for e in E if e.get("rel_dev") is not None and e["rel_dev"] <= -x_ref)
        per_fund.append({"fund": lab, "n": len(E), "idio": n_idio, "alive_ref": len(cap_e),
                         "profit_ref": round(sum(e["cap"][ref_latency]["profit"] for e in cap_e)),
                         "median_life": statistics.median([e["life"] for e in E]) if E else None})
    per_fund.sort(key=lambda r: -r["profit_ref"])
    ex = sorted((e for e in episodes if e["x"] == x_ref), key=lambda e: -abs(e["best_dev"]))[:max_examples]
    examples = [{"fund": e["fund"], "date": e["date"], "time": _hms(e["start"]), "side": e["side"],
                 "dev_pct": round(e["best_dev"], 3), "life_s": e["life"], "ended": e["ended"],
                 "depth_rial": e["depth_rial"], "realised_1d_pct": e.get("real_1d"),
                 "rel_dev_pct": e.get("rel_dev"), "realised_rel_1d_pct": e.get("rel_1d")} for e in ex]
    sus = sorted(suspect, key=lambda e: -abs(e["best_dev"]))
    suspects = {"n": len(suspect), "max_dev_pct": max_dev_pct,
                "funds": sorted({e["fund"] for e in suspect}),
                "examples": [{"fund": e["fund"], "date": e["date"], "time": _hms(e["start"]), "side": e["side"],
                              "dev_pct": round(e["best_dev"], 2), "life_s": e["life"]} for e in sus[:10]]}
    verdict = _verdict(out_sides, x_ref, ref_latency, n_days)
    return {"setup": {"funds": len(used_funds), "skipped": skipped, "days": n_days, "thresholds": thresholds,
                      "latencies": latencies, "ref_latency": ref_latency, "norm_days": norm_days,
                      "cap_rial": cap, "fees_round_trip_pct": round(fees * 100, 3),
                      "median_half_spread_pct": round(half_spread * 100, 4), "max_nav_age_min": max_age // 60,
                      "period": [min(n_days_all), max(n_days_all)] if n_days_all else None,
                      "workers": workers, "funds_from_cache": n_cached,
                      "seconds": round(time.time() - t0, 1)},
            "sides": out_sides, "per_fund": per_fund, "examples": examples, "suspects": suspects, "verdict": verdict}


def _outcomes(db, sid, days, eps, nv: _Nav, latencies, ref_latency: int, cap: float, fees: float) -> None:
    """Per episode: what each reaction delay could take, and the realised result 1 hour / 1 trading day later."""
    need: dict[int, set] = {}
    nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}
    for e in eps:
        e["cap"] = {}
        e["look"] = []
    for e in eps:
        d = e["date"]
        win = TD.market_window(db, d)
        for L in latencies:
            t = e["start"] + L
            if L > 0 and t >= e["end"]:
                continue
            st = None
            for s_, lv, nav_ in e["states"]:
                if s_ <= t:
                    st = (lv, nav_)
            if not st or not st[0]:
                continue
            units, px = _take(st[0], cap)
            if units <= 0:
                continue
            edge = (e["fair"] / px - 1.0 if e["side"] == "ask" else px / e["fair"] - 1.0) - fees
            e["cap"][L] = {"units": units, "price": px, "rial": units * px, "edge": edge, "profit": units * px * edge,
                           "nav": st[1]}
        if ref_latency in e["cap"] and win:
            t1 = min(e["start"] + ref_latency + 3600, win[1])
            e["look"].append(("1h", d, t1))
            need.setdefault(d, set()).add(t1)
            if d in nxt:
                d2 = nxt[d]
                w2 = TD.market_window(db, d2)
                if w2:
                    t2 = min(max(e["start"], w2[0]), w2[1])
                    e["look"].append(("1d", d2, t2))
                    need.setdefault(d2, set()).add(t2)
    states = {}
    for d, secs in need.items():
        ss = sorted(secs)
        ev = G.load_day(db, sid, d, "book") or []
        for s, st in zip(ss, TD._book_states(ev, ss)):
            states[(d, s)] = st
    for e in eps:
        c = e["cap"].get(ref_latency)
        if not c:
            continue
        b_in = c["price"] / c["nav"] - 1.0
        for tag, d, s in e["look"]:
            bids, asks = states.get((d, s), ([], []))
            nav = nv.at(d, s)
            lv = bids if e["side"] == "ask" else asks
            if not lv or not nav:
                continue
            b_out = lv[0][0] / nav - 1.0
            r = ((1 + b_out) / (1 + b_in) - 1.0 if e["side"] == "ask" else (1 + b_in) / (1 + b_out) - 1.0) - fees
            e[f"real_{tag}"] = round(r * 100, 4)
        e["exits"] = {tag: [d, s] for tag, d, s in e.pop("look", [])}


def _daily_t(E, key):
    """Mean and t-statistic of ``key`` over episodes, the t from DAILY means (episodes of one day are not
    independent: one market move makes many of them)."""
    by: dict[int, list] = {}
    for e in E:
        v = e.get(key)
        if v is not None:
            by.setdefault(e["date"], []).append(v)
    vals = [v for vs in by.values() for v in vs]
    daily = [statistics.mean(vs) for vs in by.values()]
    t = _tstat(daily)
    return {"n": len(vals), "days": len(daily), "mean_pct": round(statistics.mean(vals), 3) if vals else None,
            "t": round(t, 2) if t is not None else None,
            "win_pct": round(sum(1 for z in vals if z > 0) / len(vals) * 100, 1) if vals else None}


def _summarise(E, x, latencies, ref_latency, cap, fees, half_spread, n_days, capital) -> dict:
    n = len(E)
    months = max(1e-9, n_days / 21.0)               # ~21 trading days a month
    row = {"x": x, "n": n, "per_month": round(n / months, 1) if n_days else None}
    if not n:
        return row
    lives = [e["life"] for e in E]
    row["median_life_s"] = statistics.median(lives)
    row["life_buckets"] = [{"label": lab, "n": sum(1 for v in lives if lo <= v <= hi)} for lo, hi, lab in LIFE_BUCKETS]
    row["ended"] = {k: round(sum(1 for e in E if e["ended"] == k) / n * 100, 1) for k in ("taken", "cancelled", "close")}
    row["mean_dev_pct"] = round(statistics.mean(e["best_dev"] for e in E), 3)
    rd = [e["rel_dev"] for e in E if e.get("rel_dev") is not None]
    row["mean_rel_dev_pct"] = round(statistics.mean(rd), 3) if rd else None
    row["median_depth_rial"] = round(statistics.median(e["depth_rial"] for e in E))
    lat = []
    for L in latencies:
        C = [e["cap"][L] for e in E if L in e["cap"]]
        prof = sum(c["profit"] for c in C)
        rial = sum(c["rial"] for c in C)
        lat.append({"L": L, "alive_pct": round(len(C) / n * 100, 1), "n": len(C), "rial": round(rial),
                    "per_day": round(len(C) / n_days, 2) if n_days else None,
                    "edge_pct": round(prof / rial * 100, 3) if rial else None,
                    # in "always invested" the cash comes from selling a held fund: one more half spread
                    "edge_switch_pct": round((prof / rial - half_spread) * 100, 3) if rial else None})
    row["latency"] = lat
    for tag in ("1h", "1d"):
        row[f"real_{tag}"] = _daily_t(E, f"real_{tag}")
        row[f"rel_{tag}"] = _daily_t(E, f"rel_{tag}")
    return row


def _group_series(results: list) -> callable:
    """g(d, s) -> medians over the funds, in the minute of s (day d), of: the last mid bubble, the cheapest ask and the
    dearest bid against fair, the lowest and the highest mid bubble, or None when fewer than 3 funds were quoted by then. A fund whose book
    did not change in that minute counts with its last state (it was still quoted there)."""
    per_day: dict[int, list] = {}
    for r in results:
        for d, rows in (r.get("series") or {}).items():
            per_day.setdefault(int(d), []).append(rows)
    g = {}
    for d in list(per_day):
        funds = per_day.pop(d)
        mins = sorted({row[0] for rows in funds for row in rows})
        idx = [0] * len(funds)
        last = [None] * len(funds)
        day = {}
        for m in mins:
            vals = []
            for k, rows in enumerate(funds):
                if idx[k] < len(rows) and rows[idx[k]][0] == m:
                    _m, a, b, c, la, lb, lo, hi = rows[idx[k]]
                    vals.append((c, a, b, lo, hi))
                    last[k] = (c, la, lb, c, c)
                    idx[k] += 1
                elif last[k] is not None:
                    vals.append(last[k])
            if vals:
                day[m] = (len(vals),) + tuple(statistics.median(v[i] for v in vals) for i in range(5))
        g[d] = (sorted(day), day)

    def at(d, s, min_funds: int = 3):
        t = g.get(int(d))
        if not t:
            return None
        j = bisect.bisect_right(t[0], int(s) // 60) - 1
        if j < 0:
            return None
        v = t[1][t[0][j]]
        if v[0] < min_funds:
            return None
        return v[1:]
    return at


def _relative(episodes, g_at) -> None:
    """Per episode: its distance from fair relative to the group's at that minute (a market-wide move the NAV lags
    behind makes every fund look cheap at once: relative ≈ 0), and the realised result minus the group's own bubble
    change (what a switch against another fund of the group would have kept)."""
    for e in episodes:
        g0 = g_at(e["date"], e["start"])
        e["rel_dev"] = round(e["dev0"] - (g0[1] if e["side"] == "ask" else g0[2]) * 100, 4) if g0 else None
        gi = g_at(e["date"], e["s_in"])
        # the group at entry: its lowest mid in that minute for a buy (a market-wide dip is in it), highest for a sale
        g_in = (gi[3] if e["side"] == "ask" else gi[4]) if gi else None
        for tag in ("1h", "1d"):
            r = e.get(f"real_{tag}")
            ex = (e.get("exits") or {}).get(tag)
            go = g_at(*ex) if ex else None
            if r is None or g_in is None or not go:
                e[f"rel_{tag}"] = None
                continue
            dg = (go[0] - g_in) * 100
            e[f"rel_{tag}"] = round(r - dg if e["side"] == "ask" else r + dg, 4)


def _verdict(sides, x_ref, L, n_days) -> dict:
    ask = next(s for s in sides if s["side"] == "ask")
    r = next((x for x in ask["rows"] if x["x"] == x_ref), None)
    if not r or not r.get("n"):
        return {"overall": "bad", "headline": f"✖ در کل دوره هیچ سفارش فروشی {x_ref:g}٪ زیر قیمت معمول دیده نشد."}
    ri = r.get("idio") or {}
    head = (f"فروش دست‌کم {x_ref:g}٪ زیر قیمت معمول: {r['n']} بار، ولی فقط {r.get('idio_pct', 0):g}٪ مخصوص همان صندوق "
            f"(بقیه حرکت کل بازار است که NAV از آن عقب است)")
    if not ri.get("n"):
        return {"overall": "bad", "headline": "✖ " + head + " — سفارش پرتِ واقعی دیده نشد."}
    lr = next((z for z in ri["latency"] if z["L"] == L), {})
    rr = ri.get("rel_1d") or {}
    tail = (f"؛ مخصوص صندوق: {ri['n']} بار، {lr.get('per_day', 0):g} بار در روز هنوز {L} ثانیه بعد در صف، "
            f"نتیجهٔ واقعیِ یک روز بعد نسبت به گروه {rr.get('mean_pct') if rr.get('mean_pct') is not None else '—'}٪ "
            f"(t روزانه={rr.get('t')})")
    m, t = rr.get("mean_pct"), rr.get("t") or 0
    if m is not None and m > 0 and t >= 2 and lr.get("n"):
        return {"overall": "ok", "headline": "✔ " + head + tail + " — شکار سفارش‌های پرتِ واقعی می‌ارزد."}
    if m is not None and m > 0:
        return {"overall": "warn", "headline": "~ " + head + tail + " — مثبت ولی هنوز نامعنادار."}
    return {"overall": "bad", "headline": "✖ " + head + tail + " — بعد از برداشتن، نسبت به گروه سودی نمی‌ماند."}
