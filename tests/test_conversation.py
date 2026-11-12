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
        self.languages = []

    def transcribe(self, pcm, language):
        time.sleep(self.stt_delay)
        return self.texts[int(pcm[0])]

    def synthesize(self, text, language=None):
        self.synthesized.append(text)
        self.languages.append(language)
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
        self.kw = kw
        f = self.script.pop(0)
        hang = f(SimpleNamespace(messages=messages, say=on_sentence, abort=should_abort, before_tool=before_tool,
                                 server_tool=kw.get("on_server_tool")))
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


def test_web_search_gets_its_phrase_even_after_a_thinking_sound():
    """A search takes a few seconds: "Ich suche das kurz raus." before the first one, also after "Hmm…"."""
    def f(ctx):
        time.sleep(0.3)  # the filler plays meanwhile
        ctx.server_tool("web_search")
        ctx.server_tool("web_fetch")
        time.sleep(0.1)
        ctx.say("Heute bis 20 Uhr.")
        return True

    brain = FakeBrain(f)
    s = make(brain, phrases=FakeCache(), filler_delay_ms=100, still_working_s=0)
    s.web = None
    utt(s, "Wann hat der Baumarkt auf?")
    run_converse(s)
    said = [line[len("Claudine: "):] for line in s.transcript if line.startswith("Claudine")]
    assert said[0] in ph.FILLERS
    assert said[1:] == ["Ich suche das kurz raus.", "Heute bis 20 Uhr."]
    assert brain.kw["web"] is None and brain.kw["who"] == "Jan (phone)"


def test_no_web_phrase_after_the_answer_started():
    def f(ctx):
        ctx.say("Ich schaue im Netz.")
        ctx.server_tool("web_search")
        return True

    s = make(FakeBrain(f), phrases=FakeCache(), filler_delay_ms=2000, still_working_s=0)
    utt(s, "Wann fährt der Zug?")
    run_converse(s)
    assert [line for line in s.transcript if line.startswith("Claudine")] == ["Claudine: Ich schaue im Netz."]


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
        return seen[0] if seen else "", played
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


def long_first(text):
    first = Utterance(np.zeros(40000, np.int16), 0, 0, voiced_s=4.0)
    first.text = text
    first.done.set()
    return first


def test_outbound_long_first_utterance_may_be_a_mailbox(intro):
    """Not known yet: no greeting into it, the model greets (and decides if nothing more gives it away)."""
    note, played = intro("guest", outbound=True, first=long_first("Praxis Doktor Sommer, Sie sprechen mit Frau Ott"))
    assert played == []
    assert "greet briefly" in note and "recognized a mailbox" not in note


def test_outbound_mailbox_announcement_is_recognized(intro):
    note, played = intro("guest", outbound=True, first=long_first("Hallo, Sie sind verbunden mit der Mailbox von"),
                         caller_name="Mark")
    greeting = "Hallo Mark, hier ist Claudine, ich rufe im Auftrag von Jan Lenck an."
    assert played == [greeting]  # queued for after the beep
    assert "recognized a mailbox announcement" in note and f"after the beep the phone system says “{greeting}”" in note
    assert "request to call Jan back" in note and "nothing from earlier calls" in note and "no tools" in note


def test_outbound_mailbox_without_a_message_hangs_up_without_a_turn(intro):
    note, played = intro("guest", outbound=True, first=long_first("Die Mobilbox von null eins fünf eins"),
                         leave_message=False)
    assert note == "" and played == []  # no turn at all: nothing is said, Claude is not asked


# ---------------------------------------------------------------- mailbox (#65)
@pytest.mark.parametrize("text", [
    "Hallo, Sie sind verbunden mit der Mailbox von null eins fünf eins",
    "Dies ist die Mobilbox von Jan", "Die Mobil Box der Nummer", "Willkommen bei der Sprachbox",
    "Bitte sprechen Sie Ihre Nachricht nach dem Ton.", "Hinterlassen Sie eine Nachricht nach dem Signalton",
    "Sprechen Sie nach dem Piep", "Hier ist der Anrufbeantworter von Familie Lenck",
    "Please leave a message after the tone", "You have reached the voicemail of", "after the beep",
    "Залиште повідомлення після сигналу", "Це автовідповідач",
])
def test_mailbox_words(text):
    assert session_mod.MAILBOX_WORDS.search(text)


