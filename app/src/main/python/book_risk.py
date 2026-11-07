"""Your option book as a risk manager sees it: what moves the P&L, what the worst cases are and
whether the account is inside its limits.  Pure functions on the position book and the
analysed option chains (options.analyze_chain); no I/O.

Per underlying and expiry
  * net Greeks of the whole book (long wings offset shorts): delta in index units and lots,
    gamma, theta (₹ per day) and vega (₹ per 1 IV point), from Black-Scholes with each leg's
    own implied volatility in the chain (ATM IV when a strike has none);
  * stress test: P&L if the index moves −4 % … +4 % now, with IV unchanged / +3 / −2 points
    (a sell-off usually lifts IV, a rally lowers it);
  * expiry view: breakevens, maximum profit and loss (or "unlimited"), P&L at ±1σ, and the
    probability of finishing in profit under the market's own implied distribution;
  * nearest short strike's distance from spot in σ, and premium captured.

Account level
  * today's loss against a daily loss limit, margin in use, and the worst 2 % move across all
    books against a per-move limit — each raises an alert before the loss gets large.

Limits come from config (DAILY_LOSS_LIMIT_PCT, MAX_MARGIN_USE_PCT, MOVE_LOSS_LIMIT_PCT,
BOOK_DELTA_LOTS).  Analytics only, not investment advice.
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd

import config
from options import bs_price, years_to_expiry

MOVES = [-0.04, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.04]
IV_SHIFTS = [(0.0, "IV same"), (0.03, "IV +3"), (-0.02, "IV −2")]


def _exp_str(e) -> str:
    return pd.Timestamp(e).strftime("%d-%b-%Y")


def _leg_iv(chains: dict, exp: str, strike: float, kind: str, fallback: float | None) -> float | None:
    """Decimal IV for a leg: its own strike in its own expiry's chain, else that chain's ATM IV."""
    c = chains.get(exp)
    p = "ce" if kind == "CE" else "pe"
    if c:
        for r in c.get("chain") or []:
            if r["strike"] == strike and r.get(f"{p}_iv"):
                return r[f"{p}_iv"] / 100
        for r in c.get("oi_table") or []:
            v = r.get(f"{p}_iv")
            if r["strike"] == strike and v and not pd.isna(v) and v > 0:
                return float(v)
        if c.get("atm_iv"):
            return c["atm_iv"] / 100
    return fallback


def _value(legs, S, T_by_leg, iv_by_leg, dS=0.0, dIV=0.0, dT=0.0) -> float:
    tot = 0.0
    for l, T, iv in zip(legs, T_by_leg, iv_by_leg):
        t = max(T - dT, 1e-6)
        tot += l["qty"] * bs_price(S * (1 + dS), l["strike"], t, max(iv + dIV, 0.01), l["kind"])
    return tot


def _payoff(legs, x: np.ndarray) -> np.ndarray:
    out = np.zeros_like(x, dtype=float)
    for l in legs:
        intrinsic = np.maximum(x - l["strike"], 0) if l["kind"] == "CE" else np.maximum(l["strike"] - x, 0)
        out += l["qty"] * (intrinsic - l["avg"])
    return out


def assess_group(g: dict, chains: dict, lot: int, now: datetime | None = None) -> dict | None:
    """One underlying + expiry group (trades_engine.manage_positions) → risk picture."""
    legs = [dict(l, kind=l.get("kind"), strike=float(l["strike"]), qty=float(l["qty"]),
                 avg=float(l.get("avg") or 0), lp=float(l.get("lp") or l.get("ltp") or 0))
            for l in g.get("legs") or [] if l.get("qty") and l.get("kind") in ("CE", "PE")]
    if not legs:
        return None
    S = float(g.get("spot") or 0)
    if not S:
        return None
    exp = _exp_str(g["expiry"])
    T = years_to_expiry(exp, now)
    any_chain = next(iter(chains.values()), None) or {}
    atm = (chains.get(exp) or any_chain).get("atm_iv")
    atm = atm / 100 if atm else 0.15
    ivs = [_leg_iv(chains, exp, l["strike"], l["kind"], atm) or atm for l in legs]
    Ts = [T] * len(legs)

    base = _value(legs, S, Ts, ivs)
    h = S * 0.001
    up, dn = _value(legs, S + h, Ts, ivs), _value(legs, S - h, Ts, ivs)
    delta = (up - dn) / (2 * h)                                 # ₹ per point = index units
    gamma = (up - 2 * base + dn) / (h * h)                      # units per point
    theta = _value(legs, S, Ts, ivs, dT=1 / 365) - base         # ₹ per calendar day
    vega = _value(legs, S, Ts, ivs, dIV=0.01) - base            # ₹ per IV point
    cost = sum(l["qty"] * l["avg"] for l in legs)
    pnl_now = sum(l["qty"] * (l["lp"] - l["avg"]) for l in legs)
    credit = -cost

    grid = []
    for dIV, name in IV_SHIFTS:
        grid.append({"label": name, "pnl": [round(_value(legs, S, Ts, ivs, dS=m, dIV=dIV) - cost) for m in MOVES]})

    lo, hi = S * 0.6, S * 1.4
    x = np.linspace(lo, hi, 4001)
    pay = _payoff(legs, x)
    sign = np.sign(pay)
    bes = [float(x[i] + (x[i + 1] - x[i]) * (0 - pay[i]) / (pay[i + 1] - pay[i]))
           for i in range(len(x) - 1) if sign[i] != sign[i + 1] and sign[i] != 0]
    net_calls = sum(l["qty"] for l in legs if l["kind"] == "CE")
    net_puts = sum(l["qty"] for l in legs if l["kind"] == "PE")
    max_loss = None if net_calls < 0 or net_puts < 0 else float(pay.min())
    max_profit = None if net_calls > 0 or net_puts > 0 else float(pay.max())
    sig = atm * math.sqrt(max(T, 1e-6))
    z = np.log(x / S) / sig + sig / 2                            # lognormal density under implied vol
    dens = np.exp(-z * z / 2) / (x * sig * math.sqrt(2 * math.pi))
    w = dens / dens.sum()
    pop = float((w * (pay > 0)).sum())
    exp_pnl = float((w * pay).sum())
    one_sigma = S * sig
    at = lambda v: float(_payoff(legs, np.array([v]))[0])

    shorts = [l for l in legs if l["qty"] < 0]
    near = min(shorts, key=lambda l: abs(l["strike"] - S)) if shorts else None
    dist_sigma = ((near["strike"] - S) / one_sigma) if near else None

    return {
        "und": g["und"], "expiry": exp, "dte": round(T * 365, 1), "spot": S, "lot": lot,
        "structure": g.get("structure"), "credit": round(credit), "pnl_now": round(pnl_now),
        "captured": (pnl_now / credit) if credit > 0 else None,
        "delta": round(delta, 1), "delta_lots": round(delta / lot, 2) if lot else None,
        "gamma": round(gamma, 3), "theta": round(theta), "vega": round(vega),
        "per_100": round(delta * 100), "atm_iv": round(atm * 100, 2), "sigma_pts": round(one_sigma),
        "moves": MOVES, "grid": grid,
        "breakevens": [round(b) for b in bes], "max_profit": None if max_profit is None else round(max_profit),
        "max_loss": None if max_loss is None else round(max_loss),
        "pnl_expiry_spot": round(at(S)), "pnl_expiry_up1s": round(at(S + one_sigma)),
        "pnl_expiry_dn1s": round(at(S - one_sigma)), "pop": round(pop * 100), "expected_pnl": round(exp_pnl),
        "nearest_short": None if not near else {"strike": near["strike"], "kind": near["kind"],
                                                "sigma_away": round(dist_sigma, 2)},
    }


def book_alerts(b: dict, capital: float) -> list[dict]:
    """Alerts for one book, in the risk monitor's format (level, title, signals, action, side)."""
    out = []
    lots = abs(b.get("delta_lots") or 0)
    side = "call" if (b.get("delta") or 0) < 0 else "put"
    if lots >= config.BOOK_DELTA_LOTS:
        out.append({"level": "AMBER", "title": "BOOK DELTA OVER LIMIT",
                    "signals": [f"{b['und']} {b['expiry']}: net delta {b['delta_lots']:+.1f} lots — P&L moves "
                                f"₹{b['per_100']:+,} per +100 pts",
                                f"Limit {config.BOOK_DELTA_LOTS:g} lots of net delta"],
                    "action": ("Rebalance: add put-side credit or buy back calls" if b["delta"] < 0 else
                               "Rebalance: add call-side credit or buy back puts"), "side": side})
    worst = min(min(r["pnl"]) for r in b["grid"])
    lim = capital * config.MOVE_LOSS_LIMIT_PCT / 100 if capital else None
    if lim and worst < -lim:
        i = min(range(len(b["moves"])), key=lambda k: min(r["pnl"][k] for r in b["grid"]))
        out.append({"level": "RED" if worst < -2 * lim else "AMBER", "title": "STRESS LOSS OVER LIMIT",
                    "signals": [f"{b['und']} {b['expiry']}: a {b['moves'][i] * 100:+.0f}% move now → P&L ₹{worst:,.0f}",
                                f"Limit ₹{lim:,.0f} ({config.MOVE_LOSS_LIMIT_PCT:g}% of capital) per book"],
                    "action": "Cut size or buy wings until a 4% move stays inside the limit", "side": side})
    ns = b.get("nearest_short")
    if ns and abs(ns["sigma_away"]) < 0.25 and b["dte"] > 0.5:
        out.append({"level": "AMBER", "title": "SHORT STRIKE AT / IN THE MONEY",
                    "signals": [f"{ns['strike']:,.0f} {ns['kind']} is {ns['sigma_away']:+.2f}σ from spot "
                                f"({b['dte']:.0f} DTE)"],
                    "action": "Gamma is highest here — roll or hedge before it goes in the money",
                    "side": "call" if ns["kind"] == "CE" else "put"})
    if b["credit"] > 0 and b["theta"] < 0:
        out.append({"level": "AMBER", "title": "BOOK PAYS TIME DECAY",
                    "signals": [f"{b['und']} {b['expiry']}: theta ₹{b['theta']:+,}/day, vega ₹{b['vega']:+,} per IV point — "
                                "the shorts are deep in the money, so the long wings now hold the time value"],
                    "action": "A credit book that loses to decay has lost its edge — the shorts are now directional bets",
                    "side": side})
    if b.get("pop") is not None and b["pop"] < 35:
        out.append({"level": "AMBER", "title": "LOW PROBABILITY OF PROFIT",
                    "signals": [f"{b['und']} {b['expiry']}: {b['pop']}% chance of profit at expiry; breakeven(s) " +
                                ", ".join(f"{x:,}" for x in b["breakevens"]) +
                                f"; expected P&L at expiry ₹{b['expected_pnl']:,} under the implied distribution"],
                    "action": "The book needs a move it is not likely to get — restructure or close", "side": side})
    return out


