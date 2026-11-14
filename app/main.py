"""BrainCall: SIP phone on the FRITZ!Box plus an HTTP trigger for outbound calls."""
import hmac
import json
import logging
import logging.handlers
import os
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

import config  # noqa: E402
import fritz_sip  # noqa: E402
import memory  # noqa: E402
import phrases  # noqa: E402
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
logging.basicConfig(level=LOG_LEVEL, handlers=[_con, _file])
# other libraries stay at info at most: their debug output would bury BrainCall's own
for _name in ("urllib3", "requests", "httpx", "httpcore", "anthropic", "openai", "websocket", "hpack",
              "charset_normalizer", "asyncio"):
    logging.getLogger(_name).setLevel(max(LOG_LEVEL, logging.INFO))
log = logging.getLogger("main")
pyVoIP.DEBUG = False


def _excepthook(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        return
    log.critical("Unhandled error", exc_info=(exc_type, exc, tb))


sys.excepthook = _excepthook
threading.excepthook = lambda a: _excepthook(a.exc_type, a.exc_value, a.exc_traceback)

console.ASSISTANT = cfg.assistant_name or "Claude"
ha = HA(cfg)
speech = Speech(cfg)
brain = Brain(cfg, ha)
phrase_cache = phrases.PhraseCache(speech, cfg, _LOGDIR / "cache")
calls = memory.CallMemory(_LOGDIR / "calls.jsonl", memory.parse_days(cfg.caller_memory), cfg.country_code)
task_store = tasks.TaskStore(_LOGDIR / "tasks.json", cfg) if cfg.tasks else None
brain.tasks = task_store
shopping_list = (shopping.Shopping(cfg, ha, _LOGDIR / "shopping-history.jsonl")
                 if cfg.shopping_auto and ha.enabled and cfg.shopping_list else None)
brain.shopping = shopping_list
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
phone_lock = threading.Lock()  # swapping the phone (connect) vs. picking the one to stop on shutdown


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
TASK_TITLE = {"de": "Auftrag Anruf bei {who}: {status}", "en": "Task call to {who}: {status}",
              "uk": "Завдання, дзвінок до {who}: {status}"}
STATUS_WORDS = {"de": {"done": "erledigt", "partial": "teilweise erledigt", "failed": "nicht geklappt",
                       "not_reached": "nicht erreicht", "message_left": "Nachricht hinterlassen",
                       "cancelled": "abgebrochen"}}


def find_caller(number: str, wildcard: bool = True) -> dict | None:
    """Entry from callers for a number; an exact number wins over "*". None = not listed."""
    fallback = None
    for c in cfg.callers:
        if c["number"] == "*":
            fallback = fallback or c
        elif same_number(c["number"], number):
            return c
    return fallback if wildcard else None


def caller_name(caller: dict | None) -> str:
    """Name of a listed number; "" for unknown callers (the "*" entry), they get asked for their name."""
    if not caller or caller["number"] == "*":
        return ""
    return caller["name"] or OWNER or caller["number"]


def web_for(caller: dict | None):
    """The caller's web setting for Brain: None = by access (admin, read), never for unknown callers ("*")."""
    if not caller or caller["number"] == "*":
        return False
    return caller.get("web")


def lang_for(caller: dict | None) -> str:
    """Language of the caller entry, otherwise the global setting."""
    return (caller or {}).get("language") or cfg.language or "de"


def previous(number: str, access) -> str:
    """What Claude remembers of earlier calls with this number, for the intro (memory.note)."""
    try:
        return memory.note(calls.recent(number, access), OWNER_FIRST or "the owner", cfg.timezone, calls.enabled)
    except Exception:  # noqa: BLE001  the memory must never stop a call
        log.exception("Reading the caller memory failed")
        return ""


def report(answered: bool, number: str, started: float, session=None, message="", caller=None, summary="",
           ended: float = 0.0):
    ha.fire_event("braincall_call", {
        "number": number,
        "answered": answered,
        "reason": message,
        "duration_s": round((ended or time.time()) - started),
        "transcript": "\n".join(session.transcript) if session else "",
        "caller_name": caller_name(caller),
        "access": ",".join(sorted(caller["access"])) if caller else "",
        "summary": summary,
    })


def after_call(answered: bool, number: str, started: float, ended: float, session, message: str, caller,
               inbound: bool):
    """Summary, event, notification and call note; runs after the line is free again."""
    try:
        access = caller["access"] if caller else GUEST
        summary = ""
        if session and session.caller_turns:
            lang = cfg.language if cfg.language in SUMMARY_TITLE else "de"
            name = caller_name(caller)
            try:
                summary = brain.summarize(session.transcript, f"{name} ({number})" if name else number,
                                          session.actions, lang, outbound=not inbound, mailbox=session.mailbox)
            except Exception as e:  # noqa: BLE001
                log.warning("Summary failed: %s", e)
            notify = caller.get("notify") if caller else None
            if inbound and (notify if notify is not None else "admin" not in access):
                title = SUMMARY_TITLE[lang].format(who=f"{name} ({number})" if name else number)
                text = summary or "\n".join(session.transcript)[-1500:]
                for service in cfg.notify_services or []:
                    ha.notify(str(service), title, text)
            if summary:
                log.info("Summary: %s", summary)
                try:
                    calls.add(number, summary, access, inbound, datetime.fromtimestamp(ended, timezone.utc))
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


def finish(answered: bool, number: str, started: float, session, message: str, caller, inbound: bool):
    threading.Thread(target=after_call, name="after call", daemon=True,
                     args=(answered, number, started, time.time(), session, message, caller, inbound)).start()


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
        session = CallSession(call, brain, speech, cfg, phrase_cache)
        access = caller["access"] if caller else GUEST
        session.run(outbound=True, message=message, caller=number,
                    language=(task or {}).get("language") or lang_for(caller),
                    access=access, caller_name=caller_name(caller),
                    instructions=caller["instructions"] if caller else "", previous=previous(number, access),
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
            "task": task["task"], "requester": task["requester"]["name"], "attempts": len(task["attempts"]),
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
    outbound(number, f"You call back with the result of the task “{task['goal']}” (call to "
                     f"{task['name'] or task['number']}). Report it briefly in your own words and answer questions "
                     f"about it. Result ({task['status']}): {text}")


def settle_task(task: dict, session):
    """After the call of a task (line already free): retry if nobody was reached, otherwise follow-up and result.
    A message left on a mailbox ends the task as message_left: it is reported and not dialed again."""
    try:
        now = datetime.now(timezone.utc)
        # a call back after a message was left: the task was reported as message_left already (TaskStore.not_reached)
        reported = (task.get("result") or {}).get("status") == tasks.LEFT
        if session is None or not session.caller_turns:
            done = task_store.not_reached(task["id"], "no answer" if session is None else "nothing said", now)
        elif session.mailbox and not session.message_left:
            done = task_store.not_reached(task["id"], "mailbox", now)  # nothing to follow up
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
            if status == "not_reached":
                done = task_store.not_reached(task["id"], "mailbox", now)
            elif status == tasks.LEFT:
                done = task_store.finish(task["id"], {**result, "status": status}, "message left")
            else:
                if result.get("do_not_call"):
                    task_store.block(task["number"], task["id"])
                done = task_store.finish(task["id"], result)
        if done["status"] in tasks.FINAL and not (reported and done["status"] == tasks.LEFT):
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
    log.info("Task %s: calling %s (%s), attempt %d", task["id"], task["name"] or "?", task["number"],
             len(task["attempts"]))
    settle_task(task, outbound(task["number"], tasks.brief(task, OWNER_FIRST), task))


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
        try:
            call.deny()
        except Exception:  # noqa: BLE001
            pass
        return
    started = time.time()
    session = None
    if task_store is not None:  # someone we tried to reach for a task calls back: the task goes on in this call
        task = task_store.claim_callback(number, datetime.now(timezone.utc))
    rights = caller["access"] if caller else GUEST
    name = caller_name(caller)
    instructions = caller["instructions"] if caller else ""
    if task:
        instructions = (instructions + " " + tasks.callback_note(task, OWNER_FIRST)).strip()
    access = ",".join(sorted(rights))
    try:
        log.info("Answering call from %s (%s, access %s%s)", number, name or "unknown", access,
                 f", calling back for task {task['id']}" if task else "")
        call.answer()
        session = CallSession(call, brain, speech, cfg, phrase_cache)
        session.run(outbound=False, caller=number, access=rights,
                    language=(task or {}).get("language") or lang_for(caller),
                    caller_name=name, instructions=instructions,
                    previous=previous(number, rights), web=web_for(caller), grants=(task or {}).get("grants"))
    except Exception:  # noqa: BLE001
        log.exception("Incoming call failed")
    finally:
        finish(True, number, started, session, f"incoming (access {access})", caller, inbound=True)
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
        log.info("SIP %s %s (%s)", code, msg.status.phrase, method or "?")
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
    """Sets the phone up again while it stays unregistered for RECONNECT_AFTER seconds, never during a call."""
    lost = None
    while not stopping.wait(WATCHDOG_INTERVAL):
        if sip_status() == PhoneStatus.REGISTERED:
            lost = None
            continue
        if lost is None:
            lost = time.monotonic()  # also pyVoIP's own re-registration every 2 min: registered again next round
            continue
        if time.monotonic() - lost < RECONNECT_AFTER or not busy.acquire(blocking=False):
            continue
        try:
            log.warning("SIP not registered for %s min (%s), reconnecting",
                        RECONNECT_AFTER // 60, sip_status().value)
            connect(old=phone)
            log.info("SIP status: %s", sip_status().value)
        except Exception as e:  # noqa: BLE001
            log.warning("Reconnecting failed: %s - next try in %s min", e, RECONNECT_AFTER // 60)
        finally:
            lost = time.monotonic()  # next try RECONNECT_AFTER from now
            busy.release()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if not cfg.api_token:
            return True  # in that case the server only listens on 127.0.0.1
        return hmac.compare_digest(self.headers.get("Authorization", "").encode(),
                                   f"Bearer {cfg.api_token}".encode())

    def _task_id(self) -> str:
        """The id in /tasks/<id>; "" for other paths."""
        return self.path[len("/tasks/"):] if self.path.startswith("/tasks/") and task_store is not None else ""

    def _shopping_history(self):
        """GET /shopping/history?item=Milch&days=30&event=bought: events of the shopping history."""
        query = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
        try:
            days = float(query["days"]) if query.get("days") else None
        except ValueError:
            return self._send(400, {"error": "days must be a number"})
        self._send(200, {"list": shopping_list.entity_id,
                         "events": shopping_list.events(query.get("item", ""), days, query.get("event", ""))})

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            return self._send(200, {"sip": sip_status().value, "busy": busy.locked(),
                                    "time": datetime.now().isoformat(timespec="seconds")})
        if urlsplit(self.path).path == "/shopping/history" and shopping_list is not None:
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            return self._shopping_history()
        if task_store is None or not (self.path == "/tasks" or self._task_id()):
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/tasks":
            return self._send(200, {"tasks": task_store.all()})
        task = task_store.get(self._task_id())
        self._send(200 if task else 404, task or {"error": "not found"})

    def do_DELETE(self):  # noqa: N802
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
        """POST /tasks: plans a call with a task. Without a plan (goal) Claude makes one from the task text."""
        number = str(body.get("number") or "").strip()
        refusal = tasks.number_refusal(cfg, number)
        if refusal:
            return self._send(400, {"error": refusal})
        if task_store.blocked(number) and not body.get("override_do_not_call"):
            return self._send(409, {"error": "this number asked not to be called again (override_do_not_call)"})
        if not str(body.get("task") or "").strip():
            return self._send(400, {"error": "no task"})
        caller = find_caller(number, wildcard=False)
        fixed = {**body, "number": number, "name": str(body.get("name") or "") or caller_name(caller)}
        if not body.get("goal"):
            try:
                fixed = {**brain.plan(fixed["task"], number, fixed["name"]), **fixed}  # what was given wins
            except Exception as e:  # noqa: BLE001
                log.warning("Planning the task failed: %s", e)
                return self._send(502, {"error": f"planning failed: {e}"})
        requester = find_caller(str(body.get("requester") or ""), wildcard=False)
        task, error = tasks.build(cfg, fixed, {"number": requester["number"] if requester else "",
                                               "name": caller_name(requester) or OWNER},
                                  datetime.now().astimezone())
        if task is None:
            return self._send(400, {"error": error})
        if body.get("override_do_not_call"):
            task_store.unblock(number)
        self._send(201, task_store.add(task))

    def do_POST(self):  # noqa: N802
        if self.path != "/call" and not (self.path == "/tasks" and task_store is not None):
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

    def log_message(self, fmt, *args):
        log.debug("HTTP " + fmt, *args)


def check_ha_admin():
    """Warns once at startup if the token cannot read automation definitions (needs an administrator)."""
    if ha.is_admin() is False:
        log.warning("Home Assistant: the token does not belong to an administrator. Claudine cannot read the "
                    "definitions and descriptions of automations and scripts (ha_automation, ha_automations); "
                    "use a long-lived token of an administrator, e.g. of a separate user for BrainCall.")


def main():
    console.banner(cfg)
    for key in ("sip_user", "sip_password", "anthropic_api_key", "openai_api_key"):
        if not cfg[key]:
            log.error("Configuration incomplete: %s missing (config.json)", key)
            sys.exit(1)
    if not OWNER:
        log.warning("owner_name is not set (config.json) - the assistant will speak of \"the owner\"")

    log.info("Config: %s | logs: %s", config.CONFIG_FILE[0], _LOGDIR)
    for w in config.WARNINGS:
        log.warning(w)
    log.info("Caller memory: %s", calls.describe() + (f" ({calls.path})" if calls.enabled else ""))
    log.info("Callers: %s", ", ".join(f"{c['name'] or c['number']} ({','.join(sorted(c['access']))})"
                                      for c in cfg.callers) or "none, incoming calls are rejected")
    if task_store is not None:
        span = tasks.hours(cfg)
        log.info("Planned calls: on, %s, at most %s per day, %d open (%s)",
                 f"{span[0]:%H:%M}-{span[1]:%H:%M}" if span else "at any time",
                 cfg.outbound_max_calls_per_day or "any number",
                 sum(t["status"] == tasks.PLANNED for t in task_store.all()), task_store.path)
        if str(cfg.outbound_hours or "").strip() and span is None:
            log.warning("outbound_hours %r not understood (e.g. 09:00-20:00): no time limit", cfg.outbound_hours)
    else:
        log.info("Planned calls: off (tasks)")
    if cfg.web_access:
        with_web = [c["name"] or c["number"] for c in cfg.callers if web.allowed(cfg, c["access"], web_for(c))]
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
        log.info("Home Assistant: %s", "connected" if ha.enabled else "not configured")
        if ha.enabled:  # in the background: Home Assistant may still be starting
            threading.Thread(target=check_ha_admin, name="HA admin check", daemon=True).start()
        # greetings and fillers synthesized ahead, so the caller hears them without waiting for TTS
        threading.Thread(target=phrase_cache.prewarm, args=(phrases.wanted(cfg),), name="phrase cache",
                         daemon=True).start()
        threading.Thread(target=watchdog, name="SIP watchdog", daemon=True).start()
        if task_store is not None:
            threading.Thread(target=task_loop, name="planned calls", daemon=True).start()
        if shopping_list is not None:
            log.info("Shopping list: %s", shopping_list.describe())
            threading.Thread(target=shopping_loop, name="shopping list", daemon=True).start()
        elif not cfg.shopping_auto:
            log.info("Shopping list: automatic shopping list off (shopping_auto)")

        bind = "0.0.0.0" if cfg.api_token else "127.0.0.1"
        server = ThreadingHTTPServer((bind, int(cfg.http_port)), Handler)
        log.info("HTTP trigger on http://%s:%s/call", bind, cfg.http_port)
        console.start_keys(quit_, test_call, open_log)
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
