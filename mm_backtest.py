# -*- coding: utf-8 -*-
"""
موتور بک‌تستِ بازارگردانیِ قانونی — سهام بورس و فرابورس ایران
================================================================
این موتور یک بازارگردانِ «مظنه‌ای» (quote-based) را شبیه‌سازی می‌کند که طبق
«دستورالعمل فعالیت بازارگردانی در بورس تهران و فرابورس» رفتار می‌کند و سود/زیان،
موجودی (inventory)، نرخ حضور و رعایت قیود قانونی را گزارش می‌دهد.

مدلِ قانونی (هر تیک رعایت می‌شود)
---------------------------------
بازارگردان به‌طور پیوسته یک مظنهٔ دوطرفه می‌گذارد:
    bid = مرکز − نیم‌اسپرد ،  ask = مرکز + نیم‌اسپرد
با این قیود:
  • دامنهٔ مظنه:  (ask − bid)/ref ≤ max_spread_pct   (سقف قانونی)
  • دامنهٔ نوسان: bid,ask داخل [ref·(1−band), ref·(1+band)]
  • آستانهٔ قیمت: قیمت‌ها به نزدیک‌ترین tick گرد می‌شوند
  • حجم تعهد:    هر طرف حداقل order_volume سهم
  • کف/سقف موجودی: اگر موجودی به سقف رسید سمت خرید، و اگر به کف رسید سمت فروش
                    تعطیل می‌شود (و نرخ حضور افت می‌کند)
  • ساعت معاملاتی: فقط داخل [session_open, session_close]

مدلِ پرشدنِ محافظه‌کارانه (اولویت صف)
------------------------------------
سفارش بازارگردان پشتِ حجمِ موجودِ همان سطح قیمت در اردربوک می‌نشیند. هر معاملهٔ
بازار که از مظنهٔ بازارگردان رد شود، اول صفِ جلوتر را مصرف می‌کند؛ فقط مازاد به
بازارگردان می‌رسد. این سودِ اسپرد را بیش‌برآورد نمی‌کند.
  • معاملهٔ با قیمت ≤ bidِ بازارگردان  →  فروشندهٔ تهاجمی؛ ممکن است bid را پر کند
  • معاملهٔ با قیمت ≥ askِ بازارگردان  →  خریدارِ تهاجمی؛ ممکن است ask را پر کند

سود بازارگردان = گرفتنِ اسپرد + معافیت/تخفیف کارمزد بازارگردانی.

⚠ مقادیر دقیق (٪ دامنهٔ مظنه، دامنهٔ نوسان، حجم مبنا، کارمزدها، آستانهٔ قیمت) برای
هر نماد و در طول زمان فرق می‌کند؛ همه پارامتری‌اند و باید با آخرین دستورالعمل
سازمان بورس و قراردادِ همان نماد تأیید شوند.
"""

from __future__ import annotations

import itertools
import logging
import math
import os
from dataclasses import dataclass, asdict, field

logger = logging.getLogger(__name__)

# ── کارمزد معاملاتیِ سهام (کسری). بازارگردان معمولاً معاف/کم‌کارمزد است. ──
# مقادیر مرجعِ غیرِبازارگردان ~ خرید 0.3712% ، فروش 0.88% (با مالیات).
# پیش‌فرضِ این‌جا = نرخِ تخفیف‌خوردهٔ بازارگردانی (قابل‌تغییر).
MM_BUY_FEE_DEFAULT  = 0.0005
MM_SELL_FEE_DEFAULT = 0.0005

SESSION_OPEN_DEFAULT  = 90000    # 09:00:00
SESSION_CLOSE_DEFAULT = 123000   # 12:30:00


