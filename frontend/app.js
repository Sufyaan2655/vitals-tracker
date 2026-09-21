const API_BASE = ""; // same-origin: FastAPI serves this file itself

// A server error that never reaches a route handler (a bad cookie during
// auth, a dropped connection, a proxy in front of it) can come back as
// plain text instead of JSON - res.json() then throws its own cryptic
// "Unexpected token" parse error instead of the actual problem. Read the
// body as text first and only parse it, so a non-JSON response still
// surfaces something readable.
async function safeJson(res) {
  const text = await res.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(text.slice(0, 200) || `Server error (${res.status})`);
  }
}

// Video processing runs as a background job (see main.py) rather than
// blocking the upload request - the upload returns a job id immediately,
// and this polls /api/jobs/{id} until it lands on done or failed.
async function pollJob(jobId, { intervalMs = 700, timeoutMs = 60000, onTick } = {}) {
  const deadline = Date.now() + timeoutMs;
  const startedAt = Date.now();
  while (Date.now() < deadline) {
    const res = await authedFetch(`${API_BASE}/api/jobs/${jobId}`);
    const job = await safeJson(res);
    if (!res.ok) throw new Error(job.detail || "Couldn't check processing status");
    if (job.status === "done") return job.result;
    if (job.status === "failed") throw new Error(job.error || "Processing failed");
    if (onTick) onTick(Date.now() - startedAt);
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error("Processing is taking longer than expected - try again");
}

const authToggleBtn = document.getElementById("authToggleBtn");
const authPanel = document.getElementById("authPanel");
const authBackdrop = document.getElementById("authBackdrop");
const accountBar = document.getElementById("accountBar");
const accountName = document.getElementById("accountName");
const signOutBtn = document.getElementById("signOutBtn");
const authForm = document.getElementById("authForm");
const authTabs = Array.from(document.querySelectorAll(".auth-tab"));
const authUsername = document.getElementById("authUsername");
const authPassword = document.getElementById("authPassword");
const authSubmit = document.getElementById("authSubmit");
const authStatus = document.getElementById("authStatus");

const saveHint = document.getElementById("saveHint");
const saveHintSignInBtn = document.getElementById("saveHintSignInBtn");
const historySignedOut = document.getElementById("historySignedOut");
const historySignedIn = document.getElementById("historySignedIn");
const historySignInBtn = document.getElementById("historySignInBtn");
const monitorSignedOut = document.getElementById("monitorSignedOut");
const monitorControls = document.getElementById("monitorControls");
const monitorSignInBtn = document.getElementById("monitorSignInBtn");

const preview = document.getElementById("preview");
const startCameraBtn = document.getElementById("startCamera");
const recordBtn = document.getElementById("recordBtn");
const devModeToggle = document.getElementById("devMode");
const countdownEl = document.getElementById("countdown");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const hrValue = document.getElementById("hrValue");
const hrConfidence = document.getElementById("hrConfidence");
const hrPlausibility = document.getElementById("hrPlausibility");
const brValue = document.getElementById("brValue");
const brConfidence = document.getElementById("brConfidence");
const brPlausibility = document.getElementById("brPlausibility");
const noHistoryEl = document.getElementById("noHistory");
const historyTableWrap = document.getElementById("historyTableWrap");
const historyTableBody = document.getElementById("historyTableBody");
const historyStatsEl = document.getElementById("historyStats");
const exportCsvBtn = document.getElementById("exportCsv");

const devInsightsEl = document.getElementById("devInsights");
const replayVideo = document.getElementById("replayVideo");
const overlayCanvas = document.getElementById("overlayCanvas");
const faceDropoutWarning = document.getElementById("faceDropoutWarning");

const liveOverlayCanvas = document.getElementById("liveOverlayCanvas");
const liveTrackingStatus = document.getElementById("liveTrackingStatus");

const monitorToggle = document.getElementById("monitorToggle");
const monitorInterval = document.getElementById("monitorInterval");
const monitorStatus = document.getElementById("monitorStatus");

const RECORD_SECONDS = 15;
// Keeps a 15s clip well under Vercel's ~4.5MB request body limit regardless
// of what a given device's camera/encoder would otherwise pick - phone
// cameras in particular default to a much higher bitrate than this app's
// low-res forehead-ROI signal extraction actually needs.
const RECORD_VIDEO_BITS_PER_SECOND = 800_000;
// Must match backend/vitals.py's HEART_RATE_BAND_HZ / BREATHING_BAND_HZ - the
// (deliberately wide) range the FFT peak is picked from, so it can find a
// signal at all.
const HEART_RATE_BAND_BPM = [0.7 * 60, 4.0 * 60];
const BREATHING_BAND_BPM = [0.1 * 60, 0.6 * 60];
// Must match backend/vitals.py's HEART_RATE_PLAUSIBLE_BPM /
// BREATHING_RATE_PLAUSIBLE_BPM - the narrower resting-plausible sub-range
// used only to decide the direction (too low/too high) of the plausibility
// badge copy; the backend's `_plausible` boolean is what actually gates
// whether the badge shows at all.
const HEART_RATE_PLAUSIBLE_BPM = [45, 140];
const BREATHING_RATE_PLAUSIBLE_BPM = [8, 28];

// Read the palette from CSS custom properties rather than hardcoding hex
// values here too - style.css's :root is the single source of truth, so
// canvas/Chart.js colors can never drift out of sync with the theme.
const rootStyle = getComputedStyle(document.documentElement);
const cssVar = (name) => rootStyle.getPropertyValue(name).trim();
const COLORS = {
  text: cssVar("--text"),
  textDim: cssVar("--text-dim"),
  accent: cssVar("--accent"),
  accent2: cssVar("--accent-2"),
  success: cssVar("--success"),
  warning: cssVar("--warning"),
  danger: cssVar("--danger"),
  border: cssVar("--border"),
};

let mediaStream = null;
let mediaRecorder = null;
let recordedChunks = [];
let recordedMimeType = "video/webm";
let cameraOn = false;
let isRecording = false;
let chart = null;
let confidenceChart = null;

// Privacy: a camera left on with nobody actively using the page is worse
// than one that switches itself off - auto-stop after a stretch of no
// interaction rather than trusting the person to remember to hit "Disable
// camera". Doesn't apply mid-recording (obviously) or while background
// monitoring is running, since sitting on between check-ins is that
// feature's whole point, not an oversight.
const CAMERA_IDLE_TIMEOUT_MS = 25_000;
let cameraIdleTimer = null;

function resetCameraIdleTimer() {
  if (cameraIdleTimer) {
    clearTimeout(cameraIdleTimer);
    cameraIdleTimer = null;
  }
  if (!cameraOn || monitorToggle.checked) return;
  cameraIdleTimer = setTimeout(() => {
    if (isRecording) {
      resetCameraIdleTimer(); // don't kill the stream mid-clip - just wait it out
      return;
    }
    if (cameraOn && !monitorToggle.checked) {
      stopCamera();
      setStatus("Camera turned off automatically after 25s of inactivity.");
    }
  }, CAMERA_IDLE_TIMEOUT_MS);
}

["mousemove", "mousedown", "keydown", "touchstart", "scroll"].forEach((evt) => {
  document.addEventListener(evt, resetCameraIdleTimer, { passive: true });
});

let debugData = null;
let debugFps = 30;
let replayObjectUrl = null;
let overlayLoopActive = false;
const devCharts = {};

let liveTrackingTimer = null;
let liveDetectInFlight = false;
const liveCaptureCanvas = document.createElement("canvas");

// Remember the dev-mode preference across visits (this is a normal local
// web app running in the user's own browser, not an embedded preview, so
// localStorage behaves normally here).
try {
  devModeToggle.checked = localStorage.getItem("vitals_dev_mode") === "1";
} catch (e) {
  /* private browsing / storage disabled - not fatal, just skip persistence */
}

devModeToggle.addEventListener("change", () => {
  try {
    localStorage.setItem("vitals_dev_mode", devModeToggle.checked ? "1" : "0");
  } catch (e) {}
  if (devModeToggle.checked && cameraOn) {
    startLiveTracking();
  } else {
    stopLiveTracking();
  }
});

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.classList.toggle("error", isError);
}

