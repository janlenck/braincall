"""Home Assistant access: search, control limits, shopping lists, calendars and restricted bookings."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import ha as ha_mod
from conftest import FakeHTTP, FakeResponse, make_cfg
from ha import HA

STATES = [
    {"entity_id": "sensor.ebusd_hwc_storage_temp", "state": "48.5",
     "attributes": {"friendly_name": "Speicher", "unit_of_measurement": "°C", "device_class": "temperature"},
     "last_changed": "2030-01-01T10:00:00+00:00"},
    {"entity_id": "sensor.outdoor_temp", "state": "3.1",
     "attributes": {"friendly_name": "Außentemperatur", "unit_of_measurement": "°C", "device_class": "temperature"}},
    {"entity_id": "sensor.pv_power", "state": "2300",
     "attributes": {"friendly_name": "Fronius Leistung", "unit_of_measurement": "W", "device_class": "power"}},
    {"entity_id": "light.buro", "state": "off", "attributes": {"friendly_name": "Büro Licht"}},
    {"entity_id": "cover.garage", "state": "closed", "attributes": {"friendly_name": "Garagentor"}},
    {"entity_id": "lock.door", "state": "locked", "attributes": {"friendly_name": "Haustür"}},
    {"entity_id": "climate.wohnzimmer", "state": "heat",
     "attributes": {"friendly_name": "Wohnzimmer", "current_temperature": 21.5, "temperature": 22}},
    {"entity_id": "todo.shopping_list", "state": "2", "attributes": {"friendly_name": "Einkaufsliste"}},
    {"entity_id": "todo.baumarkt", "state": "1", "attributes": {"friendly_name": "Baumarkt"}},
    {"entity_id": "notify.phone", "state": "unknown", "attributes": {}},
    {"entity_id": "calendar.family", "state": "off",
     "attributes": {"friendly_name": "Familie", "supported_features": 7}},
    {"entity_id": "calendar.work", "state": "off", "attributes": {"friendly_name": "Arbeit", "supported_features": 7}},
    {"entity_id": "calendar.holidays", "state": "off",
     "attributes": {"friendly_name": "Feiertage", "supported_features": 0}},
]

TODO_ITEMS = {"service_response": {"todo.shopping_list": {"items": [
    {"summary": "Milch", "status": "needs_action"},
    {"summary": "Eier", "status": "completed", "description": "Bio"},
]}}}


def ev(summary, start, end, location=""):
    """Calendar event as Home Assistant returns it; dates (all-day) or date-times."""
    key = "date" if "T" not in start else "dateTime"
    e = {"summary": summary, "start": {key: start}, "end": {key: end}}
    if location:
        e["location"] = location
    return e


@pytest.fixture
def make_ha(monkeypatch):
    monkeypatch.setattr(ha_mod.time, "sleep", lambda s: None)

    def make(events=None, cal_error=(), **cfg):
        cfg.setdefault("ha_url", "https://ha:8123")
        cfg.setdefault("ha_token", "token")
        cfg.setdefault("calendar_max_days_ahead", 0)  # the tests book fixed dates in 2030
        h = HA(make_cfg(**cfg))
        events = events or {}

        def calendar(url, kw):
            cal = url.split("/calendars/")[1].split("?")[0]
            if cal in cal_error:
                return FakeResponse({"message": "boom"}, 500)
            return FakeResponse(events.get(cal, []))

        routes = {("GET", "/states"): FakeResponse(STATES),
                  ("GET", "/states/climate.wohnzimmer"): FakeResponse(STATES[6]),
                  ("GET", "/states/todo.shopping_list"): FakeResponse(STATES[7]),
                  ("GET", "/states/sensor.missing"): FakeResponse({}, 404),
                  ("POST", "/services/todo/get_items"): FakeResponse(TODO_ITEMS),
                  ("GET", "/calendars/"): calendar}
        for name in ("light.buro", "cover.garage"):
            routes[("GET", f"/states/{name}")] = FakeResponse(next(s for s in STATES if s["entity_id"] == name))
        h.http = FakeHTTP(routes)
        return h
    return make


# ---------------------------------------------------------------- setup
def test_supervisor_token_wins(monkeypatch):
    h = HA(make_cfg(supervisor_token="sup", ha_url="https://ha", ha_token="t"))
    assert h.base == "http://supervisor/core/api"
    assert h.http.headers["Authorization"] == "Bearer sup"


def test_disabled_without_url_or_token():
    assert not HA(make_cfg(ha_url="https://ha")).enabled
    assert not HA(make_cfg()).enabled


@pytest.mark.parametrize("value, verify", [(True, True), (False, False), ("C:/ca.pem", "C:/ca.pem"), ("", False)])
def test_verify_ssl(value, verify):
    assert HA(make_cfg(ha_url="https://ha", ha_token="t", ha_verify_ssl=value)).verify == verify


# ---------------------------------------------------------------- reading
def test_search_with_german_synonyms(make_ha):
    hits = json.loads(make_ha().search("Warmwasser Temperatur"))
    assert hits[0]["entity_id"] == "sensor.ebusd_hwc_storage_temp"


def test_search_folds_umlauts(make_ha):
    hits = json.loads(make_ha().search("Büro"))
    assert hits[0]["entity_id"] == "light.buro"


def test_search_without_hits(make_ha):
    assert make_ha().search("xyzzy").startswith("No entity found")


def test_list_by_unit_device_class_and_domain(make_ha):
    h = make_ha()
    assert [line.split(" | ")[0] for line in h.list_by("°C").splitlines()] == [
        "sensor.ebusd_hwc_storage_temp", "sensor.outdoor_temp"]
    assert h.list_by("power").startswith("sensor.pv_power")
    assert h.list_by("cover").startswith("cover.garage")
    assert h.list_by("nothing").startswith("Nothing for")


def test_list_by_limit(make_ha):
    out = make_ha().list_by("calendar", limit=1)
    assert out.endswith("… and 2 more")


def test_get_state_todo_includes_items(make_ha):
    st = json.loads(make_ha().get_state("todo.shopping_list"))
    assert st["items"] == [{"item": "Milch", "status": "open"}, {"item": "Eier", "status": "done", "note": "Bio"}]


def test_get_state_http_error(make_ha):
    assert make_ha().get_state("sensor.missing") == "Error: 404 for sensor.missing"


def test_get_state_climate_has_current_temperature(make_ha):
    st = json.loads(make_ha().get_state("climate.wohnzimmer"))
    assert st["attributes"] == {"current_temperature": 21.5, "temperature": 22}


@pytest.mark.parametrize("entity_id, attributes, expected", [
    ("cover.x", {"current_position": 40, "device_class": "garage"}, {"current_position": 40}),
    ("light.x", {"brightness": 128, "color_temp_kelvin": 3000, "supported_features": 44},
     {"brightness_pct": 50, "color_temp_kelvin": 3000}),
    ("media_player.x", {"media_title": "Song", "media_artist": "Band", "volume_level": 0.3, "source": "Radio",
                        "entity_picture": "/api/x"},
     {"media_title": "Song", "media_artist": "Band", "volume_level": 0.3, "source": "Radio"}),
    ("weather.home", {"temperature": 7.2, "temperature_unit": "°C", "humidity": 80, "wind_speed": 12,
                      "wind_speed_unit": "km/h", "attribution": "x"},
     {"temperature": 7.2, "temperature_unit": "°C", "humidity": 80, "wind_speed": 12, "wind_speed_unit": "km/h"}),
    ("sensor.x", {"unit_of_measurement": "W", "icon": "mdi:x"}, None),
])
def test_get_state_domain_attributes(make_ha, entity_id, attributes, expected):
    h = make_ha()
    h.http.routes[("GET", f"/states/{entity_id}")] = FakeResponse(
        {"entity_id": entity_id, "state": "on", "attributes": attributes})
    assert json.loads(h.get_state(entity_id)).get("attributes") == expected


def test_get_state_long_source_list_is_cut(make_ha):
    h = make_ha()
    h.http.routes[("GET", "/states/media_player.x")] = FakeResponse(
        {"entity_id": "media_player.x", "state": "on", "attributes": {"source_list": [str(i) for i in range(50)]}})
    sources = json.loads(h.get_state("media_player.x"))["attributes"]["source_list"]
    assert len(sources) == 31 and sources[-1] == "…"


def test_search_and_list_show_the_main_attributes(make_ha):
    h = make_ha()
    hit = json.loads(h.search("Wohnzimmer"))[0]
    assert hit["attributes"] == {"current_temperature": 21.5, "temperature": 22}
    assert h.list_by("climate") == "climate.wohnzimmer | Wohnzimmer | heat | current_temperature=21.5, temperature=22"
    assert h.list_by("light") == "light.buro | Büro Licht | off"


# ---------------------------------------------------------------- forecast
FORECAST = {"service_response": {"weather.home": {"forecast": [
    {"datetime": "2030-01-08T11:00:00+00:00", "condition": "rainy", "temperature": 6, "templow": 1,
     "precipitation": 4.5, "precipitation_probability": 90, "wind_bearing": 200},
    {"datetime": "2030-01-09T11:00:00+00:00", "condition": "sunny", "temperature": 8, "templow": 0},
]}}}


def forecast_ha(make_ha, response=None, weather=True):
    h = make_ha()
    states = STATES + ([{"entity_id": "weather.home", "state": "rainy",
                         "attributes": {"temperature_unit": "°C", "precipitation_unit": "mm"}}] if weather else [])
    h.http.routes[("GET", "/states")] = FakeResponse(states)
    h.http.routes[("POST", "/services/weather/get_forecasts")] = response or FakeResponse(FORECAST)
    return h


def test_forecast_daily(make_ha):
    h = forecast_ha(make_ha)
    out = json.loads(h.forecast())
    assert h.http.posted("/services/weather/get_forecasts") == [{"entity_id": "weather.home", "type": "daily"}]
    assert "return_response" in [c[1] for c in h.http.calls if "get_forecasts" in c[1]][0]
    assert out["temperature_unit"] == "°C" and out["precipitation_unit"] == "mm"
    assert out["forecast"][0] == {"time": "Tue 2030-01-08", "condition": "rainy", "temperature": 6, "templow": 1,
                                  "precipitation": 4.5, "precipitation_probability": 90}
    assert out["forecast"][1]["time"] == "Wed 2030-01-09"


def test_forecast_hourly_shows_the_time_and_unknown_type_is_daily(make_ha):
    h = forecast_ha(make_ha)
    assert json.loads(h.forecast("weather.home", "hourly"))["forecast"][0]["time"] == "Tue 2030-01-08 12:00"
    h.forecast("", "weekly")
    assert h.http.posted("/services/weather/get_forecasts")[-1]["type"] == "daily"


def test_forecast_limits_entries(make_ha):
    many = {"service_response": {"weather.home": {"forecast": [
        {"datetime": f"2030-01-08T{i:02d}:00:00+00:00", "temperature": i} for i in range(24)] * 2}}}
    h = forecast_ha(make_ha, FakeResponse(many))
    assert len(json.loads(h.forecast("", "hourly"))["forecast"]) == 24


def test_forecast_errors(make_ha):
    assert forecast_ha(make_ha).forecast("sensor.x").startswith("Error: entity_id must be")
    assert forecast_ha(make_ha, weather=False).forecast().startswith("No weather entity")
    assert forecast_ha(make_ha, FakeResponse({"message": "no"}, 400)).forecast().startswith("Error 400")
    empty = FakeResponse({"service_response": {"weather.home": {"forecast": []}}})
    assert forecast_ha(make_ha, empty).forecast() == "No daily forecast available for weather.home."


def test_local_time_is_not_utc():
    out = ha_mod._local_time("2025-01-01T10:00:00+00:00")
    assert out == "2025-01-01 11:00 local time"  # Europe/Berlin in winter, older than a day: with date
    # 30 s margin: the helper reads the clock again, a few seconds later must still count as 90 minutes
    recent = datetime.now(timezone.utc) - timedelta(minutes=90, seconds=30)
    local = recent.astimezone().strftime("%H:%M")  # same instant as the input, also across DST changes
    assert ha_mod._local_time(recent.isoformat()) == f"{local} local time (90 minutes ago)"
    assert ha_mod._local_time("garbage") == "garbage"


def test_states_are_cached(make_ha):
    h = make_ha()
    h.search("pv")
    h.list_by("W")
    assert len([c for c in h.http.calls if c[1].endswith("/states")]) == 1


# ---------------------------------------------------------------- control
def test_control_domain_not_allowed(make_ha):
    h = make_ha()
    out = h.call_service("lock.door", "unlock", confirmed=True)
    assert out.startswith("Not allowed: domain 'lock'")
    assert h.http.posted("/services/") == []


def test_control_needs_confirmation_for_confirm_domains(make_ha):
    h = make_ha()
    assert h.call_service("cover.garage", "open_cover").startswith("Safety check")
    assert h.http.posted("/services/cover") == []
    assert h.call_service("cover.garage", "open_cover", confirmed=True).startswith("Done. New state:")
    assert h.http.posted("/services/cover/open_cover") == [{"entity_id": "cover.garage"}]


def test_control_cannot_widen_the_target(make_ha):
    h = make_ha()
    h.call_service("light.buro", "light.turn_on",
                   {"brightness_pct": 50, "area_id": "house", "entity_id": "all", "device_id": "x", "label_id": "y"})
    assert h.http.posted("/services/light/turn_on") == [{"brightness_pct": 50, "entity_id": "light.buro"}]


@pytest.mark.parametrize("entity", ["", "light", None])
def test_control_invalid_entity(make_ha, entity):
    assert make_ha().call_service(entity, "turn_on").startswith("Error: entity_id")


def test_control_error_from_home_assistant(make_ha):
    h = make_ha()
    h.http.routes[("POST", "/services/light/turn_on")] = FakeResponse({"message": "bad"}, 400)
    assert h.call_service("light.buro", "turn_on").startswith("Error 400")


def _with_state(h, entity_id: str, state: str):
    h.http.routes[("GET", f"/states/{entity_id}")] = FakeResponse(
        {"entity_id": entity_id, "state": state, "attributes": {}})
    return h


def test_control_sets_time_of_input_datetime(make_ha):
    h = _with_state(make_ha(), "input_datetime.start_timer", "07:30:00")
    assert h.call_service("input_datetime.start_timer", "set_datetime",
                          {"time": "07:30:00"}).startswith("Done. New state:")
    assert h.http.posted("/services/input_datetime/set_datetime") == [
        {"time": "07:30:00", "entity_id": "input_datetime.start_timer"}]


@pytest.mark.parametrize("service", ["turn_on", "turn_off", "automation.turn_on"])
def test_control_switches_automation_on_and_off(make_ha, service):
    h = _with_state(make_ha(), "automation.r5_aircon_timer", "on")
    assert h.call_service("automation.r5_aircon_timer", service).startswith("Done. New state:")
    name = service.split(".")[-1]
    assert h.http.posted(f"/services/automation/{name}") == [{"entity_id": "automation.r5_aircon_timer"}]


@pytest.mark.parametrize("service", ["trigger", "automation.trigger", "reload", "toggle", ""])
def test_control_never_runs_an_automation(make_ha, service):
    h = make_ha()
    assert h.call_service("automation.r5_aircon_timer", service).startswith("Not allowed: automations")
    assert h.http.posted("/services/") == []


# ---------------------------------------------------------------- notify
def test_notify_entity_uses_send_message(make_ha):
    h = make_ha()
    h.notify("notify.phone", "Anruf", "Text")
    assert h.http.posted("/services/notify/send_message") == [
        {"title": "Anruf", "message": "Text", "entity_id": "notify.phone"}]


def test_notify_legacy_service(make_ha):
    h = make_ha()
    h.notify("mobile_app_pixel", "Anruf", "Text")
    assert h.http.posted("/services/notify/mobile_app_pixel") == [{"title": "Anruf", "message": "Text"}]


# ---------------------------------------------------------------- shopping list
def test_shopping_reads_the_list(make_ha):
    out = json.loads(make_ha().shopping())
    assert out["list"] == "todo.shopping_list"
    assert [i["item"] for i in out["items"]] == ["Milch", "Eier"]


def test_shopping_uses_only_the_configured_list(make_ha):
    h = make_ha(shopping_list="todo.baumarkt")
    h.http.routes[("POST", "/services/todo/get_items")] = lambda url, kw: FakeResponse({"service_response": {
        kw["json"]["entity_id"]: {"items": [{"summary": "Dübel", "status": "needs_action"}]}}})
    assert json.loads(h.shopping()) == {"list": "todo.baumarkt", "items": [{"item": "Dübel", "status": "open"}]}
    h.shopping_edit("add", "Schrauben")
    assert h.http.posted("/services/todo/add_item") == [{"entity_id": "todo.baumarkt", "item": "Schrauben"}]


def test_shopping_no_list_configured(make_ha):
    h = make_ha(shopping_list="todo.gone")
    assert h.shopping().startswith("No shopping list available")
    assert h.shopping_edit("add", "Milch").startswith("No shopping list available")
    assert h.http.posted("/services/todo/add_item") == []


def test_todo_raw_keeps_uid_and_refuses_unreadable_answers(make_ha):
    h = make_ha()
    h.http.routes[("POST", "/services/todo/get_items")] = FakeResponse({"service_response": {
        "todo.shopping_list": {"items": [{"summary": "Milch", "uid": "u1", "status": "needs_action"}]}}})
    assert h.todo_raw("todo.shopping_list") == [{"summary": "Milch", "uid": "u1", "status": "needs_action"}]
    h.http.routes[("POST", "/services/todo/get_items")] = FakeResponse({"service_response": {}})
    with pytest.raises(ValueError):
        h.todo_raw("todo.shopping_list")


def test_remove_todo(make_ha):
    h = make_ha()
    h.remove_todo("todo.shopping_list", "u1")
    assert h.http.posted("/services/todo/remove_item") == [{"entity_id": "todo.shopping_list", "item": "u1"}]
    h.http.routes[("POST", "/services/todo/remove_item")] = FakeResponse({"message": "no"}, 400)
    with pytest.raises(ValueError):
        h.remove_todo("todo.shopping_list", "u1")


@pytest.mark.parametrize("action, service, extra", [
    ("add", "add_item", {}),
    ("check", "update_item", {"status": "completed"}),
    ("uncheck", "update_item", {"status": "needs_action"}),
    ("remove", "remove_item", {}),
])
def test_shopping_edit(make_ha, action, service, extra):
    h = make_ha()
    assert h.shopping_edit(action, " Milch ").startswith("Done.")
    assert h.http.posted(f"/services/todo/{service}") == [{"entity_id": "todo.shopping_list", "item": "Milch", **extra}]


def test_shopping_edit_invalid(make_ha):
    h = make_ha()
    assert h.shopping_edit("delete_all", "x").startswith("Error")
    assert h.shopping_edit("add", "  ").startswith("Error")


# ---------------------------------------------------------------- calendars
def test_span_all_day_end_is_exclusive():
    s, e, all_day = HA._span(ev("Urlaub", "2030-01-05", "2030-01-07"))
    assert all_day and (s.day, e.day) == (5, 7)
    s, e, _ = HA._span({"summary": "x", "start": {"date": "2030-01-05"}, "end": {}})
    assert e - s == timedelta(days=1)


def test_span_timed_event_in_local_time():
    s, e, all_day = HA._span(ev("Call", "2030-01-05T09:00:00Z", "2030-01-05T10:00:00Z"))
    assert not all_day and (s.hour, e.hour) == (10, 11)


def test_events_with_details(make_ha):
    h = make_ha(default_calendars=["family"], events={"calendar.family": [
        ev("Zahnarzt", "2030-01-08T10:00:00+01:00", "2030-01-08T11:00:00+01:00", "Praxis"),
        ev("Urlaub", "2030-01-07", "2030-01-08"),
    ]})
    out = h.events(days=7, start="2030-01-07").splitlines()
    assert out[0] == "Period 2030-01-07 to 2030-01-13, calendars: Familie"
    assert out[1:] == ["Mon 2030-01-07 all-day: Urlaub  [Familie]",
                       "Tue 2030-01-08 10:00–11:00: Zahnarzt (Praxis)  [Familie]"]


def test_events_unknown_calendar(make_ha):
    assert make_ha().events(["nope"]).startswith("No matching calendar. Available: calendar.family")


def test_busy_times_without_details(make_ha):
    h = make_ha(default_calendars=["calendar.family"], all_day_free_words=["Gelber Sack"], events={"calendar.family": [
        ev("Zahnarzt", "2030-01-08T10:00:00+01:00", "2030-01-08T11:00:00+01:00", "Praxis"),
        ev("Meeting", "2030-01-08T10:30:00+01:00", "2030-01-08T12:00:00+01:00"),
        ev("Gelber Sack", "2030-01-09", "2030-01-10"),
        ev("Urlaub", "2030-01-10", "2030-01-12"),
    ]})
    out = h.busy_times(days=7, start="2030-01-07")
    assert out.splitlines()[1:] == ["Tue 2030-01-08 10:00–12:00: busy",
                                    "Thu 2030-01-10 to Fri 2030-01-11: all day busy"]
    for secret in ("Zahnarzt", "Praxis", "Meeting", "Urlaub", "Familie", "Gelber"):
        assert secret not in out


def test_busy_times_clipped_show_nothing_outside_the_period(make_ha):
    """For the grant of a planned call (#51): a holiday from the 5th to the 20th must not show beyond the days."""
    h = make_ha(default_calendars=["calendar.family"], events={"calendar.family": [
        ev("Urlaub", "2030-01-05", "2030-01-12"),
        ev("Nachtschicht", "2030-01-09T22:00:00+01:00", "2030-01-10T06:00:00+01:00"),
        ev("Reise", "2030-01-06T08:00:00+01:00", "2030-01-08T09:00:00+01:00"),
    ]})
    out = h.busy_times(days=2, start="2030-01-08", clip=True)
    assert out.splitlines()[1:] == ["Tue 2030-01-08 to Wed 2030-01-09: all day busy",
                                    "Tue 2030-01-08 00:00–09:00: busy", "Wed 2030-01-09 22:00–24:00: busy"]
    assert "2030-01-05" in h.busy_times(days=2, start="2030-01-08")  # unchanged for callers with calendar access


def test_busy_times_calendar_error_reveals_nothing(make_ha):
    h = make_ha(default_calendars=["calendar.family"], cal_error={"calendar.family"})
    out = h.busy_times(start="2030-01-07")
    assert "availability is unknown" in out
    assert "Familie" not in out and "boom" not in out


# ---------------------------------------------------------------- create_event
def created(h):
    return h.http.posted("/services/calendar/create_event")


def test_create_event_asks_first(make_ha):
    h = make_ha()
    assert h.create_event("family", "Test", "2030-01-08T10:00").startswith("Read the appointment")
    assert created(h) == []


def test_create_event_admin_timed(make_ha):
    h = make_ha(default_calendars=["family"])
    out = h.create_event("work", "Termin", "2030-01-08T14:00", confirmed=True, location="Büro")
    assert out == "Added to Arbeit: Termin."
    assert created(h) == [{"entity_id": "calendar.work", "summary": "Termin", "location": "Büro",
                           "start_date_time": "2030-01-08T14:00:00+01:00",
                           "end_date_time": "2030-01-08T15:00:00+01:00"}]


def test_create_event_all_day_end_is_inclusive_for_the_caller(make_ha):
    h = make_ha(default_calendars=["family"])
    h.create_event("", "Urlaub", "2030-01-08", "2030-01-09", confirmed=True)
    body = created(h)[0]
    assert (body["start_date"], body["end_date"]) == ("2030-01-08", "2030-01-10")


def test_create_event_read_only_calendar(make_ha):
    out = make_ha().create_event("holidays", "x", "2030-01-08", confirmed=True)
    assert "read-only" in out and "calendar.family" in out


def test_create_event_unreadable_date(make_ha):
    assert make_ha().create_event("family", "x", "next tuesday", confirmed=True).startswith("Date not readable")


def test_restricted_booking_goes_to_the_default_calendar(make_ha):
    h = make_ha(default_calendars=["calendar.family"])
    out = h.create_event("work", "Maryna kommt", "2030-01-08T14:00", confirmed=True, restricted=True)
    assert out == "Added: Maryna kommt."  # no calendar name for restricted callers
    assert created(h)[0]["entity_id"] == "calendar.family"


def test_restricted_booking_refuses_a_busy_slot(make_ha):
    h = make_ha(default_calendars=["calendar.family"], events={"calendar.family": [
        ev("Zahnarzt", "2030-01-08T13:30:00+01:00", "2030-01-08T14:30:00+01:00")]})
    out = h.create_event("", "Besuch", "2030-01-08T14:00", confirmed=True, restricted=True)
    assert out.startswith("Not added: that time is already busy")
    assert "Zahnarzt" not in out
    assert created(h) == []


def test_restricted_booking_free_all_day_reminder_does_not_block(make_ha):
    h = make_ha(default_calendars=["calendar.family"], all_day_free_words=["abfall"], events={"calendar.family": [
        ev("Abfall: Restmüll", "2030-01-08", "2030-01-09")]})
    assert h.create_event("", "Besuch", "2030-01-08T14:00", confirmed=True, restricted=True) == "Added: Besuch."


def test_restricted_booking_calendar_unreadable(make_ha):
    h = make_ha(default_calendars=["calendar.family"], cal_error={"calendar.family"})
    out = h.create_event("", "Besuch", "2030-01-08T14:00", confirmed=True, restricted=True)
    assert "NOT added" in out
    assert created(h) == []


def test_restricted_booking_read_only_default_calendar(make_ha):
    h = make_ha(default_calendars=["calendar.holidays"])
    out = h.create_event("family", "x", "2030-01-08T14:00", confirmed=True, restricted=True)
    assert out.startswith("Booking is not possible")
    assert created(h) == []


def test_restricted_booking_end_before_start(make_ha):
    h = make_ha(default_calendars=["calendar.family"])
    assert h.create_event("", "x", "2030-01-08T14:00", "2030-01-08T13:00", confirmed=True,
                          restricted=True) == "End must be after start."


def test_restricted_booking_error_hides_details(make_ha):
    h = make_ha(default_calendars=["calendar.family"])
    h.http.routes[("POST", "/services/calendar/create_event")] = FakeResponse({"message": "internal"}, 500)
    assert h.create_event("", "x", "2030-01-08T14:00", confirmed=True, restricted=True) == \
        "Appointment NOT added (error)."


def in_days(days, fmt="%Y-%m-%dT10:00"):
    return (datetime.now() + timedelta(days=days)).strftime(fmt)


def test_restricted_booking_in_the_past_is_refused(make_ha):
    h = make_ha(default_calendars=["calendar.family"])
    for start in (in_days(-1), in_days(-1, "%Y-%m-%d"), (datetime.now() - timedelta(minutes=5)).isoformat()):
        assert h.create_event("", "x", start, confirmed=True, restricted=True).startswith(
            "Not added: the start is in the past")
    assert created(h) == []


def test_restricted_booking_today_all_day_is_allowed(make_ha):
    h = make_ha(default_calendars=["calendar.family"])
    assert h.create_event("", "x", in_days(0, "%Y-%m-%d"), confirmed=True, restricted=True) == "Added: x."


def test_restricted_booking_too_long_is_refused(make_ha):
    h = make_ha(default_calendars=["calendar.family"])
    out = h.create_event("", "x", in_days(3), in_days(3, "%Y-%m-%dT14:30"), confirmed=True, restricted=True)
    assert out.startswith("Not added: a booking may last at most 4 hours")
    out = h.create_event("", "x", in_days(3, "%Y-%m-%d"), in_days(16, "%Y-%m-%d"), confirmed=True, restricted=True)
    assert out.startswith("Not added: an all-day booking may cover only one day")
    assert created(h) == []
    assert h.create_event("", "x", in_days(3), in_days(3, "%Y-%m-%dT14:00"), confirmed=True,
                          restricted=True) == "Added: x."


def test_restricted_booking_max_hours_from_config(make_ha):
    h = make_ha(default_calendars=["calendar.family"], calendar_max_hours=1.5)
    out = h.create_event("", "x", in_days(3), in_days(3, "%Y-%m-%dT12:00"), confirmed=True, restricted=True)
    assert out.startswith("Not added: a booking may last at most 1.5 hours")
    h = make_ha(default_calendars=["calendar.family"], calendar_max_hours=0)  # 0 = no limit
    assert h.create_event("", "x", in_days(3), in_days(4), confirmed=True, restricted=True) == "Added: x."


def test_restricted_booking_too_far_ahead_is_refused(make_ha):
    h = make_ha(default_calendars=["calendar.family"], calendar_max_days_ahead=90)
    out = h.create_event("", "x", in_days(91), confirmed=True, restricted=True)
    assert out.startswith("Not added: bookings are possible at most 90 days ahead")
    assert created(h) == []
    assert h.create_event("", "x", in_days(89), confirmed=True, restricted=True) == "Added: x."


def test_restricted_booking_limit_per_call(make_ha):
    h = make_ha(default_calendars=["calendar.family"])
    out = h.create_event("", "x", in_days(3), confirmed=True, restricted=True, booked=2)
    assert out.startswith("Not added: at most 2 appointment(s) can be booked per call")
    assert created(h) == []
    assert h.create_event("", "x", in_days(3), confirmed=True, restricted=True, booked=1) == "Added: x."
    h = make_ha(default_calendars=["calendar.family"], calendar_max_bookings=0)  # 0 = no limit
    assert h.create_event("", "x", in_days(3), confirmed=True, restricted=True, booked=10) == "Added: x."


def test_restricted_booking_is_checked_before_asking(make_ha):
    h = make_ha(default_calendars=["calendar.family"])
    assert h.create_event("", "x", in_days(-1), restricted=True).startswith("Not added: the start is in the past")
    assert h.create_event("", "x", in_days(3), restricted=True).startswith("Read the appointment")


def test_admin_booking_has_no_limits(make_ha):
    h = make_ha(default_calendars=["calendar.family"], calendar_max_days_ahead=90)
    assert h.create_event("", "x", in_days(-1), in_days(400), confirmed=True, booked=5) == "Added to Familie: x."
