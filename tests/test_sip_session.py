"""SIP fixes for the FRITZ!Box (digest auth, INVITE, CANCEL, ACK) and the rights note of a call session."""
import hashlib
import re
from types import SimpleNamespace

import pytest
from pyVoIP import RTP, SIP

import fritz_sip
from config import parse_access
from conftest import make_cfg
import session as session_mod
from session import CallSession


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


class Out:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data.decode(), addr))


def fake_sip():
    return SimpleNamespace(username="620", password="secret", server="192.168.178.1", port=5060,
                           myIP="192.168.178.20", myPort=5062, out=Out(), pending_invites={},
                           tagLibrary={"cid1": "ourtag"}, gen_tag=lambda: "newtag")


def headers(msg):
    lines = msg.split("\r\n\r\n")[0].split("\r\n")
    return lines[0], dict(line.split(": ", 1) for line in lines[1:])


# ---------------------------------------------------------------- digest auth
def test_challenge_www_and_proxy():
    raw = b'SIP/2.0 401 Unauthorized\r\nWWW-Authenticate: Digest realm="fritz.box", nonce="abc123", qop="auth"\r\n\r\n'
    assert fritz_sip._challenge(raw) == ("Authorization", {"realm": "fritz.box", "nonce": "abc123", "qop": "auth"})
    raw = b"SIP/2.0 407 Proxy\r\nProxy-Authenticate: Digest realm=fritz.box,nonce=xyz\r\n\r\n"
    assert fritz_sip._challenge(raw) == ("Proxy-Authorization", {"realm": "fritz.box", "nonce": "xyz"})
    assert fritz_sip._challenge(b"SIP/2.0 401 Unauthorized\r\n\r\n") == (None, {})


def test_digest_without_qop():
    sip = fake_sip()
    hdr = fritz_sip._digest(sip, "Authorization", {"realm": "fritz.box", "nonce": "n1", "opaque": "op"},
                            "sip:0151@192.168.178.1")
    ha1, ha2 = md5("620:fritz.box:secret"), md5("INVITE:sip:0151@192.168.178.1")
    assert hdr.startswith("Authorization: Digest ") and hdr.endswith("\r\n")
    assert f'response="{md5(f"{ha1}:n1:{ha2}")}"' in hdr
    assert 'uri="sip:0151@192.168.178.1"' in hdr and 'opaque="op"' in hdr and "qop" not in hdr


def test_digest_with_qop_auth(monkeypatch):
    monkeypatch.setattr(fritz_sip.secrets, "token_hex", lambda n: "cafe")
    hdr = fritz_sip._digest(fake_sip(), "Proxy-Authorization", {"realm": "r", "nonce": "n", "qop": "auth,auth-int"},
                            "sip:1@h")
    expected = md5(f"{md5('620:r:secret')}:n:00000001:cafe:auth:{md5('INVITE:sip:1@h')}")
    assert hdr.startswith("Proxy-Authorization: Digest ")
    assert "qop=auth" in hdr and "nc=00000001" in hdr and 'cnonce="cafe"' in hdr
    assert f'response="{expected}"' in hdr


# ---------------------------------------------------------------- INVITE / CANCEL / ACK
def test_build_invite():
    ms = {30000: {0: RTP.PayloadType.PCMU, 8: RTP.PayloadType.PCMA, 101: RTP.PayloadType.EVENT}}
    msg, uri = fritz_sip._build_invite(fake_sip(), "0151", "5", ms, RTP.TransmitType.SENDRECV, "z9hG4bKb1", "cid1",
                                       2, "ourtag", auth="Authorization: Digest x\r\n")
    first, h = headers(msg)
    body = msg.split("\r\n\r\n", 1)[1]
    assert uri == "sip:0151@192.168.178.1"
    assert first == "INVITE sip:0151@192.168.178.1 SIP/2.0"
    assert h["From"] == "<sip:620@192.168.178.1>;tag=ourtag"  # registrar domain, not the local IP
    assert h["Via"] == "SIP/2.0/UDP 192.168.178.20:5062;branch=z9hG4bKb1;rport"
    assert h["CSeq"] == "2 INVITE" and h["Authorization"] == "Digest x"
    assert int(h["Content-Length"]) == len(body)
    assert "m=audio 30000 RTP/AVP 0 8 101" in body and "a=fmtp:101 0-15" in body and "a=sendrecv" in body


class Counter:
    def __init__(self):
        self.n = 0

    def next(self):
        self.n += 1
        return self.n


class Sock:
    def __init__(self, answers):
        self.answers = list(answers)

    def settimeout(self, t):
        pass

    def setblocking(self, b):
        pass

    def recv(self, n):
        return self.answers.pop(0)


