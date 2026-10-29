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
    "callers": [],             # [{"number", "name", "access", …}]; "*" = everyone else, not listed = rejected
    "country_code": "49",      # +49 / 0049 count as the same number as a leading 0
    "ring_timeout": 90,        # seconds to ring before an unanswered outbound call is cancelled
    # Claude
    "anthropic_api_key": "",
    "claude_model": "claude-haiku-5-5",
    "assistant_name": "Claudine",
    "owner_name": "",          # whose assistant this is, e.g. "Jan Lenck" (first name is used to address)
    "language": "de",          # "de", "uk", "en" or "auto" (answers in the caller's language)
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
    "notify_services": [],     # e.g. ["notify.mobile_app_phone"]: summary after a call goes there
    "shopping_lists": ["todo.shopping_list"],  # the only lists callers with access "shopping" may use
    # control (only for callers with access "admin")
    "control_domains": ["light", "switch", "fan", "input_boolean", "scene", "script", "climate",
                        "media_player", "cover", "button", "number", "select", "todo"],
    "default_calendars": [],   # entity_ids or name parts; empty = all
    "calendar_write": True,    # allow creating appointments (access admin/calendar, with confirmation)
    "confirm_domains": ["cover", "lock", "alarm_control_panel"],  # ask explicitly before acting
}

ACCESS = ("admin", "read", "calendar", "shopping", "guest")
LEGACY_KEYS = ("inbound_allowed", "trusted_numbers", "caller_languages")  # until 1.0.x, replaced by callers

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = [""]  # file actually loaded (for the log)
WARNINGS: list[str] = []  # logged by main once logging is set up


def norm_number(n, country_code="") -> str:
    """Complete number in a comparable form: digits plus * and # (internal numbers like **611),
    international prefix of the own country folded to a leading 0 (+49151… == 0049151… == 0151…)."""
    s = re.sub(r"[^\d*#+]", "", str(n or ""))
    s = ("00" + s[1:] if s.startswith("+") else s).replace("+", "")
    cc = "00" + re.sub(r"\D", "", str(country_code or ""))
    if len(cc) > 2 and s.startswith(cc):
        s = "0" + s[len(cc):]
    return s


def parse_access(value) -> frozenset:
    """'shopping, calendar' or ["shopping", "calendar"] -> {"shopping", "calendar"}.
    admin includes everything; nothing valid = guest (least privilege)."""
    words = value if isinstance(value, (list, tuple, set, frozenset)) else re.split(r"[,;\s]+", str(value or ""))
    words = {str(w).strip().lower() for w in words} - {""}
    unknown = words - set(ACCESS)
    if unknown:
        WARNINGS.append(f"Unknown access {', '.join(sorted(unknown))} ignored (allowed: {', '.join(ACCESS)})")
    found = words & set(ACCESS)
    if "admin" in found:
        return frozenset({"admin"})
    found.discard("guest")
    return frozenset(found) or frozenset({"guest"})


def _callers(items) -> list:
    out = []
    for x in items if isinstance(items, list) else []:
        number = str(x.get("number") or "").strip() if isinstance(x, dict) else ""
        if not number:
            WARNINGS.append(f"Caller without number ignored: {x}")
            continue
        out.append({"number": number,
                    "name": str(x.get("name") or "").strip(),
                    "access": parse_access(x.get("access")),
                    "instructions": str(x.get("instructions") or "").strip(),
                    "language": str(x.get("language") or "").strip(),
                    "notify": x.get("notify") if isinstance(x.get("notify"), bool) else None})
    return out


def _pairs(value, field: str, default="") -> dict:
    """Old format {number: value}, add-on list [{"number": …, field: …}] or plain list of numbers -> dict."""
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    out = {}
    for x in value if isinstance(value, list) else []:
        if isinstance(x, dict) and x.get("number"):
            out[str(x["number"])] = x.get(field, "")
        elif not isinstance(x, dict) and str(x).strip():
            out[str(x).strip()] = default
    return out


