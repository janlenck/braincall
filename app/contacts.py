"""Contacts: the people BrainCall knows, in contacts.json (#93).

One contact per person: a name, several numbers each with a label ("Handy", "laut"), one mail address, the Home
Assistant user or device, rights, the language a call starts in, instructions. The file is lean: only what differs
from the default is written. It replaces the people in the setting callers; the "*" entries there (everyone else)
are rules, not people, and stay in the settings.

BrainCall reads the file at start and again whenever it changes (the contacts page, a change by voice, or by hand),
so a change never needs a restart: the live list is cfg.callers, replaced in place.

The checks here (unique name, valid numbers that belong to one contact only, mail address, rights, language) are
the same for the contacts page and for changes by voice."""
import json
import logging
import os
import re
import secrets
import threading
import unicodedata
from datetime import date
from pathlib import Path

import config

log = logging.getLogger("contacts")

FILE = "contacts.json"
RIGHTS = ("admin", "read", "calendar", "shopping")
ORIGINS = ("settings", "page", "request", "task", "call")
MAX_NAME = 80
MAX_LABEL = 30
MAX_NUMBERS = 10
MAX_TEXT = 2000
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
LANGUAGE = re.compile(r"[a-z]{2,3}|auto")
NUMBER_CHARS = re.compile(r"\+?[0-9 *#()/.\-]+")


def lean(c: dict) -> dict:
    """A contact as it is written to the file: only what differs from the default (no empty texts, no null,
    no empty label, no access guest)."""
    out = {"id": c["id"], "name": str(c.get("name") or "").strip()}
    numbers = []
    for n in config.entry_numbers(c):
        numbers.append({"number": n["number"], **({"label": n["label"]} if n["label"] else {})})
    if numbers:
        out["numbers"] = numbers
    for key in ("email", "ha_user", "ha_device"):
        value = str(c.get(key) or "").strip()
        if value:
            out[key] = value
    access = sorted(set(_access_list(c.get("access"))) - {"guest"})
    if "admin" in access:
        access = ["admin"]
    if access:
        out["access"] = access
    for key in ("instructions", "language"):
        value = str(c.get(key) or "").strip()
        if value:
            out[key] = value
    for key in ("notify", "web"):
        if isinstance(c.get(key), bool):
            out[key] = c[key]
    out["origin"] = c.get("origin") if c.get("origin") in ORIGINS else "page"
    out["added"] = str(c.get("added") or date.today().isoformat())
    return out


def _access_list(value) -> list:
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    return [w for w in re.split(r"[,;\s]+", str(value or "").strip().lower()) if w]


