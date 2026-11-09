"""Data ingestion: Yahoo Finance OHLCV + NSE institutional-flow endpoints.

Sources
-------
* OHLCV (1D / 1H / 15m) for ^NSEI and NIFTYBEES.NS ............ yfinance
* FII / DII provisional cash-market net (latest session) ...... nseindia.com/api/fiidiiTradeReact
* FII / DII cash-market history (net, last ~90 sessions) ....... niftytrader.in/fii-dii-data
      + buy/sell/net for the last ~30 sessions ................. moneycontrol.com/markets/fii-dii-data/cash
* Participant-wise open interest (daily archive CSV) ........... nsearchives.nseindia.com
      -> FII index-futures long/short ratio, client long/short ratio,
         index-options put/call OI ratio (PCR)
* Macro catalysts (Brent, USD/INR, US10Y, India VIX, DXY) ..... yfinance

NSE serves the cash FII/DII figure only for the latest session, so every sync
also pulls the ~90-session history NiftyTrader and Moneycontrol publish (same
NSE provisional figures) and history accumulates in MongoDB from there.  For a deeper back-fill
use `import_fii_dii_csv()` with any export that has date / FII net / DII net columns.
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Callable

import pandas as pd
import os

import requests

try:                                    # the standalone Android app has no yfinance: Yahoo's chart API instead
    if os.getenv("NSE_YAHOO_LITE"):
        raise ImportError
    import yfinance as yf
except ImportError:
    import yahoo_lite as yf

import config

log = logging.getLogger(__name__)
Progress = Callable[[str], None]


# =========================================================== Yahoo Finance
def fetch_ohlcv(symbol: str, timeframe: str = "1D", start: datetime | None = None) -> pd.DataFrame:
    """Return OHLCV with lowercase columns and an IST DatetimeIndex.
    `symbol` is an instrument id from config.INSTRUMENTS (or a raw Yahoo ticker)."""
    interval, period = config.TIMEFRAMES[timeframe]
    inst = config.INSTRUMENTS.get(symbol)
    ticker = inst.yahoo if inst else symbol
    if ticker is None:
        log.info("%s has no Yahoo ticker; %s data unavailable", symbol, timeframe)
        return pd.DataFrame()
    tk = yf.Ticker(ticker)
    kwargs = dict(interval=interval, auto_adjust=False, actions=False)
    if start is not None:
        start = pd.Timestamp(start)
        if start.tzinfo is not None:
            start = start.tz_convert(config.IST).tz_localize(None)
        # Yahoo rejects intraday start dates beyond its retention window.
        if (pd.Timestamp.now() - start).days >= int(period.rstrip("dy")) * (365 if period.endswith("y") else 1) - 1:
            start = None
    if start is not None:
        df = tk.history(start=start.strftime("%Y-%m-%d"), **kwargs)
    else:
        df = tk.history(period=period, **kwargs)
    if df is None or df.empty:
        log.warning("yfinance returned no data for %s %s", symbol, timeframe)
        return pd.DataFrame()
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df.index = (df.index.tz_localize(config.IST) if df.index.tz is None
                else df.index.tz_convert(config.IST))
    df.index.name = "timestamp"
    df = df[~df.index.duplicated(keep="last")].dropna(subset=["close"])
    # Yahoo sometimes emits the in-progress daily bar with zero range; keep it but
    # make sure H/L bracket O/C so candles render correctly.
    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)
    return df


def fetch_intraday_1m(symbol: str, days: int = 5) -> list[tuple]:
    """1-minute candles from Yahoo (delayed fallback when no broker feed), oldest
    first, as (datetime IST-naive, open, high, low, close)."""
    inst = config.INSTRUMENTS.get(symbol)
    h = yf.Ticker(inst.yahoo if inst else symbol).history(period=f"{days}d", interval="1m", auto_adjust=False)
    if h is None or h.empty:
        return []
    idx = h.index.tz_convert(config.IST).tz_localize(None) if h.index.tz is not None else h.index
    return [(ts.to_pydatetime(), float(o), float(hi), float(lo), float(c))
            for ts, o, hi, lo, c in zip(idx, h["Open"], h["High"], h["Low"], h["Close"])
            if c == c]


def fetch_macro(period: str = "1y") -> pd.DataFrame:
    """Daily closes for each catalyst series, columns named by MACRO_TICKERS keys."""
    out = {}
    for name, ticker in config.MACRO_TICKERS.items():
        try:
            h = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
            if not h.empty:
                s = h["Close"]
                s.index = s.index.tz_localize(None).normalize()
                out[name] = s
        except Exception as exc:  # network / delisted ticker — non-fatal
            log.warning("Macro fetch failed for %s (%s): %s", name, ticker, exc)
    return pd.DataFrame(out).sort_index()


# =========================================================== NSE endpoints
class NSEClient:
    """Session-aware NSE client. nseindia.com rejects requests that lack the
    cookies set by a normal page visit, so we warm the session up first."""

    BASE = "https://www.nseindia.com"
    ARCHIVE = "https://nsearchives.nseindia.com"
    HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/reports/fii-dii",
    }

    def __init__(self, timeout: int = 15, retries: int = 3):
        self.timeout = timeout
        self.retries = retries
        self.s = requests.Session()
        self.s.headers.update(self.HEADERS)
        self._warm = False

    def _warmup(self) -> None:
        if self._warm:
            return
        for path in ("/", "/reports/fii-dii"):
            try:
                self.s.get(self.BASE + path, timeout=self.timeout)
            except requests.RequestException as exc:
                log.debug("NSE warm-up %s failed: %s", path, exc)
        self._warm = True

    def get(self, url: str) -> requests.Response | None:
        self._warmup()
        for attempt in range(1, self.retries + 1):
            try:
                r = self.s.get(url, timeout=self.timeout)
                if r.status_code == 200:
                    return r
                if r.status_code == 404:          # holiday / file not published
                    return None
                if r.status_code in (401, 403):    # cookies expired -> re-warm
                    self._warm = False
                    self._warmup()
            except requests.RequestException as exc:
                log.debug("NSE GET %s attempt %d failed: %s", url, attempt, exc)
            time.sleep(0.8 * attempt)
        return None

    # ---------------------------------------------------------- cash FII/DII
    def fii_dii_cash(self) -> dict | None:
        """Latest provisional FII/FPI & DII cash-market net (₹ crore)."""
        r = self.get(f"{self.BASE}/api/fiidiiTradeReact")
        if r is None:
            return None
        try:
            rows = r.json()
        except ValueError:
            return None
        rec: dict = {}
        for row in rows:
            cat = str(row.get("category", "")).upper()
            net = _num(row.get("netValue"))
            d = pd.to_datetime(row.get("date"), format="%d-%b-%Y", errors="coerce")
            if pd.notna(d):
                rec["date"] = d.normalize()
            if cat.startswith("FII") or "FPI" in cat:
                rec["fii_cash_net"] = net
                rec["fii_cash_buy"] = _num(row.get("buyValue"))
                rec["fii_cash_sell"] = _num(row.get("sellValue"))
            elif cat.startswith("DII"):
                rec["dii_cash_net"] = net
                rec["dii_cash_buy"] = _num(row.get("buyValue"))
                rec["dii_cash_sell"] = _num(row.get("sellValue"))
        return rec if "date" in rec else None

    # ---------------------------------------------------------- participant OI
    def participant_oi(self, day: date) -> dict | None:
        """Parse fao_participant_oi_DDMMYYYY.csv for one trading day."""
        url = f"{self.ARCHIVE}/content/nsccl/fao_participant_oi_{day:%d%m%Y}.csv"
        r = self.get(url)
        if r is None or not r.text.strip():
            return None
        try:
            return parse_participant_oi(r.text, day)
        except Exception as exc:
            log.warning("Could not parse participant OI for %s: %s", day, exc)
            return None

    # ---------------------------------------------------------- option chain
    def option_expiries(self, symbol: str) -> list[str]:
        r = self.get(f"{self.BASE}/api/option-chain-contract-info?symbol={symbol}")
        return (r.json().get("expiryDates") or []) if r is not None else []

    def option_chain(self, symbol: str, expiry: str) -> dict | None:
        """Full option chain for one index expiry (expiry as 'DD-Mon-YYYY')."""
        r = self.get(f"{self.BASE}/api/option-chain-v3?type=Indices&symbol={symbol}&expiry={expiry}")
        if r is None:
            return None
        js = r.json()
        return js if (js.get("records") or {}).get("data") else None

    # ---------------------------------------------------------- live quotes
    def live_snapshot(self) -> dict:
        """One-shot live quotes for every configured index and ETF (2 requests).
        Returns {"ts", "market_status", "quotes": {symbol: {...}}, "breadth", "vix"}."""
        out: dict = {"ts": None, "market_status": None, "quotes": {}, "breadth": None, "vix": None}
        r = self.get(f"{self.BASE}/api/allIndices")
        if r is not None:
            j = r.json()
            out["ts"] = j.get("timestamp")
            by_name = {str(x.get("index", "")).upper(): x for x in j.get("data", [])}
            for sym, inst in config.INSTRUMENTS.items():
                x = by_name.get((inst.nse_name or "").upper()) if inst.kind == "index" else None
                if x:
                    out["quotes"][sym] = {
                        "last": _num(x.get("last")), "change": _num(x.get("variation")),
                        "pct": _num(x.get("percentChange")), "open": _num(x.get("open")),
                        "high": _num(x.get("high")), "low": _num(x.get("low")),
                        "prev": _num(x.get("previousClose")),
                        "adv": _num(x.get("advances")), "dec": _num(x.get("declines")),
                        "year_high": _num(x.get("yearHigh")), "year_low": _num(x.get("yearLow")),
                    }
            vix = by_name.get("INDIA VIX")
            if vix:
                out["vix"] = {"last": _num(vix.get("last")), "change": _num(vix.get("variation")),
                              "pct": _num(vix.get("percentChange")), "prev": _num(vix.get("previousClose"))}
            out["breadth"] = {"adv": _num(j.get("advances")), "dec": _num(j.get("declines")),
                              "unch": _num(j.get("unchanged"))}
        r = self.get(f"{self.BASE}/api/etf")
        if r is not None:
            by_sym = {str(x.get("symbol", "")).upper(): x for x in r.json().get("data", [])}
            for sym, inst in config.INSTRUMENTS.items():
                x = by_sym.get(sym.removesuffix(".NS").upper()) if inst.kind == "etf" else None
                if x:
                    last, prev = _num(x.get("ltP")), _num(x.get("prevClose"))
                    out["quotes"][sym] = {
                        "last": last, "change": _num(x.get("chn")), "pct": _num(x.get("per")),
                        "open": _num(x.get("open")), "high": _num(x.get("high")),
                        "low": _num(x.get("low")), "prev": prev, "nav": _num(x.get("nav")),
                    }
        r = self.get(f"{self.BASE}/api/marketStatus")
        if r is not None:
            cm = next((m for m in r.json().get("marketState", []) if m.get("market") == "Capital Market"), None)
            if cm:
                out["market_status"] = cm.get("marketStatus")
                out["market_message"] = cm.get("marketStatusMessage")
        return out

    # ---------------------------------------------------------- index EOD archive
    def index_close_all(self, day: date) -> pd.DataFrame | None:
        """EOD OHLCV for every NSE index on one day (ind_close_all_DDMMYYYY.csv)."""
        r = self.get(f"{self.ARCHIVE}/content/indices/ind_close_all_{day:%d%m%Y}.csv")
        if r is None or not r.text.strip():
            return None
        try:
            return parse_index_close_all(r.text)
        except Exception as exc:
            log.warning("Could not parse index archive for %s: %s", day, exc)
            return None


def parse_index_close_all(text: str) -> pd.DataFrame:
    """Rows for the indices in config.NSE_ARCHIVE_SYMBOLS, columns
    symbol/timestamp/open/high/low/close/volume."""
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip().lower() for c in df.columns]
    df["symbol"] = df["index name"].astype(str).str.strip().str.lower().map(config.NSE_ARCHIVE_SYMBOLS)
    df = df.dropna(subset=["symbol"])
    num = lambda c: pd.to_numeric(df[c].astype(str).str.replace(",", ""), errors="coerce")
    out = pd.DataFrame({
        "symbol": df["symbol"],
        "timestamp": pd.to_datetime(df["index date"], dayfirst=True).dt.tz_localize(config.IST),
        "open": num("open index value"), "high": num("high index value"),
        "low": num("low index value"), "close": num("closing index value"),
        "volume": num("volume").fillna(0),
    }).dropna(subset=["close"])
    # Some indices publish only a close on certain days: fall back to it for O/H/L.
    for c in ("open", "high", "low"):
        out[c] = out[c].where(out[c] > 0, out["close"])
    return out


def parse_participant_oi(text: str, day: date) -> dict:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    hdr = next(i for i, ln in enumerate(lines) if ln.lower().lstrip('"').startswith("client type"))
    df = pd.read_csv(io.StringIO("\n".join(lines[hdr:])))
    df.columns = [c.strip().lower() for c in df.columns]
    df["client type"] = df["client type"].astype(str).str.strip().str.upper()
    df = df.set_index("client type")

    def col(name: str) -> str:
        return next(c for c in df.columns if c.replace("  ", " ").startswith(name))

    fl, fs = col("future index long"), col("future index short")
    ocl, opl = col("option index call long"), col("option index put long")
    ocs, ops_ = col("option index call short"), col("option index put short")
    fii, cli, tot = df.loc["FII"], df.loc["CLIENT"], df.loc["TOTAL"]
    # every participant x instrument, e.g. pro_idx_call_short, client_idx_fut_long
    full = {}
    sl, ss = col("future stock long"), col("future stock short")
    for who in ("FII", "DII", "PRO", "CLIENT"):
        if who not in df.index:
            continue
        r = df.loc[who]
        for inst, (lc, sc) in (("idx_fut", (fl, fs)), ("idx_call", (ocl, ocs)), ("idx_put", (opl, ops_)),
                               ("stk_fut", (sl, ss))):
            full[f"{who.lower()}_{inst}_long"] = float(r[lc])
            full[f"{who.lower()}_{inst}_short"] = float(r[sc])
    return {**full,
        "date": pd.Timestamp(day),
        "fii_index_fut_long": float(fii[fl]),
        "fii_index_fut_short": float(fii[fs]),
        "fii_index_futures_long_short_ratio": _ratio(fii[fl], fii[fs]),
        "fii_index_fut_net_oi": float(fii[fl] - fii[fs]),
        "client_ratio": _ratio(cli[fl], cli[fs]),
        "dii_index_futures_long_short_ratio": _ratio(df.loc["DII"][fl], df.loc["DII"][fs]),
        "fii_index_call_net": float(fii[ocl] - fii[ocs]),
        "fii_index_put_net": float(fii[opl] - fii[ops_]),
        "pcr": _ratio(tot[opl], tot[ocl]),   # total index put OI / call OI
    }


MONEYCONTROL_CASH_URL = "https://www.moneycontrol.com/markets/fii-dii-data/cash/"
NIFTYTRADER_CASH_URL = "https://www.niftytrader.in/fii-dii-data"


def fetch_fii_dii_cash_history(timeout: int = 20) -> list[dict]:
    """FII/FPI and DII cash-market figures (₹ crore), merged from two pages that
    republish NSE's provisional numbers: NiftyTrader (net only, ~90 sessions)
    and Moneycontrol (buy/sell/net, ~30 sessions). Either may fail on its own."""
    by_date: dict = {}
    errors = []
    for url, parse in ((NIFTYTRADER_CASH_URL, parse_niftytrader_cash),
                       (MONEYCONTROL_CASH_URL, parse_moneycontrol_cash)):
        try:
            r = requests.get(url, headers={"User-Agent": NSEClient.HEADERS["User-Agent"]}, timeout=timeout)
            r.raise_for_status()
            for rec in parse(r.text):
                by_date.setdefault(rec["date"], {}).update({k: v for k, v in rec.items() if v is not None})
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            log.warning("FII/DII cash history from %s failed: %s", url, exc)
    if not by_date and errors:
        raise RuntimeError("; ".join(errors))
    return [by_date[d] for d in sorted(by_date)]


def _next_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("page has no __NEXT_DATA__ block")
    return json.loads(m.group(1))["props"]["pageProps"]


def parse_niftytrader_cash(html: str) -> list[dict]:
    out = []
    for row in _next_data(html)["series"]:
        d = pd.to_datetime(row.get("created_at"), errors="coerce")
        net = _num(row.get("fii_net_value"))
        if pd.isna(d) or net is None:
            continue
        out.append({"date": d.normalize(), "fii_cash_net": net,
                    "dii_cash_net": _num(row.get("dii_net_value"))})
    return out


def parse_moneycontrol_cash(html: str) -> list[dict]:
    rows = _next_data(html)["FiiDiiData"]["fiiDiiData"]
    out = []
    for row in rows:
        d = pd.to_datetime(row.get("date"), errors="coerce")
        net = _num(row.get("fiiNet"))
        if pd.isna(d) or net is None:
            continue
        out.append({"date": d.normalize(),
                    "fii_cash_buy": _num(row.get("fiiPurchase")),
                    "fii_cash_sell": _num(row.get("fiiSales")),
                    "fii_cash_net": net,
                    "dii_cash_buy": _num(row.get("diiPurchase")),
                    "dii_cash_sell": _num(row.get("diiSale") or row.get("diiSales")),
                    "dii_cash_net": _num(row.get("diiNet"))})
    return out


def import_fii_dii_csv(path: str) -> list[dict]:
    """Back-fill helper. Accepts any CSV with a date column plus columns whose
    names contain 'fii'/'fpi' and 'dii' and 'net' (e.g. Moneycontrol/NSDL exports)."""
    df = pd.read_csv(path)
    cols = {c.lower().strip(): c for c in df.columns}
    dcol = next(cols[c] for c in cols if "date" in c)
    fcol = next((cols[c] for c in cols if ("fii" in c or "fpi" in c) and "net" in c), None)
    icol = next((cols[c] for c in cols if "dii" in c and "net" in c), None)
    out = []
    for _, row in df.iterrows():
        d = pd.to_datetime(row[dcol], dayfirst=True, errors="coerce")
        if pd.isna(d):
            continue
        out.append({"date": d.normalize(),
                    "fii_cash_net": _num(row[fcol]) if fcol else None,
                    "dii_cash_net": _num(row[icol]) if icol else None})
    return out


def pick_expiry(expiries: list[str], wanted: str | None = None) -> str:
    """`wanted` if listed, else the first expiry at least 7 days out — the nearest
    that suits a positional seller."""
    if wanted in expiries:
        return wanted
    today = date.today()
    far = [e for e in expiries if (pd.to_datetime(e, format="%d-%b-%Y").date() - today).days >= 7]
    return far[0] if far else expiries[0]


def _daily_from_nse(symbol: str) -> bool:
    inst = config.INSTRUMENTS.get(symbol)
    return inst is not None and inst.daily_source == "nse"


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _ratio(a, b) -> float | None:
    a, b = float(a), float(b)
    return round(a / b, 4) if b else None


# =========================================================== orchestration
class MemoryStore:
    """Drop-in stand-in for DBHandler when MongoDB is down, so the terminal
    still works (data just isn't persisted between runs)."""

    def __init__(self):
        self.journal_keys = {"broker_fills": ("fill_id",), "broker_orders": ("order_no", "date"),
                             "pnl_daily": ("date",), "trade_journal": ("strategy_id",),
                             "ai_reviews": ("review_id",),
                             "option_chain_snapshots": ("underlying", "expiry", "ts"), "sync_log": ("job",),
                             "broker_positions": ("date",),
                             "index_constituents": ("index",), "index_weights": ("index",),
                             "news": ("id",), "market_alerts": ("id",), "direction_calls": ("und", "slot"), "writer_state": ("day", "und"), "volume_summary": ("day",),
                             "global_quotes": ("key", "session")}
        self._docs: dict[str, dict[tuple, dict]] = {n: {} for n in self.journal_keys}
        self._ohlcv: dict[tuple[str, str], pd.DataFrame] = {}
        self._fii = pd.DataFrame()
        self._macro = pd.DataFrame()
        self._sr: dict[tuple[str, str], dict] = {}

    def upsert_docs(self, name, docs, insert_only=()):
        coll = self._docs[name]
        for d in docs:
            key = tuple(d[k] for k in self.journal_keys[name])
            cur = coll.get(key)
            new = dict(d)
            if cur is not None:
                for k in insert_only:
                    if k in cur:
                        new[k] = cur[k]
                cur.update(new)
            else:
                coll[key] = new
        return len(docs)

    def find_docs(self, name, query=None, sort=None, desc=False, limit=None):
        rows = [dict(d) for d in self._docs[name].values()
                if all(d.get(k) == v for k, v in (query or {}).items() if not isinstance(v, dict))]
        if sort:
            rows.sort(key=lambda d: d.get(sort), reverse=desc)
        return rows[:limit] if limit else rows

    def delete_docs(self, name, query):
        keys = [k for k, d in self._docs[name].items() if all(d.get(a) == b for a, b in query.items())]
        for k in keys:
            del self._docs[name][k]
        return len(keys)

    def upsert_ohlcv(self, symbol, timeframe, df):
        if df is None or df.empty:
            return 0
        old = self._ohlcv.get((symbol, timeframe))
        new = df.copy() if old is None or old.empty else pd.concat([old, df])
        new.index = pd.DatetimeIndex(new.index)      # concat with an empty frame gives an object index on pandas < 2.2
        self._ohlcv[(symbol, timeframe)] = new[~new.index.duplicated(keep="last")].sort_index()
        return len(df)

    def load_ohlcv(self, symbol, timeframe, start=None, limit=None):
        df = self._ohlcv.get((symbol, timeframe), pd.DataFrame())
        if start is not None and not df.empty:
            t = pd.Timestamp(start)
            df = df[df.index >= (t.tz_localize(config.IST) if t.tzinfo is None else t)]
        return df.tail(limit) if limit else df

    def latest_timestamp(self, symbol, timeframe):
        df = self._ohlcv.get((symbol, timeframe))
        return None if df is None or df.empty else df.index[-1]

    def upsert_fii_dii(self, records):
        recs = [r for r in records if r.get("date") is not None]
        if not recs:
            return 0
        new = pd.DataFrame(recs)
        new["date"] = pd.to_datetime(new["date"]).dt.normalize()
        new = new.set_index("date")
        self._fii = new.combine_first(self._fii) if not self._fii.empty else new
        return len(recs)

    def load_fii_dii(self, start=None):
        return self._fii.sort_index()

    def fii_dii_dates(self):
        if self._fii.empty or "fii_index_futures_long_short_ratio" not in self._fii:
            return set()
        return set(self._fii["fii_index_futures_long_short_ratio"].dropna().index)

    def upsert_sr_levels(self, doc):
        self._sr[(doc["symbol"], doc["timeframe"])] = doc

    def latest_sr_levels(self, symbol, timeframe):
        return self._sr.get((symbol, timeframe))

    def upsert_macro(self, series, s):
        self._macro = self._macro.combine_first(s.rename(series).to_frame())
        return len(s)

    def load_macro(self, start=None):
        return self._macro


class DataService:
    """High-level facade used by the GUI: `sync()` pulls everything and upserts,
    `load_*()` reads back from the store."""

    def __init__(self, store=None):
        if store is None:
            try:
                from db_handler import DBHandler
                store = DBHandler()
                self.persistent = True
            except Exception as exc:
                log.error("%s -> falling back to in-memory store", exc)
                store = MemoryStore()
                self.persistent = False
        else:
            self.persistent = not isinstance(store, MemoryStore)
        self.store = store
        self.nse = NSEClient()
        # Separate session so live polling never contends with a running sync.
        self.live_nse = NSEClient(timeout=8, retries=2)
        self.oc_nse = NSEClient(timeout=15, retries=2)    # option chain, own session/thread

    # ---------------------------------------------------------- sync
    def sync(self, symbols=None, timeframes=None, oi_backfill_days: int = 135,   # ~90 sessions, matching the cash history
             progress: Progress | None = None) -> dict:
        say = progress or (lambda m: log.info(m))
        symbols = symbols or list(config.SYMBOLS)
        timeframes = timeframes or list(config.TIMEFRAMES)
        summary: dict = {"ohlcv": {}, "fii_cash": None, "fii_cash_days": 0, "participant_oi_days": 0, "macro": 0,
                         "index_archive_days": 0, "errors": []}

        if any(_daily_from_nse(s) for s in symbols) and "1D" in timeframes:
            try:
                summary["index_archive_days"] = self.sync_index_archive(progress=say)
            except Exception as exc:
                summary["errors"].append(f"NSE index archive: {exc}")
                log.exception("Index archive sync failed")

        for sym in symbols:
            for tf in timeframes:
                if (tf == "1D" and _daily_from_nse(sym)) or (tf != "1D" and not config.supports_intraday(sym)):
                    continue
                say(f"Fetching {sym} {tf} …")
                try:
                    last = self.store.latest_timestamp(sym, tf)
                    # Incremental: refetch a small overlap window; upserts dedupe it.
                    start = (pd.Timestamp(last) - pd.Timedelta(days=7)) if last is not None else None
                    df = fetch_ohlcv(sym, tf, start=start)
                    summary["ohlcv"][f"{sym}:{tf}"] = self.store.upsert_ohlcv(sym, tf, df) if len(df) else 0
                except Exception as exc:
                    summary["errors"].append(f"{sym} {tf}: {exc}")
                    log.exception("OHLCV sync failed")

        say("Fetching FII/DII cash history (~90 sessions) …")
        try:
            hist = fetch_fii_dii_cash_history()
            self.store.upsert_fii_dii(hist)
            summary["fii_cash_days"] = len(hist)
        except Exception as exc:
            summary["errors"].append(f"FII/DII cash history: {exc}")
            log.warning("FII/DII cash history failed: %s", exc)

        say("Fetching FII/DII cash provisional data from NSE …")
        try:
            cash = self.nse.fii_dii_cash()   # latest session; may be newer than the history table
            if cash:
                self.store.upsert_fii_dii([cash])
                summary["fii_cash"] = cash
            elif not summary.get("fii_cash_days"):
                summary["errors"].append("NSE FII/DII cash endpoint unavailable")
        except Exception as exc:
            summary["errors"].append(f"FII/DII cash: {exc}")

        say("Back-filling participant-wise OI (FII long/short, PCR) …")
        try:
            have = self.participant_dates()
            today = date.today()
            recs = []
            for k in range(oi_backfill_days, -1, -1):
                d = today - timedelta(days=k)
                if d.weekday() >= 5 or d in have:
                    continue
                rec = self.nse.participant_oi(d)
                if rec:
                    recs.append(rec)
                time.sleep(0.25)   # be polite to the archive server
            self.store.upsert_fii_dii(recs)
            summary["participant_oi_days"] = len(recs)
        except Exception as exc:
            summary["errors"].append(f"Participant OI: {exc}")

        say("Fetching macro catalyst series …")
        try:
            macro = fetch_macro()
            for col in macro.columns:
                summary["macro"] += self.store.upsert_macro(col, macro[col])
        except Exception as exc:
            summary["errors"].append(f"Macro: {exc}")

        say("Sync complete.")
        return summary

    # ---------------------------------------------------------- FII / DII only
    def participant_dates(self) -> set[date]:
        """Sessions whose full participant-wise OI (all four participant types) is stored."""
        df = self.store.load_fii_dii()
        if df.empty or "pro_idx_fut_long" not in df:
            return set()
        return {pd.Timestamp(d).date() for d in df.index[df["pro_idx_fut_long"].notna()]}

    def cash_dates(self) -> set[date]:
        df = self.store.load_fii_dii()
        if df.empty or "fii_cash_net" not in df:
            return set()
        return {pd.Timestamp(d).date() for d in df.index[df["fii_cash_net"].notna()]}

    def sync_fii_dii(self, lookback_days: int = 12) -> dict:
        """Fetch whatever FII/DII data is newly available: NSE's provisional cash
        figures (published ~5-7 pm IST), participant-wise OI (~7-9 pm) for any
        recent session still missing, and the cash history pages when recent
        cash days are missing.  Cheap to call repeatedly; returns what changed."""
        out = {"cash_date": None, "oi_days": [], "history_days": 0, "errors": []}
        today = date.today()
        try:
            cash = self.nse.fii_dii_cash()
            if cash:
                self.store.upsert_fii_dii([cash])
                out["cash_date"] = pd.Timestamp(cash["date"]).date()
        except Exception as exc:
            out["errors"].append(f"NSE cash: {exc}")
        recent = [today - timedelta(days=k) for k in range(lookback_days, -1, -1)]
        recent = [d for d in recent if d.weekday() < 5]
        have_cash = self.cash_dates()
        if any(d not in have_cash for d in recent[:-1]):          # a past session's cash is missing
            try:
                hist = fetch_fii_dii_cash_history()
                self.store.upsert_fii_dii(hist)
                out["history_days"] = len(hist)
            except Exception as exc:
                out["errors"].append(f"cash history: {exc}")
        have_oi = self.participant_dates()
        recs = []
        for d in recent:
            if d in have_oi:
                continue
            try:
                rec = self.nse.participant_oi(d)
            except Exception as exc:
                out["errors"].append(f"participant OI {d}: {exc}")
                continue
            if rec:
                recs.append(rec)
                out["oi_days"].append(d)
            time.sleep(0.2)
        if recs:
            self.store.upsert_fii_dii(recs)
        df = self.store.load_fii_dii()
        out["latest_cash"] = max(self.cash_dates(), default=None)
        out["latest_oi"] = max(self.participant_dates(), default=None)
        out["rows"] = len(df)
        return out

    def sync_index_archive(self, progress: Progress | None = None, workers: int = 4) -> int:
        """Back-fill / update daily OHLCV for all NSE-archive indices at once.
        One archive file per trading day covers every index, so the first run
        downloads ~NSE_HISTORY_YEARS of files and later runs only the last week."""
        say = progress or (lambda m: log.info(m))
        today = date.today()
        # Resume from the most out-of-date index; a newly added one (no rows yet)
        # triggers a full back-fill.
        lasts = [self.store.latest_timestamp(s, "1D") for s in config.NSE_ARCHIVE_SYMBOLS.values()]
        last = None if any(t is None for t in lasts) else min(pd.Timestamp(t) for t in lasts)
        start = (pd.Timestamp(last).date() - timedelta(days=5) if last is not None
                 else today - timedelta(days=365 * config.NSE_HISTORY_YEARS + 10))
        days = [start + timedelta(days=k) for k in range((today - start).days + 1)]
        days = [d for d in days if d.weekday() < 5]
        say(f"Downloading NSE index EOD archive: {len(days)} trading days …")
        self.nse._warmup()
        frames, done = [], 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for fut in as_completed(pool.submit(self.nse.index_close_all, d) for d in days):
                done += 1
                df = fut.result()
                if df is not None and len(df):
                    frames.append(df)
                if done % 50 == 0:
                    say(f"NSE index archive: {done}/{len(days)} days …")
        if not frames:
            return 0
        allrows = pd.concat(frames)
        for sym, g in allrows.groupby("symbol"):
            g = g.drop(columns="symbol").set_index("timestamp").sort_index()
            self.store.upsert_ohlcv(sym, "1D", g[~g.index.duplicated(keep="last")])
        return len(frames)

    # ---------------------------------------------------------- loads
    def load_ohlcv(self, symbol: str, timeframe: str) -> pd.DataFrame:
        df = self.store.load_ohlcv(symbol, timeframe)
        if timeframe == "1D" and _daily_from_nse(symbol):
            if df is None or df.empty:
                self.sync_index_archive()
                df = self.store.load_ohlcv(symbol, timeframe)
            return self._with_live_bar(symbol, df)
        if df is None or df.empty:   # first run: fetch on demand
            df = fetch_ohlcv(symbol, timeframe)
            if not df.empty:
                self.store.upsert_ohlcv(symbol, timeframe, df)
        return df

    def _with_live_bar(self, symbol: str, daily: pd.DataFrame) -> pd.DataFrame:
        """NSE publishes the EOD archive after the close, so during (and just after)
        the session, build today's daily bar from stored 1H candles. Not persisted:
        the archive row replaces it on the next sync."""
        if daily.empty:
            return daily
        h = self.store.load_ohlcv(symbol, "1H")
        if h is None or h.empty:
            return daily
        h = h[h.index.normalize() > daily.index[-1].normalize()]
        if h.empty:
            return daily
        live = h.resample("1D").agg({"open": "first", "high": "max", "low": "min",
                                     "close": "last", "volume": "sum"}).dropna(subset=["close"])
        return pd.concat([daily, live[daily.columns.intersection(live.columns)]])

    def live_snapshot(self) -> dict:
        return self.live_nse.live_snapshot()

    def option_expiries(self, symbol: str) -> list[str]:
        expiries = self.oc_nse.option_expiries(symbol)
        if not expiries:
            raise RuntimeError(f"NSE returned no expiries for {symbol}")
        return expiries

    def option_chain(self, symbol: str, expiry: str | None = None) -> tuple[dict, str, list[str]]:
        """(chain json, expiry used, all expiries).  With no expiry, picks the first
        one at least 7 days out — the nearest that suits a positional seller."""
        expiries = self.option_expiries(symbol)
        expiry = pick_expiry(expiries, expiry)
        js = self.oc_nse.option_chain(symbol, expiry)
        if js is None:
            raise RuntimeError(f"NSE option chain unavailable for {symbol} {expiry}")
        return js, expiry, expiries

    def latest_levels(self, symbols=None) -> dict[str, dict]:
        """Most recent 1D sr_levels document per symbol (as saved by analyses/scans)."""
        out = {}
        for sym in symbols or config.INSTRUMENTS:
            doc = self.store.latest_sr_levels(sym, "1D")
            if doc:
                out[sym] = doc
        return out

    def load_fii_dii(self) -> pd.DataFrame:
        return self.store.load_fii_dii()

    def load_macro(self) -> pd.DataFrame:
        return self.store.load_macro()

    def save_sr_levels(self, doc: dict) -> None:
        self.store.upsert_sr_levels(doc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    svc = DataService()
    print("Persistent store:", svc.persistent)
    print(json.dumps(svc.sync(progress=print), default=str, indent=2))
