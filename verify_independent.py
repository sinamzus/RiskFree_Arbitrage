"""Independent verification of a discount (gold-fund bubble) backtest result against the raw data.

Deliberately shares NO code with the backtest engine (discount_backtest, discount_fast, tse_dataset, tse_gold,
database): it opens the SQLite file read-only, decodes the stored raw rows itself and re-derives every number a
trade claims from them. Two implementations written apart agreeing is the evidence; where they disagree, the
report says where.

What is checked (for every trade record of the result):
  * buy price  = average price of walking the real asks at the entry instant for the units bought then
                 (the order book rebuilt from the raw TSE book events, applied in time order);
  * sale price = the same over the bids at the sale instant (never beyond the visible bids, except the final
                 liquidation at the end of the data);
  * NAV at the trade = a NAV-dump row of that fund at / before the instant, not older than the NAV-age limit;
  * entry bubble (average buy price vs NAV) within the entry limit;
  * trade instants inside the day's market window (first .. last TSE trade of any collected fund);
  * participation budget: units traded by the book in a fund per day vs the cap of the day's volume;
  * fees / net P&L per record, the cash path (never negative) and the final capital;
  * minimum hold time; no exit before its entry;
  * an equal-weight buy & hold rebuilt from the TSE trade tapes (cross-check of the benchmark).

Usage: python verify_independent.py data/arbitrage.db results/backtest.json
"""

from __future__ import annotations

import bisect
import collections
import datetime as _dt
import json
import sqlite3
import sys
import zlib

REL_TOL = 3e-5            # relative tolerance on prices (results round prices to 0.01 and notionals to 1 rial)


