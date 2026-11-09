"""The standalone app's engine: everything the Mac app does, without Qt and without a Mac.

It runs inside the Android app (Chaquopy) on background threads and owns:
  * the single Shoonya session (read-only), used only in market hours (9:00–15:40 IST),
    with NSE / Yahoo / the local store as fallbacks;
  * the same analysis modules as the desktop app (copied unchanged from it: option chain,
    writer risk, book risk, FII/DII, BANKNIFTY constituents, news, global markets, level
    breaks, 15-minute direction calls);
  * a SQLite store (sqlite_store.py) in the app's private files instead of MongoDB.

It exposes the same attributes the desktop window does (`risk_view.results`, `trades.data`,
`fii_view.res`, `watch`, `news_svc`, …), so api_routes.ApiRoutes answers the phone UI with
exactly the JSON the companion app used to fetch from the Mac.

Scheduling mirrors app.py (one loop, once a second):
  ticker 1 s · NSE snapshot 15 s when Shoonya isn't streaming · chains 5 min (market hours)
  · risk 3 min (market hours) / 30 min · trades 30 s while connected · BANKNIFTY 5 min (market
  hours) and on demand · news 2 / 10 min · global 5 min · levels on every 5-min close ·
  direction calls 9:30–15:15 every 15 min · FII/DII 4:30–11 pm until published · EOD after
  6:30 pm · Shoonya session open / close at the window edges.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime
from types import SimpleNamespace

import pandas as pd

import book_risk
import config
import fii_analysis
import risk_engine
import trades_engine as te
from api_routes import ApiRoutes
from banknifty_service import BankNiftyService
from data_fetcher import DataService
from indicators import analyze, to_sr_document
from market_data import MarketData
from market_hours import in_session, market_open, now_ist
from news_service import NewsService, market_window as news_window
from options import analyze_chain
from options_service import OptionsService
from shoonya_feed import (INDEX_TOKENS, REDIRECT_URL, VIX_TOKEN, OAuthCatcher, ShoonyaFeed, code_from_text,
                          load_settings, save_settings, totp_now)
from sqlite_store import SqliteStore
from trades_service import TradesService
import volume_watch
from volume_watch import VolumeWatch
import market_summary
from market_summary import MarketSummary
from watch_service import WatchService

log = logging.getLogger("engine")

FII_WINDOW = (dtime(16, 30), dtime(23, 0))
EOD_AFTER = dtime(18, 30)
PHONE_INDICES = ["^NSEI", "NIFTY BANK"]            # daily levels kept for the two traded indices


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ====================================================================== broker
class Broker:
    """Shoonya on the phone: OAuth in the phone's browser (redirect to 127.0.0.1:8765 inside the
    app), today's token reused, used only inside the market-hours window."""

    def __init__(self):
        self.feed = ShoonyaFeed()
        self.busy = False
        self.error = ""
        self.catcher: OAuthCatcher | None = None
        self.manual_after_hours = False
        self.user_off: date | None = None
        self.closing = False

    @property
    def live(self) -> bool:
        return self.feed.live

    @property
    def healthy(self) -> bool:
        f = self.feed
        if not f.live:
            return False
        if not market_open():
            return True
        return any(f._fresh(f.tick(INDEX_TOKENS[s])) for s in ("NIFTY BANK", "^NSEI"))

    def state(self) -> tuple[str, str, str]:
        f = self.feed
        if self.busy:
            return "connecting", "Shoonya: connecting…", ""
        if f.live and not self.healthy:
            return "degraded", "Shoonya: no ticks — using NSE until they resume", ""
        if f.live:
            return "live", "Shoonya real-time", ""
        if f.logged_in:
            return "degraded", f"Shoonya: {f.status} — using NSE", ""
        if not in_session():
            return "closed", "Market closed — NSE / stored data (Shoonya is used 9:00–15:40)", ""
        s = load_settings()
        if not all(s.get(k) for k in ("user_id", "client_id", "secret")):
            return "off", "Shoonya not set up — add your API details in Settings", ""
        if not (s.get("access_token") and s.get("token_date") == date.today().isoformat()):
            return "off", "Shoonya: sign in for today (Settings → Sign in)", ""
        return "off", "Shoonya: not connected — using NSE", ""

    def token_today(self) -> str | None:
        s = load_settings()
        return s["access_token"] if s.get("access_token") and s.get("token_date") == date.today().isoformat() else None

    # ---- sign-in (called from the UI thread via phone_api; blocking parts run on the pool)
    def begin_signin(self) -> str:
        s = load_settings()
        if not all(s.get(k) for k in ("user_id", "client_id", "secret")):
            raise RuntimeError("Enter your Shoonya user ID, client ID and secret first")
        self.cancel_signin()
        self.catcher = OAuthCatcher()
        self.error = ""
        return ShoonyaFeed.authorize_url(s)

    def cancel_signin(self):
        if self.catcher:
            try:
                self.catcher.close()
            except Exception:
                pass
            self.catcher = None

    def pending_code(self) -> str | None:
        return self.catcher.code if self.catcher else None

    def connect_with(self, code: str | None = None, token: str | None = None) -> str:
        s = load_settings()
        self.busy = True
        try:
            tok = token or self.feed.exchange_code(s, code)
            msg = self.feed.connect(s, tok)
            save_settings(s, tok)
            self.error = ""
            self.user_off = None
            return msg
        except Exception as exc:
            self.error = str(exc).split("\n")[0]
            raise
        finally:
            self.busy = False

    def disconnect(self, by_user: bool = True):
        if by_user:
            self.user_off = date.today()
        self.feed.close()


