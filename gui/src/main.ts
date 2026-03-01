// ============================================================================
// VoiceLink GUI — Frontend Logic
// ============================================================================
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

// ============================================================================
// Types (mirrors Rust structs)
// ============================================================================

interface ServerHealth {
  status: string;
  model: string | null;
  model_loaded: boolean;
  gpu_available: boolean;
  gpu_name: string | null;
  uptime_seconds: number;
}

interface ServerStatus {
  running: boolean;
  health: ServerHealth | null;
}

interface VoiceInfo {
  id: string;
  name: string;
  language: string;
  gender: string;
  description: string;
  model: string;
  tags: string[];
  sample_rate: number;
}

interface SapiStatus {
  registered: boolean;
  dll_path: string | null;
  voice_count: number;
}

interface SetupStatus {
  python_installed: boolean;
  deps_installed: boolean;
  server_installed: boolean;
  model_downloaded: boolean;
  server_running: boolean;
  data_dir: string;
}

interface SetupPaths {
  data_dir: string;
  python_dir: string;
  python_exe: string;
  server_dir: string;
  model_dir: string;
}

// ============================================================================
// State
// ============================================================================

let currentVoices: VoiceInfo[] = [];
let registeredVoiceIds: Set<string> = new Set();

// ============================================================================
// Navigation
// ============================================================================

function setupNavigation() {
  const navItems = document.querySelectorAll<HTMLElement>(".nav-item");
  navItems.forEach((item) => {
    item.addEventListener("click", () => {
      const page = item.dataset.page;
      if (!page) return;

      navItems.forEach((n) => n.classList.remove("active"));
      item.classList.add("active");

      document.querySelectorAll<HTMLElement>(".page").forEach((p) => {
        p.classList.toggle("active", p.id === `page-${page}`);
      });

      if (page === "voices") loadVoices();
      if (page === "setup") refreshSetupStatus();
    });
  });
}

// ============================================================================
// Server Status
// ============================================================================

async function checkServerStatus() {
  const indicator = document.getElementById("server-indicator");
  const statusEl = document.getElementById("server-status");
  const modelEl = document.getElementById("server-model");
  const deviceEl = document.getElementById("server-device");
  const voicesEl = document.getElementById("server-voices");

  try {
    const result: ServerStatus = await invoke("get_server_status");

    if (result.running && result.health) {
      indicator?.classList.remove("offline");
      indicator?.classList.add("online");
      if (statusEl) statusEl.textContent = "Running";
      if (modelEl) modelEl.textContent = result.health.model ?? "—";
      if (deviceEl) deviceEl.textContent = result.health.gpu_name ?? (result.health.gpu_available ? "GPU" : "CPU");
      if (voicesEl) voicesEl.textContent = formatUptime(result.health.uptime_seconds);
    } else if (result.running) {
      indicator?.classList.remove("offline");
      indicator?.classList.add("online");
      if (statusEl) statusEl.textContent = "Running (no health data)";
    } else {
      indicator?.classList.remove("online");
      indicator?.classList.add("offline");
      if (statusEl) statusEl.textContent = "Offline";
      if (modelEl) modelEl.textContent = "—";
      if (deviceEl) deviceEl.textContent = "—";
      if (voicesEl) voicesEl.textContent = "—";
    }
  } catch (e) {
    indicator?.classList.remove("online");
    indicator?.classList.add("offline");
    if (statusEl) statusEl.textContent = "Error";
    console.error("Status check failed:", e);
  }
}

