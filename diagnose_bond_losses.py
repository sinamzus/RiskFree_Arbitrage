#!/usr/bin/env python3
"""Diagnose WHY اخزا z-spread positions close at a loss.

Two modes
---------
  python diagnose_bond_losses.py            # run on the REAL local DB (data/arbitrage.db)
  python diagnose_bond_losses.py --demo     # synthetic proof: run the real engine on a
                                            #   hand-built order book to isolate the
                                            #   execution logic from market movement

Real-DB mode prints a per-trade loss attribution: for every trade it compares
the z-spread the SIGNAL saw (best touch) against the z-spread actually REALISED
by the average fill, so you can see how much edge the ladder sweep ate, and it
breaks the P&L down by exit_reason (signal / eod / final).
"""
from __future__ import annotations
import sys

from bond_backtest import run_bond_backtest, BondBacktestParams
from bonds import ytm_zero_coupon, days_to_maturity


# ── UI defaults (match static/index.html) ───────────────────────────────────
def default_params() -> BondBacktestParams:
    return BondBacktestParams(
        entry_bps=50.0, exit_bps=10.0, degree=2, min_curve_points=3,
        step_secs=0, force_eod=False, include_matured=True,
        signal_price="exec", min_exit_profit_bps=-1.0,
        total_capital=10_000_000_000.0, max_position_pct=0.5,
    )


def _fill_entry_z(t: dict, meta: dict) -> float | None:
    """z-spread of the AVERAGE entry FILL price (not the best-touch the signal saw)."""
    m = meta.get(t["symbol"])
    if not m or not m.get("maturity_date"):
        return None
    dtm = days_to_maturity(m["maturity_date"], t["date"])
    if dtm <= 0 or not t.get("entry_price"):
        return None
    y_fill = ytm_zero_coupon(t["entry_price"], m["face_value"], dtm)
    if y_fill <= 0:
        return None
    return (y_fill - t["entry_curve_ytm"]) * 10_000.0


