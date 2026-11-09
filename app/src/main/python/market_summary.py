"""Market summaries at 8 AM and 9 PM on trading days: one notification each.

  * 9 PM ("evening")  recaps today's session and sets up tomorrow; global markets with the US session open.
  * 8 AM ("morning")  the same recap of the last session plus what happened overnight: GIFT Nifty vs the last
                      NIFTY close (the likely opening gap), the US close and Asia's open; levels are for today.

Sections (each skipped if its data isn't loaded):
  Close       NIFTY, BANKNIFTY, India VIX: close, change, range, where it closed in the range
  Options     per index: PCR, weighted near-ATM PCR and the PCR read, OI walls, max pain, ATM IV, expected
              move, the app's premium-selling verdict
  Levels      floor pivots and CPR from the session's high / low / close, CPR width (narrow = trend-day
              setup, wide = range-day setup)
  FII / DII   cash for the session (or the latest published day) and the positioning score
  Global      GIFT Nifty gap, the global cue score and the biggest moves (refreshed when built)
  The day     unusual volume, direction-call hit rate, alerts by type, high-impact news
  Positions   open P&L per book, stop / warning alerts, free margin (your own devices only)

Trading days come from NSE's F&O holiday list (fetched once a day; if it can't be fetched, weekdays count
and the evening run also needs evidence of a session today).  Stored in `market_summary` keyed by
(day, slot).  Built only from data the app already has, plus a global refresh.  Descriptive analysis,
not a forecast or advice.  Blocking: call from a worker thread.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import date, datetime, time as dtime, timedelta

import pandas as pd

import config

log = logging.getLogger(__name__)

SLOTS = {"morning": (dtime(8, 0), dtime(9, 10)), "evening": (dtime(21, 0), dtime(23, 59))}
LABEL = {"morning": "Morning summary", "evening": "Evening summary"}
INDICES = (("NIFTY", "^NSEI"), ("BANKNIFTY", "NIFTY BANK"))
COLLECTION = "market_summary"
RETRY_S = 600

_holidays: dict = {"day": None, "set": None}


def _n(v, nd=0, sign=False) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "n/a"
    if x != x:
        return "n/a"
    return f"{x:+,.{nd}f}" if sign else f"{x:,.{nd}f}"


def _day(ts) -> str | None:
    try:
        t = pd.Timestamp(ts)
        if t.tzinfo is not None:
            t = t.tz_convert(config.IST)
        return str(t.date())
    except Exception:
        return None


def _dm(day: str) -> str:
    return f"{pd.Timestamp(day):%d %b}"


# ---------------------------------------------------------------------------------- trading calendar
def holidays() -> set[str] | None:
    """NSE F&O trading holidays (ISO dates), fetched once a day; None if NSE can't be reached."""
    today = str(date.today())
    if _holidays["day"] == today:
        return _holidays["set"]
    out = None
    try:
        from data_fetcher import NSEClient
        c = NSEClient(timeout=10, retries=2)
        r = c.get(c.BASE + "/api/holiday-master?type=trading")
        if r is not None:
            out = {str(pd.Timestamp(x["tradingDate"]).date()) for x in r.json().get("FO") or [] if x.get("tradingDate")}
    except Exception as exc:
        log.info("market summary: holiday list unavailable: %s", exc)
    _holidays.update(day=today, set=out)
    return out


def trading_day(d: date, hol: set[str] | None) -> bool:
    return d.weekday() < 5 and (not hol or str(d) not in hol)


def previous_session(d: date, hol: set[str] | None) -> date:
    x = d - timedelta(days=1)
    while not trading_day(x, hol):
        x -= timedelta(days=1)
    return x


