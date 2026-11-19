"""BrainCall: SIP phone on the FRITZ!Box plus an HTTP API: call trigger, planned calls and Brain Assist."""
import hmac
import http.client
import json
import logging
import logging.handlers
import math
import os
import queue
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import console  # noqa: E402
import deps  # noqa: E402

console.enable_ansi()
deps.ensure()

try:  # use certificates from the Windows/system store (own CA), not just certifi
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import pyVoIP  # noqa: E402
from pyVoIP.VoIP import CallState, PhoneStatus, VoIPPhone  # noqa: E402  (must be imported before pyVoIP.SIP)
from pyVoIP.SIP import SIPMessageType  # noqa: E402

import assist  # noqa: E402
import config  # noqa: E402
import contacts  # noqa: E402
import inbox  # noqa: E402
import discovery  # noqa: E402
import fritz_sip  # noqa: E402
import logfilter  # noqa: E402
import memory  # noqa: E402
import phrases  # noqa: E402
import rhythm  # noqa: E402
import shopping  # noqa: E402
import tasks  # noqa: E402
from brain import Brain  # noqa: E402
from ha import HA  # noqa: E402
from session import CallSession  # noqa: E402
from speech import Speech  # noqa: E402
import web  # noqa: E402

cfg = config.load()
_LOGDIR = Path(cfg.log_dir)
_LOGDIR.mkdir(parents=True, exist_ok=True)
_con = logging.StreamHandler(sys.stdout)
_con.setFormatter(console.ColorFormatter())
# rotates at 2 MB and keeps 5 old files (braincall.log.1 … .5): at most about 12 MB
_file = logging.handlers.RotatingFileHandler(_LOGDIR / "braincall.log", maxBytes=2_000_000,
                                             backupCount=5, encoding="utf-8")
_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
LOG_LEVEL = config.log_level(cfg)
# other libraries: only warnings, everything with trace except headers and secrets (#77)
_gate = logfilter.Gate(LOG_LEVEL, (cfg.anthropic_api_key, cfg.openai_api_key, cfg.ha_token, cfg.sip_password,
                                   cfg.supervisor_token))
for _h in (_con, _file):
    _h.addFilter(_gate)
logging.basicConfig(level=LOG_LEVEL, handlers=[_con, _file])
log = logging.getLogger("main")
pyVoIP.DEBUG = False


def _excepthook(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        return
    log.critical("Unhandled error", exc_info=(exc_type, exc, tb))


sys.excepthook = _excepthook
threading.excepthook = lambda a: _excepthook(a.exc_type, a.exc_value, a.exc_traceback)

console.ASSISTANT = cfg.assistant_name or "Claude"
# The phone needs the SIP account; without it only the HTTP API runs (Brain Assist, #50)
PHONE = bool(cfg.sip_user and cfg.sip_password)
# every request to the HTTP API needs this token; a generated one keeps the API on 127.0.0.1 (config.api_token)
API_TOKEN, TOKEN_GENERATED = config.api_token(cfg)
_gate.add(API_TOKEN)
ha = HA(cfg)
speech = Speech(cfg)
brain = Brain(cfg, ha)
phrase_cache = phrases.PhraseCache(speech, cfg, _LOGDIR / "cache")
calls = memory.CallMemory(_LOGDIR / "calls.jsonl", memory.parse_days(cfg.caller_memory), cfg.country_code)
# the calls taken for the owner (#104): told when they call in, messages tool; whatever caller_memory is
owner_inbox = inbox.Inbox(_LOGDIR / inbox.FILE, cfg.country_code, cfg.timezone)
brain.inbox = owner_inbox
task_store = tasks.TaskStore(_LOGDIR / "tasks.json", cfg) if cfg.tasks and PHONE else None
brain.tasks = task_store


def task_event(task: dict) -> dict:
    """What Home Assistant gets on every status change of a task (braincall_task_updated); the full task with its
    result comes from GET /api/tasks/<id>, the result also with braincall_task_finished."""
    return {"id": task["id"], "status": task["status"], "number": task["number"], "name": task["name"],
            "goal": task["goal"], "requester": task["requester"]["name"],
            "requester_ha_user": task["requester"].get("ha_user", ""), "next_try": task.get("next_try", ""),
            "attempts": len(task["attempts"]), "contact": task.get("contact_id", ""),
            "steps": len(task.get("steps") or [])}


def announce_task(task: dict):
    """TaskStore.on_change: the event in the background, Home Assistant may be slow and a task must not wait."""
    threading.Thread(target=ha.fire_event, args=("braincall_task_updated", task_event(task)), name="task event",
                     daemon=True).start()


if task_store is not None:
    task_store.on_change = announce_task


def announce_messages():
    """Inbox.on_change: braincall_messages_updated with the counts, in the background (brainassist#9 refreshes its
    call list on it; the entries come from GET /api/messages)."""
    def fire():
        try:
            ha.fire_event("braincall_messages_updated", owner_inbox.counts())
        except Exception:  # noqa: BLE001
            log.exception("Announcing the messages failed")
    threading.Thread(target=fire, name="messages event", daemon=True).start()


owner_inbox.on_change = announce_messages


def contact_in_use(contact: dict) -> str:
    """What an open planned call needs this contact for (its goal), so it is not deleted or merged away meanwhile;
    "" if nothing (contacts.Contacts.in_use)."""
    if task_store is None:
        return ""
    for t in task_store.all():
        mine = (t["contact_id"] == contact.get("id") if t.get("contact_id")
                else config.has_number(contact, t["number"], cfg.country_code))  # a task from before #92
        if t["status"] not in tasks.FINAL and mine:
            return t.get("goal") or t.get("name") or t["id"]
    return ""


def contact_merged(other: dict, merged: dict):
    """contacts.Contacts.on_merge: the memory of the contact merged away is found through the one it went into."""
    moved = calls.rekey({"contact": other["id"], "ha_user": other.get("ha_user") or "",
                         "ha_device": other.get("ha_device") or "",
                         "numbers": [n["number"] for n in config.entry_numbers(other)]}, merged["id"])
    if moved:
        log.info("Memory: %d summaries of %s now belong to %s", moved, other["name"], merged["name"])
    if task_store is not None and task_store.rekey_contact(other["id"], merged["id"]):
        log.info("Tasks of %s now belong to %s", other["name"], merged["name"])


contact_store = contacts.Contacts(cfg, in_use=contact_in_use, on_merge=contact_merged)
brain.contacts = contact_store
CONTACTS_INTERVAL = 2  # seconds between looks at contacts.json for a change made by hand
shopping_list = (shopping.Shopping(cfg, ha, _LOGDIR / "shopping-history.jsonl")
                 if cfg.shopping_auto and ha.enabled and cfg.shopping_list else None)
if shopping_list is not None and cfg.shopping_rhythm_list:
    rhythm.Rhythm(cfg, ha, shopping_list, _LOGDIR / "shopping-products.json", classify=brain.classify_products)
brain.shopping = shopping_list
# Brain Assist: the running conversations of POST /api/converse; one that is over is summarized (assist_closed)
conversations = assist.Conversations(on_close=lambda conv: assist_closed(conv))
ASSIST_SWEEP = 30  # seconds between looks for Assist conversations whose idle time is over
busy = threading.Lock()
sip_failed: dict = {}
abandoned: set = set()  # Call-IDs of outbound calls given up while ringing (a late 200 OK gets hung up)
answer_lock = threading.Lock()  # a 200 OK is handled either before a call is given up or as a late answer
OWNER = (cfg.owner_name or "").strip()
OWNER_FIRST = OWNER.split()[0] if OWNER else ""
MAX_BODY = 64 * 1024
UNREGISTER_TIMEOUT = 10  # seconds; the add-on gets 20 s to stop (braincall/config.yaml)
TASK_INTERVAL = 5  # seconds between looks at the planned calls
CALLBACK_WAIT = 600  # seconds to wait for a free line before the call back with a task's result is given up
WATCHDOG_INTERVAL = 30  # seconds between checks of the SIP registration
RECONNECT_AFTER = 300  # seconds without SIP registration before the phone is set up again
stopping = threading.Event()  # set on shutdown: no new phone is set up from then on
# /health (#37): the SIP watchdog loop counts as stuck after this long without a round; a reconnect blocks it for at
# most a few minutes (pyVoIP waits up to 30 s per registration step)
WATCHDOG_STALL = 600
STARTUP_GRACE = 600  # seconds the Supervisor's watchdog may find the HTTP API not started yet (SIP setup first)
HEALTH_PROBE_TIMEOUT = 5  # seconds; the Supervisor gives a watchdog request 10
started = time.monotonic()
api_up = threading.Event()  # set once the HTTP API listens
sip_watchdog: threading.Thread | None = None  # the thread of watchdog(), once started
watchdog_seen = 0.0  # time.monotonic() of the SIP watchdog loop's last round
phone_lock = threading.Lock()  # swapping the phone (connect) vs. picking the one to stop on shutdown
# what the line is doing (#100): /health, /api/status and the braincall_state event, for Brain Assist's entities.
# Not `busy`: that lock is also held while the watchdog reconnects or a planned call is picked
IDLE, RINGING, IN_CALL, DIALING = "idle", "ringing", "in_call", "dialing"
line_lock = threading.Lock()
line = {"line": IDLE, "call": None}
sip_confirmed = ""  # the SIP status as the watchdog confirmed it (not pyVoIP's moment of re-registering)
state_events: queue.Queue = queue.Queue()  # sent one after the other by one thread: in order, never during a call
state_sender: threading.Thread | None = None
state_sender_lock = threading.Lock()


def detect_local_ip(target: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 5060))
        return s.getsockname()[0]
    finally:
        s.close()


