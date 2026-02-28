# ============================================================================
# VoiceLink Research - Step 01: Explore Windows SAPI
# ============================================================================
# 
# PURPOSE: Understand how Windows discovers and uses TTS voices.
# This is exactly what Thorium Reader (and any SAPI app) does internally.
#
# WHAT WE LEARNED:
# 
# 1. SAPI voices are registered in the Windows Registry under:
#    HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\{VoiceName}
#
# 2. Each voice token has a CLSID (COM class ID) that points to a DLL
#    The CLSID is registered under:
#    HKLM\SOFTWARE\Classes\CLSID\{CLSID}\InprocServer32 → DLL path
#
# 3. The full chain is:
#    App calls SAPI → SAPI reads Registry → Finds CLSID → Finds DLL → Loads DLL → Calls Speak()
#
# 4. For VoiceLink, we need to:
#    a) Create our own CLSID
#    b) Build a DLL that implements ISpTTSEngine
#    c) Register it in both locations in the Registry
#    d) Our DLL forwards text to our AI server and returns audio
#
# ============================================================================

Write-Host "============================================" -ForegroundColor Green
Write-Host "  VoiceLink Research: SAPI Voice Explorer" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green

# --- STEP 1: List all installed SAPI voices ---
Write-Host "`n--- STEP 1: Installed SAPI Voices ---`n" -ForegroundColor Yellow

Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer

$voices = $synth.GetInstalledVoices()
Write-Host "Found $($voices.Count) voice(s):`n"

foreach ($voice in $voices) {
    $v = $voice.VoiceInfo
    Write-Host "  Voice: $($v.Name)" -ForegroundColor Cyan
    Write-Host "    Culture:  $($v.Culture)"
    Write-Host "    Gender:   $($v.Gender)"
    Write-Host "    Age:      $($v.Age)"
    Write-Host "    ID:       $($v.Id)"
    Write-Host ""
}

# --- STEP 2: Inspect the Registry (Voice Tokens) ---
Write-Host "--- STEP 2: Registry Voice Tokens ---`n" -ForegroundColor Yellow
Write-Host "Location: HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\`n"

$tokenPath = "HKLM:\SOFTWARE\Microsoft\Speech\Voices\Tokens"
Get-ChildItem $tokenPath | ForEach-Object {
    $props = Get-ItemProperty $_.PSPath
    Write-Host "  Token: $($_.PSChildName)" -ForegroundColor Cyan
    Write-Host "    Display Name: $($props.'(default)')"
    Write-Host "    CLSID:        $($props.CLSID)"
    
    # Now trace the CLSID to its DLL
    $clsid = $props.CLSID
    if ($clsid) {
        $inprocPath = "HKLM:\SOFTWARE\Classes\CLSID\$clsid\InprocServer32"
        if (Test-Path $inprocPath) {
            $inproc = Get-ItemProperty $inprocPath
            Write-Host "    DLL Path:     $($inproc.'(default)')" -ForegroundColor Green
            Write-Host "    Threading:    $($inproc.ThreadingModel)"
        }
    }
    Write-Host ""
}

# --- STEP 3: Also check OneCore voices (newer Windows voices) ---
Write-Host "--- STEP 3: OneCore Voices (if any) ---`n" -ForegroundColor Yellow
Write-Host "Location: HKLM\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens\`n"

$oneCorePath = "HKLM:\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens"
if (Test-Path $oneCorePath) {
    $oneCoreVoices = Get-ChildItem $oneCorePath
    Write-Host "Found $($oneCoreVoices.Count) OneCore voice(s):`n"
    
    foreach ($item in $oneCoreVoices) {
        $props = Get-ItemProperty $item.PSPath
        Write-Host "  Token: $($item.PSChildName)" -ForegroundColor Cyan
        Write-Host "    Display Name: $($props.'(default)')"
        Write-Host "    CLSID:        $($props.CLSID)"
        Write-Host ""
    }
} else {
    Write-Host "  No OneCore voices path found.`n"
}

# --- STEP 4: What VoiceLink needs to look like in the Registry ---
Write-Host "--- STEP 4: What VoiceLink Registry Entry Would Look Like ---`n" -ForegroundColor Yellow
Write-Host @"
  To register VoiceLink as a SAPI voice, we need TWO registry entries:

  1) Voice Token:
     HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\VoiceLink_Kokoro
       (default)  = "VoiceLink - Kokoro (Neural)"
       CLSID      = {OUR-GUID-GOES-HERE}
       409        = "VoiceLink - Kokoro (Neural)"

     Attributes subkey:
       Gender     = "Female"
       Language   = "409"       (en-US)
       Name       = "VoiceLink - Kokoro"
       Vendor     = "VoiceLink"

  2) COM Class:
     HKLM\SOFTWARE\Classes\CLSID\{OUR-GUID-GOES-HERE}
       (default)  = "VoiceLink TTS Engine"
     
     HKLM\...\{OUR-GUID-GOES-HERE}\InprocServer32
       (default)      = "C:\Program Files\VoiceLink\voicelink_bridge.dll"
       ThreadingModel = "Both"
"@

# --- STEP 5: Optional - Speak test ---
Write-Host "`n`n--- STEP 5: Voice Test ---`n" -ForegroundColor Yellow

$testText = "Once upon a time, in a land far far away, there lived a young girl named Alice."

foreach ($voice in $voices) {
    $v = $voice.VoiceInfo
    Write-Host "  Speaking with: $($v.Name)..." -ForegroundColor Cyan
    $synth.SelectVoice($v.Name)
    $synth.Speak($testText)
    Write-Host "  Done.`n"
}

$synth.Dispose()

Write-Host "============================================" -ForegroundColor Green
Write-Host "  Exploration complete!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
