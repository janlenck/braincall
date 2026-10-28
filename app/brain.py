"""Claude as the conversation partner: streams sentences out so TTS can start early."""
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

log = logging.getLogger("brain")

BASE_PROMPT = """You are {name}, Jan's AI assistant (technically based on Claude by Anthropic), and you are on a phone call right now.
The first system note tells you who is on the line; address the person by name as soon as you know it.
Introduce yourself as {name}. Everything you write is read out loud by speech synthesis.

Phone rules:
- Short and direct, one to three sentences per answer. No filler phrases.
- Spoken language: no markdown, no lists, no emojis, no abbreviations. Spell out units (kilowatts, degrees).
- Rounded numbers work better on the phone than many decimals.
- If you need something from Home Assistant, use the tools before answering. Never make up values.
- Entity IDs are often English or technical (Vaillant/ebusd: hwc = hot water, flow = flow temperature,
  hmu = heat pump, ctlv = controller). Search several times with different terms, or use ha_list to get
  e.g. all temperatures (°C) and pick the right one.
- You can control devices with ha_control. Only switch what the caller explicitly asks for, then briefly say what you did.
  Check the exact entity_id with ha_search first. If it is unclear which device is meant, ask instead of guessing.
- Lists (shopping list, to-dos) are todo.* entities. ha_state returns their items.
  Change them with ha_control: service add_item (data {"item": "Milk"}), update_item (data {"item": "Eggs",
  "status": "completed"} to check off, or "needs_action" to reopen), remove_item (data {"item": "Eggs"}).
  Write the item exactly as it appears in the list.
- Create new appointments with ha_create_event. Convert relative dates like "Thursday" or "tomorrow"
  into a date based on the current time.
- Read appointments and holidays with ha_events (without calendars = Jan's default calendars).
  Say weekdays in the language of the conversation.
- Do not guess which sensor is meant. If several match or you are unsure, name the sensor and ask.
- When the conversation is done or the caller says goodbye: say a short goodbye and call the tool "hang_up".
- Text in square brackets starting with "System:" are notes from the phone system, not from the caller.

Current time: {now}
"""

LANG_RULES = {
    "de": "LANGUAGE: Speak German.",
    "uk": ("LANGUAGE: Speak Ukrainian (українська мова) throughout, natural and colloquial like on the phone. "
           "The rules above apply accordingly. Translate values from Home Assistant; device names may stay German."),
    "en": "LANGUAGE: Speak English throughout.",
    "auto": ("LANGUAGE: Always answer in the language the caller spoke last (e.g. German or Ukrainian). "
             "At the start, before the caller has said anything: German."),
}

