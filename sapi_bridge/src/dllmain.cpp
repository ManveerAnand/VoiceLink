// ============================================================================
// VoiceLink SAPI Bridge — DLL Entry Point + COM Exports + Registry
// ============================================================================
//
// This file contains:
//   1. DllMain          — Called when the DLL is loaded/unloaded
//   2. DllGetClassObject — COM asks us for a class factory
//   3. DllCanUnloadNow   — COM asks if we can be removed from memory
//   4. DllRegisterServer — regsvr32 calls this to register us
//   5. DllUnregisterServer — regsvr32 /u calls this to unregister us
//
// Plus the registry code that makes our voices appear in Windows.
//
// THE 4 EXPORTS:
//   Every COM in-process server (DLL) must export exactly these 4 functions.
//   They're defined in voicelink.def so the linker exports them with the
//   correct names (no C++ name mangling).
//
// REGISTRATION:
//   When you run "regsvr32 voicelink_sapi.dll", Windows:
//   1. Loads our DLL (DllMain fires)
//   2. Calls DllRegisterServer
//   3. We write registry keys that tell Windows about our COM class and voices
//   4. Unloads the DLL
//
//   After this, our voices appear in any app's voice list.
// ============================================================================

// INITGUID must be defined BEFORE including guids.h in exactly ONE .cpp file.
// This makes DEFINE_GUID create actual GUID storage instead of extern declarations.
#include <initguid.h>

#include "guids.h"
#include "class_factory.h"
#include "debug.h"

#include <windows.h>
#include <sapi.h>
#include <string>
#include <vector>

// ============================================================================
// Global State
// ============================================================================

// Handle to our DLL module (needed to find our DLL path for registry)
HMODULE g_hModule = nullptr;

// Reference counting for DLL unloading.
// COM won't call FreeLibrary on us while either of these is > 0.
LONG g_lockCount = 0;   // Incremented by IClassFactory::LockServer(TRUE)
LONG g_objectCount = 0; // Incremented when a VoiceLinkEngine is created

// ============================================================================
// Voice Definitions
// ============================================================================
//
// Each entry here becomes a SAPI voice token in the registry.
// All voices share the same CLSID (same engine), but the engine reads
// the VoiceLinkVoiceId attribute to know which voice to request.
//
// The Language field uses LCID (Locale ID) in hex:
//   409 = en-US (English, United States)
//   809 = en-GB (English, United Kingdom)
//
// SAPI uses these attributes for voice selection:
//   - Name: display name in voice picker
//   - Gender: Male or Female
//   - Language: filters by locale
//   - Vendor: groups voices by provider

struct VoiceDefinition
{
    const wchar_t *tokenName;   // Registry key name (e.g., "VoiceLink_af_heart")
    const wchar_t *displayName; // Friendly name (e.g., "VoiceLink Kokoro Heart")
    const wchar_t *voiceId;     // ID sent to inference server (e.g., "af_heart")
    const wchar_t *gender;      // "Female" or "Male"
    const wchar_t *language;    // LCID in hex (e.g., "409" for en-US)
    const wchar_t *age;         // "Adult", "Child", etc.
};

// All 11 Kokoro voices, matching server/models/kokoro_model.py
static const VoiceDefinition g_voices[] = {
    // American English Female voices
    {L"VoiceLink_af_heart", L"Heart (Kokoro)", L"af_heart", L"Female", L"409", L"Adult"},
    {L"VoiceLink_af_bella", L"Bella (Kokoro)", L"af_bella", L"Female", L"409", L"Adult"},
    {L"VoiceLink_af_nicole", L"Nicole (Kokoro)", L"af_nicole", L"Female", L"409", L"Adult"},
    {L"VoiceLink_af_sarah", L"Sarah (Kokoro)", L"af_sarah", L"Female", L"409", L"Adult"},
    {L"VoiceLink_af_sky", L"Sky (Kokoro)", L"af_sky", L"Female", L"409", L"Adult"},

    // American English Male voices
    {L"VoiceLink_am_adam", L"Adam (Kokoro)", L"am_adam", L"Male", L"409", L"Adult"},
    {L"VoiceLink_am_michael", L"Michael (Kokoro)", L"am_michael", L"Male", L"409", L"Adult"},

    // British English Female voices
    {L"VoiceLink_bf_emma", L"Emma (Kokoro)", L"bf_emma", L"Female", L"809", L"Adult"},
    {L"VoiceLink_bf_isabella", L"Isabella (Kokoro)", L"bf_isabella", L"Female", L"809", L"Adult"},

    // British English Male voices
    {L"VoiceLink_bm_george", L"George (Kokoro)", L"bm_george", L"Male", L"809", L"Adult"},
    {L"VoiceLink_bm_lewis", L"Lewis (Kokoro)", L"bm_lewis", L"Male", L"809", L"Adult"},
};

