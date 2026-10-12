"""Compiled (numba) replay of the "always invested" ("hold") strategy — the optimizer's inner loop.

It is a line-by-line port of ``discount_backtest._replay`` for ``fill_mode == "hold"`` with no normal trades
(``_Parker.step`` / ``_switch_out`` / ``_close`` / ``flush`` / ``mtm``): same order of operations, same float
arithmetic, same tie-breaking (insertion order of the parked positions, stable sort of a group by its bubble), so
it produces EXACTLY the same trades and daily marks (verified by the test suite against the Python replay).

Everything that depends only on the data is packed once into numpy arrays and cached in the parker's ``pre`` dict;
what depends on a few parameters (eligibility, volume caps) is cached per parameter tuple. The rare case that needs
Python objects — the real order book of a held fund at the very instant it is sold to fund a switch — calls back
into Python (``objmode``).

Without numba (or if compiling fails) ``available()`` is False and callers keep using the Python replay.
"""

from __future__ import annotations

import math

import numpy as np

try:                                   # optional dependency
    from numba import njit, objmode
    _HAVE = True
except Exception:                      # pragma: no cover
    _HAVE = False

_BROKEN: list = []
_PACKS: dict = {}                      # id(rows) -> (rows, packed arrays): shared by copies of the same ``pre``                     # a compile / run failure switches the fast path off for this process


def available() -> bool:
    return _HAVE and not _BROKEN


# --------------------------------------------------------------------------- #
#  Packing the data (once per universe window)                                 #
# --------------------------------------------------------------------------- #

def _levels(book, d, t, side):
    import discount_backtest as D
    return D._book_side(book, d, t, side)


def pack(loaded: list[dict], pre: dict) -> dict:
    """Numpy arrays of everything the kernel reads that depends only on the data (cached in ``pre``)."""
    nb = pre.get("_nb")
    if nb is not None:
        return nb
    rows = pre["rows"]
    hit = _PACKS.get(id(rows))
    if hit is not None and hit[0] is rows:          # same data under a copied ``pre`` (e.g. a placebo's rank)
        pre["_nb"] = hit[1]
        return hit[1]
    import discount_backtest as D
    K = len(rows)
    nrows = np.array([len(rr) for rr in rows], dtype=np.int64)
    base = np.zeros(K, dtype=np.int64)
    if K > 1:
        base[1:] = np.cumsum(nrows)[:-1]
    R = int(nrows.sum())
    o = np.zeros(R, np.int64)
    d = np.zeros(R, np.int64)
    t = np.zeros(R, np.int64)
    sec = np.zeros(R, np.int64)
    fair = np.zeros(R)
    last = np.zeros(R)
    fresh = np.zeros(R, np.bool_)
    navr = np.zeros(R)
    rel = np.zeros(R)
    xb = np.zeros(R)
    xs = np.zeros(R)
    na = np.zeros(R, np.int64)
    nbid = np.zeros(R, np.int64)
    ap = np.zeros((R, 5))
    av = np.zeros((R, 5))
    bp = np.zeros((R, 5))
    bv = np.zeros((R, 5))
    has_bk = np.zeros(K, np.bool_)
    has_at = np.zeros(K, np.bool_)
    for k, rr in enumerate(rows):
        b0 = int(base[k])
        bk = loaded[k].get("book")
        has_bk[k] = bk is not None
        has_at[k] = bk is not None and hasattr(bk, "at")
        xbk, xsk = pre["xb"][k], pre["xs"][k]
        relk = pre["rel"][k]
        for i, r in enumerate(rr):
            j = b0 + i
            o[j], d[j], t[j] = r[0], r[1], r[2]
            sec[j] = D._sec(r[2])
            fair[j], last[j], fresh[j], navr[j] = r[3], r[4], bool(r[5]), r[6]
            rel[j] = relk[i]
            if xbk is not None:
                xb[j], xs[j] = xbk[i], xsk[i]
            if bk:
                a = _levels(bk, r[1], r[2], 1)
                if a:
                    na[j] = min(5, len(a))
                    for q in range(na[j]):
                        ap[j, q], av[j, q] = a[q][0], a[q][1]
                b = _levels(bk, r[1], r[2], 0)
                if b:
                    nbid[j] = min(5, len(b))
                    for q in range(nbid[j]):
                        bp[j, q], bv[j, q] = b[q][0], b[q][1]
    groups = pre["groups"]
    G = len(groups)
    g_T = np.zeros(G, np.int64)
    g_ptr = np.zeros(G + 1, np.int64)
    n_m = sum(len(g[1]) for g in groups)
    m_k = np.zeros(n_m, np.int64)
    m_i = np.zeros(n_m, np.int64)
    pos = 0
    for gi, (T, grp) in enumerate(groups):
        g_T[gi] = T
        g_ptr[gi] = pos
        for k, i in grp:
            m_k[pos], m_i[pos] = k, i
            pos += 1
    g_ptr[G] = pos
    # a row has more than 5 visible levels? (the kernel keeps 5 — the TSE book has exactly 5)
    st_lists = [sorted(getattr(l.get("day_vol"), "stale", None) or []) for l in loaded]
    st_ptr = np.zeros(K + 1, np.int64)
    for k, lst in enumerate(st_lists):
        st_ptr[k + 1] = st_ptr[k] + len(lst)
    st_ts = np.array([x for lst in st_lists for x in lst], dtype=np.int64) if st_ptr[K] else np.zeros(0, np.int64)
    navts = np.full(R, -1, np.int64)
    for k, rr in enumerate(rows):
        nt = getattr(loaded[k].get("day_vol"), "navts", None) or {}
        if nt:
            b0 = int(base[k])
            for i, r in enumerate(rr):
                v = nt.get(r[0] * 86400 + D._sec(r[2]))
                if v is not None:
                    navts[b0 + i] = v
    nb = {"navts": navts, "st_ptr": st_ptr, "st_ts": st_ts, "K": K, "R": R, "nrows": nrows, "base": base, "o": o, "d": d, "t": t, "sec": sec, "fair": fair,
          "last": last, "fresh": fresh, "navr": navr, "rel": rel, "xb": xb, "xs": xs, "na": na, "nbid": nbid,
          "ap": ap, "av": av, "bp": bp, "bv": bv, "has_bk": has_bk, "has_at": has_at,
          "g_T": g_T, "g_ptr": g_ptr, "m_k": m_k, "m_i": m_i, "_per": {}}
    pre["_nb"] = nb
    if len(_PACKS) >= 16:
        _PACKS.pop(next(iter(_PACKS)))
    _PACKS[id(rows)] = (rows, nb)
    return nb


