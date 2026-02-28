// ============================================================================
// VoiceLink SAPI Bridge — Debug / Logging Helpers
// ============================================================================
//
// COM DLLs run inside another process's address space (Thorium Reader,
// Edge, Narrator, etc.). You can't just printf() to see what's happening.
//
// These macros use OutputDebugString, which sends text to:
//   - Visual Studio's Output window (when debugging)
//   - Sysinternals DebugView (free tool, great for production debugging)
//   - Any debugger attached to the host process
//
// WHY NOT A LOGGING LIBRARY?
//   We want zero dependencies. OutputDebugString is built into Windows
//   and adds zero overhead when no debugger is attached (the call becomes
//   a no-op in the kernel).
//
// USAGE:
//   VLOG(L"Speak() called with %d characters", textLen);
//   VERR(L"HTTP request failed: %lu", GetLastError());
//
// In Release builds, you can disable logging by defining VOICELINK_NO_LOG.
// ============================================================================

#pragma once

#include <windows.h>
#include <cstdio>

// Prefix all our messages so they're easy to filter in DebugView
#define VOICELINK_LOG_PREFIX L"[VoiceLink] "

#ifndef VOICELINK_NO_LOG

// General info log
#define VLOG(fmt, ...)                                                         \
    do {                                                                       \
        wchar_t _vl_buf[1024];                                                 \
        _snwprintf_s(_vl_buf, _countof(_vl_buf), _TRUNCATE,                    \
                     VOICELINK_LOG_PREFIX fmt L"\n", ##__VA_ARGS__);            \
        OutputDebugStringW(_vl_buf);                                           \
    } while (0)

// Error log (same output, different prefix for easy grep)
#define VERR(fmt, ...)                                                         \
    do {                                                                       \
        wchar_t _vl_buf[1024];                                                 \
        _snwprintf_s(_vl_buf, _countof(_vl_buf), _TRUNCATE,                    \
                     VOICELINK_LOG_PREFIX L"ERROR: " fmt L"\n",                \
                     ##__VA_ARGS__);                                            \
        OutputDebugStringW(_vl_buf);                                           \
    } while (0)

#else

// Logging disabled — macros compile to nothing
#define VLOG(fmt, ...) ((void)0)
#define VERR(fmt, ...) ((void)0)

#endif  // VOICELINK_NO_LOG
