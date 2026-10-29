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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
from brain import Brain  # noqa: E402
from ha import HA  # noqa: E402
from session import CallSession  # noqa: E402
from speech import Speech  # noqa: E402

cfg = config.load()
_LOGDIR = Path(cfg.log_dir)
_LOGDIR.mkdir(parents=True, exist_ok=True)
_con = logging.StreamHandler(sys.stdout)
_con.setFormatter(console.ColorFormatter())
# rotates at 2 MB and keeps 5 old files (braincall.log.1 … .5): at most about 12 MB
_file = logging.handlers.RotatingFileHandler(_LOGDIR / "braincall.log", maxBytes=2_000_000,
                                             backupCount=5, encoding="utf-8")
_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_con, _file])
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
busy = threading.Lock()
sip_failed: dict = {}
abandoned: set = set()  # Call-IDs of outbound calls given up while ringing (a late 200 OK gets hung up)
answer_lock = threading.Lock()  # a 200 OK is handled either before a call is given up or as a late answer
OWNER = (cfg.owner_name or "").strip()
OWNER_FIRST = OWNER.split()[0] if OWNER else ""
MAX_BODY = 64 * 1024
UNREGISTER_TIMEOUT = 10  # seconds; the add-on gets 20 s to stop (braincall/config.yaml)
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


def lang_for(caller: dict | None) -> str:
    """Language of the caller entry, otherwise the global setting."""
    return (caller or {}).get("language") or cfg.language or "de"


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
                                          session.actions, lang)
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


def outbound(number: str, message: str):
    started = time.time()
    caller = find_caller(number, wildcard=False)  # not listed: like a guest, no "*" entry needed
    session = None
    answered = False
    call = None
    try:
        log.info("Calling %s (reason: %s)", number, message or "-")
        call = phone.call(number)
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
            return
        if call.state != CallState.ANSWERED:
            log.warning("Call rejected: SIP %s", sip_failed.get(call.call_id, call.state.value))
            return
        answered = True
        session = CallSession(call, brain, speech, cfg)
        session.run(outbound=True, message=message, caller=number, language=lang_for(caller),
                    access=caller["access"] if caller else GUEST, caller_name=caller_name(caller),
                    instructions=caller["instructions"] if caller else "")
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


def on_incoming(call):
    try:
        number = call.request.headers["From"]["number"] or ""
    except Exception:  # noqa: BLE001
        number = ""
    caller = find_caller(number)
    reason = ""
    if not norm(number):
        reason = "withheld number"
    elif caller is None:
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
    name = caller_name(caller)
    access = ",".join(sorted(caller["access"]))
    try:
        log.info("Answering call from %s (%s, access %s)", number, name or "unknown", access)
        call.answer()
        session = CallSession(call, brain, speech, cfg)
        session.run(outbound=False, caller=number, access=caller["access"], language=lang_for(caller),
                    caller_name=name, instructions=caller["instructions"])
    except Exception:  # noqa: BLE001
        log.exception("Incoming call failed")
    finally:
        finish(True, number, started, session, f"incoming (access {access})", caller, inbound=True)
        busy.release()


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

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send(200, {"sip": sip_status().value, "busy": busy.locked(),
                             "time": datetime.now().isoformat(timespec="seconds")})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/call":
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
        number = str(body.get("number") or cfg.default_number).strip()
        if not number:
            return self._send(400, {"error": "no number"})
        if sip_status() != PhoneStatus.REGISTERED:
            return self._send(503, {"error": f"SIP not registered ({sip_status().value})"})
        if not busy.acquire(blocking=False):
            return self._send(409, {"error": "line busy"})
        threading.Thread(target=outbound, args=(number, str(body.get("message") or "")), daemon=True).start()
        self._send(202, {"status": "dialing", "number": number})

    def log_message(self, fmt, *args):
        log.debug("HTTP " + fmt, *args)


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
    log.info("Callers: %s", ", ".join(f"{c['name'] or c['number']} ({','.join(sorted(c['access']))})"
                                      for c in cfg.callers) or "none, incoming calls are rejected")
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
        threading.Thread(target=watchdog, name="SIP watchdog", daemon=True).start()

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
