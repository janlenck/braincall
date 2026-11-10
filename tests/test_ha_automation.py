"""ha_automation: the definition condensed, the conditions evaluated now, the last runs from the traces."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import ha as ha_mod
from automation import Explainer, seconds_of, time_pattern
from conftest import FakeHTTP, FakeResponse, make_cfg
from ha import HA, HAError

BERLIN = ZoneInfo("Europe/Berlin")

# Sample: hourly, only when it is cold outside; heats with PV surplus, otherwise stops
HEATING = {
    "id": "1700000000001",
    "alias": "Heizung PV",
    "mode": "single",
    "triggers": [{"trigger": "time_pattern", "hours": "/1", "minutes": 0}],
    "conditions": [{"condition": "numeric_state", "entity_id": "sensor.outdoor_temp", "below": 15}],
    "actions": [{"choose": [
        {"alias": "Überschuss",
         "conditions": [{"condition": "numeric_state", "entity_id": "sensor.pv_surplus", "above": 2}],
         "sequence": [{"action": "climate.turn_on", "target": {"entity_id": "climate.heating"}}]},
        {"alias": "kein Überschuss",
         "conditions": [{"condition": "numeric_state", "entity_id": "sensor.pv_surplus", "below": 2}],
         "sequence": [{"stop": "no surplus"}]},
    ]}],
}


def state(entity_id, value, **attrs):
    return {"entity_id": entity_id, "state": str(value), "attributes": attrs,
            "last_changed": "2025-10-05T10:00:00+00:00"}


def states(outdoor=12, surplus=0.8, automation_state="on", automation_id="1700000000001"):
    auto = {"friendly_name": "Heizungsautomation", "mode": "single", "current": 0,
            "last_triggered": "2025-10-05T12:00:00+00:00"}
    if automation_id:
        auto["id"] = automation_id
    return [
        state("automation.heating", automation_state, **auto),
        state("sensor.outdoor_temp", outdoor, friendly_name="Außentemperatur", unit_of_measurement="°C"),
        state("sensor.pv_surplus", surplus, friendly_name="PV-Überschuss", unit_of_measurement="kW"),
        state("climate.heating", "off", friendly_name="Heizung"),
        state("script.morning", "off", friendly_name="Morgenroutine", mode="single"),
    ]


# Two runs (Sunday 2025-10-05, summer time: 12:00 UTC = 14:00 in Berlin): the newer one with the branch
RUNS = [
    {"run_id": "r13", "timestamp": {"start": "2025-10-05T11:00:00.1+00:00", "finish": "2025-10-05T11:00:00.2+00:00"},
     "trigger": "time pattern", "state": "stopped", "script_execution": "failed_conditions",
     "domain": "automation", "item_id": "1700000000001"},
    {"run_id": "r14", "timestamp": {"start": "2025-10-05T12:00:00.1+00:00", "finish": "2025-10-05T12:00:00.3+00:00"},
     "trigger": "time pattern", "state": "stopped", "script_execution": "finished",
     "domain": "automation", "item_id": "1700000000001"},
]
TRACE_R14 = {
    "run_id": "r14", "config": HEATING, "script_execution": "finished",
    "trace": {
        "trigger/0": [{"path": "trigger/0", "changed_variables": {"trigger": {"platform": "time_pattern"}}}],
        "condition/0": [{"path": "condition/0", "result": {"result": True}}],
        "condition/0/entity_id/0": [{"path": "condition/0/entity_id/0",
                                     "result": {"result": True, "state": 12.0, "wanted_state_below": 15.0}}],
        "action/0": [{"path": "action/0", "result": {"choice": 1}}],
        "action/0/choose/0/conditions/0": [{"path": "action/0/choose/0/conditions/0", "result": {"result": False}}],
        "action/0/choose/0/conditions/0/entity_id/0": [{"result": {"result": False, "state": 0.8}}],
        "action/0/choose/1/conditions/0": [{"path": "action/0/choose/1/conditions/0", "result": {"result": True}}],
        "action/0/choose/1/conditions/0/entity_id/0": [{"result": {"result": True, "state": 0.8}}],
        "action/0/choose/1/sequence/0": [{"path": "action/0/choose/1/sequence/0", "result": {"stop": "no surplus"}}],
    },
}


class FakeWS:
    """A websocket-client connection that answers like Home Assistant and records what was sent."""

    def __init__(self, answers):
        self.answers = answers  # type -> result, callable(msg) or HAError
        self.sent = []
        self.inbox = [{"type": "auth_required"}]

    def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        if msg["type"] == "auth":
            self.inbox.append({"type": "auth_ok"})
            return
        answer = self.answers.get(msg["type"], HAError("Unknown command.", "unknown_command"))
        if isinstance(answer, HAError):
            self.inbox.append({"id": msg["id"], "type": "result", "success": False,
                               "error": {"code": answer.code, "message": str(answer)}})
        else:
            result = answer(msg) if callable(answer) else answer
            self.inbox.append({"id": msg["id"], "type": "result", "success": True, "result": result})

    def recv(self):
        return json.dumps(self.inbox.pop(0))

    def close(self):
        pass

    def commands(self, type_):
        return [m for m in self.sent if m["type"] == type_]


@pytest.fixture
def make_ha(monkeypatch):
    def make(answers=None, routes=None, current=None, ws_down=False):
        h = HA(make_cfg(ha_url="https://ha:8123", ha_token="token", timezone="Europe/Berlin"))
        current = current if current is not None else states()
        table = {("GET", "/states"): FakeResponse(current)}
        for s in current:
            table[("GET", f"/states/{s['entity_id']}")] = FakeResponse(s)
        table.update(routes or {})
        h.http = FakeHTTP(table)
        base = {"automation/config": {"config": HEATING}, "trace/list": RUNS, "trace/get": TRACE_R14}
        ws = FakeWS({**base, **(answers or {})})

        def connect(url, **kw):
            if ws_down:
                raise ConnectionRefusedError("refused")
            return ws
        monkeypatch.setattr(ha_mod.websocket, "create_connection", connect)
        h.ws = ws
        return h
    return make


def explain(h, entity_id="automation.heating", **kw) -> dict:
    return json.loads(h.automation(entity_id, **kw))


# ---------------------------------------------------------------- the whole tool
def test_what_it_does(make_ha):
    out = explain(make_ha())
    assert out["name"] == "Heizungsautomation" and out["enabled"] is True and out["mode"] == "single"
    assert out["triggers"] == ["every hour at :00"]
    assert out["conditions"] == ["Außentemperatur below 15 °C"]
    assert out["actions"] == ["'Überschuss': if PV-Überschuss above 2 kW → climate.turn_on Heizung",
                              "'kein Überschuss': if PV-Überschuss below 2 kW → stop (no surplus)"]


def test_criteria_now(make_ha):
    now = explain(make_ha())["now"]
    assert now["conditions"] == ["Außentemperatur below 15 °C: now 12 °C → yes"]
    assert now["conditions_met"] == "yes"
    assert now["branches"] == ["'Überschuss' → no", "  PV-Überschuss above 2 kW: now 0.8 kW → no",
                               "'kein Überschuss' → yes", "  PV-Überschuss below 2 kW: now 0.8 kW → yes"]
    assert now["if_triggered_now"] == "'kein Überschuss' → stop (no surplus)"


def test_criteria_now_with_surplus(make_ha):
    now = explain(make_ha(current=states(surplus=2.5)))["now"]
    assert now["if_triggered_now"] == "'Überschuss' → climate.turn_on Heizung"


def test_criteria_not_met(make_ha):
    now = explain(make_ha(current=states(outdoor=18)))["now"]
    assert now["conditions"] == ["Außentemperatur below 15 °C: now 18 °C → no"]
    assert now["if_triggered_now"] == "No: the conditions are not met, no action would run."
    assert "branches" not in now


def test_turned_off_automation(make_ha):
    out = explain(make_ha(current=states(automation_state="off")))
    assert out["enabled"] is False
    assert out["now"]["if_triggered_now"].startswith("The automation is turned off")


def test_last_run_and_earlier_runs(make_ha):
    h = make_ha()
    out = explain(h)
    run = out["last_run"]
    assert run["time"] == "Sun 2025-10-05 14:00"
    assert run["triggered_by"] == "time pattern" and run["result"] == "ran completely"
    assert run["conditions"] == ["Außentemperatur below 15 °C: passed (then: 12.0)"]
    assert run["steps"] == ["choose: 'kein Überschuss'",
                            "'Überschuss': PV-Überschuss above 2 kW: failed (then: 0.8)",
                            "'kein Überschuss': PV-Überschuss below 2 kW: passed (then: 0.8)",
                            "stop (no surplus)"]
    assert out["earlier_runs"] == ["Sun 2025-10-05 13:00: time pattern: triggered, but the conditions were not met: "
                                   "no action ran"]
    assert out["last_triggered"] == "Sun 2025-10-05 14:00"
    assert h.ws.commands("automation/config") == [{"id": 1, "type": "automation/config",
                                                   "entity_id": "automation.heating"}]
    assert h.ws.commands("trace/list")[0]["item_id"] == "1700000000001"
    assert h.ws.commands("trace/list")[0]["domain"] == "automation"
    assert h.ws.commands("trace/get")[0]["run_id"] == "r14"  # the newest, whatever order HA lists them in


def test_why_not(make_ha):
    trace = {"run_id": "r13", "config": HEATING, "trace": {
        "trigger/0": [{"path": "trigger/0"}],
        "condition/0": [{"path": "condition/0", "result": {"result": False}}],
        "condition/0/entity_id/0": [{"path": "condition/0/entity_id/0",
                                     "result": {"result": False, "state": 16.5, "wanted_state_below": 15.0}}],
    }}
    runs = [dict(RUNS[0], timestamp={"start": "2025-10-05T13:00:00+00:00"})]
    run = explain(make_ha({"trace/list": runs, "trace/get": trace}))["last_run"]
    assert run["result"] == "triggered, but the conditions were not met: no action ran"
    assert run["conditions"] == ["Außentemperatur below 15 °C: failed (then: 16.5)"]
    assert "steps" not in run


def test_no_choice_and_errors_in_run(make_ha):
    trace = {"config": HEATING, "trace": {"action/0": [{"path": "action/0", "result": {}}]}}
    runs = [dict(RUNS[1], script_execution="error", error="Climate unavailable")]
    run = explain(make_ha({"trace/list": runs, "trace/get": trace}))["last_run"]
    assert run["steps"] == ["choose: no option matched → nothing"]
    assert run["result"] == "stopped with an error" and run["error"] == "Climate unavailable"


def test_never_triggered(make_ha):
    current = states()
    current[0]["attributes"]["last_triggered"] = None
    assert explain(make_ha(current=current))["last_triggered"] == "never"


def test_no_runs_recorded(make_ha):
    h = make_ha({"trace/list": []})
    out = explain(h)
    assert out["last_run"].startswith("No runs recorded")
    assert not h.ws.commands("trace/get")


def test_evaluate_false_skips_the_evaluation(make_ha):
    out = explain(make_ha(), evaluate=False)
    assert "now" not in out and "triggers" in out


def test_config_api_fallback(make_ha):
    h = make_ha({"automation/config": HAError("Unknown command.", "unknown_command")},
                routes={("GET", "/config/automation/config/"): FakeResponse(HEATING)})
    out = explain(h)
    assert out["triggers"] == ["every hour at :00"]
    assert any("/config/automation/config/1700000000001" in url for _, url, _ in h.http.calls)


def test_trace_config_as_last_resort(make_ha):
    h = make_ha({"automation/config": HAError("Unknown command.", "unknown_command")},
                routes={("GET", "/config/automation/config/"): FakeResponse({"message": "not found"}, 404)})
    out = explain(h)
    assert out["conditions"] == ["Außentemperatur below 15 °C"]
    assert "notes" not in out


def test_no_admin_rights(make_ha):
    denied = HAError("Unauthorized", "unauthorized")
    h = make_ha({"automation/config": denied, "trace/list": denied},
                routes={("GET", "/config/automation/config/"): FakeResponse({}, 401)})
    out = explain(h)
    assert out["definition"] == "not available."
    assert any("administrator" in n and n.startswith("Definition") for n in out["notes"])
    assert any("administrator" in n and n.startswith("Runs") for n in out["notes"])


def test_yaml_automation_without_id(make_ha):
    h = make_ha(current=states(automation_id=""))
    out = explain(h)
    assert out["triggers"] == ["every hour at :00"]  # automation/config works without an id
    assert "last_run" not in out
    assert any("without id" in n for n in out["notes"])
    assert not h.ws.commands("trace/list")


def test_websocket_down(make_ha):
    h = make_ha(ws_down=True, routes={("GET", "/config/automation/config/"): FakeResponse(HEATING)})
    out = explain(h)
    assert out["triggers"] == ["every hour at :00"]
    assert any("WebSocket failed" in n for n in out["notes"])


def test_script(make_ha):
    script = {"alias": "Morgenroutine", "sequence": [
        {"action": "light.turn_on", "target": {"entity_id": "climate.heating"}, "data": {"brightness_pct": 50}},
        {"delay": "00:10:00"}]}
    h = make_ha({"script/config": {"config": script}, "trace/list": []})
    out = explain(h, "script.morning")
    assert out["running"] is False
    assert "triggers" not in out
    assert out["actions"] == ["light.turn_on Heizung (brightness_pct=50)", "wait 10 min"]
    assert out["now"]["if_triggered_now"] == "light.turn_on Heizung (brightness_pct=50); wait 10 min"
    assert h.ws.commands("trace/list")[0] == {"id": 2, "type": "trace/list", "domain": "script",
                                              "item_id": "morning"}


def test_wrong_and_unknown_entities(make_ha):
    h = make_ha(routes={("GET", "/states/automation.nope"): FakeResponse({}, 404)})
    assert h.automation("light.kitchen").startswith("Error: entity_id must be an automation")
    assert h.automation("automation.nope").startswith("Unknown entity automation.nope")


def test_template_condition_is_rendered(make_ha):
    config = {"triggers": [{"trigger": "homeassistant", "event": "start"}],
              "conditions": ["{{ states('sensor.outdoor_temp') | float < 15 }}"], "actions": []}
    h = make_ha({"automation/config": {"config": config}},
                routes={("POST", "/template"): FakeResponse("True")})
    h.http.routes[("POST", "/template")].text = "True"
    now = explain(h)["now"]
    assert now["conditions"] == ["template {{ states('sensor.outdoor_temp') | float < 15 }}: now True → yes"]
    assert h.http.posted("/template") == [{"template": "{{ states('sensor.outdoor_temp') | float < 15 }}"}]


def test_template_error_is_unknown(make_ha):
    config = {"triggers": [], "conditions": [{"condition": "template", "value_template": "{{ x.y }}"}],
              "actions": [{"action": "light.turn_on", "target": {"entity_id": "climate.heating"}}]}
    h = make_ha({"automation/config": {"config": config}},
                routes={("POST", "/template"): FakeResponse({"message": "UndefinedError"}, 400)})
    now = explain(h)["now"]
    assert now["conditions_met"] == "unknown"
    assert now["if_triggered_now"].startswith("Only if the conditions marked unknown are met:")


# ---------------------------------------------------------------- pieces
@pytest.mark.parametrize("args, expected", [
    (("/1", 0, None), "every hour at :00"),
    (("/2", 30, None), "every 2 hours at :30"),
    ((None, 15, None), "every hour at :15"),
    (("6", "30", None), "daily at 06:30"),
    ((None, "/5", None), "every 5 minutes"),
    ((None, None, "/10"), "every 10 seconds"),
    (("8", "/15", None), "every 15 minutes between 08:00 and 08:59"),
    (("6", "30", "15"), "time pattern (hours 6, minutes 30, seconds 15)"),
])
def test_time_pattern(args, expected):
    assert time_pattern(*args) == expected


@pytest.mark.parametrize("value, seconds", [
    ("00:10:00", 600), ("01:30", 5400), ({"minutes": 5}, 300), (90, 90), ("-00:30:00", -1800),
    ("{{ x }}", None), ({"minutes": "{{ x }}"}, None),
])
def test_seconds_of(value, seconds):
    assert seconds_of(value) == seconds


def ex(at: str, *extra, sun=None):
    """Explainer at a local time in Berlin with the sample states (plus extra state dicts)."""
    current = {s["entity_id"]: s for s in states() + list(extra)}
    if sun:
        current["sun.sun"] = state("sun.sun", "below_horizon", **sun)
    return Explainer(current, BERLIN, now=datetime.fromisoformat(at).replace(tzinfo=BERLIN))


def test_describe_triggers_and_actions():
    e = ex("2025-10-05T12:00")
    assert e.trigger({"platform": "state", "entity_id": ["sensor.pv_surplus"], "to": "on", "for": "00:05:00",
                      "id": "pv"}) == "PV-Überschuss changes to on for 5 min [id pv]"
    assert e.trigger({"trigger": "numeric_state", "entity_id": "sensor.outdoor_temp", "below": 5}) \
        == "Außentemperatur goes below 5 °C"
    assert e.trigger({"trigger": "sun", "event": "sunset", "offset": "-00:30:00"}) == "at sunset -30 min"
    assert e.trigger({"trigger": "time", "at": "06:30:00", "weekday": ["mon", "tue"]}) == "at 06:30 on Mon, Tue"
    assert e.condition({"or": [{"condition": "state", "entity_id": "climate.heating", "state": ["heat", "auto"]},
                               {"condition": "time", "after": "22:00", "before": "06:00"}]}) \
        == "(Heizung is heat or auto or between 22:00 and 06:00)"
    assert e.action({"if": [{"condition": "trigger", "id": "pv"}], "then": [{"scene": "scene.x"}],
                     "else": [{"delay": {"minutes": 2}}]}) \
        == "if triggered by id pv → scene scene.x; otherwise → wait 2 min"
    assert e.action({"service": "notify.phone", "data": {"message": "Hi"}}) == "notify.phone (message=Hi)"


def test_old_syntax_definition():
    old = {"trigger": {"platform": "time_pattern", "minutes": "/10"},
           "condition": [{"condition": "numeric_state", "entity_id": "sensor.outdoor_temp", "above": 0,
                          "below": "sensor.pv_surplus"}],
           "action": [{"service": "climate.turn_off", "entity_id": "climate.heating"}]}
    d = ex("2025-10-05T12:00").definition(old)
    assert d == {"triggers": ["every 10 minutes"],
                 "conditions": ["Außentemperatur above 0 °C and below PV-Überschuss"],
                 "actions": ["climate.turn_off Heizung"]}


def test_blueprint_without_expanded_config():
    d = ex("2025-10-05T12:00").definition({"use_blueprint": {"path": "motion_light.yaml",
                                                              "input": {"motion": "binary_sensor.x"}}})
    assert d == {"blueprint": "motion_light.yaml with motion=binary_sensor.x"}


def test_description_is_complete():
    text = "Startet die Klimaanlage des R5 zur Zeit aus input_datetime.start_timer.\n" + "Details. " * 300
    d = ex("2025-10-05T12:00").definition({"description": f"  {text}  ", "triggers": [], "actions": []})
    assert d["description"] == text.strip()
    d = ex("2025-10-05T12:00").definition({"description": text, "use_blueprint": {"path": "timer.yaml"}})
    assert d == {"description": text.strip(), "blueprint": "timer.yaml"}
    assert "description" not in ex("2025-10-05T12:00").definition({"description": " ", "actions": []})


@pytest.mark.parametrize("at, ok", [("2025-10-05T23:00", True), ("2025-10-05T05:59", True),
                                    ("2025-10-05T12:00", False)])
def test_time_condition_across_midnight(at, ok):
    r, lines = ex(at).check({"condition": "time", "after": "22:00", "before": "06:00"})
    assert r is ok


def test_time_condition_weekday():
    r, lines = ex("2025-10-05T12:00").check({"condition": "time", "weekday": ["mon", "tue"]})  # a Sunday
    assert r is False and lines == ["on Mon, Tue: now Sun 12:00 → no"]


def test_state_condition_for():
    e = ex("2025-10-05T12:30")  # climate.heating off since 10:00 UTC = 12:00 local
    assert e.check({"condition": "state", "entity_id": "climate.heating", "state": "off", "for": "00:20:00"})[0]
    r, lines = e.check({"condition": "state", "entity_id": "climate.heating", "state": "off", "for": {"hours": 1}})
    assert r is False and lines == ["Heizung is off for 1 h 00 min: now off for 30 min → no"]


def test_sun_condition_night():
    sun = {"next_rising": "2025-10-06T05:20:00+00:00", "next_setting": "2025-10-05T16:50:00+00:00"}
    night = {"condition": "sun", "after": "sunset", "before": "sunrise"}
    assert ex("2025-10-05T12:00", sun=sun).check(night)[0] is False
    assert ex("2025-10-05T19:00", sun=sun).check(night)[0] is True
    assert ex("2025-10-05T12:00", sun=sun).check({"condition": "sun", "after": "sunrise"})[0] is True
    assert ex("2025-10-05T12:00").check(night)[0] is None  # no sun.sun


def test_logic_with_unknowns():
    e = ex("2025-10-05T12:00")
    unknown = {"condition": "trigger", "id": "x"}
    no = {"condition": "state", "entity_id": "climate.heating", "state": "heat"}
    yes = {"condition": "state", "entity_id": "climate.heating", "state": "off"}
    assert e.check({"condition": "and", "conditions": [unknown, no]})[0] is False
    assert e.check({"condition": "and", "conditions": [unknown, yes]})[0] is None
    assert e.check({"or": [unknown, yes]})[0] is True
    assert e.check({"not": [no]})[0] is True
    r, lines = e.check({"condition": "or", "conditions": [unknown, no]})
    assert r is None and lines[0] == "any of → unknown"
    assert lines[1] == "  triggered by id x: depends on which trigger fired → unknown"


def test_choose_with_unknown_option_before():
    e = ex("2025-10-05T12:00")
    choose = {"choose": [{"conditions": [{"condition": "trigger", "id": "manual"}], "sequence": [{"stop": ""}]},
                         {"conditions": [], "sequence": [{"action": "climate.turn_on",
                                                          "target": {"entity_id": "climate.heating"}}]}]}
    out = e.evaluate({"actions": [choose]})
    assert out["if_triggered_now"] == ("option 2 (unless option 1 applies: unknown now) → "
                                       "climate.turn_on Heizung")


def test_numeric_state_unavailable_and_limit_entity():
    e = ex("2025-10-05T12:00", state("sensor.dead", "unavailable", friendly_name="Tot"))
    assert e.check({"condition": "numeric_state", "entity_id": "sensor.dead", "above": 1}) \
        == (False, ["Tot above 1: now unavailable → no"])
    r, lines = e.check({"condition": "numeric_state", "entity_id": "sensor.outdoor_temp", "above": "sensor.pv_surplus"})
    assert r is True and lines == ["Außentemperatur above PV-Überschuss: now 12 °C, PV-Überschuss now 0.8 → yes"]


def test_run_today_shows_only_the_time():
    e = ex("2025-10-05T16:00")
    assert e.run_line({"timestamp": {"start": "2025-10-05T12:00:00+00:00"}, "state": "running"}) \
        == "14:00: still running"


# ---------------------------------------------------------------- review findings
@pytest.mark.parametrize("rendered, ok", [("True", True), ("true", True), (" TRUE ", True), ("1", False),
                                          ("yes", False), ("on", False), ("False", False)])
def test_template_counts_only_true(rendered, ok):
    e = Explainer({}, BERLIN, render=lambda t: rendered)
    assert e.check("{{ x }}")[0] is ok


def test_sun_estimate_close_to_the_event_is_unknown():
    # sunset already past today: sun.sun only has tomorrow's (16:48 UTC), today's is estimated at 18:48 local
    sun = {"next_rising": "2025-10-06T05:20:00+00:00", "next_setting": "2025-10-06T16:48:00+00:00"}
    after_sunset = {"condition": "sun", "after": "sunset"}
    assert ex("2025-10-05T18:55", sun=sun).check(after_sunset)[0] is None
    r, lines = ex("2025-10-05T20:00", sun=sun).check(after_sunset)
    assert r is True and "sunset about 18:48" in lines[0]


def test_sun_estimate_across_dst_change():
    # summer time ends in the night to Sunday 2025-10-26: tomorrow 07:02 CET = 06:02 UTC, today about 08:00 CEST
    sun = {"next_rising": "2025-10-26T06:02:00+00:00", "next_setting": "2025-10-25T16:20:00+00:00"}
    r, lines = ex("2025-10-25T07:30", sun=sun).check({"condition": "sun", "before": "sunrise"})
    assert r is True and "sunrise about 08:02" in lines[0]  # not 07:02, which would make it "after sunrise"


def test_zone_by_position():
    home = state("zone.home", "1", friendly_name="Zuhause", latitude=52.52, longitude=13.405, radius=100)
    work = state("zone.work", "0", friendly_name="Büro", latitude=52.0, longitude=13.0, radius=200, passive=True)
    near = state("person.jan", "not_home", friendly_name="Jan", latitude=52.0012, longitude=13.0,
                 gps_accuracy=20)  # 133 m from the passive zone's centre
    named = state("person.anna", "Büro", friendly_name="Anna")  # no position: in no zone for Home Assistant
    e = ex("2025-10-05T12:00", home, work, near, named)
    assert e.check({"condition": "zone", "entity_id": "person.jan", "zone": "zone.work"})[0] is True
    assert e.check({"condition": "zone", "entity_id": "person.jan", "zone": "zone.home"})[0] is False
    r, lines = e.check({"condition": "zone", "entity_id": "person.anna", "zone": "zone.work"})
    assert r is False and lines == ["Anna in Büro: Anna has no position (state Büro) → no"]


def test_actions_in_order_and_stop_in_a_branch():
    e = ex("2025-10-05T12:00")
    on = {"action": "climate.turn_on", "target": {"entity_id": "climate.heating"}}
    config = {"actions": [
        {"action": "notify.phone", "data": {"message": "check"}},
        {"choose": [{"conditions": [{"condition": "state", "entity_id": "climate.heating", "state": "off"}],
                     "sequence": [{"delay": 5}, {"stop": "already off"}]}]},
        {"if": [{"condition": "state", "entity_id": "climate.heating", "state": "off"}], "then": [on]},
    ]}
    assert e.evaluate(config)["if_triggered_now"] == ("notify.phone (message=check); "
                                                      "option 1 → wait 5 s, stop (already off)")
    config["actions"][1]["choose"][0]["sequence"] = [{"delay": 5}]
    assert e.evaluate(config)["if_triggered_now"] == ("notify.phone (message=check); option 1 → wait 5 s; "
                                                      "if yes → climate.turn_on Heizung")


def test_condition_step_inside_a_branch_stops_the_rest():
    e = ex("2025-10-05T12:00")
    config = {"actions": [
        {"if": [{"condition": "state", "entity_id": "climate.heating", "state": "off"}],
         "then": [{"condition": "numeric_state", "entity_id": "sensor.pv_surplus", "above": 2},
                  {"action": "climate.turn_on", "target": {"entity_id": "climate.heating"}}]},
        {"action": "notify.phone"}]}
    assert e.evaluate(config)["if_triggered_now"] == "if yes → stops at 'PV-Überschuss above 2 kW': not met"


def test_run_lists_only_the_steps_reached():
    config = {"actions": [
        {"if": [{"condition": "state", "entity_id": "climate.heating", "state": "off"}],
         "then": [{"condition": "numeric_state", "entity_id": "sensor.pv_surplus", "above": 2},
                  {"action": "climate.turn_on", "target": {"entity_id": "climate.heating"}}]}]}
    trace = {"trace": {
        "action/0": [{"timestamp": "2025-10-05T12:00:00.10+00:00", "result": {"choice": "then"}}],
        "action/0/if/condition/0": [{"timestamp": "2025-10-05T12:00:00.11+00:00", "result": {"result": True}}],
        "action/0/then/0": [{"timestamp": "2025-10-05T12:00:00.12+00:00", "result": {"result": False}}],
        "action/0/then/0/entity_id/0": [{"result": {"result": False, "state": 0.8}}],
    }}
    run = ex("2025-10-05T16:00").run({"script_execution": "aborted"}, trace, config)
    assert run["steps"] == ["if: yes", "if: Heizung is off: passed",
                            "PV-Überschuss above 2 kW: not met, stopped here"]
    assert run["result"] == "stopped by a condition or stop step in the actions"


@pytest.mark.parametrize("mode, current, maximum, expected", [
    ("single", 1, None, "It is running right now (mode single): a new trigger is ignored until that run ends."),
    ("restart", 1, None, "It is running right now (mode restart)"),
    ("queued", 1, 10, "It is running right now (mode queued)"),
    ("parallel", 3, 3, "3 runs are going on (mode parallel, at most 3): a new trigger is ignored."),
    ("parallel", 1, 3, ""),
    ("single", 0, None, ""),
])
def test_runs_going_on(mode, current, maximum, expected):
    out = ex("2025-10-05T12:00").evaluate({"actions": [{"delay": 1}]}, mode=mode, current=current,
                                          max_runs=maximum)
    assert out["if_triggered_now"].startswith(expected)
    assert out["if_triggered_now"].endswith("wait 1 s")


def test_running_single_automation(make_ha):
    current = states()
    current[0]["attributes"]["current"] = 1
    assert explain(make_ha(current=current))["now"]["if_triggered_now"].startswith("It is running right now")


def test_evaluation_reads_fresh_states(make_ha):
    h = make_ha()
    h._states()  # e.g. ha_search a moment ago
    h.http.routes[("GET", "/states")] = FakeResponse(states(surplus=3))
    assert explain(h)["now"]["if_triggered_now"] == "'Überschuss' → climate.turn_on Heizung"
