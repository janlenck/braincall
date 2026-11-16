"""What the log shows, and of whom (#77).

The levels: info = what is said and done; debug = plus the commands (tool calls and their results, service data,
API requests that change something); trace = plus every single step (polls, SIP messages, requests to Home
Assistant) and the debug output of other libraries.

BrainCall's own lines follow log_level. Every other logger (httpcore2, urllib3, websocket, the Anthropic SDK …) is
told apart by the file it logs from, not by a list of names: a renamed library cannot slip through. Below trace they
show only warnings. With trace they show everything except HTTP headers (the Claude API answers with its organization
and workspace id) and the configured secrets.
"""
import json
import logging
import os

TRACE = 5  # below DEBUG: every single step
logging.addLevelName(TRACE, "TRACE")

APP_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep
MIN_SECRET = 8  # shorter "secrets" (a 4-digit SIP password) would blank out ordinary words and numbers


def forms(secret: str) -> list[str]:
    """How a secret may appear in an echo: as it is, escaped in JSON (with or without \\u escapes), as repr()."""
    found = {secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1], repr(secret)[1:-1]}
    return sorted(found, key=len, reverse=True)  # the longest first: an escaped form contains the plain parts


def redact(text: str, *secrets: str) -> str:
    """The text without the secrets: an error answer or exception may echo what was sent, the token included."""
    for secret in secrets:
        if secret:
            for form in forms(secret):
                text = text.replace(form, "***")
    return text


_own_files: dict[str, bool] = {}


def own(record: logging.LogRecord) -> bool:
    """Whether the line comes from BrainCall's own code (a file in the app folder)."""
    path = record.pathname
    if path not in _own_files:
        _own_files[path] = os.path.abspath(path).startswith(APP_DIR)
    return _own_files[path]


class Gate(logging.Filter):
    """On the handlers: lets BrainCall's own lines through, other libraries' as described above."""

    def __init__(self, level: int, secrets=()):
        super().__init__()
        self.level = level
        self.secrets: list[str] = []
        for secret in secrets:
            self.add(secret)

    def add(self, secret):
        """Also keep this secret out (e.g. the API token, generated after the log is set up)."""
        secret = str(secret or "")
        if len(secret) >= MIN_SECRET and secret not in self.secrets:
            self.secrets.append(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        if own(record):
            return True
        if record.levelno < logging.WARNING and self.level > TRACE:
            return False
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001  (the handler reports a broken message itself)
            return True
        if record.levelno < logging.WARNING and "headers" in text.lower():
            return False  # httpcore2 "receive_response_headers", the SDK's "HTTP Response: … Headers({…})"
        clean = redact(text, *self.secrets)
        if clean != text:
            record.msg, record.args = clean, None
        return True
