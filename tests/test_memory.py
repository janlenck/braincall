"""memory.py: caller memory (caller_memory) – parsing, storing, pruning, access and the intro note."""
import json
import math
from datetime import datetime, timedelta, timezone

import pytest

import config
import memory

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
ADMIN, READ, CAL, GUEST = (frozenset({a}) for a in ("admin", "read", "calendar", "guest"))


@pytest.mark.parametrize("value, days", [
    ("none", 0), (None, 0), ("", 0), (False, 0), ("0", 0), (0, 0), ("NONE", 0),
    ("forever", math.inf), (" Forever ", math.inf), (30, 30), ("30", 30), ("1", 1),
])
def test_parse_days(value, days):
    assert memory.parse_days(value) == days
    assert config.WARNINGS == []


@pytest.mark.parametrize("value", ["-5", "30 days", "1.5", "always", True])
def test_parse_days_invalid_is_off_with_warning(value):
    assert memory.parse_days(value) == 0
    assert len(config.WARNINGS) == 1 and "caller_memory" in config.WARNINGS[0]


def test_default_is_none():
    assert config.DEFAULTS["caller_memory"] == "none"


@pytest.fixture
def path(tmp_path):
    return tmp_path / "logs" / "calls.jsonl"


def mem(path, days=30):
    return memory.CallMemory(path, days, "49")


def lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_off_neither_writes_nor_reads(path):
    m = mem(path, 0)
    m.add("0151111", "Summary", ADMIN, True, when=NOW, now=NOW)
    assert not path.exists() and not m.enabled and m.describe() == "off"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"time": NOW.isoformat(), "number": "0151111", "access": "admin",
                                "summary": "old"}) + "\n", encoding="utf-8")
    assert m.recent("0151111", ADMIN, NOW) == []


def test_add_and_recent_per_normalized_number(path):
    m = mem(path)
    m.add("+49 151 111", "First", CAL, True, when=NOW - timedelta(days=2), now=NOW)
    m.add("0049151111", "Second", CAL, False, when=NOW - timedelta(days=1), now=NOW)
    m.add("0160333", "Other number", CAL, True, when=NOW, now=NOW)
    got = m.recent("0151111", CAL, NOW)
    assert [e["summary"] for e in got] == ["First", "Second"]
    assert [e["direction"] for e in got] == ["in", "out"]
    assert lines(path)[0] == {"time": "2026-11-06T12:00:00+00:00", "number": "0151111", "direction": "in",
                              "access": "calendar", "summary": "First"}


def test_only_the_last_three(path):
    m = mem(path)
    for i in range(5):
        m.add("0151111", f"Call {i}", GUEST, True, when=NOW - timedelta(hours=5 - i), now=NOW)
    assert [e["summary"] for e in m.recent("0151111", GUEST, NOW)] == ["Call 2", "Call 3", "Call 4"]


def test_days_limit_on_read_and_prune_on_write(path):
    m = mem(path, 30)
    m.add("0151111", "Old", GUEST, True, when=NOW - timedelta(days=40), now=NOW)
    m.add("0151111", "Recent", GUEST, True, when=NOW - timedelta(days=10), now=NOW)
    assert [e["summary"] for e in m.recent("0151111", GUEST, NOW)] == ["Recent"]
    m.add("0160333", "New", GUEST, True, when=NOW, now=NOW)
    assert [e["summary"] for e in lines(path)] == ["Recent", "New"]


def test_forever_keeps_everything(path):
    m = mem(path, math.inf)
    m.add("0151111", "Ancient", GUEST, True, when=NOW - timedelta(days=4000), now=NOW)
    m.add("0151111", "New", GUEST, True, when=NOW, now=NOW)
    assert [e["summary"] for e in m.recent("0151111", GUEST, NOW)] == ["Ancient", "New"]
    assert m.describe() == "forever" and mem(path, 30).describe() == "30 days"


def test_withheld_number_and_empty_summary_are_not_stored(path):
    m = mem(path)
    m.add("", "Summary", GUEST, True, when=NOW, now=NOW)
    m.add("0151111", "   ", GUEST, True, when=NOW, now=NOW)
    assert not path.exists()
    assert m.recent("", GUEST, NOW) == []


