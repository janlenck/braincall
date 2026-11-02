"""Configuration: number normalisation, access rights, callers, migration, JSONC and pointer files, load()."""
import json

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
        {"number": " 0151 ", "name": " Jan ", "access": "admin", "language": "uk", "notify": True},
        {"number": "*", "access": "nonsense", "notify": "yes"},
    ])
    assert out[0] == {"number": "0151", "name": "Jan", "access": frozenset({"admin"}), "instructions": "",
                      "language": "uk", "notify": True}
    assert out[1]["access"] == frozenset({"guest"})
    assert out[1]["notify"] is None  # only real booleans count


def test_callers_without_number_are_dropped():
    assert config._callers([{"name": "Nobody"}, "junk", {"number": ""}]) == []
    assert len([w for w in config.WARNINGS if "without number" in w]) == 3


def test_callers_not_a_list():
    assert config._callers({"number": "0151"}) == []


# ---------------------------------------------------------------- migrate
def test_migrate_nothing_set():
    assert config.migrate({}) is None
    assert config.migrate({"inbound_allowed": [], "trusted_numbers": []}) is None


def test_migrate_addon_lists():
    data = {
        "owner_name": "Jan Lenck",
        "trusted_numbers": [{"number": "0151111", "name": "Jan Mobile"}, {"number": "0222", "name": ""}],
        "inbound_allowed": ["*", "0170333", "+49151111"],
        "caller_languages": [{"number": "+49151111", "language": "uk"}, {"number": "0999", "language": "en"}],
    }
    callers = config.migrate(data)
    assert callers == [
        {"number": "0151111", "name": "Jan Mobile", "access": "admin", "language": "uk"},
        {"number": "0222", "name": "Jan Lenck", "access": "admin"},
        {"number": "*", "name": "Unknown", "access": "guest"},
    ]
    assert any("0170333" in w and "dropped" in w for w in config.WARNINGS)
    assert not any("+49151111" in w and "inbound_allowed" in w for w in config.WARNINGS)  # same as a trusted one
    assert any("0999" in w for w in config.WARNINGS)


def test_migrate_old_dict_format():
    callers = config.migrate({"trusted_numbers": {"0151": "Jan"}, "caller_languages": {"0151": "en"}})
    assert callers == [{"number": "0151", "name": "Jan", "access": "admin", "language": "en"}]


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


def test_load_converts_legacy_settings(tmp_path, monkeypatch, tmp_env):
    cfg = _load(tmp_path, monkeypatch, {
        "callers": [{"number": "0999", "name": "ignored while legacy is set", "access": "admin"}],
        "trusted_numbers": [{"number": "0151", "name": "Jan"}], "inbound_allowed": ["*"],
    })
    assert [(c["number"], c["access"]) for c in cfg.callers] == [("0151", frozenset({"admin"})),
                                                                 ("*", frozenset({"guest"}))]
    assert not any(k in cfg for k in config.LEGACY_KEYS)
    assert any("Old settings" in w for w in config.WARNINGS)


def test_load_timezone_from_tz_variable(tmp_path, monkeypatch, tmp_env):
    monkeypatch.setenv("TZ", "America/New_York")
    assert _load(tmp_path, monkeypatch, {}).timezone == "America/New_York"
    assert _load(tmp_path, monkeypatch, {"timezone": "Europe/Kyiv"}).timezone == "Europe/Kyiv"
    monkeypatch.setenv("TZ", "CET-1CEST")  # POSIX string, not an IANA name
    assert _load(tmp_path, monkeypatch, {}).timezone == config.DEFAULTS["timezone"]
