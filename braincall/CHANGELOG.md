# Changelog

## 1.10.0

- New: automatic shopping list (#69), off by default: switch on *Automatic shopping list* (`shopping_auto`) and
  restart the add-on. BrainCall then looks at the shopping list once a minute. An
  item that stays checked off for an hour counts as bought: it disappears from the list and is stored with the
  time it was checked in `shopping-history.jsonl` (add-on folder). Checked by mistake in the shop and unchecked
  again within the hour: nothing is stored. Deleted items are never purchases. After a restart the hour starts
  anew. *Bought after* (`shopping_bought_after_minutes`) changes the hour
  - Claudine answers "Wann haben wir zuletzt Milch gekauft?" and "Was haben wir letzte Woche gekauft?" with the new
    tool `shopping_history` (access `admin`, `read`, `shopping`), dates and counts computed in code
  - `GET /shopping/history` (`item`, `days`, `event`) and the event `braincall_shopping_bought` (`list`, `item`,
    `key`, `time`)
- Changed: one shopping list. *Shopping lists* (`shopping_lists`) became *Shopping list* (`shopping_list`, one
  entity id, default `todo.shopping_list`). An old setting is taken over (its first list) as long as the new one
  is not set to something else; further lists are no longer used, the log says which
- New: *Log level* (`log_level`): `warning`, `info` (default, as before), `debug` (also every change seen on the
  shopping list) or `trace` (also every look at it). Other libraries never log below `info`
- New: rhythm list (#70), part of the automatic shopping list (`shopping_auto`). A second
  to-do list `todo.shopping_rhythm` (*Rhythm list*, `shopping_rhythm_list`) holds the products bought again and
  again with their interval, `Milch (7 days)`, and the next purchase as due date
  - Claude classifies each product once: staples (milk, butter, rolls) get an entry after 2 purchases, consumables
    after 3, one-off things never. Interval = median of the gaps; English in the list whatever the language
  - Products that are due are put on the shopping list, once per due date
  - Repairable: change the interval in the name, move the due date, check an entry off to pause it, `(never)` to
    keep it but never add it. A deleted or paused entry comes back with the next purchase (self-repair)
  - Missing lists are created in Home Assistant when the add-on starts (shopping list and rhythm list) and a new
    rhythm list is filled from the history
  - `shopping_history` answers what is probably running out (`due`) and the estimated stock (`stock`);
    `GET /shopping/forecast`; new tool `shopping_rhythm` (access `admin`): "Stell Pfeffer auf 60 Tage",
    "Milch pausieren"

## 1.9.6

- Fix: dates were spoken as two sentences with a pause in the middle, "Der Termin ist am 14." – "November um
  18 Uhr" (#67). A period after a number of one or two digits ends no sentence when the text goes on with it:
  after "am", "im", "zum", "der", "einen", "unser" … ("im 3. Stock", "einen 2. Versuch"), or when a month name,
  a lowercase word or a number follows ("Freitag, 14. November", "der 1. und 2. Dezember"), also with Markdown
  around it. "Es ist 18 Uhr 30. Soll ich …" is still two sentences

## 1.9.5

- Fix: a planned call that reached a mailbox was dialed again after the message had been left, up to three calls
  and messages in half an hour, and then reported as "not reached" (#65)
  - A message on the mailbox ends the task with the new status `message_left` ("Nachricht hinterlassen"): reported
    by notification, event and call back, no further attempt. A callee who calls back until the end of the time
    window still continues the task. Without a message (`leave_message` off, or the mailbox hung up first) the call
    counts as not reached and is tried again, as before
  - Mailbox announcements are recognized by the phone system ("Mailbox", "Mobilbox", "Anrufbeantworter", "nach dem
    Ton", "voicemail", "after the tone" …): no "Ähm…" or "Ich bin noch dran." into the announcement, the greeting
    and the message follow 1.5 s after the beep, then the call ends
  - On a mailbox Claudine has no tools: she used to look into the calendar instead of speaking, and the mailbox
    recorded only the thinking sounds
  - A task call and the message contain only what the task covers, nothing from earlier calls; the sentence about
    the written summary is not spoken onto a mailbox
- Fix: "Ich habe Jan T. dreimal angerufen" was spoken as two sentences, cut after the initial
- Fix: smileys are never read out, neither as characters (";-)", emojis) nor named ("Zwinkersmiley")
- Fix: summaries of calls Claudine made no longer say that the other party called; a mailbox announcement is
  not taken for a person speaking, and a call back is only mentioned if one was asked for

## 1.9.4

- Claudine can set the start time of a timer and switch timer automations on or off ("Stell den Start-Timer auf
  7:30 und schalte den R5-Timer ein"). *Controllable domains* (`control_domains`) now include `input_datetime`
  (set times with `set_datetime`) and `automation` by default. Automations can only be switched on (armed) or
  off, never run right away (`automation.trigger`) or reloaded
- Existing installations keep their saved *Controllable domains*: add `input_datetime` and `automation` there
  yourself
- Automations (#61): Claudine follows what an automation does and predicts its decisions
  - She reads the whole description of an automation or script (`ha_automation`), also of one from a blueprint;
    it used to be cut after 200 characters. She explains from the description first
  - New tool `ha_automations`: all automations and scripts with name, on/off and description, found by words in
    the name or description ("the one for the heat pump"); falls back to the REST config API and names those it
    could not read
  - `ha_automation` with `detail: "full"`: nothing cut or capped (templates, all branches), the complete definition
    (variables, templates, service data), plus every entity it uses with its current value and the attributes the
    rule reads (e.g. the hourly values of a solar forecast)
  - Forecasts: "Will the heat pump heat tonight?" She applies the rule to forecast values (`ha_forecast`, forecast
    sensors), names the inputs and says it is a forecast
  - Without administrator rights she says that the definition cannot be read, instead of guessing; the log warns
    once at startup if the token is not an administrator (Windows setup; the add-on has the rights)
  - README: how to write descriptions so Claudine can use them

## 1.9.3

- Removed the old settings *Allowed callers*, *Trusted numbers* and *Language per number* (`inbound_allowed`,
  `trusted_numbers`, `caller_languages`, until 1.0.x) (#62). They stayed in the configuration form for good and,
  while filled, replaced *Callers* completely. Only *Callers* counts now; old keys left in a saved
  configuration are ignored

## 1.9.2

- Fix: Claudine claimed she could not remember earlier calls and then quoted them after all. She now always
  knows how her memory works (`caller_memory`): off, on without notes for this caller, or on with notes, and
  answers from the notes when asked whether or what she remembers

## 1.9.1

- Fix (#51): planned calls to numbers written with spaces ("04542 4635") failed and the FRITZ!Box dialed only the
  first part. Numbers are now stored without spaces, dashes or brackets and always dialed complete; an INVITE that
  cannot be parsed is never sent
- Phone numbers are read out digit by digit ("null, vier, fünf, vier, …"; English and Ukrainian too, in the
  caller's language) instead of in groups with swallowed digits. Dates, times and prices stay as they are
- No more "TODO: Add 500 Error on Receiving SIP Response" for 183 Session Progress in the log

## 1.9.0

- Planned calls with a task (#51, first part): Claudine calls on her own, holds the conversation and works through
  the result. "Ruf im Restaurant Seeblick an und reservier einen Tisch für heute Abend, am liebsten 19 Uhr. Trag es
  in den Kalender ein und ruf mich danach zurück."
  - Callers with access `admin` plan a call on the phone (tool `plan_call`): Claudine reads the plan back (number,
    time window, what she may tell and agree to, follow-up steps) and stores it only after a yes. Also via
    `POST /tasks`; `GET /tasks` lists them, `DELETE /tasks/<id>` cancels. Tasks are kept in `tasks.json` in the log
    folder and survive a restart
  - The call starts when the line is free and the time has come. Not reached (no answer, busy, mailbox): further
    attempts every *Pause between attempts* (`task_retry_minutes`) inside the time window; a callee who calls back
    continues the task in that call
  - During the call the callee only has their own access plus what the plan grants: free/busy times of certain days
    and, if planned, one appointment with a fixed title there. Claudine says that she is an AI assistant calling on
    behalf of the requester and that only a written summary is kept
  - Afterwards, without the callee, the follow-up steps of the plan run with the tools the plan names (calendar,
    shopping list, web search): nothing else, and nothing the call did not settle clearly. The result (status,
    answers, steps, open points) goes to the notify services, as event `braincall_task_finished` and, if asked for,
    as a call back to the requester
  - Safety: *Never dial* (`outbound_blocked`: emergency and premium numbers, also for `/call`), *Only dial numbers
    matching* (`outbound_allowed_regex`; by default German mobile and landline numbers and internal numbers like `**611`
    only, also for `/call`: no short codes, service numbers or other countries; empty = no limit), *Hours for planned calls* (`outbound_hours`, 09:00-20:00), *Planned calls
    per day* (`outbound_max_calls_per_day`, 20). A callee who asks not to be called again is remembered
  - Not yet: Brain Assist front end (#50), phone menus (DTMF) and hold queues, opening hours

## 1.8.0

- Web search (#52): with *Web search* (`web_access`, off by default) the assistant looks things up on the web like
  Claude in the Claude chat ("Wann hat der Baumarkt in Mölln heute auf?", "Such mir ein Rezept für Grünkohl"), using
  Anthropic's server tools `web_search` and `web_fetch`. Callers with access `admin` or `read` get it, `web` per
  caller overrides that; guests and unknown callers never
  - Limits per answer (`web_max_searches` 3, `web_max_fetches` 2) and per day for all calls (`web_max_searches_per_day`,
    `web_max_fetches_per_day`, 50 each, kept in `web-usage.json`), `web_max_content_tokens` (8000) for long pages,
    `web_allowed_domains` / `web_blocked_domains`, `web_city` / `web_country` for local results
  - "Ich suche das kurz raus." before the first search, also after a thinking sound; URLs and citation marks are never
    read out. Long research paused by the API (`pause_turn`) is continued
  - Web pages are data, never instructions: after web content entered the call, switching, bookings and list changes
    only run after the caller said something in reply to the question. Web use is logged per caller with domains,
    the summary mentions it briefly
- Packages: `anthropic` 0.80 or newer; `start.bat` now also updates packages that are too old, not only missing ones

## 1.7.1

- Fillers are thinking sounds now ("Hmm…", "Ähm…", "Öhm…"), the same in every language (#53). "Okay", "Ja" or
  "Gut" sounded like an answer: "Ist das okay?" got an "Okay." before the actual "No", "Danke und tschüss" an
  "Okay." before the goodbye

## 1.7.0

- Snappier conversations (#53):
  - Greetings are synthesized at startup and cached in `cache/` in the log folder: incoming calls are greeted right
    after answering, outgoing calls as soon as the callee said something short ("Hallo?"); a long first utterance
    (maybe a mailbox) is still left to the model. Claude is told the greeting was spoken
  - *Pause until end of utterance* (`end_silence_ms`) defaults to 450 ms instead of 700 ms (an existing add-on
    configuration keeps its value). New *Early start* (`speculative_ms`, default 300 ms): transcription and answer start
    after a shorter pause and are dropped if the caller goes on speaking; nothing is said and no switching, booking or
    hanging up happens before the pause is long enough. Utterances are transcribed in the background as soon as they end
  - New *Fillers* (`fillers`, `filler_delay_ms`, `still_working_s`): a short "Hm"/"Ja"/"Okay" when the answer is slow,
    a matching phrase when a tool runs ("Ich schaue in den Kalender."), "Ich bin noch dran." after 5 s of silence.
    Fast answers come without filler; fillers are not part of the conversation Claude sees
  - Everything the caller said since the last answer is answered as one message. Speech while the answer is still
    being prepared restarts it with everything said (not after a changing tool ran). Speech from before the last
    sentence of an answer is never taken as the answer to a later question
  - Timing log per turn (end detected, STT, first token, first sentence, TTS, filler, playback, tools) and a summary
    per call with median and maximum per step

## 1.6.0

- New tool `ha_automation` (access `admin` and `read`, read-only): explains one automation or script. The
  definition condensed with friendly names (triggers, conditions, `choose`/`if` branches as condition → action),
  each condition checked against the current values (`state`, `numeric_state`, `time`, `sun`, `zone`,
  `template`, `and`/`or`/`not`; yes, no or unknown) with the branch that would run now, and the last runs from
  the traces: what triggered, which condition failed with the value it saw, which branch ran. Needs an
  administrator in Home Assistant (the add-on has it; on Windows the user of the token)

## 1.5.1

- Barge-in works reliably during long answers: the caller interrupts after *Minimum time to interrupt*
  (`barge_in_min_ms`, default 1000 ms) of speech, short pauses between words included, instead of the 1.4–2 s
  (or never) before. The answer counts as one block from its first sentence until it is spoken, so speech
  that starts while the next sentence is being synthesized, or while the model is still thinking, interrupts
  too, and the 0.6 s grace period is only at the start of the answer. The noise floor no longer adapts to the
  echo of our own voice. Coughs, doors and short "mhm" still do not interrupt and are not answered; speech
  that began over the end of the answer and goes on after it is answered. Ignored barge-ins are logged at debug level with speech time and threshold

## 1.5.0

- New option *Caller memory* (`caller_memory`, default `none`): the summary of each call is kept per number in
  `calls.jsonl` in the log folder and the last three are given to the assistant at the start of the next call
  with that number. `none` = off, a number of days (e.g. `30`) = only calls of that period (older entries are
  deleted), `forever` = no age limit. A caller only gets summaries of calls with at most their current access

## 1.4.0

- New tool `ha_history` (access `admin` and `read`): what one entity did in the last hours (default 24) or in
  a date range of up to 31 days, from the recorder (`/api/history/period`), e.g. "When was the garage door last
  open?". States come back as the most recent 30 changes plus the time in each state and the last period in
  each state (the 10 states with the most time); meters (`total_increasing`, `total`) as the change over the
  range including meter resets, marked as partial without a reading at the start or end (meters with
  `last_reset` cycles are left to `ha_statistics`); other numeric sensors as value at start and end and
  minimum/maximum with their time

## 1.3.0

- New tools `ha_energy` and `ha_statistics` (access `admin` and `read`): consumption, solar yield, feed-in,
  battery, gas, water, self-sufficiency and devices from the energy dashboard, and the long-term statistics of
  single sensors (change for meters, mean/min/max for measurements) for a day, month, year or date range,
  optionally per hour, day, week or month. They use the WebSocket API of Home Assistant (new dependency
  `websocket-client`)

## 1.2.0

- Speech recognition no longer discards real requests that mention music, subtitles or a web address
  ("Mach Musik an"): typical invented phrases are only dropped as the whole utterance ("Musik", "Untertitel")
  or by unambiguous patterns ("[Musik]", "♪", "Untertitel im Auftrag des ZDF", amara.org). "Telefonat mit …"
  uses the first name from *Owner name* instead of a fixed name
- `ha_state` returns the useful attributes per domain: thermostat current and target temperature and
  `hvac_action`, cover position, light brightness, media title/artist/volume/source, weather temperature,
  humidity and wind. `ha_search` and `ha_list` show the one or two most important of them
- New tool `ha_forecast` (access `admin` and `read`): weather forecast daily, hourly or twice daily via
  `weather.get_forecasts`
- Bookings by callers with access `calendar` are limited by the code: no start in the past, at most
  *Longest booking* (`calendar_max_hours`, default 4; all-day at most one day), at most *Booking lead time*
  (`calendar_max_days_ahead`, default 90) ahead and at most *Bookings per call* (`calendar_max_bookings`,
  default 2). The limits are checked before the caller is asked to confirm

## 1.1.2

- Console log lines show the date as well as the time (`YYYY-MM-DD HH:MM:SS`), like the log file

## 1.1.1

- New option *Free all-day entries* (`all_day_free_words`): all-day entries whose title contains one of these
  words (e.g. waste collection) no longer count as busy for callers with access `calendar`. Home Assistant does
  not pass on Google's "free"/"busy", so the title decides
- *Notify after calls* also works with notify entities (e.g. `notify.sm_g525f`, sent via `notify.send_message`),
  not only with legacy notify services; the error text from Home Assistant is logged

## 1.1.0

- One list **Callers** instead of *Allowed callers*, *Trusted numbers* and *Language per number*: number (or `*`),
  name, access, optional instructions for the assistant, language and notify
- Access `admin`, `read`, `calendar`, `shopping`, `guest`, combinable (`shopping, calendar`). The code decides
  which tools the assistant gets, instructions cannot widen it
- `calendar`: only free/busy times without details, books appointments in free slots of the default calendar
  without asking the owner; busy slots are refused
- `shopping`: only the lists in the new option *Shopping lists* (default `todo.shopping_list`)
- After incoming calls a short summary, translated into the configured language, goes to the services in the new
  option *Notify after calls* (phone, e-mail). The event `braincall_call` gets `caller_name`, `access`, `summary`
- Old settings keep working: as long as they are filled they are converted on every start and used instead of
  *Callers* (trusted numbers become `admin`, `*` becomes a `guest` entry, numbers only in *Allowed callers* are
  dropped). Move them to *Callers* and empty the old fields
- A fresh installation starts with an empty caller list (all incoming calls rejected) until entries are added

## 1.0.1

- Reconnects to the FRITZ!Box on its own after a restart or outage of the box: after 5 minutes without SIP
  registration and then every 5 minutes until it works again, never during a call. Before, BrainCall stayed
  offline until the add-on was restarted
- Also starts when the FRITZ!Box is not up yet (e.g. after a power cut) and registers once it is

## 1.0.0

- First version as BrainCall: answers and makes phone calls through the FRITZ!Box, reads and controls Home
  Assistant during the call
- Options form, host network for SIP/RTP, Home Assistant access through the Supervisor
- Logs and call notes in the add-on's own folder `/app_configs/<slug>`; the log file `braincall.log` rotates
  at 2 MB and keeps 5 old files
- Stopping the add-on hangs up and unregisters from the FRITZ!Box, also during startup
- Uses Home Assistant's timezone
