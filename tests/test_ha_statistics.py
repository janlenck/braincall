"""Long-term statistics over the WebSocket API: energy dashboard totals and statistics of single sensors."""
import json
import math
import os
import ssl
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import ha as ha_mod
from conftest import make_cfg
from ha import HA, HAError, buckets, stat_range

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


def energy_meta(name=""):
    return {"unit_class": "energy", "statistics_unit_of_measurement": "Wh", "display_unit_of_measurement": "kWh",
            "has_sum": True, "mean_type": 0, **({"name": name} if name else {})}


META = {sid: energy_meta() for sid in ("sensor.grid_import_t1", "sensor.grid_import_t2", "sensor.grid_export",
                                        "sensor.pv_energy", "sensor.bat_out", "sensor.bat_in", "sensor.wp_energy")}
META["sensor.wallbox_energy"] = energy_meta("Wallbox")
META["sensor.outdoor_temp"] = {"unit_class": "temperature", "statistics_unit_of_measurement": "°C",
                               "display_unit_of_measurement": "°F", "has_sum": False, "mean_type": 1,
                               "name": "Außentemperatur"}
META["sensor.wind_dir"] = {"unit_class": None, "statistics_unit_of_measurement": "°",
                           "display_unit_of_measurement": "°", "has_sum": False, "mean_type": 2}

# kWh per day; each day is spread evenly over its 24 hours. 2025-10-03 lies outside the ranges the tests ask for
DAILY = {
    "sensor.grid_import_t1": {"2025-10-01": 3.0, "2025-10-02": 2.0, "2025-10-03": 50.0},
    "sensor.grid_import_t2": {"2025-10-01": 1.0, "2025-10-02": 0.5, "2025-10-03": 50.0},
    "sensor.grid_export": {"2025-10-01": 4.0, "2025-10-02": 6.0, "2025-10-03": 50.0},
    "sensor.pv_energy": {"2025-10-01": 10.0, "2025-10-02": 14.0, "2025-10-03": 50.0},
    "sensor.bat_out": {"2025-10-01": 2.0, "2025-10-02": 1.0, "2025-10-03": 50.0},
    "sensor.bat_in": {"2025-10-01": 2.5, "2025-10-02": 3.0, "2025-10-03": 50.0},
    "sensor.wp_energy": {"2025-10-01": 4.8, "2025-10-02": 3.6, "2025-10-03": 50.0},
}


BERLIN = ZoneInfo("Europe/Berlin")  # timezone of Home Assistant and of the configuration (default)


def local(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=BERLIN)


def berlin_midnight(t: datetime) -> datetime:
    return datetime.combine(t.astimezone(BERLIN).date(), datetime.min.time(), tzinfo=BERLIN)


def hourly_changes(per_day: dict) -> list:
    return [(local(day) + timedelta(hours=h), v / 24) for day, v in per_day.items() for h in range(24)]


