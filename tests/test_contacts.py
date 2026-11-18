"""Contacts in contacts.json (#93): moving people from callers, the lean file, checks, changes without restart."""
import json
import os
import time

import config
import contacts
from conftest import make_cfg

PEOPLE = [
    {"number": "0151111", "name": "Jan", "access": "admin", "ha_user": "jan"},
    {"number": "04542 81234", "name": "Jan laut", "access": "admin"},
    {"number": "0160444", "name": "Maryna", "access": "calendar", "language": "uk"},
    {"number": "0170555", "name": "Maryna", "access": "shopping"},
    {"number": "*", "name": "Unknown", "access": "guest"},
    {"ha_device": "*", "access": "shopping"},
]


def cfg_with(callers=None, **kw):
    cfg = make_cfg(callers=json.loads(json.dumps(callers if callers is not None else PEOPLE)), **kw)
    cfg["callers_raw"] = json.loads(json.dumps(callers if callers is not None else PEOPLE))
    return cfg


def store(tmp_path, cfg=None, **kw):
    cfg = cfg or cfg_with()
    return contacts.Contacts(cfg, tmp_path / "contacts.json", **kw), cfg


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))["contacts"]


# ---------------------------------------------------------------- first start: the people move into the file
def test_people_move_from_callers_rules_stay(tmp_path):
    s, cfg = store(tmp_path)
    line = s.setup()
    assert "4 in" in line and "4 moved from callers" in line
    items = read(s.path)
    assert [c["name"] for c in items] == ["Jan", "Jan laut", "Maryna", "Maryna (2)"]
    jan = items[0]
    assert jan["numbers"] == [{"number": "0151111"}] and jan["access"] == ["admin"] and jan["ha_user"] == "jan"
    assert jan["origin"] == "settings" and len(jan["id"]) == 4
    assert items[2]["language"] == "uk" and "language" not in items[0]
    assert any("Maryna (2)" in w for w in config.WARNINGS)
    # without Home Assistant OS the own config.json is never rewritten: a warning asks to clean it up
    assert any("remove them from callers" in w for w in config.WARNINGS)
    # the live list: the contacts and the "*" rules
    assert [c["name"] for c in cfg.callers][:4] == ["Jan", "Jan laut", "Maryna", "Maryna (2)"]
    assert sum(config.is_rule(c) for c in cfg.callers) == 2


def test_lean_file_leaves_out_defaults(tmp_path):
    s, _ = store(tmp_path, cfg_with([{"number": "0151", "name": " Willi ", "access": "guest", "notify": None,
                                      "web": None, "instructions": "", "language": "de", "email": ""}]))
    s.setup()
    assert read(s.path)[0].keys() == {"id", "name", "numbers", "origin", "added"}


