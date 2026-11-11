"""Option premiums of two expiries side by side (NIFTY / BANKNIFTY) — deliberately minimal.

Two views:
  * range (default) — every strike within ±RANGE points of spot (BANKNIFTY ±6,000, NIFTY ±2,000) plus any of your
          position strikes outside it, each with what you hold there (e.g. "−240 CE 23-Nov")
  * mine  — only your position strikes plus ATM

For each strike: call and put premium (LTP) in the near and the far expiry and the difference (far − near, i.e.
what the extra time costs — roughly the credit for rolling a short leg out at the same strike).  Plus each
expiry's days to expiry and ATM straddle.  Premiums come from NSE's full chain (every strike), overlaid with the
Shoonya chain's fresher prices where it has them — Shoonya only streams strikes near the money, so deep in- or
out-of-the-money position strikes would otherwise be missing.  Used by the Mac's "Two-expiry premiums" screen and /api/expiry_compare.  Blocking.
"""
from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger(__name__)

INDICES = ("NIFTY", "BANKNIFTY")
THIN_VOL = 20          # contracts traded today below which a premium is marked thin (the last price may be old)
RANGE = {"NIFTY": 2000, "BANKNIFTY": 6000}       # points either side of spot in the range view
_nse_cache: dict = {}


def _chain(opts, und: str, exp: str | None) -> dict:
    latest = opts.fresh(und, 60)
    if latest and (exp is None or latest["expiry"] == exp):
        return latest
    return opts.analyse(und, exp, record=False)


def _nse_full(opts, und: str, exp: str) -> dict[float, dict]:
    """Every strike's LTP and volume for one expiry from NSE (cached 60 s)."""
    import time
    key = (und, exp)
    hit = _nse_cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    out: dict[float, dict] = {}
    try:
        js, _e, _x = opts.svc.option_chain(und, exp)
        for r in (js.get("records") or {}).get("data") or []:
            k = r.get("strikePrice")
            if k is None:
                continue
            row = out.setdefault(float(k), {})
            for side in ("CE", "PE"):
                o = r.get(side) or {}
                if o:
                    row[f"{side.lower()}_ltp"] = o.get("lastPrice") or None
                    row[f"{side.lower()}_vol"] = o.get("totalTradedVolume") or 0
    except Exception as exc:
        log.info("expiry compare: NSE chain %s %s: %s", und, exp, exc)
    _nse_cache[key] = (time.time(), out)
    return out


def _prices(opts, c: dict, und: str) -> dict[float, dict]:
    """NSE's full chain, overlaid with this chain's own prices (fresher when it came from Shoonya)."""
    full = {} if c.get("source") == "nse" else _nse_full(opts, und, c["expiry"])
    rows = {k: dict(v) for k, v in full.items()}
    for k, v in (c.get("strikes_all") or {}).items():
        if v.get("ce_ltp") or v.get("pe_ltp"):
            rows.setdefault(float(k), {}).update({f: v.get(f) for f in ("ce_ltp", "pe_ltp", "ce_vol", "pe_vol")
                                                  if v.get(f) is not None})
    for r in c.get("chain") or []:                     # the ±40-strike view has the freshest fields
        rows.setdefault(float(r["strike"]), {}).update({f: r.get(f) for f in ("ce_ltp", "pe_ltp", "ce_vol", "pe_vol")
                                                        if r.get(f)})
    return rows


def my_legs(trades_data: dict | None, und: str) -> list[dict]:
    """Open option legs on `und` from the Trades data: strike, kind, qty, expiry (dd-Mon-yyyy)."""
    out = []
    for g in (trades_data or {}).get("positions") or []:
        if g.get("und") != und:
            continue
        for l in g.get("legs") or []:
            if l.get("qty") and l.get("kind") in ("CE", "PE") and l.get("strike"):
                exp = pd.Timestamp(l.get("expiry") or g.get("expiry")).strftime("%d-%b-%Y") \
                    if (l.get("expiry") or g.get("expiry")) is not None else None
                out.append({"strike": float(l["strike"]), "kind": l["kind"], "qty": int(float(l["qty"])),
                            "expiry": exp, "avg": float(l.get("avg") or 0)})
    return out


def _leg_left(l: dict, e1: str, e2: str, ce1, ce2, pe1, pe2, k: float, tv, intrinsic, acc: dict) -> dict:
    """One held leg with its time value left; legs in the near expiry add to the book totals in `acc`."""
    where = "e1" if l["expiry"] == e1 else "e2" if l["expiry"] == e2 else None
    near, far = (ce1, ce2) if l["kind"] == "CE" else (pe1, pe2)
    out = {"kind": l["kind"], "qty": l["qty"], "expiry": l["expiry"], "avg": l["avg"], "in": where,
           "intrinsic": round(intrinsic(l["kind"], k), 2)}
    if where == "e1":
        t1, t2 = tv(near, l["kind"], k), tv(far, l["kind"], k)
        if t1 is None:
            acc["missing"] += 1
        else:
            rs = abs(l["qty"]) * t1
            out.update(tv_left=round(t1, 2), tv_left_rs=round(rs), far_tv=None if t2 is None else round(t2, 2))
            acc["legs"] += 1
            if l["qty"] < 0:
                acc["short_rs"] += rs                                     # still to collect as it decays
                acc["far_short_rs"] += abs(l["qty"]) * (t2 or 0)          # what the same strikes carry in the far expiry
            else:
                acc["long_rs"] += rs                                      # still to lose as it decays
    return out


