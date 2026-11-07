"""FII / DII positioning → a directional read for index option writing.

Inputs are the stored `fii_dii_activity` rows (NSE provisional cash figures and
participant-wise F&O open interest) and NIFTY's daily closes.  Each signal is
scored on a −2…+2 scale relative to its own recent history (so thresholds adapt
to the market), weighted, and combined into a −100…+100 positioning score:

  signal                         reads                                      weight
  FII cash flow (5 sessions)     net buying lifts the market                 1.0
  DII cash flow (5 sessions)     domestic buying cushions falls               0.5
  FII index futures              5-day change in long share and net OI       1.5
  FII index options              calls net − puts net (5-day change)         1.0
  Pro index options              proprietary desks' option tilt              0.5
  Client futures (contrarian)    crowded retail longs → weakness             0.75
  Put-call ratio                 high PCR = put writers defending            0.5
  NIFTY trend (10 sessions)      the tape confirms or contradicts            1.0

The score maps to a writing side (puts / both / calls), skewed structures and
strike zones built from the live option chain (OI support / resistance and the
expected move).  A walk-forward check over the stored history reports how often
NIFTY stayed on the "safe" side of a 1σ move in the next 5 sessions after each
kind of reading.  Rule-based screening, not investment advice.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

WEIGHTS = {"fii_cash": 1.0, "dii_cash": 0.5, "fii_fut": 1.5, "fii_opt": 1.0, "pro_opt": 0.5,
           "client": 0.75, "pcr": 0.5, "trend": 1.0}
LABELS = {"fii_cash": "FII cash flow (5 sessions)", "dii_cash": "DII cash flow (5 sessions)",
          "fii_fut": "FII index futures", "fii_opt": "FII index options", "pro_opt": "Pro (prop desk) index options",
          "client": "Client futures (contrarian)", "pcr": "Put-call ratio (index options OI)",
          "trend": "NIFTY trend (10 sessions)"}
FWD = 5                      # forward sessions for the walk-forward check
MIN_HIST = 20                # sessions of history before a signal is scored


def _clip(v, lo=-2.0, hi=2.0):
    return float(min(max(v, lo), hi)) if v is not None and not math.isnan(v) else 0.0


def _scaled(x: pd.Series, i: int, win: int = 60) -> float:
    """x[i] divided by the spread of x over the trailing window (no de-meaning: the sign matters)."""
    hist = x.iloc[max(0, i - win): i + 1].dropna()
    if len(hist) < MIN_HIST or pd.isna(x.iloc[i]):
        return 0.0
    sd = hist.std()
    return _clip(x.iloc[i] / sd) if sd else 0.0


def _pct_rank(x: pd.Series, i: int, win: int = 90) -> float | None:
    hist = x.iloc[max(0, i - win): i + 1].dropna()
    if len(hist) < MIN_HIST or pd.isna(x.iloc[i]):
        return None
    return float((hist <= x.iloc[i]).mean() * 100)


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    return df[name].astype(float) if name in df else pd.Series(np.nan, index=df.index)


def build_frame(fii: pd.DataFrame, nifty: pd.DataFrame | None) -> pd.DataFrame:
    """Daily frame of raw inputs and derived series, indexed by session date."""
    df = fii.copy().sort_index()
    df.index = pd.DatetimeIndex(df.index).tz_localize(None).normalize() if getattr(df.index, "tz", None) else \
        pd.DatetimeIndex(df.index).normalize()
    df = df[~df.index.duplicated(keep="last")]
    f = pd.DataFrame(index=df.index)
    f["fii_cash"] = _col(df, "fii_cash_net")
    f["dii_cash"] = _col(df, "dii_cash_net")
    f["fii_cash_5d"] = f["fii_cash"].rolling(5, min_periods=3).sum()
    f["dii_cash_5d"] = f["dii_cash"].rolling(5, min_periods=3).sum()
    f["fii_cash_20d"] = f["fii_cash"].rolling(20, min_periods=5).sum()
    fl, fs = _col(df, "fii_idx_fut_long"), _col(df, "fii_idx_fut_short")
    fl = fl.fillna(_col(df, "fii_index_fut_long"))
    fs = fs.fillna(_col(df, "fii_index_fut_short"))
    f["fii_fut_long"], f["fii_fut_short"] = fl, fs
    f["fii_long_pct"] = fl / (fl + fs) * 100
    f["fii_fut_net"] = fl - fs
    f["fii_call_net"] = _col(df, "fii_idx_call_long") - _col(df, "fii_idx_call_short")
    f["fii_put_net"] = _col(df, "fii_idx_put_long") - _col(df, "fii_idx_put_short")
    f["fii_call_net"] = f["fii_call_net"].fillna(_col(df, "fii_index_call_net"))
    f["fii_put_net"] = f["fii_put_net"].fillna(_col(df, "fii_index_put_net"))
    f["fii_opt_tilt"] = f["fii_call_net"] - f["fii_put_net"]          # >0 = bullish option book
    f["pro_opt_tilt"] = (_col(df, "pro_idx_call_long") - _col(df, "pro_idx_call_short")) - \
                        (_col(df, "pro_idx_put_long") - _col(df, "pro_idx_put_short"))
    cl, cs = _col(df, "client_idx_fut_long"), _col(df, "client_idx_fut_short")
    f["client_long_pct"] = cl / (cl + cs) * 100
    f["client_ratio"] = _col(df, "client_ratio")
    f["pcr"] = _col(df, "pcr")
    if nifty is not None and len(nifty):
        n = nifty["close"].copy()
        n.index = (n.index.tz_convert("Asia/Kolkata").tz_localize(None) if n.index.tz is not None else n.index)
        n = n.groupby(n.index.normalize()).last()
        f["nifty"] = n.reindex(f.index.union(n.index)).ffill().reindex(f.index)
        full = n.sort_index()
        ret = np.log(full).diff()
        f["nifty_ret10"] = (full / full.shift(10) - 1).reindex(f.index) * 100
        f["nifty_sd10"] = (ret.rolling(60, min_periods=20).std() * math.sqrt(10) * 100).reindex(f.index)
        fwd = (full.shift(-FWD) / full - 1) * 100
        f["fwd_ret"] = fwd.reindex(f.index)
        f["fwd_min"] = ((full[::-1].rolling(FWD, min_periods=1).min()[::-1].shift(-1) / full - 1) * 100).reindex(f.index)
        f["fwd_max"] = ((full[::-1].rolling(FWD, min_periods=1).max()[::-1].shift(-1) / full - 1) * 100).reindex(f.index)
        f["sd5"] = (ret.rolling(60, min_periods=20).std() * math.sqrt(FWD) * 100).reindex(f.index)
    return f


def score_day(f: pd.DataFrame, i: int) -> dict[str, float]:
    """Each signal's −2…+2 score on session i, using only data up to i."""
    s = {}
    s["fii_cash"] = _scaled(f["fii_cash_5d"], i)
    s["dii_cash"] = _scaled(f["dii_cash_5d"], i)
    d_long = f["fii_long_pct"].diff(5)
    d_net = f["fii_fut_net"].diff(5)
    s["fii_fut"] = _clip((_scaled(d_long, i) + _scaled(d_net, i)) / 2 * 1.4)
    s["fii_opt"] = _scaled(f["fii_opt_tilt"].diff(5), i)
    s["pro_opt"] = _scaled(f["pro_opt_tilt"].diff(5), i)
    pr = _pct_rank(f["client_long_pct"], i)
    s["client"] = _clip(-(pr - 50) / 25) if pr is not None else 0.0
    pr = _pct_rank(f["pcr"], i)
    s["pcr"] = _clip((pr - 50) / 25) if pr is not None else 0.0
    if "nifty_ret10" in f and not pd.isna(f["nifty_ret10"].iloc[i]) and f["nifty_sd10"].iloc[i]:
        s["trend"] = _clip(f["nifty_ret10"].iloc[i] / f["nifty_sd10"].iloc[i] * 1.5)
    else:
        s["trend"] = 0.0
    return s


