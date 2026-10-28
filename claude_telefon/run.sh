#!/bin/sh
# Add-on options (form in Home Assistant) are the configuration; logs and call notes go to /share
set -e
export CONFIG_PATH=/data/options.json
mkdir -p /share/claude-telefon
exec python -u /opt/claude-telefon/app/main.py