class FakeRecorder:
    """Answers the recorder commands like Home Assistant, from hourly rows:
    statistic_during_period over exactly [start, end), statistics_during_period with HA's alignment of the
    range to whole days (end_time is stretched to the end of its day)."""

    def __init__(self, changes=None, means=None):
        self.changes = changes if changes is not None else {sid: hourly_changes(d) for sid, d in DAILY.items()}
        self.means = means or {}  # sid -> [(hour start, mean, min, max)]

    @staticmethod
    def _t(text):
        return datetime.fromisoformat(text).astimezone(timezone.utc)

    def metadata(self, msg):
        return [{"statistic_id": i, **META[i]} for i in msg["statistic_ids"] if i in META]

    def during(self, msg):
        assert msg["units"] == {"energy": "kWh", "volume": "m³"}
        sid, b, e = msg["statistic_id"], self._t(msg["fixed_period"]["start_time"]), \
            self._t(msg["fixed_period"]["end_time"])
        if sid in self.changes:
            hours = [v for t, v in self.changes[sid] if b <= t < e]
            return {"change": sum(hours) if hours else None}
        if sid in self.means:
            hours = [r for r in self.means[sid] if b <= r[0] < e]
            if not hours:
                return {"mean": None, "min": None, "max": None}
            if META[sid]["mean_type"] == 2:
                x = sum(math.cos(math.radians(r[1])) for r in hours)
                y = sum(math.sin(math.radians(r[1])) for r in hours)
                mean = math.degrees(math.atan2(y, x)) % 360
            else:
                mean = sum(r[1] for r in hours) / len(hours)
            return {"mean": mean, "min": min(r[2] for r in hours), "max": max(r[3] for r in hours)}
        return {}

    def rows(self, msg):
        assert msg["types"] == ["change"] and msg["units"] == {"energy": "kWh", "volume": "m³"}
        b, e, period = self._t(msg["start_time"]), self._t(msg["end_time"]), msg["period"]
        if period == "day":  # like HA: align to whole days in its timezone, end_time to the end of its day
            b = berlin_midnight(b)
            e = berlin_midnight(e) + timedelta(days=1)
        out = {}
        for sid in msg["statistic_ids"]:
            groups = {}
            for t, v in self.changes.get(sid, []):
                if b <= t < e:
                    key = t if period == "hour" else berlin_midnight(t)
                    groups[key] = groups.get(key, 0.0) + v
            if groups:
                out[sid] = [{"start": k.timestamp() * 1000, "change": v} for k, v in sorted(groups.items())]
        return out


class FakeWS:
    """Stands in for a websocket-client connection: answers like Home Assistant, records what was sent."""

    def __init__(self, answers, auth_ok=True):
        self.answers = answers  # type -> result, callable(msg) or HAError to answer with an error
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
        self.inbox.append({"id": msg["id"] + 1000, "type": "event", "event": {}})  # unrelated message first
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


@pytest.fixture
def connect(monkeypatch):
    """connect(answers) -> FakeWS that HA._ws() will get; records the URL and options of the connection."""

    def make(answers=None, auth_ok=True, recorder=None, prefs=PREFS):
        rec = recorder or FakeRecorder()
        base = {"energy/get_prefs": prefs, "recorder/get_statistics_metadata": rec.metadata,
                "recorder/statistic_during_period": rec.during, "recorder/statistics_during_period": rec.rows}
        ws = FakeWS({**base, **(answers or {})}, auth_ok)
        seen = {}

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


def test_buckets_are_clipped_to_the_range():
    b, e = stat_range("2025-10-15", "2025-11-05")
    months = buckets(b, e, "month")
    assert [(x.date().isoformat(), y.date().isoformat()) for x, y in months] == \
        [("2025-10-15", "2025-11-01"), ("2025-11-01", "2025-11-06")]
    weeks = buckets(*stat_range("2025-10-01", "2025-10-14"), "week")  # Wednesday to Tuesday
    assert [x.date().isoformat() for x, _ in weeks] == ["2025-10-01", "2025-10-06", "2025-10-13"]


def test_buckets_hours_across_dst_change():
    hours = buckets(*stat_range("2025-10-26"), "hour")  # end of summer time: 25 hours
    assert len(hours) == 25
    assert hours[-1][1].isoformat() == "2025-10-27T00:00:00+01:00"


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
    assert out["devices"] == {"Wärmepumpe": "8.4 kWh", "Wallbox": "no data"}
    assert out["no_data"] == ["sensor.wallbox_energy"]
    assert "rows" not in out and "note" not in out
    q = ws.commands("recorder/statistic_during_period")
    assert len(q) == len(META) - 2  # one exact query per meter of the dashboard
    assert q[0]["fixed_period"] == {"start_time": "2025-10-01T00:00:00+02:00",
                                    "end_time": "2025-10-03T00:00:00+02:00"}
    assert q[0]["types"] == ["change"]
    assert ws.commands("recorder/statistics_during_period") == []  # no rows asked for