def migrate(data: dict) -> list | None:
    """callers from the old keys inbound_allowed / trusted_numbers / caller_languages; None if none is set.
    Trusted numbers become admin, "*" in inbound_allowed becomes a guest entry, everything else is dropped."""
    allowed = [str(n).strip() for n in data.get("inbound_allowed") or []]
    owner = str(data.get("owner_name") or "").strip()
    trusted = _pairs(data.get("trusted_numbers"), "name", owner)
    langs = _pairs(data.get("caller_languages"), "language")
    if not (allowed or trusted or langs):
        return None
    cc = data.get("country_code", DEFAULTS["country_code"])
    callers = [{"number": n, "name": str(name or owner or n), "access": "admin"} for n, name in trusted.items()]
    if "*" in allowed:
        callers.append({"number": "*", "name": "Unknown", "access": "guest"})
    dropped = [n for n in allowed if n not in ("*", "")
               and not any(norm_number(n, cc) == norm_number(c["number"], cc) for c in callers)]
    if dropped:
        WARNINGS.append(f"Migration: numbers only in inbound_allowed dropped: {', '.join(dropped)}")
    for n, lang in langs.items():
        match = [c for c in callers if c["number"] != "*" and norm_number(n, cc) == norm_number(c["number"], cc)]
        if match and lang:
            match[0]["language"] = str(lang)
        else:
            WARNINGS.append(f"Migration: language for {n} dropped (number is not a caller)")
    return callers


def _save_addon_options(data: dict, callers: list, token: str):
    """Writes the migrated callers into the add-on options once (Supervisor API, needs hassio_api).
    The old keys stay as empty lists: they are still in the schema for this version."""
    import requests
    options = {k: v for k, v in data.items() if k != "path"}
    options.update({k: [] for k in LEGACY_KEYS}, callers=callers)
    try:
        r = requests.post("http://supervisor/addons/self/options", json={"options": options},
                          headers={"Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
        WARNINGS.append("Migration: callers saved in the add-on options, old keys emptied")
    except Exception as e:  # noqa: BLE001
        WARNINGS.append(f"Migration: saving the add-on options failed ({e}); converted on every start until "
                        "Allowed callers / Trusted numbers / Language per number are emptied")


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
    # Order: environment variable BRAINCALL_CONFIG > CONFIG_PATH > config.json in the project folder.
    # If that file only contains {"path": "..."}, the referenced file is loaded instead (e.g. in OneDrive,
    # so keys and settings are backed up and never end up in the git repo).
    path = Path(os.environ.get("BRAINCALL_CONFIG") or os.environ.get("CONFIG_PATH") or ROOT / "config.json")
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
    legacy = migrate(data)
    if legacy is not None:
        cfg["callers"] = legacy
        WARNINGS.append("Old settings inbound_allowed / trusted_numbers / caller_languages converted to callers:\n"
                        + json.dumps({"callers": legacy}, ensure_ascii=False, indent=2))
    for key in LEGACY_KEYS:
        cfg.pop(key, None)
    for key, env in (("anthropic_api_key", "ANTHROPIC_API_KEY"),
                     ("openai_api_key", "OPENAI_API_KEY"),
                     ("ha_token", "HA_TOKEN"),
                     ("sip_password", "BRAINCALL_SIP_PASSWORD"),
                     ("api_token", "BRAINCALL_API_TOKEN"),
                     ("log_dir", "BRAINCALL_LOG_DIR")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    cfg["supervisor_token"] = os.environ.get("SUPERVISOR_TOKEN", "")
    if legacy is not None and cfg.supervisor_token:
        _save_addon_options(data, legacy, cfg.supervisor_token)
    cfg["callers"] = _callers(cfg.callers)
    # Home Assistant add-on: the Supervisor passes Home Assistant's timezone as TZ
    if not data.get("timezone") and _valid_timezone(os.environ.get("TZ", "")):
        cfg["timezone"] = os.environ["TZ"]
    cfg["log_dir"] = str(Path(os.path.expandvars(cfg.log_dir)).expanduser()) if cfg.log_dir else str(ROOT / "logs")
    return cfg
