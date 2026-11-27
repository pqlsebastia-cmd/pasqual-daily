#!/usr/bin/env python3
"""
Pasqual Daily - Stage 0 feed scan.

Reads each outlet's RSS/Atom feed (or, for two outlets without feeds, the page itself),
keeps items from the last N hours, and writes a compact candidate list plus a coverage
report. Claude then does Stage 1 (picking and writing) from the candidates file.

Python 3.9+, standard library only. Run it on a computer with normal internet access.

Usage:
    python3 rss_scan.py                         # 72h window, writes ./scan_output/
    python3 rss_scan.py --hours 48 --out ./scan_output

Outputs (in --out), dated with the Pacific date of the run:
    candidates_YYYY-MM-DD.json   every item inside the window, newest first, plus run summary
    coverage_YYYY-MM-DD.md       per outlet: reached / stale / failed / needs_fetch, item counts

Statuses:
    reached      feed or page read, at least one item inside the window
    stale        feed or page read, but nothing inside the window: Claude checks the outlet page
    needs_fetch  outlet has no usable feed: Claude opens its page directly
    failed       nothing could be read: Claude uses a web-search fallback

Exit code is 1 when fewer than MIN_REACHED outlets were read (for example no network),
so a scheduler can tell a broken run from a quiet news day.
"""

import argparse
import html
import json
import os
import re
import time
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

try:
    from zoneinfo import ZoneInfo
    PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # missing tz database: fall back to UTC dates
    PACIFIC = timezone.utc

UA = "Mozilla/5.0 (compatible; PasqualDailyScan/1.1)"
TIMEOUT = 20
MIN_REACHED = 10
MARCA_KEEP = re.compile(r"\bF1\b|f[oó]rmula 1|alonso|aston martin|villarreal", re.I)
MATCH_HOURS = 24         # a Villarreal result this fresh must run; older results were in yesterday's issue
PRIORITY_HOURS = 30      # priority outlets (Blooloop, IAAPA): every item this fresh must be used or explained
VILLARREAL_RE = re.compile(r"villarreal|submarino groguet|groguets|estadio de la cer[aá]mica", re.I)
BOILER_RE = re.compile(r"^(for [\w .&'-]{2,30} fans\.|want to bookmark|this is read by an automated voice|sign up|subscribe)"
                       r"|has over \d+ years'? experience|graduated from the university", re.I)
SCORE_RE = re.compile(r"\b\d{1,2}\s?[-–]\s?\d{1,2}\b|\b(vence|gana|cae|pierde|empata|golea|remonta|derrota|victoria|empate|resumen|cr[oó]nica)\b", re.I)
EXCERPT_HOURS = 48       # article pages fetched for an excerpt when the item is this fresh
EXCERPT_MAX = 340        # cap on article fetches per run
COMMON_FEED_PATHS = ["/feed/", "/feed", "/rss", "/rss.xml", "/atom.xml", "/index.xml"]

