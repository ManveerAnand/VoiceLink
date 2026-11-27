#include "voice_data_key.h"
#include "guids.h"

#include <sperror.h>

extern LONG g_objectCount;

namespace
{
const wchar_t *const rootNames[] = {L"", L"CLSID", L"VoiceLinkVoiceId", L"VoiceLinkServerPort", L"VoiceLinkModel"};
const wchar_t *const attrNames[] = {L"Name", L"Gender", L"Language", L"Age", L"Vendor"};

HRESULT CopyString(const wchar_t *text, wchar_t **out, HRESULT missing)
{
    if (!out) return E_POINTER;
    *out = nullptr;
    if (!text) return missing;
    const size_t bytes = (wcslen(text) + 1) * sizeof(wchar_t);
    *out = static_cast<wchar_t *>(CoTaskMemAlloc(bytes));
    if (!*out) return E_OUTOFMEMORY;
    memcpy(*out, text, bytes);
    return S_OK;
}
}

VoiceDataKey::VoiceDataKey(std::shared_ptr<const VoiceEntry> voice, DWORD port, bool attributes) noexcept
    : voice_(std::move(voice)), port_(port), attributes_(attributes)
{
    _ultow_s(port_, portText_, 10);
    StringFromGUID2(CLSID_VoiceLinkEngine, clsid_, _countof(clsid_));
    InterlockedIncrement(&g_objectCount);
}

VoiceDataKey::~VoiceDataKey() { InterlockedDecrement(&g_objectCount); }

STDMETHODIMP VoiceDataKey::QueryInterface(REFIID iid, void **out)
{
    if (!out) return E_POINTER;
    *out = nullptr;
    if (iid != IID_IUnknown && iid != __uuidof(ISpDataKey)) return E_NOINTERFACE;
    *out = static_cast<ISpDataKey *>(this);
    AddRef();
    return S_OK;
}

STDMETHODIMP_(ULONG) VoiceDataKey::AddRef() { return InterlockedIncrement(&refs_); }
STDMETHODIMP_(ULONG) VoiceDataKey::Release()
{
    const ULONG refs = InterlockedDecrement(&refs_);
    if (!refs) delete this;
    return refs;
}

const wchar_t *VoiceDataKey::StringValue(LPCWSTR name) const
{
    const wchar_t *root[] = {voice_->name.c_str(), clsid_, voice_->id.c_str(), portText_, voice_->model.c_str()};
    const wchar_t *attrs[] = {voice_->name.c_str(), voice_->gender.c_str(), voice_->lang.c_str(), voice_->age.c_str(), L"VoiceLink"};
    static_assert(_countof(root) == _countof(rootNames) && _countof(attrs) == _countof(attrNames));
    const auto names = attributes_ ? attrNames : rootNames;
    const size_t count = attributes_ ? _countof(attrs) : _countof(root);
    for (size_t i = 0; i < count; ++i)
        if (_wcsicmp(name ? name : L"", names[i]) == 0)
            return attributes_ ? attrs[i] : root[i];
    return nullptr;
}

STDMETHODIMP VoiceDataKey::GetStringValue(LPCWSTR name, LPWSTR *out)
{
    return CopyString(StringValue(name), out, SPERR_NOT_FOUND);
}

STDMETHODIMP VoiceDataKey::GetData(LPCWSTR name, ULONG *size, BYTE *data)
{
    if (!size) return E_POINTER;
    const auto value = StringValue(name);
    if (!value) return SPERR_NOT_FOUND;
    const ULONG bytes = static_cast<ULONG>((wcslen(value) + 1) * sizeof(wchar_t));
    const ULONG available = *size;
    *size = bytes;
    if (!data) return S_OK;
    if (available < bytes) return SPERR_BUFFER_TOO_SMALL;
    memcpy(data, value, bytes);
    return S_OK;
}

STDMETHODIMP VoiceDataKey::GetDWORD(LPCWSTR name, DWORD *out)
{
    if (!out) return E_POINTER;
    if (attributes_ || !name || _wcsicmp(name, L"VoiceLinkServerPort") != 0) return SPERR_NOT_FOUND;
    *out = port_;
    return S_OK;
}

STDMETHODIMP VoiceDataKey::OpenKey(LPCWSTR name, ISpDataKey **key)
{
    if (!key) return E_POINTER;
    *key = nullptr;
    if (attributes_ || !name || _wcsicmp(name, L"Attributes") != 0) return SPERR_NOT_FOUND;
    *key = new (std::nothrow) VoiceDataKey(voice_, port_, true);
    return *key ? S_OK : E_OUTOFMEMORY;
}

STDMETHODIMP VoiceDataKey::EnumKeys(ULONG index, LPWSTR *name)
{
    return CopyString(!attributes_ && index == 0 ? L"Attributes" : nullptr, name, SPERR_NO_MORE_ITEMS);
}

STDMETHODIMP VoiceDataKey::EnumValues(ULONG index, LPWSTR *name)
{
    const auto names = attributes_ ? attrNames : rootNames;
    const auto count = attributes_ ? _countof(attrNames) : _countof(rootNames);
    return CopyString(index < count ? names[index] : nullptr, name, SPERR_NO_MORE_ITEMS);
}
