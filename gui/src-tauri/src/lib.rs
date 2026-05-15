// ============================================================================
// VoiceLink GUI — Tauri Backend
// ============================================================================
//
// This is the Rust side of the management app. It provides:
//   1. System tray icon with status + menu
//   2. Tauri commands callable from the web frontend
//   3. Server health monitoring
//   4. Voice registry management (rename, enable/disable)
//   5. First-run setup (download Python, install deps, start server)
//
// The frontend (HTML/CSS/JS) calls these via tauri::invoke("command_name").
// ============================================================================

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::Mutex;
use std::os::windows::process::CommandExt;
use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::TrayIconEvent,
    AppHandle, Emitter, Manager,
};

// ============================================================================
// Data Types
// ============================================================================

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct VoiceInfo {
    pub id: String,
    pub name: String,
    pub language: String,
    pub gender: String,
    pub description: String,
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default = "default_sample_rate")]
    pub sample_rate: u32,
}

fn default_sample_rate() -> u32 {
    24000
}

#[derive(Debug, Serialize, Deserialize)]
pub struct ServerHealth {
    pub status: String,
    pub model: Option<String>,
    pub model_loaded: bool,
    pub gpu_available: bool,
    pub gpu_name: Option<String>,
    pub uptime_seconds: f64,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct ServerStatus {
    pub running: bool,
    pub health: Option<ServerHealth>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct SapiStatus {
    pub registered: bool,
    pub dll_path: Option<String>,
    pub voice_count: u32,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct GpuInfo {
    pub available: bool,
    pub name: Option<String>,
    pub vram_total_mb: Option<u64>,
    pub vram_free_mb: Option<u64>,
    pub can_run_standard: bool, // 0.6B needs ~2 GB
    pub can_run_full: bool,     // 1.7B needs ~5 GB
}

// ============================================================================
// Setup Types & Paths
// ============================================================================

/// Persistent config stored at C:\ProgramData\VoiceLink\config.json
#[derive(Debug, Serialize, Deserialize, Clone)]
struct AppConfig {
    data_dir: String,
    #[serde(default = "default_server_port")]
    server_port: u16,
    #[serde(default)]
    auto_start: bool,
    #[serde(default)]
    qwen3_enabled: bool,
    #[serde(default = "default_qwen3_tier")]
    qwen3_model_tier: String, // "standard" (0.6B) or "full" (1.7B)
    #[serde(default)]
    qwen3_installed: bool,
}

fn default_qwen3_tier() -> String {
    "standard".to_string()
}

fn default_server_port() -> u16 {
    7860
}

impl Default for AppConfig {
    fn default() -> Self {
        let base = std::env::var("ProgramData")
            .unwrap_or_else(|_| r"C:\ProgramData".to_string());
        Self {
            data_dir: PathBuf::from(base)
                .join("VoiceLink")
                .to_string_lossy()
                .to_string(),
            server_port: 7860,
            auto_start: false,
            qwen3_enabled: false,
            qwen3_model_tier: "standard".to_string(),
            qwen3_installed: false,
        }
    }
}

impl AppConfig {
    /// Config file lives at a fixed location so we can always find it
    fn config_path() -> PathBuf {
        let base = std::env::var("ProgramData")
            .unwrap_or_else(|_| r"C:\ProgramData".to_string());
        PathBuf::from(base).join("VoiceLink").join("config.json")
    }

    fn load() -> Self {
        let path = Self::config_path();
        if path.exists() {
            if let Ok(data) = std::fs::read_to_string(&path) {
                if let Ok(cfg) = serde_json::from_str::<AppConfig>(&data) {
                    return cfg;
                }
            }
        }
        Self::default()
    }

    fn save(&self) -> Result<(), String> {
        let path = Self::config_path();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("Failed to create config dir: {}", e))?;
        }
        let json = serde_json::to_string_pretty(self).map_err(|e| e.to_string())?;
        std::fs::write(&path, json).map_err(|e| format!("Failed to save config: {}", e))
    }

    fn data_dir(&self) -> PathBuf {
        PathBuf::from(&self.data_dir)
    }

    fn python_dir(&self) -> PathBuf {
        self.data_dir().join("python")
    }

    fn python_exe(&self) -> PathBuf {
        self.python_dir().join("python.exe")
    }

    fn server_dir(&self) -> PathBuf {
        self.data_dir().join("server")
    }

    fn model_dir(&self) -> PathBuf {
        self.data_dir().join("models")
    }

    fn voices_dir(&self) -> PathBuf {
        self.data_dir().join("voices")
    }
}

/// Status of each setup step
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct SetupStatus {
    pub python_installed: bool,
    pub deps_installed: bool,
    pub server_installed: bool,
    pub model_downloaded: bool,
    pub server_running: bool,
    pub data_dir: String,
}

/// Holds the server process handle so we can stop it later
struct ServerProcess(Option<std::process::Child>);

/// Whether we intentionally started the server (vs found it running externally)
struct ServerManagedByUs(bool);

/// PID of the currently running Qwen3 download process (for cancel/pause)
struct DownloadPid(Option<u32>);

// ============================================================================
// Tauri Commands — Called from the frontend via invoke()
// ============================================================================

/// Check SAPI Bridge registration status in the Windows registry
#[tauri::command]
fn get_sapi_status() -> Result<SapiStatus, String> {
    use winreg::enums::*;
    use winreg::RegKey;

    let hkcr = RegKey::predef(HKEY_CLASSES_ROOT);
    let clsid_path = r"CLSID\{D7A5E2B1-3F8C-4E69-A1B4-7C2D9E0F5A38}\InprocServer32";

    let dll_path: Option<String> = hkcr
        .open_subkey_with_flags(clsid_path, KEY_READ)
        .ok()
        .and_then(|key| key.get_value("").ok());

    let registered = dll_path.is_some();

    // Count voice tokens under Speech\Voices\Tokens that start with VoiceLink_
    let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
    let mut voice_count: u32 = 0;
    if let Ok(tokens_key) =
        hklm.open_subkey_with_flags(r"SOFTWARE\Microsoft\Speech\Voices\Tokens", KEY_READ)
    {
        for name in tokens_key.enum_keys().filter_map(|r| r.ok()) {
            if name.starts_with("VoiceLink_") {
                voice_count += 1;
            }
        }
    }

    Ok(SapiStatus {
        registered,
        dll_path,
        voice_count,
    })
}

/// Detect NVIDIA GPU via nvidia-smi (no Python/torch required).
/// Returns GPU info including VRAM — used to gate Qwen3 UI visibility.
/// If no NVIDIA GPU or nvidia-smi not found, returns available=false.
#[tauri::command]
fn check_gpu() -> Result<GpuInfo, String> {
    // Try running nvidia-smi to query GPU info
    let output = std::process::Command::new("nvidia-smi")
        .args([
            "--query-gpu=name,memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ])
        .creation_flags(0x08000000) // CREATE_NO_WINDOW
        .output();

    match output {
        Ok(out) if out.status.success() => {
            let stdout = String::from_utf8_lossy(&out.stdout);
            // Parse: "NVIDIA GeForce RTX 3060, 12288, 10240"
            let line = stdout.trim().lines().next().unwrap_or("");
            let parts: Vec<&str> = line.split(',').map(|s| s.trim()).collect();

            if parts.len() >= 3 {
                let name = parts[0].to_string();
                let total_mb = parts[1].parse::<u64>().unwrap_or(0);
                let free_mb = parts[2].parse::<u64>().unwrap_or(0);

                Ok(GpuInfo {
                    available: true,
                    name: Some(name),
                    vram_total_mb: Some(total_mb),
                    vram_free_mb: Some(free_mb),
                    can_run_standard: total_mb >= 2048,  // 0.6B needs ~2 GB total
                    can_run_full: total_mb >= 6144,      // 1.7B needs ~5 GB; 6 GB total headroom
                })
            } else {
                // nvidia-smi returned unexpected format
                Ok(GpuInfo {
                    available: false,
                    name: None,
                    vram_total_mb: None,
                    vram_free_mb: None,
                    can_run_standard: false,
                    can_run_full: false,
                })
            }
        }
        _ => {
            // nvidia-smi not found or errored — no NVIDIA GPU
            Ok(GpuInfo {
                available: false,
                name: None,
                vram_total_mb: None,
                vram_free_mb: None,
                can_run_standard: false,
                can_run_full: false,
            })
        }
    }
}

/// Check if the inference server is running and healthy
#[tauri::command]
async fn get_server_status() -> Result<ServerStatus, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(3))
        .build()
        .map_err(|e| e.to_string())?;

    match client.get("http://127.0.0.1:7860/v1/health").send().await {
        Ok(resp) => {
            if resp.status().is_success() {
                let health: ServerHealth = resp.json().await.map_err(|e| e.to_string())?;
                Ok(ServerStatus {
                    running: true,
                    health: Some(health),
                })
            } else {
                Ok(ServerStatus {
                    running: true,
                    health: None,
                })
            }
        }
        Err(_) => Ok(ServerStatus {
            running: false,
            health: None,
        }),
    }
}

/// Get list of voices from the inference server, with registry name overrides.
/// Merges Kokoro voices from /v1/voices with Qwen3 voices from /v1/qwen3/speakers.
#[tauri::command]
async fn get_voices(config: tauri::State<'_, Mutex<AppConfig>>) -> Result<Vec<VoiceInfo>, String> {
    let qwen3_enabled = {
        let cfg = config.lock().unwrap();
        cfg.qwen3_enabled
    };

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build()
        .map_err(|e| e.to_string())?;

    // Fetch Kokoro voices
    let resp = client
        .get("http://127.0.0.1:7860/v1/voices")
        .send()
        .await
        .map_err(|e| format!("Server not reachable: {}", e))?;

    let mut voices: Vec<VoiceInfo> = resp.json().await.map_err(|e| e.to_string())?;

    // Fetch Qwen3 voices if enabled
    if qwen3_enabled {
        if let Ok(qwen3_resp) = client
            .get("http://127.0.0.1:7860/v1/qwen3/speakers")
            .send()
            .await
        {
            if let Ok(qwen3_voices) = qwen3_resp.json::<Vec<serde_json::Value>>().await {
                for qv in qwen3_voices {
                    voices.push(VoiceInfo {
                        id: qv.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                        name: qv.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                        language: qv.get("language").and_then(|v| v.as_str()).unwrap_or("en-US").to_string(),
                        gender: qv.get("gender").and_then(|v| v.as_str()).unwrap_or("unknown").to_string(),
                        description: qv.get("description").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                        model: "qwen3".to_string(),
                        tags: qv.get("tags")
                            .and_then(|v| v.as_array())
                            .map(|arr| arr.iter().filter_map(|s| s.as_str().map(|s| s.to_string())).collect())
                            .unwrap_or_default(),
                        sample_rate: qv.get("sample_rate").and_then(|v| v.as_u64()).unwrap_or(24000) as u32,
                    });
                }
            }
        }
    }

    // Read custom names from registry and override server names
    {
        use winreg::enums::*;
        use winreg::RegKey;

        let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
        if let Ok(tokens_key) =
            hklm.open_subkey_with_flags(r"SOFTWARE\Microsoft\Speech\Voices\Tokens", KEY_READ)
        {
            for voice in voices.iter_mut() {
                let token_name = format!("VoiceLink_{}", voice.id);
                if let Ok(token_key) = tokens_key.open_subkey_with_flags(&token_name, KEY_READ) {
                    // Read the (Default) value — this is the display name
                    if let Ok(name) = token_key.get_value::<String, _>("") {
                        if !name.is_empty() {
                            // Strip legacy "VoiceLink " prefix if present
                            let clean = if let Some(stripped) = name.strip_prefix("VoiceLink ") {
                                stripped.to_string()
                            } else {
                                name
                            };
                            voice.name = clean;
                        }
                    }
                }
            }
        }
    }

    Ok(voices)
}

/// Rename a voice in the Windows registry (both Speech and Speech_OneCore)
#[tauri::command]
fn rename_voice(voice_id: String, new_name: String) -> Result<(), String> {
    use winreg::enums::*;
    use winreg::RegKey;

    let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
    let token_name = format!("VoiceLink_{}", voice_id);

    let token_roots = [
        r"SOFTWARE\Microsoft\Speech\Voices\Tokens",
        r"SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens",
    ];

    // Try direct HKLM write
    let has_access = hklm
        .open_subkey_with_flags(
            r"SOFTWARE\Microsoft\Speech\Voices\Tokens",
            KEY_WRITE,
        )
        .is_ok();

    if has_access {
        for root in &token_roots {
            let token_path = format!("{}\\{}", root, token_name);
            match hklm.open_subkey_with_flags(&token_path, KEY_SET_VALUE) {
                Ok(key) => {
                    key.set_value("", &new_name).map_err(|e| e.to_string())?;
                }
                Err(_) => {}
            }
            let attrs_path = format!("{}\\Attributes", token_path);
            match hklm.open_subkey_with_flags(&attrs_path, KEY_SET_VALUE) {
                Ok(key) => {
                    key.set_value("Name", &new_name).map_err(|e| e.to_string())?;
                }
                Err(_) => {}
            }
        }
    } else {
        // Elevate: build PowerShell commands for rename
        let mut ps_cmds = Vec::new();
        for root in &token_roots {
            let reg_path = format!("HKLM:\\{}\\{}", root, token_name);
            ps_cmds.push(format!(
                "if (Test-Path '{}') {{ Set-ItemProperty -Path '{}' -Name '(Default)' -Value '{}' }}",
                reg_path, reg_path, new_name.replace('\'', "''")
            ));
            let attrs_path = format!("{}\\Attributes", reg_path);
            ps_cmds.push(format!(
                "if (Test-Path '{}') {{ Set-ItemProperty -Path '{}' -Name 'Name' -Value '{}' }}",
                attrs_path, attrs_path, new_name.replace('\'', "''")
            ));
        }
        run_elevated_powershell(&ps_cmds.join("; "))?;
    }

    Ok(())
}

/// Preview a voice by sending text to the server and playing it
#[tauri::command]
async fn preview_voice(voice_id: String, text: String) -> Result<Vec<u8>, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;

