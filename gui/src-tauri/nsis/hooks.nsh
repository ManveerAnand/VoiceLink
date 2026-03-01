; ============================================================================
; VoiceLink — NSIS Installer Hooks
; ============================================================================
;
; These macros are called by Tauri's NSIS installer at specific points:
;   PREINSTALL   — Before files are copied (we uninstall previous version)
;   POSTINSTALL  — After files are copied to $INSTDIR
;   PREUNINSTALL — Before files are removed
;
; We use them to:
;   - Silently remove any prior VoiceLink installation
;   - Register/unregister the COM DLL with regsvr32
; ============================================================================

; --- Before Install: Remove any previous version ---
!macro NSIS_HOOK_PREINSTALL
    ; Check the standard Windows uninstall registry for a previous VoiceLink.
    ; Tauri writes its uninstaller path here during install.
    ; We read it and run it silently (/S) so the user gets a clean upgrade.
    ReadRegStr $0 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceLink" "QuietUninstallString"
    ${If} $0 != ""
        DetailPrint "Removing previous VoiceLink installation..."
        ; Unregister the old COM DLL first (the old uninstaller hook may not
        ; fire correctly if the DLL path changed between versions)
        ${If} ${FileExists} "$INSTDIR\voicelink_sapi.dll"
            ExecWait 'regsvr32 /u /s "$INSTDIR\voicelink_sapi.dll"'
        ${EndIf}
        ; Run the previous uninstaller silently
        ExecWait '$0' $1
        DetailPrint "Previous version removed (exit code $1)."
    ${Else}
        ; Also check the per-user key in case it was installed per-user before
        ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceLink" "QuietUninstallString"
        ${If} $0 != ""
            DetailPrint "Removing previous per-user VoiceLink installation..."
            ExecWait '$0' $1
            DetailPrint "Previous per-user version removed (exit code $1)."
        ${EndIf}
    ${EndIf}
!macroend

; --- After Install: Register the COM DLL ---
!macro NSIS_HOOK_POSTINSTALL
    ; Register the SAPI COM DLL so Windows sees our TTS voices
    ; regsvr32 writes to HKLM (admin required — installMode is perMachine)
    DetailPrint "Registering VoiceLink SAPI bridge..."
    ExecWait 'regsvr32 /s "$INSTDIR\voicelink_sapi.dll"' $0
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
    ExecWait 'regsvr32 /u /s "$INSTDIR\voicelink_sapi.dll"' $0
    ${If} $0 == 0
        DetailPrint "SAPI bridge unregistered."
    ${Else}
        DetailPrint "Warning: Could not unregister SAPI bridge (error $0)."
    ${EndIf}
!macroend
