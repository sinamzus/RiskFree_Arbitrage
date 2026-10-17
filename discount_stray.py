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


class _Nav:
    """The NAV known at a timestamp (forward pointer for a time-ordered walk, bisect otherwise)."""

    def __init__(self, db, sid: int, max_age_s: int):
        ver = NF.version(db, sid)
        self.ok = ver is not None
        self.rep, self.best = (NF._load(db, sid, ver)[1:] if self.ok else ([], []))
        self.max_age = max_age_s

    def at(self, d: int, s: int):
        ts = NF._ts(d, _hms(s))
        j = bisect.bisect_right(self.rep, ts) - 1
        if j < 0:
            return None
        nav, nd, nt = self.best[j]
        if not nd or ts - NF._ts(nd, nt) > self.max_age:
            return None                                  # not current: no reference at this instant
        return nav


def run_stray_study(db, cats, symbols, start, end, p: D.DiscountParams, progress: dict | None = None,
                    thresholds=THRESHOLDS, latencies=LATENCIES, ref_latency: int = 10, norm_days: int = 20,
                    cap_rial: float | None = None, max_examples: int = 20) -> dict:
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
    episodes = []                       # every closed episode
    spreads = []                        # relative half spreads (for the cost of funding a buy by a sale)
    n_days_all, used_funds, skipped = set(), [], []
    for fi, (sid, lab) in enumerate(funds):
        prog(phase=f"بازپخش صف‌ها: {lab}", done=fi, total=len(funds))
        days = TD.tse_days(db, sid, start, end)
        nv = _Nav(db, sid, max_age)
        if not days or not nv.ok:
            skipped.append(lab)
            continue
        used_funds.append(lab)
        trades = {d: sorted((_sec(r[1]), float(r[2]), float(r[3])) for r in (G.load_day(db, sid, d, "trades") or [])
                            if not r[4] and r[2] > 0 and r[3] > 0) for d in days}
        # the fund's own norm: median of its daily median bubble (trades at a current NAV) over the previous days
        med = {}
        for d in days:
            bs = [px / n - 1.0 for s, px, _v in trades[d] for n in (nv.at(d, s),) if n]
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
            n_days_all.add(d)
            w0, w1 = win
            tr_t = [x[0] for x in trades[d]]
            ev = sorted(G.load_day(db, sid, d, "book") or [], key=lambda r: (r[0], r[2]))
            levels: dict[int, tuple] = {}
            open_ = {}                              # (side, x) -> episode being built
            last_s = None

            def close(key, s_end, how=None):
                e = open_.pop(key)
                e["end"] = s_end
                e["life"] = s_end - e["start"]
                side, x = key
                lo, hi = bisect.bisect_left(tr_t, e["start"]), bisect.bisect_right(tr_t, s_end)
                thr = e["thr"]
                got = sum(v for _s, px, v in trades[d][lo:hi] if (px <= thr if side == "ask" else px >= thr))
                e["taken_units"] = got
                e["ended"] = how or ("taken" if got > 0 else "cancelled")
                fund_eps.append(e)

            def evaluate(s):
                bids = sorted(([bp, bv] for bp, bv, _a, _b in levels.values() if bp > 0 and bv > 0), key=lambda z: -z[0])
                asks = sorted(([ap, av] for _bp, _bv, ap, av in levels.values() if ap > 0 and av > 0), key=lambda z: z[0])
                nav = nv.at(d, s)
                if not bids or not asks or asks[0][0] <= bids[0][0] or not nav:
                    return
                if len(spreads) < 400000 and s % 60 == 0:
                    spreads.append((asks[0][0] - bids[0][0]) / (asks[0][0] + bids[0][0]))
                fair = nav * (1.0 + norm[d])
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
                                                  "start": s, "thr": thr, "fair": fair, "nav": nav, "norm": norm[d],
                                                  "states": [], "best_dev": 0.0}
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
        # what happened afterwards (1 hour, 1 trading day), at the price the position could be closed at
        _outcomes(db, sid, days, fund_eps, nv, latencies, ref_latency, cap, fees)
        episodes.extend(fund_eps)

    if not episodes and not used_funds:
        raise ValueError("برای صندوق‌های انتخابی دادهٔ TSE (صف و معاملات) یا NAV ریزدانه (feed) وجود ندارد.")
    n_days = len(n_days_all)
    half_spread = statistics.median(spreads) if spreads else 0.0
    out_sides = []
    for side, slab in SIDES:
        rows = []
        for x in thresholds:
            E = [e for e in episodes if e["side"] == side and e["x"] == x]
            rows.append(_summarise(E, x, latencies, ref_latency, cap, fees, half_spread, n_days, p.initial_capital))
        out_sides.append({"side": side, "label": slab, "rows": rows})
    x_ref = 1.0 if 1.0 in thresholds else thresholds[0]
    per_fund = []
    for lab in used_funds:
        E = [e for e in episodes if e["fund"] == lab and e["side"] == "ask" and e["x"] == x_ref]
        cap_e = [e for e in E if e["cap"].get(ref_latency)]
        per_fund.append({"fund": lab, "n": len(E), "alive_ref": len(cap_e),
                         "profit_ref": round(sum(e["cap"][ref_latency]["profit"] for e in cap_e)),
                         "median_life": statistics.median([e["life"] for e in E]) if E else None})
    per_fund.sort(key=lambda r: -r["profit_ref"])
    ex = sorted((e for e in episodes if e["x"] == x_ref), key=lambda e: -abs(e["best_dev"]))[:max_examples]
    examples = [{"fund": e["fund"], "date": e["date"], "time": _hms(e["start"]), "side": e["side"],
                 "dev_pct": round(e["best_dev"], 3), "life_s": e["life"], "ended": e["ended"],
                 "depth_rial": round(sum(p_ * v for p_, v in e["states"][0][1])) if e["states"] else 0,
                 "realised_1d_pct": e.get("real_1d")} for e in ex]
    verdict = _verdict(out_sides, x_ref, ref_latency, p.initial_capital)
    return {"setup": {"funds": len(used_funds), "skipped": skipped, "days": n_days, "thresholds": thresholds,
                      "latencies": latencies, "ref_latency": ref_latency, "norm_days": norm_days,
                      "cap_rial": cap, "fees_round_trip_pct": round(fees * 100, 3),
                      "median_half_spread_pct": round(half_spread * 100, 4), "max_nav_age_min": max_age // 60,
                      "period": [min(n_days_all), max(n_days_all)] if n_days_all else None,
                      "seconds": round(time.time() - t0, 1)},
            "sides": out_sides, "per_fund": per_fund, "examples": examples, "verdict": verdict}


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
        e.pop("look", None)