def composite(s: dict[str, float]) -> float:
    tot = sum(WEIGHTS[k] * 2 for k in WEIGHTS)
    return sum(WEIGHTS[k] * s.get(k, 0.0) for k in WEIGHTS) / tot * 100


def bias_of(score: float) -> tuple[str, str]:
    if score >= 30:
        return "Bullish", "#22c55e"
    if score >= 10:
        return "Mildly bullish", "#86efac"
    if score <= -30:
        return "Bearish", "#ef4444"
    if score <= -10:
        return "Mildly bearish", "#fca5a5"
    return "Neutral / range", "#e5e7eb"


def _ord(n: float) -> str:
    n = int(round(n))
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _fmt_cr(v):
    return "—" if v is None or pd.isna(v) else f"{v:+,.0f} cr"


def _fmt_k(v):
    if v is None or pd.isna(v):
        return "—"
    return f"{v / 1000:+,.1f}K" if abs(v) >= 1000 else f"{v:+,.0f}"


def _streak(x: pd.Series) -> tuple[int, int]:
    """(sign, length) of the current run of buying (+1) or selling (−1) days."""
    vals = [v for v in x.dropna().values[::-1]]
    if not vals:
        return 0, 0
    sign = 1 if vals[0] > 0 else -1
    n = 0
    for v in vals:
        if (v > 0) == (sign > 0):
            n += 1
        else:
            break
    return sign, n


