"""PCR dashboard data (docs/PCR_METHODOLOGY.md): every PCR indicator now and 5 / 15 / 30 minutes ago, its
trend, the last 15 minutes' OI flow (possible writing vs buying), the combined read from the methodology's
table, key strikes and an intraday series for the trend chart.

Past values come from the intraday chain snapshots (options_service.intraday_history, same expiry).  The
"now" values are computed from the same compact strike rows as the past ones, so a trend is never an
artefact of comparing a full chain with a partial one.  Single readings never produce a read: a trend needs
at least two horizons.  Interpretations describe positioning; they are not forecasts.
"""
from __future__ import annotations

import time

import config

HORIZONS = (5, 15, 30)                     # minutes
FLOW_STRIKES = 11                          # ATM ± 5 for the 15-minute flow


def _weights() -> list[float]:
    return [float(x) for x in str(config.PCR_WEIGHTS).split(",")]


def ratios(rows: dict, spot: float, n: int | None = None) -> dict:
    """OI / near / weighted PCR and volume totals from compact rows {strike: {ce_oi, pe_oi, ce_vol, …}}."""
    if not rows:
        return {}
    n = n or config.PCR_NEAR_STRIKES
    ks = sorted(rows)
    ce = sum((rows[k].get("ce_oi") or 0) for k in ks)
    pe = sum((rows[k].get("pe_oi") or 0) for k in ks)
    i = min(range(len(ks)), key=lambda j: abs(ks[j] - spot))
    half = n // 2
    win = ks[max(i - half, 0): i + half + 1]
    w = _weights()
    wt = {k: w[min(abs(ks.index(k) - i), len(w) - 1)] for k in win}
    nce = sum((rows[k].get("ce_oi") or 0) for k in win)
    npe = sum((rows[k].get("pe_oi") or 0) for k in win)
    wce = sum((rows[k].get("ce_oi") or 0) * wt[k] for k in win)
    wpe = sum((rows[k].get("pe_oi") or 0) * wt[k] for k in win)
    vols = [rows[k].get("ce_vol") for k in ks] + [rows[k].get("pe_vol") for k in ks]
    has_vol = all(v is not None for v in vols)
    cev = sum(rows[k].get("ce_vol") or 0 for k in ks) if has_vol else None
    pev = sum(rows[k].get("pe_vol") or 0 for k in ks) if has_vol else None
    return {"oi": pe / ce if ce else None, "near": npe / nce if nce else None, "wtd": wpe / wce if wce else None,
            "ce_vol": cev, "pe_vol": pev, "vol": (pev / cev) if cev else None, "atm": ks[i], "window": win}


def clean(history: list[dict], expiry: str, cur_rows: dict) -> list[dict]:
    """Snapshots fit to compare: same expiry, market hours, near the usual number of strikes with OI, and
    total OI within ±30 % of the session's median (partial or stale chains produce false PCR swings)."""
    import statistics
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))          # fixed offset: no tz database needed on the phone
    hs = []
    for h in history:
        if h.get("expiry") != expiry or not h.get("rows"):
            continue
        t = datetime.fromtimestamp(h["ts"], ist)
        if (t.hour, t.minute) < (9, 15) or (t.hour, t.minute) > (15, 30):
            continue
        hs.append(h)
    if not hs:
        return []
    live = lambda rows: sum(1 for r in rows.values() if (r.get("ce_oi") or 0) > 0 and (r.get("pe_oi") or 0) > 0)
    tot = lambda rows: sum((r.get("ce_oi") or 0) + (r.get("pe_oi") or 0) for r in rows.values())
    n_med = statistics.median([live(h["rows"]) for h in hs] + [live(cur_rows)])
    t_med = statistics.median([tot(h["rows"]) for h in hs] + [tot(cur_rows)])
    return [h for h in hs if live(h["rows"]) >= 0.8 * n_med and t_med and abs(tot(h["rows"]) / t_med - 1) <= 0.3]


