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

-- ── Raw order-book tick stream (tick-by-tick, for backtesting) ────────────
-- One row per raw TSETMC delta event (ref_id + level).
-- The API returns partial updates: each ref_id touches 1-5 levels.
-- Sort by ref_id to replay and reconstruct the full book at any instant.
-- Fields mirror TSETMC bestLimitsHistory exactly.
CREATE TABLE IF NOT EXISTS ob_ticks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT    NOT NULL,
    ins_code  TEXT    NOT NULL,
    date      INTEGER NOT NULL,   -- YYYYMMDD
    ref_id    INTEGER NOT NULL,   -- TSETMC global event sequence (sort key)
    heven     INTEGER NOT NULL,   -- HHMMSS (time of this event)
    level     INTEGER NOT NULL,   -- 1=best … 5=worst
    bid_price REAL    DEFAULT 0,
    bid_vol   INTEGER DEFAULT 0,
    bid_cnt   INTEGER DEFAULT 0,
    ask_price REAL    DEFAULT 0,
    ask_vol   INTEGER DEFAULT 0,
    ask_cnt   INTEGER DEFAULT 0,
    UNIQUE (symbol, date, ref_id, level)
);
CREATE INDEX IF NOT EXISTS ix_ob_ticks_sym_date   ON ob_ticks(symbol, date);
CREATE INDEX IF NOT EXISTS ix_ob_ticks_sym_ref    ON ob_ticks(symbol, date, ref_id);

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

-- ── Intraday price snapshots (cumulative per-instant from TSETMC) ────────
-- One row per (symbol, date, time).  Fetched from
-- ``ClosingPrice/GetClosingPriceHistory/{insCode}/{date}`` which returns
-- ~3000-6000 rows per trading day (one snapshot every few seconds).
-- Use for historical intraday price/volume charts at sub-minute resolution.
CREATE TABLE IF NOT EXISTS intraday_price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    ins_code    TEXT    NOT NULL,
    date        INTEGER NOT NULL,   -- YYYYMMDD
    time        INTEGER NOT NULL,   -- HHMMSS
    last_price  REAL    DEFAULT 0,  -- pDrCotVal (last traded)
    close_price REAL    DEFAULT 0,  -- pClosing
    trade_count INTEGER DEFAULT 0,  -- zTotTran (cumulative since open)
    cum_volume  INTEGER DEFAULT 0,  -- qTotTran5J (cumulative)
    cum_value   REAL    DEFAULT 0,  -- qTotCap (cumulative)
    UNIQUE (symbol, date, time)
);
CREATE INDEX IF NOT EXISTS ix_iph_sym_date ON intraday_price_history(symbol, date);