    let body = serde_json::json!({
        "text": text,
        "voice": voice_id,
        "speed": 1.0,
        "format": "pcm_24k_16bit"
    });

    let resp = client
        .post("http://127.0.0.1:7860/v1/tts")
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("Server error: {}", e))?;

    let bytes = resp.bytes().await.map_err(|e| e.to_string())?;
    Ok(bytes.to_vec())
}

/// Get list of voice IDs currently registered in SAPI registry
#[tauri::command]
fn get_registered_voice_ids() -> Result<Vec<String>, String> {
    use winreg::enums::*;
    use winreg::RegKey;

    // Clean up any stale HKCU entries from a previous version
    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    if let Ok(tokens_key) = hkcu
        .open_subkey_with_flags(r"SOFTWARE\Microsoft\Speech\Voices\Tokens", KEY_READ | KEY_WRITE)
    {
        let stale: Vec<String> = tokens_key
            .enum_keys()
            .filter_map(|r| r.ok())
            .filter(|n| n.starts_with("VoiceLink_"))
            .collect();
        for name in &stale {
            let _ = tokens_key.delete_subkey_all(name);
        }
    }

    let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
    let mut ids = Vec::new();

    if let Ok(tokens_key) =
        hklm.open_subkey_with_flags(r"SOFTWARE\Microsoft\Speech\Voices\Tokens", KEY_READ)
    {
        for name in tokens_key.enum_keys().filter_map(|r| r.ok()) {
            if let Some(id) = name.strip_prefix("VoiceLink_") {
                ids.push(id.to_string());
            }
        }
    }

    Ok(ids)
}

