"""Supervisor discovery: as an app on Home Assistant OS, BrainCall tells Home Assistant where its HTTP API is and
which token it needs. Home Assistant then offers Brain Assist without an address or token to type (#50).

The Supervisor accepts the message only for services listed under `discovery` in braincall/config.yaml.
"""
import json
import logging
import time

import requests

log = logging.getLogger("discovery")

SUPERVISOR = "http://supervisor"
SERVICE = "brainassist"
# Home Assistant Core and the app share the host network: 127.0.0.1 reaches the API whichever address it listens on
HOST = "127.0.0.1"
ATTEMPTS = 5
RETRY_S = 10


def message(cfg, token: str) -> dict:
    """What Home Assistant gets: the config flow of Brain Assist reads host, port and token from it."""
    return {"service": SERVICE, "config": {"host": HOST, "port": int(cfg.http_port), "token": token}}


def _forms(secret: str) -> list[str]:
    """How a secret may appear in an echo: as it is, escaped in JSON (with or without \\u escapes), as repr()."""
    forms = {secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1], repr(secret)[1:-1]}
    return sorted(forms, key=len, reverse=True)  # the longest first: an escaped form contains the plain parts


def _redact(text: str, *secrets: str) -> str:
    """The text without the secrets: an error answer or exception may echo what was sent, the token included."""
    for secret in secrets:
        if secret:
            for form in _forms(secret):
                text = text.replace(form, "***")
    return text


def announce(cfg, token: str, sleep=time.sleep) -> bool:
    """Sends the discovery message to the Supervisor; True if it took it.

    Only as an app (SUPERVISOR_TOKEN set). Tries a few times: right after a reboot the Supervisor may still be busy.
    A message that did not change since the last start is not shown again by Home Assistant; a new token replaces
    the old message, and Brain Assist updates its entry. The token is never logged.
    """
    if not cfg.supervisor_token:
        return False
    for attempt in range(1, ATTEMPTS + 1):
        try:
            r = requests.post(f"{SUPERVISOR}/discovery", json=message(cfg, token), timeout=10,
                              headers={"Authorization": f"Bearer {cfg.supervisor_token}"})
        except requests.RequestException as e:
            reason = f"{type(e).__name__}: {e}"
        else:
            if r.status_code == 200:
                log.info("Home Assistant was told where the API is: Brain Assist can be added without a token "
                         "(Settings → Devices & services)")
                return True
            # redacted before it is cut: a token cut in half would no longer be found
            reason = f"{r.status_code} {_redact(r.text, token, cfg.supervisor_token)[:200]}"
            if r.status_code in (400, 401, 403):  # will not get better by asking again
                break
        if attempt < ATTEMPTS:
            sleep(RETRY_S)
    log.warning("Telling Home Assistant about the API failed (%s). Brain Assist can still be added by hand with "
                "address and token", _redact(reason, token, cfg.supervisor_token))
    return False