@pytest.mark.parametrize("text", [
    "Ja, hallo?", "Restaurant Seeblick, guten Abend, was kann ich für Sie tun?",
    "Herr Sommer ist gerade nicht erreichbar, möchten Sie eine Nachricht hinterlassen?",  # a person
    "Der Ton ist schlecht, ich höre Sie kaum", "Nach dem Tonstudio rufe ich zurück",
])
def test_no_mailbox_words(text):
    assert not session_mod.MAILBOX_WORDS.search(text)


@pytest.fixture
def mailbox(monkeypatch):
    """An outbound call in its opening; what was heard so far is given as text. The pause that ends an
    announcement is shortened."""
    monkeypatch.setattr(session_mod, "MAILBOX_PAUSE_S", 0.05)

    def start(*script, heard="Hier ist die Mailbox von null eins", greet="Hallo Mark, hier ist Claudine.", **cfg):
        s = make(FakeBrain(*script), phrases=FakeCache(), **cfg)
        s.caller_label = "Mark"
        s._opening = True
        s._callee_text = heard
        s._greet = greet
        s.caller_turns = 1
        return s
    return start


def message(ctx):
    ctx.say("Jan möchte wissen, wann du kommst.")
    ctx.say("Bitte ruf ihn zurück.")
    return False  # even without hang_up the phone system ends the call


INTRO = [{"role": "user", "content": "[System: intro]"}]


def test_mailbox_gets_the_message_after_the_announcement(mailbox, caplog):
    s = mailbox(message)
    s._in_speech = True  # the announcement is still running
    messages = [dict(m) for m in INTRO]
    result = []
    with caplog.at_level(logging.INFO, logger="session"):
        t = threading.Thread(target=lambda: result.append(s._answer(messages, [])), daemon=True)
        t.start()
        time.sleep(0.4)
        assert s.transcript == [] and s.mailbox and not s.message_left  # prepared, but nothing said into it
        utt(s, "Bitte sprechen Sie Ihre Nachricht nach dem Ton.")  # the rest: restarts nothing
        s._in_speech = False
        s._heard_at = time.monotonic()
        t.join(5)
    assert result == [True] and s.message_left and not s.restart.is_set()
    assert s.transcript == ["Mark (mailbox): Bitte sprechen Sie Ihre Nachricht nach dem Ton.",
                            "Claudine: Hallo Mark, hier ist Claudine.", "Claudine: Jan möchte wissen, wann du kommst.",
                            "Claudine: Bitte ruf ihn zurück."]
    assert len(s.brain.seen) == 1 and s.brain.kw["call"]["mailbox"] is True  # Brain.turn: no tools but hang_up
    note = s.brain.seen[0][-1]["content"]
    assert note.startswith("[System: intro]\n[System: The phone system recognized a mailbox announcement")
    assert "says “Hallo Mark, hier ist Claudine.”" in note
    assert any("Mailbox announcement recognized" in r.getMessage() for r in caplog.records)
    assert not any("filler" in r.getMessage() or "interrupts" in r.getMessage() for r in caplog.records)


def test_mailbox_shows_only_in_a_later_part_of_the_announcement(mailbox):
    """The first part has no word of a mailbox: the turn restarts with each part until one gives it away."""
    def first(ctx):
        utt(s, "Bitte sprechen Sie Ihre Nachricht nach dem Signalton.")
        while not ctx.abort():
            time.sleep(0.01)
        return False

    s = mailbox(first, message, heard="Der gewünschte Teilnehmer null eins fünf eins")
    messages = [dict(m) for m in INTRO]
    assert s._answer(messages, []) is True and s.mailbox and s.message_left
    said = s.brain.seen[1][-1]["content"]
    assert "Bitte sprechen Sie Ihre Nachricht nach dem Signalton.\n[System: The phone system recognized" in said
    assert s.transcript[0] == "Mark: Bitte sprechen Sie Ihre Nachricht nach dem Signalton."


