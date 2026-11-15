"""Configuration: number normalisation, access rights, callers, migration, JSONC and pointer files, load()."""
import json
import os

import pytest

import config


# ---------------------------------------------------------------- norm_number
@pytest.mark.parametrize("raw, expected", [
    ("+49 151 1234567", "01511234567"),
    ("0049 151 1234567", "01511234567"),
    ("0151/123-4567", "01511234567"),
    ("**611", "**611"),          # internal FRITZ!Box numbers keep * and #
    ("#31#0151", "#31#0151"),
    ("+43 660 123", "0043660123"),  # other countries stay international
    ("", ""),
    (None, ""),
])
def test_norm_number(raw, expected):
    assert config.norm_number(raw, "49") == expected


def test_norm_number_without_country_code_keeps_prefix():
    assert config.norm_number("+49151", "") == "0049151"


@pytest.mark.parametrize("raw, expected", [
    ("04542 4635", "045424635"),         # #51: the space cut the number off in the INVITE
    (" 0151/123-4567 ", "01511234567"),
    ("+49 (0) 4542 4635", "+4945424635"),  # the (0) is not dialed after the country code
    ("0049 (0)151 123", "0049151123"),
    ("(04542) 4635", "045424635"),
    ("**611", "**611"),
    ("+49+151", "+49151"),
    (None, ""),
])
def test_dial_form(raw, expected):
    assert config.dial_form(raw) == expected


def test_norm_number_drops_the_bracketed_zero():
    assert config.norm_number("+49 (0) 4542 4635", "49") == "045424635"


# ---------------------------------------------------------------- parse_access
@pytest.mark.parametrize("value, expected", [
    ("shopping, calendar", {"shopping", "calendar"}),
    ("shopping;calendar read", {"shopping", "calendar", "read"}),
    (["Shopping", "CALENDAR"], {"shopping", "calendar"}),
    ("admin, shopping", {"admin"}),   # admin includes everything
    ("guest, shopping", {"shopping"}),
    ("guest", {"guest"}),
    ("", {"guest"}),                  # nothing valid = least privilege
    (None, {"guest"}),
])
def test_parse_access(value, expected):
    assert config.parse_access(value) == frozenset(expected)


def test_parse_access_unknown_word_is_ignored_with_warning():
    assert config.parse_access("superuser") == frozenset({"guest"})
    assert any("superuser" in w for w in config.WARNINGS)


# ---------------------------------------------------------------- _callers
def test_callers_are_normalised():
    out = config._callers([
        {"number": " 0151 ", "name": " Jan ", "access": "admin", "language": "uk", "notify": True, "web": False},
        {"number": "*", "access": "nonsense", "notify": "yes", "web": "yes"},
    ])
    assert out[0] == {"number": "0151", "ha_user": "", "ha_device": "", "name": "Jan",
                      "access": frozenset({"admin"}), "instructions": "", "language": "uk", "notify": True,
                      "web": False}
    assert out[1]["access"] == frozenset({"guest"})
    assert out[1]["notify"] is None  # only real booleans count
    assert out[1]["web"] is None


def test_callers_without_number_are_dropped():
    assert config._callers([{"name": "Nobody"}, "junk", {"number": ""}]) == []
    assert len([w for w in config.WARNINGS if "without number" in w]) == 3


def test_callers_for_brain_assist_need_no_number():
    """#50: a Home Assistant user or device instead of or next to a number."""
    out = config._callers([
        {"ha_user": " Jan ", "number": "0151", "name": "Jan", "access": "admin"},
        {"ha_user": "guest", "access": "shopping"},
        {"ha_device": "Küche", "access": "read"},
    ])
    assert [(c["number"], c["ha_user"], c["ha_device"]) for c in out] == [
        ("0151", "Jan", ""), ("", "guest", ""), ("", "", "Küche")]
    assert config.WARNINGS == []


# ---------------------------------------------------------------- API token, version
def test_api_token_from_the_configuration(tmp_path):
    cfg = config.Config(api_token=" secret ", log_dir=str(tmp_path))
    assert config.api_token(cfg) == ("secret", False)
    assert not (tmp_path / config.TOKEN_FILE).exists()