# --------------------------------------------------------------------------- #
#  پارامترها                                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class MMParams:
    # منابع
    cash: float = 10_000_000_000.0        # نقدِ اولیهٔ بازارگردانی (ریال)
    initial_inventory: int = 0            # موجودیِ سهامِ اولیه
    target_inventory: int = 0             # موجودیِ هدف (مرکزِ skew)

    # تعهداتِ مظنه
    quote_spread_pct: float = 0.015       # اسپردِ هدفِ بازارگردان (کسری، مثلاً 1.5%)
    max_spread_pct: float = 0.02          # سقفِ قانونیِ دامنهٔ مظنه
    order_volume: int = 10_000            # حجمِ تعهدِ هر طرف (سهم)

    # قیودِ قیمت
    price_band_pct: float = 0.05          # دامنهٔ نوسانِ روزانه (±، کسری)
    ref_mode: str = "prev_close"          # مرجعِ مظنه: prev_close | day_open | last
    tick_size: float = 1.0                # آستانهٔ قیمت (ریال) — گردکردنِ مطلق
    tick_pct: float = 0.0                 # اگر >0: آستانهٔ نسبی (٪ قیمت) به‌جای مطلق

    # مدیریتِ موجودی
    inventory_floor: int = -1_000_000     # کفِ موجودی (می‌تواند منفی = short)
    inventory_ceiling: int = 1_000_000    # سقفِ موجودی
    skew_pct_per_unit: float = 0.0        # شیفتِ مرکز به‌ازای هر واحدِ انحراف از هدف
                                          # (کسری به‌ازای هر order_volume سهم انحراف)

    # کارمزد و مشوّق
    buy_fee: float = MM_BUY_FEE_DEFAULT
    sell_fee: float = MM_SELL_FEE_DEFAULT

    # مرکزِ مظنه: حول مرجعِ روز یا حول میانهٔ زندهٔ بازار
    center_mode: str = "mid"              # mid (میانهٔ اردربوک) | ref (مرجعِ روز)

    # رفتارِ پایانِ روز
    flatten_eod: bool = False             # True = موجودی پایانِ روز صفر شود

    # صف خرید / صف فروش (قفلِ سقف/کف دامنهٔ نوسان)
    relieve_queue: bool = True            # در قفل، تعهدِ دوطرفه ساقط و بازارگردان
                                          # فقط سمتِ رفعِ صف را می‌گذارد (فروش روی
                                          # سقف در صف خرید، خرید روی کف در صف فروش).
                                          # False = در قفل کنار می‌کشد (بدون مظنه).

    # ساعتِ معاملاتی
    session_open: int = SESSION_OPEN_DEFAULT
    session_close: int = SESSION_CLOSE_DEFAULT


# --------------------------------------------------------------------------- #
#  ابزارها                                                                      #
# --------------------------------------------------------------------------- #

def _round_tick(price: float, p: MMParams) -> float:
    """قیمت را به نزدیک‌ترین آستانهٔ قیمت گرد می‌کند."""
    if price <= 0:
        return 0.0
    tick = price * p.tick_pct if p.tick_pct > 0 else p.tick_size
    if tick <= 0:
        return float(price)
    return round(price / tick) * tick


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


def _in_session(t: int, p: MMParams) -> bool:
    return p.session_open <= t <= p.session_close


@dataclass
class MMDayState:
    """وضعیتی که بین روزها حمل می‌شود (موجودی و نقد)."""
    cash: float
    inventory: int


# --------------------------------------------------------------------------- #
#  شبیه‌سازیِ یک روز                                                            #
# --------------------------------------------------------------------------- #

def _quote(center: float, ref: float, inventory: int, p: MMParams):
    """مظنهٔ قانونیِ دوطرفه را می‌سازد: (bid, ask) یا (None,None) اگر نشد.

    اسپرد به سقفِ قانونی محدود، حول مرکزِ skew‌شده، داخلِ دامنهٔ نوسان، گردشده به tick.
    """
    if center <= 0 or ref <= 0:
        return None, None
    spread = min(p.quote_spread_pct, p.max_spread_pct)
    # شیفتِ موجودی: اگر long (موجودی>هدف) مرکز را پایین بیاور تا فروش جذاب‌تر شود.
    if p.skew_pct_per_unit and p.order_volume > 0:
        units = (inventory - p.target_inventory) / float(p.order_volume)
        center = center * (1.0 - p.skew_pct_per_unit * units)
    half = center * spread / 2.0
    bid = _round_tick(center - half, p)
    ask = _round_tick(center + half, p)
    # دامنهٔ نوسانِ روزانه حول مرجع
    lo = ref * (1.0 - p.price_band_pct)
    hi = ref * (1.0 + p.price_band_pct)
    bid = _round_tick(_clamp(bid, lo, hi), p)
    ask = _round_tick(_clamp(ask, lo, hi), p)
    if ask <= bid:                         # گردکردن/کلمپ صفرشان کرد
        return None, None
    # قیدِ سقفِ قانونیِ دامنهٔ مظنه
    if (ask - bid) / ref > p.max_spread_pct + 1e-12:
        return None, None
    return bid, ask


