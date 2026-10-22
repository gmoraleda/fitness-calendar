"""Per-course details (description, level, category) scraped from each gym's course pages.

Course pages rarely change, so they are cached for a week on disk and shared by all studios of a gym.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Callable, Dict, List

import httpx

from .fetcher import build_headers
from .models import CourseEvent
from .store import DATA_DIR, atomic_write

TTL = 7 * 24 * 3600
_caches: Dict[str, Dict[str, dict]] = {}


async def add_info(
    client: httpx.AsyncClient, gym: str, events: List[CourseEvent], parse: Callable[[str], dict]
) -> None:
    path = DATA_DIR / f"{gym}-courses.json"
    cache = _caches.setdefault(gym, {})
    if not cache and path.exists():
        try:
            cache.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
    now = time.time()
    stale = {e.url for e in events if e.url and now - cache.get(e.url, {}).get("at", 0) > TTL}
    sem = asyncio.Semaphore(4)  # be gentle with their servers

    async def fetch(url: str) -> None:
        async with sem:
            try:
                r = await client.get(url, headers=build_headers(None), timeout=httpx.Timeout(20.0, connect=10.0))
                r.raise_for_status()
                cache[url] = {**parse(r.text), "at": now}
            except Exception:
                pass  # keep old info if any; retried on the next refresh

    if stale:
        await asyncio.gather(*(fetch(u) for u in stale))
        atomic_write(path, json.dumps(cache, ensure_ascii=False).encode("utf-8"))
    for e in events:
        info = cache.get(e.url or "", {})
        e.description = info.get("description")
        e.level = info.get("level")
        e.category = e.category or info.get("category")
