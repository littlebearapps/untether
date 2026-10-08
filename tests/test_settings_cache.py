"""#506: content-keyed parse cache inside ``load_settings_if_exists``.

The cache is keyed on the config file's exact bytes plus the ``UNTETHER__*``
environment (not ``(mtime_ns, size)``: kernel < 6.13 timestamps are coarse, so
two same-size writes inside one tick look identical to ``stat``). Every read
still compares the bytes, so an edit applies on the very next read — the #269
per-run hot-reload survives, including mid-live-session turns.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from untether import settings as S
from untether.config import ConfigError, write_config

_BASE = (
    'transport = "telegram"\n'
    "[transports.telegram]\n"
    'bot_token = "1:test"\n'
    "chat_id = 1\n"
    "allow_any_user = true\n"
)


def _cfg(max_actions: int = 5, extra: str = "") -> str:
    return _BASE + f"[progress]\nmax_actions = {max_actions}\n" + extra


class _Counter:
    def __init__(self) -> None:
        self.parses = 0
        self.migrates = 0


@pytest.fixture
def counter(monkeypatch: pytest.MonkeyPatch) -> _Counter:
    """Count real parses (cached path + uncached path) and migrate calls."""
    c = _Counter()
    orig_parse = S._parse_settings_bytes
    orig_load = S._load_settings_from_path
    orig_migrate = S.migrate_config_file

    def parse(*a, **k):
        c.parses += 1
        return orig_parse(*a, **k)

    def load(*a, **k):
        c.parses += 1
        return orig_load(*a, **k)

    def migrate(*a, **k):
        c.migrates += 1
        return orig_migrate(*a, **k)

    monkeypatch.setattr(S, "_parse_settings_bytes", parse)
    monkeypatch.setattr(S, "_load_settings_from_path", load)
    monkeypatch.setattr(S, "migrate_config_file", migrate)
    monkeypatch.delenv(S._SETTINGS_CACHE_ENV, raising=False)
    return c


def _write_same_stat(p: Path, text: str) -> None:
    """Rewrite ``p`` in place and restore its previous (atime, mtime)."""
    st = p.stat()
    p.write_text(text)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))


def _load(p: Path) -> S.UntetherSettings:
    result = S.load_settings_if_exists(p)
    assert result is not None
    return result[0]


# --- hits -------------------------------------------------------------------


def test_second_call_hits_cache_same_instance(tmp_path: Path, counter: _Counter):
    p = tmp_path / "u.toml"
    p.write_text(_cfg())
    a = S.load_settings_if_exists(p)
    b = S.load_settings_if_exists(p)
    assert a is not None and b is not None
    assert a[0] is b[0]
    assert a[1] == b[1] == p
    assert counter.parses == 1


def test_many_calls_parse_once(tmp_path: Path, counter: _Counter):
    p = tmp_path / "u.toml"
    p.write_text(_cfg())
    for _ in range(100):
        _load(p)
    assert counter.parses == 1
    assert counter.migrates == 1


def test_hit_returns_shared_instance_document_contract(
    tmp_path: Path, counter: _Counter
):
    """D2: a hit returns the SAME instance — callers must treat it as
    read-only (``model_copy()`` before mutating)."""
    p = tmp_path / "u.toml"
    p.write_text(_cfg())
    assert _load(p) is _load(p)


# --- invalidation -------------------------------------------------------------


def test_same_size_same_mtime_edit_invalidates(tmp_path: Path, counter: _Counter):
    """D1 regression: a stat-keyed cache would serve 5 here."""
    p = tmp_path / "u.toml"
    p.write_text(_cfg(5))
    assert _load(p).progress.max_actions == 5
    st = p.stat()
    _write_same_stat(p, _cfg(6))
    st2 = p.stat()
    assert (st2.st_size, st2.st_mtime_ns) == (st.st_size, st.st_mtime_ns)
    assert _load(p).progress.max_actions == 6
    assert counter.parses == 2


def test_atomic_replace_invalidates(tmp_path: Path, counter: _Counter):
    p = tmp_path / "u.toml"
    p.write_text(_cfg(5))
    assert _load(p).progress.max_actions == 5
    import tomllib

    data = tomllib.loads(_cfg(7))
    write_config(data, p)  # temp file + os.replace
    assert _load(p).progress.max_actions == 7


def test_in_place_append_invalidates(tmp_path: Path, counter: _Counter):
    p = tmp_path / "u.toml"
    p.write_text(_cfg())
    assert _load(p).footer.show_api_cost is True
    with open(p, "a") as fh:
        fh.write("[footer]\nshow_api_cost = false\n")
    assert _load(p).footer.show_api_cost is False


def test_env_override_change_invalidates(
    tmp_path: Path, counter: _Counter, monkeypatch: pytest.MonkeyPatch
):
    p = tmp_path / "u.toml"
    p.write_text('default_engine = "codex"\n' + _cfg())
    assert _load(p).default_engine == "codex"
    monkeypatch.setenv("UNTETHER__DEFAULT_ENGINE", "claude")
    assert _load(p).default_engine == "claude"
    assert counter.parses == 2
    monkeypatch.delenv("UNTETHER__DEFAULT_ENGINE")
    assert _load(p).default_engine == "codex"
    assert counter.parses == 3


def test_env_fingerprint_case_insensitive_prefix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("untether__default_engine", "claude")
    monkeypatch.setenv("UNTETHER_SETTINGS_CACHE", "1")  # single underscore: ignored
    fp = S._env_fingerprint()
    assert ("UNTETHER__DEFAULT_ENGINE", "claude") in fp
    assert all(k.startswith("UNTETHER__") for k, _ in fp)


def test_missing_file_returns_none_and_drops_entry(tmp_path: Path, counter: _Counter):
    p = tmp_path / "u.toml"
    p.write_text(_cfg(5))
    assert _load(p).progress.max_actions == 5
    p.unlink()
    assert S.load_settings_if_exists(p) is None
    assert str(p) not in S._SETTINGS_CACHE
    p.write_text(_cfg(9))
    assert _load(p).progress.max_actions == 9


# --- errors are not cached ----------------------------------------------------


def test_invalid_config_raises_every_call_not_cached(tmp_path: Path, counter: _Counter):
    p = tmp_path / "u.toml"
    p.write_text("[[broken\n")
    for _ in range(2):
        with pytest.raises(ConfigError):
            S.load_settings_if_exists(p)
    assert counter.migrates == 2
    assert str(p) not in S._SETTINGS_CACHE
    p.write_text(_cfg(4))
    assert _load(p).progress.max_actions == 4


def test_invalid_schema_raises_every_call(tmp_path: Path, counter: _Counter):
    p = tmp_path / "u.toml"
    p.write_text(_cfg(500))  # le=50
    for _ in range(2):
        with pytest.raises(ConfigError):
            S.load_settings_if_exists(p)
    assert counter.parses == 2
    assert str(p) not in S._SETTINGS_CACHE


def test_valid_then_invalid_raises_not_stale(tmp_path: Path, counter: _Counter):
    p = tmp_path / "u.toml"
    p.write_text(_cfg(5))
    _load(p)
    p.write_text("[[broken\n")
    with pytest.raises(ConfigError):
        S.load_settings_if_exists(p)


def test_revert_to_previous_bytes_may_hit_old_entry(tmp_path: Path, counter: _Counter):
    p = tmp_path / "u.toml"
    a = _cfg(5)
    p.write_text(a)
    assert _load(p).progress.max_actions == 5
    p.write_text("[[broken\n")
    with pytest.raises(ConfigError):
        S.load_settings_if_exists(p)
    p.write_text(a)
    assert _load(p).progress.max_actions == 5


def test_toml_decode_error_on_parse_logs_and_raises(tmp_path: Path):
    """The in-memory parse keeps ``read_config``'s error mapping."""
    p = tmp_path / "u.toml"
    with capture_logs() as logs, pytest.raises(ConfigError, match="Malformed TOML"):
        S._parse_settings_bytes(b"[[broken\n", p)
    assert any(r["event"] == "config.read.toml_error" for r in logs)
    with pytest.raises(ConfigError):
        S._parse_settings_bytes(b"\xff\xfe", p)


