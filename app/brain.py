"""Claude als Gesprächspartner: streamt Sätze raus, damit TTS früh starten kann."""
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

log = logging.getLogger("brain")

BASE_PROMPT = """Du bist {name}, Jans KI-Assistentin (technisch basierend auf Claude von Anthropic), und telefonierst gerade. Wer am Apparat ist, sagt dir der erste System-Hinweis; sprich die Person mit Namen an, sobald du ihn kennst.
Stell dich mit deinem Namen {name} vor. Alles, was du schreibst, wird per Sprachsynthese vorgelesen.

Regeln fürs Telefon:
- Kurz und direkt, ein bis drei Sätze pro Antwort. Keine Floskeln.
- Gesprochene Sprache: kein Markdown, keine Listen, keine Emojis, keine Abkürzungen. Einheiten ausschreiben (Kilowatt, Grad).
- Rundende Zahlen sind am Telefon besser als viele Nachkommastellen.
- Wenn du etwas aus Home Assistant brauchst, nutze die Werkzeuge, bevor du antwortest. Erfinde keine Werte.
- Entity-IDs sind oft englisch oder technisch (Vaillant/ebusd: hwc = Warmwasser, flow = Vorlauf, hmu = Wärmepumpe, ctlv = Regler).
  Suche deshalb mehrmals mit verschiedenen Begriffen, oder lass dir mit ha_liste z. B. alle Temperaturen (°C) geben und wähle dann.
- Du kannst Geräte mit ha_schalten schalten. Schalte nur, was dein Gesprächspartner ausdrücklich will, und sag danach kurz, was du getan hast.
  Prüfe vorher mit ha_suchen die genaue entity_id. Bei Unklarheit, welches Gerät gemeint ist: nachfragen statt raten.
- Listen (Einkaufsliste, To-dos) sind todo.-Entitäten. ha_zustand liefert ihre Einträge.
  Ändern mit ha_schalten: service add_item (daten {"item": "Milch"}), update_item (daten {"item": "Eier", "status": "completed"}
  zum Abhaken oder "needs_action" zum Zurücksetzen), remove_item (daten {"item": "Eier"}). Den Eintrag exakt so schreiben wie in der Liste.
- Neue Termine trägst du mit ha_termin_anlegen ein. Relative Angaben wie "Donnerstag" oder "morgen" rechnest du
  anhand der aktuellen Zeit in ein Datum um.
- Termine und Feiertage holst du mit ha_termine (ohne kalender = Jans Standardkalender). Wochentage auf Deutsch aussprechen.
- Rate nicht, welcher Sensor gemeint ist. Wenn mehrere passen oder du unsicher bist, nenne den Sensornamen und frag nach.
- Wenn das Gespräch erledigt ist oder dein Gesprächspartner sich verabschiedet: kurz verabschieden und das Werkzeug "auflegen" aufrufen.
- Texte in eckigen Klammern mit "System:" sind Hinweise der Telefonanlage, nicht vom Gesprächspartner.

Aktuelle Zeit: {now}
"""

LANG_RULES = {
    "de": "SPRACHE: Sprich Deutsch.",
    "uk": ("SPRACHE: Sprich durchgehend Ukrainisch (українська мова), natürlich und umgangssprachlich wie am Telefon. "
           "Die Regeln oben gelten sinngemäß. Werte aus Home Assistant übersetzt du; Gerätenamen darfst du deutsch lassen."),
    "en": "LANGUAGE: Speak English throughout.",
    "auto": ("SPRACHE: Antworte immer in der Sprache, in der der Anrufer zuletzt gesprochen hat "
             "(z. B. Deutsch oder Ukrainisch). Zu Beginn, bevor er etwas gesagt hat: Deutsch."),
}

TOOL_HANGUP = {
    "name": "auflegen",
    "description": "Beendet das Telefonat. Die Verabschiedung vorher als Text im selben Zug sprechen.",
    "input_schema": {"type": "object", "properties": {}},
}
TOOL_SEARCH = {
    "name": "ha_suchen",
    "description": "Unscharfe Suche nach Home-Assistant-Entitäten über ID, Name, Geräteklasse und Einheit (deutsche Begriffe werden um englische Synonyme ergänzt). Liefert die besten Treffer mit aktuellem Zustand.",
    "input_schema": {
        "type": "object",
        "properties": {"begriff": {"type": "string", "description": "z. B. 'pv leistung' oder 'garage'"}},
        "required": ["begriff"],
    },
}
TOOL_STATE = {
    "name": "ha_zustand",
    "description": "Liefert den aktuellen Zustand genau einer Home-Assistant-Entität.",
    "input_schema": {
        "type": "object",
        "properties": {"entity_id": {"type": "string"}},
        "required": ["entity_id"],
    },
}

