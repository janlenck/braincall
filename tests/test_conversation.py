"""Snappier conversations (#53): merged and stale utterances, restart while thinking, speculative start,
fillers and status phrases, greeting, timing log and phrase cache."""
import logging
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from pyVoIP.VoIP import CallState

import phrases as ph
import session as session_mod
from conftest import make_cfg
from session import CallSession, Utterance
from timing import CallTiming


class FakeSpeech:
    """Transcribes utterances made by utt(): the first sample is the index of the text."""

    def __init__(self, stt_delay=0.0):
        self.texts = []
        self.stt_delay = stt_delay
        self.synthesized = []

    def transcribe(self, pcm, language):
        time.sleep(self.stt_delay)
        return self.texts[int(pcm[0])]

    def synthesize(self, text):
        self.synthesized.append(text)
        return np.zeros(40, dtype=np.int16)  # 5 ms


class FakeCache:
    def __init__(self):
        self.asked = []

    def get(self, text, synthesize=False):
        self.asked.append(text)
        return np.zeros(40, dtype=np.int16)


class FakeBrain:
    """Runs one script function per turn: f(ctx) -> hang, ctx has messages, say, abort, before_tool."""

    def __init__(self, *script):
        self.script = list(script)
        self.seen = []  # copy of the messages of each turn

    def turn(self, messages, on_sentence, should_abort, before_tool=None, **kw):
        self.seen.append([dict(m) for m in messages])
        f = self.script.pop(0)
        hang = f(SimpleNamespace(messages=messages, say=on_sentence, abort=should_abort, before_tool=before_tool))
        messages.append({"role": "assistant", "content": "…"})  # like Brain.turn
        return hang


def block(name, **inp):
    return SimpleNamespace(name=name, input=inp)


def make(brain=None, speech=None, phrases=None, workers=True, **cfg):
    call = SimpleNamespace(state=CallState.ANSWERED, write_audio=lambda chunk: None, hangup=lambda: None)
    s = CallSession(call, brain, speech or FakeSpeech(), make_cfg(owner_name="Jan Lenck", **cfg), phrases)
    s.caller_label = "Jan"
    if workers:
        for f in (s._tts_worker, s._play_worker):
            threading.Thread(target=f, daemon=True).start()
    return s


def utt(s, text, ended_at=None, tentative=False, queue_it=True):
    """An utterance of the caller; transcribed in the background like a real one."""
    s.speech.texts.append(text)
    t = time.monotonic() if ended_at is None else ended_at
    u = Utterance(np.full(800, len(s.speech.texts) - 1, dtype=np.int16), t, t, 0.5, tentative=tentative)
    if queue_it:
        s._finished(u) if not tentative else (s._transcribe_async(u), s.utterances.put(u))
    return u


def caller_lines(s):
    return [line for line in s.transcript if line.startswith("Jan:")]


