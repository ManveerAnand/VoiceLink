# VoiceLink — Tasks and Research Tracker

> This document tracks every phase, task, and learning objective. Each task links understanding (WHY/HOW) with implementation (BUILD).

---

## Phase 0: Research and Deep Understanding
**Goal:** Before writing a single line of production code, understand every layer of the system deeply.

### 0.1 — Understand Windows SAPI (Speech API)
> *What is SAPI? How does Windows discover and use voices? What interfaces must we implement?*

- [ ] **Research: What is SAPI 5?**
  - Read Microsoft SAPI 5.4 documentation
  - Understand the difference between SAPI 4, SAPI 5, and OneCore voices
  - Document: How does an app like Thorium Reader find available voices?
  - Learning: Registry keys involved (`HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\`)

- [ ] **Research: The ISpTTSEngine Interface**
  - What methods must a TTS engine implement?
  - `Speak()`, `GetOutputFormat()`, `SetObjectToken()` — what does each do?
  - How does SAPI pass text to the engine? (plain text vs SSML)
  - How does the engine return audio? (`ISpTTSEngineSite::Write()`)

- [ ] **Research: COM (Component Object Model) Fundamentals**
  - What is COM? Why does Windows use it for plugins?
  - IUnknown, IClassFactory, reference counting
  - GUIDs, CLSIDs, ProgIDs — what are they?
  - How does DllRegisterServer work?
  - How does `CoCreateInstance` find and load our DLL?

- [ ] **Research: Audio Formats in SAPI**
  - What audio formats does SAPI expect? (PCM, sample rates)
  - What is `WAVEFORMATEX`?
  - How does streaming work — can we send audio in chunks?
  - Latency requirements — how fast must first audio arrive?

- [ ] **Experiment: List all SAPI voices on my system**
  - Write a small Python/PowerShell script using `win32com` or `pyttsx3`
  - List all installed voices, their properties, registry entries
  - Try speaking with each voice, observe the API calls

- [ ] **Experiment: Inspect Thorium Reader's TTS usage**
  - How does Thorium call SAPI? (Electron + SAPI bridge?)
  - Does it use SSML or plain text?
  - What audio format does it request?
  - Use Process Monitor to trace the COM calls

### 0.2 — Understand Neural TTS Models
> *How do modern TTS models work? What are our options? What are the tradeoffs?*

- [ ] **Research: How Neural TTS Works (High Level)**
  - Text → Tokens → Mel Spectrogram → Waveform
  - What is a vocoder? (HiFi-GAN, etc.)
  - Difference between autoregressive and non-autoregressive models
  - What determines voice quality vs speed?

- [ ] **Survey: Open-Source TTS Models**
  - Evaluate each on: quality, speed, license, ease of use, voice cloning
  - **Kokoro** — Small, fast, Apache 2.0, multiple voices
  - **Piper** — Optimized for Raspberry Pi, very fast, many languages
  - **Qwen-3 TTS** — Large model, highest quality, needs GPU
  - **Coqui/XTTS** — Voice cloning capable
  - **F5-TTS** — Zero-shot voice cloning
  - **Parler-TTS** — Describe the voice you want in natural language
  - Document comparison table with benchmarks

- [ ] **Experiment: Run Kokoro locally**
  - Install dependencies, download model
  - Generate speech from text, measure latency and quality
  - Test streaming output — can we get audio chunk by chunk?

- [ ] **Experiment: Run Piper locally**
  - Same as above
  - Compare quality and speed to Kokoro

- [ ] **Research: ONNX Runtime for TTS**
  - What is ONNX? Why use it?
  - Can we convert models to ONNX for faster CPU inference?
  - DirectML vs CUDA vs CPU backends on Windows

### 0.3 — Understand the Audio Pipeline
> *How does audio flow from a TTS model to the user's speakers through SAPI?*

- [ ] **Research: PCM Audio Basics**
  - Sample rate, bit depth, channels
  - How to calculate buffer sizes
  - WAV file format structure

- [ ] **Research: Audio Streaming**
  - Chunked audio delivery vs full buffer
  - Ring buffers and double buffering
  - Latency vs quality tradeoffs

- [ ] **Research: SAPI Audio Sink**
  - How does `ISpTTSEngineSite::Write()` work?
  - Can we send partial audio? How often?
  - What happens if audio arrives too slowly?

### 0.4 — Study Existing Projects
> *Who has done something similar? What can we learn from them?*

- [ ] **Study: sapi-tts-bridge (if exists)**
  - Search GitHub for SAPI TTS bridge projects
  - Read their code, understand their approach
  - What problems did they encounter?

- [ ] **Study: piper-windows-sapi**
  - There may be a Piper SAPI integration — find and study it
  - How did they register the COM component?
  - What shortcuts did they take?

- [ ] **Study: Other SAPI COM examples**
  - Microsoft SDK samples for TTS engines
  - Any C++ SAPI engine tutorials

---

## Phase 1: TTS Inference Server
**Goal:** Build a local server that takes text and returns high-quality streaming audio.

### 1.1 — Server Foundation
- [ ] Set up Python project with FastAPI
- [ ] Define API contract (`POST /tts` with text, voice, format params)
- [ ] WebSocket endpoint for streaming audio
- [ ] Health check endpoint
- [ ] Configuration system (model selection, GPU/CPU, port)

### 1.2 — Model Integration
- [ ] Integrate Kokoro as first model
- [ ] Abstract model interface (so we can swap models)
- [ ] Integrate Piper as second model
- [ ] Model download and management system

### 1.3 — Streaming and Performance
- [ ] Implement chunked audio streaming
- [ ] Benchmark: time to first byte, total latency, throughput
- [ ] GPU vs CPU performance comparison
- [ ] Memory profiling and optimization

### 1.4 — Testing
- [ ] Unit tests for API contract
- [ ] Integration tests with actual models
- [ ] Stress tests (concurrent requests)
- [ ] Audio quality validation

---

## Phase 2: SAPI COM Bridge
**Goal:** Build a Windows COM DLL that registers as a SAPI voice and proxies to our server.

### 2.1 — COM DLL Skeleton
- [ ] Choose language: C++ vs Rust (research tradeoffs)
- [ ] Implement `IUnknown` and `IClassFactory`
- [ ] Implement `DllRegisterServer` / `DllUnregisterServer`
- [ ] Test: DLL registers and shows up in voice list

### 2.2 — SAPI Engine Implementation
- [ ] Implement `ISpTTSEngine::Speak()`
- [ ] Implement `ISpTTSEngine::GetOutputFormat()`
- [ ] Implement `ISpObjectWithToken::SetObjectToken()`
- [ ] Forward text to inference server via HTTP/WebSocket
- [ ] Stream audio back via `ISpTTSEngineSite::Write()`

### 2.3 — Integration Testing
- [ ] Test with PowerShell `Add-Type -TypeDefinition` SAPI test
- [ ] Test with Thorium Reader
- [ ] Test with Edge Read Aloud
- [ ] Test with Windows Narrator
- [ ] Test with Balabolka

---

## Phase 3: System Integration and Installer
**Goal:** Make it easy for anyone to install and use VoiceLink.

### 3.1 — Installer
- [ ] Research installer technologies (WiX, NSIS, MSIX)
- [ ] Build installer that registers COM DLL
- [ ] Install inference server as Windows service
- [ ] Include model downloader in first-run experience

### 3.2 — System Tray App
- [ ] System tray icon with status indicator
- [ ] Voice selection and configuration
- [ ] Server start/stop controls
- [ ] Model management (download, delete, update)

### 3.3 — Auto-start and Reliability
- [ ] Server auto-starts on login
- [ ] Graceful fallback if server is down
- [ ] Auto-restart on crash
- [ ] Logging and diagnostics

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
| | | |

---

*Last updated: 2026-02-28*
