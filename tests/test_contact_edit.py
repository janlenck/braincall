"""Contacts by voice (contact_edit) and the contact every planned call gets as its anchor (#93)."""
from types import SimpleNamespace

import pytest

import assist
import contacts
import tasks
from brain import Brain
from test_assist import CALLERS, cfg, run, tool_result
from test_brain import StubHA, ToolUseBlock, message

PEOPLE = CALLERS + [{"number": "04542 81234", "name": "Jan laut", "access": "admin"}]


@pytest.fixture
def brain(tmp_path):
    b = Brain(cfg(PEOPLE), StubHA())
    b.cfg["callers_raw"] = PEOPLE
    b.contacts = contacts.Contacts(b.cfg, tmp_path / "contacts.json")
    b.contacts.setup()
    return b


def edit(id_="e1", **inp):
    return ToolUseBlock(type="tool_use", id=id_, name="contact_edit", input=inp)


def two_turns(brain, first: dict, *, who=None, callers=PEOPLE, confirm=True):
    """The change is read back, then stored after the person's yes (the same values, confirmed)."""
    who = who or {"user_name": "Jan"}
    store = assist.Conversations()
    events, fake, _ = run(brain, {"text": "Bitte ändern", **who}, message(edit(**first), stop="tool_use"),
                          message("Soll ich das so speichern?"), store=store, callers=callers)
    read_back = tool_result(fake, "e1")
    if not confirm:
        return read_back, None
    events, fake, _ = run(brain, {"text": "Ja", "conversation_id": events[0]["conversation_id"], **who},
                          message(edit("e2", **first, confirmed=True), stop="tool_use"), message("Erledigt."),
                          store=store, callers=callers)
    return read_back, tool_result(fake, "e2")


def test_an_admin_adds_a_contact_after_a_yes(brain):
    read_back, stored = two_turns(brain, {"action": "add", "name": "Willi",
                                          "add_numbers": [{"number": "0170 777", "label": "Handy"}]})
    assert "NOT stored yet" in read_back and "Willi: numbers 0170 777 (Handy)" in read_back
    assert stored.startswith("Stored: Willi") and brain.contacts.by_name("Willi")["origin"] == "request"


def test_confirmed_without_a_read_back_stores_nothing(brain):
    _, fake, _ = run(brain, {"text": "Speicher Willi", "user_name": "Jan"},
                     message(edit(action="add", name="Willi", add_numbers=[{"number": "0170777"}], confirmed=True),
                             stop="tool_use"), message("Soll ich?"), callers=PEOPLE)
    assert "NOT stored yet" in tool_result(fake, "e1") and brain.contacts.by_name("Willi") is None


def test_a_new_office_number_replaces_the_old_one_and_show(brain):
    two_turns(brain, {"action": "change", "name": "Walter", "add_numbers": [{"number": "040 1", "label": "Büro"}]})
    _, stored = two_turns(brain, {"action": "change", "name": "Walter",
                                  "add_numbers": [{"number": "040 2222", "label": "büro"}]})
    walter = brain.contacts.by_name("Walter")
    assert [n["number"] for n in walter["numbers"]] == ["0170222", "040 2222"]
    _, fake, _ = run(brain, {"text": "Was hast du für Walter?", "user_name": "Jan"},
                     message(edit(action="show", name="walter"), stop="tool_use"), message("…"), callers=PEOPLE)
    assert tool_result(fake, "e1").startswith("Walter: numbers 0170222, 040 2222 (Büro)")


def test_admin_is_never_given_or_taken_by_voice(brain):
    read_back, _ = two_turns(brain, {"action": "change", "name": "Walter", "rights": ["admin"]}, confirm=False)
    assert read_back.startswith("Not changed: admin")
    read_back, _ = two_turns(brain, {"action": "change", "name": "Jan Lenck", "rights": ["read"]}, confirm=False)
    assert read_back.startswith("Not changed: the rights of an admin")
    assert brain.contacts.by_name("Jan Lenck")["access"] == ["admin"]


def test_names_stay_unique_by_voice(brain):
    read_back, _ = two_turns(brain, {"action": "add", "name": "walter", "add_numbers": [{"number": "0170 9"}]},
                             confirm=False)
    assert "There is already a contact named Walter" in read_back
    read_back, _ = two_turns(brain, {"action": "change", "name": "Maryna", "new_name": "Walter"}, confirm=False)
    assert "Not stored: another contact is already called Walter" in read_back


