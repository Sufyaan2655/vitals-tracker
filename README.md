# Vitals Tracker

A personal heart-rate and breathing-rate tracker that reads your vital signs
straight out of a short webcam clip — no wearable, no extra hardware. It's
built on **remote photoplethysmography (rPPG)**: the same physical principle a
pulse oximeter uses (blood volume changes under the skin subtly change how
much light it reflects), except picked up by an ordinary camera instead of a
dedicated LED + sensor.

## How it actually works

**Heart rate.** Every heartbeat pushes a small pulse of blood through the
capillaries in your skin, which changes how much green light your skin
reflects by a tiny, invisible-to-the-eye amount. The app:

1. Detects your face in each frame (OpenCV Haar cascade) and tracks a forehead
   ROI (region of interest) — the most motion-stable, least occluded patch of
   skin.
2. Averages the green channel intensity of that ROI per frame, building a
   noisy time series.
3. Band-pass filters it to the physiologically plausible heart-rate range
   (42–240 bpm) with a Butterworth filter, which throws out lighting drift and
   most other noise.
4. Takes an FFT of the filtered signal and picks the dominant frequency — that
   frequency, converted to cycles/minute, is the heart rate.
5. Applies a subharmonic correction before finalizing the number. A pulse
   waveform isn't a clean sine wave — a sharp systolic upstroke followed by
   a slower diastolic decay — so it routinely carries strong energy at 2x
   the true pulse rate. Picking the tallest FFT bin can lock onto that
   second harmonic and report exactly double the real rate; if there's a
   comparably strong peak at half the frequency of the tallest bin, that
   half-frequency is treated as the true pulse rate instead. Caught with a
   real reference: an Apple Watch reading of ~55–60 bpm against this app's
   reported 120 bpm — a near-exact 2x error, the textbook symptom.
6. Doesn't trust one FFT over the whole clip. A single whole-clip spectrum
   has no defense against a bad moment — a webcam's auto-exposure or
   white-balance hunting for a couple of seconds, a flinch of motion — that
   can dominate the entire reading even when the rest of the clip is clean,
   which shows up as a reading that spikes even though the lighting "isn't
   too bad." Instead, the heart-rate estimate is the **median of several
   overlapping 6-second windows** across the clip: a transient artifact
   only corrupts the handful of windows it actually overlaps, and the
   majority of clean windows outvote it.

**Breathing rate.** Same idea, but using *motion* instead of *color*: breathing
causes small, slow shoulder/chest movement. The app computes optical flow in
the region below your face, band-passes it to the breathing range (6–36
breaths/min), and again takes the FFT peak.

**Confidence score.** Rather than just returning a number, the app reports how
much of the filtered signal's energy is actually concentrated in the expected
frequency band. A clean signal concentrates almost all its energy in one peak;
noise spreads it out. Low confidence usually means bad lighting, movement
during the clip, or the face wasn't tracked well — worth showing rather than
hiding.

## Dev mode

Confidence and bpm numbers alone don't explain *why* a reading came out the
way it did. Flip on "Dev mode" and the app shows its work both live and after
the fact:

- **Live tracking overlay** — while the camera is on (including while
  recording), a small overlay on the preview shows the current face box,
  forehead ROI (heart-rate source), and chest/shoulder ROI (breathing
  source), refreshed a few times a second. This calls a lightweight
  `POST /api/detect-face` endpoint that reuses the *exact same* detector and
  ROI math as the real pipeline (not a separate client-side detector), so
  what you see live can't drift from what a recorded clip is actually scored
  on.
- **Post-recording replay** — the just-recorded clip replayed with the same
  boxes drawn on top, frame-synced. Frames where face detection dropped out
  are flagged red (the pipeline reuses the last known box rather than
  skipping the frame — a real source of noise).
- **Raw vs. filtered signal charts** — the forehead green-channel series and
  chest optical-flow series, before and after the Butterworth band-pass, so
  you can see what the filter actually removed.
- **FFT spectra** — the same spectrum the bpm estimate is picked from, with
  the physiologically plausible band and the detected peak marked, so a
  suspiciously confident-but-wrong reading (e.g. a peak that's technically
  inside the band but clearly not a real pulse) is visible instead of hidden
  behind a single percentage.

None of this is persisted — the live overlay is stateless per-frame, and the
replay/signal data is returned only on the upload that requested it
(`dev_mode=true`), not stored with the session in SQLite.

## Accounts (optional)

Signing in is never required to try the tool — enable the camera and record
a clip works for anyone, with the result shown once and not saved. An
account only adds saved history and background monitoring; a "Sign in to
save history" link sits in the header rather than gating the app behind a
login screen. When you do sign in: real sign-up/sign-in, not a trusted
typed name — passwords are hashed with PBKDF2-HMAC-SHA256 (200k iterations,
random salt per user — stdlib only, no external auth dependency), and
sessions are a signed, HttpOnly cookie (30-day expiry) verified server-side
on every request. History is scoped to the authenticated account, so it's
actually private per person instead of namespaced by whatever string
someone typed in.