def _simulate_mm_day(date_int: int, snaps: list, trades: list,
                     p: MMParams, carry: MMDayState,
                     ref_override: float = 0.0):
    """یک روزِ بازارگردانی را شبیه‌سازی می‌کند.

    snaps : لیستِ اردربوک (مرتب به زمان) با کلیدهای bid1_price/vol, ask1_price/vol …
    trades: لیستِ معاملاتِ تیک (مرتب به seq) با price, volume, time, canceled
    carry : موجودی/نقدِ ابتدای روز
    ref_override: اگر >0 (مثلاً prev_close) مرجعِ دامنهٔ نوسان همین می‌شود.
    خروجی : (metrics_dict, end_state, fills)
    """
    cash = carry.cash
    inv = carry.inventory

    snaps = [s for s in snaps if _in_session(int(s.get("time", 0)), p)]
    trades = [t for t in trades
              if not t.get("canceled") and t.get("volume", 0) > 0
              and t.get("price", 0) > 0 and _in_session(int(t.get("time", 0)), p)]
    if not snaps or not trades:
        return _empty_day(date_int, cash, inv), MMDayState(cash, inv), []

    # مرجعِ دامنهٔ نوسان: ثابت برای کلِ روز — قیمتِ پایانیِ روز قبل (اگر داده شد)
    # وگرنه میانهٔ آغازِ روز. این مرجع، سقف/کفِ باند و تشخیصِ صف را تعیین می‌کند؛
    # نباید با میانهٔ زندهٔ بازار (که در روزِ قفل خودش روی سقف است) شناور شود.
    first = snaps[0]
    day_open_mid = _mid(first)
    band_ref = ref_override if ref_override > 0 else day_open_mid

    trades_sorted = sorted(trades, key=lambda x: (int(x["time"]), x.get("seq", 0)))
    ti = 0
    n_trades = len(trades_sorted)

    buys = sells = 0
    buy_vol = sell_vol = 0
    buy_notional = sell_notional = 0.0
    fees_paid = 0.0
    fills: list[dict] = []

    intervals = 0
    compliant_intervals = 0
    lock_buy_snaps = lock_sell_snaps = 0    # تعدادِ snapshotهای صف خرید/فروش
    relief_vol = 0                          # حجمی که بازارگردان به صف تزریق کرد

    # وضعیتِ صف: قیمتِ فعلیِ مظنه و حجمِ جلوترِ مصرف‌نشده در هر سمت.
    cur_bid = cur_ask = None
    q_ahead_bid = q_ahead_ask = 0.0       # حجمِ صفِ جلوتر (هنوز مصرف‌نشده)
    bid_remaining = ask_remaining = 0     # حجمِ باقی‌ماندهٔ سفارشِ بازارگردان

    for si, snap in enumerate(snaps):
        t0 = int(snap["time"])
        t1 = int(snaps[si + 1]["time"]) if si + 1 < len(snaps) else p.session_close + 1

        # مرکزِ مظنه: حول مرجعِ ثابت (center_mode=ref) یا میانهٔ زندهٔ بازار.
        center = band_ref if p.center_mode == "ref" else (_mid(snap) or band_ref)

        # دامنهٔ نوسان: سقف/کفِ ثابتِ روز، و تشخیصِ صف خرید/فروش (قفل).
        pc_ceiling = band_ref * (1.0 + p.price_band_pct)
        pc_floor   = band_ref * (1.0 - p.price_band_pct)
        lock = _lock_state(snap, pc_ceiling, pc_floor, p)

        # شمارشِ قفل (مستقل از رفتارِ بازارگردان)
        if lock == "buy":
            lock_buy_snaps += 1
        elif lock == "sell":
            lock_sell_snaps += 1

        if lock and p.relieve_queue:
            # تعهدِ دوطرفه ساقط؛ بازارگردان فقط سمتِ رفعِ صف را می‌گذارد.
            if lock == "buy":                  # صف خرید → فقط فروش روی سقف
                new_bid, new_ask = None, _round_tick(pc_ceiling, p)
            else:                               # صف فروش → فقط خرید روی کف
                new_bid, new_ask = _round_tick(pc_floor, p), None
        elif lock:
            # رفعِ صف خاموش است → در قفل کنار می‌کشد (بدون مظنه).
            new_bid, new_ask = None, None
        else:
            new_bid, new_ask = _quote(center, band_ref, inv, p)

        # کف/سقفِ موجودی: سمتِ پر را تعطیل کن
        buy_ok = inv < p.inventory_ceiling and cash > 0
        sell_ok = inv > p.inventory_floor

        # حضورِ قانونی
        intervals += 1
        if lock and p.relieve_queue:
            # در قفل، حضور = ارائهٔ سمتِ رفعِ صف (تعهدِ دوطرفه ساقط است).
            relieving_ok = ((lock == "buy" and new_ask is not None and sell_ok)
                            or (lock == "sell" and new_bid is not None and buy_ok))
            if relieving_ok:
                compliant_intervals += 1
        elif lock:
            pass                                # غایب در قفل → ناسازگار
        else:
            two_sided = (new_bid is not None and new_ask is not None
                         and buy_ok and sell_ok)
            if two_sided:
                compliant_intervals += 1

        # تازه‌سازیِ مظنه؛ اگر قیمت عوض شد، صف از نو (ته صف) شروع می‌شود.
        if new_bid != cur_bid:
            cur_bid = new_bid
            bid_remaining = p.order_volume if new_bid is not None else 0
            q_ahead_bid = _level_vol(snap, new_bid, "bid") if new_bid else 0.0
        if new_ask != cur_ask:
            cur_ask = new_ask
            ask_remaining = p.order_volume if new_ask is not None else 0
            q_ahead_ask = _level_vol(snap, new_ask, "ask") if new_ask else 0.0

        # معاملاتِ این بازهٔ زمانی را علیهِ مظنهٔ ساکن تطبیق بده
        while ti < n_trades and int(trades_sorted[ti]["time"]) < t1:
            tr = trades_sorted[ti]
            ti += 1
            tp = float(tr["price"])
            tv = float(tr["volume"])

            # سمتِ فروشِ تهاجمی → پرشدنِ bidِ بازارگردان
            if cur_bid is not None and buy_ok and bid_remaining > 0 and tp <= cur_bid + 1e-9:
                if q_ahead_bid > 0:
                    used = min(q_ahead_bid, tv)
                    q_ahead_bid -= used
                    tv -= used
                if tv > 0 and inv < p.inventory_ceiling:
                    room = p.inventory_ceiling - inv
                    fill = int(min(tv, bid_remaining, room))
                    if fill > 0:
                        cost = fill * cur_bid
                        fee = cost * p.buy_fee
                        if cost + fee <= cash:
                            cash -= cost + fee
                            inv += fill
                            bid_remaining -= fill
                            buys += 1
                            buy_vol += fill
                            buy_notional += cost
                            fees_paid += fee
                            if lock:
                                relief_vol += fill
                            fills.append({"date": date_int, "time": int(tr["time"]),
                                          "side": "buy", "price": cur_bid,
                                          "volume": fill, "inv": inv,
                                          "lock": lock or ""})
                continue

            # سمتِ خریدِ تهاجمی → پرشدنِ askِ بازارگردان
            if cur_ask is not None and sell_ok and ask_remaining > 0 and tp >= cur_ask - 1e-9:
                if q_ahead_ask > 0:
                    used = min(q_ahead_ask, tv)
                    q_ahead_ask -= used
                    tv -= used
                if tv > 0 and inv > p.inventory_floor:
                    room = inv - p.inventory_floor
                    fill = int(min(tv, ask_remaining, room))
                    if fill > 0:
                        proceeds = fill * cur_ask
                        fee = proceeds * p.sell_fee
                        cash += proceeds - fee
                        inv -= fill
                        ask_remaining -= fill
                        sells += 1
                        sell_vol += fill
                        sell_notional += proceeds
                        fees_paid += fee
                        if lock:
                            relief_vol += fill
                        fills.append({"date": date_int, "time": int(tr["time"]),
                                      "side": "sell", "price": cur_ask,
                                      "volume": fill, "inv": inv,
                                      "lock": lock or ""})

    # علامت‌گذاریِ موجودیِ پایانی به آخرین قیمتِ معامله
    last_price = float(trades_sorted[-1]["price"])
    if p.flatten_eod and inv != 0:
        # تسویهٔ موجودی به آخرین قیمت (با کارمزدِ همان سمت)
        if inv > 0:
            proc = inv * last_price; cash += proc - proc * p.sell_fee
            sell_vol += inv; sell_notional += proc; fees_paid += proc * p.sell_fee
        else:
            cost = (-inv) * last_price; cash += -(cost + cost * p.buy_fee)
            buy_vol += -inv; buy_notional += cost; fees_paid += cost * p.buy_fee
        inv = 0

    start_equity = carry.cash + carry.inventory * (day_open_mid or last_price)
    end_equity = cash + inv * last_price
    presence = (compliant_intervals / intervals) if intervals else 0.0

    metrics = {
        "date": date_int,
        "buys": buys, "sells": sells,
        "buy_vol": buy_vol, "sell_vol": sell_vol,
        "buy_notional": round(buy_notional, 0),
        "sell_notional": round(sell_notional, 0),
        "fees": round(fees_paid, 0),
        "end_inventory": inv,
        "end_cash": round(cash, 0),
        "pnl": round(end_equity - start_equity, 0),
        "presence_pct": round(presence * 100.0, 1),
        "last_price": last_price,
        "round_trips": min(buy_vol, sell_vol),
        "lock_buy": lock_buy_snaps,
        "lock_sell": lock_sell_snaps,
        "relief_vol": relief_vol,
    }
    return metrics, MMDayState(cash, inv), fills


