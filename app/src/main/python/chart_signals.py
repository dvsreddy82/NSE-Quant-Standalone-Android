"""Chart signals ported from two TradingView indicators (Pine v6) — "Daily / Weekly / Monthly Pivot +
Daily CPR" and "My Stuff" (multi-timeframe MAs, untouched CPRs, unfilled gaps, Savitzky flow bands) —
computed on the app's own candles for NIFTY and BANKNIFTY and turned into alerts.

Levels
  * Daily pivot, CPR (pivot / BC / TC), R1-R3, S1-S3 and previous-day close from the previous session;
    weekly and monthly pivots from the previous week (Mon-Sun) and calendar month.
  * Today's opening gap (open vs previous close) and whether it has filled.
  * Untouched ("virgin") CPRs and unfilled daily gaps from earlier sessions.  A gap is the space
    between the previous session's last close and a session that never traded back into it.
    The Pine script's partial-fill bookkeeping keeps the part price already traded through; here the
    remaining zone is the part still untraded (gap up: from the old close up to the lowest low since).
  * First-hour range (9:15-10:20) and the 9:30-10:00 range with a 5-point buffer (5 points on NIFTY,
    scaled by price for BANKNIFTY).
  * Hourly CPR from the previous 60-minute bar (status only).

Trend
  * EMA 34 x 50 of hl2 on 5m / 15m / 30m / 1h / D: BULLISH, BEARISH or NEAR CROSS (within 0.10%).
  * EMA 10 x 20 of close on 15m / 45m / 90m / 180m / D ("EMA-TF x 3").
  * SMA 9 / 20 on 5-minute bars (status only).
  * Savitzky flow bands on 15-minute bars (the script's own filter and ratcheting bands); a close
    beyond a band flips the trend.

Intraday bars are anchored at 9:15 like TradingView's NSE bars.  Signals use completed bars only,
so a forming candle can't raise and then retract an alert.  Every check is stateless (it compares
the last completed bar with the one before) and alert ids are stable per day, so restarts don't
repeat notifications.  These are mechanical chart readings, not a forecast or advice.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd

import config

OPEN_T, CLOSE_T = dtime(9, 15), dtime(15, 30)
NEAR_CROSS_PCT = 0.10
BREAK_BUFFER = 0.0005          # a 5-minute close must be 0.05% through a level (as the level-break alerts)
GAP_ALERT_PCT = 0.20          # opening gaps smaller than this aren't worth a notification
CPR_NARROW_PCT, CPR_WIDE_PCT = 0.10, 0.35
GAP_MIN_PCT, GAP_LEFT_PCT = 0.10, 0.03   # daily gaps: minimum size when formed / still unfilled
ZONE_DAYS = 60                # how far back untouched CPRs and unfilled gaps are tracked
NEAR_PCT = 3.0                # report historical zones within this distance of spot
MA_TFS = [("5m", 5), ("15m", 15), ("30m", 30), ("1h", 60), ("D", 0)]
EMA_TFS = [("15m", 15), ("45m", 45), ("90m", 90), ("180m", 180), ("D", 0)]
ALERT_MA_TFS = {"15m", "30m", "1h", "D"}     # 5-minute crosses are too frequent to notify
ALERT_EMA_TFS = {"45m", "90m", "180m", "D"}
FRESH_MIN = 20                # a bar-based signal is only raised while its bar is this recent


def _hid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _f(x) -> str:
    return f"{x:,.1f}"


# ---------------------------------------------------------------------------------- bars
def _session(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df = df[["open", "high", "low", "close"]].astype(float)
    t = df.index.time
    return df[(t >= OPEN_T) & (t < CLOSE_T)]


def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Bars of `minutes` anchored at 9:15 each session; index = bar start, column 'end' = bar end."""
    if df.empty:
        return df.assign(end=pd.Series(dtype="datetime64[ns, Asia/Kolkata]"))
    day = df.index.normalize()
    start = day + pd.Timedelta(hours=9, minutes=15)
    bucket = ((df.index - start) // pd.Timedelta(minutes=minutes)).astype(int)
    key = start + pd.to_timedelta(bucket * minutes, unit="m")
    out = df.groupby(key).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                              close=("close", "last"))
    close_at = out.index.normalize() + pd.Timedelta(hours=15, minutes=30)
    out["end"] = np.minimum(out.index + pd.Timedelta(minutes=minutes), close_at)
    return out


