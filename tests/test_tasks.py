"""Planned calls with a task (#51): what may be dialed and when, plan building, the task store with retries and
call backs, grants for the callee, plan_call with its confirmation, and the follow-up after the call."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from anthropic.types import ToolUseBlock

import config
import tasks
from brain import Brain
from config import parse_access
from conftest import make_cfg
from test_brain import StubHA, message, names, tool

TZ = ZoneInfo("Europe/Berlin")
NOON = datetime(2026, 11, 10, 12, 0, tzinfo=TZ)  # a Tuesday
JAN = {"number": "0151111", "name": "Jan Mobile"}
JAN_STORED = {**JAN, "ha_user": ""}
CALLERS = [{"number": "0151111", "name": "Jan Mobile", "access": "admin"},
           {"number": "0170222", "name": "Mark", "access": "guest"}]


GERMAN = config.DEFAULTS["outbound_allowed_regex"]


def cfg(**kw):
    kw.setdefault("outbound_allowed_regex", "")  # the tests use short made-up numbers
    return make_cfg(anthropic_api_key="test", owner_name="Jan Lenck", callers=CALLERS, **kw)


def plan(**kw):
    return {"number": "04542 1234", "name": "Restaurant Seeblick", "task": "Tisch für heute Abend reservieren",
            "goal": "Reserve a table for 4 tonight", **kw}


def build(c=None, now=NOON, **kw):
    task, error = tasks.build(c or cfg(), plan(**kw), JAN, now)
    assert error == "", error
    return task


# ---------------------------------------------------------------- numbers and hours
@pytest.mark.parametrize("number", ["110", "112", "+49 112", "0049110", "0900 123456", "0137 1234567", "11833", ""])
def test_blocked_numbers_are_never_dialed(number):
    assert tasks.number_refusal(cfg(), number)


@pytest.mark.parametrize("number", ["04542 1234", "+49 151 111", "0170222", "**611"])
def test_normal_numbers_are_fine(number):
    assert tasks.number_refusal(cfg(), number) == ""


def test_allowed_regex_limits_numbers():
    c = cfg(outbound_allowed_regex=r"0[1-9]\d{5,12}")  # own country only: mobile and landline
    assert tasks.number_refusal(c, "+49 4542 1234") == ""
    assert "outbound_allowed_regex" in tasks.number_refusal(c, "0033 1 234567")
    assert "outbound_allowed_regex" in tasks.number_refusal(c, "**611")
    assert "invalid" in tasks.number_refusal(cfg(outbound_allowed_regex="("), "04542 1234")


@pytest.mark.parametrize("number", [
    "0151 12345678", "+49 171 1234567", "0049 160 12345678", "030 123456", "04542 1234", "+49 89 12345678",
    "07903 12345", "032 221234567", "**6", "**611", "**9"])
def test_default_regex_lets_german_mobile_and_landline_through(number):
    assert tasks.number_refusal(cfg(outbound_allowed_regex=GERMAN), number) == ""


@pytest.mark.parametrize("number", [
    "110", "112", "115", "116117", "11833", "+49 112", "0900 1234567", "0800 1234567", "0700 1234567",
    "0137 1234567", "0180 5123456", "0190 123456", "0193 1234", "0033 1 23456789", "+1 212 5551234",
    "+43 1 234567", "**", "*21#", "**61234", "1234567", "0151 123", "010 33 030 123456"])
def test_default_regex_keeps_everything_else_out(number):
    assert tasks.number_refusal(cfg(outbound_allowed_regex=GERMAN), number)


@pytest.mark.parametrize("hours, at, ok", [
    ("09:00-20:00", "12:00", True), ("09:00-20:00", "08:59", False), ("09:00-20:00", "20:00", False),
    ("22:00-06:00", "23:30", True), ("22:00-06:00", "12:00", False), ("", "03:00", True), ("nonsense", "03:00", True),
])
def test_outbound_hours(hours, at, ok):
    h, m = map(int, at.split(":"))
    assert tasks.in_hours(cfg(outbound_hours=hours), NOON.replace(hour=h, minute=m)) is ok


# ---------------------------------------------------------------- building a task
def test_build_defaults():
    t = build()
    assert (t["status"], t["retries"], t["leave_message"], t["call_back"]) == ("planned", 2, True, False)
    assert t["not_before"] == "2026-11-10T12:00+01:00" and t["not_after"] == "2026-11-11T12:00+01:00"
    assert t["requester"] == JAN_STORED and t["grants"] == {} and t["follow_up"] == []


def test_build_stores_the_number_without_spaces():
    assert build(number=" 04542 4635 ")["number"] == "045424635"  # #51: dialed and read back exactly like this
    assert build(number="+49 151/111-22")["number"] == "+4915111122"


def test_build_takes_local_times_and_caps_retries():
    t = build(not_before="2026-11-10T18:00", not_after="2026-11-10T19:30", retries=99, call_back=True)
    assert t["next_try"] == "2026-11-10T18:00+01:00" and t["not_after"] == "2026-11-10T19:30+01:00"
    assert t["retries"] == 3 and t["call_back"] is True


@pytest.mark.parametrize("change, why", [
    ({"number": "112"}, "blocked"), ({"task": " "}, "task is missing"),
    ({"not_after": "2026-11-10T11:00"}, "already over"), ({"not_before": "morgen"}, "not readable"),
    ({"calendar_from": "2026-11-12"}, "both needed"), ({"book_title": "Kochen"}, "both needed"),
])
def test_build_refuses(change, why):
    task, error = tasks.build(cfg(), plan(**change), JAN, NOON)
    assert task is None and why in error


def test_build_grants_and_follow_up_tools():
    t = build(calendar_from="2026-11-12", calendar_to="2026-11-16", book_title="Kochen mit Mark",
              follow_up=[{"step": "Zutaten auf die Einkaufsliste", "tool": "shopping_edit"},
                         {"step": "Heizung aus", "tool": "ha_control"}, {"step": ""}, "x"])
    assert t["grants"] == {"calendar_from": "2026-11-12", "calendar_to": "2026-11-16", "book_title": "Kochen mit Mark"}
    assert t["follow_up"] == [{"step": "Zutaten auf die Einkaufsliste", "tool": "shopping_edit"},
                              {"step": "Heizung aus", "tool": ""}]  # ha_control is never a follow-up tool


def test_confirm_key_ignores_wording_but_not_rights():
    base = build(calendar_from="2026-11-12", calendar_to="2026-11-16")
    assert tasks.confirm_key(base) == tasks.confirm_key(build(calendar_from="2026-11-12", calendar_to="2026-11-16",
                                                              goal="Anders formuliert"))
    for change in ({"number": "04542 9999"}, {"calendar_to": "2026-11-30"}, {"book_title": "X"}, {"call_back": True},
                   {"follow_up": [{"step": "x", "tool": "shopping_edit"}]}, {"not_after": "2026-11-13T12:00"},
                   {"not_before": "2026-11-10T12:45"}, {"leave_message": False}):
        other = build(**{"calendar_from": "2026-11-12", "calendar_to": "2026-11-16", **change})
        assert tasks.confirm_key(base) != tasks.confirm_key(other), change


def test_brief_says_what_may_be_shared_and_the_etiquette():
    text = tasks.brief(build(may_share=["Name Jan Lenck", "4 Personen"], may_accept="18:00 bis 21:00",
                             questions=["Ist um 19 Uhr ein Tisch frei?"],
                             follow_up=[{"step": "Termin eintragen", "tool": "ha_create_event"}]), "Jan")
    for part in ("Reserve a table for 4 tonight", "Restaurant Seeblick", "Ist um 19 Uhr ein Tisch frei?",
                 "only: Name Jan Lenck; 4 Personen", "no address", "18:00 bis 21:00", "AI assistant",
                 "not to be called again", "no audio is recorded", "Termin eintragen",
                 "Say only what this task covers", "pass on nothing from them that this task does not cover",
                 "Before the goodbye answer every question the callee asked"):  # #87
        assert part in text, part


def test_brief_carries_the_task_in_the_requesters_words():
    """#89: goal "Jans Auftrag an das Office übermitteln", the message only in task: the call knew nothing."""
    message = "Beim Office eine Nachricht hinterlassen: erst prüfen, dass niemand im Keller ist."
    t = build(task=message, goal="Jans Auftrag an das Office übermitteln")
    text = tasks.brief(t, "Jan")
    assert "A task from Jan: Jans Auftrag an das Office übermitteln" in text
    assert f"The task in Jan's words (it is what this call covers; pass on what it asks you to pass on): {message}" \
        in text
    assert f"The task as the call gets it: {message}" in tasks.read_back(t, cfg(), True)


def test_brief_does_not_repeat_a_task_that_is_its_goal():
    for t in (build(goal="Tisch für heute Abend reservieren."), build(goal="")):  # no goal: the task is the goal
        assert "in Jan's words" not in tasks.brief(t, "Jan") and "as the call gets it" not in tasks.read_back(
            t, cfg(), True)


def test_clean_result_fixes_the_shape():
    r = tasks.clean_result({"status": "great", "summary": 5, "answers": ["x"], "steps": [{"step": "a", "done": 1}, 7],
                            "open_points": "eins", "do_not_call": 1})
    assert r == {"status": "partial", "summary": "5", "answers": {}, "steps": [{"step": "a", "done": True,
                 "result": ""}], "open_points": ["eins"], "do_not_call": True}
    assert tasks.clean_result(None)["status"] == "partial"
    assert tasks.clean_result({"status": "message_left", "summary": "x"})["status"] == "message_left"


