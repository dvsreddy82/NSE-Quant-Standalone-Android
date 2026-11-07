"""Trade journal engine: broker fills -> round trips -> strategies -> metrics and
a "why did I lose" diagnosis.  Pure functions, no I/O.

Pipeline
--------
fills (from Shoonya's TradeBook, or a CSV import)
  -> round_trips(): FIFO-match fills per contract into closed legs (entry/exit,
     P&L, charges); contracts still open stay open; options past expiry are
     settled at intrinsic value from the underlying's close.
  -> strategies(): legs of the same underlying + expiry opened on the same day
     form one strategy (iron condor, strangle, spread, ...).
  -> metrics() / breakdowns(): win rate, payoff, expectancy, profit factor,
     drawdown, P&L by structure / DTE / weekday / environment at entry, ...
  -> diagnose(): rule-based findings ranked by rupee impact — e.g. "you sold
     when IV was below realised vol", "losses ran past 2× credit".

Every fill can carry `ctx`: the market read when it was placed (spot, IV,
realised vol, VIX, environment score, trend, OI walls, events).  Fills seen live
by the app get the Options-tab analysis; imported/older fills get a context
reconstructed from daily data (no IV) by reconstruct_ctx().
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

import config

# ------------------------------------------------------------------ symbols
_OPT = re.compile(r"^(?P<und>[A-Z&]+?)(?P<exp>\d{2}[A-Z]{3}\d{2})(?P<kind>[CPF])(?P<strike>\d+(?:\.\d+)?)?$")
_OPT_SPACED = re.compile(r"^(?P<und>[A-Z&]+)\s+(?P<exp>\d{1,2}\s?[A-Z]{3}\s?\d{2,4})\s+(?P<strike>\d+(?:\.\d+)?)\s+(?P<kind>CE|PE)$")
NEUTRAL = {"Iron condor", "Iron butterfly", "Short strangle", "Short straddle"}
NEUTRAL |= {n + " + adjustments" for n in NEUTRAL}


def parse_tsym(tsym: str) -> dict:
    """NIFTY17NOV26C24000 / NIFTY25NOV26F / 'NIFTY 17NOV26 24000 CE' / NIFTYBEES-EQ."""
    t = (tsym or "").strip().upper()
    m = _OPT.match(t)
    if m:
        kind = {"C": "CE", "P": "PE", "F": "FUT"}[m["kind"]]
        return {"und": m["und"], "expiry": datetime.strptime(m["exp"], "%d%b%y").date(), "kind": kind,
                "strike": float(m["strike"]) if m["strike"] and kind != "FUT" else None}
    m = _OPT_SPACED.match(t)
    if m:
        e = m["exp"].replace(" ", "")
        exp = datetime.strptime(e, "%d%b%Y" if len(e) > 7 else "%d%b%y").date()
        return {"und": m["und"], "expiry": exp, "kind": m["kind"], "strike": float(m["strike"])}
    return {"und": t.removesuffix("-EQ"), "expiry": None, "kind": "EQ", "strike": None}


def _ts(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize(config.IST) if t.tzinfo is None else t.tz_convert(config.IST)


# ------------------------------------------------------------------ fills
def fill_from_shoonya(r: dict) -> dict | None:
    """One TradeBook row -> normalised fill."""
    try:
        qty = int(float(r.get("flqty") or r.get("qty") or 0))
        px = float(r.get("flprc") or r.get("avgprc") or 0)
    except (TypeError, ValueError):
        return None
    if qty <= 0:
        return None
    t = pd.to_datetime(r.get("fltm") or r.get("exch_tm") or r.get("norentm"), dayfirst=True, errors="coerce")
    if pd.isna(t):
        return None
    tsym = r.get("tsym", "")
    return {"fill_id": f"{r.get('norenordno', '')}-{r.get('flid', '') or t.value}",
            "order_no": str(r.get("norenordno", "")), "time": _ts(t).to_pydatetime(), "tsym": tsym,
            "exch": r.get("exch", ""), "token": str(r.get("token", "")),
            "side": "B" if str(r.get("trantype", "")).upper().startswith("B") else "S",
            "qty": qty, "price": px, "lot": int(float(r.get("ls") or 1) or 1), "product": r.get("prd", ""),
            "source": "shoonya", **_parsed(tsym)}


def _parsed(tsym):
    p = parse_tsym(tsym)
    return {**p, "expiry": datetime.combine(p["expiry"], datetime.min.time()) if p["expiry"] else None}


def fills_from_csv(df: pd.DataFrame) -> list[dict]:
    """Tolerant importer for a broker trade-history export: needs columns for
    date/time, symbol, buy/sell, quantity and price (names matched loosely)."""
    cols = {c.lower().strip(): c for c in df.columns}

    def pick(*keys, exclude=()):
        for k in keys:
            for lc, c in cols.items():
                if k in lc and not any(x in lc for x in exclude):
                    return c
        return None
    ct = pick("time", "date")
    cs = pick("tradingsymbol", "trading symbol", "tsym", "symbol", "scrip", "instrument")
    cb = pick("trantype", "buy/sell", "side", "type", "transaction")
    cq = pick("qty", "quantity", exclude=("lot",))
    cp = pick("price", "rate", "avg", exclude=("strike",))
    missing = [n for n, c in (("date/time", ct), ("symbol", cs), ("buy/sell", cb), ("qty", cq), ("price", cp)) if not c]
    if missing:
        raise ValueError("CSV is missing columns for: " + ", ".join(missing))
    out = []
    for i, r in df.iterrows():
        t = pd.to_datetime(r[ct], dayfirst=True, errors="coerce")
        try:
            qty, px = abs(int(float(str(r[cq]).replace(",", "")))), float(str(r[cp]).replace(",", ""))
        except ValueError:
            continue
        if pd.isna(t) or qty == 0:
            continue
        tsym = str(r[cs]).strip()
        side = "B" if str(r[cb]).strip().upper().startswith("B") else "S"
        fid = hashlib.sha1(f"{t}|{tsym}|{side}|{qty}|{px}|{i}".encode()).hexdigest()[:16]
        out.append({"fill_id": f"csv-{fid}", "order_no": "", "time": _ts(t).to_pydatetime(), "tsym": tsym,
                    "exch": "NFO", "token": "", "side": side, "qty": qty, "price": px, "lot": 1,
                    "product": "", "source": "csv", **_parsed(tsym)})
    return out


# ------------------------------------------------------------------ charges (estimates)
def charges(kind: str, side: str, price: float, qty: int) -> float:
    """Statutory charges estimate in ₹ for one fill (config.CHARGES)."""
    c = config.CHARGES["option" if kind in ("CE", "PE") else "future" if kind == "FUT" else "equity"]
    turnover = price * qty
    stt = turnover * (c["stt_sell"] if side == "S" else c["stt_buy"])
    exch = turnover * c["exchange"]
    sebi = turnover * 10 / 1e7
    stamp = turnover * c["stamp_buy"] if side == "B" else 0.0
    brokerage = config.BROKERAGE_PER_ORDER
    gst = 0.18 * (brokerage + exch + sebi)
    return stt + exch + sebi + stamp + brokerage + gst


# ------------------------------------------------------------------ round trips
def round_trips(fills: list[dict], settle=None, today: date | None = None) -> tuple[list[dict], list[dict]]:
    """FIFO-match fills per contract.  `settle(und, expiry_date)` returns the
    underlying's close on expiry (to settle options held to expiry) or None.
    Returns (closed legs, open legs)."""
    today = today or date.today()
    by_sym = defaultdict(list)
    for f in fills:
        by_sym[f["tsym"]].append(f)
    closed, still_open = [], []
    for tsym, fs in by_sym.items():
        fs.sort(key=lambda f: (_ts(f["time"]), f["fill_id"]))
        queue: list[dict] = []          # open lots: {side, qty, price, time, ctx, fill}
        for f in fs:
            q = f["qty"]
            while q and queue and queue[0]["side"] != f["side"]:
                lot = queue[0]
                m = min(q, lot["qty"])
                closed.append(_leg(lot, m, f["price"], f["time"], "closed", f))
                lot["qty"] -= m
                q -= m
                if lot["qty"] == 0:
                    queue.pop(0)
            if q:
                queue.append({"side": f["side"], "qty": q, "price": f["price"], "time": f["time"], "fill": f})
        for lot in queue:
            f = lot["fill"]
            exp = f.get("expiry")
            exp_d = pd.Timestamp(exp).date() if exp is not None else None
            if exp_d and exp_d < today and f["kind"] in ("CE", "PE", "FUT"):
                s = settle(f["und"], exp_d) if settle else None
                if s is not None:
                    if f["kind"] == "CE":
                        px = max(0.0, s - f["strike"])
                    elif f["kind"] == "PE":
                        px = max(0.0, f["strike"] - s)
                    else:
                        px = s
                    closed.append(_leg(lot, lot["qty"], px, datetime.combine(exp_d, datetime.min.time())
                                       + timedelta(hours=15, minutes=30), "expired", None, settle_px=s))
                    continue
            still_open.append(_leg(lot, lot["qty"], None, None, "open", None))
    return closed, still_open


def _leg(lot, qty, exit_px, exit_time, reason, exit_fill, settle_px=None) -> dict:
    f = lot["fill"]
    short = lot["side"] == "S"
    entry_px = lot["price"]
    pnl = None if exit_px is None else (entry_px - exit_px) * qty if short else (exit_px - entry_px) * qty
    ch = charges(f["kind"], lot["side"], entry_px, qty)
    if exit_px is not None and reason == "closed":
        ch += charges(f["kind"], "B" if short else "S", exit_px, qty)
    elif reason == "expired" and not short and exit_px:
        ch += exit_px * qty * 0.00125           # STT on exercised long ITM options
    return {"tsym": f["tsym"], "und": f["und"], "kind": f["kind"], "strike": f["strike"],
            "expiry": pd.Timestamp(f["expiry"]).date() if f.get("expiry") is not None else None,
            "side": "SHORT" if short else "LONG", "qty": qty, "lot": f.get("lot", 1),
            "entry_px": entry_px, "exit_px": exit_px, "entry_time": _ts(lot["time"]),
            "exit_time": _ts(exit_time) if exit_time is not None else None, "exit_reason": reason,
            "pnl": pnl, "charges": ch, "settle": settle_px, "ctx": f.get("ctx") or {},
            "entry_fill": f["fill_id"], "exit_fill": exit_fill["fill_id"] if exit_fill else None,
            "token": f.get("token", ""), "exch": f.get("exch", "")}


# ------------------------------------------------------------------ strategies
def classify(legs: list[dict]) -> tuple[str, bool]:
    """(structure name, defined risk?) from the legs' sides / kinds / strikes / quantities."""
    def strikes(side, kind):
        return sorted({l["strike"] for l in legs if l["side"] == side and l["kind"] == kind})

    def qty(side, kind):
        return sum(l["qty"] for l in legs if l["side"] == side and l["kind"] == kind)
    sc, sp, lc, lp = strikes("SHORT", "CE"), strikes("SHORT", "PE"), strikes("LONG", "CE"), strikes("LONG", "PE")
    kinds = {l["kind"] for l in legs}
    if kinds == {"FUT"}:
        return "Futures", False
    if kinds == {"EQ"}:
        return "Equity / ETF", True
    # defined risk: every short is covered by at least as many longs further out of the money
    covered_c = not sc or (lc and max(lc) > max(sc) and qty("LONG", "CE") >= qty("SHORT", "CE"))
    covered_p = not sp or (lp and min(lp) < min(sp) and qty("LONG", "PE") >= qty("SHORT", "PE"))
    defined = bool(covered_c and covered_p)
    straddle = bool(set(sc) & set(sp))
    if sc and sp and lc and lp:
        name, base = ("Iron butterfly", 3) if straddle else ("Iron condor", 4)
    elif sc and sp and not lc and not lp:
        name, base = ("Short straddle", 1) if straddle else ("Short strangle", 2)
    elif sp and lp and not sc and not lc:
        name, base = ("Bull put spread" if max(sp) > min(lp) else "Put spread (debit)"), 2
    elif sc and lc and not sp and not lp:
        name, base = ("Bear call spread" if min(sc) < max(lc) else "Call spread (debit)"), 2
    elif sp and not (sc or lc or lp):
        name, base = "Naked short put", 1
    elif sc and not (sp or lc or lp):
        name, base = "Naked short call", 1
    elif (lc or lp) and not (sc or sp):
        name, base = ("Long call" if lc and not lp else "Long put" if lp and not lc else "Long strangle/straddle"), 2
    else:
        return f"Custom ({len(legs)} legs)", defined
    n_strikes = len(set(sc) | set(sp) | set(lc) | set(lp)) if not straddle else len(set(sc) | set(lc) | set(lp))
    if n_strikes > base and name not in ("Long strangle/straddle",):
        name += " + adjustments"
    return name, defined


