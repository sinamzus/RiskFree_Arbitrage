# -*- coding: utf-8 -*-
"""
موتورِ بک‌تستِ آربیتراژِ اختيار معامله (آپشن) — بورس و فرابورس ایران
====================================================================
استراتژی‌های آربیتراژِ بدونِ‌ریسک روی زنجیرهٔ آپشنِ یک دارایی پایه، با قیمت‌های
اجراشدنی (بهترین خرید/فروشِ هر پایه) و کارمزد، آزموده می‌شوند.

استراتژی‌ها
-----------
۱) Conversion (تبدیل) — برابریِ پوت/کال:
   فروشِ کال + خریدِ پوت + خریدِ سهمِ پایه روی یک strike و سررسید.
   در سررسید قطعاً K دریافت می‌شود. سودِ تنزیل‌شده به‌ازای هر سهم:
       edge = K·DF·(1−exec_fee) − (S_ask − C_bid + P_ask) − کارمزدها
   نیازی به فروشِ استقراضی ندارد → در ایران عملی است.

۲) Reversal (معکوس) — عکسِ بالا: خریدِ کال + فروشِ پوت + فروشِ استقراضیِ سهم.
   چون فروشِ استقراضیِ سهم در ایران معمولاً ممکن نیست، پشتِ فلگِ allow_short
   است (پیش‌فرض خاموش).

۳) Box spread (جعبه) — دو strike (K1<K2) و یک سررسید، چهار آپشن:
       cost = (C1_ask − C2_bid) + (P2_ask − P1_bid)
       edge = (K2−K1)·DF·(1−exec_fee) − cost − کارمزدها
   بازدهِ قطعیِ K2−K1 در سررسید؛ فقط آپشن لازم دارد (بدونِ سهم).

مدلِ تنزیل: DF = 1/(1+r)^T با T = روزهای‌تا‌سررسید/۳۶۵.  نرخِ r قابل‌تنظیم
(نرخ‌های ایران بالا هستند؛ پیش‌فرض ۳۰٪).

اجرا: آربیتراژ یک «گیرندهٔ نقدینگی» است — برای قفل‌کردن، اسپرد را می‌شکند؛ پس
پرشدن سرِ تابلو (touch) تا حجمِ موجود است، نه صف‌محور.  سودِ قفل‌شده در لحظهٔ
ورود (به ارزشِ فعلی) شناسایی می‌شود چون بدونِ‌ریسک تا سررسید محقق است.

⚠ مقادیر (اندازهٔ قرارداد، کارمزدها، اروپایی‌بودنِ اعمال، واحدِ strike) را با
مشخصاتِ قراردادِ هر نماد و آخرین مقرراتِ بورسِ کالا/سازمان تطبیق دهید.
"""

from __future__ import annotations

import itertools
import logging
import math
import os
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

SESSION_OPEN_DEFAULT = 90000
SESSION_CLOSE_DEFAULT = 123000


@dataclass
class OptionsParams:
    capital: float = 100_000_000_000.0       # کلِ سرمایه (ریال)
    max_capital_per_trade: float = 10_000_000_000.0
    annual_rate: float = 0.30                # نرخِ بدونِ‌ریسکِ سالانه (تنزیل)
    min_edge_ann_pct: float = 5.0            # حداقلِ بازدهِ سالانه‌شده برای اقدام (٪)
    # استراتژی‌ها
    do_conversion: bool = True
    do_reversal: bool = False                # نیاز به فروشِ استقراضیِ سهم
    do_box: bool = True
    allow_short: bool = False                # اجازهٔ فروشِ استقراضیِ سهم (reversal)
    # کارمزدها (کسری)
    opt_fee: float = 0.0005                  # کارمزدِ هر طرفِ آپشن (روی پرمیوم)
    stock_fee: float = 0.0037                # کارمزدِ سهم (خرید)
    exercise_fee: float = 0.0005             # کارمزدِ اعمال در سررسید (روی K)
    # اجرا
    step_secs: int = 60                      # نمونه‌برداریِ زمانی (۰=هر اسنپ‌شات)
    quote_max_age_secs: int = 600            # مظنهٔ کهنه‌تر از این نادیده گرفته شود
    min_days_to_expiry: int = 1              # سررسیدِ نزدیک‌تر از این کنار گذاشته شود
    one_trade_per_combo_day: bool = True     # ضدِ شمارشِ تکراریِ یک فرصت در روز
    session_open: int = SESSION_OPEN_DEFAULT
    session_close: int = SESSION_CLOSE_DEFAULT


