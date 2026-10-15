"""Does the bubble (price vs NAV) predict anything? — a study of the signal itself, independent of any strategy.

At fixed instants (every ``step_min`` minutes of every trading day) the funds form a cross-section. For each fund:
its signal now, and what happened over the next horizon (1 hour, 1 trading day, 5 trading days): the change of its
price and of its NAV, each RELATIVE to the cross-section's average (so the market's common move is removed).

Signals (lower = cheaper):
  * raw      — price / NAV − 1 (across funds at one instant this ranks exactly like "vs the group average");
  * norm     — raw minus the fund's own norm (median daily bubble of its previous ``norm_days`` trading days);
  * change   — raw minus the fund's raw bubble one trading day earlier (a short-term move of the bubble).

Measured per signal and horizon:
  * IC: the rank correlation, within each cross-section, between "cheapness" (−signal) and the future relative price
    return, averaged; its t-statistic from the daily means (divided by the horizon's overlap). IC > 0 = cheap funds
    really did better;
  * the cheapest-minus-dearest spread: future relative return of the cheapest fifth minus the dearest fifth (%, mid);
  * the executable switch: sell the dearest fifth at the bid, buy the cheapest fifth at the ask, pay both fees, and
    compare at the horizon (both valued at the bid) — the only number a trader can bank. A signal "predicts" only
    when this is positive and significant; mid-price reversion smaller than the spread does not count;
  * the same IC on the future relative NAV change: a strongly NEGATIVE NAV IC with no price IC means the "discount"
    closes because the NAV catches up with the price (NAV lag), not because the price rises.
Only the data known at each instant is used for the signal; the future only enters the measured outcome.
"""
from __future__ import annotations

import bisect
import math
import statistics
import time
from dataclasses import replace

import discount_backtest as D

HORIZONS = (("1h", "۱ ساعت", 60, 0), ("1d", "۱ روز معاملاتی", 0, 1), ("5d", "۵ روز معاملاتی", 0, 5))
SIGNALS = (("raw", "حباب خام (قیمت ÷ NAV − ۱)"), ("norm", "فاصله از معمولِ خود صندوق"),
           ("change", "تغییر حباب نسبت به یک روز قبل"))


def _rank(v: list[float]) -> list[float]:
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def _pearson(x: list[float], y: list[float]) -> float | None:
    n = len(x)
    if n < 3:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sxx * syy)


def _spearman(x, y):
    return _pearson(_rank(x), _rank(y))


