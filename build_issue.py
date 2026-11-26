#!/usr/bin/env python3
"""
Pasqual Daily - build the HTML email (and a plain-text copy) from story records.

The daily run never writes HTML by hand. It writes picks.json and runs:
    python3 build_issue.py picks.json issue.html issue.txt
    python3 validate_issue.py issue.html picks.json

picks.json:
{
  "date": "Thursday, November 26, 2026",
  "test": true,                                # adds [TEST] banner line under the masthead
  "top3_urls": ["https://...", "https://...", "https://..."],   # Today in 30 seconds (by URL)
  "coverage": [["Reached", "..."], ["Not reached", "..."], ["Notes", "..."]],
  "skipped": [{"url": "...", "title": "...", "reason": "repeat: 2026-11-27 #4"}],   # every must_account scan item not used
  "villarreal_note": "",                       # optional line under F, only when no Villarreal story runs and none qualified
  "stories": [
    {"section": "A", "region": "Americas", "family": "Disney", "emoji": "🏗️",
     "category": "Business", "headline": "...", "take": "...", "outlet": "Blooloop",
     "url": "https://...", "published": "2026-11-26", "flags": "", "read": "full"}
  ]
}
Story numbers are assigned here, after sorting A to G. Records keep their order inside a section.
"""
import html
import json
import math
import sys

SANS = "Helvetica,Arial,sans-serif"
SERIF = "Georgia,'Times New Roman',serif"
SECTIONS = {  # name, color, minimum
    "A": ("Disney Parks", "#0F6E78", 3), "B": ("Industry &amp; Competition", "#8E1F2F", 10),
    "C": ("Rumor Watch", "#6A3A63", 5), "D": ("Sim / Gaming", "#2E7158", 2),
    "E": ("Tech", "#2D3E4C", 2), "F": ("F1 / Villarreal", "#B0182F", 2), "G": ("Film / TV", "#9C7A2E", 2),
}
TAGGED = {"A": "family", "B": "both", "C": "both"}
T = 'role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"'
e = html.escape


def rule(height, color, extra=""):
    return (f'<table {T} style="{extra}"><tr><td height="{height}" bgcolor="{color}" '
            f'style="background-color:{color};font-size:0;line-height:0">&nbsp;</td></tr></table>')


def number(stories):
    order = {k: i for i, k in enumerate("ABCDEFG")}
    out = sorted(stories, key=lambda s: order[s["section"]])  # stable: keeps order inside a section
    for i, s in enumerate(out, 1):
        s["n"] = i
    return out


def tags(s):
    mode = TAGGED.get(s["section"])
    t = []
    if mode == "both":
        t.append("#" + (s.get("region") or "Global").replace(" ", ""))
    if mode in ("both", "family") and (s.get("family") or "None") not in ("None", ""):
        t.append("#" + s["family"])
    return t


def card(s):
    col = SECTIONS[s["section"]][1]
    meta = "&nbsp; ".join(f'<span style="color:{col};font-weight:700;">{e(x)}</span>' for x in tags(s))
    if "Unconfirmed" in (s.get("flags") or ""):
        meta += ("&nbsp; " if meta else "") + '<span style="color:#8E1F2F;font-weight:800;letter-spacing:0.5px;">UNCONFIRMED</span>'
    meta += ("&nbsp; " if meta else "") + e(s["emoji"])
    take = s["take"]
    if (s.get("flags") or "").startswith("Update:") and s["flags"][7:].strip() not in take:
        take = take.rstrip() + " " + s["flags"][7:].strip()
    return (
        f'<tr><td style="padding:18px 0;border-top:1px solid #E6E2DA;"><table {T}><tr>'
        f'<td valign="top" width="46" style="font-family:{SANS};font-size:24px;font-weight:300;color:#B8913A;line-height:26px;">{s["n"]:02d}</td>'
        f'<td valign="top"><div style="font-family:{SANS};font-size:12px;color:#5F676D;margin:0 0 6px 0;line-height:16px;">{meta}</div>'
        f'<div style="font-family:{SANS};font-size:18px;font-weight:800;color:#111417;line-height:24px;margin:0 0 6px 0;">{e(s["headline"])}</div>'
        f'<div style="font-family:{SANS};font-size:15px;color:#3E454B;line-height:22px;margin:0 0 10px 0;">{e(take)}</div>'
        f'<a href="{e(s["url"])}" style="display:inline-block;font-family:{SANS};font-size:12px;font-weight:700;color:#111417;'
        f'text-decoration:none;background-color:#F1EEE7;border:1px solid #E6E2DA;border-radius:999px;padding:6px 14px;">'
        f'Read on {e(s["outlet"])} &#8594;</a></td></tr></table></td></tr>')