def _sec(t: int) -> int:
    t = int(t)
    return (t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100


def _day(d: int) -> int:
    d = int(d)
    return _dt.date(d // 10000, d // 100 % 100, d % 100).toordinal()


def _ts(d: int, t: int) -> int:
    return _day(d) * 86400 + _sec(t)


def _walk(levels: list, units: float) -> tuple[float, float, float]:
    """(units filled within the levels, their cost, worst price touched)."""
    left, cost, got, worst = units, 0.0, 0.0, 0.0
    for px, v in levels:
        if left <= 0:
            break
        q = v if v < left else left
        cost += q * px
        got += q
        left -= q
        worst = px
    return got, cost, worst


class _Raw:
    """Read-only access to the raw tables, decoded here (not through the app's storage helpers)."""

    def __init__(self, path: str):
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self._nav: dict[int, tuple] = {}
        self._events: dict[tuple, list] = {}
        self._wins: dict[int, tuple | None] = {}

    def sids_of(self, label: str) -> list[int]:
        rows = self.conn.execute("SELECT symbol_id FROM nav_symbol_map WHERE symbol=?", (label,)).fetchall()
        sids = [int(r[0]) for r in rows]
        with_tse = [s for s in sids if self.conn.execute(
            "SELECT 1 FROM tse_raw WHERE symbol_id=? LIMIT 1", (s,)).fetchone()]
        return with_tse or sids

    def nav_rows(self, sid: int):
        """(timestamps, rows) of the NAV dump: rows = (date, time, nav, nav_date, nav_time, last, vol)."""
        hit = self._nav.get(sid)
        if hit is None:
            rows = self.conn.execute(
                "SELECT date, time, nav, nav_date, nav_time, last, vol FROM nav_intraday WHERE symbol_id=? "
                "ORDER BY date, time", (sid,)).fetchall()
            hit = ([_ts(r[0], r[1]) for r in rows], rows)
            self._nav[sid] = hit
        return hit

    def _blob(self, sid: int, date: int, kind: str) -> list:
        r = self.conn.execute("SELECT data FROM tse_raw WHERE symbol_id=? AND date=? AND kind=?",
                              (sid, date, kind)).fetchone()
        if r is None or not r[0]:
            return []
        return json.loads(zlib.decompress(r[0]).decode("utf-8"))

    def book(self, sid: int, date: int, time: int) -> tuple[list, list]:
        """(bids best first, asks best first) at (date, time): the latest event of each of the 5 levels whose time
        is at or before the instant, events taken in TIME order (ties by event id)."""
        key = (sid, date)
        ev = self._events.get(key)
        if ev is None:
            # book rows: refID, hEven, number, pMeDem, qTitMeDem, zOrdMeDem, pMeOf, qTitMeOf, zOrdMeOf
            ev = sorted(self._blob(sid, date, "book"), key=lambda r: (_sec(r[1]), r[0]))
            self._events[key] = (ev, [_sec(r[1]) for r in ev])
            ev = self._events[key]
        rows, secs = ev
        n = bisect.bisect_right(secs, _sec(time))
        levels: dict[int, tuple] = {}
        for r in rows[:n]:
            if 1 <= int(r[2]) <= 5:
                levels[int(r[2])] = (float(r[3]), float(r[4]), float(r[6]), float(r[7]))
        bids = sorted(([p, v] for p, v, _a, _b in levels.values() if p > 0 and v > 0), key=lambda x: -x[0])
        asks = sorted(([a, b] for _p, _v, a, b in levels.values() if a > 0 and b > 0), key=lambda x: x[0])
        return bids, asks

    def window(self, date: int) -> tuple[int, int] | None:
        """(first, last) second of the day at which any collected fund traded (non-cancelled TSE trades)."""
        if date not in self._wins:
            lo = hi = None
            for (blob,) in self.conn.execute("SELECT data FROM tse_raw WHERE date=? AND kind='trades' AND n>0",
                                              (date,)):
                # trade rows: nTran, hEven, pTran, qTitTran, canceled
                for r in json.loads(zlib.decompress(blob).decode("utf-8")) if blob else []:
                    if r[4] or r[2] <= 0 or r[3] <= 0:
                        continue
                    x = _sec(r[1])
                    lo = x if lo is None or x < lo else lo
                    hi = x if hi is None or x > hi else hi
            self._wins[date] = (lo, hi) if lo is not None else None
        return self._wins[date]

    def closes(self, sid: int) -> dict[int, float]:
        """{date: last non-cancelled TSE trade price of the day}."""
        out = {}
        for d, blob in self.conn.execute(
                "SELECT date, data FROM tse_raw WHERE symbol_id=? AND kind='trades' AND n>0 ORDER BY date", (sid,)):
            rows = [r for r in (json.loads(zlib.decompress(blob).decode("utf-8")) if blob else [])
                    if not r[4] and r[2] > 0 and r[3] > 0]
            if rows:
                out[int(d)] = float(max(rows, key=lambda r: (_sec(r[1]), r[0]))[2])
        return out


class _Check:
    def __init__(self, name: str, applicable: bool = True):
        self.name, self.applicable = name, applicable
        self.checked, self.failed, self.max_dev, self.examples = 0, 0, 0.0, []

    def ok(self, dev: float = 0.0):
        self.checked += 1
        self.max_dev = max(self.max_dev, dev)

    def bad(self, text: str, dev: float = 0.0):
        self.checked += 1
        self.failed += 1
        self.max_dev = max(self.max_dev, dev)
        if len(self.examples) < 6:
            self.examples.append(text)

    def out(self) -> dict:
        st = "n/a" if not self.applicable else ("fail" if self.failed else ("pass" if self.checked else "n/a"))
        return {"name": self.name, "status": st, "checked": self.checked, "failed": self.failed,
                "max_dev": round(self.max_dev, 8), "examples": self.examples}


def verify(db_path: str, res: dict) -> dict:
    p = res.get("params") or {}
    trades = res.get("trades") or []
    raw = _Raw(str(db_path))
    hybrid = p.get("dataset") == "hybrid"
    bf, sf = float(p.get("buy_fee", 0)), float(p.get("sell_fee", 0))
    hold_like = p.get("fill_mode") in ("hold", "best")
    lim = (float(p.get("fill_max_rel_pct", 0)) if hold_like else -float(p.get("entry_discount_pct", 0))) / 100.0
    nav_age = int(p.get("max_nav_age_min", 0)) * 60
    pct = float(p.get("participation_pct", 0))
    notes = []
    if res.get("trades_truncated"):
        notes.append(f"only {len(trades)} of {res['trades_truncated']} trades are in the file: the cash path / final "
                     "capital checks are skipped")

    C = {k: _Check(n, a) for k, n, a in (
        ("buy", "buy price = walking the asks at the entry instant (book rebuilt from raw events)", hybrid),
        ("buy_depth", "units bought ≤ visible asks", hybrid),
        ("sell", "sale price = walking the bids at the sale instant", hybrid),
        ("sell_depth", "units sold ≤ visible bids (except the final liquidation)", hybrid),
        ("nav_in", "NAV at entry = NAV-dump row at that instant, within the age limit", True),
        ("nav_out", "NAV at sale = that of the fund's latest tradable quote, and current", True),
        ("limit", "entry average price within the entry limit (vs NAV)", float(p.get("baseline_days", 0)) == 0),
        ("window", "trade instants inside the market window", hybrid),
        ("budget", "participation budget per fund and day", pct > 0),
        ("arith", "fees and net P&L of every record", True),
        ("order", "no exit before entry; minimum hold respected", True),
        ("cash", "cash never negative; final capital = initial + all records", not res.get("trades_truncated")),
    )}

    sid_of: dict[str, int | None] = {}
    for t in trades:
        if t["symbol"] not in sid_of:
            s = raw.sids_of(t["symbol"])
            sid_of[t["symbol"]] = s[0] if s else None
            if not s:
                notes.append(f"fund {t['symbol']}: not found in nav_symbol_map")

    def units(t, side):
        return (t["buy_notional"] / t["entry_price"]) if side == "in" else (t["sell_notional"] / t["exit_price"])

    # ---- entries (one buy per fund and instant: lots sold in pieces share it) ----
    ent = collections.defaultdict(list)
    for t in trades:
        ent[(t["symbol"], t["entry_date"], t["entry_time"])].append(t)
    for (sym, d, tm), ts in ent.items():
        sid = sid_of.get(sym)
        if sid is None:
            continue
        u = sum(units(t, "in") for t in ts)
        px = sum(t["buy_notional"] for t in ts) / u if u > 0 else ts[0]["entry_price"]
        tsx, rows = raw.nav_rows(sid)
        T = _ts(d, tm)
        j = bisect.bisect_right(tsx, T) - 1
        r = rows[j] if j >= 0 else None
        if r is None or _ts(r[0], r[1]) != T:
            C["nav_in"].bad(f"{sym} {d} {tm}: no NAV-dump row at the entry instant")
        else:
            nav = float(r[2])
            dev = max(abs(nav - t["nav_entry"]) / nav for t in ts) if nav else 1.0
            age = (T - _ts(r[3], r[4])) if (r[3] and r[4]) else None
            if dev > REL_TOL:
                C["nav_in"].bad(f"{sym} {d} {tm}: NAV {ts[0]['nav_entry']} ≠ dump {nav}", dev)
            elif nav_age and (age is None or age > nav_age or age < -60):
                C["nav_in"].bad(f"{sym} {d} {tm}: NAV age {age}s (limit {nav_age}s)")
            else:
                C["nav_in"].ok(dev)
            if C["limit"].applicable and nav:
                rel = px / nav - 1.0
                # without a real book the fill is last·(1 + assumed half spread) while the signal is on last
                allow = lim + (0.0 if hybrid else float(p.get("half_spread_pct", 0)) / 100.0)
                if rel > allow + 2e-5:
                    C["limit"].bad(f"{sym} {d} {tm}: average buy {rel * 100:.4f}% vs limit {allow * 100:.4f}%",
                                   rel - allow)
                else:
                    C["limit"].ok()
        if hybrid:
            w = raw.window(d)
            if w is None or not (w[0] <= _sec(tm) <= w[1]):
                C["window"].bad(f"{sym} {d} {tm}: outside the window {w}")
            else:
                C["window"].ok()
            bids, asks = raw.book(sid, d, tm)
            depth = sum(v for _p, v in asks)
            if u > depth * (1 + 1e-9) + 1:
                C["buy_depth"].bad(f"{sym} {d} {tm}: bought {u:,.0f} > asks {depth:,.0f}")
            else:
                C["buy_depth"].ok()
            got, cost, _w = _walk(asks, u)
            if got <= 0:
                C["buy"].bad(f"{sym} {d} {tm}: no asks in the rebuilt book")
            else:
                exp = cost / got
                dev = abs(exp - px) / exp
                if dev > REL_TOL:
                    C["buy"].bad(f"{sym} {d} {tm}: price {px} ≠ walking the asks {exp:.2f} ({u:,.0f} units)", dev)
                else:
                    C["buy"].ok(dev)

    # ---- sales (all records sold at one instant together: they met the bids one after the other) ----
    ext = collections.defaultdict(list)
    for t in trades:
        ext[(t["symbol"], t["exit_date"], t["exit_time"], t["exit_reason"] == "end")].append(t)
    for (sym, d, tm, is_end), ts in ext.items():
        sid = sid_of.get(sym)
        if sid is None:
            continue
        u = sum(units(t, "out") for t in ts)
        proceeds = sum(t["sell_notional"] for t in ts)
        tsx, rows = raw.nav_rows(sid)
        T = _ts(d, tm)
        j = bisect.bisect_right(tsx, T) - 1

        def stale(r):
            if nav_age:
                return not (r[3] and r[4]) or T - _ts(r[3], r[4]) > nav_age or _ts(r[3], r[4]) > _ts(r[0], r[1]) + 60
            return bool(r[3]) and _day(r[0]) - _day(r[3]) > int(p.get("max_nav_age_days", 2))

        def tradable(r):
            if not (r[2] and r[2] > 0 and r[5] and r[5] > 0):
                return False
            if not hybrid:
                return True
            w = raw.window(r[0])
            if w is None or not (w[0] <= _sec(r[1]) <= w[1]):
                return False
            b_, a_ = raw.book(sid, r[0], r[1])
            return bool(b_ and a_ and b_[0][0] < a_[0][0])
        # the quote a sale is made on: the fund's latest TRADABLE dump row at / before the instant (in the window,
        # two-sided book). It must carry a current NAV — a later stale one forbids the sale — except for the
        # final liquidation, which uses the last row with a current NAV
        found, why = None, ""
        while j >= 0 and T - tsx[j] <= 3 * 86400:
            r = rows[j]
            j -= 1
            if not tradable(r):
                continue
            if stale(r):
                if is_end:
                    continue
                why = f"its latest quote ({r[0]} {r[1]}) carries a stale NAV"
                break
            found = r
            break
        if found is None:
            C["nav_out"].bad(f"{sym} {d} {tm}: sold although {why or 'no current quote'}")
        else:
            dev = max(abs(float(found[2]) - t["nav_exit"]) / float(found[2]) for t in ts)
            if dev > REL_TOL:
                C["nav_out"].bad(f"{sym} {d} {tm}: NAV {[t['nav_exit'] for t in ts]} ≠ the latest quote's "
                                 f"{found[2]} ({found[0]} {found[1]})", dev)
            else:
                C["nav_out"].ok(dev)
        if hybrid:
            w = raw.window(d)
            if w is None or not (w[0] <= _sec(tm) <= w[1]):
                C["window"].bad(f"{sym} {d} {tm} (sale): outside the window {w}")
            else:
                C["window"].ok()
            bids, _asks = raw.book(sid, d, tm)
            depth = sum(v for _p, v in bids)
            if not is_end:
                if u > depth * (1 + 1e-9) + 1:
                    C["sell_depth"].bad(f"{sym} {d} {tm}: sold {u:,.0f} > bids {depth:,.0f}")
                else:
                    C["sell_depth"].ok()
            got, cost, worst = _walk(bids, u)
            if got <= 0:
                C["sell"].bad(f"{sym} {d} {tm}: no bids in the rebuilt book")
                continue
            exp = cost + (u - got) * worst                 # beyond the depth at the worst bid (final liquidation)
            dev = abs(exp - proceeds) / exp
            if dev > REL_TOL:
                C["sell"].bad(f"{sym} {d} {tm}: proceeds {proceeds:,.0f} ≠ walking the bids {exp:,.0f}", dev)
            else:
                C["sell"].ok(dev)

    # ---- participation budget per fund and day (buys checked; sales use it up) ----
    if pct > 0:
        evs = collections.defaultdict(list)
        for t in trades:
            evs[(t["symbol"], t["entry_date"])].append((_sec(t["entry_time"]), 1, units(t, "in")))
            evs[(t["symbol"], t["exit_date"])].append((_sec(t["exit_time"]), 0, units(t, "out")))
        for (sym, d), lst in evs.items():
            sid = sid_of.get(sym)
            if sid is None:
                continue
            tsx, rows = raw.nav_rows(sid)
            day = [r for r in rows if r[0] == d]
            used = 0.0
            for s, kind, u in sorted(lst):                     # a sale at an instant comes before the buy it pays for
                if kind == 1:
                    if p.get("participation_basis") == "sofar":
                        cum = max([float(r[6] or 0) for r in day if _sec(r[1]) <= s] or [0.0])
                    else:
                        cum = max([float(r[6] or 0) for r in day] or [0.0])
                    cap = cum * pct / 100.0
                    if used + u > cap * (1 + 1e-6) + 1:
                        C["budget"].bad(f"{sym} {d}: {used + u:,.0f} units by {s // 3600:02d}:{s // 60 % 60:02d} "
                                        f"> cap {cap:,.0f}")
                    else:
                        C["budget"].ok()
                used += u

    # ---- arithmetic, order, cash path ----
    mh = int(p.get("fill_min_hold_min", 0)) * 60
    for t in trades:
        fees = t["buy_notional"] * bf + t["sell_notional"] * sf
        net = t["sell_notional"] * (1 - sf) - t["buy_notional"] * (1 + bf)
        if abs(fees - t["fees"]) > 2 or abs(net - t["net_pnl"]) > 2:
            C["arith"].bad(f"{t['symbol']} {t['entry_date']}: fees {t['fees']} / net {t['net_pnl']} ≠ {fees:.0f} / {net:.0f}")
        else:
            C["arith"].ok()
        a, b = _ts(t["entry_date"], t["entry_time"]), _ts(t["exit_date"], t["exit_time"])
        if b < a or (hold_like and mh and t["exit_reason"] == "rotate" and b - a < mh):
            C["order"].bad(f"{t['symbol']} {t['entry_date']} {t['entry_time']} → {t['exit_date']} {t['exit_time']}")
        else:
            C["order"].ok()
    if C["cash"].applicable:
        ev = []
        for t in trades:
            ev.append((_ts(t["entry_date"], t["entry_time"]), 1, -t["buy_notional"] * (1 + bf)))
            ev.append((_ts(t["exit_date"], t["exit_time"]), 0, t["sell_notional"] * (1 - sf)))
        cash = float(p.get("initial_capital", 0))
        low = cash
        for _t, _k, v in sorted(ev):
            cash += v
            low = min(low, cash)
        fin = (res.get("summary") or {}).get("final_capital")
        if low < -max(1.0, 1e-9 * cash):
            C["cash"].bad(f"cash goes down to {low:,.0f}")
        elif fin is not None and abs(cash - fin) > max(5.0, 1e-9 * fin):
            C["cash"].bad(f"final capital {fin:,.0f} ≠ initial + records {cash:,.0f}", abs(cash - fin))
        else:
            C["cash"].ok()

    # ---- buy & hold rebuilt from the TSE trade tapes (cross-check, not a pass/fail) ----
    bh = None
    try:
        d0, d1 = (res.get("period") or [None, None])
        funds = [s for s in (raw.sids_of(lab) for lab in (res.get("fund_labels") or [])) if s]
        if not funds:
            funds = [[int(r[0])] for r in raw.conn.execute("SELECT DISTINCT symbol_id FROM tse_raw")]
        series = {s[0]: {d: v for d, v in raw.closes(s[0]).items() if d0 <= d <= d1} for s in funds}
        series = {k: v for k, v in series.items() if v}
        days = sorted({d for v in series.values() for d in v})
        if days:
            first_week = set(days[:5])
            start = {k: v for k, v in series.items() if min(v) in first_week}
            cap0 = float(p.get("initial_capital", 0))
            per = cap0 / (1 + bf) / len(start)
            fin = 0.0
            for k, v in start.items():
                u = per / v[min(v)]
                fin += u * v[max(v)] * (1 - sf)
            bh = {"funds": len(start), "return_pct": round((fin / cap0 - 1) * 100, 3),
                  "reported_pct": ((res.get("benchmark") or {}).get("buyhold") or {}).get("return_pct"),
                  "note": "from each fund's last TSE trade of the day (the backtest uses the NAV dump's last price "
                          "inside the session): a few points of difference are expected; tens are not"}
    except Exception as e:                                   # pragma: no cover
        notes.append(f"buy & hold cross-check failed: {e}")

    checks = [c.out() for c in C.values()]
    ok = all(c["status"] != "fail" for c in checks)
    return {"ok": ok, "checks": checks, "buyhold": bh, "notes": notes, "trades": len(trades),
            "dataset": p.get("dataset"), "period": res.get("period")}


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    with open(argv[2], encoding="utf-8") as f:
        res = json.load(f)
    rep = verify(argv[1], res)
    for c in rep["checks"]:
        print(f"[{c['status']:4}] {c['name']}: {c['checked']} checked, {c['failed']} failed"
              + (f", max deviation {c['max_dev']}" if c["max_dev"] else ""))
        for e in c["examples"]:
            print("        ", e)
    if rep["buyhold"]:
        print("buy & hold:", rep["buyhold"])
    for n in rep["notes"]:
        print("note:", n)
    print("RESULT:", "OK" if rep["ok"] else "DIFFERENCES FOUND")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