def _flat(series: list, nb: dict, none=np.nan) -> np.ndarray:
    out = np.full(nb["R"], none)
    for k, s in enumerate(series):
        if s is None:
            continue
        b0 = int(nb["base"][k])
        for i, v in enumerate(s):
            if v is not None:
                out[b0 + i] = v
    return out


def _per_params(nb: dict, loaded: list[dict], crash, p) -> tuple:
    """Eligibility (except freshness) and volume caps per row — cached per the few parameters they depend on."""
    import discount_backtest as D
    key = (p.fill_max_rel_pct, p.mr_center != "off", p.mr_min_score, p.crash_drop_pct if crash is not None else 0.0,
           p.participation_pct, p.participation_basis)
    hit = nb["_per"].get(key)
    if hit is not None:
        return hit
    R = nb["R"]
    if "mr" not in nb:
        nb["mr"] = _flat([l.get("mr") for l in loaded], nb)
        nb["mr_none"] = np.array([l.get("mr") is None for l in loaded], np.bool_)
        nb["crash"] = _flat(crash, nb) if crash is not None else None
    last_row = np.zeros(R, np.bool_)
    last_row[nb["base"] + nb["nrows"] - 1] = True
    ok = ~last_row.copy()                       # the bubble limit is checked in the kernel (depends on the size)
    if p.mr_center != "off":
        mr = nb["mr"]
        bad = np.isnan(mr) | (mr < p.mr_min_score)
        ok &= ~bad
    if crash is not None and p.crash_drop_pct > 0 and nb["crash"] is not None:
        cr = nb["crash"]
        thr = -p.crash_drop_pct / 100.0
        ok &= ~(~np.isnan(cr) & (cr <= thr))
    cap = np.zeros(R)
    for k, l in enumerate(loaded):
        dv = l["day_vol"]
        b0 = int(nb["base"][k])
        for i in range(int(nb["nrows"][k])):
            j = b0 + i
            cap[j] = D._vol_cap(dv, p, int(nb["d"][j]), int(nb["t"][j]))
    hit = (ok, cap)
    nb["_per"][key] = hit
    return hit


# --------------------------------------------------------------------------- #
#  The rare Python call: the held fund's real book at the instant it is sold   #
# --------------------------------------------------------------------------- #

_CTX: dict = {}
_LAST: dict = {}                       # final capital of the latest run_hold call


def _at_bids(k, at_ts, buf):
    """buf[0:5] bid prices, buf[5:10] bid volumes, buf[10] best ask; returns the number of bid levels, or -1 when
    the book at that instant is not two-sided (the row's own book is used then, as in ``_Parker._close``)."""
    import discount_backtest as D
    bk = _CTX["books"][int(k)]
    d2, t2 = D._ts_to_dt(int(at_ts))
    st = bk.at(d2, t2)
    if not (st[0] and st[1]):
        return -1
    n = min(5, len(st[0]))
    for q in range(n):
        buf[q] = st[0][q][0]
        buf[5 + q] = st[0][q][1]
    buf[10] = st[1][0][0]
    return n


# --------------------------------------------------------------------------- #
#  Kernel                                                                      #
# --------------------------------------------------------------------------- #

