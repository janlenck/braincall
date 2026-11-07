"""State history from the recorder (/api/history/period): condensed changes, meters and measurements."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import pytest

from conftest import FakeHTTP, FakeResponse, make_cfg
from ha import HA, history_range

BERLIN = ZoneInfo("Europe/Berlin")

CURRENT = {
    "cover.garage": {"entity_id": "cover.garage", "state": "closed",
                     "attributes": {"friendly_name": "Garagentor", "device_class": "garage"}},
    "sensor.pv_energy": {"entity_id": "sensor.pv_energy", "state": "2",
                         "attributes": {"friendly_name": "PV Energie", "unit_of_measurement": "kWh",
                                        "state_class": "total_increasing"}},
    "sensor.outdoor_temp": {"entity_id": "sensor.outdoor_temp", "state": "3.1",
                            "attributes": {"friendly_name": "Außentemperatur", "unit_of_measurement": "°C",
                                           "state_class": "measurement"}},
}


def row(t: datetime, state: str) -> dict:
    """Minimal history entry as HA returns it (no_attributes, minimal_response)."""
    return {"state": state, "last_changed": t.astimezone(timezone.utc).isoformat()}


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


@pytest.fixture
def make_ha():
    def make(entity_id, rows, status=200):
        h = HA(make_cfg(ha_url="https://ha:8123", ha_token="token"))
        routes = {("GET", "/history/period/"): FakeResponse([rows] if rows else [])}
        if status == 200:
            routes[("GET", f"/states/{entity_id}")] = FakeResponse(CURRENT[entity_id])
        else:
            routes[("GET", f"/states/{entity_id}")] = FakeResponse({}, status)
        h.http = FakeHTTP(routes)
        return h
    return make


def history_url(h) -> str:
    return unquote(next(url for m, url, _ in h.http.calls if "/history/period/" in url))


# Sunday 2025-10-05 (summer time): midnight in Berlin is 22:00 UTC the day before
GARAGE_DAY = [row(utc("2025-10-04T22:00:00"), "closed"), row(utc("2025-10-05T06:10:00"), "open"),
              row(utc("2025-10-05T06:15:00"), "closed"), row(utc("2025-10-05T15:40:00"), "open"),
              row(utc("2025-10-05T15:52:00"), "closed")]


def test_day_of_a_cover(make_ha):
    h = make_ha("cover.garage", GARAGE_DAY)
    out = json.loads(h.history("cover.garage", start="2025-10-05"))
    assert out["name"] == "Garagentor"
    assert out["range"] == "Sun 2025-10-05 00:00 to Mon 2025-10-06 00:00"
    assert out["state_at_start"] == "closed" and out["state_at_end"] == "closed"
    assert out["changes"] == ["Sun 2025-10-05 08:10: open", "Sun 2025-10-05 08:15: closed",
                              "Sun 2025-10-05 17:40: open", "Sun 2025-10-05 17:52: closed"]
    assert out["time_in_state"]["open"] == "2 times, 17 min in total"
    assert out["last_time_in_state"]["open"] == "from Sun 2025-10-05 17:40 to 17:52"
    assert out["last_time_in_state"]["closed"] == "from Sun 2025-10-05 17:52 to Mon 2025-10-06 00:00"
    url = history_url(h)
    assert "/history/period/2025-10-04T22:00:00+00:00?" in url
    assert "filter_entity_id=cover.garage" in url and "end_time=2025-10-05T22:00:00+00:00" in url
    assert "minimal_response" in url and "no_attributes" in url
    raw = next(u for _, u, _ in h.http.calls if "/history/period/" in u)
    assert "+" not in raw and "2025-10-04T22%3A00%3A00%2B00%3A00" in raw  # '+' in a URL would be a space


def test_last_hours_until_now(make_ha):
    now = datetime.now(timezone.utc)
    rows = [row(now - timedelta(hours=24), "closed"), row(now - timedelta(hours=3), "open")]
    out = json.loads(make_ha("cover.garage", rows).history("cover.garage"))
    assert out["range"].endswith(" to now")
    assert out["state_now"] == "open"
    assert out["last_time_in_state"]["open"].endswith("to now (still)")
    assert out["time_in_state"]["closed"] == "1 time, 21 h 00 min in total"
    # the state at the start began before the range: no made-up start time
    assert out["last_time_in_state"]["closed"].startswith("from before ")


def test_no_change_in_range(make_ha):
    rows = [row(utc("2025-10-04T22:00:00"), "closed")]
    out = json.loads(make_ha("cover.garage", rows).history("cover.garage", start="2025-10-05"))
    assert out["changes"] == "No change in this range."
    assert out["time_in_state"] == {"closed": "1 time, 24 h 00 min in total"}


def test_repeated_states_are_collapsed(make_ha):
    rows = [row(utc("2025-10-04T22:00:00"), "closed"), row(utc("2025-10-05T06:00:00"), "closed"),
            row(utc("2025-10-05T07:00:00"), "open"), row(utc("2025-10-05T07:05:00"), "open")]
    out = json.loads(make_ha("cover.garage", rows).history("cover.garage", start="2025-10-05"))
    assert out["changes"] == ["Sun 2025-10-05 09:00: open"]


def test_only_the_most_recent_changes(make_ha):
    t0 = utc("2025-10-04T22:00:00")
    rows = [row(t0 + timedelta(minutes=10 * i), "open" if i % 2 else "closed") for i in range(41)]
    out = json.loads(make_ha("cover.garage", rows).history("cover.garage", start="2025-10-05"))
    assert len(out["changes"]) == 30
    assert out["earlier_changes_not_listed"] == 10
    assert out["changes"][-1] == "Sun 2025-10-05 06:40: closed"


def test_meter_change_with_reset(make_ha):
    rows = [row(utc("2025-10-04T22:00:00"), "100"), row(utc("2025-10-05T08:00:00"), "101.5"),
            row(utc("2025-10-05T10:00:00"), "unavailable"), row(utc("2025-10-05T11:00:00"), "103"),
            row(utc("2025-10-05T12:00:00"), "0.5"), row(utc("2025-10-05T14:00:00"), "2")]
    out = json.loads(make_ha("sensor.pv_energy", rows).history("sensor.pv_energy", start="2025-10-05"))
    assert out["change"] == "5 kWh"
    assert out["reading_at_start"] == "100 kWh" and out["reading_at_end"] == "2 kWh"
    assert out["meter_resets"] == 1
    assert out["unavailable"] == "1 time without a value"
    assert "changes" not in out


def test_measurement_min_max(make_ha):
    rows = [row(utc("2025-10-04T22:00:00"), "8.2"), row(utc("2025-10-05T04:00:00"), "5.4"),
            row(utc("2025-10-05T12:00:00"), "14.75"), row(utc("2025-10-05T20:00:00"), "9")]
    out = json.loads(make_ha("sensor.outdoor_temp", rows).history("sensor.outdoor_temp", start="2025-10-05"))
    assert out["unit"] == "°C"
    assert out["at_start"] == "8.2 °C" and out["at_end"] == "9 °C"
    assert out["min"] == "5.4 °C at Sun 2025-10-05 06:00"
    assert out["max"] == "14.8 °C at Sun 2025-10-05 14:00"
    assert out["readings"][1] == "Sun 2025-10-05 06:00: 5.4 °C"


def test_many_readings_are_not_listed(make_ha):
    t0 = utc("2025-10-04T22:00:00")
    rows = [row(t0 + timedelta(minutes=5 * i), str(10 + i % 7)) for i in range(200)]
    out = json.loads(make_ha("sensor.outdoor_temp", rows).history("sensor.outdoor_temp", start="2025-10-05"))
    assert out["readings"].startswith("200 readings, too many")
    assert out["min"].startswith("10 °C") and out["max"].startswith("16 °C")


def test_no_recorded_states(make_ha):
    out = json.loads(make_ha("cover.garage", []).history("cover.garage", start="2025-10-05"))
    assert out["result"].startswith("No recorded states")


def test_unknown_entity(make_ha):
    h = make_ha("cover.garage", GARAGE_DAY, status=404)
    assert h.history("cover.garage").startswith("Unknown entity cover.garage")
    assert not any("/history/" in url for _, url, _ in h.http.calls)


@pytest.mark.parametrize("kwargs", [{"entity_id": ""}, {"entity_id": "garage"}])
def test_invalid_entity_id(make_ha, kwargs):
    assert make_ha("cover.garage", GARAGE_DAY).history(**kwargs).startswith("Error: entity_id")


@pytest.mark.parametrize("kwargs", [{"start": "2999-01-01"}, {"start": "2025-01-01", "end": "2025-03-01"},
                                    {"start": "gestern"}, {"start": "2025-10-05", "end": "2025-10-01"}])
def test_invalid_ranges(make_ha, kwargs):
    h = make_ha("cover.garage", GARAGE_DAY)
    assert h.history("cover.garage", **kwargs).startswith("Error:")
    assert h.http.calls == []


# ---------------------------------------------------------------- history_range
def test_range_of_days_and_times():
    b, s = history_range(start="2025-10-05", end="2025-10-06", tz=BERLIN)
    assert (b.isoformat(), s.isoformat()) == ("2025-10-05T00:00:00+02:00", "2025-10-07T00:00:00+02:00")
    b, s = history_range(start="2025-10-05T08:00", end="2025-10-05T09:30", tz=BERLIN)
    assert (b.isoformat(), s.isoformat()) == ("2025-10-05T08:00:00+02:00", "2025-10-05T09:30:00+02:00")
    b, s = history_range(start="2025-10-26", tz=BERLIN)  # end of summer time: the day has 25 hours
    assert s.timestamp() - b.timestamp() == 25 * 3600


def test_range_ends_now():
    now = datetime.now(timezone.utc)
    b, s = history_range(start=(now - timedelta(hours=2)).astimezone(BERLIN).strftime("%Y-%m-%dT%H:%M"),
                         tz=BERLIN)
    assert abs(s.timestamp() - now.timestamp()) < 5
    b, s = history_range(tz=BERLIN)
    assert round(s.timestamp() - b.timestamp()) == 24 * 3600
    b, s = history_range(hours=100000, tz=BERLIN)  # clamped to the longest range, exact across DST changes
    assert round(s.timestamp() - b.timestamp()) == 31 * 86400
    b, s = history_range(hours="viel", tz=BERLIN)
    assert round(s.timestamp() - b.timestamp()) == 24 * 3600


def test_configured_timezone_is_used(make_ha):
    h = make_ha("cover.garage", GARAGE_DAY)
    h.tz = ZoneInfo("America/New_York")
    out = json.loads(h.history("cover.garage", start="2025-10-05"))
    assert out["range"] == "Sun 2025-10-05 00:00 to Mon 2025-10-06 00:00"
    assert "/history/period/2025-10-05T04:00:00+00:00?" in history_url(h)


def test_last_hours_across_dst_change(monkeypatch):
    import ha as ha_mod

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 10, 26, 11, 0, tzinfo=timezone.utc).astimezone(tz)  # 12:00 winter time

    monkeypatch.setattr(ha_mod, "datetime", Frozen)
    b, s = history_range(hours=24, tz=BERLIN)
    assert b.isoformat() == "2025-10-25T13:00:00+02:00"  # 24 real hours, not 24 hours on the wall clock
    assert s.isoformat() == "2025-10-26T12:00:00+01:00"
