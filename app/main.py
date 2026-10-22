from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse

from . import bodyandsoul
from .fetcher import fetch_course_page, fetch_fitx_studios, fetch_kursplan_html
from .ics import generate_ics
from .models import CourseEvent
from .parser import parse_schedule
from .store import cache_mtime, ensure_data_dir, load_cache, save_cache


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fitness-calendar")


REFRESH_INTERVAL_SECONDS = int(os.environ.get("REFRESH_INTERVAL_SECONDS", "900"))
FITX_COOKIE = os.environ.get("FITX_COOKIE")
TZ = os.environ.get("TZ", "Europe/Berlin")
FITX_WEEKS_AHEAD = int(os.environ.get("FITX_WEEKS_AHEAD", "2"))
FITX_USE_KURSPLAN = os.environ.get("FITX_USE_KURSPLAN", "true").lower() in ("1", "true", "yes")
STUDIO_LIST_TTL = 24 * 3600

# Slug -> display name. Slugs as in bodyandsoul.de/standorte/<slug>/kursangebot-center-<slug>.
BODYSOUL_CENTERS = {
    "brunnthal": "Brunnthal",
    "englischer-garten": "Englischer Garten",
    "erding": "Erding",
    "hirschgarten": "Hirschgarten",
    "muenchen-mitte": "München Mitte",
    "muenchen-nord": "München Nord",
    "sendling": "Sendling",
    "theresienhoehe": "Theresienhöhe",
    "trudering": "Trudering",
    "werk12": "WERK12",
}

try:
    os.environ.setdefault("TZ", TZ)
    if hasattr(time, "tzset"):
        time.tzset()
except Exception:
    pass


app = FastAPI()
client: Optional[httpx.AsyncClient] = None
fitx_studios: Dict[int, str] = {}  # id -> name, from the dropdown on fitx.de/fitnesskurse
fitx_studios_at = 0.0


def _tz_berlin():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Europe/Berlin")
    except Exception:
        return None


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


async def _load_fitx_studios() -> Dict[int, str]:
    global fitx_studios, fitx_studios_at
    if time.time() - fitx_studios_at > STUDIO_LIST_TTL:
        fitx_studios_at = time.time()  # also throttles retries when fitx.de is down
        try:
            fitx_studios = await fetch_fitx_studios(client)
            logger.info("Loaded %d FitX studios", len(fitx_studios))
        except Exception as e:
            logger.warning("Could not load FitX studio list: %s", e)
    return fitx_studios


class Calendar:
    def __init__(self, name: str, title: str, fetch: Callable[[], Awaitable[List[CourseEvent]]]):
        self.name = name
        self.title = title
        self.fetch = fetch
        self.events: List[CourseEvent] = load_cache(name) or []
        self.checked_at = cache_mtime(name)  # last fetch attempt (successful or not)
        self.lock = asyncio.Lock()
        self.task: Optional[asyncio.Task] = None

    async def refresh(self) -> None:
        async with self.lock:
            if time.time() - self.checked_at < REFRESH_INTERVAL_SECONDS:
                return  # someone else just refreshed
            self.checked_at = time.time()
            try:
                events = await self.fetch()
                uniq = {(e.id, int(e.start.timestamp()), int(e.end.timestamp())): e for e in events}
                self.events = sorted(uniq.values(), key=lambda e: (e.start, e.title))
                save_cache(self.name, self.events)
                logger.info("Refreshed %s: %d events", self.name, len(self.events))
            except Exception as e:
                logger.error("Refresh of %s failed, keeping last good data: %s", self.name, e)


calendars: Dict[str, Calendar] = {}


async def _make_calendar(name: str) -> Calendar:
    gym, _, key = name.partition("-")
    if gym == "fitx" and key.isdigit():
        studios = await _load_fitx_studios()
        sid = int(key)
        if studios and sid not in studios:
            raise HTTPException(status_code=404, detail="Unknown FitX studio")
        return Calendar(name, f"FitX {studios.get(sid, sid)}", lambda: _fetch_fitx(sid))
    if gym == "bodyandsoul" and key in BODYSOUL_CENTERS:
        return Calendar(
            name, f"body + soul {BODYSOUL_CENTERS[key]}", lambda: bodyandsoul.fetch_center(client, key)
        )
    raise HTTPException(status_code=404, detail="Unknown calendar")


