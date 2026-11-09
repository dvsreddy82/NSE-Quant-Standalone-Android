"""'Export for Gemini': one plain-text market brief to paste into the Gemini app (or any chat AI).

Everything comes from numbers this app has already computed — index levels, GIFT Nifty and the
global cue, the option chains, the writer risk monitor, direction calls and levels, FII/DII flows,
BANKNIFTY breadth, today's alerts and high-impact news.  Your positions, P&L and account limits are
included only when asked for (`positions=True`).  No credentials, API keys or account IDs are ever
part of it.

The text starts with an instruction block that tells the model to use only these numbers, quote
them, flag missing or stale data, and not to forecast or promise outcomes — then the data, then
a few questions to start with.  Each section is built on its own, so a section whose data isn't
loaded yet is skipped instead of breaking the brief.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import pandas as pd

import config

log = logging.getLogger(__name__)

PROMPT = """You are helping an Indian index-options seller (NIFTY and BANKNIFTY, positional, mostly short options
with hedges) read today's market. Below is a snapshot exported by their own analytics app.

Rules:
- Use only the data below. Quote the numbers behind every point (levels, %, OI, IV, ₹).
- Say plainly when something is missing, stale or contradictory; do not fill gaps with guesses.
- Index levels and strikes are points, not ₹. PCR = put OI / call OI. "Writer" means option seller.
- Do not predict the market with certainty or promise outcomes; lay out scenarios with what would confirm or
  invalidate each, and leave every decision to the trader.
- Be concise: short headings and bullets.
- End with: "Analysis of exported data — not investment advice.\""""

QUESTIONS_MARKET = """Start with:
1. What is the overall read for today (or the next session) and which 3 numbers matter most?
2. For NIFTY and BANKNIFTY: where are the strongest support / resistance and OI walls, and which side looks
   safer to write?
3. Which signals disagree with each other, and what would change the read?"""

QUESTIONS_BOOK = """4. For each open position: how does it sit against the walls, the expected move and the alerts above?
   What are the options (hold / adjust / book / exit) and their trade-offs?"""


def _n(v, nd=0, sign=False) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "n/a"
    if x != x:
        return "n/a"
    return f"{x:+,.{nd}f}" if sign else f"{x:,.{nd}f}"


def _t(v) -> str:
    try:
        ts = pd.Timestamp(v)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
        return f"{ts.tz_convert(config.IST):%d %b %H:%M}"
    except Exception:
        return str(v)


def _section(title: str, lines: list[str]) -> str:
    lines = [ln for ln in lines if ln]
    return f"## {title}\n" + "\n".join(lines) if lines else ""


# ---------------------------------------------------------------------------------- sections
def _indices(r) -> list[str]:
    o = r.overview({})
    out = [f"- {x['name']}: {_n(x['last'], 2)} ({_n(x.get('chg'), 2, True)}, {_n(x.get('pct'), 2, True)}%)"
           + (f" · day range {_n(x['low'], 2)}–{_n(x['high'], 2)}" if x.get("low") and x.get("high") else "")
           for x in o["indices"]]
    if out:
        out.append(f"- Source: {o['source']}")
    return out


def _global(r) -> list[str]:
    w = r.win.watch
    cue = w.cue or {}
    out = []
    g = cue.get("gift") or {}
    if g.get("last"):
        out.append(f"- GIFT Nifty: {_n(g['last'], 1)} ({_n(g.get('pct'), 2, True)}%), "
                   f"{_n(g.get('gap'), 0, True)} pts ({_n(g.get('gap_pct'), 2, True)}%) vs NIFTY close"
                   f" → {g.get('open', '')}" + (" [stale]" if g.get("stale") else ""))
    if cue:
        out.append(f"- Global cue score {_n(cue.get('score'), 0, True)} (−100…+100): {cue.get('read')} — "
                   f"{cue.get('writer')}")
        for d in cue.get("drivers") or []:
            if not d.startswith("GIFT"):
                out.append(f"  · {d}")
    movers = sorted((q for q in w.quotes if q.get("pct") is not None and not q.get("stale") and q["key"] != "GIFT"),
                    key=lambda q: -abs(q["pct"]))[:8]
    if movers:
        out.append("- Biggest moves: " + "; ".join(f"{q['name']} {_n(q['pct'], 2, True)}%" for q in movers))
    return out


