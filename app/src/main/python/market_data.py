"""Market data with one rule: Shoonya first, then the public sources, and
everything kept in MongoDB so the app still works offline.

* Intraday candles (1m for the intraday chart, 15m / 1H for index analysis):
  Shoonya TPSeries for every instrument Shoonya streams (NIFTY, BANKNIFTY,
  Next 50, Fin Service, Midcap Select, VIX and the ETFs); Yahoo otherwise.
  Every pull is upserted into `index_ohlcv`, and reads come from there.
* Option chains: snapshots are written to `option_chain_snapshots` (throttled)
  so the chain can be re-opened and studied offline.

All methods are blocking; call them from a worker thread.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import pandas as pd

import config
from data_fetcher import fetch_intraday_1m, fetch_ohlcv

log = logging.getLogger(__name__)

TF_MINUTES = {"1m": 1, "15m": 15, "1H": 60}
SHOONYA_DEPTH_DAYS = {"1m": 9, "15m": 58, "1H": 175}      # what TPSeries reliably returns
SNAPSHOT_EVERY = 300                                        # seconds between stored chain snapshots
VIX_SYMBOL = "INDIA VIX"


class MarketData:
    def __init__(self, svc, broker):
        self.svc, self.broker = svc, broker
        self._snap_at: dict[tuple, float] = {}

    @property
    def store(self):
        return self.svc.store

    @property
    def feed(self):
        return self.broker.feed

    def shoonya_key(self, symbol: str) -> str | None:
        return self.feed.key_for(symbol) if self.feed.live else None

    # ---------------------------------------------------------------- intraday candles
    def refresh_intraday(self, symbol: str, tf: str) -> str:
        """Bring stored `tf` candles up to date. Returns the source used:
        'shoonya', 'yahoo', or 'offline' (nothing fetched, stored copy only)."""
        last = self.store.latest_timestamp(symbol, tf)
        key = self.shoonya_key(symbol)
        if key:
            depth = SHOONYA_DEPTH_DAYS[tf]
            start = time.time() - depth * 86400
            if last is not None:
                start = max(start, pd.Timestamp(last).timestamp() - 2 * 86400)   # small overlap; upserts dedupe
            try:
                df = self.feed.time_series_df(key, start, interval=TF_MINUTES[tf])
                if len(df):
                    self.store.upsert_ohlcv(symbol, tf, df)
                return "shoonya"
            except Exception as exc:
                log.warning("Shoonya %s %s candles failed, falling back: %s", symbol, tf, exc)
        try:
            if tf == "1m":
                bars = fetch_intraday_1m(symbol, 7)
                if bars:
                    df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close"]).set_index("timestamp")
                    df.index = pd.DatetimeIndex(df.index).tz_localize(config.IST)
                    df["volume"] = 0.0
                    self.store.upsert_ohlcv(symbol, tf, df)
            elif config.supports_intraday(symbol):
                start = (pd.Timestamp(last) - pd.Timedelta(days=7)) if last is not None else None
                df = fetch_ohlcv(symbol, tf, start=start)
                if len(df):
                    self.store.upsert_ohlcv(symbol, tf, df)
            else:
                return "offline"
            return "yahoo"
        except Exception as exc:
            log.warning("Yahoo %s %s candles failed, using stored data: %s", symbol, tf, exc)
            return "offline"

    def candles_1m(self, symbols, days: int = 9) -> tuple[dict[str, list[tuple]], str]:
        """1-minute bars per symbol for the intraday chart (IST-naive datetimes), plus a
        source label. Refreshes from Shoonya / Yahoo first, then reads MongoDB."""
        sources = {self.refresh_intraday(s, "1m") for s in symbols}
        since = datetime.now() - timedelta(days=days + 3)
        out = {}
        for s in symbols:
            df = self.store.load_ohlcv(s, "1m", start=since)
            if df is None or df.empty:
                out[s] = []
                continue
            idx = df.index.tz_convert(config.IST).tz_localize(None) if df.index.tz is not None else df.index
            out[s] = [(ts.to_pydatetime(), float(o), float(h), float(l), float(c))
                      for ts, o, h, l, c in zip(idx, df["open"], df["high"], df["low"], df["close"])]
        if sources == {"shoonya"}:
            src = "Shoonya real-time · 1-min candles + live ticks"
        elif "shoonya" in sources:
            src = "Shoonya + Yahoo 1-min"
        elif sources == {"offline"}:
            src = "Offline — stored 1-min candles from MongoDB"
        else:
            src = "Yahoo 1-min (delayed) — connect Shoonya for real-time"
        return out, src

    # ---------------------------------------------------------------- option chain snapshots
    def save_chain(self, underlying: str, expiry: str, js: dict, source: str, force: bool = False) -> bool:
        """Store the chain (NSE-shaped JSON) at most every SNAPSHOT_EVERY seconds per expiry."""
        k = (underlying, expiry)
        if not force and time.time() - self._snap_at.get(k, 0) < SNAPSHOT_EVERY:
            return False
        rec = js.get("records") or {}
        slim = []
        for d in rec.get("data", []):
            row = {"strikePrice": d.get("strikePrice"), "expiryDates": d.get("expiryDates")}
            for kind in ("CE", "PE"):
                o = d.get(kind)
                if o:
                    row[kind] = {f: o.get(f) for f in ("openInterest", "changeinOpenInterest", "lastPrice", "change",
                                                        "buyPrice1", "sellPrice1", "totalTradedVolume",
                                                        "impliedVolatility", "underlyingValue")}
            slim.append(row)
        doc = {"underlying": underlying, "expiry": expiry, "ts": datetime.now().astimezone(),
               "source": source, "spot": rec.get("underlyingValue"), "timestamp": rec.get("timestamp"),
               "expiries": rec.get("expiryDates") or [expiry], "data": slim}
        try:
            self.store.upsert_docs("option_chain_snapshots", [doc])
            self._snap_at[k] = time.time()
            return True
        except Exception as exc:
            log.warning("Chain snapshot not stored: %s", exc)
            return False

    def last_chain(self, underlying: str, expiry: str | None = None) -> tuple[dict, str, str] | None:
        """Most recent stored chain as (NSE-shaped json, expiry, stored-at text)."""
        q = {"underlying": underlying}
        if expiry:
            q["expiry"] = expiry
        docs = self.store.find_docs("option_chain_snapshots", q, sort="ts", desc=True, limit=1)
        if not docs:
            return None
        d = docs[0]
        js = {"records": {"underlyingValue": d.get("spot"), "timestamp": d.get("timestamp"),
                          "expiryDates": d.get("expiries") or [d["expiry"]], "data": d.get("data") or []}}
        ts = pd.Timestamp(d["ts"])
        ts = ts.tz_convert(config.IST) if ts.tzinfo else ts
        return js, d["expiry"], f"{ts:%d-%b %H:%M}"

    def chain_history(self, underlying: str, expiry: str, day: datetime | None = None) -> list[dict]:
        """Stored snapshots for one expiry (today by default): for the intraday OI trend."""
        docs = self.store.find_docs("option_chain_snapshots", {"underlying": underlying, "expiry": expiry},
                                    sort="ts")
        day = (day or datetime.now()).date()
        out = []
        for d in docs:
            ts = pd.Timestamp(d["ts"])
            ts = ts.tz_convert(config.IST) if ts.tzinfo else ts
            if ts.date() != day:
                continue
            ce = sum(float((r.get("CE") or {}).get("openInterest") or 0) for r in d.get("data", []))
            pe = sum(float((r.get("PE") or {}).get("openInterest") or 0) for r in d.get("data", []))
            out.append({"ts": ts, "spot": d.get("spot"), "ce_oi": ce, "pe_oi": pe, "pcr": pe / ce if ce else None})
        return out
