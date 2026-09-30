"""Tests for `griot profiles list` and the `--profile` flag (plan, section 9.3/8.1).

Today's scope: only profiles of the "local" backend (plus the single
already-configured "direct" profile, gemini) — new paid profiles
(voyage/openai) and the "remote" backend don't exist yet in EMBED_PROFILES,
decision pending (section 9-26).
"""

import pytest

from griot import cli, common


class FakeVirtualMemory:
    def __init__(self, total_bytes):
        self.total = total_bytes


def _mock_ram(monkeypatch, gb):
    import psutil

    monkeypatch.setattr(psutil, "virtual_memory", lambda: FakeVirtualMemory(gb * 1024 ** 3))


# --- griot profiles list ----------------------------------------------------

def test_profiles_list_8gb_flags_medium_and_heavy_as_might_be_tight(monkeypatch, capsys):
    """The criterion is the tier's NOMINAL floor (light=8/medium=16/heavy=32GB)
    compared against real RAM, not a percentage of rss_estimate_mb — matches
    the mockup in the design notes On an 8GB machine only the light tier
    (8GB floor) fits; medium (16GB floor) and heavy (32GB floor) fall short."""
    _mock_ram(monkeypatch, 8)
    rc = cli.main(["profiles", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "8.0 GB" in out
    lines = out.splitlines()
    for name in ["bge-small", "nomic-q"]:
        line = next(l for l in lines if l.strip().startswith(name))
        assert "fits comfortably" in line, f"{name} should fit in 8GB: {line!r}"
    for name in ["jina-code", "mxbai-large", "bge-m3", "bge-large-en"]:
        line = next(l for l in lines if l.strip().startswith(name))
        assert "might be tight" in line, f"{name} should not fit in 8GB: {line!r}"


def test_profiles_list_32gb_everything_fits_comfortably(monkeypatch, capsys):
    _mock_ram(monkeypatch, 32)
    rc = cli.main(["profiles", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "32.0 GB" in out
    for name in ["bge-small", "nomic-q", "jina-code", "mxbai-large", "bge-m3", "bge-large-en"]:
        line = next(l for l in out.splitlines() if l.strip().startswith(name))
        assert "fits comfortably" in line, f"{name} should fit in 32GB: {line!r}"


def test_profiles_list_16gb_reproduces_section_9_3_mockup(monkeypatch, capsys):
    """16GB is the literal example from the section 9.3 mockup: light and
    medium fit, heavy (32GB floor) doesn't."""
    _mock_ram(monkeypatch, 16)
    rc = cli.main(["profiles", "list"])
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    for name in ["bge-small", "nomic-q", "jina-code", "mxbai-large"]:
        line = next(l for l in lines if l.strip().startswith(name))
        assert "fits comfortably" in line, f"{name} should fit in 16GB: {line!r}"
    for name in ["bge-m3", "bge-large-en"]:
        line = next(l for l in lines if l.strip().startswith(name))
        assert "might be tight" in line, f"{name} should not fit in 16GB: {line!r}"


def test_profiles_list_flags_active(monkeypatch, capsys):
    _mock_ram(monkeypatch, 16)
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "nomic-q")
    rc = cli.main(["profiles", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    nomic_line = next(l for l in out.splitlines() if l.strip().startswith("nomic-q"))
    assert "ACTIVE" in nomic_line
    jina_line = next(l for l in out.splitlines() if l.strip().startswith("jina-code"))
    assert "ACTIVE" not in jina_line


def test_profiles_list_shows_paid_profile(monkeypatch, capsys):
    _mock_ram(monkeypatch, 16)
    rc = cli.main(["profiles", "list"])
    out = capsys.readouterr().out
    assert "gemini" in out
    assert "PAID" in out


def test_profiles_list_checks_each_paid_profiles_own_credential(monkeypatch, capsys):
    """gemini uses GEMINI_TOKEN, openai-small uses GRIOT_OPENAI_API_KEY —
    each checked independently, never the other one's variable."""
    _mock_ram(monkeypatch, 16)
    monkeypatch.setattr(common, "GEMINI_TOKEN", None)
    monkeypatch.delenv("GRIOT_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-fake")

    rc = cli.main(["profiles", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    gemini_line = next(l for l in out.splitlines() if l.strip().startswith("gemini"))
    openai_line = next(l for l in out.splitlines() if l.strip().startswith("openai-small"))
    assert "missing GEMINI_TOKEN" in gemini_line
    assert "configured" in openai_line


def test_profiles_list_suggests_griot_auth_set_for_missing_credential(monkeypatch, capsys):
    """the status column for a paid profile missing its
    credential suggests the command to configure it, not just states that it's missing."""
    _mock_ram(monkeypatch, 16)
    monkeypatch.setattr(common, "GEMINI_TOKEN", None)
    monkeypatch.delenv("GRIOT_OPENAI_API_KEY", raising=False)

    rc = cli.main(["profiles", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    gemini_line = next(l for l in out.splitlines() if l.strip().startswith("gemini"))
    openai_line = next(l for l in out.splitlines() if l.strip().startswith("openai-small"))
    assert "griot auth set gemini" in gemini_line
    assert "griot auth set openai" in openai_line


# --- --profile override -----------------------------------------------------

def test_profile_flag_overrides_env_var_before_dispatch(monkeypatch):
    """`--profile` needs to set GRIOT_EMBED_PROFILE in the process BEFORE
    any import of common by the subcommand's module — tested indirectly
    here by checking os.environ, since the real module (index_code) is lazily
    imported and mocking its main wouldn't prove the setenv order."""
    monkeypatch.delenv("GRIOT_EMBED_PROFILE", raising=False)
    import importlib
    calls = []
    module = importlib.import_module("griot.index_code")

    def fake_main(argv=None):
        import os
        calls.append(os.environ.get("GRIOT_EMBED_PROFILE"))
        return None

    monkeypatch.setattr(module, "main", fake_main)
    rc = cli.main(["index", "code", "--profile", "bge-small", "--repo", "x"])
    assert rc == 0
    assert calls == ["bge-small"]


def test_profile_flag_equal_syntax(monkeypatch):
    import importlib
    calls = []
    module = importlib.import_module("griot.index_code")

    def fake_main(argv=None):
        import os
        calls.append(os.environ.get("GRIOT_EMBED_PROFILE"))
        return None

    monkeypatch.setattr(module, "main", fake_main)
    rc = cli.main(["index", "code", "--profile=nomic-q"])
    assert rc == 0
    assert calls == ["nomic-q"]


def test_profile_flag_does_not_leak_into_module_argv(monkeypatch):
    """--profile is consumed by cli.py and NEVER passed through in the REMAINDER —
    the source module's argparse doesn't know this flag."""
    import importlib
    calls = []
    module = importlib.import_module("griot.index_code")

    def fake_main(argv=None):
        calls.append(argv)
        return None

    monkeypatch.setattr(module, "main", fake_main)
    cli.main(["index", "code", "--profile", "bge-small", "--repo", "x"])
    assert calls == [["--repo", "x"]]


def test_profile_flag_absent_does_not_set_env_var(monkeypatch):
    monkeypatch.delenv("GRIOT_EMBED_PROFILE", raising=False)
    import importlib
    calls = []
    module = importlib.import_module("griot.index_code")

    def fake_main(argv=None):
        import os
        calls.append(os.environ.get("GRIOT_EMBED_PROFILE"))
        return None

    monkeypatch.setattr(module, "main", fake_main)
    cli.main(["index", "code", "--repo", "x"])
    assert calls == [None]


def test_profile_flag_no_value_gives_clear_error():
    with pytest.raises(SystemExit):
        cli.main(["index", "code", "--profile"])


def test_profile_flag_equal_empty_gives_clear_error():
    with pytest.raises(SystemExit):
        cli.main(["index", "code", "--profile="])


def test_profile_flag_repeated_last_wins(monkeypatch):
    import importlib
    calls = []
    module = importlib.import_module("griot.index_code")

    def fake_main(argv=None):
        import os
        calls.append(os.environ.get("GRIOT_EMBED_PROFILE"))
        return None

    monkeypatch.setattr(module, "main", fake_main)
    rc = cli.main(["index", "code", "--profile", "bge-small", "--profile", "nomic-q"])
    assert rc == 0
    assert calls == ["nomic-q"]


# --- griot profiles delete --------------------------------------------------
# [user-requested] "how do I stop paying for an index I no longer want to
# use" — there was no way to reclaim disk space from a profile's collection
# except manually deleting qdrant_data/<collection> by hand. Mirrors
# repos.py's add_repo()/remove_repo() split: a pure function (delete_profile,
# shared by the CLI and griot_profiles_delete) plus a thin
# CLI wrapper.


def test_delete_profile_removes_the_collection_and_returns_its_name(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    non_active = next(n for n in common.EMBED_PROFILES if n != "jina-code")
    collection = common.collection_name_for(non_active)
    path = common._collection_path(collection)
    path.mkdir(parents=True, exist_ok=True)
    (path / common._EDGE_CONFIG_MARKER).write_text("{}")

    result = cli.delete_profile(non_active)

    assert result == collection
    assert not path.exists()


def test_delete_profile_rejects_unknown_profile():
    with pytest.raises(ValueError, match="unknown profile"):
        cli.delete_profile("not-a-real-profile")


def test_delete_profile_rejects_the_active_profile(monkeypatch):
    """Safety guard: the active profile is what search/index currently
    target — deleting it out from under the user without first switching
    would be confusing at best (and matches the same "switch away first"
    posture as other irreversible operations in this codebase)."""
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    with pytest.raises(ValueError, match="active"):
        cli.delete_profile("jina-code")


def test_delete_profile_rejects_a_never_indexed_profile(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    non_active = next(n for n in common.EMBED_PROFILES if n != "jina-code")
    with pytest.raises(ValueError, match="does not exist"):
        cli.delete_profile(non_active)


def test_cmd_profiles_delete_happy_path(monkeypatch, capsys):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    non_active = next(n for n in common.EMBED_PROFILES if n != "jina-code")
    collection = common.collection_name_for(non_active)
    path = common._collection_path(collection)
    path.mkdir(parents=True, exist_ok=True)
    (path / common._EDGE_CONFIG_MARKER).write_text("{}")
    # Answered at a terminal (there is no --yes); see tests/test_cli_confirm.py.
    monkeypatch.setattr(common, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    rc = cli.main(["profiles", "delete", non_active])

    assert rc == 0
    assert "Deleted" in capsys.readouterr().out
    assert not path.exists()


def test_cmd_profiles_delete_unknown_profile_is_a_clear_cli_error(capsys):
    rc = cli.main(["profiles", "delete", "not-a-real-profile"])
    assert rc == 1
    assert "Error" in capsys.readouterr().err


def test_delete_profile_active_override_lets_a_no_longer_active_profile_be_deleted(monkeypatch):
    """[review finding] active_profile_name exists FOR a long-lived caller
    (service.delete_profile() passes its own live-resolved value) — a
    fresh CLI process never needs it (common.ACTIVE_PROFILE_NAME is never
    stale there), but the override's own contract is worth pinning
    directly: it must WIN over the frozen constant in both directions."""
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    other = next(n for n in common.EMBED_PROFILES if n != "jina-code")
    path = common._collection_path(common.collection_name_for("jina-code"))
    path.mkdir(parents=True, exist_ok=True)
    (path / common._EDGE_CONFIG_MARKER).write_text("{}")

    # frozen constant says jina-code is active, but the override says
    # `other` is — jina-code (no longer really active per the override)
    # can be deleted...
    result = cli.delete_profile("jina-code", active_profile_name=other)
    assert result == common.collection_name_for("jina-code")
    assert not path.exists()

    # ...and `other` (the one the override says IS active) must be refused.
    with pytest.raises(ValueError, match="active"):
        cli.delete_profile(other, active_profile_name=other)