def _fold(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def sort_key(c: dict) -> tuple:
    """Alphabetical by name, case and accents ignored ("Änne" next to "Anna", not after "Zoe")."""
    name = unicodedata.normalize("NFKD", _fold(c.get("name")))
    return "".join(ch for ch in name if not unicodedata.combining(ch)), _fold(c.get("name")), c.get("id", "")


class Contacts:
    """contacts.json, the live list in cfg.callers, and the checks and changes shared by page and voice.

    in_use(contact) -> str: what an open task needs this contact for ("" = nothing), so it is not deleted meanwhile.
    on_merge(other, merged): called after a merge, so the memory of the contact merged away stays reachable.
    """

    def __init__(self, cfg, path=None, in_use=None, on_merge=None):
        self.cfg = cfg
        self.path = Path(path) if path else Path(cfg.log_dir) / FILE
        self.lock = threading.RLock()
        self.items: list[dict] = []
        self.mtime = None
        self.in_use = in_use or (lambda contact: "")
        # on_merge(other, merged): after other was merged into merged, so its memory moves along (main)
        self.on_merge = on_merge or (lambda other, merged: None)
        # the "*" entries of the settings: rules for everyone else, kept as they are
        self.rules = [c for c in cfg.callers or [] if config.is_rule(c)]

    # ------------------------------------------------------------------ file
    def setup(self) -> str:
        """Reads contacts.json, or creates it from the people in callers (first start). Returns a line for the log."""
        with self.lock:
            if self.path.exists():
                self._load()
                ignored = [c for c in self.cfg.get("callers_raw") or [] if isinstance(c, dict) and not config.is_rule(
                    {k: str(c.get(k) or "").strip() for k in ("number", "ha_user", "ha_device")})]
                if ignored:
                    config.WARNINGS.append(
                        f"callers in the settings still lists {len(ignored)} people; they are ignored, the contacts "
                        f"are in {self.path}. Remove them from callers (keep only the \"*\" entries)")
                return f"{len(self.items)} in {self.path}"
            moved = self._move_from_callers()
            return f"{len(self.items)} in {self.path}" + (f", {moved} moved from callers" if moved else "")

    def _move_from_callers(self) -> int:
        raw = [c for c in self.cfg.get("callers_raw") or [] if isinstance(c, dict)]
        people = [c for c in raw if not config.is_rule({k: str(c.get(k) or "").strip()
                                                        for k in ("number", "ha_user", "ha_device")})]
        rules = [c for c in raw if c not in people]
        today = date.today().isoformat()
        taken: set = set()
        items = []
        for c in people:
            entry = {k: v for k, v in c.items() if k not in ("number", "numbers")}
            entry["numbers"] = config.entry_numbers(c)
            name = str(c.get("name") or "").strip()
            if _fold(name) in taken:
                n = 2
                while _fold(f"{name} ({n})") in taken:
                    n += 1
                config.WARNINGS.append(f"Contacts: two entries named {name!r} in callers, the second is now "
                                       f"\"{name} ({n})\"; rename or merge them on the contacts page")
                name = f"{name} ({n})"
            if not name:
                first = entry["numbers"][0]["number"] if entry["numbers"] else ""
                name = first or str(c.get("ha_user") or c.get("ha_device") or c.get("email") or "?")
            taken.add(_fold(name))
            entry.update({"id": self._new_id(items), "name": name, "origin": "settings", "added": today})
            if str(entry.get("language") or "").strip().lower() == str(self.cfg.language or "").strip().lower():
                entry.pop("language", None)  # the system language is the default
            items.append(lean(entry))
        self.items = items
        self._write()
        self._apply()
        if people:
            log.info("Contacts: %d entries moved from callers to %s", len(people), self.path)
            if self.cfg.supervisor_token:
                try:
                    config.save_option(self.cfg, "callers", rules)
                except Exception as e:  # noqa: BLE001  the contacts are safe in the file; the settings show stale entries
                    config.WARNINGS.append(f"Contacts: removing the moved people from callers in the settings "
                                           f"failed ({e}); they are ignored, remove them by hand")
            else:
                config.WARNINGS.append(f"Contacts: the people from callers are now in {self.path}; remove them from "
                                       f"callers in {config.CONFIG_FILE[0] or 'config.json'} (keep only the \"*\" "
                                       "entries), they are ignored from now on")
        return len(people)

    def _load(self):
        try:
            text = self.path.read_text(encoding="utf-8-sig")
            if not text.strip():
                raise ValueError("the file is empty")
            data = json.loads(config._strip_jsonc(text))
            raw = data.get("contacts") if isinstance(data, dict) else data
            if not isinstance(raw, list):
                raise ValueError('no list "contacts" in it')
        except (OSError, ValueError) as e:  # also a file caught half written by hand: the last good list stays
            log.error("Contacts: %s could not be read (%s); the contacts stay as they were", self.path, e)
            self.mtime = self._stat()
            return
        items, seen = [], set()
        for c in raw:
            if not isinstance(c, dict):
                continue
            c = dict(c)
            if not str(c.get("id") or "").strip() or c["id"] in seen:
                c["id"] = self._new_id(items)
            seen.add(c["id"])
            items.append(lean(c))
        self.items = items
        self.mtime = self._stat()
        self._apply()

    def _stat(self):
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"contacts": self.items}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, self.path)
        self.mtime = self._stat()

    def _apply(self):
        """The live list: rules first is not needed, find_caller prefers a person over "*" anyway."""
        entries = config._callers(self.items) + self.rules
        if isinstance(self.cfg.get("callers"), list):
            self.cfg["callers"][:] = entries
        else:
            self.cfg["callers"] = entries

    def refresh(self) -> bool:
        """Reads the file again if it changed since (a change by hand). True if it did."""
        with self.lock:
            now = self._stat()
            if now is None or now == self.mtime:
                return False
            self._load()
            log.info("Contacts: %s changed, %d contacts read", self.path, len(self.items))
            return True

    def _new_id(self, items=None) -> str:
        used = {c["id"] for c in (items if items is not None else self.items)}
        while True:
            new = secrets.token_hex(2)
            if new not in used:
                return new

    # ------------------------------------------------------------------ lookup
    def all(self) -> list[dict]:
        """All contacts, alphabetical by name (the contacts page and the list read out by voice)."""
        with self.lock:
            return sorted(json.loads(json.dumps(self.items)), key=sort_key)

    def get(self, cid: str) -> dict | None:
        with self.lock:
            return next((json.loads(json.dumps(c)) for c in self.items if c["id"] == cid), None)

    def by_name(self, name: str) -> dict | None:
        key = _fold(name)
        with self.lock:
            return next((json.loads(json.dumps(c)) for c in self.items if key and _fold(c["name"]) == key), None)

    def by_number(self, number: str) -> dict | None:
        with self.lock:
            return next((json.loads(json.dumps(c)) for c in self.items
                         if config.has_number(c, number, self.cfg.country_code)), None)

    def by_email(self, email: str) -> dict | None:
        key = _fold(email)
        with self.lock:
            return next((json.loads(json.dumps(c)) for c in self.items if key and _fold(c.get("email")) == key), None)

    # ------------------------------------------------------------------ checks
    def check(self, c: dict) -> list[dict]:
        """What is wrong with a contact before it is stored: [{"field", "code", …}], empty if nothing.
        Codes: name_missing, name_taken, name_long, number_invalid, number_taken (with "name"), number_twice,
        label_long, too_many_numbers, email_invalid, email_taken (with "name"), access_invalid, language_invalid,
        text_long, empty."""
        errors = []
        cid = c.get("id")
        others = [o for o in self.items if o["id"] != cid]
        name = " ".join(str(c.get("name") or "").split())
        if not name:
            errors.append({"field": "name", "code": "name_missing"})
        elif len(name) > MAX_NAME:
            errors.append({"field": "name", "code": "name_long"})
        else:
            twin = next((o for o in others if _fold(o["name"]) == _fold(name)), None)
            if twin:
                errors.append({"field": "name", "code": "name_taken", "name": twin["name"]})
        raw = c.get("numbers") if isinstance(c.get("numbers"), list) else []
        numbers = [n if isinstance(n, dict) else {"number": n} for n in raw]
        numbers = [n for n in numbers if str(n.get("number") or "").strip()]
        if len(numbers) > MAX_NUMBERS:
            errors.append({"field": "numbers", "code": "too_many_numbers"})
        keys = []
        for i, n in enumerate(numbers):
            number = str(n.get("number") or "").strip()
            key = config.norm_number(number, self.cfg.country_code)
            if not NUMBER_CHARS.fullmatch(number) or len(re.sub(r"[^\d*#]", "", key)) < 3:
                errors.append({"field": "numbers", "index": i, "code": "number_invalid"})
            elif key in keys:
                errors.append({"field": "numbers", "index": i, "code": "number_twice"})
            else:
                owner = next((o for o in others if config.has_number(o, number, self.cfg.country_code)), None)
                if owner:
                    errors.append({"field": "numbers", "index": i, "code": "number_taken", "name": owner["name"]})
            keys.append(key)
            if len(str(n.get("label") or "").strip()) > MAX_LABEL:
                errors.append({"field": "numbers", "index": i, "code": "label_long"})
        email = str(c.get("email") or "").strip()
        if email:
            if not EMAIL.fullmatch(email):
                errors.append({"field": "email", "code": "email_invalid"})
            else:
                owner = next((o for o in others if _fold(o.get("email")) == _fold(email)), None)
                if owner:
                    errors.append({"field": "email", "code": "email_taken", "name": owner["name"]})
        if set(_access_list(c.get("access"))) - set(RIGHTS) - {"guest"}:
            errors.append({"field": "access", "code": "access_invalid"})
        language = str(c.get("language") or "").strip().lower()
        if language and not LANGUAGE.fullmatch(language):
            errors.append({"field": "language", "code": "language_invalid"})
        if len(str(c.get("instructions") or "")) > MAX_TEXT:
            errors.append({"field": "instructions", "code": "text_long"})
        if not (numbers or email or str(c.get("ha_user") or "").strip() or str(c.get("ha_device") or "").strip()):
            errors.append({"field": "numbers", "code": "empty"})
        return errors

    # ------------------------------------------------------------------ changes
    def _clean(self, data: dict, old: dict | None, origin: str) -> dict:
        c = {k: data.get(k) for k in ("name", "numbers", "email", "ha_user", "ha_device", "access",
                                       "instructions", "language", "notify", "web")}
        c["name"] = " ".join(str(c["name"] or "").split())
        c["numbers"] = [{"number": str((n if isinstance(n, dict) else {"number": n}).get("number") or "").strip(),
                         "label": str((n if isinstance(n, dict) else {}).get("label") or "").strip()}
                        for n in (c["numbers"] if isinstance(c["numbers"], list) else [])]
        c["numbers"] = [n for n in c["numbers"] if n["number"]]
        c["language"] = str(c["language"] or "").strip().lower()
        if c["language"] == str(self.cfg.language or "").strip().lower():
            c["language"] = ""  # the system language is the default
        for key in ("notify", "web"):
            if not isinstance(c[key], bool):
                c[key] = None
        c["id"] = old["id"] if old else self._new_id()
        c["origin"] = old["origin"] if old else origin
        c["added"] = old["added"] if old else date.today().isoformat()
        return c

    def preview(self, data: dict, cid: str = "", origin: str = "page") -> tuple[dict | None, list]:
        """The contact as save() would store it, without storing: (contact, []) or (None, errors)."""
        with self.lock:
            old = next((c for c in self.items if c["id"] == cid), None) if cid else None
            if cid and old is None:
                return None, [{"field": "id", "code": "not_found"}]
            c = self._clean(data, old, origin)
            errors = self.check(c)
            return (None, errors) if errors else (lean(c), [])

    def save(self, data: dict, cid: str = "", origin: str = "page") -> tuple[dict | None, list]:
        """Creates (no cid) or changes a contact. Returns (the stored contact, []) or (None, errors)."""
        with self.lock:
            stored, errors = self.preview(data, cid, origin)
            if errors:
                return None, errors
            old = cid and next((c for c in self.items if c["id"] == cid), None)
            self.items = [stored if o["id"] == stored["id"] else o for o in self.items] if old \
                else self.items + [stored]
            self._write()
            self._apply()
            log.info("Contacts: %s %s", "changed" if old else "added", stored["name"])
            return json.loads(json.dumps(stored)), []

    def delete(self, cid: str) -> str:
        """Deletes a contact. "" if done, else why not ("not_found", or what an open task needs it for)."""
        with self.lock:
            old = next((c for c in self.items if c["id"] == cid), None)
            if old is None:
                return "not_found"
            busy = self.in_use(old)
            if busy:
                return busy
            self.items = [c for c in self.items if c["id"] != cid]
            self._write()
            self._apply()
            log.info("Contacts: deleted %s", old["name"])
            return ""

    def merge(self, into: str, other: str) -> tuple[dict | None, str]:
        """Merges contact other into contact into: numbers joined (into's first), mail and other fields taken from
        other where into has none. Returns (the merged contact, "") or (None, why not)."""
        with self.lock:
            a = next((c for c in self.items if c["id"] == into), None)
            b = next((c for c in self.items if c["id"] == other), None)
            if a is None or b is None or a is b:
                return None, "not_found"
            busy = self.in_use(b)
            if busy:
                return None, busy
            merged = json.loads(json.dumps(a))
            known = [config.norm_number(n["number"], self.cfg.country_code) for n in config.entry_numbers(a)]
            # "Jan laut" merged into "Jan" (or "Jan Lenck"): its numbers without a label get "laut"
            wa, wb = a["name"].split(), b["name"].split()
            same = 0
            while same < min(len(wa), len(wb)) and wa[same].casefold() == wb[same].casefold():
                same += 1
            rest = " ".join(wb[same:]).strip(" ()-,") if same else ""
            merged["numbers"] = config.entry_numbers(a) + [
                {"number": n["number"], "label": n["label"] or rest[:MAX_LABEL]} for n in config.entry_numbers(b)
                if config.norm_number(n["number"], self.cfg.country_code) not in known]
            for key in ("email", "ha_user", "ha_device", "instructions", "language"):
                if not merged.get(key) and b.get(key):
                    merged[key] = b[key]
            rest = [c for c in self.items if c["id"] != other]
            self.items = [lean(merged) if c["id"] == into else c for c in rest]
            self._write()
            self._apply()
            log.info("Contacts: merged %s into %s", b["name"], a["name"])
            result = self.get(into)
        try:
            self.on_merge(b, result)
        except Exception:  # noqa: BLE001  the contacts are merged; only the memory of the other stays where it was
            log.exception("Contacts: moving the memory of %s failed", b["name"])
        return result, ""

    def set_language(self, cid: str, language: str) -> bool:
        """The language a contact spoke last (after a call): stored without asking, the system language as
        left out. True if it changed."""
        language = str(language or "").strip().lower()
        if language == str(self.cfg.language or "").strip().lower():
            language = ""
        if language and not LANGUAGE.fullmatch(language):
            return False
        with self.lock:
            c = next((c for c in self.items if c["id"] == cid), None)
            if c is None or c.get("language", "") == language:
                return False
            before = c.get("language") or self.cfg.language or "?"
            changed = dict(c)
            changed.pop("language", None)
            if language:
                changed["language"] = language
            self.items = [lean(changed) if o["id"] == cid else o for o in self.items]
            self._write()
            self._apply()
            log.info("Contact %s: language %s → %s", c["name"], before, language or self.cfg.language)
            return True


