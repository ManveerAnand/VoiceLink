// ============================================================================
// VoiceLink SAPI Bridge — HTTP Client (Implementation)
// ============================================================================
//
// This file contains:
//   1. TtsHttpClient — WinHTTP wrapper for streaming TTS
//   2. JSON construction helpers (no library needed)
//   3. UTF-16 → UTF-8 conversion
//
// WINHTTP HANDLE HIERARCHY:
//   Session (WinHttpOpen)
//     └── Connection (WinHttpConnect)
//           └── Request (WinHttpOpenRequest)  ← created per Speak() call
//
// The session and connection are long-lived (created once in Init).
// Each Speak() call creates a new request, uses it, then closes it.
// ============================================================================

#include "http_client.h"
#include "debug.h"

#include <winhttp.h>
#include <sstream>
#include <iomanip>
#include <vector>

#pragma comment(lib, "winhttp.lib")

// ============================================================================
// Retry / Timeout Configuration
// ============================================================================
//
// When the server is not running (e.g. after a reboot before VoiceLink GUI
// starts), the COM DLL should not hang or crash the calling app. Instead:
//   1. Set connection/receive timeouts so we don't block forever
//   2. Retry a few times with short delays (server may be starting up)
//   3. If all retries fail, return S_OK (silence) instead of E_FAIL
//
// This makes VoiceLink gracefully degrade: the app just hears nothing
// instead of getting an error dialog or crashing.
// ============================================================================
static constexpr int    MAX_RETRIES         = 3;      // Total attempts
static constexpr DWORD  RETRY_DELAY_MS      = 500;    // Wait between retries
static constexpr DWORD  CONNECT_TIMEOUT_MS  = 2000;   // TCP connect timeout
static constexpr DWORD  SEND_TIMEOUT_MS     = 5000;   // Time to send request
static constexpr DWORD  RECEIVE_TIMEOUT_MS  = 30000;  // Time waiting for response headers

// ============================================================================
// TtsHttpClient Implementation
// ============================================================================

TtsHttpClient::~TtsHttpClient()
{
    Close();
}

HRESULT TtsHttpClient::Init(const wchar_t *host, INTERNET_PORT port)
{
    // Clean up any previous session
    Close();

    m_host = host;
    m_port = port;

    // -----------------------------------------------------------------------
    // Step 1: Create a WinHTTP session
    //
    // This is like opening a browser. It sets up the HTTP stack and
    // can be reused for many connections. The user agent string identifies
    // us in server logs (helpful for debugging).
    //
    // WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY: Use system proxy settings.
    // For localhost this doesn't matter, but it's good practice.
    // -----------------------------------------------------------------------
    m_hSession = WinHttpOpen(
        L"VoiceLink/1.0",                    // User agent
        WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, // Proxy setting
        WINHTTP_NO_PROXY_NAME,               // Proxy name (auto)
        WINHTTP_NO_PROXY_BYPASS,             // Proxy bypass (auto)
        0                                    // Flags (synchronous mode)
    );

    if (!m_hSession)
    {
        VERR(L"WinHttpOpen failed: %lu", GetLastError());
        return E_FAIL;
    }

    // Set timeouts so we don't block forever if the server is down.
    // WinHttpSetTimeouts parameters: resolve, connect, send, receive (all in ms).
    WinHttpSetTimeouts(m_hSession,
                       CONNECT_TIMEOUT_MS,  // DNS resolve (N/A for 127.0.0.1 but harmless)
                       CONNECT_TIMEOUT_MS,  // TCP connect
                       SEND_TIMEOUT_MS,     // Send
                       RECEIVE_TIMEOUT_MS); // Receive

    // -----------------------------------------------------------------------
    // Step 2: Connect to the server
    //
    // This doesn't actually open a TCP connection yet — it just associates
    // a host:port with the session. The actual TCP connection happens when
    // we send a request (WinHttpSendRequest).
    //
    // For VoiceLink, this is always localhost:7860.
    // -----------------------------------------------------------------------
    m_hConnect = WinHttpConnect(
        m_hSession, // Session handle
        host,       // Server name (L"127.0.0.1")
        port,       // Port (7860)
        0           // Reserved
    );

    if (!m_hConnect)
    {
        VERR(L"WinHttpConnect failed: %lu", GetLastError());
        Close();
        return E_FAIL;
    }

    VLOG(L"HTTP client initialized: %s:%d", host, port);
    return S_OK;
}

void TtsHttpClient::Close()
{
    if (m_hConnect)
    {
        WinHttpCloseHandle(m_hConnect);
        m_hConnect = nullptr;
    }
    if (m_hSession)
    {
        WinHttpCloseHandle(m_hSession);
        m_hSession = nullptr;
    }
}

