"""Rhythm list (#70): the products that are bought again and again, as a to-do list in Home Assistant.

One entry per product: "Milch (7 days)", the due date is the next purchase expected, the description is written by
BrainCall ("staple · learned: 7 days · last: 2026-11-14 · 12 purchases"). The list is the truth for what is recurring
and at which interval; the history (shopping.py) only feeds it. Everything BrainCall writes into it is English.

- After every purchase the product's entry is there and active (created, unpaused, due date moved on), if the
  product qualifies: a staple after 2 days with a purchase, a consumable after 3, a one-off thing never. Claude says
  once per product which it is (shopping-products.json). That is the self-repair: an entry checked off or deleted by
  mistake comes back with the next purchase.
- An interval the user changed in the name stays; BrainCall writes what it learns into the description only.
- Every few minutes: intervals typed in German are rewritten in English, a removed interval is filled in again, and
  what is due is put on the shopping list, once per due date."""
import json
import logging
import os
import re
import statistics
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import shopping as shop

log = logging.getLogger("shopping")

NEVER = 0                  # interval "(never)": recurring, but never due and never added
SYNC_SECONDS = 300         # how often the rhythm list is read without a purchase
LOW = 0.8                  # running low from this part of the interval on
NEEDED = {"staple": 2, "consumable": 3}   # days with a purchase before a product gets its entry
CLASSES = ("staple", "consumable", "one_off")
DISPLAY_NAME = "Recurring purchases"
CLASSIFY_BATCH = 40

_UNIT_DAYS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30,
              "tag": 1, "tage": 1, "tagen": 1, "woche": 7, "wochen": 7, "monat": 30, "monate": 30, "monaten": 30}
_WORD_DAYS = {"daily": 1, "weekly": 7, "monthly": 30, "never": NEVER,
              "täglich": 1, "wöchentlich": 7, "monatlich": 30, "nie": NEVER}
_GERMAN = {"tag", "tage", "tagen", "woche", "wochen", "monat", "monate", "monaten",
           "täglich", "wöchentlich", "monatlich", "nie"}
_COUNT = re.compile(r"^(\d+)\s*([a-zäöü]+)\.?$")
_NAME = re.compile(r"^(.*\S)\s*\(([^()]*)\)\s*$")
_INTERVALISH = re.compile(r"\d|\b(?:" + "|".join(sorted(set(_UNIT_DAYS) | set(_WORD_DAYS))) + r")\b", re.I)
_LEARNED = re.compile(r"\b(learned|estimated):\s*(\d+)\s*days?\b")
_CLASS = re.compile(r"^\s*(staple|consumable|one_off)\b")


def parse_interval(text: str) -> tuple[int | None, bool]:
    """(days, german) of what is in the brackets: "7 days", "2 Wochen", "weekly", "nie" …
    days is NEVER for never and None if it cannot be read."""
    t = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if t in _WORD_DAYS:
        return _WORD_DAYS[t], t in _GERMAN
    m = _COUNT.match(t)
    if m and m.group(2) in _UNIT_DAYS and int(m.group(1)) > 0:
        return int(m.group(1)) * _UNIT_DAYS[m.group(2)], m.group(2) in _GERMAN
    return None, False


def interval_text(days: int) -> str:
    return "never" if days == NEVER else f"{days} day" if days == 1 else f"{days} days"


def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


@dataclass
class Entry:
    uid: str
    summary: str
    product: str              # the name without the interval
    key: str
    interval: int | None      # days, NEVER, or None when the name has no interval
    readable: bool = True     # False: brackets that look like an interval but cannot be read
    german: bool = False      # the interval is written in German (rewritten in English)
    paused: bool = False      # checked off
    due: date | None = None
    learned: int | None = None  # what BrainCall wrote into the description last time
    cls: str = ""

    @property
    def ref(self) -> str:
        return self.uid or self.summary


def parse_entry(item: dict) -> Entry:
    summary = str(item.get("summary") or "").strip()
    product, interval, readable, german = summary, None, True, False
    m = _NAME.match(summary)
    if m:
        value, in_german = parse_interval(m.group(2))
        if value is not None:
            product, interval, german = m.group(1).strip(), value, in_german
        elif _INTERVALISH.search(m.group(2)):
            product, readable = m.group(1).strip(), False
    description = str(item.get("description") or "")
    learned, cls = _LEARNED.search(description), _CLASS.match(description)
    return Entry(uid=str(item.get("uid") or ""), summary=summary, product=product, key=shop.split(product)[0],
                 interval=interval, readable=readable, german=german, paused=item.get("status") == "completed",
                 due=_date(item.get("due")), learned=int(learned.group(2)) if learned else None,
                 cls=cls.group(1) if cls else "")


