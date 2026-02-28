// ============================================================================
// VoiceLink SAPI Bridge — TTS Engine (Implementation)
// ============================================================================
//
// This is where everything comes together. When an app says "speak this text",
// the journey is:
//
//   App → SAPI → VoiceLinkEngine::Speak()
//     → HTTP POST to localhost:7860/v1/tts
//     → Python server runs Kokoro model on GPU
//     → PCM audio streams back over HTTP
//     → We write each chunk to ISpTTSEngineSite::Write()
//     → SAPI plays the audio through the speakers
//
// THREADING NOTES:
//   Our COM class uses ThreadingModel="Both", meaning SAPI can call us
//   from any thread (STA or MTA). Speak() is the only method that does
//   significant work, and it's inherently per-call (no shared mutable
//   state across concurrent Speak calls). The HTTP client creates a new
//   request handle per Speak(), so concurrent calls are safe.
//
//   The one shared mutable thing is m_refCount, which is protected by
//   InterlockedIncrement/Decrement (atomic operations).
// ============================================================================

#include "tts_engine.h"
#include "debug.h"
#include "guids.h"

#include <sapi.h>
#include <sapiddk.h>
// NOTE: We intentionally do NOT include <sphelper.h> here.
// It contains GetVersionExW which is deprecated and triggers C4996.
// We don't need any of its helpers — we use ISpObjectToken methods directly.
#include <objbase.h> // CoTaskMemAlloc
#include <string>
#include <sstream>
#include <cmath>     // powf
#include <algorithm> // std::min, std::max
#include <vector>    // for volume scaling buffer
#include <cstdint>   // int16_t

// ============================================================================
// Constructor / Destructor
// ============================================================================

VoiceLinkEngine::VoiceLinkEngine()
    : m_refCount(1) // Start with ref count of 1 (creator holds a reference)
      ,
      m_pToken(nullptr), m_voiceId("af_heart") // Default voice if token doesn't specify
      ,
      m_serverPort(7860) // Default port
      ,
      m_initialized(false)
{
    // Track how many of our objects exist globally.
    // DllCanUnloadNow checks this — COM won't unload our DLL while objects exist.
    InterlockedIncrement(&g_objectCount);
    VLOG(L"VoiceLinkEngine created (objects: %ld)", g_objectCount);
}

VoiceLinkEngine::~VoiceLinkEngine()
{
    VLOG(L"VoiceLinkEngine destroying");

    // Release the voice token if we have one
    if (m_pToken)
    {
        m_pToken->Release();
        m_pToken = nullptr;
    }

    // Close HTTP connection
    m_httpClient.Close();

    InterlockedDecrement(&g_objectCount);
    VLOG(L"VoiceLinkEngine destroyed (objects: %ld)", g_objectCount);
}

// ============================================================================
// IUnknown Implementation
// ============================================================================

STDMETHODIMP VoiceLinkEngine::QueryInterface(REFIID riid, void **ppv)
{
    // QueryInterface is COM's version of dynamic_cast / instanceof.
    //
    // The caller asks: "Do you support interface X?"
    // We check the requested IID against all interfaces we implement.
    // If yes: set *ppv to the correct vtable pointer and AddRef().
    // If no:  set *ppv to nullptr and return E_NOINTERFACE.
    //
    // IMPORTANT: The pointer we return must point to the correct vtable.
    // For ISpTTSEngine, "this" already points to the right vtable (it's first).
    // For ISpObjectWithToken, the compiler adjusts the pointer automatically
    // via the static_cast. This is why we use static_cast, not reinterpret_cast.

    if (!ppv)
        return E_POINTER;

    *ppv = nullptr;

    if (riid == IID_IUnknown)
    {
        // Convention: return the "primary" interface for IUnknown
        *ppv = static_cast<ISpTTSEngine *>(this);
    }
    else if (riid == IID_ISpTTSEngine)
    {
        *ppv = static_cast<ISpTTSEngine *>(this);
    }
    else if (riid == IID_ISpObjectWithToken)
    {
        *ppv = static_cast<ISpObjectWithToken *>(this);
    }
    else
    {
        return E_NOINTERFACE;
    }

    AddRef();
    return S_OK;
}

STDMETHODIMP_(ULONG)
VoiceLinkEngine::AddRef()
{
    // InterlockedIncrement is an atomic operation.
    // It's equivalent to "++m_refCount" but thread-safe.
    // We need this because multiple threads might AddRef/Release simultaneously.
    return InterlockedIncrement(&m_refCount);
}

