"""Lesender Zugriff auf Home Assistant (im Add-on über den Supervisor-Proxy)."""
import json
import logging
import time

from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("ha")

# Deutsch -> typische (oft englische) Namen in Entity-IDs, u. a. ebusd/Vaillant, Fronius, evcc
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


WOCHENTAG = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _wd(d) -> str:
    return WOCHENTAG[d.weekday()]


def fold(t: str) -> str:
    """Umlaute wie in Entity-IDs: Mölln -> molln."""
    return (t.lower().replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss"))


def _local_time(iso: str) -> str:
    """UTC-Zeitstempel aus HA -> lokale Uhrzeit + Alter, damit Claude nicht UTC vorliest."""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        mins = int((datetime.now(timezone.utc) - t).total_seconds() // 60)
        local = t.astimezone()
        when = local.strftime("%H:%M") if mins < 1440 else local.strftime("%d.%m. %H:%M")
        return f"{when} Uhr Ortszeit (vor {mins} Minuten)" if mins < 180 else f"{when} Uhr Ortszeit"
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
            "zustand": s.get("state"),
            "einheit": a.get("unit_of_measurement"),
            "geaendert": _local_time(s.get("last_changed", "")),
        }

    def todo_items(self, entity_id: str) -> list:
        """Einträge einer To-do-/Einkaufsliste (der Zustand selbst enthält nur die Anzahl)."""
        r = self.http.post(f"{self.base}/services/todo/get_items?return_response",
                           json={"entity_id": entity_id}, timeout=10, verify=self.verify)
        r.raise_for_status()
        data = r.json().get("service_response", {}).get(entity_id, {})
        return [{"eintrag": i.get("summary"),
                 "status": "erledigt" if i.get("status") == "completed" else "offen",
                 **({"notiz": i["description"]} if i.get("description") else {}),
                 **({"faellig": i["due"]} if i.get("due") else {})}
                for i in data.get("items", [])]

    def get_state(self, entity_id: str) -> str:
        try:
            st = self._fmt(self._get(f"/states/{entity_id}"))
            if entity_id.startswith("todo."):
                try:
                    st["eintraege"] = self.todo_items(entity_id)
                except Exception as e:  # noqa: BLE001
                    st["eintraege_fehler"] = str(e)
            return json.dumps(st, ensure_ascii=False)
        except requests.HTTPError as e:
            return f"Fehler: {e.response.status_code} für {entity_id}"

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
        """Unscharfe Suche: jedes Wort (plus Synonyme) zählt, beste Treffer zuerst."""
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
        log.info("Suche '%s': %d Treffer", term, len(scored))
        if not hits:
            return f"Keine Entität zu '{term}' gefunden. Andere oder englische Begriffe probieren, oder ha_liste nutzen."
        return json.dumps(hits, ensure_ascii=False)

    def list_by(self, filter_: str, limit: int = 80) -> str:
        """Alle Entitäten mit passender Einheit, device_class oder Domain (z. B. '°C', 'power', 'climate')."""
        f = filter_.lower().strip()
        hits = []
        for s in self._states():
            a = s.get("attributes", {})
            if (f == str(a.get("unit_of_measurement", "")).lower()
                    or f == str(a.get("device_class", "")).lower()
                    or s["entity_id"].startswith(f + ".")):
                hits.append(f'{s["entity_id"]} | {a.get("friendly_name", "")} | {s.get("state")} {a.get("unit_of_measurement", "") or ""}')
        log.info("Liste '%s': %d Treffer", filter_, len(hits))
        if not hits:
            return f"Nichts zu '{filter_}'. Beispiele: °C, W, kW, kWh, %, temperature, power, energy, battery, sensor, climate, switch."
        extra = f"\n… und {len(hits) - limit} weitere" if len(hits) > limit else ""
        return "\n".join(hits[:limit]) + extra

    def fire_event(self, name: str, data: dict):
        if not self.enabled:
            return
        try:
            self.http.post(f"{self.base}/events/{name}", json=data, timeout=10, verify=self.verify)
        except Exception as e:  # noqa: BLE001
            log.warning("Event %s fehlgeschlagen: %s", name, e)

    # ------------------------------------------------------------------ Schalten
    def call_service(self, entity_id: str, service: str, data: dict | None = None,
                     bestaetigt: bool = False) -> str:
        entity_id = (entity_id or "").strip()
        if "." not in entity_id:
            return "Fehler: entity_id fehlt oder ist ungültig."
        domain = entity_id.split(".", 1)[0]
        allowed = [d.strip() for d in (self.cfg.control_domains or [])]
        if domain not in allowed:
            return (f"Nicht erlaubt: Bereich '{domain}' darf nicht geschaltet werden. "
                    f"Erlaubt sind: {', '.join(allowed) or 'nichts'}.")
        if domain in (self.cfg.confirm_domains or []) and not bestaetigt:
            return ("Sicherheitsabfrage: Frag Jan ausdrücklich, ob du das wirklich tun sollst, "
                    "und rufe das Werkzeug erst nach seinem klaren Ja mit bestaetigt=true erneut auf.")
        service = service.strip().split(".")[-1]
        body = {"entity_id": entity_id, **(data or {})}
        log.warning("SCHALTE %s.%s %s", domain, service, body)
        r = self.http.post(f"{self.base}/services/{domain}/{service}", json=body,
                           timeout=15, verify=self.verify)
        if r.status_code >= 400:
            return f"Fehler {r.status_code}: {r.text[:200]}"
        time.sleep(1.0)
        self._cache = (0.0, [])
        return "Ausgeführt. Neuer Zustand: " + self.get_state(entity_id)

    # ------------------------------------------------------------------ Kalender
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

    def events(self, kalender=None, tage: int = 7, ab: str = "") -> str:
        """Termine aus HA-Kalendern. kalender: Liste von entity_ids/Namensteilen; leer = Standard aus config."""
        wanted = kalender or self.cfg.default_calendars or []
        cals = self._calendars(wanted)
        if not cals:
            names = [c["entity_id"] for c in self._calendars([])]
            return f"Kein passender Kalender. Vorhanden: {', '.join(names)}"
        now = datetime.now().astimezone()
        try:
            start = datetime.fromisoformat(ab).astimezone() if ab else now.replace(hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=max(1, min(int(tage or 7), 366)))
        q = f"?start={start.isoformat()}&end={end.isoformat()}".replace("+", "%2B")
        lines = []
        for c in cals:
            name = c.get("attributes", {}).get("friendly_name") or c["entity_id"]
            try:
                evs = self._get(f"/calendars/{c['entity_id']}{q}")
            except Exception as e:  # noqa: BLE001
                lines.append(f"[{name}] Fehler: {e}")
                continue
            for ev in evs:
                st, en = ev.get("start", {}), ev.get("end", {})
                if "dateTime" in st:
                    t = datetime.fromisoformat(st["dateTime"]).astimezone()
                    when = f"{_wd(t)} " + t.strftime("%d.%m. %H:%M")
                    if "dateTime" in en:
                        when += "–" + datetime.fromisoformat(en["dateTime"]).astimezone().strftime("%H:%M")
                    key = t.isoformat()
                else:
                    d = datetime.fromisoformat(st.get("date", "1970-01-01"))
                    when = f"{_wd(d)} " + d.strftime("%d.%m.") + " ganztägig"
                    key = d.isoformat()
                extra = f" ({ev['location']})" if ev.get("location") else ""
                lines.append((key, f"{when}: {ev.get('summary', '?')}{extra}  [{name}]"))
        events_ = sorted(x for x in lines if isinstance(x, tuple))
        errors = [x for x in lines if isinstance(x, str)]
        log.info("Termine %s, %d Tage: %d", [c["entity_id"] for c in cals], (end - start).days, len(events_))
        head = f"Zeitraum {start:%d.%m.} bis {end - timedelta(seconds=1):%d.%m.%Y}, Kalender: " + \
               ", ".join(c.get("attributes", {}).get("friendly_name") or c["entity_id"] for c in cals)
        body = "\n".join(e for _, e in events_) or "Keine Termine im Zeitraum."
        return "\n".join([head, body, *errors])

    def create_event(self, kalender: str, titel: str, start: str, ende: str = "",
                     ort: str = "", beschreibung: str = "", bestaetigt: bool = False) -> str:
        """Termin anlegen über calendar.create_event (nur Kalender, deren Integration das kann)."""
        cals = self._calendars([kalender] if kalender else (self.cfg.default_calendars or []))
        writable = [c for c in cals if int(c.get("attributes", {}).get("supported_features", 0)) & 1]
        if not writable:
            names = ", ".join(c.get("attributes", {}).get("friendly_name") or c["entity_id"] for c in cals) or kalender
            alle = [c["entity_id"] for c in self._calendars([])
                    if int(c.get("attributes", {}).get("supported_features", 0)) & 1]
            return (f"Kalender '{names}' kann keine Termine anlegen (nur lesbar). "
                    f"Beschreibbare Kalender: {', '.join(alle) or 'keine'}.")
        cal = writable[0]
        if not bestaetigt:
            return ("Lies Jan den Termin (Kalender, Titel, Tag, Uhrzeit) kurz vor und frag, ob du ihn eintragen sollst. "
                    "Erst nach seinem Ja mit bestaetigt=true erneut aufrufen.")
        body = {"entity_id": cal["entity_id"], "summary": titel}
        if ort:
            body["location"] = ort
        if beschreibung:
            body["description"] = beschreibung
        try:
            if "T" in start:
                st = datetime.fromisoformat(start)
                st = st if st.tzinfo else st.astimezone()
                en = datetime.fromisoformat(ende) if ende and "T" in ende else st + timedelta(hours=1)
                en = en if en.tzinfo else en.astimezone()
                body["start_date_time"] = st.isoformat()
                body["end_date_time"] = en.isoformat()
            else:
                d = datetime.fromisoformat(start).date()
                e = datetime.fromisoformat(ende).date() + timedelta(days=1) if ende else d + timedelta(days=1)
                body["start_date"] = d.isoformat()
                body["end_date"] = e.isoformat()  # HA: Ende exklusiv
        except ValueError as e:
            return f"Datum nicht lesbar ({e}). Format: 2026-10-30T14:00 oder 2026-10-30 für ganztägig."
        log.warning("TERMIN %s", body)
        r = self.http.post(f"{self.base}/services/calendar/create_event", json=body,
                           timeout=15, verify=self.verify)
        if r.status_code >= 400:
            return f"Fehler {r.status_code}: {r.text[:200]}"
        name = cal.get("attributes", {}).get("friendly_name") or cal["entity_id"]
        return f"Eingetragen in {name}: {titel}."
