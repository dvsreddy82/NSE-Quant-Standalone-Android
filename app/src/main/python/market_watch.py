"""Market watch: three alert engines for an option writer, independent of any screen.

1. Global markets — world indices, US futures, crude, dollar, US yields, USD/INR and US VIX
   from Yahoo's public chart API (works around the clock, no broker needed).  A move past an
   instrument's threshold raises an alert, once per session and per step (1×, 2×, 3× the
   threshold).  `global_cue` turns the overnight / Asian moves into a read for India's open.
2. Level breaks — NIFTY / BANKNIFTY against today's key levels: previous-day high / low, the
   15-minute opening range, the daily support / resistance zones and the option-chain walls
   (largest put OI below spot, largest call OI above).  Judged on 5-minute closes with a small
   buffer, so a wick through a level is not a break.  Each level alerts once a day per
   direction and re-arms after price closes back across it.
3. Direction calls — every 15 minutes in market hours, the option chain now vs ~15 minutes
   ago (OI added near the money, PCR, wall shifts, ATM IV, spot momentum) plus level breaks
   and the daily trend are scored into BULLISH / BEARISH / SIDEWAYS with a writing
   suggestion.  Each call is graded at the next one, so the hit rate is visible.

Read-only analytics, not investment advice.  Fetching blocks; call it from worker threads.
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pandas as pd
import requests

import config

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0"}       # Yahoo answers 429 to a full browser UA without its cookies
CHART = "https://{}.finance.yahoo.com/v8/finance/chart/{}"

# key, name, Yahoo ticker, group, unit ("pct" or "bps" for yields), alert threshold, weight in the India cue
# (positive weight: up is good for Indian equities; negative: up is bad)
GLOBAL = [
    ("GIFT", "GIFT Nifty", "sgxnifty.org", "India (GIFT City)", "pct", 0.6, 3.0),
    ("ES", "S&P 500 futures", "ES=F", "US futures", "pct", 0.8, 2.0),
    ("NQ", "Nasdaq 100 futures", "NQ=F", "US futures", "pct", 1.0, 1.0),
    ("YM", "Dow futures", "YM=F", "US futures", "pct", 0.8, 0.5),
    ("SPX", "S&P 500", "^GSPC", "US", "pct", 1.0, 1.5),
    ("NDX", "Nasdaq Composite", "^IXIC", "US", "pct", 1.3, 0.75),
    ("DJI", "Dow Jones", "^DJI", "US", "pct", 1.0, 0.5),
    ("VIX", "US VIX", "^VIX", "US", "pct", 12.0, -0.08),
    ("N225", "Nikkei 225", "^N225", "Asia", "pct", 1.5, 1.0),
    ("HSI", "Hang Seng", "^HSI", "Asia", "pct", 1.5, 1.0),
    ("SSE", "Shanghai Composite", "000001.SS", "Asia", "pct", 1.5, 0.5),
    ("KOSPI", "KOSPI", "^KS11", "Asia", "pct", 1.5, 0.75),
    ("TWII", "Taiwan Weighted", "^TWII", "Asia", "pct", 1.5, 0.5),
    ("FTSE", "FTSE 100", "^FTSE", "Europe", "pct", 1.2, 0.5),
    ("DAX", "DAX", "^GDAXI", "Europe", "pct", 1.2, 0.75),
    ("BRENT", "Brent crude", "BZ=F", "Macro", "pct", 3.0, -0.4),
    ("DXY", "Dollar index", "DX-Y.NYB", "Macro", "pct", 0.7, -0.75),
    ("US10Y", "US 10Y yield", "^TNX", "Macro", "bps", 10.0, -0.06),
    ("USDINR", "USD / INR", "INR=X", "Macro", "pct", 0.5, -1.5),
    ("GOLD", "Gold", "GC=F", "Macro", "pct", 2.0, 0.0),
]
GLOBAL_BY_KEY = {g[0]: g for g in GLOBAL}
STALE_HOURS = 20          # a quote older than this belongs to a session that is not the latest
GIFT_URL = "https://sgxnifty.org/"     # GIFT Nifty (NSE IX, near-month NIFTY future): Yahoo has no reliable feed


# ====================================================================== global markets
def _num(txt: str) -> float:
    return float(txt.replace(",", "").replace("+", "").replace("%", "").strip())


def _gift(g) -> dict:
    """GIFT Nifty from sgxnifty.org's page: last trade, change, high / low / open, the time of the
    last trade and the intraday line.  GIFT trades ~6:30 am – 3:40 pm and 4:35 pm – 2:45 am IST,
    so a session runs from the morning to the small hours of the next day."""
    import json
    import re
    key, name, src, group, unit, thr, _w = g
    r = requests.get(GIFT_URL, headers=UA, timeout=15)
    r.raise_for_status()
    h = r.text
    cells = re.findall(r'class="main-change[^"]*">(?:\s*<i[^>]*></i>)?\s*([-+0-9.,%]+)\s*</td>', h)
    if len(cells) < 3:
        raise ValueError("GIFT Nifty quote not found on the page")
    last, chg, pct = _num(cells[0]), _num(cells[1]), _num(cells[2])
    if "negative" in h[h.find("main-change"):h.find("main-change") + 40] and chg > 0:
        chg, pct = -chg, -pct
    sub = [_num(x) for x in re.findall(r'class="main-sub">\s*([0-9.,]+)\s*</td>', h)[:3]]
    high, low, open_ = (sub + [None] * 3)[:3]
    m = re.search(r"As on \w+, (\d{1,2} \w{3} \d{4})", h)
    year = pd.Timestamp(m.group(1)).year if m else date.today().year
    m = re.search(r"Last Trade on (\d{1,2} \w{3}) (\d{1,2}:\d{2} [AP]M)", h)
    at = None
    if m:
        t = pd.Timestamp(f"{m.group(1)} {year} {m.group(2)}", tz=config.IST)
        if t > pd.Timestamp.now(tz=config.IST) + pd.Timedelta(days=2):      # "As on" in January, trade in December
            t = t - pd.DateOffset(years=1)
        at = t.to_pydatetime()
    spark = []
    m = re.search(r"chartIntradayData\s*=\s*(\[.*?\]);", h, re.S)
    if m:
        try:
            pts = [float(x["value"]) for x in json.loads(m.group(1))]
            spark = pts[::5][-78:]                                         # ~5-minute steps
        except (ValueError, KeyError):
            spark = []
    prev = last - chg
    session = (pd.Timestamp(at) - pd.Timedelta(hours=6, minutes=30)).date() if at else None
    return {"key": key, "name": name, "ticker": "GIFT NIFTY", "group": group, "unit": unit, "threshold": thr,
            "last": last, "prev": prev, "chg": chg, "pct": pct, "move": pct, "at": at,
            "high": high, "low": low, "open": open_,
            "session": str(session) if session else None,
            "stale": at is None or (time.time() - at.timestamp()) > STALE_HOURS * 3600,
            "spark": spark}


def _one(g) -> dict:
    key, name, tk, group, unit, thr, _w = g
    if key == "GIFT":
        return _gift(g)
    for host in ("query1", "query2", "query1"):
        r = requests.get(CHART.format(host, tk), params={"range": "1d", "interval": "5m"}, headers=UA, timeout=12)
        if r.status_code != 429:
            break
        time.sleep(1.5)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    m = res["meta"]
    last, prev = m.get("regularMarketPrice"), m.get("chartPreviousClose") or m.get("previousClose")
    closes = [c for c in ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [] if c is not None]
    at = datetime.fromtimestamp(m["regularMarketTime"]).astimezone() if m.get("regularMarketTime") else None
    chg = (last - prev) if last is not None and prev else None
    pct = chg / prev * 100 if chg is not None else None
    move = (chg * 100 if unit == "bps" else pct) if chg is not None else None     # in the alert's unit
    tz = m.get("exchangeTimezoneName") or "UTC"
    session = pd.Timestamp(m["regularMarketTime"], unit="s", tz="UTC").tz_convert(tz).date() if at else None
    return {"key": key, "name": name, "ticker": tk, "group": group, "unit": unit, "threshold": thr,
            "last": last, "prev": prev, "chg": chg, "pct": pct, "move": move, "at": at,
            "session": str(session) if session else None,
            "stale": at is None or (time.time() - at.timestamp()) > STALE_HOURS * 3600,
            "spark": closes[-78:]}


def fetch_global() -> tuple[list[dict], list[str]]:
    """Latest quote for every instrument in GLOBAL (parallel). Returns (quotes, failed names)."""
    out, failed = [], []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for g, fut in [(g, ex.submit(_one, g)) for g in GLOBAL]:
            try:
                out.append(fut.result())
            except Exception as exc:
                log.info("Global quote %s failed: %s", g[2], exc)
                failed.append(g[1])
    order = {g[0]: i for i, g in enumerate(GLOBAL)}
    return sorted(out, key=lambda q: order[q["key"]]), failed


def _fmt_move(q: dict) -> str:
    if q["unit"] == "bps":
        return f"{q['move']:+.0f} bps to {q['last']:.2f}%"
    return f"{q['pct']:+.2f}% to {q['last']:,.2f}"


def global_alerts(quotes: list[dict]) -> list[dict]:
    """Moves past the threshold, one per instrument / session / step. Ids are stable, so the
    caller (and the phone) alert only on ids not seen before."""
    out = []
    for q in quotes:
        if q.get("move") is None or q["stale"]:
            continue
        step = int(abs(q["move"]) // q["threshold"])
        if step < 1:
            continue
        g = GLOBAL_BY_KEY[q["key"]]
        w = g[6]
        good = (q["move"] > 0) == (w > 0) if w else None
        india = ("" if good is None else " — supportive for Indian equities" if good
                 else " — a headwind for Indian equities")
        level = "HIGH" if step >= 2 or (q["key"] in ("ES", "SPX", "USDINR", "BRENT") and abs(q["move"]) >= 1.5 * q["threshold"]) \
            else "MEDIUM"
        out.append({"id": "glb:" + _hid(q["key"], q["session"], step if step < 3 else 3, "up" if q["move"] > 0 else "dn"),
                    "kind": "GLOBAL", "level": level, "key": q["key"],
                    "title": f"{q['name']} {'▲' if q['move'] > 0 else '▼'} {abs(q['pct'] if q['unit'] == 'pct' else q['move']):.2f}"
                             f"{'%' if q['unit'] == 'pct' else ' bps'}",
                    "text": f"{q['name']} {_fmt_move(q)} (threshold {q['threshold']:g}"
                            f"{'%' if q['unit'] == 'pct' else ' bps'}){india}.",
                    "at": q["at"]})
    downs = [q for q in quotes if q["group"] in ("US futures", "US", "Asia", "Europe") and not q["stale"]
             and q["key"] != "VIX" and (q.get("pct") or 0) <= -1.0]
    ups = [q for q in quotes if q["group"] in ("US futures", "US", "Asia", "Europe") and not q["stale"]
           and q["key"] != "VIX" and (q.get("pct") or 0) >= 1.0]
    for side, grp in (("dn", downs), ("up", ups)):
        if len(grp) >= 4:
            day = max(q["session"] for q in grp)
            out.append({"id": "glb:" + _hid("broad", day, side), "kind": "GLOBAL", "level": "HIGH", "key": "BROAD",
                        "title": "Global " + ("sell-off" if side == "dn" else "rally") + f" — {len(grp)} markets "
                                 + ("down" if side == "dn" else "up") + " ≥1%",
                        "text": ", ".join(f"{q['name']} {q['pct']:+.1f}%" for q in grp[:6]) + ".", "at": None})
    return out


def gift_read(quotes: list[dict], nifty_close: float | None) -> dict | None:
    """GIFT Nifty against NIFTY's last close: the market's own estimate of the opening gap.
    GIFT is the near-month future, so the gap includes the futures premium (usually 0.1–0.4%)."""
    q = next((q for q in quotes if q["key"] == "GIFT" and q.get("last")), None)
    if not q:
        return None
    out = {"last": q["last"], "pct": q.get("pct"), "at": q.get("at"), "stale": q.get("stale")}
    if nifty_close:
        gap = q["last"] - nifty_close
        out.update(nifty_close=nifty_close, gap=round(gap, 1), gap_pct=round(gap / nifty_close * 100, 2))
        g = out["gap_pct"]
        out["open"] = ("gap-up" if g >= 0.5 else "slightly higher" if g >= 0.2 else
                       "gap-down" if g <= -0.3 else "slightly lower" if g <= -0.1 else "flat")
        out["line"] = (f"GIFT Nifty {q['last']:,.1f} ({q['pct']:+.2f}%) → {out['open']} open, "
                       f"{gap:+,.0f} pts vs NIFTY's {nifty_close:,.0f} close (incl. futures premium)")
    else:
        out["line"] = f"GIFT Nifty {q['last']:,.1f} ({q['pct']:+.2f}%)"
    return out


def global_cue(quotes: list[dict], nifty_close: float | None = None) -> dict:
    """Weighted read of the latest sessions for India's next open: score −100…+100.  GIFT Nifty
    carries the biggest weight: it is India's own overnight market."""
    num = den = 0.0
    parts = []
    for q in quotes:
        w = GLOBAL_BY_KEY[q["key"]][6]
        if not w or q.get("move") is None or q["stale"]:
            continue
        x = max(-3.0, min(3.0, q["move"] / q["threshold"]))         # moves in units of "a notable move"
        num += w * x
        den += abs(w)
        if abs(x) >= 0.5:
            parts.append((abs(w * x), f"{q['name']} {_fmt_move(q).split(' to ')[0]}"))
    score = round(num / den / 3 * 100) if den else 0
    if score >= 25:
        read, side = "Positive", "put writing has the wind behind it; keep call shorts above resistance"
    elif score <= -25:
        read, side = "Negative", "call writing favoured; be careful with fresh short puts near support"
    else:
        read, side = "Neutral", "no strong global push; let India's own chain and levels decide"
    if abs(score) >= 55:
        read = "Strongly " + read.lower()
    gift = gift_read(quotes, nifty_close)
    drivers = [t for _, t in sorted(parts, reverse=True) if not t.startswith("GIFT Nifty")][:5]
    if gift and not gift.get("stale"):
        drivers.insert(0, gift["line"])
    return {"score": score, "read": read, "writer": side, "drivers": drivers, "gift": gift}


