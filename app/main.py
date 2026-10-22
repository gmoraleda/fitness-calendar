from __future__ import annotations

import asyncio
import html
import logging
import os
import signal
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Dict, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import HTMLResponse

from . import bodyandsoul
from .fetcher import fetch_course_page, fetch_fitx_studios, fetch_kursplan_html
from .ics import generate_ics
from .models import CourseEvent
from .parser import parse_schedule
from .store import ensure_data_dir, load_cache, save_cache


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fitx")


# Comma-separated FitX studio ids (value of the studio dropdown on fitx.de/fitnesskurse). 53 = München Sendling-Westpark.
FITX_STUDIOS = [
    int(s) for s in os.environ.get("FITX_STUDIOS", os.environ.get("FITX_COURSE_ID", "53")).split(",") if s.strip()
]
REFRESH_INTERVAL_SECONDS = int(os.environ.get("REFRESH_INTERVAL_SECONDS", "900"))
FITX_COOKIE = os.environ.get("FITX_COOKIE")
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN")
TZ = os.environ.get("TZ", "Europe/Berlin")
FITX_WEEKS_AHEAD = int(os.environ.get("FITX_WEEKS_AHEAD", "2"))
FITX_USE_KURSPLAN = os.environ.get("FITX_USE_KURSPLAN", "true").lower() in ("1", "true", "yes")
# Comma-separated body+soul center slugs, e.g. "theresienhoehe,sendling". Empty disables.
BODYSOUL_CENTERS = [c.strip() for c in os.environ.get("BODYSOUL_CENTERS", "theresienhoehe,sendling").split(",") if c.strip()]


try:
    os.environ.setdefault("TZ", TZ)
    import time as _time

    if hasattr(_time, "tzset"):
        _time.tzset()
except Exception:
    pass


app = FastAPI()
client: Optional[httpx.AsyncClient] = None
bg_task: Optional[asyncio.Task] = None


def _tz_berlin():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Europe/Berlin")
    except Exception:
        return None


def _fitx_fetcher(studio: int) -> Callable[[], Awaitable[List[CourseEvent]]]:
    return lambda: _fetch_fitx(studio)


async def _fetch_fitx(studio: int) -> List[CourseEvent]:
    assert client is not None
    if not FITX_USE_KURSPLAN:
        data, content_type = await fetch_course_page(client, studio, FITX_COOKIE)
        return parse_schedule(data, content_type)
    events: list[CourseEvent] = []
    now_berlin = datetime.now().astimezone(tz=_tz_berlin())
    monday = (now_berlin - timedelta(days=now_berlin.weekday())).date()
    for i in range(max(1, FITX_WEEKS_AHEAD)):
        data, content_type = await fetch_kursplan_html(
            client, studio, monday + timedelta(days=i * 7), FITX_COOKIE
        )
        events.extend(parse_schedule(data, content_type))
    return events


def _bodysoul_fetcher(center: str) -> Callable[[], Awaitable[List[CourseEvent]]]:
    async def fetch() -> List[CourseEvent]:
        assert client is not None
        return await bodyandsoul.fetch_center(client, center)

    return fetch


class Calendar:
    def __init__(self, title: str, uid_prefix: str, fetch):
        self.title = title
        self.uid_prefix = uid_prefix
        self.fetch = fetch
        self.events: List[CourseEvent] = []


CALENDARS: Dict[str, Calendar] = {
    **{
        f"fitx-{s}": Calendar(
            f"FitX {s}", f"fitx-{s}", _fitx_fetcher(s),
        )
        for s in FITX_STUDIOS
    },
    **{
        f"bodyandsoul-{c}": Calendar(
            f"body + soul {c.replace('-', ' ').title()}", f"bodyandsoul-{c}",
            _bodysoul_fetcher(c),
        )
        for c in BODYSOUL_CENTERS
    },
}


async def _refresh_one(name: str, cal: Calendar) -> None:
    try:
        events = await cal.fetch()
        uniq = {(e.id, int(e.start.timestamp()), int(e.end.timestamp())): e for e in events}
        events = sorted(uniq.values(), key=lambda e: (e.start, e.title))
        save_cache(name, events)
        cal.events = events  # single reference swap; no lock needed in asyncio
        logger.info("Refreshed %s: %d events", name, len(events))
    except Exception as e:
        logger.error("Refresh of %s failed, keeping last good data: %s", name, e)


async def _refresh_all() -> None:
    await asyncio.gather(*(_refresh_one(n, c) for n, c in CALENDARS.items()))


async def refresh_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        await _refresh_all()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=REFRESH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _name_fitx_studios() -> None:
    try:
        names = await fetch_fitx_studios(client)
    except Exception as e:
        logger.warning("Could not load FitX studio names: %s", e)
        return
    for s in FITX_STUDIOS:
        if s in names:
            CALENDARS[f"fitx-{s}"].title = f"FitX {names[s]}"


