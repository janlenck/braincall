"""One running call: read audio, detect speech, let Claude answer, speak the answer."""
import collections
import logging
import queue
import threading
import time

import numpy as np
from pyVoIP.VoIP import CallState

from audio import FRAME, RATE, encode_g711, rms, u8_to_s16
from brain import MUTATING
import phrases as ph
import rtpfix
from timing import CallTiming

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

# What is played: the answer, or a phrase from the cache (greeting, filler, status phrase)
ANSWER, GREETING, FILLER = "answer", "greeting", "filler"
MAILBOX_S = 2.0         # outbound: a first utterance with more speech than this may be a mailbox announcement
STILL_MAX = 3           # "I'm still on it" at most this often per turn


class Utterance:
    """What the caller said between two pauses. Transcribed in the background as soon as it ends.
    A tentative utterance is cut after speculative_ms of silence: final once the pause reaches end_silence_ms,
    cancelled if the caller goes on speaking (then a longer utterance follows)."""

    def __init__(self, pcm: np.ndarray, ended_at: float, detected_at: float, voiced_s: float = 0.0,
                 tentative: bool = False):
        self.pcm = pcm
        self.ended_at = ended_at          # last speech of the caller (time.monotonic)
        self.detected_at = detected_at    # when the pause was long enough
        self.voiced_s = voiced_s
        self.tentative = tentative
        self.text: str | None = None      # None until the transcription is done
        self.stt_done_at = 0.0
        self.done = threading.Event()
        self.final = threading.Event()
        self.cancelled = threading.Event()
        self.while_thinking = False       # ended while the model prepared an answer it could not take it into
        self.noted = False                # in the transcript and log
        if not tentative:
            self.final.set()

    def __len__(self) -> int:
        return len(self.pcm)