def analyse(fii: pd.DataFrame, nifty: pd.DataFrame | None, banknifty: pd.DataFrame | None = None) -> dict:
    if fii is None or fii.empty:
        return {"available": False}
    f = build_frame(fii, nifty)
    n = len(f)
    hist = []
    for i in range(n):
        s = score_day(f, i)
        hist.append({"date": f.index[i], "score": composite(s), **{f"s_{k}": v for k, v in s.items()}})
    h = pd.DataFrame(hist).set_index("date")
    f = f.join(h)
    i = n - 1
    s = {k: float(h[f"s_{k}"].iloc[i]) for k in WEIGHTS}
    score = float(h["score"].iloc[i])
    bias, color = bias_of(score)
    active = [v for v in s.values() if abs(v) >= 0.5]
    agree = (sum(1 for v in active if (v > 0) == (score > 0)) / len(active) * 100) if active and abs(score) >= 5 else None
    conviction = ("High" if agree is not None and agree >= 70 and abs(score) >= 30 else
                  "Medium" if agree is not None and agree >= 55 and abs(score) >= 10 else "Low")
    last = f.iloc[i]

    def chg(col, k=5):
        x = f[col]
        return (x.iloc[i] - x.iloc[max(0, i - k)]) if not pd.isna(x.iloc[i]) else None

    fii_streak, dii_streak = _streak(f["fii_cash"]), _streak(f["dii_cash"])
    long_rank = _pct_rank(f["fii_long_pct"], i)
    cl_rank = _pct_rank(f["client_long_pct"], i)
    pcr_rank = _pct_rank(f["pcr"], i)
    comps = [
        ("fii_cash", _fmt_cr(last["fii_cash_5d"]), f"today {_fmt_cr(last['fii_cash'])} · "
         f"{'buying' if fii_streak[0] > 0 else 'selling'} {fii_streak[1]} day(s) in a row",
         "FIIs are net buyers in cash" if last["fii_cash_5d"] > 0 else "FIIs are net sellers in cash"),
        ("dii_cash", _fmt_cr(last["dii_cash_5d"]), f"today {_fmt_cr(last['dii_cash'])} · "
         f"{'buying' if dii_streak[0] > 0 else 'selling'} {dii_streak[1]} day(s)",
         "DIIs absorbing supply" if last["dii_cash_5d"] > 0 else "DIIs also selling"),
        ("fii_fut", f"long {last['fii_long_pct']:.1f}%" if not pd.isna(last["fii_long_pct"]) else "—",
         f"{chg('fii_long_pct') or 0:+.1f} pp in 5 sessions · net {_fmt_k(last['fii_fut_net'])} "
         f"({_fmt_k(chg('fii_fut_net'))})",
         ("adding longs / covering shorts" if s["fii_fut"] > 0.25 else
          "adding shorts / cutting longs" if s["fii_fut"] < -0.25 else "little change")
         + (f" · long share at {_ord(long_rank)} percentile of 90 sessions" if long_rank is not None else "")),
        ("fii_opt", _fmt_k(last["fii_opt_tilt"]), f"calls net {_fmt_k(last['fii_call_net'])} · puts net "
         f"{_fmt_k(last['fii_put_net'])} · 5-session change {_fmt_k(chg('fii_opt_tilt'))}",
         "option book turning bullish (calls bought / puts written)" if s["fii_opt"] > 0.25 else
         "option book turning bearish (puts bought / calls written)" if s["fii_opt"] < -0.25 else "steady"),
        ("pro_opt", _fmt_k(last["pro_opt_tilt"]), f"5-session change {_fmt_k(chg('pro_opt_tilt'))}",
         "prop desks leaning long" if s["pro_opt"] > 0.25 else "prop desks leaning short" if s["pro_opt"] < -0.25
         else "neutral"),
        ("client", f"long {last['client_long_pct']:.1f}%" if not pd.isna(last["client_long_pct"]) else "—",
         f"{_ord(cl_rank)} percentile of 90 sessions" if cl_rank is not None else "—",
         "retail crowded long (contrarian negative)" if s["client"] < -0.5 else
         "retail cautious (contrarian positive)" if s["client"] > 0.5 else "not extreme"),
        ("pcr", f"{last['pcr']:.2f}" if not pd.isna(last["pcr"]) else "—",
         f"{_ord(pcr_rank)} percentile · 5-session change {chg('pcr') or 0:+.2f}" if pcr_rank is not None else "—",
         "put writers defending (supportive)" if s["pcr"] > 0.5 else
         "call-heavy (capping)" if s["pcr"] < -0.5 else "balanced"),
        ("trend", f"{last['nifty_ret10']:+.1f}%" if "nifty_ret10" in f and not pd.isna(last["nifty_ret10"]) else "—",
         f"NIFTY {last['nifty']:,.0f}" if "nifty" in f and not pd.isna(last.get("nifty")) else "—",
         "uptrend" if s["trend"] > 0.5 else "downtrend" if s["trend"] < -0.5 else "sideways"),
    ]
    components = [{"key": k, "label": LABELS[k], "value": v, "detail": d, "reading": r, "score": s[k],
                   "weight": WEIGHTS[k]} for k, v, d, r in comps]
    warnings = []
    lp = last["fii_long_pct"]
    if (long_rank is not None and long_rank <= 10) or (not pd.isna(lp) and lp < 20):
        warnings.append(f"FII index futures are only {lp:.0f}% long (historically very short): heavy shorts can "
                        "unwind in sharp short-covering rallies — keep call writes hedged (spreads) and away from "
                        "spot.")
    if long_rank is not None and long_rank >= 90:
        warnings.append("FII long share is near a 90-session high: crowded longs can unwind fast — hedge put writes.")
    if s["fii_cash"] * s["fii_fut"] < -0.5:
        warnings.append("FII cash and futures disagree (one buying, the other selling): mixed signal — size down "
                        "and prefer defined-risk structures.")
    if "trend" in s and s["trend"] * score < -0.5 and abs(score) >= 10:
        warnings.append("Price trend is against the positioning read: wait for confirmation or widen the strikes.")
    stale = (pd.Timestamp.now().normalize() - f.index[-1]).days
    if stale > 4:
        warnings.append(f"Latest data is {stale} days old — sync FII/DII before acting on this read.")
    return {
        "available": True, "as_of": f.index[-1], "sessions": n, "score": score, "bias": bias, "color": color,
        "conviction": conviction, "agreement": agree, "components": components, "warnings": warnings,
        "playbook": playbook(bias), "evidence": evidence(f), "frame": f, "setup": setup_check(f, banknifty),
        "streaks": {"fii": fii_streak, "dii": dii_streak},
    }