def bid_remaining_target(p: MMParams) -> int:
    return p.order_volume


def _mid(snap: dict) -> float:
    b = snap.get("bid1_price", 0) or 0
    a = snap.get("ask1_price", 0) or 0
    if b > 0 and a > 0:
        return (a + b) / 2.0
    return float(a or b or 0)


def _lock_state(snap: dict, pc_ceiling: float, pc_floor: float,
                p: MMParams) -> str:
    """تشخیصِ قفلِ دامنهٔ نوسان از روی اردربوک: "buy" | "sell" | "".

    صف خرید (buy): تقاضا روی سقف و سمتِ عرضه خالی/بالاتر از سقف
                   (همه می‌خواهند بخرند، فروشنده‌ای نیست).
    صف فروش (sell): عرضه روی کف و سمتِ تقاضا خالی/پایین‌تر از کف.
    """
    tick = (pc_ceiling * p.tick_pct) if p.tick_pct > 0 else p.tick_size
    tol = max(tick, pc_ceiling * 1e-4)
    b1p = snap.get("bid1_price", 0) or 0
    b1v = snap.get("bid1_vol", 0) or 0
    a1p = snap.get("ask1_price", 0) or 0
    a1v = snap.get("ask1_vol", 0) or 0
    # صف خرید: بهترین خرید روی سقف، عرضه‌ای در/زیرِ سقف نیست.
    if b1p > 0 and b1p >= pc_ceiling - tol and (a1p <= 0 or a1v <= 0
                                                or a1p > pc_ceiling + tol):
        return "buy"
    # صف فروش: بهترین فروش روی کف، تقاضایی در/بالایِ کف نیست.
    if a1p > 0 and a1p <= pc_floor + tol and (b1p <= 0 or b1v <= 0
                                              or b1p < pc_floor - tol):
        return "sell"
    return ""