# ---------------------------------------------------------------------------------- inputs
def _ohlc(win, sym: str, day: str, use_snapshot: bool) -> dict | None:
    """The session's open / high / low / close and the previous close: daily store first, then the live tick /
    snapshot (only trusted when it is that session's own data)."""
    try:
        d = win.svc.load_ohlcv(sym, "1D")
    except Exception:
        d = None
    if d is not None and len(d) >= 2:
        days = [_day(i) for i in d.index]
        if day in days:
            i = days.index(day)
            if i >= 1:
                b, p = d.iloc[i], d.iloc[i - 1]
                return {"open": float(b["open"]), "high": float(b["high"]), "low": float(b["low"]),
                        "close": float(b["close"]), "prev": float(p["close"]), "src": "daily"}
    if not use_snapshot:
        return None
    snap = getattr(win, "_last_snap", None) or {}
    q = (snap.get("quotes") or {}).get(sym) if sym != "^INDIAVIX" else snap.get("vix")
    feed = getattr(getattr(win, "broker", None), "feed", None)
    if feed is not None and getattr(feed, "live", False):
        try:
            from shoonya_feed import INDEX_TOKENS, VIX_TOKEN
            t = feed.tick(VIX_TOKEN if sym == "^INDIAVIX" else INDEX_TOKENS[sym])
            if t and t.get("lp"):
                q = {"last": float(t["lp"]), "prev": float(t.get("c") or 0) or None,
                     "high": float(t.get("h") or 0) or None, "low": float(t.get("l") or 0) or None}
        except Exception:
            pass
    if q and q.get("last") and q.get("high") and q.get("low") and q.get("prev"):
        return {"open": None, "high": float(q["high"]), "low": float(q["low"]), "close": float(q["last"]),
                "prev": float(q["prev"]), "src": "snapshot"}
    return None


def _chain(routes, und: str) -> dict | None:
    opts = routes.win.options
    c = opts.fresh(und, 20 * 3600)
    if c:
        return c
    try:
        with routes._chain_locks[und]:
            return opts.fresh(und, 20 * 3600) or opts.analyse(und)
    except Exception as exc:
        log.info("market summary: %s chain: %s", und, exc)
        return None


# ---------------------------------------------------------------------------------- sections
def _close(ohlc: dict) -> list[str]:
    out = []
    for key, name in (("NIFTY", "NIFTY 50"), ("BANKNIFTY", "BANKNIFTY"), ("VIX", "India VIX")):
        o = ohlc.get(key)
        if not o:
            continue
        ch = o["close"] - o["prev"]
        nd = 2 if key == "VIX" else 0
        rng = pos = ""
        if key != "VIX":
            rng = f" · range {_n(o['low'])}–{_n(o['high'])}"
            if o["high"] > o["low"]:
                at = (o["close"] - o["low"]) / (o["high"] - o["low"])
                pos = " · closed " + ("near the high" if at >= 0.8 else "near the low" if at <= 0.2 else "mid-range")
        out.append(f"{name} {_n(o['close'], nd)} ({_n(ch, nd, True)}, {_n(ch / o['prev'] * 100, 2, True)}%){rng}{pos}")
    return out


def _options(chains: dict) -> list[str]:
    out = []
    for und, c in chains.items():
        if not c:
            out.append(f"{und}: option chain not available")
            continue
        b = c.get("pcr_board") or {}
        wtd = next((r.get("now") for r in b.get("rows") or [] if r.get("key") == "wtd"), None)
        cw = (c.get("call_walls") or [{}])[0].get("strike")
        pw = (c.get("put_walls") or [{}])[0].get("strike")
        out.append(f"{und} ({c.get('expiry')}, {_n(c.get('dte'), 0)} d): PCR {_n(c.get('pcr'), 2)}"
                   + (f", weighted near-ATM {_n(wtd, 2)}" if wtd is not None else "")
                   + (f" — read: {b['read']}" if b.get("read") else "")
                   + f" · call wall {_n(cw)}, put wall {_n(pw)}, max pain {_n(c.get('max_pain'))}"
                   + f" · ATM IV {_n(c.get('atm_iv'), 1)}%, ±1σ {_n(c.get('sigma_move'))} pts to expiry"
                   + (f" · selling verdict: {c['verdict']}" if c.get("verdict") else ""))
    return out