# ---------------------------------------------------------------- whom to call, who asks (#50)
PEOPLE = [{"number": "0151111", "ha_user": "Jan", "name": "Jan", "access": "admin"},
          {"number": "0170222", "name": "Mark", "access": "guest"},
          {"number": "0170333", "name": "Mark", "access": "guest"},
          {"number": "0160444", "ha_user": "maryna-id", "name": "Maryna", "access": "read"},
          {"ha_user": "dashboard", "name": "Flur", "access": "admin"},
          {"number": "*", "name": "Unknown", "access": "guest"}]


def people_cfg():
    return make_cfg(outbound_allowed_regex="", callers=PEOPLE)


def test_callee_is_a_number_or_a_person_with_a_number():
    c = people_cfg()
    assert tasks.callee(c, {"number": "04542 1234", "name": "Seeblick"}) == ("04542 1234", "Seeblick", "")
    assert tasks.callee(c, {"name": " maryna "}) == ("0160444", "Maryna", "")
    assert tasks.callee(c, {"name": "Mark"})[2].startswith("Not planned: Mark is not a contact")  # two entries
    assert tasks.callee(c, {"name": "Flur"})[2].startswith("Not planned: Flur is not a contact")  # no number
    assert tasks.callee(c, {"name": "Unknown"})[2].startswith("Not planned: Unknown")            # "*"
    assert tasks.callee(c, {}) == ("", "", "Not planned: the number is missing.")


def test_build_calls_a_person_by_name():
    task, error = tasks.build(people_cfg(), {"name": "Maryna", "task": "x"}, JAN, NOON)
    assert error == "" and (task["number"], task["name"]) == ("0160444", "Maryna")


def test_requester_is_a_person():
    c = people_cfg()
    assert tasks.requester_entry(c, "+49151111") == {"number": "0151111", "name": "Jan", "ha_user": "Jan"}
    assert tasks.requester_entry(c, user_id="maryna-id", user_name="M") == {
        "number": "0160444", "name": "Maryna", "ha_user": "maryna-id"}
    assert tasks.requester_entry(c, user_id="x", user_name="dashboard") == {
        "number": "", "name": "Flur", "ha_user": "dashboard"}
    assert tasks.requester_entry(c, user_id="u9", user_name="Gast") == {"number": "", "name": "Gast",
                                                                        "ha_user": "u9"}


def test_without_a_number_there_is_no_call_back():
    task, _ = tasks.build(cfg(), plan(call_back=True), {"number": "", "name": "Flur", "ha_user": "dashboard"}, NOON)
    assert task["call_back"] is False and task["requester"]["ha_user"] == "dashboard"
    assert "no call back: there is no number to call" in tasks.read_back(task, cfg(), True)


def test_the_store_announces_every_status_change(store):
    seen = []
    store.on_change = lambda t: seen.append(t["status"])
    t = store.add(build(retries=1, not_after="2026-11-10T19:00"))
    store.claim(NOON)
    store.not_reached(t["id"], "no answer", NOON)
    other = store.add(build())
    store.cancel(other["id"])
    store.claim(NOON + timedelta(minutes=15))
    store.finish(t["id"], {"status": "done", "summary": "ok"})
    assert seen == ["planned", "running", "planned", "planned", "cancelled", "running", "done"]


def test_planned_is_announced_before_the_scheduler_can_claim_the_task(store):
    """add() tells the listener while it still holds the lock: a claim right after cannot overtake "planned"."""
    import threading

    seen = []

    def listener(t):
        free = []
        probe = threading.Thread(target=lambda: free.append(store.lock.acquire(blocking=False)))
        probe.start()
        probe.join()
        seen.append((t["status"], free[0]))

    store.on_change = listener
    store.add(build())
    assert seen == [("planned", False)]  # the lock was held by add()


def test_find_requester_is_none_for_someone_not_listed():
    assert tasks.find_requester(people_cfg(), "0999", "u9", "Gast") is None
    assert tasks.find_requester(people_cfg(), "0151111")["ha_user"] == "Jan"


def test_a_broken_listener_does_not_break_the_task(store):
    def broken(t):
        raise RuntimeError("Home Assistant is gone")

    store.on_change = broken
    assert store.add(build())["status"] == "planned" and len(store.all()) == 1


# ---------------------------------------------------------------- store
@pytest.fixture
def store(tmp_path):
    return tasks.TaskStore(tmp_path / "tasks.json", cfg())


def test_store_survives_a_restart(store):
    t = store.add(build())
    again = tasks.TaskStore(store.path, cfg())
    assert again.get(t["id"])["goal"] == "Reserve a table for 4 tonight"
    assert again.interrupted() == []


def test_running_task_ends_as_failed_after_a_restart(store):
    t = store.add(build())
    assert store.claim(NOON)["id"] == t["id"]
    again = tasks.TaskStore(store.path, cfg())
    (lost,) = again.interrupted()
    assert lost["status"] == "failed" and "restart" in lost["result"]["summary"]
    assert again.claim(NOON) is None  # never dialed a second time


def test_claim_only_when_due_in_hours_and_within_the_day_limit(tmp_path):
    store = tasks.TaskStore(tmp_path / "t.json", cfg(outbound_max_calls_per_day=1))
    first = store.add(build(not_before="2026-11-10T18:00"))
    assert store.claim(NOON) is None                             # not due yet
    assert store.claim(NOON.replace(hour=21)) is None            # due, but outside outbound_hours
    assert store.claim(NOON.replace(hour=18))["id"] == first["id"]
    store.add(build())
    assert store.claim(NOON.replace(hour=19)) is None            # one call per day already made
    assert store.claim(NOON.replace(hour=12) + timedelta(days=1)) is not None


def test_not_reached_retries_then_gives_up(store):
    t = store.add(build(retries=1, not_after="2026-11-10T19:00"))
    store.claim(NOON)
    again = store.not_reached(t["id"], "no answer", NOON)
    assert again["status"] == "planned" and again["next_try"] == "2026-11-10T12:15+01:00"
    assert store.claim(NOON + timedelta(minutes=10)) is None
    store.claim(NOON + timedelta(minutes=15))
    done = store.not_reached(t["id"], "mailbox", NOON + timedelta(minutes=16))
    assert done["status"] == "not_reached" and "2 attempt(s)" in done["result"]["summary"]
    assert [a["outcome"] for a in done["attempts"]] == ["no answer", "mailbox"]


def test_no_retry_after_the_time_window(store):
    t = store.add(build(retries=3, not_after="2026-11-10T12:10"))
    store.claim(NOON)
    assert store.not_reached(t["id"], "no answer", NOON)["status"] == "not_reached"


def test_expired_tasks_end_as_not_reached(store):
    t = store.add(build(not_before="2026-11-10T21:00", not_after="2026-11-10T22:00"))  # only outside the hours
    assert store.expire(NOON.replace(hour=21, minute=30)) == []
    (gone,) = store.expire(NOON.replace(hour=22, minute=1))
    assert gone["id"] == t["id"] and gone["status"] == "not_reached"
    assert store.expire(NOON.replace(hour=23)) == []


def test_call_back_continues_only_a_task_that_was_tried(store):
    t = store.add(build(number="0170222"))
    assert not store.has_callback("0170222") and store.claim_callback("0170222", NOON) is None
    store.claim(NOON)
    store.not_reached(t["id"], "no answer", NOON)
    assert store.has_callback("+49170222") and not store.has_callback("0170999")
    claimed = store.claim_callback("+49170222", NOON + timedelta(minutes=5))
    assert claimed["status"] == "running" and claimed["attempts"][-1]["kind"] == "callback"
    assert store.claim(NOON + timedelta(minutes=20)) is None  # not dialed while the call back runs
    done = store.finish(t["id"], {"status": "done", "summary": "Tisch um 19 Uhr."})
    assert done["status"] == "done" and done["attempts"][-1]["outcome"] == "talked"


def test_a_task_waiting_for_a_call_back_continues_when_it_comes(store):
    """#65, #92: after a message the task waits (no retry); the callee may call back while its window lasts."""
    t = store.add(build(number="0170222", retries=2, not_after="2026-11-10T19:00"))
    store.claim(NOON)
    left = store.wait(t["id"], {"status": "message_left", "summary": "Um Rückruf gebeten."})
    store.record_step(t["id"], ["Mark (mailbox): …"], "message left", "Um Rückruf gebeten.")
    assert left["status"] == "waiting" and left["attempts"][0]["outcome"] == "talked"
    assert store.claim(NOON + timedelta(hours=1)) is None  # no further attempt
    assert store.has_callback("0170222", NOON + timedelta(hours=1))
    assert not store.has_callback("0170222", NOON.replace(hour=19, minute=1))  # only inside the time window
    assert store.claim_callback("0170222", NOON.replace(hour=19, minute=1)) is None
    claimed = store.claim_callback("0170222", NOON + timedelta(hours=1))
    assert claimed["status"] == "running" and claimed["result"]["summary"] == "Um Rückruf gebeten."
    # the call back came to nothing: still waiting for the next one, never dialed
    again = store.not_reached(t["id"], "nothing said", NOON + timedelta(hours=1))
    assert again["status"] == "waiting" and again["result"]["summary"] == "Um Rückruf gebeten."
    assert store.claim(NOON + timedelta(hours=2)) is None
    store.claim_callback("0170222", NOON + timedelta(hours=3))
    done = store.finish(t["id"], {"status": "done", "summary": "Kommt am Samstag."})
    assert done["status"] == "done" and [a["outcome"] for a in done["attempts"]] == [
        "talked", "nothing said", "talked"]
    assert not store.has_callback("0170222", NOON + timedelta(hours=4))


