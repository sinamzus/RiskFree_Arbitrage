"""Capacity ("volume") study: the same strategy and parameters, only the capital changes.

Every run trades against the real order book (buys never beyond the 5 visible ask levels, the volume cap as set in
the form); the table shows how the return, the share of the intended size actually filled and the cost of eating
into the book change with the capital — i.e. up to what size the strategy keeps working.
"""

from __future__ import annotations

from dataclasses import replace

DEFAULT_CAPITALS = [1e9, 5e9, 10e9, 20e9, 50e9, 100e9]


def run_volume_study(db, cats, symbols, start, end, base, capitals: list[float] | None = None,
                     progress: dict | None = None) -> dict:
    import discount_backtest as D
    caps = sorted({float(c) for c in (capitals or DEFAULT_CAPITALS) if c and c > 0})
    if not caps:
        raise ValueError("هیچ مقدار سرمایه‌ای داده نشده است.")
    rows, bench = [], None
    for k, cap in enumerate(caps):
        if progress is not None:
            progress.update(phase=f"سرمایهٔ {cap / 1e9:g} میلیارد ریال", done=k, total=len(caps))
        res = D.run_discount_backtest(db, cats, symbols, start, end, replace(base, initial_capital=cap))
        s = res["summary"]
        si = s.get("size_info") or {}
        tr = res["trades"]
        buy_n = sum(t["buy_notional"] for t in tr)
        sell_n = sum(t["sell_notional"] for t in tr)
        att = (res.get("attribution") or {}).get("totals") or {}
        bh = (res.get("benchmark") or {}).get("buyhold") or {}
        bench = bench or bh.get("return_pct")
        si_in = [t["spread_in"] for t in tr if t.get("spread_in") is not None]
        si_out = [t["spread_out"] for t in tr if t.get("spread_out") is not None]
        rows.append({
            "capital": cap, "final_capital": s.get("final_capital"), "return_pct": s.get("portfolio_return_pct"),
            "cagr_pct": s.get("cagr_pct"), "max_dd_pct": s.get("max_drawdown_pct"), "trades": len(tr),
            "exposure_pct": s.get("avg_exposure_pct"),
            "avg_size_ratio_pct": si.get("avg_size_ratio_pct"), "small_positions_pct": si.get("small_positions_pct"),
            "limited_by_volume": si.get("limited_by_volume"), "switch_blocked_by_volume": si.get("switch_blocked_by_volume"),
            "limited_by_price": si.get("limited_by_price"), "switch_partial_sales": si.get("switch_partial_sales"),
            "sales_over_depth": si.get("sales_over_depth"), "book_sales": si.get("book_sales"),
            "sale_value_over_depth_pct": si.get("sale_value_over_depth_pct"),
            "depth_cost_pct": round(att["spread"] / (buy_n + sell_n) * 100, 4) if att.get("spread") is not None and buy_n + sell_n else None,
            "spread_in_avg_pct": round(sum(si_in) / len(si_in) * 100, 4) if si_in else None,
            "spread_out_avg_pct": round(sum(si_out) / len(si_out) * 100, 4) if si_out else None,
            "fees": s.get("total_fees"), "buyhold_pct": bh.get("return_pct"),
            "dataset": (res.get("dataset") or {}).get("name"),
        })
    if progress is not None:
        progress.update(phase="پایان", done=len(caps), total=len(caps))
    ref = rows[0]["return_pct"] or 0.0
    capacity = None
    for r in rows:
        r["vs_smallest_pct"] = round((r["return_pct"] or 0) - ref, 3)
        if ref > 0 and (r["return_pct"] or 0) >= 0.9 * ref:
            capacity = r["capital"]
    beats = [r["capital"] for r in rows if bench is not None and (r["return_pct"] or -1e9) > bench]
    return {"rows": rows, "buyhold_pct": bench, "capacity_90pct": capacity,
            "max_capital_beating_buyhold": max(beats) if beats else None,
            "params": {k: getattr(base, k) for k in ("dataset", "fill_mode", "position_pct", "participation_pct",
                                                    "participation_basis", "fill_switch_pct")}}
