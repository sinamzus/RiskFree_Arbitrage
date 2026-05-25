"""SQLite persistence for inter-day arbitrage scanner.

Schema
------
snapshots  — one row per (symbol, scan_timestamp).
             Stores market price, NAV, premium/discount, volume, signal.
             Used for historical charting and mean-reversion analysis.

nav_cache  — one row per (symbol, date).
             NAV is published once per day; this prevents re-fetching the
             same NAV on every intra-day scan.
"""

import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
DB_PATH  = DATA_DIR / "arbitrage.db"

_SCHEMA = """
-- ── Daily OHLCV history ────────────────────────────────────────────────────
-- One row per (symbol, date).  Bootstrapped on first run (365 days back),
-- then updated incrementally — only new dates are inserted.
-- yesterday_price ≈ published NAV for fixed-income ETFs.
CREATE TABLE IF NOT EXISTS daily_history (
    symbol          TEXT    NOT NULL,
    ins_code        TEXT    NOT NULL,
    date            INTEGER NOT NULL,   -- YYYYMMDD int (Gregorian)
    open_price      REAL    DEFAULT 0,
    high_price      REAL    DEFAULT 0,
    low_price       REAL    DEFAULT 0,
    close_price     REAL    DEFAULT 0,
    yesterday_price REAL    DEFAULT 0,  -- ≈ NAV per unit
    volume          INTEGER DEFAULT 0,
    value           REAL    DEFAULT 0,
    trade_count     INTEGER DEFAULT 0,
    price_change    REAL    DEFAULT 0,
    premium_pct     REAL    DEFAULT 0,  -- (close-yesterday)/yesterday*100
    PRIMARY KEY (symbol, date)
);

CREATE INDEX IF NOT EXISTS ix_daily_date ON daily_history(date);
CREATE INDEX IF NOT EXISTS ix_daily_symbol_date ON daily_history(symbol, date);

-- ── Intraday tick trades ───────────────────────────────────────────────────
-- One row per (symbol, date, seq).  Fetched per-date on bootstrap and
-- updated with today's ticks on each scan.
-- time encodes HHMMSS as an integer (e.g. 91530 = 09:15:30).
CREATE TABLE IF NOT EXISTS intraday_trades (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT    NOT NULL,
    ins_code  TEXT    NOT NULL,
    date      INTEGER NOT NULL,   -- YYYYMMDD int
    seq       INTEGER NOT NULL,
    time      INTEGER NOT NULL,   -- HHMMSS int
    price     REAL    NOT NULL,
    volume    INTEGER NOT NULL,
    canceled  INTEGER DEFAULT 0,
    UNIQUE (symbol, date, seq)
);

CREATE INDEX IF NOT EXISTS ix_intraday_symbol_date ON intraday_trades(symbol, date);

-- ── Intraday order-book snapshots ─────────────────────────────────────────
-- One row per (symbol, date, time).  Captured at every scanner tick.
-- Stores top-5 bid/ask levels so tradability can be analysed offline.
CREATE TABLE IF NOT EXISTS intraday_orderbook (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    ins_code    TEXT    NOT NULL,
    date        INTEGER NOT NULL,   -- YYYYMMDD
    time        INTEGER NOT NULL,   -- HHMMSS
    -- 5 bid levels (buy queue, sorted best=highest first)
    bid1_price  REAL    DEFAULT 0,  bid1_vol INTEGER DEFAULT 0,  bid1_cnt INTEGER DEFAULT 0,
    bid2_price  REAL    DEFAULT 0,  bid2_vol INTEGER DEFAULT 0,  bid2_cnt INTEGER DEFAULT 0,
    bid3_price  REAL    DEFAULT 0,  bid3_vol INTEGER DEFAULT 0,  bid3_cnt INTEGER DEFAULT 0,
    bid4_price  REAL    DEFAULT 0,  bid4_vol INTEGER DEFAULT 0,  bid4_cnt INTEGER DEFAULT 0,
    bid5_price  REAL    DEFAULT 0,  bid5_vol INTEGER DEFAULT 0,  bid5_cnt INTEGER DEFAULT 0,
    -- 5 ask levels (sell queue, sorted best=lowest first)
    ask1_price  REAL    DEFAULT 0,  ask1_vol INTEGER DEFAULT 0,  ask1_cnt INTEGER DEFAULT 0,
    ask2_price  REAL    DEFAULT 0,  ask2_vol INTEGER DEFAULT 0,  ask2_cnt INTEGER DEFAULT 0,
    ask3_price  REAL    DEFAULT 0,  ask3_vol INTEGER DEFAULT 0,  ask3_cnt INTEGER DEFAULT 0,
    ask4_price  REAL    DEFAULT 0,  ask4_vol INTEGER DEFAULT 0,  ask4_cnt INTEGER DEFAULT 0,
    ask5_price  REAL    DEFAULT 0,  ask5_vol INTEGER DEFAULT 0,  ask5_cnt INTEGER DEFAULT 0,
    -- Pre-computed summary metrics
    spread_pct  REAL    DEFAULT 0,   -- (ask1 - bid1) / mid × 100
    bid_depth   INTEGER DEFAULT 0,   -- sum of bid1..bid5 volume
    ask_depth   INTEGER DEFAULT 0,   -- sum of ask1..ask5 volume
    nav         REAL    DEFAULT 0,   -- cancel_nav at the time of this snapshot
    UNIQUE (symbol, date, time)
);

CREATE INDEX IF NOT EXISTS ix_ob_symbol_date ON intraday_orderbook(symbol, date);

-- ── Scan snapshots ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS snapshots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol               TEXT    NOT NULL,
    name                 TEXT    DEFAULT '',
    scanned_at           TEXT    NOT NULL,   -- "YYYY-MM-DD HH:MM:SS"
    date                 TEXT    NOT NULL,   -- "YYYY-MM-DD"
    market_price         REAL    DEFAULT 0,
    nav                  REAL    DEFAULT 0,
    issue_nav            REAL    DEFAULT 0,
    cancel_nav           REAL    DEFAULT 0,
    statistical_nav      REAL    DEFAULT 0,
    premium_discount_pct REAL    DEFAULT 0,
    net_profit_pct       REAL    DEFAULT 0,
    volume               INTEGER DEFAULT 0,
    value                REAL    DEFAULT 0,
    trade_count          INTEGER DEFAULT 0,
    best_bid             REAL    DEFAULT 0,
    best_ask             REAL    DEFAULT 0,
    signal               TEXT    DEFAULT 'HOLD',
    actionable           INTEGER DEFAULT 0,
    nav_source           TEXT    DEFAULT '',
    -- intraday context (computed from tick data during scan)
    intraday_trend       TEXT    DEFAULT '',   -- WIDENING | NARROWING | STABLE | ''
    trend_slope          REAL    DEFAULT 0,    -- %/tick
    vwap                 REAL    DEFAULT 0,
    vwap_premium_pct     REAL    DEFAULT 0,
    tick_count_today     INTEGER DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS uix_symbol_scanned
    ON snapshots(symbol, scanned_at);

CREATE INDEX IF NOT EXISTS ix_symbol_date
    ON snapshots(symbol, date);

CREATE INDEX IF NOT EXISTS ix_date
    ON snapshots(date);

-- NAV cache: reuse today's NAV without re-hitting FIPIRAN/Rahavard
CREATE TABLE IF NOT EXISTS nav_cache (
    symbol               TEXT NOT NULL,
    date                 TEXT NOT NULL,   -- "YYYY-MM-DD"
    nav                  REAL DEFAULT 0,
    issue_nav            REAL DEFAULT 0,
    cancel_nav           REAL DEFAULT 0,
    statistical_nav      REAL DEFAULT 0,
    nav_source           TEXT DEFAULT '',
    PRIMARY KEY (symbol, date)
);
"""


