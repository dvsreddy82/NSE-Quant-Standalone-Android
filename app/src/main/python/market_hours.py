"""Market-hours helpers shared by the desktop app and the standalone Android engine (no Qt)."""
from __future__ import annotations

from datetime import datetime, time as dtime

import pandas as pd

import config

IST_OPEN, IST_CLOSE = dtime(9, 15), dtime(15, 30)
SESSION = (dtime(9, 0), dtime(15, 40))          # when Shoonya is used (IST, weekdays): pre-open to close + 10 min


def now_ist() -> datetime:
    return pd.Timestamp.now(tz=config.IST).to_pydatetime()


def market_open(t: datetime | None = None) -> bool:
    t = t or now_ist()
    return t.weekday() < 5 and IST_OPEN <= t.time() <= IST_CLOSE


def in_session(t: datetime | None = None) -> bool:
    t = t or now_ist()
    return t.weekday() < 5 and SESSION[0] <= t.time() <= SESSION[1]
