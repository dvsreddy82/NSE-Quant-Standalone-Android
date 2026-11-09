"""Option-chain analytics for positional index option *selling* (NIFTY / BANKNIFTY).

Pure functions, no I/O.  Entry point:
    analyze_chain(chain_json, expiry, daily, vix, engine) -> dict

What it measures (and why a premium seller cares)
-------------------------------------------------
* ATM implied vol (IV) vs realised vol (HV10/HV20): sellers are paid the gap
  between what options *price in* and what the index *actually moves*.  IV well
  above HV = rich premium; IV below HV = you are being under-paid for the risk.
* Expected move (ATM straddle and 1σ = S·IV·√T): the market's own estimate of the
  range to expiry.  Short strikes outside it are where time decay works for you.
* OI walls / max pain / PCR: where option writers have the most at stake.  Big
  call OI above spot and put OI below often act as a range through expiry.
* Trend / ADX / S-R zones from the main engine: neutral structures (condor,
  strangle) need a range; a trending tape favours a one-sided credit spread.
* Events inside the expiry window and India VIX level: gap risk the premium
  must compensate for.

Candidate structures are screened, not advised: every number is an estimate
(lognormal, risk-neutral, at current IV) and real outcomes depend on gaps,
IV changes, slippage, margin and the trader's own risk limits.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time as dtime

import numpy as np
import pandas as pd

import config

RISK_FREE = 0.065          # ~ 91-day T-bill
EXPIRY_TIME = dtime(15, 30)
TARGET_SHORT_DELTA = 0.16  # ≈ 1σ OTM: the classic positional short strike
SPREAD_SHORT_DELTA = 0.28  # one-sided credit spreads sit a little closer
MIN_DTE_POSITIONAL = 7     # below this gamma dominates; not a positional trade

UNDERLYINGS = {
    "NIFTY": {"name": "NIFTY 50", "daily": "^NSEI", "lot": config.NIFTY_LOT, "wing": 200},   # min wing
    "BANKNIFTY": {"name": "NIFTY Bank", "daily": "NIFTY BANK", "lot": config.BANKNIFTY_LOT, "wing": 500},
}


# =========================================================== Black-Scholes
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_price(S, K, T, sigma, kind, r=RISK_FREE):
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if kind == "CE" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "CE":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def bs_greeks(S, K, T, sigma, kind, r=RISK_FREE) -> dict:
    """delta, gamma, theta (per calendar day), vega (per 1 vol point)."""
    if T <= 0 or sigma <= 0:
        itm = (S > K) if kind == "CE" else (S < K)
        return {"delta": (1.0 if kind == "CE" else -1.0) if itm else 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    gamma = _npdf(d1) / (S * sigma * sq)
    vega = S * _npdf(d1) * sq / 100
    if kind == "CE":
        delta = _ncdf(d1)
        theta = -S * _npdf(d1) * sigma / (2 * sq) - r * K * math.exp(-r * T) * _ncdf(d2)
    else:
        delta = _ncdf(d1) - 1
        theta = -S * _npdf(d1) * sigma / (2 * sq) + r * K * math.exp(-r * T) * _ncdf(-d2)
    return {"delta": delta, "gamma": gamma, "theta": theta / 365, "vega": vega}


def implied_vol(price, S, K, T, kind, r=RISK_FREE) -> float | None:
    """Bisection IV; None when the price is below intrinsic or unusable."""
    if price is None or price <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, S - K * math.exp(-r * T)) if kind == "CE" else max(0.0, K * math.exp(-r * T) - S)
    if price <= intrinsic + 1e-6:
        return None
    lo, hi = 0.005, 3.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_price(S, K, T, mid, kind, r) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def prob_between(S, lo, hi, T, sigma) -> float:
    """Risk-neutral lognormal P(lo < S_T < hi)."""
    if T <= 0 or sigma <= 0:
        return float(lo < S < hi)
    sd = sigma * math.sqrt(T)
    mu = math.log(S) + (RISK_FREE - 0.5 * sigma * sigma) * T

    def cdf(x):
        return 0.0 if x <= 0 else _ncdf((math.log(x) - mu) / sd)
    return max(0.0, cdf(hi) - cdf(lo)) if hi != math.inf else 1.0 - cdf(lo)


def prob_touch(S, K, T, sigma) -> float:
    """Rough probability of *touching* K before expiry (≈ 2 × P(finish beyond K))."""
    p_beyond = prob_between(S, K, math.inf, T, sigma) if K > S else prob_between(S, 0, K, T, sigma)
    return min(1.0, 2 * p_beyond)


# =========================================================== chain parsing
def parse_chain(js: dict, expiry: str) -> tuple[dict, pd.DataFrame]:
    rec = js.get("records") or {}
    spot = float(rec.get("underlyingValue") or 0)
    rows = []
    for d in rec.get("data", []):
        if d.get("expiryDates") not in (None, expiry):
            continue
        row = {"strike": float(d["strikePrice"])}
        for kind in ("CE", "PE"):
            o = d.get(kind) or {}
            p = kind.lower()
            row[f"{p}_oi"] = float(o.get("openInterest") or 0)
            row[f"{p}_chg_oi"] = float(o.get("changeinOpenInterest") or 0)
            row[f"{p}_ltp"] = float(o.get("lastPrice") or 0)
            row[f"{p}_bid"] = float(o.get("buyPrice1") or 0)
            row[f"{p}_ask"] = float(o.get("sellPrice1") or 0)
            row[f"{p}_vol"] = float(o.get("totalTradedVolume") or 0)
            row[f"{p}_nse_iv"] = float(o.get("impliedVolatility") or 0)
            row[f"{p}_px_chg"] = float(o.get("change") or 0)          # option price change vs previous close
            spot = spot or float(o.get("underlyingValue") or 0)
        rows.append(row)
    df = pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)
    meta = {"spot": spot, "timestamp": rec.get("timestamp"), "expiries": rec.get("expiryDates", [])}
    return meta, df


def years_to_expiry(expiry: str, now: datetime | None = None) -> float:
    ist_now = pd.Timestamp(now or pd.Timestamp.now(tz=config.IST))
    if ist_now.tzinfo is None:
        ist_now = ist_now.tz_localize(config.IST)
    exp = pd.Timestamp(datetime.combine(pd.to_datetime(expiry, format="%d-%b-%Y").date(), EXPIRY_TIME),
                       tz=config.IST)
    return max((exp - ist_now).total_seconds(), 0) / (365 * 24 * 3600)


def _fair(row, p) -> float:
    """Usable option price: bid/ask mid when both sides quote, else LTP."""
    b, a, l = row[f"{p}_bid"], row[f"{p}_ask"], row[f"{p}_ltp"]
    if b > 0 and a > 0 and a >= b:
        return 0.5 * (a + b)
    return l


def _sell_px(row, p) -> float:
    return row[f"{p}_bid"] if row[f"{p}_bid"] > 0 else row[f"{p}_ltp"]


def _buy_px(row, p) -> float:
    return row[f"{p}_ask"] if row[f"{p}_ask"] > 0 else row[f"{p}_ltp"]


def enrich(df: pd.DataFrame, S: float, T: float) -> pd.DataFrame:
    """Add own IV / delta for every strike (NSE's IV column is often 0 for ITM legs)."""
    df = df.copy()
    for p, kind in (("ce", "CE"), ("pe", "PE")):
        ivs, deltas = [], []
        for _, row in df.iterrows():
            iv = implied_vol(_fair(row, p), S, row["strike"], T, kind)
            if iv is None and row[f"{p}_nse_iv"] > 0:
                iv = row[f"{p}_nse_iv"] / 100
            ivs.append(iv)
            deltas.append(bs_greeks(S, row["strike"], T, iv, kind)["delta"] if iv else None)
        df[f"{p}_iv"] = ivs
        df[f"{p}_delta"] = deltas
    return df


# =========================================================== full chain view
BUILDUP = {(1, 1): "Long build-up", (-1, 1): "Short build-up", (1, -1): "Short covering", (-1, -1): "Long unwinding"}
CHAIN_SIDE = 40          # strikes each side of ATM kept for the chain view


def _sgn(v: float) -> int:
    return 1 if v > 0 else -1 if v < 0 else 0


def buildup(px_chg: float, oi_chg: float) -> str:
    """Classic price-vs-OI read: price up + OI up = long build-up, price down + OI up =
    short build-up (writers adding), price up + OI down = short covering, price down + OI
    down = long unwinding."""
    return BUILDUP.get((_sgn(px_chg), _sgn(oi_chg)), "")


def oi_flow(df: pd.DataFrame, px_ce: str = "ce_px_chg", px_pe: str = "pe_px_chg",
            oi_ce: str = "ce_chg_oi", oi_pe: str = "pe_chg_oi") -> dict:
    """OI additions / reductions per side and what they *possibly* are, from OI change with premium change
    (OI up + premium down = possible writing, OI up + premium up = possible buying, OI down + premium up =
    possible short covering, OI down + premium down = possible long unwinding).  Not confirmed identities."""
    out = {}
    for side, oc, pc in (("pe", oi_pe, px_pe), ("ce", oi_ce, px_ce)):
        oi, px = df[oc].astype(float), df[pc].astype(float)
        out[f"{side}_add"] = float(oi[oi > 0].sum())
        out[f"{side}_cut"] = float(-oi[oi < 0].sum())
        out[f"{side}_net"] = float(oi.sum())
        out[f"{side}_writing"] = float(oi[(oi > 0) & (px < 0)].sum())
        out[f"{side}_buying"] = float(oi[(oi > 0) & (px > 0)].sum())
        out[f"{side}_covering"] = float(-oi[(oi < 0) & (px > 0)].sum())
        out[f"{side}_unwinding"] = float(-oi[(oi < 0) & (px < 0)].sum())
    out["chg_pcr"] = out["pe_add"] / out["ce_add"] if out["ce_add"] > 0 else None
    added = out["pe_add"] + out["ce_add"]
    # put writing and call buying lean bullish; call writing and put buying lean bearish (spec table)
    bull = out["pe_writing"] + out["ce_buying"]
    bear = out["ce_writing"] + out["pe_buying"]
    out["flow_balance"] = (bull - bear) / added if added > 0 else None
    return out


def pcr_set(df: pd.DataFrame, S: float, step: float, n: int | None = None) -> dict:
    """The PCR indicators of docs/PCR_METHODOLOGY.md for one expiry, each computed on its own:
    overall OI PCR, volume PCR, change-in-OI PCR (additions only), n-strike and weighted PCR around ATM,
    and the strongest put / call concentrations."""
    n = n or config.PCR_NEAR_STRIKES
    ce_oi, pe_oi = float(df.ce_oi.sum()), float(df.pe_oi.sum())
    ce_v, pe_v = float(df.ce_vol.sum()), float(df.pe_vol.sum())
    i_atm = int((df["strike"] - S).abs().idxmin())
    half = n // 2
    win = df.iloc[max(i_atm - half, 0): i_atm + half + 1]
    w = [float(x) for x in str(config.PCR_WEIGHTS).split(",")]
    wt = np.array([w[min(abs(j - i_atm), len(w) - 1)] for j in win.index])
    near_ce, near_pe = float(win.ce_oi.sum()), float(win.pe_oi.sum())
    wce, wpe = float((win.ce_oi * wt).sum()), float((win.pe_oi * wt).sum())
    flow = oi_flow(df)
    top = lambda col, k=3: [{"strike": float(r.strike), "oi": float(r[col])} for _, r in df.nlargest(k, col).iterrows()
                            if r[col] > 0]
    return {"oi_pcr": pe_oi / ce_oi if ce_oi else None, "vol_pcr": pe_v / ce_v if ce_v else None,
            "chg_pcr": flow["chg_pcr"], "near_n": len(win),
            "near_pcr": near_pe / near_ce if near_ce else None, "weighted_pcr": wpe / wce if wce else None,
            "flow": flow, "top_put_oi": top("pe_oi"), "top_call_oi": top("ce_oi"),
            "top_put_add": top("pe_chg_oi"), "top_call_add": top("ce_chg_oi")}


def build_chain(df: pd.DataFrame, S: float, T: float, step: float, mp: float, lot: int) -> tuple[list[dict], dict]:
    """Per-strike rows for the option-chain table (±CHAIN_SIDE strikes around ATM)
    with IV, Greeks, price change, build-up and strike PCR, plus chain totals."""
    i_atm = int((df["strike"] - S).abs().idxmin())
    sub = df.iloc[max(i_atm - CHAIN_SIDE, 0): i_atm + CHAIN_SIDE + 1]
    rows = []
    for _, r in sub.iterrows():
        k = float(r.strike)
        row = {"strike": k, "pcr": (r.pe_oi / r.ce_oi) if r.ce_oi else None}
        for p, kind in (("ce", "CE"), ("pe", "PE")):
            iv = r[f"{p}_iv"]
            iv = None if iv is None or pd.isna(iv) or iv <= 0 else float(iv)     # no solvable IV (deep ITM)
            g = bs_greeks(S, k, T, iv, kind) if iv else {}
            ltp, chg = float(r[f"{p}_ltp"]), float(r[f"{p}_px_chg"])
            prev = ltp - chg
            row.update({
                f"{p}_oi": float(r[f"{p}_oi"]), f"{p}_chg_oi": float(r[f"{p}_chg_oi"]),
                f"{p}_oi_pct": (float(r[f"{p}_chg_oi"]) / (r[f"{p}_oi"] - r[f"{p}_chg_oi"]) * 100)
                if r[f"{p}_oi"] - r[f"{p}_chg_oi"] > 0 else None,
                f"{p}_ltp": ltp, f"{p}_chg": chg, f"{p}_chg_pct": chg / prev * 100 if prev > 0 else None,
                f"{p}_bid": float(r[f"{p}_bid"]), f"{p}_ask": float(r[f"{p}_ask"]), f"{p}_vol": float(r[f"{p}_vol"]),
                f"{p}_iv": iv * 100 if iv else None, f"{p}_delta": g.get("delta"), f"{p}_gamma": g.get("gamma"),
                f"{p}_theta": g.get("theta"), f"{p}_vega": g.get("vega"),
                f"{p}_intrinsic": max(0.0, S - k) if kind == "CE" else max(0.0, k - S),
                f"{p}_buildup": buildup(chg, float(r[f"{p}_chg_oi"])) if ltp else "",
            })
            row[f"{p}_time_value"] = max(ltp - row[f"{p}_intrinsic"], 0.0) if ltp else None
        rows.append(row)
    ce_oi, pe_oi = float(df.ce_oi.sum()), float(df.pe_oi.sum())
    ce_chg, pe_chg = float(df.ce_chg_oi.sum()), float(df.pe_chg_oi.sum())
    above, below = df[df.strike >= S], df[df.strike <= S]
    summary = {
        "ce_oi": ce_oi, "pe_oi": pe_oi, "ce_chg_oi": ce_chg, "pe_chg_oi": pe_chg,
        "pcr": pe_oi / ce_oi if ce_oi else None, "pcr_chg": oi_flow(df)["chg_pcr"],      # additions only
        "ce_vol": float(df.ce_vol.sum()), "pe_vol": float(df.pe_vol.sum()),
        "vol_pcr": float(df.pe_vol.sum()) / float(df.ce_vol.sum()) if df.ce_vol.sum() else None,
        "resistance": float(above.loc[above.ce_oi.idxmax(), "strike"]) if len(above) and above.ce_oi.max() > 0 else None,
        "support": float(below.loc[below.pe_oi.idxmax(), "strike"]) if len(below) and below.pe_oi.max() > 0 else None,
        "res_add": float(above.loc[above.ce_chg_oi.idxmax(), "strike"]) if len(above) and above.ce_chg_oi.max() > 0 else None,
        "sup_add": float(below.loc[below.pe_chg_oi.idxmax(), "strike"]) if len(below) and below.pe_chg_oi.max() > 0 else None,
        "max_pain": mp, "lot": lot, "step": step,
    }
    return rows, summary


# =========================================================== market read
def realised_vol(daily: pd.DataFrame | None, n: int) -> float | None:
    if daily is None or len(daily) < n + 1:
        return None
    r = np.log(daily["close"]).diff().dropna().tail(n)
    return float(r.std() * math.sqrt(252))


def max_pain(df: pd.DataFrame) -> float:
    strikes = df["strike"].values
    pain = [float((np.maximum(0, k - strikes) * df["ce_oi"].values).sum()
                  + (np.maximum(0, strikes - k) * df["pe_oi"].values).sum()) for k in strikes]
    return float(strikes[int(np.argmin(pain))])


def _walls(df: pd.DataFrame, S: float, side: str, n: int = 3) -> list[dict]:
    if side == "call":
        sub, col, chg = df[df.strike > S], "ce_oi", "ce_chg_oi"
    else:
        sub, col, chg = df[df.strike < S], "pe_oi", "pe_chg_oi"
    top = sub.nlargest(n, col)
    return [{"strike": float(r.strike), "oi": float(r[col]), "chg_oi": float(r[chg])} for _, r in top.iterrows()]


def _nearest(df: pd.DataFrame, x: float) -> pd.Series:
    return df.iloc[int((df["strike"] - x).abs().idxmin())]


def _liquid(df: pd.DataFrame, p: str) -> pd.Series:
    """Strikes you can actually trade: open interest, volume today, two-sided quote
    and a bid/ask spread within ~10% of the price (or ₹2 for cheap options)."""
    bid, ask = df[f"{p}_bid"], df[f"{p}_ask"]
    mid = (bid + ask) / 2
    if (bid > 0).sum() == 0:          # market closed: quotes cleared, fall back to OI + LTP
        return (df[f"{p}_oi"] > 0) & (df[f"{p}_ltp"] > 0)
    return ((df[f"{p}_oi"] > 0) & (df[f"{p}_vol"] > 0) & (bid > 0) & (ask >= bid)
            & ((ask - bid) <= np.maximum(2.0, 0.10 * mid)))


def _pick_by_delta(df: pd.DataFrame, p: str, target: float, S: float) -> pd.Series | None:
    side = df[(df.strike > S) if p == "ce" else (df.strike < S)]
    side = side[side[f"{p}_delta"].notna() & _liquid(side, p)]
    if side.empty:
        return None
    # among strikes within ±0.03 delta of target, prefer the deepest-OI (most liquid) one
    gap = (side[f"{p}_delta"].abs() - target).abs()
    near = side[gap <= gap.min() + 0.03]
    return near.loc[near[f"{p}_oi"].idxmax()]


def _row_at(df: pd.DataFrame, k: float, p: str | None = None) -> pd.Series | None:
    """Row at strike k; with `p`, the nearest *liquid* strike to k instead."""
    if p is None:
        m = df[df.strike == k]
        return None if m.empty else m.iloc[0]
    liq = df[_liquid(df, p)]
    if liq.empty:
        return None
    return liq.loc[(liq.strike - k).abs().idxmin()]


# =========================================================== strategies
@dataclass
class Leg:
    side: str      # "SELL" | "BUY"
    kind: str      # "CE" | "PE"
    strike: float
    price: float
    iv: float | None

    def as_dict(self):
        return {"side": self.side, "kind": self.kind, "strike": float(self.strike),
                "price": round(float(self.price), 2),
                "iv": None if self.iv is None or pd.isna(self.iv) else round(float(self.iv) * 100, 1)}


def _payoff(legs: list[Leg], ST: np.ndarray) -> np.ndarray:
    pnl = np.zeros_like(ST, dtype=float)
    for lg in legs:
        intrinsic = np.maximum(0, ST - lg.strike) if lg.kind == "CE" else np.maximum(0, lg.strike - ST)
        sgn = 1 if lg.side == "SELL" else -1
        pnl += sgn * (lg.price - intrinsic)
    return pnl


def _evaluate(name: str, legs: list[Leg], S: float, T: float, sigma: float, lot: int, defined: bool,
              real_sigma: float | None = None) -> dict:
    credit = sum((l.price if l.side == "SELL" else -l.price) for l in legs)
    grid = np.linspace(S * 0.6, S * 1.4, 4001)
    pnl = _payoff(legs, grid)
    # breakevens: sign changes of the expiry payoff
    sign = np.sign(pnl)
    idx = np.where(np.diff(sign) != 0)[0]
    bes = [float(grid[i] - pnl[i] * (grid[i + 1] - grid[i]) / (pnl[i + 1] - pnl[i])) for i in idx]
    lo_be = min((b for b in bes if b < S), default=0.0)
    hi_be = max((b for b in bes if b > S), default=math.inf)
    pop = prob_between(S, lo_be, hi_be, T, sigma)
    max_loss = float(-pnl.min()) if defined else None
    greeks = {"delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0}
    for l in legs:
        g = bs_greeks(S, l.strike, T, l.iv or sigma, l.kind)
        sgn = -1 if l.side == "SELL" else 1
        for k in greeks:
            greeks[k] += sgn * g[k]
    # expected value under the same lognormal (shows the trade is not "free money")
    z = np.random.default_rng(7).standard_normal(20000)
    ST = S * np.exp((RISK_FREE - 0.5 * sigma ** 2) * T + sigma * math.sqrt(T) * z)
    sim = _payoff(legs, ST)
    # same simulation at *realised* vol: positive = premium is rich vs how the index actually moves
    ev_real = pop_real = None
    if real_sigma:
        STr = S * np.exp(-0.5 * real_sigma ** 2 * T + real_sigma * math.sqrt(T) * z)
        simr = _payoff(legs, STr)
        ev_real = round(float(simr.mean()) * lot)
        pop_real = round(prob_between(S, lo_be, hi_be, T, real_sigma) * 100, 1)
    return {
        "name": name, "legs": [l.as_dict() for l in legs], "defined_risk": defined,
        "credit_pts": round(credit, 2), "credit_rs": round(credit * lot), "lot": lot,
        "max_loss_pts": None if max_loss is None else round(max_loss, 2),
        "max_loss_rs": None if max_loss is None else round(max_loss * lot),
        "breakevens": [round(b) for b in (lo_be, hi_be) if b not in (0.0, math.inf)],
        "pop": round(pop * 100, 1),
        "reward_risk": None if not max_loss else round(credit / max_loss, 2),
        "net_delta": round(greeks["delta"] * lot, 2), "theta_rs_day": round(greeks["theta"] * lot, 0),
        "vega_rs": round(greeks["vega"] * lot, 0),
        "cvar_rs": round(float(np.sort(sim)[: len(sim) // 20].mean()) * lot),   # avg of worst 5%
        "profit_target_rs": round(credit * 0.5 * lot), "stop_rs": round(credit * 1.5 * lot),
        "ev_real_rs": ev_real, "pop_real": pop_real,
    }


def _beyond(strike_row, wall: float | None, zones: list[dict], side: str, step: float, df) -> pd.Series:
    """Nudge a short strike so it sits beyond the OI wall / S-R zone if that is close by."""
    k = float(strike_row["strike"])
    targets = []
    if wall:
        targets.append(wall)
    for z in zones[:2]:
        targets.append(z["high"] if side == "call" else z["low"])
    for t in targets:
        if side == "call" and k <= t <= k + 3 * step:
            k = math.ceil(t / step) * step
        if side == "put" and k - 3 * step <= t <= k:
            k = math.floor(t / step) * step
    r = _row_at(df, k)
    return strike_row if r is None or not bool(_liquid(df.loc[[r.name]], "ce" if side == "call" else "pe").iloc[0]) else r


def build_strategies(df, S, T, sigma, lot, wing, step, call_wall, put_wall, sup_zones, res_zones, bias,
                     real_sigma=None) -> list[dict]:
    out = []
    # wings scale with the expected move (~0.5σ) so the credit is worth the width
    sigma_pts = S * sigma * math.sqrt(T)
    wing = max(wing, round(0.5 * sigma_pts / step) * step)
    sc = _pick_by_delta(df, "ce", TARGET_SHORT_DELTA, S)
    sp = _pick_by_delta(df, "pe", TARGET_SHORT_DELTA, S)
    if sc is not None and sp is not None:
        sc = _beyond(sc, call_wall, res_zones, "call", step, df)
        sp = _beyond(sp, put_wall, sup_zones, "put", step, df)
        lc, lp = _row_at(df, sc.strike + wing, "ce"), _row_at(df, sp.strike - wing, "pe")
        if lc is not None and lc.strike <= sc.strike:
            lc = None
        if lp is not None and lp.strike >= sp.strike:
            lp = None
        shorts = [Leg("SELL", "CE", sc.strike, _sell_px(sc, "ce"), sc.ce_iv),
                  Leg("SELL", "PE", sp.strike, _sell_px(sp, "pe"), sp.pe_iv)]
        if lc is not None and lp is not None:
            ic = _evaluate("Iron condor", shorts + [Leg("BUY", "CE", lc.strike, _buy_px(lc, "ce"), lc.ce_iv),
                                                    Leg("BUY", "PE", lp.strike, _buy_px(lp, "pe"), lp.pe_iv)],
                           S, T, sigma, lot, True, real_sigma)
            ic["kind"] = "neutral"
            out.append(ic)
        st = _evaluate("Short strangle", shorts, S, T, sigma, lot, False, real_sigma)
        st["kind"] = "neutral"
        out.append(st)

    # one-sided credit spread in the direction of the bias
    if bias in ("bullish", "bearish"):
        p, kind = ("pe", "PE") if bias == "bullish" else ("ce", "CE")
        short = _pick_by_delta(df, p, SPREAD_SHORT_DELTA, S)
        if short is not None:
            short = _beyond(short, put_wall if bias == "bullish" else call_wall,
                            sup_zones if bias == "bullish" else res_zones,
                            "put" if bias == "bullish" else "call", step, df)
            long_k = short.strike - wing if bias == "bullish" else short.strike + wing
            lr = _row_at(df, long_k, p)
            if lr is not None and lr.strike != short.strike:
                cs = _evaluate("Bull put spread" if bias == "bullish" else "Bear call spread",
                               [Leg("SELL", kind, short.strike, _sell_px(short, p), short[f"{p}_iv"]),
                                Leg("BUY", kind, lr.strike, _buy_px(lr, p), lr[f"{p}_iv"])],
                               S, T, sigma, lot, True, real_sigma)
                cs["kind"] = bias
                out.append(cs)
    return out


# =========================================================== top level
def analyze_chain(underlying: str, js: dict, expiry: str, daily: pd.DataFrame | None = None,
                  vix: pd.Series | None = None, engine: dict | None = None,
                  now: datetime | None = None) -> dict:
    u = UNDERLYINGS[underlying]
    meta, raw = parse_chain(js, expiry)
    S = meta["spot"]
    T = years_to_expiry(expiry, now)
    dte = T * 365
    df = enrich(raw, S, T)
    step = float(np.diff(df.strike.values)[np.abs(df.strike.values[:-1] - S).argmin()]) if len(df) > 1 else 50.0

    atm = _nearest(df, S)
    atm_ivs = [v for v in (atm.ce_iv, atm.pe_iv) if v]
    atm_iv = float(np.mean(atm_ivs)) if atm_ivs else None
    straddle = _fair(atm, "ce") + _fair(atm, "pe")
    sigma_move = S * atm_iv * math.sqrt(T) if atm_iv else None
    hv10, hv20 = realised_vol(daily, 10), realised_vol(daily, 20)

    ce_oi, pe_oi = df.ce_oi.sum(), df.pe_oi.sum()
    pcr = pe_oi / ce_oi if ce_oi else None
    pset = pcr_set(df, S, step)
    pcr_chg = pset["chg_pcr"]                        # put OI added / call OI added (reductions excluded)
    flow_bal = pset["flow"]["flow_balance"]
    cwalls, pwalls = _walls(df, S, "call"), _walls(df, S, "put")
    mp = max_pain(df)

    skew = None
    if sigma_move:
        put_iv = _nearest(df, S - sigma_move).pe_iv
        call_iv = _nearest(df, S + sigma_move).ce_iv
        if put_iv and call_iv:
            skew = round((put_iv - call_iv) * 100, 2)

    vix_now = vix_pct = None
    if vix is not None and len(vix.dropna()):
        v = vix.dropna()
        vix_now = round(float(v.iloc[-1]), 2)
        vix_pct = round(float((v.tail(252) <= vix_now).mean() * 100))

    regime = (engine or {}).get("regime") or {}
    signal = (engine or {}).get("signal") or {}
    sup_zones = (engine or {}).get("support_zones") or []
    res_zones = (engine or {}).get("resistance_zones") or []

    # ---- directional bias (for one-sided spreads only)
    votes = []
    trend = (regime.get("trend") or "").lower()
    if "bull" in trend or "up" in trend:
        votes.append(1)
    elif "bear" in trend or "down" in trend:
        votes.append(-1)
    # one positioning vote, from OI change *with* premium change (writing vs buying) — not from the PCR level,
    # which alone doesn't say bullish or bearish, and not twice from correlated PCR measures
    if flow_bal is not None:
        votes.append(1 if flow_bal >= config.FLOW_MIN_SHARE else -1 if flow_bal <= -config.FLOW_MIN_SHARE else 0)
    if signal.get("recommendation", "").startswith("ACCUMULATE"):
        votes.append(1)
    if signal.get("recommendation", "").startswith(("PROFIT BOOKING", "AVOID")):
        votes.append(-1)
    sv = sum(votes)
    bias = "bullish" if sv >= 2 else "bearish" if sv <= -2 else "neutral"

    # ---- environment checklist for premium selling
    checks: list[tuple[int, str]] = []
    vrp = None
    if atm_iv and hv20:
        vrp = atm_iv / hv20
        if vrp >= 1.15:
            checks.append((1, f"IV {atm_iv*100:.1f}% is {vrp:.2f}× 20-day realised vol {hv20*100:.1f}% — "
                              "options are pricing more movement than the index has been delivering, "
                              "which is the core edge for a seller."))
        elif vrp >= 0.95:
            checks.append((0, f"IV {atm_iv*100:.1f}% ≈ realised vol {hv20*100:.1f}% — premium is fair, not rich; "
                              "edge comes only from strike placement and time decay."))
        else:
            checks.append((-1, f"IV {atm_iv*100:.1f}% is BELOW realised vol {hv20*100:.1f}% — you'd be selling "
                               "cheap insurance into a market that is moving more than priced."))
    if hv10 and hv20 and hv10 > hv20 * 1.25:
        checks.append((-1, f"Realised vol is expanding (HV10 {hv10*100:.1f}% vs HV20 {hv20*100:.1f}%)."))
    if dte < MIN_DTE_POSITIONAL:
        checks.append((-1, f"Only {dte:.1f} days to expiry — gamma risk is high and decay is mostly spent; "
                           "for a positional seller a later expiry is usually better."))
    elif dte <= 45:
        checks.append((1, f"{dte:.0f} days to expiry — inside the 7–45 day window where theta decay is "
                          "meaningful but gamma is still manageable for a hold-to-target trade."))
    else:
        checks.append((0, f"{dte:.0f} days to expiry — decay is slow this far out."))
    if vix_now is not None:
        if vix_now >= 22:
            checks.append((0, f"India VIX {vix_now:.1f} is high ({vix_pct}th pct) — premiums are fat but so are "
                              "gaps; prefer defined-risk structures and smaller size."))
        elif vix_now <= 11:
            checks.append((-1, f"India VIX {vix_now:.1f} is very low ({vix_pct}th pct) — little premium and "
                               "vol-expansion risk works against sellers."))
        else:
            checks.append((1, f"India VIX {vix_now:.1f} ({vix_pct}th pct of the last year) — normal regime."))
    adx = regime.get("adx")
    if adx is not None:
        if adx >= 28 and bias != "neutral":
            checks.append((-1, f"ADX {adx:.0f}: the index is trending ({bias}) — neutral strangles/condors get "
                               "run over in trends; a one-sided credit spread fits better."))
        elif adx < 20:
            checks.append((1, f"ADX {adx:.0f}: no strong trend — range-bound tape suits neutral structures."))
    if cwalls and pwalls:
        cw, pw = cwalls[0]["strike"], pwalls[0]["strike"]
        width = (cw - pw) / S * 100
        checks.append((1 if sigma_move and (cw - S) > 0.6 * sigma_move and (S - pw) > 0.6 * sigma_move else 0,
                       f"Largest call OI at {cw:,.0f} and put OI at {pw:,.0f} ({width:.1f}% wide) — writers are "
                       "defending this band; it is the market's own range estimate."))
    exp_date = pd.to_datetime(expiry, format="%d-%b-%Y").date()
    from indicators import event_calendar
    events = [e for e in event_calendar(date.today(), max(int(dte) + 1, 1))
              if e["date"] <= exp_date and e["category"] != "Derivatives expiry"]
    if events:
        checks.append((-1, "Event risk before expiry: " + "; ".join(f"{e['event']} ({e['date']:%d-%b})"
                                                                     for e in events)
                           + " — expect an IV pop / gap around it."))

    score = sum(s for s, _ in checks)
    verdict = ("FAVOURABLE for premium selling" if score >= 3 else
               "MIXED — size down, prefer defined risk" if score >= 1 else
               "UNFAVOURABLE — premium not paying for the risk")

    strategies = []
    if atm_iv and T > 0:
        strategies = build_strategies(df, S, T, atm_iv, u["lot"], u["wing"], step,
                                      cwalls[0]["strike"] if cwalls else None,
                                      pwalls[0]["strike"] if pwalls else None, sup_zones, res_zones, bias,
                                      hv20)
        for s in strategies:
            s["why"], s["risks"], s["exit"] = _explain(s, S, sigma_move, cwalls, pwalls, vrp, dte, bias,
                                                       adx, events, sup_zones, res_zones)
            s["fit"] = _fit(s, bias, adx, score)
        strategies.sort(key=lambda s: -s["fit"])

    chain, chain_sum = build_chain(df, S, T, step, mp, u["lot"])
    half = max(12 * step, 1.6 * (sigma_move or 0))     # wide enough to show the short strikes
    near = df[(df.strike >= S - half) & (df.strike <= S + half)]
    return {
        "underlying": underlying, "name": u["name"], "expiry": expiry, "expiries": meta["expiries"],
        "timestamp": meta["timestamp"], "spot": S, "dte": round(dte, 1), "step": step, "lot": u["lot"],
        "atm": float(atm.strike), "atm_iv": None if atm_iv is None else round(atm_iv * 100, 2),
        "hv10": None if hv10 is None else round(hv10 * 100, 2), "hv20": None if hv20 is None else round(hv20 * 100, 2),
        "vrp": None if vrp is None else round(vrp, 2), "straddle": round(straddle, 2),
        "sigma_move": None if sigma_move is None else round(sigma_move, 1),
        "pcr": None if pcr is None else round(pcr, 2), "pcr_chg": None if pcr_chg is None else round(pcr_chg, 2),
        "pcr_set": pset,
        # every strike (not just the ±CHAIN_SIDE view): the intraday snapshots and the PCR board use these so the
        # overall PCR covers the whole expiry and past / present compare like with like
        "strikes_all": {float(r.strike): {"ce_oi": float(r.ce_oi), "pe_oi": float(r.pe_oi), "ce_ltp": float(r.ce_ltp),
                                          "pe_ltp": float(r.pe_ltp), "ce_vol": float(r.ce_vol), "pe_vol": float(r.pe_vol),
                                          "ce_iv": float(r.ce_iv) * 100 if r.ce_iv and not pd.isna(r.ce_iv) else None,
                                          "pe_iv": float(r.pe_iv) * 100 if r.pe_iv and not pd.isna(r.pe_iv) else None}
                        for r in df.itertuples()},
        "max_pain": mp, "call_walls": cwalls, "put_walls": pwalls, "skew": skew,
        "vix": vix_now, "vix_pct": vix_pct, "bias": bias, "adx": adx, "trend": regime.get("trend"),
        "checks": checks, "score": score, "verdict": verdict, "events": events,
        "strategies": strategies,
        "chain": chain, "chain_summary": chain_sum,
        "oi_table": near[["strike", "ce_oi", "pe_oi", "ce_chg_oi", "pe_chg_oi", "ce_ltp", "pe_ltp",
                          "ce_iv", "pe_iv"]].to_dict("records"),
    }


def _edge(s: dict) -> int:
    """+1 / 0 / −1: realised-vol expectancy beyond ±10% of the credit."""
    ev = s.get("ev_real_rs")
    if ev is None or not s["credit_rs"]:
        return 0
    return 1 if ev > 0.1 * s["credit_rs"] else -1 if ev < -0.1 * s["credit_rs"] else 0


def _fit(s: dict, bias: str, adx, score: int) -> int:
    f = score
    f += {1: 1, 0: 0, -1: -2}[_edge(s)]
    if s["kind"] == "neutral":
        f += 2 if bias == "neutral" else -1
        f += 1 if (adx or 0) < 22 else -1 if (adx or 0) >= 28 else 0
        f += 1 if s["defined_risk"] else -1     # strangle only beats condor on premium, not on risk
    else:
        f += 3 if bias == s["kind"] else -3
    return f


def _explain(s, S, sigma_move, cwalls, pwalls, vrp, dte, bias, adx, events, sup, res):
    shorts = [l for l in s["legs"] if l["side"] == "SELL"]
    why, risks, exit_ = [], [], []
    for l in shorts:
        dist = abs(l["strike"] - S)
        sig = f" ({dist / sigma_move:.2f}σ)" if sigma_move else ""
        side = "above" if l["kind"] == "CE" else "below"
        txt = f"Short {l['strike']:,.0f} {l['kind']} is {dist:,.0f} pts {side} spot{sig}"
        walls = cwalls if l["kind"] == "CE" else pwalls
        if walls and ((l["kind"] == "CE" and l["strike"] >= walls[0]["strike"]) or
                      (l["kind"] == "PE" and l["strike"] <= walls[0]["strike"])):
            txt += f", at/behind the {walls[0]['strike']:,.0f} OI wall ({walls[0]['oi']:,.0f} contracts open)"
        zones = res if l["kind"] == "CE" else sup
        if zones:
            z = zones[0]
            if (l["kind"] == "CE" and l["strike"] >= z["low"]) or (l["kind"] == "PE" and l["strike"] <= z["high"]):
                txt += f", beyond the {z['low']:,.0f}–{z['high']:,.0f} {'resistance' if l['kind']=='CE' else 'support'} zone"
        why.append(txt + ".")
    why.append(f"Model probability of expiring between breakevens {', '.join(f'{b:,}' for b in s['breakevens'])}: "
               f"{s['pop']:.0f}% — you keep the premium in that range as time passes "
               f"(≈ ₹{s['theta_rs_day']:,.0f}/lot/day of decay at today's prices).")
    if s.get("ev_real_rs") is not None:
        ev, edge = s["ev_real_rs"], _edge(s)
        why.append(f"If the index keeps moving like its last 20 sessions, the simulated average outcome is "
                   f"{'+' if ev >= 0 else '−'}₹{abs(ev):,}/lot (win-rate {s['pop_real']:.0f}%) — "
                   + ("a positive expectancy: the premium more than pays for the expected movement."
                      if edge > 0 else
                      "a NEGATIVE expectancy: on recent movement the premium does not cover the losses on "
                      "the bad outcomes." if edge < 0 else
                      "roughly break-even: the win-rate is high, but the occasional loss eats the premium."))
    if vrp:
        why.append(f"IV/realised-vol ratio {vrp:.2f}: " +
                   ("options are rich — the market is paying you more than recent movement justifies."
                    if vrp >= 1.15 else "premium is roughly fair." if vrp >= 0.95
                    else "premium is cheap relative to actual movement — the main edge is missing."))
    if s["kind"] != "neutral":
        why.append(f"Directional read is {bias} (trend and the OI writing / buying flow), so only the side you expect "
                   "price to move away from is sold.")

    if not s["defined_risk"]:
        risks.append("UNLIMITED loss on a gap through either short strike; margin is several times a condor's.")
    else:
        risks.append(f"Max loss ₹{s['max_loss_rs']:,} per lot (reward:risk {s['reward_risk']}) if price closes "
                     "beyond a long wing at expiry.")
    risks.append(f"Average loss in the worst 5% of simulated outcomes: ₹{abs(s['cvar_rs']):,} per lot — "
                 "a high win-rate does not mean small losses.")
    if s["vega_rs"] < 0:
        risks.append(f"Short vega ₹{abs(s['vega_rs']):,.0f}/lot per 1 IV point: an IV spike hurts even if price "
                     "doesn't move.")
    if adx and adx >= 28 and s["kind"] == "neutral":
        risks.append(f"Trend strength (ADX {adx:.0f}) raises the chance one short strike is tested.")
    for e in events:
        risks.append(f"{e['event']} on {e['date']:%d-%b} falls inside this expiry.")

    exit_.append(f"Book profit at ~50% of credit (≈ ₹{s['profit_target_rs']:,}/lot) — the last half of the "
                 "premium takes longest to earn and carries the most gamma risk.")
    exit_.append(f"Cut if the position loses ≈ 1.5× credit (₹{s['stop_rs']:,}/lot) or spot closes beyond a "
                 "short strike on a daily basis — don't wait for expiry hoping for a reversal.")
    if s["kind"] == "neutral":
        exit_.append("If one side is threatened, roll the untested side closer (collect more credit) rather "
                     "than widening the losing side.")
    exit_.append("Close or roll 1–2 sessions before expiry to avoid expiry-day gamma swings.")
    return why, risks, exit_
