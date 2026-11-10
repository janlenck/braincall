"""Automations and scripts explained for the phone (tool ha_automation): the definition condensed into a few lines
with friendly names, the conditions checked against the current states (which branch would run now), and the
last runs from the traces (what triggered, which condition failed, which branch ran)."""
import math
import re
from datetime import datetime, time as dtime, timedelta, timezone

from ha import _duration, _float, _val, _when

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# The most lines per part of the definition, of the evaluation and of the steps of a run; runs listed in total
DEF_LINES = {"triggers": 5, "conditions": 6, "actions": 8}
NOW_LINES = 14
RUN_LINES = 12
RUNS = 5
TEXT_MAX = 200
TEMPLATE_MAX = 90
SUN_MARGIN = 15 * 60  # seconds: closer than this to an estimated sunrise/sunset is reported as unknown
ANSWER = {True: "yes", False: "no", None: "unknown"}

# script_execution of a trace -> what it means
EXECUTION = {
    "finished": "ran completely",
    "failed_conditions": "triggered, but the conditions were not met: no action ran",
    "failed_single": "not started: it was still running from before (mode single)",
    "failed_max_runs": "not started: too many runs at the same time",
    "aborted": "stopped by a condition or stop step in the actions",
    "error": "stopped with an error",
    "cancelled": "cancelled (restarted by a new trigger, or turned off)",
}
# Trace paths: global conditions, steps of any sequence, conditions of choose options and of if
_COND_PATH = re.compile(r"^conditions?/(\d+)$")
_STEP_PATH = re.compile(r"(?:^|/)(?:actions?|sequence|then|else|default|parallel)/\d+$")
_BRANCH_COND = re.compile(r"(?:^|/)(?:choose/\d+/conditions?|if/conditions?)/\d+$")
# Keys of the definition a trace path names, and those that hold lists
_ALIASES = {"conditions": ("conditions", "condition"), "condition": ("condition", "conditions"),
            "action": ("actions", "action"), "actions": ("actions", "action")}
_LIST_KEYS = ("choose", "sequence", "then", "else", "default", "parallel", "conditions", "if")


def as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _short(text, n: int = TEXT_MAX) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _cap(lines: list, n: int) -> list:
    return lines if len(lines) <= n else lines[:n] + [f"… {len(lines) - n} more"]


def parts(config: dict):
    """(triggers, conditions, actions) of an automation (new plural keys or old singular ones) or a script."""
    triggers = config.get("triggers", config.get("trigger"))
    conditions = config.get("conditions", config.get("condition"))
    actions = config.get("actions", config.get("action"))
    if actions is None:
        actions = config.get("sequence")
    return triggers, conditions, actions


