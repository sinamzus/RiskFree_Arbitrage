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


def _row_at(rows: list, date: int, time: int):
    """The fund's latest prepared row on ``date`` at or before ``time`` (None without one)."""
    keys = [(r[1], r[2]) for r in rows]
    j = bisect.bisect_right(keys, (date, int(time))) - 1
    return rows[j] if j >= 0 and rows[j][1] == date else None


def trade_moment(db, cats: list[str] | None, symbols: list[str] | None, focus: str, date: int, time: int,
                 baseline_pct: float = 0.0, params: dict | None = None) -> dict:
    """Every selected fund at a trade's instant, read from the SAME prepared rows the backtest used (dataset, NAV
    known at that instant, the fund's own norm): price, NAV, raw bubble, the fund's own norm and the distance from
    it (the signal when ``baseline_days`` > 0), the bubble at the best ask / bid against the signal's fair value, and
    the group average. ``norm_days`` (the backtest's baseline window, else 20 trading days) defines the norm shown
    even when the strategy itself compares with NAV directly."""
    import discount_backtest as D
    from dataclasses import fields, replace
    known = {f.name for f in fields(D.DiscountParams)}
    p = D.DiscountParams(**{k: v for k, v in (params or {}).items() if k in known}) if params else D.DiscountParams()
    norm_days = p.baseline_days if p.baseline_days > 0 else 20
    # the norm with the backtest's own rules (same rows → same daily medians); the display itself also shows a NAV
    # past the age limit (with its age) instead of hiding the fund
    p_norm = replace(p, baseline_days=norm_days, mr_center="off", crash_drop_pct=0.0, rally_rise_pct=0.0)
    p_raw = replace(p_norm, baseline_days=0, max_nav_age_min=0, max_nav_age_days=-1)
    funds = D._universe(db, cats, None) if cats else []
    if symbols:
        have = {lab for _s, lab in funds}
        funds += [f for f in D._universe(db, None, symbols) if f[1] not in have]
    if focus and all(lab != focus for _s, lab in funds):
        funds += D._universe(db, None, [focus])
    out, book_src = [], None
    focus_book = None
    for sid, lab in funds:
        try:
            rn = D._load_ex(db, sid, date, date, p_norm)["rows"]
            rr = D._load_ex(db, sid, date, date, p_raw)["rows"]
        except Exception:
            continue
        r = _row_at(rr, date, time)
        if r is None:
            continue
        _o, _d, t, _fair, last, _fresh, nav = r
        if not nav or nav <= 0 or not last:
            continue
        rnorm = next((x for x in rn if x[1] == date and x[6]), None)      # the norm is one value per fund and day
        norm = (rnorm[3] / rnorm[6] - 1.0) if rnorm is not None else None
        # the fair value the strategy compared with: NAV × (1 + own norm) when it uses a norm, else NAV
        fair = nav * (1.0 + norm) if (p.baseline_days > 0 and norm is not None) else nav
        bk = _book_at(db, sid, date, time)
        src = "tse" if bk is not None else None
        if bk is None and lab == focus:
            bk = _book_from_snapshots(db, lab, date, time)
            src = "snapshots" if bk is not None else None
        bid1 = bk[0][0][0] if bk and bk[0] else None
        ask1 = bk[1][0][0] if bk and bk[1] else None
        raw_b = last / nav - 1.0
        rec = {"symbol": lab, "time": int(t), "age_min": round((_sec(time) - _sec(t)) / 60.0, 1),
               "nav": nav, "last": last, "bid": bid1, "ask": ask1,
               "bubble_raw_pct": round(raw_b * 100, 3),
               "norm_pct": round(norm * 100, 3) if norm is not None else None,
               "vs_norm_pct": round((raw_b - norm) * 100, 3) if norm is not None else None,
               # the signal itself (what entry / switch thresholds are compared with)
               "bubble_last_pct": round((last / fair - 1) * 100, 3),
               "bubble_ask_pct": round((ask1 / fair - 1) * 100, 3) if ask1 else None,
               "bubble_bid_pct": round((bid1 / fair - 1) * 100, 3) if bid1 else None}
        try:
            import nav_feed as NF
            k = NF.known_at(db, sid, date, time)
            if k is not None:
                rec["nav_date"], rec["nav_time"] = k[1], k[2]
                if k[1] and k[2]:
                    rec["nav_age_min"] = round((D._ord(date) * 86400 + _sec(time)
                                                - D._ord(k[1]) * 86400 - _sec(k[2])) / 60.0, 1)
        except Exception:
            pass
        out.append(rec)
        if lab == focus:
            focus_book, book_src = bk, src
    raws = [f["bubble_raw_pct"] for f in out]
    group = round(sum(raws) / len(raws), 3) if raws else None
    for f in out:
        f["vs_group_pct"] = round(f["bubble_raw_pct"] - group, 3) if group is not None else None
    return {"date": date, "time": time, "focus": focus, "norm_days": norm_days,
            "signal": "norm" if p.baseline_days > 0 else "nav", "group_bubble_pct": group,
            "book": {"bids": focus_book[0], "asks": focus_book[1]} if focus_book else None,
            "book_source": book_src, "funds": out}
