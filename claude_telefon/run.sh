#!/bin/sh
# Add-on options (form in Home Assistant) are the configuration.
# Logs and call notes go to /config, which is /app_configs/<slug> in Home Assistant.
set -e
export CONFIG_PATH=/data/options.json
export CLAUDE_TELEFON_LOG_DIR=/config

# Up to version 1.0.1 they were in /share/claude-telefon: move them over once
OLD=/share/claude-telefon
if [ -d "$OLD" ]; then
    for f in "$OLD"/call-notes.txt "$OLD"/claude-telefon.log* "$OLD"/last-trigger.txt; do
        [ -e "$f" ] || continue
        name=$(basename "$f")
        if [ ! -e "/config/$name" ]; then
            mv "$f" "/config/$name"
        elif [ "$name" = call-notes.txt ]; then
            cat "$f" >> "/config/$name" && rm "$f"   # keep notes from both places
        else
            # e.g. after going back to 1.0.1 and updating again: keep both histories
            mv "$f" "/config/$name.from-share-$(date +%Y%m%d-%H%M%S)"
        fi
    done
    rmdir "$OLD" 2>/dev/null || true
    echo "Moved logs and call notes from $OLD to /config"
fi

exec python -u /opt/claude-telefon/app/main.py
