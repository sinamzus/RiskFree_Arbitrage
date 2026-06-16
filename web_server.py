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
    from datetime import datetime as _dt, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        _tz = ZoneInfo("Asia/Tehran")
    except Exception:
        # Fallback for Windows without tzdata: Iran Standard Time UTC+3:30
        _tz = timezone(timedelta(hours=3, minutes=30))

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
        up_to = int(date_str) if date_str else None
        avail = db.get_intraday_snapshot_dates(symbol, up_to)

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
            ins_code = db.get_ins_code(symbol) or ""
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

        anchor = int(date_str) if date_str else None
        rows = db.get_client_type_history(symbol, days_back, anchor)

        history = []
        for d in rows:
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

    @app.route("/api/strategies")
    def api_strategies():
        """Return the catalogue of available NAV-free backtest strategies."""
        from backtest import STRATEGIES
        return jsonify({"strategies": STRATEGIES})

    @app.route("/api/backtest")
    def api_backtest():
        """Backtest a NAV-free intraday reversion strategy for *symbol*.

        Query params:
          symbol   – fund symbol (required)
          strategy – vwap | sma | prev_close (optional, default vwap)
          start    – YYYYMMDD inclusive (optional)
          end      – YYYYMMDD inclusive (optional)
          capital  – max Rials per position (optional)
          entry    – entry discount % vs reference (optional, default 0.15)
          exit     – exit premium % vs reference   (optional, default 0.15)
          window   – moving-average window in snapshots, "sma" only (default 20)
          force_eod– "1"/"0" liquidate open positions at day end (default 1)
        """
        from backtest import run_backtest, BacktestParams

        symbol = request.args.get("symbol", "")
        if not symbol:
            return jsonify({"error": "symbol required"}), 400

        def _int(name):
            v = request.args.get(name, "")
            return int(v) if v else None

        def _float(name, default):
            v = request.args.get(name, "")
            try:
                return float(v) if v != "" else default
            except ValueError:
                return default

        params = BacktestParams(
            capital=_float("capital", 1_000_000_000),
            strategy=request.args.get("strategy", "vwap") or "vwap",
            entry_discount_pct=_float("entry", 0.15),
            exit_premium_pct=_float("exit", 0.15),
            ma_window=int(_float("window", 20)) or 20,
            force_eod=request.args.get("force_eod", "1") != "0",
        )

        try:
            result = run_backtest(db, symbol, _int("start"), _int("end"), params)
        except Exception as e:
            logger.exception("backtest failed for %s", symbol)
            return jsonify({"error": str(e)}), 500

        logger.info("[Backtest] %s: %d days, %d trades, net=%s",
                    symbol, result["days_tested"],
                    result["summary"]["trade_count"],
                    result["summary"]["total_net_pnl"])
        return jsonify(result)

    # ──────────────────────────────────────────────────────────────────────
    #  Bond (اوراق بدهی) endpoints
    # ──────────────────────────────────────────────────────────────────────

    @app.route("/api/bonds/series")
    def api_bonds_series():
        """Return all registered bond series (registry)."""
        series = db.get_bond_series(active_only=False)
        return jsonify({"series": series})

    @app.route("/api/bonds/series", methods=["POST"])
    def api_bonds_series_save():
        """Upsert bond series from JSON body. Used by the Bond Registry UI."""
        from flask import request as req
        data = req.get_json(silent=True)
        if not data or "series" not in data:
            return jsonify({"error": "body must be {series:[...]}"}), 400
        try:
            n = db.upsert_bond_series(data["series"])
            return jsonify({"saved": n})
        except Exception as e:
            logger.exception("bond series save failed")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/bonds/scan")
    def api_bonds_scan():
        """Live scan: fetch prices for all active bonds, compute yield curve & signals.

        Returns the full BondSnapshot list + yield curve coefficients.
        """
        from datetime import date as _date
        from bonds import run_bond_scan, AKHZA_SERIES, analyze_bonds
        from data_fetcher import TSETMCFetcher

        today = int(_date.today().strftime("%Y%m%d"))
        series = db.get_bond_series(active_only=True)

        # Seed DB with built-in series if registry is empty
        if not series:
            db.upsert_bond_series(AKHZA_SERIES)
            series = db.get_bond_series(active_only=True)

        price_basis = request.args.get('price_basis', 'close')
        fetcher = TSETMCFetcher()
        try:
            snapshots = run_bond_scan(db, fetcher, price_basis=price_basis)
        except Exception as e:
            logger.exception("bond scan failed")
            return jsonify({"error": str(e)}), 500

        # Persist today's results
        if snapshots:
            from dataclasses import asdict
            db.save_bond_prices([
                {**asdict(s), "date": today} for s in snapshots
            ])

        from bonds import fit_yield_curve
        valid_pts = [(s.days_to_mat, s.ytm) for s in snapshots if s.ytm > 0 and s.days_to_mat > 0]
        coeffs = fit_yield_curve(valid_pts)

        from dataclasses import asdict
        return jsonify({
            "date": today,
            "snapshots": [asdict(s) for s in snapshots],
            "curve_coeffs": coeffs,
        })

    @app.route("/api/bonds/latest")
    def api_bonds_latest():
        """Return most recent stored bond scan (from DB, no network call)."""
        from datetime import date as _date
        from bonds import AKHZA_SERIES, fit_yield_curve

        series = db.get_bond_series(active_only=True)
        if not series:
            db.upsert_bond_series(AKHZA_SERIES)
            series = db.get_bond_series(active_only=True)

        # Find latest date with bond_prices
        with db._conn() as conn:
            row = conn.execute("SELECT MAX(date) AS d FROM bond_prices").fetchone()
        latest = row["d"] if row and row["d"] else None

        if not latest:
            return jsonify({"date": None, "snapshots": [], "curve_coeffs": [],
                            "series": series})

        prices = db.get_bond_prices(latest)
        valid_pts = [(p["days_to_mat"], p["ytm"]) for p in prices if p.get("ytm",0) > 0]
        coeffs = fit_yield_curve(valid_pts)

        return jsonify({
            "date": latest,
            "snapshots": prices,
            "curve_coeffs": coeffs,
            "series": series,
        })

    @app.route("/api/bonds/history")
    def api_bonds_history():
        """Return price+yield history for a single bond series."""
        symbol = request.args.get("symbol", "")
        days   = int(request.args.get("days", 90))
        if not symbol:
            return jsonify({"error": "symbol required"}), 400
        return jsonify({"history": db.get_bond_price_history(symbol, days)})

    @app.route("/api/bonds/discover")
    def api_bonds_discover():
        """Discover ACTIVE اخزا treasury bills from TSETMC and rebuild the registry.

        Searches TSETMC, keeps only genuine treasury bills (drops options and
        derivatives), parses each instrument's maturity from its name, flags
        active vs matured, then REPLACES the registry with the clean set. This
        removes stale placeholders, matured bonds and options that previously
        polluted bond_series and caused HTTP 500 spam on price fetches.
        """
        from data_fetcher import TSETMCFetcher

        # Always purge unusable placeholder rows (no ins_code) — even if the
        # network call below fails, this clears stale junk that would otherwise
        # silently break the scan and empty the chart.
        purged = db.purge_placeholder_bond_series()

        fetcher = TSETMCFetcher()
        discovered = fetcher.discover_akhza()

        if not discovered:
            return jsonify({"discovered": 0, "active": 0, "purged": purged,
                            "series": db.get_bond_series(active_only=False),
                            "note": "no treasury bills found — TSE network reachable?"})

        # Replace the registry with the clean, verified set.
        db.clear_bond_series()
        db.upsert_bond_series(discovered)

        active = [d for d in discovered if d["active"]]
        return jsonify({
            "discovered": len(discovered),
            "active":     len(active),
            "matured":    len(discovered) - len(active),
            "series":     discovered,
        })

    # Guard against overlapping collection runs (it's network-heavy).
    _bond_collect_state = {"running": False, "last": None}
    _bond_collect_lock = threading.Lock()

    @app.route("/api/bonds/collect")
    def api_bonds_collect():
        """Collect historical daily + intraday-tick + order-book data for اخزا.

        Query params:
          days      – recent trading days of tick/OB to collect (default 30)
          daily     – days of daily OHLCV (default 365)
          full      – "1" force full daily re-download (default 0)
          no_ob     – "1" skip order-book history
          no_intraday – "1" skip tick trades
          workers   – thread-pool size (optional)
          async     – "1" run in background, return immediately (default 0)

        This is what makes اخزا tick-by-tick backtesting possible — it fills the
        same symbol-keyed tables the backtest reads.
        """
        from bonds import collect_bond_history
        from data_fetcher import TSETMCFetcher

        def _int(name, default):
            v = request.args.get(name, "")
            try:
                return int(v) if v != "" else default
            except ValueError:
                return default

        kwargs = dict(
            force_full=request.args.get("full", "0") == "1",
            daily_days=_int("daily", 365),
            intraday_days=_int("days", 30),
            fetch_intraday=request.args.get("no_intraday", "0") != "1",
            fetch_ob=request.args.get("no_ob", "0") != "1",
            workers=_int("workers", 0) or None,
        )

        def _run():
            with _bond_collect_lock:
                _bond_collect_state["running"] = True
            try:
                fetcher = TSETMCFetcher()
                summary = collect_bond_history(db, fetcher, **kwargs)
                _bond_collect_state["last"] = summary
                logger.info("Bond collection done: %s", summary)
            except Exception:
                logger.exception("bond collection failed")
            finally:
                with _bond_collect_lock:
                    _bond_collect_state["running"] = False

        if _bond_collect_state["running"]:
            return jsonify({"status": "already running"}), 409

        if request.args.get("async", "0") == "1":
            threading.Thread(target=_run, daemon=True, name="bond-collect").start()
            return jsonify({"status": "started", "params": kwargs})

        # Synchronous: run and return the summary.
        try:
            fetcher = TSETMCFetcher()
            summary = collect_bond_history(db, fetcher, **kwargs)
            _bond_collect_state["last"] = summary
            return jsonify({"status": "done", **summary})
        except Exception as e:
            logger.exception("bond collection failed")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/bonds/collect/status")
    def api_bonds_collect_status():
        return jsonify({"running": _bond_collect_state["running"],
                        "last": _bond_collect_state["last"]})

    @app.route("/api/bonds/backtest")
    def api_bonds_backtest():
        """Tick-by-tick cross-sectional z-spread backtest over اخزا history.

        Query params:
          symbols   – comma-separated اخزا symbols (optional; default all)
          start     – YYYYMMDD inclusive (optional)
          end       – YYYYMMDD inclusive (optional)
          capital   – max Rials per position (default 1e9)
          entry_bps – BUY when z-spread ≥ this (default 50)
          exit_bps  – SELL when z-spread ≤ this (default 10)
          degree    – curve polynomial degree (default 2)
          min_pts   – min simultaneous series to fit a curve (default 3)
          step      – grid downsample in seconds, 0 = every snapshot (default 0)
          force_eod – "1"/"0" liquidate at day end (default 1)
        """
        from bond_backtest import run_bond_backtest, BondBacktestParams

        def _int(name):
            v = request.args.get(name, "")
            return int(v) if v else None

        def _float(name, default):
            v = request.args.get(name, "")
            try:
                return float(v) if v != "" else default
            except ValueError:
                return default

        syms_arg = request.args.get("symbols", "").strip()
        symbols = [s.strip() for s in syms_arg.split(",") if s.strip()] or None

        from bond_backtest import BUY_COST, SELL_COST
        strategy = request.args.get("strategy", "zspread")
        if strategy not in ("zspread", "outlier"):
            strategy = "zspread"
        degree = int(_float("degree", 2)) or 2
        degree = 1 if degree < 1 else (2 if degree > 2 else degree)   # curve degree 1 or 2 only
        params = BondBacktestParams(
            capital=_float("capital", 1_000_000_000),
            entry_bps=_float("entry_bps", 50.0),
            exit_bps=_float("exit_bps", 10.0),
            degree=degree,
            min_curve_points=int(_float("min_pts", 3)) or 3,
            step_secs=int(_float("step", 0)),
            force_eod=request.args.get("force_eod", "1") != "0",
            include_matured=request.args.get("include_matured", "1") != "0",
            buy_fee=_float("buy_fee", BUY_COST),
            sell_fee=_float("sell_fee", SELL_COST),
            strategy=strategy,
            min_exit_profit_bps=_float("min_exit_profit_bps", -1.0),
            total_capital=_float("total_capital", 10_000_000_000.0),
            max_position_pct=_float("max_position_pct", 0.5),
            signal_price=("mid" if request.args.get("signal_price") == "mid" else "exec"),
            entry_max_bps=_float("entry_max_bps", 150.0),
            curve_trim_bps=_float("curve_trim_bps", 150.0),
            min_dtm=int(_float("min_dtm", 30)),
            exit_needs_replacement=request.args.get("exit_needs_replacement", "1") != "0",
            min_hold_days=int(_float("min_hold_days", 1)),
            entry_confirm_ticks=int(_float("entry_confirm_ticks", 0)),
            entry_best_first=request.args.get("entry_best_first", "0") == "1",
        )

        try:
            result = run_bond_backtest(db, symbols, _int("start"), _int("end"), params)
        except Exception as e:
            logger.exception("bond backtest failed")
            return jsonify({"error": str(e)}), 500

        # Attach symbol → ins_code so the UI can deep-link each row to TSETMC.
        try:
            ins_codes = {s.get("symbol", ""): (s.get("ins_code") or "").strip()
                         for s in db.get_bond_series(active_only=False)
                         if s.get("symbol")}
        except Exception:
            ins_codes = {}
        result["ins_codes"] = ins_codes

        logger.info("[BondBacktest] %s, %d days, %d trades, net=%s",
                    strategy, result["days_tested"], result["summary"]["trade_count"],
                    result["summary"]["total_net_pnl"])
        return jsonify(result)

    # Guard + progress state for the (heavier) parameter optimizer.
    _bond_opt_state = {"running": False, "progress": {}, "result": None}
    _bond_opt_lock = threading.Lock()

    @app.route("/api/bonds/optimize", methods=["GET", "POST"])
    def api_bonds_optimize():
        """Grid-search اخزا backtest parameters; suggest the best combination.

        Params (query or JSON):
          symbols, start, end, capital, force_eod, min_trades
        Runs in the background; poll /api/bonds/optimize/status for progress
        and the final ranked result.
        """
        from bond_backtest import optimize_bond_backtest, BondBacktestParams

        body = request.get_json(silent=True) or {}
        def _get(name, default=None):
            if name in body:
                return body[name]
            return request.args.get(name, default)

        def _int(name):
            v = _get(name)
            try:
                return int(v) if v not in (None, "") else None
            except (ValueError, TypeError):
                return None

        def _float(name, default):
            v = _get(name)
            try:
                return float(v) if v not in (None, "") else default
            except (ValueError, TypeError):
                return default

        syms_arg = _get("symbols", "")
        if isinstance(syms_arg, list):
            symbols = [s for s in syms_arg if s] or None
        else:
            symbols = [s.strip() for s in str(syms_arg).split(",") if s.strip()] or None

        from bond_backtest import BUY_COST, SELL_COST
        opt_strategy = str(_get("strategy", "zspread"))
        if opt_strategy not in ("zspread", "outlier"):
            opt_strategy = "zspread"
        base = BondBacktestParams(
            capital=_float("capital", 1_000_000_000),
            step_secs=int(_float("step", 0)),
            force_eod=str(_get("force_eod", "1")) != "0",
            include_matured=str(_get("include_matured", "1")) != "0",
            buy_fee=_float("buy_fee", BUY_COST),
            sell_fee=_float("sell_fee", SELL_COST),
            strategy=opt_strategy,
            min_exit_profit_bps=_float("min_exit_profit_bps", -1.0),
            total_capital=_float("total_capital", 10_000_000_000.0),
            max_position_pct=_float("max_position_pct", 0.5),
            signal_price=("mid" if str(_get("signal_price", "exec")) == "mid" else "exec"),
            entry_max_bps=_float("entry_max_bps", 150.0),
            curve_trim_bps=_float("curve_trim_bps", 150.0),
            min_dtm=int(_float("min_dtm", 30)),
            exit_needs_replacement=str(_get("exit_needs_replacement", "1")) != "0",
            min_hold_days=int(_float("min_hold_days", 1)),
            entry_confirm_ticks=int(_float("entry_confirm_ticks", 0)),
            entry_best_first=str(_get("entry_best_first", "0")) == "1",
        )
        min_trades   = int(_float("min_trades", 3))
        opt_metric   = str(_get("opt_metric",   "sharpe"))
        walk_forward = str(_get("walk_forward", "1")) != "0"
        n_jobs       = int(_float("n_jobs", 0))   # 0 = auto (cores − 1)
        start, end   = _int("start"), _int("end")

        if _bond_opt_state["running"]:
            return jsonify({"status": "already running",
                            "progress": _bond_opt_state["progress"]}), 409

        def _run():
            with _bond_opt_lock:
                _bond_opt_state["running"] = True
                _bond_opt_state["progress"] = {"done": 0, "total": 0, "phase": "coarse"}
                _bond_opt_state["result"] = None
            try:
                res = optimize_bond_backtest(
                    db, symbols, start, end, base=base, min_trades=min_trades,
                    opt_metric=opt_metric, walk_forward=walk_forward,
                    progress=_bond_opt_state["progress"],
                    progress_lock=_bond_opt_lock,
                    n_jobs=n_jobs)
                _bond_opt_state["result"] = res
                logger.info("[BondOptimize] %d combos, best score=%s metric=%s",
                            res["tested_combos"],
                            res["best"]["score"] if res["best"] else None,
                            opt_metric)
            except Exception:
                logger.exception("bond optimize failed")
                _bond_opt_state["result"] = {"error": "optimization failed"}
            finally:
                with _bond_opt_lock:
                    _bond_opt_state["running"] = False

        threading.Thread(target=_run, daemon=True, name="bond-optimize").start()
        return jsonify({"status": "started"})

    @app.route("/api/bonds/optimize/status")
    def api_bonds_optimize_status():
        return jsonify({"running": _bond_opt_state["running"],
                        "progress": _bond_opt_state["progress"],
                        "result": _bond_opt_state["result"]})

    # ──────────────────────────────────────────────────────────────────────
    #  Market-making (بازارگردانی) — stocks بورس/فرابورس
    # ──────────────────────────────────────────────────────────────────────

    def _mm_params_from(getter):
        """Build MMParams from a param getter (request.args.get or body.get)."""
        from mm_backtest import MMParams

        def _f(name, default):
            v = getter(name, None)
            try:
                return float(v) if v not in (None, "") else default
            except (ValueError, TypeError):
                return default

        def _i(name, default):
            return int(_f(name, default))

        def _b(name, default):
            v = getter(name, None)
            if v in (None, ""):
                return default
            return str(v) == "1" or str(v).lower() == "true"

        return MMParams(
            cash=_f("cash", 10_000_000_000.0),
            initial_inventory=_i("initial_inventory", 0),
            target_inventory=_i("target_inventory", 0),
            quote_spread_pct=_f("quote_spread_pct", 0.015),
            max_spread_pct=_f("max_spread_pct", 0.02),
            order_volume=_i("order_volume", 10_000),
            price_band_pct=_f("price_band_pct", 0.05),
            ref_mode=str(getter("ref_mode", None) or "prev_close"),
            tick_size=_f("tick_size", 1.0),
            tick_pct=_f("tick_pct", 0.0),
            inventory_floor=_i("inventory_floor", -1_000_000),
            inventory_ceiling=_i("inventory_ceiling", 1_000_000),
            skew_pct_per_unit=_f("skew_pct_per_unit", 0.0),
            buy_fee=_f("buy_fee", 0.0005),
            sell_fee=_f("sell_fee", 0.0005),
            center_mode=str(getter("center_mode", None) or "mid"),
            flatten_eod=_b("flatten_eod", False),
            relieve_queue=_b("relieve_queue", True),
        )

    @app.route("/api/mm/symbols")
    def api_mm_symbols():
        """List instruments. ?watch=1 → watchlist only; ?market=/?type= filter."""
        try:
            watch_only = request.args.get("watch", "0") == "1"
            market = request.args.get("market") or None
            itype = request.args.get("type") or None
            rows = db.get_instruments(market=market, itype=itype,
                                      watch_only=watch_only)
        except Exception as e:
            logger.exception("mm symbols failed")
            return jsonify({"error": str(e)}), 500
        # also report which watchlist symbols actually have OB data collected
        for r in rows:
            try:
                r["ob_days"] = len(db.get_ob_dates(r["symbol"]))
            except Exception:
                r["ob_days"] = 0
        return jsonify({"instruments": rows, "count": len(rows)})

    @app.route("/api/mm/watch", methods=["POST"])
    def api_mm_watch():
        """Toggle watchlist membership: {symbols:[...], watch:true|false}."""
        body = request.get_json(silent=True) or {}
        syms = [s for s in (body.get("symbols") or []) if s]
        watch = bool(body.get("watch", True))
        try:
            n = db.set_instrument_watch(syms, watch)
        except Exception as e:
            logger.exception("mm watch failed")
            return jsonify({"error": str(e)}), 500
        return jsonify({"updated": n, "watch": watch})

    @app.route("/api/mm/backtest")
    def api_mm_backtest():
        """Market-making backtest for one stock symbol."""
        from mm_backtest import run_mm_backtest
        symbol = (request.args.get("symbol") or "").strip()
        if not symbol:
            return jsonify({"error": "symbol required"}), 400

        def _int(name):
            v = request.args.get(name, "")
            return int(v) if v else None

        params = _mm_params_from(request.args.get)
        try:
            result = run_mm_backtest(db, symbol, _int("start"), _int("end"), params)
        except Exception as e:
            logger.exception("mm backtest failed")
            return jsonify({"error": str(e)}), 500
        return jsonify(result)

    _mm_opt_state = {"running": False, "progress": {}, "result": None}
    _mm_opt_lock = threading.Lock()

    @app.route("/api/mm/optimize", methods=["GET", "POST"])
    def api_mm_optimize():
        from mm_backtest import optimize_mm_backtest
        body = request.get_json(silent=True) or {}

        def _get(name, default=None):
            if name in body:
                return body[name]
            return request.args.get(name, default)

        symbol = str(_get("symbol", "") or "").strip()
        if not symbol:
            return jsonify({"error": "symbol required"}), 400
        base = _mm_params_from(_get)
        opt_metric = str(_get("opt_metric", "sharpe"))
        try:
            min_presence = float(_get("min_presence_pct", 50.0))
        except (ValueError, TypeError):
            min_presence = 50.0
        try:
            n_jobs = int(float(_get("n_jobs", 0)))
        except (ValueError, TypeError):
            n_jobs = 0

        def _int(name):
            v = _get(name)
            try:
                return int(v) if v not in (None, "") else None
            except (ValueError, TypeError):
                return None
        start, end = _int("start"), _int("end")

        if _mm_opt_state["running"]:
            return jsonify({"status": "already running",
                            "progress": _mm_opt_state["progress"]}), 409

        def _run():
            with _mm_opt_lock:
                _mm_opt_state["running"] = True
                _mm_opt_state["progress"] = {"done": 0, "total": 0, "phase": "grid"}
                _mm_opt_state["result"] = None
            try:
                res = optimize_mm_backtest(
                    db, symbol, start, end, base=base, opt_metric=opt_metric,
                    min_presence_pct=min_presence,
                    progress=_mm_opt_state["progress"],
                    progress_lock=_mm_opt_lock, n_jobs=n_jobs)
                _mm_opt_state["result"] = res
                logger.info("[MMOptimize] %s: %d combos, best=%s",
                            symbol, res.get("tested_combos", 0),
                            res["best"]["score"] if res.get("best") else None)
            except Exception:
                logger.exception("mm optimize failed")
                _mm_opt_state["result"] = {"error": "optimization failed"}
            finally:
                with _mm_opt_lock:
                    _mm_opt_state["running"] = False

        threading.Thread(target=_run, daemon=True, name="mm-optimize").start()
        return jsonify({"status": "started"})

    @app.route("/api/mm/optimize/status")
    def api_mm_optimize_status():
        return jsonify({"running": _mm_opt_state["running"],
                        "progress": _mm_opt_state["progress"],
                        "result": _mm_opt_state["result"]})

    # ──────────────────────────────────────────────────────────────────────
    #  Options arbitrage (آربیتراژ اختيار معامله)
    # ──────────────────────────────────────────────────────────────────────

    def _opt_params_from(getter):
        from options_backtest import OptionsParams

        def _f(name, default):
            v = getter(name, None)
            try:
                return float(v) if v not in (None, "") else default
            except (ValueError, TypeError):
                return default

        def _i(name, default):
            return int(_f(name, default))

        def _b(name, default):
            v = getter(name, None)
            if v in (None, ""):
                return default
            return str(v) == "1" or str(v).lower() == "true"

        return OptionsParams(
            capital=_f("capital", 100_000_000_000.0),
            max_capital_per_trade=_f("max_capital_per_trade", 10_000_000_000.0),
            annual_rate=_f("annual_rate", 0.30),
            min_edge_ann_pct=_f("min_edge_ann_pct", 5.0),
            do_conversion=_b("do_conversion", True),
            do_reversal=_b("do_reversal", False),
            do_box=_b("do_box", True),
            allow_short=_b("allow_short", False),
            opt_fee=_f("opt_fee", 0.0005),
            stock_fee=_f("stock_fee", 0.0037),
            exercise_fee=_f("exercise_fee", 0.0005),
            step_secs=_i("step_secs", 60),
            quote_max_age_secs=_i("quote_max_age_secs", 600),
            min_days_to_expiry=_i("min_days_to_expiry", 1),
        )

    @app.route("/api/opt/underlyings")
    def api_opt_underlyings():
        """Underlyings that have option chains, with chain + OB-coverage counts."""
        try:
            rows = db.get_option_underlyings()
        except Exception as e:
            logger.exception("opt underlyings failed")
            return jsonify({"error": str(e)}), 500
        for r in rows:
            try:
                r["ob_days"] = len(db.get_ob_dates(r["underlying"]))
            except Exception:
                r["ob_days"] = 0
        return jsonify({"underlyings": rows, "count": len(rows)})

    @app.route("/api/opt/backtest")
    def api_opt_backtest():
        from options_backtest import run_options_backtest
        underlying = (request.args.get("underlying") or "").strip()
        if not underlying:
            return jsonify({"error": "underlying required"}), 400

        def _int(name):
            v = request.args.get(name, "")
            return int(v) if v else None

        params = _opt_params_from(request.args.get)
        try:
            result = run_options_backtest(db, underlying, _int("start"),
                                          _int("end"), params)
        except Exception as e:
            logger.exception("opt backtest failed")
            return jsonify({"error": str(e)}), 500
        return jsonify(result)

    @app.route("/api/opt/live")
    def api_opt_live():
        """اسکنِ لحظه‌ای آربیتراژِ آپشن با قیمت‌های زنده از TSETMC BestLimits."""
        from options_backtest import scan_options_live
        from data_fetcher import TSETMCFetcher
        underlying = (request.args.get("underlying") or "").strip()
        if not underlying:
            return jsonify({"error": "underlying required"}), 400
        params = _opt_params_from(request.args.get)
        try:
            result = scan_options_live(db, TSETMCFetcher(), underlying, params)
        except Exception as e:
            logger.exception("opt live scan failed")
            return jsonify({"error": str(e)}), 500
        return jsonify(result)

    _opt_opt_state = {"running": False, "progress": {}, "result": None}
    _opt_opt_lock = threading.Lock()

    @app.route("/api/opt/optimize", methods=["GET", "POST"])
    def api_opt_optimize():
        from options_backtest import optimize_options_backtest
        body = request.get_json(silent=True) or {}

        def _get(name, default=None):
            if name in body:
                return body[name]
            return request.args.get(name, default)

        underlying = str(_get("underlying", "") or "").strip()
        if not underlying:
            return jsonify({"error": "underlying required"}), 400
        base = _opt_params_from(_get)
        opt_metric = str(_get("opt_metric", "total_profit"))
        try:
            n_jobs = int(float(_get("n_jobs", 0)))
        except (ValueError, TypeError):
            n_jobs = 0

        def _int(name):
            v = _get(name)
            try:
                return int(v) if v not in (None, "") else None
            except (ValueError, TypeError):
                return None
        start, end = _int("start"), _int("end")

        if _opt_opt_state["running"]:
            return jsonify({"status": "already running",
                            "progress": _opt_opt_state["progress"]}), 409

        def _run():
            with _opt_opt_lock:
                _opt_opt_state["running"] = True
                _opt_opt_state["progress"] = {"done": 0, "total": 0, "phase": "grid"}
                _opt_opt_state["result"] = None
            try:
                res = optimize_options_backtest(
                    db, underlying, start, end, base=base, opt_metric=opt_metric,
                    progress=_opt_opt_state["progress"],
                    progress_lock=_opt_opt_lock, n_jobs=n_jobs)
                _opt_opt_state["result"] = res
            except Exception:
                logger.exception("opt optimize failed")
                _opt_opt_state["result"] = {"error": "optimization failed"}
            finally:
                with _opt_opt_lock:
                    _opt_opt_state["running"] = False

        threading.Thread(target=_run, daemon=True, name="opt-optimize").start()
        return jsonify({"status": "started"})

    @app.route("/api/opt/optimize/status")
    def api_opt_optimize_status():
        return jsonify({"running": _opt_opt_state["running"],
                        "progress": _opt_opt_state["progress"],
                        "result": _opt_opt_state["result"]})

    # ──────────────────────────────────────────────────────────────────────
    #  Data coverage + on-demand collection (funds + اخزا)
    # ──────────────────────────────────────────────────────────────────────

    def _coverage_rows(kind: str, cov: dict) -> list[dict]:
        """Build coverage rows for *kind* ("funds" | "bonds")."""
        if kind == "bonds":
            try:
                series = db.get_bond_series(active_only=False)
            except Exception:
                series = []
            entries = [{"symbol": s.get("symbol", ""),
                        "name": s.get("name", s.get("symbol", "")),
                        "ins_code": (s.get("ins_code") or "").strip(),
                        "maturity_date": s.get("maturity_date", 0),
                        "active": bool(s.get("active", 1))}
                       for s in series]
        else:
            from config import FIXED_INCOME_ETFS
            entries = [{"symbol": f.get("symbol", ""),
                        "name": f.get("name", f.get("symbol", "")),
                        "ins_code": (f.get("ins_code") or "").strip(),
                        "maturity_date": 0, "active": True}
                       for f in FIXED_INCOME_ETFS]

        rows = []
        for e in entries:
            c = cov.get(e["symbol"], {})
            rows.append({**e,
                         "daily":     c.get("daily"),
                         "ticks":     c.get("ticks"),
                         "ob":        c.get("ob"),
                         "snapshots": c.get("snapshots"),
                         "client":    c.get("client")})
        return rows

    @app.route("/api/data_coverage")
    def api_data_coverage():
        """Per-symbol data completeness for funds and اخزا.

        Returns {today, funds:[…], bonds:[…]} where each row carries the
        symbol's coverage of every time-series table (daily/ticks/ob/snapshots).
        """
        cov = db.coverage_summary()
        today_int = int(datetime.now().strftime("%Y%m%d"))
        return jsonify({
            "today": today_int,
            "funds": _coverage_rows("funds", cov),
            "bonds": _coverage_rows("bonds", cov),
        })

    # Guard + progress state for on-demand collection (network-heavy).
    _collect_state = {"running": False, "progress": {}, "last": None}
    _collect_lock = threading.Lock()

    @app.route("/api/collect", methods=["POST"])
    def api_collect():
        """Collect history for selected symbols (funds or اخزا).

        JSON body:
          type      – "funds" | "bonds"  (default "funds")
          symbols   – list of symbols to collect (required; [] = all of type)
          days      – recent trading days of tick/OB (default 30)
          daily     – days of daily OHLCV (default 365)
          full      – force full daily re-download (default false)
          no_ob / no_intraday – skip those passes
          async     – run in background, return immediately (default true)
        """
        from collector import collect_history
        from data_fetcher import TSETMCFetcher

        body = request.get_json(silent=True) or {}
        kind = body.get("type", "funds")
        wanted = set(body.get("symbols") or [])

        # Resolve targets (symbol + ins_code) for the requested type.
        rows = _coverage_rows(kind, {})
        targets = [{"symbol": r["symbol"], "ins_code": r["ins_code"]}
                   for r in rows
                   if r["ins_code"] and (not wanted or r["symbol"] in wanted)]

        if not targets:
            return jsonify({"error": "no collectable symbols "
                            "(need a discovered ins_code)"}), 400

        kwargs = dict(
            force_full=bool(body.get("full", False)),
            daily_days=int(body.get("daily", 365) or 365),
            intraday_days=int(body.get("days", 30) or 30),
            fetch_intraday=not body.get("no_intraday", False),
            fetch_ob=not body.get("no_ob", False),
            workers=int(body.get("workers", 0)) or None,
        )

        if _collect_state["running"]:
            return jsonify({"status": "already running",
                            "progress": _collect_state["progress"]}), 409

        def _run():
            with _collect_lock:
                _collect_state["running"] = True
                _collect_state["progress"] = {"done": 0, "total": len(targets),
                                              "current": ""}
            try:
                fetcher = TSETMCFetcher()
                summary = collect_history(
                    db, fetcher, targets,
                    progress=_collect_state["progress"],
                    progress_lock=_collect_lock, **kwargs)
                summary["type"] = kind
                _collect_state["last"] = summary
                logger.info("Collection done (%s): %s", kind, summary)
            except Exception:
                logger.exception("collection failed")
            finally:
                with _collect_lock:
                    _collect_state["running"] = False

        if body.get("async", True):
            threading.Thread(target=_run, daemon=True, name="collect").start()
            return jsonify({"status": "started", "type": kind,
                            "count": len(targets)})

        # Synchronous
        try:
            fetcher = TSETMCFetcher()
            summary = collect_history(db, fetcher, targets, **kwargs)
            summary["type"] = kind
            _collect_state["last"] = summary
            return jsonify({"status": "done", **summary})
        except Exception as e:
            logger.exception("collection failed")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/collect/status")
    def api_collect_status():
        return jsonify({"running": _collect_state["running"],
                        "progress": _collect_state["progress"],
                        "last": _collect_state["last"]})

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
