"""STT and TTS via the OpenAI API."""
import logging
import re
import unicodedata

import numpy as np
import requests

from audio import resample_24k_to_8k, to_wav

log = logging.getLogger("speech")

STT_URL = "https://api.openai.com/v1/audio/transcriptions"
TTS_URL = "https://api.openai.com/v1/audio/speech"

# Context hint for the transcription in "auto" mode. Deliberately German/Ukrainian:
# it describes the spoken languages and prevents misdetection as Polish, Serbian, Chinese …
STT_HINT = "Telefongespräch auf Deutsch oder Ukrainisch (українською)."

# Typical phrases the speech recognition invents from noise or echo (they occur in German, hence German text).
# Discarded only as the whole utterance, so "Mach Musik an" still reaches Claude.
HALLUCINATIONS = {"musik", "untertitel", "vielen dank fürs zuschauen", "danke fürs zuschauen"}
# Unambiguous patterns: music markers, echoes of STT_HINT and amara.org anywhere; subtitle credits, thank-yous
# and copyright notes only at the start, so "Schalte die Untertitel der Sendung aus" still reaches Claude
HALLUCINATION_PATTERNS = re.compile(
    r"♪|♫|[\[(*]\s*musik\s*[\])*]|amara\.org|telefongespräch auf deutsch|українською\)"
    r"|^untertitel(?:ung)?\s+(?:von|im auftrag|des|durch|by)\b|^(?:vielen )?dank(?:e)? f[üu]r'?s zuschauen\b"
    r"|^copyright\b|©|^www\.\S+$")


# Phone numbers are spelled digit by digit before TTS: the voice otherwise reads "04542 4635" in groups
# ("vierundsechzig" for 46, swallowed digits). A number starts with +, 0 or * and has at least 5 digits;
# spaces, slashes and dashes between the digits are dropped. Times, dates and decimals stay as they are.
PHONE_NUMBER = re.compile(r"(?<![\w.,+*#/-])(?:\+|\*{1,2}|0)[0-9*#]?(?:[ /-]?[0-9*#]){3,}(?![\w*#+]|[.,:]\d)")
DATE = re.compile(r"\d{1,2}([/-])\d{1,2}\1\d{2,4}")  # 09/11/2026, 09-11-26: a date, not a number
DIGIT_WORDS = {
    "de": "null eins zwei drei vier fünf sechs sieben acht neun".split() + ["plus", "Stern", "Raute"],
    "en": "zero one two three four five six seven eight nine".split() + ["plus", "star", "hash"],
    "uk": "нуль один два три чотири п'ять шість сім вісім дев'ять".split() + ["плюс", "зірочка", "решітка"],
}


def spell_numbers(text: str, language: str = "") -> str:
    """Phone numbers in the text as single words, one per digit: "045424635" -> "null, vier, fünf, vier, …".
    Language without words: the digits separated by commas, read one by one in any language."""
    words = DIGIT_WORDS.get(language)

    def spell(m):
        chars = [c for c in m.group(0) if c in "0123456789+*#"]
        if sum(c.isdigit() for c in chars) < 5 or DATE.fullmatch(m.group(0)):
            return m.group(0)
        return ", ".join(words["0123456789+*#".index(c)] if words else c for c in chars)

    return PHONE_NUMBER.sub(spell, text)


class Speech:
    def __init__(self, cfg):
        self.cfg = cfg
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {cfg.openai_api_key}"
        first = (cfg.owner_name or "").strip().split(" ")[0].lower()
        self.hallucinations = HALLUCINATIONS | ({f"telefonat mit {first}"} if first else set())

    def is_hallucination(self, text: str) -> bool:
        """True for empty text, invented phrases and text that is neither Latin nor Cyrillic script."""
        norm = " ".join(text.lower().split()).strip(" .!?,…")
        letters = [c for c in norm if c.isalpha()]
        ok = sum(unicodedata.name(c, "").startswith(("LATIN", "CYRILLIC")) for c in letters)
        return (len(norm) < 2 or norm in self.hallucinations or bool(HALLUCINATION_PATTERNS.search(norm))
                or ok < len(letters) / 2)

    def transcribe(self, pcm: np.ndarray, language: str = "de") -> str:
        files = {"file": ("utterance.wav", to_wav(pcm), "audio/wav")}
        data = {"model": self.cfg.stt_model}
        if language and language != "auto":
            data["language"] = language
        else:
            data["prompt"] = STT_HINT  # see STT_HINT
        r = self.http.post(STT_URL, files=files, data=data, timeout=30)
        r.raise_for_status()
        text = (r.json().get("text") or "").strip()
        if self.is_hallucination(text):
            if text:
                log.info("(discarded: %s)", text)
            return ""
        return text

    def synthesize(self, text: str, language: str | None = None) -> np.ndarray:
        """language: of the conversation, for spelled phone numbers (de, en, uk; "" = unknown: plain digits;
        None = the configured one)."""
        letters = [c for c in text if c.isalpha()]
        cyr = sum(unicodedata.name(c, "").startswith("CYRILLIC") for c in letters)
        ukrainian = bool(letters) and cyr > len(letters) / 2
        spoken = spell_numbers(text, "uk" if ukrainian else self.cfg.language if language is None else language)
        body = {
            "model": self.cfg.tts_model,
            "voice": self.cfg.tts_voice,
            "input": spoken,
            "response_format": "pcm",
        }
        if "tts-1" not in self.cfg.tts_model:
            instr = self.cfg.tts_instructions or ""
            if ukrainian:  # Ukrainian text -> Ukrainian pronunciation
                if "Deutsch" in instr or "German" in instr:  # user instructions may be German or English
                    instr = instr.replace("Deutsch", "Ukrainisch").replace("German", "Ukrainian")
                else:
                    instr += " Language: Ukrainian, native pronunciation."
            if instr:
                body["instructions"] = instr
        r = self.http.post(TTS_URL, json=body, timeout=30)
        r.raise_for_status()
        return resample_24k_to_8k(r.content)
