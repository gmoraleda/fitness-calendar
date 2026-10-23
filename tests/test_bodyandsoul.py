from app.bodyandsoul import parse_activities

NODE = {
    "id": "a1", "name": None, "start": "2026-10-26T06:45:00+01:00", "end": "2026-10-26T07:45:00+01:00",
    "isCancelled": False, "detailsPageURL": "https://www.eversports.de/org/activity/a1",
    "room": {"name": "Studio 1"}, "teacher": None,
    "activityGroup": {"name": "HOT IRON® 2", "level": "ALL_LEVELS", "category": {"name": "Workout"},
                      "description": {"plainText": " Langhantel. "}, "venue": {"name": "body + soul Center Sendling"}},
}


def test_parse_activities():
    [ev] = parse_activities([NODE, {**NODE, "id": "a2", "isCancelled": True}])
    assert ev.title == "HOT IRON® 2" and ev.room == "Studio 1" and ev.instructor is None
    assert ev.start.isoformat() == "2026-10-26T06:45:00+01:00"
    assert ev.level == "All Levels" and ev.category == "Workout" and ev.description == "Langhantel."


if __name__ == "__main__":
    test_parse_activities()
    print("ok")
