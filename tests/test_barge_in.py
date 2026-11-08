"""Barge-in with synthetic audio: real speech interrupts a long answer, short noises do not (#46)."""
import logging
from types import SimpleNamespace

import numpy as np
import pytest

import session as session_mod
from audio import FRAME
from conftest import make_cfg
from session import CallSession

ECHO = 1200     # our own voice coming back on the line: below the threshold while we speak
VOICE = 6000    # the caller
NOISE = 30


def frame(level):
    """One 20 ms frame with the given RMS (a 400 Hz tone, or quiet noise)."""
    if level <= NOISE:
        return np.random.default_rng(0).normal(0, NOISE, FRAME).astype(np.int16)
    t = np.arange(FRAME) / 8000
    return (np.sin(2 * np.pi * 400 * t) * level * np.sqrt(2)).astype(np.int16)


def speech(ms, syllable=200, pause=60):
    """Speech of about `ms` milliseconds: syllables with short pauses in between."""
    out, n = [], 0
    while n * 20 < ms:
        for _ in range(syllable // 20):
            out.append(VOICE)
        for _ in range(pause // 20):
            out.append(NOISE)
        n = len(out)
    return out[: ms // 20]


class Line:
    """Feeds frames into a session with a fake clock; `playing` = our answer is going on."""

    def __init__(self, **cfg):
        self.s = CallSession(SimpleNamespace(), brain=None, speech=None, cfg=make_cfg(**cfg))
        self.now = 100.0
        self.interrupted_at = None
        self.feed([NOISE] * 50)  # settle the noise floor before the call starts

    def start_answer(self):
        self.s.answer_started_at = self.now
        self.s.playing.set()

    def end_answer(self):
        self.s.playing.clear()

    def feed(self, levels, echo=False):
        for level in levels:
            if echo and self.s.playing.is_set():
                level = max(level, ECHO)
            self.s._frame(frame(level), self.now)
            if self.s.interrupt.is_set() and self.interrupted_at is None:
                self.interrupted_at = self.now
            self.now += 0.02

    def utterances(self):
        out = []
        while not self.s.utterances.empty():
            out.append(self.s.utterances.get())
        return out


def test_long_answer_is_interrupted_after_one_second_of_speech():
    line = Line()
    line.start_answer()
    line.feed([NOISE] * 1500, echo=True)  # 30 s of answer: the echo must neither interrupt nor raise the floor
    assert not line.s.interrupt.is_set()
    assert line.s.noise < 60

    started = line.now
    line.feed(speech(1200), echo=True)
    assert line.s.interrupt.is_set()
    assert 0.9 <= line.interrupted_at - started <= 1.1
    assert not line.s.playing.is_set()  # the rest of the answer is dropped

    line.feed(speech(800) + [NOISE] * 40)  # the caller finishes the sentence
    (u,) = line.utterances()
    assert len(u) >= 100 * FRAME  # the whole utterance, from its start, is answered


def test_speech_starting_between_two_sentences_interrupts():
    """`playing` stays set for the whole answer; the grace period is only at its start."""
    line = Line()
    line.start_answer()
    line.feed([NOISE] * 200, echo=True)
    line.s.answer_started_at = line.now - 5  # grace period is long over
    line.feed(speech(1100))                  # no echo: we are waiting for the next sentence's TTS
    assert line.s.interrupt.is_set()


@pytest.mark.parametrize("noise", [
    [VOICE] * 15,                                    # 300 ms of noise
    [VOICE] * 4 + [NOISE] * 3 + [VOICE] * 4 + [NOISE] * 3 + [VOICE] * 4,  # a cough
    [VOICE] * 6,                                     # a door
    speech(700),                                     # a short "mhm, ja"
])
def test_noise_while_we_speak_neither_interrupts_nor_is_answered(noise):
    line = Line()
    line.start_answer()
    line.feed([NOISE] * 100, echo=True)
    line.feed(noise + [NOISE] * 60, echo=True)
    assert not line.s.interrupt.is_set()
    assert line.utterances() == []


def test_no_barge_in_in_the_first_moment_of_the_answer():
    line = Line(barge_in_min_ms=200)
    line.start_answer()
    line.feed(speech(1000), echo=True)  # 200 ms would be enough, but not during the first 600 ms
    assert line.interrupted_at is not None
    assert line.interrupted_at - line.s.answer_started_at > session_mod.GRACE_S


def test_no_delayed_barge_in_from_a_burst_in_the_grace_period():
    """The burst is long enough, but over when the grace period ends: no interruption afterwards."""
    line = Line(barge_in_min_ms=200)
    line.start_answer()
    line.feed([VOICE] * 15 + [NOISE] * 60, echo=True)
    assert not line.s.interrupt.is_set()
    assert line.utterances() == []


def test_speech_begun_before_the_answer_interrupts_it():
    """The caller goes on talking while the model thinks: once we speak, they can still interrupt."""
    line = Line()
    line.feed(speech(600))  # while the model is thinking
    line.start_answer()
    line.feed(speech(1000), echo=True)
    assert line.s.interrupt.is_set()
    assert line.interrupted_at - line.s.answer_started_at > session_mod.GRACE_S
    line.feed([NOISE] * 40)
    assert len(line.utterances()) == 1


def test_echo_below_the_threshold_does_not_count_as_speech():
    """After a short noise the segment stays open on our echo (above 0.7× threshold), but must not interrupt."""
    line = Line()
    line.start_answer()
    line.feed([NOISE] * 100, echo=True)
    line.feed([VOICE] * 15 + [1700] * 150)  # echo at 85 % of the threshold for 3 s
    assert not line.s.interrupt.is_set()


def test_barge_in_min_ms_is_configurable():
    line = Line(barge_in_min_ms=2000)
    line.start_answer()
    line.feed([NOISE] * 100, echo=True)
    started = line.now
    line.feed(speech(1500), echo=True)
    assert not line.s.interrupt.is_set()
    line.feed(speech(800), echo=True)
    assert 1.9 <= line.interrupted_at - started <= 2.1


def test_barge_in_off():
    line = Line(barge_in=False)
    line.start_answer()
    line.feed([NOISE] * 100, echo=True)
    line.feed(speech(2000) + [NOISE] * 60, echo=True)
    assert not line.s.interrupt.is_set()
    assert line.utterances() == []


def test_speech_that_goes_on_after_the_answer_ended_is_kept():
    line = Line()
    line.start_answer()
    line.feed([NOISE] * 100, echo=True)
    line.feed(speech(400), echo=True)  # "Ja, aber …" over our last words
    line.end_answer()
    line.feed(speech(500) + [NOISE] * 40)
    assert not line.s.interrupt.is_set()
    assert len(line.utterances()) == 1


def test_normal_utterance_when_we_are_silent():
    line = Line()
    line.feed(speech(300) + [NOISE] * 40)
    assert len(line.utterances()) == 1


def test_ignored_barge_in_is_logged(caplog):
    line = Line()
    line.start_answer()
    line.feed([NOISE] * 100, echo=True)
    with caplog.at_level(logging.DEBUG, logger="session"):
        line.feed([VOICE] * 15 + [NOISE] * 40, echo=True)
    (rec,) = [r for r in caplog.records if "ignored while speaking" in r.getMessage()]
    assert "needs 1000 ms" in rec.getMessage() and "threshold 2000" in rec.getMessage()


def test_playing_spans_the_whole_answer(monkeypatch):
    """Set by the first sentence, kept between sentences, cleared when the answer has been spoken."""
    s = CallSession(SimpleNamespace(write_audio=lambda chunk: None), brain=None, speech=None, cfg=make_cfg())
    monkeypatch.setattr(s, "alive", lambda: True)
    pcm = np.zeros(FRAME, dtype=np.int16)
    s._play(pcm)
    first = s.answer_started_at
    assert s.playing.is_set() and first > 0
    s._play(pcm)
    assert s.playing.is_set() and s.answer_started_at == first  # grace period not restarted
    s.wait_spoken()
    assert not s.playing.is_set()
    s.interrupt.set()
    s._play(pcm)
    assert not s.playing.is_set()  # nothing more is played after an interruption
