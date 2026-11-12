# Fitness Calendar

Turns gym class schedules into calendar feeds you can subscribe to (iPhone, macOS, Google Calendar). Supports all **FitX** studios, **Fitness First** clubs, **ELEMENTS** studios and **body + soul** centers (via the Eversports schedule API their site embeds). Free, no tracking: fonts are self-hosted and nothing on the page calls a third party. Each subscriber picks the courses they care about and gets their own feed URL. Runs on a Raspberry Pi with Docker.

## How it works
- Studios are fetched on demand: the first time someone picks a studio (or a calendar app requests its feed), its schedule is scraped and cached in `/data/<calendar>.json`.
- Once cached data is older than `REFRESH_INTERVAL_SECONDS` (15 min), the next request serves it immediately and re-fetches in the background. Studios nobody uses are never fetched.
- body + soul (Eversports) and Fitness First (the JSON behind fitnessfirst.de/kurse/kursplan, 7 days ahead) classes come with description, level and category. ELEMENTS (the course plan HTML elements.com/kursplan/ loads via admin-ajax, ~10 days ahead) has description, trainer and room. For FitX, each course's page is fetched once and cached for a week in `/data/fitx-courses.json`. The picker groups courses by category and shows the details; calendar events include them in their notes.
- If a gym's site is down or returns nothing, the last good data keeps being served.
- Courses that need a reserved spot (body + soul: bookable on Eversports; FitX: limited "Plätze frei") get a 🎟 in the event title and a "Booking required" note. Fitness First (app booking) and ELEMENTS (sign-up or waiting list) classes always do. For body + soul the event link is the Eversports page with the **Book now** button; for FitX it's the course info page, not a booking page.
- Feeds are generated on request, so filtering costs nothing. The only stored state is anonymous subscription stats in `/data/stats.json` (calendar, chosen courses, calendar app; no IPs). Subscribe links carry a random `sub` id so each subscription counts once.

## Using it
Open the site root (e.g. `https://ics.moraleda.work/` or `http://<pi-ip>:8787/`):

1. Pick a gym, then a studio from its dropdown (the choice is remembered in your browser).
2. Tap the courses you want. Nothing selected = all courses. The search box only narrows the list; it doesn't affect the link.
3. Tap **Subscribe** (or **Copy link**). Changing the selection gives a new link; an existing subscription keeps its old filter.

## Endpoints
- `GET /` – course picker
- `GET /fitx-<studio-id>.ics` – FitX feed, e.g. `/fitx-53.ics` (id = `value` of the studio dropdown on [fitx.de/fitnesskurse](https://www.fitx.de/fitnesskurse); 53 = München Sendling-Westpark)
- `GET /bodyandsoul-<center>.ics` – body + soul feed, e.g. `/bodyandsoul-sendling.ics` (centers in `BODYSOUL_CENTERS` in `app/main.py`; their Eversports widget ids, from `data-eversports-widget-id` on `bodyandsoul.de/class-schedule-<center>/`, in `app/bodyandsoul.py`)
- `GET /fitnessfirst-<club-id>.ics` – Fitness First feed, e.g. `/fitnessfirst-3016.ics` (ids from `drupalSettings.courseSchedule.allClubsData` on [fitnessfirst.de/kurse/kursplan](https://www.fitnessfirst.de/kurse/kursplan); 3016 = München Am Marienplatz)
- `GET /elements-<studio>.ics` – ELEMENTS feed, e.g. `/elements-siemensallee.ics` (studios in `STUDIOS` in `app/elements.py`)
- `GET /api/courses/<calendar>` – course titles for the picker
- Add `?course=<title>` (repeatable, exact title) to any feed to include only those courses
- `GET /stats` – active subscriptions (feeds fetched in the last 14 days) per studio, course and calendar app
- `GET /health` – `OK`

## Configuration (`.env`)
| Variable | Default | |
|---|---|---|
| `FITX_WEEKS_AHEAD` | `2` | Weeks of FitX classes to fetch (body + soul: 4 weeks). |
| `REFRESH_INTERVAL_SECONDS` | `900` | Max age of a studio's cached classes before re-fetching. |
| `FITX_COOKIE` | – | Optional, never logged. Secret. |
| `NTFY_URL` | – | [ntfy](https://ntfy.sh) topic URL for alerts, e.g. `https://ntfy.sh/<random>`. Use a hard-to-guess topic: anyone who knows it can read it. Unset = no alerts. |
| `CLOUDFLARED_TOKEN` | – | Cloudflare Tunnel token for the `cloudflared` sidecar. Secret. |
| `PORT` | `8787` | Host port (container always listens on 8787). |
| `TZ` | `Europe/Berlin` | |

## Running on the Pi
The Pi (armv7) runs the app plus a Cloudflare Tunnel sidecar from `~/fitx-calendar`:

```sh
cp .env.example .env   # then edit
docker compose -f docker-compose.armv7.yml -f docker-compose.armv7.cloudflared.yml up -d --build
```

`scripts/bootstrap_fitx_pi.sh` automates first-time setup.

### Deploying an update
The Pi has no GitHub access, so push from your machine straight to the Pi's checkout, then rebuild:

```sh
git push ssh://pi@<pi-ip>/home/pi/fitx-calendar main:refs/remotes/origin/main
ssh pi@<pi-ip> 'cd ~/fitx-calendar && git merge --ff-only origin/main && \
  docker compose -f docker-compose.armv7.yml -f docker-compose.armv7.cloudflared.yml up -d --build'
```

### Public access (Cloudflare Tunnel)
With `CLOUDFLARED_TOKEN` set, the tunnel exposes the app at the public hostname configured in the Cloudflare dashboard (pointing to `http://fitx-calendar:8787`). Without a token, cloudflared falls back to a quick tunnel; find its `https://<random>.trycloudflare.com` URL with `docker logs cloudflared`.

## Alerts
With `NTFY_URL` set, subscribe to that topic in the ntfy app to get a push when:
- a studio's refresh starts failing (error, or no courses at all: the usual sign of a site redesign breaking a parser), and again when it recovers. One alert per outage, not one per retry. Only studios someone uses get refreshed, so only those alert.
- the FitX or Fitness First studio list can't be loaded or comes back empty (retried, and re-alerted, once a day).

The app can't report its own downtime; point an external uptime check (e.g. UptimeRobot) at `/health` for that.

## Troubleshooting
- **No courses for a studio / feed stale:** check `docker logs fitx-calendar` for `Refresh of <calendar> failed`. A failure keeps the last good data and retries after `REFRESH_INTERVAL_SECONDS`; no courses means no successful fetch yet.
- **Parser broke after a site redesign:** FitX parsing lives in `app/parser.py`, body + soul in `app/bodyandsoul.py`, Fitness First in `app/fitnessfirst.py`, ELEMENTS in `app/elements.py`.

## Development
Python 3.11, FastAPI, httpx, BeautifulSoup. ICS is generated by hand (`app/ics.py`).

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
DATA_DIR=./.data .venv/bin/uvicorn app.main:app --port 8787
for t in tests/test_*.py; do .venv/bin/python -m tests.$(basename $t .py); done
```

To try the Docker image locally without starting the Cloudflare tunnel (don't `docker compose up` with the real `.env`):

```sh
docker build -t fitx-calendar:local . && docker run --rm -p 8787:8787 fitx-calendar:local
```
