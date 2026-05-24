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


def _aggregate_ticks(ticks: list, interval_min: int) -> list:
    """Aggregate tick rows into OHLCV bars of *interval_min* minutes.

    Each tick dict has: seq, time (HHMMSS int), price, volume, canceled.
    Returns list of bar dicts sorted ascending by bar_time (HHMM int).
    """
    bars: dict = {}
    for t in sorted(ticks, key=lambda x: x.get("seq", 0)):
        if t.get("canceled"):
            continue
        heven = t.get("time", 0)
        h = heven // 10000
        m = (heven % 10000) // 100
        bar_m   = (m // interval_min) * interval_min
        bar_key = h * 100 + bar_m          # HHMM int
        price   = t.get("price",  0)
        volume  = t.get("volume", 0)
        if price <= 0:
            continue
        if bar_key not in bars:
            bars[bar_key] = {
                "time":   bar_key,
                "open":   price,
                "high":   price,
                "low":    price,
                "close":  price,
                "volume": 0,
                "count":  0,
            }
        b = bars[bar_key]
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
        """Latest snapshot for every tracked fund, sorted by |premium_discount|."""
        rows = db.get_latest()
        return jsonify({"funds": rows, "last_scan": db.last_scan_time()})

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

    @app.route("/api/intraday_bars")
    def api_intraday_bars():
        """Aggregate intraday tick trades into OHLCV bars.

        Query params:
          symbol   – fund symbol (required)
          date     – YYYYMMDD int (default: today)
          interval – minutes per bar: 1, 5, 15, 30, 60 (default: 5)
        """
        symbol   = request.args.get("symbol", "")
        interval = int(request.args.get("interval", 5))
        date_int = request.args.get(
            "date",
            datetime.now().strftime("%Y%m%d"),
        )
        date_int = int(date_int)
        if not symbol:
            return jsonify({"error": "symbol required"}), 400
        if interval not in (1, 2, 3, 5, 10, 15, 30, 60):
            interval = 5

        ticks = db.get_intraday_trades(symbol, date_int)
        bars  = _aggregate_ticks(ticks, interval)
        return jsonify({
            "symbol":   symbol,
            "date":     date_int,
            "interval": interval,
            "bars":     bars,
        })

    @app.route("/api/intraday_dates")
    def api_intraday_dates():
        """Return list of dates (YYYYMMDD) that have intraday tick data."""
        symbol = request.args.get("symbol", "")
        if not symbol:
            return jsonify({"error": "symbol required"}), 400
        dates = db.get_intraday_dates(symbol)
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