static constexpr size_t g_voiceCount = _countof(g_voices);

// ============================================================================
// DllMain — DLL Entry Point
// ============================================================================

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID /*lpReserved*/)
{
    switch (reason)
    {
    case DLL_PROCESS_ATTACH:
        // Save our module handle — we need it later to find our DLL path
        g_hModule = hModule;

        // Tell Windows we don't need DLL_THREAD_ATTACH/DETACH notifications.
        // This is a micro-optimization: Windows won't call DllMain for every
        // new thread, which saves a tiny bit of overhead in multi-threaded apps.
        DisableThreadLibraryCalls(hModule);

        VLOG(L"DLL loaded (DLL_PROCESS_ATTACH)");
        break;

    case DLL_PROCESS_DETACH:
        VLOG(L"DLL unloading (DLL_PROCESS_DETACH)");
        break;
    }
    return TRUE;
}

// ============================================================================
// DllGetClassObject — COM asks us for a class factory
// ============================================================================
//
// When an app calls CoCreateInstance(CLSID_VoiceLinkEngine, ...), COM:
//   1. Looks up CLSID in registry → finds our DLL path
//   2. Loads our DLL (DllMain fires)
//   3. Calls DllGetClassObject(CLSID_VoiceLinkEngine, IID_IClassFactory, ...)
//   4. We create a VoiceLinkClassFactory and return it
//   5. COM calls factory->CreateInstance() to get the actual engine
//
// The rclsid parameter is the CLSID of the class being requested.
// We only support CLSID_VoiceLinkEngine.

STDAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, LPVOID *ppv)
{
    VLOG(L"DllGetClassObject called");

    if (!ppv)
        return E_POINTER;
    *ppv = nullptr;

    // Only our engine class is supported
    if (rclsid != CLSID_VoiceLinkEngine)
    {
        VERR(L"Unknown CLSID requested");
        return CLASS_E_CLASSNOTAVAILABLE;
    }

    // Create a class factory
    auto *factory = new (std::nothrow) VoiceLinkClassFactory();
    if (!factory)
    {
        return E_OUTOFMEMORY;
    }

    // Ask the factory for the requested interface (usually IID_IClassFactory)
    HRESULT hr = factory->QueryInterface(riid, ppv);
    factory->Release(); // Balance the ref from new (constructor sets it to 1)

    return hr;
}

// ============================================================================
// DllCanUnloadNow — COM asks if we can be removed from memory
// ============================================================================
//
// COM periodically calls this to see if it can free our DLL.
// We return S_OK (yes, unload us) only when:
//   - No VoiceLinkEngine objects exist (g_objectCount == 0)
//   - No server locks are held (g_lockCount == 0)
//
// If we return S_FALSE, COM keeps us loaded.

STDAPI DllCanUnloadNow()
{
    BOOL canUnload = (g_lockCount == 0 && g_objectCount == 0);
    VLOG(L"DllCanUnloadNow: locks=%ld, objects=%ld → %s",
         g_lockCount, g_objectCount, canUnload ? L"YES" : L"NO");
    return canUnload ? S_OK : S_FALSE;
}

// ============================================================================
// Registry Helpers
// ============================================================================

// Get the full path to our DLL
static std::wstring GetDllPath()
{
    wchar_t path[MAX_PATH] = {};
    GetModuleFileNameW(g_hModule, path, MAX_PATH);
    return path;
}

// Convert a GUID to string form: {XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}
static std::wstring GuidToString(const GUID &guid)
{
    wchar_t buf[64] = {};
    StringFromGUID2(guid, buf, _countof(buf));
    return buf;
}

// Create a registry key and set its default value
static HRESULT CreateKeyWithDefault(HKEY hParent, const wchar_t *subkey,
                                    const wchar_t *defaultValue, HKEY *phkResult = nullptr)
{
    HKEY hKey = nullptr;
    LONG result = RegCreateKeyExW(
        hParent, subkey, 0, nullptr,
        REG_OPTION_NON_VOLATILE, KEY_WRITE, nullptr,
        &hKey, nullptr);

    if (result != ERROR_SUCCESS)
    {
        VERR(L"RegCreateKeyEx failed for %s: %ld", subkey, result);
        return HRESULT_FROM_WIN32(result);
    }

    if (defaultValue)
    {
        result = RegSetValueExW(
            hKey, nullptr, 0, REG_SZ,
            reinterpret_cast<const BYTE *>(defaultValue),
            static_cast<DWORD>((wcslen(defaultValue) + 1) * sizeof(wchar_t)));
    }

    if (phkResult)
    {
        *phkResult = hKey;
    }
    else
    {
        RegCloseKey(hKey);
    }

    return (result == ERROR_SUCCESS) ? S_OK : HRESULT_FROM_WIN32(result);
}

