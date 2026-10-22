from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import List

import httpx
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

from .fetcher import build_headers
from .models import CourseEvent

BERLIN = ZoneInfo("Europe/Berlin")
MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["januar", "februar", "märz", "april", "mai", "juni",
         "juli", "august", "september", "oktober", "november", "dezember"]
    )
}


def center_url(center: str) -> str:
    return f"https://www.bodyandsoul.de/standorte/{center}/kursangebot-center-{center}"


async def fetch_center(client: httpx.AsyncClient, center: str) -> List[CourseEvent]:
    resp = await client.get(
        center_url(center), headers=build_headers(None), timeout=httpx.Timeout(20.0, connect=10.0)
    )
    resp.raise_for_status()
    events = parse_courseplan(resp.text)
    if not events:
        # Don't overwrite a good cache with an empty calendar (e.g. site redesign/outage page).
        raise ValueError(f"No body+soul courses found for {center}")
    return events


def _week_monday(label: str) -> date:
    # Labels: "22. - 28 Juni 2026", "29. Juni - 05. Juli 2026". Last date is the Sunday.
    day, month, year = re.findall(r"(\d{1,2})\.?\s*([^\W\d_]+)\s+(\d{4})", label)[-1]
    return date(int(year), MONTHS[month.lower()], int(day)) - timedelta(days=6)


def parse_courseplan(html: str) -> List[CourseEvent]:
    soup = BeautifulSoup(html, "html.parser")
    uniq: dict[tuple, CourseEvent] = {}
    for slide in soup.select("#courseplaner .week-slide"):
        monday = _week_monday(slide.select_one(".week").get_text(" ", strip=True))
        for row in slide.select("tr"):
            cells = row.find_all("td")
            if len(cells) != 8:  # time column + Mon..Sun
                continue
            for i, cell in enumerate(cells[1:]):
                day = monday + timedelta(days=i)
                for a in cell.select("a.course-item"):
                    meta = [p.strip() for p in a.select_one(".meta").get_text(strip=True).split("|")]
                    hh, mm = map(int, meta[0].split(":"))
                    room = meta[1] if len(meta) > 1 else None
                    dur = re.search(r"\d+", meta[2]) if len(meta) > 2 else None
                    start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=BERLIN)
                    end = start + timedelta(minutes=int(dur.group()) if dur else 60)
                    title = a.select_one(".title").get_text(" ", strip=True)
                    ev = CourseEvent(
                        id=f"{title}-{room}",
                        title=title,
                        start=start,
                        end=end,
                        room=room,
                        description=a.get("href"),
                    )
                    uniq[(ev.id, start)] = ev
    return sorted(uniq.values(), key=lambda e: (e.start, e.title))