STDMETHODIMP_(ULONG)
VoiceLinkEngine::Release()
{
    // InterlockedDecrement atomically decrements and returns the new value.
    // If it hits 0, nobody holds a reference to us anymore → self-destruct.
    //
    // WHY "delete this"? In COM, objects manage their own lifetime.
    // The caller doesn't "new" or "delete" us — they use AddRef/Release.
    // When the last Release() brings the count to 0, we clean up.
    LONG count = InterlockedDecrement(&m_refCount);
    if (count == 0)
    {
        delete this;
    }
    return static_cast<ULONG>(count);
}

// ============================================================================
// ISpObjectWithToken Implementation
// ============================================================================

STDMETHODIMP VoiceLinkEngine::SetObjectToken(ISpObjectToken *pToken)
{
    // SAPI calls this right after creating us, passing in the voice token
    // that identifies which voice we should be.
    //
    // The token lives in the registry at something like:
    //   HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\VoiceLink_af_heart
    //
    // It has attributes we can read:
    //   VoiceLinkVoiceId = "af_heart"
    //   VoiceLinkServerPort = "7860"
    //
    // We read these to know which voice to request from the server.

    VLOG(L"SetObjectToken called");

    if (!pToken)
        return E_INVALIDARG;

    // Release old token if we had one, then store and AddRef the new one
    if (m_pToken)
    {
        m_pToken->Release();
    }
    m_pToken = pToken;
    m_pToken->AddRef();

    // -----------------------------------------------------------------------
    // Read custom attributes from the voice token
    // -----------------------------------------------------------------------

    // Voice ID (e.g., "af_heart", "am_adam")
    std::wstring voiceIdW = ReadTokenAttribute(L"VoiceLinkVoiceId");
    if (!voiceIdW.empty())
    {
        m_voiceId = WideToUtf8(voiceIdW.c_str());
        VLOG(L"Voice ID from token: %s", voiceIdW.c_str());
    }
    else
    {
        VLOG(L"No VoiceLinkVoiceId in token, using default: af_heart");
    }

    // Server port (default: 7860)
    std::wstring portW = ReadTokenAttribute(L"VoiceLinkServerPort");
    if (!portW.empty())
    {
        m_serverPort = static_cast<INTERNET_PORT>(_wtoi(portW.c_str()));
        VLOG(L"Server port from token: %d", m_serverPort);
    }

    // -----------------------------------------------------------------------
    // Initialize the HTTP client
    // -----------------------------------------------------------------------
    HRESULT hr = m_httpClient.Init(L"127.0.0.1", m_serverPort);
    if (FAILED(hr))
    {
        VERR(L"Failed to initialize HTTP client");
        return hr;
    }

    m_initialized = true;
    VLOG(L"VoiceLinkEngine initialized: voice=%hs, port=%d", m_voiceId.c_str(), m_serverPort);
    return S_OK;
}

STDMETHODIMP VoiceLinkEngine::GetObjectToken(ISpObjectToken **ppToken)
{
    // SAPI occasionally asks us which token we're associated with.
    // Simple: return the token that was given to us in SetObjectToken.

    if (!ppToken)
        return E_POINTER;

    *ppToken = m_pToken;
    if (m_pToken)
    {
        m_pToken->AddRef(); // Caller gets a reference, must Release() it
    }
    return S_OK;
}

// ============================================================================
// ISpTTSEngine::GetOutputFormat
// ============================================================================