## Background monitoring

An opt-in mode (requires signing in, since it only makes sense tied to an
account) that records a short check-in clip on an interval (5–30 minutes,
configurable) while the tab stays open, and sends a browser notification if
a reading comes back unusually low (below the same resting-plausible
thresholds the plausibility badge uses). Worth being precise about what
this actually is: a recurring `getUserMedia` capture from an open browser
tab, **not** an OS-level background service. It needs the browser process
to keep running (the tab itself can be inactive or behind other windows)
and cannot check anything with the tab closed or the computer asleep —
stated plainly in the UI rather than oversold.

## Architecture

```
frontend/            served as static files (by the backend locally, by Vercel's static hosting in production)
  index.html         optional auth widget + camera capture UI + dev-mode overlay/charts + history
  app.js              optional auth + getUserMedia -> MediaRecorder -> upload -> render + background monitoring
  style.css

backend/
  vitals.py          the actual signal-processing pipeline (pure + testable)
  auth.py            password hashing (PBKDF2) + signed session tokens (stdlib only)
  main.py            FastAPI app: auth/upload/history/tags/delete endpoints
  models.py          SQLAlchemy ORM models: users, recording_sessions, tags (many-to-many), jobs
  database.py        engine/session setup - reads DATABASE_URL (Postgres in prod, SQLite for local dev)
  db.py              CRUD functions over the ORM models - no raw SQL, no per-call connect/close
  alembic/            versioned schema migrations (`alembic upgrade head`), not ad-hoc ALTER TABLE calls
  test_vitals.py     unit tests against synthetic sine waves (validates the math)
  test_auth.py       unit tests for password hashing + session tokens
  test_db.py          unit tests for the CRUD layer against an in-memory SQLite database
  test_api.py        end-to-end test hitting the real API with a synthetic clip
```