def seconds_of(v):
    """'00:10:00', '10:00' (HH:MM), {'minutes': 10} or 600 -> seconds; None for templates or unreadable values."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        keys = ("days", "hours", "minutes", "seconds", "milliseconds")
        try:
            return timedelta(**{k: float(x) for k, x in v.items() if k in keys}).total_seconds()
        except (TypeError, ValueError):
            return None
    t = str(v).strip()
    sign = -1 if t.startswith("-") else 1
    try:
        nums = [float(x) for x in t.lstrip("+-").split(":")]
    except ValueError:
        return None
    if len(nums) == 1:
        return sign * nums[0]
    if len(nums) == 2:
        nums.append(0.0)
    if len(nums) != 3:
        return None
    return sign * (nums[0] * 3600 + nums[1] * 60 + nums[2])


def _span(v) -> str:
    """Duration or offset for reading out: '10 min', '-30 min', or the template as it is."""
    s = seconds_of(v)
    if s is None:
        return _short(v, TEMPLATE_MAX)
    return ("-" if s < 0 else "") + _duration(abs(s))


def _is_true(text) -> bool:
    """Result of a template condition like Home Assistant reads it: only 'true' (any case) counts."""
    return str(text).strip().lower() == "true"


def _parse_time(text):
    """ISO timestamp -> aware datetime, or None."""
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else None


def time_pattern(hours=None, minutes=None, seconds=None) -> str:
    """time_pattern trigger in words. Like Home Assistant: a smaller unit defaults to 0 when a larger one is set,
    otherwise to every value."""
    h, m, s = (None if v is None else str(v).strip() for v in (hours, minutes, seconds))
    if m is None and h is not None:
        m = "0"
    if s is None and m is not None:
        s = "0"

    def step(v):
        if v in (None, "*"):
            return 1
        found = re.fullmatch(r"/(\d+)", v)
        return int(found.group(1)) if found else None

    def fixed(v):
        return int(v) if v is not None and v.isdigit() else None

    def every(n, unit):
        return f"every {unit}" if n == 1 else f"every {n} {unit}s"

    if fixed(s) == 0:
        if fixed(m) is not None:
            if fixed(h) is not None:
                return f"daily at {fixed(h):02d}:{fixed(m):02d}"
            if step(h):
                return every(step(h), "hour") + f" at :{fixed(m):02d}"
        elif step(m) and h in (None, "*"):
            return every(step(m), "minute")
        elif step(m) and fixed(h) is not None:
            return every(step(m), "minute") + f" between {fixed(h):02d}:00 and {fixed(h):02d}:59"
    elif step(s) and m is None and h is None:
        return every(step(s), "second")
    given = ", ".join(f"{k} {v}" for k, v in (("hours", h), ("minutes", m), ("seconds", s)) if v is not None)
    return f"time pattern ({given})"


class Explainer:
    """Describes and evaluates one automation or script.
    states: entity_id -> state dict of Home Assistant; render(template) -> rendered text (raises on errors)."""

    def __init__(self, states: dict, tz=None, render=None, now: datetime | None = None):
        self.states = states
        self.tz = tz
        self.render = render
        self.now = (now or datetime.now(timezone.utc)).astimezone(tz)

    # ------------------------------------------------------------------ names and values
    def name(self, eid) -> str:
        s = self.states.get(str(eid)) or {}
        return s.get("attributes", {}).get("friendly_name") or str(eid)

    def names(self, eids, joiner: str = " or ") -> str:
        return joiner.join(self.name(e) for e in as_list(eids)) or "?"

    def _unit(self, c: dict) -> str:
        if c.get("attribute"):
            return ""
        first = next(iter(as_list(c.get("entity_id"))), "")
        return (self.states.get(str(first)) or {}).get("attributes", {}).get("unit_of_measurement") or ""

    @staticmethod
    def _is_entity(v) -> bool:
        return isinstance(v, str) and "." in v and _float(v) is None and " " not in v and "{" not in v

    def _limit(self, v, unit: str) -> str:
        """Threshold of numeric_state: a number, or another entity (input_number, sensor)."""
        if self._is_entity(v):
            return self.name(v)
        f = _float(v)
        return _val(f, unit) if f is not None else _short(v, TEMPLATE_MAX)

    def _range(self, c: dict, verb: str = "") -> str:
        unit = self._unit(c)
        out = []
        if c.get("above") is not None:
            out.append(f"{verb}above {self._limit(c['above'], unit)}")
        if c.get("below") is not None:
            out.append(f"{verb}below {self._limit(c['below'], unit)}")
        return " and ".join(out) or "has a number"

    @staticmethod
    def _values(v) -> str:
        return " or ".join(str(x) for x in as_list(v))

    def _who(self, c: dict, joiner: str = " or ") -> str:
        who = self.names(c.get("entity_id"), joiner)
        return who + (f" attribute {c['attribute']}" if c.get("attribute") else "")

    def _at(self, a) -> str:
        if isinstance(a, dict):
            off = f" {_span(a['offset'])}" if a.get("offset") else ""
            return f"time of {self.name(a.get('entity_id'))}{off}"
        if self._is_entity(a):
            return f"time of {self.name(a)}"
        t = str(a)
        return t[:5] if re.fullmatch(r"\d\d:\d\d(:00)?", t) else t

    # ------------------------------------------------------------------ definition
    def trigger(self, t) -> str:
        if not isinstance(t, dict):
            return _short(t)
        text = self._trigger(str(t.get("trigger") or t.get("platform") or "?"), t)
        if t.get("id") not in (None, ""):
            text += f" [id {t['id']}]"
        if t.get("enabled") is False:
            text += " (disabled)"
        return _short(f"{t['alias']}: {text}" if t.get("alias") else text)

    def _trigger(self, kind: str, t: dict) -> str:
        wait = f" for {_span(t['for'])}" if t.get("for") is not None else ""
        if kind == "time_pattern":
            return time_pattern(t.get("hours"), t.get("minutes"), t.get("seconds"))
        if kind == "time":
            days = f" on {', '.join(str(d).capitalize() for d in as_list(t['weekday']))}" if t.get("weekday") else ""
            return "at " + " or ".join(self._at(a) for a in as_list(t.get("at"))) + days
        if kind == "state":
            text = self._who(t) + " changes"
            if t.get("from") is not None:
                text += f" from {self._values(t['from'])}"
            if t.get("not_from") is not None:
                text += f" not from {self._values(t['not_from'])}"
            if t.get("to") is not None:
                text += f" to {self._values(t['to'])}"
            if t.get("not_to") is not None:
                text += f" not to {self._values(t['not_to'])}"
            return text + wait
        if kind == "numeric_state":
            return f"{self._who(t)} {self._range(t, 'goes ')}{wait}"
        if kind == "sun":
            off = f" {_span(t['offset'])}" if t.get("offset") else ""
            return f"at {t.get('event', 'sunset')}{off}"
        if kind == "homeassistant":
            return f"Home Assistant {t.get('event', 'start')}"
        if kind == "zone":
            verb = "leaves" if t.get("event") == "leave" else "enters"
            return f"{self.names(t.get('entity_id'))} {verb} {self.name(t.get('zone'))}"
        if kind == "template":
            return f"template becomes true: {_short(t.get('value_template', ''), TEMPLATE_MAX)}{wait}"
        if kind == "event":
            return f"event {self._values(t.get('event_type'))}"
        if kind == "calendar":
            off = f" {_span(t['offset'])}" if t.get("offset") else ""
            return f"{self.name(t.get('entity_id'))}: appointment {t.get('event', 'start')}{off}"
        if kind == "device":
            what = self.name(t["entity_id"]) + " " if t.get("entity_id") else ""
            return f"device trigger {what}{t.get('type', '')}".rstrip()
        if kind == "mqtt":
            return f"MQTT message on {t.get('topic', '?')}"
        if kind == "conversation":
            return "voice command " + " or ".join(f"'{c}'" for c in as_list(t.get("command")))
        return f"{kind} trigger"

    @staticmethod
    def _kind(c: dict) -> str:
        if c.get("condition"):
            return str(c["condition"])
        return next((k for k in ("and", "or", "not") if k in c), "?")

    @staticmethod
    def _subs(c: dict, kind: str) -> list:
        return as_list(c.get("conditions") if c.get("conditions") is not None else c.get(kind))

    def condition(self, c) -> str:
        if isinstance(c, str):
            return "template " + _short(c, TEMPLATE_MAX)
        if not isinstance(c, dict):
            return _short(c)
        text = self._condition(self._kind(c), c)
        if c.get("enabled") is False:
            text += " (disabled)"
        return _short(f"{c['alias']}: {text}" if c.get("alias") else text)

    def _condition(self, kind: str, c: dict) -> str:
        wait = f" for {_span(c['for'])}" if c.get("for") is not None else ""
        if kind in ("and", "or"):
            return "(" + f" {kind} ".join(self.condition(x) for x in self._subs(c, kind)) + ")"
        if kind == "not":
            return "not (" + " or ".join(self.condition(x) for x in self._subs(c, kind)) + ")"
        if kind == "state":
            joiner = " or " if c.get("match") == "any" else " and "
            return f"{self._who(c, joiner)} is {self._values(c.get('state'))}{wait}"
        if kind == "numeric_state":
            how = " (computed by a template)" if c.get("value_template") else ""
            return f"{self._who(c, ' and ')}{how} {self._range(c)}"
        if kind == "time":
            out = []
            if c.get("after") and c.get("before"):
                out.append(f"between {self._at(c['after'])} and {self._at(c['before'])}")
            elif c.get("after"):
                out.append(f"after {self._at(c['after'])}")
            elif c.get("before"):
                out.append(f"before {self._at(c['before'])}")
            if c.get("weekday"):
                out.append("on " + ", ".join(str(d).capitalize() for d in as_list(c["weekday"])))
            return " ".join(out) or "any time"
        if kind == "sun":
            out = []
            for edge in ("after", "before"):
                if c.get(edge):
                    off = c.get(f"{edge}_offset")
                    out.append(f"{edge} {c[edge]}" + (f" {_span(off)}" if off else ""))
            return " and ".join(out) or "sun"
        if kind == "zone":
            return f"{self.names(c.get('entity_id'), ' and ')} in {self.names(c.get('zone'))}"
        if kind == "template":
            return "template " + _short(c.get("value_template", ""), TEMPLATE_MAX)
        if kind == "trigger":
            return "triggered by id " + self._values(c.get("id"))
        if kind == "device":
            what = " " + self.name(c["entity_id"]) if c.get("entity_id") else ""
            return f"device condition{what} {c.get('type', '')}".rstrip()
        return f"{kind} condition"

    def conds(self, conditions) -> str:
        return " and ".join(self.condition(c) for c in as_list(conditions)) or "always"

    def action(self, a) -> str:
        if not isinstance(a, dict):
            return _short(a)
        text = "(disabled) " if a.get("enabled") is False else ""
        text += self._action(a)
        return f"{a['alias']}: {text}" if a.get("alias") else text

    def steps(self, seq) -> str:
        return "; ".join(self.action(a) for a in as_list(seq)) or "nothing"

    def option(self, k: int, o: dict) -> str:
        """Name of a choose option: its alias, or its number."""
        return f"'{o['alias']}'" if isinstance(o, dict) and o.get("alias") else f"option {k}"

    def _action(self, a: dict) -> str:
        if "choose" in a:
            out = [f"if {self.conds(o.get('conditions'))} → {self.steps(o.get('sequence'))}"
                   for o in as_list(a["choose"]) if isinstance(o, dict)]
            if a.get("default") is not None:
                out.append(f"otherwise → {self.steps(a['default'])}")
            return "; ".join(out)
        if "if" in a:
            text = f"if {self.conds(a['if'])} → {self.steps(a.get('then'))}"
            return text + (f"; otherwise → {self.steps(a['else'])}" if a.get("else") is not None else "")
        if "repeat" in a:
            r = a["repeat"] if isinstance(a["repeat"], dict) else {}
            how = (f"{r['count']} times" if "count" in r else f"while {self.conds(r['while'])}" if "while" in r
                   else f"until {self.conds(r['until'])}" if "until" in r else "for each item")
            return f"repeat {how}: {self.steps(r.get('sequence'))}"
        if "parallel" in a:
            return "in parallel: " + self.steps(a["parallel"])
        if "sequence" in a:
            return self.steps(a["sequence"])
        if "condition" in a or any(k in a for k in ("and", "or", "not")):
            return "continue only if " + self.condition(a)
        service = a.get("action") if isinstance(a.get("action"), str) else a.get("service")
        if isinstance(service, str):
            return self._service(service, a)
        if "delay" in a:
            return f"wait {_span(a['delay'])}"
        if "wait_template" in a:
            return "wait until " + _short(a["wait_template"], TEMPLATE_MAX)
        if "wait_for_trigger" in a:
            return "wait for " + " or ".join(self.trigger(t) for t in as_list(a["wait_for_trigger"]))
        if "scene" in a:
            return f"scene {self.name(a['scene'])}"
        if "event" in a:
            return f"fire event {a['event']}"
        if "stop" in a:
            return f"stop ({a['stop']})" if a.get("stop") else "stop"
        if "variables" in a:
            return "set variables " + ", ".join(map(str, a["variables"] or {}))
        if "device_id" in a:
            what = " " + self.name(a["entity_id"]) if a.get("entity_id") else ""
            return f"device action{what} {a.get('type', '')}".rstrip()
        return _short(a, TEMPLATE_MAX)

    def _service(self, service: str, a: dict) -> str:
        target = a.get("target") if isinstance(a.get("target"), dict) else {}
        data = a.get("data") or a.get("data_template") or {}
        data = data if isinstance(data, dict) else {}
        ents = as_list(target.get("entity_id")) + as_list(a.get("entity_id")) + as_list(data.get("entity_id"))
        who = [self.name(e) for e in ents]
        for key, label in (("area_id", "area"), ("device_id", "device"), ("floor_id", "floor"), ("label_id", "label")):
            who += [f"{label} {x}" for x in as_list(target.get(key)) + as_list(a.get(key))]
        extra = [f"{k}={_short(v, 40)}" for k, v in data.items() if k != "entity_id"]
        text = service + (" " + ", ".join(who) if who else "")
        if extra:
            text += " (" + ", ".join(extra[:3]) + (", …" if len(extra) > 3 else "") + ")"
        return text

    def action_lines(self, actions) -> list:
        """One line per top-level step; a choose step one line per option."""
        lines = []
        for a in as_list(actions):
            if isinstance(a, dict) and "choose" in a and a.get("enabled") is not False:
                head = f"{a['alias']}: " if a.get("alias") else ""
                for k, o in enumerate(as_list(a["choose"]), 1):
                    if isinstance(o, dict):
                        lines.append(_short(f"{head}{self.option(k, o)}: if {self.conds(o.get('conditions'))} → "
                                            f"{self.steps(o.get('sequence'))}"))
                if a.get("default") is not None:
                    lines.append(_short(f"{head}otherwise → {self.steps(a['default'])}"))
            else:
                lines.append(_short(self.action(a)))
        return lines

    def definition(self, config: dict, automation: bool = True) -> dict:
        out = {}
        # the whole description, not cut like the other lines: it often says what the automation is for
        if str(config.get("description") or "").strip():
            out["description"] = str(config["description"]).strip()
        if config.get("use_blueprint") and not any(x is not None for x in parts(config)):
            bp = config["use_blueprint"] if isinstance(config["use_blueprint"], dict) else {}
            inputs = ", ".join(f"{k}={_short(v, 40)}" for k, v in (bp.get("input") or {}).items())
            return {**out, "blueprint": f"{bp.get('path', '?')}" + (f" with {inputs}" if inputs else "")}
        triggers, conditions, actions = parts(config)
        if automation:
            out["triggers"] = _cap([self.trigger(t) for t in as_list(triggers)], DEF_LINES["triggers"]) or "none"
            out["conditions"] = _cap([self.condition(c) for c in as_list(conditions)],
                                     DEF_LINES["conditions"]) or "none"
        out["actions"] = _cap(self.action_lines(actions), DEF_LINES["actions"]) or "none"
        return out

    # ------------------------------------------------------------------ evaluation now
    def check(self, c, indent: str = ""):
        """(True/False/None, lines) of one condition against the current states; None = cannot be told now."""
        if isinstance(c, str):
            c = {"condition": "template", "value_template": c}
        if not isinstance(c, dict):
            return None, [f"{indent}{_short(c)}: not evaluated → unknown"]
        desc = self.condition(c)
        if c.get("enabled") is False:
            return True, [f"{indent}{desc}: skipped"]
        kind = self._kind(c)
        if kind in ("and", "or", "not"):
            results, lines = [], []
            for sub in self._subs(c, kind):
                r, sub_lines = self.check(sub, indent + "  ")
                results.append(r)
                lines += sub_lines
            if kind == "and":
                res = False if False in results else (None if None in results else True)
            else:
                res = True if True in results else (None if None in results else False)
                res = (None if res is None else not res) if kind == "not" else res
            head = {"and": "all of", "or": "any of", "not": "none of"}[kind]
            label = f"{c['alias']}: {head}" if c.get("alias") else head
            return res, [f"{indent}{label} → {ANSWER[res]}"] + lines
        try:
            res, detail = getattr(self, f"_check_{kind}", self._check_other)(c)
        except Exception as e:  # noqa: BLE001  a strange definition must not break the answer
            res, detail = None, f"not evaluated ({e})"
        return res, [f"{indent}{desc}: {detail} → {ANSWER[res]}"]

    def check_all(self, conditions, indent: str = ""):
        results, lines = [], []
        for c in as_list(conditions):
            r, sub = self.check(c, indent)
            results.append(r)
            lines += sub
        res = False if False in results else (None if None in results else True)
        return res, lines

    def _value(self, eid: str, attribute=None):
        s = self.states.get(str(eid))
        if s is None:
            return None, False
        return (s.get("attributes", {}).get(attribute) if attribute else s.get("state")), True

    def _since(self, eid: str):
        s = self.states.get(str(eid)) or {}
        return _parse_time(s.get("last_changed") or s.get("last_updated") or "")

    def _check_state(self, c: dict):
        wanted = []
        for w in as_list(c.get("state")):  # the state can also be that of a helper, e.g. input_select.mode
            wanted.append(self.states[w].get("state") if self._is_entity(w) and w in self.states else w)
        secs = seconds_of(c["for"]) if c.get("for") is not None else 0
        results, details = [], []
        for e in as_list(c.get("entity_id")):
            v, found = self._value(e, c.get("attribute"))
            if not found:
                results.append(False)
                details.append(f"{e} not found")
                continue
            ok = any(v == w or str(v) == str(w) for w in wanted)
            detail = f"now {v}"
            if ok and c.get("for") is not None:
                since = self._since(e)
                if secs is None or since is None:
                    ok = None
                else:
                    age = self.now.timestamp() - since.timestamp()
                    ok = age >= secs
                    detail += f" for {_duration(age)}"
            results.append(ok)
            details.append(detail if len(as_list(c.get("entity_id"))) == 1 else f"{self.name(e)} {detail}")
        if c.get("match") == "any":
            res = True if True in results else (None if None in results else False)
        else:
            res = False if False in results or not results else (None if None in results else True)
        return res, ", ".join(details)

    def _threshold(self, v):
        """(number or None, text) of a numeric_state limit; another entity is read from its current state."""
        if v is None:
            return None, ""
        if self._is_entity(v):
            state, _ = self._value(v)
            f = _float(state)
            return f, f"{self.name(v)} now {state}"
        return _float(v), ""

    def _check_numeric_state(self, c: dict):
        if c.get("value_template"):
            return None, "computed by a template, not evaluated"
        unit = self._unit(c)
        above, above_text = self._threshold(c.get("above"))
        below, below_text = self._threshold(c.get("below"))
        if (c.get("above") is not None and above is None) or (c.get("below") is not None and below is None):
            return False, "limit has no number: " + ", ".join(t for t in (above_text, below_text) if t)
        results, details = [], []
        ents = as_list(c.get("entity_id"))
        for e in ents:
            v, found = self._value(e, c.get("attribute"))
            f = _float(v)
            if f is None:
                results.append(False)
                details.append(f"{self.name(e)} " * (len(ents) > 1) + (f"now {v}" if found else "not found"))
                continue
            results.append((above is None or f > above) and (below is None or f < below))
            details.append(f"{self.name(e)} " * (len(ents) > 1) + f"now {_val(f, unit)}")
        details += [t for t in (above_text, below_text) if t]
        return (bool(results) and all(results)), ", ".join(details)

    def _clock(self, v):
        """(time of day, ok) for after/before of a time condition: 'HH:MM[:SS]', input_datetime or timestamp
        sensor; ok False if it cannot be read."""
        if v is None:
            return None, True
        if self._is_entity(v):
            state, found = self._value(v)
            if not found or state in (None, "unknown", "unavailable"):
                return None, False
            t = _parse_time(state)
            if t is not None:
                return t.astimezone(self.tz).time(), True
            v = str(state)[-8:]  # input_datetime: 'HH:MM:SS' or 'YYYY-MM-DD HH:MM:SS'
        try:
            return dtime.fromisoformat(str(v).strip()), True
        except ValueError:
            return None, False

    def _check_time(self, c: dict):
        after, ok_a = self._clock(c.get("after"))
        before, ok_b = self._clock(c.get("before"))
        now = self.now
        detail = f"now {WEEKDAYS[now.weekday()].capitalize()} {now:%H:%M}"
        if not (ok_a and ok_b):
            return None, detail + ", time of the helper unknown"
        t = now.time().replace(tzinfo=None)
        lo, hi = after or dtime(0), before or dtime(23, 59, 59, 999999)
        ok = (lo <= t < hi) if lo < hi else not (hi <= t < lo)
        if c.get("weekday"):
            ok = ok and WEEKDAYS[now.weekday()] in [str(d).lower()[:3] for d in as_list(c["weekday"])]
        return ok, detail

    def _sun_today(self, attr: str):
        """(today's sunrise or sunset, exact) from sun.sun. It only has the next ones: one already past today is
        estimated as the next one 24 hours earlier (in UTC, so a DST change does not shift it), a few minutes off."""
        t = _parse_time((self.states.get("sun.sun") or {}).get("attributes", {}).get(attr))
        if t is None:
            return None, False
        if t.astimezone(self.tz).date() == self.now.date():
            return t.astimezone(self.tz), True
        t = (t.astimezone(timezone.utc) - timedelta(days=1)).astimezone(self.tz)
        return (t, False) if t.date() == self.now.date() else (None, False)

    def _check_sun(self, c: dict):
        events = {"sunrise": self._sun_today("next_rising"), "sunset": self._sun_today("next_setting")}
        wanted, estimated = {}, []
        for edge in ("after", "before"):
            if c.get(edge):
                ev, exact = events.get(str(c[edge]), (None, False))
                off = seconds_of(c.get(f"{edge}_offset") or 0)
                if ev is None or off is None:
                    return None, "sun times not available (sun.sun)"
                wanted[edge] = ev + timedelta(seconds=off)
                if not exact:
                    estimated.append(wanted[edge])
        now = self.now
        detail = f"now {now:%H:%M}, " + ", ".join(f"{k} {'about ' * (not ok)}{v:%H:%M}"
                                                  for k, (v, ok) in events.items() if v)
        if any(abs(now.timestamp() - t.timestamp()) < SUN_MARGIN for t in estimated):
            return None, detail + ", too close to the estimated time"
        if c.get("before") == "sunrise" and c.get("after") == "sunset":  # Home Assistant: the night, across midnight
            return now <= wanted["before"] or now >= wanted["after"], detail
        ok = (("before" not in wanted or now <= wanted["before"])
              and ("after" not in wanted or now >= wanted["after"]))
        return ok, detail

    def _check_zone(self, c: dict):
        """Like Home Assistant: the position (latitude/longitude) within the radius of one of the zones, the GPS
        accuracy counted in its favour. Without a position an entity is in no zone, whatever its state says."""
        zones = [self.states.get(str(z)) for z in as_list(c.get("zone"))]
        results, details = [], []
        for e in as_list(c.get("entity_id")):
            s = self.states.get(str(e))
            a = (s or {}).get("attributes", {})
            lat, lon = _float(a.get("latitude")), _float(a.get("longitude"))
            if s is None or lat is None or lon is None:
                results.append(False)
                details.append(f"{self.name(e)} has no position" + (f" (state {s.get('state')})" if s else ""))
                continue
            accuracy = _float(a.get("gps_accuracy")) or 0.0
            inside = False
            for z in zones:
                za = (z or {}).get("attributes", {})
                zlat, zlon, radius = _float(za.get("latitude")), _float(za.get("longitude")), _float(za.get("radius"))
                if z and z.get("state") != "unavailable" and None not in (zlat, zlon, radius):
                    inside = inside or _distance(lat, lon, zlat, zlon) - accuracy < radius
            results.append(inside)
            details.append(f"{self.name(e)} now {s.get('state')}")
        return bool(results) and all(results), ", ".join(details)

    def _check_template(self, c: dict):
        tpl = str(c.get("value_template", ""))
        if "trigger" in tpl:
            return None, "depends on the trigger"
        if self.render is None:
            return None, "not evaluated"
        try:
            out = self.render(tpl)
        except Exception as e:  # noqa: BLE001  e.g. variables of the automation, which only exist in a run
            return None, f"cannot be evaluated now ({_short(e, 80)})"
        return _is_true(out), f"now {_short(out, 40)}"

    @staticmethod
    def _check_trigger(c: dict):
        return None, "depends on which trigger fired"

    @staticmethod
    def _check_other(c: dict):
        return None, "not evaluated"

    @staticmethod
    def _gate(mode: str, current, max_runs) -> str:
        """What a new trigger does while runs are going on (mode single, restart, queued, parallel)."""
        running = int(_float(current) or 0)
        if not running:
            return ""
        mode = mode or "single"
        if mode == "single":
            return "It is running right now (mode single): a new trigger is ignored until that run ends."
        if mode == "restart":
            return "It is running right now (mode restart): a new trigger stops that run and starts again."
        limit = int(_float(max_runs) or 10)
        if running >= limit:
            return f"{running} runs are going on (mode {mode}, at most {limit}): a new trigger is ignored."
        if mode == "queued":
            return "It is running right now (mode queued): a new run waits until the current one ends."
        return ""

    def evaluate(self, config: dict, enabled: bool = True, mode: str = "", current=0, max_runs=None) -> dict:
        """The conditions with their current values, the choose/if branches and what would happen, in order, if
        it were triggered now."""
        _, conditions, actions = parts(config)
        out = {}
        res = True
        if as_list(conditions):
            res, lines = self.check_all(conditions)
            out["conditions"] = _cap(lines, NOW_LINES)
            out["conditions_met"] = ANSWER[res]
        branch_lines = []
        if res is False:
            decision = ["No: the conditions are not met, no action would run."]
        else:
            done, _ = self._simulate(actions, branch_lines)
            decision = ["; ".join(done) or "Nothing: it has no actions."]
            if res is None:
                decision.insert(0, "Only if the conditions marked unknown are met:")
        if branch_lines:
            out["branches"] = _cap(branch_lines, NOW_LINES)
        gate = self._gate(mode, current, max_runs)
        if gate:
            decision.insert(0, gate)
        if not enabled:
            decision.insert(0, "The automation is turned off: it does not run at all.")
        out["if_triggered_now"] = _short(" ".join(decision), 2 * TEXT_MAX)
        return out

    def _simulate(self, seq, branch_lines: list, depth: int = 0):
        """([what each step would do, in order], stopped) of a sequence now; follows the chosen choose/if branch
        and ends at a stop or an unmet condition step."""
        done = []
        for a in as_list(seq):
            if not isinstance(a, dict) or a.get("enabled") is False:
                continue
            if depth > 5:
                done.append(_short(self.action(a), 100))
            elif "choose" in a:
                text, picked = self._choose_now(a, branch_lines)
                if picked is None:
                    done.append(text)
                    continue
                sub, stopped = self._simulate(picked, branch_lines, depth + 1)
                done.append(f"{text} → {', '.join(sub) or 'nothing'}")
                if stopped:
                    return done, True
            elif "if" in a:
                r, lines = self.check_all(a["if"], "  ")
                branch_lines += [f"if → {ANSWER[r]}"] + lines
                other = a.get("else")
                if r is None:
                    done.append(f"if unknown now: either {self.steps(a.get('then'))}, or "
                                f"{self.steps(other) if other is not None else 'nothing'}")
                    continue
                sub, stopped = self._simulate(a.get("then") if r else other, branch_lines, depth + 1)
                done.append(f"if {ANSWER[r]} → {', '.join(sub) or 'nothing'}")
                if stopped:
                    return done, True
            elif "condition" in a or any(k in a for k in ("and", "or", "not")):
                r, lines = self.check(a, "  ")
                branch_lines += ["condition step:"] + lines
                if r is False:
                    done.append(f"stops at '{self.condition(a)}': not met")
                    return done, True
                if r is None:
                    done.append(f"continues only if {self.condition(a)} (unknown now)")
            elif "stop" in a:
                done.append(self.action(a))
                return done, True
            elif "sequence" in a and "repeat" not in a:
                sub, stopped = self._simulate(a["sequence"], branch_lines, depth + 1)
                done += sub
                if stopped:
                    return done, True
            else:
                done.append(self.action(a))
        return done, False

    def _choose_now(self, a: dict, lines: list):
        """(text, sequence of the option that would run, or None) of a choose step now."""
        unknown = []
        for k, o in enumerate(as_list(a["choose"]), 1):
            if not isinstance(o, dict):
                continue
            label = self.option(k, o)
            r, sub = self.check_all(o.get("conditions"), "  ")
            lines += [f"{label} → {ANSWER[r]}"] + sub
            if r:
                unless = f" (unless {', '.join(unknown)} applies: unknown now)" if unknown else ""
                return label + unless, o.get("sequence")
            if r is None:
                unknown.append(label)
        unless = f" (unless {', '.join(unknown)} applies: unknown now)" if unknown else ""
        if a.get("default") is not None:
            return "no option matches, otherwise" + unless, a["default"]
        return "no option matches → nothing from this step" + unless, None

    # ------------------------------------------------------------------ traces
    def _start(self, summary: dict):
        t = _parse_time((summary.get("timestamp") or {}).get("start") or "")
        return t.astimezone(self.tz) if t else None

    @staticmethod
    def _outcome(summary: dict) -> str:
        if summary.get("state") == "running":
            return "still running"
        ex = summary.get("script_execution") or ""
        return EXECUTION.get(ex, ex or summary.get("state") or "?")

    def run_line(self, summary: dict) -> str:
        t = self._start(summary)
        when = _when(t, self.now) if t else "?"
        trig = f"{summary['trigger']}: " if summary.get("trigger") else ""
        return f"{when}: {trig}{self._outcome(summary)}"

    def run(self, summary: dict, trace: dict | None, config: dict | None) -> dict:
        """The most recent run: when, what triggered, the result, and from the trace the global conditions, then in
        order the steps that were reached, the branch conditions with the values they saw and the branch taken."""
        t = self._start(summary)
        out = {"time": _when(t, self.now) if t else "?"}
        if summary.get("trigger"):
            out["triggered_by"] = summary["trigger"]
        out["result"] = self._outcome(summary)
        if summary.get("error"):
            out["error"] = _short(summary["error"])
        steps = (trace or {}).get("trace") or {}
        if not isinstance(steps, dict) or not steps:
            return out
        config = config or {}
        lines = []
        for path in sorted((p for p in steps if _COND_PATH.match(p)), key=_path_key):
            lines.append(self._condition_result(path, steps, config))
        if lines:
            out["conditions"] = _cap(lines, RUN_LINES)
        reached = []
        for path, els in steps.items():
            if _BRANCH_COND.search(path):
                line = self._condition_result(path, steps, config)
            elif _STEP_PATH.search(path):
                line = self._step_result(_node(config, path), (els or [{}])[-1])
            else:
                continue
            if line:
                reached.append((str((els or [{}])[0].get("timestamp") or ""), _path_key(path), line))
        if reached:
            out["steps"] = _cap([line for *_, line in sorted(reached)], RUN_LINES)
        return out

    @staticmethod
    def _seen(steps: dict, path: str) -> str:
        """The values a condition saw in the run, from its sub-results in the trace (e.g. 'state': 0.8)."""
        seen = []
        for p, els in steps.items():
            if p.startswith(path + "/"):
                r = (els or [{}])[-1].get("result") or {}
                if "state" in r:
                    seen.append(str(r["state"]))
        return ", ".join(seen[:3])

    def _condition_result(self, path: str, steps: dict, config: dict) -> str:
        """'<option>: <condition>: passed/failed (then: <value>)' of one condition in a trace."""
        node = _node(config, path)
        desc = self.condition(node) if node is not None else path
        found = re.search(r"choose/(\d+)/conditions?/\d+$", path)
        if found:
            option = _node(config, path[:found.start()] + f"choose/{found[1]}")
            desc = f"{self.option(int(found[1]) + 1, option)}: {desc}"
        elif re.search(r"(?:^|/)if/", path) and _BRANCH_COND.search(path):
            desc = f"if: {desc}"
        el = (steps[path] or [{}])[-1]
        ok = (el.get("result") or {}).get("result")
        text = f"{desc}: " + ("error " + _short(el["error"], 80) if el.get("error") else "passed" if ok else "failed")
        seen = self._seen(steps, path)
        return _short(text + (f" (then: {seen})" if seen else ""))

    def _step_result(self, step, el: dict) -> str:
        """One step that was reached in a run; empty for a plain sequence, whose steps follow on their own."""
        step = step if isinstance(step, dict) else {}
        result, error = el.get("result") or {}, el.get("error")
        if error:
            return f"{_short(self.action(step), 100)}: error {_short(error, 80)}"
        if "choose" in step:
            choice, opts = result.get("choice"), as_list(step["choose"])
            if choice == "default":
                return "choose: no option matched → otherwise"
            if isinstance(choice, int) and 0 <= choice < len(opts):
                return f"choose: {self.option(choice + 1, opts[choice])}"
            return "choose: no option matched → nothing"
        if "if" in step:
            return {"then": "if: yes", "else": "if: no"}.get(result.get("choice"), "if: no → nothing")
        if "condition" in step or any(k in step for k in ("and", "or", "not")):
            return f"{self.condition(step)}: " + ("passed" if result.get("result") else "not met, stopped here")
        if not step or "parallel" in step or ("sequence" in step and "repeat" not in step):
            return ""
        return _short(self.action(step), 120)


def _path_key(path: str) -> tuple:
    """Trace paths in natural order: 'action/2' before 'action/10'."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in path.split("/"))


def _node(config: dict, path: str):
    """The part of the definition a trace path points to, e.g. 'action/0/choose/1/conditions/0'; None if the
    definition does not have it."""
    segs = path.split("/")
    _, conditions, actions = parts(config or {})
    if segs[0] in ("action", "actions", "sequence"):
        obj = as_list(actions)
    elif segs[0] in ("condition", "conditions"):
        obj = as_list(conditions)
    else:
        return None
    for seg in segs[1:]:
        if seg.isdigit():
            if not isinstance(obj, list) or int(seg) >= len(obj):
                return None
            obj = obj[int(seg)]
        elif isinstance(obj, list):
            continue  # 'if/condition/0': the conditions of 'if' are the list itself
        elif isinstance(obj, dict):
            key = next((k for k in _ALIASES.get(seg, (seg,)) if k in obj), None)
            if key is None:
                return None
            obj = as_list(obj[key]) if seg in _LIST_KEYS else obj[key]
        else:
            return None
    return obj


def _distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371008.8 * math.asin(math.sqrt(min(1.0, a)))