def test_energy_rows_per_day_stay_in_the_range(connect):
    """HA stretches end_time to the end of its day: the next midnight would add the whole next day."""
    ws = connect()
    out = json.loads(make_ha().energy("2025-10-01", "2025-10-02", "day"))
    assert out["rows"] == [
        {"time": "Wed 2025-10-01", "grid_import": 4.0, "grid_export": 4.0, "solar": 10.0,
         "battery_discharge": 2.0, "battery_charge": 2.5},
        {"time": "Thu 2025-10-02", "grid_import": 2.5, "grid_export": 6.0, "solar": 14.0,
         "battery_discharge": 1.0, "battery_charge": 3.0},
    ]
    q = ws.commands("recorder/statistics_during_period")[0]
    assert q["period"] == "day" and q["end_time"] == "2025-10-02T23:59:59+02:00"
    assert out["totals"]["solar"] == "24.0 kWh"


def test_energy_rows_per_month_do_not_change_the_totals(connect):
    """A month breakdown of a single day must not read the whole month."""
    connect()
    day = json.loads(make_ha().energy("2025-10-02"))
    connect()
    month = json.loads(make_ha().energy("2025-10-02", period="month"))
    assert month["totals"] == day["totals"]
    assert month["rows"] == [{"time": "2025-10", "grid_import": 2.5, "grid_export": 6.0, "solar": 14.0,
                              "battery_discharge": 1.0, "battery_charge": 3.0}]


def test_energy_rows_per_hour(connect):
    ws = connect()
    out = json.loads(make_ha().energy("2025-10-02", period="hour"))
    assert len(out["rows"]) == 24
    assert out["rows"][0]["time"] == "Thu 2025-10-02 00:00" and out["rows"][0]["solar"] == round(14 / 24, 2)
    q = ws.commands("recorder/statistics_during_period")[0]
    assert q["period"] == "hour" and q["end_time"] == "2025-10-03T00:00:00+02:00"


def test_energy_too_many_rows_are_not_fetched(connect):
    ws = connect()
    out = json.loads(make_ha().energy("2025-10-01", "2025-10-03", "hour"))
    assert out["rows"].startswith("72 rows, too many")
    assert ws.commands("recorder/statistics_during_period") == []
    assert out["totals"]["solar"] == "74.0 kWh"


def test_energy_without_grid_meter_has_no_home_consumption(connect):
    connect(prefs={"energy_sources": [{"type": "solar", "stat_energy_from": "sensor.pv_energy"}]})
    out = json.loads(make_ha().energy("2025-10-01"))
    assert out["totals"] == {"solar": "10.0 kWh"}


def test_energy_note_for_rows_up_to_now(connect):
    connect()
    out = json.loads(make_ha().energy(period="day"))
    assert out["note"].startswith("The rows are compiled hourly")
    connect()
    assert "note" not in json.loads(make_ha().energy())


def test_energy_gas_in_different_units_is_not_added_up(connect):
    META["sensor.gas_m3"] = {"unit_class": "volume", "statistics_unit_of_measurement": "L", "has_sum": True}
    META["sensor.gas_kwh"] = energy_meta()
    rec = FakeRecorder({"sensor.gas_m3": hourly_changes({"2025-10-01": 2.4}),
                        "sensor.gas_kwh": hourly_changes({"2025-10-01": 12.0})})
    try:
        connect(recorder=rec, prefs={"energy_sources": [{"type": "gas", "stat_energy_from": "sensor.gas_m3"},
                                                        {"type": "gas", "stat_energy_from": "sensor.gas_kwh"}]})
        out = json.loads(make_ha().energy("2025-10-01"))
    finally:
        del META["sensor.gas_m3"], META["sensor.gas_kwh"]
    assert out["totals"] == {"gas": ["2.4 m³", "12.0 kWh"]}


def test_energy_devices_with_the_same_name(connect):
    prefs = {"energy_sources": [], "device_consumption": [{"stat_consumption": "sensor.wp_energy", "name": "Keller"}],
             "device_consumption_water": [{"stat_consumption": "sensor.pv_energy", "name": "Keller"}]}
    connect(prefs=prefs)
    out = json.loads(make_ha().energy("2025-10-01"))
    assert out["devices"] == {"Keller": "4.8 kWh", "Keller (sensor.pv_energy)": "10.0 kWh"}


def test_energy_long_range_per_month(connect):
    ws = connect()
    out = json.loads(make_ha().energy("2025", period="month"))
    assert [r["time"] for r in out["rows"]] == ["2025-10"]  # months without data are left out
    assert ws.commands("recorder/statistics_during_period")[0]["period"] == "day"


