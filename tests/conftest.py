"""Shared helpers for the tests. app/ is on sys.path via pyproject.toml (tool.pytest.ini_options)."""
import json
import os
import time

import pytest

import pyVoIP.VoIP  # noqa: F401  must be imported before pyVoIP.SIP (circular import in pyVoIP 1.6.8)

import config


@pytest.fixture(autouse=True)
def berlin_time():
    """ha.py works in the local timezone of the machine: make it the same everywhere."""
    if not hasattr(time, "tzset"):  # Windows: tests assume the machine runs in Europe/Berlin
        yield
        return
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.fixture(autouse=True)
def clean_warnings():
    config.WARNINGS.clear()
    yield
    config.WARNINGS.clear()


def make_cfg(**overrides) -> config.Config:
    """Configuration with the defaults plus overrides, callers already parsed like config.load() does."""
    cfg = config.Config(json.loads(json.dumps(config.DEFAULTS)))
    cfg["supervisor_token"] = ""
    cfg.update(overrides)
    cfg["callers"] = config._callers(cfg.callers)
    return cfg


class FakeResponse:
    def __init__(self, data=None, status=200, content=b""):
        self.data = data
        self.status_code = status
        self.content = content
        self.text = json.dumps(data) if data is not None else ""

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}", response=self)


class FakeHTTP:
    """Stands in for requests.Session: answers from a {(method, path-prefix): response} table, records calls."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []
        self.headers = {}

    def _answer(self, method, url, **kw):
        self.calls.append((method, url, kw))
        best = None
        for (m, prefix), resp in self.routes.items():
            if m == method and prefix in url and (best is None or len(prefix) > len(best[0])):
                best = (prefix, resp)
        if best is None:
            return FakeResponse({}, 200)
        resp = best[1]
        return resp(url, kw) if callable(resp) else resp

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)

    def post(self, url, **kw):
        return self._answer("POST", url, **kw)

    def posted(self, path_part):
        return [kw.get("json") for m, url, kw in self.calls if m == "POST" and path_part in url]


@pytest.fixture
def tmp_env(monkeypatch):
    """No secrets or config paths from the developer's environment leak into config.load()."""
    for key in ("BRAINCALL_CONFIG", "CONFIG_PATH", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HA_TOKEN",
                "BRAINCALL_SIP_PASSWORD", "BRAINCALL_API_TOKEN", "BRAINCALL_LOG_DIR", "SUPERVISOR_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    return os.environ