class Database:
    """Thin wrapper around a SQLite database for arbitrage history."""

    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self):
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            # ── migrate intraday_orderbook: add nav column if absent ──────────
            existing_ob = {row[1] for row in conn.execute("PRAGMA table_info(intraday_orderbook)").fetchall()}
            if "nav" not in existing_ob:
                conn.execute("ALTER TABLE intraday_orderbook ADD COLUMN nav REAL DEFAULT 0")
                logger.debug("Migrated intraday_orderbook: added column nav")
            # ── migrate existing DBs: add columns if absent ───────────────────
            existing_snap = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)").fetchall()}
            for col, defn in [
                ("intraday_trend",    "TEXT    DEFAULT ''"),
                ("trend_slope",       "REAL    DEFAULT 0"),
                ("vwap",              "REAL    DEFAULT 0"),
                ("vwap_premium_pct",  "REAL    DEFAULT 0"),
                ("tick_count_today",  "INTEGER DEFAULT 0"),
                # order-book tradability columns
                ("tradable",          "INTEGER DEFAULT 0"),
                ("tradable_volume",   "INTEGER DEFAULT 0"),
                ("tradable_value",    "REAL    DEFAULT 0"),
                ("spread_pct",        "REAL    DEFAULT 0"),
                ("ob_score",          "REAL    DEFAULT 0"),
                ("tradability_reason","TEXT    DEFAULT ''"),
            ]:
                if col not in existing_snap:
                    conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {defn}")
                    logger.debug("Migrated snapshots: added column %s", col)
        logger.debug("Database initialised at %s", self.path)

    # ------------------------------------------------------------------ #
    #  Write                                                               #
    # ------------------------------------------------------------------ #

    def save_scan(self, opportunities: list, scanned_at: Optional[datetime] = None):
        """Persist a list of ArbitrageOpportunity objects as a batch."""
        from arbitrage import ArbitrageOpportunity  # avoid circular at module level
        if scanned_at is None:
            scanned_at = datetime.now()

        ts  = scanned_at.strftime("%Y-%m-%d %H:%M:%S")
        day = scanned_at.strftime("%Y-%m-%d")

        rows = []
        for o in opportunities:
            ctx = getattr(o, "intraday", None)
            rows.append((
                o.symbol, o.name, ts, day,
                o.market_price, o.nav, o.issue_nav, o.cancel_nav,
                o.statistical_nav, o.premium_discount_pct, o.net_profit_pct,
                o.volume, o.value, o.trade_count,
                o.best_bid, o.best_ask, o.signal,
                1 if o.actionable else 0,
                "",  # nav_source
                # intraday context (None-safe)
                ctx.trend_label      if ctx else "",
                ctx.trend_slope      if ctx else 0.0,
                ctx.vwap             if ctx else 0.0,
                ctx.vwap_premium_pct if ctx else 0.0,
                ctx.tick_count       if ctx else 0,
                # order-book tradability
                1 if getattr(o, "tradable", False) else 0,
                getattr(o, "tradable_volume", 0),
                getattr(o, "tradable_value",  0.0),
                getattr(o, "spread_pct",      0.0),
                getattr(o, "ob_score",        0.0),
                getattr(o, "tradability_reason", ""),
            ))

        with self._conn() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO snapshots
                   (symbol, name, scanned_at, date,
                    market_price, nav, issue_nav, cancel_nav,
                    statistical_nav, premium_discount_pct, net_profit_pct,
                    volume, value, trade_count,
                    best_bid, best_ask, signal, actionable, nav_source,
                    intraday_trend, trend_slope, vwap, vwap_premium_pct, tick_count_today,
                    tradable, tradable_volume, tradable_value,
                    spread_pct, ob_score, tradability_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )

        logger.info("Saved %d fund snapshots at %s", len(rows), ts)

    def cache_nav(self, symbol: str, nav_data: dict, nav_date: Optional[str] = None):
        """Store today's NAV so it's not re-fetched on the next intra-day scan."""
        today = nav_date or date.today().strftime("%Y-%m-%d")
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO nav_cache
                   (symbol, date, nav, issue_nav, cancel_nav,
                    statistical_nav, nav_source)
                   VALUES (?,?,?,?,?,?,?)""",
                (symbol, today,
                 nav_data.get("nav_per_unit", 0),
                 nav_data.get("issue_nav", 0),
                 nav_data.get("cancel_nav", 0),
                 nav_data.get("statistical_nav", 0),
                 nav_data.get("source", "")),
            )

    # ------------------------------------------------------------------ #
    #  Read                                                                #
    # ------------------------------------------------------------------ #

    def get_cached_nav(self, symbol: str,
                       nav_date: Optional[str] = None) -> Optional[dict]:
        """Return cached NAV for *symbol* on *nav_date* (default: today)."""
        today = nav_date or date.today().strftime("%Y-%m-%d")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM nav_cache WHERE symbol=? AND date=?",
                (symbol, today),
            ).fetchone()
        if row and row["nav"] > 0:
            return {
                "nav_per_unit":    row["nav"],
                "issue_nav":       row["issue_nav"],
                "cancel_nav":      row["cancel_nav"],
                "statistical_nav": row["statistical_nav"],
                "nav_date":        row["date"],
                "source":          row["nav_source"] + " (cached)",
                "total_nav":       0,
                "fund_units":      0,
            }
        return None

    def get_symbols(self) -> list[str]:
        """Return all distinct symbols stored in the DB."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM snapshots ORDER BY symbol"
            ).fetchall()
        return [r["symbol"] for r in rows]

    def get_history(self, symbol: str, days: int = 30) -> list[dict]:
        """Return time-series data for *symbol* over the last *days* days."""
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT scanned_at, market_price, nav, issue_nav, cancel_nav,
                          statistical_nav, premium_discount_pct, net_profit_pct,
                          volume, signal, actionable
                   FROM snapshots
                   WHERE symbol=? AND scanned_at >= ?
                   ORDER BY scanned_at ASC""",
                (symbol, since),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_latest(self) -> list[dict]:
        """Return the most recent snapshot for every symbol."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT s.*
                   FROM snapshots s
                   INNER JOIN (
                       SELECT symbol, MAX(scanned_at) AS latest
                       FROM snapshots GROUP BY symbol
                   ) m ON s.symbol=m.symbol AND s.scanned_at=m.latest
                   ORDER BY ABS(s.premium_discount_pct) DESC""",
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self, symbol: str, days: int = 30) -> dict:
        """Compute mean-reversion statistics for *symbol*."""
        rows = self.get_history(symbol, days)
        if not rows:
            return {"mean": 0, "std": 0, "count": 0,
                    "actionable_count": 0, "current": None}

        pds = [r["premium_discount_pct"] for r in rows
               if r["premium_discount_pct"] != 0]
        if not pds:
            return {"mean": 0, "std": 0, "count": 0,
                    "actionable_count": 0, "current": None}

        n = len(pds)
        mean = sum(pds) / n
        variance = sum((x - mean) ** 2 for x in pds) / n
        std = variance ** 0.5

        current = rows[-1]["premium_discount_pct"]
        z_score = (current - mean) / std if std > 0 else 0

        actionable_count = sum(1 for r in rows if r["actionable"])

        return {
            "mean":             round(mean, 4),
            "std":              round(std, 4),
            "z_score":          round(z_score, 2),
            "current":          round(current, 4),
            "count":            n,
            "actionable_count": actionable_count,
            "days":             days,
        }

    def get_all_stats(self, days: int = 30) -> dict[str, dict]:
        """Return stats for every symbol in the DB."""
        return {s: self.get_stats(s, days) for s in self.get_symbols()}

    def last_scan_time(self) -> Optional[str]:
        """Return the timestamp of the most recent scan."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(scanned_at) AS t FROM snapshots"
            ).fetchone()
        return row["t"] if row else None

    # ------------------------------------------------------------------ #
    #  Daily history                                                       #
    # ------------------------------------------------------------------ #

    def get_last_daily_date(self, symbol: str) -> Optional[int]:
        """Return the most-recent date (YYYYMMDD int) stored for *symbol*, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM daily_history WHERE symbol=?",
                (symbol,),
            ).fetchone()
        return int(row["d"]) if row and row["d"] else None

    def save_daily_history(self, symbol: str, ins_code: str,
                           entries: list[dict]) -> int:
        """Bulk-insert daily OHLCV rows for *symbol*.  Skips existing dates.

        Returns the number of new rows inserted.
        """
        if not entries:
            return 0
        rows = [
            (
                symbol, ins_code,
                e["date"], e["open_price"], e["high_price"], e["low_price"],
                e["close_price"], e["yesterday_price"],
                e["volume"], e["value"], e["trade_count"],
                e["price_change"], e["premium_pct"],
            )
            for e in entries
        ]
        with self._conn() as conn:
            cur = conn.executemany(
                """INSERT OR IGNORE INTO daily_history
                   (symbol, ins_code, date, open_price, high_price, low_price,
                    close_price, yesterday_price, volume, value, trade_count,
                    price_change, premium_pct)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            inserted = cur.rowcount
        logger.debug("save_daily_history: %d new rows for %s", inserted, symbol)
        return inserted

    def get_daily_history(self, symbol: str, days: int = 365) -> list[dict]:
        """Return daily OHLCV rows for *symbol* (most recent *days* rows)."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM daily_history
                   WHERE symbol=?
                   ORDER BY date DESC LIMIT ?""",
                (symbol, days),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]   # ascending by date

    def get_all_daily_history(self, days: int = 90) -> dict[str, list[dict]]:
        """Return daily history for every symbol — keyed by symbol."""
        symbols = self.get_symbols()
        # Also pull from daily_history which may have symbols not yet in snapshots
        with self._conn() as conn:
            extra = conn.execute(
                "SELECT DISTINCT symbol FROM daily_history"
            ).fetchall()
        all_syms = list(set(symbols) | {r["symbol"] for r in extra})
        return {s: self.get_daily_history(s, days) for s in all_syms}

    # ------------------------------------------------------------------ #
    #  Intraday trades                                                     #
    # ------------------------------------------------------------------ #

    def get_last_intraday_date(self, symbol: str) -> Optional[int]:
        """Return the most-recent date (YYYYMMDD) with intraday data, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM intraday_trades WHERE symbol=?",
                (symbol,),
            ).fetchone()
        return int(row["d"]) if row and row["d"] else None

    def save_intraday_trades(self, symbol: str, ins_code: str,
                             date_int: int, trades: list[dict]) -> int:
        """Bulk-insert intraday tick rows.  Skips duplicates (same seq on same date).

        Returns the number of new rows inserted.
        """
        if not trades:
            return 0
        rows = [
            (symbol, ins_code, date_int,
             t["seq"], t["time"], t["price"], t["volume"], t["canceled"])
            for t in trades
        ]
        with self._conn() as conn:
            cur = conn.executemany(
                """INSERT OR IGNORE INTO intraday_trades
                   (symbol, ins_code, date, seq, time, price, volume, canceled)
                   VALUES (?,?,?,?,?,?,?,?)""",
                rows,
            )
            inserted = cur.rowcount
        logger.debug("save_intraday_trades: %d new rows for %s on %s",
                     inserted, symbol, date_int)
        return inserted

    def get_intraday_trades(self, symbol: str, date_int: int) -> list[dict]:
        """Return all intraday ticks for *symbol* on *date_int*, sorted by seq."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT seq, time, price, volume, canceled
                   FROM intraday_trades
                   WHERE symbol=? AND date=?
                   ORDER BY seq ASC""",
                (symbol, date_int),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_intraday_dates(self, symbol: str) -> list[int]:
        """Return sorted list of dates (YYYYMMDD) that have intraday data for *symbol*."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT date FROM intraday_trades WHERE symbol=? ORDER BY date",
                (symbol,),
            ).fetchall()
        return [r["date"] for r in rows]

    # ------------------------------------------------------------------ #
    #  Order-book snapshots                                               #
    # ------------------------------------------------------------------ #

    def save_orderbook_snapshot(self, symbol: str, ins_code: str,
                                date_int: int, time_int: int,
                                order_book: dict,
                                nav: float = 0.0) -> bool:
        """Persist one order-book snapshot.

        *order_book* must be {"bids": [...x5], "asks": [...x5]}
        with each level having keys price, volume, count.
        *nav* is the cancel_nav at the time of this snapshot (optional).
        Duplicate (symbol, date, time) rows are silently ignored.
        Returns True if the row was newly inserted.
        """
        bids = order_book.get("bids", []) or []
        asks = order_book.get("asks", []) or []

        def _lv(lst, i, key):
            try:
                return lst[i].get(key, 0) or 0
            except IndexError:
                return 0

        bid_depth = sum(_lv(bids, i, "volume") for i in range(5))
        ask_depth = sum(_lv(asks, i, "volume") for i in range(5))

        bp1 = _lv(bids, 0, "price")
        ap1 = _lv(asks, 0, "price")
        if bp1 > 0 and ap1 > 0:
            mid = (bp1 + ap1) / 2
            spread_pct = round((ap1 - bp1) / mid * 100, 4) if mid > 0 else 0
        else:
            spread_pct = 0

        with self._conn() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO intraday_orderbook
                   (symbol, ins_code, date, time,
                    bid1_price, bid1_vol, bid1_cnt,
                    bid2_price, bid2_vol, bid2_cnt,
                    bid3_price, bid3_vol, bid3_cnt,
                    bid4_price, bid4_vol, bid4_cnt,
                    bid5_price, bid5_vol, bid5_cnt,
                    ask1_price, ask1_vol, ask1_cnt,
                    ask2_price, ask2_vol, ask2_cnt,
                    ask3_price, ask3_vol, ask3_cnt,
                    ask4_price, ask4_vol, ask4_cnt,
                    ask5_price, ask5_vol, ask5_cnt,
                    spread_pct, bid_depth, ask_depth, nav)
                   VALUES (?,?,?,?,
                           ?,?,?, ?,?,?, ?,?,?, ?,?,?, ?,?,?,
                           ?,?,?, ?,?,?, ?,?,?, ?,?,?, ?,?,?,
                           ?,?,?,?)""",
                (symbol, ins_code, date_int, time_int,
                 _lv(bids,0,"price"), _lv(bids,0,"volume"), _lv(bids,0,"count"),
                 _lv(bids,1,"price"), _lv(bids,1,"volume"), _lv(bids,1,"count"),
                 _lv(bids,2,"price"), _lv(bids,2,"volume"), _lv(bids,2,"count"),
                 _lv(bids,3,"price"), _lv(bids,3,"volume"), _lv(bids,3,"count"),
                 _lv(bids,4,"price"), _lv(bids,4,"volume"), _lv(bids,4,"count"),
                 _lv(asks,0,"price"), _lv(asks,0,"volume"), _lv(asks,0,"count"),
                 _lv(asks,1,"price"), _lv(asks,1,"volume"), _lv(asks,1,"count"),
                 _lv(asks,2,"price"), _lv(asks,2,"volume"), _lv(asks,2,"count"),
                 _lv(asks,3,"price"), _lv(asks,3,"volume"), _lv(asks,3,"count"),
                 _lv(asks,4,"price"), _lv(asks,4,"volume"), _lv(asks,4,"count"),
                 spread_pct, bid_depth, ask_depth, nav or 0.0),
            )
            inserted = cur.rowcount > 0
        return inserted

    def get_latest_orderbook(self, symbol: str) -> Optional[dict]:
        """Return the most-recent order-book snapshot for *symbol*, or None."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM intraday_orderbook
                   WHERE symbol=?
                   ORDER BY date DESC, time DESC
                   LIMIT 1""",
                (symbol,),
            ).fetchone()
        return dict(row) if row else None

    def get_orderbook_history(self, symbol: str,
                              date_int: int,
                              limit: int = 500) -> list[dict]:
        """Return intraday order-book snapshots for *symbol* on *date_int*.

        Returns up to *limit* rows sorted ascending by time.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT date, time,
                          bid1_price, bid1_vol, bid1_cnt,
                          bid2_price, bid2_vol, bid2_cnt,
                          bid3_price, bid3_vol, bid3_cnt,
                          bid4_price, bid4_vol, bid4_cnt,
                          bid5_price, bid5_vol, bid5_cnt,
                          ask1_price, ask1_vol, ask1_cnt,
                          ask2_price, ask2_vol, ask2_cnt,
                          ask3_price, ask3_vol, ask3_cnt,
                          ask4_price, ask4_vol, ask4_cnt,
                          ask5_price, ask5_vol, ask5_cnt,
                          spread_pct, bid_depth, ask_depth
                   FROM intraday_orderbook
                   WHERE symbol=? AND date=?
                   ORDER BY time ASC
                   LIMIT ?""",
                (symbol, date_int, limit),
            ).fetchall()
        return [dict(r) for r in rows]
