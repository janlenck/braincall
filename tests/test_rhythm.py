"""Rhythm list (#70): recurring products with their interval in Home Assistant, self-repair, automatic adding,
forecast, classification by Claude and the creation of missing lists."""
import json
import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
import rhythm as rh
import shopping as shop
from brain import Brain
from config import parse_access
from conftest import make_cfg

SHOP = "todo.shopping_list"
RHYTHM = "todo.shopping_rhythm"
START = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)  # Monday 10:00 in Berlin

CLASSES = {"Milch": ("staple", 7), "Butter": ("staple", 10), "Pfeffer": ("consumable", 120),
           "Hamster": ("one_off", 0), "Brot": ("staple", 3)}


class Clock:
    def __init__(self):
        self.t = START

    def __call__(self):
        return self.t


class FakeHA:
    """Home Assistant with to-do lists that know uid, status, due date and description."""

    def __init__(self, lists=(SHOP, RHYTHM)):
        self.lists = {e: [] for e in lists}
        self.running = True
        self.fail = set()          # lists that cannot be read
        self.empty = set()         # lists that answer without items although the entity counts open ones
        self.flow_fail = None
        self.entries = {}
        self.flows, self.renamed, self.events = [], [], []
        self._n = 0

    def core_running(self):
        return self.running

    def entity_exists(self, entity_id):
        return entity_id in self.lists

    def entity_state(self, entity_id):
        if entity_id in self.fail:
            raise OSError("connection refused")
        return str(sum(1 for i in self.lists[entity_id] if i["status"] == "needs_action"))

    def todo_raw(self, entity_id):
        if entity_id in self.fail:
            raise OSError("connection refused")
        if entity_id in self.empty:
            return []
        return [dict(i) for i in self.lists[entity_id]]

    def find(self, entity_id, ref):
        return next((i for i in self.lists[entity_id] if ref in (i.get("uid"), i["summary"])), None)

    def todo_call(self, service, entity_id, item=None, rename=None, status=None, due_date=None, description=None):
        items = self.lists[entity_id]
        if service == "add_item":
            self._n += 1
            new = {"summary": item, "uid": f"x{self._n}", "status": "needs_action"}
            if due_date:
                new["due"] = due_date
            if description:
                new["description"] = description
            items.append(new)
        elif service == "update_item":
            it = self.find(entity_id, item)
            assert it is not None, item
            if rename:
                it["summary"] = rename
            if status:
                it["status"] = status
            if due_date:
                it["due"] = due_date
            if description is not None:
                it["description"] = description
        elif service == "remove_item":
            items.remove(self.find(entity_id, item))

    def remove_todo(self, entity_id, item):
        self.todo_call("remove_item", entity_id, item=item)

    def fire_event(self, name, data):
        self.events.append((name, data))

    def config_entries(self, domain):
        return self.entries.get(domain, [])

    def config_flow(self, handler, data):
        if self.flow_fail:
            raise self.flow_fail
        self.flows.append((handler, data))
        entity_id = SHOP if handler == "shopping_list" else "todo." + data["todo_list_name"].lower().replace(" ", "_")
        self.lists[entity_id] = []
        return {"type": "create_entry"}

    def rename_entity(self, entity_id, name):
        self.renamed.append((entity_id, name))

    # what the user does in the app
    def summaries(self, entity_id):
        return [i["summary"] for i in self.lists[entity_id]]

    def entry(self, product):
        return next(i for i in self.lists[RHYTHM] if i["summary"].split(" (")[0] == product)


class Classifier:
    def __init__(self):
        self.calls = []
        self.fail = None

    def __call__(self, names):
        self.calls.append(list(names))
        if self.fail:
            raise self.fail
        return {n: {"class": CLASSES[n][0], "typical_days": CLASSES[n][1]} for n in names if n in CLASSES}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ha():
    return FakeHA()


@pytest.fixture
def classify():
    return Classifier()


@pytest.fixture
def make(tmp_path, ha, clock, classify, monkeypatch):
    monkeypatch.setattr(shop.time, "sleep", lambda s: None)

    def make_watcher(**cfg):
        cfg.setdefault("timezone", "Europe/Berlin")
        c = make_cfg(**cfg)
        s = shop.Shopping(c, ha, tmp_path / "shopping-history.jsonl", now=clock)
        if c.shopping_rhythm_list:
            rh.Rhythm(c, ha, s, tmp_path / "shopping-products.json", classify=classify)
        s.poll()
        return s
    return make_watcher