def test_energy_dashboard_not_set_up(connect):
    connect({"energy/get_prefs": HAError("No prefs", "not_found")})
    assert make_ha().energy("2025-10-01").startswith("The energy dashboard is not set up")
    connect(prefs={"energy_sources": [], "device_consumption": []})
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


def test_energy_rows_with_iso_start_times():
    """Older Home Assistant versions send the start of a row as an ISO string."""
    spans = buckets(*stat_range("2025-10-01"), "day")
    stats = {"sensor.pv": [{"start": "2025-09-30T22:00:00+00:00", "change": 7.0},
                           {"start": "2025-10-01T22:00:00+00:00", "change": 9.0}]}  # outside: dropped
    assert HA._energy_rows({"sensor.pv": "solar"}, stats, spans, "day") == [{"time": "Wed 2025-10-01", "solar": 7.0}]


def test_energy_missing_electricity_meter_gives_no_derived_figures(connect):
    """An unknown grid import is not zero: no home consumption or self-sufficiency from it."""
    rec = FakeRecorder()
    del rec.changes["sensor.grid_import_t2"]
    connect(recorder=rec)
    out = json.loads(make_ha().energy("2025-10-01"))
    assert out["no_data"] == ["sensor.grid_import_t2", "sensor.wallbox_energy"]
    assert "home_consumption" not in out["totals"] and "self_sufficiency" not in out["totals"]
    assert "solar_used_in_house_approx" not in out["totals"]
    assert out["totals"]["grid_import"] == "3.0 kWh"


def test_energy_gas_rows_in_different_units_are_not_added_up(connect):
    META["sensor.gas_m3"] = {"unit_class": "volume", "statistics_unit_of_measurement": "L", "has_sum": True}
    META["sensor.gas_kwh"] = energy_meta()
    rec = FakeRecorder({"sensor.gas_m3": hourly_changes({"2025-10-01": 2.4}),
                        "sensor.gas_kwh": hourly_changes({"2025-10-01": 12.0}),
                        "sensor.pv_energy": hourly_changes({"2025-10-01": 10.0})})
    try:
        connect(recorder=rec, prefs={"energy_sources": [{"type": "gas", "stat_energy_from": "sensor.gas_m3"},
                                                        {"type": "gas", "stat_energy_from": "sensor.gas_kwh"},
                                                        {"type": "solar", "stat_energy_from": "sensor.pv_energy"}]})
        out = json.loads(make_ha().energy("2025-10-01", period="day"))
    finally:
        del META["sensor.gas_m3"], META["sensor.gas_kwh"]
    assert out["rows"] == [{"time": "Wed 2025-10-01", "gas (m³)": 2.4, "gas (kWh)": 12.0, "solar": 10.0}]


@pytest.fixture
def utc_machine():
    """The machine runs in UTC while BrainCall and Home Assistant are configured for Europe/Berlin."""
    if not hasattr(time, "tzset"):
        pytest.skip("needs time.tzset")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield  # the autouse fixture in conftest.py restores the timezone afterwards


def test_energy_uses_the_configured_timezone(connect, utc_machine):
    ws = connect()
    out = json.loads(make_ha(timezone="Europe/Berlin").energy("2025-10-01", "2025-10-02", "day"))
    assert ws.commands("recorder/statistic_during_period")[0]["fixed_period"] == {
        "start_time": "2025-10-01T00:00:00+02:00", "end_time": "2025-10-03T00:00:00+02:00"}
    q = ws.commands("recorder/statistics_during_period")[0]
    assert q["start_time"] == "2025-10-01T00:00:00+02:00" and q["end_time"] == "2025-10-02T23:59:59+02:00"
    assert [r["time"] for r in out["rows"]] == ["Wed 2025-10-01", "Thu 2025-10-02"]
    assert out["totals"]["solar"] == "24.0 kWh"