def completed(bars: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    return bars[bars["end"] <= now] if len(bars) else bars


def build_daily(daily: pd.DataFrame, m1: pd.DataFrame, today) -> pd.DataFrame:
    """Daily OHLC (+ last 1-minute close/low) with sessions covered by 1-minute candles taken from them."""
    d = daily[["open", "high", "low", "close"]].astype(float).copy() if daily is not None and len(daily) else \
        pd.DataFrame(columns=["open", "high", "low", "close"])
    d.index = pd.DatetimeIndex([pd.Timestamp(ts).date() for ts in d.index])
    d["last_close"], d["last_low"] = d["close"], d["close"]
    if len(m1):
        g = m1.groupby(m1.index.date).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                          close=("close", "last"), last_low=("low", "last"), n=("close", "size"))
        for day, r in g.iterrows():
            if r["n"] >= 60 or day == today:
                d.loc[pd.Timestamp(day), ["open", "high", "low", "close", "last_close", "last_low"]] = \
                    [r["open"], r["high"], r["low"], r["close"], r["close"], r["last_low"]]
    d = d[~d.index.duplicated(keep="last")].sort_index()
    return d


def merge15(m15: pd.DataFrame, m1: pd.DataFrame) -> pd.DataFrame:
    """Stored 15-minute candles for older sessions plus 15-minute bars built from 1-minute candles."""
    a = _session(m15)
    b = resample(m1, 15).drop(columns="end") if len(m1) else a.iloc[0:0]
    if len(b):
        days = set(b.index.date)
        a = a[[d not in days for d in a.index.date]]
    return pd.concat([a, b]).sort_index()


# ---------------------------------------------------------------------------------- levels
def pivots(h: float, l: float, c: float) -> dict:
    p = (h + l + c) / 3
    bc = (h + l) / 2
    tc = 2 * p - bc
    rng = h - l
    return {"P": p, "TC": max(tc, bc), "BC": min(tc, bc),
            "R1": 2 * p - l, "R2": p + rng, "R3": h + 2 * (p - l),
            "S1": 2 * p - h, "S2": p - rng, "S3": l - 2 * (h - p)}


def period_pivot(d: pd.DataFrame, rule: str, today) -> float | None:
    past = d[d.index < pd.Timestamp(today)]
    if past.empty:
        return None
    g = past.resample(rule).agg({"high": "max", "low": "min", "close": "last"}).dropna()
    cur = pd.Timestamp(today).to_period("W-SUN" if rule == "W-SUN" else "M")
    g = g[[pd.Timestamp(ix).to_period("W-SUN" if rule == "W-SUN" else "M") < cur for ix in g.index]]
    if g.empty:
        return None
    r = g.iloc[-1]
    return (r["high"] + r["low"] + r["close"]) / 3


def untouched_cprs(d: pd.DataFrame) -> list[dict]:
    """CPRs of earlier sessions that no later session (including its own) has traded into."""
    out = []
    rows = d.tail(ZONE_DAYS + 1)
    idx = list(rows.index)
    for i in range(1, len(idx) - 1):                 # today's CPR is tracked separately
        pv = pivots(*rows.iloc[i - 1][["high", "low", "close"]])
        later = rows.iloc[i:-1]
        if ((later["high"] >= pv["BC"]) & (later["low"] <= pv["TC"])).any():
            continue
        out.append({"day": idx[i].date(), "top": pv["TC"], "bot": pv["BC"]})
    return out


def unfilled_gaps(d: pd.DataFrame) -> list[dict]:
    """Daily gaps (a session entirely beyond the previous session's last close) with the part still
    untraded up to yesterday.  Gap up: [prev last close, session low]; gap down: [session high, prev last low]."""
    out = []
    rows = d.tail(ZONE_DAYS + 1)
    for i in range(1, len(rows) - 1):
        prev, cur = rows.iloc[i - 1], rows.iloc[i]
        later = rows.iloc[i + 1:-1]
        if cur["low"] > prev["last_close"] * (1 + GAP_MIN_PCT / 100):
            bot, top = prev["last_close"], cur["low"]
            if len(later):
                top = min(top, later["low"].min())
            if top - bot > bot * GAP_LEFT_PCT / 100:
                out.append({"day": rows.index[i].date(), "type": "up", "bot": bot, "top": top,
                            "orig_top": cur["low"]})
        elif cur["high"] < prev["last_low"] * (1 - GAP_MIN_PCT / 100):
            bot, top = cur["high"], prev["last_low"]
            if len(later):
                bot = max(bot, later["high"].max())
            if top - bot > bot * GAP_LEFT_PCT / 100:
                out.append({"day": rows.index[i].date(), "type": "down", "bot": bot, "top": top,
                            "orig_bot": cur["high"]})
    return out


# ---------------------------------------------------------------------------------- trend
def ma_status(short: float, long: float) -> str:
    if short != short or long != long:
        return "N/A"
    if abs(short - long) / max(abs(long), 1e-9) * 100 <= NEAR_CROSS_PCT:
        return "NEAR CROSS"
    return "BEARISH" if long > short else "BULLISH"


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


SG_WEIGHTS = np.array([-4, -3, -2, -1, 0, 1, 2, 3] + [-4] * 8, dtype=float)   # the script's coefficients


