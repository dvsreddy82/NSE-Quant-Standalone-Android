"""A tiny stand-in for the parts of yfinance this app uses (`Ticker(t).history(...)`), built on
Yahoo's public chart API with `requests` only.  Used where yfinance can't be installed (the
standalone Android app) or when NSE_YAHOO_LITE=1.  Returns the same shape as yfinance:
Open / High / Low / Close / Volume columns and a tz-aware DatetimeIndex in the exchange's zone.
"""
from __future__ import annotations

import time

import pandas as pd
import requests

UA = {"User-Agent": "Mozilla/5.0"}       # Yahoo answers 429 to a full browser UA without its cookies
CHART = "https://{}.finance.yahoo.com/v8/finance/chart/{}"


def _seconds(period: str) -> int:
    if period == "max":
        return 30 * 365 * 86400
    if period.endswith("mo"):
        return int(period[:-2]) * 31 * 86400
    n, unit = int(period[:-1] or 1), period[-1]
    return n * {"d": 86400, "y": 365 * 86400, "w": 7 * 86400}.get(unit, 86400)


class Ticker:
    def __init__(self, symbol: str):
        self.symbol = symbol

    def history(self, period: str | None = None, start=None, end=None, interval: str = "1d",
                auto_adjust: bool = False, actions: bool = False, **_kw) -> pd.DataFrame:
        now = int(time.time())
        if start is not None:
            p1 = int(pd.Timestamp(start).timestamp())
        else:
            p1 = now - _seconds(period or "1mo")
        p2 = int(pd.Timestamp(end).timestamp()) if end is not None else now + 86400
        params = {"period1": p1, "period2": p2, "interval": {"60m": "1h"}.get(interval, interval),
                  "includePrePost": "false", "events": "div,split"}
        r = None
        for host in ("query1", "query2", "query1"):
            r = requests.get(CHART.format(host, self.symbol), params=params, headers=UA, timeout=20)
            if r.status_code != 429:
                break
            time.sleep(1.5)
        r.raise_for_status()
        res = (r.json().get("chart") or {}).get("result") or []
        if not res or not res[0].get("timestamp"):
            return pd.DataFrame()
        res = res[0]
        q = res["indicators"]["quote"][0]
        tz = res["meta"].get("exchangeTimezoneName") or "UTC"
        idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(tz)
        if interval in ("1d", "1wk", "1mo"):
            idx = idx.normalize()
        df = pd.DataFrame({"Open": q.get("open"), "High": q.get("high"), "Low": q.get("low"),
                           "Close": q.get("close"), "Volume": q.get("volume")}, index=idx)
        df = df.astype(float)
        df = df[~df.index.duplicated(keep="last")].dropna(subset=["Close"])
        return df