# --- migration ----------------------------------------------------------------


def test_migration_runs_on_miss_only(tmp_path: Path, counter: _Counter):
    p = tmp_path / "u.toml"
    p.write_text('bot_token = "1:test"\nchat_id = 1\nallow_any_user = true\n')
    with capture_logs() as logs:
        s = _load(p)
    assert s.transports.telegram.chat_id == 1
    assert "[transports.telegram]" in p.read_text()
    assert [r["event"] for r in logs].count("config.migrated") == 1
    assert counter.migrates == 1
    assert counter.parses == 1
    _load(p)
    assert counter.migrates == 1
    assert counter.parses == 1


# --- A→B→A write race -----------------------------------------------------------


def test_write_during_parse_never_cached_under_wrong_bytes(
    tmp_path: Path, counter: _Counter, monkeypatch: pytest.MonkeyPatch
):
    p = tmp_path / "u.toml"
    a, b = _cfg(5), _cfg(6)
    p.write_text(a)
    inner = S._parse_settings_bytes  # the counting wrapper
    fired = [False]

    def racing(*args, **kwargs):
        if not fired[0]:
            fired[0] = True
            p.write_text(b)
        return inner(*args, **kwargs)

    monkeypatch.setattr(S, "_parse_settings_bytes", racing)
    # (a) parsed the captured A bytes
    assert _load(p).progress.max_actions == 5
    assert counter.parses == 1
    # (b) the file now holds B
    assert _load(p).progress.max_actions == 6
    assert counter.parses == 2
    # (c) A restored byte-for-byte with B's timestamps
    st = p.stat()
    p.write_text(a)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert _load(p).progress.max_actions == 5
    assert counter.parses in (2, 3)


# --- bounds / isolation ---------------------------------------------------------


