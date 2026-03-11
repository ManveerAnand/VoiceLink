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

interface GpuInfo {
  available: boolean;
  name: string | null;
  vram_total_mb: number | null;
  vram_free_mb: number | null;
  can_run_standard: boolean;
  can_run_full: boolean;
}

interface AppSettings {
  data_dir: string;
  server_port: number;
  auto_start: boolean;
  qwen3_enabled: boolean;
  qwen3_model_tier: string;
  qwen3_installed: boolean;
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
let cachedGpuInfo: GpuInfo | null = null;
let qwen3DownloadPaused = false;

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
      if (page === "voice-studio") setupVoiceStudioTabs();
      if (page === "narrate") setupNarratePage();
    });
  });
}

// ============================================================================
// Server Status
// ============================================================================

/** Update the Start/Stop button label and styling based on server state */
function updateServerToggle(btn: HTMLButtonElement | null, running: boolean) {
  if (!btn) return;
  btn.disabled = false;
  if (running) {
    btn.textContent = "Stop Server";
    btn.classList.remove("btn-success");
    btn.classList.add("btn-danger");
  } else {
    btn.textContent = "Start Server";
    btn.classList.remove("btn-danger");
    btn.classList.add("btn-success");
  }
}

/** Wire up the Start/Stop Server button on the dashboard */
function setupServerToggle() {
  const btn = document.getElementById("btn-server-toggle") as HTMLButtonElement | null;
  btn?.addEventListener("click", async () => {
    if (!btn) return;
    const isRunning = btn.classList.contains("btn-danger");
    btn.disabled = true;
    btn.textContent = isRunning ? "Stopping…" : "Starting…";
    try {
      if (isRunning) {
        await invoke("stop_server");
      } else {
        await invoke("start_server");
        // Give the server a moment to boot before re-checking
        await new Promise((r) => setTimeout(r, 2000));
      }
    } catch (e) {
      console.error("Server toggle failed:", e);
    }
    await checkServerStatus();
  });
}