-- ── Client type daily aggregate (حقیقی / حقوقی per day) ────────────────────
-- One row per (symbol, date).  From ClientType/GetClientTypeHistory.
-- I = Individual (حقیقی), N = Legal entity (حقوقی).
CREATE TABLE IF NOT EXISTS client_type_daily (
    symbol     TEXT    NOT NULL,
    ins_code   TEXT    NOT NULL,
    date       INTEGER NOT NULL,   -- YYYYMMDD
    buy_i_vol  INTEGER DEFAULT 0,  buy_n_vol  INTEGER DEFAULT 0,
    buy_i_val  REAL    DEFAULT 0,  buy_n_val  REAL    DEFAULT 0,
    buy_i_cnt  INTEGER DEFAULT 0,  buy_n_cnt  INTEGER DEFAULT 0,
    sell_i_vol INTEGER DEFAULT 0,  sell_n_vol INTEGER DEFAULT 0,
    sell_i_val REAL    DEFAULT 0,  sell_n_val REAL    DEFAULT 0,
    sell_i_cnt INTEGER DEFAULT 0,  sell_n_cnt INTEGER DEFAULT 0,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS ix_ctd_date ON client_type_daily(date);

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

-- ── Bond (اوراق بدهی) registry ────────────────────────────────────────────
-- One row per bond series.  Manually seeded + auto-discovered via TSETMC.
-- Zero-coupon bonds (اخزا): face_value=1000000, coupon_rate=0
CREATE TABLE IF NOT EXISTS bond_series (
    symbol        TEXT PRIMARY KEY,
    ins_code      TEXT DEFAULT '',
    name          TEXT DEFAULT '',
    face_value    REAL DEFAULT 1000000,
    maturity_date INTEGER NOT NULL,   -- YYYYMMDD Gregorian
    issue_date    INTEGER DEFAULT 0,  -- YYYYMMDD Gregorian
    coupon_rate   REAL DEFAULT 0.0,   -- 0 = zero-coupon
    active        INTEGER DEFAULT 1,  -- 1 = still trading
    verified      INTEGER DEFAULT 0   -- 1 = ins_code confirmed on TSETMC
);
CREATE INDEX IF NOT EXISTS ix_bond_maturity ON bond_series(maturity_date);

-- ── Bond daily price snapshots ────────────────────────────────────────────
-- One row per (symbol, date). Updated on each scan like daily_history.
CREATE TABLE IF NOT EXISTS bond_prices (
    symbol        TEXT    NOT NULL,
    date          INTEGER NOT NULL,   -- YYYYMMDD
    last_price    REAL    DEFAULT 0,
    close_price   REAL    DEFAULT 0,
    ytm           REAL    DEFAULT 0,  -- yield to maturity (decimal, e.g. 0.28)
    days_to_mat   INTEGER DEFAULT 0,
    volume        INTEGER DEFAULT 0,
    value         REAL    DEFAULT 0,
    trade_count   INTEGER DEFAULT 0,
    curve_ytm     REAL    DEFAULT 0,  -- fitted curve YTM at this maturity
    z_spread_bps  REAL    DEFAULT 0,  -- (ytm - curve_ytm) × 10000
    signal        TEXT    DEFAULT 'HOLD',
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS ix_bp_date ON bond_prices(date);
CREATE INDEX IF NOT EXISTS ix_bp_sym  ON bond_prices(symbol);
"""


class Database:
    """Thin wrapper around a SQLite database for arbitrage history."""

    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _conn(self):
        # timeout: block (instead of raising "database is locked") for up to
        # 30s when another connection holds the write lock — needed because
        # parallel fetch workers write concurrently.
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # WAL lets readers run while a writer is active and greatly reduces
        # lock contention; busy_timeout makes writers wait their turn rather
        # than fail immediately.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
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

        # ── ensure intraday_orderbook exists (executescript can silently skip
        #    new tables when the DB was created with an older schema version) ──
        with self._conn() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "intraday_orderbook" not in tables:
                logger.warning("intraday_orderbook missing — creating explicitly")
                conn.executescript("""
CREATE TABLE IF NOT EXISTS intraday_orderbook (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    ins_code    TEXT    NOT NULL,
    date        INTEGER NOT NULL,
    time        INTEGER NOT NULL,
    bid1_price  REAL DEFAULT 0,  bid1_vol INTEGER DEFAULT 0,  bid1_cnt INTEGER DEFAULT 0,
    bid2_price  REAL DEFAULT 0,  bid2_vol INTEGER DEFAULT 0,  bid2_cnt INTEGER DEFAULT 0,
    bid3_price  REAL DEFAULT 0,  bid3_vol INTEGER DEFAULT 0,  bid3_cnt INTEGER DEFAULT 0,
    bid4_price  REAL DEFAULT 0,  bid4_vol INTEGER DEFAULT 0,  bid4_cnt INTEGER DEFAULT 0,
    bid5_price  REAL DEFAULT 0,  bid5_vol INTEGER DEFAULT 0,  bid5_cnt INTEGER DEFAULT 0,
    ask1_price  REAL DEFAULT 0,  ask1_vol INTEGER DEFAULT 0,  ask1_cnt INTEGER DEFAULT 0,
    ask2_price  REAL DEFAULT 0,  ask2_vol INTEGER DEFAULT 0,  ask2_cnt INTEGER DEFAULT 0,
    ask3_price  REAL DEFAULT 0,  ask3_vol INTEGER DEFAULT 0,  ask3_cnt INTEGER DEFAULT 0,
    ask4_price  REAL DEFAULT 0,  ask4_vol INTEGER DEFAULT 0,  ask4_cnt INTEGER DEFAULT 0,
    ask5_price  REAL DEFAULT 0,  ask5_vol INTEGER DEFAULT 0,  ask5_cnt INTEGER DEFAULT 0,
    spread_pct  REAL DEFAULT 0,
    bid_depth   INTEGER DEFAULT 0,
    ask_depth   INTEGER DEFAULT 0,
    nav         REAL DEFAULT 0,
    UNIQUE (symbol, date, time)
);
CREATE INDEX IF NOT EXISTS ix_ob_symbol_date ON intraday_orderbook(symbol, date);
""")
            else:
                # ── migrate intraday_orderbook: add nav column if absent ────────
                existing_ob = {row[1] for row in conn.execute(
                    "PRAGMA table_info(intraday_orderbook)"
                ).fetchall()}
                if "nav" not in existing_ob:
                    conn.execute(
                        "ALTER TABLE intraday_orderbook ADD COLUMN nav REAL DEFAULT 0"
                    )
                    logger.debug("Migrated intraday_orderbook: added column nav")

            # ── ensure new intraday tables exist (added 2026-06) ─────────────
            if "intraday_price_history" not in tables:
                logger.warning("intraday_price_history missing — creating explicitly")
                conn.executescript("""
CREATE TABLE IF NOT EXISTS intraday_price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    ins_code    TEXT    NOT NULL,
    date        INTEGER NOT NULL,
    time        INTEGER NOT NULL,
    last_price  REAL    DEFAULT 0,
    close_price REAL    DEFAULT 0,
    trade_count INTEGER DEFAULT 0,
    cum_volume  INTEGER DEFAULT 0,
    cum_value   REAL    DEFAULT 0,
    UNIQUE (symbol, date, time)
);
CREATE INDEX IF NOT EXISTS ix_iph_sym_date ON intraday_price_history(symbol, date);
""")
            if "client_type_daily" not in tables:
                logger.warning("client_type_daily missing — creating explicitly")
                conn.executescript("""
CREATE TABLE IF NOT EXISTS client_type_daily (
    symbol     TEXT    NOT NULL,
    ins_code   TEXT    NOT NULL,
    date       INTEGER NOT NULL,
    buy_i_vol  INTEGER DEFAULT 0,  buy_n_vol  INTEGER DEFAULT 0,
    buy_i_val  REAL    DEFAULT 0,  buy_n_val  REAL    DEFAULT 0,
    buy_i_cnt  INTEGER DEFAULT 0,  buy_n_cnt  INTEGER DEFAULT 0,
    sell_i_vol INTEGER DEFAULT 0,  sell_n_vol INTEGER DEFAULT 0,
    sell_i_val REAL    DEFAULT 0,  sell_n_val REAL    DEFAULT 0,
    sell_i_cnt INTEGER DEFAULT 0,  sell_n_cnt INTEGER DEFAULT 0,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS ix_ctd_date ON client_type_daily(date);
""")
            # ── ensure bond tables exist (added 2026-06) ─────────────────────
            if "bond_series" not in tables:
                conn.executescript("""
CREATE TABLE IF NOT EXISTS bond_series (
    symbol TEXT PRIMARY KEY, ins_code TEXT DEFAULT '', name TEXT DEFAULT '',
    face_value REAL DEFAULT 1000000, maturity_date INTEGER NOT NULL,
    issue_date INTEGER DEFAULT 0, coupon_rate REAL DEFAULT 0.0,
    active INTEGER DEFAULT 1, verified INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_bond_maturity ON bond_series(maturity_date);
""")
            if "bond_prices" not in tables:
                conn.executescript("""
CREATE TABLE IF NOT EXISTS bond_prices (
    symbol TEXT NOT NULL, date INTEGER NOT NULL,
    last_price REAL DEFAULT 0, close_price REAL DEFAULT 0,
    ytm REAL DEFAULT 0, days_to_mat INTEGER DEFAULT 0,
    volume INTEGER DEFAULT 0, value REAL DEFAULT 0, trade_count INTEGER DEFAULT 0,
    curve_ytm REAL DEFAULT 0, z_spread_bps REAL DEFAULT 0,
    signal TEXT DEFAULT 'HOLD',
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS ix_bp_date ON bond_prices(date);
CREATE INDEX IF NOT EXISTS ix_bp_sym  ON bond_prices(symbol);
""")
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

    def upsert_today_history(self, symbol: str, ins_code: str,
                             date_int: int, entry: dict) -> None:
        """Insert or REPLACE today's daily OHLCV row.

        Called on every scan so today's bar stays current throughout the day.
        Unlike save_daily_history (INSERT OR IGNORE), this always overwrites
        the same-date row with fresher data.
        """
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO daily_history
                   (symbol, ins_code, date, open_price, high_price, low_price,
                    close_price, yesterday_price, volume, value, trade_count,
                    price_change, premium_pct)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    symbol, ins_code, date_int,
                    entry.get("open_price",      0),
                    entry.get("high_price",      0),
                    entry.get("low_price",       0),
                    entry.get("close_price",     0),
                    entry.get("yesterday_price", 0),
                    entry.get("volume",          0),
                    entry.get("value",           0),
                    entry.get("trade_count",     0),
                    entry.get("price_change",    0),
                    entry.get("premium_pct",     0),
                ),
            )

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

    # ------------------------------------------------------------------ #
    #  Intraday price snapshots (per-second cumulative state)              #
    # ------------------------------------------------------------------ #

    def save_intraday_price_history(self, symbol: str, ins_code: str,
                                    date_int: int, snapshots: list[dict]) -> int:
        """Bulk-insert intraday price snapshots from GetClosingPriceHistory.

        Each snapshot dict has: time, last_price, close_price, trade_count,
        cum_volume, cum_value.  Skips duplicates on (symbol, date, time).
        """
        if not snapshots:
            return 0
        rows = [
            (symbol, ins_code, date_int,
             s["time"], s.get("last_price",  0), s.get("close_price", 0),
             s.get("trade_count", 0), s.get("cum_volume", 0), s.get("cum_value", 0))
            for s in snapshots
        ]
        with self._conn() as conn:
            cur = conn.executemany(
                """INSERT OR IGNORE INTO intraday_price_history
                   (symbol, ins_code, date, time, last_price, close_price,
                    trade_count, cum_volume, cum_value)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            return cur.rowcount

    def get_intraday_price_history(self, symbol: str, date_int: int) -> list[dict]:
        """Return all intraday price snapshots for *symbol* on *date_int*."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT time, last_price, close_price,
                          trade_count, cum_volume, cum_value
                   FROM intraday_price_history
                   WHERE symbol=? AND date=? ORDER BY time ASC""",
                (symbol, date_int),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    #  Client type daily aggregate (حقیقی vs حقوقی)                       #
    # ------------------------------------------------------------------ #

    def save_client_type(self, symbol: str, ins_code: str,
                         date_int: int, ct: dict) -> bool:
        """Upsert client-type daily aggregate for *symbol* on *date_int*."""
        if not ct:
            return False
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO client_type_daily
                   (symbol, ins_code, date,
                    buy_i_vol,  buy_n_vol,  buy_i_val,  buy_n_val,
                    buy_i_cnt,  buy_n_cnt,
                    sell_i_vol, sell_n_vol, sell_i_val, sell_n_val,
                    sell_i_cnt, sell_n_cnt)
                   VALUES (?,?,?, ?,?,?,?, ?,?, ?,?,?,?, ?,?)""",
                (symbol, ins_code, date_int,
                 ct.get("buy_i_vol",  0), ct.get("buy_n_vol",  0),
                 ct.get("buy_i_val",  0), ct.get("buy_n_val",  0),
                 ct.get("buy_i_cnt",  0), ct.get("buy_n_cnt",  0),
                 ct.get("sell_i_vol", 0), ct.get("sell_n_vol", 0),
                 ct.get("sell_i_val", 0), ct.get("sell_n_val", 0),
                 ct.get("sell_i_cnt", 0), ct.get("sell_n_cnt", 0)),
            )
        return True

    def get_client_type(self, symbol: str, date_int: int) -> Optional[dict]:
        """Return client-type aggregate for *symbol* on *date_int*, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM client_type_daily WHERE symbol=? AND date=?",
                (symbol, date_int),
            ).fetchone()
        return dict(row) if row else None

    def get_client_type_range(self, symbol: str,
                              start_date: int, end_date: int) -> list[dict]:
        """Return all client-type rows for *symbol* between dates (inclusive)."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM client_type_daily
                   WHERE symbol=? AND date BETWEEN ? AND ?
                   ORDER BY date ASC""",
                (symbol, start_date, end_date),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    #  Raw OB tick stream                                                 #
    # ------------------------------------------------------------------ #

    def save_ob_ticks(self, symbol: str, ins_code: str,
                      date_int: int, raw_rows: list[dict]) -> int:
        """Persist raw bestLimitsHistory rows for *symbol* on *date_int*.

        *raw_rows* is the unmodified list from the TSETMC API (each dict has
        refID, hEven, number, pMeDem, qTitMeDem, zOrdMeDem, pMeOf, qTitMeOf, zOrdMeOf).
        Duplicate (symbol, date, ref_id, level) rows are silently ignored.
        Returns the number of newly inserted rows.
        """
        if not raw_rows:
            return 0
        inserted = 0
        with self._conn() as conn:
            for row in raw_rows:
                level = row.get("number", 0)
                if not (1 <= level <= 5):
                    continue
                cur = conn.execute(
                    """INSERT OR IGNORE INTO ob_ticks
                       (symbol, ins_code, date, ref_id, heven, level,
                        bid_price, bid_vol, bid_cnt,
                        ask_price, ask_vol, ask_cnt)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (symbol, ins_code, date_int,
                     row.get("refID",      0),
                     row.get("hEven",      0),
                     level,
                     row.get("pMeDem",     0),
                     row.get("qTitMeDem",  0),
                     row.get("zOrdMeDem",  0),
                     row.get("pMeOf",      0),
                     row.get("qTitMeOf",   0),
                     row.get("zOrdMeOf",   0)),
                )
                inserted += cur.rowcount
        return inserted

    def get_ob_tick_dates(self, symbol: str) -> list[int]:
        """Return sorted list of dates that have raw OB ticks for *symbol*."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT date FROM ob_ticks WHERE symbol=? ORDER BY date",
                (symbol,),
            ).fetchall()
        return [r["date"] for r in rows]

    def get_ob_ticks(self, symbol: str, date_int: int) -> list[dict]:
        """Return all raw OB tick rows for *symbol* on *date_int*, sorted by ref_id."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT ref_id, heven, level,
                          bid_price, bid_vol, bid_cnt,
                          ask_price, ask_vol, ask_cnt
                   FROM ob_ticks
                   WHERE symbol=? AND date=?
                   ORDER BY ref_id ASC, level ASC""",
                (symbol, date_int),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_intraday_dates(self, symbol: str) -> list[int]:
        """Return sorted list of dates (YYYYMMDD) that have intraday tick data for *symbol*."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT date FROM intraday_trades WHERE symbol=? ORDER BY date",
                (symbol,),
            ).fetchall()
        return [r["date"] for r in rows]

    def get_ob_dates(self, symbol: str) -> list[int]:
        """Return sorted list of dates (YYYYMMDD) that have OB snapshots for *symbol*."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT date FROM intraday_orderbook WHERE symbol=? ORDER BY date",
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

    def get_ins_code(self, symbol: str) -> Optional[str]:
        """Return the most recent ins_code for *symbol* from daily_history, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT ins_code FROM daily_history "
                "WHERE symbol=? AND ins_code!='' LIMIT 1",
                (symbol,),
            ).fetchone()
        return row["ins_code"] if row else None

    def get_intraday_snapshot_dates(self, symbol: str,
                                    up_to: Optional[int] = None) -> list[int]:
        """Return sorted list of dates with intraday_price_history data for *symbol*.

        If *up_to* is given (YYYYMMDD int) only dates <= up_to are returned.
        """
        with self._conn() as conn:
            if up_to is not None:
                rows = conn.execute(
                    "SELECT DISTINCT date FROM intraday_price_history "
                    "WHERE symbol=? AND date<=? ORDER BY date ASC",
                    (symbol, up_to),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT DISTINCT date FROM intraday_price_history "
                    "WHERE symbol=? ORDER BY date ASC",
                    (symbol,),
                ).fetchall()
        return [r["date"] for r in rows]

    def get_client_type_history(self, symbol: str, days: int = 30,
                                anchor_date: Optional[int] = None) -> list[dict]:
        """Return up to *days* rows from client_type_daily for *symbol*.

        Rows are returned in ascending date order.
        If *anchor_date* is given, only rows with date <= anchor_date are returned.
        """
        with self._conn() as conn:
            if anchor_date is not None:
                rows = conn.execute(
                    "SELECT * FROM client_type_daily "
                    "WHERE symbol=? AND date<=? "
                    "ORDER BY date DESC LIMIT ?",
                    (symbol, anchor_date, days),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM client_type_daily "
                    "WHERE symbol=? ORDER BY date DESC LIMIT ?",
                    (symbol, days),
                ).fetchall()
        return [dict(r) for r in reversed(rows)]

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

    # ------------------------------------------------------------------ #
    #  Bond registry & prices                                             #
    # ------------------------------------------------------------------ #

    def get_bond_series(self, active_only: bool = True) -> list[dict]:
        """Return all bond series, optionally only active ones."""
        with self._conn() as conn:
            if active_only:
                rows = conn.execute(
                    "SELECT * FROM bond_series WHERE active=1 ORDER BY maturity_date ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bond_series ORDER BY maturity_date ASC"
                ).fetchall()
        return [dict(r) for r in rows]

    def upsert_bond_series(self, series: list[dict]) -> int:
        """Insert or replace bond series rows. Returns count upserted."""
        if not series:
            return 0
        rows = [
            (s["symbol"], s.get("ins_code",""), s.get("name",""),
             s.get("face_value", 1_000_000), int(s["maturity_date"]),
             s.get("issue_date", 0), s.get("coupon_rate", 0.0),
             1 if s.get("active", True) else 0,
             1 if s.get("verified", False) else 0)
            for s in series
        ]
        with self._conn() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO bond_series
                   (symbol, ins_code, name, face_value, maturity_date,
                    issue_date, coupon_rate, active, verified)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return len(rows)

    def update_bond_ins_code(self, symbol: str, ins_code: str) -> None:
        """Update ins_code (and mark verified) for a bond series."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE bond_series SET ins_code=?, verified=1 WHERE symbol=?",
                (ins_code, symbol),
            )

    def clear_bond_series(self) -> int:
        """Delete all rows from bond_series. Returns rows deleted.

        Used by /api/bonds/discover to rebuild the registry cleanly from live
        TSETMC data (removing stale placeholders, options and matured bonds).
        """
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM bond_series")
            return cur.rowcount

    def save_bond_prices(self, prices: list[dict]) -> int:
        """Upsert today's bond price + yield snapshot. Returns count."""
        if not prices:
            return 0
        rows = [
            (p["symbol"], p["date"], p.get("last_price", 0), p.get("close_price", 0),
             p.get("ytm", 0), p.get("days_to_mat", 0),
             p.get("volume", 0), p.get("value", 0), p.get("trade_count", 0),
             p.get("curve_ytm", 0), p.get("z_spread_bps", 0),
             p.get("signal", "HOLD"))
            for p in prices
        ]
        with self._conn() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO bond_prices
                   (symbol, date, last_price, close_price, ytm, days_to_mat,
                    volume, value, trade_count, curve_ytm, z_spread_bps, signal)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return len(rows)

    def get_bond_prices(self, date_int: int) -> list[dict]:
        """Return all bond price rows for a given date."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT bp.*, bs.name, bs.face_value, bs.maturity_date,
                          bs.ins_code, bs.coupon_rate
                   FROM bond_prices bp
                   JOIN bond_series bs USING (symbol)
                   WHERE bp.date=?
                   ORDER BY bs.maturity_date ASC""",
                (date_int,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_bond_price_history(self, symbol: str, days: int = 90) -> list[dict]:
        """Return daily price+yield history for a single bond series."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM bond_prices WHERE symbol=?
                   ORDER BY date DESC LIMIT ?""",
                (symbol, days),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
