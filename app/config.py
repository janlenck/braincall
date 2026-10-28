"""Configuration from config.json (next to the app folder, or referenced from there).
API keys may also come from environment variables."""
import json
import re
import os
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULTS = {
    # SIP / FRITZ!Box
    "sip_server": "192.168.178.1",
    "sip_port": 5060,
    "sip_user": "",
    "sip_password": "",
    "local_ip": "",            # empty = detect automatically
    "sip_local_port": 5062,
    "rtp_port_min": 30000,
    "rtp_port_max": 30100,
    # who to call / who may call
    "default_number": "",      # target of the test call (key A, test-call.bat), grants no rights
    "inbound_allowed": [],     # empty = only trusted_numbers, ["*"] = accept all (withheld numbers are always rejected)
    "trusted_numbers": {},     # {"number": "name"} – only these get access to Home Assistant
    "country_code": "49",      # +49 / 0049 count as the same number as a leading 0
    "ring_timeout": 90,        # seconds to ring before an unanswered outbound call is cancelled
    # Claude
    "anthropic_api_key": "",
    "claude_model": "claude-haiku-5-5",
    "assistant_name": "Claudine",
    "owner_name": "",          # whose assistant this is, e.g. "Jan Lenck" (first name is used to address)
    "language": "de",          # "de", "uk", "en" or "auto" (answers in the caller's language)
    "caller_languages": {},    # language per number, e.g. {"0151xxxxxxx": "uk"}
    "extra_prompt": "",
    "timezone": "Europe/Berlin",
    # OpenAI STT/TTS
    "openai_api_key": "",
    "stt_model": "gpt-4o-transcribe",
    "tts_model": "gpt-4o-mini-tts",
    "tts_voice": "onyx",
    "tts_instructions": "Speak natural, brisk German like on the phone.",
    # voice activity detection (VAD)
    "vad_min_energy": 500,
    "end_silence_ms": 700,
    "silence_timeout": 12,
    "barge_in": True,
    "barge_in_min_ms": 1000,   # how long the caller must speak before the assistant lets itself be interrupted
    # logs and call notes
    "log_dir": "",             # empty = logs\ in the project folder
    # HTTP trigger
    "http_port": 8099,
    "api_token": "",           # empty = only reachable from 127.0.0.1
    # Home Assistant
    "ha_url": "",
    "ha_token": "",
    "ha_verify_ssl": True,     # true/false or path to the CA certificate (.crt/.pem)
    # control (only for trusted_numbers / outbound calls)
    "control_domains": ["light", "switch", "fan", "input_boolean", "scene", "script", "climate",
                        "media_player", "cover", "button", "number", "select", "todo"],
    "default_calendars": [],   # entity_ids or name parts; empty = all
    "calendar_write": True,    # allow creating appointments (trusted_numbers only, with confirmation)
    "confirm_domains": ["cover", "lock", "alarm_control_panel"],  # ask explicitly before acting
}

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = [""]  # file actually loaded (for the log)


def _strip_jsonc(text: str) -> str:
    """Allows // and /* */ comments and a trailing comma before ] or } (JSONC); strings stay untouched."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[\]}])", r"\1", "".join(out))


def _valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return bool(name)
    except Exception:  # noqa: BLE001  (also POSIX strings like "CET-1CEST" and empty names)
        return False


class Config(dict):
    __getattr__ = dict.get


def _read(path: Path) -> dict:
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    # Pointer file {"path": "C:\\dir\\config.json"} (valid JSON) or {"path": "C:\dir\config.json"} (taken literally)
    m = re.fullmatch(r'\s*\{\s*"path"\s*:\s*"([^"]+)"\s*,?\s*\}\s*', _strip_jsonc(text))
    if m:
        raw = m.group(1)
        # Only decode as JSON if every backslash is doubled; otherwise "C:\temp\new" would turn into tab/newline.
        # A result starting with a single backslash (rooted on the current drive) was a literal UNC path
        # like \\nas/share/config.json, so it stays as written.
        path = raw.replace("\\\\", "\\") if re.fullmatch(r'(?:[^\\]|\\\\)*', raw) else raw
        if path.startswith("\\") and not path.startswith("\\\\"):
            path = raw
        return {"path": path}
    try:
        return json.loads(_strip_jsonc(text))
    except json.JSONDecodeError as e:
        line = text.splitlines()[max(0, e.lineno - 2)] if e.lineno > 1 else ""
        raise SystemExit(f"\n\033[91m{path} is not valid JSON: {e.msg} in line {e.lineno}.\033[0m\n"
                         f"Usually a comma is missing at the end of the line before:\n"
                         f"  line {e.lineno - 1}: {line.strip()[:60]}\n") from None


def load() -> Config:
    # Order: environment variable CLAUDE_TELEFON_CONFIG > CONFIG_PATH > config.json in the project folder.
    # If that file only contains {"path": "..."}, the referenced file is loaded instead (e.g. in OneDrive,
    # so keys and settings are backed up and never end up in the git repo).
    path = Path(os.environ.get("CLAUDE_TELEFON_CONFIG") or os.environ.get("CONFIG_PATH") or ROOT / "config.json")
    data = {}
    for _ in range(3):
        if not path.exists():
            if data.get("path"):
                raise SystemExit(f"\n\033[91mConfiguration not found: {path}\033[0m\n"
                                 f"(referenced from config.json in the project folder)\n")
            break
        data = _read(path)
        if set(data) == {"path"}:
            path = Path(os.path.expandvars(data["path"])).expanduser()
            continue
        break
    data.pop("path", None)
    CONFIG_FILE[0] = str(path)
    cfg = Config(DEFAULTS)
    cfg.update({k: v for k, v in data.items() if v is not None and not k.startswith("_")})
    # Home Assistant add-on options have no free-form objects: [{"number": …, "name"/"language": …}] -> {number: …}
    for key, field in (("trusted_numbers", "name"), ("caller_languages", "language")):
        items = cfg.get(key)
        if isinstance(items, list) and items and all(isinstance(x, dict) for x in items):
            cfg[key] = {str(x["number"]): x.get(field, "") for x in items if x.get("number")}
    for key, env in (("anthropic_api_key", "ANTHROPIC_API_KEY"),
                     ("openai_api_key", "OPENAI_API_KEY"),
                     ("ha_token", "HA_TOKEN"),
                     ("sip_password", "CLAUDE_TELEFON_SIP_PASSWORD"),
                     ("api_token", "CLAUDE_TELEFON_API_TOKEN")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    cfg["supervisor_token"] = os.environ.get("SUPERVISOR_TOKEN", "")
    # Home Assistant add-on: the Supervisor passes Home Assistant's timezone as TZ
    if not data.get("timezone") and _valid_timezone(os.environ.get("TZ", "")):
        cfg["timezone"] = os.environ["TZ"]
    cfg["log_dir"] = str(Path(os.path.expandvars(cfg.log_dir)).expanduser()) if cfg.log_dir else str(ROOT / "logs")
    return cfg
