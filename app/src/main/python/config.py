"""Central configuration for the NSE Analytics terminal.

Every value can be overridden with an environment variable of the same name,
so the code never needs editing to point at another MongoDB or change tuning.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines, # comments). Real environment
    variables win over the file. The file is git-ignored — keep secrets there."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip().removeprefix("export ").strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        os.environ.setdefault(key, val)


# The project's .env (when run from source), then ~/.nse_analytics/.env — the one the packaged
# macOS app reads, since secrets are never bundled into it.  The first value seen wins.
_load_dotenv(Path(__file__).resolve().parent / ".env")
_load_dotenv(Path(os.getenv("NSE_APP_HOME", str(Path.home() / ".nse_analytics"))) / ".env")


def _env(name: str, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return type(default)(raw)


# ---------------------------------------------------------------- database
MONGO_URI: str = _env("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB: str = _env("MONGO_DB", "nse_analytics")
MONGO_TIMEOUT_MS: int = _env("MONGO_TIMEOUT_MS", 3000)

# ---------------------------------------------------------------- instruments
@dataclass(frozen=True)
class Instrument:
    symbol: str                 # id used in MongoDB and the UI
    name: str
    group: str
    kind: str = "index"         # "index" | "etf"
    yahoo: str | None = None    # Yahoo ticker (intraday, and daily when daily_source == "yahoo")
    nse_name: str | None = None  # name as it appears in NSE's ind_close_all_*.csv archive
    daily_source: str = "yahoo"  # "yahoo" | "nse"
    etf: str | None = None      # tradable ETF tracking this index
    underlying: str | None = None  # for ETFs: the index symbol they track


def _ix(name: str, group: str, yahoo: str | None, etf: str | None = None,
        display: str | None = None) -> Instrument:
    """NSE index: daily OHLCV from the NSE archive (Yahoo's daily history is
    missing or truncated for most of these), intraday from Yahoo."""
    return Instrument(symbol=name, name=display or name.title().replace("Nifty", "NIFTY"), group=group,
                      yahoo=yahoo, nse_name=name, daily_source="nse", etf=etf)


_INDICES = [
    Instrument("^NSEI", "NIFTY 50", "Large cap", yahoo="^NSEI", nse_name="NIFTY 50", etf="NIFTYBEES.NS"),
    _ix("NIFTY NEXT 50", "Large cap", "^NSMIDCP", "JUNIORBEES.NS"),
    _ix("NIFTY 100", "Large cap", "^CNX100", "NIF100BEES.NS"),
    _ix("NIFTY 200", "Large cap", "^CNX200"),
    _ix("NIFTY 500", "Broad market", "^CRSLDX", "MONIFTY500.NS"),
    _ix("NIFTY TOTAL MARKET", "Broad market", "NIFTY_TOTAL_MKT.NS"),
    _ix("NIFTY500 MULTICAP 50:25:25", "Broad market", "NIFTY500_MULTICAP.NS", "MULTICAP.NS"),
    _ix("NIFTY LARGEMIDCAP 250", "Broad market", "NIFTY_LARGEMID250.NS"),
    _ix("NIFTY MIDSMALLCAP 400", "Broad market", "NIFTYMIDSML400.NS"),
    _ix("NIFTY MIDCAP 50", "Mid cap", "^NSEMDCP50"),
    _ix("NIFTY MIDCAP 100", "Mid cap", "NIFTY_MIDCAP_100.NS", "MOM100.NS"),
    _ix("NIFTY MIDCAP 150", "Mid cap", "NIFTYMIDCAP150.NS", "MID150BEES.NS"),
    _ix("NIFTY MIDCAP SELECT", "Mid cap", None, "MIDSELIETF.NS"),
    _ix("NIFTY SMALLCAP 50", "Small cap", "NIFTYSMLCAP50.NS"),
    _ix("NIFTY SMALLCAP 100", "Small cap", "^CNXSC"),
    _ix("NIFTY SMALLCAP 250", "Small cap", "NIFTYSMLCAP250.NS", "HDFCSML250.NS"),
    _ix("NIFTY SMALLCAP 500", "Small cap", None),
    _ix("NIFTY MICROCAP 250", "Small cap", "NIFTY_MICROCAP250.NS"),
    # ---- sectoral
    _ix("NIFTY BANK", "Sectoral", "^NSEBANK", "BANKBEES.NS", "NIFTY Bank"),
    _ix("NIFTY PRIVATE BANK", "Sectoral", "NIFTY_PVT_BANK.NS", "PVTBANIETF.NS", "NIFTY Private Bank"),
    _ix("NIFTY PSU BANK", "Sectoral", "^CNXPSUBANK", "PSUBNKBEES.NS", "NIFTY PSU Bank"),
    _ix("NIFTY FINANCIAL SERVICES", "Sectoral", "NIFTY_FIN_SERVICE.NS", "BFSI.NS", "NIFTY Fin Services (FINNIFTY)"),
    _ix("NIFTY FINANCIAL SERVICES 25/50", "Sectoral", None, None, "NIFTY Fin Services 25/50"),
    _ix("NIFTY AUTO", "Sectoral", "^CNXAUTO", "AUTOBEES.NS", "NIFTY Auto"),
    _ix("NIFTY IT", "Sectoral", "^CNXIT", "ITBEES.NS", "NIFTY IT"),
    _ix("NIFTY FMCG", "Sectoral", "^CNXFMCG", "FMCGIETF.NS", "NIFTY FMCG"),
    _ix("NIFTY METAL", "Sectoral", "^CNXMETAL", "METALIETF.NS", "NIFTY Metal"),
    _ix("NIFTY PHARMA", "Sectoral", "^CNXPHARMA", "PHARMABEES.NS", "NIFTY Pharma"),
    _ix("NIFTY HEALTHCARE INDEX", "Sectoral", "NIFTY_HEALTHCARE.NS", "HEALTHY.NS", "NIFTY Healthcare"),
    _ix("NIFTY REALTY", "Sectoral", "^CNXREALTY", "MOREALTY.NS", "NIFTY Realty"),
    _ix("NIFTY MEDIA", "Sectoral", "^CNXMEDIA", None, "NIFTY Media"),
    _ix("NIFTY OIL & GAS", "Sectoral", "NIFTY_OIL_AND_GAS.NS", "OILIETF.NS", "NIFTY Oil & Gas"),
    _ix("NIFTY CONSUMER DURABLES", "Sectoral", "NIFTY_CONSR_DURBL.NS", None, "NIFTY Consumer Durables"),
    _ix("NIFTY CHEMICALS", "Sectoral", "NIFTY_CHEMICALS.NS", "GROWWCHEM.NS", "NIFTY Chemicals"),
]
_ETF_NAMES = {
    "NIFTYBEES.NS": "Nippon India ETF Nifty 50 BeES",
    "JUNIORBEES.NS": "Nippon India ETF Nifty Next 50 Junior BeES",
    "NIF100BEES.NS": "Nippon India ETF Nifty 100",
    "BANKBEES.NS": "Nippon India ETF Nifty Bank BeES",
    "MONIFTY500.NS": "Motilal Oswal Nifty 500 ETF",
    "MULTICAP.NS": "Mirae Asset Nifty500 Multicap 50:25:25 ETF",
    "MOM100.NS": "Motilal Oswal Nifty Midcap 100 ETF",
    "MID150BEES.NS": "Nippon India ETF Nifty Midcap 150",
    "MIDSELIETF.NS": "ICICI Pru Nifty Midcap Select ETF",
    "HDFCSML250.NS": "HDFC Nifty Smallcap 250 ETF",
    "PVTBANIETF.NS": "ICICI Pru Nifty Private Bank ETF",
    "PSUBNKBEES.NS": "Nippon India ETF Nifty PSU Bank BeES",
    "BFSI.NS": "Mirae Asset Nifty Financial Services ETF",
    "AUTOBEES.NS": "Nippon India Nifty Auto ETF",
    "ITBEES.NS": "Nippon India ETF Nifty IT",
    "FMCGIETF.NS": "ICICI Pru Nifty FMCG ETF",
    "METALIETF.NS": "ICICI Pru Nifty Metal ETF",
    "PHARMABEES.NS": "Nippon India Nifty Pharma ETF",
    "HEALTHY.NS": "Aditya Birla SL Nifty Healthcare ETF",
    "MOREALTY.NS": "Motilal Oswal Nifty Realty ETF",
    "OILIETF.NS": "ICICI Pru Nifty Oil & Gas ETF",
    "GROWWCHEM.NS": "Groww Nifty Chemicals ETF",
}
_ETFS = [Instrument(ix.etf, _ETF_NAMES[ix.etf], "ETFs (tradable)", kind="etf", yahoo=ix.etf,
                    underlying=ix.symbol) for ix in _INDICES if ix.etf]

INSTRUMENTS: dict[str, Instrument] = {i.symbol: i for i in _INDICES + _ETFS}
SYMBOLS: dict[str, str] = {s: i.name for s, i in INSTRUMENTS.items()}
INDEX_SYMBOLS = [i.symbol for i in _INDICES]
NSE_ARCHIVE_SYMBOLS = {i.nse_name.lower(): i.symbol for i in _INDICES if i.daily_source == "nse"}
DEFAULT_SYMBOL = "NIFTYBEES.NS"
EXECUTION_SYMBOL = "NIFTYBEES.NS"   # default instrument we trade
REFERENCE_INDEX = "^NSEI"
NSE_HISTORY_YEARS: int = _env("NSE_HISTORY_YEARS", 2)   # archive back-fill depth
LIVE_REFRESH_SECONDS: int = _env("LIVE_REFRESH_SECONDS", 15)  # live dashboard poll interval
NIFTY_LOT: int = _env("NIFTY_LOT", 65)            # F&O lot sizes — NSE revises these periodically
BANKNIFTY_LOT: int = _env("BANKNIFTY_LOT", 30)
OPTIONS_REFRESH_SECONDS: int = _env("OPTIONS_REFRESH_SECONDS", 60)
TRADES_SYNC_SECONDS: int = _env("TRADES_SYNC_SECONDS", 30)     # Trades tab: broker book refresh
TRADING_CAPITAL: float = _env("TRADING_CAPITAL", 0.0)          # 0 = use Shoonya's cash limit
BROKERAGE_PER_ORDER: float = _env("BROKERAGE_PER_ORDER", 0.0)  # Shoonya: zero brokerage
# book-risk limits (book_risk.py): % of capital (TRADING_CAPITAL, or margin used + available)
DAILY_LOSS_LIMIT_PCT: float = _env("DAILY_LOSS_LIMIT_PCT", 2.0)    # stop adding risk past this day loss
MOVE_LOSS_LIMIT_PCT: float = _env("MOVE_LOSS_LIMIT_PCT", 3.0)      # worst P&L of a book for a 4% move
MAX_MARGIN_USE_PCT: float = _env("MAX_MARGIN_USE_PCT", 60.0)       # keep a buffer for gap margin calls
BOOK_DELTA_LOTS: float = _env("BOOK_DELTA_LOTS", 3.0)              # net delta per book, in index lots
# writer signals (writer_signals.py)
PROFIT_TAKE_PCT: float = _env("PROFIT_TAKE_PCT", 50.0)             # % of the credit captured: consider booking
PROFIT_TAKE_HIGH_PCT: float = _env("PROFIT_TAKE_HIGH_PCT", 70.0)   # … and the strong nudge
SHORT_DELTA_WARN: float = _env("SHORT_DELTA_WARN", 0.30)           # short leg delta: plan a roll
SHORT_DELTA_EXIT: float = _env("SHORT_DELTA_EXIT", 0.40)           # short leg delta: act
VIX_SPIKE_PCT: float = _env("VIX_SPIKE_PCT", 8.0)                  # India VIX up this % on the day
ATM_IV_SPIKE_PTS: float = _env("ATM_IV_SPIKE_PTS", 2.0)            # ATM IV up this many points since the open
EXPIRY_GAMMA_PCT: float = _env("EXPIRY_GAMMA_PCT", 0.5)            # expiry day after 13:00: spot this close to a short
TIME_EXIT_DTE: int = _env("TIME_EXIT_DTE", 2)                      # days to expiry for the time-based exit
TIME_EXIT_PCT: float = _env("TIME_EXIT_PCT", 1.0)                  # … with a short strike this close (% of spot)
PCR_HIGH: float = _env("PCR_HIGH", 1.5)
PCR_LOW: float = _env("PCR_LOW", 0.6)
SKEW_RISE_PTS: float = _env("SKEW_RISE_PTS", 2.0)                  # put-minus-call IV up this much since the open
FUT_PRICE_PCT: float = _env("FUT_PRICE_PCT", 0.3)                  # futures buildup: price move on the day
FUT_OI_PCT: float = _env("FUT_OI_PCT", 2.0)                        # … and open-interest change on the day
LOSING_STREAK_DAYS: int = _env("LOSING_STREAK_DAYS", 3)
MAX_ORDERS_PER_DAY: int = _env("MAX_ORDERS_PER_DAY", 12)           # executed orders before the overtrading nudge
# Statutory charges (fractions of premium / turnover) used to estimate net P&L.
# NSE / SEBI revise these; update here if they change.
CHARGES = {
    "option": {"stt_sell": 0.001, "stt_buy": 0.0, "exchange": 0.0003503, "stamp_buy": 0.00003},
    "future": {"stt_sell": 0.0002, "stt_buy": 0.0, "exchange": 0.0000173, "stamp_buy": 0.00002},
    "equity": {"stt_sell": 0.001, "stt_buy": 0.001, "exchange": 0.0000297, "stamp_buy": 0.00015},
}


def pair_of(symbol: str) -> str | None:
    """ETF for an index, or the index for an ETF (used to translate levels)."""
    inst = INSTRUMENTS.get(symbol)
    if inst is None:
        return None
    return inst.etf if inst.kind == "index" else inst.underlying


def supports_intraday(symbol: str) -> bool:
    inst = INSTRUMENTS.get(symbol)
    return inst is None or inst.yahoo is not None

# Timeframe -> (yfinance interval, yfinance period). Daily pulls ~2y so the
# 200 EMA is fully warmed up; intraday is capped by Yahoo's history limits.
TIMEFRAMES: dict[str, tuple[str, str]] = {
    "1D": ("1d", "2y"),
    "1H": ("1h", "180d"),
    "15m": ("15m", "60d"),
}

PHONE_API_PORT: int = _env("PHONE_API_PORT", 8787)   # read-only API for the Android companion app

# Timeframes kept in index_ohlcv: the analysis timeframes plus 1-minute bars for the intraday chart.
STORED_TIMEFRAMES: list[str] = list(TIMEFRAMES) + ["1m"]

# Macro series used by the catalyst module (Yahoo tickers).
MACRO_TICKERS: dict[str, str] = {
    "crude": "BZ=F",        # Brent crude futures
    "usdinr": "INR=X",      # USD/INR
    "us10y": "^TNX",        # US 10Y yield (x10)
    "indiavix": "^INDIAVIX",
    "dxy": "DX-Y.NYB",      # Dollar index
}

# ---------------------------------------------------------------- analytics
ANALYSIS_WINDOW_DAYS: int = _env("ANALYSIS_WINDOW_DAYS", 60)   # S/R look-back
SWING_WINDOW: int = _env("SWING_WINDOW", 3)                   # fractal half-width
ZONE_ATR_MULT: float = _env("ZONE_ATR_MULT", 0.35)             # zone half-width
PROXIMITY_ATR_MULT: float = _env("PROXIMITY_ATR_MULT", 0.6)    # "price is testing level"
VALUE_AREA_PCT: float = _env("VALUE_AREA_PCT", 0.70)
VP_BINS: int = _env("VP_BINS", 48)
RSI_PERIOD: int = 14
RSI_OVERBOUGHT: float = 70.0
RSI_OVERSOLD: float = 30.0
FII_LS_BEARISH: float = _env("FII_LS_BEARISH", 0.60)   # FII index-fut long/short below => heavy shorting
FII_LS_BULLISH: float = _env("FII_LS_BULLISH", 1.20)
TRADING_DAYS_PER_MONTH = 21

# ---------------------------------------------------------------- paths
APP_HOME = Path(_env("NSE_APP_HOME", str(Path.home() / ".nse_analytics")))
CHART_DIR = APP_HOME / "chart"
LOG_FILE = APP_HOME / "app.log"
APP_HOME.mkdir(parents=True, exist_ok=True)
CHART_DIR.mkdir(parents=True, exist_ok=True)

IST = "Asia/Kolkata"
