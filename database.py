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
    nav_source           TEXT    DEFAULT ''
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
            rows.append((
                o.symbol, o.name, ts, day,
                o.market_price, o.nav, o.issue_nav, o.cancel_nav,
                o.statistical_nav, o.premium_discount_pct, o.net_profit_pct,
                o.volume, o.value, o.trade_count,
                o.best_bid, o.best_ask, o.signal,
                1 if o.actionable else 0,
                "",  # nav_source (not on dataclass; extend if needed)
            ))

        with self._conn() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO snapshots
                   (symbol, name, scanned_at, date,
                    market_price, nav, issue_nav, cancel_nav,
                    statistical_nav, premium_discount_pct, net_profit_pct,
                    volume, value, trade_count,
                    best_bid, best_ask, signal, actionable, nav_source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
