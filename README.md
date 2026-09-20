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

## Accounts

Real sign-up/sign-in, not a trusted typed name: passwords are hashed with
PBKDF2-HMAC-SHA256 (200k iterations, random salt per user — stdlib only, no
external auth dependency), and sessions are a signed, HttpOnly cookie
(30-day expiry) verified server-side on every request. History is scoped to
the authenticated account, so it's actually private per person instead of
namespaced by whatever string someone typed in.

## Background monitoring

An opt-in mode that records a short check-in clip on an interval (5–30
minutes, configurable) while the tab stays open, and sends a browser
notification if a reading comes back unusually low (below the same
resting-plausible thresholds the plausibility badge uses). Worth being
precise about what this actually is: a recurring `getUserMedia` capture
from an open browser tab, **not** an OS-level background service. It needs
the browser process to keep running (the tab itself can be inactive or
behind other windows) and cannot check anything with the tab closed or the
computer asleep — stated plainly in the UI rather than oversold.

## Architecture

```
frontend/            served as static files by the backend itself
  index.html         auth screens + camera capture UI + dev-mode overlay/charts + history
  app.js              auth + getUserMedia -> MediaRecorder -> upload -> render + background monitoring
  style.css

backend/
  vitals.py          the actual signal-processing pipeline (pure + testable)
  auth.py            password hashing (PBKDF2) + signed session tokens (stdlib only)
  main.py            FastAPI app: auth/upload/history/delete endpoints
  db.py              SQLite persistence: users + per-user sessions
  test_vitals.py     unit tests against synthetic sine waves (validates the math)
  test_auth.py       unit tests for password hashing + session tokens
  test_api.py        end-to-end test hitting the real API with a synthetic clip
```

**API surface:**
- `POST /api/auth/signup` / `POST /api/auth/login` / `POST /api/auth/logout`
  / `GET /api/auth/me` — account creation, sign-in, sign-out, and session
  check. Sets/clears the session cookie.
- `POST /api/sessions/upload` — `video`, optional `dev_mode` (`true`/`false`)
  to also return the tracking/signal debug payload above. Requires a signed-
  in session.
- `POST /api/detect-face` — a single JPEG frame in, `{detected, regions}`
  out; powers the live dev-mode overlay, stateless, nothing persisted.
- `GET /api/sessions` — the signed-in account's session history.
- `DELETE /api/sessions/{id}` — remove a session (scoped to the
  authenticated account).

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

Then open `http://localhost:8000` in a browser, create an account (or sign
in), allow camera access, and record a 15-second clip while sitting still
and facing the camera in good, even lighting. Record a few clips over
different days/times to see your trend line build up.

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