if _HAVE:
    @njit(cache=True)
    def _fill(px, vol, n, units):
        left = units
        cost = 0.0
        got = 0.0
        worst = 0.0
        for q in range(n):
            if left <= 0:
                break
            v = vol[q]
            qq = left if left < v else v
            cost += qq * px[q]
            got += qq
            left -= qq
            worst = px[q]
        avg = cost / got if got > 0 else 0.0
        return got, avg, worst

    @njit(cache=True)
    def _lim_units(px, vol, n, lim_px):
        """Same as discount_backtest._lim_units: most units with an average price at or below lim_px."""
        cost = 0.0
        got = 0.0
        for q in range(n):
            p_ = px[q]
            v = vol[q]
            if p_ <= lim_px:
                cost += v * p_
                got += v
                continue
            room = lim_px * got - cost
            if room <= 0:
                break
            qq = room / (p_ - lim_px)
            if qq >= v:
                cost += v * p_
                got += v
                continue
            got += qq
            break
        return got

    @njit(cache=True)
    def _units_for_cash(px, vol, n, need, sf, total):
        """Same as discount_backtest._units_for_cash: units to sell line by line for ``need`` net proceeds."""
        left = need
        got = 0.0
        worst = 0.0
        for q in range(n):
            if left <= 0 or got >= total:
                break
            per = px[q] * (1 - sf)
            qq = left / per
            if qq > vol[q]:
                qq = vol[q]
            if qq > total - got:
                qq = total - got
            got += qq
            left -= qq * per
            worst = px[q]
        if left > 0 and got < total and worst > 0:
            qq = left / (worst * (1 - sf))
            got = got + qq if qq < total - got else total
        return got

    @njit(cache=True)
    def _units_to_sell(rr, total, need, slot, use_buf, buf, nbuf, has_bk_j, bp, bv, nbid, last, hs, sf):
        """Same as _Parker._units_to_sell (``use_buf``: the bids of the book at the sale instant, in buf)."""
        if use_buf:
            u = _units_for_cash(buf[0:5], buf[5:10], nbuf, need, sf, total)
        elif has_bk_j and nbid[rr] > 0:
            u = _units_for_cash(bp[rr], bv[rr], nbid[rr], need, sf, total)
        else:
            per = last[rr] * (1 - hs) * (1 - sf)
            if per > 0:
                u = need / per
                if u > total:
                    u = total
            else:
                u = total
        if (total - u) * last[rr] < 0.25 * slot:
            u = total
        return u

    @njit(cache=True)
    def _buy_adj(r, slot, bf, has_bk_k, ap, av, na, cap, last, fair, rel, max_rel):
        """Same as _Parker._buy_adj: (correction, unit limit, price ok) — the average price of buying the intended
        position, never below last; or, when that is too dear, of the part that keeps the average within the limit."""
        rv = rel[r]
        if not has_bk_k or na[r] <= 0 or fair[r] <= 0:
            return 0.0, np.inf, rv <= max_rel
        a1 = ap[r, 0]
        want = slot / (a1 * (1 + bf))
        if cap[r] < want:
            want = cap[r]
        got, avg, _w = _fill(ap[r], av[r], na[r], want)
        px = avg if got > 0 else a1
        x = (px - last[r]) / fair[r]
        x = x if x > 0.0 else 0.0
        if rv + x <= max_rel or rv > max_rel:
            return x, np.inf, rv + x <= max_rel
        lim = _lim_units(ap[r], av[r], na[r], fair[r] * (1 + max_rel))
        if lim <= 0:
            return x, np.inf, False
        g2, a2, _w = _fill(ap[r], av[r], na[r], lim)
        if g2 * a2 * (1 + bf) < 0.25 * slot:
            return x, np.inf, False
        x2 = (a2 - last[r]) / fair[r]
        return (x2 if x2 > 0.0 else 0.0), lim, True

    @njit(cache=True)
    def _sell_adj(r, units, has_bk_k, bp, bv, nbid, last, fair):
        """Same as _Parker._sell_adj: average price of selling ``units`` (beyond the depth at the worst bid)."""
        if not has_bk_k or nbid[r] <= 0 or fair[r] <= 0:
            return 0.0
        got, avg, worst = _fill(bp[r], bv[r], nbid[r], units)
        if got < units:
            avg = (avg * got + worst * (units - got)) / units
        x = (last[r] - avg) / fair[r]
        return x if x > 0.0 else 0.0

    @njit(cache=True)
    def _stale(j, T, cur_ts, st_ptr, st_ts, cur_i, base, navts, max_nav_s):
        """Fund j's NAV is stale at T: older than the limit at T, or its latest snapshot was dropped for it."""
        if max_nav_s > 0 and cur_i[j] >= 0:
            nt = navts[base[j] + cur_i[j]]
            if nt >= 0 and T - nt > max_nav_s:
                return True
        lo = st_ptr[j]
        hi = st_ptr[j + 1]
        if lo == hi:
            return False
        a, b = lo, hi                      # last index with st_ts <= T
        while a < b:
            m = (a + b) // 2
            if st_ts[m] <= T:
                a = m + 1
            else:
                b = m
        x = a - 1
        return x >= lo and st_ts[x] > cur_ts[j]

    @njit(cache=True)
    def _xi(k, i, not_before, dl, req_fresh, base, nrows, d, fresh, o, sec):
        if dl <= 0:
            return i
        b0 = base[k]
        day = d[b0 + i]
        n = 0
        for j in range(i + 1, nrows[k]):
            r = b0 + j
            if d[r] != day:
                return -1
            if req_fresh and not fresh[r]:
                continue
            n += 1
            if n >= dl and (not_before < 0 or o[r] * 86400 + sec[r] >= not_before):
                return j
        return -1

    @njit(cache=True)
    def _kernel(K, nrows, base, o, d, t, sec, fair, last, fresh, navr, rank, rel, xs, na, nbid, ap, av, bp, bv,
                has_bk, has_at, g_T, g_ptr, m_k, m_i, okst, cap, st_ptr, st_ts, navts, max_nav_s,
                req_fresh, hs, bf, sf, position_pct, switch, max_rel, min_hold_s, max_age_s, dl, cash0, start_ts, end_ts,
                tr_i, tr_f, mk_i, mk_f):
        # trade records: tr_i[n] = (k, sig_row, entry_row, exit_row, at_ts, reason, seq) ; tr_f[n] = (units, cost_part,
        # ask, px, mid_in, mid_out)    marks: mk_i[n] = (date, elapsed) ; mk_f[n] = (equity, exp_area)
        cash = cash0
        invested = 0.0
        exp_area = 0.0
        last_ts = start_ts
        parked = np.zeros(K, np.bool_)
        p_units = np.zeros(K)
        p_cost = np.zeros(K)
        p_ask = np.zeros(K)
        p_ts = np.zeros(K, np.int64)
        p_mid = np.zeros(K)
        p_sig = np.zeros(K, np.int64)
        p_ent = np.zeros(K, np.int64)
        p_seq = np.zeros(K, np.int64)
        seq = 0
        cur_i = np.full(K, -1, np.int64)
        cur_ts = np.zeros(K, np.int64)
        cand_T = np.full(K, -1, np.int64)
        cand_v = np.zeros(K)
        cand_lim = np.full(K, np.inf)
        n_tr = 0
        n_mk = 0
        mk_day = -1
        mk_date = -1
        buf = np.zeros(11)
        order = np.zeros(2 * K + 2, np.int64)
        keys = np.zeros(K + 1)
        G = g_T.shape[0]
        for g in range(G):
            T = g_T[g]
            a0 = g_ptr[g]
            a1 = g_ptr[g + 1]
            if T // 86400 != mk_day:
                if mk_day >= 0:
                    if T > last_ts:
                        eq_ = cash + invested
                        frac = (invested / eq_) if eq_ > 0 else 0.0
                        exp_area += frac * (T - last_ts)
                        last_ts = T
                    # liquidation value of the book (insertion order)
                    v = cash
                    npk = 0
                    for j in range(K):
                        if parked[j]:
                            order[npk] = j
                            npk += 1
                    for x in range(1, npk):          # sort by insertion sequence
                        y = order[x]
                        z = x - 1
                        while z >= 0 and p_seq[order[z]] > p_seq[y]:
                            order[z + 1] = order[z]
                            z -= 1
                        order[z + 1] = y
                    for x in range(npk):
                        j = order[x]
                        ci = cur_i[j]
                        if ci < 0:
                            v += p_cost[j]
                            continue
                        rr = base[j] + ci
                        bid = bp[rr, 0] if (has_bk[j] and nbid[rr] > 0) else last[rr] * (1 - hs)
                        v += p_units[j] * bid * (1 - sf)
                    mk_i[n_mk, 0] = mk_date
                    mk_i[n_mk, 1] = last_ts - start_ts
                    mk_f[n_mk, 0] = v
                    mk_f[n_mk, 1] = exp_area
                    n_mk += 1
                mk_day = T // 86400
            mk_date = d[base[m_k[a0]] + m_i[a0]]
            if T > last_ts:
                eq_ = cash + invested
                frac = (invested / eq_) if eq_ > 0 else 0.0
                exp_area += frac * (T - last_ts)
                last_ts = T
            # ---- step ----
            for a in range(a0, a1):
                k = m_k[a]
                cur_i[k] = m_i[a]
                cur_ts[k] = T
            # the fund's data ends here: sell what is still parked
            for a in range(a0, a1):
                k = m_k[a]
                i = m_i[a]
                if parked[k] and i == nrows[k] - 1:
                    # _close(k, units, i, "end")
                    r = base[k] + i
                    units = p_units[k]
                    if has_bk[k] and nbid[r] > 0:
                        got, px, wpx = _fill(bp[r], bv[r], nbid[r], units)
                        if got < units:
                            px = (px * got + wpx * (units - got)) / units
                    else:
                        px = last[r] * (1 - hs)
                    b_ = bp[r, 0] if (has_bk[k] and nbid[r] > 0) else 0.0
                    a_ = ap[r, 0] if (has_bk[k] and na[r] > 0) else 0.0
                    mid = (b_ + a_) / 2.0 if (b_ > 0 and a_ > b_) else last[r]
                    frac = units / p_units[k]
                    cost_part = p_cost[k] * frac
                    sell_n = units * px
                    tr_i[n_tr, 0] = k
                    tr_i[n_tr, 1] = p_sig[k]
                    tr_i[n_tr, 2] = p_ent[k]
                    tr_i[n_tr, 3] = r
                    tr_i[n_tr, 4] = -1
                    tr_i[n_tr, 5] = 0
                    tr_f[n_tr, 0] = units
                    tr_f[n_tr, 1] = cost_part
                    tr_f[n_tr, 2] = p_ask[k]
                    tr_f[n_tr, 3] = px
                    tr_f[n_tr, 4] = p_mid[k]
                    tr_f[n_tr, 5] = mid
                    n_tr += 1
                    p_units[k] -= units
                    p_cost[k] -= cost_part
                    if p_units[k] <= 1e-9:
                        parked[k] = False
                    cash, invested = cash + sell_n * (1 - sf), invested + (-cost_part)
            # which funds are candidates right now — at the average price of buying the intended position
            slot_c = position_pct / 100.0 * (cash + invested) * (1 + bf)
            for a in range(a0, a1):
                k = m_k[a]
                r = base[k] + m_i[a]
                if req_fresh and not fresh[r]:
                    continue
                xa, lim_, okp = _buy_adj(r, slot_c, bf, has_bk[k], ap, av, na, cap, last, fair, rel, max_rel)
                cand_lim[k] = lim_
                if okst[r] and okp:
                    cand_T[k] = T
                    cand_v[k] = rank[r] + xa
                else:
                    cand_T[k] = -1
            # entries: stable sort of the group by (candidate value or rank)
            ng = a1 - a0
            for x in range(ng):
                a = a0 + x
                k = m_k[a]
                order[x] = a
                keys[x] = cand_v[k] if cand_T[k] != -1 else rank[base[k] + m_i[a]]
            for x in range(1, ng):
                y = order[x]
                ky = keys[x]
                z = x - 1
                while z >= 0 and keys[z] > ky:
                    order[z + 1] = order[z]
                    keys[z + 1] = keys[z]
                    z -= 1
                order[z + 1] = y
                keys[z + 1] = ky
            ent = order[:ng].copy()
            for x in range(ng):
                a = ent[x]
                k = m_k[a]
                i = m_i[a]
                if cand_T[k] == -1 or cand_T[k] != T or parked[k]:
                    continue
                cv = cand_v[k]
                better = False
                for k2 in range(K):
                    if k2 == k or cand_T[k2] == -1 or T - cand_T[k2] > max_age_s or parked[k2]:
                        continue
                    if _stale(k2, T, cur_ts, st_ptr, st_ts, cur_i, base, navts, max_nav_s):
                        continue
                    if cand_v[k2] < cv - 1e-12:
                        better = True
                        break
                if better:
                    continue
                r = base[k] + i
                lim = cand_lim[k]
                slot = position_pct / 100.0 * (cash + invested) * (1 + bf)
                amount = slot if slot < cash else cash
                sale_ts = -1
                if slot > 0 and amount < 0.25 * slot:
                    cap_pre = cap[r]
                    if lim < cap_pre:
                        cap_pre = lim
                    if has_bk[k]:
                        sv = 0.0
                        for q in range(na[r]):
                            sv += av[r, q]
                        if sv < cap_pre:
                            cap_pre = sv
                    a1p = ap[r, 0] if (has_bk[k] and na[r] > 0) else last[r] * (1 + hs)
                    if cap_pre * a1p * (1 + bf) < 0.25 * slot:
                        continue
                    # the cash the buy needs: the intended order at its average price
                    if has_bk[k] and na[r] > 0:
                        want = slot / (ap[r, 0] * (1 + bf))
                        if cap[r] < want:
                            want = cap[r]
                        if lim < want:
                            want = lim
                        g_, a_n, _w = _fill(ap[r], av[r], na[r], want)
                        need = g_ * a_n * (1 + bf)
                    else:
                        ask0 = last[r] * (1 + hs)
                        want = slot / (ask0 * (1 + bf))
                        if cap[r] < want:
                            want = cap[r]
                        need = want * ask0 * (1 + bf)
                    need -= cash
                    # ---- _switch_out(k, i, T, need) ----
                    npk = 0
                    for j in range(K):
                        if parked[j]:
                            order[ng + npk] = j
                            npk += 1
                    for x2 in range(1, npk):
                        y = order[ng + x2]
                        z = x2 - 1
                        while z >= 0 and p_seq[order[ng + z]] > p_seq[y]:
                            order[ng + z + 1] = order[ng + z]
                            z -= 1
                        order[ng + z + 1] = y
                    worst = -1
                    w_rel = 0.0
                    for x2 in range(npk):
                        j = order[ng + x2]
                        if j == k or cur_i[j] < 0 or T - cur_ts[j] > max_age_s:
                            continue
                        if min_hold_s != 0 and T - p_ts[j] < min_hold_s:
                            continue
                        if _stale(j, T, cur_ts, st_ptr, st_ts, cur_i, base, navts, max_nav_s):
                            continue
                        rr = base[j] + cur_i[j]
                        u_ = _units_to_sell(rr, p_units[j], need, slot, False, buf, 0, has_bk[j], bp, bv, nbid,
                                            last, hs, sf)
                        rv = rank[rr] - _sell_adj(rr, u_, has_bk[j], bp, bv, nbid, last, fair)
                        if worst < 0 or rv > w_rel:
                            worst = j
                            w_rel = rv
                    do_sale = need > 0 and not (worst < 0 or cand_v[k] > w_rel - switch)
                    sale_i = -1
                    at_ts = -1
                    if do_sale:
                        if dl > 0:
                            ja = _xi(worst, cur_i[worst], -1, dl, req_fresh, base, nrows, d, fresh, o, sec)
                            if ja < 0:
                                do_sale = False
                            else:
                                ra = base[worst] + ja
                                ta = o[ra] * 86400 + sec[ra]
                                if _xi(k, i, ta, dl, req_fresh, base, nrows, d, fresh, o, sec) < 0:
                                    do_sale = False
                                else:
                                    sale_ts = ta
                                    sale_i = ja
                        else:
                            sale_i = cur_i[worst]
                            at_ts = T
                    if do_sale:
                        # the sale must free enough cash for the new position (never sell for nothing)
                        rs0 = base[worst] + sale_i
                        if cash + p_units[worst] * last[rs0] * (1 - hs) * (1 - sf) < 0.25 * slot:
                            do_sale = False
                            sale_ts = -1
                    if do_sale:
                        # ---- _close(worst, units, sale_i, "rotate", at_ts) ----
                        rs = base[worst] + sale_i
                        use_st = 0
                        if at_ts >= 0 and has_at[worst]:
                            with objmode(nlev="int64"):
                                nlev = _at_bids(worst, at_ts, buf)
                            if nlev > 0:
                                use_st = nlev
                        # only as much as the buy needs, line by line over the bids the sale meets
                        units = _units_to_sell(rs, p_units[worst], need, slot, use_st > 0, buf, use_st, has_bk[worst],
                                               bp, bv, nbid, last, hs, sf)
                        if use_st > 0:
                            got, px, wpx = _fill(buf[0:5], buf[5:10], use_st, units)
                            if got < units:
                                px = (px * got + wpx * (units - got)) / units
                            b_ = buf[0]
                            a_ = buf[10]
                        else:
                            if has_bk[worst] and nbid[rs] > 0:
                                got, px, wpx = _fill(bp[rs], bv[rs], nbid[rs], units)
                                if got < units:
                                    px = (px * got + wpx * (units - got)) / units
                            else:
                                px = last[rs] * (1 - hs)
                            b_ = bp[rs, 0] if (has_bk[worst] and nbid[rs] > 0) else 0.0
                            a_ = ap[rs, 0] if (has_bk[worst] and na[rs] > 0) else 0.0
                        mid = (b_ + a_) / 2.0 if (b_ > 0 and a_ > b_) else last[rs]
                        frac = units / p_units[worst]
                        cost_part = p_cost[worst] * frac
                        sell_n = units * px
                        tr_i[n_tr, 0] = worst
                        tr_i[n_tr, 1] = p_sig[worst]
                        tr_i[n_tr, 2] = p_ent[worst]
                        tr_i[n_tr, 3] = rs
                        tr_i[n_tr, 4] = at_ts
                        tr_i[n_tr, 5] = 1
                        tr_f[n_tr, 0] = units
                        tr_f[n_tr, 1] = cost_part
                        tr_f[n_tr, 2] = p_ask[worst]
                        tr_f[n_tr, 3] = px
                        tr_f[n_tr, 4] = p_mid[worst]
                        tr_f[n_tr, 5] = mid
                        n_tr += 1
                        p_units[worst] -= units
                        p_cost[worst] -= cost_part
                        if p_units[worst] <= 1e-9:
                            parked[worst] = False
                        cash, invested = cash + sell_n * (1 - sf), invested + (-cost_part)
                    slot = position_pct / 100.0 * (cash + invested) * (1 + bf)
                    amount = slot if slot < cash else cash
                if slot <= 0 or amount < 0.25 * slot:
                    continue
                jb = _xi(k, i, sale_ts, dl, req_fresh, base, nrows, d, fresh, o, sec)
                if jb < 0:
                    continue
                rb = base[k] + jb
                t_in = o[rb] * 86400 + sec[rb]
                ask = last[rb] * (1 + hs)
                cap_u = cap[rb]
                units = amount / (ask * (1 + bf))
                if cap_u < units:
                    units = cap_u
                if has_bk[k]:
                    a1b = ap[rb, 0] if na[rb] > 0 else ask
                    want = amount / (a1b * (1 + bf))
                    if cap_u < want:
                        want = cap_u
                    if lim < want:
                        want = lim
                    if na[rb] > 0:
                        units, ask, _w = _fill(ap[rb], av[rb], na[rb], want)
                    else:
                        units, ask = want, last[rb] * (1 + hs)
                    if units > 0 and units * ask * (1 + bf) > amount:
                        want = amount / (ask * (1 + bf))
                        if na[rb] > 0:
                            units, ask, _w = _fill(ap[rb], av[rb], na[rb], want)
                        else:
                            units, ask = want, last[rb] * (1 + hs)
                if units <= 0 or units * ask * (1 + bf) < 0.25 * slot:
                    continue
                b_ = bp[rb, 0] if (has_bk[k] and nbid[rb] > 0) else 0.0
                a_ = ap[rb, 0] if (has_bk[k] and na[rb] > 0) else 0.0
                parked[k] = True
                p_units[k] = units
                p_cost[k] = units * ask
                p_ask[k] = ask
                p_ts[k] = t_in
                p_mid[k] = (b_ + a_) / 2.0 if (b_ > 0 and a_ > b_) else last[rb]
                p_sig[k] = r
                p_ent[k] = rb
                seq += 1
                p_seq[k] = seq
                cash -= units * ask * (1 + bf)
                invested += units * ask
        # ---- flush (insertion order) ----
        npk = 0
        for j in range(K):
            if parked[j]:
                order[npk] = j
                npk += 1
        for x in range(1, npk):
            y = order[x]
            z = x - 1
            while z >= 0 and p_seq[order[z]] > p_seq[y]:
                order[z + 1] = order[z]
                z -= 1
            order[z + 1] = y
        for x in range(npk):
            k = order[x]
            if cur_i[k] < 0:
                continue
            r = base[k] + nrows[k] - 1
            units = p_units[k]
            if has_bk[k] and nbid[r] > 0:
                got, px, wpx = _fill(bp[r], bv[r], nbid[r], units)
                if got < units:
                    px = (px * got + wpx * (units - got)) / units
            else:
                px = last[r] * (1 - hs)
            b_ = bp[r, 0] if (has_bk[k] and nbid[r] > 0) else 0.0
            a_ = ap[r, 0] if (has_bk[k] and na[r] > 0) else 0.0
            mid = (b_ + a_) / 2.0 if (b_ > 0 and a_ > b_) else last[r]
            frac = units / p_units[k]
            cost_part = p_cost[k] * frac
            sell_n = units * px
            tr_i[n_tr, 0] = k
            tr_i[n_tr, 1] = p_sig[k]
            tr_i[n_tr, 2] = p_ent[k]
            tr_i[n_tr, 3] = r
            tr_i[n_tr, 4] = -1
            tr_i[n_tr, 5] = 0
            tr_f[n_tr, 0] = units
            tr_f[n_tr, 1] = cost_part
            tr_f[n_tr, 2] = p_ask[k]
            tr_f[n_tr, 3] = px
            tr_f[n_tr, 4] = p_mid[k]
            tr_f[n_tr, 5] = mid
            n_tr += 1
            p_units[k] -= units
            p_cost[k] -= cost_part
            if p_units[k] <= 1e-9:
                parked[k] = False
            cash, invested = cash + sell_n * (1 - sf), invested + (-cost_part)
        final = cash + invested
        if end_ts > last_ts:
            eq_ = cash + invested
            frac = (invested / eq_) if eq_ > 0 else 0.0
            exp_area += frac * (end_ts - last_ts)
            last_ts = end_ts
        if mk_date >= 0:
            mk_i[n_mk, 0] = mk_date
            mk_i[n_mk, 1] = last_ts - start_ts
            mk_f[n_mk, 0] = final
            mk_f[n_mk, 1] = exp_area
            n_mk += 1
        return n_tr, n_mk, final


