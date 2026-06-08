"""Iranian Fixed-Income Bond (اوراق بدهی) Analysis Module — اسناد خزانه اسلامی (اخزا).

Strategy Overview
-----------------
اخزا (Islamic Treasury Bills / اسناد خزانه اسلامی) are zero-coupon government
instruments issued by the Iranian Treasury and traded on the Tehran Stock
Exchange (TSE/TSETMC).  They have:

  - Face value of 1,000,000 Rials (یک میلیون ریال).
  - Fixed maturity date; no periodic coupons (zero-coupon).
  - Price determined by the secondary market; discount to face value implies a yield.

Arbitrage / Relative-Value Signal
-----------------------------------
Because اخزا across different maturities represent the same issuer risk (the
sovereign), they should price consistently on a term-structure (yield curve).
When a specific series trades at a yield significantly above the fitted curve it
is "cheap" — buy signal.  When it trades well below the curve it is "rich" —
sell / avoid signal.

The z-spread (actual YTM minus fitted curve YTM at the same maturity) measures
this richness/cheapness in basis points (bps).  Thresholds are configurable;
defaults:

  BUY  if z_spread_bps >= +50 bps  (bond yields 50 bps more than curve)
  SELL if z_spread_bps <= -50 bps  (bond yields 50 bps less than curve)
  HOLD otherwise

Implementation Notes
---------------------
- Pure Python, standard-library only (no numpy / scipy).
- Dates throughout are YYYYMMDD integers (e.g. 20260608 == 2026-06-08 Gregorian).
  اخزا are denominated and traded in Gregorian calendar on TSETMC.
- ins_code values marked as "" must be discovered via TSETMCFetcher.discover_ins_code
  or the TSETMC search API before live data can be fetched.
- AKHZA_SERIES dates/codes below are APPROXIMATE PLACEHOLDERS.  Real values
  must be verified against official TSE announcements or the TSETMC instrument
  database.  Any series whose ins_code is empty cannot be traded until discovery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

# =========================================================================== #
#  Bond series registry                                                        #
# =========================================================================== #

# NOTE: All entries below are placeholder/approximate values.
# maturity_date and issue_date are YYYYMMDD Gregorian ints.
# ins_code = "" means the TSETMC instrument code has not yet been discovered;
# call TSETMCFetcher.discover_ins_code(symbol) to populate.
# THESE DATES NEED VERIFICATION against official TSE / Ministry of Finance
# announcements before use in production.

# Series with maturity_date in the past are kept with active=False so they
# can still be used for historical back-testing but won't appear in live scans.
# ALL dates are APPROXIMATE and need verification via /api/bonds/discover
# or by consulting the official TSE bond register (bourse.ir / tsetmc.com).
AKHZA_SERIES: list[dict] = [
    # ── Expired series (kept for history) ───────────────────────────────
    {"symbol": "اخزا۱", "ins_code": "", "name": "اسناد خزانه اسلامی سری اول",
     "face_value": 1_000_000, "maturity_date": 20240101, "issue_date": 20210601,
     "coupon_rate": 0.0, "active": False, "verified": False},
    {"symbol": "اخزا۲", "ins_code": "", "name": "اسناد خزانه اسلامی سری دوم",
     "face_value": 1_000_000, "maturity_date": 20240601, "issue_date": 20211201,
     "coupon_rate": 0.0, "active": False, "verified": False},
    {"symbol": "اخزا۳", "ins_code": "", "name": "اسناد خزانه اسلامی سری سوم",
     "face_value": 1_000_000, "maturity_date": 20250101, "issue_date": 20220601,
     "coupon_rate": 0.0, "active": False, "verified": False},
    {"symbol": "اخزا۴", "ins_code": "", "name": "اسناد خزانه اسلامی سری چهارم",
     "face_value": 1_000_000, "maturity_date": 20250601, "issue_date": 20221201,
     "coupon_rate": 0.0, "active": False, "verified": False},
    {"symbol": "اخزا۵", "ins_code": "", "name": "اسناد خزانه اسلامی سری پنجم",
     "face_value": 1_000_000, "maturity_date": 20260101, "issue_date": 20230601,
     "coupon_rate": 0.0, "active": False, "verified": False},
    # ── Active series (maturity > June 2026) ─────────────────────────────
    {"symbol": "اخزا۶", "ins_code": "", "name": "اسناد خزانه اسلامی سری ششم",
     "face_value": 1_000_000, "maturity_date": 20261201, "issue_date": 20231201,
     "coupon_rate": 0.0, "active": True, "verified": False},
    {"symbol": "اخزا۷", "ins_code": "", "name": "اسناد خزانه اسلامی سری هفتم",
     "face_value": 1_000_000, "maturity_date": 20270601, "issue_date": 20240601,
     "coupon_rate": 0.0, "active": True, "verified": False},
    {"symbol": "اخزا۸", "ins_code": "", "name": "اسناد خزانه اسلامی سری هشتم",
     "face_value": 1_000_000, "maturity_date": 20271201, "issue_date": 20241201,
     "coupon_rate": 0.0, "active": True, "verified": False},
    {"symbol": "اخزا۹", "ins_code": "", "name": "اسناد خزانه اسلامی سری نهم",
     "face_value": 1_000_000, "maturity_date": 20280601, "issue_date": 20250601,
     "coupon_rate": 0.0, "active": True, "verified": False},
    {"symbol": "اخزا۱۴", "ins_code": "", "name": "اسناد خزانه اسلامی سری چهاردهم",
     "face_value": 1_000_000, "maturity_date": 20281201, "issue_date": 20261201,
     "coupon_rate": 0.0, "active": True, "verified": False},
    # placeholder extras — the user should discover real series via /api/bonds/discover
    {"symbol": "اخزا۱۰", "ins_code": "", "name": "اسناد خزانه اسلامی سری دهم",
     "face_value": 1_000_000, "maturity_date": 20290601, "issue_date": 20260601,
     "coupon_rate": 0.0, "active": True, "verified": False},
    {"symbol": "اخزا۱۱", "ins_code": "", "name": "اسناد خزانه اسلامی سری یازدهم",
     "face_value": 1_000_000, "maturity_date": 20291201, "issue_date": 20261201,
     "coupon_rate": 0.0, "active": True, "verified": False},
]


# =========================================================================== #
#  Date utilities                                                              #
# =========================================================================== #

def _today_int() -> int:
    """Return today's date as a YYYYMMDD integer."""
    d = date.today()
    return d.year * 10_000 + d.month * 100 + d.day