def test_waiting_ends_as_no_reply_and_can_be_cancelled(store):
    t = store.add(build(number="0170222", not_after="2026-11-10T19:00"))
    store.claim(NOON)
    store.wait(t["id"], {"status": "wait", "summary": "Ruft zurück.", "open_points": ["Termin"]})
    assert store.expire(NOON.replace(hour=18)) == []
    (gone,) = store.expire(NOON.replace(hour=19, minute=1))
    assert gone["status"] == "no_reply" and "Ruft zurück." in gone["result"]["summary"]
    assert gone["result"]["open_points"] == ["Termin"]
    other = store.add(build(number="0170222"))
    store.claim(NOON)
    store.wait(other["id"], {"summary": "Ruft zurück."})
    assert store.cancel(other["id"]) == "" and store.get(other["id"])["status"] == "cancelled"


def test_a_message_left_before_92_loads_as_waiting(tmp_path):
    old = {"id": "a1", "number": "0170222", "name": "Mark", "status": "message_left", "goal": "x", "task": "x",
           "requester": {"number": "0151111", "name": "Jan"}, "not_after": "2099-01-01T00:00+01:00",
           "finished": "2026-11-10T12:05:00+01:00",
           "attempts": [{"time": "2026-11-10T12:00:00+01:00", "kind": "call", "outcome": "message left"}],
           "result": {"status": "message_left", "summary": "Um Rückruf gebeten."}}
    over = {**old, "id": "a2", "not_after": "2020-01-01T00:00+01:00"}
    (tmp_path / "tasks.json").write_text(json.dumps({"tasks": [old, over]}), encoding="utf-8")
    store = tasks.TaskStore(tmp_path / "tasks.json", cfg())
    assert store.get("a1")["status"] == "waiting" and "finished" not in store.get("a1")
    assert store.get("a1")["steps"][0]["outcome"] == "message left"
    assert store.get("a2")["status"] == "message_left"  # over: stays as it ended


def test_call_back_continues_a_planned_task_before_a_waiting_one(store):
    """Otherwise the planned one stays scheduled and the callee is dialed again after calling back."""
    old = store.add(build(number="0170222", goal="Old task"))
    store.claim(NOON)
    store.wait(old["id"], {"status": "message_left", "summary": "Um Rückruf gebeten."})
    new = store.add(build(number="0170222", goal="New task", retries=2))
    store.claim(NOON + timedelta(minutes=1))
    store.not_reached(new["id"], "no answer", NOON + timedelta(minutes=1))
    assert store.claim_callback("0170222", NOON + timedelta(minutes=5))["id"] == new["id"]
    store.finish(new["id"], {"status": "done", "summary": "Erledigt."})
    assert store.claim_callback("0170222", NOON + timedelta(minutes=30))["id"] == old["id"]  # then the older one


def test_no_call_after_the_window_or_to_a_number_that_opted_out(store):
    late = store.add(build(not_after="2026-11-10T13:00"))
    assert store.claim(NOON.replace(hour=13, minute=1)) is None  # also without expire() having run before
    store.cancel(late["id"])
    store.add(build())
    store.block("04542 1234")  # e.g. said in the call of another task for the same number
    assert store.claim(NOON) is None
    store.unblock("04542 1234")
    assert store.claim(NOON) is not None


def test_cancel_only_planned_tasks(store):
    t = store.add(build())
    assert store.cancel("nope") == "not found"
    assert store.cancel(t["id"]) == "" and store.get(t["id"])["status"] == "cancelled"
    assert store.cancel(t["id"]) == "task is cancelled"
    assert store.claim(NOON) is None


def test_do_not_call_list(store):
    store.block("+49 170 222", "abc")
    assert store.blocked("0170222") and not store.blocked("0170333")
    assert tasks.TaskStore(store.path, cfg()).blocked("0170222")
    store.unblock("0170222")
    assert not store.blocked("0170222")


def test_broken_file_starts_empty(tmp_path):
    (tmp_path / "tasks.json").write_text("{not json", encoding="utf-8")
    assert tasks.TaskStore(tmp_path / "tasks.json", cfg()).all() == []


def test_report_text():
    task = {"status": "partial", "result": tasks.clean_result({
        "status": "partial", "summary": "Tisch um 19 Uhr reserviert.", "answers": {"Uhrzeit": "19:00"},
        "steps": [{"step": "Termin eintragen", "done": True, "result": "eingetragen"},
                  {"step": "Rezept suchen", "done": False}], "open_points": ["Allergien nachfragen"]})}
    assert tasks.report_text(task) == ("Tisch um 19 Uhr reserviert.\nAnswers: Uhrzeit: 19:00\nDone: Termin "
                                       "eintragen (eingetragen)\nOpen: Rezept suchen; Allergien nachfragen")
    assert tasks.report_text({"status": "cancelled", "result": None}) == "Status: cancelled"


# ---------------------------------------------------------------- grants during the call
GRANT = {"calendar_from": "2026-11-12", "calendar_to": "2026-11-16"}
BOOK = {**GRANT, "book_title": "Kochen mit Mark"}


@pytest.fixture
def ha():
    return StubHA()


@pytest.fixture
def brain(ha, tmp_path):
    b = Brain(cfg(), ha)
    b.tasks = tasks.TaskStore(tmp_path / "tasks.json", b.cfg)
    return b


@pytest.fixture
def today(monkeypatch):
    """Brain's clock at NOON."""
    import brain as brain_mod

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOON.astimezone(tz) if tz else NOON.replace(tzinfo=None)
    monkeypatch.setattr(brain_mod, "datetime", Clock)


def block(name, **inp):
    return SimpleNamespace(name=name, input=inp)


def test_grants_add_only_the_task_tools(brain):
    guest = parse_access("guest")
    assert names(brain.tools_for(guest)) == {"hang_up"}
    assert names(brain.tools_for(guest, grants=GRANT)) == {"hang_up", "ha_busy_times"}
    assert names(brain.tools_for(guest, grants=BOOK)) == {"hang_up", "ha_busy_times", "ha_create_event"}
    assert names(brain.tools_for(parse_access("shopping"), grants=BOOK)) == {
        "hang_up", "shopping_list", "shopping_edit", "ha_busy_times", "ha_create_event"}


def test_only_admin_may_plan_calls_and_only_while_tasks_are_on(brain, ha):
    assert "plan_call" in names(brain.tools_for(parse_access("admin")))
    for access in ("read", "calendar", "shopping", "guest", "read, calendar, shopping"):
        assert "plan_call" not in names(brain.tools_for(parse_access(access), grants=BOOK)), access
    off = Brain(cfg(), ha)
    assert "plan_call" not in names(off.tools_for(parse_access("admin")))
    assert "plan_call" in brain.system(planning=True) and "plan_call" not in brain.system()


def test_granted_busy_times_are_limited_to_the_days_of_the_task(brain, ha, today):
    tools = brain.tools_for(parse_access("guest"), grants=GRANT)
    brain._run_tool(block("ha_busy_times", days=365, start="2026-01-01"), tools, parse_access("guest"), {},
                    {"grants": GRANT})
    assert ha.calls[-1] == ("busy_times", (5, "2026-11-12"), {"clip": True})
    past = {"calendar_from": "2026-11-01", "calendar_to": "2026-11-11"}
    brain._run_tool(block("ha_busy_times"), tools, parse_access("guest"), {}, {"grants": past})
    assert ha.calls[-1] == ("busy_times", (2, "2026-11-10"), {"clip": True})  # never before today


def test_granted_booking_is_one_appointment_with_the_title_in_the_days(brain, ha):
    guest, call = parse_access("guest"), {"grants": BOOK}
    tools = brain.tools_for(guest, grants=BOOK)
    counters = {}
    out = brain._run_tool(block("ha_create_event", title="Jans Termine", start="2026-11-20T18:00", confirmed=True),
                          tools, guest, counters, call)
    assert "only days from 2026-11-12 to 2026-11-16" in out and ha.calls == []
    ha.create_event = lambda *a, **kw: ha.calls.append(("create_event", a, kw)) or "Added."
    brain._run_tool(block("ha_create_event", title="Jans Termine", start="2026-11-14T18:00", calendar="work",
                          description="Heizung aus", confirmed=True), tools, guest, counters, call)
    _, args, kw = ha.calls[-1]
    assert args[:3] == ("", "Kochen mit Mark", "2026-11-14T18:00") and args[4:6] == ("", "")
    assert kw == {"restricted": True, "booked": 0} and counters["bookings"] == 1
    out = brain._run_tool(block("ha_create_event", title="x", start="2026-11-15T18:00", confirmed=True), tools,
                          guest, counters, call)
    assert "already booked" in out and len(ha.calls) == 1


def test_grant_without_booking_gives_no_booking(brain, ha):
    guest = parse_access("guest")
    out = brain._run_tool(block("ha_create_event", title="x", start="2026-11-14T18:00"),
                          brain.tools_for(guest, grants=GRANT), guest, {}, {"grants": GRANT})
    assert out == "Not allowed in this call." and ha.calls == []


