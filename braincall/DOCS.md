# BrainCall

**Brain Call – when your brain calls from home.**

BrainCall answers and makes phone calls with Claude through your FRITZ!Box and can read and control
Home Assistant during the call. The add-on is the same program as the Windows version, configured through this page.

## Before the first start

1. **FRITZ!Box:** Telephony → Telephony Devices → new IP phone. Note user name and password, assign an
   outgoing number and the incoming numbers it should answer.
2. **Configuration tab:** fill in at least
   - FRITZ!Box address, user name and password of the IP phone
   - Anthropic and OpenAI API key
   - Owner (your name) and the **Callers** (see below)

   Home Assistant access needs no token, the add-on gets it automatically.
3. **Info tab:** enable *Start on boot* and *Watchdog*, then *Start*. The log must show `SIP status: REGISTERED`.

## Callers

One entry per number: number (or `*` for everyone else), name, access, optional instructions, language
and notify. Without a `*` entry unknown numbers are rejected; withheld numbers always.

| access | may |
|---|---|
| `admin` | everything: read and control Home Assistant, appointments |
| `read` | read everything (also appointments with details), change nothing |
| `calendar` | only free/busy times, no details; books an appointment in a free slot without asking you |
| `shopping` | only the lists in *Shopping lists* |
| `guest` | nothing, takes a message |

Several at once separated by commas, e.g. `shopping, calendar`. The access decides which tools the assistant
gets in the call; *instructions* (e.g. `If Jan is free, book her wish date titled "Maryna comes".`) can only
narrow it.

After an incoming call a short summary (translated into the configured language) goes to every service in
*Notify after calls*, e.g. `notify.mobile_app_<phone>` or an e-mail notifier. Default: for everyone except
`admin`; *notify* per caller overrides it.

Updating from 1.0.x converts *Allowed callers*, *Trusted numbers* and *Language per number* once: trusted
numbers become `admin`, `*` becomes a `guest` entry, numbers only allowed to call are dropped. The old fields
are emptied afterwards (check the log).

Logs (`braincall.log`) and call notes of unknown callers (`call-notes.txt`) are in the add-on's own folder
`/app_configs/98055a04_braincall` (Samba, File editor). It is part of the add-on's backup.

## On/off switch

Stopping the add-on unregisters the IP phone from the FRITZ!Box: calls then only ring on your other phones
and no API costs arise. A switch for dashboards and automations:

1. Settings → Devices & services → *Supervisor* → device *BrainCall*: enable the entity **Running**
   (disabled by default), e.g. `binary_sensor.braincall_running`.
2. The add-on slug is in the browser address of the add-on page (Settings → Apps → BrainCall):
   `/config/app/98055a04_braincall/info` → `98055a04_braincall`. The prefix is derived from
   the repository URL; it is `98055a04` when the repository was added as `https://github.com/janlenck/braincall`.
3. Add to `configuration.yaml` (or create the same as a *Template → Switch* helper in the UI) and restart:

```yaml
template:
  - switch:
      - name: BrainCall
        unique_id: braincall_switch
        state: "{{ is_state('binary_sensor.braincall_running', 'on') }}"
        turn_on:
          - action: hassio.app_start
            data:
              app: 98055a04_braincall
        turn_off:
          - action: hassio.app_stop
            data:
              app: 98055a04_braincall
```

Switching on takes a few seconds until the agent is registered at the FRITZ!Box again.

## Calls from Home Assistant

The call trigger listens on `http://127.0.0.1:8099/call` on the Home Assistant host (no token needed there):

```yaml
rest_command:
  braincall:
    url: "http://127.0.0.1:8099/call"
    method: post
    content_type: "application/json"
    payload: '{"message": {{ message | tojson }}{% if number is defined %}, "number": {{ number | tojson }}{% endif %}}'
```

Then e.g. `action: rest_command.braincall` with `data: {message: "The washing machine is done."}`.
After each call the event `braincall_call` is fired (`number`, `answered`, `reason`, `duration_s`,
`transcript`, `caller_name`, `access`, `summary`).

## Updates

A new add-on version appears as an update in Home Assistant. Installing it rebuilds the image from the
matching release of the GitHub repository.