def line(text, bold=False):
    color, weight = ("#8E1F2F", "700") if bold else ("#5F676D", "400")
    return (f'<tr><td style="padding:12px 0;border-top:1px solid #E6E2DA;font-family:{SANS};font-size:13px;'
            f'font-weight:{weight};color:{color};">{e(text)}</td></tr>')


def section(key, items, note_f=""):
    name, col, minimum = SECTIONS[key]
    rows = ""
    if key == "E" and not any(s["emoji"] == "🍏" for s in items):
        rows += line("Quiet Apple cycle; strongest non-Apple tech follows.")
    rows += "".join(card(s) for s in items)
    if key == "F" and note_f:
        rows += line(note_f)
    if len(items) < minimum:
        rows += line("Low News Today", bold=True)
    return (f'<tr><td style="padding:22px 16px 0 16px;">'
            f'<div style="font-family:{SANS};font-size:22px;font-weight:800;color:#111417;">'
            f'<span style="color:{col};font-size:16px;margin-right:8px;">&#9632;</span>{name}'
            f'<span style="font-size:13px;font-weight:600;color:#5F676D;">&nbsp; {len(items)}</span></div>'
            f'<table {T} style="margin-top:6px;">{rows}</table></td></tr>')


def part(title):
    return (f'<tr><td style="padding:40px 16px 4px 16px;"><div style="font-family:{SANS};font-size:13px;font-weight:800;'
            f'letter-spacing:2px;color:#0F6E78;text-transform:uppercase;">{title}</div>{rule(2, "#111417", "margin-top:10px")}</td></tr>')


def skipped_rows(data):
    """Priority items (Blooloop, IAAPA) the run did not use, with its reason, for the plain-text footer."""
    sk = data.get("skipped") or []
    if not sk:
        return []
    return [["Not used from Blooloop, IAAPA and gaming sources", "; ".join(f'{(x.get("title") or "")[:70]} ({x.get("reason") or ""}) {x.get("url") or ""}' for x in sk)]]


def skipped_html(data):
    """Same list in the email, each headline linked, so the Blooloop newsletter is fully replaced."""
    sk = data.get("skipped") or []
    if not sk:
        return ""
    items = "".join(f'<li style="margin:0 0 4px 0;"><a href="{e(x.get("url") or "")}" style="color:#3E454B;">{e((x.get("title") or "")[:90])}</a> '
                    f'<span style="color:#8A9197;">({e(x.get("reason") or "")})</span></li>' for x in sk)
    return (f'<p style="margin:10px 0 4px 0;"><b style="color:#111417;">Also from Blooloop, IAAPA and gaming sources, not in today\'s picks:</b></p>'
            f'<ul style="margin:0;padding-left:18px;">{items}</ul>')


