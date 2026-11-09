"""Planned calls with a task (#51): the task store, what may be dialed and when, and the texts Claude gets.

A task is planned by an admin (on the phone with the tool plan_call, or via POST /tasks), called by the scheduler
in main.py, followed up without the callee (Brain.follow_up) and ends with a structured result.
Everything is kept in <log_dir>/tasks.json, so tasks survive a restart."""
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import config

log = logging.getLogger("tasks")

PLANNED, RUNNING = "planned", "running"
FINAL = ("done", "partial", "failed", "not_reached", "cancelled")
KEEP_DAYS = 30            # finished tasks stay in tasks.json this long
DEFAULT_WINDOW_H = 24     # a task without not_after is given up this long after not_before
MAX_ITEMS, MAX_TEXT = 12, 500

# What the follow-up after the call may use, if the confirmed plan names it ("web" = web_search and web_fetch)
FOLLOW_TOOLS = ("ha_create_event", "ha_events", "shopping_edit", "shopping_list", "web")

TOOL_PLAN_CALL = {
    "name": "plan_call",
    "description": "Plans a phone call that you make later on your own with a task (e.g. agree on an appointment, "
                   "reserve a table) and then work through: you call, hold the conversation, do the follow-up "
                   "steps and report the result. The line is busy during this call, so the planned call starts "
                   "after it. Call it first without confirmed: the phone system checks the plan and tells you what "
                   "to read back. Only after the caller said yes, call it again with the same values and "
                   "confirmed=true.",
    "input_schema": {
        "type": "object",
        "properties": {
            "number": {"type": "string", "description": "phone number to call, complete with area code"},
            "name": {"type": "string", "description": "person or business to call"},
            "task": {"type": "string", "description": "the whole task in the requester's words"},
            "goal": {"type": "string", "description": "what the call should achieve, one sentence"},
            "questions": {"type": "array", "items": {"type": "string"}, "description": "what to ask the callee"},
            "may_share": {"type": "array", "items": {"type": "string"},
                          "description": "the only personal details you may give the callee, e.g. 'name Jan Lenck', "
                                         "'table for 4', 'callback number 0151…'"},
            "may_accept": {"type": "string",
                           "description": "what you may agree to on your own, e.g. 'a table between 18:00 and "
                                          "21:00 today'; anything else is left open and reported"},
            "calendar_from": {"type": "string", "description": "YYYY-MM-DD: with calendar_to, the callee may hear "
                                                               "when the owner is free or busy in these days "
                                                               "(no details)"},
            "calendar_to": {"type": "string", "description": "YYYY-MM-DD, last day (inclusive)"},
            "book_title": {"type": "string", "description": "if set, one appointment with this title may be booked "
                                                            "during the call into a free time of those days"},
            "follow_up": {
                "type": "array",
                "description": "steps you do after the call, without the callee, with the answers collected",
                "items": {"type": "object",
                          "properties": {"step": {"type": "string"},
                                         "tool": {"type": "string", "enum": list(FOLLOW_TOOLS)}},
                          "required": ["step"]},
            },
            "not_before": {"type": "string", "description": "earliest time to call, YYYY-MM-DDTHH:MM local time; "
                                                            "default now"},
            "not_after": {"type": "string", "description": "give up after this time; default 24 hours later"},
            "retries": {"type": "integer", "description": "further attempts if nobody is reached, default 2"},
            "leave_message": {"type": "boolean", "description": "leave a short message on a mailbox, default true"},
            "call_back": {"type": "boolean", "description": "call the requester back with the result (a notification "
                                                            "is sent in any case)"},
            "language": {"type": "string", "enum": ["de", "uk", "en", "auto"]},
            "override_do_not_call": {"type": "boolean", "description": "only if the requester explicitly wants to "
                                                                       "call a number that asked not to be called"},
            "confirmed": {"type": "boolean", "description": "only true after the caller said yes to the plan"},
        },
        "required": ["number", "task"],
    },
}

