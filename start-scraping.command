#!/bin/bash
# Mac: double-click this. It sets up what it needs the first time, then collects.
# Everything it installs lives in this folder - nothing is added to your system.
cd "$(dirname "$0")"
echo "Clash Royale replay collector"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is needed. Install it from https://www.python.org/downloads/ and"
  echo "then double-click this file again."
  read -n 1 -s -r -p "Press any key to close."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "First run: setting up (a few minutes, one time only)..."
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q playwright beautifulsoup4 lxml
  ./.venv/bin/playwright install chromium
fi

CFG="coordinator/settings.txt"
SERVER=$(grep -E "^server" "$CFG" | cut -d= -f2- | tr -d " ")
TOKEN=$(grep -E "^token"  "$CFG" | cut -d= -f2- | tr -d " ")
# Ask for the access code rather than making anyone edit a config file, and remember it.
if [ -z "$TOKEN" ] || [ "$TOKEN" = "CHANGE-ME" ] || [ "$TOKEN" = "ASK-THE-ADMIN" ]; then
  echo "You need the access code from whoever invited you."
  echo "It looks like:  cr-1234abcd..."
  echo
  read -r -p "Paste the access code and press Enter: " TOKEN
  TOKEN=$(echo "$TOKEN" | tr -d " \t\r\n")
  if [ -z "$TOKEN" ]; then
    echo "No code entered. Ask the admin for it, then run this again."
    read -n 1 -s -r -p "Press any key to close."
    exit 1
  fi
  printf "server = %s\ntoken = %s\n" "$SERVER" "$TOKEN" > "$CFG"
  echo "Saved. You will not be asked again."
  echo
fi

echo
echo "A browser window will open. Log in to RoyaleAPI there."
echo "It may sit on a loading page for a minute or two - that is normal."
echo

# Restart on crash, sleep or network loss. Ctrl-C twice to stop for real.
while true; do
  ./.venv/bin/python coordinator/client.py --server "$SERVER" --token "$TOKEN"
  code=$?
  [ $code -eq 0 ] && break
  echo
  echo "Stopped unexpectedly. Restarting in 15s - your progress is saved."
  echo "Close this window if you want to stop."
  sleep 15
done
read -n 1 -s -r -p "Done. Press any key to close."
