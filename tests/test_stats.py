import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()

from app import stats  # noqa: E402

APPLE = "iOS/17.2 (21C62) dataaccessd/1.0"
GOOGLE = "Google-Calendar-Importer"


def test_stats():
    stats.record("FitX A", [], APPLE, "abc123", "u1")
    stats.record("FitX A", [], APPLE, "abc123", "u1")  # same subscription polling again
    stats.record("FitX A", ["yoga", "spin"], GOOGLE, None, "u2?course=yoga&course=spin")
    stats.record("FitX B", ["yoga"], GOOGLE, "BAD id!", "u3?course=yoga")  # invalid id -> app + URL
    s = stats.summary()
    assert s["total"] == 3 and s["all_courses"] == 1
    assert s["calendars"] == [("FitX A", 2), ("FitX B", 1)]
    assert ("yoga", "FitX A") in dict(s["courses"]) and dict(s["clients"]) == {"Apple Calendar": 1, "Google Calendar": 2}
    assert stats.summary(now=stats.time.time() + stats.ACTIVE + 1)["total"] == 0  # unsubscribed after 14 quiet days
    assert "<h1>3</h1>" in stats.page() and stats.PATH.exists()


if __name__ == "__main__":
    test_stats()
    print("ok")
