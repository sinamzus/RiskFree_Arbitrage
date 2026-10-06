# -*- coding: utf-8 -*-
"""Comprehensive parameter study for the NAV-discount strategy.

One study answers, for EVERY modelled parameter at once:

  * which settings work (search: stratified random sampling of the whole space, then
    coordinate-descent refinement of the best few — on the training region only);
  * which parameters matter at all (importance = share of the variance of the results
    explained by the parameter, "eta squared"), and how each value behaves on average
    (marginal tables) and in pairs (heat maps);
  * whether the optimiser found something real or fitted noise (train-vs-hold-out rank
    correlation across ALL tried configurations, the hold-out percentile of the winner,
    stability of the neighbourhood around the winner);
  * what a trader who re-optimises period after period would really have earned
    (walk-forward, computed from per-block results — no extra simulation);
  * how fragile the winner is to the execution assumptions (spread, fees).

Method notes
  * The simulation is causal (baseline, mean-reversion score and bubble index only use
    the past), so each configuration is simulated ONCE over the whole period and its trades
    are assigned to time blocks by entry date; every block is replayed on its own capital.
  * The last ``holdout`` blocks are never used for selection or refinement.
  * Slow-to-build inputs (baseline window, NAV age, mean-reversion score, bubble index) are
    shared by many configurations: configurations are grouped by them so each universe is
    built once.
"""
from __future__ import annotations

import bisect
import itertools
import math
import random
import time
from dataclasses import asdict, replace

import discount_backtest as D
import discount_validation as V

# --------------------------------------------------------------------------- #
#  Search space                                                                #
# --------------------------------------------------------------------------- #

# kind: "slow" = changes the loaded universe (grouped), "fast" = only the simulation/replay
DIMS: dict[str, dict] = {
    "entry_mode":         {"kind": "slow", "label": "حالت ورود", "choices": ["fund", "index", "both"]},
    "baseline_days":      {"kind": "slow", "label": "تعدیل حباب دائمی (روز)", "choices": [0, 10, 20, 40, 60]},
    "max_nav_age_days":   {"kind": "slow", "label": "حداکثر سن NAV (روز)", "choices": [1, 2, 3, 5]},
    "mr_center":          {"kind": "slow", "label": "مرکز بازگشت به میانگین", "choices": ["off", "zero", "category", "self"]},
    "mr_window_days":     {"kind": "slow", "label": "پنجرهٔ امتیاز بازگشت (روز)", "choices": [10, 20, 30, 40]},
    "mr_horizon_days":    {"kind": "slow", "label": "افق بازگشت (روز)", "choices": [3, 5, 10]},
    "mr_min_score":       {"kind": "fast", "label": "حداقل امتیاز بازگشت", "choices": [50, 60, 70, 80, 90]},
    "entry_discount_pct": {"kind": "fast", "label": "ورود: تخفیف ≥ ٪", "choices": [0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0]},
    "exit_discount_pct":  {"kind": "fast", "label": "خروج: تخفیف ≤ ٪", "choices": [0.5, 0.25, 0.0, -0.25, -0.5]},
    "index_entry_pct":    {"kind": "fast", "label": "ورود: شاخص ≤ −٪", "choices": [0.1, 0.2, 0.3, 0.5, 1.0]},
    "index_exit_pct":     {"kind": "fast", "label": "خروج: شاخص ≥ −٪", "choices": [0.2, 0.1, 0.0, -0.1]},
    "max_hold_days":      {"kind": "fast", "label": "حداکثر نگه‌داری (روز)", "choices": [1, 2, 3, 5, 7, 10, 15, 30]},
    "stop_loss_pct":      {"kind": "fast", "label": "حد ضرر", "choices": [0.0, 1.0, 2.0, 3.0, 5.0]},
    "stop_mode":          {"kind": "fast", "label": "نوع حد ضرر", "choices": ["nav_widen", "nav_level", "price"]},
    "position_pct":       {"kind": "fast", "label": "سهم هر پوزیشن از سرمایه ٪", "choices": [5, 10, 20, 33, 50]},
}
SLOW_ORDER = ["entry_mode", "baseline_days", "max_nav_age_days", "mr_center", "mr_window_days", "mr_horizon_days"]
FAST_ORDER = ["entry_discount_pct", "exit_discount_pct", "index_entry_pct", "index_exit_pct", "max_hold_days",
              "stop_loss_pct", "stop_mode", "mr_min_score", "position_pct"]
ORDER = SLOW_ORDER + FAST_ORDER

PRESETS = {
    "quick": ["entry_discount_pct", "exit_discount_pct", "max_hold_days", "stop_loss_pct"],
    "medium": ["entry_discount_pct", "exit_discount_pct", "max_hold_days", "stop_loss_pct", "stop_mode",
               "baseline_days", "mr_center", "mr_min_score"],
    "full": ORDER,
}

OBJECTIVES = {
    "robust": "پایدار: میانگین بازدهِ بلوک‌ها − ½ انحراف معیارشان (پیش‌فرض)",
    "portfolio_return_pct": "بازده کل دورهٔ آموزش",
    "cagr_pct": "بازده سالانهٔ مرکب آموزش",
    "profit_factor": "Profit Factor آموزش",
}


def default_space() -> dict:
    return {k: list(v["choices"]) for k, v in DIMS.items()}


