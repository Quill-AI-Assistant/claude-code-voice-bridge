#!/usr/bin/env python3
"""
Claude Code Voice Bridge.

Speak -> Whisper -> Claude Code -> Kokoro -> Speakers

    python3 bridge.py
    python3 bridge.py --voice af_sarah --speed 1.3
    python3 bridge.py --resume SESSION_ID
    python3 bridge.py --mic-off
"""

from __future__ import annotations

import argparse
import atexit
import collections
import glob
import io
import json
import os
import platform
import queue
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave

# Suppress PortAudio C-level stderr (macOS AUHAL warnings).
# Preserve Python's sys.stderr so exceptions still print.
if platform.system() == "Darwin":
    _saved_fd = os.dup(2)
    _devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(_devnull, 2)
    os.close(_devnull)
    sys.stderr = os.fdopen(_saved_fd, "w", closefd=False)

import httpx
import numpy as np
import sounddevice as sd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STT_URL = "http://127.0.0.1:2022/v1/audio/transcriptions"
TTS_URL = "http://127.0.0.1:8880/v1/audio/speech"

SAMPLE_RATE = 16000
TTS_SAMPLE_RATE = 24000
FRAME = int(SAMPLE_RATE * 0.03)

VOICE = "af_sky"
SPEED = 1.0
ENERGY = 500
SPEECH_FRAMES = 3
SILENCE_FRAMES = 30
MAX_RECORD = 30
MIN_TTS_BYTES = 960
MIN_TTS_BUF = 10
RESPONSE_TIMEOUT = 120
POLL_INTERVAL = 0.05
SID_DISPLAY = 8
TTS_QUEUE_MAX = 50

GHOSTS = re.compile(r'^\[.*\]$|^\.+$|^(thank you|thanks)\.?$|^you$', re.I)
SPLIT = re.compile(r'(?<=[.!?])\s+|\n')

C_RESET  = "\033[0m"
C_BOLD   = "\033[1m"
C_DIM    = "\033[90m"
C_USER   = "\033[38;5;117m"
C_CLAUDE = "\033[38;5;183m"
C_THINK  = "\033[38;5;208m"
C_CMD    = "\033[38;5;222m"
C_OK     = "\033[38;5;114m"
C_ERR    = "\033[38;5;203m"
C_WARN   = "\033[38;5;215m"

VOICE_COMMANDS = (
    "help", "stop bridge", "exit bridge",
    "voices", "sessions", "calibrate", "status", "session",
)


def _rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


# ---------------------------------------------------------------------------
# Mic
# ---------------------------------------------------------------------------

class Mic:
    def __init__(self, device=None, energy=ENERGY):
        self.energy = energy
        self.enabled = True
        self._q: queue.Queue = queue.Queue()
        self._streak = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=FRAME, device=device, callback=self._cb)
            self._stream.start()
        except sd.PortAudioError as e:
            print(f"{C_ERR}  Mic failed to open: {e}{C_RESET}")
            print(f"{C_CMD}  Try: pkill -f bridge.py{C_RESET}")
            sys.exit(1)

    def _cb(self, data, frames, time_info, status):
        frame = data[:, 0].copy()
        if self.enabled:
            self._q.put(frame)
        with self._lock:
            self._streak = self._streak + 1 if _rms(frame) > self.energy else 0

    def record(self) -> np.ndarray | None:
        if not self.enabled:
            return None
        pre: collections.deque = collections.deque(maxlen=10)
        out: list = []
        loud_n = quiet_n = 0
        recording = False
        rec_start = 0.0

        self._drain()
        sys.stdout.write(f"\r{C_CMD}  Listening... {C_RESET}")
        sys.stdout.flush()

        while not self._stop.is_set():
            try:
                frame = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            loud = _rms(frame) > self.energy
            if not recording:
                pre.append(frame)
                loud_n = loud_n + 1 if loud else 0
                if loud_n >= SPEECH_FRAMES:
                    recording = True
                    out.extend(pre)
                    rec_start = time.time()
                    sys.stdout.write(f"\r{C_OK}  Recording... {C_RESET}")
                    sys.stdout.flush()
            else:
                out.append(frame)
                quiet_n = 0 if loud else quiet_n + 1
                if quiet_n >= SILENCE_FRAMES or time.time() - rec_start > MAX_RECORD:
                    break

        if not out:
            return None
        audio = np.concatenate(out)
        dur = len(audio) / SAMPLE_RATE
        if dur < 0.5:
            return None
        print(f"\r{C_DIM}  {dur:.1f}s captured {C_RESET}")
        return audio

    def interrupted(self) -> bool:
        with self._lock:
            if self._streak >= 3:
                self._streak = 0
                return True
        return False

    def calibrate(self) -> int:
        print(f"{C_THINK}  Calibrating (2s silence)... {C_RESET}", end="", flush=True)
        samples: list[float] = []
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                samples.append(_rms(self._q.get(timeout=0.1)))
            except queue.Empty:
                pass
        if not samples:
            return self.energy
        ambient = sorted(samples)[int(len(samples) * 0.9)]
        self.energy = max(200, min(3000, int(ambient * 3)))
        print(f"\r{C_CMD}  Threshold: {self.energy} (ambient: {int(ambient)})  {C_RESET}")
        return self.energy

    def _drain(self):
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def stop(self):
        self._stop.set()
        self._stream.stop()
        self._stream.close()


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------