def test_mailbox_without_leave_message_gets_no_word(mailbox):
    s = mailbox()  # no script: a turn would fail
    s.leave_message = False
    assert s._answer([dict(m) for m in INTRO], []) is True
    assert s.mailbox and not s.message_left and s.transcript == [] and "mailbox" not in s.call_info


def test_mailbox_without_a_cached_greeting_lets_claude_introduce_herself(mailbox):
    s = mailbox(message, greet="")
    assert s._answer([dict(m) for m in INTRO], []) is True
    assert "Start with who you are and for whom you are calling" in s.brain.seen[0][-1]["content"]
    assert s.transcript[0] == "Claudine: Jan möchte wissen, wann du kommst."


def test_mailbox_that_hangs_up_before_the_message_got_none(mailbox):
    def late(ctx):
        s.call.state = CallState.ENDED
        ctx.say("Jan möchte wissen, wann du kommst.")
        return False

    s = mailbox(late)
    assert s._answer([dict(m) for m in INTRO], []) is True
    assert s.mailbox and not s.message_left  # the task tries again (main.settle_task)


def test_error_on_a_mailbox_is_not_spoken_as_the_message(monkeypatch):
    """An API failure must not put "Bei mir ist ein Fehler aufgetreten" on the recording and end the task."""
    monkeypatch.setattr(session_mod, "MAILBOX_PAUSE_S", 0.05)

    def boom(ctx):
        raise RuntimeError("API down")

    for brain, heard, said in ((FakeBrain(boom), "Hier ist die Mailbox von null eins", []),
                               (FakeBrain(boom), "Ja, hallo?", ["Bei mir ist gerade ein Fehler aufgetreten. "
                                                                 "Ich lege auf."])):
        speech = FakeSpeech()
        s = make(brain, speech, workers=False)
        monkeypatch.setattr(s, "_reader", lambda: None)
        first = long_first(heard)
        monkeypatch.setattr(s, "listen", lambda timeout, first=first: first)
        s.run(outbound=True, caller="0170222")
        assert speech.synthesized == said and not s.message_left


def test_greeting_already_playing_is_cut_when_the_mailbox_shows(mailbox):
    """A short first utterance gets the greeting at once, before its words are known."""
    s = mailbox(message)
    s.playing.set()
    started, cut = threading.Event(), []

    def long_greeting(pcm):  # stands for the greeting being sent: two seconds, unless it is interrupted
        started.set()
        deadline = time.monotonic() + 2
        while not s.interrupt.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        cut.append(s.interrupt.is_set())

    send = s._send
    s._send = long_greeting
    s.audio_q.put(("Hallo Mark, hier ist Claudine.", np.zeros(40, np.int16), session_mod.GREETING))
    assert started.wait(2)
    s._send = send
    assert s._answer([dict(m) for m in INTRO], []) is True and s.message_left
    assert cut == [True]
    assert s.transcript.count("Claudine: Hallo Mark, hier ist Claudine.") == 2  # cut, and again after the beep


def test_speech_into_the_message_is_a_person_after_all(mailbox):
    def cut(ctx):
        ctx.say("Jan möchte wissen, wann du kommst.")
        s.sentence_q.join()
        s.interrupt.set()  # barge-in: more than a second of speech while we talk
        return False

    s = mailbox(cut)
    messages = [dict(m) for m in INTRO]
    assert s._answer(messages, []) is False  # the call goes on
    assert not s.mailbox and not s.message_left and "mailbox" not in s.call_info
    assert "this is a person, not a mailbox" in messages[-1]["content"]