# --------------------------------------------------------------------------- #
#  Driver                                                                      #
# --------------------------------------------------------------------------- #

def final_capital(fill: dict, p, d0: int, d1: int) -> float | None:
    """Final capital of the "hold" replay (what the placebo test needs), or None without the fast path."""
    acc = run_hold(fill, p, d0, d1, lite=True)
    return None if acc is None else _LAST.get("final")


class LiteTrade:
    """What the optimizer reads of a trade (same values as the full ``Trade``)."""
    __slots__ = ("entry_date", "net_pnl", "net_pct")

    def __init__(self, entry_date, net_pnl, net_pct):
        self.entry_date, self.net_pnl, self.net_pct = entry_date, net_pnl, net_pct


def run_hold(fill: dict, p, d0: int, d1: int, marks: list | None = None, lite: bool = False):
    """Fast equivalent of ``_replay([], p, d0, d1, fill=fill, marks=marks)[0]`` for ``fill_mode == "hold"``:
    returns the accepted (parked) trades, appends the daily marks. None when the fast path is unavailable.
    ``lite``: only entry date / net P&L / net % per trade (``LiteTrade``) — what the optimizer needs."""
    if not available() or p.fill_mode != "hold":
        return None
    import discount_backtest as D
    loaded = fill["loaded"]
    pre = fill.get("pre") or D._Parker.prepare(loaded)
    if not loaded or not pre["groups"]:
        return None
    try:
        nb = pack(loaded, pre)
        okst, cap = _per_params(nb, loaded, fill.get("crash"), p)
        rank = nb["rel"] if pre.get("rank") is None else _flat(pre["rank"], nb, 0.0)
        R = nb["R"]
        tr_i = np.zeros((R + 8, 6), np.int64)
        tr_f = np.zeros((R + 8, 6))
        G = len(pre["groups"])
        mk_i = np.zeros((G + 2, 2), np.int64)
        mk_f = np.zeros((G + 2, 2))
        _CTX["books"] = [l.get("book") for l in loaded]
        start_ts = D._ord(d0) * 86400
        end_ts = (D._ord(d1) + 1) * 86400
        n_tr, n_mk, _final = _kernel(
            nb["K"], nb["nrows"], nb["base"], nb["o"], nb["d"], nb["t"], nb["sec"], nb["fair"], nb["last"],
            nb["fresh"], nb["navr"], rank, nb["rel"], nb["xs"], nb["na"], nb["nbid"], nb["ap"], nb["av"], nb["bp"],
            nb["bv"], nb["has_bk"], nb["has_at"], nb["g_T"], nb["g_ptr"], nb["m_k"], nb["m_i"], okst, cap,
            nb["st_ptr"], nb["st_ts"], nb["navts"], max(0, int(p.max_nav_age_min)) * 60,
            bool(p.require_fresh), p.half_spread_pct / 100.0, p.buy_fee, p.sell_fee, float(p.position_pct),
            p.fill_switch_pct / 100.0, p.fill_max_rel_pct / 100.0, max(0, int(p.fill_min_hold_min)) * 60, max(1, int(p.fill_quote_age_min)) * 60,
            max(0, int(p.exec_delay_snaps)), float(p.initial_capital), start_ts, end_ts, tr_i, tr_f, mk_i, mk_f)
    except Exception as e:                # never fail a study because of the fast path: fall back to Python
        _BROKEN.append(repr(e))
        return None
    finally:
        _CTX.pop("books", None)
    _LAST["final"] = float(_final)
    if marks is not None:
        for x in range(n_mk):
            marks.append((int(mk_i[x, 0]), float(mk_f[x, 0]), float(mk_f[x, 1]), int(mk_i[x, 1])))
    if lite:
        return _lite(nb, p, tr_i[:n_tr], tr_f[:n_tr])
    return _trades(nb, loaded, pre, p, tr_i[:n_tr], tr_f[:n_tr])