// ---------------------------------------------------------------------------
// Auth: sign in / create account / sign out - entirely optional. Recording a
// clip and seeing the result works with nobody signed in; signing in only
// adds saved history and background monitoring. Session state lives in an
// HttpOnly cookie the server sets - fetch() sends it automatically on
// same-origin requests.
// ---------------------------------------------------------------------------

let isSignedIn = false;
let authMode = "login";

function setAuthMode(mode) {
  authMode = mode;
  authTabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.mode === mode));
  authSubmit.textContent = mode === "login" ? "Sign in" : "Create account";
  authPassword.autocomplete = mode === "login" ? "current-password" : "new-password";
  authStatus.textContent = "";
  authStatus.classList.remove("error");
}

authTabs.forEach((tab) => {
  tab.addEventListener("click", () => setAuthMode(tab.dataset.mode));
});

function openAuthPanel() {
  authPanel.classList.remove("hidden");
  authBackdrop.classList.remove("hidden");
  authUsername.focus();
}

function closeAuthPanel() {
  authPanel.classList.add("hidden");
  authBackdrop.classList.add("hidden");
}

authToggleBtn.addEventListener("click", () => {
  authPanel.classList.contains("hidden") ? openAuthPanel() : closeAuthPanel();
});
saveHintSignInBtn.addEventListener("click", openAuthPanel);
historySignInBtn.addEventListener("click", openAuthPanel);
monitorSignInBtn.addEventListener("click", openAuthPanel);

document.addEventListener("click", (e) => {
  if (!authPanel.classList.contains("hidden") && !e.target.closest(".auth-widget")) {
    closeAuthPanel();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !authPanel.classList.contains("hidden")) closeAuthPanel();
});

// Below 700px the nav bar drops Home/How it works/About to leave room for
// the auth widget - this menu is how they stay reachable rather than only
// discoverable by scrolling.
const navMenuToggle = document.getElementById("navMenuToggle");
const navMenuPanel = document.getElementById("navMenuPanel");

function closeNavMenu() {
  navMenuPanel.classList.add("hidden");
  navMenuToggle.setAttribute("aria-expanded", "false");
}

navMenuToggle.addEventListener("click", () => {
  const opening = navMenuPanel.classList.contains("hidden");
  navMenuPanel.classList.toggle("hidden", !opening);
  navMenuToggle.setAttribute("aria-expanded", String(opening));
});
navMenuPanel.querySelectorAll(".nav-link").forEach((link) => {
  link.addEventListener("click", closeNavMenu);
});
document.addEventListener("click", (e) => {
  if (!navMenuPanel.classList.contains("hidden") && !e.target.closest("#navMenuToggle, #navMenuPanel")) {
    closeNavMenu();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !navMenuPanel.classList.contains("hidden")) closeNavMenu();
});

authForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = authUsername.value.trim();
  const password = authPassword.value;
  if (!username || !password) return;

  authSubmit.disabled = true;
  authStatus.textContent = "";
  authStatus.classList.remove("error");

  try {
    const endpoint = authMode === "login" ? "/api/auth/login" : "/api/auth/signup";
    const fd = new FormData();
    fd.append("username", username);
    fd.append("password", password);
    const res = await fetch(`${API_BASE}${endpoint}`, { method: "POST", body: fd });
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data.detail || "Something went wrong");
    closeAuthPanel();
    applySignedInState(data.username);
  } catch (err) {
    authStatus.textContent = err.message;
    authStatus.classList.add("error");
  } finally {
    authSubmit.disabled = false;
  }
});

signOutBtn.addEventListener("click", async () => {
  try {
    await fetch(`${API_BASE}/api/auth/logout`, { method: "POST" });
  } catch (e) {}
  stopBackgroundMonitor();
  applySignedOutState();
});

function applySignedInState(username) {
  isSignedIn = true;
  authToggleBtn.classList.add("hidden");
  accountBar.classList.remove("hidden");
  accountName.textContent = username;
  historySignedOut.classList.add("hidden");
  historySignedIn.classList.remove("hidden");
  monitorSignedOut.classList.add("hidden");
  monitorControls.classList.remove("hidden");
  saveHint.classList.add("hidden");
  authUsername.value = "";
  authPassword.value = "";
  refreshHistory();
  restoreBackgroundMonitorPreference();
}

function applySignedOutState() {
  isSignedIn = false;
  authToggleBtn.classList.remove("hidden");
  accountBar.classList.add("hidden");
  historySignedOut.classList.remove("hidden");
  historySignedIn.classList.add("hidden");
  monitorSignedOut.classList.remove("hidden");
  monitorControls.classList.add("hidden");
}

async function checkAuth() {
  try {
    const res = await fetch(`${API_BASE}/api/auth/me`);
    if (!res.ok) {
      applySignedOutState();
      return;
    }
    const data = await safeJson(res);
    applySignedInState(data.username);
  } catch (err) {
    applySignedOutState();
  }
}

// A 401 from a call that actually requires an account (history, delete)
// means the session cookie expired or was cleared elsewhere - fall back to
// the signed-out state for those sections. Recording itself never 401s;
// uploading works signed-out on purpose.
async function authedFetch(url, options) {
  const res = await fetch(url, options);
  if (res.status === 401) {
    applySignedOutState();
  }
  return res;
}

// ---------------------------------------------------------------------------
// Camera: enable/disable toggle.
// ---------------------------------------------------------------------------

startCameraBtn.addEventListener("click", () => {
  if (cameraOn) {
    stopCamera();
  } else {
    enableCamera();
  }
});

// Awaitable, reusable by both the button and background monitoring (which
// needs to know whether the camera actually came on before scheduling
// checks - a fire-and-forget button.click() can't be awaited for that).
async function enableCamera() {
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      // `max` (not just `ideal`) matters on phones - a bare ideal hint is
      // routinely ignored in favor of the camera's native resolution, which
      // on a phone can be several times a laptop webcam's, ballooning the
      // recorded file for no benefit (rPPG only needs a small, stable
      // forehead ROI, not high resolution).
      video: { width: { ideal: 640, max: 640 }, height: { ideal: 480, max: 480 }, facingMode: "user" },
      audio: false,
    });
    preview.srcObject = mediaStream;
    cameraOn = true;
    recordBtn.disabled = false;
    startCameraBtn.textContent = "Disable camera";
    setStatus("Camera ready. Sit still, face well-lit, then record a clip.");
    if (devModeToggle.checked) startLiveTracking();
    resetCameraIdleTimer();
    return true;
  } catch (err) {
    setStatus(
      "Couldn't access the camera. Check browser permissions and that no other app is using it.",
      true
    );
    return false;
  }
}

function stopCamera() {
  if (mediaStream) {
    mediaStream.getTracks().forEach((t) => t.stop());
    mediaStream = null;
  }
  preview.srcObject = null;
  cameraOn = false;
  recordBtn.disabled = true;
  startCameraBtn.textContent = "Enable camera";
  setStatus("Camera off.");
  stopLiveTracking();
  if (monitorToggle.checked) stopBackgroundMonitor();
  resetCameraIdleTimer(); // clears the pending timeout now that the camera is off
}

recordBtn.addEventListener("click", () => startRecording({ silent: false }));

// `silent` skips the visible countdown/status/result-card churn - used by
// background monitoring, which records every few minutes without hijacking
// the screen each time. Returns the upload result (or null on failure)
// either way, so a caller can act on it (e.g. decide whether to notify).
function startRecording({ silent }) {
  return new Promise((resolve) => {
    if (!mediaStream) {
      resolve(null);
      return;
    }
    recordedChunks = [];
    isRecording = true;

    // Safari/iOS supports neither webm variant - MediaRecorder there only
    // takes mp4. Falling through to an unconditional "video/webm" (the old
    // behavior) throws a NotSupportedError on that platform instead of
    // recording anything, so mp4 has to be a real fallback, not an
    // afterthought. recordedMimeType is what the resulting Blob and upload
    // filename are actually tagged with, further down.
    recordedMimeType = MediaRecorder.isTypeSupported("video/webm;codecs=vp8")
      ? "video/webm;codecs=vp8"
      : MediaRecorder.isTypeSupported("video/webm")
        ? "video/webm"
        : "video/mp4";
    mediaRecorder = new MediaRecorder(mediaStream, {
      mimeType: recordedMimeType,
      videoBitsPerSecond: RECORD_VIDEO_BITS_PER_SECOND,
    });

    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) recordedChunks.push(e.data);
    };
    mediaRecorder.onstop = async () => {
      const data = await handleRecordingComplete({ silent });
      resolve(data);
    };

    mediaRecorder.start();
    if (!silent) {
      recordBtn.disabled = true;
      startCameraBtn.disabled = true; // don't let the stream get killed mid-recording
      resultsEl.classList.add("hidden");
      devInsightsEl.classList.add("hidden");
      overlayLoopActive = false;
      setStatus("Recording... stay still.");
    }

    let remaining = RECORD_SECONDS;
    if (!silent) countdownEl.textContent = `${remaining}s remaining`;
    const timer = setInterval(() => {
      remaining -= 1;
      if (!silent) countdownEl.textContent = remaining > 0 ? `${remaining}s remaining` : "";
      if (remaining <= 0) {
        clearInterval(timer);
        mediaRecorder.stop();
      }
    }, 1000);
  });
}

