#!/usr/bin/env bash
# DeckBot installer for Steam Deck (run from Desktop Mode > Konsole).
# Installs into this folder only - nothing system-wide, no sudo needed.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

echo "== Installing DeckBot =="
if ! command -v python3 >/dev/null; then
  echo "python3 not found. SteamOS normally includes it - try updating SteamOS."; exit 1
fi

rm -rf .venv
if ! python3 -m venv .venv 2>/dev/null; then
  echo "venv without pip, bootstrapping pip..."
  rm -rf .venv
  python3 -m venv --without-pip .venv
  curl -fsSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python - --quiet
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet --upgrade "ccxt>=4.5,<5"
chmod +x deckbot deckbot.py
echo "Installed $(.venv/bin/python -c 'import ccxt; print("ccxt", ccxt.__version__)')"

if [ -f data/config.json ]; then
  echo "Existing settings found - keeping them."
  systemctl --user restart deckbot.service 2>/dev/null && echo "Background bot restarted." || true
else
  restored=no
  if .venv/bin/python -c "import sys; sys.path.insert(0, '.'); import panel; sys.exit(0 if panel.find_backups('deckbot') else 1)" 2>/dev/null; then
    read -r -p "A DeckBot backup was found. Restore it instead of setting up from scratch? [Y/n] " rb
    if [[ ! "${rb:-Y}" =~ ^[Nn] ]] && ./deckbot restore; then
      restored=yes
      ./deckbot start
    fi
  fi
  if [ "$restored" = no ]; then
    ./deckbot setup
    read -r -p "Start DeckBot in the background now? [Y/n] " yn
    if [[ ! "${yn:-Y}" =~ ^[Nn] ]]; then
      ./deckbot start
    fi
  fi
fi
echo
echo "Handy commands (run from this folder):"
echo "  ./deckbot status   ./deckbot logs   ./deckbot go-live   ./deckbot stop"