def account_check(today: dict | None, limits: dict | None, books: list[dict]) -> dict:
    """Account-level limits: daily loss, margin use, worst 2 % move across every book."""
    limits, today = limits or {}, today or {}
    used = float(limits.get("margin_used") or 0)
    avail = float(limits.get("available") or 0)
    capital = config.TRADING_CAPITAL or (used + avail)
    day = float(today.get("total") or 0)
    alerts = []
    dl = capital * config.DAILY_LOSS_LIMIT_PCT / 100 if capital else None
    if dl and day < -dl:
        alerts.append({"level": "RED", "title": "DAILY LOSS LIMIT HIT",
                       "text": f"Today's P&L ₹{day:,.0f} is past the ₹{dl:,.0f} limit "
                               f"({config.DAILY_LOSS_LIMIT_PCT:g}% of ₹{capital:,.0f}). Stop adding risk today; "
                               "only reduce."})
    elif dl and day < -0.7 * dl:
        alerts.append({"level": "AMBER", "title": "70% OF DAILY LOSS LIMIT",
                       "text": f"Today's P&L ₹{day:,.0f} of a ₹{dl:,.0f} limit — no new positions until it recovers."})
    use = used / (used + avail) * 100 if used + avail else None
    if use is not None and use > config.MAX_MARGIN_USE_PCT:
        alerts.append({"level": "AMBER", "title": f"MARGIN USE {use:.0f}%",
                       "text": f"₹{used:,.0f} of ₹{used + avail:,.0f} in use (limit {config.MAX_MARGIN_USE_PCT:g}%) — "
                               "a margin call on a gap is possible; keep a buffer."})
    worst2 = {}
    for m in (-0.02, 0.02):
        k = MOVES.index(m)
        worst2[m] = sum(min(r["pnl"][k] for r in b["grid"]) - b["grid"][0]["pnl"][MOVES.index(0.0)] for b in books)
    return {"capital": capital, "day_pnl": day, "daily_limit": dl, "margin_use_pct": use,
            "move_2pct": {"down": round(worst2[-0.02]), "up": round(worst2[0.02])},
            "theta_day": sum(b["theta"] for b in books), "vega": sum(b["vega"] for b in books), "alerts": alerts}