# ====================================================================== key levels
BREAK_BUFFER = 0.0005        # a 5-minute close must be 0.05% through the level
LEVEL_WEIGHT = {"OI put wall": 3, "OI call wall": 3, "Daily S/R zone": 3,
                "Previous-day low": 2, "Previous-day high": 2, "Opening-range low": 1, "Opening-range high": 1}


def build_levels(spot: float, daily: pd.DataFrame | None, sr_doc: dict | None, chain: dict | None,
                 orange: tuple[float, float] | None, today: date | None = None) -> list[dict]:
    """Today's levels for one index, nearest first on each side, merged when within 0.1%."""
    today = today or date.today()
    lv: list[dict] = []
    if daily is not None and len(daily):
        d = daily[[ts.date() < today for ts in daily.index]]
        if len(d):
            lv += [{"name": "Previous-day high", "price": float(d["high"].iloc[-1])},
                   {"name": "Previous-day low", "price": float(d["low"].iloc[-1])}]
    if orange:
        lv += [{"name": "Opening-range high", "price": orange[0]}, {"name": "Opening-range low", "price": orange[1]}]
    for z in (sr_doc or {}).get("support_zones", [])[:2]:
        lv.append({"name": "Daily S/R zone", "price": float(z["low"]), "detail": f"zone {z['low']:,.0f}–{z['high']:,.0f}"})
    for z in (sr_doc or {}).get("resistance_zones", [])[:2]:
        lv.append({"name": "Daily S/R zone", "price": float(z["high"]), "detail": f"zone {z['low']:,.0f}–{z['high']:,.0f}"})
    if chain:
        pw = [w for w in chain.get("put_walls") or [] if w["strike"] < spot]
        cw = [w for w in chain.get("call_walls") or [] if w["strike"] > spot]
        if pw:
            lv.append({"name": "OI put wall", "price": float(pw[0]["strike"]), "detail": "largest put OI below spot"})
        if cw:
            lv.append({"name": "OI call wall", "price": float(cw[0]["strike"]), "detail": "largest call OI above spot"})
    lv = [x for x in lv if x["price"] and x["price"] > 0]
    lv.sort(key=lambda x: (-LEVEL_WEIGHT.get(x["name"], 0)))
    merged: list[dict] = []
    for x in lv:                                     # strongest name wins, the others are listed
        hit = next((m for m in merged if abs(m["price"] - x["price"]) / spot < 0.001), None)
        if hit:
            hit.setdefault("also", []).append(x["name"])
        else:
            merged.append(dict(x))
    for m in merged:
        m["side"] = "support" if m["price"] < spot else "resistance"
        m["dist_pct"] = (spot - m["price"]) / spot * 100
        m["weight"] = LEVEL_WEIGHT.get(m["name"], 1) + len(m.get("also", []))
    return sorted(merged, key=lambda m: -m["price"])


