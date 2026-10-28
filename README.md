# Claude Telefon

A phone voice agent: Claude calls you through a FRITZ!Box and talks with you, and you can call it.
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
| `claude_telefon/` | Home Assistant add-on: options form, Dockerfile, documentation |
| `repository.yaml` | makes this repository an add-on repository for Home Assistant |

## Home Assistant add-on

On Home Assistant OS the agent runs as an add-on, with an on/off switch in Home Assistant:

[![Add app repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fjanlenck%2Fclaude-telefon)

1. Click the button above, or: Settings → Apps → App store → ⋮ → Repositories → add
   `https://github.com/janlenck/claude-telefon` (Home Assistant calls add-ons "apps")
2. Install *Claude Telefon*, fill in the Configuration tab, start it.

Details, the on/off switch and calls from Home Assistant: [claude_telefon/DOCS.md](claude_telefon/DOCS.md).
The add-on lives in `claude_telefon/` and builds `app/` from the release tag of its version; the Windows
setup below stays as it is.

## Setup (Windows)

1. **FRITZ!Box:** Telephony → Telephony Devices → new IP phone. Note user name and password,
   assign an outgoing number (ideally its own MSN) and the incoming numbers it should answer.
   Leave "allow login from the internet" off.
2. **Configuration:** copy `config.example.json` to `config.json` and fill it in, or keep the real
   config somewhere backed up and let `config.json` only point to it:
   ```json
   { "path": "C:\\Users\\Name\\OneDrive\\claude-telefon\\config.json" }
   ```
   Backslashes doubled as in normal JSON; single backslashes are accepted too. Environment variables
   work as well, e.g. `"%OneDrive%\\claude-telefon\\config.json"`. Details: top of `config.example.json`.
3. **Firewall** (PowerShell as administrator): `powershell -ExecutionPolicy Bypass -File firewall.ps1`
4. **Start:** `start.bat`. The first start creates the virtual environment in
   `%LOCALAPPDATA%\claude-telefon\venv`; missing packages are installed automatically
   (or run `install.bat`). The log must show `SIP status: REGISTERED`.
5. **Test:** press `A` in the agent window, or run `test-call.bat [number]`.

Keys in the agent window: `X` quit, `A` test call to `default_number`, `L` open the log file.

## Callers and permissions

- `trusted_numbers` (`{"number": "name"}`): full access, may query and control Home Assistant,
  incoming and outgoing. The name is used in the log; the first word is used to address the person.
  Numbers are compared completely; `+49…`, `0049…` and `0…` count as the same (`country_code`).
- Everyone else (incoming if `inbound_allowed` contains `"*"` or the number, or when the agent calls a
  number that is not trusted): small talk only. The assistant gives no information about the house and
  controls nothing. Incoming, it asks for name and reason; the conversation is saved to
  `logs/call-notes.txt` and sent to Home Assistant as an event.
- Withheld numbers are never answered: the call is rejected right away, logged as unknown and reported
  to Home Assistant (`claude_telefon_call` with `answered: false`).
- `default_number` is only the target of the test call and grants no rights.
- `control_domains` limits what may be controlled; `confirm_domains` (e.g. `cover`) require an explicit "yes".
- `owner_name` (e.g. `"Jan Lenck"`) is whose assistant this is; the first name is used in conversations.

## Home Assistant

Read access needs `ha_url` and a long-lived `ha_token`. Certificates from the Windows certificate store
(own CA) are trusted automatically.

Triggering a call from HA (set `api_token` so the trigger listens on the LAN):

```yaml
rest_command:
  claude_call:
    url: "http://<agent-host>:8099/call"
    method: post
    headers:
      Authorization: !secret claude_telefon_auth   # "Bearer <api_token>"
    content_type: "application/json"
    payload: '{"message": {{ message | tojson }}}'
```

After each call the agent fires the HA event `claude_telefon_call`
(`number`, `answered`, `reason`, `duration_s`, `transcript`).

## Languages

`language`: `de`, `uk`, `en` or `auto` (answers in the language the caller spoke last).
`caller_languages` sets the language per number. Fixed phrases exist in German, Ukrainian and English.

## Limits

- Response time per turn is about 1.5–3 s (STT after end of speech, then Claude, then TTS).
- An unanswered outbound call is cancelled after `ring_timeout` seconds.
- The machine must be on, awake and in the home network.
- Costs are billed to your Anthropic and OpenAI API accounts.

## License

MIT, see [LICENSE](LICENSE).
