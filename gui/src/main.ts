// ============================================================================
// VoiceLink GUI — Frontend Logic
// ============================================================================
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { revealItemInDir } from "@tauri-apps/plugin-opener";

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
  /** Python packages AND this tier's models are on disk (computed by the app, not a saved flag) */
  qwen3_installed: boolean;
  qwen3_runtime_installed: boolean;
  qwen3_models_installed: boolean;
  qwen3_full_models_installed: boolean;
  /** Anything of Qwen3 on disk: shows "Remove Qwen3" even after a partial install or removal */
  qwen3_remnants: boolean;
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
let voicesLoaded = false;
let favoriteIds: string[] = [];
let removedIds: string[] = [];
let defaultVoice: { id: string | null; name: string } = { id: null, name: "" };
let testPicker: VoicePicker | null = null;
let narratePicker: VoicePicker | null = null;
let cachedGpuInfo: GpuInfo | null = null;
let qwen3DownloadPaused = false;
let qwen3Downloading = false;

// ============================================================================
// Setup-incomplete gray-out state
// ============================================================================

let setupComplete = false;

// Setup lives in the sidebar only until everything is installed; the Settings
// "Repair / re-run setup" link brings it back. Stay visible while it is open.
function syncSetupNav() {
  const onSetup = document.getElementById("page-setup")?.classList.contains("active");
  document.getElementById("nav-setup-item")?.classList.toggle("hidden", setupComplete && !onSetup);
}

function wireRepairSetupLink() {
  document.getElementById("btn-repair-setup")?.addEventListener("click", () => {
    const nav = document.getElementById("nav-setup-item");
    nav?.classList.remove("hidden");
    nav?.click();
  });
}

function applySetupIncompleteState(isComplete: boolean) {
  setupComplete = isComplete;
  syncSetupNav();
  const dashboard = document.getElementById("page-dashboard");
  const voices = document.getElementById("page-voices");
  const dashBanner = document.getElementById("dashboard-setup-banner");
  const voicesBanner = document.getElementById("voices-setup-banner");

  if (isComplete) {
    dashboard?.classList.remove("setup-incomplete");
    voices?.classList.remove("setup-incomplete");
    dashBanner?.classList.add("hidden");
    voicesBanner?.classList.add("hidden");
  } else {
    dashboard?.classList.add("setup-incomplete");
    voices?.classList.add("setup-incomplete");
    dashBanner?.classList.remove("hidden");
    voicesBanner?.classList.remove("hidden");
  }
}

function wireGotoSetupButtons() {
  const navigate = (page: string) => {
    document.querySelectorAll<HTMLElement>(".nav-item").forEach((n) => {
      n.classList.toggle("active", n.dataset.page === page);
    });
    document.querySelectorAll<HTMLElement>(".page").forEach((p) => {
      p.classList.toggle("active", p.id === `page-${page}`);
    });
    if (page === "setup") refreshSetupStatus();
  };

  document.getElementById("btn-goto-setup")?.addEventListener("click", () => navigate("setup"));
  document.querySelectorAll<HTMLElement>(".btn-goto-setup").forEach((btn) => {
    btn.addEventListener("click", () => navigate("setup"));
  });
}

// ============================================================================
// Spotlight Onboarding Tour
// ============================================================================

interface TourStep {
  targetId: string;
  title: string;
  body: string;
  position: "top" | "bottom" | "left" | "right";
  waitForNav?: string;
}

const TOUR_STEPS: TourStep[] = [
  {
    targetId: "nav-setup-item",
    title: "Start Here",
    body: "Click Setup to download the AI voice engine and get everything ready.",
    position: "right",
    waitForNav: "setup",
  },
  {
    targetId: "btn-run-setup",
    title: "Run Setup",
    body: "Click this button to download and install everything automatically. It takes a few minutes.",
    position: "bottom",
  },
];

const TOUR_POST_SETUP_STEPS: TourStep[] = [
  {
    targetId: "nav-dashboard-item",
    title: "Dashboard",
    body: "Your server is running! Monitor the status and test voices from here.",
    position: "right",
  },
  {
    targetId: "nav-voices-item",
    title: "Voice Manager",
    body: "Browse, rename, enable, or disable your AI voices here.",
    position: "right",
  },
];

class SpotlightTour {
  private steps: TourStep[];
  private currentIndex = 0;
  private overlay: HTMLDivElement | null = null;
  private highlight: HTMLDivElement | null = null;
  private tooltip: HTMLDivElement | null = null;
  private active = false;
  private waitingForNav: string | null = null;

  constructor(steps: TourStep[]) {
    this.steps = steps;
  }

  start() {
    if (this.steps.length === 0 || this.active) return;
    this.active = true;
    this.currentIndex = 0;
    this.createOverlay();
    this.showStep(0);
  }

  private createOverlay() {
    this.overlay = document.createElement("div");
    this.overlay.className = "spotlight-overlay";

    this.highlight = document.createElement("div");
    this.highlight.className = "spotlight-highlight";

    this.tooltip = document.createElement("div");
    this.tooltip.className = "spotlight-tooltip";

    this.overlay.appendChild(this.highlight);
    this.overlay.appendChild(this.tooltip);
    document.body.appendChild(this.overlay);

    this.overlay.addEventListener("click", (e) => {
      if (e.target === this.overlay) {
        // Allow clicking through to the highlighted target
      }
    });

    window.addEventListener("resize", this.onResize);
    window.addEventListener("keydown", this.onKeyDown);
  }

  private onResize = () => this.showStep(this.currentIndex);

  // Escape always closes the tour, even if the tooltip is somehow unreachable
  private onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Escape") this.end();
  };

  private showStep(index: number) {
    if (!this.overlay || !this.highlight || !this.tooltip) return;
    if (index >= this.steps.length) {
      this.end();
      return;
    }

    this.currentIndex = index;
    const step = this.steps[index];
    const target = document.querySelector(`[data-page="${step.targetId}"]`)
      || document.getElementById(step.targetId);

    if (!target) {
      this.next();
      return;
    }

    const rect = (target as HTMLElement).getBoundingClientRect();
    const pad = 8;

    this.highlight.style.left = `${rect.left - pad}px`;
    this.highlight.style.top = `${rect.top - pad}px`;
    this.highlight.style.width = `${rect.width + pad * 2}px`;
    this.highlight.style.height = `${rect.height + pad * 2}px`;

    const totalSteps = this.steps.length;
    this.tooltip.innerHTML = `
      <div class="spotlight-step-indicator">Step ${index + 1} of ${totalSteps}</div>
      <h4>${step.title}</h4>
      <p>${step.body}</p>
      <div class="spotlight-actions">
        <button class="btn-skip-tour">Skip Tour</button>
        <button class="btn-next-tour">${index === totalSteps - 1 ? "Finish" : "Next"}</button>
      </div>
    `;

    this.positionTooltip(rect, step.position);

    this.tooltip.querySelector(".btn-skip-tour")?.addEventListener("click", () => this.end());
    this.tooltip.querySelector(".btn-next-tour")?.addEventListener("click", () => {
      if (step.waitForNav) {
        this.waitingForNav = step.waitForNav;
        this.end();
      } else {
        this.next();
      }
    });
  }

  private positionTooltip(targetRect: DOMRect, position: string) {
    if (!this.tooltip) return;
    const gap = 16;

    this.tooltip.style.left = "";
    this.tooltip.style.top = "";
    this.tooltip.style.right = "";
    this.tooltip.style.bottom = "";

    switch (position) {
      case "right":
        this.tooltip.style.left = `${targetRect.right + gap}px`;
        this.tooltip.style.top = `${targetRect.top}px`;
        break;
      case "bottom":
        this.tooltip.style.left = `${targetRect.left}px`;
        this.tooltip.style.top = `${targetRect.bottom + gap}px`;
        break;
      case "left":
        this.tooltip.style.left = `${targetRect.left - 340}px`;
        this.tooltip.style.top = `${targetRect.top}px`;
        break;
      case "top":
        this.tooltip.style.left = `${targetRect.left}px`;
        this.tooltip.style.top = `${targetRect.top - gap - 160}px`;
        break;
    }

    // Clamp inside the window so Next/Skip stay reachable at any window size
    const margin = 12;
    const t = this.tooltip.getBoundingClientRect();
    const left = Math.max(margin, Math.min(t.left, window.innerWidth - t.width - margin));
    const top = Math.max(margin, Math.min(t.top, window.innerHeight - t.height - margin));
    this.tooltip.style.left = `${left}px`;
    this.tooltip.style.top = `${top}px`;
  }

  next() {
    this.showStep(this.currentIndex + 1);
  }

  end() {
    localStorage.setItem("tourCompleted", "true");
    this.active = false;
    window.removeEventListener("resize", this.onResize);
    window.removeEventListener("keydown", this.onKeyDown);
    if (this.overlay) {
      this.overlay.remove();
      this.overlay = null;
      this.highlight = null;
      this.tooltip = null;
    }
  }

  getWaitingNav(): string | null {
    return this.waitingForNav;
  }

  clearWaitingNav() {
    this.waitingForNav = null;
  }

  isActive(): boolean {
    return this.active;
  }
}

let onboardingTour: SpotlightTour | null = null;
let postSetupTour: SpotlightTour | null = null;

function startOnboardingTourIfNeeded(isSetupDone: boolean) {
  if (isSetupDone) return;
  if (localStorage.getItem("tourCompleted")) return;

  onboardingTour = new SpotlightTour(TOUR_STEPS);
  onboardingTour.start();
}

function startPostSetupTour() {
  if (localStorage.getItem("tourCompleted")) return;

  postSetupTour = new SpotlightTour(TOUR_POST_SETUP_STEPS);
  postSetupTour.start();
}

// ============================================================================
// Setup Log Viewer
// ============================================================================

function wireLogViewers() {
  document.querySelectorAll<HTMLButtonElement>(".btn-log-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const step = btn.dataset.step;
      if (!step) return;
      const output = document.getElementById(`log-output-${step}`);
      if (!output) return;
      const isCollapsed = output.classList.toggle("collapsed");
      btn.textContent = isCollapsed ? "Show Details" : "Hide Details";
    });
  });

  document.querySelectorAll<HTMLButtonElement>(".btn-open-terminal").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const step = btn.dataset.step;
      if (!step) return;
      try {
        await invoke("open_setup_terminal", { stepName: step });
      } catch (e) {
        console.error("Failed to open terminal:", e);
      }
    });
  });
}

function appendLogLine(step: string, line: string) {
  const logContainer = document.getElementById(`log-${step}`);
  const logOutput = document.getElementById(`log-output-${step}`);
  if (!logContainer || !logOutput) return;

  logContainer.classList.remove("hidden");
  logOutput.textContent += line + "\n";

  // Auto-scroll to bottom
  logOutput.scrollTop = logOutput.scrollHeight;
}

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
      syncSetupNav();

      if (page === "voices") loadVoices();
      if (page === "setup") refreshSetupStatus();
      if (page === "voice-studio") {
        setupVoiceStudioTabs();
        document.getElementById("nav-voice-studio-badge")?.classList.add("hidden");
        try { localStorage.setItem("voicestudio-seen", "1"); } catch (_) { /* storage blocked */ }
      }
      if (page === "narrate") setupNarratePage();

      // Tour: auto-advance when user navigates to the awaited page
      if (onboardingTour && onboardingTour.getWaitingNav() === page) {
        onboardingTour.clearWaitingNav();
        // Small delay so the page renders before spotlighting
        setTimeout(() => {
          const stepTour = new SpotlightTour([TOUR_STEPS[1]]);
          stepTour.start();
        }, 300);
      }
    });
  });
}

// ============================================================================
// Server Status
// ============================================================================

type ServerPhase = "offline" | "loading" | "stopping" | "ready";

/** What the Rust side reports ("server-state" event, get_server_state): the tray and Dashboard share it */
interface ServerStateEvent {
  state: ServerPhase;
  device?: string;
}

let serverPhase: ServerPhase = "offline";
let serverDevice = "";

/** The only place that sets the Dashboard's server status text, dot and Start/Stop button */
function applyServerState(ev: ServerStateEvent) {
  serverPhase = ev.state;
  serverDevice = ev.device ?? "";
  const indicator = document.getElementById("server-indicator");
  const statusEl = document.getElementById("server-status");
  const btn = document.getElementById("btn-server-toggle") as HTMLButtonElement | null;
  const ready = serverPhase === "ready";
  const busy = serverPhase === "loading" || serverPhase === "stopping";

  indicator?.classList.toggle("online", ready);
  indicator?.classList.toggle("offline", !ready);
  if (statusEl) {
    statusEl.textContent = { offline: "Offline", loading: "Loading model…", stopping: "Stopping…", ready: "Running" }[serverPhase];
  }
  if (btn) {
    btn.disabled = busy;
    btn.textContent = { offline: "Start Server", loading: "Starting… (loading model)", stopping: "Stopping…", ready: "Stop Server" }[serverPhase];
    btn.classList.toggle("btn-danger", ready);
    btn.classList.toggle("btn-success", !ready);
  }
  updateServerLogHint(serverPhase === "offline");
  if (ready) {
    checkServerStatus();
  } else {
    for (const id of ["server-model", "server-device", "server-voices"]) {
      const el = document.getElementById(id);
      if (el) el.textContent = "—";
    }
  }
}

