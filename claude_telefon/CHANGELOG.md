# Changelog

## 1.0.1

- Icon and logo: telephone handset with a coiled cord that fades out
- Documentation: current Home Assistant names ("Apps"), on/off switch with `hassio.app_start`/`hassio.app_stop`
- Log file `claude-telefon.log` rotates at 2 MB and keeps 5 old files instead of growing without limit

## 1.0.0

- First version as Home Assistant add-on: options form, host network for SIP/RTP, Home Assistant access through
  the Supervisor, logs and call notes in `/share/claude-telefon`
- Stopping the add-on hangs up and unregisters from the FRITZ!Box, also during startup
- Uses Home Assistant's timezone
