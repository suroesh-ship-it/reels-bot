#!/bin/bash
set -e

echo ""
echo "🎬 Reels Bot — One-time Setup"
echo "=============================="
echo ""

# ── Homebrew ───────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
  echo "Installing Homebrew (you may be asked for your Mac password)..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv)"
  echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
else
  echo "✅ Homebrew already installed"
fi

# ── ffmpeg ─────────────────────────────────────────────────────────────────
if ! command -v ffmpeg &>/dev/null; then
  echo "Installing ffmpeg..."
  brew install ffmpeg
else
  echo "✅ ffmpeg already installed"
fi

# ── Python packages ────────────────────────────────────────────────────────
echo "Installing Python packages (this may take a few minutes)..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
pip3 install -r "$SCRIPT_DIR/requirements.txt" --quiet

echo ""
echo "✅ All done! Setup complete."
echo ""
echo "To start the app, double-click start.sh"
echo "(or run:  bash \"$SCRIPT_DIR/start.sh\")"
echo ""
