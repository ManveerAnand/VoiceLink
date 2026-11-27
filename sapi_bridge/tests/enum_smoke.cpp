// Run with the full path to the matching DLL. The live VoiceLink manifest is
// never edited: HKCU is redirected only inside this process.
#include <initguid.h>
#include "guids.h"
#include <sapi.h>
#include <sperror.h>
#include <wrl/client.h>
#include <cstdio>
#include <stdexcept>
#include <string>

using Microsoft::WRL::ComPtr;
using FactoryFn = HRESULT(STDAPICALLTYPE *)(REFCLSID, REFIID, void **);
using UnloadFn = HRESULT(STDAPICALLTYPE *)();

#define CHECK(expr) do { if (!(expr)) throw std::runtime_error(#expr); } while (false)
#define CHECK_HR(expr) do { HRESULT hr_ = (expr); if (FAILED(hr_)) { \
    std::fprintf(stderr, "HRESULT %08lX at line %d: ", static_cast<unsigned long>(hr_), __LINE__); \
    throw std::runtime_error(#expr); } } while (false)

struct Key
{
    HKEY key{};
    ~Key() { if (key) RegCloseKey(key); }
    void Create(HKEY parent, const wchar_t *name)
    {
        DWORD disposition;
        CHECK(RegCreateKeyExW(parent, name, 0, nullptr, REG_OPTION_VOLATILE, KEY_ALL_ACCESS,
            nullptr, &key, &disposition) == ERROR_SUCCESS && disposition == REG_CREATED_NEW_KEY);
    }
    void String(const wchar_t *name, const wchar_t *value)
    {
        CHECK(RegSetValueExW(key, name, 0, REG_SZ, reinterpret_cast<const BYTE *>(value),
            static_cast<DWORD>((wcslen(value) + 1) * sizeof(wchar_t))) == ERROR_SUCCESS);
    }
    void Number(const wchar_t *name, DWORD value)
    {
        CHECK(RegSetValueExW(key, name, 0, REG_DWORD,
            reinterpret_cast<const BYTE *>(&value), sizeof(value)) == ERROR_SUCCESS);
    }
};

template<class T>
ComPtr<T> Create(FactoryFn factoryFn, REFCLSID clsid)
{
    ComPtr<IClassFactory> factory;
    CHECK_HR(factoryFn(clsid, IID_PPV_ARGS(&factory)));
    ComPtr<T> out;
    CHECK_HR(factory->CreateInstance(nullptr, IID_PPV_ARGS(&out)));
    return out;
}

std::wstring Read(ISpDataKey *key, const wchar_t *name)
{
    wchar_t *text;
    CHECK_HR(key->GetStringValue(name, &text));
    std::wstring value(text);
    CoTaskMemFree(text);
    return value;
}

void Verify(FactoryFn factoryFn, REFCLSID clsid, const wchar_t *category, ULONG expected, bool custom)
{
    auto voices = Create<IEnumSpObjectTokens>(factoryFn, clsid);
    ULONG count;
    CHECK_HR(voices->GetCount(&count));
    CHECK(count == expected);
    std::wstring previous;
    for (ULONG i = 0; i < count; ++i)
    {
        ComPtr<ISpObjectToken> token;
        ULONG fetched;
        CHECK(voices->Next(1, &token, &fetched) == S_OK && fetched == 1);
        const auto id = Read(token.Get(), L"VoiceLinkVoiceId");
        wchar_t *tokenId;
        CHECK_HR(token->GetId(&tokenId));
        const auto wanted = std::wstring(category) + L"\\TokenEnums\\VoiceLink\\" + id;
        const bool idMatches = wanted == tokenId;
        CoTaskMemFree(tokenId);
        CHECK(idMatches);
        CHECK(Read(token.Get(), L"VoiceLinkModel") == (custom ? L"qwen3" : L"kokoro"));
        CHECK(Read(token.Get(), L"VoiceLinkServerPort") == (custom ? L"2468" : L"7860"));
        const auto name = Read(token.Get(), nullptr);
        CHECK(!name.empty() && previous < name);
        previous = name;
        ComPtr<ISpDataKey> attrs;
        CHECK_HR(token->OpenKey(L"Attributes", &attrs));
        CHECK(Read(attrs.Get(), L"Name") == name);
        CHECK(Read(attrs.Get(), L"Vendor") == L"VoiceLink");
        DWORD port = 0;
        CHECK_HR(token->GetDWORD(L"VoiceLinkServerPort", &port));
        CHECK(port == (custom ? 2468u : 7860u));
        ULONG bytes = 0;
        CHECK_HR(attrs->GetData(L"Vendor", &bytes, nullptr));
        wchar_t vendor[10]{};
        CHECK(bytes == sizeof(vendor));
        CHECK_HR(attrs->GetData(L"Vendor", &bytes, reinterpret_cast<BYTE *>(vendor)));
        CHECK(wcscmp(vendor, L"VoiceLink") == 0);
        wchar_t *valueName{};
        CHECK_HR(attrs->EnumValues(4, &valueName));
        const bool lastIsVendor = wcscmp(valueName, L"Vendor") == 0;
        CoTaskMemFree(valueName);
        CHECK(lastIsVendor && attrs->EnumValues(5, &valueName) == SPERR_NO_MORE_ITEMS);
        CHECK(attrs->SetStringValue(L"Name", L"changed") == E_ACCESSDENIED);
        BOOL matches = FALSE;
        CHECK_HR(token->MatchesAttributes(L"Vendor=VoiceLink", &matches));
        CHECK(matches);
        ComPtr<ISpeechObjectToken> automation;
        CHECK_HR(token.As(&automation));
        BSTR description{};
        CHECK_HR(automation->GetDescription(0, &description));
        const bool descriptionMatches = description && name == description;
        SysFreeString(description);
        CHECK(descriptionMatches);
        if (custom)
        {
            CHECK(id == L"fixture" && name == L"Stage zero");
            auto engine = Create<ISpObjectWithToken>(factoryFn, CLSID_VoiceLinkEngine);
            CHECK_HR(engine->SetObjectToken(token.Get()));
        }
    }
}

void Suite(FactoryFn fn)
{
    auto verifyBoth = [fn](ULONG count, bool custom = false) {
        Verify(fn, CLSID_VoiceLinkTokenEnumSapi, SPCAT_VOICES, count, custom);
        Verify(fn, CLSID_VoiceLinkTokenEnumOneCore,
            L"HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Speech_OneCore\\Voices", count, custom);
    };
    verifyBoth(11);
    Key software, root, voices, good, bad;
    software.Create(HKEY_CURRENT_USER, L"Software");
    root.Create(software.key, L"VoiceLink");
    root.Number(L"ManifestVersion", 1);
    verifyBoth(0);
    root.Number(L"ServerPort", 2468);
    voices.Create(root.key, L"Voices");
    good.Create(voices.key, L"fixture");
    good.String(L"Name", L"Stage zero");
    good.String(L"Gender", L"Female");
    good.String(L"Language", L"809");
    good.String(L"Age", L"Adult");
    good.String(L"Model", L"qwen3");
    bad.Create(voices.key, L"broken");
    bad.Number(L"Name", 23);
    verifyBoth(1, true);
    const BYTE shortVersion = 1;
    CHECK(RegSetValueExW(root.key, L"ManifestVersion", 0, REG_DWORD, &shortVersion, 1) == ERROR_SUCCESS);
    verifyBoth(0);
    root.Number(L"ManifestVersion", 1);
    good.String(L"Language", L"zzzz");
    verifyBoth(0);
}

int wmain(int argc, wchar_t **argv)
{
    if (argc != 2 || FAILED(CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED))) return 2;
    HMODULE module = LoadLibraryW(argv[1]);
    int result = 1;
    Key hive;
    // A unique key avoids deleting another run's fixture on cleanup.
    GUID id;
    wchar_t suffix[39];
    if (FAILED(CoCreateGuid(&id))) return 2;
    StringFromGUID2(id, suffix, _countof(suffix));
    const auto path = std::wstring(L"Software\\VoiceLink-Stage0-") + suffix;
    bool redirected = false;
    try
    {
        CHECK(module);
        const auto factory = reinterpret_cast<FactoryFn>(GetProcAddress(module, "DllGetClassObject"));
        const auto unload = reinterpret_cast<UnloadFn>(GetProcAddress(module, "DllCanUnloadNow"));
        CHECK(factory && unload);
        hive.Create(HKEY_CURRENT_USER, path.c_str());
        CHECK(RegOverridePredefKey(HKEY_CURRENT_USER, hive.key) == ERROR_SUCCESS);
        redirected = true;
        Suite(factory);
        CHECK(unload() == S_OK);
        result = 0;
    }
    catch (const std::exception &ex)
    {
        std::fprintf(stderr, "FAIL %s\n", ex.what());
    }
    if (redirected && RegOverridePredefKey(HKEY_CURRENT_USER, nullptr) != ERROR_SUCCESS) return 2;
    if (hive.key && RegDeleteTreeW(HKEY_CURRENT_USER, path.c_str()) != ERROR_SUCCESS) return 2;
    CoUninitialize();
    if (module) FreeLibrary(module);
    if (!result) std::printf("PASS %zu-bit SAPI + OneCore: default, empty, custom, malformed, attributes, description, engine, unload\n", sizeof(void*) * 8);
    return result;
}