TOOL_LIST = {
    "name": "ha_liste",
    "description": "Listet ALLE Entitäten mit einer bestimmten Einheit, Geräteklasse oder Domain samt Zustand. "
                   "Beispiele: '°C', 'W', 'kW', 'kWh', '%', 'temperature', 'power', 'battery', 'climate', 'switch', 'cover'.",
    "input_schema": {
        "type": "object",
        "properties": {"filter": {"type": "string"}},
        "required": ["filter"],
    },
}

TOOL_CONTROL = {
    "name": "ha_schalten",
    "description": "Ruft einen Home-Assistant-Dienst für genau eine Entität auf, z. B. Licht an/aus. "
                   "service z. B. turn_on, turn_off, toggle, open_cover, close_cover, set_temperature, "
                   "set_hvac_mode, turn_on mit daten {'brightness_pct': 50}. Liefert danach den neuen Zustand.",
    "input_schema": {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string"},
            "service": {"type": "string"},
            "daten": {"type": "object", "description": "optionale Dienst-Parameter"},
            "bestaetigt": {"type": "boolean", "description": "nur true, wenn der Gesprächspartner auf Nachfrage ausdrücklich zugestimmt hat"},
        },
        "required": ["entity_id", "service"],
    },
}

TOOL_EVENTS = {
    "name": "ha_termine",
    "description": "Liest Termine aus Home-Assistant-Kalendern (calendar.*). Ohne 'kalender' werden Jans Standardkalender "
                   "genommen. 'kalender' sind entity_ids oder Namensteile, z. B. ['molln', 'feiertage']. "
                   "'tage' = Zeitraum ab heute (Standard 7), 'ab' = optionales Startdatum JJJJ-MM-TT.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kalender": {"type": "array", "items": {"type": "string"}},
            "tage": {"type": "integer"},
            "ab": {"type": "string"},
        },
    },
}

TOOL_CREATE_EVENT = {
    "name": "ha_termin_anlegen",
    "description": "Trägt einen Termin in einen Home-Assistant-Kalender ein. start/ende als 2026-10-30T14:00 "
                   "(ohne ende = 1 Stunde) oder nur Datum 2026-10-30 für ganztägig. Ohne kalender = Jans Standardkalender. "
                   "Erst nach ausdrücklichem Ja des Gesprächspartners mit bestaetigt=true aufrufen.",
    "input_schema": {
        "type": "object",
        "properties": {
            "titel": {"type": "string"},
            "start": {"type": "string"},
            "ende": {"type": "string"},
            "kalender": {"type": "string", "description": "entity_id oder Namensteil, z. B. 'molln'"},
            "ort": {"type": "string"},
            "beschreibung": {"type": "string"},
            "bestaetigt": {"type": "boolean"},
        },
        "required": ["titel", "start"],
    },
}

_END = re.compile(r"[.!?…:](?=\s)")


class SentenceSplitter:
    def __init__(self, emit):
        self.emit = emit
        self.buf = ""
        self.emitted = []
        self.fake_hangup = False

    def _out(self, s: str):
        if "tool call" in s.lower() or "auflegen]" in s.lower():
            self.fake_hangup = True
        s = re.sub(r"\[[^\]]*\]?", "", s)             # [Tool call: …] o. ä. nie vorlesen
        s = re.sub(r"^[^\[]*\]", "", s)                # Rest einer über zwei Sätze verteilten Klammer
        s = s.replace("*", "").replace("#", "").strip()
        if s:
            self.emitted.append(s)
            self.emit(s)

    def feed(self, text: str):
        self.buf += text
        while True:
            m = None
            for cand in _END.finditer(self.buf):
                if cand.end() >= 12:
                    m = cand
                    break
            if m:
                self._out(self.buf[: m.end()])
                self.buf = self.buf[m.end():]
                continue
            if len(self.buf) > 220 and ", " in self.buf[100:]:
                cut = self.buf.rindex(", ") + 1
                self._out(self.buf[:cut])
                self.buf = self.buf[cut:]
                continue
            break

    def flush(self):
        self._out(self.buf)
        self.buf = ""