def playbook(bias: str) -> dict:
    """Option-writing posture for each bias (screening rules, not advice)."""
    books = {
        "Bullish": {"side": "Put writing", "skew": "puts closer (~0.20–0.25Δ), calls far (≤0.10Δ) or none",
                    "structures": ["Bull put spread below OI support", "Short put / put ratio below support",
                                   "Skewed strangle: put leg near support, call leg beyond 1.4σ"],
                    "avoid": "Naked calls near spot — positioning favours upside bursts."},
        "Mildly bullish": {"side": "Put-leaning, both sides", "skew": "put side ~0.20Δ, call side ~0.12Δ",
                           "structures": ["Iron condor skewed upward", "Bull put spread + far bear call spread"],
                           "avoid": "Tight call strikes."},
        "Neutral / range": {"side": "Both sides (range)", "skew": "balanced, ~0.15Δ each side (≈ ±1σ)",
                            "structures": ["Iron condor at ≈ ±1σ", "Short strangle with defined wings",
                                           "Prefer it when ATM IV is above realised vol"],
                            "avoid": "Directional bets; adjust early if either short strike is tested."},
        "Mildly bearish": {"side": "Call-leaning, both sides", "skew": "call side ~0.20Δ, put side ~0.12Δ",
                           "structures": ["Iron condor skewed downward", "Bear call spread + far bull put spread"],
                           "avoid": "Tight put strikes."},
        "Bearish": {"side": "Call writing", "skew": "calls closer (~0.20–0.25Δ), puts far (≤0.10Δ) or none",
                    "structures": ["Bear call spread above OI resistance", "Short call above resistance",
                                   "Skewed strangle: call leg near resistance, put leg beyond 1.4σ"],
                    "avoid": "Naked puts near spot — positioning favours downside air pockets."},
    }
    return books[bias]


