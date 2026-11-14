"""Automatic shopping list (#69): an item counts as bought only after it stayed checked for the full time,
the history, the answers of shopping_history and the settings."""
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
import shopping as shop
from brain import Brain
from config import parse_access
from conftest import make_cfg

LIST = "todo.shopping_list"
START = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)  # 10:00 in Berlin


class Clock:
    def __init__(self):
        self.t = START

    def __call__(self):
        return self.t


class FakeHA:
    """The shopping list in Home Assistant: items with uid, summary and status."""

    def __init__(self):
        self.items = []
        self.state = None       # overrides the count of open items as entity state
        self.fail = None        # exception raised by every read
        self.remove_fail = None  # exception raised by remove_todo
        self.removed = []
        self.events = []
        self._uid = 0

    def core_running(self):
        return True

    def entity_exists(self, entity_id):
        return True

    def entity_state(self, entity_id):
        if self.fail:
            raise self.fail
        if self.state is not None:
            return self.state
        return str(sum(1 for i in self.items if i["status"] == "needs_action"))

    def todo_raw(self, entity_id):
        if self.fail:
            raise self.fail
        return [dict(i) for i in self.items]

    def remove_todo(self, entity_id, item):
        if self.remove_fail:
            raise self.remove_fail
        self.removed.append(item)
        self.items = [i for i in self.items if item not in (i.get("uid"), i["summary"])]

    def fire_event(self, name, data):
        self.events.append((name, data))

    # what the user does in the app
    def add(self, summary, checked=False, uid=True):
        self._uid += 1
        item = {"summary": summary, "status": "completed" if checked else "needs_action"}
        if uid:
            item["uid"] = f"u{self._uid}"
        self.items.append(item)
        return item

    def find(self, summary):
        return next(i for i in self.items if i["summary"] == summary)

    def check(self, summary):
        self.find(summary)["status"] = "completed"

    def uncheck(self, summary):
        self.find(summary)["status"] = "needs_action"

    def delete(self, summary):
        self.items.remove(self.find(summary))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ha():
    return FakeHA()


@pytest.fixture
def make(tmp_path, ha, clock):
    def make_shopping(**cfg):
        cfg.setdefault("timezone", "Europe/Berlin")
        return shop.Shopping(make_cfg(**cfg), ha, tmp_path / "shopping-history.jsonl", now=clock)
    return make_shopping


@pytest.fixture
def s(make):
    """Watching, after the first poll of an empty list."""
    watcher = make()
    watcher.poll()
    return watcher


def run(s, clock, minutes):
    """Polls once a minute for that many minutes."""
    for _ in range(minutes):
        clock.t += timedelta(minutes=1)
        s.poll()


def events(s, kind=None):
    return [e for e in s.history.entries() if kind is None or e["event"] == kind]


# ---------------------------------------------------------------- the hour
def test_checked_by_mistake_and_unchecked_is_not_bought(s, ha, clock):
    ha.add("Milch")
    run(s, clock, 5)
    ha.check("Milch")          # meant the macaroni
    run(s, clock, 20)
    ha.uncheck("Milch")
    run(s, clock, 120)
    assert [i["summary"] for i in ha.items] == ["Milch"]
    assert [e["event"] for e in events(s)] == ["added"]
    assert ha.removed == [] and ha.events == []


def test_checked_for_an_hour_is_bought_with_the_time_of_the_check(s, ha, clock):
    ha.add("2 Milch")
    run(s, clock, 1)
    ha.check("2 Milch")
    run(s, clock, 1)
    checked_at = clock.t
    run(s, clock, 59)
    assert ha.items and ha.removed == []      # 59 minutes are not enough
    run(s, clock, 1)
    assert ha.items == [] and ha.removed == ["u1"]
    bought, = events(s, "bought")
    assert bought == {"time": checked_at.isoformat(timespec="seconds"), "list": LIST, "event": "bought",
                      "item": "2 Milch", "key": "milch", "amount": "2", "uid": "u1"}
    assert ha.events == [(shop.BOUGHT_EVENT, {"list": LIST, "item": "2 Milch", "key": "milch",
                                              "time": bought["time"]})]
    run(s, clock, 5)
    assert len(events(s, "bought")) == 1 and events(s, "removed") == []