def _active(dim: str, c: dict) -> bool:
    """Is ``dim`` meaningful given the (partial) configuration ``c``?"""
    mode = c.get("entry_mode") or "fund"
    if dim == "entry_discount_pct":
        return mode in ("fund", "both")
    if dim == "exit_discount_pct":
        return mode in ("fund", "both")
    if dim == "index_entry_pct":
        return mode in ("index", "both")
    if dim == "index_exit_pct":
        return mode == "index"
    if dim == "stop_mode":
        return (c.get("stop_loss_pct") or 0) > 0
    if dim in ("mr_window_days", "mr_horizon_days", "mr_min_score"):
        return (c.get("mr_center") or "off") != "off"
    return True


def _valid(c: dict) -> bool:
    mode = c.get("entry_mode") or "fund"
    if mode in ("fund", "both") and c.get("entry_discount_pct") is not None and c.get("exit_discount_pct") is not None:
        if c["exit_discount_pct"] >= c["entry_discount_pct"] - 0.05:
            return False
    if mode == "index" and c.get("index_entry_pct") is not None and c.get("index_exit_pct") is not None:
        if c["index_exit_pct"] >= c["index_entry_pct"] - 0.02:
            return False
    return True


def _normalize(c: dict, base: D.DiscountParams) -> dict:
    """Inactive dims -> None (so equivalent configurations collapse)."""
    out = {}
    for d in ORDER:
        if d in c:
            out[d] = c[d]
    for d in ORDER:
        if d in out and not _active(d, out):
            out[d] = None
    return out


def _to_params(base: D.DiscountParams, c: dict) -> D.DiscountParams:
    kw = {k: v for k, v in c.items() if v is not None}
    if "max_hold_days" in kw:
        kw["max_hold_days"] = int(kw["max_hold_days"])
    return replace(base, **kw)


def _ckey(c: dict) -> tuple:
    return tuple((d, c.get(d)) for d in ORDER if d in c)


# --------------------------------------------------------------------------- #
#  Raw-data cache so many universes can be built cheaply                       #
# --------------------------------------------------------------------------- #

class _RawCache:
    """Wraps the Database: reads each fund's intraday rows ONCE (from the earliest warm-up
    any configuration needs) and serves date-sliced copies."""

    def __init__(self, db, min_start: int | None):
        self._db = db
        self._min_start = min_start
        self._rows: dict[int, tuple[list, list]] = {}
        self._dates: dict[tuple, list] = {}

    def __getattr__(self, name):
        return getattr(self._db, name)

    def get_nav_intraday(self, sid, start=None, end=None):
        ent = self._rows.get(sid)
        if ent is None:
            rows = self._db.get_nav_intraday(sid, self._min_start, None)
            ent = (rows, [r[0] for r in rows])
            self._rows[sid] = ent
        rows, ds = ent
        a = bisect.bisect_left(ds, start) if start else 0
        b = bisect.bisect_right(ds, end) if end else len(rows)
        return rows[a:b]

    def get_nav_intraday_dates(self, start=None, end=None):
        k = (start, end)
        if k not in self._dates:
            self._dates[k] = self._db.get_nav_intraday_dates(start, end)
        return self._dates[k]


# --------------------------------------------------------------------------- #
#  Small statistics helpers                                                    #
# --------------------------------------------------------------------------- #

def _num(x, nd=3):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd)


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0 + 1
        i = j + 1
    return r


def _spearman(a, b):
    if len(a) < 8:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = _mean(ra), _mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else 0.0


def _percentile_of(x, xs):
    if not xs:
        return None
    return sum(1 for v in xs if v < x) / len(xs) * 100.0


def _eta2(groups: dict) -> float | None:
    """Share of variance explained by group membership (0..1)."""
    allv = [v for g in groups.values() for v in g]
    if len(allv) < 8 or len(groups) < 2:
        return None
    m = _mean(allv)
    sst = sum((v - m) ** 2 for v in allv)
    if sst <= 1e-12:
        return 0.0
    ssb = sum(len(g) * (_mean(g) - m) ** 2 for g in groups.values() if g)
    return max(0.0, min(1.0, ssb / sst))


# --------------------------------------------------------------------------- #
#  The study                                                                   #
# --------------------------------------------------------------------------- #

