"""BANKNIFTY and its constituent banks, as one live picture.

* Constituents: NSE's official list (nsearchives ind_niftybanklist.csv), refreshed
  weekly and stored in MongoDB (`index_constituents`) so it works offline.
* Weights: NSE no longer publishes live weights, so they are estimated from the
  last ~6 sessions of 1-minute returns — BANKNIFTY's return is (by construction)
  the weighted sum of its stocks' returns, so a non-negative least-squares fit
  recovers the weights (R² ≈ 0.97; it rebuilds each day's index move to within
  ~0.02%).  Re-fitted once a day and stored.
* Prices: Shoonya ticks (LTP, previous close, day high/low, VWAP, volume, bid/ask)
  for every stock when connected; otherwise the latest stored 1-minute candles
  (Shoonya or Yahoo).  1-minute candles are kept in `index_ohlcv` as "<SYMBOL>.NS".
* Derived: points each bank added to / took from BANKNIFTY today, weighted
  breadth, price vs VWAP, relative volume vs the same time on previous sessions,
  intraday sparklines, and alerts (big movers, heavyweights against the index,
  narrow breadth).
Blocking; call from worker threads (except `live_snapshot`, which only reads ticks).
"""
from __future__ import annotations

import io
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config
from shoonya_feed import INDEX_TOKENS

log = logging.getLogger(__name__)

INDEX = "NIFTY BANK"
LIST_URL = "/content/indices/ind_niftybanklist.csv"
FALLBACK = ["AUBANK", "AXISBANK", "BANKBARODA", "CANBK", "FEDERALBNK", "HDFCBANK", "ICICIBANK", "IDFCFIRSTB",
            "INDUSINDBK", "KOTAKBANK", "PNB", "SBIN", "UNIONBANK", "YESBANK"]
NAMES = {"AUBANK": "AU Small Finance", "AXISBANK": "Axis Bank", "BANKBARODA": "Bank of Baroda",
         "CANBK": "Canara Bank", "FEDERALBNK": "Federal Bank", "HDFCBANK": "HDFC Bank", "ICICIBANK": "ICICI Bank",
         "IDFCFIRSTB": "IDFC First", "INDUSINDBK": "IndusInd Bank", "KOTAKBANK": "Kotak Mahindra",
         "PNB": "Punjab National", "SBIN": "State Bank", "UNIONBANK": "Union Bank", "YESBANK": "Yes Bank"}
FIT_DAYS = 7          # calendar days of 1-minute bars for the weight fit
BIG_MOVE = 2.0        # % move that raises an alert


def store_symbol(sym: str) -> str:
    return f"{sym}.NS"


