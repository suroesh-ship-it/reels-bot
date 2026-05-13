#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load Homebrew into PATH
eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true

# Check dependencies
if ! command -v python3 &>/dev/null; then
  echo "❌ Python3 not found. Please run install.sh first."
  read -p "Press Enter to close..."
  exit 1
fi

if ! python3 -c "import flask" 2>/dev/null; then
  echo "❌ Flask not found. Please run install.sh first."
  read -p "Press Enter to close..."
  exit 1
fi

echo "🎬 Starting Reels Bot..."
echo "Opening http://localhost:5050 in your browser..."
echo "(Close this window to stop the app)"
echo ""

python3 "$SCRIPT_DIR/app.py"