# sections: A Disney Parks, B Industry, C Rumors, D Sim/Gaming, E Tech, F F1/Villarreal, G Film/TV
# "feeds": known feed URLs, tried first (verified from a live run on Nov 25, 2026).
# "no_feed": True means Claude must open the page itself.
# "scrape": no feed exists, so the script reads the page itself (see SCRAPERS). Dates are date-only.
OUTLETS = [
    {"name": "Disney Parks Blog", "sections": "A", "home": "https://disneyparksblog.com/",
     "feeds": ["https://disneyparksblog.com/feed/"]},
    {"name": "Disney Company Newsroom", "sections": "A,G", "home": "https://thewaltdisneycompany.com/news/",
     "feeds": ["https://thewaltdisneycompany.com/feed/"]},
    # Blooloop: every section feed is read and merged (theme parks alone missed most of the daily newsletter).
    {"name": "Blooloop", "sections": "A,B", "home": "https://blooloop.com/news/", "merge": True, "priority": True,
     "feeds": ["https://blooloop.com/feeds/news.rss", "https://blooloop.com/feeds/theme-park.rss",
               "https://blooloop.com/feeds/features.rss", "https://blooloop.com/feeds/museum.rss",
               "https://blooloop.com/feeds/water-parks.rss", "https://blooloop.com/feeds/zoo-aquarium.rss",
               "https://blooloop.com/feeds/immersive.rss", "https://blooloop.com/feeds/technology.rss"]},
    {"name": "Attractions Magazine", "sections": "A,B,C", "home": "https://attractionsmagazine.com/",
     "feeds": ["https://attractionsmagazine.com/feed/"]},
    {"name": "Theme Park Insider", "sections": "A,B", "home": "https://www.themeparkinsider.com/",
     "feeds": ["https://www.themeparkinsider.com/news/rss.xml"]},
    {"name": "ParkStop", "sections": "A,C", "home": "https://orlandoparkstop.com/",
     "feeds": ["https://orlandoparkstop.com/feed/"]},
    {"name": "Boardwalk Times", "sections": "A,C", "home": "https://boardwalkstoriesfromtheseashore.substack.com/",
     "feeds": ["https://boardwalkstoriesfromtheseashore.substack.com/feed"]},
    {"name": "Orlando Business Journal", "sections": "A,B", "home": "https://www.bizjournals.com/orlando/",
     "no_feed": True},  # blocks scripts (403)
    {"name": "Inside Universal", "sections": "B,C", "home": "https://www.insideuniversal.net/",
     "feeds": ["https://www.insideuniversal.net/feed/"]},
    {"name": "IAAPA news", "sections": "B", "home": "https://iaapa.org/news/latestnews", "priority": True,
     "feeds": ["https://iaapa.org/rss.xml"]},
    {"name": "EuroAmusement Professional", "sections": "B", "home": "https://www.eap-magazin.de/",
     "no_feed": True},  # advertises no feed
    {"name": "Screamscape", "sections": "B,C", "home": "https://www.screamscape.com/", "scrape": "screamscape"},
    {"name": "Planet Coaster news (Frontier)", "sections": "D", "home": "https://www.planetcoaster.com/news", "priority_hours": 72,
     "scrape": "frontier"},
    # Gaming: low volume, so every item in the window must be used or explained (priority_hours = full window).
    {"name": "Sim games (Google News)", "sections": "D", "google_news": True, "merge": True, "priority_hours": 72,
     "home": "https://news.google.com/search?q=Planet+Coaster+2",
     "feeds": ["https://news.google.com/rss/search?q=%22Planet+Coaster%22+when:3d&hl=en-US&gl=US&ceid=US:en",
               "https://news.google.com/rss/search?q=(Parkitect+OR+%22Theme+Park+Tycoon+2%22+OR+Metaparks+OR+%22NoLimits+2%22+OR+%22RollerCoaster+Tycoon%22)+when:3d&hl=en-US&gl=US&ceid=US:en"]},
    # Creators Pasqual follows: leads for D. A video counts only with an official tie (preview access, teaser, reveal);
    # otherwise it stays a lead, Unconfirmed, or is skipped as creator chatter.
    {"name": "Sim creators (YouTube)", "sections": "D", "merge": True, "priority_hours": 30, "home": "https://www.youtube.com/@PlanetCoaster",
     "feeds": [
               "https://www.youtube.com/feeds/videos.xml?channel_id=UCzpQvNnyoKgpu0lJdeXbFmQ",  # Planet Coaster 2 (official)
               "https://www.youtube.com/feeds/videos.xml?channel_id=UCs9i_iUEMv5ZSqUvt-9tOew",  # Benji's Adventures
               "https://www.youtube.com/feeds/videos.xml?channel_id=UC7cJdusA0kUuRb4EJix8j1w",  # Rudi Rennkamel
               "https://www.youtube.com/feeds/videos.xml?channel_id=UC4EUXrCSgY4X-ecLVDCA27g",  # Nerdchacho
               "https://www.youtube.com/feeds/videos.xml?channel_id=UCyIz8ItMLR9ru19bAWCUiug",  # Geekism
               "https://www.youtube.com/feeds/videos.xml?channel_id=UCF1M91ptiuNID6VfxtdRWow",  # Channel5 Gaming
               "https://www.youtube.com/feeds/videos.xml?channel_id=UCFpU2nyIaNd_MNHJRoxVhdA",  # MrSimside
               "https://www.youtube.com/feeds/videos.xml?channel_id=UCpiDZsC79K1_tWqHMgRW3Pw",  # Tagi
               "https://www.youtube.com/feeds/videos.xml?channel_id=UCc7iDc94kkK7C_nG19Y7A9g",  # PixelWess89
     ]},
    # Park news creators: leads for A and B only (reveals, construction), not a rumor source. Link the written source when one exists.
    {"name": "Park news creators (YouTube)", "sections": "A,B", "merge": True, "home": "https://www.youtube.com/@DSNYNewscast",
     "feeds": [
               "https://www.youtube.com/feeds/videos.xml?channel_id=UCRH49DSFyl8oPwsiHyZ9O0g",  # DSNY Newscast
               "https://www.youtube.com/feeds/videos.xml?channel_id=UCy9i4O3Bvdc5fHG6LgtnP5g",  # Theme Park Stop
               "https://www.youtube.com/feeds/videos.xml?channel_id=UCcTXq3_bEm_XOVukujbNFfA",  # coasterradio
     ]},
    {"name": "Reddit (sim games)", "sections": "D", "merge": True, "priority_hours": 30, "home": "https://www.reddit.com/r/PlanetCoaster/",
     "feeds": ["https://www.reddit.com/r/PlanetCoaster/top/.rss?t=week", "https://www.reddit.com/r/ThemeParkTycoon2/top/.rss?t=week",
               "https://www.reddit.com/r/Parkitect/top/.rss?t=week"]},
    {"name": "Steam news: Parkitect", "sections": "D", "home": "https://store.steampowered.com/app/453090/", "priority_hours": 72,
     "feeds": ["https://store.steampowered.com/feeds/news/app/453090/"]},
    {"name": "Steam news: NoLimits 2", "sections": "D", "home": "https://store.steampowered.com/app/301320/", "priority_hours": 72,
     "feeds": ["https://store.steampowered.com/feeds/news/app/301320/"]},
    {"name": "Steam news: Planet Coaster 2", "sections": "D", "home": "https://store.steampowered.com/app/2688950/", "priority_hours": 72,
     "feeds": ["https://store.steampowered.com/feeds/news/app/2688950/"]},
    # r/PlanetCoaster removed: Reddit refuses scripts (404 / 429). Community leads come from searches.
    {"name": "9to5Mac", "sections": "E", "home": "https://9to5mac.com/", "feeds": ["https://9to5mac.com/feed/"]},
    {"name": "TechRadar", "sections": "E", "home": "https://www.techradar.com/",
     "feeds": ["https://www.techradar.com/feeds.xml"]},
    {"name": "The Verge", "sections": "E", "home": "https://www.theverge.com/",
     "feeds": ["https://www.theverge.com/rss/index.xml"]},
    {"name": "Motorsport.com F1", "sections": "F", "home": "https://www.motorsport.com/f1/",
     "feeds": ["https://www.motorsport.com/rss/f1/news/"]},
    {"name": "Marca", "sections": "F", "home": "https://www.marca.com/",
     "feeds": ["https://www.marca.com/rss/googlenews/portada.xml"]},
    # Villarreal: the club site has no feed and Marca's front-page feed misses most club news,
    # so a Google News search covers match results, club news, injuries and transfer rumors from all outlets.
    {"name": "Villarreal (Google News)", "sections": "F", "google_news": True, "merge": True,
     "home": "https://news.google.com/search?q=Villarreal+CF",
     "feeds": ["https://news.google.com/rss/search?q=%22Villarreal%22+when:1d&hl=es&gl=ES&ceid=ES:es",
               "https://news.google.com/rss/search?q=Villarreal+(fichaje+OR+lesi%C3%B3n+OR+renovaci%C3%B3n+OR+rumor)+when:2d&hl=es&gl=ES&ceid=ES:es"]},
    {"name": "AS", "sections": "F", "home": "https://as.com/", "no_feed": True},  # advertises no feed
    {"name": "Mediterraneo", "sections": "F", "home": "https://www.elperiodicomediterraneo.com/",
     "no_feed": True},  # security filter blocks scripts (406)
    {"name": "Variety", "sections": "G", "home": "https://variety.com/", "feeds": ["https://variety.com/feed/"]},
    {"name": "Deadline", "sections": "G", "home": "https://deadline.com/", "feeds": ["https://deadline.com/feed/"]},
    {"name": "The Hollywood Reporter", "sections": "G", "home": "https://www.hollywoodreporter.com/",
     "feeds": ["https://www.hollywoodreporter.com/feed/"]},
]


