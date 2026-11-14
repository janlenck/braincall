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

One entry per number: number (or `*` for everyone else), name, access, optional instructions, language,
notify and web. Without a `*` entry unknown numbers are rejected; withheld numbers always. After a fresh
installation the list is empty: add `*` with access `guest` so unknown callers can leave a message.

| access | may |
|---|---|
| `admin` | everything: read and control Home Assistant, appointments |
| `read` | read everything (also history, energy statistics, automations and appointments with details), change nothing |
| `calendar` | only free/busy times, no details; books an appointment in a free slot without asking you |
| `shopping` | only the *Shopping list*, and what was bought when |
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

Logs (`braincall.log`), call notes of unknown callers (`call-notes.txt`), the caller memory (`calls.jsonl`), the
shopping history (`shopping-history.jsonl`) and the web usage of the day (`web-usage.json`) are in the add-on's own folder
`/app_configs/98055a04_braincall` (Samba, File editor). It is part of the add-on's backup.

## Web search

*Web search* (`web_access`, off by default) lets the assistant look things up on the web and read pages, like Claude
in the Claude chat: opening hours, recipes, train times, weather warnings. It decides itself when to search. Search
and page reading run at Anthropic (`web_search`, `web_fetch`); a search costs about one cent plus tokens.

- Callers with access `admin` or `read` get it; *web* per caller switches it on or off. Unknown callers (`*`) and
  guests never.
- Limits per answer (*Searches per answer* 3, *Pages read per answer* 2) and per day for all calls together
  (*Searches per day*, *Pages read per day*, 50 each, counted in `web-usage.json`); 0 = no limit.
- *Only these sites* / *Never these sites* restrict the sites; *Location* and *Country* give local results.
- On the phone the assistant says "Ich suche das kurz raus." before the first search and reads out no URLs.
- Web pages are only data: after a search, switching, bookings and list changes wait until the caller has said
  yes. The log shows every answer with web use (searches, pages, domains), the summary mentions it briefly.

## Energy and statistics

Callers with access `admin` or `read` can ask for consumption over a period, e.g. "How much power did I use
yesterday?" or "What did the PV produce in October?". The answer comes from the energy dashboard
(*Settings > Dashboards > Energy*): grid import, feed-in, solar, battery, gas, water, home consumption,
self-sufficiency and the individual devices. Other sensors with a `state_class` work too, e.g. the energy of
the heat pump or the average outdoor temperature. Totals reach up to a few minutes ago.

They can also ask what happened recently, e.g. "When was the garage door last open?", "Was anyone at the front
door today?" or "How long was the light on?". The answer comes from the recorder, which keeps about 10 days
by default (`recorder: purge_keep_days`); at most 31 days per question.

## Automations

Callers with access `admin` or `read` can ask what an automation or script does and why, e.g. "What does the
heating automation do?", "Would it heat right now?" or "Why did it not switch on at 14:00?". The assistant reads
the definition (triggers, conditions, branches), checks each condition against the current values and looks at
the last runs Home Assistant keeps (the traces, the last 5 per automation, only for automations with an id).
It also finds an automation by what its description says ("the one for the heat pump") and can predict a decision
from forecast values ("Will the heat pump heat tonight?"), as a forecast.
Nothing is changed. The add-on has the administrator rights this needs without an extra token.

Write the description of an automation for the assistant, it is what she explains first: what the automation is
for, its inputs (sensors, forecasts), thresholds, times and the decision rule.

## Conversation flow

Greetings, fillers ("Hmm…", "Ähm…") and status phrases ("Ich schaue in den Kalender.") are synthesized once and kept
in `cache/` in the add-on folder, so the caller hears them without waiting. Incoming calls are greeted right after
answering, outgoing calls as soon as the callee said "Hallo?".

- *Pause until end of utterance* (`end_silence_ms`, default 450 ms): after updating from an older version the old
  value (700) stays in the form; set it to 450 for faster answers.
