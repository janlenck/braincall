"""main.py: caller lookup, HTTP trigger, rejecting calls and the handling after a call.
main configures itself on import, so it is imported once with a test configuration."""
import http.client
import importlib
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyVoIP.VoIP import PhoneStatus

import config

CALLERS = [
    {"number": "*", "name": "Unknown", "access": "guest"},
    {"number": "0151111", "name": "Jan Mobile", "access": "admin"},
    {"number": "+49170222", "name": "Maryna", "access": "calendar", "language": "uk"},
    {"number": "0160333", "name": "", "access": "shopping", "notify": False},
    {"number": "0171444", "name": "Walter", "access": "read", "notify": True},
]


@pytest.fixture(scope="module")
def main(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("braincall")
    cfg_file = tmp / "config.json"
    cfg_file.write_text(json.dumps({
        "sip_user": "620", "sip_password": "x", "anthropic_api_key": "test", "openai_api_key": "test",
        "owner_name": "Jan Lenck", "log_dir": str(tmp / "logs"), "callers": CALLERS,
        "notify_services": ["notify.phone"], "api_token": "secret",
    }), encoding="utf-8")
    saved = {k: os.environ.get(k) for k in ("BRAINCALL_CONFIG", "BRAINCALL_LOG_DIR", "SUPERVISOR_TOKEN")}
    os.environ["BRAINCALL_CONFIG"] = str(cfg_file)
    os.environ.pop("BRAINCALL_LOG_DIR", None)
    os.environ.pop("SUPERVISOR_TOKEN", None)
    hooks = sys.excepthook, threading.excepthook
    try:
        module = importlib.import_module("main")
    finally:
        sys.excepthook, threading.excepthook = hooks
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return module


@pytest.fixture
def events(main, monkeypatch):
    """Everything main sends to Home Assistant, instead of sending it."""
    sent = {"events": [], "notify": []}
    monkeypatch.setattr(main.ha, "fire_event", lambda name, data: sent["events"].append((name, data)))
    monkeypatch.setattr(main.ha, "notify", lambda service, title, text: sent["notify"].append((service, title, text)))
    return sent


# ---------------------------------------------------------------- callers
@pytest.mark.parametrize("number, name", [
    ("0151111", "Jan Mobile"),
    ("+49151111", "Jan Mobile"),   # exact entry wins over "*" even though "*" comes first
    ("0170222", "Maryna"),
    ("0999", "Unknown"),
])
def test_find_caller(main, number, name):
    assert main.find_caller(number)["name"] == name


def test_find_caller_without_wildcard(main, monkeypatch):
    assert main.find_caller("0999", wildcard=False) is None
    monkeypatch.setitem(main.cfg, "callers", [c for c in main.cfg.callers if c["number"] != "*"])
    assert main.find_caller("0999") is None


def test_same_number_never_matches_empty(main):
    assert not main.same_number("", "")
    assert not main.same_number("anonymous", "")


def test_caller_name_and_language(main):
    assert main.caller_name(main.find_caller("0999")) == ""      # unknown: will be asked for the name
    assert main.caller_name(main.find_caller("0160333")) == "Jan Lenck"  # listed without name
    assert main.caller_name(None) == ""
    assert main.lang_for(main.find_caller("0170222")) == "uk"
    assert main.lang_for(main.find_caller("0151111")) == "de"
    assert main.lang_for(None) == "de"


# ---------------------------------------------------------------- rejecting incoming calls
def incoming(number):
    denied = []
    call = SimpleNamespace(request=SimpleNamespace(headers={"From": {"number": number}}),
                           deny=lambda: denied.append(True))
    return call, denied


@pytest.mark.parametrize("number, reason", [("", "withheld number"), ("anonymous", "withheld number")])
def test_withheld_numbers_are_rejected(main, events, number, reason):
    call, denied = incoming(number)
    main.on_incoming(call)
    assert denied == [True]
    (name, data), = events["events"]
    assert name == "braincall_call" and data["answered"] is False and data["reason"] == f"rejected: {reason}"


def test_unlisted_number_rejected_without_wildcard(main, events, monkeypatch):
    monkeypatch.setitem(main.cfg, "callers", [c for c in main.cfg.callers if c["number"] != "*"])
    call, denied = incoming("0999")
    main.on_incoming(call)
    assert denied == [True]
    assert events["events"][0][1]["reason"] == "rejected: not in callers"


def test_incoming_while_busy_is_denied(main, events):
    call, denied = incoming("0151111")
    assert main.busy.acquire(blocking=False)
    try:
        main.on_incoming(call)
    finally:
        main.busy.release()
    assert denied == [True]


# ---------------------------------------------------------------- after the call
def session(*transcript, actions=()):
    return SimpleNamespace(transcript=list(transcript), caller_turns=len(transcript), actions=list(actions))


@pytest.fixture
def after(main, events, monkeypatch):
    monkeypatch.setattr(main.brain, "summarize", lambda transcript, caller, actions, lang: f"Summary of {caller}")

    def run(number, inbound=True, s=None):
        s = s or session("caller: Hallo", "Claudine: Hallo!")
        main.after_call(True, number, 0.0, 30.0, s, "test", main.find_caller(number), inbound)
        return events
    return run


def test_after_call_guest_is_notified_and_noted(main, after):
    notes = Path(main._LOGDIR) / "call-notes.txt"
    notes.unlink(missing_ok=True)
    sent = after("0999")
    assert sent["notify"] == [("notify.phone", "Anruf von 0999", "Summary of 0999")]
    data = sent["events"][0][1]
    assert (data["summary"], data["duration_s"], data["access"]) == ("Summary of 0999", 30, "guest")
    text = notes.read_text(encoding="utf-8")
    assert "from 0999" in text and "caller: Hallo" in text and "Summary: Summary of 0999" in text


def test_after_call_admin_is_not_notified(after):
    sent = after("0151111")
    assert sent["notify"] == []
    assert sent["events"][0][1]["summary"] == "Summary of Jan Mobile (0151111)"


def test_after_call_notify_override_per_caller(after):
    assert after("0160333")["notify"] == []       # notify: false
    assert len(after("0171444")["notify"]) == 1   # read access, notify: true


def test_after_call_outbound_is_not_notified(main, after):
    notes = Path(main._LOGDIR) / "call-notes.txt"
    notes.unlink(missing_ok=True)
    assert after("0999", inbound=False)["notify"] == []
    assert not notes.exists()


def test_after_call_without_caller_speech_has_no_summary(after):
    sent = after("0999", s=session())
    assert sent["notify"] == [] and sent["events"][0][1]["summary"] == ""


def test_after_call_summary_failure_falls_back_to_transcript(main, after, monkeypatch):
    def fail(*a):
        raise RuntimeError("API down")
    monkeypatch.setattr(main.brain, "summarize", fail)
    sent = after("0999")
    assert sent["notify"][0][2] == "caller: Hallo\nClaudine: Hallo!"


# ---------------------------------------------------------------- HTTP trigger
@pytest.fixture
def server(main, monkeypatch):
    dialed = []

    def outbound(number, message):
        dialed.append((number, message))
        main.busy.release()

    monkeypatch.setattr(main, "outbound", outbound)
    monkeypatch.setattr(main, "sip_status", lambda: PhoneStatus.REGISTERED)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def request(method, path, body=None, token="secret", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        h = {"Authorization": f"Bearer {token}"} if token else {}
        h.update(headers or {})
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        conn.close()
        return out

    request.dialed = dialed
    yield request
    srv.shutdown()
    srv.server_close()


def wait_dialed(server):
    for _ in range(50):
        if server.dialed:
            return server.dialed
        threading.Event().wait(0.02)
    return server.dialed


def test_health(server):
    status, body = server("GET", "/health", token=None)
    assert status == 200 and body["sip"] == PhoneStatus.REGISTERED.value and body["busy"] is False


def test_call_starts_outbound(server):
    status, body = server("POST", "/call", {"number": "0170222", "message": "Termin?"})
    assert (status, body) == (202, {"status": "dialing", "number": "0170222"})
    assert wait_dialed(server) == [("0170222", "Termin?")]


@pytest.mark.parametrize("token", [None, "wrong", "secretX"])
def test_call_needs_the_token(server, token):
    assert server("POST", "/call", {"number": "0151111"}, token=token)[0] == 401
    assert server.dialed == []


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"text"'])
def test_call_invalid_body(server, body):
    assert server("POST", "/call", body) == (400, {"error": "invalid json"})


def test_call_body_too_large(server):
    assert server("POST", "/call", b"{}", headers={"Content-Length": str(64 * 1024 + 1)})[0] == 413


def test_call_without_number(server, main, monkeypatch):
    monkeypatch.setitem(main.cfg, "default_number", "")
    assert server("POST", "/call", {"message": "x"}) == (400, {"error": "no number"})


def test_call_uses_default_number(server, main, monkeypatch):
    monkeypatch.setitem(main.cfg, "default_number", "0151111")
    assert server("POST", "/call", {})[0] == 202
    assert wait_dialed(server) == [("0151111", "")]


def test_call_when_not_registered(server, main, monkeypatch):
    monkeypatch.setattr(main, "sip_status", lambda: PhoneStatus.FAILED)
    assert server("POST", "/call", {"number": "0151111"})[0] == 503


def test_call_when_line_busy(server, main):
    assert main.busy.acquire(blocking=False)
    try:
        assert server("POST", "/call", {"number": "0151111"}) == (409, {"error": "line busy"})
    finally:
        main.busy.release()


def test_unknown_paths(server):
    assert server("GET", "/call")[0] == 404
    assert server("POST", "/health")[0] == 404


def test_config_was_loaded_from_the_test_file(main):
    assert main.cfg.sip_user == "620"
    assert config.CONFIG_FILE[0].endswith("config.json")
