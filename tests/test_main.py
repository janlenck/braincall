"""main.py: caller lookup, HTTP trigger, rejecting calls and the handling after a call.
main configures itself on import, so it is imported once with a test configuration."""
import http.client
import importlib
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyVoIP.VoIP import PhoneStatus

import config
from conftest import CONFIG_ENV

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
        "outbound_allowed_regex": "",  # the tests use short made-up numbers
    }), encoding="utf-8")
    saved = {k: os.environ.pop(k, None) for k in CONFIG_ENV}  # e.g. BRAINCALL_API_TOKEN would beat "secret"
    os.environ["BRAINCALL_CONFIG"] = str(cfg_file)
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
    sent = {"events": [], "notify": [], "state": []}

    def fire(name, data):  # braincall_state (#100) comes from its own thread, on every change of the line: apart
        sent["state" if name == "braincall_state" else "events"].append(data if name == "braincall_state"
                                                                         else (name, data))
    monkeypatch.setattr(main.ha, "fire_event", fire)
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


def busy_rejected(main, number):
    call, denied = incoming(number)
    assert main.busy.acquire(blocking=False)
    try:
        main.on_incoming(call)
    finally:
        main.busy.release()
    assert denied == [True]


def test_incoming_while_busy_is_logged_and_reported(main, events, caplog):
    """#25: a second call during a running call shows up in the log and as a braincall_call event."""
    with caplog.at_level(logging.WARNING, logger="main"):
        busy_rejected(main, "0999")
    assert any(r.getMessage() == "Incoming call from 0999 (unknown) rejected (line busy)" for r in caplog.records)
    (name, data), = events["events"]
    assert name == "braincall_call" and data["answered"] is False and data["reason"] == "rejected: line busy"
    assert data["access"] == "guest"
    assert events["notify"] == [("notify.phone", "Verpasster Anruf von 0999",
                                 "Die Leitung war besetzt (ein anderes Gespräch lief), der Anruf wurde abgewiesen.")]


def test_incoming_while_busy_notifies_like_a_summary(main, events):
    busy_rejected(main, "0151111")  # admin: no notification
    busy_rejected(main, "0160333")  # notify: false
    assert events["notify"] == []
    busy_rejected(main, "0171444")  # read access, notify: true
    assert events["notify"][0][1] == "Verpasster Anruf von Walter (0171444)"
    assert [e[1]["caller_name"] for e in events["events"]] == ["Jan Mobile", "Jan Lenck", "Walter"]


# ---------------------------------------------------------------- after the call
def session(*transcript, actions=(), mailbox=False):
    return SimpleNamespace(transcript=list(transcript), caller_turns=len(transcript), actions=list(actions),
                           mailbox=mailbox, message_left=False, unanswered=False)


@pytest.fixture
def after(main, events, monkeypatch):
    monkeypatch.setattr(main.brain, "summarize",
                        lambda transcript, caller, actions, lang, **kw: events.update(summarized=kw)
                        or f"Summary of {caller}")

    def run(number, inbound=True, s=None):
        s = s or session("caller: Hallo", "Claudine: Hallo!")
        ended = time.time()
        main.after_call(True, number, ended - 30, ended, s, "test", main.find_caller(number), inbound)
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
    def fail(*a, **kw):
        raise RuntimeError("API down")
    monkeypatch.setattr(main.brain, "summarize", fail)
    sent = after("0999")
    assert sent["notify"][0][2] == "caller: Hallo\nClaudine: Hallo!"


def test_event_reason_says_the_call_limit_ended_the_call(main, after, monkeypatch):
    """#22: max_call_minutes ended the call."""
    s = session("caller: Hallo", "Claudine: Hallo!")
    s.limit_reached = True
    assert after("0999", s=s)["events"][0][1]["reason"] == "test; ended: call limit"
    events = []
    monkeypatch.setattr(main.ha, "fire_event", lambda name, data: events.append(data))
    main.report(True, "0999", time.time(), s)
    assert events[0]["reason"] == "ended: call limit"


def test_summary_is_told_the_direction_and_a_mailbox(after):
    assert after("0999")["summarized"] == {"outbound": False, "mailbox": False, "unanswered": False}
    s = session("0999 (mailbox): Bitte sprechen Sie nach dem Ton.", "Claudine: Hallo …", mailbox=True)
    assert after("0999", inbound=False, s=s)["summarized"] == {"outbound": True, "mailbox": True, "unanswered": False}


@pytest.fixture
def remembered(main, monkeypatch, tmp_path):
    import memory
    calls = memory.CallMemory(tmp_path / "calls.jsonl", 30, main.cfg.country_code)
    monkeypatch.setattr(main, "calls", calls)
    return calls


def test_memory_is_off_by_default(main, after):
    assert not main.calls.enabled
    after("0999")
    assert not (Path(main._LOGDIR) / "calls.jsonl").exists()
    assert "no memory of earlier calls or conversations" in main.previous("0999", main.GUEST)


def test_after_call_remembers_summary_for_next_call(main, after, remembered):
    after("+49170222")
    after("0999", inbound=False)
    note = main.previous("0170222", frozenset({"calendar"}))
    assert "(phone, they called): Summary of Maryna (+49170222)" in note
    assert "You remember earlier calls and conversations with this caller" in note
    assert "none for this caller" in main.previous("0160333", main.GUEST)
    assert "(phone, you called them): Summary of 0999" in main.previous("0999", main.GUEST)


# ---------------------------------------------------------------- one memory on the phone and in Brain Assist (#50)
PEOPLE = [
    {"number": "0151111", "ha_user": "Jan", "name": "Jan", "access": "admin"},
    {"number": "0170222", "ha_user": "maryna-id", "name": "Maryna", "access": "calendar", "notify": True},
    {"ha_device": "Küche", "access": "read"},
    {"ha_user": "*", "access": "guest"},
]


@pytest.fixture
def people(main, monkeypatch, remembered, after):
    """Callers for phone and Assist, the memory on, summaries faked ("Summary of <who>")."""
    monkeypatch.setitem(main.cfg, "callers", config._callers(PEOPLE))
    return main


def conversation(main, text="Wie warm ist es?", channel="text", **who):
    """An Assist conversation that is over, as assist.Conversations hands it to on_close."""
    req = main.assist.parse({"text": text, "channel": channel, **who})
    conv = main.assist.Conversation("c1", "key")
    conv.person = main.assist.identify(main.cfg.callers, req)
    conv.channel, conv.language, conv.label = channel, "de", main.assist.label(conv.person, req)
    conv.device = req["device_name"] or req["area_name"]
    conv.transcript = [f"{conv.label}: {text}", "Claudine: 21 Grad."]
    conv.ended = time.time()
    return conv


def test_a_phone_call_is_remembered_in_assist(people, after):
    after("0151111")
    note = people.recall(people.assist.identify(people.cfg.callers, people.assist.parse(
        {"text": "x", "user_name": "jan"})))
    assert "(phone, they called): Summary of Jan (0151111)" in note and "with this person" in note


def test_an_assist_conversation_is_remembered_on_the_phone(people, events):
    people.after_assist(conversation(people, "Stell die Heizung auf 21 Grad", user_name="Jan",
                                     device_name="Tablet", area_name="Flur"))
    note = people.previous("0151111", frozenset({"admin"}), people.find_caller("0151111"))
    assert "(Home Assistant chat, device “Tablet”): Summary of Jan via Tablet" in note
    assert events["summarized"] == {"channel": "text"}
    line = json.loads(people.calls.path.read_text(encoding="utf-8").splitlines()[-1])
    assert (line["number"], line["ha_user"], line["channel"], line["device"]) == (
        "0151111", "jan", "assist-text", "Tablet")
    assert "direction" not in line
    assert events["notify"] == []


def test_assist_conversations_are_never_announced(people, events):
    """Notifications are for calls only, also for an entry with notify: true (#50)."""
    people.after_assist(conversation(people, "Hat Jan Freitag Zeit?", channel="voice", user_id="maryna-id",
                                     user_name="Maryna K"))
    assert events["summarized"] == {"channel": "voice"}
    assert events["notify"] == []


def test_a_voice_device_has_its_own_memory(people, after):
    after("0151111")
    kitchen = {"device_name": "Satellite", "area_name": "Küche"}
    people.after_assist(conversation(people, "Wie warm ist es draußen?", channel="voice", **kitchen))
    person = people.assist.identify(people.cfg.callers, people.assist.parse({"text": "x", **kitchen}))
    note = people.recall(person)
    assert "(Home Assistant, spoken, device “Satellite”)" in note and "the people who used this device" in note
    assert "Summary of someone via Satellite" in note
    assert "Jan (0151111)" not in note  # nothing of a person, whoever spoke there
    assert "Satellite" not in people.previous("0151111", frozenset({"admin"}), people.find_caller("0151111"))