# --------------------------------------------------------------------------- #
#  ابزارها                                                                      #
# --------------------------------------------------------------------------- #

def _in_session(t: int, p: OptionsParams) -> bool:
    return p.session_open <= t <= p.session_close


def _secs(t: int) -> int:
    return (t // 10000) * 3600 + ((t // 100) % 100) * 60 + (t % 100)


def _df(annual_rate: float, days: int) -> float:
    if days <= 0:
        return 1.0
    return 1.0 / (1.0 + annual_rate) ** (days / 365.0)


def _top(snap: dict):
    """(bid, ask, bid_vol, ask_vol) از یک اسنپ‌شاتِ اردربوک."""
    return (snap.get("bid1_price", 0) or 0, snap.get("ask1_price", 0) or 0,
            snap.get("bid1_vol", 0) or 0, snap.get("ask1_vol", 0) or 0)


# --------------------------------------------------------------------------- #
#  بارگذاریِ کشِ روز                                                            #
# --------------------------------------------------------------------------- #

def _load_opt_cache(db, underlying: str, chain: list, start, end):
    """اردربوکِ پایه و همهٔ آپشن‌های زنجیره را per-date یک‌بار از DB می‌خواند.

    خروجی: (dates, cache) که cache[date] = {"under":[snaps], "opts":{sym:[snaps]}}.
    """
    sym_list = [underlying] + [c["symbol"] for c in chain]
    date_set: set[int] = set()
    sym_dates: dict[str, set] = {}
    for sym in sym_list:
        ds = set(db.get_ob_dates(sym))
        sym_dates[sym] = ds
        date_set |= ds
    dates = sorted(date_set)
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]

    cache: dict[int, dict] = {}
    for d in dates:
        if d not in sym_dates.get(underlying, ()):    # پایه لازم است
            continue
        under = db.get_orderbook_history(underlying, d, limit=20000)
        opts = {}
        for c in chain:
            sym = c["symbol"]
            if d in sym_dates.get(sym, ()):
                snaps = db.get_orderbook_history(sym, d, limit=20000)
                if snaps:
                    opts[sym] = snaps
        if under and opts:
            cache[d] = {"under": under, "opts": opts}
    return [d for d in dates if d in cache], cache


# --------------------------------------------------------------------------- #
#  شبیه‌سازیِ یک روز                                                            #
# --------------------------------------------------------------------------- #

