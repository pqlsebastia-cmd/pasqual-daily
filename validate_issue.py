#!/usr/bin/env python3
"""
Pasqual Daily - check picks.json and the built issue.html before anything is sent.
    python3 validate_issue.py issue.html picks.json scan_output/candidates_YYYY-MM-DD.json
Prints OK and exits 0, or prints one problem per line and exits 1.
"""
import html
import json
from datetime import datetime
import re
import sys
from urllib.parse import urlparse

from build_issue import number

EMOJI = {
    "A": "🏰 🏗️ 🎭 🎢 📄 📜 ⚙️", "B": "🎢 🌐 🌋 👻 🎃 🎟️ 🎡 🧱 🏗️ 📖 🐬 🐼 🐉 🛠️ 📜",
    "C": "🕵️ 📄 🧭", "D": "🎮 🕹️ 🏎️ 🧩", "E": "🍏 🤖 💻 🔒 🛰️ 🥽 ⚙️", "F": "⚽ 🏁 🛠️ 📝", "G": "🎬 📺 🎭 🧑‍💼",
}
REGIONS = {"Americas", "Europe", "Asia", "Middle East", "Global"}
FAMILIES = {"Disney", "Universal", "Competitors", "Miral", "None"}
CATEGORIES = {"Business", "Tech", "Deep Dive", "Rumor", "Gaming", "Sports", "Film-TV"}
READS = {"full", "excerpt", "snippet", "headline-only"}
FIRST_LIVE = "2026-11-26"   # issues before this were setup tests and never count as history
SKIP_REASONS = ("repeat:", "same story as", "not relevant:", "lower priority:", "duplicate of")
SCORE = re.compile(r"\b\d{1,2}\s?[-–]\s?\d{1,2}\b")
MAX = {"A": 10, "B": 15, "C": 10, "D": 5, "E": 7, "F": 7, "G": 7}


def check_records(stories, issue_date=None):
    E = []
    seen = set()
    for i, s in enumerate(stories, 1):
        who = f"story {i} ({(s.get('headline') or '?')[:40]})"
        for k in ("headline", "take", "outlet", "url", "emoji", "region", "family", "category"):
            if not isinstance(s.get(k), str) or not s.get(k).strip():
                E.append(f"{who}: {k} is missing or empty")
        sec = s.get("section")
        if sec not in EMOJI:
            E.append(f"{who}: bad section {sec}"); continue
        if s.get("emoji") not in EMOJI[sec].split():
            E.append(f"{who}: emoji {s.get('emoji')} not in section {sec} set")
        if s.get("region") not in REGIONS: E.append(f"{who}: bad region")
        if s.get("family") not in FAMILIES: E.append(f"{who}: bad family")
        if s.get("category") not in CATEGORIES: E.append(f"{who}: bad category")
        if s.get("read") not in READS: E.append(f"{who}: read must be full, snippet or headline-only")
        fl = s.get("flags") or ""
        if fl and fl != "Unconfirmed" and not fl.startswith("Update:"):
            E.append(f"{who}: flags must be empty, Unconfirmed or 'Update: ...'")
        if fl.startswith("Update:") and len(fl[7:].split()) > 12: E.append(f"{who}: update clause over 12 words")
        u = s.get("url") or ""
        p = urlparse(u)
        if "news.google.com" in p.netloc:
            E.append(f"{who}: Google News link; link the original article")
        if p.scheme not in ("http", "https") or p.path in ("", "/"):
            E.append(f"{who}: URL is not an article link")
        key = p.netloc + p.path.rstrip("/")
        if key in seen: E.append(f"{who}: duplicate URL in this issue")
        seen.add(key)
        if sec == "B" and s.get("family") == "Disney": E.append(f"{who}: #Disney in section B")
        if sec == "A" and s.get("family") not in ("Disney", "Miral"): E.append(f"{who}: A needs Disney or Miral")
        if sec == "C" and fl != "Unconfirmed": E.append(f"{who}: C items must be Unconfirmed")
        if sec in ("B", "C") and s.get("family") == "None": E.append(f"{who}: B/C need a family")
        if issue_date:
            try:
                pub = datetime.strptime(str(s.get("published", ""))[:10], "%Y-%m-%d").date()
                limit = 14 if s.get("category") == "Deep Dive" else 3
                if (issue_date - pub).days > limit:
                    E.append(f"{who}: published {pub}, older than {limit} days")
                if pub > issue_date:
                    E.append(f"{who}: published date is in the future")
            except ValueError:
                E.append(f"{who}: published must start with YYYY-MM-DD")
        if s.get("read") == "headline-only" and len((s.get("take") or "").split()) > 25:
            E.append(f"{who}: headline-only take is too long; it may only restate the headline")
    for k in MAX:
        n = sum(1 for s in stories if s.get("section") == k)
        if n > MAX[k]: E.append(f"section {k}: {n} stories, max {MAX[k]}")
    if sum(1 for s in stories if s.get("section") == "C" and s.get("family") == "Disney") > 3:
        E.append("section C: more than 3 Disney rumors")
    e_items = [s for s in stories if s.get("section") == "E"]
    if any(s["emoji"] == "🍏" for s in e_items) and e_items[0]["emoji"] != "🍏":
        E.append("section E: an Apple story exists but does not lead")
    return E


