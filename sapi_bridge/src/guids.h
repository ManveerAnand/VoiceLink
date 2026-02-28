// ============================================================================
// VoiceLink SAPI Bridge — GUID Definitions
// ============================================================================
//
// A GUID (Globally Unique Identifier) is a 128-bit number that uniquely
// identifies our COM class across ALL software on ALL computers in the world.
//
// When an app asks Windows "give me a VoiceLink engine", it uses our CLSID.
// Windows looks up this CLSID in the registry to find our DLL, loads it,
// and calls DllGetClassObject with this CLSID.
//
// HOW DEFINE_GUID WORKS:
//   - When INITGUID is defined (only in dllmain.cpp), DEFINE_GUID creates
//     the actual GUID constant in memory (allocates storage).
//   - In all other files, DEFINE_GUID creates an "extern" declaration
//     (just a reference, no storage).
//   - This ensures the GUID exists exactly once in the final DLL.
//
// GENERATING A GUID:
//   You can generate one with: PowerShell> [guid]::NewGuid()
//   Or in Visual Studio: Tools > Create GUID
//   The odds of collision are astronomically low (2^122 possibilities).
// ============================================================================

#pragma once

#include <windows.h>
#include <guiddef.h>

// {D7A5E2B1-3F8C-4E69-A1B4-7C2D9E0F5A38}
// CLSID for VoiceLinkEngine — the COM class that implements ISpTTSEngine.
//
// This is what goes in the registry:
//   HKCR\CLSID\{D7A5E2B1-3F8C-4E69-A1B4-7C2D9E0F5A38}\InprocServer32
//     (Default) = "C:\...\voicelink_sapi.dll"
//     ThreadingModel = "Both"
//
// And what each SAPI voice token references:
//   HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\VoiceLink_af_heart
//     CLSID = "{D7A5E2B1-3F8C-4E69-A1B4-7C2D9E0F5A38}"
//
// Every voice token points to the SAME CLSID because it's the same engine.
// The engine reads the token's attributes to know which specific voice to use.
//
// clang-format off
DEFINE_GUID(CLSID_VoiceLinkEngine,
    0xd7a5e2b1, 0x3f8c, 0x4e69,
    0xa1, 0xb4, 0x7c, 0x2d, 0x9e, 0x0f, 0x5a, 0x38);
// clang-format on