async function checkServerStatus() {
  const indicator = document.getElementById("server-indicator");
  const statusEl = document.getElementById("server-status");
  const modelEl = document.getElementById("server-model");
  const deviceEl = document.getElementById("server-device");
  const voicesEl = document.getElementById("server-voices");

  const toggleBtn = document.getElementById("btn-server-toggle") as HTMLButtonElement | null;

  try {
    const result: ServerStatus = await invoke("get_server_status");

    if (result.running && result.health) {
      indicator?.classList.remove("offline");
      indicator?.classList.add("online");
      if (statusEl) statusEl.textContent = "Running";
      if (modelEl) modelEl.textContent = result.health.model ?? "—";
      if (deviceEl) deviceEl.textContent = result.health.gpu_name ?? (result.health.gpu_available ? "GPU" : "CPU");
      if (voicesEl) voicesEl.textContent = formatUptime(result.health.uptime_seconds);
      updateServerToggle(toggleBtn, true);
    } else if (result.running) {
      indicator?.classList.remove("offline");
      indicator?.classList.add("online");
      if (statusEl) statusEl.textContent = "Running (no health data)";
      updateServerToggle(toggleBtn, true);
    } else {
      indicator?.classList.remove("online");
      indicator?.classList.add("offline");
      if (statusEl) statusEl.textContent = "Offline";
      if (modelEl) modelEl.textContent = "—";
      if (deviceEl) deviceEl.textContent = "—";
      if (voicesEl) voicesEl.textContent = "—";
      updateServerToggle(toggleBtn, false);
    }
  } catch (e) {
    indicator?.classList.remove("online");
    indicator?.classList.add("offline");
    if (statusEl) statusEl.textContent = "Error";
    updateServerToggle(toggleBtn, false);
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

      // Source badge based on model and tags
      let sourceBadge = "";
      if (v.model === "qwen3") {
        if (v.tags.includes("qwen3-designed")) {
          sourceBadge = `<span class="badge badge-accent">Qwen3 Designed</span>`;
        } else if (v.tags.includes("qwen3-cloned")) {
          sourceBadge = `<span class="badge badge-warning">Qwen3 Cloned</span>`;
        } else {
          sourceBadge = `<span class="badge badge-success">Qwen3</span>`;
        }
      } else {
        sourceBadge = `<span class="badge badge-muted">Kokoro</span>`;
      }

      return `
    <div class="voice-card card ${isEnabled ? "" : "voice-disabled"}" data-voice-id="${v.id}">
      <div class="voice-header">
        <span class="voice-name">${escapeHtml(v.name)}</span>
        <div class="voice-header-right">
          ${sourceBadge}
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
  // Route to the correct backend based on voice ID prefix
  let pcmBytes: number[];
  if (voiceId.startsWith("qwen3_")) {
    pcmBytes = await invoke("qwen3_preview_voice", { voiceId, text });
  } else {
    pcmBytes = await invoke("preview_voice", { voiceId, text });
  }

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

    // Get or create a message element for alert-only mode
    let msgEl = document.getElementById("modal-message");
    if (!msgEl) {
      msgEl = document.createElement("p");
      msgEl.id = "modal-message";
      msgEl.style.cssText = "margin: 8px 0 16px; color: var(--text-secondary); font-size: 13px; line-height: 1.5;";
      input.parentElement!.insertBefore(msgEl, input);
    }

    if (alertOnly) {
      input.style.display = "none";
      msgEl.style.display = "";
      msgEl.textContent = defaultValue;
      okBtn.textContent = "OK";
      cancelBtn.style.display = "none";
    } else {
      input.style.display = "";
      msgEl.style.display = "none";
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
// Voicepack download script — uses huggingface_hub (installed with kokoro)
// Downloads the model + all 11 English voicepacks from HuggingFace to the
// local HF cache. This ensures ALL voices work, not just the default.
const VOICEPACK_DOWNLOAD_SCRIPT = `
import sys, os
try:
    from huggingface_hub import hf_hub_download
except ImportError:
    print('ERROR: huggingface_hub not installed', flush=True)
    sys.exit(1)

repo = 'hexgrad/Kokoro-82M'
voices = [
    'af_heart', 'af_bella', 'af_nicole', 'af_sarah', 'af_sky',
    'am_adam', 'am_michael',
    'bf_emma', 'bf_isabella',
    'bm_george', 'bm_lewis',
]

print('Downloading Kokoro model...', flush=True)
hf_hub_download(repo, 'kokoro-v1_0.pth')
hf_hub_download(repo, 'config.json')
print('Model downloaded.', flush=True)

for i, v in enumerate(voices, 1):
    print(f'Downloading voicepack {i}/{len(voices)}: {v}...', flush=True)
    hf_hub_download(repo, f'voices/{v}.pt')

print('All voicepacks downloaded.', flush=True)

# Write marker file so the GUI knows everything is ready
marker = os.path.join(os.environ.get('VOICELINK_DATA_DIR', '.'), '.voices_ready')
with open(marker, 'w') as f:
    f.write(','.join(voices))
print('DONE', flush=True)
`;

type StepName = "python" | "deps" | "server" | "model" | "start";

let setupRunning = false;
let stepStartTime: number | null = null;
let elapsedTimerId: number | null = null;

function formatElapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}

function startElapsedTimer(step: StepName) {
  stepStartTime = Date.now();
  if (elapsedTimerId) clearInterval(elapsedTimerId);
  elapsedTimerId = window.setInterval(() => {
    if (!stepStartTime) return;
    const elapsed = formatElapsed(Date.now() - stepStartTime);
    const txt = document.getElementById(`text-${step}`);
    if (txt) {
      // Preserve existing text, append elapsed time
      const base = txt.textContent?.replace(/\s*\(\d+[ms]\s*\d*s?\)$/, "") || "";
      txt.textContent = `${base} (${elapsed})`;
    }
  }, 1000);
}

function stopElapsedTimer() {
  if (elapsedTimerId) {
    clearInterval(elapsedTimerId);
    elapsedTimerId = null;
  }
  stepStartTime = null;
}

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
      startElapsedTimer("deps");

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

      stopElapsedTimer();
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

    // Step 4: Download model + voicepacks from HuggingFace
    if (!status.model_downloaded) {
      setStepIcon("model", "running");
      showStepProgress("model", true);
      setOverallStatus("Downloading voice model & voicepacks (~340 MB)...");
      startElapsedTimer("model");

      // Run a Python script that downloads the model and all 11 voicepacks
      // via huggingface_hub. This ensures every voice works on first use.
      await invoke("setup_run_command", {
        program: paths.python_exe,
        args: ["-c", VOICEPACK_DOWNLOAD_SCRIPT],
        stepName: "model",
        env: { VOICELINK_DATA_DIR: paths.data_dir },
      });

      stopElapsedTimer();
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
    startElapsedTimer("start");

    await invoke("start_server");

    // The server takes time to load the model (~30-60s for Kokoro ONNX).
    // Poll the health endpoint with retries instead of a single check.
    let serverReady = false;
    for (let attempt = 0; attempt < 20; attempt++) {
      setStepProgress("start", 50 + attempt * 2, `Waiting for server to load model... (${(attempt + 1) * 3}s)`);
      await new Promise((r) => setTimeout(r, 3000));
      try {
        const status: SetupStatus = await invoke("get_setup_status");
        if (status.server_running) {
          serverReady = true;
          break;
        }
      } catch (_) { /* keep trying */ }
    }

    stopElapsedTimer();
    showStepProgress("start", false);

    if (serverReady) {
      setStepIcon("start", "done");
      setOverallStatus("Setup complete! VoiceLink is ready.", "success");
      if (btn) {
        btn.innerHTML = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5" /></svg> All Set!`;
      }
    } else {
      setStepIcon("start", "error");
      setOverallStatus("Server is still loading. It may take a minute for the model to initialize. Check Dashboard.", "info");
    }
  } catch (e) {
    console.error("Setup failed:", e);
    setOverallStatus(`Setup failed: ${e}`, "error");
    if (btn) {
      btn.textContent = "Retry Setup";
      btn.disabled = false;
    }
  } finally {
    stopElapsedTimer();
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
// Settings — Auto-start toggle + server port + Qwen3
// ============================================================================

async function setupSettings() {
  const toggle = document.getElementById("setting-autostart") as HTMLInputElement | null;
  const portInput = document.getElementById("setting-server-url") as HTMLInputElement | null;

  // Load current persisted settings
  let settings: AppSettings | null = null;
  try {
    settings = await invoke<AppSettings>("get_settings");

    if (toggle) toggle.checked = settings.auto_start;
    if (portInput) portInput.value = `http://127.0.0.1:${settings.server_port}`;
  } catch (e) {
    console.error("Failed to load settings:", e);
    // Fallback: check registry directly for autostart
    try {
      if (toggle) {
        const enabled: boolean = await invoke("get_autostart");
        toggle.checked = enabled;
      }
    } catch (_) { /* ignore */ }
  }

  // Handle auto-start toggle changes
  toggle?.addEventListener("change", async () => {
    if (!toggle) return;
    try {
      await invoke("set_autostart", { enabled: toggle.checked });
    } catch (e) {
      console.error("Failed to set autostart:", e);
      toggle.checked = !toggle.checked;
    }
  });

  // Listen for watchdog giving up (server crashed too many times)
  listen("server-watchdog-gave-up", (event) => {
    console.warn("Server watchdog gave up after", event.payload, "restarts");
    const statusEl = document.getElementById("server-status");
    if (statusEl) statusEl.textContent = "Crashed (restart failed)";
  });

  // ---- Qwen3 GPU Detection & Settings ----
  await setupQwen3Settings(settings);
}

// ============================================================================
// Qwen3 Settings — GPU gated, only shown when CUDA GPU available
// ============================================================================

async function setupQwen3Settings(settings: AppSettings | null) {
  const qwen3Card = document.getElementById("qwen3-settings");
  const gpuNameEl = document.getElementById("qwen3-gpu-name");
  const vramEl = document.getElementById("qwen3-vram");
  const statusBadge = document.getElementById("qwen3-status-badge");
  const qwen3Toggle = document.getElementById("setting-qwen3-enabled") as HTMLInputElement | null;
  const tierSelect = document.getElementById("setting-qwen3-tier") as HTMLSelectElement | null;
  const detailsSection = document.getElementById("qwen3-details");
  const tierDescText = document.getElementById("qwen3-tier-text");
  const downloadBtn = document.getElementById("btn-download-qwen3") as HTMLButtonElement | null;
  const voiceStudioNav = document.getElementById("nav-voice-studio");

  // Check GPU
  try {
    cachedGpuInfo = await invoke<GpuInfo>("check_gpu");
  } catch (e) {
    console.error("GPU check failed:", e);
    // No GPU section shown
    return;
  }

  // If no NVIDIA GPU, hide Qwen3 entirely (design decision: "clear out the option")
  if (!cachedGpuInfo.available) {
    // Qwen3 card stays hidden, Voice Studio nav stays hidden
    return;
  }

  // GPU is available — show the Qwen3 settings card
  qwen3Card?.classList.remove("hidden");

  if (gpuNameEl) gpuNameEl.textContent = cachedGpuInfo.name ?? "Unknown GPU";
  if (vramEl) {
    const total = cachedGpuInfo.vram_total_mb ?? 0;
    const free = cachedGpuInfo.vram_free_mb ?? 0;
    vramEl.textContent = `${free.toLocaleString()} MB free / ${total.toLocaleString()} MB total`;
  }

  // Disable Full tier option if not enough VRAM
  if (tierSelect) {
    const fullOption = tierSelect.querySelector('option[value="full"]') as HTMLOptionElement | null;
    if (fullOption && !cachedGpuInfo.can_run_full) {
      fullOption.textContent = "Full (1.7B) — Not enough VRAM";
      fullOption.disabled = true;
    }
    // Disable Standard too if not enough VRAM for even that
    const stdOption = tierSelect.querySelector('option[value="standard"]') as HTMLOptionElement | null;
    if (stdOption && !cachedGpuInfo.can_run_standard) {
      stdOption.textContent = "Standard (0.6B) — Not enough VRAM";
      stdOption.disabled = true;
    }
  }

  // Apply persisted settings
  if (settings) {
    if (qwen3Toggle) qwen3Toggle.checked = settings.qwen3_enabled;
    if (tierSelect) tierSelect.value = settings.qwen3_model_tier;

    // Show details if enabled
    if (settings.qwen3_enabled && detailsSection) {
      detailsSection.classList.remove("hidden");
    }

    // Update status badge
    updateQwen3StatusBadge(statusBadge, settings);

    // Show Voice Studio + Narrate nav if Qwen3 enabled + installed
    if (settings.qwen3_enabled && settings.qwen3_installed) {
      voiceStudioNav?.classList.remove("hidden");
      document.getElementById("nav-narrate")?.classList.remove("hidden");
    }

    // Update download button state
    updateQwen3DownloadButton(downloadBtn, settings);
  }

  // Tier description update
  function updateTierDescription() {
    if (!tierDescText || !tierSelect) return;
    if (tierSelect.value === "full") {
      tierDescText.textContent = "All built-in voices + voice cloning + voice design + emotion control. Needs ~5 GB VRAM.";
    } else {
      tierDescText.textContent = "9 built-in voices + voice cloning. Needs ~2 GB VRAM.";
    }
  }
  updateTierDescription();

  // Handle Qwen3 enable toggle
  qwen3Toggle?.addEventListener("change", async () => {
    if (!qwen3Toggle) return;

    // Can't enable if VRAM too low for even standard
    if (qwen3Toggle.checked && !cachedGpuInfo?.can_run_standard) {
      qwen3Toggle.checked = false;
      await showModal("Not Enough VRAM", "Your GPU does not have enough VRAM to run even the Standard (0.6B) model. At least 2 GB free VRAM is required.", true);
      return;
    }

    try {
      await invoke("save_settings", { qwen3Enabled: qwen3Toggle.checked });

      if (detailsSection) {
        detailsSection.classList.toggle("hidden", !qwen3Toggle.checked);
      }

      // Refresh settings to update badge and nav
      const newSettings = await invoke<AppSettings>("get_settings");
      updateQwen3StatusBadge(statusBadge, newSettings);

      if (voiceStudioNav) {
        const showQwen3 = newSettings.qwen3_enabled && newSettings.qwen3_installed;
        voiceStudioNav.classList.toggle("hidden", !showQwen3);
        document.getElementById("nav-narrate")?.classList.toggle("hidden", !showQwen3);
      }
    } catch (e) {
      console.error("Failed to save Qwen3 setting:", e);
      qwen3Toggle.checked = !qwen3Toggle.checked;
    }
  });

  // Handle tier change
  tierSelect?.addEventListener("change", async () => {
    if (!tierSelect) return;
    updateTierDescription();
    try {
      await invoke("save_settings", { qwen3ModelTier: tierSelect.value });
      // Re-check download state since tier changed
      const newSettings = await invoke<AppSettings>("get_settings");
      updateQwen3DownloadButton(downloadBtn, newSettings);
    } catch (e) {
      console.error("Failed to save tier:", e);
    }
  });

  // Handle download button (download or resume)
  downloadBtn?.addEventListener("click", async () => {
    if (qwen3DownloadPaused) {
      qwen3DownloadPaused = false;
    }
    await downloadQwen3Models();
  });
}

function updateQwen3StatusBadge(badge: HTMLElement | null, settings: AppSettings) {
  if (!badge) return;
  if (!settings.qwen3_enabled) {
    badge.textContent = "Disabled";
    badge.className = "badge badge-muted";
  } else if (!settings.qwen3_installed) {
    badge.textContent = "Not Installed";
    badge.className = "badge badge-warning";
  } else {
    badge.textContent = "Ready";
    badge.className = "badge badge-success";
  }
}

function updateQwen3DownloadButton(btn: HTMLButtonElement | null, settings: AppSettings) {
  if (!btn) return;
  if (settings.qwen3_installed) {
    btn.textContent = "Models Installed";
    btn.disabled = true;
    btn.classList.remove("btn-primary");
    btn.classList.add("btn-secondary");
  } else {
    btn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg> Download Qwen3 Models`;
    btn.disabled = false;
    btn.classList.add("btn-primary");
    btn.classList.remove("btn-secondary");
  }
}

async function downloadQwen3Models() {
  const btn = document.getElementById("btn-download-qwen3") as HTMLButtonElement | null;
  const progressDiv = document.getElementById("progress-qwen3");
  const fillEl = document.getElementById("fill-qwen3") as HTMLElement | null;
  const textEl = document.getElementById("text-qwen3");

  // Show downloading state with a Cancel button
  if (btn) {
    btn.disabled = false;
    btn.textContent = "Cancel Download";
    btn.classList.remove("btn-primary");
    btn.classList.add("btn-warning");
    // Replace click handler with cancel logic temporarily
    btn.onclick = async () => {
      qwen3DownloadPaused = true;
      btn.disabled = true;
      btn.textContent = "Cancelling...";
      try { await invoke("cancel_qwen3_download"); } catch (_) { /* may already be done */ }
    };
  }
  progressDiv?.classList.remove("hidden");

  // Listen for real-time progress from the download script
  const unlisten = await listen<{ step: string; line?: string }>("setup-progress", (event) => {
    const { step, line } = event.payload;
    if (!line || !textEl) return;
    // Only handle qwen3 download events
    if (step !== "qwen3-download" && step !== "qwen3-check" && step !== "qwen3-pip") return;

    if (line.startsWith("PROGRESS:")) {
      // e.g. "PROGRESS: 42% 1250/3000 MB -- Qwen3-TTS-12Hz-0.6B-Base/model.safetensors"
      const match = line.match(/PROGRESS:\s*(\d+)%\s*(\d+)\/(\d+)\s*MB\s*--\s*(.*)/);
      if (match) {
        const [, pct, doneMb, totalMb, fileName] = match;
        if (fillEl) fillEl.style.width = `${pct}%`;
        const shortFile = fileName.length > 40 ? fileName.substring(0, 37) + "..." : fileName;
        textEl.textContent = `${pct}% (${doneMb}/${totalMb} MB) ${shortFile}`;
      }
    } else if (line.startsWith("SIZE:")) {
      // e.g. "SIZE: 3200 MB total across 3 models"
      const match = line.match(/SIZE:\s*(\d+)\s*MB/);
      if (match) {
        textEl.textContent = `Total download: ~${match[1]} MB`;
      }
    } else if (line.startsWith("DOWNLOAD:")) {
      // e.g. "DOWNLOAD: [2/3] Qwen/Qwen3-TTS-12Hz-0.6B-Base"
      const match = line.match(/\[(\d+)\/(\d+)\]\s*(.*)/);
      if (match) {
        const shortName = match[3].split("/").pop() ?? match[3];
        textEl.textContent = `Downloading ${shortName} (${match[1]}/${match[2]})...`;
      }
    } else if (line.startsWith("OK:") && line.includes("->")) {
      const parts = line.match(/OK:\s*(.*?)\s*->/);
      if (parts) {
        const shortName = parts[1].split("/").pop() ?? parts[1];
        textEl.textContent = `Downloaded ${shortName}`;
      }
    }
  });

  try {
    // Get paths and tier
    const settings = await invoke<AppSettings>("get_settings");
    const paths: SetupPaths = await invoke("get_setup_paths");
    const tier = settings.qwen3_model_tier;
    const pythonExe = "python";

    // Step 1: Ensure huggingface_hub is installed (quick, no-op if present)
    if (textEl) textEl.textContent = "Checking dependencies...";
    if (fillEl) fillEl.style.width = "5%";

    try {
      await invoke("setup_run_command", {
        program: pythonExe,
        args: ["-c", "import huggingface_hub; print('OK')"],
        stepName: "qwen3-check",
      });
    } catch {
      if (textEl) textEl.textContent = "Installing huggingface_hub...";
      await invoke("setup_run_command", {
        program: pythonExe,
        args: ["-m", "pip", "install", "--no-warn-script-location", "huggingface_hub"],
        stepName: "qwen3-pip",
      });
    }

    // Step 2: Download models using the robust downloader script.
    if (textEl) textEl.textContent = "Starting model download...";
    if (fillEl) fillEl.style.width = "10%";

    const scriptPath = paths.server_dir.replace(/[/\\]$/, "") + "\\download_qwen3.py";
    const result = await invoke<string>("setup_run_command", {
      program: pythonExe,
      args: [scriptPath, "--tier", tier, "--data-dir", paths.data_dir],
      stepName: "qwen3-download",
      env: { VOICELINK_DATA_DIR: paths.data_dir },
    });

    // Check result
    const isDone = result.includes("DONE");
    const isPartial = result.includes("PARTIAL");

    if (isDone || isPartial) {
      await invoke("save_settings", { qwen3Installed: true });

      if (fillEl) fillEl.style.width = "100%";
      if (textEl) textEl.textContent = isDone ? "All models downloaded!" : "Downloaded (some models had issues)";

      const newSettings = await invoke<AppSettings>("get_settings");
      const statusBadge = document.getElementById("qwen3-status-badge");
      updateQwen3StatusBadge(statusBadge, newSettings);
      updateQwen3DownloadButton(btn, newSettings);

      const voiceStudioNav = document.getElementById("nav-voice-studio");
      if (newSettings.qwen3_enabled && newSettings.qwen3_installed) {
        voiceStudioNav?.classList.remove("hidden");
        document.getElementById("nav-narrate")?.classList.remove("hidden");
      }
    } else {
      throw new Error("Download completed but could not verify success");
    }
  } catch (e) {
    console.error("Qwen3 download failed:", e);
    const errStr = String(e);

    if (qwen3DownloadPaused) {
      // User paused — show resume button
      if (textEl) textEl.textContent = "Download paused. Progress is saved — resume anytime.";
      if (fillEl) fillEl.style.width = "0%";
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Resume Download";
        btn.classList.remove("btn-warning");
        btn.classList.add("btn-primary");
        btn.onclick = null; // Reset to default handler
      }
    } else {
      // Actual error
      const shortErr = extractDownloadError(errStr);
      if (textEl) textEl.textContent = `Failed: ${shortErr}`;
      if (fillEl) fillEl.style.width = "0%";
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Retry Download";
        btn.classList.remove("btn-warning");
        btn.classList.add("btn-primary");
        btn.onclick = null;
      }
    }
  } finally {
    unlisten();
  }
}

/** Extract the most useful error line from a download failure message */
function extractDownloadError(error: string): string {
  // Look for our structured error messages first
  const errorLine = error.split("\n").find(l => l.includes("ERROR:") || l.includes("FATAL:"));
  if (errorLine) {
    return errorLine.replace(/^.*?(ERROR:|FATAL:)\s*/, "").trim();
  }
  // Fallback: truncate long errors
  const clean = error.replace(/^.*?Command failed:\s*/s, "").trim();
  return clean.length > 200 ? clean.slice(0, 200) + "..." : clean;
}

// ============================================================================
// Voice Studio — Clone, Design, and Built-in Qwen3 voice management
// ============================================================================

function setupVoiceStudioTabs() {
  // Tab switching
  const tabs = document.querySelectorAll<HTMLElement>(".studio-tab");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const targetTab = tab.dataset.tab;
      if (!targetTab) return;

      tabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");

      document.querySelectorAll<HTMLElement>(".studio-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.id === `studio-${targetTab}`);
      });
    });
  });

  // Clone form validation — enable Preview button when all fields filled
  setupCloneForm();

  // Design form validation — enable Generate button when fields filled
  setupDesignForm();

  // Load built-in Qwen3 voices list
  loadQwen3Voices();
}

// ---- Clone Voice Form ----

function setupCloneForm() {
  const nameInput = document.getElementById("clone-name") as HTMLInputElement | null;
  const audioInput = document.getElementById("clone-audio") as HTMLInputElement | null;
  const transcriptInput = document.getElementById("clone-transcript") as HTMLInputElement | null;
  const genderSelect = document.getElementById("clone-gender") as HTMLSelectElement | null;
  const descriptionInput = document.getElementById("clone-description") as HTMLInputElement | null;
  const cloneBtn = document.getElementById("btn-clone-create") as HTMLButtonElement | null;
  const tempContainer = document.getElementById("clone-temp-voices");

  if (!nameInput || !audioInput || !transcriptInput || !cloneBtn || !tempContainer) return;

  // Track temp clones in memory (gone when app closes)
  interface TempClone {
    voiceId: string;
    name: string;
    gender: string;
    description: string;
    saved: boolean;
  }
  const tempClones: TempClone[] = [];

  function validateCloneForm() {
    const valid =
      nameInput!.value.trim().length > 0 &&
      audioInput!.files != null &&
      audioInput!.files.length > 0 &&
      transcriptInput!.value.trim().length > 0;
    cloneBtn!.disabled = !valid;
  }

  nameInput.addEventListener("input", validateCloneForm);
  audioInput.addEventListener("change", validateCloneForm);
  transcriptInput.addEventListener("input", validateCloneForm);

  function renderTempCards() {
    if (!tempContainer) return;
    if (tempClones.length === 0) {
      tempContainer.innerHTML = "";
      return;
    }
    tempContainer.innerHTML = tempClones.map((clone, idx) => `
      <div class="clone-temp-card" data-idx="${idx}" data-voice-id="${escapeHtml(clone.voiceId)}">
        <div class="clone-temp-header">
          <div class="clone-temp-info">
            <span class="clone-temp-name">${escapeHtml(clone.name)}</span>
            <span class="voice-badge ${clone.gender}">${escapeHtml(clone.gender === "unknown" ? "Other" : clone.gender)}</span>
            <span class="clone-temp-badge ${clone.saved ? "saved" : "temp"}">${clone.saved ? "Saved" : "Unsaved"}</span>
          </div>
        </div>
        ${clone.description ? `<div class="clone-temp-desc">${escapeHtml(clone.description)}</div>` : ""}
        <div class="clone-temp-test">
          <input type="text" class="temp-test-text" placeholder="Type text to test this voice..." value="Hello, this is a test of my cloned voice." />
          <button class="btn btn-test-play temp-test-btn" data-idx="${idx}">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            Test
          </button>
        </div>
        <div class="clone-temp-actions">
          <button class="btn btn-discard temp-discard-btn" data-idx="${idx}">Discard</button>
          ${!clone.saved ? `<button class="btn btn-save-lib temp-save-btn" data-idx="${idx}">Save to Library</button>` : ""}
        </div>
      </div>
    `).join("");

    // Wire test buttons
    tempContainer.querySelectorAll<HTMLButtonElement>(".temp-test-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const idx = parseInt(btn.dataset.idx || "0");
        const clone = tempClones[idx];
        if (!clone) return;
        const card = btn.closest(".clone-temp-card") as HTMLElement;
        const textInput = card?.querySelector(".temp-test-text") as HTMLInputElement;
        const text = textInput?.value.trim();
        if (!text) return;

        btn.disabled = true;
        btn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg> Playing...`;

        try {
          const pcmBytes = await invoke<number[]>("qwen3_preview_voice", {
            voiceId: clone.voiceId,
            text,
          });
          if (pcmBytes && pcmBytes.length > 0) {
            playPcmAudio(new Uint8Array(pcmBytes));
          }
        } catch (e: any) {
          console.error("Test clone failed:", e);
          await showModal("Error", `Test failed: ${e}`, true);
        } finally {
          btn.disabled = false;
          btn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg> Test`;
        }
      });
    });

    // Wire save buttons
    tempContainer.querySelectorAll<HTMLButtonElement>(".temp-save-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const idx = parseInt(btn.dataset.idx || "0");
        const clone = tempClones[idx];
        if (!clone) return;

        btn.disabled = true;
        btn.textContent = "Saving...";

        try {
          clone.saved = true;
          renderTempCards();
          await showModal("Saved", `"${clone.name}" saved to your voice library. You can find it in Qwen3 Voices.`, true);
          loadQwen3Voices();
        } catch (e: any) {
          clone.saved = false;
          console.error("Save failed:", e);
          await showModal("Error", `Failed to save: ${e}`, true);
          renderTempCards();
        }
      });
    });

    // Wire discard buttons
    tempContainer.querySelectorAll<HTMLButtonElement>(".temp-discard-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const idx = parseInt(btn.dataset.idx || "0");
        const clone = tempClones[idx];
        if (!clone) return;

        // Delete from server
        try {
          await invoke("qwen3_delete_clone", { voiceId: clone.voiceId });
        } catch (e) {
          console.warn("Server cleanup failed (may already be removed):", e);
        }

        tempClones.splice(idx, 1);
        renderTempCards();
      });
    });
  }

  // Clone button: create the clone, play preview, show temp card
  cloneBtn.addEventListener("click", async () => {
    const file = audioInput.files?.[0];
    if (!file) return;

    cloneBtn.disabled = true;
    cloneBtn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg> Cloning...`;

    try {
      const arrayBuffer = await file.arrayBuffer();
      const audioData = Array.from(new Uint8Array(arrayBuffer));

      const pcmBytes = await invoke<number[]>("qwen3_clone_voice", {
        name: nameInput.value.trim(),
        transcript: transcriptInput.value.trim(),
        audioData,
        audioFilename: file.name,
        gender: genderSelect?.value || "unknown",
        description: descriptionInput?.value.trim() || "",
        previewText: "Hello, this is a preview of the cloned voice.",
      });

      const safeName = nameInput.value.trim().replace(/[^a-zA-Z0-9_\- ]/g, "").trim();
      const voiceId = `qwen3_custom_${safeName}`;

      // Play the preview audio
      if (pcmBytes && pcmBytes.length > 0) {
        playPcmAudio(new Uint8Array(pcmBytes));
      }

      // Add temp card
      tempClones.push({
        voiceId,
        name: nameInput.value.trim(),
        gender: genderSelect?.value || "unknown",
        description: descriptionInput?.value.trim() || "",
        saved: false,
      });
      renderTempCards();

      // Reset form for next clone
      nameInput.value = "";
      audioInput.value = "";
      transcriptInput.value = "";
      if (descriptionInput) descriptionInput.value = "";
      validateCloneForm();

    } catch (e: any) {
      console.error("Clone failed:", e);
      await showModal("Error", `Voice cloning failed: ${e}`, true);
    } finally {
      cloneBtn.disabled = false;
      cloneBtn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg> Clone Voice`;
      validateCloneForm();
    }
  });
}

// ---- Design Voice Form ----

function setupDesignForm() {
  const nameInput = document.getElementById("design-name") as HTMLInputElement | null;
  const descInput = document.getElementById("design-description") as HTMLTextAreaElement | null;
  const generateBtn = document.getElementById("btn-design-generate") as HTMLButtonElement | null;
  const saveBtn = document.getElementById("btn-design-save") as HTMLButtonElement | null;

  if (!nameInput || !descInput || !generateBtn || !saveBtn) return;

  let lastDesignVoiceId: string | null = null;

  function validateDesignForm() {
    const valid =
      nameInput!.value.trim().length > 0 &&
      descInput!.value.trim().length >= 10;
    generateBtn!.disabled = !valid;
  }

  nameInput.addEventListener("input", validateDesignForm);
  descInput.addEventListener("input", validateDesignForm);

  // Generate: design the voice and play preview
  generateBtn.addEventListener("click", async () => {
    generateBtn.disabled = true;
    generateBtn.textContent = "Generating...";

    try {
      const pcmBytes = await invoke<number[]>("qwen3_design_voice", {
        name: nameInput.value.trim(),
        description: descInput.value.trim(),
        sampleText: "Hello, this is a preview of the designed voice.",
      });

      if (pcmBytes && pcmBytes.length > 0) {
        playPcmAudio(new Uint8Array(pcmBytes));
        lastDesignVoiceId = `qwen3_custom_${nameInput.value.trim().replace(/[^a-zA-Z0-9_-]/g, "_")}`;
        saveBtn.disabled = false;
      }
    } catch (e: any) {
      console.error("Design voice failed:", e);
      alert(`Voice design failed: ${e}`);
    } finally {
      generateBtn.disabled = false;
      generateBtn.textContent = "Generate";
    }
  });

  // Save: register the designed voice in SAPI
  saveBtn.addEventListener("click", async () => {
    if (!lastDesignVoiceId) return;
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving...";

    try {
      await invoke("toggle_voice", { voiceId: lastDesignVoiceId, enabled: true });
      alert(`Voice "${nameInput.value.trim()}" saved and registered in SAPI!`);
      loadVoices();
      loadQwen3Voices();
    } catch (e: any) {
      console.error("Save designed voice failed:", e);
      alert(`Failed to save voice: ${e}`);
    } finally {
      saveBtn.textContent = "Save to Library";
    }
  });
}

// ---- Built-in Qwen3 Voices ----

async function loadQwen3Voices() {
  const container = document.getElementById("qwen3-voices-list");
  if (!container) return;

  try {
    const speakers = await invoke<any[]>("qwen3_list_speakers");
    if (!speakers || speakers.length === 0) {
      container.innerHTML = `<p class="placeholder">No Qwen3 voices found. Download the models first.</p>`;
      return;
    }

    container.innerHTML = speakers
      .map(
        (v) => `
      <div class="voice-card card" data-voice-id="${escapeHtml(v.id)}">
        <div class="voice-header">
          <span class="voice-name">${escapeHtml(v.name)}</span>
          <div class="voice-header-right">
            <span class="voice-badge ${v.gender?.toLowerCase() || ""}">${escapeHtml(v.gender || "")}</span>
            ${v.tags?.map((t: string) => `<span class="voice-tag">${escapeHtml(t)}</span>`).join("") || ""}
          </div>
        </div>
        <div class="voice-description">${escapeHtml(v.description || "")}</div>
        <div class="voice-actions">
          <button class="btn btn-sm btn-secondary qwen3-preview-btn" data-voice="${escapeHtml(v.id)}">Preview</button>
        </div>
      </div>
    `
      )
      .join("");

    // Wire preview buttons
    container.querySelectorAll<HTMLButtonElement>(".qwen3-preview-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const voiceId = btn.dataset.voice;
        if (!voiceId) return;

        btn.disabled = true;
        btn.textContent = "Playing...";

        try {
          const pcmBytes = await invoke<number[]>("qwen3_preview_voice", {
            voiceId,
            text: "Hello! This is a preview of my voice.",
          });
          if (pcmBytes && pcmBytes.length > 0) {
            playPcmAudio(new Uint8Array(pcmBytes));
          }
        } catch (e: any) {
          console.error("Qwen3 preview failed:", e);
        } finally {
          btn.disabled = false;
          btn.textContent = "Preview";
        }
      });
    });
  } catch (e) {
    container.innerHTML = `<p class="placeholder">Could not load Qwen3 voices. Is the server running?</p>`;
    console.error("Load Qwen3 voices failed:", e);
  }
}

// ---- PCM Audio Playback ----

// ============================================================================
// Narrate Page
// ============================================================================

let narrateInitialized = false;
let narratePcmData: Uint8Array | null = null;
let narrateAudioCtx: AudioContext | null = null;
let narrateSource: AudioBufferSourceNode | null = null;
let narrateBuffer: AudioBuffer | null = null;
let narrateStartTime = 0;
let narrateOffset = 0;
let narratePlaying = false;
let narrateRafId = 0;

function setupNarratePage() {
  const voiceSelect = document.getElementById("narrate-voice") as HTMLSelectElement | null;
  const textArea = document.getElementById("narrate-text") as HTMLTextAreaElement | null;
  const fileInput = document.getElementById("narrate-file") as HTMLInputElement | null;
  const speedSlider = document.getElementById("narrate-speed") as HTMLInputElement | null;
  const speedLabel = document.getElementById("narrate-speed-label");
  const charCount = document.getElementById("narrate-char-count");
  const generateBtn = document.getElementById("btn-narrate-generate") as HTMLButtonElement | null;

  if (!voiceSelect || !textArea) return;

  // Populate voice selector with Qwen3 voices
  const qwen3Voices = currentVoices.filter((v) => v.id.startsWith("qwen3_"));
  voiceSelect.innerHTML = qwen3Voices
    .map((v) => `<option value="${v.id}">${escapeHtml(v.name)}</option>`)
    .join("");

  if (narrateInitialized) return;
  narrateInitialized = true;

  // Speed slider
  speedSlider?.addEventListener("input", () => {
    if (speedLabel) speedLabel.textContent = `${parseFloat(speedSlider.value).toFixed(2)}×`;
  });

  // Character count
  textArea.addEventListener("input", () => {
    const len = textArea.value.length;
    if (charCount) charCount.textContent = `${len.toLocaleString()} / 50,000`;
    if (generateBtn) generateBtn.disabled = len === 0;
  });

  // File upload
  fileInput?.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      textArea.value = (reader.result as string).substring(0, 50000);
      textArea.dispatchEvent(new Event("input"));
    };
    reader.readAsText(file);
  });

  // Generate narration
  generateBtn?.addEventListener("click", async () => {
    const text = textArea.value.trim();
    if (!text) return;

    const voiceId = voiceSelect.value;
    const language = (document.getElementById("narrate-language") as HTMLSelectElement)?.value || "auto";
    const speed = parseFloat(speedSlider?.value || "1.0");

    const progressDiv = document.getElementById("narrate-progress");
    const statusEl = document.getElementById("narrate-status");
    const progressBar = document.getElementById("narrate-progress-bar");
    const resultDiv = document.getElementById("narrate-result");

    generateBtn.disabled = true;
    generateBtn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg> Generating...`;
    if (progressDiv) progressDiv.style.display = "block";
    if (resultDiv) resultDiv.style.display = "none";
    if (statusEl) statusEl.textContent = "Generating narration...";
    if (progressBar) progressBar.style.width = "30%";

    // Stop any playing audio
    narrateStopPlayback();

    try {
      const pcmBytes = await invoke<number[]>("qwen3_narrate", {
        voiceId,
        text,
        language,
        speed,
      });

      if (progressBar) progressBar.style.width = "100%";
      if (statusEl) statusEl.textContent = "Done!";

      if (pcmBytes && pcmBytes.length > 0) {
        narratePcmData = new Uint8Array(pcmBytes);
        const totalSecs = (narratePcmData.length / 2) / 24000;

        // Duration badge
        const durationEl = document.getElementById("narrate-duration");
        if (durationEl) durationEl.textContent = formatTime(totalSecs);

        // Time displays
        const timeTotal = document.getElementById("narrate-time-total");
        if (timeTotal) timeTotal.textContent = formatTime(totalSecs);
        const timeCur = document.getElementById("narrate-time");
        if (timeCur) timeCur.textContent = "0:00";

        // Reset seek
        const seekFill = document.getElementById("narrate-seek-fill");
        if (seekFill) seekFill.style.width = "0%";

        // Prepare AudioBuffer
        narrateAudioCtx = new AudioContext({ sampleRate: 24000 });
        const int16 = new Int16Array(narratePcmData.buffer, narratePcmData.byteOffset, narratePcmData.byteLength / 2);
        const float32 = new Float32Array(int16.length);
        for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;
        narrateBuffer = narrateAudioCtx.createBuffer(1, float32.length, 24000);
        narrateBuffer.copyToChannel(float32, 0);
        narrateOffset = 0;

        if (resultDiv) resultDiv.style.display = "block";
      }
    } catch (e: any) {
      console.error("Narration failed:", e);
      if (statusEl) statusEl.textContent = `Error: ${e}`;
    } finally {
      generateBtn.disabled = false;
      generateBtn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3" /></svg> Generate`;
      setTimeout(() => { if (progressDiv) progressDiv.style.display = "none"; }, 2000);
    }
  });

  // Play/Pause button
  document.getElementById("btn-narrate-playpause")?.addEventListener("click", () => {
    if (narratePlaying) {
      narrateStopPlayback();
    } else {
      narrateStartPlayback(narrateOffset);
    }
  });

  // Seek bar click
  document.getElementById("narrate-seek")?.addEventListener("click", (e) => {
    if (!narrateBuffer) return;
    const bar = e.currentTarget as HTMLElement;
    const rect = bar.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const seekTo = pct * narrateBuffer.duration;
    if (narratePlaying) {
      narrateStopPlayback();
      narrateStartPlayback(seekTo);
    } else {
      narrateOffset = seekTo;
      updateNarrateSeekUI(pct);
      const timeCur = document.getElementById("narrate-time");
      if (timeCur) timeCur.textContent = formatTime(seekTo);
    }
  });

  // Download WAV
  document.getElementById("btn-narrate-download")?.addEventListener("click", () => {
    if (!narratePcmData) return;
    const wavBlob = pcmToWavBlob(narratePcmData, 24000);
    const url = URL.createObjectURL(wavBlob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "narration.wav";
    a.click();
    URL.revokeObjectURL(url);
  });
}

function narrateStartPlayback(fromOffset: number) {
  if (!narrateAudioCtx || !narrateBuffer) return;

  // Resume if suspended
  if (narrateAudioCtx.state === "suspended") narrateAudioCtx.resume();

  narrateSource = narrateAudioCtx.createBufferSource();
  narrateSource.buffer = narrateBuffer;
  narrateSource.connect(narrateAudioCtx.destination);
  narrateStartTime = narrateAudioCtx.currentTime - fromOffset;
  narrateSource.start(0, fromOffset);
  narratePlaying = true;

  // Show pause icon
  const iconPlay = document.getElementById("narrate-icon-play");
  const iconPause = document.getElementById("narrate-icon-pause");
  if (iconPlay) iconPlay.style.display = "none";
  if (iconPause) iconPause.style.display = "block";

  narrateSource.onended = () => {
    if (narratePlaying) {
      narratePlaying = false;
      narrateOffset = 0;
      cancelAnimationFrame(narrateRafId);
      if (iconPlay) iconPlay.style.display = "block";
      if (iconPause) iconPause.style.display = "none";
      updateNarrateSeekUI(0);
      const timeCur = document.getElementById("narrate-time");
      if (timeCur) timeCur.textContent = "0:00";
    }
  };

  // Animate seek bar
  function tick() {
    if (!narratePlaying || !narrateAudioCtx || !narrateBuffer) return;
    const elapsed = narrateAudioCtx.currentTime - narrateStartTime;
    const pct = Math.min(elapsed / narrateBuffer.duration, 1);
    updateNarrateSeekUI(pct);
    const timeCur = document.getElementById("narrate-time");
    if (timeCur) timeCur.textContent = formatTime(elapsed);
    narrateRafId = requestAnimationFrame(tick);
  }
  narrateRafId = requestAnimationFrame(tick);
}

function narrateStopPlayback() {
  if (narrateSource) {
    narratePlaying = false; // set before stop to prevent onended reset
    if (narrateAudioCtx) {
      narrateOffset = narrateAudioCtx.currentTime - narrateStartTime;
    }
    try { narrateSource.stop(); } catch (_) { /* ignore */ }
    narrateSource = null;
  }
  cancelAnimationFrame(narrateRafId);

  // Show play icon
  const iconPlay = document.getElementById("narrate-icon-play");
  const iconPause = document.getElementById("narrate-icon-pause");
  if (iconPlay) iconPlay.style.display = "block";
  if (iconPause) iconPause.style.display = "none";
}

function updateNarrateSeekUI(pct: number) {
  const fill = document.getElementById("narrate-seek-fill");
  if (fill) fill.style.width = `${(pct * 100).toFixed(1)}%`;
}

function formatTime(secs: number): string {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/** Convert raw 16-bit PCM bytes to a WAV Blob */
function pcmToWavBlob(pcmData: Uint8Array, sampleRate: number): Blob {
  const numSamples = pcmData.length / 2;
  const buffer = new ArrayBuffer(44 + pcmData.length);
  const view = new DataView(buffer);

  // RIFF header
  writeString(view, 0, "RIFF");
  view.setUint32(4, 36 + pcmData.length, true);
  writeString(view, 8, "WAVE");

  // fmt chunk
  writeString(view, 12, "fmt ");
  view.setUint32(16, 16, true);         // chunk size
  view.setUint16(20, 1, true);          // PCM format
  view.setUint16(22, 1, true);          // mono
  view.setUint32(24, sampleRate, true); // sample rate
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true);          // block align
  view.setUint16(34, 16, true);         // bits per sample

  // data chunk
  writeString(view, 36, "data");
  view.setUint32(40, pcmData.length, true);
  new Uint8Array(buffer, 44).set(pcmData);

  return new Blob([buffer], { type: "audio/wav" });
}

function writeString(view: DataView, offset: number, str: string) {
  for (let i = 0; i < str.length; i++) {
    view.setUint8(offset + i, str.charCodeAt(i));
  }
}

// ============================================================================
// PCM Playback
// ============================================================================

function playPcmAudio(pcmData: Uint8Array) {
  try {
    const audioCtx = new AudioContext({ sampleRate: 24000 });
    const int16 = new Int16Array(pcmData.buffer, pcmData.byteOffset, pcmData.byteLength / 2);
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) {
      float32[i] = int16[i] / 32768;
    }
    const buffer = audioCtx.createBuffer(1, float32.length, 24000);
    buffer.copyToChannel(float32, 0);
    const source = audioCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(audioCtx.destination);
    source.start();
    source.onended = () => audioCtx.close();
  } catch (e) {
    console.error("PCM playback failed:", e);
  }
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

window.addEventListener("DOMContentLoaded", async () => {
  setupNavigation();
  setupQuickTest();
  setupServerToggle();
  setupRefreshButton();
  setupSetupWizard();
  setupSettings();
  startStatusPolling();

  // Initial voice load for the dashboard quick-test dropdown
  loadVoices();

  // Auto-start server on launch if setup is complete but server isn't running
  try {
    const status: SetupStatus = await invoke("get_setup_status");
    if (
      status.python_installed &&
      status.deps_installed &&
      status.server_installed &&
      status.model_downloaded &&
      !status.server_running
    ) {
      console.log("Setup complete but server offline — auto-starting...");
      await invoke("start_server");
    }
  } catch (e) {
    console.error("Auto-start check failed:", e);
  }
});
