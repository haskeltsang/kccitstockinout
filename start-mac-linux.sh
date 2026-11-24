#!/usr/bin/env sh
# IT Stock Manager - launcher for macOS / Linux
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 was not found. Please install Python 3 first."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating a private Python environment (first run only)..."
  python3 -m venv .venv
fi

. .venv/bin/activate

echo "Installing dependencies (first run only)..."
python -m pip install --quiet --disable-pip-version-check -r requirements.txt

echo ""
echo "Starting IT Stock Manager at http://localhost:8077"
echo "Press Ctrl+C to stop."
echo ""
exec python app.py
