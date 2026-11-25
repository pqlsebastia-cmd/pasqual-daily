#!/usr/bin/env python3
"""
Pasqual Daily - check picks.json and the built issue.html before anything is sent.
    python3 validate_issue.py issue.html picks.json
Prints OK and exits 0, or prints one problem per line and exits 1.
"""
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
READS = {"full", "snippet", "headline-only"}
MAX = {"A": 10, "B": 15, "C": 10, "D": 5, "E": 7, "F": 7, "G": 7}


def check_records(stories, issue_date=None):
    E = []
    seen = set()
    for i, s in enumerate(stories, 1):
        who = f"story {i} ({s.get('headline', '?')[:40]})"
        sec = s.get("section")
        if sec not in EMOJI:
            E.append(f"{who}: bad section {sec}"); continue
        if s.get("emoji") not in EMOJI[sec].split():
            E.append(f"{who}: emoji {s.get('emoji')} not in section {sec} set")
        if s.get("region") not in REGIONS: E.append(f"{who}: bad region")
        if s.get("family") not in FAMILIES: E.append(f"{who}: bad family")
        if s.get("category") not in CATEGORIES: E.append(f"{who}: bad category")
        if s.get("read") not in READS: E.append(f"{who}: read must be full, snippet or headline-only")
        fl = s.get("flags", "")
        if fl and fl != "Unconfirmed" and not fl.startswith("Update:"):
            E.append(f"{who}: flags must be empty, Unconfirmed or 'Update: ...'")
        if fl.startswith("Update:") and len(fl[7:].split()) > 12: E.append(f"{who}: update clause over 12 words")
        u = s.get("url", "")
        p = urlparse(u)
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
        if s.get("read") == "headline-only" and len(s.get("take", "").split()) > 25:
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
    links = re.findall(r'<a href="([^"]+)"', h)
    want = [s["url"] for s in stories]
    if [l.replace("&amp;", "&") for l in links] != want:
        E.append(f"links: {len(links)} in HTML, {len(want)} stories, or order differs")
    if h.count("Read on ") != len(stories): E.append("one chip per story check failed")
    nums = re.findall(r'width="46"[^>]*>(\d\d)<', h)
    if nums != ["%02d" % i for i in range(1, len(stories) + 1)]: E.append("numbering is not 01..N")
    for m in re.finditer(r"<(\w+)[^>]*background-color[^>]*>", h):
        if not (m[1] == "a" or (m[1] in ("td", "table") and "bgcolor=" in m[0])):
            E.append("color fill outside a td with bgcolor: " + m[0][:60])
    if h.count(">UNCONFIRMED<") != sum("Unconfirmed" in s.get("flags", "") for s in stories):
        E.append("UNCONFIRMED labels do not match flags")
    if re.search(r">[^<]*https?://", h): E.append("raw URL visible in text")
    if re.search(r"\{[A-Z_ ,]+\}", h): E.append("template placeholder left in HTML")
    if len(h.encode()) > 100000: E.append("HTML over 100KB, Gmail will clip it")
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
    if issue_date is None:
        errors.append('date must look like "Thursday, November 26, 2026"')
    if not data.get("coverage"):
        errors.append("coverage report is missing")
    if len(data.get("top3_urls", [])) != 3:
        errors.append("top3_urls needs exactly 3 story links")
    print("\n".join(errors) or "OK")
    sys.exit(1 if errors else 0)
