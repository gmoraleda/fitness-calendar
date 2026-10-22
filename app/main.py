from __future__ import annotations

import asyncio
import html
import json
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
from .courseinfo import add_info
from .parser import parse_fitx_course_page, parse_schedule
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
    await add_info(client, "fitx", events, parse_fitx_course_page)
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
    first = {}
    for e in cal.events:
        first.setdefault(e.title, e)
    return {
        "title": cal.title,
        "courses": [
            {"title": t, "category": e.category, "level": e.level, "description": e.description, "url": e.url}
            for t, e in sorted(first.items(), key=lambda kv: kv[0].lower())
        ],
    }


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


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    studios = await _load_fitx_studios()

    def opts(prefix: str, items: Dict) -> list:
        return [[f"{prefix}-{k}", v] for k, v in sorted(items.items(), key=lambda kv: kv[1].lower())]

    gyms = {
        "bodyandsoul": {"name": "body + soul", "note": f"{len(BODYSOUL_CENTERS)} centers in and around München",
                        "options": opts("bodyandsoul", BODYSOUL_CENTERS)},
        "fitx": {"name": "FitX", "note": f"{len(studios)} studios across Germany", "options": opts("fitx", studios)},
    }
    # "</" escaped so studio names can never close the <script> tag.
    return PAGE.replace("{{GYMS}}", json.dumps(gyms, ensure_ascii=False).replace("</", "<\\/"))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fitness Calendar</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400&display=swap" rel="stylesheet">