class STT:
    def __init__(self, url=STT_URL):
        self.url = url
        self._http = httpx.Client(timeout=30)

    def transcribe(self, audio: np.ndarray) -> tuple[str, float | None]:
        """Returns (text, confidence 0-1 or None)."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(audio.tobytes())
        try:
            r = self._http.post(self.url,
                                files={"file": ("a.wav", buf.getvalue(), "audio/wav")},
                                data={"model": "whisper-1",
                                      "response_format": "verbose_json"})
            r.raise_for_status()
            d = r.json()
            text = d.get("text", "").strip()
            conf = None
            segments = d.get("segments", [])
            if segments:
                logprobs = [s["avg_logprob"] for s in segments if "avg_logprob" in s]
                if logprobs:
                    conf = min(1.0, max(0.0, 1.0 + sum(logprobs) / len(logprobs)))
            return text, conf
        except httpx.HTTPStatusError:
            try:
                r = self._http.post(self.url,
                                    files={"file": ("a.wav", buf.getvalue(), "audio/wav")},
                                    data={"model": "whisper-1"})
                r.raise_for_status()
                return r.json().get("text", "").strip(), None
            except Exception as e:
                print(f"{C_ERR}  STT: {e}{C_RESET}")
                return "", None
        except Exception as e:
            print(f"{C_ERR}  STT: {e}{C_RESET}")
            return "", None

    def close(self):
        self._http.close()


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

class Speaker:
    def __init__(self, url=TTS_URL, voice=VOICE, speed=SPEED):
        self.url = url
        self.voice = voice
        self.speed = speed
        self.enabled = True
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._playing = threading.Event()
        self._cut = threading.Event()
        self._http = httpx.Client(timeout=30)
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                text = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            if not text or self._cut.is_set() or not self.enabled:
                self._q.task_done()
                continue
            self._playing.set()
            try:
                self._speak(text)
            finally:
                self._playing.clear()
                self._q.task_done()

    def _speak(self, text: str):
        text = re.sub(r'[`*_~#\[\](){}<>|]', '', text).strip()
        if len(text) < 2:
            return
        payload = {"model": "tts-1", "input": text,
                   "voice": self.voice, "response_format": "pcm"}
        if self.speed != 1.0:
            payload["speed"] = self.speed
        try:
            r = self._http.post(self.url, json=payload)
            if r.status_code != 200 or len(r.content) < MIN_TTS_BYTES:
                return
            pcm = np.frombuffer(r.content, dtype=np.int16).astype(np.float32) / 32768.0
            sd.play(pcm, samplerate=TTS_SAMPLE_RATE, blocksize=2048)
            while sd.get_stream() and sd.get_stream().active:
                if self._cut.is_set():
                    sd.stop()
                    return
                time.sleep(POLL_INTERVAL)
        except sd.PortAudioError:
            pass
        except Exception:
            try:
                self._http.close()
            except Exception:
                pass
            self._http = httpx.Client(timeout=30)

    def say(self, text: str):
        if self.enabled and not self._cut.is_set() and self._q.qsize() < TTS_QUEUE_MAX:
            self._q.put(text)

    def say_sync(self, text: str):
        if self.enabled:
            self._cut.clear()
            self._speak(text)

    @property
    def busy(self) -> bool:
        return self._playing.is_set() or not self._q.empty()

    def interrupt(self):
        self._cut.set()
        while not self._q.empty():
            try:
                self._q.get_nowait()
                self._q.task_done()
            except queue.Empty:
                break
        sd.stop()

    def resume(self):
        self._cut.clear()

    def drain(self):
        self._q.join()

    def stop(self):
        self._stop.set()
        sd.stop()
        self._http.close()


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

class Claude:
    def __init__(self, session=None, cwd=None):
        self.session = session
        self.cwd = cwd or os.getcwd()
        self._proc = None
        self._q: queue.Queue = queue.Queue()
        self._text = ""

    def start(self):
        self.kill()
        self._text = ""
        self._q = queue.Queue()
        cmd = ["claude", "-p", "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose",
               "--strict-mcp-config"]
        if self.session:
            cmd += ["--resume", self.session]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=self.cwd, text=True, bufsize=1)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        has_response = False
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") == "assistant":
                    has_response = True
                    for block in (obj.get("message", {}).get("content") or []):
                        if block.get("type") == "text":
                            txt = block["text"]
                            new = txt[len(self._text):] if txt.startswith(self._text) else txt
                            if new:
                                self._text = txt
                                self._q.put(new)
                elif obj.get("type") == "result":
                    sid = obj.get("session_id", "")
                    if sid:
                        self.session = sid
                    if has_response:
                        self._q.put(None)
                        has_response = False
                    self._text = ""
        except Exception:
            pass
        self._q.put(None)

    def send(self, text: str):
        if not self._proc or self._proc.poll() is not None:
            self.start()
        msg = json.dumps({"type": "user", "message": {"role": "user", "content": text}})
        try:
            self._proc.stdin.write(msg + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self.start()
            time.sleep(0.5)
            try:
                self._proc.stdin.write(msg + "\n")
                self._proc.stdin.flush()
            except Exception:
                return
        while True:
            try:
                chunk = self._q.get(timeout=RESPONSE_TIMEOUT)
            except queue.Empty:
                break
            if chunk is None:
                break
            yield chunk

    def kill(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


# ---------------------------------------------------------------------------
# Text -> TTS chunker
# ---------------------------------------------------------------------------

def chunk_to_tts(buf: str, spk: Speaker) -> str:
    while len(buf) > MIN_TTS_BUF:
        m = SPLIT.search(buf)
        if m:
            sentence = buf[:m.end()].strip()
            buf = buf[m.end():]
            if sentence:
                spk.say(sentence)
        else:
            break
    return buf


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

HELP = f"""
  {C_BOLD}Commands{C_RESET}
  /voice NAME     change voice         /voices        list voices
  /speed N        speed 0.5-2.0        /mic on|off    toggle mic
  /tts on|off     toggle voice out     /sessions      list sessions
  /attach ID      switch session       /session       status
  /calibrate      auto-set mic level   /help          this
  Esc             interrupt Claude     Ctrl+C         quit
  /exit or say "stop bridge"           exit
