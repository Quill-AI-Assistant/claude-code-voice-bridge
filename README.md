# Claude Code Voice Bridge

Talk to Claude Code hands-free. All voice processing runs locally.

```
You ──mic──> Whisper STT ──text──> Claude Code ──text──> Kokoro TTS ──audio──> Speakers
             (local)                (CLI)                 (local)
```

## Quick start

```bash
# One-time setup (installs everything)
./setup.sh

# Run
python3 bridge.py
```

That's it. Speak into your mic. Claude responds with voice.

The bridge auto-starts STT/TTS services if they're not running.

## Usage

```bash
python3 bridge.py                          # new session, full voice
python3 bridge.py --voice af_sarah         # pick a voice
python3 bridge.py --speed 1.3              # speak faster
python3 bridge.py --resume abc123          # resume a previous session
python3 bridge.py --mic-off               # type input, hear output
python3 bridge.py --tts-off               # voice input, read output
python3 bridge.py --calibrate             # auto-detect mic threshold
```

## Runtime commands

Type or say these anytime:

| Command | What it does |
|---|---|
| `/help` | Show all commands |
| `/voice NAME` | Change voice |
| `/voices` | List available voices |
| `/speed N` | Set speed (0.5-2.0) |
| `/mic on\|off` | Toggle microphone |
| `/tts on\|off` | Toggle voice output |
| `/calibrate` | Auto-set mic threshold |
| `/sessions` | List recent sessions |
| `/attach ID` | Switch to a session |
| `/session` | Show current status |
| `/exit` | Quit |
| `speak faster` | Increase speed |
| `speak slower` | Decrease speed |
| `change voice to X` | Switch voice |

## How it works

1. A persistent `claude -p --input-format stream-json` subprocess stays alive for the session
2. Your voice is captured by a persistent audio stream with energy-based VAD
3. Speech audio goes to a local Whisper server for transcription
4. Text goes to Claude Code as a stream-json message — full tool access, file edits, everything
5. Claude's streaming response is chunked by sentence and queued for TTS
6. A background thread generates audio via Kokoro and plays it through speakers
7. Speak during playback to interrupt — playback polls every 50ms for your voice

## Prerequisites

| Requirement | Auto-installed by setup.sh |
|---|---|
| Python 3.10+ | No |
| Claude Code CLI | No |
| PortAudio | Yes (via Homebrew/apt) |
| FFmpeg | Yes (via Homebrew/apt) |
| VoiceMode (Whisper + Kokoro) | Yes |

## Custom STT/TTS

Any OpenAI-compatible endpoint works:

```bash
python3 bridge.py --stt-url http://localhost:9000/v1/audio/transcriptions \
                  --tts-url http://localhost:9000/v1/audio/speech
```

Tested with: Whisper.cpp, Deepgram, Kokoro, OpenAI TTS, ElevenLabs.

## Voices

With Kokoro TTS, you get 60+ voices. Run `/voices` for the full list. Some favorites:

| Voice | ID |
|---|---|
| Sky (default) | `af_sky` |
| Nova | `af_nova` |
| Sarah | `af_sarah` |
| Adam | `am_adam` |
| Emma | `bf_emma` |
| George | `bm_george` |

## Architecture

```
┌─────────────────────────────────────────────┐
│              Voice Bridge                    │
│                                              │
│  Mic ──> STT ──> ┌──────────────┐ ──> TTS  │
│                  │  Claude Code  │          │
│  Keyboard ─────> │  (persistent  │ ────────>│
│                  │   subprocess) │          │
│                  └──────────────┘          │
└─────────────────────────────────────────────┘
```

- **Single process**: Claude Code runs once, stays alive across turns
- **Bidirectional stream-json**: stdin/stdout as newline-delimited JSON
- **Interruptible**: Speak to stop Claude mid-sentence
- **Type anytime**: Keyboard works even while mic is listening
- **Session portable**: Resume in the bridge or in regular `claude --resume`

## License

MIT
