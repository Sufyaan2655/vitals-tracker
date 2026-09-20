const API_BASE = ""; // same-origin: FastAPI serves this file itself

const authScreen = document.getElementById("authScreen");
const appScreen = document.getElementById("appScreen");
const accountBar = document.getElementById("accountBar");
const accountName = document.getElementById("accountName");
const signOutBtn = document.getElementById("signOutBtn");
const authForm = document.getElementById("authForm");
const authTabs = Array.from(document.querySelectorAll(".auth-tab"));
const authUsername = document.getElementById("authUsername");
const authPassword = document.getElementById("authPassword");
const authSubmit = document.getElementById("authSubmit");
const authStatus = document.getElementById("authStatus");

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
let cameraOn = false;
let chart = null;
let confidenceChart = null;

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
// Auth: sign in / create account / sign out. Session state lives entirely in
// an HttpOnly cookie the server sets - fetch() sends it automatically on
// same-origin requests, nothing to manage here beyond checking who (if
// anyone) is signed in and reacting to a 401 by showing the auth screen.
// ---------------------------------------------------------------------------

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
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Something went wrong");
    enterApp(data.username);
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
  stopCamera();
  stopBackgroundMonitor();
  showAuthScreen();
});

function showAuthScreen() {
  authScreen.classList.remove("hidden");
  appScreen.classList.add("hidden");
  accountBar.classList.add("hidden");
  authPassword.value = "";
  authStatus.textContent = "";
  authStatus.classList.remove("error");
}

function enterApp(username) {
  authScreen.classList.add("hidden");
  appScreen.classList.remove("hidden");
  accountBar.classList.remove("hidden");
  accountName.textContent = username;
  refreshHistory();
  restoreBackgroundMonitorPreference();
}

async function checkAuth() {
  try {
    const res = await fetch(`${API_BASE}/api/auth/me`);
    if (!res.ok) {
      showAuthScreen();
      return;
    }
    const data = await res.json();
    enterApp(data.username);
  } catch (err) {
    showAuthScreen();
  }
}

// A 401 from any authenticated call means the session cookie expired or was
// cleared elsewhere - fall back to the sign-in screen instead of leaving the
// app stuck showing stale data it can no longer fetch.
async function authedFetch(url, options) {
  const res = await fetch(url, options);
  if (res.status === 401) {
    stopCamera();
    stopBackgroundMonitor();
    showAuthScreen();
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
      video: { width: 640, height: 480, facingMode: "user" },
      audio: false,
    });
    preview.srcObject = mediaStream;
    cameraOn = true;
    recordBtn.disabled = false;
    startCameraBtn.textContent = "Disable camera";
    setStatus("Camera ready. Sit still, face well-lit, then record a clip.");
    if (devModeToggle.checked) startLiveTracking();
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

    const mimeType = MediaRecorder.isTypeSupported("video/webm;codecs=vp8")
      ? "video/webm;codecs=vp8"
      : "video/webm";
    mediaRecorder = new MediaRecorder(mediaStream, { mimeType });

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
  const blob = new Blob(recordedChunks, { type: "video/webm" });
  const devMode = !silent && devModeToggle.checked;

  const formData = new FormData();
  formData.append("video", blob, "clip.webm");
  formData.append("dev_mode", devMode ? "true" : "false");

  try {
    const res = await authedFetch(`${API_BASE}/api/sessions/upload`, {
      method: "POST",
      body: formData,
    });
    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.detail || "Processing failed");
    }

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
        const data = await res.json();
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
  try {
    const res = await authedFetch(`${API_BASE}/api/sessions`);
    if (!res.ok) return;
    const sessions = await res.json();
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
    const sessions = await res.json();
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
