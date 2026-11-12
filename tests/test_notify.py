import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp()

from app import main  # noqa: E402
from app.models import CourseEvent  # noqa: E402

START = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)
EVENT = CourseEvent(id="1", title="Yoga", start=START, end=START + timedelta(hours=1))


def test_alerts_once_per_outage():
    sent = []

    async def fake_notify(title, message, tags="warning"):
        sent.append(title)

    main.notify = fake_notify
    results = [[EVENT], ValueError("parser broke"), ValueError("parser broke"), [], [EVENT]]

    async def fetch():
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def run():
        cal = main.Calendar("test-1", "Test Gym", fetch)
        for _ in range(5):
            cal.checked_at = 0  # skip the refresh throttle
            await cal.refresh()
        return cal

    cal = asyncio.run(run())
    # ok, fail (alert), fail, empty (still failing), ok (recovered)
    assert sent == ["Test Gym: refresh failing", "Test Gym: back to normal"], sent
    assert cal.events == [EVENT] and not cal.failing


if __name__ == "__main__":
    test_alerts_once_per_outage()
    print("ok")
