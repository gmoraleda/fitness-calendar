from app.fitnessfirst import parse_items

ITEM = {
    "id": 2713175583, "title": "LES MILLS BODYCOMBAT", "url": "/kurse/bodycombat", "description": " Cardio. ",
    "time": {"from": "07:00", "to": "08:00"}, "category": "Les Mills BodyCombat", "categoryDisplayOnly": "Cardio",
    "club": "München - Am Marienplatz", "timestampStart": "2026-11-12T07:00:00+01:00",
    "level": "L2/L3", "levelInfo": {"code": "L2/L3", "label": "Mittelstufe, Fortgeschritten"},
}


def test_parse_items():
    [ev] = parse_items([ITEM])
    assert ev.title == "LES MILLS BODYCOMBAT" and ev.id == "2713175583"
    assert ev.start.isoformat() == "2026-11-12T07:00:00+01:00" and ev.end.isoformat() == "2026-11-12T08:00:00+01:00"
    assert ev.category == "Cardio" and ev.level == "Mittelstufe, Fortgeschritten" and ev.description == "Cardio."
    assert ev.url == "https://www.fitnessfirst.de/kurse/bodycombat" and ev.booking
    assert ev.location == "Fitness First München - Am Marienplatz"


if __name__ == "__main__":
    test_parse_items()
    print("ok")