TOOL_TASK_RESULT = {
    "name": "task_result",
    "description": "Reports the final result of the task. Call it exactly once, as the last step.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["done", "partial", "failed", "not_reached"],
                       "description": "done = goal reached and all follow-up steps done; partial = something is "
                                      "open; failed = the callee declined or it did not work; not_reached = nobody "
                                      "answered in person (mailbox, no reply)"},
            "summary": {"type": "string", "description": "two to four sentences for the requester, plain text"},
            "answers": {"type": "object", "description": "what the callee said, e.g. {\"date\": \"2026-11-14 18:00\"}",
                        "additionalProperties": {"type": "string"}},
            "steps": {"type": "array",
                      "items": {"type": "object",
                                "properties": {"step": {"type": "string"}, "done": {"type": "boolean"},
                                               "result": {"type": "string"}},
                                "required": ["step", "done"]}},
            "open_points": {"type": "array", "items": {"type": "string"}},
            "do_not_call": {"type": "boolean", "description": "the callee asked not to be called again"},
        },
        "required": ["status", "summary"],
    },
}


# ---------------------------------------------------------------------- what may be dialed, and when
def number_refusal(cfg, number: str) -> str:
    """Why this number is never dialed (emergency, premium rate, outside outbound_allowed_regex); "" = fine."""
    n = config.norm_number(number, cfg.country_code)
    if not re.sub(r"\D", "", n):
        return "no phone number"
    forms = {n}
    if n != config.norm_number(number, ""):  # written with the country prefix: +49 110 is 110 as well
        forms.add(n[1:])
    for prefix in cfg.outbound_blocked or []:
        p = re.sub(r"[^\d*#]", "", str(prefix))
        if p and any(f.startswith(p) for f in forms):
            return f"numbers starting with {p} are blocked (outbound_blocked)"
    pattern = str(cfg.outbound_allowed_regex or "").strip()
    if pattern:
        try:
            if not re.fullmatch(pattern, n):
                return "the number does not match outbound_allowed_regex"
        except re.error as e:
            return f"outbound_allowed_regex is invalid ({e}), no call is made"
    return ""


def hours(cfg):
    """outbound_hours "09:00-20:00" -> (start, end); None = no limit (also when it cannot be read)."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", str(cfg.outbound_hours or ""))
    try:
        return (dtime(int(m[1]), int(m[2])), dtime(int(m[3]) % 24, int(m[4]))) if m else None
    except ValueError:
        return None


def in_hours(cfg, now: datetime) -> bool:
    """Whether planned calls may be made at this time (local time of cfg.timezone)."""
    span = hours(cfg)
    if span is None:
        return True
    start, end = span
    t = now.astimezone(_tz(cfg)).time()
    return start <= t < end if start < end else (t >= start or t < end)


def _tz(cfg) -> ZoneInfo:
    return ZoneInfo(cfg.timezone or "Europe/Berlin")


def parse_time(value, cfg) -> datetime | None:
    """'2026-11-10T18:00' or '2026-11-10' in local time -> aware datetime; None if empty or unreadable."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=_tz(cfg))


def _texts(value) -> list:
    items = value if isinstance(value, list) else ([value] if value else [])
    return [str(x).strip()[:MAX_TEXT] for x in items if str(x).strip()][:MAX_ITEMS]


