#include "voice_manifest.h"

#include <algorithm>
#include <utility>
#include <set>
#include <string_view>

namespace
{
struct BuiltinVoice
{
    const wchar_t *id, *name, *gender, *lang;
};

constexpr BuiltinVoice kBuiltinVoices[] = {
    {L"af_heart", L"Heart (Kokoro)", L"Female", L"409"},
    {L"af_bella", L"Bella (Kokoro)", L"Female", L"409"},
    {L"af_nicole", L"Nicole (Kokoro)", L"Female", L"409"},
    {L"af_sarah", L"Sarah (Kokoro)", L"Female", L"409"},
    {L"af_sky", L"Sky (Kokoro)", L"Female", L"409"},
    {L"am_adam", L"Adam (Kokoro)", L"Male", L"409"},
    {L"am_michael", L"Michael (Kokoro)", L"Male", L"409"},
    {L"bf_emma", L"Emma (Kokoro)", L"Female", L"809"},
    {L"bf_isabella", L"Isabella (Kokoro)", L"Female", L"809"},
    {L"bm_george", L"George (Kokoro)", L"Male", L"809"},
    {L"bm_lewis", L"Lewis (Kokoro)", L"Male", L"809"},
};

struct Key
{
    HKEY handle{};
    ~Key() { if (handle) RegCloseKey(handle); }
    LSTATUS Open(HKEY parent, const wchar_t *path)
    {
        return RegOpenKeyExW(parent, path, 0, KEY_READ, &handle);
    }
};

// Match the GUI's character counts and reject controls and broken UTF-16.
bool ValidText(std::wstring_view text, size_t limit)
{
    if (text.empty())
        return false;
    size_t count = 0;
    for (size_t i = 0; i < text.size(); ++i)
    {
        wchar_t c = text[i];
        if (++count > limit || c < 0x20 || (c >= 0x7f && c <= 0x9f))
            return false;
        if (IS_HIGH_SURROGATE(c))
        {
            if (++i == text.size() || !IS_LOW_SURROGATE(text[i]))
                return false;
        }
        else if (IS_LOW_SURROGATE(c))
            return false;
    }
    return true;
}

bool ReadString(HKEY key, const wchar_t *name, std::wstring &value)
{
    wchar_t buf[257];
    DWORD type = 0, bytes = sizeof(buf);
    if (RegQueryValueExW(key, name, nullptr, &type, reinterpret_cast<BYTE *>(buf), &bytes) != ERROR_SUCCESS ||
        type != REG_SZ || bytes < sizeof(wchar_t) || bytes % sizeof(wchar_t) != 0)
        return false;
    size_t length = bytes / sizeof(wchar_t);
    if (buf[length - 1] != L'\0' || !ValidText({buf, length - 1}, 128))
        return false;
    value.assign(buf, length - 1);
    return true;
}

void ReadEntries(HKEY root, std::vector<VoiceEntry> &voices)
{
    Key list;
    if (list.Open(root, L"Voices") != ERROR_SUCCESS)
        return;
    for (DWORD index = 0; voices.size() < 256; ++index)
    {
        wchar_t id[256]; // Maximum Windows registry key name plus terminator.
        DWORD length = _countof(id);
        if (RegEnumKeyExW(list.handle, index, id, &length, nullptr, nullptr, nullptr, nullptr) != ERROR_SUCCESS)
            break;
        std::wstring_view leaf(id, length);
        if (!ValidText(leaf, 150) || leaf.find_first_of(L"\\/'") != leaf.npos || leaf.find(L"..") != leaf.npos)
            continue;
        Key key;
        VoiceEntry v;
        if (key.Open(list.handle, id) != ERROR_SUCCESS ||
            !ReadString(key.handle, L"Name", v.name) ||
            !ReadString(key.handle, L"Gender", v.gender) || (v.gender != L"Female" && v.gender != L"Male") ||
            !ReadString(key.handle, L"Language", v.lang) || v.lang.size() > 4 ||
            v.lang.find_first_not_of(L"0123456789abcdefABCDEF") != v.lang.npos ||
            !ReadString(key.handle, L"Age", v.age) || v.age != L"Adult" ||
            !ReadString(key.handle, L"Model", v.model) || (v.model != L"kokoro" && v.model != L"qwen3"))
            continue;
        v.id.assign(leaf);
        voices.push_back(std::move(v));
    }
}

void SortAndDisambiguate(std::vector<VoiceEntry> &voices)
{
    auto byName = [](const VoiceEntry &a, const VoiceEntry &b) {
        return a.name == b.name ? a.id < b.id : a.name < b.name;
    };
    std::sort(voices.begin(), voices.end(), byName);
    std::set<std::wstring> used;
    for (const auto &v : voices)
        used.insert(v.name);
    std::wstring previous;
    for (auto &voice : voices)
    {
        if (voice.name != previous)
        {
            previous = voice.name;
            continue;
        }
        const auto suffix = L" (" + voice.id + L")";
        do { voice.name += suffix; } while (!used.insert(voice.name).second);
    }
    std::sort(voices.begin(), voices.end(), byName);
}
}

std::vector<VoiceEntry> LoadVoices(DWORD &port) noexcept
{
    port = 7860;
    try
    {
        Key root;
        LONG result = root.Open(HKEY_CURRENT_USER, L"Software\\VoiceLink");
        DWORD version = 0, bytes = sizeof(version);
        if (result == ERROR_SUCCESS)
        {
            DWORD configured = 0;
            if (RegGetValueW(root.handle, nullptr, L"ServerPort", RRF_RT_REG_DWORD, nullptr, &configured, &bytes) ==
                    ERROR_SUCCESS && bytes == sizeof(configured) && configured >= 1 && configured <= 65535)
                port = configured;
            bytes = sizeof(version);
            result = RegGetValueW(root.handle, nullptr, L"ManifestVersion", RRF_RT_REG_DWORD, nullptr, &version, &bytes);
        }
        std::vector<VoiceEntry> voices;
        if (result == ERROR_FILE_NOT_FOUND || result == ERROR_PATH_NOT_FOUND)
        {
            for (const auto &v : kBuiltinVoices)
                voices.push_back({v.id, v.name, v.gender, v.lang, L"Adult", L"kokoro"});
        }
        else if (result == ERROR_SUCCESS && bytes == sizeof(version) && version == 1)
            ReadEntries(root.handle, voices);

        SortAndDisambiguate(voices);
        return voices;
    }
    catch (...)
    {
        return {};
    }
}