def savitzky(b15: pd.DataFrame, length: int = 15) -> pd.DataFrame:
    """Savitzky flow bands as the Pine script computes them: its 16-tap filter (newest bar first,
    normalised by the coefficient sum), averaged over length-1 bars; bands 2 x SMA(range, 100) wide that
    only ratchet towards price; a close beyond a band sets the direction."""
    c = b15["close"].to_numpy()
    n = len(c)
    raw = np.full(n, np.nan)
    norm = SG_WEIGHTS.sum()
    for i in range(15, n):
        raw[i] = (SG_WEIGHTS * c[i - np.arange(16)]).sum() / norm
    basis = pd.Series(raw, index=b15.index).rolling(length - 1).mean()
    dist = (b15["high"] - b15["low"]).rolling(100).mean() * 2
    hb, lb = (basis + dist).to_numpy(copy=True), (basis - dist).to_numpy(copy=True)
    direction = np.full(n, np.nan)
    d = np.nan
    for i in range(n):
        if i > 0:
            pl, ph = (lb[i - 1] if lb[i - 1] == lb[i - 1] else 0.0), (hb[i - 1] if hb[i - 1] == hb[i - 1] else 0.0)
            if lb[i] == lb[i] and not (lb[i] > pl or c[i - 1] < pl):
                lb[i] = pl
            if hb[i] == hb[i] and not (hb[i] < ph or c[i - 1] > ph):
                hb[i] = ph
        if hb[i] == hb[i] and c[i] > hb[i]:
            d = 1
        elif lb[i] == lb[i] and c[i] < lb[i]:
            d = 0
        direction[i] = d
    return pd.DataFrame({"close": c, "basis": basis.to_numpy(), "upper": hb, "lower": lb, "dir": direction},
                        index=b15.index)


