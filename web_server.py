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

    # Store push function so the scanner thread can call it
    app.push_to_sse = push_to_sse

    # ── Routes ───────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return send_from_directory(str(STATIC_DIR), "index.html")

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
