// ============================================================================
// VoiceLink GUI — Tauri Backend
// ============================================================================
//
// This is the Rust side of the management app. It provides:
//   1. System tray icon with status + menu
//   2. Tauri commands callable from the web frontend
//   3. Server health monitoring
//   4. Voice registry management (rename, enable/disable)
//
// The frontend (HTML/CSS/JS) calls these via tauri::invoke("command_name").
// ============================================================================

use serde::{Deserialize, Serialize};
use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::TrayIconEvent,
    Manager,
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

/// Get list of voices from the inference server
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

    let voices: Vec<VoiceInfo> = resp.json().await.map_err(|e| e.to_string())?;
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

        // Open the Attributes subkey and update the Name value
        let attrs_path = format!("{}\\Attributes", token_path);
        match hklm.open_subkey_with_flags(&attrs_path, KEY_SET_VALUE) {
            Ok(key) => {
                key.set_value("Name", &new_name).map_err(|e| e.to_string())?;
            }
            Err(_) => {
                // Try setting Name at the token level (some tokens store it there)
                if let Ok(key) = hklm.open_subkey_with_flags(&token_path, KEY_SET_VALUE) {
                    let _ = key.set_value("Name", &new_name);
                }
            }
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

    let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
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
        let display_name = format!("VoiceLink {} (Kokoro)",
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
// App Setup — Tray icon, window management
// ============================================================================

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            get_server_status,
            get_sapi_status,
            get_voices,
            get_registered_voice_ids,
            rename_voice,
            toggle_voice,
            preview_voice,
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

            // Show the main window after setup
            if let Some(win) = app.get_webview_window("main") {
                let _ = win.show();
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running VoiceLink");
}