/// Run a PowerShell command elevated via UAC prompt.
/// Writes commands to a temp .ps1 file and runs it elevated to avoid quoting issues.
fn run_elevated_powershell(commands: &str) -> Result<(), String> {
    use std::os::windows::process::CommandExt;
    use std::io::Write;
    const CREATE_NO_WINDOW: u32 = 0x08000000;

    // Write commands to a temporary .ps1 script file
    let temp_dir = std::env::temp_dir();
    let script_path = temp_dir.join("voicelink_elevate.ps1");
    {
        let mut f = std::fs::File::create(&script_path)
            .map_err(|e| format!("Failed to create temp script: {}", e))?;
        f.write_all(commands.as_bytes())
            .map_err(|e| format!("Failed to write temp script: {}", e))?;
    }

    let script_str = script_path.to_string_lossy().to_string();

    // Use Start-Process to run the script elevated
    let status = std::process::Command::new("powershell")
        .args([
            "-NoProfile",
            "-Command",
            &format!(
                "Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','\"{}\"' -Verb RunAs -Wait",
                script_str
            ),
        ])
        .creation_flags(CREATE_NO_WINDOW)
        .status()
        .map_err(|e| format!("Failed to launch elevation prompt: {}", e))?;

    // Clean up temp script
    let _ = std::fs::remove_file(&script_path);

    if status.success() {
        Ok(())
    } else {
        Err("Elevation was cancelled or failed. Voice toggling requires a one-time permission grant.".to_string())
    }
}

/// Toggle a voice on/off in SAPI by adding/removing its registry token.
/// Writes to BOTH 64-bit and 32-bit (WOW6432Node) registry views so
/// the voice appears in all apps regardless of architecture.
/// Tries direct HKLM write first; if not admin, elevates via UAC prompt.
#[tauri::command]
fn toggle_voice(voice_id: String, enabled: bool) -> Result<(), String> {
    use winreg::enums::*;
    use winreg::RegKey;

    let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
    let token_name = format!("VoiceLink_{}", voice_id);

    let token_roots = [
        r"SOFTWARE\Microsoft\Speech\Voices\Tokens",
        r"SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens",
    ];

    // Registry access flags: native 64-bit view + WOW64 32-bit view
    let reg_views: &[u32] = &[KEY_WOW64_64KEY, KEY_WOW64_32KEY];

    // Check if we have direct HKLM write access
    let has_access = hklm
        .open_subkey_with_flags(
            r"SOFTWARE\Microsoft\Speech\Voices\Tokens",
            KEY_WRITE,
        )
        .is_ok();

    if enabled {
        // Read CLSID from InprocServer32
        let hkcr = RegKey::predef(HKEY_CLASSES_ROOT);
        let clsid = "{D7A5E2B1-3F8C-4E69-A1B4-7C2D9E0F5A38}";
        let dll_path: String = hkcr
            .open_subkey_with_flags(
                r"CLSID\{D7A5E2B1-3F8C-4E69-A1B4-7C2D9E0F5A38}\InprocServer32",
                KEY_READ,
            )
            .and_then(|k| k.get_value(""))
            .unwrap_or_default();

        if dll_path.is_empty() {
            return Err("COM DLL not registered. Run regsvr32 first.".to_string());
        }

        let lang = if voice_id.starts_with('b') { "809" } else { "409" };
        let is_qwen3 = voice_id.starts_with("qwen3_");
        let model_name = if is_qwen3 { "qwen3" } else { "kokoro" };

        let gender = if is_qwen3 {
            match voice_id.as_str() {
                "qwen3_serena" | "qwen3_vivian" | "qwen3_ono_anna" | "qwen3_sohee" => "Female",
                _ => "Male"
            }
        } else if voice_id.contains("_m_") || voice_id.starts_with("am_") || voice_id.starts_with("bm_") {
            "Male"
        } else {
            "Female"
        };

        let display_name = if is_qwen3 {
            let name_part = if let Some(stripped) = voice_id.strip_prefix("qwen3_custom_") {
                stripped.to_string()
            } else if let Some(stripped) = voice_id.strip_prefix("qwen3_") {
                stripped.to_string()
            } else {
                voice_id.clone()
            };
            format!("{} (Qwen3)", name_part)
        } else {
            let raw_name = voice_id.split('_').last().unwrap_or(&voice_id);
            format!("{} (Kokoro)",
                raw_name.chars().next().map(|c| c.to_uppercase().to_string()).unwrap_or_default()
                    + &raw_name[1..]
            )
        };

        // Check for existing custom name (from rename)
        let existing_name: Option<String> = {
            let first_root = token_roots[0];
            let check_path = format!("{}\\{}", first_root, token_name);
            hklm.open_subkey_with_flags(&check_path, KEY_READ)
                .ok()
                .and_then(|k| k.get_value::<String, _>("").ok())
                .filter(|n| !n.is_empty())
        };
        let final_name = existing_name.unwrap_or(display_name);

        if has_access {
            // Direct write path — write to both 64-bit and 32-bit registry views
            for &view_flag in reg_views {
                for root in &token_roots {
                    let token_path = format!("{}\\{}", root, token_name);
                    let (token_key, _) = hklm
                        .create_subkey_with_flags(&token_path, KEY_WRITE | view_flag)
                        .map_err(|e| format!("Failed to create token key: {}", e))?;

                    token_key.set_value("", &final_name).map_err(|e| e.to_string())?;
                    token_key.set_value("CLSID", &clsid).map_err(|e| e.to_string())?;
                    token_key.set_value("VoiceLinkVoiceId", &voice_id).map_err(|e| e.to_string())?;
                    token_key.set_value("VoiceLinkServerPort", &"7860").map_err(|e| e.to_string())?;
                    token_key.set_value("VoiceLinkModel", &model_name).map_err(|e| e.to_string())?;

                    let attrs_path = format!("{}\\Attributes", token_path);
                    let (attrs_key, _) = hklm
                        .create_subkey_with_flags(&attrs_path, KEY_WRITE | view_flag)
                        .map_err(|e| format!("Failed to create attrs key: {}", e))?;

                    attrs_key.set_value("Name", &final_name).map_err(|e| e.to_string())?;
                    attrs_key.set_value("Gender", &gender).map_err(|e| e.to_string())?;
                    attrs_key.set_value("Language", &lang).map_err(|e| e.to_string())?;
                    attrs_key.set_value("Age", &"Adult").map_err(|e| e.to_string())?;
                    attrs_key.set_value("Vendor", &"VoiceLink").map_err(|e| e.to_string())?;
                }
            }
        } else {
            // Elevate: build PowerShell reg commands for both native + WOW6432Node
            let safe_name = final_name.replace('\'', "''");
            let safe_vid = voice_id.replace('\'', "''");
            let mut ps_cmds = Vec::new();

            // Native paths (64-bit) + WOW6432Node paths (32-bit)
            let all_roots: Vec<String> = token_roots.iter().map(|r| r.to_string()).chain(
                token_roots.iter().map(|r| r.replace(r"SOFTWARE\Microsoft", r"SOFTWARE\WOW6432Node\Microsoft"))
            ).collect();

            for root in &all_roots {
                let reg_path = format!("HKLM:\\{}\\{}", root, token_name);
                let attrs_path = format!("{}\\Attributes", reg_path);
                ps_cmds.push(format!("New-Item -Path '{}' -Force | Out-Null", reg_path));
                ps_cmds.push(format!("Set-ItemProperty -Path '{}' -Name '(Default)' -Value '{}'", reg_path, safe_name));
                ps_cmds.push(format!("Set-ItemProperty -Path '{}' -Name 'CLSID' -Value '{}'", reg_path, clsid));
                ps_cmds.push(format!("Set-ItemProperty -Path '{}' -Name 'VoiceLinkVoiceId' -Value '{}'", reg_path, safe_vid));
                ps_cmds.push(format!("Set-ItemProperty -Path '{}' -Name 'VoiceLinkServerPort' -Value '7860'", reg_path));
                ps_cmds.push(format!("Set-ItemProperty -Path '{}' -Name 'VoiceLinkModel' -Value '{}'", reg_path, model_name));
                ps_cmds.push(format!("New-Item -Path '{}' -Force | Out-Null", attrs_path));
                ps_cmds.push(format!("Set-ItemProperty -Path '{}' -Name 'Name' -Value '{}'", attrs_path, safe_name));
                ps_cmds.push(format!("Set-ItemProperty -Path '{}' -Name 'Gender' -Value '{}'", attrs_path, gender));
                ps_cmds.push(format!("Set-ItemProperty -Path '{}' -Name 'Language' -Value '{}'", attrs_path, lang));
                ps_cmds.push(format!("Set-ItemProperty -Path '{}' -Name 'Age' -Value 'Adult'", attrs_path));
                ps_cmds.push(format!("Set-ItemProperty -Path '{}' -Name 'Vendor' -Value 'VoiceLink'", attrs_path));
            }
            run_elevated_powershell(&ps_cmds.join("; "))?;
        }
    } else {
        // Remove voice token from both 64-bit and 32-bit registry views
        if has_access {
            for &view_flag in reg_views {
                for root in &token_roots {
                    if let Ok(tokens_key) = hklm.open_subkey_with_flags(root, KEY_WRITE | view_flag) {
                        let _ = tokens_key.delete_subkey_all(&token_name);
                    }
                }
            }
        } else {
            let mut ps_cmds = Vec::new();
            let all_roots: Vec<String> = token_roots.iter().map(|r| r.to_string()).chain(
                token_roots.iter().map(|r| r.replace(r"SOFTWARE\Microsoft", r"SOFTWARE\WOW6432Node\Microsoft"))
            ).collect();
            for root in &all_roots {
                let reg_path = format!("HKLM:\\{}\\{}", root, token_name);
                ps_cmds.push(format!(
                    "if (Test-Path '{}') {{ Remove-Item -Path '{}' -Recurse -Force }}",
                    reg_path, reg_path
                ));
            }
            run_elevated_powershell(&ps_cmds.join("; "))?;
        }
    }

    Ok(())
}

