"""Trades & P&L data pipeline, independent of any screen.

`refresh(pull, days)` optionally pulls today's books from Shoonya (TradeBook,
OrderBook, PositionBook, Limits — read-only) into MongoDB, then rebuilds the
whole journal from the stored fills: round trips, strategies, metrics,
breakdowns, the "why I lost" diagnosis and the open-position management plan.
Blocking; call from a worker thread.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import pandas as pd

import config
import trades_engine as te
from options import UNDERLYINGS
from shoonya_feed import INDEX_TOKENS

log = logging.getLogger(__name__)

SETTLE_SYMBOL = {"NIFTY": "^NSEI", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FINANCIAL SERVICES",
                 "MIDCPNIFTY": "NIFTY MIDCAP SELECT"}


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class TradesService:
    def __init__(self, svc, broker, options):
        self.svc, self.broker, self.options = svc, broker, options
        self.last_book: dict = {}
        self._ctx_cache: dict[str, dict] = {}

    @property
    def store(self):
        return self.svc.store

    # ---------------------------------------------------------------- context
    def fill_ctx(self, f: dict) -> dict:
        """Market read stamped on a newly seen fill: the latest option-chain analysis
        for its underlying (computed now if older than 15 min)."""
        und = f["und"]
        if und not in UNDERLYINGS:
            return {}
        res = self.options.fresh(und)
        if res is None:
            exp = pd.Timestamp(f["expiry"]).strftime("%d-%b-%Y") if f.get("expiry") is not None else None
            try:
                res = self.options.analyse(und, exp)
            except Exception as exc:
                log.warning("Could not analyse %s for fill context: %s", und, exc)
                return {}
        return te.ctx_from_analysis(res)

    # ---------------------------------------------------------------- pipeline
    def refresh(self, pull: bool, days) -> dict:
        svc, feed, store = self.svc, self.broker.feed, self.store
        pull = pull and feed.live
        status = []
        if pull:
            tb, ob, pb, lim = feed.trade_book(), feed.order_book(), feed.position_book(), feed.limits()
            fills = [f for f in (te.fill_from_shoonya(r) for r in tb) if f]
            known = {d["fill_id"] for d in store.find_docs("broker_fills")}
            for f in fills:
                if f["fill_id"] not in known:
                    f["ctx"] = self.fill_ctx(f)
            store.upsert_docs("broker_fills", fills, insert_only=("ctx",))
            today = datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc)
            store.upsert_docs("broker_orders", [{**o, "order_no": str(o.get("norenordno", "")), "date": today}
                                                for o in ob if o.get("norenordno")])
            lim_d = self.limits(lim)
            store.upsert_docs("pnl_daily", [{"date": today, "realised": sum(_num(r.get("rpnl")) for r in pb),
                                             "mtm": sum(_num(r.get("urmtom")) for r in pb), **lim_d}])
            store.upsert_docs("broker_positions", [{"date": today, "rows": pb}])      # offline copy of the book
            feed.watch(f"{r.get('exch')}|{r.get('token')}" for r in pb)
            self.last_book = {"positions": pb, "orders": ob, "limits": lim_d, "at": datetime.now()}
            status.append(f"Synced with Shoonya at {datetime.now():%H:%M:%S} · {len(fills)} fills, "
                          f"{len(ob)} orders, {len(pb)} positions today")
            self.carry_forward(pb)
        elif self.last_book:
            status.append(f"Last Shoonya sync {self.last_book['at']:%H:%M:%S}")
        else:
            stored = store.find_docs("broker_positions", sort="date", desc=True, limit=1)
            if stored:
                d = pd.Timestamp(stored[0]["date"])
                self.last_book = {"positions": stored[0].get("rows") or [], "orders": None, "limits": {},
                                  "at": d.to_pydatetime(), "offline": True}
                status.append(f"Offline — positions as last synced on {d:%d-%b}; connect Shoonya for live P&L")
            else:
                status.append("Showing stored trades — connect Shoonya to pull today's book")

        all_fills = store.find_docs("broker_fills", sort="time")
        macro = svc.load_macro()
        vix = macro["indiavix"] if "indiavix" in macro else None
        dailies: dict[str, pd.DataFrame] = {}

        def daily(und):
            sym = SETTLE_SYMBOL.get(und)
            if sym and sym not in dailies:
                dailies[sym] = svc.load_ohlcv(sym, "1D")
            return dailies.get(sym)

        for f in all_fills:                                  # older / imported fills: reconstruct context
            if not f.get("ctx"):
                if f["fill_id"] not in self._ctx_cache:
                    self._ctx_cache[f["fill_id"]] = te.reconstruct_ctx(f["und"], f["time"], daily(f["und"]), vix,
                                                                         f.get("expiry"))
                f["ctx"] = self._ctx_cache[f["fill_id"]]

        def settle(und, d):
            df = daily(und)
            if df is None or df.empty:
                return None
            idx = df.index.tz_convert(config.IST).date if df.index.tz is not None else df.index.date
            hit = df["close"][idx <= d]          # expiry close (or the last session before a holiday)
            if not len(hit) or (d - idx[idx <= d][-1]).days > 4:
                return None
            return float(hit.iloc[-1])

        closed, open_legs = te.round_trips(all_fills, settle)
        strats = te.strategies(closed, open_legs)
        if days is not None:
            now = pd.Timestamp.now(tz=config.IST)
            cut = (now.normalize() if days == 0 else
                   pd.Timestamp(now.year, 1, 1, tz=config.IST) if days == -1 else now - pd.Timedelta(days=days))
            strats = [s for s in strats if s["status"] == "open" or (s["exit_time"] or s["entry_time"]) >= cut]
            fills_shown = [f for f in all_fills if pd.Timestamp(f["time"]).tz_convert(config.IST) >= cut]
        else:
            fills_shown = all_fills
        book = self.last_book
        lim = book.get("limits") or {}
        capital = config.TRADING_CAPITAL or lim.get("available_total") or None
        spot, sigma = {}, {}
        for und in ("NIFTY", "BANKNIFTY"):
            t = feed.tick(INDEX_TOKENS["^NSEI" if und == "NIFTY" else "NIFTY BANK"]) if feed.live else None
            res = self.options.fresh(und, max_age=86400)
            if t and t.get("lp"):
                spot[und] = float(t["lp"])
            elif res:
                spot[und] = res.get("spot")
            if res:
                sigma[und] = res.get("sigma_move")
        positions = te.manage_positions(book.get("positions") or [], spot, sigma)
        orders = book.get("orders")
        if orders is None:
            today = datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc)
            orders = [o for o in store.find_docs("broker_orders") if pd.Timestamp(o.get("date")) == pd.Timestamp(today)]
        return {
            "status": " · ".join(status), "positions": positions, "orders": orders,
            "fills": list(reversed(fills_shown)), "strategies": strats, "metrics": te.metrics(strats),
            "breakdowns": te.breakdowns(strats), "findings": te.diagnose(strats, capital),
            "equity": te.equity_curve(strats), "limits": lim,
            "today": {"open": sum(g["open_pnl"] for g in positions), "total": sum(g["mtm"] for g in positions)},
            "journal": {d["strategy_id"]: d for d in store.find_docs("trade_journal")},
            "explain": te._RULES,
        }

    def carry_forward(self, pb: list[dict]):
        """Positions carried from earlier sessions have no fills in today's
        TradeBook.  Record a 'carry-forward' opening fill at Shoonya's average
        price for any carried quantity the stored fills don't explain, so the
        position shows up in trade history when it is closed."""
        start = pd.Timestamp.now(tz=config.IST).normalize()
        stored = self.store.find_docs("broker_fills")
        new = []
        for r in pb:
            cf = int(_num(r.get("cfbuyqty"))) - int(_num(r.get("cfsellqty")))
            if not cf:
                continue
            tsym = r.get("tsym", "")
            before = sum((f["qty"] if f["side"] == "B" else -f["qty"]) for f in stored
                         if f["tsym"] == tsym and pd.Timestamp(f["time"]).tz_convert(config.IST) < start)
            diff = cf - before
            if not diff:
                continue
            side = "B" if diff > 0 else "S"
            px = _num(r.get("cfbuyavgprc") if side == "B" else r.get("cfsellavgprc")) or _num(r.get("netavgprc"))
            new.append({"fill_id": f"cf-{tsym}-{start.date()}", "order_no": "", "tsym": tsym,
                        "time": (start + pd.Timedelta(hours=9)).to_pydatetime(), "exch": r.get("exch", ""),
                        "token": str(r.get("token", "")), "side": side, "qty": abs(diff), "price": px,
                        "lot": int(_num(r.get("ls")) or 1), "product": r.get("prd", ""), "source": "carry-forward",
                        "ctx": {"source": "carry-forward", "first_seen": str(start.date())},
                        **te._parsed(tsym)})
        if new:
            self.store.upsert_docs("broker_fills", new, insert_only=("ctx",))

    @staticmethod
    def limits(lim: dict) -> dict:
        """Shoonya Limits -> margin used / available (approximate: cash + pay-in +
        collateral − margin used)."""
        if not lim:
            return {}
        used = _num(lim.get("marginused"))
        total = sum(_num(lim.get(k)) for k in ("cash", "payin", "brkcollamt", "collateral", "unclearedcash"))
        return {"margin_used": used, "available_total": total, "available": total - used,
                "cash": _num(lim.get("cash")), "premium": _num(lim.get("premium"))}

    def import_csv(self, path: str) -> tuple[int, int]:
        fills = te.fills_from_csv(pd.read_csv(path))
        return len(fills), self.store.upsert_docs("broker_fills", fills, insert_only=("ctx",))