def test_as_an_app_the_settings_keep_only_the_rules(tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(config, "save_option", lambda cfg, key, value: saved.update({key: value}))
    s, _ = store(tmp_path, cfg_with(supervisor_token="t"))
    s.setup()
    assert saved["callers"] == [{"number": "*", "name": "Unknown", "access": "guest"},
                                {"ha_device": "*", "access": "shopping"}]


def test_an_existing_file_wins_and_people_left_in_callers_are_named(tmp_path):
    path = tmp_path / "contacts.json"
    path.write_text(json.dumps({"contacts": [{"id": "ab12", "name": "Frank", "numbers": [{"number": "0160999"}]}]}))
    s, cfg = store(tmp_path)
    s.setup()
    assert [c["name"] for c in cfg.callers if not config.is_rule(c)] == ["Frank"]
    assert any("still lists 4 people" in w for w in config.WARNINGS)


# ---------------------------------------------------------------- several numbers per person
def test_a_person_has_several_numbers_with_labels(tmp_path):
    s, cfg = store(tmp_path)
    s.setup()
    jan, laut = s.by_name("Jan"), s.by_name("jan laut")
    stored, errors = s.save({**jan, "numbers": jan["numbers"] + [{"number": "+49 4542 81235", "label": "laut"}]},
                            jan["id"])
    assert errors == [] and stored["numbers"][1] == {"number": "+49 4542 81235", "label": "laut"}
    entry = next(c for c in cfg.callers if c["name"] == "Jan")
    assert config.has_number(entry, "0454281235", "49") and config.phone_of(entry, "0454281235", "49") == "laut"
    assert config.phone_of(entry, "0151111", "49") == ""
    assert s.by_number("0049 4542 81235")["name"] == "Jan" and laut["name"] == "Jan laut"


# ---------------------------------------------------------------- checks
def codes(errors):
    return sorted((e["field"], e["code"]) for e in errors)


def test_names_are_unique_and_required(tmp_path):
    s, _ = store(tmp_path)
    s.setup()
    _, errors = s.save({"name": "  jan  ", "numbers": [{"number": "0170000"}]})
    assert codes(errors) == [("name", "name_taken")] and errors[0]["name"] == "Jan"
    _, errors = s.save({"name": " ", "numbers": [{"number": "0170000"}]})
    assert codes(errors) == [("name", "name_missing")]
    jan = s.by_name("Jan")
    assert s.save({**jan, "name": "JAN"}, jan["id"])[1] == []  # its own name, other case: fine


def test_numbers_belong_to_one_contact(tmp_path):
    s, _ = store(tmp_path)
    s.setup()
    _, errors = s.save({"name": "Neu", "numbers": [{"number": "+49151111"}, {"number": "0170 1"}, {"number": "x1"},
                                                   {"number": "01709"}, {"number": "0170-9"}]})
    assert codes(errors) == [("numbers", "number_invalid"), ("numbers", "number_taken"), ("numbers", "number_twice")]
    taken = next(e for e in errors if e["code"] == "number_taken")
    assert taken == {"field": "numbers", "index": 0, "code": "number_taken", "name": "Jan"}


def test_other_checks(tmp_path):
    s, _ = store(tmp_path)
    s.setup()
    _, errors = s.save({"name": "X", "email": "nope", "access": ["root"], "language": "Deutsch"})
    assert codes(errors) == [("access", "access_invalid"), ("email", "email_invalid"), ("language", "language_invalid")]
    assert codes(s.save({"name": "Only a name"})[1]) == [("numbers", "empty")]
    assert s.save({"name": "Hotel XYZ", "email": "info@hotel.example", "language": "it"})[1] == []
    _, errors = s.save({"name": "Hotel 2", "email": "INFO@hotel.example"})
    assert codes(errors) == [("email", "email_taken")]
    assert s.save({"name": "Y", "numbers": [{"number": "**611"}]})[1] == []  # internal FRITZ!Box number


# ---------------------------------------------------------------- changes
def test_save_keeps_id_origin_and_leaves_out_the_system_language(tmp_path):
    s, _ = store(tmp_path, cfg_with([]))
    s.setup()
    new, errors = s.save({"name": "Frank", "numbers": [{"number": "0160 555", "label": "Handy"}], "language": "de",
                          "notify": False, "web": "yes", "access": ["shopping", "guest"]})
    assert errors == []
    assert new == {"id": new["id"], "name": "Frank", "numbers": [{"number": "0160 555", "label": "Handy"}],
                   "access": ["shopping"], "notify": False, "origin": "page", "added": new["added"]}
    changed, _ = s.save({**new, "name": "Frank M"}, new["id"], origin="request")
    assert (changed["id"], changed["origin"], changed["name"]) == (new["id"], "page", "Frank M")
    assert s.save({"name": "Z"}, "nope")[1] == [{"field": "id", "code": "not_found"}]


def test_delete_is_refused_while_a_task_needs_the_contact(tmp_path):
    s, cfg = store(tmp_path, in_use=lambda c: "Tisch reservieren" if c["name"] == "Jan" else "")
    s.setup()
    jan, maryna = s.by_name("Jan"), s.by_name("Maryna")
    assert s.delete(jan["id"]) == "Tisch reservieren" and s.get(jan["id"])
    assert s.delete(maryna["id"]) == "" and s.get(maryna["id"]) is None
    assert "Maryna" not in [c["name"] for c in cfg.callers] and s.delete("nope") == "not_found"


def test_merge_joins_numbers_and_keeps_the_target(tmp_path):
    s, cfg = store(tmp_path)
    s.setup()
    jan, laut = s.by_name("Jan"), s.by_name("Jan laut")
    merged, why = s.merge(jan["id"], laut["id"])
    assert why == "" and merged["name"] == "Jan"
    assert merged["numbers"] == [{"number": "0151111"}, {"number": "04542 81234", "label": "laut"}]
    assert s.get(laut["id"]) is None
    entry = next(c for c in cfg.callers if c["name"] == "Jan")
    assert config.has_number(entry, "045428 1234", "49")
    assert s.merge(jan["id"], jan["id"]) == (None, "not_found")


def test_language_follows_the_last_call(tmp_path):
    s, _ = store(tmp_path)
    s.setup()
    jan = s.by_name("Jan")
    assert s.set_language(jan["id"], "en") is True and s.get(jan["id"])["language"] == "en"
    assert s.set_language(jan["id"], "en") is False
    assert s.set_language(jan["id"], "de") is True and "language" not in s.get(jan["id"])  # system language
    assert s.set_language(jan["id"], "klingon!") is False


# ---------------------------------------------------------------- no restart: the live list follows the file
def test_a_change_by_hand_is_read_without_restart(tmp_path):
    s, cfg = store(tmp_path)
    s.setup()
    live = cfg.callers
    data = json.loads(s.path.read_text())
    data["contacts"].append({"id": "zz99", "name": "Willi", "numbers": [{"number": "0170 777"}]})
    s.path.write_text(json.dumps(data))
    later = time.time() + 5
    os.utime(s.path, (later, later))
    assert s.refresh() is True and s.refresh() is False
    assert cfg.callers is live and any(c["name"] == "Willi" for c in live)


def test_a_broken_file_keeps_the_contacts(tmp_path):
    s, cfg = store(tmp_path)
    s.setup()
    before = [c["name"] for c in cfg.callers]
    s.path.write_text("{ broken")
    later = time.time() + 5
    os.utime(s.path, (later, later))
    s.refresh()
    assert [c["name"] for c in cfg.callers] == before


def test_an_empty_or_misshapen_file_keeps_the_contacts_an_empty_list_clears_them(tmp_path):
    """Review of #94: a file caught half written (empty) or without a list must not wipe the live contacts."""
    s, cfg = store(tmp_path)
    s.setup()
    before = [c["name"] for c in cfg.callers]
    for text in ("", "   ", '{"people": []}', '{"contacts": {}}', "42"):
        s.path.write_text(text)
        stamp = time.time() + 5 + len(text)
        os.utime(s.path, (stamp, stamp))
        s.refresh()
        assert [c["name"] for c in cfg.callers] == before, text
        assert len(s.items) == 4
    s.path.write_text('{"contacts": []}')
    os.utime(s.path, (time.time() + 99, time.time() + 99))
    s.refresh()
    assert s.items == [] and all(config.is_rule(c) for c in cfg.callers)


def test_merge_tells_who_was_merged(tmp_path):
    merged = []
    s, _ = store(tmp_path, on_merge=lambda other, into: merged.append((other["name"], into["name"])))
    s.setup()
    s.merge(s.by_name("Jan")["id"], s.by_name("Jan laut")["id"])
    assert merged == [("Jan laut", "Jan")]


# ---------------------------------------------------------------- order
def test_all_is_alphabetical_ignoring_case_and_accents(tmp_path):
    path = tmp_path / "contacts.json"
    names = ["Zoe", "willi", "Änne", "Bert", "anna", "Ölaf", "Otto"]
    path.write_text(json.dumps({"contacts": [{"id": f"{i:04x}", "name": n, "numbers": [{"number": f"0170{i}"}]}
                                             for i, n in enumerate(names)]}))
    s, _ = store(tmp_path)
    s.setup()
    assert [c["name"] for c in s.all()] == ["anna", "Änne", "Bert", "Ölaf", "Otto", "willi", "Zoe"]
    assert [c["name"] for c in read(path)] == names  # the file keeps its order
