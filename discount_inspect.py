"""What the market looked like at a backtest trade's instant: the real order book of the traded fund and, for every
selected fund, its NAV / last price / best bid & ask and the resulting bubbles — i.e. the signal the strategy saw.

Used by the backtest's trades table (hover a buy or sell price). The order book comes from the collected TSE data
(``tse_dataset``: the precomputed book at every snapshot instant, else a replay of that day's events); without TSE
data, from the app's own order-book snapshots (``orderbook_history``) when there are any.
"""

from __future__ import annotations

import bisect


def _sec(t: int) -> int:
    t = int(t)
    return (t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100


def _book_at(db, sid: int, date: int, time: int):
    """(bids, asks) of fund ``sid`` at (date, time) from the TSE data, or None."""
    import tse_dataset as TD
    import tse_gold as G
    try:
        TD.ensure_schema(db)
        st = (TD._hybrid_day_states(db, sid, date) or {}).get(int(time))
        if st is None:
            ev = G.load_day(db, sid, date, "book")
            if not ev:
                return None
            st = TD._book_states(ev, [_sec(time)])[0]
        bids, asks = st
        return ([list(x) for x in bids], [list(x) for x in asks]) if (bids or asks) else None
    except Exception:
        return None


def _book_from_snapshots(db, label: str, date: int, time: int):
    """Fallback: the app's own order-book snapshots (latest at or before ``time``)."""
    try:
        rows = db.get_orderbook_history(label, date, 20000)
    except Exception:
        return None
    best = None
    for r in rows:
        if int(r.get("time") or 0) <= int(time):
            best = r
        else:
            break
    if best is None:
        return None
    bids = [[best[f"bid{i}_price"], best[f"bid{i}_vol"]] for i in range(1, 6) if (best.get(f"bid{i}_price") or 0) > 0]
    asks = [[best[f"ask{i}_price"], best[f"ask{i}_vol"]] for i in range(1, 6) if (best.get(f"ask{i}_price") or 0) > 0]
    return (bids, asks) if (bids or asks) else None


def trade_moment(db, cats: list[str] | None, symbols: list[str] | None, focus: str, date: int, time: int,
                 baseline_pct: float = 0.0) -> dict:
    import discount_backtest as D
    funds = D._universe(db, cats, None) if cats else []
    if symbols:
        have = {lab for _s, lab in funds}
        funds += [f for f in D._universe(db, None, symbols) if f[1] not in have]
    if focus and all(lab != focus for _s, lab in funds):
        funds += D._universe(db, None, [focus])
    out, book_src = [], None
    focus_book = None
    for sid, lab in funds:
        rows = db.get_nav_intraday(sid, date, date)
        keys = [int(r[1]) for r in rows]
        j = bisect.bisect_right(keys, int(time)) - 1
        if j < 0:
            continue
        _d, t, nav, nav_d, last, _vol, nav_t = rows[j]
        if not nav or nav <= 0:
            continue
        bk = _book_at(db, sid, date, time)
        src = "tse" if bk is not None else None
        if bk is None and lab == focus:
            bk = _book_from_snapshots(db, lab, date, time)
            src = "snapshots" if bk is not None else None
        bid1 = bk[0][0][0] if bk and bk[0] else None
        ask1 = bk[1][0][0] if bk and bk[1] else None
        fair = nav * (1 + baseline_pct / 100.0)
        rec = {"symbol": lab, "time": int(t), "age_min": round((_sec(time) - _sec(t)) / 60.0, 1),
               "nav": nav, "nav_date": nav_d, "nav_time": nav_t, "last": last,
               "bubble_last_pct": round((last / fair - 1) * 100, 3) if last else None,
               "bid": bid1, "ask": ask1,
               "bubble_ask_pct": round((ask1 / fair - 1) * 100, 3) if ask1 else None,
               "bubble_bid_pct": round((bid1 / fair - 1) * 100, 3) if bid1 else None}
        out.append(rec)
        if lab == focus:
            focus_book, book_src = bk, src
    return {"date": date, "time": time, "focus": focus,
            "book": {"bids": focus_book[0], "asks": focus_book[1]} if focus_book else None,
            "book_source": book_src, "funds": out}
