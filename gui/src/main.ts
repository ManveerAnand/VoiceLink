// ============================================================================
// VoiceLink GUI — Frontend Logic
// ============================================================================
import { invoke } from "@tauri-apps/api/core";

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

  select.innerHTML = currentVoices
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
  startStatusPolling();

  // Initial voice load for the dashboard quick-test dropdown
  loadVoices();
});
