# Changelog

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
- Old settings are converted on start and written back once (needs Supervisor API access): trusted numbers become
  `admin`, `*` becomes a `guest` entry, numbers only in *Allowed callers* are dropped

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