def run_converse(s, messages=None):
    messages = messages or [{"role": "user", "content": "[System: intro]"}, {"role": "assistant", "content": "Hallo"}]
    t = threading.Thread(target=s._converse, args=(messages,), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "conversation did not end"
    s.hangup()
    return messages


def hang_up(ctx):
    return True


# ---------------------------------------------------------------- merging and stale utterances
def test_everything_said_since_the_answer_is_one_user_message():
    brain = FakeBrain(hang_up)
    s = make(brain)
    utt(s, "Nein.")
    utt(s, "Nein.")
    run_converse(s)
    assert brain.seen[0][-1] == {"role": "user", "content": "Nein. Nein."}
    assert caller_lines(s) == ["Jan: Nein.", "Jan: Nein."]
    assert s.caller_turns == 1


def test_utterance_from_before_the_last_sentence_is_dropped(caplog):
    s = make(FakeBrain(), workers=False, silence_timeout=0.3)
    s._stale_before = time.monotonic()
    utt(s, "Nein.", ended_at=s._stale_before - 2)
    with caplog.at_level(logging.INFO, logger="session"):
        assert s._wait_input(0.3) is session_mod.TIMEOUT
    assert any("belongs to an earlier question" in r.getMessage() for r in caplog.records)


def test_answer_sets_the_stale_limit_to_its_last_sentence():
    def answer(ctx):
        ctx.say("Soll ich das Licht anmachen?")
        return False

    s = make(FakeBrain(answer))
    before = time.monotonic()
    s._answer([{"role": "user", "content": "[System: intro]"}], [])
    assert s._stale_before > before
    assert s._stale(Utterance(np.zeros(10, np.int16), before, before))  # said before the question


def test_nein_twice_during_an_answer_is_not_kept():
    """Short "Nein" during an answer that was not interrupted neither interrupts nor waits for the next question."""
    from test_barge_in import NOISE, Line, speech
    line = Line()
    line.start_answer()
    line.feed([NOISE] * 100, echo=True)
    for _ in range(2):
        line.feed(speech(400) + [NOISE] * 20, echo=True)
    line.end_answer()
    line.feed([NOISE] * 40)
    assert line.utterances() == []


# ---------------------------------------------------------------- speech while the model is thinking
def test_speech_while_thinking_restarts_the_turn_with_everything():
    def first(ctx):
        utt(s, "Und in der Küche auch.")  # the caller goes on while the model thinks
        assert ctx.abort()
        ctx.say("Das kommt nicht an.")
        return False

    def second(ctx):
        ctx.say("Mache ich.")
        return True

    brain = FakeBrain(first, second)
    s = make(brain)
    utt(s, "Mach das Licht an.")
    messages = run_converse(s)
    assert brain.seen[1][-1] == {"role": "user", "content": "Mach das Licht an. Und in der Küche auch."}
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]  # nothing of turn one
    assert not any("Das kommt nicht an" in line for line in s.transcript)
    assert "Claudine: Mache ich." in s.transcript


def test_no_restart_after_a_changing_tool_ran():
    def first(ctx):
        assert ctx.before_tool(block("ha_control", entity_id="light.wohnzimmer", service="turn_on"))
        utt(s, "Und in der Küche auch.")
        assert not ctx.abort()
        ctx.say("Licht ist an.")
        return False

    def second(ctx):
        return True

    brain = FakeBrain(first, second)
    s = make(brain, fillers=False)
    utt(s, "Mach das Licht an.")
    run_converse(s)
    # said before the answer's last sentence, but not answered yet: it is the next turn
    assert brain.seen[1][-1] == {"role": "user", "content": "Und in der Küche auch."}
    assert "Claudine: Licht ist an." in s.transcript


def test_speech_during_the_first_turn_is_answered_with_the_intro():
    def first(ctx):
        utt(s, "Ist Jan da?")
        return False

    brain = FakeBrain(first, hang_up)
    s = make(brain)
    messages = [{"role": "user", "content": "[System: intro]"}]
    assert s._answer(messages, []) is True
    assert brain.seen[1] == [{"role": "user", "content": "[System: intro]\nIst Jan da?"}]
    assert messages[0] == brain.seen[1][0] and len(messages) == 2


# ---------------------------------------------------------------- speculative start
def test_short_pause_starts_speculatively_and_is_confirmed():
    from test_barge_in import NOISE, Line, speech
    line = Line(speculative_ms=300, end_silence_ms=450)
    line.s._waiting.set()
    line.feed(speech(600) + [NOISE] * 15)
    (u,) = line.utterances()
    assert u.tentative and not u.final.is_set()
    line.feed([NOISE] * 10)
    assert u.final.is_set() and not u.cancelled.is_set()
    assert line.utterances() == []  # the same utterance, not a second one


def test_speculative_start_is_cancelled_when_the_caller_goes_on():
    from test_barge_in import NOISE, Line, speech
    line = Line(speculative_ms=300, end_silence_ms=450)
    line.s._waiting.set()
    line.feed(speech(600) + [NOISE] * 15)
    (first,) = line.utterances()
    line.s._waiting.clear()  # the speculative turn runs
    line.feed(speech(800) + [NOISE] * 30)
    assert first.cancelled.is_set()
    (longer,) = line.utterances()
    assert not longer.tentative and len(longer) > len(first)