K_SIGMA = {"Bullish": (0.8, 1.4), "Mildly bullish": (0.9, 1.2), "Neutral / range": (1.0, 1.0),
           "Mildly bearish": (1.2, 0.9), "Bearish": (1.4, 0.8)}


def strike_zones(bias: str, res: dict) -> dict | None:
    """Short-strike zones from a live option-chain analysis: the farther of the
    expected-move distance (scaled by bias) and the OI wall on each side."""
    if not res or not res.get("sigma_move"):
        return None
    S, sig, step = res["spot"], res["sigma_move"], res.get("step") or 50
    summ = res.get("chain_summary") or {}
    kp, kc = K_SIGMA[bias]
    put = S - kp * sig
    if summ.get("support") and summ["support"] < S:
        put = min(put, summ["support"])
    call = S + kc * sig
    if summ.get("resistance") and summ["resistance"] > S:
        call = max(call, summ["resistance"])
    put = math.floor(put / step) * step
    call = math.ceil(call / step) * step
    wing = max(step, round(0.5 * sig / step) * step)
    return {"underlying": res["underlying"], "expiry": res["expiry"], "dte": res["dte"], "spot": S, "sigma": sig,
            "put": put, "put_hedge": put - wing, "call": call, "call_hedge": call + wing,
            "put_dist": (put / S - 1) * 100, "call_dist": (call / S - 1) * 100,
            "support": summ.get("support"), "resistance": summ.get("resistance"),
            "use_put": bias != "Bearish", "use_call": bias != "Bullish"}