def next_levels(o: dict) -> dict:
    """Floor pivots and CPR for the next session from this session's high / low / close."""
    import chart_signals as cs
    lv = cs.pivots(o["high"], o["low"], o["close"])
    w = (lv["TC"] - lv["BC"]) / o["close"] * 100
    lv["width"] = w
    lv["type"] = ("narrow — trend-day setup" if w < cs.CPR_NARROW_PCT else
                  "wide — range-day setup" if w > cs.CPR_WIDE_PCT else "normal")
    return lv


def _levels(ohlc: dict) -> list[str]:
    out = []
    for und, _s in INDICES:
        o = ohlc.get(und)
        if not o:
            continue
        lv = next_levels(o)
        side = "above" if o["close"] > lv["TC"] else "below" if o["close"] < lv["BC"] else "inside"
        out.append(f"{und}: CPR {_n(lv['BC'])}–{_n(lv['TC'])} ({lv['type']}, {lv['width']:.2f}%), last close "
                   f"{side} it · R1 {_n(lv['R1'])} R2 {_n(lv['R2'])} · S1 {_n(lv['S1'])} S2 {_n(lv['S2'])}")
    return out


def _flows(win, session: str) -> list[str]:
    x = win.fii_view.res or {}
    if not x.get("available"):
        return ["FII / DII data not loaded"]
    f = x["frame"]
    last = f.dropna(subset=["fii_cash"]).iloc[-1] if "fii_cash" in f and f["fii_cash"].notna().any() else None
    out = []
    if last is not None:
        cr = lambda v: _n(v, 1 if v is not None and abs(float(v)) < 10 else 0, True)
        out.append(f"FII cash {cr(last.get('fii_cash'))} Cr, DII {cr(last.get('dii_cash'))} Cr"
                   + ("" if _day(last.name) == session else
                      f" (latest published: {pd.Timestamp(last.name):%d %b}"
                      + (f"; {_dm(session)} not out yet)" if (_day(last.name) or "") < session else ")")))
    out.append(f"Positioning score {_n(x.get('score'), 0, True)} (−100…+100): {x.get('bias')}, "
               f"conviction {x.get('conviction')}")
    s = x.get("setup") or {}
    if s.get("direction") and _day(s.get("date")) == session:
        out.append(f"Evening squeeze-loading setup: {s['direction'].upper()}")
    return out


def _global(win) -> list[str]:
    w = win.watch
    try:
        w.poll_global()
    except Exception as exc:
        log.info("market summary: global refresh failed: %s", exc)
    cue = w.cue or {}
    out = []
    g = cue.get("gift") or {}
    if g.get("last"):
        out.append(f"GIFT Nifty {_n(g['last'], 0)}: {_n(g.get('gap'), 0, True)} pts ({_n(g.get('gap_pct'), 2, True)}%) "
                   f"vs the last NIFTY close" + (f" → {g['open']}" if g.get("open") else "")
                   + (" [stale]" if g.get("stale") else ""))
    if cue.get("read"):
        out.append(f"Global cue {_n(cue.get('score'), 0, True)}: {cue['read']}")
    movers = sorted((q for q in w.quotes if q.get("pct") is not None and not q.get("stale") and not q.get("offline")
                     and q.get("key") != "GIFT"), key=lambda q: -abs(q["pct"]))[:5]
    if movers:
        out.append("Moves: " + ", ".join(f"{q['name']} {_n(q['pct'], 1, True)}%" for q in movers))
    return out