def _int_to_date(date_int: int) -> date:
    """Convert a YYYYMMDD integer to a datetime.date."""
    y = date_int // 10_000
    m = (date_int % 10_000) // 100
    d = date_int % 100
    return date(y, m, d)


def days_to_maturity(maturity_date_int: int, from_date_int: int = 0) -> int:
    """Return the number of calendar days between *from_date_int* and *maturity_date_int*.

    Parameters
    ----------
    maturity_date_int:
        Maturity date as a YYYYMMDD integer (e.g. 20270601).
    from_date_int:
        Settlement / valuation date as a YYYYMMDD integer.
        Defaults to today (0 triggers today's date).

    Returns
    -------
    int
        Number of days; negative if maturity is in the past.
    """
    if from_date_int == 0:
        from_date_int = _today_int()
    mat  = _int_to_date(maturity_date_int)
    from_ = _int_to_date(from_date_int)
    return (mat - from_).days


# =========================================================================== #
#  Yield math                                                                  #
# =========================================================================== #

def ytm_zero_coupon(price: float, face_value: float, days_to_mat: int) -> float:
    """Annual yield-to-maturity for a zero-coupon bond (actual/365 day-count).

    Formula
    -------
    YTM = (face_value / price) ^ (365 / days_to_mat) - 1

    Parameters
    ----------
    price:
        Current clean (= dirty for zero-coupon) market price in Rials.
    face_value:
        Par / face value at maturity (typically 1,000,000 for اخزا).
    days_to_mat:
        Number of calendar days to maturity (must be > 0).

    Returns
    -------
    float
        Annualised YTM as a decimal, e.g. 0.28 represents 28%.
        Returns 0.0 if inputs are invalid (price <= 0, days_to_mat <= 0).
    """
    if price <= 0 or face_value <= 0 or days_to_mat <= 0:
        return 0.0
    if price >= face_value:
        # Bond trading at or above par — YTM is zero or negative; return 0
        return 0.0
    try:
        ratio = face_value / price
        ytm = ratio ** (365.0 / days_to_mat) - 1.0
        return ytm
    except (ZeroDivisionError, OverflowError, ValueError):
        return 0.0


