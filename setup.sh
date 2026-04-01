#!/usr/bin/env bash
set -euo pipefail

# Voice Bridge setup — installs all dependencies from zero.

echo ""
echo "  Claude Code Voice Bridge — Setup"
echo "  ────────────────────────────────────"
echo ""

# Platform
OS="$(uname -s)"
ARCH="$(uname -m)"
echo "  Platform: $OS $ARCH"

# Python
if ! command -v python3 &>/dev/null; then
    echo "  ERROR: python3 not found. Install Python 3.10+ first."
    exit 1
fi
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "  Python: $PY_VER"

# System dependencies
if [[ "$OS" == "Darwin" ]]; then
    if ! command -v brew &>/dev/null; then
        echo "  ERROR: Homebrew not found. Install from https://brew.sh"
        exit 1
    fi
    echo "  Checking system dependencies..."
    for pkg in portaudio ffmpeg; do
        if ! brew list "$pkg" &>/dev/null; then
            echo "  Installing $pkg..."
            brew install "$pkg"
        else
            echo "  $pkg: ok"
        fi
    done
elif [[ "$OS" == "Linux" ]]; then
    echo "  Checking system dependencies..."
    for pkg in portaudio19-dev ffmpeg; do
        if ! dpkg -s "$pkg" &>/dev/null 2>&1; then
            echo "  Installing $pkg..."
            sudo apt-get install -y "$pkg"
        else
            echo "  $pkg: ok"
        fi
    done
fi

# Python dependencies
echo "  Installing Python packages..."
pip3 install -q httpx numpy sounddevice

# Claude Code
if command -v claude &>/dev/null; then
    echo "  Claude Code: ok"
else
    echo "  WARNING: Claude Code CLI not found."
    echo "  Install from: https://docs.anthropic.com/en/docs/claude-code"
fi

# VoiceMode (Whisper + Kokoro)
if command -v voicemode &>/dev/null; then
    echo "  VoiceMode: ok"
else
    echo "  Installing VoiceMode (STT + TTS services)..."
    if command -v uvx &>/dev/null; then
        uvx voice-mode-install --yes
    else
        echo "  Installing uv first..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
        uvx voice-mode-install --yes
    fi
fi

# Install Whisper + Kokoro if not already
if [[ -d "$HOME/.voicemode/services/whisper" ]]; then
    echo "  Whisper STT: ok"
else
    echo "  Installing Whisper STT..."
    voicemode whisper service install 2>/dev/null || true
fi

if [[ -d "$HOME/.voicemode/services/kokoro" ]]; then
    echo "  Kokoro TTS: ok"
else
    echo "  Installing Kokoro TTS..."
    voicemode kokoro install 2>/dev/null || true
fi

echo ""
echo "  Setup complete. Run:"
echo ""
echo "    python3 bridge.py"
echo ""
echo "  Or with a specific voice:"
echo ""
echo "    python3 bridge.py --voice af_sarah --speed 1.2"
echo ""
