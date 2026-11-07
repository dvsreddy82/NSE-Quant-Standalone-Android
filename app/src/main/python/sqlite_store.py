"""A persistent store with the same interface as MongoDB's DBHandler / data_fetcher.MemoryStore,
kept in one SQLite file.  Used by the standalone Android app (no MongoDB on a phone); works
on any machine.  Reads are served from memory (loaded on first use); every write goes to
memory and to SQLite, so nothing is lost between runs.

Option-chain snapshots are pruned to the last SNAPSHOT_DAYS days and 1-minute candles to
ONE_MIN_DAYS days, to keep the file small on a phone.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime

import numpy as np
import pandas as pd

import config
from data_fetcher import MemoryStore

SNAPSHOT_DAYS = 5
ONE_MIN_DAYS = 30


def _enc(o):
    if isinstance(o, (pd.Timestamp, datetime)):
        return {"__dt__": pd.Timestamp(o).isoformat()}
    if isinstance(o, date):
        return {"__d__": o.isoformat()}
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (set, tuple)):
        return list(o)
    raise TypeError(type(o))


def _dec(d):
    if "__dt__" in d:
        return pd.Timestamp(d["__dt__"]).to_pydatetime()
    if "__d__" in d:
        return date.fromisoformat(d["__d__"])
    return d


def _dumps(o) -> str:
    return json.dumps(o, default=_enc, allow_nan=True)


def _loads(s: str):
    return json.loads(s, object_hook=_dec)


class SqliteStore(MemoryStore):
    def __init__(self, path: str):
        super().__init__()
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS docs (coll TEXT, key TEXT, body TEXT, PRIMARY KEY (coll, key));
            CREATE TABLE IF NOT EXISTS ohlcv (symbol TEXT, tf TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
                                              PRIMARY KEY (symbol, tf, ts));
            CREATE TABLE IF NOT EXISTS fii (date TEXT PRIMARY KEY, body TEXT);
            CREATE TABLE IF NOT EXISTS macro (series TEXT, date TEXT, value REAL, PRIMARY KEY (series, date));
            CREATE TABLE IF NOT EXISTS sr (symbol TEXT, tf TEXT, body TEXT, PRIMARY KEY (symbol, tf));
        """)
        self._loaded_ohlcv: set[tuple[str, str]] = set()
        self._prune()
        self._load()

    # ---------------------------------------------------------------- load / prune
    def _prune(self):
        cut = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=SNAPSHOT_DAYS)).isoformat()
        cut1m = int((pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=ONE_MIN_DAYS)).timestamp())
        with self._lock:
            rows = self.db.execute("SELECT key, body FROM docs WHERE coll='option_chain_snapshots'").fetchall()
            old = [k for k, b in rows if (_loads(b).get("ts") is not None
                                          and pd.Timestamp(_loads(b)["ts"]).isoformat() < cut)]
            self.db.executemany("DELETE FROM docs WHERE coll='option_chain_snapshots' AND key=?", [(k,) for k in old])
            self.db.execute("DELETE FROM ohlcv WHERE tf='1m' AND ts < ?", (cut1m,))
            self.db.commit()

    def _load(self):
        with self._lock:
            for coll, key, body in self.db.execute("SELECT coll, key, body FROM docs"):
                if coll in self._docs:
                    d = _loads(body)
                    self._docs[coll][tuple(d[k] for k in self.journal_keys[coll])] = d
            rows = self.db.execute("SELECT date, body FROM fii").fetchall()
            if rows:
                recs = [dict(_loads(b), date=pd.Timestamp(dt)) for dt, b in rows]
                MemoryStore.upsert_fii_dii(self, recs)
            rows = self.db.execute("SELECT series, date, value FROM macro").fetchall()
            if rows:
                m = pd.DataFrame(rows, columns=["series", "date", "value"])
                m["date"] = pd.to_datetime(m["date"])
                self._macro = m.pivot(index="date", columns="series", values="value").sort_index()
            for sym, tf, body in self.db.execute("SELECT symbol, tf, body FROM sr"):
                self._sr[(sym, tf)] = _loads(body)

    def _ensure_ohlcv(self, symbol, timeframe):
        k = (symbol, timeframe)
        if k in self._loaded_ohlcv:
            return
        self._loaded_ohlcv.add(k)
        with self._lock:
            rows = self.db.execute("SELECT ts, o, h, l, c, v FROM ohlcv WHERE symbol=? AND tf=? ORDER BY ts",
                                   (symbol, timeframe)).fetchall()
        if rows:
            df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
            df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts"), unit="s", utc=True)).tz_convert(config.IST)
            df.index.name = "timestamp"
            MemoryStore.upsert_ohlcv(self, symbol, timeframe, df)

    # ---------------------------------------------------------------- documents
    def upsert_docs(self, name, docs, insert_only=()):
        n = MemoryStore.upsert_docs(self, name, docs, insert_only)
        keys = self.journal_keys[name]
        with self._lock:
            rows = []
            for d in docs:
                k = tuple(d[x] for x in keys)
                full = self._docs[name][k]
                rows.append((name, _dumps(list(k)), _dumps(full)))
            self.db.executemany("INSERT OR REPLACE INTO docs (coll, key, body) VALUES (?, ?, ?)", rows)
            self.db.commit()
        return n

    def delete_docs(self, name, query):
        keys = [k for k, d in self._docs[name].items() if all(d.get(a) == b for a, b in query.items())]
        n = MemoryStore.delete_docs(self, name, query)
        with self._lock:
            self.db.executemany("DELETE FROM docs WHERE coll=? AND key=?", [(name, _dumps(list(k))) for k in keys])
            self.db.commit()
        return n

    # ---------------------------------------------------------------- candles
    def upsert_ohlcv(self, symbol, timeframe, df):
        self._ensure_ohlcv(symbol, timeframe)
        n = MemoryStore.upsert_ohlcv(self, symbol, timeframe, df)
        if df is None or df.empty:
            return n
        idx = df.index if df.index.tz is not None else df.index.tz_localize(config.IST)
        vol = df["volume"] if "volume" in df else pd.Series(0.0, index=df.index)
        rows = [(symbol, timeframe, int(ts.timestamp()), float(o), float(h), float(l), float(c),
                 float(v) if v == v else 0.0)
                for ts, o, h, l, c, v in zip(idx, df["open"], df["high"], df["low"], df["close"], vol)]
        with self._lock:
            self.db.executemany("INSERT OR REPLACE INTO ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            self.db.commit()
        return n

    def load_ohlcv(self, symbol, timeframe, start=None, limit=None):
        self._ensure_ohlcv(symbol, timeframe)
        return MemoryStore.load_ohlcv(self, symbol, timeframe, start, limit)

    def latest_timestamp(self, symbol, timeframe):
        self._ensure_ohlcv(symbol, timeframe)
        return MemoryStore.latest_timestamp(self, symbol, timeframe)

    # ---------------------------------------------------------------- FII / DII, macro, levels
    def upsert_fii_dii(self, records):
        n = MemoryStore.upsert_fii_dii(self, records)
        days = {pd.Timestamp(r["date"]).normalize() for r in records if r.get("date") is not None}
        rows = []
        for d in days:
            if d in self._fii.index:
                row = {k: v for k, v in self._fii.loc[d].to_dict().items() if not (isinstance(v, float) and v != v)}
                rows.append((d.strftime("%Y-%m-%d"), _dumps(row)))
        with self._lock:
            self.db.executemany("INSERT OR REPLACE INTO fii VALUES (?, ?)", rows)
            self.db.commit()
        return n

    def upsert_macro(self, series, s):
        n = MemoryStore.upsert_macro(self, series, s)
        rows = [(series, pd.Timestamp(d).strftime("%Y-%m-%d"), float(v)) for d, v in s.dropna().items()]
        with self._lock:
            self.db.executemany("INSERT OR REPLACE INTO macro VALUES (?, ?, ?)", rows)
            self.db.commit()
        return n

    def upsert_sr_levels(self, doc):
        MemoryStore.upsert_sr_levels(self, doc)
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO sr VALUES (?, ?, ?)",
                            (doc["symbol"], doc["timeframe"], _dumps({k: v for k, v in doc.items() if k != "_id"})))
            self.db.commit()