def test_statistics_rows_use_the_configured_timezone(connect, utc_machine):
    day = datetime(2025, 10, 25, 22, tzinfo=timezone.utc)  # 2025-10-26 00:00 in Berlin, 25 hours long
    ws = connect(recorder=FakeRecorder({"sensor.wp_energy": [(day + timedelta(hours=h), 1.0) for h in range(26)]}))
    out = json.loads(make_ha(timezone="Europe/Berlin").statistics("sensor.wp_energy", "2025-10-26", period="hour"))
    assert out["change"] == 25.0
    assert len(out["rows"]) == 25  # end of summer time in Berlin, not in UTC
    assert out["rows"][2]["time"] == "Sun 2025-10-26 02:00" and out["rows"][3]["time"] == "Sun 2025-10-26 02:00"
    assert ws.commands("recorder/statistic_during_period")[1]["fixed_period"]["start_time"] == \
        "2025-10-26T00:00:00+02:00"


def test_invalid_timezone_falls_back_to_the_machine():
    assert make_ha(timezone="Mars/Olympus").tz is None
    assert make_ha(timezone="Europe/Berlin").tz == BERLIN


# ---------------------------------------------------------------- single sensors
def test_statistics_meter(connect):
    ws = connect()
    out = json.loads(make_ha().statistics("sensor.wp_energy", "2025-10-01", "2025-10-02", "day"))
    assert out["change"] == 8.4 and out["unit"] == "kWh"
    assert out["rows"] == [{"time": "Wed 2025-10-01", "change": 4.8}, {"time": "Thu 2025-10-02", "change": 3.6}]
    assert {m["types"][0] for m in ws.commands("recorder/statistic_during_period")} == {"change"}
    assert len(ws.commands("recorder/statistic_during_period")) == 3  # total + one per row


def test_statistics_measurement_weights_by_recorded_time(connect):
    """A full day at 10 °F and one recorded hour at 34 °F: the mean is close to 11, not 22."""
    start = local("2025-10-01")
    rows = [(start + timedelta(hours=h), 10.0, 5.0, 15.0) for h in range(24)]
    rows.append((start + timedelta(days=1), 34.0, 33.0, 35.0))
    ws = connect(recorder=FakeRecorder(changes={}, means={"sensor.outdoor_temp": rows}))
    out = json.loads(make_ha().statistics("sensor.outdoor_temp", "2025-10-01", "2025-10-02", "day"))
    assert out["unit"] == "°F"  # HA returns the values in the unit of the entity
    assert out["mean"] == 11.0 and out["min"] == 5.0 and out["max"] == 35.0
    assert out["rows"] == [{"time": "Wed 2025-10-01", "mean": 10.0, "min": 5.0, "max": 15.0},
                           {"time": "Thu 2025-10-02", "mean": 34.0, "min": 33.0, "max": 35.0}]
    assert ws.commands("recorder/statistic_during_period")[0]["types"] == ["mean", "min", "max"]


def test_statistics_circular_mean_comes_from_home_assistant(connect):
    start = local("2025-10-01")
    rows = [(start, 359.0, 359.0, 359.0), (start + timedelta(hours=1), 1.0, 1.0, 1.0)]
    connect(recorder=FakeRecorder(changes={}, means={"sensor.wind_dir": rows}))
    out = json.loads(make_ha().statistics("sensor.wind_dir", "2025-10-01"))
    assert out["mean"] in (0.0, 360.0)


def test_statistics_unknown_sensor(connect):
    ws = connect()
    assert make_ha().statistics("sensor.nothing", "2025-10-01").startswith("No long-term statistics for sensor")
    assert ws.commands("recorder/statistic_during_period") == []
    assert make_ha().statistics("").startswith("Error: entity_id missing")


def test_statistics_no_data(connect):
    connect()
    out = json.loads(make_ha().statistics("sensor.wallbox_energy", "2025-10-01"))
    assert out["result"] == "No data in this range."


def test_statistics_too_many_rows(connect):
    ws = connect()
    out = json.loads(make_ha().statistics("sensor.wp_energy", "2025-10-01", "2025-10-03", "hour"))
    assert out["rows"].startswith("72 rows, too many") and out["change"] == 58.4
    assert len(ws.commands("recorder/statistic_during_period")) == 1