def _pick(history: list[dict], expiry: str, target: float, tol: float) -> dict | None:
    c = [h for h in history if h.get("expiry") == expiry and abs(h["ts"] - target) <= tol]
    return min(c, key=lambda h: abs(h["ts"] - target)) if c else None


def trend(cur: float | None, past: dict[int, float | None]) -> str:
    """rising / falling / stable / rapid, from the 5 / 15 / 30-minute changes (needs two horizons)."""
    if cur is None:
        return "—"
    d = {h: cur - v for h, v in past.items() if v is not None}
    if len(d) < 2:
        return "building history"
    band = config.PCR_STABLE_BAND
    if 5 in d and abs(d[5]) >= 2 * band:
        return "rapid rise" if d[5] > 0 else "rapid fall"
    longer = [v for h, v in d.items() if h >= 15]
    if longer and all(v > band for v in longer) and d.get(5, 0) >= -band / 2:
        return "rising"
    if longer and all(v < -band for v in longer) and d.get(5, 0) <= band / 2:
        return "falling"
    return "stable"


def flow(now_rows: dict, then_rows: dict, spot: float) -> dict | None:
    """OI added near ATM over the window, split by premium change (possible writing / buying)."""
    if not then_rows:
        return None
    near = sorted(now_rows, key=lambda k: abs(k - spot))[:FLOW_STRIKES]
    f = {"pe_writing": 0.0, "pe_buying": 0.0, "ce_writing": 0.0, "ce_buying": 0.0, "pe_cut": 0.0, "ce_cut": 0.0}
    for k in near:
        if k not in then_rows:
            continue
        for side in ("pe", "ce"):
            d_oi = (now_rows[k].get(f"{side}_oi") or 0) - (then_rows[k].get(f"{side}_oi") or 0)
            d_px = (now_rows[k].get(f"{side}_ltp") or 0) - (then_rows[k].get(f"{side}_ltp") or 0)
            if d_oi > 0 and d_px < 0:
                f[f"{side}_writing"] += d_oi
            elif d_oi > 0 and d_px > 0:
                f[f"{side}_buying"] += d_oi
            elif d_oi < 0:
                f[f"{side}_cut"] += -d_oi
    added = f["pe_writing"] + f["pe_buying"] + f["ce_writing"] + f["ce_buying"]
    base = sum((now_rows[k].get("pe_oi") or 0) + (now_rows[k].get("ce_oi") or 0) for k in near) or 1
    f["added"] = added
    f["active"] = added / base >= 0.004
    f["balance"] = ((f["pe_writing"] + f["ce_buying"]) - (f["ce_writing"] + f["pe_buying"])) / added if added else None
    return f


def combined_read(pcr_trend: str, fl: dict | None) -> tuple[str, str]:
    """The methodology's table: PCR trend (weighted near-ATM, the single positioning measure) × OI flow."""
    if pcr_trend in ("—", "building history") or fl is None:
        return "No read", "needs at least 15–30 minutes of snapshots"
    if not fl.get("active"):
        return ("Potentially sideways", "PCR stable and little fresh OI near the money") if pcr_trend == "stable" \
            else ("No read", f"PCR {pcr_trend} but little fresh OI near the money")
    pw, pb, cw, cb = fl["pe_writing"], fl["pe_buying"], fl["ce_writing"], fl["ce_buying"]
    up, down = pcr_trend in ("rising", "rapid rise"), pcr_trend in ("falling", "rapid fall")
    if up and pw > max(pb, cw):
        return "Bullish lean", "PCR rising with put writing"
    if down and cw > max(cb, pw):
        return "Bearish lean", "PCR falling with call writing"
    if up and pb > pw:
        return "Potentially bearish", "PCR rising with put buying (protection demand)"
    if down and cb > cw:
        return "Potentially bullish", "PCR falling with call buying"
    bal = fl.get("balance") or 0
    if pcr_trend == "stable" and abs(bal) < config.FLOW_MIN_SHARE:
        return "Potentially sideways", "PCR stable, writing and buying balanced"
    if pcr_trend == "stable":
        side = ("bullish (put writing / call buying)" if bal > 0 else "bearish (call writing / put buying)")
        return "Mixed", f"15-min flow leans {side}, but PCR is stable — not confirmed"
    return "Mixed", "PCR trend and OI flow disagree — no trade on PCR alone"