def _chains(r) -> list[str]:
    out = []
    for und in ("NIFTY", "BANKNIFTY"):
        c = r.win.options.fresh(und, 12 * 3600)
        if not c:
            try:                                     # not cached yet: fetch it (as /api/chain does)
                with r._chain_locks[und]:
                    c = r.win.options.fresh(und, 12 * 3600) or r.win.options.analyse(und)
            except Exception as exc:
                log.info("gemini export: %s chain: %s", und, exc)
        if not c:
            out.append(f"### {und}: option chain not loaded yet")
            continue
        wall = lambda w: f"{_n(w['strike'])} (OI {_n(w['oi'] / 1e3, 1)}k, chg {_n(w['chg_oi'] / 1e3, 1, True)}k)"
        cw = ", ".join(wall(w) for w in (c.get("call_walls") or [])[:3])
        pw = ", ".join(wall(w) for w in (c.get("put_walls") or [])[:3])
        out += [f"### {und} — expiry {c['expiry']} ({_n(c.get('dte'), 1)} days), chain as of {_t(c.get('timestamp'))}",
                f"- Spot {_n(c['spot'], 2)}, ATM {_n(c['atm'])}, ATM IV {_n(c.get('atm_iv'), 1)}%, "
                f"HV10/HV20 {_n(c.get('hv10'), 1)}/{_n(c.get('hv20'), 1)}%, IV−HV premium {_n(c.get('vrp'), 1, True)}",
                f"- ATM straddle {_n(c.get('straddle'), 1)} · expected move ±{_n(c.get('sigma_move'))} pts to expiry",
                f"- PCR {_n(c.get('pcr'), 2)} (change {_n(c.get('pcr_chg'), 2, True)}) · max pain {_n(c.get('max_pain'))}"
                f" · put−call skew {_n(c.get('skew'), 1, True)} IV pts",
                f"- Call OI walls (above spot): {cw or 'n/a'}",
                f"- Put OI walls (below spot): {pw or 'n/a'}",
                f"- Trend {c.get('trend') or 'n/a'}, ADX {_n(c.get('adx'), 0)}, bias {c.get('bias') or 'n/a'}",
                f"- App verdict for premium selling: {c.get('verdict')} (score {_n(c.get('score'), 0, True)})"]
        for s, t in (c.get("checks") or [])[:8]:
            out.append(f"  · [{s:+}] {t}")
        ev = [f"{e['date']:%d %b} {e['event']}" for e in c.get("events") or [] if e.get("date")]
        if ev:
            out.append("- Events before expiry: " + "; ".join(ev))
    return out


def _risk(r) -> list[str]:
    out = []
    for und, x in r.win.risk_view.results.items():
        up, dn = x.get("danger_up") or (None, ""), x.get("danger_down") or (None, "")
        out += [f"### {und} — regime {x['regime']}, bias {x['bias']}",
                f"- Call wall {_n(x.get('call_wall'))}, put wall {_n(x.get('put_wall'))}, "
                f"net dealer gamma {_n(x['gex']['net'] / 1e7, 2, True)} Cr per 1%, gamma flip {_n(x['gex'].get('flip'))}",
                f"- Danger up {_n(up[0])} ({up[1]}); danger down {_n(dn[0])} ({dn[1]})"]
        for a in x.get("alerts") or []:
            out.append(f"- {a['level']}: {a['title']} — " + "; ".join(a.get("signals", [])[:2])
                       + (f" → {a['action']}" if a.get("action") else ""))
        for p in (x.get("prohibitions") or [])[:3]:
            out.append(f"  · Avoid: {p}")
        for s in (x.get("safe") or [])[:3]:
            out.append(f"  · Safer zone: {s}")
    return out


def _calls(r) -> list[str]:
    w = r.win.watch
    out = []
    for und in ("NIFTY", "BANKNIFTY"):
        c = w.latest_call(und)
        if c:
            right, n = w.hit_rate(w.today_calls(und))
            out.append(f"- {und} {str(c.get('slot', ''))[-5:]}: {c.get('direction')} ({c.get('confidence')}, "
                       f"score {_n(c.get('score'), 0, True)}) at {_n(c.get('spot'), 1)}"
                       + (f" · today's calls right {right}/{n}" if n else "") + f" — app's idea: {c.get('idea')}")
            for s in (c.get("signals") or [])[:5]:
                out.append(f"  · [{s['score']:+}] {s['text']}")
        lv = w.levels.get(und) or []
        if lv:
            st = w.level_status(und)
            out.append(f"- {und} levels: " + "; ".join(
                f"{m['name']} {_n(m['price'])}" + (f" ({st[round(m['price'])]})" if st.get(round(m["price"])) else "")
                for m in lv[:8]))
    return out


