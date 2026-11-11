"""Option premiums of two expiries side by side (NIFTY / BANKNIFTY) — deliberately minimal.

For the strikes around ATM: call and put premium (LTP) in the near and the far expiry and the difference
(far − near, i.e. what the extra time costs).  Plus each expiry's days to expiry and ATM straddle.
Used by the Mac's "Two-expiry premiums" screen and /api/expiry_compare (the phones).  Blocking.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

INDICES = ("NIFTY", "BANKNIFTY")


def _chain(opts, und: str, exp: str | None) -> dict:
    latest = opts.fresh(und, 60)
    if latest and (exp is None or latest["expiry"] == exp):
        return latest
    return opts.analyse(und, exp, record=False)


def compare(opts, und: str, e1: str | None = None, e2: str | None = None, n: int = 10) -> dict:
    """und: NIFTY / BANKNIFTY; e1, e2: expiries (default: the nearest two); n strikes each side of ATM."""
    und = "BANKNIFTY" if "BANK" in (und or "").upper() else "NIFTY"
    expiries = opts._expiry_list(und)
    e1 = e1 if e1 in expiries else (expiries[0] if expiries else None)
    e2 = e2 if e2 in expiries and e2 != e1 else next((e for e in expiries if e != e1), None)
    if not e1 or not e2:
        raise RuntimeError(f"{und}: need two expiries")
    c1, c2 = _chain(opts, und, e1), _chain(opts, und, e2)
    spot, atm = c1["spot"], c1["atm"]
    r1 = {r["strike"]: r for r in c1.get("chain") or []}
    r2 = {r["strike"]: r for r in c2.get("chain") or []}
    strikes = sorted(set(r1) & set(r2))
    if not strikes:
        raise RuntimeError(f"{und}: no common strikes")
    i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - spot))
    rows = []
    for k in strikes[max(i - n, 0): i + n + 1]:
        a, b = r1[k], r2[k]
        ce1, ce2, pe1, pe2 = (a.get("ce_ltp") or None, b.get("ce_ltp") or None, a.get("pe_ltp") or None,
                              b.get("pe_ltp") or None)
        rows.append({"strike": k, "ce1": ce1, "ce2": ce2, "ce_diff": (ce2 - ce1) if ce1 and ce2 else None,
                     "pe1": pe1, "pe2": pe2, "pe_diff": (pe2 - pe1) if pe1 and pe2 else None})

    def straddle(rs: dict) -> float | None:
        r = rs.get(atm)
        return (r["ce_ltp"] + r["pe_ltp"]) if r and r.get("ce_ltp") and r.get("pe_ltp") else None

    return {"und": und, "spot": spot, "atm": atm, "expiries": expiries[:8], "lot": c1.get("lot"),
            "e1": {"expiry": e1, "dte": c1.get("dte"), "straddle": straddle(r1), "atm_iv": c1.get("atm_iv")},
            "e2": {"expiry": e2, "dte": c2.get("dte"), "straddle": straddle(r2), "atm_iv": c2.get("atm_iv")},
            "rows": rows, "source": c1.get("source"), "as_of": str(c1.get("timestamp"))}
