# Pub-Streamer

Desktop streaming client for [Audio Pub](https://audiopub.site). Captures microphone and per-application audio on Windows, applies VST effects, and encodes via ffmpeg to Icecast. Chat TTS, local recording, and Mastodon posting are built in.

## Requirements

- Windows 10 version 2004 (May 2020 Update) or later
- [uv](https://docs.astral.sh/uv/)
- ffmpeg in PATH

## Running from source

```
uv sync
uv run python main.py
```

`config.ini` is created next to `main.py` on first run with default values.

## Building

Releases are built on GitHub Actions. Push a `v*` tag to trigger a build and create a GitHub Release:

```
git tag v1.0.0 && git push origin v1.0.0
```

The workflow uses Nuitka with MinGW on `windows-latest` and produces a standalone `PubStreamer.dist/` folder. MSVC is not used because certain generated C files (pyasn1, requests) exceed its internal heap limit on constrained machines.

To build locally, install MinGW and run:

```
uv run python build.py
```

Set `GITHUB_ACTIONS=true` in the environment or edit `build.py` to pass `--mingw64` explicitly.

## Audio sources

Sources are added from the Sources tab. Each source has independent volume and a VST chain. A master chain processes the final mix before encoding.

**Microphone** — any WASAPI input device enumerated by pyaudiowpatch.

**Application** — per-process WASAPI loopback via `AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS` (Windows 10 2004+). Elevated 32-bit processes (such as NVDA) cannot be captured this way; for those, Pub-Streamer injects `audio_hook32.dll` or `audio_hook64.dll` into the target process, which copies audio out via shared memory.

**Chat TTS** — adding TTS as a source routes its audio through the mixer and into the stream, not just to the local speaker.

## TTS engines

| Engine | Requires |
|--------|----------|
| SAPI 5 | Nothing — uses Windows built-in voices |
| Microsoft Edge | Internet connection |
| gTTS | Internet — uses the Google Translate endpoint |
| OpenAI TTS | API key |
| ElevenLabs | API key; v3 model and speed (0.70–1.20) supported |
| Azure Cognitive Services | Subscription key + region |
| AWS Polly | Access key ID + secret access key |
| Google Cloud TTS | Service account credentials JSON |
| Piper | Local inference — requires an `.onnx` model file |

The TTS queue is capped (default: 5 messages). Messages that arrive when the queue is full are dropped. Press **Escape** to stop the current utterance.

## Configuration

The UI writes all settings to `config.ini`. A few settings have no UI control:

| Section | Key | Default | Notes |
|---------|-----|---------|-------|
| `[audio]` | `chunk_frames` | `1024` | Mixer buffer in frames. Increase (e.g. `4096`) on slow hardware |
| `[audio]` | `bitrate` | `96` | Stream bitrate in kbps |
| `[tts]` | `max_queue` | `5` | Messages queued before dropping |
| `[ui]` | `language` | *(blank)* | `en`, `ja`, or blank for system locale |

## Language

The UI is available in English and Japanese. Switch via **Help → Language**; the change takes effect on next launch.

## Recording

Recording writes to a local file independently of streaming. Split into stems records each source to its own file in a timestamped folder. Recording can be tied to the stream so it starts and stops automatically.

## Mastodon

Posts to a Mastodon instance when a stream goes live. The post template supports `{url}`, `{title}`, and `{description}` substitutions.

## Building native components

`audio_hook32.dll`, `audio_hook64.dll`, and `injector32.exe` are **not** checked into the repo — `LegacyCapture` (used to capture audio from most injectable target processes; see "Application" above) requires them in `native/dist/` and won't work until they're built. To build them you need Visual Studio 2022 (any edition with the C++ workload) and CMake (standalone or the copy bundled with VS):

```
.\build_native.ps1
```

The script locates your VS/CMake install automatically and places the outputs in `native/dist/`.

## License

GPL v3 — see [LICENSE](LICENSE).
