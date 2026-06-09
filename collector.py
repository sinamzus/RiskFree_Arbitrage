"""Generic per-symbol historical data collector (daily + tick + order-book).

Funds (صندوق) and اخزا (treasury bills) both store their time-series into the
same symbol-keyed tables (``daily_history``, ``intraday_trades``,
``intraday_orderbook``), so a single collector serves both.  The on-demand
"collect data" dialog in the web UI drives this for whatever symbols the user
selects.

Fetching runs across a bounded thread pool to overlap network latency; the
fetcher's process-wide rate limiter (see data_fetcher._RateLimiter) keeps the
aggregate request rate ban-safe regardless of worker count.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _recent_trading_dates(n: int) -> list[int]:
    """Return the last *n* non-weekend dates as YYYYMMDD ints (most recent first).

    Iran's weekend is Thursday (weekday 3) and Friday (weekday 4).
    """
    out, d = [], datetime.now()
    while len(out) < n:
        if d.weekday() not in (3, 4):
            out.append(int(d.strftime("%Y%m%d")))
        d -= timedelta(days=1)
    return out


def _days_between(a: int, b: int) -> int:
    """Absolute calendar days between two YYYYMMDD ints."""
    da = datetime.strptime(str(a), "%Y%m%d")
    db_ = datetime.strptime(str(b), "%Y%m%d")
    return abs((db_ - da).days)


def collect_history(db, fetcher, targets: list[dict], *,
                    force_full: bool = False,
                    daily_days: int = 365,
                    intraday_days: int = 30,
                    fetch_intraday: bool = True,
                    fetch_ob: bool = True,
                    workers: int | None = None,
                    progress: dict | None = None,
                    progress_lock: threading.Lock | None = None) -> dict:
    """Collect daily + intraday-tick + order-book history for *targets*.

    Parameters
    ----------
    db, fetcher  : Database, TSETMCFetcher
    targets      : list of {"symbol", "ins_code"} — only these are collected.
    force_full   : re-download the full daily window even if data exists.
    daily_days   : days of daily OHLCV to request.
    intraday_days: recent trading days of tick + OB to collect.
    fetch_intraday / fetch_ob : toggle those passes.
    workers      : thread-pool size (defaults to config.FETCH_WORKERS).
    progress     : optional dict updated in-place with live progress
                   ({done, total, current}); guarded by *progress_lock*.

    Returns
    -------
    dict: {symbols, daily_new, intraday_new, ob_new, dates, per_symbol:{...}}
    """
    try:
        from config import FETCH_WORKERS
    except ImportError:
        FETCH_WORKERS = 5
    workers = max(1, workers or FETCH_WORKERS)

    targets = [t for t in targets if (t.get("ins_code") or "").strip()]
    today_int = int(datetime.now().strftime("%Y%m%d"))
    recent_dates = (_recent_trading_dates(intraday_days)
                    if (fetch_intraday or fetch_ob) else [])

    totals = {"daily_new": 0, "intraday_new": 0, "ob_new": 0}
    per_symbol: dict[str, dict] = {}
    lock = threading.Lock()
    if progress is not None:
        with (progress_lock or lock):
            progress.update({"done": 0, "total": len(targets), "current": ""})

    def _collect_one(target: dict):
        symbol = target.get("symbol", "")
        ins = (target.get("ins_code") or "").strip()
        d_new = i_new = o_new = 0
        try:
            # ── Daily OHLCV ──────────────────────────────────────────────
            last_date = None if force_full else db.get_last_daily_date(symbol)
            days_n = daily_days if last_date is None else min(
                _days_between(last_date, today_int) + 3, daily_days)
            entries = fetcher.get_historical_daily(ins, days=days_n)
            if entries:
                if last_date:
                    entries = [e for e in entries if e["date"] > last_date]
                d_new = db.save_daily_history(symbol, ins, entries)

            # ── Intraday ticks ───────────────────────────────────────────
            if fetch_intraday:
                have = {dd: len(db.get_intraday_trades(symbol, dd))
                        for dd in db.get_intraday_dates(symbol)}
                for date_int in recent_dates:
                    if date_int > today_int:
                        continue
                    is_today = date_int == today_int
                    if not is_today and have.get(date_int, 0) >= 10:
                        continue
                    trades = fetcher.get_intraday_trades(ins, date_int)
                    if trades:
                        i_new += db.save_intraday_trades(symbol, ins, date_int, trades)

            # ── Order-book history ───────────────────────────────────────
            if fetch_ob:
                have_ob = set(db.get_ob_dates(symbol))
                for date_int in recent_dates:
                    if date_int >= today_int or date_int in have_ob:
                        continue
                    snaps = fetcher.get_best_limits_history(ins, date_int)
                    if not snaps:
                        continue
                    for snap in snaps:
                        if db.save_orderbook_snapshot(
                            symbol, ins, date_int, snap.get("time", 0),
                            {"bids": snap.get("bids", []),
                             "asks": snap.get("asks", [])}, nav=0.0):
                            o_new += 1
        except Exception as exc:
            logger.warning("collect_history: %s (%s) failed: %s", symbol, ins, exc)

        with lock:
            totals["daily_new"]    += d_new
            totals["intraday_new"] += i_new
            totals["ob_new"]       += o_new
            per_symbol[symbol] = {"daily": d_new, "ticks": i_new, "ob": o_new}
        if progress is not None:
            with (progress_lock or lock):
                progress["done"] += 1
                progress["current"] = symbol
        logger.info("  collect %s: daily+%d tick+%d ob+%d", symbol, d_new, i_new, o_new)

    logger.info("collect_history: %d symbols, %d workers", len(targets), workers)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_collect_one, targets))

    return {
        "symbols":      len(targets),
        "daily_new":    totals["daily_new"],
        "intraday_new": totals["intraday_new"],
        "ob_new":       totals["ob_new"],
        "dates":        recent_dates,
        "per_symbol":   per_symbol,
    }