# ====================================================================== sync log
class SyncLog:
    def __init__(self, store):
        self.store = store
        try:
            self.last = {d["job"]: d for d in store.find_docs("sync_log")}
        except Exception:
            self.last = {}

    def _record(self, job: str, ok: bool, msg: str, extra: dict | None = None):
        doc = {"job": job, "at": datetime.now().astimezone(), "ok": ok, "message": msg, **(extra or {})}
        self.last[job] = doc
        try:
            self.store.upsert_docs("sync_log", [doc])
        except Exception as exc:
            log.warning("sync_log not written: %s", exc)

    def age(self, job: str) -> float | None:
        d = self.last.get(job)
        if not d or not d.get("at"):
            return None
        at = pd.Timestamp(d["at"])
        at = at if at.tzinfo else at.tz_localize("UTC")
        return (pd.Timestamp.now(tz="UTC") - at).total_seconds()

    def ran_today(self, job: str) -> bool:
        d = self.last.get(job)
        if not d or not d.get("at"):
            return False
        at = pd.Timestamp(d["at"])
        return (at if at.tzinfo else at.tz_localize("UTC")).tz_convert(config.IST).date() == now_ist().date()


# ====================================================================== engine
class Engine:
    def __init__(self, home: str):
        self.home = home
        store = SqliteStore(f"{home}/nse_quant.db")
        self.svc = DataService(store)
        self.svc.persistent = True
        self.broker = Broker()
        self.market = MarketData(self.svc, self.broker)
        self.options = OptionsService(self.svc, self.broker, self.market)
        self.trades_svc = TradesService(self.svc, self.broker, self.options)
        self.bnf_svc = BankNiftyService(self.svc, self.broker, self.market)
        self.news_svc = NewsService(self.svc)
        self.watch = WatchService(self.svc, self.options, self.market)
        self.volume = VolumeWatch(self.svc, self.broker, self.market)
        self.summary = MarketSummary(self.svc.store)
        self.sync_svc = SyncLog(self.svc.store)
        # the attributes api_routes reads (named as on the desktop window)
        self.risk_view = SimpleNamespace(results={})
        self.trades = SimpleNamespace(data={})
        self.fii_view = SimpleNamespace(res=None, zone_res={})
        self._last_snap: dict | None = None
        self._snap_at = 0.0
        self._ticker_state = ({}, {})
        self._risk_at = self._bnf_at = self._news_at = self._global_at = 0.0
        self._setup_active = None
        self._account = None
        self.routes = ApiRoutes(self, self.need)
        self.pool = ThreadPoolExecutor(max_workers=5, thread_name_prefix="engine")
        self.busy: set[str] = set()
        self.last_run: dict[str, float] = {}
        self.events: list[dict] = []                 # status lines for the UI
        self._lvl_slot = self._call_slot = None
        self._stop = threading.Event()
        self.started = False

    # ---------------------------------------------------------------- plumbing
    def status(self, msg: str):
        log.info(msg)
        self.events.append({"at": datetime.now().astimezone().isoformat(), "text": msg})
        del self.events[:-50]

    def run(self, key: str, fn, done=None, every: float = 0, force: bool = False):
        """Run `fn` on the pool unless it is already running or ran less than `every` s ago."""
        if key in self.busy or (not force and time.time() - self.last_run.get(key, 0) < every):
            return
        self.busy.add(key)
        self.last_run[key] = time.time()

        def job():
            try:
                out = fn()
                if done:
                    done(out)
            except Exception as exc:
                log.warning("%s failed: %s", key, exc)
                log.debug(traceback.format_exc())
            finally:
                self.busy.discard(key)

        self.pool.submit(job)

    def need(self, what: str):
        """A screen asked for fresh data."""
        {"banknifty": lambda: self.load_banknifty(force=True), "risk": lambda: self.load_risk(force=True),
         "trades": lambda: self.sync_trades(pull=self.broker.live), "global": lambda: self.load_global(force=True)
         }.get(what, lambda: None)()

    def start(self):
        if self.started:
            return
        self.started = True
        self.status("Engine started")
        threading.Thread(target=self._loop, name="engine-loop", daemon=True).start()

    def _loop(self):
        t0 = time.time()
        self.sync_trades(pull=False)
        self.load_fii()
        while not self._stop.is_set():
            try:
                self.tick(time.time() - t0)
            except Exception as exc:
                log.warning("tick failed: %s", exc)
            time.sleep(1)

    # ---------------------------------------------------------------- the schedule
    def tick(self, up: float):
        t = now_ist()
        open_, sess = market_open(t), in_session(t)
        self.session_tick(sess)
        if not self.broker.healthy:
            self.run("snapshot", self.svc.live_snapshot, self._snap_done, every=15)
        self.update_ticker()
        if up > 5:
            self.load_global()
        if up > 8:
            self.load_news()
        if up > 15:
            self.ensure_daily()
            self.fii_window(t)
        if up > 20:
            self.run("chains", self._chains, every=300 if open_ else 1800)
            self.refresh_levels(every=300)
            self.check_chart(every=300)
        if up > 30:
            self.load_risk(every=180 if open_ else 1800)
        if up > 40 and t.minute % 5 == 0 and self.volume.due(t):
            self.build_volume()
        if up > 45:
            slot = self.summary.due(t)
            if slot:
                self.build_summary(slot)
        if open_:
            self.load_banknifty(every=300)
            if self.broker.live:
                self.sync_trades(pull=True, every=30)
                self.run("intraday", self._intraday, every=300)
        if open_ and t.minute % 5 == 0 and t.second >= 3 and (t.hour, t.minute) >= (9, 20):
            slot = f"{t:%Y-%m-%d %H:%M}"
            if self._lvl_slot != slot:
                self._lvl_slot = slot
                self.check_levels(t)
        if open_ and t.minute % 15 == 0 and t.second >= 8 and (9, 30) <= (t.hour, t.minute) <= (15, 15):
            slot = f"{t:%Y-%m-%d %H:%M}"
            if self._call_slot != slot:
                self._call_slot = slot
                self.make_calls(t.replace(second=0, microsecond=0, tzinfo=None))
        self.preopen_cue(t)

    # ---------------------------------------------------------------- Shoonya window
    def session_tick(self, sess: bool):
        b = self.broker
        code = b.pending_code()                               # the browser just handed back a sign-in code
        if code and not b.busy:
            b.cancel_signin()
            if not sess:
                b.manual_after_hours = True                   # signed in by hand after hours: leave it on
            self.run("connect", lambda: b.connect_with(code=code), lambda m: self._connected(m), force=True)
            return
        if time.time() - self.last_run.get("session", 0) < 30:
            return
        self.last_run["session"] = time.time()
        if sess:
            b.closing = False
            b.manual_after_hours = False
            if not (b.busy or b.feed.live or b.feed.logged_in) and b.user_off != date.today() and b.token_today():
                tok = b.token_today()
                self.run("connect", lambda: b.connect_with(token=tok), lambda m: self._connected(m), every=120)
        else:
            if (b.feed.live or b.feed.logged_in) and not b.manual_after_hours and not b.closing:
                b.closing = True
                self.status("Market-hours window over — storing closing candles, then closing Shoonya")
                self.run("intraday", lambda: self._intraday(full=True), lambda _o: b.disconnect(by_user=False),
                         force=True)

    def _connected(self, msg: str):
        self.status(f"Shoonya: {msg} — real-time data on")
        self.sync_trades(pull=True, force=True)

    def _intraday(self, full: bool = False) -> int:
        n = 0
        for s in ("^NSEI", "NIFTY BANK", "INDIA VIX"):
            if self.market.feed.key_for(s) or s == "INDIA VIX":
                self.market.refresh_intraday(s, "1m")
                n += 1
                if full:
                    for tf in ("15m", "1H"):
                        self.market.refresh_intraday(s, tf)
        self.sync_svc._record("intraday", True, f"{n} instruments")
        return n

    # ---------------------------------------------------------------- prices
    def _snap_done(self, snap: dict):
        if snap.get("quotes"):
            self._last_snap = snap
            self._snap_at = time.time()

    def update_ticker(self):
        feed, quotes = self.broker.feed, {}
        snap = (self._last_snap or {}).get("quotes") or {}
        for sym, key in (("NIFTY BANK", INDEX_TOKENS["NIFTY BANK"]), ("^NSEI", INDEX_TOKENS["^NSEI"]),
                         ("INDIA VIX", VIX_TOKEN)):
            tk = feed.tick(key) if feed.live else None
            if tk and feed._fresh(tk):
                quotes[sym] = {"last": _num(tk.get("lp")), "prev": _num(tk.get("c")) or None,
                               "high": _num(tk.get("h")) or None, "low": _num(tk.get("l")) or None}
            elif sym == "INDIA VIX":
                v = (self._last_snap or {}).get("vix") or {}
                if v.get("last"):
                    quotes[sym] = {"last": v["last"], "prev": v.get("prev") or
                                   (v["last"] - v["change"] if v.get("change") is not None else None)}
            elif snap.get(sym, {}).get("last"):
                q = snap[sym]
                quotes[sym] = {"last": q["last"], "prev": q.get("prev"), "high": q.get("high"), "low": q.get("low")}
        extra = {"source": "Shoonya real-time" if feed.live and self.broker.healthy else
                 ("NSE snapshot (≈15 s)" if snap else "waiting for prices")}
        td = self.trades.data or {}
        if td.get("positions"):
            extra["book_pnl"] = (td.get("today") or {}).get("open")
        res = self.risk_view.results.values()
        extra["setup"] = self._setup_active
        extra["red"] = sum(1 for r in res for a in r["alerts"] if a["level"] == "RED")
        extra["amber"] = sum(1 for r in res for a in r["alerts"] if a["level"] == "AMBER")
        today = f"{now_ist():%Y-%m-%d}"
        extra["direction"] = {u: c["dir"] for u, c in self.watch.calls.items() if c.get("day") == today}
        extra["global_read"] = self.watch.cue.get("read")
        self._ticker_state = (quotes, extra)

    # ---------------------------------------------------------------- daily data
    def ensure_daily(self):
        """First run: back-fill daily history; then the EOD sync once a trading day after 6:30 pm."""
        t = now_ist()
        age = self.sync_svc.age("eod")
        done_today = self.sync_svc.ran_today("eod") and (self.sync_svc.last.get("eod") or {}).get("after_close")
        due = (t.weekday() < 5 and t.time() >= EOD_AFTER and not done_today) or age is None or age > 26 * 3600
        if due:
            after = t.time() >= EOD_AFTER

            def job():
                self.status("Syncing daily history (NSE archive, FII/DII, macro)…")
                return self.svc.sync(symbols=PHONE_INDICES, progress=lambda m: None)

            def done(summary):
                errs = summary.get("errors") or []
                self.sync_svc._record("eod", len(errs) < 5, f"{summary.get('index_archive_days', 0)} archive days",
                                      {"after_close": after})
                self.status(f"Daily sync done · {len(errs)} warning(s)")
                self.compute_levels(force=True)
                self.load_fii()

            self.run("eod", job, done, every=3600)
        elif not self.svc.latest_levels(PHONE_INDICES):
            self.compute_levels()

    def compute_levels(self, force: bool = False):
        svc = self.svc

        def job():
            have = {} if force else svc.latest_levels(PHONE_INDICES)
            fii, macro = svc.load_fii_dii(), svc.load_macro()
            for sym in PHONE_INDICES:
                if sym in have:
                    continue
                daily = svc.load_ohlcv(sym, "1D")
                if len(daily) > 60:
                    svc.save_sr_levels(to_sr_document(analyze(sym, "1D", daily, daily, fii, macro)))

        self.run("sr_levels", job, every=600, force=force)

    # ---------------------------------------------------------------- FII / DII
    def fii_window(self, t):
        d = self.sync_svc.last.get("fii_dii") or {}
        complete = str(d.get("latest_cash")) == str(t.date()) and str(d.get("latest_oi")) == str(t.date())
        startup = "fii_sync" not in self.last_run
        if startup or (t.weekday() < 5 and FII_WINDOW[0] <= t.time() <= FII_WINDOW[1] and not complete):
            def done(out):
                self.sync_svc._record("fii_dii", bool(out.get("latest_cash")), "FII/DII sync",
                                      {"latest_cash": str(out.get("latest_cash")), "latest_oi": str(out.get("latest_oi"))})
                self.load_fii()

            self.run("fii_sync", self.svc.sync_fii_dii, done, every=15 * 60)

    def load_fii(self):
        svc, market, options = self.svc, self.market, self.options

        def job():
            res = fii_analysis.analyse(svc.load_fii_dii(), svc.load_ohlcv("^NSEI", "1D"),
                                       svc.load_ohlcv("NIFTY BANK", "1D"))
            s = res.get("setup") if res.get("available") else None
            res["setup_chains"] = []
            if s:
                day = pd.Timestamp(s["date"]).date()
                for und in ("BANKNIFTY", "NIFTY"):
                    chain = None
                    live = options.fresh(und, 8 * 3600)
                    if live and day == datetime.now().date():
                        chain = live
                    else:
                        stored = market.last_chain(und)
                        if stored and stored[2][:6] == f"{day:%d-%b}":
                            try:
                                chain = analyze_chain(und, stored[0], stored[1])
                            except Exception:
                                chain = None
                    conf = fii_analysis.chain_confirmation(s["direction"] or s["lean"], chain)
                    if conf:
                        res["setup_chains"].append(conf)
            return res

        def done(res):
            self.fii_view.res = res
            self._setup_active = None
            if not res.get("available"):
                return
            s = res.get("setup")
            if not s or not s["direction"]:
                return
            age = (datetime.now().date() - pd.Timestamp(s["date"]).date()).days
            if age > 4:
                return
            self._setup_active = s["direction"]
            key = f"{pd.Timestamp(s['date']):%Y-%m-%d}:{s['direction']}"
            if (self.sync_svc.last.get("setup_alert") or {}).get("key") == key:
                return
            g = (s.get("record") or {}).get(s["direction"]) or {}
            conf = sum(1 for c in res.get("setup_chains") or [] if c["confirmed"])
            side = "calls" if s["direction"] == "up" else "puts"
            msg = (f"{'▲ UP' if s['direction'] == 'up' else '▼ DOWN'} squeeze-loading setup for "
                   f"{pd.Timestamp(s['date']):%d %b} close"
                   + (f" · chain confirms on {conf}/2" if res.get("setup_chains") else "")
                   + (f" · BANKNIFTY up next session {g['up']:.0f}% of {g['n']} past cases" if g.get("n") else "")
                   + f" · short {side} are the exposed side")
            self.sync_svc._record("setup_alert", True, msg, {"key": key})
            self.status("⚑ " + msg)

        self.run("fii", job, done, force=True)

    # ---------------------------------------------------------------- chains, BANKNIFTY, trades
    def _chains(self):
        out = {}
        for und in ("NIFTY", "BANKNIFTY"):
            try:
                res = self.options.analyse(und, None)
                out[und] = res
            except Exception as exc:
                log.warning("chain %s failed: %s", und, exc)
        self.fii_view.zone_res.update(out)
        return out

    def load_banknifty(self, force: bool = False, every: float = 120):
        def done(_out):
            self._bnf_at = time.time()
        self.run("bnf", lambda: self.bnf_svc.refresh(), done, every=every, force=force)

    def sync_trades(self, pull: bool, force: bool = False, every: float = 0):
        def done(d):
            self.trades.data = d
        self.run("trades" + ("_pull" if pull else ""), lambda: self.trades_svc.refresh(pull, 90), done,
                 every=every, force=force)

    # ---------------------------------------------------------------- news & global
    def load_news(self, force: bool = False):
        news = self.news_svc

        def job():
            new = news.poll()
            alerts = news.select_alerts(new)
            if alerts:
                at = datetime.now().astimezone()
                news.store.upsert_docs("news", [{"id": d["id"], "alerted": True, "alerted_at": at,
                                                 "related": d.get("related", 0)} for d in alerts])
            return alerts

        self.run("news", job, every=120 if news_window() else 600, force=force)

    def load_global(self, force: bool = False):
        self.run("global", self.watch.poll_global, every=300, force=force)

    def preopen_cue(self, t):
        if t.weekday() >= 5 or not ((8, 30) <= (t.hour, t.minute) < (9, 15)) or not self.watch.quotes:
            return
        day = f"{t:%Y-%m-%d}"
        if (self.sync_svc.last.get("preopen_cue") or {}).get("day") == day:
            return
        c = self.watch.cue
        msg = f"{c['read']} ({c['score']:+d}) · " + "; ".join(c.get("drivers", [])[:4]) + f" · {c['writer']}"
        self.sync_svc._record("preopen_cue", True, msg, {"day": day})
        self.status("🌐 Before the open: " + msg)

    # ---------------------------------------------------------------- levels & direction calls
    def refresh_levels(self, every: float = 0):
        quotes = self._ticker_state[0] or {}
        spots = {u: (quotes.get(s) or {}).get("last") for u, s in (("NIFTY", "^NSEI"), ("BANKNIFTY", "NIFTY BANK"))}

        def job():
            for und, sp in spots.items():
                try:
                    self.watch.refresh_levels(und, sp)
                except Exception as exc:
                    log.warning("levels %s failed: %s", und, exc)

        self.run("levels", job, every=every)

    def check_levels(self, t):
        quotes = self._ticker_state[0] or {}
        if not self.broker.healthy and time.time() - self._snap_at > 150:
            self.refresh_levels()
            return
        for und, sym in (("NIFTY", "^NSEI"), ("BANKNIFTY", "NIFTY BANK")):
            q = quotes.get(sym) or {}
            if not q.get("last"):
                continue
            w = self.watch.watcher
            first = und not in w.prev or w.day != t.date()
            prev = q.get("prev") if (t.hour, t.minute) <= (9, 20) else None
            for a in self.watch.check_levels(und, q["last"], prev if first else None):
                self.status("📉 " + a["title"])
        self.refresh_levels()
        self.check_chart()

    def build_volume(self):
        """After 3:35 pm on trading days: the unusual-volume summary for NIFTY 50 / BANKNIFTY stocks."""
        today = now_ist().date()

        def job():
            out = self.volume.build(day=today)
            if out["checked"]:
                for a in self.watch._fresh_ids([volume_watch.alert(out)]):
                    self.status("📊 " + a["title"])

        self.run("volume", job, every=600)

    def build_summary(self, slot: str):
        """8 am and 9 pm on trading days: the market summary (one notification each)."""
        def job():
            s = self.summary.run(self.routes, slot)
            if s["session"]:
                for a in self.watch._fresh_ids([market_summary.alert(s)]):
                    self.status(("☀️ " if slot == "morning" else "🌙 ") + a["title"])

        self.run("summary", job, every=60)

    def check_chart(self, every: float = 0):
        """Chart signals (pivots, CPR, gaps, MAs, Savitzky) — alerts reach the phone via /api/alerts."""
        def job():
            for und in ("NIFTY", "BANKNIFTY"):
                try:
                    for a in self.watch.check_chart(und):
                        self.status("📈 " + a["title"])
                except Exception as exc:
                    log.warning("chart signals %s failed: %s", und, exc)

        self.run("chart", job, every=every)

    def make_calls(self, slot=None, manual: bool = False):
        slot = slot or now_ist().replace(second=0, microsecond=0, tzinfo=None)
        risk = {u: dict(r) for u, r in self.risk_view.results.items()}

        def job():
            out = {}
            for und in ("NIFTY", "BANKNIFTY"):
                try:
                    out[und] = self.watch.make_call(und, slot, risk.get(und))
                except Exception as exc:
                    out[und] = {"error": str(exc)}
            return out

        def done(out):
            ok = {u: c for u, c in out.items() if "error" not in c}
            if ok:
                self.status(f"➚ Direction {slot:%H:%M} · " + " · ".join(f"{u} {c['dir']}" for u, c in ok.items()))

        self.run("calls", job, done, force=True)

    # ---------------------------------------------------------------- writer risk + book risk
    def load_risk(self, force: bool = False, every: float = 180):
        opts, svc, trades = self.options, self.svc, self.trades_svc
        td = self.trades.data or {}
        limits, today = dict(td.get("limits") or {}), dict(td.get("today") or {})
        orders = list(td.get("orders") or [])
        capital = config.TRADING_CAPITAL or _num(limits.get("margin_used")) + _num(limits.get("available"))

        def job():
            fii = svc.load_fii_dii()
            frame = fii_analysis.build_frame(fii, svc.load_ohlcv("^NSEI", "1D")) if len(fii) else None
            book = trades.last_book.get("positions")
            if book is None:
                docs = svc.store.find_docs("broker_positions", sort="date", desc=True, limit=1)
                book = docs[0].get("rows") if docs else []
            out, wctx = {}, {}
            for und, sym in (("NIFTY", "^NSEI"), ("BANKNIFTY", "NIFTY BANK")):
                try:
                    res = opts.fresh(und, 45) or opts.analyse(und)
                    hist = opts.intraday_history(und)
                    prev = risk_engine.pick_previous(hist, res["expiry"])
                    c = svc.store.load_ohlcv(sym, "1m", start=pd.Timestamp.now(tz=config.IST) - pd.Timedelta(minutes=90))
                    path = list(zip(c.index, c["close"])) if c is not None and len(c) else []
                    if path and market_open():
                        path.append((pd.Timestamp.now(tz=config.IST), res["spot"]))
                    groups = te.manage_positions(book or [], {und: res["spot"]}, {und: res.get("sigma_move")})
                    legs = [l for g in groups if g["und"] == und for l in g["legs"]]
                    chains = {}
                    for e in {pd.Timestamp(l["expiry"]).strftime("%d-%b-%Y") for l in legs
                              if l.get("expiry") is not None and l.get("qty")} - {res["expiry"]}:
                        try:
                            chains[e] = opts.analyse(und, e, record=False)
                        except Exception as exc:
                            log.warning("risk: %s %s chain failed: %s", und, e, exc)
                    r = risk_engine.assess(res, prev, path, frame, fii, legs, chains)
                    books = []
                    for g in groups:
                        if g["und"] == und and any(l.get("qty") for l in g["legs"]):
                            b = book_risk.assess_group(g, {res["expiry"]: res, **chains}, res.get("lot") or 1)
                            if b:
                                books.append(b)
                    r["books"] = books
                    r["alerts"] += [a for b in books for a in book_risk.book_alerts(b, capital)]
                    out[und] = r
                    self.fii_view.zone_res[und] = res
                    wctx[und] = {"res": res, "chains": {res["expiry"]: res, **chains},
                                 "groups": [g for g in groups if g["und"] == und], "books": books}
                except Exception as exc:
                    log.warning("risk %s failed: %s", und, exc)
            try:
                for a in self.watch.check_writer(wctx, self.broker.feed, orders):
                    self.status(f"Writer signal: {a['title']}")
            except Exception as exc:
                log.warning("writer signals failed: %s", exc)
            return out

        def done(out):
            self._risk_at = time.time()
            self.risk_view.results.update(out)
            books = [b for r in out.values() for b in r.get("books", [])]
            self._account = book_risk.account_check(today, limits, books) if (limits or books) else None

        self.run("risk", job, done, every=every, force=force)