def build(data):
    stories = number([dict(s) for s in data["stories"]])
    by = {k: [s for s in stories if s["section"] == k] for k in SECTIONS}
    words = sum(len((s["headline"] + " " + s["take"]).split()) for s in stories)
    mins = max(3, math.ceil(words / 200))
    url_to = {s["url"]: s for s in stories}
    top = [url_to[u] for u in data.get("top3_urls", []) if u in url_to][:3]
    brief = "".join(
        f'<tr><td valign="top" width="30" style="font-family:{SANS};font-size:13px;font-weight:800;color:#B8913A;padding:4px 0;">{s["n"]:02d}</td>'
        f'<td style="font-family:{SANS};font-size:14px;color:#111417;padding:4px 0;line-height:20px;">{e(s["headline"])}</td></tr>' for s in top)
    test = (f'<tr><td style="padding:10px 16px 0 16px;font-family:{SANS};font-size:12px;font-weight:700;color:#8E1F2F;">'
            f'TEST ISSUE: not logged to Notion.</td></tr>') if data.get("test") else ""
    cov_rows = list(data.get("coverage") or []) + skipped_rows(data)
    cov = "".join(f'<p style="margin:0 0 8px 0;"><b style="color:#111417;">{e(str(a))}:</b> {e(str(b))}</p>' for a, b in (data.get("coverage") or [])) + skipped_html(data)
    counts = " · ".join(f"{k} {len(by[k])}" for k in SECTIONS)
    body = (
        f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Pasqual Daily - {e(data["date"])}</title></head>'
        f'<body style="margin:0;padding:0;background:#EFECE5;"><table {T} bgcolor="#EFECE5" style="background-color:#EFECE5;"><tr><td align="center" style="padding:0;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640" bgcolor="#FBFAF7" style="width:100%;max-width:640px;background-color:#FBFAF7;">'
        f'<tr><td style="padding:30px 16px 0 16px;"><table {T}><tr>'
        f'<td style="font-family:{SERIF};font-size:30px;font-weight:700;color:#111417;">Pasqual Daily</td>'
        f'<td align="right" style="font-family:{SANS};font-size:12px;color:#5F676D;">{e(data["date"])}<br>{len(stories)} stories &middot; {mins} min read</td>'
        f'</tr></table><table {T} style="margin-top:14px"><tr>'
        f'<td width="60%" height="4" bgcolor="#0F6E78" style="background-color:#0F6E78;font-size:0;line-height:0">&nbsp;</td>'
        f'<td width="25%" bgcolor="#B8913A" style="background-color:#B8913A;font-size:0;line-height:0">&nbsp;</td>'
        f'<td width="15%" bgcolor="#8E1F2F" style="background-color:#8E1F2F;font-size:0;line-height:0">&nbsp;</td></tr></table></td></tr>'
        f'{test}'
        f'<tr><td style="padding:22px 16px 0 16px;"><table {T}><tr><td bgcolor="#F1EEE7" style="background-color:#F1EEE7;border-radius:12px;padding:16px 18px;">'
        f'<div style="font-family:{SANS};font-size:11px;font-weight:800;letter-spacing:2px;color:#0F6E78;text-transform:uppercase;margin-bottom:6px;">Today in 30 seconds</div>'
        f'<table {T}>{brief}</table></td></tr></table></td></tr>'
        + part("🎢 Daily Themed Industry Digest") + "".join(section(k, by[k]) for k in "ABCD")
        + part("☕ Pasqual Daily Coffee") + "".join(section(k, by[k], data.get("villarreal_note", "")) for k in "EFG")
        + f'<tr><td style="padding:40px 16px 30px 16px;">{rule(2, "#111417", "margin-bottom:14px")}'
        f'<div style="font-family:{SANS};font-size:11px;font-weight:800;letter-spacing:2px;color:#0F6E78;text-transform:uppercase;margin-bottom:8px;">Coverage</div>'
        f'<div style="font-family:{SANS};font-size:12px;line-height:18px;color:#5F676D;">'
        f'<p style="margin:0 0 8px 0;"><b style="color:#111417;">Stories per section:</b> {counts}</p>{cov}'
        f'<div style="border-top:1px solid #E6E2DA;margin-top:10px;padding-top:10px;color:#111417;">'
        f'Save stories in your Newsletter Inbox chat, for example "save 3, 7".</div></div></td></tr>'
        f'</table></td></tr></table></body></html>')
    text = [f"PASQUAL DAILY - {data['date']}" + (" (TEST)" if data.get("test") else ""), ""]
    for k in SECTIONS:
        text.append(f"{k} - {html.unescape(SECTIONS[k][0]).upper()} ({len(by[k])})")
        for s in by[k]:
            fl = f" [{s['flags']}]" if s.get("flags") else ""
            text += [f"{s['n']:02d}. {s['headline']}{fl}", f"    {s['take']}", f"    {s['outlet']}: {s['url']}"]
        text.append("")
    text += [f"{a}: {b}" for a, b in cov_rows]
    return body, "\n".join(text), stories


if __name__ == "__main__":
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    h, t, numbered = build(data)
    open(sys.argv[2], "w", encoding="utf-8").write(h)
    if len(sys.argv) > 3:
        open(sys.argv[3], "w", encoding="utf-8").write(t)
    json.dump(numbered, open(sys.argv[1].replace(".json", "_numbered.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"built {len(numbered)} stories, {len(h.encode())} bytes")
