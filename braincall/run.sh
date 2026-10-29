#!/bin/sh
# Add-on options (form in Home Assistant) are the configuration.
# Logs and call notes go to /config, which is /app_configs/<slug> in Home Assistant.
set -e
export CONFIG_PATH=/data/options.json
export BRAINCALL_LOG_DIR=/config

exec python -u /opt/braincall/app/main.py
