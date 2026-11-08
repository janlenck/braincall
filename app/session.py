"""One running call: read audio, detect speech, let Claude answer, speak the answer."""
import collections
import logging
import queue
import threading
import time

import numpy as np
from pyVoIP.VoIP import CallState

from audio import FRAME, RATE, encode_g711, rms, u8_to_s16
import rtpfix

log = logging.getLogger("session")

# Fixed phrases spoken without the model, per conversation language
PHRASES = {
    "de": {"silence": "Ich höre nichts mehr, ich lege auf. Tschüss!",
           "error": "Bei mir ist gerade ein Fehler aufgetreten. Ich lege auf."},
    "uk": {"silence": "Я вас більше не чую, тому завершую дзвінок. До побачення!",
           "error": "У мене сталася помилка. Я завершую дзвінок."},
    "en": {"silence": "I can't hear you anymore, I'll hang up now. Bye!",
           "error": "Something went wrong on my side. I'll hang up now."},
}

ENDED = object()
TIMEOUT = object()

# Voice activity detection on 20 ms frames
ECHO_FACTOR = 4.0       # while we speak the threshold is raised so the echo of our own voice does not count
ONSET_IDLE = 3          # consecutive loud frames that start an utterance while we are silent
ONSET_PLAYING = (8, 12)  # loud frames among the last frames that start a possible barge-in while we speak
GAP_FRAMES = 8          # pauses up to 160 ms inside speech count as speech (barge-in time)
AFTER_ANSWER = 10       # loud frames after our answer ended that keep an utterance begun while we spoke
GRACE_S = 0.6           # no barge-in at the very start of an answer


