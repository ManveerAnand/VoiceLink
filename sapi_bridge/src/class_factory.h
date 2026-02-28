// ============================================================================
// VoiceLink SAPI Bridge — Class Factory (Header)
// ============================================================================
//
// In COM, you never directly "new" an object from outside the DLL.
// Instead, COM asks the DLL for a "class factory" that knows how to
// create objects.
//
// The flow:
//   1. App calls CoCreateInstance(CLSID_VoiceLinkEngine, ...)
//   2. COM calls our DllGetClassObject(CLSID_VoiceLinkEngine, IID_IClassFactory, ...)
//   3. DllGetClassObject creates a VoiceLinkClassFactory and returns it
//   4. COM calls factory->CreateInstance(nullptr, IID_ISpTTSEngine, ...)
//   5. The factory creates a VoiceLinkEngine and returns it
//   6. COM releases the factory (it's no longer needed)
//
// WHY THIS INDIRECTION?
//   - Separation of concerns: creation logic is separate from the object itself
//   - COM can ask for different types of factories in the future
//   - The factory can implement pooling, caching, or other creation strategies
//   - Standard COM pattern — every COM DLL does it this way
//
// In practice, our factory is extremely simple: it just does "new VoiceLinkEngine".
// But the pattern must be followed for COM compatibility.
// ============================================================================

#pragma once

#include <windows.h>
#include <unknwn.h>

// Global counters (defined in dllmain.cpp)
extern LONG g_lockCount;

class VoiceLinkClassFactory : public IClassFactory {
public:
    VoiceLinkClassFactory();
    ~VoiceLinkClassFactory();

    // -----------------------------------------------------------------------
    // IUnknown — Same as VoiceLinkEngine (every COM object needs these)
    // -----------------------------------------------------------------------
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override;
    STDMETHODIMP_(ULONG) AddRef() override;
    STDMETHODIMP_(ULONG) Release() override;

    // -----------------------------------------------------------------------
    // IClassFactory — The factory interface
    // -----------------------------------------------------------------------

    // Create an instance of VoiceLinkEngine.
    //
    // Parameters:
    //   pUnkOuter — For COM aggregation (we don't support it, must be nullptr)
    //   riid      — The interface the caller wants (usually IID_ISpTTSEngine)
    //   ppv       — Output: pointer to the newly created object
    //
    // This is essentially: *ppv = new VoiceLinkEngine() with QI for riid.
    STDMETHODIMP CreateInstance(IUnknown* pUnkOuter, REFIID riid, void** ppv) override;

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
    LONG m_refCount;
};