def test_everyone_else_is_remembered_by_their_own_id(people):
    for user, name in (("u1", "Tom"), ("u2", "Eva")):
        people.after_assist(conversation(people, "Hallo", user_id=user, user_name=name))
    note = people.recall(people.assist.identify(people.cfg.callers, people.assist.parse(
        {"text": "x", "user_id": "u2", "user_name": "Eva"})))
    assert "Summary of Eva" in note and "Tom" not in note


def test_closed_conversation_is_summarized_in_the_background(people, monkeypatch):
    done = []
    monkeypatch.setattr(people, "after_assist", done.append)
    conv = conversation(people, user_name="Jan")
    people.assist_closed(conv)
    for _ in range(100):
        if done:
            break
        time.sleep(0.01)
    assert done == [conv]


def test_nothing_is_summarized_without_memory(main, monkeypatch):
    """No request to Claude for a summary nobody would use: without the memory, not even for notify: true."""
    monkeypatch.setitem(main.cfg, "callers", config._callers(PEOPLE))
    monkeypatch.setattr(main, "after_assist", lambda conv: pytest.fail("must not be summarized"))
    assert not main.calls.enabled
    main.assist_closed(conversation(main, user_name="Jan"))            # memory off, no notify
    main.assist_closed(conversation(main, user_id="maryna-id"))       # memory off, notify: true
    time.sleep(0.05)


def test_memory_can_be_forgotten_per_person(server, people, after):
    after("0151111")
    after("0170222")
    people.after_assist(conversation(people, user_name="Jan"))
    assert server("DELETE", "/api/memory", token=None)[0] == 401
    # a call from before 1.12: stored under the number only
    with open(people.calls.path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "number": "0151111",
                            "direction": "in", "access": "admin", "summary": "Old call"}) + "\n")
    assert server("DELETE", "/api/memory?ha_user=Jan") == (200, {"forgotten": 3})  # the calls and the chat
    assert server("DELETE", "/api/memory?ha_user=Jan") == (200, {"forgotten": 0})
    people.after_assist(conversation(people, user_id="maryna-id"))  # stored under her user and number …
    with open(people.calls.path, "a", encoding="utf-8") as f:       # … and one under the user only
        f.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "ha_user": "maryna-id",
                            "channel": "assist-text", "access": "calendar", "summary": "Chat"}) + "\n")
    assert server("DELETE", "/api/memory?number=%2B49170222") == (200, {"forgotten": 3})
    assert people.calls.path.read_text(encoding="utf-8") == ""
    assert server("DELETE", "/api/memory")[0] == 400
    assert "none for this caller" in people.previous("0151111", frozenset({"admin"}), people.find_caller("0151111"))


def test_memory_is_ordered_by_call_end_not_save_time(main, remembered, events, monkeypatch):
    monkeypatch.setattr(main.brain, "summarize", lambda transcript, caller, actions, lang, **kw: transcript[0])
    now = time.time()
    for text, ended in (("caller: later call", now - 60), ("caller: earlier call", now - 3600)):
        s = session(text)
        main.after_call(True, "0999", ended - 30, ended, s, "test", main.find_caller("0999"), True)
    note = main.previous("0999", main.GUEST)
    assert note.index("earlier call") < note.index("later call")


def test_after_call_without_summary_is_not_remembered(main, after, remembered, monkeypatch):
    def fail(*a, **kw):
        raise RuntimeError("API down")
    monkeypatch.setattr(main.brain, "summarize", fail)
    after("0999")
    after("0160333", s=session())
    assert not remembered.path.exists()


def test_memory_failure_does_not_stop_the_event(main, after, remembered, monkeypatch):
    def broken(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(remembered, "add", broken)
    monkeypatch.setattr(remembered, "recent", broken)
    assert after("0999")["events"][0][1]["summary"] == "Summary of 0999"
    assert main.previous("0999", main.GUEST) == ""


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
    request.port = srv.server_address[1]
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
    assert body["status"] == "ok" and body["problems"] == []
    assert body["line"] == "idle" and "call" not in body  # no number or name without the token (#100)


class _Thread:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive


def test_health_503_when_the_sip_watchdog_hangs_or_stopped(server, main, monkeypatch):
    """#37: the Supervisor restarts the app on a 503; a hanging or dead SIP watchdog loop is one."""
    monkeypatch.setattr(main, "sip_watchdog", _Thread())
    monkeypatch.setattr(main, "watchdog_seen", time.monotonic() - 40)
    assert server("GET", "/health", token=None)[0] == 200
    monkeypatch.setattr(main, "watchdog_seen", time.monotonic() - main.WATCHDOG_STALL - 5)
    status, body = server("GET", "/health", token=None)
    assert status == 503 and body["status"] == "unhealthy" and body["problems"][0].startswith("SIP watchdog stuck")
    assert body["sip"] == PhoneStatus.REGISTERED.value  # the status fields are still there
    monkeypatch.setattr(main, "watchdog_seen", time.monotonic())
    monkeypatch.setattr(main, "sip_watchdog", _Thread(alive=False))
    assert server("GET", "/health", token=None)[1]["problems"] == ["SIP watchdog stopped"]


def test_health_ok_while_sip_is_not_registered(server, main, monkeypatch):
    """The watchdog reconnects on its own; a restart would not help while the FRITZ!Box is down."""
    monkeypatch.setattr(main, "sip_watchdog", _Thread())
    monkeypatch.setattr(main, "watchdog_seen", time.monotonic())
    monkeypatch.setattr(main, "sip_status", lambda: PhoneStatus.FAILED)
    status, body = server("GET", "/health", token=None)
    assert status == 200 and body["sip"] == PhoneStatus.FAILED.value


def test_the_watchdog_loop_notes_every_round(main, monkeypatch):
    class Twice:
        rounds = 0

        def wait(self, timeout):
            self.rounds += 1
            return self.rounds > 2

        def is_set(self):
            return False
    monkeypatch.setattr(main, "stopping", Twice())
    monkeypatch.setattr(main, "sip_status", lambda: PhoneStatus.REGISTERED)
    monkeypatch.setattr(main, "watchdog_seen", 0.0)
    main.watchdog()
    assert time.monotonic() - main.watchdog_seen < 5


@pytest.fixture
def ingress(main):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), main.IngressHandler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def get(path):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        conn.request("GET", path)
        r = conn.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        conn.close()
        return out
    yield get
    srv.shutdown()
    srv.server_close()


def test_watchdog_health_on_the_ingress_port_asks_the_api(main, server, ingress, monkeypatch):
    """#37: the Supervisor asks [HOST]:8098 (the ingress port on its network), whatever http_port is and also when
    the API listens on 127.0.0.1 only; the answer is the API's own /health, from any client (no token)."""
    monkeypatch.setattr(main, "INGRESS_PROXY", "172.30.32.2")  # the test client is not the ingress proxy
    monkeypatch.setitem(main.cfg, "http_port", server.port)
    monkeypatch.setattr(main, "api_up", threading.Event())
    main.api_up.set()
    status, body = ingress("/health")
    assert status == 200 and body["status"] == "ok" and body["sip"] == PhoneStatus.REGISTERED.value
    monkeypatch.setattr(main, "sip_watchdog", _Thread(alive=False))
    status, body = ingress("/health")
    assert status == 503 and body["problems"] == ["SIP watchdog stopped"]


def test_watchdog_health_503_when_the_api_does_not_answer(main, ingress, monkeypatch):
    import socket
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setitem(main.cfg, "http_port", port)
    monkeypatch.setattr(main, "api_up", threading.Event())
    main.api_up.set()
    status, body = ingress("/health")
    assert status == 503 and body["problems"][0].startswith(f"HTTP API not answering on port {port}")


def test_watchdog_health_while_starting(main, ingress, monkeypatch):
    """SIP is set up before the API listens, which may take minutes while the FRITZ!Box is down: no restart then,
    but one if the API never comes up."""
    monkeypatch.setattr(main, "api_up", threading.Event())
    monkeypatch.setattr(main, "started", time.monotonic() - 30)
    assert ingress("/health") == (200, {"status": "starting", "problems": []})
    monkeypatch.setattr(main, "started", time.monotonic() - main.STARTUP_GRACE - 5)
    status, body = ingress("/health")
    assert status == 503 and body["problems"][0].startswith("HTTP API not started after")


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


# ---------------------------------------------------------------- token for everything, Brain Assist (#50)
@pytest.mark.parametrize("method, path", [
    ("POST", "/call"), ("GET", "/tasks"), ("POST", "/tasks"), ("DELETE", "/tasks/abc"), ("GET", "/api/tasks"),
    ("POST", "/api/tasks"), ("DELETE", "/api/tasks/abc"), ("GET", "/api/version"), ("POST", "/api/converse"),
    ("GET", "/api/status"),
])
@pytest.mark.parametrize("token", [None, "wrong"])
def test_every_endpoint_needs_the_token(server, method, path, token):
    """Also from 127.0.0.1: other apps on the host reach the port too."""
    assert server(method, path, {"text": "Licht an", "number": "0151111"}, token=token)[0] == 401
    assert server.dialed == []


def test_only_health_is_open(server, main):
    status, body = server("GET", "/health", token=None)
    assert status == 200 and body["phone"] is True