@pytest.fixture
def s(make):
    return make()


def run(s, clock, minutes):
    for _ in range(minutes):
        clock.t += timedelta(minutes=1)
        s.poll()


def buy(s, ha, clock, name, days_later=0):
    """days_later days on, the product goes on the list, is checked off in the shop and stays checked."""
    clock.t += timedelta(days=days_later)
    if name not in ha.summaries(SHOP):
        ha.lists[SHOP].append({"summary": name, "uid": f"s{len(ha.lists[SHOP])}-{clock.t.timestamp()}",
                               "status": "needs_action"})
    run(s, clock, 1)
    next(i for i in ha.lists[SHOP] if i["summary"] == name)["status"] = "completed"
    run(s, clock, 62)
    assert name not in ha.summaries(SHOP)


def history(s, kind=None):
    return [e for e in s.history.entries() if kind is None or e["event"] == kind]


# ---------------------------------------------------------------- reading entries
@pytest.mark.parametrize("text, days, german", [
    ("7 days", 7, False), ("1 day", 1, False), ("2 weeks", 14, False), ("3 months", 90, False),
    ("daily", 1, False), ("weekly", 7, False), ("monthly", 30, False), ("never", rh.NEVER, False),
    ("10 Tage", 10, True), ("2 Wochen", 14, True), ("wöchentlich", 7, True), ("nie", rh.NEVER, True),
    ("Bio", None, False), ("ten days", None, False), ("0 days", None, False),
])
def test_parse_interval(text, days, german):
    assert rh.parse_interval(text) == (days, german if days is not None else False)


def test_parse_entry():
    e = rh.parse_entry({"summary": "Milch (7 days)", "uid": "u1", "status": "completed", "due": "2026-11-20",
                        "description": "staple · learned: 7 days · last: 2026-11-13 · 3 purchases"})
    assert (e.product, e.key, e.interval, e.paused, e.due, e.learned, e.cls) == (
        "Milch", "milch", 7, True, date(2026, 11, 20), 7, "staple")
    assert rh.parse_entry({"summary": "Milch (Bio)"}).product == "Milch (Bio)"   # brackets that are no interval
    odd = rh.parse_entry({"summary": "Milch (10 Tg)"})
    assert odd.product == "Milch" and not odd.readable


# ---------------------------------------------------------------- creating the lists
@pytest.fixture
def bare(tmp_path, clock, classify, monkeypatch):
    """A watcher on a Home Assistant without any list."""
    monkeypatch.setattr(shop.time, "sleep", lambda s: None)
    ha = FakeHA(lists=())
    c = make_cfg(timezone="Europe/Berlin")
    s = shop.Shopping(c, ha, tmp_path / "shopping-history.jsonl", now=clock)
    rh.Rhythm(c, ha, s, tmp_path / "shopping-products.json", classify=classify)
    return s, ha


def test_lists_are_created_when_home_assistant_has_started(bare, clock, caplog):
    s, ha = bare
    ha.running = False
    s.poll()
    assert ha.flows == [] and not s.lists_checked     # integrations may still be loading
    ha.running = True
    with caplog.at_level(logging.INFO, "shopping"):
        run(s, clock, 1)
    assert ha.flows == [("shopping_list", {}), ("local_todo", {"todo_list_name": "Shopping rhythm"})]
    assert ha.renamed == [(RHYTHM, "Recurring purchases")]
    assert s.rhythm.on and s.lists_checked
    assert sum("Created the" in r.getMessage() for r in caplog.records) == 2
    run(s, clock, 5)
    assert len(ha.flows) == 2


def test_existing_lists_are_left_alone(s, ha):
    assert ha.flows == [] and ha.renamed == [] and s.rhythm.on


