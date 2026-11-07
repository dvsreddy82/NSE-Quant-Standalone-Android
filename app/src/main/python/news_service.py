"""News that can move NIFTY and BANKNIFTY: fetched, filtered, rated and stored.

Sources (public RSS, polled every 2 min in and around market hours, 10 min otherwise):
  RBI press releases and notifications · NSE corporate announcements (BANKNIFTY banks only) ·
  Economic Times Markets / Stocks · Livemint Markets · Business Standard Markets ·
  Google News searches (Nifty / Bank Nifty, RBI / big banks; each item names its publisher).

Each headline is kept only if it touches NIFTY, BANKNIFTY, a constituent bank, the RBI or a
macro driver (rates, inflation, crude, rupee, FII flows, Fed, global sell-offs, SEBI F&O rules).
It is tagged (BANKNIFTY / NIFTY / MACRO / FILING), rated HIGH / MEDIUM / LOW by rules (RBI
policy, inflation / GDP data, heavyweight bank results, regulator action, F&O rule changes,
crashes, war… are HIGH), given a headline tone (positive / negative / neutral — a keyword read,
not a forecast), and merged with the same story from other outlets.  Stored in MongoDB `news`.
HIGH items first seen within an hour of publication raise an alert (Mac + phone).
Feed text is untrusted data: it is only displayed, never acted on.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import pandas as pd
import requests

import config

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36"}
GN = "https://news.google.com/rss/search?hl=en-IN&gl=IN&ceid=IN:en&q="
SOURCES = [
    ("RBI", "rbi", "https://www.rbi.org.in/pressreleases_rss.xml"),
    ("RBI notifications", "rbi", "https://www.rbi.org.in/notifications_rss.xml"),
    ("NSE filings", "nse", "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"),
    ("Economic Times", "news", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Economic Times", "news", "https://economictimes.indiatimes.com/markets/stocks/news/rssfeeds/2146842.cms"),
    ("Livemint", "news", "https://www.livemint.com/rss/markets"),
    ("Business Standard", "news", "https://www.business-standard.com/rss/markets-106.rss"),
    ("Google News", "news", GN + "Nifty+OR+%22Bank+Nifty%22+OR+Sensex+when:1d"),
    ("Google News", "news", GN + "%22Reserve+Bank+of+India%22+OR+%22RBI+policy%22+OR+%22RBI+governor%22+OR+%22HDFC+Bank%22+OR+%22ICICI+Bank%22+OR+SBI+OR+%22Kotak+Mahindra+Bank%22+OR+"
                                "%22Axis+Bank%22+when:1d"),
]

BANKS = {  # name fragments → symbol
    "hdfc bank": "HDFCBANK", "icici bank": "ICICIBANK", "state bank of india": "SBIN", " sbi ": "SBIN",
    "kotak mahindra bank": "KOTAKBANK", "kotak bank": "KOTAKBANK", "axis bank": "AXISBANK", "indusind": "INDUSINDBK",
    "federal bank": "FEDERALBNK", "au small finance": "AUBANK", "au bank": "AUBANK", "bank of baroda": "BANKBARODA",
    "idfc first": "IDFCFIRSTB", "punjab national bank": "PNB", " pnb ": "PNB", "canara bank": "CANBK",
    "union bank of india": "UNIONBANK", "yes bank": "YESBANK",
}
HEAVY = {"HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK"}
# NSE filings name the company exactly: match the listed bank, not a group company (Axis MF, PNB MetLife…)
NSE_BANKS = {"hdfc bank limited": "HDFCBANK", "icici bank limited": "ICICIBANK", "state bank of india": "SBIN",
             "kotak mahindra bank limited": "KOTAKBANK", "axis bank limited": "AXISBANK",
             "indusind bank limited": "INDUSINDBK", "the federal bank": "FEDERALBNK", "federal bank limited": "FEDERALBNK",
             "au small finance bank": "AUBANK", "bank of baroda": "BANKBARODA", "idfc first bank": "IDFCFIRSTB",
             "punjab national bank": "PNB", "canara bank": "CANBK", "union bank of india": "UNIONBANK",
             "yes bank limited": "YESBANK"}

KW = {
    "BANKNIFTY": r"bank nifty|banknifty|nifty bank|banking|lender|npa|credit growth|deposit|repo|rbi|psu bank|private bank",
    "NIFTY": r"\bnifty\b|sensex|dalal street|stock market|equit(?:y|ies)|market cap|\bindices\b|f&o|derivative",
    "MACRO": r"inflation|\bcpi\b|\bwpi\b|\bgdp\b|crude|brent|\boil price|rupee|dollar index|bond yield|treasury yield|"
             r"\bfed\b|federal reserve|powell|\bfii\b|\bfpi\b|foreign investor|tariff|recession|\biip\b|fiscal deficit|"
             r"budget|monsoon|war\b|missile|ceasefire|sanction|opec|global sell|wall street|nasdaq|nikkei",
}
MKT = r"nifty|sensex|bank nifty|banknifty|dalal street|indian (?:stock|equity) market|nasdaq|\bdow\b|s&p 500|" \
      r"wall street|kospi|nikkei|hang seng|global markets?|asian markets?|world markets?|stock markets?"
# HIGH rules run on the HEADLINE only, and each needs a market-relevant subject.
HIGH = [
    r"(?:\brbi\b|reserve bank|\bmpc\b).*(?:\brepo\b|\bpolicy\b|rate cut|rate hike|cuts? rate|hikes? rate|\bcrr\b|stance)|"
    r"monetary policy (?:review|decision|outcome)|repo rate",
    r"\bcpi\b|retail inflation|wpi inflation|\bgdp\b (?:data|growth|grows|grew|print)|fiscal deficit|union budget|"
    r"budget 20\d\d",
    r"(?:\bfed\b|federal reserve|fomc|powell).*(?:rate|cut|hike|decision|pause)",
    r"sebi.*(?:f&o|index derivative|equity derivative|weekly expir|lot size)|(?:f&o|weekly expiry) (?:rule|norm|curb|change)",
    r"(?:" + MKT + r").*(?:crash|plunge|tank|bloodbath|biggest (?:fall|drop|gain|rally)|circuit|sell-?off|rout)|"
    r"(?:crash|plunge|bloodbath|rout|sell-?off).*(?:" + MKT + r")",
    r"(?:nifty|sensex|bank nifty)[^,;:|]{0,25}(?:record|all-time|lifetime|fresh) high",
    r"\bwar\b|missile|attack on|terror attack|ceasefire|sanctions? (?:on|against)",
    r"(?:downgrade|upgrade).*(?:india|sovereign|outlook)|(?:moody|fitch|s&p global).*india",
    r"(?:crude|brent|oil price).*(?:surge|jump|spike|soar|plunge|slump|crash|\$1\d\d|\$[3-5]\d\b)",
    r"(?:fii|fpi|foreign investor).*(?:record|biggest|massive|heavy)",
]
JUNK = r"recruitment|vacanc|admit card|score ?card|festive offer|share price live|stock price, news, quote|" \
       r"how much you would have made|fake .* call|scam call|fd rates|fixed deposit rates|raiffeisen|\brbis?\b.*game|" \
       r"pros and cons|live updates|cricket|horoscope|lottery"
RBI_ROUTINE = r"money market operations|auction|weekly statistical|treasury bill|variable rate|lending and deposit rates|" \
              r"reserve money|sectoral deployment|government stock|state government securit|draft|ombudsman"
NSE_ROUTINE = r"newspaper|trading window|analysts?/institutional|transcript|loss of share|duplicate share|esop|" \
              r"allotment of|certificate under|reg\.? 74|compliance certificate|investor meet|change in registrar"
NSE_HIGH = r"financial result|outcome of board|press release|credit rating|reserve bank|\brbi\b|penalty|resignation|" \
           r"appointment of (?:md|ceo|managing)|merger|acquisition|fraud|default"
POS = r"rall(?:y|ies)|surge|soar|jump|gain|climb|rise[sn]?\b|record high|all-time high|beats?\b|upgrade|inflow|cuts? rates|" \
      r"rate cut|strong|bullish|recover|rebound|higher|profit (?:jumps|rises|up)|green"
NEG = r"fall|slump|plunge|tank|crash|drop|decline|slide|sink|lower|miss(?:es|ed)?\b|downgrade|outflow|sell-off|selloff|" \
      r"penalty|hike|war\b|attack|bearish|weak|loss|red\b|probe|fraud|default|concern|worr"


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _when(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
        if d.tzinfo is None:          # no zone given (RBI): Indian sources publish in IST
            d = pd.Timestamp(d, tz=config.IST).to_pydatetime()
        return d
    except (TypeError, ValueError):
        pass
    try:   # NSE: 06-Nov-2026 12:49:03 (IST)
        return pd.Timestamp(datetime.strptime(s.strip(), "%d-%b-%Y %H:%M:%S"), tz=config.IST).to_pydatetime()
    except ValueError:
        return None


def _norm(title: str) -> str:
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)          # Google News: " - Publisher"
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def _tokens(title: str) -> set[str]:
    return {w for w in _norm(title).split() if len(w) > 2}


def classify(title: str, summary: str, kind: str) -> dict | None:
    """Tags, impact and tone for one headline, or None when it is not relevant."""
    head = f" {title} ".lower()
    if re.search(JUNK, head):
        return None
    text = f" {title} {summary} ".lower()
    # news headlines are judged on the headline; RBI / NSE items also use their description
    scope = text if kind in ("rbi", "nse") else head
    if kind == "nse":
        name = title.lower().strip()
        banks = sorted({sym for full, sym in NSE_BANKS.items() if name.startswith(full) or name == full})
    else:
        banks = sorted({sym for frag, sym in BANKS.items() if frag in scope})
    tags = []
    for tag, rx in KW.items():
        if re.search(rx, scope):
            tags.append(tag)
    if banks and "BANKNIFTY" not in tags:
        tags.append("BANKNIFTY")
    if kind == "rbi":
        tags = sorted(set(tags) | {"BANKNIFTY", "RBI"})
        acts = re.search(r"penalty|restrict|bars?\b|ban\b|embargo|supersed|action against|directions? to", text)
        impact = ("LOW" if re.search(RBI_ROUTINE, text) else
                  "HIGH" if re.search(HIGH[0], head) or (acts and banks) else "MEDIUM")
    elif kind == "nse":
        if not banks:
            return None
        tags = sorted(set(tags) | {"FILING", "BANKNIFTY"})
        if re.search(NSE_ROUTINE, text):
            impact = "LOW"
        elif re.search(NSE_HIGH, text):
            impact = "HIGH" if set(banks) & HEAVY else "MEDIUM"
        else:
            impact = "MEDIUM" if set(banks) & HEAVY else "LOW"
    else:
        if not tags:
            return None
        if re.search(RBI_ROUTINE, head):
            return {"tags": tags, "banks": banks, "impact": "LOW", "tone": "neutral"}
        impact = "HIGH" if any(re.search(rx, head) for rx in HIGH) else \
            "MEDIUM" if banks or re.search(r"\bnifty\b|bank nifty|sensex|\brbi\b|\bfii\b", head) else "LOW"
        if set(banks) & HEAVY and re.search(r"q[1-4] (?:result|profit|earnings|net)|net profit|quarterly (?:result|profit)",
                                            head):
            impact = "HIGH"
        if set(banks) & HEAVY and re.search(r"(?:rbi|reserve bank).*(?:penalty|restrict|bars?|ban|action)", head):
            impact = "HIGH"
    pos, neg = len(re.findall(POS, text)), len(re.findall(NEG, text))
    tone = "positive" if pos > neg else "negative" if neg > pos else "neutral"
    return {"tags": tags, "banks": banks, "impact": impact, "tone": tone}


STOP = set("""the a an and or of to in on for at by as is are was be with from after amid over into its it this that what
why how will can may new today live update updates says said sees see key five four three top amid ahead
sensex nifty bank stock stocks market markets share shares index points pts india indian d-street dalal street trade
trades trading session day week open opens close closes higher lower up down gain gains rise rises fall falls""".split())


THEMES = {  # different words for the same story collapse to one theme term
    "SELLOFF": r"sell-?off|selloff|rout|crash|plunge|bloodbath|tumble|slump|tank|meltdown|wipe",
    "US": r"wall street|nasdaq|\bdow\b|s&p|\bus\b|american|chip stocks|ai stocks",
    "ASIA": r"asia|asian|kospi|nikkei|japan|korea|hang seng|taiwan",
    "RALLY": r"rally|surge|soar|jump|rebound|defy|defies|shrug|recover|fireworks|zoom",
    "RBI": r"\brbi\b|reserve bank|repo|monetary policy|\bmpc\b",
    "FED": r"\bfed\b|federal reserve|fomc|powell",
    "CRUDE": r"crude|brent|oil",
    "FII": r"\bfii|\bfpi|foreign (?:fund|investor|inflow|outflow)",
    "INFLATION": r"inflation|\bcpi\b|\bwpi\b",
    "GIFT": r"gift nifty|sgx",
}


def topic_terms(title: str) -> set[str]:
    low = title.lower()
    themes = {k for k, rx in THEMES.items() if re.search(rx, low)}
    words = {w for w in _tokens(title) if w not in STOP and not w.isdigit()}
    return themes | words


class NewsService:
    def __init__(self, svc):
        self.svc = svc
        self.started = datetime.now(timezone.utc)
        self.status: dict = {"ok": 0, "failed": [], "at": None, "new": 0}
        self._alerted: list[tuple[datetime, set[str]]] = []
        self._last_alert: datetime | None = None
        self._held = 0                                      # HIGH headlines held back since the last alert

    def select_alerts(self, new: list[dict]) -> list[dict]:
        """Alert-worthy items, one per topic: a HIGH headline sharing two or more key terms with
        one already alerted in the last 3 hours is the same story (listed, not re-alerted)."""
        now = datetime.now(timezone.utc)
        self._alerted = [(t, k) for t, k in self._alerted if now - t < timedelta(hours=3)]
        out = []
        for d in sorted((x for x in new if x.get("alert")), key=lambda x: x["published"], reverse=True):
            terms = topic_terms(d["title"])
            same_story = any(len(terms & k) >= 2 for _t, k in self._alerted)
            cooling = self._last_alert is not None and now - self._last_alert < timedelta(minutes=20)
            if same_story or cooling or out:          # one alert per poll; the rest are counted
                d["alert"] = False
                self._held += 1
                continue
            d["related"] = self._held                 # headlines held back since the previous alert
            self._held = 0
            self._alerted.append((now, terms))
            self._last_alert = now
            out.append(d)
        return out

    @property
    def store(self):
        return self.svc.store

    def _fetch(self, src) -> list[dict]:
        name, kind, url = src
        r = requests.get(url, headers=UA, timeout=15)
        r.raise_for_status()
        out = []
        for it in ET.fromstring(r.content).findall(".//item")[:120]:
            title = _clean(it.findtext("title") or "")
            if not title:
                continue
            summary = _clean(it.findtext("description") or "")
            publisher = it.findtext("source") or name
            if kind == "nse":
                summary = summary.replace(title, "").strip(" |")
            out.append({"title": title, "summary": summary[:400], "link": (it.findtext("link") or "").strip(),
                        "published": _when(it.findtext("pubDate")), "source": publisher.strip(), "kind": kind})
        return out

    def poll(self) -> list[dict]:
        """Fetch every source, keep the relevant items, merge duplicates, store. Returns the
        items new to the store (each with `alert` set when it deserves a notification)."""
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(lambda s: (s, self._try(s)), SOURCES))
        fetched, failed = [], []
        for (name, _k, _u), items in results:
            if items is None:
                failed.append(name)
            else:
                fetched += items
        now = datetime.now(timezone.utc)
        recent = self.recent(hours=36)
        known_ids = {d["id"] for d in recent}
        clusters = [(_tokens(d["title"]), d) for d in recent]
        new, updates = [], []
        for it in sorted(fetched, key=lambda x: x["published"] or now):
            if it["published"] and it["published"] < now - timedelta(days=2):
                continue
            c = classify(it["title"], it["summary"], it["kind"])
            if not c:
                continue
            nid = hashlib.sha1(_norm(it["title"]).encode()).hexdigest()[:16]
            if nid in known_ids:
                continue
            toks = _tokens(it["title"])
            dup = next((d for t, d in clusters if t and toks and len(t & toks) / len(t | toks) >= 0.6), None)
            if dup is not None:                       # same story from another outlet
                if it["source"] not in dup.get("sources", []):
                    dup.setdefault("sources", []).append(it["source"])
                    updates.append(dup)
                known_ids.add(nid)
                continue
            doc = {"id": nid, "title": re.sub(r"\s+-\s+[^-]{2,40}$", "", it["title"]) if it["kind"] == "news"
                   else it["title"], "summary": it["summary"], "link": it["link"], "source": it["source"],
                   "sources": [it["source"]], "published": it["published"] or now, "seen_at": now, **c}
            pub = doc["published"]
            doc["alert"] = (c["impact"] == "HIGH" and now - pub <= timedelta(minutes=60)
                            and now - self.started > timedelta(seconds=30)) or \
                           (c["impact"] == "HIGH" and pub > self.started - timedelta(minutes=10))
            new.append(doc)
            clusters.append((toks, doc))
            known_ids.add(nid)
        if new or updates:
            self.store.upsert_docs("news", new + [{k: v for k, v in u.items()} for u in updates])
        self.status = {"ok": len(SOURCES) - len(failed), "failed": failed, "at": datetime.now(), "new": len(new),
                       "total": len(SOURCES)}
        return new

    def _try(self, src):
        try:
            return self._fetch(src)
        except Exception as exc:
            log.info("News source %s failed: %s", src[0], exc)
            return None

    def recent(self, hours: int = 24, impact: str | None = None, tag: str | None = None, limit: int = 300) -> list[dict]:
        q = {"impact": impact} if impact else {}               # filter in the store, then trim
        docs = self.store.find_docs("news", q, sort="published", desc=True, limit=limit * (4 if tag else 1) + 50)
        cut = datetime.now(timezone.utc) - timedelta(hours=hours)
        out = []
        for d in docs:
            p = pd.Timestamp(d["published"])
            p = p.tz_localize("UTC") if p.tzinfo is None else p
            if p.to_pydatetime() < cut:
                continue
            if impact and d.get("impact") != impact:
                continue
            if tag and tag not in d.get("tags", []):
                continue
            d["published"] = p.to_pydatetime()
            out.append(d)
        return out[:limit]


def market_window(now=None) -> bool:
    """08:30–16:00 IST on weekdays: poll every 2 min; otherwise every 10 min."""
    t = pd.Timestamp.now(tz=config.IST) if now is None else now
    return t.weekday() < 5 and (t.hour * 60 + t.minute) in range(8 * 60 + 30, 16 * 60)

