"""Speech recognition filter, TTS request and the audio helpers."""
import io
import wave

import numpy as np
import pytest

import audio
import rtpfix
import speech as speech_mod
from conftest import FakeHTTP, FakeResponse, make_cfg
from speech import Speech

PCM = np.zeros(1600, dtype=np.int16)


def stt(text, language="de", **cfg):
    s = Speech(make_cfg(openai_api_key="test", **cfg))
    s.http = FakeHTTP({("POST", "/audio/transcriptions"): FakeResponse({"text": text})})
    return s.transcribe(PCM, language), s.http.calls[0][2]["data"]


# ---------------------------------------------------------------- transcribe
@pytest.mark.parametrize("text", [
    "Wie warm ist es draußen?",
    "Привіт, як справи?",
    "Ja.",
    "Schalte das Licht im Büro an",
    "Mach Musik im Wohnzimmer an",
    "Musik lauter",
    "Mach Musik an.",
    "Die Untertitel sind zu klein",
    "Schau mal auf www.example.de nach",
    "Danke, das wars.",
    "Mach den Fernseher fürs Zuschauen an.",
    "Schalte die Untertitel der Sendung aus.",
])
def test_transcribe_keeps_real_speech(text):
    assert stt(text)[0] == text


@pytest.mark.parametrize("text", [
    "",
    "   ",
    "Untertitel im Auftrag des ZDF, 2021",
    "Vielen Dank fürs Zuschauen!",
    "Copyright WDR 2020",
    "♪",
    "♪ La la la ♪",
    "Musik",
    "[Musik]",
    "(Musik)",
    "Untertitel",
    "Untertitel von Stephanie Geiges",
    "Untertitelung des ZDF für funk, 2017",
    "Untertitel der Amara.org-Community",
    "Danke für's Zuschauen, bis zum nächsten Mal!",
    "www.zdf.de",
    "Telefongespräch auf Deutsch oder Ukrainisch (українською).",
    "你好，今天天气怎么样",  # neither Latin nor Cyrillic: misdetected language
    "a",
])
def test_transcribe_discards_hallucinations(text):
    assert stt(text)[0] == ""


def test_transcribe_owner_phrase_comes_from_owner_name():
    assert stt("Telefonat mit Maria.", owner_name="Maria Muster")[0] == ""
    assert stt("Telefonat mit Jan.", owner_name="Maria Muster")[0] == "Telefonat mit Jan."
    assert stt("Ich hatte ein Telefonat mit Maria", owner_name="Maria Muster")[0] != ""
    assert stt("Telefonat mit Maria.")[0] == "Telefonat mit Maria."  # no owner_name: nothing to match


def test_transcribe_language_or_hint():
    assert stt("Hallo", "de")[1] == {"model": "gpt-4o-transcribe", "language": "de"}
    assert stt("Hallo", "auto")[1] == {"model": "gpt-4o-transcribe", "prompt": speech_mod.STT_HINT}


# ---------------------------------------------------------------- synthesize
def tts(text, **cfg):
    s = Speech(make_cfg(openai_api_key="test", **cfg))
    raw = np.zeros(2400, dtype="<i2").tobytes()  # 100 ms at 24 kHz
    s.http = FakeHTTP({("POST", "/audio/speech"): FakeResponse(content=raw)})
    pcm = s.synthesize(text)
    return pcm, s.http.calls[0][2]["json"]


def test_synthesize_request_and_resampling():
    pcm, body = tts("Hallo Jan.", tts_voice="nova", tts_instructions="Speak natural German.")
    assert body == {"model": "gpt-4o-mini-tts", "voice": "nova", "input": "Hallo Jan.", "response_format": "pcm",
                    "instructions": "Speak natural German."}
    assert pcm.dtype == np.int16 and len(pcm) == 800  # 100 ms at 8 kHz


@pytest.mark.parametrize("instructions, expected", [
    ("Speak natural German, cheerful.", "Speak natural Ukrainian, cheerful."),
    ("Sprich natürliches Deutsch.", "Sprich natürliches Ukrainisch."),
    ("Cheerful and brisk.", "Cheerful and brisk. Language: Ukrainian, native pronunciation."),
])
def test_synthesize_switches_pronunciation_for_ukrainian(instructions, expected):
    _, body = tts("Добрий день, як справи?", tts_instructions=instructions)
    assert body["instructions"] == expected


