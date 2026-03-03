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

// ============================================================================
// Setup Types & Paths
// ============================================================================

/// Persistent config stored at C:\ProgramData\VoiceLink\config.json
#[derive(Debug, Serialize, Deserialize, Clone)]
struct AppConfig {
    data_dir: String,
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

/// Get list of voices from the inference server, with registry name overrides
#[tauri::command]
async fn get_voices() -> Result<Vec<VoiceInfo>, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build()
        .map_err(|e| e.to_string())?;

    let resp = client
        .get("http://127.0.0.1:7860/v1/voices")
        .send()
        .await
        .map_err(|e| format!("Server not reachable: {}", e))?;

    let mut voices: Vec<VoiceInfo> = resp.json().await.map_err(|e| e.to_string())?;

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

    let token_roots = [
        r"SOFTWARE\Microsoft\Speech\Voices\Tokens",
        r"SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens",
    ];

    let token_name = format!("VoiceLink_{}", voice_id);

    for root in &token_roots {
        let token_path = format!("{}\\{}", root, token_name);

        // Update the token's (Default) value — this is what SAPI apps display
        match hklm.open_subkey_with_flags(&token_path, KEY_SET_VALUE) {
            Ok(key) => {
                key.set_value("", &new_name).map_err(|e| e.to_string())?;
            }
            Err(_) => {}
        }

        // Also update the Attributes\Name subkey
        let attrs_path = format!("{}\\Attributes", token_path);
        match hklm.open_subkey_with_flags(&attrs_path, KEY_SET_VALUE) {
            Ok(key) => {
                key.set_value("Name", &new_name).map_err(|e| e.to_string())?;
            }
            Err(_) => {}
        }
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

/// Toggle a voice on/off in SAPI by adding/removing its registry token
#[tauri::command]
fn toggle_voice(voice_id: String, enabled: bool) -> Result<(), String> {
    use winreg::enums::*;
    use winreg::RegKey;

    // Check if we have admin privileges by trying to open HKLM with write access
    let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
    if hklm
        .open_subkey_with_flags(
            r"SOFTWARE\Microsoft\Speech\Voices\Tokens",
            KEY_WRITE,
        )
        .is_err()
    {
        return Err(
            "Administrator privileges required. Please restart VoiceLink as Administrator to toggle voices."
                .to_string(),
        );
    }

    let token_name = format!("VoiceLink_{}", voice_id);

    let token_roots = [
        r"SOFTWARE\Microsoft\Speech\Voices\Tokens",
        r"SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens",
    ];

    if enabled {
        // Re-register voice token: read CLSID from InprocServer32
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

        // Fetch voice metadata from server to get gender/language
        // For now, use the voice_id to infer language (a=en-US, b=en-GB)
        let lang = if voice_id.starts_with('b') { "809" } else { "409" };
        let gender = if voice_id.contains("_m_") || voice_id.starts_with("am_") || voice_id.starts_with("bm_") {
            "Male"
        } else {
            "Female"
        };
        // Capitalize voice name from ID (e.g. af_heart -> Heart)
        let raw_name = voice_id.split('_').last().unwrap_or(&voice_id);
        let display_name = format!("{} (Kokoro)",
            raw_name.chars().next().map(|c| c.to_uppercase().to_string()).unwrap_or_default()
                + &raw_name[1..]
        );

        for root in &token_roots {
            let token_path = format!("{}\\{}", root, token_name);
            let (token_key, _) = hklm
                .create_subkey_with_flags(&token_path, KEY_WRITE)
                .map_err(|e| format!("Failed to create token key: {}", e))?;

            token_key.set_value("", &display_name).map_err(|e| e.to_string())?;
            token_key.set_value("CLSID", &clsid).map_err(|e| e.to_string())?;
            token_key.set_value("VoiceLinkVoiceId", &voice_id).map_err(|e| e.to_string())?;
            token_key.set_value("VoiceLinkServerPort", &"7860").map_err(|e| e.to_string())?;

            let attrs_path = format!("{}\\Attributes", token_path);
            let (attrs_key, _) = hklm
                .create_subkey_with_flags(&attrs_path, KEY_WRITE)
                .map_err(|e| format!("Failed to create attrs key: {}", e))?;

            attrs_key.set_value("Name", &display_name).map_err(|e| e.to_string())?;
            attrs_key.set_value("Gender", &gender).map_err(|e| e.to_string())?;
            attrs_key.set_value("Language", &lang).map_err(|e| e.to_string())?;
            attrs_key.set_value("Age", &"Adult").map_err(|e| e.to_string())?;
            attrs_key.set_value("Vendor", &"VoiceLink").map_err(|e| e.to_string())?;
        }
    } else {
        // Remove voice token from both registries
        for root in &token_roots {
            // Delete recursively (token + Attributes subkey)
            if let Ok(tokens_key) = hklm.open_subkey_with_flags(root, KEY_WRITE) {
                let _ = tokens_key.delete_subkey_all(&token_name);
            }
        }
    }

    Ok(())
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

    let mut child = cmd.spawn()
        .map_err(|e| format!("Failed to run {}: {}", program, e))?;

    // Read stdout and stderr line-by-line, emitting progress events
    // so the frontend shows real-time status during long pip installs.
    let stdout_handle = child.stdout.take();
    let stderr_handle = child.stderr.take();

    let step_clone = step_name.clone();
    let app_clone = app.clone();
    let stdout_task = tokio::spawn(async move {
        let mut lines = Vec::new();
        if let Some(stdout) = stdout_handle {
            let mut reader = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                // Emit the latest line as status text so user sees activity
                let _ = app_clone.emit(
                    "setup-progress",
                    serde_json::json!({
                        "step": step_clone,
                        "progress": 50,
                        "status": "running",
                        "line": line,
                    }),
                );
                lines.push(line);
            }
        }
        lines.join("\n")
    });

    let step_clone2 = step_name.clone();
    let app_clone2 = app.clone();
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
                lines.push(line);
            }
        }
        lines.join("\n")
    });

    let status = child.wait().await
        .map_err(|e| format!("Failed to wait for {}: {}", program, e))?;

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
    }

    // Wait a moment for server to start
    tokio::time::sleep(std::time::Duration::from_secs(2)).await;

    Ok(())
}