def test_api_token_is_generated_once_and_kept(tmp_path):
    cfg = config.Config(api_token="", log_dir=str(tmp_path / "logs"))
    token, generated = config.api_token(cfg)
    file = tmp_path / "logs" / config.TOKEN_FILE
    assert generated and len(token) >= 40
    assert file.read_text(encoding="utf-8").strip() == token
    assert config.api_token(cfg) == (token, True)  # the same after a restart
    if os.name != "nt":
        assert file.stat().st_mode & 0o077 == 0  # only for the owner


def test_api_token_file_can_be_replaced_by_hand(tmp_path):
    (tmp_path / config.TOKEN_FILE).write_text("my-own\n", encoding="utf-8")
    assert config.api_token(config.Config(api_token="", log_dir=str(tmp_path))) == ("my-own", True)


@pytest.mark.skipif(os.name == "nt", reason="file modes are a Unix thing")
def test_api_token_file_put_there_by_hand_is_restricted_to_the_owner(tmp_path):
    file = tmp_path / config.TOKEN_FILE
    file.write_text("my-own\n", encoding="utf-8")
    file.chmod(0o644)
    assert config.api_token(config.Config(api_token="", log_dir=str(tmp_path))) == ("my-own", True)
    assert file.stat().st_mode & 0o777 == 0o600
    assert config.WARNINGS == []


@pytest.mark.skipif(os.name == "nt", reason="file modes are a Unix thing")
def test_api_token_file_that_cannot_be_restricted_is_reported(tmp_path, monkeypatch):
    file = tmp_path / config.TOKEN_FILE
    file.write_text("my-own\n", encoding="utf-8")
    file.chmod(0o644)

    def refuse(self, mode):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(type(file), "chmod", refuse)
    assert config.api_token(config.Config(api_token="", log_dir=str(tmp_path))) == ("my-own", True)
    assert len(config.WARNINGS) == 1 and "readable by other users" in config.WARNINGS[0]


def test_version_is_the_add_on_version():
    text = (config.ROOT / "braincall" / "config.yaml").read_text(encoding="utf-8")
    assert config.version() and f'version: "{config.version()}"' in text


def test_callers_not_a_list():
    assert config._callers({"number": "0151"}) == []


# ---------------------------------------------------------------- JSONC
def test_strip_jsonc():
    text = """{
      // comment
      "a": "x // not a comment", /* block
      comment */ "b": [1, 2,],
      "c": "quote \\" // still string",
      "d": "/* also string */",
    }"""
    assert json.loads(config._strip_jsonc(text)) == {
        "a": "x // not a comment", "b": [1, 2], "c": 'quote " // still string', "d": "/* also string */"}


# ---------------------------------------------------------------- pointer files
def _pointer(tmp_path, raw_text):
    p = tmp_path / "config.json"
    p.write_text(raw_text, encoding="utf-8")
    return config._read(p)


def test_pointer_doubled_backslashes(tmp_path):
    assert _pointer(tmp_path, r'{ "path": "C:\\Users\\Jan\\config.json" }') == {"path": r"C:\Users\Jan\config.json"}


def test_pointer_single_backslashes_taken_literally(tmp_path):
    # would be tab/newline as JSON
    assert _pointer(tmp_path, r'{"path": "C:\temp\new\config.json"}') == {"path": r"C:\temp\new\config.json"}


def test_pointer_unc_paths(tmp_path):
    assert _pointer(tmp_path, r'{"path": "\\\\nas\\share\\c.json"}') == {"path": r"\\nas\share\c.json"}
    assert _pointer(tmp_path, r'{"path": "\\nas\share\c.json"}') == {"path": r"\\nas\share\c.json"}
    assert _pointer(tmp_path, r'{"path": "\\nas/share/c.json"}') == {"path": r"\\nas/share/c.json"}