// Set a named string value on an open registry key
static HRESULT SetStringValue(HKEY hKey, const wchar_t *name, const wchar_t *value)
{
    LONG result = RegSetValueExW(
        hKey, name, 0, REG_SZ,
        reinterpret_cast<const BYTE *>(value),
        static_cast<DWORD>((wcslen(value) + 1) * sizeof(wchar_t)));
    return (result == ERROR_SUCCESS) ? S_OK : HRESULT_FROM_WIN32(result);
}

// Recursively delete a registry key and all its subkeys
static HRESULT DeleteKeyRecursive(HKEY hParent, const wchar_t *subkey)
{
    LONG result = RegDeleteTreeW(hParent, subkey);
    if (result == ERROR_FILE_NOT_FOUND)
        return S_OK; // Already gone, that's fine
    return (result == ERROR_SUCCESS) ? S_OK : HRESULT_FROM_WIN32(result);
}

// ============================================================================
// DllRegisterServer — Register the COM class and SAPI voice tokens
// ============================================================================
//
// When you run "regsvr32 voicelink_sapi.dll", this function creates:
//
// 1. COM Class Registration:
//    HKCR\CLSID\{D7A5E2B1-...}\
//      (Default) = "VoiceLink TTS Engine"
//      \InprocServer32\
//        (Default) = "C:\...\voicelink_sapi.dll"
//        ThreadingModel = "Both"
//
// 2. SAPI Voice Tokens (one per voice):
//    HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\VoiceLink_af_heart\
//      (Default) = "VoiceLink Kokoro Heart"
//      CLSID = "{D7A5E2B1-...}"
//      VoiceLinkVoiceId = "af_heart"
//      \Attributes\
//        Name = "VoiceLink Kokoro Heart"
//        Gender = "Female"
//        Language = "409"
//        Age = "Adult"
//        Vendor = "VoiceLink"
//
// After registration, every SAPI app will discover our voices.