<style>
:root {
  color-scheme: light dark;
  --bg: #f9fafb; --surface: #fff; --card-a: #fbf9f6; --card-b: #f4efe9; --border: #e5e7eb;
  --fg: #030712; --muted: #565d6b; --accent: #3d5a8f; --link: #0d8fb5; --check: #030712; --check-fg: #fff;
  --serif: "Newsreader", Georgia, serif; --sans: "Inter", -apple-system, system-ui, sans-serif; --mono: "JetBrains Mono", ui-monospace, Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root {
  --bg: #030712; --surface: #0c111c; --card-a: #12161f; --card-b: #0e1219; --border: #1f2937;
  --fg: #f5f5f5; --muted: #9ca3af; --accent: #8fa8d8; --link: #12a8da; --check: #f5f5f5; --check-fg: #030712;
} }
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.6 var(--sans); color: var(--fg); background: var(--bg); -webkit-font-smoothing: antialiased; }
.bar { position: sticky; top: 0; z-index: 2; border-bottom: 1px solid var(--border); background: color-mix(in srgb, var(--bg) 85%, transparent); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }
.bar div, main { max-width: 720px; margin: 0 auto; padding: 0 16px; }
.bar div { height: 52px; display: flex; align-items: center; font-weight: 500; font-size: 14px; }
.hero { padding: 56px 0 40px; }
.tagline { font: italic 17px var(--serif); color: var(--accent); margin: 0 0 8px; }
h1 { font: 400 clamp(34px, 6vw, 46px)/1.1 var(--serif); letter-spacing: -0.015em; margin: 0 0 16px; }
h1 em, h2 em { color: var(--accent); }
.lede { color: var(--muted); max-width: 34em; margin: 0; }
.eyebrow { font: 500 11px var(--mono); letter-spacing: .12em; text-transform: uppercase; color: var(--muted); margin: 0 0 6px; }
.eyebrow::before { content: "• "; color: var(--accent); }
h2 { font: 400 30px/1.2 var(--serif); margin: 0 0 18px; }
select, .q { width: 100%; font: inherit; color: inherit; padding: 11px 14px; background-color: var(--surface); border: 1px solid var(--border); border-radius: 10px; outline: none; transition: border-color .15s, box-shadow .15s; }
select { appearance: none; cursor: pointer; padding-right: 40px; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' fill='none' stroke='%239ca3af' stroke-width='1.6' stroke-linecap='round'%3E%3Cpath d='M1 1.5l5 5 5-5'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 14px center; }
select:focus, .q:focus { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent); }
.card { margin-top: 16px; padding: 22px; border: 1px solid var(--border); border-radius: 14px; background: linear-gradient(160deg, var(--card-a), var(--card-b)); box-shadow: 0 1px 2px rgba(0,0,0,.03), 0 12px 32px -12px rgba(0,0,0,.12); }
.card[hidden], .group[hidden], .row[hidden] { display: none; }
.q { margin-bottom: 8px; }
.group { margin-top: 18px; }
.group h3 { font: 500 11px var(--mono); letter-spacing: .12em; text-transform: uppercase; color: var(--muted); margin: 0 0 4px; }
.row { border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent); }
.row:last-child { border-bottom: 0; }
.line { display: flex; align-items: center; gap: 12px; padding: 9px 0; }
.line label { flex: 1; display: flex; align-items: center; gap: 12px; cursor: pointer; min-width: 0; }
.line input { appearance: none; flex: none; width: 18px; height: 18px; margin: 0; border: 1.5px solid color-mix(in srgb, var(--muted) 60%, transparent); border-radius: 5px; background: var(--surface); cursor: pointer; display: grid; place-content: center; transition: background .12s, border-color .12s; }
.line input:checked { background: var(--check); border-color: var(--check); }
.line input:checked::after { content: ""; width: 9px; height: 5px; border: solid var(--check-fg); border-width: 0 0 2px 2px; transform: translateY(-1px) rotate(-45deg); }
.line input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.title { font-weight: 500; }
.level { font: 11px var(--mono); color: var(--muted); white-space: nowrap; }
.more { flex: none; font: 12px var(--mono); color: var(--muted); background: none; border: 1px solid var(--border); border-radius: 999px; padding: 2px 10px; cursor: pointer; }
.more[aria-expanded="true"] { color: var(--fg); border-color: var(--muted); }
.desc { margin: 0 0 12px 30px; font: 400 16px/1.55 var(--serif); color: var(--muted); white-space: pre-line; }
.desc a { font: 13px var(--sans); color: var(--link); }
.tools { display: flex; gap: 16px; margin-top: 14px; font-size: 13px; color: var(--muted); }
.tools button { font: inherit; color: var(--fg); background: none; border: 0; padding: 0; text-decoration: underline; text-underline-offset: 3px; cursor: pointer; }
.tools .count { margin-left: auto; font-family: var(--mono); font-size: 12px; }
.sub { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 18px; margin-top: 20px; padding-top: 20px; border-top: 1px solid var(--border); }
.sub .go { font-weight: 500; text-decoration: none; color: var(--bg); background: var(--fg); padding: 10px 18px; border-radius: 10px; }
.sub button { font: inherit; font-size: 14px; color: var(--fg); background: none; border: 0; padding: 0; text-decoration: underline; text-underline-offset: 3px; cursor: pointer; }
.url { flex-basis: 100%; font: 11px/1.5 var(--mono); color: var(--muted); word-break: break-all; }
.status { font-size: 13px; color: var(--muted); margin: 12px 0 0; }
.steps { list-style: none; margin: 0; padding: 0; }
.step { display: grid; grid-template-columns: 44px 1fr; gap: 0 12px; padding: 32px 0 36px; border-top: 1px solid var(--border); }
.step[hidden] { display: none; }
.n { font: 400 38px/1 var(--serif); color: var(--accent); }
.step h2 { margin-bottom: 16px; }
.chains { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
.chain { text-align: left; font: inherit; color: inherit; cursor: pointer; padding: 18px 20px; border: 1px solid var(--border); border-radius: 14px; background: linear-gradient(160deg, var(--card-a), var(--card-b)); transition: border-color .15s, box-shadow .15s; }
.chain:hover { border-color: color-mix(in srgb, var(--accent) 50%, var(--border)); }
.chain[aria-pressed="true"] { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent); }
.chain b { display: block; font: 400 24px/1.2 var(--serif); }
.chain small { display: block; margin-top: 4px; font: 11px var(--mono); letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
.step .card { margin-top: 0; }
@media (max-width: 520px) { .step { grid-template-columns: 1fr; } .n { font-size: 28px; margin-bottom: 8px; } }
footer { padding: 24px 0 56px; border-top: 1px solid var(--border); font-size: 13px; color: var(--muted); }
@media (max-width: 520px) { .level { display: none; } .card { padding: 16px; } }
</style></head><body>
<div class="bar"><div>Fitness Calendar</div></div>
<main>
  <div class="hero">
    <p class="tagline">Group classes, on your schedule</p>
    <h1>Your gym's classes, <em>in your calendar.</em></h1>
    <p class="lede">Pick a studio, choose the courses you like and subscribe. Your calendar stays up to date as the timetable changes. Nothing selected means every course.</p>
  </div>

  <ol class="steps">
    <li class="step"><span class="n">1</span><div>
      <p class="eyebrow">Gym</p>
      <h2>Choose your <em>gym</em></h2>
      <div class="chains"></div>
    </div></li>
    <li class="step" id="step-location" hidden><span class="n">2</span><div>
      <p class="eyebrow">Location</p>
      <h2>Choose your <em>studio</em></h2>
      <select></select>
    </div></li>
    <li class="step" id="step-courses" hidden><span class="n">3</span><div>
      <p class="eyebrow">Courses</p>
      <h2>Pick your <em>courses</em></h2>
      <div class="card"></div>
    </div></li>
  </ol>

  <footer>Timetables are refreshed from the gyms' websites every 15 minutes.</footer>
</main>

<template id="tpl">
  <input class="q" type="search" placeholder="Search courses…">
  <div class="list"></div>
  <div class="tools"><button type="button" class="all">Select all</button><button type="button" class="none">Clear</button><span class="count"></span></div>
  <div class="sub"><a class="go link">Subscribe</a><button type="button" class="copy">Copy link</button><span class="url"></span></div>
  <p class="status"></p>
</template>

<script>
const store = { get: k => { try { return localStorage.getItem(k) } catch { return null } },
                set: (k, v) => { try { localStorage.setItem(k, v) } catch {} } };
const el = (tag, props = {}, ...kids) => { const n = Object.assign(document.createElement(tag), props); n.append(...kids); return n; };

const GYMS = {{GYMS}};
const chains = document.querySelector('.chains'), select = document.querySelector('select');
const locationStep = document.getElementById('step-location'), coursesStep = document.getElementById('step-courses');
const card = coursesStep.querySelector('.card');
let gym = null;

for (const [id, g] of Object.entries(GYMS)) {
  const b = el('button', { type: 'button', className: 'chain' }, el('b', { textContent: g.name }), el('small', { textContent: g.note }));
  b.dataset.gym = id;
  b.setAttribute('aria-pressed', 'false');
  b.onclick = () => pickGym(id, true);
  chains.append(b);
}

function pickGym(id, scroll) {
  gym = id;
  store.set('gym', id);
  chains.querySelectorAll('.chain').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.gym === id)));
  const g = GYMS[id];
  locationStep.querySelector('h2 em').textContent = id === 'fitx' ? 'studio' : 'center';
  select.replaceChildren(el('option', { value: '', textContent: 'Select a ' + g.name + (id === 'fitx' ? ' studio…' : ' center…') }),
                         ...g.options.map(([v, n]) => el('option', { value: v, textContent: n })));
  locationStep.hidden = false;
  const saved = store.get('studio-' + id);
  select.value = saved && g.options.some(([v]) => v === saved) ? saved : '';
  load(select.value);
  if (scroll) locationStep.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

  async function load(name) {
    store.set('studio-' + gym, name);
    coursesStep.hidden = !name;
    if (!name) return;
    card.replaceChildren(document.getElementById('tpl').content.cloneNode(true));
    const list = card.querySelector('.list'), link = card.querySelector('.link'), status = card.querySelector('.status');
    const count = card.querySelector('.count');
    const update = () => {
      const picked = [...list.querySelectorAll('input:checked')];
      const qs = picked.map(b => 'course=' + encodeURIComponent(b.value)).join('&');
      link.href = card.querySelector('.url').textContent = 'webcal://' + location.host + '/' + name + '.ics' + (qs ? '?' + qs : '');
      count.textContent = picked.length ? picked.length + ' selected' : 'all courses';
    };
    update();
    status.textContent = 'Loading courses…';
    let courses = [];
    try {
      const r = await fetch('/api/courses/' + name);
      if (!r.ok) throw new Error(r.status);
      courses = (await r.json()).courses;
    } catch { status.textContent = 'Could not load courses.'; return; }
    if (select.value !== name) return;  // switched studio meanwhile
    status.textContent = courses.length ? '' : 'No courses available right now (the gym website may be down). You can still subscribe; courses appear once it is back.';

    const groups = {};
    for (const c of courses) (groups[c.category || 'Other'] ??= []).push(c);
    const cats = Object.keys(groups).sort((a, b) => (a === 'Other') - (b === 'Other') || a.localeCompare(b));
    for (const cat of cats) {
      const g = el('div', { className: 'group' }, el('h3', { textContent: cat }));
      for (const c of groups[cat]) {
        const cb = el('input', { type: 'checkbox', value: c.title });
        const line = el('div', { className: 'line' },
          el('label', {}, cb, el('span', { className: 'title', textContent: c.title })),
          ...(c.level ? [el('span', { className: 'level', textContent: c.level })] : []));
        const row = el('div', { className: 'row' }, line);
        if (c.description || c.url) {
          const desc = el('p', { className: 'desc', hidden: true, textContent: c.description || '' });
          if (c.url) desc.append(el('br'), el('a', { href: c.url, target: '_blank', rel: 'noopener', textContent: 'Course page ↗' }));
          const btn = el('button', { type: 'button', className: 'more', textContent: 'Info' });
          btn.setAttribute('aria-expanded', 'false');
          btn.onclick = () => { desc.hidden = !desc.hidden; btn.setAttribute('aria-expanded', String(!desc.hidden)); };
          line.append(btn); row.append(desc);
        }
        g.append(row);
      }
      list.append(g);
    }
    list.addEventListener('change', update);
    card.querySelector('.q').addEventListener('input', e => {
      const q = e.target.value.toLowerCase();
      list.querySelectorAll('.row').forEach(r => r.hidden = !r.querySelector('.title').textContent.toLowerCase().includes(q));
      list.querySelectorAll('.group').forEach(g => g.hidden = !g.querySelector('.row:not([hidden])'));
    });
    const setAll = v => { list.querySelectorAll('.row:not([hidden]) input').forEach(b => b.checked = v); update(); };
    card.querySelector('.all').onclick = () => setAll(true);
    card.querySelector('.none').onclick = () => setAll(false);
    card.querySelector('.copy').onclick = e => navigator.clipboard?.writeText(link.href).then(() => {
      e.target.textContent = 'Copied'; setTimeout(() => e.target.textContent = 'Copy link', 1500);
    });
  }

select.addEventListener('change', () => {
  load(select.value);
  if (select.value) coursesStep.scrollIntoView({ behavior: 'smooth', block: 'start' });
});
const savedGym = store.get('gym');
if (savedGym && GYMS[savedGym]) pickGym(savedGym, false);
</script></body></html>"""
