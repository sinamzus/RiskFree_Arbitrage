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