def price_zero_coupon(ytm: float, face_value: float, days_to_mat: int) -> float:
    """Price a zero-coupon bond given YTM (inverse of ytm_zero_coupon).

    Formula
    -------
    price = face_value / (1 + ytm) ^ (days_to_mat / 365)

    Parameters
    ----------
    ytm:
        Annual yield-to-maturity as decimal (e.g. 0.28 = 28%).
    face_value:
        Par / face value at maturity.
    days_to_mat:
        Days to maturity (must be > 0).

    Returns
    -------
    float
        Theoretical price in Rials.  Returns 0.0 on invalid input.
    """
    if ytm <= -1.0 or face_value <= 0 or days_to_mat <= 0:
        return 0.0
    try:
        discount = (1.0 + ytm) ** (days_to_mat / 365.0)
        if discount == 0:
            return 0.0
        return face_value / discount
    except (ZeroDivisionError, OverflowError, ValueError):
        return 0.0


# =========================================================================== #
#  Pure-Python polynomial yield curve fitting                                  #
# =========================================================================== #

def _solve_3x3(A: list[list[float]], b: list[float]) -> list[float]:
    """Solve a 3×3 linear system Ax = b using Gaussian elimination with partial pivoting.

    Parameters
    ----------
    A:
        3×3 matrix as a list of 3 rows, each row a list of 3 floats.
    b:
        Right-hand side vector of length 3.

    Returns
    -------
    list[float]
        Solution vector [x0, x1, x2].  Returns [0.0, 0.0, 0.0] if the
        system is singular or ill-conditioned.
    """
    # Copy to avoid mutating caller's data
    a = [[A[i][j] for j in range(3)] for i in range(3)]
    x = [b[i] for i in range(3)]

    for col in range(3):
        # Partial pivot: find row with largest absolute value in this column
        max_row = col
        max_val = abs(a[col][col])
        for row in range(col + 1, 3):
            if abs(a[row][col]) > max_val:
                max_val = abs(a[row][col])
                max_row = row
        # Swap rows
        a[col], a[max_row] = a[max_row], a[col]
        x[col], x[max_row] = x[max_row], x[col]

        pivot = a[col][col]
        if abs(pivot) < 1e-15:
            logger.debug("_solve_3x3: singular or near-singular matrix at column %d", col)
            return [0.0, 0.0, 0.0]

        # Eliminate below
        for row in range(col + 1, 3):
            factor = a[row][col] / pivot
            for j in range(col, 3):
                a[row][j] -= factor * a[col][j]
            x[row] -= factor * x[col]

    # Back substitution
    result = [0.0, 0.0, 0.0]
    for row in range(2, -1, -1):
        pivot = a[row][row]
        if abs(pivot) < 1e-15:
            return [0.0, 0.0, 0.0]
        s = x[row]
        for j in range(row + 1, 3):
            s -= a[row][j] * result[j]
        result[row] = s / pivot

    return result