async function handleRecordingComplete({ silent = false } = {}) {
  if (!silent) setStatus("Processing clip (detecting face, extracting pulse signal)...");
  const blob = new Blob(recordedChunks, { type: recordedMimeType });
  const devMode = !silent && devModeToggle.checked;
  // Extension has to match what was actually recorded (webm vs Safari's
  // mp4) - the backend picks its temp-file suffix from this filename, and a
  // mismatched container/extension can confuse OpenCV's decoder.
  const extension = recordedMimeType.startsWith("video/mp4") ? "mp4" : "webm";

  const formData = new FormData();
  formData.append("video", blob, `clip.${extension}`);
  formData.append("dev_mode", devMode ? "true" : "false");

  try {
    const res = await authedFetch(`${API_BASE}/api/sessions/upload`, {
      method: "POST",
      body: formData,
    });
    const upload = await safeJson(res);

    if (!res.ok) {
      throw new Error(upload.detail || "Upload failed");
    }

    // Upload just hands back a job id - the actual face detection/FFT work
    // happens in a background task on the server so this request doesn't
    // block for the several seconds processing takes. On a hosted deploy
    // the first request in a while also eats a cold-start delay loading
    // opencv/numpy, on top of that - so this can take noticeably longer
    // than it does running locally; the status message says so rather than
    // just sitting on "Processing clip..." with no explanation.
    const data = await pollJob(upload.job_id, {
      onTick: (elapsedMs) => {
        if (!silent && elapsedMs > 6000) {
          setStatus("Still working - a server that's been idle takes a few extra seconds to start up...");
        }
      },
    });

    if (!silent) {
      showResult(data);
      if (devMode && data.debug) {
        setupDevInsights(blob, data);
      }
      setStatus("Done. Recording another clip will add to your trend line below.");
    }
    await refreshHistory();
    return data;
  } catch (err) {
    if (!silent) setStatus(`Error: ${err.message}`, true);
    return null;
  } finally {
    isRecording = false;
    resetCameraIdleTimer(); // clip just finished - start the idle clock fresh
    if (!silent) {
      recordBtn.disabled = false;
      startCameraBtn.disabled = false;
    }
  }
}

function confidenceClass(score) {
  if (score >= 0.6) return "conf-high";
  if (score >= 0.35) return "conf-medium";
  return "conf-low";
}

function showResult(data) {
  resultsEl.classList.remove("hidden");
  hrValue.textContent = Math.round(data.heart_rate_bpm);
  hrConfidence.textContent = `confidence ${(data.heart_rate_confidence * 100).toFixed(0)}%`;
  hrConfidence.className = `stat-confidence ${confidenceClass(data.heart_rate_confidence)}`;
  brValue.textContent = Math.round(data.breathing_rate_bpm);
  brConfidence.textContent = `confidence ${(data.breathing_rate_confidence * 100).toFixed(0)}%`;
  brConfidence.className = `stat-confidence ${confidenceClass(data.breathing_rate_confidence)}`;

  setPlausibilityBadge(
    hrPlausibility,
    data.heart_rate_plausible,
    data.heart_rate_bpm,
    HEART_RATE_PLAUSIBLE_BPM,
    "Lower than a typical resting reading.",
    "Higher than a typical resting reading - movement or lighting can cause this."
  );
  setPlausibilityBadge(
    brPlausibility,
    data.breathing_rate_plausible,
    data.breathing_rate_bpm,
    BREATHING_RATE_PLAUSIBLE_BPM,
    "Slower than typical resting breathing.",
    "Faster than typical resting breathing."
  );

  saveHint.classList.toggle("hidden", data.saved !== false);
}

// A high confidence score means the algorithm found a clean, concentrated
// peak - it says nothing about whether that peak is a physiologically
// plausible number for someone sitting still. This badge is deliberately a
// second, independent check so a confidently-wrong reading (e.g. noise that
// happens to land cleanly in-band) doesn't ship silently.
function setPlausibilityBadge(el, isPlausible, bpm, range, lowMessage, highMessage) {
  if (isPlausible) {
    el.classList.add("hidden");
    el.textContent = "";
    return;
  }
  el.classList.remove("hidden");
  el.textContent = bpm < range[0] ? lowMessage : highMessage;
}

// ---------------------------------------------------------------------------
// Live dev-mode tracking: while the camera is on and dev mode is enabled,
// periodically send a frame from the live preview to the same face/ROI
// detector the real pipeline uses, and draw the result over the preview -
// so you can see what's being tracked *while* recording, not just after.
// ---------------------------------------------------------------------------