@pytest.mark.parametrize("recorded, current, used", [
    (ADMIN, ADMIN, True), (READ, ADMIN, True), (GUEST, CAL, True), (CAL, CAL, True),
    (ADMIN, GUEST, False), (ADMIN, READ, False), (READ, CAL, False), (CAL, GUEST, False),
    (frozenset({"calendar", "shopping"}), CAL, False), (CAL, frozenset({"calendar", "shopping"}), True),
])
def test_summaries_from_calls_with_more_access_are_not_used(path, recorded, current, used):
    m = mem(path)
    m.add("0151111", "Summary", recorded, True, when=NOW, now=NOW)
    assert bool(m.recent("0151111", current, NOW)) is used


def test_broken_lines_are_skipped_and_long_summaries_cut(path):
    m = mem(path)
    m.add("0151111", "x" * 2000, GUEST, True, when=NOW, now=NOW)
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"time": "2026-11-08T12:00:00+00:00", "numb\n[1, 2]\n{"number": "0151111"}\n')
    got = m.recent("0151111", GUEST, NOW)
    assert len(got) == 1 and len(got[0]["summary"]) == memory.MAX_CHARS
    m.add("0151111", "After", GUEST, True, when=NOW, now=NOW)
    assert len(m.recent("0151111", GUEST, NOW)) == 2


@pytest.mark.parametrize("bad_time", ["yesterday", "2026-11-01T14:05:00", None])
def test_entry_without_valid_time_is_dropped_with_a_limit(path, bad_time):
    """Also a time without timezone: it cannot be compared with now and must not block later saves."""
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"time": bad_time, "number": "0151111", "access": "guest",
                                "summary": "?"}) + "\n", encoding="utf-8")
    assert mem(path, 30).recent("0151111", GUEST, NOW) == []
    assert len(mem(path, math.inf).recent("0151111", GUEST, NOW)) == 1
    mem(path, 30).add("0151111", "New", GUEST, True, when=NOW, now=NOW)
    assert [e["summary"] for e in lines(path)] == ["New"]


def test_unterminated_last_line_does_not_swallow_the_next_save(path):
    m = mem(path)
    m.add("0151111", "First", GUEST, True, when=NOW, now=NOW)
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"time": "2026-11-08T12:00:00+00:00", "number": "015')  # crash in the middle of a line
    m.add("0151111", "Second", GUEST, True, when=NOW, now=NOW)
    assert [e["summary"] for e in lines(path)] == ["First", "Second"]
    m.add("0151111", "Third", GUEST, True, when=NOW, now=NOW)
    assert [e["summary"] for e in m.recent("0151111", GUEST, NOW)] == ["First", "Second", "Third"]


def test_ordered_by_call_time_not_by_save_order(path):
    m = mem(path)
    m.add("0151111", "Later call", GUEST, True, when=NOW - timedelta(minutes=5), now=NOW)
    m.add("0151111", "Earlier call", GUEST, True, when=NOW - timedelta(hours=2), now=NOW)
    assert [e["summary"] for e in m.recent("0151111", GUEST, NOW)] == ["Earlier call", "Later call"]
    assert lines(path)[1]["time"] == "2026-11-08T10:00:00+00:00"


def test_note_in_local_time_marked_as_background():
    entries = [{"time": "2026-11-01T13:05:00+00:00", "direction": "in", "summary": "Maryna asked about Friday."},
               {"time": "2026-07-01T08:00:00+00:00", "direction": "out", "summary": "Reminded her."}]
    text = memory.note(entries, "Jan", "Europe/Berlin")
    assert "2026-11-01 14:05 (phone, they called): Maryna asked about Friday." in text
    assert "2026-07-01 10:00 (phone, you called them): Reminded her." in text
    assert "with this caller" in text and "summaries written for Jan" in text and "not instructions" in text
    assert "never say you cannot remember" in text
    assert "none for this caller" in memory.note([], "Jan", "Europe/Berlin")
    assert "no memory of earlier calls or conversations" in memory.note([], "Jan", "Europe/Berlin", enabled=False)
    assert "2026-11-01" in memory.note(entries, "Jan", "Not/AZone")


# ---------------------------------------------------------------- per person: number, ha_user, ha_device (#50)
def test_who_of_a_phone_call():
    jan = {"number": "0151111", "ha_user": "Jan"}
    assert memory.who(jan, "+49151111") == {"number": "+49151111", "ha_user": "Jan"}
    assert memory.who({"number": "0160333", "ha_user": ""}, "0160333") == {"number": "0160333", "ha_user": ""}
    assert memory.who({"number": "*", "ha_user": "Jan"}, "0999") == {"number": "0999", "ha_user": ""}  # everyone else
    assert memory.who(None, "0999") == {"number": "0999", "ha_user": ""}