def test_a_list_that_cannot_be_created_switches_its_part_off(bare, clock, caplog):
    s, ha = bare
    ha.lists[SHOP] = []
    ha.flow_fail = ValueError("401 unauthorized")
    with caplog.at_level(logging.WARNING, "shopping"):
        s.poll()
    assert not s.rhythm.on and s.lists_checked
    warning, = [r.getMessage() for r in caplog.records]
    assert "could not be created" in warning and "Local To-do" in warning and "Shopping rhythm" in warning
    ha.lists[SHOP].append({"summary": "Milch", "uid": "a", "status": "needs_action"})
    run(s, clock, 1)
    assert [e["event"] for e in history(s)] == ["added"]   # the shopping list works on its own


def test_a_local_todo_list_of_that_name_is_not_created_twice(bare):
    s, ha = bare
    ha.lists[SHOP] = []
    ha.entries["local_todo"] = [{"title": "Shopping rhythm", "entry_id": "e1"}]
    s.poll()
    assert ha.flows == [] and not s.rhythm.on


def test_a_new_rhythm_list_is_filled_from_the_history(bare, clock, classify):
    s, ha = bare
    for days_ago, name in ((20, "Milch"), (13, "Milch"), (6, "Milch"), (20, "Pfeffer"), (10, "Hamster"),
                           (5, "Hamster"), (1, "Hamster")):
        s.history.add({"time": (clock.t - timedelta(days=days_ago)).isoformat(), "list": SHOP,
                       "event": "bought", "item": name, "key": shop.split(name)[0]})
    s.poll()
    assert ha.summaries(RHYTHM) == ["Milch (7 days)"]
    assert classify.calls == [["Milch", "Pfeffer", "Hamster"]]