def _simulate_opt_day(date_int: int, day: dict, meta: dict, p: OptionsParams):
    """یک روز را روی زنجیرهٔ آپشن اسکن می‌کند و آربیتراژهای قفل‌شدنی را برمی‌گرداند.

    meta: نگاشتِ option_symbol → {opt_type, strike, expiry, contract_size}.
    خروجی: (day_metrics, trades_list)
    """
    from bonds import days_to_maturity

    under_snaps = [s for s in day["under"]
                   if _in_session(int(s.get("time", 0)), p)]
    if not under_snaps:
        return _empty_opt_day(date_int), []

    # سری‌ها: پایه + آپشن‌ها، هرکدام لیستِ اسنپ‌شاتِ مرتب + مکان‌نما.
    series = {"__under__": under_snaps}
    for sym, snaps in day["opts"].items():
        s2 = [s for s in snaps if _in_session(int(s.get("time", 0)), p)]
        if s2:
            series[sym] = s2

    # تایم‌لاینِ یکپارچه
    times = sorted({int(s["time"]) for snaps in series.values() for s in snaps})
    cursors = {k: 0 for k in series}
    cur = {k: None for k in series}          # آخرین topِ هر سری: (b,a,bv,av,t)

    trades: list[dict] = []
    seen_combo: set = set()
    step = p.step_secs
    last_scan = -10 ** 9
    n_scans = 0

    # گروه‌بندیِ آپشن‌ها بر اساسِ سررسید برای box/conversion
    by_expiry: dict[int, list] = {}
    for sym in series:
        if sym == "__under__":
            continue
        m = meta.get(sym)
        if m and m.get("expiry"):
            by_expiry.setdefault(m["expiry"], []).append(sym)

    for t in times:
        # پیش‌بردِ مکان‌نماها تا این لحظه
        for k, snaps in series.items():
            i = cursors[k]
            adv = False
            while i < len(snaps) and int(snaps[i]["time"]) <= t:
                b, a, bv, av = _top(snaps[i])
                cur[k] = (b, a, bv, av, int(snaps[i]["time"]))
                i += 1
                adv = True
            cursors[k] = i
            if adv:
                pass
        if step > 0 and _secs(t) - last_scan < step:
            continue
        last_scan = _secs(t)
        n_scans += 1

        u = cur["__under__"]
        if not u:
            continue
        u_bid, u_ask, u_bv, u_av, u_t = u
        if u_ask <= 0:
            continue
        fresh = lambda c: c is not None and abs(_secs(t) - _secs(c[4])) <= p.quote_max_age_secs

        for expiry, syms in by_expiry.items():
            dte = days_to_maturity(expiry, date_int)
            if dte < p.min_days_to_expiry:
                continue
            df = _df(p.annual_rate, dte)
            # جدولِ strike → {call:(b,a,bv,av), put:...}, contract_size
            calls: dict[float, tuple] = {}
            puts: dict[float, tuple] = {}
            cs_by_strike: dict[float, int] = {}
            for sym in syms:
                c = cur.get(sym)
                if not fresh(c):
                    continue
                m = meta[sym]
                K = m["strike"]
                cs_by_strike[K] = m.get("contract_size", 1000) or 1000
                if m["opt_type"] == "call":
                    calls[K] = c
                elif m["opt_type"] == "put":
                    puts[K] = c

            # ── Conversion / Reversal (per strike) ──
            for K in set(calls) & set(puts):
                cb, ca, cbv, cav, _ = calls[K]
                pb, pa, pbv, pav, _ = puts[K]
                cs = cs_by_strike.get(K, 1000)
                if p.do_conversion and cb > 0 and pa > 0:
                    _try_conversion(date_int, t, expiry, dte, df, K,
                                    u_ask, u_av, cb, cbv, pa, pav, cs,
                                    p, trades, seen_combo)
                if p.do_reversal and p.allow_short and ca > 0 and pb > 0 and u_bid > 0:
                    _try_reversal(date_int, t, expiry, dte, df, K,
                                  u_bid, u_bv, ca, cav, pb, pbv, cs,
                                  p, trades, seen_combo)

            # ── Box spread (per strike pair) ──
            if p.do_box:
                strikes = sorted(set(calls) & set(puts))
                for ii in range(len(strikes)):
                    for jj in range(ii + 1, len(strikes)):
                        K1, K2 = strikes[ii], strikes[jj]
                        _try_box(date_int, t, expiry, dte, df, K1, K2,
                                 calls[K1], calls[K2], puts[K1], puts[K2],
                                 cs_by_strike.get(K1, 1000), p, trades, seen_combo)

    metrics = _opt_day_metrics(date_int, trades, n_scans)
    return metrics, trades