function startLiveTracking() {
  if (liveTrackingTimer) return;
  liveTrackingStatus.textContent = "starting tracker...";
  liveTrackingStatus.classList.remove("hidden", "warning");
  liveTrackingTimer = setInterval(captureAndDetectLive, 450);
}

function stopLiveTracking() {
  if (liveTrackingTimer) {
    clearInterval(liveTrackingTimer);
    liveTrackingTimer = null;
  }
  liveTrackingStatus.classList.add("hidden");
  const ctx = liveOverlayCanvas.getContext("2d");
  ctx.clearRect(0, 0, liveOverlayCanvas.width, liveOverlayCanvas.height);
}

async function captureAndDetectLive() {
  if (liveDetectInFlight || !mediaStream || preview.readyState < 2) return;
  const w = preview.videoWidth;
  const h = preview.videoHeight;
  if (!w || !h) return;

  liveCaptureCanvas.width = w;
  liveCaptureCanvas.height = h;
  liveCaptureCanvas.getContext("2d").drawImage(preview, 0, 0, w, h);

  liveDetectInFlight = true;
  liveCaptureCanvas.toBlob(
    async (blob) => {
      if (!blob) {
        liveDetectInFlight = false;
        return;
      }
      try {
        const fd = new FormData();
        fd.append("frame", blob, "frame.jpg");
        const res = await fetch(`${API_BASE}/api/detect-face`, { method: "POST", body: fd });
        const data = await safeJson(res);
        drawLiveOverlay(data, w, h);
      } catch (err) {
        /* transient network hiccup - just skip this tick */
      } finally {
        liveDetectInFlight = false;
      }
    },
    "image/jpeg",
    0.7
  );
}

function drawLiveOverlay(data, w, h) {
  liveOverlayCanvas.width = w;
  liveOverlayCanvas.height = h;
  const ctx = liveOverlayCanvas.getContext("2d");
  ctx.clearRect(0, 0, w, h);

  if (!data.detected) {
    liveTrackingStatus.textContent = "no face detected";
    liveTrackingStatus.classList.add("warning");
    return;
  }
  liveTrackingStatus.textContent = "tracking forehead + chest regions";
  liveTrackingStatus.classList.remove("warning");

  const r = data.regions;
  drawBox(ctx, r.face_bbox, COLORS.text, "face");
  drawBox(ctx, r.forehead_roi_bbox, COLORS.accent, "forehead (HR)");
  drawBox(ctx, r.chest_roi_bbox, COLORS.accent2, "chest (BR)");
}

// ---------------------------------------------------------------------------
// Dev mode replay: after a clip is uploaded, replay it with the face/ROI
// boxes the backend actually used drawn on top, plus the raw + filtered
// signals and their FFT spectra, so it's visible *why* a given bpm/
// confidence came out the way it did.
// ---------------------------------------------------------------------------

function setupDevInsights(blob, data) {
  devInsightsEl.classList.remove("hidden");
  debugData = data.debug;
  debugFps = data.fps || 30;

  if (replayObjectUrl) URL.revokeObjectURL(replayObjectUrl);
  replayObjectUrl = URL.createObjectURL(blob);
  replayVideo.src = replayObjectUrl;
  replayVideo.currentTime = 0;

  const detectedFrames = debugData.face_detected.filter(Boolean).length;
  const totalFrames = debugData.face_detected.length;
  if (detectedFrames < totalFrames) {
    faceDropoutWarning.textContent =
      `Face detection dropped out on ${totalFrames - detectedFrames} of ${totalFrames} frames ` +
      `(shown in red below) - those frames reuse the last known box rather than skipping, ` +
      `which is a real source of noise in the signal.`;
    faceDropoutWarning.classList.remove("hidden");
  } else {
    faceDropoutWarning.classList.add("hidden");
  }

  replayVideo.onloadedmetadata = () => {
    overlayCanvas.width = debugData.frame_width;
    overlayCanvas.height = debugData.frame_height;
    if (!overlayLoopActive) {
      overlayLoopActive = true;
      requestAnimationFrame(overlayLoop);
    }
  };

  renderDevCharts(data);
}

function overlayLoop() {
  if (devInsightsEl.classList.contains("hidden")) {
    overlayLoopActive = false;
    return;
  }
  drawOverlayFrame();
  requestAnimationFrame(overlayLoop);
}

function drawBox(ctx, bbox, color, label, dashed = false) {
  if (!bbox) return;
  const [x, y, w, h] = bbox;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  if (dashed) ctx.setLineDash([6, 4]);
  ctx.strokeRect(x, y, w, h);
  ctx.restore();
  ctx.fillStyle = color;
  ctx.font = "600 12px 'Bricolage Grotesque', sans-serif";
  const labelY = y > 16 ? y - 5 : y + h + 14;
  ctx.fillText(label, x + 2, labelY);
}

function drawOverlayFrame() {
  if (!debugData) return;
  const idx = Math.min(
    debugData.face_bboxes.length - 1,
    Math.max(0, Math.round(replayVideo.currentTime * debugFps))
  );
  const ctx = overlayCanvas.getContext("2d");
  ctx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);

  const detected = debugData.face_detected[idx];
  drawBox(ctx, debugData.face_bboxes[idx], detected ? COLORS.text : COLORS.danger, detected ? "face" : "face (lost)", !detected);
  drawBox(ctx, debugData.forehead_roi_bboxes[idx], COLORS.accent, "forehead ROI");
  drawBox(ctx, debugData.chest_roi_bboxes[idx], COLORS.accent2, "chest ROI");
}