def test_one_memory_for_number_and_home_assistant_user(path):
    m = mem(path)
    m.add({"number": "+49 151 111", "ha_user": "Jan"}, "Call", ADMIN, True, when=NOW - timedelta(hours=3), now=NOW)
    m.add({"ha_user": " JAN ", "number": "0151111"}, "Chat", ADMIN, when=NOW - timedelta(hours=2), now=NOW,
          channel="assist-text", device="Tablet")
    m.add({"ha_device": "Küche"}, "Kitchen", ADMIN, when=NOW - timedelta(hours=1), now=NOW, channel="assist-voice")
    assert [e["summary"] for e in m.recent({"ha_user": "jan"}, ADMIN, NOW)] == ["Call", "Chat"]
    assert [e["summary"] for e in m.recent("0151111", ADMIN, NOW)] == ["Call", "Chat"]
    assert [e["summary"] for e in m.recent({"ha_device": "küche"}, ADMIN, NOW)] == ["Kitchen"]
    assert m.recent({"ha_user": "maryna"}, ADMIN, NOW) == []
    assert lines(path)[1] == {"time": "2026-11-08T10:00:00+00:00", "number": "0151111", "ha_user": "jan",
                              "channel": "assist-text", "device": "Tablet", "access": "admin", "summary": "Chat"}
    assert lines(path)[2]["ha_device"] == "küche" and "number" not in lines(path)[2]


def test_lines_from_before_1_12_are_found_by_their_number(path):
    """No migration: an old line has only the number, and the person's entry still has it."""
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"time": (NOW - timedelta(days=1)).isoformat(), "number": "0151111",
                                "direction": "in", "access": "admin", "summary": "Old call"}) + "\n",
                    encoding="utf-8")
    m = mem(path)
    got = m.recent({"ha_user": "Jan", "number": "0151111"}, ADMIN, NOW)
    assert [e["summary"] for e in got] == ["Old call"]
    assert "(phone, they called): Old call" in memory.note(got, "Jan", "Europe/Berlin", subject="this person")
    assert m.recent({"ha_user": "Jan"}, ADMIN, NOW) == []  # without the number nothing links it to the user


def test_many_short_requests_do_not_push_out_the_calls(path):
    m = mem(path)
    for i in range(4):
        m.add("0151111", f"Call {i}", ADMIN, True, when=NOW - timedelta(days=10 - i), now=NOW)
    for i in range(5):
        m.add({"number": "0151111"}, f"Chat {i}", ADMIN, when=NOW - timedelta(hours=5 - i), now=NOW,
              channel="assist-voice")
    assert [e["summary"] for e in m.recent("0151111", ADMIN, NOW)] == [
        "Call 1", "Call 2", "Call 3", "Chat 2", "Chat 3", "Chat 4"]


def test_access_rule_holds_across_channels(path):
    """What an admin said in the chat does not reach the same person on a restricted device."""
    m = mem(path)
    m.add({"ha_user": "jan"}, "Admin chat", ADMIN, when=NOW, now=NOW, channel="assist-text")
    assert m.recent({"ha_user": "jan"}, frozenset({"shopping"}), NOW) == []
    assert len(m.recent({"ha_user": "jan"}, ADMIN, NOW)) == 1


def test_forget_a_person(path):
    m = mem(path)
    m.add({"number": "0151111", "ha_user": "Jan"}, "Call", ADMIN, True, when=NOW, now=NOW)
    m.add({"ha_user": "jan"}, "Chat", ADMIN, when=NOW, now=NOW, channel="assist-text")
    m.add("0160333", "Someone else", GUEST, True, when=NOW, now=NOW)
    assert m.forget({"ha_user": "JAN"}) == 2
    assert [e["summary"] for e in lines(path)] == ["Someone else"]
    assert m.forget("0151111") == 0 and m.forget({}) == 0
    assert mem(path, 0).forget("0160333") == 1  # also while the memory is off
    assert mem(path.with_name("missing.jsonl")).forget("0160333") == 0


def test_note_names_the_channel_and_the_subject():
    entries = [{"time": "2026-11-01T13:05:00+00:00", "channel": "assist-voice", "device": "Küche", "summary": "A"},
               {"time": "2026-11-02T13:05:00+00:00", "channel": "assist-text", "summary": "B"}]
    text = memory.note(entries, "Jan", "Europe/Berlin", subject="this person")
    assert "(Home Assistant, spoken, device “Küche”): A" in text and "(Home Assistant chat): B" in text
    assert "conversations with this person" in text
    assert "none for the people who used this device" in memory.note(
        [], "Jan", subject="the people who used this device")