STDMETHODIMP VoiceLinkEngine::GetOutputFormat(
    const GUID * /*pTargetFmtId*/,
    const WAVEFORMATEX * /*pTargetWaveFormatEx*/,
    GUID *pDesiredFmtId,
    WAVEFORMATEX **ppCoMemDesiredWaveFormatEx)
{
    // SAPI calls this before Speak() to ask what audio format we'll produce.
    //
    // We ALWAYS produce: 24kHz, 16-bit, mono PCM
    // This matches Kokoro's native output — no resampling, no quality loss.
    //
    // IMPORTANT: We must allocate the WAVEFORMATEX with CoTaskMemAlloc.
    // SAPI owns the memory after we return it and will CoTaskMemFree it.
    // Using "new" or "malloc" here would cause a crash later.
    //
    // WHY CoTaskMemAlloc?
    //   COM has a strict rule: if memory crosses a COM boundary (from us to
    //   SAPI), it must use the COM allocator (CoTaskMemAlloc/CoTaskMemFree).
    //   This ensures both sides agree on HOW to free the memory.
    //   Regular new/delete or malloc/free could use different heaps.

    VLOG(L"GetOutputFormat called");

    if (!pDesiredFmtId || !ppCoMemDesiredWaveFormatEx)
        return E_POINTER;

    // Allocate WAVEFORMATEX using COM's allocator
    auto *pwfx = static_cast<WAVEFORMATEX *>(
        CoTaskMemAlloc(sizeof(WAVEFORMATEX)));

    if (!pwfx)
        return E_OUTOFMEMORY;

    // Fill in 24kHz, 16-bit, mono PCM format
    //
    // WAVEFORMATEX fields:
    //   wFormatTag     = 1 (WAVE_FORMAT_PCM)
    //   nChannels      = 1 (mono)
    //   nSamplesPerSec = 24000 (24kHz)
    //   wBitsPerSample = 16
    //   nBlockAlign    = nChannels * wBitsPerSample / 8 = 2 bytes
    //   nAvgBytesPerSec = nSamplesPerSec * nBlockAlign = 48000 bytes/sec
    //   cbSize         = 0 (no extra data after this struct)
    pwfx->wFormatTag = WAVE_FORMAT_PCM;
    pwfx->nChannels = 1;
    pwfx->nSamplesPerSec = 24000;
    pwfx->wBitsPerSample = 16;
    pwfx->nBlockAlign = pwfx->nChannels * pwfx->wBitsPerSample / 8;   // 2
    pwfx->nAvgBytesPerSec = pwfx->nSamplesPerSec * pwfx->nBlockAlign; // 48000
    pwfx->cbSize = 0;

    // Tell SAPI we're producing WAV format (PCM)
    *pDesiredFmtId = SPDFID_WaveFormatEx;
    *ppCoMemDesiredWaveFormatEx = pwfx;

    return S_OK;
}

// ============================================================================
// ISpTTSEngine::Speak — THE MAIN EVENT
// ============================================================================

