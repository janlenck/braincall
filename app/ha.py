"""Home Assistant access via the REST API (inside an add-on via the Supervisor proxy).
Long-term statistics (energy dashboard) only exist in the WebSocket API, so those use a short WebSocket session."""
import contextlib
import itertools
import json
import logging
import ssl
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import websocket

log = logging.getLogger("ha")

# Spoken German search terms -> typical (often English) parts of entity IDs, e.g. ebusd/Vaillant, Fronius, evcc.
# The keys are German on purpose: they match what callers say.
SYNONYMS = {
    "warmwasser": ["hwc", "dhw", "hot_water", "hotwater", "water_heater", "storage", "speicher", "tank"],
    "temperatur": ["temp", "°c", "temperature"],
    "aussen": ["outside", "outdoor", "aussen", "außen"],
    "außen": ["outside", "outdoor", "aussen"],
    "vorlauf": ["flow", "vorlauf"],
    "ruecklauf": ["return", "rücklauf"],
    "rücklauf": ["return", "ruecklauf"],
    "heizung": ["heat", "hmu", "ctlv", "heating", "wp", "waermepumpe"],
    "wärmepumpe": ["heat_pump", "heatpump", "hmu", "ebusd", "vaillant", "arotherm"],
    "waermepumpe": ["heat_pump", "heatpump", "hmu", "ebusd", "vaillant", "arotherm"],
    "pv": ["solar", "photovoltaic", "fronius", "pv"],
    "solar": ["pv", "fronius", "photovoltaic"],
    "leistung": ["power", "kw"],
    "verbrauch": ["consumption", "load", "energy", "kwh"],
    "energie": ["energy", "kwh"],
    "batterie": ["battery", "akku", "soc"],
    "akku": ["battery", "soc"],
    "netz": ["grid", "netz"],
    "einspeisung": ["grid_export", "export", "feed"],
    "bezug": ["grid_import", "import"],
    "auto": ["evcc", "car", "vehicle", "charge", "wallbox"],
    "wallbox": ["evcc", "charge", "wallbox", "loadpoint"],
    "garage": ["garage", "cover"],
    "kalender": ["calendar."],
    "termin": ["calendar."],
    "feiertag": ["feiertag", "holiday"],
    "einkauf": ["shopping", "einkauf", "todo."],
    "liste": ["todo.", "list"],
    "aufgabe": ["todo.", "task"],
    "tor": ["cover", "door", "gate"],
    "licht": ["light", "lamp"],
    "lampe": ["light", "lamp"],
    "feuchtigkeit": ["humidity"],
    "zimmer": ["room"],
}

# Service data keys that would widen a call beyond the one checked entity (e.g. area_id = whole room)
TARGET_KEYS = ("entity_id", "area_id", "device_id", "floor_id", "label_id")

# Useful attributes per domain for ha_state: short answers instead of complete attribute dumps
ATTRS = {
    "climate": ("current_temperature", "temperature", "target_temp_low", "target_temp_high", "hvac_action",
                "hvac_modes", "preset_mode", "current_humidity", "humidity", "fan_mode"),
    "water_heater": ("current_temperature", "temperature", "operation_mode", "away_mode"),
    "cover": ("current_position", "current_tilt_position"),
    "light": ("brightness_pct", "color_temp_kelvin", "color_mode", "effect"),
    "fan": ("percentage", "preset_mode", "oscillating", "direction"),
    "humidifier": ("current_humidity", "humidity", "mode", "action"),
    "media_player": ("media_title", "media_artist", "media_album_name", "media_series_title", "app_name",
                     "volume_level", "is_volume_muted", "source", "source_list"),
    "vacuum": ("battery_level", "status"),
    "weather": ("temperature", "apparent_temperature", "temperature_unit", "humidity", "dew_point",
                "wind_speed", "wind_gust_speed", "wind_speed_unit", "wind_bearing", "pressure", "pressure_unit",
                "cloud_coverage", "uv_index", "visibility", "visibility_unit", "precipitation_unit"),
}
# The one or two most important of them for ha_search / ha_list
BRIEF_ATTRS = {
    "climate": ("current_temperature", "temperature"),
    "water_heater": ("current_temperature", "temperature"),
    "cover": ("current_position",),
    "light": ("brightness_pct",),
    "media_player": ("media_title", "volume_level"),
    "weather": ("temperature",),
}
# Forecast fields worth reading out, and how many entries per forecast type
FORECAST_KEYS = ("condition", "temperature", "templow", "precipitation", "precipitation_probability",
                 "wind_speed", "wind_gust_speed", "humidity", "uv_index", "is_daytime")
FORECAST_LIMIT = {"daily": 7, "hourly": 24, "twice_daily": 14}

WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Long-term statistics: grouping periods, the most rows read out per period, the longest range in days
STAT_PERIODS = ("hour", "day", "week", "month")
STAT_ROWS = {"hour": 48, "day": 31, "week": 26, "month": 24}
STAT_MAX_DAYS = 1100
WS_TIMEOUT = 20
# Units the statistics are requested in, so meters of one kind add up (energy in kWh, gas/water volume in m³)
STAT_UNITS = {"energy": "kWh", "volume": "m³"}
# Energy dashboard: order and names of the totals
ENERGY_KEYS = ("grid_import", "grid_export", "solar", "battery_charge", "battery_discharge", "gas", "water")


