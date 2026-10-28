"""Fixed INVITE for pyVoIP 1.6.8 (outbound calls via the FRITZ!Box).

Bugs in the original:
- the authenticated INVITE reuses the Via branch of the first one -> discarded as a retransmission
- digest uri does not match the request URI, no qop, no 407/Proxy-Authenticate
- From header uses the local IP instead of the registrar domain
- error responses are never surfaced
"""
import hashlib
import logging
import re
import secrets

import pyVoIP
from pyVoIP import SIP

log = logging.getLogger("sip")


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf8")).hexdigest()


def _challenge(raw: bytes):
    text = raw.decode("utf8", "replace")
    m = re.search(r"^(WWW|Proxy)-Authenticate:\s*Digest\s+(.+)$", text, re.I | re.M)
    if not m:
        return None, {}
    params = {k: a or b for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', m.group(2))}
    hdr = "Authorization" if m.group(1).lower() == "www" else "Proxy-Authorization"
    return hdr, params


def _build_invite(sip, number, sess_id, ms, sendtype, branch, call_id, cseq, tag, auth=""):
    body = "v=0\r\n"
    body += f"o=pyVoIP {sess_id} {int(sess_id) + 2} IN IP4 {sip.myIP}\r\n"
    body += "s=pyVoIP\r\n"
    body += f"c=IN IP4 {sip.myIP}\r\n"
    body += "t=0 0\r\n"
    for port in ms:
        body += f"m=audio {port} RTP/AVP " + " ".join(str(p) for p in ms[port]) + "\r\n"
    for port in ms:
        for p in ms[port]:
            body += f"a=rtpmap:{p} {ms[port][p]}/{ms[port][p].rate}\r\n"
            if str(ms[port][p]) == "telephone-event":
                body += f"a=fmtp:{p} 0-15\r\n"
    body += "a=ptime:20\r\n"
    body += f"a={sendtype}\r\n"

    uri = f"sip:{number}@{sip.server}"
    req = f"INVITE {uri} SIP/2.0\r\n"
    req += f"Via: SIP/2.0/UDP {sip.myIP}:{sip.myPort};branch={branch};rport\r\n"
    req += "Max-Forwards: 70\r\n"
    req += f"Contact: <sip:{sip.username}@{sip.myIP}:{sip.myPort}>\r\n"
    req += f"To: <{uri}>\r\n"
    req += f"From: <sip:{sip.username}@{sip.server}>;tag={tag}\r\n"
    req += f"Call-ID: {call_id}\r\n"
    req += f"CSeq: {cseq} INVITE\r\n"
    req += f"Allow: {', '.join(pyVoIP.SIPCompatibleMethods)}\r\n"
    req += "Content-Type: application/sdp\r\n"
    req += "User-Agent: claude-telefon\r\n"
    req += auth
    req += f"Content-Length: {len(body)}\r\n\r\n"
    return req + body, uri


def _digest(sip, hdr, ch, uri):
    realm, nonce = ch.get("realm", ""), ch.get("nonce", "")
    ha1 = _md5(f"{sip.username}:{realm}:{sip.password}")
    ha2 = _md5(f"INVITE:{uri}")
    parts = [f'username="{sip.username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    qop = ch.get("qop", "")
    if "auth" in [q.strip() for q in qop.split(",")]:
        nc, cnonce = "00000001", secrets.token_hex(8)
        resp = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        parts += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
    else:
        resp = _md5(f"{ha1}:{nonce}:{ha2}")
    parts += [f'response="{resp}"', "algorithm=MD5"]
    if "opaque" in ch:
        parts.append(f'opaque="{ch["opaque"]}"')
    return f"{hdr}: Digest " + ",".join(parts) + "\r\n"


def patched_invite(self, number, ms, sendtype):
    call_id = self.gen_call_id()
    sess_id = self.sessID.next()
    tag = self.gen_tag()
    self.tagLibrary[call_id] = tag
    branch = "z9hG4bK" + secrets.token_hex(12)
    cseq = self.inviteCounter.next()
    invite, uri = _build_invite(self, number, str(sess_id), ms, sendtype, branch, call_id, cseq, tag)

    with self.recvLock:
        self.out.sendto(invite.encode("utf8"), (self.server, self.port))
        while True:
            raw = self.s.recv(8192)
            resp = SIP.SIPMessage(raw)
            if resp.type != SIP.SIPMessageType.RESPONSE or resp.headers.get("Call-ID") != call_id:
                self.parse_message(resp)
                continue
            code = int(resp.status)
            log.info("SIP %s %s for INVITE", code, resp.status.phrase)
            if code in (100, 180, 183):
                return SIP.SIPMessage(invite.encode("utf8")), call_id, sess_id
            if code in (401, 407):
                break
            # final response without auth (e.g. 403/404/486): pass on to the status logger
            self.out.sendto(self.gen_ack(resp).encode("utf8"), (self.server, self.port))
            self.parse_message(resp)
            return SIP.SIPMessage(invite.encode("utf8")), call_id, sess_id

        self.out.sendto(self.gen_ack(resp).encode("utf8"), (self.server, self.port))
        hdr, ch = _challenge(raw)
        if not hdr:
            log.warning("Auth challenge not readable:\n%s", raw.decode("utf8", "replace"))
        auth = _digest(self, hdr or "Authorization", ch, uri)
        branch = "z9hG4bK" + secrets.token_hex(12)
        cseq = self.inviteCounter.next()
        invite, _ = _build_invite(self, number, str(sess_id), ms, sendtype, branch, call_id, cseq, tag, auth)
        self.out.sendto(invite.encode("utf8"), (self.server, self.port))
        log.info("INVITE with credentials sent")
        return SIP.SIPMessage(invite.encode("utf8")), call_id, sess_id


def patched_gen_ack(self, resp):
    """ACK with the To tag of the response (the original invents a new one -> FRITZ!Box drops the ACK)."""
    tag = self.tagLibrary.get(resp.headers["Call-ID"], self.gen_tag())
    to_tag = resp.headers["To"].get("tag", "")
    to = resp.headers["To"]["raw"] + (f";tag={to_tag}" if to_tag else "")
    if 200 <= int(resp.status) < 300 and resp.headers.get("Contact"):
        target = resp.headers["Contact"].strip().strip("<").split(">")[0]
        branch = "z9hG4bK" + secrets.token_hex(12)
    else:
        target = resp.headers["To"]["raw"].strip().strip("<").strip(">")
        branch = resp.headers["Via"][0].get("branch", "z9hG4bK" + secrets.token_hex(12))
    ack = f"ACK {target} SIP/2.0\r\n"
    ack += f"Via: SIP/2.0/UDP {self.myIP}:{self.myPort};branch={branch};rport\r\n"
    ack += "Max-Forwards: 70\r\n"
    ack += f"To: {to}\r\n"
    ack += f"From: {resp.headers['From']['raw']};tag={tag}\r\n"
    ack += f"Call-ID: {resp.headers['Call-ID']}\r\n"
    ack += f"CSeq: {resp.headers['CSeq']['check']} ACK\r\n"
    ack += "User-Agent: claude-telefon\r\n"
    ack += "Content-Length: 0\r\n\r\n"
    return ack


def apply(phone):
    import types
    phone.sip.invite = types.MethodType(patched_invite, phone.sip)
    phone.sip.gen_ack = types.MethodType(patched_gen_ack, phone.sip)
