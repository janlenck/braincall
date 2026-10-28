"""Konfiguration aus config.json (neben dem app-Ordner); API-Keys alternativ aus Umgebungsvariablen."""
import json
import os
from pathlib import Path

DEFAULTS = {
    # SIP / FRITZ!Box
    "sip_server": "192.168.178.1",
    "sip_port": 5060,
    "sip_user": "",
    "sip_password": "",
    "local_ip": "",            # leer = automatisch ermitteln
    "sip_local_port": 5062,
    "rtp_port_min": 30000,
    "rtp_port_max": 30100,
    # Wen anrufen / wer darf anrufen
    "default_number": "",
    "inbound_allowed": [],     # leer = alle ablehnen, ["*"] = alle annehmen
    "trusted_numbers": {},     # {"Nummer": "Name"} – nur diese bekommen Zugriff auf Home Assistant
    "ring_timeout": 90,        # max. Sekunden warten auf Abheben (pyVoIP kann nicht aktiv abbrechen)
    # Claude
    "anthropic_api_key": "",
    "claude_model": "claude-haiku-5-5",
    "assistant_name": "Claudine",
    "language": "de",          # "de", "uk", "en" oder "auto" (antwortet in der Sprache des Anrufers)
    "caller_languages": {},    # Sprache je Nummer, z. B. {"0151xxxxxxx": "uk"}
    "extra_prompt": "",
    "timezone": "Europe/Berlin",
    # OpenAI STT/TTS
    "openai_api_key": "",
    "stt_model": "gpt-4o-transcribe",
    "tts_model": "gpt-4o-mini-tts",
    "tts_voice": "onyx",
    "tts_instructions": "Sprich natürliches, zügiges Deutsch wie am Telefon.",
    # Sprach-Erkennung (VAD)
    "vad_min_energy": 500,
    "end_silence_ms": 700,
    "silence_timeout": 12,
    "barge_in": True,
    "barge_in_min_ms": 1000,   # so lange muss jemand sprechen, bis Claudine sich unterbrechen lässt
    # HTTP-Trigger
    "http_port": 8099,
    "api_token": "",           # leer = nur 127.0.0.1
    # Home Assistant (lesend)
    "ha_url": "",
    "ha_token": "",
    "ha_verify_ssl": True,     # true/false oder Pfad zum CA-Zertifikat (.crt/.pem)
    # Schalten (nur für trusted_numbers / ausgehende Anrufe)
    "control_domains": ["light", "switch", "fan", "input_boolean", "scene", "script", "climate",
                        "media_player", "cover", "button", "number", "select", "todo"],
    "default_calendars": ["calendar.molln", "feiertag"],  # entity_ids oder Namensteile; leer = alle
    "calendar_write": True,    # Termine anlegen erlauben (nur trusted_numbers, mit Rückfrage)
    "confirm_domains": ["cover", "lock", "alarm_control_panel"],  # vorher ausdrücklich nachfragen
}

ROOT = Path(__file__).resolve().parent.parent


def _strip_jsonc(text: str) -> str:
    """Erlaubt // und /* */ Kommentare sowie Komma vor ] oder } (JSONC), Strings bleiben unangetastet."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True; out.append(c); i += 1
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c); i += 1
    import re
    return re.sub(r",(\s*[\]}])", r"\1", "".join(out))


class Config(dict):
    __getattr__ = dict.get


def load() -> Config:
    path = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
    data = {}
    if path.exists():
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
        try:
            data = json.loads(_strip_jsonc(text))
        except json.JSONDecodeError as e:
            line = text.splitlines()[max(0, e.lineno - 2)] if e.lineno > 1 else ""
            raise SystemExit(f"\n\033[91mconfig.json ist kein gültiges JSON: {e.msg} in Zeile {e.lineno}.\033[0m\n"
                             f"Meist fehlt am Ende der Zeile davor ein Komma:\n  Zeile {e.lineno - 1}: {line.strip()[:60]}\n")
    cfg = Config(DEFAULTS)
    cfg.update({k: v for k, v in data.items() if v is not None and not k.startswith("_")})
    for key, env in (("anthropic_api_key", "ANTHROPIC_API_KEY"),
                     ("openai_api_key", "OPENAI_API_KEY"),
                     ("ha_token", "HA_TOKEN"),
                     ("sip_password", "CLAUDE_TELEFON_SIP_PASSWORD"),
                     ("api_token", "CLAUDE_TELEFON_API_TOKEN")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    cfg["supervisor_token"] = os.environ.get("SUPERVISOR_TOKEN", "")
    return cfg
