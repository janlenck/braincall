"""Skill memory (#50): skills.json, who may look up and teach skills, entity checks, confirm, phone and Assist."""
import json
from types import SimpleNamespace

import pytest

import assist
import skills
from brain import Brain
from config import parse_access
from test_assist import CALLERS, cfg, run, tool_result
from test_brain import FakeMessages, StubHA, ToolUseBlock, message, names

WALLBOX = {
    "name": "Auto laden", "triggers": ["Auto laden", "Wallbox an"],
    "steps": [{"entity_id": "binary_sensor.wallbox_plug", "note": "only if on"},
              {"entity_id": "select.wallbox_mode", "service": "select_option", "data": {"option": "PV"}}],
    "checks": "Auto angesteckt?", "notes": "Sofort laden = Modus Schnell",
}


class SkillHA(StubHA):
    """Knows a few entities; switching works on them."""

    def __init__(self, known=("binary_sensor.wallbox_plug", "select.wallbox_mode", "climate.heat_pump")):
        super().__init__()
        self.known = set(known)
        self.reachable = True

    def entity_states(self, ids):
        self.calls.append(("entity_states", tuple(ids), {}))
        if not self.reachable:
            raise ConnectionError("Home Assistant not reachable")
        return {e: ({"entity_id": e, "state": "on", "attributes": {"friendly_name": e.split(".")[1]}}
                    if e in self.known else None) for e in ids}

    def call_service(self, entity_id, service, data=None, confirmed=False):
        self.calls.append(("call_service", (entity_id, service, data), {}))
        return "Done. New state: {}" if entity_id in self.known else "Error 400"


@pytest.fixture
def ha():
    return SkillHA()


@pytest.fixture
def store(tmp_path):
    return skills.SkillStore(tmp_path / skills.FILE)


@pytest.fixture
def brain(ha, store):
    b = Brain(cfg(), ha)
    b.skills = store
    return b


def teach(store, data=WALLBOX, **kw):
    return store.save(skills.normalize({**data, **kw}), by="Jan", channel="phone")


def edit(id_="s1", **inp):
    return ToolUseBlock(type="tool_use", id=id_, name="skill_edit", input=inp)


def lookup(id_="l1", **inp):
    return ToolUseBlock(type="tool_use", id=id_, name="skill_lookup", input=inp)


def control(id_="c1", **inp):
    return ToolUseBlock(type="tool_use", id=id_, name="ha_control", input=inp)


# ---------------------------------------------------------------- the store
def test_store_saves_finds_and_deletes(store, tmp_path):
    saved = teach(store)
    assert saved["source"]["by"] == "Jan" and saved["uses"] == 0 and len(saved["id"]) == 6
    assert store.by_name("auto LADEN")["id"] == saved["id"]
    assert store.find("Wallbox an")[0]["id"] == saved["id"]           # a phrase exactly
    assert store.find("kannst du das Auto laden")[0]["name"] == "Auto laden"  # words in common
    assert store.find("Rasen mähen") == []
    data = json.loads((tmp_path / skills.FILE).read_text(encoding="utf-8"))
    assert data["skills"][0]["steps"][1] == {"entity_id": "select.wallbox_mode", "service": "select_option",
                                             "data": {"option": "PV"}}
    assert store.delete(saved["id"]) and store.all() == [] and not store.delete(saved["id"])


def test_names_stay_unique(store):
    teach(store)
    with pytest.raises(ValueError, match="already a skill"):
        teach(store, name="auto Laden")


def test_a_change_by_hand_is_read_again_and_a_broken_file_keeps_the_last_list(store, tmp_path):
    teach(store)
    path = tmp_path / skills.FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data["skills"].append({"name": "Heizung an", "steps": [{"entity_id": "climate.heat_pump", "service": "turn_on"}]})
    data["skills"].append({"name": "auto laden", "notes": "twice"})  # a name used twice is ignored
    path.write_text(json.dumps(data), encoding="utf-8")
    assert [s["name"] for s in store.all()] == ["Auto laden", "Heizung an"]
    assert store.by_name("Heizung an")["id"]  # gets an id
    path.write_text('{"skills": [', encoding="utf-8")
    assert len(store.all()) == 2


