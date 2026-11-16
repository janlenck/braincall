"""What the log shows of other libraries (#77): only warnings, everything with trace except headers and secrets."""
import logging
import os
import sys

import pytest

import logfilter
from logfilter import TRACE, Gate

SITE = os.path.join(sys.prefix, "lib", "site-packages", "httpcore2", "_trace.py")  # any file outside the app
OWN = os.path.join(logfilter.APP_DIR, "brain.py")
KEY = "sk-ant-api03-very-secret"
HEADERS = ("receive_response_headers.complete return_value=(b'HTTP/1.1', 200, b'OK', "
           "[(b'anthropic-organization-id', b'org-1'), (b'anthropic-workspace-id', b'ws-1')])")


def record(level, msg, path=SITE, name="httpcore2.http11", args=None):
    return logging.LogRecord(name, level, path, 1, msg, args, None)


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING])
@pytest.mark.parametrize("libs", [logging.DEBUG, logging.INFO])
def test_other_libraries_only_warn_below_trace(level, libs):
    """Also with log_level debug: httpcore2 (not in any list) writes nothing below warning."""
    gate = Gate(level)
    assert not gate.filter(record(libs, "connect_tcp.started host='api.anthropic.com'"))
    assert gate.filter(record(logging.WARNING, "Retrying"))


@pytest.mark.parametrize("level", [TRACE, logging.DEBUG, logging.INFO])
def test_own_lines_always_pass(level):
    """BrainCall's own lines are already decided by the logger level."""
    assert Gate(level).filter(record(TRACE, "HA GET /api/states 200", path=OWN, name="ha"))


def test_trace_shows_other_libraries():
    assert Gate(TRACE).filter(record(logging.DEBUG, "connect_tcp.started host='api.anthropic.com' port=443"))
    assert Gate(TRACE).filter(record(logging.INFO, 'HTTP Request: POST https://api.anthropic.com/v1/messages "200"'))


@pytest.mark.parametrize("msg", [HEADERS, "HTTP Response: POST https://api.anthropic.com/v1/messages \"200 OK\" "
                                          "Headers({'anthropic-organization-id': 'org-1'})"])
def test_trace_never_shows_headers(msg):
    """The Claude API answers with its organization and workspace id."""
    assert not Gate(TRACE).filter(record(logging.DEBUG, msg))


def test_trace_hides_secrets():
    gate = Gate(TRACE, [KEY, "", None, "1234"])  # empty and short ones are no secrets
    r = record(logging.DEBUG, "Request options: %s", args=({"api_key": KEY, "pin": "1234"},))
    assert gate.filter(r)
    assert KEY not in r.getMessage() and "***" in r.getMessage() and "1234" in r.getMessage()


def test_secret_added_later():
    gate = Gate(TRACE)
    gate.add("generated-api-token")
    r = record(logging.DEBUG, 'send "Bearer generated-api-token"')
    assert gate.filter(r) and r.getMessage() == 'send "Bearer ***"'


def test_secret_in_a_warning_is_hidden_too():
    r = record(logging.WARNING, f"401 for key {KEY}")
    assert Gate(logging.INFO, [KEY]).filter(r) and KEY not in r.getMessage()


def test_through_a_real_handler(tmp_path):
    """The gate on a handler: a library logger without a level of its own, root at debug."""
    stream = tmp_path / "log.txt"
    handler = logging.FileHandler(stream, encoding="utf-8")
    handler.addFilter(Gate(logging.DEBUG))
    lib = logging.getLogger("some_renamed_library")
    lib.addHandler(handler)
    lib.setLevel(logging.DEBUG)
    try:
        lib.debug("noise")
        lib.warning("problem")
    finally:
        lib.removeHandler(handler)
        handler.close()
    assert stream.read_text(encoding="utf-8") == "problem\n"


def test_redact_forms():
    assert logfilter.redact('{"token": "a\\"b"}', 'a"b') == '{"token": "***"}'