def describe(c: dict, system_language: str = "") -> str:
    """A contact in words for Claude (contact_edit, the read-back)."""
    numbers = ", ".join(n["number"] + (f" ({n['label']})" if n["label"] else "") for n in config.entry_numbers(c))
    access = [a for a in _access_list(c.get("access")) if a != "guest"]
    parts = [f"{c.get('name')}: numbers {numbers or 'none'}"]
    if c.get("email"):
        parts.append(f"mail {c['email']}")
    default = f"{system_language} (system language)" if system_language else "the system language"
    parts.append(f"language {c.get('language') or default}")
    parts.append(f"rights {', '.join(access) or 'guest'}")
    if c.get("ha_user"):
        parts.append(f"Home Assistant user {c['ha_user']}")
    if c.get("instructions"):
        parts.append(f"instructions: {c['instructions']}")
    return "; ".join(parts)


def problems(errors: list, numbers: list | None = None) -> str:
    """The errors of check() in words for Claude."""
    out = []
    for e in errors:
        n = (numbers or [])[e["index"]]["number"] if e.get("index") is not None and numbers and \
            e["index"] < len(numbers) else ""
        out.append({
            "name_missing": "the name is missing",
            "name_taken": f"another contact is already called {e.get('name')}: ask for a name that tells them "
                          "apart, e.g. with the last name",
            "name_long": "the name is too long",
            "number_invalid": f"{n or 'a number'} is not a valid phone number",
            "number_twice": f"{n or 'a number'} is given twice",
            "number_taken": f"{n or 'a number'} already belongs to {e.get('name')}",
            "label_long": "a label is too long",
            "too_many_numbers": "too many numbers",
            "email_invalid": "the mail address is not valid",
            "email_taken": f"the mail address already belongs to {e.get('name')}",
            "access_invalid": "unknown rights",
            "language_invalid": "the language must be a two-letter code such as en or it",
            "text_long": "the instructions are too long",
            "empty": "a contact needs a phone number, a mail address or a Home Assistant user",
            "not_found": "the contact does not exist (any more)",
        }.get(e["code"], e["code"]))
    return "; ".join(out)
