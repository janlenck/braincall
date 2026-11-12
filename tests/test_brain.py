"""Brain: which tools a caller gets, tool execution, sentence splitting and the turn loop."""
import itertools
from types import SimpleNamespace

import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

import brain as brain_mod
from brain import Brain, SentenceSplitter
from config import ACCESS, parse_access
from conftest import make_cfg


class StubHA:
    """Records calls; every tool returns a short text."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.calls = []

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return f"{name} ok"
        return method


def names(tools):
    return {t["name"] for t in tools}


READ = {"ha_search", "ha_list", "ha_state", "ha_forecast", "ha_energy", "ha_statistics", "ha_history",
        "ha_automation", "ha_automations", "ha_events"}
SHOP = {"shopping_list", "shopping_edit"}


@pytest.fixture
def ha():
    return StubHA()


@pytest.fixture
def brain(ha):
    return Brain(make_cfg(anthropic_api_key="test", owner_name="Jan Lenck"), ha)


# ---------------------------------------------------------------- tools_for
@pytest.mark.parametrize("access, expected", [
    ("admin", {"hang_up", *READ, "ha_control", "ha_create_event", *SHOP}),
    ("guest", {"hang_up"}),
    ("read", {"hang_up", *READ}),
    ("calendar", {"hang_up", "ha_busy_times", "ha_create_event"}),
    ("read, calendar", {"hang_up", *READ, "ha_create_event"}),  # read sees details, no need for busy times
    ("shopping", {"hang_up", *SHOP}),
    ("shopping, calendar", {"hang_up", *SHOP, "ha_busy_times", "ha_create_event"}),
])
def test_tools_for(brain, access, expected):
    assert names(brain.tools_for(parse_access(access))) == expected


def test_no_access_combination_without_admin_can_control(brain):
    """The core promise: only admin may switch anything, whatever else is combined."""
    others = [a for a in ACCESS if a != "admin"]
    for n in range(1, len(others) + 1):
        for combo in itertools.combinations(others, n):
            tools = names(brain.tools_for(parse_access(list(combo))))
            assert "ha_control" not in tools, combo
            if "read" not in combo:
                assert not tools & READ, combo  # no details without read


def test_calendar_write_off(ha):
    b = Brain(make_cfg(anthropic_api_key="test", calendar_write=False), ha)
    assert "ha_create_event" not in names(b.tools_for(parse_access("calendar")))
    assert "ha_create_event" not in names(b.tools_for(parse_access("admin")))


def test_no_control_domains_means_no_control_tool(ha):
    b = Brain(make_cfg(anthropic_api_key="test", control_domains=[]), ha)
    assert "ha_control" not in names(b.tools_for(parse_access("admin")))


def test_without_home_assistant_only_hang_up():
    b = Brain(make_cfg(anthropic_api_key="test"), StubHA(enabled=False))
    for access in ("admin", "read", "calendar", "shopping", "guest"):
        assert names(b.tools_for(parse_access(access))) == {"hang_up"}, access


def test_busy_tool_names_the_owner(brain):
    busy = next(t for t in brain.tools_for(parse_access("calendar")) if t["name"] == "ha_busy_times")
    assert "Jan is busy" in busy["description"]
    assert "{owner}" in brain_mod.TOOL_BUSY["description"]  # the shared template stays untouched


# ---------------------------------------------------------------- _run_tool
def block(name, **inp):
    return SimpleNamespace(name=name, input=inp)


def test_run_tool_refuses_tools_not_given_to_the_caller(brain, ha):
    access = parse_access("guest")
    out = brain._run_tool(block("ha_control", entity_id="light.x", service="turn_on"),
                          brain.tools_for(access), access)
    assert out == "Not allowed in this call."
    assert ha.calls == []


def test_run_tool_create_event_is_restricted_for_non_admin(brain, ha):
    for access, restricted in (("calendar", True), ("admin", False)):
        ha.calls.clear()
        a = parse_access(access)
        brain._run_tool(block("ha_create_event", title="T", start="2030-01-01T10:00", confirmed=True),
                        brain.tools_for(a), a)
        assert ha.calls[0][0] == "create_event"
        assert ha.calls[0][2]["restricted"] is restricted


def test_run_tool_counts_restricted_bookings_per_call(brain, ha):
    ha.create_event = lambda *a, **k: ha.calls.append(k) or ("Added: T." if k["booked"] < 2 else "Not added")
    a = parse_access("calendar")
    counters = {}
    for _ in range(3):
        brain._run_tool(block("ha_create_event", title="T", start="2030-01-01T10:00", confirmed=True),
                        brain.tools_for(a), a, counters)
    assert [k["booked"] for k in ha.calls] == [0, 1, 2]
    assert counters == {"bookings": 2}  # the refused third booking does not count


def test_run_tool_admin_bookings_are_not_counted(brain, ha):
    ha.create_event = lambda *a, **k: "Added to Familie: T."
    a = parse_access("admin")
    counters = {}
    brain._run_tool(block("ha_create_event", title="T", start="2030-01-01T10:00", confirmed=True),
                    brain.tools_for(a), a, counters)
    assert counters == {}


def test_run_tool_forecast(brain, ha):
    a = parse_access("read")
    assert brain._run_tool(block("ha_forecast", type="hourly"), brain.tools_for(a), a) == "forecast ok"
    assert ha.calls == [("forecast", ("", "hourly"), {})]


def test_run_tool_energy_and_statistics(brain, ha):
    a = parse_access("read")
    tools = brain.tools_for(a)
    assert brain._run_tool(block("ha_energy", start="2026-10"), tools, a) == "energy ok"
    assert brain._run_tool(block("ha_statistics", entity_id="sensor.wp", start="2026-11-01", end="2026-11-02",
                                 period="day"), tools, a) == "statistics ok"
    assert ha.calls == [("energy", ("2026-10", "", ""), {}),
                        ("statistics", ("sensor.wp", "2026-11-01", "2026-11-02", "day"), {})]


def test_run_tool_history(brain, ha):
    a = parse_access("read")
    tools = brain.tools_for(a)
    assert brain._run_tool(block("ha_history", entity_id="cover.garage"), tools, a) == "history ok"
    assert brain._run_tool(block("ha_history", entity_id="cover.garage", hours=48), tools, a) == "history ok"
    assert brain._run_tool(block("ha_history", entity_id="lock.door", start="2026-11-07", end="2026-11-07"),
                           tools, a) == "history ok"
    assert ha.calls == [("history", ("cover.garage", None, "", ""), {}),
                        ("history", ("cover.garage", 48, "", ""), {}),
                        ("history", ("lock.door", None, "2026-11-07", "2026-11-07"), {})]


def test_run_tool_automation(brain, ha):
    a = parse_access("read")
    tools = brain.tools_for(a)
    assert brain._run_tool(block("ha_automation", entity_id="automation.heating"), tools, a) == "automation ok"
    assert brain._run_tool(block("ha_automation", entity_id="script.x", evaluate=False), tools, a) \
        == "automation ok"
    assert brain._run_tool(block("ha_automation", entity_id="automation.heating", detail="full"), tools, a) \
        == "automation ok"
    assert brain._run_tool(block("ha_automations", term="Wärmepumpe"), tools, a) == "automations ok"
    assert brain._run_tool(block("ha_automations"), tools, a) == "automations ok"
    assert ha.calls == [("automation", ("automation.heating", True), {"full": False}),
                        ("automation", ("script.x", False), {"full": False}),
                        ("automation", ("automation.heating", True), {"full": True}),
                        ("automations", ("Wärmepumpe",), {}), ("automations", ("",), {})]


@pytest.mark.parametrize("access", ["guest", "calendar", "shopping"])
def test_automation_not_for_callers_without_read(brain, ha, access):
    a = parse_access(access)
    assert brain._run_tool(block("ha_automation", entity_id="automation.heating"), brain.tools_for(a), a) \
        == "Not allowed in this call."
    assert brain._run_tool(block("ha_automations"), brain.tools_for(a), a) == "Not allowed in this call."
    assert ha.calls == []


@pytest.mark.parametrize("access", ["guest", "calendar", "shopping"])
def test_history_not_for_callers_without_read(brain, ha, access):
    a = parse_access(access)
    assert brain._run_tool(block("ha_history", entity_id="cover.garage"), brain.tools_for(a), a) \
        == "Not allowed in this call."
    assert ha.calls == []


@pytest.mark.parametrize("access", ["guest", "calendar", "shopping"])
def test_energy_not_for_callers_without_read(brain, ha, access):
    a = parse_access(access)
    assert brain._run_tool(block("ha_energy"), brain.tools_for(a), a) == "Not allowed in this call."
    assert ha.calls == []


def test_run_tool_passes_arguments(brain, ha):
    a = parse_access("admin")
    brain._run_tool(block("ha_control", entity_id="cover.garage", service="open_cover", confirmed=1),
                    brain.tools_for(a), a)
    assert ha.calls == [("call_service", ("cover.garage", "open_cover", {}, True), {})]


def test_run_tool_errors_become_text(brain, ha):
    def boom(*a, **k):
        raise RuntimeError("HA down")
    ha.search = boom
    a = parse_access("admin")
    assert brain._run_tool(block("ha_search", query="x"), brain.tools_for(a), a) == "Error: HA down"


# ---------------------------------------------------------------- SentenceSplitter
def split(*chunks):
    out = []
    s = SentenceSplitter(out.append)
    for c in chunks:
        s.feed(c)
    before_flush = list(out)
    s.flush()
    return before_flush, out, s


def test_splitter_emits_sentences_while_streaming():
    before, out, _ = split("Hallo Jan. Wie geht ", "es dir heute? Gut")
    assert before == ["Hallo Jan. Wie geht es dir heute?"]  # "Hallo Jan." alone is too short
    assert out == before + ["Gut"]


def test_splitter_needs_whitespace_after_punctuation():
    _, out, _ = split("Es sind 21.5 Grad. Um 14:30 Uhr kommt die Sonne.")
    assert out == ["Es sind 21.5 Grad.", "Um 14:30 Uhr kommt die Sonne."]


def test_splitter_strips_markdown():
    _, out, _ = split("**Wichtig:** Das Licht ist an. ## Ende")
    assert out == ["Wichtig: Das Licht ist an.", "Ende"]


def test_splitter_detects_hang_up_written_as_text():
    _, out, s = split("Tschüss, bis bald! [Tool call: hang_up] ")
    assert out == ["Tschüss, bis bald!"]
    assert s.fake_hangup


def test_splitter_system_note_is_not_a_hang_up():
    _, out, s = split("Ich lege gleich auf, keine Sorge. [System: test]")
    assert out == ["Ich lege gleich auf, keine Sorge."]
    assert not s.fake_hangup


def test_splitter_does_not_cut_after_an_initial():
    """#65: "Ich habe Jan T." - pause - "dreimal angerufen"."""
    before, out, _ = split("Ich habe Jan T. dreimal angerufen, aber nur die Mailbox erreicht. Soll ich es später ",
                           "noch einmal versuchen? Das geht z. B. am Montag. Ende")
    assert out == ["Ich habe Jan T. dreimal angerufen, aber nur die Mailbox erreicht.",
                   "Soll ich es später noch einmal versuchen?", "Das geht z. B. am Montag.", "Ende"]
    assert before == out[:3]
    _, out, _ = split("Wir fahren mit der Linie A. Sie kommt um 8 Uhr.")  # merged, said in one go: no harm
    assert out == ["Wir fahren mit der Linie A. Sie kommt um 8 Uhr."]


