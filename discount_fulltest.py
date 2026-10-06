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

import datetime as dt
import hashlib
import json
import math
import os
import random
import re
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
                rows.append((sid, di, t, nav, nd, 0, nav * f, nav * f, 0))
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
                if stale:
                    px_rel -= 0.015
                last = nav_t * (1 + px_rel)
                grew = rng.random() > 0.15                 # 15% of snapshots carry a stale (no-volume) quote
                if grew:
                    vol += rng.randint(30, 400) if thin else rng.randint(300, 4000)
                rows.append((sid, di, t, nav_t, nd, 0, last, last, vol))
            if sym == "GOLDX":                             # closing indication after the last trade
                rows.append((sid, di, b * 10000 + 500, nav, nd, 0, nav * 0.97, nav * 0.97, vol))
    con.executemany("INSERT INTO nav_intraday (symbol_id,date,time,nav,nav_date,nav_time,last,close,vol) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


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
                         participation_pct=0.0, baseline_days=0, half_spread_pct=0.05)
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
    ("حداکثر سن NAV", {}, "max_nav_age_days", [0, 5], "change"),
    ("نیم‌اسپرد", {}, "half_spread_pct", [0.0, 0.3], "change"),
    ("کارمزد خرید", {}, "buy_fee", [0.0, 0.01], "change"),
    ("کارمزد فروش", {}, "sell_fee", [0.0, 0.01], "change"),
    ("سقف حجم", {}, "participation_pct", [0.0, 0.5], "change"),
    ("سهم هر پوزیشن", {}, "position_pct", [5.0, 40.0], "change"),
    ("فقط قیمت تازه", {}, "require_fresh", [True, False], "change"),
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
    "iexit": ("index_exit_pct", "-0.12", -0.12), "mrcenter": ("mr_center", "self", "self"),
    "mrwin": ("mr_window_days", "25", 25), "mrmin": ("mr_min_score", "77", 77.0), "mrhor": ("mr_horizon_days", "7", 7),
    "spread": ("half_spread_pct", "0.07", 0.07), "part": ("participation_pct", "3.5", 3.5),
    "buyfee": ("buy_fee", "0.2", 0.002), "sellfee": ("sell_fee", "0.25", 0.0025), "navage": ("max_nav_age_days", "2", 2),
    "fresh": ("require_fresh", "0", False), "smode": ("session_mode", "fixed", "fixed"),
    "sstart": ("session_start", "10:15", 101500), "send": ("session_end", "17:45", 174500),
}
# DiscountParams fields that are deliberately not exposed in the UI
NOT_EXPOSED = {"index_min_share", "mr_lag"}


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
    bad = []
    for k, (field, _v, exp) in WEB_MAP.items():
        g = got.get(field)
        ok = (abs(g - exp) < 1e-9) if isinstance(exp, float) and isinstance(g, (int, float)) and not isinstance(g, bool) else g == exp
        if not ok:
            bad.append(f"{k}→{field}: ارسال {_v} ← خوانده شد {g!r} (انتظار {exp!r})")
    R.add("سیم‌کشی وب", f"هر ۲۴ پارامترِ فرم به فیلد درست DiscountParams می‌رسد", not bad, "؛ ".join(bad) or "همه درست")
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
    "sstart": "disc-sstart", "send": "disc-send",
}


def _hhmmss(v: str) -> int:
    d = v.replace(":", "")
    return int(d[:-2]) * 10000 + int(d[-2:]) * 100


def test_static_ui(R: Results):
    html = _html()
    dflt = asdict(D.DiscountParams())
    missing = [i for i in UI_ID.values() if _el(html, i) is None]
    R.add("رابط کاربری (ایستا)", "همهٔ کنترل‌های پارامتر در صفحه وجود دارند", not missing, ", ".join(missing) or "کامل")
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
        elif k in ("buyfee", "sellfee"):
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
    # each searched dimension is really a DiscountParams field the server parses
    web_fields = {f for f, _v, _e in WEB_MAP.values()}
    bad = [d for d in S.DIMS if d not in web_fields]
    R.add("رابط کاربری (ایستا)", "هر پارامتر جستجوشدنی یک فیلد وبیِ خوانده‌شده است", not bad, ", ".join(bad) or "درست")


# --------------------------------------------------------------------------- #
#  4) Optimizer                                                                #
# --------------------------------------------------------------------------- #