def _day_recap(routes, session: str) -> list[str]:
    import alert_prefs
    import volume_watch
    win, w = routes.win, routes.win.watch
    out = []
    vol = getattr(win, "volume", None)
    v = vol.latest() if vol else None
    if v and v.get("day") == session:
        a = volume_watch.alert(v)
        top = [ln.split(", spike")[0].split(" — ")[0] for ln in a["text"].splitlines()[:3]]
        out.append("Unusual volume: " + a["title"].split(": ", 1)[-1] + (" — " + "; ".join(top) if top else ""))
    calls = []
    for und, _s in INDICES:
        right, n = w.hit_rate(w.today_calls(und, session))
        if n:
            calls.append(f"{und} {right}/{n}")
    if calls:
        out.append("Direction calls right: " + ", ".join(calls))
    labels = {k: lab for k, _g, lab, _h in alert_prefs.CATALOG}
    counts: dict[str, int] = {}
    for a in w.recent_alerts(4, ("GLOBAL", "LEVEL", "WRITER", "VOLUME")):
        if _day(a.get("raised_at")) == session:
            cat = alert_prefs.category(a)
            counts[cat] = counts.get(cat, 0) + 1
    if counts:
        out.append("Alerts: " + ", ".join(f"{labels.get(k, k)} {n}" for k, n in
                                           sorted(counts.items(), key=lambda kv: -kv[1])[:6]))
    try:
        news = win.news_svc.recent(hours=14, impact="HIGH", limit=3)
    except Exception:
        news = []
    out += [f"News: {d['title']}" for d in news]
    return out


def _positions(win) -> list[str]:
    d = win.trades.data or {}
    out = []
    for g in d.get("positions") or []:
        al = [t for a, t in g.get("alerts") or [] if a in ("stop", "warn")]
        out.append(f"{g['und']} {g['structure']} ({g['expiry']}): open P&L ₹{_n(g.get('open_pnl'), 0, True)}"
                   + (f" — {al[0]}" if al else ""))
    lim = d.get("limits") or {}
    if out and lim.get("available") is not None:
        out.append(f"Margin used ₹{_n(lim.get('margin_used'))}, free ₹{_n(lim.get('available'))}")
    return out


# ---------------------------------------------------------------------------------- build
def slot_for(now: datetime) -> str:
    """Which summary a manual build means at this time."""
    return "morning" if now.time() < dtime(9, 15) else "evening"


def build(routes, slot: str = "evening", today: date | None = None, include_positions: bool = True) -> dict:
    """(blocking) → {"day", "slot", "session_day", "session", "sections": [{"title", "lines"}], "title", "text"}."""
    from market_hours import now_ist
    today = today or now_ist().date()
    hol = holidays()
    win = routes.win
    out = {"day": str(today), "slot": slot, "built_at": datetime.now().astimezone(), "sections": [], "title": ""}
    if not trading_day(today, hol):
        out.update(session=False, session_day=None, text="Not a trading day (weekend or NSE holiday).")
        return out
    # the session being summarised: today in the evening (and before 3:30 pm for a manual build), the last one
    # in the morning
    session = today if slot == "evening" else previous_session(today, hol)
    sd = str(session)
    # the live snapshot is the session's own data in the evening, and still the last session's before the open
    snap_ok = slot == "evening" or (today == now_ist().date() and now_ist().time() < dtime(9, 15))
    ohlc = {und: _ohlc(win, sym, sd, use_snapshot=snap_ok) for und, sym in INDICES}
    ohlc["VIX"] = _ohlc(win, "^INDIAVIX", sd, use_snapshot=snap_ok)
    chains = {und: _chain(routes, und) for und, _s in INDICES}
    had = (any(o and o["src"] == "daily" for o in ohlc.values())
           or any(c and _day(c.get("timestamp")) == sd for c in chains.values()))
    if slot == "evening" and hol is None and not had:          # no holiday list: need evidence of a session
        out.update(session=False, session_day=sd, text="No session today (holiday?).")
        return out
    out.update(session=True, session_day=sd)
    nxt = "today" if slot == "morning" else "tomorrow"
    parts = [(f"Close {_dm(sd)}", lambda: _close(ohlc)), ("Options at the close", lambda: _options(chains)),
             (f"Levels for {nxt}", lambda: _levels(ohlc)), ("FII / DII", lambda: _flows(win, sd)),
             ("Global overnight" if slot == "morning" else "Global at 9 PM", lambda: _global(win)),
             (f"The day ({_dm(sd)})", lambda: _day_recap(routes, sd))]
    if include_positions:
        parts.append(("My positions", lambda: _positions(win)))
    for title, fn in parts:
        try:
            lines_ = [ln for ln in fn() if ln]
        except Exception as exc:                 # one missing feed shouldn't sink the summary
            log.info("market summary: %s skipped: %s", title, exc)
            lines_ = []
        if lines_:
            out["sections"].append({"title": title, "lines": lines_})
    head = []
    if slot == "morning":
        g = (win.watch.cue or {}).get("gift") or {}
        if g.get("last") and not g.get("stale"):
            head.append(f"GIFT Nifty {_n(g.get('gap'), 0, True)} pts")
    for und, _s in INDICES:
        o = ohlc.get(und)
        if o:
            head.append(f"{und} {_n(o['close'])} ({_n((o['close'] - o['prev']) / o['prev'] * 100, 2, True)}%)")
    out["title"] = f"{LABEL[slot]} {_dm(str(today))}" + (": " + " · ".join(head) if head else "")
    out["text"] = text(out)
    return out