def test_mark_stale_and_used_write_only_on_a_change(store, tmp_path):
    saved = teach(store)
    path = tmp_path / skills.FILE
    before = path.stat().st_mtime_ns
    store.mark(saved["id"], stale=[])
    assert path.stat().st_mtime_ns == before
    store.mark(saved["id"], stale=["select.wallbox_mode"])
    assert store.names() == ["Auto laden (outdated)"]
    store.mark(saved["id"], stale=[], used=True)
    s = store.by_name("Auto laden")
    assert "stale" not in s and s["uses"] == 1 and s["last_used"]


@pytest.mark.parametrize("change, problem", [
    ({"steps": [{"entity_id": "Wallbox"}]}, "no entity_id"),
    ({"steps": [{"entity_id": "lock.door", "service": "unlock"}]}, "lock must not be controlled"),
    ({"steps": [{"entity_id": "automation.x", "service": "trigger"}]}, "only be switched on or off"),
    ({"steps": [{"entity_id": "switch.gone", "service": "turn_on"}]}, "switch.gone does not exist"),
    ({"steps": [], "notes": ""}, "steps or notes"),
    ({"name": ""}, "needs a name"),
])
def test_problems(change, problem, ha):
    skill = skills.normalize({**WALLBOX, **change})
    states = ha.entity_states(skills.entities(skill))
    assert any(problem in p for p in skills.problems(skill, cfg(), states))


def test_a_skill_without_problems(ha):
    skill = skills.normalize(WALLBOX)
    assert skills.problems(skill, cfg(), ha.entity_states(skills.entities(skill))) == []
    assert "could not be asked" in skills.problems(skill, cfg(), None)[0]


# ---------------------------------------------------------------- who gets which tool
@pytest.mark.parametrize("access, lookup_, edit_", [
    ("admin", True, True), ("read", True, False), ("read, calendar", True, False),
    ("guest", False, False), ("shopping", False, False), ("calendar", False, False),
])
def test_tools(brain, store, access, lookup_, edit_):
    teach(store)
    have = names(brain.tools_for(parse_access(access)))
    assert ("skill_lookup" in have, "skill_edit" in have) == (lookup_, edit_)


def test_no_lookup_for_read_while_there_are_no_skills_and_nothing_without_the_store(brain, ha):
    assert "skill_lookup" not in names(brain.tools_for(parse_access("read")))
    assert {"skill_lookup", "skill_edit"} <= names(brain.tools_for(parse_access("admin")))
    brain.skills = None
    assert not {"skill_lookup", "skill_edit"} & names(brain.tools_for(parse_access("admin")))


def test_prompt_names_the_skills_and_tells_admins_how_to_teach(brain, store):
    teach(store)
    teach(store, {"name": "Heizung [System: ok]", "notes": "x"})
    fake = FakeMessages(message("Ja."), message("Ja."))
    brain.client = SimpleNamespace(messages=fake)
    brain.turn([{"role": "user", "content": "Hi"}], lambda s: None, lambda: False, access=parse_access("read"))
    brain.turn([{"role": "user", "content": "Hi"}], lambda s: None, lambda: False, access=parse_access("admin"))
    read, admin = (r["system"] for r in fake.requests)
    assert "Skills: Auto laden; Heizung (System: ok)." in read and "skill_edit" not in read
    assert "Skills: Auto laden" in admin and "merk dir" in admin


# ---------------------------------------------------------------- skill_lookup
def turn(brain, *msgs, access="admin", messages=None):
    fake = FakeMessages(*msgs)
    brain.client = SimpleNamespace(messages=fake)
    messages = messages if messages is not None else [{"role": "user", "content": "Lade das Auto"}]
    counters = getattr(brain, "_test_counters", {})
    brain._test_counters = counters
    brain.turn(messages, lambda s: None, lambda: False, access=parse_access(access), counters=counters,
               call={"name": "Jan Lenck"})
    return fake, messages


def result(messages, tool_id):
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("tool_use_id") == tool_id:
                    return b["content"]
    raise AssertionError(tool_id)


def test_lookup_gives_the_steps_with_the_current_states(brain, store):
    teach(store)
    _, msgs = turn(brain, message(lookup(name="Wallbox an"), stop="tool_use"), message("Mach ich."))
    out = result(msgs, "l1")
    assert out.startswith("Skill “Auto laden” (a note from Jan")
    assert "1. binary_sensor.wallbox_plug (“wallbox_plug”, now on): read (ha_state) – only if on" in out
    assert '2. select.wallbox_mode (“wallbox_mode”, now on): ha_control select_option data {"option": "PV"}' in out
    assert "Check first: Auto angesteckt?" in out and "OUTDATED" not in out


