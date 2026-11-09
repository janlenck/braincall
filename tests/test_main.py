"""main.py: caller lookup, HTTP trigger, rejecting calls and the handling after a call.
main configures itself on import, so it is imported once with a test configuration."""
import http.client
import importlib
import json
import os
import sys
import threading
import time
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
    def fail(*a):
        raise RuntimeError("API down")
    monkeypatch.setattr(main.brain, "summarize", fail)
    sent = after("0999")
    assert sent["notify"][0][2] == "caller: Hallo\nClaudine: Hallo!"


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
    assert main.previous("0999", main.GUEST) == ""


def test_after_call_remembers_summary_for_next_call(main, after, remembered):
    after("+49170222")
    after("0999", inbound=False)
    note = main.previous("0170222", frozenset({"calendar"}))
    assert "(they called): Summary of Maryna (+49170222)" in note
    assert "Previous calls with this number" in note
    assert "(you called them): Summary of 0999" in main.previous("0999", main.GUEST)


def test_memory_is_ordered_by_call_end_not_save_time(main, remembered, events, monkeypatch):
    monkeypatch.setattr(main.brain, "summarize", lambda transcript, caller, actions, lang: transcript[0])
    now = time.time()
    for text, ended in (("caller: later call", now - 60), ("caller: earlier call", now - 3600)):
        s = session(text)
        main.after_call(True, "0999", ended - 30, ended, s, "test", main.find_caller("0999"), True)
    note = main.previous("0999", main.GUEST)
    assert note.index("earlier call") < note.index("later call")


def test_after_call_without_summary_is_not_remembered(main, after, remembered, monkeypatch):
    def fail(*a):
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
    import tasks
    store = tasks.TaskStore(tmp_path / "tasks.json", main.cfg)
    monkeypatch.setattr(main, "task_store", store)
    monkeypatch.setattr(main.brain, "tasks", store)
    monkeypatch.setattr(main, "sip_status", lambda: PhoneStatus.REGISTERED)
    monkeypatch.setitem(main.cfg, "outbound_hours", "")
    ctx = SimpleNamespace(store=store, dialed=[], sessions=[], events=events, followed=[])

    def outbound(number, message, task=None):
        ctx.dialed.append((number, message, task))
        main.busy.release()
        return ctx.sessions.pop(0) if ctx.sessions else None

    def follow_up(task, transcript, actions=None):
        ctx.followed.append(task["id"])
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


def talked(*lines):
    return SimpleNamespace(transcript=list(lines or ("Seeblick: Ja, 19 Uhr geht.",)), caller_turns=1, actions=[])


def test_due_task_is_called_followed_up_and_reported(main, planned):
    task = planned.add()
    planned.sessions.append(talked())
    main.run_tasks()
    (number, message, passed), = planned.dialed
    assert number == "04542 1234" and "AI assistant" in message and passed["id"] == task["id"]
    assert planned.followed == [task["id"]] and planned.store.get(task["id"])["status"] == "done"
    assert planned.events["notify"] == [("notify.phone", "Auftrag Anruf bei Seeblick: erledigt",
                                         "Tisch um 19 Uhr reserviert.\nAnswers: Uhrzeit: 19:00")]
    name, data = planned.events["events"][-1]
    assert name == "braincall_task_finished" and data["status"] == "done" and data["answers"] == {"Uhrzeit": "19:00"}
    assert not main.busy.locked()


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
    assert [d[0] for d in planned.dialed] == ["04542 1234", "0151111"]
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


def test_tasks_api(server, planned):
    assert server("GET", "/tasks", token=None)[0] == 401
    status, task = server("POST", "/tasks", {"number": "04542 1234", "task": "Tisch reservieren",
                                             "goal": "Reserve a table", "requester": "0151111", "call_back": True})
    assert status == 201 and task["status"] == "planned" and task["requester"]["name"] == "Jan Mobile"
    assert server("GET", "/tasks")[1]["tasks"][0]["id"] == task["id"]
    assert server("GET", f"/tasks/{task['id']}")[1]["goal"] == "Reserve a table"
    assert server("GET", "/tasks/nope")[0] == 404
    assert server("DELETE", f"/tasks/{task['id']}") == (200, {"status": "cancelled"})
    assert server("DELETE", f"/tasks/{task['id']}")[0] == 409
    assert server("DELETE", "/tasks/nope")[0] == 404
    assert server("DELETE", "/call")[0] == 404


def test_tasks_api_refuses(server, planned):
    assert server("POST", "/tasks", {"number": "112", "task": "x", "goal": "x"})[0] == 400
    assert server("POST", "/tasks", {"number": "04542 1234"}) == (400, {"error": "no task"})
    assert server("POST", "/tasks", {"number": "04542 1234", "task": "x", "goal": "x",
                                     "not_after": "2020-01-01T10:00"})[0] == 400
    planned.store.block("04542 1234")
    assert server("POST", "/tasks", {"number": "04542 1234", "task": "x", "goal": "x"})[0] == 409
    assert server("POST", "/tasks", {"number": "04542 1234", "task": "x", "goal": "x",
                                     "override_do_not_call": True})[0] == 201
    assert server("POST", "/call", {"number": "0900 123"})[0] == 400
    assert planned.store.all()[0]["status"] == "planned" and len(planned.store.all()) == 1


def test_tasks_api_plans_free_text_with_claude(server, planned, main, monkeypatch):
    monkeypatch.setattr(main.brain, "plan", lambda text, number, name: {
        "number": "0999", "goal": "Reserve a table for 4", "may_share": ["Name Jan Lenck"], "retries": 1})
    status, task = server("POST", "/tasks", {"number": "04542 1234", "task": "Reservier einen Tisch für 4"})
    assert status == 201 and task["number"] == "04542 1234"  # the number given is never replaced
    assert task["goal"] == "Reserve a table for 4" and task["may_share"] == ["Name Jan Lenck"] and task["retries"] == 1


def test_call_back_from_the_callee_continues_the_task(main, planned, monkeypatch):
    task = planned.add(number="0888777", name="Mark")  # not in callers, and no "*" entry
    monkeypatch.setitem(main.cfg, "callers", [c for c in main.cfg.callers if c["number"] != "*"])
    main.run_tasks()  # not answered
    ran = {}

    class Session:
        transcript, caller_turns, actions = ["0888777: Sie hatten angerufen?"], 1, []

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
