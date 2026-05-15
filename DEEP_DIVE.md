# VoiceLink — Technical Deep Dive

> A living document. This is our knowledge base — everything we learn about TTS, COM, audio, companies, models, and system internals goes here. Not a README. A reference manual built through exploration.

**Last updated:** 2026-03-11 (v6 — Qwen3-TTS CUDA graph acceleration via faster-qwen3-tts, ~6x speedup)

---

## Table of Contents

- [1. The Problem Space](#1-the-problem-space)
- [2. Windows Speech Architecture](#2-windows-speech-architecture)
- [3. COM (Component Object Model)](#3-com-component-object-model)
- [4. Neural TTS — How It Works](#4-neural-tts--how-it-works)
- [5. TTS Models — Detailed Survey](#5-tts-models--detailed-survey)
- [6. Companies & Organizations](#6-companies--organizations)
- [7. Audio Engineering](#7-audio-engineering)
- [8. Key Specifications & Formats](#8-key-specifications--formats)
- [9. Related Projects & Prior Art](#9-related-projects--prior-art)
- [10. Architecture Decision](#10-architecture-decision)
- [11. User Experience Design](#11-user-experience-design)
- [12. Glossary](#12-glossary)
- [13. Management GUI (Tauri v2)](#13-management-gui-tauri-v2)
- [14. Installer & Deployment](#14-installer--deployment)
- [15. Qwen3-TTS Integration Plan](#15-qwen3-tts-integration-plan)

---

## 1. The Problem Space

### What Apps Use SAPI?

Any Windows app that calls the system TTS goes through SAPI. Known apps:

| App | How It Uses TTS | Notes |
|-----|----------------|-------|
| **Thorium Reader** | Read Aloud for EPUB/PDF | Electron app, uses Windows SAPI via IPC. This is our primary target. |
| **Microsoft Edge** | Read Aloud feature | Interestingly, Edge can ALSO use cloud Azure voices (not just SAPI). |
| **Windows Narrator** | Full screen reader for accessibility | Uses both SAPI and OneCore voices. |
| **Balabolka** | Dedicated TTS app, reads any text/document | Power-user app, explicitly picks SAPI voices. |
| **Calibre** | E-book reader with TTS plugin | Via plugin, uses SAPI. |
| **NVDA** | Screen reader for visually impaired | Open-source, uses SAPI + eSpeak. |
| **JAWS** | Commercial screen reader | Uses SAPI + its own Eloquence engine. |
| **PowerShell/C#** | `System.Speech.Synthesis.SpeechSynthesizer` | .NET wrapper around SAPI. |
| **VBA/Office** | Macros can call SAPI | `CreateObject("SAPI.SpVoice")`. |

**Key insight:** If we register one SAPI voice, ALL of these apps get upgraded automatically. That's the leverage.

### Why Built-in Voices Sound Bad

Microsoft's desktop SAPI voices (David, Zira) use **concatenative synthesis** — a 2000s-era approach:
1. Record a human saying thousands of phoneme combinations
2. At runtime, stitch together the right phoneme clips
3. Apply basic smoothing at the joins

The result: each individual sound is human, but the stitching creates that characteristic robotic cadence. There's no natural rhythm, no emotion, no prosody variation.

Microsoft's **OneCore voices** (Mark, Heera, etc.) are slightly better — they use a basic neural approach — but they're still nowhere near modern open-source models.

Microsoft's **Azure Neural voices** (used in Edge's cloud Read Aloud) are actually very good, but they require an internet connection and a paid API. Our project brings that quality level to SAPI — locally, free, offline.

---

## 2. Windows Speech Architecture

### SAPI 5 (Speech API version 5)

- **Introduced:** Windows XP, 2001
- **Current version:** SAPI 5.4 (Windows 10/11)
- **What it is:** A COM-based API for both speech recognition (STT) and speech synthesis (TTS)
- **Where it lives:** `%SystemRoot%\System32\Speech\` and `Speech_OneCore\`

### The Two Voice Registries

Windows has **two separate** locations for TTS voices:

```
1. Classic SAPI 5 (Desktop voices):
   HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\
   → David, Zira
   → Any app using System.Speech or SAPI COM can see these

2. OneCore (Modern voices):
   HKLM\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens\
   → David, Zira, Mark, Heera, Ravi, Hemant, Kalpana (on our system)
   → UWP apps and newer APIs can see these
   → NOT always visible to classic SAPI 5 apps
```

**For VoiceLink**, we register in **both** SAPI paths (classic + OneCore) to cover all apps.

### 32-bit vs 64-bit Registry Views (WOW6432Node)

On 64-bit Windows, the registry is split into two views:

| View | Registry Path | Who reads it |
|------|--------------|--------------|
| **64-bit (native)** | `HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\` | 64-bit apps: Edge, Narrator, modern .NET |
| **32-bit (WOW64)** | `HKLM\SOFTWARE\WOW6432Node\Microsoft\Speech\Voices\Tokens\` | 32-bit apps: Adobe Acrobat, older Balabolka, legacy Office |

Windows transparently redirects 32-bit process registry access through `WOW6432Node`. A 32-bit app calling `RegOpenKeyEx(HKLM, "SOFTWARE\Microsoft\Speech\...")` actually reads from `SOFTWARE\WOW6432Node\Microsoft\Speech\...`.

**VoiceLink ships two DLLs:**
- `voicelink_sapi.dll` — 64-bit, registered via `System32\regsvr32.exe`
- `voicelink_sapi_32.dll` — 32-bit, registered via `SysWOW64\regsvr32.exe`

Each `regsvr32` writes to its own registry view automatically. The Tauri GUI's `toggle_voice` command also writes to both views using `KEY_WOW64_64KEY` and `KEY_WOW64_32KEY` flags.

**Counterintuitive naming:** `System32\regsvr32.exe` is 64-bit; `SysWOW64\regsvr32.exe` is 32-bit. This is a legacy Windows naming artifact.

### Voice Token Structure (What We Discovered)

From our exploration on 2026-02-28, each voice token has:

```
HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_EN-US_DAVID_11.0
    (default)  = "Microsoft David Desktop - English (United States)"
    409        = "Microsoft David Desktop - English (United States)"   ← locale-specific name
    CLSID      = {179F3D56-1B0B-42B2-A962-59B7EF59FE1B}              ← COM class ID
    VoicePath  = C:\WINDOWS\Speech_OneCore\Engines\TTS\en-US\M1033David
    LangDataPath = C:\WINDOWS\Speech_OneCore\Engines\TTS\en-US\MSTTSLocEnUS.dat
```

And the CLSID maps to a DLL:

```
HKLM\SOFTWARE\Classes\CLSID\{179F3D56-1B0B-42B2-A962-59B7EF59FE1B}\InprocServer32
    (default)      = C:\Windows\System32\speech_onecore\engines\tts\MSTTSEngine_OneCore.dll
    ThreadingModel = Both
```

**Crucial finding:** ALL OneCore voices share the same CLSID/DLL (`{179F3D56-...}` / `MSTTSEngine_OneCore.dll`). The single DLL loads different voice data based on the `VoicePath`. This is the pattern we'll follow — one VoiceLink DLL, multiple voice tokens.

### SAPI COM Interfaces We Must Implement

| Interface | Purpose | Key Methods |
|-----------|---------|-------------|
| `IUnknown` | Base COM interface (all COM objects) | `QueryInterface()`, `AddRef()`, `Release()` |
| `ISpTTSEngine` | The TTS engine itself | `Speak()`, `GetOutputFormat()` |
| `ISpObjectWithToken` | Receives configuration from the voice token | `SetObjectToken()`, `GetObjectToken()` |
| `IClassFactory` | Creates instances of our engine | `CreateInstance()`, `LockServer()` |

### How `Speak()` Works (The Core Flow)

```cpp
HRESULT Speak(
    DWORD dwSpeakFlags,           // Flags (async, SSML, etc.)
    REFGUID rguidFormatId,        // Requested audio format
    const WAVEFORMATEX *pWaveFormatEx,  // Audio format details
    const SPVTEXTFRAG *pTextFragList,   // Linked list of text fragments
    ISpTTSEngineSite *pOutputSite       // Where to write audio output
);
```

- `pTextFragList`: SAPI breaks the text into fragments. Each fragment has the text and optional SSML attributes (rate, pitch, volume changes).
- `pOutputSite`: This is our audio sink. We call `pOutputSite->Write(audioData, byteCount)` to send audio back. We can call it multiple times for streaming.

---

## 3. COM (Component Object Model)

### What Is COM?

- **Created by:** Microsoft, early 1990s
- **Purpose:** Language-neutral binary standard for component interop
- **Still used in:** DirectX, Shell extensions, Office, SAPI, Windows Runtime (WinRT is built on COM)
- **Key idea:** Define interfaces (vtables) at the binary level, so any language can implement or call them

### Core Concepts

**Interface:** A contract — a list of method signatures. Defined in IDL (Interface Definition Language) or in C++ as abstract classes with pure virtual functions. Every interface inherits from `IUnknown`.

**CLSID (Class ID):** A 128-bit GUID that uniquely identifies a COM class. Example: `{179F3D56-1B0B-42B2-A962-59B7EF59FE1B}`.

**GUID (Globally Unique Identifier):** A 128-bit number guaranteed to be unique across space and time. Generated using algorithms based on MAC address + timestamp or random numbers. Format: `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`.

**vtable (Virtual Method Table):** The binary layout of an interface. It's literally an array of function pointers in memory. This is why COM works across languages — any language that can call a function pointer at a memory offset can use COM.

```
Memory layout of a COM object:
┌──────────────┐
│ pVtable ──────────► ┌──────────────────────┐
├──────────────┤       │ QueryInterface ptr   │  offset 0
│ ref_count    │       │ AddRef ptr           │  offset 4
│ internal_data│       │ Release ptr          │  offset 8
└──────────────┘       │ Speak ptr            │  offset 12
                       │ GetOutputFormat ptr  │  offset 16
                       └──────────────────────┘
```

**IUnknown — The Root of Everything:**

```cpp
interface IUnknown {
    HRESULT QueryInterface(REFIID riid, void **ppvObject);  // "Do you support interface X?"
    ULONG AddRef();                                          // "I'm using you"
    ULONG Release();                                         // "I'm done" (destroy if count hits 0)
};
```

**In-Process Server (InprocServer32):** A COM component packaged as a DLL that runs inside the caller's process. This is what we're building. It's fast because there's no inter-process communication — SAPI loads our DLL directly.

**Registration:** COM components must be registered in the Windows Registry so `CoCreateInstance()` can find them. Our DLL exports `DllRegisterServer()` which writes the registry entries. On 64-bit Windows, we ship both a 64-bit and a 32-bit DLL and register each with its respective `regsvr32` so that both 64-bit apps (Edge, Narrator) and 32-bit apps (Adobe Acrobat) can load our engine.

---

## 4. Neural TTS — How It Works

### The Three-Stage Pipeline

All modern neural TTS follows roughly this pipeline:

```
┌──────────┐     ┌──────────────────┐     ┌─────────────┐
│   TEXT    │ ──► │  ACOUSTIC MODEL  │ ──► │  VOCODER    │ ──► AUDIO
│ Frontend  │     │  (Neural Network) │     │ (Waveform   │
│           │     │                    │     │  Generator) │
│ "Hello"   │     │ Phonemes + Style  │     │ Mel → PCM  │
│   ↓       │     │   ↓                │     │             │
│ /həloʊ/  │     │ Mel Spectrogram    │     │ 24kHz PCM  │
└──────────┘     └──────────────────┘     └─────────────┘
```

### Stage 1: Text Frontend / Linguistic Analysis

Converts raw text to a linguistic representation:

- **Normalization:** "Dr. Smith bought 3 apples for $4.50" → "Doctor Smith bought three apples for four dollars and fifty cents"
- **Grapheme-to-Phoneme (G2P):** "hello" → /həˈloʊ/ (IPA phonemes)
  - English is notoriously hard — "read" has two pronunciations
  - Uses lookup dictionaries (CMUDict) + learned rules for unknown words
- **Prosody prediction:** Where to place stress, pauses, intonation curves

**Kokoro uses Misaki** for this stage (see section 6).

### Stage 2: Acoustic Model

The core neural network. Takes phonemes + speaker embedding → produces mel spectrogram.

**What is a mel spectrogram?**
- A 2D representation of sound: X-axis = time, Y-axis = frequency (mel scale), value = energy
- "Mel scale" maps frequencies to how humans perceive pitch (we're more sensitive to low-frequency differences)
- Typically 80 mel bands, one frame every 10-12ms
- It's essentially a "recipe" for the sound, but not the sound itself

**Architectures:**
- **Tacotron 2** (2017, Google) — The breakthrough. Autoregressive (generates one frame at a time). Slow.
- **FastSpeech 2** (2020, Microsoft) — Non-autoregressive (generates all frames at once). 10-100x faster.
- **VITS** (2021) — End-to-end, combines acoustic model + vocoder. Used by Piper.
- **StyleTTS 2** (2023) — Style-based, excellent prosody. **Used by Kokoro.**
- **Matcha-TTS** (2024) — Flow-matching based, very fast and high quality.

### Stage 3: Vocoder

Converts mel spectrogram → actual audio waveform.

- **WaveNet** (2016, DeepMind) — First neural vocoder. Autoregressive. Very slow.
- **WaveRNN** (2018) — Lighter, faster.
- **HiFi-GAN** (2020) — GAN-based, fast, high quality. Used by many TTS systems.
- **iSTFT-based** (2022+) — Uses inverse Short-Time Fourier Transform. Very fast, good quality. **Used by Kokoro.**

### Voice Embeddings

How one model produces multiple voices:
- Each voice is encoded as a **fixed-size vector** (e.g., 256 dimensions)
- This vector captures: pitch range, speaking rate, breathiness, formant structure, accent
- At inference time, the vector is fed into the acoustic model as a conditioning signal
- The model produces output "in the style of" that voice vector

This is why Kokoro can have many voices with one ~82MB model — each voice is just a small embedding file.

---

## 5. TTS Models — Detailed Survey

### Kokoro (by Hexgrad)

| Property | Details |
|----------|---------|
| **Creator** | Hexgrad (individual/small team) |
| **Architecture** | StyleTTS 2 based acoustic model + iSTFT vocoder |
| **Model size** | ~82MB (v0.19) |
| **Languages** | American English, British English, Japanese, Chinese, Korean, French, Hindi, Italian, Brazilian Portuguese, Spanish |
| **Voices** | ~50+ voice embeddings included |
| **Sample rate** | 24,000 Hz |
| **License** | Apache 2.0 |
| **GitHub** | https://github.com/hexgrad/kokoro |
| **HuggingFace** | hexgrad/Kokoro-82M |
| **Python package** | `pip install kokoro` |
| **Text frontend** | Misaki (also by Hexgrad) |
| **Speed** | ~5-15x realtime on CPU, 50x+ on GPU |
| **Streaming** | Yes — outputs audio in chunks per sentence/clause |
| **Voice cloning** | No (fixed voice set, but can fine-tune) |

**Voice naming convention:** `{lang}{gender}_{name}`
- `a` = American English, `b` = British English, `j` = Japanese
- `f` = Female, `m` = Male

**Test results on our system (2026-02-28):**
- Voices tested: `af_heart`, `af_bella`, `am_adam`, `am_michael`
- Audio samples saved to: `research/audio_samples/`
- Hardware: RTX 4060 Laptop (8GB VRAM), Python 3.11
- Quality assessment: *(to be filled after listening)*
- Latency: *(to be filled)*

---

### Qwen3-TTS (by Alibaba / Qwen Team) — Researched 2026-03-03

| Property | Details |
|----------|---------|
| **Creator** | Alibaba Cloud / Qwen Team |
| **Released** | January 22, 2026 |
| **Architecture** | Discrete multi-codebook language model with Dual-Track hybrid streaming |
| **Tokenizer** | Qwen3-TTS-Tokenizer-12Hz (12.5 Hz, 16-layer multi-codebook, causal ConvNet) |
| **Model sizes** | 0.6B parameters (~1.2 GB) and 1.7B parameters (~3.4 GB) |
| **Languages** | 10: Chinese, English, Japanese, Korean, German, French, Russian, Portuguese, Spanish, Italian |
| **Sample rate** | 24,000 Hz (confirmed) |
| **License** | Apache 2.0 |
| **GitHub** | https://github.com/QwenLM/Qwen3-TTS |
| **HuggingFace** | Qwen/Qwen3-TTS-12Hz-* namespace |
| **Python package** | `pip install faster-qwen3-tts` (CUDA graph accelerated wrapper) |
| **Speed (baseline)** | ~0.19x realtime on RTX 4060 Laptop (unusably slow) |
| **Speed (w/ CUDA graphs)** | ~1.0-1.1x realtime on RTX 4060 Laptop (usable!) |
| **Warmup** | First call ~100s (CUDA graph capture), subsequent calls fast |
| **Voice cloning** | Yes — 3 second audio clip + transcript |
| **Voice design** | Yes (1.7B only) — describe a voice in natural language |
| **Quality** | State-of-the-art, emotion/prosody-aware, instruction-controllable |
| **VRAM needed** | ~2.6 GB (0.6B), ~5-6 GB (1.7B) |
| **Requires** | NVIDIA GPU with CUDA, Python 3.12 (no flash-attn/vLLM needed) |

**Released model variants:**

| Model | Params | Download | Capabilities | Streaming |
|-------|--------|----------|-------------|----------|
| Tokenizer-12Hz | small | ~few 100 MB | Encode/decode audio to tokens. Required by all models. | — |
| 0.6B-CustomVoice | 0.6B | ~1.2 GB | 9 built-in speakers; no instruction control | Yes |
| 0.6B-Base | 0.6B | ~1.2 GB | Voice cloning from 3s audio; fine-tuning base | Yes |
| 1.7B-CustomVoice | ~2B | ~3.4 GB | 9 built-in speakers + instruction-based emotion/tone control | Yes |
| 1.7B-VoiceDesign | ~2B | ~3.4 GB | Create new voices from text descriptions | Yes |
| 1.7B-Base | ~2B | ~3.4 GB | Voice cloning + fine-tuning base | Yes |

**Built-in speakers (9 total, available in all CustomVoice models):**

| Speaker | Description | Native Language |
|---------|-------------|----------------|
| Vivian | Bright, slightly edgy young female | Chinese |
| Serena | Warm, gentle young female | Chinese |
| Uncle_Fu | Seasoned male, low mellow timbre | Chinese |
| Dylan | Youthful Beijing male, clear natural timbre | Chinese (Beijing) |
| Eric | Lively Chengdu male, slightly husky brightness | Chinese (Sichuan) |
| **Ryan** | Dynamic male, strong rhythmic drive | **English** |
| **Aiden** | Sunny American male, clear midrange | **English** |
| Ono_Anna | Playful Japanese female, light nimble timbre | Japanese |
| Sohee | Warm Korean female, rich emotion | Korean |

Only 2 English speakers (both male). All speakers can speak all 10 languages, but quality is best in their native one.

**Key technical features we verified:**
- End-to-end multi-codebook LM architecture (no DiT, no cascading errors from separate stages)
- Dual-Track streaming: first audio packet after 1 character input, 97ms latency
- Instruction-driven control: pass `instruct="Very happy"` or `instruct="Speak angrily"` to shape delivery
- Voice cloning needs only 3 seconds of reference audio + transcript
- Voice design creates voices from descriptions like "Male, 17 years old, tenor range, gaining confidence"
- Fine-tuning supported on the Base models

**CUDA Graph Acceleration (faster-qwen3-tts):**

The baseline `qwen-tts` package is unusably slow: ~0.19x realtime on RTX 4060 Laptop (a 4-second clip takes 22 seconds to generate). The bottleneck is Python overhead between ~500 small CUDA kernel launches per decode step — the GPU spends more time waiting than computing.

We use [`faster-qwen3-tts`](https://github.com/andimarafioti/faster-qwen3-tts) (MIT license) which captures the entire decode step as a CUDA graph and replays it as a single GPU operation. This eliminates all Python overhead during generation.

| Metric | Baseline (qwen-tts) | CUDA Graphs (faster-qwen3-tts) |
|--------|---------------------|-------------------------------|
| Voice clone RTF | 0.19x (22.5s for 4.2s audio) | **1.11x** (5.0s for 5.6s audio) |
| Built-in speaker RTF | ~0.19x | **1.0-1.04x** |
| VRAM (0.6B) | 2.55 GB | 2.60 GB (negligible change) |
| Audio quality | Baseline | **Identical** (same computation) |
| First call | Instant | ~100s (CUDA graph capture, one-time) |
| Subsequent calls | Slow | Fast (~1x realtime) |

No flash-attention, no vLLM, no Triton required. Drop-in replacement with identical output.

Benchmarked on: NVIDIA RTX 4060 Laptop GPU (8.6 GB VRAM), PyTorch 2.10.0+cu128, Windows 11.

**Quality difference between 0.6B and 1.7B:**
- Both have the same 9 speakers and voice cloning capability
- 1.7B adds Voice Design (create voices from text descriptions)
- 1.7B supports the `instruct` parameter for emotion/tone control on CustomVoice
- 1.7B has better prosody and handles edge cases more naturally (3x the parameters)
- 0.6B is "use the fixed voices and clone" — 1.7B is "also design and control"

**Tradeoffs vs Kokoro:**

| | Kokoro | Qwen3-TTS (0.6B) | Qwen3-TTS (1.7B) |
|---|---|---|---|
| Model size | ~82 MB (ONNX) | ~1.2 GB | ~3.4 GB |
| Tokenizer | Misaki (included) | Tokenizer-12Hz (~few 100 MB extra) | Same |
| GPU required | No (CPU is fine) | Yes (CUDA) | Yes (CUDA) |
| VRAM | 0 (CPU) / ~1 GB (GPU) | ~2.6 GB | ~5-6 GB |
| English voices | 11 (6F, 5M) | 2 (both male) | 2 (both male) |
| Voice cloning | No | Yes (3s clip) | Yes (3s clip) |
| Voice design | No | No | Yes |
| Emotion control | No | No | Yes (via instruct) |
| Streaming | Yes (chunked) | Yes (97ms latency) | Yes (97ms latency) |
| Speed on GPU | ~50x realtime | ~1.0x realtime (CUDA graphs) | ~1.0x realtime (CUDA graphs) |
| Speed on CPU | ~5-15x realtime | Not practical | Not practical |
| Languages | 10 | 10 | 10 |
| License | Apache 2.0 | Apache 2.0 | Apache 2.0 |

**Our plan:** Optional feature, gated behind CUDA GPU detection. The Qwen3 option is not shown at all if no compatible GPU exists. Adds voice cloning and voice design to VoiceLink without replacing Kokoro as the default engine. See [Section 15](#15-qwen3-tts-integration-plan) for full integration design.

---

### Piper (by Rhasspy / Michael Hansen)

| Property | Details |
|----------|---------|
| **Creator** | Michael Hansen (Rhasspy project) |
| **Architecture** | VITS (Variational Inference TTS) |
| **Model size** | 20-80MB per voice |
| **Languages** | 30+ languages, 100+ voices |
| **License** | MIT |
| **GitHub** | https://github.com/rhasspy/piper |
| **Speed** | Extremely fast, runs on Raspberry Pi |
| **Quality** | Good but not as natural as Kokoro or Qwen |
| **Use case** | Embedded devices, low-resource environments |

**Why we skipped it:** User tested and didn't like the sound quality. Fair — Piper optimizes for speed over quality.

---

### F5-TTS (by SWivid)

| Property | Details |
|----------|---------|
| **Creator** | SWivid (research team) |
| **Architecture** | Flow-matching based with DiT (Diffusion Transformer) |
| **Key feature** | Zero-shot voice cloning — give it any audio sample |
| **Quality** | Very high, especially for cloned voices |
| **Speed** | Moderate — needs GPU for reasonable speed |
| **License** | CC-BY-NC 4.0 (non-commercial only!) |
| **GitHub** | https://github.com/SWivid/F5-TTS |

**Interesting for VoiceLink because:** Users could clone their favorite audiobook narrator's voice and use it for TTS. Ethical/legal concerns apply.

---

### Parler-TTS (by Hugging Face)

| Property | Details |
|----------|---------|
| **Creator** | Hugging Face team |
| **Architecture** | Based on text description conditioning |
| **Key feature** | Describe the voice: "A warm female voice, speaking slowly with a British accent" |
| **Quality** | Good, improving rapidly |
| **License** | Apache 2.0 |
| **GitHub** | https://github.com/huggingface/parler-tts |

**Interesting because:** Instead of picking from fixed voices, users describe what they want. Novel UX concept.

---

### Coqui / XTTS (by Coqui AI — Defunct)

| Property | Details |
|----------|---------|
| **Creator** | Coqui AI (company shut down Jan 2024) |
| **Architecture** | GPT-based with voice cloning |
| **Key feature** | Multi-language voice cloning from 6-second sample |
| **Status** | Company dead, but model/code is open source |
| **License** | CPML (Coqui Public Model License) — restrictive |
| **GitHub** | https://github.com/coqui-ai/TTS |

**Cautionary note:** Coqui showed that TTS startups are hard to monetize. The tech lives on as open source, though.

---

## 6. Companies & Organizations

### Hexgrad

- **What:** Individual developer / small team behind Kokoro TTS
- **Created:** Kokoro TTS, Misaki (text frontend)
- **Philosophy:** Small, efficient models that punch above their weight
- **Why they matter to us:** Kokoro is our primary voice engine. Apache 2.0 license means we can ship it freely.
- **Misaki:** Their text-to-phoneme library. Handles English (with CMUDict), Japanese, Chinese, etc. Replaces eSpeak for phoneme generation.

### Alibaba / Qwen Team

- **What:** AI research division of Alibaba Cloud (Chinese tech giant)
- **Based in:** Hangzhou, China
- **Created:** Qwen series of LLMs (Qwen, Qwen-2, Qwen-2.5, Qwen-3), Qwen-Audio, **Qwen3-TTS** (released Jan 2026)
- **Why they matter:** Qwen3-TTS is now released and confirmed Apache 2.0. It's the second TTS engine we're integrating into VoiceLink for voice cloning and voice design.
- **License:** Apache 2.0 (confirmed for all Qwen3-TTS models)
- **GitHub:** https://github.com/QwenLM/Qwen3-TTS (8.9k stars, very active)

### Rhasspy / Michael Hansen

- **What:** Open-source voice assistant project
- **Created:** Piper TTS, Wyoming protocol, Rhasspy voice assistant
- **Philosophy:** Voice tech should run locally, on cheap hardware, with no cloud dependency
- **Funded by:** Nabu Casa (Home Assistant)
- **Why they matter:** Piper proved neural TTS can run on a $35 Raspberry Pi. Inspiration for efficiency.

### Microsoft

- **SAPI team:** Built the Speech API (1995-present). Now part of Windows Core.
- **Azure AI Speech:** Cloud-based neural TTS. Excellent quality but requires internet + paid API.
- **Important distinction:** SAPI desktop voices (David, Zira) ≠ Azure Neural voices. The desktop ones are old tech.
- **OneCore:** The middle ground. Better than SAPI desktop, worse than Azure Neural. Introduced with Windows 10.

### Hugging Face

- **What:** AI model hub + research company
- **Based in:** New York / Paris
- **Created:** Transformers library, Diffusers, Parler-TTS, model hub
- **Why they matter:** Most open-source TTS models are hosted on Hugging Face Hub. Kokoro downloads from there. It's the npm/pip of AI models.

### SWivid (F5-TTS)

- **What:** Research team/group
- **Created:** F5-TTS (flow-matching TTS with voice cloning)
- **Why they matter:** Pushed zero-shot voice cloning quality forward significantly
- **Caveat:** Non-commercial license limits our ability to include it in VoiceLink

### Coqui AI (Defunct)

- **What was:** Berlin-based TTS startup, spun out of Mozilla's voice team
- **Rose:** 2021-2023, created XTTS (excellent multi-lingual voice cloning)
- **Fell:** January 2024, company shut down
- **Legacy:** Their TTS library and XTTS model remain open-source on GitHub
- **Lesson:** Pure TTS is hard to monetize as a standalone business

---

## 7. Audio Engineering

### PCM Audio Basics

**PCM (Pulse Code Modulation):** The standard raw digital audio format.

- **Sample:** A single measurement of the audio waveform at a point in time
- **Sample rate:** How many measurements per second (Hz)
  - 8,000 Hz — telephone quality
  - 16,000 Hz — typical for STT/SAPI
  - 22,050 Hz — common for older TTS
  - 24,000 Hz — **Kokoro's output**
  - 44,100 Hz — CD quality
  - 48,000 Hz — professional audio / video
- **Bit depth:** Precision of each sample
  - 16-bit — standard (range: -32768 to 32767)
  - 32-bit float — used internally by neural models (range: -1.0 to 1.0)
- **Channels:** 1 = mono, 2 = stereo. TTS is always mono.

**Calculating data rates:**
```
Bytes per second = sample_rate × (bit_depth / 8) × channels

Kokoro output:    24000 × 2 × 1 = 48,000 bytes/sec = 48 KB/s
SAPI 16kHz 16bit: 16000 × 2 × 1 = 32,000 bytes/sec = 32 KB/s
```

### WAV File Format

```
RIFF header (12 bytes):
  "RIFF" + file_size + "WAVE"

fmt chunk (24 bytes):
  "fmt " + chunk_size + audio_format(1=PCM) + channels + sample_rate
  + byte_rate + block_align + bits_per_sample

data chunk (variable):
  "data" + data_size + [raw PCM samples...]
```

### Resampling (24kHz → 16kHz)

SAPI may request audio at 16kHz or 22kHz. Kokoro outputs at 24kHz. We need to convert.

**Resampling** = changing the sample rate of audio. For downsampling (24k→16k):
1. Apply a low-pass filter (anti-aliasing) to remove frequencies above the new Nyquist (8kHz)
2. Resample by interpolation

Python library: `scipy.signal.resample` or `librosa.resample`. We'll do this in the inference server.

### WAVEFORMATEX (SAPI's Audio Format Struct)

```cpp
typedef struct {
    WORD  wFormatTag;      // 1 = PCM
    WORD  nChannels;       // 1 = mono
    DWORD nSamplesPerSec;  // sample rate
    DWORD nAvgBytesPerSec; // sample_rate * block_align
    WORD  nBlockAlign;     // channels * (bits_per_sample / 8)
    WORD  wBitsPerSample;  // 16
    WORD  cbSize;          // 0 for PCM
} WAVEFORMATEX;
```

Our DLL's `GetOutputFormat()` will fill this struct to tell SAPI what format our audio is in.

---

## 8. Key Specifications & Formats

### SSML (Speech Synthesis Markup Language)

Some apps send SSML instead of plain text:

```xml
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
  <voice name="VoiceLink-Kokoro">
    <prosody rate="slow" pitch="high">
      Once upon a time...
    </prosody>
    <break time="500ms"/>
    There lived a girl named Alice.
  </voice>
</speak>
```

Our engine needs to parse SSML or at minimum strip it to plain text. Kokoro/Misaki has basic SSML support.

### Language Codes (LCID)

| Hex Code | Language |
|----------|----------|
| `0x0409` | en-US (English, United States) |
| `0x0809` | en-GB (English, United Kingdom) |
| `0x0411` | ja-JP (Japanese) |
| `0x0804` | zh-CN (Chinese, Simplified) |

These appear in voice token registration (`409` = en-US for David/Zira).

---

## 9. Related Projects & Prior Art

### Projects That Have Done Something Similar

We researched GitHub on 2026-02-28 for projects that bridge custom/neural TTS into SAPI. Here's what exists:

| Project | Stars | Language | Approach | Status |
|---------|-------|----------|----------|--------|
| **NaturalVoiceSAPIAdapter** | 680 | C++ | Bridges Azure Neural voices to SAPI via COM DLL | Active, mature |
| **windows-text-to-speech** | 11 | Rust | SAPI engine DLL with Piper TTS support | Active |
| **PySpTTSEnginePoC** | 0 | Python | Pure Python COM SAPI engine using comtypes | PoC, inactive |

### NaturalVoiceSAPIAdapter (gexgd0419) — The Gold Standard

**Repo:** https://github.com/gexgd0419/NaturalVoiceSAPIAdapter

The most successful project in this space (680 stars). It makes Azure Neural TTS voices (Microsoft's cloud voices) available to any SAPI 5 app.

**Architecture:**
- C++ COM DLL implementing `ISpTTSEngine`
- Connects to Azure Speech SDK (for local embedded voices) OR REST API (for cloud voices)
- Builds SSML from SAPI `SPVTEXTFRAG` linked list
- Handles: cancel, skip, sentence boundaries, bookmark events, silence compensation
- Output format: `SPSF_24kHz16BitMono` (24kHz, 16-bit, mono)
- Has an installer and a settings GUI window
- Codebase: ~5000 lines of C++

**Key findings from studying their code:**
1. They use 24kHz 16-bit mono — same as Kokoro's native output. No resampling needed.
2. The SSML building from `SPVTEXTFRAG` is non-trivial — handles rate, pitch, volume changes from SAPI.
3. They handle cancellation (`SPVES_ABORT`) by checking `pOutputSite->GetActions()` in a loop.
4. Audio is streamed chunk-by-chunk to `pOutputSite->Write()`.
5. Silence compensation: when switching between sentences, they calculate trailing silence and compensate for network delay.
6. Error handling: all C++ exceptions are caught at the COM boundary and converted to `HRESULT`.

**Key limitation that creates our opportunity:**
- Requires internet connection for cloud voices
- Requires Azure subscription key ($) for non-Edge voices
- Local embedded voices require Microsoft's limited-access SDK license
- **VoiceLink fills this gap: same architecture, but local, free, with open-source models**

### windows-text-to-speech (Lej77) — Rust Approach

**Repo:** https://github.com/Lej77/windows-text-to-speech

A Rust implementation supporting Piper TTS as a SAPI voice.

**Key findings:**
- Rust COM is viable using the `windows` crate, but complex
- They catalogued all Kokoro Rust crates: `sherpa-rs`, `kokoros`/`kokorox`, `kokoro-tts`, `kokoroxide`, `kokoro-tiny`
- Installation via `regsvr32 ./windows_tts_engine.dll`
- Piper requires eSpeak NG data files — dependency management is messy
- Has a comprehensive README with all the SAPI reference links we need:
  - [TTS Engine Vendor Porting Guide (SAPI 5.3)](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ms717037(v=vs.85))
  - [ISpTTSEngine Interface Reference](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ms717235(v=vs.85))
  - [Sample Engines (SAPI 5.3)](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ms720179(v=vs.85))

**Important note from their README:**
> The modern `Windows.Media.SpeechSynthesis.SpeechSynthesizer` API has a remark:
> "Only Microsoft-signed voices installed on the system can be used to generate speech."
>
> So the modern API is locked down. **SAPI 5 (legacy) is the only path for custom voices.**

### PySpTTSEnginePoC (bostjanv) — Python Proof of Concept

**Repo:** https://github.com/bostjanv/PySpTTSEnginePoC

A minimal proof-of-concept showing you CAN implement SAPI TTS in Python.

**How it works:**
- Uses Python `comtypes` library to implement COM interfaces
- Defines the SAPI interfaces in an IDL file (`pysapi.idl`), compiles with MIDL
- Generates Python COM type definitions with `comtypes`
- The "TTS" just plays a pre-recorded WAV file on every `Speak()` call
- Registers the voice via Python (`winreg` module)
- Only ~200 lines of Python

**Key findings:**
- It works, but runs as a COM **local server** (out-of-process), not in-process
- Must run `python sapi_tts_engine.py /regserver` as admin to register
- Python process must be running for the voice to work
- Fragile: if Python crashes, the voice disappears silently
- Good for learning, not for production

**Their register_voice() function is a perfect reference** for the registry entries we need:
```python
paths = [
    f"SOFTWARE\\Microsoft\\Speech\\Voices\\Tokens\\{id}",
    f"SOFTWARE\\WOW6432Node\\Microsoft\\Speech\\Voices\\Tokens\\{id}"
]
# Must register in BOTH paths for 64-bit and 32-bit app compatibility
```

### Other Notable References

- **eSpeak SAPI support:** The original eSpeak had SAPI 5 integration. eSpeak-ng (the modern fork) does NOT — [Issue #7](https://github.com/espeak-ng/espeak-ng/issues/7) has been open since 2015 with useful discussion.
- **Microsoft Speech Platform:** A separate installable runtime (not built into Windows) that provides additional voices. Some are better than David/Zira. Not widely known.
- **sherpa-onnx:** A C++/Rust/Python toolkit by k2-fsa that runs many TTS models (including Kokoro) via ONNX Runtime. Could be useful for a single-binary approach later.

---

## 10. Architecture Decision

### Date: 2026-02-28

### Decision: Hybrid Architecture (C++ COM DLL + Python Inference Server)

After studying all three existing projects and evaluating four possible approaches, we chose the **Hybrid** architecture.

### Options Evaluated

| Approach | Reliability | Speed | Ease of Dev | Model Flexibility | User Setup |
|----------|-----------|-------|-------------|-------------------|------------|
| **A. Pure Python (comtypes COM)** | Poor — fragile, crashes | Slow (out-of-process IPC) | Fast to build | Excellent | Complex (needs Python) |
| **B. Pure C++ (single binary + ONNX)** | Excellent | Fastest | Very slow to build | Poor (recompile to change) | Simple (.dll) |
| **C. Pure Rust** | Good | Fast | Slow (COM is complex in Rust) | Moderate | Simple (.dll) |
| **D. Hybrid: C++ COM DLL + Python server** | Excellent | Fast (~2ms overhead) | Moderate | Excellent | Simple (installer bundles everything) |

### Why Hybrid Wins

**The bottleneck is ALWAYS the model inference (~100-500ms), never the COM layer (~0.1ms) or HTTP (~2ms).** So:

1. **COM layer (C++) must be rock-solid** — it loads into every SAPI app's process. If it crashes, the app crashes. C++ in-process COM is the proven standard (NaturalVoiceSAPIAdapter proves this with 680 stars).

2. **Inference layer (Python) must be flexible** — switching from Kokoro to Qwen-3 should be a config change, not a recompile. Python has the ML ecosystem. GPU management is trivial.

3. **The HTTP/WebSocket bridge between them is negligible** — ~2ms localhost overhead vs 100ms+ inference. And it gives us clean separation: update the server without touching the COM DLL.

### Latency Budget

```
COM call overhead:        ~0.1 ms
HTTP request/response:    ~2 ms
Kokoro inference (GPU):   ~100-300 ms  (for first chunk)
Kokoro inference (CPU):   ~300-800 ms  (for first chunk)
Streaming chunk overhead:  ~1 ms per chunk
────────────────────────────────────
Total time to first audio: ~102-802 ms
Subsequent chunks:         ~10-50 ms each (pipeline effect)
```

For comparison: NaturalVoiceSAPIAdapter with Azure cloud has ~500-2000ms latency (network). We're faster.

### The Architecture

```
                          IN-PROCESS (fast, reliable)          LOCALHOST (flexible)
                         ┌──────────────────────────┐        ┌─────────────────────┐
 Thorium Reader  ─SAPI─► │  voicelink.dll (C++)     │ ─HTTP─►│ Python server       │
                         │                          │        │                     │
                         │  • ISpTTSEngine          │◄─PCM──│  • Kokoro / Qwen-3  │
                         │  • Parses SPVTEXTFRAG    │ stream │  • GPU/CPU auto     │
                         │  • Streams to SAPI sink  │        │  • Model hot-swap   │
                         │  • Cancel/skip handling  │        │  • Health endpoint  │
                         └──────────────────────────┘        └─────────────────────┘
                          Stable, rarely changes              Updated frequently
                          ~1000 lines C++                     Python + FastAPI
```

### Communication Protocol

```
DLL → Server:  POST http://localhost:7860/v1/tts
               Body: { "text": "...", "voice": "af_heart", "format": "pcm_24k_16bit" }
               Response: streaming binary PCM audio (chunked transfer encoding)

DLL → Server:  GET http://localhost:7860/v1/health
               Response: { "status": "ok", "model": "kokoro", "gpu": true }

DLL → Server:  GET http://localhost:7860/v1/voices
               Response: [{ "id": "af_heart", "name": "Heart", "lang": "en-US", ... }]
```

---

## 11. User Experience Design

### Design Principle: A 10th-Grader Must Be Able to Set It Up

No terminal. No Python. No registry editing. No `pip install`. Just a `.exe`.

### Installation Flow

```
1. User downloads VoiceLink-Setup.exe from GitHub Releases (~150MB)
   (Includes: COM DLL + embedded Python + Kokoro model + tray app)

2. Double-click → Windows installer (NSIS or WiX)
   ┌─────────────────────────────────────────┐
   │  Welcome to VoiceLink Setup             │
   │                                         │
   │  VoiceLink adds AI-powered voices to    │
   │  any Windows app that supports          │
   │  text-to-speech.                        │
   │                                         │
   │  [Install]  [Advanced Options]          │
   └─────────────────────────────────────────┘

3. Installer does (behind the scenes):
   a. Copies files to C:\Program Files\VoiceLink\
   b. Extracts embedded Python runtime (not installed system-wide)
   c. Registers voicelink.dll via regsvr32 (creates SAPI voice tokens)
   d. Installs VoiceLink Server as a Windows Service
   e. Creates system tray app shortcut in Startup
   f. Downloads Kokoro model if not bundled (~82MB, with progress bar)

4. Settings window opens automatically:
   ┌─────────────────────────────────────────┐
   │  VoiceLink Settings                     │
   │                                         │
   │  ✅ Server running (GPU: RTX 4060)      │
   │                                         │
   │  Voices:                                │
   │  ☑ Kokoro - Heart (Female, warm)        │
   │  ☑ Kokoro - Adam (Male, natural)        │
   │  ☐ Kokoro - Bella (Female, clear)       │
   │  ☐ Kokoro - Michael (Male, deep)        │
   │                                         │
   │  [▶ Test Voice]  [⚙ Advanced]           │
   │                                         │
   │  Models:                                │
   │  ● Kokoro (82MB) — Installed ✓          │
   │  ○ Qwen-3 TTS (2GB) — [Download]       │
   │                                         │
   │  [Save]  [Close to tray]                │
   └─────────────────────────────────────────┘

5. User opens Thorium Reader → Settings → Read Aloud → Voice:
   Now sees "VoiceLink - Heart" alongside "Microsoft David"
   Selects it → clicks Read Aloud → hears AI narration
```

### System Tray Behavior

```
🔗 (tray icon, green = running, red = error, yellow = loading)

Right-click menu:
  ├─ ✅ Server Running
  ├─ ─────────────
  ├─ Open Settings
  ├─ Test Voice
  ├─ ─────────────
  ├─ Start Server
  ├─ Stop Server
  ├─ ─────────────
  └─ Quit VoiceLink
```

### Fallback Behavior

When things go wrong, VoiceLink should fail gracefully:

| Scenario | Behavior |
|----------|----------|
| Server not running | DLL returns `SPERR_UNINITIALIZED`. SAPI falls back to next voice or shows error. |
| Server overloaded | DLL has 5-second timeout. Returns empty audio rather than hanging. |
| Model not downloaded | Server returns HTTP 503. DLL falls back gracefully. |
| GPU not available | Server auto-falls back to CPU. Slower but works. |
| VoiceLink uninstalled | Installer runs `regsvr32 /u`, removes registry entries. Clean. |

### File Structure After Installation

```
C:\Program Files\VoiceLink\
├── voicelink.dll              # COM DLL (registered via regsvr32)
├── voicelink_tray.exe         # System tray app / settings GUI
├── uninstall.exe              # Uninstaller
├── python/                    # Embedded Python runtime (~15MB)
│   ├── python.exe
│   ├── python311.dll
│   └── Lib/
├── server/                    # Python inference server
│   ├── server.py
│   ├── config.yaml
│   └── requirements frozen
├── models/                    # TTS models (downloaded)
│   ├── kokoro/
│   │   ├── model.onnx (or .pt)
│   │   └── voices/
│   └── qwen3/ (optional)
└── logs/
    └── voicelink.log
```

### Technology Choices for UX Components

| Component | Technology | Why |
|-----------|-----------|-----|
| Installer | **NSIS** (Nullsoft Scriptable Install System) | Free, lightweight, widely used (VLC, Notepad++ use it). Or **WiX** for MSI. |
| Tray App | **C# WPF** or **Tauri** | WPF: native Windows, small binary. Tauri: web UI, cross-platform potential. |
| Settings GUI | Part of tray app | Keeps it as one process. |
| Server management | Windows Service + named pipe | Tray app monitors service health. |
| Embedded Python | Python embeddable package | Official 15MB zip from python.org. No system-wide install needed. |

---

## 12. Glossary

| Term | Definition |
|------|-----------|
| **SAPI** | Speech Application Programming Interface. Microsoft's COM-based speech API. |
| **COM** | Component Object Model. Binary interop standard for Windows components. |
| **CLSID** | Class Identifier. 128-bit GUID uniquely identifying a COM class. |
| **GUID** | Globally Unique Identifier. 128-bit number. |
| **vtable** | Virtual method table. Array of function pointers that defines a COM interface in memory. |
| **IUnknown** | Root COM interface. QueryInterface + AddRef + Release. |
| **ISpTTSEngine** | SAPI interface for TTS engines. Speak + GetOutputFormat. |
| **InprocServer32** | Registry key indicating a COM DLL that loads in-process. |
| **PCM** | Pulse Code Modulation. Raw digital audio format. |
| **Mel spectrogram** | 2D time-frequency representation of audio using the mel (perceptual) scale. |
| **Vocoder** | Converts mel spectrograms to audio waveforms. |
| **Phoneme** | Smallest unit of speech sound. /h/ /ə/ /l/ /oʊ/ = "hello". |
| **G2P** | Grapheme-to-Phoneme. Converting written text to pronunciation. |
| **Prosody** | Rhythm, stress, and intonation of speech. |
| **ONNX** | Open Neural Network Exchange. Portable model format. |
| **StyleTTS 2** | Architecture used by Kokoro. Style-based TTS with excellent prosody. |
| **iSTFT** | Inverse Short-Time Fourier Transform. Fast vocoder method. |
| **SSML** | Speech Synthesis Markup Language. XML format for TTS markup. |
| **OneCore** | Microsoft's newer speech subsystem (Windows 10+). Separate from SAPI 5. |
| **WAVEFORMATEX** | Windows struct describing an audio format (sample rate, bit depth, etc.). |
| **HuggingFace Hub** | Repository for AI models. Where Kokoro downloads its weights from. |
| **Voice embedding** | Small vector encoding a speaker's voice characteristics. |
| **Zero-shot voice cloning** | Mimicking any voice from a short audio sample without fine-tuning. |
| **comtypes** | Python library for COM interop. Can implement COM servers in Python. |
| **regsvr32** | Windows utility to register/unregister COM DLLs. Calls `DllRegisterServer()`. |
| **In-process server** | COM DLL loaded into the caller's process (fast). Registered under `InprocServer32`. |
| **Local server** | COM EXE running as separate process (slower, IPC overhead). Registered under `LocalServer32`. |
| **NSIS** | Nullsoft Scriptable Install System. Free installer builder (used by VLC, Notepad++). |
| **Embedded Python** | Official Python distribution as a standalone zip (~15MB). No system install needed. |
| **FastAPI** | Python web framework for building APIs. Supports async, WebSocket, streaming. |
| **Tauri** | Framework for building desktop apps with Rust backend + web frontend. Uses system WebView2 on Windows. ~5MB binary vs. Electron's ~100MB. |
| **WebView2** | Microsoft Edge-based web rendering engine. Pre-installed on Windows 10/11. Used by Tauri for the frontend UI. |
| **invoke()** | Tauri's IPC mechanism. Frontend calls `invoke("command_name", args)` to run Rust functions. |
| **winreg** | Rust crate for Windows registry access. Used to read/write SAPI voice tokens. |
| **AudioContext** | Web Audio API interface. Used in the frontend to play PCM audio bytes from voice previews. |
| **sherpa-onnx** | C++ toolkit for running TTS/STT models via ONNX Runtime. Supports Kokoro. |
| **SPVTEXTFRAG** | SAPI struct — linked list of text fragments passed to `Speak()`. Contains text + SSML attributes. |
| **SPVES_ABORT** | SAPI action flag. When set, the engine should stop speaking immediately. |
| **ISpTTSEngineSite** | SAPI interface passed to `Speak()`. Used to write audio back (`Write()`) and check for cancel (`GetActions()`). |

---

*This document grows as we learn. Every experiment, every discovery gets added here.*

---

## 13. Management GUI (Tauri v2)

### Why Tauri?

We evaluated several options for the desktop GUI:

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **C# WPF** | Native Windows, small binary | C# adds another language to the stack, no cross-platform | Good but adds complexity |
| **Electron** | Easy web UI, huge ecosystem | 100MB+ binary, memory hog | Too heavy |
| **Tauri v2** | Rust backend, web frontend, ~5MB binary, uses system WebView2 | Smaller ecosystem than Electron | **Chosen** — Rust matches our systems-level work, tiny binary |
| **Python + tkinter** | Same language as server | Ugly UIs, hard to make modern dark theme | No |

Tauri v2 was the clear winner: the Rust backend gives us direct `winreg` access for registry operations (no shelling out to `reg.exe`), `reqwest` for HTTP calls to the inference server, and the web frontend lets us build a polished dark-theme UI with standard HTML/CSS/TypeScript.

### Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│  Tauri App (voicelink-gui)                                      │
│                                                                 │
│  ┌─────────────────────────────┐  ┌─────────────────────────────┐  │
│  │  Frontend (WebView2)         │  │  Rust Backend               │  │
│  │                             │  │                             │  │
│  │  index.html                  │  │  lib.rs                      │  │
│  │  main.ts (Vite + TypeScript) │  │  ─ get_server_status()       │  │
│  │  styles.css (dark theme)     │  │  ─ get_sapi_status()         │  │
│  │                             │  │  ─ get_voices()              │  │
│  │  invoke("get_voices")  ────┼──┾  ─ rename_voice()            │  │
│  │  invoke("toggle_voice") ───┼──┾  ─ preview_voice()           │  │
│  │  invoke("preview_voice") ──┼──┾  ─ toggle_voice()            │  │
│  │                             │  │  ─ get_registered_voice_ids()│  │
│  └─────────────────────────────┘  └─────────────────────────────┘  │
│         │ PCM bytes (WebAudio)             │ winreg + reqwest  │
└─────────┼─────────────────────────────────┼──────────────────┘
          │                                 │
          v                                 v
   ┌─────────────────┐          ┌─────────────────────┐
   │  Speakers        │          │  Windows Registry     │
   │  (AudioContext)  │          │  Speech\Voices\Tokens  │
   └─────────────────┘          │  Speech_OneCore\...    │
                                 └─────────────────────┘
```

### Voice Toggle Design

The toggle feature lets users enable/disable individual voices in SAPI without uninstalling anything. This is important because:
- Users may not want all 11 voices cluttering their voice dropdown in Thorium/Edge
- Different workflows may need different voice subsets
- Disabling is reversible — no data loss

**How it works:**

| Action | What happens in the registry |
|--------|-----------------------------|
| **Enable voice** | Creates `VoiceLink_{id}` token under both `Speech\Voices\Tokens\` and `Speech_OneCore\Voices\Tokens\` with full structure: CLSID, VoiceLinkVoiceId, VoiceLinkServerPort, and Attributes subkey (Name, Gender, Language LCID, Age, Vendor) |
| **Disable voice** | Deletes the `VoiceLink_{id}` token and all subkeys from both registries |

**Language/gender inference from voice ID:**
```
Voice ID prefix → Language + Gender
  af_*  → en-US (409), Female      bf_*  → en-GB (809), Female
  am_*  → en-US (409), Male        bm_*  → en-GB (809), Male
```

**Admin privileges required:** Writing to `HKLM` needs elevation. The GUI must run as administrator (same requirement as `regsvr32` for the COM DLL).

### Audio Preview Pipeline

When the user clicks "Test" on a voice card:

```
1. main.ts reads text from Quick Test textarea (or uses fallback)
2. invoke("preview_voice", { voiceId, text })
3. Rust: HTTP POST to localhost:7860/v1/tts with {text, voice_id}
4. Server: Kokoro generates 24kHz 16-bit mono PCM
5. Rust: returns Vec<u8> of PCM bytes to frontend
6. main.ts: PCM bytes → Int16Array → Float32Array (divide by 32768)
7. AudioContext.createBuffer(1, length, 24000) → play through speakers
```

### File Structure

```
gui/
├── index.html              # Single-page app: sidebar + 3 pages + modal overlay
├── src/
│   ├── main.ts              # Navigation, status polling, voice management, audio playback
│   └── styles.css           # Dark theme, cards, toggle switches, modal, scrollbar
├── src-tauri/
│   ├── src/
│   │   └── lib.rs           # Tauri commands + tray icon setup
│   ├── Cargo.toml           # Rust deps: tauri, reqwest, winreg, tokio, serde
│   ├── tauri.conf.json      # App config: window size, tray, identifier
│   └── icons/               # App icons (PNG, ICO)
├── package.json             # npm scripts: dev, build
└── vite.config.ts           # Vite config for frontend bundling
```

### Key CSS Lessons Learned

1. **Scroll fix:** `body { min-height: 100vh; display: flex; overflow: hidden; }` prevents child `overflow-y: auto` from working because `min-height` lets body grow beyond viewport. Fix: use `height: 100vh` to constrain it.

2. **Modal in flex body:** A `position: fixed` overlay inside a `display: flex` body can still participate in flex layout in some WebView2 edge cases. Fix: explicit `width: 100vw; height: 100vh; top: 0; left: 0` plus `display: none !important` for the hidden state.

3. **Browser `prompt()` in Tauri:** Shows "localhost:1420 says" which looks unprofessional. Always use custom in-app modals for user input.

---

## 14. Installer & Deployment

### Technology Choice

Tauri v2's built-in NSIS bundler was selected over standalone WiX/MSIX because:
- Zero extra tooling — `npx tauri build` produces the NSIS installer directly
- Hook system (`installerHooks` in `tauri.conf.json`) lets us run custom NSIS macros at install/uninstall time
- Resources (`resources` in `tauri.conf.json`) automatically bundles files into the installer
- Produces a single `.exe` (~2.8 MB) that extracts and installs everything

### Installer Architecture

```
VoiceLink_0.1.0_x64-setup.exe (NSIS)
│
├── PREINSTALL hook
│   └── Reads QuietUninstallString from registry
│       └── Runs previous uninstaller silently → clean upgrade
│
├── File extraction → C:\Program Files\VoiceLink\
│   ├── voicelink-gui.exe          (Tauri app, ~13 MB)
│   ├── voicelink_sapi.dll         (COM DLL, ~283 KB, static CRT)
│   ├── uninstall.exe              (NSIS uninstaller)
│   └── server/                    (TTS server source files)
│       ├── main.py, config.py, __init__.py
│       ├── models/ (base.py, kokoro_model.py)
│       ├── routers/ (tts.py)
│       └── requirements.txt
│
├── POSTINSTALL hook
│   └── regsvr32 /s "$INSTDIR\voicelink_sapi.dll"
│       └── Registers CLSID + 11 voice tokens in SAPI registry
│
└── First-run → setup wizard in Tauri app handles the rest
```

### Setup Wizard (First-Run Experience)

The management app includes a 5-step setup wizard that runs on first launch:

```
Step 1: Python Runtime
  └── Download python-3.11.9-embed-amd64.zip (~15 MB) from python.org
  └── Extract to C:\ProgramData\VoiceLink\python\
  └── Modify ._pth file: uncomment "import site", add data dir path

Step 2: Python Packages
  └── Download get-pip.py, install pip
  └── pip install -r requirements.txt (FastAPI, Kokoro, PyTorch, etc.)
  └── ~900 MB of packages — takes 5-10 minutes
  └── Live pip output streamed line-by-line to frontend

Step 3: TTS Server
  └── Copy server/ from install dir to C:\ProgramData\VoiceLink\server/

Step 4: Voice Model
  └── Download kokoro-v1.0.onnx (~310 MB) from GitHub releases
  └── Download voices-v1.0.bin (~27 MB)
  └── Progress bar shows download percentage

Step 5: Start Server
  └── Launch: python -m server.main (with PYTHONPATH set)
  └── Wait for health endpoint (localhost:7860/v1/health)
  └── Mark setup complete
```

### Deployment Lessons Learned

These were discovered by testing on a clean Windows 11 laptop with no developer tools:

| Issue | Root Cause | Fix |
|-------|-----------|-----|
| regsvr32 error 3 | DLL dynamically linked to `vcruntime140.dll`, missing on clean machines | Compile with `/MT` (static CRT) — DLL becomes self-contained |
| regsvr32 error 3 (still) | NSIS hooks referenced `$INSTDIR\resources\voicelink_sapi.dll` but Tauri puts resources in `$INSTDIR\` root | Remove `resources\` prefix from all hook paths |
| "No module named server" | Embedded Python's `._pth` file restricts `sys.path`; ignores `PYTHONPATH` env var entirely | Add data directory to `._pth` file during `setup_enable_pip` |
| Blank CMD windows | GUI app spawning console subprocesses (`python.exe`, `pip`) creates visible console | `CREATE_NO_WINDOW` (0x08000000) creation flag on all `Command::new()` calls |
| Progress bar stuck at 0% | `setup_run_command` used `.output()` which blocks until process exits | Rewrote with `tokio::spawn` + `AsyncBufReadExt` to stream stdout/stderr line-by-line |
| 0-byte model ghost file | Download creates file immediately, interruption leaves empty file; `.exists()` returns true | Check `file.metadata().len() > 1000`, delete partial files on error |
| Multiple installs stacking | No check for previous installation before installing new version | `NSIS_HOOK_PREINSTALL` reads `QuietUninstallString` from registry, runs previous uninstaller silently |

### COM DLL Dependencies (After `/MT` Fix)

```
dumpbin /dependents voicelink_sapi.dll:
  WINHTTP.dll      ← HTTP client for inference server
  ole32.dll        ← COM infrastructure
  ADVAPI32.dll     ← Registry access
  KERNEL32.dll     ← Base Windows API
```

No `vcruntime140.dll`, no `ucrtbase.dll`, no `msvcp*.dll`. Fully self-contained.

### Data Directory Structure

```
C:\ProgramData\VoiceLink\          (~1.3 GB total)
├── config.json                     (data_dir setting)
├── python/                         (~950 MB)
│   ├── python.exe                  (embedded Python 3.11)
│   ├── python311.dll
│   ├── python311._pth              (modified: import site + data dir)
│   ├── Lib/site-packages/          (pip packages: torch, kokoro, fastapi, etc.)
│   └── Scripts/                    (pip, uvicorn, etc.)
├── models/                         (~337 MB)
│   ├── kokoro-v1.0.onnx            (310 MB ONNX model)
│   └── voices-v1.0.bin             (27 MB voice embeddings)
└── server/                         (~30 KB, copied from install dir)
    ├── main.py, config.py, __init__.py
    ├── models/
    └── routers/
```

The data directory is **preserved across uninstall/reinstall** so users don't re-download 1.3 GB on upgrades. Only `C:\Program Files\VoiceLink\` is removed by the uninstaller.

---

## 15. Qwen3-TTS Integration Plan

> Decided 2026-03-03 after researching Qwen3-TTS in depth. This section captures the full design for adding Qwen3-TTS as an optional feature alongside Kokoro.

### Design Principles

1. **Kokoro is the default.** It works on any machine (CPU or GPU), has 11 English voices, and is already proven.
2. **Qwen3 is optional functionality.** It adds voice cloning and voice design. Not "advanced mode" — just more features.
3. **GPU gated, not toggle-gated.** If no CUDA GPU is detected, the Qwen3 option does not appear in settings at all. No toggle to flip back, no error message — just not shown.
4. **Separate download.** Qwen3 models are not part of the initial setup wizard. The user explicitly opts in and downloads them from within the app.
5. **Lazy loaded.** Qwen3 model loads into VRAM only when a Qwen3 voice is actually selected for speech. Unloads after idle timeout to free the GPU.

### GPU Detection

Before showing any Qwen3 UI, the backend must verify:

```
1. NVIDIA GPU present (check nvidia-smi or pynvml)
2. CUDA available (torch.cuda.is_available())
3. Enough VRAM (>= 2 GB free for 0.6B, >= 5 GB free for 1.7B)
```

If any check fails, the Qwen3 section is hidden from the UI entirely. The check runs at app startup and is cached.

### Model Tier Selection

When the user enables Qwen3, they pick a tier:

| Tier | Models Downloaded | Total Size | Capabilities | Min VRAM |
|------|------------------|-----------|-------------|----------|
| **Standard** (0.6B) | Tokenizer-12Hz + 0.6B-CustomVoice + 0.6B-Base | ~1.5 GB | 9 built-in voices + voice cloning | ~2 GB |
| **Full** (1.7B) | Tokenizer-12Hz + 1.7B-CustomVoice + 1.7B-VoiceDesign + 1.7B-Base | ~10 GB | Everything above + voice design + emotion control | ~5-6 GB |

Default recommendation based on detected VRAM.

### UI Changes

**Settings page:**
- New "Qwen3 TTS" section (only visible if CUDA GPU detected)
- Toggle to enable/disable Qwen3
- Model tier selector (Standard / Full)
- Download progress bar (shows during initial model download)
- Status indicator: "Not installed" / "Downloading..." / "Ready" / "Disabled"

**New "Voice Studio" nav section (only visible when Qwen3 is enabled and downloaded):**
- **Clone a Voice** tab: Upload or record 3s audio clip + transcript → preview → save to library
- **Design a Voice** tab (1.7B only): Type a description → generate → preview → save to library
- **Qwen3 Voices** tab: The 9 built-in Qwen3 speakers, enable/disable for SAPI

**Voice Manager:**
- Shows all voices unified: Kokoro voices + Qwen3 built-in voices + user-created Qwen3 voices
- Each voice has a source badge: `Kokoro`, `Qwen3`, `Qwen3 (Cloned)`, `Qwen3 (Designed)`
- Enable/disable, rename — works the same regardless of source

### Server Architecture

Extend the existing FastAPI server rather than running a second process:

```
/v1/tts                ← Kokoro (existing, unchanged)
/v1/qwen3/tts          ← Qwen3 synthesis for registered voices
/v1/qwen3/clone        ← Voice Studio: create a cloned voice
/v1/qwen3/design       ← Voice Studio: create a designed voice
/v1/qwen3/speakers     ← List built-in Qwen3 speakers
```

One server, one port, one process to manage, one watchdog.

**Lazy loading strategy:**
- Qwen3 model loads on first `/v1/qwen3/*` request
- Idle timeout: unload after 5 minutes of no Qwen3 requests
- This keeps Kokoro always fast and GPU memory free when Qwen3 isn't in use

### COM DLL Routing

The COM DLL already reads voice tokens from the registry. For Qwen3 voices, we add a `Model` registry field:

```
HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\VoiceLink_af_heart
    CLSID   = {our-clsid}
    Model   = kokoro          ← hits /v1/tts (or absent for backward compat)

HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\VoiceLink_Qwen3_Ryan
    CLSID   = {our-clsid}
    Model   = qwen3            ← hits /v1/qwen3/tts
    Speaker = Ryan

HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\VoiceLink_MyClonedVoice
    CLSID   = {our-clsid}
    Model   = qwen3            ← hits /v1/qwen3/tts
    VoiceProfile = my_narrator ← points to saved clone profile
```

The DLL reads `Model` and routes to the correct endpoint. If `Model` is missing or `kokoro`, use `/v1/tts`. If `qwen3`, use `/v1/qwen3/tts`.

### Voice Profile Storage

User-created voices (cloned or designed) are saved as self-contained folders:

```
C:\ProgramData\VoiceLink\voices\
├── my_narrator\
│   ├── meta.json              (name, source, creation date, model tier)
│   ├── ref_audio.wav          (3s reference clip, for cloned voices)
│   ├── prompt.bin             (precomputed voice clone prompt, for reuse)
│   └── description.txt        (text description, for designed voices)
├── warm_female\
│   ├── meta.json
│   ├── ref_audio.wav          (generated by VoiceDesign, then used as clone ref)
│   ├── prompt.bin
│   └── description.txt
```

This makes voices portable — zip and share.

### Fallback Behavior

- If the user selects a Qwen3 voice but the GPU is busy (another app using VRAM), the server returns an error
- The COM DLL retry logic (already implemented) catches this, returns silence (S_OK)
- The user hears nothing rather than a crash — same graceful degradation pattern
- Kokoro voices always work regardless of GPU state

### Config Changes

```json
{
    "data_dir": "C:\\ProgramData\\VoiceLink",
    "server_port": 7860,
    "auto_start": true,
    "qwen3_enabled": false,
    "qwen3_model_tier": "standard",
    "qwen3_installed": false
}
```

### Download Sizes (What the User Needs)

| Component | Already installed | Additional for Qwen3 Standard | Additional for Qwen3 Full |
|-----------|------------------|-------------------------------|---------------------------|
| Python 3.11 + base deps | ~950 MB | 0 | 0 |
| Kokoro model | ~337 MB | 0 | 0 |
| `faster-qwen3-tts` pip package | 0 | ~few 100 MB (includes torch CUDA deps) | Same |
| Tokenizer-12Hz | 0 | ~few 100 MB | ~few 100 MB |
| 0.6B models | 0 | ~2.4 GB (CustomVoice + Base) | 0 |
| 1.7B models | 0 | 0 | ~10 GB (CustomVoice + VoiceDesign + Base) |
| **Total new download** | **0** | **~3 GB** | **~10 GB** |

### Implementation Order

1. GPU detection command in Rust backend (`check_gpu`)
2. Settings UI: Qwen3 section (conditional on GPU), tier picker, download trigger
3. Qwen3 download/install flow (separate from main setup)
4. Server endpoints: `/v1/qwen3/tts`, `/v1/qwen3/clone`, `/v1/qwen3/design`
5. Lazy loading in server (load on first request, unload on idle)
6. Voice Studio UI: Clone tab, Design tab
7. Voice profile storage + registry integration
8. COM DLL: read `Model` field, route to correct endpoint
9. Unified Voice Manager with source badges