def _level_vol(snap: dict, price: float, side: str) -> float:
    """حجمِ نمایش‌داده‌شده در سطحِ قیمتِ price سمتِ side (برای تخمینِ صفِ جلوتر).

    اگر قیمتِ بازارگردان از بهترین سطح بهتر باشد (تنها در صف) → 0.
    اگر با یکی از پنج سطح برابر باشد → حجمِ همان سطح. وگرنه (بدتر) → عمقِ کل.
    """
    pref = "bid" if side == "bid" else "ask"
    best = snap.get(f"{pref}1_price", 0) or 0
    if best <= 0:
        return 0.0
    # بهتر از بهترین سطح (bid بالاتر / ask پایین‌تر) = جلوی صف
    if (side == "bid" and price > best) or (side == "ask" and price < best):
        return 0.0
    total = 0.0
    for lvl in range(1, 6):
        lp = snap.get(f"{pref}{lvl}_price", 0) or 0
        lv = snap.get(f"{pref}{lvl}_vol", 0) or 0
        if lp and abs(lp - price) < max(1.0, price * 1e-6):
            return float(lv)
        total += lv
    return total                            # قیمتِ بازارگردان بدتر از کل تابلو


def _empty_day(date_int, cash, inv):
    return {"date": date_int, "buys": 0, "sells": 0, "buy_vol": 0, "sell_vol": 0,
            "buy_notional": 0.0, "sell_notional": 0.0, "fees": 0.0,
            "end_inventory": inv, "end_cash": round(cash, 0), "pnl": 0.0,
            "presence_pct": 0.0, "last_price": 0.0, "round_trips": 0,
            "lock_buy": 0, "lock_sell": 0, "relief_vol": 0}


# --------------------------------------------------------------------------- #
#  ورودیِ اصلی                                                                  #
# --------------------------------------------------------------------------- #