@app.on_event("startup")
async def on_startup() -> None:
    global client, bg_task
    ensure_data_dir()
    client = httpx.AsyncClient(http2=False, headers={})
    await _name_fitx_studios()
    for name, cal in CALENDARS.items():
        cal.events = load_cache(name) or []
        logger.info("Loaded %d cached events for %s", len(cal.events), name)
    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
    except (NotImplementedError, RuntimeError):
        pass
    bg_task = asyncio.create_task(refresh_loop(stop_event))


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if bg_task:
        bg_task.cancel()
        try:
            await bg_task
        except BaseException:
            pass
    if client:
        await client.aclose()


@app.get("/health")
async def health() -> Response:
    return Response(content="OK", media_type="text/plain")


def _ics_response(name: str, courses: List[str]) -> Response:
    cal = CALENDARS.get(name)
    if not cal:
        raise HTTPException(status_code=404, detail="Unknown calendar")
    events = cal.events
    if courses:
        wanted = set(courses)
        events = [e for e in events if e.title in wanted]
    return Response(
        content=generate_ics(cal.uid_prefix, events, cal.title),
        media_type="text/calendar; charset=utf-8",
    )


@app.get("/{name}.ics")
async def named_calendar(name: str, course: List[str] = Query(default=[])) -> Response:
    return _ics_response(name, course)


@app.post("/refresh")
async def refresh(x_refresh_token: Optional[str] = Header(default=None, alias="X-Refresh-Token")) -> dict:
    if REFRESH_TOKEN and x_refresh_token != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    await _refresh_all()
    return {"status": "ok", "events": {n: len(c.events) for n, c in CALENDARS.items()}}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    sections = []
    for name, cal in CALENDARS.items():
        titles = sorted({e.title for e in cal.events}, key=str.lower)
        boxes = "".join(
            f'<label><input type="checkbox" value="{html.escape(t)}"> {html.escape(t)}</label>' for t in titles
        ) or "<p class=muted>No courses loaded yet.</p>"
        sections.append(
            f'<section data-name="{html.escape(name)}"><h2>{html.escape(cal.title)}</h2>'
            f'<input class="q" type="search" placeholder="Filter courses…">'
            f'<div class="boxes">{boxes}</div>'
            f'<p>Subscribe: <a class="link"></a> <button type="button">Copy</button></p>'
            f'<p class="muted">Nothing ticked = all courses.</p></section>'
        )
    return PAGE.replace("{{SECTIONS}}", "".join(sections))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Course Calendars</title>
<style>
:root { color-scheme: light dark; --fg:#1d1d1f; --bg:#fff; --muted:#6e6e73; --line:#d2d2d7; --accent:#0071e3; }
@media (prefers-color-scheme: dark) { :root { --fg:#f5f5f7; --bg:#1c1c1e; --muted:#98989d; --line:#3a3a3c; --accent:#2997ff; } }
body { font: 16px/1.4 -apple-system, system-ui, sans-serif; color: var(--fg); background: var(--bg); max-width: 720px; margin: 0 auto; padding: 16px; }
section { border-top: 1px solid var(--line); padding: 8px 0 16px; }
.boxes { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 4px 16px; margin: 8px 0; }
label { display: flex; gap: 8px; align-items: center; padding: 6px 0; } label[hidden] { display: none; }
.q { width: 100%; box-sizing: border-box; padding: 8px; font: inherit; border: 1px solid var(--line); border-radius: 8px; background: transparent; color: inherit; }
a { color: var(--accent); word-break: break-all; }
button { font: inherit; padding: 4px 12px; }
.muted { color: var(--muted); font-size: 14px; }
</style></head><body>
<h1>Course Calendars</h1>
<p class="muted">Tick the courses you want, then tap the link to subscribe. Changing the ticks gives a new link.</p>
{{SECTIONS}}
<script>
document.querySelectorAll('section').forEach(s => {
  const link = s.querySelector('.link');
  const update = () => {
    const qs = [...s.querySelectorAll('.boxes input:checked')]
      .map(b => 'course=' + encodeURIComponent(b.value)).join('&');
    const url = 'webcal://' + location.host + '/' + s.dataset.name + '.ics' + (qs ? '?' + qs : '');
    link.href = link.textContent = url;
  };
  s.addEventListener('change', update);
  s.querySelector('.q').addEventListener('input', e => {
    const q = e.target.value.toLowerCase();
    s.querySelectorAll('.boxes label').forEach(l => l.hidden = !l.textContent.toLowerCase().includes(q));
  });
  s.querySelector('button').addEventListener('click', () => navigator.clipboard?.writeText(link.href));
  update();
});
</script></body></html>"""