def compare(opts, und: str, e1: str | None = None, e2: str | None = None, n: int = 10,
            legs: list[dict] | None = None, mode: str | None = None) -> dict:
    """und: NIFTY / BANKNIFTY; e1, e2: expiries (default: your positions' expiry and the next one, else the nearest
    two); legs: your open legs (my_legs); mode "range" (default: ±RANGE points plus your strikes) or "mine"
    (your strikes plus ATM).  `n` is kept for older callers and ignored."""
    und = "BANKNIFTY" if "BANK" in (und or "").upper() else "NIFTY"
    expiries = opts._expiry_list(und)
    legs = legs or []
    mode = "mine" if mode == "mine" and legs else "range"
    held_exps = [e for e in expiries if any(l["expiry"] == e for l in legs)]
    if e1 not in expiries:
        e1 = held_exps[0] if held_exps else (expiries[0] if expiries else None)
    if e2 not in expiries or e2 == e1:
        later = [e for e in expiries if pd.Timestamp(e) > pd.Timestamp(e1)] if e1 else []
        e2 = later[0] if later else next((e for e in expiries if e != e1), None)
    if not e1 or not e2:
        raise RuntimeError(f"{und}: need two expiries")
    c1, c2 = _chain(opts, und, e1), _chain(opts, und, e2)
    spot, atm = c1["spot"], c1["atm"]
    p1, p2 = _prices(opts, c1, und), _prices(opts, c2, und)
    mine_k = {l["strike"] for l in legs}
    if mode == "mine":
        strikes = sorted(mine_k | {atm})
    else:
        r = RANGE[und]
        window = {k for k in set(p1) | set(p2) if abs(k - spot) <= r
                  and any((p.get(k) or {}).get(f) for p in (p1, p2) for f in ("ce_ltp", "pe_ltp"))}
        strikes = sorted(window | mine_k | {atm})

    def px(rows, k, f):
        v = (rows.get(k) or {}).get(f)
        return float(v) if v else None

    def intrinsic(kind: str, k: float) -> float:
        return max(spot - k, 0.0) if kind == "CE" else max(k - spot, 0.0)

    def tv(price: float | None, kind: str, k: float) -> float | None:
        """Time value = premium − intrinsic value (what can still decay before expiry).  None when the price is
        below intrinsic value by more than 0.5% — an old last price, not a real quote."""
        if price is None:
            return None
        iv = intrinsic(kind, k)
        if price < iv * 0.995 - 1:
            return None
        return max(price - iv, 0.0)

    rows = []
    left = {"short_rs": 0.0, "long_rs": 0.0, "far_short_rs": 0.0, "legs": 0, "missing": 0}
    for k in strikes:
        ce1, ce2, pe1, pe2 = px(p1, k, "ce_ltp"), px(p2, k, "ce_ltp"), px(p1, k, "pe_ltp"), px(p2, k, "pe_ltp")
        mine = [l for l in legs if l["strike"] == k]
        thin = [f for f, rs, side in (("ce1", p1, "ce"), ("ce2", p2, "ce"), ("pe1", p1, "pe"), ("pe2", p2, "pe"))
                if (rs.get(k) or {}).get(f"{side}_ltp") and ((rs.get(k) or {}).get(f"{side}_vol") or 0) < THIN_VOL]
        rows.append({"strike": k, "ce1": ce1, "ce2": ce2, "ce_diff": (ce2 - ce1) if ce1 and ce2 else None,
                     "pe1": pe1, "pe2": pe2, "pe_diff": (pe2 - pe1) if pe1 and pe2 else None,
                     "atm": k == atm, "no_trades": thin,
                     "mine": [_leg_left(l, e1, e2, ce1, ce2, pe1, pe2, k, tv, intrinsic, left) for l in mine],
                     "tv": {"ce1": tv(ce1, "CE", k), "ce2": tv(ce2, "CE", k), "pe1": tv(pe1, "PE", k),
                            "pe2": tv(pe2, "PE", k)},
                     "mine_text": ", ".join(f"{l['qty']:+d} {l['kind']} {str(l['expiry'] or '')[:6]}" for l in mine)})

    def straddle(rs: dict) -> float | None:
        r = rs.get(atm) or {}
        return (r["ce_ltp"] + r["pe_ltp"]) if r.get("ce_ltp") and r.get("pe_ltp") else None

    dte1 = max(float(c1.get("dte") or 0), 0.5)
    net = left["short_rs"] - left["long_rs"]
    summary = None
    if left["legs"]:
        summary = {"expiry": e1, "dte": c1.get("dte"), "short_rs": round(left["short_rs"]),
                   "long_rs": round(left["long_rs"]), "net_rs": round(net), "per_day_rs": round(net / dte1),
                   "far_expiry": e2, "far_short_rs": round(left["far_short_rs"]),
                   "roll_extra_rs": round(left["far_short_rs"] - left["short_rs"]),
                   "legs": left["legs"], "missing": left["missing"]}
    return {"und": und, "spot": spot, "atm": atm, "expiries": expiries[:8], "lot": c1.get("lot"), "mode": mode,
            "range_pts": RANGE[und],
            "has_positions": bool(legs), "time_value_left": summary,
            "e1": {"expiry": e1, "dte": c1.get("dte"), "straddle": straddle(p1), "atm_iv": c1.get("atm_iv")},
            "e2": {"expiry": e2, "dte": c2.get("dte"), "straddle": straddle(p2), "atm_iv": c2.get("atm_iv")},
            "rows": rows, "source": c1.get("source"), "as_of": str(c1.get("timestamp"))}
