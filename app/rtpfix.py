"""Better audio quality for pyVoIP 1.6.8.

Original problems:
- Outgoing audio goes through 8-bit linear (audible noise) before being encoded to G.711.
- If PCMA (A-law, usual for incoming calls on the FRITZ!Box) is negotiated,
  pyVoIP still encodes µ-law -> distorted, "broken" sound.
Fix: we encode 16-bit audio ourselves for the negotiated codec and pass it through unchanged.
"""
import logging
import time

from pyVoIP import RTP

log = logging.getLogger("rtp")

SILENCE = {"PCMU": b"\xff", "PCMA": b"\xd5"}


def _padded_reader(pm, fill: bytes):
    def read(length: int = 160) -> bytes:
        while pm.rebuilding:
            time.sleep(0.01)
        with pm.bufferLock:
            packet = pm.buffer.read(length)
            if len(packet) < length:
                packet += fill * (length - len(packet))
        return packet
    return read


def setup(call) -> str:
    codec = "PCMU"
    for client in getattr(call, "RTPClients", []):
        pref = client.preference
        codec = "PCMA" if pref == RTP.PayloadType.PCMA else "PCMU"
        client.encode_packet = lambda payload: payload  # already encoded
        client.pmout.read = _padded_reader(client.pmout, SILENCE[codec])
    log.info("Audio codec: %s", codec)
    return codec