def test_splitter_never_reads_out_emojis_and_smileys(brain):
    _, out, _ = split("Jeder Tag ist willkommen ;-) 😉 Bis bald 👍🏽. Es ist 14:30 Uhr :) Tschüss ☀️!")
    assert out == ["Jeder Tag ist willkommen Bis bald.", "Es ist 14:30 Uhr Tschüss!"]
    assert "Zwinkersmiley" in brain.system("de")  # and not named in words either


def test_splitter_cuts_long_text_at_a_comma():
    text = "Das ist ein sehr langer Satz ohne Punkt, " * 6
    before, out, _ = split(text)
    assert before and before[0].endswith(",")
    assert "".join(out).replace(" ", "") == text.replace(" ", "")


# ---------------------------------------------------------------- _repair
def test_repair_fills_missing_tool_results():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a"}, {"type": "tool_use", "id": "b"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "x"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c"}]},
        {"role": "user", "content": "caller text"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "d"}]},
    ]
    Brain._repair(msgs)
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["b", "a"]
    assert msgs[4]["content"][0]["tool_use_id"] == "c"
    assert msgs[4]["content"][1] == {"type": "text", "text": "caller text"}
    assert msgs[6] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "d", "content": "Not executed (aborted)."}]}


# ---------------------------------------------------------------- turn
def message(*content, stop="end_turn"):
    blocks = [TextBlock(type="text", text=c) if isinstance(c, str) else c for c in content]
    return Message(id="m", type="message", role="assistant", model="test", content=blocks, stop_reason=stop,
                   stop_sequence=None, usage=Usage(input_tokens=1, output_tokens=1))