def fetch(url, tries=3):
    """GET with a size cap. Google News and Reddit answer 429/503 when busy, so those retry with a pause."""
    for n in range(tries):
        req = Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read(4_000_000)  # cap: a page or feed larger than 4 MB is cut
        except HTTPError as exc:
            if exc.code in (429, 503) and n < tries - 1:
                time.sleep(15 * (n + 1))
                continue
            raise


class FeedLinkFinder(HTMLParser):
    """Collects <link rel="alternate" type="application/rss+xml|atom+xml" href="..."> tags."""

    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        kind = a.get("type", "").lower()
        if "alternate" in a.get("rel", "").lower() and ("rss" in kind or "atom" in kind):
            if a.get("href"):
                self.links.append(a["href"])


def discover_feeds(html_bytes, base_url):
    finder = FeedLinkFinder()
    finder.feed(html_bytes.decode("utf-8", errors="replace"))
    found = [urljoin(base_url, h) for h in finder.links]
    for p in COMMON_FEED_PATHS:
        found.append(urljoin(base_url, p))
    seen, out = set(), []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def namespaced(tag):
    return tag.startswith("{") and not tag.startswith("{http://www.w3.org/2005/Atom}")


def parse_date(text):
    """RFC 822 (RSS) or ISO 8601 (Atom). Works on Python 3.9, whose fromisoformat is strict."""
    if not text:
        return None
    text = text.strip()
    d = None
    try:
        d = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        iso = text.replace("Z", "+00:00")
        iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso)            # +0000 -> +00:00
        iso = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], iso)  # 6-digit fraction
        try:
            d = datetime.fromisoformat(iso)
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