def check_html(h, stories):
    E = []
    low = h.lower()
    for t in ("<style", "class=", "<script", "<img", "<form"):
        if t in low: E.append("forbidden " + t)
    links = re.findall(r'<a href="([^"]+)" style="display:inline-block', h)
    want = [s.get("url") or "" for s in stories]
    if [html.unescape(l) for l in links] != want:
        E.append(f"links: {len(links)} in HTML, {len(want)} stories, or order differs")
    if h.count("Read on ") != len(stories): E.append("one chip per story check failed")
    nums = re.findall(r'width="46"[^>]*>(\d\d)<', h)
    if nums != ["%02d" % i for i in range(1, len(stories) + 1)]: E.append("numbering is not 01..N")
    for m in re.finditer(r"<(\w+)[^>]*background-color[^>]*>", h):
        if not (m[1] == "a" or (m[1] in ("td", "table") and "bgcolor=" in m[0])):
            E.append("color fill outside a td with bgcolor: " + m[0][:60])
    if h.count(">UNCONFIRMED<") != sum("Unconfirmed" in (s.get("flags") or "") for s in stories):
        E.append("UNCONFIRMED labels do not match flags")
    cards = h.split(">Coverage<")[0]
    if re.search(r">[^<]*https?://", cards): E.append("raw URL visible in a story card")
    if re.search(r"\{[A-Z_ ,]+\}", h): E.append("template placeholder left in HTML")
    if len(h.encode()) > 100000: E.append("HTML over 100KB, Gmail will clip it")
    return E


DISNEY_RE = re.compile(r"disney|imagineer|epcot|magic kingdom|hollywood studios|animal kingdom|\bdca\b|disney cruise|\bdcl\b", re.I)
RIVAL_RE = re.compile(r"universal|epic universe|six flags|seaworld|busch gardens|merlin|legoland|alton towers|cedar fair|dollywood|"
                      r"europa.?park|efteling|port ?aventura|puy du fou|chimelong|fantawild|miral|sea ?world|knott|hersheypark", re.I)
APPLE_RE = re.compile(r"apple|iphone|ipad|\bmac|macos|ios\b|airpods|vision pro|watchos|app store|tim cook", re.I)
DIGEST, COFFEE = set("ABCD"), set("EFG")


from datetime import date as _date
ISSUE_DAY = None


def check_placement(stories, cands):
    """Every story must sit in the right section: Disney only in A, no Disney-only story in B,
    Villarreal and Apple emoji only on those stories, and park outlets' items never land in Tech, F1 or Film."""
    E = []
    by_url = {norm(c["url"]): c for c in (cands or [])}
    for s in stories:
        sec, txt = s.get("section"), " ".join(str(s.get(k) or "") for k in ("headline", "take", "url"))
        who = f"story {s.get('n')} ({(s.get('headline') or '?')[:40]})"
        if sec == "A" and RIVAL_RE.search(txt) and not DISNEY_RE.search(txt) and s.get("family") != "Miral":
            E.append(f"{who}: names a competitor but no Disney park; move it to B")
        if sec == "B" and DISNEY_RE.search(s.get("headline") or "") and not RIVAL_RE.search(txt):
            E.append(f"{who}: a Disney story in B; move it to A or G")
        if s.get("emoji") == "⚽" and "villarreal" not in txt.lower():
            E.append(f"{who}: ⚽ is only for Villarreal stories")
        if s.get("emoji") == "🍏" and not APPLE_RE.search(txt):
            E.append(f"{who}: 🍏 is only for Apple stories")
        if sec != "F" and re.search(r"villarreal", s.get("headline") or "", re.I):
            E.append(f"{who}: Villarreal stories go in F")
        m = re.search(r"/(20\d\d)[/-](\d{1,2})[/-](\d{1,2})/", s.get("url") or "")
        if m and ISSUE_DAY and s.get("category") != "Deep Dive":
            try:
                age = (ISSUE_DAY - _date(int(m[1]), int(m[2]), int(m[3]))).days
                if age > 3:
                    E.append(f"{who}: the article URL is dated {m[1]}-{m[2]}-{m[3]}, {age} days old (limit 3 days unless Deep Dive)")
            except ValueError:
                pass
        c = by_url.get(norm(s.get("url")))
        if c:
            secs = set((c.get("sections") or "").replace(" ", "").split(","))
            if secs and secs <= DIGEST and sec in COFFEE and not DISNEY_RE.search(txt):
                E.append(f"{who}: a theme park outlet's item placed in {sec}")
            if secs and secs <= COFFEE and sec in DIGEST and sec != "A":
                E.append(f"{who}: a {','.join(sorted(secs))} outlet's item placed in {sec}")
    return E


