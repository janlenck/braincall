"""Home Assistant access via the REST API (inside an add-on via the Supervisor proxy)."""
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("ha")

# Spoken German search terms -> typical (often English) parts of entity IDs, e.g. ebusd/Vaillant, Fronius, evcc.
# The keys are German on purpose: they match what callers say.
SYNONYMS = {
    "warmwasser": ["hwc", "dhw", "hot_water", "hotwater", "water_heater", "storage", "speicher", "tank"],
    "temperatur": ["temp", "°c", "temperature"],
    "aussen": ["outside", "outdoor", "aussen", "außen"],
    "außen": ["outside", "outdoor", "aussen"],
    "vorlauf": ["flow", "vorlauf"],
    "ruecklauf": ["return", "rücklauf"],
    "rücklauf": ["return", "ruecklauf"],
    "heizung": ["heat", "hmu", "ctlv", "heating", "wp", "waermepumpe"],
    "wärmepumpe": ["heat_pump", "heatpump", "hmu", "ebusd", "vaillant", "arotherm"],
    "waermepumpe": ["heat_pump", "heatpump", "hmu", "ebusd", "vaillant", "arotherm"],
    "pv": ["solar", "photovoltaic", "fronius", "pv"],
    "solar": ["pv", "fronius", "photovoltaic"],
    "leistung": ["power", "kw"],
    "verbrauch": ["consumption", "load", "energy", "kwh"],
    "energie": ["energy", "kwh"],
    "batterie": ["battery", "akku", "soc"],
    "akku": ["battery", "soc"],
    "netz": ["grid", "netz"],
    "einspeisung": ["grid_export", "export", "feed"],
    "bezug": ["grid_import", "import"],
    "auto": ["evcc", "car", "vehicle", "charge", "wallbox"],
    "wallbox": ["evcc", "charge", "wallbox", "loadpoint"],
    "garage": ["garage", "cover"],
    "kalender": ["calendar."],
    "termin": ["calendar."],
    "feiertag": ["feiertag", "holiday"],
    "einkauf": ["shopping", "einkauf", "todo."],
    "liste": ["todo.", "list"],
    "aufgabe": ["todo.", "task"],
    "tor": ["cover", "door", "gate"],
    "licht": ["light", "lamp"],
    "lampe": ["light", "lamp"],
    "feuchtigkeit": ["humidity"],
    "zimmer": ["room"],
}

WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _wd(d) -> str:
    return WEEKDAY[d.weekday()]


def fold(t: str) -> str:
    """Fold umlauts the way entity IDs do: Mölln -> molln."""
    return t.lower().replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")


