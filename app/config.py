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
    "end_silence_ms": 450,     # pause after which an utterance is finished and answered
    "speculative_ms": 300,     # STT and Claude start after this pause, spoken only after end_silence_ms; 0 = off
    "silence_timeout": 12,
    "barge_in": True,
    "barge_in_min_ms": 1000,   # how long the caller must speak before the assistant lets itself be interrupted
    # fillers and status phrases (cached audio in <log_dir>/cache, like the greetings)
    "fillers": True,           # "Hmm…", "Moment, ich schau kurz nach" … while the answer is being prepared
    "filler_delay_ms": 600,    # a filler only if the answer has no audio this long after the caller's speech
    "still_working_s": 5,      # "Ich bin noch dran" after this many seconds without anything said; 0 = off
    # logs and call notes
    "log_dir": "",             # empty = logs\ in the project folder
    "caller_memory": "none",   # summaries of earlier calls per number as context: "none", days (e.g. 30), "forever"
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
    "all_day_free_words": [],  # all-day entries whose title contains one of these do not count as busy
    "calendar_write": True,    # allow creating appointments (access admin/calendar, with confirmation)
    # limits for bookings by callers without admin (0 = no limit)
    "calendar_max_hours": 4,         # longest timed booking; all-day bookings are always at most one day
    "calendar_max_days_ahead": 90,   # how far ahead
    "calendar_max_bookings": 2,      # appointments per call
    "confirm_domains": ["cover", "lock", "alarm_control_panel"],  # ask explicitly before acting
    # planned calls with a task (plan_call for admins, POST /tasks): tasks.json in the log folder
    "tasks": True,
    "task_max_retries": 3,           # most further attempts a task may ask for if nobody is reached
    "task_retry_minutes": 15,        # pause between attempts
    "outbound_hours": "09:00-20:00",  # planned calls only in this time of day; "" = any time
    "outbound_max_calls_per_day": 20,  # planned calls (attempts) per day; 0 = no limit
    # never dialed, whoever asks (also POST /call): numbers starting with these
    "outbound_blocked": ["110", "112", "118", "0137", "019", "0900"],
    # only numbers that match it are dialed, written in national form (+49 4542 1234 -> 045421234); "" = no limit.
    # Default: German mobile (015x-017x) and landline numbers plus internal FRITZ!Box numbers (**6, **611):
    # no short codes (110, 112, 115, 116 117, 118 xx), no service numbers (0137, 0180, 019x, 0700, 0800, 0900),
    # no other countries
    "outbound_allowed_regex": r"\*\*\d{1,4}|0(?:1[5-7]\d{8,9}|(?![789]00)[2-9]\d{5,11})",
    # web search and web fetch (Anthropic server tools), for access admin and read unless "web" per caller says else
    "web_access": False,
    "web_max_searches": 3,           # per turn (one answer); 0 = no limit
    "web_max_fetches": 2,            # per turn; 0 = no limit
    "web_max_searches_per_day": 50,  # all calls together; 0 = no limit
    "web_max_fetches_per_day": 50,
    "web_max_content_tokens": 8000,  # longest page text taken in, in tokens; 0 = no limit
    "web_allowed_domains": [],       # only these sites (e.g. ["bahn.de"]); or:
    "web_blocked_domains": [],       # never these sites
    "web_city": "",                  # for local results, e.g. "Mölln" (the timezone is always passed)
    "web_country": "",               # two letters, e.g. "DE"
}

ACCESS = ("admin", "read", "calendar", "shopping", "guest")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = [""]  # file actually loaded (for the log)
WARNINGS: list[str] = []  # logged by main once logging is set up


def dial_form(n) -> str:
    """A number as it is stored and read back: digits, * and # (internal numbers like **611) and a leading +;
    spaces, dashes, slashes and brackets dropped ("04542 4635" -> "045424635"). "+49 (0)151 …" loses the (0)."""
    raw = str(n or "").strip()
    if re.match(r"(\+|00)", raw):
        raw = re.sub(r"\(\s*0\s*\)", "", raw)
    s = re.sub(r"[^\d*#+]", "", raw)
    return s[:1] + s[1:].replace("+", "")


def norm_number(n, country_code="") -> str:
    """Complete number in a comparable and dialable form: digits plus * and # (internal numbers like **611),
    international prefix of the own country folded to a leading 0 (+49151… == 0049151… == 0151…)."""
    s = dial_form(n)
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
                    "notify": x.get("notify") if isinstance(x.get("notify"), bool) else None,
                    "web": x.get("web") if isinstance(x.get("web"), bool) else None})
    return out


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
    for key, env in (("anthropic_api_key", "ANTHROPIC_API_KEY"),
                     ("openai_api_key", "OPENAI_API_KEY"),
                     ("ha_token", "HA_TOKEN"),
                     ("sip_password", "BRAINCALL_SIP_PASSWORD"),
                     ("api_token", "BRAINCALL_API_TOKEN"),
                     ("log_dir", "BRAINCALL_LOG_DIR")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    cfg["supervisor_token"] = os.environ.get("SUPERVISOR_TOKEN", "")
    cfg["callers"] = _callers(cfg.callers)
    if cfg.web_allowed_domains and cfg.web_blocked_domains:
        WARNINGS.append("web_allowed_domains and web_blocked_domains are both set: only web_allowed_domains is used")
    # Home Assistant add-on: the Supervisor passes Home Assistant's timezone as TZ
    if not data.get("timezone") and _valid_timezone(os.environ.get("TZ", "")):
        cfg["timezone"] = os.environ["TZ"]
    cfg["log_dir"] = str(Path(os.path.expandvars(cfg.log_dir)).expanduser()) if cfg.log_dir else str(ROOT / "logs")
    return cfg