/// Stop the inference server
#[tauri::command]
async fn stop_server(app: AppHandle) -> Result<(), String> {
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
    use winreg::enums::*;
    use winreg::RegKey;

    if enabled {
        // Get the path to the running VoiceLink.exe
        let exe_path = std::env::current_exe()
            .map_err(|e| format!("Failed to get exe path: {}", e))?;

        // Register in HKCU Run key: "path\to\VoiceLink.exe" --minimized
        let hkcu = RegKey::predef(HKEY_CURRENT_USER);
        let (key, _) = hkcu
            .create_subkey(AUTOSTART_REG_KEY)
            .map_err(|e| format!("Failed to open registry: {}", e))?;

        let cmd = format!(
            r#""{}" --minimized"#,
            exe_path.to_string_lossy()
        );
        key.set_value(AUTOSTART_VALUE_NAME, &cmd)
            .map_err(|e| format!("Failed to set registry value: {}", e))?;
    } else {
        // Remove from registry
        let hkcu = RegKey::predef(HKEY_CURRENT_USER);
        if let Ok(key) = hkcu.open_subkey_with_flags(AUTOSTART_REG_KEY, KEY_WRITE) {
            let _ = key.delete_value(AUTOSTART_VALUE_NAME);
        }

        // Clean up old VBS script if it exists
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
// App Setup — Tray icon, window management
// ============================================================================

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(Mutex::new(ServerProcess(None)))
        .manage(Mutex::new(AppConfig::load()))
        .invoke_handler(tauri::generate_handler![
            get_server_status,
            get_sapi_status,
            get_voices,
            get_registered_voice_ids,
            rename_voice,
            toggle_voice,
            preview_voice,
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

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running VoiceLink");
}
