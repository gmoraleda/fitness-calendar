from app.parser import parse_fitx_course_page

HTML = """<meta name="description" content="Kraft für alle.">
<div class="course_hero__condition"><div class="course_hero__conditions_headline">Intensität</div>
<div class="course_hero__conditions_subline"><span>moderat - intensiv</span></div></div>
<label><span class="course_hero__goals-progress-title">Kraft</span><progress class="course_hero__goals-progress-bar" value="80"></progress></label>
<label><span class="course_hero__goals-progress-title">Ausdauer</span><progress class="course_hero__goals-progress-bar" value="5"></progress></label>"""


def test_parse_fitx_course_page():
    info = parse_fitx_course_page(HTML)
    assert info == {"description": "Kraft für alle.", "level": "moderat - intensiv", "category": "Kraft"}


if __name__ == "__main__":
    test_parse_fitx_course_page()
    print("ok")
