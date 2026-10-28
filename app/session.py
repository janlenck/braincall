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
        self.playing = threading.Event()
        self.interrupt = threading.Event()

        self.sentence_q: queue.Queue = queue.Queue()
        self.audio_q: queue.Queue = queue.Queue()
        self.transcript: list[str] = []
        self.restricted = False
        self.seg_voiced = 0
        self.language = cfg.language or "de"
        self.caller_label = "caller"
        self.owner = (cfg.owner_name or "").strip()                 # "Jan Lenck"
        self.owner_first = self.owner.split()[0] if self.owner else "the owner"
        self.codec = rtpfix.setup(call)
        self.play_started_at = 0.0

    # ------------------------------------------------------------------ Status
    def alive(self) -> bool:
        return self.active and self.call.state == CallState.ANSWERED

    # ------------------------------------------------------------------ input
    def _reader(self):
        """Reads one frame every 20 ms (non-blocking; pyVoIP would otherwise block on digital silence)."""
        end_frames = max(10, int(self.cfg.end_silence_ms / 20))
        noise = 200.0
        voiced = 0
        silent = 0
        in_speech = False
        started_while_playing = False
        self.seg_voiced = 0
        buf: list[np.ndarray] = []
        pre = collections.deque(maxlen=15)  # 300 ms pre-roll
        next_t = time.monotonic()

        while self.alive():
            try:
                data = self.call.read_audio(FRAME, False)
            except Exception:  # noqa: BLE001  call ended
                break
            a = u8_to_s16(data)
            e = rms(a)
            thr = max(float(self.cfg.vad_min_energy), noise * 3.0)
            if self.playing.is_set():
                thr *= 4.0  # do not mistake the echo of our own voice for the caller

            if not in_speech:
                pre.append(a)
                if e > thr:
                    voiced += 1
                else:
                    voiced = 0
                    noise = max(50.0, 0.95 * noise + 0.05 * e)
                need = 20 if self.playing.is_set() else 3  # while speaking, 400 ms of real speech are needed
                if voiced >= need:
                    self.seg_voiced = voiced
                    in_speech = True
                    started_while_playing = self.playing.is_set()
                    buf = list(pre)
                    silent = 0
                    self.speech_started_at = time.monotonic()
                    self.user_speaking.set()
            else:
                buf.append(a)
                silent = 0 if e > thr * 0.7 else silent + 1
                if silent == 0:
                    self.seg_voiced += 1
                if silent >= end_frames or len(buf) >= 1500:
                    in_speech = False
                    voiced = 0
                    self.user_speaking.clear()
                    speech_frames = len(buf) - silent
                    keep = speech_frames >= 10
                    if started_while_playing and not self.cfg.barge_in:
                        keep = False
                    min_frames = int(float(self.cfg.barge_in_min_ms or 1000) / 20)
                    if started_while_playing and self.seg_voiced < min_frames:
                        keep = False  # noise/throat-clearing while we speak -> ignore
                    if keep:
                        self.utterances.put(np.concatenate(buf))
                    pre.clear()

            next_t += FRAME / RATE
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()

        self.active = False
        self.utterances.put(None)

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

    def _barge(self) -> bool:
        now = time.monotonic()
        # counts only actually voiced 20 ms frames; pauses and fading noise do not count
        need_frames = float(self.cfg.barge_in_min_ms or 1000) / 20
        return (self.cfg.barge_in and self.user_speaking.is_set()
                and now - self.play_started_at > 0.6        # ignore the very start of our own sentence
                and self.seg_voiced >= need_frames)

    def _play(self, pcm: np.ndarray):
        data = encode_g711(pcm, self.codec)
        self.play_started_at = time.monotonic()
        self.playing.set()
        t0 = time.monotonic()
        sent = 0
        try:
            for i in range(0, len(data), FRAME):
                if not self.alive() or self.interrupt.is_set():
                    return
                if self._barge():
                    log.info("%s interrupts", self.caller_label)
                    self.interrupt.set()
                    return
                while sent / RATE - (time.monotonic() - t0) > 0.08:
                    time.sleep(0.005)
                chunk = data[i:i + FRAME]
                self.call.write_audio(chunk)
                sent += len(chunk)
            # wait until the buffer has really been sent
            while sent / RATE - (time.monotonic() - t0) > 0 and self.alive():
                if self._barge():
                    self.interrupt.set()
                    return
                time.sleep(0.01)
        finally:
            self.playing.clear()

    def wait_spoken(self):
        self.sentence_q.join()
        self.audio_q.join()

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
                return text

    # ------------------------------------------------------------------ call flow
    def _assistant_turn(self, messages) -> bool:
        self.interrupt.clear()
        hang = self.brain.turn(messages, self.say,
                               lambda: self.interrupt.is_set() or not self.alive(),
                               restricted=self.restricted, language=self.language)
        self.wait_spoken()
        self.interrupt.clear()
        return hang

    def run(self, outbound: bool, message: str = "", caller: str = "", trusted: bool = True,
            language: str = "", caller_name: str = ""):
        self.restricted = not trusted  # only trusted_numbers may query and control the house
        full = caller_name if trusted else ""
        self.caller_label = full or caller or "unknown"            # for the log: "Jan Mobile"
        name = full.split("(")[0].split()[0] if full.strip() else ""  # to address the person: "Jan"
        owner = self.owner_first
        keep_quiet = (f"You have no access to the house in this call. Give no information at all about {owner}, "
                      f"the house, whether anyone is at home, or about devices. ")
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

            if outbound:
                who = (f"{name} (number {caller})" if name
                       else f"the number {caller or 'unknown'} (name unknown; {owner} asked you to make this call — "
                            "politely ask who you are talking to)")
                intro = (f"[System: You are calling {who}. Reason: {message or 'no specific reason given'}. "
                         + ("" if trusted else keep_quiet)
                         + (f"When the call was answered you heard: “{heard}”. " if heard
                            else "Nothing was heard when the call was answered. ")
                         + "If that sounds like a mailbox: do not wait for the beep, IMMEDIATELY leave a short "
                         + "message of two or three sentences and hang up afterwards. "
                         + "Otherwise greet briefly and get straight to the point.]")
            elif trusted:
                intro = (f"[System: {name} is calling you (from {caller or 'unknown'}). "
                         + (f"Already said when answering: “{heard}”. " if heard else "")
                         + f"Answer briefly and address {name} by name.]")
            else:
                assistant = self.cfg.assistant_name or "the call assistant"
                intro = (f"[System: Someone is calling {owner}, number {caller}. This is NOT {owner}. "
                         + (f"When answering you heard: “{heard}”. " if heard else "")
                         + f"You are {owner}'s phone assistant. Introduce yourself (in the conversation language) as: "
                         + f"This is {assistant}, {self.owner or owner}'s assistant. "
                         + "First ask for the caller's name, then for the reason of the call "
                         + f"and whether {owner} should call back. " + keep_quiet
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