STDMETHODIMP VoiceLinkEngine::Speak(
    DWORD /*dwSpeakFlags*/,
    REFGUID /*rguidFormatId*/,
    const WAVEFORMATEX * /*pWaveFormatEx*/,
    const SPVTEXTFRAG *pTextFragList,
    ISpTTSEngineSite *pOutputSite)
{
    // -----------------------------------------------------------------------
    // This is where the magic happens. The entire purpose of VoiceLink
    // flows through this one method:
    //
    //   SAPI gives us text → we send it to Kokoro → we stream audio back
    //
    // The pOutputSite is our lifeline to SAPI. It lets us:
    //   - Write audio data: pOutputSite->Write(pcmData, byteCount, &written)
    //   - Check for abort: pOutputSite->GetActions() & SPVES_ABORT
    //   - Report events: pOutputSite->EventNotify(event) (future)
    // -----------------------------------------------------------------------

    VLOG(L"Speak() called");

    if (!pTextFragList || !pOutputSite)
        return E_INVALIDARG;

    if (!m_initialized || !m_httpClient.IsInitialized())
    {
        VERR(L"Speak() called but engine not initialized");
        return E_FAIL;
    }

    // -----------------------------------------------------------------------
    // Step 1: Extract text from the SPVTEXTFRAG linked list
    //
    // SAPI doesn't give us a simple string. It gives us a linked list of
    // "text fragments", each with:
    //   - pTextStart: pointer to the text (NOT null-terminated!)
    //   - ulTextLen: number of characters
    //   - State.eAction: what to do (speak, silence, spell out, etc.)
    //   - pNext: next fragment
    //
    // Example for "Hello, World!":
    //   Fragment 1: pTextStart="Hello, ", ulTextLen=7, eAction=SPVA_Speak
    //   Fragment 2: pTextStart="World!", ulTextLen=6, eAction=SPVA_Speak
    //   Fragment 3: nullptr (end of list)
    // -----------------------------------------------------------------------
    std::string textUtf8 = ExtractText(pTextFragList);

    if (textUtf8.empty())
    {
        VLOG(L"No text to speak, returning S_OK");
        return S_OK;
    }

    VLOG(L"Text to synthesize: %zu bytes (UTF-8)", textUtf8.size());

    // -----------------------------------------------------------------------
    // Step 2: Get rate and volume from SAPI
    //
    // ISpTTSEngineSite provides rate and volume that the app or user set:
    //   GetRate()   → LONG from -10 to +10 (0 = normal)
    //   GetVolume() → USHORT from 0 to 100 (100 = full volume)
    //
    // RATE MAPPING:
    //   SAPI rate is an abstract scale. We convert it to a speed multiplier:
    //     speed = 3^(rate/10)
    //   This gives:
    //     Rate -10 → 0.33x (very slow)
    //     Rate  -5 → 0.58x
    //     Rate   0 → 1.0x  (normal)
    //     Rate   5 → 1.73x
    //     Rate  10 → 3.0x  (very fast)
    //   Clamped to [0.25, 4.0] to match server limits.
    //
    // VOLUME MAPPING:
    //   We scale each PCM sample by (volume / 100.0).
    //   Volume 100 = no change, Volume 50 = half amplitude, Volume 0 = silence.
    // -----------------------------------------------------------------------

    LONG sapiRate = 0;
    pOutputSite->GetRate(&sapiRate);

    USHORT sapiVolume = 100;
    pOutputSite->GetVolume(&sapiVolume);

    // Convert SAPI rate (-10..+10) to speed multiplier
    float speed = powf(3.0f, static_cast<float>(sapiRate) / 10.0f);
    speed = (std::max)(0.25f, (std::min)(4.0f, speed)); // Clamp to server limits

    // Volume as a fraction for PCM scaling
    float volumeScale = static_cast<float>(sapiVolume) / 100.0f;

    VLOG(L"SAPI rate=%ld → speed=%.2f, volume=%u → scale=%.2f",
         sapiRate, speed, sapiVolume, volumeScale);

    // -----------------------------------------------------------------------
    // Step 3: Build the JSON request for the inference server
    // -----------------------------------------------------------------------
    std::string jsonBody = BuildTtsRequestJson(textUtf8, m_voiceId, speed);

    // -----------------------------------------------------------------------
    // Step 4: Send to server and stream audio back
    //
    // The onChunk callback writes each chunk of PCM audio to SAPI.
    // The checkAbort callback checks if the app wants us to stop.
    //
    // This is where streaming pays off: audio starts playing within
    // ~100ms (GPU) or ~300ms (CPU) of calling Speak().
    // -----------------------------------------------------------------------

    // Track total bytes written (for logging)
    ULONG totalBytesWritten = 0;

    HRESULT hr = m_httpClient.StreamSynthesize(
        jsonBody.c_str(),
        static_cast<DWORD>(jsonBody.size()),

        // onChunk: called for each chunk of PCM audio from the server
        [&](const BYTE *data, DWORD size) -> HRESULT
        {
            // ---------------------------------------------------------------
            // Apply volume scaling if needed
            //
            // The audio is 16-bit signed PCM (int16). To adjust volume,
            // we multiply each sample by the volume fraction.
            //
            // We only do this work if volume != 100 (the common case
            // skips the copy entirely — zero overhead).
            // ---------------------------------------------------------------

            const BYTE *writeData = data;
            std::vector<BYTE> scaledBuf;

            if (volumeScale < 0.999f) // Only scale if volume < 100
            {
                scaledBuf.resize(size);
                const int16_t *src = reinterpret_cast<const int16_t *>(data);
                int16_t *dst = reinterpret_cast<int16_t *>(scaledBuf.data());
                DWORD sampleCount = size / sizeof(int16_t);

                for (DWORD i = 0; i < sampleCount; ++i)
                {
                    // Scale and clamp to int16 range
                    float scaled = static_cast<float>(src[i]) * volumeScale;
                    scaled = (std::max)(-32768.0f, (std::min)(32767.0f, scaled));
                    dst[i] = static_cast<int16_t>(scaled);
                }

                writeData = scaledBuf.data();
            }

            // Write audio data to SAPI's output site.
            //
            // ISpTTSEngineSite::Write() is like writing to a pipe:
            //   - SAPI buffers the data internally
            //   - The audio subsystem reads from the buffer and plays it
            //   - Write() blocks if the buffer is full (backpressure)
            //
            // The &written output tells us how many bytes SAPI accepted.
            // Usually it's all of them, but we should check.
            ULONG written = 0;
            HRESULT writeHr = pOutputSite->Write(writeData, size, &written);

            if (FAILED(writeHr))
            {
                VERR(L"ISpTTSEngineSite::Write failed: 0x%08lX", writeHr);
                return writeHr;
            }

            totalBytesWritten += written;
            return S_OK;
        },

        // checkAbort: called between chunks to see if we should stop
        [&]() -> bool
        {
            // GetActions() returns a bitmask of pending events.
            // SPVES_ABORT means "stop speaking NOW" — the user clicked stop,
            // navigated away, or the app is shutting down.
            //
            // We check this between chunks (every ~170ms of audio).
            // This gives responsive cancellation without checking too often.
            DWORD actions = pOutputSite->GetActions();
            if (actions & SPVES_ABORT)
            {
                VLOG(L"Abort requested by SAPI");
                return true; // Signal abort
            }
            return false;
        });

    if (hr == E_ABORT)
    {
        VLOG(L"Speak() aborted after %lu bytes", totalBytesWritten);
    }
    else if (FAILED(hr))
    {
        VERR(L"Speak() failed: 0x%08lX (wrote %lu bytes before failure)",
             hr, totalBytesWritten);
    }
    else
    {
        VLOG(L"Speak() completed: %lu bytes of audio streamed", totalBytesWritten);
    }

    // Return S_OK even on abort — that's the SAPI convention.
    // Errors are only for real failures (server down, etc.)
    return (hr == E_ABORT) ? S_OK : hr;
}

