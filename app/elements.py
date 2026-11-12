"""ELEMENTS: the course plan HTML that elements.com/kursplan/ loads from admin-ajax (~10 days ahead)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

from .fetcher import build_headers
from .models import CourseEvent

BERLIN = ZoneInfo("Europe/Berlin")
PAGE = "https://www.elements.com/kursplan/"
AJAX = "https://www.elements.com/wp/wp-admin/admin-ajax.php"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Slug (option value on PAGE minus "elements-") -> display name.
STUDIOS = {
    "balanstrasse": "München - Balanstraße",
    "donnersbergerbruecke": "München - Donnersbergerbrücke",
    "siemensallee": "München - Siemensallee",
    "paulinenbruecke": "Stuttgart - Paulinenbrücke",
    "eschborn": "Eschborn",
    "eschenheimer-turm": "Frankfurt - Eschenheimer Turm",
    "henninger-turm": "Frankfurt - Henninger Turm",
}

_nonce: Optional[str] = None  # WordPress nonce from PAGE; the same for every anonymous visitor, valid 12-24h


async def fetch_studio(client: httpx.AsyncClient, studio: str) -> List[CourseEvent]:
    global _nonce
    for _ in range(2):
        if not _nonce:
            page = await client.get(PAGE, headers=build_headers(None), timeout=TIMEOUT)
            page.raise_for_status()
            _nonce = re.search(r'elements_ajax = \{"nonce":"(\w+)"', page.text).group(1)
        r = await client.post(AJAX, headers=build_headers(None), timeout=TIMEOUT,
                              data={"action": "get_courseplan", "studio": f"elements-{studio}", "nonce": _nonce})
        if r.status_code != 403:
            break
        _nonce = None  # expired, fetch a fresh one
    r.raise_for_status()
    return parse_courseplan(r.text, f"ELEMENTS {STUDIOS[studio]}")


def parse_courseplan(html: str, location: str) -> List[CourseEvent]:
    events = []
    for day in BeautifulSoup(html, "html.parser").select(".accordion"):
        date = day.select_one("[data-day]")["data-day"]  # 12.11.2026
        for tr in day.select("tr[data-course-name]"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
            time, minutes, trainer, room = cells[0], cells[2], cells[3], cells[4]  # 08:00 | name | 60 min | ...
            start = datetime.strptime(f"{date} {time}", "%d.%m.%Y %H:%M").replace(tzinfo=BERLIN)
            course_id = tr.select_one("input[name=course_id]")
            events.append(CourseEvent(
                id=course_id["value"] if course_id else tr["data-course-name"],
                title=tr["data-course-name"],
                start=start,
                end=start + timedelta(minutes=int(re.search(r"\d+", minutes).group())),
                location=location,
                instructor=trainer or None,
                room=room or None,
                description=tr.get("data-course-description", "").strip() or None,
                url=PAGE,
                booking=tr.form is not None,  # sign-up or waiting list form
            ))
    return sorted(events, key=lambda e: (e.start, e.title))
