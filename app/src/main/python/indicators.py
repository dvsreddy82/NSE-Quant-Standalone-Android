"""Quant engine: S/R detection, pivots, volume profile, FII/DII correlation,
catalyst attribution and the NIFTYBEES swing-signal engine.

Everything here is pure pandas/numpy (no I/O) so it is unit-testable and can be
reused from notebooks or a scheduler as well as from the GUI.

Entry point: `analyze(symbol, timeframe, df, daily, fii, macro)` -> dict.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

import config


# =================================================================== basics
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=min(n, len(s))).mean()


def rsi(close: pd.Series, n: int = config.RSI_PERIOD) -> pd.Series:
    """Wilder's RSI."""
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0).where(dn.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift()
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
                     axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up, dn = df["high"].diff(), -df["low"].diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    mdi = 100 * minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for n in (20, 50, 200):
        out[f"ema{n}"] = ema(out["close"], n)
    out["rsi"] = rsi(out["close"])
    out["atr"] = atr(out)
    out["adx"] = adx(out)
    return out


# =================================================================== pivots
def pivot_levels(h: float, l: float, c: float) -> dict[str, dict[str, float]]:
    """Standard (floor), Fibonacci and Camarilla pivots from a period's H/L/C."""
    p = (h + l + c) / 3
    r = h - l
    return {
        "standard": {"P": p, "R1": 2 * p - l, "S1": 2 * p - h, "R2": p + r, "S2": p - r,
                     "R3": h + 2 * (p - l), "S3": l - 2 * (h - p)},
        "fibonacci": {"P": p, "R1": p + 0.382 * r, "S1": p - 0.382 * r, "R2": p + 0.618 * r,
                      "S2": p - 0.618 * r, "R3": p + r, "S3": p - r},
        "camarilla": {"R1": c + r * 1.1 / 12, "S1": c - r * 1.1 / 12,
                      "R2": c + r * 1.1 / 6, "S2": c - r * 1.1 / 6,
                      "R3": c + r * 1.1 / 4, "S3": c - r * 1.1 / 4,
                      "R4": c + r * 1.1 / 2, "S4": c - r * 1.1 / 2},
    }


# month-end alias: "ME" from pandas 2.2 ("M" is deprecated there); the Android build ships pandas 2.1
MONTH_END = "ME" if tuple(int(x) for x in pd.__version__.split(".")[:2]) >= (2, 2) else "M"


def period_pivots(daily: pd.DataFrame, freq: str) -> dict:
    """Pivots for the *current* week ('W') or month ('M'), derived from the last
    completed period. Early in a month this is exactly the projection for the
    upcoming month."""
    rule = {"W": "W-FRI", "M": MONTH_END}[freq]
    agg = daily.resample(rule).agg({"high": "max", "low": "min", "close": "last"}).dropna()
    if agg.empty:
        return {}
    # Only periods whose label (period end) is before today are complete.
    now = pd.Timestamp.now(tz=daily.index.tz or config.IST).normalize()
    completed = agg[agg.index.normalize() < now]
    if completed.empty:
        completed = agg
    row = completed.iloc[-1]
    return {"based_on": completed.index[-1].strftime("%Y-%m-%d"),
            **pivot_levels(row["high"], row["low"], row["close"])}


# =================================================================== volume profile
def volume_profile(df: pd.DataFrame, bins: int = config.VP_BINS,
                   va_pct: float = config.VALUE_AREA_PCT) -> dict:
    """Distribute each bar's volume uniformly across the price bins it spans.
    Falls back to a time-at-price (TPO) profile when the feed has no volume
    (common for the ^NSEI index on Yahoo)."""
    lo, hi = float(df["low"].min()), float(df["high"].max())
    if not math.isfinite(lo) or hi <= lo:
        return {}
    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    vol = df["volume"].fillna(0).to_numpy(dtype=float)
    tpo = vol.sum() <= 0
    if tpo:
        vol = np.ones(len(df))
    hist = np.zeros(bins)
    for l, h, v in zip(df["low"].to_numpy(), df["high"].to_numpy(), vol):
        i0 = max(int(np.searchsorted(edges, l, side="right")) - 1, 0)
        i1 = min(int(np.searchsorted(edges, h, side="left")), bins)
        i1 = max(i1, i0 + 1)
        hist[i0:i1] += v / (i1 - i0)

    poc_i = int(hist.argmax())
    # Grow the value area outward from the POC, always taking the heavier side.
    target, acc, a, b = hist.sum() * va_pct, hist[poc_i], poc_i, poc_i
    while acc < target and (a > 0 or b < bins - 1):
        nxt_dn = hist[a - 1] if a > 0 else -1
        nxt_up = hist[b + 1] if b < bins - 1 else -1
        if nxt_up >= nxt_dn:
            b += 1
            acc += hist[b]
        else:
            a -= 1
            acc += hist[a]
    # High-volume nodes: local maxima above the 70th percentile.
    thr = np.percentile(hist, 70)
    hvn = [float(centers[i]) for i in range(1, bins - 1)
           if hist[i] >= thr and hist[i] >= hist[i - 1] and hist[i] >= hist[i + 1]]
    return {"poc": float(centers[poc_i]), "vah": float(edges[b + 1]), "val": float(edges[a]),
            "prices": centers.tolist(), "volumes": hist.tolist(), "hvn": hvn, "tpo_mode": bool(tpo)}


# =================================================================== swings & zones
def swing_points(df: pd.DataFrame, window: int = config.SWING_WINDOW) -> pd.DataFrame:
    """Fractal swing highs/lows: bar is the extreme of +/- `window` bars."""
    h, l = df["high"], df["low"]
    w = 2 * window + 1
    is_hi = h == h.rolling(w, center=True).max()
    is_lo = l == l.rolling(w, center=True).min()
    rows = [(ts, float(h[ts]), "high") for ts in df.index[is_hi.fillna(False)]]
    rows += [(ts, float(l[ts]), "low") for ts in df.index[is_lo.fillna(False)]]
    return pd.DataFrame(rows, columns=["ts", "price", "kind"]).sort_values("ts").reset_index(drop=True)


@dataclass
class Zone:
    low: float
    high: float
    strength: float = 0.0
    touches: int = 0
    sources: list[str] = field(default_factory=list)

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2

    def to_dict(self) -> dict:
        d = asdict(self)
        d["mid"] = round(self.mid, 2)
        d["low"], d["high"], d["strength"] = round(self.low, 2), round(self.high, 2), round(self.strength, 2)
        d["sources"] = sorted(set(self.sources))
        return d


def count_touches(df: pd.DataFrame, low: float, high: float) -> int:
    """Distinct visits: consecutive bars inside the zone count as one touch, and
    a touch only counts if price then left the zone (i.e. it was respected)."""
    inside = (df["high"] >= low) & (df["low"] <= high)
    starts = inside & ~inside.shift(fill_value=False)
    return int(starts.sum())


def build_zones(df: pd.DataFrame, candidates: list[tuple[float, float, str]], atr_val: float,
                half_width_mult: float = config.ZONE_ATR_MULT) -> list[Zone]:
    """Cluster weighted candidate levels into zones.
    candidates: (price, weight, source_label)."""
    if not candidates or not atr_val or not math.isfinite(atr_val):
        return []
    half = atr_val * half_width_mult
    cands = sorted(candidates, key=lambda c: c[0])
    # Anchor-based clustering: a cluster may span at most 2*half from its first
    # member, so dense ladders of levels cannot chain into one giant zone.
    clusters: list[list[tuple[float, float, str]]] = [[cands[0]]]
    for c in cands[1:]:
        if c[0] - clusters[-1][0][0] <= 2 * half:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    zones = []
    for cl in clusters:
        prices = np.array([c[0] for c in cl])
        weights = np.array([c[1] for c in cl])
        center = float((prices * weights).sum() / weights.sum())
        z = Zone(low=center - half, high=center + half, sources=[c[2] for c in cl])
        z.strength = float(weights.sum())
        # Neighbouring clusters whose centres sit within one half-width are the same area.
        if zones and z.mid - zones[-1].mid <= half:
            prev = zones[-1]
            prev.high, prev.strength = z.high, prev.strength + z.strength
            prev.sources += z.sources
        else:
            if zones and z.low < zones[-1].high:   # partial overlap: split at the midpoint
                zones[-1].high = z.low = (zones[-1].high + z.low) / 2
            zones.append(z)
    for z in zones:
        z.touches = count_touches(df, z.low, z.high)
        z.strength += 0.5 * min(z.touches, 8)
    return zones


# =================================================================== divergence
def rsi_divergence(df: pd.DataFrame, lookback: int = 40, window: int = config.SWING_WINDOW) -> dict:
    """Compare the last two confirmed swing lows/highs inside `lookback` bars."""
    sub = df.tail(lookback)
    sw = swing_points(sub, window)
    res = {"bullish": False, "bearish": False, "detail": ""}
    lows = sw[sw.kind == "low"].tail(2)
    if len(lows) == 2:
        (t1, p1), (t2, p2) = lows[["ts", "price"]].itertuples(index=False)
        r1, r2 = sub.at[t1, "rsi"], sub.at[t2, "rsi"]
        if p2 < p1 and r2 > r1 and r2 < 50:
            res["bullish"] = True
            res["detail"] = f"Bullish RSI divergence: price LL {p1:.2f}->{p2:.2f}, RSI HL {r1:.0f}->{r2:.0f}"
    highs = sw[sw.kind == "high"].tail(2)
    if len(highs) == 2:
        (t1, p1), (t2, p2) = highs[["ts", "price"]].itertuples(index=False)
        r1, r2 = sub.at[t1, "rsi"], sub.at[t2, "rsi"]
        if p2 > p1 and r2 < r1 and r1 > 55:
            res["bearish"] = True
            res["detail"] = (res["detail"] + "; " if res["detail"] else "") + \
                f"Bearish RSI divergence: price HH {p1:.2f}->{p2:.2f}, RSI LH {r1:.0f}->{r2:.0f}"
    return res


# =================================================================== regime
def market_regime(df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    slope = (df["ema20"].iloc[-1] / df["ema20"].iloc[-6] - 1) * 100 if len(df) > 6 else 0.0
    adx_v = float(last["adx"]) if pd.notna(last["adx"]) else 0.0
    above200 = pd.notna(last["ema200"]) and last["close"] > last["ema200"]
    if adx_v < 20:
        trend = "Range-bound"
    elif last["close"] > last["ema50"] and last["ema20"] > last["ema50"] and slope > 0:
        trend = "Bullish"
    elif last["close"] < last["ema50"] and last["ema20"] < last["ema50"] and slope < 0:
        trend = "Bearish"
    else:
        trend = "Range-bound"
    return {"trend": trend, "adx": round(adx_v, 1), "ema20_slope_pct": round(float(slope), 2),
            "above_ema200": bool(above200)}


# =================================================================== institutional
def institutional_analysis(daily: pd.DataFrame, fii: pd.DataFrame) -> dict:
    out = {"available": False, "drivers": [], "thesis": "No institutional data yet",
           "bias": 0}
    if fii is None or fii.empty:
        return out
    f = fii.copy()
    f.index = pd.to_datetime(f.index).normalize()
    px = daily["close"].copy()
    px.index = px.index.tz_localize(None).normalize() if px.index.tz is not None else px.index.normalize()
    px = px[~px.index.duplicated(keep="last")]
    ret = px.pct_change() * 100
    j = f.join(ret.rename("ret"), how="inner")
    out["available"] = True

    def tail_sum(col, n):
        return float(f[col].dropna().tail(n).sum()) if col in f and f[col].notna().any() else None

    def latest(col):
        return float(f[col].dropna().iloc[-1]) if col in f and f[col].notna().any() else None

    out["fii_cash_5d"] = tail_sum("fii_cash_net", 5)
    out["dii_cash_5d"] = tail_sum("dii_cash_net", 5)
    out["fii_cash_last"] = latest("fii_cash_net")
    out["dii_cash_last"] = latest("dii_cash_net")
    out["fii_ls"] = latest("fii_index_futures_long_short_ratio")
    out["client_ls"] = latest("client_ratio")
    out["pcr"] = latest("pcr")
    ls = f.get("fii_index_futures_long_short_ratio", pd.Series(dtype=float)).dropna()
    out["fii_ls_change_5d"] = float(ls.iloc[-1] - ls.iloc[-6]) if len(ls) > 5 else None
    # FII index-futures positioning has been structurally short for long stretches,
    # so judge it against its own recent history as well as absolute thresholds.
    out["fii_ls_percentile"] = (round(float((ls.tail(60) <= ls.iloc[-1]).mean() * 100), 0)
                                if len(ls) >= 15 else None)

    # Correlations: does FII flow lead/move with price?
    corr = {}
    if "fii_cash_net" in j and j["fii_cash_net"].notna().sum() >= 10:
        corr["fii_cash_vs_return"] = float(j["fii_cash_net"].corr(j["ret"]))
    if "fii_index_fut_net_oi" in j and j["fii_index_fut_net_oi"].notna().sum() >= 10:
        corr["fii_fut_oi_chg_vs_return"] = float(j["fii_index_fut_net_oi"].diff().corr(j["ret"]))
    out["correlation"] = {k: round(v, 2) for k, v in corr.items() if math.isfinite(v)}

    # ---- multi-factor thesis
    ret5 = float((px.iloc[-1] / px.iloc[-6] - 1) * 100) if len(px) > 5 else 0.0
    out["price_ret_5d"] = round(ret5, 2)
    fii5, dii5, fls, cls = out["fii_cash_5d"], out["dii_cash_5d"], out["fii_ls"], out["client_ls"]
    lsd = out["fii_ls_change_5d"]
    bias, d = 0, out["drivers"]
    pct = out["fii_ls_percentile"]
    fii_short_heavy = fls is not None and (
        (pct is not None and pct <= 20) or (fls < config.FII_LS_BEARISH and (lsd or 0) < 0))
    fii_long_heavy = fls is not None and (fls > config.FII_LS_BULLISH or (pct is not None and pct >= 80))
    out["fii_short_heavy"] = fii_short_heavy

    if ret5 > 0.5 and ((fii5 or 0) > 0 or (lsd or 0) > 0.05) and not fii_short_heavy:
        out["thesis"] = "Institution-driven up-move (FII buying / adding longs with price)"
        bias += 2
    elif ret5 > 0.5 and (fii_short_heavy or (lsd or 0) < -0.05) and (cls or 0) > 1.2:
        out["thesis"] = "RETAIL TRAP divergence: price rising while FIIs short index futures and clients are long"
        bias -= 2
    elif ret5 < -0.5 and (fii5 or 0) < 0 and (dii5 or 0) > abs(fii5 or 0) * 0.8:
        out["thesis"] = "FII selling being absorbed by DIIs — potential base building"
        bias += 1
    elif ret5 < -0.5 and (fii5 or 0) < 0:
        out["thesis"] = "Institutional distribution (FII selling drives the decline)"
        bias -= 2
    else:
        out["thesis"] = "Mixed / neutral institutional positioning"

    if fii5 is not None:
        d.append(f"FII cash 5-session net: ₹{fii5:,.0f} cr")
        bias += 1 if fii5 > 0 else -1 if fii5 < -5000 else 0
    if dii5 is not None:
        d.append(f"DII cash 5-session net: ₹{dii5:,.0f} cr")
        bias += 1 if dii5 > 3000 else 0
    if fls is not None:
        tag = "heavily SHORT" if fii_short_heavy else "net LONG" if fii_long_heavy else "neutral"
        chg = f" ({lsd:+.2f} in 5d" + (f", {pct:.0f}th pct of 60d" if pct is not None else "") + ")" \
            if lsd is not None else ""
        d.append(f"FII index-futures long/short {fls:.2f}{chg} — {tag}")
        bias += -1 if fii_short_heavy else 1 if fii_long_heavy else 0
    if cls is not None:
        d.append(f"Client (retail) futures long/short {cls:.2f}"
                 + (" — crowded long (contrarian bearish)" if cls > 2 else ""))
    if out["pcr"] is not None:
        p = out["pcr"]
        d.append(f"Index options PCR (OI) {p:.2f} — "
                 + ("oversold / put-heavy (contrarian bullish)" if p > 1.3 else
                    "call-heavy / complacent (contrarian bearish)" if p < 0.7 else "neutral"))
    out["bias"] = bias
    return out


# =================================================================== catalysts
_MACRO_META = {   # (label, sign: +1 if a rise is good for Nifty, threshold %)
    "crude": ("Brent crude", -1, 4.0),
    "usdinr": ("USD/INR", -1, 0.8),
    "us10y": ("US 10Y yield", -1, 4.0),
    "indiavix": ("India VIX", -1, 15.0),
    "dxy": ("Dollar index", -1, 1.5),
}

# FOMC decision dates (2nd day of meeting). Extend yearly.
_FOMC = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16",
         "2026-10-28", "2026-12-09", "2027-01-27", "2027-03-17"]


def _last_weekday_of_month(y: int, m: int, weekday: int) -> date:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def event_calendar(start: date, days: int = 45) -> list[dict]:
    """Known/seasonal Indian-market catalysts in [start, start+days]."""
    end = start + timedelta(days=days)
    ev: list[dict] = []
    for y in {start.year, end.year}:
        ev.append({"date": date(y, 2, 1), "event": "Union Budget", "category": "Fiscal policy"})
        for m in (2, 4, 6, 8, 10, 12):   # RBI MPC: bi-monthly, outcome usually ~first week
            ev.append({"date": date(y, m, 6), "event": "RBI MPC policy (approx. date)",
                       "category": "Monetary policy (RBI)"})
        for m in (1, 4, 7, 10):
            ev.append({"date": date(y, m, 12), "event": "Quarterly earnings season begins",
                       "category": "Earnings"})
        for m in range(1, 13):
            ev.append({"date": _last_weekday_of_month(y, m, 1), "event": "Nifty monthly F&O expiry",
                       "category": "Derivatives expiry"})
    for s in _FOMC:
        ev.append({"date": date.fromisoformat(s), "event": "US FOMC rate decision",
                   "category": "Monetary policy (US Fed)"})
    return sorted([e for e in ev if start <= e["date"] <= end], key=lambda e: e["date"])


def catalyst_analysis(daily: pd.DataFrame, macro: pd.DataFrame, fii: pd.DataFrame | None = None,
                      top_n: int = 6) -> dict:
    px = daily["close"].copy()
    px.index = px.index.tz_localize(None).normalize() if px.index.tz is not None else px.index.normalize()
    px = px[~px.index.duplicated(keep="last")]
    ret = px.pct_change() * 100
    out: dict = {"macro": [], "attribution": [], "upcoming": event_calendar(date.today())}

    if macro is not None and not macro.empty:
        m = macro.copy()
        m.index = pd.to_datetime(m.index).normalize()
        m = m.ffill()
        mret = m.pct_change() * 100
        joined = mret.join(ret.rename("nifty"), how="inner").tail(120)
        for col in m.columns:
            if col not in _MACRO_META or m[col].dropna().size < 25:
                continue
            label, sign, thr = _MACRO_META[col]
            s = m[col].dropna()
            ch5 = float((s.iloc[-1] / s.iloc[-6] - 1) * 100)
            ch20 = float((s.iloc[-1] / s.iloc[-21] - 1) * 100)
            c = joined[[col, "nifty"]].dropna()
            corr = float(c[col].corr(c["nifty"])) if len(c) > 20 else float("nan")
            impact = "headwind" if ch5 * sign < 0 else "tailwind"
            flag = abs(ch5) >= thr
            out["macro"].append({
                "series": col, "label": label, "last": round(float(s.iloc[-1]), 2),
                "chg_5d": round(ch5, 2), "chg_20d": round(ch20, 2),
                "corr_with_nifty": round(corr, 2) if math.isfinite(corr) else None,
                "impact": impact, "significant": flag,
                "note": f"{label} {ch5:+.1f}% (5d) — {'significant ' if flag else ''}{impact} for Indian equities",
            })

        # ---- why did the biggest moves happen? (last 60 sessions)
        recent = ret.dropna().tail(config.ANALYSIS_WINDOW_DAYS)
        z = (mret - mret.rolling(60).mean()) / mret.rolling(60).std()
        events = event_calendar(recent.index[0].date() - timedelta(days=3), days=90) if len(recent) else []
        for d, r in recent.reindex(recent.abs().sort_values(ascending=False).index).head(top_n).items():
            reasons = []
            for col in z.columns:
                if col not in _MACRO_META:
                    continue
                window = z[col].loc[d - pd.Timedelta(days=2):d].dropna()
                if not window.empty and abs(window.iloc[-1]) >= 1.5:
                    label, sign, _ = _MACRO_META[col]
                    reasons.append(f"{label} shock ({mret[col].loc[window.index[-1]]:+.1f}%)")
            for e in events:
                if abs((pd.Timestamp(e["date"]) - d).days) <= 1:
                    reasons.append(e["event"])
            if fii is not None and not fii.empty and "fii_cash_net" in fii:
                fv = fii["fii_cash_net"].reindex([d]).dropna()
                if not fv.empty and abs(fv.iloc[0]) > 3000:
                    reasons.append(f"Heavy FII {'buying' if fv.iloc[0] > 0 else 'selling'} ₹{fv.iloc[0]:,.0f} cr")
            if not reasons:
                reasons.append("Stock/sector specific or global risk sentiment (no macro shock detected)")
            out["attribution"].append({"date": d.strftime("%Y-%m-%d"), "return_pct": round(float(r), 2),
                                       "type": "Rally" if r > 0 else "Sell-off", "reasons": reasons})
    out["attribution"].sort(key=lambda a: a["date"], reverse=True)
    return out


# =================================================================== projection & signal
def projected_range(daily: pd.DataFrame, supports: list[Zone], resistances: list[Zone],
                    monthly: dict) -> dict:
    c = float(daily["close"].iloc[-1])
    lr = np.log(daily["close"]).diff().dropna().tail(config.ANALYSIS_WINDOW_DAYS)
    sigma_m = float(lr.std() * math.sqrt(config.TRADING_DAYS_PER_MONTH)) if len(lr) > 10 else 0.04
    raw_lo, raw_hi = c * math.exp(-sigma_m), c * math.exp(sigma_m)

    def snap(target, zones, lower):
        near = [z for z in zones if abs(z.mid - target) / c < 0.012]
        if not near:
            return target, "1σ volatility band"
        # Prefer strong zones, discounted by distance from the 1σ target.
        best = max(near, key=lambda z: z.strength / (1 + 100 * abs(z.mid - target) / c))
        return best.mid, f"strongest zone near 1σ ({', '.join(sorted(set(best.sources))[:3])})"

    lo, lo_src = snap(raw_lo, supports, True)
    hi, hi_src = snap(raw_hi, resistances, False)
    return {"lower": round(lo, 2), "upper": round(hi, 2), "lower_basis": lo_src, "upper_basis": hi_src,
            "sigma_month_pct": round(sigma_m * 100, 2),
            "monthly_pivot": round(monthly.get("standard", {}).get("P", float("nan")), 2) if monthly else None}


def _near(price: float, level: float | None, tol: float) -> bool:
    return level is not None and math.isfinite(level) and abs(price - level) <= tol


def generate_signal(df: pd.DataFrame, regime: dict, supports: list[Zone], resistances: list[Zone],
                    vp: dict, monthly: dict, div: dict, inst: dict) -> dict:
    last = df.iloc[-1]
    c, a = float(last["close"]), float(last["atr"])
    tol = a * config.PROXIMITY_ATR_MULT
    r = float(last["rsi"])
    ms = mr = monthly.get("standard", {})

    buy, sell, why_buy, why_sell = 0, 0, [], []

    # --- 1. location
    sup = next((z for z in sorted(supports, key=lambda z: -z.mid)
                if z.low - tol <= c <= z.high + tol), None)
    loc_buy = []
    if sup:
        loc_buy.append(f"support zone {sup.low:.2f}–{sup.high:.2f} (strength {sup.strength:.1f}, {sup.touches} touches)")
    if _near(c, vp.get("val"), tol):
        loc_buy.append(f"Value Area Low {vp['val']:.2f}")
    if _near(c, last.get("ema50"), tol):
        loc_buy.append(f"50 EMA {last['ema50']:.2f}")
    for k in ("S1", "S2"):
        if _near(c, ms.get(k), tol):
            loc_buy.append(f"monthly {k} {ms[k]:.2f}")
    if loc_buy:
        buy += 1
        why_buy.append("Price testing " + ", ".join(loc_buy))

    res = next((z for z in sorted(resistances, key=lambda z: z.mid)
                if z.low - tol <= c <= z.high + tol), None)
    loc_sell = []
    if res:
        loc_sell.append(f"resistance zone {res.low:.2f}–{res.high:.2f} (strength {res.strength:.1f})")
    if _near(c, vp.get("vah"), tol):
        loc_sell.append(f"Value Area High {vp['vah']:.2f}")
    for k in ("R1", "R2"):
        if _near(c, mr.get(k), tol):
            loc_sell.append(f"monthly {k} {mr[k]:.2f}")
    if loc_sell:
        sell += 1
        why_sell.append("Price approaching " + ", ".join(loc_sell))

    # --- 2. momentum
    if div["bullish"]:
        buy += 1
        why_buy.append(div["detail"].split(";")[0])
    elif r <= 40:
        buy += 1
        why_buy.append(f"RSI {r:.0f} — oversold pocket")
    if r >= config.RSI_OVERBOUGHT:
        sell += 1
        why_sell.append(f"RSI {r:.0f} — overbought (>70)")
    elif div["bearish"]:
        sell += 1
        why_sell.append(div["detail"].split("; ")[-1])

    # --- 3. institutions
    if inst.get("available"):
        if (inst.get("dii_cash_5d") or 0) > 0 or (inst.get("fii_cash_5d") or 0) > 0 or inst["bias"] > 0:
            buy += 1
            why_buy.append(f"Positive institutional bias: {inst['thesis']}")
        fls = inst.get("fii_ls")
        if inst.get("fii_short_heavy") or (inst.get("fii_ls_change_5d") or 0) < -0.1:
            sell += 1
            why_sell.append(f"FII heavy shorting in index futures (L/S {fls:.2f})" if fls is not None
                            else "FII long/short ratio falling sharply")

    # --- 4. structure
    if regime["above_ema200"] and regime["trend"] != "Bearish":
        buy += 1
        why_buy.append("Primary trend intact (above 200 EMA) — dips are buyable")
    if pd.notna(last.get("ema20")) and c - last["ema20"] > 2 * a:
        sell += 1
        why_sell.append(f"Stretched {((c - last['ema20']) / a):.1f} ATR above 20 EMA")

    # Buy-on-dips only works with the primary trend: in a bearish regime below the
    # 200 EMA a full buy setup is downgraded to a counter-trend watch.
    bearish_structure = regime["trend"] == "Bearish" and not regime["above_ema200"]
    if buy >= 3 and sell >= 3:
        rec, color = "HOLD — MIXED SIGNALS", "#eab308"
    elif buy >= 3 and loc_buy and bearish_structure:
        rec, color = "WATCH — COUNTER-TREND BASE", "#84cc16"
    elif buy >= 3 and loc_buy:
        rec, color = "ACCUMULATE ZONE", "#22c55e"
    elif sell >= 3 and loc_sell:
        rec, color = "PROFIT BOOKING ZONE", "#ef4444"
    elif buy == 2 and loc_buy:
        rec, color = "WATCH — NEAR ACCUMULATION", "#84cc16"
    elif sell == 2 and (loc_sell or r >= config.RSI_OVERBOUGHT):
        rec, color = "TIGHTEN STOPS / PARTIAL BOOK", "#f97316"
    elif regime["trend"] == "Bearish" and not regime["above_ema200"]:
        rec, color = "AVOID — WAIT FOR BASE", "#a855f7"
    else:
        rec, color = "HOLD", "#eab308"

    # --- trade plan
    below = sorted([z for z in supports if z.mid < c], key=lambda z: -z.mid)
    above = sorted([z for z in resistances if z.mid > c], key=lambda z: z.mid)
    entry_zone = sup or (below[0] if below else None)
    plan = {}
    if entry_zone:
        stop = entry_zone.low - 0.5 * a
        entry = min(c, entry_zone.high)
        # Targets must be at least 1 ATR beyond entry to be worth the trade.
        tgts = [round(z.low, 2) for z in above if z.low > entry + a][:2] or [round(entry + 2 * a, 2)]
        risk = entry - stop
        plan = {"entry_zone": [round(entry_zone.low, 2), round(entry_zone.high, 2)],
                "stop_loss": round(stop, 2), "targets": tgts,
                "risk_reward": round((tgts[0] - entry) / risk, 2) if risk > 0 else None}
    return {"recommendation": rec, "color": color, "buy_score": buy, "sell_score": sell,
            "buy_reasons": why_buy, "sell_reasons": why_sell, "plan": plan,
            "rsi": round(r, 1), "atr": round(a, 2), "close": round(c, 2)}


# =================================================================== orchestration
def analyze(symbol: str, timeframe: str, df: pd.DataFrame, daily: pd.DataFrame,
            fii: pd.DataFrame | None = None, macro: pd.DataFrame | None = None) -> dict:
    """Full pipeline for one symbol/timeframe. `daily` is always the 1D series
    (pivots, projection, flow correlation); `df` is the chart timeframe."""
    if df is None or len(df) < 30 or daily is None or len(daily) < 30:
        raise ValueError(f"Not enough data for {symbol} {timeframe} (need >= 30 bars)")
    daily = add_indicators(daily)
    df = daily if timeframe == "1D" else add_indicators(df)
    last_close = float(df["close"].iloc[-1])
    a = float(df["atr"].iloc[-1])

    cutoff = df.index[-1] - pd.Timedelta(days=config.ANALYSIS_WINDOW_DAYS)
    win = df[df.index >= cutoff]
    swing_win = df[df.index >= df.index[-1] - pd.Timedelta(days=config.ANALYSIS_WINDOW_DAYS * 2)]

    monthly = period_pivots(daily, "M")
    weekly = period_pivots(daily, "W")
    vp = volume_profile(win)

    cands: list[tuple[float, float, str]] = []
    for _, sw in swing_points(swing_win).iterrows():
        recency = 1.0 if sw.ts >= cutoff else 0.6
        cands.append((sw.price, 1.0 * recency, f"swing {sw.kind}"))
    for name, w in (("standard", 1.5), ("fibonacci", 1.0), ("camarilla", 0.8)):
        for k, v in monthly.get(name, {}).items():
            cands.append((v, w, f"M-{name[:3]} {k}"))
        for k, v in weekly.get(name, {}).items():
            cands.append((v, w * 0.5, f"W-{name[:3]} {k}"))
    if vp:
        cands += [(vp["poc"], 2.0, "POC"), (vp["vah"], 1.5, "VAH"), (vp["val"], 1.5, "VAL")]
        cands += [(h, 0.8, "HVN") for h in vp["hvn"]]
    for n in (20, 50, 200):
        v = df[f"ema{n}"].iloc[-1]
        if pd.notna(v):
            cands.append((float(v), {20: 0.7, 50: 1.2, 200: 1.5}[n], f"EMA{n}"))
    # Keep only levels within a sensible band (±12%) of current price.
    cands = [c for c in cands if abs(c[0] / last_close - 1) < 0.12]

    zones = build_zones(win, cands, a)
    zones = [z for z in zones if z.strength >= 2.0]
    supports = sorted([z for z in zones if z.mid <= last_close], key=lambda z: -z.mid)
    resistances = sorted([z for z in zones if z.mid > last_close], key=lambda z: z.mid)

    regime = market_regime(df)
    div = rsi_divergence(df)
    inst = institutional_analysis(daily, fii)
    cat = catalyst_analysis(daily, macro, fii)
    proj = projected_range(daily, supports, resistances, monthly)
    signal = generate_signal(df, regime, supports, resistances, vp, monthly, div, inst)

    drivers = []
    drivers.append(f"Regime {regime['trend']} (ADX {regime['adx']}, 20 EMA slope {regime['ema20_slope_pct']:+.2f}%/5 bars, "
                   f"{'above' if regime['above_ema200'] else 'below'} 200 EMA)")
    if vp:
        pos = "above value (strength)" if last_close > vp["vah"] else \
              "below value (weakness)" if last_close < vp["val"] else "inside value area (balance)"
        drivers.append(f"Volume profile POC {vp['poc']:.2f}, VA {vp['val']:.2f}–{vp['vah']:.2f}: price {pos}")
    drivers.append(f"Institutions: {inst['thesis']}")
    drivers += signal["buy_reasons"] + signal["sell_reasons"]
    drivers += [m["note"] for m in cat["macro"] if m["significant"]]

    return {
        "symbol": symbol, "timeframe": timeframe, "as_of": df.index[-1].isoformat(),
        "close": last_close, "atr": a, "frame": df,
        "monthly_pivots": monthly, "weekly_pivots": weekly, "volume_profile": vp,
        "support_zones": [z.to_dict() for z in supports],
        "resistance_zones": [z.to_dict() for z in resistances],
        "regime": regime, "divergence": div, "institutional": inst, "catalysts": cat,
        "projection": proj, "signal": signal, "drivers": drivers,
    }


def to_sr_document(result: dict) -> dict:
    """Shape an analysis result for the `sr_levels` collection."""
    vp = result["volume_profile"] or {}
    return {
        "symbol": result["symbol"], "timeframe": result["timeframe"],
        "calculated_date": pd.Timestamp(result["as_of"]),
        "support_zones": result["support_zones"], "resistance_zones": result["resistance_zones"],
        "poc_level": vp.get("poc"), "vah": vp.get("vah"), "val": vp.get("val"),
        "monthly_pivots": result["monthly_pivots"], "weekly_pivots": result["weekly_pivots"],
        "projection": result["projection"], "regime": result["regime"],
        "signal": {k: v for k, v in result["signal"].items() if k != "color"},
    }


if __name__ == "__main__":
    import json
    import logging
    import sys

    from data_fetcher import DataService

    logging.basicConfig(level=logging.WARNING)
    sym = sys.argv[1] if len(sys.argv) > 1 else config.DEFAULT_SYMBOL
    tf = sys.argv[2] if len(sys.argv) > 2 else "1D"
    svc = DataService()
    daily = svc.load_ohlcv(sym, "1D")
    frame = daily if tf == "1D" else svc.load_ohlcv(sym, tf)
    res = analyze(sym, tf, frame, daily, svc.load_fii_dii(), svc.load_macro())
    svc.save_sr_levels(to_sr_document(res))
    res.pop("frame")
    res["volume_profile"] = {k: v for k, v in res["volume_profile"].items() if k not in ("prices", "volumes")}
    print(json.dumps(res, indent=2, default=str))