def _label(m: dict) -> str:
    s = m["name"] + (f" + {', '.join(m['also'])}" if m.get("also") else "")
    return f"{s} {m['price']:,.0f}"


class LevelWatcher:
    """Crossings of today's levels on 5-minute closes; state per index, reset each day."""

    def __init__(self):
        self.day: date | None = None
        self.prev: dict[str, float] = {}            # index -> previous 5-minute close
        self.fired: dict[str, set] = {}             # index -> {(level name, rounded price, direction)}
        self.status: dict[str, dict] = {}           # index -> {round(price): "broken" / "reclaimed" / ...}

    def reset(self, day: date):
        self.day, self.prev, self.fired, self.status = day, {}, {}, {}

    def check(self, und: str, close: float, levels: list[dict], prev_close: float | None = None,
              now: datetime | None = None) -> list[dict]:
        now = now or datetime.now()
        if self.day != now.date():
            self.reset(now.date())
        p0 = self.prev.get(und, prev_close)
        self.prev[und] = close
        if p0 is None:
            return []
        out = []
        fired = self.fired.setdefault(und, set())
        st = self.status.setdefault(und, {})
        for i, m in enumerate(levels):
            L, buf = m["price"], m["price"] * BREAK_BUFFER
            down = p0 >= L and close < L - buf
            up = p0 <= L and close > L + buf
            if not (down or up):
                continue
            d = "down" if down else "up"
            key = (m["name"], round(L), d)
            st[round(L)] = "broken below" if down else "broken above"
            if key in fired:
                continue
            fired.add(key)
            if m["name"] in ("Previous-day high", "Previous-day low") and not m.get("also"):
                continue                       # announced by the open-play alerts (chart_signals), status kept
            fired.discard((m["name"], round(L), "up" if down else "down"))     # re-arm the other way
            below = [x for x in levels if x["price"] < close - buf and x is not m]
            above = [x for x in levels if x["price"] > close + buf and x is not m]
            nxt_dn = max(below, key=lambda x: x["price"]) if below else None
            nxt_up = min(above, key=lambda x: x["price"]) if above else None
            was_support = m["side"] == "support" or (down and m["price"] < p0)
            if down:
                title = f"{und} broke {'support' if was_support else 'below'} {_label(m)}"
                txt = (f"5-min close {close:,.1f} (prev {p0:,.1f}). Short puts near {L:,.0f} are now under pressure"
                       + (f"; next support {_label(nxt_dn)}" if nxt_dn else "")
                       + ". Writer read: avoid fresh short puts above that, hedge or roll existing ones lower"
                       + (f"; call writing above {_label(nxt_up)} is favoured." if nxt_up else "."))
            else:
                title = f"{und} broke {'resistance' if m['side'] == 'resistance' else 'above'} {_label(m)}"
                txt = (f"5-min close {close:,.1f} (prev {p0:,.1f}). Short calls near {L:,.0f} are now under pressure"
                       + (f"; next resistance {_label(nxt_up)}" if nxt_up else "")
                       + ". Writer read: avoid fresh short calls below that, hedge or roll existing ones higher"
                       + (f"; put writing below {_label(nxt_dn)} is favoured." if nxt_dn else "."))
            if (down and m["side"] == "resistance") or (up and m["side"] == "support"):
                title = f"{und} {'lost' if down else 'reclaimed'} {_label(m)}"
            out.append({"id": "lvl:" + _hid(und, now.date(), m["name"], round(L), d), "kind": "LEVEL",
                        "level": "HIGH" if m["weight"] >= 3 else "MEDIUM", "und": und, "direction": d,
                        "title": title, "text": txt, "price": L, "close": close, "at": now.astimezone()})
        return out