def build(res: dict, history: list[dict], now: float | None = None) -> dict:
    now = now or time.time()
    exp, spot = res["expiry"], res["spot"]
    view = {r["strike"]: r for r in res.get("chain") or []}            # ±40 strikes with OI change / build-up
    cur_rows = {k: {**v, **(view.get(k) or {})} for k, v in (res.get("strikes_all") or view).items()}
    cur = ratios(cur_rows, spot)
    raw_n = len([h for h in history if h.get("expiry") == exp])
    history = clean(history, exp, cur_rows)
    past = {}
    for h in HORIZONS:
        snap = _pick(history, exp, now - h * 60, max(120, h * 60 * 0.4))
        if not snap:
            past[h] = (None, {})
            continue
        # like with like: both sides on the strikes they share, then lined up to today's full-chain value
        common = set(cur_rows) & set(snap["rows"])
        then_c = ratios({k: snap["rows"][k] for k in common}, snap["spot"])
        now_c = ratios({k: cur_rows[k] for k in common}, spot)
        aligned = dict(then_c)
        for key in ("oi", "near", "wtd"):
            if cur.get(key) is not None and now_c.get(key) is not None and then_c.get(key) is not None:
                aligned[key] = cur[key] - (now_c[key] - then_c[key])
        past[h] = (snap, aligned)
    pset = res.get("pcr_set") or {}

    def row(key, name, value, note=""):
        vals = {h: past[h][1].get(key) for h in HORIZONS}
        return {"key": key, "name": name, "now": value, **{f"m{h}": vals[h] for h in HORIZONS},
                "trend": trend(value, vals), "note": note}

    rows = [row("oi", "Overall OI PCR", cur.get("oi"), "positioning on the whole expiry (all strikes)"),
            row("near", f"{config.PCR_NEAR_STRIKES}-strike PCR", cur.get("near"), "positioning around ATM"),
            row("wtd", "Weighted PCR", cur.get("wtd"), "near-ATM, weighted to ATM — the positioning measure scored")]
    # volume PCR: day so far, and over the last 5 / 15 minutes from cumulative volume differences
    vrow = {"key": "vol", "name": "Volume PCR", "now": pset.get("vol_pcr"), "trend": "—",
            "note": "activity (opening and closing trades), not positioning"}
    for h in HORIZONS:
        p = past[h][1]
        if cur.get("ce_vol") is not None and p.get("ce_vol") is not None:
            dc, dp = cur["ce_vol"] - p["ce_vol"], cur["pe_vol"] - p["pe_vol"]
            vrow[f"m{h}"] = dp / dc if dc > 0 and dp >= 0 else None
        else:
            vrow[f"m{h}"] = None
    vrow["note"] += "; 5/15/30 columns = PCR of the volume traded in that window"
    rows.append(vrow)
    fl = pset.get("flow") or {}
    rows.append({"key": "chg", "name": "Change-in-OI PCR", "now": pset.get("chg_pcr"), "m5": None, "m15": None,
                 "m30": None, "trend": "—", "note": f"put OI added {fl.get('pe_add', 0):,.0f} ÷ call OI added "
                                                    f"{fl.get('ce_add', 0):,.0f} today (reductions excluded)"})
    snap15 = past[15][0]
    f15 = flow(cur_rows, snap15["rows"] if snap15 else {}, spot)
    mom = {h: (cur.get("wtd") - past[h][1]["wtd"]) if cur.get("wtd") is not None and past[h][1].get("wtd") is not None
           else None for h in HORIZONS}
    rows.append({"key": "mom", "name": "PCR momentum (weighted)", "now": mom[5], "m5": None, "m15": mom[15],
                 "m30": mom[30], "trend": "—", "note": "now − 5 min (now column); now − 15 / 30 min"})
    wtd_trend = rows[2]["trend"]
    read, why = combined_read(wtd_trend, f15)
    # key strikes
    ks = sorted(view) or sorted(cur_rows)                       # key strikes from the chain view (has OI change)
    cur_rows_all, cur_rows = cur_rows, {k: cur_rows[k] for k in ks}
    below = [k for k in ks if k < spot]
    above = [k for k in ks if k > spot]
    g = lambda k, f: cur_rows[k].get(f) or 0
    then = (snap15 or {}).get("rows") or {}

    def strike(k):
        r = cur_rows[k]
        p = then.get(k) or {}
        pcr = (r.get("pe_oi") or 0) / r["ce_oi"] if r.get("ce_oi") else None
        pcr0 = (p.get("pe_oi") or 0) / p["ce_oi"] if p.get("ce_oi") else None
        return {"strike": k, "ce_oi": r.get("ce_oi"), "pe_oi": r.get("pe_oi"), "ce_chg": r.get("ce_chg_oi"),
                "pe_chg": r.get("pe_chg_oi"), "pcr": pcr, "pcr_15": pcr0,
                "pcr_chg": (pcr - pcr0) if pcr is not None and pcr0 is not None else None,
                "ce_build": r.get("ce_buildup"), "pe_build": r.get("pe_buildup")}
    levels = {
        "put_oi": max(below, key=lambda k: g(k, "pe_oi")) if below else None,
        "call_oi": max(above, key=lambda k: g(k, "ce_oi")) if above else None,
        "put_add": max(ks, key=lambda k: g(k, "pe_chg_oi")) if ks and max(g(k, "pe_chg_oi") for k in ks) > 0 else None,
        "call_add": max(ks, key=lambda k: g(k, "ce_chg_oi")) if ks and max(g(k, "ce_chg_oi") for k in ks) > 0 else None,
        "put_unwind": min(ks, key=lambda k: g(k, "pe_chg_oi")) if ks and min(g(k, "pe_chg_oi") for k in ks) < 0 else None,
        "call_unwind": min(ks, key=lambda k: g(k, "ce_chg_oi")) if ks and min(g(k, "ce_chg_oi") for k in ks) < 0 else None,
    }
    # local zones: the strongest put / call OI within the near window (OI concentration, not confirmed S/R)
    win = cur.get("window") or []
    levels["local_support"] = max((k for k in win if k <= spot), key=lambda k: g(k, "pe_oi"), default=None)
    levels["local_resistance"] = max((k for k in win if k >= spot), key=lambda k: g(k, "ce_oi"), default=None)
    series = []
    shared = set.intersection(*(set(h["rows"]) for h in history)) if history else set()
    for h in sorted(history, key=lambda x: x["ts"]):
        rws = {k: h["rows"][k] for k in shared} if len(shared) >= 20 else h["rows"]   # one strike set all day
        r = ratios(rws, h["spot"])
        if r.get("oi") is not None:
            series.append({"ts": h["ts"], "oi": r["oi"], "near": r["near"], "wtd": r["wtd"], "spot": h["spot"]})
    return {"underlying": res["underlying"], "expiry": exp, "spot": spot, "atm": cur.get("atm"), "rows": rows,
            "flow15": f15, "flow_day": fl, "read": read, "why": why, "positioning_trend": wtd_trend,
            "levels": levels, "strikes": [strike(k) for k in win], "series": series[-200:],
            "snapshots": len(series), "rejected": raw_n - len(history), "as_of": now}
