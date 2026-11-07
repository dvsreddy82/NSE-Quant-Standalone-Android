"""Writer risk monitor: protects a NIFTY / BANKNIFTY option writer from squeezes,
put flushes and negative-gamma traps.

Inputs: the live option-chain analysis (options.analyze_chain), an earlier
intraday observation of the same chain (for OI / IV / straddle changes), the
spot path of the last hour, participant-wise OI (fii_analysis.build_frame) and
the trader's open legs.  Output: regime, bias, max-danger strikes, colour-coded
alerts that quote the exact numbers that fired, and directives (prohibitions,
safe zones beyond 0.10 delta AND 1.5x the expected move, guidance for each
open short leg).

Threat vectors and triggers (the trader's own rule set):
 1. Gamma squeeze / call unwinding (RED up): spot within 0.5% of the call wall,
    call OI at the wall falling, call IV rising with spot up, put OI building at ATM.
 2. Put flush (RED down): the mirror image at the put wall.
 3. Institutional divergence (AMBER): FII / Pro adding longs while clients write
    calls; FII futures long share > 65% or a 2-day rebound from < 20%.
 4. Low-vol compression / negative GEX (AMBER): VIX < 12, the ATM straddle
    expanding, or spot in a negative dealer-gamma zone trending without pullbacks.

Rules are mechanical screens on exchange data, not a forecast or advice.
GEX uses the common convention (dealers long calls, short puts); Indian option
flow does not always fit it, so it is reported as a model reading.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from options import bs_greeks, enrich, parse_chain, years_to_expiry

NEAR_PCT = 0.5            # % distance that counts as "at" a wall
IV_UP = 0.4               # vol points of IV expansion that count as rising
OI_DROP = 0.03            # intraday OI fall (fraction of OI) that counts as unwinding
DAY_DROP = 0.02           # day OI fall (fraction of OI) that counts as unwinding
VIX_LOW = 12.0
STRADDLE_UP = 0.04        # straddle +4% intraday despite time decay
SAFE_DELTA = 0.10
SAFE_SIGMA = 1.5
STOP_DELTA = 0.30         # delta at which a short leg is in trouble
EXIT_DELTA = 0.40
MIN_AGE, MAX_AGE = 10 * 60, 75 * 60     # comparison window for intraday changes (seconds)


# ---------------------------------------------------------------- observations
def compact(res: dict, ts: float | None = None) -> dict:
    """A small copy of a chain analysis, kept in memory to measure intraday change."""
    return {"ts": ts or time.time(), "spot": res["spot"], "straddle": res.get("straddle"),
            "expiry": res["expiry"],
            "rows": {r["strike"]: {k: r.get(k) for k in ("ce_oi", "pe_oi", "ce_iv", "pe_iv", "ce_ltp", "pe_ltp")}
                     for r in res.get("chain") or []}}


def compact_from_js(js: dict, expiry: str, ts: float) -> dict | None:
    """Same, rebuilt from a stored NSE-shaped snapshot (IV recomputed)."""
    try:
        meta, raw = parse_chain(js, expiry)
        S = meta["spot"]
        T = years_to_expiry(expiry, pd.Timestamp(ts, unit="s", tz="Asia/Kolkata"))
        df = enrich(raw, S, T)
    except Exception:
        return None
    atm = df.iloc[int((df.strike - S).abs().idxmin())]
    rows = {}
    for _, r in df.iterrows():
        rows[float(r.strike)] = {"ce_oi": float(r.ce_oi), "pe_oi": float(r.pe_oi),
                                 "ce_iv": float(r.ce_iv) * 100 if r.ce_iv and not pd.isna(r.ce_iv) else None,
                                 "pe_iv": float(r.pe_iv) * 100 if r.pe_iv and not pd.isna(r.pe_iv) else None,
                                 "ce_ltp": float(r.ce_ltp), "pe_ltp": float(r.pe_ltp)}
    return {"ts": ts, "spot": S, "straddle": float(atm.ce_ltp + atm.pe_ltp), "expiry": expiry, "rows": rows}


def pick_previous(history: list[dict], expiry: str, now: float | None = None) -> dict | None:
    """The observation closest to 30 minutes old (10-75 min window), same expiry."""
    now = now or time.time()
    cands = [h for h in history if h["expiry"] == expiry and MIN_AGE <= now - h["ts"] <= MAX_AGE]
    return min(cands, key=lambda h: abs((now - h["ts"]) - 1800)) if cands else None


# ---------------------------------------------------------------- helpers
@dataclass
class Alert:
    level: str            # RED / AMBER / GREEN
    title: str
    signals: list[str] = field(default_factory=list)
    action: str = ""
    side: str = ""        # "call" / "put" / "both" / ""


def _lakh(v):
    if v is None or pd.isna(v):
        return "—"
    a = abs(v)
    s = f"{a / 1e5:.2f}L" if a >= 1e5 else f"{a / 1e3:.1f}K" if a >= 1e3 else f"{a:,.0f}"
    return ("−" if v < 0 else "+") + s


def _row(rows, k):
    return next((r for r in rows if r["strike"] == k), None)


def gex_profile(res: dict) -> dict:
    """Net dealer gamma exposure (₹ per 1% move) at spot and the gamma-flip level."""
    rows, S, lot = res.get("chain") or [], res["spot"], res.get("lot") or 1
    T = max(res.get("dte", 1), 0.1) / 365

    def net_at(x):
        tot = 0.0
        for r in rows:
            for p, kind, sign in (("ce", "CE", 1), ("pe", "PE", -1)):
                iv = r.get(f"{p}_iv")
                if not iv or not r.get(f"{p}_oi"):
                    continue
                g = bs_greeks(x, r["strike"], T, iv / 100, kind)["gamma"]
                tot += sign * g * r[f"{p}_oi"] * lot * x * x * 0.01
        return tot

    now = net_at(S)
    grid = np.linspace(S * 0.96, S * 1.04, 33)
    vals = [net_at(x) for x in grid]
    flips = [float(a + (b - a) * abs(va) / ((abs(va) + abs(vb)) or 1))
             for a, b, va, vb in zip(grid, grid[1:], vals, vals[1:]) if va * vb < 0]
    flip = min(flips, key=lambda x: abs(x - S)) if flips else None      # the sign change nearest spot
    return {"net": now, "flip": flip, "negative": now < 0}


def _strike_for_delta(res, kind, target, side):
    """First strike beyond `side` of spot whose |delta| ≤ target and that lies beyond 1.5σ."""
    S, sig = res["spot"], res.get("sigma_move") or 0
    rows = sorted(res.get("chain") or [], key=lambda r: r["strike"], reverse=(kind == "PE"))
    p = "ce" if kind == "CE" else "pe"
    lim = S + SAFE_SIGMA * sig if kind == "CE" else S - SAFE_SIGMA * sig
    for r in rows:
        k = r["strike"]
        beyond = k >= lim if kind == "CE" else k <= lim
        d = r.get(f"{p}_delta")
        if beyond and d is not None and abs(d) <= target and r.get(f"{p}_oi"):
            return k, d
    return None, None


def _spot_for_delta(strike, iv, T, kind, target):
    """Spot level at which a short option's |delta| reaches `target` (for stop levels)."""
    if not iv or T <= 0:
        return None
    lo, hi = strike * 0.85, strike * 1.15
    f = lambda x: abs(bs_greeks(x, strike, T, iv / 100, kind)["delta"]) - target
    if f(lo) * f(hi) > 0:
        return None
    for _ in range(50):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# ---------------------------------------------------------------- engine
