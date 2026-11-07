"""Option-chain analysis for NIFTY / BANKNIFTY, independent of any screen.

Source order: Shoonya real-time chain → NSE snapshot → the last chain stored in
MongoDB (offline).  Each live chain is stored as a snapshot (every 5 min) so it
can be studied later.  The latest result per underlying is kept in `latest`,
which other features read (fill context in Trades, chart levels, FII/DII
strike zones).  Methods block; call them from worker threads.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

import pandas as pd

import risk_engine
from data_fetcher import pick_expiry
from indicators import analyze
from options import UNDERLYINGS, analyze_chain
from shoonya_feed import INDEX_TOKENS, VIX_TOKEN

log = logging.getLogger(__name__)

SPOT_SYMBOL = {"NIFTY": "^NSEI", "BANKNIFTY": "NIFTY BANK"}
STEP = {"NIFTY": 50, "BANKNIFTY": 100}


class OptionsService:
    def __init__(self, svc, broker, market):
        self.svc, self.broker, self.market = svc, broker, market
        self.latest: dict[str, tuple[float, dict]] = {}          # underlying -> (time, analysis)
        self._engine: dict[str, tuple[float, dict, pd.DataFrame]] = {}
        self._expiries: dict[str, tuple[float, list[str]]] = {}
        self._lock = threading.Lock()
        self.history: dict[str, deque] = {}                       # underlying -> intraday chain observations
        self._seeded: set[str] = set()

    def fresh(self, underlying: str, max_age: float = 900) -> dict | None:
        hit = self.latest.get(underlying)
        return hit[1] if hit and time.time() - hit[0] < max_age else None

    def _expiry_list(self, underlying: str) -> list[str]:
        hit = self._expiries.get(underlying)
        if hit is None or time.time() - hit[0] > 3600:
            hit = self._expiries[underlying] = (time.time(), self.svc.option_expiries(underlying))
        return hit[1]

    def _shoonya_chain(self, underlying: str, expiry: str | None):
        feed = self.broker.feed
        expiries = self._expiry_list(underlying)
        exp = pick_expiry(expiries, expiry or None)
        t = feed.tick(INDEX_TOKENS[SPOT_SYMBOL[underlying]])
        spot = float((t or {}).get("lp") or 0) if feed._fresh(t) else 0
        if not spot:
            raise RuntimeError("no fresh index tick")
        first = (underlying, exp) not in feed.chains
        feed.ensure_chain(underlying, exp, spot, STEP[underlying])
        for _ in range(30 if first else 0):           # let the first ticks arrive
            if feed.chain_json(underlying, exp, expiries)[1] > 0.8 * len(feed.chains[(underlying, exp)]):
                break
            time.sleep(0.1)
        js, live_legs = feed.chain_json(underlying, exp, expiries)
        if live_legs == 0:
            raise RuntimeError("no live option ticks")
        return js, exp

    def analyse(self, underlying: str, expiry: str | None = None, record: bool = True) -> dict:
        """`record=False` (e.g. a second expiry for position checks) leaves `latest` and the
        intraday history untouched."""
        js = note = None
        source, exp = "nse", expiry
        if self.broker.feed.live:
            try:
                js, exp = self._shoonya_chain(underlying, expiry)
                source = "shoonya"
            except Exception as exc:
                log.warning("Shoonya option chain failed, using NSE: %s", exc)
                note = f"Shoonya unavailable: {exc}"
        if js is None:
            try:
                js, exp, _ = self.svc.option_chain(underlying, expiry or None)
            except Exception as exc:
                stored = self.market.last_chain(underlying, expiry or None) or self.market.last_chain(underlying)
                if not stored:
                    raise RuntimeError(f"No live or stored option chain for {underlying}: {exc}") from exc
                js, exp, at = stored
                source, note = "offline", f"offline — last stored chain from {at}"
        if source != "offline":
            self.market.save_chain(underlying, exp, js, source)
        engine, daily = self._engine_read(underlying)
        res = analyze_chain(underlying, js, exp, daily, self._vix(), engine)
        res["source"], res["source_note"] = source, note
        if not record:
            return res
        with self._lock:
            self.latest[underlying] = (time.time(), res)
            if source != "offline":
                self.history.setdefault(underlying, deque(maxlen=400)).append(risk_engine.compact(res))
        return res

    def intraday_history(self, underlying: str) -> list[dict]:
        """Today's observations of the chain: in-memory ones plus, the first time,
        snapshots stored in MongoDB earlier today (so a restart keeps the comparison)."""
        if underlying not in self._seeded:
            self._seeded.add(underlying)
            day = pd.Timestamp.now(tz="Asia/Kolkata").normalize()
            seeded = []
            for d in self.svc.store.find_docs("option_chain_snapshots", {"underlying": underlying}, sort="ts"):
                ts = pd.Timestamp(d["ts"])
                ts = ts if ts.tzinfo else ts.tz_localize("UTC")
                if ts.tz_convert("Asia/Kolkata") < day:
                    continue
                js = {"records": {"underlyingValue": d.get("spot"), "data": d.get("data") or [],
                                  "expiryDates": d.get("expiries") or [d["expiry"]]}}
                obs = risk_engine.compact_from_js(js, d["expiry"], ts.timestamp())
                if obs:
                    seeded.append(obs)
            with self._lock:
                dq = self.history.setdefault(underlying, deque(maxlen=400))
                merged = sorted(seeded + list(dq), key=lambda h: h["ts"])
                dq.clear()
                dq.extend(merged)
        with self._lock:
            return list(self.history.get(underlying) or [])

    def _engine_read(self, underlying: str):
        hit = self._engine.get(underlying)
        if hit is None or time.time() - hit[0] > 600:      # the EOD engine read changes slowly
            sym = UNDERLYINGS[underlying]["daily"]
            daily = self.svc.load_ohlcv(sym, "1D")
            engine = analyze(sym, "1D", daily, daily, self.svc.load_fii_dii(), self.svc.load_macro()) \
                if len(daily) > 60 else {}
            hit = self._engine[underlying] = (time.time(), engine, daily)
        return hit[1], hit[2]

    def _vix(self):
        macro = self.svc.load_macro()
        vix = macro["indiavix"].dropna() if "indiavix" in macro else None
        feed = self.broker.feed
        vt = feed.tick(VIX_TOKEN) if feed.live else None
        if vix is not None and vt and vt.get("lp"):
            vix = pd.concat([vix, pd.Series([float(vt["lp"])], index=[pd.Timestamp.now().normalize()])])
        return vix
