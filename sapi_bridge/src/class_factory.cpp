#include "class_factory.h"
#include "tts_engine.h"
#include "token_enum.h"
#include "debug.h"

// ============================================================================
// Constructor / Destructor
// ============================================================================

VoiceLinkClassFactory::VoiceLinkClassFactory(Kind kind)
    : m_kind(kind), m_refCount(1)
{
    InterlockedIncrement(&g_objectCount);
}

VoiceLinkClassFactory::~VoiceLinkClassFactory()
{
    InterlockedDecrement(&g_objectCount);
}

// ============================================================================
// IUnknown Implementation
// ============================================================================

STDMETHODIMP VoiceLinkClassFactory::QueryInterface(REFIID riid, void **ppv)
{
    if (!ppv)
        return E_POINTER;
    *ppv = nullptr;

    // We support IUnknown and IClassFactory
    if (riid == IID_IUnknown || riid == IID_IClassFactory)
    {
        *ppv = static_cast<IClassFactory *>(this);
        AddRef();
        return S_OK;
    }

    return E_NOINTERFACE;
}

STDMETHODIMP_(ULONG)
VoiceLinkClassFactory::AddRef()
{
    return InterlockedIncrement(&m_refCount);
}

STDMETHODIMP_(ULONG)
VoiceLinkClassFactory::Release()
{
    LONG count = InterlockedDecrement(&m_refCount);
    if (count == 0)
    {
        delete this;
    }
    return static_cast<ULONG>(count);
}

// ============================================================================
// IClassFactory Implementation
// ============================================================================

STDMETHODIMP VoiceLinkClassFactory::CreateInstance(
    IUnknown *pUnkOuter,
    REFIID riid,
    void **ppv)
{
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (pUnkOuter) return CLASS_E_NOAGGREGATION;

    try
    {
        IUnknown *object;
        if (m_kind == Kind::Engine)
        {
            object = static_cast<ISpTTSEngine *>(new VoiceLinkEngine());
        }
        else
        {
            auto *enumerator = new VoiceLinkTokenEnum();
            enumerator->Init(m_kind == Kind::EnumSapi ? VoiceLinkTokenEnum::Category::Sapi :
                                                        VoiceLinkTokenEnum::Category::OneCore);
            object = enumerator;
        }
        HRESULT hr = object->QueryInterface(riid, ppv);
        object->Release();
        return hr;
    }
    catch (const std::bad_alloc &)
    {
        return E_OUTOFMEMORY;
    }
}

STDMETHODIMP VoiceLinkClassFactory::LockServer(BOOL fLock)
{
    // LockServer prevents COM from unloading our DLL.
    // Some apps lock the server to keep the DLL loaded for fast
    // subsequent object creation.
    if (fLock)
    {
        InterlockedIncrement(&g_lockCount);
    }
    else
    {
        InterlockedDecrement(&g_lockCount);
    }
    return S_OK;
}