def test_no_speculative_start_while_not_waiting_or_off():
    from test_barge_in import NOISE, Line, speech
    line = Line(speculative_ms=300)
    line.feed(speech(600) + [NOISE] * 30)
    assert not line.utterances()[0].tentative
    line = Line(speculative_ms=0)
    line.s._waiting.set()
    line.feed(speech(600) + [NOISE] * 30)
    assert not line.utterances()[0].tentative


def test_dropped_speculative_answer_says_and_changes_nothing():
    def speculative(ctx):
        threading.Timer(0.1, spec.cancelled.set).start()
        assert not ctx.before_tool(block("ha_control", entity_id="light.x", service="turn_on"))
        ctx.say("Das darf nicht gesagt werden.")
        assert ctx.abort()
        return False

    def real(ctx):
        return True

    brain = FakeBrain(speculative, real)
    s = make(brain)
    spec = utt(s, "Mach das", tentative=True)
    threading.Timer(0.3, lambda: utt(s, "Mach das Licht an.")).start()
    messages = run_converse(s)
    assert brain.seen[1][-1] == {"role": "user", "content": "Mach das Licht an."}
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert caller_lines(s) == ["Jan: Mach das Licht an."]
    assert "Das darf nicht gesagt werden." not in s.speech.synthesized
    assert not s._side_effects


def test_confirmed_speculative_answer_is_spoken():
    def speculative(ctx):
        threading.Timer(0.1, spec.final.set).start()
        ctx.say("Licht ist an.")
        return True

    s = make(FakeBrain(speculative))
    spec = utt(s, "Mach das Licht an.", tentative=True)
    run_converse(s)
    assert caller_lines(s) == ["Jan: Mach das Licht an."]
    assert "Claudine: Licht ist an." in s.transcript


# ---------------------------------------------------------------- fillers and status phrases
def slow(seconds, text="Antwort kommt."):
    def f(ctx):
        time.sleep(seconds)
        ctx.say(text)
        return True
    return f


def test_slow_answer_gets_a_filler_first():
    s = make(FakeBrain(slow(0.5)), phrases=FakeCache(), filler_delay_ms=100, still_working_s=0)
    utt(s, "Wie warm ist es?")
    messages = run_converse(s)
    said = [line for line in s.transcript if line.startswith("Claudine")]
    assert said[0] in {f"Claudine: {t}" for t in ph.TEXTS["de"]["fillers"]}
    assert said[1] == "Claudine: Antwort kommt."
    assert all("Hm" not in str(m["content"]) for m in messages)  # fillers are not part of the conversation
    assert "filler" in s.timing.turns[0].marks and "audio" in s.timing.turns[0].marks


def test_fast_answer_gets_no_filler():
    s = make(FakeBrain(slow(0)), phrases=FakeCache(), filler_delay_ms=300, still_working_s=0)
    utt(s, "Hallo")
    run_converse(s)
    assert [line for line in s.transcript if line.startswith("Claudine")] == ["Claudine: Antwort kommt."]


def test_fillers_off():
    s = make(FakeBrain(slow(0.4)), phrases=FakeCache(), fillers=False, filler_delay_ms=100)
    utt(s, "Hallo")
    run_converse(s)
    assert [line for line in s.transcript if line.startswith("Claudine")] == ["Claudine: Antwort kommt."]


def test_tool_call_plays_a_status_phrase_once():
    def f(ctx):
        assert ctx.before_tool(block("ha_events"))
        assert ctx.before_tool(block("ha_search", query="x"))
        time.sleep(0.1)
        ctx.say("Du hast morgen frei.")
        return True

    s = make(FakeBrain(f), phrases=FakeCache(), filler_delay_ms=2000, still_working_s=0)
    utt(s, "Habe ich morgen Termine?")
    run_converse(s)
    said = [line for line in s.transcript if line.startswith("Claudine")]
    assert said == ["Claudine: Ich schaue in den Kalender.", "Claudine: Du hast morgen frei."]