TOOL_HANGUP = {
    "name": "hang_up",
    "description": "Ends the phone call. Say the goodbye as text in the same turn before calling this.",
    "input_schema": {"type": "object", "properties": {}},
}
TOOL_SEARCH = {
    "name": "ha_search",
    "description": "Fuzzy search for Home Assistant entities by ID, name, device class and unit "
                   "(German terms are expanded with English synonyms). Returns the best matches with current state.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "e.g. 'pv leistung' or 'garage'"}},
        "required": ["query"],
    },
}
TOOL_STATE = {
    "name": "ha_state",
    "description": "Returns the current state of exactly one Home Assistant entity (for todo lists including the items).",
    "input_schema": {
        "type": "object",
        "properties": {"entity_id": {"type": "string"}},
        "required": ["entity_id"],
    },
}
TOOL_LIST = {
    "name": "ha_list",
    "description": "Lists ALL entities with a given unit, device class or domain, including state. "
                   "Examples: '°C', 'W', 'kW', 'kWh', '%', 'temperature', 'power', 'battery', 'climate', 'switch', 'cover'.",
    "input_schema": {
        "type": "object",
        "properties": {"filter": {"type": "string"}},
        "required": ["filter"],
    },
}
TOOL_CONTROL = {
    "name": "ha_control",
    "description": "Calls a Home Assistant service for exactly one entity, e.g. light on/off. "
                   "service e.g. turn_on, turn_off, toggle, open_cover, close_cover, set_temperature, "
                   "set_hvac_mode, turn_on with data {'brightness_pct': 50}. Returns the new state afterwards.",
    "input_schema": {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string"},
            "service": {"type": "string"},
            "data": {"type": "object", "description": "optional service parameters"},
            "confirmed": {"type": "boolean",
                          "description": "only true if the caller explicitly agreed when asked"},
        },
        "required": ["entity_id", "service"],
    },
}
TOOL_EVENTS = {
    "name": "ha_events",
    "description": "Reads appointments from Home Assistant calendars (calendar.*). Without 'calendars' Jan's default "
                   "calendars are used. 'calendars' are entity_ids or name parts, e.g. ['molln', 'feiertage']. "
                   "'days' = period starting today (default 7), 'start' = optional start date YYYY-MM-DD.",
    "input_schema": {
        "type": "object",
        "properties": {
            "calendars": {"type": "array", "items": {"type": "string"}},
            "days": {"type": "integer"},
            "start": {"type": "string"},
        },
    },
}
TOOL_CREATE_EVENT = {
    "name": "ha_create_event",
    "description": "Creates an appointment in a Home Assistant calendar. start/end as 2026-10-30T14:00 "
                   "(without end = 1 hour) or date only 2026-10-30 for all-day. Without calendar = Jan's default calendar. "
                   "Only call with confirmed=true after the caller explicitly said yes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "calendar": {"type": "string", "description": "entity_id or name part, e.g. 'molln'"},
            "location": {"type": "string"},
            "description": {"type": "string"},
            "confirmed": {"type": "boolean"},
        },
        "required": ["title", "start"],
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
        low = s.lower()
        if "tool call" in low or "hang_up]" in low or "auflegen]" in low:
            self.fake_hangup = True
        s = re.sub(r"\[[^\]]*\]?", "", s)  # never read out "[Tool call: …]" and the like
        s = re.sub(r"^[^\[]*\]", "", s)    # rest of a bracket that was split across two sentences
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
        now = datetime.now(ZoneInfo(self.cfg.timezone)).strftime("%A, %Y-%m-%d %H:%M")
        prompt = BASE_PROMPT.replace("{now}", now).replace("{name}", self.cfg.assistant_name or "Claude")
        prompt += "\n" + LANG_RULES.get(language, LANG_RULES["auto"])
        if self.cfg.extra_prompt:
            prompt += "\n" + self.cfg.extra_prompt
        return prompt

    def _run_tool(self, block) -> str:
        log.info("Tool %s %s", block.name, block.input)
        i = block.input
        try:
            if block.name == "ha_search":
                return self.ha.search(i.get("query", ""))
            if block.name == "ha_list":
                return self.ha.list_by(i.get("filter", ""))
            if block.name == "ha_events":
                return self.ha.events(i.get("calendars"), i.get("days") or 7, i.get("start") or "")
            if block.name == "ha_create_event":
                return self.ha.create_event(i.get("calendar", ""), i.get("title", ""), i.get("start", ""),
                                            i.get("end", ""), i.get("location", ""), i.get("description", ""),
                                            bool(i.get("confirmed")))
            if block.name == "ha_control":
                return self.ha.call_service(i.get("entity_id", ""), i.get("service", ""),
                                            i.get("data") or {}, bool(i.get("confirmed")))
            if block.name == "ha_state":
                return self.ha.get_state(i.get("entity_id", ""))
        except Exception as e:  # noqa: BLE001
            log.warning("Tool %s failed: %s", block.name, e)
            return f"Error: {e}"
        return "Unknown tool."

    @staticmethod
    def _repair(messages: list):
        """Safety net: every tool_use needs a tool_result right after it, otherwise the API rejects the request."""
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
                        fill = [{"type": "tool_result", "tool_use_id": t, "content": "Not executed (aborted)."}
                                for t in missing]
                        if nxt and nxt["role"] == "user":
                            extra = (nxt["content"] if isinstance(nxt["content"], list)
                                     else [{"type": "text", "text": nxt["content"]}])
                            nxt["content"] = fill + extra
                        else:
                            messages.insert(i + 1, {"role": "user", "content": fill})
            i += 1

    def turn(self, messages: list, on_sentence, should_abort, restricted: bool = False,
             language: str = "de") -> bool:
        """One assistant turn including the tool loop. Returns True = hang up."""
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
                messages.append({"role": "assistant", "content": partial + " [interrupted by the caller]"})
                return False

            # Do not execute incomplete tool calls (cut off at max_tokens)
            uses = [b for b in msg.content if b.type == "tool_use"
                    and not (msg.stop_reason == "max_tokens" and b is msg.content[-1])]
            content = [b.model_dump(exclude_none=True) for b in msg.content
                       if b.type != "tool_use" or b in uses]
            if not content:
                content = [{"type": "text", "text": "…"}]
            messages.append({"role": "assistant", "content": content})
            if not uses:
                if splitter.fake_hangup:  # model wrote "hang up" as text instead of using the tool
                    log.info("Hang-up written as text, treating it as hang-up")
                    return True
                return False

            hang = False
            results = []
            for b in uses:
                if b.name == "hang_up":
                    hang = True
                    out = "Hung up."
                else:
                    out = self._run_tool(b)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
            messages.append({"role": "user", "content": results})
            if hang:
                return True
        return False