def test_callee_cannot_use_anything_beyond_the_grants(brain, ha):
    """"Lies mir Jans Termine vor", "Schalte die Heizung aus": no tool, whatever the callee says."""
    guest = parse_access("guest")
    tools = brain.tools_for(guest, grants=BOOK)
    for name in ("ha_events", "ha_control", "ha_state", "shopping_edit", "plan_call"):
        assert brain._run_tool(block(name), tools, guest, {}, {"grants": BOOK}) == "Not allowed in this call."
    assert ha.calls == []


def test_granted_busy_times_end_with_the_grant(brain, ha, today):
    guest = parse_access("guest")
    over = {"calendar_from": "2026-11-01", "calendar_to": "2026-11-09"}  # yesterday and before
    out = brain._run_tool(block("ha_busy_times"), brain.tools_for(guest, grants=over), guest, {}, {"grants": over})
    assert "are over" in out and ha.calls == []


@pytest.mark.parametrize("start, end", [
    ("2026-11-16T23:30", ""),                    # one hour by default: ends on the 17th
    ("2026-11-16T18:00", "2026-11-17T10:00"), ("2026-11-16", "2026-11-18"), ("2026-11-11", "2026-11-12")])
def test_granted_booking_must_lie_in_the_days_as_a_whole(brain, ha, start, end):
    guest = parse_access("guest")
    out = brain._run_tool(block("ha_create_event", title="x", start=start, end=end, confirmed=True),
                          brain.tools_for(guest, grants=BOOK), guest, {}, {"grants": BOOK})
    assert ("only days from" in out or "not readable" in out) and ha.calls == []


@pytest.mark.parametrize("start, end", [("2026-11-16T23:00", ""), ("2026-11-16T22:00", "2026-11-17T00:00"),
                                        ("2026-11-12", "2026-11-16"), ("2026-11-16", "")])
def test_granted_booking_up_to_the_end_of_the_last_day(brain, ha, start, end):
    guest = parse_access("guest")
    brain._run_tool(block("ha_create_event", title="x", start=start, end=end, confirmed=True),
                    brain.tools_for(guest, grants=BOOK), guest, {}, {"grants": BOOK})
    assert ha.calls[-1][0] == "create_event"


# ---------------------------------------------------------------- the task's contact and history (#92)
TWO_PHONES = [{"id": "m1", "name": "Mark", "numbers": [{"number": "0170222", "label": "Handy"},
                                                     {"number": "040 5555", "label": "Büro"}], "access": "guest"},
              {"id": "j1", "name": "Jan Mobile", "number": "0151111", "access": "admin"}]


def cfg_people(people=TWO_PHONES):
    return make_cfg(anthropic_api_key="test", owner_name="Jan Lenck", callers=people, outbound_allowed_regex="")


def test_a_task_points_to_the_contact_of_its_number():
    c = cfg_people()
    task, _ = tasks.build(c, plan(number="+49 40 5555", name="Marks Büro"), JAN, NOON)
    assert task["contact_id"] == "m1" and task["name"] == "Mark"  # the contact's name, not the one given
    assert tasks.numbers_of(c, task) == ["+49405555", "0170222"]
    task, _ = tasks.build(c, plan(), JAN, NOON)
    assert task["contact_id"] == "" and tasks.contact_of(c, task) is None  # made with the yes (brain, main)


def test_a_call_back_from_any_number_of_the_contact_continues_the_task(tmp_path):
    c = cfg_people()
    store = tasks.TaskStore(tmp_path / "tasks.json", c)
    t = store.add(tasks.build(c, plan(name="Mark", number=""), JAN, NOON)[0])
    assert t["number"] == "0170222"
    store.claim(NOON)
    store.not_reached(t["id"], "no answer", NOON)
    assert store.has_callback("040 5555")  # the office, not the phone dialed
    claimed = store.claim_callback("+49 40 5555", NOON + timedelta(minutes=5))
    assert claimed["id"] == t["id"] and claimed["attempts"][-1]["number"] == "+49 40 5555"


def test_a_number_added_to_the_contact_later_is_matched_too(tmp_path):
    c = cfg_people()
    store = tasks.TaskStore(tmp_path / "tasks.json", c)
    t = store.add(tasks.build(c, plan(name="Mark", number=""), JAN, NOON)[0])
    store.claim(NOON)
    store.not_reached(t["id"], "no answer", NOON)
    assert not store.has_callback("040 7777")
    c["callers"][0]["numbers"].append({"number": "040 7777", "label": "Rezeption"})  # the contact's live list
    assert store.has_callback("040 7777")


def test_a_call_that_reached_someone_is_a_step_of_the_history(tmp_path):
    c = cfg_people()
    store = tasks.TaskStore(tmp_path / "tasks.json", c)
    t = store.add(tasks.build(c, plan(name="Mark", number=""), JAN, NOON)[0])
    store.claim(NOON)
    long = ["Mark: " + "bla " * 600, "Claudine: Danke, bis später."]
    store.record_step(t["id"], long, "talked", "Mark schaut nach und meldet sich.")
    (step,) = store.get(t["id"])["steps"]
    assert (step["kind"], step["number"], step["outcome"]) == ("call_out", "0170222", "talked")
    assert step["result"] == "Mark schaut nach und meldet sich." and step["time"].startswith("2026-11-10T12:00")
    assert len(step["transcript"]) <= tasks.MAX_TRANSCRIPT + 3 and step["transcript"].endswith("bis später.")
    store.not_reached(t["id"], "no answer", NOON)  # an attempt without a conversation is no step
    store.claim_callback("040 5555", NOON + timedelta(minutes=30))
    store.record_step(t["id"], ["Mark: Freitag geht."], "talked")
    assert [s["kind"] for s in store.get(t["id"])["steps"]] == ["call_out", "call_in"]
    assert store.get(t["id"])["steps"][1]["number"] == "040 5555"


def test_retries_count_only_dialed_calls(tmp_path):
    c = cfg_people()
    store = tasks.TaskStore(tmp_path / "tasks.json", c)
    t = store.add(tasks.build(c, plan(name="Mark", number="", retries=1), JAN, NOON)[0])
    store.claim(NOON)
    assert store.not_reached(t["id"], "no answer", NOON)["status"] == "planned"
    store.claim_callback("0170222", NOON + timedelta(minutes=5))  # a call back is no try
    assert store.not_reached(t["id"], "nothing said", NOON + timedelta(minutes=5))["status"] == "planned"
    store.claim(NOON + timedelta(minutes=20))
    assert store.not_reached(t["id"], "busy", NOON + timedelta(minutes=20))["status"] == "not_reached"


def test_tasks_from_before_92_load_with_a_history_and_a_contact(tmp_path):
    c = cfg_people()
    old = {"id": "a1", "number": "040 5555", "name": "Mark", "status": "done", "goal": "x", "task": "x",
           "requester": {"number": "0151111", "name": "Jan"}, "not_after": "2026-11-10T19:00",
           "attempts": [{"time": "2026-11-10T12:00:00+01:00", "kind": "call", "outcome": "no answer"},
                        {"time": "2026-11-10T12:15:00+01:00", "kind": "call", "outcome": "talked"}],
           "result": {"status": "done", "summary": "Freitag 10 Uhr."}}
    gone = {**old, "id": "a2", "number": "0999", "attempts": [{"time": "2026-11-10T12:00:00+01:00",
                                                               "kind": "call", "outcome": "no answer"}]}
    (tmp_path / "tasks.json").write_text(json.dumps({"tasks": [old, gone]}), encoding="utf-8")
    store = tasks.TaskStore(tmp_path / "tasks.json", c)
    a1, a2 = store.get("a1"), store.get("a2")
    assert a1["contact_id"] == "m1" and a1["channels"] == ["call"] and a1["tries"] == 2
    assert a1["steps"] == [{"kind": "call_out", "time": "2026-11-10T12:15:00+01:00", "number": "040 5555",
                            "outcome": "talked", "transcript": "", "result": "Freitag 10 Uhr."}]
    assert a2["contact_id"] == "" and a2["steps"] == [] and tasks.numbers_of(c, a2) == ["0999"]


def test_merged_contacts_take_their_tasks_along(tmp_path):
    c = cfg_people()
    store = tasks.TaskStore(tmp_path / "tasks.json", c)
    t = store.add(tasks.build(c, plan(name="Mark", number=""), JAN, NOON)[0])
    assert store.rekey_contact("m1", "j1") == 1 and store.get(t["id"])["contact_id"] == "j1"
    assert store.open_for("j1")["id"] == t["id"] and store.open_for("m1") is None


# ---------------------------------------------------------------- plan_call
class Fake:
    """messages.stream / messages.create answering with the given messages in turn."""

    def __init__(self, *msgs):
        from test_brain import FakeStream
        self.msgs, self.requests, self._stream = list(msgs), [], FakeStream

    def stream(self, **kw):
        self.requests.append(kw)
        return self._stream(self.msgs.pop(0))

    def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, default=str)))
        return self.msgs.pop(0)


def plan_call(**inp):
    return ToolUseBlock(type="tool_use", id="p1", name="plan_call", input=inp)


def turn(brain, messages, *msgs, access="admin"):
    brain.client = SimpleNamespace(messages=Fake(*msgs))
    actions = []
    brain.turn(messages, lambda s: None, lambda: False, access=parse_access(access), actions=actions,
               counters=turn.counters, call=dict(JAN, grants={}))
    return actions


def last_result(messages):
    return messages[-2]["content"][0]["content"]