def run_mm_backtest(db, symbol: str,
                    start_date: int | None = None,
                    end_date: int | None = None,
                    params: MMParams | None = None) -> dict:
    """بک‌تستِ بازارگردانی روی یک نماد در بازهٔ تاریخی.

    برای هر روز اردربوک و معاملاتِ تیک را بار می‌کند و مظنهٔ بازارگردان را
    شبیه‌سازی می‌کند. موجودی/نقد بین روزها حمل می‌شود (مگر flatten_eod).
    """
    p = params or MMParams()
    dates, cache, prev_close_map = _load_mm_cache(db, symbol, start_date, end_date)
    day_rows, state, all_fills = _replay_mm(dates, cache, prev_close_map, p)
    summary = _summarize_mm(day_rows, p, state)
    return {
        "symbol": symbol,
        "params": asdict(p),
        "days_tested": len(day_rows),
        "days": day_rows,
        "fills": all_fills[:5000],          # سقف برای حجمِ پاسخ
        "fill_count": len(all_fills),
        "summary": summary,
    }


def _load_mm_cache(db, symbol: str, start_date, end_date):
    """اردربوک و معاملاتِ تیکِ هر روز را یک‌بار از DB می‌خواند (کارِ گرانِ I/O).

    خروجی: (dates, cache, prev_close_map) که cache نگاشتِ date → (snaps, trades)
    است؛ بهینه‌ساز همین کش را روی همهٔ ترکیب‌ها بدونِ خواندنِ دوبارهٔ DB replay می‌کند.
    """
    dates = db.get_ob_dates(symbol)
    if start_date:
        dates = [d for d in dates if d >= start_date]
    if end_date:
        dates = [d for d in dates if d <= end_date]
    dates = sorted(dates)
    cache: dict[int, tuple] = {}
    for d in dates:
        snaps = db.get_orderbook_history(symbol, d, limit=20000)
        trades = db.get_intraday_trades(symbol, d)
        cache[d] = (snaps, trades)
    prev_close_map = _prev_close_map(db, symbol)
    return dates, cache, prev_close_map


def _replay_mm(dates, cache, prev_close_map, p: MMParams):
    """replayِ خالصِ بازارگردانی روی کشِ روز (بدونِ DB) برای یک پارامتر."""
    state = MMDayState(p.cash, p.initial_inventory)
    day_rows: list[dict] = []
    all_fills: list[dict] = []
    use_pc = (p.ref_mode == "prev_close")
    for d in dates:
        snaps, trades = cache[d]
        ref_override = prev_close_map.get(d, 0.0) if use_pc else 0.0
        metrics, state, fills = _simulate_mm_day(d, snaps, trades, p, state,
                                                 ref_override=ref_override)
        all_fills.extend(fills)
        day_rows.append(metrics)
    return day_rows, state, all_fills


def _prev_close_map(db, symbol: str) -> dict:
    """نگاشتِ date → close روزِ قبل (برای مرجعِ مظنه)."""
    try:
        rows = db.get_daily_history(symbol, days=4000)
    except Exception:
        return {}
    rows = sorted(rows, key=lambda r: r.get("date", 0))
    out = {}
    prev = 0.0
    for r in rows:
        d = r.get("date", 0)
        if prev > 0:
            out[d] = prev
        prev = r.get("close_price", 0) or prev
    return out


def _summarize_mm(day_rows: list, p: MMParams, end_state: MMDayState) -> dict:
    if not day_rows:
        return {"trade_days": 0, "total_pnl": 0.0, "total_return_pct": 0.0,
                "avg_presence_pct": 0.0, "total_buys": 0, "total_sells": 0,
                "total_volume": 0, "total_fees": 0.0, "end_inventory": end_state.inventory,
                "sharpe": 0.0, "max_drawdown_pct": 0.0, "win_days": 0, "win_rate": 0.0,
                "lock_buy_snaps": 0, "lock_sell_snaps": 0, "total_relief_vol": 0}
    pnls = [r["pnl"] for r in day_rows]
    total_pnl = sum(pnls)
    base = p.cash + p.initial_inventory * (day_rows[0].get("last_price") or 0)
    base = base or p.cash or 1.0
    # equity curve & drawdown
    eq = 0.0; peak = 0.0; mdd = 0.0
    for x in pnls:
        eq += x; peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak - eq) / (base))
    mu = total_pnl / len(pnls)
    var = sum((x - mu) ** 2 for x in pnls) / max(len(pnls) - 1, 1)
    sd = math.sqrt(var)
    sharpe = (mu / sd * math.sqrt(252)) if sd > 1e-9 else 0.0
    win_days = sum(1 for x in pnls if x > 0)
    presences = [r["presence_pct"] for r in day_rows]
    return {
        "trade_days": len(day_rows),
        "total_pnl": round(total_pnl, 0),
        "total_return_pct": round(total_pnl / base * 100.0, 3),
        "avg_presence_pct": round(sum(presences) / len(presences), 1),
        "total_buys": sum(r["buys"] for r in day_rows),
        "total_sells": sum(r["sells"] for r in day_rows),
        "total_volume": sum(r["buy_vol"] + r["sell_vol"] for r in day_rows),
        "total_fees": round(sum(r["fees"] for r in day_rows), 0),
        "end_inventory": end_state.inventory,
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(mdd * 100.0, 3),
        "win_days": win_days,
        "win_rate": round(win_days / len(pnls) * 100.0, 1),
        "lock_buy_snaps": sum(r.get("lock_buy", 0) for r in day_rows),
        "lock_sell_snaps": sum(r.get("lock_sell", 0) for r in day_rows),
        "total_relief_vol": sum(r.get("relief_vol", 0) for r in day_rows),
    }


