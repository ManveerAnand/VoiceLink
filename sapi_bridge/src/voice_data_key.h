#pragma once

#include "voice_manifest.h"
#include <sapi.h>
#include <memory>

class VoiceDataKey final : public ISpDataKey
{
public:
    VoiceDataKey(std::shared_ptr<const VoiceEntry> voice, DWORD port, bool attributes = false) noexcept;
    STDMETHODIMP QueryInterface(REFIID iid, void **out) override;
    STDMETHODIMP_(ULONG) AddRef() override;
    STDMETHODIMP_(ULONG) Release() override;
    STDMETHODIMP GetData(LPCWSTR name, ULONG *size, BYTE *data) override;
    STDMETHODIMP GetStringValue(LPCWSTR name, LPWSTR *value) override;
    STDMETHODIMP GetDWORD(LPCWSTR name, DWORD *value) override;
    STDMETHODIMP OpenKey(LPCWSTR name, ISpDataKey **key) override;
    STDMETHODIMP EnumKeys(ULONG index, LPWSTR *name) override;
    STDMETHODIMP EnumValues(ULONG index, LPWSTR *name) override;

    STDMETHODIMP SetData(LPCWSTR, ULONG, const BYTE *) override { return E_ACCESSDENIED; }
    STDMETHODIMP SetStringValue(LPCWSTR, LPCWSTR) override { return E_ACCESSDENIED; }
    STDMETHODIMP SetDWORD(LPCWSTR, DWORD) override { return E_ACCESSDENIED; }
    STDMETHODIMP DeleteKey(LPCWSTR) override { return E_ACCESSDENIED; }
    STDMETHODIMP DeleteValue(LPCWSTR) override { return E_ACCESSDENIED; }
    STDMETHODIMP CreateKey(LPCWSTR, ISpDataKey **key) override
    {
        if (key) *key = nullptr;
        return E_ACCESSDENIED;
    }

private:
    ~VoiceDataKey();
    const wchar_t *StringValue(LPCWSTR name) const;
    LONG refs_ = 1;
    std::shared_ptr<const VoiceEntry> voice_;
    DWORD port_;
    bool attributes_;
    wchar_t portText_[12]{};
    wchar_t clsid_[39]{};
};