class Brain:
    def __init__(self, cfg, ha):
        self.cfg = cfg
        self.ha = ha
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self.tools = [TOOL_HANGUP] + ([TOOL_SEARCH, TOOL_LIST, TOOL_STATE, TOOL_EVENTS] if ha.enabled else [])
        if ha.enabled and cfg.control_domains:
            self.tools.append(TOOL_CONTROL)
        if ha.enabled and cfg.calendar_write:
            self.tools.append(TOOL_CREATE_EVENT)

    def system(self, language: str = "de") -> str:
        now = datetime.now(ZoneInfo(self.cfg.timezone)).strftime("%A, %d.%m.%Y %H:%M")
        prompt = BASE_PROMPT.replace("{now}", now).replace("{name}", self.cfg.assistant_name or "Claude")
        prompt += "\n" + LANG_RULES.get(language, LANG_RULES["auto"])
        if self.cfg.extra_prompt:
            prompt += "\n" + self.cfg.extra_prompt
        return prompt

    def _run_tool(self, block) -> str:
        log.info("Tool %s %s", block.name, block.input)
        try:
            if block.name == "ha_suchen":
                return self.ha.search(block.input.get("begriff", ""))
            if block.name == "ha_liste":
                return self.ha.list_by(block.input.get("filter", ""))
            if block.name == "ha_termine":
                i = block.input
                return self.ha.events(i.get("kalender"), i.get("tage") or 7, i.get("ab") or "")
            if block.name == "ha_termin_anlegen":
                i = block.input
                return self.ha.create_event(i.get("kalender", ""), i.get("titel", ""), i.get("start", ""),
                                            i.get("ende", ""), i.get("ort", ""), i.get("beschreibung", ""),
                                            bool(i.get("bestaetigt")))
            if block.name == "ha_schalten":
                i = block.input
                return self.ha.call_service(i.get("entity_id", ""), i.get("service", ""),
                                            i.get("daten") or {}, bool(i.get("bestaetigt")))
            if block.name == "ha_zustand":
                return self.ha.get_state(block.input.get("entity_id", ""))
        except Exception as e:  # noqa: BLE001
            log.warning("Tool %s fehlgeschlagen: %s", block.name, e)
            return f"Fehler: {e}"
        return "Unbekanntes Werkzeug."

    @staticmethod
    def _repair(messages: list):
        """Sicherheitsnetz: jedes tool_use braucht direkt danach ein tool_result, sonst lehnt die API ab."""
        i = 0
        while i < len(messages):
            m = messages[i]
            if m["role"] == "assistant" and isinstance(m["content"], list):
                ids = [b["id"] for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
                if ids:
                    nxt = messages[i + 1] if i + 1 < len(messages) else None
                    have = set()
                    if nxt and nxt["role"] == "user" and isinstance(nxt["content"], list):
                        have = {b.get("tool_use_id") for b in nxt["content"] if isinstance(b, dict)}
                    missing = [t for t in ids if t not in have]
                    if missing:
                        fill = [{"type": "tool_result", "tool_use_id": t, "content": "Nicht ausgeführt (abgebrochen)."}
                                for t in missing]
                        if nxt and nxt["role"] == "user":
                            extra = nxt["content"] if isinstance(nxt["content"], list) else [{"type": "text", "text": nxt["content"]}]
                            nxt["content"] = fill + extra
                        else:
                            messages.insert(i + 1, {"role": "user", "content": fill})
            i += 1

    def turn(self, messages: list, on_sentence, should_abort, restricted: bool = False,
             language: str = "de") -> bool:
        """Ein Antwortzug inkl. Tool-Schleife. Rückgabe True = auflegen."""
        for _ in range(8):
            self._repair(messages)
            splitter = SentenceSplitter(on_sentence)
            aborted = False
            with self.client.messages.stream(
                model=self.cfg.claude_model,
                max_tokens=2000,
                system=self.system(language),
                tools=[TOOL_HANGUP] if restricted else self.tools,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    if should_abort():
                        aborted = True
                        break
                    splitter.feed(text)
                if not aborted:
                    splitter.flush()
                    msg = stream.get_final_message()

            if aborted:
                partial = " ".join(splitter.emitted) or "…"
                messages.append({"role": "assistant", "content": partial + " [vom Gesprächspartner unterbrochen]"})
                return False

            # Unvollständige Tool-Aufrufe (bei max_tokens abgeschnitten) nicht ausführen
            uses = [b for b in msg.content if b.type == "tool_use"
                    and not (msg.stop_reason == "max_tokens" and b is msg.content[-1])]
            content = [b.model_dump(exclude_none=True) for b in msg.content
                       if b.type != "tool_use" or b in uses]
            if not content:
                content = [{"type": "text", "text": "…"}]
            messages.append({"role": "assistant", "content": content})
            if not uses:
                if splitter.fake_hangup:  # Modell hat "auflegen" als Text geschrieben statt das Werkzeug zu nutzen
                    log.info("Auflegen als Text erkannt")
                    return True
                return False

            hang = False
            results = []
            for b in uses:
                if b.name == "auflegen":
                    hang = True
                    out = "Aufgelegt."
                else:
                    out = self._run_tool(b)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
            messages.append({"role": "user", "content": results})
            if hang:
                return True
        return False