def diagnose_real() -> int:
    from database import Database
    db = Database()
    p = default_params()
    res = run_bond_backtest(db, params=p)
    trades = res["trades"]
    s = res["summary"]
    cap = res.get("capital", {})

    print("=" * 72)
    print("اخزا Z-SPREAD BACKTEST — LOSS DIAGNOSIS  (signal_price=exec)")
    print("=" * 72)
    print(f"guards              : entry_max={p.entry_max_bps:g}bps  "
          f"curve_trim={p.curve_trim_bps:g}bps  (0=off)")
    print(f"days tested/skipped : {res['days_tested']}/{res['days_skipped']}")
    print(f"symbols             : {len(res['symbols'])}  {res['symbols']}")
    print(f"trades              : {s['trade_count']}   win-rate {s['win_rate']}%")
    print(f"net P&L             : {s['total_net_pnl']:,.0f} Rials")
    print(f"total fees          : {s['total_fees']:,.0f} Rials")
    print(f"capital change      : {cap.get('capital_change',0):,.0f} "
          f"({cap.get('capital_change_pct',0)}%)  MWRR {cap.get('mwrr_annual_pct',0)}%/yr")
    if not trades:
        print("\nNO TRADES — the DB has no overlapping اخزا order-book history.")
        print("Collect intraday order books first (📊 وضعیت داده / collector).")
        return 0

    # Rebuild symbol meta to recompute fill yields.
    from bond_backtest import _resolve_universe
    _, meta = _resolve_universe(db, None, include_matured=p.include_matured)

    # ── Breakdown by exit_reason ────────────────────────────────────────────
    print("\n" + "-" * 72)
    print("P&L BY EXIT REASON  (where the losses actually come from)")
    print("-" * 72)
    print(f"{'reason':10s} {'#':>4s} {'#loss':>6s} {'net P&L':>16s} {'loss only':>16s}")
    by = {}
    for t in trades:
        r = t["exit_reason"]
        d = by.setdefault(r, {"n": 0, "loss_n": 0, "net": 0.0, "loss": 0.0})
        d["n"] += 1
        d["net"] += t["net_pnl"]
        if t["net_pnl"] < 0:
            d["loss_n"] += 1
            d["loss"] += t["net_pnl"]
    for r, d in sorted(by.items()):
        print(f"{r:10s} {d['n']:>4d} {d['loss_n']:>6d} "
              f"{d['net']:>16,.0f} {d['loss']:>16,.0f}")

    # ── Edge slippage: signal-z vs realised fill-z on ENTRY ─────────────────
    gaps = []
    for t in trades:
        fz = _fill_entry_z(t, meta)
        if fz is not None:
            gaps.append(t["entry_z_bps"] - fz)   # +ve = fill worse than signal
    if gaps:
        print("\n" + "-" * 72)
        print("ENTRY EDGE SLIPPAGE  (signal best-ask z  −  average-fill z, bps)")
        print("-" * 72)
        avg = sum(gaps) / len(gaps)
        print(f"mean {avg:+.1f} bps   max {max(gaps):+.1f}   min {min(gaps):+.1f}")
        print("  >0 means the average fill was RICHER than the touch the signal "
              "checked\n  → the ladder sweep ate this much of the entry edge.")

    # ── Worst losers, fully attributed ──────────────────────────────────────
    losers = sorted([t for t in trades if t["net_pnl"] < 0],
                    key=lambda x: x["net_pnl"])
    print("\n" + "-" * 72)
    print(f"WORST {min(15, len(losers))} LOSING TRADES")
    print("-" * 72)
    hdr = (f"{'sym':8s} {'in-date':>9s} {'out-date':>9s} {'reason':7s} "
           f"{'sigZ':>6s} {'fillZ':>6s} {'in-px':>10s} {'out-px':>10s} "
           f"{'net%':>7s} {'net P&L':>14s}")
    print(hdr)
    for t in losers[:15]:
        fz = _fill_entry_z(t, meta)
        print(f"{t['symbol']:8s} {t['date']:>9d} {t['exit_date']:>9d} "
              f"{t['exit_reason']:7s} {t['entry_z_bps']:>6.0f} "
              f"{(f'{fz:.0f}' if fz is not None else '?'):>6s} "
              f"{t['entry_price']:>10,.0f} {t['exit_price']:>10,.0f} "
              f"{t['net_pct']:>7.3f} {t['net_pnl']:>14,.0f}")

    # ── P&L by ENTRY z-spread bucket ────────────────────────────────────────
    # If the catastrophic losses concentrate in the extreme-z buckets, the
    # cause is GARBAGE SIGNALS (bad data / curve misfit), not the strategy.
    print("\n" + "-" * 72)
    print("P&L BY ENTRY Z-SPREAD BUCKET  (is the strategy trading garbage outliers?)")
    print("-" * 72)
    buckets = [(-1e9, 0), (0, 50), (50, 100), (100, 200), (200, 500), (500, 1e9)]
    blab = ["z<0", "0–50", "50–100", "100–200", "200–500", "500+"]
    print(f"{'z bucket':>10s} {'#':>5s} {'win%':>6s} {'net P&L':>18s} {'avg/trade':>14s}")
    for (lo, hi), lab in zip(buckets, blab):
        g = [t for t in trades if lo <= t["entry_z_bps"] < hi]
        if not g:
            continue
        gnet = sum(t["net_pnl"] for t in g)
        gwin = sum(1 for t in g if t["net_pnl"] > 0)
        print(f"{lab:>10s} {len(g):>5d} {100*gwin/len(g):>5.0f}% "
              f"{gnet:>18,.0f} {gnet/len(g):>14,.0f}")

    # ── P&L by SYMBOL (worst offenders) ─────────────────────────────────────
    print("\n" + "-" * 72)
    print("WORST 12 SYMBOLS BY NET P&L  (is the loss concentrated in a few bad series?)")
    print("-" * 72)
    bysym = {}
    for t in trades:
        d = bysym.setdefault(t["symbol"], {"n": 0, "net": 0.0, "win": 0})
        d["n"] += 1
        d["net"] += t["net_pnl"]
        d["win"] += 1 if t["net_pnl"] > 0 else 0
    print(f"{'symbol':10s} {'#':>5s} {'win%':>6s} {'net P&L':>18s}")
    for sym, d in sorted(bysym.items(), key=lambda kv: kv[1]["net"])[:12]:
        print(f"{sym:10s} {d['n']:>5d} {100*d['win']/d['n']:>5.0f}% {d['net']:>18,.0f}")

    # ── Intraday vs overnight (curve / duration risk) ───────────────────────
    print("\n" + "-" * 72)
    print("INTRADAY vs OVERNIGHT  (does holding overnight bleed to curve shifts?)")
    print("-" * 72)
    intra = [t for t in trades if t["exit_date"] == t["date"]]
    over = [t for t in trades if t["exit_date"] != t["date"]]
    for lab, g in [("intraday", intra), ("overnight", over)]:
        if not g:
            continue
        gnet = sum(t["net_pnl"] for t in g)
        gwin = sum(1 for t in g if t["net_pnl"] > 0)
        print(f"{lab:10s} {len(g):>5d} trades  win {100*gwin/len(g):>4.0f}%  "
              f"net {gnet:>18,.0f}  avg {gnet/len(g):>12,.0f}")

    # ── WHAT-IF filters (approximate: ignores capital reallocation) ─────────
    # Recompute total net P&L if we had REJECTED implausible/extreme trades.
    print("\n" + "-" * 72)
    print("WHAT-IF  (approx total net if these trades had NOT been taken)")
    print("-" * 72)
    base = sum(t["net_pnl"] for t in trades)
    print(f"{'as-is (all trades)':40s} {base:>18,.0f}")
    for cap in (300, 200, 150, 100):
        kept = [t for t in trades if t["entry_z_bps"] <= cap]
        print(f"{'only entry z ≤ ' + str(cap) + ' bps':40s} "
              f"{sum(t['net_pnl'] for t in kept):>18,.0f}  "
              f"({len(kept)} trades)")
    print(f"{'intraday only (no overnight holds)':40s} "
          f"{sum(t['net_pnl'] for t in intra):>18,.0f}  ({len(intra)} trades)")
    combo = [t for t in trades if t["entry_z_bps"] <= 150 and t["exit_date"] == t["date"]]
    print(f"{'intraday AND entry z ≤ 150 bps':40s} "
          f"{sum(t['net_pnl'] for t in combo):>18,.0f}  ({len(combo)} trades)")

    _data_sanity(db, p)
    return 0


