// ============================================================================
// VoiceLink SAPI Bridge — HTTP Client (Header)
// ============================================================================
//
// This is how our COM DLL talks to the Python inference server.
//
// WHY HTTP? (and not pipes, shared memory, etc.)
//   1. HTTP is universal — the server can be tested with curl, Postman, etc.
//   2. HTTP chunked transfer encoding gives us natural streaming
//   3. HTTP keeps the two components loosely coupled
//   4. Localhost HTTP is fast — kernel bypasses the network stack
//
// WHY WINHTTP? (and not libcurl, Boost.Beast, etc.)
//   1. Zero dependencies — WinHTTP is built into Windows since XP
//   2. No DLL to ship, no version conflicts
//   3. Supports chunked transfer encoding natively
//   4. Async support if we need it later
//   5. TLS support if we ever need remote servers
//
// THE FLOW:
//   1. Open a session (once, reused across all requests)
//   2. Connect to localhost:7860 (once, reused)
//   3. For each Speak() call:
//      a. Open a POST request to /v1/tts
//      b. Send JSON body: {"text": "...", "voice": "af_heart"}
//      c. Read response in chunks (each chunk is raw PCM audio)
//      d. Pass each chunk to SAPI via callback
//      e. Close the request
// ============================================================================

#pragma once

#include <windows.h>
#include <winhttp.h>
#include <functional>
#include <string>

// Forward declaration — defined in winhttp.h but just in case
#ifndef HINTERNET
typedef LPVOID HINTERNET;
#endif

// ============================================================================
// TtsHttpClient — Streaming HTTP client for the TTS inference server
// ============================================================================
//
// Lifecycle:
//   1. Construct
//   2. Call Init() once (creates WinHTTP session + connection)
//   3. Call StreamSynthesize() for each Speak() call (can call many times)
//   4. Call Close() or let destructor handle it
//
// Thread Safety:
//   WinHTTP handles are thread-safe for different requests. Our COM engine
//   uses threading model "Both", so Speak() can be called from any thread.
//   Each Speak() call creates its own request handle, so concurrent calls
//   are safe as long as they don't share request handles.
// ============================================================================

class TtsHttpClient {
public:
    TtsHttpClient() = default;
    ~TtsHttpClient();

    // Non-copyable (WinHTTP handles are not reference-counted)
    TtsHttpClient(const TtsHttpClient&) = delete;
    TtsHttpClient& operator=(const TtsHttpClient&) = delete;

    // -----------------------------------------------------------------------
    // Init — Create the WinHTTP session and connect to the server
    //
    // Call this once after construction. It creates:
    //   - A session handle (like opening a browser)
    //   - A connection handle (like typing in a URL)
    //
    // Returns S_OK on success, E_FAIL on error.
    // -----------------------------------------------------------------------
    HRESULT Init(const wchar_t* host, INTERNET_PORT port);

    // -----------------------------------------------------------------------
    // Close — Release all WinHTTP handles
    // -----------------------------------------------------------------------
    void Close();

    // -----------------------------------------------------------------------
    // StreamSynthesize — Send text to the server, get back streaming audio
    //
    // Parameters:
    //   jsonBody    — The JSON request body (UTF-8 encoded)
    //   jsonBodyLen — Length of jsonBody in bytes
    //   onChunk     — Called for each chunk of PCM audio data received.
    //                 Return S_OK to continue, any error to abort.
    //   checkAbort  — Called between chunks. Return true to abort.
    //
    // Returns:
    //   S_OK      — All audio received and delivered successfully
    //   E_ABORT   — Aborted by checkAbort() returning true
    //   E_FAIL    — HTTP error (server down, bad response, etc.)
    //
    // HOW STREAMING WORKS:
    //   The server uses HTTP chunked transfer encoding. This means it starts
    //   sending audio before the full synthesis is complete. We read each
    //   chunk as it arrives (WinHttpReadData blocks until data is available)
    //   and immediately pass it to SAPI. Result: audio starts playing within
    //   ~100ms of sending the request.
    // -----------------------------------------------------------------------
    HRESULT StreamSynthesize(
        const char* jsonBody,
        DWORD jsonBodyLen,
        const std::function<HRESULT(const BYTE* data, DWORD size)>& onChunk,
        const std::function<bool()>& checkAbort
    );

    // -----------------------------------------------------------------------
    // IsInitialized — Check if Init() has been called successfully
    // -----------------------------------------------------------------------
    bool IsInitialized() const { return m_hConnect != nullptr; }

private:
    HINTERNET m_hSession = nullptr;    // WinHTTP session (like a browser instance)
    HINTERNET m_hConnect = nullptr;    // Connection to localhost:7860
    std::wstring m_host;
    INTERNET_PORT m_port = 0;
};

// ============================================================================
// JSON Helpers
// ============================================================================
//
// We construct JSON by hand instead of pulling in a JSON library.
// Our request is simple enough that this is cleaner than adding a dependency:
//   {"text": "Hello world", "voice": "af_heart", "speed": 1.0}
//
// The tricky part is escaping the text properly. JSON requires:
//   " → \"    (quote)
//   \ → \\    (backslash)
//   \n → \\n  (newline)
//   \r → \\r  (carriage return)
//   \t → \\t  (tab)
//   Control chars (0x00-0x1F) → \uXXXX
// ============================================================================

// Escape a UTF-8 string for safe inclusion in a JSON string value.
// The result does NOT include the surrounding quotes.
std::string JsonEscapeUtf8(const std::string& input);

// Build the complete JSON body for a TTS request.
std::string BuildTtsRequestJson(const std::string& text,
                                const std::string& voiceId,
                                float speed);

// ============================================================================
// Encoding Helpers
// ============================================================================

// Convert a wide string (UTF-16, what Windows/SAPI uses) to UTF-8
// (what our HTTP server expects).
std::string WideToUtf8(const wchar_t* wide, int len = -1);