// ============================================================================
// Qwen3 Commands — Voice Studio API proxies
// ============================================================================

/// Get list of Qwen3 speakers (built-in + custom)
#[tauri::command]
async fn qwen3_list_speakers() -> Result<serde_json::Value, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| e.to_string())?;

    let resp = client
        .get("http://127.0.0.1:7860/v1/qwen3/speakers")
        .send()
        .await
        .map_err(|e| format!("Server not reachable: {}", e))?;

    let json: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
    Ok(json)
}

/// Delete a cloned voice profile from the server
#[tauri::command]
async fn qwen3_delete_clone(voice_id: String) -> Result<(), String> {
    // Extract the profile name from voice_id ("qwen3_custom_Name" -> "Name")
    let profile_name = voice_id
        .strip_prefix("qwen3_custom_")
        .unwrap_or(&voice_id);

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| e.to_string())?;

    let resp = client
        .delete(format!("http://127.0.0.1:7860/v1/qwen3/clone/{}", profile_name))
        .send()
        .await
        .map_err(|e| format!("Server error: {}", e))?;

    if !resp.status().is_success() {
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("Delete failed: {}", body));
    }
    Ok(())
}

/// Clone a voice via Qwen3 — uploads reference audio + transcript to server
#[tauri::command]
async fn qwen3_clone_voice(
    name: String,
    transcript: String,
    audio_data: Vec<u8>,
    audio_filename: String,
    gender: String,
    description: String,
    preview_text: String,
) -> Result<Vec<u8>, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(300))
        .build()
        .map_err(|e| e.to_string())?;

    // Detect MIME type from extension
    let mime = match audio_filename.rsplit('.').next().unwrap_or("wav").to_lowercase().as_str() {
        "mp3" => "audio/mpeg",
        "m4a" | "mp4" | "aac" => "audio/mp4",
        "ogg" | "oga" => "audio/ogg",
        "flac" => "audio/flac",
        "webm" => "audio/webm",
        _ => "audio/wav",
    };

    let audio_part = reqwest::multipart::Part::bytes(audio_data)
        .file_name(audio_filename)
        .mime_str(mime)
        .map_err(|e| e.to_string())?;

    let form = reqwest::multipart::Form::new()
        .text("name", name)
        .text("transcript", transcript)
        .text("gender", gender)
        .text("description", description)
        .text("preview_text", preview_text)
        .part("audio", audio_part);

    let resp = client
        .post("http://127.0.0.1:7860/v1/qwen3/clone")
        .multipart(form)
        .send()
        .await
        .map_err(|e| format!("Server error: {}", e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("Clone failed ({}): {}", status, body));
    }

    let bytes = resp.bytes().await.map_err(|e| e.to_string())?;
    Ok(bytes.to_vec())
}

/// Design a voice via Qwen3 from a text description (1.7B only)
#[tauri::command]
async fn qwen3_design_voice(
    name: String,
    description: String,
    sample_text: String,
) -> Result<Vec<u8>, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(120))
        .build()
        .map_err(|e| e.to_string())?;

    let body = serde_json::json!({
        "name": name,
        "description": description,
        "sample_text": sample_text,
    });

    let resp = client
        .post("http://127.0.0.1:7860/v1/qwen3/design")
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("Server error: {}", e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("Design failed ({}): {}", status, body));
    }

    let bytes = resp.bytes().await.map_err(|e| e.to_string())?;
    Ok(bytes.to_vec())
}

/// Preview a Qwen3 voice (built-in or custom) by synthesizing text
#[tauri::command]
async fn qwen3_preview_voice(voice_id: String, text: String) -> Result<Vec<u8>, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(300))
        .build()
        .map_err(|e| e.to_string())?;

    let body = serde_json::json!({
        "text": text,
        "voice": voice_id,
        "speed": 1.0,
    });

    let resp = client
        .post("http://127.0.0.1:7860/v1/qwen3/tts")
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("Server error: {}", e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("Qwen3 TTS failed ({}): {}", status, body));
    }

    let bytes = resp.bytes().await.map_err(|e| e.to_string())?;
    Ok(bytes.to_vec())
}

/// Narrate long text with Qwen3 TTS — supports language selection
#[tauri::command]
async fn qwen3_narrate(voice_id: String, text: String, language: String, speed: f64) -> Result<Vec<u8>, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(600))
        .build()
        .map_err(|e| e.to_string())?;

    let body = serde_json::json!({
        "text": text,
        "voice": voice_id,
        "speed": speed,
        "language": language,
    });

    let resp = client
        .post("http://127.0.0.1:7860/v1/qwen3/tts")
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("Server error: {}", e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("Qwen3 narration failed ({}): {}", status, body));
    }

    let bytes = resp.bytes().await.map_err(|e| e.to_string())?;
    Ok(bytes.to_vec())
}

/// Save PCM audio as a WAV file — shows a native Save dialog
#[tauri::command]
async fn save_wav_file(pcm_data: Vec<u8>) -> Result<Option<String>, String> {
    // Show native save dialog
    let file = rfd::AsyncFileDialog::new()
        .set_title("Save Narration")
        .set_file_name("narration.wav")
        .add_filter("WAV Audio", &["wav"])
        .save_file()
        .await;

    let handle = match file {
        Some(h) => h,
        None => return Ok(None), // User cancelled
    };

    let path = handle.path().to_owned();

    // Build WAV from PCM: 16-bit mono 24kHz
    let sample_rate: u32 = 24000;
    let data_len = pcm_data.len() as u32;
    let file_len = 36 + data_len;

    let mut wav = Vec::with_capacity(44 + pcm_data.len());
    wav.extend_from_slice(b"RIFF");
    wav.extend_from_slice(&file_len.to_le_bytes());
    wav.extend_from_slice(b"WAVE");
    wav.extend_from_slice(b"fmt ");
    wav.extend_from_slice(&16u32.to_le_bytes());       // chunk size
    wav.extend_from_slice(&1u16.to_le_bytes());        // PCM format
    wav.extend_from_slice(&1u16.to_le_bytes());        // mono
    wav.extend_from_slice(&sample_rate.to_le_bytes()); // sample rate
    wav.extend_from_slice(&(sample_rate * 2).to_le_bytes()); // byte rate
    wav.extend_from_slice(&2u16.to_le_bytes());        // block align
    wav.extend_from_slice(&16u16.to_le_bytes());       // bits per sample
    wav.extend_from_slice(b"data");
    wav.extend_from_slice(&data_len.to_le_bytes());
    wav.extend_from_slice(&pcm_data);

    std::fs::write(&path, &wav)
        .map_err(|e| format!("Failed to save WAV: {}", e))?;

    Ok(Some(path.to_string_lossy().to_string()))
}