def build(cfg, data: dict, requester: dict, now: datetime, source: str = "api") -> tuple[dict | None, str]:
    """A task from the tool input or the API body. Returns (task, "") or (None, why it is refused)."""
    number = str(data.get("number") or "").strip()
    refusal = number_refusal(cfg, number)
    if refusal:
        return None, f"Not planned: {refusal}."
    text = str(data.get("task") or "").strip()
    if not text:
        return None, "Not planned: the task is missing."
    not_before = parse_time(data.get("not_before"), cfg) or now
    not_after = parse_time(data.get("not_after"), cfg) or max(not_before, now) + timedelta(hours=DEFAULT_WINDOW_H)
    if data.get("not_before") and parse_time(data.get("not_before"), cfg) is None:
        return None, "Not planned: not_before is not readable (format 2026-11-10T18:00)."
    if data.get("not_after") and parse_time(data.get("not_after"), cfg) is None:
        return None, "Not planned: not_after is not readable (format 2026-11-10T18:00)."
    if not_after <= max(not_before, now):
        return None, "Not planned: the time window is already over."
    try:
        retries = int(data.get("retries") if data.get("retries") is not None else 2)
    except (TypeError, ValueError):
        retries = 2
    grants = {}
    start, end = parse_time(data.get("calendar_from"), cfg), parse_time(data.get("calendar_to"), cfg)
    if start and end and end >= start:
        grants = {"calendar_from": start.date().isoformat(), "calendar_to": end.date().isoformat()}
        if str(data.get("book_title") or "").strip():
            grants["book_title"] = str(data["book_title"]).strip()[:120]
    elif data.get("calendar_from") or data.get("calendar_to") or data.get("book_title"):
        return None, "Not planned: calendar_from and calendar_to (YYYY-MM-DD) are both needed for the calendar."
    steps = []
    for s in data.get("follow_up") if isinstance(data.get("follow_up"), list) else []:
        if isinstance(s, dict) and str(s.get("step") or "").strip():
            tool = str(s.get("tool") or "")
            steps.append({"step": str(s["step"]).strip()[:MAX_TEXT], "tool": tool if tool in FOLLOW_TOOLS else ""})
    language = str(data.get("language") or "")
    return {
        "id": uuid.uuid4().hex[:8],
        "created": now.isoformat(timespec="seconds"),
        "source": source,
        "requester": {"number": str(requester.get("number") or ""), "name": str(requester.get("name") or "")},
        "number": number,
        "name": str(data.get("name") or "").strip()[:80],
        "language": language if language in ("de", "uk", "en", "auto") else "",
        "task": text[:2000],
        "goal": str(data.get("goal") or "").strip()[:MAX_TEXT] or text[:MAX_TEXT],
        "questions": _texts(data.get("questions")),
        "may_share": _texts(data.get("may_share")),
        "may_accept": str(data.get("may_accept") or "").strip()[:MAX_TEXT],
        "grants": grants,
        "follow_up": steps[:MAX_ITEMS],
        "not_before": not_before.isoformat(timespec="minutes"),
        "not_after": not_after.isoformat(timespec="minutes"),
        "retries": max(0, min(retries, int(cfg.task_max_retries or 0))),
        "leave_message": data.get("leave_message") is not False,
        "call_back": bool(data.get("call_back")) and bool(requester.get("number")),
        "status": PLANNED,
        "attempts": [],
        "next_try": not_before.isoformat(timespec="minutes"),
        "result": None,
    }, ""


def confirm_key(task: dict) -> str:
    """What a yes of the requester covers: whom to call, when, and everything that grants rights or changes
    something. Wording (goal, questions) may differ between the read-back and the confirmed call."""
    return json.dumps([config.norm_number(task["number"]), task["not_before"][:13], task["not_after"][:13],
                       task["grants"], sorted({s["tool"] for s in task["follow_up"]}), task["call_back"],
                       task["retries"]], sort_keys=True)


def when(task: dict, cfg) -> str:
    """The time window in words for the read-back."""
    start, end = datetime.fromisoformat(task["not_before"]), datetime.fromisoformat(task["not_after"])
    span = hours(cfg)
    limit = f", only between {span[0]:%H:%M} and {span[1]:%H:%M}" if span else ""
    return f"from {start:%A %Y-%m-%d %H:%M} until {end:%A %Y-%m-%d %H:%M}{limit}"


