"""Long-term statistics over the WebSocket API: energy dashboard totals and statistics of single sensors."""
import json
import ssl
from datetime import datetime, timedelta

import pytest

import ha as ha_mod
from conftest import make_cfg
from ha import HA, HAError, stat_range

PREFS = {
    "energy_sources": [
        {"type": "grid",
         "flow_from": [{"stat_energy_from": "sensor.grid_import_t1"}, {"stat_energy_from": "sensor.grid_import_t2"}],
         "flow_to": [{"stat_energy_to": "sensor.grid_export"}]},
        {"type": "solar", "stat_energy_from": "sensor.pv_energy"},
        {"type": "battery", "stat_energy_from": "sensor.bat_out", "stat_energy_to": "sensor.bat_in"},
    ],
    "device_consumption": [{"stat_consumption": "sensor.wp_energy", "name": "Wärmepumpe"},
                           {"stat_consumption": "sensor.wallbox_energy"}],
}
META = {
    "sensor.grid_import_t1": {"unit_class": "energy", "statistics_unit_of_measurement": "Wh", "has_sum": True},
    "sensor.grid_import_t2": {"unit_class": "energy", "statistics_unit_of_measurement": "kWh", "has_sum": True},
    "sensor.grid_export": {"unit_class": "energy", "statistics_unit_of_measurement": "kWh", "has_sum": True},
    "sensor.pv_energy": {"unit_class": "energy", "statistics_unit_of_measurement": "kWh", "has_sum": True},
    "sensor.bat_out": {"unit_class": "energy", "statistics_unit_of_measurement": "kWh", "has_sum": True},
    "sensor.bat_in": {"unit_class": "energy", "statistics_unit_of_measurement": "kWh", "has_sum": True},
    "sensor.wp_energy": {"unit_class": "energy", "statistics_unit_of_measurement": "kWh", "has_sum": True},
    "sensor.wallbox_energy": {"unit_class": "energy", "statistics_unit_of_measurement": "kWh", "has_sum": True,
                              "name": "Wallbox"},
    "sensor.outdoor_temp": {"unit_class": "temperature", "statistics_unit_of_measurement": "°C",
                            "has_sum": False, "mean_type": 1, "name": "Außentemperatur"},
}


def ms(day: str) -> float:
    """Local midnight of a day as milliseconds since 1970, like HA sends the start of a row."""
    return datetime.fromisoformat(day).astimezone().timestamp() * 1000


def rows(per_day: dict, key="change"):
    out = []
    for day, v in per_day.items():
        start = ms(day)
        out.append({"start": start, "end": start + 86_400_000, key: v})
    return out


STATS = {  # two days, 2025-10-01 and 2025-10-02, already converted to kWh by HA (units={"energy": "kWh"})
    "sensor.grid_import_t1": rows({"2025-10-01": 3.0, "2025-10-02": 2.0}),
    "sensor.grid_import_t2": rows({"2025-10-01": 1.0, "2025-10-02": 0.5}),
    "sensor.grid_export": rows({"2025-10-01": 4.0, "2025-10-02": 6.0}),
    "sensor.pv_energy": rows({"2025-10-01": 10.0, "2025-10-02": 14.0}),
    "sensor.bat_out": rows({"2025-10-01": 2.0, "2025-10-02": 1.0}),
    "sensor.bat_in": rows({"2025-10-01": 2.5, "2025-10-02": 3.0}),
    "sensor.wp_energy": rows({"2025-10-01": 4.2, "2025-10-02": 3.8}),
    "sensor.wallbox_energy": [],
}


