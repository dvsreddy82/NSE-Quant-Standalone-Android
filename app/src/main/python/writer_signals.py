"""Decision signals for an option writer, on top of the risk monitor and book risk:
when to enter, when to book profit, when to adjust and when to get out.

  1  PROFIT TARGET          a book has captured PROFIT_TAKE_PCT / PROFIT_TAKE_HIGH_PCT of its credit
  2  SHORT LEG DELTA        a short leg's delta passes SHORT_DELTA_WARN / SHORT_DELTA_EXIT
  3  VOLATILITY SPIKE       India VIX up VIX_SPIKE_PCT on the day, or ATM IV up ATM_IV_SPIKE_PTS since the open
  4  EXPIRY-DAY GAMMA       expiry day after 13:00 with spot within EXPIRY_GAMMA_PCT of a short strike
  5  INSIDE EXPECTED MOVE   a short strike nearer than the 1σ move to its expiry
  6  FUTURES BUILDUP        near-month future: long / short buildup, short covering, long unwinding
  7  WRITING CONDITIONS     once a day: IV rank (VIX percentile), IV vs realised vol, the chain's verdict
  8  OI WALL SHIFT          the biggest call / put OI strike moves (confirmed on two checks)
  9  PCR EXTREME / SKEW     PCR past PCR_HIGH / PCR_LOW on two checks running (with whether the additions look
                           like writing or buying); put skew up SKEW_RISE_PTS since the open
  10 MAX PAIN PULL          last two days to expiry: where expiry tends to pin
  11 TIME-BASED EXIT        TIME_EXIT_DTE days or less to expiry with a short within TIME_EXIT_PCT
  12 DISCIPLINE             LOSING_STREAK_DAYS losing days in a row; more than MAX_ORDERS_PER_DAY orders today

`WriterWatch.check` takes what the app already has (analysed chains, position groups from
trades_engine.manage_positions, book_risk books, the VIX and futures ticks, daily P&L and today's
orders) and returns alerts with ids that are stable for the day, so each fires once.  The
morning baselines (ATM IV, skew, OI walls) are kept in the store so a restart keeps them.

Analytics to support decisions, not investment advice.
"""
from __future__ import annotations

import hashlib
import math
from datetime import date, datetime

import pandas as pd

import config
from book_risk import _leg_iv
from options import bs_greeks, years_to_expiry

KIND = "WRITER"


def _hid(*parts) -> str:
    return "wrt:" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def _f(v) -> float | None:
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def _exp_str(e) -> str:
    return pd.Timestamp(e).strftime("%d-%b-%Y")


def _rs(v: float) -> str:
    return f"₹{v:,.0f}" if v >= 0 else f"−₹{-v:,.0f}"