async def get_calendar(name: str) -> Calendar:
    """Calendars are created and fetched on first request, then refreshed in the background when stale."""
    cal = calendars.get(name)
    if cal is None:
        cal = calendars[name] = await _make_calendar(name)
    if time.time() - cal.checked_at >= REFRESH_INTERVAL_SECONDS:
        if not cal.events:
            await cal.refresh()
        elif not (cal.task and not cal.task.done()):
            cal.task = asyncio.create_task(cal.refresh())
    return cal


@app.on_event("startup")
async def on_startup() -> None:
    global client
    ensure_data_dir()
    client = httpx.AsyncClient(http2=False, headers={})
    await _load_fitx_studios()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if client:
        await client.aclose()


@app.get("/health")
async def health() -> Response:
    return Response(content="OK", media_type="text/plain")


@app.get("/api/courses/{name}")
async def courses(name: str) -> dict:
    cal = await get_calendar(name)
    return {"title": cal.title, "courses": sorted({e.title for e in cal.events}, key=str.lower)}


@app.get("/{name}.ics")
async def ics_feed(name: str, course: List[str] = Query(default=[])) -> Response:
    cal = await get_calendar(name)
    events = cal.events
    if course:
        wanted = set(course)
        events = [e for e in events if e.title in wanted]
    return Response(
        content=generate_ics(cal.name, events, cal.title),
        media_type="text/calendar; charset=utf-8",
    )


