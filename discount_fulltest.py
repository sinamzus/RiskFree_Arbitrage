# -*- coding: utf-8 -*-
"""End-to-end test suite for the NAV-discount tab.

It exists to catch one CLASS of bugs that unit checks miss: a parameter that looks wired up but is not
(dead parameter), a value that is reported but not the one simulated, a UI field that is not read, a UI
default that disagrees with the engine, a searched dimension that cannot matter in the chosen mode.

Everything runs on a synthetic world built on the fly (no real data needed):

  1. parameter audit      every parameter must change the result when it should and must NOT when it is
                          documented as inert (e.g. per-fund thresholds in index mode)
  2. web wiring           query string -> DiscountParams for every exposed parameter; defaults; no
                          parameter forgotten (new DiscountParams fields must be mapped or whitelisted)
  3. static UI audit      every control id exists, UI defaults == engine defaults, discParams() reads every
                          control, the optimizer's "apply" mapping covers every searched dimension
  4. optimizer            mode-aware dimensions, no active dimension left empty, reported config ==
                          simulated config, determinism
  5. invariants           accounting identity, exposure bounds, session/pre-open rules, participation cap,
                          fee identity, filters really respected by every trade, over random settings
  6. monotonicity         higher costs never help; return% is independent of the starting capital
  7. explanation          the loss decomposition adds up
  8. UI (optional)        real browser: apply-optimizer-result fills the form, no console errors, tooltips

Run:  python tools/run_tests.py            (add --ui for the browser part)
"""
from __future__ import annotations

import bisect
import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import shutil
import sqlite3
import tempfile
import time
import traceback
from dataclasses import asdict, fields, replace
from pathlib import Path

import discount_backtest as D
import discount_study as S

ROOT = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- #
#  Synthetic world                                                             #
# --------------------------------------------------------------------------- #

FUNDS = [  # id, symbol, category
    (1, "REVA", "equity"), (2, "REVB", "equity"), (3, "GOLDX", "gold"),
    (4, "GOLDY", "gold"), (5, "RW1", "fi"), (6, "GOLDZ", "gold"),
]