def test_synthesize_tts1_gets_no_instructions():
    _, body = tts("Hallo", tts_model="tts-1", tts_instructions="Speak German.")
    assert "instructions" not in body


@pytest.mark.parametrize("text, language, expected", [
    ("Ich rufe Kastor unter 04542 4635 an.", "de",
     "Ich rufe Kastor unter null, vier, fünf, vier, zwei, vier, sechs, drei, fünf an."),
    ("Unter 045424635.", "de", "Unter null, vier, fünf, vier, zwei, vier, sechs, drei, fünf."),
    ("Call +44 20 7946.", "en", "Call plus, four, four, two, zero, seven, nine, four, six."),
    ("Номер 0151/23456", "uk", "Номер нуль, один, п'ять, один, два, три, чотири, п'ять, шість"),
    ("Numéro 0151 23456", "fr", "Numéro 0, 1, 5, 1, 2, 3, 4, 5, 6"),  # no words: digits one by one
])
def test_spell_numbers(text, language, expected):
    assert speech_mod.spell_numbers(text, language) == expected


@pytest.mark.parametrize("text", [
    "Um 21:15 Uhr am 09.11.2026.", "Es sind 0,5 Grad.", "Das kostet 1050 Euro.", "Am 2026-11-09.", "Ruf **611 an.",
    "James Bond 007.", "Code 0815",
])
def test_spell_numbers_leaves_other_numbers(text):
    assert speech_mod.spell_numbers(text, "de") == text


def test_synthesize_spells_phone_numbers_in_the_conversation_language():
    _, body = tts("Kastor hat die 04542 4635.")
    assert body["input"] == "Kastor hat die null, vier, fünf, vier, zwei, vier, sechs, drei, fünf."
    s = Speech(make_cfg(openai_api_key="test"))
    s.http = FakeHTTP({("POST", "/audio/speech"): FakeResponse(content=np.zeros(240, dtype="<i2").tobytes())})
    s.synthesize("Call 04542 4635.", "en")
    assert s.http.calls[0][2]["json"]["input"] == "Call zero, four, five, four, two, four, six, three, five."


# ---------------------------------------------------------------- audio helpers
def test_u8_s16_conversion():
    assert audio.u8_to_s16(bytes([128, 255, 0])).tolist() == [0, 32512, -32768]
    u8 = bytes(range(256))
    assert audio.s16_to_u8(audio.u8_to_s16(u8)) == u8


def tone(freq, seconds=0.5, rate=24000, amp=10000):
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype("<i2")


def test_resample_keeps_speech_band_and_removes_aliasing():
    low = audio.resample_24k_to_8k(tone(1000).tobytes())
    high = audio.resample_24k_to_8k(tone(6000).tobytes())  # above 4 kHz would alias into the speech band
    assert len(low) == 4000
    assert audio.rms(low[100:-100]) == pytest.approx(10000 / np.sqrt(2), rel=0.05)
    assert audio.rms(high[100:-100]) < 0.02 * audio.rms(low[100:-100])


def test_resample_edge_cases():
    assert audio.resample_24k_to_8k(b"").size == 0
    assert audio.resample_24k_to_8k(bytes(301)).size == 50  # 150 samples, odd trailing byte dropped


def test_rms():
    assert audio.rms(np.full(10, 100, dtype=np.int16)) == pytest.approx(100)
    assert audio.rms(np.zeros(0, dtype=np.int16)) == 0.0


def test_to_wav():
    with wave.open(io.BytesIO(audio.to_wav(np.arange(160, dtype=np.int16)))) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 8000, 160)


@pytest.mark.parametrize("codec", ["PCMU", "PCMA"])
def test_g711_silence_matches_rtp_padding(codec):
    """rtpfix pads short packets with the codec's silence byte; it must be what silence encodes to."""
    data = audio.encode_g711(np.zeros(160, dtype=np.int16), codec)
    assert data == rtpfix.SILENCE[codec] * 160
