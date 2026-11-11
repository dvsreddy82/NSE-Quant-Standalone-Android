"""Option premiums of two expiries side by side (NIFTY / BANKNIFTY) — deliberately minimal.

Two views:
  * mine  (default when you hold positions on the index) — the strikes of your open positions plus ATM, each with
          what you hold there (e.g. "−240 CE 23-Nov"), so near vs far premiums show exactly where your risk is
  * atm   the strikes around ATM

For each strike: call and put premium (LTP) in the near and the far expiry and the difference (far − near, i.e.
what the extra time costs — roughly the credit for rolling a short leg out at the same strike).  Plus each
expiry's days to expiry and ATM straddle.  Premiums come from every strike of the chain, so far-from-ATM
position strikes are priced too.  Used by the Mac's "Two-expiry premiums" screen and /api/expiry_compare.  Blocking.
"""
from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger(__name__)

INDICES = ("NIFTY", "BANKNIFTY")
THIN_VOL = 20          # contracts traded today below which a premium is marked thin (the last price may be old)


def _chain(opts, und: str, exp: str | None) -> dict:
    latest = opts.fresh(und, 60)
    if latest and (exp is None or latest["expiry"] == exp):
        return latest
    return opts.analyse(und, exp, record=False)


def _prices(c: dict) -> dict[float, dict]:
    rows = {float(k): v for k, v in (c.get("strikes_all") or {}).items()}
    for r in c.get("chain") or []:                     # the ±40-strike view has the freshest fields
        rows.setdefault(float(r["strike"]), {}).update({"ce_ltp": r.get("ce_ltp"), "pe_ltp": r.get("pe_ltp"),
                                                        "ce_vol": r.get("ce_vol"), "pe_vol": r.get("pe_vol")})
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


def compare(opts, und: str, e1: str | None = None, e2: str | None = None, n: int = 10,
            legs: list[dict] | None = None, mode: str | None = None) -> dict:
    """und: NIFTY / BANKNIFTY; e1, e2: expiries (default: your positions' expiry and the next one, else the nearest
    two); n strikes each side of ATM for mode "atm"; legs: your open legs (my_legs) for mode "mine"."""
    und = "BANKNIFTY" if "BANK" in (und or "").upper() else "NIFTY"
    expiries = opts._expiry_list(und)
    legs = legs or []
    mode = mode if mode in ("mine", "atm") else ("mine" if legs else "atm")
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
    p1, p2 = _prices(c1), _prices(c2)
    common = sorted(set(p1) & set(p2))
    if not common:
        raise RuntimeError(f"{und}: no common strikes")
    if mode == "mine" and legs:
        want = sorted({l["strike"] for l in legs} | {atm})
        strikes = [k for k in want if k in p1 or k in p2]
    else:
        i = min(range(len(common)), key=lambda j: abs(common[j] - spot))
        strikes = common[max(i - n, 0): i + n + 1]

    def px(rows, k, f):
        v = (rows.get(k) or {}).get(f)
        return float(v) if v else None

    rows = []
    for k in strikes:
        ce1, ce2, pe1, pe2 = px(p1, k, "ce_ltp"), px(p2, k, "ce_ltp"), px(p1, k, "pe_ltp"), px(p2, k, "pe_ltp")
        mine = [l for l in legs if l["strike"] == k]
        thin = [f for f, rs, side in (("ce1", p1, "ce"), ("ce2", p2, "ce"), ("pe1", p1, "pe"), ("pe2", p2, "pe"))
                if (rs.get(k) or {}).get(f"{side}_ltp") and ((rs.get(k) or {}).get(f"{side}_vol") or 0) < THIN_VOL]
        rows.append({"strike": k, "ce1": ce1, "ce2": ce2, "ce_diff": (ce2 - ce1) if ce1 and ce2 else None,
                     "pe1": pe1, "pe2": pe2, "pe_diff": (pe2 - pe1) if pe1 and pe2 else None,
                     "atm": k == atm, "no_trades": thin,
                     "mine": [{"kind": l["kind"], "qty": l["qty"], "expiry": l["expiry"], "avg": l["avg"],
                               "in": "e1" if l["expiry"] == e1 else "e2" if l["expiry"] == e2 else None}
                              for l in mine],
                     "mine_text": ", ".join(f"{l['qty']:+d} {l['kind']} {str(l['expiry'] or '')[:6]}" for l in mine)})

    def straddle(rs: dict) -> float | None:
        r = rs.get(atm) or {}
        return (r["ce_ltp"] + r["pe_ltp"]) if r.get("ce_ltp") and r.get("pe_ltp") else None

    return {"und": und, "spot": spot, "atm": atm, "expiries": expiries[:8], "lot": c1.get("lot"), "mode": mode,
            "has_positions": bool(legs),
            "e1": {"expiry": e1, "dte": c1.get("dte"), "straddle": straddle(p1), "atm_iv": c1.get("atm_iv")},
            "e2": {"expiry": e2, "dte": c2.get("dte"), "straddle": straddle(p2), "atm_iv": c2.get("atm_iv")},
            "rows": rows, "source": c1.get("source"), "as_of": str(c1.get("timestamp"))}