def test_no_status_phrase_when_the_model_already_said_something():
    def f(ctx):
        ctx.say("Ich schaue nach.")
        assert ctx.before_tool(block("ha_search", query="x"))
        return True

    s = make(FakeBrain(f), phrases=FakeCache(), filler_delay_ms=2000, still_working_s=0)
    utt(s, "Wie warm ist es?")
    run_converse(s)
    assert [line for line in s.transcript if line.startswith("Claudine")] == ["Claudine: Ich schaue nach."]


def test_long_work_gets_still_on_it():
    s = make(FakeBrain(slow(0.9)), phrases=FakeCache(), filler_delay_ms=5000, still_working_s=0.3)
    utt(s, "Was hat die PV im Oktober gemacht?")
    run_converse(s)
    said = [line[len("Claudine: "):] for line in s.transcript if line.startswith("Claudine")]
    assert said[0] in ph.TEXTS["de"]["still"]
    assert said[-1] == "Antwort kommt."


def test_filler_does_not_raise_the_threshold_after_it_ended():
    """A filler on its own sets `playing` only while it plays: speech while thinking is not a barge-in."""
    s = make(workers=False)
    s._thinking = True
    s._play(np.zeros(40, np.int16), session_mod.FILLER, "Hm.")
    assert not s.playing.is_set()
    s._play(np.zeros(40, np.int16), session_mod.ANSWER, "Antwort.")
    assert s.playing.is_set() and not s._thinking


def test_pick_never_repeats_and_tool_phrases():
    options = ph.TEXTS["de"]["fillers"]
    last = ""
    for _ in range(50):
        nxt = ph.pick(options, last)
        assert nxt != last
        last = nxt
    cfg = make_cfg()
    assert ph.tool_key("ha_busy_times", {}, cfg) == "calendar"
    assert ph.tool_key("ha_control", {"entity_id": "light.x"}, cfg) == "control"
    assert ph.tool_key("ha_control", {"entity_id": "cover.garage"}, cfg) == ""  # asks for a yes first
    assert ph.tool_key("ha_control", {"entity_id": "todo.einkauf"}, cfg) == "write"
    assert ph.tool_key("ha_create_event", {"confirmed": True}, cfg) == "write"
    assert ph.tool_key("web_search", {}, cfg) == "web"
    assert ph.tool_key("hang_up", {}, cfg) == ""
    assert ph.tool_key("ha_history", {}, cfg) == "lookup"


def test_phrase_language():
    assert ph.lang_of("uk") == "uk"
    assert ph.lang_of("auto", "Привіт, як справи?") == "uk"
    assert ph.lang_of("auto", "Hallo") == "de"
    assert ph.lang_of("fr") == ""


# ---------------------------------------------------------------- greeting
@pytest.fixture
def intro(monkeypatch):
    """Runs CallSession.run up to the first assistant turn: (intro note, played phrases)."""
    def run(access, outbound=False, first=session_mod.TIMEOUT, **kw):
        s = make(workers=False, phrases=FakeCache())
        seen = []
        for name in ("_reader", "_tts_worker", "_play_worker", "hangup"):
            monkeypatch.setattr(s, name, lambda: None)
        monkeypatch.setattr(s, "listen", lambda timeout: first)
        monkeypatch.setattr(s, "_assistant_turn", lambda messages, *a: seen.append(messages[0]["content"]) or True)
        from config import parse_access
        s.run(outbound=outbound, caller="0151111", access=parse_access(access), **kw)
        played = []
        while not s.audio_q.empty():
            item = s.audio_q.get()
            if item:
                played.append(item[0])
        return seen[0], played
    return run


def test_inbound_unknown_caller_is_greeted_at_once(intro):
    note, played = intro("guest")
    assert played == ["Hallo, hier ist Claudine, die Assistentin von Jan Lenck."]
    assert "already said “Hallo, hier ist Claudine" in note and "do not greet" in note
    assert "Introduce yourself" not in note


