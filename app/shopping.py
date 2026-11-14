"""Automatic shopping list (#69): BrainCall watches the shopping list in Home Assistant once a minute.

An item that stays checked off for shopping_bought_after_minutes (default an hour) counts as bought: it is removed
from the list and stored in shopping-history.jsonl in the log folder, with the time it was checked. Checked by
mistake and unchecked again within the hour: nothing is stored. An item that disappears before its hour is over
(deleted by hand, "clear completed", removed by Claudine) is never a purchase.

The snapshot of the list lives in memory only: after a restart the first poll is the starting point, writes no
events, and every item that is checked then starts its hour anew."""
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ha import fold

log = logging.getLogger("shopping")

TRACE = 5  # below DEBUG: every poll
logging.addLevelName(TRACE, "TRACE")

POLL_SECONDS = 60
BUILTIN_LIST = "todo.shopping_list"  # the entity of Home Assistant's own Shopping list integration
BOUGHT_EVENT = "braincall_shopping_bought"
RECENT_DAYS = 14   # shopping_history recent without days
MAX_DATES = 12     # dates of one product in an answer
MAX_RECENT = 60    # purchases in a recent answer
MAX_MATCHES = 8    # products in an item answer when the name only matches in part

# An amount before or after the name: "2 Milch", "2x Milch", "500 g Mehl", "Milch 2", "Milch (2)", "Mehl 500g",
# "Milch x2". The unit list is short on purpose: a number alone is an amount, a word that is no unit stays in the name.
_UNIT = (r"(?:x|×|stk\.?|stück|st\.?|g|gr|kg|mg|ml|cl|l|ltr\.?|liter|pck\.?|pack|packung(?:en)?|pkg|dosen?|"
         r"flaschen?|becher|bund|beutel|tüten?|glas|gläser|kisten?|rollen?|pcs?|pieces?|cans?|bottles?)")
_NUM = r"\d+(?:[.,]\d+)?(?:/\d+)?"
_AMOUNT = rf"(?:{_NUM}\s*{_UNIT}?|{_UNIT}\s*{_NUM})"
_LEAD = re.compile(rf"^\s*({_AMOUNT})\.?\s+(.+?)\s*$", re.I)
_TRAIL = re.compile(rf"^\s*(.+?)\s+\(?({_AMOUNT})\)?\s*$", re.I)


def parts(name: str) -> tuple[str, str]:
    """(product, amount) as written: "2 Milch" -> ("Milch", "2"), "Mehl 500 g" -> ("Mehl", "500 g")."""
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    amount = ""
    m = _LEAD.match(text)
    if m:
        amount, text = m.group(1), m.group(2)
    else:
        m = _TRAIL.match(text)
        if m:
            text, amount = m.group(1), m.group(2)
    return text, re.sub(r"\s+", " ", amount).strip()


def split(name: str) -> tuple[str, str]:
    """(key, amount) of an item: the name folded for counting and the amount written before or after it.
    "2 Milch", "milch" and "Milch (2)" are the same product "milch"; "H-Milch" is another one."""
    text, amount = parts(name)
    return fold(text).strip(" .,;:-"), amount


@dataclass
class Item:
    name: str
    checked: bool
    since: datetime | None = None  # checked since (the first poll that saw it checked)
    uid: str = ""
    removing: bool = False         # a removal was tried and failed: tried again on the next poll


