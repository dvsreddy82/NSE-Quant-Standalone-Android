"""Alert categories and which of them raise a notification.

Every alert the app produces maps to one category (`category`).  The Mac's *Alert settings* page and the
phone's Settings use the same catalogue (served in /api/alerts as "categories"); each device keeps its own
selection.  A category that is switched off still records and shows its alerts on their pages — only the
notification is skipped.  The Mac / standalone engine selection lives in `alert_prefs.json` in the app home.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# key, group, label, what it covers
CATALOG: list[tuple[str, str, str, str]] = [
    ("risk", "Risk & positions", "Writer risk monitor (RED)", "Squeeze, put-flush, divergence and negative-gamma checks"),
    ("account", "Risk & positions", "Account limits", "Daily loss, margin use, book delta and 4 % stress loss"),
    ("exit", "Risk & positions", "Position EXIT checks", "Stop rules hit on your open positions"),
    ("w_profit", "Writer signals", "Profit targets", "50 % / 70 % of a book's credit captured"),
    ("w_adjust", "Writer signals", "Short-leg delta and expected move", "Short delta past 0.30 / 0.40; strike inside 1σ"),
    ("w_exit", "Writer signals", "Expiry gamma and time exits", "Expiry-day gamma near a short strike; low DTE close to a short"),
    ("w_vol", "Writer signals", "Volatility", "India VIX spike, ATM IV spike, put skew steepening"),
    ("w_position", "Writer signals", "Positioning", "Futures buildup, OI wall shifts, PCR extremes, max pain"),
    ("w_conditions", "Writer signals", "Writing conditions (daily)", "IV rank, IV vs realised vol, chain verdict"),
    ("w_discipline", "Writer signals", "Discipline", "Losing streak, too many orders"),
    ("levels", "Levels & direction", "Level breaks", "OI walls, daily S/R zones, opening range on 5-min closes"),
    ("direction", "Levels & direction", "15-minute direction calls", "NIFTY / BANKNIFTY call every 15 minutes"),
    ("open_play", "Chart signals", "Open play", "Gap type → direction → whole number → PDH / PDL"),
    ("c_pivots", "Chart signals", "Pivots and CPR", "Daily / weekly / monthly pivots, R1–R3, S1–S3, CPR breaks"),
    ("c_gaps", "Chart signals", "Gaps and untouched CPRs", "Opening-gap fill, older unfilled gaps, virgin CPRs"),
    ("c_ranges", "Chart signals", "First-hour and 9:30–10:00 ranges", "Breakouts / breakdowns of the morning ranges"),
    ("c_trend", "Chart signals", "Moving averages and Savitzky", "EMA 34×50 / 10×20 crosses, all-aligned, Savitzky flips"),
    ("global", "Market", "Global markets", "GIFT Nifty and world markets; the before-the-open read"),
    ("news", "Market", "High-impact news", "Market-moving headlines"),
    ("breadth", "Market", "BANKNIFTY breadth", "Heavyweights against the index, narrow breadth"),
    ("setup", "Market", "Evening FII/DII setup", "Squeeze-loading setup after the close"),
    ("volume", "Market", "Daily unusual-volume summary", "NIFTY 50 / BANKNIFTY stocks, after the close"),
]
KEYS = [c[0] for c in CATALOG]

WRITER = {"profit": "w_profit", "delta": "w_adjust", "emove": "w_adjust", "gamma": "w_exit", "time": "w_exit",
          "vix": "w_vol", "ivspike": "w_vol", "skew": "w_vol", "futures": "w_position", "wall": "w_position",
          "pcr": "w_position", "maxpain": "w_position", "conditions": "w_conditions", "streak": "w_discipline",
          "overtrade": "w_discipline"}
CHART = {"op": "open_play", "cross": "c_pivots", "cpr": "c_pivots", "cprtouch": "c_pivots", "gap": "c_gaps",
         "gapfill": "c_gaps", "gapin": "c_gaps", "gapfull": "c_gaps", "vcpr": "c_gaps", "fh": "c_ranges",
         "r930": "c_ranges", "ma": "c_trend", "ema": "c_trend", "aligned": "c_trend", "sg": "c_trend"}
SOURCES = {"Writer risk monitor": "risk", "Account limits": "account", "My positions": "exit",
           "BANKNIFTY monitor": "breadth", "Evening setup": "setup", "Volume summary": "volume",
           "Global markets": "global", "Level break": "levels", "Direction call": "direction",
           "Writer signals": None}


def category(a: dict) -> str | None:
    """The category of a stored alert (market_alerts) or an /api/alerts item."""
    if a.get("cat"):
        return a["cat"]
    kind = a.get("kind") or a.get("level")
    if kind == "WRITER" or a.get("source") == "Writer signals":
        return WRITER.get(a.get("signal"), "w_position")
    if kind == "LEVEL":
        return CHART.get(a.get("signal"), "c_pivots") if a.get("src") == "chart" or a.get("signal") in CHART \
            else "levels"
    by_kind = {"GLOBAL": "global", "VOLUME": "volume", "DIRECTION": "direction", "NEWS": "news", "SETUP": "setup"}
    if kind in by_kind:
        return by_kind[kind]
    return SOURCES.get(a.get("source"))


class AlertPrefs:
    """Which categories notify on this machine (all on by default)."""

    def __init__(self, path: str | Path | None = None):
        home = Path(os.getenv("NSE_APP_HOME", str(Path.home() / ".nse_analytics")))
        self.path = Path(path) if path else home / "alert_prefs.json"
        self.off: set[str] = set()
        self.load()

    def load(self):
        try:
            self.off = set(json.loads(self.path.read_text()).get("off") or []) & set(KEYS)
        except FileNotFoundError:
            self.off = set()
        except Exception as exc:
            log.warning("alert_prefs.json unreadable, all alerts on: %s", exc)
            self.off = set()

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"off": sorted(self.off)}, indent=1))
        except Exception as exc:
            log.warning("alert_prefs.json not saved: %s", exc)

    def enabled(self, cat: str | None) -> bool:
        return cat is None or cat not in self.off

    def allows(self, a: dict) -> bool:
        return self.enabled(category(a))

    def set(self, cat: str, on: bool):
        (self.off.discard if on else self.off.add)(cat)
        self.save()


def catalog_json() -> list[dict]:
    return [{"key": k, "group": g, "label": l, "help": h} for k, g, l, h in CATALOG]
