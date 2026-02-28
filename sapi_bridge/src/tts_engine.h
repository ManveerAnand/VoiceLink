// ============================================================================
// VoiceLink SAPI Bridge — TTS Engine (Header)
// ============================================================================
//
// This is the heart of VoiceLink. VoiceLinkEngine is the COM object that
// Windows sees as a "TTS voice". It implements two interfaces:
//
//   ISpTTSEngine — The engine that turns text into audio
//     - Speak(): receives text, returns audio
//     - GetOutputFormat(): tells SAPI what audio format we produce
//
//   ISpObjectWithToken — Links the engine to its registry token
//     - SetObjectToken(): SAPI tells us which voice token created us
//     - GetObjectToken(): SAPI asks which token we're associated with
//
// Plus IUnknown (the base of all COM objects):
//     - QueryInterface(): "Do you support this interface?"
//     - AddRef(): "I need you, don't go away"
//     - Release(): "I'm done with you" (destructs when count hits 0)
//
// LIFECYCLE:
//   1. App calls CoCreateInstance(CLSID_VoiceLinkEngine, ISpTTSEngine)
//   2. COM calls our DllGetClassObject → ClassFactory::CreateInstance
//   3. SAPI calls SetObjectToken(token for "VoiceLink Kokoro Heart")
//   4. SAPI calls GetOutputFormat() → we say "24kHz, 16-bit, mono"
//   5. For each utterance, SAPI calls Speak(text, site)
//   6. We extract text, HTTP POST to server, stream audio back via site
//   7. App calls Release() → ref count hits 0 → destructor runs
//
// VTABLE MEMORY LAYOUT:
//   Because we inherit from TWO interfaces (ISpTTSEngine + ISpObjectWithToken),
//   and both inherit from IUnknown, our C++ object has TWO vtable pointers:
//
//   VoiceLinkEngine object in memory:
//   ┌─────────────────────────────────┐
//   │ vtptr → ISpTTSEngine vtable     │ ← "this" pointer for ISpTTSEngine
//   │ vtptr → ISpObjectWithToken vtbl │ ← offset "this" for ISpObjectWithToken
//   │ m_refCount                      │
//   │ m_pToken                        │
//   │ m_httpClient                    │
//   │ m_voiceId                       │
//   │ ...                             │
//   └─────────────────────────────────┘
//
//   QueryInterface handles the pointer adjustment between the two vtables.
//   This is standard COM "diamond inheritance" — the compiler does most
//   of the work, but we need to be aware of it for correct QI behavior.
// ============================================================================

#pragma once

#include <windows.h>
#include <sapi.h>
#include <sapiddk.h>
#include <string>

#include "http_client.h"

// Global counters (defined in dllmain.cpp)
extern LONG g_objectCount;

class VoiceLinkEngine : public ISpTTSEngine, public ISpObjectWithToken
{
public:
    VoiceLinkEngine();
    ~VoiceLinkEngine();

    // -----------------------------------------------------------------------
    // IUnknown — The foundation of every COM object
    //
    // Every COM interface inherits from IUnknown. These three methods are
    // how COM manages object lifetime and interface discovery.
    // -----------------------------------------------------------------------

    // "Do you support interface X?" Returns S_OK + pointer if yes.
    // This is how COM does runtime type checking (like dynamic_cast).
    STDMETHODIMP QueryInterface(REFIID riid, void **ppv) override;

    // "I need a reference to you." Increments the reference count.
    // Thread-safe via InterlockedIncrement.
    STDMETHODIMP_(ULONG)
    AddRef() override;

    // "I'm done with you." Decrements the reference count.
    // When it hits 0, the object deletes itself.
    STDMETHODIMP_(ULONG)
    Release() override;

    // -----------------------------------------------------------------------
    // ISpTTSEngine — The actual TTS engine interface
    //
    // SAPI calls these methods to make us speak.
    // -----------------------------------------------------------------------

    // THE BIG ONE. SAPI calls this when an app wants text spoken.
    //
    // Parameters:
    //   dwSpeakFlags    — Bitfield: SPF_ASYNC, SPF_PURGEBEFORESPEAK, etc.
    //   rguidFormatId   — Requested audio format GUID (usually SPDFID_WaveFormatEx)
    //   pWaveFormatEx   — Requested audio format details (can be null)
    //   pTextFragList   — Linked list of text fragments to speak
    //   pOutputSite     — Where we write audio data back to SAPI
    //
    // This method:
    //   1. Extracts text from the fragment list
    //   2. Sends it to the Python inference server via HTTP
    //   3. Streams the PCM audio back through pOutputSite->Write()
    //   4. Checks for abort between chunks
    STDMETHODIMP Speak(
        DWORD dwSpeakFlags,
        REFGUID rguidFormatId,
        const WAVEFORMATEX *pWaveFormatEx,
        const SPVTEXTFRAG *pTextFragList,
        ISpTTSEngineSite *pOutputSite) override;

    // Tell SAPI what audio format we produce.
    //
    // We always produce: 24kHz, 16-bit, mono PCM
    // This matches Kokoro's native output format, so no resampling needed.
    //
    // SAPI calls this before Speak() to set up its audio pipeline.
    // We must allocate the WAVEFORMATEX with CoTaskMemAlloc because
    // SAPI will free it with CoTaskMemFree.
    STDMETHODIMP GetOutputFormat(
        const GUID *pTargetFmtId,
        const WAVEFORMATEX *pTargetWaveFormatEx,
        GUID *pDesiredFmtId,
        WAVEFORMATEX **ppCoMemDesiredWaveFormatEx) override;

    // -----------------------------------------------------------------------
    // ISpObjectWithToken — Links this engine to its registry token
    //
    // Each voice (Kokoro Heart, Kokoro Adam, etc.) has a registry token.
    // When SAPI creates our engine for a specific voice, it calls
    // SetObjectToken so we know WHICH voice we should be.
    // -----------------------------------------------------------------------

    // SAPI tells us which voice token we were created for.
    // We read custom attributes from the token:
    //   - VoiceLinkVoiceId: "af_heart", "am_adam", etc.
    //   - VoiceLinkServerPort: "7860" (optional, defaults to 7860)
    STDMETHODIMP SetObjectToken(ISpObjectToken *pToken) override;

    // SAPI asks which token we're associated with.
    STDMETHODIMP GetObjectToken(ISpObjectToken **ppToken) override;

private:
    // -----------------------------------------------------------------------
    // Private Helpers
    // -----------------------------------------------------------------------

    // Walk the SPVTEXTFRAG linked list and extract all speakable text.
    // Handles SPVA_Speak fragments, skips bookmarks/silence/etc.
    // Also captures the max RateAdj across fragments via pMaxRateAdj output.
    std::string ExtractText(const SPVTEXTFRAG *pTextFragList, LONG *pMaxRateAdj = nullptr);

    // Read a string attribute from our voice token's registry entry.
    std::wstring ReadTokenAttribute(const wchar_t *attrName);

    // -----------------------------------------------------------------------
    // Member Variables
    // -----------------------------------------------------------------------

    LONG m_refCount;            // COM reference count (InterlockedIncrement/Decrement)
    ISpObjectToken *m_pToken;   // Our voice token (AddRef'd, released in destructor)
    TtsHttpClient m_httpClient; // HTTP connection to the inference server
    std::string m_voiceId;      // Voice ID from token ("af_heart", "am_adam", etc.)
    INTERNET_PORT m_serverPort; // Server port from token (default: 7860)
    bool m_initialized;         // Have we been fully set up via SetObjectToken?
};
