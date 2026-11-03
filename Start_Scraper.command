#!/bin/bash
# Maryland General (Governor + Question 3) — double-click to start the scraper.
cd "$(dirname "$0")"
echo ""; echo "Starting Maryland General Election scraper..."; echo ""
if ! python3 -c "from playwright.sync_api import sync_playwright" 2>/dev/null; then
  echo "Installing Playwright for AP results (one-time setup)..."
  pip3 install playwright --break-system-packages -q
  python3 -m playwright install chromium
fi
( sleep 4; open "http://localhost:8765" ) &
python3 MD_General_Scraper.py
read -p "Press Enter to close..."