/** Wire up the Start/Stop Server button on the dashboard; the new state arrives as a "server-state" event */
function setupServerToggle() {
  const btn = document.getElementById("btn-server-toggle") as HTMLButtonElement | null;
  btn?.addEventListener("click", async () => {
    if (serverPhase === "loading" || serverPhase === "stopping") return;
    const stopping = serverPhase === "ready";
    try {
      await invoke(stopping ? "stop_server" : "start_server");
    } catch (e) {
      console.error("Server toggle failed:", e);
      await showModal(stopping ? "Could not stop the server" : "Could not start the server", errorText(e), true);
    }
  });
}

/** While the server is down, show the tail of server.log so a crash leaves a trace */
async function updateServerLogHint(serverDown: boolean) {
  const el = document.getElementById("server-log-hint");
  if (!el) return;
  try {
    const log = serverDown ? await invoke<{ path: string; lines: string[] }>("get_server_log_tail") : null;
    if (!log || log.lines.length === 0) {
      el.classList.add("hidden");
      return;
    }
    el.textContent = `Last server log lines:\n${log.lines.join("\n")}\n`;
    const btn = document.createElement("button");
    btn.className = "btn btn-secondary btn-sm";
    btn.textContent = "Show log file";
    btn.addEventListener("click", () => revealItemInDir(log.path).catch(console.error));
    el.appendChild(btn);
    el.classList.remove("hidden");
  } catch (e) {
    el.classList.add("hidden");
  }
}

/** Model, device and uptime details while the server is running (the state itself comes from "server-state") */
async function checkServerStatus() {
  if (serverPhase !== "ready") return;
  const modelEl = document.getElementById("server-model");
  const deviceEl = document.getElementById("server-device");
  const voicesEl = document.getElementById("server-voices");

  try {
    const result: ServerStatus = await invoke("get_server_status");
    const health = result.running ? result.health : null;
    if (modelEl) modelEl.textContent = health?.model ?? "—";
    if (deviceEl) deviceEl.textContent = health?.gpu_name ?? (health?.gpu_available ? "GPU" : serverDevice || "CPU");
    if (voicesEl) voicesEl.textContent = health ? formatUptime(health.uptime_seconds) : "—";
    // The server may have started after the first voice load
    if (health && !voicesLoaded && setupComplete) loadVoices();
  } catch (e) {
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
  let gotEvent = false;
  listen<ServerStateEvent>("server-state", (e) => {
    gotEvent = true;
    applyServerState(e.payload);
  });
  invoke<ServerStateEvent>("get_server_state")
    .then((ev) => { if (!gotEvent) applyServerState(ev); })
    .catch(console.error);
  checkSapiStatus();
  window.setInterval(checkServerStatus, 5000);
}

// ============================================================================
// Voice Management
// ============================================================================

async function loadVoices() {
  const container = document.getElementById("voices-list");
  if (!container) return;

  // Favorites / removed list / default voice live in config + registry, not on the server
  try {
    const [prefs, def] = await Promise.all([
      invoke<{ favorites: string[]; removed: string[] }>("get_voice_prefs"),
      invoke<{ id: string | null; name: string }>("get_default_voice"),
    ]);
    favoriteIds = prefs.favorites;
    removedIds = prefs.removed;
    defaultVoice = def;
  } catch (e) {
    console.error("Load voice prefs failed:", e);
  }

  try {
    const [voices, regIds] = await Promise.all([
      invoke<VoiceInfo[]>("get_voices"),
      invoke<string[]>("get_registered_voice_ids"),
    ]);
    currentVoices = voices;
    registeredVoiceIds = new Set(regIds);
    voicesLoaded = true;
    renderVoices(container);
  } catch (e) {
    container.innerHTML = `<p class="placeholder error">Could not load voices. Is the server running?</p>`;
    console.error("Load voices failed:", e);
  }
  refreshVoiceViews();
}

/** Re-draw everything that shows voices outside the Voice Manager grid */
function refreshVoiceViews() {
  testPicker?.setVoices(currentVoices.filter((v) => registeredVoiceIds.has(v.id)));
  refreshNarrateVoices();
  renderFavorites();
  updateRestoreCard();
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

      const isFav = favoriteIds.includes(v.id);

      return `
    <div class="voice-card card ${isEnabled ? "" : "voice-disabled"}" data-voice-id="${v.id}">
      <div class="voice-header">
        <span class="voice-name">${escapeHtml(displayVoiceName(v.name))}</span>
        <div class="voice-header-right">
          <button class="voice-star ${isFav ? "on" : ""}" data-id="${v.id}" aria-pressed="${isFav}" aria-label="${isFav ? "Remove from favorites" : "Add to favorites"}" title="${isFav ? "Remove from favorites" : "Add to favorites"}">${isFav ? "★" : "☆"}</button>
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
        <button class="btn btn-sm btn-secondary btn-delete" data-id="${v.id}">Delete</button>
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

  container.querySelectorAll<HTMLButtonElement>(".btn-delete").forEach((btn) => {
    btn.addEventListener("click", () => handleDeleteVoice(btn.dataset.id!));
  });

  container.querySelectorAll<HTMLButtonElement>(".voice-star").forEach((btn) => {
    btn.addEventListener("click", () => handleToggleFavorite(btn.dataset.id!));
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
    refreshVoiceViews();
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
    await showModal("Error", `Toggle failed: ${errorText(e)}`, true);
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
  let pcmBytes: number[];
  if (voiceId.startsWith("qwen3_")) {
    pcmBytes = await invoke("qwen3_preview_voice", { voiceId, text });
  } else {
    pcmBytes = await invoke("preview_voice", { voiceId, text });
  }

  playPcmAudio(new Uint8Array(pcmBytes));
}

async function handleDeleteVoice(voiceId: string) {
  const voice = currentVoices.find((v) => v.id === voiceId);
  if (!voice) return;
  const name = displayVoiceName(voice.name);
  const isClone = voiceId.startsWith("qwen3_custom_");

  const ok = await showModal(
    `Delete ${name}?`,
    isClone
      ? `${name} will be deleted for good, including its reference audio. This can't be undone.`
      : `${name} will be removed from VoiceLink and from every app (Edge, Balabolka...). You can restore it later in Settings.`,
    false,
    "Delete voice"
  );
  if (ok === null) return;

  try {
    // Unregisters from SAPI first (UAC prompt if needed); nothing is lost if that is cancelled
    await invoke("delete_voice", { voiceId });
    if (isClone) await invoke("qwen3_delete_clone", { voiceId });
  } catch (e) {
    await showModal("Error", `Delete failed: ${e}`, true);
  }
  await loadVoices();
  checkSapiStatus();
}

async function handleToggleFavorite(voiceId: string) {
  const favorite = !favoriteIds.includes(voiceId);
  try {
    await invoke("set_favorite_voice", { voiceId, favorite });
    favoriteIds = favorite ? [...favoriteIds, voiceId] : favoriteIds.filter((id) => id !== voiceId);
    const container = document.getElementById("voices-list");
    if (container) renderVoices(container);
    refreshVoiceViews();
  } catch (e) {
    await showModal("Error", `Could not update favorites: ${e}`, true);
  }
}

// ============================================================================
// Favorite voices (Dashboard)
// ============================================================================

function renderFavorites() {
  const box = document.getElementById("favorite-chips");
  const line = document.getElementById("default-voice-line");
  if (!box) return;

  const favs = currentVoices.filter((v) => favoriteIds.includes(v.id));
  box.innerHTML = favs.length
    ? favs
        .map((v) => {
          const name = escapeHtml(displayVoiceName(v.name));
          const on = registeredVoiceIds.has(v.id);
          return `<div class="fav-chip ${v.id === defaultVoice.id ? "is-default" : ""} ${on ? "" : "is-off"}">
            <button class="fav-name" data-id="${v.id}" title="${on ? `Make ${name} the Windows default voice` : "Turn this voice on in Voice Manager first"}">★ ${name}</button>
            <button class="fav-play" data-id="${v.id}" aria-label="Play a sample of ${name}" title="Play a sample">▶</button>
          </div>`;
        })
        .join("")
    : `<span class="fav-empty">${currentVoices.length ? "Star voices in Voice Manager to pin them here." : "Voices appear here once the server is running."}</span>`;

  if (line) line.textContent = `Default: ${defaultVoice.name ? displayVoiceName(defaultVoice.name) : "Windows default"}`;

  box.querySelectorAll<HTMLButtonElement>(".fav-name").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await invoke("set_default_voice", { voiceId: btn.dataset.id });
        defaultVoice = await invoke("get_default_voice");
        renderFavorites();
      } catch (e) {
        await showModal("Error", `${e}`, true);
      }
    });
  });
  box.querySelectorAll<HTMLButtonElement>(".fav-play").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await playVoicePreview(btn.dataset.id!, "Hello! This is a sample of my voice.");
      } catch (e) {
        await showModal("Error", `Preview failed: ${e}`, true);
      } finally {
        btn.disabled = false;
      }
    });
  });
}

// ============================================================================
// Restore removed voices (Settings)
// ============================================================================

/** "af_heart" -> "Heart", "qwen3_ono_anna" -> "Ono Anna" */
function removedVoiceName(id: string): string {
  const words = id.startsWith("qwen3_") ? id.slice(6).split("_") : [id.split("_").pop() ?? id];
  return words.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
}

function updateRestoreCard() {
  const list = document.getElementById("restore-voices-list");
  if (!list) return;
  if (removedIds.length === 0) {
    list.innerHTML = `<div class="path-hint">No removed voices</div>`;
    return;
  }
  list.innerHTML =
    removedIds
      .map(
        (id) => `<div class="setting-row">
          <label>${escapeHtml(removedVoiceName(id))} <span class="voice-tag">${id.startsWith("qwen3_") ? "Qwen3" : "Kokoro"}</span></label>
          <button class="btn btn-secondary btn-sm" data-restore="${escapeHtml(id)}">Restore</button>
        </div>`
      )
      .join("") +
    (removedIds.length > 1
      ? `<div class="setting-row"><label>All removed voices</label><button class="btn btn-secondary btn-sm" data-restore-all>Restore all</button></div>`
      : "");
}

async function handleRestoreVoices(ids: string[]) {
  const status = document.getElementById("restore-voices-status");
  const buttons = document.querySelectorAll<HTMLButtonElement>("#restore-voices-list button");
  if (!status || ids.length === 0) return;

  const say = (msg: string) => {
    status.textContent = msg;
    status.classList.remove("hidden");
  };
  buttons.forEach((b) => (b.disabled = true));
  try {
    // Kokoro voicepacks come back from Hugging Face; built-in Qwen3 voices have no file to fetch
    const kokoro = ids.filter((id) => !id.startsWith("qwen3_"));
    if (kokoro.length) {
      say("Downloading voice files...");
      const paths = await invoke<SetupPaths>("get_setup_paths");
      const script = `from huggingface_hub import hf_hub_download
for v in ${JSON.stringify(kokoro)}:
    print('Downloading ' + v + '...', flush=True)
    hf_hub_download('hexgrad/Kokoro-82M', 'voices/' + v + '.pt')
`;
      await invoke("setup_run_command", {
        program: paths.python_exe,
        args: ["-c", script],
        stepName: "voice-restore",
        env: { VOICELINK_DATA_DIR: paths.data_dir },
      });
    }
    await invoke("unremove_voices", { voiceIds: ids });
    removedIds = removedIds.filter((id) => !ids.includes(id));

    say("Registering voices (you may see a permission prompt)...");
    let failed = 0;
    for (const id of ids) {
      try {
        await invoke("toggle_voice", { voiceId: id, enabled: true });
      } catch (_) {
        failed++;
      }
    }
    say(failed ? `Restored ${ids.length} voices. ${failed} could not be turned on; switch them on in Voice Manager.` : `Restored ${ids.length} voice${ids.length === 1 ? "" : "s"}.`);
    await loadVoices();
    checkSapiStatus();
  } catch (e) {
    say(`Restore failed: ${String(e).split("stderr:").pop()!.trim()}`);
  }
  updateRestoreCard();
}