_XML_SAFE_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}


def _fix_entities(xml_text):
    """Replace HTML-only named entities (&nbsp; etc.) that make strict XML parsing fail."""
    def rep(m):
        name = m.group(1)
        if name in _XML_SAFE_ENTITIES:
            return m.group(0)
        ch = html.unescape("&" + name + ";")
        return ch if ch != "&" + name + ";" else ""
    return re.sub(r"&([A-Za-z][A-Za-z0-9]*);", rep, xml_text)


def url_key(u):
    """Dedupe key: URL without query string, except where the query names the item (YouTube ?v=)."""
    if "youtube.com/watch" in u or "news.google.com" in u:
        return strip_tracking(u)
    return u.split("?")[0].rstrip("/")


def strip_tracking(u):
    """Drop utm_* tracking parameters so links are clean and match the archive."""
    if "?" not in u or "news.google.com" in u:
        return u
    base, q = u.split("?", 1)
    keep = [x for x in q.split("&") if x and not x.lower().startswith("utm_")]
    return base + ("?" + "&".join(keep) if keep else "")


def clean_text(raw):
    """Feed descriptions often hold HTML. Return plain text, whitespace collapsed."""
    txt = re.sub(r"<[^>]+>", " ", html.unescape(raw or ""))
    return " ".join(html.unescape(txt).split())


DATE_PRIORITY = {"published": 0, "pubdate": 0, "issued": 1, "date": 1, "updated": 2, "modified": 2}