def text(s: dict) -> str:
    return "\n".join(f"{x['title'].upper()}\n" + "\n".join(f"• {ln}" for ln in x["lines"]) for x in s["sections"]) \
        + "\n\nAnalysis of the market data — not advice."


def alert(s: dict) -> dict:
    """One notification per summary (kind SUMMARY, stable id per day and slot)."""
    return {"id": "sum:" + hashlib.sha1(f"{s['day']}|{s['slot']}".encode()).hexdigest()[:12], "kind": "SUMMARY",
            "level": "MEDIUM", "slot": s["slot"], "title": s["title"], "text": s["text"], "day": s["day"]}


def short_text(s: dict) -> str:
    """A few lines for a desktop banner."""
    keep = [ln for x in s.get("sections", []) if x["title"].startswith(("Close", "Global")) for ln in x["lines"]]
    return "\n".join(keep[:4])


def lines(s: dict | None) -> list[str]:
    """Flat lines for /api/signals."""
    if not s or not s.get("sections"):
        return []
    return [s["title"]] + [f"{x['title']}: {ln}" for x in s["sections"] for ln in x["lines"]]


class MarketSummary:
    """Schedule + storage around build()."""

    def __init__(self, store):
        self.store = store
        self.last: dict | None = None
        self._done: set[tuple[str, str]] = set()
        self._tried: dict[tuple[str, str], float] = {}

    def latest(self) -> dict | None:
        if self.last:
            return self.last
        try:
            docs = self.store.find_docs(COLLECTION, sort="day", desc=True, limit=2)
        except Exception:
            docs = []
        docs = [d for d in docs if d.get("sections")]
        if docs:
            self.last = max(docs, key=lambda d: (d["day"], d.get("slot") == "evening"))
        return self.last

    def _stored(self, day: str, slot: str) -> bool:
        if (day, slot) in self._done:
            return True
        try:
            hit = bool(self.store.find_docs(COLLECTION, {"day": day, "slot": slot}, limit=1))
        except Exception:
            hit = False
        if hit:
            self._done.add((day, slot))
        return hit

    def due(self, now: datetime) -> str | None:
        """The slot to build now ("morning" from 8:00, "evening" from 9 pm, weekdays), once each per day."""
        if now.weekday() >= 5:
            return None
        day = str(now.date())
        for slot, (a, b) in SLOTS.items():
            if a <= now.time() <= b and not self._stored(day, slot) \
                    and time.time() - self._tried.get((day, slot), 0) >= RETRY_S:
                return slot
        return None

    def run(self, routes, slot: str, include_positions: bool = True) -> dict:
        s = build(routes, slot, include_positions=include_positions)
        self._tried[(s["day"], slot)] = time.time()
        if s["session"]:
            try:
                self.store.upsert_docs(COLLECTION, [dict(s)])
            except Exception as exc:
                log.warning("market_summary not stored: %s", exc)
            self._done.add((s["day"], slot))
            self.last = s
        elif "Not a trading day" in s.get("text", ""):
            self._done.add((s["day"], slot))            # nothing to do today
        return s