# ====================================================================== 15-minute direction call
CALL_WINDOW = (8 * 60, 30 * 60)       # compare with the chain observed 8-30 min ago (closest to 15)


def pick_15m(history: list[dict], expiry: str, now: float | None = None) -> dict | None:
    now = now or time.time()
    c = [h for h in history if h["expiry"] == expiry and CALL_WINDOW[0] <= now - h["ts"] <= CALL_WINDOW[1]]
    return min(c, key=lambda h: abs((now - h["ts"]) - 900)) if c else None


def _round(x: float, step: float, up: bool) -> float:
    return (math.ceil(x / step) if up else math.floor(x / step)) * step


def direction_call(res: dict, prev: dict | None, levels: list[dict] | None = None,
                   breaks: list[dict] | None = None, risk: dict | None = None) -> dict:
    """Score the last ~15 minutes of the chain into a direction and a writing suggestion."""
    S, step = res["spot"], res.get("step") or 50
    rows = {r["strike"]: r for r in res.get("chain") or []}
    near = sorted(rows, key=lambda k: abs(k - S))[:11]               # ATM ± 5 strikes
    sig: list[tuple[int, str]] = []

    if prev:
        mv = (S - prev["spot"]) / prev["spot"] * 100
        mins = (time.time() - prev["ts"]) / 60
        s = 2 if mv >= 0.35 else 1 if mv >= 0.12 else -2 if mv <= -0.35 else -1 if mv <= -0.12 else 0
        sig.append((s, f"Spot {mv:+.2f}% in {mins:.0f} min ({prev['spot']:,.1f} → {S:,.1f})"))
        prow = prev.get("rows") or {}
        # OI change *with* premium change over the window, per near-ATM strike: OI up + premium down = possible
        # writing, OI up + premium up = possible buying (not confirmed identities).  Put writing / call buying
        # lean bullish, call writing / put buying lean bearish.  This is the one positioning score; the PCR
        # below is shown for context only, because it moves with the same OI and would count it twice.
        fl = {"pe_w": 0.0, "pe_b": 0.0, "ce_w": 0.0, "ce_b": 0.0}
        base = sum((rows[k].get("pe_oi") or 0) + (rows[k].get("ce_oi") or 0) for k in near) or 1
        added = 0.0
        for k in near:
            if k not in prow:
                continue
            for side in ("pe", "ce"):
                d_oi = (rows[k].get(f"{side}_oi") or 0) - ((prow.get(k) or {}).get(f"{side}_oi") or 0)
                d_px = (rows[k].get(f"{side}_ltp") or 0) - ((prow.get(k) or {}).get(f"{side}_ltp") or 0)
                if d_oi > 0:
                    added += d_oi
                    if d_px < 0:
                        fl[f"{side}_w"] += d_oi
                    elif d_px > 0:
                        fl[f"{side}_b"] += d_oi
        if added / base >= 0.004:
            r = ((fl["pe_w"] + fl["ce_b"]) - (fl["ce_w"] + fl["pe_b"])) / added
            s = 2 if r >= 0.5 else 1 if r >= 0.2 else -2 if r <= -0.5 else -1 if r <= -0.2 else 0
            what = ("put writing / call buying dominated (bullish lean)" if r >= 0.2 else
                    "call writing / put buying dominated (bearish lean)" if r <= -0.2 else "mixed — no clear lean")
            sig.append((s, f"Near-ATM OI added with premium: put writing {_lakh(fl['pe_w'])}, put buying "
                           f"{_lakh(fl['pe_b'])}, call writing {_lakh(fl['ce_w'])}, call buying {_lakh(fl['ce_b'])}"
                           f" — {what}"))
        else:
            sig.append((0, "Near-ATM OI barely changed — no fresh positioning"))
        pk = lambda rr, key: sum((rr.get(k) or {}).get(key) or 0 for k in near)
        pcr_now = pk(rows, "pe_oi") / (pk(rows, "ce_oi") or 1)
        pcr_then = pk(prow, "pe_oi") / (pk(prow, "ce_oi") or 1) if prow else None
        if pcr_then:
            sig.append((0, f"Near-ATM PCR {pcr_then:.2f} → {pcr_now:.2f} (context; scored through the OI flow above)"))
        atm = min(rows, key=lambda k: abs(k - S)) if rows else None
        iv_now = _avg((rows.get(atm) or {}).get("ce_iv"), (rows.get(atm) or {}).get("pe_iv")) if atm else None
        iv_then = _avg((prow.get(atm) or {}).get("ce_iv"), (prow.get(atm) or {}).get("pe_iv")) if atm and prow else None
        if iv_now and iv_then:
            div = iv_now - iv_then
            if div >= 0.5 and mv < 0:
                sig.append((-1, f"ATM IV up {div:+.1f} pts while spot fell — buyers paying for downside"))
            elif div <= -0.5 and mv > 0:
                sig.append((1, f"ATM IV down {div:+.1f} pts while spot rose — calm, orderly up-move"))
            elif abs(div) >= 0.5:
                sig.append((0, f"ATM IV {iv_then:.1f}% → {iv_now:.1f}%"))
        common = set(rows) & set(prow)                   # same strikes on both sides of the comparison
        pw_then = _wall({k: prow[k] for k in common}, prev["spot"], "pe")
        cw_then = _wall({k: prow[k] for k in common}, prev["spot"], "ce")
        pw_now, cw_now = _wall({k: rows[k] for k in common}, S, "pe"), _wall({k: rows[k] for k in common}, S, "ce")
        if pw_then and pw_now and pw_now != pw_then:
            sig.append((1 if pw_now > pw_then else -1, f"Put wall moved {pw_then:,.0f} → {pw_now:,.0f}"))
        if cw_then and cw_now and cw_now != cw_then:
            sig.append((1 if cw_now > cw_then else -1, f"Call wall moved {cw_then:,.0f} → {cw_now:,.0f}"))
    else:
        sig.append((0, "No chain from ~15 min ago yet — this call uses levels and positioning only"))

    pcr = res.get("pcr")
    if pcr is not None:            # a PCR level alone is not bullish or bearish: context, not scored
        sig.append((0, f"Total PCR (OI) {pcr:.2f} (context only)"))
    trend = (res.get("trend") or "").lower()
    if "bull" in trend or "bear" in trend:
        sig.append((1 if "bull" in trend else -1, f"Daily trend: {res.get('trend')}"))
    if (res.get("dte") or 99) < 1 and res.get("max_pain"):
        gap = (res["max_pain"] - S) / S * 100
        if abs(gap) >= 0.2:
            sig.append((1 if gap > 0 else -1, f"Expiry day: max pain {res['max_pain']:,.0f} is {gap:+.2f}% away"))
    for b in (breaks or [])[-2:]:
        sig.append((1 if b["direction"] == "up" else -1, b["title"]))

    score = sum(s for s, _ in sig)
    direction = "BULLISH" if score >= 3 else "BEARISH" if score <= -3 else "SIDEWAYS"
    lean = "" if direction != "SIDEWAYS" or score == 0 else (" (leaning up)" if score > 0 else " (leaning down)")
    conf = "High" if abs(score) >= 6 else "Medium" if abs(score) >= 4 else "Low"
    if direction == "SIDEWAYS":
        conf = "Medium" if abs(score) <= 1 else "Low"

    sigma = res.get("sigma_move") or S * 0.01
    pw = (res.get("put_walls") or [{}])[0].get("strike")
    cw = (res.get("call_walls") or [{}])[0].get("strike")
    sup = [l for l in levels or [] if l["price"] < S]
    resi = [l for l in levels or [] if l["price"] > S]
    put_k = min(pw or 1e12, _round(S - 0.8 * sigma, step, False))
    call_k = max(cw or 0, _round(S + 0.8 * sigma, step, True))
    if direction == "BULLISH":
        idea = (f"Sell puts at or below {put_k:,.0f}" + (f" (under {_label(sup[0])})" if sup else "")     # levels: high → low
                + f"; avoid fresh short calls below {call_k:,.0f}.")
    elif direction == "BEARISH":
        idea = (f"Sell calls at or above {call_k:,.0f}" + (f" (over {_label(resi[-1])})" if resi else "")
                + f"; avoid fresh short puts above {put_k:,.0f}.")
    else:
        idea = f"Range {put_k:,.0f}–{call_k:,.0f}: a hedged strangle / iron condor outside it fits; no directional add."
    reds = [a for a in (risk or {}).get("alerts", []) if a.get("level") == "RED"]
    if reds:
        idea += " ⚠ Risk monitor RED: " + "; ".join(a["title"] for a in reds[:2]) + " — that side only as a spread."
    return {"und": res["underlying"], "spot": S, "expiry": res["expiry"], "direction": direction + lean,
            "dir": direction, "score": score, "confidence": conf, "signals": [{"score": s, "text": t} for s, t in sig],
            "idea": idea, "put_strike": put_k, "call_strike": call_k, "pcr": pcr, "source": res.get("source")}