def test_pointer_with_comment_and_bom(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('// where the real file is\n{"path": "/srv/bc.json",}\n', encoding="utf-8-sig")
    assert config._read(p) == {"path": "/srv/bc.json"}


def test_invalid_json_names_the_line(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{\n  "a": 1\n  "b": 2\n}', encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        config._read(p)
    assert "not valid JSON" in str(e.value) and '"a": 1' in str(e.value)


# ---------------------------------------------------------------- load()
def _load(tmp_path, monkeypatch, data, name="config.json"):
    p = tmp_path / name
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("BRAINCALL_CONFIG", str(p))
    return config.load()


def test_load_defaults_and_overrides(tmp_path, monkeypatch, tmp_env):
    cfg = _load(tmp_path, monkeypatch, {
        "sip_user": "620", "language": "uk", "_comment": "ignored", "tts_voice": None,
        "callers": [{"number": "0151", "name": "Jan", "access": "read, shopping"}],
    })
    assert cfg.sip_user == "620" and cfg.language == "uk"
    assert cfg.sip_server == config.DEFAULTS["sip_server"]
    assert cfg.tts_voice == config.DEFAULTS["tts_voice"]  # None = default
    assert "_comment" not in cfg
    assert cfg.callers[0]["access"] == frozenset({"read", "shopping"})
    assert cfg.log_dir == str(config.ROOT / "logs")
    assert config.CONFIG_FILE[0] == str(tmp_path / "config.json")
    assert cfg.missing_key is None  # attribute access never raises


def test_load_secrets_from_environment(tmp_path, monkeypatch, tmp_env):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    monkeypatch.setenv("BRAINCALL_SIP_PASSWORD", "env-pw")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "sup")
    cfg = _load(tmp_path, monkeypatch, {"anthropic_api_key": "file-key", "sip_password": "file-pw"})
    assert cfg.anthropic_api_key == "env-key"
    assert cfg.sip_password == "env-pw"
    assert cfg.supervisor_token == "sup"


def test_load_follows_pointer_with_variables(tmp_path, monkeypatch, tmp_env):
    real = tmp_path / "real"
    real.mkdir()
    (real / "bc.json").write_text('{"sip_user": "from-real", "log_dir": "${BC_TEST_DIR}/logs"}', encoding="utf-8")
    monkeypatch.setenv("BC_TEST_DIR", str(real))
    cfg = _load(tmp_path, monkeypatch, json.dumps({"path": "${BC_TEST_DIR}/bc.json"}))
    assert cfg.sip_user == "from-real"
    assert config.CONFIG_FILE[0] == str(real / "bc.json")
    assert cfg.log_dir == str(real / "logs")


def test_load_missing_pointer_target_stops(tmp_path, monkeypatch, tmp_env):
    with pytest.raises(SystemExit) as e:
        _load(tmp_path, monkeypatch, json.dumps({"path": str(tmp_path / "missing.json")}))
    assert "Configuration not found" in str(e.value)


_LEGACY = {"trusted_numbers": [{"number": "0151", "name": "Jan"}], "inbound_allowed": ["*"],
           "caller_languages": [{"number": "0151", "language": "uk"}]}  # settings removed in 1.9.3


def test_load_ignores_removed_legacy_settings(tmp_path, monkeypatch, tmp_env):
    cfg = _load(tmp_path, monkeypatch, {
        **_LEGACY, "callers": [{"number": "0999", "name": "Maryna", "access": "calendar"}]})
    assert [(c["number"], c["access"]) for c in cfg.callers] == [("0999", frozenset({"calendar"}))]
    assert _load(tmp_path, monkeypatch, _LEGACY).callers == []


def test_load_timezone_from_tz_variable(tmp_path, monkeypatch, tmp_env):
    monkeypatch.setenv("TZ", "America/New_York")
    assert _load(tmp_path, monkeypatch, {}).timezone == "America/New_York"
    assert _load(tmp_path, monkeypatch, {"timezone": "Europe/Kyiv"}).timezone == "Europe/Kyiv"
    monkeypatch.setenv("TZ", "CET-1CEST")  # POSIX string, not an IANA name
    assert _load(tmp_path, monkeypatch, {}).timezone == config.DEFAULTS["timezone"]


# ---------------------------------------------------------------- deps
def test_outdated_packages_are_found(monkeypatch):
    import importlib.metadata

    import deps
    monkeypatch.setattr(importlib.metadata, "version", lambda name: {"anthropic": "0.79.2"}[name])
    assert deps.outdated("anthropic>=0.80")
    assert not deps.outdated("anthropic>=0.79")
    assert not deps.outdated("pyVoIP==1.6.8")  # only minimum versions are checked