# ---------------------------------------------------------------------------------- analysis
def analyse(und: str, m1: pd.DataFrame, m15: pd.DataFrame, daily: pd.DataFrame,
            now: datetime | None = None) -> dict:
    """→ {"state": {...}, "alerts": [...]} for one index.  m1 / m15 / daily: stored candles."""
    now = pd.Timestamp(now or datetime.now(config.IST))
    now = now.tz_localize(config.IST) if now.tzinfo is None else now.tz_convert(config.IST)
    today = now.date()
    m1 = _session(m1)
    d = build_daily(daily, m1, today)
    past = d[d.index < pd.Timestamp(today)]
    if len(past) < 2:
        return {"state": {"error": "not enough daily candles"}, "alerts": []}
    prev = past.iloc[-1]
    lv = pivots(prev["high"], prev["low"], prev["close"])
    lv["PDC"] = prev["close"]
    wp, mp = period_pivot(d, "W-SUN", today), period_pivot(d, "MS", today)
    today_m1 = m1[m1.index.date == today]
    b5_all = resample(m1, 5)
    b5 = completed(b5_all, now)
    t5 = b5[b5.index.date == today]
    spot = float(today_m1["close"].iloc[-1]) if len(today_m1) else float(prev["close"])
    scale = spot / 25000
    cpr_w = (lv["TC"] - lv["BC"]) / spot * 100
    st: dict = {"und": und, "spot": spot, "as_of": (today_m1.index[-1] if len(today_m1) else None),
                "prev_day": past.index[-1].date(), "levels": {k: round(v, 2) for k, v in lv.items()},
                "weekly_pivot": wp, "monthly_pivot": mp, "cpr_width_pct": round(cpr_w, 3),
                "cpr_type": "narrow (trend-day setup)" if cpr_w < CPR_NARROW_PCT else
                            "wide (range-day setup)" if cpr_w > CPR_WIDE_PCT else "normal"}
    alerts: list[dict] = []

    def alert(key, level, title, text, **extra):
        alerts.append({"id": "chart:" + _hid(und, today, *key), "kind": "LEVEL", "src": "chart", "level": level,
                       "und": und, "title": f"{und} {title}", "text": text, "signal": key[0],
                       "at": now.to_pydatetime(), **extra})

    fresh = len(t5) and (now - t5["end"].iloc[-1]) <= pd.Timedelta(minutes=FRESH_MIN)

    # ---- opening gap and CPR position at the open
    if len(today_m1):
        o = float(today_m1["open"].iloc[0])
        gap = o - prev["close"]
        gap_pct = gap / prev["close"] * 100
        pos = "above" if o > lv["TC"] else "below" if o < lv["BC"] else "inside"
        hi, lo = float(today_m1["high"].max()), float(today_m1["low"].min())
        filled = (lo <= prev["close"]) if gap > 0 else (hi >= prev["close"]) if gap < 0 else True
        st.update(open=o, gap=gap, gap_pct=gap_pct, gap_filled=filled, open_vs_cpr=pos,
                  cpr_touched=bool(hi >= lv["BC"] and lo <= lv["TC"]))
        st["open_note"] = (f"Opened {_f(o)} vs previous close {_f(prev['close'])} ({gap:+,.0f} pts), {pos} the CPR "
                           f"{_f(lv['BC'])}–{_f(lv['TC'])} ({st['cpr_type']}).")
        if abs(gap_pct) >= GAP_ALERT_PCT:          # the open itself is announced by the open-play alert
            if filled and len(t5):
                hit = t5[(t5["low"] <= prev["close"]) if gap > 0 else (t5["high"] >= prev["close"])]
                if len(hit) and hit.index[0] == t5.index[-1] and fresh:     # raised on the bar that fills it
                    alert(("gapfill",), "MEDIUM", f"opening gap filled at {_f(prev['close'])}",
                          f"Price traded back to the previous close {_f(prev['close'])} "
                          f"({hit.index[0]:%H:%M} bar); the {'gap-up' if gap > 0 else 'gap-down'} has closed.")

    # ---- 5-minute closes through today's levels
    names = {"P": "daily pivot", "TC": "CPR top", "BC": "CPR bottom", "R1": "R1", "R2": "R2", "R3": "R3",
             "S1": "S1", "S2": "S2", "S3": "S3", "PDC": "previous close"}
    marks = [(names[k], v, "HIGH" if k in ("TC", "BC", "R2", "S2", "R3", "S3") else "MEDIUM") for k, v in lv.items()]
    if wp:
        marks.append(("weekly pivot", wp, "HIGH"))
    if mp:
        marks.append(("monthly pivot", mp, "HIGH"))
    if fresh and len(t5) >= 2:                   # both closes from today: the open itself is the gap alert
        p0, p1 = float(t5["close"].iloc[-2]), float(t5["close"].iloc[-1])
        for name, L, sev in marks:
            if name in ("CPR top", "CPR bottom"):
                continue
            buf = L * BREAK_BUFFER
            if (p0 <= L and p1 > L + buf) or (p0 >= L and p1 < L - buf):
                d_ = "up" if p1 > L else "down"
                alert(("cross", name, round(L), d_), sev,
                      f"5-min close {'above' if d_ == 'up' else 'below'} {name} {L:,.0f}",
                      f"Close {_f(p1)} (prev {_f(p0)}). " + _next_levels(marks, p1))
        if p0 <= lv["TC"] and p1 > lv["TC"] * (1 + BREAK_BUFFER):
            alert(("cpr", "above"), "HIGH", f"broke above the CPR ({_f(lv['BC'])}–{_f(lv['TC'])})",
                  f"5-min close {_f(p1)} above CPR top {_f(lv['TC'])}: bullish while it holds above "
                  f"{_f(lv['TC'])} ({st['cpr_type']} CPR). " + _next_levels(marks, p1))
        if p0 >= lv["BC"] and p1 < lv["BC"] * (1 - BREAK_BUFFER):
            alert(("cpr", "below"), "HIGH", f"broke below the CPR ({_f(lv['BC'])}–{_f(lv['TC'])})",
                  f"5-min close {_f(p1)} below CPR bottom {_f(lv['BC'])}: bearish while it stays under "
                  f"{_f(lv['BC'])} ({st['cpr_type']} CPR). " + _next_levels(marks, p1))
        # first touch of today's (virgin) CPR
        touch = (t5["high"] >= lv["BC"]) & (t5["low"] <= lv["TC"])
        if st.get("open_vs_cpr") != "inside" and touch.any() and touch.iloc[-1] and not touch.iloc[:-1].any():
            alert(("cprtouch",), "MEDIUM", f"first touch of today's CPR {_f(lv['BC'])}–{_f(lv['TC'])}",
                  f"Price reached the CPR for the first time today ({t5.index[-1]:%H:%M} bar, close {_f(p1)}).")

    # ---- open play: gap type → direction → whole number → PDH / PDL
    _open_play(und, t5, prev, now, bool(fresh), alert, st)

    # ---- first-hour range (9:15-10:20) and 9:30-10:00 range
    fh = today_m1[(today_m1.index.time >= dtime(9, 15)) & (today_m1.index.time < dtime(10, 20))]
    if len(fh) and now.time() >= dtime(10, 20):
        fh_h, fh_l = float(fh["high"].max()), float(fh["low"].min())
        st["first_hour"] = {"high": fh_h, "low": fh_l}
        after = t5[t5.index.time >= dtime(10, 20)]
        for side, lvl in (("high", fh_h), ("low", fh_l)):
            brk = after["close"] > lvl if side == "high" else after["close"] < lvl
            if fresh and len(after) and brk.iloc[-1] and not brk.iloc[:-1].any():
                alert(("fh", side), "HIGH", f"first-hour {side} {'breakout' if side == 'high' else 'breakdown'} "
                                            f"{lvl:,.0f}",
                      f"5-min close {_f(after['close'].iloc[-1])} {'above' if side == 'high' else 'below'} the "
                      f"9:15–10:20 range {_f(fh_l)}–{_f(fh_h)}.")
    r30 = today_m1[(today_m1.index.time >= dtime(9, 30)) & (today_m1.index.time < dtime(10, 0))]
    if len(r30) and now.time() >= dtime(10, 0):
        buf = 5 * scale
        hi30, lo30 = float(r30["high"].max()) + buf, float(r30["low"].min()) - buf
        st["range_930"] = {"high": hi30, "low": lo30}
        after = t5[t5.index.time >= dtime(10, 0)]
        for side, lvl in (("high", hi30), ("low", lo30)):
            brk = after["close"] > lvl if side == "high" else after["close"] < lvl
            if fresh and len(after) and brk.iloc[-1] and not brk.iloc[:-1].any():
                alert(("r930", side), "MEDIUM", f"broke the 9:30–10:00 {side} {lvl:,.0f}",
                      f"5-min close {_f(after['close'].iloc[-1])} {'above' if side == 'high' else 'below'} "
                      f"the 9:30–10:00 range ({_f(lo30)}–{_f(hi30)}, incl. {buf:.0f}-pt buffer).")

    # ---- historical untouched CPRs and unfilled gaps
    cprs, gaps = untouched_cprs(d), unfilled_gaps(d)
    live_c, live_g = cprs, gaps
    if len(today_m1):                              # what today has already traded through
        hi, lo = float(today_m1["high"].max()), float(today_m1["low"].min())
        live_c = [c for c in cprs if not (hi >= c["bot"] and lo <= c["top"])]
        live_g = []
        for g in gaps:
            g = dict(g)
            if g["type"] == "up":
                g["top"] = min(g["top"], lo)
            else:
                g["bot"] = max(g["bot"], hi)
            if g["top"] - g["bot"] > g["bot"] * GAP_LEFT_PCT / 100:
                live_g.append(g)
    st["untouched_cprs"] = [c for c in live_c if abs((c["top"] + c["bot"]) / 2 - spot) / spot * 100 <= NEAR_PCT]
    st["unfilled_gaps"] = [g for g in live_g if min(abs(g["top"] - spot), abs(g["bot"] - spot)) / spot * 100 <= NEAR_PCT]
    if fresh:
        bar = t5.iloc[-1]
        before = t5.iloc[:-1]
        for c in cprs:
            hit = bar["high"] >= c["bot"] and bar["low"] <= c["top"]
            hit_before = len(before) and ((before["high"] >= c["bot"]) & (before["low"] <= c["top"])).any()
            if hit and not hit_before:
                alert(("vcpr", c["day"]), "HIGH", f"reached the untouched CPR of {c['day']:%d %b} "
                                                  f"({_f(c['bot'])}–{_f(c['top'])})",
                      f"First touch of that session's CPR (5-min close {_f(bar['close'])}). Untouched CPRs often "
                      f"act as support / resistance on the first visit.")
        for g in gaps:
            enter = bar["high"] >= g["bot"] and bar["low"] <= g["top"]
            entered_before = len(before) and ((before["high"] >= g["bot"]) & (before["low"] <= g["top"])).any()
            full = bar["low"] <= g["bot"] if g["type"] == "up" else bar["high"] >= g["top"]
            if enter and not entered_before and not full:
                alert(("gapin", g["day"]), "MEDIUM", f"entered the unfilled {g['type']}-gap of {g['day']:%d %b} "
                                                     f"({_f(g['bot'])}–{_f(g['top'])})",
                      f"5-min bar {bar.name:%H:%M} traded into it (close {_f(bar['close'])}); fill level "
                      f"{_f(g['bot'] if g['type'] == 'up' else g['top'])}.")
            if full:
                alert(("gapfull", g["day"]), "MEDIUM", f"filled the {g['type']}-gap of {g['day']:%d %b}",
                      f"Price reached {_f(g['bot'] if g['type'] == 'up' else g['top'])}; that gap is closed.")

    # ---- hourly CPR (status)
    b15 = merge15(m15, m1)
    b60 = completed(resample(b15, 60), now)
    if len(b60):
        h = b60.iloc[-1]
        hp = pivots(h["high"], h["low"], h["close"])
        st["hourly_cpr"] = {"bar": b60.index[-1], "P": hp["P"], "TC": hp["TC"], "BC": hp["BC"],
                            "pos": "above" if spot > hp["TC"] else "below" if spot < hp["BC"] else "inside"}

    # ---- moving averages
    hl2 = lambda b: (b["high"] + b["low"]) / 2
    frames = {}

    def frame(tf, mins):
        if tf not in frames:
            if mins == 0:
                frames[tf] = d.assign(end=[pd.Timestamp(ix).tz_localize(config.IST) + pd.Timedelta(hours=15, minutes=30)
                                           for ix in d.index])
            elif mins == 5:
                frames[tf] = b5_all
            else:
                frames[tf] = resample(b15, mins)
            frames[tf] = completed(frames[tf], now)
        return frames[tf]

    ma_rows, ema_rows = [], []
    for tf, mins in MA_TFS:
        b = frame(tf, mins)
        if len(b) < 52:
            ma_rows.append({"tf": tf, "status": "N/A"})
            continue
        s, l_ = _ema(hl2(b), 34), _ema(hl2(b), 50)
        cur, old = ma_status(s.iloc[-1], l_.iloc[-1]), ma_status(s.iloc[-2], l_.iloc[-2])
        ma_rows.append({"tf": tf, "status": cur, "short": float(s.iloc[-1]), "long": float(l_.iloc[-1]),
                        "gap_pct": float((s.iloc[-1] - l_.iloc[-1]) / l_.iloc[-1] * 100), "cross": _last_cross(s, l_),
                        "bar": b.index[-1],
                        "rising": bool(l_.iloc[-1] >= l_.iloc[-2])})
        recent = mins == 0 or (now - b["end"].iloc[-1]) <= pd.Timedelta(minutes=max(FRESH_MIN, mins))
        if tf in ALERT_MA_TFS and recent and cur != old and cur in ("BULLISH", "BEARISH") and old != "N/A":
            alert(("ma", tf, cur, b.index[-1]), "HIGH" if tf in ("1h", "D") else "MEDIUM",
                  f"{tf} EMA 34×50 turned {cur}",
                  f"EMA34 {_f(s.iloc[-1])} {'above' if cur == 'BULLISH' else 'below'} EMA50 {_f(l_.iloc[-1])} "
                  f"(hl2, {tf} bars, was {old}).")
    for tf, mins in EMA_TFS:
        b = frame(tf, mins)
        if len(b) < 22:
            ema_rows.append({"tf": tf, "status": "N/A"})
            continue
        f_, s_ = _ema(b["close"], 10), _ema(b["close"], 20)
        cur = "BULLISH" if f_.iloc[-1] > s_.iloc[-1] else "BEARISH"
        old = "BULLISH" if f_.iloc[-2] > s_.iloc[-2] else "BEARISH"
        ema_rows.append({"tf": tf, "status": cur, "fast": float(f_.iloc[-1]), "slow": float(s_.iloc[-1]),
                         "gap_pct": float((f_.iloc[-1] - s_.iloc[-1]) / s_.iloc[-1] * 100),
                         "cross": _last_cross(f_, s_), "bar": b.index[-1]})
        recent = mins == 0 or (now - b["end"].iloc[-1]) <= pd.Timedelta(minutes=max(FRESH_MIN, mins))
        if tf in ALERT_EMA_TFS and recent and cur != old:
            alert(("ema", tf, cur, b.index[-1]), "HIGH" if tf in ("180m", "D") else "MEDIUM",
                  f"{tf} EMA 10×20 turned {cur}", f"EMA10 {_f(f_.iloc[-1])} vs EMA20 {_f(s_.iloc[-1])} on {tf} bars.")
    st["ma"], st["ema"] = ma_rows, ema_rows
    sts = [r["status"] for r in ma_rows]
    if all(s == "BULLISH" for s in sts) or all(s == "BEARISH" for s in sts):
        st["aligned"] = sts[0]
        b15c = completed(resample(b15, 15), now)
        if len(b15c) and (now - b15c["end"].iloc[-1]) <= pd.Timedelta(minutes=FRESH_MIN):
            alert(("aligned", sts[0]), "HIGH", f"all timeframes {sts[0]} (EMA 34×50, 5m→D)",
                  "5m, 15m, 30m, 1h and daily EMA34/EMA50 all agree — a trend-aligned session.")
    if len(b5) >= 20:
        s9, s20 = b5["close"].rolling(9).mean().iloc[-1], b5["close"].rolling(20).mean().iloc[-1]
        st["sma_9_20_5m"] = {"sma9": float(s9), "sma20": float(s20), "status": "BULLISH" if s9 > s20 else "BEARISH"}

    # ---- Savitzky flow bands (15-minute)
    b15c = completed(resample(b15, 15), now).drop(columns="end")
    if len(b15c) > 130:
        sg = savitzky(b15c)
        last = sg.iloc[-1]
        if last["dir"] == last["dir"]:
            up = last["dir"] == 1
            st["savitzky"] = {"trend": "UP" if up else "DOWN", "band": float(last["lower"] if up else last["upper"]),
                              "basis": float(last["basis"])}
            if len(sg) >= 2 and sg["dir"].iloc[-2] == sg["dir"].iloc[-2] and sg["dir"].iloc[-2] != last["dir"] \
                    and (now - (sg.index[-1] + pd.Timedelta(minutes=15))) <= pd.Timedelta(minutes=FRESH_MIN):
                alert(("sg", "up" if up else "down", sg.index[-1]), "HIGH",
                      f"Savitzky flow turned {'UP' if up else 'DOWN'} (15m)",
                      f"15-min close {_f(last['close'])} {'above the upper' if up else 'below the lower'} band; "
                      f"trailing band now {_f(st['savitzky']['band'])}.")
    return {"state": st, "alerts": alerts}