def strategies(closed: list[dict], open_legs: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for l in closed + open_legs:
        key = (l["und"], l["expiry"], l["entry_time"].date())
        groups[key].append(l)
    out = []
    for (und, exp, d), legs in groups.items():
        structure, defined = classify(legs)
        is_open = any(l["exit_reason"] == "open" for l in legs)
        credit = sum((l["entry_px"] if l["side"] == "SHORT" else -l["entry_px"]) * l["qty"] for l in legs)
        pnl = sum(l["pnl"] or 0 for l in legs)
        ch = sum(l["charges"] for l in legs)
        entry = min(l["entry_time"] for l in legs)
        exit_ = None if is_open else max(l["exit_time"] for l in legs)
        lots = max((l["qty"] // max(l["lot"], 1) for l in legs), default=0)
        ctx = next((l["ctx"] for l in sorted(legs, key=lambda l: l["entry_time"]) if l["ctx"]), {})
        width = _max_width(legs)
        max_loss = (width - credit) if defined and width is not None and credit else None
        exp_dt = datetime.combine(exp, datetime.min.time()) if exp else None
        out.append({
            "id": f"{und}|{exp or '-'}|{d}", "und": und, "expiry": exp, "entry_date": d,
            "entry_time": entry, "exit_time": exit_, "status": "open" if is_open else "closed",
            "structure": structure, "defined_risk": defined, "legs": sorted(legs, key=lambda l: (l["kind"], l["strike"] or 0)),
            "credit": credit, "pnl": pnl, "charges": ch, "net": pnl - ch, "lots": lots, "max_loss": max_loss,
            "dte_entry": (exp_dt.date() - entry.date()).days if exp_dt else None,
            "holding_days": ((exit_ or pd.Timestamp.now(tz=config.IST)) - entry).total_seconds() / 86400,
            "expired": any(l["exit_reason"] == "expired" for l in legs),
            "held_to_expiry": bool(exp) and not is_open and (any(l["exit_reason"] == "expired" for l in legs)
                                                            or exit_.date() >= exp),
            "loss_x_credit": (-(pnl - ch) / credit) if credit > 0 and pnl - ch < 0 else 0.0,
            "captured": ((pnl - ch) / credit) if credit > 0 else None,
            "ctx": ctx,
        })
    out.sort(key=lambda s: s["entry_time"])
    for s in out:
        s["flags"] = [f for f, _ in _flags(s)]
    return out


def _max_width(legs) -> float | None:
    w = []
    for kind in ("CE", "PE"):
        sh = [l["strike"] for l in legs if l["kind"] == kind and l["side"] == "SHORT"]
        lo = [l["strike"] for l in legs if l["kind"] == kind and l["side"] == "LONG"]
        if sh and lo:
            qty = max(l["qty"] for l in legs if l["kind"] == kind)
            w.append(abs(max(sh) - max(lo) if kind == "CE" else min(sh) - min(lo)) * qty)
    return max(w) if w else None


def short_sigma(s: dict) -> float | None:
    """Closest short strike's distance from spot at entry, in expected-move σ."""
    c = s["ctx"]
    spot, iv = c.get("spot"), (c.get("atm_iv") or c.get("hv20"))
    if not spot or not iv or s["dte_entry"] is None:
        return None
    sig = spot * iv / 100 * math.sqrt(max(s["dte_entry"], 0.5) / 365)
    d = [abs(l["strike"] - spot) / sig for l in s["legs"] if l["side"] == "SHORT" and l["strike"]]
    return min(d) if d else None


# ------------------------------------------------------------------ context
def reconstruct_ctx(und: str, when, daily: pd.DataFrame | None, vix: pd.Series | None, expiry=None) -> dict:
    """Best-effort market read at `when` from daily data (no option IV available)."""
    if daily is None or daily.empty:
        return {}
    d = daily.copy()
    d.index = d.index.tz_convert(config.IST).tz_localize(None).normalize() if d.index.tz is not None else d.index.normalize()
    day = pd.Timestamp(_ts(when).date())
    hist = d[d.index < day]               # info available before that session
    if len(hist) < 30:
        return {}
    close = hist["close"]
    r = np.log(close).diff().dropna()
    hv20 = float(r.tail(20).std() * math.sqrt(252) * 100)
    from indicators import adx as _adx, ema
    adx = float(_adx(hist).iloc[-1])
    e20, e50 = ema(close, 20).iloc[-1], ema(close, 50).iloc[-1]
    trend = "Bullish" if close.iloc[-1] > e20 > e50 and adx >= 20 else \
            "Bearish" if close.iloc[-1] < e20 < e50 and adx >= 20 else "Range-bound"
    v = None
    if vix is not None and len(vix.dropna()):
        vv = vix.dropna()
        vv.index = pd.to_datetime(vv.index).normalize()
        vv = vv[vv.index < day]
        v = float(vv.iloc[-1]) if len(vv) else None
    from indicators import event_calendar
    exp_d = pd.Timestamp(expiry).date() if expiry is not None else None
    events = [e["event"] for e in event_calendar(day.date(), max(((exp_d - day.date()).days if exp_d else 7), 1))
              if e["category"] != "Derivatives expiry"] if exp_d else []
    return {"spot": float(close.iloc[-1]), "hv20": round(hv20, 2), "vix": v, "adx": round(adx, 1),
            "trend": trend, "events": events, "source": "reconstructed"}


def ctx_from_analysis(res: dict) -> dict:
    """Snapshot of an Options-tab analysis to stamp on a new fill."""
    if not res:
        return {}
    return {"spot": res.get("spot"), "atm_iv": res.get("atm_iv"), "hv20": res.get("hv20"), "vrp": res.get("vrp"),
            "vix": res.get("vix"), "dte": res.get("dte"), "score": res.get("score"), "verdict": res.get("verdict"),
            "bias": res.get("bias"), "adx": res.get("adx"), "trend": res.get("trend"),
            "sigma_move": res.get("sigma_move"), "pcr": res.get("pcr"), "max_pain": res.get("max_pain"),
            "call_wall": (res.get("call_walls") or [{}])[0].get("strike"),
            "put_wall": (res.get("put_walls") or [{}])[0].get("strike"),
            "events": [e["event"] for e in res.get("events") or []],
            "expiry": res.get("expiry"), "source": res.get("source") or "live"}


# ------------------------------------------------------------------ metrics
def metrics(strats: list[dict]) -> dict:
    cl = [s for s in strats if s["status"] == "closed"]
    nets = [s["net"] for s in cl]
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x <= 0]
    eq = np.cumsum(nets) if nets else np.array([])
    dd = float((eq - np.maximum.accumulate(np.concatenate([[0], eq]))[1:]).min()) if len(eq) else 0.0
    streak = cur = 0
    for x in nets:
        cur = cur + 1 if x <= 0 else 0
        streak = max(streak, cur)
    gp, gl = sum(wins), -sum(losses)
    return {
        "trades": len(cl), "open": sum(s["status"] == "open" for s in strats),
        "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(cl) * 100 if cl else None,
        "avg_win": float(np.mean(wins)) if wins else None, "avg_loss": float(np.mean(losses)) if losses else None,
        "payoff": float(np.mean(wins) / -np.mean(losses)) if wins and losses and np.mean(losses) else None,
        "expectancy": float(np.mean(nets)) if nets else None,
        "profit_factor": gp / gl if gl else None,
        "gross": sum(s["pnl"] for s in cl), "charges": sum(s["charges"] for s in cl), "net": sum(nets),
        "max_drawdown": dd, "max_losing_streak": streak,
        "best": max(cl, key=lambda s: s["net"]) if cl else None, "worst": min(cl, key=lambda s: s["net"]) if cl else None,
        "avg_holding_days": float(np.mean([s["holding_days"] for s in cl])) if cl else None,
    }


def equity_curve(strats: list[dict]) -> pd.DataFrame:
    cl = [s for s in strats if s["status"] == "closed"]
    if not cl:
        return pd.DataFrame(columns=["net", "cum"])
    df = pd.DataFrame({"date": [s["exit_time"].date() for s in cl], "net": [s["net"] for s in cl]})
    daily = df.groupby("date")["net"].sum().sort_index()
    return pd.DataFrame({"net": daily, "cum": daily.cumsum()})


def _bucket_dte(d):
    if d is None:
        return "unknown"
    return "0–2 d" if d <= 2 else "3–6 d" if d <= 6 else "7–14 d" if d <= 14 else "15–30 d" if d <= 30 else "30+ d"


def _bucket_vrp(c):
    v = c.get("vrp")
    if v is None:
        return "unknown"
    return "IV < RV (<0.95)" if v < 0.95 else "fair (0.95–1.15)" if v < 1.15 else "rich (≥1.15)"


def _bucket_verdict(c):
    s = c.get("score")
    if s is None:
        return "not captured"
    return "favourable (≥3)" if s >= 3 else "mixed (1–2)" if s >= 1 else "unfavourable (≤0)"


def breakdowns(strats: list[dict]) -> dict[str, list[dict]]:
    cl = [s for s in strats if s["status"] == "closed"]
    dims = {
        "Structure": lambda s: s["structure"],
        "Underlying": lambda s: s["und"],
        "Days to expiry at entry": lambda s: _bucket_dte(s["dte_entry"]),
        "Selling environment at entry": lambda s: _bucket_verdict(s["ctx"]),
        "IV vs realised vol at entry": lambda s: _bucket_vrp(s["ctx"]),
        "Risk type": lambda s: "defined risk" if s["defined_risk"] else "undefined risk",
        "Held to expiry": lambda s: "yes" if s["held_to_expiry"] else "no",
        "Entry weekday": lambda s: s["entry_time"].strftime("%a"),
        "Entry time": lambda s: ("before 09:30" if s["entry_time"].time() < datetime.strptime("09:30", "%H:%M").time()
                                 else "after 15:00" if s["entry_time"].hour >= 15 else
                                 f"{s['entry_time'].hour:02d}:00–{s['entry_time'].hour + 1:02d}:00"),
    }
    out = {}
    for name, fn in dims.items():
        g = defaultdict(list)
        for s in cl:
            g[fn(s)].append(s["net"])
        rows = [{"bucket": k, "trades": len(v), "win_rate": sum(x > 0 for x in v) / len(v) * 100,
                 "net": sum(v), "avg": sum(v) / len(v)} for k, v in g.items()]
        out[name] = sorted(rows, key=lambda r: r["net"])
    return out


# ------------------------------------------------------------------ diagnosis
def _flags(s: dict) -> list[tuple[str, str]]:
    """Per-strategy rule hits: (flag id, short label)."""
    c, out = s["ctx"], []
    carried = c.get("source") == "carry-forward"      # entry time / market context unknown
    sig = short_sigma(s)
    if c.get("score") is not None and c["score"] <= 0:
        out.append(("unfavourable", "sold in an UNFAVOURABLE environment"))
    if c.get("vrp") is not None and c["vrp"] < 0.95:
        out.append(("cheap_iv", "IV below realised vol"))
    if sig is not None and sig < 0.75 and any(l["side"] == "SHORT" for l in s["legs"]):
        out.append(("too_close", f"short strike only {sig:.2f}σ away"))
    if not carried and s["dte_entry"] is not None and s["dte_entry"] < 5 and any(l["side"] == "SHORT" for l in s["legs"]):
        out.append(("low_dte", f"entered {s['dte_entry']} days before expiry"))
    if c.get("events"):
        out.append(("event", "event before expiry: " + ", ".join(c["events"][:2])))
    if s["structure"] in NEUTRAL and (c.get("adx") or 0) >= 28:
        out.append(("trend", f"neutral structure in a trending market (ADX {c['adx']:.0f})"))
    if s["structure"] in ("Bull put spread", "Naked short put") and c.get("bias") == "bearish" or \
       s["structure"] in ("Bear call spread", "Naked short call") and c.get("bias") == "bullish":
        out.append(("against_bias", f"{s['structure']} against a {c['bias']} read"))
    if s["loss_x_credit"] >= 2:
        out.append(("no_stop", f"loss reached {s['loss_x_credit']:.1f}× the credit"))
    if not s["defined_risk"] and any(l["side"] == "SHORT" for l in s["legs"]):
        out.append(("naked", "undefined risk"))
    if s["held_to_expiry"] and any(l["side"] == "SHORT" for l in s["legs"]):
        out.append(("expiry_hold", "held short options into expiry day"))
    if not carried and s["entry_time"].time() < datetime.strptime("09:30", "%H:%M").time():
        out.append(("opening", "entered in the first 15 minutes"))
    return out


_RULES = {
    "unfavourable": ("Selling when the environment was UNFAVOURABLE",
                     "The Options tab scored the setup ≤ 0 (cheap premium, events, trend, low DTE…) and you sold anyway.",
                     "Only sell when the environment reads FAVOURABLE, or cut size to a third when it is MIXED."),
    "cheap_iv": ("Selling cheap premium (IV below realised vol)",
                 "Options were pricing less movement than the index was actually making, so the premium did not "
                 "pay for the risk.",
                 "Wait for IV ÷ realised vol ≥ 1.15, or switch to defined-risk spreads / smaller size."),
    "too_close": ("Short strikes too close to spot",
                  "Strikes inside ~0.75σ of the expected move get tested often; one adverse day turns them into losses.",
                  "Sell around 1σ (≈ 0.16 delta) and beyond the nearest OI wall / S-R zone."),
    "low_dte": ("Entering with fewer than 5 days to expiry",
                "Close to expiry gamma is high: small index moves swing short options violently.",
                "Positional selling works best 7–45 days out; exit or roll 1–2 days before expiry."),
    "event": ("Holding through known events",
              "RBI / FOMC / budget days gap the index and spike IV — exactly what hurts a short-premium book.",
              "Close or hedge before the event, or wait for IV to pop and sell after it."),
    "trend": ("Neutral structures in a trending market",
              "Strangles and condors need a range; with ADX ≥ 28 one side keeps getting pushed.",
              "In trends use a one-sided credit spread in the direction of the trend, or stay out."),
    "against_bias": ("Directional spreads against the directional read",
                     "You sold the side the trend / PCR / OI change was pointing toward.",
                     "Sell puts in bullish reads and calls in bearish ones — never the other way round."),
    "no_stop": ("Letting losers run past the stop (≥ 2× credit)",
                "A seller's edge is many small wins; one loss of 2–5× the credit wipes out several of them.",
                "Exit at 1.5× credit or on a daily close beyond the short strike — no averaging, no hoping."),
    "naked": ("Undefined-risk positions",
              "Naked strangles / short options have no cap: gap days produce the largest losses.",
              "Buy wings (iron condor / credit spreads); the small cost caps the tail."),
    "expiry_hold": ("Holding short options into expiry day",
                    "The last session carries the most gamma for the least remaining premium.",
                    "Book or roll 1–2 sessions before expiry once ~50–70% of the credit is captured."),
    "opening": ("Entering in the first 15 minutes",
                "Opening prints are noisy: wide spreads and false moves.",
                "Let the first 15–30 minutes settle before selling premium."),
    "revenge": ("Re-entering quickly after a loss (revenge trading)",
                "A new position within an hour of closing a loser is usually emotion, not a setup.",
                "After a loss, step away for the session or until the checklist is green again."),
    "overtrading": ("Too many new positions on one day",
                    "Days with more than 3 new strategies concentrated risk and lowered selectivity.",
                    "Cap yourself at 1–2 well-chosen positions a day."),
}


def diagnose(strats: list[dict], capital: float | None = None) -> list[dict]:
    """Findings ranked by how many rupees the pattern cost you."""
    cl = [s for s in strats if s["status"] == "closed"]
    if not cl:
        return []
    hits: dict[str, list[dict]] = defaultdict(list)
    for s in cl:
        for fid, _ in _flags(s):
            hits[fid].append(s)
    # behaviour rules that need the sequence of trades
    by_exit = sorted(cl, key=lambda s: s["exit_time"])
    for s in cl:
        prior = [p for p in by_exit if p["net"] < 0 and p["exit_time"] <= s["entry_time"]
                 and (s["entry_time"] - p["exit_time"]).total_seconds() <= 3600 and p is not s]
        if prior:
            hits["revenge"].append(s)
    per_day = defaultdict(list)
    for s in cl:
        per_day[s["entry_date"]].append(s)
    for d, ss in per_day.items():
        if len(ss) > 3:
            hits["overtrading"].extend(ss)

    findings = []
    total = len(cl)
    for fid, ss in hits.items():
        title, why, fix = _RULES[fid]
        ids = {s["id"] for s in ss}
        others = [s for s in cl if s["id"] not in ids]
        net = sum(s["net"] for s in ss)
        lost = sum(s["net"] for s in ss if s["net"] < 0)
        wr = sum(s["net"] > 0 for s in ss) / len(ss) * 100
        wr_o = sum(s["net"] > 0 for s in others) / len(others) * 100 if others else None
        findings.append({"id": fid, "title": title, "why": why, "fix": fix, "trades": len(ss),
                         "share": len(ss) / total * 100, "net": net, "lost": lost, "win_rate": wr,
                         "win_rate_others": wr_o, "avg": net / len(ss),
                         "avg_others": (sum(s["net"] for s in others) / len(others)) if others else None,
                         "examples": [s["id"] for s in sorted(ss, key=lambda s: s["net"])[:3]]})

    # whole-book patterns
    m = metrics(strats)
    if m["payoff"] is not None and m["win_rate"] is not None and m["payoff"] < 0.5 and m["net"] < 0:
        findings.append({"id": "payoff", "title": "Small wins, big losses",
                         "why": f"Average win ₹{m['avg_win']:,.0f} vs average loss ₹{-m['avg_loss']:,.0f} "
                                f"(payoff {m['payoff']:.2f}). At a {m['win_rate']:.0f}% win rate you need a payoff "
                                f"of at least {(100 - m['win_rate']) / max(m['win_rate'], 1):.2f} to break even.",
                         "fix": "Cap losses at 1.5× credit and let winners reach ~50% of credit before booking.",
                         "trades": m["trades"], "share": 100, "net": m["net"], "lost": m["net"],
                         "win_rate": m["win_rate"], "win_rate_others": None, "avg": m["expectancy"],
                         "avg_others": None, "examples": [m["worst"]["id"]] if m["worst"] else []})
    winners = [s for s in cl if s["net"] > 0 and s["captured"] is not None]
    if winners and np.mean([s["captured"] for s in winners]) < 0.3 and m["net"] < 0:
        cap = np.mean([s["captured"] for s in winners]) * 100
        findings.append({"id": "early_profit", "title": "Booking winners too early",
                         "why": f"Winning trades kept only {cap:.0f}% of the credit on average, while losers ran.",
                         "fix": "Target ~50% of credit on winners; tighten the loss side instead.",
                         "trades": len(winners), "share": len(winners) / total * 100, "net": sum(s["net"] for s in winners),
                         "lost": 0, "win_rate": 100, "win_rate_others": None, "avg": None, "avg_others": None,
                         "examples": []})
    if m["gross"] > 0 and m["charges"] > 0.25 * m["gross"]:
        findings.append({"id": "charges", "title": "Charges eating the profits",
                         "why": f"Estimated charges ₹{m['charges']:,.0f} are {m['charges'] / m['gross'] * 100:.0f}% of "
                                "gross P&L — many small, frequent trades.",
                         "fix": "Fewer, larger-premium trades; avoid closing tiny residual premiums.",
                         "trades": m["trades"], "share": 100, "net": -m["charges"], "lost": -m["charges"],
                         "win_rate": m["win_rate"], "win_rate_others": None, "avg": None, "avg_others": None,
                         "examples": []})
    if capital:
        big = [s for s in cl if s["net"] < -0.02 * capital]
        if big:
            findings.append({"id": "sizing", "title": "Single losses above 2% of capital",
                             "why": f"{len(big)} trade(s) lost more than 2% of ₹{capital:,.0f} each — position size "
                                    "was too large for the risk.",
                             "fix": "Size so that the maximum (or stop) loss of any one position is ≤ 1–2% of capital.",
                             "trades": len(big), "share": len(big) / total * 100, "net": sum(s["net"] for s in big),
                             "lost": sum(s["net"] for s in big), "win_rate": 0, "win_rate_others": None,
                             "avg": None, "avg_others": None, "examples": [s["id"] for s in big[:3]]})
    findings.sort(key=lambda f: f["lost"])
    return findings


# ------------------------------------------------------------------ open positions
def manage_positions(rows: list[dict], spot: dict[str, float], sigma: dict[str, float] | None = None,
                     today: date | None = None) -> list[dict]:
    """Group live PositionBook rows by underlying + expiry and apply the
    management plan: book at 50% of credit, stop at 1.5× credit, short strike
    breached, expiry approaching."""
    today = today or date.today()
    groups = defaultdict(list)
    for r in rows:
        try:
            q = int(float(r.get("netqty") or 0))
        except ValueError:
            continue
        p = parse_tsym(r.get("tsym", ""))
        lp = float(r.get("lp") or 0)
        avg = float(r.get("netavgprc") or r.get("upldprc") or 0)
        rp, um = float(r.get("rpnl") or 0), float(r.get("urmtom") or 0)
        groups[(p["und"], p["expiry"])].append({**p, "tsym": r.get("tsym"), "qty": q, "avg": avg, "lp": lp,
                                                "rpnl": rp, "mtm": um, "token": str(r.get("token", "")),
                                                "exch": r.get("exch", ""), "prd": r.get("prd", "")})
    out = []
    for (und, exp), legs in groups.items():
        live = [l for l in legs if l["qty"]]
        credit = sum(-l["qty"] * l["avg"] for l in live)            # premium received for net shorts
        value = sum(-l["qty"] * l["lp"] for l in live)              # cost to close now
        open_pnl = credit - value
        realised = sum(l["rpnl"] for l in legs)
        sp = spot.get(und)
        alerts = []
        dte = (exp - today).days if exp else None
        if credit > 0 and live:
            cap = open_pnl / credit
            if cap >= 0.5:
                alerts.append(("book", f"{cap * 100:.0f}% of the credit captured — book profit (plan: ~50%)."))
            if open_pnl <= -1.5 * credit:
                alerts.append(("stop", f"Loss is {-open_pnl / credit:.1f}× the credit — plan says exit at 1.5×."))
        if sp:
            # the range is defined by the outermost short strike on each side
            sg = (sigma or {}).get(und)
            for kind, outer in (("CE", max), ("PE", min)):
                shorts = [l for l in live if l["qty"] < 0 and l["kind"] == kind and l["strike"]]
                if not shorts:
                    continue
                k = outer(l["strike"] for l in shorts)
                beyond = sp >= k if kind == "CE" else sp <= k
                if beyond:
                    alerts.append(("stop", f"Spot {sp:,.0f} is through the outermost short {k:,.0f} {kind} — the "
                                           "range is broken."))
                elif sg and abs(sp - k) < 0.25 * sg:
                    alerts.append(("warn", f"Spot {sp:,.0f} is within 0.25σ ({abs(sp - k):,.0f} pts) of the "
                                           f"outermost short {k:,.0f} {kind}."))
        if dte is not None and live and any(l["qty"] < 0 for l in live) and dte <= 1:
            alerts.append(("warn", f"{'Expiry today' if dte == 0 else 'Expiry tomorrow'} — close or roll to avoid "
                                   "expiry-day gamma."))
        structure, defined = classify([{"side": "SHORT" if l["qty"] < 0 else "LONG", "kind": l["kind"],
                                        "strike": l["strike"], "qty": abs(l["qty"])} for l in live]) if live else ("Closed today", True)
        out.append({"und": und, "expiry": exp, "dte": dte, "structure": structure, "defined_risk": defined,
                    "legs": legs, "credit": credit, "open_pnl": open_pnl, "realised": realised,
                    "mtm": sum(l["mtm"] for l in legs) + realised, "captured": (open_pnl / credit) if credit > 0 else None,
                    "alerts": alerts, "spot": sp})
    out.sort(key=lambda g: (g["expiry"] or date.max, g["und"]))
    return out