def tool(name, id_="t1", **inp):
    return ToolUseBlock(type="tool_use", id=id_, name=name, input=inp)


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        """Events like the SDK's MessageStream: a start per block, text in pieces of 7 characters."""
        for i, b in enumerate(self.msg.content):
            self.snapshot = self.msg.content[:i + 1]
            yield SimpleNamespace(type="content_block_start", index=i, content_block=b)
            if b.type == "text":
                for j in range(0, len(b.text), 7):
                    yield SimpleNamespace(type="text", text=b.text[j:j + 7])

    @property
    def current_message_snapshot(self):
        return SimpleNamespace(content=getattr(self, "snapshot", []))

    def get_final_message(self):
        return self.msg


class FakeMessages:
    def __init__(self, *msgs):
        self.msgs = list(msgs)
        self.requests = []

    def stream(self, **kw):
        self.requests.append(kw)
        return FakeStream(self.msgs.pop(0))


def run_turn(brain, *msgs, access="admin", abort=lambda: False):
    fake = FakeMessages(*msgs)
    brain.client = SimpleNamespace(messages=fake)
    said, actions = [], []
    messages = [{"role": "user", "content": "[System: test]"}]
    hang = brain.turn(messages, said.append, abort, access=parse_access(access), actions=actions)
    return hang, said, messages, actions, fake