def _open_play(und: str, t5: pd.DataFrame, prev: pd.Series, now: pd.Timestamp, fresh: bool, alert, st: dict):
    """The open-play tree: gap up / flat / gap down → going up / down → the next whole number in that
    direction → PDH and PDL (near, breaking up, breaking down).  Direction is the last 15 minutes'
    move (≥ DIRECTION_PCT flips it; smaller moves keep the previous direction)."""
    if t5.empty:
        return
    o, pc, pdh, pdl = float(t5["open"].iloc[0]), float(prev["close"]), float(prev["high"]), float(prev["low"])
    gap_pct = (o - pc) / pc * 100
    kind = ("Gap-up" if gap_pct >= config.OPEN_GAP_PCT else "Gap-down" if gap_pct <= -config.OPEN_GAP_PCT
            else "Flat (sideways)")
    closes = t5["close"].to_numpy()
    dirs, d, pending = [], None, None
    for i, c in enumerate(closes):                 # a new direction must show on two bars in a row
        ref = closes[i - 3] if i >= 3 else o
        ch = (c - ref) / ref * 100
        cand = "up" if ch >= config.DIRECTION_PCT else "down" if ch <= -config.DIRECTION_PCT else None
        if d is None:
            d = cand or ("up" if c >= o else "down")
        elif cand and cand != d:
            if pending == cand:
                d, pending = cand, None
            else:
                pending = cand
        else:
            pending = None
        dirs.append(d)
    price = float(closes[-1])
    step = config.WHOLE_STEP_BANKNIFTY if und == "BANKNIFTY" else config.WHOLE_STEP_NIFTY
    major = step * (2 if und == "BANKNIFTY" else 5)
    up_w = (np.floor(price / step) + 1) * step
    dn_w = (np.ceil(price / step) - 1) * step
    target = up_w if d == "up" else dn_w
    near = config.NEAR_LEVEL_PCT

    def pd_state(name, L):
        dist = (L - price) / price * 100
        where = "above" if L > price else "below"
        if abs(dist) <= near:
            return f"{name} {L:,.0f} (near, price {'above' if price > L else 'below'})"
        return f"{name} {L:,.0f} ({abs(dist):.2f}% {where})"

    path = (f"{kind} open ({gap_pct:+.2f}%) → going {d} → next whole number {target:,.0f} "
            f"({target - price:+,.0f} pts) · {pd_state('PDH', pdh)} · {pd_state('PDL', pdl)}")
    flips = [i for i in range(1, len(dirs)) if dirs[i] != dirs[i - 1]]
    st["open_play"] = {"kind": kind, "gap_pct": round(gap_pct, 2), "direction": d, "whole_up": up_w,
                       "whole_down": dn_w, "target": target, "pdh": pdh, "pdl": pdl, "path": path,
                       "flips": len(flips)}
    if not fresh:
        return
    bar = t5.index[-1]
    if now.time() <= dtime(9, 45) and len(t5) <= 6:
        alert(("op", "open"), "MEDIUM", f"{kind} open, going {d}", (st.get("open_note", "") + " " + path).strip())
    if flips and flips[-1] == len(dirs) - 1 and len(flips) <= config.MAX_DIRECTION_ALERTS:
        alert(("op", "dir", bar), "MEDIUM", f"now going {d} ({kind.lower()} day)",
              f"15-min move turned {'up' if d == 'up' else 'down'} at {price:,.1f}. " + path)
    if len(closes) < 2:
        return
    p0, p1 = float(closes[-2]), float(price)
    buf = BREAK_BUFFER
    lo_, hi_ = min(p0, p1), max(p0, p1)
    for W in np.arange(np.ceil(lo_ / step) * step, hi_ + step / 2, step):
        if W <= lo_ or W >= hi_:
            continue
        up = p1 > W * (1 + buf) and p0 <= W
        dn = p1 < W * (1 - buf) and p0 >= W
        if up or dn:
            nxt = W + step if up else W - step
            alert(("op", "wn", round(W), "up" if up else "down"), "HIGH" if W % major == 0 else "MEDIUM",
                  f"5-min close {'above' if up else 'below'} whole number {W:,.0f}",
                  f"Close {p1:,.1f} (prev {p0:,.1f}); next whole number {nxt:,.0f}. " + path)
    dist_t = abs(target - p1) / p1 * 100
    if target % major == 0 and dist_t <= near and abs(target - p0) / p0 * 100 > near:   # majors only: crosses cover the rest
        alert(("op", "wnnear", round(target), d), "MEDIUM", f"near whole number {target:,.0f} (going {d})",
              f"{abs(target - p1):,.0f} pts away at {p1:,.1f}. " + path)
    for name, L in (("PDH", pdh), ("PDL", pdl)):
        if p0 <= L and p1 > L * (1 + buf):
            alert(("op", "brk", name, "up"), "HIGH", f"breaking above {name} {L:,.0f} ({kind.lower()} day)",
                  f"5-min close {p1:,.1f} above {'the previous-day high' if name == 'PDH' else 'the previous-day low'}"
                  f" {L:,.0f}. " + path)
        elif p0 >= L and p1 < L * (1 - buf):
            alert(("op", "brk", name, "down"), "HIGH", f"breaking below {name} {L:,.0f} ({kind.lower()} day)",
                  f"5-min close {p1:,.1f} below {'the previous-day high' if name == 'PDH' else 'the previous-day low'}"
                  f" {L:,.0f}. " + path)
        elif (abs(L - p1) / p1 * 100 <= near and abs(L - p0) / p0 * 100 > near
              and ((closes > L).all() or (closes < L).all())):      # only before the level has been crossed today
            alert(("op", "near", name, "above" if L > p1 else "below"), "MEDIUM",
                  f"near {name} {L:,.0f} ({'from below' if L > p1 else 'from above'}, going {d})",
                  f"{abs(L - p1):,.0f} pts away at {p1:,.1f}. " + path)