class CallSession:
    def __init__(self, call, brain, speech, cfg):
        self.call = call
        self.brain = brain
        self.speech = speech
        self.cfg = cfg
        self.active = True

        self.utterances: queue.Queue = queue.Queue()
        self.user_speaking = threading.Event()
        self.speech_started_at = 0.0
        self.playing = threading.Event()  # set for the whole answer, from its first sentence until it is spoken
        self.interrupt = threading.Event()
        self._answer_lock = threading.Lock()
        self.answer_started_at = 0.0

        self.sentence_q: queue.Queue = queue.Queue()
        self.audio_q: queue.Queue = queue.Queue()
        self.transcript: list[str] = []
        self.access = frozenset({"guest"})
        self.actions: list[str] = []  # changing tool calls, for the summary
        self.counters: dict[str, int] = {}  # per-call limits, e.g. bookings by callers without admin
        self.caller_turns = 0
        self.language = cfg.language or "de"
        self.caller_label = "caller"
        self.owner = (cfg.owner_name or "").strip()                 # "Jan Lenck"
        self.owner_first = self.owner.split()[0] if self.owner else "the owner"
        self.codec = rtpfix.setup(call)

        # VAD state (see _frame)
        self.noise = 200.0
        self._end_frames = max(10, int(cfg.end_silence_ms / 20))
        self._run = 0                                     # consecutive loud frames before an utterance
        self._recent: collections.deque = collections.deque(maxlen=ONSET_PLAYING[1])
        self._pre: collections.deque = collections.deque(maxlen=15)  # 300 ms pre-roll
        self._in_speech = False
        self._buf: list[np.ndarray] = []
        self._silent = 0
        self._gap = 0
        self._started_while_playing = False
        self._overlaps_answer = False                     # the answer went on during this utterance
        self._barged = False                              # this utterance interrupted the answer
        self.seg_voiced = 0                               # speech frames of the current utterance
        self._after_answer = 0
        self._peak = 0.0
        self._seg_thr = 0.0

    # ------------------------------------------------------------------ Status
    def alive(self) -> bool:
        return self.active and self.call.state == CallState.ANSWERED

    # ------------------------------------------------------------------ input
    def _reader(self):
        """Reads one frame every 20 ms (non-blocking; pyVoIP would otherwise block on digital silence)."""
        next_t = time.monotonic()
        while self.alive():
            try:
                data = self.call.read_audio(FRAME, False)
            except Exception:  # noqa: BLE001  call ended
                break
            self._frame(u8_to_s16(data), time.monotonic())

            next_t += FRAME / RATE
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()

        self.active = False
        self.utterances.put(None)

    def _min_frames(self) -> int:
        return int(float(self.cfg.barge_in_min_ms or 1000) / 20)

    def _frame(self, a: np.ndarray, now: float):
        """One 20 ms frame: finds utterances and interrupts the answer when the caller really speaks."""
        e = rms(a)
        playing = self.playing.is_set()
        thr = max(float(self.cfg.vad_min_energy), self.noise * 3.0)
        if playing:
            thr *= ECHO_FACTOR  # do not mistake the echo of our own voice for the caller
        loud = e > thr

        if not self._in_speech:
            self._pre.append(a)
            self._recent.append(loud)
            if loud:
                self._run += 1
            else:
                self._run = 0
                if not playing:  # frozen while we speak, our echo must not raise the noise floor
                    self.noise = max(50.0, 0.95 * self.noise + 0.05 * e)
            # while we speak, loud frames with short dips start a possible barge-in
            onset = sum(self._recent) if playing else self._run
            if onset >= (ONSET_PLAYING[0] if playing else ONSET_IDLE):
                self._in_speech = True
                self._started_while_playing = self._overlaps_answer = playing
                self._buf = list(self._pre)
                self._silent = self._gap = self._after_answer = 0
                self._barged = False
                self.seg_voiced = onset
                self._peak, self._seg_thr = e, thr
                self._recent.clear()
                self.speech_started_at = now
                self.user_speaking.set()
            return

        self._buf.append(a)
        self._peak = max(self._peak, e)
        if playing:  # also speech begun while the model was thinking may interrupt once we speak
            self._overlaps_answer = True
        self._silent = 0 if e > thr * 0.7 else self._silent + 1
        if loud:
            # short pauses between syllables and words count as speech
            self.seg_voiced += 1 + (self._gap if self._gap <= GAP_FRAMES else 0)
            self._gap = 0
            if not playing:
                self._after_answer += 1
        else:
            self._gap += 1

        if self._barge(now):
            log.info("%s interrupts (%d ms speech)", self.caller_label, self.seg_voiced * 20)
            self._barged = True
            with self._answer_lock:
                self.interrupt.set()
                self.playing.clear()  # the rest of the answer is dropped, listen with the normal threshold

        if self._silent >= self._end_frames or len(self._buf) >= 1500:
            self._end_segment()

    def _end_segment(self):
        self._in_speech = False
        self._run = 0
        self.user_speaking.clear()
        keep = len(self._buf) - self._silent >= 10
        if self._started_while_playing:
            # keep it if it interrupted us, or the caller went on speaking after our answer had ended
            if not (self._barged or self._after_answer >= AFTER_ANSWER):
                keep = False
                why = ("barge-in off" if not self.cfg.barge_in
                       else "over before it could interrupt" if self.seg_voiced >= self._min_frames() else "too short")
                log.debug("ignored while speaking: %d ms speech (needs %d ms, %s), loudest %.0f, threshold %.0f",
                          self.seg_voiced * 20, self._min_frames() * 20, why, self._peak, self._seg_thr)
        if keep:
            self.utterances.put(np.concatenate(self._buf))
        self._pre.clear()

    # ------------------------------------------------------------------ output
    def say(self, text: str):
        self.sentence_q.put(text)

    def _tts_worker(self):
        while True:
            s = self.sentence_q.get()
            try:
                if s is None:
                    return
                if self.interrupt.is_set() or not self.alive():
                    continue
                pcm = self.speech.synthesize(s)
                self.audio_q.put((s, pcm))
            except Exception as e:  # noqa: BLE001
                log.warning("TTS failed: %s", e)
            finally:
                self.sentence_q.task_done()

    def _play_worker(self):
        while True:
            item = self.audio_q.get()
            try:
                if item is None:
                    return
                s, pcm = item
                if self.interrupt.is_set() or not self.alive():
                    continue
                name = self.cfg.assistant_name or "Claude"
                log.info("%s: %s", name, s)
                self.transcript.append(f"{name}: {s}")
                self._play(pcm)
            finally:
                self.audio_q.task_done()

    def _barge(self, now: float) -> bool:
        """Speech during the answer has lasted barge_in_min_ms (pauses inside speech included) and goes on."""
        return (bool(self.cfg.barge_in) and self._overlaps_answer and self.playing.is_set()
                and not self.interrupt.is_set()
                and self._gap <= GAP_FRAMES                  # the caller is still speaking, not a stale count
                and now - self.answer_started_at > GRACE_S   # only the start of the answer, not of every sentence
                and self.seg_voiced >= self._min_frames())

    def _play(self, pcm: np.ndarray):
        data = encode_g711(pcm, self.codec)
        with self._answer_lock:
            if self.interrupt.is_set():
                return
            if not self.playing.is_set():  # first sentence of the answer
                self.answer_started_at = time.monotonic()
                self.playing.set()
        t0 = time.monotonic()
        sent = 0
        for i in range(0, len(data), FRAME):
            if not self.alive() or self.interrupt.is_set():
                return
            while sent / RATE - (time.monotonic() - t0) > 0.08:
                time.sleep(0.005)
            chunk = data[i:i + FRAME]
            self.call.write_audio(chunk)
            sent += len(chunk)
        # wait until the buffer has really been sent
        while sent / RATE - (time.monotonic() - t0) > 0 and self.alive() and not self.interrupt.is_set():
            time.sleep(0.01)

    def wait_spoken(self):
        """Waits until the answer is spoken (or dropped after an interruption); then the answer is over."""
        self.sentence_q.join()
        self.audio_q.join()
        with self._answer_lock:
            self.playing.clear()

    # ------------------------------------------------------------------ listening
    def listen(self, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            try:
                u = self.utterances.get(timeout=0.2)
            except queue.Empty:
                if not self.alive():
                    return ENDED
                if time.monotonic() > deadline and not self.user_speaking.is_set():
                    return TIMEOUT
                continue
            return ENDED if u is None else u

    def user_text(self, timeout: float):
        """Waits for an intelligible utterance. Returns text, TIMEOUT or ENDED."""
        deadline = time.monotonic() + timeout
        while True:
            u = self.listen(max(0.5, deadline - time.monotonic()))
            if u is ENDED or u is TIMEOUT:
                return u
            try:
                text = self.speech.transcribe(u, self.language)
            except Exception as e:  # noqa: BLE001
                log.warning("STT failed: %s", e)
                text = ""
            if text:
                log.info("%s: %s", self.caller_label, text)
                self.transcript.append(f"{self.caller_label}: {text}")
                self.caller_turns += 1
                return text

    # ------------------------------------------------------------------ call flow
    def _assistant_turn(self, messages) -> bool:
        self.interrupt.clear()
        hang = self.brain.turn(messages, self.say,
                               lambda: self.interrupt.is_set() or not self.alive(),
                               access=self.access, language=self.language, actions=self.actions,
                               counters=self.counters)
        self.wait_spoken()
        self.interrupt.clear()
        return hang

    def _rights(self, name: str) -> str:
        """System note on what the caller may do (the tools themselves are limited in Brain.tools_for)."""
        owner = self.owner_first
        who = name or "The caller"
        quiet = (f"Give no information at all about {owner}, the house, whether anyone is at home, or about devices"
                 + (", except what is allowed above. " if self.access - {"guest"} else ". "))
        if "admin" in self.access:
            return ""
        if self.access == {"guest"}:
            return f"You have no access to the house in this call. {quiet}"
        parts = []
        if "read" in self.access:
            parts.append(f"{who} may ask about everything in Home Assistant (states, sensors, appointments "
                         "with details), but must not switch or change anything. ")
        elif "calendar" in self.access:
            parts.append(f"{who} may ask whether {owner} is free or busy (ha_busy_times: you only see busy times, "
                         "never guess or reveal what an appointment is). ")
        if "calendar" in self.access and self.cfg.calendar_write:
            parts.append(f"{who} may book an appointment in a free slot of the default calendar without {owner}'s "
                         "approval: repeat day and time once, and after a yes call ha_create_event with "
                         "confirmed=true. ")
        if "shopping" in self.access:
            parts.append(f"{who} may hear and change the shopping list (shopping_list, shopping_edit). ")
        return "".join(parts) + ("" if "read" in self.access else quiet)

    def run(self, outbound: bool, message: str = "", caller: str = "", access=frozenset({"guest"}),
            language: str = "", caller_name: str = "", instructions: str = "", previous: str = ""):
        """previous: system note with summaries of earlier calls with this number (memory.note), may be empty."""
        self.access = frozenset(access)  # decides the tools Claude gets (Brain.tools_for)
        full = caller_name
        self.caller_label = full or caller or "unknown"            # for the log: "Jan Mobile"
        name = full.split("(")[0].split()[0] if full.strip() else ""  # to address the person: "Jan"
        owner = self.owner_first
        rights = self._rights(name)
        if instructions:
            rights += f"Instructions from {owner} for this caller: {instructions} "
        rights += previous
        if language:
            self.language = language
        threads = [threading.Thread(target=f, daemon=True)
                   for f in (self._reader, self._tts_worker, self._play_worker)]
        for t in threads:
            t.start()
        try:
            # Listen briefly: does a person answer ("Hello?") or a mailbox?
            heard = ""
            first = self.listen(2.5)
            if isinstance(first, np.ndarray):
                try:
                    heard = self.speech.transcribe(first, self.language)
                except Exception as e:  # noqa: BLE001
                    log.warning("STT failed: %s", e)
            elif first is ENDED:
                return
            if heard:
                self.transcript.append(f"{self.caller_label}: {heard}")
                self.caller_turns += 1

            if outbound:
                who = (f"{name} (number {caller})" if name
                       else f"the number {caller or 'unknown'} (name unknown; {owner} asked you to make this call — "
                            "politely ask who you are talking to)")
                intro = (f"[System: You are calling {who}. Reason: {message or 'no specific reason given'}. "
                         + rights
                         + (f"When the call was answered you heard: “{heard}”. " if heard
                            else "Nothing was heard when the call was answered. ")
                         + "If that sounds like a mailbox: do not wait for the beep, IMMEDIATELY leave a short "
                         + "message of two or three sentences and hang up afterwards. "
                         + "Otherwise greet briefly and get straight to the point.]")
            elif self.access != {"guest"} and name:
                intro = (f"[System: {name} is calling you (from {caller or 'unknown'}). "
                         + (f"Already said when answering: “{heard}”. " if heard else "")
                         + rights
                         + f"Answer briefly and address {name} by name.]")
            else:
                assistant = self.cfg.assistant_name or "the call assistant"
                known = f"The caller is probably {name}. " if name else ""
                intro = (f"[System: Someone is calling {owner}, number {caller}. This is NOT {owner}. " + known
                         + (f"When answering you heard: “{heard}”. " if heard else "")
                         + f"You are {owner}'s phone assistant. Introduce yourself (in the conversation language) as: "
                         + f"This is {assistant}, {self.owner or owner}'s assistant. "
                         + "First ask for the caller's name, then for the reason of the call "
                         + f"and whether {owner} should call back. " + rights
                         + "At the end briefly repeat what you noted, say goodbye and hang up. "
                         + f"The conversation is passed on to {owner} as a note.]")
            messages = [{"role": "user", "content": intro}]

            silence = 0
            while self.alive():
                if self._assistant_turn(messages) or not self.alive():
                    break
                text = self.user_text(float(self.cfg.silence_timeout))
                if text is ENDED:
                    break
                if text is TIMEOUT:
                    silence += 1
                    if silence >= 2:
                        self.say(PHRASES.get(self.language, PHRASES["de"])["silence"])
                        self.wait_spoken()
                        break
                    messages.append({"role": "user",
                                     "content": "[System: The caller has not said anything for a while.]"})
                    continue
                silence = 0
                messages.append({"role": "user", "content": text})
        except Exception:  # noqa: BLE001
            log.exception("Error during call")
            try:
                self.say(PHRASES.get(self.language, PHRASES["de"])["error"])
                self.wait_spoken()
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.hangup()

    def hangup(self):
        if self.call.state == CallState.ANSWERED:
            time.sleep(0.3)
            try:
                self.call.hangup()
            except Exception:  # noqa: BLE001
                pass
        self.active = False
        self.sentence_q.put(None)
        self.audio_q.put(None)
