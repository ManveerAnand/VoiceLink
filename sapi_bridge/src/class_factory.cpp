// ============================================================================
// VoiceLink SAPI Bridge — Class Factory (Implementation)
// ============================================================================
//
// This is a textbook COM class factory. Its only real method is
// CreateInstance(), which does: new VoiceLinkEngine() → QueryInterface().
//
// Everything else (IUnknown, LockServer) is standard boilerplate that
// every COM DLL implements identically.
// ============================================================================

#include "class_factory.h"
#include "tts_engine.h"
#include "debug.h"

// ============================================================================
// Constructor / Destructor
// ============================================================================

VoiceLinkClassFactory::VoiceLinkClassFactory()
    : m_refCount(1)
{
    VLOG(L"VoiceLinkClassFactory created");
}

VoiceLinkClassFactory::~VoiceLinkClassFactory() {
    VLOG(L"VoiceLinkClassFactory destroyed");
}

// ============================================================================
// IUnknown Implementation
// ============================================================================

STDMETHODIMP VoiceLinkClassFactory::QueryInterface(REFIID riid, void** ppv) {
    if (!ppv) return E_POINTER;
    *ppv = nullptr;

    // We support IUnknown and IClassFactory
    if (riid == IID_IUnknown || riid == IID_IClassFactory) {
        *ppv = static_cast<IClassFactory*>(this);
        AddRef();
        return S_OK;
    }

    return E_NOINTERFACE;
}

STDMETHODIMP_(ULONG) VoiceLinkClassFactory::AddRef() {
    return InterlockedIncrement(&m_refCount);
}

STDMETHODIMP_(ULONG) VoiceLinkClassFactory::Release() {
    LONG count = InterlockedDecrement(&m_refCount);
    if (count == 0) {
        delete this;
    }
    return static_cast<ULONG>(count);
}

// ============================================================================
// IClassFactory Implementation
// ============================================================================

STDMETHODIMP VoiceLinkClassFactory::CreateInstance(
    IUnknown* pUnkOuter,
    REFIID riid,
    void** ppv
) {
    // COM aggregation is an advanced feature where one object can appear
    // to be part of another object. We don't support it.
    // If pUnkOuter is non-null, the caller is trying to aggregate us.
    if (pUnkOuter != nullptr) {
        return CLASS_E_NOAGGREGATION;
    }

    if (!ppv) return E_POINTER;
    *ppv = nullptr;

    // Create the engine object
    // The constructor sets ref count to 1
    auto* engine = new (std::nothrow) VoiceLinkEngine();
    if (!engine) {
        VERR(L"Failed to allocate VoiceLinkEngine");
        return E_OUTOFMEMORY;
    }

    // Ask the engine for the interface the caller wants.
    // This also AddRef's the returned interface pointer.
    HRESULT hr = engine->QueryInterface(riid, ppv);

    // Release our initial reference. If QI succeeded, the object survives
    // (ref count goes from 2 back to 1). If QI failed, this destroys it
    // (ref count goes from 1 to 0).
    engine->Release();

    if (FAILED(hr)) {
        VERR(L"VoiceLinkEngine doesn't support requested interface");
    } else {
        VLOG(L"VoiceLinkEngine instance created successfully");
    }

    return hr;
}

STDMETHODIMP VoiceLinkClassFactory::LockServer(BOOL fLock) {
    // LockServer prevents COM from unloading our DLL.
    // Some apps lock the server to keep the DLL loaded for fast
    // subsequent object creation.
    if (fLock) {
        InterlockedIncrement(&g_lockCount);
    } else {
        InterlockedDecrement(&g_lockCount);
    }
    return S_OK;
}