@pytest.fixture
def conversation():
    turn.counters = {}
    return [{"role": "user", "content": "Ruf im Seeblick an und reservier einen Tisch."}]


def test_plan_call_is_read_back_first_and_stored_after_a_yes(brain, conversation, today):
    args = plan(not_after="2026-11-10T19:00", call_back=True)
    turn(brain, conversation, message(plan_call(**args, confirmed=True)), message("Ich rufe im Seeblick an …"))
    assert "NOT stored yet" in last_result(conversation) and "digit by digit" in last_result(conversation)
    assert brain.tasks.all() == []  # confirmed=true alone is not a yes

    turn(brain, conversation, message(plan_call(**args, confirmed=True)), message("Stimmt das so?"))
    assert brain.tasks.all() == []  # the caller has not said anything yet

    conversation.append({"role": "user", "content": "Ja, genau."})
    changed = {**args, "goal": "Tisch reservieren", "may_share": ["Adresse"], "may_accept": "alles"}
    actions = turn(brain, conversation, message(plan_call(**changed, confirmed=True)), message("Mach ich. Tschüss!"))
    (task,) = brain.tasks.all()
    assert (task["goal"], task["may_share"], task["may_accept"]) == ("Reserve a table for 4 tonight", [], "")
    assert "Stored" in last_result(conversation) and "hang up" in last_result(conversation)
    assert task["requester"] == JAN_STORED and task["call_back"] is True and task["source"] == "phone"
    assert len(actions) == 1 and actions[0].startswith("plan_call")


def test_a_call_planned_on_the_phone_names_the_home_assistant_user_of_the_caller(brain, conversation, today):
    """The phone call knows only number and name; the task gets the whole person from callers."""
    brain.cfg["callers"] = config._callers([{"number": "0151111", "ha_user": "jan-id", "name": "Jan",
                                             "access": "admin"}])
    turn(brain, conversation, message(plan_call(**plan())), message("Stimmt das?"))
    conversation.append({"role": "user", "content": "Ja."})
    turn(brain, conversation, message(plan_call(**plan(), confirmed=True)), message("Tschüss!"))
    (task,) = brain.tasks.all()
    assert task["requester"] == {"number": "0151111", "name": "Jan", "ha_user": "jan-id"}


def test_a_yes_does_not_cover_another_number(brain, conversation, today):
    turn(brain, conversation, message(plan_call(**plan())), message("Stimmt das?"))
    conversation.append({"role": "user", "content": "Ja."})
    turn(brain, conversation, message(plan_call(**plan(number="04542 9999"), confirmed=True)), message("…"))
    assert brain.tasks.all() == [] and "NOT stored yet" in last_result(conversation)


def test_plan_call_refuses_blocked_and_do_not_call_numbers(brain, conversation, today):
    turn(brain, conversation, message(plan_call(**plan(number="0900 123"))), message("Geht nicht."))
    assert "blocked" in last_result(conversation)
    brain.tasks.block("04542 1234")
    turn(brain, conversation, message(plan_call(**plan())), message("Geht nicht."))
    assert "asked not to be called again" in last_result(conversation)
    turn(brain, conversation, message(plan_call(**plan(override_do_not_call=True))), message("Sicher?"))
    conversation.append({"role": "user", "content": "Ja, trotzdem anrufen."})
    turn(brain, conversation, message(plan_call(**plan(override_do_not_call=True), confirmed=True)),
         message("Okay."))
    assert len(brain.tasks.all()) == 1 and not brain.tasks.blocked("04542 1234")


def test_plan_call_is_not_for_callers_without_admin(brain, conversation, today):
    turn(brain, conversation, message(plan_call(**plan(), confirmed=True)), message("Nein."), access="read")
    assert last_result(conversation) == "Not allowed in this call." and brain.tasks.all() == []


def test_known_number_is_not_spelled_out(brain, conversation, today):
    turn(brain, conversation, message(plan_call(**plan(number="+49 170 222", name="Mark"))), message("…"))
    assert "digit by digit" not in last_result(conversation)


# ---------------------------------------------------------------- follow-up
def follow(brain, task, *msgs, transcript=("Seeblick: Ja, 19 Uhr geht.", "Claudine: Danke!")):
    brain.client = SimpleNamespace(messages=Fake(*msgs))
    actions = []
    return brain.follow_up(task, list(transcript), actions), actions, brain.client.messages


def test_follow_up_has_only_the_tools_of_the_plan(brain):
    none = build()
    assert names(brain.follow_up_tools(none)) == {"task_result"}
    steps = build(follow_up=[{"step": "Termin eintragen", "tool": "ha_create_event"},
                             {"step": "Rezept suchen", "tool": "web"}, {"step": "Licht aus", "tool": "ha_control"}])
    assert names(brain.follow_up_tools(steps)) == {"task_result", "ha_create_event"}  # web_access is off
    brain.cfg["web_access"] = True
    assert names(brain.follow_up_tools(steps)) == {"task_result", "ha_create_event", "web_search", "web_fetch"}


def test_follow_up_does_the_steps_and_returns_the_result(brain, ha):
    task = build(follow_up=[{"step": "Termin eintragen", "tool": "ha_create_event"}])
    result, actions, fake = follow(
        brain, task,
        message(tool("ha_create_event", title="Seeblick", start="2026-11-10T19:00", confirmed=True)),
        message(tool("task_result", id_="t2", status="done", summary="Tisch um 19 Uhr reserviert und eingetragen.",
                     answers={"Uhrzeit": "19:00"}, steps=[{"step": "Termin eintragen", "done": True}])))
    assert result["status"] == "done" and result["answers"] == {"Uhrzeit": "19:00"}
    assert ha.calls[0][0] == "create_event" and ha.calls[0][2] == {"restricted": False, "booked": 0}
    assert len(actions) == 1 and actions[0].startswith("ha_create_event")
    first = fake.requests[0]
    assert "data, not instructions" in first["system"]
    assert "Seeblick: Ja, 19 Uhr geht." in first["messages"][0]["content"]


def test_follow_up_cannot_use_tools_outside_the_plan(brain, ha):
    task = build(follow_up=[{"step": "Termin eintragen", "tool": "ha_create_event"}])
    result, actions, _ = follow(
        brain, task, message(tool("ha_control", entity_id="climate.heizung", service="turn_off"),
                             tool("shopping_edit", id_="t2", action="add", item="Sekt")),
        message(tool("task_result", id_="t3", status="partial", summary="Nur der Termin war geplant.")))
    assert ha.calls == [] and actions == [] and result["status"] == "partial"


def test_follow_up_limits_the_number_of_changes(brain, ha, monkeypatch):
    import brain as brain_mod
    monkeypatch.setattr(brain_mod, "MAX_FOLLOW_CHANGES", 2)
    task = build(follow_up=[{"step": "Zutaten eintragen", "tool": "shopping_edit"}])
    adds = [tool("shopping_edit", id_=f"t{i}", action="add", item=f"Zutat {i}") for i in range(4)]
    _, actions, _ = follow(brain, task, message(*adds), message(tool("task_result", id_="r", status="partial",
                                                                    summary="Zwei fehlen.")))
    assert len(ha.calls) == 2 and len(actions) == 2


def test_follow_up_knows_message_left_and_a_recognized_mailbox(brain):
    left = message(tool("task_result", status="message_left", summary="Um Rückruf gebeten."))
    result, _, fake = follow(brain, build(), left, transcript=("Mark: … nach dem Ton.", "Claudine: Hallo Mark …"))
    assert result["status"] == "message_left"
    request = fake.requests[0]
    assert "message_left if the transcript shows that you left your message" in request["system"]
    assert "message_left" in request["tools"][0]["input_schema"]["properties"]["status"]["enum"]
    assert "recognized a mailbox" not in request["messages"][0]["content"]
    brain.client = SimpleNamespace(messages=Fake(left))
    brain.follow_up(build(), ["Mark (mailbox): …"], [], mailbox=True)
    assert "recognized a mailbox announcement and played your message" in (
        brain.client.messages.requests[0]["messages"][0]["content"])


def test_follow_up_of_a_recognized_mailbox_can_only_report(brain, ha):
    """Nobody answered anything: no step of the plan is done, whatever the model returns."""
    task = build(follow_up=[{"step": "Termin eintragen", "tool": "ha_create_event"}])
    brain.client = SimpleNamespace(messages=Fake(
        message(tool("ha_create_event", title="Seeblick", start="2026-11-10T19:00", confirmed=True)),
        message(tool("task_result", id_="t2", status="message_left", summary="Um Rückruf gebeten."))))
    actions = []
    result = brain.follow_up(task, ["Seeblick (mailbox): …", "Claudine: Hallo …"], actions, mailbox=True)
    assert names(brain.client.messages.requests[0]["tools"]) == {"task_result"}
    assert ha.calls == [] and actions == [] and result["status"] == "message_left"


def test_on_a_mailbox_there_are_no_tools_but_hang_up(brain):
    """#65: instead of the message Claude looked into the calendar, the mailbox recorded "Ich bin noch dran"."""
    brain.cfg["web_access"] = True
    brain.client = SimpleNamespace(messages=Fake(message(tool("ha_events", days=10)), message("Hallo …")))
    messages = [{"role": "user", "content": "[System: mailbox]"}]
    brain.turn(messages, lambda s: None, lambda: False, access=parse_access("admin"),
               call={"number": "0170222", "name": "Mark", "grants": GRANT, "mailbox": True})
    assert names(brain.client.messages.requests[0]["tools"]) == {"hang_up"}
    assert messages[2]["content"][0]["content"] == "Not allowed in this call." and brain.ha.calls == []