class WriterWatch:
    def __init__(self, store=None):
        self.store = store
        self._base: dict[tuple[str, str], dict] = {}        # (day, und) -> morning baselines
        self._wall_seen: dict[tuple[str, str], tuple[float, int]] = {}   # (und, side) -> (candidate, times seen)
        self._pcr_seen: dict[str, str | None] = {}          # und -> PCR extreme side seen on the last check

    # ---------------------------------------------------------------- baselines
    def _baseline(self, day: str, und: str, res: dict) -> dict:
        k = (day, und)
        if k not in self._base:
            doc = None
            if self.store is not None:
                try:
                    hit = self.store.find_docs("writer_state", {"day": day, "und": und}, limit=1)
                    doc = hit[0] if hit else None
                except Exception:
                    doc = None
            if not doc:
                doc = {"day": day, "und": und, "atm_iv": res.get("atm_iv"), "skew": res.get("skew"),
                       "call_wall": (res.get("call_walls") or [{}])[0].get("strike"),
                       "put_wall": (res.get("put_walls") or [{}])[0].get("strike")}
                self._save(doc)
            self._base[k] = doc
        b = self._base[k]
        for f in ("atm_iv", "skew"):                     # first usable value of the day
            if b.get(f) is None and res.get(f) is not None:
                b[f] = res[f]
                self._save(b)
        return b

    def _save(self, doc: dict):
        if self.store is None:
            return
        try:
            self.store.upsert_docs("writer_state", [{k: v for k, v in doc.items() if k != "_id"}])
        except Exception:
            pass

    # ---------------------------------------------------------------- main
    def check(self, ctx: dict[str, dict], vix: dict | None = None, futs: dict[str, dict] | None = None,
              pnl_days: list[dict] | None = None, orders: list[dict] | None = None,
              now: datetime | None = None) -> list[dict]:
        """ctx[und] = {"res": chain analysis (nearest expiry), "chains": {expiry: analysis},
        "groups": position groups for und, "books": book_risk books for und}."""
        now = now or pd.Timestamp.now(tz=config.IST).to_pydatetime()
        day = str(now.date())
        hm = (now.hour, now.minute)
        market = now.weekday() < 5 and (9, 15) <= hm <= (15, 30)
        out: list[dict] = []

        def add(sig, level, und, title, text, *key):
            out.append({"id": _hid(sig, day, und, *key), "kind": KIND, "signal": sig, "level": level,
                        "und": und, "title": title, "text": text})

        for und, c in ctx.items():
            res = c.get("res") or {}
            S = _f(res.get("spot"))
            if not S:
                continue
            chains = c.get("chains") or {res.get("expiry"): res}
            groups = c.get("groups") or []
            books = {b["expiry"]: b for b in c.get("books") or []}
            if not market:                    # prices outside the session are stale: nothing to act on
                continue
            self._positions(add, und, S, res, chains, groups, books, now)
            self._market(add, und, S, res, self._baseline(day, und, res), books, now)

        if market:
            self._vix(add, vix)
            for und, t in (futs or {}).items():
                net = sum((b.get("delta") or 0) for b in (ctx.get(und) or {}).get("books") or [])
                self._future(add, und, t, net)
            if hm >= (9, 20):
                self._discipline(add, pnl_days or [], orders or [], now)
        return out

    # ---------------------------------------------------------------- 1, 2, 4, 5, 11: your positions
    def _positions(self, add, und, S, res, chains, groups, books, now):
        today = now.date()
        after_one = (now.hour, now.minute) >= (13, 0)
        for g in groups:
            live = [l for l in g.get("legs") or [] if l.get("qty") and l.get("kind") in ("CE", "PE")]
            if not live or g.get("expiry") is None:
                continue
            exp = _exp_str(g["expiry"])
            exp_d = pd.Timestamp(g["expiry"]).date()
            dte = (exp_d - today).days
            credit, pnl = _f(g.get("credit")) or 0.0, _f(g.get("open_pnl")) or 0.0
            # 1 profit target
            if credit > 0:
                cap = pnl / credit * 100
                left = credit - pnl
                if cap >= config.PROFIT_TAKE_HIGH_PCT:
                    add("profit", "AMBER", und, f"TAKE PROFIT · {und} {exp}: {cap:.0f}% of the credit in hand",
                        f"{_rs(pnl)} of {_rs(credit)} captured with {dte} day(s) left. Holding on risks the {_rs(pnl)} "
                        f"to earn the last {_rs(left)} — closing (or rolling to a fresh position) usually pays better.",
                        exp, "high")
                elif cap >= config.PROFIT_TAKE_PCT:
                    add("profit", "INFO", und, f"Profit target · {und} {exp}: {cap:.0f}% of the credit in hand",
                        f"{_rs(pnl)} of {_rs(credit)} captured with {dte} day(s) left — the plan's {config.PROFIT_TAKE_PCT:.0f}% "
                        "target. Consider booking it; the remaining premium carries most of the risk.", exp, "base")
            T = years_to_expiry(exp, now)
            atm = _f((chains.get(exp) or res).get("atm_iv"))
            atm = atm / 100 if atm else 0.15
            for l in live:
                if l["qty"] >= 0:
                    continue
                K, kind = float(l["strike"]), l["kind"]
                iv = _leg_iv(chains, exp, K, kind, atm) or atm
                d = abs(bs_greeks(S, K, max(T, 1e-6), iv, kind)["delta"])
                name = f"{K:,.0f} {kind}"
                lots = abs(l["qty"]) / (res.get("lot") or 1)
                gap_pct = abs(S - K) / S * 100
                flagged = False
                # 2 short leg delta
                if d >= config.SHORT_DELTA_EXIT:
                    add("delta", "RED", und, f"SHORT {name} DELTA {d:.2f} · {und} {exp}",
                        f"The leg now behaves like {d * lots:.1f} lots of short index — ₹{d * abs(l['qty']) * 100:,.0f} per 100 pts "
                        f"against you. Past {config.SHORT_DELTA_EXIT:.2f}: roll it further out or close it.", exp, K, kind, "exit")
                    flagged = True
                elif d >= config.SHORT_DELTA_WARN:
                    add("delta", "AMBER", und, f"Short {name} delta {d:.2f} · {und} {exp}",
                        f"Past {config.SHORT_DELTA_WARN:.2f} (₹{d * abs(l['qty']) * 100:,.0f} per 100 pts). Spot {S:,.0f} is "
                        f"{abs(S - K):,.0f} pts away — plan the roll now while it is cheap.", exp, K, kind, "warn")
                    flagged = True
                # 4 expiry-day gamma
                if dte == 0 and after_one and gap_pct <= config.EXPIRY_GAMMA_PCT:
                    add("gamma", "RED", und, f"EXPIRY-DAY GAMMA · short {name}",
                        f"Expiry today and spot {S:,.0f} is {gap_pct:.2f}% from your short strike. In the last hours a small "
                        "move swings this leg sharply — close or hedge rather than hope.", exp, K, kind)
                    flagged = True
                # 11 time-based exit
                elif 0 < dte <= config.TIME_EXIT_DTE and gap_pct <= config.TIME_EXIT_PCT:
                    add("time", "AMBER", und, f"Time-based exit · short {name}, {dte} day(s) to expiry",
                        f"Spot {S:,.0f} is {gap_pct:.2f}% from the strike with {dte} day(s) left — gamma rises fast into "
                        "expiry. Roll to the next expiry or close early.", exp, K, kind)
                    flagged = True
                # 5 inside the expected move
                sigma = S * iv * math.sqrt(max(T, 1e-6))
                if not flagged and dte >= 1 and abs(S - K) < sigma:
                    add("emove", "INFO", und, f"Short {name} is inside the expected move · {und} {exp}",
                        f"{abs(S - K):,.0f} pts from spot against a 1σ move of ±{sigma:,.0f} pts to expiry (IV {iv * 100:.1f}%) — "
                        "roughly a 1-in-3 chance of being tested. Keep a roll level ready.", exp, K, kind)

    # ---------------------------------------------------------------- 3 (IV), 7, 8, 9, 10: the chain
    def _market(self, add, und, S, res, base, books, now):
        hm = (now.hour, now.minute)
        iv, iv0 = _f(res.get("atm_iv")), _f(base.get("atm_iv"))
        if hm >= (9, 20) and iv is not None and iv0 is not None:
            rise = iv - iv0
            if rise >= 2 * config.ATM_IV_SPIKE_PTS:
                add("ivspike", "RED", und, f"ATM IV SPIKE · {und} +{rise:.1f} pts",
                    f"ATM IV {iv0:.1f}% → {iv:.1f}% since the open. Short premium loses even without a spot move, "
                    "and a fast IV rise often comes before a big move — cut size, don't add.", "big")
            elif rise >= config.ATM_IV_SPIKE_PTS:
                add("ivspike", "AMBER", und, f"ATM IV up {rise:.1f} pts · {und}",
                    f"ATM IV {iv0:.1f}% → {iv:.1f}% since the open — premiums are swelling; avoid adding shorts until it settles.",
                    "base")
        # 7 writing conditions, once a day after 9:45
        if hm >= (9, 45):
            vp, vrp, hv = res.get("vix_pct"), _f(res.get("vrp")), _f(res.get("hv20"))
            verdict = res.get("verdict") or ""
            parts = []
            if vp is not None:
                parts.append(f"IV rank (VIX percentile, 1 yr) {vp:.0f}" + (" — premium rich" if vp >= 60 else
                                                                          " — premium thin" if vp <= 25 else ""))
            if iv is not None and hv:
                parts.append(f"IV {iv:.1f}% vs realised {hv:.1f}% ({iv - hv:+.1f} pts) — " +
                             ("writers have an edge" if iv - hv > 1 else "no edge: options are cheap" if iv - hv < -1
                              else "fair"))
            if parts:
                head = verdict.split(" — ")[0] if verdict else "see chain"
                lvl = "AMBER" if verdict.startswith("UNFAVOURABLE") else "INFO"
                add("conditions", lvl, und, f"Writing conditions · {und}: {head}",
                    "; ".join(parts) + ". " + (verdict or ""))
        # 8 OI wall shift (two consecutive checks)
        step = _f(res.get("step")) or 50
        for side, key, toward in (("call", "call_wall", -1), ("put", "put_wall", 1)):
            walls = res.get(f"{side}_walls") or []
            ref = _f(base.get(key))
            if not walls or ref is None:
                continue
            cur = float(walls[0]["strike"])
            if abs(cur - ref) < step / 2:
                self._wall_seen.pop((und, side), None)
                continue
            cand, n = self._wall_seen.get((und, side), (None, 0))
            n = n + 1 if cand == cur else 1
            self._wall_seen[(und, side)] = (cur, n)
            if n < 2:
                continue
            narrowing = (cur - ref) * toward > 0
            word = ("Ceiling lowered" if side == "call" else "Floor raised") if narrowing else \
                   ("Ceiling raised" if side == "call" else "Floor lowered")
            note = " Check your short strikes against the new wall." if narrowing and books else ""
            add("wall", "AMBER" if narrowing else "INFO", und, f"OI wall shift · {und}: {word} {ref:,.0f} → {cur:,.0f}",
                f"The biggest {side} OI is now at {cur:,.0f} ({walls[0]['oi']:,.0f} contracts) — writers have moved "
                f"their {'resistance' if side == 'call' else 'support'}, so the market's own range is now "
                f"{'narrower' if narrowing else 'wider'} on that side.{note}", side, cur)
            base[key] = cur                               # the new wall is the reference from here
            self._save(base)
            self._wall_seen.pop((und, side), None)
        # 9 PCR extremes and skew
        pcr = _f(res.get("pcr"))
        side = "high" if pcr is not None and pcr >= config.PCR_HIGH else "low" if pcr is not None and pcr <= config.PCR_LOW else None
        seen = self._pcr_seen.get(und)
        self._pcr_seen[und] = side
        if side and seen == side:                  # extreme on two checks in a row, not one snapshot
            fl = (res.get("pcr_set") or {}).get("flow") or {}
            if side == "high":
                who = ("mostly possible put writing (OI up, premium down)" if fl.get("pe_writing", 0) > fl.get("pe_buying", 0)
                       else "mostly possible put buying (OI up, premium up)" if fl.get("pe_buying") else "writing vs buying unclear")
                add("pcr", "AMBER", und, f"PCR extreme · {und} {pcr:.2f}",
                    f"Put OI is {pcr:.2f}× call OI; today's put additions are {who}. A high PCR is not bullish by "
                    "itself — check spot, futures OI and IV. Put writers: positioning is crowded, don't add near spot.",
                    "high")
            else:
                who = ("mostly possible call writing (OI up, premium down)" if fl.get("ce_writing", 0) > fl.get("ce_buying", 0)
                       else "mostly possible call buying (OI up, premium up)" if fl.get("ce_buying") else "writing vs buying unclear")
                add("pcr", "AMBER", und, f"PCR extreme · {und} {pcr:.2f}",
                    f"Call OI dominates (PCR {pcr:.2f}); today's call additions are {who}. A low PCR is not bearish by "
                    "itself — check spot, futures OI and IV. Call writers: keep strikes wide.", "low")
        sk, sk0 = _f(res.get("skew")), _f(base.get("skew"))
        if hm >= (9, 30) and sk is not None and sk0 is not None and sk - sk0 >= config.SKEW_RISE_PTS:
            add("skew", "AMBER", und, f"Put skew steepening · {und} +{sk - sk0:.1f} pts",
                f"OTM put IV minus call IV {sk0:.1f} → {sk:.1f} pts since the open — traders are paying up for "
                "downside protection, a fear signal that often comes before a fall.")
        # 10 max pain pull
        mp, dte = _f(res.get("max_pain")), _f(res.get("dte"))
        if mp and dte is not None and dte <= 2.5 and hm >= (10, 0):
            gap = (S - mp) / S * 100
            add("maxpain", "INFO", und, f"Max pain {mp:,.0f} · {und} expiry {res.get('expiry')}",
                f"Spot {S:,.0f} is {gap:+.2f}% from max pain with {dte:.1f} day(s) to expiry. With no news, expiry often "
                "drifts toward it — close first the leg that max pain would test.", res.get("expiry"))

    # ---------------------------------------------------------------- 3 India VIX
    def _vix(self, add, t):
        lp, c = _f((t or {}).get("lp")), _f((t or {}).get("c"))
        if not lp or not c:
            return
        pct = (lp / c - 1) * 100
        if pct >= 2 * config.VIX_SPIKE_PCT:
            add("vix", "RED", "INDIA VIX", f"INDIA VIX SPIKE +{pct:.1f}% ({lp:.2f})",
                f"VIX {c:.2f} → {lp:.2f}. Short options lose value fast when volatility jumps — don't add shorts, "
                "check every leg's distance, consider buying wings.", "big")
        elif pct >= config.VIX_SPIKE_PCT:
            add("vix", "AMBER", "INDIA VIX", f"India VIX up {pct:.1f}% ({lp:.2f})",
                f"VIX {c:.2f} → {lp:.2f} today — premiums are swelling; hold off on new shorts until it settles.", "base")

    # ---------------------------------------------------------------- 6 futures buildup
    def _future(self, add, und, t, net_delta):
        lp, c, oi, poi = (_f((t or {}).get(k)) for k in ("lp", "c", "oi", "poi"))
        if not (lp and c and oi and poi):
            return
        p, o = (lp / c - 1) * 100, (oi / poi - 1) * 100
        if abs(p) < config.FUT_PRICE_PCT or abs(o) < config.FUT_OI_PCT:
            return
        state, meaning, against = {
            (1, 1): ("LONG BUILDUP", "fresh longs: the rally has money behind it and tends to continue", net_delta < 0),
            (-1, 1): ("SHORT BUILDUP", "fresh shorts: the fall has money behind it and tends to continue", net_delta > 0),
            (1, -1): ("SHORT COVERING", "shorts exiting: rallies like this often fade once covering ends", False),
            (-1, -1): ("LONG UNWINDING", "longs exiting: weakness without fresh shorts, often limited", False),
        }[(1 if p > 0 else -1, 1 if o > 0 else -1)]
        txt = f"{und} future {p:+.2f}% with OI {o:+.1f}% on the day — {meaning}."
        if against:
            txt += " This runs against your book's delta — check the threatened side."
        add("futures", "AMBER" if against else "INFO", und, f"{state} · {und} futures", txt, state)

    # ---------------------------------------------------------------- 12 discipline
    def _discipline(self, add, pnl_days, orders, now):
        today = now.date()
        rows = []
        for d in pnl_days:
            dt = pd.Timestamp(d.get("date")).date() if d.get("date") is not None else None
            if dt and dt < today:
                rows.append((dt, (_f(d.get("realised")) or 0) + (_f(d.get("mtm")) or 0)))
        rows.sort()
        last = rows[-config.LOSING_STREAK_DAYS:]
        if len(last) == config.LOSING_STREAK_DAYS and all(v < 0 for _, v in last):
            tot = sum(v for _, v in last)
            add("streak", "AMBER", "ACCOUNT", f"{config.LOSING_STREAK_DAYS} losing days in a row ({_rs(tot)})",
                "Losing streaks are when revenge trades happen. Trade smaller today, only A-grade setups, and "
                "stop at the first loss limit.")
        done = [o for o in orders if str(o.get("status", "")).upper() == "COMPLETE"]
        if len(done) >= config.MAX_ORDERS_PER_DAY:
            add("overtrade", "AMBER", "ACCOUNT", f"Overtrading check · {len(done)} orders today",
                f"{len(done)} executed orders (limit {config.MAX_ORDERS_PER_DAY}). Frequent adjustments bleed charges "
                "and often chase the market — pause and review the plan before the next order.")
