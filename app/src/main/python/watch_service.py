"""State and storage for the market-watch engines (market_watch.py): global quotes, level
breaks and the 15-minute direction calls.  Everything is kept in MongoDB:

  global_quotes    one doc per instrument and session (latest quote of that session)
  market_alerts    every alert raised (GLOBAL / LEVEL), by stable id — an id alerts once
  direction_calls  one doc per index and 15-minute slot, graded at the next slot

Blocking methods are marked; call them from worker threads.  `check_levels` is cheap and runs
on the GUI thread.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

import pandas as pd

import config
import market_watch as mw

log = logging.getLogger(__name__)

SPOT_SYMBOL = {"NIFTY": "^NSEI", "BANKNIFTY": "NIFTY BANK"}


class WatchService:
    def __init__(self, svc, options, market):
        self.svc, self.options, self.market = svc, options, market
        self.quotes: list[dict] = []
        self.failed: list[str] = []
        self.cue: dict = {}
        self.global_at: datetime | None = None
        self.levels: dict[str, list[dict]] = {}
        self.levels_at: dict[str, datetime] = {}
        self.watcher = mw.LevelWatcher()
        self.calls: dict[str, dict] = {}            # latest call per index
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._orange: dict[str, tuple[float, float]] = {}

    @property
    def store(self):
        return self.svc.store

    # ---------------------------------------------------------------- alerts log
    def _fresh_ids(self, alerts: list[dict]) -> list[dict]:
        """Alerts not raised before (memory, then MongoDB); stores the new ones."""
        new = []
        for a in alerts:
            if a["id"] in self._seen:
                continue
            self._seen.add(a["id"])
            try:
                if self.store.find_docs("market_alerts", {"id": a["id"]}, limit=1):
                    continue
            except Exception:
                pass
            new.append(a)
        if new:
            now = datetime.now().astimezone()
            docs = [{**a, "raised_at": now, "day": str(now.date())} for a in new]
            try:
                self.store.upsert_docs("market_alerts", docs)
            except Exception as exc:
                log.warning("market_alerts not stored: %s", exc)
        return new

    def recent_alerts(self, days: int = 1, kinds=("GLOBAL", "LEVEL")) -> list[dict]:
        out = []
        for i in range(days):
            day = str((pd.Timestamp.now(tz=config.IST) - pd.Timedelta(days=i)).date())
            try:
                out += self.store.find_docs("market_alerts", {"day": day})
            except Exception:
                pass
        out = [a for a in out if a.get("kind") in kinds]
        return sorted(out, key=lambda a: _utc(a["raised_at"]), reverse=True)

    # ---------------------------------------------------------------- global markets (blocking)
    def poll_global(self) -> tuple[list[dict], list[dict]]:
        quotes, failed = mw.fetch_global()
        if not quotes:                                            # offline: last stored session per instrument
            quotes = self.stored_global()
        cue = mw.global_cue(quotes)
        with self._lock:
            self.quotes, self.failed, self.cue = quotes, failed, cue
            self.global_at = datetime.now()
        live = [q for q in quotes if not q.get("offline")]
        if live:
            try:
                self.store.upsert_docs("global_quotes", [{k: v for k, v in q.items() if k != "spark"} for q in live
                                                         if q.get("session")])
            except Exception as exc:
                log.warning("global_quotes not stored: %s", exc)
        return quotes, self._fresh_ids(mw.global_alerts(live))

    def stored_global(self) -> list[dict]:
        try:
            docs = self.store.find_docs("global_quotes", sort="at", desc=True, limit=400)
        except Exception:
            return []
        best: dict[str, dict] = {}
        for d in docs:
            best.setdefault(d["key"], d)
        order = {g[0]: i for i, g in enumerate(mw.GLOBAL)}
        out = []
        for k, d in sorted(best.items(), key=lambda kv: order.get(kv[0], 99)):
            d = dict(d, spark=[], offline=True, stale=True)
            out.append(d)
        return out

    # ---------------------------------------------------------------- key levels
    def refresh_levels(self, und: str, spot: float | None = None) -> list[dict]:
        """(blocking) Rebuild today's levels for one index."""
        sym = SPOT_SYMBOL[und]
        daily = self.svc.load_ohlcv(sym, "1D")
        sr = self.svc.latest_levels([sym]).get(sym)
        chain = self.options.fresh(und, 6 * 3600)
        spot = spot or (chain or {}).get("spot") or (float(daily["close"].iloc[-1]) if len(daily) else None)
        if not spot:
            return []
        lv = mw.build_levels(spot, daily, sr, chain, self._opening_range(und))
        with self._lock:
            self.levels[und] = lv
            self.levels_at[und] = datetime.now()
        return lv

    def _opening_range(self, und: str) -> tuple[float, float] | None:
        """High / low of 9:15-9:30 today from the stored 1-minute candles (after 9:30)."""
        now = mw.now_ist()
        if now.weekday() >= 5 or (now.hour, now.minute) < (9, 30):
            return None
        hit = self._orange.get(und)
        if hit and self._orange.get(und + ":day") == now.date():
            return hit
        sym = SPOT_SYMBOL[und]
        start = pd.Timestamp(now.date(), tz=config.IST) + pd.Timedelta(hours=9, minutes=15)
        try:
            df = self.store.load_ohlcv(sym, "1m", start=start)
            if df is None or df.empty or df.index[0] > start + pd.Timedelta(minutes=5):
                self.market.refresh_intraday(sym, "1m")
                df = self.store.load_ohlcv(sym, "1m", start=start)
        except Exception as exc:
            log.info("Opening range for %s unavailable: %s", und, exc)
            return None
        if df is None or df.empty:
            return None
        df = df[df.index < start + pd.Timedelta(minutes=15)]
        if len(df) < 10:
            return None
        rng = (float(df["high"].max()), float(df["low"].min()))
        self._orange[und], self._orange[und + ":day"] = rng, now.date()
        return rng

    def check_levels(self, und: str, close: float, prev_close: float | None) -> list[dict]:
        """5-minute close against today's levels; returns alerts not raised before."""
        lv = self.levels.get(und) or []
        if not lv:
            return []
        return self._fresh_ids(self.watcher.check(und, close, lv, prev_close))

    def level_status(self, und: str) -> dict:
        return self.watcher.status.get(und, {})

    # ---------------------------------------------------------------- 15-minute direction call (blocking)
    def make_call(self, und: str, slot: datetime, risk: dict | None) -> dict:
        res = self.options.fresh(und, 120) or self.options.analyse(und)
        hist = self.options.intraday_history(und)
        prev = mw.pick_15m(hist, res["expiry"])
        day = str(slot.date())
        breaks = [a for a in self.recent_alerts(1, ("LEVEL",)) if a.get("und") == und and a.get("day") == day]
        breaks.sort(key=lambda a: _utc(a["raised_at"]))
        call = mw.direction_call(res, prev, self.levels.get(und), breaks, risk)
        call.update({"slot": slot.strftime("%Y-%m-%d %H:%M"), "day": day, "at": datetime.now().astimezone()})
        earlier = self.today_calls(und, day)
        if earlier:
            last = earlier[-1]
            if not last.get("outcome"):
                last["outcome"] = mw.grade(last, res["spot"])
                last["spot_after"] = res["spot"]
                self._save_call(last)
            call["changed"] = last["dir"] != call["dir"]
        else:
            call["changed"] = True
        self._save_call(call)
        with self._lock:
            self.calls[und] = call
        return call

    def _save_call(self, call: dict):
        try:
            self.store.upsert_docs("direction_calls", [call])
        except Exception as exc:
            log.warning("direction call not stored: %s", exc)

    def today_calls(self, und: str, day: str | None = None) -> list[dict]:
        day = day or str(mw.now_ist().date())
        try:
            docs = self.store.find_docs("direction_calls", {"und": und, "day": day})
        except Exception:
            return []
        return sorted(docs, key=lambda d: d["slot"])

    def latest_call(self, und: str) -> dict | None:
        c = self.calls.get(und)
        if c:
            return c
        docs = self.today_calls(und)
        return docs[-1] if docs else None

    @staticmethod
    def hit_rate(calls: list[dict]) -> tuple[int, int]:
        graded = [c for c in calls if c.get("outcome") in ("right", "wrong")]
        return sum(c["outcome"] == "right" for c in graded), len(graded)


def _utc(t) -> pd.Timestamp:
    """MongoDB returns naive UTC datetimes; the in-memory store keeps aware ones."""
    ts = pd.Timestamp(t)
    return ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")
