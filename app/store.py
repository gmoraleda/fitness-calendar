from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from zoneinfo import ZoneInfo

from .models import CourseEvent


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")).resolve()
BERLIN = ZoneInfo("Europe/Berlin")


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_cache(name: str, events: List[CourseEvent]) -> None:
    ensure_data_dir()
    payload = [
        {
            "id": e.id,
            "title": e.title,
            "start": e.start.isoformat(),
            "end": e.end.isoformat(),
            "location": e.location,
            "instructor": e.instructor,
            "room": e.room,
            "description": e.description,
            "category": e.category,
            "level": e.level,
            "url": e.url,
        }
        for e in events
    ]
    atomic_write(DATA_DIR / f"{name}.json", json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def cache_mtime(name: str) -> float:
    try:
        return (DATA_DIR / f"{name}.json").stat().st_mtime
    except OSError:
        return 0.0


def load_cache(name: str) -> Optional[List[CourseEvent]]:
    path = DATA_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        return [
            CourseEvent(
                id=str(it.get("id", "")),
                title=str(it.get("title", "")),
                start=datetime.fromisoformat(it["start"]).astimezone(BERLIN),
                end=datetime.fromisoformat(it["end"]).astimezone(BERLIN),
                location=it.get("location"),
                instructor=it.get("instructor"),
                room=it.get("room"),
                description=it.get("description"),
                category=it.get("category"),
                level=it.get("level"),
                url=it.get("url"),
            )
            for it in json.loads(path.read_text(encoding="utf-8"))
        ]
    except Exception:
        return None
