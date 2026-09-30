"""Tests for the single CLI (griot.cli) — only the DISPATCH and search
formatting.

The modules' mains are monkeypatched: nothing here indexes, embeds, or makes
a real network call — each subcommand's behavior is already covered by the
modules' own tests; what matters here is that the right argv reaches the
right main.
"""

import importlib

import pytest

from griot import __version__, cli, common, logdb


class FakeHit:
    """Same shape as the points that common.search returns (ScoredPoint)."""

    def __init__(self, score, payload, id="00000000-0000-0000-0000-000000000001"):
        self.score = score
        self.payload = payload
        self.id = id


def _patch_module_main(monkeypatch, module_name, calls, key, rc=None):
    module = importlib.import_module(module_name)

    def fake_main(argv=None):
        calls.append((key, argv))
        return rc

    monkeypatch.setattr(module, "main", fake_main)


# --- dispatch for each subcommand -------------------------------------------

@pytest.mark.parametrize("source", cli.INDEX_SOURCES)
def test_index_dispatches_to_source_module(monkeypatch, source):
    calls = []
    _patch_module_main(monkeypatch, f"griot.index_{source}", calls, source)
    rc = cli.main(["index", source, "--repo", "my-repo", "--dry-run"])
    assert rc == 0
    assert calls == [(source, ["--repo", "my-repo", "--dry-run"])]


@pytest.mark.parametrize("source", cli.INDEX_SOURCES)
def test_index_dispatches_path_to_source_module(monkeypatch, source, tmp_path):
    """--path is passed through the same way --repo is today — via REMAINDER,
    without the CLI knowing the flag; the real parser is each source
    module's own."""
    calls = []
    _patch_module_main(monkeypatch, f"griot.index_{source}", calls, source)
    rc = cli.main(["index", source, "--path", str(tmp_path)])
    assert rc == 0
    assert calls == [(source, ["--path", str(tmp_path)])]