class FakeWS:
    """Stands in for a websocket-client connection: answers like Home Assistant, records what was sent."""

    def __init__(self, answers, auth_ok=True):
        self.answers = answers  # type -> result, or HAError to answer with an error
        self.auth_ok = auth_ok
        self.sent = []
        self.inbox = [{"type": "auth_required", "ha_version": "2026.10.0"}]
        self.closed = False

    def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        if msg["type"] == "auth":
            self.inbox.append({"type": "auth_ok" if self.auth_ok else "auth_invalid", "message": "Invalid access"})
            return
        self.inbox.append({"id": msg["id"] + 100, "type": "event", "event": {}})  # unrelated message first
        answer = self.answers.get(msg["type"])
        if isinstance(answer, HAError):
            self.inbox.append({"id": msg["id"], "type": "result", "success": False,
                               "error": {"code": answer.code, "message": str(answer)}})
        else:
            result = answer(msg) if callable(answer) else answer
            self.inbox.append({"id": msg["id"], "type": "result", "success": True, "result": result})

    def recv(self):
        return json.dumps(self.inbox.pop(0))

    def close(self):
        self.closed = True

    def commands(self, type_):
        return [m for m in self.sent if m["type"] == type_]


def metadata(msg):
    return [{"statistic_id": i, **META[i]} for i in msg["statistic_ids"] if i in META]


def statistics(msg):
    return {i: STATS[i] for i in msg["statistic_ids"] if i in STATS}


@pytest.fixture
def connect(monkeypatch):
    """connect(answers) -> FakeWS that HA._ws() will get; records the URL and options of the connection."""
    seen = {}

    def make(answers=None, auth_ok=True):
        ws = FakeWS(answers if answers is not None else {
            "energy/get_prefs": PREFS,
            "recorder/get_statistics_metadata": metadata,
            "recorder/statistics_during_period": statistics,
        }, auth_ok)

        def create_connection(url, **kw):
            seen.update(url=url, **kw)
            return ws
        monkeypatch.setattr(ha_mod.websocket, "create_connection", create_connection)
        ws.seen = seen
        return ws
    return make


def make_ha(**cfg):
    cfg.setdefault("ha_url", "https://ha:8123")
    cfg.setdefault("ha_token", "token")
    return HA(make_cfg(**cfg))


# ---------------------------------------------------------------- connection
def test_websocket_urls():
    assert make_ha().ws_url == "wss://ha:8123/api/websocket"
    assert make_ha(ha_url="http://192.168.1.5:8123/").ws_url == "ws://192.168.1.5:8123/api/websocket"
    assert HA(make_cfg(supervisor_token="sup", ha_url="https://ha", ha_token="t")).ws_url == \
        "ws://supervisor/core/websocket"
    assert HA(make_cfg()).ws_url is None


def test_websocket_login_and_tls(connect):
    ws = connect()
    make_ha().energy("2025-10-01")
    assert ws.sent[0] == {"type": "auth", "access_token": "token"}
    assert ws.seen["url"] == "wss://ha:8123/api/websocket"
    assert isinstance(ws.seen["sslopt"]["context"], ssl.SSLContext)
    assert ws.closed


