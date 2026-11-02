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


@pytest.mark.xfail(strict=True, reason="#21: ha_state drops all attributes")
def test_get_state_climate_has_current_temperature(make_ha):
    st = json.loads(make_ha().get_state("climate.wohnzimmer"))
    assert "21.5" in json.dumps(st)


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


# ---------------------------------------------------------------- shopping lists
def test_shopping_reads_the_first_allowed_list(make_ha):
    out = json.loads(make_ha().shopping())
    assert out["list"] == "todo.shopping_list"
    assert [i["item"] for i in out["items"]] == ["Milch", "Eier"]


def test_shopping_other_lists_are_not_allowed(make_ha):
    h = make_ha()
    assert h.shopping("Baumarkt").startswith("Not allowed")
    assert h.shopping_edit("add", "Schrauben", "todo.baumarkt").startswith("Not allowed")
    assert h.http.posted("/services/todo/add_item") == []


def test_shopping_list_by_friendly_name(make_ha):
    h = make_ha(shopping_lists=["todo.shopping_list", "todo.baumarkt"])
    assert json.loads(h.shopping("baumarkt"))["list"] == "todo.baumarkt"


def test_shopping_no_list_configured(make_ha):
    assert make_ha(shopping_lists=["todo.gone"]).shopping().startswith("No shopping list available")


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


@pytest.mark.xfail(strict=True, reason="#24: restricted callers can book in the past")
def test_restricted_booking_in_the_past_is_refused(make_ha):
    h = make_ha(default_calendars=["calendar.family"])
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT10:00")
    h.create_event("", "x", yesterday, confirmed=True, restricted=True)
    assert created(h) == []