def _data_sanity(db, p) -> None:
    """Per-series curve bias: a healthy اخزا oscillates around the curve (mean z≈0).

    A series with a large PERSISTENT |mean z| sits permanently off the fitted
    curve — the signature of a wrong maturity_date / face_value (so its dtm and
    therefore its YTM are systematically biased). Those series throw off endless
    one-sided 'cheap' (or 'rich') signals that never revert → guaranteed losses.
    This recomputes every tick's z-spread (exactly as the engine does) and
    aggregates it per symbol so the broken inputs can be named and fixed.
    """
    from bond_backtest import (_resolve_universe, _load_day_cache,
                               _build_decision_stream)
    from bonds import days_to_maturity
    universe, meta = _resolve_universe(db, None, include_matured=p.include_matured)
    dates, cache = _load_day_cache(db, universe, meta, None, None)
    stream = _build_decision_stream(dates, cache, p)

    acc: dict = {}   # sym -> [n, sum_z, sum_z2]
    for day in stream["days"]:
        if day.get("skipped"):
            continue
        for _t, items in day["events"]:
            for it in items:
                sym, z = it[0], it[1]
                d = acc.setdefault(sym, [0, 0.0, 0.0])
                d[0] += 1; d[1] += z; d[2] += z * z

    rows = []
    last = dates[-1] if dates else 0
    for sym, (n, sz, sz2) in acc.items():
        if n == 0:
            continue
        mean = sz / n
        var = max(sz2 / n - mean * mean, 0.0)
        std = var ** 0.5
        mat = meta.get(sym, {}).get("maturity_date", 0)
        dtm = days_to_maturity(mat, last) if mat else 0
        rows.append((sym, n, mean, std, mat, dtm))

    rows.sort(key=lambda r: abs(r[2]), reverse=True)
    print("\n" + "-" * 72)
    print("DATA SANITY — per-series curve bias  (|mean z| large ⇒ off-curve / bad data)")
    print("-" * 72)
    print(f"{'symbol':10s} {'ticks':>7s} {'mean z':>9s} {'std z':>8s} "
          f"{'maturity':>10s} {'dtm':>6s}  flag")
    for sym, n, mean, std, mat, dtm in rows[:20]:
        flag = ""
        if abs(mean) > 150:
            flag = "<<< BROKEN (persistent off-curve — check maturity/face_value)"
        elif abs(mean) > 60:
            flag = "<< suspect"
        bad_dtm = "  ⚠dtm≤0" if dtm <= 0 else ""
        print(f"{sym:10s} {n:>7d} {mean:>9.1f} {std:>8.1f} "
              f"{mat:>10d} {dtm:>6d}{bad_dtm}  {flag}")
    print("\nHealthy series have mean z within ±~30 bps. Anything persistently")
    print("> ±150 bps is almost certainly a registry data error, not a real edge.")


