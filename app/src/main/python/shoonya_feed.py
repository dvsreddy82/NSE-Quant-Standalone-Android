"""Read-only real-time market data from Shoonya (Finvasia) — no order placement.

Shoonya retired its QuickAuth login (/NorenWClientTP/ answers 502) in April
2026 and moved to an OAuth flow.  Shoonya has not published updated docs or an
updated Python library; the endpoints below follow OpenAlgo's maintained
Shoonya integration (github.com/marketcalls/openalgo, broker/shoonya):

1. Browser: https://api.shoonya.com/OAuthlogin/authorize/oauth?client_id=<client id>
   -> the user signs in on Shoonya's own page -> redirect to the registered
   redirect URL with ?code=...  (this app catches it on 127.0.0.1)
2. POST /NorenWClientAPI/GenAcsTok  jData={"code", "checksum": sha256(client_id+secret+code)}
   -> access_token
3. REST: /NorenWClientAPI/<route>, body "jData=<json with uid>", header
   "Authorization: Bearer <token>"  (some routes want "&jKey=<token>" instead;
   we retry that way when the Bearer form is refused)
4. Websocket wss://api.shoonya.com/NorenWSAPI/ : {"t":"a","uid","actid",
   "source":"API","accesstoken"} -> "ak"; touchline subscribe {"t":"t","k":"NSE|26000#..."};
   heartbeat {"t":"h"} every 30 s.

This module uses only GenAcsTok, SearchScrip, GetOptionChain, TPSeries (intraday
candles), the read-only account books and the touchline websocket.  Order routes
(PlaceOrder / ModifyOrder / CancelOrder) are called from one place only —
trade_engine.py, the Option selling desk — and only once LIVE is switched on and armed.  The password and TOTP are typed on Shoonya's
own site, so this app never sees them.  User ID, client ID and secret (and the
day's access token) are kept in ~/.nse_analytics/shoonya.json, owner-only,
outside the repo.

Ticks are kept in memory keyed "EXCH|token"; the UI reads snapshots from it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import threading
import time
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pandas as pd
import requests

import config

log = logging.getLogger(__name__)

API = "https://api.shoonya.com/NorenWClientAPI/"
WS = "wss://api.shoonya.com/NorenWSAPI/"
AUTHORIZE = "https://api.shoonya.com/OAuthlogin/authorize/oauth?client_id={client_id}"
CALLBACK_PORT = 8765
REDIRECT_URL = f"http://127.0.0.1:{CALLBACK_PORT}/shoonya/callback"
SETTINGS_FILE = config.APP_HOME / "shoonya.json"
SETTINGS_KEYS = ("user_id", "client_id", "secret")
# The only NSE indices Shoonya streams (verified with GetQuotes over the 26000+ token range);
# every other index stays on NSE's delayed feed.
INDEX_TOKENS = {"^NSEI": "NSE|26000", "NIFTY BANK": "NSE|26009", "NIFTY NEXT 50": "NSE|26013",
                "NIFTY FINANCIAL SERVICES": "NSE|26037", "NIFTY MIDCAP SELECT": "NSE|26074"}
VIX_TOKEN = "NSE|26017"
FRESH_SECONDS = 30            # a tick older than this is not shown as live
CHAIN_STRIKES = 40            # strikes on each side of ATM to subscribe
HEARTBEAT_SECONDS = 30
TIMEOUT = 15


ENV_KEYS = {"user_id": "SHOONYA_USER_ID", "client_id": "SHOONYA_CLIENT_ID", "secret": "SHOONYA_SECRET"}


def load_settings() -> dict:
    """Saved settings, with SHOONYA_* values from the environment / .env taking precedence."""
    try:
        s = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        s = {}
    for k, env in ENV_KEYS.items():
        if os.getenv(env):
            s[k] = os.getenv(env)
    return s


def from_env() -> bool:
    return all(os.getenv(e) for e in ENV_KEYS.values())


def totp_now(key: str | None = None, at: float | None = None) -> tuple[str, int] | None:
    """RFC 6238 TOTP (6 digits, 30 s) from SHOONYA_TOTP_KEY: (code, seconds left)."""
    key = (key or os.getenv("SHOONYA_TOTP_KEY") or "").replace(" ", "").upper()
    if not key:
        return None
    secret = base64.b32decode(key + "=" * (-len(key) % 8))
    t = int(at if at is not None else time.time())
    digest = hmac.new(secret, struct.pack(">Q", t // 30), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}", 30 - t % 30


def save_settings(values: dict, token: str | None = None) -> None:
    # credentials that live in .env are not duplicated into the settings file
    data = {k: values.get(k, "") for k in SETTINGS_KEYS if not os.getenv(ENV_KEYS[k])}
    if token:
        data.update(access_token=token, token_date=date.today().isoformat())
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))
    os.chmod(SETTINGS_FILE, 0o600)


def option_tsym(underlying: str, expiry: str, kind: str, strike: float) -> str:
    """Noren NFO symbol, e.g. NIFTY17NOV26C24000 (expiry given as DD-Mon-YYYY)."""
    d = datetime.strptime(expiry, "%d-%b-%Y")
    return f"{underlying}{d:%d}{d:%b}{d:%y}".upper() + f"{'C' if kind == 'CE' else 'P'}{strike:g}"


def checksum(client_id: str, secret: str, code: str) -> str:
    return hashlib.sha256(f"{client_id}{secret}{code}".encode()).hexdigest()


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _jdata(d: dict) -> str:
    # the server splits the body on '&', so a literal '&' (e.g. M&M) must be escaped
    return "jData=" + json.dumps(d).replace("&", "\\u0026")


# ================================================================ OAuth callback
class OAuthCatcher:
    """Tiny one-shot HTTP server on 127.0.0.1 that receives Shoonya's redirect."""

    def __init__(self, port: int = CALLBACK_PORT):
        self.code: str | None = None
        catcher = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code = (q.get("code") or [None])[0]
                ok = bool(code)
                if ok:
                    catcher.code = code
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                msg = ("Shoonya sign-in received — you can close this tab and return to the app."
                       if ok else "No authorisation code in this request.")
                self.wfile.write(f"<html><body style='font-family:sans-serif;padding:40px'>{msg}"
                                 "</body></html>".encode())

            def log_message(self, *_):      # keep the code out of the console
                pass

        self.server = HTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def code_from_text(text: str) -> str | None:
    """Accept either the bare code or the whole redirected URL pasted by the user."""
    text = text.strip()
    if not text:
        return None
    if "code=" in text:
        return (urllib.parse.parse_qs(urllib.parse.urlparse(text).query).get("code") or [None])[0]
    return text


