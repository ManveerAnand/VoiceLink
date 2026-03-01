# VoiceLink — Tasks and Research Tracker

> This document tracks every phase, task, and learning objective. Each task links understanding (WHY/HOW) with implementation (BUILD).

---

## Phase 0: Research and Deep Understanding
**Goal:** Before writing a single line of production code, understand every layer of the system deeply.

### 0.1 — Understand Windows SAPI (Speech API)
> *What is SAPI? How does Windows discover and use voices? What interfaces must we implement?*

- [x] **Research: What is SAPI 5?**
  - Read Microsoft SAPI 5.4 documentation
  - Understand the difference between SAPI 4, SAPI 5, and OneCore voices
  - Document: How does an app like Thorium Reader find available voices?
  - Learning: Registry keys involved (`HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\`)
  - ✅ Documented in DEEP_DIVE.md Section 2

- [x] **Research: The ISpTTSEngine Interface**
  - What methods must a TTS engine implement?
  - `Speak()`, `GetOutputFormat()`, `SetObjectToken()` — what does each do?
  - How does SAPI pass text to the engine? (plain text vs SSML)
  - How does the engine return audio? (`ISpTTSEngineSite::Write()`)
  - ✅ Documented in DEEP_DIVE.md Section 2

- [x] **Research: COM (Component Object Model) Fundamentals**
  - What is COM? Why does Windows use it for plugins?
  - IUnknown, IClassFactory, reference counting
  - GUIDs, CLSIDs, ProgIDs — what are they?
  - How does DllRegisterServer work?
  - How does `CoCreateInstance` find and load our DLL?
  - ✅ Documented in DEEP_DIVE.md Section 3

- [x] **Research: Audio Formats in SAPI**
  - What audio formats does SAPI expect? (PCM, sample rates)
  - What is `WAVEFORMATEX`?
  - How does streaming work — can we send audio in chunks?
  - Latency requirements — how fast must first audio arrive?
  - ✅ Documented in DEEP_DIVE.md Section 7

- [x] **Experiment: List all SAPI voices on my system**
  - Write a small Python/PowerShell script using `win32com` or `pyttsx3`
  - List all installed voices, their properties, registry entries
  - Try speaking with each voice, observe the API calls
  - ✅ Script: `research/01_explore_sapi.ps1`. Found David, Zira, 7 OneCore voices.

- [ ] **Experiment: Inspect Thorium Reader's TTS usage**
  - How does Thorium call SAPI? (Electron + SAPI bridge?)
  - Does it use SSML or plain text?
  - What audio format does it request?
  - Use Process Monitor to trace the COM calls

### 0.2 — Understand Neural TTS Models
> *How do modern TTS models work? What are our options? What are the tradeoffs?*

- [x] **Research: How Neural TTS Works (High Level)**
  - Text → Tokens → Mel Spectrogram → Waveform
  - What is a vocoder? (HiFi-GAN, etc.)
  - Difference between autoregressive and non-autoregressive models
  - What determines voice quality vs speed?
  - ✅ Documented in DEEP_DIVE.md Section 4

- [x] **Survey: Open-Source TTS Models**
  - Evaluate each on: quality, speed, license, ease of use, voice cloning
  - **Kokoro** — Small, fast, Apache 2.0, multiple voices
  - **Piper** — Optimized for Raspberry Pi, very fast, many languages
  - **Qwen-3 TTS** — Large model, highest quality, needs GPU
  - **Coqui/XTTS** — Voice cloning capable
  - **F5-TTS** — Zero-shot voice cloning
  - **Parler-TTS** — Describe the voice you want in natural language
  - ✅ Documented in DEEP_DIVE.md Section 5 & 6

- [x] **Experiment: Run Kokoro locally**
  - Install dependencies, download model
  - Generate speech from text, measure latency and quality
  - Test streaming output — can we get audio chunk by chunk?
  - ✅ Script: `research/02_kokoro_test.py`. 4 voices tested, quality verified.

- [ ] **Experiment: Run Piper locally** *(Skipped — user tested Piper previously, prefers Kokoro)*

- [ ] **Research: ONNX Runtime for TTS** *(Future — Kokoro PyTorch works well for now)*

### 0.3 — Understand the Audio Pipeline
> *How does audio flow from a TTS model to the user's speakers through SAPI?*

- [x] **Research: PCM Audio Basics**
  - Sample rate, bit depth, channels
  - How to calculate buffer sizes
  - WAV file format structure
  - ✅ Documented in DEEP_DIVE.md Section 7

- [x] **Research: Audio Streaming**
  - Chunked audio delivery vs full buffer
  - Ring buffers and double buffering
  - Latency vs quality tradeoffs
  - ✅ Implemented in server — chunked transfer encoding via StreamingResponse

- [x] **Research: SAPI Audio Sink**
  - How does `ISpTTSEngineSite::Write()` work?
  - Can we send partial audio? How often?
  - What happens if audio arrives too slowly?
  - ✅ Documented in DEEP_DIVE.md Section 2

### 0.4 — Study Existing Projects
> *Who has done something similar? What can we learn from them?*

- [x] **Study: NaturalVoiceSAPIAdapter (680★, C++)**
  - Bridges Azure Neural voices → SAPI via COM DLL
  - Key learnings: SPVTEXTFRAG→SSML building, cancel handling, 24kHz PCM streaming
  - ✅ Documented in DEEP_DIVE.md Section 9

- [x] **Study: windows-text-to-speech (Rust)**
  - SAPI engine in Rust with Piper support
  - Confirmed: modern SpeechSynthesizer API is locked to MS-signed voices
  - ✅ Documented in DEEP_DIVE.md Section 9

- [x] **Study: PySpTTSEnginePoC (Python)**
  - Pure Python COM TTS engine using comtypes
  - Good for learning, too fragile for production (out-of-process, crashy)
  - ✅ Documented in DEEP_DIVE.md Section 9

---

## Phase 1: TTS Inference Server
**Goal:** Build a local server that takes text and returns high-quality streaming audio.

### 1.1 — Server Foundation
- [x] Set up Python project with FastAPI
- [x] Define API contract (`POST /v1/tts` with text, voice, format params)
- [ ] WebSocket endpoint for streaming audio *(deferred — HTTP streaming works well)*
- [x] Health check endpoint (`GET /v1/health`)
- [x] Configuration system (model selection, GPU/CPU, port) — `server/config.py`

### 1.2 — Model Integration
- [x] Integrate Kokoro as first model — `server/models/kokoro_model.py`
- [x] Abstract model interface (so we can swap models) — `server/models/base.py`
- [ ] Integrate Piper as second model
- [ ] Model download and management system

### 1.3 — Streaming and Performance
- [x] Implement chunked audio streaming — StreamingResponse + chunked transfer
- [ ] Benchmark: time to first byte, total latency, throughput
- [ ] GPU vs CPU performance comparison *(GPU not active yet — CUDA mismatch)*
- [ ] Memory profiling and optimization

### 1.4 — Testing
- [ ] Unit tests for API contract
- [x] Integration tests with actual models — `research/03_test_server.py`
- [ ] Stress tests (concurrent requests)
- [ ] Audio quality validation

---

## Phase 2: SAPI COM Bridge
**Goal:** Build a Windows COM DLL that registers as a SAPI voice and proxies to our server.

### 2.1 — COM DLL Skeleton
- [x] Choose language: C++ *(decided after evaluating all 4 approaches — see DEEP_DIVE.md Section 10)*
- [x] Implement `IUnknown` and `IClassFactory`
- [x] Implement `DllRegisterServer` / `DllUnregisterServer`
  - Registers in both `Speech\Voices\Tokens` (classic SAPI) and `Speech_OneCore\Voices\Tokens` (Thorium/Electron/UWP)
- [x] Test: DLL registers and shows up in voice list — 11 voices visible

### 2.2 — SAPI Engine Implementation
- [x] Implement `ISpTTSEngine::Speak()`
- [x] Implement `ISpTTSEngine::GetOutputFormat()` — 24kHz 16-bit mono PCM
- [x] Implement `ISpObjectWithToken::SetObjectToken()` — reads VoiceLinkVoiceId from registry
- [x] Forward text to inference server via HTTP chunked transfer (WinHTTP)
- [x] Stream audio back via `ISpTTSEngineSite::Write()` — 8KB chunks (~170ms audio)
- [x] Handle SAPI rate mapping — `speed = 1.0 + effectiveRate * 0.5`, clamped [0.25, 4.0]
- [x] Handle SAPI volume control — maps 0-100 to float 0.0-1.0
- [ ] Rate control in Thorium Reader *(rate works in PowerShell but Thorium behavior TBD)*

### 2.3 — Integration Testing
- [x] Test with PowerShell SAPI — end-to-end speech confirmed (2.5x realtime)
- [x] Test with Thorium Reader — voices visible and speaking after OneCore registry fix
- [ ] Test with Edge Read Aloud
- [ ] Test with Windows Narrator
- [ ] Test with Balabolka

---

## Phase 3: Management GUI (Tauri v2)
**Goal:** Build a desktop app to manage VoiceLink — monitor server, configure voices, control SAPI registration.

### 3.1 — GUI Foundation
- [x] Choose framework: **Tauri v2** (Rust backend + web frontend, small binary)
- [x] Scaffold project with `create-tauri-app` — `gui/` directory
- [x] Configure `tauri.conf.json` — window 900×650, centered, tray icon
- [x] Set up Rust backend with `reqwest`, `winreg`, `tokio`, `serde` deps
- [x] Set up Vite + TypeScript frontend

### 3.2 — Dashboard
- [x] Sidebar navigation: Dashboard, Voices, Settings
- [x] Inference Server status card — polls `/v1/health` every 5s (status, model, GPU, uptime)
- [x] SAPI Bridge status card — checks registry CLSID + counts VoiceLink_ tokens
- [x] Quick Test panel — textarea + voice dropdown + Preview button with PCM→WebAudio playback

### 3.3 — Voice Manager
- [x] Voice grid — loads from `/v1/voices`, displays name, gender badge, description, tags, language, model
- [x] Rename voice — custom in-app modal (replaced browser `prompt()`), updates Attributes\Name in both registries
- [x] Test voice — plays voice preview using Quick Test textarea text (shared text), falls back to default
- [x] Toggle voice on/off — registers/unregisters voice token in both Speech and Speech_OneCore registries
  - Enable: creates full token structure (CLSID, VoiceLinkVoiceId, VoiceLinkServerPort, Attributes with Name/Gender/Language/Age/Vendor)
  - Disable: deletes token recursively with `delete_subkey_all`
  - Language/gender inferred from voice_id prefix (a*=en-US, b*=en-GB, *m_*=Male)
- [x] Registered state tracking — `get_registered_voice_ids` returns current SAPI-registered voices
- [ ] **Test toggle feature end-to-end** *(next session — toggle compiles but not yet verified in app)*

### 3.4 — UI Polish
- [x] Dark theme with CSS variables (purple accent, card-based layout)
- [x] Custom scrollbar styling
- [x] Fixed scroll issue — `body { height: 100vh }` constrains flex layout so `#content { overflow-y: auto }` works
- [x] Custom modal overlay for rename/errors (replaced ugly browser prompt)
- [x] Disabled voice visual feedback — dimmed cards with strikethrough name
- [x] System tray icon — "Open" and "Quit" menu, double-click shows window
- [ ] Responsive grid at different window sizes

### 3.5 — Settings Page
- [x] Server URL display (localhost:7860)
- [x] Auto-start toggle (UI only — not yet functional)
- [x] About section
- [ ] Persist settings to config file

### 3.6 — Installer & Setup Wizard
- [x] Research installer technologies — Tauri v2 NSIS bundler selected
- [x] Build NSIS installer with `tauri.conf.json` config
  - `installMode: perMachine` (writes to `C:\Program Files\VoiceLink`)
  - Resources: COM DLL + server/ directory bundled into installer
  - NSIS hooks for COM registration/unregistration (`hooks.nsh`)
- [x] Auto-uninstall previous version via `NSIS_HOOK_PREINSTALL`
  - Reads `QuietUninstallString` from registry, runs silently before new install
- [x] COM DLL registration via `NSIS_HOOK_POSTINSTALL` (regsvr32)
- [x] COM DLL unregistration via `NSIS_HOOK_PREUNINSTALL` (regsvr32 /u)
- [x] Static CRT linking (`/MT`) — DLL has zero external dependencies
- [x] First-run setup wizard (5 steps):
  1. Download + extract embedded Python 3.11 (~15 MB)
  2. Enable pip, install dependencies from `requirements.txt`
  3. Copy server files to data directory
  4. Download Kokoro ONNX model (~310 MB) + voices (~27 MB) from GitHub
  5. Start inference server
- [x] Configurable data directory (`C:\ProgramData\VoiceLink` default)
- [x] `setup_enable_pip` — modifies `._pth` file, adds data dir to Python path
- [x] `setup_download_file` — streaming download with progress events
- [x] `setup_run_command` — async stdout/stderr streaming with line-by-line progress
- [x] `CREATE_NO_WINDOW` flag on all subprocess spawns (no CMD flashes)
- [x] Model download reliability — checks file size (not just existence), cleans up 0-byte partials
- [x] Embedded Python `._pth` fix — adds data directory so `python -m server.main` works
- [ ] Auto-start server on login (Windows Task Scheduler or startup folder)

### 3.3 — Auto-start and Reliability
- [ ] Server auto-starts on login
- [ ] Graceful fallback if server is down
- [ ] Auto-restart on crash
- [ ] Logging and diagnostics

### 3.7 — Toggle Feature
- [x] Toggle voice on/off — tested end-to-end
- [x] Voices visible/hidden in SAPI after toggle

---

## Phase 4: Polish and Production
**Goal:** Make it reliable, maintainable, and shippable.

### 4.1 — CI/CD
- [ ] GitHub Actions: build COM DLL on push
- [ ] GitHub Actions: build and test inference server
- [ ] GitHub Actions: build installer
- [ ] Code signing for the DLL and installer
- [ ] Automated release pipeline

### 4.2 — Documentation
- [ ] User guide: Installation and setup
- [ ] Developer guide: Architecture and contributing
- [ ] API documentation for inference server
- [ ] Troubleshooting guide

### 4.3 — Quality
- [ ] Error handling audit
- [ ] Memory leak testing
- [ ] Security review (localhost-only server, no external access)
- [ ] Accessibility testing

---

## Phase 5: Ship and Iterate
**Goal:** Get it into real users' hands and improve based on feedback.

- [ ] GitHub Release v0.1.0 with installer
- [ ] Write announcement post (Reddit r/epub, r/accessibility, r/programming)
- [ ] Collect feedback
- [ ] Plan v0.2.0 features (voice cloning? more models? Linux support?)

---

## Notes and Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-02-28 | Project started | Thorium Reader TTS quality is terrible, neural TTS exists but can't be used |
| 2026-02-28 | Name: VoiceLink | Clean, memorable, describes the bridge concept |
| 2026-02-28 | Learning-first approach | Understanding every layer matters more than shipping fast |
| 2026-02-28 | Skipped Piper | User tested Piper previously, prefers Kokoro quality |
| 2026-02-28 | Architecture: Hybrid C++ COM + Python server | Best balance of reliability (C++ COM) and flexibility (Python ML) |
| 2026-02-28 | UX: Single .exe installer | A 10th-grader should be able to set it up — no terminal, no Python |
| 2026-02-28 | Server port: 7860 | Avoids common conflicts, easy to remember |
| 2026-02-28 | Audio format: 24kHz 16-bit mono | Matches both Kokoro output and SAPI SPSF_24kHz16BitMono — no resampling |
| 2026-02-28 | Dual registry registration | Must register in both Speech and Speech_OneCore for Thorium/Electron apps |
| 2026-02-28 | Phase 2 complete | COM DLL working end-to-end in PowerShell and Thorium Reader |
| 2026-03-01 | GUI framework: Tauri v2 | Rust backend for registry/HTTP ops, web frontend for UI, small binary (~5MB) |
| 2026-03-01 | Custom modal over browser prompt | `prompt()` shows ugly "localhost:1420 says" — built in-app modal with backdrop |
| 2026-03-01 | Scroll fix: height vs min-height | `body { min-height: 100vh }` let body grow beyond viewport, defeating `overflow-y: auto` |
| 2026-03-01 | Voice toggle architecture | Enable/disable voices in SAPI registry. Toggle creates/deletes full token structure. |
| 2026-03-01 | Shared test text | Voice card "Test" and Quick Test panel share the same textarea — no hardcoded strings |
| 2026-03-01 | NSIS installer built | Tauri v2 NSIS bundler with hooks for COM registration. 2.8 MB installer. |
| 2026-03-01 | Static CRT (`/MT`) | COM DLL dynamically linked to vcruntime140.dll, failed on clean machines. `/MT` embeds it. |
| 2026-03-01 | Embedded Python `._pth` | Embedded Python ignores `PYTHONPATH` when `._pth` exists. Must add data dir to `._pth` file directly. |
| 2026-03-01 | NSIS resource path | Tauri v2 installs resources to `$INSTDIR\` root, not `$INSTDIR\resources\`. Hooks must use correct path. |
| 2026-03-01 | Data dir persists | Uninstaller removes app from Program Files but preserves `C:\ProgramData\VoiceLink` (~1.3 GB) for fast reinstalls. |
| 2026-03-01 | `CREATE_NO_WINDOW` | GUI app spawning console subprocesses flashes CMD windows. `0x08000000` creation flag prevents this. |
| | | |

---

*Last updated: 2026-03-02 (Installer working, setup wizard tested on clean machine, server startup fix in progress)*
