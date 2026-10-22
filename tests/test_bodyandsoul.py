from app.bodyandsoul import parse_courseplan

ROW = '<tr><td class="times">06:00</td>' + '<td></td>' * 3 + (
    '<td><a href="/x" class="course-item"><div class="meta">06:45 | Studio 1 | 60 Min.</div>'
    '<div class="title">HOT IRON® 1</div></a></td>'
) + '<td></td>' * 3 + '</tr>'
HTML = f'<div id="courseplaner"><div class="week-slide"><div class="week">29. Juni - 05. Juli&nbsp;2026</div><table>{ROW}</table></div></div>'


def test_parse_courseplan():
    [ev] = parse_courseplan(HTML)
    assert ev.title == "HOT IRON® 1" and ev.room == "Studio 1"
    assert ev.start.isoformat() == "2026-07-02T06:45:00+02:00"  # Thursday
    assert ev.end.isoformat() == "2026-07-02T07:45:00+02:00"


if __name__ == "__main__":
    test_parse_courseplan()
    print("ok")
