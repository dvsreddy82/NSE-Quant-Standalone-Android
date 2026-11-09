"""Daily summary of sudden volume changes in NIFTY 50 and BANKNIFTY stocks.

Universe: NSE's official NIFTY 50 and NIFTY Bank lists (refreshed weekly, stored in
`index_constituents`; a built-in list is the fallback).  Candles: 15-minute bars with volume per
stock ("<SYMBOL>.NS" in `index_ohlcv`), from Shoonya when connected and Yahoo otherwise.

For each stock and session it measures
  * day relative volume — the session's volume vs the average of the previous 20 sessions up to the
    same time of day (the whole day once the session is over);
  * sudden spikes — 15-minute bars whose volume is ≥ SPIKE_X times the median volume of the same
    15-minute slot over the previous 20 sessions (so the busy open and close are compared with
    themselves), and at least MIN_SHARE of an average day's volume;
  * what price did: the session's change, the bar move at the spike, where it closed vs the spike
    bar and within the day's range.
Stocks with day volume ≥ RVOL_X or at least one spike make the summary, strongest first.  A
summary is built once each trading day after the close (`volume_summary`, keyed by day) and
raised as one alert.  Volume with price is a description of the session, not a forecast.
Blocking; call from worker threads.
"""
from __future__ import annotations

import hashlib
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd

import config

log = logging.getLogger(__name__)

LISTS = {"NIFTY": ("NIFTY 50", "/content/indices/ind_nifty50list.csv"),
         "BANKNIFTY": ("NIFTY BANK", "/content/indices/ind_niftybanklist.csv")}
FALLBACK = {
    "NIFTY": ["ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE",
              "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM",
              "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC",
              "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC",
              "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATASTEEL",
              "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO", "TMPV"],
    "BANKNIFTY": ["AUBANK", "AXISBANK", "BANKBARODA", "CANBK", "FEDERALBNK", "HDFCBANK", "ICICIBANK", "IDFCFIRSTB",
                  "INDUSINDBK", "KOTAKBANK", "PNB", "SBIN", "UNIONBANK", "YESBANK"],
}
BASE_DAYS = 20          # sessions in the baseline
MIN_BASE = 5            # need at least this many earlier sessions
RVOL_X = 2.0            # day volume vs the 20-session average that makes the list
SPIKE_X = 5.0           # 15-minute bar vs the median of the same slot
MIN_SHARE = 0.03        # a spike bar must carry ≥ 3% of an average day's volume
BLOCK_X, BLOCK_MOVE = 10.0, 0.2   # a ≥10× bar that moves price < 0.2%: possibly a block / bulk deal
TOP_N = 10              # stocks per index in the summary
SUMMARY_AT = dtime(15, 35)


def store_symbol(sym: str) -> str:
    return f"{sym}.NS"