def test_turn_plain_answer(brain):
    hang, said, messages, _, fake = run_turn(brain, message("Hallo Jan, wie kann ich helfen?"))
    assert hang is False
    assert said == ["Hallo Jan, wie kann ich helfen?"]
    assert messages[-1]["role"] == "assistant"
    assert "Current time" in fake.requests[0]["system"]


def test_turn_runs_tools_then_answers(brain, ha):
    hang, said, messages, actions, _ = run_turn(
        brain, message(tool("ha_state", entity_id="sensor.temp"), stop="tool_use"), message("Es sind 21 Grad."))
    assert hang is False
    assert ha.calls[0][0] == "get_state"
    assert messages[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "get_state ok"}]}
    assert said == ["Es sind 21 Grad."]
    assert actions == []  # reading is not an action


def test_turn_keeps_booking_count_across_turns(brain, ha):
    ha.create_event = lambda *a, **k: ha.calls.append(k["booked"]) or "Added: T."
    counters = {}
    for _ in range(2):
        brain.client = SimpleNamespace(messages=FakeMessages(
            message(tool("ha_create_event", title="T", start="2030-01-01T10:00", confirmed=True), stop="tool_use"),
            message("Eingetragen.")))
        brain.turn([{"role": "user", "content": "x"}], lambda s: None, lambda: False,
                   access=parse_access("calendar"), counters=counters)
    assert ha.calls == [0, 1]
    assert counters == {"bookings": 2}