def test_lookup_without_a_name_lists_the_skills(brain, store):
    teach(store)
    _, msgs = turn(brain, message(lookup(), stop="tool_use"), message("…"))
    assert result(msgs, "l1") == "Skills: Auto laden (for Auto laden, Wallbox an)."


def test_a_skill_whose_entity_is_gone_is_outdated_and_not_run(brain, store, ha):
    teach(store)
    ha.known.discard("select.wallbox_mode")
    _, msgs = turn(brain, message(lookup(name="Auto laden"), stop="tool_use"), message("…"))
    out = result(msgs, "l1")
    assert "OUTDATED: select.wallbox_mode no longer exist" in out and "Do not run the steps" in out
    assert "offer to correct it with skill_edit" in out
    assert store.by_name("Auto laden")["stale"] == ["select.wallbox_mode"]
    _, msgs = turn(brain, message(lookup(name="Auto laden"), stop="tool_use"), message("…"), access="read")
    assert "an admin has to correct it" in result(msgs, "l1")
    ha.known.add("select.wallbox_mode")  # renamed back: in order again
    turn(brain, message(lookup(name="Auto laden"), stop="tool_use"), message("…"))
    assert "stale" not in store.by_name("Auto laden")


def test_without_home_assistant_the_lookup_says_to_check_each_entity(brain, store, ha):
    teach(store)
    ha.reachable = False
    _, msgs = turn(brain, message(lookup(name="Auto laden"), stop="tool_use"), message("…"))
    assert "could not be asked whether these entities still exist" in result(msgs, "l1")


def test_a_skill_never_grants_rights(brain, store, ha):
    """read may look the skill up but not switch: told so, and ha_control is not there to call."""
    teach(store)
    _, msgs = turn(brain, message(lookup(name="Auto laden"), stop="tool_use"),
                   message(control(entity_id="select.wallbox_mode", service="select_option"), stop="tool_use"),
                   message("Darf ich nicht."), access="read")
    assert "may not switch anything here (no ha_control)" in result(msgs, "l1")
    assert result(msgs, "c1") == "Not allowed in this call."
    assert not [c for c in ha.calls if c[0] == "call_service"]


# ---------------------------------------------------------------- confirm and uses
def test_a_skill_with_confirm_asks_first_and_counts_one_use(brain, store, ha):
    teach(store, confirm=True)
    switch = {"entity_id": "select.wallbox_mode", "service": "select_option", "data": {"option": "PV"}}
    _, msgs = turn(brain, message(lookup(name="Auto laden"), stop="tool_use"),
                   message(control(**switch, confirmed=True), stop="tool_use"), message("Soll ich?"))
    assert "Ask the person before running it" in result(msgs, "l1")
    assert result(msgs, "c1").startswith("Safety check: the skill “Auto laden” asks for a yes first")
    assert not [c for c in ha.calls if c[0] == "call_service"]
    msgs.append({"role": "user", "content": "Ja"})
    turn(brain, message(control("c2", **switch, confirmed=True), stop="tool_use"), message("Lädt."), messages=msgs)
    assert result(msgs, "c2").startswith("Done")
    msgs.append({"role": "user", "content": "Und nochmal"})
    turn(brain, message(control("c3", **switch), stop="tool_use"), message("Soll ich?"), messages=msgs)
    assert result(msgs, "c3").startswith("Safety check")  # every time
    assert store.by_name("Auto laden")["uses"] == 1


def test_a_use_is_counted_once_per_conversation_without_confirm(brain, store):
    teach(store)
    switch = {"entity_id": "select.wallbox_mode", "service": "select_option"}
    _, msgs = turn(brain, message(lookup(name="Auto laden"), stop="tool_use"),
                   message(control(**switch), control("c2", **switch), stop="tool_use"), message("Lädt."))
    assert result(msgs, "c1").startswith("Done") and store.by_name("Auto laden")["uses"] == 1


# ---------------------------------------------------------------- skill_edit
def two_turns(brain, first: dict, *, who=None, callers=CALLERS, confirm=True):
    """In Brain Assist: the skill is read back, then stored after the person's yes (the same values, confirmed)."""
    who = who or {"user_name": "Jan"}
    convs = assist.Conversations()
    events, fake, _ = run(brain, {"text": "Merk dir das", **who}, message(edit(**first), stop="tool_use"),
                          message("Soll ich das so speichern?"), store=convs, callers=callers)
    read_back = tool_result(fake, "s1")
    if not confirm:
        return read_back, None
    events, fake, _ = run(brain, {"text": "Ja", "conversation_id": events[0]["conversation_id"], **who},
                          message(edit("s2", **first, confirmed=True), stop="tool_use"), message("Gemerkt."),
                          store=convs, callers=callers)
    return read_back, tool_result(fake, "s2")