def test_check_uncheck_check_counts_from_the_last_check(s, ha, clock):
    ha.add("Butter")
    run(s, clock, 1)
    ha.check("Butter")
    run(s, clock, 30)
    ha.uncheck("Butter")
    run(s, clock, 10)
    ha.check("Butter")
    run(s, clock, 1)
    last_check = clock.t
    run(s, clock, 59)
    assert ha.items
    run(s, clock, 1)
    assert ha.items == []
    assert events(s, "bought")[0]["time"] == last_check.isoformat(timespec="seconds")


def test_the_time_is_configurable(make, ha, clock):
    s = make(shopping_bought_after_minutes=10)
    ha.add("Brot", checked=True)
    s.poll()
    run(s, clock, 10)
    assert ha.items == [] and len(events(s, "bought")) == 1


@pytest.mark.parametrize("checked", [False, True])
def test_deleted_items_are_never_bought(s, ha, clock, checked):
    ha.add("Hamster")
    run(s, clock, 1)
    if checked:
        ha.check("Hamster")
        run(s, clock, 30)
    ha.delete("Hamster")       # by hand or "clear completed"
    run(s, clock, 90)
    assert [e["event"] for e in events(s)] == ["added", "removed"]
    assert ha.events == []


def test_an_item_that_stayed_checked_long_enough_counts_whoever_removes_it(s, ha, clock):
    ha.add("Eier")
    run(s, clock, 1)
    ha.check("Eier")
    run(s, clock, 1)
    ha.remove_fail = OSError("timeout")
    run(s, clock, 60)
    ha.delete("Eier")          # the removal went through after all, or somebody cleared the list
    run(s, clock, 1)
    assert [e["event"] for e in events(s)] == ["added", "bought"]


def test_failed_removal_is_tried_again_and_written_once(s, ha, clock, caplog):
    ha.add("Käse")
    run(s, clock, 1)
    ha.check("Käse")
    run(s, clock, 1)
    ha.remove_fail = OSError("Home Assistant busy")
    with caplog.at_level(logging.WARNING, "shopping"):
        run(s, clock, 63)
    assert len([r for r in caplog.records if "removing" in r.getMessage()]) == 1
    assert events(s, "bought") == []
    ha.remove_fail = None
    run(s, clock, 1)
    assert ha.items == [] and len(events(s, "bought")) == 1


def test_rename_keeps_the_hour(s, ha, clock):
    ha.add("Milch")
    run(s, clock, 1)
    ha.check("Milch")
    run(s, clock, 30)
    ha.find("Milch")["summary"] = "Milch 1,5 %"
    run(s, clock, 31)   # the hour started with the first poll that saw the check
    assert ha.items == []
    assert events(s, "bought")[0]["item"] == "Milch 1,5 %"


def test_items_without_uid_are_followed_by_name(s, ha, clock):
    ha.add("Salz", uid=False)
    ha.add("Zucker", uid=False)
    run(s, clock, 1)
    ha.check("Zucker")
    run(s, clock, 61)
    assert ha.removed == ["Zucker"] and [i["summary"] for i in ha.items] == ["Salz"]
    assert [e["event"] for e in events(s)] == ["added", "added", "bought"]


def test_list_reloaded_with_new_uids_is_no_change(s, ha, clock):
    ha.add("Milch")
    ha.add("Brot")
    run(s, clock, 1)
    ha.check("Brot")
    run(s, clock, 30)
    for n, item in enumerate(ha.items):
        item["uid"] = f"new{n}"
    run(s, clock, 31)
    assert [e["event"] for e in events(s)] == ["added", "added", "bought"]
    assert ha.removed == ["new1"]