# ====================================================================== Shoonya actions for the UI
def _totp_file():
    return config.APP_HOME / "shoonya_totp.json"


def _totp_key() -> str | None:
    import json
    try:
        return json.loads(_totp_file().read_text()).get("key") or None
    except (OSError, ValueError):
        return None


def shoonya_settings_view() -> dict:
    s = load_settings()
    tok = s.get("access_token") and s.get("token_date") == date.today().isoformat()
    key = _totp_key()
    now = totp_now(key) if key else None
    return {"user_id": s.get("user_id", ""), "client_id": s.get("client_id", ""),
            "has_secret": bool(s.get("secret")), "signed_in_today": bool(tok), "redirect_url": REDIRECT_URL,
            "has_totp": bool(key), "totp": now[0] if now else None, "totp_left": now[1] if now else None}


def shoonya_save(values: dict):
    """Save the API details (the secret only when a new one is typed). Keeps today's token."""
    import json
    import os
    s = load_settings()
    for k in ("user_id", "client_id"):
        if values.get(k) is not None:
            s[k] = values[k].strip()
    if values.get("secret"):
        s["secret"] = values["secret"].strip()
    token = s.get("access_token") if s.get("token_date") == date.today().isoformat() else None
    save_settings(s, token)
    if values.get("totp_key") is not None:                  # optional: shows the TOTP code while signing in
        _totp_file().write_text(json.dumps({"key": values["totp_key"].replace(" ", "").upper()}))
        os.chmod(_totp_file(), 0o600)


def code_from(text: str) -> str | None:
    return code_from_text(text)