def nnls(A: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Non-negative least squares by successive removal of negative coefficients."""
    P = list(range(A.shape[1]))
    while P:
        x, *_ = np.linalg.lstsq(A[:, P], b, rcond=None)
        if (x >= 0).all():
            w = np.zeros(A.shape[1])
            w[P] = x
            return w
        P.pop(int(np.argmin(x)))
    return np.zeros(A.shape[1])


class BankNiftyService:
    def __init__(self, svc, broker, market):
        self.svc, self.broker, self.market = svc, broker, market
        self.symbols: list[str] = []
        self.names: dict[str, str] = {}
        self.weights: dict[str, float] = {}
        self.fit: dict = {}
        self.keys: dict[str, str] = {}             # symbol -> Shoonya "NSE|token"
        self.day: dict[str, dict] = {}             # symbol -> today's stats from 1-minute candles
        self.prev_close: dict[str, float] = {}
        self.sparks: dict[str, list[float]] = {}
        self.rvol: dict[str, float] = {}

    @property
    def store(self):
        return self.svc.store

    # ---------------------------------------------------------------- constituents
    def load_constituents(self) -> list[str]:
        docs = self.store.find_docs("index_constituents", {"index": INDEX})
        doc = docs[0] if docs else None
        fresh = doc and (pd.Timestamp.now(tz="UTC") - pd.Timestamp(doc["updated"]).tz_convert("UTC")).days < 7
        if not fresh:
            try:
                r = self.svc.nse.get(self.svc.nse.ARCHIVE + LIST_URL)
                if r is not None and "Symbol" in r.text:
                    df = pd.read_csv(io.StringIO(r.text))
                    doc = {"index": INDEX, "updated": datetime.now().astimezone(),
                           "symbols": [str(s).strip() for s in df["Symbol"]],
                           "names": {str(s).strip(): str(n).strip() for s, n in zip(df["Symbol"], df["Company Name"])}}
                    self.store.upsert_docs("index_constituents", [doc])
            except Exception as exc:
                log.warning("Constituent list not refreshed: %s", exc)
        self.symbols = (doc or {}).get("symbols") or FALLBACK
        self.names = {s: NAMES.get(s) or (doc or {}).get("names", {}).get(s, s) for s in self.symbols}
        return self.symbols

    def resolve_keys(self):
        """Shoonya tokens for every constituent (cached), registered with the feed so the
        market-data layer and the websocket use them."""
        feed = self.broker.feed
        if not feed.live:
            return
        todo = [s for s in self.symbols if s not in self.keys]

        def one(s):
            try:
                r = feed._post("SearchScrip", {"exch": "NSE", "stext": f"{s}-EQ"})
                hit = next((v for v in r.get("values") or [] if v.get("tsym") == f"{s}-EQ"), None)
                return s, hit and f"NSE|{hit['token']}"
            except Exception as exc:
                log.debug("SearchScrip %s failed: %s", s, exc)
                return s, None
        if todo:
            with ThreadPoolExecutor(6) as pool:
                for s, k in pool.map(one, todo):
                    if k:
                        self.keys[s] = k
        for s, k in self.keys.items():
            feed.symbol_keys[store_symbol(s)] = k
        feed.watch(self.keys.values())

    # ---------------------------------------------------------------- candles, weights, day stats
    def refresh(self, refit: bool = False) -> dict:
        """Pull 1-minute candles (Shoonya / Yahoo → MongoDB), recompute today's stats,
        sparklines, relative volume and — once a day or on request — the weights."""
        if not self.symbols:
            self.load_constituents()
        self.resolve_keys()
        syms = [store_symbol(s) for s in self.symbols] + [INDEX]
        with ThreadPoolExecutor(6) as pool:
            sources = set(pool.map(lambda s: self.market.refresh_intraday(s, "1m"), syms))
        since = datetime.now() - timedelta(days=FIT_DAYS + 3)
        frames = {}
        for s in syms:
            df = self.store.load_ohlcv(s, "1m", start=since)
            if df is not None and len(df):
                df = df.copy()
                df.index = df.index.tz_convert(config.IST) if df.index.tz is not None else df.index.tz_localize(config.IST)
                frames[s] = df
        self._day_stats(frames)
        self._load_or_fit_weights(frames, refit)
        return {"sources": sources, "weights": self.weights, "fit": self.fit}

    def _day_stats(self, frames: dict[str, pd.DataFrame]):
        today = pd.Timestamp.now(tz=config.IST).normalize()
        for s in self.symbols + ["__INDEX__"]:
            key = INDEX if s == "__INDEX__" else store_symbol(s)
            df = frames.get(key)
            if df is None or df.empty:
                continue
            days = sorted(set(df.index.normalize()))
            last_day = days[-1]
            cur = df[df.index.normalize() == last_day]
            prev = df[df.index.normalize() < last_day]
            name = s
            if len(prev):
                self.prev_close[name] = float(prev["close"].iloc[-1])
            vol = cur["volume"].fillna(0)
            vwap = float((cur["close"] * vol).sum() / vol.sum()) if vol.sum() > 0 else None
            self.day[name] = {"open": float(cur["open"].iloc[0]), "high": float(cur["high"].max()),
                              "low": float(cur["low"].min()), "last": float(cur["close"].iloc[-1]),
                              "volume": float(vol.sum()), "vwap": vwap, "as_of": cur.index[-1],
                              "is_today": last_day == today}
            self.sparks[name] = [float(x) for x in cur["close"].values[::2]]
            # relative volume: today's volume so far vs the average of previous sessions up to the same time
            if vol.sum() > 0 and len(prev):
                t = cur.index[-1].time()
                past = [g[g.index.time <= t]["volume"].sum() for _d, g in prev.groupby(prev.index.normalize())]
                past = [p for p in past[-5:] if p > 0]
                self.rvol[name] = float(vol.sum() / np.mean(past)) if past else None

    def _load_or_fit_weights(self, frames, refit: bool):
        docs = self.store.find_docs("index_weights", {"index": INDEX})
        doc = docs[0] if docs else None
        stale = not doc or pd.Timestamp(doc["updated"]).tz_convert(config.IST).date() < datetime.now().date()
        if doc and not refit and not stale and set(doc["weights"]) == set(self.symbols):
            self.weights, self.fit = doc["weights"], doc.get("fit", {})
            return
        try:
            w, fit = self.fit_weights(frames)
            self.weights, self.fit = w, fit
            self.store.upsert_docs("index_weights", [{"index": INDEX, "updated": datetime.now().astimezone(),
                                                      "weights": w, "fit": fit}])
        except Exception as exc:
            log.warning("Weight fit failed: %s", exc)
            if doc:
                self.weights, self.fit = doc["weights"], doc.get("fit", {})
            else:
                self.weights = {s: 1 / len(self.symbols) for s in self.symbols}
                self.fit = {"note": "equal weights (not enough data to fit yet)"}

    def fit_weights(self, frames) -> tuple[dict[str, float], dict]:
        cols = {s: frames[store_symbol(s)]["close"] for s in self.symbols if store_symbol(s) in frames}
        if INDEX not in frames or len(cols) < len(self.symbols) * 0.8:
            raise RuntimeError("not enough 1-minute history yet")
        df = pd.concat([pd.DataFrame(cols), frames[INDEX]["close"].rename("IDX")], axis=1, join="inner").dropna()
        df = df[df.index.time >= pd.Timestamp("09:16").time()]
        r = np.log(df).groupby(df.index.date).diff().dropna()
        if len(r) < 300:
            raise RuntimeError(f"only {len(r)} matched 1-minute bars")
        syms = list(cols)
        A, b = r[syms].values, r["IDX"].values
        w = nnls(A, b)
        r2 = 1 - ((b - A @ w) ** 2).sum() / ((b - b.mean()) ** 2).sum()
        tot = w.sum() or 1
        return ({s: float(v / tot) for s, v in zip(syms, w)},
                {"r2": float(r2), "bars": int(len(r)), "days": int(len(set(r.index.date))),
                 "raw_sum": float(tot), "at": datetime.now().strftime("%d %b %H:%M")})

    # ---------------------------------------------------------------- live picture
    def live_snapshot(self) -> dict:
        """Everything the screen draws, from ticks (Shoonya) or the day's candles."""
        feed = self.broker.feed
        rows = []
        idx_t = feed.tick(INDEX_TOKENS[INDEX]) if feed.live else None
        idx_day = self.day.get("__INDEX__", {})
        if idx_t and feed._fresh(idx_t):
            idx = {"last": _f(idx_t.get("lp")), "prev": _f(idx_t.get("c")) or self.prev_close.get("__INDEX__"),
                   "high": _f(idx_t.get("h")) or idx_day.get("high"), "low": _f(idx_t.get("l")) or idx_day.get("low"),
                   "open": _f(idx_t.get("o")) or idx_day.get("open"), "live": True}
        else:
            idx = {"last": idx_day.get("last"), "prev": self.prev_close.get("__INDEX__"), "high": idx_day.get("high"),
                   "low": idx_day.get("low"), "open": idx_day.get("open"), "live": False}
        for s in self.symbols:
            d = self.day.get(s, {})
            t = feed.tick(self.keys[s]) if feed.live and s in self.keys else None
            if t and feed._fresh(t):
                q = {"last": _f(t.get("lp")), "prev": _f(t.get("c")) or self.prev_close.get(s),
                     "open": _f(t.get("o")) or d.get("open"), "high": _f(t.get("h")) or d.get("high"),
                     "low": _f(t.get("l")) or d.get("low"), "vwap": _f(t.get("ap")) or d.get("vwap"),
                     "volume": _f(t.get("v")) or d.get("volume"), "bid": _f(t.get("bp1")), "ask": _f(t.get("sp1")),
                     "live": True}
            else:
                q = {"last": d.get("last"), "prev": self.prev_close.get(s), "open": d.get("open"),
                     "high": d.get("high"), "low": d.get("low"), "vwap": d.get("vwap"), "volume": d.get("volume"),
                     "live": False}
            q.update({"symbol": s, "name": self.names.get(s, s), "weight": self.weights.get(s, 0.0),
                      "rvol": self.rvol.get(s), "spark": self.sparks.get(s) or []})
            if q["last"] and q["prev"]:
                q["chg"] = q["last"] - q["prev"]
                q["pct"] = q["chg"] / q["prev"] * 100
            else:
                q["chg"] = q["pct"] = None
            q["vs_vwap"] = (q["last"] / q["vwap"] - 1) * 100 if q.get("vwap") and q["last"] else None
            q["range_pos"] = ((q["last"] - q["low"]) / (q["high"] - q["low"]) * 100
                              if q.get("high") and q.get("low") and q["high"] > q["low"] and q["last"] else None)
            rows.append(q)
        idx["spark"] = self.sparks.get("__INDEX__") or []      # today's index line (every 2nd minute)
        if idx.get("live") and idx.get("last") and idx["spark"]:
            idx["spark"] = idx["spark"] + [idx["last"]]            # end on the live tick
        base = idx["prev"] or idx["last"]
        for q in rows:
            q["contrib"] = q["weight"] * q["pct"] / 100 * base if q["pct"] is not None and base else None
        idx["chg"] = (idx["last"] - idx["prev"]) if idx.get("last") and idx.get("prev") else None
        idx["pct"] = idx["chg"] / idx["prev"] * 100 if idx["chg"] is not None else None
        explained = sum(q["contrib"] for q in rows if q["contrib"] is not None)
        up_w = sum(q["weight"] for q in rows if (q["pct"] or 0) > 0)
        adv = sum(1 for q in rows if (q["pct"] or 0) > 0)
        dec = sum(1 for q in rows if (q["pct"] or 0) < 0)
        return {"index": idx, "rows": rows, "explained": explained, "up_weight": up_w * 100, "adv": adv, "dec": dec,
                "alerts": self.alerts(idx, rows, up_w), "fit": self.fit, "live": bool(idx.get("live")),
                "as_of": idx_day.get("as_of")}

    @staticmethod
    def alerts(idx: dict, rows: list[dict], up_w: float) -> list[tuple[str, str]]:
        """(level, text) — red for moves against a short-option writer's comfort, amber for watch items."""
        out = []
        ipct = idx.get("pct")
        for q in sorted(rows, key=lambda q: -abs(q["pct"] or 0)):
            if q["pct"] is not None and abs(q["pct"]) >= BIG_MOVE:
                out.append(("amber", f"{q['symbol']} {q['pct']:+.2f}% — "
                                     f"{q['contrib']:+.0f} pts on BANKNIFTY (weight {q['weight'] * 100:.1f}%)"))
        if ipct is not None and abs(ipct) >= 0.2:            # the index must actually be moving
            heavy = [q for q in sorted(rows, key=lambda q: -q["weight"])[:2] if q["pct"] is not None]
            against = [q for q in heavy if (q["pct"] > 0) != (ipct > 0) and abs(q["pct"]) >= 0.3]
            if against:
                out.append(("red", "Heavyweights against the index: " + ", ".join(
                    f"{q['symbol']} {q['pct']:+.2f}%" for q in against) + f" while BANKNIFTY {ipct:+.2f}% — "
                    "the move lacks its biggest supports"))
            if ipct > 0.3 and up_w < 0.5:
                out.append(("red", f"Narrow rally: BANKNIFTY {ipct:+.2f}% but only {up_w * 100:.0f}% of the weight is up"))
            if ipct < -0.3 and up_w > 0.5:
                out.append(("red", f"Narrow fall: BANKNIFTY {ipct:+.2f}% while {up_w * 100:.0f}% of the weight is up"))
        vw = [q for q in rows if q["vs_vwap"] is not None and q["weight"] >= 0.08]
        below = [q for q in vw if q["vs_vwap"] < -0.3]
        above = [q for q in vw if q["vs_vwap"] > 0.3]
        if ipct is not None and ipct > 0 and len(below) >= max(2, len(vw) // 2):
            out.append(("amber", "Most heavyweights trade below VWAP: " + ", ".join(
                f"{q['symbol']} {q['vs_vwap']:+.2f}%" for q in below)))
        if ipct is not None and ipct < 0 and len(above) >= max(2, len(vw) // 2):
            out.append(("amber", "Most heavyweights trade above VWAP: " + ", ".join(
                f"{q['symbol']} {q['vs_vwap']:+.2f}%" for q in above)))
        for q in rows:
            if q.get("rvol") and q["rvol"] >= 2 and q["weight"] >= 0.05:
                out.append(("amber", f"{q['symbol']} volume {q['rvol']:.1f}× its usual pace for this time"))
        return out


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None