/// Get Qwen3 model status (loaded, tier, idle time)
#[tauri::command]
async fn qwen3_get_status() -> Result<serde_json::Value, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build()
        .map_err(|e| e.to_string())?;

    let resp = client
        .get("http://127.0.0.1:7860/v1/qwen3/status")
        .send()
        .await
        .map_err(|e| format!("Server not reachable: {}", e))?;

    let json: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
    Ok(json)
}

// ============================================================================
// Setup Commands — First-run automated setup
// ============================================================================

/// Check what's already installed and return setup status
#[tauri::command]
async fn get_setup_status(config: tauri::State<'_, Mutex<AppConfig>>) -> Result<SetupStatus, String> {
    // Collect file-based checks while holding the lock, then drop it before network IO
    let (python_ok, deps_ok, server_ok, model_ok, data_dir_str) = {
        let cfg = config.lock().unwrap();

        let python_ok = cfg.python_exe().exists();

        let embedded_deps = cfg.python_dir()
            .join("Lib").join("site-packages").join("fastapi").exists();
        let deps_marker = cfg.data_dir().join(".deps_installed");
        let deps_ok = embedded_deps || deps_marker.exists();

        let server_ok = cfg.server_dir().join("main.py").exists();

        // Check for the .voices_ready marker written by the voicepack download
        // script. This file only exists after ALL voicepacks + model have been
        // successfully downloaded from HuggingFace to the local HF cache.
        let model_ok = cfg.data_dir().join(".voices_ready").exists();

        (python_ok, deps_ok, server_ok, model_ok, cfg.data_dir.clone())
    }; // MutexGuard dropped here — safe to do async IO now

    // Check if server is actually running via HTTP health endpoint (same as Dashboard)
    let server_running = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
    {
        Ok(client) => client
            .get("http://127.0.0.1:7860/v1/health")
            .send()
            .await
            .map_or(false, |r| r.status().is_success()),
        Err(_) => false,
    };

    Ok(SetupStatus {
        python_installed: python_ok,
        deps_installed: deps_ok,
        server_installed: server_ok,
        model_downloaded: model_ok,
        server_running,
        data_dir: data_dir_str,
    })
}

/// Download a file from a URL to a local path, with progress reporting via events
#[tauri::command]
async fn setup_download_file(
    app: AppHandle,
    url: String,
    dest: String,
    step_name: String,
) -> Result<String, String> {
    use std::io::Write;

    let dest_path = PathBuf::from(&dest);
    // Create parent directories
    if let Some(parent) = dest_path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("Failed to create directory: {}", e))?;
    }

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(600))
        .build()
        .map_err(|e| e.to_string())?;

    let resp = client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("Download failed: {}", e))?;

    let total_size = resp.content_length().unwrap_or(0);
    let mut downloaded: u64 = 0;

    let mut file = std::fs::File::create(&dest_path)
        .map_err(|e| format!("Failed to create file {}: {}", dest, e))?;

    let mut stream = resp.bytes_stream();
    use futures_util::StreamExt;
    let result: Result<(), String> = async {
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|e| format!("Download error: {}", e))?;
            file.write_all(&chunk)
                .map_err(|e| format!("Write error: {}", e))?;
            downloaded += chunk.len() as u64;

            // Emit progress event to frontend
            let progress = if total_size > 0 {
                (downloaded as f64 / total_size as f64 * 100.0) as u32
            } else {
                0
            };
            let _ = app.emit(
                "setup-progress",
                serde_json::json!({
                    "step": step_name,
                    "progress": progress,
                    "downloaded": downloaded,
                    "total": total_size,
                }),
            );
        }
        Ok(())
    }.await;

    // If download failed, remove the partial/empty file so it doesn't
    // trick the status check into thinking the model is downloaded.
    if result.is_err() {
        let _ = std::fs::remove_file(&dest_path);
    }
    result?;

    Ok(dest_path.to_string_lossy().to_string())
}

/// Extract a zip file to a destination directory
#[tauri::command]
async fn setup_extract_zip(zip_path: String, dest_dir: String) -> Result<(), String> {
    let zip_path = PathBuf::from(&zip_path);
    let dest_dir = PathBuf::from(&dest_dir);

    std::fs::create_dir_all(&dest_dir)
        .map_err(|e| format!("Failed to create dir: {}", e))?;

    let file = std::fs::File::open(&zip_path)
        .map_err(|e| format!("Failed to open zip: {}", e))?;

    let mut archive = zip::ZipArchive::new(file)
        .map_err(|e| format!("Failed to read zip: {}", e))?;

    for i in 0..archive.len() {
        let mut entry = archive.by_index(i).map_err(|e| e.to_string())?;
        let out_path = dest_dir.join(
            entry.mangled_name()
        );

        if entry.is_dir() {
            std::fs::create_dir_all(&out_path).map_err(|e| e.to_string())?;
        } else {
            if let Some(parent) = out_path.parent() {
                std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            }
            let mut out_file = std::fs::File::create(&out_path)
                .map_err(|e| format!("Failed to create {}: {}", out_path.display(), e))?;
            std::io::copy(&mut entry, &mut out_file).map_err(|e| e.to_string())?;
        }
    }

    Ok(())
}

/// Enable pip in the embedded Python by modifying the ._pth file
#[tauri::command]
fn setup_enable_pip(config: tauri::State<'_, Mutex<AppConfig>>) -> Result<(), String> {
    let cfg = config.lock().map_err(|e| e.to_string())?;
    let python = cfg.python_dir();

    // Find the ._pth file (e.g. python311._pth)
    let pth_file = std::fs::read_dir(&python)
        .map_err(|e| e.to_string())?
        .filter_map(|e| e.ok())
        .find(|e| {
            e.path()
                .extension()
                .map(|ext| ext == "_pth")
                .unwrap_or(false)
        });

    if let Some(pth_entry) = pth_file {
        let pth_path = pth_entry.path();
        let content = std::fs::read_to_string(&pth_path).map_err(|e| e.to_string())?;

        // Uncomment "import site" line and add Lib\site-packages
        let mut new_lines: Vec<String> = Vec::new();
        let mut has_import_site = false;
        let data_dir_str = cfg.data_dir().to_string_lossy().to_string();
        let mut has_data_dir = false;
        for line in content.lines() {
            if line.trim() == "#import site" {
                new_lines.push("import site".to_string());
                has_import_site = true;
            } else if line.trim() == "import site" {
                new_lines.push(line.to_string());
                has_import_site = true;
            } else {
                new_lines.push(line.to_string());
            }
            // Check if data dir is already listed
            if line.trim() == data_dir_str {
                has_data_dir = true;
            }
        }
        if !has_import_site {
            new_lines.push("import site".to_string());
        }
        // Add the data directory so "python -m server.main" can find
        // the server package. Embedded Python ignores PYTHONPATH when
        // a ._pth file exists, so this is the only way.
        if !has_data_dir {
            new_lines.push(data_dir_str);
        }

        std::fs::write(&pth_path, new_lines.join("\n"))
            .map_err(|e| format!("Failed to write {}: {}", pth_path.display(), e))?;
    }

    Ok(())
}