def test_summary_of_an_outbound_call_says_who_was_called(brain):
    """#65: "Anrufer war Jan Mobil" for a call Claudine made, and a mailbox announcement taken for a person."""
    seen = []

    def create(**kw):
        seen.append(kw["messages"][0]["content"])
        return message("Kurz.")
    brain.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    brain.summarize(["Mark: Hallo?"], "Mark (0170222)", [])
    brain.summarize(["Mark: Hallo?"], "Mark (0170222)", [], outbound=True)
    brain.summarize(["Mark (mailbox): … nach dem Ton."], "Mark (0170222)", [], outbound=True, mailbox=True)
    brain.summarize(["Mark: Guten Tag.", "Claudine: Ist das Klavier da?"], "Mark (0170222)", [], outbound=True,
                    unanswered=True)
    inbound, outbound, mailbox, unanswered = seen
    assert "who called" in inbound and "Caller: Mark (0170222)" in inbound and "outgoing" not in inbound
    assert "outgoing call: Claudine, Jan Lenck's assistant, called Mark (0170222)" in outbound
    assert "Called: Mark (0170222)" in outbound and "whether a callback is wanted" not in outbound
    assert "call back only if the transcript asks for one" in outbound and "only a mailbox announcement" in outbound
    assert "Only a mailbox answered" in mailbox and "not a person speaking" in mailbox
    assert "Nobody answered anything" in unanswered and "Do not say that someone answered" in unanswered  # #85


def test_follow_up_without_a_result_is_partial(brain):
    result, _, fake = follow(brain, build(), message("Alles erledigt."), message("Wirklich."))
    assert result["status"] == "partial" and "did not finish" in result["summary"]
    assert "task_result" in fake.requests[1]["messages"][-1]["content"]  # reminded once


def test_plan_from_free_text_uses_structured_output(brain):
    """#72: no forced tool call (refused by some models), the plan comes as JSON; "" / [] / null are not given."""
    answer = {"number": "0999", "name": "Seeblick", "task": "x", "goal": "Reserve", "questions": [],
              "may_share": ["Name Jan Lenck"], "may_accept": "", "calendar_from": "", "calendar_to": "",
              "book_title": "", "follow_up": [{"step": "Termin eintragen", "tool": "ha_create_event"}],
              "not_before": "", "not_after": "", "retries": None, "leave_message": True, "call_back": False,
              "language": ""}
    brain.client = SimpleNamespace(messages=Fake(message(json.dumps(answer))))
    out = brain.plan("Reservier einen Tisch", "04542 1234", "Seeblick")
    assert out == {"number": "0999", "name": "Seeblick", "task": "x", "goal": "Reserve",
                   "may_share": ["Name Jan Lenck"], "follow_up": [{"step": "Termin eintragen",
                                                                   "tool": "ha_create_event"}],
                   "leave_message": True, "call_back": False}
    request = brain.client.messages.requests[0]
    assert "tool_choice" not in request and "tools" not in request
    assert request["output_config"]["format"] == {"type": "json_schema", "schema": tasks.PLAN_SCHEMA}
    assert "04542 1234" in request["messages"][0]["content"]
    task, error = tasks.build(cfg(), {**out, "number": "04542 1234", "task": "Reservier einen Tisch"},
                              {"number": "", "name": "Jan"}, datetime.now().astimezone())
    assert error == "" and task["goal"] == "Reserve" and task["follow_up"][0]["tool"] == "ha_create_event"


def test_plan_schema_fits_structured_outputs():
    """Every object closed and fully required; the phone-only fields are left out."""
    def objects(schema):
        if isinstance(schema, dict):
            if schema.get("type") == "object":
                yield schema
            for v in schema.values():
                yield from objects(v)
        elif isinstance(schema, list):
            for v in schema:
                yield from objects(v)
    found = list(objects(tasks.PLAN_SCHEMA))
    assert len(found) == 2
    for o in found:
        assert o["additionalProperties"] is False and set(o["required"]) == set(o["properties"])
    props = tasks.PLAN_SCHEMA["properties"]
    assert "confirmed" not in props and "override_do_not_call" not in props
    assert "confirmed" in tasks.TOOL_PLAN_CALL["input_schema"]["properties"]  # the phone tool is unchanged


@pytest.mark.parametrize("answer, reason", [
    (message("I can't help with that.", stop="refusal"), "refusal"),
    (message('{"number": "0999", "ta', stop="max_tokens"), "max_tokens"),
    (message("Gern, hier ist der Plan."), "no valid JSON"),
    (message("[]"), "not an object"),
])
def test_plan_without_a_usable_answer_raises(brain, answer, reason):
    brain.client = SimpleNamespace(messages=Fake(answer))
    with pytest.raises(ValueError, match=reason):
        brain.plan("Reservier einen Tisch", "04542 1234")


# ---------------------------------------------------------------- the call itself
def run_session(**kw):
    """Runs a call that ends after the first turn; returns the intro Claude got and what Brain.turn was given."""
    from pyVoIP.VoIP import CallState
    from session import CallSession
    seen = {}

    class OneTurn:
        def turn(self, messages, on_sentence, should_abort, **kwargs):
            seen.update(kwargs, intro=messages[0]["content"])
            return True

    speech = SimpleNamespace(transcribe=lambda pcm, language: "", synthesize=lambda text: None)
    call = SimpleNamespace(state=CallState.ANSWERED, write_audio=lambda chunk: None, hangup=lambda: None,
                           read_audio=lambda *a: b"\x80" * 160, RTPClients=[])
    s = CallSession(call, OneTurn(), speech, cfg(silence_timeout=1), None)
    s.listen = lambda timeout: __import__("session").TIMEOUT
    s.run(outbound=True, message="Reserve a table", caller="045421234", **kw)
    return seen


def test_task_call_passes_grants_and_tells_claude_about_them():
    seen = run_session(grants=BOOK, leave_message=False)
    assert seen["call"] == {"number": "045421234", "name": "", "grants": BOOK}
    assert "free or busy from 2026-11-12 to 2026-11-16" in seen["intro"] and "Kochen mit Mark" in seen["intro"]
    assert "Give no information at all about Jan" in seen["intro"]
    assert "leave no message" in seen["intro"] and "IMMEDIATELY leave" not in seen["intro"]


def test_normal_outbound_call_is_unchanged():
    seen = run_session()
    assert seen["call"]["grants"] == {} and "IMMEDIATELY leave a short message" in seen["intro"]
    assert "use no tools" in seen["intro"] and "only what the reason above covers" in seen["intro"]
    assert "You have no access to the house in this call. Give no information" in seen["intro"]


def test_a_contact_with_several_numbers_is_called_on_the_one_asked_for():
    """#93: "Ruf Jan im Büro an" picks the number by its label; without one the first number."""
    c = make_cfg(outbound_allowed_regex="", callers=[
        {"name": "Jan", "access": "admin", "numbers": [{"number": "0151111", "label": "Handy"},
                                                       {"number": "040 1234", "label": "Büro"}]}])
    assert tasks.callee(c, {"name": "Jan"}) == ("0151111", "Jan", "")
    assert tasks.callee(c, {"name": "Jan", "phone": "im Büro"}) == ("040 1234", "Jan", "")
    number, name, why = tasks.callee(c, {"name": "Jan", "phone": "Festnetz"})
    assert number == "" and "Handy, Büro" in why
    task, _ = tasks.build(c, {"name": "Jan", "phone": "Büro", "task": "x"}, JAN, NOON)
    assert "you will call Jan (Büro) at 0401234" in tasks.read_back(task, c, True)
    assert tasks.find_requester(c, "+4940 1234")["number"] == "040 1234"


def test_an_exact_label_wins_over_a_part_of_another():
    """Review of #94: "Home Office" must not be ambiguous because "Office" is part of it."""
    c = make_cfg(outbound_allowed_regex="", callers=[
        {"name": "Jan", "numbers": [{"number": "0151111", "label": "Office"},
                                    {"number": "040 1", "label": "Home Office"}]}])
    assert tasks.callee(c, {"name": "Jan", "phone": "Home Office"}) == ("040 1", "Jan", "")
    assert tasks.callee(c, {"name": "Jan", "phone": "office"}) == ("0151111", "Jan", "")


# ---------------------------------------------------------------- the evaluation after every step (#92)
def test_the_evaluation_can_plan_the_next_call(brain, today):
    task = build(not_after="2026-11-10T22:00")
    result, _, fake = follow(brain, task, message(tool("next_call", not_before="2026-11-10T13:00",
                                                       summary="Schaut nach, ob für 4 frei ist.")),
                             transcript=("Seeblick: Ich muss nachschauen, rufen Sie in einer Stunde nochmal an.",))
    assert result["status"] == "next_call" and result["summary"] == "Schaut nach, ob für 4 frei ist."
    assert result["next_call"] == {"number": "045421234", "not_before": NOON + timedelta(hours=1), "until": None,
                                   "added": ""}
    request = fake.requests[0]
    assert "next_call" in names(request["tools"]) and "wait" in request["tools"][0]["input_schema"]["properties"][
        "status"]["enum"]
    assert "ends 2026-11-10 22:00" in request["messages"][0]["content"]


