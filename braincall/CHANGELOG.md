# Changelog

## 1.0.0

- First version as BrainCall: answers and makes phone calls through the FRITZ!Box, reads and controls Home
  Assistant during the call
- Options form, host network for SIP/RTP, Home Assistant access through the Supervisor
- Logs and call notes in the add-on's own folder `/app_configs/<slug>`; the log file `braincall.log` rotates
  at 2 MB and keeps 5 old files
- Stopping the add-on hangs up and unregisters from the FRITZ!Box, also during startup
- Uses Home Assistant's timezone