"""


def handle_command(text: str, mic, spk: Speaker, cc: Claude) -> bool:
    cmd = text.strip().lower().rstrip(".!?,")

    if cmd in ("/help", "help"):
        print(HELP)
        return True

    if cmd in ("/exit", "/quit", "stop bridge", "exit bridge"):
        raise KeyboardInterrupt

    if cmd in ("/mic on", "/mic off", "mic on", "mic off"):
        if mic:
            mic.enabled = "on" in cmd
            print(f"{C_CMD}  Mic {'on' if mic.enabled else 'off'}{C_RESET}")
        return True

    if cmd in ("/tts on", "/tts off", "tts on", "tts off"):
        spk.enabled = "on" in cmd
        print(f"{C_CMD}  TTS {'on' if spk.enabled else 'off'}{C_RESET}")
        return True

    if cmd.startswith("/voice "):
        new_voice = re.sub(r'[^a-zA-Z0-9_]', '', text.strip()[7:].strip())
        if new_voice:
            spk.voice = new_voice
            print(f"{C_CMD}  Voice: {spk.voice}{C_RESET}")
            spk.say_sync(f"Voice changed to {spk.voice}.")
        else:
            print(f"{C_CMD}  Invalid voice name{C_RESET}")
        return True

    if cmd in ("/voices", "voices"):
        base = spk.url.rsplit("/audio/speech", 1)[0] if "/audio/speech" in spk.url else spk.url
        try:
            r = httpx.get(f"{base}/audio/voices", timeout=5)
            voices = r.json().get("voices", []) if r.status_code == 200 else []
            voices = [v for v in voices if "v0" not in v]
            print(f"{C_CMD}  {', '.join(voices[:20])}{C_RESET}")
        except Exception:
            print(f"{C_CMD}  Could not reach TTS server{C_RESET}")
        return True

    if cmd.startswith("/speed "):
        try:
            spk.speed = round(max(0.5, min(2.0, float(cmd[7:]))), 1)
        except ValueError:
            pass
        print(f"{C_CMD}  Speed: {spk.speed:.1f}x{C_RESET}")
        return True

    if "speak faster" in cmd:
        spk.speed = round(min(2.0, spk.speed + 0.2), 1)
        print(f"{C_CMD}  Speed: {spk.speed:.1f}x{C_RESET}")
        return True

    if "speak slower" in cmd:
        spk.speed = round(max(0.5, spk.speed - 0.2), 1)
        print(f"{C_CMD}  Speed: {spk.speed:.1f}x{C_RESET}")
        return True

    for keyword in ("change voice", "switch voice", "voice of", "voice to"):
        if keyword in cmd:
            raw = cmd.split(keyword)[-1].strip()
            if raw.startswith("to "):
                raw = raw[3:]
            voice = re.sub(r'[^a-zA-Z0-9_]', '', raw)
            if voice:
                spk.voice = voice
                print(f"{C_CMD}  Voice: {voice}{C_RESET}")
                spk.say_sync(f"Voice changed to {voice}.")
            return True

    if cmd in ("/calibrate", "calibrate"):
        if mic:
            mic.calibrate()
        else:
            print(f"{C_CMD}  No mic{C_RESET}")
        return True

    if cmd in ("/sessions", "/list", "sessions", "list sessions"):
        sessions = list_sessions()
        if not sessions:
            print(f"{C_CMD}  No sessions found{C_RESET}")
        for sid, mtime, size in sessions:
            cur = f" {C_OK}<-{C_CMD}" if sid == cc.session else ""
            ts = time.strftime("%H:%M", time.localtime(mtime))
            print(f"{C_CMD}  {sid[:SID_DISPLAY]}..  {ts}  {size // 1024}KB{cur}{C_RESET}")
        return True

    if cmd.startswith("/attach "):
        partial = text.strip()[8:].strip()
        match = next((s for s, _, _ in list_sessions() if s.startswith(partial)), None)
        if match:
            cc.session = match
            cc.start()
            time.sleep(0.5)
            print(f"{C_CMD}  Attached: {match[:SID_DISPLAY]}...{C_RESET}")
        else:
            print(f"{C_ERR}  Not found: {partial}{C_RESET}")
        return True

    if cmd in ("/session", "/status", "session", "status"):
        print(f"{C_CMD}  Session: {cc.session or '(new)'}")
        print(f"  Mic: {'on' if mic and mic.enabled else 'off'}"
              f" | TTS: {'on' if spk.enabled else 'off'}"
              f" | Voice: {spk.voice}"
              f" | Speed: {spk.speed}x{C_RESET}")
        return True

    return False


def list_sessions():
    base = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(base):
        return []
    sessions = []
    for path in glob.glob(os.path.join(base, "*", "*.jsonl")):
        sid = os.path.basename(path).replace(".jsonl", "")
        if len(sid) > 30 and "-" in sid:
            sessions.append((sid, os.path.getmtime(path), os.path.getsize(path)))
    sessions.sort(key=lambda x: x[1], reverse=True)
    return sessions[:10]


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def _service_up(url: str, post: bool = False) -> bool:
    try:
        if post:
            httpx.post(url, json={"model": "tts-1", "input": ".",
                       "voice": "af_sky", "response_format": "pcm"}, timeout=5)
        else:
            base = url.rsplit("/audio/", 1)[0] if "/audio/" in url else url
            httpx.get(base, timeout=2)
        return True
    except Exception:
        return False


def _try_start_service(name: str, check_fn=None, retries: int = 5) -> bool:
    vm = shutil.which("voicemode")
    if not vm:
        return False
    try:
        subprocess.run([vm, "service", "start", name],
                       capture_output=True, timeout=60)
    except Exception:
        return False
    for _ in range(retries):
        time.sleep(3)
        if check_fn and check_fn():
            return True
    return check_fn() if check_fn else True


def preflight(mic_on, tts_on, stt_url, tts_url):
    errors = []
    if not shutil.which("claude"):
        errors.append("claude CLI not found — https://docs.anthropic.com/en/docs/claude-code")

    if mic_on and not _service_up(stt_url):
        print(f"{C_THINK}  Starting STT... {C_RESET}", end="", flush=True)
        if _try_start_service("whisper", lambda: _service_up(stt_url)):
            print(f"\r{C_OK}  STT ready        {C_RESET}")
        else:
            print(f"\r{C_RESET}")
            errors.append(f"STT not reachable: {stt_url}")

    if tts_on and not _service_up(tts_url, post=True):
        print(f"{C_THINK}  Starting TTS... {C_RESET}", end="", flush=True)
        if _try_start_service("kokoro", lambda: _service_up(tts_url, post=True)):
            print(f"\r{C_OK}  TTS ready        {C_RESET}")
        else:
            print(f"\r{C_RESET}")
            errors.append(f"TTS not reachable: {tts_url}")

    return errors


# ---------------------------------------------------------------------------
# Terminal input
# ---------------------------------------------------------------------------

_term_state = None


def _setup_terminal():
    global _term_state
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        _term_state = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        atexit.register(_restore_terminal)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except Exception:
        pass


def _restore_terminal():
    global _term_state
    if _term_state is not None:
        try:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _term_state)
            _term_state = None
        except Exception:
            pass


def _start_input_thread(typed: queue.Queue, esc_pressed: threading.Event):
    def reader():
        buf = ""
        while True:
            try:
                ch = os.read(sys.stdin.fileno(), 1)
            except Exception:
                typed.put(None)
                break
            if not ch:
                typed.put(None)
                break
            byte = ch[0]

            if byte == 0x1b:
                ready, _, _ = select.select([sys.stdin.fileno()], [], [], 0.05)
                if ready:
                    try:
                        os.read(sys.stdin.fileno(), 8)
                    except Exception:
                        pass
                else:
                    esc_pressed.set()
            elif byte == 0x03:
                typed.put(None)
                break
            elif byte in (0x0a, 0x0d):
                if buf.strip():
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    typed.put(buf.strip())
                buf = ""
            elif byte == 0x7f:
                if buf:
                    buf = buf[:-1]
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            elif 0x20 <= byte < 0x7f:
                buf += chr(byte)
                sys.stdout.write(chr(byte))
                sys.stdout.flush()

    threading.Thread(target=reader, daemon=True).start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Claude Code Voice Bridge")
    p.add_argument("--mic-off", action="store_true", help="keyboard only")
    p.add_argument("--tts-off", action="store_true", help="no voice output")
    p.add_argument("--voice", default=VOICE, help="TTS voice")
    p.add_argument("--speed", type=float, default=SPEED, help="TTS speed")
    p.add_argument("--resume", metavar="ID", help="session ID")
    p.add_argument("--device", type=int, help="mic device index")
    p.add_argument("--energy", type=int, default=ENERGY, help="VAD threshold")
    p.add_argument("--calibrate", action="store_true", help="auto-set mic level")
    p.add_argument("--stt-url", default=STT_URL)
    p.add_argument("--tts-url", default=TTS_URL)
    p.add_argument("--working-dir", metavar="DIR")
    args = p.parse_args()

    errors = preflight(not args.mic_off, not args.tts_off, args.stt_url, args.tts_url)
    if errors:
        for e in errors:
            print(f"{C_ERR}  {e}{C_RESET}")
        sys.exit(1)

    mic = Mic(args.device, args.energy) if not args.mic_off else None
    if mic and args.calibrate:
        mic.calibrate()

    spk = Speaker(args.tts_url, args.voice, args.speed)
    spk.enabled = not args.tts_off
    stt = STT(args.stt_url) if mic else None
    cc = Claude(args.resume, args.working_dir)

    mic_l = f"{C_OK}on{C_RESET}" if mic else f"{C_DIM}off{C_RESET}"
    tts_l = f"{C_OK}on{C_RESET}" if spk.enabled else f"{C_DIM}off{C_RESET}"
    print(f"""
{C_BOLD}  Claude Code Voice Bridge{C_RESET}
  Mic {mic_l}  TTS {tts_l}  Voice {C_BOLD}{args.voice}{C_RESET}  Speed {args.speed}x
  Session {args.resume or '(new)'}  |  say "help" or type /help