def test_optimizer(R: Results, db):
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
                ok = any(S._active(d, {"entry_mode": m, "mr_center": c, "stop_loss_pct": 1.0})
                         for m in modes for c in centers)
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
    # (b) real run: reported config == simulated config
    base = _base()
    space = {k: list(S.DIMS[k]["choices"]) for k in S.ORDER}
    st = S.Study(db, None, None, None, None, base, space, n_samples=160, blocks=6, min_trades=8, max_seconds=90,
                 max_universes=8, seed=5)
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
        full = _run(db, p)["summary"]["final_capital"]
        U = st._universe(p)
        import discount_validation as V
        tr = V._sim_all(U, p)
        _a, s, _c = D._portfolio_summary(tr, p, d0, d1)
        if abs(s["final_capital"] - full) > 1:
            bad.append(f"{rec['cfg']}: {s['final_capital']:,.0f} ≠ {full:,.0f}")
    R.add("بهینه‌ساز", "نتیجهٔ ثبت‌شدهٔ هر ترکیب = اجرای مستقیم همان ترکیب در بک‌تست (دو مسیر کدی)", not bad,
          "؛ ".join(bad) or f"{len(sample)} ترکیب تطبیق داده شد")
    b = out["best"]["cfg"]
    act_missing = [d for d in S.ORDER if d in st.space and S._active(d, b) and b.get(d) is None]
    R.add("بهینه‌ساز", "ترکیب برنده همهٔ پارامترهای فعالش را دارد (اعمال نتیجه همه‌چیز را عوض می‌کند)", not act_missing,
          ", ".join(act_missing))
    # (c) determinism
    st2 = S.Study(db, None, None, None, None, base, space, n_samples=160, blocks=6, min_trades=8, max_seconds=90,
                  max_universes=8, seed=5)
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


# --------------------------------------------------------------------------- #
#  5) Invariants over random settings                                          #
# --------------------------------------------------------------------------- #

def _random_params(rng: random.Random) -> D.DiscountParams:
    c = {d: rng.choice(S.DIMS[d]["choices"]) for d in S.ORDER}
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
            if p.max_nav_age_days >= 0:
                pass                                                          # NAV age is checked by the audit above
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
            pg.click("#disc-run"); pg.wait_for_selector("#disc-cards .opt-mc", timeout=90000)
            R.add("مرورگر", "بک‌تست اجرا و نتیجه رسم می‌شود", True, "")
            R.add("مرورگر", "نمودار سرمایهٔ درگیر رسم می‌شود", pg.locator("#disc-exp-chart canvas").count() > 0, "")
            # optimizer: apply result must fill EVERY searched field in the form
            pg.click("#disc-study-cfg summary"); pg.wait_for_selector("#ds-dims tbody tr")
            pg.fill("#ds-n", "60"); pg.fill("#ds-sec", "60"); pg.locator("#ds-n").blur()
            for mode in ("index", "fund"):
                pg.select_option("#disc-entrymode", mode)
                pg.click("text=⚡ سریع"); pg.wait_for_timeout(300)
                on = pg.evaluate("[...document.querySelectorAll('#ds-dims [data-dim]')].filter(c=>c.checked).map(c=>c.dataset.dim)")
                want = {"index": {"index_entry_pct", "index_exit_pct"}, "fund": {"entry_discount_pct", "exit_discount_pct"}}[mode]
                R.add("مرورگر", f"پیش‌تنظیم سریع در حالت «{mode}» پارامترهای همان حالت را تیک می‌زند", want <= set(on), str(on))
            pg.select_option("#disc-entrymode", "fund")
            pg.click("#disc-opt-btn")
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
                form = pg.evaluate("""() => { const o = {}; const m = %s; for (const k in m) { const e = document.getElementById(m[k]); o[k] = e ? e.value : null; } return o; }""" % json.dumps(
                    dict(re.findall(r"(\w+):\s*'([\w-]+)'", re.search(r"const DS_FORM = \{(.*?)\};", _html(), re.S).group(1)))))
                bad = [f"{k}: فرم {form[k]!r} ≠ ترکیب {v!r}" for k, v in cfg.items()
                       if v is not None and k in form and str(form[k]) != str(v) and not (isinstance(v, (int, float)) and float(form[k]) == float(v))]
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
        cases.append(("بهینه‌ساز پارامتر بی‌اثرِ حالت شاخص را جستجو کند", caught(test_optimizer, db)))
    finally:
        S.Study._adapt_space = o
    o = S.Study._complete
    S.Study._complete = lambda self, c: S._normalize(self._fill(c), self.base)
    try:
        cases.append(("ترکیبِ برنده پارامتر فعالِ خالی داشته باشد", caught(test_optimizer, db)))
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
    cases.append(("پیش‌فرض فرم با موتور فرق کند", with_html(lambda h: h.replace(
        'id="disc-buyfee" type="number" step="0.005" value="0.12"', 'id="disc-buyfee" type="number" step="0.005" value="0.145"'))))
    cases.append(("discParams یک کنترل را نخواند", with_html(lambda h: h.replace("navage: v('disc-navage'), ", ""))))
    cases.append(("نگاشت «اعمال» یک پارامتر را نداشته باشد", with_html(lambda h: h.replace(
        "stop_mode: 'disc-stopmode', position_pct: 'disc-pos'};", "position_pct: 'disc-pos'};"))))
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
             ("بهینه‌ساز", lambda: test_optimizer(R, db)),
             ("ناوردایی‌ها", lambda: test_invariants(R, db)),
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
