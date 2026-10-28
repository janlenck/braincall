"""Claude Telefon: SIP phone on the FRITZ!Box plus an HTTP trigger for outbound calls."""
import hmac
import json
import logging
import re
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
_file = logging.FileHandler(_LOGDIR / "claude-telefon.log", encoding="utf-8")
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


def detect_local_ip(target: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 5060))
        return s.getsockname()[0]
    finally:
        s.close()


def norm(n: str) -> str:
    """Complete number in a comparable form: digits plus * and # (internal numbers like **611),
    international prefix of the own country folded to a leading 0 (+49151… == 0049151… == 0151…)."""
    s = re.sub(r"[^\d*#+]", "", str(n or ""))
    s = ("00" + s[1:] if s.startswith("+") else s).replace("+", "")
    cc = "00" + re.sub(r"\D", "", str(cfg.country_code or ""))
    if len(cc) > 2 and s.startswith(cc):
        s = "0" + s[len(cc):]
    return s


def same_number(a: str, b: str) -> bool:
    return bool(norm(a)) and norm(a) == norm(b)


def trusted_name(number: str) -> str:
    """Name for a number listed in trusted_numbers, otherwise "".
    trusted_numbers may be a list (name = owner_name) or an object {"number": "name"}."""
    tn = cfg.trusted_numbers or []
    items = tn.items() if isinstance(tn, dict) else ((n, OWNER) for n in tn)
    for n, name in items:
        if same_number(n, number):
            return str(name or OWNER or n)
    return ""


def report(answered: bool, number: str, started: float, session=None, message=""):
    ha.fire_event("claude_telefon_call", {
        "number": number,
        "answered": answered,
        "reason": message,
        "duration_s": round(time.time() - started),
        "transcript": "\n".join(session.transcript) if session else "",
    })


def lang_for(number: str) -> str:
    """Language for a number from caller_languages, otherwise the global setting."""
    for n, lang in (cfg.caller_languages or {}).items():
        if same_number(n, number):
            return lang
    return cfg.language or "de"


def _ringing(call) -> bool:
    return call.state in (CallState.DIALING, CallState.RINGING) and call.call_id not in sip_failed


def _wait_ringing(call, seconds: float):
    deadline = time.monotonic() + seconds
    while _ringing(call) and time.monotonic() < deadline:
        time.sleep(0.2)


def outbound(number: str, message: str):
    started = time.time()
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
        name = trusted_name(number)
        session.run(outbound=True, message=message, caller=number, language=lang_for(number),
                    trusted=bool(name), caller_name=name)
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
        report(answered, number, started, session, message)
        busy.release()