def norm(n: str) -> str:
    return config.norm_number(n, cfg.country_code)


def same_number(a: str, b: str) -> bool:
    return bool(norm(a)) and norm(a) == norm(b)


GUEST = frozenset({"guest"})
SUMMARY_TITLE = {"de": "Anruf von {who}", "en": "Call from {who}", "uk": "Дзвінок від {who}"}
MISSED_TITLE = {"de": "Verpasster Anruf von {who}", "en": "Missed call from {who}",
                "uk": "Пропущений дзвінок від {who}"}
NEW_CONTACT = {"de": "Neu in den Kontakten: {name}", "en": "New contact: {name}", "uk": "Новий контакт: {name}"}
MISSED_TEXT = {"de": "Die Leitung war besetzt (ein anderes Gespräch lief), der Anruf wurde abgewiesen.",
               "en": "The line was busy (another call was going on), the call was rejected.",
               "uk": "Лінія була зайнята (тривала інша розмова), дзвінок відхилено."}
TASK_TITLE = {"de": "Auftrag Anruf bei {who}: {status}", "en": "Task call to {who}: {status}",
              "uk": "Завдання, дзвінок до {who}: {status}"}
STATUS_WORDS = {"de": {"done": "erledigt", "partial": "teilweise erledigt", "failed": "nicht geklappt",
                       "not_reached": "nicht erreicht", "message_left": "Nachricht hinterlassen",
                       "cancelled": "abgebrochen", "waiting": "wartet auf Rückruf",
                       "no_reply": "kein Rückruf"},
                "en": {"waiting": "waiting for a call back", "no_reply": "no call back"},
                "uk": {"waiting": "очікує зворотного дзвінка", "no_reply": "без зворотного дзвінка"}}


def find_caller(number: str, wildcard: bool = True) -> dict | None:
    """Entry from callers for a number; an exact number wins over "*". None = not listed."""
    fallback = None
    for c in cfg.callers:
        if c["number"] == "*":
            fallback = fallback or c
        elif config.has_number(c, number, cfg.country_code):
            return c
    return fallback if wildcard else None


def caller_name(caller: dict | None) -> str:
    """Name of a listed number; "" for unknown callers (the "*" entry), they get asked for their name."""
    if not caller or caller["number"] == "*":
        return ""
    return caller["name"] or OWNER or caller["number"]


def caller_label(caller: dict | None, number: str) -> str:
    """For the log and notifications: the person and which of their phones, "Jan (laut)"; only the person if the
    number has no label. "" for unknown callers. Everything said to others names only the person (caller_name)."""
    name = caller_name(caller)
    phone = config.phone_of(caller, number, cfg.country_code) if name else ""
    return f"{name} ({phone})" if phone else name


def web_for(caller: dict | None):
    """The caller's web setting for Brain: None = by access (admin, read), never for unknown callers ("*")."""
    if not caller or caller["number"] == "*":
        return False
    return caller.get("web")


def lang_for(caller: dict | None) -> str:
    """Language of the caller entry, otherwise the global setting."""
    return (caller or {}).get("language") or cfg.language or "de"


def previous(number: str, access, caller: dict | None = None) -> str:
    """What Claude remembers of earlier calls and Assist conversations with this caller, for the intro
    (memory.note). caller: the entry of the number, so the notes of its Home Assistant user are found too."""
    try:
        text = memory.note(calls.recent(memory.who(caller, number), access), OWNER_FIRST or "the owner",
                           cfg.timezone, calls.enabled)
    except Exception:  # noqa: BLE001  the memory must never stop a call
        log.exception("Reading the caller memory failed")
        text = ""
    return text + (owner_inbox.note(OWNER_FIRST or "the owner") if "admin" in access else "")


def same_person(whom: dict) -> list:
    """Everything a person's summaries may be stored under, for forgetting them: what was asked for, plus the
    number and the Home Assistant user of the caller entries that name them (an entry links the two; lines from
    before 1.12 have only the number). "*" entries name nobody, and a device's memory stays its own."""
    user = str(whom.get("ha_user") or "").strip().casefold()
    out = [whom]
    for c in cfg.callers:
        entry_user = c["ha_user"].strip().casefold()
        named = (user and entry_user == user) or (whom.get("number") and config.has_number(c, whom["number"],
                                                                                           cfg.country_code))
        if not named or c["number"] == "*" or entry_user == "*":
            continue
        out.append({"ha_user": c["ha_user"], "contact": c.get("id") or ""})
        out += [{"number": n["number"]} for n in c.get("numbers") or []]
    return out


def recall(person: dict) -> str:
    """What Claude remembers of the person of an Assist conversation (assist.identify), for its first note."""
    text = memory.note(calls.recent(person["who"], person["access"]), OWNER_FIRST or "the owner", cfg.timezone,
                       calls.enabled, subject=assist.subject(person))
    return text + (owner_inbox.note(OWNER_FIRST or "the owner", phone=False) if "admin" in person["access"] else "")


def after_assist(conv):
    """Summary of an Assist conversation that is over, into the memory of the person. Never a notification: those
    are for calls, which may go unnoticed; an Assist conversation was seen by whoever had it (#50)."""
    try:
        person = conv.person
        lang = cfg.language if cfg.language in SUMMARY_TITLE else "de"
        summary = brain.summarize(conv.transcript, conv.label, conv.actions, lang, channel=conv.channel)
        if not summary:
            return
        log.info("Summary (Assist, %s): %s", conv.label, summary)
        calls.add(person["who"], summary, person["access"], when=datetime.fromtimestamp(conv.ended, timezone.utc),
                  channel=f"assist-{conv.channel}", device=conv.device)
    except Exception:  # noqa: BLE001
        log.exception("Summary of an Assist conversation failed")


def assist_closed(conv):
    """An Assist conversation is over (assist.Conversations.on_close). Summarized in the background, and only if
    the memory is on: nobody else uses the summary."""
    if not conv.transcript or conv.person is None or not calls.enabled:
        return
    threading.Thread(target=after_assist, args=(conv,), name="after assist", daemon=True).start()


def contacts_loop():
    """Reads contacts.json again when it was changed by hand: no restart needed."""
    while not stopping.wait(CONTACTS_INTERVAL):
        try:
            contact_store.refresh()
        except Exception:  # noqa: BLE001
            log.exception("Contacts: reading the file again failed")


INGRESS_PORT = 8098  # ingress_port in braincall/config.yaml


def start_ingress():
    """The contacts page for Home Assistant's ingress: listens only on the Supervisor's network, so it is not
    reachable from the LAN without the token (the app runs with the host network)."""
    try:
        server = ThreadingHTTPServer((INGRESS_HOST, INGRESS_PORT), IngressHandler)
    except OSError as e:
        log.warning("Contacts page in Home Assistant not available: cannot listen on %s:%s (%s); it is still at "
                    "/contacts on the API port, with the token. The Supervisor's watchdog cannot ask /health either "
                    "and restarts the app if it is on", INGRESS_HOST, INGRESS_PORT, e)
        return
    threading.Thread(target=server.serve_forever, name="contacts page", daemon=True).start()
    log.info("Contacts page: in Home Assistant's sidebar (ingress, %s:%s)", INGRESS_HOST, INGRESS_PORT)


def assist_loop():
    while not stopping.wait(ASSIST_SWEEP):
        try:
            conversations.sweep()
        except Exception:  # noqa: BLE001
            log.exception("Brain Assist: closing conversations failed")


def report(answered: bool, number: str, started: float, session=None, message="", caller=None, summary="",
           ended: float = 0.0):
    if session is not None and getattr(session, "limit_reached", False):  # max_call_minutes ended it (#22)
        message = f"{message}; ended: call limit" if message else "ended: call limit"
    ha.fire_event("braincall_call", {
        "number": number,
        "answered": answered,
        "reason": message,
        "duration_s": round((ended or time.time()) - started),
        "transcript": "\n".join(session.transcript) if session else "",
        "caller_name": caller_name(caller),
        "phone": config.phone_of(caller, number, cfg.country_code) if caller else "",
        "access": ",".join(sorted(caller["access"])) if caller else "",
        "summary": summary,
    })


def for_owner(caller, inbound: bool = True) -> bool:
    """Whether the owner hears about this incoming call (notification and messages, #104): everyone without admin
    rights, unless their contact says notify: false (or an admin's says notify: true)."""
    access = caller["access"] if caller else GUEST
    notify = caller.get("notify") if caller else None
    return inbound and (notify if notify is not None else "admin" not in access)


