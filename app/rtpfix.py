"""Bessere Tonqualität für pyVoIP 1.6.8.

Original-Problem:
- Ausgehender Ton läuft über 8-Bit-Linear (hörbares Rauschen) bevor er G.711 kodiert wird.
- Wird PCMA (A-law, bei der FRITZ!Box für eingehende Anrufe üblich) ausgehandelt,
  kodiert pyVoIP trotzdem µ-law -> verzerrter, „kaputter“ Klang.
Lösung: Wir kodieren 16-Bit-Audio selbst passend zum ausgehandelten Codec und reichen es durch.
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
        client.encode_packet = lambda payload: payload  # schon fertig kodiert
        client.pmout.read = _padded_reader(client.pmout, SILENCE[codec])
    log.info("Audio-Codec: %s", codec)
    return codec
