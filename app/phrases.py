"""Fixed phrases spoken without the model (greetings, fillers, status phrases) and their cached audio.

The audio is synthesized once and kept in <log_dir>/cache. The file name is a hash of the text and the TTS
settings, so a change of language, voice, TTS model, speaking style, assistant or owner name simply leads to
new files; files no longer used are deleted after the next start."""
import hashlib
import json
import logging
import random
import re
import threading
import unicodedata
from pathlib import Path

import numpy as np

log = logging.getLogger("phrases")

# Thinking sounds while the answer is prepared, the same for every language: words like "Okay" or "Ja" sound
# like an answer ("Ist das okay?" - "Okay." - … - "Nein"), a pondering "Hmm" does not.
FILLERS = ["Hmm…", "Ähm…", "Öhm…"]

TEXTS = {
    "de": {
        "greet": "Hallo, hier ist {assistant}, die Assistentin von {owner}.",
        "greet_plain": "Hallo, hier ist {assistant}.",
        "greet_known": "Hallo {name}, hier ist {assistant}.",
        "call": "Hallo, hier ist {assistant}, ich rufe im Auftrag von {owner} an.",
        "call_named": "Hallo {name}, hier ist {assistant}, ich rufe im Auftrag von {owner} an.",
        "fillers": FILLERS,
        "still": ["Ich bin noch dran.", "Einen Moment noch."],
        "tools": {"lookup": "Moment, ich schau kurz nach.", "calendar": "Ich schaue in den Kalender.",
                  "control": "Ich schalte das.", "list": "Ich schaue auf die Liste.",
                  "write": "Ich trage das ein.", "web": "Ich suche das kurz raus."},
    },
    "en": {
        "greet": "Hello, this is {assistant}, {owner}'s assistant.",
        "greet_plain": "Hello, this is {assistant}.",
        "greet_known": "Hi {name}, this is {assistant}.",
        "call": "Hello, this is {assistant}, calling on behalf of {owner}.",
        "call_named": "Hello {name}, this is {assistant}, calling on behalf of {owner}.",
        "fillers": FILLERS,
        "still": ["I'm still on it.", "Just a moment."],
        "tools": {"lookup": "One moment, let me check.", "calendar": "Let me look at the calendar.",
                  "control": "I'll switch that.", "list": "Let me check the list.",
                  "write": "I'll put that in.", "web": "Let me look that up."},
    },
    "uk": {
        "greet": "Добрий день, це {assistant}, асистентка {owner}.",
        "greet_plain": "Добрий день, це {assistant}.",
        "greet_known": "Привіт, {name}, це {assistant}.",
        "call": "Добрий день, це {assistant}, я телефоную від імені {owner}.",
        "call_named": "Добрий день, {name}, це {assistant}, я телефоную від імені {owner}.",
        "fillers": FILLERS,
        "still": ["Я ще тут, хвилинку.", "Ще хвилинку."],
        "tools": {"lookup": "Хвилинку, зараз подивлюся.", "calendar": "Дивлюся в календар.",
                  "control": "Зараз перемкну.", "list": "Дивлюся список.",
                  "write": "Зараз запишу.", "web": "Зараз пошукаю."},
    },
}


# German words or letters: in "auto" mode Latin script alone could also be English
_GERMAN = re.compile(r"[äöüß]|\b(ich|und|nicht|das|ist|der|ja|nein|bitte|danke|hallo|wie|mir|mal|kannst|du|sie|"
                     r"habe|hast|wir|noch|auch|schon|heute|morgen|gibt|bei|mit|von|zu|ein|eine|kein)\b", re.I)


def lang_of(language: str, last_text: str = "") -> str:
    """Phrase language for the conversation language. "auto" (German at the start, see brain.LANG_RULES)
    follows the caller's last words: Cyrillic = Ukrainian, German words = German, otherwise unknown ("")."""
    if language in TEXTS:
        return language
    if language in ("auto", ""):
        letters = [c for c in last_text if c.isalpha()]
        cyr = sum(unicodedata.name(c, "").startswith("CYRILLIC") for c in letters)
        if letters and cyr > len(letters) / 2:
            return "uk"
        return "de" if not letters or _GERMAN.search(last_text) else ""
    return ""  # a language without phrases: the model greets, no fillers


def langs_of(language: str) -> set:
    """Languages whose phrases a conversation in this language may need."""
    return {"de", "uk"} if language in ("auto", "") else {lang_of(language)} - {""}


def first_name(name: str) -> str:
    """'Jan Mobile' -> 'Jan', 'Maryna (Kyiv)' -> 'Maryna' (as the session addresses callers)."""
    return name.split("(")[0].split()[0] if name.strip() else ""


