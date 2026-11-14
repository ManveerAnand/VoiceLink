# Contributing to VoiceLink

Thanks for helping! Bug reports, ideas and code are all welcome.

## Reporting bugs and ideas

Open an [issue](https://github.com/ManveerAnand/VoiceLink/issues/new/choose) and fill in the form. The app you used, your VoiceLink version and your Windows version help a lot.

## Sending code

1. For anything bigger than a small fix, open an issue first so we can agree on the approach before you spend time on it.
2. Fork the repo, create a branch, make your change.
3. Keep each PR focused on one thing. Small PRs get reviewed and merged much faster.
4. Open a pull request against `main` and fill in the template.

## Building locally

These are the same steps CI runs (`.github/workflows/ci.yml`):

```sh
# SAPI bridge (C++), 64-bit and 32-bit
cmake -B sapi_bridge/build -S sapi_bridge -G "Visual Studio 17 2022" -A x64
cmake --build sapi_bridge/build --config Release
cmake -B sapi_bridge/build32 -S sapi_bridge -G "Visual Studio 17 2022" -A Win32
cmake --build sapi_bridge/build32 --config Release

# GUI (Tauri: Node 20 + Rust stable)
cd gui
npm ci
npm run tauri build

# Python server
pip install -r server/requirements.txt
```

## License

By contributing you agree your work is released under this project's [LICENSE](LICENSE).
