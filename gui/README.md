# VoiceLink Management GUI

Desktop management app for VoiceLink. Built with [Tauri v2](https://tauri.app/) (Rust backend + HTML/CSS/TypeScript frontend).

## Features

- **Dashboard** — Server status, SAPI bridge status, quick voice test
- **Voice Manager** — Rename, enable/disable, test individual voices
- **Settings** — Server URL, auto-start, about info
- **Setup Wizard** — First-run experience: downloads Python, installs deps, fetches model, starts server
- **System Tray** — Background icon with Open/Quit menu

## Development

```bash
npm install
npm run tauri dev
```

## Build

```bash
npm run tauri build
```

Produces `VoiceLink_0.1.0_x64-setup.exe` in `src-tauri/target/release/bundle/nsis/`.

## Architecture

- **Frontend:** Vanilla TypeScript + Vite, single-page app with sidebar navigation
- **Backend:** Rust (Tauri commands) — registry ops via `winreg`, HTTP via `reqwest`, async via `tokio`
- **Installer:** NSIS via Tauri bundler with custom hooks for COM DLL registration
