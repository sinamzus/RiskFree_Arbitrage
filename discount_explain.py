# -*- coding: utf-8 -*-
"""Explain WHY a losing trade lost, from what actually happened to it.

For every losing trade of a backtest this module
  * splits the result into pieces that add up (NAV drift, change of the discount,
    spread, fees),
  * looks at the path between entry and exit (deepest discount, best/worst price,
    how much of a gain was given back),
  * compares with what the other selected funds' NAVs did over the same hours
    (market-wide move vs. fund-specific),
and turns that into one primary cause plus a list of contributing tags, in Persian.

The numbers are exact accounting; the wording of a *cause* is an interpretation
("probably stale NAV", "market-wide move") and is phrased as such.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right

# primary cause -> (short label, one-line meaning)
CAUSES = {
    "costs": ("هزینه سود را خورد", "قیمت کمی به نفع ما حرکت کرد ولی اسپرد و کارمزد بیشتر از آن بود"),
    "nav_fall": ("افت NAV (دارایی پایه)", "ارزش خودِ صندوق پایین آمد؛ قیمت با NAV همراه شد"),
    "widened": ("تخفیف بازتر شد", "قیمت نسبت به NAV ارزان‌تر شد؛ همگرایی رخ نداد"),
    "both": ("افت NAV و بازتر شدن تخفیف", "هر دو با هم علیه ما حرکت کردند"),
    "noise": ("نوسان بی‌علت", "هیچ‌کدام از اجزا به‌تنهایی توضیح نمی‌دهند"),
}


def _f(x: float, d: int = 2) -> str:
    return f"{x:+.{d}f}%"


def _key(d: int, t: int) -> int:
    return int(d) * 1_000_000 + int(t)


class _Fund:
    """Sorted (date,time) keys + rows of one fund for fast window lookups."""
    __slots__ = ("rows", "keys")

    def __init__(self, rows: list[tuple]):
        self.rows = rows
        self.keys = [_key(r[1], r[2]) for r in rows]

    def window(self, k0: int, k1: int) -> list[tuple]:
        return self.rows[bisect_left(self.keys, k0):bisect_right(self.keys, k1)]

    def nav_at(self, k: int):
        i = bisect_right(self.keys, k) - 1
        return self.rows[i][6] if i >= 0 else None


def explain_trade(t: dict, fund: _Fund | None, peers: list[_Fund], p) -> dict | None:
    """Explanation of one trade dict (as produced by asdict(Trade)); None for non-losers."""
    w = t["buy_notional"]
    if t["net_pnl"] > 0 or w <= 0 or t["entry_price"] <= 0 or t["nav_entry"] <= 0:
        return None
    hs = p.half_spread_pct / 100.0
    entry_mid = t["entry_price"] / (1.0 + hs)
    exit_mid = t["exit_price"] / (1.0 - hs)
    gm = exit_mid / entry_mid - 1.0                                  # mid-to-mid price move
    nav_c = t["nav_exit"] / t["nav_entry"] - 1.0                      # NAV drift while held
    conv = (1.0 + gm) / (1.0 + nav_c) - 1.0                           # change of price/NAV
    sp = 1.0 - (1.0 - hs) / (1.0 + hs)
    fee = t["fees"] / w
    net = t["net_pct"] / 100.0
    comp = {"gross_pct": gm * 100, "nav_pct": nav_c * 100, "convergence_pct": conv * 100,
            "spread_pct": -sp * 100, "fee_pct": -fee * 100, "net_pct": net * 100}

    # ---- path between entry and exit ------------------------------------------
    path = {}
    k0, k1 = _key(t["entry_date"], t["entry_time"]), _key(t["exit_date"], t["exit_time"])
    if fund is not None:
        ws = fund.window(k0, k1)
        if ws:
            lasts = [r[4] for r in ws]
            rels = [r[4] / r[3] - 1.0 for r in ws if r[3] > 0]
            lo_i = min(range(len(ws)), key=lambda i: ws[i][4])
            path = {
                "snapshots": len(ws),
                "worst_pct": (min(lasts) / entry_mid - 1.0) * 100,       # worst mid price vs entry
                "best_pct": (max(lasts) / entry_mid - 1.0) * 100,        # best mid price vs entry
                "deepest_rel_pct": min(rels) * 100 if rels else None,    # deepest discount vs fair value
                "peak_rel_pct": max(rels) * 100 if rels else None,
                "worst_date": ws[lo_i][1], "worst_time": ws[lo_i][2],
            }

    # ---- what the other funds' NAVs did over the same hours --------------------
    peer_chg = None
    ch = []
    for pf in peers:
        a, b = pf.nav_at(k0), pf.nav_at(k1)
        if a and b and a > 0:
            ch.append(b / a - 1.0)
    if ch:
        peer_chg = sum(ch) / len(ch) * 100

    # ---- primary cause ----------------------------------------------------------
    a_nav = max(0.0, -nav_c)
    b_conv = max(0.0, -conv)
    if gm > 0:
        code = "costs"
    elif a_nav + b_conv < 1e-6:
        code = "noise"
    elif a_nav >= 2 * b_conv:
        code = "nav_fall"
    elif b_conv >= 2 * a_nav:
        code = "widened"
    else:
        code = "both"

    # ---- contributing tags ------------------------------------------------------
    tags: list[dict] = []
    reason = t["exit_reason"]
    entry_rel = t["rel_entry_pct"]                                    # vs the fund's own fair value
    if reason == "stop":
        tags.append({"code": "stop", "text": f"حد ضرر فعال شد: تخفیف نسبت به لحظهٔ ورود از {_f(t['rel_entry_pct'])} "
                                              f"به {_f(t['rel_exit_pct'])} رسید"})
    elif reason == "time":
        tags.append({"code": "time", "text": f"سقف نگه‌داری ({p.max_hold_days} روز) تمام شد و تخفیف هنوز بسته نشده بود"})
    elif reason == "end":
        tags.append({"code": "end", "text": "داده تمام شد و پوزیشن باز بود؛ با قیمت آخرین لحظه بسته شد"})
    if t["hold_days"] == 0:
        tags.append({"code": "intraday", "text": "همان روز بسته شد؛ در بازهٔ کوتاه نوسان لحظه‌ای و اسپرد اثر بیشتری دارند"})

    edge = -entry_rel
    cost_pct = (sp + fee) * 100
    if edge < cost_pct:
        tags.append({"code": "weak_edge", "text": f"لبهٔ ورود کمتر از هزینه بود: تخفیف نسبت به معمول {edge:.2f}% "
                                                    f"ولی هزینهٔ رفت‌وبرگشت {cost_pct:.2f}% (آستانهٔ ورود را بالاتر ببرید یا هزینه را کم کنید)"})
    if t["base_pct"] <= -0.3 or t["base_pct"] >= 0.3:
        tags.append({"code": "perm_bubble", "text": f"این صندوق معمولاً {'زیر' if t['base_pct'] < 0 else 'بالای'} NAV معامله می‌شود "
                                                      f"(حباب معمول {_f(t['base_pct'])})؛ عدد خام {_f(t['disc_entry_pct'])} گمراه‌کننده بود"})
    if t.get("mr_score") is not None and t["mr_score"] < getattr(p, "mr_min_score", 70) + 10:
        tags.append({"code": "low_mr", "text": f"امتیاز بازگشت به میانگین هنگام ورود پایین بود ({t['mr_score']}؛ حداقل {p.mr_min_score:g})"})
    if peer_chg is not None and nav_c * 100 <= -0.15:
        if peer_chg <= -0.15:
            tags.append({"code": "market_wide", "text": f"افت NAV همگروه بود: NAV سایر صندوق‌های انتخاب‌شده هم در همین بازه {_f(peer_chg)} "
                                                          f"تغییر کرد (حرکت کل بازار/دسته، نه ضعف مخصوص این صندوق)"})
        else:
            tags.append({"code": "idio_nav", "text": f"افت NAV مخصوص این صندوق بود: NAV خودش {_f(nav_c * 100)} ولی سایر صندوق‌ها {_f(peer_chg)} "
                                                       f"(نشانهٔ NAV کهنه/بالا هنگام ورود: تخفیف ظاهری بود)"})
    if code in ("nav_fall", "both") and conv >= 0 and nav_c * 100 <= -0.15:
        tags.append({"code": "stale_nav", "text": "تخفیف بسته شد ولی از راه افت NAV، نه بالا رفتن قیمت؛ یعنی تخفیف ظاهری "
                                                    "(NAV لحظهٔ ورود بالاتر از ارزش واقعی دارایی بود)"})
    if path.get("best_pct") is not None and path["best_pct"] - gm * 100 >= cost_pct + 0.2 and path["best_pct"] > cost_pct:
        tags.append({"code": "gave_back", "text": f"در میانهٔ مسیر تا {_f(path['best_pct'])} سود ناخالص داشت ولی قیمت برگشت "
                                                    f"(خروج زودتر یا هدف خروج پایین‌تر شاید بهتر بود)"})
    if path.get("deepest_rel_pct") is not None and path["deepest_rel_pct"] < entry_rel - 0.3:
        tags.append({"code": "deeper", "text": f"پس از ورود تخفیف عمیق‌تر هم شد: تا {_f(path['deepest_rel_pct'])} "
                                                f"نسبت به معمول (ورود در {_f(entry_rel)})"})
    if t.get("idx_entry_pct") is not None and t.get("idx_exit_pct") is not None:
        di = t["idx_exit_pct"] - t["idx_entry_pct"]
        if di <= -0.2:
            tags.append({"code": "index_down", "text": f"شاخص حباب کل گروه هم بدتر شد ({_f(t['idx_entry_pct'])} → {_f(t['idx_exit_pct'])})"})

    # ---- one readable paragraph ---------------------------------------------------
    hold = "همان روز" if t["hold_days"] == 0 else f"{t['hold_days']} روز بعد"
    head = (f"خرید با قیمت {_f(t['disc_entry_pct'])} نسبت به NAV (منفی = تخفیف؛ نسبت به حباب معمولِ صندوق {_f(entry_rel)}) "
            f"و فروش {hold} با {_f(t['disc_exit_pct'])}.")
    body = (f"تغییر قیمت (میانی) {_f(gm * 100)} = تغییر NAV {_f(nav_c * 100)} و تغییر قیمت/NAV (همگرایی) {_f(conv * 100)}؛ "
            f"هزینه {_f(-(sp + fee) * 100)} (اسپرد {sp * 100:.2f}% + کارمزد {fee * 100:.2f}%) ← خالص {_f(net * 100)}.")
    label, meaning = CAUSES[code]
    why = f"علت اصلی: {label} — {meaning}."
    return {"code": code, "label": label, "text": " ".join([head, body, why]),
            "components": {k: round(v, 3) for k, v in comp.items()},
            "path": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in path.items()},
            "peer_nav_pct": round(peer_chg, 3) if peer_chg is not None else None,
            "tags": tags}


def explain_trades(trades: list[dict], loaded: list[dict], p) -> dict:
    """Attach ``why`` to every losing trade (in place) and return the cause summary."""
    funds = {l["label"]: _Fund(l["rows"]) for l in loaded}
    total_loss = 0.0
    groups: dict[str, dict] = {}
    tag_n: dict[str, dict] = {}
    n_lose = 0
    for t in trades:
        peers = [f for k, f in funds.items() if k != t["symbol"]]
        w = explain_trade(t, funds.get(t["symbol"]), peers, p)
        t["why"] = w
        if w is None:
            continue
        n_lose += 1
        loss = -t["net_pnl"]
        total_loss += loss
        g = groups.setdefault(w["code"], {"code": w["code"], "label": w["label"], "count": 0, "loss": 0.0})
        g["count"] += 1
        g["loss"] += loss
        for tg in w["tags"]:
            x = tag_n.setdefault(tg["code"], {"code": tg["code"], "count": 0, "loss": 0.0})
            x["count"] += 1
            x["loss"] += loss
    causes = sorted(groups.values(), key=lambda g: -g["loss"])
    for g in causes:
        g["share_pct"] = round(g["loss"] / total_loss * 100, 1) if total_loss else 0.0
        g["loss"] = round(g["loss"], 0)
        g["meaning"] = CAUSES[g["code"]][1]
    tags = sorted(tag_n.values(), key=lambda g: -g["count"])
    for x in tags:
        x["loss"] = round(x["loss"], 0)
    return {"losing_trades": n_lose, "total_trades": len(trades), "total_loss": round(total_loss, 0),
            "causes": causes, "tags": tags, "tag_labels": TAG_LABELS}


def attribute_trades(trades: list[dict], p, initial_capital: float | None = None) -> dict:
    """Where did the profit come from?  Every trade's net P&L is split into

      * ``nav_pnl``    — the fund's NAV moving between entry and exit, applied to the cost basis
                         (what holding the fund would have earned if its price/NAV ratio had not changed);
      * ``bubble_pnl`` — the rest of the gross move: the price/NAV ratio converging (or diverging);
      * ``spread``     — the assumed half-spreads paid on both sides (already inside the prices);
      * ``fees``       — broker / exchange fees,

    so that  nav_pnl + bubble_pnl − spread − fees = net  exactly (bubble_pnl is the residual, spread-free).
    Returns totals, per-symbol rows and a same-day vs longer split."""
    hs = p.half_spread_pct / 100.0
    tot = {"nav": 0.0, "bubble": 0.0, "spread": 0.0, "fees": 0.0, "net": 0.0, "cost": 0.0}
    by_sym: dict[str, dict] = {}
    by_len = {"same_day": {"n": 0, "net": 0.0, "wins": 0}, "longer": {"n": 0, "net": 0.0, "wins": 0}}
    for t in trades:
        buy, sell = float(t["buy_notional"]), float(t["sell_notional"])
        nav_e, nav_x = float(t.get("nav_entry") or 0), float(t.get("nav_exit") or 0)
        nav_pnl = buy * (nav_x / nav_e - 1.0) if nav_e > 0 and nav_x > 0 else 0.0
        si, so = t.get("spread_in"), t.get("spread_out")
        if si is not None and so is not None:
            # what was paid over / received under the mid at the fill (assumed spread, or the real book + its depth)
            spread = buy * si + sell * so
        else:
            spread = buy * hs / (1 + hs) + sell * hs / (1 - hs)
        fees = float(t["fees"])
        net = float(t["net_pnl"])
        bubble = net - nav_pnl + spread + fees
        for d_ in (tot, by_sym.setdefault(t["symbol"], {"nav": 0.0, "bubble": 0.0, "spread": 0.0, "fees": 0.0, "net": 0.0,
                                                         "cost": 0.0, "n": 0})):
            d_["nav"] += nav_pnl
            d_["bubble"] += bubble
            d_["spread"] += spread
            d_["fees"] += fees
            d_["net"] += net
            d_["cost"] += buy
        by_sym[t["symbol"]]["n"] += 1
        g = by_len["same_day" if t.get("hold_days", 1) == 0 else "longer"]
        g["n"] += 1
        g["net"] += net
        g["wins"] += 1 if net > 0 else 0
    cap = float(initial_capital or p.initial_capital or 0)
    net_t = tot["net"]

    def pct(x, base):
        return round(x / base * 100, 2) if base else None
    out = {
        "totals": {k: round(v, 0) for k, v in tot.items()},
        "pct_of_capital": {k: pct(tot[k], cap) for k in ("nav", "bubble", "spread", "fees", "net")},
        "share_of_net_pct": {k: pct(tot[k], net_t) for k in ("nav", "bubble", "spread", "fees")},
        "bubble_capture_pct_of_cost": pct(tot["bubble"], tot["cost"]),     # average gross convergence per rial invested
        "nav_move_pct_of_cost": pct(tot["nav"], tot["cost"]),
        "per_symbol": sorted(({"symbol": s, "trades": d_["n"], **{k: round(d_[k], 0) for k in ("nav", "bubble", "spread", "fees", "net")}}
                              for s, d_ in by_sym.items()), key=lambda r: -r["net"]),
        "same_day": {"trades": by_len["same_day"]["n"], "net": round(by_len["same_day"]["net"], 0),
                     "wins": by_len["same_day"]["wins"]},
        "longer": {"trades": by_len["longer"]["n"], "net": round(by_len["longer"]["net"], 0),
                   "wins": by_len["longer"]["wins"]},
    }
    return out


TAG_LABELS = {
    "stop": "خروج با حد ضرر", "time": "پایان سقف نگه‌داری", "end": "پایان داده با پوزیشن باز",
    "intraday": "بستن در همان روز", "weak_edge": "لبهٔ ورود کمتر از هزینه",
    "perm_bubble": "حباب دائمیِ صندوق", "low_mr": "امتیاز بازگشت پایین",
    "market_wide": "افت NAV همگروه (بازار)", "idio_nav": "افت NAV مخصوص صندوق",
    "stale_nav": "تخفیف ظاهری (NAV بالا)", "gave_back": "سود میانهٔ مسیر پس داده شد",
    "deeper": "تخفیف پس از ورود عمیق‌تر شد", "index_down": "شاخص گروه هم بدتر شد",
}