# ---------------------------------------------------------------- restart and errors
def test_first_poll_after_a_restart_writes_nothing_and_restarts_the_hour(make, ha, clock):
    ha.add("Milch")
    ha.add("Brot", checked=True)   # checked before the restart
    s = make()
    s.poll()
    assert events(s) == []
    run(s, clock, 59)
    assert len(ha.items) == 2
    run(s, clock, 1)
    assert [i["summary"] for i in ha.items] == ["Milch"]
    bought, = events(s)
    assert bought["event"] == "bought" and bought["time"] == START.isoformat(timespec="seconds")


@pytest.mark.parametrize("problem", ["error", "unavailable", "unknown", "empty"])
def test_an_answer_that_cannot_be_trusted_changes_nothing(s, ha, clock, caplog, problem):
    ha.add("Milch")
    ha.add("Brot")
    run(s, clock, 1)
    ha.check("Brot")
    run(s, clock, 1)
    real = ha.items
    if problem == "error":
        ha.fail = OSError("connection refused")
    elif problem == "empty":
        ha.items, ha.state = [], "1"   # a broken integration answering "no items"
    else:
        ha.state = problem
    with caplog.at_level(logging.WARNING, "shopping"):
        run(s, clock, 90)
    assert [e["event"] for e in events(s)] == ["added", "added"]
    assert ha.removed == []
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and "cannot be read" in warnings[0]
    ha.fail, ha.state, ha.items = None, None, real
    with caplog.at_level(logging.WARNING, "shopping"):
        run(s, clock, 1)
    assert "can be read again" in caplog.records[-1].getMessage()
    assert ha.removed == ["u2"]   # the hour went on while the list could not be read


def test_an_empty_list_is_fine(s, ha, clock):
    run(s, clock, 3)
    assert events(s) == [] and not s.failing


def test_history_survives_a_line_cut_off_by_a_crash(s, ha, clock):
    ha.add("Milch")
    run(s, clock, 1)
    with open(s.history.path, "a", encoding="utf-8") as f:
        f.write('{"time": "2026-11-14T09:01:00+00:00", "ev')
    ha.add("Brot")
    run(s, clock, 1)
    assert [e["item"] for e in events(s)] == ["Milch", "Brot"]


# ---------------------------------------------------------------- logging
def test_logging_levels(make, ha, clock, caplog):
    ha.add("Milch")
    s = make()
    with caplog.at_level(shop.TRACE, "shopping"):
        s.poll()
        ha.check("Milch")
        run(s, clock, 61)
    by_level = {}
    for r in caplog.records:
        by_level.setdefault(r.levelno, []).append(r.getMessage())
    assert len(by_level[shop.TRACE]) == 62   # every poll
    assert all("items" in m for m in by_level[shop.TRACE])
    debug = by_level[logging.DEBUG]
    assert any("checked" in m for m in debug) and any("bought" in m for m in debug)
    assert not by_level.get(logging.INFO) and not by_level.get(logging.WARNING)


def test_nothing_at_info_in_normal_operation(make, ha, clock, caplog):
    s = make()
    with caplog.at_level(logging.INFO, "shopping"):
        s.poll()
        ha.add("Milch")
        run(s, clock, 1)
        ha.check("Milch")
        run(s, clock, 61)
    assert caplog.records == []


# ---------------------------------------------------------------- names
@pytest.mark.parametrize("name, key, amount", [
    ("Milch", "milch", ""),
    ("2 Milch", "milch", "2"),
    ("2x Milch", "milch", "2x"),
    ("Milch x2", "milch", "x2"),
    ("Milch (2)", "milch", "2"),
    ("Milch 2", "milch", "2"),
    ("500 g Mehl", "mehl", "500 g"),
    ("Mehl 500g", "mehl", "500g"),
    ("1,5 l Milch", "milch", "1,5 l"),
    ("2 Liter Milch", "milch", "2 Liter"),
    ("2 Laugenbrezeln", "laugenbrezeln", "2"),
    ("H-Milch", "h-milch", ""),
    ("Grüner Tee", "gruner tee", ""),
    ("  Pfeffer   schwarz ", "pfeffer schwarz", ""),
    ("Vitamin B12", "vitamin b12", ""),
])
def test_split(name, key, amount):
    assert shop.split(name) == (key, amount)


