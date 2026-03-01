; ============================================================================
; VoiceLink — NSIS Installer Hooks
; ============================================================================
;
; These macros are called by Tauri's NSIS installer at specific points:
;   POSTINSTALL  — After files are copied to $INSTDIR
;   PREUNINSTALL — Before files are removed
;
; We use them to register/unregister the COM DLL with regsvr32,
; which writes the voice entries into HKLM\SOFTWARE\Microsoft\Speech\...
; ============================================================================

; --- After Install: Register the COM DLL ---
!macro NSIS_HOOK_POSTINSTALL
    ; Register the SAPI COM DLL so Windows sees our TTS voices
    ; regsvr32 writes to HKLM (admin required — installMode is perMachine)
    DetailPrint "Registering VoiceLink SAPI bridge..."
    ExecWait 'regsvr32 /s "$INSTDIR\resources\voicelink_sapi.dll"' $0
    ${If} $0 == 0
        DetailPrint "SAPI bridge registered successfully."
    ${Else}
        MessageBox MB_ICONEXCLAMATION "Failed to register SAPI bridge (error $0). Voice synthesis may not work."
    ${EndIf}
!macroend

; --- Before Uninstall: Unregister the COM DLL ---
!macro NSIS_HOOK_PREUNINSTALL
    ; Unregister the COM DLL (removes CLSID + voice tokens from registry)
    DetailPrint "Unregistering VoiceLink SAPI bridge..."
    ExecWait 'regsvr32 /u /s "$INSTDIR\resources\voicelink_sapi.dll"' $0
    ${If} $0 == 0
        DetailPrint "SAPI bridge unregistered."
    ${Else}
        DetailPrint "Warning: Could not unregister SAPI bridge (error $0)."
    ${EndIf}
!macroend
