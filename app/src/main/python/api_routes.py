"""The phone API's endpoints, independent of Qt and of HTTP.

`ApiRoutes(win, need)` answers `get(path, query)` with JSON-ready dicts from whatever `win`
holds (the desktop MainWindow, or the standalone Android engine, which exposes the same
attributes).  `need(what)` asks the owner to refresh stale data.  Used by api_server.py (HTTP
for the companion app) and by the standalone Android app (called in-process).
"""
from __future__ import annotations

import hashlib
import re
import math
import threading
import time
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

import config

VERSION = 1


def _clean(o):
    """JSON-safe: numpy / pandas scalars, timestamps, NaN → None, tuples → lists."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_clean(v) for v in o]
    if isinstance(o, (pd.Timestamp, datetime, date)):
        return o.isoformat()
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return None if math.isnan(v) or math.isinf(v) else round(v, 4)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, pd.DataFrame):
        return None
    return o


def _alert_out(a: dict) -> dict:
    return {k: a.get(k) for k in ("id", "kind", "level", "title", "text", "raised_at", "und", "direction")}


def _id(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _kind(text: str) -> str:
    """The wording of an alert without its live numbers, so the id stays the same while prices move."""
    head = text.split(":")[0] if ":" in text[:48] else text
    return re.sub(r"[-+]?[\d.,]+[%×x]?", "#", head).strip()[:60]




class ApiRoutes:
    def __init__(self, win, need=None):
        self.win = win
        self.need = need or (lambda _what: None)
        self.started_at = time.time()
        self._first_seen: dict[str, datetime] = {}
        self._chain_locks = {"NIFTY": threading.Lock(), "BANKNIFTY": threading.Lock()}
        self.table = {"/api/health": self.health, "/api/ticker": self.ticker, "/api/overview": self.overview,
                      "/api/banknifty": self.banknifty, "/api/chain": self.chain, "/api/risk": self.risk,
                      "/api/fii": self.fii, "/api/positions": self.positions, "/api/alerts": self.alerts,
                      "/api/news": self.news, "/api/global": self.global_markets, "/api/signals": self.signals}

    def get(self, path: str):
        """'/api/chain?und=NIFTY' → JSON-safe dict. Raises KeyError for an unknown endpoint."""
        url = urlparse(path)
        route = self.table[url.path]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        return _clean(route(q))

    def health(self, _q):
        from market_hours import market_open, now_ist
        kind, text, _c = self.win.broker.state()
        return {"version": VERSION, "app": "NSE Quant Terminal", "server_time": now_ist(), "market_open": market_open(),
                "shoonya": {"state": kind, "text": text}, "mongodb": self.win.svc.persistent,
                "uptime_s": int(time.time() - self.started_at)}

    def ticker(self, _q):
        quotes, extra = getattr(self.win, "_ticker_state", ({}, {}))
        names = {"NIFTY BANK": "BANKNIFTY", "^NSEI": "NIFTY 50", "INDIA VIX": "India VIX"}
        out = []
        for sym in ("NIFTY BANK", "^NSEI", "INDIA VIX"):
            q = quotes.get(sym) or {}
            if q.get("last"):
                ch = (q["last"] - q["prev"]) if q.get("prev") else None
                out.append({"symbol": sym, "name": names[sym], "last": q["last"], "prev": q.get("prev"),
                            "chg": ch, "pct": ch / q["prev"] * 100 if ch is not None else None,
                            "high": q.get("high"), "low": q.get("low")})
        return {"quotes": out, **extra, "time": datetime.now()}

    def overview(self, _q):
        from shoonya_feed import INDEX_TOKENS, VIX_TOKEN
        feed = self.win.broker.feed
        snap = (self.win._last_snap or {}).get("quotes") or {}
        names = {"^NSEI": "NIFTY 50", "NIFTY BANK": "BANKNIFTY", "NIFTY NEXT 50": "NIFTY Next 50",
                 "NIFTY FINANCIAL SERVICES": "FINNIFTY", "NIFTY MIDCAP SELECT": "MIDCPNIFTY"}
        rows = []
        for sym, key in list(INDEX_TOKENS.items()) + [("INDIA VIX", VIX_TOKEN)]:
            t = feed.tick(key) if feed.live else None
            if t and feed._fresh(t):
                last, prev, hi, lo = (_f(t.get("lp")), _f(t.get("c")), _f(t.get("h")), _f(t.get("l")))
                live = True
            else:
                s = snap.get(sym) or ((self.win._last_snap or {}).get("vix") if sym == "INDIA VIX" else {}) or {}
                last, prev, hi, lo, live = s.get("last"), s.get("prev"), s.get("high"), s.get("low"), False
            if not last:
                continue
            ch = last - prev if prev else None
            rows.append({"symbol": sym, "name": names.get(sym, "India VIX"), "last": last, "prev": prev, "chg": ch,
                         "pct": ch / prev * 100 if ch is not None and prev else None, "high": hi, "low": lo,
                         "live": live})
        fr = self.win.fii_view.res or {}
        head = {}
        if fr.get("available"):
            f = fr["frame"].iloc[-1]
            head = {"as_of": fr["as_of"], "fii_cash": f.get("fii_cash"), "dii_cash": f.get("dii_cash"),
                    "score": fr["score"], "bias": fr["bias"]}
        return {"indices": rows, "fii": head, "source": "Shoonya real-time" if feed.live else "NSE delayed"}

    def banknifty(self, _q):
        bnf = self.win.bnf_svc
        if not bnf.symbols or time.time() - self.win._bnf_at > (120 if self.win.broker.live else 60):
            self.need("banknifty")
        if not bnf.symbols:
            return {"loading": True}
        s = bnf.live_snapshot()
        return {"index": s["index"], "rows": [{k: v for k, v in r.items() if k != "spark"} | {"spark": r["spark"][-60:]}
                                              for r in s["rows"]],
                "explained": s["explained"], "up_weight": s["up_weight"], "adv": s["adv"], "dec": s["dec"],
                "alerts": [{"level": lv, "text": t} for lv, t in s["alerts"]], "fit": s["fit"], "live": s["live"]}

    def chain(self, q):
        und = (q.get("und") or "NIFTY").upper()
        if und not in self._chain_locks:
            raise ValueError("und must be NIFTY or BANKNIFTY")
        n = int(q.get("n") or 15)
        opts = self.win.options
        res = opts.fresh(und, 20 if self.win.broker.live else 90)
        if res is None:
            with self._chain_locks[und]:
                res = opts.fresh(und, 20 if self.win.broker.live else 90) or opts.analyse(und)
        rows = res.get("chain") or []
        i = min(range(len(rows)), key=lambda j: abs(rows[j]["strike"] - res["spot"])) if rows else 0
        keep = ("strike", "pcr", "ce_oi", "ce_chg_oi", "ce_ltp", "ce_chg_pct", "ce_iv", "ce_delta", "ce_buildup",
                "pe_oi", "pe_chg_oi", "pe_ltp", "pe_chg_pct", "pe_iv", "pe_delta", "pe_buildup")
        return {"underlying": und, "expiry": res["expiry"], "dte": res["dte"], "spot": res["spot"], "atm": res["atm"],
                "atm_iv": res.get("atm_iv"), "sigma_move": res.get("sigma_move"), "straddle": res.get("straddle"),
                "lot": res.get("lot"), "vix": res.get("vix"), "skew": res.get("skew"), "source": res.get("source"),
                "timestamp": res.get("timestamp"), "summary": res.get("chain_summary"),
                "verdict": res.get("verdict"), "score": res.get("score"),
                "rows": [{k: r.get(k) for k in keep} for r in rows[max(i - n, 0): i + n + 1]]}

    def risk(self, _q):
        if time.time() - self.win._risk_at > 60:
            self.need("risk")
        out = {}
        for und, r in self.win.risk_view.results.items():
            out[und] = {k: r[k] for k in ("spot", "expiry", "dte", "regime", "bias", "danger_up", "danger_down",
                                          "alerts", "prohibitions", "safe", "positions", "call_wall", "put_wall",
                                          "atm_iv", "vix", "sigma", "window_min", "source", "timestamp")}
            out[und]["gex"] = {"net_cr": r["gex"]["net"] / 1e7, "flip": r["gex"].get("flip")}
            out[und]["books"] = r.get("books") or []
        return {"indices": out, "updated": self.win._risk_at, "account": getattr(self.win, "_account", None)}

    def fii(self, _q):
        from fii_analysis import strike_zones
        r = self.win.fii_view.res or {}
        if not r.get("available"):
            return {"available": False}
        f = r["frame"].tail(15).iloc[::-1]
        recent = [{"date": ts, "fii_cash": x.get("fii_cash"), "dii_cash": x.get("dii_cash"),
                   "fii_long_pct": x.get("fii_long_pct"), "fii_fut_net": x.get("fii_fut_net"), "pcr": x.get("pcr"),
                   "score": x.get("score"), "nifty": x.get("nifty")} for ts, x in f.iterrows()]
        s = r.get("setup") or {}
        setup = {"date": s.get("date"), "direction": s.get("direction"), "lean": s.get("lean"),
                 "conditions": [{"name": a, "value": b, "ok": c} for a, b, c in s.get("conditions") or []],
                 "record": s.get("record")}
        zones = {u: strike_zones(r["bias"], self.win.fii_view.zone_res.get(u)) for u in ("NIFTY", "BANKNIFTY")}
        return {"available": True, "as_of": r["as_of"], "sessions": r["sessions"], "score": r["score"], "bias": r["bias"],
                "conviction": r["conviction"], "agreement": r.get("agreement"), "playbook": r["playbook"],
                "warnings": r["warnings"], "components": r["components"], "evidence": r.get("evidence"),
                "setup": setup, "zones": zones, "recent": recent}

    def positions(self, _q):
        d = self.win.trades.data or {}
        if not d:
            self.need("trades")
            return {"loading": True}
        groups = []
        for g in d.get("positions") or []:
            groups.append({"und": g["und"], "expiry": g["expiry"], "dte": g.get("dte"), "structure": g["structure"],
                           "defined_risk": g["defined_risk"], "credit": g["credit"], "open_pnl": g["open_pnl"],
                           "mtm": g["mtm"], "captured": g.get("captured"), "spot": g.get("spot"),
                           "alerts": [{"level": a, "text": t} for a, t in g["alerts"]],
                           "legs": [{"tsym": l.get("tsym"), "qty": l.get("qty"), "avg": l.get("avg"), "ltp": l.get("lp"),
                                     "mtm": l.get("mtm"), "kind": l.get("kind"), "strike": l.get("strike")}
                                    for l in g["legs"]]})
        at = self.win.trades_svc.last_book.get("at")
        return {"positions": groups, "today": d.get("today"), "limits": d.get("limits"), "status": d.get("status"),
                "synced_at": at, "metrics": {k: v for k, v in (d.get("metrics") or {}).items()
                                             if k not in ("best", "worst")}}

    def news(self, q):
        hours = min(int(q.get("hours") or 24), 72)
        items = self.win.news_svc.recent(hours=hours, impact=q.get("impact") or None, tag=q.get("tag") or None,
                                         limit=int(q.get("limit") or 60))
        keep = ("id", "title", "summary", "link", "source", "sources", "published", "impact", "tags", "banks", "tone",
                "alerted", "related")
        return {"items": [{k: d.get(k) for k in keep} for d in items], "status": self.win.news_svc.status}

    def global_markets(self, _q):
        w = self.win.watch
        if not w.quotes:
            self.need("global")
        keep = ("key", "name", "group", "unit", "threshold", "last", "prev", "chg", "pct", "move", "at", "session",
                "stale", "offline", "spark")
        return {"quotes": [{k: q.get(k) for k in keep} for q in w.quotes], "cue": w.cue,
                "checked_at": w.global_at, "failed": w.failed,
                "alerts": [_alert_out(a) for a in w.recent_alerts(1, ("GLOBAL",))]}

    def signals(self, _q):
        w = self.win.watch
        quotes = (getattr(self.win, "_ticker_state", ({}, {}))[0]) or {}
        out = {}
        for und, sym in (("NIFTY", "^NSEI"), ("BANKNIFTY", "NIFTY BANK")):
            calls = w.today_calls(und)
            right, n = w.hit_rate(calls)
            st = w.level_status(und)
            spot = (quotes.get(sym) or {}).get("last")
            out[und] = {"spot": spot, "call": w.latest_call(und),
                        "calls": [{k: c.get(k) for k in ("slot", "dir", "direction", "score", "confidence", "spot",
                                                         "spot_after", "outcome")} for c in calls],
                        "hits": right, "graded": n,
                        "levels": [{"name": m["name"], "also": m.get("also", []), "price": m["price"],
                                    "status": st.get(round(m["price"]))} for m in w.levels.get(und) or []]}
        return {**out, "alerts": [_alert_out(a) for a in w.recent_alerts(1, ("LEVEL",))],
                "writer": [_alert_out(a) for a in w.recent_alerts(1, ("WRITER",))], "time": datetime.now()}

    def alerts(self, _q):
        """Every active alert with a stable id; the phone raises a notification for unseen ids."""
        items = []
        for und, r in self.win.risk_view.results.items():
            for a in r["alerts"]:
                if a["level"] in ("RED", "AMBER"):
                    items.append({"id": _id("risk", date.today(), und, a["level"], a["title"]), "level": a["level"],
                                  "source": "Writer risk monitor", "title": f"{und}: {a['title']}",
                                  "text": " · ".join(a["signals"][:2]) + f" → {a['action']}"})
        acc = getattr(self.win, "_account", None) or {}
        for a in acc.get("alerts", []):
            items.append({"id": _id("acct", date.today(), _kind(a["title"])), "level": a["level"], "source": "Account limits",
                          "title": a["title"], "text": a["text"]})
        sync = self.win.sync_svc.last.get("setup_alert") or {}
        if getattr(self.win, "_setup_active", None) and sync.get("key"):
            items.append({"id": _id("setup", sync["key"]), "level": "SETUP", "source": "Evening setup",
                          "title": "Squeeze-loading setup " + ("▲ UP" if self.win._setup_active == "up" else "▼ DOWN"),
                          "text": sync.get("message", "")})
        if self.win.bnf_svc.symbols:
            for lv, t in self.win.bnf_svc.live_snapshot()["alerts"]:
                if lv == "red":
                    items.append({"id": _id("bnf", date.today(), _kind(t)), "level": "RED",
                                  "source": "BANKNIFTY monitor", "title": "BANKNIFTY breadth", "text": t})
        for g in (self.win.trades.data or {}).get("positions") or []:
            for a, t in g["alerts"]:
                if a == "stop":
                    items.append({"id": _id("pos", date.today(), g["und"], g["expiry"], _kind(t)), "level": "RED",
                                  "source": "My positions", "title": f"EXIT · {g['und']} {g['structure']}", "text": t})
        for d in self.win.news_svc.recent(hours=6, impact="HIGH", limit=40):
            if d.get("alerted"):
                extra = f" (+{d['related']} related)" if d.get("related") else ""
                items.append({"id": _id("news", d["id"]), "level": "NEWS", "source": d.get("source", "News"),
                              "title": "Market news · " + "/".join(t for t in d.get("tags", [])
                                                                   if t in ("BANKNIFTY", "NIFTY", "RBI", "MACRO")),
                              "text": d["title"] + extra, "link": d.get("link")})
        w = self.win.watch
        for a in w.recent_alerts(1, ("WRITER",)):
            items.append({"id": a["id"], "level": "WRITER", "source": "Writer signals", "title": a["title"],
                          "text": a["text"], "severity": a.get("level"), "signal": a.get("signal")})
        for a in w.recent_alerts(1, ("GLOBAL", "LEVEL")):
            ts = pd.Timestamp(a["raised_at"])
            age = (pd.Timestamp.now(tz="UTC") - (ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC"))).total_seconds()
            if age < (12 if a["kind"] == "GLOBAL" else 3) * 3600:
                items.append({"id": a["id"], "level": a["kind"], "source": "Global markets" if a["kind"] == "GLOBAL"
                              else "Level break", "title": a["title"], "text": a["text"], "severity": a.get("level")})
        for und in ("NIFTY", "BANKNIFTY"):
            c = w.latest_call(und)
            if c and c.get("day") == str(date.today()):
                items.append({"id": _id("dir", und, c["slot"]), "level": "DIRECTION", "source": "Direction call",
                              "title": f"{und} {c['slot'][-5:]} · {c['direction']} ({c['confidence']})",
                              "text": c["idea"], "dir": c["dir"], "changed": bool(c.get("changed"))})
        # newest first: each alert carries the time it was raised (or first seen, for standing alerts)
        now = datetime.now().astimezone()
        news_at = {_id("news", d["id"]): d.get("alerted_at") or d.get("published")
                   for d in self.win.news_svc.recent(hours=6, impact="HIGH", limit=40) if d.get("alerted")}
        raised = {a["id"]: a.get("raised_at") for a in w.recent_alerts(1, ("GLOBAL", "LEVEL", "WRITER"))}
        for it in items:
            t = raised.get(it["id"]) or news_at.get(it["id"])
            if it["level"] == "DIRECTION":
                c = w.latest_call("BANKNIFTY" if it["title"].startswith("BANKNIFTY") else "NIFTY") or {}
                t = c.get("at")
            if t is None:
                t = self._first_seen.setdefault(it["id"], now)
            ts = pd.Timestamp(t)
            it["at"] = (ts.tz_localize("UTC") if ts.tzinfo is None else ts).tz_convert(config.IST).isoformat()
        items.sort(key=lambda x: x["at"], reverse=True)
        return {"alerts": items, "time": datetime.now()}



def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None