def save_caller(number: str, name: str, rule) -> tuple[dict | None, str]:
    """An unknown caller (the "*" entry) who gave their name becomes a contact (#104) with what that entry allowed
    (never admin, never more), so the next call knows them and the owner can have them called back by name.
    Returns (the new contact or None, a note for the owner's messages: what was done)."""
    name = " ".join(str(name or "").split())
    if not name or not norm(number):
        return None, ""
    if contact_store.by_number(number):
        return None, ""
    twin = contact_store.by_name(name)
    if twin:
        return None, (f"not saved as a contact: there already is a contact {twin['name']} with another number "
                      "(perhaps a new number of theirs, contact_edit can add it)")
    rule = rule or {}
    # the same as before, only with a name: rights, notify and instructions of the "*" entry, and never the web
    # (web_for gives "*" none; a contact with read rights would otherwise get it by default)
    data = {"name": name, "numbers": [{"number": number}],
            "access": sorted(set(rule.get("access") or ()) - {"admin", "guest"}),
            "instructions": rule.get("instructions") or "", "web": False}
    if isinstance(rule.get("notify"), bool):
        data["notify"] = rule["notify"]
    saved, errors = contact_store.save(data, origin="call")
    if errors:
        log.info("Caller %s (%s) not saved as a contact: %s", name, number, contacts.problems(errors))
        return None, f"not saved as a contact ({contacts.problems(errors)})"
    return saved, "saved as a new contact (name as understood on the phone; contact_edit can correct it)"


def after_call(answered: bool, number: str, started: float, ended: float, session, message: str, caller,
               inbound: bool, for_task: bool = False):
    """Summary, event, notification, the owner's messages and call note; runs after the line is free again.
    for_task: an incoming call that went on with a task (a call back we waited for): reported with the task."""
    try:
        access = caller["access"] if caller else GUEST
        summary = ""
        notify = for_owner(caller, inbound)
        owner_hears = notify and not for_task  # a task's call back is reported with the task, not as a message
        if session and session.caller_turns:
            lang = cfg.language if cfg.language in SUMMARY_TITLE else "de"
            name = caller_name(caller)
            unknown = inbound and caller is not None and not name  # the "*" entry: may become a contact
            talked = session.caller_turns >= 2 and not (session.mailbox or session.unanswered)
            # a contact's language follows what they spoke last (#93); not for a mailbox or a very short call
            spoken = {} if talked and ((caller and caller.get("id")) or unknown) else None
            facts = {} if owner_hears else None  # name, call back, when (#104)
            try:
                summary = brain.summarize(session.transcript, f"{name} ({number})" if name else number,
                                          session.actions, lang, outbound=not inbound, mailbox=session.mailbox,
                                          unanswered=session.unanswered,
                                          **({"spoken": spoken} if spoken is not None else {}),
                                          **({"facts": facts} if facts is not None else {}))
            except Exception as e:  # noqa: BLE001
                log.warning("Summary failed: %s", e)
            facts = facts or {}
            contact, note = None, ""
            if unknown and owner_hears and facts.get("name"):
                try:
                    contact, note = save_caller(number, facts["name"], caller)
                except Exception:  # noqa: BLE001  the summary and the message still go out
                    log.exception("Saving the caller %s as a contact failed", number)
            target = (contact or {}).get("id") or (caller.get("id") if caller and name else "")
            if spoken and spoken.get("language") and target:
                try:
                    contact_store.set_language(target, spoken["language"])
                except OSError as e:
                    log.warning("Contacts: storing the language failed: %s", e)
            if notify:
                label = caller_label(caller, number) or facts.get("name", "")
                title = SUMMARY_TITLE[lang].format(who=f"{label} ({number})" if label else number)
                text = summary or "\n".join(session.transcript)[-1500:]
                if contact:
                    text += "\n" + NEW_CONTACT[lang].format(name=contact["name"])
                for service in cfg.notify_services or []:
                    ha.notify(str(service), title, text)
            if owner_hears:
                try:
                    owner_inbox.add(number, summary or "\n".join(session.transcript)[-inbox.MAX_SUMMARY:],
                                    name=name or facts.get("name", ""), contact=target,
                                    callback=facts.get("callback", False), when=facts.get("when", ""), note=note,
                                    at=datetime.fromtimestamp(ended, timezone.utc), duration=ended - started)
                except Exception:  # noqa: BLE001
                    log.exception("Saving the message in %s failed", owner_inbox.path)
            elif not inbound and not (session.mailbox or session.unanswered):
                try:  # we talked with them: a call back they asked for before this call is done
                    owner_inbox.settle(number, datetime.fromtimestamp(ended, timezone.utc))
                except Exception:  # noqa: BLE001
                    log.exception("Settling the call backs to %s failed", number)
            if summary:
                log.info("Summary: %s", summary)
                try:
                    calls.add(memory.who(caller, number), summary, access, inbound,
                              datetime.fromtimestamp(ended, timezone.utc))
                except Exception:  # noqa: BLE001  event and call note still follow
                    log.exception("Saving the summary in %s failed", calls.path)
        report(answered, number, started, session, message, caller, summary, ended)
        if inbound and session and access == GUEST and session.transcript:
            notes = _LOGDIR / "call-notes.txt"
            with open(notes, "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M}  from {number or 'withheld'} ===\n")
                f.write("\n".join(session.transcript) + "\n")
                if summary:
                    f.write(f"Summary: {summary}\n")
            log.info("Call note saved: %s", notes)
    except Exception:  # noqa: BLE001
        log.exception("After-call handling failed")


def finish(answered: bool, number: str, started: float, session, message: str, caller, inbound: bool,
           for_task: bool = False):
    threading.Thread(target=after_call, name="after call", daemon=True,
                     args=(answered, number, started, time.time(), session, message, caller, inbound,
                           for_task)).start()