Persistence moved from hand-rolled `sqlite3` calls to SQLAlchemy over
whatever `DATABASE_URL` points at - Postgres in production, SQLite locally
with zero setup. Schema changes are Alembic migrations (`alembic revision
--autogenerate`, `alembic upgrade head`), not scripts that ALTER a table in
place and hope every environment ends up in the same state - see [Deploying
to Vercel](#deploying-to-vercel--postgres) below for the production setup.

**API surface:**
- `POST /api/auth/signup` / `POST /api/auth/login` / `POST /api/auth/logout`
  / `GET /api/auth/me` — account creation, sign-in, sign-out, and session
  check. Sets/clears the session cookie.
- `POST /api/sessions/upload` — `video`, optional `dev_mode` (`true`/`false`).
  Returns `{job_id, status}` immediately; the actual face detection/filtering/
  FFT work (the compute-heavy part of the app) runs as a background task
  rather than blocking the request. Works signed-out (the eventual result
  comes back with `"saved": false, "id": null` and nothing is written to the
  DB) or signed-in (saved to that account's history once the job finishes).
- `GET /api/jobs/{id}` — poll a processing job: `{status: pending|processing|
  done|failed, result, error}`. `result` is the same payload the upload
  endpoint used to return directly; `error` is set only if `status` is
  `failed`. Scoped to the owning account for a signed-in upload; an
  anonymous upload's job is reachable by anyone who has its id, matching the
  trust boundary the old synchronous response already had.
- `POST /api/detect-face` — a single JPEG frame in, `{detected, regions}`
  out; powers the live dev-mode overlay, stateless, nothing persisted.
- `POST /api/sessions/{id}/tags` — `name` (form field), attach a label like
  "resting" or "post-workout" to one of your own sessions. Creates the tag
  for your account the first time it's used; reusing a name attaches the
  same tag, not a duplicate. Returns the session's updated tag list.
- `DELETE /api/sessions/{id}/tags/{name}` — detach a tag from a session.
- `GET /api/tags` — every tag name you've ever created, for an autocomplete
  list rather than retyping "post-workout" identically every time.
- `GET /api/sessions` — the signed-in account's session history. Requires a
  session.
- `DELETE /api/sessions/{id}` — remove a session (scoped to the
  authenticated account). Requires a session.

**History view** also shows summary stats (session count, average HR/BR, HR
range), a confidence-over-time chart alongside the bpm trend chart, a
per-session table (flagging plausibility outliers), and a CSV export of the
full history.

Everything runs from one origin (`http://localhost:8000`) because `getUserMedia`
requires a "secure context," and browsers treat `localhost` as secure even
over plain HTTP — so there's no TLS setup needed for local use.

## Running it

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

No `DATABASE_URL` needed for local use — it defaults to a SQLite file
(`backend/vitals.db`), created automatically on first run. Then open
`http://localhost:8000` in a browser, allow camera access, and record a
15-second clip while sitting still and facing the camera in good, even
lighting — no account needed. Sign in (optional, top of the page) if you
want that clip and future ones saved so you can see a trend line build up
over time instead of just the one-off reading.

## Deploying to Vercel + Postgres

The app is deploy-ready for Vercel (`vercel.json` at the repo root routes
`/api/*` to the FastAPI app and everything else to the static `frontend/`
files), but it needs two things set as environment variables in the Vercel
project settings first - Vercel's own serverless filesystem doesn't persist
between invocations, so anything the app used to just write to a local file
has to come from configuration instead:

1. **`DATABASE_URL`** — a real Postgres connection string. The easiest path
   is Vercel's own Storage tab (Postgres, built on Neon) or connecting a
   Neon project directly; either way, Vercel injects the connection string
   for you. If it arrives as `postgres://...`, that's fine - `database.py`
   normalizes it to the `postgresql://` form SQLAlchemy 2.x expects.
2. **`SESSION_SECRET_KEY`** — a random secret for signing session cookies
   (`python -c "import secrets; print(secrets.token_hex(32))"` locally,
   paste the output in). Without this, `auth.py` falls back to a local
   file that won't survive between serverless invocations, silently
   invalidating sessions.

Then, from your own machine, point `DATABASE_URL` at that same production
database and run the migrations once (this creates the schema - it's the
one manual step, since Vercel doesn't run a build-time migration hook on
its own):

```bash
cd backend
DATABASE_URL="postgresql://..." alembic upgrade head
```

**Worth knowing before you rely on this in production**: this app was built
as an always-on server (a background job queue backed by a database row,
a signing secret persisted to disk), and Vercel's serverless model is a
different shape. Concretely: the background task that processes a video
after `/api/sessions/upload` responds still runs *inside* the same
function invocation (Starlette runs it before the invocation is considered
finished, even though the HTTP response was already sent) - which means a
slow clip can still hit Vercel's function execution time limit and get
killed mid-processing, leaving that one job stuck rather than failing
cleanly. A platform built for long-running processes (Render, Railway,
Fly.io) is a more natural fit for this app's actual shape; Vercel works,
with that caveat, because the schema is now normalized Postgres either way.

## Running the tests

```bash
cd backend
python -m pytest -v
```

`test_vitals.py` validates the actual math (band-pass filter + FFT peak
detection) against synthetic signals with a known, injected frequency — this
is the part that matters for correctness, independent of how good the face
detector is. `test_auth.py` validates password hashing round-trips, rejects
tampered/expired session tokens, and confirms salts are unique per hash.
`test_api.py` draws a synthetic face-like frame, encodes it into a real
video file, and pushes it through the actual HTTP API end to end (signing
up a throwaway account first, like a real browser would), confirming the
full auth → upload → process → store → respond path works.

## Known limitations (worth stating honestly — this matters for interviews)

- **Accuracy is "personal wellness," not medical-grade.** This is not a
  substitute for a pulse oximeter or ECG, and it says so nowhere near
  confidently enough to be used for any health decision. Frame it as a fun,
  educational rPPG demo, not a diagnostic tool.
- **Lighting and movement matter a lot.** Uneven or flickering light (e.g.
  fluorescent bulbs) and head movement during the clip both degrade the
  signal — this is inherent to the technique, not a bug.
- **Green-channel + FFT-peak is the simplest rPPG method that works.** More
  robust published methods exist (see roadmap below).

## Extension roadmap (a realistic few-weeks plan if you want to go deeper)

**Week 1 — better signal extraction.** Swap the plain green-channel method for
**CHROM** or **POS**, two well-known rPPG algorithms that combine all three
color channels in a way that's far more robust to lighting changes and skin
tone variation than a single channel. This is a meaningful, citable upgrade
(these are the methods real rPPG research papers benchmark against) and
mostly touches `vitals.py`.

**Week 1–2 — real Eulerian Video Magnification.** Right now the app *extracts
a number* from color changes; the original MIT Eulerian Video Magnification
technique goes further and produces a **video where you can visibly see the
pulse**, by building a Laplacian pyramid, band-pass filtering each pyramid
level over time, amplifying, and reconstructing. Adding an endpoint that
returns a magnified preview clip is a fantastic demo upgrade — arguably the
single best "wow" addition to this project.

**Week 2–3 — signal-quality-aware UI.** Show a live "tracking quality" meter
during recording (e.g., face-detection rate over the last second) so people
get real-time feedback instead of finding out after 15 seconds that the clip
was unusable.

**Week 3 — deployment.** Deploy behind real HTTPS (Caddy/Let's Encrypt is the
easy path) so it's usable outside `localhost`, and swap SQLite for Postgres if
you want multiple people using it concurrently. The session cookie already
sets `Secure` automatically based on the request's scheme (`https` → on,
`http://localhost` dev → off, no code change needed at deploy time), and
passwords are already properly hashed — this step is mostly about
rate-limiting login attempts and a password-reset flow before opening the
door to strangers.

**Stretch — validate against a real reference.** If you have access to a
pulse oximeter or fitness tracker with a heart-rate sensor, record simultaneous
readings and compute actual error statistics (mean absolute error, correlation)
between your rPPG estimate and ground truth. This turns "I built an rPPG demo"
into "I built an rPPG demo and measured it was accurate to within X bpm of a
reference sensor across N trials" — which is a dramatically stronger resume
line and a great thing to put in a README.
