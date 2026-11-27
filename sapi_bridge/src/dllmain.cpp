// In-process SAPI engine and the two category-specific token enumerators.
#include <initguid.h>
#include "guids.h"
#include "class_factory.h"

#include <new>
#include <cwchar>

HMODULE g_hModule = nullptr;
LONG g_lockCount = 0;
LONG g_objectCount = 0;

namespace
{
using Kind = VoiceLinkClassFactory::Kind;
struct ComClass
{
    const CLSID &clsid;
    Kind kind;
    const wchar_t *description;
    const wchar_t *enumKey;
};
const ComClass classes[] = {
    {CLSID_VoiceLinkEngine, Kind::Engine, L"VoiceLink TTS Engine", nullptr},
    {CLSID_VoiceLinkTokenEnumSapi, Kind::EnumSapi, L"VoiceLink SAPI token enumerator",
        L"SOFTWARE\\Microsoft\\Speech\\Voices\\TokenEnums\\VoiceLink"},
    {CLSID_VoiceLinkTokenEnumOneCore, Kind::EnumOneCore, L"VoiceLink OneCore token enumerator",
        L"SOFTWARE\\Microsoft\\Speech_OneCore\\Voices\\TokenEnums\\VoiceLink"},
};

bool Missing(LSTATUS status)
{
    return status == ERROR_FILE_NOT_FOUND || status == ERROR_PATH_NOT_FOUND;
}

LSTATUS WriteString(HKEY root, const wchar_t *key, const wchar_t *name, const wchar_t *value)
{
    return RegSetKeyValueW(root, key, name, REG_SZ, value,
        static_cast<DWORD>((wcslen(value) + 1) * sizeof(wchar_t)));
}

LSTATUS PurgeLegacyTokens(HKEY root, const wchar_t *path)
{
    HKEY tokens;
    LSTATUS status = RegOpenKeyExW(root, path, 0, KEY_ENUMERATE_SUB_KEYS | KEY_QUERY_VALUE |
                                   KEY_SET_VALUE | DELETE, &tokens);
    if (Missing(status)) return ERROR_SUCCESS;
    if (status != ERROR_SUCCESS) return status;
    DWORD index = 0;
    for (;;)
    {
        wchar_t name[256];
        DWORD length = _countof(name);
        status = RegEnumKeyExW(tokens, index, name, &length, nullptr, nullptr, nullptr, nullptr);
        if (status != ERROR_SUCCESS) break;
        if (_wcsnicmp(name, L"VoiceLink_", 10) == 0)
        {
            status = RegDeleteTreeW(tokens, name);
            if (status != ERROR_SUCCESS && !Missing(status)) break;
            // The next sibling shifts into this index.
        }
        else
            ++index;
    }
    RegCloseKey(tokens);
    return status == ERROR_NO_MORE_ITEMS ? ERROR_SUCCESS : status;
}

void RecordError(LSTATUS &first, LSTATUS result)
{
    if (first == ERROR_SUCCESS && result != ERROR_SUCCESS && !Missing(result))
        first = result;
}

LSTATUS PurgeLegacyTokens()
{
    constexpr auto sapi = L"SOFTWARE\\Microsoft\\Speech\\Voices\\Tokens";
    constexpr auto oneCore = L"SOFTWARE\\Microsoft\\Speech_OneCore\\Voices\\Tokens";
    LSTATUS result = PurgeLegacyTokens(HKEY_LOCAL_MACHINE, sapi);
    RecordError(result, PurgeLegacyTokens(HKEY_LOCAL_MACHINE, oneCore));
    RecordError(result, PurgeLegacyTokens(HKEY_CURRENT_USER, sapi));
    return result;
}
}

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH)
    {
        g_hModule = module;
        DisableThreadLibraryCalls(module);
    }
    return TRUE;
}

STDAPI DllGetClassObject(REFCLSID clsid, REFIID iid, void **out)
{
    if (!out) return E_POINTER;
    *out = nullptr;
    for (const auto &entry : classes)
    {
        if (clsid != entry.clsid) continue;
        auto *factory = new (std::nothrow) VoiceLinkClassFactory(entry.kind);
        if (!factory) return E_OUTOFMEMORY;
        HRESULT hr = factory->QueryInterface(iid, out);
        factory->Release();
        return hr;
    }
    return CLASS_E_CLASSNOTAVAILABLE;
}

STDAPI DllCanUnloadNow()
{
    return InterlockedCompareExchange(&g_lockCount, 0, 0) == 0 &&
           InterlockedCompareExchange(&g_objectCount, 0, 0) == 0 ? S_OK : S_FALSE;
}

STDAPI DllRegisterServer()
{
    wchar_t path[32768];
    DWORD length = GetModuleFileNameW(g_hModule, path, _countof(path));
    if (!length || length == _countof(path)) return HRESULT_FROM_WIN32(GetLastError());

    for (const auto &entry : classes)
    {
        wchar_t guid[39], key[128], inproc[160];
        StringFromGUID2(entry.clsid, guid, _countof(guid));
        swprintf_s(key, L"SOFTWARE\\Classes\\CLSID\\%s", guid);
        swprintf_s(inproc, L"%s\\InprocServer32", key);
        LSTATUS status = WriteString(HKEY_LOCAL_MACHINE, key, nullptr, entry.description);
        if (status == ERROR_SUCCESS) status = WriteString(HKEY_LOCAL_MACHINE, inproc, nullptr, path);
        if (status == ERROR_SUCCESS) status = WriteString(HKEY_LOCAL_MACHINE, inproc, L"ThreadingModel", L"Both");
        if (status == ERROR_SUCCESS && entry.enumKey)
            status = WriteString(HKEY_LOCAL_MACHINE, entry.enumKey, L"CLSID", guid);
        if (status != ERROR_SUCCESS) return HRESULT_FROM_WIN32(status);
    }
    return HRESULT_FROM_WIN32(PurgeLegacyTokens());
}

STDAPI DllUnregisterServer()
{
    LSTATUS result = ERROR_SUCCESS;
    for (const auto &entry : classes)
    {
        wchar_t guid[39], key[128];
        StringFromGUID2(entry.clsid, guid, _countof(guid));
        swprintf_s(key, L"SOFTWARE\\Classes\\CLSID\\%s", guid);
        if (entry.enumKey) RecordError(result, RegDeleteTreeW(HKEY_LOCAL_MACHINE, entry.enumKey));
        RecordError(result, RegDeleteTreeW(HKEY_LOCAL_MACHINE, key));
    }
    RecordError(result, PurgeLegacyTokens());
    return HRESULT_FROM_WIN32(result);
}