HRESULT TtsHttpClient::StreamSynthesize(
    const char *jsonBody,
    DWORD jsonBodyLen,
    const std::function<HRESULT(const BYTE *data, DWORD size)> &onChunk,
    const std::function<bool()> &checkAbort,
    ULONGLONG *pTotalAudioBytes)
{
    if (!m_hConnect)
    {
        VERR(L"StreamSynthesize called but client not initialized");
        return E_FAIL;
    }

    // -----------------------------------------------------------------------
    // Retry loop: if the server is down or starting up, retry a few times
    // before giving up. This handles the common case where the COM DLL is
    // loaded (e.g. by Thorium) before the VoiceLink GUI has started the
    // server. Each retry waits briefly to give the server time to come up.
    // -----------------------------------------------------------------------
    HRESULT lastHr = E_FAIL;

    for (int attempt = 0; attempt < MAX_RETRIES; ++attempt)
    {
        if (attempt > 0)
        {
            VLOG(L"Retry attempt %d/%d after %lu ms delay",
                 attempt + 1, MAX_RETRIES, RETRY_DELAY_MS);
            Sleep(RETRY_DELAY_MS);

            // Check abort between retries (don't make the user wait)
            if (checkAbort && checkAbort())
            {
                VLOG(L"Synthesis aborted during retry wait");
                return S_OK; // SAPI convention: return S_OK on abort
            }
        }

        lastHr = StreamSynthesizeOnce(jsonBody, jsonBodyLen, onChunk,
                                       checkAbort, pTotalAudioBytes);

        // Success or user abort: stop retrying
        if (SUCCEEDED(lastHr) || lastHr == E_ABORT)
            return (lastHr == E_ABORT) ? S_OK : lastHr;

        VERR(L"StreamSynthesize attempt %d failed: 0x%08lX", attempt + 1, lastHr);
    }

    // All retries exhausted. Return S_OK (silence) instead of E_FAIL.
    // This prevents the calling app from showing an error or crashing.
    // The user just hears nothing, which is much better than a crash.
    VERR(L"All %d attempts failed. Returning silence to avoid app crash.", MAX_RETRIES);
    return S_OK;
}

// ============================================================================
// StreamSynthesizeOnce — Single attempt at HTTP TTS synthesis
// ============================================================================

