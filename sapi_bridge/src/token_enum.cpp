#include "token_enum.h"
#include "voice_data_key.h"

#include <sapiddk.h>
#include <sperror.h>
#include <wrl/client.h>

using Microsoft::WRL::ComPtr;
extern LONG g_objectCount;

VoiceLinkTokenEnum::VoiceLinkTokenEnum() { InterlockedIncrement(&g_objectCount); }
VoiceLinkTokenEnum::~VoiceLinkTokenEnum()
{
    if (tokens_) tokens_->Release();
    InterlockedDecrement(&g_objectCount);
}

void VoiceLinkTokenEnum::Init(Category category) noexcept
{
    ComPtr<ISpObjectTokenEnumBuilder> builder;
    if (FAILED(CoCreateInstance(CLSID_SpObjectTokenEnum, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&builder))) ||
        FAILED(builder->SetAttribs(nullptr, nullptr)))
        return;

    const auto categoryId = category == Category::Sapi ? SPCAT_VOICES :
        L"HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Speech_OneCore\\Voices";
    DWORD port;
    for (auto &voice : LoadVoices(port))
    {
        try
        {
            const auto id = std::wstring(categoryId) + L"\\TokenEnums\\VoiceLink\\" + voice.id;
            ComPtr<ISpObjectTokenInit> token;
            if (FAILED(CoCreateInstance(CLSID_SpObjectToken, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&token))))
                continue;
            ComPtr<ISpDataKey> key;
            key.Attach(new VoiceDataKey(std::make_shared<VoiceEntry>(std::move(voice)), port));
            if (SUCCEEDED(token->InitFromDataKey(categoryId, id.c_str(), key.Get())))
            {
                ISpObjectToken *ptr = token.Get();
                builder->AddTokens(1, &ptr);
            }
        }
        catch (const std::bad_alloc &)
        {
            continue;
        }
    }
    tokens_ = builder.Detach();
}

STDMETHODIMP VoiceLinkTokenEnum::QueryInterface(REFIID iid, void **out)
{
    if (!out) return E_POINTER;
    *out = nullptr;
    if (iid != IID_IUnknown && iid != __uuidof(IEnumSpObjectTokens)) return E_NOINTERFACE;
    *out = static_cast<IEnumSpObjectTokens *>(this);
    AddRef();
    return S_OK;
}

STDMETHODIMP_(ULONG) VoiceLinkTokenEnum::AddRef() { return InterlockedIncrement(&refs_); }
STDMETHODIMP_(ULONG) VoiceLinkTokenEnum::Release()
{
    const ULONG refs = InterlockedDecrement(&refs_);
    if (!refs) delete this;
    return refs;
}

STDMETHODIMP VoiceLinkTokenEnum::Next(ULONG count, ISpObjectToken **tokens, ULONG *fetched)
{
    if (tokens_) return tokens_->Next(count, tokens, fetched);
    if (!tokens || (!fetched && count > 1)) return E_POINTER;
    if (fetched) *fetched = 0;
    if (count) *tokens = nullptr;
    return count ? S_FALSE : S_OK;
}

STDMETHODIMP VoiceLinkTokenEnum::Skip(ULONG count)
{
    if (tokens_) return tokens_->Skip(count);
    return count ? S_FALSE : S_OK;
}

STDMETHODIMP VoiceLinkTokenEnum::Reset() { return tokens_ ? tokens_->Reset() : S_OK; }

STDMETHODIMP VoiceLinkTokenEnum::Clone(IEnumSpObjectTokens **out)
{
    if (!out) return E_POINTER;
    if (tokens_) return tokens_->Clone(out);
    *out = new (std::nothrow) VoiceLinkTokenEnum;
    return *out ? S_OK : E_OUTOFMEMORY;
}

STDMETHODIMP VoiceLinkTokenEnum::Item(ULONG index, ISpObjectToken **out)
{
    if (!out) return E_POINTER;
    if (tokens_) return tokens_->Item(index, out);
    *out = nullptr;
    return SPERR_NO_MORE_ITEMS;
}

STDMETHODIMP VoiceLinkTokenEnum::GetCount(ULONG *count)
{
    if (!count) return E_POINTER;
    if (tokens_) return tokens_->GetCount(count);
    *count = 0;
    return S_OK;
}