def _summarise(E, x, latencies, ref_latency, cap, fees, half_spread, n_days, capital) -> dict:
    n = len(E)
    months = max(1e-9, n_days / 21.0)               # ~21 trading days a month
    years = max(1e-9, n_days / 240.0)               # ~240 trading days a year
    row = {"x": x, "n": n, "per_month": round(n / months, 1) if n_days else None}
    if not n:
        return row
    lives = [e["life"] for e in E]
    row["median_life_s"] = statistics.median(lives)
    row["life_buckets"] = [{"label": lab, "n": sum(1 for v in lives if lo <= v <= hi)} for lo, hi, lab in LIFE_BUCKETS]
    row["ended"] = {k: round(sum(1 for e in E if e["ended"] == k) / n * 100, 1) for k in ("taken", "cancelled", "close")}
    row["mean_dev_pct"] = round(statistics.mean(e["best_dev"] for e in E), 3)
    row["median_depth_rial"] = round(statistics.median(sum(p_ * v for p_, v in e["states"][0][1]) for e in E if e["states"]))
    lat = []
    for L in latencies:
        C = [e["cap"][L] for e in E if L in e["cap"]]
        prof = sum(c["profit"] for c in C)
        rial = sum(c["rial"] for c in C)
        lat.append({"L": L, "alive_pct": round(len(C) / n * 100, 1), "n": len(C), "rial": round(rial),
                    "edge_pct": round(prof / rial * 100, 3) if rial else None,
                    "profit_rial": round(prof), "profit_year_pct_capital": round(prof / years / capital * 100, 2),
                    # in "always invested" the cash comes from selling a held fund: one more half spread
                    "edge_switch_pct": round((prof / rial - half_spread) * 100, 3) if rial else None})
    row["latency"] = lat
    for tag in ("1h", "1d"):
        v = [e[f"real_{tag}"] for e in E if e.get(f"real_{tag}") is not None]
        row[f"real_{tag}"] = {"n": len(v), "mean_pct": round(statistics.mean(v), 3) if v else None,
                              "t": round(_tstat(v), 2) if _tstat(v) is not None else None,
                              "win_pct": round(sum(1 for z in v if z > 0) / len(v) * 100, 1) if v else None}
    return row


def _verdict(sides, x_ref, L, capital) -> dict:
    ask = next(s for s in sides if s["side"] == "ask")
    r = next((x for x in ask["rows"] if x["x"] == x_ref), None)
    if not r or not r.get("n"):
        return {"overall": "bad", "headline": f"✖ در کل دوره هیچ سفارش فروشی {x_ref:g}٪ زیر قیمت معمول دیده نشد."}
    lr = next((z for z in r["latency"] if z["L"] == L), {})
    rd = r.get("real_1d") or {}
    base = (f"سفارش فروش دست‌کم {x_ref:g}٪ ارزان‌تر از قیمت معمول: {r['n']} بار ({r['per_month']} بار در ماه)، "
            f"عمر میانه {r['median_life_s']:g} ثانیه؛ {lr.get('alive_pct', 0):g}٪ هنوز {L} ثانیه بعد در صف بودند")
    if not lr.get("n"):
        return {"overall": "bad", "headline": "✖ " + base + " — با واکنش " + f"{L} ثانیه‌ای چیزی برای برداشتن نمی‌ماند."}
    real = rd.get("mean_pct")
    tail = (f"؛ سود کاغذی {lr['profit_year_pct_capital']:+.2f}٪ سرمایه در سال، "
            f"نتیجهٔ واقعیِ یک روز بعد {real:+.2f}٪ به‌ازای هر بار (t={rd.get('t')})" if real is not None else "")
    if real is not None and real > 0 and (rd.get("t") or 0) >= 2 and lr["profit_year_pct_capital"] >= 1:
        return {"overall": "ok", "headline": "✔ " + base + tail + " — شکارشان می‌ارزد."}
    if lr["profit_year_pct_capital"] > 0 and (real is None or real > 0):
        return {"overall": "warn", "headline": "~ " + base + tail + " — مثبت ولی کوچک یا هنوز نامعنادار."}
    return {"overall": "bad", "headline": "✖ " + base + tail + " — بعد از برداشتن، سودی نمی‌ماند (ارزانی واقعی نبود)."}