def test_no_thinking_sounds_into_a_possible_mailbox_announcement(mailbox):
    s = mailbox(heard="Praxis Doktor Sommer, Sie sprechen mit")
    s._suspect = True  # long first utterance
    assert not s._queue_phrase("Hmm…", session_mod.FILLER) and s.audio_q.empty()
    s._opening = False  # we answered and were heard out: a person
    assert s._queue_phrase("Hmm…", session_mod.FILLER)
    s.mailbox = True
    assert not s._queue_phrase("Ich bin noch dran.", session_mod.FILLER, before_answer=False)


def test_the_opening_ends_with_the_first_answer_that_was_heard_out(mailbox):
    def answer(ctx):
        ctx.say("Hallo, hier ist Claudine.")
        return False

    s = mailbox(answer, answer, heard="Ja, hallo?")
    assert s._answer([dict(m) for m in INTRO], []) is False and not s._opening
    utt(s, "Ich dachte schon, da geht nur die Mailbox ran.")  # said by a person later on
    assert s._answer([dict(m) for m in INTRO], s._transcribed(s._drain())) is False and not s.mailbox


def test_sound_of_the_other_side_is_timed_for_the_end_of_the_announcement():
    s = make(workers=False)
    loud, quiet = np.full(160, 4000, np.int16), np.zeros(160, np.int16)
    for i in range(10):
        s._frame(loud, 100.0 + i * 0.02)
    assert s._in_speech and s._heard_at == pytest.approx(100.18)
    for i in range(40):
        s._frame(quiet, 100.2 + i * 0.02)
    assert not s._in_speech and s._heard_at == pytest.approx(100.18)  # silence does not move it
    for i in range(3):
        s._frame(loud, 101.0 + i * 0.02)
    assert s._in_speech and s._heard_at == pytest.approx(101.04)  # also the short beep counts from its start


def test_mailbox_call_from_answer_to_hang_up(monkeypatch):
    """#65 as a whole, with the real reader and voice detection: the announcement comes in parts with pauses,
    then the beep. One turn, nothing played before the beep is over, then greeting and message, then hang up."""
    monkeypatch.setattr(session_mod, "MAILBOX_S", 0.4)
    monkeypatch.setattr(session_mod, "MAILBOX_PAUSE_S", 0.5)
    loud = bytes(0x80 + (60 if i % 2 else -60) for i in range(160))
    parts = [(0.1, 0.7), (1.1, 1.5), (1.9, 2.5), (2.8, 3.0)]  # seconds after the answer; the last one is the beep
    texts = ["Hier ist die Mailbox von null eins", "fünf eins", "Bitte sprechen Sie Ihre Nachricht nach dem Ton.", ""]
    t0 = time.monotonic()
    written = []

    def read_audio(n, blocking):
        t = time.monotonic() - t0
        return loud if any(a <= t < b for a, b in parts) else b"\x80" * 160

    def hangup():
        call.state = CallState.ENDED

    call = SimpleNamespace(state=CallState.ANSWERED, read_audio=read_audio, hangup=hangup, RTPClients=[],
                           write_audio=lambda chunk: written.append(time.monotonic() - t0))
    speech = FakeSpeech()
    speech.transcribe = lambda pcm, language: texts.pop(0)
    brain = FakeBrain(message)
    s = CallSession(call, brain, speech, make_cfg(owner_name="Jan Lenck", end_silence_ms=200, speculative_ms=0),
                    FakeCache())
    s.run(outbound=True, message="Frag, wann er kommt.", caller="0170222", caller_name="Mark Tee")
    assert len(brain.seen) == 1 and brain.kw["call"]["mailbox"] is True
    assert "you heard: “Hier ist die Mailbox von null eins”" in brain.seen[0][0]["content"]
    assert written and written[0] >= 3.0 + 0.5 - 0.1  # not a sound of ours before the beep and the pause are over
    assert s.mailbox and s.message_left and call.state == CallState.ENDED
    assert s.transcript == [
        "Mark Tee: Hier ist die Mailbox von null eins", "Mark Tee (mailbox): fünf eins",
        "Mark Tee (mailbox): Bitte sprechen Sie Ihre Nachricht nach dem Ton.",
        "Claudine: Hallo Mark, hier ist Claudine, ich rufe im Auftrag von Jan Lenck an.",
        "Claudine: Jan möchte wissen, wann du kommst.", "Claudine: Bitte ruf ihn zurück."]


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
    assert not any("Hmm…" == t for t in ph.wanted(make_cfg(fillers=False)))


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


