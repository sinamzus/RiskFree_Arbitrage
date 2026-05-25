"""intraday_context.py — Intraday premium-trend analysis from tick data.

For each trade tick: premium_pct = (tick_price − NAV) / NAV × 100
A linear regression slope over the last *window* ticks shows whether the
premium/discount is widening or narrowing in real time.

Usage
-----
    from intraday_context import compute_intraday_context, IntraydayContext

    ticks = db.get_intraday_trades(symbol, today_int)   # list[dict]
    ctx   = compute_intraday_context(ticks, nav=16_500)
    if ctx:
        print(ctx.trend_label, ctx.trend_slope)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_TICKS_FOR_TREND   = 5    # need at least this many ticks to compute slope
TREND_WINDOW          = 30   # use last N ticks for slope calculation
STABLE_SLOPE_THRESH   = 0.002  # |slope| < this  ➜ "STABLE" (% per tick)


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class IntraydayContext:
    """Intraday premium/discount trend for a single fund on a single day."""

    tick_count: int            # total valid ticks today
    vwap: float                # volume-weighted average price (all ticks)
    vwap_premium_pct: float    # (vwap − nav) / nav × 100

    # Trend computed over the last *trend_ticks* ticks
    trend_slope: float         # Δ premium_pct per tick (+ = widening premium)
    trend_label: str           # "WIDENING" | "NARROWING" | "STABLE" | "UNKNOWN"
    trend_ticks: int           # number of ticks used for trend

    latest_price: float        # price of the most-recent tick
    latest_premium_pct: float  # (latest_price − nav) / nav × 100
    latest_time_int: int       # HHMMSS of latest tick (e.g. 111530 = 11:15:30)

    # ── Signal helpers (read-only, derived) ──────────────────────────────────
    @property
    def is_widening(self) -> bool:
        return self.trend_label == "WIDENING"

    @property
    def is_narrowing(self) -> bool:
        return self.trend_label == "NARROWING"

    @property
    def trend_icon(self) -> str:
        return {"WIDENING": "↗", "NARROWING": "↘", "STABLE": "→",
                "UNKNOWN": "?"}.get(self.trend_label, "?")

    def __str__(self) -> str:
        return (
            f"{self.trend_icon} {self.trend_label}  "
            f"slope={self.trend_slope:+.4f}%/tick  "
            f"latest={self.latest_premium_pct:+.3f}%  "
            f"VWAP_prem={self.vwap_premium_pct:+.3f}%  "
            f"n={self.tick_count}"
        )


# ── Core computation ──────────────────────────────────────────────────────────

def _linreg_slope(y: list[float]) -> float:
    """Return the OLS slope β₁ of y ~ a + β₁·t where t = 0,1,…,n-1."""
    n = len(y)
    if n < 2:
        return 0.0
    sx  = n * (n - 1) / 2          # sum(0..n-1)
    sx2 = n * (n - 1) * (2*n - 1) / 6
    sy  = sum(y)
    sxy = sum(i * v for i, v in enumerate(y))
    denom = n * sx2 - sx * sx
    if abs(denom) < 1e-12:
        return 0.0
    return (n * sxy - sx * sy) / denom


def compute_intraday_context(
    ticks: list[dict],
    nav: float,
    window: int = TREND_WINDOW,
) -> Optional[IntraydayContext]:
    """Compute intraday context from a list of tick dicts.

    Parameters
    ----------
    ticks
        List of dicts as returned by ``Database.get_intraday_trades()``.
        Required keys: ``price`` (float), ``volume`` (int), ``seq`` (int),
        ``time`` (int, HHMMSS).  Cancelled ticks (``canceled==1``) are skipped.
    nav
        Today's NAV per unit (cancel_nav from fipiran/TSETMC).
    window
        Number of most-recent ticks to use for slope calculation.

    Returns
    -------
    IntraydayContext or None if there are fewer than MIN_TICKS_FOR_TREND
    valid ticks or nav ≤ 0.
    """
    if nav <= 0:
        logger.debug("compute_intraday_context: nav ≤ 0, skipping")
        return None

    # Filter cancelled ticks, keep only valid price+volume
    valid = [
        t for t in ticks
        if not t.get("canceled") and t.get("price", 0) > 0 and t.get("volume", 0) > 0
    ]

    if len(valid) < MIN_TICKS_FOR_TREND:
        logger.debug(
            "compute_intraday_context: only %d valid ticks (need %d)",
            len(valid), MIN_TICKS_FOR_TREND,
        )
        return None

    # Sort by seq (ascending) — DB already sorts but be safe
    valid.sort(key=lambda t: t["seq"])

    # ── VWAP ─────────────────────────────────────────────────────────────────
    total_value  = sum(t["price"] * t["volume"] for t in valid)
    total_volume = sum(t["volume"] for t in valid)
    vwap = total_value / total_volume if total_volume > 0 else 0.0
    vwap_premium_pct = (vwap - nav) / nav * 100.0

    # ── Premium series for the trend window ──────────────────────────────────
    window_ticks = valid[-window:]            # last *window* ticks
    premiums = [(t["price"] - nav) / nav * 100.0 for t in window_ticks]

    slope = _linreg_slope(premiums)

    if abs(slope) < STABLE_SLOPE_THRESH:
        label = "STABLE"
    elif slope > 0:
        label = "WIDENING"   # premium rising (or discount shrinking)
    else:
        label = "NARROWING"  # premium falling (or discount deepening)

    # ── Latest tick ──────────────────────────────────────────────────────────
    latest = valid[-1]
    latest_price       = latest["price"]
    latest_premium_pct = (latest_price - nav) / nav * 100.0
    latest_time_int    = latest.get("time", 0)

    return IntraydayContext(
        tick_count         = len(valid),
        vwap               = vwap,
        vwap_premium_pct   = vwap_premium_pct,
        trend_slope        = round(slope, 6),
        trend_label        = label,
        trend_ticks        = len(window_ticks),
        latest_price       = latest_price,
        latest_premium_pct = round(latest_premium_pct, 4),
        latest_time_int    = latest_time_int,
    )


# ── Signal qualifier ──────────────────────────────────────────────────────────

def qualify_signal(
    signal: str,
    ctx: Optional[IntraydayContext],
) -> tuple[str, list[str]]:
    """Given the base signal and intraday context, return a qualified signal
    and a list of supporting/warning reasons.

    Returns
    -------
    (qualified_signal, reasons)
        qualified_signal — one of:
            "BUY"          original BUY confirmed
            "BUY_WEAK"     BUY signal but discount may be reversing
            "SELL"         original SELL confirmed
            "SELL_WEAK"    SELL signal but premium may be fading
            "HOLD"         no intraday conflict (pass-through)
        reasons — human-readable strings to append to the opportunity report
    """
    if ctx is None or signal == "HOLD":
        return signal, []

    reasons: list[str] = []
    qualified = signal

    if signal == "BUY":
        # Discount trade (market price < NAV, premium_pct < 0).
        # slope < 0 → premium declining further → discount DEEPENING → confirms BUY
        # slope > 0 → premium rising toward zero → discount CLOSING   → weakens BUY
        if ctx.is_widening:
            # Premium trending UP = discount is shrinking = reverting toward par
            qualified = "BUY_WEAK"
            reasons.append(
                f"⚠ روند درون‌روزی: تخفیف در حال کاهش است "
                f"(slope={ctx.trend_slope:+.4f}%/tick)"
            )
        elif ctx.is_narrowing:
            # Premium trending DOWN = discount is deepening = favorable entry
            reasons.append(
                f"✓ روند درون‌روزی: تخفیف در حال عمق‌تر شدن است "
                f"(slope={ctx.trend_slope:+.4f}%/tick)"
            )
        else:
            reasons.append(
                f"→ روند درون‌روزی: ثابت (slope={ctx.trend_slope:+.4f}%/tick)"
            )

    elif signal == "SELL":
        # Premium trade (market price > NAV, premium_pct > 0).
        # slope > 0 → premium growing → confirms SELL
        # slope < 0 → premium shrinking → weakens SELL
        if ctx.is_narrowing:
            # Premium trending DOWN = premium is fading = reverting toward par
            qualified = "SELL_WEAK"
            reasons.append(
                f"⚠ روند درون‌روزی: صرف در حال کاهش است "
                f"(slope={ctx.trend_slope:+.4f}%/tick)"
            )
        elif ctx.is_widening:
            # Premium trending UP = premium is growing = favorable entry
            reasons.append(
                f"✓ روند درون‌روزی: صرف در حال رشد است "
                f"(slope={ctx.trend_slope:+.4f}%/tick)"
            )
        else:
            reasons.append(
                f"→ روند درون‌روزی: صرف ثابت (slope={ctx.trend_slope:+.4f}%/tick)"
            )

    # VWAP confirmation
    if ctx.vwap > 0:
        reasons.append(
            f"VWAP={ctx.vwap:,.0f}  صرف_VWAP={ctx.vwap_premium_pct:+.3f}%  "
            f"تیک={ctx.tick_count}"
        )

    return qualified, reasons