class CallSession:
    def __init__(self, call, brain, speech, cfg, phrases=None):
        self.call = call
        self.brain = brain
        self.speech = speech
        self.cfg = cfg
        self.phrases = phrases  # PhraseCache; None = no cached greeting and no fillers
        self.active = True

        self.utterances: queue.Queue = queue.Queue()
        self.user_speaking = threading.Event()
        self.speech_started_at = 0.0
        self.playing = threading.Event()  # set for the whole answer, from its first sentence until it is spoken
        self.interrupt = threading.Event()
        self._answer_lock = threading.Lock()
        self.answer_started_at = 0.0

        # turn state, guarded by _answer_lock
        self.restart = threading.Event()  # the caller spoke while the model was thinking: answer everything
        self._thinking = False            # from taking the caller's words until the first sentence is played
        self._side_effects = False        # a changing tool ran in this turn: no restart any more
        self._turn_audio = False          # the first sentence of the answer has been synthesized
        self._turn_said = False           # the model said something in this turn
        self._turn_phrase = False         # a filler or status phrase was played in this turn
        self._turn_last_sentence = 0.0    # start of the last sentence played in this turn
        self._turn_token = 0
        self._stale_before = 0.0          # utterances that ended before this belong to an earlier question
        self._last_output_at = 0.0        # end of the last audio we played
        self._last_phrase = ""
        self._spec_turn: Utterance | None = None
        self._turn_heard: list = []
        self._timer = None
        self.timing = CallTiming()

        self.sentence_q: queue.Queue = queue.Queue()
        self.audio_q: queue.Queue = queue.Queue()
        self.transcript: list[str] = []
        self.access = frozenset({"guest"})
        self.actions: list[str] = []  # changing tool calls, for the summary
        self.counters: dict[str, int] = {}  # per-call limits, e.g. bookings by callers without admin
        self.caller_turns = 0
        self.language = cfg.language or "de"
        self.caller_label = "caller"
        self.last_text = ""
        self.owner = (cfg.owner_name or "").strip()                 # "Jan Lenck"
        self.owner_first = self.owner.split()[0] if self.owner else "the owner"
        self.codec = rtpfix.setup(call)

        # VAD state (see _frame)
        self.noise = 200.0
        self._end_frames = max(10, int(cfg.end_silence_ms / 20))
        spec = int(float(cfg.speculative_ms or 0) / 20)
        self._spec_frames = spec if 0 < spec < self._end_frames else 0
        self._waiting = threading.Event()                 # waiting for the caller: speculative start allowed
        self._spec: Utterance | None = None               # tentative utterance of the running segment
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

        if self._spec is not None and self._silent == 0:
            self._spec.cancelled.set()  # the caller goes on speaking: the speculative answer is dropped
            log.debug("speculative start cancelled, %s goes on speaking", self.caller_label)
            self._spec = None
        elif (self._spec is None and self._spec_frames and self._silent == self._spec_frames
              and self._waiting.is_set() and not self._started_while_playing and not playing
              and len(self._buf) - self._silent >= 10):
            self._speculate(now)

        if self._silent >= self._end_frames or len(self._buf) >= 1500:
            self._end_segment(now)

    def _speculate(self, now: float):
        """A short pause: transcribe and answer already, the answer is only spoken once the pause is long enough."""
        u = Utterance(np.concatenate(self._buf), now - self._silent * FRAME / RATE, now,
                      self.seg_voiced * FRAME / RATE, tentative=True)
        self._spec = u
        self._transcribe_async(u)
        self.utterances.put(u)

    def _end_segment(self, now: float = 0.0):
        now = now or time.monotonic()
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
        spec, self._spec = self._spec, None
        if keep and spec is not None and not spec.cancelled.is_set():
            spec.pcm = np.concatenate(self._buf)  # the same speech plus the rest of the pause
            spec.detected_at = now
            spec.final.set()
        else:
            if spec is not None:
                spec.cancelled.set()
            if keep:
                self._finished(Utterance(np.concatenate(self._buf), now - self._silent * FRAME / RATE, now,
                                         self.seg_voiced * FRAME / RATE))
        self._pre.clear()

    def _finished(self, u: Utterance):
        """A finished utterance: transcribe it now and queue it for the conversation."""
        with self._answer_lock:
            if self._thinking:
                # said while the model prepares an answer: answer everything together, unless a changing
                # tool already ran - then it is the next turn
                u.while_thinking = True
                if not self._side_effects:
                    self.restart.set()
        self._transcribe_async(u)
        self.utterances.put(u)

    def _transcribe_async(self, u: Utterance):
        """Transcribes right away in the background, not only when the conversation asks for it."""
        if self.speech is None:
            return

        def work():
            try:
                text = self.speech.transcribe(u.pcm, self.language)
            except Exception as e:  # noqa: BLE001
                log.warning("STT failed: %s", e)
                text = ""
            u.text = text
            u.stt_done_at = time.monotonic()
            u.done.set()

        threading.Thread(target=work, name="stt", daemon=True).start()

    # ------------------------------------------------------------------ output
    def say(self, text: str):
        self.sentence_q.put((text, ANSWER))

    def _tts_worker(self):
        while True:
            item = self.sentence_q.get()
            try:
                if item is None:
                    return
                s, kind = item
                if self.interrupt.is_set() or self.restart.is_set() or not self.alive():
                    continue
                pcm = self.speech.synthesize(s)
                with self._answer_lock:
                    if kind == ANSWER and not self._turn_audio:
                        self._turn_audio = True  # from now on no filler before the answer
                        if self._timer is not None:
                            self._timer.mark("tts")
                    self.audio_q.put((s, pcm, kind))
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
                s, pcm, kind = item
                if self.interrupt.is_set() or not self.alive():
                    continue
                self._play(pcm, kind, s)
            finally:
                self.audio_q.task_done()

    def _barge(self, now: float) -> bool:
        """Speech during the answer has lasted barge_in_min_ms (pauses inside speech included) and goes on."""
        return (bool(self.cfg.barge_in) and self._overlaps_answer and self.playing.is_set()
                and not self.interrupt.is_set()
                and self._gap <= GAP_FRAMES                  # the caller is still speaking, not a stale count
                and now - self.answer_started_at > GRACE_S   # only the start of the answer, not of every sentence
                and self.seg_voiced >= self._min_frames())

    def _play(self, pcm: np.ndarray, kind: str = ANSWER, text: str = "") -> bool:
        """Plays one sentence of the answer or a cached phrase. False = dropped."""
        now = time.monotonic()
        own = False
        with self._answer_lock:
            if self.interrupt.is_set() or (kind == ANSWER and self.restart.is_set()):
                return False  # interrupted, or the caller went on speaking before the answer began
            if kind == ANSWER:
                if not self.playing.is_set():  # first sentence of the answer
                    self.answer_started_at = now
                    self.playing.set()
                self._thinking = False
                self._turn_last_sentence = now
                if self._timer is not None:
                    self._timer.mark("playback", now)
            else:
                if not self.playing.is_set():
                    # a phrase on its own: echo threshold and grace period only while it plays
                    self.answer_started_at = now
                    self.playing.set()
                    own = True
                if kind == FILLER and self._timer is not None and "filler" not in self._timer.marks:
                    self._timer.filler = text
                    self._timer.mark("filler", now)
        if text:
            name = self.cfg.assistant_name or "Claude"
            log.info("%s: %s%s", name, text, "" if kind == ANSWER else f" ({kind})")
            self.transcript.append(f"{name}: {text}")
        self._send(pcm)
        if own:
            with self._answer_lock:
                if not self.interrupt.is_set():
                    self.playing.clear()  # waiting for the answer again: listen with the normal threshold
        self._last_output_at = time.monotonic()
        return True

    def _send(self, pcm: np.ndarray):
        data = encode_g711(pcm, self.codec)
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

    # ------------------------------------------------------------------ cached phrases
    def _phrase_texts(self) -> dict:
        return ph.TEXTS.get(ph.lang_of(self.language, self.last_text), {})

    def _queue_phrase(self, text: str, kind: str, before_answer: bool = True) -> bool:
        """Plays a cached phrase without waiting for TTS; before_answer: only while no answer audio is ready."""
        pcm = self.phrases.get(text) if self.phrases is not None and text else None
        if pcm is None:
            return False
        with self._answer_lock:
            if self.interrupt.is_set() or (before_answer and self._turn_audio):
                return False
            self._turn_phrase = True
            self._last_phrase = text
            self.audio_q.put((text, pcm, kind))
        return True

    def _play_greeting(self, text: str) -> bool:
        if self.phrases is None or not text:
            return False
        pcm = self.phrases.get(text, synthesize=True)  # normally cached at startup
        if pcm is None:
            return False
        self.audio_q.put((text, pcm, GREETING))
        return True

    def _watch(self, token: int, t_end: float, spec):
        """Filler when the answer is slow, "still on it" when nothing has been said for a while."""
        delay = float(self.cfg.filler_delay_ms or 0) / 1000
        still = float(self.cfg.still_working_s or 0)
        filler_due = delay > 0
        stills = 0
        while self._turn_token == token and self.alive():
            time.sleep(0.05)
            if spec is not None and not spec.final.is_set():
                continue  # nothing is said for a speculative answer
            now = time.monotonic()
            t = self._phrase_texts()
            if not t:
                return
            if filler_due and now >= t_end + delay:
                filler_due = False
                if self._thinking and not self._turn_phrase:
                    self._queue_phrase(ph.pick(t["fillers"], self._last_phrase), FILLER)
            idle = (self.sentence_q.unfinished_tasks == 0 and self.audio_q.unfinished_tasks == 0)
            if (still > 0 and stills < STILL_MAX and idle and now - max(self._last_output_at, t_end) >= still
                    and self._turn_token == token):
                stills += 1
                self._queue_phrase(ph.pick(t["still"], self._last_phrase), FILLER, before_answer=False)
                self._last_output_at = now

    # ------------------------------------------------------------------ hooks for Brain.turn
    def _await_confirm(self, spec) -> bool:
        """Speculative turn: waits until the pause is long enough (True) or the caller goes on speaking."""
        while not (spec.final.is_set() or spec.cancelled.is_set()) and self.alive():
            spec.final.wait(0.02)
        if spec.cancelled.is_set() or not spec.final.is_set():
            return False
        self._note_heard(self._turn_heard)
        return True

    def _abort(self) -> bool:
        spec = self._spec_turn
        return (self.interrupt.is_set() or not self.alive() or self.restart.is_set()
                or (spec is not None and spec.cancelled.is_set()))

    def _say_answer(self, text: str):
        spec = self._spec_turn
        if spec is not None and not self._await_confirm(spec):
            return  # the caller went on speaking: nothing of this answer is said
        if self._timer is not None:
            self._timer.mark("sentence")
        self._turn_said = True
        self.sentence_q.put((text, ANSWER))

    def _on_token(self):
        if self._timer is not None:
            self._timer.mark("token")

    def _before_tool(self, block) -> bool:
        """Changing tools (and hanging up) only for confirmed words; status phrase for the first tool."""
        spec = self._spec_turn
        changing = block.name in MUTATING or block.name == "hang_up"
        if changing and spec is not None and not self._await_confirm(spec):
            return False
        with self._answer_lock:
            if self.interrupt.is_set() or self.restart.is_set() or not self.alive():
                return False
            if block.name in MUTATING:
                self._side_effects = True
            phrase_ok = not (self._turn_said or self._turn_phrase)
        if phrase_ok and self.cfg.fillers and (spec is None or spec.final.is_set()):
            key = ph.tool_key(block.name, block.input, self.cfg)
            text = self._phrase_texts().get("tools", {}).get(key, "")
            self._queue_phrase(text, FILLER)
        return True

    def _after_tool(self, name: str, seconds: float):
        if self._timer is not None:
            self._timer.tool(name, seconds)

    # ------------------------------------------------------------------ turns
    def _begin_turn(self):
        with self._answer_lock:
            self._thinking = True
            self._side_effects = False
            self._turn_audio = self._turn_said = self._turn_phrase = False
            self._turn_last_sentence = 0.0
            self.restart.clear()

    def _assistant_turn(self, messages, spec=None, timer=None) -> bool:
        self._spec_turn = spec
        self._timer = timer
        self.interrupt.clear()
        with self._answer_lock:
            self._turn_token += 1
            token = self._turn_token
        if self.phrases is not None and self.cfg.fillers and timer is not None:
            threading.Thread(target=self._watch, args=(token, timer.t0, spec), name="fillers", daemon=True).start()
        try:
            hang = self.brain.turn(messages, self._say_answer, self._abort,
                                   access=self.access, language=self.language, actions=self.actions,
                                   counters=self.counters, before_tool=self._before_tool,
                                   after_tool=self._after_tool, on_token=self._on_token)
            if spec is not None:
                self._await_confirm(spec)
            self.wait_spoken()
        finally:
            with self._answer_lock:
                self._turn_token += 1  # ends the filler watch
                self._thinking = False
                if self._turn_last_sentence:
                    self._stale_before = self._turn_last_sentence
            self._spec_turn = None
            self._timer = None
            self.interrupt.clear()
        if timer is not None and (spec is None or spec.final.is_set()) and not self.restart.is_set():
            log.info(timer.line())
        return hang

    def _note_heard(self, heard):
        """Caller's words into the log and transcript, once each."""
        new = [u for u in heard if not u.noted and u.text]
        for u in new:
            u.noted = True
            log.info("%s: %s", self.caller_label, u.text)
            self.transcript.append(f"{self.caller_label}: {u.text}")
            self.last_text = u.text
        if new:
            self.caller_turns += 1

    def _stale(self, u: Utterance) -> bool:
        if u.while_thinking or u.ended_at >= self._stale_before:
            return False
        log.info("Dropped what %s said %.1f s before the last sentence of the answer (belongs to an earlier "
                 "question)", self.caller_label, self._stale_before - u.ended_at)
        return True

    def _drain(self) -> list:
        """Everything the caller said that is waiting in the queue (the end marker stays)."""
        out = []
        while True:
            try:
                u = self.utterances.get_nowait()
            except queue.Empty:
                return out
            if u is None:
                self.utterances.put(None)
                return out
            if not (u.tentative and u.cancelled.is_set()) and not self._stale(u):
                out.append(u)

    def _transcribed(self, batch) -> list:
        """Waits for the transcriptions; keeps intelligible ones. A cancelled tentative utterance is left out."""
        out = []
        for u in batch:
            while not u.done.wait(0.02):
                if (u.tentative and u.cancelled.is_set()) or not self.alive():
                    break
            if u.tentative and u.cancelled.is_set():
                continue
            if u.done.is_set() and u.text:
                out.append(u)
            elif u.done.is_set() and not u.tentative:
                log.debug("nothing intelligible in an utterance of %.1f s", len(u.pcm) / RATE)
        return out

    def _wait_input(self, timeout: float, pending=None):
        """What the caller says next: all utterances since the answer (the last one may still be tentative),
        TIMEOUT or ENDED. Utterances that belong to an earlier question are dropped."""
        deadline = time.monotonic() + timeout
        batch = list(pending or [])
        self._waiting.set()
        try:
            while True:
                try:
                    u = self.utterances.get(timeout=0.05 if batch else 0.2)
                except queue.Empty:
                    if not self.alive():
                        return ENDED
                    if batch and not self.user_speaking.is_set():
                        return batch
                    if time.monotonic() > deadline and not self.user_speaking.is_set():
                        return batch or TIMEOUT
                    continue
                if u is None:
                    return ENDED
                if not (u.tentative and u.cancelled.is_set()) and not self._stale(u):
                    batch.append(u)
                    if u.tentative:
                        return batch  # speculative start
        finally:
            self._waiting.clear()

    @staticmethod
    def _add_user(messages, text: str):
        if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], str):
            messages[-1] = {"role": "user", "content": messages[-1]["content"] + "\n" + text}
        else:
            messages.append({"role": "user", "content": text})

    def _answer(self, messages, heard):
        """Answers what the caller said (heard: utterances with text; empty for the first turn and after a
        silence note). Speech while the model is still thinking restarts the turn with everything said.
        Returns True = hang up, False = go on, or a list of utterances to wait on with when a speculative
        answer was dropped because the caller went on speaking."""
        heard = list(heard)
        while True:
            self._begin_turn()
            heard += self._transcribed(self._drain())  # said meanwhile (e.g. during the transcription)
            spec = next((u for u in heard if u.tentative and not u.final.is_set()), None)
            snapshot = [dict(m) for m in messages]
            if heard:
                self._add_user(messages, " ".join(u.text for u in heard))
            self._turn_heard = heard
            if spec is None:
                self._note_heard(heard)
            timer = self.timing.start(max(u.ended_at for u in heard)) if heard else None
            if timer is not None:
                timer.mark("detected", max(u.detected_at for u in heard))
                timer.mark("stt", max(u.stt_done_at for u in heard))
            hang = self._assistant_turn(messages, spec, timer)
            if spec is not None and not spec.final.is_set():
                messages[:] = snapshot
                if timer is not None:
                    self.timing.turns.remove(timer)
                log.debug("speculative answer dropped, %s goes on speaking", self.caller_label)
                return [u for u in heard if not u.tentative]
            if self.restart.is_set():
                messages[:] = snapshot
                if timer is not None:
                    self.timing.turns.remove(timer)
                more = self._transcribed(self._drain())
                log.info("%s spoke while the answer was being prepared: answering everything together",
                         self.caller_label)
                heard += more
                continue
            return hang

    def _converse(self, messages):
        silence = 0
        pending = []
        while self.alive():
            got = self._wait_input(float(self.cfg.silence_timeout), pending)
            pending = []
            if got is ENDED:
                return
            if got is TIMEOUT:
                silence += 1
                if silence >= 2:
                    self.say(PHRASES.get(self.language, PHRASES["de"])["silence"])
                    self.wait_spoken()
                    return
                messages.append({"role": "user", "content": "[System: The caller has not said anything for a while.]"})
                if self._answer(messages, []) is True:
                    return
                continue
            heard = self._transcribed(got)
            if any(u.tentative and u.cancelled.is_set() for u in got):
                pending = [u for u in heard if not u.tentative]  # the rest of the utterance follows
                continue
            if not heard:
                continue  # noise
            silence = 0
            result = self._answer(messages, heard)
            if isinstance(result, list):
                pending = result
            elif result:
                return

    # ------------------------------------------------------------------ listening
    def listen(self, timeout: float):
        """Next finished utterance, TIMEOUT or ENDED (used before the conversation starts)."""
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
            if u is None:
                return ENDED
            if u.tentative:
                continue
            return u

    def _text_of(self, u: Utterance) -> str:
        if self.speech is not None and not u.done.wait(30):
            return ""
        return u.text or ""

    # ------------------------------------------------------------------ call flow
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

    def _greeting(self, outbound: bool, name: str) -> str:
        """Text of the cached greeting for this call, "" = the model greets (no cache, other language)."""
        if self.phrases is None:
            return ""
        known = name if (outbound or self.access != {"guest"}) else ""
        return ph.greeting(ph.lang_of(self.language), self.cfg.assistant_name or "Claude", self.owner, known,
                           outbound=outbound, on_behalf="admin" not in self.access)

    def run(self, outbound: bool, message: str = "", caller: str = "", access=frozenset({"guest"}),
            language: str = "", caller_name: str = "", instructions: str = "", previous: str = ""):
        """previous: system note with summaries of earlier calls with this number (memory.note), may be empty."""
        self.access = frozenset(access)  # decides the tools Claude gets (Brain.tools_for)
        full = caller_name
        self.caller_label = full or caller or "unknown"            # for the log: "Jan Mobile"
        name = ph.first_name(full)                                  # to address the person: "Jan"
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
            heard = ""
            greet = self._greeting(outbound, name)
            greeted = ""
            if outbound or not greet:
                # Listen briefly: does a person answer ("Hello?") or a mailbox?
                first = self.listen(2.5)
                if first is ENDED:
                    return
                at = len(self.transcript)
                person = first is TIMEOUT or first.voiced_s <= MAILBOX_S  # long speech may be a mailbox
                if greet and person and self._play_greeting(greet):
                    greeted = greet  # right away, while the first words are transcribed and Claude prepares
                elif greet:
                    log.info("Long first utterance (%.1f s): no cached greeting, maybe a mailbox", first.voiced_s)
                if isinstance(first, Utterance):
                    heard = self._text_of(first)
                if heard:
                    self.transcript.insert(at, f"{self.caller_label}: {heard}")
                    self.last_text = heard
                    self.caller_turns += 1
            elif self._play_greeting(greet):
                greeted = greet
            said = (f"You have already said “{greeted}” (played by the phone system); do not greet or introduce "
                    "yourself again. " if greeted else "")

            if outbound:
                who = (f"{name} (number {caller})" if name
                       else f"the number {caller or 'unknown'} (name unknown; {owner} asked you to make this call — "
                            "politely ask who you are talking to)")
                intro = (f"[System: You are calling {who}. Reason: {message or 'no specific reason given'}. "
                         + rights
                         + (f"When the call was answered you heard: “{heard}”. " if heard
                            else "Nothing was heard when the call was answered. ")
                         + said
                         + "If that sounds like a mailbox: do not wait for the beep, IMMEDIATELY leave a short "
                         + "message of two or three sentences and hang up afterwards. "
                         + ("Otherwise get straight to the point.]" if greeted
                            else "Otherwise greet briefly and get straight to the point.]"))
            elif self.access != {"guest"} and name:
                intro = (f"[System: {name} is calling you (from {caller or 'unknown'}). "
                         + (f"Already said when answering: “{heard}”. " if heard else "")
                         + said
                         + rights
                         + (f"Briefly ask what you can do for {name}.]" if greeted
                            else f"Answer briefly and address {name} by name.]"))
            else:
                assistant = self.cfg.assistant_name or "the call assistant"
                known = f"The caller is probably {name}. " if name else ""
                intro = (f"[System: Someone is calling {owner}, number {caller}. This is NOT {owner}. " + known
                         + (f"When answering you heard: “{heard}”. " if heard else "")
                         + f"You are {owner}'s phone assistant. "
                         + (said if greeted else
                            "Introduce yourself (in the conversation language) as: "
                            f"This is {assistant}, {self.owner or owner}'s assistant. ")
                         + "First ask for the caller's name, then for the reason of the call "
                         + f"and whether {owner} should call back. " + rights
                         + "At the end briefly repeat what you noted, say goodbye and hang up. "
                         + f"The conversation is passed on to {owner} as a note.]")
            messages = [{"role": "user", "content": intro}]
            if self._answer(messages, []) is True or not self.alive():
                return
            self._converse(messages)
        except Exception:  # noqa: BLE001
            log.exception("Error during call")
            try:
                self.say(PHRASES.get(self.language, PHRASES["de"])["error"])
                self.wait_spoken()
            except Exception:  # noqa: BLE001
                pass
        finally:
            summary = self.timing.summary()
            if summary:
                log.info(summary)
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
