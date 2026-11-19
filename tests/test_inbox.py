"""The owner's messages (#104, #31): the calls taken for them, the messages tool and the summary's facts."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import inbox
from brain import Brain
from config import parse_access
from conftest import make_cfg
from test_brain import StubHA, message, names, run_turn, tool

NOW = datetime(2026, 11, 19, 18, 54, tzinfo=timezone.utc)


@pytest.fixture
def box(tmp_path):
    return inbox.Inbox(tmp_path / inbox.FILE, "49", "Europe/Berlin")


# ---------------------------------------------------------------- store
def test_a_message_is_new_until_heard_and_a_call_back_stays_open_until_done(box):
    a = box.add("04541 111", "Möchte einen neuen Termin.", name="Erika Muster", callback=True, when="Sonntag",
                now=NOW)
    b = box.add("0170 222", "Wollte nur Bescheid sagen.", name="Max", now=NOW)
    assert [e["id"] for e in box.open(NOW)] == [a["id"], b["id"]]
    assert box.heard([a["id"], b["id"]], NOW) == 2
    assert [e["id"] for e in box.open(NOW)] == [a["id"]]  # the call back stays open
    closed = box.done([a["id"].upper()], NOW)
    assert [e["id"] for e in closed] == [a["id"]] and box.open(NOW) == []
    assert box.done([a["id"]], NOW) == []  # already done
    assert len(box.recent(7, NOW)) == 2


def test_an_outgoing_conversation_settles_the_call_backs_of_that_number(box):
    a = box.add("04541 111", "Rückruf bitte.", callback=True, now=NOW)
    other = box.add("0170 222", "Rückruf bitte.", callback=True, now=NOW)
    assert [e["id"] for e in box.settle("+494541111", NOW)] == [a["id"]]
    assert [e["id"] for e in box.open(NOW)] == [other["id"]]
    assert box.settle("", NOW) == []


def test_old_entries_are_dropped_open_call_backs_kept_longer(box):
    old = NOW - timedelta(days=inbox.DAYS + 1)
    box.add("0111", "alt", at=old, now=old)
    back = box.add("0222", "alt, Rückruf", callback=True, at=old, now=old)
    box.add("0333", "neu", now=NOW)
    assert [e["number"] for e in box.entries(NOW)] == ["0222", "0333"]
    assert box.entries(old + timedelta(days=inbox.OPEN_DAYS + 1)) == []  # not even an open call back is kept
    box.done([back["id"]], NOW)
    assert [e["number"] for e in box.entries(NOW)] == ["0333"]


def test_a_broken_file_is_an_empty_inbox(box):
    box.path.write_text("{not json", encoding="utf-8")
    assert box.open(NOW) == []
    box.add("0111", "neu", now=NOW)
    assert len(box.open(NOW)) == 1


def test_the_line_names_who_what_call_back_and_state(box):
    e = box.add("04541 111", "Termin verpasst, möchte neuen Termin.", name="Erika Muster", callback=True,
                when="Sonntag oder Montag", note="saved as a new contact", now=NOW)
    line = box.line(e)
    assert line.startswith(f"- id {e['id']} · Thu 2026-11-19 19:54 · Erika Muster (04541 111) · Termin verpasst")
    assert "wants a call back (Sonntag oder Montag): open" in line and "saved as a new contact" in line
    assert line.endswith("· new")
    missed = box.add("", "", kind=inbox.MISSED, now=NOW)
    assert "name unknown (number withheld) · missed call, no message · new" in box.line(missed)


# ---------------------------------------------------------------- the note for the owner
def test_the_owners_note_lists_what_is_open(box):
    assert "no new messages, no open call backs" in box.note("Jan", now=NOW)
    box.add("04541 111", "Möchte einen neuen Termin.", name="Erika Muster", callback=True, now=NOW)
    note = box.note("Jan", now=NOW)
    assert "Messages for Jan: calls you took for them (1 new, 1 open call back)" in note
    assert "Erika Muster (04541 111)" in note and "In your first answer" in note
    assert "Gibt es was Offenes?" in note and "Never read out the ids" in note
    assert "In your first answer" not in box.note("Jan", phone=False, now=NOW)  # Brain Assist: only when asked


def test_the_note_names_the_latest_and_counts_the_rest(box):
    for i in range(inbox.NOTE_MAX + 2):
        box.add(f"0{i}11", f"Nachricht {i}", now=NOW + timedelta(minutes=i))
    note = box.note("Jan", now=NOW + timedelta(hours=1))
    assert "Nachricht 0" not in note and f"Nachricht {inbox.NOTE_MAX + 1}" in note and "(2 older ones" in note


# ---------------------------------------------------------------- the messages tool
@pytest.fixture
def brain(box):
    b = Brain(make_cfg(anthropic_api_key="test", owner_name="Jan Lenck"), StubHA())
    b.inbox = box
    return b


def test_only_admins_get_the_messages_tool(brain):
    assert "messages" in names(brain.tools_for(parse_access("admin")))
    assert "Jan" in next(t for t in brain.tools_for(parse_access("admin")) if t["name"] == "messages")["description"]
    for access in ("guest", "read", "calendar, shopping"):
        assert "messages" not in names(brain.tools_for(parse_access(access)))
    brain.inbox = None
    assert "messages" not in names(brain.tools_for(parse_access("admin")))


def ask(brain, access="admin", **inp) -> str:
    """One turn in which Claude uses messages with inp; returns what the tool answered."""
    _, _, msgs, _, _ = run_turn(brain, message(tool("messages", **inp), stop="tool_use"), message("Gut."),
                                access=access)
    return next(b["content"] for m in msgs if isinstance(m["content"], list) for b in m["content"]
                if isinstance(b, dict) and b.get("type") == "tool_result")


def test_the_owner_hears_the_new_messages_and_they_count_as_heard(brain, box):
    e = box.add("04541 111", "Möchte einen neuen Termin.", name="Erika Muster", callback=True)
    box.add("0170 222", "Nur ein Gruß.", name="Max")
    out = ask(brain, action="new")
    assert out.startswith("New calls and open call backs") and "Erika Muster" in out and "Nur ein Gruß." in out
    assert [x["id"] for x in box.open()] == [e["id"]]  # heard; the call back stays open
    assert ask(brain, action="done", ids=[e["id"]]).startswith("Done: - id") and box.open() == []
    assert ask(brain, action="new") == "Nothing new and no open call backs."


def test_recent_lists_heard_and_done_calls_too(brain, box):
    e = box.add("04541 111", "Termin.", name="Erika Muster", callback=True)
    box.done([e["id"]])
    out = ask(brain, action="recent", days=3)
    assert out.startswith("Calls of the last 3 days") and "wants a call back: done" in out
    assert ask(brain, action="done", ids=[]).startswith("Nothing done: give the ids")


def test_a_guest_cannot_use_the_messages_tool(brain, box):
    box.add("04541 111", "Geheim.", name="Erika Muster")
    assert ask(brain, access="guest", action="new") == "Not allowed in this call." and len(box.open()) == 1


# ---------------------------------------------------------------- name and call back from the summary
def summarize(brain, answer: str, **kw):
    asked = []
    brain.client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **k: asked.append(k["messages"][0]["content"]) or message(answer)))
    facts = {}
    text = brain.summarize(["0451: Hallo, hier ist Erika Muster", "Claudine: Gern."], "0451", [], "de",
                           facts=facts, **kw)
    return text, facts, asked[0]


def test_the_summary_tells_name_call_back_and_when(brain):
    text, facts, prompt = summarize(brain, "Erika Muster bittet um Rückruf.\nNAME: Erika Muster\nCALLBACK: yes\n"
                                           "WHEN: Sonntag oder Montag")
    assert text == "Erika Muster bittet um Rückruf."
    assert facts == {"name": "Erika Muster", "callback": True, "when": "Sonntag oder Montag"}
    assert "NAME:" in prompt and "CALLBACK:" in prompt and "WHEN:" in prompt


def test_the_summary_without_name_or_call_back(brain):
    text, facts, _ = summarize(brain, "Jemand hat aufgelegt.\nNAME: none\nCALLBACK: no\nWHEN: none")
    assert (text, facts) == ("Jemand hat aufgelegt.", {"name": "", "callback": False, "when": ""})
    text, facts, _ = summarize(brain, "Nur Text, keine Angaben.")
    assert (text, facts) == ("Nur Text, keine Angaben.", {"name": "", "callback": False, "when": ""})


def test_language_and_facts_together(brain):
    spoken = {}
    text, facts, _ = summarize(brain, "Summary.\nNAME: Erika Muster\nCALLBACK: Yes\nWHEN: none\nLANGUAGE: en",
                               spoken=spoken)
    assert text == "Summary." and spoken == {"language": "en"} and facts["callback"] is True


def test_without_facts_the_prompt_does_not_ask_for_them(brain):
    asked = []
    brain.client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **k: asked.append(k["messages"][0]["content"]) or message("Kurz.")))
    assert brain.summarize(["a: b"], "0451", [], "de") == "Kurz."
    assert "NAME:" not in asked[0]
