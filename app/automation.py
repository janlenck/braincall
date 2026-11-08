"""Automations and scripts explained for the phone (tool ha_automation): the definition condensed into a few lines
with friendly names, the conditions checked against the current states (which branch would run now), and the
last runs from the traces (what triggered, which condition failed, which branch ran)."""
import re
from datetime import datetime, time as dtime, timedelta, timezone

from ha import _duration, _float, _val, _when

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# The most lines per part of the definition, of the evaluation and of the steps of a run; runs listed in total
DEF_LINES = {"triggers": 5, "conditions": 6, "actions": 8}
NOW_LINES = 14
RUN_LINES = 8
RUNS = 5
TEXT_MAX = 200
TEMPLATE_MAX = 90
TRUE_WORDS = ("true", "yes", "on", "enable")
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
_COND_PATH = re.compile(r"^conditions?/(\d+)$")
_STEP_PATH = re.compile(r"^(?:actions?|sequence)/(\d+)$")


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


def _truthy(text) -> bool:
    """Result of a template condition like Home Assistant reads it."""
    t = str(text).strip().lower()
    f = _float(t)
    return (f != 0) if f is not None else t in TRUE_WORDS


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
        if config.get("use_blueprint") and not any(x is not None for x in parts(config)):
            bp = config["use_blueprint"] if isinstance(config["use_blueprint"], dict) else {}
            inputs = ", ".join(f"{k}={_short(v, 40)}" for k, v in (bp.get("input") or {}).items())
            return {"blueprint": f"{bp.get('path', '?')}" + (f" with {inputs}" if inputs else "")}
        triggers, conditions, actions = parts(config)
        out = {}
        if config.get("description"):
            out["description"] = _short(config["description"])
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
        """Today's sunrise or sunset (local) from sun.sun: next_rising/next_setting, a day earlier if already past."""
        t = _parse_time((self.states.get("sun.sun") or {}).get("attributes", {}).get(attr))
        if t is None:
            return None
        t = t.astimezone(self.tz)
        return t if t.date() == self.now.date() else t - timedelta(days=1)

    def _check_sun(self, c: dict):
        events = {"sunrise": self._sun_today("next_rising"), "sunset": self._sun_today("next_setting")}
        wanted = {}
        for edge in ("after", "before"):
            if c.get(edge):
                ev = events.get(str(c[edge]))
                off = seconds_of(c.get(f"{edge}_offset") or 0)
                if ev is None or off is None:
                    return None, "sun times not available (sun.sun)"
                wanted[edge] = ev + timedelta(seconds=off)
        now = self.now
        detail = f"now {now:%H:%M}, " + ", ".join(f"{k} {v:%H:%M}" for k, v in events.items() if v)
        if c.get("before") == "sunrise" and c.get("after") == "sunset":  # Home Assistant: the night, across midnight
            return now <= wanted["before"] or now >= wanted["after"], detail
        ok = (("before" not in wanted or now <= wanted["before"])
              and ("after" not in wanted or now >= wanted["after"]))
        return ok, detail

    def _check_zone(self, c: dict):
        zones = {}
        for z in as_list(c.get("zone")):
            zones[str(z)] = "home" if z == "zone.home" else self.name(z)
        results, details = [], []
        for e in as_list(c.get("entity_id")):
            v, _ = self._value(e)
            results.append(v in zones.values())
            details.append(f"{self.name(e)} now {v}")
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
        return _truthy(out), f"now {_short(out, 40)}"

    @staticmethod
    def _check_trigger(c: dict):
        return None, "depends on which trigger fired"

    @staticmethod
    def _check_other(c: dict):
        return None, "not evaluated"

    def evaluate(self, config: dict, enabled: bool = True) -> dict:
        """The conditions with their current values, the choose/if branches and what would happen if it were
        triggered now."""
        _, conditions, actions = parts(config)
        out = {}
        res = True
        if as_list(conditions):
            res, lines = self.check_all(conditions)
            out["conditions"] = _cap(lines, NOW_LINES)
            out["conditions_met"] = ANSWER[res]
        branch_lines, decision = [], []
        if res is False:
            decision.append("No: the conditions are not met, no action would run.")
        else:
            for a in as_list(actions):
                if not isinstance(a, dict) or a.get("enabled") is False:
                    continue
                if "choose" in a:
                    decision.append(self._choose_now(a, branch_lines))
                elif "if" in a:
                    r, lines = self.check_all(a["if"], "  ")
                    branch_lines += [f"if → {ANSWER[r]}"] + lines
                    then = self.steps(a.get("then"))
                    other = self.steps(a.get("else")) if a.get("else") is not None else "nothing"
                    decision.append({True: f"if: yes → {then}", False: f"if: no → {other}",
                                     None: f"if: unknown → {then}, or else {other}"}[r])
                elif "condition" in a or any(k in a for k in ("and", "or", "not")):
                    r, lines = self.check(a, "  ")
                    branch_lines += ["condition step:"] + lines
                    if r is False:
                        decision.append(f"Stops at the step '{self.condition(a)}': not met.")
                        break
                    if r is None:
                        decision.append(f"Step '{self.condition(a)}' unknown: it stops there if not met.")
            if not decision:
                decision.append("Would run its actions: " + _short(self.steps(actions)))
            if res is None:
                decision.insert(0, "Only if the conditions marked unknown are met:")
        if branch_lines:
            out["branches"] = _cap(branch_lines, NOW_LINES)
        if not enabled:
            decision.insert(0, "The automation is turned off: it does not run at all.")
        out["if_triggered_now"] = " ".join(decision)
        return out

    def _choose_now(self, a: dict, lines: list) -> str:
        unknown = []
        for k, o in enumerate(as_list(a["choose"]), 1):
            if not isinstance(o, dict):
                continue
            label = self.option(k, o)
            r, sub = self.check_all(o.get("conditions"), "  ")
            lines += [f"{label} → {ANSWER[r]}"] + sub
            if r:
                text = f"{label} → {self.steps(o.get('sequence'))}"
                return text + (f" (unless {', '.join(unknown)} applies: unknown now)" if unknown else "")
            if r is None:
                unknown.append(label)
        text = ("no option matches → " + (self.steps(a["default"]) if a.get("default") is not None
                                          else "nothing from this step"))
        return text + (f" (unless {', '.join(unknown)} applies: unknown now)" if unknown else "")

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
        """The most recent run: when, what triggered, the result, and from the trace each condition and branch."""
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
        _, conditions, actions = parts(config or {})
        conditions, actions = as_list(conditions), as_list(actions)
        lines = []
        for path in sorted((p for p in steps if _COND_PATH.match(p)), key=lambda p: int(_COND_PATH.match(p)[1])):
            i = int(_COND_PATH.match(path)[1])
            desc = self.condition(conditions[i]) if i < len(conditions) else path
            el = (steps[path] or [{}])[-1]
            ok = (el.get("result") or {}).get("result")
            text = f"{desc}: " + ("error " + _short(el["error"], 80) if el.get("error")
                                  else "passed" if ok else "failed")
            seen = self._seen(steps, path)
            lines.append(text + (f" (then: {seen})" if seen else ""))
        if lines:
            out["conditions"] = _cap(lines, RUN_LINES)
        lines = []
        for path in sorted((p for p in steps if _STEP_PATH.match(p)), key=lambda p: int(_STEP_PATH.match(p)[1])):
            i = int(_STEP_PATH.match(path)[1])
            step = actions[i] if i < len(actions) and isinstance(actions[i], dict) else {}
            el = (steps[path] or [{}])[-1]
            lines.append(self._step_result(step, el.get("result") or {}, el.get("error")))
        if lines:
            out["steps"] = _cap(lines, RUN_LINES)
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

    def _step_result(self, step: dict, result: dict, error) -> str:
        if error:
            return f"{_short(self.action(step), 100)}: error {_short(error, 80)}"
        if "choose" in step:
            choice = result.get("choice")
            opts = as_list(step["choose"])
            if choice == "default":
                return f"no option matched → otherwise: {self.steps(step.get('default'))}"
            if isinstance(choice, int) and 0 <= choice < len(opts) and isinstance(opts[choice], dict):
                return _short(f"{self.option(choice + 1, opts[choice])} ran → "
                              f"{self.steps(opts[choice].get('sequence'))}")
            return "no option matched → nothing from this step"
        if "if" in step:
            choice = result.get("choice")
            if choice == "then":
                return _short(f"if: yes → {self.steps(step.get('then'))}")
            if choice == "else":
                return _short(f"if: no → {self.steps(step.get('else'))}")
            return "if: no → nothing"
        if "condition" in step or any(k in step for k in ("and", "or", "not")):
            return f"{self.condition(step)}: " + ("passed" if result.get("result") else "not met, stopped here")
        return "ran: " + _short(self.action(step), 120) if step else "ran"