def test_turn_records_changing_actions(brain):
    _, _, _, actions, _ = run_turn(
        brain, message(tool("shopping_edit", action="add", item="Milch"), stop="tool_use"), message("Erledigt."))
    assert actions == ['shopping_edit {"action": "add", "item": "Milch"} -> shopping_edit ok']


def test_turn_hang_up(brain):
    hang, said, *_ = run_turn(brain, message("Tschüss, bis bald!", tool("hang_up"), stop="tool_use"))
    assert hang is True
    assert said == ["Tschüss, bis bald!"]


def test_turn_hang_up_written_as_text(brain):
    hang, *_ = run_turn(brain, message("Tschüss, bis bald! [hang_up]"))
    assert hang is True


def test_turn_guest_gets_only_hang_up_and_cannot_use_other_tools(brain, ha):
    hang, _, messages, _, fake = run_turn(
        brain, message(tool("ha_control", entity_id="lock.door", service="unlock"), stop="tool_use"),
        message("Das darf ich leider nicht."), access="guest")
    assert names(fake.requests[0]["tools"]) == {"hang_up"}
    assert ha.calls == []
    assert messages[2]["content"][0]["content"] == "Not allowed in this call."


def test_turn_does_not_run_tool_cut_off_by_max_tokens(brain, ha):
    hang, _, messages, _, _ = run_turn(
        brain, message("Ich schalte", tool("ha_control", entity_id="light.x", service="turn_on"), stop="max_tokens"))
    assert hang is False
    assert ha.calls == []
    assert all(b["type"] != "tool_use" for b in messages[-1]["content"])


def test_turn_aborted_by_barge_in(brain):
    calls = iter([False, True])
    hang, said, messages, _, _ = run_turn(
        brain, message("Erster Satz ist hier. Zweiter Satz kommt nicht mehr."), abort=lambda: next(calls, True))
    assert hang is False
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"].endswith("[interrupted by the caller]")


def test_turn_hooks_time_tools_and_tokens(brain, ha):
    fake = FakeMessages(message(tool("ha_state", entity_id="sensor.temp"), stop="tool_use"),
                        message("Es sind 21 Grad."))
    brain.client = SimpleNamespace(messages=fake)
    seen, timed, tokens = [], [], []
    brain.turn([{"role": "user", "content": "x"}], lambda s: None, lambda: False, access=parse_access("admin"),
               before_tool=lambda b: seen.append(b.name) or True, after_tool=lambda n, s: timed.append(n),
               on_token=lambda: tokens.append(1))
    assert seen == ["ha_state"] and timed == ["ha_state"] and tokens


def test_turn_before_tool_can_stop_a_tool(brain, ha):
    fake = FakeMessages(message(tool("ha_control", entity_id="light.x", service="turn_on"), stop="tool_use"))
    brain.client = SimpleNamespace(messages=fake)
    messages = [{"role": "user", "content": "x"}]
    actions = []
    hang = brain.turn(messages, lambda s: None, lambda: False, access=parse_access("admin"), actions=actions,
                      before_tool=lambda b: False)
    assert hang is False and ha.calls == [] and actions == []
    assert messages[2]["content"][0]["content"] == "Not executed (aborted)."
    assert messages[-1]["role"] == "assistant" and "[interrupted by the caller]" in messages[-1]["content"]
    assert len(fake.requests) == 1


def test_turn_aborted_before_the_request(brain):
    fake = FakeMessages()
    brain.client = SimpleNamespace(messages=fake)
    messages = [{"role": "user", "content": "x"}]
    assert brain.turn(messages, lambda s: None, lambda: True) is False
    assert fake.requests == [] and messages[-1]["role"] == "assistant"


def test_system_prompt_mentions_waiting_phrases_only_with_fillers(brain):
    assert "waiting phrase" in brain.system()
    brain.cfg["fillers"] = False
    assert "waiting phrase" not in brain.system()