class Study:
    def __init__(self, db, cats, symbols, start, end, base: D.DiscountParams, space: dict,
                 n_samples=400, blocks=6, holdout_frac=0.3, objective="robust", min_trades=30,
                 max_seconds=300, max_universes=20, seed=7, progress=None, lock=None):
        self.cats, self.symbols, self.start, self.end = cats, symbols, start, end
        self.base = base
        self.space = {k: [v for v in vs] for k, vs in space.items() if k in DIMS and vs}
        self.n_samples = max(20, int(n_samples))
        self.blocks_n = max(4, min(12, int(blocks)))
        self.holdout_frac = holdout_frac
        self.objective = objective if objective in OBJECTIVES else "robust"
        self.min_trades = int(min_trades)
        self.max_seconds = max(20, float(max_seconds))
        self.max_universes = max(1, int(max_universes))
        self.rng = random.Random(seed)
        self.progress, self.lock = progress, lock
        self.t0 = time.time()
        self.truncated = False
        self.cache_u: dict[tuple, dict] = {}
        self.cache_order: list[tuple] = []
        self.records: list[dict] = []
        self.by_key: dict[tuple, dict] = {}

    # ---- helpers --------------------------------------------------------------
    def _prog(self, **kw):
        if self.progress is not None:
            if self.lock:
                with self.lock:
                    self.progress.update(kw)
            else:
                self.progress.update(kw)

    def _time_left(self) -> bool:
        return time.time() - self.t0 < self.max_seconds

    def _setup(self):
        funds = D._universe(self.db_raw, self.cats, self.symbols)
        if not funds:
            raise ValueError("صندوقی برای مطالعه انتخاب نشده است.")
        dates = self.db_raw.get_nav_intraday_dates(self.start, self.end)
        if len(dates) < self.blocks_n * 3:
            raise ValueError(f"برای {self.blocks_n} بلوک زمانی دست‌کم {self.blocks_n * 3} روز دادهٔ معاملاتی لازم است (داریم {len(dates)}).")
        self.dates = dates
        n = len(dates)
        self.bounds = []
        for j in range(self.blocks_n):
            a = dates[j * n // self.blocks_n]
            b = dates[(j + 1) * n // self.blocks_n - 1]
            self.bounds.append((a, b))
        self.H = max(1, min(self.blocks_n - 2, int(round(self.blocks_n * self.holdout_frac))))
        self.T = self.blocks_n - self.H                    # first holdout block index
        self.train_rng = (dates[0], self.bounds[self.T - 1][1])
        self.hold_rng = (self.bounds[self.T][0], dates[-1])

    # ---- universe ---------------------------------------------------------------
    def _ukey(self, p: D.DiscountParams) -> tuple:
        return (p.baseline_days, p.max_nav_age_days, p.entry_mode != "fund", p.mr_center,
                p.mr_window_days if p.mr_center != "off" else 0,
                p.mr_horizon_days if p.mr_center != "off" else 0)

    def _universe(self, p: D.DiscountParams):
        k = self._ukey(p)
        U = self.cache_u.get(k)
        if U is None:
            U = V.load_universe(self.db_raw, self.cats, self.symbols, self.start, self.end, p)
            self.cache_u[k] = U
            self.cache_order.append(k)
            while len(self.cache_order) > 3:                 # keep memory bounded
                self.cache_u.pop(self.cache_order.pop(0), None)
        else:
            self.cache_order.remove(k)
            self.cache_order.append(k)
        return U

    # ---- evaluation ---------------------------------------------------------------
    def _summ(self, trades, p, lo, hi, with_t=False):
        sub = [t for t in trades if lo <= t.entry_date <= hi]
        acc, s, _c = D._portfolio_summary(sub, p, lo, hi)
        if with_t:
            nets = [t.net_pct for t in acc]
            n = len(nets)
            sd = _std(nets) * math.sqrt(n / (n - 1)) if n > 2 else 0.0
            s["t_stat"] = (_mean(nets) / (sd / math.sqrt(n))) if n >= 5 and sd > 1e-12 else None
        return s

    def _passive(self, U) -> dict:
        """Equal-weight buy-and-hold of the same funds over every block / region (no fees)."""
        def span(lo, hi):
            rs = []
            for it in U["items"]:
                a, b = bisect.bisect_left(it["dates"], lo), bisect.bisect_right(it["dates"], hi)
                if b - a >= 2:
                    rs.append(it["rows"][b - 1][4] / it["rows"][a][4] - 1.0)
            return _mean(rs) * 100 if rs else 0.0
        return {"blocks": [_num(span(lo, hi), 2) for lo, hi in self.bounds],
                "train": _num(span(*self.train_rng), 2), "hold": _num(span(*self.hold_rng), 2)}

    def evaluate(self, cfg: dict, src: str = "random", base_override: D.DiscountParams | None = None):
        """Simulate one configuration over the whole period; returns the record or None."""
        base = base_override or self.base
        c = _normalize(cfg, base)
        key = _ckey(c)
        if base_override is None and key in self.by_key:
            return self.by_key[key]
        p = _to_params(base, c)
        U = self._universe(p)
        if not U["items"]:
            return None
        trades = V._sim_all(U, p)
        blocks = []
        for lo, hi in self.bounds:
            s = self._summ(trades, p, lo, hi)
            blocks.append({"ret": s["portfolio_return_pct"], "n": s["trade_count"], "win": s["win_rate"]})
        tr = self._summ(trades, p, *self.train_rng)
        ho = self._summ(trades, p, *self.hold_rng, with_t=True)
        rets = [b["ret"] for b in blocks[:self.T]]
        robust = _mean(rets) - 0.5 * _std(rets)
        if self.objective == "robust":
            score = robust
        else:
            v = tr.get(self.objective)
            score = v if v is not None else -1e9
        rec = {"cfg": c, "src": src,
               "train": {k: tr.get(k) for k in ("portfolio_return_pct", "cagr_pct", "trade_count", "win_rate",
                                                "profit_factor", "max_drawdown_pct", "final_capital", "net_profit",
                                                "avg_exposure_pct")},
               "hold": {k: ho.get(k) for k in ("portfolio_return_pct", "cagr_pct", "trade_count", "win_rate",
                                               "profit_factor", "max_drawdown_pct", "final_capital", "net_profit",
                                               "avg_exposure_pct")},
               "hold_t": ho.get("t_stat"),
               "blocks": blocks, "robust": robust, "score": score,
               "ok": tr["trade_count"] >= self.min_trades}
        if base_override is None:
            self.by_key[key] = rec
            self.records.append(rec)
        return rec

    # ---- sampling -------------------------------------------------------------------
    def _slow_combos(self) -> list[dict]:
        combos = [{}]
        for d in SLOW_ORDER:
            ch = self.space.get(d)
            nxt = []
            for c in combos:
                if ch is None:
                    nxt.append(c)                                  # fixed at base
                elif not _active(d, {**self._base_cfg(), **c}):
                    nxt.append({**c, d: None})
                else:
                    for v in ch:
                        nxt.append({**c, d: v})
            combos = nxt
        return combos

    def _base_cfg(self) -> dict:
        b = self.base
        return {"entry_mode": b.entry_mode, "baseline_days": b.baseline_days, "max_nav_age_days": b.max_nav_age_days,
                "mr_center": b.mr_center, "mr_window_days": b.mr_window_days, "mr_horizon_days": b.mr_horizon_days,
                "mr_min_score": b.mr_min_score, "entry_discount_pct": b.entry_discount_pct,
                "exit_discount_pct": b.exit_discount_pct, "index_entry_pct": b.index_entry_pct,
                "index_exit_pct": b.index_exit_pct, "max_hold_days": b.max_hold_days,
                "stop_loss_pct": b.stop_loss_pct, "stop_mode": b.stop_mode, "position_pct": b.position_pct}

    def _fill(self, partial: dict) -> dict:
        """Complete a configuration with base values for dimensions that are not searched."""
        full = dict(self._base_cfg())
        full.update({k: v for k, v in partial.items() if v is not None or k in full})
        return full

    def _sample_fast(self, slow: dict, m: int) -> list[dict]:
        fast_dims = [d for d in FAST_ORDER if d in self.space]
        cols = {}
        for d in fast_dims:
            ch = self.space[d]
            col = []
            while len(col) < m:
                blk = list(ch)
                self.rng.shuffle(blk)
                col.extend(blk)
            col = col[:m]
            self.rng.shuffle(col)
            cols[d] = col
        out = []
        for i in range(m):
            cfg = self._fill(slow)
            for d in fast_dims:
                cfg[d] = cols[d][i]
            # a few resamples to satisfy "exit well below entry"
            if not _valid(cfg):
                for _ in range(6):
                    for d in ("exit_discount_pct", "index_exit_pct"):
                        if d in self.space:
                            cfg[d] = self.rng.choice(self.space[d])
                    if _valid(cfg):
                        break
            if _valid(cfg):
                out.append(cfg)
        return out

    # ---- run ---------------------------------------------------------------------------
    def run(self, db) -> dict:
        warm_p = replace(self.base, baseline_days=max([self.base.baseline_days] + self.space.get("baseline_days", [])),
                         mr_window_days=max([self.base.mr_window_days] + self.space.get("mr_window_days", [])),
                         mr_center="self" if "self" in self.space.get("mr_center", []) or self.base.mr_center == "self"
                         else self.base.mr_center)
        self.db_raw = _RawCache(db, D._warmup_start(self.start, warm_p) if self.start else None)
        self._prog(phase="setup", done=0, total=1)
        self._setup()

        combos = self._slow_combos()
        fast_dims = [d for d in FAST_ORDER if d in self.space]
        fast_size = 1
        for d in fast_dims:
            fast_size *= len(self.space[d])
        exhaustive = len(combos) * fast_size <= max(self.n_samples, 800) and len(combos) <= max(self.max_universes, 60)
        if not exhaustive and len(combos) > self.max_universes:
            self.rng.shuffle(combos)
            combos = combos[:self.max_universes]
        per = fast_size if exhaustive else max(4, self.n_samples // max(1, len(combos)))
        total = per * len(combos)
        self.exhaustive = exhaustive
        self._prog(phase="search", done=0, total=total + 40, combos=len(combos))

        # the reference configuration = the form's own values (always evaluated first)
        default_rec = self.evaluate(_normalize(self._fill({}), self.base), "default")
        self.passive = self._passive(self._universe(_to_params(self.base, default_rec["cfg"]))) if default_rec else \
            {"blocks": [], "train": None, "hold": None}
        done = 0
        search_end = self.t0 + self.max_seconds * 0.8        # keep 20% for refinement + analysis
        for ci, slow in enumerate(combos):
            # every structural combination gets an equal share of the time budget
            slice_end = time.time() + max(2.0, (search_end - time.time()) / max(1, len(combos) - ci))
            if exhaustive:
                cfgs = []
                for vals in itertools.product(*(self.space[d] for d in fast_dims)):
                    cfg = self._fill(slow)
                    cfg.update(dict(zip(fast_dims, vals)))
                    if _valid(cfg):
                        cfgs.append(cfg)
            else:
                cfgs = self._sample_fast(slow, per)
            for cfg in cfgs:
                if time.time() > slice_end and not exhaustive:
                    break
                if not self._time_left():
                    self.truncated = True
                    break
                self.evaluate(cfg, "random")
                done += 1
                if done % 5 == 0:
                    self._prog(done=done)
            if self.truncated:
                break
        random_recs = [r for r in self.records if r["src"] in ("random", "default")]
        self.min_trades_requested = self.min_trades
        qual = [r for r in random_recs if r["ok"]]
        if len(qual) < 20 and random_recs:
            # too few configurations reach the trade minimum: lower it (never below 5) instead of failing
            counts = sorted((r["train"]["trade_count"] for r in random_recs), reverse=True)
            m = max(5, min(self.min_trades, counts[min(20, len(counts)) - 1]))
            if m < self.min_trades:
                self.min_trades = m
                for r in self.records:
                    r["ok"] = r["train"]["trade_count"] >= m
                qual = [r for r in random_recs if r["ok"]]
        if len(random_recs) < 3:
            raise ValueError(f"فضای جستجو فقط {len(random_recs)} ترکیب متفاوت دارد؛ پارامتر یا مقدار بیشتری برای جستجو بدهید (پانل «۴) تنظیمات بهینه‌سازی»).")
        if len(qual) < 3:
            raise ValueError(f"فقط {len(qual)} ترکیب حتی {self.min_trades} معامله در آموزش داشتند (از {len(random_recs)} ترکیب). "
                             f"آستانه‌های ورود را سهل‌تر کنید، بازه یا دستهٔ بیشتری انتخاب کنید یا صندوق‌های بیشتری را فعال کنید.")

        # ---- refinement of the best few (training region only) -------------------------
        self._prog(phase="refine")
        seeds = sorted(qual, key=lambda r: -r["score"])[:3]
        for s in seeds:
            cur = s
            for _round in range(3):
                improved = False
                for d in ORDER:
                    if d not in self.space or d not in cur["cfg"] or cur["cfg"][d] is None:
                        continue
                    ch = self.space[d]
                    if cur["cfg"][d] not in ch:
                        continue
                    i = ch.index(cur["cfg"][d])
                    for j in (i - 1, i + 1):
                        if not (0 <= j < len(ch)) or not self._time_left():
                            continue
                        cand = dict(cur["cfg"])
                        cand[d] = ch[j]
                        cand = self._fill(cand)
                        cand = _normalize(cand, self.base)
                        if not _valid(cand):
                            continue
                        r = self.evaluate(cand, "refine")
                        done += 1
                        self._prog(done=min(done, total + 39))
                        if r and r["ok"] and r["score"] > cur["score"] + 1e-9:
                            cur, improved = r, True
                if not improved:
                    break

        allq = [r for r in self.records if r["ok"]]
        best = max(allq, key=lambda r: r["score"])
        self._prog(phase="analysis")
        return self._analyse(best, default_rec, qual, allq)

    # ---- analysis -------------------------------------------------------------------------
    def _fmt_cfg(self, c: dict) -> dict:
        return {k: v for k, v in c.items() if v is not None}

    def _row(self, r: dict) -> dict:
        return {"cfg": self._fmt_cfg(r["cfg"]), "src": r["src"], "score": _num(r["score"]), "ok": r["ok"],
                "train": {k: _num(v) for k, v in r["train"].items()},
                "hold": {k: _num(v) for k, v in r["hold"].items()}, "hold_t": _num(r.get("hold_t"), 2),
                "blocks": [_num(b["ret"], 2) for b in r["blocks"]],
                "block_n": [b["n"] for b in r["blocks"]]}

    def _analyse(self, best, default_rec, qual, allq) -> dict:
        hold_of = lambda r: r["hold"]["portfolio_return_pct"] or 0.0          # noqa: E731
        # --- generalisation of the search --------------------------------------------------
        sc = [r["score"] for r in qual]
        ho = [hold_of(r) for r in qual]
        rho = _spearman(sc, ho)
        pct = _percentile_of(hold_of(best), ho)
        pos_hold = sum(1 for x in ho if x > 0) / len(ho) * 100
        top_k = sorted(qual, key=lambda r: -r["score"])[:max(5, len(qual) // 10)]
        top_hold = _mean([hold_of(r) for r in top_k])

        # --- importance + marginals (random phase only, so the design is balanced) ------------
        imp, marg = [], {}
        for d in ORDER:
            if d not in self.space or len(self.space[d]) < 2:
                continue
            gs_t, gs_h = {}, {}
            rows = []
            for r in qual:
                v = r["cfg"].get(d)
                if v is None:
                    continue
                gs_t.setdefault(v, []).append(r["score"])
                gs_h.setdefault(v, []).append(hold_of(r))
            if len(gs_t) < 2:
                continue
            et, eh = _eta2(gs_t), _eta2(gs_h)
            imp.append({"dim": d, "label": DIMS[d]["label"], "eta_train": _num(et, 3), "eta_hold": _num(eh, 3),
                        "n_active": sum(len(g) for g in gs_t.values())})
            for v in self.space[d]:
                if v in gs_t:
                    rows.append({"value": v, "n": len(gs_t[v]),
                                 "train_mean": _num(_mean(gs_t[v]), 3), "hold_mean": _num(_mean(gs_h[v]), 3),
                                 "hold_median": _num(_median(gs_h[v]), 3),
                                 "hold_pos_pct": _num(sum(1 for x in gs_h[v] if x > 0) / len(gs_h[v]) * 100, 1)})
            marg[d] = rows
        imp.sort(key=lambda x: -((x["eta_train"] or 0) + (x["eta_hold"] or 0)))

        # --- pair heat maps for the three most important dims -------------------------------------
        pairs = []
        top_dims = [x["dim"] for x in imp if len(self.space[x["dim"]]) >= 3][:3]
        for i in range(len(top_dims)):
            for j in range(i + 1, len(top_dims)):
                a, b = top_dims[i], top_dims[j]
                cell = {}
                for r in qual:
                    va, vb = r["cfg"].get(a), r["cfg"].get(b)
                    if va is None or vb is None:
                        continue
                    cell.setdefault((va, vb), []).append(r)
                va_l = [v for v in self.space[a] if any(k[0] == v for k in cell)]
                vb_l = [v for v in self.space[b] if any(k[1] == v for k in cell)]
                grid = [[None if (va, vb) not in cell else
                         {"n": len(cell[(va, vb)]),
                          "train": _num(_mean([x["score"] for x in cell[(va, vb)]]), 2),
                          "hold": _num(_mean([hold_of(x) for x in cell[(va, vb)]]), 2)} for vb in vb_l] for va in va_l]
                pairs.append({"a": a, "b": b, "a_label": DIMS[a]["label"], "b_label": DIMS[b]["label"],
                              "a_values": va_l, "b_values": vb_l, "grid": grid})

        # --- neighbourhood of the winner -------------------------------------------------------------
        self._prog(phase="neighbours")
        nb = []
        for d in ORDER:
            if d not in self.space or best["cfg"].get(d) is None or best["cfg"][d] not in self.space[d]:
                continue
            ch = self.space[d]
            i = ch.index(best["cfg"][d])
            cand_idx = [j for j in (i - 1, i + 1) if 0 <= j < len(ch)]
            if DIMS[d]["kind"] == "slow" and isinstance(ch[0], str):
                cand_idx = [j for j in range(len(ch)) if j != i][:3]
            for j in cand_idx:
                cand = dict(best["cfg"])
                cand[d] = ch[j]
                cand = _normalize(self._fill(cand), self.base)
                if not _valid(cand):
                    continue
                r = self.evaluate(cand, "neighbour")
                if r is None:
                    continue
                nb.append({"dim": d, "label": DIMS[d]["label"], "from": best["cfg"][d], "to": ch[j],
                           "ok": r["ok"], "score": _num(r["score"]), "train": _num(r["train"]["portfolio_return_pct"]),
                           "hold": _num(hold_of(r)), "trades": r["train"]["trade_count"]})
        good = [x for x in nb if x["ok"] and x["score"] is not None and
                x["score"] >= 0.7 * best["score"] and (x["hold"] or 0) > 0]
        plateau = (len(good) / len(nb) * 100) if nb else None

        # --- walk-forward over the blocks (re-optimise each period) -----------------------------------
        pool = [r for r in qual]
        wf = []
        B = self.blocks_n
        for j in range(2, B):
            elig = [r for r in pool if sum(b["n"] for b in r["blocks"][:j]) >= max(5, self.min_trades * j // max(1, self.T))]
            if not elig:
                continue
            pick = max(elig, key=lambda r: _mean([b["ret"] for b in r["blocks"][:j]]) -
                       0.5 * _std([b["ret"] for b in r["blocks"][:j]]))
            wf.append({"block": j + 1, "range": list(self.bounds[j]), "holdout": j >= self.T,
                       "picked": self._fmt_cfg(pick["cfg"]),
                       "oos": _num(pick["blocks"][j]["ret"], 2),
                       "default": _num(default_rec["blocks"][j]["ret"], 2) if default_rec else None,
                       "median_cfg": _num(_median([r["blocks"][j]["ret"] for r in pool]), 2),
                       "passive": self.passive["blocks"][j] if self.passive.get("blocks") else None,
                       "hindsight_best": _num(best["blocks"][j]["ret"], 2)})
        wf_stats = None
        if wf:
            wf_stats = {"n_blocks": len(wf), "oos_mean": _num(_mean([w["oos"] for w in wf]), 2),
                        "default_mean": _num(_mean([w["default"] for w in wf if w["default"] is not None]), 2),
                        "median_cfg_mean": _num(_mean([w["median_cfg"] for w in wf]), 2),
                        "positive_blocks": sum(1 for w in wf if w["oos"] > 0)}

        # --- execution-assumption sensitivity of the winner ----------------------------------------------
        self._prog(phase="sensitivity")
        sens = []
        for name, kw in (("فرض‌های فعلی", {}),
                         ("اسپرد ×۲", {"half_spread_pct": self.base.half_spread_pct * 2}),
                         ("اسپرد ×۳", {"half_spread_pct": self.base.half_spread_pct * 3}),
                         ("کارمزد +۵۰٪", {"buy_fee": self.base.buy_fee * 1.5, "sell_fee": self.base.sell_fee * 1.5}),
                         ("سقف حجم نصف", {"participation_pct": (self.base.participation_pct / 2) if self.base.participation_pct > 0 else 2.5})):
            r = self.evaluate(best["cfg"], "sens", replace(self.base, **kw))
            if r:
                sens.append({"name": name, "train": _num(r["train"]["portfolio_return_pct"]),
                             "hold": _num(hold_of(r)), "trades": r["hold"]["trade_count"]})

        # --- verdict ------------------------------------------------------------------------------------
        lights = []

        def light(title, level, text):
            lights.append({"title": title, "level": level, "text": text})
        if rho is None:
            light("تعمیم‌پذیری جستجو", "warn", "نمونهٔ کافی برای سنجش همبستگی آموزش و آزمون نیست.")
        else:
            lv = "ok" if rho >= 0.25 else ("warn" if rho >= 0.1 else "bad")
            light("تعمیم‌پذیری جستجو", lv,
                  f"همبستگی رتبه‌ای (Spearman) بین امتیاز آموزش و بازدهٔ آزمون در {len(qual)} ترکیب: {rho:+.2f} "
                  "(ممکن است فقط از ساختار هزینه بیاید: ترکیب‌های پرمعامله در هر دو دوره بیشتر هزینه می‌دهند؛ با چراغ‌های بعدی بخوانید). "
                  + ("آنچه روی گذشته خوب بوده روی دادهٔ دیده‌نشده هم تا حدی خوب بوده." if lv == "ok" else
                     "ارتباط ضعیف است؛ بخش بزرگی از «بهترین»ها نویز است." if lv == "warn" else
                     "تقریباً هیچ؛ بهینه‌سازی روی نویز برازش شده و رتبه‌بندی آموزش اطلاعاتی دربارهٔ آینده ندارد."))
        ho_b = hold_of(best)
        pct = pct or 0.0
        tt = best.get("hold_t")
        weak_t = tt is not None and tt < 1.5
        lv = "ok" if (ho_b > 0 and pct >= 70 and not weak_t) else ("warn" if ho_b > 0 and pct >= 50 else "bad")
        light("برندهٔ آموزش در دادهٔ دیده‌نشده", lv,
              f"بازدهٔ آزمون {ho_b:+.2f}٪ (در دورهٔ آزمون {pos_hold:.0f}٪ از ترکیب‌ها سودده بودند)؛ "
              f"جایگاه برنده بین ترکیب‌ها: صدک {pct:.0f}. میانگین آزمونِ ۱۰٪ برترِ آموزش: {top_hold:+.2f}٪."
              + (f" آمارهٔ t تقریبیِ معاملات آزمون {tt:.1f}" + (" (کمتر از ۱٫۵: سود قابل‌تشخیص از نوسان نیست)." if weak_t else ".") if tt is not None else ""))
        pas = self.passive.get("hold")
        if pas is not None:
            lv2 = "ok" if ho_b >= pas and ho_b > 0 else ("warn" if ho_b > 0 else "bad")
            light("نسبت به پسیو (هم‌وزن، بدون کارمزد)", lv2,
                  f"در دورهٔ آزمون برنده {ho_b:+.2f}٪ و خرید و نگه‌داری هم‌وزنِ همین صندوق‌ها {pas:+.2f}٪ "
                  f"(سرمایهٔ درگیرِ استراتژی {(best['hold'].get('avg_exposure_pct') or 0):.0f}٪ از زمان). "
                  + ("استراتژی از حضور ساده در بازار بهتر است." if lv2 == "ok" else
                     "سود دارد ولی کمتر از پسیو است؛ اگر سرمایهٔ درگیر کم است، بازده به‌ازای هر ریالِ درگیر را هم ببینید." if lv2 == "warn" else
                     "ضرر دارد."))
        if plateau is None:
            light("پایداری اطراف برنده", "warn", "همسایه‌ای برای سنجش نبود.")
        else:
            lv = "ok" if plateau >= 60 else ("warn" if plateau >= 35 else "bad")
            light("پایداری اطراف برنده", lv,
                  f"{plateau:.0f}٪ از {len(nb)} همسایه (یک گام تغییر در یک پارامتر) هم نزدیک به برنده و در آزمون سودده‌اند. "
                  + ("ناحیهٔ هموار = قابل‌اعتماد." if lv == "ok" else "قله‌ای تیز و شکننده است." if lv == "bad" else "نیمه‌پایدار."))
        if wf_stats:
            lv = "ok" if (wf_stats["oos_mean"] > 0 and wf_stats["oos_mean"] > (wf_stats["default_mean"] or 0)
                          and wf_stats["positive_blocks"] >= wf_stats["n_blocks"] * 0.6) else \
                ("warn" if wf_stats["oos_mean"] > 0 else "bad")
            light("بازبهینه‌سازی دوره‌به‌دوره (walk-forward)", lv,
                  f"اگر هر دوره فقط با دادهٔ قبل از آن بهینه می‌شد: میانگین بازدهٔ هر بلوک {wf_stats['oos_mean']:+.2f}٪ "
                  f"(پارامتر پیش‌فرض {wf_stats['default_mean']:+.2f}٪، ترکیب میانه {wf_stats['median_cfg_mean']:+.2f}٪)؛ "
                  f"{wf_stats['positive_blocks']} از {wf_stats['n_blocks']} بلوک سودده.")
        if sens:
            x2 = next((s for s in sens if s["name"] == "اسپرد ×۲"), None)
            if x2 is not None:
                h2 = x2["hold"] or 0.0
                lv = "ok" if h2 > 0 else "bad"
                light("حساسیت به فرض اجرایی", lv,
                      f"آزمونِ برنده با اسپرد دو برابر: {h2:+.2f}٪ " + ("(هنوز سودده)." if lv == "ok" else
                      "(سود به فرض خوشبینانهٔ اسپرد وابسته است؛ دادهٔ اردربوک نداریم)."))
        levels = [l["level"] for l in lights]
        overall = "bad" if levels.count("bad") >= 2 else \
            ("ok" if levels.count("bad") == 0 and levels.count("ok") >= len(levels) - 1 else "warn")
        headline = {"ok": "نتیجهٔ بهینه‌سازی قابل‌اعتناست: چند آزمونِ مستقل هم‌جهت‌اند.",
                    "warn": "نتیجه نیمه‌معتبر است؛ فقط با احتیاط و پس از اعتبارسنجی کامل از آن استفاده کنید.",
                    "bad": "بهینه‌سازی نشانهٔ برازش بر نویز دارد؛ «بهترین ترکیب» را اعمال نکنید."}[overall]

        # --- plain-language findings -----------------------------------------------------------------------
        findings = []
        if self.min_trades < self.min_trades_requested:
            findings.append(f"کمتر از ۲۰ ترکیب به {self.min_trades_requested} معامله در آموزش رسیدند؛ «حداقل معامله» خودکار به {self.min_trades} کاهش یافت. "
                            "نتیجه با نمونهٔ معاملاتیِ کم ضعیف‌تر است؛ بازهٔ بلندتر یا صندوق بیشتر بگیرید.")
        if getattr(self, "exhaustive", False):
            findings.append("فضای جستجو کوچک بود و همهٔ ترکیب‌ها یکی‌یکی آزموده شد (نه نمونه‌گیری تصادفی).")
        if imp:
            t = imp[0]
            findings.append(f"مهم‌ترین پارامتر: «{t['label']}» (توضیح {((t['eta_train'] or 0) * 100):.0f}٪ از تغییرات امتیاز آموزش و "
                            f"{((t['eta_hold'] or 0) * 100):.0f}٪ از بازدهٔ آزمون).")
            weak = [x["label"] for x in imp if (x["eta_train"] or 0) < 0.02 and (x["eta_hold"] or 0) < 0.02]
            if weak:
                findings.append("پارامترهای بی‌اثر (کمتر از ۲٪ توضیح): " + "، ".join(weak) + " — ثابت نگه دارید و وقت صرف آن‌ها نکنید.")
        for d, rows in marg.items():
            vals = [r for r in rows if r["n"] >= 3]
            if len(vals) >= 3:
                bh = max(vals, key=lambda r: r["hold_mean"])
                bt = max(vals, key=lambda r: r["train_mean"])
                if bh["value"] != bt["value"]:
                    findings.append(f"«{DIMS[d]['label']}»: آموزش {bt['value']} را بهتر می‌داند ولی آزمون {bh['value']} را — ناهماهنگی نشانهٔ نویز است.")
        if rho is not None and rho < 0.1:
            findings.append("چون رتبه‌بندی آموزش با نتیجهٔ آزمون همبستگی ندارد، هر ترکیبِ «بهترین» اساساً شانسی است؛ تنها یافتهٔ قابل‌اعتماد اثرِ میانگینِ پارامترهاست (جدول‌های حاشیه‌ای).")

        top = sorted(allq, key=lambda r: -r["score"])[:20]
        scatter = [[_num(r["score"], 2), _num(hold_of(r), 2), 0] for r in qual[:1500]]
        scatter.append([_num(best["score"], 2), _num(hold_of(best), 2), 1])
        return {
            "verdict": {"overall": overall, "headline": headline, "lights": lights},
            "setup": {"funds": len(D._universe(self.db_raw, self.cats, self.symbols)),
                      "dates": [self.dates[0], self.dates[-1]], "blocks": [list(b) for b in self.bounds],
                      "train_blocks": self.T, "hold_blocks": self.H,
                      "train_range": list(self.train_rng), "hold_range": list(self.hold_rng),
                      "objective": self.objective, "objective_label": OBJECTIVES[self.objective],
                      "min_trades": self.min_trades, "min_trades_requested": self.min_trades_requested,
                      "exhaustive": getattr(self, "exhaustive", False), "n_random": len([r for r in self.records if r["src"] == "random"]),
                      "n_qualified": len(qual), "n_refine": len([r for r in self.records if r["src"] == "refine"]),
                      "n_universes": len(self.cache_order), "seconds": round(time.time() - self.t0, 1),
                      "truncated": self.truncated,
                      "searched": [d for d in ORDER if d in self.space],
                      "labels": {d: DIMS[d]["label"] for d in ORDER}},
            "best": self._row(best), "default": self._row(default_rec) if default_rec else None,
            "top": [self._row(r) for r in top],
            "generalisation": {"spearman": _num(rho, 3), "best_hold_percentile": _num(pct, 1),
                               "share_hold_positive_pct": _num(pos_hold, 1), "top_decile_hold_mean": _num(top_hold, 2),
                               "hold_median_all": _num(_median(ho), 2), "n": len(qual)},
            "importance": imp, "marginals": {d: v for d, v in marg.items()}, "pairs": pairs,
            "neighbours": {"rows": nb, "plateau_pct": _num(plateau, 1)},
            "walk_forward": {"rows": wf, "stats": wf_stats},
            "sensitivity": sens, "scatter": scatter, "findings": findings, "passive": self.passive,
        }


def run_study(db, cats=None, symbols=None, start=None, end=None, base: D.DiscountParams | None = None,
              space: dict | None = None, n_samples=400, blocks=6, test_frac=0.3, objective="robust",
              min_trades=30, max_seconds=300, max_universes=20, seed=7, progress=None, progress_lock=None) -> dict:
    base = base or D.DiscountParams()
    st = Study(db, cats, symbols, start, end, base, space or default_space(), n_samples=n_samples, blocks=blocks,
               holdout_frac=test_frac, objective=objective, min_trades=min_trades, max_seconds=max_seconds,
               max_universes=max_universes, seed=seed, progress=progress, lock=progress_lock)
    out = st.run(db)
    out["base_params"] = asdict(base)
    return out


_STR_CHOICES = {"entry_mode": {"fund", "index", "both"}, "mr_center": {"off", "zero", "category", "self"},
                "stop_mode": {"nav_widen", "nav_level", "price"}}


def clean_space(raw: dict | None, enabled: list[str] | None = None) -> dict:
    """Validate a user-supplied {dim: [values]} dict (unknown dims / bad values dropped)."""
    space = {}
    src = raw if raw else default_space()
    for d, vals in src.items():
        if d not in DIMS or (enabled is not None and d not in enabled):
            continue
        if not isinstance(vals, (list, tuple)):
            vals = [vals]
        out = []
        for v in vals:
            if d in _STR_CHOICES:
                if str(v) in _STR_CHOICES[d] and str(v) not in out:
                    out.append(str(v))
            else:
                try:
                    x = float(v)
                except (TypeError, ValueError):
                    continue
                if d in ("max_hold_days", "baseline_days", "max_nav_age_days", "mr_window_days", "mr_horizon_days"):
                    x = int(x)
                if x not in out:
                    out.append(x)
        if out:
            space[d] = out
    return space


def space_info() -> dict:
    return {"dims": [{"key": k, "label": DIMS[k]["label"], "kind": DIMS[k]["kind"], "choices": DIMS[k]["choices"]}
                     for k in ORDER],
            "presets": PRESETS, "objectives": OBJECTIVES}