def assess(res: dict, prev: dict | None, spot_path: list[tuple] | None, fii: pd.DataFrame | None,
           fii_raw: pd.DataFrame | None, legs: list[dict] | None, chains: dict[str, dict] | None = None) -> dict:
    rows = res.get("chain") or []
    S, step = res["spot"], res.get("step") or 50
    sig = res.get("sigma_move") or S * 0.02
    summ = res.get("chain_summary") or {}
    atm = min(rows, key=lambda r: abs(r["strike"] - S))["strike"] if rows else S
    age = (time.time() - prev["ts"]) / 60 if prev else None
    dS = (S - prev["spot"]) if prev else None
    dSp = (dS / prev["spot"] * 100) if prev else None
    win = f"in {age:.0f} min" if prev else ""

    def d_oi(k, p):
        if not prev or k not in prev["rows"]:
            return None
        r = _row(rows, k)
        old = prev["rows"][k].get(f"{p}_oi")
        return (r[f"{p}_oi"] - old) if r and old is not None else None

    def d_iv(k, p):
        if not prev or k not in prev["rows"]:
            return None
        r = _row(rows, k)
        old = prev["rows"][k].get(f"{p}_iv")
        new = r.get(f"{p}_iv") if r else None
        return (new - old) if new is not None and old is not None else None

    above = [r for r in rows if S <= r["strike"] <= S + 2 * sig]
    below = [r for r in rows if S - 2 * sig <= r["strike"] <= S]
    call_wall = max(above, key=lambda r: r["ce_oi"])["strike"] if above else summ.get("resistance")
    put_wall = max(below, key=lambda r: r["pe_oi"])["strike"] if below else summ.get("support")
    alerts: list[Alert] = []
    checks = {}

    # ---- 1. gamma squeeze up / 2. put flush down (mirror images)
    for side in ("up", "down"):
        p, q = ("ce", "pe") if side == "up" else ("pe", "ce")
        wall = call_wall if side == "up" else put_wall
        if wall is None:
            continue
        wr = _row(rows, wall)
        dist = (wall - S) / S * 100 if side == "up" else (S - wall) / S * 100
        c1 = dist <= NEAR_PCT
        sig1 = (f"Spot {S:,.1f} is {dist:+.2f}% from the {wall:,.0f} {'CE' if side == 'up' else 'PE'} wall "
                f"(OI {_lakh(wr[p + '_oi'])[1:]})" + (" — breached" if dist < 0 else ""))
        # writers fleeing the wall (day change and intraday change)
        day = wr[f"{p}_chg_oi"]
        intr = d_oi(wall, p)
        c2 = day <= -DAY_DROP * wr[f"{p}_oi"] or (intr is not None and intr <= -OI_DROP * wr[f"{p}_oi"])
        sig2 = (f"{wall:,.0f} {'CE' if side == 'up' else 'PE'} OI {_lakh(day)} today"
                + (f", {_lakh(intr)} {win}" if intr is not None else ""))
        # IV of ATM + first two OTM strikes on that side, rising while spot moves toward the wall
        ks = [k for k in sorted({r["strike"] for r in rows}, reverse=(side == "down"))
              if (k >= atm if side == "up" else k <= atm)][:3]
        divs = [v for v in (d_iv(k, p) for k in ks) if v is not None]
        div = sum(divs) / len(divs) if divs else None
        moving = dS is not None and ((dS > 0) if side == "up" else (dS < 0))
        c3 = div is not None and div >= IV_UP and moving
        sig3 = (f"{'Call' if side == 'up' else 'Put'} IV {div:+.2f} vol pts at {', '.join(f'{k:,.0f}' for k in ks)} "
                f"while spot {dS:+.1f} pts ({dSp:+.2f}%) {win}") if div is not None else \
            "IV change: needs ≥10 min of intraday history"
        # opposite base migrating toward/through spot: biggest fresh opposite-side OI right at ATM
        near = [r for r in rows if abs(r["strike"] - atm) <= step]
        best = max(rows, key=lambda r: r[f"{q}_chg_oi"]) if rows else None
        mig = max(near, key=lambda r: r[f"{q}_chg_oi"]) if near else None
        c4 = bool(mig and best and mig[f"{q}_chg_oi"] > 0 and mig[f"{q}_chg_oi"] >= 0.6 * best[f"{q}_chg_oi"])
        sig4 = (f"{'Put' if side == 'up' else 'Call'} OI {_lakh(mig[q + '_chg_oi'])} today at "
                f"{mig['strike']:,.0f} (ATM band) vs largest {_lakh(best[q + '_chg_oi'])} at {best['strike']:,.0f}"
                ) if mig and best else ""
        hits = [c2, c3, c4]
        checks[side] = {"near": c1, "unwind": c2, "iv": c3, "migrate": c4, "dist": dist, "wall": wall}
        n = sum(hits)
        if c1 and n >= 2:
            lvl = "RED"
        elif (c1 and n == 1) or (dist <= 2 * NEAR_PCT and n >= 2):
            lvl = "AMBER"
        else:
            continue
        sigs = [sig1] + [s for s, h in zip((sig2, sig3, sig4), hits) if h]
        if side == "up":
            title = "IMMINENT GAMMA SQUEEZE UPWARD" if lvl == "RED" else "Call-side squeeze risk building"
            action = ("Forbid writing calls. Cover short calls now or convert them to spreads (buy a call "
                      "0.5σ above)." if lvl == "RED" else "No fresh call writing near the wall; hedge short calls.")
        else:
            title = "PUT FLUSH / MOMENTUM DOWN" if lvl == "RED" else "Put-side flush risk building"
            action = ("Forbid writing puts. Cover short puts now or convert them to spreads (buy a put "
                      "0.5σ below)." if lvl == "RED" else "No fresh put writing near the wall; hedge short puts.")
        alerts.append(Alert(lvl, title, sigs, action, "call" if side == "up" else "put"))

    # ---- 3. participant OI divergence (EOD data)
    if fii is not None and len(fii) >= 3:
        f = fii.iloc[-3:]
        lp = f["fii_long_pct"]
        sigs = []
        if lp.iloc[-1] > 65:
            sigs.append(f"FII index-futures long share {lp.iloc[-1]:.1f}% (> 65%)")
        if lp.iloc[0] < 20 and lp.iloc[-1] - lp.iloc[0] >= 3:
            sigs.append(f"FII long share rebounding from oversold: {lp.iloc[0]:.1f}% → {lp.iloc[-1]:.1f}% in 2 sessions")
        if fii_raw is not None and len(fii_raw) >= 2 and "client_idx_call_short" in fii_raw:
            a, b = fii_raw.iloc[-2], fii_raw.iloc[-1]

            def dd(col):
                return float(b.get(col, np.nan) - a.get(col, np.nan))
            inst_long = []
            for who in ("fii", "pro"):
                fut = dd(f"{who}_idx_fut_long") - dd(f"{who}_idx_fut_short")
                calls = dd(f"{who}_idx_call_long")
                if fut > 0:
                    inst_long.append(f"{who.upper()} net futures {_lakh(fut)}")
                if calls > 0:
                    inst_long.append(f"{who.upper()} call longs {_lakh(calls)}")
            client_write = dd("client_idx_call_short") - dd("client_idx_call_long")
            if inst_long and client_write > 0:
                sigs.append(f"{', '.join(inst_long)} while clients net-wrote {_lakh(client_write)[1:]} calls "
                            f"({fii_raw.index[-1]:%d %b})")
        if sigs:
            alerts.append(Alert("AMBER", "INSTITUTIONAL SQUEEZE LOADING", sigs,
                                "Do not sell unhedged OTM calls (the counter-trend side); use spreads.", "call"))

    # ---- 4. low-vol compression / negative GEX
    gex = gex_profile(res) if rows else {"net": 0, "flip": None, "negative": False}
    sigs = []
    vix = res.get("vix")
    if vix is not None and vix < VIX_LOW:
        sigs.append(f"India VIX {vix:.2f} (< {VIX_LOW:.0f}): premiums thin versus tail risk")
    if prev and prev.get("straddle") and res.get("straddle"):
        chg = res["straddle"] / prev["straddle"] - 1
        if chg >= STRADDLE_UP:
            sigs.append(f"ATM straddle {prev['straddle']:,.1f} → {res['straddle']:,.1f} ({chg * 100:+.1f}%) {win} "
                        "despite time decay")
    expansion = None
    if spot_path and len(spot_path) >= 20:
        px = [p for _, p in spot_path[-60:]]
        move = px[-1] - px[0]
        if abs(move) / px[0] * 100 >= 0.5:
            run = np.maximum.accumulate(px) if move > 0 else np.minimum.accumulate(px)
            pull = float(np.max(np.abs(run - np.array(px))))
            if pull <= 0.25 * abs(move):
                expansion = (move, pull, len(px))
    if gex["negative"]:
        txt = (f"Net dealer gamma {gex['net'] / 1e7:+.2f} Cr per 1% at {S:,.0f} (negative zone"
               + (f"; flip ≈ {gex['flip']:,.0f}" if gex["flip"] else "") + ")")
        if expansion:
            sigs.append(txt + f"; spot moved {expansion[0]:+.0f} pts in {expansion[2]} min with only "
                              f"{expansion[1]:.0f} pts pullback")
        else:
            sigs.append(txt)
    elif expansion:
        sigs.append(f"Spot moved {expansion[0]:+.0f} pts in {expansion[2]} min with only {expansion[1]:.0f} pts pullback")
    if sigs:
        alerts.append(Alert("AMBER", "SKEW IMBALANCE / NEGATIVE GAMMA", sigs,
                            "Restrict naked writing; use defined-risk structures (iron condors, credit spreads).",
                            "both"))

    # ---- regime, bias, danger strikes
    red = [a for a in alerts if a.level == "RED"]
    if red:
        regime = "Squeeze in Progress"
    elif gex["negative"] or expansion:
        regime = "Negative Gamma Trending"
    else:
        regime = "Positive Gamma Range-Bound"
    near = [r for r in rows if abs(r["strike"] - S) <= sig]
    pe_add = sum(max(r["pe_chg_oi"], 0) for r in near)
    ce_add = sum(max(r["ce_chg_oi"], 0) for r in near)
    flow = (pe_add - ce_add) / (pe_add + ce_add) if pe_add + ce_add else 0
    mom = max(min((dSp or 0) / 0.3, 1), -1)
    bscore = 0.7 * flow + 0.3 * mom
    bias = "Bullish Delta" if bscore > 0.25 else "Bearish Delta" if bscore < -0.25 else "Neutral Mean-Reverting"

    def danger(side):
        p = "ce" if side == "up" else "pe"
        band = above if side == "up" else below
        if not band:
            return None, ""
        wall = call_wall if side == "up" else put_wall
        top = max((r[f"{p}_oi"] for r in band), default=0)
        unw = [r for r in band if r[f"{p}_chg_oi"] <= -0.05 * r[f"{p}_oi"] and r[f"{p}_oi"] >= 0.3 * top]
        if checks.get(side, {}).get("near") or not unw:
            r = _row(rows, wall)
            return wall, (f"largest {'call' if side == 'up' else 'put'} OI ({_lakh(r[p + '_oi'])[1:]}) "
                          f"{abs(checks.get(side, {}).get('dist', 0)):.2f}% away; a break forces writers to cover")
        r = min(unw, key=lambda r: r[f"{p}_chg_oi"])
        return r["strike"], (f"writers cut {_lakh(r[p + '_chg_oi'])[1:]} today — "
                             f"{'resistance' if side == 'up' else 'support'} thinning here")
    up_k, up_why = danger("up")
    dn_k, dn_why = danger("down")

    # ---- directives
    sides_red = {a.side for a in red}
    sides_amber = {a.side for a in alerts if a.level == "AMBER"}
    call_safe, call_d = _strike_for_delta(res, "CE", SAFE_DELTA, "up")
    put_safe, put_d = _strike_for_delta(res, "PE", SAFE_DELTA, "down")
    prohib = []
    if "call" in sides_red:
        prohib.append(f"NO call writing at any strike below {call_safe:,.0f}; no naked calls at all"
                      if call_safe else "NO call writing")
    if "put" in sides_red:
        prohib.append(f"NO put writing at any strike above {put_safe:,.0f}; no naked puts at all"
                      if put_safe else "NO put writing")
    if "call" in sides_amber and "call" not in sides_red:
        prohib.append("No unhedged OTM calls — call side only as spreads")
    if "put" in sides_amber and "put" not in sides_red:
        prohib.append("No unhedged OTM puts — put side only as spreads")
    if "both" in sides_amber:
        prohib.append("No naked writing on either side — defined risk only")
    if not prohib:
        prohib.append("None triggered")
    safe = []
    for kind, k, d, blocked in (("CE", call_safe, call_d, "call" in sides_red),
                                ("PE", put_safe, put_d, "put" in sides_red)):
        if k is None:
            safe.append(f"{kind}: no strike in the loaded chain is beyond both {SAFE_SIGMA}σ and 0.10Δ")
            continue
        wing = k + step * max(2, round(0.5 * sig / step)) * (1 if kind == "CE" else -1)
        txt = (f"{kind} {k:,.0f} or farther (Δ {d:+.2f}, {abs(k / S - 1) * 100:.1f}% away, beyond {SAFE_SIGMA}σ "
               f"= {SAFE_SIGMA * sig:,.0f} pts)")
        amber = "both" in sides_amber or ("call" if kind == "CE" else "put") in sides_amber
        if blocked:
            txt += f" — side under RED: only as a spread with a long wing at {wing:,.0f}"
        elif amber:
            txt += f" — hedged: buy {wing:,.0f}"
        safe.append(txt)
    guidance = position_guidance(res, legs or [], sides_red, sig, step, {**(chains or {}), res["expiry"]: res})
    ok = not alerts
    if ok:
        alerts.append(Alert("GREEN", "SAFE REGIME", [
            f"Spot {S:,.1f}: {checks.get('up', {}).get('dist', 0):.2f}% below the {call_wall:,.0f} call wall and "
            f"{checks.get('down', {}).get('dist', 0):.2f}% above the {put_wall:,.0f} put wall"
            if call_wall and put_wall else f"Spot {S:,.1f}",
            f"Net dealer gamma {gex['net'] / 1e7:+.2f} Cr per 1% (positive: moves tend to be absorbed)",
        ], "Writing permitted outside the safe zones below; keep standard stops.", ""))
    return {
        "underlying": res["underlying"], "expiry": res["expiry"], "spot": S, "dte": res.get("dte"),
        "sigma": sig, "atm": atm, "atm_iv": res.get("atm_iv"), "vix": vix, "source": res.get("source"),
        "regime": regime, "bias": bias, "bias_score": bscore,
        "danger_up": (up_k, up_why), "danger_down": (dn_k, dn_why),
        "alerts": [a.__dict__ for a in alerts], "prohibitions": prohib, "safe": safe, "positions": guidance,
        "gex": gex, "window_min": age, "checks": checks, "call_wall": call_wall, "put_wall": put_wall,
        "timestamp": res.get("timestamp"),
    }