def _lite(nb, p, tr_i, tr_f) -> list:
    bf, sf = p.buy_fee, p.sell_fee
    d = nb["d"]
    out = []
    for x in range(tr_i.shape[0]):
        units, cost_part, _ask, px = float(tr_f[x, 0]), float(tr_f[x, 1]), float(tr_f[x, 2]), float(tr_f[x, 3])
        sell_n = units * px
        bfee, sfee = cost_part * bf, sell_n * sf
        net = sell_n - sfee - (cost_part + bfee)
        out.append(LiteTrade(int(d[int(tr_i[x, 2])]), round(net, 0),
                             round(net / (cost_part + bfee) * 100, 4) if cost_part + bfee else 0))
    return out


def _trades(nb, loaded, pre, p, tr_i, tr_f) -> list:
    """Trade objects exactly as ``_Parker._close`` builds them."""
    import discount_backtest as D
    bf, sf = p.buy_fee, p.sell_fee
    mr_on = p.mr_center != "off"
    out = []
    labels = [l["label"] for l in loaded]
    for x in range(tr_i.shape[0]):
        k, rs, re_, rx, at_ts, reason = (int(v) for v in tr_i[x])
        units, cost_part, ask, px, mid_in, mid_out = (float(v) for v in tr_f[x])
        b0 = int(nb["base"][k])
        i_sig = rs - b0
        e = pre["rows"][k][re_ - b0]
        xr = pre["rows"][k][rx - b0]
        o_e, d_e, t_e, fair_e, _l, _f, navr_e = e
        o, d, t, nav, _last, _f2, nav_raw = xr
        if at_ts >= 0:
            d, t = D._ts_to_dt(at_ts)
            o = D._ord(d)
        mrk = loaded[k].get("mr")
        mr_score = round(mrk[i_sig], 1) if mr_on and mrk and mrk[i_sig] is not None else None
        sell_n = units * px
        bfee, sfee = cost_part * bf, sell_n * sf
        net = sell_n - sfee - (cost_part + bfee)
        out.append(D.Trade(
            symbol=labels[k], entry_date=d_e, entry_time=t_e, exit_date=d, exit_time=t,
            volume=int(units), entry_price=round(ask, 2), exit_price=round(px, 2),
            nav_entry=round(navr_e, 2), nav_exit=round(nav_raw, 2),
            disc_entry_pct=round((ask / navr_e - 1) * 100, 4) if navr_e else 0,
            disc_exit_pct=round((px / nav_raw - 1) * 100, 4) if nav_raw else 0,
            rel_entry_pct=round((ask / fair_e - 1) * 100, 4) if fair_e else 0,
            rel_exit_pct=round((px / nav - 1) * 100, 4) if nav else 0,
            base_pct=round((fair_e / navr_e - 1) * 100, 4) if navr_e else 0,
            mr_score=mr_score, buy_notional=round(cost_part, 0), sell_notional=round(sell_n, 0),
            fees=round(bfee + sfee, 0), net_pnl=round(net, 0),
            net_pct=round(net / (cost_part + bfee) * 100, 4) if cost_part + bfee else 0,
            hold_days=max(0, D._ord(d) - o_e), exit_reason="rotate" if reason == 1 else "end", origin="fill",
            spread_in=(1 - mid_in / ask) if mid_in and ask > 0 else None,
            spread_out=(mid_out / px - 1) if mid_out > 0 and px > 0 else None))
    return out