def _session(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = df.copy()
    df.index = df.index.tz_convert(config.IST) if df.index.tz is not None else df.index.tz_localize(config.IST)
    t = df.index.time
    df = df[(t >= dtime(9, 15)) & (t < dtime(15, 30))]
    df["volume"] = df["volume"].fillna(0).astype(float)
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def analyse_stock(df15: pd.DataFrame, day) -> dict | None:
    """One stock, one session, from 15-minute bars. None when the baseline is too thin."""
    df = _session(df15)
    if df.empty:
        return None
    days = sorted(set(df.index.date))
    if day not in days:
        return None
    prev_days = [d for d in days if d < day][-BASE_DAYS:]
    if len(prev_days) < MIN_BASE:
        return None
    cur = df[df.index.date == day]
    base = df[np.isin(df.index.date, prev_days)]
    if cur["volume"].sum() <= 0 or base["volume"].sum() <= 0:
        return None
    daily_vol = base.groupby(base.index.date)["volume"].sum()
    avg_day = float(daily_vol.mean())
    slot = lambda ix: (ix.hour * 60 + ix.minute - 555) // 15
    base_slot = base.groupby([slot(ix) for ix in base.index])["volume"].median()
    last_slot = slot(cur.index[-1])           # compare with earlier sessions up to the same time of day
    same_time = base[[slot(ix) <= last_slot for ix in base.index]].groupby(lambda ix: ix.date())["volume"].sum()
    avg_so_far = float(same_time.mean()) if len(same_time) else avg_day
    prev_close = float(df[df.index.date == prev_days[-1]]["close"].iloc[-1])
    vol = float(cur["volume"].sum())
    hi, lo, last = float(cur["high"].max()), float(cur["low"].min()), float(cur["close"].iloc[-1])
    spikes = []
    for ix, r in cur.iterrows():
        b = base_slot.get(slot(ix))
        if not b or b <= 0:
            continue
        x = r["volume"] / b
        if x >= SPIKE_X and r["volume"] >= MIN_SHARE * avg_day:
            move = (r["close"] - r["open"]) / r["open"] * 100 if r["open"] else 0.0
            spikes.append({"at": ix.strftime("%H:%M"), "x": round(float(x), 1), "volume": float(r["volume"]),
                           "block": bool(x >= BLOCK_X and abs(move) < BLOCK_MOVE),
                           "share": round(float(r["volume"] / avg_day * 100), 1), "move_pct": round(float(move), 2),
                           "price": float(r["close"]),
                           "after_pct": round(float((last - r["close"]) / r["close"] * 100), 2)})
    rng = hi - lo
    return {"rvol": round(vol / avg_so_far, 2), "volume": vol, "avg_volume": avg_day, "spikes": spikes,
            "as_of": cur.index[-1].strftime("%H:%M"),
            "chg_pct": round((last - prev_close) / prev_close * 100, 2), "close": last, "prev_close": prev_close,
            "high": hi, "low": lo, "close_in_range": round((last - lo) / rng * 100) if rng > 0 else 50,
            "bars": len(cur), "base_days": len(prev_days)}


def read(s: dict) -> str:
    """Plain-language read of volume with price (descriptive)."""
    txt = _read_full(s)
    if s.get("as_of", "15:15") < "15:15":         # session still running: no "close" yet
        txt = (txt.replace("closed near", "trading near").replace("firm close", "holding firm")
               .replace("weak close", "trading weak").replace("into the close", "so far"))
    return txt


def _read_full(s: dict) -> str:
    blocks = [k for k in s["spikes"] if k.get("block")]
    note = ""
    if blocks:
        b = max(blocks, key=lambda k: k["x"])
        note = f"{b['x']:.0f}× at {b['at']} with price flat — possibly a block / bulk deal"
        if s["rvol"] < RVOL_X and max(s["spikes"], key=lambda k: k["x"]) is b:
            return f"normal day volume, but {note}"
        return _read(s) + " · " + note
    return _read(s)


def _read(s: dict) -> str:
    up, strong = s["chg_pct"] > 0, s["rvol"] >= 2.5
    pos = s["close_in_range"]
    if s["rvol"] < RVOL_X and s["spikes"]:
        top = max(s["spikes"], key=lambda k: k["x"])
        after = top["after_pct"]
        return (f"normal day volume, but a {top['x']:.0f}× burst at {top['at']} ({top['move_pct']:+.1f}% in that bar"
                + (f", {after:+.1f}% after it" if abs(after) >= 0.2 else ", little move after it") + ")")
    if s["chg_pct"] >= 1 and pos >= 70:
        return "buying on heavy volume, closed near the high" if strong else "up on above-normal volume, firm close"
    if s["chg_pct"] <= -1 and pos <= 30:
        return "selling on heavy volume, closed near the low" if strong else "down on above-normal volume, weak close"
    if abs(s["chg_pct"]) < 0.5:
        return "heavy volume but price flat — churn / absorption"
    if up and pos < 40:
        return "rose but gave back gains into the close on high volume"
    if not up and pos > 60:
        return "fell but recovered into the close on high volume"
    return ("up" if up else "down") + " on high volume"


class VolumeWatch:
    def __init__(self, svc, broker=None, market=None):
        self.svc, self.broker, self.market = svc, broker, market
        self.lists: dict[str, list[str]] = {}
        self.keys: dict[str, str] = {}
        self.last: dict | None = None
        self.status = ""

    @property
    def store(self):
        return self.svc.store

    # ---------------------------------------------------------------- universe
    def load_lists(self) -> dict[str, list[str]]:
        for und, (index, url) in LISTS.items():
            docs = self.store.find_docs("index_constituents", {"index": index})
            doc = docs[0] if docs else None
            fresh = doc and (pd.Timestamp.now(tz="UTC") - pd.Timestamp(doc["updated"]).tz_convert("UTC")).days < 7
            if not fresh:
                try:
                    r = self.svc.nse.get(self.svc.nse.ARCHIVE + url)
                    if r is not None and "Symbol" in r.text:
                        df = pd.read_csv(io.StringIO(r.text))
                        doc = {"index": index, "updated": datetime.now().astimezone(),
                               "symbols": [str(s).strip() for s in df["Symbol"]],
                               "names": {str(s).strip(): str(n).strip() for s, n in zip(df["Symbol"], df["Company Name"])}}
                        self.store.upsert_docs("index_constituents", [doc])
                except Exception as exc:
                    log.info("%s list not refreshed: %s", index, exc)
            self.lists[und] = (doc or {}).get("symbols") or FALLBACK[und]
            self.names = {**getattr(self, "names", {}), **((doc or {}).get("names") or {})}
        return self.lists

    def symbols(self) -> list[str]:
        if not self.lists:
            self.load_lists()
        return sorted(set(self.lists["NIFTY"]) | set(self.lists["BANKNIFTY"]))

    def _resolve_keys(self, syms: list[str]):
        feed = getattr(self.broker, "feed", None)
        if feed is None or not feed.live:
            return
        todo = [s for s in syms if s not in self.keys]

        def one(s):
            try:
                r = feed._post("SearchScrip", {"exch": "NSE", "stext": f"{s}-EQ"})
                hit = next((v for v in r.get("values") or [] if v.get("tsym") == f"{s}-EQ"), None)
                return s, hit and f"NSE|{hit['token']}"
            except Exception:
                return s, None
        with ThreadPoolExecutor(6) as pool:
            for s, k in pool.map(one, todo):
                if k:
                    self.keys[s] = k
        for s, k in self.keys.items():
            feed.symbol_keys[store_symbol(s)] = k

    def refresh(self) -> set[str]:
        """Bring every stock's 15-minute candles up to date (Shoonya / Yahoo → store)."""
        syms = self.symbols()
        self._resolve_keys(syms)
        with ThreadPoolExecutor(6) as pool:
            return set(pool.map(lambda s: self._refresh_one(s), syms))

    def _refresh_one(self, s: str) -> str:
        try:
            return self.market.refresh_intraday(store_symbol(s), "15m")
        except Exception as exc:
            log.info("15m candles for %s not refreshed: %s", s, exc)
            return "failed"

    # ---------------------------------------------------------------- summary
    def build(self, day=None, refresh: bool = True) -> dict:
        """(blocking) The summary for `day` (default: the latest session in the data)."""
        if refresh:
            src = self.refresh()
        else:
            src = set()
            self.symbols()
        since = pd.Timestamp.now(tz=config.IST) - pd.Timedelta(days=45)
        frames = {}
        for s in self.symbols():
            try:
                df = self.store.load_ohlcv(store_symbol(s), "15m", start=since)
            except Exception:
                df = None
            if df is not None and len(df):
                frames[s] = df
        if day is None:
            seen = [max(_session(f).index.date) for f in frames.values() if len(_session(f))]
            day = max(seen) if seen else datetime.now(config.IST).date()
        stats = {}
        for s, df in frames.items():
            r = analyse_stock(df, day)
            if r:
                stats[s] = r
        out = {"day": str(day), "built_at": datetime.now().astimezone(), "sources": sorted(src),
               "checked": len(stats), "universe": len(self.symbols()), "indices": {}}
        for und, syms in self.lists.items():
            rows = []
            for s in syms:
                r = stats.get(s)
                if not r:
                    continue
                top = max((x["x"] for x in r["spikes"]), default=0)
                if r["rvol"] >= RVOL_X or r["spikes"]:
                    rows.append({"symbol": s, "name": getattr(self, "names", {}).get(s, s), **r, "max_spike": top,
                                 "read": read(r), "score": max(r["rvol"], top / 1.5)})
            rows.sort(key=lambda x: -x["score"])
            ok = [stats[s] for s in syms if s in stats]
            up_heavy = sum(1 for x in rows if x["chg_pct"] > 0)
            out["indices"][und] = {"stocks": rows[:TOP_N], "flagged": len(rows), "checked": len(ok),
                                   "up_on_volume": up_heavy, "down_on_volume": len(rows) - up_heavy,
                                   "median_rvol": round(float(np.median([x["rvol"] for x in ok])), 2) if ok else None}
        self.last = out
        try:
            self.store.upsert_docs("volume_summary", [out])
        except Exception as exc:
            log.warning("volume_summary not stored: %s", exc)
        return out

    def latest(self) -> dict | None:
        if self.last:
            return self.last
        try:
            docs = self.store.find_docs("volume_summary", sort="day", desc=True, limit=1)
        except Exception:
            docs = []
        self.last = docs[0] if docs else None
        return self.last

    def due(self, now: datetime) -> bool:
        """After 3:35 pm on a weekday, once per day."""
        if now.weekday() >= 5 or now.time() < SUMMARY_AT:
            return False
        last = self.latest()
        return not last or last.get("day") != str(now.date())


def alert(summary: dict) -> dict:
    """One alert for the day's summary (kind VOLUME, stable id per day)."""
    day = summary["day"]
    parts, lines, shown = [], [], set()
    for und in ("NIFTY", "BANKNIFTY"):
        x = summary["indices"].get(und) or {}
        if not x:
            continue
        parts.append(f"{x['flagged']} {und}")
        for s in [s for s in x["stocks"] if s["symbol"] not in shown][:5 if und == "NIFTY" else 3]:
            shown.add(s["symbol"])
            sp = f", spike {s['max_spike']:.0f}× at {max(s['spikes'], key=lambda k: k['x'])['at']}" if s["spikes"] else ""
            lines.append(f"{s['symbol']} {s['chg_pct']:+.1f}% on {s['rvol']:.1f}× volume{sp} — {s['read']}")
    title = f"Unusual volume {pd.Timestamp(day):%d %b}: " + " · ".join(parts) + " stocks"
    return {"id": "vol:" + hashlib.sha1(day.encode()).hexdigest()[:12], "kind": "VOLUME", "level": "MEDIUM",
            "title": title, "text": "\n".join(lines) or "No unusual volume today.", "day": day}


def summary_lines(summary: dict | None, n: int = 6) -> list[str]:
    if not summary:
        return []
    out = [f"Session {summary['day']} · {summary['checked']} of {summary['universe']} stocks checked"]
    for und in ("NIFTY", "BANKNIFTY"):
        x = summary["indices"].get(und) or {}
        if not x:
            continue
        out.append(f"{und}: {x['flagged']} with unusual volume ({x['up_on_volume']} up, {x['down_on_volume']} down);"
                   f" median volume {x['median_rvol']}× normal")
        for s in x["stocks"][:n]:
            sp = "; spikes " + ", ".join(f"{k['at']} {k['x']:.0f}× ({k['move_pct']:+.1f}%)"
                                         for k in sorted(s["spikes"], key=lambda k: -k["x"])[:3]) \
                if s["spikes"] else ""
            out.append(f"  {s['symbol']} {s['chg_pct']:+.2f}% · {s['rvol']:.1f}× day volume{sp} — {s['read']}")
    return out