def evidence(f: pd.DataFrame) -> dict:
    """Walk-forward check: after each kind of reading, how often did NIFTY stay on
    the writer's safe side of a 1σ (5-session) move over the next 5 sessions?"""
    if "fwd_min" not in f:
        return {}
    d = f.dropna(subset=["fwd_min", "fwd_max", "sd5", "score"])
    d = d.iloc[MIN_HIST:]                                    # skip the warm-up
    out = {}
    groups = {"bullish": d[d["score"] >= 10], "neutral": d[(d["score"] > -10) & (d["score"] < 10)],
              "bearish": d[d["score"] <= -10]}
    for k, g in groups.items():
        if g.empty:
            out[k] = {"n": 0}
            continue
        put_safe = (g["fwd_min"] > -g["sd5"]).mean() * 100      # never fell more than 1σ
        call_safe = (g["fwd_max"] < g["sd5"]).mean() * 100      # never rose more than 1σ
        both = ((g["fwd_min"] > -g["sd5"]) & (g["fwd_max"] < g["sd5"])).mean() * 100
        out[k] = {"n": len(g), "put_safe": float(put_safe), "call_safe": float(call_safe), "range": float(both),
                  "avg_ret": float(g["fwd_ret"].mean()), "up": float((g["fwd_ret"] > 0).mean() * 100)}
    allp = d
    out["all"] = {"n": len(allp),
                  "put_safe": float((allp["fwd_min"] > -allp["sd5"]).mean() * 100) if len(allp) else None,
                  "call_safe": float((allp["fwd_max"] < allp["sd5"]).mean() * 100) if len(allp) else None}
    return out


# ---------------------------------------------------------------- evening setup alert
SETUP_SCORE = 50          # |positioning score| at or beyond this
SETUP_FUT_DAYS = 2        # consecutive sessions of FII index-futures net moving the same way
SETUP_CASH_DAYS = 3       # consecutive sessions of FII cash buying / selling


def _run(x: pd.Series) -> pd.Series:
    """Length of the current run of True values at each row."""
    x = x.fillna(False).astype(int)
    return x.groupby((x != x.shift()).cumsum()).cumsum() * x


def setup_flags(f: pd.DataFrame) -> pd.DataFrame:
    """Per session: the institutional squeeze-loading setup, up and down.
    Up   = score ≥ +50, FII futures net rising ≥2 sessions (covering shorts / adding longs),
           FII cash buying ≥3 sessions.   Down = the mirror image."""
    fut = f["fii_fut_net"].diff()
    out = pd.DataFrame(index=f.index)
    out["fut_up_run"], out["fut_dn_run"] = _run(fut > 0), _run(fut < 0)
    out["cash_buy_run"], out["cash_sell_run"] = _run(f["fii_cash"] > 0), _run(f["fii_cash"] < 0)
    out["up"] = (f["score"] >= SETUP_SCORE) & (out["fut_up_run"] >= SETUP_FUT_DAYS) & \
        (out["cash_buy_run"] >= SETUP_CASH_DAYS)
    out["down"] = (f["score"] <= -SETUP_SCORE) & (out["fut_dn_run"] >= SETUP_FUT_DAYS) & \
        (out["cash_sell_run"] >= SETUP_CASH_DAYS)
    return out