def make_world(path: str, days: int = 150, seed: int = 11) -> str:
    """SQLite file with a nav_intraday table the engine can read. Features: mean-reverting funds, a
    permanent-premium fund, a random walk, a gold fund whose trading hours change day to day with
    pre-open quotes, a fund with stale NAV days, crash episodes, stale-price snapshots, thin days."""
    from tools.import_nav_dump import _SCHEMA
    rng = random.Random(seed)
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    con.execute("CREATE TABLE IF NOT EXISTS nav_symbol_map (symbol_id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, matches INTEGER DEFAULT 0)")
    con.execute("CREATE TABLE IF NOT EXISTS nav_symbol_category (symbol_id INTEGER PRIMARY KEY, category TEXT NOT NULL, source TEXT DEFAULT 'auto', score REAL DEFAULT 0)")
    for sid, sym, cat in FUNDS:
        con.execute("INSERT INTO nav_symbol_map VALUES (?,?,1)", (sid, sym))
        con.execute("INSERT INTO nav_symbol_category VALUES (?,?,'manual',1)", (sid, cat))
    d0 = dt.date(2026, 1, 3)
    day_list = []
    d = d0
    while len(day_list) < days:
        if d.weekday() not in (3, 4):                      # Iranian weekend = Thursday + Friday
            day_list.append(d)
        d += dt.timedelta(days=1)
    crash_days = set(rng.sample(range(10, days - 5), 8))
    mkt_days = set(random.Random(seed + 99).sample(range(10, days - 5), 10))   # market-wide fast fall (all funds)
    lag_days = set(rng.sample(range(10, days - 5), 6))
    rows = []
    for sid, sym, cat in FUNDS:
        nav = 1_000_000.0 * (1 + sid / 10)
        rel = 0.0
        premium = 0.02 if sym == "REVB" else 0.0
        for k, day in enumerate(day_list):
            di = day.year * 10000 + day.month * 100 + day.day
            nav *= 1 + rng.gauss(0.0003, 0.004)
            stale = sym == "GOLDY" and k % 4 == 0
            nav_date = (day - dt.timedelta(days=3)) if stale else day
            nd = nav_date.year * 10000 + nav_date.month * 100 + nav_date.day
            if sym == "GOLDX":
                a, b = (11, 15) if k % 2 == 0 else (12, 18)
                pre = [((a - 1) * 10000 + (20 + j * 10) * 100, 0.98) for j in range(3)]
            else:
                a, b, pre = 9, 12, []
            for t, f in pre:                               # pre-open: volume 0, indicative price
                rows.append((sid, di, t, nav, nd, t, nav * f, nav * f, 0))
            n_snap = int((b - a) * 60 / 15) if sym == "GOLDX" else 14
            vol = 0
            thin = rng.random() < 0.15
            nav_t = nav
            for j in range(n_snap):
                mins = a * 60 + j * 15
                t = (mins // 60) * 10000 + (mins % 60) * 100
                if sym == "RW1":
                    rel += rng.gauss(0, 0.0015)
                else:
                    rel = 0.86 * rel + rng.gauss(0, 0.0035)
                px_rel = rel + premium
                if sym == "GOLDZ" and k in crash_days:     # NAV falls fast, price lags -> bubble turns positive
                    nav_t = nav * (1 - 0.04 * (j + 1) / n_snap)
                    px_rel = rel + 0.04 * (j + 1) / n_snap * 0.8
                elif sym == "GOLDZ" and k in lag_days:     # price falls first, NAV stale -> illusory discount
                    px_rel = rel - 0.03 * min(1.0, (j + 1) / 6)
                    nav_t = nav
                else:
                    nav_t = nav
                if k in mkt_days:                          # common shock: every price falls 2.5% in the first hour
                    px_rel -= 0.025 * min(1.0, (j + 1) / 5)
                if stale:
                    px_rel -= 0.015
                last = nav_t * (1 + px_rel)
                grew = rng.random() > 0.15                 # 15% of snapshots carry a stale (no-volume) quote
                if grew:
                    vol += rng.randint(30, 400) if thin else rng.randint(300, 4000)
                ntime = _tm(t, 50) if (sym == "REVB" and k % 5 == 0 and j < 6) else t   # NAV computed 50 min earlier
                rows.append((sid, di, t, nav_t, nd, ntime, last, last, vol))
            if sym == "GOLDX":                             # closing indication after the last trade
                rows.append((sid, di, b * 10000 + 500, nav, nd, b * 10000 + 500, nav * 0.97, nav * 0.97, vol))
    con.executemany("INSERT INTO nav_intraday (symbol_id,date,time,nav,nav_date,nav_time,last,close,vol) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def _tm(t: int, minutes: int) -> int:
    """HHMMSS minus ``minutes``."""
    s = (t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100 - minutes * 60
    s = max(0, s)
    return (s // 3600) * 10000 + (s // 60 % 60) * 100 + s % 60


def _db(path: str):
    from database import Database
    return Database(Path(path))


# --------------------------------------------------------------------------- #
#  Helpers                                                                     #
# --------------------------------------------------------------------------- #

class Results:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, group: str, name: str, ok: bool | None, detail: str = ""):
        self.items.append({"group": group, "name": name, "status": "skip" if ok is None else ("pass" if ok else "fail"),
                           "detail": detail})

    def summary(self) -> dict:
        c = {"pass": 0, "fail": 0, "skip": 0}
        for i in self.items:
            c[i["status"]] += 1
        return {"total": len(self.items), **c}


def _base(**kw) -> D.DiscountParams:
    p = D.DiscountParams(initial_capital=5e9, entry_discount_pct=0.4, exit_discount_pct=0.0, max_hold_days=5,
                         participation_pct=0.0, baseline_days=0, half_spread_pct=0.05,
                         # neutral engine settings the tests were written against (the UI defaults differ on purpose)
                         position_pct=10.0, fill_mode="off", fill_switch_pct=0.5, fill_max_rel_pct=0.0,
                         crash_drop_pct=0.0, crash_window_min=30, crash_cooldown_min=30, crash_scope="category",
                         mr_center="off", mr_min_score=70.0, mr_lag=4, max_nav_age_min=30,
                         buy_fee=0.0012, sell_fee=0.0012, participation_basis="day", exec_delay_snaps=0)
    return replace(p, **kw) if kw else p


def _run(db, p: D.DiscountParams, symbols=None) -> dict:
    return D.run_discount_backtest(db, None, symbols, None, None, p)


def _sig(res: dict) -> tuple:
    h = hashlib.md5(json.dumps([(t["symbol"], t["entry_date"], t["entry_time"], t["exit_date"], t["exit_time"],
                                 t["volume"]) for t in res["trades"]]).encode()).hexdigest()[:10]
    return (round(res["summary"]["final_capital"]), len(res["trades"]), h)


# --------------------------------------------------------------------------- #
#  1) Parameter audit                                                          #
# --------------------------------------------------------------------------- #

# (name, base overrides, field, values, expectation)
#   "change" = result must differ for at least two values;  "same" = identical;  "invariant_ret" = return% same
AUDIT = [
    ("ورود: تخفیف (حالت هر صندوق)", {}, "entry_discount_pct", [0.2, 1.5], "change"),
    ("خروج: تخفیف (حالت هر صندوق)", {}, "exit_discount_pct", [0.5, -0.5], "change"),
    ("حداکثر نگه‌داری", {}, "max_hold_days", [1, 30], "change"),
    ("حد ضرر NAV: بازتر شدن", {"stop_mode": "nav_widen"}, "stop_loss_pct", [0.0, 0.4], "change"),
    ("حد ضرر قیمت", {"stop_mode": "price"}, "stop_loss_pct", [0.0, 0.8], "change"),
    ("حد ضرر NAV: سطح", {"stop_mode": "nav_level"}, "stop_loss_pct", [0.0, 0.8], "change"),
    ("نوع حد ضرر", {"stop_loss_pct": 0.8}, "stop_mode", ["nav_widen", "nav_level", "price"], "change"),
    ("تعدیل حباب دائمی", {}, "baseline_days", [0, 20], "change"),
    ("حالت ورود", {}, "entry_mode", ["fund", "index", "both"], "change"),
    ("ورود شاخص (حالت شاخص)", {"entry_mode": "index"}, "index_entry_pct", [0.1, 0.8], "change"),
    ("خروج شاخص (حالت شاخص)", {"entry_mode": "index"}, "index_exit_pct", [0.3, -0.3], "change"),
    ("ورود شاخص (حالت هر دو)", {"entry_mode": "both"}, "index_entry_pct", [0.1, 0.8], "change"),
    ("مرکز بازگشت به میانگین", {"mr_min_score": 70}, "mr_center", ["off", "zero", "category", "self"], "change"),
    ("پنجرهٔ امتیاز بازگشت", {"mr_center": "zero"}, "mr_window_days", [10, 40], "change"),
    ("افق بازگشت", {"mr_center": "zero"}, "mr_horizon_days", [2, 15], "change"),
    ("حداقل امتیاز بازگشت", {"mr_center": "zero"}, "mr_min_score", [30, 95], "change"),
    ("حداکثر سن NAV", {"max_nav_age_min": 0}, "max_nav_age_days", [0, 5], "change"),
    ("حداکثر سن NAV (دقیقه)", {}, "max_nav_age_min", [0, 15], "change"),
    ("نیم‌اسپرد", {}, "half_spread_pct", [0.0, 0.3], "change"),
    ("کارمزد خرید", {}, "buy_fee", [0.0, 0.01], "change"),
    ("کارمزد فروش", {}, "sell_fee", [0.0, 0.01], "change"),
    ("سقف حجم", {}, "participation_pct", [0.0, 0.5], "change"),
    ("سهم هر پوزیشن", {}, "position_pct", [5.0, 40.0], "change"),
    ("فقط قیمت تازه", {}, "require_fresh", [True, False], "change"),
    ("فیلتر ریزش: آستانه", {"crash_scope": "all"}, "crash_drop_pct", [0.0, 0.3], "change"),
    ("فیلتر ریزش: پنجره", {"crash_drop_pct": 0.3, "crash_scope": "all"}, "crash_window_min", [10, 240], "change"),
    ("فیلتر ریزش: توقف موقت", {"crash_drop_pct": 0.3, "crash_scope": "all", "crash_window_min": 30}, "crash_cooldown_min", [0, 240], "change"),
    ("فیلتر ریزش: گروه", {"crash_drop_pct": 0.3}, "crash_scope", ["category", "all"], "change"),
    ("حداقل سهم صندوق‌ها برای شاخص (حالت شاخص)", {"entry_mode": "index"}, "index_min_share", [0.3, 1.0], "change"),
    ("فاصلهٔ جفت‌های امتیاز بازگشت", {"mr_center": "zero"}, "mr_lag", [1, 8], "change"),
    ("پر کردن سرمایهٔ بیکار: روشن/خاموش", {}, "fill_mode", ["off", "best", "hold"], "change"),
    ("نگه‌داری: حداقل برتری برای جابه‌جایی", {"fill_mode": "hold", "fill_max_rel_pct": 1.0, "position_pct": 25}, "fill_switch_pct", [0.2, 3.0], "change"),
    ("پر کردن: حداکثر حباب نسبی", {"fill_mode": "best"}, "fill_max_rel_pct", [-0.1, 0.5], "change"),
    ("پر کردن: آستانهٔ فروش", {"fill_mode": "best"}, "fill_exit_rel_pct", [0.1, 1.5], "change"),
    ("حالت ساعت جلسه", {"session_start": 90000, "session_end": 123000}, "session_mode", ["auto", "fixed"], "change"),
    ("پایان جلسه (ساعت ثابت)", {"session_mode": "fixed", "session_start": 90000}, "session_end", [110000, 123000], "change"),
    ("شروع جلسه (ساعت ثابت)", {"session_mode": "fixed", "session_end": 123000}, "session_start", [90000, 113000], "change"),
    # ---- must be inert in these settings ----
    ("ورود تخفیف در حالت شاخص بی‌اثر است", {"entry_mode": "index"}, "entry_discount_pct", [0.2, 1.5], "same"),
    ("خروج تخفیف در حالت شاخص بی‌اثر است", {"entry_mode": "index"}, "exit_discount_pct", [0.5, -0.5], "same"),
    ("ورود/خروج شاخص در حالت هر صندوق بی‌اثر است", {"entry_mode": "fund"}, "index_entry_pct", [0.1, 0.8], "same"),
    ("خروج شاخص در حالت هر دو بی‌اثر است", {"entry_mode": "both"}, "index_exit_pct", [0.3, -0.3], "same"),
    ("پنجرهٔ بازگشت وقتی فیلتر خاموش است بی‌اثر است", {"mr_center": "off"}, "mr_window_days", [10, 40], "same"),
    ("حداقل امتیاز وقتی فیلتر خاموش است بی‌اثر است", {"mr_center": "off"}, "mr_min_score", [30, 95], "same"),
    ("نوع حد ضرر وقتی حد ضرر صفر است بی‌اثر است", {"stop_loss_pct": 0.0}, "stop_mode", ["nav_widen", "price"], "same"),
    ("پنجرهٔ ریزش وقتی فیلتر خاموش است بی‌اثر است", {"crash_drop_pct": 0.0}, "crash_window_min", [10, 240], "same"),
    ("توقف موقت وقتی فیلتر خاموش است بی‌اثر است", {"crash_drop_pct": 0.0}, "crash_cooldown_min", [0, 240], "same"),
    ("حداقل سهم شاخص در حالت هر صندوق بی‌اثر است", {"entry_mode": "fund"}, "index_min_share", [0.3, 1.0], "same"),
    ("فاصلهٔ جفت‌ها وقتی فیلتر بازگشت خاموش است بی‌اثر است", {"mr_center": "off"}, "mr_lag", [1, 8], "same"),
    ("حداکثر حباب نسبی وقتی پر کردن خاموش است بی‌اثر است", {"fill_mode": "off"}, "fill_max_rel_pct", [-0.1, 0.5], "same"),
    ("آستانهٔ فروش پارک وقتی پر کردن خاموش است بی‌اثر است", {"fill_mode": "off"}, "fill_exit_rel_pct", [0.1, 1.5], "same"),
    ("نگه‌داری: حداقل زمان پیش از جابه‌جایی", {"fill_mode": "hold", "fill_max_rel_pct": 1.0, "position_pct": 25}, "fill_min_hold_min", [0, 100000], "change"),
    ("مبنای سقف حجم", {"participation_pct": 0.3}, "participation_basis", ["sofar", "day"], "change"),
    ("تأخیر اجرا", {}, "exec_delay_snaps", [0, 2], "change"),
    ("مبنای سقف حجم بدون سقف بی‌اثر است", {"participation_pct": 0.0}, "participation_basis", ["sofar", "day"], "same"),
    ("عمر قیمت جاری", {"fill_mode": "hold", "fill_max_rel_pct": 1.0, "position_pct": 25}, "fill_quote_age_min", [1, 600], "change"),
    ("حداقل زمان نگه‌داری وقتی «نگه‌داری» نیست بی‌اثر است", {"fill_mode": "best"}, "fill_min_hold_min", [0, 100000], "same"),
    ("عمر قیمت وقتی پر کردن خاموش است بی‌اثر است", {"fill_mode": "off"}, "fill_quote_age_min", [1, 600], "same"),
    ("برتری جابه‌جایی وقتی «نگه‌داری» نیست بی‌اثر است", {"fill_mode": "best"}, "fill_switch_pct", [0.2, 3.0], "same"),
    ("آستانهٔ فروش پارک در «نگه‌داری» بی‌اثر است", {"fill_mode": "hold", "fill_max_rel_pct": 1.0}, "fill_exit_rel_pct", [0.1, 1.5], "same"),
    ("آستانهٔ ورود در «نگه‌داری» بی‌اثر است", {"fill_mode": "hold", "fill_max_rel_pct": 1.0}, "entry_discount_pct", [0.2, 1.5], "same"),
    ("آستانهٔ خروج در «نگه‌داری» بی‌اثر است", {"fill_mode": "hold", "fill_max_rel_pct": 1.0}, "exit_discount_pct", [0.0, -0.5], "same"),
    ("حداکثر روز نگه‌داری در «نگه‌داری» بی‌اثر است", {"fill_mode": "hold", "fill_max_rel_pct": 1.0}, "max_hold_days", [2, 30], "same"),
    ("حد ضرر در «نگه‌داری» بی‌اثر است", {"fill_mode": "hold", "fill_max_rel_pct": 1.0}, "stop_loss_pct", [0.0, 1.0], "same"),
    ("ساعت ثابت وقتی حالت خودکار است بی‌اثر است", {"session_mode": "auto"}, "session_end", [100000, 123000], "same"),
    # ---- invariance ----
    ("بازده٪ به سرمایهٔ اولیه وابسته نیست", {}, "initial_capital", [1e9, 9e10], "invariant_ret"),
]


def test_parameter_audit(R: Results, db):
    for name, over, field, values, expect in AUDIT:
        try:
            outs = []
            for v in values:
                res = _run(db, _base(**over, **{field: v}))
                outs.append(res)
            if expect == "invariant_ret":
                rets = [o["summary"]["portfolio_return_pct"] for o in outs]
                ok = max(rets) - min(rets) < 1e-6
                R.add("ممیزی پارامتر", name, ok, f"بازده‌ها {rets}")
                continue
            sigs = [_sig(o) for o in outs]
            changed = len(set(sigs)) > 1
            if expect == "change":
                info = " | ".join(f"{v}: سرمایه {s[0]:,} / {s[1]} معامله" for v, s in zip(values, sigs))
                R.add("ممیزی پارامتر", f"{name} باید نتیجه را عوض کند", changed,
                      info if changed else "پارامتر مرده است — تغییرش هیچ اثری نداشت! " + info)
            else:
                R.add("ممیزی پارامتر", name, not changed,
                      "بی‌اثر ✓" if not changed else "اثر ناخواسته: " + str(sigs))
        except Exception as e:                              # a crash is a failure of the audit itself
            R.add("ممیزی پارامتر", name, False, f"خطا: {type(e).__name__}: {e}")


def test_cost_monotonic(R: Results, db):
    for field, lo, hi in (("half_spread_pct", 0.0, 0.3), ("buy_fee", 0.0, 0.01), ("sell_fee", 0.0, 0.01)):
        a = _run(db, _base(**{field: lo}))["summary"]["final_capital"]
        b = _run(db, _base(**{field: hi}))["summary"]["final_capital"]
        R.add("یکنوایی هزینه", f"افزایش «{field}» هرگز سرمایهٔ نهایی را بیشتر نمی‌کند", b <= a + 1,
              f"{lo}: {a:,.0f} → {hi}: {b:,.0f}")
    p = _base()
    r1, r2 = _run(db, p), _run(db, p)
    R.add("تکرارپذیری", "دو اجرای یکسان نتیجهٔ یکسان می‌دهند", _sig(r1) == _sig(r2), "")


# --------------------------------------------------------------------------- #
#  2) Web wiring                                                               #
# --------------------------------------------------------------------------- #

# query key -> (DiscountParams field, test value (string), expected parsed value)
WEB_MAP = {
    "initial": ("initial_capital", "7000000000", 7e9), "pos": ("position_pct", "17", 17.0),
    "entry": ("entry_discount_pct", "0.77", 0.77), "exit": ("exit_discount_pct", "-0.21", -0.21),
    "hold": ("max_hold_days", "7", 7), "stop": ("stop_loss_pct", "2.5", 2.5),
    "stopmode": ("stop_mode", "price", "price"), "base": ("baseline_days", "33", 33),
    "entrymode": ("entry_mode", "both", "both"), "ientry": ("index_entry_pct", "0.44", 0.44),
    "iexit": ("index_exit_pct", "-0.12", -0.12), "mrcenter": ("mr_center", "category", "category"),
    "mrwin": ("mr_window_days", "25", 25), "mrmin": ("mr_min_score", "77", 77.0), "mrhor": ("mr_horizon_days", "7", 7),
    "spread": ("half_spread_pct", "0.07", 0.07), "part": ("participation_pct", "3.5", 3.5),
    "buyfee": ("buy_fee", "0.2", 0.002), "sellfee": ("sell_fee", "0.25", 0.0025), "navage": ("max_nav_age_days", "2", 2),
    "fresh": ("require_fresh", "0", False), "smode": ("session_mode", "fixed", "fixed"),
    "sstart": ("session_start", "10:15", 101500), "send": ("session_end", "17:45", 174500),
    "crashdrop": ("crash_drop_pct", "0.8", 0.8), "crashwin": ("crash_window_min", "45", 45),
    "crashcool": ("crash_cooldown_min", "90", 90), "crashscope": ("crash_scope", "category", "category"),
    "imin": ("index_min_share", "60", 0.6), "mrlag": ("mr_lag", "6", 6),
    "navmin": ("max_nav_age_min", "17", 17), "fillswitch": ("fill_switch_pct", "0.9", 0.9),
    "ds": ("dataset", "hybrid", "hybrid"), "grid": ("tse_grid_sec", "120", 120),
    "fillminhold": ("fill_min_hold_min", "150", 150), "fillage": ("fill_quote_age_min", "45", 45),
    "partbasis": ("participation_basis", "day", "day"), "delay": ("exec_delay_snaps", "3", 3),
    "fillmode": ("fill_mode", "best", "best"), "fillmax": ("fill_max_rel_pct", "0.15", 0.15),
    "fillexit": ("fill_exit_rel_pct", "0.45", 0.45),
}
# DiscountParams fields that are deliberately not exposed in the UI
NOT_EXPOSED: set = set()


def test_web_wiring(R: Results, db):
    from web_server import create_app
    app = create_app(db)
    c = app.test_client()
    q = "&".join(f"{k}={v[1]}" for k, v in WEB_MAP.items())
    r = c.get(f"/api/disc/backtest?symbols=REVA,GOLDX&{q}")
    if r.status_code != 200:
        R.add("سیم‌کشی وب", "درخواست بک‌تست با همهٔ پارامترها", False, f"HTTP {r.status_code}: {r.get_data(as_text=True)[:200]}")
        return
    got = r.get_json()["params"]
    # the assumed spread only exists on the NAV-dump dataset (order-book datasets force it to 0): read it there
    got["half_spread_pct"] = c.get(f"/api/disc/backtest?symbols=REVA&ds=dump&spread={WEB_MAP['spread'][1]}"
                                   ).get_json()["params"]["half_spread_pct"]
    bad = []
    for k, (field, _v, exp) in WEB_MAP.items():
        g = got.get(field)
        ok = (abs(g - exp) < 1e-9) if isinstance(exp, float) and isinstance(g, (int, float)) and not isinstance(g, bool) else g == exp
        if not ok:
            bad.append(f"{k}→{field}: ارسال {_v} ← خوانده شد {g!r} (انتظار {exp!r})")
    R.add("سیم‌کشی وب", f"هر ۳۴ پارامترِ فرم به فیلد درست DiscountParams می‌رسد", not bad, "؛ ".join(bad) or "همه درست")
    r0 = c.get("/api/disc/backtest?symbols=REVA")
    d0 = r0.get_json()["params"]
    dflt = asdict(D.DiscountParams())
    bad = [f"{f}: {d0.get(f)!r} ≠ {dflt[f]!r}" for _k, (f, _v, _e) in WEB_MAP.items()
           if f not in ("initial_capital",) and d0.get(f) != dflt[f]]
    R.add("سیم‌کشی وب", "بدون پارامتر، پیش‌فرض‌های سرور = پیش‌فرض‌های موتور", not bad, "؛ ".join(bad) or "یکسان")
    mapped = {f for f, _v, _e in WEB_MAP.values()}
    missing = {f.name for f in fields(D.DiscountParams)} - mapped - NOT_EXPOSED
    R.add("سیم‌کشی وب", "هیچ پارامتر موتور بدون مسیر وب نیست (پارامتر جدید باید نگاشت یا در فهرست استثنا بیاید)",
          not missing, "فراموش‌شده: " + ", ".join(sorted(missing)) if missing else "کامل")
    # invalid values must not crash
    r = c.get("/api/disc/backtest?symbols=REVA&entrymode=nonsense&stopmode=zzz&mrcenter=q&sstart=xx&hold=abc")
    R.add("سیم‌کشی وب", "مقدار نامعتبر کرش نمی‌کند و به پیش‌فرض برمی‌گردد", r.status_code == 200,
          f"HTTP {r.status_code}")


# --------------------------------------------------------------------------- #
#  3) Static UI audit                                                          #
# --------------------------------------------------------------------------- #

def _html() -> str:
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _el(html: str, eid: str):
    m = re.search(r'<(input|select)\b[^>]*\bid="%s"[^>]*>' % re.escape(eid), html)
    if not m:
        return None
    tag = m.group(1)
    attrs = m.group(0)
    if tag == "input":
        v = re.search(r'\bvalue="([^"]*)"', attrs)
        return {"tag": tag, "value": v.group(1) if v else "", "checked": "checked" in attrs.replace('id="%s"' % eid, "")}
    end = html.index("</select>", m.end())
    opts = re.findall(r'<option\b([^>]*)>', html[m.end():end])
    vals = [re.search(r'value="([^"]*)"', o).group(1) for o in opts if re.search(r'value="([^"]*)"', o)]
    sel = [re.search(r'value="([^"]*)"', o).group(1) for o in opts if "selected" in o and re.search(r'value="([^"]*)"', o)]
    return {"tag": tag, "options": vals, "value": sel[0] if sel else (vals[0] if vals else "")}


UI_ID = {  # query key -> element id
    "initial": "disc-capital", "pos": "disc-pos", "entry": "disc-entry", "exit": "disc-exit", "hold": "disc-hold",
    "stop": "disc-stop", "stopmode": "disc-stopmode", "base": "disc-base", "entrymode": "disc-entrymode",
    "ientry": "disc-ientry", "iexit": "disc-iexit", "mrcenter": "disc-mrcenter", "mrwin": "disc-mrwin",
    "mrmin": "disc-mrmin", "mrhor": "disc-mrhor", "spread": "disc-spread", "part": "disc-part", "buyfee": "disc-buyfee",
    "sellfee": "disc-sellfee", "navage": "disc-navage", "fresh": "disc-fresh", "smode": "disc-smode",
    "sstart": "disc-sstart", "send": "disc-send", "crashdrop": "disc-crashdrop", "crashwin": "disc-crashwin",
    "crashcool": "disc-crashcool", "crashscope": "disc-crashscope", "imin": "disc-imin", "mrlag": "disc-mrlag",
    "navmin": "disc-navmin", "ds": "disc-dataset", "grid": "disc-grid", "fillswitch": "disc-fillswitch", "fillminhold": "disc-fillminhold", "fillage": "disc-fillage", "partbasis": "disc-partbasis", "delay": "disc-delay", "fillmode": "disc-fillmode", "fillmax": "disc-fillmax", "fillexit": "disc-fillexit",
}


def _hhmmss(v: str) -> int:
    d = v.replace(":", "")
    return int(d[:-2]) * 10000 + int(d[-2:]) * 100


def test_static_ui(R: Results):
    html = _html()
    dflt = asdict(D.DiscountParams())
    missing = [i for i in UI_ID.values() if _el(html, i) is None]
    R.add("رابط کاربری (ایستا)", "همهٔ کنترل‌های پارامتر در صفحه وجود دارند", not missing, ", ".join(missing) or "کامل")
    import shutil as _sh
    import subprocess as _sp
    node = _sh.which("node")
    if node:                                  # a JS syntax error leaves the whole app stuck on "loading"
        errs = []
        for k, js in enumerate(re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", html, re.S)):
            fd, fp = tempfile.mkstemp(suffix=".js")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(js)
            r = _sp.run([node, "--check", fp], capture_output=True, text=True)
            os.remove(fp)
            if r.returncode:
                errs.append(r.stderr.strip()[:300])
        R.add("رابط کاربری (ایستا)", "اسکریپت‌های صفحه خطای نحوی ندارند", not errs, "؛ ".join(errs) or "سالم")
    bad = []
    for k, eid in UI_ID.items():
        el = _el(html, eid)
        if el is None:
            continue
        field = WEB_MAP[k][0]
        eng = dflt[field]
        v = el.get("value")
        if k == "fresh":
            ok = bool(el.get("checked")) == eng
        elif k in ("sstart", "send"):
            ok = _hhmmss(v) == eng
        elif k == "initial":
            ok = abs(float(v) * 1e6 - eng) < 1
        elif k in ("buyfee", "sellfee", "imin"):
            ok = abs(float(v) / 100 - eng) < 1e-9
        elif isinstance(eng, str):
            ok = v == eng
        else:
            ok = abs(float(v) - float(eng)) < 1e-9
        if not ok:
            bad.append(f"{eid}: فرم {v!r} ≠ موتور {eng!r}")
    R.add("رابط کاربری (ایستا)", "پیش‌فرض هر فیلد فرم = پیش‌فرض موتور", not bad, "؛ ".join(bad) or "یکسان")
    m = re.search(r"function discParams\(\) \{(.*?)\n\}", html, re.S)
    body = m.group(1) if m else ""
    unread = [eid for k, eid in UI_ID.items() if f"'{eid}'" not in body and f'"{eid}"' not in body]
    R.add("رابط کاربری (ایستا)", "تابع discParams() همهٔ کنترل‌ها را می‌خواند (وگرنه تغییر فیلد اثری ندارد)", not unread,
          ", ".join(unread) or "همه خوانده می‌شوند")
    wrong = []
    for k, eid in UI_ID.items():                       # the query key sent for that control must be the one the server parses
        if k == "initial":
            continue
        pat = re.search(r"\b%s:\s*(?:v\('%s'\)|document\.getElementById\('%s'\)[^,]*)" % (k, eid, eid), body)
        if not pat:
            wrong.append(f"{eid}↛{k}")
    R.add("رابط کاربری (ایستا)", "کلید ارسالی هر کنترل همان کلیدی است که سرور می‌خواند", not wrong, ", ".join(wrong) or "درست")
    # option values vs server whitelist and optimizer choices
    bad = []
    for eid, dim in (("disc-entrymode", "entry_mode"), ("disc-stopmode", "stop_mode"), ("disc-mrcenter", "mr_center")):
        opts = set((_el(html, eid) or {}).get("options", []))
        need = set(S.DIMS[dim]["choices"])
        if not need <= opts:
            bad.append(f"{eid}: گزینه‌های {sorted(need - opts)} در فرم نیست")
    R.add("رابط کاربری (ایستا)", "گزینه‌های منوها شامل همهٔ مقدارهای قابل جستجوی بهینه‌ساز است", not bad, "؛ ".join(bad) or "کامل")
    # optimizer apply mapping
    m = re.search(r"const DS_FORM = \{(.*?)\};", html, re.S)
    ds = dict(re.findall(r"(\w+):\s*'([\w-]+)'", m.group(1))) if m else {}
    R.add("رابط کاربری (ایستا)", "نگاشت «اعمال نتیجهٔ بهینه‌سازی» همهٔ پارامترهای جستجوشدنی را پوشش می‌دهد",
          set(ds) == set(S.DIMS), f"کم: {sorted(set(S.DIMS) - set(ds))} · اضافه: {sorted(set(ds) - set(S.DIMS))}")
    bad = [f"{k}→{i}" for k, i in ds.items() if _el(html, i) is None or f"'{i}'" not in body]
    R.add("رابط کاربری (ایستا)", "هر کنترلِ مقصدِ «اعمال» وجود دارد و در بک‌تست خوانده می‌شود", not bad, ", ".join(bad) or "درست")
    # "fixed value" column: each searched dimension maps to the query key the server reads for that very field
    mq = re.search(r"const DS_QKEY = \{(.*?)\};", html, re.S)
    qk = dict(re.findall(r"(\w+):\s*'(\w+)'", mq.group(1))) if mq else {}
    bad = [d for d in S.DIMS if d not in qk or qk[d] not in WEB_MAP or WEB_MAP[qk[d]][0] != d]
    R.add("رابط کاربری (ایستا)", "ستون «مقدار ثابت»: هر پارامتر به کلید درستِ فرم/سرور نگاشت می‌شود", not bad,
          ", ".join(bad) or f"{len(qk)} پارامتر")
    # each searched dimension is really a DiscountParams field the server parses
    web_fields = {f for f, _v, _e in WEB_MAP.values()}
    bad = [d for d in S.DIMS if d not in web_fields]
    R.add("رابط کاربری (ایستا)", "هر پارامتر جستجوشدنی یک فیلد وبیِ خوانده‌شده است", not bad, ", ".join(bad) or "درست")


# --------------------------------------------------------------------------- #
#  4) Optimizer                                                                #
# --------------------------------------------------------------------------- #

def test_optimizer_static(R: Results, db):
    # (a) mode-aware space: no searched dimension may be inert for the mode(s) in play
    for mode in ("fund", "index", "both"):
        for preset in ("quick", "medium", "full"):
            base = _base(entry_mode=mode)
            space = {k: list(S.DIMS[k]["choices"]) for k in S.PRESETS[preset]}
            if preset != "full":                            # presets that do not search the mode itself
                space.pop("entry_mode", None)
            st = S.Study(db, None, ["REVA"], None, None, base, space, n_samples=20)
            inert = []
            for d in st.space:
                modes = st.space.get("entry_mode") or [mode]
                centers = st.space.get("mr_center") or [base.mr_center]
                sess = st.space.get("session_mode") or [base.session_mode]
                fills = st.space.get("fill_mode") or [base.fill_mode]
                ok = any(S._active(d, {"entry_mode": m, "mr_center": c, "stop_loss_pct": 1.0, "crash_drop_pct": 1.0,
                                       "session_mode": sm, "fill_mode": fm})
                         for m in modes for c in centers for sm in sess for fm in fills)
                if not ok:
                    inert.append(d)
            need = set()
            modes = st.space.get("entry_mode") or [mode]
            if any(m in ("fund", "both") for m in modes):
                need |= {"entry_discount_pct", "exit_discount_pct"}
            if any(m in ("index", "both") for m in modes):
                need.add("index_entry_pct")
            if "index" in modes:
                need.add("index_exit_pct")
            lack = need - set(st.space)
            R.add("بهینه‌ساز", f"پیش‌تنظیم «{preset}» در حالت «{mode}»: پارامتر بی‌اثر نیست و پارامتر لازم هست",
                  not inert and not lack, f"بی‌اثر: {inert} · کم: {sorted(lack)}")
    # (a2) a dimension that becomes active during refinement must get a concrete value (never silently the form's)
    st = S.Study(db, None, ["REVA"], None, None, _base(), {k: list(S.DIMS[k]["choices"]) for k in S.ORDER}, n_samples=20)
    bad = []
    cases = [({"stop_loss_pct": 2.0}, "stop_mode"), ({"entry_mode": "index"}, "index_entry_pct"),
             ({"entry_mode": "index"}, "index_exit_pct"), ({"entry_mode": "index"}, "index_min_share"),
             ({"mr_center": "zero"}, "mr_window_days"), ({"mr_center": "zero"}, "mr_horizon_days"),
             ({"mr_center": "zero"}, "mr_min_score"), ({"mr_center": "zero"}, "mr_lag"),
             ({"crash_drop_pct": 1.0}, "crash_window_min"), ({"crash_drop_pct": 1.0}, "crash_cooldown_min"),
             ({"crash_drop_pct": 1.0}, "crash_scope"), ({"session_mode": "fixed"}, "session_start"),
             ({"session_mode": "fixed"}, "session_end"), ({"fill_mode": "best"}, "fill_max_rel_pct"),
             ({"fill_mode": "best"}, "fill_exit_rel_pct")]
    for change, dim in cases:
        off = st._complete({})                           # the form's configuration (everything inactive stays None)
        cand = dict(off)
        cand.update(change)
        full = st._complete(cand)
        if full.get(dim) is None:
            bad.append(f"{list(change)[0]}→{dim}")
    R.add("بهینه‌ساز", "هر پارامتری که در جابه‌جایی فعال شود مقدار مشخص می‌گیرد", not bad, ", ".join(bad) or f"{len(cases)} حالت")



def test_optimizer_fill(R: Results, db):
    """The optimizer evaluates idle-capital parking through its own universe slices: it must give exactly the
    result of the normal backtest (two code paths), and the fill dimensions must be searched and applicable."""
    g = "بهینه‌ساز"
    import discount_validation as V
    base = _base()
    dflt_space = S.default_space()
    info = {d["key"]: d for d in S.space_info()["dims"]}
    R.add(g, "پارامترهای مسجل (مثل fill_mode، crash_scope) از جستجوی پیش‌فرض خارج‌اند ولی قابل انتخاب می‌مانند",
          all(d not in dflt_space and info[d]["settled"] for d in ("fill_mode", "crash_scope", "session_mode", "mr_horizon_days"))
          and "fill_max_rel_pct" in dflt_space and "fill_switch_pct" in dflt_space, "")
    R.add(g, "فیلتر بازگشت به میانگین و معمولِ هر صندوق در جستجوی پیش‌فرض‌اند",
          all(d in dflt_space and not info[d]["settled"] for d in ("mr_center", "mr_window_days", "mr_min_score", "baseline_days")),
          str(sorted(d for d in ("mr_center", "mr_window_days", "mr_min_score", "baseline_days") if d not in dflt_space)))
    space = dict(dflt_space, **{d: list(S.DIMS[d]["choices"]) for d in ("fill_mode", "fill_exit_rel_pct", "mr_center")})
    st = S.Study(db, None, None, None, None, base, space, n_samples=40, blocks=6, min_trades=5,
                 max_seconds=60, max_universes=3, seed=2)
    rng = random.Random(4)
    bad = []
    n_on = 0
    for it in range(4):
        cfg = {d: rng.choice(S.DIMS[d]["choices"]) for d in S.ORDER if d in st.space}
        cfg.update(fill_mode="best", fill_max_rel_pct=rng.choice([-0.1, 0.0, 0.1]), fill_exit_rel_pct=rng.choice([0.2, 0.5]))
        cfg = S._normalize(st._fill(cfg), base)
        if not S._valid(cfg):
            continue
        p = S._to_params(base, cfg)
        full = _run(db, p)
        if "db_raw" not in st.__dict__:
            st.db_raw = S._RawCache(db, None)
            st._setup()
        U = st._universe(p)
        tr = V._sim_all(U, p)
        dd0, dd1 = full["period"]
        fill = st._fill_ctx(U, dd0, dd1)
        _a, s, _c = D._portfolio_summary(tr, p, dd0, dd1, fill=fill)
        n_on += 1
        if abs(s["final_capital"] - full["summary"]["final_capital"]) > 1:
            bad.append(f"{s['final_capital']:,.0f} ≠ {full['summary']['final_capital']:,.0f}")
    R.add(g, "ارزیابی ترکیب با «پر کردن» در مسیر بهینه‌ساز = بک‌تست مستقیم (دو مسیر کدی)", not bad and n_on > 0,
          "؛ ".join(bad[:2]) or f"{n_on} ترکیب")


def test_optimizer(R: Results, db):
    # (b) real run: reported config == simulated config
    base = _base()
    space = S.default_space()
    st = S.Study(db, None, None, None, None, base, space, n_samples=110, blocks=6, min_trades=8, max_seconds=45,
                 max_universes=6, seed=5)
    out = st.run(db)
    empty = [r["cfg"] for r in st.records if any(S._active(d, r["cfg"]) and r["cfg"].get(d) is None
                                                  for d in S.ORDER if d in st.space)]
    R.add("بهینه‌ساز", f"هیچ ترکیبی با پارامتر فعال ولی خالی ثبت نشده ({len(st.records)} ترکیب بررسی شد)", not empty,
          f"{len(empty)} ترکیب معیوب" if empty else "")
    rng = random.Random(1)
    sample = rng.sample(st.records, min(10, len(st.records)))
    d0, d1 = out["setup"]["dates"]
    bad = []
    for rec in sample:
        p = S._to_params(base, rec["cfg"])
        # over the study's period: every configuration is measured from the common end of the warm-up
        full = _run_range(db, p, d0, d1)["summary"]["final_capital"]
        U = st._universe(p)
        import discount_validation as V
        tr = V._sim_all(U, p)
        fill = st._fill_ctx(U, d0, d1) if D.fill_on(p) else None
        _a, s, _c = D._portfolio_summary(tr, p, d0, d1, fill=fill)
        if abs(s["final_capital"] - full) > 1:
            bad.append(f"{rec['cfg']}: {s['final_capital']:,.0f} ≠ {full:,.0f}")
    R.add("بهینه‌ساز", "نتیجهٔ ثبت‌شدهٔ هر ترکیب = اجرای مستقیم همان ترکیب در بک‌تست (دو مسیر کدی)", not bad,
          "؛ ".join(bad) or f"{len(sample)} ترکیب تطبیق داده شد")
    b = out["best"]["cfg"]
    act_missing = [d for d in S.ORDER if d in st.space and S._active(d, b) and b.get(d) is None]
    R.add("بهینه‌ساز", "ترکیب برنده همهٔ پارامترهای فعالش را دارد (اعمال نتیجه همه‌چیز را عوض می‌کند)", not act_missing,
          ", ".join(act_missing))
    # (c) determinism
    st2 = S.Study(db, None, None, None, None, base, space, n_samples=110, blocks=6, min_trades=8, max_seconds=45,
                  max_universes=6, seed=5)
    out2 = st2.run(db)
    R.add("بهینه‌ساز", "با بذر یکسان، برندهٔ یکسان", out["best"]["cfg"] == out2["best"]["cfg"], "")
    # (d) hold-out never influences selection: changing only hold-out data must not change the winner
    #     (checked indirectly: every selection score uses train blocks only)
    tb = out["setup"]["train_blocks"]
    ok = all(abs(r["score"] - (sum(x["ret"] for x in r["blocks"][:tb]) / tb
                               - 0.5 * math.sqrt(sum((x["ret"] - sum(y["ret"] for y in r["blocks"][:tb]) / tb) ** 2
                                                     for x in r["blocks"][:tb]) / tb))) < 1e-6
             for r in st.records[:50] if r["ok"])
    R.add("بهینه‌ساز", "امتیاز انتخاب فقط از بلوک‌های آموزش ساخته می‌شود (آزمون نشت نمی‌کند)", ok, "")
    # (e) the sanity of the verdict: the report must contain every section
    need = ["verdict", "best", "importance", "marginals", "walk_forward", "sensitivity", "neighbours", "generalisation"]
    R.add("بهینه‌ساز", "گزارش همهٔ بخش‌ها را دارد", all(k in out for k in need), ", ".join(k for k in need if k not in out))
    # (f) execution-assumption sensitivity must really change the assumption (it was silently ignored once)
    sens = {x["name"]: x for x in out.get("sensitivity", [])}
    b0, b3 = sens.get("فرض‌های فعلی"), sens.get("اسپرد ×۳")
    R.add("بهینه‌ساز", "حساسیت برنده به اسپرد واقعاً اسپرد را عوض می‌کند (اسپرد ×۳ ≠ پایه)",
          b0 is not None and b3 is not None and (b0["train"], b0["hold"]) != (b3["train"], b3["hold"]),
          f"{b0} / {b3}")


# --------------------------------------------------------------------------- #
#  5) Invariants over random settings                                          #
# --------------------------------------------------------------------------- #

def _random_params(rng: random.Random) -> D.DiscountParams:
    c = {d: rng.choice(S.DIMS[d]["choices"]) for d in S.ORDER}
    c["fill_mode"] = "off"                                    # parking has its own tests (its trades break the entry-rule invariants)
    c["exec_delay_snaps"] = 0                                 # the invariants below are stated at the signal's own snapshot
    c["participation_basis"] = "day"                          # ... and with the whole-day volume cap (see test_realism for the rest)
    c = S._normalize(c, _base())
    if not S._valid(c):
        c["exit_discount_pct"] = -0.5
        c["index_exit_pct"] = -0.1
    p = S._to_params(_base(), c)
    return replace(p, participation_pct=rng.choice([0.0, 2.0, 10.0]), half_spread_pct=rng.choice([0.0, 0.05, 0.2]),
                   session_mode=rng.choice(["auto", "auto", "fixed"]))


def test_invariants(R: Results, db, n: int = 24):
    rng = random.Random(3)
    raw = {sid: db.get_nav_intraday(sid) for sid, _s, _c in FUNDS}
    names = {s: i for i, s, _c in FUNDS}
    wins = {s: D._day_windows(raw[i]) for s, i in names.items()}
    raw_by_key = {s: {(r[0], r[1]): r for r in raw[i]} for s, i in names.items()}
    by_sym_vol = {}
    for s, i in names.items():
        dv = {}
        for r in raw[i]:
            dv[r[0]] = max(dv.get(r[0], 0), r[5] or 0)
        by_sym_vol[s] = dv
    fails: dict[str, list] = {}

    def fail(key, msg):
        fails.setdefault(key, []).append(msg)

    n_trades = 0
    for it in range(n):
        p = _random_params(rng)
        tag = f"#{it}"
        try:
            res = _run(db, p)
        except Exception as e:
            fail("اجرای بک‌تست بدون کرش", f"{tag}: {type(e).__name__}: {e}")
            continue
        s, tr = res["summary"], res["trades"]
        n_trades += len(tr)
        net = sum(t["net_pnl"] for t in tr)
        if abs((s["final_capital"] - p.initial_capital) - net) > max(100, 1e-6 * p.initial_capital):
            fail("اتحاد حسابداری (سرمایهٔ نهایی − اولیه = جمع سود معاملات)", f"{tag}: {s['final_capital'] - p.initial_capital - net:.0f}")
        e = res["exposure"]
        if e and not (0 <= e["avg_pct"] <= 100.0001 and e["avg_pct"] <= e["peak_pct"] + 1e-6 and e["peak_pct"] <= 100.0001):
            fail("سرمایهٔ درگیر بین ۰ و ۱۰۰٪ و میانگین ≤ اوج", f"{tag}: avg {e['avg_pct']} peak {e['peak_pct']}")
        if e and tr:
            ind = sum(t["buy_notional"] * ((D._ord(t["exit_date"]) * 86400 + D._sec(t["exit_time"]))
                                           - (D._ord(t["entry_date"]) * 86400 + D._sec(t["entry_time"]))) for t in tr)
            if ind and abs(e["invested_rial_seconds"] / ind - 1) > 3e-3:
                fail("انتگرال سرمایهٔ درگیر = جمع مستقل معاملات", f"{tag}: نسبت {e['invested_rial_seconds'] / ind:.4f}")
        for t in tr:
            if t.get("origin") == "fill":
                continue
            key = (t["entry_date"], t["entry_time"])
            if (t["exit_date"], t["exit_time"]) < key:
                fail("خروج پس از ورود", f"{tag} {t['symbol']}")
            if t["volume"] < 0 or t["buy_notional"] <= 0 or t["sell_notional"] <= 0:
                fail("مبلغ خرید و فروش مثبت (حجم کسری در معاملهٔ کوچک ممکن است ۰ نمایش داده شود؛ سود خطی است)", f"{tag} {t['symbol']}")
            net_calc = t["sell_notional"] * (1 - p.sell_fee) - t["buy_notional"] * (1 + p.buy_fee)
            if abs(net_calc - t["net_pnl"]) > 3:
                fail("هویت کارمزد: سود = فروش×(۱−کارمزد) − خرید×(۱+کارمزد)", f"{tag} {t['symbol']}: {net_calc - t['net_pnl']:.1f}")
            if p.session_mode == "auto":
                w = wins[t["symbol"]].get(t["entry_date"])
                if w is None or not (w[0] <= t["entry_time"] <= w[1]):
                    fail("ورود فقط داخل بازهٔ واقعی معامله (نه پیش‌گشایش/بعد از بسته‌شدن)", f"{tag} {t['symbol']} {t['entry_date']} {t['entry_time']} پنجره {w}")
            else:
                if not (p.session_start <= t["entry_time"] <= p.session_end):
                    fail("ورود فقط داخل ساعت ثابت", f"{tag} {t['symbol']} {t['entry_time']}")
            if p.participation_pct > 0:
                cap = by_sym_vol[t["symbol"]].get(t["entry_date"], 0) * p.participation_pct / 100.0
                if t["volume"] > cap + 1:
                    fail("سقف سهم از حجم روز رعایت می‌شود", f"{tag} {t['symbol']}: {t['volume']} > {cap:.0f}")
            if t["exit_reason"] == "stop" and p.stop_loss_pct <= 0:
                fail("خروج با حد ضرر فقط وقتی حد ضرر روشن است", f"{tag} {t['symbol']}")
            if t["exit_reason"] == "time" and p.max_hold_days > 0 and t["hold_days"] < p.max_hold_days:
                fail("خروج زمانی فقط پس از سقف نگه‌داری", f"{tag} {t['symbol']}: {t['hold_days']} < {p.max_hold_days}")
            if p.mr_center != "off" and (t["mr_score"] is None or t["mr_score"] < p.mr_min_score - 0.06):
                fail("فیلتر بازگشت: امتیاز هر معامله ≥ حداقل", f"{tag} {t['symbol']}: {t['mr_score']} < {p.mr_min_score}")
            if p.entry_mode in ("index", "both"):
                if t["idx_entry_pct"] is None or t["idx_entry_pct"] > -p.index_entry_pct + 0.01:
                    fail("ورود با شاخص: مقدار شاخص هنگام ورود زیر آستانه است", f"{tag} {t['symbol']}: {t['idx_entry_pct']} > {-p.index_entry_pct}")
            if p.entry_mode in ("fund", "both"):
                lim = -p.entry_discount_pct + p.half_spread_pct * 2 + 0.05     # ask price includes the half spread
                if t["rel_entry_pct"] > lim:
                    fail("ورود هر صندوق: تخفیف نسبت به معمول زیر آستانه است", f"{tag} {t['symbol']}: {t['rel_entry_pct']} > {lim:.2f}")
            rr = raw_by_key[t["symbol"]].get((t["entry_date"], t["entry_time"]))
            if rr is not None:
                _d, _t, _n, nd, _l, _v, nt = rr
                if p.max_nav_age_days >= 0 and nd and D._ord(t["entry_date"]) - D._ord(nd) > p.max_nav_age_days:
                    fail("ورود فقط با NAV نه‌چندان کهنه (روز)", f"{tag} {t['symbol']}: NAV از {nd}")
                if p.max_nav_age_min > 0 and nd and nt:
                    age = (D._ord(t["entry_date"]) * 86400 + D._sec(t["entry_time"])) - (D._ord(nd) * 86400 + D._sec(nt))
                    if age > p.max_nav_age_min * 60:
                        fail("ورود فقط با NAV نه‌چندان کهنه (دقیقه)", f"{tag} {t['symbol']}: سن NAV {age / 60:.0f} دقیقه > {p.max_nav_age_min}")
    for k in ["اجرای بک‌تست بدون کرش", "اتحاد حسابداری (سرمایهٔ نهایی − اولیه = جمع سود معاملات)",
              "سرمایهٔ درگیر بین ۰ و ۱۰۰٪ و میانگین ≤ اوج", "انتگرال سرمایهٔ درگیر = جمع مستقل معاملات",
              "خروج پس از ورود", "مبلغ خرید و فروش مثبت (حجم کسری در معاملهٔ کوچک ممکن است ۰ نمایش داده شود؛ سود خطی است)", "هویت کارمزد: سود = فروش×(۱−کارمزد) − خرید×(۱+کارمزد)",
              "ورود فقط داخل بازهٔ واقعی معامله (نه پیش‌گشایش/بعد از بسته‌شدن)", "ورود فقط داخل ساعت ثابت",
              "سقف سهم از حجم روز رعایت می‌شود", "خروج با حد ضرر فقط وقتی حد ضرر روشن است",
              "خروج زمانی فقط پس از سقف نگه‌داری", "فیلتر بازگشت: امتیاز هر معامله ≥ حداقل",
              "ورود با شاخص: مقدار شاخص هنگام ورود زیر آستانه است",
              "ورود هر صندوق: تخفیف نسبت به معمول زیر آستانه است"]:
        f = fails.get(k, [])
        R.add("ناوردایی‌ها", k, not f, (f"{len(f)} مورد؛ نمونه: " + " | ".join(f[:3])) if f else f"{n} تنظیم تصادفی، {n_trades} معامله")



# --------------------------------------------------------------------------- #
#  5b) Market-fall filter                                                      #
# --------------------------------------------------------------------------- #

def _brute_crash(rows_by_fund, groups, W, C):
    """Naive O(n^2) re-implementation of discount_backtest._crash_series (independent check)."""
    mins = [[D._minutes(r[1], r[2]) for r in rows] for rows in rows_by_fund]
    ret = []
    for k, rows in enumerate(rows_by_fund):
        rk = []
        for i in range(len(rows)):
            val = None
            for j in range(i - 1, -1, -1):
                if mins[k][j] <= mins[k][i] - W:
                    val = rows[i][4] / rows[j][4] - 1.0
                    break
            rk.append(val)
        ret.append(rk)
    members = {}
    for k, g in enumerate(groups):
        members.setdefault(g, []).append(k)
    stamps = sorted({m for mk in mins for m in mk})
    gret = {}                                                   # (group, T) -> group return or None
    for T in stamps:
        for g, ks in members.items():
            vals = []
            for k in ks:
                for i in range(len(mins[k]) - 1, -1, -1):
                    if mins[k][i] <= T and ret[k][i] is not None:
                        if T - mins[k][i] <= W:
                            vals.append(ret[k][i])
                        break
            need = max(1, (len(ks) + 1) // 2)
            gret[(g, T)] = sum(vals) / len(vals) if len(vals) >= need else None
    out = []
    for k, rows in enumerate(rows_by_fund):
        ok = []
        for i in range(len(rows)):
            T = mins[k][i]
            cand = [gret[(groups[k], t)] for t in stamps if T - C <= t <= T and gret[(groups[k], t)] is not None]
            ok.append(min(cand) if cand else None)
        out.append(ok)
    return out


def test_crash_filter(R: Results, db):
    g = "فیلتر ریزش بازار"
    names = {s: i for i, s, _c in FUNDS}
    cats = {s: c for _i, s, c in FUNDS}
    start, end = 20260301, 20260420                       # a slice with market-wide shock days, keeps the naive check fast
    for scope in ("all", "category"):
        p = _base(crash_drop_pct=0.5, crash_window_min=30, crash_cooldown_min=60, crash_scope=scope)
        loaded = []
        for sid, sym, _c in FUNDS:
            d = D._load_ex(db, sid, start, end, p)
            d["label"] = sym
            loaded.append(d)
        groups = [cats[l["label"]] if scope == "category" else "all" for l in loaded]
        fast = D._crash_series([l["rows"] for l in loaded], groups, p)
        slow = _brute_crash([l["rows"] for l in loaded], groups, 30, 60)
        bad = 0
        for fa, sl in zip(fast, slow):
            for a, b in zip(fa, sl):
                if (a is None) != (b is None) or (a is not None and abs(a - b) > 1e-12):
                    bad += 1
        n = sum(len(x) for x in fast)
        R.add(g, f"سری ریزشِ سریع = پیاده‌سازی ساده و مستقل (گروه «{scope}»، {n} لحظه)", bad == 0, f"{bad} اختلاف" if bad else "یکسان")
    # no trade may start while the filter says "blocked"
    for scope in ("all", "category"):
        p = _base(crash_drop_pct=0.4, crash_window_min=30, crash_cooldown_min=45, crash_scope=scope)
        res = _run(db, p)
        loaded = []
        for sid, sym, _c in FUNDS:
            d = D._load_ex(db, sid, None, None, p)
            d["label"] = sym
            loaded.append(d)
        groups = [cats[l["label"]] if scope == "category" else "all" for l in loaded]
        cr = D._crash_series([l["rows"] for l in loaded], groups, p)
        pos = {l["label"]: {(r[1], r[2]): i for i, r in enumerate(l["rows"])} for l in loaded}
        bad = []
        for t in res["trades"]:
            i = pos[t["symbol"]].get((t["entry_date"], t["entry_time"]))
            k = next(j for j, l in enumerate(loaded) if l["label"] == t["symbol"])
            v = cr[k][i] if i is not None else None
            if v is not None and v <= -p.crash_drop_pct / 100.0:
                bad.append(f"{t['symbol']} {t['entry_date']} {t['entry_time']}: {v:.4f}")
        off = _run(db, _base(crash_scope=scope))
        R.add(g, f"هیچ معامله‌ای وسط ریزش شروع نمی‌شود (گروه «{scope}»؛ {len(res['trades'])} معامله از {len(off['trades'])})",
              not bad and len(res["trades"]) < len(off["trades"]), "؛ ".join(bad[:3]) or "درست")
    # thresholds: blocked share never grows with a larger threshold; an enormous one equals "off"
    shares = [_run(db, _base(crash_drop_pct=x, crash_scope="all"))["crash_info"]["blocked_share_pct"] for x in (0.2, 0.5, 1.0, 2.0)]
    R.add(g, "با بزرگ‌تر شدن آستانه، سهم لحظه‌های ممنوع کم می‌شود", all(a >= b for a, b in zip(shares, shares[1:])), str(shares))
    a = _sig(_run(db, _base(crash_drop_pct=80.0)))
    b = _sig(_run(db, _base()))
    R.add(g, "آستانهٔ بی‌نهایت بزرگ = فیلتر خاموش", a == b, "")
    # exits and open positions are never touched: the filter only gates NEW entries
    r_on = _run(db, _base(crash_drop_pct=0.4, crash_scope="all"))
    R.add(g, "پوزیشن‌هایی که باز شده‌اند همیشه بسته می‌شوند (فیلتر فقط ورود را می‌بندد)",
          all(t["exit_date"] >= t["entry_date"] for t in r_on["trades"]) and r_on["summary"]["trade_count"] > 0, "")


# --------------------------------------------------------------------------- #
#  5c) Idle-capital parking                                                    #
# --------------------------------------------------------------------------- #

def _cash_path(res: dict, p: D.DiscountParams):
    ev = []
    for t in res["trades"]:
        ev.append((D._ord(t["entry_date"]) * 86400 + D._sec(t["entry_time"]), 1, -t["buy_notional"] * (1 + p.buy_fee)))
        ev.append((D._ord(t["exit_date"]) * 86400 + D._sec(t["exit_time"]), 0, t["sell_notional"] * (1 - p.sell_fee)))
    ev.sort(key=lambda x: (x[0], x[1]))                      # exits before entries at the same instant
    cash = p.initial_capital
    lo = cash
    for _ts, _k, v in ev:
        cash += v
        lo = min(lo, cash)
    return lo, cash


def test_hold(R: Results, db):
    """fill_mode == "hold": always invested; a position is sold ONLY to switch into a clearly better candidate."""
    g = "نگه‌داری تا کاندیدای بهتر"
    rng = random.Random(21)
    bad_cash, bad_reason, bad_switch, bad_exp, n_tr, n_rot, n_run = [], [], [], [], 0, 0, 8
    for it in range(n_run):
        p = _random_params(rng)
        p = replace(p, fill_mode="hold", fill_max_rel_pct=rng.choice([0.0, 0.5, 2.0]),
                    fill_switch_pct=rng.choice([0.2, 0.5, 1.5]), participation_pct=rng.choice([0.0, 5.0]),
                    position_pct=rng.choice([10, 25, 50]))
        res = _run(db, p)
        lo, cash_final = _cash_path(res, p)
        if lo < -1e-6 * p.initial_capital - 1:
            bad_cash.append(f"#{it}: {lo / p.initial_capital * 100:.3f}٪")
        if abs(cash_final - res["summary"]["final_capital"]) > max(50, 1e-6 * p.initial_capital):
            bad_cash.append(f"#{it}: مسیر نقد ≠ گزارش")
        hs2 = p.half_spread_pct * 2
        entries = {}
        for t in res["trades"]:
            entries.setdefault((D._ord(t["entry_date"]) * 86400 + D._sec(t["entry_time"])), []).append(t)
        for t in res["trades"]:
            n_tr += 1
            if t["exit_reason"] not in ("rotate", "end"):
                bad_reason.append(f"#{it} {t['symbol']}: {t['exit_reason']}")
                continue
            if t["exit_reason"] != "rotate":
                continue
            n_rot += 1
            ts = D._ord(t["exit_date"]) * 86400 + D._sec(t["exit_time"])
            # a switch is only legal if some other fund was bought at that very instant, clearly cheaper
            cands = [e for e in entries.get(ts, []) if e["symbol"] != t["symbol"]]
            if not cands or not any(e["rel_entry_pct"] <= t["rel_exit_pct"] + hs2 - p.fill_switch_pct + 0.02 for e in cands):
                bad_switch.append(f"#{it} {t['symbol']} @{t['exit_date']} {t['exit_time']}")
        e = res["exposure"]
        if not (0 <= e["avg_pct"] <= 100.0001):
            bad_exp.append(f"#{it}: {e['avg_pct']}")
    R.add(g, "نقد هرگز منفی نمی‌شود و سرمایهٔ نهایی = مسیر نقدِ مستقل", not bad_cash, "؛ ".join(bad_cash[:3]) or f"{n_run} تنظیم تصادفی")
    R.add(g, "تنها علت‌های خروج «جابه‌جایی» و «پایان داده» است (نه زمان، نه حد ضرر، نه سیگنال فروش)",
          not bad_reason and n_tr > 0, "؛ ".join(bad_reason[:3]) or f"{n_tr} معامله")
    R.add(g, "هر جابه‌جایی همان لحظه به صندوقی با حبابِ دست‌کم «برتری» کمتر رفته است",
          not bad_switch and n_rot > 0, "؛ ".join(bad_switch[:3]) or f"{n_rot} جابه‌جایی")
    base = _base(position_pct=25)
    rest = _run(db, replace(base, fill_mode="hold", fill_max_rel_pct=2.0, fill_switch_pct=0.5))
    r0 = _run(db, replace(base, fill_mode="off"))
    R.add(g, "سرمایهٔ درگیر در «نگه‌داری» از حالت عادی بیشتر است", rest["summary"]["avg_exposure_pct"] > r0["summary"]["avg_exposure_pct"],
          f"{r0['summary']['avg_exposure_pct']}→{rest['summary']['avg_exposure_pct']}٪")
    # minimum holding time: no switch-out earlier than N minutes after the purchase
    mh = 600
    res_mh = _run(db, replace(base, fill_mode="hold", fill_max_rel_pct=2.0, fill_switch_pct=0.3, fill_min_hold_min=mh))
    short = []
    for t in res_mh["trades"]:
        if t["exit_reason"] != "rotate":
            continue
        a = D._ord(t["entry_date"]) * 86400 + D._sec(t["entry_time"])
        b = D._ord(t["exit_date"]) * 86400 + D._sec(t["exit_time"])
        if b - a < mh * 60:
            short.append(f"{t['symbol']} {t['entry_date']} {t['entry_time']}→{t['exit_time']}")
    base_rot = sum(1 for t in _run(db, replace(base, fill_mode="hold", fill_max_rel_pct=2.0, fill_switch_pct=0.3))["trades"]
                   if t["exit_reason"] == "rotate")
    n_rot_mh = sum(1 for t in res_mh["trades"] if t["exit_reason"] == "rotate")
    R.add(g, "با «حداقل زمان نگه‌داری» هیچ جابه‌جایی زودتر از آن انجام نمی‌شود و تعداد جابه‌جایی‌ها کمتر می‌شود",
          not short and n_rot_mh < base_rot, "؛ ".join(short[:3]) or f"جابه‌جایی {base_rot}→{n_rot_mh}")
    # sizing / liquidity diagnostics now exist for the parker's positions
    si = rest["summary"].get("size_info", {})
    R.add(g, "آمار اندازهٔ پوزیشن و سقف حجم برای حالت «نگه‌داری» گزارش می‌شود",
          si.get("positions", 0) > 0 and "switch_sales" in si and si.get("avg_size_ratio_pct") is not None, str(si)[:160])
    tight = _run(db, replace(base, fill_mode="hold", fill_max_rel_pct=2.0, participation_pct=0.5))["summary"]["size_info"]
    R.add(g, "با سقف حجم تنگ، سهم «محدود به حجم» و فروش‌های بیش از سقف دیده می‌شود",
          tight["limited_by_volume"] > 0 or tight["switch_sales_over_cap"] > 0 or tight["switch_blocked_by_volume"] > 0, str(tight)[:160])
    huge = _run(db, replace(base, fill_mode="hold", fill_max_rel_pct=2.0, fill_switch_pct=1000.0))
    R.add(g, "با برتریِ خیلی بزرگ هرگز جابه‌جایی نمی‌شود (فقط «پایان داده»)",
          all(t["exit_reason"] == "end" for t in huge["trades"]) and huge["trades"], "")


class FakeTSE:
    """Stands in for TSETMCFetcher: answers the search / trade-history / best-limits URLs with synthetic JSON.
    ``fail`` = set of (ins_code, date, kind) that answer None (a failed request)."""

    def __init__(self, fail=None):
        self.fail = set(fail or [])
        self.calls = 0

    def _get(self, url, silent=False, html=False, retries=3):
        self.calls += 1
        parts = url.split("/")
        if "GetInstrumentSearch" in url:
            from urllib.parse import unquote
            sym = unquote(parts[-1])
            return {"instrumentSearch": [
                {"insCode": "999" + sym, "lVal18AFC": sym + "x", "lVal30": "something else"},
                {"insCode": "111" + sym, "lVal18AFC": sym, "lVal30": "صندوق طلای " + sym}]}
        if "GetTradeHistory" in url:
            ins, d = parts[-3], int(parts[-2])
            if (ins, d, "trades") in self.fail:
                return None
            return {"tradeHistory": [{"nTran": k + 1, "hEven": 120000 + k * 100, "pTran": 1000 + k, "qTitTran": 10 * (k + 1),
                                      "canceled": 0} for k in range(5)]}
        if "/BestLimits/" in url:
            ins, d = parts[-2], int(parts[-1])
            if (ins, d, "book") in self.fail:
                return None
            return {"bestLimitsHistory": [{"refID": 100 + k, "hEven": 115900 + k * 10, "number": 1 + k % 5,
                                           "pMeDem": 990, "qTitMeDem": 5, "zOrdMeDem": 1, "pMeOf": 1010,
                                           "qTitMeOf": 7, "zOrdMeOf": 2} for k in range(10)]}
        return None


def test_tse_collect(R: Results, db_path: str):
    """TSE tick / order-book collection: codes, storage, resumability, failures, stop."""
    import shutil as _sh
    import threading as _th
    import tse_gold as G
    g = "جمع‌آوری دادهٔ TSE (طلا)"
    p2 = db_path + ".tse.db"
    _sh.copyfile(db_path, p2)
    db = _db(p2)
    funds = G.gold_funds(db)
    R.add(g, "صندوق‌های طلا از دادهٔ NAV شناخته می‌شوند", sorted(f["symbol"] for f in funds) == ["GOLDX", "GOLDY", "GOLDZ"],
          str([f["symbol"] for f in funds]))
    days = {f["symbol_id"]: G.dump_dates(db, f["symbol_id"]) for f in funds}
    d_fail = days[funds[0]["symbol_id"]][3]
    fake = FakeTSE(fail={("111GOLDX", d_fail, "trades")})
    res = G.collect(db, fake, workers=2)
    ins = G.get_ins_map(db)
    R.add(g, "کد TSE: نماد دقیق + «صندوق» در نام انتخاب می‌شود (نه نماد مشابه)",
          all(ins[f["symbol_id"]]["ins_code"] == "111" + f["symbol"] for f in funds), str({k: v["ins_code"] for k, v in ins.items()}))
    want = sum(len(v) for v in days.values()) * 2
    R.add(g, "برای هر روزِ دادهٔ NAV، معاملات و اردربوک گرفته و ذخیره می‌شود؛ درخواست ناموفق ذخیره نمی‌شود",
          res["total"] == want and res["failed"] == 1 and res["done"] == want, str({k: res[k] for k in ("total", "done", "failed")}))
    rows = G.load_day(db, funds[0]["symbol_id"], days[funds[0]["symbol_id"]][0], "book")
    R.add(g, "داده بدون اتلاف ذخیره و بازخوانی می‌شود (فیلدهای اردربوک و ترتیب refID)",
          rows is not None and len(rows) == 10 and rows[0][:3] == [100, 115900, 1] and rows[0][3:] == [990, 5, 1, 1010, 7, 2], str((rows or [])[:1]))
    todo = G.plan(db)
    R.add(g, "ادامه‌پذیر: در اجرای دوم فقط همان روزِ ناموفق باقی است", todo == [(funds[0]["symbol_id"], "GOLDX", d_fail, "trades")], str(todo[:3]))
    calls0 = fake.calls
    fake.fail.clear()
    res2 = G.collect(db, fake, workers=2)
    R.add(g, "اجرای دوم فقط درخواستِ باقی‌مانده را می‌زند", res2["total"] == 1 and res2["failed"] == 0 and fake.calls - calls0 <= 2,
          f"{res2['total']} کار · {fake.calls - calls0} درخواست")
    cov = {r["symbol"]: r for r in G.coverage(db)}
    R.add(g, "جدول پوشش: روزهای NAV = روزهای تیک = روزهای اردربوک", all(
        r["dump_days"] == r["trades_days"] == r["book_days"] > 0 for r in cov.values()), str({k: (v["dump_days"], v["trades_days"], v["book_days"]) for k, v in cov.items()}))
    # stop: a fresh DB copy, stop immediately → nothing (or almost nothing) fetched, and resumable afterwards
    p3 = db_path + ".tse2.db"
    _sh.copyfile(db_path, p3)
    db3 = _db(p3)
    ev = _th.Event()
    ev.set()
    r3 = G.collect(db3, FakeTSE(), stop=ev)
    R.add(g, "توقف: کاری انجام نمی‌شود و بعداً همه باقی است", r3["stopped"] and len(G.plan(db3)) == want, f"{len(G.plan(db3))}/{want}")
    G.set_ins(db3, funds[0]["symbol_id"], "GOLDX", "MANUAL1", "", "manual")
    G.resolve_ins_codes(db3, FakeTSE(), funds, refresh=True)
    R.add(g, "کد دستی با جستجوی دوباره پاک نمی‌شود", G.get_ins_map(db3)[funds[0]["symbol_id"]]["ins_code"] == "MANUAL1", "")
    for pth in (p2, p3):
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(pth + ext)
            except OSError:
                pass


def _hms_add(t: int, sec: int) -> int:
    s = min(86399, max(0, (t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100 + sec))
    return (s // 3600) * 10000 + (s // 60 % 60) * 100 + s % 60


def _fake_tse_from_dump(db, n_days: int | None = None, half: float = 0.001):
    """Write synthetic TSE data for the gold funds, consistent with the dump: at every dump snapshot where the
    volume grew a trade at its price, and a 3-level book around it (bid_k / ask_k = price·(1 ∓ k·half),
    depth ≈ 1e9 rial per level). Book events are stamped 1 s before the snapshot so they are in place at it."""
    import tse_gold as G
    G.ensure_schema(db)
    out = {}
    for f in G.gold_funds(db):
        sid = f["symbol_id"]
        G.set_ins(db, sid, f["symbol"], "111" + f["symbol"], "fake", "manual")
        days = G.dump_dates(db, sid)[: n_days or None]
        raw = db.get_nav_intraday(sid, days[0], days[-1])
        by_day = {}
        for r in raw:
            by_day.setdefault(r[0], []).append(r)
        for d in days:
            trades, book, prev, seq, ref = [], [], 0, 0, 0
            for (_d, t, _nav, _nd, last, vol, _nt) in by_day.get(d, []):
                vol = vol or 0
                if not last or last <= 0:
                    continue
                s = (t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100 - 1
                tb = (s // 3600) * 10000 + (s // 60 % 60) * 100 + s % 60
                for k in (1, 2, 3):
                    ref += 1
                    q = int(1e9 / last) * k
                    book.append([ref, tb, k, round(last * (1 - k * half), 4), q, 1, round(last * (1 + k * half), 4), q, 1])
                if vol > prev:
                    seq += 1
                    trades.append([seq, t, last, vol - prev, 0])
                prev = max(prev, vol)
            G.store_day(db, sid, d, "trades", "111" + f["symbol"], trades)
            G.store_day(db, sid, d, "book", "111" + f["symbol"], book)
        out[sid] = days
    return out


def test_tse_dataset(R: Results, db_path: str):
    """Backtest on the TSE dataset: grid building, NAV join without look-ahead, real-book fills, period = TSE days."""
    import shutil as _sh
    import tse_dataset as TD
    import tse_gold as G
    g = "بک‌تست روی دادهٔ TSE (تیک و اردربوک)"
    p2 = db_path + ".tseds.db"
    _sh.copyfile(db_path, p2)
    db = _db(p2)
    days = _fake_tse_from_dump(db, n_days=90)
    funds = G.gold_funds(db)
    sid = funds[0]["symbol_id"]
    raw, book = TD.load_raw(db, sid, None, None, 300)
    dump = db.get_nav_intraday(sid)
    dkey = [(r[0], r[1]) for r in dump]
    bad = []
    for (d, t, nav, nav_d, mid, cum, nav_t) in raw[::7]:
        j = bisect.bisect_right(dkey, (d, t)) - 1
        if j < 0 or dump[j][2] != nav:
            bad.append(f"NAV {d} {t}")
        bids, asks = book[(d, t)]
        if abs(mid - (bids[0][0] + asks[0][0]) / 2) > 1e-6 * mid:
            bad.append(f"mid {d} {t}")
        # the book at t = the one built at the dump snapshot at or before t (no look-ahead)
        k = bisect.bisect_right(dkey, (d, t)) - 1
        if abs(mid - dump[k][4]) > 1e-6 * mid and dump[k][0] == d:
            bad.append(f"book≠snapshot {d} {t}")
    R.add(g, "ساخت شبکه: NAV = آخرین اسنپ‌شاتِ NAV تا همان لحظه، mid = میانهٔ بهترین صف‌ها، بدون نگاه به آینده",
          not bad and len(raw) > 100, "؛ ".join(bad[:3]) or f"{len(raw)} ردیف")
    R.add(g, "فقط روزهای دارای دادهٔ TSE استفاده می‌شوند", sorted({r[0] for r in raw}) == days[sid], "")
    base = _base(entry_discount_pct=0.3, dataset="tse", tse_grid_sec=300, half_spread_pct=0.05)
    res = _run(db, base)
    s = res["summary"]
    lv = {}
    for f in funds:
        _r, bk = TD.load_raw(db, f["symbol_id"], None, None, 300)
        lv[f["symbol"]] = bk
    bad, n = [], 0
    for t in res["trades"]:
        b = lv[t["symbol"]].get((t["entry_date"], t["entry_time"]))
        x = lv[t["symbol"]].get((t["exit_date"], t["exit_time"]))
        if not b or not x:
            continue
        n += 1
        if not (b[1][0][0] - 0.01 <= t["entry_price"] <= b[1][-1][0] + 0.01):
            bad.append(f"خرید {t['symbol']} {t['entry_price']} خارج از صف فروش {b[1][0][0]}–{b[1][-1][0]}")
        if not (x[0][-1][0] - 0.01 <= t["exit_price"] <= x[0][0][0] + 0.01):
            bad.append(f"فروش {t['symbol']} {t['exit_price']} خارج از صف خرید")
        if t["spread_in"] is None or t["spread_in"] < 0.0009:
            bad.append(f"spread_in {t['spread_in']}")
    R.add(g, "خرید با قیمت صف فروش و فروش با قیمت صف خرید (نه اسپرد فرضی)؛ هزینهٔ بالای میانه ثبت می‌شود",
          not bad and n > 0 and res["dataset"]["name"] == "tse", "؛ ".join(bad[:3]) or f"{n} معامله")
    R.add(g, "دورهٔ بک‌تست = روزهای دادهٔ TSE", res["period"][0] >= min(min(v) for v in days.values())
          and res["period"][1] <= max(max(v) for v in days.values()) and res["dataset"]["days"] <= 90, str(res["period"]))
    T = res["attribution"]["totals"]
    R.add(g, "تفکیک سود با قیمت‌های واقعی هم دقیقاً جمع می‌شود", abs(T["nav"] + T["bubble"] - T["spread"] - T["fees"] - T["net"])
          <= max(50.0, 2.0 * len(res["trades"])), str(T))
    hp = _base(fill_mode="hold", fill_max_rel_pct=2.0, position_pct=25, dataset="tse", tse_grid_sec=300)
    rh = _run(db, hp)
    lo, cash_final = _cash_path(rh, hp)
    bad = []
    for t in rh["trades"]:
        b = lv[t["symbol"]].get((t["entry_date"], t["entry_time"]))
        if b and t["volume"] > sum(v for _p, v in b[1]) + 1:
            bad.append(f"{t['symbol']}: خرید {t['volume']} > عمق صف {sum(v for _p, v in b[1])}")
    R.add(g, "حالت «همیشه سرمایه‌گذاری» روی TSE: هیچ خریدی از عمق صف بزرگ‌تر نیست، نقد منفی نمی‌شود",
          not bad and lo > -1e-6 * hp.initial_capital - 1 and abs(cash_final - rh["summary"]["final_capital"]) < max(50, 1e-6 * hp.initial_capital)
          and len(rh["trades"]) > 0, "؛ ".join(bad[:3]) or f"{len(rh['trades'])} معامله")
    r_dump = _run(db, replace(base, dataset="dump"))
    R.add(g, "دو دیتاست نتیجهٔ متفاوت می‌دهند و انتخاب دیتاست در نتیجه ثبت می‌شود",
          _sig(r_dump) != _sig(res) and r_dump["dataset"]["name"] == "dump", "")
    # hybrid: the dump's snapshots decide, the TSE book at that very instant executes
    rhy = _run(db, replace(base, dataset="hybrid"))
    dump_keys = {f["symbol"]: {(r[0], r[1]): r for r in db.get_nav_intraday(f["symbol_id"])} for f in funds}
    bad, n = [], 0
    for t in rhy["trades"]:
        r = dump_keys[t["symbol"]].get((t["entry_date"], t["entry_time"]))
        if r is None:
            bad.append(f"{t['symbol']} {t['entry_date']} {t['entry_time']}: زمان ورود اسنپ‌شات NAV نیست")
            continue
        n += 1
        last = r[4]
        lo_, hi_ = last * (1 + 0.001), last * (1 + 0.003)              # the fake asks at that snapshot
        if not (lo_ - 0.02 <= t["entry_price"] <= hi_ + 0.02):
            bad.append(f"{t['symbol']}: خرید {t['entry_price']} خارج از صف فروش {lo_:.2f}–{hi_:.2f}")
    R.add(g, "حالت «سیگنال از دادهٔ NAV، معامله با اردربوک»: ورود در زمان اسنپ‌شات NAV و با قیمت صف فروشِ همان لحظه",
          not bad and n > 0 and rhy["dataset"]["name"] == "hybrid" and _sig(rhy) != _sig(r_dump),
          "؛ ".join(bad[:3]) or f"{n} معامله")
    # order-book datasets: the assumed spread plays no part; every buy is inside the asks and every sale inside the
    # bids of the book at that very instant (also a switch sale, whose instant is the candidate's snapshot)
    # the hybrid book is precomputed once and stored: a fresh process (memo cleared) reads it back identically,
    # and the book at any collected fund's snapshot instant equals a replay of the raw event stream
    with db._conn() as conn:
        n_store = conn.execute("SELECT COUNT(*) FROM tse_hybrid").fetchone()[0]
    TD._LOADS.clear()
    rhy2 = _run(db, replace(base, dataset="hybrid"))
    f0 = funds[0]["symbol_id"]
    _r, bk0 = TD.load_hybrid(db, f0, None, None)
    d0_ = sorted({k[0] for k in bk0.keys()})[3]
    inst = TD._union_times(db, [d0_])[d0_]
    ev = G.load_day(db, f0, d0_, "book")
    rep_ = TD._book_states(ev, [TD._sec(t) for t in inst])
    bad = [t for t, st in zip(inst, rep_) if [list(map(list, x)) for x in bk0.at(d0_, t)] != [list(map(list, x)) for x in st]]
    R.add(g, "اردربوک ترکیبی یک بار ساخته و ذخیره می‌شود؛ خواندن دوباره همان نتیجه را می‌دهد و با بازپخش رویدادها یکی است",
          n_store > 0 and _sig(rhy2) == _sig(rhy) and not bad and len(inst) > 0,
          f"{n_store} روز-صندوق ذخیره؛ {len(inst)} لحظه؛ ناهمخوان: {bad[:3]}")
    # double confirmation: the signal holds on the last price AND at the head of the opposite queue
    hpc = _base(fill_mode="hold", fill_max_rel_pct=0.0, position_pct=25, dataset="hybrid")
    rc = _run(db, hpc)
    rowmap = {}
    for f in funds:
        L = D._load_ex(db, f["symbol_id"], None, None, hpc)
        for r in L["rows"]:
            rowmap[(f["symbol"], r[1], r[2])] = r
            bk = L["book"]
    bad, n = [], 0
    for t in rc["trades"]:
        r = rowmap.get((t["symbol"], t["entry_date"], t["entry_time"]))
        if r is None:
            continue
        n += 1
        bks = D._load_ex(db, next(f["symbol_id"] for f in funds if f["symbol"] == t["symbol"]), None, None, hpc)["book"]
        a1 = D._best(bks, r[1], r[2], 1, 0.0)
        if r[4] / r[3] - 1 > 1e-12 or (a1 > 0 and a1 / r[3] - 1 > 1e-12):
            bad.append(f"{t['symbol']} {t['entry_date']} {t['entry_time']}: آخرین {r[4] / r[3] - 1:.4%} / سر صف فروش {a1 / r[3] - 1:.4%}")
    pre = D._Parker.prepare([dict(L, label="x")])
    neg = sum(1 for arr in (pre["xb"][0] or []) + (pre["xs"][0] or []) if arr < 0)
    R.add(g, "سیگنال دوتأییدی: هر ورود هم با آخرین قیمت و هم با سر صف فروش شرط را دارد؛ اردربوک هرگز سیگنال را آسان‌تر نمی‌کند",
          not bad and n > 0 and neg == 0, "؛ ".join(bad[:3]) or f"{n} ورود، اصلاح منفی: {neg}")
    # hover in the trades table: the book / bubbles at a trade's instant are exactly what the backtest saw
    from web_server import create_app
    cl = create_app(db).test_client()
    bad, n = [], 0
    for t in rhy["trades"][:12]:
        for side, d, tm, px in (("buy", t["entry_date"], t["entry_time"], t["entry_price"]),
                                ("sell", t["exit_date"], t["exit_time"], t["exit_price"])):
            r = cl.get(f"/api/disc/trade_moment?focus={t['symbol']}&date={d}&time={tm}&cats=gold&base={t['base_pct']}")
            j = r.get_json() or {}
            if r.status_code != 200 or not j.get("book"):
                bad.append(f"{t['symbol']} {side}: {r.status_code} {j.get('error')}")
                continue
            n += 1
            lv = j["book"]["asks"] if side == "buy" else j["book"]["bids"]
            lo, hi = min(x[0] for x in lv), max(x[0] for x in lv)
            if not (lo - 0.02 <= px <= hi + 0.02):
                bad.append(f"{t['symbol']} {side} {px} خارج از {lo}–{hi}")
            me = next((f for f in j["funds"] if f["symbol"] == t["symbol"]), None)
            if me is None or me["bubble_last_pct"] is None or len(j["funds"]) < 2:
                bad.append(f"{t['symbol']} {side}: حباب‌ها ناقص")
    R.add(g, "نمایش اردربوک و حباب در لحظهٔ هر خرید و فروش (hover): همان صف‌هایی که بک‌تست با آن معامله کرد",
          not bad and n > 0, "؛ ".join(bad[:3]) or f"{n} لحظه")
    # the signal is confirmed at the AVERAGE price of the whole order (all ask levels it walks), not only the best
    # ask: every hold-mode buy's real fill price (rel_entry_pct = fill vs fair) is within the entry limit
    bad, n, deep = [], 0, 0
    for mx in (0.0, 0.3):
        hpv = _base(fill_mode="hold", fill_max_rel_pct=mx, position_pct=100, dataset="hybrid", participation_pct=0.0)
        rv = _run(db, hpv)
        for t in rv["trades"]:
            n += 1
            if t.get("spread_in") is not None and t["spread_in"] > 0.0012:
                deep += 1                                     # walked past the first ask level
            if t["rel_entry_pct"] > mx + 1e-6:
                bad.append(f"{t['symbol']} {t['entry_date']} {t['entry_time']}: {t['rel_entry_pct']}% > {mx}%")
    R.add(g, "سیگنال خرید با میانگین قیمت کل سفارش (همهٔ سطرهای صف فروش) تأیید می‌شود: قیمت واقعی هر خرید در حد مجاز ورود است",
          not bad and n > 0 and deep > 0, "؛ ".join(bad[:3]) or f"{n} خرید، {deep} خرید از سطرهای بعدی")
    # partial entry: when the whole order would pay too much on average, only the part whose AVERAGE price stays
    # within the limit is bought (checked independently by bisection over the real asks) — and the switch sells
    # only as much of the held fund as that buy needs
    import tse_dataset as _TD0
    bad, n_lim, n_part, ent_ok, n_top = [], 0, 0, 0, 0
    sid_of = {f["symbol"]: f["symbol_id"] for f in funds}
    for mx, pos_ in ((0.0, 100), (-0.1, 50)):
        hpl = _base(fill_mode="hold", fill_max_rel_pct=mx, position_pct=pos_, dataset="hybrid", participation_pct=0.0)
        rl = _run(db, hpl)
        si = rl["summary"].get("size_info") or {}
        n_lim += si.get("limited_by_price") or 0
        n_part += si.get("switch_partial_sales") or 0
        n_top += si.get("topups") or 0
        books = {s: D._load_ex(db, sid_of[s], None, None, hpl) for s in {t["symbol"] for t in rl["trades"]}}
        legs = {}
        for t in rl["trades"]:
            legs.setdefault((t["symbol"], t["entry_date"], t["entry_time"]), []).append(t)
        for (sym, d_, t_), ts in legs.items():
            L = books[sym]
            r = next((x for x in L["rows"] if x[1] == d_ and x[2] == t_), None)
            asks = D._book_side(L["book"], d_, t_, 1) if r else None
            if not asks or abs(ts[0]["rel_entry_pct"] - mx) > 1e-3 or asks[-1][0] <= r[3] * (1 + mx / 100):
                continue
            lo, hi = 0.0, float(sum(v for _p, v in asks))     # most units whose average price is within the limit
            for _ in range(80):
                mid = (lo + hi) / 2
                g_, a_, _w = _TD0.fill(asks, mid)
                lo, hi = (mid, hi) if a_ <= r[3] * (1 + mx / 100) * (1 + 1e-12) else (lo, mid)
            vol = sum(x["volume"] for x in ts)
            ent_ok += 1
            if not (lo - len(ts) - 1 <= vol <= lo + 1):
                bad.append(f"{sym} {d_} {t_}: {vol} ≠ {lo:.1f}")
        for t in rl["trades"]:
            if t["rel_entry_pct"] > mx + 1e-6:
                bad.append(f"{t['symbol']} {t['entry_date']} {t['entry_time']}: {t['rel_entry_pct']}% > {mx}%")
        # a partial switch sale raises no more than the buy at the same instant costs
        for (sym, d_, t_), ts in legs.items():
            for x in ts:
                if x["exit_reason"] != "rotate" or x is ts[-1]:
                    continue                                   # only the legs that left a remnant
                buy = [b for b in rl["trades"] if b["entry_date"] == x["exit_date"] and b["entry_time"] == x["exit_time"]]
                cost = sum(b["buy_notional"] for b in buy) * (1 + hpl.buy_fee)
                net = x["sell_notional"] * (1 - hpl.sell_fee)
                if not buy or net > cost * 1.005:
                    bad.append(f"{sym}: فروش {net:,.0f} > خرید {cost:,.0f}")
    R.add(g, "خرید جزئی: اگر میانگین کل سفارش از حد بیشتر باشد، فقط بخشی که میانگینش در حد است خریده می‌شود؛ "
             "در جابه‌جایی فقط به اندازهٔ لازم از صندوق قبلی فروخته می‌شود",
          not bad and n_lim > 0 and n_part > 0 and ent_ok > 0 and n_top > 0,
          "؛ ".join(bad[:3]) or f"{n_lim} خرید محدود به قیمت، {ent_ok} بررسی مستقل، {n_part} فروش جزئی، {n_top} تکمیل موقعیت")
    # execution audit: (1) one participation budget per fund and day — buys never exceed the cap of the day's volume;
    # (2) two switches at one instant never sell into the same bids twice — the proceeds at an instant are at most
    # those of walking its bids once for all the units sold then; (3) with an execution delay nothing is sold before
    # its buy filled
    import collections as _C
    bad, n_fd, n_inst = [], 0, 0
    for cfg in ({"participation_pct": 2.0, "participation_basis": "day", "position_pct": 50, "fill_max_rel_pct": 0.3},
                {"participation_pct": 0.0, "position_pct": 100, "fill_max_rel_pct": 0.0},
                {"participation_pct": 0.0, "position_pct": 25, "fill_max_rel_pct": 0.3, "fill_min_hold_min": 0}):
        hpx = _base(fill_mode="hold", dataset="hybrid", **cfg)
        rx = _run(db, hpx)
        L = {f["symbol"]: D._load_ex(db, f["symbol_id"], None, None, hpx) for f in funds}
        if cfg["participation_pct"] > 0:
            buys = _C.defaultdict(float)
            for t in rx["trades"]:
                buys[(t["symbol"], t["entry_date"])] += t["buy_notional"] / t["entry_price"]
            for (sym, d_), u in buys.items():
                n_fd += 1
                cap_d = L[sym]["day_vol"].get(d_, 0) * cfg["participation_pct"] / 100.0
                if u > cap_d * (1 + 1e-6) + 1:
                    bad.append(f"{sym} {d_}: خرید {u:,.0f} > سقف {cap_d:,.0f}")
        sales = _C.defaultdict(lambda: [0.0, 0.0])
        for t in rx["trades"]:
            if t["exit_reason"] == "rotate":
                g_ = sales[(t["symbol"], t["exit_date"], t["exit_time"])]
                g_[0] += t["sell_notional"] / t["exit_price"]
                g_[1] += t["sell_notional"]
        for (sym, d_, t_), (u, proceeds) in sales.items():
            bids = L[sym]["book"].at(d_, t_)[0]
            if not bids:
                continue
            n_inst += 1
            if u > sum(v for _p, v in bids) * (1 + 1e-9) + 1:
                bad.append(f"{sym} {d_} {t_}: فروش {u:,.0f} واحد > عمق صف خرید {sum(v for _p, v in bids):,.0f}")
            got, avg, worst = _TD0.fill(bids, u)
            best = got * avg + (u - got) * worst
            if proceeds > best * (1 + 1e-4) + 1:
                bad.append(f"{sym} {d_} {t_}: فروش {proceeds:,.0f} > یک بار پیمودن صف خرید {best:,.0f}")
    hpd = _base(fill_mode="hold", dataset="hybrid", exec_delay_snaps=1, fill_min_hold_min=0, position_pct=25,
                fill_max_rel_pct=0.3, participation_pct=0.0)
    for t in _run(db, hpd)["trades"]:
        if (t["exit_date"], t["exit_time"]) < (t["entry_date"], t["entry_time"]):
            bad.append(f"{t['symbol']}: خروج {t['exit_date']} {t['exit_time']} پیش از ورود {t['entry_date']} {t['entry_time']}")
    R.add(g, "اجرا: سقف حجم برای هر صندوق و روز یک بودجه است؛ صف خرید یک لحظه دو بار فروخته نمی‌شود؛ "
             "با تأخیر اجرا هیچ فروشی پیش از پر شدن خریدش نیست",
          not bad and n_fd > 0 and n_inst > 0, "؛ ".join(bad[:3]) or f"{n_fd} صندوق-روز، {n_inst} لحظهٔ فروش")
    # the independent verifier (no engine code) agrees with the backtest — and catches deliberate tampering
    import copy as _cp
    import verify_independent as VI
    bad, n_ok = [], 0
    for cfg in ({"position_pct": 50, "fill_max_rel_pct": 0.1, "participation_pct": 10.0, "participation_basis": "sofar",
                 "fill_min_hold_min": 60},
                {"position_pct": 100, "fill_max_rel_pct": 0.0, "participation_pct": 0.0}):
        rv = json.loads(json.dumps(_run(db, _base(fill_mode="hold", dataset="hybrid", **cfg)), default=str))
        rep = VI.verify(p2, rv)
        if not rep["ok"]:
            bad.append(f"{cfg}: " + "؛ ".join(f"{c['name']}: {c['examples'][:1]}" for c in rep["checks"] if c["status"] == "fail"))
        n_ok += sum(c["checked"] for c in rep["checks"])
    tamper = {"قیمت خرید": lambda t: t.update(entry_price=t["entry_price"] * 1.001),
              "مبلغ فروش": lambda t: t.update(sell_notional=t["sell_notional"] * 1.002),
              "NAV فروش": lambda t: t.update(nav_exit=t["nav_exit"] * 1.01),
              "زمان ورود": lambda t: t.update(entry_time=t["entry_time"] + 7),
              "سود خالص": lambda t: t.update(net_pnl=t["net_pnl"] + 1e6)}
    missed = []
    for name, fn in tamper.items():
        rv2 = _cp.deepcopy(rv)
        fn(rv2["trades"][len(rv2["trades"]) // 2])
        if VI.verify(p2, rv2)["ok"]:
            missed.append(name)
    R.add(g, "بررسی مستقل (بدون هیچ کدی از موتور بک‌تست): قیمت‌ها، NAV، عمق صف، سقف حجم، پنجرهٔ بازار و نقد با دادهٔ خام "
             "می‌خوانند؛ هر دست‌کاری عمدی گرفته می‌شود",
          not bad and not missed and n_ok > 0,
          "؛ ".join(bad[:2]) or (f"گرفته نشد: {missed}" if missed else f"{n_ok} بررسی، {len(tamper)} دست‌کاری همه گرفته شد"))
    # the normal strategy takes the same partial entry
    hpo = _base(dataset="hybrid", position_pct=100, entry_discount_pct=0.0, participation_pct=0.0)
    ro = _run(db, hpo)
    bad = [f"{t['symbol']} {t['entry_date']} {t['entry_time']}: {t['rel_entry_pct']}%" for t in ro["trades"]
           if t["rel_entry_pct"] > 1e-6]
    R.add(g, "استراتژی معمولی هم فقط تا جایی می‌خرد که میانگین قیمت خرید در حد آستانهٔ ورود باشد",
          not bad and len(ro["trades"]) > 0, "؛ ".join(bad[:3]) or f"{len(ro['trades'])} معامله")
    ck = rhy["dataset"].get("clock") or {}
    R.add(g, "بررسی ساعت NAV↔TSE: در دادهٔ هم‌ساعت، جابه‌جایی صفر بهترین است؛ هیچ معامله‌ای بیرون از ساعت بازار نیست",
          ck.get("aligned") is True and ck.get("best_offset_min") == 0 and ck.get("trade_legs_checked", 0) > 0
          and ck.get("trade_legs_outside_window") == 0 and (ck.get("rows_outside_window_pct") or 0) > 0,
          json.dumps({k: ck.get(k) for k in ("best_offset_min", "dev_at_0_pct", "best_dev_pct", "rows_outside_window_pct",
                                              "trade_legs_outside_pct")}))
    # a NAV file whose clock runs 2 hours late must be caught (best offset −120 min, not aligned)
    import tse_dataset as _TD
    _orig = db.get_nav_intraday

    def _late(sid, s=None, e=None):
        return [(r[0], _hms_add(r[1], 7200), *r[2:]) for r in _orig(sid, s, e)]
    db.get_nav_intraday = _late
    _TD._CLOCK.clear()                        # (the NAV rows changed behind the database's back)
    with db._conn() as _c:
        _c.execute("DELETE FROM tse_cache WHERE name LIKE 'clock|%'")
    try:
        ck2 = _TD.clock_check(db, [(f["symbol_id"], f["symbol"]) for f in funds], None, None, [])
    finally:
        db.get_nav_intraday = _orig
    R.add(g, "بررسی ساعت: فایل NAV با ۲ ساعت اختلاف شناسایی می‌شود",
          ck2.get("aligned") is False and ck2.get("best_offset_min") == -120,
          json.dumps({k: ck2.get(k) for k in ("best_offset_min", "dev_at_0_pct", "best_dev_pct")}))
    R.add(g, "در دیتاست‌های اردربوک اسپرد فرضی هیچ اثری ندارد",
          _sig(_run(db, replace(base, dataset="hybrid", half_spread_pct=0.5))) == _sig(rhy)
          and _sig(_run(db, replace(base, half_spread_pct=0.5))) == _sig(res), "")
    for ds in ("hybrid", "tse"):
        rr = _run(db, replace(hp, dataset=ds))
        books = {f["symbol"]: (TD.load_hybrid(db, f["symbol_id"], None, None)[1] if ds == "hybrid"
                               else TD.load_raw(db, f["symbol_id"], None, None, 300)[1]) for f in funds}
        bad, n = [], 0
        for t in rr["trades"]:
            bk = books[t["symbol"]]
            bi, ai = bk.at(t["entry_date"], t["entry_time"])
            bx, _ax = bk.at(t["exit_date"], t["exit_time"])
            n += 1
            if not ai or not (ai[0][0] - 0.01 <= t["entry_price"] <= ai[-1][0] + 0.01):
                bad.append(f"{t['symbol']} خرید {t['entry_price']} خارج از صف فروش")
            if not bx or not (bx[-1][0] - 0.01 <= t["exit_price"] <= bx[0][0] + 0.01):
                bad.append(f"{t['symbol']} فروش {t['exit_price']} ({t['exit_reason']} {t['exit_time']}) خارج از صف خرید")
            if t["spread_in"] is None or t["spread_in"] <= 0 or t["spread_out"] is None or t["spread_out"] <= 0:
                bad.append(f"اسپرد واقعی {t['spread_in']} / {t['spread_out']}")
        R.add(g, f"«همیشه سرمایه‌گذاری» ({ds}): خرید از صف فروش و فروش (حتی فروش برای جابه‌جایی) به صف خرید همان لحظه",
              not bad and n > 0, "؛ ".join(bad[:3]) or f"{n} معامله")
    st = S.run_study(db, None, None, None, None, base=hp, space={"fill_switch_pct": [0.3, 0.8], "position_pct": [25, 50]},
                     n_samples=8, blocks=4, test_frac=0.3, max_seconds=60, min_trades=1)
    R.add(g, "بهینه‌ساز روی دیتاست TSE اجرا می‌شود", bool(st.get("best")) and st["base_params"]["dataset"] == "tse",
          str(st.get("error") or st.get("setup", {}).get("n_random")))
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(p2 + ext)
        except OSError:
            pass


def test_window_marks(R: Results, db):
    """Optimizer, "hold" mode: one continuous run read per block from daily liquidation-value marks. The blocks
    must chain exactly into the whole period, and the whole period must equal the ordinary backtest."""
    g = "بهینه‌سازی موازی"
    hp = _base(fill_mode="hold", fill_max_rel_pct=2.0, position_pct=50)
    st = S.Study(db, None, None, None, None, hp, {"fill_switch_pct": [0.8]}, blocks=5, min_trades=1, workers=1)
    st._open(db)
    U = st._universe(hp)
    acc, marks = st._run_once(hp, U)
    whole = S._window(acc, marks, st.dates[0], st.dates[-1], hp)
    chain = 1.0
    for lo, hi in st.bounds:
        chain *= 1 + S._window(acc, marks, lo, hi, hp)["portfolio_return_pct"] / 100
    _a, ref, _c = D._portfolio_summary([], hp, st.dates[0], st.dates[-1], fill=st._fill_ctx(U, st.dates[0], st.dates[-1]))
    ok = (abs(whole["portfolio_return_pct"] - ref["portfolio_return_pct"]) < 1e-3
          and abs((chain - 1) * 100 - whole["portfolio_return_pct"]) < 1e-2 and len(marks) > 10
          and whole["trade_count"] == ref["trade_count"])
    R.add(g, "یک اجرای پیوسته: بلوک‌ها دقیقاً به کل دوره زنجیر می‌شوند و کل دوره = بک‌تست عادی", ok,
          f"کل {whole['portfolio_return_pct']} / بک‌تست {ref['portfolio_return_pct']} / زنجیر بلوک‌ها {round((chain - 1) * 100, 3)}"
          f" / معامله {whole['trade_count']} و {ref['trade_count']}")
    # the backtest's equity curve / drawdown are the daily liquidation value (not cash + purchase cost, which stays
    # flat while the holdings move): same drawdown as the optimizer's marks, one point per trading day
    _a2, ref2, curve2 = D._portfolio_summary([], hp, st.dates[0], st.dates[-1], fill=st._fill_ctx(U, st.dates[0], st.dates[-1]))
    mk_eq = {d: e for d, e, _x, _y in marks}
    same = all(abs(curve2_d[1] - mk_eq[curve2_d[0]]) <= 1 for curve2_d in curve2[1:] if curve2_d[0] in mk_eq)
    R.add(g, "منحنی سرمایه و افت سرمایهٔ بک‌تست = ارزش روزانه به قیمت بازار (همان افتِ بهینه‌ساز)، نه نقد + بهای خرید",
          abs(ref2["max_drawdown_pct"] - whole["max_drawdown_pct"]) < 1e-3 and same and len(curve2) >= len(marks),
          f"افت بک‌تست {ref2['max_drawdown_pct']} / افت از ارزش روزانه {whole['max_drawdown_pct']} / {len(curve2)} نقطه، {len(marks)} روز")


def test_same_instant_sales(R: Results):
    """Two candidates at one instant, each needing part of the same held fund: the second sale must meet only the
    bids the first one left (here 2000 units at 102, then 90) — in Python and in the compiled replay alike."""
    g = "بک‌تست روی دادهٔ TSE (تیک و اردربوک)"
    d = 20260103
    o = D._ord(d)

    def mk(label, snaps):
        rows, book, dv = [], {}, D._DayVol()
        for t, fair, last, bids, asks in snaps:
            rows.append((o, d, t, fair, last, True, fair))
            book[(d, t)] = (bids, asks)
        dv[d] = 10 ** 12
        return {"label": label, "rows": rows, "book": book, "day_vol": dv, "mr": None, "sid": len(label), "raw": []}
    W = mk("W", [(90000, 101.0, 100.0, [[99.9, 1e9]], [[100.0, 1e9]]),
                 (100000, 100.0, 102.0, [[102.0, 2000], [90.0, 1e9]], [[102.1, 1e9]]),
                 (110000, 100.0, 102.0, [[102.0, 1e9]], [[102.1, 1e9]])])
    cheap = [(83000, 100.0, 110.0, [[109, 1]], [[110, 1]]), (100000, 100.0, 97.0, [[96.9, 1e9]], [[97.0, 1500], [130.0, 1e9]]),
             (110000, 100.0, 97.0, [[96.9, 1e9]], [[97.0, 1e9]])]
    p = _base(fill_mode="hold", dataset="hybrid", position_pct=100, fill_max_rel_pct=0.0, fill_switch_pct=0.5,
              fill_min_hold_min=0, buy_fee=0.0, sell_fee=0.0, initial_capital=1e6, participation_pct=0.0)
    ctx = {"loaded": [W, mk("A", cheap), mk("B", cheap)], "crash": None}
    acc = D._replay([], p, d, d, False, ctx)[0]
    sold = [t for t in acc if t.symbol == "W" and t.exit_time == 100000]
    u = sum(t.sell_notional / t.exit_price for t in sold)
    proceeds = sum(t.sell_notional for t in sold)
    got, avg, worst = __import__("tse_dataset").fill([[102.0, 2000], [90.0, 1e9]], u)
    once = got * avg + (u - got) * worst
    ok = len(sold) >= 2 and proceeds <= once * (1 + 1e-9) + 1
    det = f"{len(sold)} فروش در یک لحظه، {u:,.0f} واحد: {proceeds:,.0f} ≤ {once:,.0f}"
    try:
        import discount_fast as X
        if X.available():
            ctx2 = {"loaded": [W, mk("A", cheap), mk("B", cheap)], "crash": None}
            a2 = X.run_hold(ctx2, p, d, d)
            same = a2 is not None and [asdict(x) for x in acc] == [asdict(x) for x in a2]
            ok = ok and same
            det += "؛ کامپایل‌شده یکسان" if same else "؛ کامپایل‌شده متفاوت!"
    except Exception as e:                            # noqa: BLE001
        ok, det = False, f"{det}؛ خطا {e}"
    R.add(g, "دو جابه‌جایی در یک لحظه: فروش دوم فقط به آنچه از صف خرید مانده می‌فروشد (نه دوباره به همان سطرها)", ok, det)
    # a thin buying queue: the switch sells only what the visible bids take (2000 units); the rest stays held
    W2 = mk("W", [(90000, 101.0, 100.0, [[99.9, 1e9]], [[100.0, 1e9]]),
                  (100000, 100.0, 102.0, [[102.0, 1500], [101.0, 500]], [[102.1, 1e9]]),
                  (110000, 100.0, 102.0, [[102.0, 1e9]], [[102.1, 1e9]])])
    ctx = {"loaded": [W2, mk("A", cheap)], "crash": None}
    acc = D._replay([], p, d, d, False, ctx)[0]
    at10 = sum(t.sell_notional / t.exit_price for t in acc if t.symbol == "W" and t.exit_time == 100000)
    held = sum(t.volume for t in acc if t.symbol == "W" and t.exit_reason == "end")
    ok = 0 < at10 <= 2000 + 1e-6 and held > 0
    det = f"فروخته در ۱۰:۰۰: {at10:,.0f} از عمق ۲۰۰۰؛ نگه‌داشته تا بعد: {held:,.0f}"
    try:
        import discount_fast as X
        if X.available():
            a2 = X.run_hold({"loaded": [W2, mk("A", cheap)], "crash": None}, p, d, d)
            same = a2 is not None and [asdict(x) for x in acc] == [asdict(x) for x in a2]
            ok = ok and same
            det += "؛ کامپایل‌شده یکسان" if same else "؛ کامپایل‌شده متفاوت!"
    except Exception as e:                            # noqa: BLE001
        ok, det = False, f"{det}؛ خطا {e}"
    R.add(g, "فروش هرگز بیش از عمق دیده‌شدهٔ صف خرید نیست؛ باقی‌مانده نگه داشته و بعداً فروخته می‌شود", ok, det)


def test_fast_replay(R: Results, db_path: str):
    """The compiled (numba) replay of the "hold" strategy must give EXACTLY the Python replay: every trade field
    and every daily mark, on both datasets, with delays, volume caps, filters and placebo ranks."""
    import shutil as _sh
    import discount_fast as X
    g = "بهینه‌سازی موازی"
    if not X.available():
        # numba missing = skip; numba installed but the kernel failed to compile/run = a real failure
        R.add(g, "شبیه‌سازی کامپایل‌شده = شبیه‌سازی پایتون", False if X._HAVE else None,
              ("کرنل کامپایل نشد: " + str(X._BROKEN[:1])[:300]) if X._HAVE else "numba نصب نیست (مسیر پایتون استفاده می‌شود)")
        return
    p2 = db_path + ".fast.db"
    _sh.copyfile(db_path, p2)
    db = _db(p2)
    _fake_tse_from_dump(db, n_days=80)
    cfgs = [{}, {"fill_switch_pct": 0.3, "fill_min_hold_min": 60}, {"exec_delay_snaps": 1},
            {"participation_pct": 5.0, "participation_basis": "sofar", "position_pct": 100},
            {"crash_drop_pct": 0.5, "fill_quote_age_min": 15}, {"require_fresh": False, "mr_center": "off"},
            # tight entry limits: partial buys (average price at the limit) and partial switch sales
            {"fill_max_rel_pct": 0.0, "position_pct": 100, "participation_pct": 0.0},
            {"fill_max_rel_pct": 0.0, "participation_pct": 0.0, "exec_delay_snaps": 1},
            {"fill_max_rel_pct": -0.1, "position_pct": 50, "participation_pct": 0.0},
            # one volume budget per fund and day; several switches in one group
            {"fill_max_rel_pct": 0.3, "position_pct": 25, "participation_pct": 2.0, "participation_basis": "sofar",
             "fill_min_hold_min": 0},
            # ranking against each fund's own norm + the mean-reversion filter
            {"baseline_days": 10, "mr_center": "self", "mr_window_days": 10, "mr_min_score": 50.0},
            {"baseline_days": 6, "mr_center": "zero", "mr_window_days": 10, "mr_min_score": 60.0}]
    bad, n, ntr = [], 0, 0
    for ds in ("dump", "hybrid"):
        for c in cfgs:
            p = replace(_base(fill_mode="hold", fill_max_rel_pct=2.0, position_pct=50, dataset=ds), **c)
            st = S.Study(db, None, None, None, None, p, {"fill_switch_pct": [0.8]}, blocks=4, min_trades=1, workers=1)
            st._open(db)
            U = st._universe(p)
            d0, d1 = st.dates[0], st.dates[-1]
            ctx = st._fill_ctx(U, d0, d1)
            rng = random.Random(5)
            rk = [[rng.gauss(0, 0.01) for _ in rr] for rr in ctx["pre"]["rows"]]
            for fctx in (ctx, dict(ctx, pre=dict(ctx["pre"], rank=rk))):
                m1, m2 = [], []
                a1, s1, _c = D._portfolio_summary([], p, d0, d1, fill=fctx, marks=m1)
                a2 = X.run_hold(fctx, p, d0, d1, marks=m2)
                fin = X.final_capital(fctx, p, d0, d1)
                lite = X.run_hold(fctx, p, d0, d1, lite=True)
                n += 1
                ntr += len(a1)
                if a2 is None or [asdict(x) for x in a1] != [asdict(x) for x in a2] or m1 != m2 \
                        or round(fin, 0) != s1["final_capital"] \
                        or [(t.entry_date, t.net_pnl, t.net_pct) for t in a1] != [(t.entry_date, t.net_pnl, t.net_pct) for t in lite]:
                    bad.append(f"{ds} {c} rank={'placebo' if fctx is not ctx else 'real'}")
    R.add(g, "شبیه‌سازی کامپایل‌شده (numba) دقیقاً همان معامله‌ها و ارزش‌های روزانهٔ شبیه‌سازی پایتون را می‌دهد",
          not bad and ntr > 0, "؛ ".join(bad[:3]) or f"{n} حالت، {ntr} معامله، همه یکسان")
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(p2 + ext)
        except OSError:
            pass


def test_warmup(R: Results, db):
    """Returns are measured from the end of the warm-up: the backtest (strategy, equity, benchmarks), the validation
    and the optimizer (one common start for every configuration searched). The mean-reversion score warms up on the
    same days as the per-fund norm, not after it."""
    import discount_validation as V
    g = "گرم‌شدن"
    wd = D.warmup_days
    cases = [({}, 0), ({"baseline_days": 20}, 6), ({"mr_center": "zero", "mr_window_days": 20}, 6),
             ({"mr_center": "self", "mr_window_days": 20}, 26), ({"mr_center": "self", "mr_window_days": 10, "baseline_days": 60}, 20),
             ({"mr_center": "category", "mr_window_days": 30, "baseline_days": 20}, 10)]
    bad = [f"{c} → {wd(_base(**c))} ≠ {w}" for c, w in cases if wd(_base(**c)) != w]
    R.add(g, "طول گرم‌شدن (روز معاملاتی) = بیشینهٔ نیاز معمولِ صندوق و امتیاز بازگشت", not bad, "؛ ".join(bad))

    sids = [sid for sid, _l in D._universe(db, None, None)]
    all_dates = D.dataset_dates(db, sids, None, None, _base())
    p = _base(baseline_days=20, mr_center="self", mr_window_days=10, mr_min_score=0.0)
    w = wd(p)
    r = _run(db, p)
    eff = all_dates[w]
    wi = r.get("warmup") or {}
    eq0 = r["equity_curve"][0][0] if r["equity_curve"] else None
    b = r.get("benchmark") or {}
    first_trade = min((t["entry_date"] for t in r["trades"]), default=None)
    R.add(g, "بک‌تست از پایان گرم‌شدن شروع می‌شود (دوره، منحنی سرمایه، اولین معامله)",
          wi.get("effective_start") == eff and wi.get("shifted") and r["period"][0] == eff
          and (eq0 is None or eq0 >= eff) and (first_trade is None or first_trade >= eff)
          and b.get("first") is not None and b["first"] >= eff,
          f"warm={w} eff={eff} period={r['period']} eq0={eq0} bench={b.get('first')} first_trade={first_trade} info={wi}")
    # the first day after the warm-up is tradable: the norm AND the mean-reversion score already exist on it
    loaded = []
    for sid in sids:
        dl = D._load_ex(db, sid, eff, None, p)
        if dl["rows"]:
            loaded.append(dl)
    D._compute_mr(db, loaded, p, eff, None)
    ready = sum(1 for dl in loaded if dl["rows"] and dl["rows"][0][1] == eff and dl["mr"][0] is not None)
    R.add(g, "روز اول پس از گرم‌شدن هم معمولِ صندوق و هم امتیاز بازگشت آماده است",
          ready >= max(1, len(loaded) // 2), f"{ready} از {len(loaded)} صندوق")
    # warm-ups run side by side: a mean-reversion score shorter than the baseline adds no days
    p2 = _base(baseline_days=30, mr_center="zero", mr_window_days=10)
    dl = next(x for x in (D._load_ex(db, sid, None, None, p2) for sid in sids) if x["rows"])
    D._compute_mr(db, [dl], p2, None, None)
    R.add(g, "امتیاز بازگشت هم‌زمان با معمولِ صندوق گرم می‌شود، نه پس از آن",
          dl["mr"] and dl["mr"][0] is not None, f"first={dl['rows'][0][1] if dl['rows'] else None}")
    later = all_dates[w + 15]
    r2 = _run_range(db, p, later)
    R.add(g, "شروعی که پس از گرم‌شدن است جابه‌جا نمی‌شود", r2["period"][0] == later and not r2["warmup"]["shifted"],
          f"{r2['period']}")
    U = V.load_universe(db, None, None, None, None, p)
    R.add(g, "اعتبارسنجی هم از پایان گرم‌شدن شروع می‌شود", U["d0"] == eff, f"{U['d0']} vs {eff}")
    # optimizer: one common start — the most demanding configuration searched
    space = {"baseline_days": [0, 20], "mr_center": ["off", "self"], "mr_window_days": [10, 20], "mr_min_score": [50, 70]}
    st = S.Study(db, None, None, None, None, _base(fill_mode="hold", fill_max_rel_pct=2.0), space, blocks=4, min_trades=1, workers=1)
    st._open(db)
    need = max(wd(_base(baseline_days=b_, mr_center=c, mr_window_days=n_)) for b_ in (0, 20) for c in ("off", "self") for n_ in (10, 20))
    R.add(g, "بهینه‌ساز: همهٔ ترکیب‌ها از یک شروع مشترک پس از سخت‌ترین گرم‌شدن سنجیده می‌شوند",
          need == 26 and st.dates[0] == all_dates[need] and st.bounds[0][0] == all_dates[need] and st.warmup["days"] == need,
          f"need={need} start={st.dates[0]} expected={all_dates[need]}")
    st2 = S.Study(db, None, None, None, None, _base(fill_mode="hold", fill_max_rel_pct=2.0), {"fill_switch_pct": [0.5, 0.8]},
                  blocks=4, min_trades=1, workers=1)
    st2._open(db)
    R.add(g, "بدون نیاز به گرم‌شدن، دورهٔ بهینه‌ساز از اولین روز داده است",
          st2.dates[0] == all_dates[0] and not st2.warmup["shifted"], f"{st2.dates[0]} vs {all_dates[0]}")


def test_nav_feed(R: Results, db_path: str):
    """With the fine-grained NAV feed imported, every NAV the backtest sees is the one RECEIVED by that instant (not
    the dump's sampled copy); the independent verifier reads the feed the same way, on its own."""
    import shutil as _sh
    import json as _js
    import nav_feed as NF
    import verify_independent as VI
    g = "NAV ریزدانه (feed)"
    p2 = db_path + ".feed.db"
    _sh.copyfile(db_path, p2)
    db = _db(p2)
    _fake_tse_from_dump(db, n_days=40)
    p = _base(fill_mode="hold", dataset="hybrid", fill_max_rel_pct=0.3, position_pct=50, participation_pct=0.0,
              fill_min_hold_min=0)
    r0 = _js.loads(_js.dumps(_run(db, p), default=str))              # before the feed: the dump's NAVs
    # the feed: every dump NAV (+0.1 %) RECEIVED 20 s after the dump shows it, so at a dump instant only the
    # previous one is known; plus an MW re-send of a stale NAV that must never win over a newer one
    with db._conn() as conn:
        conn.executescript(__import__("tools.import_nav_feed", fromlist=["SCHEMA"]).SCHEMA)
        rows = conn.execute("SELECT symbol_id, date, time, nav FROM nav_intraday WHERE nav>0").fetchall()
        feed = []
        for sid, d, t, nav in rows:
            s = (t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100 + 20
            feed.append((sid, d, (s // 3600) * 10000 + (s // 60 % 60) * 100 + s % 60, nav * 1.001, d, t, "N2"))
        conn.executemany("INSERT INTO nav_feed VALUES (?,?,?,?,?,?,?)", feed)
        sid0, d0, t0 = rows[len(rows) // 2][:3]
        conn.execute("INSERT INTO nav_feed VALUES (?,?,?,?,?,?,?)", (sid0, d0, t0, 1.0, 20000101, 90000, "MW"))
    NF._VER.clear()                           # (the 2-second memo of the feed's fingerprint)
    r1 = _js.loads(_js.dumps(_run(db, p), default=str))
    ns = r1.get("nav_source") or {}
    bad, n = [], 0
    for t in r1["trades"]:
        sid = next(f[0] for f in FUNDS if f[1] == t["symbol"])
        k = NF.known_at(db, sid, t["entry_date"], t["entry_time"])
        n += 1
        if k is None or abs(k[0] - t["nav_entry"]) > 1e-6 * k[0]:
            bad.append(f"{t['symbol']} {t['entry_date']} {t['entry_time']}: {t['nav_entry']} ≠ {k}")
    R.add(g, "با feed، NAV هر معامله همان NAV دریافت‌شده تا آن لحظه است (نه نمونهٔ دادهٔ قبلی)",
          ns.get("source") == "feed" and not bad and n > 0 and ns.get("nav_changed_pct", 0) > 50,
          "؛ ".join(bad[:2]) or f"{n} معامله؛ {ns.get('nav_changed_pct')}% سطرها NAV دیگری گرفتند")
    k = NF.known_at(db, sid0, d0, t0)
    R.add(g, "NAV کهنه‌ای که دوباره فرستاده شود جای NAV جدیدتر را نمی‌گیرد؛ NAV دریافت‌نشده دیده نمی‌شود",
          k is not None and k[0] != 1.0 and k[1] != 20000101, f"{k}")
    rep1, rep0 = VI.verify(p2, r1), VI.verify(p2, r0)
    fails0 = [c["name"] for c in rep0["checks"] if c["status"] == "fail"]
    R.add(g, "بررسی مستقل feed را خودش می‌خواند: نتیجهٔ با feed درست است و نتیجهٔ با NAV قدیمی رد می‌شود",
          rep1["ok"] and not rep0["ok"] and any("NAV" in x for x in fails0),
          "؛ ".join(f"{c['name']}: {c['examples'][:1]}" for c in rep1["checks"] if c["status"] == "fail") or str(fails0))
    with db._conn() as conn:                  # a re-import (even one correcting values in place) must be picked up
        conn.execute("UPDATE nav_feed SET nav = nav * 1.01")
    NF._VER.clear()                           # (the 2-second memo of the fingerprint)
    r2 = _run(db, p)
    R.add(g, "ورود دوبارهٔ feed بدون پاک کردن کش اعمال می‌شود", _sig(r2) != _sig(r1), "")
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(p2 + ext)
        except OSError:
            pass


def _run_range(db, p: D.DiscountParams, start=None, end=None) -> dict:
    return D.run_discount_backtest(db, None, None, start, end, p)


def test_stale_nav_no_trade(R: Results, db):
    """A fund whose latest snapshot carries a stale NAV is neither bought nor sold (not even to fund a switch)."""
    import bisect as _bs
    g = "NAV به‌روز"
    import shutil as _sh
    p2 = db.path.parent / (db.path.name + ".stale.db")
    _sh.copyfile(db.path, p2)
    db = _db(str(p2))
    # a quarter of the snapshots get a NAV computed 20 minutes earlier (stale for a 5-minute limit)
    rng = random.Random(9)
    with db._conn() as conn:
        rows = conn.execute("SELECT rowid, date, time FROM nav_intraday").fetchall()
        upd = []
        for rid, d, t in rows:
            sec = (t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100
            if sec >= 3600 and rng.random() < 0.25:
                s2 = sec - 1200
                upd.append((d, (s2 // 3600) * 10000 + (s2 // 60 % 60) * 100 + s2 % 60, rid))
            else:
                upd.append((d, t, rid))
        conn.executemany("UPDATE nav_intraday SET nav_date=?, nav_time=? WHERE rowid=?", upd)
    hp = _base(fill_mode="hold", fill_max_rel_pct=2.0, position_pct=50, max_nav_age_min=5, max_nav_age_days=1)
    res = _run(db, hp)
    info = {}
    for sid, lab in D._universe(db, None, None):
        L = D._load_ex(db, sid, None, None, hp)
        valid = [r[0] * 86400 + D._sec(r[2]) for r in L["rows"]]
        info[lab] = (valid, sorted(L["day_vol"].stale))
    n_st = sum(len(v[1]) for v in info.values())
    bad, n = [], 0
    for t in res["trades"]:
        for d, tm, side in ((t["entry_date"], t["entry_time"], "خرید"), (t["exit_date"], t["exit_time"], "فروش")):
            if side == "فروش" and t["exit_reason"] == "end":
                continue
            valid, stale = info[t["symbol"]]
            T = D._ord(d) * 86400 + D._sec(tm)
            lv = valid[_bs.bisect_right(valid, T) - 1] if _bs.bisect_right(valid, T) else -1
            ls = stale[_bs.bisect_right(stale, T) - 1] if _bs.bisect_right(stale, T) else -1
            n += 1
            if ls > lv:
                bad.append(f"{t['symbol']} {side} {d} {tm}")
    R.add(g, "صندوقی که آخرین اسنپ‌شاتش NAV کهنه دارد نه خریده می‌شود نه فروخته (حتی برای جابه‌جایی)",
          not bad and n > 0 and n_st > 0, "؛ ".join(bad[:3]) or f"{n} خرید/فروش، {n_st} اسنپ‌شات NAV کهنه")
    # strict: at the instant of every buy and every switch sale, the fund's NAV is at most the limit old
    old_ = [f"{t['symbol']} {k} {t[k]}" for t in res["trades"] for k in ("nav_age_in_min", "nav_age_out_min")
            if not (k == "nav_age_out_min" and t["exit_reason"] == "end")
            and (t.get(k) is None or t[k] > hp.max_nav_age_min + 1e-9)]
    R.add(g, "در لحظهٔ هر خرید و هر فروش، سن NAV همان صندوق حداکثر ۵ دقیقه است (در نتایج برای هر معامله ثبت می‌شود)",
          not old_ and len(res["trades"]) > 0, "؛ ".join(old_[:3]) or f"{len(res['trades'])} معامله")
    import discount_fast as X
    if X.available():
        st = S.Study(db, None, None, None, None, hp, {"fill_switch_pct": [0.8]}, blocks=4, min_trades=1, workers=1)
        st._open(db)
        U = st._universe(hp)
        d0, d1 = st.dates[0], st.dates[-1]
        ctx = st._fill_ctx(U, d0, d1)
        m1, m2 = [], []
        a1, _s1, _c1 = D._portfolio_summary([], hp, d0, d1, fill=ctx, marks=m1)
        a2 = X.run_hold(ctx, hp, d0, d1, marks=m2)
        R.add(g, "با NAV کهنه هم شبیه‌سازی کامپایل‌شده دقیقاً همان نتیجهٔ پایتون را می‌دهد",
              a2 is not None and [asdict(x) for x in a1] == [asdict(x) for x in a2] and m1 == m2 and len(a1) > 0,
              f"{len(a1)} معامله")
    from discount_study import space_info
    info2 = {d["key"]: d for d in space_info()["dims"]}
    R.add(g, "سن NAV پارامتر بهینه‌سازی نیست (یک قاعدهٔ ثابت است) و پیش‌فرض ۵ دقیقه است",
          "max_nav_age_min" not in info2 and "max_nav_age_days" not in info2 and "max_nav_age_min" not in S.DIMS
          and D.DiscountParams().max_nav_age_min == 5, "")


def test_volume_study(R: Results, db):
    """Capacity study: only the capital changes; buys stay within the visible depth, so a bigger capital fills a
    smaller share of its intended size; the API runs it in the background."""
    import discount_volume as VS
    g = "مطالعهٔ حجم"
    hp = _base(fill_mode="hold", fill_max_rel_pct=2.0, position_pct=50, participation_pct=2.0)
    res = VS.run_volume_study(db, None, None, None, None, hp, [1e8, 1e10, 1e12])
    rows = res["rows"]
    fills = [r["avg_size_ratio_pct"] for r in rows if r["avg_size_ratio_pct"] is not None]
    ok = (len(rows) == 3 and [r["capital"] for r in rows] == [1e8, 1e10, 1e12]
          and len(fills) >= 2 and all(a >= b - 1e-9 for a, b in zip(fills, fills[1:]))
          and rows[-1]["trades"] <= rows[0]["trades"] and rows[0]["trades"] > 0)
    R.add(g, "فقط سرمایه عوض می‌شود؛ با سرمایهٔ بزرگ‌تر سهم پرشدهٔ هر خرید کمتر است (و خرید زیر یک‌بیستم هدف انجام نمی‌شود)", ok,
          " · ".join(f"{r['capital']:.0e}: {r['return_pct']}% پرشدن {r['avg_size_ratio_pct']}% محدود {r['limited_by_volume']}" for r in rows))
    from web_server import create_app
    cl = create_app(db).test_client()
    r = cl.post("/api/disc/volume", json={"symbols": "REVA,GOLDX", "capitals": "0.5, 5", "fillmode": "hold", "fillmax": "2"})
    st = None
    for _ in range(300):
        st = cl.get("/api/disc/volume/status").get_json()
        if not st["running"] and st["result"] is not None:
            break
        time.sleep(0.1)
    rr = (st or {}).get("result") or {}
    R.add(g, "API مطالعهٔ حجم در پس‌زمینه اجرا و نتیجه برمی‌گرداند",
          r.status_code == 200 and len(rr.get("rows") or []) == 2 and rr["rows"][0]["capital"] == 5e8, str(rr.get("error") or ""))


def test_optimizer_coverage(R: Results):
    """Every strategy / execution parameter of the backtest can be studied by the optimizer, the form's default is
    among its choices, and the study dialog can put every one of them back into the form."""
    import re as _re
    dflt = asdict(D.DiscountParams())
    # the capital: volume study; the data: a choice; the NAV age: a fixed rule (only a current NAV is traded)
    not_dims = {"initial_capital", "dataset", "tse_grid_sec", "max_nav_age_days", "max_nav_age_min"}
    miss = [f for f in dflt if f not in S.DIMS and f not in not_dims]
    off = [f"{d}={dflt[d]}" for d in S.ORDER if dflt[d] not in S.DIMS[d]["choices"]]
    html = _html()
    m = _re.search(r"const DS_FORM = \{(.*?)\};", html, _re.S)
    keys = set(_re.findall(r"(\w+):\s*'disc-", m.group(1))) if m else set()
    nform = [d for d in S.ORDER if d not in keys]
    R.add("بهینه‌ساز", "همهٔ پارامترهای بک‌تست در بهینه‌ساز هستند، پیش‌فرض فرم جزو گزینه‌هاست و «اعمال» همه را به فرم برمی‌گرداند",
          not miss and not off and not nform, f"جا افتاده: {miss} · پیش‌فرض خارج از گزینه‌ها: {off} · بدون نگاشت فرم: {nform}")


def test_optimizer_reliability(R: Results, db_path: str):
    """The optimizer's reliability diagnostics are calibrated: where bubbles mean-revert the winner's edge over the
    passive index is significant (bootstrap) and the ranking carries over (PBO low); where each fund's bubble is a
    random walk (no information), no edge is reported and the verdict is never "reliable"."""
    import shutil as _sh
    import sqlite3 as _sq
    g = "بهینه‌ساز: قابلیت اطمینان"
    hp = _base(fill_mode="hold", fill_max_rel_pct=0.3, position_pct=50, participation_pct=0.0)
    space = {"fill_switch_pct": [0.3, 0.5, 0.8, 1.2], "position_pct": [20, 33, 50], "fill_min_hold_min": [0, 60, 240],
             "fill_max_rel_pct": [-0.1, 0.0, 0.1, 0.3]}

    def study(path):
        return S.run_study(_db(path), None, None, None, None, base=hp, space=space, n_samples=80, blocks=6,
                           test_frac=0.34, max_seconds=120, min_trades=1, workers=1)
    r = study(db_path)
    rel = r.get("reliability") or {}
    vp = ((rel.get("bootstrap") or {}).get("vs_passive") or {})
    pb = rel.get("pbo") or {}
    ok_sig = (vp.get("p_positive") or 0) >= 0.9 and pb.get("pbo") is not None and pb["pbo"] <= 0.5 \
        and r.get("recommended") is not None and (rel.get("convergence") or {}).get("neighbours_checked", 0) > 0
    R.add(g, "دنیای دارای سیگنال: برتری بر شاخص هم‌وزن معنادار (بوت‌استرپ) و رتبه‌بندی تکرارپذیر (PBO پایین)؛ ترکیب پیشنهادی ناحیهٔ پایدار ساخته می‌شود",
          ok_sig, f"p={vp.get('p_positive')} PBO={pb.get('pbo')} پیشنهادی={'بله' if r.get('recommended') else 'خیر'}")
    bad, ps = [], []
    for sd in (1, 2, 3):
        p2 = f"{db_path}.noise{sd}.db"
        _sh.copyfile(db_path, p2)
        con = _sq.connect(p2)
        rng = random.Random(sd)
        walk, up = {}, []
        for rid, sid, nav in con.execute("SELECT rowid, symbol_id, nav FROM nav_intraday ORDER BY symbol_id, date, time"):
            w = walk.get(sid, rng.gauss(0, 0.01)) + rng.gauss(0, 0.0015)      # a random walk: never pulled back
            walk[sid] = max(-0.08, min(0.08, w))
            up.append((nav * (1 + walk[sid]), rid))
        con.executemany("UPDATE nav_intraday SET last=? WHERE rowid=?", up)
        con.commit()
        con.close()
        rn = study(p2)
        pp = (((rn.get("reliability") or {}).get("bootstrap") or {}).get("vs_passive") or {}).get("p_positive")
        ps.append(pp)
        if pp is None or pp >= 0.9 or rn["verdict"]["overall"] == "ok":
            bad.append(f"بذر {sd}: p={pp} حکم={rn['verdict']['overall']}")
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(p2 + ext)
            except OSError:
                pass
    R.add(g, "دنیای بی‌سیگنال (حباب گام تصادفی): هیچ برتریِ معناداری گزارش نمی‌شود و حکم هرگز «قابل‌اعتنا» نیست",
          not bad, "؛ ".join(bad) or f"احتمال برتری در ۳ دنیا: {ps}")


def test_parallel_study(R: Results, db):
    """The optimizer's parallel workers (separate processes) must find exactly what one process finds."""
    import sys as _sys
    main = getattr(_sys.modules.get("__main__"), "__file__", None)
    if not main or not os.path.exists(main):
        R.add("بهینه‌سازی موازی", "نتیجهٔ موازی = نتیجهٔ تک‌هسته‌ای", None, "اجرا از stdin: پردازه‌های جدا ساخته نمی‌شوند")
        return
    hp = _base(fill_mode="hold", fill_max_rel_pct=2.0, position_pct=50)
    space = {"fill_switch_pct": [0.3, 0.8, 1.5], "position_pct": [25, 50], "fill_min_hold_min": [0, 60]}
    out = {}
    for w in (1, 2):
        out[w] = S.run_study(db, None, None, None, None, base=hp, space=space, n_samples=24, blocks=4, test_frac=0.3,
                             max_seconds=60, min_trades=1, workers=w)
    top = {w: {tuple(sorted(r["cfg"].items())): r["score"] for r in out[w]["top"]} for w in out}
    tm = out[2].get("timing") or {}
    R.add("بهینه‌سازی موازی", "نتیجهٔ موازی (۲ هسته) دقیقاً همان نتیجهٔ تک‌هسته‌ای است",
          top[1] == top[2] and tm.get("workers") == 2 and not tm.get("parallel_error"),
          f"{len(top[1])} ترکیب برتر؛ timing={tm}")


def test_validation_hold(R: Results, db):
    """Validation of the always-invested mode: the placebo (rank by ANOTHER fund's bubble) must be calibrated —
    rarely 'significant' where bubbles carry no information, always where they mean-revert."""
    import discount_validation as V
    g = "اعتبارسنجی حالت «همیشه سرمایه‌گذاری»"
    p = D.DiscountParams(initial_capital=1e10, position_pct=25, fill_mode="hold", fill_max_rel_pct=2.0, participation_pct=0,
                         half_spread_pct=0.02, baseline_days=0, mr_center="off", crash_drop_pct=0, max_nav_age_min=30)

    def pv(kind, sd):
        U = V.universe_from_rows([{"label": f"W{i}", "rows": V._synthetic_rows(kind, seed=100 + sd * 7 + i), "day_vol": {}}
                                  for i in range(6)])
        real = V._hold_run(V._hold_ctx(U), p, U["d0"], U["d1"])["portfolio_return_pct"]
        return V.hold_placebo(U, p, real, n=30, max_seconds=30)["p_value"]
    noise = [pv("noise", sd) for sd in range(8)]
    R.add(g, "placebo کالیبره است: در دنیای بدون اطلاعات (حباب = قدم‌زدن تصادفی) به‌ندرت «معنادار» می‌گوید",
          sum(1 for x in noise if x <= 0.05) <= 2, f"p-valueها: {noise}")
    rev = [pv("revert", sd) for sd in range(3)]
    R.add(g, "placebo قدرت دارد: در دنیای با بازگشت حباب همیشه معنادار است", all(x <= 0.05 for x in rev), f"p-valueها: {rev}")
    res = V.run_validation(db, None, None, None, None, _base(fill_mode="hold", fill_max_rel_pct=2.0, position_pct=25),
                           n_perm=30, max_seconds=10)
    ok = (res.get("mode") == "hold" and res["placebo"].get("n", 0) >= 20 and len(res["blocks"].get("blocks", [])) == 4
          and len(res["stress"]) >= 6 and len(res["verdict"]["findings"]) == 3
          and res["stress"][0]["return_pct"] == res["summary"]["portfolio_return_pct"])
    R.add(g, "اعتبارسنجی در این حالت معامله و گزارش واقعی می‌دهد (نه صفر): placebo، بلوک‌ها، سخت‌گیری، حکم",
          ok, f"{res.get('summary', {}).get('trade_count')} معامله · placebo {res.get('placebo', {}).get('n')}")


def test_realism(R: Results, db):
    """Volume cap basis (so far vs whole day) and execution delay (fill at the N-th next fresh snapshot)."""
    g = "واقع‌بینی اجرا (حجم تا لحظه، تأخیر)"
    base = _base(entry_discount_pct=0.3)
    funds = dict((lab, sid) for sid, lab in D._universe(db, None, None))
    # --- volume cap: no entry is larger than participation × volume traded UP TO that snapshot ---------------
    pct = 0.4
    pv = replace(base, participation_pct=pct, participation_basis="sofar")
    res = _run(db, pv)
    bad, n = [], 0
    loaded = {lab: D._load_ex(db, sid, None, None, pv) for lab, sid in funds.items()}
    for t in res["trades"]:
        cum = loaded[t["symbol"]]["day_vol"].cum.get((t["entry_date"], t["entry_time"]))
        if cum is None:
            continue
        n += 1
        if t["volume"] > cum * pct / 100.0 + 1:
            bad.append(f"{t['symbol']} {t['entry_date']} {t['entry_time']}: {t['volume']} > {cum * pct / 100:.0f}")
    R.add(g, "سقف حجم: هیچ ورودی از «سهم × حجمِ معامله‌شده تا همان لحظه» بزرگ‌تر نیست", not bad and n > 0,
          "؛ ".join(bad[:3]) or f"{n} ورود")
    day_res = _run(db, replace(pv, participation_basis="day"))
    R.add(g, "مبنای «کل حجم روز» (خوش‌بینانه) معامله‌های بزرگ‌تر/بیشتر از «تا لحظه» می‌دهد",
          sum(t["volume"] for t in day_res["trades"]) >= sum(t["volume"] for t in res["trades"]), "")
    # --- execution delay ---------------------------------------------------------------------------------------
    dly = 2
    pd_ = replace(base, exec_delay_snaps=dly)
    r0, r2 = _run(db, base), _run(db, pd_)
    bad, n = [], 0
    ld = {lab: D._load_ex(db, sid, None, None, pd_) for lab, sid in funds.items()}
    thr = -pd_.entry_discount_pct / 100.0
    for t in r2["trades"]:
        rows = ld[t["symbol"]]["rows"]
        j = next((k for k, r in enumerate(rows) if r[1] == t["entry_date"] and r[2] == t["entry_time"]), None)
        if j is None:
            continue
        n += 1
        fresh_back, i0 = 0, j - 1
        while i0 >= 0 and rows[i0][1] == t["entry_date"]:
            if rows[i0][5]:
                fresh_back += 1
                if fresh_back == dly:
                    break
            i0 -= 1
        if fresh_back != dly or i0 < 0 or rows[i0][4] / rows[i0][3] - 1.0 > thr + 1e-9:
            bad.append(f"{t['symbol']} {t['entry_date']} {t['entry_time']}")
        if abs(t["entry_price"] - round(rows[j][4] * (1 + pd_.half_spread_pct / 100.0), 2)) > 0.011:
            bad.append(f"{t['symbol']}: قیمت ورود ≠ قیمت اسنپ‌شات پرشدن")
    R.add(g, "تأخیر اجرا: هر ورود دقیقاً در N‌امین اسنپ‌شات تازهٔ بعد از یک سیگنال معتبر و با قیمت همان اسنپ‌شات پر می‌شود",
          not bad and n > 0, "؛ ".join(bad[:3]) or f"{n} ورود")
    R.add(g, "تأخیر اجرا نتیجه را عوض می‌کند و دو اجرا یک‌سان نیستند", _sig(r0) != _sig(r2), "")
    hp = _base(fill_mode="hold", fill_max_rel_pct=2.0, position_pct=25, exec_delay_snaps=2, participation_basis="sofar",
               participation_pct=5.0)
    rh = _run(db, hp)
    lo, cash_final = _cash_path(rh, hp)
    bad = []
    if lo < -1e-6 * hp.initial_capital - 1:
        bad.append(f"نقد منفی {lo / hp.initial_capital * 100:.3f}٪")
    if abs(cash_final - rh["summary"]["final_capital"]) > max(50, 1e-6 * hp.initial_capital):
        bad.append("سرمایهٔ نهایی ≠ مسیر نقد")
    if any(t["exit_reason"] not in ("rotate", "end") for t in rh["trades"]):
        bad.append("علت خروجِ غیرمجاز")
    R.add(g, "حالت «نگه‌داری» با تأخیر اجرا: نقد هرگز منفی نیست، سرمایهٔ نهایی با مسیر نقد می‌خواند، علت‌های خروج مجاز",
          not bad and len(rh["trades"]) > 0, "؛ ".join(bad) or f"{len(rh['trades'])} معامله")


def test_attribution(R: Results, db):
    """Profit attribution (NAV move vs bubble convergence vs spread vs fees) must reconcile exactly."""
    g = "تفکیک سود NAV / حباب"
    for lab, p in (("قواعد عادی", _base(entry_discount_pct=0.3, position_pct=25)),
                   ("نگه‌داری", _base(fill_mode="hold", fill_max_rel_pct=2.0, position_pct=25))):
        res = _run(db, p)
        a = res["attribution"]
        T = a["totals"]
        net_report = res["summary"]["final_capital"] - res["summary"]["initial_capital"]
        tol = max(50.0, 2.0 * len(res["trades"]))               # per-trade rounding of rials
        R.add(g, f"{lab}: NAV + حباب − اسپرد − کارمزد = سود خالص",
              abs(T["nav"] + T["bubble"] - T["spread"] - T["fees"] - T["net"]) <= tol, f"{T}")
        R.add(g, f"{lab}: جمع خالصِ تفکیک = سود خالص گزارش‌شدهٔ پرتفوی", abs(T["net"] - net_report) <= tol,
              f"{T['net']:,.0f} ≠ {net_report:,.0f}")
        hs = p.half_spread_pct / 100.0
        nav_ind = sum(t["buy_notional"] * (t["nav_exit"] / t["nav_entry"] - 1) for t in res["trades"] if t["nav_entry"] > 0)
        sp_ind = sum(t["buy_notional"] * hs / (1 + hs) + t["sell_notional"] * hs / (1 - hs) for t in res["trades"])
        R.add(g, f"{lab}: اثر NAV و اسپرد با محاسبهٔ مستقل از روی معامله‌ها یکی است",
              abs(T["nav"] - nav_ind) <= tol and abs(T["spread"] - sp_ind) <= tol, f"{T['nav']:,.0f} / {nav_ind:,.0f}")
        R.add(g, f"{lab}: جمع سود هر نماد = جمع کل", abs(sum(r["net"] for r in a["per_symbol"]) - T["net"]) <= tol, "")
    # a world where price == NAV·const has no bubble PnL beyond spread/fees: with spread = fees = 0, bubble == net − nav
    res0 = _run(db, _base(entry_discount_pct=0.3, half_spread_pct=0.0, buy_fee=0.0, sell_fee=0.0))
    T0 = res0["attribution"]["totals"]
    R.add(g, "بدون اسپرد و کارمزد: اسپرد و کارمزد صفر و NAV + حباب = خالص", T0["spread"] == 0 and T0["fees"] == 0
          and abs(T0["nav"] + T0["bubble"] - T0["net"]) <= max(50.0, 2.0 * len(res0["trades"])), str(T0))


def test_export(R: Results, db):
    """Result export: pushes to a separate branch of a (temporary) bare remote, fast-forward on the second push,
    and never touches the working tree, index, HEAD or current branch."""
    import subprocess
    import result_export as X
    g = "ارسال نتایج برای تحلیل"
    tmp = tempfile.mkdtemp(prefix="disc_export_test_")
    def git(*a, cwd=None):
        return subprocess.run(["git", *a], cwd=cwd or work, capture_output=True, text=True,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    bare, work = os.path.join(tmp, "remote.git"), os.path.join(tmp, "work")
    subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
    os.makedirs(work)
    git("init", "-q", "-b", "main"); git("remote", "add", "origin", bare)
    open(os.path.join(work, "a.txt"), "w").write("x"); git("add", "a.txt"); git("commit", "-q", "-m", "init")
    open(os.path.join(work, "dirty.txt"), "w").write("keep me")                  # untracked file must survive
    head0 = git("rev-parse", "HEAD").stdout.strip()
    res1 = _run(db, _base(entry_discount_pct=0.5))
    files = X.build_files({"backtest": res1, "study": {"mode": "study", "x": [1, 2]}}, {"commit": head0})
    r1 = X.push_files(Path(work), files)
    R.add(g, "اولین پوش شاخهٔ نتایج را می‌سازد", r1["ok"], r1.get("message", ""))
    ls = git("ls-tree", "-r", "--name-only", "analysis-results", cwd=bare).stdout.split()
    R.add(g, "فایل‌های نتایج روی شاخهٔ راه دور هستند", {"results/backtest.json", "results/optimizer.json", "results/meta.json"} <= set(ls), str(ls))
    r2 = X.push_files(Path(work), X.build_files({"backtest": res1}, {}))
    n = git("rev-list", "--count", "analysis-results", cwd=bare).stdout.strip()
    R.add(g, "پوش دوم fast-forward است (تاریخچهٔ هر ارسال می‌ماند)", r2["ok"] and n == "2", f"{n} commit; {r2.get('message', '')}")
    R.add(g, "پوشهٔ کاری، ایندکس، HEAD و شاخهٔ فعلی دست‌نخورده‌اند",
          git("rev-parse", "HEAD").stdout.strip() == head0 and git("status", "--porcelain").stdout.strip() == "?? dirty.txt"
          and git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main", "")
    big = json.loads(files["results/backtest.json"])
    R.add(g, "محتوای ارسالی همان بک‌تست است (پارامترها و خلاصه)", big["summary"]["final_capital"] == res1["summary"]["final_capital"]
          and "params" in big, "")
    bad = X.push_files(Path(work), files, remote="nowhere")
    R.add(g, "خطای مخزن راه دور به‌صورت پیام قابل فهم برمی‌گردد (کرش نمی‌کند)", (not bad["ok"]) and bool(bad.get("message")), "")
    shutil.rmtree(tmp, ignore_errors=True)


def test_fill(R: Results, db):
    g = "پر کردن سرمایهٔ بیکار"
    rng = random.Random(8)
    bad_cash, bad_final, bad_overlap, bad_gate, n_fill, n_run = [], [], [], [], 0, 10
    for it in range(n_run):
        p = _random_params(rng)
        p = replace(p, fill_mode="best", fill_max_rel_pct=rng.choice([-0.2, 0.0, 0.2]),
                    fill_exit_rel_pct=rng.choice([0.1, 0.3, 0.8]), participation_pct=rng.choice([0.0, 5.0]))
        res = _run(db, p)
        lo, cash_final = _cash_path(res, p)
        if lo < -1e-6 * p.initial_capital - 1:
            bad_cash.append(f"#{it}: کمترین نقد {lo / p.initial_capital * 100:.3f}٪")
        if abs(cash_final - res["summary"]["final_capital"]) > max(50, 1e-6 * p.initial_capital):
            bad_final.append(f"#{it}: مسیر نقد {cash_final:,.0f} ≠ گزارش {res['summary']['final_capital']:,.0f}")
        by = {}
        for t in res["trades"]:
            a = D._ord(t["entry_date"]) * 86400 + D._sec(t["entry_time"])
            b = D._ord(t["exit_date"]) * 86400 + D._sec(t["exit_time"])
            by.setdefault(t["symbol"], []).append((a, b, t["origin"]))
        for sym, lst in by.items():
            lst.sort()
            hi = None
            # a fund is never held twice at once: intervals of one ENTRY (partial chunks share an entry) may touch;
            # lots of one parked position (its first buy and top-ups) overlap by design — never a parked position
            # and a normal trade, nor two normal trades
            ents = {}
            for a, b, o in lst:
                ents[(a, o)] = max(ents.get((a, o), 0), b)
            seq = sorted((a, b, o) for (a, o), b in ents.items())
            for x, (a1, b1, o1) in enumerate(seq):
                for a2, b2, o2 in seq[x + 1:]:
                    if a2 >= b1:
                        break
                    if not (o1 == "fill" and o2 == "fill"):
                        bad_overlap.append(f"#{it} {sym}")
        for t in res["trades"]:
            if t["origin"] != "fill":
                continue
            n_fill += 1
            if t["rel_entry_pct"] > p.fill_max_rel_pct + p.half_spread_pct * 2 + 0.05:
                bad_gate.append(f"#{it} {t['symbol']}: {t['rel_entry_pct']} > {p.fill_max_rel_pct}")
            if t["exit_reason"] not in ("signal", "time", "stop", "rotate", "end"):
                bad_gate.append(f"#{it} {t['symbol']}: علت خروج {t['exit_reason']}")
        e = res["exposure"]
        if e and not (0 <= e["avg_pct"] <= 100.0001 and e["peak_pct"] <= 100.0001):
            bad_gate.append(f"#{it}: سرمایهٔ درگیر {e['avg_pct']} / {e['peak_pct']}")
    R.add(g, "نقد هرگز منفی نمی‌شود (جمع نقدِ همهٔ معاملات، عادی + پارک‌شده، با استخر مشترک)", not bad_cash,
          "؛ ".join(bad_cash[:3]) or f"{n_run} تنظیم تصادفی، {n_fill} معاملهٔ پارک‌شده")
    R.add(g, "سرمایهٔ نهایی گزارش‌شده = مسیرِ نقدِ مستقل از روی معاملات", not bad_final, "؛ ".join(bad_final[:3]))
    R.add(g, "یک صندوق هم‌زمان دو بار نگه داشته نمی‌شود (پارک‌شده روی معاملهٔ عادی نمی‌نشیند)", not bad_overlap,
          "؛ ".join(bad_overlap[:3]))
    R.add(g, "پوزیشن پارک‌شده فقط در صندوقِ در/زیر معمولِ خودش (حد «حداکثر حباب نسبی»)؛ علت‌های خروج مجاز؛ سرمایهٔ درگیر ≤ ۱۰۰٪",
          not bad_gate and n_fill > 0, "؛ ".join(bad_gate[:3]) or f"{n_fill} معامله")
    base = _base(entry_discount_pct=0.8, position_pct=20)
    off = _sig(_run(db, base))
    none = _sig(_run(db, replace(base, fill_mode="best", fill_max_rel_pct=-50.0)))
    R.add(g, "اگر هیچ صندوقی شرط «حداکثر حباب نسبی» را نداشته باشد، نتیجه برابر حالت خاموش است", off == none, "")
    on = _run(db, replace(base, fill_mode="best"))
    fi = on["fill_info"]
    R.add(g, "پارک کردن، سرمایهٔ درگیر را بالا می‌برد و معاملهٔ پارک‌شده دارد",
          fi is not None and fi["trades"] > 0 and fi["avg_exposure_pct"] > fi["base_avg_exposure_pct"],
          f"درگیری {fi['base_avg_exposure_pct']}→{fi['avg_exposure_pct']}٪، {fi['trades']} معامله" if fi else "")
    s = on["summary"]
    ind = sum(t["buy_notional"] * ((D._ord(t["exit_date"]) * 86400 + D._sec(t["exit_time"]))
                                   - (D._ord(t["entry_date"]) * 86400 + D._sec(t["entry_time"]))) for t in on["trades"])
    e = on["exposure"]
    R.add(g, "انتگرال سرمایهٔ درگیر شاملِ پارک‌شده‌ها هم هست (جمع مستقل معاملات)",
          ind > 0 and abs(e["invested_rial_seconds"] / ind - 1) < 3e-3, f"نسبت {e['invested_rial_seconds'] / ind:.4f}" if ind else "")

# --------------------------------------------------------------------------- #
#  7) Loss explanation                                                         #
# --------------------------------------------------------------------------- #

def test_explain(R: Results, db):
    res = _run(db, _base(entry_discount_pct=0.3, max_hold_days=3))
    bad, n = [], 0
    for t in res["trades"]:
        w = t.get("why")
        if not w:
            continue
        n += 1
        c = w["components"]
        recon = c["gross_pct"] + c["spread_pct"] + c["fee_pct"]
        if abs(recon - c["net_pct"]) > 0.05:
            bad.append(f"{t['symbol']} {t['entry_date']}: {recon:.3f} ≠ {c['net_pct']:.3f}")
        if t["net_pnl"] > 0:
            bad.append("تفسیر برای معامله‌ٔ سودده")
    R.add("تفسیر زیان", "تفکیک زیان جمع می‌شود (ناخالص − اسپرد − کارمزد = خالص)", not bad and n > 0,
          f"{n} معاملهٔ زیان‌ده" + (": " + "؛ ".join(bad[:3]) if bad else ""))


# --------------------------------------------------------------------------- #
#  8) Browser (optional)                                                       #
# --------------------------------------------------------------------------- #

def test_ui_browser(R: Results, db_path: str, port: int = 5199):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        R.add("مرورگر", "Playwright در دسترس نیست", None, "pip install playwright و playwright install chromium")
        return
    import threading
    from web_server import create_app
    app = create_app(_db(db_path))
    th = threading.Thread(target=lambda: app.run(port=port, threaded=True), daemon=True)
    th.start()
    time.sleep(2)
    errs = []
    try:
        with sync_playwright() as p:
            exe = os.environ.get("CHROMIUM_PATH")
            b = p.chromium.launch(**({"executable_path": exe} if exe else {}), args=["--no-sandbox"])
            pg = b.new_page(viewport={"width": 1400, "height": 1500})
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" else None)
            lwc = os.environ.get("LWC_LOCAL")
            if lwc:
                pg.route("**/unpkg.com/**", lambda r: r.fulfill(status=200, content_type="application/javascript", path=lwc))
            pg.goto(f"http://localhost:{port}/")
            pg.wait_for_timeout(2500)
            pg.evaluate("document.getElementById('loading').classList.add('hidden')")
            pg.fill("#disc-capital", "5000"); pg.fill("#disc-part", "0"); pg.fill("#disc-mintrades", "10")
            sel = pg.evaluate("discSelectedIds().length")
            R.add("مرورگر", "صندوق‌های فعال انتخاب شده‌اند", sel > 0, f"{sel} صندوق")
            # a dropdown choice disables the fields it makes meaningless (and re-enables them)
            rel = pg.evaluate("""() => {
              const set = (id, v) => { const e = document.getElementById(id); e.value = v; e.dispatchEvent(new Event('change', {bubbles: true})); };
              const dis = id => document.getElementById(id).disabled;
              const o = {}; const fm0 = document.getElementById('disc-fillmode').value, mr0 = document.getElementById('disc-mrcenter').value;
              set('disc-fillmode', 'hold'); o.hold = [dis('disc-entry'), dis('disc-stop'), !dis('disc-fillswitch')];
              set('disc-fillmode', 'off'); set('disc-entrymode', 'fund');
              o.off = [!dis('disc-entry'), dis('disc-fillswitch'), dis('disc-fillmax'), dis('disc-ientry')];
              set('disc-mrcenter', 'off'); o.mr = [dis('disc-mrwin'), dis('disc-mrmin')];
              set('disc-mrcenter', 'self'); o.mr2 = [!dis('disc-mrwin')];
              set('disc-fillmode', fm0); set('disc-mrcenter', mr0);
              return o; }""")
            R.add("مرورگر", "انتخاب گزینهٔ کرکره‌ای، فیلدهای بی‌اثر همان حالت را خاموش و بقیه را روشن می‌کند",
                  all(all(v) for v in rel.values()), str(rel))
            pg.click("#disc-run"); pg.wait_for_selector("#disc-cards .opt-mc", timeout=90000)
            R.add("مرورگر", "بک‌تست اجرا و نتیجه رسم می‌شود", True, "")
            R.add("مرورگر", "نمودار سرمایهٔ درگیر رسم می‌شود", pg.locator("#disc-exp-chart canvas").count() > 0, "")
            # result tabs: a new result must not wipe the previous one
            R.add("مرورگر", "نتیجهٔ بک‌تست در یک تب نمایش داده می‌شود", pg.locator("#disc-res-bar .dres-tab").count() == 1, "")
            first_final = pg.evaluate("window._discLastRes.summary.final_capital")
            pg.click("#disc-stats-btn"); pg.wait_for_selector("#disc-stats .disc-tbl", timeout=60000)
            R.add("مرورگر", "نتیجهٔ جدید (آمار) تب جدید می‌سازد و بک‌تست پاک نمی‌شود",
                  pg.locator("#disc-res-bar .dres-tab").count() == 2 and pg.locator("#disc-cards .opt-mc").count() == 0, "")
            pg.locator("#disc-res-bar .dres-tab").first.click(); pg.wait_for_selector("#disc-cards .opt-mc")
            R.add("مرورگر", "برگشتن به تب اول، همان بک‌تست (با نمودار) را نشان می‌دهد و آمار را نه",
                  pg.locator("#disc-exp-chart canvas").count() > 0 and pg.locator("#disc-stats .disc-tbl").count() == 0
                  and pg.evaluate("window._discLastRes.summary.final_capital") == first_final, "")
            pg.locator("#disc-res-bar .dres-tab").nth(1).locator(".dres-x").click()
            R.add("مرورگر", "بستن یک تب فقط همان را حذف می‌کند", pg.locator("#disc-res-bar .dres-tab").count() == 1
                  and pg.locator("#disc-cards .opt-mc").count() > 0, "")
            # the independent verification opens its own result tab (the backtest tab stays)
            pg.click("#disc-verify-btn"); pg.wait_for_selector("#disc-verify .disc-tbl tbody tr", timeout=90000)
            vt = pg.locator("#disc-res-bar .dres-tab").count()
            vtitle = pg.locator("#disc-res-bar .dres-tab.active .dres-t").inner_text()
            vrows = pg.locator("#disc-verify .disc-tbl tbody tr").count()
            pg.locator("#disc-res-bar .dres-tab").first.click(); pg.wait_for_selector("#disc-cards .opt-mc")
            R.add("مرورگر", "بررسی مستقل در تب جدید خودش نمایش داده می‌شود و تب بک‌تست سر جایش می‌ماند",
                  vt == 2 and "بررسی مستقل ✅" in vtitle and vrows >= 10 and pg.locator("#disc-verify .disc-tbl").count() == 0,
                  f"{vt} تب، عنوان «{vtitle}»، {vrows} ردیف")
            pg.locator("#disc-res-bar .dres-tab").nth(1).locator(".dres-x").click()
            pg.click("#disc-tse-btn"); pg.wait_for_selector("#disc-tse .disc-tbl tbody tr")
            R.add("مرورگر", "پنل دادهٔ TSE باز می‌شود و صندوق‌های طلا را با روزهای NAV نشان می‌دهد",
                  pg.locator("#disc-tse .disc-tbl tbody tr").count() == 4, pg.locator("#disc-tse").inner_text()[:120])
            pg.locator("#disc-res-bar .dres-tab").first.click(); pg.wait_for_selector("#disc-cards .opt-mc")
            # optimizer: apply result must fill EVERY searched field in the form
            pg.select_option("#disc-fillmode", "hold")
            pg.click("#disc-opt-btn"); pg.wait_for_selector("#disc-study-cfg.visible #ds-dims tbody tr")
            pg.click("text=⚡ سریع"); pg.wait_for_timeout(300)
            on = pg.evaluate("[...document.querySelectorAll('#ds-dims [data-dim]')].filter(c=>c.checked).map(c=>c.dataset.dim)")
            R.add("مرورگر", "در حالت «همیشه سرمایه‌گذاری» پیش‌تنظیم سریع پارامترهای همان حالت را تیک می‌زند",
                  set(on) == {"fill_max_rel_pct", "fill_switch_pct", "fill_min_hold_min"}, str(on))
            pg.click("#ds-cancel"); pg.select_option("#disc-fillmode", "off")
            pg.click("#disc-opt-btn"); pg.wait_for_selector("#disc-study-cfg.visible #ds-dims tbody tr")
            R.add("مرورگر", "دکمهٔ بهینه‌سازی یک پنجرهٔ انتخاب پارامتر باز می‌کند و هنوز چیزی اجرا نشده",
                  pg.locator("#disc-opt .dv-head").count() == 0 and pg.locator("#ds-start").is_visible(), "")
            pg.fill("#ds-n", "60"); pg.fill("#ds-sec", "60"); pg.locator("#ds-n").blur()
            for mode in ("index", "fund"):
                pg.select_option("#disc-entrymode", mode)
                pg.click("text=⚡ سریع"); pg.wait_for_timeout(300)
                on = pg.evaluate("[...document.querySelectorAll('#ds-dims [data-dim]')].filter(c=>c.checked).map(c=>c.dataset.dim)")
                want = {"index": {"index_entry_pct", "index_exit_pct"}, "fund": {"entry_discount_pct", "exit_discount_pct"}}[mode]
                R.add("مرورگر", f"پیش‌تنظیم سریع در حالت «{mode}» پارامترهای همان حالت را تیک می‌زند", want <= set(on), str(on))
            pg.select_option("#disc-entrymode", "fund")
            pg.click("#ds-start")
            R.add("مرورگر", "با «شروع» پنجره بسته می‌شود و مطالعه اجرا می‌شود", not pg.locator("#disc-study-cfg.visible").count(), "")
            for _ in range(150):
                pg.wait_for_timeout(1000)
                if pg.locator("#disc-opt .dv-head").count():
                    break
            ok_study = pg.locator("#disc-opt .dv-head").count() > 0
            R.add("مرورگر", "مطالعهٔ بهینه‌سازی تا گزارش کامل اجرا می‌شود", ok_study, pg.locator("#disc-opt").inner_text()[:150] if not ok_study else "")
            if ok_study:
                cfg = pg.evaluate("_discStudy.best.cfg")
                pg.evaluate("c => discApplyStudy(c)", cfg)
                pg.wait_for_timeout(1500)
                ds_form = dict(re.findall(r"(\w+):\s*'([\w-]+)'", re.search(r"const DS_FORM = \{(.*?)\};", _html(), re.S).group(1)))
                form = pg.evaluate("""m => { const o = {}; for (const k in m) { const e = document.getElementById(m[k]); o[k] = e ? (e.type === 'checkbox' ? e.checked : e.value) : null; } return o; }""", ds_form)

                def expect(k, v):
                    if k in ("index_min_share", "buy_fee", "sell_fee"):
                        return round(v * 100, 4)
                    if k in ("session_start", "session_end"):
                        return f"{int(v) // 10000:02d}:{int(v) // 100 % 100:02d}"
                    return v
                bad = []
                for k, v in cfg.items():
                    if v is None or k not in form:
                        continue
                    e, g = expect(k, v), form[k]
                    same = (g == e) if isinstance(e, (bool, str)) and not isinstance(e, (int, float)) or isinstance(g, bool) \
                        else abs(float(g) - float(e)) < 1e-6
                    if not same:
                        bad.append(f"{k}: فرم {g!r} ≠ ترکیب {e!r}")
                R.add("مرورگر", "«اعمال نتیجه» همهٔ پارامترهای ترکیب برنده را در فرم می‌نویسد", not bad, "؛ ".join(bad) or "همه نوشته شد")
            miss = pg.evaluate("""()=>{const out=[];document.getElementById('disc-app').querySelectorAll('button,label,th,.omk,option,.disc-h,.pill,summary').forEach(e=>{ if(e.offsetParent===null&&e.tagName!=='OPTION')return; const isO=e.tagName==='OPTION'; if(isO?e.title:e.closest('[title]'))return; out.push(e.tagName+':'+(e.textContent||'').trim().replace(/\\s+/g,' ').slice(0,40));});return [...new Set(out)];}""")
            R.add("مرورگر", "هر دکمه/گزینه/ستونِ دیده‌شده تول‌تیپ دارد", not miss, "بدون تول‌تیپ: " + "، ".join(miss[:8]))
            R.add("مرورگر", "هیچ خطای جاوااسکریپت در کنسول نیست", not errs, "؛ ".join(errs[:3]))
            b.close()
    except Exception as e:
        R.add("مرورگر", "اجرای مرورگر", False, f"{type(e).__name__}: {e}")



# --------------------------------------------------------------------------- #
#  9) Mutation check: the tests themselves must be able to fail                 #
# --------------------------------------------------------------------------- #

def test_mutations(R: Results, db):
    """Re-introduce, one at a time, the real bugs this suite was written for; each must be caught."""
    g = "جهش (آزمونِ آزمون‌ها)"

    def caught(fn, *a) -> list:
        r = Results()
        fn(r, *a)
        return [i for i in r.items if i["status"] == "fail"]

    orig_html = _html

    def with_html(transform):
        globals()["_html"] = lambda: transform(orig_html())
        try:
            r = Results()
            test_static_ui(r)
            return [i for i in r.items if i["status"] == "fail"]
        finally:
            globals()["_html"] = orig_html

    cases = []
    o = S.Study._adapt_space
    S.Study._adapt_space = lambda self: None
    try:
        cases.append(("بهینه‌ساز پارامتر بی‌اثرِ حالت شاخص را جستجو کند", caught(test_optimizer_static, db)))
    finally:
        S.Study._adapt_space = o
    o = S.Study._complete
    S.Study._complete = lambda self, c: S._normalize(self._fill(c), self.base)
    try:
        cases.append(("ترکیبِ برنده پارامتر فعالِ خالی داشته باشد", caught(test_optimizer_static, db)))
    finally:
        S.Study._complete = o
    o = D._simulate
    D._simulate = lambda label, rows, dv, p, *a, **k: o(label, rows, dv, replace(p, stop_mode="nav_widen"), *a, **k)
    try:
        cases.append(("موتور نوع حد ضرر را نادیده بگیرد", caught(test_parameter_audit, db)))
    finally:
        D._simulate = o
    o = D._prep
    D._prep = lambda raw, p: o(raw, replace(p, baseline_days=0))
    try:
        cases.append(("موتور تعدیل حباب دائمی را نادیده بگیرد", caught(test_parameter_audit, db)))
    finally:
        D._prep = o
    o = D.effective_start
    D.effective_start = lambda db_, sids, start, end, p, warm=None: start
    try:
        cases.append(("بازده از درون گرم‌شدن سنجیده شود", caught(test_warmup, db)))
    finally:
        D.effective_start = o
    o = D.warmup_days
    D.warmup_days = lambda p: (max(3, p.baseline_days // 3) if p.baseline_days > 0 else 0) + (
        (max(3, p.mr_window_days // 3) + (p.mr_window_days if p.mr_center == "self" else 0)) if p.mr_center != "off" else 0)
    o_pre = D._DayVol.__init__

    def _no_pre(self, *a, **k):            # (the old behaviour: the score warmed up only after the baseline)
        o_pre(self, *a, **k)
        type(self).pre = property(lambda s: None, lambda s, v: None)
    D._DayVol.__init__ = _no_pre
    try:
        cases.append(("امتیاز بازگشت پس از معمولِ صندوق گرم شود (نه هم‌زمان)", caught(test_warmup, db)))
    finally:
        D.warmup_days = o
        D._DayVol.__init__ = o_pre
        if isinstance(D._DayVol.__dict__.get("pre"), property):
            del D._DayVol.pre
    o = D._Parker.make_room
    D._Parker.make_room = lambda self, symbol, need, cash, invested, ts: (cash, invested)
    try:
        cases.append(("پارک‌شده‌ها هنگام نیاز معاملهٔ عادی نقد را پس ندهند", caught(test_fill, db)))
    finally:
        D._Parker.make_room = o
    cases.append(("پیش‌فرض فرم با موتور فرق کند", with_html(lambda h: h.replace(
        'id="disc-buyfee" type="number" step="0.005" value="0.125"', 'id="disc-buyfee" type="number" step="0.005" value="0.145"'))))
    cases.append(("discParams یک کنترل را نخواند", with_html(lambda h: h.replace("navage: v('disc-navage'), ", ""))))
    cases.append(("نگاشت «اعمال» یک پارامتر را نداشته باشد", with_html(lambda h: h.replace(
        "stop_mode: 'disc-stopmode', ", "", 1))))
    for name, f in cases:
        R.add(g, f"اگر «{name}»، آزمون‌ها باید شکست بخورند", bool(f), (f"{len(f)} شکست: " + f[0]["name"][:60]) if f else "آزمون کور است!")

# --------------------------------------------------------------------------- #
#  Runner                                                                      #
# --------------------------------------------------------------------------- #

def run_all(ui: bool = False, progress: dict | None = None, workdir: str | None = None) -> dict:
    R = Results()
    t0 = time.time()
    tmp = workdir or tempfile.mkdtemp(prefix="discount_fulltest_")
    path = make_world(os.path.join(tmp, "world.db"))
    db = _db(path)
    steps = [("ممیزی پارامترها", lambda: test_parameter_audit(R, db)),
             ("هزینه و تکرارپذیری", lambda: test_cost_monotonic(R, db)),
             ("سیم‌کشی وب", lambda: test_web_wiring(R, db)),
             ("رابط کاربری ایستا", lambda: test_static_ui(R)),
             ("بهینه‌ساز", lambda: (test_optimizer_static(R, db), test_optimizer_fill(R, db), test_optimizer(R, db))),
             ("ناوردایی‌ها", lambda: test_invariants(R, db)),
             ("فیلتر ریزش بازار", lambda: test_crash_filter(R, db)),
             ("پر کردن سرمایهٔ بیکار", lambda: test_fill(R, db)),
             ("نگه‌داری تا کاندیدای بهتر", lambda: test_hold(R, db)),
             ("واقع‌بینی اجرا (حجم تا لحظه، تأخیر)", lambda: test_realism(R, db)),
             ("اعتبارسنجی حالت «همیشه سرمایه‌گذاری»", lambda: test_validation_hold(R, db)),
             ("بهینه‌سازی موازی", lambda: (test_optimizer_coverage(R), test_window_marks(R, db), test_fast_replay(R, path), test_parallel_study(R, db))),
             ("بهینه‌ساز: قابلیت اطمینان", lambda: test_optimizer_reliability(R, path)),
             ("مطالعهٔ حجم", lambda: test_volume_study(R, db)),
             ("NAV به‌روز", lambda: test_stale_nav_no_trade(R, db)),
             ("گرم‌شدن", lambda: test_warmup(R, db)),
             ("NAV ریزدانه (feed)", lambda: test_nav_feed(R, path)),
             ("جمع‌آوری دادهٔ TSE (طلا)", lambda: test_tse_collect(R, path)),
             ("بک‌تست روی دادهٔ TSE", lambda: (test_tse_dataset(R, path), test_same_instant_sales(R))),
             ("تفکیک سود NAV / حباب", lambda: test_attribution(R, db)),
             ("ارسال نتایج برای تحلیل", lambda: test_export(R, db)),
             ("تفسیر زیان", lambda: test_explain(R, db)),
             ("جهش (آزمونِ آزمون‌ها)", lambda: test_mutations(R, db))]
    if ui:
        steps.append(("مرورگر", lambda: test_ui_browser(R, path)))
    for i, (name, fn) in enumerate(steps):
        if progress is not None:
            progress.update(phase=name, done=i, total=len(steps))
        try:
            fn()
        except Exception as e:
            R.add(name, "اجرای گروه آزمون", False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
    return {"summary": R.summary(), "items": R.items, "seconds": round(time.time() - t0, 1)}
