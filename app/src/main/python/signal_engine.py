"""Rules-based market classification from the PCR read plus independent confirmation (methodology §11).

Classes: Strong Bullish, Bullish, Neutral, Bearish, Strong Bearish, No Trade.

Evidence — five items, each from a different source, each counted once with **equal weight**:
  1 PCR read      the methodology's table: weighted near-ATM PCR trend × 15-min OI flow (premium-based writing vs
                  buying, so "option premium changes" are already inside it and are not counted again)
  2 Spot          spot change over the last 15 minutes (≥ DIRECTION_PCT)
  3 Futures       near-month future today: price vs previous close and OI vs previous OI (long / short build-up,
                  short covering, long unwinding) — needs Shoonya; missing otherwise
  4 IV            ATM IV over 15 minutes against spot (IV up while spot falls = demand for protection, bearish;
                  IV down while spot rises = orderly rise, bullish; anything else neutral)
  5 Volume        option volume in the last 15 minutes vs the session's average 15 minutes — participation, so it
                  confirms whichever direction the others give but never sets one

Rules:
  * No Trade — any data gate fails (chain older than MAX_CHAIN_AGE in market hours, no PCR read yet, fewer than
    15 minutes of clean snapshots), or directional evidence conflicts (at least one bullish and one bearish item).
  * The PCR read anchors the direction (this is a PCR system).  With a bullish PCR read and no bearish item:
      Strong Bullish  PCR read is the confirmed table row ("Bullish lean") and at least two of spot / futures / IV
                      agree and volume is expanding
      Bullish         at least one of spot / futures / IV agrees
      Neutral         nothing confirms it (an unconfirmed lean is not a trade signal)
    Bearish mirrors this.  A "Potentially sideways" read with flat spot and no futures build-up either way is Neutral.
    "Mixed" / "No read" is No Trade.
  * Confidence (0–100) = 100 × (evidence items agreeing with the class) ÷ 5.  Items with no data count as not
    agreeing, so missing data lowers confidence.  For Neutral, an item agrees when it is neutral.  No Trade has
    confidence 0.  Equal weights are deliberate: nothing has been backtested yet, so no item is assumed to matter
    more.  The number is the share of evidence in agreement, **not a probability**; it should be calibrated against
    outcomes (methodology §8) before being treated as one.
"""
from __future__ import annotations

import time

import config
import pcr_board

MAX_CHAIN_AGE = 300            # seconds; older chains in market hours → No Trade
IV_PTS = 0.5                   # ATM IV change that counts (vol points)
VOL_EXPAND = 1.2               # last-15-min option volume vs the session's average 15 minutes
N_EVIDENCE = 5
CLASSES = ("Strong Bullish", "Bullish", "Neutral", "Bearish", "Strong Bearish", "No Trade")


def _atm_iv(rows: dict, spot: float) -> float | None:
    if not rows:
        return None
    k = min(rows, key=lambda x: abs(x - spot))
    v = [x for x in (rows[k].get("ce_iv"), rows[k].get("pe_iv")) if x]
    return sum(v) / len(v) if v else None


def _vol(rows: dict) -> float | None:
    vals = [(r.get("ce_vol"), r.get("pe_vol")) for r in rows.values()]
    if any(a is None or b is None for a, b in vals):
        return None
    return sum(a + b for a, b in vals)