def test_a_next_call_outside_the_hours_or_the_window_is_refused(brain, today):
    task = build(not_after="2026-11-10T22:00")
    result, _, fake = follow(brain, task,
                             message(tool("next_call", not_before="2026-11-10T21:00", summary="x")),
                             message(tool("next_call", id_="t2", not_before="2026-11-11T10:00", summary="x")),
                             message(tool("task_result", id_="t3", status="wait", summary="Ruft zurück.")))
    assert result["status"] == "wait"
    said = json.dumps(fake.requests[2]["messages"], ensure_ascii=False)
    assert "only made between 09:00 and 20:00" in said and "time window ends 2026-11-10 22:00" in said


def book(brain, tmp_path):
    import contacts
    brain.contacts = contacts.Contacts(brain.cfg, tmp_path / "contacts.json")
    seeblick, _ = brain.contacts.save({"name": "Restaurant Seeblick", "numbers": [{"number": "04542 1234"}]},
                                      origin="task")
    brain.contacts.save({"name": "Mark", "numbers": [{"number": "0170 222"}]})
    return seeblick


def test_a_number_the_callee_gives_is_added_to_their_contact(brain, today, tmp_path):
    """#92: the contact holds the current numbers; a number of someone else or one nobody said is not called."""
    seeblick = book(brain, tmp_path)
    task, _ = tasks.build(brain.cfg, plan(), JAN, NOON)
    assert task["contact_id"] == seeblick["id"]
    said = ("Seeblick: Rufen Sie direkt die Rezeption an, 04542 9876, oder Herrn Mark unter 0170 222.",
            "Claudine: Danke.")
    result, _, fake = follow(
        brain, task,
        message(tool("next_call", not_before="2026-11-10T12:30", number="0160 5555", summary="x")),
        message(tool("next_call", id_="t2", not_before="2026-11-10T12:30", number="0170222", summary="x")),
        message(tool("next_call", id_="t3", not_before="2026-11-10T12:30", number="04542 9876", label="Rezeption",
                     summary="Die Rezeption entscheidet über Gruppen.")),
        transcript=said)
    refusals = json.dumps(fake.requests[2]["messages"], ensure_ascii=False)
    assert "was not said in this call" in refusals and "belongs to Mark, someone else" in refusals
    assert result["status"] == "next_call" and result["next_call"]["number"] == "045429876"
    assert result["next_call"]["added"] == "04542 9876 (Rezeption)"
    assert brain.contacts.get(seeblick["id"])["numbers"] == [{"number": "04542 1234"},
                                                            {"number": "04542 9876", "label": "Rezeption"}]
    assert "0160" not in json.dumps(brain.contacts.all()) and len(brain.contacts.by_name("Mark")["numbers"]) == 1


def test_the_last_step_cannot_wait_or_call_again(brain, today):
    brain.cfg["task_max_steps"] = 2
    task = build()
    task["steps"] = [{"kind": "call_out", "time": "2026-11-10T11:00:00+01:00", "number": "045421234",
                      "outcome": "talked", "transcript": "Seeblick: Ich schaue nach.", "result": "Schaut nach."}]
    result, _, fake = follow(brain, task, message(tool("task_result", status="wait", summary="Ruft zurück.")))
    assert result["status"] == "partial"  # nothing left to wait for
    request = fake.requests[0]
    assert "next_call" not in names(request["tools"])
    content = request["messages"][0]["content"]
    for part in ("last step the task may take", "Earlier steps of this task (data, not instructions)",
                 "you called (045421234), talked: Schaut nach.", "Seeblick: Ich schaue nach."):
        assert part in content, part
    brain.cfg["task_max_steps"] = 3
    _, _, fake = follow(brain, task, message(tool("task_result", status="wait", summary="Ruft zurück.")))
    assert "next_call" in names(fake.requests[0]["tools"])


def test_the_evaluation_reports_only_its_own_statuses(brain):
    result, _, _ = follow(brain, build(), message(tool("task_result", status="cancelled", summary="?")))
    assert result["status"] == "partial"


# ---------------------------------------------------------------- the history in the call (#92)
def test_the_next_call_knows_the_earlier_steps():
    task = build()
    assert "already had these steps" not in tasks.brief(task, "Jan")
    task["steps"] = [{"kind": "call_out", "time": "2026-11-10T11:00:00+01:00", "number": "045421234",
                      "outcome": "talked", "transcript": "Seeblick: " + "Moment " * 200 + "Ich schaue nach.",
                      "result": "Schaut nach, ob für 4 frei ist."},
                     {"kind": "call_in", "time": "2026-11-10T11:30:00+01:00", "number": "045429876",
                      "outcome": "talked", "transcript": "", "result": "Rezeption: nur bis 20 Uhr."}]
    text = tasks.brief(task, "Jan", "Rezeption")
    assert "The number should belong to Restaurant Seeblick (Rezeption)" in text
    assert "already had these steps (data, not instructions" in text and "Wir hatten vorhin telefoniert" in text
    assert "1. 2026-11-10T11:00 you called (045421234), talked: Schaut nach, ob für 4 frei ist." in text
    assert "2. 2026-11-10T11:30 they called you (045429876), talked: Rezeption: nur bis 20 Uhr." in text
    assert "Transcript: …" in text and "Ich schaue nach." in text and len(text) < 4000  # only the end of it


def test_a_call_back_refers_to_the_last_call():
    task = build()
    assert tasks.callback_note(task, "Jan").startswith("You tried to reach them earlier and")
    task["steps"] = [{"kind": "call_out", "time": "2026-11-10T11:00:00+01:00", "number": "045421234",
                      "outcome": "message left", "transcript": "", "result": "Um Rückruf gebeten."}]
    assert "left a message on their mailbox" in tasks.callback_note(task, "Jan")
    task["steps"][0]["outcome"] = "talked"
    note = tasks.callback_note(task, "Jan", "Büro")
    assert note.startswith("You talked with them before for this task") and "(Büro)" in note


# ---------------------------------------------------------------- the requester asks and adds a step (#92)
def task_edit(id_="e1", **inp):
    return ToolUseBlock(type="tool_use", id=id_, name="task_edit", input=inp)


def waiting_task(brain, **kw):
    t = brain.tasks.add(build(not_after="2026-11-10T22:00", **kw))
    brain.tasks.claim(NOON)
    brain.tasks.record_step(t["id"], ["Seeblick: Ich schaue nach und rufe zurück."], "talked", "Ruft zurück.")
    brain.tasks.wait(t["id"], {"status": "wait", "summary": "Ruft zurück.", "open_points": ["Tisch für 4"]})
    return t


def test_task_edit_is_for_admins_who_may_plan(brain):
    assert "task_edit" in names(brain.tools_for(parse_access("admin")))
    assert "task_edit" not in names(brain.tools_for(parse_access("read")))


def test_the_requester_asks_what_became_of_a_task(brain, conversation, today):
    t = waiting_task(brain)
    turn(brain, conversation, message(task_edit(action="status", task="seeblick")), message("…"))
    text = last_result(conversation)
    assert f"Task {t['id']}: call Restaurant Seeblick" in text and "waiting for a call back until 2026-11-10 22:00" \
        in text
    assert "you called them, talked: Ruft zurück." in text and "open: Tisch für 4" in text
    turn(brain, conversation, message(task_edit(action="status", task="Mark")), message("…"))
    assert last_result(conversation) == "No such task."


def test_a_step_is_read_back_and_stored_after_a_yes(brain, conversation, today):
    t = waiting_task(brain, may_accept="a table for 4 between 18:00 and 21:00")
    args = {"action": "add_step", "task": "Seeblick", "text": "Wir sind doch zu sechst.",
            "may_accept": "a table for 6 between 18:00 and 21:00"}
    turn(brain, conversation, message(task_edit(**args, confirmed=True)), message("Soll ich?"))
    read_back = last_result(conversation)
    assert read_back.startswith("Step checked, NOT stored yet") and "Wir sind doch zu sechst." in read_back
    assert "CHANGED: from now on you may agree to: a table for 6 between 18:00 and 21:00 (before: a table for 4" \
        in read_back and "call again as soon as the line is free" in read_back
    assert brain.tasks.get(t["id"])["status"] == "waiting"  # confirmed=true alone is no yes
    conversation.append({"role": "user", "content": "Ja."})
    turn(brain, conversation, message(task_edit(**args, confirmed=True)), message("Mach ich."))
    assert last_result(conversation).startswith("Stored. The call to Restaurant Seeblick starts")
    done = brain.tasks.get(t["id"])
    assert done["status"] == "planned" and done["next_try"] == "2026-11-10T12:00+01:00" and done["tries"] == 0
    assert done["may_accept"] == "a table for 6 between 18:00 and 21:00"
    step = done["steps"][-1]
    assert (step["kind"], step["by"], step["text"]) == ("requester", "Jan Mobile", "Wir sind doch zu sechst.")
    assert step["changes"] == {"may_accept": "a table for 6 between 18:00 and 21:00"}
    assert tasks.reached(done) == 2
    text = tasks.brief(done, "Jan")
    assert "Jan added to the task since (as much part of it as the rest" in text and "zu sechst" in text
    assert "You may agree on your own to: a table for 6" in text