/// Run a command and return its output (used for pip install, etc.)
/// Streams stdout/stderr line-by-line and emits progress events so the
/// frontend can show real-time status instead of being stuck at 0%.
#[tauri::command]
async fn setup_run_command(
    app: AppHandle,
    program: String,
    args: Vec<String>,
    step_name: String,
    env: Option<std::collections::HashMap<String, String>>,
    cwd: Option<String>,
) -> Result<String, String> {
    use tokio::io::{AsyncBufReadExt, BufReader};

    let _ = app.emit(
        "setup-progress",
        serde_json::json!({
            "step": step_name,
            "progress": 0,
            "status": "running",
        }),
    );

    // CREATE_NO_WINDOW (0x08000000) prevents a visible console window
    // from appearing when a GUI app spawns a console subprocess.
    let mut cmd = tokio::process::Command::new(&program);
    cmd.args(&args)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .creation_flags(0x08000000); // CREATE_NO_WINDOW

    // Inject optional environment variables (e.g. VOICELINK_DATA_DIR)
    if let Some(ref envs) = env {
        for (k, v) in envs {
            cmd.env(k, v);
        }
    }

    // Set working directory if specified
    if let Some(ref dir) = cwd {
        cmd.current_dir(dir);
    }

    let mut child = cmd.spawn()
        .map_err(|e| format!("Failed to run {}: {}", program, e))?;

    // Store PID for qwen3 downloads so they can be cancelled/paused
    if step_name.starts_with("qwen3") {
        if let Some(pid) = child.id() {
            if let Ok(mut dl) = app.state::<Mutex<DownloadPid>>().lock() {
                dl.0 = Some(pid);
            }
        }
    }

    // Prepare log file for this step so "Open Terminal" can tail it
    let log_path = {
        let data_dir = {
            let config = app.state::<Mutex<AppConfig>>();
            config.lock().ok().map(|cfg| cfg.data_dir())
        };
        if let Some(dd) = data_dir {
            let log_dir = dd.join("logs");
            let _ = std::fs::create_dir_all(&log_dir);
            let p = log_dir.join(format!("setup-{}.log", step_name));
            let _ = std::fs::write(&p, "");
            Some(p)
        } else {
            None
        }
    };

    let stdout_handle = child.stdout.take();
    let stderr_handle = child.stderr.take();

    let step_clone = step_name.clone();
    let app_clone = app.clone();
    let log_path_stdout = log_path.clone();
    let stdout_task = tokio::spawn(async move {
        let mut lines = Vec::new();
        if let Some(stdout) = stdout_handle {
            let mut reader = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                let _ = app_clone.emit(
                    "setup-progress",
                    serde_json::json!({
                        "step": step_clone,
                        "progress": 50,
                        "status": "running",
                        "line": line,
                    }),
                );
                if let Some(ref lp) = log_path_stdout {
                    use std::io::Write;
                    if let Ok(mut f) = std::fs::OpenOptions::new().append(true).open(lp) {
                        let _ = writeln!(f, "{}", line);
                    }
                }
                lines.push(line);
            }
        }
        lines.join("\n")
    });

    let step_clone2 = step_name.clone();
    let app_clone2 = app.clone();
    let log_path_stderr = log_path.clone();
    let stderr_task = tokio::spawn(async move {
        let mut lines = Vec::new();
        if let Some(stderr) = stderr_handle {
            let mut reader = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                let _ = app_clone2.emit(
                    "setup-progress",
                    serde_json::json!({
                        "step": step_clone2,
                        "progress": 50,
                        "status": "running",
                        "line": line,
                    }),
                );
                if let Some(ref lp) = log_path_stderr {
                    use std::io::Write;
                    if let Ok(mut f) = std::fs::OpenOptions::new().append(true).open(lp) {
                        let _ = writeln!(f, "{}", line);
                    }
                }
                lines.push(line);
            }
        }
        lines.join("\n")
    });

    let status = child.wait().await
        .map_err(|e| format!("Failed to wait for {}: {}", program, e))?;

    // Clear download PID
    if step_name.starts_with("qwen3") {
        if let Ok(mut dl) = app.state::<Mutex<DownloadPid>>().lock() {
            dl.0 = None;
        }
    }

    let stdout = stdout_task.await.unwrap_or_default();
    let stderr = stderr_task.await.unwrap_or_default();

    let _ = app.emit(
        "setup-progress",
        serde_json::json!({
            "step": step_name,
            "progress": 100,
            "status": if status.success() { "done" } else { "error" },
        }),
    );

    if status.success() {
        Ok(stdout)
    } else {
        Err(format!("Command failed:\nstdout: {}\nstderr: {}", stdout, stderr))
    }
}

/// Open a PowerShell window tailing the setup log file for a given step.
#[tauri::command]
fn open_setup_terminal(
    app: AppHandle,
    step_name: String,
) -> Result<(), String> {
    let config = app.state::<Mutex<AppConfig>>();
    let cfg = config.lock().map_err(|e| e.to_string())?;
    let log_dir = cfg.data_dir().join("logs");
    let log_path = log_dir.join(format!("setup-{}.log", step_name));

    // Create log dir and file if they don't exist yet
    std::fs::create_dir_all(&log_dir)
        .map_err(|e| format!("Failed to create logs dir: {}", e))?;
    if !log_path.exists() {
        std::fs::write(&log_path, "")
            .map_err(|e| format!("Failed to create log file: {}", e))?;
    }

    let log_path_str = log_path.to_string_lossy().to_string();
    std::process::Command::new("powershell")
        .args([
            "-Command",
            &format!(
                "Set-Location '{}'; Write-Host 'VoiceLink Setup Log — {}' -ForegroundColor Cyan; Write-Host ''; Get-Content '{}' -Wait -Tail 100",
                log_dir.to_string_lossy(),
                step_name,
                log_path_str,
            ),
        ])
        .spawn()
        .map_err(|e| format!("Failed to open terminal: {}", e))?;

    Ok(())
}

/// Cancel/pause the currently running Qwen3 download process.
/// Uses taskkill /T to also terminate child processes (pip, snapshot_download).
/// Since huggingface_hub caches partial downloads, re-running will resume.
#[tauri::command]
fn cancel_qwen3_download(app: AppHandle) -> Result<(), String> {
    let pid = {
        let dl = app.state::<Mutex<DownloadPid>>();
        let mut guard = dl.lock().map_err(|e| e.to_string())?;
        guard.0.take()
    };

    if let Some(pid) = pid {
        // Kill the process tree (the download spawns child processes)
        let _ = std::process::Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .creation_flags(0x08000000)
            .output();
        Ok(())
    } else {
        Err("No download process running".to_string())
    }
}

/// Change the data directory and persist to config
#[tauri::command]
fn set_data_dir(config: tauri::State<'_, Mutex<AppConfig>>, new_dir: String) -> Result<(), String> {
    let mut cfg = config.lock().map_err(|e| e.to_string())?;
    cfg.data_dir = new_dir;
    cfg.save()
}

/// Copy the server/ directory into the data dir.
/// Resolves source automatically: bundled resource (production) or repo path (dev).
#[tauri::command]
fn setup_install_server(app: AppHandle, config: tauri::State<'_, Mutex<AppConfig>>) -> Result<(), String> {
    let cfg = config.lock().map_err(|e| e.to_string())?;
    let dest = cfg.server_dir();

    // Try 1: Bundled resource path (production install)
    let resource_path = app.path().resource_dir()
        .map(|p| p.join("server"))
        .unwrap_or_default();

    // Try 2: Dev path relative to the Cargo project
    let dev_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent().unwrap_or(std::path::Path::new("."))
        .parent().unwrap_or(std::path::Path::new("."))
        .join("server");

    let source = if resource_path.join("main.py").exists() {
        resource_path
    } else if dev_path.join("main.py").exists() {
        dev_path
    } else {
        return Err(format!(
            "Server source not found.\n  Checked resource: {}\n  Checked dev: {}",
            resource_path.display(), dev_path.display()
        ));
    };

    // Copy the server directory recursively
    fn copy_dir_recursive(src: &std::path::Path, dst: &std::path::Path) -> Result<(), String> {
        std::fs::create_dir_all(dst).map_err(|e| e.to_string())?;
        for entry in std::fs::read_dir(src).map_err(|e| e.to_string())? {
            let entry = entry.map_err(|e| e.to_string())?;
            let src_path = entry.path();
            let dst_path = dst.join(entry.file_name());

            if src_path.is_dir() {
                // Skip __pycache__ and .pyc files
                if entry.file_name() == "__pycache__" {
                    continue;
                }
                copy_dir_recursive(&src_path, &dst_path)?;
            } else {
                std::fs::copy(&src_path, &dst_path)
                    .map_err(|e| format!("Copy failed: {}", e))?;
            }
        }
        Ok(())
    }

    copy_dir_recursive(&source, &dest)
}