@pytest.mark.parametrize("source", cli.INDEX_SOURCES)
def test_index_path_and_repo_together_errors_in_source_module(source, tmp_path, capsys):
    """--path and --repo are mutually exclusive in each source module's
    parser (not mocked here: needs the module's real argparse to validate)."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["index", source, "--path", str(tmp_path), "--repo", "my-repo"])
    assert exc.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


@pytest.mark.parametrize("command,module_name", [
    ("ask", "griot.ask"),
    ("quality-check", "griot.quality_check"),
    ("auth", "griot.auth"),
    ("stats", "griot.stats"),
    ("repos", "griot.repos"),
    ("golden-set", "griot.golden_set"),
])
def test_passthrough_subcommands_dispatch(monkeypatch, command, module_name):
    calls = []
    _patch_module_main(monkeypatch, module_name, calls, command)
    rc = cli.main([command, "arg1", "--flag", "value"])
    assert rc == 0


@pytest.mark.parametrize("command,module_name", [
    ("ask", "griot.ask"),
    ("quality-check", "griot.quality_check"),
    ("auth", "griot.auth"),
    ("stats", "griot.stats"),
    ("repos", "griot.repos"),
    ("golden-set", "griot.golden_set"),
])
def test_passthrough_subcommand_flag_as_the_very_first_token_dispatches(monkeypatch, command, module_name):
    """[real finding] argparse.REMAINDER + subparsers has a
    long-standing CPython bug: when a subparser's ONLY positional is a bare
    REMAINDER, the first forwarded token being option-like used to get
    rejected as "unrecognized arguments" by the TOP-level parser instead of
    reaching the subparser — e.g. `griot stats --json` (documented in this
    project's own README.md) was silently broken. This is the case
    test_passthrough_subcommands_dispatch (above) does NOT cover: that one
    always has a non-flag token ('arg1') first, which never triggered the
    bug in the first place."""
    calls = []
    _patch_module_main(monkeypatch, module_name, calls, command)
    rc = cli.main([command, "--flag", "value"])
    assert rc == 0
    assert calls == [(command, ["--flag", "value"])]


def test_stats_json_flag_as_first_token_reaches_stats_main(monkeypatch):
    """The exact real-world repro: `griot stats --json` is documented in
    README.md's Usage section — confirm it actually dispatches, not just a
    synthetic module/flag pair."""
    from griot import stats

    calls = []

    def fake_main(argv=None):
        calls.append(argv)
        return 0

    monkeypatch.setattr(stats, "main", fake_main)
    rc = cli.main(["stats", "--json"])
    assert rc == 0
    assert calls == [["--json"]]


def test_mcp_subcommand_runs_the_real_server(monkeypatch):
    """`griot mcp` (that decision — a single subcommand, not
    `python -m griot.mcp_server` nor a separate binary) actually needs to
    call mcp.run() — real finding: mcp_server.py had no way at all to run
    the server until this fix."""
    import griot.mcp_server as mcp_server_module

    calls = []
    monkeypatch.setattr(mcp_server_module.mcp, "run", lambda *a, **kw: calls.append((a, kw)))

    rc = cli.main(["mcp"])

    assert rc == 0
    assert calls


def test_index_all_runs_sources_in_pipeline_order(monkeypatch):
    calls = []
    for source in cli.INDEX_SOURCES:
        _patch_module_main(monkeypatch, f"griot.index_{source}", calls, source)
    rc = cli.main(["index", "all", "--repo", "my-repo"])
    assert rc == 0
    assert [c[0] for c in calls] == cli.INDEX_SOURCES
    # the passed-through args arrive identical across all sources
    assert all(c[1] == ["--repo", "my-repo"] for c in calls)


def test_index_all_stops_at_first_failure_and_names_source(monkeypatch, capsys):
    calls = []
    _patch_module_main(monkeypatch, "griot.index_code", calls, "code")

    def broken_main(argv=None):
        calls.append(("commits", argv))
        raise RuntimeError("repos.json disappeared")

    monkeypatch.setattr(importlib.import_module("griot.index_commits"), "main", broken_main)
    # tags/branches/platform must NOT run after the error
    for source in ["tags", "branches", "platform"]:
        _patch_module_main(monkeypatch, f"griot.index_{source}", calls, source)

    rc = cli.main(["index", "all"])
    assert rc == 1
    assert [c[0] for c in calls] == ["code", "commits"]
    err = capsys.readouterr().err
    assert "griot index commits" in err  # reports WHICH source failed


def test_index_all_stops_on_nonzero_exit_code(monkeypatch, capsys):
    calls = []
    _patch_module_main(monkeypatch, "griot.index_code", calls, "code", rc=3)
    for source in ["commits", "tags", "branches", "platform"]:
        _patch_module_main(monkeypatch, f"griot.index_{source}", calls, source)

    rc = cli.main(["index", "all"])
    assert rc == 3
    assert [c[0] for c in calls] == ["code"]
    assert "griot index code" in capsys.readouterr().err


# --- a run that DIES must not be invisible ---------------------------------
# [real incident] A whole `griot index
# code` died with Kind(WouldBlock) (lock collision with another process) and
# never reached its log_run_summary() call, so nothing was ever recorded:
# `griot stats` kept showing the previous SUCCESS as the
# most recent run, as if the failure had never happened. A run that starts
# must leave a trace whether it succeeds or dies.


def _broken_main(exc):
    def main(argv=None):
        raise exc
    return main


def test_a_crashing_single_source_records_a_failed_run(monkeypatch):
    monkeypatch.setattr(importlib.import_module("griot.index_code"), "main",
                        _broken_main(RuntimeError("Kind(WouldBlock)")))

    with pytest.raises(RuntimeError):
        cli.main(["index", "code"])

    runs = logdb.read_since(common.LOG_DIR, "runs", days=1)
    assert len(runs) == 1
    assert runs[0]["script"] == "index_code.py"
    assert "WouldBlock" in runs[0]["error"]


def test_a_crashing_source_inside_index_all_records_a_failed_run(monkeypatch):
    calls = []
    _patch_module_main(monkeypatch, "griot.index_code", calls, "code")
    monkeypatch.setattr(importlib.import_module("griot.index_commits"), "main",
                        _broken_main(RuntimeError("boom")))
    for source in ["tags", "branches", "platform"]:
        _patch_module_main(monkeypatch, f"griot.index_{source}", calls, source)

    assert cli.main(["index", "all"]) == 1

    runs = logdb.read_since(common.LOG_DIR, "runs", days=1)
    assert [r["script"] for r in runs] == ["index_commits.py"]
    assert "boom" in runs[0]["error"]


def test_a_failed_run_records_no_fake_counts(monkeypatch):
    """indexed/skipped/failed must be None, not 0: a died run genuinely
    doesn't know its counts, and 0 would read as 'ran fine, did nothing'
    while also skewing griot stats' totals."""
    monkeypatch.setattr(importlib.import_module("griot.index_code"), "main",
                        _broken_main(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        cli.main(["index", "code"])

    run = logdb.read_since(common.LOG_DIR, "runs", days=1)[0]
    assert run["indexed"] is None and run["skipped"] is None and run["failed"] is None


def test_a_successful_run_records_no_error_field(monkeypatch):
    calls = []
    _patch_module_main(monkeypatch, "griot.index_code", calls, "code")

    assert cli.main(["index", "code"]) == 0

    # the source module's own main() is what logs a success — the failure
    # path here must not add a second, spurious record on top of it
    assert logdb.read_since(common.LOG_DIR, "runs", days=1) == []


def test_a_run_killed_by_keyboard_interrupt_is_still_recorded(monkeypatch):
    """Ctrl-C is the most common way a real indexing run dies. It's a
    BaseException, so a plain `except Exception` would miss it."""
    monkeypatch.setattr(importlib.import_module("griot.index_code"), "main",
                        _broken_main(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        cli.main(["index", "code"])

    assert len(logdb.read_since(common.LOG_DIR, "runs", days=1)) == 1


# --- griot index all --sources ---------------------------------


def test_index_all_sources_filters_to_requested_subset(monkeypatch):
    calls = []
    for source in cli.INDEX_SOURCES:
        _patch_module_main(monkeypatch, f"griot.index_{source}", calls, source)

    rc = cli.main(["index", "all", "--sources", "code,commits", "--path", "/tmp/some-repo"])

    assert rc == 0
    assert [c[0] for c in calls] == ["code", "commits"]
    # --sources never leaks into the argv passed through to the source modules
    assert all(c[1] == ["--path", "/tmp/some-repo"] for c in calls)


def test_index_all_sources_equal_syntax(monkeypatch):
    calls = []
    for source in cli.INDEX_SOURCES:
        _patch_module_main(monkeypatch, f"griot.index_{source}", calls, source)

    rc = cli.main(["index", "all", "--sources=tags,branches"])

    assert rc == 0
    assert [c[0] for c in calls] == ["tags", "branches"]


def test_index_all_without_sources_still_runs_all_five(monkeypatch):
    calls = []
    for source in cli.INDEX_SOURCES:
        _patch_module_main(monkeypatch, f"griot.index_{source}", calls, source)

    rc = cli.main(["index", "all"])

    assert rc == 0
    assert [c[0] for c in calls] == cli.INDEX_SOURCES


def test_index_all_sources_rejects_unknown_source(capsys):
    rc = cli.main(["index", "all", "--sources", "code,comits"])  # intentional typo
    assert rc != 0
    assert "comits" in capsys.readouterr().err


def test_index_all_sources_does_not_affect_single_source_command(monkeypatch):
    """--sources only makes sense with `all` — passed through as --sources to
    a single source's argparse REMAINDER it would just be noise; confirms
    that the global extraction doesn't break this case (the flag is simply
    unused)."""
    calls = []
    _patch_module_main(monkeypatch, "griot.index_code", calls, "code")
    rc = cli.main(["index", "code", "--repo", "x"])
    assert rc == 0
    assert calls == [("code", ["--repo", "x"])]


# --- griot search -----------------------------------------------------------

def test_search_formats_results_without_chat(monkeypatch, capsys):
    search_calls = []

    def fake_search(query, limit=5, diverse=False):
        search_calls.append((query, limit))
        return [
            FakeHit(0.912, {"source_type": "commit", "repo": "shop", "commit_hash": "abcdef1234", "content": "fix: fix shipping\n\ndetail"}),
            FakeHit(0.774, {"source_type": "code", "repo": "shop", "file_path": "app/shipping.py", "content": "def calculate_shipping():"}),
        ]

    monkeypatch.setattr(common, "search", fake_search)

    def explode(*a, **kw):  # search must NEVER synthesize via LLM
        raise AssertionError("griot search must not call chat_completion")

    monkeypatch.setattr(common, "chat_completion", explode)

    rc = cli.main(["search", "how does shipping work", "--limit", "2"])
    assert rc == 0
    assert search_calls == [("how does shipping work", 2)]
    out = capsys.readouterr().out
    # formatting: score + source_label (from ask.source_label) + preview
    assert "[0.912] commit abcdef12 — shop" in out
    assert "[0.774] shop/app/shipping.py" in out
    assert "fix: fix shipping" in out


def test_search_empty_results(monkeypatch, capsys):
    monkeypatch.setattr(common, "search", lambda query, limit=5, diverse=False: [])
    rc = cli.main(["search", "nothing"])
    assert rc == 0
    assert "No results" in capsys.readouterr().out


# --- errors and --version ------------------------------------------------------

def test_invalid_subcommand_errors_clearly(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["unknown-command"])
    assert exc.value.code == 2
    assert "unknown-command" in capsys.readouterr().err


def test_invalid_index_source_errors_clearly(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["index", "unknown-source"])
    assert exc.value.code == 2
    assert "unknown-source" in capsys.readouterr().err


def test_missing_subcommand_errors(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert f"griot {__version__}" in capsys.readouterr().out


def test_module_mains_accept_argv():
    """Step contract: every main() in the consolidated modules accepts an
    injectable argv (main(argv=None)) — that's what allows CLI dispatch and
    tests without touching sys.argv."""
    import inspect

    for module_name in ["griot.index_code", "griot.index_commits", "griot.index_tags",
                        "griot.index_branches", "griot.index_platform", "griot.ask",
                        "griot.quality_check",
                        "griot.auth", "griot.stats", "griot.repos", "griot.golden_set"]:
        sig = inspect.signature(importlib.import_module(module_name).main)
        assert "argv" in sig.parameters, module_name


# --- a held collection and --dry-run ----------------------------------------
# A dry-run writes nothing when it succeeds, so a dry-run that dies must not
# leave a "died" record either: `griot stats` counted those as failed
# indexing runs, and a colliding dry-run shadowed the last real run in
# griot_index_status.


@pytest.mark.parametrize("flag", ["--dry-run", "--dry", "--dr"])
def test_a_dry_run_that_dies_is_not_recorded_as_a_failed_run(monkeypatch, flag):
    # argparse accepts any unambiguous prefix, so the flag cannot be matched literally.
    monkeypatch.setattr(importlib.import_module("griot.index_code"), "main",
                        _broken_main(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        cli.main(["index", "code", flag])

    assert logdb.read_since(common.LOG_DIR, "runs", days=1) == []


def test_a_dry_run_inside_index_all_is_not_recorded_either(monkeypatch):
    monkeypatch.setattr(importlib.import_module("griot.index_code"), "main",
                        _broken_main(RuntimeError("boom")))

    assert cli.main(["index", "all", "--dry-run"]) == 1

    assert logdb.read_since(common.LOG_DIR, "runs", days=1) == []


@pytest.mark.parametrize("argv", [["--repo", "dry-run"], ["--drift"], ["--repo", "x"]])
def test_only_a_real_dry_run_flag_skips_the_failed_run_record(monkeypatch, argv):
    monkeypatch.setattr(importlib.import_module("griot.index_code"), "main",
                        _broken_main(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        cli.main(["index", "code", *argv])

    assert len(logdb.read_since(common.LOG_DIR, "runs", days=1)) == 1


def _busy(name="codebase__jina-code"):
    return common.CollectionBusyError(name, common.QDRANT_PATH / name, f"Could not open collection '{name}'")


def test_a_held_collection_prints_an_explanation_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(common, "find_collection_holders", lambda path: [])
    monkeypatch.setattr(importlib.import_module("griot.index_code"), "main", _broken_main(_busy()))

    rc = cli.main(["index", "code", "--dry-run"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "holds the collection 'codebase__jina-code'" in err
    assert "Traceback" not in err


def test_a_held_collection_is_explained_for_every_command_not_just_index(monkeypatch, capsys):
    monkeypatch.setattr(common, "find_collection_holders", lambda path: [])

    def busy_search(*a, **kw):
        raise _busy()

    monkeypatch.setattr(common, "search", busy_search)

    rc = cli.main(["search", "anything"])

    assert rc == 1
    assert "holds the collection" in capsys.readouterr().err


def test_a_held_collection_inside_index_all_is_explained_once(monkeypatch, capsys):
    monkeypatch.setattr(common, "find_collection_holders", lambda path: [])
    monkeypatch.setattr(importlib.import_module("griot.index_code"), "main", _broken_main(_busy()))

    rc = cli.main(["index", "all"])

    err = capsys.readouterr().err
    assert rc == 1
    assert err.count("holds the collection") == 1
    assert "Traceback" not in err


def test_a_real_run_killed_by_a_held_collection_is_still_recorded(monkeypatch, capsys):
    monkeypatch.setattr(common, "find_collection_holders", lambda path: [])
    monkeypatch.setattr(importlib.import_module("griot.index_code"), "main", _broken_main(_busy()))

    assert cli.main(["index", "code"]) == 1

    runs = logdb.read_since(common.LOG_DIR, "runs", days=1)
    assert len(runs) == 1 and "Could not open collection" in runs[0]["error"]


def test_the_explanation_names_an_mcp_server_holder_and_how_to_get_in(monkeypatch, capsys):
    monkeypatch.setattr(common, "find_collection_holders", lambda path: [
        {"pid": 4242, "command": "python /home/u/.local/bin/griot mcp", "started": "2026-09-24 01:00", "role": "mcp"}])
    monkeypatch.setattr(importlib.import_module("griot.index_code"), "main", _broken_main(_busy()))

    cli.main(["index", "code"])

    err = capsys.readouterr().err
    assert "PID 4242" in err and "griot mcp" in err and "2026-09-24 01:00" in err
    assert "without a tool call" in err and "griot_index_repo" in err


def test_the_explanation_tells_you_to_wait_for_an_indexing_holder(monkeypatch, capsys):
    monkeypatch.setattr(common, "find_collection_holders", lambda path: [
        {"pid": 77, "command": "python -m griot.cli index all --repo x", "started": None, "role": "index"}])
    monkeypatch.setattr(importlib.import_module("griot.index_code"), "main", _broken_main(_busy()))

    cli.main(["index", "code"])

    err = capsys.readouterr().err
    assert "PID 77" in err and "finish" in err
    assert "griot_index_repo" not in err


def test_an_indexing_holder_is_not_mistaken_for_an_mcp_server_by_a_repo_name(monkeypatch, capsys):
    monkeypatch.setattr(common, "find_collection_holders", lambda path: [
        {"pid": 78, "command": "python /x/griot index all --repo mcp-gateway", "started": None, "role": "index"}])
    monkeypatch.setattr(importlib.import_module("griot.index_code"), "main", _broken_main(_busy()))

    cli.main(["index", "code"])

    err = capsys.readouterr().err
    assert "finish" in err and "griot_index_repo" not in err


def test_an_mcp_server_among_several_holders_still_gets_the_mcp_advice(monkeypatch, capsys):
    monkeypatch.setattr(common, "find_collection_holders", lambda path: [
        {"pid": 5, "command": "Python", "started": None, "role": None},
        {"pid": 6, "command": "python -m griot.mcp_server", "started": None, "role": "mcp"}])
    monkeypatch.setattr(importlib.import_module("griot.index_code"), "main", _broken_main(_busy()))

    cli.main(["index", "code"])

    err = capsys.readouterr().err
    assert "PID 5" in err and "PID 6" in err and "griot_index_repo" in err


def test_the_explanation_still_helps_when_the_holder_cannot_be_found(monkeypatch, capsys):
    monkeypatch.setattr(common, "find_collection_holders", lambda path: [])
    monkeypatch.setattr(importlib.import_module("griot.index_code"), "main", _broken_main(_busy()))

    cli.main(["index", "code"])

    err = capsys.readouterr().err
    assert "PID" not in err and "run the command again" in err


def test_a_real_held_collection_is_explained_end_to_end(tmp_path):
    """The real thing: a second process holds the collection, a real CLI
    process runs a dry-run against it. Unit tests here mock the holder and the
    exception, so nothing else proves the pieces agree with the real engine."""
    import os
    import shutil
    import subprocess
    import sys
    import threading

    env = {**os.environ, "GRIOT_CONFIG_DIR": str(tmp_path / "config"), "GRIOT_DATA_DIR": str(tmp_path / "data"),
           "GRIOT_EMBED_PROFILE": "jina-code", "GRIOT_MCP_CONCURRENCY_MODE": "single"}
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)

    holder = subprocess.Popen(
        [sys.executable, "-c",
         "from griot import common; import time; common.get_client(); print('ready', flush=True); time.sleep(120)"],
        env=env, stdout=subprocess.PIPE, text=True)
    watchdog = threading.Timer(90, holder.kill)  # a holder that never says "ready" must fail this test, not hang CI
    watchdog.start()
    try:
        assert holder.stdout.readline().strip() == "ready"
        proc = subprocess.run(
            [sys.executable, "-m", "griot.cli", "index", "commits", "--path", str(repo), "--dry-run"],
            env=env, capture_output=True, text=True, timeout=120)
    finally:
        watchdog.cancel()
        holder.kill()
        holder.wait()

    assert proc.returncode == 1
    assert "holds the collection" in proc.stderr
    if shutil.which("lsof"):  # naming the holder is best-effort and needs lsof; everything else must hold without it
        assert f"PID {holder.pid}" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not (tmp_path / "data" / "griot" / "logs").exists() or logdb.read_since(
        tmp_path / "data" / "griot" / "logs", "runs", days=1) == []
