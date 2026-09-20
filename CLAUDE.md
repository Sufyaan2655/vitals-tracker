# Vitals Tracker — project handoff

This file is written for an AI coding agent (Claude Code) picking up this
project cold, with no prior conversation history. It describes what exists,
why it's built the way it is, what's verified vs. assumed, and what's next.

## What this is

A personal web app that estimates **heart rate** and **breathing rate** from
a short webcam clip — no wearable or extra hardware. It's built on **remote
photoplethysmography (rPPG)**: a heartbeat causes tiny, invisible-to-the-eye
changes in facial skin color as blood volume shifts under the skin (the same
principle a pulse-oximeter LED+sensor uses, here picked up by an ordinary
camera instead). Breathing is estimated from chest/shoulder motion via
optical flow. This started as a resume/portfolio project for a CS student
applying to SWE internships — the target audience for polish is "technically
impressive to an interviewer, demoable to a non-technical person."

## Current status (as of handoff)

- Core pipeline, backend, frontend, and tests are all written and were
  verified working (16/16 tests passing) in a Linux dev environment.
- The user is running it locally on **Windows** and hit one real bug (see
  "Known issues" below) which is already fixed in the committed code.
- Code is committed to local git (`main` branch) and a remote `origin` is
  set to `https://github.com/Sufyaan2655/vitals-tracker.git`, but as of this
  handoff **it may not be pushed yet** — check `git log origin/main` vs
  `git log main` and push if they've diverged. No CI/CD exists.
- The app has **not yet been validated against a real reference sensor**
  (pulse oximeter, fitness tracker) — accuracy is plausible based on the
  signal processing being textbook-correct and passing synthetic tests, but
  real-world accuracy on an actual face has not been measured. This is the
  single most valuable next step for making this resume-ready (see roadmap).

## Repository layout

```
README.md              user-facing docs: architecture, how rPPG works, run instructions, roadmap
backend/
  vitals.py             the signal-processing pipeline (this is the core IP of the project)
  main.py                FastAPI app: upload endpoint, history endpoint, serves frontend statics
  db.py                  SQLite persistence (one table: sessions)
  test_vitals.py         unit tests: synthetic sine waves validate the filter+FFT math
  test_api.py             end-to-end test: synthetic video through the real HTTP API
  requirements.txt        pinned deps (see "Known issues" for why opencv is pinned exactly)
frontend/
  index.html              camera capture UI + results + trends chart
  app.js                  getUserMedia -> MediaRecorder -> upload -> render chart (Chart.js via CDN)
  style.css               dark-theme styling
```

Note: on the user's disk this currently sits one level deeper than expected
(`.../vitals-tracker/vitals-tracker/...`) because of how a zip was extracted
— the inner `vitals-tracker/` folder is the actual git repo root. Worth
flattening at some point but not urgent.

## How the core algorithm works (backend/vitals.py)

**Heart rate:**
1. Detect a face per frame with OpenCV's Haar cascade
   (`haarcascade_frontalface_default.xml`, bundled with opencv).
2. Crop a forehead ROI (a sub-rectangle of the face bbox — most motion-stable,
   least occluded skin region).
3. Average the green channel of that ROI per frame → a noisy time series.
   Green is used because it carries the strongest blood-volume-pulse signal
   for typical RGB sensors — this is the simplest rPPG method that works; see
   roadmap for the more robust CHROM/POS alternative.
4. `detrend()` to remove slow lighting drift, then a Butterworth band-pass
   filter to 0.7–4.0 Hz (42–240 bpm — the physiologically plausible range).
5. FFT the filtered signal, take the dominant frequency, convert to bpm.

**Breathing rate:** same shape, but using **motion** instead of color —
Farneback optical flow's vertical component in the region below the face
(chest/shoulders), band-passed to 0.1–0.6 Hz (6–36 breaths/min), FFT peak.

**Confidence score:** fraction of the filtered signal's spectral energy that
falls inside the expected band. Clean periodic signals concentrate energy in
one peak; noise spreads it out. This is reported to the user rather than
hidden — low confidence usually means bad lighting or movement during
capture, which is a real limitation of the technique, not a bug to silently
paper over.

All of this logic is split so it's testable without a real face video:
`bandpass_filter`/`dominant_frequency_bpm`/`signal_quality` are pure math,
tested against synthetic sine waves with known injected frequencies.
`extract_signals_from_frames` takes frames + a supplied bbox (no detector
needed) and is tested with synthetic painted frames. `process_video` is the
real end-to-end entry point that adds face detection on top, and is only
exercised by the API-level test (`test_api.py`), which draws an actual
synthetic face-like image (circle + eyes + mouth) that OpenCV's Haar cascade
reliably detects, so that test genuinely exercises the full pipeline
including detection, not just the math.