def test_start_line_names_the_version(main):
    """The log says which version runs, from the first line of every start."""
    line = main.start_line()
    assert line.startswith(f"BrainCall {config.version()} starting, Python 3.")
    assert config.version().count(".") == 2


def test_start_line_also_with_log_level_warning(main, monkeypatch):
    """A start that stops on an incomplete configuration still says which version ran, also at warning."""
    import logging
    seen = []
    handler = logging.Handler()
    handler.emit = seen.append
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.WARNING)
    root.addHandler(handler)
    monkeypatch.setattr(main, "incomplete", lambda: ["anthropic_api_key"])
    try:
        with pytest.raises(SystemExit):
            main.main()
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
    assert [(r.levelname, r.getMessage().split(",")[0]) for r in seen] == [
        ("INFO", f"BrainCall {config.version()} starting"),
        ("ERROR", "Configuration incomplete: anthropic_api_key missing (config.json)")]


def test_version(server, main):
    status, body = server("GET", "/api/version")
    assert status == 200
    assert body == {"api": 1, "version": config.version(), "phone": True, "assistant": "Claudine",
                    "home_assistant": False, "tasks": True, "line": True}
    assert body["version"].count(".") == 2


@pytest.mark.parametrize("method, path, token, level", [
    ("GET", "/api/tasks", "secret", 5),        # a poll: trace
    ("GET", "/health", None, 5),
    ("GET", "/api/version", "secret", 5),
    ("POST", "/api/tasks", "secret", 10),      # changes something (or is refused): debug
    ("DELETE", "/api/tasks/abc", "secret", 10),
    ("GET", "/api/tasks", "wrong", 10),        # refused: debug
    ("GET", "/api/nothing", "secret", 10),
])
def test_polls_are_logged_at_trace(server, main, caplog, method, path, token, level):
    """#77: a GET that worked would fill the debug log once a minute."""
    with caplog.at_level(5, logger="main"):
        status = server(method, path, {"task": "x"}, token=token)[0]
    lines = [r for r in caplog.records if r.getMessage().startswith(f'HTTP "{method} {path} ')]
    assert len(lines) == 1, [r.getMessage() for r in caplog.records]
    assert lines[0].levelno == level
    assert lines[0].getMessage().endswith(f'" {status} -')


def test_tasks_are_also_under_api(server, main):
    assert server("GET", "/api/tasks") == server("GET", "/tasks")
    assert server("GET", "/api/tasks")[0] == 200
    assert server("GET", "/api/nothing")[0] == 404


def test_call_without_a_phone(server, main, monkeypatch):
    monkeypatch.setattr(main, "PHONE", False)
    status, body = server("POST", "/call", {"number": "0151111"})
    assert status == 503 and "no phone" in body["error"]
    assert server("GET", "/health", token=None)[1]["phone"] is False


def test_start_needs_the_phone_settings_only_for_the_phone(main, monkeypatch):
    assert main.incomplete() == []
    monkeypatch.setitem(main.cfg, "openai_api_key", "")
    assert main.incomplete() == ["openai_api_key"]
    monkeypatch.setitem(main.cfg, "sip_password", "")
    assert main.incomplete() == ["sip_password", "openai_api_key"]  # half a SIP account is a mistake
    monkeypatch.setitem(main.cfg, "sip_user", "")
    assert main.incomplete() == []  # no phone: Brain Assist only
    monkeypatch.setitem(main.cfg, "anthropic_api_key", "")
    assert main.incomplete() == ["anthropic_api_key"]


def converse(server, body, token="secret"):
    """POST /api/converse: the status and the events of the stream."""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("POST", "/api/converse", body=json.dumps(body).encode(),
                 headers={"Authorization": f"Bearer {token}"})
    r = conn.getresponse()
    raw = r.read().decode()
    conn.close()
    return r.status, r.getheader("Content-Type"), [json.loads(line) for line in raw.splitlines() if line]


def test_converse_streams_the_answer(server, main, monkeypatch):
    monkeypatch.setitem(main.cfg, "callers", config._callers([{"ha_user": "Jan", "name": "Jan", "access": "admin"}]))
    seen = {}

    def turn(messages, on_text, aborted, **kw):
        seen.update(kw, messages=list(messages))
        for piece in ("Es sind ", "**21 Grad**."):
            on_text(piece)
        return False

    monkeypatch.setattr(main.brain, "turn", turn)
    status, kind, events = converse(server, {"text": "Wie warm ist es?", "user_name": "jan", "language": "de"})
    assert status == 200 and kind == "application/x-ndjson; charset=utf-8"
    assert [e["type"] for e in events] == ["start", "text", "text", "end"]
    assert "".join(e["text"] for e in events if e["type"] == "text") == "Es sind **21 Grad**."
    assert events[0]["access"] == "admin" and events[-1]["continue_conversation"] is False
    assert seen["channel"] == "text" and seen["access"] == {"admin"} and seen["language"] == "de"
    assert seen["messages"][0]["content"].endswith("\nWie warm ist es?")
    assert len(main.conversations) == 1


def test_converse_refuses_unlisted_users_without_claude(server, main, monkeypatch):
    monkeypatch.setattr(main.brain, "turn", lambda *a, **kw: pytest.fail("Claude must not be asked"))
    status, _, events = converse(server, {"text": "Licht an", "user_name": "Fremder", "channel": "voice"})
    assert status == 200 and [e["type"] for e in events] == ["start", "text", "end"]
    assert "nicht freigeschaltet" in events[1]["text"]


def test_converse_failure_ends_the_stream_with_the_reason(server, main, monkeypatch):
    def boom(*a, **kw):
        raise KeyError("callers")

    monkeypatch.setattr(main.assist, "converse", boom)
    status, _, events = converse(server, {"text": "Licht an"})
    assert status == 200 and events == [{"type": "error", "message": "KeyError: 'callers'"}]


@pytest.mark.parametrize("body, error", [({}, "no text"), ({"text": "x", "channel": "fax"}, "channel")])
def test_converse_invalid_request(server, body, error):
    status, body = server("POST", "/api/converse", body)
    assert status == 400 and error in body["error"]


def test_config_was_loaded_from_the_test_file(main):
    assert main.cfg.sip_user == "620"
    assert config.CONFIG_FILE[0].endswith("config.json")


def test_web_setting_per_caller(main):
    """Unknown callers ("*") never get the web tools; listed callers by access unless "web" says else."""
    assert main.web_for({"number": "*", "access": frozenset({"read"}), "web": True}) is False
    assert main.web_for(None) is False
    assert main.web_for(main.find_caller("0171444")) is None  # Walter: by access (read)
    assert main.web_for({"number": "0151", "web": False}) is False


# ---------------------------------------------------------------- planned calls with a task (#51)
@pytest.fixture
def planned(main, events, monkeypatch, tmp_path):
    """A fresh task store, a registered phone, and outbound() replaced: records calls, returns the next session."""
    import contacts
    import tasks
    # contacts made for tasks (#92) go to a file of this test; the live list is a copy, so other tests keep theirs
    monkeypatch.setitem(main.cfg, "callers", list(main.cfg.callers))
    book = contacts.Contacts(main.cfg, tmp_path / "contacts.json", in_use=main.contact_in_use)
    book.items = main.contact_store.all()
    monkeypatch.setattr(main, "contact_store", book)
    monkeypatch.setattr(main.brain, "contacts", book)
    store = tasks.TaskStore(tmp_path / "tasks.json", main.cfg)
    store.on_change = main.announce_task
    monkeypatch.setattr(main, "task_store", store)
    monkeypatch.setattr(main.brain, "tasks", store)
    monkeypatch.setattr(main, "sip_status", lambda: PhoneStatus.REGISTERED)
    monkeypatch.setitem(main.cfg, "outbound_hours", "")
    ctx = SimpleNamespace(store=store, dialed=[], sessions=[], events=events, followed=[])

    def outbound(number, message, task=None):
        ctx.dialed.append((number, message, task))
        main.busy.release()
        return ctx.sessions.pop(0) if ctx.sessions else None

    def follow_up(task, transcript, actions=None, mailbox=False):
        ctx.followed.append(task["id"])
        ctx.mailbox = mailbox
        return ctx.result

    def add(**kw):
        task, error = tasks.build(main.cfg, {"number": "04542 1234", "name": "Seeblick", "task": "Tisch reservieren",
                                             **kw}, {"number": "0151111", "name": "Jan Mobile"},
                                  main.datetime.now().astimezone())
        assert error == "", error
        return store.add(task)

    ctx.add = add
    ctx.result = {"status": "done", "summary": "Tisch um 19 Uhr reserviert.", "answers": {"Uhrzeit": "19:00"}}
    monkeypatch.setattr(main, "outbound", outbound)
    monkeypatch.setattr(main.brain, "follow_up", follow_up)
    return ctx


