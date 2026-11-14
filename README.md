# BrainCall

<img src="braincall/logo.png" alt="BrainCall" width="300">

**Brain Call – when your brain calls from home.**

A phone voice agent built on Claude: it calls you through a FRITZ!Box and talks with you, and you can call it.
It can read and control Home Assistant (states, lights, lists, calendars) during the call, and look things up on
the web (opening hours, recipes, train times) if you switch that on.

Runs on Windows (Python 3.12) or Linux. Audio is G.711 over SIP using pyVoIP (with fixes for the FRITZ!Box).

```
Trigger (HA automation, script, test-call.bat)
   │ HTTP POST /call {"message": "...", "number": "..."}
   ▼
app/main.py ── SIP/RTP ──► FRITZ!Box ──► phone
   │
   ├─ voice activity detection ─► OpenAI STT (gpt-4o-transcribe)
   ├─ Claude (streaming, tools: Home Assistant, web search/fetch, hang up)
   └─ OpenAI TTS (gpt-4o-mini-tts) ─► phone
```

## Files

| Path | Purpose |
|---|---|
| `app/main.py` | entry point: SIP phone, HTTP trigger, incoming/outgoing calls |
| `app/session.py` | one call: listening, barge-in, turn taking |
| `app/brain.py` | Claude prompt, tools, sentence streaming |
| `app/ha.py` | Home Assistant: search, state, control, lists, calendars, energy and long-term statistics |
| `app/automation.py` | explains automations and scripts: description, definition, current evaluation, inputs, last runs (`ha_automation`) |
| `app/speech.py` | OpenAI speech-to-text and text-to-speech |
| `app/fritz_sip.py`, `app/rtpfix.py` | fixes for pyVoIP (INVITE/ACK, audio quality) |
| `app/config.py` | configuration loading (JSONC, pointer file, environment variables) |
| `app/phrases.py` | fixed phrases (greetings, fillers, status phrases) and their cached audio |
| `app/timing.py` | timing log per turn and per call |
| `app/memory.py` | caller memory: summaries of earlier calls per number (`caller_memory`) |
| `app/web.py` | web search and web fetch: tool definitions, limits per answer and per day, usage log |
| `app/shopping.py` | automatic shopping list: watches the list, removes what stayed checked, history (`shopping-history.jsonl`), `shopping_history` |
| `app/rhythm.py` | rhythm list: recurring products with interval, self-repair, automatic adding, forecast, `shopping_rhythm` |
| `app/tasks.py` | planned calls with a task: task store (`tasks.json`), numbers and hours that may be dialed, plan and result |
| `start.bat` / `install.bat` / `test-call.bat` | start, (re)install packages, trigger a test call |
| `firewall.ps1` | Windows firewall rules (run as administrator) |
| `braincall/` | Home Assistant add-on: options form, Dockerfile, documentation |
| `repository.yaml` | makes this repository an add-on repository for Home Assistant |
| `tests/` | automated tests (pytest), see [Tests](#tests) |

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
  - `read`: read everything (states, sensors, history, energy and long-term statistics, automations,
    appointments with details),
    change nothing.
  - `calendar`: only free/busy times of the default calendars, no titles or places. Books an appointment in a
    free slot of the default calendar after repeating day and time to the caller, without asking you.
    A busy slot is refused by the code, and so are bookings in the past, longer than `calendar_max_hours`
    (default 4; all-day at most one day), more than `calendar_max_days_ahead` days ahead (default 90) and more
    than `calendar_max_bookings` per call (default 2); 0 switches a limit off. All-day entries count as busy unless their title contains one of the
    `all_day_free_words` (e.g. `["gelber", "sack", "abfall"]`); Home Assistant does not pass on Google's
    "free"/"busy".
  - `shopping`: only the shopping list `shopping_list` (read, add, check off, remove) and, with the automatic
    shopping list, what was bought when (`shopping_history`).
  - `guest`: no access; asks for name and reason. The note goes to `logs/call-notes.txt`.
- `web` (`true`/`false`) overrides who may search the web (see [Web search](#web-search)); default: `admin` and
  `read`. Guests and the `"*"` entry never get it.
- `instructions` are appended to the prompt for this caller; they can narrow, not widen, the access.
- `language` fixes the language of this caller.
- Numbers are compared completely; `+49…`, `0049…` and `0…` count as the same (`country_code`).
  A listed number wins over `"*"`. Without `"*"` unknown numbers are rejected.
- Withheld numbers are never answered: the call is rejected right away, logged as unknown and reported
  to Home Assistant (`braincall_call` with `answered: false`).
- After an incoming call Claude writes a short summary in `language` (translated if the caller spoke another
  language) and sends it to everything in `notify_services`: notify entities like `notify.phone` or notify
  services like `notify.mobile_app_phone` or an SMTP notifier. Default: for every caller except `admin`; `"notify": true/false` per caller overrides it.
- `caller_memory` (default `"none"`): remembers the summary of each call per number in `logs/calls.jsonl` and
  gives the last three to Claude at the start of the next call with that number ("Previous calls from this
  number: …"), e.g. so Maryna does not have to repeat last week's date. `30` keeps only the last 30 days (older
  entries are deleted), `"forever"` has no age limit. Only calls with a summary count, withheld numbers never.
  A caller only gets summaries of calls with at most their current access: after lowering a number from
  `admin` to `guest`, the old `admin` calls are no longer used. With `"none"` the file is neither read nor
  written; an existing one stays until you delete it.
- `default_number` is only the target of the test call and grants no rights.
- `control_domains` limits what may be controlled; `confirm_domains` (e.g. `cover`) require an explicit "yes".
  `input_datetime` lets Claudine set times of helpers (e.g. the start time of a timer: "Stell den Timer auf
  7:30"). `automation` only allows switching an automation on or off (arming a timer automation); she can never
  run one with `automation.trigger`.
- `owner_name` (e.g. `"Jan Lenck"`) is whose assistant this is; the first name is used in conversations.

## Home Assistant

Read access needs `ha_url` and a long-lived `ha_token`. Certificates from the Windows certificate store
(own CA) are trusted automatically.

Consumption over a period ("How much power did I use yesterday?", "What did the PV produce in October?")
comes from the long-term statistics of Home Assistant, which only the WebSocket API offers
(`/api/websocket`, same URL, token and `ha_verify_ssl`). Callers with access `admin` or `read` get two tools:

- `ha_energy`: totals of the energy dashboard (*Settings > Dashboards > Energy*) for a day, a month, a year or
  a date range: grid import, feed-in, solar, battery, gas, water, home consumption, self-sufficiency and the
  individual devices, optionally per hour, day, week or month.
- `ha_statistics`: one sensor with a `state_class`: the change for meters (kWh, m³), mean, minimum and maximum
  for measurements (°C, W, %).

Totals are exact for the range, up to a few minutes ago. A breakdown per hour, day, week or month comes from
the hourly statistics, so the current hour is only in the totals there.

Questions about the past ("When was the garage door last open?", "Was anyone at the front door today?",
"How long was the light on?") are answered by `ha_history` (access `admin` and `read`), using the REST
endpoint `/api/history/period` of the recorder. One entity, the last hours (default 24) or a date range of at
most 31 days, condensed for the phone:

- states such as `open`/`closed`, `on`/`off` or `home`: the changes (the most recent 30), the time in each
  state and the last period in each state (the 10 states with the most time);
- meters (`state_class: total_increasing` or `total`): the change over the range, with meter resets, marked as
  partial when there is no reading at the start or end. Meters with `last_reset` (new cycles) are left to
  `ha_statistics`;
- other numeric sensors: value at start and end, minimum and maximum with their time, the readings if there
  are at most 30.

A state that was first recorded inside the range is not taken as the state at its start (`unknown`), and a
sensor that became `unavailable` is reported as such, not with its last value.

The recorder keeps about 10 days by default (`recorder: purge_keep_days`); older data of sensors come from
`ha_statistics`.

Questions about automations and scripts ("What does the heating automation do?", "Would it heat right now?",
"Why did it not switch on at 14:00?") are answered by `ha_automation` (access `admin` and `read`, read-only):

- the definition, condensed to a few lines with friendly names: triggers (time pattern, state, numeric state,
  sun, …), conditions, and `choose`/`if` branches as *condition → action*. It comes from the WebSocket command
  `automation/config` (`script/config`), which also works for YAML automations without `id`, otherwise from
  `/api/config/automation/config/<id>`;
- each condition checked against the current states: `state`, `numeric_state` (`above`/`below`, `attribute`,
  limits from other entities), `time`, `sun`, `zone`, `template` (rendered via `/api/template`) and
  `and`/`or`/`not`, each *yes*, *no* or *unknown* (e.g. `trigger` conditions, which depend on the trigger), and
  which branch would run if it were triggered now;
- the last runs from the traces (`trace/list`, `trace/get`): what triggered, which condition passed or failed
  with the value it saw, which branch ran, or why it did not start. Home Assistant keeps the last 5 runs per
  automation, and only for automations with an `id`;
- the description in full. With `detail: "full"` nothing is cut (templates, all branches), `config` holds the
  complete definition (variables, templates, service data) and `inputs` lists every entity the definition uses
  with its current value and the attributes it reads (e.g. the hourly values of a solar forecast). Claudine uses that to predict a decision ("Will the heat pump
  heat tonight?"): she applies the rule to forecast values (`ha_forecast`, forecast sensors) and says it is a
  forecast.

`ha_automations` lists all automations and scripts with name, on/off and description (shortened to 400
characters) and finds them by words in the name or description ("the one for the heat pump"). The definitions
are read over the WebSocket (REST config API for what it cannot read) and kept for 5 minutes; the ones it could
not read are named in the result.

**Write the description for Claudine.** It is what she explains first. Put in what the automation is for and how
it decides: inputs (which sensors and forecasts), thresholds, times and the decision rule, e.g. *"Every hour:
heats with the heat pump if the outside temperature is below 15 °C and the solar forecast for the next 3 hours
(sensor.solar_forecast_next_3h) is above 2 kWh; otherwise it waits for the next hour."*

The config and trace commands need administrator rights in Home Assistant. The add-on has them (the Supervisor
talks to Home Assistant as an administrator); on Windows the user of `ha_token` must be an administrator, ideally
a separate user for BrainCall. Without them Claudine says that she cannot read the definition, and the log warns
once at startup.

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

## Automatic shopping list

`shopping_list` (default `todo.shopping_list`) is the one shopping list: callers with access `shopping` use only
this one. With `shopping_auto: true` (default `false`, takes effect after a restart) BrainCall looks at it once a
minute and keeps a history of what was bought, so Claudine can answer "When did we last buy milk?" or "What did we buy last week?".

- An item that stays checked off for `shopping_bought_after_minutes` (default 60) counts as **bought**: it is
  removed from the list and stored with the time it was checked.
- Checked by mistake in the shop and unchecked again within that time: nothing is stored, the item stays.
- An item that is deleted before its time is over (by hand, "clear completed" in the app, removed by Claudine)
  is never a purchase, checked or not. Whoever wants the history leaves checked items alone.
- After a restart the time starts anew for every checked item; the start counts as its time of purchase.
  Items added or deleted while BrainCall was not running are not in the history.
- A list that cannot be read, or is `unavailable`, changes nothing: no events, nothing removed.

The history is `logs/shopping-history.jsonl` (in the add-on: its config folder), one JSON line per event
(`added`, `bought`, `removed`), never pruned. `key` is the name folded for counting, without the amount:
"2 Milch", "milch" and "Milch (2)" are the same product, "H-Milch" is another one.

- Tool `shopping_history` (access `admin`, `read`, `shopping`): one product (last bought, on how many days, the
  dates, whether it is on the list now) or the purchases of the last days. Dates and counts are computed in code.
- `GET /shopping/history` (with `api_token` like `/tasks`), optionally `?item=Milch&days=30&event=bought`.
- Event `braincall_shopping_bought` (`list`, `item`, `key`, `time`) for own automations.

With `shopping_auto: false` nothing is watched, written or removed, and `shopping_history` is not offered.
Until 1.9.x the setting was the
list `shopping_lists`; its first entry is taken over as long as `shopping_list` is not set to something else.

### Rhythm list

`shopping_rhythm_list` (default `todo.shopping_rhythm`, `""` = none) is a second to-do list with the products that
are bought again and again, one entry each:

| Field | Content | Written by |
| --- | --- | --- |
| name | `Milch (7 days)`: product and interval | BrainCall; you may change it |
| due date | next purchase expected (last purchase + interval) | BrainCall on every purchase; you may move it |
| description | `staple · learned: 7 days · last: 2026-11-14 · 12 purchases` | BrainCall |

Everything BrainCall writes there is English, whatever `language` says; product names are never translated.

**Which products get an entry.** Claude classifies each product once by its name (`shopping-products.json`):
`staple` (milk, butter, rolls) after 2 days with a purchase, `consumable` (pepper, dishwasher tabs) after 3,
`one_off` (a hamster, a piano) never. The interval is the median of the gaps between purchases; with only two
purchases of a staple and a gap that does not fit its typical interval (a holiday in between), the typical
interval is taken and the description says `estimated`. If Claude cannot be asked, the product counts as a
consumable and is asked about again with its next purchase.

**What you can do** (app, dashboard, or Claudine for `admin` with `shopping_rhythm`):

- change the interval in the name (`Milch (10 days)`): it stays, what BrainCall learns goes into the description,
  which marks the interval `set by hand`; remove it (`Milch`) and BrainCall fills in the learned one again and
  follows what it learns from then on;
- write `(never)`: the entry stays, the product is never due and never added;
- move the due date: it stays until the next purchase;
- check the entry off: paused, not added; ends by unchecking it or with the next purchase;
- delete the entry: not recurring any more; it comes back with the next purchase (self-repair);
- add an entry by hand (`Spülmaschinentabs (60 days)`): it counts at once; without a due date today + interval.

Intervals: `(N days)`, `(N weeks)`, `(N months)`, `(daily)`, `(weekly)`, `(monthly)`, `(never)`; German ones typed
by hand (`(10 Tage)`, `(2 Wochen)`, `(wöchentlich)`, `(nie)`) are rewritten in English with the same value.
"Clear completed" on the rhythm list deletes the paused entries; each comes back with its next purchase.

**Self-repair.** After every purchase the product's entry is there and active (created, unpaused, due date moved
on). A paused or deleted entry is never put on the shopping list by BrainCall, so it only gets there when someone
needs it, and with that purchase it is back.

**Automatic adding.** Every 5 minutes a product is put on the shopping list (name without the interval) when its
entry is active, its due date is today or earlier, it is not on the shopping list (open or checked) and it was not
added for this due date before. Deleted from the shopping list, it comes back only after the next purchase or when
you move its due date.

**Forecast.** `shopping_history` modes `due` (running low or probably out) and `stock` (everything) and
`GET /shopping/forecast`: `in_stock` (less than 80 % of the interval passed), `running_low`, `probably_out`
(past the due date), `on_list`, `paused`, `never`. It is an estimate from the buying rhythm, not an inventory.

A list that cannot be read, or answers without items while it still counts open ones, changes nothing; a new
list that could not be filled and an entry that could not be updated after a purchase are done again with the
next polls.

**Creating the lists.** With `shopping_auto` on, BrainCall creates a missing `todo.shopping_list` (the built-in
*Shopping list*), another missing shopping list and the rhythm list (*Local To-do*, display name "Recurring
purchases") once Home Assistant has finished starting, and fills a new rhythm list from the history. Existing lists
are never touched. Creating needs administrator rights (the add-on has them); otherwise the log says how to create
the list by hand and that part stays off.

## Logging

`log_level` (default `"info"`) decides how much `braincall.log` and the console show: `"warning"` only problems,
`"info"` calls and changes, `"debug"` also details (every change seen on the shopping list, every removal,
every change to the rhythm list, every product added or classified), `"trace"` also every look at the lists. Other libraries never log below `info`.

## Web search

With `web_access: true` Claudine can look things up on the web like Claude in the Claude chat: "Wann hat der
Baumarkt in Mölln heute auf?", "Such mir ein Rezept für Grünkohl". Claude decides itself when to search and which
page to read. Search and fetch are Anthropic's server tools (`web_search`, `web_fetch`) and run at Anthropic inside
the request, no own crawler or search provider. Costs: $10 per 1,000 searches plus the tokens of the results and
pages, billed to the Anthropic account.

- **Who:** callers with access `admin` or `read`; `"web": true` / `false` per caller changes that. Guests and unknown
  callers (`"*"`) never get it. Off by default (`web_access: false`).
- **Limits:** `web_max_searches` (default 3) and `web_max_fetches` (default 2) per answer, enforced by the API;
  `web_max_searches_per_day` / `web_max_fetches_per_day` (default 50 each) for all calls together, counted in
  `logs/web-usage.json`. When the day is used up the tool is left out; in a call that already used it, it stays
  (the API needs it for the earlier results) with at most one more search per answer, and Claude is told not to use
  it. `web_max_content_tokens` (default 8000) cuts long pages. 0 = no limit for each of them.
- **Sites:** `web_allowed_domains` (only these) or `web_blocked_domains` (never these); if both are set only the
  first counts. `web_city` / `web_country` (e.g. `"Mölln"`, `"DE"`) and the timezone localize results.
- **On the phone:** before the first search Claudine says "Ich suche das kurz raus." (also after a "Hmm…"), and "Ich
  bin noch dran." if it takes long. Answers are short, without URLs; URLs and citation marks are never read out.
  A long research that the API pauses (`pause_turn`) is continued automatically.
- **Safety:** web pages are data, never instructions. Once a search or page result is in the conversation, switching,
  bookings and list changes are held back until the caller has said something after Claude asked: a page cannot
  confirm a change by itself. Every answer with web use is logged per caller (`Web Jan Mobile (phone): 2 searches,
  1 fetches (today 7/50 …); domains: …; queries: …`), and the summary after the call mentions lookups briefly.
- Fetching is limited to URLs that already appeared in the conversation (search results, what the caller said).
  Pages that need JavaScript cannot be read; text, HTML and PDF work. The basic tool versions are used on every model
  (no dynamic filtering): no code execution in between, and the limits per answer hold.

## Planned calls with a task

Claudine can make a call on her own with a task, hold the conversation and work through the result:
"Ruf im Restaurant Seeblick an und reservier einen Tisch für vier für heute Abend, am liebsten 19 Uhr, sonst zwischen
18 und 21 Uhr. Trag es in den Kalender ein und ruf mich danach zurück." On by default (`tasks: true`).

**Planning.** A caller with access `admin` gives the task on the phone. Claudine collects what is missing (the number:
asked for, or looked up with the [web search](#web-search); never guessed), then calls the tool `plan_call`. The code
checks the plan and Claudine reads it back: whom she calls at which number, when, what she may tell the callee, what
she may agree to on her own, what she does afterwards and how the result comes back. The task is stored only after
the caller said something in reply (a `confirmed=true` from the model alone is not enough), and a yes covers only
the plan that was read back: another number, time window, grant or follow-up tool is read back again.
One line: the planned call starts after the requester hung up.

Without the phone, `POST /tasks` plans a call (same token as `/call`):

```bash
curl -X POST http://127.0.0.1:8099/tasks -H "Content-Type: application/json" -d '{
  "number": "04542 1234", "name": "Restaurant Seeblick",
  "task": "Tisch für 4 für heute Abend reservieren, am liebsten 19 Uhr, sonst 18 bis 21 Uhr. In den Kalender eintragen.",
  "requester": "0151 1234567", "call_back": true, "not_after": "2026-11-10T19:00"}'
```

With only `number` and `task` Claude turns the text into a plan. Every field of the plan can also be given directly
and then wins: `goal`, `questions`, `may_share`, `may_accept`, `calendar_from` / `calendar_to` / `book_title`,
`follow_up` (`[{"step": "…", "tool": "ha_create_event"}]`), `not_before`, `not_after` (local time, default 24 hours),
`retries` (default 2), `leave_message`, `call_back` (needs `requester`, a number from `callers`), `language`.
`GET /tasks` lists all tasks, `GET /tasks/<id>` shows one, `DELETE /tasks/<id>` cancels a planned one.

**The call.** Tasks are called when they are due, the line is free and it is inside `outbound_hours`.
No answer or busy: the next attempt follows after `task_retry_minutes`, at most `retries` times and only inside the
time window; then the task ends as `not_reached`. A mailbox gets a short message, and that ends the task as
`message_left`: it is reported and not dialed again. With `leave_message` false, or if the mailbox hangs up before the
message, it counts as not reached and is tried again. If the callee calls back (between the attempts, or after a
message until the end of the time window), the call is answered (also from a number that is not in `callers`), the
task continues there and its new result is reported.

A mailbox is recognized by its announcement ("Mailbox", "Mobilbox", "Anrufbeantworter", "nach dem Ton", "voicemail",
"after the tone" …) while the call opens: nothing is played into the announcement (no thinking sounds), Claude
prepares the message meanwhile without any tool, and 1.5 s after the last sound (the beep) the greeting and the
message follow, then the call ends. The message holds only what the task covers. If someone speaks into the message
it is a person after all and the call goes on as a conversation. An announcement without such words is left to
Claude as before.

**Rights in three phases.** The requester is not the person on the phone:

- *Planning*: only `admin`.
- *During the call*: the callee has their own access from `callers` (not listed = guest) plus the grants of the plan,
  nothing else: `ha_busy_times` for the days `calendar_from` to `calendar_to` (busy times only, no details), and with
  `book_title` one appointment with exactly this title in a free time of these days. "Lies mir Jans Termine vor" or
  "Schalte die Heizung aus" find no tool. Claudine is told what she may share (`may_share`) and agree to
  (`may_accept`); anything else stays open.
- *Follow-up* (after hang-up, without the callee): Claude gets the plan, the transcript and only the tools that the
  follow-up steps of the plan name, out of `ha_create_event`, `ha_events`, `shopping_edit`, `shopping_list` and `web`
  (web search, if `web_access` is on). Never `ha_control`. The transcript and web pages are data: a step that the call
  did not settle clearly is not done but reported as open. At most 40 changes per task.

**Result.** Status `done`, `partial`, `failed`, `not_reached`, `message_left` or `cancelled`, a summary, the
collected answers, each follow-up step with its outcome, and open points. It is stored with the task in `logs/tasks.json` (finished tasks
for 30 days), sent to `notify_services`, fired as event `braincall_task_finished` (`id`, `status`, `number`, `name`,
`task`, `requester`, `attempts`, `summary`, `answers`, `steps`, `open_points`) and, with `call_back`, reported in a
call to the requester as soon as the line is free. The call itself is summarized like every call (`braincall_call`,
[caller memory](#callers-and-permissions)). A task that was running when BrainCall stopped is not dialed again: it
ends as `failed` and is reported.

**Safety and etiquette.**

- `outbound_blocked`: numbers starting with one of these are never dialed, also not via `/call` (default: 110, 112,
  118…, 0137, 019…, 0900). `outbound_allowed_regex` allows only matching numbers, written in national form
  (`+49 4542 1234` is `045421234`), also for `/call`. The default `\*\*\d{1,4}|0(?:1[5-7]\d{8,9}|(?![789]00)[2-9]\d{5,11})`
  lets through German mobile (015x–017x) and landline numbers and internal FRITZ!Box numbers (`**6`, `**611`): no
  short codes (110, 112, 115, 116 117, 118 xx), no service numbers (0137, 0180, 019x, 0700, 0800, 0900) and no other
  countries. It is not exact about lengths. `""` = no limit; for another country set your own expression.
- `outbound_hours` (default `09:00-20:00`) and `outbound_max_calls_per_day` (default 20, attempts count) apply to
  planned calls; `/call` and the call back to the requester are not limited by them.
- At the start Claudine says that she is an AI assistant calling on behalf of the requester, and before the goodbye
  that a written summary is kept and no audio is recorded.
- A callee who asks not to be called again is stored (`do_not_call` in `tasks.json`); a new task for that number is
  refused unless the requester explicitly overrides it (`override_do_not_call`).

Not part of this yet: the Brain Assist front end (service action, `todo` entity), phone menus (DTMF) and hold
queues, opening hours, and a second call while the requester stays on the line.

## Conversation flow and timing

- **Greeting without waiting:** greetings are synthesized at startup and kept in `cache/` in the log folder
  (rebuilt when language, voice, TTS model, speaking style, assistant or owner name change). Incoming calls hear
  "Hallo, hier ist Claudine, die Assistentin von Jan Lenck." (known callers "Hallo Jan, hier ist Claudine.") right
  after answering. Outgoing calls play "Hallo, hier ist Claudine, ich rufe im Auftrag von Jan Lenck an." as soon as
  the callee has said something short like "Hallo?", or after 2.5 s of silence. A longer first utterance may be a
  mailbox announcement: no greeting and no thinking sounds are played into it; a recognized mailbox gets the
  greeting and the message after the beep (see [planned calls](#planned-calls-with-a-task)), otherwise the model
  greets. Claude is told the greeting has been spoken.
- **End of speech:** an utterance counts as finished after `end_silence_ms` (default 450 ms) of silence. After
  `speculative_ms` (default 300 ms) transcription and Claude already start; the answer is only spoken and changing
  tools (switching, booking, hanging up) only run once the pause is long enough. If the caller goes on speaking it is
  dropped and the longer utterance is answered. Every utterance is transcribed in the background as soon as it ends.
- **Fillers** (`fillers`, default on): if the answer has no audio `filler_delay_ms` (default 600 ms) after the
  caller stopped, a short thinking sound "Hmm…", "Ähm…" or "Öhm…" (the same in every language, never the same twice in a row; no
  "Okay" or "Ja", which would sound like an answer). The first tool call of an
  answer plays a matching phrase ("Ich schaue in den Kalender.", "Ich schalte das.", "Moment, ich schau kurz
  nach."), and after `still_working_s` (default 5) seconds without anything said "Ich bin noch dran." Fillers are
  in the log and transcript, not in the conversation Claude sees, and can be interrupted like answers.
- **Everything said counts once:** all the caller said since the last answer is answered as one message
  ("Nein. Nein."). Speech while Claude is still preparing the answer (nothing said yet) restarts the answer with
  everything said, unless a changing tool already ran; then it is the next turn. What was said before the last
  sentence of an answer is never taken as the answer to a later question (dropped, logged).
- **Timing log:** every turn logs, in ms after the end of the caller's speech, when the end was detected, the
  transcription, Claude's first token and sentence, the first TTS audio, filler and playback, and the tool calls with
  their duration. At the end of a call: median and maximum per step, e.g.
  `Timing of 6 turns, median/max ms after end of speech: end detected 452/470, STT 980/1400, …, first audio 640/1210`.

## Languages

`language`: `de`, `uk`, `en` or `auto` (answers in the language the caller spoke last).
`language` in a `callers` entry sets the language per caller. Fixed phrases exist in German, Ukrainian and English.

## Limits

- Response time per turn is about 1.5–3 s (STT after end of speech, then Claude, then TTS); a filler is heard
  before that (see [Conversation flow and timing](#conversation-flow-and-timing)).
- An unanswered outbound call is cancelled after `ring_timeout` seconds.
- The machine must be on, awake and in the home network.
- If the FRITZ!Box is restarted or unreachable, BrainCall sets up the SIP connection again after
  5 minutes without registration and keeps trying every 5 minutes. Calls in between fail.
- Costs are billed to your Anthropic and OpenAI API accounts.

## Tests

The tests in `tests/` run without a FRITZ!Box, Home Assistant or API keys: network access is replaced by fakes.
They cover access rights and tools per caller, web search (tools by access, limits, server tool blocks, `pause_turn`,
confirmation after web content), Home Assistant control limits, calendars and restricted bookings,
planned calls (numbers and hours, plan and confirmation, grants for the callee, retries, mailbox and call backs,
follow-up, `/tasks`), the automatic shopping list (the hour, mistakes, restarts, errors, history, `/shopping/history`),
the rhythm list (learning, classes, self-repair, automatic adding, forecast, creating lists, `/shopping/forecast`),
configuration and pointer files, the HTTP trigger, the speech recognition filter, audio and the
SIP fixes, and the conversation flow (merged and stale utterances, restart while thinking, speculative start, fillers,
mailbox announcements, timing).

```bash
python -m pip install -r requirements.txt pytest
python -m pytest -q
```

On Windows use the virtual environment of `start.bat`: `%LOCALAPPDATA%\braincall\venv\Scripts\python.exe -m pytest -q`
(pytest installed there once). Tests marked `xfail` describe known bugs with an open issue; once fixed they fail
until the marker is removed. CI runs the tests on Python 3.12 and 3.13.

## License

MIT, see [LICENSE](LICENSE).