# ─────────────────────────────────────────────────────────────────────────────
#  Synthetic proof — run the REAL engine on a hand-built order book
# ─────────────────────────────────────────────────────────────────────────────
def _ob_row(sym, date, time, bids, asks):
    """bids/asks = list of (price, vol) best-first. Pads to 5 levels."""
    row = {"symbol": sym, "ins_code": "", "date": date, "time": time,
           "spread_pct": 0.0, "bid_depth": 0, "ask_depth": 0, "nav": 0.0}
    for i in range(5):
        bp, bv = bids[i] if i < len(bids) else (0, 0)
        ap, av = asks[i] if i < len(asks) else (0, 0)
        row[f"bid{i+1}_price"], row[f"bid{i+1}_vol"], row[f"bid{i+1}_cnt"] = bp, bv, 1
        row[f"ask{i+1}_price"], row[f"ask{i+1}_vol"], row[f"ask{i+1}_cnt"] = ap, av, 1
    return row


def _px(ytm, fv, dtm):
    from bonds import price_zero_coupon
    return round(price_zero_coupon(ytm, fv, dtm))


def diagnose_demo() -> int:
    import tempfile, os, sqlite3
    from pathlib import Path

    FV = 1_000_000
    DATE = 20260101
    CURVE = 0.30            # flat 30% curve, CONSTANT all day (no market move)

    # 3 anchor bonds sit EXACTLY on the flat curve with zero spread — they pin
    # the curve and never trade. dtm 100/250/400.
    anchors = [("ANC1", 100), ("ANC2", 250), ("ANC3", 400)]
    # Target bond, dtm 200. Build an ASYMMETRIC ladder:
    #   • best ASK is cheap  (yield 30.7% > curve+50bps) → entry signal fires,
    #     but only 50 units there; deeper asks are RICH (yield < curve).
    #   • best BID is high   (yield 30.0% ≤ curve+10bps) → exit signal fires,
    #     but only 50 units there; deeper bids are CHEAP.
    TGT, TDTM = "TARGET", 200

    def ask_ladder(touch):   # cheap touch over an expensive deep book
        return [(_px(touch, FV, TDTM), 50),
                (_px(0.300, FV, TDTM), 5000),
                (_px(0.293, FV, TDTM), 5000),
                (_px(0.286, FV, TDTM), 5000),
                (_px(0.279, FV, TDTM), 5000)]

    def bid_ladder(touch):   # high touch over a cheap deep book
        return [(_px(touch, FV, TDTM), 50),
                (_px(0.310, FV, TDTM), 5000),
                (_px(0.320, FV, TDTM), 5000),
                (_px(0.330, FV, TDTM), 5000),
                (_px(0.340, FV, TDTM), 5000)]

    rows = []
    # Anchors are identical & constant at both times (curve never moves). The
    # TARGET book differs between the two snapshots ONLY at the touch, so the
    # engine actually re-evaluates and fires a genuine SIGNAL exit (not a forced
    # end-of-test close): t=100000 → entry fires; t=110000 → exit fires.
    #   entry snap: best ASK cheap (30.7%) → BUY ; best BID neutral
    #   exit  snap: best BID reverted (29.9%) → SELL
    snaps = {
        100000: (bid_ladder(0.299), ask_ladder(0.307)),
        110000: (bid_ladder(0.300), ask_ladder(0.305)),
    }
    for tm in (100000, 110000):
        for sym, dtm in anchors:
            pa = _px(CURVE, FV, dtm)
            rows.append(_ob_row(sym, DATE, tm, [(pa, 100)], [(pa, 100)]))
        b, a = snaps[tm]
        rows.append(_ob_row(TGT, DATE, tm, b, a))

    tmp = tempfile.mkdtemp()
    dbpath = Path(tmp) / "demo.db"
    conn = sqlite3.connect(dbpath)
    conn.execute("""CREATE TABLE bond_series(symbol TEXT, ins_code TEXT, name TEXT,
        face_value REAL, maturity_date INTEGER, issue_date INTEGER,
        coupon_rate REAL, active INTEGER, verified INTEGER)""")
    coldefs = ["id INTEGER PRIMARY KEY AUTOINCREMENT", "symbol TEXT", "ins_code TEXT",
               "date INTEGER", "time INTEGER"]
    for i in range(1, 6):
        coldefs += [f"bid{i}_price REAL", f"bid{i}_vol INTEGER", f"bid{i}_cnt INTEGER"]
    for i in range(1, 6):
        coldefs += [f"ask{i}_price REAL", f"ask{i}_vol INTEGER", f"ask{i}_cnt INTEGER"]
    coldefs += ["spread_pct REAL", "bid_depth INTEGER", "ask_depth INTEGER", "nav REAL"]
    conn.execute(f"CREATE TABLE intraday_orderbook({','.join(coldefs)})")

    # maturity_date = DATE + dtm (approx via date math)
    from datetime import date as _d, timedelta
    def mat_of(dtm):
        d = _d(DATE // 10000, (DATE % 10000) // 100, DATE % 100) + timedelta(days=dtm)
        return d.year * 10000 + d.month * 100 + d.day

    for sym, dtm in anchors + [(TGT, TDTM)]:
        conn.execute("INSERT INTO bond_series VALUES(?,?,?,?,?,?,?,?,?)",
                     (sym, "", sym, FV, mat_of(dtm), 0, 0.0, 1, 1))
    keys = [c.split()[0] for c in coldefs if not c.startswith("id ")]
    ph = ",".join("?" * len(keys))
    for r in rows:
        conn.execute(f"INSERT INTO intraday_orderbook({','.join(keys)}) VALUES({ph})",
                     [r[k] for k in keys])
    conn.commit()
    conn.close()

    # Patch _today_int so include_matured doesn't drop our future-dated bonds.
    import bonds
    bonds._today_int = lambda: DATE

    class _DB:
        def __init__(self, path):
            import sqlite3 as s
            self.path = path
        def _q(self, sql, args=()):
            import sqlite3 as s
            c = s.connect(self.path); c.row_factory = s.Row
            out = [dict(r) for r in c.execute(sql, args).fetchall()]; c.close()
            return out
        def get_bond_series(self, active_only=True):
            return self._q("SELECT * FROM bond_series")
        def get_ob_dates(self, symbol):
            return [r["date"] for r in self._q(
                "SELECT DISTINCT date FROM intraday_orderbook WHERE symbol=? ORDER BY date", (symbol,))]
        def get_orderbook_history(self, symbol, date_int, limit=20000):
            return self._q("SELECT * FROM intraday_orderbook WHERE symbol=? AND date=? ORDER BY time", (symbol, date_int))

    db = _DB(str(dbpath))
    p = BondBacktestParams(entry_bps=50.0, exit_bps=10.0, degree=2, min_curve_points=3,
                           force_eod=False, signal_price="exec",
                           total_capital=10_000_000_000.0, max_position_pct=0.5)

    print("=" * 72)
    print("SYNTHETIC PROOF — flat 30% curve, ZERO market movement between entry & exit")
    print("=" * 72)
    print(f"Target dtm={TDTM}, FV={FV:,}")
    print(f"  ENTRY snap: best ASK 30.7% (cheap,50u) over deep asks 30.0/29.3/28.6/27.9% (RICH,5000u)")
    print(f"  EXIT  snap: best BID 30.0% (50u) over deep bids 31/32/33/34% (CHEAP,5000u) → exit fires")
    print()

    res = run_bond_backtest(db, params=p)
    for t in res["trades"]:
        print(f"TRADE {t['symbol']}: reason={t['exit_reason']}")
        print(f"  signal entry z = {t['entry_z_bps']:.0f} bps (best ASK)")
        print(f"  signal exit  z = {t['exit_z_bps']:.0f} bps (best BID)")
        print(f"  AVG entry fill price = {t['entry_price']:,.0f}  "
              f"(best ask was {_px(0.307,FV,TDTM):,})")
        print(f"  AVG exit  fill price = {t['exit_price']:,.0f}  "
              f"(best bid was {_px(0.300,FV,TDTM):,})")
        print(f"  volume = {t['volume']:,}   fees = {t['fees']:,.0f}")
        print(f"  NET P&L = {t['net_pnl']:,.0f} Rials ({t['net_pct']:.3f}%)")
    s = res["summary"]
    print(f"\nSUMMARY: {s['trade_count']} trades, net {s['total_net_pnl']:,.0f} Rials, "
          f"win-rate {s['win_rate']}%")
    print("\nINTERPRETATION")
    print("-" * 72)
    if s["total_net_pnl"] >= 0:
        print("FIXED: fills are now capped at the edge price (yield == curve ± threshold).")
        print("The engine lifts ONLY the 50 genuinely-cheap units at the best ask and sells")
        print("ONLY into the rich best bid — it no longer sweeps the rich deep asks / cheap")
        print("deep bids.  Position size now tracks real liquidity, and on a flat, unmoving")
        print("curve the round-trip is a small profit, exactly as it should be.")
    else:
        print("BUG PRESENT: the signal said 'cheap on the touch' and fired, but the engine")
        print("swept the ENTIRE ask ladder (ceiling = asks[-1]) filling rich deep levels, and")
        print("on exit swept the ENTIRE bid ladder (floor = bids[-1]) dumping into cheap deep")
        print("bids.  On a flat, unmoving curve the trade should be ~break-even minus fees;")
        print("instead it is a large loss — pure execution flaw.")
    return 0


if __name__ == "__main__":
    if "--demo" in sys.argv:
        sys.exit(diagnose_demo())
    sys.exit(diagnose_real())
