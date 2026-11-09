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
    assert t["requester"] == JAN and t["grants"] == {} and t["follow_up"] == []


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
                 "not to be called again", "no audio is recorded", "Termin eintragen"):
        assert part in text, part


def test_clean_result_fixes_the_shape():
    r = tasks.clean_result({"status": "great", "summary": 5, "answers": ["x"], "steps": [{"step": "a", "done": 1}, 7],
                            "open_points": "eins", "do_not_call": 1})
    assert r == {"status": "partial", "summary": "5", "answers": {}, "steps": [{"step": "a", "done": True,
                 "result": ""}], "open_points": ["eins"], "do_not_call": True}
    assert tasks.clean_result(None)["status"] == "partial"


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
    assert task["requester"] == JAN and task["call_back"] is True and task["source"] == "phone"
    assert len(actions) == 1 and actions[0].startswith("plan_call")


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


def test_follow_up_without_a_result_is_partial(brain):
    result, _, fake = follow(brain, build(), message("Alles erledigt."), message("Wirklich."))
    assert result["status"] == "partial" and "did not finish" in result["summary"]
    assert "task_result" in fake.requests[1]["messages"][-1]["content"]  # reminded once


def test_plan_from_free_text_uses_the_forced_tool(brain):
    brain.client = SimpleNamespace(messages=Fake(message(plan_call(number="0999", task="x", goal="Reserve"))))
    out = brain.plan("Reservier einen Tisch", "04542 1234", "Seeblick")
    assert out["goal"] == "Reserve"
    request = brain.client.messages.requests[0]
    assert request["tool_choice"] == {"type": "tool", "name": "plan_call"}
    assert "04542 1234" in request["messages"][0]["content"]


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
    assert "You have no access to the house in this call. Give no information" in seen["intro"]