async function checkSapiStatus() {
  const indicator = document.getElementById("sapi-indicator");
  const registryEl = document.getElementById("sapi-registry");
  const dllEl = document.getElementById("sapi-dll");

  try {
    const result: SapiStatus = await invoke("get_sapi_status");

    if (result.registered) {
      indicator?.classList.remove("offline");
      indicator?.classList.add("online");
      if (registryEl) registryEl.textContent = `${result.voice_count} voices registered`;
      if (dllEl) {
        const path = result.dll_path ?? "";
        const fileName = path.split("\\").pop() ?? path;
        dllEl.textContent = fileName || "—";
      }
    } else {
      indicator?.classList.remove("online");
      indicator?.classList.add("offline");
      if (registryEl) registryEl.textContent = "Not registered";
      if (dllEl) dllEl.textContent = "—";
    }
  } catch (e) {
    if (registryEl) registryEl.textContent = "Error";
    console.error("SAPI check failed:", e);
  }
}

function startStatusPolling() {
  checkServerStatus();
  checkSapiStatus();
  window.setInterval(checkServerStatus, 5000);
}

// ============================================================================
// Voice Management
// ============================================================================

async function loadVoices() {
  const container = document.getElementById("voices-list");
  if (!container) return;

  try {
    const [voices, regIds] = await Promise.all([
      invoke<VoiceInfo[]>("get_voices"),
      invoke<string[]>("get_registered_voice_ids"),
    ]);
    currentVoices = voices;
    registeredVoiceIds = new Set(regIds);
    renderVoices(container);
    populateTestVoiceSelect();
  } catch (e) {
    container.innerHTML = `<p class="placeholder error">Could not load voices. Is the server running?</p>`;
    console.error("Load voices failed:", e);
  }
}

function renderVoices(container: HTMLElement) {
  if (currentVoices.length === 0) {
    container.innerHTML = `<p class="placeholder">No voices found.</p>`;
    return;
  }

  container.innerHTML = currentVoices
    .map((v) => {
      const isEnabled = registeredVoiceIds.has(v.id);
      return `
    <div class="voice-card card ${isEnabled ? "" : "voice-disabled"}" data-voice-id="${v.id}">
      <div class="voice-header">
        <span class="voice-name">${escapeHtml(v.name)}</span>
        <div class="voice-header-right">
          <span class="voice-badge ${v.gender.toLowerCase()}">${v.gender}</span>
          <label class="switch voice-toggle" title="${isEnabled ? "Disable in SAPI" : "Enable in SAPI"}">
            <input type="checkbox" data-id="${v.id}" ${isEnabled ? "checked" : ""} />
            <span class="slider"></span>
          </label>
        </div>
      </div>
      <div class="voice-description">${escapeHtml(v.description)}</div>
      <div class="voice-tags">${v.tags.map((t) => `<span class="voice-tag">${escapeHtml(t)}</span>`).join("")}</div>
      <div class="voice-details">
        <span class="voice-lang">${escapeHtml(v.language)}</span>
        <span class="voice-model">${escapeHtml(v.model)}</span>
      </div>
      <div class="voice-actions">
        <button class="btn btn-sm btn-secondary btn-rename" data-id="${v.id}">Rename</button>
        <button class="btn btn-sm btn-primary btn-test" data-id="${v.id}">Test</button>
      </div>
    </div>
  `;
    })
    .join("");

  // Wire up action buttons
  container.querySelectorAll<HTMLButtonElement>(".btn-rename").forEach((btn) => {
    btn.addEventListener("click", () => handleRename(btn.dataset.id!));
  });

  container.querySelectorAll<HTMLButtonElement>(".btn-test").forEach((btn) => {
    btn.addEventListener("click", () => handleTestVoice(btn.dataset.id!));
  });

  // Wire up toggle switches
  container.querySelectorAll<HTMLInputElement>(".voice-toggle input").forEach((toggle) => {
    toggle.addEventListener("change", () => handleToggleVoice(toggle.dataset.id!, toggle.checked));
  });
}

async function handleRename(voiceId: string) {
  const voice = currentVoices.find((v) => v.id === voiceId);
  if (!voice) return;

  const newName = await showModal("Rename Voice", voice.name);
  if (!newName || newName === voice.name) return;

  try {
    await invoke("rename_voice", { voiceId, newName });
    voice.name = newName;
    const container = document.getElementById("voices-list");
    if (container) renderVoices(container);
  } catch (e) {
    await showModal("Error", `Rename failed: ${e}`, true);
  }
}