STDAPI DllRegisterServer()
{
    VLOG(L"DllRegisterServer called");

    std::wstring dllPath = GetDllPath();
    std::wstring clsidStr = GuidToString(CLSID_VoiceLinkEngine);

    VLOG(L"DLL path: %s", dllPath.c_str());
    VLOG(L"CLSID: %s", clsidStr.c_str());

    // -----------------------------------------------------------------------
    // Step 1: Register the COM class in HKEY_CLASSES_ROOT\CLSID
    //
    // This tells COM: "CLSID {D7A5E2B1-...} maps to voicelink_sapi.dll"
    //
    // ThreadingModel = "Both" means our DLL works correctly in both
    // single-threaded apartment (STA) and multi-threaded apartment (MTA).
    // SAPI typically uses STA, but some apps use MTA.
    // -----------------------------------------------------------------------
    {
        std::wstring clsidKeyPath = std::wstring(L"CLSID\\") + clsidStr;

        HKEY hClsid = nullptr;
        HRESULT hr = CreateKeyWithDefault(HKEY_CLASSES_ROOT, clsidKeyPath.c_str(),
                                          L"VoiceLink TTS Engine", &hClsid);
        if (FAILED(hr))
            return hr;

        // InprocServer32 tells COM this is an in-process (DLL) server
        std::wstring inprocPath = clsidKeyPath + L"\\InprocServer32";
        HKEY hInproc = nullptr;
        hr = CreateKeyWithDefault(HKEY_CLASSES_ROOT, inprocPath.c_str(),
                                  dllPath.c_str(), &hInproc);
        if (FAILED(hr))
        {
            RegCloseKey(hClsid);
            return hr;
        }

        SetStringValue(hInproc, L"ThreadingModel", L"Both");
        RegCloseKey(hInproc);
        RegCloseKey(hClsid);

        VLOG(L"COM class registered");
    }

    // -----------------------------------------------------------------------
    // Step 2: Register each voice as a SAPI voice token
    //
    // Windows has TWO speech registries:
    //
    //   1. Classic SAPI 5:
    //      HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\
    //      Used by: PowerShell, Balabolka, older .NET apps, classic SAPI apps
    //
    //   2. OneCore (Modern Speech API):
    //      HKLM\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens\
    //      Used by: Thorium Reader, Chromium/Electron apps, Edge Read Aloud,
    //               Windows Narrator, UWP apps, modern .NET
    //
    // We register in BOTH so every app can discover VoiceLink voices.
    // The NaturalVoiceSAPIAdapter project does the same thing.
    // -----------------------------------------------------------------------
    const wchar_t *tokenRoots[] = {
        L"SOFTWARE\\Microsoft\\Speech\\Voices\\Tokens",
        L"SOFTWARE\\Microsoft\\Speech_OneCore\\Voices\\Tokens",
    };

    for (const wchar_t *tokensRoot : tokenRoots)
    {
        VLOG(L"Registering voices under: %s", tokensRoot);

        for (size_t i = 0; i < g_voiceCount; ++i)
        {
            const VoiceDefinition &voice = g_voices[i];

            std::wstring tokenPath = std::wstring(tokensRoot) + L"\\" + voice.tokenName;

            // Create the voice token key
            HKEY hToken = nullptr;
            HRESULT hr = CreateKeyWithDefault(HKEY_LOCAL_MACHINE, tokenPath.c_str(),
                                              voice.displayName, &hToken);
            if (FAILED(hr))
            {
                VERR(L"Failed to create token for %s", voice.tokenName);
                continue; // Try the next voice
            }

            // Set token values
            SetStringValue(hToken, L"CLSID", clsidStr.c_str());
            SetStringValue(hToken, L"VoiceLinkVoiceId", voice.voiceId);
            SetStringValue(hToken, L"VoiceLinkServerPort", L"7860");

            // Create the Attributes subkey
            HKEY hAttrs = nullptr;
            std::wstring attrsPath = tokenPath + L"\\Attributes";
            hr = CreateKeyWithDefault(HKEY_LOCAL_MACHINE, attrsPath.c_str(),
                                      nullptr, &hAttrs);
            if (SUCCEEDED(hr) && hAttrs)
            {
                SetStringValue(hAttrs, L"Name", voice.displayName);
                SetStringValue(hAttrs, L"Gender", voice.gender);
                SetStringValue(hAttrs, L"Language", voice.language);
                SetStringValue(hAttrs, L"Age", voice.age);
                SetStringValue(hAttrs, L"Vendor", L"VoiceLink");
                RegCloseKey(hAttrs);
            }

            RegCloseKey(hToken);
            VLOG(L"Registered voice: %s (%s)", voice.displayName, voice.voiceId);
        }
    }

    VLOG(L"DllRegisterServer completed: %zu voices x 2 registries", g_voiceCount);
    return S_OK;
}

// ============================================================================
// DllUnregisterServer — Remove all registry entries
// ============================================================================
//
// Called by "regsvr32 /u voicelink_sapi.dll".
// Removes everything DllRegisterServer created.

STDAPI DllUnregisterServer()
{
    VLOG(L"DllUnregisterServer called");

    std::wstring clsidStr = GuidToString(CLSID_VoiceLinkEngine);

    // -----------------------------------------------------------------------
    // Step 1: Remove the COM class registration
    // -----------------------------------------------------------------------
    {
        std::wstring clsidKeyPath = std::wstring(L"CLSID\\") + clsidStr;
        DeleteKeyRecursive(HKEY_CLASSES_ROOT, clsidKeyPath.c_str());
        VLOG(L"COM class unregistered");
    }

    // -----------------------------------------------------------------------
    // Step 2: Remove all voice tokens from BOTH registries
    // -----------------------------------------------------------------------
    const wchar_t *tokenRoots[] = {
        L"SOFTWARE\\Microsoft\\Speech\\Voices\\Tokens",
        L"SOFTWARE\\Microsoft\\Speech_OneCore\\Voices\\Tokens",
    };

    for (const wchar_t *tokensRoot : tokenRoots)
    {
        for (size_t i = 0; i < g_voiceCount; ++i)
        {
            std::wstring tokenPath = std::wstring(tokensRoot) + L"\\" + g_voices[i].tokenName;
            DeleteKeyRecursive(HKEY_LOCAL_MACHINE, tokenPath.c_str());
            VLOG(L"Unregistered voice: %s from %s", g_voices[i].tokenName, tokensRoot);
        }
    }

    VLOG(L"DllUnregisterServer completed");
    return S_OK;
}