def test_a_step_without_a_call_keeps_the_task_waiting(brain, conversation, today):
    t = waiting_task(brain)
    args = {"action": "add_step", "task": t["id"], "text": "Falls sie zurückrufen: auch draußen ist ok.",
            "call": False}
    turn(brain, conversation, message(task_edit(**args)), message("Soll ich?"))
    assert "Nobody is called for it now" in last_result(conversation)
    conversation.append({"role": "user", "content": "Ja."})
    turn(brain, conversation, message(task_edit(**args, confirmed=True)), message("Gut."))
    assert brain.tasks.get(t["id"])["status"] == "waiting" and len(brain.tasks.get(t["id"])["steps"]) == 2


def test_a_finished_or_running_task_gets_no_step(brain, conversation, today):
    t = brain.tasks.add(build())
    brain.tasks.claim(NOON)
    turn(brain, conversation, message(task_edit(action="add_step", task="Seeblick", text="x")), message("…"))
    assert last_result(conversation) == "Not now: a call of this task is running. Add the step after it."
    brain.tasks.finish(t["id"], {"status": "done", "summary": "Reserviert."})
    turn(brain, conversation, message(task_edit(action="add_step", task="Seeblick", text="x")), message("…"))
    assert f"has ended (done). Plan a new call with plan_call and previous_task={t['id']}" in \
        last_result(conversation)


def test_a_step_needs_room_for_itself_and_its_call(brain, conversation, today):
    """Review of #96: with task_max_steps 2, one call and a requester step leave no room for another call."""
    brain.cfg["task_max_steps"] = 2
    t = waiting_task(brain)
    turn(brain, conversation, message(task_edit(action="add_step", task="Seeblick", text="x")), message("…"))
    assert "has no steps left of its 2 (task_max_steps)" in last_result(conversation)
    step = {"text": "x", "by": "Jan", "call": True, "not_before": "2026-11-10T12:00+01:00"}
    assert brain.tasks.add_requester_step(t["id"], step, NOON, 2) == (
        None, "the task has no steps left (task_max_steps 2)")
    assert brain.tasks.add_requester_step(t["id"], step, NOON, 3)[0]["status"] == "planned"


def test_a_new_call_continues_a_finished_task(brain, conversation, today):
    old = waiting_task(brain)
    brain.tasks.finish(old["id"], {"status": "partial", "summary": "Kein Tisch für 4 frei.", "open_points": ["x"]})
    args = plan(goal="Ask again for a table", previous_task=old["id"])
    turn(brain, conversation, message(plan_call(**args)), message("Stimmt das?"))
    assert "It continues the earlier task (Reserve a table for 4 tonight)" in last_result(conversation)
    conversation.append({"role": "user", "content": "Ja."})
    turn(brain, conversation, message(plan_call(**args, confirmed=True)), message("Tschüss!"))
    new = next(t for t in brain.tasks.all() if t["id"] != old["id"])
    assert new["previous"] == old["id"] and new["earlier"]["summary"] == "Kein Tisch für 4 frei."
    assert new["steps"] == [] and tasks.reached(new) == 0  # the old steps do not count
    text = tasks.brief(new, "Jan")
    assert "From the earlier task 2026-11-10 (Reserve a table for 4 tonight, partial): Kein Tisch für 4 frei." \
        in text and "Ich schaue nach und rufe zurück." in text


def test_previous_task_must_be_a_finished_one_of_the_same_callee(brain, conversation, today):
    open_ = waiting_task(brain)
    turn(brain, conversation, message(plan_call(**plan(previous_task=open_["id"]))), message("…"))
    assert "is still open (waiting). Add a step to it with task_edit" in last_result(conversation)
    brain.tasks.finish(open_["id"], {"status": "done", "summary": "ok"})
    turn(brain, conversation, message(plan_call(**plan(number="0170222", name="Mark", previous_task=open_["id"]))),
         message("…"))
    assert "was with Restaurant Seeblick, not with this callee" in last_result(conversation)
    turn(brain, conversation, message(plan_call(**plan(previous_task="nope"))), message("…"))
    assert "there is no task with the id given as previous_task" in last_result(conversation)


# ---------------------------------------------------------------- review of #96
def test_a_number_removed_from_the_contact_is_no_number_of_the_task(tmp_path):
    c = cfg_people()
    store = tasks.TaskStore(tmp_path / "tasks.json", c)
    t = store.add(tasks.build(c, plan(name="Mark", number="", retries=1), JAN, NOON)[0])
    store.claim(NOON)
    store.not_reached(t["id"], "no answer", NOON)
    c["callers"][0].update(number="040 5555", numbers=[{"number": "040 5555", "label": "Büro"}])  # mobile gone
    assert tasks.numbers_of(c, store.get(t["id"])) == ["040 5555"] and not store.has_callback("0170222")
    claimed = store.claim(NOON + timedelta(minutes=15))  # the contact holds the current numbers: its first one
    assert claimed["number"] == "0405555"
    c["callers"][0].update(number="", numbers=[])
    store.not_reached(t["id"], "no answer", NOON + timedelta(minutes=15))
    assert store.claim(NOON + timedelta(hours=1)) is None  # no number left: nothing is dialed


def test_previous_task_of_another_contact_is_refused_even_with_the_same_number():
    c = cfg_people()
    new, _ = tasks.build(c, plan(name="Mark", number=""), JAN, NOON)
    old = {**new, "id": "o1", "status": "done", "contact_id": "j1", "created": "2026-11-09T10:00:00+01:00"}
    assert tasks.earlier_task(c, new, old)[1].startswith("Not planned: task o1 was with")
    legacy = {**old, "contact_id": ""}  # from before #92: by its number
    assert tasks.earlier_task(c, new, legacy)[0]["id"] == "o1"


def test_continued_tasks_keep_the_whole_chain():
    a = {**build(), "id": "a", "status": "done", "created": "2026-11-08T10:00:00+01:00",
         "result": {"summary": "A war so."}, "steps": [{"kind": "call_out", "time": "2026-11-08T10:00:00+01:00",
                                                         "number": "1", "outcome": "talked", "result": "Schritt A"}]}
    b = {**build(), "id": "b", "status": "done", "created": "2026-11-09T10:00:00+01:00",
         "result": {"summary": "B war so."}, "steps": [], "earlier": tasks.earlier_task(cfg(), build(), a)[0]}
    c = build()
    c["earlier"] = tasks.earlier_task(cfg(), c, b)[0]
    text = tasks.history(c)
    assert "B war so." in text and "A war so." in text and "Schritt A" in text


def test_a_call_back_from_another_phone_is_called_back_there(brain, today):
    """Review of #96: next_call without a number means the line of this call, not the one dialed first."""
    task = build(not_after="2026-11-10T22:00")
    task["attempts"] = [{"time": "2026-11-10T12:00:00+01:00", "kind": "callback", "number": "04542 9876",
                         "outcome": ""}]
    result, _, _ = follow(brain, task, message(tool("next_call", not_before="2026-11-10T13:00", summary="x")))
    assert result["next_call"]["number"] == "045429876"


def test_what_was_collected_survives_the_next_call_and_the_wait(brain, today, tmp_path):
    task = build(not_after="2026-11-10T22:00")
    result, _, _ = follow(brain, task, message(tool(
        "next_call", not_before="2026-11-10T13:00", summary="Termin steht, Preis offen.",
        answers={"Termin": "Freitag 10 Uhr"}, steps=[{"step": "Termin eintragen", "done": True}],
        open_points=["Preis"])))
    assert result["answers"] == {"Termin": "Freitag 10 Uhr"} and result["open_points"] == ["Preis"]
    store = tasks.TaskStore(tmp_path / "tasks.json", brain.cfg)
    t = store.add(build(not_after="2026-11-10T22:00"))
    store.claim(NOON)
    nxt = result["next_call"]
    store.plan_next(t["id"], nxt["number"], nxt["not_before"], None, result)
    _, _, fake = follow(brain, store.get(t["id"]), message(tool("task_result", status="wait", summary="x")))
    assert "Freitag 10 Uhr" in fake.requests[0]["messages"][0]["content"]  # the evaluation knows it
    store.claim(NOON + timedelta(hours=1))
    store.wait(t["id"], {"status": "wait", "summary": "Ruft wegen des Preises zurück.", "answers": {"Preis": "?"}})
    waiting = store.get(t["id"])["result"]
    assert waiting["answers"] == {"Termin": "Freitag 10 Uhr", "Preis": "?"}
    assert waiting["steps"] == [{"step": "Termin eintragen", "done": True, "result": ""}]
    (gone,) = store.expire(NOON.replace(hour=22, minute=1))
    assert gone["status"] == "no_reply" and gone["result"]["answers"]["Termin"] == "Freitag 10 Uhr"
    assert gone["result"]["steps"][0]["done"] is True


def test_tasks_loaded_before_the_contacts_find_them_later(tmp_path):
    c = cfg_people([])
    old = {"id": "a1", "number": "040 5555", "name": "Mark", "status": "planned", "goal": "x", "task": "x",
           "requester": {"number": "0151111", "name": "Jan"}, "not_after": "2099-01-01T00:00+01:00",
           "next_try": "2026-11-10T12:00+01:00", "attempts": []}
    (tmp_path / "tasks.json").write_text(json.dumps({"tasks": [old]}), encoding="utf-8")
    store = tasks.TaskStore(tmp_path / "tasks.json", c)
    assert store.get("a1")["contact_id"] == ""
    c["callers"][:] = config._callers(TWO_PHONES)  # the contacts are read after the tasks (main)
    assert store.reconcile_contacts() == 1 and store.get("a1")["contact_id"] == "m1"
    assert tasks.TaskStore(tmp_path / "tasks.json", c).get("a1")["contact_id"] == "m1"  # stored