async function handleToggleVoice(voiceId: string, enabled: boolean) {
  try {
    await invoke("toggle_voice", { voiceId, enabled });
    if (enabled) {
      registeredVoiceIds.add(voiceId);
    } else {
      registeredVoiceIds.delete(voiceId);
    }
    const container = document.getElementById("voices-list");
    if (container) renderVoices(container);
    populateTestVoiceSelect();
    // Refresh dashboard SAPI status
    checkSapiStatus();
  } catch (e) {
    // Revert the toggle visually
    if (enabled) {
      registeredVoiceIds.delete(voiceId);
    } else {
      registeredVoiceIds.add(voiceId);
    }
    const container = document.getElementById("voices-list");
    if (container) renderVoices(container);
    await showModal("Error", `Toggle failed: ${e}`, true);
  }
}

function getTestText(): string {
  const textEl = document.getElementById("test-text") as HTMLTextAreaElement | null;
  return textEl?.value.trim() || "Hello! This is a test of VoiceLink.";
}

async function handleTestVoice(voiceId: string) {
  const text = getTestText();
  try {
    await playVoicePreview(voiceId, text);
  } catch (e) {
    console.error("Preview failed:", e);
    await showModal("Error", `Preview failed: ${e}`, true);
  }
}

async function playVoicePreview(voiceId: string, text: string) {
  const pcmBytes: number[] = await invoke("preview_voice", { voiceId, text });

  const sampleRate = 24000;
  const audioCtx = new AudioContext({ sampleRate });
  const int16 = new Int16Array(new Uint8Array(pcmBytes).buffer);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    float32[i] = int16[i] / 32768;
  }
  const buffer = audioCtx.createBuffer(1, float32.length, sampleRate);
  buffer.getChannelData(0).set(float32);
  const source = audioCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(audioCtx.destination);
  source.start();
}

function populateTestVoiceSelect() {
  const select = document.getElementById("test-voice") as HTMLSelectElement | null;
  if (!select) return;

  // Only show registered (enabled) voices in Quick Test dropdown
  const enabledVoices = currentVoices.filter((v) => registeredVoiceIds.has(v.id));
  select.innerHTML = enabledVoices
    .map((v) => `<option value="${v.id}">${escapeHtml(v.name)}</option>`)
    .join("");
}

// ============================================================================
// Quick Test (Dashboard)
// ============================================================================