# ---------------------------------------------------------------- answers
def bought(s, clock, name, days_ago):
    s.history.add({"time": (clock.t - timedelta(days=days_ago)).isoformat(timespec="seconds"), "list": LIST,
                   "event": "bought", "item": name, "key": shop.split(name)[0]})


def test_item_answer(s, ha, clock):
    clock.t = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
    for days_ago in (20, 13, 6):
        bought(s, clock, "Milch", days_ago)
    bought(s, clock, "2 Milch", 6)    # the same day counts once
    bought(s, clock, "H-Milch", 2)
    ha.add("Milch")
    run(s, clock, 1)
    out = json.loads(s.answer("item", "milch"))
    product, = out["products"]
    assert product["product"] in ("Milch", "2 Milch")
    assert product["purchases"] == 3 and product["days_ago"] == 6
    assert product["last_bought"] == "Saturday 2026-11-14"
    assert product["dates"] == ["Saturday 2026-11-14", "Saturday 2026-11-07", "Saturday 2026-10-31"]
    assert product["on_list"] == "open"
    assert "started on Saturday 2026-10-31" in out["note"]


def test_item_answer_matching_in_part(s, clock):
    bought(s, clock, "H-Milch", 2)
    bought(s, clock, "Hafermilch", 3)
    out = json.loads(s.answer("item", "Milch"))
    assert {p["product"] for p in out["products"]} == {"H-Milch", "Hafermilch"}
    assert "only matches in part" in out["note"]


def test_item_answer_never_bought(s, clock):
    assert "history is empty" in json.loads(s.answer("item", "Pfeffer"))["note"]
    bought(s, clock, "Milch", 3)
    out = json.loads(s.answer("item", "Pfeffer"))
    assert out["products"] == [] and out["note"].startswith("Not bought since the history started")


def test_recent_answer(s, clock):
    bought(s, clock, "Milch", 1)
    bought(s, clock, "Brot", 3)
    bought(s, clock, "Pfeffer", 40)
    out = json.loads(s.answer("recent", days=14))
    assert [p["item"] for p in out["purchases"]] == ["Milch", "Brot"]
    assert out["purchases"][0]["date"] == "Friday 2026-11-13"
    assert [p["item"] for p in json.loads(s.answer("recent", days=60))["purchases"]] == ["Milch", "Brot", "Pfeffer"]
    assert json.loads(s.answer("recent"))["days"] == shop.RECENT_DAYS
    assert s.answer("both").startswith("Error")


def test_events_for_the_api(s, ha, clock):
    ha.add("Milch")
    run(s, clock, 1)
    bought(s, clock, "Milch", 40)
    assert len(s.events(item="2 Milch")) == 2
    assert [e["event"] for e in s.events(days=7)] == ["added"]
    assert len(s.events(event="bought")) == 1


# ---------------------------------------------------------------- tool and settings
def test_shopping_history_tool_only_while_watching():
    class HA:
        enabled = True
    brain = Brain(make_cfg(anthropic_api_key="test"), HA())
    names = lambda access: {t["name"] for t in brain.tools_for(parse_access(access))}  # noqa: E731
    assert "shopping_history" not in names("admin")
    brain.shopping = SimpleNamespace(rhythm=None)
    for access in ("admin", "read", "shopping", "shopping, calendar"):
        assert "shopping_history" in names(access)
    for access in ("calendar", "guest"):
        assert "shopping_history" not in names(access)