def classify(res: dict, history: list[dict], fut: dict | None = None, now: float | None = None,
             market_open: bool = True) -> dict:
    now = now or time.time()
    board = res.get("pcr_board") or {}
    spot, exp = res["spot"], res["expiry"]
    cur_rows = res.get("strikes_all") or {r["strike"]: r for r in res.get("chain") or []}
    hist = pcr_board.clean(history, exp, cur_rows)
    snap15 = pcr_board._pick(hist, exp, now - 900, 360)
    ev: list[dict] = []

    def add(key, name, vote, text, available=True):
        ev.append({"key": key, "name": name, "vote": vote, "text": text, "available": available})

    # 1 PCR read
    read = board.get("read") or "No read"
    pv = {"Bullish lean": 1, "Potentially bullish": 1, "Bearish lean": -1, "Potentially bearish": -1}.get(read, 0)
    add("pcr", "PCR read", pv, f"{read} — {board.get('why', '')}", read not in ("No read",))
    # 2 spot over 15 minutes
    if snap15:
        mv = (spot - snap15["spot"]) / snap15["spot"] * 100
        sv = 1 if mv >= config.DIRECTION_PCT else -1 if mv <= -config.DIRECTION_PCT else 0
        add("spot", "Spot (15 min)", sv, f"{snap15['spot']:,.1f} → {spot:,.1f} ({mv:+.2f}%)")
    else:
        mv = None
        add("spot", "Spot (15 min)", 0, "no snapshot from 15 minutes ago", False)
    # 3 futures build-up today
    if fut and fut.get("lp") and fut.get("c") and fut.get("oi") and fut.get("poi"):
        lp, c, oi, poi = (float(fut[k]) for k in ("lp", "c", "oi", "poi"))
        dp, doi = (lp - c) / c * 100, (oi - poi) / poi * 100 if poi else 0.0
        if abs(dp) < config.FUT_PRICE_PCT or abs(doi) < config.FUT_OI_PCT:
            fv, what = 0, "no clear build-up"
        elif dp > 0 and doi > 0:
            fv, what = 1, "long build-up"
        elif dp < 0 and doi > 0:
            fv, what = -1, "short build-up"
        elif dp > 0:
            fv, what = 1, "short covering"
        else:
            fv, what = -1, "long unwinding"
        add("fut", "Futures (today)", fv, f"price {dp:+.2f}%, OI {doi:+.1f}% — {what}")
    else:
        add("fut", "Futures (today)", 0, "no futures data (needs Shoonya)", False)
    # 4 ATM IV vs spot over 15 minutes
    iv_now = _atm_iv(cur_rows, spot)
    iv_then = _atm_iv(snap15["rows"], snap15["spot"]) if snap15 else None
    if iv_now is not None and iv_then is not None and mv is not None:
        d = iv_now - iv_then
        if d >= IV_PTS and mv < 0:
            iv_v, what = -1, "IV up while spot fell — protection demand"
        elif d <= -IV_PTS and mv > 0:
            iv_v, what = 1, "IV down while spot rose — orderly rise"
        else:
            iv_v, what = 0, "no directional IV signal"
        add("iv", "ATM IV (15 min)", iv_v, f"{iv_then:.1f}% → {iv_now:.1f}% — {what}")
    else:
        add("iv", "ATM IV (15 min)", 0, "IV history not available", False)
    # 5 volume expansion (participation)
    v_now = _vol(cur_rows)
    v15 = _vol(snap15["rows"]) if snap15 else None
    first = min(hist, key=lambda h: h["ts"]) if hist else None
    v0 = _vol(first["rows"]) if first else None
    expanding = None
    if None not in (v_now, v15, v0) and first and now - first["ts"] > 1800:
        last15 = v_now - v15
        avg15 = (v_now - v0) / ((now - first["ts"]) / 900)
        if avg15 > 0 and last15 >= 0:
            expanding = last15 >= VOL_EXPAND * avg15
            add("vol", "Option volume (15 min)", 0, f"{last15 / avg15:.2f}× the session's average 15 minutes — "
                                                    f"{'expanding' if expanding else 'not expanding'}")
    if expanding is None:
        add("vol", "Option volume (15 min)", 0, "not enough volume history", False)

    # ---- gates
    reasons = []
    age = now - float(res.get("as_of_ts") or now)
    if market_open and res.get("source") == "offline":
        reasons.append("using a stored chain — no live data")
    if market_open and age > MAX_CHAIN_AGE:
        reasons.append(f"chain is {age / 60:.0f} min old")
    if not hist or (now - min(h["ts"] for h in hist)) < 900:
        reasons.append("fewer than 15 minutes of clean snapshots")
    if read == "No read":
        reasons.append("no PCR read yet")
    direction = [e for e in ev if e["key"] in ("pcr", "spot", "fut", "iv")]
    bull = [e for e in direction if e["vote"] > 0]
    bear = [e for e in direction if e["vote"] < 0]
    if bull and bear:
        reasons.append("evidence conflicts: " + ", ".join(e["name"] for e in bull) + " bullish vs "
                       + ", ".join(e["name"] for e in bear) + " bearish")
    if read == "Mixed":
        reasons.append("PCR read is mixed")

    cls = "No Trade"
    if not reasons:
        conf_items = [e for e in direction if e["key"] != "pcr"]
        if pv != 0:
            n_conf = sum(1 for e in conf_items if e["vote"] == pv)
            if read in ("Bullish lean", "Bearish lean") and n_conf >= 2 and expanding:
                cls = "Strong Bullish" if pv > 0 else "Strong Bearish"
            elif n_conf >= 1:
                cls = "Bullish" if pv > 0 else "Bearish"
            else:
                cls = "Neutral"
                reasons.append(f"PCR {('bullish' if pv > 0 else 'bearish')} lean not confirmed by spot, futures or IV")
        elif read == "Potentially sideways" and all(e["vote"] == 0 for e in conf_items):
            cls = "Neutral"
        else:
            reasons.append("no directional PCR read and no sideways confirmation")
    # ---- confidence: share of the five evidence items agreeing with the class
    if cls == "No Trade":
        conf, agree = 0, []
    else:
        want = 1 if "Bullish" in cls else -1 if "Bearish" in cls else 0
        agree = []
        for e in ev:
            if not e["available"]:
                continue
            if e["key"] == "vol":
                ok = (expanding is True) if want != 0 else (expanding is False)
            else:
                ok = e["vote"] == want
            if ok:
                agree.append(e["name"])
        conf = round(100 * len(agree) / N_EVIDENCE)
    return {"class": cls, "confidence": conf, "agree": agree, "evidence": ev, "reasons": reasons,
            "note": "Confidence = share of the 5 evidence items agreeing (equal weights, uncalibrated) — not a "
                    "probability. Analysis, not advice."}