@dataclass
class Info:
    """What the history says about one product."""
    dates: list = field(default_factory=list)   # local days with a purchase, oldest first
    cls: str = "consumable"
    learned: int | None = None
    estimated: bool = False

    @property
    def last(self) -> date | None:
        return self.dates[-1] if self.dates else None

    @property
    def qualifies(self) -> bool:
        return self.cls in NEEDED and len(self.dates) >= NEEDED[self.cls] and bool(self.learned)

    def describe(self) -> str:
        out = [self.cls]
        if self.learned:
            out.append(f"{'estimated' if self.estimated else 'learned'}: {interval_text(self.learned)}")
        if self.last:
            out.append(f"last: {self.last.isoformat()}")
        out.append(f"{len(self.dates)} purchase{'' if len(self.dates) == 1 else 's'}")
        return " · ".join(out)


class Products:
    """shopping-products.json: the class Claude gave each product (key -> class, typical_days, time)."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            self.data = {}
        except (OSError, ValueError) as e:
            log.warning("Reading %s failed, products are classified again: %s", self.path, e)
            self.data = {}

    def get(self, key: str) -> dict | None:
        p = self.data.get(key)
        return p if isinstance(p, dict) and p.get("class") in CLASSES else None

    def put(self, items: dict, now: datetime):
        with self.lock:
            for key, (cls, typical) in items.items():
                self.data[key] = {"class": cls, "typical_days": typical, "time": now.isoformat(timespec="seconds")}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)


class Rhythm:
    def __init__(self, cfg, ha, shopping, products_path, classify=None):
        self.cfg = cfg
        self.ha = ha
        self.shopping = shopping
        self.entity_id = cfg.shopping_rhythm_list
        self.products = Products(products_path)
        self.classify = classify  # names -> {name: {"class", "typical_days"}}; None = everything is a consumable
        self.on = False            # the list exists (set by ensure)
        self.last_sync: datetime | None = None
        self.failing = False
        self.warned: set = set()   # entries with an interval that cannot be read, warned once each
        shopping.rhythm = self

    # ------------------------------------------------------------------ the list
    def ensure(self):
        """At the start: the rhythm list exists, or it is created and filled from the history. Raises if Home
        Assistant cannot be asked (tried again with the next poll)."""
        if self.ha.entity_exists(self.entity_id):
            self.on = True
            return
        if not shop.create_list(self.ha, self.entity_id, "rhythm list", DISPLAY_NAME):
            return
        self.on = True
        self.fill()

    def _read(self) -> list[Entry]:
        state = self.ha.entity_state(self.entity_id)
        if state in ("unavailable", "unknown", "None", ""):
            raise ValueError(f"the rhythm list is {state or 'empty'}")
        return [parse_entry(i) for i in self.ha.todo_raw(self.entity_id)]

    def _update(self, entry: Entry, **data):
        self.ha.todo_call("update_item", self.entity_id, item=entry.ref, **data)
        log.debug("Rhythm list: %s updated %s", entry.summary, data)

    def _failed(self, e):
        if self.failing:
            log.log(shop.TRACE, "Rhythm list %s still not readable: %s", self.entity_id, e)
            return
        self.failing = True
        log.warning("Rhythm list %s cannot be read: %s. Nothing is changed or added until it works again",
                    self.entity_id, e)

    def _ok(self):
        if self.failing:
            self.failing = False
            log.warning("Rhythm list %s can be read again", self.entity_id)

    # ------------------------------------------------------------------ what the history says
    def _today(self) -> date:
        return self.shopping._local(self.shopping.now()).date()

    def info(self, key: str, entries: list | None = None) -> Info:
        entries = self.shopping.history.entries() if entries is None else entries
        days = sorted({self.shopping._local(shop._time(e)).date() for e in entries
                       if e["event"] == "bought" and e["key"] == key})
        product = self.products.get(key) or {}
        info = Info(dates=days, cls=product.get("class") or "consumable")
        typical = product.get("typical_days") or 0
        if len(days) >= 2:
            gaps = [(b - a).days for a, b in zip(days, days[1:], strict=False)]
            if len(days) == 2 and typical and not typical / 2 <= gaps[0] <= typical * 2:
                info.learned, info.estimated = int(typical), True  # one gap that does not fit, e.g. a holiday
            else:
                info.learned = max(1, round(statistics.median(gaps)))
        return info

    def _classify(self, products: dict):
        """Asks Claude once about every product (key -> name) not classified yet. Failing: they count as
        consumables for now and are asked about again with their next purchase."""
        missing = {k: n for k, n in products.items() if self.products.get(k) is None}
        if not missing or self.classify is None:
            return
        found = {}
        items = list(missing.items())
        for start in range(0, len(items), CLASSIFY_BATCH):
            batch = dict(items[start:start + CLASSIFY_BATCH])
            try:
                result = self.classify(list(batch.values()))
            except Exception as e:  # noqa: BLE001
                log.warning("Rhythm list: classifying %s failed, counted as consumables for now: %s",
                            ", ".join(batch.values()), e)
                continue
            for key, name in batch.items():
                r = result.get(name) or {}
                if r.get("class") in CLASSES:
                    typical = r.get("typical_days")
                    found[key] = (r["class"], int(typical) if isinstance(typical, (int, float)) and typical > 0
                                  else None)
                    log.debug("Rhythm list: %s is a %s (typically every %s days)", name, r["class"],
                              found[key][1] or "-")
        if found:
            self.products.put(found, self.shopping.now())

    # ------------------------------------------------------------------ after a purchase
    def on_bought(self, name: str, key: str):
        """A purchase was recorded: the product's entry is there and active afterwards, if it qualifies."""
        if not self.on:
            return
        try:
            self._classify({key: shop.parts(name)[0]})
            self._apply(key, shop.parts(name)[0], self._read())
            self._ok()
        except Exception as e:  # noqa: BLE001
            self._failed(e)

    def fill(self):
        """A rhythm list that was just created: an entry for every product of the history that qualifies."""
        names = {}
        for e in self.shopping.history.entries():
            if e["event"] == "bought":
                names[e["key"]] = shop.parts(e["item"])[0]
        self._classify(names)
        entries = self._read()
        for key, name in names.items():
            self._apply(key, name, entries)
        log.info("Rhythm list %s filled from the history: %d products", self.entity_id,
                 sum(1 for k in names if self.info(k).qualifies))

    def _apply(self, key: str, name: str, entries: list[Entry]):
        info = self.info(key)
        entry = next((e for e in entries if e.key == key), None)
        if entry is None:
            if not info.qualifies:
                return
            summary = f"{name} ({interval_text(info.learned)})"
            self.ha.todo_call("add_item", self.entity_id, item=summary,
                              due_date=(info.last + timedelta(days=info.learned)).isoformat(),
                              description=info.describe())
            self.shopping.history.add({"time": shop._iso(self.shopping.now()), "list": self.entity_id,
                                       "event": "listed", "item": summary, "key": key})
            log.debug("Rhythm list: %s added", summary)
            return
        data = {"description": info.describe()}
        if entry.interval == NEVER:  # stays as it is, only the description goes on
            return self._update(entry, **data)
        if entry.paused:
            data["status"] = "needs_action"
        user_set = entry.interval is not None and entry.interval != entry.learned
        days = entry.interval if user_set else (info.learned or entry.interval)
        if days:
            if not user_set and (days != entry.interval or entry.german):
                data["rename"] = f"{entry.product} ({interval_text(days)})"
            data["due_date"] = (info.last + timedelta(days=days)).isoformat()
        self._update(entry, **data)

    # ------------------------------------------------------------------ every few minutes
    def due_for_sync(self, now: datetime) -> bool:
        return self.on and (self.last_sync is None or now - self.last_sync >= timedelta(seconds=SYNC_SECONDS))

    def sync(self, now: datetime):
        """Repairs what the user typed (German interval, interval removed, no due date) and adds what is due."""
        self.last_sync = now
        try:
            entries = self._read()
            self._ok()
        except Exception as e:  # noqa: BLE001
            self._failed(e)
            return
        log.log(shop.TRACE, "Rhythm list %s: %d entries", self.entity_id, len(entries))
        today = self._today()
        for e in entries:
            if not e.readable:
                if e.ref not in self.warned:
                    self.warned.add(e.ref)
                    log.warning("Rhythm list: the interval of %r cannot be read, the entry is left alone. "
                                "Write e.g. (7 days), (2 weeks), (monthly) or (never)", e.summary)
                continue
            data = {}
            if e.german and e.interval is not None:
                data["rename"] = f"{e.product} ({interval_text(e.interval)})"
            elif e.interval is None and e.learned:
                e.interval = e.learned
                data["rename"] = f"{e.product} ({interval_text(e.learned)})"
            if e.interval and e.due is None:
                e.due = today + timedelta(days=e.interval)
                data["due_date"] = e.due.isoformat()
            if data:
                try:
                    self._update(e, **data)
                except Exception as err:  # noqa: BLE001
                    log.warning("Rhythm list: updating %s failed: %s", e.summary, err)
        self._add_due(entries, today)

    def _add_due(self, entries: list[Entry], today: date):
        due = [e for e in entries if e.readable and not e.paused and e.interval and e.due and e.due <= today]
        if not due:
            return
        try:
            on_list = {shop.split(i.get("summary"))[0] for i in self.ha.todo_raw(self.shopping.entity_id)}
        except Exception as e:  # noqa: BLE001
            log.log(shop.TRACE, "Rhythm list: shopping list not readable, nothing added: %s", e)
            return
        done = {(e["key"], e.get("due")) for e in self.shopping.history.entries()
                if e["event"] == "added" and e.get("by") == "rhythm"}
        for e in due:
            if e.key in on_list or (e.key, e.due.isoformat()) in done:
                continue
            try:
                self.shopping.add_item(e.product, e.due)
            except Exception as err:  # noqa: BLE001
                log.warning("Rhythm list: adding %s to the shopping list failed: %s", e.product, err)

    # ------------------------------------------------------------------ answers
    def forecast(self) -> list[dict]:
        """Every product of the rhythm list with interval, due date and estimated stock; staples first."""
        entries = self._read()
        history = self.shopping.history.entries()
        on_list = self.shopping.on_list()
        today = self._today()
        out = []
        for e in entries:
            if not e.readable:
                continue
            info = self.info(e.key, history)
            cls = (self.products.get(e.key) or {}).get("class") or e.cls or ""
            item = {"product": e.product, "class": cls or "unknown",
                    "interval": interval_text(e.interval) if e.interval is not None else "none",
                    "status": self._status(e, on_list, today)}
            if e.due and e.interval != NEVER:
                item["next_due"] = e.due.strftime("%A %Y-%m-%d")
            if info.last:
                item["last_bought"] = info.last.strftime("%A %Y-%m-%d")
                item["purchases"] = len(info.dates)
            out.append(item)
        out.sort(key=lambda i: (i["class"] != "staple", i.get("next_due", "9")[-10:]))
        return out

    @staticmethod
    def _status(e: Entry, on_list: dict, today: date) -> str:
        if e.paused:
            return "paused"
        if e.interval == NEVER:
            return "never"
        if e.key in on_list:
            return "on_list"
        if not e.due or not e.interval:
            return "unknown"
        if e.due <= today:
            return "probably_out"
        return "running_low" if 1 - (e.due - today).days / e.interval >= LOW else "in_stock"

    # ------------------------------------------------------------------ changes asked for in a call
    def edit(self, action: str, item: str, interval=None, due: str = "") -> str:
        """shopping_rhythm: interval, pause, resume, remove or due of one entry (access admin). Takes no lock:
        Home Assistant keeps the changes in order, and a call must not wait for a poll that is running."""
        entries = self._read()
        wanted = shop.split(item)[0]
        matches = [e for e in entries if e.key == wanted] or [e for e in entries if wanted and wanted in e.key]
        if len(matches) != 1:
            names = ", ".join(e.summary for e in (matches or entries)) or "none"
            return (f"Not clear which entry: {names}." if matches else
                    f"{item} is not on the rhythm list. Entries: {names}.")
        e = matches[0]
        if action == "interval":
            raw = str(interval if interval is not None else "").strip().lower()
            value = (NEVER if raw in ("0", "never", "nie") else int(raw) if raw.isdigit()
                     else parse_interval(raw)[0])
            if value is None:
                return "Error: interval must be a number of days or never."
            data = {"rename": f"{e.product} ({interval_text(value)})"}
            last = self.info(e.key).last
            if value != NEVER and last:
                data["due_date"] = (last + timedelta(days=value)).isoformat()
            self._update(e, **data)
        elif action in ("pause", "resume"):
            self._update(e, status="completed" if action == "pause" else "needs_action")
        elif action == "remove":
            self.ha.todo_call("remove_item", self.entity_id, item=e.ref)
            log.debug("Rhythm list: %s removed", e.summary)
        elif action == "due":
            day = _date(due)
            if day is None:
                return "Error: due must be a date like 2026-12-01."
            self._update(e, due_date=day.isoformat())
        else:
            return "Error: action must be interval, pause, resume, remove or due."
        log.warning("SHOPPING rhythm %s %s %s", action, e.summary, interval if action == "interval" else due)
        entry = next((f for f in self.forecast() if shop.split(f["product"])[0] == e.key), None)
        return "Done. " + json.dumps(entry or {"removed": e.product}, ensure_ascii=False)