# ---------------------------------------------------------------- learning
def test_staple_after_two_consumable_after_three_one_off_never(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Pfeffer")
    buy(s, ha, clock, "Hamster")
    assert ha.summaries(RHYTHM) == []
    buy(s, ha, clock, "Milch", days_later=7)
    milch = ha.entry("Milch")
    assert milch["summary"] == "Milch (7 days)"
    assert milch["due"] == "2026-11-16"
    assert milch["description"] == "staple · learned: 7 days · last: 2026-11-09 · 2 purchases"
    buy(s, ha, clock, "Pfeffer", days_later=100)
    buy(s, ha, clock, "Hamster")
    assert ha.summaries(RHYTHM) == ["Milch (7 days)"]
    buy(s, ha, clock, "Pfeffer", days_later=110)
    buy(s, ha, clock, "Hamster")
    assert ha.summaries(RHYTHM) == ["Milch (7 days)", "Pfeffer (108 days)"]   # median of 107 and 110
    assert [e["key"] for e in history(s, "listed")] == ["milch", "pfeffer"]


def test_two_purchases_far_apart_use_the_typical_interval(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=30)   # a holiday in between
    milch = ha.entry("Milch")
    assert milch["summary"] == "Milch (7 days)" and "estimated: 7 days" in milch["description"]
    buy(s, ha, clock, "Milch", days_later=8)
    buy(s, ha, clock, "Milch", days_later=6)
    milch = ha.entry("Milch")
    assert milch["summary"] == "Milch (8 days)" and "learned: 8 days" in milch["description"]  # median of 30, 8, 6


def test_an_interval_changed_by_the_user_stays(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    ha.entry("Milch")["summary"] = "Milch (10 days)"
    buy(s, ha, clock, "Milch", days_later=5)
    milch = ha.entry("Milch")
    assert milch["summary"] == "Milch (10 days)"
    assert "learned: 6 days" in milch["description"]
    assert milch["due"] == (clock.t.date() + timedelta(days=10)).isoformat()


def test_a_removed_interval_is_filled_in_again(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    ha.entry("Milch")["summary"] = "Milch"
    run(s, clock, 6)
    assert ha.entry("Milch")["summary"] == "Milch (7 days)"


@pytest.mark.parametrize("typed, written", [("10 Tage", "10 days"), ("2 Wochen", "14 days"),
                                            ("wöchentlich", "7 days"), ("nie", "never")])
def test_german_intervals_are_rewritten_and_stay_the_users(s, ha, clock, typed, written):
    buy(s, ha, clock, "Butter")
    buy(s, ha, clock, "Butter", days_later=9)
    ha.entry("Butter")["summary"] = f"Butter ({typed})"
    run(s, clock, 6)
    assert ha.entry("Butter")["summary"] == f"Butter ({written})"
    buy(s, ha, clock, "Butter", days_later=9)
    assert ha.entry("Butter")["summary"] == f"Butter ({written})"


def test_an_unreadable_interval_is_left_alone_with_one_warning(s, ha, clock, caplog):
    ha.lists[RHYTHM].append({"summary": "Milch (10 Tg)", "uid": "m", "status": "needs_action", "due": "2026-11-01"})
    with caplog.at_level(logging.WARNING, "shopping"):
        run(s, clock, 30)
    assert ha.summaries(RHYTHM) == ["Milch (10 Tg)"] and ha.summaries(SHOP) == []
    assert sum("cannot be read" in r.getMessage() for r in caplog.records) == 1


# ---------------------------------------------------------------- what the user does in the rhythm list
def test_a_paused_entry_is_not_added_and_the_next_purchase_ends_the_pause(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    ha.entry("Milch")["summary"] = "Milch (9 days)"
    ha.entry("Milch")["status"] = "completed"
    clock.t += timedelta(days=20)
    run(s, clock, 90)                                   # long past due, and longer than the hour
    assert ha.summaries(SHOP) == [] and ha.entry("Milch")["status"] == "completed"
    buy(s, ha, clock, "Milch")                          # put on the list by hand and bought
    assert ha.entry("Milch")["status"] == "needs_action"
    assert ha.entry("Milch")["summary"] == "Milch (9 days)"


def test_a_deleted_entry_comes_back_with_the_next_purchase(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    ha.lists[RHYTHM].clear()                            # deleted by mistake, or "clear completed"
    clock.t += timedelta(days=30)
    run(s, clock, 10)
    assert ha.summaries(SHOP) == [] and ha.summaries(RHYTHM) == []
    buy(s, ha, clock, "Milch")
    assert ha.summaries(RHYTHM) == ["Milch (18 days)"]   # median of 7 and 30, rounded
    assert ha.entry("Milch")["description"].endswith("3 purchases")


def test_never_stays_and_is_never_added(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    ha.entry("Milch")["summary"] = "Milch (never)"
    for _ in range(3):
        buy(s, ha, clock, "Milch", days_later=7)
    clock.t += timedelta(days=60)
    run(s, clock, 10)
    assert ha.entry("Milch")["summary"] == "Milch (never)" and ha.summaries(SHOP) == []
    assert ha.entry("Milch")["description"].endswith("5 purchases")


def test_an_entry_made_by_hand_counts_without_history(s, ha, clock):
    ha.lists[RHYTHM].append({"summary": "Spülmaschinentabs (60 days)", "uid": "t", "status": "needs_action"})
    ha.lists[RHYTHM].append({"summary": "Hamsterfutter (14 days)", "uid": "h", "status": "needs_action",
                             "due": "2026-11-01"})
    run(s, clock, 6)
    assert ha.find(RHYTHM, "t")["due"] == "2027-01-01"   # today + interval
    assert ha.summaries(SHOP) == ["Hamsterfutter"]


def test_a_due_date_moved_by_the_user_stays_until_the_next_purchase(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    ha.entry("Milch")["due"] = "2026-12-24"
    clock.t += timedelta(days=20)
    run(s, clock, 10)
    assert ha.entry("Milch")["due"] == "2026-12-24" and ha.summaries(SHOP) == []


def test_a_checked_entry_of_the_rhythm_list_is_never_removed(s, ha, clock):
    ha.lists[RHYTHM].append({"summary": "Pfeffer (never)", "uid": "p", "status": "completed"})
    run(s, clock, 120)
    assert ha.summaries(RHYTHM) == ["Pfeffer (never)"]


# ---------------------------------------------------------------- automatic adding
def test_due_products_are_added_once(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)            # due on 2026-11-16
    clock.t += timedelta(days=6)
    run(s, clock, 10)
    assert ha.summaries(SHOP) == []
    clock.t += timedelta(days=1)
    run(s, clock, 6)
    assert ha.summaries(SHOP) == ["Milch"]
    added, = [e for e in history(s, "added") if e.get("by") == "rhythm"]
    assert added["due"] == "2026-11-16" and added["key"] == "milch"
    run(s, clock, 10)
    assert ha.summaries(SHOP) == ["Milch"]
    assert len([e for e in history(s, "added") if e["key"] == "milch"]) == 3   # 2 by hand, 1 by the rhythm list


def test_an_added_product_that_is_deleted_comes_back_only_with_a_new_due_date(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    clock.t += timedelta(days=8)
    run(s, clock, 6)
    ha.lists[SHOP].clear()                              # "not needed right now"
    run(s, clock, 60)
    assert ha.summaries(SHOP) == []
    ha.entry("Milch")["due"] = clock.t.date().isoformat()   # moved by the user
    run(s, clock, 6)
    assert ha.summaries(SHOP) == ["Milch"]


def test_products_on_the_list_are_not_added_again(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    ha.lists[SHOP].append({"summary": "2 Milch", "uid": "m", "status": "completed"})
    clock.t += timedelta(days=8)
    run(s, clock, 6)
    assert ha.summaries(SHOP) == ["2 Milch"]


def test_the_cycle_goes_on_after_the_purchase(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    clock.t += timedelta(days=7)
    run(s, clock, 6)
    assert ha.summaries(SHOP) == ["Milch"]
    buy(s, ha, clock, "Milch")                          # the added item is bought
    assert ha.entry("Milch")["due"] == "2026-11-23"
    clock.t += timedelta(days=7)
    run(s, clock, 6)
    assert ha.summaries(SHOP) == ["Milch"]


def test_nothing_is_added_while_a_list_cannot_be_read(s, ha, clock, caplog):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    ha.fail.add(RHYTHM)
    clock.t += timedelta(days=10)
    with caplog.at_level(logging.WARNING, "shopping"):
        run(s, clock, 30)
    assert ha.summaries(SHOP) == []
    assert sum("Rhythm list" in r.getMessage() and "cannot be read" in r.getMessage() for r in caplog.records) == 1
    ha.fail = {SHOP}
    ha.fail.discard(RHYTHM)
    s.rhythm.last_sync = None
    with caplog.at_level(logging.WARNING, "shopping"):
        s.rhythm.sync(clock.t)
    assert ha.summaries(SHOP) == []


# ---------------------------------------------------------------- classification
def test_every_product_is_classified_once(s, ha, clock, classify, tmp_path):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    assert classify.calls == [["Milch"]]
    saved = json.loads((tmp_path / "shopping-products.json").read_text(encoding="utf-8"))
    assert saved["milch"]["class"] == "staple" and saved["milch"]["typical_days"] == 7
    assert rh.Products(tmp_path / "shopping-products.json").get("milch")["class"] == "staple"   # after a restart


def test_a_failed_classification_counts_as_consumable_and_is_asked_again(s, ha, clock, classify, caplog):
    classify.fail = RuntimeError("overloaded")
    with caplog.at_level(logging.WARNING, "shopping"):
        buy(s, ha, clock, "Milch")
        buy(s, ha, clock, "Milch", days_later=7)
    assert ha.summaries(RHYTHM) == []                   # a consumable needs 3
    assert len(classify.calls) == 2 and any("classifying" in r.getMessage() for r in caplog.records)
    classify.fail = None
    buy(s, ha, clock, "Milch", days_later=7)
    assert ha.summaries(RHYTHM) == ["Milch (7 days)"]


def test_classify_products_uses_structured_output():
    calls = []

    class Messages:
        def create(self, **kw):
            calls.append(kw)
            text = json.dumps({"products": [{"name": "Milch", "class": "staple", "typical_days": 7},
                                            {"name": "Klavier", "class": "one_off", "typical_days": 0}]})
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])

    brain = Brain(make_cfg(anthropic_api_key="test"), SimpleNamespace(enabled=True))
    brain.client = SimpleNamespace(messages=Messages())
    out = brain.classify_products(["Milch", "Klavier"])
    assert out == {"Milch": {"class": "staple", "typical_days": 7}, "Klavier": {"class": "one_off", "typical_days": 0}}
    kw, = calls
    assert kw["output_config"]["format"]["type"] == "json_schema" and "tool_choice" not in kw
    assert "- Milch\n- Klavier" in kw["messages"][0]["content"]


# ---------------------------------------------------------------- answers and changes in a call
def test_forecast_and_tool_answers(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)            # due 2026-11-16
    buy(s, ha, clock, "Butter")
    buy(s, ha, clock, "Butter", days_later=10)          # bought 2026-11-19, due 2026-11-29
    ha.lists[RHYTHM].append({"summary": "Pfeffer (90 days)", "uid": "p", "status": "completed"})
    assert ha.summaries(SHOP) == ["Milch"]               # was due while the butter was bought
    ha.lists[SHOP].clear()                              # "not needed right now"
    run(s, clock, 1)
    clock.t += timedelta(days=8)                        # 2026-11-27: butter due in 2 of 10 days
    products = s.rhythm.forecast()
    assert [(p["product"], p["status"]) for p in products] == [
        ("Milch", "probably_out"), ("Butter", "running_low"), ("Pfeffer", "paused")]
    assert products[0]["next_due"] == "Monday 2026-11-16" and products[0]["interval"] == "7 days"
    due = json.loads(s.answer("due"))
    assert [p["product"] for p in due["due"]] == ["Milch", "Butter"] and "estimate" in due["note"]
    assert len(json.loads(s.answer("stock"))["products"]) == 3
    item, = json.loads(s.answer("item", "Milch"))["products"]
    assert item["class"] == "staple" and item["interval"] == "7 days" and item["status"] == "probably_out"


def test_without_rhythm_list_there_is_no_forecast(make):
    s = make(shopping_rhythm_list="")
    assert s.rhythm is None and s.answer("due").startswith("There is no rhythm list")


@pytest.mark.parametrize("action, value, check", [
    ("interval", "60", lambda e: e["summary"] == "Milch (60 days)" and e["due"] == "2027-01-08"),
    ("interval", "never", lambda e: e["summary"] == "Milch (never)"),
    ("pause", None, lambda e: e["status"] == "completed"),
    ("due", "2026-12-24", lambda e: e["due"] == "2026-12-24"),
])
def test_edit(s, ha, clock, action, value, check):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    out = s.rhythm.edit(action, "milch", interval=value if action == "interval" else None,
                        due=value if action == "due" else "")
    assert out.startswith("Done.")
    assert check(ha.entry("Milch"))


def test_edit_remove_resume_and_unknown(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    s.rhythm.edit("pause", "Milch")
    s.rhythm.edit("resume", "Milch")
    assert ha.entry("Milch")["status"] == "needs_action"
    assert "not on the rhythm list" in s.rhythm.edit("pause", "Klavier")
    assert s.rhythm.edit("remove", "Milch").startswith("Done.") and ha.summaries(RHYTHM) == []


def test_tools_by_access(s):
    brain = Brain(make_cfg(anthropic_api_key="test"), SimpleNamespace(enabled=True))
    brain.shopping = s
    names = lambda access: {t["name"] for t in brain.tools_for(parse_access(access))}  # noqa: E731
    assert {"shopping_history", "shopping_rhythm"} <= names("admin")
    assert "shopping_rhythm" not in names("shopping") and "shopping_history" in names("shopping")


def test_rhythm_list_equal_to_the_shopping_list_means_none(tmp_path, tmp_env):
    (tmp_path / "config.json").write_text(json.dumps({"shopping_rhythm_list": SHOP}), encoding="utf-8")
    tmp_env["BRAINCALL_CONFIG"] = str(tmp_path / "config.json")
    assert config.load().shopping_rhythm_list == ""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    assert config.load().shopping_rhythm_list == RHYTHM


def test_an_added_item_deleted_before_the_next_poll_does_not_hide_a_later_one(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    clock.t += timedelta(days=8)
    s.rhythm.sync(clock.t)
    assert ha.summaries(SHOP) == ["Milch"]
    ha.lists[SHOP].clear()                              # gone before the watcher saw it
    run(s, clock, 2)
    ha.lists[SHOP].append({"summary": "Milch", "uid": "late", "status": "needs_action"})
    run(s, clock, 1)
    assert history(s, "added")[-1]["uid"] == "late"


# ---------------------------------------------------------------- second review of #71
def test_a_fill_that_failed_is_done_later(bare, clock):
    s, ha = bare
    for days_ago in (14, 7):
        s.history.add({"time": (clock.t - timedelta(days=days_ago)).isoformat(), "list": SHOP,
                       "event": "bought", "item": "Milch", "key": "milch"})
    ha.fail = {RHYTHM}               # created, but not readable right after
    s.poll()
    assert RHYTHM in ha.lists and ha.summaries(RHYTHM) == [] and s.rhythm.unfilled
    ha.fail = set()
    run(s, clock, 1)
    assert ha.summaries(RHYTHM) == ["Milch (7 days)"] and not s.rhythm.unfilled
    run(s, clock, 3)
    assert ha.summaries(RHYTHM) == ["Milch (7 days)"]


@pytest.mark.parametrize("problem", ["error", "empty"])
def test_a_purchase_update_that_failed_is_done_later_without_duplicates(s, ha, clock, problem):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    if problem == "error":
        ha.fail = {RHYTHM}
    else:
        ha.empty = {RHYTHM}          # an answer that would make the entry look missing
    buy(s, ha, clock, "Milch", days_later=7)
    assert s.rhythm.pending == {"milch": "Milch"}
    ha.fail, ha.empty = set(), set()
    run(s, clock, 1)
    assert ha.summaries(RHYTHM) == ["Milch (7 days)"] and s.rhythm.pending == {}
    assert ha.entry("Milch")["description"].endswith("3 purchases")
    assert ha.entry("Milch")["due"] == (clock.t.date() + timedelta(days=7)).isoformat()


def test_a_purchase_leaves_an_unreadable_entry_alone(s, ha, clock):
    ha.lists[RHYTHM].append({"summary": "Milch (10 Tg)", "uid": "m", "status": "completed"})
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    assert ha.lists[RHYTHM] == [{"summary": "Milch (10 Tg)", "uid": "m", "status": "completed"}]


def test_the_users_interval_stays_when_the_learned_one_catches_up(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    ha.entry("Milch")["summary"] = "Milch (10 days)"
    buy(s, ha, clock, "Milch", days_later=13)        # learned: median of 7 and 13 = 10, like the user's
    assert ha.entry("Milch")["description"].startswith("staple · learned: 10 days · set by hand")
    buy(s, ha, clock, "Milch", days_later=7)         # learned 7 again
    assert ha.entry("Milch")["summary"] == "Milch (10 days)"
    ha.entry("Milch")["summary"] = "Milch"           # given back to BrainCall
    run(s, clock, 6)
    assert ha.entry("Milch")["summary"] == "Milch (7 days)" and "set by hand" not in ha.entry("Milch")["description"]
    buy(s, ha, clock, "Milch", days_later=9)
    assert ha.entry("Milch")["summary"] == "Milch (8 days)"   # follows what is learned again


def test_german_and_asked_for_intervals_are_the_users(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    ha.entry("Milch")["summary"] = "Milch (1 Woche)"  # the same value as learned
    run(s, clock, 6)
    assert "set by hand" in ha.entry("Milch")["description"]
    buy(s, ha, clock, "Butter")
    buy(s, ha, clock, "Butter", days_later=10)
    s.rhythm.edit("interval", "Butter", interval="10")
    assert "set by hand" in ha.entry("Butter")["description"]


def test_an_untrusted_shopping_list_answer_adds_nothing(s, ha, clock):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    ha.lists[SHOP].append({"summary": "Milch", "uid": "m", "status": "needs_action"})
    clock.t += timedelta(days=8)
    ha.empty = {SHOP}
    s.rhythm.sync(clock.t)
    assert ha.summaries(SHOP) == ["Milch"]


def test_an_added_product_whose_line_cannot_be_written_is_not_added_twice(s, ha, clock, monkeypatch):
    buy(s, ha, clock, "Milch")
    buy(s, ha, clock, "Milch", days_later=7)
    real = s.history.add

    def broken(entry):
        if entry.get("by") == "rhythm":
            raise OSError("disk full")
        real(entry)
    monkeypatch.setattr(s.history, "add", broken)
    clock.t += timedelta(days=8)
    run(s, clock, 6)
    assert ha.summaries(SHOP) == ["Milch"]
    ha.lists[SHOP].clear()                            # "not needed right now"
    run(s, clock, 30)
    assert ha.summaries(SHOP) == []
    monkeypatch.setattr(s.history, "add", real)
    run(s, clock, 1)
    assert len([e for e in history(s, "added") if e.get("by") == "rhythm"]) == 1
    run(s, clock, 30)
    assert ha.summaries(SHOP) == []