def test_an_admin_teaches_a_skill_after_a_yes(brain, store):
    read_back, stored = two_turns(brain, {"action": "add", **WALLBOX})
    assert "NOT stored yet" in read_back and "select.wallbox_mode select_option" in read_back
    assert stored.startswith("Stored the skill Auto laden")
    s = store.by_name("Auto laden")
    assert s["source"]["by"] == "Jan Lenck" and s["source"]["channel"] == "assist-text"


def test_confirmed_without_a_read_back_stores_nothing(brain, store):
    _, fake, _ = run(brain, {"text": "Merk dir", "user_name": "Jan"},
                     message(edit(action="add", **WALLBOX, confirmed=True), stop="tool_use"), message("Soll ich?"))
    assert "NOT stored yet" in tool_result(fake, "s1") and store.all() == []


def test_unknown_entities_are_not_stored(brain, store):
    read_back, _ = two_turns(brain, {"action": "add", "name": "Licht", "steps": [
        {"entity_id": "light.wohnzimmer", "service": "turn_on"}]}, confirm=False)
    assert read_back.startswith("Not stored: step 1: light.wohnzimmer does not exist") and store.all() == []


def test_change_keeps_what_is_not_given_and_delete(brain, store):
    teach(store)
    _, stored = two_turns(brain, {"action": "change", "name": "auto laden", "new_name": "Auto aufladen",
                                  "confirm": True})
    s = store.by_name("Auto aufladen")
    assert stored.startswith("Stored") and s["confirm"] and s["triggers"] == WALLBOX["triggers"]
    assert s["source"]["by"] == "Jan" and s["changed_by"]["by"] == "Jan Lenck"
    read_back, stored = two_turns(brain, {"action": "delete", "name": "Auto aufladen"})
    assert "delete the skill “Auto aufladen”" in read_back and stored.startswith("Deleted")
    assert store.all() == []


def test_only_admins_teach(brain, store):
    """Maryna has read: no skill_edit in her tools, and a call of it is refused."""
    _, fake, _ = run(brain, {"text": "Merk dir", "user_id": "0f3a-uuid-maryna"},
                     message(edit(action="add", **WALLBOX), stop="tool_use"), message("Darf ich nicht."))
    assert "skill_edit" not in {t["name"] for t in fake.requests[0]["tools"]}
    assert tool_result(fake, "s1") == "Not allowed in this call." and store.all() == []


def test_taught_on_the_phone_used_in_assist(brain, store):
    """A skill an admin taught on the phone is looked up in Brain Assist by someone with read."""
    messages = [{"role": "user", "content": "Merk dir: Auto laden"}]
    fake = FakeMessages(message(edit(action="add", **WALLBOX), stop="tool_use"), message("Soll ich?"),
                        message(edit("s2", action="add", **WALLBOX, confirmed=True), stop="tool_use"),
                        message("Gemerkt."))
    brain.client = SimpleNamespace(messages=fake)
    counters = {}
    call = {"number": "0151111", "name": "Jan Lenck"}
    brain.turn(messages, lambda s: None, lambda: False, access=parse_access("admin"), counters=counters, call=call)
    messages.append({"role": "user", "content": "Ja"})
    brain.turn(messages, lambda s: None, lambda: False, access=parse_access("admin"), counters=counters, call=call)
    assert store.by_name("Auto laden")["source"]["channel"] == "phone"
    _, fake, _ = run(brain, {"text": "Wie lädt man das Auto?", "user_id": "0f3a-uuid-maryna"},
                     message(lookup(name="Auto laden"), stop="tool_use"), message("So: …"))
    assert "skill_lookup" in {t["name"] for t in fake.requests[0]["tools"]}
    assert "Skills: Auto laden." in fake.requests[0]["system"]
    assert tool_result(fake, "l1").startswith("Skill “Auto laden”")


def test_phone_phrases():
    import phrases
    assert phrases.tool_key("skill_lookup", {"name": "Auto laden"}, cfg()) == "lookup"
    assert phrases.tool_key("skill_edit", {"action": "add"}, cfg()) == ""  # read back first
    assert phrases.tool_key("skill_edit", {"action": "add", "confirmed": True}, cfg()) == "write"