def invite_sip(answers=()):
    import threading
    sip = fake_sip()
    sip.__dict__.update(recvLock=threading.Lock(), s=Sock(answers), inviteCounter=Counter(), sessID=Counter(),
                        gen_call_id=lambda: "cid9", parse_message=lambda m: None)
    sip.gen_ack = lambda resp: fritz_sip.patched_gen_ack(sip, resp)
    return sip


MS = {30000: {0: RTP.PayloadType.PCMU, 101: RTP.PayloadType.EVENT}}


@pytest.mark.parametrize("number", ["04542 4635", "0151-123", "", "sip:x"])
def test_invite_refuses_a_number_that_is_not_dialable(number):
    sip = invite_sip()
    with pytest.raises(ValueError):
        fritz_sip.patched_invite(sip, number, MS, RTP.TransmitType.SENDRECV)
    assert sip.out.sent == [] and sip.pending_invites == {}  # nothing rings anywhere


def test_invite_with_credentials_dials_the_complete_number():
    challenge = ("SIP/2.0 401 Unauthorized\r\nVia: SIP/2.0/UDP 192.168.178.20:5062;branch=z9hG4bKx;rport\r\n"
                 "From: <sip:620@192.168.178.1>;tag=ourtag\r\nTo: <sip:045424635@192.168.178.1>;tag=fb\r\n"
                 "Call-ID: cid9\r\nCSeq: 1 INVITE\r\n"
                 'WWW-Authenticate: Digest realm="fritz.box", nonce="abc"\r\nContent-Length: 0\r\n\r\n')
    sip = invite_sip([challenge.encode()])
    msg, call_id, _ = fritz_sip.patched_invite(sip, "045424635", MS, RTP.TransmitType.SENDRECV)
    invites = [m for m, _ in sip.out.sent if m.startswith("INVITE")]
    assert len(invites) == 2 and all(m.startswith("INVITE sip:045424635@192.168.178.1 SIP/2.0") for m in invites)
    assert "Authorization: Digest" in invites[1] and call_id == "cid9" and "cid9" in sip.pending_invites
    assert msg.headers["Call-ID"] == "cid9"


def test_cancel_reuses_branch_and_cseq_of_the_invite():
    sip = fake_sip()
    assert fritz_sip.cancel(sip, "cid1") is False
    sip.pending_invites["cid1"] = ("sip:0151@192.168.178.1", "z9hG4bKb2", 3, "ourtag")
    assert fritz_sip.cancel(sip, "cid1") is True
    msg, addr = sip.out.sent[0]
    first, h = headers(msg)
    assert addr == ("192.168.178.1", 5060)
    assert first == "CANCEL sip:0151@192.168.178.1 SIP/2.0"
    assert "branch=z9hG4bKb2;" in h["Via"] and h["CSeq"] == "3 CANCEL" and h["From"].endswith("tag=ourtag")


def response(status="200 OK", contact=True):
    raw = (f"SIP/2.0 {status}\r\nVia: SIP/2.0/UDP 192.168.178.20:5062;branch=z9hG4bKinv;rport\r\n"
           "From: <sip:620@192.168.178.1>;tag=ourtag\r\nTo: <sip:0151@192.168.178.1>;tag=theirtag\r\n"
           "Call-ID: cid1\r\nCSeq: 2 INVITE\r\n"
           + ("Contact: <sip:0151@192.168.178.1:5060>\r\n" if contact else "") + "Content-Length: 0\r\n\r\n")
    return SIP.SIPMessage(raw.encode())


def test_ack_for_200_goes_to_contact_with_their_tag():
    first, h = headers(fritz_sip.patched_gen_ack(fake_sip(), response()))
    assert first == "ACK sip:0151@192.168.178.1:5060 SIP/2.0"
    assert h["To"] == "<sip:0151@192.168.178.1>;tag=theirtag"
    assert h["From"] == "<sip:620@192.168.178.1>;tag=ourtag"
    assert h["CSeq"] == "2 ACK"
    assert "branch=z9hG4bKinv" not in h["Via"]  # 2xx ACK is its own transaction


def test_ack_for_error_reuses_the_invite_branch():
    first, h = headers(fritz_sip.patched_gen_ack(fake_sip(), response("486 Busy Here", contact=False)))
    assert first == "ACK sip:0151@192.168.178.1 SIP/2.0"
    assert "branch=z9hG4bKinv;" in h["Via"]


def test_bye_confirmed():
    sip = SimpleNamespace(pending_byes={"cid1"})
    assert fritz_sip.bye_confirmed(sip, "cid1") is True
    assert fritz_sip.bye_confirmed(sip, "cid1") is False