// ============================================================================
// Private Helpers
// ============================================================================

std::string VoiceLinkEngine::ExtractText(const SPVTEXTFRAG *pTextFragList)
{
    // Walk the linked list and concatenate all speakable text.
    //
    // SPVTEXTFRAG::State.eAction tells us what each fragment represents:
    //   SPVA_Speak   — Normal text to speak (most common)
    //   SPVA_Silence — Insert a pause (we could send a silence buffer)
    //   SPVA_SpellOut — Spell each character ("C-A-T")
    //   SPVA_Bookmark — An event marker (we ignore these)
    //   etc.
    //
    // For v1, we handle Speak and SpellOut. Everything else is skipped.

    std::wstring fullText;

    for (const SPVTEXTFRAG *frag = pTextFragList; frag; frag = frag->pNext)
    {
        if (!frag->pTextStart || frag->ulTextLen == 0)
            continue;

        switch (frag->State.eAction)
        {
        case SPVA_Speak:
            // Normal speech — just append the text
            fullText.append(frag->pTextStart, frag->ulTextLen);
            fullText += L' '; // Space between fragments
            break;

        case SPVA_SpellOut:
            // Spell each character with pauses
            // "cat" → "c. a. t."
            for (ULONG i = 0; i < frag->ulTextLen; ++i)
            {
                fullText += frag->pTextStart[i];
                fullText += L". ";
            }
            break;

        case SPVA_Silence:
            // TODO: Could insert a "..." or send silence audio
            break;

        default:
            // Skip bookmarks, pronunciations, etc.
            break;
        }
    }

    // Trim trailing whitespace
    while (!fullText.empty() && fullText.back() == L' ')
    {
        fullText.pop_back();
    }

    // Convert UTF-16 (Windows native) to UTF-8 (HTTP server expects)
    return WideToUtf8(fullText.c_str(), static_cast<int>(fullText.size()));
}

std::wstring VoiceLinkEngine::ReadTokenAttribute(const wchar_t *attrName)
{
    // Read a custom attribute from our voice token in the registry.
    //
    // The token has an "Attributes" subkey with values like:
    //   Name = "VoiceLink Kokoro Heart"
    //   VoiceLinkVoiceId = "af_heart"
    //
    // ISpObjectToken provides methods to read these without touching
    // the registry directly.

    if (!m_pToken || !attrName)
        return {};

    // SpGetSubTokenFromToken reads from the Attributes subkey
    // But for custom attributes at the token level, we use GetStringValue
    LPWSTR value = nullptr;
    HRESULT hr = m_pToken->GetStringValue(attrName, &value);

    if (SUCCEEDED(hr) && value)
    {
        std::wstring result(value);
        CoTaskMemFree(value); // SAPI allocates with CoTaskMemAlloc
        return result;
    }

    // Try the Attributes subkey
    ISpDataKey *pAttrs = nullptr;
    hr = m_pToken->OpenKey(L"Attributes", &pAttrs);
    if (SUCCEEDED(hr) && pAttrs)
    {
        hr = pAttrs->GetStringValue(attrName, &value);
        pAttrs->Release();

        if (SUCCEEDED(hr) && value)
        {
            std::wstring result(value);
            CoTaskMemFree(value);
            return result;
        }
    }

    return {};
}