// ============================================================================
// Voice picker: one dropdown used by Quick Test and Narrate
// ============================================================================

interface VoicePicker {
  /** `preferred` is picked when the current choice isn't in the list (else the first voice) */
  setVoices(voices: VoiceInfo[], preferred?: string): void;
  readonly value: string;
}

/**
 * Custom combobox (focus stays on the button, arrow keys move the highlighted
 * option via aria-activedescendant). Listeners are added once here; setVoices
 * only re-draws the list contents.
 */
function createVoicePicker(host: HTMLElement, label: string, onChange?: (id: string) => void): VoicePicker {
  const uid = host.id;
  host.innerHTML = `
    <button type="button" class="vp-trigger" role="combobox" aria-haspopup="listbox" aria-expanded="false" aria-controls="${uid}-list" aria-label="${label}">
      <span class="vp-current"></span>
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>
    </button>
    <div id="${uid}-list" class="vp-list hidden" role="listbox" aria-label="${label}"></div>`;
  const trigger = host.querySelector<HTMLButtonElement>(".vp-trigger")!;
  const current = host.querySelector<HTMLElement>(".vp-current")!;
  const list = host.querySelector<HTMLElement>(".vp-list")!;

  let voices: VoiceInfo[] = [];
  let flat: VoiceInfo[] = []; // options in display order (a favorite appears twice)
  let value = "";
  let active = -1;

  const isOpen = () => !list.classList.contains("hidden");

  function renderList() {
    flat = [];
    const sections: [string, VoiceInfo[]][] = [
      ["★ Favorites", voices.filter((v) => favoriteIds.includes(v.id))],
      ["Kokoro", voices.filter((v) => v.model !== "qwen3")],
      ["Qwen3", voices.filter((v) => v.model === "qwen3")],
    ];
    list.innerHTML =
      sections
        .filter(([, vs]) => vs.length)
        .map(([title, vs], g) => `
          <div role="group" aria-labelledby="${uid}-g${g}">
            <div class="vp-group-label" id="${uid}-g${g}">${title}</div>
            ${vs.map((v) => {
              flat.push(v);
              const i = flat.length - 1;
              return `<div class="vp-option" role="option" id="${uid}-o${i}" data-i="${i}" aria-selected="${v.id === value}">
                <span class="vp-name">${escapeHtml(displayVoiceName(v.name))}</span>
                <span class="voice-tag">${escapeHtml(v.language)}</span>
                <span class="voice-badge ${v.gender.toLowerCase()}">${escapeHtml(v.gender)}</span>
                ${v.tags.includes("qwen3-cloned") ? `<span class="badge badge-warning">cloned</span>` : ""}
              </div>`;
            }).join("")}
          </div>`)
        .join("") || `<div class="vp-empty">No voices available</div>`;
  }

  function setActive(i: number) {
    if (!flat.length) return;
    active = Math.max(0, Math.min(flat.length - 1, i));
    list.querySelectorAll(".vp-option.active").forEach((o) => o.classList.remove("active"));
    const opt = list.querySelector<HTMLElement>(`#${uid}-o${active}`);
    opt?.classList.add("active");
    opt?.scrollIntoView({ block: "nearest" });
    trigger.setAttribute("aria-activedescendant", `${uid}-o${active}`);
  }

  function open() {
    if (isOpen()) return;
    list.classList.remove("hidden");
    trigger.setAttribute("aria-expanded", "true");
    setActive(Math.max(0, flat.findIndex((v) => v.id === value)));
  }

  function close() {
    list.classList.add("hidden");
    trigger.setAttribute("aria-expanded", "false");
    trigger.removeAttribute("aria-activedescendant");
  }

  function choose(i: number) {
    if (flat[i]) {
      value = flat[i].id;
      current.textContent = displayVoiceName(flat[i].name);
      renderList();
      onChange?.(value);
    }
    close();
  }

  trigger.addEventListener("click", () => (isOpen() ? close() : open()));
  trigger.addEventListener("keydown", (e) => {
    if (!isOpen()) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        open();
      }
      return;
    }
    switch (e.key) {
      case "ArrowDown": setActive(active + 1); break;
      case "ArrowUp": setActive(active - 1); break;
      case "Home": setActive(0); break;
      case "End": setActive(flat.length - 1); break;
      case "Enter":
      case " ": choose(active); break;
      case "Escape": close(); break;
      default: return;
    }
    e.preventDefault();
    e.stopPropagation();
  });
  // Keep focus on the button while clicking options
  list.addEventListener("mousedown", (e) => e.preventDefault());
  list.addEventListener("click", (e) => {
    const opt = (e.target as HTMLElement).closest<HTMLElement>(".vp-option");
    if (opt) choose(Number(opt.dataset.i));
  });
  list.addEventListener("mouseover", (e) => {
    const opt = (e.target as HTMLElement).closest<HTMLElement>(".vp-option");
    if (opt) setActive(Number(opt.dataset.i));
  });
  // Outside click / Tab away closes (one document listener per picker, added here only)
  document.addEventListener("pointerdown", (e) => {
    if (!host.contains(e.target as Node)) close();
  });
  host.addEventListener("focusout", (e) => {
    if (!host.contains(e.relatedTarget as Node)) close();
  });

  return {
    setVoices(vs, preferred) {
      voices = vs;
      if (!vs.some((v) => v.id === value)) value = (vs.some((v) => v.id === preferred) ? preferred : vs[0]?.id) ?? "";
      current.textContent = vs.length ? displayVoiceName(vs.find((v) => v.id === value)!.name) : "No voices";
      renderList();
      if (isOpen()) setActive(active);
      onChange?.(value);
    },
    get value() {
      return value;
    },
  };
}

// ============================================================================
// Quick Test (Voice Manager)
// ============================================================================