def parse_feed(xml_bytes):
    """Returns a list of {title, url, published} from an RSS 2.0, RSS 1.0 or Atom feed."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        text = _fix_entities(xml_bytes.decode("utf-8", errors="replace"))
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError:
            # Broken feed (e.g. one bad item or a stray tag): parse each item on its own and keep the good ones.
            good = []
            for m in re.finditer(r"<(item|entry)\b.*?</\1>", text, re.S):
                try:
                    ET.fromstring(re.sub(r"<(\w+):", r"<\1_", re.sub(r"</(\w+):", r"</\1_", m.group(0))).encode("utf-8"))
                    good.append(re.sub(r"<(\w+):", r"<\1_", re.sub(r"</(\w+):", r"</\1_", m.group(0))))
                except ET.ParseError:
                    continue
            if not good:
                raise
            root = ET.fromstring(("<rss><channel>" + "".join(good) + "</channel></rss>").encode("utf-8"))
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title, url, guid, summary, source, source_url = "", "", "", "", "", ""
        best_date, best_rank = "", 99
        for child in el:
            name = local(child.tag)
            if name == "title" and not namespaced(child.tag):
                title = html.unescape("".join(child.itertext()).strip())
            elif name == "link" and not namespaced(child.tag):
                href = child.attrib.get("href")
                if href:  # Atom
                    if child.attrib.get("rel", "alternate") == "alternate" or not url:
                        url = href.strip()
                elif child.text and not url:  # RSS
                    url = child.text.strip()
            elif name in DATE_PRIORITY and (child.text or "").strip():
                if DATE_PRIORITY[name] < best_rank:  # published beats updated
                    best_date, best_rank = child.text, DATE_PRIORITY[name]
            elif name == "guid":
                guid = (child.text or "").strip()
            elif name in ("description", "summary", "encoded") and not summary:
                summary = clean_text("".join(child.itertext()))
            elif name == "group" and not summary:
                for g in child:
                    if local(g.tag) == "description":
                        summary = clean_text("".join(g.itertext()))[:600]
            elif name == "source":
                source = (child.text or "").strip()
                source_url = child.attrib.get("url", "")
        if not url and guid.startswith("http"):
            url = guid
        if title and url:
            it = {"title": " ".join(title.split()), "url": url, "published": parse_date(best_date)}
            if summary and summary.lower() not in title.lower():
                it["summary"] = summary[:400]
            if source:
                it["source"], it["source_url"] = source, source_url
            items.append(it)
    return items


class ScreamscapeParser(HTMLParser):
    """Screamscape groups headlines under 'News Update: M/D/YYYY' lines. Its links go to topic
    pages (one page per park or company), not to single articles. Text is accumulated across
    tags so a date split by <b> or <font> tags is still found. Parsing stops at the footer."""

    DATE_RE = re.compile(r"News Updates?:\s*(\d{1,2})/(\d{1,2})/(\d{4})")
    STOP_RE = re.compile(r"Previous Headlines|Copyright\s*©", re.I)

    def __init__(self):
        super().__init__()
        self.cur = None
        self.a = None
        self.buf = ""
        self.done = False
        self.items = []

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag == "a":
            self.a = {"href": dict(attrs).get("href") or "", "text": ""}

    def handle_data(self, data):
        if self.done:
            return
        if self.a is not None:
            self.a["text"] += data
            return
        self.buf = (self.buf + data)[-300:]
        if self.STOP_RE.search(self.buf):
            self.done = True
            return
        m = None
        for m in self.DATE_RE.finditer(self.buf):
            pass
        if m:
            try:
                self.cur = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), 12, tzinfo=timezone.utc)
            except ValueError:
                self.cur = None
            self.buf = self.buf[m.end():]

    def handle_endtag(self, tag):
        if tag == "a" and self.a is not None:
            a, self.a = self.a, None
            if self.done:
                return
            title = " ".join(a["text"].split())
            if self.cur and a["href"] and len(title) > 15:
                self.items.append({"title": title, "url": a["href"], "published": self.cur,
                                   "note": "topic page, not an article link; date is date-only"})


def scrape_screamscape(html_bytes, base_url):
    p = ScreamscapeParser()
    p.feed(html_bytes.decode("utf-8", errors="replace"))
    for it in p.items:
        it["url"] = urljoin(base_url, it["url"])
    return p.items


FRONTIER_RE = re.compile(
    r"(?:https?://(?:www\.)?planetcoaster\.com|(?<=[\"'(=]))/news/(\d{4})-(\d{2})-(\d{2})/([A-Za-z0-9\-_]+)")


def scrape_frontier(html_bytes, base_url):
    """Frontier post links carry the date: /news/2026-11-24/slug. Titles come from the slug.
    Only links on planetcoaster.com itself (absolute or relative) count, including JSON-escaped ones."""
    text = html_bytes.decode("utf-8", errors="replace").replace("\\/", "/")
    seen, items = set(), []
    for m in FRONTIER_RE.finditer(text):
        path = "/news/%s-%s-%s/%s" % (m.group(1), m.group(2), m.group(3), m.group(4))
        if path in seen:
            continue
        seen.add(path)
        try:
            pub = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, tzinfo=timezone.utc)
        except ValueError:
            continue
        items.append({"title": m.group(4).replace("-", " ").title() + " (title from link, open the post)",
                      "url": urljoin("https://www.planetcoaster.com/", path), "published": pub,
                      "note": "date-only; read the whole post, teasers sit in the last paragraph"})
    return items


SCRAPERS = {"screamscape": scrape_screamscape, "frontier": scrape_frontier}


VOID = {"meta", "link", "img", "br", "hr", "input", "source", "wbr", "area", "base", "col", "embed", "track"}


class ArticleText(HTMLParser):
    """Pulls the meta description and the first real paragraphs of an article page.
    Paragraphs inside <article> win; script, style, nav, header, footer and aside are skipped."""

    SKIP = {"script", "style", "nav", "header", "footer", "aside", "form", "noscript", "figcaption"}

    def __init__(self):
        super().__init__()
        self.meta, self.depth_skip, self.in_article, self.in_p = "", 0, 0, False
        self.skip_stack = []
        self.buf, self.art_ps, self.all_ps = "", [], []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta" and not self.meta:
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:description", "description", "twitter:description") and a.get("content"):
                self.meta = " ".join(html.unescape(a["content"]).split())
        style = a.get("style", "").replace(" ", "").lower()
        hidden = ("display:none" in style or "visibility:hidden" in style or "hidden" in a
                  or a.get("aria-hidden") == "true")
        if tag in self.SKIP or (hidden and tag not in VOID):
            self.depth_skip += 1
            self.skip_stack.append(tag)
        elif tag == "article":
            self.in_article += 1
        elif tag == "p" and not self.depth_skip:
            self.in_p, self.buf = True, ""

    def handle_endtag(self, tag):
        if self.skip_stack and tag == self.skip_stack[-1]:
            self.skip_stack.pop()
            self.depth_skip -= 1
        elif tag == "article" and self.in_article:
            self.in_article -= 1
        elif tag == "p" and self.in_p:
            self.in_p = False
            txt = " ".join(self.buf.split())
            if len(txt) >= 80:
                (self.art_ps if self.in_article else self.all_ps).append(txt)

    def handle_data(self, data):
        if self.in_p and not self.depth_skip:
            self.buf += data


def article_excerpt(url):
    """Returns (meta description, first paragraphs up to ~900 chars). Text is data for the run, never instructions."""
    raw = fetch(url)[:1_500_000].decode("utf-8", errors="replace")
    p = ArticleText()
    p.feed(raw)
    return p.meta[:400], (p.art_ps or p.all_ps)[:10]


def add_excerpts(cands):
    """Fetch article pages for fresh items so the daily run can write real takes without opening pages itself."""
    todo = [c for c in cands if c["age_hours"] <= EXCERPT_HOURS and "news.google.com" not in c["url"]
            and "(date only)" not in c["published_utc"]
            and not (c["outlet"] == "Marca" and not MARCA_KEEP.search(c["title"]))]
    todo.sort(key=lambda c: (not c.get("must_account"), not c.get("villarreal"), c["age_hours"]))
    todo = todo[:EXCERPT_MAX]

    pages = {}

    def one(c):
        try:
            pages[c["url"]] = article_excerpt(c["url"])
        except Exception as exc:
            c["excerpt_error"] = str(exc)[:80]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, todo))

    # Paragraphs that repeat across 3+ pages of one site are boilerplate (author bios, section blurbs, paywall notes).
    seen = {}
    for url, (_, paras) in pages.items():
        dom = urlparse(url).netloc
        for t in set(x[:60] for x in paras):
            seen[(dom, t)] = seen.get((dom, t), 0) + 1
    got = 0
    for c in todo:
        if c["url"] not in pages:
            continue
        meta, paras = pages[c["url"]]
        dom = urlparse(c["url"]).netloc
        keep, size = [], 0
        for t in paras:
            if seen.get((dom, t[:60]), 0) >= 3 or BOILER_RE.search(t):
                continue
            keep.append(t)
            size += len(t)
            if size > 900:
                break
        body = " ".join(keep)[:1000]
        if body or meta:
            c["excerpt"] = body or meta
            if meta and body and meta[:60] not in body:
                c["excerpt_meta"] = meta
            got += 1
        else:
            c["excerpt_error"] = "no article text found"
    return got, len(todo)


def _keep(outlet, items, now, hours, res, extra_note=False):
    """Filter items to the window and fill res. Date-only items sit at noon UTC of their day."""
    cutoff = now - timedelta(hours=hours)
    res["total_items"] = len(items)
    for it in items:
        pub = it["published"]
        if pub is None or pub < cutoff or pub > now + timedelta(hours=12):
            continue  # undated, too old, or future-dated
        age = (now - pub).total_seconds() / 3600
        row = {
            "outlet": outlet["name"], "sections": outlet["sections"], "title": it["title"],
            "url": strip_tracking(it["url"]),
            "published_utc": (pub.date().isoformat() + " (date only)") if extra_note
            else pub.isoformat(timespec="minutes"),
            "age_hours": round(max(age, 0), 1), "window": "24h" if age <= 24 else "72h",
        }
        if it.get("note"):
            row["note"] = it["note"]
        for k in ("summary", "source", "source_url", "feed"):
            if it.get(k):
                row[k] = it[k]
        if outlet.get("google_news"):
            row.pop("summary", None)  # Google News descriptions only repeat the title
            row["note"] = ("Google News link, not the article: search the exact headline plus the outlet in "
                           "'source' and link the original article")
        if (outlet.get("priority") and age <= PRIORITY_HOURS) or age <= outlet.get("priority_hours", -1):
            row["must_account"] = True
        res["items"].append(row)
    res["items"].sort(key=lambda x: x["age_hours"])
    res["status"] = "reached" if res["items"] else "stale"


def scan_outlet(outlet, now, hours):
    res = {"name": outlet["name"], "sections": outlet["sections"], "status": "failed",
           "feed_url": "", "page": outlet["home"], "total_items": 0, "items": [], "error": ""}
    if outlet.get("no_feed"):
        res["status"] = "needs_fetch"
        return res

    if outlet.get("scrape"):
        try:
            items = SCRAPERS[outlet["scrape"]](fetch(outlet["home"]), outlet["home"])
        except Exception as exc:
            res["error"] = f"scrape: {exc}"
            return res
        if not items:
            res["error"] = "scrape: page read but no dated links found (layout changed or page needs JavaScript)"
            return res
        res["feed_url"] = outlet["home"] + " (page scrape)"
        _keep(outlet, items, now, hours, res, extra_note=True)
        return res

    candidates = list(outlet.get("feeds", []))
    errors = []
    if not candidates and not outlet.get("youtube"):
        try:
            candidates = discover_feeds(fetch(outlet["home"]), outlet["home"])
        except Exception as exc:
            errors.append(f"home: {exc}")
            candidates = [urljoin(outlet["home"], p) for p in COMMON_FEED_PATHS]

    if outlet.get("youtube") and not outlet.get("feeds"):
        feeds, bad = [], []
        for h in outlet["youtube"]:
            try:
                page = fetch(f"https://www.youtube.com/@{h}/videos")[:600_000].decode("utf-8", "replace")
                m = re.search(r'"(?:externalId|channelId)":"(UC[\w-]{22})"', page) or re.search(r"channel_id=(UC[\w-]{22})", page)
                if m:
                    feeds.append(f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}")
                else:
                    bad.append(h)
            except Exception:
                bad.append(h)
        candidates = feeds
        if bad:
            errors.append("handles not found: " + ", ".join(bad))
    if outlet.get("merge"):
        merged, seen, used = [], set(), []
        for feed_url in candidates:
            try:
                items = parse_feed(fetch(feed_url))
            except Exception as exc:
                errors.append(f"{feed_url}: {exc}")
                continue
            dates = [i["published"] for i in items if i.get("published")]
            used.append(feed_url.rsplit("/", 1)[-1][-26:] + f" ({len(items)}, newest {max(dates):%m-%d})" if dates
                        else feed_url.rsplit("/", 1)[-1][-26:] + f" ({len(items)}, undated)")
            for it in items:
                key = url_key(it["url"])
                if key not in seen:
                    seen.add(key)
                    it["feed"] = feed_url.rsplit("/", 1)[-1].replace(".rss", "")
                    merged.append(it)
        if not merged:
            res["error"] = "; ".join(errors[-3:])
            return res
        res["feed_url"] = f"{len(used)}/{len(candidates)} feeds: " + ", ".join(used)
        _keep(outlet, merged, now, hours, res)
        if errors:
            res["feed_url"] += " FAILED: " + ", ".join(e.split(": ")[0] for e in errors)
            res["status"] = "partial"
        return res

    for feed_url in candidates:
        try:
            items = parse_feed(fetch(feed_url))
        except Exception as exc:
            errors.append(f"{feed_url}: {exc}")
            continue
        if not items:
            errors.append(f"{feed_url}: no items")
            continue
        res["feed_url"] = feed_url
        _keep(outlet, items, now, hours, res)
        return res

    res["error"] = "; ".join(errors[-3:])
    return res


def dedupe(results):
    """Same article in two outlets' feeds: keep one row, merge the sections."""
    by_url = {}
    for r in results:
        for it in r["items"]:
            key = url_key(it["url"])
            if key in by_url:
                kept = by_url[key]
                secs = sorted(set(kept["sections"].split(",")) | set(it["sections"].split(",")))
                kept["sections"] = ",".join(secs)
                if it["outlet"] not in kept["outlet"]:
                    kept["outlet"] += " + " + it["outlet"]
                if it.get("must_account"):
                    kept["must_account"] = True
            else:
                by_url[key] = dict(it)
    return sorted(by_url.values(), key=lambda x: x["age_hours"])