def load(tmp_path, tmp_env, data):
    (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")
    tmp_env["BRAINCALL_CONFIG"] = str(tmp_path / "config.json")
    return config.load()


def test_settings_default(tmp_path, tmp_env):
    cfg = load(tmp_path, tmp_env, {})
    assert cfg.shopping_auto is False and cfg.shopping_list == LIST and cfg.shopping_bought_after_minutes == 60
    assert "shopping_lists" not in cfg
    assert config.log_level(cfg) == logging.INFO


@pytest.mark.parametrize("data, wanted, warned", [
    ({"shopping_lists": ["todo.einkauf"]}, "todo.einkauf", False),
    ({"shopping_lists": ["todo.einkauf"], "shopping_list": LIST}, "todo.einkauf", False),
    ({"shopping_lists": ["todo.einkauf", "todo.baumarkt"]}, "todo.einkauf", True),
    ({"shopping_lists": ["todo.einkauf"], "shopping_list": "todo.neu"}, "todo.neu", False),
    ({"shopping_lists": []}, LIST, False),
])
def test_old_shopping_lists_is_taken_over(tmp_path, tmp_env, data, wanted, warned):
    cfg = load(tmp_path, tmp_env, data)
    assert cfg.shopping_list == wanted and "shopping_lists" not in cfg
    assert bool(config.WARNINGS) == warned


@pytest.mark.parametrize("value, level", [
    ("trace", shop.TRACE), ("debug", logging.DEBUG), ("INFO", logging.INFO), ("warning", logging.WARNING),
    ("loud", logging.INFO),
])
def test_log_level(value, level):
    assert config.log_level(make_cfg(log_level=value)) == level
    assert bool(config.WARNINGS) == (value == "loud")


def test_invalid_minutes_fall_back(tmp_path, tmp_env):
    cfg = load(tmp_path, tmp_env, {"shopping_bought_after_minutes": 0, "shopping_auto": True})
    assert cfg.shopping_bought_after_minutes == 60 and cfg.shopping_auto is True
    assert config.WARNINGS


# ---------------------------------------------------------------- review of #71
def test_deleted_in_the_last_minute_of_the_hour_is_not_bought(s, ha, clock):
    ha.add("Milch")
    run(s, clock, 1)
    ha.check("Milch")
    run(s, clock, 59)          # seen checked for 58 minutes so far
    ha.delete("Milch")         # deleted shortly before the hour is over
    run(s, clock, 1)           # this poll would be at 60 minutes
    assert [e["event"] for e in events(s)] == ["added", "removed"] and ha.events == []


def test_a_purchase_that_cannot_be_written_is_kept_and_announced_after_writing(s, ha, clock, caplog, monkeypatch):
    ha.add("Milch")
    run(s, clock, 1)
    ha.check("Milch")
    real = s.history.add

    def broken(entry):
        if entry["event"] == "bought":
            raise OSError("disk full")
        real(entry)
    monkeypatch.setattr(s.history, "add", broken)
    with caplog.at_level(logging.WARNING, "shopping"):
        run(s, clock, 65)
    assert ha.items == [] and events(s, "bought") == [] and ha.events == []
    assert sum("writing failed" in r.getMessage() for r in caplog.records) == 1
    monkeypatch.setattr(s.history, "add", real)
    run(s, clock, 1)
    bought, = events(s, "bought")
    assert ha.events == [(shop.BOUGHT_EVENT, {"list": LIST, "item": "Milch", "key": "milch", "time": bought["time"]})]
    run(s, clock, 3)
    assert len(events(s, "bought")) == 1 and len(ha.events) == 1


def test_answers_do_not_wait_for_a_running_poll(s, ha, clock):
    ha.add("Milch")
    run(s, clock, 1)
    with s.lock:               # a poll that hangs on Home Assistant
        assert s.on_list() == {"milch": "open"}
        assert json.loads(s.answer("item", "Milch"))["on_list"] == "open"