def talked(*lines, mailbox=False, message_left=False, unanswered=False):
    """The session of a call in which the other side said something; mailbox / message_left / unanswered: what
    the phone system recognized (CallSession.mailbox, .message_left, .unanswered)."""
    return SimpleNamespace(transcript=list(lines or ("Seeblick: Ja, 19 Uhr geht.",)), caller_turns=1, actions=[],
                           mailbox=mailbox, message_left=message_left, unanswered=unanswered)


def test_due_task_is_called_followed_up_and_reported(main, planned):
    task = planned.add()
    planned.sessions.append(talked())
    main.run_tasks()
    (number, message, passed), = planned.dialed
    assert number == "045421234" and "AI assistant" in message and passed["id"] == task["id"]
    assert planned.followed == [task["id"]] and planned.store.get(task["id"])["status"] == "done"
    assert planned.events["notify"] == [("notify.phone", "Auftrag Anruf bei Seeblick: erledigt",
                                         "Tisch um 19 Uhr reserviert.\nAnswers: Uhrzeit: 19:00")]
    name, data = planned.events["events"][-1]
    assert name == "braincall_task_finished" and data["status"] == "done" and data["answers"] == {"Uhrzeit": "19:00"}
    assert not main.busy.locked()


def test_a_call_that_reached_someone_becomes_a_step_of_the_task(main, planned):
    """#92: the history keeps the call (direction, number, transcript, result); a try without an answer does not."""
    task = planned.add(retries=1)
    main.run_tasks()  # nobody answers
    planned.sessions.append(talked("Seeblick: Ja, 19 Uhr geht.", "Claudine: Danke!"))
    planned.store.claim(main.datetime.now().astimezone() + timedelta(hours=1))
    main.settle_task(planned.store.get(task["id"]), planned.sessions.pop())
    (step,) = planned.store.get(task["id"])["steps"]
    assert (step["kind"], step["number"], step["outcome"]) == ("call_out", "045421234", "talked")
    assert step["transcript"] == "Seeblick: Ja, 19 Uhr geht.\nClaudine: Danke!"
    assert step["result"] == "Tisch um 19 Uhr reserviert."


def test_an_open_task_keeps_its_contact_by_id(main, planned):
    """#92: the contact of an open task cannot be deleted, found by its id even if its numbers changed."""
    saved, _ = main.contact_store.save({"name": "Seeblick", "numbers": [{"number": "04542 1234"}]}, origin="task")
    task = planned.add()
    assert task["contact_id"] == saved["id"]
    moved = {**saved, "numbers": [{"number": "04542 9999"}]}
    assert main.contact_in_use(moved) == task["goal"]
    planned.store.cancel(task["id"])
    assert main.contact_in_use(moved) == ""


def next_call(when, summary="Schaut nach, ob für 4 frei ist."):
    return {"status": "next_call", "summary": summary, "answers": {}, "steps": [], "open_points": [],
            "do_not_call": False, "next_call": {"number": "045421234", "not_before": when, "until": None,
                                                "added": ""}}


def test_the_evaluation_plans_the_next_call_of_the_task(main, planned):
    """#92: the callee asks to be called again; the task stays open, the second call ends it with one report."""
    task = planned.add(retries=1, call_back=True)
    later = main.datetime.now().astimezone() + timedelta(hours=1)
    planned.sessions.append(talked("Seeblick: Rufen Sie in einer Stunde nochmal an."))
    planned.result = next_call(later)
    main.run_tasks()
    t = planned.store.get(task["id"])
    assert t["status"] == "planned" and t["tries"] == 0 and t["next_try"] == later.isoformat(timespec="minutes")
    assert [s["result"] for s in t["steps"]] == ["Schaut nach, ob für 4 frei ist."]
    assert planned.events["notify"] == []  # not over yet: nothing reported
    main.run_tasks()
    assert len(planned.dialed) == 1  # not due yet
    planned.store.tasks[0]["next_try"] = "2026-01-01T00:00+01:00"
    planned.sessions.append(talked("Seeblick: Ja, 19 Uhr geht."))
    planned.result = {"status": "done", "summary": "Tisch um 19 Uhr reserviert."}
    main.run_tasks()
    second = planned.dialed[1][1]  # the second call knows the first
    assert "already had these steps" in second and "Schaut nach, ob für 4 frei ist." in second
    done = planned.store.get(task["id"])
    assert done["status"] == "done" and len(done["steps"]) == 2
    assert [d[0] for d in planned.dialed] == ["045421234", "045421234", "0151111"]
    assert len(planned.events["notify"]) == 1


def test_a_next_call_that_reaches_nobody_ends_the_task_partial(main, planned):
    task = planned.add(retries=0)
    planned.sessions.append(talked("Seeblick: Rufen Sie später nochmal an."))
    planned.result = next_call(main.datetime.now().astimezone() - timedelta(minutes=1))
    main.run_tasks()
    main.run_tasks()  # nobody answers the second call, no retry left
    done = planned.store.get(task["id"])
    assert done["status"] == "partial" and "Schaut nach, ob für 4 frei ist." in done["result"]["summary"]
    assert planned.events["notify"][-1][1] == "Auftrag Anruf bei Seeblick: teilweise erledigt"


def test_a_callee_who_wants_no_more_calls_ends_the_task(main, planned):
    task = planned.add()
    planned.sessions.append(talked("Seeblick: Rufen Sie hier nie wieder an."))
    planned.result = {**next_call(main.datetime.now().astimezone()), "do_not_call": True}
    main.run_tasks()
    assert planned.store.get(task["id"])["status"] == "partial" and planned.store.blocked("04542 1234")


def test_unanswered_task_call_is_retried_without_follow_up(main, planned):
    task = planned.add(retries=1)
    main.run_tasks()
    again = planned.store.get(task["id"])
    assert again["status"] == "planned" and again["attempts"][0]["outcome"] == "no answer"
    assert planned.followed == [] and planned.events["notify"] == []
    main.run_tasks()
    assert len(planned.dialed) == 1  # the next attempt is not due yet


def test_mailbox_counts_as_not_reached(main, planned):
    task = planned.add(retries=0)
    planned.sessions.append(talked("Mailbox: Bitte sprechen Sie nach dem Ton."))
    planned.result = {"status": "not_reached", "summary": "Mailbox."}
    main.run_tasks()
    done = planned.store.get(task["id"])
    assert done["status"] == "not_reached" and done["attempts"][0]["outcome"] == "mailbox"
    assert planned.events["notify"][0][1] == "Auftrag Anruf bei Seeblick: nicht erreicht"


LEFT = {"status": "message_left", "summary": "Nur die Mailbox erreicht, um Rückruf gebeten."}


def test_message_on_the_mailbox_makes_the_task_wait_without_another_call(main, planned):
    """#65: the task was dialed twice more after the message had been left. #92: it waits for the call back
    (a short notification, no call back to the requester) and ends as no_reply when its window is over."""
    task = planned.add(retries=2, call_back=True)
    planned.sessions.append(talked("Seeblick (mailbox): Bitte sprechen Sie nach dem Ton.", "Claudine: Hallo …",
                                   mailbox=True, message_left=True))
    planned.result = {"status": "partial", "summary": LEFT["summary"]}  # the phone system knows better
    main.run_tasks()
    waiting = planned.store.get(task["id"])
    assert waiting["status"] == "waiting" and waiting["attempts"][0]["outcome"] == "message left"
    assert [s["outcome"] for s in waiting["steps"]] == ["message left"]
    assert planned.mailbox is True  # the follow-up is told, it only writes the summary
    assert planned.events["notify"] == [("notify.phone", "Auftrag Anruf bei Seeblick: wartet auf Rückruf",
                                         LEFT["summary"])]
    assert wait_for(lambda: planned.events["events"][-1][1]["status"] == "waiting")
    assert [d[0] for d in planned.dialed] == ["045421234"]  # the callee once, no call back yet
    planned.store.tasks[0]["next_try"] = "2026-01-01T00:00+01:00"
    main.run_tasks()
    assert len(planned.dialed) == 1  # never dialed again
    planned.store.tasks[0]["not_after"] = "2026-01-01T00:00+01:00"  # the window is over: nobody called back
    main.run_tasks()
    done = planned.store.get(task["id"])
    assert done["status"] == "no_reply" and LEFT["summary"] in done["result"]["summary"]
    assert planned.events["notify"][-1][1] == "Auftrag Anruf bei Seeblick: kein Rückruf"
    assert [d[0] for d in planned.dialed] == ["045421234", "0151111"]  # now the requester gets the result


def test_recognized_mailbox_without_a_message_is_retried_without_follow_up(main, planned):
    task = planned.add(retries=1)  # e.g. leave_message off, or the mailbox hung up before the message
    planned.sessions.append(talked("Seeblick: Hier ist die Mailbox.", mailbox=True))
    main.run_tasks()
    again = planned.store.get(task["id"])
    assert again["status"] == "planned" and again["attempts"][0]["outcome"] == "mailbox"
    assert planned.followed == [] and planned.events["notify"] == []