def test_websocket_without_verification(connect):
    ws = connect()
    make_ha(ha_verify_ssl=False).energy("2025-10-01")
    assert ws.seen["sslopt"] == {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
    ws = connect()
    make_ha(ha_url="http://ha:8123").energy("2025-10-01")
    assert ws.seen["sslopt"] == {}


def test_websocket_login_failure(connect):
    ws = connect(auth_ok=False)
    with pytest.raises(HAError, match="login failed"):
        make_ha().energy("2025-10-01")
    assert ws.closed


# ---------------------------------------------------------------- date ranges
def test_stat_range_day_month_year():
    b, e = stat_range("2025-10-05")
    assert (b.isoformat(), e.isoformat()) == ("2025-10-05T00:00:00+02:00", "2025-10-06T00:00:00+02:00")
    b, e = stat_range("2025-10")  # whole month, across the end of summer time
    assert (b.isoformat(), e.isoformat()) == ("2025-10-01T00:00:00+02:00", "2025-11-01T00:00:00+01:00")
    b, e = stat_range("2024-02")
    assert e.date().isoformat() == "2024-03-01"
    b, e = stat_range("2025")
    assert (b.date().isoformat(), e.date().isoformat()) == ("2025-01-01", "2026-01-01")
    b, e = stat_range("2025-10-27", "2025-11")  # end inclusive, a month as end means its last day
    assert (b.date().isoformat(), e.date().isoformat()) == ("2025-10-27", "2025-12-01")


def test_stat_range_defaults_and_order():
    b, e = stat_range()
    assert b.date() == datetime.now().date() and e - b <= timedelta(hours=25)
    b, e = stat_range("2025-10-10", "2025-10-01")
    assert (b.date().isoformat(), e.date().isoformat()) == ("2025-10-01", "2025-10-11")


def test_stat_range_limits():
    with pytest.raises(ValueError):
        stat_range("gestern")
    with pytest.raises(ValueError, match="too long"):
        stat_range("2020-01-01", "2025-01-01")


def test_invalid_dates_answer_without_connecting(connect):
    ws = connect()
    assert make_ha().energy("letzte Woche").startswith("Error:")
    assert make_ha().statistics("sensor.x", "2020", "2025").startswith("Error: Range too long")
    assert ws.sent == []


# ---------------------------------------------------------------- energy dashboard
def test_energy_totals(connect):
    ws = connect()
    out = json.loads(make_ha().energy("2025-10-01", "2025-10-02"))
    assert out["range"] == "2025-10-01 to 2025-10-02"
    assert out["totals"] == {
        "grid_import": "6.5 kWh", "grid_export": "10.0 kWh", "solar": "24.0 kWh",
        "battery_charge": "5.5 kWh", "battery_discharge": "3.0 kWh",
        "home_consumption": "18.0 kWh",          # 6.5 + 24 + 3 - 10 - 5.5
        "solar_used_in_house_approx": "8.5 kWh",  # 24 - 10 - 5.5
        "self_sufficiency": "64 %",              # 1 - 6.5 / 18
    }
    assert out["devices"] == {"Wärmepumpe": "8.0 kWh", "Wallbox": "0 kWh"}
    assert out["no_data"] == ["sensor.wallbox_energy"]
    assert "rows" not in out and "note" not in out
    q = ws.commands("recorder/statistics_during_period")[0]
    assert q["types"] == ["change"] and q["units"] == {"energy": "kWh"} and q["period"] == "day"
    assert q["start_time"] == "2025-10-01T00:00:00+02:00" and q["end_time"] == "2025-10-03T00:00:00+02:00"
    assert sorted(q["statistic_ids"]) == sorted(META.keys() - {"sensor.outdoor_temp"})


def test_energy_rows_per_day(connect):
    connect()
    out = json.loads(make_ha().energy("2025-10-01", "2025-10-02", "day"))
    assert out["rows"][0] == {"time": "Wed 2025-10-01", "grid_import": 4.0, "grid_export": 4.0, "solar": 10.0,
                              "battery_discharge": 2.0, "battery_charge": 2.5}
    assert out["rows"][1]["time"] == "Thu 2025-10-02"


def test_energy_too_many_rows(connect):
    hours = {"sensor.pv_energy": [{"start": ms("2025-10-01") + h * 3_600_000, "change": 1.0} for h in range(49)]}
    ws = connect({"energy/get_prefs": {"energy_sources": [{"type": "solar", "stat_energy_from": "sensor.pv_energy"}]},
                  "recorder/get_statistics_metadata": metadata,
                  "recorder/statistics_during_period": hours})
    out = json.loads(make_ha().energy("2025-10-01", "2025-10-03", "hour"))
    assert out["rows"].startswith("49 rows, too many")
    assert out["totals"] == {"solar": "49.0 kWh"}  # no grid meter: no home consumption
    assert ws.commands("recorder/statistics_during_period")[0]["period"] == "hour"


def test_energy_long_range_is_fetched_per_month(connect):
    ws = connect()
    make_ha().energy("2025")
    assert ws.commands("recorder/statistics_during_period")[0]["period"] == "month"


def test_energy_note_for_ranges_up_to_now(connect):
    connect()
    out = json.loads(make_ha().energy())
    assert out["note"].startswith("Statistics are compiled hourly")


def test_energy_dashboard_not_set_up(connect):
    connect({"energy/get_prefs": HAError("No prefs", "not_found")})
    assert make_ha().energy("2025-10-01").startswith("The energy dashboard is not set up")
    connect({"energy/get_prefs": {"energy_sources": [], "device_consumption": []}})
    assert make_ha().energy("2025-10-01").startswith("The energy dashboard in Home Assistant has no sensors")


def test_energy_other_errors_are_raised(connect):
    connect({"energy/get_prefs": HAError("Unauthorized", "unauthorized")})
    with pytest.raises(HAError, match="Unauthorized"):
        make_ha().energy("2025-10-01")


def test_energy_roles_newer_grid_format_and_duplicates():
    prefs = {"energy_sources": [
        {"type": "grid", "stat_energy_from": "sensor.imp", "stat_energy_to": "sensor.exp"},
        {"type": "grid", "flow_from": [{"stat_energy_from": "sensor.imp"}], "flow_to": []},
        {"type": "gas", "stat_energy_from": "sensor.gas"},
        {"type": "water", "stat_energy_from": "sensor.water"},
        {"type": "solar", "stat_energy_from": None},
    ], "device_consumption_water": [{"stat_consumption": "sensor.garden"}]}
    assert HA._energy_roles(prefs) == [("grid_import", "sensor.imp", ""), ("grid_export", "sensor.exp", ""),
                                       ("gas", "sensor.gas", ""), ("water", "sensor.water", ""),
                                       ("device_water", "sensor.garden", "")]


# ---------------------------------------------------------------- single sensors
def test_statistics_meter(connect):
    ws = connect()
    out = json.loads(make_ha().statistics("sensor.wp_energy", "2025-10-01", "2025-10-02", "day"))
    assert out["change"] == 8.0 and out["unit"] == "kWh"
    assert out["rows"] == [{"time": "Wed 2025-10-01", "change": 4.2}, {"time": "Thu 2025-10-02", "change": 3.8}]
    assert ws.commands("recorder/statistics_during_period")[0]["types"] == ["change"]


def test_statistics_measurement(connect):
    STATS["sensor.outdoor_temp"] = [
        {"start": ms("2025-10-01"), "end": ms("2025-10-02"), "mean": 10.0, "min": 4.0, "max": 15.0},
        {"start": ms("2025-10-02"), "end": ms("2025-10-03"), "mean": 12.0, "min": 6.5, "max": 18.25},
    ]
    try:
        ws = connect()
        out = json.loads(make_ha().statistics("sensor.outdoor_temp", "2025-10-01", "2025-10-02"))
    finally:
        del STATS["sensor.outdoor_temp"]
    assert out == {"entity_id": "sensor.outdoor_temp", "name": "Außentemperatur", "unit": "°C",
                   "range": "2025-10-01 to 2025-10-02", "mean": 11.0, "min": 4.0, "max": 18.2}
    assert ws.commands("recorder/statistics_during_period")[0]["types"] == ["mean", "min", "max"]


def test_statistics_iso_row_times(connect):
    """Older Home Assistant versions send the start of a row as an ISO string."""
    connect({"recorder/get_statistics_metadata": metadata,
             "recorder/statistics_during_period": {"sensor.pv_energy": [
                 {"start": "2025-09-30T22:00:00+00:00", "change": 7.0}]}})
    out = json.loads(make_ha().statistics("sensor.pv_energy", "2025-10-01", period="day"))
    assert out["rows"] == [{"time": "Wed 2025-10-01", "change": 7.0}]


def test_statistics_unknown_sensor(connect):
    ws = connect()
    assert make_ha().statistics("sensor.nothing", "2025-10-01").startswith("No long-term statistics for sensor")
    assert ws.commands("recorder/statistics_during_period") == []
    assert make_ha().statistics("").startswith("Error: entity_id missing")


def test_statistics_no_data(connect):
    connect()
    out = json.loads(make_ha().statistics("sensor.wallbox_energy", "2025-10-01"))
    assert out["result"] == "No data in this range."