def _leg_greeks(chains: dict[str, dict], leg: dict, fallback: dict):
    """(delta, iv, T, expiry label) for a leg, from the chain of its own expiry when loaded."""
    exp = leg.get("expiry")
    key = pd.Timestamp(exp).strftime("%d-%b-%Y") if exp is not None else None
    res = chains.get(key) or fallback
    rows = res.get("chain") or []
    S, k, kind = res["spot"], leg["strike"], leg["kind"]
    p = kind.lower()
    T = max(res.get("dte", 1), 0.05) / 365
    if key and key in chains:
        T = max(years_to_expiry(key), 0.5 / 365)
    r = _row(rows, k)
    iv = r.get(f"{p}_iv") if r else None
    if not iv:                                   # deep ITM / outside the chain: borrow the nearest solvable IV
        near = sorted((x for x in rows if x.get(f"{p}_iv")), key=lambda x: abs(x["strike"] - k))
        iv = near[0][f"{p}_iv"] if near else res.get("atm_iv")
    d = bs_greeks(S, k, T, iv / 100, kind)["delta"] if iv else None
    return d, iv, T, key, res is not fallback or key == fallback.get("expiry")


def position_guidance(res: dict, legs: list[dict], red_sides: set, sig: float, step: float,
                      chains: dict[str, dict] | None = None) -> list[str]:
    """Hold / trail / hedge / exit for each open short leg of this underlying."""
    chains = chains or {res["expiry"]: res}
    out = []
    shorts = [l for l in legs if l.get("qty", 0) < 0 and l.get("kind") in ("CE", "PE")]
    longs = {(l["kind"], l["strike"]) for l in legs if l.get("qty", 0) > 0 and l.get("kind") in ("CE", "PE")}
    if not shorts:
        return ["No open short legs in this underlying (connect Shoonya / sync Trades to include your book)."]
    # the whole book first: long wings offset short legs, so net delta is what moves P&L
    by_exp: dict[str, list] = {}
    for l in legs:
        if l.get("kind") in ("CE", "PE") and l.get("qty"):
            d, _iv, _T, exp, _own = _leg_greeks(chains, l, res)
            if d is not None:
                by_exp.setdefault(exp or "?", []).append((l, d))
    lot = res.get("lot") or 1
    for exp, items in sorted(by_exp.items()):
        net = sum(l["qty"] * d for l, d in items)
        wings = [f"{l['strike']:,.0f} {l['kind']} ×{l['qty']}" for l, _d in items if l["qty"] > 0]
        out.append(f"Book {exp}: net delta {net:+,.0f} units (≈ {net / lot:+.1f} lots of the index) — "
                   f"P&L moves ≈ ₹{net * 100:+,.0f} per +100 pts"
                   + (f" · long wings: {', '.join(wings)}" if wings else " · no long wings (unhedged)"))

    for l in sorted(shorts, key=lambda l: (l["kind"], l["strike"])):
        k, kind = l["strike"], l["kind"]
        d, iv, T, exp, own = _leg_greeks(chains, l, res)
        name = f"Short {k:,.0f} {kind} ×{abs(l['qty'])}" + (f" ({exp[:6]})" if exp else "")
        if not own:
            name += " [delta from a different expiry's IV]"
        hedged = any(hk == kind and ((hs > k) if kind == "CE" else (hs < k)) for hk, hs in longs)
        side = "call" if kind == "CE" else "put"
        wing = k + step * max(2, round(0.5 * sig / step)) * (1 if kind == "CE" else -1)
        if side in red_sides:
            out.append(f"{name}: EXIT or convert to a spread now (buy {wing:,.0f} {kind}) — RED alert on the "
                       f"{side} side" + (" (already has a long wing)" if hedged else ""))
            continue
        if d is None:
            out.append(f"{name}: no IV available — check it manually")
            continue
        stop = _spot_for_delta(k, iv, T, kind, STOP_DELTA)
        stop_txt = f"; delta stop ({STOP_DELTA:.2f}Δ) at spot {stop:,.0f}" if stop else ""
        if abs(d) >= EXIT_DELTA:
            out.append(f"{name}: Δ {d:+.2f} ≥ {EXIT_DELTA} — EXIT SHORT or roll {'up' if kind == 'CE' else 'down'}"
                       f" and out")
        elif abs(d) >= STOP_DELTA:
            out.append(f"{name}: Δ {d:+.2f} past the {STOP_DELTA} stop — hedge with the {wing:,.0f} {kind} or cut")
        elif abs(d) >= 0.20:
            out.append(f"{name}: Δ {d:+.2f} — trail: tighten the stop{stop_txt}")
        else:
            out.append(f"{name}: Δ {d:+.2f} — HOLD{stop_txt}" + (" (hedged)" if hedged else ""))
    return out