## Architecture / API surface

- `POST /api/sessions/upload` — multipart form: `video` (webm/mp4 blob) +
  `username` (plain string, no auth). Returns heart rate, breathing rate,
  confidence scores, fps, frame count, face-detection rate. Stores the
  result in SQLite keyed by username.
- `GET /api/sessions?username=X` — returns that user's session history,
  ordered by time, for the trends chart.
- Frontend is served by FastAPI itself as static files from `/`, so the
  whole app is same-origin at `http://localhost:8000`. This matters:
  `getUserMedia` (camera access) requires a "secure context," and browsers
  treat `localhost` as secure even over plain HTTP, so no TLS setup is
  needed for local use. Don't "fix" this by splitting frontend/backend onto
  different ports without adding HTTPS or you'll break camera access.
- No authentication yet — `username` is a trusted, unvalidated string typed
  into a text field. Fine for a personal/demo tool, not fine as shipped to
  strangers (see roadmap).

## Known issues already hit and fixed

**opencv-python-headless version pin.** `requirements.txt` originally said
`opencv-python-headless>=4.9`. On the user's Windows machine, pip resolved
that to `5.0.0.93` (a very new major-version release at the time), which has
a broken native extension on Windows — `cv2.CascadeClassifier` and other
bindings don't load (symptom: `AttributeError: module 'cv2' has no attribute
'CascadeClassifier'`). Fixed by pinning to `opencv-python-headless==4.13.0.92`
exactly, which is the version this whole project was built and tested
against. **Do not loosen this pin to `>=` without re-verifying on Windows** —
if this keeps recurring across environments, that's a sign to switch to
`opencv-contrib-python-headless` or vendor a known-good wheel instead of
trusting `>=` resolution.

**Windows pip install failures** (`WinError 2 ... .deleteme`) were hit before
the opencv issue, caused by pip trying to replace an in-use script in a
global Python install. Resolved by using a venv (`python -m venv venv`)
instead of installing globally — the repo assumes venv-based setup going
forward.

## Extension roadmap (priority order)

1. **Validate against a real reference sensor.** If the user has a pulse
   oximeter or fitness tracker, record simultaneous ground-truth readings
   and compute actual error stats (mean absolute error, correlation) against
   this app's output across several trials. This is the highest-value next
   step — it turns "I built an rPPG demo" into "I measured mine accurate to
   within X bpm of a reference sensor across N trials," which is a much
   stronger thing to put in a README/resume than an unvalidated demo.
2. **CHROM or POS rPPG method.** Replace the plain green-channel extraction
   in `estimate_vitals_from_signals`/`extract_signals_from_frames` with one
   of these two well-known, more lighting/skin-tone-robust rPPG algorithms.
   This is a self-contained change mostly confined to `vitals.py`, and is
   the kind of upgrade that's directly citable against real rPPG literature.
3. **Real Eulerian Video Magnification.** Currently the app extracts a
   *number* from color change. The original MIT technique produces a
   *video* where the pulse is visibly amplified (Laplacian pyramid,
   temporal band-pass per level, amplify, reconstruct). Adding an endpoint
   that returns a magnified preview clip would be the strongest demo
   addition — arguably worth doing before #2.
4. **Real auth.** Replace the plain `username` string with actual
   signup/login (hashed passwords or OAuth) so session history is properly
   private per person instead of trusted to whatever name is typed in.
5. **Live tracking-quality feedback** during recording (e.g. show
   face-detection rate in the last second) so a bad recording is obvious
   before the 15 seconds finish, not after.
6. **Deployment.** HTTPS via Caddy/Let's Encrypt if it needs to run
   somewhere other than localhost; swap SQLite for Postgres if it needs to
   support concurrent multi-user traffic.

## Working conventions established so far

- Tests are meant to validate *actual correctness* (synthetic signals with
  known injected frequencies), not just "does it run" — keep that bar for
  any new signal-processing code.
- Honesty about limitations is treated as a feature, not a weakness to hide
  — the README and this file both say plainly where the technique is weak
  (lighting, movement, "wellness not medical-grade"). Keep that tone in any
  new docs.
- Dependencies are pinned exactly where a version bump has already caused a
  real breakage (see opencv above); everything else uses `>=` and should
  probably stay loose unless another concrete breakage is found.
