# ============================================================================
# VoiceLink SAPI Bridge — CMake Build
# ============================================================================
#
# This builds a COM DLL that registers as a Windows SAPI TTS voice.
# When any app (Thorium Reader, Edge, Narrator) asks the voice to speak,
# it forwards the text to our Python inference server and streams the
# AI-generated audio back through SAPI.
#
# BUILD INSTRUCTIONS:
#   1. Open "x64 Native Tools Command Prompt for VS 2022"
#   2. cd to this directory
#   3. cmake -B build -G "Visual Studio 17 2022" -A x64
#   4. cmake --build build --config Release
#   5. The DLL will be at: build/Release/voicelink_sapi.dll
#
# REGISTER THE DLL:
#   regsvr32 build/Release/voicelink_sapi.dll       (as admin)
#
# UNREGISTER:
#   regsvr32 /u build/Release/voicelink_sapi.dll    (as admin)
# ============================================================================

cmake_minimum_required(VERSION 3.20)
project(voicelink_sapi LANGUAGES CXX)

# ----------------------------------------------------------------------------
# C++ Standard
# We use C++17 for:
#   - std::string_view (zero-copy string slices)
#   - std::optional (nullable values without pointers)
#   - structured bindings (auto [key, value] = ...)
#   - if constexpr (compile-time branching)
# We do NOT use C++20 because we want broad compiler compatibility.
# ----------------------------------------------------------------------------
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ----------------------------------------------------------------------------
# Source Files
# The DLL is built from these files, each with a clear responsibility:
#
#   dllmain.cpp         — DLL entry point + 4 COM exports + registry
#   class_factory.cpp   — IClassFactory: creates VoiceLinkEngine instances
#   tts_engine.cpp      — ISpTTSEngine: the actual TTS engine
#   http_client.cpp     — WinHTTP wrapper for talking to inference server
#
# Headers (guids.h, debug.h) are included for IDE navigation.
# ----------------------------------------------------------------------------
set(SOURCES
    src/dllmain.cpp
    src/class_factory.cpp
    src/tts_engine.cpp
    src/http_client.cpp
)

set(HEADERS
    src/guids.h
    src/debug.h
    src/class_factory.h
    src/tts_engine.h
    src/http_client.h
)

# ----------------------------------------------------------------------------
# Build as a Shared Library (DLL)
# A COM DLL must be a "shared library" — it gets loaded into an app's process
# space when the app calls CoCreateInstance. The app and our DLL share the
# same memory space, which is why COM in-process servers are fast.
# ----------------------------------------------------------------------------
add_library(voicelink_sapi SHARED ${SOURCES} ${HEADERS})

# ----------------------------------------------------------------------------
# Module Definition File (.def)
# This tells the linker EXACTLY which functions to export from the DLL.
# Without this, our functions would get "name mangled" by C++ and COM
# wouldn't be able to find them.
#
# The 4 exports every COM DLL needs:
#   DllGetClassObject   — COM calls this to get our class factory
#   DllCanUnloadNow     — COM calls this to check if DLL can be freed
#   DllRegisterServer   — regsvr32 calls this to register us in the registry
#   DllUnregisterServer — regsvr32 /u calls this to clean up
# ----------------------------------------------------------------------------
set_target_properties(voicelink_sapi PROPERTIES
    LINK_FLAGS "/DEF:\"${CMAKE_CURRENT_SOURCE_DIR}/src/voicelink.def\""
)

# ----------------------------------------------------------------------------
# Libraries We Link Against
# These are all part of Windows — no third-party dependencies needed.
#
#   winhttp.lib  — WinHTTP for talking to the Python server
#   ole32.lib    — COM runtime (CoTaskMemAlloc, CoCreateInstance, etc.)
#   oleaut32.lib — OLE Automation (VARIANT, BSTR — used by SAPI)
#   uuid.lib     — Pre-defined GUIDs (IID_IUnknown, IID_IClassFactory, etc.)
#   advapi32.lib — Registry functions (RegCreateKeyEx, RegSetValueEx, etc.)
#   sapi.lib     — SAPI type library
# ----------------------------------------------------------------------------
target_link_libraries(voicelink_sapi PRIVATE
    winhttp
    ole32
    oleaut32
    uuid
    advapi32
    sapi
)

# ----------------------------------------------------------------------------
# Compiler Settings
# ----------------------------------------------------------------------------
target_compile_definitions(voicelink_sapi PRIVATE
    # UNICODE and _UNICODE: Use wide-char (UTF-16) Win32 APIs by default.
    # Windows internally uses UTF-16, and SAPI gives us UTF-16 text,
    # so this is the natural choice.
    UNICODE
    _UNICODE

    # _WIN32_WINNT=0x0A00: Target Windows 10+.
    # This enables modern Win32 APIs and suppresses deprecation warnings
    # for older OS versions we don't support.
    _WIN32_WINNT=0x0A00

    # WIN32_LEAN_AND_MEAN: Exclude rarely-used Windows headers.
    # Speeds up compilation by skipping Winsock1, DDE, RPC, etc.
    WIN32_LEAN_AND_MEAN
)

# Warning level 4 (high) + treat warnings as errors in Release
target_compile_options(voicelink_sapi PRIVATE
    /W4         # High warning level — catches many subtle bugs
    /WX         # Warnings are errors — enforces clean code
    /permissive- # Strict standards conformance
    /Zc:__cplusplus # Report correct C++ version in __cplusplus macro
)

# In Debug builds, enable runtime checks and debug iterators
target_compile_definitions(voicelink_sapi PRIVATE
    $<$<CONFIG:Debug>:_DEBUG>
    $<$<CONFIG:Debug>:_ITERATOR_DEBUG_LEVEL=2>
)

# In Release builds, optimize for speed
target_compile_options(voicelink_sapi PRIVATE
    $<$<CONFIG:Release>:/O2>    # Maximum speed optimization
    $<$<CONFIG:Release>:/GL>    # Whole program optimization
)

# Link-time code generation for Release (pairs with /GL)
set_target_properties(voicelink_sapi PROPERTIES
    LINK_FLAGS_RELEASE "/LTCG"
)

# ----------------------------------------------------------------------------
# Output Name
# The DLL will be named "voicelink_sapi.dll" in the output directory.
# This name appears in the registry under InprocServer32.
# ----------------------------------------------------------------------------
set_target_properties(voicelink_sapi PROPERTIES
    OUTPUT_NAME "voicelink_sapi"
    PREFIX ""  # Don't add "lib" prefix (Windows convention)
)
