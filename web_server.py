"""Flask web server for the inter-day arbitrage dashboard.

Endpoints
---------
GET  /                              → serves the single-page UI
GET  /api/funds                     → latest snapshot for every tracked fund
GET  /api/history                   → ?symbol=X&days=30  scan-snapshot time-series
GET  /api/daily_history             → ?symbol=X&days=365 daily OHLCV from daily_history table
GET  /api/stats                     → ?symbol=X&days=30  mean-reversion stats
GET  /api/all_stats                 → ?days=30  stats for all symbols
GET  /api/stream                    → SSE stream: pushed on every new scan
POST /api/scan                      → trigger an immediate scan (optional manual)
"""

import json
import logging
import queue
import threading
import time
from datetime import datetime
from typing import Optional


def _aggregate_ticks(ticks: list, interval_min: int, date_int: int = 0) -> list:
    """Aggregate tick rows into OHLCV bars of *interval_min* minutes.

    Each tick dict has: seq, time (HHMMSS int), price, volume, canceled.
    Returns list of bar dicts sorted ascending by bar_time (Unix UTC seconds).
    """
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    _tz = ZoneInfo("Asia/Tehran")

    d = str(date_int or 19700101)
    year, month, day = int(d[:4]), int(d[4:6]), int(d[6:8])

    bars: dict = {}
    for t in sorted(ticks, key=lambda x: x.get("seq", 0)):
        if t.get("canceled"):
            continue
        heven = t.get("time", 0)
        h = heven // 10000
        m = (heven % 10000) // 100
        bar_m   = (m // interval_min) * interval_min
        price   = t.get("price",  0)
        volume  = t.get("volume", 0)
        if price <= 0:
            continue

        # Proper Unix timestamp: Iran local time → UTC
        try:
            bar_unix = int(_dt(year, month, day, h, bar_m, 0, tzinfo=_tz).timestamp())
        except Exception:
            bar_unix = h * 3600 + bar_m * 60  # fallback

        if bar_unix not in bars:
            bars[bar_unix] = {
                "time":   bar_unix,
                "open":   price,
                "high":   price,
                "low":    price,
                "close":  price,
                "volume": 0,
                "count":  0,
            }
        b = bars[bar_unix]
        b["high"]   = max(b["high"], price)
        b["low"]    = min(b["low"],  price)
        b["close"]  = price
        b["volume"] += volume
        b["count"]  += 1
    return sorted(bars.values(), key=lambda x: x["time"])

from flask import Flask, jsonify, request, Response, send_from_directory
from pathlib import Path

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(db, scan_callback=None):
    """
    Parameters
    ----------
    db            : Database  instance
    scan_callback : callable  called when POST /api/scan is triggered
    """
    app = Flask(__name__, static_folder=str(STATIC_DIR))
    app.config["JSON_AS_ASCII"] = False   # allow Persian text in JSON

    # ── SSE subscriber registry ──────────────────────────────────────────
    _sse_queues: list[queue.Queue] = []
    _sse_lock = threading.Lock()

    def push_to_sse(data: dict):
        """Push *data* to all connected SSE clients."""
        payload = json.dumps(data, ensure_ascii=False)
        with _sse_lock:
            dead = []
            for q in _sse_queues:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                _sse_queues.remove(q)

    # Bootstrap state — updated by main.py via push_to_sse
    _bootstrap_state = {"status": "idle", "message": ""}

    _orig_push_to_sse = push_to_sse
    def push_to_sse(data: dict):
        # Track bootstrap status so late-connecting browsers can query it
        if data.get("type") == "bootstrap_start":
            _bootstrap_state["status"]  = "running"
            _bootstrap_state["message"] = data.get("message", "")
        elif data.get("type") == "bootstrap_complete":
            _bootstrap_state["status"]  = "done"
            _bootstrap_state["message"] = data.get("message", "")
        _orig_push_to_sse(data)

    # Store push function so the scanner thread can call it
    app.push_to_sse = push_to_sse

    # ── Routes ───────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return send_from_directory(str(STATIC_DIR), "index.html")

    @app.route("/api/bootstrap_status")
    def api_bootstrap_status():
        """Return current bootstrap state so the UI can show progress on page-load."""
        return jsonify(_bootstrap_state)

    @app.route("/api/funds")
    def api_funds():
        """All configured funds, enriched with latest scan snapshot where available.

        Always returns all configured funds — even those not yet scanned.
        Funds without a snapshot get zero/null values so the UI can show them.
        """
        from config import FIXED_INCOME_ETFS
        latest_map = {r["symbol"]: r for r in db.get_latest()}

        funds = []
        for f in FIXED_INCOME_ETFS:
            sym = f["symbol"]
            snap = latest_map.get(sym)
            if snap:
                row = dict(snap)
            else:
                # stub — fund is configured but not yet scanned
                row = {
                    "symbol":               sym,
                    "name":                 f.get("name", sym),
                    "market_price":         0,
                    "nav":                  0,
                    "issue_nav":            0,
                    "cancel_nav":           0,
                    "statistical_nav":      0,
                    "premium_discount_pct": 0,
                    "net_profit_pct":       0,
                    "volume":               0,
                    "value":                0,
                    "trade_count":          0,
                    "best_bid":             0,
                    "best_ask":             0,
                    "signal":               "HOLD",
                    "actionable":           0,
                    "intraday_trend":       "",
                    "trend_slope":          0,
                    "vwap":                 0,
                    "vwap_premium_pct":     0,
                    "tick_count_today":     0,
                    "tradable":             0,
                    "tradable_volume":      0,
                    "tradable_value":       0,
                    "spread_pct":           0,
                    "ob_score":             0,
                    "tradability_reason":   "",
                    "scanned_at":           None,
                }
            funds.append(row)

        # Sort: scanned funds first (by |premium_discount|), unscanned at bottom
        funds.sort(key=lambda r: (
            0 if r.get("scanned_at") else 1,
            -abs(r.get("premium_discount_pct") or 0),
        ))

        return jsonify({"funds": funds, "last_scan": db.last_scan_time()})

    @app.route("/api/history")
    def api_history():
        symbol = request.args.get("symbol", "")
        days   = int(request.args.get("days", 30))
        if not symbol:
            return jsonify({"error": "symbol required"}), 400
        history = db.get_history(symbol, days)
        stats   = db.get_stats(symbol, days)
        return jsonify({"symbol": symbol, "days": days,
                        "history": history, "stats": stats})

    @app.route("/api/stats")
    def api_stats():
        symbol = request.args.get("symbol", "")
        days   = int(request.args.get("days", 30))
        if not symbol:
            return jsonify({"error": "symbol required"}), 400
        return jsonify(db.get_stats(symbol, days))

    @app.route("/api/all_stats")
    def api_all_stats():
        days = int(request.args.get("days", 30))
        return jsonify(db.get_all_stats(days))

    @app.route("/api/daily_history")
    def api_daily_history():
        symbol = request.args.get("symbol", "")
        days   = int(request.args.get("days", 365))
        if not symbol:
            return jsonify({"error": "symbol required"}), 400
        history = db.get_daily_history(symbol, days)

        # Belt-and-suspenders: if today's bar is missing (scan hasn't written it
        # yet, or the fund had close_price=0 at bootstrap time), synthesise it
        # from the most recent snapshot row so the daily chart always shows today.
        today_int = int(datetime.now().strftime("%Y%m%d"))
        has_today = any(r.get("date") == today_int for r in history)
        if not has_today:
            latest_map = {r["symbol"]: r for r in db.get_latest()}
            snap = latest_map.get(symbol)
            if snap:
                close  = (snap.get("market_price") or snap.get("best_bid") or 0)
                nav    = (snap.get("cancel_nav") or snap.get("nav") or 0)
                prem   = round((close - nav) / nav * 100, 4) if nav > 0 and close > 0 else 0
                prev   = history[-1].get("close_price", 0) if history else 0
                today_row = {
                    "symbol":          symbol,
                    "ins_code":        snap.get("ins_code", ""),
                    "date":            today_int,
                    "open_price":      close,
                    "high_price":      close,
                    "low_price":       close,
                    "close_price":     close,
                    "yesterday_price": nav,
                    "volume":          snap.get("volume", 0),
                    "value":           snap.get("value", 0),
                    "trade_count":     snap.get("trade_count", 0),
                    "price_change":    close - prev,
                    "premium_pct":     prem,
                }
                history = history + [today_row]

        return jsonify({"symbol": symbol, "days": days, "history": history})

    def _nav_for_date(symbol: str, date_int: int) -> float:
        """Return NAV for *symbol* on *date_int* (YYYYMMDD).

        Uses daily_history.yesterday_price (≈ NAV) for historical dates.
        For today, falls back to the latest snapshot.
        """
        today_int = int(datetime.now().strftime("%Y%m%d"))
        if date_int < today_int:
            # Historical: use yesterday_price from daily_history
            rows = db.get_daily_history(symbol, days=365)
            for r in rows:
                if r.get("date") == date_int:
                    return r.get("yesterday_price", 0)
            return 0
        else:
            # Today or future: use latest snapshot NAV
            latest = {r["symbol"]: r for r in db.get_latest()}
            snap = latest.get(symbol, {})
            return snap.get("cancel_nav") or snap.get("nav") or 0

    @app.route("/api/intraday_bars")
    def api_intraday_bars():
        """Aggregate intraday tick trades into OHLCV bars + premium per bar.

        Query params:
          symbol   – fund symbol (required)
          days     – number of recent trading days to return (default: 1)
          date     – optional end date YYYYMMDD (limits to dates <= this)
          interval – minutes per bar: 1, 5, 15, 30, 60 (default: 5)

        Bar `time` field is UTC Unix timestamp (Asia/Tehran aware).
        Response includes ``nav`` (from most recent date with data).
        """
        symbol   = request.args.get("symbol", "")
        interval = int(request.args.get("interval", 5))
        days_back = int(request.args.get("days", 1))
        date_str  = request.args.get("date", "")

        if not symbol:
            return jsonify({"error": "symbol required"}), 400
        if interval not in (1, 2, 3, 5, 10, 15, 30, 60):
            interval = 5
        days_back = max(1, min(days_back, 60))

        # Determine dates to fetch
        available_dates = db.get_intraday_dates(symbol)
        if not available_dates:
            return jsonify({"symbol": symbol, "interval": interval,
                            "days": days_back, "nav": 0, "bars": []})

        if date_str:
            end_date = int(date_str)
            pool = [d for d in available_dates if d <= end_date]
        else:
            pool = list(available_dates)
        dates_to_fetch = pool[-days_back:]

        all_bars = []
        nav = 0.0
        for date_int in dates_to_fetch:
            ticks     = db.get_intraday_trades(symbol, date_int)
            if not ticks:
                continue
            date_nav  = _nav_for_date(symbol, date_int)
            if date_nav > 0 and nav == 0:
                nav = date_nav
            bars = _aggregate_ticks(ticks, interval, date_int)
            for b in bars:
                if date_nav > 0:
                    b["premium_pct"] = round((b["close"] - date_nav) / date_nav * 100, 4)
                else:
                    b["premium_pct"] = None
            all_bars.extend(bars)

        all_bars.sort(key=lambda x: x["time"])

        return jsonify({
            "symbol":   symbol,
            "interval": interval,
            "days":     days_back,
            "nav":      nav,
            "bars":     all_bars,
        })

    @app.route("/api/intraday_ticks")
    def api_intraday_ticks():
        """Return raw tick-level trades for *symbol*.

        Query params:
          symbol – fund symbol (required)
          days   – number of recent trading days (default: 1)
          date   – optional end date YYYYMMDD

        Each tick: seq, unix_time (UTC Unix seconds), time (HHMMSS int),
                   price, volume, premium_pct, date (YYYYMMDD int)
        """
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        _tz = ZoneInfo("Asia/Tehran")

        symbol    = request.args.get("symbol", "")
        days_back = int(request.args.get("days", 1))
        date_str  = request.args.get("date", "")

        if not symbol:
            return jsonify({"error": "symbol required"}), 400
        days_back = max(1, min(days_back, 60))

        available_dates = db.get_intraday_dates(symbol)
        if not available_dates:
            return jsonify({"symbol": symbol, "days": days_back,
                            "nav": 0, "tick_count": 0, "ticks": []})

        if date_str:
            end_date = int(date_str)
            pool = [d for d in available_dates if d <= end_date]
        else:
            pool = list(available_dates)
        dates_to_fetch = pool[-days_back:]

        result = []
        nav = 0.0
        for date_int in dates_to_fetch:
            ticks    = db.get_intraday_trades(symbol, date_int)
            date_nav = _nav_for_date(symbol, date_int)
            if date_nav > 0 and nav == 0:
                nav = date_nav

            d = str(date_int)
            year, month, day = int(d[:4]), int(d[4:6]), int(d[6:8])

            for t in ticks:
                if t.get("canceled"):
                    continue
                p = t.get("price", 0)
                if p <= 0:
                    continue
                heven = t.get("time", 0)
                h  = heven // 10000
                mi = (heven % 10000) // 100
                sec = heven % 100
                try:
                    unix_t = int(_dt(year, month, day, h, mi, sec, tzinfo=_tz).timestamp())
                except Exception:
                    unix_t = heven
                prem = round((p - date_nav) / date_nav * 100, 4) if date_nav > 0 else None
                result.append({
                    "seq":         t.get("seq", 0),
                    "unix_time":   unix_t,
                    "time":        heven,
                    "date":        date_int,
                    "price":       p,
                    "volume":      t.get("volume", 0),
                    "premium_pct": prem,
                })

        result.sort(key=lambda x: x["unix_time"])

        return jsonify({
            "symbol":     symbol,
            "days":       days_back,
            "nav":        nav,
            "tick_count": len(result),
            "ticks":      result,
        })

    @app.route("/api/intraday_dates")
    def api_intraday_dates():
        """Return list of dates (YYYYMMDD) that have intraday tick data."""
        symbol = request.args.get("symbol", "")
        if not symbol:
            return jsonify({"error": "symbol required"}), 400
        dates = db.get_intraday_dates(symbol)
        return jsonify({"symbol": symbol, "dates": dates})

    @app.route("/api/intraday_snapshots")
    def api_intraday_snapshots():
        """Return sub-minute price snapshots from intraday_price_history.

        Query params:
          symbol – fund symbol (required)
          days   – number of recent trading days (default: 1)
          date   – optional anchor date YYYYMMDD (returns days ≤ this)

        Each snapshot: {time (unix), date (YYYYMMDD int), hhmmss,
                        price, premium_pct, volume (per-snapshot diff),
                        cum_volume, trade_count}
        """
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        tz = ZoneInfo("Asia/Tehran")

        symbol    = request.args.get("symbol", "")
        days_back = max(1, min(int(request.args.get("days", 1)), 60))
        date_str  = request.args.get("date", "")

        if not symbol:
            return jsonify({"error": "symbol required"}), 400

        # Determine which dates have snapshot data
        import sqlite3 as _sq
        conn = _sq.connect(db.path)
        conn.row_factory = _sq.Row
        if date_str:
            avail = [r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM intraday_price_history "
                "WHERE symbol=? AND date<=? ORDER BY date ASC",
                (symbol, int(date_str))
            ).fetchall()]
        else:
            avail = [r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM intraday_price_history "
                "WHERE symbol=? ORDER BY date ASC",
                (symbol,)
            ).fetchall()]
        conn.close()

        if not avail:
            return jsonify({"symbol": symbol, "days": days_back,
                            "nav": 0, "snapshots": []})

        dates_to_fetch = avail[-days_back:]

        out, nav = [], 0.0
        for date_int in dates_to_fetch:
            snaps = db.get_intraday_price_history(symbol, date_int)
            if not snaps:
                continue
            date_nav = _nav_for_date(symbol, date_int)
            if date_nav > 0 and nav == 0:
                nav = date_nav

            ystr = str(date_int)
            y, m, d = int(ystr[:4]), int(ystr[4:6]), int(ystr[6:])

            prev_vol = 0
            for s in snaps:
                t = int(s["time"])
                hh = t // 10000
                mm = (t // 100) % 100
                ss = t % 100
                if not (0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60):
                    continue
                try:
                    dt_local = _dt(y, m, d, hh, mm, ss, tzinfo=tz)
                except Exception:
                    continue
                price = s["last_price"] or s["close_price"] or 0
                cum   = int(s["cum_volume"] or 0)
                # Per-snapshot volume = diff from previous (clip negatives)
                diff = cum - prev_vol if cum >= prev_vol else cum
                prev_vol = cum

                out.append({
                    "time":        int(dt_local.timestamp()),
                    "date":        date_int,
                    "hhmmss":      t,
                    "price":       price,
                    "cum_volume":  cum,
                    "volume":      diff,
                    "trade_count": int(s["trade_count"] or 0),
                    "premium_pct": (round((price - date_nav) / date_nav * 100, 4)
                                    if date_nav > 0 else None),
                })

        out.sort(key=lambda x: x["time"])
        return jsonify({
            "symbol": symbol, "days": days_back,
            "nav": nav, "snapshots": out,
        })

    @app.route("/api/live_intraday")
    def api_live_intraday():
        """Today's LIVE intraday bars, fetched fresh from TSETMC on every call.

        Strategy (in priority order so the chart is never empty during a session):
          1. Aggregate today's raw ticks (Trade/GetTrade) into `interval`-minute
             OHLCV bars — true minute resolution, right up to the latest trade.
          2. Fall back to Trade/GetTradeIntraday (sparse ~2-min bars).
          3. Always fetch the current spot price (ClosingPriceInfo) and append it
             as the most-recent point so "price right now" is always visible.

        Query params:
          symbol   – fund symbol (required)
          interval – minutes per bar (default 1)

        Returns {symbol, date, nav, spot, fetched_at, source, bar_count, bars}
        Each bar: {time (UTC unix), hhmmss, open, high, low, close, volume, premium_pct}
        """
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        from data_fetcher import TSETMCFetcher
        from config import FIXED_INCOME_ETFS
        import sqlite3 as _sq

        symbol   = request.args.get("symbol", "")
        interval = int(request.args.get("interval", 1) or 1)
        if interval not in (1, 2, 3, 5, 10, 15, 30, 60):
            interval = 1
        if not symbol:
            return jsonify({"error": "symbol required"}), 400

        # ── Resolve ins_code: config is authoritative (no DB dependency) ──────
        ins_code = ""
        for f in FIXED_INCOME_ETFS:
            if f.get("symbol") == symbol:
                ins_code = str(f.get("ins_code", "") or "")
                break
        latest_map = {r["symbol"]: r for r in db.get_latest()}
        snap = latest_map.get(symbol, {})
        if not ins_code:
            ins_code = snap.get("ins_code", "") or ""
        if not ins_code:
            conn = _sq.connect(db.path)
            row = conn.execute(
                "SELECT ins_code FROM daily_history "
                "WHERE symbol=? AND ins_code!='' LIMIT 1", (symbol,)
            ).fetchone()
            conn.close()
            ins_code = row[0] if row else ""
        if not ins_code:
            return jsonify({"error": f"ins_code not found for {symbol}",
                            "bars": [], "bar_count": 0}), 404

        tz = ZoneInfo("Asia/Tehran")
        now = _dt.now(tz)
        today_int = int(now.strftime("%Y%m%d"))

        tsetmc = TSETMCFetcher()

        # ── NAV (for premium): snapshot → ClosingPriceInfo embedded → yesterday ──
        nav = snap.get("cancel_nav") or snap.get("nav") or 0
        cpi = tsetmc.get_closing_price_info(ins_code) or {}
        if nav <= 0:
            nav = cpi.get("embedded_nav") or cpi.get("yesterday_price") or 0

        def _prem(p):
            return round((p - nav) / nav * 100, 4) if (nav > 0 and p > 0) else None

        # ── 1) Aggregate today's raw ticks into interval-minute bars ──────────
        source = "trades"
        bars = []
        ticks = tsetmc.get_today_trades(ins_code)
        if ticks:
            raw = _aggregate_ticks(ticks, interval, today_int)
            for b in raw:
                b["premium_pct"] = _prem(b["close"])
                bars.append(b)

        # ── 2) Fall back to GetTradeIntraday sparse bars ──────────────────────
        if not bars:
            source = "intraday_bars"
            y, m, d = now.year, now.month, now.day
            for r in tsetmc.get_today_intraday_bars(ins_code):
                heven = r.get("time", 0)
                if not heven:
                    continue
                hh, mi, ss = heven // 10000, (heven % 10000) // 100, heven % 100
                if not (0 <= hh < 24 and 0 <= mi < 60 and 0 <= ss < 60):
                    continue
                close = r.get("close", 0) or 0
                if close <= 0:
                    continue
                try:
                    unix_t = int(_dt(y, m, d, hh, mi, ss, tzinfo=tz).timestamp())
                except Exception:
                    continue
                bars.append({
                    "time":   unix_t, "hhmmss": heven,
                    "open":   r.get("open", close), "high": r.get("high", close),
                    "low":    r.get("low", close),  "close": close,
                    "volume": r.get("volume", 0),   "premium_pct": _prem(close),
                })

        bars.sort(key=lambda x: x["time"])

        # ── 3) Current spot price — always show "right now" ───────────────────
        spot = cpi.get("last_price") or cpi.get("close_price") or 0
        if spot > 0:
            spot_unix = int(now.replace(second=0, microsecond=0).timestamp())
            spot_hhmmss = now.hour * 10000 + now.minute * 100
            if bars and bars[-1]["time"] >= spot_unix:
                # market gives us a fresher last bar; just refresh its close
                last = bars[-1]
                last["close"] = spot
                last["high"]  = max(last["high"], spot)
                last["low"]   = min(last["low"],  spot)
                last["premium_pct"] = _prem(spot)
            else:
                bars.append({
                    "time": spot_unix, "hhmmss": spot_hhmmss,
                    "open": spot, "high": spot, "low": spot, "close": spot,
                    "volume": 0, "premium_pct": _prem(spot),
                })

        logger.info("[live_intraday] %s ins=%s src=%s bars=%d spot=%s nav=%s",
                    symbol, ins_code, source, len(bars), spot, nav)
        return jsonify({
            "symbol":     symbol,
            "date":       today_int,
            "nav":        nav,
            "spot":       spot,
            "interval":   interval,
            "source":     source,
            "fetched_at": int(time.time()),
            "bar_count":  len(bars),
            "bars":       bars,
        })

    @app.route("/api/client_type")
    def api_client_type():
        """Return individual (حقیقی) vs legal (حقوقی) money flow history.

        Query params:
          symbol – fund symbol (required)
          days   – number of recent trading days (default: 30)
          date   – optional anchor date YYYYMMDD (default: latest)

        For each day returns:
          date, buy_i_vol, buy_n_vol, sell_i_vol, sell_n_vol,
          buy_i_val, buy_n_val, sell_i_val, sell_n_val,
          net_i_val (= buy_i_val - sell_i_val),
          net_n_val (= buy_n_val - sell_n_val)
        """
        symbol    = request.args.get("symbol", "")
        days_back = max(1, min(int(request.args.get("days", 30)), 365))
        date_str  = request.args.get("date", "")

        if not symbol:
            return jsonify({"error": "symbol required"}), 400

        import sqlite3 as _sq
        conn = _sq.connect(db.path)
        conn.row_factory = _sq.Row
        if date_str:
            anchor = int(date_str)
            rows = conn.execute(
                "SELECT * FROM client_type_daily "
                "WHERE symbol=? AND date<=? "
                "ORDER BY date DESC LIMIT ?",
                (symbol, anchor, days_back)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM client_type_daily "
                "WHERE symbol=? ORDER BY date DESC LIMIT ?",
                (symbol, days_back)
            ).fetchall()
        conn.close()

        history = []
        for r in reversed(rows):
            d = dict(r)
            d["net_i_val"] = d["buy_i_val"] - d["sell_i_val"]
            d["net_n_val"] = d["buy_n_val"] - d["sell_n_val"]
            d["net_i_vol"] = d["buy_i_vol"] - d["sell_i_vol"]
            d["net_n_vol"] = d["buy_n_vol"] - d["sell_n_vol"]
            history.append(d)

        return jsonify({"symbol": symbol, "days": days_back,
                        "history": history})

    @app.route("/api/orderbook")
    def api_orderbook():
        """Return the latest order-book snapshot + tradability for *symbol*.

        Query params:
          symbol  – fund symbol (required)
          nav     – optional NAV override for tradability calculation
          direction – optional "BUY"|"SELL" override
        """
        from orderbook import compute_tradability, orderbook_from_db_row
        symbol    = request.args.get("symbol", "")
        nav_param = float(request.args.get("nav", 0) or 0)
        dir_param = request.args.get("direction", "")

        if not symbol:
            return jsonify({"error": "symbol required"}), 400

        row = db.get_latest_orderbook(symbol)
        if not row:
            return jsonify({"symbol": symbol, "snapshot": None,
                            "tradability": None})

        ob = orderbook_from_db_row(row)
        nav = nav_param

        # Try to get NAV from latest scan snapshot
        if nav <= 0:
            latest_map = {r["symbol"]: r for r in db.get_latest()}
            snap = latest_map.get(symbol, {})
            nav = snap.get("cancel_nav") or snap.get("nav") or 0

        direction = dir_param or "BUY"
        td = compute_tradability(direction, nav, ob) if nav > 0 else None

        return jsonify({
            "symbol":   symbol,
            "snapshot": {
                "date":       row["date"],
                "time":       row["time"],
                "spread_pct": row["spread_pct"],
                "bid_depth":  row["bid_depth"],
                "ask_depth":  row["ask_depth"],
                "bids": [
                    {"price": row[f"bid{i}_price"],
                     "volume": row[f"bid{i}_vol"],
                     "count":  row[f"bid{i}_cnt"]}
                    for i in range(1, 6)
                ],
                "asks": [
                    {"price": row[f"ask{i}_price"],
                     "volume": row[f"ask{i}_vol"],
                     "count":  row[f"ask{i}_cnt"]}
                    for i in range(1, 6)
                ],
            },
            "tradability": {
                "tradeable":            td.tradeable,
                "direction":            td.direction,
                "threshold_price":      td.threshold_price,
                "best_executable_price": td.best_executable_price,
                "executable_volume":    td.executable_volume,
                "executable_value":     td.executable_value,
                "avg_fill_price":       td.avg_fill_price,
                "slippage_pct":         td.slippage_pct,
                "spread_pct":           td.spread_pct,
                "book_depth_score":     td.book_depth_score,
                "reason":               td.reason,
            } if td else None,
        })

    @app.route("/api/orderbook_history")
    def api_orderbook_history():
        """Return intraday order-book snapshots for *symbol* on *date*.

        Query params:
          symbol – fund symbol (required)
          date   – YYYYMMDD (default: today)
          limit  – max rows (default: 500)
        """
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        symbol   = request.args.get("symbol", "")
        date_str = request.args.get("date", "")
        limit    = int(request.args.get("limit", 500))

        if not symbol:
            return jsonify({"error": "symbol required"}), 400

        if date_str:
            date_int = int(date_str)
        else:
            date_int = int(_dt.now(ZoneInfo("Asia/Tehran")).strftime("%Y%m%d"))

        rows = db.get_orderbook_history(symbol, date_int, limit)
        logger.info("[OB-History] symbol=%s date=%s → %d snapshots", symbol, date_int, len(rows))

        # Convert to compact format for the chart:
        # Each entry: {time (HHMMSS), spread_pct, bid_depth, ask_depth,
        #              b1p, b1v, b2p, b2v, b3p, b3v, b4p, b4v, b5p, b5v,
        #              a1p, a1v, a2p, a2v, a3p, a3v, a4p, a4v, a5p, a5v}
        out = []
        for r in rows:
            entry = {
                "time":       r["time"],
                "spread_pct": r["spread_pct"],
                "bid_depth":  r["bid_depth"],
                "ask_depth":  r["ask_depth"],
            }
            for i in range(1, 6):
                entry[f"b{i}p"] = r[f"bid{i}_price"]
                entry[f"b{i}v"] = r[f"bid{i}_vol"]
                entry[f"a{i}p"] = r[f"ask{i}_price"]
                entry[f"a{i}v"] = r[f"ask{i}_vol"]
            out.append(entry)

        return jsonify({"symbol": symbol, "date": date_int,
                        "count": len(out), "snapshots": out})

    @app.route("/api/orderbook_dates")
    def api_orderbook_dates():
        """Return dates with OB snapshots — union of intraday_orderbook dates."""
        symbol = request.args.get("symbol", "")
        if not symbol:
            return jsonify({"error": "symbol required"}), 400
        dates = db.get_ob_dates(symbol)
        return jsonify({"symbol": symbol, "dates": dates})

    @app.route("/api/stream")
    def api_stream():
        """Server-Sent Events endpoint for real-time scan updates."""
        q: queue.Queue = queue.Queue(maxsize=20)
        with _sse_lock:
            _sse_queues.append(q)
        logger.debug("SSE client connected (%d total)", len(_sse_queues))

        def generate():
            # Send a heartbeat immediately so the browser doesn't time out
            yield "data: {\"type\":\"connected\"}\n\n"
            try:
                while True:
                    try:
                        payload = q.get(timeout=25)
                        yield f"data: {payload}\n\n"
                    except queue.Empty:
                        # Heartbeat to keep connection alive
                        yield ": heartbeat\n\n"
            except GeneratorExit:
                pass
            finally:
                with _sse_lock:
                    try:
                        _sse_queues.remove(q)
                    except ValueError:
                        pass
                logger.debug("SSE client disconnected (%d remaining)",
                             len(_sse_queues))

        return Response(
            generate(),
            content_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.route("/api/scan", methods=["POST"])
    def api_scan():
        """Trigger an immediate manual scan (runs in background thread)."""
        if scan_callback:
            threading.Thread(target=scan_callback, daemon=True).start()
            return jsonify({"status": "scan started"})
        return jsonify({"status": "no scanner configured"}), 503

    return app


def run_server(db, host: str = "0.0.0.0", port: int = 5000,
               scan_callback=None):
    """Start Flask in a daemon thread.  Returns the app object."""
    app = create_app(db, scan_callback)

    def _run():
        import logging as _log
        # Suppress Flask/Werkzeug request logs (clutters the console)
        _log.getLogger("werkzeug").setLevel(_log.WARNING)
        app.run(host=host, port=port, threaded=True, use_reloader=False)

    t = threading.Thread(target=_run, daemon=True, name="flask")
    t.start()
    logger.info("Web UI started at http://localhost:%d", port)
    return app