HRESULT TtsHttpClient::StreamSynthesizeOnce(
    const char *jsonBody,
    DWORD jsonBodyLen,
    const std::function<HRESULT(const BYTE *data, DWORD size)> &onChunk,
    const std::function<bool()> &checkAbort,
    ULONGLONG *pTotalAudioBytes)
{

    // -----------------------------------------------------------------------
    // Step 1: Create a POST request to /v1/tts
    //
    // This creates a request object. The actual HTTP request isn't sent yet.
    // Think of it like filling out a form before clicking "Submit".
    //
    // We use HTTP (not HTTPS) because it's localhost only. No data leaves
    // the machine, so encryption would be wasted CPU cycles.
    // -----------------------------------------------------------------------
    HINTERNET hRequest = WinHttpOpenRequest(
        m_hConnect,                   // Connection handle
        L"POST",                      // HTTP method
        L"/v1/tts",                   // URL path
        nullptr,                      // HTTP version (nullptr = HTTP/1.1)
        WINHTTP_NO_REFERER,           // Referrer (none)
        WINHTTP_DEFAULT_ACCEPT_TYPES, // Accept types (*/*)
        0                             // Flags (no HTTPS)
    );

    if (!hRequest)
    {
        VERR(L"WinHttpOpenRequest failed: %lu", GetLastError());
        return E_FAIL;
    }

    // -----------------------------------------------------------------------
    // Step 2: Set headers
    //
    // Content-Type tells the server we're sending JSON.
    // The server uses this to parse the request body correctly.
    // -----------------------------------------------------------------------
    const wchar_t *headers = L"Content-Type: application/json\r\n";
    BOOL headerOk = WinHttpAddRequestHeaders(
        hRequest,
        headers,
        static_cast<DWORD>(wcslen(headers)),
        WINHTTP_ADDREQ_FLAG_ADD);

    if (!headerOk)
    {
        VERR(L"WinHttpAddRequestHeaders failed: %lu", GetLastError());
        WinHttpCloseHandle(hRequest);
        return E_FAIL;
    }

    // -----------------------------------------------------------------------
    // Step 3: Send the request
    //
    // This actually opens the TCP connection (if not already open from a
    // previous request), sends the HTTP headers, and sends the JSON body.
    //
    // WinHttpSendRequest combines "send headers" and "write body" in one
    // call for simple requests. For larger bodies, you'd use
    // WinHttpWriteData separately.
    //
    // NOTE: This blocks until the server acknowledges the request.
    // For localhost, this is nearly instant (~0.1ms).
    // -----------------------------------------------------------------------
    BOOL sendOk = WinHttpSendRequest(
        hRequest,
        WINHTTP_NO_ADDITIONAL_HEADERS, 0,          // Additional headers (none)
        const_cast<char *>(jsonBody), jsonBodyLen, // Request body
        jsonBodyLen,                               // Total body length
        0                                          // Context (unused)
    );

    if (!sendOk)
    {
        DWORD err = GetLastError();
        VERR(L"WinHttpSendRequest failed: %lu (is the server running?)", err);
        WinHttpCloseHandle(hRequest);
        return E_FAIL;
    }

    // -----------------------------------------------------------------------
    // Step 4: Receive the response headers
    //
    // This blocks until the server sends back the HTTP response headers.
    // The server starts streaming audio immediately after, so this wait
    // includes the time for the model to generate the first audio chunk.
    //
    // For Kokoro on GPU: ~50-100ms
    // For Kokoro on CPU: ~200-500ms
    // -----------------------------------------------------------------------
    BOOL recvOk = WinHttpReceiveResponse(hRequest, nullptr);

    if (!recvOk)
    {
        VERR(L"WinHttpReceiveResponse failed: %lu", GetLastError());
        WinHttpCloseHandle(hRequest);
        return E_FAIL;
    }

    // -----------------------------------------------------------------------
    // Step 5: Check HTTP status code
    //
    // 200 = OK, everything else is an error.
    // Common errors:
    //   404 = wrong URL path
    //   422 = invalid JSON (Pydantic validation failed)
    //   500 = server crashed during synthesis
    // -----------------------------------------------------------------------
    DWORD statusCode = 0;
    DWORD statusSize = sizeof(statusCode);
    WinHttpQueryHeaders(
        hRequest,
        WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
        WINHTTP_HEADER_NAME_BY_INDEX,
        &statusCode,
        &statusSize,
        WINHTTP_NO_HEADER_INDEX);

    // -----------------------------------------------------------------------
    // Step 5b: Read X-Audio-Length header (if present)
    //
    // The server pre-generates all audio and reports the exact total byte
    // count in this header. The TTS engine uses it for perfectly
    // proportional SPEI_WORD_BOUNDARY event offsets, so Edge Read Aloud
    // text highlighting stays in sync with the audio.
    // -----------------------------------------------------------------------
    if (pTotalAudioBytes)
    {
        *pTotalAudioBytes = 0;
        wchar_t headerBuf[64] = {};
        DWORD headerBufSize = sizeof(headerBuf);
        BOOL hdrOk = WinHttpQueryHeaders(
            hRequest,
            WINHTTP_QUERY_CUSTOM,
            L"X-Audio-Length",
            headerBuf,
            &headerBufSize,
            WINHTTP_NO_HEADER_INDEX);
        if (hdrOk && headerBufSize > 0)
        {
            *pTotalAudioBytes = static_cast<ULONGLONG>(_wtoi64(headerBuf));
            VLOG(L"Server reported X-Audio-Length: %llu bytes", *pTotalAudioBytes);
        }
    }

    if (statusCode != 200)
    {
        VERR(L"Server returned HTTP %lu", statusCode);

        // Try to read the error body for debugging
        char errBuf[512] = {};
        DWORD errRead = 0;
        WinHttpReadData(hRequest, errBuf, sizeof(errBuf) - 1, &errRead);
        if (errRead > 0)
        {
            errBuf[errRead] = '\0';
            VLOG(L"Server error body: %hs", errBuf);
        }

        WinHttpCloseHandle(hRequest);
        return E_FAIL;
    }

    // -----------------------------------------------------------------------
    // Step 6: Read the streaming audio response
    //
    // The server sends raw PCM audio using HTTP chunked transfer encoding.
    // We read it in chunks and pass each one to SAPI immediately.
    //
    // Buffer size: 8192 bytes = ~170ms of audio at 24kHz/16-bit/mono
    // (24000 samples/sec * 2 bytes/sample = 48000 bytes/sec)
    // (8192 / 48000 = 0.170 seconds)
    //
    // Between chunks, we check if the app wants us to stop (abort).
    // This happens when the user clicks "stop" or navigates away.
    // -----------------------------------------------------------------------
    constexpr DWORD CHUNK_BUF_SIZE = 8192;
    BYTE buffer[CHUNK_BUF_SIZE];
    HRESULT result = S_OK;

    for (;;)
    {
        // Check if the caller wants us to abort
        if (checkAbort && checkAbort())
        {
            VLOG(L"Synthesis aborted by caller");
            result = E_ABORT;
            break;
        }

        // Read the next chunk of audio data
        // WinHttpReadData blocks until:
        //   - Data is available (returns that data)
        //   - Connection closes (returns 0 bytes = we're done)
        //   - Error occurs (returns FALSE)
        DWORD bytesRead = 0;
        BOOL readOk = WinHttpReadData(
            hRequest,
            buffer,
            CHUNK_BUF_SIZE,
            &bytesRead);

        if (!readOk)
        {
            VERR(L"WinHttpReadData failed: %lu", GetLastError());
            result = E_FAIL;
            break;
        }

        // 0 bytes = server finished sending audio
        if (bytesRead == 0)
        {
            break;
        }

        // Pass this chunk to SAPI (via the callback)
        HRESULT chunkResult = onChunk(buffer, bytesRead);
        if (FAILED(chunkResult))
        {
            VERR(L"onChunk callback failed: 0x%08lX", chunkResult);
            result = chunkResult;
            break;
        }
    }

    // -----------------------------------------------------------------------
    // Step 7: Clean up
    //
    // Close the request handle. The session and connection handles stay
    // open for the next Speak() call.
    // -----------------------------------------------------------------------
    WinHttpCloseHandle(hRequest);

    return result;
}

