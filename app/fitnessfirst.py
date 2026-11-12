"""Fitness First: the JSON behind fitnessfirst.de/kurse/kursplan (Magicline). One request per day."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta
from typing import Dict, List

import httpx
from zoneinfo import ZoneInfo

from .fetcher import build_headers
from .models import CourseEvent

BERLIN = ZoneInfo("Europe/Berlin")
BASE = "https://www.fitnessfirst.de"
DAYS_AHEAD = 7  # the API serves nothing past today + 6
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


async def fetch_clubs(client: httpx.AsyncClient) -> Dict[int, str]:
    """Club id -> name, from the course plan page's drupalSettings."""
    r = await client.get(f"{BASE}/kurse/kursplan", headers=build_headers(None), timeout=TIMEOUT)
    r.raise_for_status()
    settings = re.search(r'data-drupal-selector="drupal-settings-json">(.*?)</script>', r.text, re.S)
    return {int(c["id"]): c["title"] for c in json.loads(settings.group(1))["courseSchedule"]["allClubsData"]}


async def fetch_club(client: httpx.AsyncClient, club: int) -> List[CourseEvent]:
    async def day(d: str) -> list:
        r = await client.get(f"{BASE}/magicline/api/course-plan/{club}", headers=build_headers(None),
                             params={"selectedDate": d, "limit": 500, "page": 1}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()["classes"].get(d, {}).get("items", [])

    today = date.today()
    days = await asyncio.gather(*(day((today + timedelta(days=i)).isoformat()) for i in range(DAYS_AHEAD)))
    return parse_items([it for items in days for it in items])


def parse_items(items: list) -> List[CourseEvent]:
    events = []
    for it in items:
        start = datetime.fromisoformat(it["timestampStart"]).astimezone(BERLIN)
        h, m = map(int, it["time"]["to"].split(":"))
        events.append(CourseEvent(
            id=str(it["id"]),
            title=it["title"],
            start=start,
            end=start.replace(hour=h, minute=m),
            location=f"Fitness First {it['club']}" if it.get("club") else None,
            category=it.get("categoryDisplayOnly") or it.get("category"),
            level=(it.get("levelInfo") or {}).get("label") or it.get("level"),
            description=(it.get("description") or "").strip() or None,
            url=BASE + it["url"] if it.get("url") else None,
            booking=True,  # "Um an einem Kurs teilnehmen zu können, musst du diesen vorab ... buchen."
        ))
    return sorted(events, key=lambda e: (e.start, e.title))