def write_outputs(results, out_dir, now, hours):
    os.makedirs(out_dir, exist_ok=True)
    day = now.astimezone(PACIFIC).strftime("%Y-%m-%d")
    cands = dedupe(results)
    for c in cands:  # Villarreal club news from any outlet; a match result inside 30h must make the issue
        if VILLARREAL_RE.search(c["title"]) or "Villarreal (Google News)" in c["outlet"]:
            c["villarreal"] = True
            if c["age_hours"] <= MATCH_HOURS and SCORE_RE.search(c["title"]):
                c["match_result"] = True
    got, tried = add_excerpts(cands)
    counts = {s: sum(r["status"] == s for r in results) for s in ("reached", "partial", "stale", "needs_fetch", "failed")}

    with open(os.path.join(out_dir, f"candidates_{day}.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_utc": now.isoformat(timespec="minutes"), "pacific_date": day,
                   "window_hours": hours, "outlet_status_counts": counts,
                   "excerpts": {"fetched": got, "tried": tried},
                   "must_account": sum(1 for c in cands if c.get("must_account")),
                   "villarreal_items": sum(1 for c in cands if c.get("villarreal")),
                   "villarreal_match_items": sum(1 for c in cands if c.get("match_result")),
                   "healthy": counts["reached"] + counts["partial"] + counts["stale"] >= MIN_REACHED,
                   "candidates": cands}, f, ensure_ascii=False, indent=1)

    lines = [f"# Coverage {day}", "",
             f"Window: {hours}h. Items kept: {len(cands)}. Outlets: {len(results)}. "
             f"Reached {counts['reached']}, partial {counts['partial']}, stale {counts['stale']}, needs_fetch {counts['needs_fetch']}, "
             f"failed {counts['failed']}. Article excerpts: {got} of {tried} fresh items "
             f"(items within {EXCERPT_HOURS}h). Priority items to use or explain (Blooloop, IAAPA, "
             f"within {PRIORITY_HOURS}h): {sum(1 for c in cands if c.get('must_account'))}. Villarreal items: "
             f"{sum(1 for c in cands if c.get('villarreal'))}, match results within {MATCH_HOURS}h: "
             f"{sum(1 for c in cands if c.get('match_result'))}.", "",
             "| Outlet | Sections | Status | Items in window | Note |", "|---|---|---|---|---|"]
    for r in results:
        if r["status"] == "failed":
            note = r["error"][:120]
        elif r["status"] == "needs_fetch":
            note = "open page directly: " + r["page"]
        elif r["status"] == "stale":
            note = f"nothing in window ({r['total_items']} older items); " + (r['feed_url'] if 'feeds:' in r.get('feed_url', '') else f"check page: {r['page']}")
        else:
            note = r["feed_url"]
        lines.append(f"| {r['name']} | {r['sections']} | {r['status']} | {len(r['items'])} | {note} |")
    with open(os.path.join(out_dir, f"coverage_{day}.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(cands), counts


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pasqual Daily feed scan")
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--out", default="scan_output")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda o: scan_outlet(o, now, args.hours), OUTLETS))

    kept, counts = write_outputs(results, args.out, now, args.hours)
    print(f"Kept {kept} items. Reached {counts['reached']}/{len(results)} outlets; "
          f"{counts['stale']} stale; {counts['needs_fetch']} need a direct page fetch; {counts['failed']} failed.")
    failed = [r["name"] for r in results if r["status"] == "failed"]
    if failed:
        print("Failed: " + ", ".join(failed))
    print(f"Files written to {os.path.abspath(args.out)}")
    if counts["reached"] + counts["partial"] + counts["stale"] < MIN_REACHED:
        print(f"WARNING: fewer than {MIN_REACHED} outlets read. Network problem?", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