""")

    print(f"{C_THINK}  Starting Claude Code... {C_RESET}", end="", flush=True)
    cc.start()
    time.sleep(0.5)
    print(f"\r{C_OK}  Claude Code ready       {C_RESET}\n")

    if spk.enabled:
        spk.say_sync("Claude Code Voice Bridge Ready.")

    typed: queue.Queue[str | None] = queue.Queue()
    esc_pressed = threading.Event()
    _setup_terminal()
    _start_input_thread(typed, esc_pressed)

    try:
        while True:
            text = None

            try:
                line = typed.get_nowait()
                if line is None:
                    break
                text = line
            except queue.Empty:
                pass

            if text is None and mic and mic.enabled:
                audio = mic.record()
                if audio is None:
                    continue
                sys.stdout.write(f"\r{C_THINK}  Transcribing... {C_RESET}")
                sys.stdout.flush()
                text, conf = stt.transcribe(audio)
                if not text or GHOSTS.match(text.strip()):
                    print(f"\r{C_DIM}  (nothing)           {C_RESET}")
                    continue
                low = text.strip().lower().rstrip(".!?,")
                is_cmd = low.startswith("/") or low in VOICE_COMMANDS
                if len(text.split()) < 2 and not is_cmd:
                    if conf is None or conf < 0.5:
                        print(f"\r{C_DIM}  (fragment: \"{text}\") {C_RESET}")
                        continue
                if conf is not None:
                    pct = int(conf * 100)
                    conf_color = C_OK if pct >= 80 else C_WARN if pct >= 50 else C_ERR
                    conf_tag = f" {conf_color}[{pct}%]{C_RESET}"
                else:
                    conf_tag = ""
                print(f"\r{C_USER}  You: {text}{conf_tag}{C_RESET}")

            elif text is None:
                try:
                    line = typed.get(timeout=0.2)
                    if line is None:
                        break
                    text = line
                except queue.Empty:
                    continue

            if text is None:
                continue

            if handle_command(text, mic, spk, cc):
                continue

            spk.resume()
            t0 = time.time()
            sys.stdout.write(f"{C_THINK}  Thinking... {C_RESET}")
            sys.stdout.flush()

            buf = ""
            cut = False
            started = False

            for chunk in cc.send(text):
                if not started:
                    latency = time.time() - t0
                    started = True
                    sys.stdout.write(f"\r{C_CLAUDE}  Claude:{C_RESET} ")
                    sys.stdout.flush()

                voice_int = mic and mic.enabled and spk.busy and mic.interrupted()
                key_int = esc_pressed.is_set()
                if not cut and (voice_int or key_int):
                    esc_pressed.clear()
                    print(f"\n{C_WARN}  [interrupted]{C_RESET}")
                    spk.interrupt()
                    cut = True
                    break

                buf += chunk
                sys.stdout.write(chunk)
                sys.stdout.flush()
                if spk.enabled:
                    buf = chunk_to_tts(buf, spk)

            if not cut:
                if buf.strip() and spk.enabled:
                    spk.say(buf.strip())
                if started:
                    sid = cc.session[:SID_DISPLAY] if cc.session else ""
                    print(f"\n{C_DIM}  [{latency:.1f}s {sid}]{C_RESET}")
                else:
                    sys.stdout.write(f"\r{C_DIM}  (no response)       {C_RESET}\n")

                while spk.enabled and spk.busy:
                    voice_int = mic and mic.enabled and mic.interrupted()
                    key_int = esc_pressed.is_set()
                    if voice_int or key_int:
                        esc_pressed.clear()
                        print(f"{C_WARN}  [interrupted]{C_RESET}")
                        spk.interrupt()
                        break
                    time.sleep(POLL_INTERVAL)

            print()

    except KeyboardInterrupt:
        pass
    finally:
        _restore_terminal()
        try:
            sd.stop()
        except Exception:
            pass
        if mic:
            try:
                mic.stop()
            except Exception:
                pass
        try:
            spk.stop()
        except Exception:
            pass
        if stt:
            stt.close()
        cc.kill()
        print(f"\n{C_CMD}  Stopped.{C_RESET}")
        if cc.session:
            print(f"{C_CMD}  Session: {cc.session}{C_RESET}")
            print(f"{C_CMD}  Resume:  python3 bridge.py --resume {cc.session[:SID_DISPLAY]}{C_RESET}")
        print()


if __name__ == "__main__":
    main()