def read_back(task: dict, cfg, known: bool) -> str:
    """What Claude is told after the first plan_call: the checked plan, to be read back before a yes."""
    parts = [f"Plan checked, NOT stored yet. Read it back briefly: you will call {task['name'] or 'the number'} "
             f"at {task['number']}" + ("" if known else " (say the number digit by digit, it is not in the callers "
                                                        "list)")
             + f", {when(task, cfg)}, goal: {task['goal']}."]
    if task["may_share"]:
        parts.append(f"You will share only: {'; '.join(task['may_share'])}.")
    if task["may_accept"]:
        parts.append(f"You may agree to: {task['may_accept']}.")
    if task["grants"]:
        g = task["grants"]
        parts.append(f"The callee may hear free and busy times from {g['calendar_from']} to {g['calendar_to']}"
                     + (f" and book one appointment '{g['book_title']}' there." if g.get("book_title") else "."))
    if task["follow_up"]:
        parts.append("Afterwards you will: " + "; ".join(s["step"] for s in task["follow_up"]) + ".")
    parts.append(f"Not reached: {task['retries']} more attempt(s)"
                 + (", with a message on the mailbox." if task["leave_message"] else ", no message on a mailbox."))
    parts.append("The result comes as a notification" + (" and you call back." if task["call_back"] else "."))
    parts.append("Ask whether that is right. Only after a yes call plan_call again with the same values and "
                 "confirmed=true.")
    return " ".join(parts)


def brief(task: dict, owner: str) -> str:
    """The reason of the call for Claude: what to achieve, what may be shared and decided, and the etiquette."""
    who = ((task["requester"]["name"] or owner).split("(")[0].split() or ["the owner"])[0]  # first name
    parts = [f"A task from {who}: {task['goal']}"]
    if task["name"]:
        parts.append(f"The number should belong to {task['name']}")
    if task["questions"]:
        parts.append("Ask: " + "; ".join(task["questions"]))
    parts.append("You may tell the callee only: " + ("; ".join(task["may_share"]) if task["may_share"]
                                                     else f"that you call for {who}")
                 + f". Nothing else about {who} (no address, no other personal data)")
    parts.append(f"You may agree on your own to: {task['may_accept']}. Anything outside that you do not decide: "
                 f"say that {who} will get back about it" if task["may_accept"]
                 else f"Do not agree to anything on your own that the task does not cover; say that {who} will "
                      "get back about it")
    if task["follow_up"]:
        parts.append("Steps for after the call (" + "; ".join(s["step"] for s in task["follow_up"])
                     + ") are done by the phone system later: only collect what is needed for them")
    parts.append(f"Etiquette: say right at the start that you are an AI assistant calling on behalf of {who}, unless "
                 "the greeting already said so. If the callee does not want to talk or asks not to be called again, "
                 "accept that, apologize briefly and end the call. Before the goodbye say in one sentence that "
                 f"{who} gets a written summary of this call and that no audio is recorded. When everything is "
                 "settled, repeat the result briefly, say goodbye and hang up")
    return ". ".join(p.rstrip(". ") for p in parts) + "."


def callback_note(task: dict, owner: str) -> str:
    """For an inbound call from a number with an open task: the callee is probably calling back."""
    return ("You tried to reach this number earlier and they are probably calling back. After the greeting say why "
            "you had called and continue with it. " + brief(task, owner))


def report_text(task: dict) -> str:
    """The result in a few lines, for notifications and for the call back to the requester."""
    r = task.get("result") or {}
    lines = [str(r.get("summary") or "").strip() or f"Status: {task['status']}"]
    if r.get("answers"):
        lines.append("Answers: " + "; ".join(f"{k}: {v}" for k, v in r["answers"].items()))
    done = [s for s in r.get("steps") or [] if s.get("done")]
    if done:
        lines.append("Done: " + "; ".join(f"{s['step']}" + (f" ({s['result']})" if s.get("result") else "")
                                           for s in done))
    open_ = [s["step"] for s in r.get("steps") or [] if not s.get("done")] + list(r.get("open_points") or [])
    if open_:
        lines.append("Open: " + "; ".join(open_))
    return "\n".join(lines)


