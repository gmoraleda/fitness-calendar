"""Anonymous subscription stats: which feeds calendar apps keep fetching, and with which courses.

No IPs are stored. Subscribe links carry a random `sub` id, so one subscription counts once
however many devices sync it. Older links without it are told apart by app + URL.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from collections import Counter
from typing import List, Optional

from .store import DATA_DIR, atomic_write

PATH = DATA_DIR / "stats.json"
ACTIVE = 14 * 24 * 3600  # calendar apps refresh at least weekly; quieter = unsubscribed
CLIENTS = [("Google", "Google Calendar"), ("Microsoft", "Outlook"), ("Outlook", "Outlook"),
           ("iOS", "Apple Calendar"), ("Mac OS", "Apple Calendar"), ("CalendarAgent", "Apple Calendar"),
           ("dataaccessd", "Apple Calendar")]

try:
    subs: dict = json.loads(PATH.read_text(encoding="utf-8"))
except Exception:
    subs = {}
_saved = 0.0


def record(calendar: str, courses: List[str], user_agent: str, sub: Optional[str], url: str) -> None:
    global _saved
    if not (sub and re.fullmatch(r"[a-z0-9]{4,16}", sub)):
        sub = hashlib.sha1(f"{user_agent} {url}".encode()).hexdigest()[:12]
    now = time.time()
    old = subs.get(sub)
    subs[sub] = {
        "calendar": calendar,
        "courses": sorted(c[:100] for c in courses[:50]),
        "client": next((name for k, name in CLIENTS if k in user_agent), "Other"),
        "first": old["first"] if old else now,
        "last": now,
    }
    if not old or now - _saved > 3600:  # spare the Pi's SD card; at most an hour of "last seen" is lost
        # ponytail: anyone can add keys via ?sub=; pruning bounds it to 14 days of traffic. Rate-limit if abused.
        for k in [k for k, s in subs.items() if now - s["last"] > ACTIVE]:
            del subs[k]
        atomic_write(PATH, json.dumps(subs, ensure_ascii=False).encode("utf-8"))
        _saved = now


def summary(now: Optional[float] = None) -> dict:
    now = now or time.time()
    active = [s for s in subs.values() if now - s["last"] < ACTIVE]
    return {
        "total": len(active),
        "calendars": Counter(s["calendar"] for s in active).most_common(),
        "courses": Counter((c, s["calendar"]) for s in active for c in s["courses"]).most_common(25),
        "all_courses": sum(1 for s in active if not s["courses"]),
        "clients": Counter(s["client"] for s in active).most_common(),
    }


def page() -> str:
    s = summary()
    courses = [(f"{c} · {cal}", n) for (c, cal), n in s["courses"]]
    if s["all_courses"]:
        courses.insert(0, ("Every course (no filter)", s["all_courses"]))

    def table(pairs: list) -> str:
        rows = "".join(f"<tr><td>{html.escape(str(k))}</td><td>{n}</td></tr>" for k, n in pairs)
        return f"<table>{rows or '<tr><td>No data yet</td><td></td></tr>'}</table>"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calendar Stats</title>
<style>
:root {{ color-scheme: light dark; --bg: #f9fafb; --fg: #030712; --muted: #565d6b; --border: #e5e7eb; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #030712; --fg: #f5f5f5; --muted: #9ca3af; --border: #1f2937; }} }}
body {{ margin: 0; font: 15px/1.6 -apple-system, system-ui, sans-serif; background: var(--bg); color: var(--fg); }}
main {{ max-width: 720px; margin: 0 auto; padding: 40px 16px 56px; }}
h1 {{ font-size: 64px; font-weight: 600; line-height: 1; margin: 0; }}
p {{ color: var(--muted); margin: 8px 0 0; }}
h2 {{ font-size: 12px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); margin: 36px 0 6px; }}
table {{ width: 100%; border-collapse: collapse; }}
td {{ padding: 7px 0; border-bottom: 1px solid var(--border); }}
td:last-child {{ text-align: right; padding-left: 16px; font-variant-numeric: tabular-nums; }}
</style></head><body><main>
<h1>{s["total"]}</h1>
<p>Active subscriptions: calendars that fetched a feed in the last 14 days.</p>
<h2>Studios</h2>{table(s["calendars"])}
<h2>Courses</h2>{table(courses)}
<h2>Apps</h2>{table(s["clients"])}
</main></body></html>"""
