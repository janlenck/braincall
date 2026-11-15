"""discovery.py: the message to the Supervisor that lets Brain Assist connect without a token typed (#50)."""
import logging

import requests

import discovery
from conftest import make_cfg


class Resp:
    def __init__(self, status: int, text: str = ""):
        self.status_code = status
        self.text = text


def fake_post(monkeypatch, *answers):
    """requests.post answers with `answers` in turn (a Resp or an exception); returns the list of requests."""
    sent = []
    queue = list(answers)

    def post(url, json=None, headers=None, timeout=None):
        sent.append({"url": url, "json": json, "headers": headers})
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(discovery.requests, "post", post)
    return sent


def test_message_names_host_port_and_token():
    cfg = make_cfg(http_port=8123)
    assert discovery.message(cfg, "tok") == {
        "service": "brainassist", "config": {"host": "127.0.0.1", "port": 8123, "token": "tok"}}


def test_outside_the_app_nothing_is_sent(monkeypatch):
    sent = fake_post(monkeypatch)
    assert discovery.announce(make_cfg(), "tok") is False
    assert sent == []


def test_the_app_tells_the_supervisor_and_never_logs_the_token(monkeypatch, caplog):
    cfg = make_cfg()
    cfg["supervisor_token"] = "sup"
    sent = fake_post(monkeypatch, Resp(200, '{"result": "ok", "data": {"uuid": "x"}}'))
    with caplog.at_level(logging.INFO, logger="discovery"):
        assert discovery.announce(cfg, "the-api-token") is True
    assert sent == [{"url": "http://supervisor/discovery",
                     "json": {"service": "brainassist",
                              "config": {"host": "127.0.0.1", "port": 8099, "token": "the-api-token"}},
                     "headers": {"Authorization": "Bearer sup"}}]
    assert "the-api-token" not in caplog.text and "Brain Assist" in caplog.text


def test_a_busy_supervisor_is_asked_again(monkeypatch):
    cfg = make_cfg()
    cfg["supervisor_token"] = "sup"
    sent = fake_post(monkeypatch, requests.ConnectionError("not up yet"), Resp(502, "bad gateway"), Resp(200))
    waits = []
    assert discovery.announce(cfg, "tok", sleep=waits.append) is True
    assert len(sent) == 3 and waits == [discovery.RETRY_S] * 2


def test_a_refusal_is_not_repeated_and_says_what_still_works(monkeypatch, caplog):
    cfg = make_cfg()
    cfg["supervisor_token"] = "sup"
    sent = fake_post(monkeypatch, Resp(403, "Apps must list services they provide via discovery in their config!"))
    with caplog.at_level(logging.WARNING, logger="discovery"):
        assert discovery.announce(cfg, "the-api-token", sleep=lambda s: None) is False
    assert len(sent) == 1
    assert "403" in caplog.text and "by hand" in caplog.text and "the-api-token" not in caplog.text


def test_gives_up_after_a_few_attempts(monkeypatch):
    cfg = make_cfg()
    cfg["supervisor_token"] = "sup"
    sent = fake_post(monkeypatch, *[requests.Timeout("slow")] * discovery.ATTEMPTS)
    assert discovery.announce(cfg, "tok", sleep=lambda s: None) is False
    assert len(sent) == discovery.ATTEMPTS