# ================================================================ feed
class ShoonyaFeed:
    def __init__(self):
        self.token: str | None = None
        self.uid: str | None = None
        self.ticks: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.ws_open = False
        self.status = "not connected"
        self.symbol_keys: dict[str, str] = {}             # app symbol -> "EXCH|token"
        self.chains: dict[tuple[str, str], dict] = {}     # (underlying, expiry) -> {key: (strike, kind, lot)}
        self._subscribed: set[str] = set()
        self._ws = None
        self._stop = threading.Event()
        self._ws_lock = threading.Lock()
        self._jkey_routes: set[str] = set()                # routes that refused the Bearer form
        self._futs: dict[str, tuple[str | None, date]] = {}  # underlying -> (near-month future key, day looked up)

    @property
    def logged_in(self) -> bool:
        return self.token is not None

    @property
    def live(self) -> bool:
        return self.logged_in and self.ws_open

    # ------------------------------------------------------------ login
    @staticmethod
    def authorize_url(settings: dict) -> str:
        return AUTHORIZE.format(client_id=urllib.parse.quote(settings["client_id"].strip()))

    def exchange_code(self, settings: dict, code: str) -> str:
        """Trade the one-time code from the browser redirect for an access token."""
        cid, sec = settings["client_id"].strip(), settings["secret"].strip()
        r = requests.post(API + "GenAcsTok", data=_jdata({"code": code, "checksum": checksum(cid, sec, code)}),
                          headers={"Content-Type": "text/plain"}, timeout=TIMEOUT)
        try:
            j = r.json()
        except ValueError:
            raise RuntimeError(f"Shoonya token exchange: HTTP {r.status_code} {r.text[:120]!r}") from None
        if j.get("stat") != "Ok" or not j.get("access_token"):
            raise RuntimeError(f"Shoonya token exchange refused: {j.get('emsg') or j}")
        return j["access_token"]

    def connect(self, settings: dict, token: str) -> str:
        """Use an access token: verify it with a cheap REST call, open the websocket
        and subscribe the index / ETF touchlines.  Blocking — call from a worker thread."""
        self.close()
        self.uid, self.token = settings["user_id"].strip(), token
        try:
            probe = self._post("SearchScrip", {"exch": "NSE", "stext": "NIFTYBEES-EQ"})
        except Exception as exc:
            probe = {"stat": "Not_Ok", "emsg": str(exc)}
        emsg = str(probe.get("emsg", ""))
        if probe.get("stat") != "Ok":
            if any(w in emsg.lower() for w in ("session", "token", "unauthor", "expired")):
                self.token = None
                self.status = "token rejected"
                raise RuntimeError(f"Shoonya did not accept the session: {emsg}")
            log.warning("Shoonya SearchScrip probe failed (continuing): %s", emsg)
        self.symbol_keys = dict(INDEX_TOKENS)
        self._resolve_etfs()
        self._start_ws()
        for _ in range(100):                      # wait up to 10 s for the 'ak' acknowledgement
            if self.ws_open:
                break
            time.sleep(0.1)
        if not self.ws_open:
            raise RuntimeError("Signed in, but the Shoonya websocket did not acknowledge within 10 s "
                               f"(status: {self.status}).")
        return f"Connected as {self.uid}"

    def close(self):
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws, self.token, self.ws_open = None, None, False
        self._subscribed.clear()
        self.status = "not connected"

    # ------------------------------------------------------------ REST
    def _post(self, route: str, data: dict) -> dict:
        body = dict(data, uid=self.uid)
        j = None
        if route not in self._jkey_routes:
            r = requests.post(API + route, data=_jdata(body), timeout=TIMEOUT,
                              headers={"Content-Type": "text/plain", "Authorization": f"Bearer {self.token}"})
            j = self._json(r)
        if j is None or (j.get("stat") != "Ok" and "session" in str(j.get("emsg", "")).lower()):
            # some Noren routes still want the token as jKey in a form body
            r = requests.post(API + route, data=_jdata(body) + f"&jKey={self.token}", timeout=TIMEOUT,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
            jk = self._json(r)
            if jk is not None and jk.get("stat") == "Ok":
                self._jkey_routes.add(route)
            j = jk if jk is not None else j
        if j is None:
            raise RuntimeError(f"{route}: HTTP {r.status_code} {r.text[:120]!r}")
        return j

    # ------------------------------------------------------------ account (read-only)
    def _book(self, route: str) -> list[dict]:
        r = self._post(route, {"actid": self.uid})
        if r.get("stat") == "Ok":
            return r.get("values") or []
        emsg = str(r.get("emsg", ""))
        if "no data" in emsg.lower():
            return []
        raise RuntimeError(f"{route}: {emsg or r}")

    def position_book(self) -> list[dict]:
        return self._book("PositionBook")

    def order_book(self) -> list[dict]:
        return self._book("OrderBook")

    def trade_book(self) -> list[dict]:
        return self._book("TradeBook")

    def limits(self) -> dict:
        r = self._post("Limits", {"actid": self.uid})
        return r if r.get("stat") == "Ok" else {}

    def time_series(self, key: str, start: float, end: float | None = None, interval: int = 1) -> list[tuple]:
        """Intraday candles (TPSeries) for "EXCH|token" between epoch seconds, oldest
        first, as (datetime IST-naive, open, high, low, close)."""
        exch, token = key.split("|", 1)
        r = self._post("TPSeries", {"exch": exch, "token": token, "st": str(int(start)),
                                    "et": str(int(end or time.time())), "intrv": str(interval)})
        if r.get("stat") != "Ok":
            emsg = str(r.get("emsg", ""))
            if "no data" in emsg.lower():
                return []
            raise RuntimeError(f"TPSeries: {emsg or r}")
        out = []
        for c in r.get("values") or []:
            if c.get("stat") not in (None, "Ok"):
                continue
            try:
                ts = datetime.strptime(c["time"], "%d-%m-%Y %H:%M:%S")
                out.append((ts, float(c["into"]), float(c["inth"]), float(c["intl"]), float(c["intc"])))
            except (KeyError, ValueError, TypeError):
                continue
        return sorted(out)

    def time_series_df(self, key: str, start: float, end: float | None = None, interval: int = 1) -> pd.DataFrame:
        """TPSeries as an OHLCV DataFrame with an IST DatetimeIndex (volume is 0 for indices)."""
        exch, token = key.split("|", 1)
        r = self._post("TPSeries", {"exch": exch, "token": token, "st": str(int(start)),
                                    "et": str(int(end or time.time())), "intrv": str(interval)})
        if r.get("stat") != "Ok":
            if "no data" in str(r.get("emsg", "")).lower():
                return pd.DataFrame()
            raise RuntimeError(f"TPSeries: {r.get('emsg') or r}")
        rows = []
        for c in r.get("values") or []:
            try:
                rows.append({"timestamp": datetime.strptime(c["time"], "%d-%m-%Y %H:%M:%S"),
                             "open": float(c["into"]), "high": float(c["inth"]), "low": float(c["intl"]),
                             "close": float(c["intc"]), "volume": float(c.get("intv") or 0)})
            except (KeyError, ValueError, TypeError):
                continue
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows).set_index("timestamp").sort_index()
        df.index = df.index.tz_localize(config.IST)
        return df[~df.index.duplicated(keep="last")]

    def key_for(self, symbol: str) -> str | None:
        """Shoonya "EXCH|token" for an app symbol (indices Shoonya streams, and resolved ETFs)."""
        if symbol == "INDIA VIX":
            return VIX_TOKEN
        return self.symbol_keys.get(symbol) or INDEX_TOKENS.get(symbol)

    def future_key(self, underlying: str) -> str | None:
        """Near-month index future ("NFO|token") for NIFTY / BANKNIFTY: looked up once a day by
        scrip search, then subscribed so its ticks carry price and open interest (oi / poi)."""
        today = date.today()
        hit = self._futs.get(underlying)
        if hit is None or hit[1] != today:
            best = None
            try:
                r = self._post("SearchScrip", {"exch": "NFO", "stext": f"{underlying} FUT"})
                for v in r.get("values") or []:
                    if v.get("instname") != "FUTIDX" or (v.get("symname") or "").upper() != underlying:
                        continue
                    try:
                        exd = datetime.strptime(v["exd"], "%d-%b-%Y").date()
                    except (KeyError, ValueError):
                        continue
                    if exd >= today and (best is None or exd < best[0]):
                        best = (exd, f"NFO|{v['token']}")
            except Exception as exc:
                log.info("Future lookup for %s failed: %s", underlying, exc)
            hit = (best[1] if best else None, today)
            self._futs[underlying] = hit
        if hit[0]:
            self.watch([hit[0]])
        return hit[0]

    def watch(self, keys) -> None:
        """Subscribe extra instruments (e.g. open-position legs) for live prices."""
        self._subscribe({k for k in keys if k and "|" in k})

    @staticmethod
    def _json(r) -> dict | None:
        try:
            j = r.json()
            return j if isinstance(j, dict) else {"stat": "Ok", "values": j}
        except ValueError:
            return None

    def _resolve_etfs(self):
        """ETF tokens via scrip search (indices beyond NIFTY/BANKNIFTY have no documented token)."""
        from concurrent.futures import ThreadPoolExecutor

        def one(sym):
            tsym = sym.removesuffix(".NS").upper() + "-EQ"
            try:
                r = self._post("SearchScrip", {"exch": "NSE", "stext": tsym})
            except Exception as exc:
                log.debug("SearchScrip %s failed: %s", tsym, exc)
                return sym, None
            hit = next((v for v in r.get("values") or [] if v.get("tsym", "").upper() == tsym), None)
            return sym, hit and f"NSE|{hit['token']}"

        self._post("SearchScrip", {"exch": "NSE", "stext": "NIFTYBEES-EQ"})   # settles Bearer vs jKey once
        etfs = [s for s, i in config.INSTRUMENTS.items() if i.kind == "etf"]
        with ThreadPoolExecutor(6) as pool:
            for sym, key in pool.map(one, etfs):
                if key:
                    self.symbol_keys[sym] = key

    # ------------------------------------------------------------ websocket
    def _start_ws(self):
        import websocket

        self._stop = threading.Event()
        stop = self._stop

        def on_open(ws):
            self.status = "authenticating websocket…"
            ws.send(json.dumps({"t": "a", "uid": self.uid, "actid": self.uid, "source": "API",
                                "accesstoken": self.token}))

        def on_message(ws, message):
            try:
                msg = json.loads(message)
            except ValueError:
                return
            t = msg.get("t")
            if t == "ak":
                if str(msg.get("s", "")).upper() == "OK":
                    self.ws_open, self.status = True, "live"
                    self._subscribed.clear()
                    keys = set(self.symbol_keys.values()) | {VIX_TOKEN}
                    for chain in self.chains.values():
                        keys |= set(chain)
                    self._subscribe(keys)
                else:
                    self.status = f"websocket auth refused: {msg.get('emsg') or msg}"
                    log.warning("Shoonya websocket auth refused: %s", msg.get("emsg") or msg)
            elif t in ("tk", "tf", "dk", "df"):
                self._on_tick(msg)

        def on_close(ws, *_):
            self.ws_open = False
            if not stop.is_set():
                self.status = "websocket closed — reconnecting"

        def on_error(ws, err):
            log.warning("Shoonya websocket error: %s", err)

        def heartbeat():
            while not stop.wait(HEARTBEAT_SECONDS):
                if self.ws_open:
                    try:
                        self._send({"t": "h"})
                    except Exception:
                        pass

        def run():
            backoff = 1
            while not stop.is_set():
                self._ws = websocket.WebSocketApp(WS, on_open=on_open, on_message=on_message,
                                                  on_close=on_close, on_error=on_error)
                self._ws.run_forever(ping_interval=HEARTBEAT_SECONDS, ping_timeout=10)
                if stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 30)

        threading.Thread(target=run, daemon=True).start()
        threading.Thread(target=heartbeat, daemon=True).start()

    def _send(self, obj: dict):
        with self._ws_lock:
            self._ws.send(json.dumps(obj))

    def _on_tick(self, msg: dict):
        key = f"{msg.get('e')}|{msg.get('tk')}"
        with self.lock:
            cur = self.ticks.setdefault(key, {})
            cur.update({k: v for k, v in msg.items() if k not in ("t", "e", "tk")})
            cur["_ts"] = time.time()

    def _subscribe(self, keys):
        new = [k for k in keys if k not in self._subscribed]
        if not new or not self.ws_open:
            return
        for i in range(0, len(new), 50):          # keep each subscribe message modest
            self._send({"t": "t", "k": "#".join(new[i:i + 50])})
        self._subscribed |= set(new)

    # ------------------------------------------------------------ reads
    def tick(self, key: str) -> dict | None:
        with self.lock:
            t = self.ticks.get(key)
            return dict(t) if t else None

    def _fresh(self, t: dict | None) -> bool:
        return bool(t) and time.time() - t.get("_ts", 0) < FRESH_SECONDS and _f(t.get("lp")) is not None

    def overlay_snapshot(self, snap: dict) -> dict:
        """Copy of an NSE live snapshot with Shoonya's real-time prices laid over it."""
        if not self.live:
            return snap
        out = dict(snap)
        quotes = {k: dict(v) for k, v in (snap.get("quotes") or {}).items()}
        n = 0
        for sym, key in self.symbol_keys.items():
            t = self.tick(key)
            if not self._fresh(t):
                continue
            lp, prev = _f(t.get("lp")), _f(t.get("c"))
            q = quotes.setdefault(sym, {})
            q.update({"last": lp, "open": _f(t.get("o")) or q.get("open"), "high": _f(t.get("h")) or q.get("high"),
                      "low": _f(t.get("l")) or q.get("low")})
            if prev:
                q.update({"prev": prev, "change": lp - prev, "pct": (lp / prev - 1) * 100})
            elif _f(t.get("pc")) is not None:
                q["pct"] = _f(t.get("pc"))
            n += 1
        vt = self.tick(VIX_TOKEN)
        if self._fresh(vt):
            lp, prev = _f(vt["lp"]), _f(vt.get("c"))
            out["vix"] = {"last": lp, "prev": prev, "change": lp - prev if prev else None,
                          "pct": (lp / prev - 1) * 100 if prev else _f(vt.get("pc"))}
        out["quotes"] = quotes
        out["source"] = f"Shoonya real-time · {n} instruments"
        return out

    # ------------------------------------------------------------ option chain
    def ensure_chain(self, underlying: str, expiry: str, spot: float, step: float) -> dict:
        """Look up the contracts around ATM once per expiry and subscribe them."""
        ck = (underlying, expiry)
        if ck not in self.chains:
            atm = round(spot / step) * step
            r = self._post("GetOptionChain", {"exch": "NFO", "tsym": option_tsym(underlying, expiry, "CE", atm),
                                              "strprc": str(atm), "cnt": str(CHAIN_STRIKES)})
            if r.get("stat") != "Ok" or not r.get("values"):
                raise RuntimeError(f"Shoonya returned no option contracts for {underlying} {expiry}: "
                                   f"{r.get('emsg') or r}")
            self.chains[ck] = {f"NFO|{v['token']}": (float(v["strprc"]), "CE" if v["optt"] == "CE" else "PE",
                                                    int(float(v.get("ls") or 1)))
                               for v in r["values"] if v.get("optt") in ("CE", "PE")}
        self._subscribe(self.chains[ck])
        return self.chains[ck]

    def chain_json(self, underlying: str, expiry: str, expiries: list[str] | None = None,
                   spot_key: str | None = None) -> tuple[dict, int]:
        """Option chain in the same shape as NSE's option-chain-v3 JSON, so
        options.analyze_chain() runs on it unchanged.  OI / volume are converted
        from shares to contracts (NSE's unit).  Returns (json, legs with a live tick)."""
        contracts = self.chains[(underlying, expiry)]
        spot_t = self.tick(spot_key or INDEX_TOKENS["^NSEI" if underlying == "NIFTY" else "NIFTY BANK"])
        spot = _f((spot_t or {}).get("lp")) or 0.0
        by_strike: dict[float, dict] = {}
        live = 0
        for key, (strike, kind, lot) in contracts.items():
            t = self.tick(key) or {}
            if self._fresh(t):
                live += 1
            oi, poi = _f(t.get("oi")) or 0.0, _f(t.get("poi"))
            by_strike.setdefault(strike, {"strikePrice": strike, "expiryDates": expiry})[kind] = {
                "openInterest": oi / lot,
                "changeinOpenInterest": (oi - poi) / lot if poi is not None else 0.0,
                "lastPrice": _f(t.get("lp")) or 0.0,
                "change": ((_f(t.get("lp")) or 0.0) - _f(t.get("c"))) if _f(t.get("c")) and _f(t.get("lp")) else 0.0,
                "buyPrice1": _f(t.get("bp1")) or 0.0,
                "sellPrice1": _f(t.get("sp1")) or 0.0,
                "totalTradedVolume": (_f(t.get("v")) or 0.0) / lot,
                "impliedVolatility": 0,
                "underlyingValue": spot,
            }
        ts = max((self.tick(k) or {}).get("_ts", 0) for k in contracts)
        return ({"records": {"underlyingValue": spot, "expiryDates": expiries or [expiry],
                             "timestamp": (pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(config.IST)
                                           .strftime("%d-%b-%Y %H:%M:%S")) if ts else None,
                             "data": [by_strike[k] for k in sorted(by_strike)]}}, live)