// ============================================================================
// JSON Helpers
// ============================================================================

std::string JsonEscapeUtf8(const std::string &input)
{
    // JSON string escaping rules (RFC 8259, Section 7):
    //   - Quotation mark (") → \"
    //   - Reverse solidus (\) → \\
    //   - Solidus (/) → \/ (optional, we skip this)
    //   - Backspace → \b
    //   - Form feed → \f
    //   - Newline → \n
    //   - Carriage return → \r
    //   - Tab → \t
    //   - Any character < U+0020 → \uXXXX

    std::string result;
    result.reserve(input.size() + input.size() / 8); // Pre-allocate ~12% extra

    for (unsigned char ch : input)
    {
        switch (ch)
        {
        case '"':
            result += "\\\"";
            break;
        case '\\':
            result += "\\\\";
            break;
        case '\b':
            result += "\\b";
            break;
        case '\f':
            result += "\\f";
            break;
        case '\n':
            result += "\\n";
            break;
        case '\r':
            result += "\\r";
            break;
        case '\t':
            result += "\\t";
            break;
        default:
            if (ch < 0x20)
            {
                // Control character → \u00XX
                char hex[8];
                snprintf(hex, sizeof(hex), "\\u%04x", ch);
                result += hex;
            }
            else
            {
                result += static_cast<char>(ch);
            }
            break;
        }
    }

    return result;
}

std::string BuildTtsRequestJson(const std::string &text,
                                const std::string &voiceId,
                                float speed)
{
    // Build JSON by hand. This is simple enough that a library would be
    // overkill, and we avoid adding any dependencies.
    //
    // Output format:
    //   {"text": "escaped text", "voice": "af_heart", "speed": 1.0, "format": "pcm_24k_16bit"}

    std::ostringstream json;
    json << "{\"text\": \"" << JsonEscapeUtf8(text)
         << "\", \"voice\": \"" << JsonEscapeUtf8(voiceId)
         << "\", \"speed\": " << std::fixed << std::setprecision(2) << speed
         << ", \"format\": \"pcm_24k_16bit\"}";

    return json.str();
}

// ============================================================================
// Encoding Helpers
// ============================================================================

std::string WideToUtf8(const wchar_t *wide, int len)
{
    // Windows uses UTF-16 (2 bytes per most characters, 4 bytes for rare ones).
    // Our HTTP server expects UTF-8 (1-4 bytes per character, ASCII-compatible).
    //
    // WideCharToMultiByte is the Windows API for this conversion.
    // We call it twice:
    //   1. First call with output buffer = nullptr → returns required size
    //   2. Second call with properly sized buffer → does the conversion
    //
    // This two-call pattern is common in Windows APIs that return
    // variable-length data.

    if (!wide || (len == 0))
    {
        return {};
    }

    // First call: get required buffer size
    int needed = WideCharToMultiByte(
        CP_UTF8, // Target encoding
        0,       // Flags (0 for UTF-8)
        wide,    // Source wide string
        len,     // Source length (-1 if null-terminated)
        nullptr, // Output buffer (nullptr = just tell me the size)
        0,       // Output buffer size
        nullptr, // Default character (must be nullptr for UTF-8)
        nullptr  // Used default character flag (must be nullptr for UTF-8)
    );

    if (needed <= 0)
    {
        return {};
    }

    // Second call: do the actual conversion
    std::string result(static_cast<size_t>(needed), '\0');
    WideCharToMultiByte(
        CP_UTF8, 0,
        wide, len,
        result.data(), needed,
        nullptr, nullptr);

    // If len was -1, the result includes a null terminator — trim it
    if (len == -1 && !result.empty() && result.back() == '\0')
    {
        result.pop_back();
    }

    return result;
}
