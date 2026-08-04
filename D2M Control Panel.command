#!/bin/bash
# Double-click this file in Finder to launch the D2M Control Panel.
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 isn't installed or isn't on your PATH."
  echo "Install it with:  brew install python3"
  echo "(or from https://www.python.org/downloads/), then double-click this file again."
  read -n 1 -s -r -p "Press any key to close this window..."
  exit 1
fi

python3 control_panel.py