def test_merge_by_voice_gives_the_phone_its_label(brain):
    read_back, stored = two_turns(brain, {"action": "merge", "name": "Jan Lenck", "merge_from": "Jan laut"})
    assert "merge Jan laut into Jan Lenck" in read_back and stored.startswith("Merged:")
    jan = brain.contacts.by_name("Jan Lenck")
    assert jan["numbers"] == [{"number": "0151111"}, {"number": "04542 81234", "label": "laut"}]
    assert brain.contacts.by_name("Jan laut") is None


def test_only_admins_get_contact_edit(brain):
    _, fake, _ = run(brain, {"text": "Hallo", "user_id": "0f3a-uuid-maryna"}, message("Hallo."), callers=PEOPLE)
    assert "contact_edit" not in {t["name"] for t in fake.requests[0]["tools"]}
    _, fake, _ = run(brain, {"text": "Hallo", "user_name": "Jan"}, message("Hallo."), callers=PEOPLE)
    assert "contact_edit" in {t["name"] for t in fake.requests[0]["tools"]}


# ---------------------------------------------------------------- every task has a contact
def plan(id_="p1", **inp):
    return ToolUseBlock(type="tool_use", id=id_, name="plan_call", input=inp)


def plan_twice(brain, tmp_path, **inp):
    brain.cfg["outbound_allowed_regex"] = ""
    brain.tasks = tasks.TaskStore(tmp_path / "tasks.json", brain.cfg)
    store = assist.Conversations()
    events, fake, _ = run(brain, {"text": "Ruf im Restaurant an", "user_name": "Jan"},
                          message(plan(**inp), stop="tool_use"), message("Soll ich?"), store=store, callers=PEOPLE)
    read_back = tool_result(fake, "p1")
    events, fake, _ = run(brain, {"text": "Ja", "conversation_id": events[0]["conversation_id"], "user_name": "Jan"},
                          message(plan("p2", **inp, confirmed=True), stop="tool_use"), message("Gut."),
                          store=store, callers=PEOPLE)
    return read_back, tool_result(fake, "p2")


def test_a_planned_call_to_a_new_number_creates_its_contact(brain, tmp_path):
    read_back, stored = plan_twice(brain, tmp_path, number="04542 5555", name="Restaurant Seeblick",
                                   task="Tisch für 4 reservieren", language="uk")
    assert "save Restaurant Seeblick as a new contact" in read_back and stored.startswith("Stored (task")
    contact = brain.contacts.by_name("Restaurant Seeblick")
    assert contact["numbers"] == [{"number": "045425555"}] and contact["origin"] == "task"
    assert contact["language"] == "uk" and "access" not in contact


def test_a_planned_call_to_a_known_number_creates_nothing(brain, tmp_path):
    read_back, _ = plan_twice(brain, tmp_path, name="Walter", task="Frag nach dem Termin")
    assert "new contact" not in read_back and len(brain.contacts.all()) == len(PEOPLE) - 0


def test_a_taken_name_with_another_number_is_asked_about(brain, tmp_path):
    read_back, _ = plan_twice(brain, tmp_path, number="0160 9999", name="Walter", task="x")
    assert read_back.startswith("Not planned: there is already a contact named Walter")
    assert brain.tasks.all() == []


# ---------------------------------------------------------------- the language a contact spoke last
def test_summary_names_the_language_spoken():
    class Once:
        def __init__(self, text):
            self.text, self.prompts = text, []

        def create(self, **kw):
            self.prompts.append(kw["messages"][0]["content"])
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])

    b = Brain(cfg(), StubHA())
    b.client = SimpleNamespace(messages=Once("Jan fragte nach dem Wetter.\nLANGUAGE: EN"))
    spoken = {}
    text = b.summarize(["Jan: Hello"], "Jan (0151)", [], "de", spoken=spoken)
    assert text == "Jan fragte nach dem Wetter." and spoken == {"language": "en"}
    assert "LANGUAGE:" in b.client.messages.prompts[0]
    b.client = SimpleNamespace(messages=Once("Kurz."))
    assert b.summarize(["x"], "Jan", [], "de") == "Kurz." and "LANGUAGE" not in b.client.messages.prompts[0]