def test_inbound_known_caller_is_greeted_by_name(intro):
    note, played = intro("admin", caller_name="Jan Mobile")
    assert played == ["Hallo Jan, hier ist Claudine."]
    assert "Briefly ask what you can do for Jan" in note


def test_outbound_intro_after_the_callee_spoke(intro):
    first = Utterance(np.zeros(4000, np.int16), 0, 0, voiced_s=0.6)
    first.text = "Ja, hallo?"
    first.done.set()
    note, played = intro("calendar", outbound=True, first=first, caller_name="Maryna", message="Termin")
    assert played == ["Hallo Maryna, hier ist Claudine, ich rufe im Auftrag von Jan Lenck an."]
    assert "you heard: “Ja, hallo?”" in note and "mailbox" in note and "do not greet" in note


def test_outbound_long_first_utterance_may_be_a_mailbox(intro):
    first = Utterance(np.zeros(40000, np.int16), 0, 0, voiced_s=4.0)
    first.text = "Hallo, Sie sind verbunden mit der Mailbox von"
    first.done.set()
    note, played = intro("guest", outbound=True, first=first)
    assert played == []
    assert "greet briefly" in note


def test_wanted_phrases_follow_the_configuration():
    cfg = make_cfg(owner_name="Jan Lenck", callers=[
        {"number": "0151", "name": "Jan Mobile", "access": "admin"},
        {"number": "0170", "name": "Maryna", "access": "calendar", "language": "uk"},
        {"number": "*", "name": "Unknown", "access": "guest"}])
    texts = ph.wanted(cfg)
    assert "Hallo, hier ist Claudine, die Assistentin von Jan Lenck." in texts
    assert "Hallo Jan, hier ist Claudine." in texts
    assert "Привіт, Maryna, це Claudine." in texts
    assert "Ich schaue in den Kalender." in texts and "Дивлюся в календар." in texts
    assert len(texts) == len(set(texts))
    assert not any("Mhm." == t for t in ph.wanted(make_cfg(fillers=False)))


# ---------------------------------------------------------------- phrase cache
def test_phrase_cache_on_disk(tmp_path):
    speech = FakeSpeech()
    cfg = make_cfg()
    cache = ph.PhraseCache(speech, cfg, tmp_path)
    assert cache.get("Hm.") is None  # not synthesized during a call
    cache.prewarm(["Hm.", "Ja."])
    assert speech.synthesized == ["Hm.", "Ja."]
    again = ph.PhraseCache(speech, cfg, tmp_path)
    assert again.get("Hm.") is not None and speech.synthesized == ["Hm.", "Ja."]  # from disk
    other = ph.PhraseCache(speech, make_cfg(tts_voice="echo"), tmp_path)
    assert other.key("Hm.") != cache.key("Hm.")
    other.prewarm(["Hm."])
    assert len(list(tmp_path.glob("*.pcm"))) == 1  # files of the old voice are removed


# ---------------------------------------------------------------- timing
def test_timing_line_and_summary():
    timing = CallTiming()
    for stt, play in ((0.8, 1.6), (1.0, 2.0), (1.2, 1.8)):
        t = timing.start(100.0)
        t.mark("stt", 100 + stt)
        t.mark("playback", 100 + play)
        t.tool("ha_search", 0.2)
    line = timing.turns[0].line()
    assert "STT +800" in line and "first audio +1600" in line and "ha_search 200 ms" in line
    summary = timing.summary()
    assert "Timing of 3 turns" in summary
    assert "STT 1000/1200" in summary and "first audio 1800/2000" in summary and "tools 200/200 (3x)" in summary


def test_turn_is_timed_from_the_end_of_speech():
    s = make(FakeBrain(slow(0)))
    utt(s, "Hallo")
    run_converse(s)
    (t,) = s.timing.turns
    assert {"detected", "stt", "sentence", "tts", "playback", "audio"} <= set(t.marks)
    assert t.marks["stt"] >= 0 and t.marks["playback"] >= t.marks["sentence"]
