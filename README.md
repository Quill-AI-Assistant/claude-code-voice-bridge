# Claude Code Voice Bridge

Hands-free voice conversations with Claude Code. Speak commands, hear responses. All voice processing runs locally.

```
You --mic--> Whisper STT --text--> Claude Code --text--> Kokoro TTS --audio--> Speakers
              (local)               (CLI)                 (local)
```

## Why this project

Claude Code has voice input (`/voice`) but no voice output. You can speak to it, but it can't speak back. This bridge closes the loop — full two-way voice conversation with Claude Code, hands-free.

This matters for:

- **Accessibility** — developers with vision or motor disabilities who need hands-free coding
- **Workflow** — architecture discussions, code review, planning — tasks that are naturally conversational
- **Multitasking** — talk to Claude while your hands are on the keyboard, soldering iron, or whiteboard

Everything runs locally. Whisper transcribes your voice on-device. Kokoro speaks Claude's responses on-device. Only the Claude Code CLI touches the network. No API keys for voice, no cloud TTS, no data leaves your machine for speech processing.

Related: [anthropics/claude-code#42226](https://github.com/anthropics/claude-code/issues/42226) — feature request for native bidirectional voice support.

## Quick start

```bash
git clone https://github.com/Quill-AI-Assistant/claude-code-voice-bridge.git
cd claude-code-voice-bridge
./setup.sh
python3 bridge.py
```

Speak into your mic. Claude responds with voice.

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

Type or say these anytime — even while the mic is listening:

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
| `Esc` | Interrupt Claude mid-response |
| `speak faster` | Increase speed |
| `speak slower` | Decrease speed |
| `change voice to X` | Switch voice by speaking |

## How it works

1. A persistent `claude -p --input-format stream-json` subprocess stays alive for the session
2. Your voice is captured by a persistent audio stream with energy-based VAD
3. Speech is sent to a local Whisper server for transcription with confidence scoring
4. Transcribed text goes to Claude Code as a stream-json message — full tool access, file edits, everything
5. Claude's streaming response is chunked by sentence boundaries and queued for TTS
6. A background thread generates audio via Kokoro and plays it with interruptible polling
7. Speak during playback or press Esc to interrupt — playback stops within 50ms

## Confidence scoring

Every voice input shows a colour-coded confidence score from Whisper:

- **Green [85%]** — high confidence, sent as-is
- **Yellow [62%]** — medium confidence, may need clarification
- **Red [30%]** — low confidence, single-word fragments are filtered

Low-confidence single words are automatically rejected to prevent Whisper hallucinations from triggering commands.

## Architecture

```
+----------------------------------------------+
|          Claude Code Voice Bridge             |
|                                               |
|  Mic --> STT --> +---------------+ --> TTS    |
|                  |  Claude Code  |            |
|  Keyboard -----> |  (persistent  | ---------> |
|                  |   subprocess) |            |
|                  +---------------+            |
+----------------------------------------------+
```

- **Single process**: Claude Code runs once, stays alive across turns
- **Bidirectional stream-json**: stdin/stdout as newline-delimited JSON
- **Interruptible**: Voice or Esc stops Claude mid-sentence
- **Type anytime**: Keyboard works even while mic is listening
- **Session portable**: Resume in the bridge or in regular `claude --resume`

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

## Verified

Built and verified by [@ahuzmeza](https://github.com/ahuzmeza).

## License

MIT
