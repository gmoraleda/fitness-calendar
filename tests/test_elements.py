from app.elements import parse_courseplan

ROW = """<tr data-course-name="{name}" data-course-description="Atmen.&#10;">
  <td><span>{time}</span></td><td><a class="open-course-overlay">{name}</a></td><td><span>60 min</span></td>
  <td><span>Nikolaus</span></td><td><span>R1</span></td><td></td><td><a class="open-course-overlay">Mehr</a></td>
  <td>{form}</td></tr>"""
FORM = '<form method="post"><input type="hidden" name="course_id" value="385558"/><button name="subscribe">Zum Kurs anmelden</button></form>'
HTML = f"""<div class="courseplan" data-studio="elements-siemensallee">
<div class="accordion open"><div class="head"><span data-day="12.11.2026">Donnerstag, 12.11.2026</span></div>
<div class="body"><table><thead><tr><td><span>Uhrzeit</span></td></tr></thead><tbody>
{ROW.format(name="QiGong", time="08:00", form=FORM)}
{ROW.format(name="Sauna-Aufguss", time="18:30", form="")}
</tbody></table></div></div></div>"""


def test_parse_courseplan():
    qigong, sauna = parse_courseplan(HTML, "ELEMENTS München - Siemensallee")
    assert qigong.title == "QiGong" and qigong.id == "385558" and qigong.description == "Atmen."
    assert qigong.start.isoformat() == "2026-11-12T08:00:00+01:00" and qigong.end.isoformat() == "2026-11-12T09:00:00+01:00"
    assert qigong.instructor == "Nikolaus" and qigong.room == "R1" and qigong.booking
    assert sauna.id == "Sauna-Aufguss" and not sauna.booking


if __name__ == "__main__":
    test_parse_courseplan()
    print("ok")