function setupQuickTest() {
  const testHost = document.getElementById("test-voice");
  if (testHost) testPicker = createVoicePicker(testHost, "Voice");
  const narrateHost = document.getElementById("narrate-voice");
  if (narrateHost) narratePicker = createVoicePicker(narrateHost, "Voice", syncNarrateLanguage);

  document.getElementById("btn-manage-voices")?.addEventListener("click", () => {
    document.querySelector<HTMLElement>('.nav-item[data-page="voices"]')?.click();
  });
  document.getElementById("restore-voices-list")?.addEventListener("click", (e) => {
    const btn = (e.target as HTMLElement).closest("button");
    if (!btn || btn.disabled) return;
    handleRestoreVoices(btn.hasAttribute("data-restore-all") ? [...removedIds] : [btn.dataset.restore!]);
  });

  const btn = document.getElementById("btn-preview");
  btn?.addEventListener("click", async () => {
    const textEl = document.getElementById("test-text") as HTMLTextAreaElement;
    if (!textEl || !testPicker) return;

    const text = textEl.value.trim();
    const voiceId = testPicker.value;
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

// confirmLabel turns it into a Cancel / <confirmLabel> question: resolves "" on confirm, null on cancel
function showModal(title: string, defaultValue: string, alertOnly = false, confirmLabel?: string): Promise<string | null> {
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

    const messageOnly = alertOnly || !!confirmLabel;
    okBtn.classList.toggle("btn-danger", !!confirmLabel);
    okBtn.classList.toggle("btn-primary", !confirmLabel);

    if (messageOnly) {
      input.style.display = "none";
      msgEl.style.display = "";
      msgEl.textContent = defaultValue;
      okBtn.textContent = confirmLabel ?? "OK";
      cancelBtn.style.display = confirmLabel ? "" : "none";
      setTimeout(() => (confirmLabel ? cancelBtn : okBtn).focus(), 50);
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
      overlay.removeEventListener("keydown", onKey);
    }

    function onOk() {
      cleanup();
      resolve(messageOnly ? "" : input.value.trim());
    }

    function onCancel() {
      cleanup();
      resolve(null);
    }

    // On the overlay so Escape also works when a button has focus; Enter on a
    // focused button is that button's own click
    function onKey(e: KeyboardEvent) {
      if (e.key === "Enter" && e.target === input) onOk();
      if (e.key === "Escape") onCancel();
    }

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    overlay.addEventListener("keydown", onKey);
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
let setupPaused = false;
let stepStartTime: number | null = null;
let elapsedTimerId: number | null = null;

function formatElapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}

/** 652 MB, 13.3 GB; one decimal below 100 MB so small files still show progress */
function formatBytes(bytes: number): string {
  const mb = bytes / 1024 / 1024;
  if (mb < 1) return `${Math.round(bytes / 1024)} KB`;
  if (mb < 100) return `${mb.toFixed(1)} MB`;
  if (mb < 1024) return `${Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
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

/** Tell the tray (tooltip, menu, taskbar, notifications) how setup is going */
function trayReport(phase: "step" | "progress" | "paused" | "failed" | "done", step?: StepName, percent?: number, message?: string) {
  invoke("set_setup_tray", { phase, step, percent: percent === undefined ? undefined : Math.round(percent), message }).catch(() => {});
}

function setStepIcon(step: StepName, state: "pending" | "running" | "done" | "error") {
  if (state === "running") trayReport("step", step, 0);
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
  trayReport("progress", step, percent);
  const fill = document.getElementById(`fill-${step}`) as HTMLElement;
  const txt = document.getElementById(`text-${step}`);
  if (fill) fill.style.width = `${Math.min(100, percent)}%`;
  if (txt && text) txt.textContent = text;
  else if (txt) txt.textContent = `${percent}%`;
}

function setStepText(step: StepName, text: string) {
  const txt = document.getElementById(`text-${step}`);
  if (txt) txt.textContent = text;
}

// pip pauses and retries on its own; these flags make it ride out a flaky connection
const PIP_NET_ARGS = ["--retries", "10", "--timeout", "30", "--resume-retries", "10"];
// Failures worth waiting out. Anything else (bad package, disk full) fails right away.
const NETWORK_ERROR = /connect|timed out|timeout|resolve|getaddrinfo|network|retries exceeded|ssl|reset|aborted|remote end|unreachable|offline/i;

/**
 * setup_run_command that retries dropped connections (8 tries, 5 to 60 s apart).
 * `ui` says where the wait is shown and what pauses it; it defaults to the setup page.
 */
async function runNetworkCommand(
  step: string,
  args: Record<string, unknown>,
  ui: { paused: () => boolean; say: (text: string) => void } = {
    paused: () => setupPaused,
    say: (text) => setStepText(step as StepName, text),
  },
): Promise<string> {
  const maxAttempts = 8;
  for (let attempt = 1; ; attempt++) {
    if (ui.paused()) throw "Paused";
    try {
      return await invoke<string>("setup_run_command", { ...args, stepName: step });
    } catch (e) {
      if (ui.paused() || attempt >= maxAttempts || !NETWORK_ERROR.test(String(e).split("stderr:").pop()!)) throw e;
      const wait = Math.min(60, 5 * 2 ** (attempt - 1));
      for (let s = wait; s > 0 && !ui.paused(); s--) {
        ui.say(`Connection problem — retrying in ${s}s (attempt ${attempt + 1}/${maxAttempts})`);
        await new Promise((r) => setTimeout(r, 1000));
      }
    }
  }
}

function setOverallStatus(msg: string, type: "info" | "success" | "error" = "info") {
  const el = document.getElementById("setup-overall-status");
  if (!el) return;
  el.textContent = msg;
  el.className = `setup-status-msg status-${type}`;
}

function setDataDirLocked(locked: boolean) {
  const input = document.getElementById("setup-data-dir") as HTMLInputElement | null;
  if (input) input.readOnly = locked;
  document.getElementById("btn-save-path")?.classList.toggle("hidden", locked);
  const hint = document.getElementById("setup-path-hint");
  if (hint) {
    hint.dataset.editableText ??= hint.textContent ?? "";
    hint.textContent = locked
      ? "Installed here. Moving the install will be available in Settings."
      : hint.dataset.editableText;
  }
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
    // Choosing the folder only makes sense before anything is installed in it
    setDataDirLocked(status.python_installed || setupRunning);

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
        btn.innerHTML = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" /></svg> ${setupPaused ? "Resume Setup" : "Run Setup"}`;
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
  setupPaused = false;
  setDataDirLocked(true);

  const btn = document.getElementById("btn-run-setup") as HTMLButtonElement;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Setting up...";
  }
  const pauseBtn = document.getElementById("btn-pause-setup") as HTMLButtonElement | null;
  pauseBtn?.classList.remove("hidden");
  if (pauseBtn) pauseBtn.disabled = false;

  // Clear text left over from a failed or paused run (e.g. "3.6 / 10.7 MB")
  for (const s of ["python", "deps", "server", "model", "start"] as StepName[]) {
    setStepProgress(s, 0, "Starting...");
  }
  setOverallStatus("Starting setup...");
  const useGpu = (document.getElementById("setup-use-gpu") as HTMLInputElement | null)?.checked ?? true;

  try {
    await invoke("resume_setup");
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

      await runNetworkCommand("python", {
        program: paths.python_exe,
        args: [getPipDest, "--no-warn-script-location"],
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
      await runNetworkCommand("deps", {
        program: paths.python_exe,
        args: [
          "-m", "pip", "install", "--no-warn-script-location", ...PIP_NET_ARGS,
          "fastapi>=0.115.0",
          "uvicorn[standard]>=0.34.0",
          "python-multipart>=0.0.9",
          "pydantic-settings>=2.7.0",
          "pyyaml>=6.0",
          "soundfile>=0.13.0",
          "numpy>=1.26.0,<2.0",
          "loguru>=0.7.0",
        ],
      });

      // pip's default torch on Windows is CPU-only; install the CUDA build first
      // so kokoro reuses it. cu126 still supports older cards (GTX 10xx).
      // Unchecked: kokoro pulls the (much smaller) CPU build instead.
      const gpu = useGpu ? await invoke<GpuInfo>("check_gpu").catch(() => null) : null;
      if (gpu?.available) {
        setStepProgress("deps", 40, "Installing PyTorch for your GPU (large download)...");
        await runNetworkCommand("deps", {
          program: paths.python_exe,
          args: [
            "-m", "pip", "install", "--no-warn-script-location", ...PIP_NET_ARGS,
            "torch", "--index-url", "https://download.pytorch.org/whl/cu126",
          ],
        });
      }

      setStepProgress("deps", 60, "Installing Kokoro...");

      // Install kokoro separately (it's a bigger install)
      await runNetworkCommand("deps", {
        program: paths.python_exe,
        args: ["-m", "pip", "install", "--no-warn-script-location", ...PIP_NET_ARGS, "kokoro==0.9.4"],
      });

      // Only now is the package step really complete (an interrupted install
      // must not count as done)
      await invoke("setup_mark_deps_installed");

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
      await runNetworkCommand("model", {
        program: paths.python_exe,
        args: ["-c", VOICEPACK_DOWNLOAD_SCRIPT],
        env: { VOICELINK_DATA_DIR: paths.data_dir },
      });

      stopElapsedTimer();
      setStepIcon("model", "done");
      showStepProgress("model", false);
    } else {
      setStepIcon("model", "done");
    }

    // Step 5: Start the server (nothing left to pause)
    pauseBtn?.classList.add("hidden");
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
    trayReport("done");

    if (serverReady) {
      setStepIcon("start", "done");
      setOverallStatus("Setup complete! VoiceLink is ready.", "success");
      if (btn) {
        btn.innerHTML = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5" /></svg> All Set!`;
      }
      applySetupIncompleteState(true);
      loadVoices();
      setTimeout(() => startPostSetupTour(), 600);
    } else {
      setStepIcon("start", "error");
      setOverallStatus("Server is still loading. It may take a minute for the model to initialize. Check Dashboard.", "info");
    }
  } catch (e) {
    console.error("Setup failed:", e);
    if (setupPaused) {
      trayReport("paused");
      setOverallStatus("Paused — progress is saved. Click Resume Setup to continue.");
      if (btn) btn.disabled = false;
    } else {
      trayReport("failed", undefined, undefined, String(e));
      setOverallStatus(`Setup failed: ${e}`, "error");
      if (btn) {
        btn.textContent = "Retry Setup";
        btn.disabled = false;
      }
    }
  } finally {
    stopElapsedTimer();
    setupRunning = false;
    pauseBtn?.classList.add("hidden");
    if (setupPaused) refreshSetupStatus(); // running steps go back to pending, button says Resume
    // If setup failed before Python was installed, the folder can be changed again
    invoke<SetupStatus>("get_setup_status").then((s) => setDataDirLocked(s.python_installed)).catch(() => {});
  }
}

function setupSetupWizard() {
  const btn = document.getElementById("btn-run-setup");
  btn?.addEventListener("click", runSetup);

  document.getElementById("btn-pause-setup")?.addEventListener("click", async (e) => {
    setupPaused = true;
    (e.currentTarget as HTMLButtonElement).disabled = true;
    setOverallStatus("Pausing...");
    await invoke("pause_setup").catch(() => {});
  });

  // Offer the 2.3 GB CUDA PyTorch only when there is an NVIDIA GPU to use it
  invoke<GpuInfo>("check_gpu")
    .then((g) => { if (g.available) document.getElementById("setup-gpu-option")?.classList.remove("hidden"); })
    .catch(() => {});

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

  // Tray menu / left click: press the same buttons the user would
  listen<string>("tray-action", (event) => {
    const id = { pause: "btn-pause-setup", resume: "btn-run-setup", "open-setup": "nav-setup-item" }[event.payload];
    if (id) document.getElementById(id)?.click();
  });

  // Listen for progress events from Rust backend
  listen<{ step: string; progress: number; downloaded?: number; total?: number; status?: string; line?: string; retry_in?: number }>(
    "setup-progress",
    (event) => {
      const { step, progress, downloaded, total, line, status, retry_in } = event.payload;
      const stepName = step as StepName;

      if (status === "waiting") {
        setStepText(stepName, `Connection lost — retrying in ${retry_in}s`);
      } else if (downloaded && total && total > 0) {
        setStepProgress(stepName, progress, `${formatBytes(downloaded)} / ${formatBytes(total)}`);
      } else if (line) {
        const shortLine = line.length > 60 ? line.substring(0, 57) + "..." : line;
        setStepProgress(stepName, progress, shortLine);
        appendLogLine(step, line);
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

  // Voice Studio is discoverable on any NVIDIA PC; "NEW" until its page is first opened
  voiceStudioNav?.classList.remove("hidden");
  let seen = false;
  try { seen = localStorage.getItem("voicestudio-seen") === "1"; } catch (_) { /* storage blocked */ }
  document.getElementById("nav-voice-studio-badge")?.classList.toggle("hidden", seen);

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

    refreshQwen3UI(settings);
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

      refreshQwen3UI(await invoke<AppSettings>("get_settings"));
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
      refreshQwen3UI(await invoke<AppSettings>("get_settings"));
    } catch (e) {
      console.error("Failed to save tier:", e);
    }
  });

  // One button: Download / Pause / Resume (Settings and the Voice Studio intro card share it)
  async function onQwen3ButtonClick(fromStudio: boolean) {
    if (qwen3Downloading) {
      qwen3DownloadPaused = true;
      qwen3Els("btn-download-qwen3", "btn-studio-install").forEach((b) => {
        b.disabled = true;
        b.textContent = "Pausing...";
      });
      try { await invoke("cancel_qwen3_download"); } catch (_) { /* may already be done */ }
      return;
    }
    qwen3DownloadPaused = false;
    if (fromStudio) {
      // Settings only offers the download once Qwen3 is enabled, so enable it here too
      try {
        await invoke("save_settings", { qwen3Enabled: true });
      } catch (e) {
        console.error("Failed to enable Qwen3:", e);
        return;
      }
      if (qwen3Toggle) qwen3Toggle.checked = true;
      detailsSection?.classList.remove("hidden");
      const s = await invoke<AppSettings>("get_settings");
      refreshQwen3UI(s);
      if (s.qwen3_installed) return;
    }
    await downloadQwen3Models();
  }
  downloadBtn?.addEventListener("click", () => onQwen3ButtonClick(false));
  document.getElementById("btn-studio-install")?.addEventListener("click", () => onQwen3ButtonClick(true));
  document.getElementById("btn-design-switch")?.addEventListener("click", () => switchToFullTier());
  document.getElementById("btn-remove-qwen3")?.addEventListener("click", () => removeQwen3());
}

/** Bring every Qwen3 control in line with the saved settings (Settings card, Voice Studio, nav, Remove row) */
function refreshQwen3UI(settings: AppSettings) {
  updateQwen3StatusBadge(document.getElementById("qwen3-status-badge"), settings);
  updateQwen3DownloadButton(document.getElementById("btn-download-qwen3") as HTMLButtonElement | null, settings);
  updateStudioView(settings);
  document.getElementById("qwen3-remove-row")?.classList.toggle("hidden", !settings.qwen3_remnants);
  const tierSelect = document.getElementById("setting-qwen3-tier") as HTMLSelectElement | null;
  if (tierSelect) tierSelect.value = settings.qwen3_model_tier;
}

/** Elements that exist twice (Settings card + Voice Studio intro card) and must stay in sync */
function qwen3Els(...ids: string[]): HTMLButtonElement[] { // typed as buttons; also used for progress divs/spans
  return ids.map((id) => document.getElementById(id) as HTMLButtonElement | null).filter((e): e is HTMLButtonElement => !!e);
}

/** Voice Studio: tools once Qwen3 is enabled + installed, otherwise the install intro card */
function updateStudioView(settings: AppSettings) {
  const ready = settings.qwen3_enabled && settings.qwen3_installed;
  document.getElementById("studio-intro")?.classList.toggle("hidden", ready);
  document.getElementById("studio-tools")?.classList.toggle("hidden", !ready);
  if (ready) {
    updateDesignPanel(settings);
    return;
  }

  const needs = document.getElementById("studio-intro-needs");
  if (needs) {
    needs.textContent = settings.qwen3_models_installed
      // models from an earlier install are kept; only the Python engine is missing
      ? "Qwen3's models are already downloaded. It needs a one-time engine install (~0.5 GB)."
      : settings.qwen3_model_tier === "full"
        ? "Needs Qwen3 · ~13.3 GB download · ~5 GB VRAM"
        : "Needs Qwen3 · ~5.3 GB download · ~2 GB VRAM";
  }

  // A download in progress or paused owns the button label
  const btn = document.getElementById("btn-studio-install") as HTMLButtonElement | null;
  if (btn && !qwen3Downloading && !qwen3DownloadPaused) {
    const noVram = !cachedGpuInfo?.can_run_standard;
    btn.disabled = noVram;
    btn.textContent = noVram ? "Not enough free VRAM" : "Install Qwen3";
  }
}

/** Design a Voice: the form on the Full tier, otherwise an explanation with a "Switch to Full tier" button */
function updateDesignPanel(settings: AppSettings) {
  const canDesign = settings.qwen3_model_tier === "full" && settings.qwen3_models_installed;
  document.getElementById("design-locked")?.classList.toggle("hidden", canDesign);
  document.getElementById("design-ready")?.classList.toggle("hidden", !canDesign);
  if (canDesign) return;

  const why = document.getElementById("design-locked-why");
  if (why) {
    why.textContent = settings.qwen3_model_tier === "full"
      ? "The Full (1.7B) models are not downloaded yet."
      : "You are on the Standard (0.6B) tier. It clones voices and has the built-in voices, but voice design needs the larger Full (1.7B) models. Your cloned voices keep working after the switch.";
  }
  const note = document.getElementById("design-locked-note");
  if (note) {
    const vram = cachedGpuInfo?.can_run_full === false
      ? ` Your GPU has ${(cachedGpuInfo.vram_free_mb ?? 0).toLocaleString()} MB free right now; Full needs about 5 GB, so close other GPU programs first.`
      : "";
    note.textContent = (settings.qwen3_full_models_installed
      ? "The Full models are already downloaded, so switching is instant."
      : "Switching downloads about 13.3 GB (the Standard models stay installed) and needs about 5 GB of free GPU memory.") + vram;
  }
  const btn = document.getElementById("btn-design-switch") as HTMLButtonElement | null;
  if (btn && !qwen3Downloading && !qwen3DownloadPaused) {
    btn.disabled = false;
    btn.textContent = "Switch to Full tier";
  }
}

interface RemoveInfo { models_bytes: number; package_bytes: number; voices_bytes: number; voice_count: number }

/** The "Remove Qwen3?" dialog. Resolves null on Cancel, else whether the user's own voices go too. */
function askRemoveQwen3(info: RemoveInfo): Promise<{ deleteVoices: boolean } | null> {
  return new Promise((resolve) => {
    const overlay = document.getElementById("qwen3-remove-overlay")!;
    const ok = document.getElementById("qwen3-remove-ok")!;
    const cancel = document.getElementById("qwen3-remove-cancel")!;
    const box = document.getElementById("qwen3-remove-voices") as HTMLInputElement;
    const freed = info.models_bytes + info.package_bytes;

    document.getElementById("qwen3-remove-body")!.innerHTML =
      `<strong>This frees about ${formatBytes(freed)}</strong> (models ${formatBytes(info.models_bytes)}, engine ${formatBytes(info.package_bytes)}). `
      + "Qwen3's voices disappear from Windows apps such as Edge and Balabolka, because they cannot speak without it. "
      + "Windows asks for administrator permission once.";
    document.getElementById("qwen3-remove-voices-row")!.classList.toggle("hidden", info.voice_count === 0);
    document.getElementById("qwen3-remove-voices-text")!.textContent =
      `Also delete my ${info.voice_count} cloned and designed voice${info.voice_count === 1 ? "" : "s"} (${formatBytes(info.voices_bytes)}). `
      + "Leave this unticked to keep them: installing Qwen3 again brings them back.";
    box.checked = false;

    const done = (value: { deleteVoices: boolean } | null) => {
      overlay.classList.add("hidden");
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      overlay.removeEventListener("keydown", onKey);
      resolve(value);
    };
    const onOk = () => done({ deleteVoices: box.checked });
    const onCancel = () => done(null);
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onCancel(); };
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    overlay.addEventListener("keydown", onKey);
    overlay.classList.remove("hidden");
    setTimeout(() => cancel.focus(), 50); // the safe choice has the focus
  });
}

let qwen3Removing = false;

/**
 * Remove Qwen3 completely. Order matters: Windows voices first (one permission prompt; if
 * declined nothing has changed), then stop the server (it holds the engine's files open and
 * VRAM), then delete files and uninstall packages, then restart the server for Kokoro.
 * Every step can be repeated, so an interrupted removal is finished by pressing the button again.
 */
async function removeQwen3() {
  if (qwen3Removing) return;
  if (qwen3Downloading) {
    await showModal("Qwen3 is installing", "Pause the download, or wait for it to finish, before removing Qwen3.", true);
    return;
  }
  const status = document.getElementById("qwen3-remove-status")!;
  const btn = document.getElementById("btn-remove-qwen3") as HTMLButtonElement;
  const say = (text: string, color = "") => { status.textContent = text; status.style.color = color; status.classList.remove("hidden"); };

  let info: RemoveInfo;
  try { info = await invoke<RemoveInfo>("qwen3_remove_info"); } catch (e) { say(`Could not check Qwen3's size: ${errorText(e)}`, "var(--danger)"); return; }
  const choice = await askRemoveQwen3(info);
  if (!choice) return;

  qwen3Removing = true;
  btn.disabled = true;
  let serverWasRunning = false;
  try {
    say("Removing Qwen3's voices from Windows... approve the permission prompt.");
    try {
      await invoke("qwen3_remove_windows_voices");
    } catch (e) {
      say(`Qwen3 was not removed: ${errorText(e)} Nothing was changed.`, "var(--danger)");
      return;
    }

    say("Stopping the server...");
    serverWasRunning = await invoke<{ running: boolean }>("get_server_status").then((s) => s.running, () => false);
    if (serverWasRunning) await invoke("stop_server");

    say("Deleting the Qwen3 engine and models...");
    const freed = await invoke<number>("qwen3_remove_files", { deleteVoices: choice.deleteVoices });
    await invoke("save_settings", { qwen3Enabled: false, qwen3Installed: false });
    const toggle = document.getElementById("setting-qwen3-enabled") as HTMLInputElement | null;
    if (toggle) toggle.checked = false;
    document.getElementById("qwen3-details")?.classList.add("hidden");
    say(`Qwen3 removed. Freed ${formatBytes(freed)}.`, "var(--success)");
  } catch (e) {
    say(`Qwen3 was only partly removed: ${errorText(e)} Its voices are already gone from Windows. Press Remove Qwen3 again to finish.`, "var(--danger)");
  } finally {
    if (serverWasRunning) invoke("start_server").catch((e) => console.error("Restarting the server failed:", e));
    qwen3Removing = false;
    btn.disabled = false;
    refreshQwen3UI(await invoke<AppSettings>("get_settings"));
    loadVoices();
  }
}

/** Design tab button: use the Full tier, downloading it first when needed */
async function switchToFullTier() {
  if (qwen3Downloading) {
    // same button pauses a running download
    qwen3DownloadPaused = true;
    try { await invoke("cancel_qwen3_download"); } catch (_) { /* may already be done */ }
    return;
  }
  qwen3DownloadPaused = false;
  try {
    const s = await invoke<AppSettings>("get_settings");
    if (s.qwen3_full_models_installed) {
      await invoke("save_settings", { qwen3ModelTier: "full" });
      refreshQwen3UI(await invoke<AppSettings>("get_settings"));
      return;
    }
    // The tier only changes once the Full models are complete, so Standard keeps working meanwhile
    await downloadQwen3Models("full");
  } catch (e) {
    await showModal("Could not switch tier", errorText(e), true);
  }
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
    btn.textContent = "Installed";
    btn.disabled = true;
    btn.classList.remove("btn-primary", "btn-warning");
    btn.classList.add("btn-secondary");
  } else {
    btn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg> Install Qwen3`;
    btn.disabled = false;
    btn.classList.add("btn-primary");
    btn.classList.remove("btn-secondary");
  }
}

// ---- Qwen3 download progress card: one implementation for Settings and the Voice Studio intro ----

const Q3_STEPS: Record<string, string[]> = {
  standard: ["Engine", "Tokenizer", "Base", "CustomVoice"],
  full: ["Engine", "Tokenizer", "Base", "CustomVoice", "VoiceDesign"],
};

// What faster-qwen3-tts needs at runtime. Bump QWEN3_RUNTIME_VERSION in lib.rs when this changes.
// faster-qwen3-tts and qwen-tts-hf go in with --no-deps: their own dependency lists pull in
// gradio (a demo UI with ~60 packages) and let pip swap the PyTorch build. These are the imports
// they really need.
const QWEN3_RUNTIME_DEPS = [
  // 5.15.1 is what faster-qwen3-tts was verified with; newer 5.x (e.g. 5.23) crashes
  // qwen-tts's speech encoder ("property 'upsample' has no setter")
  "transformers==5.15.1",
  "huggingface-hub>=1.5.0,<2.0",
  "accelerate==1.12.0",
  "librosa==0.11.0",
  "torchaudio==2.11.1",
  "onnxruntime==1.32.0",
  "sox==1.5.0",
  "einops==0.8.2",
];
const QWEN3_RUNTIME_PACKAGES = ["faster-qwen3-tts==0.5.4", "qwen-tts-hf==0.1.1.post1"];
const QWEN3_IMPORT_CHECK = "import faster_qwen3_tts, qwen_tts, torchaudio, librosa, onnxruntime, sox, einops, accelerate, torch, sys; "
  + "sys.exit(0 if torch.cuda.is_available() else 'NO_CUDA')";

const q3 = {
  state: "downloading" as "downloading" | "paused" | "done" | "error",
  steps: Q3_STEPS.standard,
  done: 0, // models finished
  pct: 0,
  text: "",
  totalMb: 0,
  samples: [] as { t: number; mb: number }[], // recent (time, MB downloaded) points for speed and ETA
};
let q3Timer: number | undefined;

function qwen3ProgressBoxes(): HTMLElement[] {
  return ["progress-qwen3", "progress-qwen3-studio"]
    .map((id) => document.getElementById(id))
    .filter((e): e is HTMLElement => !!e);
}

/** Reset the card and show it (call when a download starts or resumes) */
function startQwen3Progress(tier: string) {
  Object.assign(q3, { state: "downloading", steps: Q3_STEPS[tier] ?? Q3_STEPS.standard, done: 0, pct: 0, text: "", totalMb: 0, samples: [] });
  const markup = `
    <div class="q3-head">
      <div class="q3-eq" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div>
      <span class="q3-state"></span>
      <span class="q3-meta"></span>
    </div>
    <ol class="q3-steps">${q3.steps.map((n) => `<li>${n}</li>`).join("")}</ol>
    <div class="q3-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><div class="q3-fill"></div></div>
    <div class="q3-text"></div>`;
  qwen3ProgressBoxes().forEach((b) => {
    b.innerHTML = markup;
    b.classList.add("q3");
    b.classList.remove("hidden");
  });
  window.clearInterval(q3Timer);
  q3Timer = window.setInterval(renderQwen3Progress, 1000); // keeps the ETA counting down between updates
  renderQwen3Progress();
}

function stopQwen3Progress(state: typeof q3.state) {
  q3.state = state;
  window.clearInterval(q3Timer);
  renderQwen3Progress();
}

/** Record a progress point; the speed is measured over about the last 5 s */
function addQwen3Sample(mb: number, totalMb: number) {
  const t = Date.now();
  q3.totalMb = totalMb;
  q3.samples.push({ t, mb });
  // The progress script reports per file, so keep the newest point that is at least 5 s old as the baseline
  while (q3.samples.length > 2 && q3.samples[1].t <= t - 5000) q3.samples.shift();
}

/** "18.4 MB/s · about 4 min left", or "" until there are two points to compare */
function qwen3SpeedText(): string {
  const [first, last] = [q3.samples[0], q3.samples[q3.samples.length - 1]];
  if (!first || first === last) return "";
  const secs = (last.t - first.t) / 1000;
  if (secs < 1 || last.mb <= first.mb) return "";
  const speed = (last.mb - first.mb) / secs;
  const left = (q3.totalMb - last.mb) / speed - (Date.now() - last.t) / 1000;
  const mins = Math.round(left / 60);
  const eta = left < 20 ? "almost there" : left < 90 ? "about 1 min left"
    : mins < 60 ? `about ${mins} min left` : `about ${Math.floor(mins / 60)} h ${mins % 60} min left`;
  return `${speed.toFixed(1)} MB/s · ${eta}`;
}

function renderQwen3Progress() {
  const label = { downloading: "Downloading", paused: "Paused", done: "Voice Studio is ready", error: "Download stopped" }[q3.state];
  const meta = q3.state === "downloading" ? qwen3SpeedText() : "";
  for (const box of qwen3ProgressBoxes()) {
    box.dataset.state = q3.state;
    box.querySelector(".q3-state")!.textContent = label;
    box.querySelector(".q3-meta")!.textContent = meta;
    box.querySelector(".q3-text")!.textContent = q3.text;
    const bar = box.querySelector<HTMLElement>(".q3-bar")!;
    bar.setAttribute("aria-valuenow", String(q3.pct));
    box.querySelector<HTMLElement>(".q3-fill")!.style.width = `${q3.pct}%`;
    box.querySelectorAll("li").forEach((li, i) => {
      li.classList.toggle("done", q3.state === "done" || i < q3.done);
      li.classList.toggle("current", q3.state !== "done" && i === q3.done);
    });
  }
}

/** Percent of the bar the engine install takes when it has to run; the model download fills the rest */
let q3PctBase = 0;

/**
 * Install Qwen3: the Python engine (if missing), then the models of `tierOverride` or the
 * saved tier. A finished install is what makes Settings say "Ready"; the app derives that
 * from the files on disk, so an interrupted run is simply picked up again.
 */
async function downloadQwen3Models(tierOverride?: string) {
  // Settings card, Voice Studio intro card and the Design tab show the same state
  const btns = qwen3Els("btn-download-qwen3", "btn-studio-install", "btn-design-switch");
  const setText = (t: string) => { q3.text = t; renderQwen3Progress(); };
  const setFill = (pct: number) => { q3.pct = pct; renderQwen3Progress(); };
  q3PctBase = 0;
  const setBtns = (label: string, warn: boolean) => btns.forEach((b) => {
    b.disabled = false;
    b.textContent = label;
    b.classList.toggle("btn-warning", warn);
    b.classList.toggle("btn-primary", !warn);
  });

  // Show downloading state; the same button now pauses (HF keeps partial files)
  qwen3Downloading = true;
  setBtns("Pause Download", true);
  const stepTier = tierOverride ?? await invoke<AppSettings>("get_settings").then((s) => s.qwen3_model_tier).catch(() => "standard");
  startQwen3Progress(stepTier);

  // Listen for real-time progress from the download script
  const unlisten = await listen<{ step: string; line?: string }>("setup-progress", (event) => {
    const { step, line } = event.payload;
    if (!line) return;
    // Only handle qwen3 download events
    if (step !== "qwen3-download" && step !== "qwen3-check" && step !== "qwen3-pip") return;

    if (line.startsWith("PROGRESS:")) {
      // e.g. "PROGRESS: 42% 1250/3000 MB -- Qwen3-TTS-12Hz-0.6B-Base/model.safetensors"
      const match = line.match(/PROGRESS:\s*(\d+)%\s*(\d+)\/(\d+)\s*MB\s*--\s*(.*)/);
      if (match) {
        const [, pct, doneMb, totalMb, fileName] = match;
        if (Number(totalMb) > 0) {
          setFill(q3PctBase + Number(pct) * (100 - q3PctBase) / 100);
          addQwen3Sample(Number(doneMb), Number(totalMb));
        }
        const shortFile = fileName.length > 40 ? fileName.substring(0, 37) + "..." : fileName;
        setText(Number(totalMb) > 0
          ? `${pct}% · ${formatBytes(Number(doneMb) * 1048576)} of ${formatBytes(Number(totalMb) * 1048576)} · ${shortFile}`
          : shortFile);
      }
    } else if (line.startsWith("SIZE:")) {
      // e.g. "SIZE: 3200 MB total across 3 models"
      const match = line.match(/SIZE:\s*(\d+)\s*MB/);
      if (match) {
        q3.totalMb = Number(match[1]);
        setText(`Total download: ~${formatBytes(Number(match[1]) * 1048576)}`);
      }
    } else if (line.startsWith("DOWNLOAD:")) {
      // e.g. "DOWNLOAD: [2/3] Qwen/Qwen3-TTS-12Hz-0.6B-Base"
      const match = line.match(/\[(\d+)\/(\d+)\]\s*(.*)/);
      if (match) {
        const shortName = match[3].split("/").pop() ?? match[3];
        q3.done = Number(match[1]); // the engine and the models before this one are finished
        setText(`Downloading ${match[1]} of ${match[2]} · ${shortName}`);
      }
    } else if (line.startsWith("OK:") && line.includes("->")) {
      const parts = line.match(/OK:\s*(.*?)\s*->/);
      if (parts) {
        const shortName = parts[1].split("/").pop() ?? parts[1];
        q3.done = Math.min(q3.done + 1, q3.steps.length);
        setText(`Downloaded ${shortName}`);
      }
    }
  });

  try {
    // Get paths and tier
    const settings = await invoke<AppSettings>("get_settings");
    const paths: SetupPaths = await invoke("get_setup_paths");
    const tier = tierOverride ?? settings.qwen3_model_tier;
    const pythonExe = paths.python_exe; // the app's own Python, not whatever "python" is on PATH
    const ui = { paused: () => qwen3DownloadPaused, say: setText };

    // Step 1: the Python engine (faster-qwen3-tts and what it imports). Installs that only
    // have the models (from before this step existed) get just this, no model re-download.
    if (!settings.qwen3_runtime_installed) {
      q3PctBase = 25;
      const pip = (packages: string[], extra: string[] = []) => runNetworkCommand("qwen3-pip", {
        program: pythonExe,
        args: ["-m", "pip", "install", "--no-warn-script-location", ...PIP_NET_ARGS, ...extra, ...packages],
      }, ui);

      // Qwen3 needs CUDA PyTorch; a setup done without GPU acceleration has the CPU build
      setText("Checking PyTorch...");
      setFill(2);
      const hasCudaTorch = await invoke("setup_run_command", {
        program: pythonExe, args: ["-c", "import sys, torch; sys.exit(0 if torch.cuda.is_available() else 3)"], stepName: "qwen3-check",
      }).then(() => true, () => false);
      if (!hasCudaTorch) {
        setText("Installing PyTorch for your GPU (large download)...");
        await pip(["torch"], ["--index-url", "https://download.pytorch.org/whl/cu126"]);
      }
      if (qwen3DownloadPaused) throw new Error("Paused");

      setText("Installing the Qwen3 engine...");
      setFill(10);
      await pip(QWEN3_RUNTIME_DEPS);
      if (qwen3DownloadPaused) throw new Error("Paused");
      await pip(QWEN3_RUNTIME_PACKAGES, ["--no-deps"]);

      setText("Checking the engine...");
      setFill(20);
      await invoke("setup_run_command", {
        program: pythonExe, args: ["-c", QWEN3_IMPORT_CHECK], stepName: "qwen3-check",
      }).catch((e) => {
        throw new Error(String(e).includes("NO_CUDA")
          ? "The engine installed, but PyTorch cannot use your GPU. Update your NVIDIA driver and try again."
          : `The Qwen3 engine did not install correctly: ${extractDownloadError(String(e))}`);
      });
      await invoke("qwen3_mark_runtime_installed");
    }
    q3.done = 1;
    renderQwen3Progress();

    // Step 2: Download models using the robust downloader script (files already present are skipped).
    if (qwen3DownloadPaused) throw new Error("Paused");
    setText("Starting model download...");
    setFill(q3PctBase);

    const scriptPath = paths.server_dir.replace(/[/\\]$/, "") + "\\download_qwen3.py";
    const result = await invoke<string>("setup_run_command", {
      program: pythonExe,
      args: [scriptPath, "--tier", tier, "--data-dir", paths.data_dir],
      stepName: "qwen3-download",
      env: { VOICELINK_DATA_DIR: paths.data_dir },
    });

    if (!result.includes("DONE")) {
      // PARTIAL: some model failed, so nothing is marked as installed
      throw new Error(extractDownloadError(result) || "Some Qwen3 models did not download. Press Retry.");
    }
    // The tier only changes once its models are complete (switching from the Design tab)
    if (tier !== settings.qwen3_model_tier) await invoke("save_settings", { qwen3ModelTier: tier });

    setFill(100);
    q3.text = "Qwen3 is installed";
    stopQwen3Progress("done");
    btns.forEach((b) => { b.disabled = true; b.textContent = "Installed"; });

    const newSettings = await invoke<AppSettings>("get_settings");
    refreshQwen3UI(newSettings);
    // Let the success state show for a moment, then swap the intro card for the tools
    await new Promise((r) => setTimeout(r, 1800));
    updateStudioView(newSettings);
  } catch (e) {
    console.error("Qwen3 download failed:", e);
    const errStr = String(e);

    if (qwen3DownloadPaused) {
      // User paused — show resume button
      setText("Progress is saved — resume anytime.");
      stopQwen3Progress("paused");
      setBtns("Resume Download", false);
    } else {
      // Actual error
      const shortErr = extractDownloadError(errStr);
      setText(`Failed: ${shortErr}`);
      setFill(0);
      stopQwen3Progress("error");
      setBtns("Retry Download", false);
    }
  } finally {
    qwen3Downloading = false;
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

// Tabs and forms must be wired once; this runs on every visit to the page
let studioWired = false;

function setupVoiceStudioTabs() {
  // Built-in voices list refreshes on every visit
  loadQwen3Voices();
  if (studioWired) return;
  studioWired = true;

  // Tab switching
  const tabs = document.querySelectorAll<HTMLElement>(".studio-tab");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const targetTab = tab.dataset.tab;
      if (!targetTab) return;

      tabs.forEach((t) => {
        t.classList.toggle("active", t === tab);
        t.setAttribute("aria-selected", String(t === tab));
        t.tabIndex = t === tab ? 0 : -1;
      });

      document.querySelectorAll<HTMLElement>(".studio-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.id === `studio-${targetTab}`);
      });
    });
    // Arrow keys move between tabs, as in any tab list
    tab.addEventListener("keydown", (e) => {
      const list = Array.from(tabs);
      const i = list.indexOf(tab) + (e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0);
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const next = list[(i + list.length) % list.length];
      next.focus();
      next.click();
    });
    tab.tabIndex = tab.classList.contains("active") ? 0 : -1;
  });

  // Clone form validation — enable Preview button when all fields filled
  setupCloneForm();

  // Design form validation — enable Generate button when fields filled
  setupDesignForm();
}

// ---- Shared pieces of the clone and design forms ----

/** The folder-safe name the server stores a voice under (see _safe_name in server/routers/qwen3.py) */
function voiceFolderName(name: string): string {
  return name.replace(/[^\p{L}\p{N}_\- ]/gu, "").trim();
}

/** Show the "working..." card with a running clock and, after a while, reassurance. Returns the stop function. */
function showBusy(prefix: "clone" | "design", hints: [number, string][]): () => void {
  const box = document.getElementById(`${prefix}-busy`)!;
  const timeEl = document.getElementById(`${prefix}-busy-time`)!;
  const textEl = document.getElementById(`${prefix}-busy-text`)!;
  const t0 = Date.now();
  const tick = () => {
    const s = Math.floor((Date.now() - t0) / 1000);
    timeEl.textContent = `${s} s`;
    textEl.textContent = [...hints].reverse().find(([from]) => s >= from)?.[1] ?? "";
  };
  box.classList.remove("hidden");
  tick();
  const id = window.setInterval(tick, 1000);
  return () => { window.clearInterval(id); box.classList.add("hidden"); };
}

interface NewVoice {
  voiceId: string;
  name: string;
  pcm: Uint8Array;
  badges: string[];
  sampleText: string;
  notice?: string; // shown under the name, e.g. why a clone is voice-only
}

/** Card for a voice that was just created: preview it right away, then save it to the library or discard it */
function addVoiceResult(container: HTMLElement, v: NewVoice) {
  const card = document.createElement("div");
  card.className = "voice-result";
  const seconds = (v.pcm.length / 48000).toFixed(1);
  card.innerHTML = `
    <div class="voice-result-head">
      <span class="clone-temp-name">${escapeHtml(v.name)}</span>
      ${v.badges.map((b) => `<span class="badge badge-warning">${escapeHtml(b)}</span>`).join("")}
      <span class="clone-temp-badge temp" data-role="state">Not saved yet</span>
    </div>
    ${v.notice ? '<p class="field-hint" role="status"></p>' : ""}
    <div class="voice-result-play">
      <button type="button" class="btn btn-primary" data-act="preview">&#9654; Preview the new voice</button>
      <span class="muted">${seconds} s sample</span>
    </div>
    <div class="voice-result-test">
      <input type="text" value="${escapeHtml(v.sampleText)}" maxlength="500" aria-label="Text for ${escapeHtml(v.name)} to say" />
      <button type="button" class="btn btn-secondary btn-sm" data-act="speak">Speak this text</button>
    </div>
    <div class="voice-result-actions">
      <span class="voice-result-note" role="status" aria-live="polite"></span>
      <button type="button" class="btn btn-secondary btn-sm" data-act="discard">Discard</button>
      <button type="button" class="btn btn-primary" data-act="save">Save to library</button>
    </div>`;
  container.prepend(card);
  // Filled after the card is in the page so screen readers announce it
  if (v.notice) card.querySelector<HTMLElement>('[role="status"].field-hint')!.textContent = v.notice;

  const $ = (act: string) => card.querySelector<HTMLButtonElement>(`[data-act="${act}"]`)!;
  const note = card.querySelector<HTMLElement>(".voice-result-note")!;
  const textInput = card.querySelector<HTMLInputElement>('input[type="text"]')!;
  let playTimer = 0;

  const resetPreview = () => { window.clearTimeout(playTimer); $("preview").innerHTML = "&#9654; Preview the new voice"; };
  const play = (pcm: Uint8Array) => {
    playPcmAudio(pcm);
    $("preview").innerHTML = "&#9632; Stop";
    playTimer = window.setTimeout(resetPreview, (pcm.length / 48000) * 1000 + 300);
  };
  play(v.pcm); // hear it as soon as it is ready

  $("preview").addEventListener("click", () => {
    if ($("preview").textContent?.includes("Stop")) { stopCurrentPlayback(); resetPreview(); } else play(v.pcm);
  });

  $("speak").addEventListener("click", async () => {
    const text = textInput.value.trim();
    if (!text) return;
    $("speak").disabled = true;
    note.textContent = "Generating...";
    try {
      play(new Uint8Array(await invoke<number[]>("qwen3_preview_voice", { voiceId: v.voiceId, text })));
      note.textContent = "";
    } catch (e) {
      note.textContent = `Could not speak: ${errorText(e)}`;
    } finally {
      $("speak").disabled = false;
    }
  });

  $("save").addEventListener("click", async () => {
    $("save").disabled = true;
    try {
      await invoke("qwen3_save_clone", { voiceId: v.voiceId });
      card.classList.add("saved");
      const state = card.querySelector<HTMLElement>('[data-role="state"]')!;
      state.textContent = "Saved";
      state.className = "clone-temp-badge saved";
      $("save").classList.add("hidden");
      $("discard").textContent = "Delete";
      note.textContent = "In your library. Turn it on in Voice Manager to use it in other apps.";
      loadVoices();
      loadQwen3Voices();
    } catch (e) {
      $("save").disabled = false;
      note.textContent = `Could not save: ${errorText(e)}`;
    }
  });

  $("discard").addEventListener("click", async () => {
    $("discard").disabled = true;
    try {
      stopCurrentPlayback();
      await invoke("qwen3_delete_clone", { voiceId: v.voiceId });
      card.remove();
      loadVoices();
      loadQwen3Voices();
    } catch (e) {
      $("discard").disabled = false;
      note.textContent = `Could not delete: ${errorText(e)}`;
    }
  });
}

// ---- Clone Voice Form ----

const CLONE_RATE = 24000;
// What the research says (Alibaba Model Studio voice-cloning guide, Qwen3-TTS README): 10-20 s is
// the sweet spot, 3 s is the model's stated minimum and 60 s the cap we enforce on the server.
const CLIP_MIN_S = 3;
const CLIP_GOOD_FROM_S = 10;
const CLIP_GOOD_TO_S = 20;
const CLIP_MAX_S = 60;

interface Clip {
  fileName: string;
  seconds: number;
  wav: Uint8Array; // what is uploaded: 24 kHz mono 16-bit, whatever the source format was
  url: string;     // object URL of the wav, for the player
  peaks: number[]; // 0..1 per waveform bar
  blocked: boolean;
}

/** Decode any audio file the browser can play to mono samples at 24 kHz */
async function decodeClip(file: File): Promise<Float32Array> {
  const ctx = new AudioContext({ sampleRate: CLONE_RATE });
  try {
    const buf = await ctx.decodeAudioData(await file.arrayBuffer());
    const out = new Float32Array(buf.length);
    for (let c = 0; c < buf.numberOfChannels; c++) {
      const ch = buf.getChannelData(c);
      for (let i = 0; i < out.length; i++) out[i] += ch[i] / buf.numberOfChannels;
    }
    return out;
  } finally {
    ctx.close();
  }
}

function encodeWav(samples: Float32Array, rate: number): Uint8Array {
  const out = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const text = (at: number, s: string) => { for (let i = 0; i < s.length; i++) out.setUint8(at + i, s.charCodeAt(i)); };
  text(0, "RIFF"); out.setUint32(4, 36 + samples.length * 2, true); text(8, "WAVE"); text(12, "fmt ");
  out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, 1, true);
  out.setUint32(24, rate, true); out.setUint32(28, rate * 2, true); out.setUint16(32, 2, true); out.setUint16(34, 16, true);
  text(36, "data"); out.setUint32(40, samples.length * 2, true);
  samples.forEach((s, i) => out.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s)) * 32767, true));
  return new Uint8Array(out.buffer);
}

/** One verdict on the clip: length first, then recording quality */
function judgeClip(samples: Float32Array): { level: "ok" | "warn" | "bad"; text: string } {
  const secs = samples.length / CLONE_RATE;
  const dur = `${secs.toFixed(1)} s`;
  if (secs < CLIP_MIN_S) return { level: "bad", text: `Too short: ${dur}. Use at least ${CLIP_MIN_S} seconds of speech; 10 to 20 seconds gives the best match.` };
  if (secs > CLIP_MAX_S) return { level: "bad", text: `Too long: ${Math.round(secs)} s. The limit is ${CLIP_MAX_S} seconds. Cut out the clearest 10 to 20 seconds.` };

  let peak = 0, clipped = 0;
  for (const s of samples) { const a = Math.abs(s); if (a > peak) peak = a; if (a > 0.99) clipped++; }
  const frame = CLONE_RATE / 50; // 20 ms
  let quiet = 0, frames = 0;
  for (let i = 0; i + frame <= samples.length; i += frame, frames++) {
    let e = 0;
    for (let j = 0; j < frame; j++) e += samples[i + j] ** 2;
    if (Math.sqrt(e / frame) < 0.01) quiet++;
  }

  const issues: string[] = [];
  if (peak < 0.003) return { level: "bad", text: "This clip is silent. Check the recording." };
  if (peak < 0.05) issues.push("It is very quiet, so noise will be loud next to the voice.");
  if (clipped / samples.length > 0.005) issues.push("It is distorted (too loud and clipped).");
  if (quiet / frames > 0.4) issues.push("Over 40% of it is silence. Long pauses make a weaker clone.");
  if (secs < CLIP_GOOD_FROM_S) issues.unshift(`Short clip (${dur}). It works, but 10 to 20 seconds matches the voice better.`);
  if (secs > CLIP_GOOD_TO_S) issues.unshift(`Long clip (${dur}). More than 20 seconds does not improve the match and makes cloning slower.`);
  if (issues.length) return { level: "warn", text: issues.join(" ") };
  return { level: "ok", text: `Good clip: ${dur}, clear level.` };
}

function drawWave(canvas: HTMLCanvasElement, peaks: number[]) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.floor(canvas.clientWidth * dpr));
  const h = Math.max(1, Math.floor(canvas.clientHeight * dpr));
  canvas.width = w;
  canvas.height = h;
  const g = canvas.getContext("2d");
  if (!g) return;
  g.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent-hover").trim() || "#7c6ef7";
  const barW = 3 * dpr, gap = 1 * dpr;
  const n = Math.floor(w / (barW + gap));
  for (let i = 0; i < n; i++) {
    const p = peaks[Math.floor((i / n) * peaks.length)] ?? 0;
    const bh = Math.max(2 * dpr, p * h * 0.9);
    g.fillRect(i * (barW + gap), (h - bh) / 2, barW, bh);
  }
}

function setupCloneForm() {
  const el = <T extends HTMLElement>(id: string) => document.getElementById(id) as T | null;
  const form = el<HTMLFormElement>("clone-form");
  const drop = el("clone-drop");
  const fileInput = el<HTMLInputElement>("clone-audio");
  const browse = el<HTMLButtonElement>("drop-browse");
  const fileBox = el("drop-file");
  const nameEl = el("drop-name");
  const durEl = el("drop-dur");
  const changeBtn = el<HTMLButtonElement>("drop-change");
  const canvas = el<HTMLCanvasElement>("wave-canvas");
  const playhead = el("wave-playhead");
  const player = el<HTMLAudioElement>("clone-player");
  const check = el("clone-clip-check");
  const transcript = el<HTMLTextAreaElement>("clone-transcript");
  const transcriptHint = el("clone-transcript-hint");
  const nameInput = el<HTMLInputElement>("clone-name");
  const gender = el<HTMLSelectElement>("clone-gender");
  const submit = el<HTMLButtonElement>("btn-clone-create");
  const formHint = el("clone-form-hint");
  const results = el("clone-results");
  if (!form || !drop || !fileInput || !browse || !fileBox || !nameEl || !durEl || !changeBtn || !canvas || !playhead
      || !player || !check || !transcript || !transcriptHint || !nameInput || !gender || !submit || !formHint || !results) return;

  let clip: Clip | null = null;
  let busy = false;

  function setCheck(level: string, text: string) {
    check!.dataset.level = level;
    check!.textContent = text;
    check!.classList.toggle("hidden", !text);
  }

  function updateState() {
    // The transcript field explains its own trade-off: with text the clone copies accent and rhythm, without it only the sound
    transcriptHint!.innerHTML = transcript!.value.trim()
      ? "Used word for word next to the audio, so Qwen3 can match the accent and rhythm. <strong>A wrong word can break the clone, and VoiceLink then falls back to voice-only mode.</strong>"
      : "<strong>No transcript: voice-only mode.</strong> Copies the sound of the voice but less of its accent and speaking style. Fine for a quick try; type what is said for the best match.";

    const missing = busy ? "" : !clip ? "Add a recording to continue."
      : clip.blocked ? "Pick a different clip to continue."
      : !voiceFolderName(nameInput!.value) ? "Give the voice a name (letters or numbers)."
      : "";
    formHint!.textContent = missing;
    submit!.disabled = busy || !!missing;
  }

  function clearClip() {
    if (clip) URL.revokeObjectURL(clip.url);
    clip = null;
    player!.removeAttribute("src");
    player!.load();
    drop!.classList.remove("has-file");
    fileBox!.classList.add("hidden");
    browse!.classList.remove("hidden");
    fileInput!.value = "";
    setCheck("", "");
    updateState();
  }

  async function loadClip(file: File) {
    if (!(file.type.startsWith("audio/") || /\.(wav|mp3|m4a|flac|ogg|aac|opus)$/i.test(file.name))) {
      setCheck("bad", "That is not an audio file. Use WAV, MP3, M4A, FLAC or OGG.");
      return;
    }
    if (file.size > 100 * 1024 * 1024) {
      setCheck("bad", "That file is very large. Cut a 10 to 20 second clip from it first.");
      return;
    }
    setCheck("", "Reading the clip...");
    check!.classList.remove("hidden");
    let samples: Float32Array;
    try {
      samples = await decodeClip(file);
    } catch (e) {
      clearClip();
      setCheck("bad", "VoiceLink could not read this file. Try saving it as WAV or MP3 and add it again.");
      return;
    }
    if (clip) URL.revokeObjectURL(clip.url);
    const verdict = judgeClip(samples);
    const blocks = 240;
    const per = Math.max(1, Math.floor(samples.length / blocks));
    const peaks: number[] = [];
    for (let b = 0; b < blocks; b++) {
      let m = 0;
      for (let i = b * per; i < Math.min(samples.length, (b + 1) * per); i++) m = Math.max(m, Math.abs(samples[i]));
      peaks.push(m);
    }
    const norm = Math.max(...peaks, 0.001);
    const wav = encodeWav(samples, CLONE_RATE);
    clip = {
      fileName: file.name,
      seconds: samples.length / CLONE_RATE,
      wav,
      url: URL.createObjectURL(new Blob([wav], { type: "audio/wav" })),
      peaks: peaks.map((p) => p / norm),
      blocked: verdict.level === "bad",
    };
    nameEl!.textContent = file.name;
    nameEl!.title = file.name;
    durEl!.textContent = `${clip.seconds.toFixed(1)} s`;
    player!.src = clip.url;
    drop!.classList.add("has-file");
    browse!.classList.add("hidden");
    fileBox!.classList.remove("hidden");
    drawWave(canvas!, clip.peaks);
    playhead!.style.left = "0%";
    setCheck(verdict.level, verdict.text);
    // Suggest a voice name from the file name, once
    if (!nameInput!.value.trim()) nameInput!.value = file.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").slice(0, 40);
    updateState();
  }

  // Choose / replace: click or keyboard on the buttons, or drop a file on the zone
  browse.addEventListener("click", () => fileInput.click());
  changeBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => { const f = fileInput.files?.[0]; if (f) loadClip(f); });
  for (const type of ["dragenter", "dragover"]) {
    drop.addEventListener(type, (e) => { e.preventDefault(); if (!busy) drop.classList.add("dragover"); });
  }
  drop.addEventListener("dragleave", (e) => { if (!drop.contains(e.relatedTarget as Node | null)) drop.classList.remove("dragover"); });
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("dragover");
    const f = e.dataTransfer?.files?.[0];
    if (f && !busy) loadClip(f);
  });
  // A file dropped beside the zone must not make the window navigate to it
  for (const type of ["dragover", "drop"]) window.addEventListener(type, (e) => e.preventDefault());

  // Moving marker over the waveform while the clip plays
  let raf = 0;
  const follow = () => {
    playhead.style.left = `${player.duration ? (player.currentTime / player.duration) * 100 : 0}%`;
    raf = player.paused ? 0 : requestAnimationFrame(follow);
  };
  player.addEventListener("play", () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(follow); });
  player.addEventListener("pause", follow);
  player.addEventListener("ended", follow);
  player.addEventListener("seeked", follow);
  new ResizeObserver(() => { if (clip) drawWave(canvas, clip.peaks); }).observe(canvas);

  nameInput.addEventListener("input", updateState);
  transcript.addEventListener("input", updateState);
  updateState();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy || !clip || clip.blocked) return;
    const name = nameInput.value.trim();
    const text = transcript.value.trim();
    busy = true;
    for (const c of [nameInput, transcript, gender, browse, changeBtn]) c.disabled = true;
    updateState();
    const stop = showBusy("clone", [
      [0, "Sending the clip to Qwen3..."],
      [4, "Qwen3 is learning the voice and speaking a short preview."],
      [20, "The first clone after VoiceLink starts is the slowest: Qwen3 loads onto your GPU and warms up (about a minute). Later ones take seconds."],
      [120, "Still working. If this goes past five minutes, check the Dashboard that the server is running."],
    ]);
    try {
      const clone = await invoke<{ pcm: number[]; voice_only: boolean }>("qwen3_clone_voice", {
        name,
        transcript: text,
        audioData: Array.from(clip.wav),
        audioFilename: "reference.wav",
        gender: gender.value,
        description: "",
        previewText: "Hello, this is a preview of the cloned voice.",
      });
      addVoiceResult(results, {
        voiceId: `qwen3_custom_${voiceFolderName(name)}`,
        name,
        pcm: new Uint8Array(clone.pcm),
        badges: clone.voice_only ? ["Cloned", "voice-only"] : ["Cloned"],
        sampleText: "This is how my cloned voice sounds when it reads a new sentence.",
        // Only when the user typed a transcript: without one, voice-only is what they asked for
        notice: text && clone.voice_only
          ? "Qwen3 could not follow this transcript, so this voice uses voice-only mode (the sound of the voice, less of its accent). For a closer match, check the transcript says exactly what the clip says, word for word, and clone again."
          : undefined,
      });
      clearClip();
      nameInput.value = "";
      transcript.value = "";
    } catch (err) {
      await showModal("Voice cloning failed", errorText(err), true);
    } finally {
      stop();
      busy = false;
      for (const c of [nameInput, transcript, gender, browse, changeBtn]) c.disabled = false;
      updateState();
    }
  });
}

// ---- Design Voice Form ----

function setupDesignForm() {
  const el = <T extends HTMLElement>(id: string) => document.getElementById(id) as T | null;
  const form = el<HTMLFormElement>("design-form");
  const nameInput = el<HTMLInputElement>("design-name");
  const descInput = el<HTMLTextAreaElement>("design-description");
  const sampleInput = el<HTMLInputElement>("design-sample");
  const submit = el<HTMLButtonElement>("btn-design-generate");
  const hint = el("design-form-hint");
  const results = el("design-results");
  if (!form || !nameInput || !descInput || !sampleInput || !submit || !hint || !results) return;

  let busy = false;
  function updateState() {
    const missing = busy ? ""
      : descInput!.value.trim().length < 10 ? "Describe the voice in at least a few words."
      : sampleInput!.value.trim().length < 10 ? "Write a sample sentence for the preview."
      : !voiceFolderName(nameInput!.value) ? "Give the voice a name (letters or numbers)."
      : "";
    hint!.textContent = missing;
    submit!.disabled = busy || !!missing;
  }
  for (const input of [nameInput, descInput, sampleInput]) input.addEventListener("input", updateState);
  document.querySelectorAll<HTMLElement>("#design-examples .chip").forEach((chip) => {
    chip.addEventListener("click", () => { descInput.value = chip.dataset.text ?? ""; updateState(); descInput.focus(); });
  });
  updateState();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy || submit.disabled) return;
    const name = nameInput.value.trim();
    const sample = sampleInput.value.trim();
    busy = true;
    for (const c of [nameInput, descInput, sampleInput]) c.disabled = true;
    updateState();
    const stop = showBusy("design", [
      [0, "Sending the description to Qwen3..."],
      [4, "Qwen3 is imagining the voice and speaking your sample sentence."],
      [20, "The first design after VoiceLink starts is the slowest: the 1.7B model loads onto your GPU (a minute or two). Later ones take seconds."],
      [150, "Still working. If this goes past five minutes, check the Dashboard that the server is running."],
    ]);
    try {
      const pcm = await invoke<number[]>("qwen3_design_voice", { name, description: descInput.value.trim(), sampleText: sample });
      addVoiceResult(results, {
        voiceId: `qwen3_custom_${voiceFolderName(name)}`,
        name,
        pcm: new Uint8Array(pcm),
        badges: ["Designed"],
        sampleText: "And this is the same voice reading something completely different.",
      });
      nameInput.value = "";
    } catch (err) {
      await showModal("Voice design failed", errorText(err), true);
    } finally {
      stop();
      busy = false;
      for (const c of [nameInput, descInput, sampleInput]) c.disabled = false;
      updateState();
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

    container.querySelectorAll<HTMLButtonElement>(".qwen3-preview-btn").forEach((btn) => {
      const actions = btn.parentElement!;
      const note = document.createElement("p");
      note.className = "field-hint preview-note";
      note.setAttribute("role", "status");
      note.setAttribute("aria-live", "polite");
      actions.after(note);
      wireQwen3Preview(btn, note, btn.dataset.voice ?? "", "Hello! This is a preview of my voice.");
    });
  } catch (e) {
    container.innerHTML = `<p class="placeholder">Could not load Qwen3 voices: ${escapeHtml(errorText(e))}</p>`;
    console.error("Load Qwen3 voices failed:", e);
  }
}

/** Qwen3 loads onto the GPU on the first request after VoiceLink starts; later ones are quick */
let qwen3Warm = false;

/**
 * Preview button with visible states: Generating (with the first-use note and a clock),
 * Stop while it plays, and the server's real reason when it fails.
 */
function wireQwen3Preview(btn: HTMLButtonElement, note: HTMLElement, voiceId: string, text: string) {
  let playTimer = 0;
  const idle = () => { window.clearTimeout(playTimer); btn.disabled = false; btn.textContent = "Preview"; };

  btn.addEventListener("click", async () => {
    if (btn.textContent === "Stop") { stopCurrentPlayback(); idle(); return; }

    btn.disabled = true;
    btn.textContent = "Generating...";
    note.style.color = "";
    const t0 = Date.now();
    const clock = window.setInterval(() => {
      const s = Math.round((Date.now() - t0) / 1000);
      note.textContent = qwen3Warm
        ? `Generating... ${s} s`
        : `Generating... ${s} s. The first preview after VoiceLink starts loads Qwen3 onto your GPU; this can take about a minute.`;
    }, 500);
    note.textContent = "Generating...";
    try {
      const pcm = new Uint8Array(await invoke<number[]>("qwen3_preview_voice", { voiceId, text }));
      qwen3Warm = true;
      if (pcm.length === 0) throw new Error("Qwen3 returned no audio.");
      note.textContent = "";
      playPcmAudio(pcm);
      btn.disabled = false;
      btn.textContent = "Stop";
      playTimer = window.setTimeout(idle, (pcm.length / 48000) * 1000 + 300);
    } catch (e) {
      idle();
      note.textContent = `Preview failed: ${errorText(e)}`;
      note.style.color = "var(--danger)";
    } finally {
      window.clearInterval(clock);
    }
  });
}

// ---- PCM Audio Playback ----

// ============================================================================
// Narrate Page
// ============================================================================

let narrateWired = false;
let narratePcmData: Uint8Array | null = null;
let narrateAudioCtx: AudioContext | null = null;
let narrateSource: AudioBufferSourceNode | null = null;
let narrateBuffer: AudioBuffer | null = null;
let narrateStartTime = 0;
let narrateOffset = 0;
let narratePlaying = false;
let narrateRafId = 0;
let narrateQwen3Ok = false; // Qwen3 voices are offered only when it is enabled and installed

/** Kokoro voices (plus Qwen3's when usable); starts on the default / a favorite Kokoro voice */
function refreshNarrateVoices() {
  const vs = currentVoices.filter((v) => v.model !== "qwen3" || narrateQwen3Ok);
  const preferred = [defaultVoice.id, ...favoriteIds].find((id) => vs.some((v) => v.id === id && v.model !== "qwen3"));
  narratePicker?.setVoices(vs, preferred ?? undefined);
}

/** Language only applies to Qwen3; a Kokoro voice carries its own language */
function syncNarrateLanguage(voiceId: string) {
  const qwen3 = voiceId.startsWith("qwen3_");
  const select = document.getElementById("narrate-language") as HTMLSelectElement | null;
  if (select) select.disabled = !qwen3;
  document.getElementById("narrate-language-hint")?.classList.toggle("hidden", qwen3);
}

/** Runs on every visit to the Narrate page: refresh the voice list, wire listeners once */
function setupNarratePage() {
  refreshNarrateVoices();
  if (!voicesLoaded) loadVoices();
  invoke<AppSettings>("get_settings")
    .then((s) => {
      narrateQwen3Ok = s.qwen3_enabled && s.qwen3_installed;
      refreshNarrateVoices();
    })
    .catch(console.error);
  if (narrateWired) return;
  narrateWired = true;

  const textArea = document.getElementById("narrate-text") as HTMLTextAreaElement | null;
  const fileInput = document.getElementById("narrate-file") as HTMLInputElement | null;
  const speedSlider = document.getElementById("narrate-speed") as HTMLInputElement | null;
  const speedLabel = document.getElementById("narrate-speed-label");
  const charCount = document.getElementById("narrate-char-count");
  const generateBtn = document.getElementById("btn-narrate-generate") as HTMLButtonElement | null;

  if (!textArea) return;

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

    const voiceId = narratePicker?.value ?? "";
    if (!voiceId) return;
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
    if (statusEl) { statusEl.textContent = "Generating narration... the first one after VoiceLink starts loads Qwen3 and can take a minute."; statusEl.style.color = ""; }
    if (progressBar) progressBar.style.width = "30%";

    // Stop any playing audio
    narrateStopPlayback();

    let narrationFailed = false;
    try {
      const pcmBytes = voiceId.startsWith("qwen3_")
        ? await invoke<number[]>("qwen3_narrate", { voiceId, text, language, speed })
        : await invoke<number[]>("kokoro_narrate", { voiceId, text, speed });

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
      narrationFailed = true;
      // Stays on screen (unlike the "Done!" message) so the reason can be read
      if (statusEl) { statusEl.textContent = `Narration failed: ${errorText(e)}`; statusEl.style.color = "var(--danger)"; }
      if (progressBar) progressBar.style.width = "0%";
    } finally {
      generateBtn.disabled = false;
      generateBtn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3" /></svg> Generate`;
      if (!narrationFailed) setTimeout(() => { if (progressDiv) progressDiv.style.display = "none"; }, 2000);
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

  // Download WAV — native save dialog
  document.getElementById("btn-narrate-download")?.addEventListener("click", async () => {
    if (!narratePcmData) return;
    const btn = document.getElementById("btn-narrate-download") as HTMLButtonElement | null;
    if (!btn) return;

    btn.disabled = true;
    const origHtml = btn.innerHTML;
    btn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg> Saving...`;

    try {
      const savedPath = await invoke<string | null>("save_wav_file", {
        pcmData: Array.from(narratePcmData),
      });

      if (savedPath) {
        btn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> Saved!`;
        btn.classList.remove("btn-secondary");
        btn.classList.add("btn-success");
        setTimeout(() => {
          btn.innerHTML = origHtml;
          btn.classList.remove("btn-success");
          btn.classList.add("btn-secondary");
        }, 3000);
      } else {
        // User cancelled the dialog
        btn.innerHTML = origHtml;
      }
    } catch (e: any) {
      console.error("Save failed:", e);
      btn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg> Failed`;
      btn.classList.remove("btn-secondary");
      btn.classList.add("btn-danger");
      setTimeout(() => {
        btn.innerHTML = origHtml;
        btn.classList.remove("btn-danger");
        btn.classList.add("btn-secondary");
      }, 3000);
    } finally {
      btn.disabled = false;
    }
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
export function pcmToWavBlob(pcmData: Uint8Array, sampleRate: number): Blob {
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
// PCM Playback (singleton — stops previous audio before starting new)
// ============================================================================

let _activeAudioCtx: AudioContext | null = null;
let _activeAudioSrc: AudioBufferSourceNode | null = null;

function stopCurrentPlayback() {
  if (_activeAudioSrc) {
    try { _activeAudioSrc.stop(); } catch (_) { /* already stopped */ }
    _activeAudioSrc = null;
  }
  if (_activeAudioCtx) {
    _activeAudioCtx.close();
    _activeAudioCtx = null;
  }
}

function playPcmAudio(pcmData: Uint8Array) {
  try {
    stopCurrentPlayback();

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

    _activeAudioCtx = audioCtx;
    _activeAudioSrc = source;

    source.onended = () => {
      audioCtx.close();
      if (_activeAudioCtx === audioCtx) {
        _activeAudioCtx = null;
        _activeAudioSrc = null;
      }
    };

    source.start();
  } catch (e) {
    console.error("PCM playback failed:", e);
  }
}

// ============================================================================
// Utilities
// ============================================================================

// The registry name other apps see ends in " (Kokoro)" / " (Qwen3)"; the model
// is already shown as a badge in our own UI, so drop the suffix here.
function displayVoiceName(name: string): string {
  return name.replace(/\s*\((Kokoro|Qwen3)\)$/, "");
}

/** Message for an error dialog: never empty, so a failure can't show a blank pop-up */
function errorText(e: unknown): string {
  const text = (e instanceof Error ? e.message : String(e ?? "")).trim();
  return text || "No details were reported. The log files are in the VoiceLink data folder.";
}

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
  wireGotoSetupButtons();
  wireRepairSetupLink();
  wireLogViewers();
  startStatusPolling();

  // Check setup state and apply gray-out / onboarding tour
  try {
    const status: SetupStatus = await invoke("get_setup_status");
    const allDone = status.python_installed && status.deps_installed &&
      status.server_installed && status.model_downloaded;

    applySetupIncompleteState(allDone);

    if (allDone) {
      loadVoices();
      if (!status.server_running) {
        await invoke("start_server");
      }
    } else {
      startOnboardingTourIfNeeded(false);
    }
  } catch (e) {
    console.error("Init check failed:", e);
    applySetupIncompleteState(false);
  }
});
