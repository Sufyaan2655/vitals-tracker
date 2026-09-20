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
way it did. Flip on "Dev mode" before recording a clip and the app also
returns, and renders:

- **Tracking overlay** — the just-recorded clip replayed with the actual face
  bounding box, forehead ROI (the heart-rate source region), and chest/shoulder
  ROI (the breathing source region) drawn on top, frame-synced. Frames where
  face detection dropped out are flagged red (the pipeline reuses the last
  known box rather than skipping the frame — a real source of noise).
- **Raw vs. filtered signal charts** — the forehead green-channel series and
  chest optical-flow series, before and after the Butterworth band-pass, so
  you can see what the filter actually removed.
- **FFT spectra** — the same spectrum the bpm estimate is picked from, with
  the physiologically plausible band and the detected peak marked, so a
  suspiciously confident-but-wrong reading (e.g. a peak that's technically
  inside the band but clearly not a real pulse) is visible instead of hidden
  behind a single percentage.

This data isn't persisted — it's returned only on the upload that requested
it (`dev_mode=true`), not stored with the session in SQLite.

## Architecture

```
frontend/            served as static files by the backend itself
  index.html         camera capture UI + dev-mode overlay/charts + history
  app.js              getUserMedia -> MediaRecorder -> upload -> render
  style.css

backend/
  vitals.py          the actual signal-processing pipeline (pure + testable)
  main.py            FastAPI app: upload/history/delete endpoints
  db.py              SQLite persistence, one row per session
  test_vitals.py     unit tests against synthetic sine waves (validates the math)
  test_api.py        end-to-end test hitting the real API with a synthetic clip
```

**API surface:**
- `POST /api/sessions/upload` — `video` + `username`, optional `dev_mode`
  (`true`/`false`) to also return the tracking/signal debug payload above.
- `GET /api/sessions?username=X` — session history.
- `DELETE /api/sessions/{id}?username=X` — remove a session (scoped to the
  matching username, same no-real-auth trust model as everything else).

**History view** also shows summary stats (session count, average HR/BR, HR
range), a confidence-over-time chart alongside the bpm trend chart, a
per-session table, and a CSV export of the full history.

Everything runs from one origin (`http://localhost:8000`) because `getUserMedia`
requires a "secure context," and browsers treat `localhost` as secure even
over plain HTTP — so there's no TLS setup needed for local use.

## Running it

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

Then open `http://localhost:8000` in a browser, allow camera access, enter a
name, and record a 15-second clip while sitting still and facing the camera in
good, even lighting. Record a few clips over different days/times to see your
trend line build up.

## Running the tests

```bash
cd backend
python -m pytest -v
```

`test_vitals.py` validates the actual math (band-pass filter + FFT peak
detection) against synthetic signals with a known, injected frequency — this
is the part that matters for correctness, independent of how good the face
detector is. `test_api.py` draws a synthetic face-like frame, encodes it into a
real video file, and pushes it through the actual HTTP API end to end,
confirming the full upload → process → store → respond path works.

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

**Week 2 — real accounts.** Swap the plain `username` string for actual
signup/login (hashed passwords or OAuth) so sessions are properly private
per-user instead of trusted to whatever name someone types in.

**Week 2–3 — signal-quality-aware UI.** Show a live "tracking quality" meter
during recording (e.g., face-detection rate over the last second) so people
get real-time feedback instead of finding out after 15 seconds that the clip
was unusable.

**Week 3 — deployment.** Deploy behind real HTTPS (Caddy/Let's Encrypt is the
easy path) so it's usable outside `localhost`, and swap SQLite for Postgres if
you want multiple people using it concurrently.

**Stretch — validate against a real reference.** If you have access to a
pulse oximeter or fitness tracker with a heart-rate sensor, record simultaneous
readings and compute actual error statistics (mean absolute error, correlation)
between your rPPG estimate and ground truth. This turns "I built an rPPG demo"
into "I built an rPPG demo and measured it was accurate to within X bpm of a
reference sensor across N trials" — which is a dramatically stronger resume
line and a great thing to put in a README.
