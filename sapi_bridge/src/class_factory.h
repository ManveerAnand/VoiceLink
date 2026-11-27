#pragma once

#include <windows.h>
#include <unknwn.h>

// Global counters (defined in dllmain.cpp)
extern LONG g_lockCount;
extern LONG g_objectCount;

class VoiceLinkClassFactory : public IClassFactory
{
public:
    enum class Kind { Engine, EnumSapi, EnumOneCore };
    explicit VoiceLinkClassFactory(Kind kind);
    ~VoiceLinkClassFactory();

    // -----------------------------------------------------------------------
    // IUnknown — Same as VoiceLinkEngine (every COM object needs these)
    // -----------------------------------------------------------------------
    STDMETHODIMP QueryInterface(REFIID riid, void **ppv) override;
    STDMETHODIMP_(ULONG)
    AddRef() override;
    STDMETHODIMP_(ULONG)
    Release() override;

    // -----------------------------------------------------------------------
    // IClassFactory — The factory interface
    // -----------------------------------------------------------------------

    STDMETHODIMP CreateInstance(IUnknown *pUnkOuter, REFIID riid, void **ppv) override;

    // Lock or unlock the DLL in memory.
    //
    // When fLock is TRUE, increment a global lock count.
    // When FALSE, decrement it.
    // DllCanUnloadNow returns S_OK only when both lock count AND object count are 0.
    //
    // This prevents COM from unloading our DLL while a factory exists,
    // even if all objects have been Released.
    STDMETHODIMP LockServer(BOOL fLock) override;

private:
    Kind m_kind;
    LONG m_refCount;
};