def test_end_late_answer_acks_every_time_but_sends_bye_once(monkeypatch):
    started = []
    monkeypatch.setattr(fritz_sip.threading, "Thread", lambda target, daemon: SimpleNamespace(
        start=lambda: started.append(target)))
    sip = fake_sip()
    sip.late_answered, sip.pending_byes = set(), set()
    sip.gen_ack = lambda r: fritz_sip.patched_gen_ack(sip, r)
    sip.gen_bye = lambda r: "BYE sip:x SIP/2.0\r\nVia: SIP/2.0/UDP h;branch=z9hG4bKinv;rport\r\n\r\n"
    fritz_sip.end_late_answer(sip, response())
    fritz_sip.end_late_answer(sip, response())  # retransmitted 200 OK
    assert [m.split(" ")[0] for m, _ in sip.out.sent] == ["ACK", "ACK"]
    assert len(started) == 1 and sip.pending_byes == {"cid1"}
    monkeypatch.setattr(fritz_sip.time, "sleep", lambda s: None)
    started[0]()  # never confirmed: sent once per retry, then given up
    byes = [m for m, _ in sip.out.sent[2:]]
    assert len(byes) == len(fritz_sip.BYE_RETRIES) and len(set(byes)) == 1  # identical retransmissions
    assert re.search(r"branch=(?!z9hG4bKinv)", byes[0])  # own transaction, not the INVITE's branch
    assert sip.pending_byes == set()


def test_end_late_answer_bye_stops_when_confirmed(monkeypatch):
    sip = fake_sip()
    sip.late_answered, sip.pending_byes = set(), set()
    sip.gen_ack = lambda r: "ACK"
    sip.gen_bye = lambda r: "BYE sip:x SIP/2.0\r\nVia: SIP/2.0/UDP h;branch=z9hG4bKinv;rport\r\n\r\n"
    started = []
    monkeypatch.setattr(fritz_sip.threading, "Thread", lambda target, daemon: SimpleNamespace(
        start=lambda: started.append(target)))
    monkeypatch.setattr(fritz_sip.time, "sleep", lambda s: fritz_sip.bye_confirmed(sip, "cid1"))
    fritz_sip.end_late_answer(sip, response())
    started[0]()
    assert [m.split(" ")[0] for m, _ in sip.out.sent] == ["ACK", "BYE"]


# ---------------------------------------------------------------- session rights
@pytest.fixture
def session():
    def make(access, **cfg):
        s = CallSession(SimpleNamespace(), brain=None, speech=None, cfg=make_cfg(owner_name="Jan Lenck", **cfg))
        s.access = parse_access(access)
        return s
    return make


def test_rights_admin_has_no_restrictions(session):
    assert session("admin")._rights("Jan") == ""


def test_rights_guest_gets_nothing(session):
    note = session("guest")._rights("")
    assert note.startswith("You have no access to the house")
    assert "whether anyone is at home" in note and "except" not in note


def test_rights_calendar(session):
    note = session("calendar")._rights("Maryna")
    assert "Maryna may ask whether Jan is free or busy" in note
    assert "ha_create_event with confirmed=true" in note
    assert "except what is allowed above" in note
    assert "ha_create_event" not in session("calendar", calendar_write=False)._rights("Maryna")


def test_rights_read_does_not_add_the_silence_rule(session):
    note = session("read, shopping")._rights("Walter")
    assert "may ask about everything" in note and "shopping list" in note
    assert "Give no information" not in note


# ---------------------------------------------------------------- intro with caller memory
@pytest.fixture
def intro(session, monkeypatch):
    """Runs CallSession.run up to the first assistant turn and returns the intro note."""
    def run(access, **kw):
        s = session(access)
        seen = []
        for name in ("_reader", "_tts_worker", "_play_worker", "hangup"):
            monkeypatch.setattr(s, name, lambda: None)
        monkeypatch.setattr(s, "listen", lambda timeout: session_mod.TIMEOUT)
        monkeypatch.setattr(s, "alive", lambda: True)
        monkeypatch.setattr(s, "_assistant_turn", lambda messages, *a: seen.append(messages[0]["content"]) or True)
        s.run(caller="0151111", access=parse_access(access), **kw)
        return seen[0]
    return run


@pytest.mark.parametrize("access, kw", [
    ("admin", {"outbound": False, "caller_name": "Jan Mobile"}),
    ("calendar", {"outbound": True, "caller_name": "Maryna", "message": "Reminder"}),
    ("guest", {"outbound": False}),
])
def test_intro_contains_previous_calls(intro, access, kw):
    note = "Previous calls with this number (…): - 2026-11-01 14:05 (they called): Asked about Friday. "
    assert note in intro(access, previous=note, **kw)
    assert "Previous calls" not in intro(access, **kw)