/// Get the paths used by the setup system
#[tauri::command]
fn get_setup_paths(config: tauri::State<'_, Mutex<AppConfig>>) -> Result<serde_json::Value, String> {
    let cfg = config.lock().map_err(|e| e.to_string())?;
    Ok(serde_json::json!({
        "data_dir": cfg.data_dir(),
        "python_dir": cfg.python_dir(),
        "python_exe": cfg.python_exe(),
        "server_dir": cfg.server_dir(),
        "model_dir": cfg.model_dir(),
    }))
}

/// Start the inference server using the bundled Python
#[tauri::command]
async fn start_server(app: AppHandle) -> Result<(), String> {
    let config = app.state::<Mutex<AppConfig>>();
    let cfg = config.lock().map_err(|e| e.to_string())?.clone();

    let python = cfg.python_exe();
    if !python.exists() {
        return Err("Python not installed. Run setup first.".to_string());
    }

    let server = cfg.server_dir();
    if !server.join("main.py").exists() {
        return Err("Server not installed. Run setup first.".to_string());
    }

    // Check if already running
    if std::net::TcpStream::connect("127.0.0.1:7860").is_ok() {
        return Ok(()); // Already running
    }

    // Start server as a detached background process
    // DETACHED_PROCESS (0x08) + CREATE_NO_WINDOW (0x08000000) ensures
    // no console window flashes on screen when the server starts.
    // PYTHONPATH must include the data dir so embedded Python can find
    // the "server" package (embedded Python's ._pth restricts sys.path).
    //
    // We use a Python bootstrap snippet instead of `-m server.main` so we can
    // monkey-patch subprocess.Popen to always pass CREATE_NO_WINDOW. This
    // prevents spaCy, HuggingFace, and other libraries from flashing CMD
    // windows when they download models on first run.
    let bootstrap = r#"
import subprocess, sys
_orig = subprocess.Popen.__init__
def _patched(self, *a, **kw):
    if sys.platform == 'win32' and 'creationflags' not in kw:
        kw['creationflags'] = 0x08000000
    _orig(self, *a, **kw)
subprocess.Popen.__init__ = _patched
import runpy; runpy.run_module('server.main', run_name='__main__', alter_sys=True)
"#;
    let child = std::process::Command::new(python.to_string_lossy().to_string())
        .args(["-c", bootstrap.trim()])
        .current_dir(cfg.data_dir())
        .env("PYTHONPATH", cfg.data_dir())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .creation_flags(0x00000008 | 0x08000000) // DETACHED_PROCESS | CREATE_NO_WINDOW
        .spawn()
        .map_err(|e| format!("Failed to start server: {}", e))?;

    // Store the child process handle (drop the lock before await)
    {
        let state = app.state::<Mutex<ServerProcess>>();
        let mut proc = state.lock().map_err(|e| e.to_string())?;
        proc.0 = Some(child);

        // Mark that we manage the server (enables watchdog auto-restart)
        let managed = app.state::<Mutex<ServerManagedByUs>>();
        let mut m = managed.lock().map_err(|e| e.to_string())?;
        m.0 = true;
    }

    // Wait a moment for server to start
    tokio::time::sleep(std::time::Duration::from_secs(2)).await;

    Ok(())
}

/// Stop the inference server
#[tauri::command]
async fn stop_server(app: AppHandle) -> Result<(), String> {
    // Clear managed flag first so watchdog doesn't restart
    {
        let managed = app.state::<Mutex<ServerManagedByUs>>();
        let mut m = managed.lock().map_err(|e| e.to_string())?;
        m.0 = false;
    }

    let state = app.state::<Mutex<ServerProcess>>();
    let mut proc = state.lock().map_err(|e| e.to_string())?;

    if let Some(ref mut child) = proc.0 {
        let _ = child.kill();
        let _ = child.wait();
        proc.0 = None;
    }

    Ok(())
}

// ============================================================================
// Settings — Persist to config.json
// ============================================================================

/// Return the current settings to the frontend
#[tauri::command]
fn get_settings(config: tauri::State<'_, Mutex<AppConfig>>) -> Result<serde_json::Value, String> {
    let cfg = config.lock().map_err(|e| e.to_string())?;
    Ok(serde_json::json!({
        "data_dir": cfg.data_dir,
        "server_port": cfg.server_port,
        "auto_start": cfg.auto_start,
        "qwen3_enabled": cfg.qwen3_enabled,
        "qwen3_model_tier": cfg.qwen3_model_tier,
        "qwen3_installed": cfg.qwen3_installed,
    }))
}

/// Update settings and persist to disk. Also syncs auto_start with registry.
#[tauri::command]
fn save_settings(
    app: AppHandle,
    server_port: Option<u16>,
    auto_start: Option<bool>,
    qwen3_enabled: Option<bool>,
    qwen3_model_tier: Option<String>,
    qwen3_installed: Option<bool>,
) -> Result<(), String> {
    let config = app.state::<Mutex<AppConfig>>();
    let mut cfg = config.lock().map_err(|e| e.to_string())?;

    if let Some(port) = server_port {
        cfg.server_port = port;
    }

    if let Some(auto) = auto_start {
        cfg.auto_start = auto;
        // Sync with registry (drop lock first is not needed — set_autostart_inner)
        set_autostart_inner(auto)?;
    }

    if let Some(enabled) = qwen3_enabled {
        cfg.qwen3_enabled = enabled;
    }
    if let Some(tier) = qwen3_model_tier {
        cfg.qwen3_model_tier = tier;
    }
    if let Some(installed) = qwen3_installed {
        cfg.qwen3_installed = installed;
    }

    cfg.save()?;
    Ok(())
}