def _sec(t: int) -> int:
    return (t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100


def _hms(s: int) -> int:
    return (s // 3600) * 10000 + (s // 60 % 60) * 100 + s % 60


def run_signal_study(db, cats, symbols, start, end, p: D.DiscountParams, progress: dict | None = None,
                     step_min: int = 30, norm_days: int = 20, quote_age_min: int = 30,
                     min_funds: int = 5) -> dict:
    t0 = time.time()

    def prog(**kw):
        if progress is not None:
            progress.update(kw)

    funds = D._universe(db, cats, symbols)
    if not funds:
        raise ValueError("صندوقی انتخاب نشده است.")
    pl = replace(p, baseline_days=0, mr_center="off", crash_drop_pct=0.0, rally_rise_pct=0.0, fill_mode="off")
    data = []
    for i, (sid, lab) in enumerate(funds):
        prog(phase=f"خواندن داده: {lab}", done=i, total=len(funds))
        ld = D._load_ex(db, sid, start, end, pl)
        rows = ld["rows"]
        if not rows:
            continue
        keys = [(r[1], r[2]) for r in rows]
        # the fund's own norm per day: median of its daily median bubble over the previous norm_days days
        per_day: dict[int, list] = {}
        for r in rows:
            if r[5]:
                per_day.setdefault(r[1], []).append(r[4] / r[6] - 1.0)
        days = sorted(per_day)
        med = [statistics.median(per_day[d]) for d in days]
        norm = {}
        for j, d in enumerate(days):
            w = med[max(0, j - norm_days):j]
            if len(w) >= max(3, norm_days // 3):
                norm[d] = statistics.median(w)
        data.append({"label": lab, "rows": rows, "keys": keys, "norm": norm, "book": ld.get("book")})
    if len(data) < 3:
        raise ValueError(f"دست‌کم ۳ صندوق با داده لازم است (داریم {len(data)}).")
    min_funds = max(3, min(min_funds, len(data)))         # a cross-section needs this many funds
    all_days = sorted({r[1] for f in data for r in f["rows"]})
    day_ix = {d: i for i, d in enumerate(all_days)}
    age = quote_age_min * 60

    def at(f, d: int, t: int):
        """The fund's latest row on day d at or before t, within the quote age (None otherwise)."""
        j = bisect.bisect_right(f["keys"], (d, t)) - 1
        if j < 0:
            return None
        r = f["rows"][j]
        if r[1] != d or _sec(t) - _sec(r[2]) > age:
            return None
        return r

    hs = p.half_spread_pct / 100.0

    def quotes(f, r):
        """(best bid, best ask) at the row: the real book, else the price ∓ the assumed half spread."""
        b = D._best(f["book"], r[1], r[2], 0, 0.0) if f["book"] else 0.0
        a = D._best(f["book"], r[1], r[2], 1, 0.0) if f["book"] else 0.0
        if not (b > 0 and a > b):
            b, a = r[4] * (1 - hs), r[4] * (1 + hs)
        return b, a

    inst = [(d, _hms(s)) for d in all_days for s in range(9 * 3600, 18 * 3600 + 1, step_min * 60)]
    fees = (p.buy_fee + p.sell_fee) * 100
    # per signal / horizon: lists of per-cross-section (day, IC, spread, NAV IC, NAV spread)
    acc = {(s, h): [] for s, _l in SIGNALS for h, *_r in HORIZONS}
    # executable: switching from the dearest fifth to the cheapest fifth NOW (sell at the bid, buy at the ask, pay both
    # fees) and comparing, at the horizon, the cheap funds' bid with the dear funds' bid — the real gain of a switch
    sw = {(s, h): [] for s, _l in SIGNALS for h, *_r in HORIZONS}
    decomp = {h: {"cheap_price": [], "cheap_nav": [], "dear_price": [], "dear_nav": []} for h, *_r in HORIZONS}
    n_cs = 0
    for k, (d, t) in enumerate(inst):
        if k % 200 == 0:
            prog(phase="سنجش سیگنال در مقطع‌ها", done=k, total=len(inst))
        now = []
        for f in data:
            r = at(f, d, t)
            if r is None or not r[6]:
                continue
            b = r[4] / r[6] - 1.0
            sig = {"raw": b}
            if d in f["norm"]:
                sig["norm"] = b - f["norm"][d]
            di = day_ix[d]
            if di > 0:
                rp = at(f, all_days[di - 1], t)
                if rp is not None and rp[6]:
                    sig["change"] = b - (rp[4] / rp[6] - 1.0)
            now.append((f, r, sig))
        if len(now) < min_funds:
            continue
        n_cs += 1
        for h, _hl, mins, ndays in HORIZONS:
            if ndays:
                di = day_ix[d] + ndays
                if di >= len(all_days):
                    continue
                d1, t1 = all_days[di], t
            else:
                s1 = _sec(t) + mins * 60
                if s1 > 18 * 3600:
                    continue
                d1, t1 = d, _hms(s1)
            fut = []
            for f, r, sig in now:
                r1 = at(f, d1, t1)
                if r1 is None or not r1[6] or r1 is r:
                    continue
                b0, a0 = quotes(f, r)
                b1, _a1 = quotes(f, r1)
                fut.append((sig, r1[4] / r[4] - 1.0, r1[6] / r[6] - 1.0, b1 / a0 - 1.0, b1 / b0 - 1.0))
            if len(fut) < min_funds:
                continue
            mp = sum(x[1] for x in fut) / len(fut)
            mn = sum(x[2] for x in fut) / len(fut)
            for s, _l in SIGNALS:
                pts = [(x[0][s], x[1] - mp, x[2] - mn, x[3], x[4]) for x in fut if s in x[0]]
                if len(pts) < min_funds:
                    continue
                cheap = [-x[0] for x in pts]
                ic = _spearman(cheap, [x[1] for x in pts])
                nic = _spearman(cheap, [x[2] for x in pts])
                srt = sorted(pts, key=lambda x: x[0])
                q = max(1, len(srt) // 5)
                sp = (sum(x[1] for x in srt[:q]) / q - sum(x[1] for x in srt[-q:]) / q) * 100
                nsp = (sum(x[2] for x in srt[:q]) / q - sum(x[2] for x in srt[-q:]) / q) * 100
                acc[(s, h)].append((d, ic, sp, nic, nsp))
                gain = (sum(x[3] for x in srt[:q]) / q - sum(x[4] for x in srt[-q:]) / q) * 100 - fees
                sw[(s, h)].append((d, gain))
                if s == "raw":
                    dc = decomp[h]
                    dc["cheap_price"].append(sum(x[1] for x in srt[:q]) / q * 100)
                    dc["cheap_nav"].append(sum(x[2] for x in srt[:q]) / q * 100)
                    dc["dear_price"].append(sum(x[1] for x in srt[-q:]) / q * 100)
                    dc["dear_nav"].append(sum(x[2] for x in srt[-q:]) / q * 100)

    def tstat(vals: list, overlap: float) -> float | None:
        if len(vals) < 3:
            return None
        sd = statistics.pstdev(vals)
        if sd <= 0:
            return None
        n_eff = max(1.0, len(vals) / max(1.0, overlap))
        return statistics.mean(vals) / sd * math.sqrt(n_eff)

    out_sig = []
    for s, sl in SIGNALS:
        rows_out = []
        for h, hl, mins, ndays in HORIZONS:
            L = [x for x in acc[(s, h)] if x[1] is not None]
            if not L:
                rows_out.append({"h": h, "label": hl, "n_cs": 0})
                continue
            by_day: dict[int, list] = {}
            for d, ic, *_r in L:
                by_day.setdefault(d, []).append(ic)
            daily = [statistics.mean(v) for v in by_day.values()]
            ic_t = tstat(daily, ndays or 1)
            ic = statistics.mean(x[1] for x in L)
            sp = statistics.mean(x[2] for x in L)
            nics = [x[3] for x in L if x[3] is not None]
            nic = statistics.mean(nics) if nics else None
            nsp = statistics.mean(x[4] for x in L)
            g_day: dict[int, list] = {}
            for d, gv in sw[(s, h)]:
                g_day.setdefault(d, []).append(gv)
            gd = [statistics.mean(v) for v in g_day.values()]
            sw_mean = statistics.mean(x[1] for x in sw[(s, h)]) if sw[(s, h)] else None
            sw_t = tstat(gd, ndays or 1)
            pays = sw_mean is not None and sw_mean > 0 and sw_t is not None and sw_t >= 2
            verdict = ("predicts" if ic_t is not None and ic_t >= 2 and ic > 0 and pays else
                       "weak" if ic_t is not None and ic_t >= 2 and ic > 0 else
                       "reverse" if ic_t is not None and ic_t <= -2 else "none")
            rows_out.append({"h": h, "label": hl, "n_cs": len(L), "days": len(by_day),
                             "ic": round(ic, 4), "ic_t": round(ic_t, 2) if ic_t is not None else None,
                             "hit_pct": round(sum(1 for x in L if x[1] > 0) / len(L) * 100, 1),
                             "spread_pct": round(sp, 3), "nav_ic": round(nic, 4) if nic is not None else None,
                             "nav_spread_pct": round(nsp, 3),
                             "switch_net_pct": round(sw_mean, 3) if sw_mean is not None else None,
                             "switch_t": round(sw_t, 2) if sw_t is not None else None, "verdict": verdict})
        out_sig.append({"key": s, "label": sl, "rows": rows_out})
    dec = {}
    for h, hl, *_r in HORIZONS:
        dc = decomp[h]
        if dc["cheap_price"]:
            dec[h] = {"label": hl, **{k: round(statistics.mean(v), 3) for k, v in dc.items()}}
    best = [(r["ic_t"] or 0, s["label"], r["label"], r) for s in out_sig for r in s["rows"] if r.get("n_cs")]
    good = [b for b in best if b[3]["verdict"] == "predicts"]
    weak = [b for b in best if b[3]["verdict"] == "weak"]
    if good:
        g = max(good, key=lambda x: x[0])
        head = (f"✔ «{g[1]}» در افق {g[2]} بازده نسبی آینده را پیش‌بینی می‌کند (IC {g[3]['ic']:+.3f}، t={g[3]['ic_t']}) "
                f"و جابه‌جایی از گران‌ترین‌ها به ارزان‌ترین‌ها با قیمت‌های قابل‌اجرا (صف خرید و فروش) و کارمزد "
                f"{g[3]['switch_net_pct']:+.2f}٪ خالص می‌دهد (t={g[3]['switch_t']}).")
        overall = "ok"
    elif weak:
        g = max(weak, key=lambda x: x[0])
        head = (f"~ «{g[1]}» در افق {g[2]} معنادار است (IC {g[3]['ic']:+.3f}، t={g[3]['ic_t']}) ولی جابه‌جایی از "
                f"گران‌ترین‌ها به ارزان‌ترین‌ها با قیمت‌های قابل‌اجرا و کارمزد {g[3]['switch_net_pct']:+.2f}٪ می‌دهد: "
                f"اثر واقعی است ولی از هزینهٔ معامله کوچک‌تر.")
        overall = "warn"
    else:
        head = "✖ هیچ‌کدام از تعریف‌های حباب بازده نسبی آیندهٔ قیمت را به‌طور معنادار پیش‌بینی نمی‌کند."
        overall = "bad"
    raw1 = next((r for s in out_sig if s["key"] == "raw" for r in s["rows"] if r["h"] == "1d"), None)
    lag = None
    if raw1 and raw1.get("nav_ic") is not None:
        lag = (raw1["nav_ic"] <= -0.1 and abs(raw1.get("ic") or 0) < abs(raw1["nav_ic"]) / 2)
    return {"setup": {"funds": len(data), "days": len(all_days), "instants": len(inst), "cross_sections": n_cs,
                      "step_min": step_min, "norm_days": norm_days, "quote_age_min": quote_age_min,
                      "fees_round_trip_pct": round(fees, 3), "dataset": p.dataset, "min_funds": min_funds,
                      "grid_sec": p.tse_grid_sec if p.dataset == "tse" else None,
                      "period": [all_days[0], all_days[-1]], "seconds": round(time.time() - t0, 1)},
            "signals": out_sig, "decomposition": dec, "nav_lag": lag,
            "verdict": {"overall": overall, "headline": head}}