def test_distinct_paths_isolated(tmp_path: Path, counter: _Counter):
    p1, p2 = tmp_path / "a.toml", tmp_path / "b.toml"
    p1.write_text(_cfg(1))
    p2.write_text(_cfg(2))
    s1, s2 = _load(p1), _load(p2)
    assert s1 is not s2
    assert (s1.progress.max_actions, s2.progress.max_actions) == (1, 2)
    assert _load(p1) is s1 and _load(p2) is s2
    assert counter.parses == 2


def test_lru_bound(tmp_path: Path, counter: _Counter):
    paths = []
    for i in range(6):
        p = tmp_path / f"u{i}.toml"
        p.write_text(_cfg(i))
        paths.append(p)
        _load(p)
    assert len(S._SETTINGS_CACHE) <= S._SETTINGS_CACHE_MAX == 4
    assert str(paths[0]) not in S._SETTINGS_CACHE
    before = counter.parses
    _load(paths[0])
    assert counter.parses == before + 1


# --- kill switch ----------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "false", "off", "no", " OFF "])
def test_kill_switch_disables_cache(
    tmp_path: Path, counter: _Counter, monkeypatch: pytest.MonkeyPatch, value: str
):
    monkeypatch.setenv(S._SETTINGS_CACHE_ENV, value)
    p = tmp_path / "u.toml"
    p.write_text(_cfg())
    with capture_logs() as logs:
        objs = [_load(p) for _ in range(3)]
    assert counter.parses == 3
    assert counter.migrates == 3
    assert objs[0] is not objs[1]
    assert S._SETTINGS_CACHE == {}
    assert not any(
        r["event"] == "config.loaded" and r["log_level"] == "info" for r in logs
    )


# --- logging ----------------------------------------------------------------------


def test_config_loaded_logged_once_per_real_parse(
    tmp_path: Path, counter: _Counter, monkeypatch: pytest.MonkeyPatch
):
    p = tmp_path / "u.toml"
    p.write_text(_cfg(5))

    def loaded(logs):
        return [r for r in logs if r["event"] == "config.loaded"]

    with capture_logs() as logs:
        for _ in range(5):
            _load(p)
    recs = loaded(logs)
    assert len(recs) == 1
    assert recs[0]["reason"] == "first_load"
    assert recs[0]["log_level"] == "info"
    assert recs[0]["path"] == str(p)

    with capture_logs() as logs:
        p.write_text(_cfg(6))
        _load(p)
        _load(p)
    recs = loaded(logs)
    assert [r["reason"] for r in recs] == ["content_changed"]
    assert recs[0]["log_level"] == "info"

    with capture_logs() as logs:
        monkeypatch.setenv("UNTETHER__DEFAULT_ENGINE", "claude")
        _load(p)
    recs = loaded(logs)
    assert [r["reason"] for r in recs] == ["env_changed"]


# --- strict loader stays uncached -------------------------------------------------


def test_load_settings_strict_is_uncached(tmp_path: Path, counter: _Counter):
    p = tmp_path / "u.toml"
    p.write_text(_cfg())
    with capture_logs() as logs:
        a, _ = S.load_settings(p)
        b, _ = S.load_settings(p)
    assert a is not b
    assert a == b
    assert counter.parses == 2
    assert S._SETTINGS_CACHE == {}
    recs = [r for r in logs if r["event"] == "config.loaded"]
    assert all(r["log_level"] == "debug" and r["reason"] == "uncached" for r in recs)
    assert S._bound_settings_class(p) is S._bound_settings_class(p)


def test_clear_settings_cache(tmp_path: Path, counter: _Counter):
    p = tmp_path / "u.toml"
    p.write_text(_cfg())
    _load(p)
    S._bound_settings_class(p)
    assert S._SETTINGS_CACHE
    S.clear_settings_cache()
    assert S._SETTINGS_CACHE == {}
    assert S._bound_settings_class.cache_info().currsize == 0


def test_in_memory_source_keeps_env_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """init > env > dotenv > toml > secrets — env still beats the file."""
    p = tmp_path / "u.toml"
    p.write_text('default_engine = "codex"\n' + _cfg())
    monkeypatch.setenv("UNTETHER__DEFAULT_ENGINE", "pi")
    cached = S._parse_settings_bytes(p.read_bytes(), p)
    strict, _ = S.load_settings(p)
    assert cached.default_engine == strict.default_engine == "pi"
    assert cached.model_dump() == strict.model_dump()


# --- bridge-level hot reload (#269 kept) ------------------------------------------


def test_bridge_helpers_see_edits(counter: _Counter):
    from untether import runner_bridge

    p = S.HOME_CONFIG_PATH  # the per-test tmp path (#808 _isolated_config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_cfg(5, "[footer]\nshow_api_cost = true\n"))
    assert runner_bridge._load_progress_settings().max_actions == 5
    assert runner_bridge._load_footer_settings().show_api_cost is True
    _write_same_stat(p, _cfg(6, "[footer]\nshow_api_cost = true\n"))
    assert runner_bridge._load_progress_settings().max_actions == 6
    # The per-turn footer read is what proves live-session turns see edits.
    p.write_text(_cfg(6, "[footer]\nshow_api_cost = false\n"))
    assert runner_bridge._load_footer_settings().show_api_cost is False
    assert counter.parses == 3
