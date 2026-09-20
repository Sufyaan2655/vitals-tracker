const API_BASE = ""; // same-origin: FastAPI serves this file itself

const usernameInput = document.getElementById("username");
const preview = document.getElementById("preview");
const startCameraBtn = document.getElementById("startCamera");
const recordBtn = document.getElementById("recordBtn");
const devModeToggle = document.getElementById("devMode");
const countdownEl = document.getElementById("countdown");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const hrValue = document.getElementById("hrValue");
const hrConfidence = document.getElementById("hrConfidence");
const brValue = document.getElementById("brValue");
const brConfidence = document.getElementById("brConfidence");
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

const RECORD_SECONDS = 15;
// Must match backend/vitals.py's HEART_RATE_BAND_HZ / BREATHING_BAND_HZ - the
// physiologically plausible ranges the FFT peak is picked from.
const HEART_RATE_BAND_BPM = [0.7 * 60, 4.0 * 60];
const BREATHING_BAND_BPM = [0.1 * 60, 0.6 * 60];

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

// Remember the last-used name/dev-mode preference across visits (this is a
// normal local web app running in the user's own browser, not an embedded
// preview, so localStorage behaves normally here).
try {
  const savedName = localStorage.getItem("vitals_username");
  if (savedName) usernameInput.value = savedName;
  devModeToggle.checked = localStorage.getItem("vitals_dev_mode") === "1";
} catch (e) {
  /* private browsing / storage disabled - not fatal, just skip persistence */
}

usernameInput.addEventListener("change", () => {
  try {
    localStorage.setItem("vitals_username", usernameInput.value.trim());
  } catch (e) {}
  refreshHistory();
});

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

function currentUsername() {
  return usernameInput.value.trim() || "anonymous";
}

// ---------------------------------------------------------------------------
// Camera: enable/disable toggle.
// ---------------------------------------------------------------------------

startCameraBtn.addEventListener("click", async () => {
  if (cameraOn) {
    stopCamera();
    return;
  }
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
  } catch (err) {
    setStatus(
      "Couldn't access the camera. Check browser permissions and that no other app is using it.",
      true
    );
  }
});

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
}

recordBtn.addEventListener("click", () => {
  if (!mediaStream) return;
  recordedChunks = [];

  const mimeType = MediaRecorder.isTypeSupported("video/webm;codecs=vp8")
    ? "video/webm;codecs=vp8"
    : "video/webm";
  mediaRecorder = new MediaRecorder(mediaStream, { mimeType });

  mediaRecorder.ondataavailable = (e) => {
    if (e.data.size > 0) recordedChunks.push(e.data);
  };
  mediaRecorder.onstop = handleRecordingComplete;

  mediaRecorder.start();
  recordBtn.disabled = true;
  startCameraBtn.disabled = true; // don't let the stream get killed mid-recording
  resultsEl.classList.add("hidden");
  devInsightsEl.classList.add("hidden");
  overlayLoopActive = false;
  setStatus("Recording... stay still.");

  let remaining = RECORD_SECONDS;
  countdownEl.textContent = `${remaining}s remaining`;
  const timer = setInterval(() => {
    remaining -= 1;
    countdownEl.textContent = remaining > 0 ? `${remaining}s remaining` : "";
    if (remaining <= 0) {
      clearInterval(timer);
      mediaRecorder.stop();
    }
  }, 1000);
});

async function handleRecordingComplete() {
  setStatus("Processing clip (detecting face, extracting pulse signal)...");
  const blob = new Blob(recordedChunks, { type: "video/webm" });
  const devMode = devModeToggle.checked;

  const formData = new FormData();
  formData.append("video", blob, "clip.webm");
  formData.append("username", currentUsername());
  formData.append("dev_mode", devMode ? "true" : "false");

  try {
    const res = await fetch(`${API_BASE}/api/sessions/upload`, {
      method: "POST",
      body: formData,
    });
    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.detail || "Processing failed");
    }

    showResult(data);
    if (devMode && data.debug) {
      setupDevInsights(blob, data);
    }
    setStatus("Done. Recording another clip will add to your trend line below.");
    await refreshHistory();
  } catch (err) {
    setStatus(`Error: ${err.message}`, true);
  } finally {
    recordBtn.disabled = false;
    startCameraBtn.disabled = false;
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
  ctx.font = "600 12px 'Space Grotesk', sans-serif";
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
    const res = await fetch(`${API_BASE}/api/sessions?username=${encodeURIComponent(currentUsername())}`);
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

  const cards = [
    [sessions.length, "Sessions"],
    [Math.round(avg(hr)), "Avg HR (bpm)"],
    [Math.round(avg(br)), "Avg BR (br/min)"],
    [Math.min(...hr).toFixed(0) + "-" + Math.max(...hr).toFixed(0), "HR range"],
  ];
  historyStatsEl.innerHTML = cards
    .map(([value, label]) => `<div class="stat-mini"><div class="stat-mini-value">${value}</div><div class="stat-mini-label">${label}</div></div>`)
    .join("");
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
        <td>${Math.round(s.heart_rate_bpm)}</td>
        <td>${Math.round(s.breathing_rate_bpm)}</td>
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
    const res = await fetch(`${API_BASE}/api/sessions/${id}?username=${encodeURIComponent(currentUsername())}`, {
      method: "DELETE",
    });
    if (!res.ok) throw new Error("Delete failed");
    await refreshHistory();
  } catch (err) {
    setStatus(`Couldn't delete session: ${err.message}`, true);
    btn.disabled = false;
  }
});

exportCsvBtn.addEventListener("click", async () => {
  try {
    const res = await fetch(`${API_BASE}/api/sessions?username=${encodeURIComponent(currentUsername())}`);
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
    a.download = `vitals_${currentUsername()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    setStatus(`Couldn't export CSV: ${err.message}`, true);
  }
});

// Load any existing history on page load (in case this browser has a saved name).
refreshHistory();