class HAError(Exception):
    """Error answer of the WebSocket API; code e.g. 'not_found'."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def _wd(d) -> str:
    return WEEKDAY[d.weekday()]


def fold(t: str) -> str:
    """Fold umlauts the way entity IDs do: Büro -> buro."""
    return t.lower().replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")


def _local_time(iso: str) -> str:
    """UTC timestamp from HA -> local time plus age, so the model never reads out UTC."""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        mins = int((datetime.now(timezone.utc) - t).total_seconds() // 60)
        local = t.astimezone()
        when = local.strftime("%H:%M") if mins < 1440 else local.strftime("%Y-%m-%d %H:%M")
        return f"{when} local time ({mins} minutes ago)" if mins < 180 else f"{when} local time"
    except Exception:  # noqa: BLE001
        return iso


def _num(v: float) -> float:
    """Rounded for the phone: 2 decimals below 10, otherwise 1."""
    return round(v, 2) if abs(v) < 10 else round(v, 1)


def _span_of(text: str):
    """'2026-10-05' -> that day, '2026-10' -> that month, '2026' -> that year: (first day, last day)."""
    t = (text or "").strip()
    if len(t) == 4 and t.isdigit():
        return date(int(t), 1, 1), date(int(t), 12, 31)
    if len(t) == 7 and t[4] == "-":
        first = date(int(t[:4]), int(t[5:]), 1)
        return first, (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    d = datetime.fromisoformat(t).date()
    return d, d


def _midnight(d: date, tz=None) -> datetime:
    """Midnight of a day in tz (None = timezone of the machine)."""
    t = datetime.combine(d, datetime.min.time())
    return t.replace(tzinfo=tz) if tz else t.astimezone()


def stat_range(start: str = "", end: str = "", tz=None):
    """Midnight of the first day up to the midnight after the last day (end inclusive), in tz.
    Without start: today. Without end: the whole day, month or year given as start."""
    today = datetime.now(tz).astimezone(tz).date()
    first, last = _span_of(start) if start else (today, today)
    if end:
        last = _span_of(end)[1]
    if last < first:
        first, last = last, first
    if (last - first).days >= STAT_MAX_DAYS:
        raise ValueError(f"Range too long, at most {STAT_MAX_DAYS} days.")
    return _midnight(first, tz), _midnight(last + timedelta(days=1), tz)


def _row_time(v, tz=None) -> datetime:
    """Start of a statistics row: milliseconds since 1970 (newer HA) or an ISO string (older HA)."""
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, timezone.utc).astimezone(tz)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(tz)


def _row_label(t: datetime, period: str) -> str:
    if period == "hour":
        return f"{_wd(t)} " + t.strftime("%Y-%m-%d %H:%M")
    if period == "week":
        return "week from " + t.strftime("%Y-%m-%d")
    if period == "month":
        return t.strftime("%Y-%m")
    return f"{_wd(t)} " + t.strftime("%Y-%m-%d")


def _unit(meta: dict) -> str:
    """Unit of the returned values: the requested one (STAT_UNITS), otherwise HA converts into the unit of the
    entity (display_unit_of_measurement)."""
    if meta.get("unit_class") in STAT_UNITS:
        return STAT_UNITS[meta["unit_class"]]
    return meta.get("display_unit_of_measurement") or meta.get("statistics_unit_of_measurement") or ""


def _next_start(t: datetime, period: str, tz=None) -> datetime:
    """Start of the following hour, day, week (Monday) or month in tz, correct across DST changes."""
    if period == "hour":
        return (t.astimezone(timezone.utc) + timedelta(hours=1)).astimezone(tz)
    d = t.date()
    if period == "week":
        d += timedelta(days=7 - d.weekday())
    elif period == "month":
        d = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    else:
        d += timedelta(days=1)
    return _midnight(d, tz)


def buckets(begin: datetime, stop: datetime, period: str, tz=None) -> list:
    """[(start, end)] per period within [begin, stop) in tz; the first and last one may be partial."""
    out, t = [], begin
    while t < stop:
        n = min(_next_start(t, period, tz), stop)
        out.append((t, n))
        t = n
    return out


def _has_mean(meta: dict) -> bool:
    mt = meta.get("mean_type")  # newer HA: 0 none, 1 arithmetic, 2 circular; older: has_mean
    return bool(meta.get("has_mean")) or (mt not in (None, 0, "none"))


class HA:
    def __init__(self, cfg):
        self.cfg = cfg
        self.verify = True
        if cfg.supervisor_token:
            self.base = "http://supervisor/core/api"
            self.ws_url = "ws://supervisor/core/websocket"
            token = cfg.supervisor_token
        elif cfg.ha_url and cfg.ha_token:
            self.base = cfg.ha_url.rstrip("/") + "/api"
            self.ws_url = "ws" + self.base.removeprefix("http") + "/websocket"  # http -> ws, https -> wss
            token = cfg.ha_token
            v = cfg.ha_verify_ssl
            self.verify = v if isinstance(v, str) and v else bool(v)
        else:
            self.base = None
            self.ws_url = None
            token = ""
        self.token = token
        try:  # dates in statistics follow the configured timezone, like the current time in the prompt
            self.tz = ZoneInfo(cfg.timezone) if cfg.timezone else None
        except (ZoneInfoNotFoundError, ValueError):
            self.tz = None
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {token}"
        self._cache = (0.0, [])

    @property
    def enabled(self) -> bool:
        return self.base is not None

    def _get(self, path):
        r = self.http.get(self.base + path, timeout=10, verify=self.verify)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ websocket
    def _sslopt(self) -> dict:
        """TLS for wss:// like the REST calls: ha_verify_ssl true, false or the path to a CA file."""
        if not str(self.ws_url).startswith("wss:"):
            return {}
        if self.verify is False:
            return {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
        return {"context": ssl.create_default_context(cafile=self.verify if isinstance(self.verify, str) else None)}

    @contextlib.contextmanager
    def _ws(self):
        """Short WebSocket session: logs in, yields call(type, **data) -> result, closes afterwards."""
        if not self.ws_url:
            raise HAError("Home Assistant is not configured.")
        ws = websocket.create_connection(self.ws_url, timeout=WS_TIMEOUT, sslopt=self._sslopt())
        try:
            msg = json.loads(ws.recv())
            if msg.get("type") == "auth_required":
                ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                msg = json.loads(ws.recv())
            if msg.get("type") != "auth_ok":
                raise HAError(f"WebSocket login failed: {msg.get('message') or msg.get('type')}", "auth")
            ids = itertools.count(1)

            def call(type_: str, **data):
                n = next(ids)
                ws.send(json.dumps({"id": n, "type": type_, **data}))
                while True:
                    msg = json.loads(ws.recv())
                    if msg.get("id") == n and msg.get("type") == "result":
                        break
                if not msg.get("success"):
                    err = msg.get("error") or {}
                    raise HAError(err.get("message") or err.get("code") or "unknown error", err.get("code") or "")
                return msg.get("result")

            yield call
        finally:
            ws.close()

    @staticmethod
    def _during(call, sid: str, begin: datetime, stop: datetime, types: list) -> dict:
        """One value over exactly [begin, stop): HA combines hourly and 5-minute statistics, weights the mean by
        time and averages directions (circular mean) correctly."""
        return call("recorder/statistic_during_period", statistic_id=sid, types=types, units=STAT_UNITS,
                    fixed_period={"start_time": begin.isoformat(), "end_time": stop.isoformat()}) or {}

    @staticmethod
    def _rows_note(stop: datetime) -> str:
        if stop > datetime.now(timezone.utc):
            return "The rows are compiled hourly: the current hour is only in the totals."
        return ""

    @staticmethod
    def _energy_roles(prefs: dict) -> list:
        """[(role, statistic_id, name)] from the energy dashboard settings (energy/get_prefs)."""
        roles = []
        for src in prefs.get("energy_sources") or []:
            kind = src.get("type")
            if kind == "grid":
                roles += [("grid_import", f.get("stat_energy_from"), "") for f in src.get("flow_from") or []]
                roles += [("grid_export", f.get("stat_energy_to"), "") for f in src.get("flow_to") or []]
                roles += [("grid_import", src.get("stat_energy_from"), ""),  # newer HA: one entry per meter
                          ("grid_export", src.get("stat_energy_to"), "")]
            elif kind == "solar":
                roles.append(("solar", src.get("stat_energy_from"), ""))
            elif kind == "battery":
                roles += [("battery_discharge", src.get("stat_energy_from"), ""),
                          ("battery_charge", src.get("stat_energy_to"), "")]
            elif kind in ("gas", "water"):
                roles.append((kind, src.get("stat_energy_from"), ""))
        for key, role in (("device_consumption", "device"), ("device_consumption_water", "device_water")):
            roles += [(role, d.get("stat_consumption"), d.get("name") or "") for d in prefs.get(key) or []]
        return list(dict.fromkeys((r, sid, name) for r, sid, name in roles if sid))  # each meter counted once

    def energy(self, start: str = "", end: str = "", period: str = "") -> str:
        """Totals of the energy dashboard (grid, solar, battery, gas, water, devices) for a date range,
        optionally broken down per hour, day, week or month."""
        try:
            begin, stop = stat_range(start, end, self.tz)
        except ValueError as e:
            return f"Error: {e} Dates as YYYY-MM-DD, YYYY-MM or YYYY."
        period = period if period in STAT_PERIODS else ""
        spans = buckets(begin, stop, period, self.tz) if period else []
        with self._ws() as call:
            try:
                prefs = call("energy/get_prefs")
            except HAError as e:
                if e.code == "not_found":
                    return "The energy dashboard is not set up in Home Assistant (Settings > Dashboards > Energy)."
                raise
            roles = self._energy_roles(prefs or {})
            if not roles:
                return "The energy dashboard in Home Assistant has no sensors configured."
            ids = list(dict.fromkeys(sid for _, sid, _ in roles))
            meta = {m["statistic_id"]: m for m in call("recorder/get_statistics_metadata", statistic_ids=ids) or []}
            change = {sid: self._during(call, sid, begin, stop, ["change"]).get("change") for sid in ids}
            stats = {}
            if spans and len(spans) <= STAT_ROWS[period]:
                # Hourly rows, or daily rows added up per week/month. HA stretches end_time to the end of the
                # period it falls in, so for days the last included second is sent, not the next midnight.
                fetch = "hour" if period == "hour" else "day"
                last = stop if fetch == "hour" else stop - timedelta(seconds=1)
                stats = call("recorder/statistics_during_period", start_time=begin.isoformat(),
                             end_time=last.isoformat(), statistic_ids=ids, period=fetch, types=["change"],
                             units=STAT_UNITS) or {}

        totals, devices, missing = {}, {}, []
        for role, sid, name in roles:
            if change.get(sid) is None:
                missing.append(sid)
            value, unit = change.get(sid) or 0.0, _unit(meta.get(sid, {}))
            if role in ("device", "device_water"):
                label = name or meta.get(sid, {}).get("name") or sid
                if label in devices:  # two devices with the same name: keep both
                    label = f"{label} ({sid})"
                devices[label] = f"{_num(value)} {unit}".rstrip() if change.get(sid) is not None else "no data"
                continue
            by_unit = totals.setdefault(role, {})
            by_unit[unit] = by_unit.get(unit, 0.0) + value
        out = {"range": f"{begin:%Y-%m-%d} to {stop - timedelta(seconds=1):%Y-%m-%d}"}
        out["totals"] = {}
        for k in ENERGY_KEYS:
            if k in totals:  # gas in m³ and in kWh are not added up
                parts = [f"{_num(v)} {u}".rstrip() for u, v in totals[k].items()]
                out["totals"][k] = parts[0] if len(parts) == 1 else parts
        electric = {k: totals[k] for k in ENERGY_KEYS[:5] if k in totals}
        # Derived figures only when every electricity meter has data: a missing one is unknown, not zero
        complete = not any(r in ENERGY_KEYS[:5] and change.get(sid) is None for r, sid, _ in roles)
        if complete and "grid_import" in electric and all(set(v) == {"kWh"} for v in electric.values()):
            kwh = {k: v["kWh"] for k, v in electric.items()}
            imp, exp = kwh["grid_import"], kwh.get("grid_export", 0.0)
            solar, charge, discharge = (kwh.get(k, 0.0) for k in ("solar", "battery_charge", "battery_discharge"))
            home = imp + solar + discharge - exp - charge
            out["totals"]["home_consumption"] = f"{_num(home)} kWh"
            if "solar" in kwh:
                out["totals"]["solar_used_in_house_approx"] = f"{_num(max(0.0, solar - exp - charge))} kWh"
            if home > 0:
                out["totals"]["self_sufficiency"] = f"{round(max(0.0, 1 - imp / home) * 100)} %"
        if devices:
            out["devices"] = devices
        if spans:
            if len(spans) > STAT_ROWS[period]:
                out["rows"] = f"{len(spans)} rows, too many to read out: choose a longer period."
            else:
                # one column per role; a role with meters in different units (gas m³ / kWh) gets one per unit
                columns = {sid: role if len(totals[role]) == 1 else f"{role} ({_unit(meta.get(sid, {}))})"
                           for role, sid, _ in roles if role in totals}
                out["rows"] = self._energy_rows(columns, stats, spans, period, self.tz)
                note = self._rows_note(stop)
                if note:
                    out["note"] = note
        if missing:
            out["no_data"] = missing
        log.info("Energy %s, %s: %d statistics", out["range"], period or "total", len(ids))
        return json.dumps(out, ensure_ascii=False)

    @staticmethod
    def _energy_rows(columns: dict, stats: dict, spans: list, period: str, tz=None) -> list:
        """Per period one row with the sums per column ({statistic_id: column}, devices only in the totals).
        Rows outside the range are dropped."""
        sums = [{} for _ in spans]
        for sid, col in columns.items():
            for r in stats.get(sid) or []:
                t = _row_time(r.get("start"), tz)
                i = next((i for i, (b, e) in enumerate(spans) if b <= t < e), None)
                if i is not None:
                    sums[i][col] = sums[i].get(col, 0.0) + (r.get("change") or 0)
        return [{"time": _row_label(b, period), **{k: _num(v) for k, v in row.items()}}
                for (b, _), row in zip(spans, sums, strict=True) if row]

    def statistics(self, entity_id: str, start: str = "", end: str = "", period: str = "") -> str:
        """Long-term statistics of one sensor: change for meters (kWh, m³), mean/min/max for measurements."""
        entity_id = (entity_id or "").strip()
        if not entity_id:
            return "Error: entity_id missing."
        try:
            begin, stop = stat_range(start, end, self.tz)
        except ValueError as e:
            return f"Error: {e} Dates as YYYY-MM-DD, YYYY-MM or YYYY."
        period = period if period in STAT_PERIODS else ""
        spans = buckets(begin, stop, period, self.tz) if period else []
        with self._ws() as call:
            metas = call("recorder/get_statistics_metadata", statistic_ids=[entity_id]) or []
            if not metas:
                return (f"No long-term statistics for {entity_id}. Only sensors with a state_class have them; "
                        "check the entity_id with ha_search.")
            m = metas[0]
            meter = bool(m.get("has_sum"))
            if not meter and not _has_mean(m):
                return f"{entity_id} has statistics, but neither sums nor mean values."
            keys = ["change"] if meter else ["mean", "min", "max"]
            total = self._during(call, entity_id, begin, stop, keys)
            rows = []
            if spans and len(spans) <= STAT_ROWS[period]:  # one exact query per row, also for partial periods
                rows = [(b, self._during(call, entity_id, b, e, keys)) for b, e in spans]
        out = {"entity_id": entity_id, "name": m.get("name") or entity_id, "unit": _unit(m),
               "range": f"{begin:%Y-%m-%d} to {stop - timedelta(seconds=1):%Y-%m-%d}"}
        values = {k: _num(total[k]) for k in keys if total.get(k) is not None}
        if not values:
            out["result"] = "No data in this range."
            return json.dumps(out, ensure_ascii=False)
        out.update(values)
        if spans:
            out["rows"] = ([{"time": _row_label(b, period), **{k: _num(r[k]) for k in keys if r.get(k) is not None}}
                            for b, r in rows if any(r.get(k) is not None for k in keys)]
                           if len(spans) <= STAT_ROWS[period]
                           else f"{len(spans)} rows, too many to read out: choose a longer period.")
        log.info("Statistics %s %s, %s", entity_id, out["range"], period or "total")
        return json.dumps(out, ensure_ascii=False)

    # ------------------------------------------------------------------ states
    @staticmethod
    def _attrs(s: dict, table: dict) -> dict:
        """Allowlisted attributes of the entity's domain; light brightness 0–255 as brightness_pct."""
        a = dict(s.get("attributes", {}))
        if isinstance(a.get("brightness"), (int, float)):
            a["brightness_pct"] = round(a["brightness"] * 100 / 255)
        if isinstance(a.get("source_list"), list) and len(a["source_list"]) > 30:
            a["source_list"] = a["source_list"][:30] + ["…"]
        keys = table.get(s["entity_id"].split(".", 1)[0], ())
        return {k: a[k] for k in keys if a.get(k) not in (None, "", [])}

    @classmethod
    def _fmt(cls, s: dict, brief: bool = False) -> dict:
        a = s.get("attributes", {})
        out = {
            "entity_id": s["entity_id"],
            "name": a.get("friendly_name"),
            "state": s.get("state"),
            "unit": a.get("unit_of_measurement"),
            "changed": _local_time(s.get("last_changed", "")),
        }
        attrs = cls._attrs(s, BRIEF_ATTRS if brief else ATTRS)
        if attrs:
            out["attributes"] = attrs
        return out

    def todo_items(self, entity_id: str) -> list:
        """Items of a to-do/shopping list (the entity state itself only holds the count)."""
        r = self.http.post(f"{self.base}/services/todo/get_items?return_response",
                           json={"entity_id": entity_id}, timeout=10, verify=self.verify)
        r.raise_for_status()
        data = r.json().get("service_response", {}).get(entity_id, {})
        return [{"item": i.get("summary"),
                 "status": "done" if i.get("status") == "completed" else "open",
                 **({"note": i["description"]} if i.get("description") else {}),
                 **({"due": i["due"]} if i.get("due") else {})}
                for i in data.get("items", [])]

    def get_state(self, entity_id: str) -> str:
        try:
            st = self._fmt(self._get(f"/states/{entity_id}"))
            if entity_id.startswith("todo."):
                try:
                    st["items"] = self.todo_items(entity_id)
                except Exception as e:  # noqa: BLE001
                    st["items_error"] = str(e)
            return json.dumps(st, ensure_ascii=False)
        except requests.HTTPError as e:
            return f"Error: {e.response.status_code} for {entity_id}"

    def _states(self):
        ts, states = self._cache
        if time.time() - ts > 30:
            states = self._get("/states")
            self._cache = (time.time(), states)
        return states

    @staticmethod
    def _hay(s: dict) -> str:
        a = s.get("attributes", {})
        h = " ".join(str(x) for x in (s["entity_id"], a.get("friendly_name", ""),
                                       a.get("device_class", ""), a.get("unit_of_measurement", ""))).lower()
        return h + " " + fold(h)

    def search(self, term: str, limit: int = 30) -> str:
        """Fuzzy search: every word (plus synonyms) scores, best matches first."""
        groups = []
        for w in term.lower().replace("-", " ").split():
            alts = {w, fold(w)}
            for key, syn in SYNONYMS.items():
                if key in w or w in key:
                    alts.update(syn)
            groups.append(alts)
        scored = []
        for s in self._states():
            hay = self._hay(s)
            score = sum(1 for alts in groups if any(a in hay for a in alts))
            if score:
                scored.append((score, s["entity_id"].startswith("sensor."), s))
        scored.sort(key=lambda x: (-x[0], not x[1], x[2]["entity_id"]))
        hits = [self._fmt(s, brief=True) for _, _, s in scored[:limit]]
        log.info("Search '%s': %d hits", term, len(scored))
        if not hits:
            return f"No entity found for '{term}'. Try other or English terms, or use ha_list."
        return json.dumps(hits, ensure_ascii=False)

    def list_by(self, filter_: str, limit: int = 80) -> str:
        """All entities with a matching unit, device_class or domain (e.g. '°C', 'power', 'climate')."""
        f = filter_.lower().strip()
        hits = []
        for s in self._states():
            a = s.get("attributes", {})
            if (f == str(a.get("unit_of_measurement", "")).lower()
                    or f == str(a.get("device_class", "")).lower()
                    or s["entity_id"].startswith(f + ".")):
                brief = ", ".join(f"{k}={v}" for k, v in self._attrs(s, BRIEF_ATTRS).items())
                hits.append(f'{s["entity_id"]} | {a.get("friendly_name", "")} | '
                            f'{s.get("state")} {a.get("unit_of_measurement", "") or ""}'.rstrip()
                            + (f" | {brief}" if brief else ""))
        log.info("List '%s': %d hits", filter_, len(hits))
        if not hits:
            return (f"Nothing for '{filter_}'. Examples: °C, W, kW, kWh, %, temperature, power, energy, "
                    "battery, sensor, climate, switch.")
        extra = f"\n… and {len(hits) - limit} more" if len(hits) > limit else ""
        return "\n".join(hits[:limit]) + extra

    def forecast(self, entity_id: str = "", kind: str = "daily") -> str:
        """Weather forecast via weather.get_forecasts (daily, hourly or twice_daily)."""
        kind = kind if kind in FORECAST_LIMIT else "daily"
        weather = [s for s in self._states() if s["entity_id"].startswith("weather.")]
        entity_id = (entity_id or "").strip()
        if entity_id and not entity_id.startswith("weather."):
            return "Error: entity_id must be a weather.* entity."
        if not entity_id:
            if not weather:
                return "No weather entity (weather.*) found in Home Assistant."
            entity_id = weather[0]["entity_id"]
        r = self.http.post(f"{self.base}/services/weather/get_forecasts?return_response",
                           json={"entity_id": entity_id, "type": kind}, timeout=15, verify=self.verify)
        if r.status_code >= 400:
            return f"Error {r.status_code}: {r.text[:200]}"
        entries = r.json().get("service_response", {}).get(entity_id, {}).get("forecast") or []
        if not entries:
            return f"No {kind} forecast available for {entity_id}."
        state = next((s for s in weather if s["entity_id"] == entity_id), {})
        units = {k: v for k, v in state.get("attributes", {}).items()
                 if k in ("temperature_unit", "precipitation_unit", "wind_speed_unit") and v}
        out = []
        for e in entries[:FORECAST_LIMIT[kind]]:
            item = {k: e[k] for k in FORECAST_KEYS if e.get(k) is not None}
            try:
                t = datetime.fromisoformat(str(e.get("datetime", "")).replace("Z", "+00:00")).astimezone()
                item = {"time": f"{_wd(t)} " + t.strftime("%Y-%m-%d" if kind == "daily" else "%Y-%m-%d %H:%M"),
                        **item}
            except ValueError:
                item = {"time": e.get("datetime"), **item}
            out.append(item)
        log.info("Forecast %s %s: %d entries", entity_id, kind, len(out))
        return json.dumps({"entity_id": entity_id, "type": kind, **units, "forecast": out}, ensure_ascii=False)

    def fire_event(self, name: str, data: dict):
        if not self.enabled:
            return
        try:
            self.http.post(f"{self.base}/events/{name}", json=data, timeout=10, verify=self.verify)
        except Exception as e:  # noqa: BLE001
            log.warning("Event %s failed: %s", name, e)

    # ------------------------------------------------------------------ control
    def call_service(self, entity_id: str, service: str, data: dict | None = None,
                     confirmed: bool = False) -> str:
        entity_id = (entity_id or "").strip()
        if "." not in entity_id:
            return "Error: entity_id missing or invalid."
        domain = entity_id.split(".", 1)[0]
        allowed = [d.strip() for d in (self.cfg.control_domains or [])]
        if domain not in allowed:
            return (f"Not allowed: domain '{domain}' must not be controlled. "
                    f"Allowed: {', '.join(allowed) or 'nothing'}.")
        if domain in (self.cfg.confirm_domains or []) and not confirmed:
            return ("Safety check: explicitly ask the caller whether you should really do this, "
                    "and only call the tool again with confirmed=true after a clear yes.")
        service = service.strip().split(".")[-1]
        body = {k: v for k, v in (data or {}).items() if k not in TARGET_KEYS}
        body["entity_id"] = entity_id
        log.warning("CONTROL %s.%s %s", domain, service, body)
        r = self.http.post(f"{self.base}/services/{domain}/{service}", json=body,
                           timeout=15, verify=self.verify)
        if r.status_code >= 400:
            return f"Error {r.status_code}: {r.text[:200]}"
        time.sleep(1.0)
        self._cache = (0.0, [])
        return "Done. New state: " + self.get_state(entity_id)

    def notify(self, service: str, title: str, message: str):
        """Message via a notify entity (notify.send_message, e.g. notify.sm_g525f) or a legacy notify service
        (e.g. notify.mobile_app_phone, an SMTP notifier)."""
        name = service.strip().removeprefix("notify.")
        if not self.enabled or not name:
            return
        try:
            body = {"title": title, "message": message}
            if any(s["entity_id"] == f"notify.{name}" for s in self._states()):
                url, body = f"{self.base}/services/notify/send_message", {**body, "entity_id": f"notify.{name}"}
            else:
                url = f"{self.base}/services/notify/{name}"
            r = self.http.post(url, json=body, timeout=15, verify=self.verify)
            if r.status_code >= 400:
                log.warning("notify.%s failed: %s %s", name, r.status_code, r.text[:200])
                return
            log.info("Notified via notify.%s", name)
        except Exception as e:  # noqa: BLE001
            log.warning("notify.%s failed: %s", name, e)

    # ------------------------------------------------------------------ shopping lists
    def _shopping_list(self, wanted: str):
        """(entity_id, error) of an allowed shopping list; without a name the first one that exists."""
        allowed = [str(x).strip() for x in self.cfg.shopping_lists or [] if str(x).strip()]
        states = {s["entity_id"]: s for s in self._states() if s["entity_id"] in allowed}
        lists = [e for e in allowed if e in states]
        if not lists:
            return "", f"No shopping list available (shopping_lists: {', '.join(allowed) or 'empty'})."
        if not wanted:
            return lists[0], ""
        w = fold(wanted)
        for e in lists:
            if w == fold(e) or w in fold(e + " " + str(states[e].get("attributes", {}).get("friendly_name", ""))):
                return e, ""
        return "", f"Not allowed: only these lists may be used: {', '.join(lists)}."

    def shopping(self, wanted: str = "") -> str:
        entity_id, err = self._shopping_list(wanted)
        if err:
            return err
        return json.dumps({"list": entity_id, "items": self.todo_items(entity_id)}, ensure_ascii=False)

    def shopping_edit(self, action: str, item: str, wanted: str = "") -> str:
        entity_id, err = self._shopping_list(wanted)
        if err:
            return err
        services = {"add": ("add_item", {}), "check": ("update_item", {"status": "completed"}),
                    "uncheck": ("update_item", {"status": "needs_action"}), "remove": ("remove_item", {})}
        if action not in services or not str(item or "").strip():
            return "Error: action must be add, check, uncheck or remove, and item must be set."
        service, extra = services[action]
        body = {"entity_id": entity_id, "item": str(item).strip(), **extra}
        log.warning("SHOPPING todo.%s %s", service, body)
        r = self.http.post(f"{self.base}/services/todo/{service}", json=body, timeout=15, verify=self.verify)
        if r.status_code >= 400:
            return f"Error {r.status_code}: {r.text[:200]}"
        return "Done. " + self.shopping(entity_id)

    # ------------------------------------------------------------------ calendars
    def _calendars(self, wanted) -> list:
        cals = [s for s in self._states() if s["entity_id"].startswith("calendar.")]
        if not wanted:
            return cals
        out = []
        for w in wanted:
            w = fold(str(w))
            for c in cals:
                hay = fold(c["entity_id"] + " " + str(c.get("attributes", {}).get("friendly_name", "")))
                if (w == fold(c["entity_id"]) or w in hay) and c not in out:
                    out.append(c)
        return out

    def _fetch_events(self, cals: list, begin: datetime, end: datetime):
        """[(calendar name, event)] plus error lines for the given calendars and period."""
        q = f"?start={begin.isoformat()}&end={end.isoformat()}".replace("+", "%2B")
        found, errors = [], []
        for c in cals:
            name = c.get("attributes", {}).get("friendly_name") or c["entity_id"]
            try:
                found += [(name, ev) for ev in self._get(f"/calendars/{c['entity_id']}{q}")]
            except Exception as e:  # noqa: BLE001
                errors.append(f"[{name}] Error: {e}")
        return found, errors

    @staticmethod
    def _span(ev: dict):
        """(start, end, all_day) of an event in local time; the end of all-day events is exclusive."""
        st, en = ev.get("start", {}), ev.get("end", {})
        if "dateTime" in st:
            s = datetime.fromisoformat(st["dateTime"]).astimezone()
            e = datetime.fromisoformat(en["dateTime"]).astimezone() if "dateTime" in en else s + timedelta(hours=1)
            return s, e, False
        d = datetime.fromisoformat(st.get("date", "1970-01-01"))  # naive: whole days also across DST changes
        e = datetime.fromisoformat(en["date"]) if en.get("date") else d
        return d.astimezone(), max(e, d + timedelta(days=1)).astimezone(), True

    def _period(self, days, start: str):
        now = datetime.now().astimezone()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            begin = datetime.fromisoformat(start).astimezone() if start else midnight
        except ValueError:
            begin = midnight
        return begin, begin + timedelta(days=max(1, min(int(days or 7), 366)))

    def events(self, calendars=None, days: int = 7, start: str = "") -> str:
        """Appointments from HA calendars. calendars: entity_ids/name parts; empty = defaults from config."""
        wanted = calendars or self.cfg.default_calendars or []
        cals = self._calendars(wanted)
        if not cals:
            names = [c["entity_id"] for c in self._calendars([])]
            return f"No matching calendar. Available: {', '.join(names)}"
        begin, end = self._period(days, start)
        evs, errors = self._fetch_events(cals, begin, end)
        found = []
        for name, ev in evs:
            t, e, all_day = self._span(ev)
            if all_day:
                when = f"{_wd(t)} " + t.strftime("%Y-%m-%d") + " all-day"
            else:
                when = f"{_wd(t)} " + t.strftime("%Y-%m-%d %H:%M") + "–" + e.strftime("%H:%M")
            extra = f" ({ev['location']})" if ev.get("location") else ""
            found.append((t.isoformat(), f"{when}: {ev.get('summary', '?')}{extra}  [{name}]"))
        found.sort()
        log.info("Events %s, %d days: %d", [c["entity_id"] for c in cals], (end - begin).days, len(found))
        head = (f"Period {begin:%Y-%m-%d} to {end - timedelta(seconds=1):%Y-%m-%d}, calendars: "
                + ", ".join(c.get("attributes", {}).get("friendly_name") or c["entity_id"] for c in cals))
        body = "\n".join(e for _, e in found) or "No appointments in this period."
        return "\n".join([head, body, *errors])

    def _busy(self, begin: datetime, end: datetime):
        """Merged busy blocks [(start, end, all_day)] of the default calendars, without any details."""
        cals = self._calendars(self.cfg.default_calendars or [])
        evs, errors = self._fetch_events(cals, begin, end)
        # HA does not pass on Google's "free"/"busy", so all-day reminders (waste collection …) are marked by words
        free = [fold(str(w).strip()) for w in self.cfg.all_day_free_words or [] if str(w).strip()]
        spans = []
        for _, ev in evs:
            span = self._span(ev)
            if not (span[2] and any(w in fold(str(ev.get("summary", ""))) for w in free)):
                spans.append(span)
        spans.sort()
        merged = []
        for s, e, all_day in spans:
            if merged and s <= merged[-1][1] and all_day == merged[-1][2]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e, all_day])
        return [tuple(m) for m in merged if m[1] > begin and m[0] < end], errors

    def busy_times(self, days: int = 7, start: str = "") -> str:
        """Only when the owner is busy (default calendars), no titles, places or calendar names."""
        begin, end = self._period(days, start)
        blocks, errors = self._busy(begin, end)
        if errors:  # names and diagnostics stay in the log: this tool is for callers without read access
            log.warning("Busy times: %s", "; ".join(errors))
            return "The calendar cannot be read right now, availability is unknown. Do not book anything."
        lines = []
        for s, e, all_day in blocks:
            if all_day:
                last = e - timedelta(days=1)
                when = f"{_wd(s)} {s:%Y-%m-%d}" + (f" to {_wd(last)} {last:%Y-%m-%d}" if last.date() > s.date() else "")
                lines.append(f"{when}: all day busy")
            elif s.date() == e.date():
                lines.append(f"{_wd(s)} {s:%Y-%m-%d %H:%M}–{e:%H:%M}: busy")
            else:
                lines.append(f"{_wd(s)} {s:%Y-%m-%d %H:%M} to {_wd(e)} {e:%Y-%m-%d %H:%M}: busy")
        log.info("Busy times %d days: %d blocks", (end - begin).days, len(lines))
        head = (f"Period {begin:%Y-%m-%d} to {end - timedelta(seconds=1):%Y-%m-%d}. Only busy times, no details; "
                "all other times are free.")
        return "\n".join([head, "\n".join(lines) or "Nothing busy in this period."])

    def _booking_refusal(self, begin: datetime, stop: datetime, all_day: bool, booked: int) -> str:
        """Why a booking by a caller without admin is outside the limits; empty if it is within them."""
        now = datetime.now().astimezone()
        max_bookings = int(self.cfg.calendar_max_bookings or 0)
        max_hours = float(self.cfg.calendar_max_hours or 0)
        max_days = int(self.cfg.calendar_max_days_ahead or 0)
        if max_bookings and booked >= max_bookings:
            return (f"Not added: at most {max_bookings} appointment(s) can be booked per call. "
                    "Tell the caller that no further appointment can be added in this call.")
        if stop <= begin:
            return "End must be after start."
        if (begin.date() < now.date()) if all_day else (begin <= now):
            return "Not added: the start is in the past. Ask the caller for a time in the future."
        if all_day and (stop.date() - begin.date()).days > 1:
            return ("Not added: an all-day booking may cover only one day. "
                    "Ask the caller for a single day or a time slot.")
        if not all_day and max_hours and stop - begin > timedelta(hours=max_hours):
            return (f"Not added: a booking may last at most {max_hours:g} hours. "
                    "Ask the caller for a shorter appointment.")
        if max_days and begin > now + timedelta(days=max_days):
            return (f"Not added: bookings are possible at most {max_days} days ahead. "
                    "Ask the caller for an earlier date.")
        return ""

    def create_event(self, calendar: str, title: str, start: str, end: str = "",
                     location: str = "", description: str = "", confirmed: bool = False,
                     restricted: bool = False, booked: int = 0) -> str:
        """Create an appointment via calendar.create_event (only calendars whose integration supports it).
        restricted (callers without admin): only the default calendar, only into a free slot and within the
        limits calendar_max_hours / calendar_max_days_ahead / calendar_max_bookings (booked = already booked
        in this call)."""
        if restricted:
            calendar = ""
        cals = self._calendars([calendar] if calendar else (self.cfg.default_calendars or []))
        writable = [c for c in cals if int(c.get("attributes", {}).get("supported_features", 0)) & 1]
        if not writable and restricted:
            log.warning("Restricted booking: default calendar %s is read-only",
                        [c["entity_id"] for c in cals] or "missing")
            return "Booking is not possible right now. Tell the caller that the appointment could not be added."
        if not writable:
            names = ", ".join(c.get("attributes", {}).get("friendly_name") or c["entity_id"] for c in cals) or calendar
            all_writable = [c["entity_id"] for c in self._calendars([])
                            if int(c.get("attributes", {}).get("supported_features", 0)) & 1]
            return (f"Calendar '{names}' cannot create appointments (read-only). "
                    f"Writable calendars: {', '.join(all_writable) or 'none'}.")
        cal = writable[0]
        body = {"entity_id": cal["entity_id"], "summary": title}
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        try:
            if "T" in start:
                st = datetime.fromisoformat(start)
                st = st if st.tzinfo else st.astimezone()
                en = datetime.fromisoformat(end) if end and "T" in end else st + timedelta(hours=1)
                en = en if en.tzinfo else en.astimezone()
                body["start_date_time"] = st.isoformat()
                body["end_date_time"] = en.isoformat()
                begin, stop, all_day = st, en, False
            else:
                d = datetime.fromisoformat(start).date()
                e = datetime.fromisoformat(end).date() + timedelta(days=1) if end else d + timedelta(days=1)
                body["start_date"] = d.isoformat()
                body["end_date"] = e.isoformat()  # HA: end date is exclusive
                begin = datetime.fromisoformat(body["start_date"]).astimezone()
                stop = datetime.fromisoformat(body["end_date"]).astimezone()
                all_day = True
        except ValueError as e:
            return f"Date not readable ({e}). Format: 2026-10-30T14:00, or 2026-10-30 for all-day."
        # Restricted callers: checked before asking, so the caller is not asked to confirm what is refused anyway
        if restricted:
            refusal = self._booking_refusal(begin, stop, all_day, booked)
            if refusal:
                log.warning("Restricted booking refused (%s – %s): %s", begin, stop, refusal)
                return refusal
            blocks, errors = self._busy(begin, stop)
            if errors:
                log.warning("Restricted booking: %s", "; ".join(errors))
                return "The calendar cannot be read right now, appointment NOT added."
            if blocks:
                return ("Not added: that time is already busy. Check ha_busy_times and suggest a free time "
                        "to the caller.")
        if not confirmed:
            return ("Read the appointment (calendar, title, day, time) back to the caller and ask whether to add it. "
                    "Only call again with confirmed=true after a yes.")
        log.warning("EVENT %s", body)
        r = self.http.post(f"{self.base}/services/calendar/create_event", json=body,
                           timeout=15, verify=self.verify)
        if r.status_code >= 400:
            log.warning("EVENT failed: %s %s", r.status_code, r.text[:200])
            return "Appointment NOT added (error)." if restricted else f"Error {r.status_code}: {r.text[:200]}"
        name = cal.get("attributes", {}).get("friendly_name") or cal["entity_id"]
        return f"Added: {title}." if restricted else f"Added to {name}: {title}."