# --------------------------------------------------------------------------- #
#  بهینه‌ساز — جستجوی شبکه‌ای موازی با گیتِ حضورِ قانونی                          #
# --------------------------------------------------------------------------- #
#
# داده‌های روز یک‌بار در حافظه بار می‌شوند (_load_mm_cache) و هر ترکیب فقط replay
# می‌شود (_replay_mm).  ترکیب‌ها بینِ هسته‌ها پخش می‌شوند: fork (لینوکس/WSL) کشِ
# روز را copy-on-write به ارث می‌برد؛ spawn (ویندوز) آن را یک‌بار pickle می‌کند.

MM_GRID = {
    "quote_spread_pct":   [0.004, 0.008, 0.012, 0.016, 0.020],
    "order_volume":       [5_000, 10_000, 20_000, 50_000],
    "skew_pct_per_unit":  [0.0, 0.001, 0.003],
    "center_mode":        ["mid", "ref"],
    "ref_mode":           ["prev_close", "day_open"],
}
# چیدمانِ تاپلِ ترکیب: 0:spread 1:vol 2:skew 3:center_idx 4:ref_idx
_CENTER = MM_GRID["center_mode"]
_REF = MM_GRID["ref_mode"]


def _mm_combo_params(base: MMParams, c: tuple) -> MMParams:
    d = asdict(base)
    d.update(quote_spread_pct=float(c[0]), order_volume=int(c[1]),
             skew_pct_per_unit=float(c[2]),
             center_mode=_CENTER[c[3]], ref_mode=_REF[c[4]])
    return MMParams(**d)


def _mm_combo_result(p: MMParams, day_rows, end_state, opt_metric: str,
                     min_presence_pct: float) -> dict:
    s = _summarize_mm(day_rows, p, end_state)
    # گیتِ قانونی: حضورِ ناکافی = نامعتبر (بازارگردانِ غیرمتعهد).
    if s["trade_days"] == 0 or s["avg_presence_pct"] < min_presence_pct:
        score = -1e9
    else:
        m = opt_metric
        if m == "total_pnl":
            score = s["total_pnl"]
        elif m == "return_pct":
            score = s["total_return_pct"]
        elif m == "presence_pnl":          # سود وزن‌داده به نرخِ حضور
            score = s["total_pnl"] * (s["avg_presence_pct"] / 100.0)
        else:                               # sharpe (پیش‌فرض)
            score = s["sharpe"]
    return {"params": asdict(p), "summary": s, "score": round(float(score), 4)}


def _mm_valid(c: tuple, base: MMParams) -> bool:
    # اسپردِ مظنه نباید از سقفِ قانونی بیشتر باشد.
    return float(c[0]) <= base.max_spread_pct + 1e-12


def _mm_all_combos(base: MMParams) -> list[tuple]:
    return [c for c in itertools.product(
        MM_GRID["quote_spread_pct"], MM_GRID["order_volume"],
        MM_GRID["skew_pct_per_unit"], range(len(_CENTER)), range(len(_REF)))
        if _mm_valid(c, base)]


# شیِ مشترک برای workerهای fork/spawn (مثلِ bond_backtest._OPT_SHARED).
_MM_SHARED: dict = {}


def _mm_spawn_init(shared: dict) -> None:
    _MM_SHARED.clear()
    _MM_SHARED.update(shared)


def _mm_worker(combo: tuple) -> dict:
    sh = _MM_SHARED
    base = sh["base"]
    p = _mm_combo_params(base, combo)
    day_rows, end_state, _ = _replay_mm(sh["dates"], sh["cache"],
                                        sh["prev_close_map"], p)
    r = _mm_combo_result(p, day_rows, end_state, sh["opt_metric"],
                         sh["min_presence_pct"])
    cnt = sh.get("counter")
    if cnt is not None:
        with cnt.get_lock():
            cnt.value += 1
    return r


