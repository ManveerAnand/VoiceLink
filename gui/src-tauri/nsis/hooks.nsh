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
;   - Kill any running VoiceLink / SAPI processes that lock files
;   - Unregister the COM DLL before overwriting
;   - Silently remove any prior VoiceLink installation
;   - Register/unregister BOTH 64-bit and 32-bit COM DLLs
;
; ARCHITECTURE NOTE:
;   We ship two copies of voicelink_sapi.dll:
;     voicelink_sapi.dll     — 64-bit (for Edge, Narrator, 64-bit apps)
;     voicelink_sapi_32.dll  — 32-bit (for Adobe Acrobat, 32-bit apps)
;
;   System32\regsvr32.exe is the 64-bit registrar (counterintuitive!)
;   SysWOW64\regsvr32.exe is the 32-bit registrar
;   Each writes to its own registry view automatically.
; ============================================================================

; --- Helper: forcefully stop VoiceLink and release the SAPI DLL ---
!macro _VoiceLink_KillProcesses
    ; 1. Kill the main Tauri GUI app (may be in system tray)
    DetailPrint "Stopping running VoiceLink processes..."
    nsExec::ExecToLog 'taskkill /F /IM "VoiceLink.exe" /T'
    nsExec::ExecToLog 'taskkill /F /IM "voicelink-gui.exe" /T'

    ; 2. Kill the Python TTS server if it was launched by VoiceLink
    nsExec::ExecToLog 'taskkill /F /IM "voicelink_server.exe" /T'

    ; 3. Unregister BOTH COM DLLs so SAPI releases handles
    ${If} ${FileExists} "$INSTDIR\voicelink_sapi.dll"
        DetailPrint "Unregistering previous 64-bit SAPI bridge..."
        ExecWait '$WINDIR\System32\regsvr32.exe /u /s "$INSTDIR\voicelink_sapi.dll"'
    ${EndIf}
    ${If} ${FileExists} "$INSTDIR\voicelink_sapi_32.dll"
        DetailPrint "Unregistering previous 32-bit SAPI bridge..."
        ExecWait '$WINDIR\SysWOW64\regsvr32.exe /u /s "$INSTDIR\voicelink_sapi_32.dll"'
    ${EndIf}

    ; 4. Brief pause to let OS release file handles
    Sleep 1500
!macroend

; --- Before Install: Kill processes, then remove previous version ---
!macro NSIS_HOOK_PREINSTALL
    ; Force-kill any running VoiceLink processes and unregister COM DLLs
    !insertmacro _VoiceLink_KillProcesses

    ; Check the standard Windows uninstall registry for a previous VoiceLink.
    ; Tauri writes its uninstaller path here during install.
    ReadRegStr $0 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceLink" "QuietUninstallString"
    ${If} $0 != ""
        DetailPrint "Removing previous VoiceLink installation..."
        ExecWait '$0' $1
        DetailPrint "Previous version removed (exit code $1)."
    ${Else}
        ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceLink" "QuietUninstallString"
        ${If} $0 != ""
            DetailPrint "Removing previous per-user VoiceLink installation..."
            ExecWait '$0' $1
            DetailPrint "Previous per-user version removed (exit code $1)."
        ${EndIf}
    ${EndIf}

    ; Safety net: if DLLs are still locked after uninstall, try once more
    ${If} ${FileExists} "$INSTDIR\voicelink_sapi.dll"
        DetailPrint "Verifying 64-bit DLL is unlocked..."
        Delete "$INSTDIR\voicelink_sapi.dll"
        ${If} ${FileExists} "$INSTDIR\voicelink_sapi.dll"
            nsExec::ExecToLog 'taskkill /F /IM "VoiceLink.exe" /T'
            nsExec::ExecToLog 'taskkill /F /IM "voicelink-gui.exe" /T'
            Sleep 2000
            Delete "$INSTDIR\voicelink_sapi.dll"
        ${EndIf}
    ${EndIf}
    ${If} ${FileExists} "$INSTDIR\voicelink_sapi_32.dll"
        Delete "$INSTDIR\voicelink_sapi_32.dll"
    ${EndIf}
!macroend

; --- After Install: Register BOTH COM DLLs ---
!macro NSIS_HOOK_POSTINSTALL
    ; Register 64-bit SAPI COM DLL (for 64-bit apps: Edge, Narrator, etc.)
    DetailPrint "Registering 64-bit SAPI bridge..."
    ExecWait '$WINDIR\System32\regsvr32.exe /s "$INSTDIR\voicelink_sapi.dll"' $0
    ${If} $0 == 0
        DetailPrint "64-bit SAPI bridge registered."
    ${Else}
        MessageBox MB_ICONEXCLAMATION "Failed to register 64-bit SAPI bridge (error $0). Voice synthesis may not work in 64-bit apps."
    ${EndIf}

    ; Register 32-bit SAPI COM DLL (for 32-bit apps: Adobe Acrobat, etc.)
    ${If} ${FileExists} "$INSTDIR\voicelink_sapi_32.dll"
        DetailPrint "Registering 32-bit SAPI bridge..."
        ExecWait '$WINDIR\SysWOW64\regsvr32.exe /s "$INSTDIR\voicelink_sapi_32.dll"' $0
        ${If} $0 == 0
            DetailPrint "32-bit SAPI bridge registered."
        ${Else}
            DetailPrint "Warning: 32-bit SAPI registration failed (error $0). 32-bit apps may not see VoiceLink voices."
        ${EndIf}
    ${EndIf}
!macroend

; --- Before Uninstall: Kill processes, then unregister BOTH COM DLLs ---
!macro NSIS_HOOK_PREUNINSTALL
    ; Force-kill any running VoiceLink processes first
    !insertmacro _VoiceLink_KillProcesses

    ; Unregister 64-bit COM DLL
    DetailPrint "Unregistering 64-bit SAPI bridge..."
    ExecWait '$WINDIR\System32\regsvr32.exe /u /s "$INSTDIR\voicelink_sapi.dll"' $0
    ${If} $0 == 0
        DetailPrint "64-bit SAPI bridge unregistered."
    ${Else}
        DetailPrint "Warning: Could not unregister 64-bit SAPI bridge (error $0)."
    ${EndIf}

    ; Unregister 32-bit COM DLL
    ${If} ${FileExists} "$INSTDIR\voicelink_sapi_32.dll"
        DetailPrint "Unregistering 32-bit SAPI bridge..."
        ExecWait '$WINDIR\SysWOW64\regsvr32.exe /u /s "$INSTDIR\voicelink_sapi_32.dll"' $0
        ${If} $0 == 0
            DetailPrint "32-bit SAPI bridge unregistered."
        ${Else}
            DetailPrint "Warning: Could not unregister 32-bit SAPI bridge (error $0)."
        ${EndIf}
    ${EndIf}
!macroend