def _local_time(iso: str) -> str:
    """UTC timestamp from HA -> local time plus age, so the model never reads out UTC."""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        mins = int((datetime.now(timezone.utc) - t).total_seconds() // 60)
        local = t.astimezone()
        when = local.strftime("%H:%M") if mins < 1440 else local.strftime("%Y-%m-%d %H:%M")
        return f"{when} local time ({mins} minutes ago)" if mins < 180 else f"{when} local time"
    except Exception:  # noqa: BLE001
        return iso


class HA:
    def __init__(self, cfg):
        self.cfg = cfg
        self.verify = True
        if cfg.supervisor_token:
            self.base = "http://supervisor/core/api"
            token = cfg.supervisor_token
        elif cfg.ha_url and cfg.ha_token:
            self.base = cfg.ha_url.rstrip("/") + "/api"
            token = cfg.ha_token
            v = cfg.ha_verify_ssl
            self.verify = v if isinstance(v, str) and v else bool(v)
        else:
            self.base = None
            token = ""
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {token}"
        self._cache = (0.0, [])

    @property
    def enabled(self) -> bool:
        return self.base is not None

    def _get(self, path):
        r = self.http.get(self.base + path, timeout=10, verify=self.verify)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _fmt(s: dict) -> dict:
        a = s.get("attributes", {})
        return {
            "entity_id": s["entity_id"],
            "name": a.get("friendly_name"),
            "state": s.get("state"),
            "unit": a.get("unit_of_measurement"),
            "changed": _local_time(s.get("last_changed", "")),
        }

    def todo_items(self, entity_id: str) -> list:
        """Items of a to-do/shopping list (the entity state itself only holds the count)."""
        r = self.http.post(f"{self.base}/services/todo/get_items?return_response",
                           json={"entity_id": entity_id}, timeout=10, verify=self.verify)
        r.raise_for_status()
        data = r.json().get("service_response", {}).get(entity_id, {})
        return [{"item": i.get("summary"),
                 "status": "done" if i.get("status") == "completed" else "open",
                 **({"note": i["description"]} if i.get("description") else {}),
                 **({"due": i["due"]} if i.get("due") else {})}
                for i in data.get("items", [])]

    def get_state(self, entity_id: str) -> str:
        try:
            st = self._fmt(self._get(f"/states/{entity_id}"))
            if entity_id.startswith("todo."):
                try:
                    st["items"] = self.todo_items(entity_id)
                except Exception as e:  # noqa: BLE001
                    st["items_error"] = str(e)
            return json.dumps(st, ensure_ascii=False)
        except requests.HTTPError as e:
            return f"Error: {e.response.status_code} for {entity_id}"

    def _states(self):
        ts, states = self._cache
        if time.time() - ts > 30:
            states = self._get("/states")
            self._cache = (time.time(), states)
        return states

    @staticmethod
    def _hay(s: dict) -> str:
        a = s.get("attributes", {})
        h = " ".join(str(x) for x in (s["entity_id"], a.get("friendly_name", ""),
                                       a.get("device_class", ""), a.get("unit_of_measurement", ""))).lower()
        return h + " " + fold(h)

    def search(self, term: str, limit: int = 30) -> str:
        """Fuzzy search: every word (plus synonyms) scores, best matches first."""
        groups = []
        for w in term.lower().replace("-", " ").split():
            alts = {w, fold(w)}
            for key, syn in SYNONYMS.items():
                if key in w or w in key:
                    alts.update(syn)
            groups.append(alts)
        scored = []
        for s in self._states():
            hay = self._hay(s)
            score = sum(1 for alts in groups if any(a in hay for a in alts))
            if score:
                scored.append((score, s["entity_id"].startswith("sensor."), s))
        scored.sort(key=lambda x: (-x[0], not x[1], x[2]["entity_id"]))
        hits = [self._fmt(s) for _, _, s in scored[:limit]]
        log.info("Search '%s': %d hits", term, len(scored))
        if not hits:
            return f"No entity found for '{term}'. Try other or English terms, or use ha_list."
        return json.dumps(hits, ensure_ascii=False)

    def list_by(self, filter_: str, limit: int = 80) -> str:
        """All entities with a matching unit, device_class or domain (e.g. '°C', 'power', 'climate')."""
        f = filter_.lower().strip()
        hits = []
        for s in self._states():
            a = s.get("attributes", {})
            if (f == str(a.get("unit_of_measurement", "")).lower()
                    or f == str(a.get("device_class", "")).lower()
                    or s["entity_id"].startswith(f + ".")):
                hits.append(f'{s["entity_id"]} | {a.get("friendly_name", "")} | '
                            f'{s.get("state")} {a.get("unit_of_measurement", "") or ""}')
        log.info("List '%s': %d hits", filter_, len(hits))
        if not hits:
            return (f"Nothing for '{filter_}'. Examples: °C, W, kW, kWh, %, temperature, power, energy, "
                    "battery, sensor, climate, switch.")
        extra = f"\n… and {len(hits) - limit} more" if len(hits) > limit else ""
        return "\n".join(hits[:limit]) + extra

    def fire_event(self, name: str, data: dict):
        if not self.enabled:
            return
        try:
            self.http.post(f"{self.base}/events/{name}", json=data, timeout=10, verify=self.verify)
        except Exception as e:  # noqa: BLE001
            log.warning("Event %s failed: %s", name, e)

    # ------------------------------------------------------------------ control
    def call_service(self, entity_id: str, service: str, data: dict | None = None,
                     confirmed: bool = False) -> str:
        entity_id = (entity_id or "").strip()
        if "." not in entity_id:
            return "Error: entity_id missing or invalid."
        domain = entity_id.split(".", 1)[0]
        allowed = [d.strip() for d in (self.cfg.control_domains or [])]
        if domain not in allowed:
            return (f"Not allowed: domain '{domain}' must not be controlled. "
                    f"Allowed: {', '.join(allowed) or 'nothing'}.")
        if domain in (self.cfg.confirm_domains or []) and not confirmed:
            return ("Safety check: explicitly ask the caller whether you should really do this, "
                    "and only call the tool again with confirmed=true after a clear yes.")
        service = service.strip().split(".")[-1]
        body = {"entity_id": entity_id, **(data or {})}
        log.warning("CONTROL %s.%s %s", domain, service, body)
        r = self.http.post(f"{self.base}/services/{domain}/{service}", json=body,
                           timeout=15, verify=self.verify)
        if r.status_code >= 400:
            return f"Error {r.status_code}: {r.text[:200]}"
        time.sleep(1.0)
        self._cache = (0.0, [])
        return "Done. New state: " + self.get_state(entity_id)

    # ------------------------------------------------------------------ calendars
    def _calendars(self, wanted) -> list:
        cals = [s for s in self._states() if s["entity_id"].startswith("calendar.")]
        if not wanted:
            return cals
        out = []
        for w in wanted:
            w = fold(str(w))
            for c in cals:
                hay = fold(c["entity_id"] + " " + str(c.get("attributes", {}).get("friendly_name", "")))
                if (w == fold(c["entity_id"]) or w in hay) and c not in out:
                    out.append(c)
        return out

    def events(self, calendars=None, days: int = 7, start: str = "") -> str:
        """Appointments from HA calendars. calendars: entity_ids/name parts; empty = defaults from config."""
        wanted = calendars or self.cfg.default_calendars or []
        cals = self._calendars(wanted)
        if not cals:
            names = [c["entity_id"] for c in self._calendars([])]
            return f"No matching calendar. Available: {', '.join(names)}"
        now = datetime.now().astimezone()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            begin = datetime.fromisoformat(start).astimezone() if start else midnight
        except ValueError:
            begin = midnight
        end = begin + timedelta(days=max(1, min(int(days or 7), 366)))
        q = f"?start={begin.isoformat()}&end={end.isoformat()}".replace("+", "%2B")
        lines = []
        for c in cals:
            name = c.get("attributes", {}).get("friendly_name") or c["entity_id"]
            try:
                evs = self._get(f"/calendars/{c['entity_id']}{q}")
            except Exception as e:  # noqa: BLE001
                lines.append(f"[{name}] Error: {e}")
                continue
            for ev in evs:
                st, en = ev.get("start", {}), ev.get("end", {})
                if "dateTime" in st:
                    t = datetime.fromisoformat(st["dateTime"]).astimezone()
                    when = f"{_wd(t)} " + t.strftime("%Y-%m-%d %H:%M")
                    if "dateTime" in en:
                        when += "–" + datetime.fromisoformat(en["dateTime"]).astimezone().strftime("%H:%M")
                    key = t.isoformat()
                else:
                    d = datetime.fromisoformat(st.get("date", "1970-01-01"))
                    when = f"{_wd(d)} " + d.strftime("%Y-%m-%d") + " all-day"
                    key = d.isoformat()
                extra = f" ({ev['location']})" if ev.get("location") else ""
                lines.append((key, f"{when}: {ev.get('summary', '?')}{extra}  [{name}]"))
        found = sorted(x for x in lines if isinstance(x, tuple))
        errors = [x for x in lines if isinstance(x, str)]
        log.info("Events %s, %d days: %d", [c["entity_id"] for c in cals], (end - begin).days, len(found))
        head = (f"Period {begin:%Y-%m-%d} to {end - timedelta(seconds=1):%Y-%m-%d}, calendars: "
                + ", ".join(c.get("attributes", {}).get("friendly_name") or c["entity_id"] for c in cals))
        body = "\n".join(e for _, e in found) or "No appointments in this period."
        return "\n".join([head, body, *errors])

    def create_event(self, calendar: str, title: str, start: str, end: str = "",
                     location: str = "", description: str = "", confirmed: bool = False) -> str:
        """Create an appointment via calendar.create_event (only calendars whose integration supports it)."""
        cals = self._calendars([calendar] if calendar else (self.cfg.default_calendars or []))
        writable = [c for c in cals if int(c.get("attributes", {}).get("supported_features", 0)) & 1]
        if not writable:
            names = ", ".join(c.get("attributes", {}).get("friendly_name") or c["entity_id"] for c in cals) or calendar
            all_writable = [c["entity_id"] for c in self._calendars([])
                            if int(c.get("attributes", {}).get("supported_features", 0)) & 1]
            return (f"Calendar '{names}' cannot create appointments (read-only). "
                    f"Writable calendars: {', '.join(all_writable) or 'none'}.")
        cal = writable[0]
        if not confirmed:
            return ("Read the appointment (calendar, title, day, time) back to the caller and ask whether to add it. "
                    "Only call again with confirmed=true after a yes.")
        body = {"entity_id": cal["entity_id"], "summary": title}
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        try:
            if "T" in start:
                st = datetime.fromisoformat(start)
                st = st if st.tzinfo else st.astimezone()
                en = datetime.fromisoformat(end) if end and "T" in end else st + timedelta(hours=1)
                en = en if en.tzinfo else en.astimezone()
                body["start_date_time"] = st.isoformat()
                body["end_date_time"] = en.isoformat()
            else:
                d = datetime.fromisoformat(start).date()
                e = datetime.fromisoformat(end).date() + timedelta(days=1) if end else d + timedelta(days=1)
                body["start_date"] = d.isoformat()
                body["end_date"] = e.isoformat()  # HA: end date is exclusive
        except ValueError as e:
            return f"Date not readable ({e}). Format: 2026-10-30T14:00, or 2026-10-30 for all-day."
        log.warning("EVENT %s", body)
        r = self.http.post(f"{self.base}/services/calendar/create_event", json=body,
                           timeout=15, verify=self.verify)
        if r.status_code >= 400:
            return f"Error {r.status_code}: {r.text[:200]}"
        name = cal.get("attributes", {}).get("friendly_name") or cal["entity_id"]
        return f"Added to {name}: {title}."
