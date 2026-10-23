from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List

import httpx
from zoneinfo import ZoneInfo

from .models import CourseEvent

BERLIN = ZoneInfo("Europe/Berlin")
API = "https://graphql-widget-master.production.eversports.cloud/api/graphql"
WEEKS_AHEAD = 4

# Center slug -> Eversports schedule widget id (data-eversports-widget-id on
# bodyandsoul.de/class-schedule-<slug>/).
WIDGETS = {
    "brunnthal": "9f03cea9-5780-4237-98be-0cd1ad412893",
    "englischer-garten": "de0504ab-0fcd-4562-8060-b78353762898",
    "erding": "1c71b5b3-3da3-4748-a93f-40423b65d9dd",
    "hirschgarten": "8ab8fb37-87f0-4bb9-b384-a484d8a8d82d",
    "muenchen-mitte": "1ed4023f-5211-4798-bc07-b25ef3b9b8c4",
    "muenchen-nord": "0000c822-c6d3-4266-9731-ef09c234d136",
    "sendling": "6e10bf1e-55fa-4998-abca-0cd930f73506",
    "theresienhoehe": "e7782813-7b6b-4c19-ad1f-b1930f7ebffe",
    "trudering": "cc18422b-d3dd-4a82-9d3f-95a6319ac58f",
    "werk12": "3cf71100-77fe-4ecb-8960-ba47cada1017",
}

QUERY = """
query($w: ID!, $t: TimeRangeInput, $after: Cursor) {
  widget(id: $w) {
    ... on WidgetActivitySchedule {
      activities(timeRange: $t, first: 200, after: $after,
                 activityGroupPublicationStates: [ACTIVE, VIEW_ONLY], isArchived: false) {
        nodes {
          id name start end isCancelled detailsPageURL
          room { name }
          teacher { name }
          activityGroup { name level category { name } description { plainText } venue { name } }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


async def fetch_center(client: httpx.AsyncClient, center: str) -> List[CourseEvent]:
    today = date.today()
    variables = {
        "w": WIDGETS[center],
        "t": {"start": today.isoformat(), "end": (today + timedelta(weeks=WEEKS_AHEAD)).isoformat()},
        "after": None,
    }
    nodes: list = []
    while True:
        resp = await client.post(API, json={"query": QUERY, "variables": variables},
                                 timeout=httpx.Timeout(20.0, connect=10.0))
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise ValueError(f"Eversports error for {center}: {body['errors'][0].get('message')}")
        page = body["data"]["widget"]["activities"]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        variables["after"] = page["pageInfo"]["endCursor"]
    events = parse_activities(nodes)
    if not events:
        # Don't overwrite a good cache with an empty calendar.
        raise ValueError(f"No body+soul courses found for {center}")
    return events


def parse_activities(nodes: list) -> List[CourseEvent]:
    events = []
    for n in nodes:
        if n.get("isCancelled"):
            continue
        g = n.get("activityGroup") or {}
        level = g.get("level")
        events.append(CourseEvent(
            id=n["id"],
            title=n.get("name") or g.get("name") or "Kurs",
            start=datetime.fromisoformat(n["start"]).astimezone(BERLIN),
            end=datetime.fromisoformat(n["end"]).astimezone(BERLIN),
            location=(g.get("venue") or {}).get("name"),
            room=(n.get("room") or {}).get("name"),
            instructor=(n.get("teacher") or {}).get("name"),
            category=((g.get("category") or {}).get("name") or "").strip() or None,
            level=level.replace("_", " ").title() if level else None,
            description=((g.get("description") or {}).get("plainText") or "").strip() or None,
            url=n.get("detailsPageURL"),
        ))
    return sorted(events, key=lambda e: (e.start, e.title))