# ---------------------------------------------------------------------- line state (#100)
def set_line(state: str, number: str = "", caller=None, direction: str = ""):
    """The line changes: idle, ringing (incoming, before it is answered), dialing (outbound, until answered) or
    in_call. `since` is when the line got into this state. A change fires braincall_state. Never raises: a call
    goes on and the line is freed whatever happens here."""
    try:
        with line_lock:
            if state == line["line"] and state == IDLE:
                return
            line["line"] = state
            line["call"] = None if state == IDLE else {
                "number": number, "caller_name": caller_name(caller), "direction": direction,
                "since": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        announce_state()
    except Exception:  # noqa: BLE001
        log.exception("Line state %s not set", state)


def sip_value() -> str:
    """The SIP status as text; "unknown" if the phone cannot say."""
    try:
        return sip_status().value
    except Exception:  # noqa: BLE001
        return "unknown"


def line_status() -> dict:
    """line, sip and the current call (number, caller_name, direction, since; None while idle)."""
    with line_lock:
        return {"line": line["line"], "sip": sip_value(), "call": dict(line["call"]) if line["call"] else None}


def announce_state(sip: str | None = None):
    """Fires braincall_state with the line, the SIP status and the current call; in the background.
    sip: a newly confirmed SIP status (note_sip). Otherwise the event has the last confirmed one, so a change of the
    line during pyVoIP's re-registration does not announce a moment of REGISTERING. Queued while line_lock is held:
    the events go out in the order of their snapshots."""
    global sip_confirmed, state_sender
    with line_lock:
        if sip is not None:
            sip_confirmed = sip
        data = {"line": line["line"], "sip": sip_confirmed or sip_value(),
                "call": dict(line["call"]) if line["call"] else None}
        state_events.put(data)
    with state_sender_lock:
        if state_sender is None or not state_sender.is_alive():
            state_sender = threading.Thread(target=send_states, name="state events", daemon=True)
            state_sender.start()


def note_sip():
    """Confirms the SIP status (watchdog, after connecting); a change fires braincall_state."""
    current = sip_value()
    if current != sip_confirmed:
        announce_state(current)


def send_states():
    while True:
        data = state_events.get()
        try:
            ha.fire_event("braincall_state", data)
        except Exception:  # noqa: BLE001
            log.exception("Event braincall_state failed")
        finally:
            state_events.task_done()


def _ringing(call) -> bool:
    return call.state in (CallState.DIALING, CallState.RINGING) and call.call_id not in sip_failed


def _wait_ringing(call, seconds: float):
    deadline = time.monotonic() + seconds
    while _ringing(call) and time.monotonic() < deadline:
        time.sleep(0.2)


def outbound(number: str, message: str, task: dict | None = None):
    """Calls a number and holds the conversation; the caller of this function holds `busy`, it is released here.
    task: the planned call this is (tasks.build). Returns the session, or None if the call was not answered."""
    started = time.time()
    caller = find_caller(number, wildcard=False)  # not listed: like a guest, no "*" entry needed
    session = None
    answered = False
    call = None
    try:
        refusal = tasks.number_refusal(cfg, number)
        if refusal:
            log.warning("Not calling %s: %s", number, refusal)
            return None
        dial = norm(number)  # the complete number, without spaces or dashes (they would cut it off in the INVITE)
        log.info("Calling %s (reason: %s)", dial, message or "-")
        set_line(DIALING, number, caller, "out")
        call = phone.call(dial)
        _wait_ringing(call, float(cfg.ring_timeout))
        if _ringing(call):
            log.info("Not answered after %ss, cancelling", cfg.ring_timeout)
            for wait in (0.5, 1, 2, 4):  # UDP: repeat until the FRITZ!Box answers with 487 Request Terminated
                fritz_sip.cancel(phone.sip, call.call_id)
                _wait_ringing(call, wait)
                if not _ringing(call):
                    break
            if _ringing(call):
                log.warning("CANCEL not confirmed, the phone may keep ringing")
            return None
        if call.state != CallState.ANSWERED:
            log.warning("Call rejected: SIP %s", sip_failed.get(call.call_id, call.state.value))
            return None
        answered = True
        set_line(IN_CALL, number, caller, "out")
        session = CallSession(call, brain, speech, cfg, phrase_cache)
        access = caller["access"] if caller else GUEST
        session.run(outbound=True, message=message, caller=number,
                    language=(task or {}).get("language") or lang_for(caller),
                    access=access, caller_name=caller_name(caller),
                    instructions=caller["instructions"] if caller else "",
                    previous=previous(number, access, caller),
                    web=web_for(caller), grants=(task or {}).get("grants"),
                    leave_message=(task or {}).get("leave_message", True))
        log.info("Call ended")
    except TimeoutError as e:
        log.warning("Outbound call failed: %s", e)
    except Exception:  # noqa: BLE001
        log.exception("Outbound call failed")
    finally:
        if call is not None:
            sip_failed.pop(call.call_id, None)
            phone.sip.pending_invites.pop(call.call_id, None)
            if not answered:
                with answer_lock:  # from here on a 200 OK is ended as a late answer (logged_parse)
                    abandoned.add(call.call_id)
                if call.state == CallState.ANSWERED:  # picked up before that, e.g. while cancelling
                    try:
                        call.hangup()
                    except Exception:  # noqa: BLE001
                        log.exception("Hanging up the cancelled call failed")
                phone.calls.pop(call.call_id, None)  # pyVoIP never drops unanswered calls -> RTP port stays taken
        set_line(IDLE)
        finish(answered, number, started, session, message, caller, inbound=False)
        busy.release()
    return session


# ---------------------------------------------------------------------- planned calls with a task (#51)
def report_task(task: dict):
    """The result of a finished task: notification, Home Assistant event and, if asked for, a call back."""
    try:
        lang = cfg.language if cfg.language in TASK_TITLE else "de"
        status = STATUS_WORDS.get(lang, {}).get(task["status"], task["status"])
        title = TASK_TITLE[lang].format(who=task["name"] or task["number"], status=status)
        text = tasks.report_text(task)
        for service in cfg.notify_services or []:
            ha.notify(str(service), title, text)
        ha.fire_event("braincall_task_finished", {
            "id": task["id"], "status": task["status"], "number": task["number"], "name": task["name"],
            "task": task["task"], "requester": task["requester"]["name"],
            "requester_ha_user": task["requester"].get("ha_user", ""), "attempts": len(task["attempts"]),
            **{k: (task["result"] or {}).get(k) for k in ("summary", "answers", "steps", "open_points")}})
        if task["call_back"] and task["status"] != "cancelled":
            call_back(task, text)
    except Exception:  # noqa: BLE001
        log.exception("Reporting task %s failed", task.get("id"))


def call_back(task: dict, text: str):
    """Calls the requester with the result as soon as the line is free (the notification was sent already)."""
    number = task["requester"]["number"]
    deadline = time.monotonic() + CALLBACK_WAIT
    while not busy.acquire(blocking=False):
        if stopping.wait(1) or time.monotonic() > deadline:
            log.warning("Task %s: no call back to %s, the line stayed busy", task["id"], number)
            return
    if sip_status() != PhoneStatus.REGISTERED:
        busy.release()
        log.warning("Task %s: no call back to %s, SIP not registered", task["id"], number)
        return
    outbound(number, tasks.call_back_brief(task, text, OWNER_FIRST))


def report_waiting(task: dict):
    """A task waits for a call back (#92): a short notification with what came of it so far, no call back. The
    final report comes when it ends."""
    try:
        lang = cfg.language if cfg.language in TASK_TITLE else "de"
        status = STATUS_WORDS.get(lang, {}).get(task["status"], task["status"])
        title = TASK_TITLE[lang].format(who=task["name"] or task["number"], status=status)
        for service in cfg.notify_services or []:
            ha.notify(str(service), title, tasks.report_text(task))
    except Exception:  # noqa: BLE001
        log.exception("Reporting task %s failed", task.get("id"))


def settle_task(task: dict, session):
    """After a call of a task (line already free): retry if nobody was reached (also when nothing we said was
    answered). Otherwise the call is a step of the task's history and the evaluation decides (#92): the next call,
    waiting for a call back (also after a message on the mailbox), or the end with its report."""
    try:
        now = datetime.now(timezone.utc)
        if session is None or not session.caller_turns:
            done = task_store.not_reached(task["id"], "no answer" if session is None else "nothing said", now)
        elif session.mailbox and not session.message_left:
            done = task_store.not_reached(task["id"], "mailbox", now)  # nothing to follow up
        elif session.unanswered:
            # only what was said when the call was picked up, nothing answered after (#85): an unrecognized
            # mailbox, an announcement, or someone who hung up at once. Nobody was reached, nothing to follow up
            done = task_store.not_reached(task["id"], "no reply", now)
        else:
            try:
                result = brain.follow_up(task, session.transcript, list(session.actions), mailbox=session.mailbox)
            except Exception as e:  # noqa: BLE001
                log.exception("Follow-up of task %s failed", task["id"])
                result = ({"summary": "Only the mailbox answered; a message was left."} if session.mailbox else
                          {"status": "partial", "summary": f"The call took place, but the follow-up failed ({e}).",
                           "open_points": ["Check the call summary and do the follow-up steps by hand."]})
            status = result.get("status")
            if session.mailbox:
                status = tasks.LEFT  # the phone system knows; the follow-up only wrote the summary
            elif status == tasks.LEFT and not task["leave_message"]:
                status = "not_reached"  # no message was to be left: what was said there does not end the task
            if status != "not_reached":  # someone was reached, or a message was left: a step of the history (#92)
                task_store.record_step(task["id"], session.transcript,
                                       "message left" if status == tasks.LEFT else "talked", result.get("summary"))
            if result.get("do_not_call"):  # the line of this call: a call back may come from another phone
                task_store.block((task["attempts"] or [{}])[-1].get("number") or task["number"], task["id"])
                if status in (tasks.WAIT, tasks.NEXT):  # nobody is called again: the task ends with what it has
                    status = "partial"
            if status == "not_reached":
                done = task_store.not_reached(task["id"], "mailbox", now)
            elif status in (tasks.LEFT, tasks.WAIT):
                done = task_store.wait(task["id"], result, "message left" if status == tasks.LEFT else "talked")
                report_waiting(done)
            elif status == tasks.NEXT:
                nxt = result["next_call"]
                done = task_store.plan_next(task["id"], nxt["number"], nxt["not_before"], nxt["until"],
                                            {**result, "status": "partial"})
            else:
                done = task_store.finish(task["id"], {**result, "status": status})
        if done["status"] in tasks.FINAL:
            report_task(done)
    except Exception:  # noqa: BLE001
        log.exception("Task %s failed", task.get("id"))


def run_tasks():
    """One look at the planned calls: ends those whose window is over and makes the next call that is due."""
    for expired in task_store.expire(datetime.now(timezone.utc)):
        report_task(expired)
    now = datetime.now(timezone.utc)  # reporting may have taken minutes (call back to the requester)
    if sip_status() != PhoneStatus.REGISTERED or not busy.acquire(blocking=False):
        return
    task = None
    try:
        task = task_store.claim(now)
    finally:
        if task is None:
            busy.release()
    if task is None:
        return
    phone = config.phone_of(tasks.contact_of(cfg, task), task["number"], cfg.country_code)
    log.info("Task %s: calling %s (%s%s), attempt %d, step %d", task["id"], task["name"] or "?", task["number"],
             f", {phone}" if phone else "", task["tries"], len(task["steps"]) + 1)
    settle_task(task, outbound(task["number"], tasks.brief(task, OWNER_FIRST, phone), task))


def task_loop():
    for interrupted in task_store.interrupted():
        report_task(interrupted)
    while not stopping.wait(TASK_INTERVAL):
        try:
            run_tasks()
        except Exception:  # noqa: BLE001
            log.exception("Planned calls: error")


def shopping_loop():
    """Looks at the shopping list once a minute (shopping.py), the first time right at the start."""
    while True:
        try:
            shopping_list.poll()
        except Exception:  # noqa: BLE001
            log.exception("Shopping list: error")
        if stopping.wait(shopping.POLL_SECONDS):
            return


def shopping_lookup_loop():
    """Automatic shopping list off: the list of the shopping tools is looked for once Home Assistant runs (#74)."""
    while True:
        try:
            if shopping.locate(ha, cfg):
                return
        except Exception as e:  # noqa: BLE001  Home Assistant cannot be asked yet
            log.debug("Shopping list: looking for it failed, tried again in a minute: %s", e)
        if stopping.wait(shopping.POLL_SECONDS):
            return


def rejected_busy(call, number: str, caller):
    """An incoming call while the line is busy (another call, a reconnect): denied, but logged, reported as a
    braincall_call event and notified like a summary would be (#25)."""
    callback = task_store is not None and task_store.has_callback(number)
    log.warning("Incoming call from %s (%s) rejected (line busy)%s", number, caller_label(caller, number) or "unknown",
                ": a task waits for a call back from this number, it stays waiting" if callback else "")
    try:
        call.deny()
    except Exception:  # noqa: BLE001
        pass
    try:
        report(False, number, time.time(), message="rejected: line busy", caller=caller)
        if for_owner(caller):
            lang = cfg.language if cfg.language in MISSED_TITLE else "de"
            label = caller_label(caller, number)
            title = MISSED_TITLE[lang].format(who=f"{label} ({number})" if label else number)
            for service in cfg.notify_services or []:
                ha.notify(str(service), title, MISSED_TEXT[lang])
            owner_inbox.add(number, "missed call: the line was busy, no message", name=caller_name(caller),
                            contact=(caller.get("id") or "") if caller and caller_name(caller) else "",
                            kind=inbox.MISSED)
    except Exception:  # noqa: BLE001
        log.exception("Reporting the rejected call from %s failed", number)


def on_incoming(call):
    try:
        number = call.request.headers["From"]["number"] or ""
    except Exception:  # noqa: BLE001
        number = ""
    caller = find_caller(number)
    task = None
    reason = ""
    if not norm(number):
        reason = "withheld number"
    elif caller is None and (task_store is None or not task_store.has_callback(number)):
        reason = "not in callers"
    if reason:
        log.warning("Incoming call from %s rejected (%s)", number or "unknown", reason)
        try:
            call.deny()
        except Exception:  # noqa: BLE001
            pass
        report(False, number or "unknown", time.time(), message=f"rejected: {reason}")
        return
    if not busy.acquire(blocking=False):
        rejected_busy(call, number, caller)
        return
    started = time.time()
    session = None
    set_line(RINGING, number, caller, "in")
    if task_store is not None:  # someone we tried to reach for a task calls back: the task goes on in this call
        task = task_store.claim_callback(number, datetime.now(timezone.utc))
    rights = caller["access"] if caller else GUEST
    name = caller_name(caller)
    instructions = caller["instructions"] if caller else ""
    if task:
        phone = config.phone_of(tasks.contact_of(cfg, task), number, cfg.country_code)
        instructions = (instructions + " " + tasks.callback_note(task, OWNER_FIRST, phone)).strip()
    access = ",".join(sorted(rights))
    try:
        log.info("Answering call from %s (%s, access %s%s)", number, caller_label(caller, number) or "unknown", access,
                 f", calling back for task {task['id']}" if task else "")
        call.answer()
        set_line(IN_CALL, number, caller, "in")
        session = CallSession(call, brain, speech, cfg, phrase_cache)
        session.run(outbound=False, caller=number, access=rights,
                    language=(task or {}).get("language") or lang_for(caller),
                    caller_name=name, instructions=instructions,
                    previous=previous(number, rights, caller), web=web_for(caller),
                    grants=(task or {}).get("grants"))
    except Exception:  # noqa: BLE001
        log.exception("Incoming call failed")
    finally:
        set_line(IDLE)
        finish(True, number, started, session, f"incoming (access {access})", caller, inbound=True,
               for_task=bool(task))
        busy.release()
        if task:
            threading.Thread(target=settle_task, args=(task, session), name="task", daemon=True).start()


_orig_parse = None  # pyVoIP's own parse_message, set in main()


def logged_parse(msg):
    """In front of pyVoIP's response handling: pyVoIP silently swallows error responses (403, 404, 486, 488 …)
    and gets CANCEL/late answers wrong."""
    if msg.type == SIPMessageType.MESSAGE:
        return _orig_parse(msg)
    try:
        code = int(msg.status)
        cid = msg.headers.get("Call-ID", "")
        cseq = msg.headers.get("CSeq", {})
        method = cseq.get("method", "") if isinstance(cseq, dict) else ""
    except Exception:  # noqa: BLE001
        return _orig_parse(msg)
    if method != "REGISTER" and code != 100:
        # the steps (183, 200 OK, the auth challenge, 487 after CANCEL) at trace; a refusal (486, 403 …) at info
        level = logfilter.TRACE if code < 400 or code in (401, 407, 487) else logging.INFO
        log.log(level, "SIP %s %s (%s)", code, msg.status.phrase, method or "?")
    if method == "INVITE" and code < 200:
        return None  # 180/183: nothing to do (pyVoIP would only print "TODO: Add 500 Error …" for 183)
    if method == "CANCEL":
        return None  # pyVoIP would take the 200 OK for an answered call
    if method == "BYE" and code >= 200 and fritz_sip.bye_confirmed(phone.sip, cid):
        return None
    if method == "INVITE" and code == 200:
        with answer_lock:  # see outbound(): the call is either still ours or already given up
            if cid in abandoned:
                return fritz_sip.end_late_answer(phone.sip, msg)
            return _orig_parse(msg)
    if method == "INVITE" and code >= 300 and code not in (401, 407):
        sip_failed[cid] = f"{code} {msg.status.phrase}"
        # pyVoIP only acknowledges 404/503 (and crashes on them while dialing): ACK every final error here
        phone.sip.out.sendto(phone.sip.gen_ack(msg).encode("utf8"), (phone.sip.server, phone.sip.port))
        return None
    return _orig_parse(msg)


def sip_status() -> PhoneStatus:
    return phone.get_status() if phone is not None else PhoneStatus.INACTIVE


def connect(old=None):
    """Sets up a new SIP phone and registers it, replacing `old`.
    pyVoIP gives up for good after 3 failed registrations (FRITZ!Box rebooting or down) and never tries again."""
    global phone, _orig_parse
    local_ip = cfg.local_ip or detect_local_ip(cfg.sip_server)  # no route while the network is down: retried
    log.info("Local IP %s, SIP %s@%s:%s, local SIP port %s",
             local_ip, cfg.sip_user, cfg.sip_server, cfg.sip_port, cfg.sip_local_port)
    new = VoIPPhone(cfg.sip_server, int(cfg.sip_port), cfg.sip_user, cfg.sip_password,
                    myIP=local_ip, callCallback=on_incoming, sipPort=int(cfg.sip_local_port),
                    rtpPortLow=int(cfg.rtp_port_min), rtpPortHigh=int(cfg.rtp_port_max))
    if old is not None:
        stopper = threading.Thread(target=old.stop, daemon=True)
        stopper.start()
        stopper.join(UNREGISTER_TIMEOUT)
        old.sip._close_sockets()  # stop() may give up before closing them, the SIP port must be free again
    with phone_lock:
        if stopping.is_set():
            return  # shutting down: the phone to stop has already been picked
        phone = new
        _orig_parse = phone.sip.parse_message
        phone.sip.parse_message = logged_parse
        fritz_sip.apply(phone)
    # a stop during registering closes the socket, the registration then fails without retrying
    new.start()
    if stopping.is_set():
        new.stop()  # shutdown picked this phone before it was started, its stop() did nothing


def watchdog():
    """Sets the phone up again while it stays unregistered for RECONNECT_AFTER seconds, never during a call.
    Every round is noted in watchdog_seen: a loop that hangs (e.g. pyVoIP stuck in a reconnect) makes /health 503."""
    global watchdog_seen
    lost = None
    watchdog_seen = time.monotonic()
    while not stopping.wait(WATCHDOG_INTERVAL):
        watchdog_seen = time.monotonic()
        if sip_status() == PhoneStatus.REGISTERED:
            lost = None
            note_sip()
            continue
        if lost is None:
            lost = time.monotonic()  # also pyVoIP's own re-registration every 2 min: registered again next round
            continue
        note_sip()  # unregistered for a second round: no re-registration, braincall_state says so
        if time.monotonic() - lost < RECONNECT_AFTER or not busy.acquire(blocking=False):
            continue
        try:
            log.warning("SIP not registered for %s min (%s), reconnecting",
                        RECONNECT_AFTER // 60, sip_status().value)
            connect(old=phone)
            log.info("SIP status: %s", sip_status().value)
            note_sip()
        except Exception as e:  # noqa: BLE001
            log.warning("Reconnecting failed: %s - next try in %s min", e, RECONNECT_AFTER // 60)
        finally:
            lost = time.monotonic()  # next try RECONNECT_AFTER from now
            busy.release()


def problems() -> list:
    """Why the process is not healthy (GET /health answers 503 then, #37): the SIP watchdog loop stopped or hangs.
    SIP not registered is none: the watchdog reconnects, a restart would not help while the FRITZ!Box is down."""
    found = []
    if sip_watchdog is not None and not stopping.is_set():
        idle = time.monotonic() - watchdog_seen
        if not sip_watchdog.is_alive():
            found.append("SIP watchdog stopped")
        elif idle > WATCHDOG_STALL:
            found.append(f"SIP watchdog stuck for {int(idle)} s")
    return found


def health() -> tuple[int, dict]:
    """GET /health: 200 while healthy, 503 with the problems; the status fields in both cases."""
    found = problems()
    return (503 if found else 200), {"status": "unhealthy" if found else "ok", "problems": found,
                                     "sip": sip_status().value, "busy": busy.locked(), "line": line["line"],
                                     "phone": PHONE, "time": datetime.now().isoformat(timespec="seconds")}


def probe_api() -> tuple[int, dict]:
    """GET /health on the ingress port, which the Supervisor's watchdog asks (braincall/config.yaml): it listens on
    the Supervisor's network on a fixed port, whatever http_port is and even if the API listens on 127.0.0.1 only.
    It asks the API's own /health, so an HTTP API that no longer answers counts as unhealthy too. While the app
    starts (SIP is set up before the API listens) it is healthy for STARTUP_GRACE seconds."""
    if not api_up.is_set():
        waited = time.monotonic() - started
        if waited < STARTUP_GRACE:
            return 200, {"status": "starting", "problems": []}
        return 503, {"status": "unhealthy", "problems": [f"HTTP API not started after {int(waited)} s"]}
    conn = http.client.HTTPConnection("127.0.0.1", int(cfg.http_port), timeout=HEALTH_PROBE_TIMEOUT)
    try:
        conn.request("GET", "/health")
        r = conn.getresponse()
        body = json.loads(r.read() or b"{}")
        if r.status in (200, 503) and isinstance(body, dict):
            return r.status, body
        problem = f"HTTP API answered /health with {r.status}"
    except Exception as e:  # noqa: BLE001
        problem = f"HTTP API not answering on port {cfg.http_port}: {e}"
    finally:
        conn.close()
    return 503, {"status": "unhealthy", "problems": [problem]}


PAGE = Path(__file__).resolve().parent / "web" / "contacts.html"
INGRESS_PROXY = "172.30.32.2"   # the Supervisor's ingress proxy: the only client of the contacts page in Home Assistant
INGRESS_HOST = "172.30.32.1"    # the host on the Supervisor's network: the ingress port listens only there
_admins: dict = {}              # Home Assistant user id -> (time, is admin)
_choices = [0.0, {}]            # time, persons and devices for the contacts page (Home Assistant, cached)


def ingress_admin(user_id: str) -> bool:
    """Whether a Home Assistant user may use the contacts page through ingress: administrators only. Home
    Assistant shows the page to administrators only (panel_admin); this keeps out others who open the address
    directly, since the page can give admin rights. Unknown (the token cannot list users): refused, not cached."""
    now = time.monotonic()
    known = _admins.get(user_id)
    if known and now - known[0] < 300:
        return known[1]
    result = ha.user_admin(user_id) if user_id and ha.enabled else None
    if result is None:
        if "warned" not in _admins:
            _admins["warned"] = (now, False)
            log.warning("Contacts page: cannot check whether the Home Assistant user is an administrator (the token "
                        "of Home Assistant must belong to an administrator to list users); the page refuses until "
                        "it can. It is also at /contacts on the API port, with the API token")
        return False
    _admins[user_id] = (now, result)
    return result


def contact_choices() -> dict:
    """Persons and devices of Home Assistant for the contacts page, read at most once a minute."""
    if ha.enabled and time.monotonic() - _choices[0] > 60:
        _choices[:] = [time.monotonic(), ha.people_and_devices()]
    return _choices[1] or {"users": [], "devices": []}


class Handler(BaseHTTPRequestHandler):
    ingress = False  # IngressHandler: the contacts page inside Home Assistant (no token, Home Assistant logged in)

    def _send(self, code: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        """Every request needs the token, also from 127.0.0.1 (other apps on the host reach that too)."""
        return hmac.compare_digest(self.headers.get("Authorization", "").encode(),
                                   f"Bearer {API_TOKEN}".encode())

    def _body(self) -> dict | None:
        """The JSON object of a request; None after an error was sent."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError("negative Content-Length")
            if length > MAX_BODY:
                self._send(413, {"error": "body too large"})
                return None
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("not an object")
            return body
        except Exception:  # noqa: BLE001
            self._send(400, {"error": "invalid json"})
            return None

    def _contacts(self) -> bool:
        """The contacts page and its API (#93). True if the request was one of them (and is answered).
        GET /contacts (in Home Assistant: /) the page; GET /api/contacts the contacts with the choices for the form;
        POST /api/contacts a new one; PUT /api/contacts/<id> a change; DELETE /api/contacts/<id>;
        POST /api/contacts/<id>/merge {"other": id} merges another contact into this one."""
        path = urlsplit(self.path).path
        if self.command == "GET" and (path == "/contacts" or (self.ingress and path in ("/", "/index.html"))):
            try:
                data = PAGE.read_bytes()
            except OSError:
                self._send(404, {"error": "page not found"})
                return True
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return True
        if not (path == "/api/contacts" or path.startswith("/api/contacts/")):
            return False
        allowed = ingress_admin(self.headers.get("X-Remote-User-Id", "")) if self.ingress else self._authorized()
        if not allowed:
            self._send(403 if self.ingress else 401, {"error": "unauthorized"})
            return True
        parts = path[len("/api/contacts"):].strip("/").split("/")
        cid = parts[0] if parts[0] else ""
        if self.command == "GET" and not cid:
            contact_store.refresh()
            items = contact_store.all()
            for c in items:
                c["in_use"] = contact_in_use({"numbers": config.entry_numbers(c)})
            self._send(200, {"contacts": items, "system_language": cfg.language or "de",
                             "web_access": bool(cfg.web_access), "phrase_languages": sorted(phrases.TEXTS),
                             **contact_choices()})
            return True
        if self.command == "DELETE" and cid and len(parts) == 1:
            why = contact_store.delete(cid)
            if why == "not_found":
                self._send(404, {"error": "not_found"})
            elif why:
                self._send(409, {"error": "in_use", "task": why})
            else:
                self._send(200, {"deleted": cid})
            return True
        if self.command not in ("POST", "PUT"):
            self._send(405, {"error": "method not allowed"})
            return True
        body = self._body()
        if body is None:
            return True
        if self.command == "POST" and cid and parts[1:] == ["merge"]:
            merged, why = contact_store.merge(cid, str(body.get("other") or ""))
            if merged is None:
                self._send(404 if why == "not_found" else 409,
                           {"error": "not_found"} if why == "not_found" else {"error": "in_use", "task": why})
            else:
                self._send(200, merged)
            return True
        if (self.command == "POST" and cid) or (self.command == "PUT" and (not cid or len(parts) > 1)):
            self._send(404, {"error": "not found"})
            return True
        stored, errors = contact_store.save(body, cid, origin="page")
        if errors:
            self._send(404 if errors[0]["code"] == "not_found" else 400, {"errors": errors})
        else:
            self._send(201 if self.command == "POST" else 200, stored)
        return True

    def _route(self):
        """/api/tasks and /api/shopping/… are the paths since 1.11; the ones without /api still work."""
        if self.path.startswith(("/api/tasks", "/api/shopping/")):
            self.path = self.path[len("/api"):]

    def _converse(self, body: dict):
        """POST /api/converse (Brain Assist): one Assist request, answered as a stream of JSON lines
        (assist.converse). No Content-Length: the stream ends when the connection is closed."""
        try:
            req = assist.parse(body)
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        gone = threading.Event()

        def emit(event: dict):
            if gone.is_set():
                return
            try:
                self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()
            except OSError:  # Brain Assist closed the connection: the turn stops at the next check
                gone.set()

        try:
            assist.converse(brain, cfg, conversations, req, emit, gone.is_set, OWNER_FIRST, recall)
        except Exception as e:  # noqa: BLE001  the headers are out: the reason goes into the stream
            log.exception("Brain Assist request failed")
            emit({"type": "error", "message": f"{type(e).__name__}: {e}"[:300]})

    def _messages(self) -> bool:
        """The owner's messages (#104) for Brain Assist's call list (brainassist#9). True if the request was one of
        them (and is answered). GET /api/messages?days=7&open=1: the entries, newest first, with the counts;
        PATCH /api/messages/<id> {"heard": true|false, "done": true|false}; DELETE /api/messages/<id>."""
        path = urlsplit(self.path).path
        if not (path == "/api/messages" or path.startswith("/api/messages/")):
            return False
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return True
        entry_id = path[len("/api/messages/"):] if path.startswith("/api/messages/") else ""
        if self.command == "GET" and not entry_id:
            query = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
            try:
                days = float(query["days"]) if query.get("days") else None
            except ValueError:
                days = -1.0
            if days is not None and not (math.isfinite(days) and 0 < days <= inbox.OPEN_DAYS):
                self._send(400, {"error": f"days must be a number from 0 to {inbox.OPEN_DAYS}"})
                return True
            items = owner_inbox.recent(days) if days is not None else owner_inbox.entries()
            if query.get("open") in ("1", "true", "yes"):
                items = [e for e in items if inbox.is_open(e)]
            self._send(200, {"messages": items[::-1], **owner_inbox.counts()})
            return True
        if not entry_id or self.command not in ("PATCH", "DELETE"):
            self._send(404 if not entry_id else 405, {"error": "not found" if not entry_id else "method not allowed"})
            return True
        if self.command == "DELETE":
            gone = owner_inbox.delete(entry_id)
            self._send(200 if gone else 404, {"deleted": entry_id} if gone else {"error": "not found"})
            return True
        body = self._body()
        if body is None:
            return True
        change = {k: body[k] for k in ("heard", "done") if k in body}
        if not change or not all(isinstance(v, bool) for v in change.values()):
            self._send(400, {"error": "give heard and/or done as true or false"})
            return True
        entry = owner_inbox.update(entry_id, **change)
        self._send(200 if entry else 404, entry or {"error": "not found"})
        return True

    def _task_id(self) -> str:
        """The id in /tasks/<id>; "" for other paths."""
        return self.path[len("/tasks/"):] if self.path.startswith("/tasks/") and task_store is not None else ""

    def _shopping_history(self):
        """GET /shopping/history?item=Milch&days=30&event=bought: events of the shopping history."""
        query = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
        try:
            days = float(query["days"]) if query.get("days") else None
        except ValueError:
            days = -1.0
        if days is not None and not (math.isfinite(days) and 0 < days <= 36500):
            return self._send(400, {"error": "days must be a number from 0 to 36500"})
        self._send(200, {"list": shopping_list.entity_id,
                         "events": shopping_list.events(query.get("item", ""), days, query.get("event", ""))})

    def do_GET(self):  # noqa: N802
        if self._contacts() or self._messages():
            return
        self._route()
        if self.path == "/health":  # the only path without token: nothing but the status (503: unhealthy, #37)
            return self._send(*health())
        if self.path == "/api/version":
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {"api": assist.API_VERSION, "version": config.version(), "phone": PHONE,
                                    "assistant": cfg.assistant_name or "Claude",
                                    "home_assistant": bool(ha.enabled), "tasks": task_store is not None,
                                    "line": True, "messages": True})
        if self.path == "/api/status":  # the line with the current call (#100); /health has no number or name
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {**line_status(), "phone": PHONE})
        if urlsplit(self.path).path == "/shopping/history" and shopping_list is not None:
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            return self._shopping_history()
        if self.path == "/shopping/forecast" and shopping_list is not None and shopping_list.rhythm is not None:
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            try:
                products = shopping_list.rhythm.forecast()
            except Exception as e:  # noqa: BLE001
                return self._send(503, {"error": f"rhythm list not readable: {e}"})
            return self._send(200, {"list": shopping_list.rhythm.entity_id, "products": products})
        if task_store is None or not (self.path == "/tasks" or self._task_id()):
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/tasks":
            return self._send(200, {"tasks": task_store.all()})
        task = task_store.get(self._task_id())
        self._send(200 if task else 404, task or {"error": "not found"})

    def _forget(self):
        """DELETE /api/memory?number=… | ha_user=… | ha_device=…: forgets what is remembered of a person."""
        query = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
        whom = {k: query.get(k, "") for k in memory.KEYS}
        if not any(whom.values()):
            return self._send(400, {"error": "say whom to forget: number, ha_user or ha_device"})
        forgotten = sum(calls.forget(alias) for alias in same_person(whom))
        log.info("Memory: %d summaries forgotten for %s", forgotten,
                 ", ".join(f"{k} {v}" for k, v in whom.items() if v))
        self._send(200, {"forgotten": forgotten})

    def do_PATCH(self):  # noqa: N802
        if not self._messages():
            self._send(404, {"error": "not found"})

    def do_PUT(self):  # noqa: N802
        if not self._contacts():
            self._send(404, {"error": "not found"})

    def do_DELETE(self):  # noqa: N802
        if self._contacts() or self._messages():
            return
        self._route()
        if urlsplit(self.path).path == "/api/memory":
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            return self._forget()
        if not self._task_id():
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        task_id = self._task_id()
        error = task_store.cancel(task_id)
        if error:
            return self._send(404 if error == "not found" else 409, {"error": error})
        self._send(200, {"status": "cancelled"})
        report_task(task_store.get(task_id))  # notification and event like every finished task, no call back

    def _plan_task(self, body: dict):
        """POST /tasks: plans a call with a task. Without a plan (goal) Claude makes one from the task text.
        The callee is "number", or "name" alone for a person from callers. The requester is a person: "requester"
        (a phone number), or "requester_user_id" / "requester_user_name" (Brain Assist: the Home Assistant user
        who asked); without any of them the owner."""
        number, name, refusal = tasks.callee(cfg, body)
        if refusal:
            return self._send(400, {"error": refusal.removeprefix("Not planned: ")})
        refusal = tasks.number_refusal(cfg, number)
        if refusal:
            return self._send(400, {"error": refusal})
        if task_store.blocked(number) and not body.get("override_do_not_call"):
            return self._send(409, {"error": "this number asked not to be called again (override_do_not_call)"})
        if not str(body.get("task") or "").strip():
            return self._send(400, {"error": "no task"})
        caller = find_caller(number, wildcard=False)
        name = name or caller_name(caller)
        fixed = {**body, "number": number, **({"name": name} if name else {})}  # no name: the plan may find one
        if not body.get("goal"):
            try:
                fixed = {**brain.plan(fixed["task"], number, fixed.get("name", "")), **fixed}  # what was given wins
            except Exception as e:  # noqa: BLE001
                log.warning("Planning the task failed: %s", e)
                return self._send(502, {"error": f"planning failed: {e}"})
        asked = {k: str(body.get(k) or "").strip() for k in ("requester", "requester_user_id", "requester_user_name")}
        requester = tasks.requester_entry(cfg, asked["requester"], asked["requester_user_id"],
                                          asked["requester_user_name"])
        if not any(asked.values()):  # an automation or script: the owner asks
            requester = {"number": "", "name": OWNER, "ha_user": ""}
        elif asked["requester"] and not (requester["number"] or asked["requester_user_id"]
                                         or asked["requester_user_name"]):  # a phone number that is not listed
            requester = {"number": "", "name": OWNER, "ha_user": ""}
        task, error = tasks.build(cfg, fixed, requester, datetime.now(tasks._tz(cfg)))
        if task is None:
            return self._send(400, {"error": error})
        if task["previous"]:  # continues a finished task of the same contact: its history goes along (#92)
            earlier, refusal = tasks.earlier_task(cfg, task, task_store.get(task["previous"]))
            if refusal:
                return self._send(400, {"error": refusal.removeprefix("Not planned: ")})
            task["earlier"] = earlier
        if caller is None and not task["name"]:  # every task has a contact (#92), and a contact a name
            return self._send(400, {"error": "the number belongs to no contact: give the name of the person or "
                                             "business (it is saved as a contact)"})
        new_contact, refusal = brain.contact_for_task(task, caller is not None)
        if refusal:  # e.g. the name belongs to another contact: refused like by voice, nothing stored
            return self._send(409, {"error": refusal.removeprefix("Not planned: ")})
        if body.get("override_do_not_call"):
            task_store.unblock(number)
        if new_contact:  # saved first: the task points to it (#92)
            saved, errors = contact_store.save(new_contact, origin="task")
            if saved:
                task["contact_id"] = saved["id"]
            else:
                log.info("Contact for task %s not saved: %s", task["id"], contacts.problems(errors))
        self._send(201, task_store.add(task))

    def do_POST(self):  # noqa: N802
        if self._contacts():
            return
        self._route()
        if self.path not in ("/call", "/api/converse") and not (self.path == "/tasks" and task_store is not None):
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError("negative Content-Length")  # rfile.read(-1) would read until the client closes
            if length > MAX_BODY:
                return self._send(413, {"error": "body too large"})
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("not an object")
        except Exception:  # noqa: BLE001
            return self._send(400, {"error": "invalid json"})
        if self.path == "/tasks":
            return self._plan_task(body)
        if self.path == "/api/converse":
            return self._converse(body)
        if not PHONE:
            return self._send(503, {"error": "no phone: sip_user and sip_password are not set"})
        number = str(body.get("number") or cfg.default_number).strip()
        if not number:
            return self._send(400, {"error": "no number"})
        refusal = tasks.number_refusal(cfg, number)
        if refusal:
            return self._send(400, {"error": refusal})
        if sip_status() != PhoneStatus.REGISTERED:
            return self._send(503, {"error": f"SIP not registered ({sip_status().value})"})
        if not busy.acquire(blocking=False):
            return self._send(409, {"error": "line busy"})
        threading.Thread(target=outbound, args=(number, str(body.get("message") or "")), daemon=True).start()
        self._send(202, {"status": "dialing", "number": number})

    def log_request(self, code="-", size="-"):
        """A GET that worked is a poll (e.g. /api/tasks once a minute, /health): trace. What changes something, and
        every refusal or error: debug (#77)."""
        code = int(code) if isinstance(code, int) else code  # HTTPStatus.OK -> 200
        polled = self.command == "GET" and isinstance(code, int) and code < 400
        log.log(logfilter.TRACE if polled else logging.DEBUG, 'HTTP "%s" %s %s', self.requestline, code, size)

    def log_message(self, fmt, *args):
        log.debug("HTTP " + fmt, *args)


class IngressHandler(Handler):
    """The contacts page inside Home Assistant (ingress): only the page and its API, only from the Supervisor's
    ingress proxy, which has logged the user in. Everything else of the API needs the token on the main port.
    Besides: GET /health for the Supervisor's watchdog (probe_api), from any client, it is only the status."""
    ingress = True

    def _only_page(self):
        if self.command == "GET" and self.path == "/health":
            return self._send(*probe_api())
        if self.client_address[0] != INGRESS_PROXY:
            return self._send(403, {"error": "only through Home Assistant"})
        if not self._contacts():
            self._send(404, {"error": "not found"})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _only_page


def check_ha_admin():
    """Warns once at startup if the token cannot read automation definitions (needs an administrator)."""
    if ha.is_admin() is False:
        log.warning("Home Assistant: the token does not belong to an administrator. Claudine cannot read the "
                    "definitions and descriptions of automations and scripts (ha_automation, ha_automations); "
                    "use a long-lived token of an administrator, e.g. of a separate user for BrainCall.")


def incomplete() -> list:
    """Settings that are missing to start. Claude is always needed. Without the SIP account (both sip_user and
    sip_password empty) there is no phone and only the HTTP API runs (Brain Assist); the phone also needs OpenAI
    for speech."""
    needed = ["anthropic_api_key"]
    if cfg.sip_user or cfg.sip_password:
        needed += ["sip_user", "sip_password", "openai_api_key"]
    return [key for key in needed if not cfg[key]]


def start_line() -> str:
    """The first line of every start: which version runs (also when the configuration is incomplete)."""
    return f"BrainCall {config.version() or '(version unknown)'} starting, Python {sys.version.split()[0]}"


def log_start():
    """The start line as info, also with log_level warning: it bypasses the level, not the handlers' filters."""
    log.handle(log.makeRecord(log.name, logging.INFO, __file__, 0, start_line(), None, None))


def main():
    global sip_watchdog
    console.banner(cfg)
    log_start()
    missing = incomplete()
    if missing:
        log.error("Configuration incomplete: %s missing (config.json)", ", ".join(missing))
        sys.exit(1)
    if not OWNER:
        log.warning("owner_name is not set (config.json) - the assistant will speak of \"the owner\"")

    log.info("Config: %s | logs: %s", config.CONFIG_FILE[0], _LOGDIR)
    try:
        log.info("Contacts: %s", contact_store.setup())
        if task_store is not None and task_store.reconcile_contacts():
            log.info("Tasks: contacts found for tasks stored before 1.16.0")
    except OSError as e:
        log.error("Contacts: %s could not be written (%s); the callers from the settings are used",
                  contact_store.path, e)
    for w in config.WARNINGS:
        log.warning(w)
    log.info("Caller memory: %s", calls.describe() + (f" ({calls.path})" if calls.enabled else ""))
    log.info("Callers: %s", ", ".join(
        f"{c['name'] or c['number'] or c['ha_user'] or c['ha_device'] or c['email']} ({','.join(sorted(c['access']))})"
        for c in cfg.callers) or "none, incoming calls and Brain Assist requests are rejected")
    with_assist = [c["name"] or c["ha_user"] or c["ha_device"] for c in cfg.callers if c["ha_user"] or c["ha_device"]]
    log.info("Brain Assist: %s", ", ".join(with_assist) or "nobody (no contact with ha_user or ha_device)")
    if task_store is not None:
        span = tasks.hours(cfg)
        log.info("Planned calls: on, %s, at most %s per day, %d open (%s)",
                 f"{span[0]:%H:%M}-{span[1]:%H:%M}" if span else "at any time",
                 cfg.outbound_max_calls_per_day or "any number",
                 sum(t["status"] == tasks.PLANNED for t in task_store.all()), task_store.path)
        if str(cfg.outbound_hours or "").strip() and span is None:
            log.warning("outbound_hours %r not understood (e.g. 09:00-20:00): no time limit", cfg.outbound_hours)
    else:
        log.info("Planned calls: off (%s)", "tasks" if PHONE else "no phone")
    if cfg.web_access:
        with_web = [c["name"] or c["number"] or c["ha_user"] or c["ha_device"] for c in cfg.callers
                    if web.allowed(cfg, c["access"], web_for(c))]
        log.info("Web search: on for %s", ", ".join(with_web) or "nobody (access admin/read or \"web\": true)")
    else:
        log.info("Web search: off (web_access)")
    server = None

    def on_sigterm(signum, frame):  # Home Assistant stops the add-on with SIGTERM
        log.info("Stop requested")
        stopping.set()
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()
        else:
            raise KeyboardInterrupt  # pyVoIP swallows it while registering; `stopping` covers that case

    # before registering: a stop during startup must unregister from the FRITZ!Box as well
    signal.signal(signal.SIGTERM, on_sigterm)

    def quit_():
        log.info("Quit by key press")
        threading.Thread(target=server.shutdown, daemon=True).start()

    def test_call():
        number = str(cfg.default_number or "").strip()
        if not number:
            log.warning("No default_number in config.json")
        elif sip_status() != PhoneStatus.REGISTERED:
            log.warning("SIP not registered (%s)", sip_status().value)
        elif not busy.acquire(blocking=False):
            log.warning("Line busy")
        else:
            hello = f"Briefly say hello to {OWNER_FIRST}." if OWNER_FIRST else "Briefly say hello."
            threading.Thread(target=outbound, args=(number, "Test call by key press. " + hello),
                             daemon=True).start()

    def open_log():
        try:
            os.startfile(str(_LOGDIR / "braincall.log"))  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            log.warning("Opening the log failed: %s", e)

    try:
        # as an app: the contacts page inside Home Assistant (ingress) and the watchdog's /health; before SIP, so
        # the watchdog sees the app starting while the FRITZ!Box takes its time
        if cfg.supervisor_token:
            start_ingress()
        if PHONE:
            try:
                connect()
            except Exception as e:  # noqa: BLE001  e.g. FRITZ!Box or network not up yet after a power cut: the watchdog retries
                log.warning("SIP start failed: %s", e)
            for _ in range(50):
                if sip_status() == PhoneStatus.REGISTERED or stopping.is_set():
                    break
                time.sleep(0.2)
            if stopping.is_set():
                return
            log.info("SIP status: %s", sip_status().value)
            note_sip()
        else:
            log.info("Phone: off (sip_user and sip_password not set), only the HTTP API runs (Brain Assist)")
        log.info("Home Assistant: %s", "connected" if ha.enabled else "not configured")
        if ha.enabled:  # in the background: Home Assistant may still be starting
            threading.Thread(target=check_ha_admin, name="HA admin check", daemon=True).start()
        if PHONE:
            # greetings and fillers synthesized ahead, so the caller hears them without waiting for TTS
            threading.Thread(target=phrase_cache.prewarm, args=(phrases.wanted(cfg),), name="phrase cache",
                             daemon=True).start()
            sip_watchdog = threading.Thread(target=watchdog, name="SIP watchdog", daemon=True)
            sip_watchdog.start()
        if task_store is not None:
            threading.Thread(target=task_loop, name="planned calls", daemon=True).start()
        threading.Thread(target=assist_loop, name="assist conversations", daemon=True).start()
        threading.Thread(target=contacts_loop, name="contacts", daemon=True).start()
        if shopping_list is not None:
            log.info("Shopping list: %s", shopping_list.describe())
            threading.Thread(target=shopping_loop, name="shopping list", daemon=True).start()
        elif not cfg.shopping_auto:
            log.info("Shopping list: automatic shopping list off (shopping_auto)")
            if ha.enabled and cfg.shopping_list:
                threading.Thread(target=shopping_lookup_loop, name="shopping list lookup", daemon=True).start()

        # a token set in the configuration opens the API to the LAN; a generated one keeps it on this host
        bind = "127.0.0.1" if TOKEN_GENERATED else "0.0.0.0"
        server = ThreadingHTTPServer((bind, int(cfg.http_port)), Handler)
        log.info("HTTP API on http://%s:%s, %s", bind, cfg.http_port,
                 f"only from this host, token in {_LOGDIR / config.TOKEN_FILE}" if TOKEN_GENERATED
                 else "reachable in the network, token from the configuration (api_token)")
        # as an app: tell Home Assistant where the API is, so Brain Assist needs no address or token typed
        threading.Thread(target=discovery.announce, args=(cfg, API_TOKEN), name="discovery", daemon=True).start()
        console.start_keys(quit_, test_call, open_log)
        api_up.set()  # listening since the server was made: requests wait in the queue until serve_forever
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down …")
        stopping.set()  # ends the watchdog
        if server is not None:
            server.server_close()
        with phone_lock:  # held only briefly by connect(): after this no other phone is swapped in
            last = phone
        if last is not None:
            unregister(last)


def unregister(last: VoIPPhone):
    # pyVoIP waits up to 30 s per step for the FRITZ!Box to confirm; do not let that block the shutdown
    stopper = threading.Thread(target=last.stop, daemon=True)
    stopper.start()
    stopper.join(UNREGISTER_TIMEOUT)
    if stopper.is_alive():
        log.warning("FRITZ!Box did not confirm the unregistration within %ss. Bye.", UNREGISTER_TIMEOUT)
        logging.shutdown()
        os._exit(0)  # pyVoIP's own threads would otherwise keep the process alive
    log.info("Unregistered. Bye.")


phone: VoIPPhone | None = None

if __name__ == "__main__":
    main()