/// Internal helper to set/clear the HKCU Run key without needing an AppHandle
fn set_autostart_inner(enabled: bool) -> Result<(), String> {
    use winreg::enums::*;
    use winreg::RegKey;

    if enabled {
        let exe_path = std::env::current_exe()
            .map_err(|e| format!("Failed to get exe path: {}", e))?;
        let hkcu = RegKey::predef(HKEY_CURRENT_USER);
        let (key, _) = hkcu
            .create_subkey(AUTOSTART_REG_KEY)
            .map_err(|e| format!("Failed to open registry: {}", e))?;
        let cmd = format!(r#""{}" --minimized"#, exe_path.to_string_lossy());
        key.set_value(AUTOSTART_VALUE_NAME, &cmd)
            .map_err(|e| format!("Failed to set registry value: {}", e))?;
    } else {
        let hkcu = RegKey::predef(HKEY_CURRENT_USER);
        if let Ok(key) = hkcu.open_subkey_with_flags(AUTOSTART_REG_KEY, KEY_WRITE) {
            let _ = key.delete_value(AUTOSTART_VALUE_NAME);
        }
    }

    Ok(())
}

// ============================================================================
// Auto-start — Launch VoiceLink GUI on user login
// ============================================================================
// Uses HKCU registry Run key to launch VoiceLink.exe --minimized on login.
// HKCU doesn't require admin elevation (same approach as Discord, Steam, etc.).
// The app starts hidden in the system tray and auto-starts the TTS server.

const AUTOSTART_REG_KEY: &str = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run";
const AUTOSTART_VALUE_NAME: &str = "VoiceLink";

/// Check if auto-start is enabled
#[tauri::command]
fn get_autostart(_app: AppHandle) -> Result<bool, String> {
    use winreg::enums::*;
    use winreg::RegKey;

    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    match hkcu.open_subkey(AUTOSTART_REG_KEY) {
        Ok(key) => {
            let val: Result<String, _> = key.get_value(AUTOSTART_VALUE_NAME);
            Ok(val.is_ok())
        }
        Err(_) => Ok(false),
    }
}

/// Enable or disable auto-start
#[tauri::command]
fn set_autostart(app: AppHandle, enabled: bool) -> Result<(), String> {
    // Update registry via shared helper
    set_autostart_inner(enabled)?;

    // Also persist to config.json
    let config = app.state::<Mutex<AppConfig>>();
    if let Ok(mut cfg) = config.lock() {
        cfg.auto_start = enabled;
        let _ = cfg.save();
    }

    // Clean up old VBS script if it exists
    if !enabled {
        let config = app.state::<Mutex<AppConfig>>();
        let data_dir = config.lock().ok().map(|c| c.data_dir.clone());
        if let Some(dir) = data_dir {
            let vbs_path = PathBuf::from(dir).join("start_server.vbs");
            let _ = std::fs::remove_file(vbs_path);
        }
    }

    Ok(())
}

// ============================================================================
// Server Watchdog — Auto-restart on crash
// ============================================================================
//
// Background task that monitors the server health every 15 seconds.
// If the server was started by us (ServerManagedByUs == true) and it goes
// offline, the watchdog restarts it automatically. Capped at 5 consecutive
// restarts to prevent infinite loops. Counter resets on successful health check.
// ============================================================================

const WATCHDOG_INTERVAL_SECS: u64 = 15;
const MAX_CONSECUTIVE_RESTARTS: u32 = 5;

async fn server_watchdog(app: AppHandle) {
    let mut consecutive_restarts: u32 = 0;

    loop {
        tokio::time::sleep(std::time::Duration::from_secs(WATCHDOG_INTERVAL_SECS)).await;

        // Check if we're supposed to be managing the server
        let is_managed = {
            let managed = app.state::<Mutex<ServerManagedByUs>>();
            managed.lock().map(|m| m.0).unwrap_or(false)
        };

        if !is_managed {
            consecutive_restarts = 0;
            continue;
        }

        // Health check
        let server_alive = match reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .build()
        {
            Ok(client) => client
                .get("http://127.0.0.1:7860/v1/health")
                .send()
                .await
                .map_or(false, |r| r.status().is_success()),
            Err(_) => false,
        };

        if server_alive {
            // Server is healthy, reset restart counter
            if consecutive_restarts > 0 {
                eprintln!("[watchdog] Server recovered after {} restart(s)", consecutive_restarts);
                consecutive_restarts = 0;
            }
            continue;
        }

        // Server is down and we manage it — try to restart
        consecutive_restarts += 1;

        if consecutive_restarts > MAX_CONSECUTIVE_RESTARTS {
            eprintln!(
                "[watchdog] Server crashed {} times. Giving up auto-restart. \
                 User must restart manually.",
                MAX_CONSECUTIVE_RESTARTS
            );
            // Disable managed flag to stop retrying
            if let Ok(mut m) = app.state::<Mutex<ServerManagedByUs>>().lock() {
                m.0 = false;
            }
            // Notify the frontend
            let _ = app.emit("server-watchdog-gave-up", consecutive_restarts);
            continue;
        }

        eprintln!(
            "[watchdog] Server offline (attempt {}/{}). Restarting...",
            consecutive_restarts, MAX_CONSECUTIVE_RESTARTS
        );

        // Get config for restart
        let cfg = {
            let config = app.state::<Mutex<AppConfig>>();
            let locked = match config.lock() {
                Ok(c) => c.clone(),
                Err(_) => continue,
            };
            locked
        };

        let python = cfg.python_exe();
        if !python.exists() {
            continue;
        }

        let bootstrap = r#"
import subprocess, sys
_orig = subprocess.Popen.__init__
def _patched(self, *a, **kw):
    if sys.platform == 'win32' and 'creationflags' not in kw:
        kw['creationflags'] = 0x08000000
    _orig(self, *a, **kw)
subprocess.Popen.__init__ = _patched
import runpy; runpy.run_module('server.main', run_name='__main__', alter_sys=True)
"#;
        match std::process::Command::new(python.to_string_lossy().to_string())
            .args(["-c", bootstrap.trim()])
            .current_dir(cfg.data_dir())
            .env("PYTHONPATH", cfg.data_dir())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .creation_flags(0x00000008 | 0x08000000)
            .spawn()
        {
            Ok(child) => {
                // Update the stored process handle
                if let Ok(mut proc) = app.state::<Mutex<ServerProcess>>().lock() {
                    proc.0 = Some(child);
                }
                eprintln!("[watchdog] Server process spawned. Waiting for it to become healthy...");
                // Give it time to start before next health check
                tokio::time::sleep(std::time::Duration::from_secs(5)).await;
            }
            Err(e) => {
                eprintln!("[watchdog] Failed to restart server: {}", e);
            }
        }
    }
}

// ============================================================================
// App Setup — Tray icon, window management
// ============================================================================

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(Mutex::new(ServerProcess(None)))
        .manage(Mutex::new(ServerManagedByUs(false)))
        .manage(Mutex::new(AppConfig::load()))
        .manage(Mutex::new(DownloadPid(None)))
        .invoke_handler(tauri::generate_handler![
            get_server_status,
            get_sapi_status,
            check_gpu,
            get_voices,
            get_registered_voice_ids,
            rename_voice,
            toggle_voice,
            preview_voice,
            qwen3_list_speakers,
            qwen3_clone_voice,
            qwen3_delete_clone,
            qwen3_design_voice,
            qwen3_preview_voice,
            qwen3_narrate,
            save_wav_file,
            qwen3_get_status,
            cancel_qwen3_download,
            open_setup_terminal,
            get_setup_status,
            get_setup_paths,
            set_data_dir,
            setup_download_file,
            setup_extract_zip,
            setup_enable_pip,
            setup_run_command,
            setup_install_server,
            start_server,
            stop_server,
            get_autostart,
            set_autostart,
            get_settings,
            save_settings,
        ])
        .setup(|app| {
            // Build tray menu
            let show = MenuItemBuilder::new("Open VoiceLink").id("show").build(app)?;
            let quit = MenuItemBuilder::new("Quit").id("quit").build(app)?;
            let menu = MenuBuilder::new(app)
                .item(&show)
                .separator()
                .item(&quit)
                .build()?;

            // Attach menu to existing tray icon (defined in tauri.conf.json)
            if let Some(tray) = app.tray_by_id("voicelink-tray") {
                tray.set_menu(Some(menu))?;
                tray.on_menu_event(move |app, event| match event.id().as_ref() {
                    "show" => {
                        if let Some(win) = app.get_webview_window("main") {
                            let _ = win.show();
                            let _ = win.set_focus();
                        }
                    }
                    "quit" => {
                        app.exit(0);
                    }
                    _ => {}
                });
                tray.on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::DoubleClick { .. } = event {
                        if let Some(win) = tray.app_handle().get_webview_window("main") {
                            let _ = win.show();
                            let _ = win.set_focus();
                        }
                    }
                });
            }

            // If launched with --minimized (auto-start on login), keep window
            // hidden and just live in the system tray. Otherwise show normally.
            let minimized = std::env::args().any(|a| a == "--minimized");
            if !minimized {
                if let Some(win) = app.get_webview_window("main") {
                    let _ = win.show();
                }
            }

            // Spawn the server watchdog background task.
            // It monitors server health and auto-restarts on crash.
            let watchdog_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                server_watchdog(watchdog_handle).await;
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running VoiceLink");
}