def _ann_pct(edge: float, outlay: float, dte: int) -> float:
    if outlay <= 0 or dte <= 0:
        return 0.0
    return (edge / outlay) * (365.0 / dte) * 100.0


def _try_conversion(date_int, t, expiry, dte, df, K, u_ask, u_av,
                    cb, cbv, pa, pav, cs, p, trades, seen):
    # ارزشِ فعلیِ سود به‌ازای هر سهم
    outlay = u_ask - cb + pa                  # نقدِ خالصِ خروجی اکنون
    if outlay <= 0:
        return
    edge = (K * df * (1.0 - p.exercise_fee)
            - outlay
            - p.opt_fee * (cb + pa) - p.stock_fee * u_ask)
    if edge <= 0:
        return
    ann = _ann_pct(edge, outlay, dte)
    if ann < p.min_edge_ann_pct:
        return
    key = ("conv", expiry, K)
    if p.one_trade_per_combo_day and key in seen:
        return
    # حجمِ قابل‌اجرا (سهم): کف legها
    shares = min(u_av, cbv * cs, pav * cs)
    if shares <= 0:
        return
    cap_shares = int(p.max_capital_per_trade // outlay) if outlay > 0 else 0
    shares = int(min(shares, cap_shares)) if cap_shares > 0 else int(shares)
    if shares <= 0:
        return
    seen.add(key)
    trades.append({
        "date": date_int, "time": t, "strategy": "conversion",
        "expiry": expiry, "dte": dte, "strike": K, "legs": "−C +P +S",
        "edge_per_share": round(edge, 2), "outlay_per_share": round(outlay, 2),
        "shares": shares, "profit": round(edge * shares, 0),
        "capital": round(outlay * shares, 0), "ann_pct": round(ann, 2),
    })


def _try_reversal(date_int, t, expiry, dte, df, K, u_bid, u_bv,
                  ca, cav, pb, pbv, cs, p, trades, seen):
    proceeds = u_bid + pb - ca                # نقدِ ورودیِ اکنون
    edge = (proceeds
            - K * df * (1.0 + p.exercise_fee)
            - p.opt_fee * (ca + pb) - p.stock_fee * u_bid)
    if edge <= 0:
        return
    outlay = K * df                           # سرمایهٔ معادل (تعهدِ سررسید)
    ann = _ann_pct(edge, outlay, dte)
    if ann < p.min_edge_ann_pct:
        return
    key = ("rev", expiry, K)
    if p.one_trade_per_combo_day and key in seen:
        return
    shares = min(u_bv, cav * cs, pbv * cs)
    cap_shares = int(p.max_capital_per_trade // outlay) if outlay > 0 else 0
    shares = int(min(shares, cap_shares)) if cap_shares > 0 else int(shares)
    if shares <= 0:
        return
    seen.add(key)
    trades.append({
        "date": date_int, "time": t, "strategy": "reversal",
        "expiry": expiry, "dte": dte, "strike": K, "legs": "+C −P −S",
        "edge_per_share": round(edge, 2), "outlay_per_share": round(outlay, 2),
        "shares": shares, "profit": round(edge * shares, 0),
        "capital": round(outlay * shares, 0), "ann_pct": round(ann, 2),
    })


def _try_box(date_int, t, expiry, dte, df, K1, K2, c1, c2, p1, p2, cs,
             p, trades, seen):
    c1b, c1a, c1bv, c1av, _ = c1
    c2b, c2a, c2bv, c2av, _ = c2
    p1b, p1a, p1bv, p1av, _ = p1
    p2b, p2a, p2bv, p2av, _ = p2
    # long box: +C(K1) −C(K2) +P(K2) −P(K1)
    if not (c1a > 0 and c2b > 0 and p2a > 0 and p1b > 0):
        return
    cost = (c1a - c2b) + (p2a - p1b)
    if cost <= 0:
        return
    edge = ((K2 - K1) * df * (1.0 - p.exercise_fee) - cost
            - p.opt_fee * (c1a + c2b + p2a + p1b))
    if edge <= 0:
        return
    ann = _ann_pct(edge, cost, dte)
    if ann < p.min_edge_ann_pct:
        return
    key = ("box", expiry, K1, K2)
    if p.one_trade_per_combo_day and key in seen:
        return
    cs = cs or 1000
    shares = min(c1av, c2bv, p2av, p1bv) * cs
    cap_shares = int(p.max_capital_per_trade // cost) if cost > 0 else 0
    shares = int(min(shares, cap_shares)) if cap_shares > 0 else int(shares)
    if shares <= 0:
        return
    seen.add(key)
    trades.append({
        "date": date_int, "time": t, "strategy": "box",
        "expiry": expiry, "dte": dte, "strike": K1, "strike2": K2,
        "legs": f"box {K1:.0f}/{K2:.0f}",
        "edge_per_share": round(edge, 2), "outlay_per_share": round(cost, 2),
        "shares": shares, "profit": round(edge * shares, 0),
        "capital": round(cost * shares, 0), "ann_pct": round(ann, 2),
    })


def _opt_day_metrics(date_int, trades, n_scans):
    return {
        "date": date_int,
        "arbs": len(trades),
        "profit": round(sum(t["profit"] for t in trades), 0),
        "capital": round(sum(t["capital"] for t in trades), 0),
        "scans": n_scans,
        "by_strategy": {s: sum(1 for t in trades if t["strategy"] == s)
                        for s in ("conversion", "reversal", "box")},
    }


def _empty_opt_day(date_int):
    return {"date": date_int, "arbs": 0, "profit": 0.0, "capital": 0.0,
            "scans": 0, "by_strategy": {"conversion": 0, "reversal": 0, "box": 0}}


# --------------------------------------------------------------------------- #
#  ورودیِ اصلی                                                                  #
# --------------------------------------------------------------------------- #

def _chain_meta(chain: list) -> dict:
    return {c["symbol"]: {"opt_type": c.get("opt_type", ""),
                          "strike": float(c.get("strike", 0) or 0),
                          "expiry": int(c.get("expiry", 0) or 0),
                          "contract_size": int(c.get("contract_size", 1000) or 1000)}
            for c in chain}


def run_options_backtest(db, underlying: str,
                         start_date: int | None = None,
                         end_date: int | None = None,
                         params: OptionsParams | None = None) -> dict:
    """بک‌تستِ آربیتراژِ آپشن روی زنجیرهٔ یک دارایی پایه."""
    p = params or OptionsParams()
    chain = db.get_option_series(underlying=underlying)
    chain = [c for c in chain if (c.get("ins_code") or "") and c.get("strike")
             and c.get("expiry") and c.get("opt_type") in ("call", "put")]
    if not chain:
        return {"underlying": underlying, "params": asdict(p), "error":
                "زنجیرهٔ آپشنی برای این پایه ثبت نشده (اول کشف/جمع‌آوری کنید)."}
    meta = _chain_meta(chain)
    dates, cache = _load_opt_cache(db, underlying, chain, start_date, end_date)
    day_rows, all_trades = [], []
    for d in dates:
        m, trades = _simulate_opt_day(d, cache[d], meta, p)
        day_rows.append(m)
        all_trades.extend(trades)
    return {
        "underlying": underlying,
        "params": asdict(p),
        "chain_size": len(chain),
        "days_tested": len(day_rows),
        "date_from": dates[0] if dates else None,
        "date_to": dates[-1] if dates else None,
        "days": day_rows,
        "trades": all_trades[:3000],
        "trade_count": len(all_trades),
        "summary": _summarize_opt(all_trades, day_rows, p),
    }


def _summarize_opt(trades: list, day_rows: list, p: OptionsParams) -> dict:
    if not trades:
        return {"arb_count": 0, "total_profit": 0.0, "total_capital": 0.0,
                "avg_ann_pct": 0.0, "trade_days": len(day_rows), "win_rate": 100.0,
                "by_strategy": {"conversion": 0, "reversal": 0, "box": 0},
                "max_ann_pct": 0.0, "profit_days": 0}
    profit = sum(t["profit"] for t in trades)
    cap = sum(t["capital"] for t in trades)
    anns = [t["ann_pct"] for t in trades]
    by_strat = {s: sum(1 for t in trades if t["strategy"] == s)
                for s in ("conversion", "reversal", "box")}
    profit_days = sum(1 for d in day_rows if d["profit"] > 0)
    return {
        "arb_count": len(trades),
        "total_profit": round(profit, 0),
        "total_capital": round(cap, 0),
        "avg_ann_pct": round(sum(anns) / len(anns), 2),
        "max_ann_pct": round(max(anns), 2),
        "capital_weighted_ann_pct": round(
            sum(t["ann_pct"] * t["capital"] for t in trades) / cap, 2) if cap else 0.0,
        "trade_days": len(day_rows),
        "profit_days": profit_days,
        "win_rate": 100.0,                   # آربیتراژِ قفل‌شده ⇒ همه سودده
        "by_strategy": by_strat,
    }


# --------------------------------------------------------------------------- #
#  بهینه‌ساز — جستجوی شبکه‌ای موازی                                              #
# --------------------------------------------------------------------------- #

OPT_GRID = {
    "annual_rate":      [0.20, 0.25, 0.30, 0.35],
    "min_edge_ann_pct": [0.0, 5.0, 10.0, 20.0],
    "do_box":           [False, True],
    "do_conversion":    [True],
    "step_secs":        [0, 60],
    "quote_max_age_secs": [300, 900],
}
# چیدمان: 0:rate 1:min_edge 2:do_box 3:do_conv 4:step 5:max_age


def _opt_combo_params(base: OptionsParams, c: tuple) -> OptionsParams:
    d = asdict(base)
    d.update(annual_rate=float(c[0]), min_edge_ann_pct=float(c[1]),
             do_box=bool(c[2]), do_conversion=bool(c[3]),
             step_secs=int(c[4]), quote_max_age_secs=int(c[5]))
    return OptionsParams(**d)


def _opt_all_combos() -> list[tuple]:
    return [c for c in itertools.product(
        OPT_GRID["annual_rate"], OPT_GRID["min_edge_ann_pct"], OPT_GRID["do_box"],
        OPT_GRID["do_conversion"], OPT_GRID["step_secs"],
        OPT_GRID["quote_max_age_secs"])
        if c[2] or c[3]]                     # حداقل یک استراتژی فعال


def _opt_result(p, day_rows, trades, opt_metric):
    s = _summarize_opt(trades, day_rows, p)
    if s["arb_count"] == 0:
        score = -1e9
    elif opt_metric == "ann_pct":
        score = s["capital_weighted_ann_pct"]
    elif opt_metric == "arb_count":
        score = s["arb_count"]
    else:                                     # total_profit (پیش‌فرض)
        score = s["total_profit"]
    return {"params": asdict(p), "summary": s, "score": round(float(score), 2)}


_OPT_SHARED: dict = {}


def _opt_spawn_init(shared):
    _OPT_SHARED.clear(); _OPT_SHARED.update(shared)


def _opt_worker(combo):
    sh = _OPT_SHARED
    p = _opt_combo_params(sh["base"], combo)
    day_rows, all_trades = [], []
    for d in sh["dates"]:
        m, tr = _simulate_opt_day(d, sh["cache"][d], sh["meta"], p)
        day_rows.append(m); all_trades.extend(tr)
    r = _opt_result(p, day_rows, all_trades, sh["opt_metric"])
    cnt = sh.get("counter")
    if cnt is not None:
        with cnt.get_lock():
            cnt.value += 1
    return r


def _opt_sort_key(r):
    p = r["params"]
    return (-r["score"], p["annual_rate"], p["min_edge_ann_pct"],
            p["do_box"], p["step_secs"], p["quote_max_age_secs"])


def optimize_options_backtest(db, underlying: str,
                              start_date=None, end_date=None,
                              base: OptionsParams | None = None,
                              opt_metric: str = "total_profit",
                              top_n: int = 15, n_jobs: int = 0,
                              progress: dict | None = None,
                              progress_lock=None) -> dict:
    """جستجوی شبکه‌ای روی پارامترهای آربیتراژِ آپشن (داده یک‌بار، replayِ موازی)."""
    import threading
    base = base or OptionsParams()
    chain = db.get_option_series(underlying=underlying)
    chain = [c for c in chain if (c.get("ins_code") or "") and c.get("strike")
             and c.get("expiry") and c.get("opt_type") in ("call", "put")]
    if not chain:
        return {"underlying": underlying, "tested_combos": 0, "best": None, "top": [],
                "error": "زنجیرهٔ آپشنی ثبت نشده."}
    meta = _chain_meta(chain)
    dates, cache = _load_opt_cache(db, underlying, chain, start_date, end_date)
    combos = _opt_all_combos()
    if progress is not None:
        with (progress_lock or threading.Lock()):
            progress.update({"done": 0, "total": len(combos), "phase": "grid"})
    if not dates:
        return {"underlying": underlying, "tested_combos": 0, "best": None,
                "top": [], "days_available": 0,
                "error": "دادهٔ اردربوکِ کافی نیست."}
    shared = {"base": base, "dates": dates, "cache": cache, "meta": meta,
              "opt_metric": opt_metric}
    results = _opt_run_combos(combos, shared, progress, progress_lock, n_jobs)
    results.sort(key=_opt_sort_key)
    best = results[0] if results and results[0]["score"] > -1e8 else None
    return {
        "underlying": underlying, "tested_combos": len(results),
        "opt_metric": opt_metric, "days_available": len(dates),
        "date_from": dates[0], "date_to": dates[-1],
        "best": best, "top": results[:top_n],
    }


def _opt_run_combos(combos, shared, progress, lock, n_jobs):
    jobs = n_jobs if n_jobs > 0 else max(1, (os.cpu_count() or 2) - 1)
    jobs = min(jobs, len(combos))
    if jobs <= 1:
        _OPT_SHARED.clear(); _OPT_SHARED.update(shared)
        out = []
        for i, c in enumerate(combos):
            out.append(_opt_worker(c))
            if progress is not None:
                progress["done"] = i + 1
        return out
    import multiprocessing as _mp
    import threading as _th
    methods = _mp.get_all_start_methods()
    method = os.environ.get("OPT_OPT_START_METHOD") or (
        "fork" if "fork" in methods else "spawn" if "spawn" in methods else "")
    if method not in methods:
        return _opt_run_combos(combos, shared, progress, lock, 1)
    ctx = _mp.get_context(method)
    counter = ctx.Value("q", 0)
    shared = {**shared, "counter": counter}
    if method == "fork":
        _OPT_SHARED.clear(); _OPT_SHARED.update(shared)
        init, initargs = None, ()
    else:
        init, initargs = _opt_spawn_init, (shared,)
    stop = _th.Event()

    def _poll():
        while not stop.wait(0.4):
            if progress is not None:
                with (lock or _th.Lock()):
                    progress["done"] = counter.value
    poller = _th.Thread(target=_poll, daemon=True)
    if progress is not None:
        poller.start()
    out = []
    try:
        with ctx.Pool(processes=jobs, initializer=init, initargs=initargs) as pool:
            for r in pool.imap_unordered(_opt_worker, combos, chunksize=4):
                out.append(r)
    finally:
        stop.set()
        _OPT_SHARED.clear()
    if progress is not None:
        with (lock or _th.Lock()):
            progress["done"] = len(combos)
    return out