def norm(u):
    p = urlparse(u or "")
    key = (p.netloc.replace("www.", "") + p.path).rstrip("/").lower()
    if "youtube.com" in p.netloc or "news.google.com" in p.netloc:
        key += "?" + p.query  # the query is the item id there
    return key


def check_scan(data, stories, cands, issue_date):
    """Nothing from the priority outlets may disappear silently, and Villarreal news must be covered."""
    E = []
    picked = {norm(s["url"]) for s in stories}
    skipped = {norm(x.get("url", "")): x for x in data.get("skipped", [])}
    for c in cands:
        if not c.get("must_account") and not (c.get("match_result") and norm(c["url"]) in skipped):
            continue
        k = norm(c["url"])
        if k in picked:
            continue
        x = skipped.get(k)
        if not x:
            E.append(f"scan item not used and not explained in skipped: {c['outlet']}: {c['title'][:60]}")
            continue
        r = (x.get("reason") or "").strip().lower()
        if not r.startswith(SKIP_REASONS) or len(r.split(":", 1)[-1].strip()) < 4:
            E.append("skipped reason must be 'repeat: <date> #<n>', 'same story as <headline>', 'not relevant: <why>', "
                     f"'lower priority: <why>' or 'duplicate of <headline>', with the detail filled in: {c['title'][:50]}")
        if r.startswith("repeat"):
            dates = re.findall(r"\d{4}-\d{2}-\d{2}", r)
            if not dates or any(d < FIRST_LIVE or (issue_date and d >= issue_date.isoformat()) for d in dates):
                E.append(f"repeat must name the earlier live issue date ({FIRST_LIVE} or later, before today): {c['title'][:50]}")
    vcf = [s for s in stories if s.get("section") == "F" and s.get("emoji") == "⚽"]
    has_vcf = bool(vcf)
    matches = [c for c in cands if c.get("match_result")]
    if matches:
        result_story = any(SCORE.search((s.get("headline") or "") + " " + (s.get("take") or "")) for s in vcf)
        repeat_ok = any((skipped.get(norm(c["url"]), {}).get("reason") or "").lower().startswith("repeat:") for c in matches)
        if not result_story and not repeat_ok:
            E.append("Villarreal played: the scan has a match result from the last 24h but no ⚽ story gives the score "
                     "(or list the match in skipped as 'repeat: <date> #<n>' if an earlier live issue ran it)")
    elif any(c.get("villarreal") and c["age_hours"] <= 30 for c in cands) and not has_vcf and not data.get("villarreal_note"):
        E.append("Villarreal news in the scan within 30h but no ⚽ story and no villarreal_note explaining why")
    by_url = {norm(c["url"]): c for c in cands}
    for s in stories:
        c = by_url.get(norm(s["url"]))
        if c and c.get("excerpt") and s.get("read") == "headline-only":
            E.append(f"story {s['n']}: the scan has an article excerpt; read must be excerpt or better, not headline-only")
    return E


if __name__ == "__main__":
    h = open(sys.argv[1], encoding="utf-8").read()
    data = json.load(open(sys.argv[2], encoding="utf-8"))
    stories = number([dict(s) for s in data["stories"]])
    try:
        issue_date = datetime.strptime(data["date"], "%A, %B %d, %Y").date()
    except (KeyError, ValueError):
        issue_date = None
    errors = check_records(stories, issue_date) + check_html(h, stories)
    cands = None
    if len(sys.argv) > 3:
        try:
            cands = json.load(open(sys.argv[3], encoding="utf-8")).get("candidates", [])
        except (OSError, ValueError) as exc:
            cands = None
            print(f"WARNING: scan file not readable ({exc}); scan checks skipped. Say so in the coverage notes.")
        if cands is not None:
            errors += check_scan(data, stories, cands, issue_date)
    ISSUE_DAY = issue_date
    errors += check_placement(stories, cands)
    if issue_date is None:
        errors.append('date must look like "Thursday, November 26, 2026"')
    if not data.get("coverage"):
        errors.append("coverage report is missing")
    if len(data.get("top3_urls", [])) != 3:
        errors.append("top3_urls needs exactly 3 story links")
    print("\n".join(errors) or "OK")
    sys.exit(1 if errors else 0)