# ---------------------------------------------------------------- review fixes
def test_outbound_timeout_without_greeting_audio_does_not_crash(monkeypatch):
    s = make(workers=False, phrases=SimpleNamespace(get=lambda text, synthesize=False: None))
    seen = []
    for name in ("_reader", "_tts_worker", "_play_worker", "hangup"):
        monkeypatch.setattr(s, name, lambda: None)
    monkeypatch.setattr(s, "listen", lambda timeout: session_mod.TIMEOUT)
    monkeypatch.setattr(s, "_assistant_turn", lambda messages, *a: seen.append(messages[0]["content"]) or True)
    s.run(outbound=True, caller="0151111")
    assert "greet briefly" in seen[0]  # the model greets, the call goes on


def test_auto_language_phrases_only_for_known_languages():
    assert ph.lang_of("auto", "") == "de"  # German at the start, like the prompt
    assert ph.lang_of("auto", "Ist das Licht an?") == "de"
    assert ph.lang_of("auto", "Is the light on in the kitchen?") == ""
    s = make(workers=False, language="auto")
    s.last_text = "Could you check the weather?"
    assert s._phrase_texts() == {}


def test_wanted_phrases_for_auto_callers_and_callers_without_name():
    cfg = make_cfg(owner_name="Jan Lenck", callers=[
        {"number": "0170", "name": "Maryna", "access": "calendar", "language": "auto"},
        {"number": "0151", "name": "", "access": "admin"}])
    texts = ph.wanted(cfg)
    assert "Дивлюся в календар." in texts and "Ich schaue in den Kalender." in texts
    assert texts.count("Hmm…") == 1  # thinking sounds are the same for every language
    assert "Hallo Jan, hier ist Claudine." in texts  # main.caller_name falls back to the owner name


def test_read_only_tool_is_not_run_after_the_speculation_was_cancelled():
    s = make(workers=False)
    spec = Utterance(np.zeros(10, np.int16), 0, 0, tentative=True)
    s._spec_turn = spec
    assert s._before_tool(block("ha_search", query="x"))
    spec.cancelled.set()
    assert not s._before_tool(block("ha_search", query="x"))


def test_restart_signal_comes_after_the_utterance_is_queued(monkeypatch):
    s = make(workers=False)
    s._thinking = True
    queued = []
    monkeypatch.setattr(s.restart, "set", lambda: queued.append(s.utterances.qsize()))
    utt(s, "Und noch etwas.")
    assert queued == [1]


def test_answer_waits_while_the_caller_adds_something():
    """Speech that began while the model thought: the answer does not start over it, the turn restarts."""
    s = make(workers=False)
    s._thinking = True
    s._in_speech = s._seg_thinking = True
    played = []
    t = threading.Thread(target=lambda: played.append(s._play(np.zeros(40, np.int16), session_mod.ANSWER, "A.")))
    t.start()
    time.sleep(0.2)
    assert played == [] and s.transcript == []
    s._in_speech = False
    utt(s, "ach, und außerdem")  # the utterance ends while we still think: restart
    t.join(2)
    assert played == [False] and s.restart.is_set()


def test_hold_for_the_caller_is_limited(monkeypatch):
    monkeypatch.setattr(session_mod, "HOLD_MAX_S", 0.1)
    s = make(workers=False)
    s._thinking = s._in_speech = s._seg_thinking = True  # constant noise
    assert s._play(np.zeros(40, np.int16), session_mod.ANSWER, "A.")


def test_phone_numbers_are_spoken_in_the_language_of_the_caller():
    s = make(language="auto")
    s.last_text = "Яка в нього номер?"
    s.say("045424635.")  # nothing but a number: its own text says nothing about the language
    s.sentence_q.join()
    assert s.speech.languages == ["uk"]
