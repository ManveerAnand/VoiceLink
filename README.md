<h1 align="center">VoiceLink</h1>
<p align="center">
  <strong>Give your Windows apps a voice that actually sounds human.</strong>
</p>

<br>

## Ever tried listening to an ebook and wanted to throw your laptop?

You open Thorium Reader, find a great book, hit "Read Aloud" and... **Microsoft David** starts talking. Flat. Robotic. Zero emotion. Like a GPS navigator reading Shakespeare.

The same story with Windows Narrator, Edge Read Aloud, or any app that uses the built in Windows voices. They all share the same pool of voices that Microsoft ships, and honestly, they sound like they were recorded in 2005. Because they were.

Here is the frustrating part: **AI voices that sound almost human already exist.** Open source models like [Kokoro](https://github.com/hexgrad/kokoro), Piper, and Qwen TTS can read text with natural pauses, emotions, and rhythm. The kind of voice you would actually enjoy listening to for hours.

But none of your Windows apps can use them. The apps only know how to talk to Microsoft's voice system (called SAPI). And these AI models don't speak SAPI.

**That is the gap VoiceLink fills.**

<br>

## What VoiceLink actually does

VoiceLink makes AI voices show up as regular Windows voices. No hacks, no workarounds. Your apps do not even know the difference.

Here is how it works:

```
Your App                    VoiceLink                    AI Voice Engine
(Thorium, Edge, etc.)       (the bridge)                 (Kokoro, Piper, etc.)
                                                        
  "Read this text"                                      
       |                                                
       +-----> Talks to Windows -----> VoiceLink        
               voice system           receives the text  
                                           |            
                                           +-----> Sends text to the
                                                   AI model running
                                                   on your computer
                                                        |
                                           <-----+     
                                      Gets back natural  
                                      sounding audio     
       <-----+                             |            
  Plays the audio            Streams it back to your app 
  through your speakers                                  
```

From your app's point of view, VoiceLink is just another voice option in the dropdown list, right next to Microsoft David and Zira. But when you select it, you get studio quality AI speech instead.

<br>

## Hear the difference

| Voice | What it sounds like |
|-------|-------------------|
| Microsoft David (built in) | Monotone, robotic, reads every sentence the same way |
| Microsoft Zira (built in) | Slightly better, but still clearly a machine |
| **VoiceLink + Kokoro** | Natural rhythm, proper emphasis, sounds like a real person reading to you |

*(Audio samples coming soon)*

<br>

## How to set it up

> **Note:** VoiceLink is still being built. A one click installer is coming. For now, here is what the setup will look like.

### What you need
1. Windows 10 or 11
2. About 500 MB of free space (for the AI voice model)
3. A reasonably modern computer (a dedicated GPU helps but is not required)

### Installation (coming soon)
1. Download the VoiceLink installer
2. Run it (it handles everything: the voice engine, the AI model, and the Windows registration)
3. Open your favorite app (Thorium Reader, Edge, Narrator, Balabolka, anything with Read Aloud)
4. Pick a VoiceLink voice from the voice list
5. Enjoy actually pleasant text to speech

No terminal. No Python. No configuration files. Just install and go.

<br>

## What works today

The project is being built in stages. Here is where we are:

| Stage | Status | What it means |
|-------|--------|--------------|
| Research and understanding | Done | We know exactly how Windows voices work under the hood |
| AI voice server | Working | A local server that takes text and returns AI generated audio (Kokoro, 11 voices) |
| Windows voice driver | Working | COM DLL registered as SAPI voice — works in Thorium Reader, PowerShell, and other apps |
| Management GUI | Working | Tauri desktop app — dashboard, voice manager with rename/toggle/test, system tray icon |
| Installer | Planned | The one click setup experience |

Check [TASKS.md](TASKS.md) for the full breakdown of every single task.

<br>

## Under the hood (for the curious)

You do not need to understand any of this to use VoiceLink. But if you are the kind of person who likes to know how things work:

**VoiceLink has three parts:**

1. **The Voice Server** runs on your computer and loads the AI model. When it gets text, it generates audio that sounds like a real person. Built with Python and FastAPI.

2. **The Windows Driver** is a small file (a DLL) that registers itself as a Windows voice. When any app asks it to speak, it quietly passes the text to the voice server and streams the audio back. Built with C++.

3. **The Installer** bundles everything together so you never have to touch code. It sets up the server, registers the driver, and downloads the AI model.

The whole thing runs 100% on your computer. No internet needed after setup. No cloud. Your text never leaves your machine.

<br>

## Why this exists

I was reading ebooks in Thorium Reader and the built in voices were genuinely painful to listen to. AI voices that sound incredible exist as open source projects, but there was no simple way to plug them into Windows apps.

So I am building the bridge myself, and learning every layer of how it works along the way. This project is as much about understanding the technology deeply as it is about shipping something useful.

<br>

## Want to help?

This is an open project. Whether you are a developer, a designer, or just someone who wants better voices on Windows, you are welcome here. Open an issue, start a discussion, or just star the repo if you think this should exist.

<br>

## License

MIT. See [LICENSE](LICENSE) for details.

<br>

<p align="center">
  Built by <a href="https://github.com/ManveerAnand">Manveer Anand</a>. Learning in public, one piece at a time.
</p>