def fit_yield_curve(
    points: list[tuple[float, float]],
    degree: int = 2,
) -> list[float]:
    """Fit a polynomial yield curve to (days_to_maturity, ytm) pairs.

    Uses the method of least squares (normal equations) for degree 2;
    for degree 1 uses a simple two-point (or two-variable OLS) formula.

    Parameters
    ----------
    points:
        List of (days, ytm) tuples.  Both values should be positive and finite.
        Points with days <= 0 or ytm <= 0 are silently ignored.
    degree:
        Polynomial degree.  Supported values: 1 or 2.
        If fewer than degree+1 valid points are available the function falls
        back to a lower degree.  Returns [] if fewer than 2 valid points exist.

    Returns
    -------
    list[float]
        Coefficient list [a0, a1, ...] such that
        ``ytm = a0 + a1*days + a2*days**2 + ...``
        Returns [] if fitting is not possible.

    Notes
    -----
    For a degree-2 fit with n points the normal equations are:

        [ n       Σx     Σx²  ] [a0]   [Σy  ]
        [ Σx      Σx²    Σx³  ] [a1] = [Σxy ]
        [ Σx²     Σx³    Σx⁴  ] [a2]   [Σx²y]

    This is solved with _solve_3x3 using Gaussian elimination.
    """
    # Filter valid points
    valid = [(d, y) for d, y in points if d > 0 and y > 0]
    n = len(valid)

    if n < 2:
        logger.debug("fit_yield_curve: not enough valid points (%d)", n)
        return []

    # Degrade degree if not enough points
    effective_degree = degree
    if n < degree + 1:
        effective_degree = n - 1
        logger.debug(
            "fit_yield_curve: only %d points, degrading degree from %d to %d",
            n, degree, effective_degree,
        )

    if effective_degree == 1:
        # Degree-1: OLS line  y = a0 + a1*x
        # Normal equations (2×2):
        #   n*a0    + Σx*a1    = Σy
        #   Σx*a0   + Σx²*a1  = Σxy
        sx  = sum(d for d, _ in valid)
        sy  = sum(y for _, y in valid)
        sx2 = sum(d * d for d, _ in valid)
        sxy = sum(d * y for d, y in valid)
        denom = n * sx2 - sx * sx
        if abs(denom) < 1e-15:
            # All points at the same maturity — return horizontal line
            mean_y = sy / n
            return [mean_y, 0.0]
        a0 = (sy * sx2 - sxy * sx) / denom
        a1 = (n * sxy - sx * sy) / denom
        return [a0, a1]

    # Degree-2: normal equations (3×3)
    sx  = sum(d      for d, _ in valid)
    sx2 = sum(d**2   for d, _ in valid)
    sx3 = sum(d**3   for d, _ in valid)
    sx4 = sum(d**4   for d, _ in valid)
    sy  = sum(y      for _, y in valid)
    sxy = sum(d*y    for d, y in valid)
    sx2y = sum(d**2 * y for d, y in valid)

    A = [
        [float(n),  sx,  sx2],
        [sx,        sx2, sx3],
        [sx2,       sx3, sx4],
    ]
    b = [sy, sxy, sx2y]
    coeffs = _solve_3x3(A, b)
    # If _solve_3x3 returned all zeros and it's not a trivial case, fall back to degree-1
    if all(c == 0.0 for c in coeffs) and not all(y == 0.0 for _, y in valid):
        logger.debug("fit_yield_curve: degree-2 failed (singular), falling back to degree-1")
        return fit_yield_curve(valid, degree=1)
    return coeffs


def eval_curve(coeffs: list[float], x: float) -> float:
    """Evaluate a polynomial at *x*.

    Parameters
    ----------
    coeffs:
        Coefficient list [a0, a1, a2, ...] where the polynomial is
        a0 + a1*x + a2*x² + ...
    x:
        Point at which to evaluate.

    Returns
    -------
    float
        Polynomial value at x.  Returns 0.0 if coeffs is empty.
    """
    if not coeffs:
        return 0.0
    result = 0.0
    for i, c in enumerate(coeffs):
        result += c * (x ** i)
    return result


# =========================================================================== #
#  Bond snapshot dataclass                                                     #
# =========================================================================== #

