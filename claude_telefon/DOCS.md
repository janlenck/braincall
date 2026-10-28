# Claude Telefon

Claude answers and makes phone calls through your FRITZ!Box and can read and control Home Assistant during
the call. The add-on is the same program as the Windows version, configured through this page.

## Before the first start

1. **FRITZ!Box:** Telephony → Telephony Devices → new IP phone. Note user name and password, assign an
   outgoing number and the incoming numbers it should answer.
2. **Configuration tab:** fill in at least
   - FRITZ!Box address, user name and password of the IP phone
   - Anthropic and OpenAI API key
   - Owner (your name) and the trusted numbers (only they may query and control Home Assistant)

   Home Assistant access needs no token, the add-on gets it automatically.
3. **Info tab:** enable *Start on boot* and *Watchdog*, then *Start*. The log must show `SIP status: REGISTERED`.

Logs and call notes of unknown callers are in `/share/claude-telefon` (Samba share `share`, File editor).

## On/off switch

Stopping the add-on unregisters the IP phone from the FRITZ!Box: calls then only ring on your other phones
and no API costs arise. A switch for dashboards and automations:

1. Settings → Devices & services → *Supervisor* → device *Claude Telefon*: enable the entity **Running**
   (disabled by default), e.g. `binary_sensor.claude_telefon_running`.
2. Look up the add-on slug: it is in the browser address on the add-on page,
   e.g. `/hassio/addon/a1b2c3d4_claude_telefon` → `a1b2c3d4_claude_telefon`.
3. Add to `configuration.yaml` (or create the same as a *Template → Switch* helper in the UI) and restart:

```yaml
template:
  - switch:
      - name: Claude Telefon
        unique_id: claude_telefon_switch
        state: "{{ is_state('binary_sensor.claude_telefon_running', 'on') }}"
        turn_on:
          - action: hassio.addon_start
            data:
              addon: a1b2c3d4_claude_telefon
        turn_off:
          - action: hassio.addon_stop
            data:
              addon: a1b2c3d4_claude_telefon
```

Switching on takes a few seconds until the agent is registered at the FRITZ!Box again.

## Calls from Home Assistant

The call trigger listens on `http://127.0.0.1:8099/call` on the Home Assistant host (no token needed there):

```yaml
rest_command:
  claude_call:
    url: "http://127.0.0.1:8099/call"
    method: post
    content_type: "application/json"
    payload: '{"message": {{ message | tojson }}{% if number is defined %}, "number": {{ number | tojson }}{% endif %}}'
```

Then e.g. `action: rest_command.claude_call` with `data: {message: "The washing machine is done."}`.
After each call the event `claude_telefon_call` is fired (`number`, `answered`, `reason`, `duration_s`,
`transcript`).

## Updates

A new add-on version appears as an update in Home Assistant. Installing it rebuilds the image from the
matching release of the GitHub repository.