function destroyDevCharts() {
  Object.values(devCharts).forEach((c) => c && c.destroy());
}

function verticalLineDataset(xValue, maxY, color, label, dashed) {
  return {
    label,
    data: [
      { x: xValue, y: 0 },
      { x: xValue, y: maxY },
    ],
    borderColor: color,
    borderWidth: dashed ? 1.5 : 2,
    borderDash: dashed ? [5, 4] : [],
    pointRadius: 0,
    fill: false,
    tension: 0,
  };
}

function makeSignalChart(canvasId, times, raw, filtered, rawLabel, filteredLabel, filteredColor) {
  const ctx = document.getElementById(canvasId).getContext("2d");
  const toPoints = (arr) => arr.map((v, i) => ({ x: times[i], y: v }));
  return new Chart(ctx, {
    type: "line",
    data: {
      datasets: [
        {
          label: rawLabel,
          data: toPoints(raw),
          borderColor: COLORS.textDim,
          backgroundColor: "transparent",
          borderWidth: 1,
          pointRadius: 0,
          tension: 0.1,
        },
        {
          label: filteredLabel,
          data: toPoints(filtered),
          borderColor: filteredColor,
          backgroundColor: "transparent",
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.1,
        },
      ],
    },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: { type: "linear", title: { display: true, text: "seconds", color: COLORS.textDim }, ticks: { color: COLORS.textDim }, grid: { color: COLORS.border } },
        y: { ticks: { color: COLORS.textDim }, grid: { color: COLORS.border }, title: { display: true, text: "raw (thin) vs. filtered (bold)", color: COLORS.textDim, font: { size: 10 } } },
      },
      plugins: { legend: { labels: { color: COLORS.text, boxWidth: 12, font: { size: 11 } } } },
    },
  });
}

function makeFftChart(canvasId, freqsHz, magnitude, bandBpm, peakBpm, accentColor, xMax) {
  const bpm = freqsHz.map((f) => f * 60);
  const points = bpm.map((x, i) => ({ x, y: magnitude[i] }));
  const maxMag = Math.max(...magnitude, 1);

  const ctx = document.getElementById(canvasId).getContext("2d");
  return new Chart(ctx, {
    type: "line",
    data: {
      datasets: [
        {
          label: "spectrum",
          data: points,
          borderColor: accentColor,
          backgroundColor: "transparent",
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.15,
        },
        verticalLineDataset(bandBpm[0], maxMag, COLORS.textDim, "plausible range", true),
        verticalLineDataset(bandBpm[1], maxMag, COLORS.textDim, "plausible range", true),
        verticalLineDataset(peakBpm, maxMag, COLORS.danger, "detected peak", false),
      ],
    },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: { type: "linear", min: 0, max: xMax, title: { display: true, text: "bpm / min", color: COLORS.textDim }, ticks: { color: COLORS.textDim }, grid: { color: COLORS.border } },
        y: { ticks: { color: COLORS.textDim }, grid: { color: COLORS.border }, title: { display: true, text: "FFT magnitude", color: COLORS.textDim, font: { size: 10 } } },
      },
      plugins: {
        legend: {
          labels: {
            color: COLORS.text,
            boxWidth: 12,
            font: { size: 11 },
            filter: (item) => item.text !== "plausible range" || item.datasetIndex === 1,
          },
        },
      },
    },
  });
}

function renderDevCharts(data) {
  destroyDevCharts();
  const debug = data.debug;

  devCharts.hrSignal = makeSignalChart(
    "hrSignalChart",
    debug.frame_times_s,
    debug.green_signal_raw,
    debug.green_signal_filtered,
    "raw forehead green channel",
    "band-passed (0.7-4.0 Hz)",
    COLORS.accent
  );
  devCharts.brSignal = makeSignalChart(
    "brSignalChart",
    debug.frame_times_s,
    debug.chest_signal_raw,
    debug.chest_signal_filtered,
    "raw chest optical-flow",
    "band-passed (0.1-0.6 Hz)",
    COLORS.accent2
  );
  devCharts.hrFft = makeFftChart(
    "hrFftChart",
    debug.hr_fft_freqs_hz,
    debug.hr_fft_magnitude,
    HEART_RATE_BAND_BPM,
    data.heart_rate_bpm,
    COLORS.accent,
    260
  );
  devCharts.brFft = makeFftChart(
    "brFftChart",
    debug.br_fft_freqs_hz,
    debug.br_fft_magnitude,
    BREATHING_BAND_BPM,
    data.breathing_rate_bpm,
    COLORS.accent2,
    40
  );
}

// ---------------------------------------------------------------------------
// History: trend charts, summary stats, session table (with delete), CSV export.
// ---------------------------------------------------------------------------

async function refreshHistory() {
  if (!isSignedIn) return; // signed-out state already shows the sign-in prompt instead
  try {
    const res = await authedFetch(`${API_BASE}/api/sessions`);
    if (!res.ok) return;
    const sessions = await safeJson(res);
    renderChart(sessions);
    renderConfidenceChart(sessions);
    renderStats(sessions);
    renderHistoryTable(sessions);
  } catch (err) {
    /* history is a nice-to-have; don't block the main flow on it */
  }
}