function setupQuickTest() {
  const btn = document.getElementById("btn-preview");
  btn?.addEventListener("click", async () => {
    const textEl = document.getElementById("test-text") as HTMLTextAreaElement;
    const selectEl = document.getElementById("test-voice") as HTMLSelectElement;
    if (!textEl || !selectEl) return;

    const text = textEl.value.trim();
    const voiceId = selectEl.value;
    if (!text || !voiceId) return;

    btn.setAttribute("disabled", "true");
    btn.textContent = "Playing...";

    try {
      await playVoicePreview(voiceId, text);
    } catch (e) {
      await showModal("Error", `Preview failed: ${e}`, true);
    } finally {
      btn.removeAttribute("disabled");
      btn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg> Preview`;
    }
  });
}

// ============================================================================
// Custom Modal (replaces browser prompt/alert)
// ============================================================================

function showModal(title: string, defaultValue: string, alertOnly = false): Promise<string | null> {
  return new Promise((resolve) => {
    const overlay = document.getElementById("modal-overlay")!;
    const titleEl = document.getElementById("modal-title")!;
    const input = document.getElementById("modal-input") as HTMLInputElement;
    const okBtn = document.getElementById("modal-ok")!;
    const cancelBtn = document.getElementById("modal-cancel")!;

    titleEl.textContent = title;
    overlay.classList.remove("hidden");

    if (alertOnly) {
      input.style.display = "none";
      okBtn.textContent = "OK";
      cancelBtn.style.display = "none";
    } else {
      input.style.display = "";
      input.value = defaultValue;
      okBtn.textContent = "Save";
      cancelBtn.style.display = "";
      setTimeout(() => { input.focus(); input.select(); }, 50);
    }

    function cleanup() {
      overlay.classList.add("hidden");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      input.removeEventListener("keydown", onKey);
    }

    function onOk() {
      cleanup();
      resolve(alertOnly ? "" : input.value.trim());
    }

    function onCancel() {
      cleanup();
      resolve(null);
    }

    function onKey(e: KeyboardEvent) {
      if (e.key === "Enter") onOk();
      if (e.key === "Escape") onCancel();
    }

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    input.addEventListener("keydown", onKey);
  });
}

// ============================================================================
// Setup Wizard
// ============================================================================

const PYTHON_ZIP_URL = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip";
const GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py";
// Kokoro model from HuggingFace (ONNX version, ~82MB)
const MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx";
const VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin";

type StepName = "python" | "deps" | "server" | "model" | "start";

let setupRunning = false;

function setStepIcon(step: StepName, state: "pending" | "running" | "done" | "error") {
  const icon = document.getElementById(`step-icon-${step}`);
  if (!icon) return;

  icon.className = `step-icon step-${state}`;

  const svgMap: Record<string, string> = {
    pending: `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" /></svg>`,
    running: `<div class="spinner"></div>`,
    done: `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6L9 17l-5-5" /></svg>`,
    error: `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>`,
  };
  icon.innerHTML = svgMap[state];
}

function showStepProgress(step: StepName, show: boolean) {
  const el = document.getElementById(`progress-${step}`);
  if (el) el.classList.toggle("hidden", !show);
}

function setStepProgress(step: StepName, percent: number, text?: string) {
  const fill = document.getElementById(`fill-${step}`) as HTMLElement;
  const txt = document.getElementById(`text-${step}`);
  if (fill) fill.style.width = `${Math.min(100, percent)}%`;
  if (txt && text) txt.textContent = text;
  else if (txt) txt.textContent = `${percent}%`;
}

function setOverallStatus(msg: string, type: "info" | "success" | "error" = "info") {
  const el = document.getElementById("setup-overall-status");
  if (!el) return;
  el.textContent = msg;
  el.className = `setup-status-msg status-${type}`;
}

async function refreshSetupStatus() {
  const banner = document.getElementById("external-server-banner");
  const stepsContainer = document.getElementById("setup-steps-container");

  try {
    const status: SetupStatus = await invoke("get_setup_status");
    setStepIcon("python", status.python_installed ? "done" : "pending");
    setStepIcon("deps", status.deps_installed ? "done" : "pending");
    setStepIcon("server", status.server_installed ? "done" : "pending");
    setStepIcon("model", status.model_downloaded ? "done" : "pending");
    setStepIcon("start", status.server_running ? "done" : "pending");

    const dataDirInput = document.getElementById("setup-data-dir") as HTMLInputElement;
    if (dataDirInput) dataDirInput.value = status.data_dir;

    const allDone = status.python_installed && status.deps_installed && status.server_installed && status.model_downloaded;
    const externalServer = status.server_running && !allDone;

    // Show/hide external-server banner and dim step list when running externally
    if (banner) banner.classList.toggle("hidden", !externalServer);
    if (stepsContainer) stepsContainer.classList.toggle("dimmed", externalServer);

    // Update button text based on status
    const btn = document.getElementById("btn-run-setup") as HTMLButtonElement;
    if (btn && !setupRunning) {
      if (allDone && status.server_running) {
        btn.innerHTML = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5" /></svg> All Set!`;
        btn.disabled = true;
        setOverallStatus("Everything is installed and running.", "success");
      } else if (externalServer) {
        btn.innerHTML = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5" /></svg> Server Active`;
        btn.disabled = true;
        setOverallStatus("", "success");
      } else if (allDone) {
        btn.textContent = "Start Server";
        btn.disabled = false;
      } else {
        btn.innerHTML = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" /></svg> Run Setup`;
        btn.disabled = false;
      }
    }
  } catch (e) {
    console.error("Failed to check setup status:", e);
    // On error, ensure banner is hidden and steps are not dimmed
    if (banner) banner.classList.add("hidden");
    if (stepsContainer) stepsContainer.classList.remove("dimmed");
  }
}

async function runSetup() {
  if (setupRunning) return;
  setupRunning = true;

  const btn = document.getElementById("btn-run-setup") as HTMLButtonElement;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Setting up...";
  }

  try {
    const status: SetupStatus = await invoke("get_setup_status");
    const paths: SetupPaths = await invoke("get_setup_paths");

    // Step 1: Download & install Python
    if (!status.python_installed) {
      setStepIcon("python", "running");
      showStepProgress("python", true);
      setOverallStatus("Downloading Python runtime...");

      const zipDest = `${paths.data_dir}\\python-embed.zip`;
      await invoke("setup_download_file", {
        url: PYTHON_ZIP_URL,
        dest: zipDest,
        stepName: "python",
      });

      setStepProgress("python", 100, "Extracting...");
      await invoke("setup_extract_zip", {
        zipPath: zipDest,
        destDir: paths.python_dir,
      });

      // Enable pip by modifying ._pth file
      await invoke("setup_enable_pip");

      // Download get-pip.py and run it
      setStepProgress("python", 100, "Installing pip...");
      const getPipDest = `${paths.python_dir}\\get-pip.py`;
      await invoke("setup_download_file", {
        url: GET_PIP_URL,
        dest: getPipDest,
        stepName: "python",
      });

      await invoke("setup_run_command", {
        program: paths.python_exe,
        args: [getPipDest, "--no-warn-script-location"],
        stepName: "python",
      });

      setStepIcon("python", "done");
      showStepProgress("python", false);
    } else {
      setStepIcon("python", "done");
    }

    // Step 2: Install Python dependencies
    if (!status.deps_installed) {
      setStepIcon("deps", "running");
      showStepProgress("deps", true);
      setStepProgress("deps", 0, "Installing packages...");
      setOverallStatus("Installing Python packages...");

      // Install main deps
      await invoke("setup_run_command", {
        program: paths.python_exe,
        args: [
          "-m", "pip", "install", "--no-warn-script-location",
          "fastapi>=0.115.0",
          "uvicorn[standard]>=0.34.0",
          "pydantic-settings>=2.7.0",
          "pyyaml>=6.0",
          "soundfile>=0.13.0",
          "numpy>=1.26.0,<2.0",
          "loguru>=0.7.0",
        ],
        stepName: "deps",
      });

      setStepProgress("deps", 60, "Installing Kokoro...");

      // Install kokoro separately (it's a bigger install)
      await invoke("setup_run_command", {
        program: paths.python_exe,
        args: ["-m", "pip", "install", "--no-warn-script-location", "kokoro>=0.3"],
        stepName: "deps",
      });

      setStepIcon("deps", "done");
      showStepProgress("deps", false);
    } else {
      setStepIcon("deps", "done");
    }

    // Step 3: Install server files
    if (!status.server_installed) {
      setStepIcon("server", "running");
      showStepProgress("server", true);
      setStepProgress("server", 50, "Copying server files...");
      setOverallStatus("Installing server files...");

      await invoke("setup_install_server");

      setStepIcon("server", "done");
      showStepProgress("server", false);
    } else {
      setStepIcon("server", "done");
    }

    // Step 4: Download model
    if (!status.model_downloaded) {
      setStepIcon("model", "running");
      showStepProgress("model", true);
      setOverallStatus("Downloading voice model (~82 MB)...");

      // Download the ONNX model
      await invoke("setup_download_file", {
        url: MODEL_URL,
        dest: `${paths.model_dir}\\kokoro-v1.0.onnx`,
        stepName: "model",
      });

      setStepProgress("model", 90, "Downloading voices...");

      // Download voices.bin
      await invoke("setup_download_file", {
        url: VOICES_URL,
        dest: `${paths.model_dir}\\voices-v1.0.bin`,
        stepName: "model",
      });

      setStepIcon("model", "done");
      showStepProgress("model", false);
    } else {
      setStepIcon("model", "done");
    }

    // Step 5: Start the server
    setStepIcon("start", "running");
    showStepProgress("start", true);
    setStepProgress("start", 50, "Starting server...");
    setOverallStatus("Starting TTS server...");

    await invoke("start_server");

    // Verify server is running
    await new Promise((r) => setTimeout(r, 3000));
    const finalStatus: SetupStatus = await invoke("get_setup_status");

    if (finalStatus.server_running) {
      setStepIcon("start", "done");
      showStepProgress("start", false);
      setOverallStatus("Setup complete! VoiceLink is ready.", "success");
      if (btn) {
        btn.innerHTML = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5" /></svg> All Set!`;
      }
    } else {
      setStepIcon("start", "error");
      setOverallStatus("Server started but may still be loading. Check Dashboard.", "info");
    }
  } catch (e) {
    console.error("Setup failed:", e);
    setOverallStatus(`Setup failed: ${e}`, "error");
    if (btn) {
      btn.textContent = "Retry Setup";
      btn.disabled = false;
    }
  } finally {
    setupRunning = false;
  }
}

function setupSetupWizard() {
  const btn = document.getElementById("btn-run-setup");
  btn?.addEventListener("click", runSetup);

  // Save path button — lets user change the data directory
  const savePathBtn = document.getElementById("btn-save-path");
  savePathBtn?.addEventListener("click", async () => {
    const input = document.getElementById("setup-data-dir") as HTMLInputElement;
    if (!input) return;
    const newDir = input.value.trim();
    if (!newDir) return;

    try {
      await invoke("set_data_dir", { newDir });
      setOverallStatus("Path saved. Refreshing status...", "success");
      await refreshSetupStatus();
    } catch (e) {
      setOverallStatus(`Failed to save path: ${e}`, "error");
    }
  });

  // Listen for progress events from Rust backend
  listen<{ step: string; progress: number; downloaded?: number; total?: number; status?: string; line?: string }>(
    "setup-progress",
    (event) => {
      const { step, progress, downloaded, total, line } = event.payload;
      const stepName = step as StepName;

      if (downloaded && total && total > 0) {
        // File download — show MB progress
        const mb = (downloaded / 1024 / 1024).toFixed(1);
        const totalMb = (total / 1024 / 1024).toFixed(1);
        setStepProgress(stepName, progress, `${mb} / ${totalMb} MB`);
      } else if (line) {
        // Command output — show the last meaningful line (e.g. pip activity)
        // Truncate long lines and show a pulsing progress bar at 50%
        const shortLine = line.length > 60 ? line.substring(0, 57) + "..." : line;
        setStepProgress(stepName, progress, shortLine);
      } else {
        setStepProgress(stepName, progress);
      }
    }
  );

  // Initial status check
  refreshSetupStatus();
}

// ============================================================================
// Refresh voices button
// ============================================================================

function setupRefreshButton() {
  document.getElementById("btn-refresh-voices")?.addEventListener("click", loadVoices);
}

// ============================================================================
// Utilities
// ============================================================================

function escapeHtml(str: string): string {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// ============================================================================
// Init
// ============================================================================

window.addEventListener("DOMContentLoaded", () => {
  setupNavigation();
  setupQuickTest();
  setupRefreshButton();
  setupSetupWizard();
  startStatusPolling();

  // Initial voice load for the dashboard quick-test dropdown
  loadVoices();
});