def grade(call: dict, spot_now: float) -> str:
    """Was the previous call right? (move > 0.05% in its direction; SIDEWAYS: within ±0.15%)."""
    mv = (spot_now - call["spot"]) / call["spot"] * 100
    if call["dir"] == "BULLISH":
        return "right" if mv > 0.05 else "wrong" if mv < -0.05 else "flat"
    if call["dir"] == "BEARISH":
        return "right" if mv < -0.05 else "wrong" if mv > 0.05 else "flat"
    return "right" if abs(mv) <= 0.15 else "wrong"


def call_slots(day: date) -> list[datetime]:
    """09:30, 09:45 … 15:15 IST."""
    out, t = [], datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30)
    while t.hour < 15 or (t.hour == 15 and t.minute <= 15):
        out.append(t)
        t = t + pd.Timedelta(minutes=15)
    return out


# ====================================================================== helpers
def _wall(rows: dict, spot: float, side: str):
    key = f"{side}_oi"
    cand = {k: (v or {}).get(key) or 0 for k, v in rows.items() if (k < spot if side == "pe" else k > spot)}
    return max(cand, key=cand.get) if cand and max(cand.values()) > 0 else None


def _avg(*v):
    v = [x for x in v if x]
    return sum(v) / len(v) if v else None


def _lakh(v: float) -> str:
    a = abs(v)
    s = f"{a / 1e5:.1f}L" if a >= 1e5 else f"{a / 1e3:.0f}K" if a >= 1e3 else f"{a:.0f}"
    return ("+" if v >= 0 else "−") + s


def _hid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def now_ist() -> datetime:
    return pd.Timestamp.now(tz=config.IST).to_pydatetime()