function renderChart(sessions) {
  if (!sessions || sessions.length === 0) {
    noHistoryEl.style.display = "block";
    if (chart) {
      chart.destroy();
      chart = null;
    }
    return;
  }
  noHistoryEl.style.display = "none";

  const labels = sessions.map((s) => formatSessionTime(s.created_at));
  const hr = sessions.map((s) => s.heart_rate_bpm);
  const br = sessions.map((s) => s.breathing_rate_bpm);

  const ctx = document.getElementById("trendsChart").getContext("2d");
  if (chart) chart.destroy();
  chart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Heart rate (bpm)",
          data: hr,
          borderColor: COLORS.accent,
          backgroundColor: "transparent",
          tension: 0.3,
          yAxisID: "y",
        },
        {
          label: "Breathing rate (br/min)",
          data: br,
          borderColor: COLORS.accent2,
          backgroundColor: "transparent",
          tension: 0.3,
          yAxisID: "y1",
        },
      ],
    },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      scales: {
        y: { type: "linear", position: "left", title: { display: true, text: "bpm", color: COLORS.textDim }, ticks: { color: COLORS.textDim }, grid: { color: COLORS.border } },
        y1: { type: "linear", position: "right", title: { display: true, text: "br/min", color: COLORS.textDim }, ticks: { color: COLORS.textDim }, grid: { drawOnChartArea: false } },
        x: { ticks: { color: COLORS.textDim }, grid: { color: COLORS.border } },
      },
      plugins: {
        legend: { labels: { color: COLORS.text } },
      },
    },
  });
}

function renderConfidenceChart(sessions) {
  const canvas = document.getElementById("confidenceChart");
  if (!sessions || sessions.length === 0) {
    if (confidenceChart) {
      confidenceChart.destroy();
      confidenceChart = null;
    }
    canvas.classList.add("hidden");
    return;
  }
  canvas.classList.remove("hidden");

  const labels = sessions.map((s) => formatSessionTime(s.created_at));
  const hrConf = sessions.map((s) => Math.round(s.heart_rate_confidence * 100));
  const brConf = sessions.map((s) => Math.round(s.breathing_rate_confidence * 100));

  const ctx = canvas.getContext("2d");
  if (confidenceChart) confidenceChart.destroy();
  confidenceChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Heart rate confidence (%)",
          data: hrConf,
          borderColor: COLORS.accent,
          backgroundColor: "transparent",
          tension: 0.3,
        },
        {
          label: "Breathing confidence (%)",
          data: brConf,
          borderColor: COLORS.accent2,
          backgroundColor: "transparent",
          tension: 0.3,
        },
      ],
    },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      scales: {
        y: { type: "linear", min: 0, max: 100, title: { display: true, text: "confidence %", color: COLORS.textDim }, ticks: { color: COLORS.textDim }, grid: { color: COLORS.border } },
        x: { ticks: { color: COLORS.textDim }, grid: { color: COLORS.border } },
      },
      plugins: {
        legend: { labels: { color: COLORS.text } },
      },
    },
  });
}

function renderStats(sessions) {
  if (!sessions || sessions.length === 0) {
    historyStatsEl.innerHTML = "";
    return;
  }
  const avg = (arr) => arr.reduce((a, b) => a + b, 0) / arr.length;
  const hr = sessions.map((s) => s.heart_rate_bpm);
  const br = sessions.map((s) => s.breathing_rate_bpm);

  // Two-Signal Rule (DESIGN.md): coral for heart-rate data, amber for
  // breathing-rate data, neutral for anything that isn't a signal reading.
  const cards = [
    [sessions.length, "Sessions", ""],
    [Math.round(avg(hr)), "Avg HR (bpm)", "signal-hr"],
    [Math.round(avg(br)), "Avg BR (br/min)", "signal-br"],
    [Math.min(...hr).toFixed(0) + "–" + Math.max(...hr).toFixed(0), "HR range", "signal-hr"],
  ];
  historyStatsEl.innerHTML = cards
    .map(([value, label, cls]) => `<div class="stat-mini"><div class="stat-mini-value ${cls}">${value}</div><div class="stat-mini-label">${label}</div></div>`)
    .join("");
}

function plausibilityDot(isPlausible, title) {
  if (isPlausible !== false) return ""; // undefined/true both render clean
  return `<span class="plausibility-dot" title="${title}"></span>`;
}