def _options(items: Dict[str, str]) -> str:
    return "".join(
        f'<option value="{html.escape(k)}">{html.escape(v)}</option>'
        for k, v in sorted(items.items(), key=lambda kv: kv[1].lower())
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    studios = await _load_fitx_studios()
    return (
        PAGE.replace("{{FITX}}", _options({f"fitx-{k}": v for k, v in studios.items()}))
        .replace("{{BODYSOUL}}", _options({f"bodyandsoul-{k}": v for k, v in BODYSOUL_CENTERS.items()}))
    )


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fitness Calendar</title>
<style>
:root { color-scheme: light dark; --fg:#1d1d1f; --bg:#f2f2f7; --card:#fff; --muted:#6e6e73; --line:#e5e5ea; --field:#f2f2f7; --accent:#0071e3; --fitx:#f39200; --bs:#9b7b62; }
@media (prefers-color-scheme: dark) { :root { --fg:#f5f5f7; --bg:#000; --card:#1c1c1e; --muted:#98989d; --line:#38383a; --field:#2c2c2e; --accent:#2997ff; } }
* { box-sizing: border-box; }
body { font: 16px/1.45 -apple-system, BlinkMacSystemFont, "Inter", system-ui, sans-serif; color: var(--fg); background: var(--bg); max-width: 760px; margin: 0 auto; padding: 40px 16px 64px; -webkit-font-smoothing: antialiased; }
header { margin-bottom: 28px; }
h1 { font-size: 34px; letter-spacing: -0.02em; margin: 0 0 6px; }
.intro { color: var(--muted); margin: 0; }
section { background: var(--card); border-radius: 20px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 2px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.06); }
h2 { display: flex; align-items: center; gap: 10px; font-size: 21px; letter-spacing: -0.01em; margin: 0 0 16px; }
h2::before { content: ""; width: 12px; height: 12px; border-radius: 50%; background: var(--brand); }
select, .q { width: 100%; padding: 12px 14px; font: inherit; color: inherit; background-color: var(--field); border: 1px solid transparent; border-radius: 12px; outline: none; transition: border-color .15s, box-shadow .15s; }
select { appearance: none; padding-right: 40px; cursor: pointer; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' fill='none' stroke='%238e8e93' stroke-width='2' stroke-linecap='round'%3E%3Cpath d='M1 1.5l5 5 5-5'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 14px center; }
select:focus, .q:focus { border-color: var(--brand); box-shadow: 0 0 0 3px color-mix(in srgb, var(--brand) 25%, transparent); }
.q { margin-top: 12px; }
.boxes { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0 12px; }
label { position: relative; padding: 7px 14px; border-radius: 999px; background: var(--field); font-size: 15px; cursor: pointer; user-select: none; transition: background .15s, color .15s; }
label:hover { background: color-mix(in srgb, var(--brand) 15%, var(--field)); }
label input { position: absolute; opacity: 0; pointer-events: none; }
label:has(input:checked) { background: var(--brand); color: #fff; }
label:has(input:focus-visible) { outline: 2px solid var(--brand); outline-offset: 2px; }
label[hidden] { display: none; }
.tools { display: flex; gap: 16px; font-size: 14px; }
.tools button { background: none; border: 0; color: var(--accent); font: inherit; padding: 0; cursor: pointer; }
.sub { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 20px; padding-top: 20px; border-top: 1px solid var(--line); }
.sub .go, .sub button { font: inherit; font-size: 15px; font-weight: 600; padding: 10px 18px; border-radius: 12px; border: 0; cursor: pointer; text-decoration: none; }
.sub .go { background: var(--brand); color: #fff; }
.sub button { background: var(--field); color: var(--fg); }
.url { flex-basis: 100%; font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted); word-break: break-all; }
.muted { color: var(--muted); font-size: 14px; margin: 12px 0 0; }
.body[hidden] { display: none; }
</style></head><body>
<header>
<h1>Fitness Calendar</h1>
<p class="intro">Pick your studio, choose the courses you like, and subscribe. Nothing selected = all courses.</p>
</header>

<section style="--brand: var(--fitx)">
  <h2>FitX</h2>
  <select><option value="">Choose a studio…</option>{{FITX}}</select>
  <div class="body" hidden></div>
</section>

<section style="--brand: var(--bs)">
  <h2>body + soul</h2>
  <select><option value="">Choose a center…</option>{{BODYSOUL}}</select>
  <div class="body" hidden></div>
</section>

<template id="tpl">
  <input class="q" type="search" placeholder="Search courses…">
  <div class="boxes"></div>
  <div class="tools"><button type="button" class="all">Select all</button><button type="button" class="none">Clear</button></div>
  <div class="sub"><a class="go link">Subscribe</a><button type="button" class="copy">Copy link</button><span class="url"></span></div>
  <p class="muted status"></p>
</template>

<script>
const store = { get: k => { try { return localStorage.getItem(k) } catch { return null } },
                set: (k, v) => { try { localStorage.setItem(k, v) } catch {} } };

document.querySelectorAll('section').forEach((s, i) => {
  const select = s.querySelector('select'), body = s.querySelector('.body');
  const key = 'studio-' + i;

  async function load(name) {
    store.set(key, name);
    body.hidden = !name;
    if (!name) return;
    body.replaceChildren(document.getElementById('tpl').content.cloneNode(true));
    const boxes = body.querySelector('.boxes'), link = body.querySelector('.link'), status = body.querySelector('.status');
    status.textContent = 'Loading courses…';
    const update = () => {
      const qs = [...boxes.querySelectorAll('input:checked')].map(b => 'course=' + encodeURIComponent(b.value)).join('&');
      link.href = body.querySelector('.url').textContent = 'webcal://' + location.host + '/' + name + '.ics' + (qs ? '?' + qs : '');
    };
    update();
    try {
      const r = await fetch('/api/courses/' + name);
      if (!r.ok) throw new Error(r.status);
      const { courses } = await r.json();
      if (select.value !== name) return;  // user switched studio meanwhile
      for (const c of courses) {
        const l = document.createElement('label'), cb = document.createElement('input');
        cb.type = 'checkbox'; cb.value = c;
        l.append(cb, ' ' + c);
        boxes.append(l);
      }
      status.textContent = courses.length ? '' : 'No courses available right now (the gym website may be down). You can still subscribe; courses appear once it is back.';
    } catch (e) {
      status.textContent = 'Could not load courses.';
    }
    boxes.addEventListener('change', update);
    body.querySelector('.q').addEventListener('input', e => {
      const q = e.target.value.toLowerCase();
      boxes.querySelectorAll('label').forEach(l => l.hidden = !l.textContent.toLowerCase().includes(q));
    });
    const setAll = v => { boxes.querySelectorAll('label:not([hidden]) input').forEach(b => b.checked = v); update(); };
    body.querySelector('.all').onclick = () => setAll(true);
    body.querySelector('.none').onclick = () => setAll(false);
    body.querySelector('.copy').onclick = e => navigator.clipboard?.writeText(link.href).then(() => {
      e.target.textContent = 'Copied ✓'; setTimeout(() => e.target.textContent = 'Copy link', 1500);
    });
  }

  select.addEventListener('change', () => load(select.value));
  const saved = store.get(key);
  if (saved && [...select.options].some(o => o.value === saved)) { select.value = saved; load(saved); }
});
</script></body></html>"""
