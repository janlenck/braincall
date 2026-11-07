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
and notify. Without a `*` entry unknown numbers are rejected; withheld numbers always. After a fresh
installation the list is empty: add `*` with access `guest` so unknown callers can leave a message.

| access | may |
|---|---|
| `admin` | everything: read and control Home Assistant, appointments |
| `read` | read everything (also history, energy statistics and appointments with details), change nothing |
| `calendar` | only free/busy times, no details; books an appointment in a free slot without asking you |
| `shopping` | only the lists in *Shopping lists* |
| `guest` | nothing, takes a message |

Several at once separated by commas, e.g. `shopping, calendar`. The access decides which tools the assistant
gets in the call. For `calendar` the code also refuses bookings in the past, longer than *Longest booking*
(default 4 hours; all-day at most one day), further ahead than *Booking lead time* (default 90 days) and more
than *Bookings per call* (default 2); 0 switches a limit off. All-day entries count as busy unless their title contains one of the
*Free all-day entries* words (e.g. `gelber`, `sack`, `abfall`): Home Assistant does not pass on Google's
"free"/"busy". *Instructions* (e.g. `If Jan is free, book her wish date titled "Maryna comes".`) can only
narrow it.

After an incoming call a short summary (translated into the configured language) goes to every service in
*Notify after calls*: notify entities (e.g. `notify.<phone>`) or notify services (e.g. `notify.mobile_app_<phone>`, an e-mail notifier). Default: for everyone except
`admin`; *notify* per caller overrides it.

*Caller memory* (default `none`): the summary of each call is kept per number in `calls.jsonl` and the last
three are given to the assistant at the start of the next call with that number, so callers do not have to
repeat themselves. A number of days (e.g. `30`) keeps only that recent calls, `forever` has no age limit.
A caller only gets summaries of calls with at most their current access. With `none` the file is neither read
nor written; delete it to forget earlier calls.

After updating from 1.0.x *Allowed callers*, *Trusted numbers* and *Language per number* keep working: on
every start they are converted (trusted numbers become `admin`, `*` becomes a `guest` entry, numbers only
allowed to call are dropped) and used instead of *Callers*. The log shows the converted list. Enter it under
*Callers* and empty the three old fields; from then on only *Callers* counts.

Logs (`braincall.log`), call notes of unknown callers (`call-notes.txt`) and the caller memory (`calls.jsonl`) are in the add-on's own folder
`/app_configs/98055a04_braincall` (Samba, File editor). It is part of the add-on's backup.

## Energy and statistics

Callers with access `admin` or `read` can ask for consumption over a period, e.g. "How much power did I use
yesterday?" or "What did the PV produce in October?". The answer comes from the energy dashboard
(*Settings > Dashboards > Energy*): grid import, feed-in, solar, battery, gas, water, home consumption,
self-sufficiency and the individual devices. Other sensors with a `state_class` work too, e.g. the energy of
the heat pump or the average outdoor temperature. Totals reach up to a few minutes ago.

They can also ask what happened recently, e.g. "When was the garage door last open?", "Was anyone at the front
door today?" or "How long was the light on?". The answer comes from the recorder, which keeps about 10 days
by default (`recorder: purge_keep_days`); at most 31 days per question.

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