def on_incoming(call):
    try:
        number = call.request.headers["From"]["number"] or ""
    except Exception:  # noqa: BLE001
        number = ""
    accept_all = "*" in [str(n).strip() for n in cfg.inbound_allowed]
    name = trusted_name(number)
    trusted = bool(name)
    reason = ""
    if not norm(number):
        reason = "withheld number"
    elif not trusted and not accept_all and not any(same_number(n, number) for n in cfg.inbound_allowed):
        reason = "not in inbound_allowed"
    if reason:
        log.warning("Incoming call from %s rejected (%s)", number or "unknown", reason)
        try:
            call.deny()
        except Exception:  # noqa: BLE001
            pass
        ha.fire_event("claude_telefon_call", {"number": number or "unknown", "answered": False,
                                              "reason": f"rejected: {reason}", "duration_s": 0, "transcript": ""})
        return
    if not busy.acquire(blocking=False):
        try:
            call.deny()
        except Exception:  # noqa: BLE001
            pass
        return
    started = time.time()
    session = None
    try:
        log.info("Answering call from %s (%s)", number or "withheld", name if trusted else "unknown, notes only")
        call.answer()
        session = CallSession(call, brain, speech, cfg)
        session.run(outbound=False, caller=number, trusted=trusted, language=lang_for(number), caller_name=name)
    except Exception:  # noqa: BLE001
        log.exception("Incoming call failed")
    finally:
        report(True, number, started, session, "incoming" if trusted else "incoming (untrusted caller)")
        if session and not trusted and session.transcript:
            notes = _LOGDIR / "call-notes.txt"
            with open(notes, "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M}  from {number or 'withheld'} ===\n")
                f.write("\n".join(session.transcript) + "\n")
            log.info("Call note saved: %s", notes)
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
            self._send(200, {"sip": phone.get_status().value, "busy": busy.locked(),
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
        if phone.get_status() != PhoneStatus.REGISTERED:
            return self._send(503, {"error": f"SIP not registered ({phone.get_status().value})"})
        if not busy.acquire(blocking=False):
            return self._send(409, {"error": "line busy"})
        threading.Thread(target=outbound, args=(number, str(body.get("message") or "")), daemon=True).start()
        self._send(202, {"status": "dialing", "number": number})

    def log_message(self, fmt, *args):
        log.debug("HTTP " + fmt, *args)


def main():
    global phone
    console.banner(cfg)
    for key in ("sip_user", "sip_password", "anthropic_api_key", "openai_api_key"):
        if not cfg[key]:
            log.error("Configuration incomplete: %s missing (config.json)", key)
            sys.exit(1)
    if not OWNER:
        log.warning("owner_name is not set (config.json) - the assistant will speak of \"the owner\"")

    local_ip = cfg.local_ip or detect_local_ip(cfg.sip_server)
    log.info("Config: %s | logs: %s", config.CONFIG_FILE[0], _LOGDIR)
    log.info("Local IP %s, SIP %s@%s:%s, local SIP port %s",
             local_ip, cfg.sip_user, cfg.sip_server, cfg.sip_port, cfg.sip_local_port)
    phone = VoIPPhone(cfg.sip_server, int(cfg.sip_port), cfg.sip_user, cfg.sip_password,
                      myIP=local_ip, callCallback=on_incoming, sipPort=int(cfg.sip_local_port),
                      rtpPortLow=int(cfg.rtp_port_min), rtpPortHigh=int(cfg.rtp_port_max))
    global _orig_parse
    _orig_parse = phone.sip.parse_message
    phone.sip.parse_message = logged_parse
    fritz_sip.apply(phone)
    phone.start()
    for _ in range(50):
        if phone.get_status() == PhoneStatus.REGISTERED:
            break
        time.sleep(0.2)
    log.info("SIP status: %s", phone.get_status().value)
    log.info("Home Assistant: %s", "connected" if ha.enabled else "not configured")

    bind = "0.0.0.0" if cfg.api_token else "127.0.0.1"
    server = ThreadingHTTPServer((bind, int(cfg.http_port)), Handler)
    log.info("HTTP trigger on http://%s:%s/call", bind, cfg.http_port)

    def quit_():
        log.info("Quit by key press")
        threading.Thread(target=server.shutdown, daemon=True).start()

    def test_call():
        number = str(cfg.default_number or "").strip()
        if not number:
            log.warning("No default_number in config.json")
        elif phone.get_status() != PhoneStatus.REGISTERED:
            log.warning("SIP not registered (%s)", phone.get_status().value)
        elif not busy.acquire(blocking=False):
            log.warning("Line busy")
        else:
            hello = f"Briefly say hello to {OWNER_FIRST}." if OWNER_FIRST else "Briefly say hello."
            threading.Thread(target=outbound, args=(number, "Test call by key press. " + hello),
                             daemon=True).start()

    def open_log():
        import os
        try:
            os.startfile(str(_LOGDIR / "claude-telefon.log"))  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            log.warning("Opening the log failed: %s", e)

    console.start_keys(quit_, test_call, open_log)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down …")
        server.server_close()
        try:
            phone.stop()
        except Exception:  # noqa: BLE001
            pass
        log.info("Unregistered. Bye.")


phone: VoIPPhone

if __name__ == "__main__":
    main()
