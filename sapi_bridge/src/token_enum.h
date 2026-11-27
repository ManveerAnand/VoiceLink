#pragma once

#include <sapi.h>

class VoiceLinkTokenEnum final : public IEnumSpObjectTokens
{
public:
    enum class Category { Sapi, OneCore };
    VoiceLinkTokenEnum();
    void Init(Category category) noexcept;
    STDMETHODIMP QueryInterface(REFIID iid, void **out) override;
    STDMETHODIMP_(ULONG) AddRef() override;
    STDMETHODIMP_(ULONG) Release() override;
    STDMETHODIMP Next(ULONG count, ISpObjectToken **tokens, ULONG *fetched) override;
    STDMETHODIMP Skip(ULONG count) override;
    STDMETHODIMP Reset() override;
    STDMETHODIMP Clone(IEnumSpObjectTokens **out) override;
    STDMETHODIMP Item(ULONG index, ISpObjectToken **out) override;
    STDMETHODIMP GetCount(ULONG *count) override;

private:
    ~VoiceLinkTokenEnum();
    LONG refs_ = 1;
    IEnumSpObjectTokens *tokens_ = nullptr;
};