def _fii(r) -> list[str]:
    x = r.win.fii_view.res or {}
    if not x.get("available"):
        return []
    out = [f"- As of {x['as_of']:%d %b}: score {_n(x['score'], 0, True)} (−100…+100), {x['bias']}, conviction "
           f"{x['conviction']}; writing side it favours: {x['playbook'].get('side')}"]
    for c in x.get("components") or []:
        out.append(f"  · {c['label']}: {c['value']} ({c['reading']})")
    f = x["frame"].tail(5)
    out.append("- Last sessions (₹ Cr cash FII / DII, FII index-fut long %): " + "; ".join(
        f"{ts:%d %b} {_n(v.get('fii_cash'), 0, True)} / {_n(v.get('dii_cash'), 0, True)}, {_n(v.get('fii_long_pct'), 0)}%"
        for ts, v in f.iterrows()))
    for wn in (x.get("warnings") or [])[:3]:
        out.append(f"  · Note: {wn}")
    s = x.get("setup") or {}
    if s.get("direction"):
        out.append(f"- Evening squeeze-loading setup on {pd.Timestamp(s['date']):%d %b}: {s['direction'].upper()}")
    return out


def _breadth(r) -> list[str]:
    b = r.win.bnf_svc
    if not b.symbols:
        return []
    s = b.live_snapshot()
    out = [f"- {s['adv']} up / {s['dec']} down; heavyweights up by weight {_n(s['up_weight'], 0)}%"]
    top = sorted(s["rows"], key=lambda x: -(x.get("weight") or 0))[:6]
    if top:
        out.append("- Heaviest: " + "; ".join(f"{x.get('symbol')} {_n(x.get('pct'), 2, True)}%" for x in top))
    out += [f"- {lv.upper()}: {t}" for lv, t in s["alerts"][:4]]
    return out


def _alerts(r) -> list[str]:
    w = r.win.watch
    seen, out = set(), []
    for a in w.recent_alerts(1, ("WRITER", "LEVEL", "GLOBAL")):          # newest first; one line per subject
        key = (a.get("kind"), a["title"].split(" ▲")[0].split(" ▼")[0].split(":")[0])
        if key in seen or len(out) >= 12:
            continue
        seen.add(key)
        out.append(f"- {a.get('level', '')} {a['title']}: {a['text']} ({_t(a.get('raised_at'))})")
    for d in r.win.news_svc.recent(hours=12, impact="HIGH", limit=8):
        out.append(f"- NEWS ({_t(d.get('published'))}, {d.get('source', '')}): {d['title']}")
    return out


def _book(r) -> list[str]:
    d = r.win.trades.data or {}
    out = []
    for g in d.get("positions") or []:
        legs = ", ".join(f"{l.get('qty'):+} {l.get('tsym')} @ {_n(l.get('avg'), 2)} (ltp {_n(l.get('lp'), 2)})"
                         for l in g["legs"])
        out.append(f"- {g['und']} {g['structure']} exp {g['expiry']} ({_n(g.get('dte'), 1)} d), "
                   f"{'defined' if g['defined_risk'] else 'UNDEFINED'} risk: credit ₹{_n(g['credit'])}, open P&L "
                   f"₹{_n(g['open_pnl'], 0, True)}, captured {_n(g.get('captured'), 0)}% — {legs}")
        out += [f"  · {a}: {t}" for a, t in g["alerts"]]
    t = d.get("today") or {}
    if t:
        out.append(f"- Today: open P&L ₹{_n(t.get('open'), 0, True)}, day MTM ₹{_n(t.get('total'), 0, True)}")
    acc = getattr(r.win, "_account", None) or {}
    for a in acc.get("alerts", []):
        out.append(f"- Account: {a['title']} — {a['text']}")
    return out or ["- No open positions."]


SECTIONS = [("Indices", _indices), ("GIFT Nifty and global markets", _global), ("Option chains", _chains),
            ("Writer risk monitor", _risk), ("Direction calls and key levels", _calls),
            ("FII / DII positioning", _fii), ("BANKNIFTY breadth", _breadth),
            ("Today's alerts and high-impact news", _alerts)]


def build(routes, positions: bool = False) -> dict:
    """routes: api_routes.ApiRoutes (its .win carries the services).  → {"text", "chars", "missing"}."""
    from market_hours import market_open, now_ist
    now = now_ist()
    head = (f"# NSE market snapshot — {now:%a %d %b %Y, %H:%M} IST "
            f"(market {'open' if market_open() else 'closed'})")
    parts, missing = [PROMPT, head], []
    sections = SECTIONS + ([("My open positions and P&L", _book)] if positions else [])
    for title, fn in sections:
        try:
            s = _section(title, fn(routes))
        except Exception as exc:                    # one missing feed shouldn't sink the brief
            log.info("gemini export: %s skipped: %s", title, exc)
            s = ""
        if s:
            parts.append(s)
        else:
            missing.append(title)
    if missing:
        parts.append("## Not available in this snapshot\n- " + ", ".join(missing))
    parts.append(QUESTIONS_MARKET + ("\n" + QUESTIONS_BOOK if positions else ""))
    text = "\n\n".join(parts)
    return {"text": text, "chars": len(text), "missing": missing, "positions": positions, "time": datetime.now()}
