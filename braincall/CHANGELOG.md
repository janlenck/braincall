# Changelog

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