def test_call_in_which_nothing_we_said_was_answered_is_retried_without_follow_up(main, planned):
    """#85: an unrecognized mailbox said "Guten Tag.", recorded our questions and hung up. It was reported as
    partial ("jemand hat abgenommen, dann brach das Gespräch ab") and never tried again."""
    task = planned.add(retries=1)
    planned.sessions.append(talked("Seeblick: Guten Tag.", "Claudine: Steht das Klavier vor der Tür?",
                                   unanswered=True))
    planned.result = {"status": "partial", "summary": "Jemand hat abgenommen, dann brach das Gespräch ab."}
    main.run_tasks()
    again = planned.store.get(task["id"])
    assert again["status"] == "planned" and again["attempts"][0]["outcome"] == "no reply"
    assert planned.followed == [] and planned.events["notify"] == []


def test_message_left_reported_by_the_follow_up_makes_the_task_wait_too(main, planned):
    task = planned.add(retries=2)  # the phone system did not recognize the announcement, Claude did
    planned.sessions.append(talked("Seeblick: Sprechen Sie jetzt.", "Claudine: Hallo …"))
    planned.result = dict(LEFT)
    main.run_tasks()
    assert planned.store.get(task["id"])["status"] == "waiting" and planned.mailbox is False


def test_message_left_does_not_count_when_no_message_was_to_be_left(main, planned):
    task = planned.add(retries=2, leave_message=False)
    planned.sessions.append(talked("Seeblick: Sprechen Sie jetzt."))
    planned.result = dict(LEFT)
    main.run_tasks()
    again = planned.store.get(task["id"])
    assert again["status"] == "planned" and again["attempts"][0]["outcome"] == "mailbox"


def test_the_call_back_asks_the_requester_what_the_plan_left_to_them(main, planned):
    """#102: "frag ihn, und frag mich dann, ob ich die Garage öffnen soll": the call back is mandatory, asks the
    question and lets the requester give the order there with their own rights."""
    planned.add(ask_requester=["Soll ich die Garage öffnen?"])
    planned.sessions.append(talked("Fahrer: Ja, bitte die Garage auf."))
    planned.result = {"status": "done", "summary": "Der Fahrer möchte, dass die Garage geöffnet wird."}
    main.run_tasks()
    assert [d[0] for d in planned.dialed] == ["045421234", "0151111"]
    back = planned.dialed[1][1]
    assert "Der Fahrer möchte, dass die Garage geöffnet wird." in back
    assert "Before you say goodbye, ask Jan what the plan left for them to decide: Soll ich die Garage öffnen?" in back
    assert "is their order, not the callee's" in back