function formatSessionTime(createdAt) {
  return new Date(createdAt * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function renderHistoryTable(sessions) {
  if (!sessions || sessions.length === 0) {
    historyTableWrap.classList.add("hidden");
    historyTableBody.innerHTML = "";
    return;
  }
  historyTableWrap.classList.remove("hidden");

  historyTableBody.innerHTML = sessions
    .slice()
    .reverse()
    .map(
      (s) => `
      <tr>
        <td>${formatSessionTime(s.created_at)}</td>
        <td>${Math.round(s.heart_rate_bpm)}${plausibilityDot(s.heart_rate_plausible, "Outside the typical resting heart-rate range")}</td>
        <td>${Math.round(s.breathing_rate_bpm)}${plausibilityDot(s.breathing_rate_plausible, "Outside the typical resting breathing-rate range")}</td>
        <td>${(s.heart_rate_confidence * 100).toFixed(0)}% / ${(s.breathing_rate_confidence * 100).toFixed(0)}%</td>
        <td><button class="row-delete" data-id="${s.id}" title="Delete this session">&times;</button></td>
      </tr>`
    )
    .join("");
}

historyTableBody.addEventListener("click", async (e) => {
  const btn = e.target.closest(".row-delete");
  if (!btn) return;
  const id = btn.dataset.id;
  btn.disabled = true;
  try {
    const res = await authedFetch(`${API_BASE}/api/sessions/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error("Delete failed");
    await refreshHistory();
  } catch (err) {
    setStatus(`Couldn't delete session: ${err.message}`, true);
    btn.disabled = false;
  }
});

exportCsvBtn.addEventListener("click", async () => {
  try {
    const res = await authedFetch(`${API_BASE}/api/sessions`);
    const sessions = await safeJson(res);
    if (!sessions.length) return;

    const columns = [
      "id",
      "created_at",
      "heart_rate_bpm",
      "heart_rate_confidence",
      "breathing_rate_bpm",
      "breathing_rate_confidence",
      "fps",
      "num_frames",
      "face_detection_rate",
    ];
    const rows = sessions.map((s) => columns.map((c) => s[c]).join(","));
    const csv = [columns.join(","), ...rows].join("\n");

    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `vitals_${accountName.textContent || "history"}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    setStatus(`Couldn't export CSV: ${err.message}`, true);
  }
});

// ---------------------------------------------------------------------------
// Background monitoring: while enabled, records a short check-in clip on an
// interval and fires a browser notification if a reading comes back
// unusually low. Honest about what this actually is - a recurring
// getUserMedia capture from an open browser tab, not an OS-level background
// service - it needs the tab to keep existing (it can be inactive/behind
// other windows) and the browser process to keep running; it cannot check
// anything with the tab closed or the machine asleep.
// ---------------------------------------------------------------------------

let monitorTimer = null;
let monitorInFlight = false;

try {
  monitorToggle.checked = localStorage.getItem("vitals_monitor_on") === "1";
  const savedInterval = localStorage.getItem("vitals_monitor_interval");
  if (savedInterval) monitorInterval.value = savedInterval;
} catch (e) {}

function restoreBackgroundMonitorPreference() {
  if (monitorToggle.checked) startBackgroundMonitor();
}

monitorToggle.addEventListener("change", async () => {
  if (monitorToggle.checked) {
    const started = await startBackgroundMonitor();
    if (!started) monitorToggle.checked = false;
  } else {
    stopBackgroundMonitor();
  }
  try {
    localStorage.setItem("vitals_monitor_on", monitorToggle.checked ? "1" : "0");
  } catch (e) {}
});

monitorInterval.addEventListener("change", () => {
  try {
    localStorage.setItem("vitals_monitor_interval", monitorInterval.value);
  } catch (e) {}
  if (monitorTimer) {
    stopBackgroundMonitor({ keepToggleOn: true });
    startBackgroundMonitor();
  }
});

async function startBackgroundMonitor() {
  if (!isSignedIn) return false; // the control is hidden signed-out; this guards programmatic calls too
  if (!("Notification" in window)) {
    monitorStatus.textContent = "This browser doesn't support notifications - background monitoring needs them to be useful.";
    monitorStatus.classList.add("error");
    return false;
  }
  let permission = Notification.permission;
  if (permission === "default") {
    permission = await Notification.requestPermission();
  }
  if (permission !== "granted") {
    monitorStatus.textContent = "Notifications are blocked - allow them for this site to use background monitoring.";
    monitorStatus.classList.add("error");
    return false;
  }
  if (!cameraOn) {
    const enabled = await enableCamera();
    if (!enabled) {
      monitorStatus.textContent = "Couldn't enable the camera - background monitoring needs it on.";
      monitorStatus.classList.add("error");
      return false;
    }
  }

  const minutes = parseInt(monitorInterval.value, 10) || 10;
  monitorStatus.textContent = `Monitoring: checking every ${minutes} minutes while this tab stays open.`;
  monitorStatus.classList.remove("error");

  if (monitorTimer) clearInterval(monitorTimer);
  monitorTimer = setInterval(() => runMonitorCheck(), minutes * 60 * 1000);
  return true;
}

function stopBackgroundMonitor({ keepToggleOn = false } = {}) {
  if (monitorTimer) {
    clearInterval(monitorTimer);
    monitorTimer = null;
  }
  if (!keepToggleOn) {
    monitorToggle.checked = false;
    monitorStatus.textContent = "";
    monitorStatus.classList.remove("error");
  }
  resetCameraIdleTimer(); // the idle-privacy timeout was suppressed while monitoring ran
}

async function runMonitorCheck() {
  if (monitorInFlight || !cameraOn || recordBtn.disabled) return;
  monitorInFlight = true;
  try {
    const data = await startRecording({ silent: true });
    if (!data) return;
    const now = new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    monitorStatus.textContent = `Last check at ${now}: ${Math.round(data.heart_rate_bpm)} bpm, ${Math.round(data.breathing_rate_bpm)} br/min.`;
    monitorStatus.classList.remove("error");

    const hrTooLow = data.heart_rate_bpm < HEART_RATE_PLAUSIBLE_BPM[0];
    const brTooLow = data.breathing_rate_bpm < BREATHING_RATE_PLAUSIBLE_BPM[0];
    if (hrTooLow || brTooLow) notifyLowVitals(data, hrTooLow, brTooLow);
  } finally {
    monitorInFlight = false;
  }
}

function notifyLowVitals(data, hrTooLow, brTooLow) {
  const parts = [];
  if (hrTooLow) parts.push(`heart rate ${Math.round(data.heart_rate_bpm)} bpm`);
  if (brTooLow) parts.push(`breathing rate ${Math.round(data.breathing_rate_bpm)} br/min`);
  try {
    new Notification("Vitals Tracker: unusually low reading", {
      body: `${parts.join(" and ")} - lower than typical for resting. Could be a real reading or a tracking issue; check in when you can.`,
      tag: "vitals-low-reading",
    });
  } catch (e) {
    /* Notification constructor can throw in some contexts (e.g. service-worker-only browsers) - not fatal */
  }
}

// Entry point: find out who (if anyone) is already signed in.
checkAuth();