def clean_result(data: dict) -> dict:
    """The task_result input in a fixed shape (the model's output is not trusted to have it)."""
    data = data if isinstance(data, dict) else {}
    status = data.get("status") if data.get("status") in ("done", "partial", "failed", "not_reached") else "partial"
    answers = data.get("answers") if isinstance(data.get("answers"), dict) else {}
    steps = [{"step": str(s.get("step") or "")[:MAX_TEXT], "done": bool(s.get("done")),
              "result": str(s.get("result") or "")[:MAX_TEXT]}
             for s in (data.get("steps") if isinstance(data.get("steps"), list) else []) if isinstance(s, dict)]
    return {"status": status, "summary": str(data.get("summary") or "").strip()[:2000],
            "answers": {str(k)[:80]: str(v)[:MAX_TEXT] for k, v in list(answers.items())[:MAX_ITEMS * 2]},
            "steps": steps[:MAX_ITEMS * 2], "open_points": _texts(data.get("open_points")),
            "do_not_call": bool(data.get("do_not_call"))}


# ---------------------------------------------------------------------- store
class TaskStore:
    """tasks.json in the log folder: all tasks (finished ones for KEEP_DAYS) and the numbers not to call again."""

    def __init__(self, path, cfg):
        self.path = Path(path)
        self.cfg = cfg
        self.lock = threading.RLock()
        self.tasks: list[dict] = []
        self.do_not_call: list[dict] = []
        self._load()

    def _key(self, number: str) -> str:
        return config.norm_number(number, self.cfg.country_code)

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.tasks = [t for t in data.get("tasks", []) if isinstance(t, dict) and t.get("id")]
            self.do_not_call = [d for d in data.get("do_not_call", []) if isinstance(d, dict) and d.get("number")]
        except FileNotFoundError:
            return
        except (OSError, ValueError, AttributeError) as e:
            log.error("Reading %s failed, starting without tasks: %s", self.path, e)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"tasks": self.tasks, "do_not_call": self.do_not_call}, ensure_ascii=False,
                                  indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def interrupted(self) -> list[dict]:
        """Tasks that were running when the program stopped: the state of the call is unknown, so they are not
        dialed again but end as failed (to be reported by the caller of this method)."""
        with self.lock:
            out = [t for t in self.tasks if t["status"] == RUNNING]
            for t in out:
                t["status"] = "failed"
                t["finished"] = datetime.now(_tz(self.cfg)).isoformat(timespec="seconds")
                t["result"] = clean_result({"status": "failed", "summary": "Interrupted by a restart of BrainCall.",
                                            "open_points": ["Check what happened and plan the call again."]})
            if out:
                self._save()
            return [dict(t) for t in out]

    def blocked(self, number: str) -> bool:
        key = self._key(number)
        with self.lock:
            return any(d["number"] == key for d in self.do_not_call)

    def block(self, number: str, task_id: str = ""):
        with self.lock:
            if not self.blocked(number):
                self.do_not_call.append({"number": self._key(number), "task": task_id,
                                         "time": datetime.now(_tz(self.cfg)).isoformat(timespec="seconds")})
                self._save()

    def unblock(self, number: str):
        key = self._key(number)
        with self.lock:
            self.do_not_call = [d for d in self.do_not_call if d["number"] != key]
            self._save()

    def add(self, task: dict) -> dict:
        with self.lock:
            now = datetime.now(_tz(self.cfg))
            self.tasks = [t for t in self.tasks if t["status"] not in FINAL or not t.get("finished")
                          or now - datetime.fromisoformat(t["finished"]) <= timedelta(days=KEEP_DAYS)]
            self.tasks.append(task)
            self._save()
        log.info("Task %s planned: call %s (%s) %s; goal: %s", task["id"], task["name"] or "?", task["number"],
                 when(task, self.cfg), task["goal"])
        return task

    def get(self, task_id: str) -> dict | None:
        with self.lock:
            return next((dict(t) for t in self.tasks if t["id"] == task_id), None)

    def all(self) -> list[dict]:
        with self.lock:
            return [dict(t) for t in self.tasks]

    def _find(self, task_id: str) -> dict | None:
        return next((t for t in self.tasks if t["id"] == task_id), None)

    def cancel(self, task_id: str) -> str:
        """"" = cancelled, otherwise why not."""
        with self.lock:
            t = self._find(task_id)
            if t is None:
                return "not found"
            if t["status"] != PLANNED:
                return f"task is {t['status']}"
            self._finish(t, {"status": "cancelled", "summary": "Cancelled."}, "cancelled")
            return ""

    def calls_today(self, now: datetime) -> int:
        day = now.astimezone(_tz(self.cfg)).date().isoformat()
        with self.lock:
            return sum(a["time"].startswith(day) for t in self.tasks for a in t["attempts"])

    def expire(self, now: datetime) -> list[dict]:
        """Planned tasks whose time window is over end as not_reached. Returns them (to be reported)."""
        out = []
        with self.lock:
            for t in self.tasks:
                if t["status"] == PLANNED and now > datetime.fromisoformat(t["not_after"]):
                    tried = len(t["attempts"])
                    self._finish(t, {"status": "not_reached", "summary":
                                     f"Nobody was reached in the time window ({tried} attempt(s))." if tried else
                                     "The time window passed before a call was possible."}, "not_reached")
                    out.append(dict(t))
        return out

    def claim(self, now: datetime) -> dict | None:
        """The next task due now (earliest first), marked as running; None if nothing is due or allowed now."""
        if not in_hours(self.cfg, now):
            return None
        limit = int(self.cfg.outbound_max_calls_per_day or 0)
        with self.lock:
            due = [t for t in self.tasks if t["status"] == PLANNED and datetime.fromisoformat(t["next_try"]) <= now]
            if not due or (limit and self.calls_today(now) >= limit):
                return None
            t = min(due, key=lambda t: t["next_try"])
            return self._start(t, now, "call")

    def claim_callback(self, number: str, now: datetime) -> dict | None:
        """The open task for a number that was tried before and now calls in; marked as running."""
        key = self._key(number)
        with self.lock:
            t = next((t for t in self.tasks if t["status"] == PLANNED and t["attempts"]
                      and self._key(t["number"]) == key), None)
            return self._start(t, now, "callback") if t else None

    def has_callback(self, number: str) -> bool:
        """Whether a call from this number would continue an open task (see claim_callback)."""
        key = self._key(number)
        with self.lock:
            return any(t["status"] == PLANNED and t["attempts"] and self._key(t["number"]) == key
                       for t in self.tasks)

    def _start(self, t: dict, now: datetime, kind: str) -> dict:
        t["status"] = RUNNING
        t["attempts"].append({"time": now.astimezone(_tz(self.cfg)).isoformat(timespec="seconds"), "kind": kind,
                              "outcome": ""})
        self._save()
        return dict(t)

    def not_reached(self, task_id: str, outcome: str, now: datetime) -> dict:
        """After an attempt without a conversation: planned again for a retry, or final. Returns the task."""
        with self.lock:
            t = self._find(task_id)
            t["attempts"][-1]["outcome"] = outcome
            calls = sum(a["kind"] == "call" for a in t["attempts"])
            again = now + timedelta(minutes=max(1, int(self.cfg.task_retry_minutes or 15)))
            if calls <= t["retries"] and again < datetime.fromisoformat(t["not_after"]):
                t["status"] = PLANNED
                t["next_try"] = again.astimezone(_tz(self.cfg)).isoformat(timespec="minutes")
                self._save()
                log.info("Task %s: %s, next attempt at %s", t["id"], outcome, t["next_try"])
            else:
                self._finish(t, {"status": "not_reached",
                                 "summary": f"Nobody was reached after {calls} attempt(s) ({outcome})."},
                             "not_reached")
            return dict(t)

    def finish(self, task_id: str, result: dict, outcome: str = "talked") -> dict:
        with self.lock:
            t = self._find(task_id)
            if t["attempts"] and not t["attempts"][-1]["outcome"]:
                t["attempts"][-1]["outcome"] = outcome
            self._finish(t, result, result["status"])
            return dict(t)

    def _finish(self, t: dict, result: dict, status: str):
        t["status"] = status
        t["result"] = clean_result(result) | {"status": status}
        t["finished"] = datetime.now(_tz(self.cfg)).isoformat(timespec="seconds")
        self._save()
        log.info("Task %s finished: %s - %s", t["id"], status, t["result"]["summary"])