def test_mailbox_follow_up_failure_still_waits_for_the_call_back(main, planned, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("API down")
    task = planned.add()
    planned.sessions.append(talked("Seeblick (mailbox): …", mailbox=True, message_left=True))
    monkeypatch.setattr(main.brain, "follow_up", boom)
    main.run_tasks()
    done = planned.store.get(task["id"])
    assert done["status"] == "waiting" and done["result"]["summary"].startswith("Only the mailbox answered")


def test_task_waits_while_the_line_is_busy_or_sip_is_down(main, planned, monkeypatch):
    planned.add()
    assert main.busy.acquire(blocking=False)
    try:
        main.run_tasks()
    finally:
        main.busy.release()
    monkeypatch.setattr(main, "sip_status", lambda: PhoneStatus.FAILED)
    main.run_tasks()
    assert planned.dialed == []


def test_no_task_call_outside_the_hours(main, planned, monkeypatch):
    planned.add()
    now = main.datetime.now().astimezone()
    monkeypatch.setitem(main.cfg, "outbound_hours", f"{(now.hour + 2) % 24:02d}:00-{(now.hour + 3) % 24:02d}:00")
    main.run_tasks()
    assert planned.dialed == [] and not main.busy.locked()


def test_result_is_called_back_to_the_requester(main, planned):
    planned.add(call_back=True)
    planned.sessions.append(talked())
    main.run_tasks()
    assert [d[0] for d in planned.dialed] == ["045421234", "0151111"]
    assert "Tisch um 19 Uhr reserviert." in planned.dialed[1][1] and planned.dialed[1][2] is None


def test_do_not_call_wish_is_stored(main, planned):
    planned.add()
    planned.sessions.append(talked("Seeblick: Bitte rufen Sie hier nicht mehr an."))
    planned.result = {"status": "failed", "summary": "Möchte nicht angerufen werden.", "do_not_call": True}
    main.run_tasks()
    assert planned.store.blocked("045421234")


def test_failed_follow_up_ends_as_partial(main, planned, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("API down")
    task = planned.add()
    planned.sessions.append(talked())
    monkeypatch.setattr(main.brain, "follow_up", boom)
    main.run_tasks()
    done = planned.store.get(task["id"])
    assert done["status"] == "partial" and "API down" in done["result"]["summary"]


def test_blocked_numbers_are_not_dialed_by_outbound(main, monkeypatch):
    assert main.busy.acquire(blocking=False)
    monkeypatch.setattr(main, "phone", None)  # dialing would fail on the missing phone
    monkeypatch.setattr(main, "finish", lambda *a, **kw: None)
    assert main.outbound("112", "test") is None
    assert not main.busy.locked()


def test_outbound_dials_the_complete_number_without_spaces(main, monkeypatch):
    dialed = []

    def call(number):  # stops right after dialing
        dialed.append(number)
        raise TimeoutError("test")

    assert main.busy.acquire(blocking=False)
    monkeypatch.setattr(main, "phone", SimpleNamespace(call=call))
    monkeypatch.setattr(main, "finish", lambda *a, **kw: None)
    assert main.outbound("04542 4635", "test") is None  # #51: the INVITE only got "04542"
    main.busy.acquire()
    main.outbound("+49 (0) 4542-4635", "test")
    assert dialed == ["045424635", "045424635"] and not main.busy.locked()


def test_tasks_api(server, planned):
    assert server("GET", "/tasks", token=None)[0] == 401
    status, task = server("POST", "/tasks", {"number": "04542 1234", "name": "Seeblick", "task": "Tisch reservieren",
                                             "goal": "Reserve a table", "requester": "0151111", "call_back": True})
    assert status == 201 and task["status"] == "planned" and task["requester"]["name"] == "Jan Mobile"
    assert server("GET", "/tasks")[1]["tasks"][0]["id"] == task["id"]
    assert server("GET", f"/tasks/{task['id']}")[1]["goal"] == "Reserve a table"
    assert server("GET", "/tasks/nope")[0] == 404
    assert server("DELETE", f"/tasks/{task['id']}") == (200, {"status": "cancelled"})
    for _ in range(50):
        if planned.events["events"]:
            break
        time.sleep(0.02)
    assert planned.events["events"][-1][1]["status"] == "cancelled" and planned.dialed == []  # no call back
    assert server("DELETE", f"/tasks/{task['id']}")[0] == 409
    assert server("DELETE", "/tasks/nope")[0] == 404
    assert server("DELETE", "/call")[0] == 404


def wait_for(check, tries=100):
    for _ in range(tries):
        if check():
            return True
        time.sleep(0.02)
    return False


def test_every_status_change_is_announced(server, planned):
    """braincall_task_updated on planned, running, planned again and the end: Brain Assist's to-do list follows."""
    status, task = server("POST", "/api/tasks", {"number": "04542 1234", "name": "Seeblick", "task": "x", "goal": "x",
                                                 "retries": 1})
    assert status == 201
    store, now = planned.store, datetime.now().astimezone()
    store.claim(now)
    store.not_reached(task["id"], "no answer", now)
    store.claim(now + timedelta(hours=1))
    store.finish(task["id"], {"status": "done", "summary": "ok"})

    def updates():
        return [d["status"] for n, d in planned.events["events"] if n == "braincall_task_updated"]

    assert wait_for(lambda: len(updates()) == 5)
    assert sorted(updates()) == sorted(["planned", "running", "planned", "running", "done"])
    first = next(d for n, d in planned.events["events"] if n == "braincall_task_updated")
    assert first["id"] == task["id"] and first["goal"] == "x" and first["requester"] == "Jan Lenck"


def test_tasks_api_calls_a_person_by_name_for_a_home_assistant_user(server, planned, main, monkeypatch):
    monkeypatch.setitem(main.cfg, "callers", config._callers(PEOPLE))
    status, task = server("POST", "/api/tasks", {"name": "maryna", "task": "Frag nach Freitag", "goal": "Termin",
                                                 "requester_user_id": "", "requester_user_name": "jan",
                                                 "call_back": True})
    assert status == 201 and (task["number"], task["name"]) == ("0170222", "Maryna")
    assert task["requester"] == {"number": "0151111", "name": "Jan", "ha_user": "Jan"} and task["call_back"] is True

    status, task = server("POST", "/api/tasks", {"number": "04542 1234", "name": "Seeblick", "task": "x",
                                                 "goal": "x", "requester_user_id": "u-77",
                                                 "requester_user_name": "Flur", "call_back": True})
    assert status == 201 and task["call_back"] is False  # nobody to call back
    assert task["requester"] == {"number": "", "name": "Flur", "ha_user": "u-77"}

    # an unlisted number next to a Home Assistant user: the user asks, not the owner
    status, task = server("POST", "/api/tasks", {"number": "04542 1234", "name": "Seeblick", "task": "x",
                                                 "goal": "x", "requester": "0999", "requester_user_name": "Flur"})
    assert status == 201 and task["requester"] == {"number": "", "name": "Flur", "ha_user": "Flur"}

    assert server("POST", "/api/tasks", {"name": "Küche", "task": "x", "goal": "x"}) == (
        400, {"error": "Küche is not a contact with a phone number. Ask for the number."})
    assert server("POST", "/api/tasks", {"task": "x", "goal": "x"}) == (400, {"error": "the number is missing."})


def test_tasks_api_refuses(server, planned):
    assert server("POST", "/tasks", {"number": "112", "task": "x", "goal": "x"})[0] == 400
    assert server("POST", "/tasks", {"number": "04542 1234"}) == (400, {"error": "no task"})
    assert server("POST", "/tasks", {"number": "04542 1234", "task": "x", "goal": "x",
                                     "not_after": "2020-01-01T10:00"})[0] == 400
    # every task has a contact (#92): a number nobody has needs a name
    assert server("POST", "/tasks", {"number": "04542 1234", "task": "x", "goal": "x"}) == (
        400, {"error": "the number belongs to no contact: give the name of the person or business (it is saved as "
                       "a contact)"})
    planned.store.block("04542 1234")
    assert server("POST", "/tasks", {"number": "04542 1234", "name": "Seeblick", "task": "x", "goal": "x"})[0] == 409
    assert server("POST", "/tasks", {"number": "04542 1234", "name": "Seeblick", "task": "x", "goal": "x",
                                     "override_do_not_call": True})[0] == 201
    assert server("POST", "/call", {"number": "0900 123"})[0] == 400
    assert planned.store.all()[0]["status"] == "planned" and len(planned.store.all()) == 1


def test_tasks_api_continues_a_finished_task(server, planned):
    """#92: previous_task passes the history of a finished task of the same callee on."""
    old = planned.add()
    assert server("POST", "/api/tasks", {"name": "Seeblick", "number": "04542 1234", "task": "x", "goal": "x",
                                         "previous_task": old["id"]})[1]["error"].startswith(
        f"task {old['id']} is still open")
    planned.store.cancel(old["id"])
    status, task = server("POST", "/api/tasks", {"name": "Seeblick", "number": "04542 1234", "task": "x",
                                                 "goal": "x", "previous_task": old["id"]})
    assert status == 201 and task["earlier"]["id"] == old["id"] and task["earlier"]["status"] == "cancelled"


def test_tasks_api_plans_free_text_with_claude(server, planned, main, monkeypatch):
    monkeypatch.setattr(main.brain, "plan", lambda text, number, name: {
        "number": "0999", "name": "Restaurant Seeblick", "goal": "Reserve a table for 4",
        "may_share": ["Name Jan Lenck"], "retries": 1})
    status, task = server("POST", "/tasks", {"number": "04542 1234", "task": "Reservier einen Tisch für 4"})
    assert status == 201 and task["number"] == "045421234"  # the number given is never replaced, only spaces drop
    assert task["name"] == "Restaurant Seeblick" and task["contact_id"]  # the plan named it: a contact for it
    assert task["goal"] == "Reserve a table for 4" and task["may_share"] == ["Name Jan Lenck"] and task["retries"] == 1


def test_tasks_api_says_why_planning_failed(server, planned, main, monkeypatch):
    """#72: an answer without a usable plan is a 502 with the reason, not an empty plan."""
    def refuse(text, number, name):
        raise ValueError("no plan (refusal)")
    monkeypatch.setattr(main.brain, "plan", refuse)
    status, body = server("POST", "/tasks", {"number": "04542 1234", "task": "Reservier einen Tisch"})
    assert status == 502 and body["error"] == "planning failed: no plan (refusal)"
    assert planned.store.all() == []


def test_call_back_from_the_callee_continues_the_task(main, planned, monkeypatch):
    task = planned.add(number="0888777", name="Mark")  # not in callers, and no "*" entry
    monkeypatch.setitem(main.cfg, "callers", [c for c in main.cfg.callers if c["number"] != "*"])
    main.run_tasks()  # not answered
    ran = {}

    class Session:
        transcript, caller_turns, actions = ["0888777: Sie hatten angerufen?"], 1, []
        mailbox = message_left = unanswered = False

        def __init__(self, *a):
            pass

        def run(self, **kw):
            ran.update(kw)

    monkeypatch.setattr(main, "CallSession", Session)
    monkeypatch.setattr(main, "finish", lambda *a, **kw: None)
    call, denied = incoming("0888777")
    call.answer = lambda: None
    main.on_incoming(call)
    assert denied == [] and ran["access"] == main.GUEST and "probably calling back" in ran["instructions"]
    for _ in range(100):
        if planned.store.get(task["id"])["status"] == "done":
            break
        time.sleep(0.02)
    assert planned.store.get(task["id"])["status"] == "done" and planned.followed == [task["id"]]
    call, denied = incoming("0888777")  # no open task any more: rejected like any unlisted number
    main.on_incoming(call)
    assert denied == [True]


def test_call_back_from_another_phone_of_the_contact_continues_the_task(main, planned, monkeypatch):
    """#92: any number of the counterpart's contact is matched; the call is a step of the task (call_in)."""
    saved, _ = main.contact_store.save({"name": "Mark", "numbers": [{"number": "0888777", "label": "Handy"},
                                                                    {"number": "0888666", "label": "Büro"}]})
    task = planned.add(number="0888777", name="Mark")
    assert task["contact_id"] == saved["id"]
    main.run_tasks()  # not answered
    ran = {}

    class Session:
        transcript, caller_turns, actions = ["Mark: Sie hatten angerufen?"], 1, []
        mailbox = message_left = unanswered = False

        def __init__(self, *a):
            pass

        def run(self, **kw):
            ran.update(kw)

    monkeypatch.setattr(main, "CallSession", Session)
    monkeypatch.setattr(main, "finish", lambda *a, **kw: None)
    call, denied = incoming("0888666")
    call.answer = lambda: None
    main.on_incoming(call)
    assert denied == [] and "probably calling back" in ran["instructions"] and "Mark (Büro)" in ran["instructions"]
    assert wait_for(lambda: planned.store.get(task["id"])["status"] == "done")
    (step,) = planned.store.get(task["id"])["steps"]
    assert (step["kind"], step["number"], step["transcript"]) == ("call_in", "0888666", "Mark: Sie hatten angerufen?")
    ran.clear()
    call, denied = incoming("0888666")  # the task is over: a normal call of the contact, no continuation
    call.answer = lambda: None
    main.on_incoming(call)
    assert denied == [] and ran["access"] == main.GUEST and "calling back" not in ran["instructions"]
    assert len(planned.store.get(task["id"])["attempts"]) == 2


def test_the_phone_that_asked_not_to_be_called_is_blocked(main, planned, monkeypatch):
    """Review of #96: a call back from the office asks not to be called again: the office is blocked."""
    task = planned.add(number="0888777", name="Mark")
    main.run_tasks()  # not answered
    claimed = planned.store.claim_callback("0888666", main.datetime.now().astimezone())
    assert claimed is None  # not a number of the task
    planned.store.tasks[0]["attempts"].append({"time": "2026-11-10T12:00:00+01:00", "kind": "callback",
                                               "number": "0888666", "outcome": ""})
    planned.store.tasks[0]["status"] = "running"
    planned.result = {"status": "failed", "summary": "Will keine Anrufe.", "do_not_call": True}
    main.settle_task(planned.store.get(task["id"]), talked("Mark: Rufen Sie nie wieder an."))
    assert planned.store.blocked("0888666") and not planned.store.blocked("0888777")


def test_call_back_after_a_message_continues_the_task(main, planned, monkeypatch):
    """The message asks for a call back: it is answered (also from an unlisted number) and reported again."""
    task = planned.add(number="0888777", name="Mark")
    monkeypatch.setitem(main.cfg, "callers", [c for c in main.cfg.callers if c["number"] != "*"])
    planned.sessions.append(talked("Mark (mailbox): …", mailbox=True, message_left=True))
    planned.result = dict(LEFT)
    main.run_tasks()
    assert planned.store.get(task["id"])["status"] == "waiting" and len(planned.events["notify"]) == 1
    ran, turns = {}, [0, 1]  # first call back: nothing said; second: a conversation

    class Session:
        transcript, actions, mailbox, message_left = ["Mark: Du hattest angerufen?"], [], False, False
        unanswered = False

        def __init__(self, *a):
            self.caller_turns = turns.pop(0)

        def run(self, **kw):
            ran.update(kw)

    def wait(status, attempts):
        for _ in range(100):
            t = planned.store.get(task["id"])
            if t["status"] == status and len(t["attempts"]) == attempts:
                return t
            time.sleep(0.02)
        raise AssertionError(planned.store.get(task["id"]))

    monkeypatch.setattr(main, "CallSession", Session)
    monkeypatch.setattr(main, "finish", lambda *a, **kw: None)
    planned.result = {"status": "done", "summary": "Mark kommt am Samstag."}
    for expected, notified in (("waiting", 1), ("done", 2)):
        call, denied = incoming("0888777")
        call.answer = lambda: None
        main.on_incoming(call)
        assert denied == [] and "left a message on their mailbox" in ran["instructions"]
        wait(expected, 3 - len(turns))
        time.sleep(0.05)
        assert len(planned.events["notify"]) == notified  # hanging up without a word reports nothing new
    assert len(planned.dialed) == 1  # and never leads to another call
    assert planned.events["notify"][-1][1:] == ("Auftrag Anruf bei Mark: erledigt", "Mark kommt am Samstag.")


@pytest.mark.parametrize("admin, warned", [(False, True), (True, False), (None, False)])
def test_warns_once_if_the_token_is_no_admin(main, monkeypatch, caplog, admin, warned):
    monkeypatch.setattr(main.ha, "is_admin", lambda: admin)
    with caplog.at_level("WARNING"):
        main.check_ha_admin()
    assert any("not belong to an administrator" in r.message for r in caplog.records) is warned


# ---------------------------------------------------------------- shopping history
def test_shopping_history_api(server, main, monkeypatch, tmp_path):
    import shopping
    assert server("GET", "/shopping/history")[0] == 404   # automatic shopping list off (no Home Assistant)
    watcher = shopping.Shopping(main.cfg, main.ha, tmp_path / "shopping-history.jsonl")
    for days_ago, name in ((40, "Milch"), (3, "2 Milch"), (2, "Brot")):
        t = datetime.now(timezone.utc) - timedelta(days=days_ago)
        watcher.history.add({"time": t.isoformat(timespec="seconds"), "list": "todo.shopping_list",
                             "event": "bought", "item": name, "key": shopping.split(name)[0]})
    monkeypatch.setattr(main, "shopping_list", watcher)
    assert server("GET", "/shopping/history", token=None)[0] == 401
    status, body = server("GET", "/shopping/history")
    assert status == 200 and body["list"] == "todo.shopping_list" and len(body["events"]) == 3
    assert [e["item"] for e in server("GET", "/shopping/history?item=milch&days=30")[1]["events"]] == ["2 Milch"]
    for days in ("many", "nan", "inf", "-inf", "1e400", "-5", "0"):
        assert server("GET", f"/shopping/history?days={days}")[0] == 400, days


def test_shopping_forecast_api(server, main, monkeypatch, tmp_path):
    import shopping
    assert server("GET", "/shopping/forecast")[0] == 404
    watcher = shopping.Shopping(main.cfg, main.ha, tmp_path / "shopping-history.jsonl")
    monkeypatch.setattr(main, "shopping_list", watcher)
    assert server("GET", "/shopping/forecast")[0] == 404          # no rhythm list
    products = [{"product": "Milch", "class": "staple", "interval": "7 days", "status": "probably_out"}]
    watcher.rhythm = SimpleNamespace(entity_id="todo.shopping_rhythm", forecast=lambda: products)
    assert server("GET", "/shopping/forecast", token=None)[0] == 401
    assert server("GET", "/shopping/forecast") == (200, {"list": "todo.shopping_rhythm", "products": products})

    def broken():
        raise OSError("down")
    watcher.rhythm = SimpleNamespace(entity_id="todo.shopping_rhythm", forecast=broken)
    assert server("GET", "/shopping/forecast")[0] == 503


def test_shopping_lookup_without_automatic_shopping_list_waits_for_home_assistant(main, monkeypatch):
    """shopping_auto off: the list of the shopping tools is looked for until Home Assistant answers (#74)."""
    answers = [OSError("refused"), False, True]
    tries = []

    def locate(ha, cfg):
        tries.append(1)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(main.shopping, "locate", locate)
    monkeypatch.setattr(main.stopping, "wait", lambda s: False)
    main.shopping_lookup_loop()
    assert len(tries) == 3 and answers == []


# ---------------------------------------------------------------- contacts (#93)
@pytest.fixture
def contact_book(main, monkeypatch, tmp_path):
    """A contacts store in a temporary folder; the live list is a copy, so other tests keep theirs."""
    import contacts
    monkeypatch.setitem(main.cfg, "callers", list(main.cfg.callers))
    monkeypatch.setitem(main.cfg, "callers_raw", [
        {"number": "0151111", "name": "Jan", "access": "admin"},
        {"number": "*", "name": "Unknown", "access": "guest"}])
    store = contacts.Contacts(main.cfg, tmp_path / "contacts.json", in_use=main.contact_in_use)
    monkeypatch.setitem(main.cfg, "callers", [c for c in main.cfg.callers if config.is_rule(c)])
    store.rules = list(main.cfg.callers)
    store.setup()
    monkeypatch.setattr(main, "contact_store", store)
    monkeypatch.setattr(main, "contact_choices", lambda: {"users": [{"value": "u1", "label": "Jan"}], "devices": []})
    return store


def test_contacts_api(server, contact_book, main):
    status, body = server("GET", "/api/contacts")
    assert status == 200 and [c["name"] for c in body["contacts"]] == ["Jan"]
    assert body["users"] == [{"value": "u1", "label": "Jan"}] and body["phrase_languages"] == ["de", "en", "uk"]
    assert server("GET", "/api/contacts", token=None)[0] == 401
    willi = {"name": "Willi", "numbers": [{"number": "0170 777", "label": "Handy"}]}
    status, new = server("POST", "/api/contacts", willi)
    assert status == 201 and new["origin"] == "page"
    # now known on the phone, without restart
    assert main.find_caller("0170777", wildcard=False)["name"] == "Willi"
    assert main.caller_label(main.find_caller("0170777"), "0170777") == "Willi (Handy)"
    status, body = server("POST", "/api/contacts", {"name": "willi", "numbers": [{"number": "0151111"}]})
    assert status == 400 and sorted(e["code"] for e in body["errors"]) == ["name_taken", "number_taken"]
    status, changed = server("PUT", f"/api/contacts/{new['id']}", {**new, "email": "willi@example.com"})
    assert status == 200 and changed["email"] == "willi@example.com"
    jan = contact_book.by_name("Jan")
    status, merged = server("POST", f"/api/contacts/{jan['id']}/merge", {"other": new["id"]})
    assert status == 200 and [n["number"] for n in merged["numbers"]] == ["0151111", "0170 777"]
    assert server("DELETE", f"/api/contacts/{new['id']}")[0] == 404
    assert server("DELETE", f"/api/contacts/{jan['id']}") == (200, {"deleted": jan["id"]})
    assert server("PUT", "/api/contacts", {"name": "x"})[0] == 404


def test_contacts_page_is_served(server, main):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("GET", "/contacts")
    r = conn.getresponse()
    page = r.read().decode()
    conn.close()
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/html")
    assert "api/contacts" in page and '"/api/contacts' not in page  # relative: also works behind ingress


def test_ingress_serves_only_the_contacts_page(main, contact_book, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "ingress_admin", lambda user: calls.append(user) or user == "admin-id")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), main.IngressHandler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def get(path, user="admin-id", proxy=True):
        monkeypatch.setattr(main, "INGRESS_PROXY", "127.0.0.1" if proxy else "172.30.32.2")
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        conn.request("GET", path, headers={"X-Remote-User-Id": user})
        r = conn.getresponse()
        r.read()
        conn.close()
        return r.status
    try:
        assert get("/") == 200 and get("/api/contacts") == 200
        assert get("/api/contacts", user="someone") == 403
        assert get("/api/tasks") == 404 and get("/api/version") == 404  # nothing else of the API
        assert get("/api/contacts", proxy=False) == 403                 # only through Home Assistant
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_contact_with_several_numbers_is_one_person(main, monkeypatch):
    monkeypatch.setitem(main.cfg, "callers", config._callers([
        {"id": "j1", "name": "Jan", "access": "admin", "ha_user": "jan",
         "numbers": [{"number": "0151111", "label": "Handy"}, {"number": "04542 81234", "label": "laut"}]}]))
    caller = main.find_caller("+49454281234")
    assert caller["name"] == "Jan" and main.caller_name(caller) == "Jan"
    assert main.caller_label(caller, "+49454281234") == "Jan (laut)"
    import memory
    assert memory.who(caller, "+49454281234") == {"number": "+49454281234", "ha_user": "jan", "contact": "j1",
                                                  "numbers": ["0151111", "04542 81234"]}


def test_a_contacts_language_follows_the_last_call(main, events, monkeypatch):
    """#93: after a call with at least two utterances of a contact, the language they spoke is stored."""
    stored = []
    monkeypatch.setattr(main.contact_store, "set_language", lambda cid, lang: stored.append((cid, lang)))

    def summarize(transcript, caller, actions, lang, spoken=None, **kw):
        if spoken is not None:
            spoken["language"] = "en"
        return "Summary"
    monkeypatch.setattr(main.brain, "summarize", summarize)
    jan = {**main.find_caller("0151111"), "id": "j1"}
    two = session("caller: Hello", "Claudine: Hallo!", "caller: How are you?")
    two.caller_turns = 2
    ended = time.time()
    main.after_call(True, "0151111", ended - 30, ended, two, "test", jan, True)
    assert stored == [("j1", "en")]
    one = session("caller: Hello", "Claudine: Hallo!")
    one.caller_turns = 1
    main.after_call(True, "0151111", ended - 30, ended, one, "test", jan, True)    # too short
    main.after_call(True, "0999", ended - 30, ended, two, "test", main.find_caller("0999"), True)  # not a contact
    assert stored == [("j1", "en")]


def test_ingress_refuses_when_the_admin_check_is_not_possible(main, monkeypatch):
    """Review of #94: unknown means no, and it is not cached (the next request asks Home Assistant again)."""
    answers = [None, True]
    monkeypatch.setattr(main.ha, "user_admin", lambda user: answers.pop(0))
    monkeypatch.setattr(type(main.ha), "enabled", property(lambda self: True))
    monkeypatch.setattr(main, "_admins", {})
    assert main.ingress_admin("u1") is False
    assert main.ingress_admin("u1") is True and answers == []


def test_tasks_api_refuses_a_name_that_belongs_to_another_contact(server, contact_book, main, monkeypatch):
    """Review of #94: like by voice, an unknown number under the name of an existing contact is refused."""
    import tasks
    monkeypatch.setitem(main.cfg, "outbound_allowed_regex", "")
    store = tasks.TaskStore(contact_book.path.parent / "tasks.json", main.cfg)
    monkeypatch.setattr(main, "task_store", store)
    monkeypatch.setattr(main.brain, "contacts", contact_book)
    status, body = server("POST", "/api/tasks", {"number": "0160 9999", "name": "Jan", "task": "x", "goal": "x"})
    assert status == 409 and "already a contact named Jan" in body["error"] and store.all() == []
    status, _ = server("POST", "/api/tasks", {"number": "0160 9999", "name": "Seeblick", "task": "x", "goal": "x"})
    assert status == 201 and contact_book.by_name("Seeblick")["origin"] == "task"


# ---------------------------------------------------------------- line state for Brain Assist (#100)
def states(main, sent):
    """The braincall_state events sent so far (they go out from their own thread)."""
    main.state_events.join()
    return [(d["line"], (d["call"] or {}).get("direction")) for d in sent["state"]]


def test_an_incoming_call_rings_is_in_call_and_frees_the_line(main, events, server, monkeypatch):
    seen = {}

    class Session:
        transcript, caller_turns, actions = [], 0, []

        def __init__(self, *a):
            pass

        def run(self, **kw):
            seen["health"] = server("GET", "/health", token=None)[1]["line"]
            seen["status"] = server("GET", "/api/status")

    monkeypatch.setattr(main, "CallSession", Session)
    monkeypatch.setattr(main, "finish", lambda *a, **kw: None)
    call, denied = incoming("0151111")
    call.answer = lambda: seen.setdefault("answered", main.line_status()["line"])
    main.on_incoming(call)
    assert seen["answered"] == "ringing" and seen["health"] == "in_call"
    status, body = seen["status"]
    assert status == 200 and body["line"] == "in_call" and body["sip"] == "REGISTERED" and body["phone"] is True
    assert {k: v for k, v in body["call"].items() if k != "since"} == {
        "number": "0151111", "caller_name": "Jan Mobile", "direction": "in"}
    assert datetime.fromisoformat(body["call"]["since"]).tzinfo is not None
    assert states(main, events) == [("ringing", "in"), ("in_call", "in"), ("idle", None)]
    assert events["state"][-1] == {"line": "idle", "sip": "REGISTERED", "call": None}
    assert server("GET", "/api/status")[1] == {"line": "idle", "sip": "REGISTERED", "call": None, "phone": True}


def test_a_rejected_call_does_not_change_the_line(main, events):
    busy_rejected(main, "0151111")
    call, denied = incoming("")  # withheld
    main.on_incoming(call)
    assert states(main, events) == [] and main.line_status()["line"] == "idle"


class _Call:
    def __init__(self, state):
        self.state, self.call_id = state, "cid-100"

    def hangup(self):
        pass


def phone_for(main, monkeypatch, state, seen):
    def call(number):
        seen.append(main.line_status())
        return _Call(state)
    monkeypatch.setattr(main, "phone", SimpleNamespace(call=call, calls={}, sip=SimpleNamespace(pending_invites={})))
    monkeypatch.setattr(main, "finish", lambda *a, **kw: None)


def test_an_outbound_call_dials_is_in_call_and_frees_the_line(main, events, monkeypatch):
    from pyVoIP.VoIP import CallState
    seen = []

    class Session:
        def __init__(self, *a):
            pass

        def run(self, **kw):
            seen.append(main.line_status())

    phone_for(main, monkeypatch, CallState.ANSWERED, seen)
    monkeypatch.setattr(main, "CallSession", Session)
    assert main.busy.acquire(blocking=False)
    main.outbound("0171444", "test")
    assert [s["line"] for s in seen] == ["dialing", "in_call"]
    assert seen[1]["call"]["caller_name"] == "Walter" and seen[1]["call"]["direction"] == "out"
    assert states(main, events) == [("dialing", "out"), ("in_call", "out"), ("idle", None)]
    assert not main.busy.locked() and main.line_status()["line"] == "idle"


def test_an_outbound_call_nobody_answers_goes_back_to_idle(main, events, monkeypatch):
    from pyVoIP.VoIP import CallState
    phone_for(main, monkeypatch, CallState.ENDED, [])
    assert main.busy.acquire(blocking=False)
    assert main.outbound("0999", "test") is None
    assert states(main, events) == [("dialing", "out"), ("idle", None)]
    assert events["state"][0]["call"]["caller_name"] == ""  # not listed (no "*" for outbound)


def test_a_refused_number_never_dials(main, events, monkeypatch):
    phone_for(main, monkeypatch, None, [])
    assert main.busy.acquire(blocking=False)
    assert main.outbound("112", "test") is None
    assert states(main, events) == []


def watchdog_rounds(main, monkeypatch, statuses):
    """Runs the SIP watchdog loop for one round per status, never reconnecting."""
    rounds = iter(statuses)
    current = {}

    class Stop:
        def wait(self, timeout):
            current["status"] = next(rounds, None)
            return current["status"] is None

        def is_set(self):
            return False
    monkeypatch.setattr(main, "stopping", Stop())
    monkeypatch.setattr(main, "sip_status", lambda: current.get("status") or PhoneStatus.REGISTERED)
    monkeypatch.setattr(main, "RECONNECT_AFTER", 10 ** 6)
    main.watchdog()


def test_a_lost_registration_is_announced_from_the_second_round(main, events, monkeypatch):
    """pyVoIP re-registers every 2 min (registering for a moment): one round is no change, two are."""
    monkeypatch.setattr(main, "sip_confirmed", "REGISTERED")
    R, G, F = PhoneStatus.REGISTERED, PhoneStatus.REGISTERING, PhoneStatus.FAILED
    watchdog_rounds(main, monkeypatch, [R, G, R, F, F, F, R])
    main.state_events.join()
    assert [d["sip"] for d in events["state"]] == ["FAILED", "REGISTERED"]
    assert {d["line"] for d in events["state"]} == {"idle"}


def test_a_call_during_a_re_registration_announces_the_confirmed_sip_status(main, events, monkeypatch):
    """A line change in pyVoIP's moment of REGISTERING (every 2 min) must not announce it: no flapping."""
    monkeypatch.setattr(main, "sip_confirmed", "REGISTERED")
    monkeypatch.setattr(main, "sip_status", lambda: PhoneStatus.REGISTERING)
    main.set_line(main.RINGING, "0151111", main.find_caller("0151111"), "in")
    main.set_line(main.IDLE)
    main.state_events.join()
    assert [d["sip"] for d in events["state"]] == ["REGISTERED", "REGISTERED"]
    assert main.line_status()["sip"] == "REGISTERING"  # /api/status is the moment, like /health


def test_state_events_are_queued_in_the_order_of_their_snapshots(main, events, monkeypatch):
    """A watchdog announcement racing a call: whatever comes first, the last event has the current line."""
    monkeypatch.setattr(main, "sip_confirmed", "REGISTERED")
    statuses = iter([PhoneStatus.FAILED] * 200 + [PhoneStatus.REGISTERED] * 200)
    monkeypatch.setattr(main, "sip_status", lambda: next(statuses, PhoneStatus.REGISTERED))
    watchdog = threading.Thread(target=lambda: [main.note_sip() for _ in range(200)])
    watchdog.start()
    for _ in range(50):
        main.set_line(main.IN_CALL, "0151111", None, "in")
        main.set_line(main.IDLE)
    main.set_line(main.IN_CALL, "0151111", None, "in")
    watchdog.join()
    main.state_events.join()
    assert events["state"][-1]["line"] == "in_call"
    main.set_line(main.IDLE)
    main.state_events.join()
