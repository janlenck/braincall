# BrainCall

<img src="braincall/logo.png" alt="BrainCall" width="300">

**Brain Call – when your brain calls from home.**

A phone voice agent built on Claude: it calls you through a FRITZ!Box and talks with you, and you can call it.
It can read and control Home Assistant (states, lights, lists, calendars) during the call.

Runs on Windows (Python 3.12) or Linux. Audio is G.711 over SIP using pyVoIP (with fixes for the FRITZ!Box).

```
Trigger (HA automation, script, test-call.bat)
   │ HTTP POST /call {"message": "...", "number": "..."}
   ▼
app/main.py ── SIP/RTP ──► FRITZ!Box ──► phone
   │
   ├─ voice activity detection ─► OpenAI STT (gpt-4o-transcribe)
   ├─ Claude (streaming, tools: Home Assistant, hang up)
   └─ OpenAI TTS (gpt-4o-mini-tts) ─► phone
```

## Files

| Path | Purpose |
|---|---|
| `app/main.py` | entry point: SIP phone, HTTP trigger, incoming/outgoing calls |
| `app/session.py` | one call: listening, barge-in, turn taking |
| `app/brain.py` | Claude prompt, tools, sentence streaming |
| `app/ha.py` | Home Assistant: search, state, control, lists, calendars |
| `app/speech.py` | OpenAI speech-to-text and text-to-speech |
| `app/fritz_sip.py`, `app/rtpfix.py` | fixes for pyVoIP (INVITE/ACK, audio quality) |
| `app/config.py` | configuration loading (JSONC, pointer file, environment variables) |
| `start.bat` / `install.bat` / `test-call.bat` | start, (re)install packages, trigger a test call |
| `firewall.ps1` | Windows firewall rules (run as administrator) |
| `braincall/` | Home Assistant add-on: options form, Dockerfile, documentation |
| `repository.yaml` | makes this repository an add-on repository for Home Assistant |

## Home Assistant add-on

On Home Assistant OS the agent runs as an add-on, with an on/off switch in Home Assistant:

[![Add app repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fjanlenck%2Fbraincall)

1. Click the button above, or: Settings → Apps → App store → ⋮ → Repositories → add
   `https://github.com/janlenck/braincall` (Home Assistant calls add-ons "apps")
2. Install *BrainCall*, fill in the Configuration tab, start it.

Details, the on/off switch and calls from Home Assistant: [braincall/DOCS.md](braincall/DOCS.md).
The add-on lives in `braincall/` and builds `app/` from the release tag of its version; the Windows
setup below stays as it is.

## Setup (Windows)

1. **FRITZ!Box:** Telephony → Telephony Devices → new IP phone. Note user name and password,
   assign an outgoing number (ideally its own MSN) and the incoming numbers it should answer.
   Leave "allow login from the internet" off.
2. **Configuration:** copy `config.example.json` to `config.json` and fill it in, or keep the real
   config somewhere backed up and let `config.json` only point to it:
   ```json
   { "path": "C:\\Users\\Name\\OneDrive\\braincall\\config.json" }
   ```
   Backslashes doubled as in normal JSON; single backslashes are accepted too. Environment variables
   work as well, e.g. `"%OneDrive%\\braincall\\config.json"`. Details: top of `config.example.json`.
3. **Firewall** (PowerShell as administrator): `powershell -ExecutionPolicy Bypass -File firewall.ps1`
4. **Start:** `start.bat`. The first start creates the virtual environment in
   `%LOCALAPPDATA%\braincall\venv`; missing packages are installed automatically
   (or run `install.bat`). The log must show `SIP status: REGISTERED`.
5. **Test:** press `A` in the agent window, or run `test-call.bat [number]`.

Keys in the agent window: `X` quit, `A` test call to `default_number`, `L` open the log file.

## Callers and permissions

`callers` is the one list of who may call and what they may do, incoming and outgoing:

```json
"callers": [
  { "number": "0151xxxxxxx", "name": "Jan Mobile", "access": "admin" },
  { "number": "0170xxxxxxx", "name": "Maryna", "access": "calendar", "language": "uk",
    "instructions": "If Jan is free, book her wish date titled \"Maryna comes\"." },
  { "number": "*", "name": "Unknown", "access": "guest" }
]
```

- `access` (several possible, `"shopping, calendar"` or a list) decides which tools Claude gets in the call.
  The code enforces it, not the prompt: a caller cannot talk the assistant into more.
  - `admin`: everything, read and control Home Assistant, appointments.
  - `read`: read everything (states, sensors, appointments with details), change nothing.
  - `calendar`: only free/busy times of the default calendars, no titles or places. Books an appointment in a
    free slot of the default calendar after repeating day and time to the caller, without asking you.
    A busy slot is refused by the code (all-day entries count as busy).
  - `shopping`: only the to-do lists in `shopping_lists` (read, add, check off, remove).
  - `guest`: no access; asks for name and reason. The note goes to `logs/call-notes.txt`.
- `instructions` are appended to the prompt for this caller; they can narrow, not widen, the access.
- `language` fixes the language of this caller.
- Numbers are compared completely; `+49…`, `0049…` and `0…` count as the same (`country_code`).
  A listed number wins over `"*"`. Without `"*"` unknown numbers are rejected.
- Withheld numbers are never answered: the call is rejected right away, logged as unknown and reported
  to Home Assistant (`braincall_call` with `answered: false`).
- After an incoming call Claude writes a short summary in `language` (translated if the caller spoke another
  language) and sends it to every service in `notify_services` (e.g. `notify.mobile_app_phone`, an SMTP
  notifier). Default: for every caller except `admin`; `"notify": true/false` per caller overrides it.
- Older settings `inbound_allowed`, `trusted_numbers` and `caller_languages` are converted on every start and
  used instead of `callers` as long as they are set: trusted numbers become `admin`, `"*"` becomes a `guest`
  entry, other numbers only allowed to call are dropped. The log shows the converted list to copy over.
- `default_number` is only the target of the test call and grants no rights.
- `control_domains` limits what may be controlled; `confirm_domains` (e.g. `cover`) require an explicit "yes".
- `owner_name` (e.g. `"Jan Lenck"`) is whose assistant this is; the first name is used in conversations.

## Home Assistant

Read access needs `ha_url` and a long-lived `ha_token`. Certificates from the Windows certificate store
(own CA) are trusted automatically.

Triggering a call from HA (set `api_token` so the trigger listens on the LAN):

```yaml
rest_command:
  braincall:
    url: "http://<agent-host>:8099/call"
    method: post
    headers:
      Authorization: !secret braincall_auth   # "Bearer <api_token>"
    content_type: "application/json"
    payload: '{"message": {{ message | tojson }}}'
```

After each call the agent fires the HA event `braincall_call`
(`number`, `answered`, `reason`, `duration_s`, `transcript`, `caller_name`, `access`, `summary`).

## Languages

`language`: `de`, `uk`, `en` or `auto` (answers in the language the caller spoke last).
`language` in a `callers` entry sets the language per caller. Fixed phrases exist in German, Ukrainian and English.

## Limits

- Response time per turn is about 1.5–3 s (STT after end of speech, then Claude, then TTS).
- An unanswered outbound call is cancelled after `ring_timeout` seconds.
- The machine must be on, awake and in the home network.
- If the FRITZ!Box is restarted or unreachable, BrainCall sets up the SIP connection again after
  5 minutes without registration and keeps trying every 5 minutes. Calls in between fail.
- Costs are billed to your Anthropic and OpenAI API accounts.

## License

MIT, see [LICENSE](LICENSE).