@dataclass
class BondSnapshot:
    """Point-in-time market snapshot for a single اخزا series.

    All monetary values in Rials.  YTM values are decimals (0.28 = 28%).
    z_spread_bps is in basis points (1 bp = 0.01%).
    """
    # Identity
    symbol:      str
    ins_code:    str
    name:        str

    # Bond terms
    face_value:   float = 1_000_000.0
    maturity_date: int  = 0             # YYYYMMDD int

    # Derived from current date + maturity_date
    days_to_mat: int    = 0

    # Market data (from TSETMC ClosingPriceInfo)
    last_price:  float  = 0.0
    close_price: float  = 0.0
    ytm:         float  = 0.0          # computed from close_price
    volume:      int    = 0
    value:       float  = 0.0
    trade_count: int    = 0

    # Curve / signal (filled by analyze_bonds)
    curve_ytm:    float = 0.0          # fitted curve YTM at this maturity
    z_spread_bps: float = 0.0          # (ytm - curve_ytm) * 10_000
    signal:       str   = "HOLD"       # "BUY", "SELL", or "HOLD"
    signal_reason: str  = ""


# =========================================================================== #
#  Analysis                                                                    #
# =========================================================================== #

def analyze_bonds(
    snapshots: list[BondSnapshot],
    entry_bps: float = 50.0,
    exit_bps: float = -50.0,
) -> list[BondSnapshot]:
    """Compute yield curve and relative-value signals for a list of bond snapshots.

    Parameters
    ----------
    snapshots:
        List of BondSnapshot objects with ytm already populated (ytm > 0).
        Snapshots with ytm == 0 are included in output but cannot contribute
        to curve fitting or receive meaningful signals.
    entry_bps:
        Z-spread threshold (bps) above which a bond is flagged BUY.
        Default: +50 bps.
    exit_bps:
        Z-spread threshold (bps) below which a bond is flagged SELL.
        Default: -50 bps.

    Returns
    -------
    list[BondSnapshot]
        Updated snapshots with curve_ytm, z_spread_bps, signal, and
        signal_reason filled in.  Sorted by z_spread_bps descending
        (cheapest / highest-yielding relative to curve first).
    """
    # Collect (days_to_mat, ytm) pairs for curve fitting
    curve_points: list[tuple[float, float]] = []
    for s in snapshots:
        if s.ytm > 0 and s.days_to_mat > 0:
            curve_points.append((float(s.days_to_mat), s.ytm))

    if len(curve_points) < 2:
        logger.warning(
            "analyze_bonds: only %d valid points — cannot fit yield curve",
            len(curve_points),
        )
        coeffs: list[float] = []
    else:
        degree = 2 if len(curve_points) >= 3 else 1
        coeffs = fit_yield_curve(curve_points, degree=degree)
        logger.debug(
            "analyze_bonds: fitted degree-%d curve from %d points, coeffs=%s",
            degree, len(curve_points), [round(c, 6) for c in coeffs],
        )

    for s in snapshots:
        if not coeffs or s.days_to_mat <= 0:
            s.curve_ytm    = 0.0
            s.z_spread_bps = 0.0
            s.signal       = "HOLD"
            s.signal_reason = "Insufficient data for curve fitting"
            continue

        curve_ytm = eval_curve(coeffs, float(s.days_to_mat))
        s.curve_ytm = curve_ytm

        if s.ytm > 0 and curve_ytm > 0:
            s.z_spread_bps = (s.ytm - curve_ytm) * 10_000.0
        else:
            s.z_spread_bps = 0.0

        if s.z_spread_bps >= entry_bps:
            s.signal = "BUY"
            s.signal_reason = (
                f"Cheap: YTM {s.ytm:.2%} vs curve {curve_ytm:.2%} "
                f"(+{s.z_spread_bps:.0f} bps)"
            )
        elif s.z_spread_bps <= exit_bps:
            s.signal = "SELL"
            s.signal_reason = (
                f"Rich: YTM {s.ytm:.2%} vs curve {curve_ytm:.2%} "
                f"({s.z_spread_bps:.0f} bps)"
            )
        else:
            s.signal = "HOLD"
            s.signal_reason = (
                f"On-curve: YTM {s.ytm:.2%} vs curve {curve_ytm:.2%} "
                f"({s.z_spread_bps:+.0f} bps)"
            )

    # Sort cheapest first (highest z-spread first)
    snapshots.sort(key=lambda s: s.z_spread_bps, reverse=True)
    return snapshots