def greeting(lang: str, assistant: str, owner: str, name: str = "", outbound: bool = False,
             on_behalf: bool = True) -> str:
    """The greeting spoken right away. name = first name of a known caller; on_behalf = an outbound call says
    whom it calls for (not to the owner themselves). "" for languages without phrases."""
    t = TEXTS.get(lang)
    if not t:
        return ""
    fields = {"assistant": assistant or "Claude", "owner": owner, "name": name}
    if outbound and owner and on_behalf:
        key = "call_named" if name else "call"
    elif name:
        key = "greet_known"
    else:
        key = "greet" if owner else "greet_plain"
    return t[key].format(**fields)


def tool_key(name: str, data: dict, cfg) -> str:
    """Which status phrase fits a tool call; "" = none (e.g. a change that still needs a yes from the caller)."""
    data = data or {}
    if name == "hang_up":
        return ""
    if name in ("ha_events", "ha_busy_times"):
        return "calendar"
    if name == "ha_create_event":
        return "write" if data.get("confirmed") else "calendar"
    if name == "shopping_list":
        return "list"
    if name == "shopping_edit":
        return "write"
    if name == "ha_control":
        entity = str(data.get("entity_id") or "")
        domain = entity.split(".")[0]
        if domain == "todo":
            return "write"
        if domain in (cfg.confirm_domains or []) and not data.get("confirmed"):
            return ""  # the tool will ask for a yes first
        return "control"
    if name.startswith("web"):
        return "web"
    return "lookup"


def pick(options: list, last: str = "") -> str:
    """A random phrase, never the same as the last one."""
    choice = [o for o in options if o != last] or list(options)
    return random.choice(choice) if choice else ""


def wanted(cfg) -> list:
    """All phrases worth synthesizing ahead for this configuration."""
    assistant = cfg.assistant_name or "Claude"
    owner = (cfg.owner_name or "").strip()
    out = []
    langs = langs_of(cfg.language or "de")
    for c in cfg.callers or []:
        langs |= langs_of(c.get("language") or cfg.language or "de")
    for lang in sorted(langs):
        t = TEXTS[lang]
        out += [greeting(lang, assistant, owner), greeting(lang, assistant, owner, outbound=True)]
        if cfg.fillers:
            out += t["fillers"] + t["still"] + list(t["tools"].values())
    for c in cfg.callers or []:
        name = first_name(c.get("name") or owner or c["number"])  # like main.caller_name
        lang = lang_of(c.get("language") or cfg.language or "de")
        if c["number"] == "*" or not name or not lang:
            continue
        if c["access"] != frozenset({"guest"}):
            out.append(greeting(lang, assistant, owner, name))
        out.append(greeting(lang, assistant, owner, name, outbound=True, on_behalf="admin" not in c["access"]))
    return list(dict.fromkeys(t for t in out if t))


class PhraseCache:
    """Synthesized phrases in memory and on disk (16-bit PCM, 8 kHz)."""

    def __init__(self, speech, cfg, folder):
        self.speech = speech
        self.cfg = cfg
        self.folder = Path(folder)
        self.mem: dict[str, np.ndarray] = {}
        self.lock = threading.Lock()

    def key(self, text: str) -> str:
        raw = json.dumps([text, self.cfg.tts_model, self.cfg.tts_voice, self.cfg.tts_instructions or ""],
                         ensure_ascii=False)
        return hashlib.sha1(raw.encode()).hexdigest()[:24]

    def get(self, text: str, synthesize: bool = False):
        """Audio of a phrase; synthesizes it (and stores it) only with synthesize=True, else None if unknown."""
        if not text:
            return None
        k = self.key(text)
        with self.lock:
            if k in self.mem:
                return self.mem[k]
        path = self.folder / f"{k}.pcm"
        pcm = None
        try:
            if path.exists():
                pcm = np.frombuffer(path.read_bytes(), dtype="<i2").astype(np.int16)
        except OSError as e:
            log.warning("Reading %s failed: %s", path, e)
        if pcm is None and synthesize and self.speech is not None:
            try:
                pcm = self.speech.synthesize(text)
            except Exception as e:  # noqa: BLE001
                log.warning("TTS of \"%s\" failed: %s", text, e)
                return None
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                path.write_bytes(pcm.astype("<i2").tobytes())
            except OSError as e:
                log.warning("Saving %s failed: %s", path, e)
        if pcm is not None:
            with self.lock:
                self.mem[k] = pcm
        return pcm

    def prewarm(self, texts: list):
        """Synthesizes missing phrases and deletes files of phrases no longer used."""
        keep = set()
        new = 0
        for text in texts:
            k = self.key(text)
            keep.add(f"{k}.pcm")
            if not (self.folder / f"{k}.pcm").exists():
                new += 1
            self.get(text, synthesize=True)
        try:
            for f in self.folder.glob("*.pcm"):
                if f.name not in keep:
                    f.unlink()
        except OSError as e:
            log.warning("Cleaning %s failed: %s", self.folder, e)
        log.info("Phrase cache: %d phrases ready (%d new) in %s", len(keep), new, self.folder)
