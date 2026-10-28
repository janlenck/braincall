"""STT und TTS über die OpenAI-API."""
import logging
import unicodedata

import numpy as np
import requests

from audio import resample_24k_to_8k, to_wav

log = logging.getLogger("speech")

STT_URL = "https://api.openai.com/v1/audio/transcriptions"
TTS_URL = "https://api.openai.com/v1/audio/speech"

# typische Erfindungen der Spracherkennung bei Rauschen/Echo
STT_HINT = "Telefongespräch auf Deutsch oder Ukrainisch (українською)."

HALLUCINATIONS = ("telefongespräch auf deutsch", "українською)", "telefonat mit jan", "untertitel", "vielen dank fürs zuschauen", "danke fürs zuschauen",
                  "copyright", "amara.org", "www.", "musik", "♪")


class Speech:
    def __init__(self, cfg):
        self.cfg = cfg
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {cfg.openai_api_key}"

    def transcribe(self, pcm: np.ndarray, language: str = "de") -> str:
        files = {"file": ("utterance.wav", to_wav(pcm), "audio/wav")}
        data = {"model": self.cfg.stt_model}
        if language and language != "auto":
            data["language"] = language
        else:
            data["prompt"] = STT_HINT  # verhindert Fehl-Erkennung als Polnisch, Serbisch, Chinesisch …
        r = self.http.post(STT_URL, files=files, data=data, timeout=30)
        r.raise_for_status()
        text = (r.json().get("text") or "").strip()
        norm = text.lower().strip(" .!?,")
        letters = [c for c in norm if c.isalpha()]
        ok = sum(unicodedata.name(c, "").startswith(("LATIN", "CYRILLIC")) for c in letters)
        if not norm or any(h in norm for h in HALLUCINATIONS) or len(norm) < 2 or ok < len(letters) / 2:
            if text:
                log.info("(verworfen: %s)", text)
            return ""
        return text

    def synthesize(self, text: str) -> np.ndarray:
        body = {
            "model": self.cfg.tts_model,
            "voice": self.cfg.tts_voice,
            "input": text,
            "response_format": "pcm",
        }
        if "tts-1" not in self.cfg.tts_model:
            instr = self.cfg.tts_instructions or ""
            letters = [c for c in text if c.isalpha()]
            cyr = sum(unicodedata.name(c, "").startswith("CYRILLIC") for c in letters)
            if letters and cyr > len(letters) / 2:  # ukrainischer Text -> ukrainische Aussprache
                instr = (instr.replace("Deutsch", "Ukrainisch") if "Deutsch" in instr
                         else instr + " Sprache: Ukrainisch, muttersprachliche Aussprache.")
            if instr:
                body["instructions"] = instr
        r = self.http.post(TTS_URL, json=body, timeout=30)
        r.raise_for_status()
        return resample_24k_to_8k(r.content)