def setup_check(f: pd.DataFrame, banknifty: pd.DataFrame | None = None) -> dict:
    """Today's (latest session's) setup, the conditions behind it, and how BANKNIFTY moved the
    next session every previous time it fired, compared with all sessions."""
    fl = setup_flags(f)
    last = fl.iloc[-1]
    row = f.iloc[-1]
    direction = "up" if last["up"] else "down" if last["down"] else None
    lean = "up" if row["score"] >= 0 else "down"
    d = direction or lean
    conds = [
        (f"Positioning score {'≥ +' if d == 'up' else '≤ −'}{SETUP_SCORE}", f"{row['score']:+.0f}",
         row["score"] >= SETUP_SCORE if d == "up" else row["score"] <= -SETUP_SCORE),
        (f"FII index futures net {'rising (covering)' if d == 'up' else 'falling (adding shorts)'} "
         f"≥ {SETUP_FUT_DAYS} sessions", f"{int(last['fut_up_run'] if d == 'up' else last['fut_dn_run'])} sessions",
         (last["fut_up_run"] if d == "up" else last["fut_dn_run"]) >= SETUP_FUT_DAYS),
        (f"FII cash {'buying' if d == 'up' else 'selling'} ≥ {SETUP_CASH_DAYS} sessions",
         f"{int(last['cash_buy_run'] if d == 'up' else last['cash_sell_run'])} sessions",
         (last["cash_buy_run"] if d == "up" else last["cash_sell_run"]) >= SETUP_CASH_DAYS),
    ]
    out = {"date": f.index[-1], "direction": direction, "lean": lean, "conditions": conds, "record": None}
    if banknifty is not None and len(banknifty):
        b = banknifty.copy()
        b.index = (b.index.tz_convert("Asia/Kolkata").tz_localize(None) if b.index.tz is not None
                   else b.index).normalize()
        b = b[~b.index.duplicated(keep="last")]
        nxt = pd.DataFrame({"ret": b["close"].shift(-1) / b["close"] - 1,
                            "gap": b["open"].shift(-1) / b["close"] - 1}).reindex(f.index) * 100
        hist = fl.join(nxt).iloc[MIN_HIST:].dropna(subset=["ret"])

        def stats(g):
            if not len(g):
                return {"n": 0}
            return {"n": int(len(g)), "up": float((g["ret"] > 0).mean() * 100), "avg": float(g["ret"].mean()),
                    "gap_up": float((g["gap"] > 0).mean() * 100), "big": int((g["ret"].abs() >= 1.5).sum()),
                    "dates": [f"{x:%d %b}" for x in g.index[-6:]]}
        out["record"] = {"all": stats(hist), "up": stats(hist[hist["up"]]), "down": stats(hist[hist["down"]])}
    return out


def chain_confirmation(direction: str, res: dict | None) -> dict | None:
    """Does the closing option chain agree?  Up: OTM calls within 1σ above spot being bought
    (long build-up) and more puts than calls written today (PCR of the OI change > 1).
    Down: the mirror image."""
    if not res or not res.get("chain"):
        return None
    S, sig = res["spot"], res.get("sigma_move") or res["spot"] * 0.02
    summ = res.get("chain_summary") or {}
    if direction == "up":
        band = [r for r in res["chain"] if S < r["strike"] <= S + sig]
        bought = [r["strike"] for r in band if r.get("ce_buildup") == "Long build-up"]
        pcr = summ.get("pcr_chg")
        writers = pcr is not None and pcr > 1
        txt_w = (f"PCR of today's OI change {pcr:.2f} ({'> 1: put writers defending' if writers else '≤ 1: no put-writing support'})"
                 if pcr else "PCR change n/a")
        side = "calls above spot"
    else:
        band = [r for r in res["chain"] if S - sig <= r["strike"] < S]
        bought = [r["strike"] for r in band if r.get("pe_buildup") == "Long build-up"]
        pcr = summ.get("pcr_chg")
        writers = pcr is not None and pcr < 1
        txt_w = (f"PCR of today's OI change {pcr:.2f} ({'< 1: call writers capping' if writers else '≥ 1: no call-writing cap'})"
                 if pcr else "PCR change n/a")
        side = "puts below spot"
    share = len(bought) / len(band) if band else 0
    return {"underlying": res["underlying"], "bought_share": share, "bought": bought, "writers": writers,
            "confirmed": share >= 0.5 and writers,
            "text": f"{res['underlying']}: {len(bought)}/{len(band)} {side} in long build-up · {txt_w}"}