def _mm_sort_key(r: dict):
    p = r["params"]
    return (-r["score"], p["quote_spread_pct"], p["order_volume"],
            p["skew_pct_per_unit"], p["center_mode"], p["ref_mode"])


def optimize_mm_backtest(db, symbol: str,
                         start_date: int | None = None,
                         end_date: int | None = None,
                         base: MMParams | None = None,
                         opt_metric: str = "sharpe",
                         min_presence_pct: float = 50.0,
                         top_n: int = 15,
                         n_jobs: int = 0,
                         progress: dict | None = None,
                         progress_lock=None) -> dict:
    """جستجوی شبکه‌ای روی پارامترهای بازارگردانی برای یک نماد.

    داده یک‌بار بار می‌شود، هر ترکیب replay می‌شود، نتایج بر اساسِ opt_metric با
    گیتِ حداقل‌حضور (قید قانونی) رتبه‌بندی می‌شوند. موازی روی هسته‌ها (n_jobs=0=auto).
    """
    import threading
    base = base or MMParams()
    dates, cache, prev_close_map = _load_mm_cache(db, symbol, start_date, end_date)
    combos = _mm_all_combos(base)
    if progress is not None:
        with (progress_lock or threading.Lock()):
            progress.update({"done": 0, "total": len(combos), "phase": "grid"})

    if not dates or not combos:
        return {"symbol": symbol, "tested_combos": 0, "opt_metric": opt_metric,
                "days_available": len(dates), "date_from": dates[0] if dates else None,
                "date_to": dates[-1] if dates else None, "best": None, "top": []}

    shared = {"base": base, "dates": dates, "cache": cache,
              "prev_close_map": prev_close_map, "opt_metric": opt_metric,
              "min_presence_pct": min_presence_pct}

    results = _mm_run_combos(combos, shared, progress, progress_lock, n_jobs)
    results.sort(key=_mm_sort_key)
    best = results[0] if results and results[0]["score"] > -1e8 else None
    return {
        "symbol": symbol,
        "tested_combos": len(results),
        "opt_metric": opt_metric,
        "min_presence_pct": min_presence_pct,
        "days_available": len(dates),
        "date_from": dates[0], "date_to": dates[-1],
        "best": best,
        "top": results[:top_n],
    }


def _mm_run_combos(combos, shared, progress, lock, n_jobs) -> list[dict]:
    """ترکیب‌ها را موازی (fork/spawn) یا ترتیبی ارزیابی می‌کند."""
    jobs = n_jobs if n_jobs > 0 else max(1, (os.cpu_count() or 2) - 1)
    jobs = min(jobs, len(combos))
    if jobs <= 1:
        _MM_SHARED.clear(); _MM_SHARED.update(shared)
        out = []
        for i, c in enumerate(combos):
            out.append(_mm_worker(c))
            if progress is not None and (i % 8 == 0):
                import threading
                with (lock or threading.Lock()):
                    progress["done"] = i + 1
        if progress is not None:
            progress["done"] = len(combos)
        return out

    import multiprocessing as _mp
    import threading as _th
    methods = _mp.get_all_start_methods()
    method = os.environ.get("MM_OPT_START_METHOD") or (
        "fork" if "fork" in methods else "spawn" if "spawn" in methods else "")
    if method not in methods:               # بدونِ multiprocessing → ترتیبی
        return _mm_run_combos(combos, shared, progress, lock, 1)

    ctx = _mp.get_context(method)
    counter = ctx.Value("q", 0)
    shared = {**shared, "counter": counter}
    if method == "fork":
        _MM_SHARED.clear(); _MM_SHARED.update(shared)
        init, initargs = None, ()
    else:
        init, initargs = _mm_spawn_init, (shared,)

    poller_stop = _th.Event()

    def _poll():
        while not poller_stop.wait(0.4):
            if progress is not None:
                with (lock or _th.Lock()):
                    progress["done"] = counter.value
    poller = _th.Thread(target=_poll, daemon=True)
    if progress is not None:
        poller.start()

    out = []
    try:
        with ctx.Pool(processes=jobs, initializer=init, initargs=initargs) as pool:
            for r in pool.imap_unordered(_mm_worker, combos, chunksize=4):
                out.append(r)
    finally:
        poller_stop.set()
        _MM_SHARED.clear()
    if progress is not None:
        with (lock or _th.Lock()):
            progress["done"] = len(combos)
    return out
