"""Audio-Helfer: pyVoIP liefert/erwartet 8 kHz, 8 Bit unsigned linear (Stille = 128)."""
import io
import wave

import numpy as np

try:
    import audioop  # Python <= 3.12
except ImportError:  # Python 3.13+: audioop-lts
    import audioop  # type: ignore  # noqa: F811

RATE = 8000
FRAME = 160  # 20 ms bei 8 kHz

# Tiefpass (Hamming-gefensterter Sinc) für 24 kHz -> 8 kHz, Grenzfrequenz 3,4 kHz
_n = np.arange(-24, 25)
_fc = 3400 / 24000
_H = np.sinc(2 * _fc * _n) * 2 * _fc * np.hamming(len(_n))
_H /= _H.sum()


def u8_to_s16(data: bytes) -> np.ndarray:
    a = np.frombuffer(data, dtype=np.uint8).astype(np.int16)
    return ((a - 128) * 256).astype(np.int16)


def s16_to_u8(a: np.ndarray) -> bytes:
    return ((a.astype(np.int32) // 256) + 128).clip(0, 255).astype(np.uint8).tobytes()


def resample_24k_to_8k(raw: bytes) -> np.ndarray:
    """OpenAI-TTS 'pcm' = 24 kHz, 16 Bit LE mono."""
    raw = raw[: len(raw) // 2 * 2]
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if a.size == 0:
        return np.zeros(0, dtype=np.int16)
    y = np.convolve(a, _H, mode="same")[::3]
    return np.clip(y, -32768, 32767).astype(np.int16)


def rms(a: np.ndarray) -> float:
    if a.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(a.astype(np.float32) ** 2)))


def to_wav(a: np.ndarray, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(a.astype("<i2").tobytes())
    return buf.getvalue()


def encode_g711(a: np.ndarray, codec: str) -> bytes:
    """16-Bit-PCM direkt nach G.711 (µ-law oder A-law) – ohne 8-Bit-Zwischenschritt."""
    raw = a.astype("<i2").tobytes()
    return audioop.lin2alaw(raw, 2) if codec == "PCMA" else audioop.lin2ulaw(raw, 2)