# =========================================================================== #
#  Bond scan entry point                                                       #
# =========================================================================== #

def run_bond_scan(db, fetcher) -> list[BondSnapshot]:
    """Fetch live market data for all active اخزا series and return ranked snapshots.

    Parameters
    ----------
    db:
        A database object exposing ``get_bond_series() -> list[dict]``.
        Each dict should have the same keys as entries in AKHZA_SERIES.
        Falls back to AKHZA_SERIES if db.get_bond_series() is not available
        or returns an empty list.
    fetcher:
        A data fetcher (e.g. TSETMCFetcher) exposing
        ``get_closing_price_info(ins_code: str) -> dict | None``.
        The returned dict must include at least: close_price, last_price,
        volume, value, trade_count.

    Returns
    -------
    list[BondSnapshot]
        Snapshots sorted by z_spread_bps descending (cheapest first).
        Series without a known ins_code or without trade data are included
        with ytm == 0.
    """
    # ── 1. Load series registry ──────────────────────────────────────────
    try:
        series_list: list[dict] = db.get_bond_series()
    except AttributeError:
        logger.warning(
            "run_bond_scan: db has no get_bond_series() method — using AKHZA_SERIES"
        )
        series_list = []

    if not series_list:
        logger.info("run_bond_scan: using built-in AKHZA_SERIES (%d entries)", len(AKHZA_SERIES))
        series_list = AKHZA_SERIES

    today = _today_int()
    snapshots: list[BondSnapshot] = []

    for series in series_list:
        symbol       = series.get("symbol", "")
        ins_code     = series.get("ins_code", "").strip()
        name         = series.get("name", symbol)
        face_value   = float(series.get("face_value", 1_000_000))
        maturity_int = int(series.get("maturity_date", 0))

        dtm = days_to_maturity(maturity_int, today) if maturity_int else 0

        # Skip expired bonds
        if maturity_int > 0 and dtm <= 0:
            logger.debug("run_bond_scan: %s already matured (%d), skipping", symbol, maturity_int)
            continue

        snap = BondSnapshot(
            symbol=symbol,
            ins_code=ins_code,
            name=name,
            face_value=face_value,
            maturity_date=maturity_int,
            days_to_mat=dtm,
        )

        if not ins_code:
            snap.signal_reason = "No ins_code — discovery required"
            snapshots.append(snap)
            continue

        # ── 2. Fetch price data ──────────────────────────────────────────
        try:
            price_data: Optional[dict] = fetcher.get_closing_price_info(ins_code)
        except Exception as exc:
            logger.warning("run_bond_scan: error fetching %s (%s): %s", symbol, ins_code, exc)
            price_data = None

        if not price_data:
            snap.signal_reason = "Price data unavailable"
            snapshots.append(snap)
            continue

        close_price = float(price_data.get("close_price", 0) or 0)
        last_price  = float(price_data.get("last_price",  0) or 0)
        volume      = int(price_data.get("volume",       0) or 0)
        value       = float(price_data.get("value",      0) or 0)
        trade_count = int(price_data.get("trade_count",  0) or 0)

        snap.close_price = close_price
        snap.last_price  = last_price
        snap.volume      = volume
        snap.value       = value
        snap.trade_count = trade_count

        # ── 3. Compute YTM from close price ─────────────────────────────
        ref_price = close_price if close_price > 0 else last_price
        if ref_price > 0 and dtm > 0:
            snap.ytm = ytm_zero_coupon(ref_price, face_value, dtm)
        else:
            snap.ytm = 0.0

        snapshots.append(snap)

    logger.info(
        "run_bond_scan: built %d snapshots (%d with YTM > 0)",
        len(snapshots),
        sum(1 for s in snapshots if s.ytm > 0),
    )

    # ── 4. Fit curve and generate signals ───────────────────────────────
    return analyze_bonds(snapshots)