class History:
    """shopping-history.jsonl: one JSON line per event, appended, never pruned and never rewritten.
    A line cut off by a crash is skipped when reading and does not break the lines after it."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def add(self, entry: dict):
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            cut = False
            if self.path.exists() and self.path.stat().st_size:
                with open(self.path, "rb") as f:
                    f.seek(-1, 2)
                    cut = f.read(1) != b"\n"
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(("\n" if cut else "") + line)

    def entries(self) -> list[dict]:
        with self.lock:
            if not self.path.exists():
                return []
            text = self.path.read_text(encoding="utf-8", errors="replace")
        out = []
        for line in text.splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(e, dict) and e.get("event") and e.get("key") is not None and _time(e) is not None:
                out.append(e)
        return out


def list_title(entity_id: str) -> str:
    """The name a Local To-do list needs to get this entity id: todo.shopping_rhythm -> "Shopping rhythm"."""
    return entity_id.split(".", 1)[-1].replace("_", " ").capitalize()


def create_list(ha, entity_id: str, what: str, display_name: str = "") -> bool:
    """Creates a missing to-do list in Home Assistant: todo.shopping_list as the built-in Shopping list, any other
    as a Local To-do list. True when it exists afterwards; otherwise one warning with what to do by hand.
    Raises only if Home Assistant cannot be asked (tried again later)."""
    builtin = entity_id == BUILTIN_LIST
    title = list_title(entity_id)
    how = ("Settings > Devices & services > Add integration > Shopping list" if builtin else
           f"Settings > Devices & services > Add integration > Local To-do, named \"{title}\"")
    try:
        if not builtin and any(e.get("title") == title for e in ha.config_entries("local_todo")):
            raise ValueError(f"a Local To-do list \"{title}\" exists, but not as {entity_id}: "
                             "set the setting to its entity id")
        ha.config_flow("shopping_list" if builtin else "local_todo", {} if builtin else {"todo_list_name": title})
        for _ in range(20):
            if ha.entity_exists(entity_id):
                break
            time.sleep(0.5)
        else:
            raise ValueError(f"it was created, but {entity_id} did not show up")
    except Exception as e:  # noqa: BLE001
        log.warning("The %s %s is missing and could not be created: %s. Create it by hand (%s); until then it is "
                    "not used", what, entity_id, e, how)
        return False
    if display_name:
        try:
            ha.rename_entity(entity_id, display_name)
        except Exception as e:  # noqa: BLE001
            log.info("The %s %s keeps its name: %s", what, entity_id, e)
    log.info("Created the %s %s in Home Assistant", what, entity_id)
    return True


def _time(entry: dict) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(entry.get("time")))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


class Shopping:
    def __init__(self, cfg, ha, path, now=None):
        self.cfg = cfg
        self.ha = ha
        self.entity_id = cfg.shopping_list
        self.after = timedelta(minutes=cfg.shopping_bought_after_minutes)
        self.history = History(path)
        self.now = now or (lambda: datetime.now(timezone.utc))
        try:
            self.tz = ZoneInfo(cfg.timezone) if cfg.timezone else None
        except (ZoneInfoNotFoundError, ValueError):
            self.tz = None
        self.items: dict[str, Item] | None = None  # None until the first poll after the start
        self.failing = False
        self.lock = threading.Lock()
        self.rhythm = None          # rhythm.Rhythm while there is a rhythm list (#70)
        self.lists_checked = False  # missing lists were looked for (and created) after the start
        self.expected: list[str] = []  # names just added by the rhythm list: not logged again as added
        self.view: dict = {}        # {key: "open" | "checked"} as last seen, for answers without waiting for a poll
        self.pending: list = []     # purchases whose history line could not be written yet
        self.write_failing = False

    def describe(self) -> str:
        return (f"watching {self.entity_id}, items checked for {self.cfg.shopping_bought_after_minutes} min "
                f"count as bought (history: {self.history.path.name})"
                + (f", rhythm list {self.rhythm.entity_id}" if self.rhythm else ""))

    def _ensure_lists(self) -> bool:
        """Once after the start, when Home Assistant is running: creates the lists that are missing. False while
        Home Assistant is still starting (an integration not loaded yet must not look like a missing list)."""
        if not self.ha.core_running():
            log.log(TRACE, "Shopping list: Home Assistant is still starting")
            return False
        if not self.ha.entity_exists(self.entity_id):
            create_list(self.ha, self.entity_id, "shopping list")
        if self.rhythm is not None:
            self.rhythm.ensure()
        self.lists_checked = True
        return True

    # ------------------------------------------------------------------ watching
    def poll(self):
        """One look at the list: compare with the last one, remove what stayed checked long enough.
        The lock only keeps two polls apart; answers read self.view and the history file and never wait for it."""
        with self.lock:
            now = self.now()
            self._flush()
            try:
                if not self.lists_checked and not self._ensure_lists():
                    return
                state = self.ha.entity_state(self.entity_id)
                if state in ("unavailable", "unknown", "None", ""):
                    raise ValueError(f"the list is {state or 'empty'}")
                raw = self.ha.todo_raw(self.entity_id)
                if not raw and state.isdigit() and int(state) > 0:
                    raise ValueError(f"no items in the answer, but {state} open")
            except Exception as e:  # noqa: BLE001  an answer that cannot be trusted changes nothing
                self._failed(e)
                return
            if self.failing:
                self.failing = False
                log.warning("Shopping list %s can be read again", self.entity_id)
            seen = self._read(raw)
            log.log(TRACE, "Shopping list %s: %d items, %d checked", self.entity_id, len(seen),
                    sum(1 for i in seen.values() if i.checked))
            if self.items is None:
                for item in seen.values():
                    item.since = now if item.checked else None
                self.items = seen
                self.view = self._view()
                checked = sum(1 for i in seen.values() if i.checked)
                log.debug("Shopping list %s: starting with %d items, %d checked (their time starts now)",
                          self.entity_id, len(seen), checked)
                return
            self._compare(seen, now)
            self._remove_due(now)
            self.view = self._view()
            if self.rhythm is not None and self.rhythm.due_for_sync(now):
                self.rhythm.sync(now)

    def _failed(self, e):
        if self.failing:
            log.log(TRACE, "Shopping list %s still not readable: %s", self.entity_id, e)
            return
        self.failing = True
        log.warning("Shopping list %s cannot be read: %s. Nothing is changed until it works again",
                    self.entity_id, e)

    @staticmethod
    def _read(raw: list) -> dict[str, Item]:
        """{id: Item}: the uid, or the name for lists without uids (a second item of that name gets #2 …)."""
        out: dict[str, Item] = {}
        for i in raw:
            name = str(i.get("summary") or "").strip()
            uid = str(i.get("uid") or "").strip()
            key = uid or f"name:{name}"
            n = 2
            while key in out:
                key = f"{uid or 'name:' + name}#{n}"
                n += 1
            out[key] = Item(name=name, checked=i.get("status") == "completed", uid=uid)
        return out

    @staticmethod
    def _rekey(old: dict, seen: dict) -> dict:
        """All ids changed at once (the integration was reloaded): the old items under the new ids, matched by
        name and status, so a reload is not logged as everything removed and added again."""
        pool = list(old.items())
        out = {}
        for nid, item in seen.items():
            for j, (_, it) in enumerate(pool):
                if fold(it.name) == fold(item.name) and it.checked == item.checked:
                    out[nid] = pool.pop(j)[1]
                    break
        out.update(pool)
        log.debug("Shopping list: all item ids changed, %d of %d items matched by name", len(old) - len(pool),
                  len(old))
        return out

    def _compare(self, seen: dict, now: datetime):
        old = self.items
        if old and seen and not old.keys() & seen.keys():
            old = self._rekey(old, seen)
        items: dict[str, Item] = {}
        for nid, new in seen.items():
            item = old.get(nid)
            if item is None:
                new.since = now if new.checked else None
                items[nid] = new
                if fold(new.name) in self.expected:  # put there by the rhythm list, already in the history
                    self.expected.remove(fold(new.name))
                    continue
                self._event("added", new, now)
                log.debug("Shopping list: added %s%s", new.name, " (checked)" if new.checked else "")
                continue
            item.uid = new.uid
            if new.name != item.name:
                log.debug("Shopping list: %s renamed to %s", item.name, new.name)
                item.name = new.name
            if new.checked and not item.checked:
                item.checked, item.since = True, now
                log.debug("Shopping list: %s checked, bought if it stays checked until %s", item.name,
                          self._clock(now + self.after))
            elif item.checked and not new.checked:
                minutes = int((now - item.since).total_seconds() // 60) if item.since else 0
                item.checked, item.since, item.removing = False, None, False
                log.debug("Shopping list: %s unchecked after %d min, not bought", item.name, minutes)
            items[nid] = item
        for oid in old.keys() - seen.keys():
            item = old[oid]
            if item.removing:  # removing it after the full time was tried and seemed to fail, but went through
                self._bought(item)
            else:
                self._event("removed", item, now)
                log.debug("Shopping list: %s deleted%s, not bought", item.name,
                          " while checked" if item.checked else "")
        self.items = items
        self.expected.clear()  # what the rhythm list added shows up in the first poll after it, or never

    def _due(self, item: Item, now: datetime) -> bool:
        return item.checked and item.since is not None and now - item.since >= self.after

    def _remove_due(self, now: datetime):
        for nid, item in list(self.items.items()):
            if not self._due(item, now):
                continue
            try:
                self.ha.remove_todo(self.entity_id, item.uid or item.name)
            except Exception as e:  # noqa: BLE001
                if not item.removing:
                    log.warning("Shopping list: removing %s failed, tried again in a minute: %s", item.name, e)
                item.removing = True
                continue
            del self.items[nid]
            self._bought(item)

    def _bought(self, item: Item):
        """The item is off the list: its purchase is written, then announced. A purchase that cannot be written
        is kept and written with the next poll; the event and the rhythm list follow only then."""
        log.debug("Shopping list: %s bought (checked %s), removed from the list", item.name, self._clock(item.since))
        self.pending.append((self._entry("bought", item, item.since), item.name))
        self._flush()

    def _flush(self):
        while self.pending:
            entry, name = self.pending[0]
            try:
                self.history.add(entry)
            except OSError as e:
                if not self.write_failing:
                    self.write_failing = True
                    log.warning("Shopping history: writing a purchase failed, tried again in a minute: %s", e)
                return
            self.pending.pop(0)
            if self.write_failing:
                self.write_failing = False
                log.warning("Shopping history: purchases written again")
            self.ha.fire_event(BOUGHT_EVENT, {"list": self.entity_id, "item": name, "key": entry["key"],
                                              "time": entry["time"]})
            if self.rhythm is not None:
                self.rhythm.on_bought(name, entry["key"])

    def add_item(self, name: str, due):
        """Puts a product that is due on the shopping list (rhythm list, #70); the history says so once."""
        self.ha.todo_call("add_item", self.entity_id, item=name)
        self.expected.append(fold(name))
        self.history.add({"time": _iso(self.now()), "list": self.entity_id, "event": "added", "item": name,
                          "key": split(name)[0], "by": "rhythm", "due": due.isoformat()})
        log.debug("Shopping list: %s added, it was due on %s", name, due.isoformat())

    def _entry(self, event: str, item: Item, when: datetime) -> dict:
        key, amount = split(item.name)
        entry = {"time": _iso(when), "list": self.entity_id, "event": event, "item": item.name, "key": key}
        if amount:
            entry["amount"] = amount
        if item.uid:
            entry["uid"] = item.uid
        return entry

    def _event(self, event: str, item: Item, when: datetime) -> dict:
        entry = self._entry(event, item, when)
        try:
            self.history.add(entry)
        except OSError as e:
            log.warning("Shopping history: writing failed: %s", e)
        return entry

    # ------------------------------------------------------------------ answers
    def _local(self, t: datetime) -> datetime:
        return t.astimezone(self.tz) if self.tz else t.astimezone()

    def _clock(self, t: datetime) -> str:
        return self._local(t).strftime("%H:%M")

    def _day(self, t: datetime) -> str:
        return self._local(t).strftime("%A %Y-%m-%d")

    def _view(self) -> dict:
        out = {}
        for item in (self.items or {}).values():
            key = split(item.name)[0]
            if out.get(key) != "open":
                out[key] = "checked" if item.checked else "open"
        return out

    def on_list(self) -> dict:
        """{key: "open" | "checked"} of the list as last seen; never waits for a poll that is running."""
        return dict(self.view)

    def answer(self, mode: str, item: str = "", days=None) -> str:
        """The tool shopping_history. Dates and counts are computed here, the model only reads them out."""
        entries = self.history.entries()
        start = min((_time(e) for e in entries), default=None)
        if mode == "item":
            return self._item(item, entries, start)
        if mode == "recent":
            return self._recent(days, entries, start)
        if mode in ("due", "stock"):
            return self._forecast(mode)
        return "Error: mode must be item, recent, due or stock."

    def _forecast(self, mode: str) -> str:
        if self.rhythm is None or not self.rhythm.on:
            return "There is no rhythm list: no forecast, only item and recent."
        try:
            products = self.rhythm.forecast()
        except Exception as e:  # noqa: BLE001
            return f"Error: the rhythm list cannot be read: {e}"
        note = ("Estimated from the buying rhythm, not an inventory: say it is an estimate. Staples come first.")
        if mode == "stock":
            return json.dumps({"products": products, "note": note}, ensure_ascii=False)
        due = [p for p in products if p["status"] in ("probably_out", "running_low")]
        on_list = [p["product"] for p in products if p["status"] == "on_list"]
        return json.dumps({"due": due, "already_on_the_list": on_list, "note": note}, ensure_ascii=False)

    def _started(self, start) -> str:
        return (f"The history started on {self._day(start)}." if start else
                "The history is empty: nothing has been bought since it was switched on.")

    def _item(self, item: str, entries: list, start) -> str:
        wanted = split(item)[0]
        if not wanted:
            return "Error: item must be set."
        products: dict[str, list] = {}
        for e in entries:
            if e["event"] == "bought":
                products.setdefault(e["key"], []).append(e)
        keys = [wanted] if wanted in products else sorted(k for k in products if wanted in k or k in wanted)
        on_list = self.on_list()
        today = self._local(self.now()).date()
        out = []
        for key in keys[:MAX_MATCHES]:
            bought = sorted(products[key], key=_time, reverse=True)
            days = sorted({self._local(_time(e)).date() for e in bought}, reverse=True)
            product = {"product": bought[0]["item"], "purchases": len(days),
                       "last_bought": self._day(_time(bought[0])), "days_ago": (today - days[0]).days,
                       "dates": [d.strftime("%A %Y-%m-%d") for d in days[:MAX_DATES]]}
            if len(days) > MAX_DATES:
                product["older_dates"] = len(days) - MAX_DATES
            if key in on_list:
                product["on_list"] = on_list[key]
            out.append(product)
        if self.rhythm is not None and self.rhythm.on:
            try:
                rhythm = {split(p["product"])[0]: p for p in self.rhythm.forecast()}
            except Exception:  # noqa: BLE001  the history answers on its own
                rhythm = {}
            for product in out:
                extra = rhythm.get(split(product["product"])[0], {})
                product.update({k: extra[k] for k in ("class", "interval", "next_due", "status") if k in extra})
        result = {"item": item, "products": out, "note": self._started(start)}
        if not out:
            result["note"] = f"Not bought since the history started. {self._started(start)}"
            if wanted in on_list:
                result["on_list"] = on_list[wanted]
        elif len(keys) > MAX_MATCHES:
            result["more_products"] = len(keys) - MAX_MATCHES
        if keys and keys != [wanted]:
            result["note"] += " The name only matches in part: say which products these are."
        return json.dumps(result, ensure_ascii=False)

    def _recent(self, days, entries: list, start) -> str:
        try:
            days = max(1, min(3650, int(days))) if days else RECENT_DAYS
        except (TypeError, ValueError):
            days = RECENT_DAYS
        since = self.now() - timedelta(days=days)
        bought = sorted((e for e in entries if e["event"] == "bought" and _time(e) >= since), key=_time,
                        reverse=True)
        out = [{"date": self._day(_time(e)), "item": e["item"]} for e in bought[:MAX_RECENT]]
        result = {"days": days, "purchases": out, "note": self._started(start)}
        if len(bought) > MAX_RECENT:
            result["more"] = len(bought) - MAX_RECENT
        return json.dumps(result, ensure_ascii=False)

    def events(self, item: str = "", days=None, event: str = "") -> list[dict]:
        """GET /shopping/history: the events, optionally of one product, the last days and one kind."""
        out = self.history.entries()
        if item:
            wanted = split(item)[0]
            out = [e for e in out if e["key"] == wanted]
        if days:
            since = self.now() - timedelta(days=float(days))
            out = [e for e in out if _time(e) >= since]
        if event:
            out = [e for e in out if e["event"] == event]
        return out