def _last_cross(a: pd.Series, b: pd.Series):
    """Bar start of the most recent cross of a over / under b (None if none in the data)."""
    sign = np.sign((a - b).dropna())
    ch = sign[sign != sign.shift()].index
    return ch[-1] if len(ch) > 1 else None


def _next_levels(marks, price) -> str:
    above = sorted((m for m in marks if m[1] > price), key=lambda m: m[1])
    below = sorted((m for m in marks if m[1] < price), key=lambda m: -m[1])
    parts = []
    if above:
        parts.append(f"next up: {above[0][0]} {above[0][1]:,.0f}")
    if below:
        parts.append(f"next down: {below[0][0]} {below[0][1]:,.0f}")
    return ("; ".join(parts) + ".") if parts else ""


def summary_lines(st: dict) -> list[str]:
    """Plain-text lines for the Gemini snapshot and tooltips."""
    if not st or st.get("error"):
        return []
    lv = st["levels"]
    out = [f"- Daily pivots (from {st['prev_day']:%d %b}): R3 {lv['R3']:,.0f} · R2 {lv['R2']:,.0f} · R1 {lv['R1']:,.0f}"
           f" · P {lv['P']:,.0f} · S1 {lv['S1']:,.0f} · S2 {lv['S2']:,.0f} · S3 {lv['S3']:,.0f}",
           f"- CPR {lv['BC']:,.0f}–{lv['TC']:,.0f} (width {st['cpr_width_pct']:.2f}%, {st['cpr_type']})"
           + (f", open {st['open_vs_cpr']} it" if st.get("open_vs_cpr") else "")
           + (", touched today" if st.get("cpr_touched") else ", untouched so far" if "cpr_touched" in st else ""),
           f"- Weekly pivot {st['weekly_pivot']:,.0f} · monthly pivot {st['monthly_pivot']:,.0f} · previous close "
           f"{lv['PDC']:,.0f}" if st.get("weekly_pivot") and st.get("monthly_pivot") else ""]
    if st.get("gap") is not None:
        out.append(f"- Opening gap {st['gap']:+,.0f} pts ({st['gap_pct']:+.2f}%), "
                   + ("filled" if st["gap_filled"] else "not filled yet"))
    if st.get("open_play"):
        out.append("- Open play: " + st["open_play"]["path"])
    if st.get("first_hour"):
        out.append(f"- First hour (9:15–10:20) {st['first_hour']['low']:,.0f}–{st['first_hour']['high']:,.0f}")
    if st.get("hourly_cpr"):
        h = st["hourly_cpr"]
        out.append(f"- Hourly CPR ({h['bar']:%H:%M} bar) {h['BC']:,.0f}–{h['TC']:,.0f}, price {h['pos']}")
    if st.get("ma"):
        out.append("- EMA 34×50 (hl2): " + ", ".join(f"{r['tf']} {r['status']}" for r in st["ma"])
                   + (f" — all {st['aligned']}" if st.get("aligned") else ""))
    if st.get("ema"):
        out.append("- EMA 10×20: " + ", ".join(f"{r['tf']} {r['status']}" for r in st["ema"]))
    if st.get("savitzky"):
        s = st["savitzky"]
        out.append(f"- Savitzky flow (15m): {s['trend']}, trailing band {s['band']:,.0f}")
    for c in st.get("untouched_cprs", [])[-4:]:
        out.append(f"- Untouched CPR of {c['day']:%d %b}: {c['bot']:,.0f}–{c['top']:,.0f}")
    for g in st.get("unfilled_gaps", [])[-4:]:
        out.append(f"- Unfilled {g['type']}-gap of {g['day']:%d %b}: {g['bot']:,.0f}–{g['top']:,.0f}")
    return [x for x in out if x]