- *Early start* (`speculative_ms`, default 300 ms): transcription and answer start already after this pause, but are
  only spoken once the caller is really done. 0 = off.
- *Fillers* on/off, *Filler after* (default 600 ms) and *Still on it after* (default 5 s, 0 = off).

What the caller says while the answer is being prepared is answered together with the rest. The log shows a timing
line per turn and a summary per call (median/max per step) to compare settings.

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

## Planned calls with a task

With *Planned calls* on, a caller with access `admin` can say e.g. "Ruf im Restaurant Seeblick an und reservier einen
Tisch für vier für heute Abend, am liebsten 19 Uhr. Trag es in den Kalender ein und ruf mich danach zurück."
The assistant reads the plan back (number, time, what it may tell and agree to, what it does afterwards) and stores it
after a yes. After you hung up it calls, tries again if nobody is reached (a callee who calls back continues the task),
then does the follow-up steps of the plan (calendar, shopping list, web search) and reports the result: as a
notification (*Notify services*), as event `braincall_task_finished` and, if asked for, with a call back.
On a mailbox it leaves a short message after the beep and does not call again: the task ends as "message left"
(`message_left`).

- During the call the callee only gets what the plan grants (free/busy times of certain days, one appointment with a
  fixed title), never details of your calendar and no control of the house.
- *Never dial*, *Only dial numbers matching*, *Hours for planned calls* and *Planned calls per day* limit what is
  dialed and when. The assistant says that it is an AI calling on your behalf; a callee who does not want to be
  called again is remembered.
- From Home Assistant: `POST http://127.0.0.1:8099/tasks` with `{"number": "…", "task": "…"}` plans a call,
  `GET /tasks` lists the tasks with their status and result, `DELETE /tasks/<id>` cancels one. Tasks are kept in
  `tasks.json` in the add-on's config folder and survive a restart.

## Automatic shopping list

*Shopping list* (default `todo.shopping_list`) is the one list callers with access `shopping` use. With
*Automatic shopping list* switched on (it is off by default) and the add-on restarted, BrainCall looks at it once
a minute:

- An item that stays checked off for *Bought after* (default 60 minutes) counts as bought: it disappears from the
  list and is stored with the time it was checked.
- Checked by mistake and unchecked again before: nothing is stored, the item stays on the list.
- Deleted items (by hand, "clear completed", removed by the assistant) are never purchases, checked or not.
- After a restart of the add-on the time starts anew for checked items.

The assistant answers "When did we last buy milk?" and "What did we buy last week?" from it. The history is
`shopping-history.jsonl` in the add-on's folder; `GET /shopping/history?item=Milch&days=30` returns it, and every
purchase fires the event `braincall_shopping_bought` (`list`, `item`, `key`, `time`). Switch *Automatic shopping
list* off and nothing is watched, written or removed.

### Rhythm list

*Rhythm list* (default `todo.shopping_rhythm`) holds the products bought again and again, e.g. `Milch (7 days)`,
with the next purchase as due date. With the automatic shopping list on, the add-on creates it (and a missing
shopping list) as a *Local To-do* list "Recurring purchases" and fills it from what was bought.

- Milk, butter, rolls get an entry after 2 purchases, pepper or dishwasher tabs after 3, a hamster or a piano never:
  Claude says once per product which kind it is.
- When a product is due and not on the shopping list, it is put there, once per due date.
- Change the interval in the name, move the due date, check an entry off to pause it, write `(never)` to keep it but
  never have it added. A deleted or paused entry comes back with the next purchase of the product.
- "What is probably running out?" and "What do we still have?" are answered from it, as an estimate.
  `GET /shopping/forecast` returns the same.

Everything the add-on writes into the rhythm list is English. Empty *Rhythm list* = none.

*Log level* `debug` shows every change seen on the lists and every removal, `trace` also each look at them.

## Updates

A new add-on version appears as an update in Home Assistant. Installing it rebuilds the image from the
matching release of the GitHub repository.
