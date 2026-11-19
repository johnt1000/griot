"""repos.json and the golden set, when they are there and cannot be used,
are said with one sentence on every surface that reads them: the file, the
classified cause, and the command last. Never the parser's text, never a
traceback, never "nothing registered" for a file that could not be read."""

import json
import os
import subprocess
import sys

import pytest
from mcp.client.client import Client

from griot import common, doctor, golden_set, harnesses, index_code, jobs, mcp_server, quality_check, repos, stats


def _run(tmp_path, *args):
    """The real CLI in a subprocess with no terminal, with the directories
    the test process uses (conftest points both at tmp_path)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "XDG_", "NO_COLOR"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               GRIOT_UPDATE_CHECK="false")
    return subprocess.run([sys.executable, "-m", "griot.cli", *args], env=env, capture_output=True, text=True,
                          timeout=180, stdin=subprocess.DEVNULL, start_new_session=True)


def _repos_sentence(cause):
    return (f"{common.REPOS_JSON_PATH} could not be read ({cause}). Fix it, or delete it and register again:\n"
            f"  griot repos add <path>")


def _golden_sentence(cause):
    return (f"{common.GOLDEN_SET_PATH} could not be read ({cause}). Fix it, or delete it and curate again:\n"
            f"  griot golden-set suggest <repo-path>")


# Each way repos.json can be there and unusable, with the cause every surface says.
def _corrupted(path):
    path.write_text("[not json")


def _wrong_shape(path):
    path.write_text(json.dumps({"a": 1}))


def _no_permission(path):
    path.write_text("[]")
    path.chmod(0)


def _a_directory(path):
    path.mkdir()


UNUSABLE = [
    pytest.param(_corrupted, "corrupted JSON", id="corrupted"),
    pytest.param(_wrong_shape, "not a list of directory paths", id="wrong-shape"),
    pytest.param(_no_permission, "permission denied", id="permission"),
    pytest.param(_a_directory, "it is a directory, not a file", id="directory"),
]


@pytest.fixture
def unusable_repos(request):
    make, cause = request.param
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    make(common.REPOS_JSON_PATH)
    yield cause
    if common.REPOS_JSON_PATH.is_file():
        common.REPOS_JSON_PATH.chmod(0o600)


def _repos_params():
    return [pytest.param(p.values, id=p.id) for p in UNUSABLE]


# --- griot repos add / remove ------------------------------------------------------------------------------

@pytest.mark.parametrize("unusable_repos", _repos_params(), indirect=True)
@pytest.mark.parametrize("action", ["add", "remove"])
def test_repos_add_and_remove_say_the_one_sentence_for_an_unusable_file(tmp_path, unusable_repos, action):
    target = tmp_path / "project"
    target.mkdir()
    argv = ["repos", action, str(target)] + (["--yes"] if action == "remove" else [])

    done = _run(tmp_path, *argv)

    assert done.returncode == 1
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    # Checked before anyone is asked: no terminal refusal, the file first.
    assert done.stderr == f"Error: {_repos_sentence(unusable_repos)}\n"


@pytest.mark.parametrize("action", ["add", "remove"])
def test_at_a_terminal_nobody_is_asked_about_a_file_that_cannot_be_read(tmp_path, action):
    """With a real pseudo-terminal, so common.is_interactive() is true
    without being faked: the sentence, and no question before it."""
    import pty
    import select
    target = tmp_path / "project"
    target.mkdir()
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    _corrupted(common.REPOS_JSON_PATH)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "XDG_", "NO_COLOR"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               GRIOT_UPDATE_CHECK="false")
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen([sys.executable, "-m", "griot.cli", "repos", action, str(target)],
                                stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
        os.close(slave)
        output = b""
        while True:
            ready, _, _ = select.select([master], [], [], 60)
            if not ready:
                proc.kill()
                pytest.fail(f"no output for 60 s; so far: {output!r}")
            try:
                chunk = os.read(master, 4096)
            except OSError:  # the child closed its end
                break
            if not chunk:
                break
            output += chunk
        code = proc.wait(timeout=60)
    finally:
        os.close(master)
    text = output.decode().replace("\r\n", "\n")
    assert code == 1
    assert text == f"Error: {_repos_sentence('corrupted JSON')}\n"


def test_repos_remove_with_nothing_registered_says_what_repos_list_says(tmp_path):
    target = tmp_path / "project"
    target.mkdir()

    done = _run(tmp_path, "repos", "remove", str(target), "--yes")

    # A conflict with the state, not a malformed command.
    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == f"Error: No repository registered in {common.REPOS_JSON_PATH}.\n  griot repos add <path>\n"
    listed = _run(tmp_path, "repos", "list")
    assert listed.stdout == f"No repository registered in {common.REPOS_JSON_PATH}.\n  griot repos add <path>\n"


def test_cmd_add_and_cmd_remove_say_the_sentence_when_the_file_breaks_after_the_check(tmp_path, capsys):
    """The file can change between check_add and the write: the same sentence."""
    target = tmp_path / "project"
    target.mkdir()
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    _corrupted(common.REPOS_JSON_PATH)
    assert repos.cmd_add(str(target)) == 1
    assert capsys.readouterr().err == f"Error: {_repos_sentence('corrupted JSON')}\n"
    assert repos.cmd_remove(str(target)) == 1
    assert capsys.readouterr().err == f"Error: {_repos_sentence('corrupted JSON')}\n"


def test_repos_add_refusing_an_already_registered_path_is_unchanged(tmp_path, capsys):
    target = tmp_path / "project"
    target.mkdir()
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(target.resolve())]))
    assert repos.main(["add", str(target)]) == 1
    assert capsys.readouterr().err == (f"Error: '{target.resolve()}' is already in {common.REPOS_JSON_PATH}.\n"
                                       f"  griot repos list\n")


# --- MCP griot_repos_add / griot_repos_remove ---------------------------------------------------------------

async def _never_asked(ctx, params):
    raise AssertionError("a person was asked about a call that was going to be refused")


@pytest.mark.anyio
@pytest.mark.parametrize("unusable_repos", _repos_params(), indirect=True)
@pytest.mark.parametrize("tool", ["griot_repos_add", "griot_repos_remove"])
async def test_mcp_repos_add_and_remove_raise_the_sentence_without_asking(tmp_path, unusable_repos, tool):
    target = tmp_path / "project"
    target.mkdir()
    async with Client(mcp_server.mcp, elicitation_callback=_never_asked) as client:
        result = await client.call_tool(tool, {"path": str(target), "confirm": True})
    assert result.is_error
    text = result.content[0].text
    assert _repos_sentence(unusable_repos) in text
    assert "Expecting" not in text and "Errno" not in text


@pytest.mark.anyio
async def test_mcp_repos_add_refuses_an_already_registered_path_without_asking(tmp_path):
    target = tmp_path / "project"
    target.mkdir()
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(target.resolve())]))
    async with Client(mcp_server.mcp, elicitation_callback=_never_asked) as client:
        result = await client.call_tool("griot_repos_add", {"path": str(target)})
    assert result.is_error is False
    assert result.structured_content == {"changed": False,
                                         "message": f"'{target.resolve()}' is already in {common.REPOS_JSON_PATH}."
                                                    f"\n  griot repos list"}


@pytest.mark.anyio
async def test_mcp_repos_remove_refuses_an_unregistered_path_without_asking(tmp_path):
    target = tmp_path / "project"
    target.mkdir()
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(tmp_path / "other")]))
    async with Client(mcp_server.mcp, elicitation_callback=_never_asked) as client:
        result = await client.call_tool("griot_repos_remove", {"path": str(target), "confirm": True})
    assert result.structured_content["changed"] is False
    assert "not found in" in result.structured_content["message"]


@pytest.mark.anyio
async def test_mcp_repos_remove_of_an_unregistered_path_is_refused_before_offering_confirm(tmp_path):
    """Asking for confirm=true about a call that will be refused trains the
    agent to retry for nothing: the refusal comes first."""
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(tmp_path / "other")]))
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_repos_remove", {"path": str(tmp_path)})
    assert result.structured_content == {
        "changed": False,
        "message": f"'{tmp_path.resolve()}' not found in {common.REPOS_JSON_PATH}.\n  griot repos list"}


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["griot_repos_add", "griot_repos_remove"])
async def test_mcp_repos_refusal_quoting_the_agent_s_path_is_printable(tmp_path, tool):
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(tmp_path / "other")]))
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool(tool, {"path": str(tmp_path / "a\u2028b\x1b[31m"), "confirm": True})
    message = result.structured_content["message"]
    assert result.structured_content["changed"] is False
    # The agent's part is one printable line; the line after it is griot's
    # own command, as the CLI ends the same refusal.
    reason, command = message.split("\n")
    assert reason.isprintable(), repr(message)
    # The path does not exist: add answers with how to add a directory,
    # remove (the path is not in the list) with the list, as the CLI does.
    assert command == {"griot_repos_add": "  griot repos add <directory>",
                       "griot_repos_remove": "  griot repos list"}[tool]


# --- the indexers, doctor, stats, quality-check, the MCP allowlist, the install summary ----------------------

@pytest.mark.parametrize("unusable_repos", _repos_params(), indirect=True)
def test_an_indexer_says_the_sentence_for_an_unusable_file(unusable_repos, capsys):
    assert index_code.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"Error: {_repos_sentence(unusable_repos)}\n"


def test_an_indexer_with_nothing_registered_says_what_repos_list_says(capsys):
    assert index_code.main([]) == 1
    assert capsys.readouterr().err == (f"Error: No repository registered in {common.REPOS_JSON_PATH}.\n"
                                       f"  griot repos add <path>\n")


@pytest.mark.parametrize("module", ["index_code", "index_commits", "index_tags", "index_branches", "index_platform"])
def test_the_five_indexers_read_repos_json_through_one_helper(module, monkeypatch, capsys):
    import importlib
    mod = importlib.import_module(f"griot.{module}")
    monkeypatch.setattr(common, "load_repos_or_say", lambda: None)
    assert mod.main([]) == 1


@pytest.mark.parametrize("unusable_repos", _repos_params(), indirect=True)
def test_doctor_detail_and_fix_are_the_two_parts_of_the_sentence(unusable_repos):
    check = doctor.check_repositories(common)
    assert check["status"] == doctor.FAIL
    assert f"{check['detail']}:\n  {check['fix']}" == _repos_sentence(unusable_repos)


@pytest.mark.parametrize("unusable_repos", _repos_params(), indirect=True)
def test_stats_lists_an_unusable_repos_json_with_the_sentence(unusable_repos):
    state = stats.load_state()
    assert state["repos_error"] == unusable_repos
    attention = stats.compute_stats([], [], {"points_count": 1, "embed_profile": "any"}, state=state)["attention"]
    assert _repos_sentence(unusable_repos).replace(":\n  ", ": ") in attention


def test_stats_says_nothing_about_repos_json_when_it_is_fine_or_missing():
    state = stats.load_state()
    assert state["repos_error"] is None
    attention = stats.compute_stats([], [], {"points_count": 1, "embed_profile": "any"}, state=state)["attention"]
    assert not [line for line in attention if "repos.json" in line]


@pytest.mark.parametrize("unusable_repos", _repos_params(), indirect=True)
def test_quality_check_from_history_says_the_sentence(unusable_repos):
    with pytest.raises(quality_check._NotMeasurable) as raised:
        quality_check._history_targets(None)
    assert str(raised.value) == _repos_sentence(unusable_repos)


@pytest.mark.parametrize("unusable_repos", _repos_params(), indirect=True)
def test_the_mcp_allowlist_says_the_file_could_not_be_read_not_that_the_path_is_outside(tmp_path, unusable_repos,
                                                                                         monkeypatch):
    monkeypatch.delenv("GRIOT_MCP_INDEX_ROOTS", raising=False)
    assert jobs.repository_path_refusal(str(tmp_path)) == _repos_sentence(unusable_repos)


def test_the_mcp_allowlist_refusal_ends_with_the_command(tmp_path, monkeypatch):
    monkeypatch.delenv("GRIOT_MCP_INDEX_ROOTS", raising=False)
    assert jobs.repository_path_refusal(str(tmp_path)) == (
        "path outside the allowed repo list. Register it, or add a parent directory to GRIOT_MCP_INDEX_ROOTS:\n"
        "  griot repos add <path>")
    assert jobs.repository_path_refusal(str(tmp_path), allow_env_roots=False) == (
        "path outside the allowed repo list. Register it first:\n  griot repos add <path>")


@pytest.mark.parametrize("unusable_repos", _repos_params(), indirect=True)
def test_the_install_summary_does_not_call_an_unusable_file_nothing_registered(unusable_repos):
    lines = harnesses._registration_lines(common)
    assert lines == [f"  {_repos_sentence(unusable_repos)}"]


def test_the_install_summary_with_nothing_registered_ends_with_the_command():
    assert harnesses._registration_lines(common) == [
        f"  No repository registered in {common.REPOS_JSON_PATH}.\n  griot repos add <path>"]


def test_the_stale_points_note_ends_with_the_sentence_once_per_run(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(common, "_said_stale_notes", set())
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    _corrupted(common.REPOS_JSON_PATH)
    repo = tmp_path / "alpha"
    repo.mkdir()
    documents = [{"id": "alpha:code:a.py:0", "metadata": {"repo": "alpha"}}]
    for source in ("code", "commit"):
        common.prune_orphans(documents, source_type=source, repo_paths=[str(repo)])
    err = capsys.readouterr().err
    assert err == f"Stale points of alpha (code) are not removed: {_repos_sentence('corrupted JSON')}\n"


def test_griot_index_all_says_a_stale_points_note_once_and_a_lone_source_each_time(monkeypatch):
    from griot import cli
    seen = []
    monkeypatch.setattr(cli, "_run_index_source", lambda source, rest: seen.append(common._said_stale_notes) or 0)
    assert cli.main(["index", "all", "--sources", "code,commits"]) == 0
    assert seen == [set(), set()] and seen[0] is seen[1], "one set for the whole run"
    assert common._said_stale_notes is None, "outside a run, each note is said"


# --- the golden set ----------------------------------------------------------------------------------------

GOLDEN_SHAPES = [
    pytest.param({"a": 1}, id="dict"),
    pytest.param(["q"], id="not-objects"),
]
SHAPE = "not a list of cases"


def _write_golden(content):
    common.secure_mkdir(common.GOLDEN_SET_PATH.parent)
    common.GOLDEN_SET_PATH.write_text(json.dumps(content))


@pytest.mark.parametrize("content", GOLDEN_SHAPES)
@pytest.mark.parametrize("argv", [["golden-set", "remove", "1", "--yes"],
                                  ["golden-set", "list"], ["golden-set", "list", "--json"]])
def test_the_golden_set_cli_says_the_sentence_for_a_file_of_another_shape(tmp_path, content, argv):
    _write_golden(content)
    done = _run(tmp_path, *argv)
    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == f"Error: {_golden_sentence(SHAPE)}\n"


@pytest.mark.parametrize("content", GOLDEN_SHAPES)
def test_golden_set_add_and_suggest_refuse_a_file_of_another_shape_before_searching(content, capsys, monkeypatch,
                                                                                    tmp_path):
    _write_golden(content)
    monkeypatch.setattr(common, "is_interactive", lambda: True)
    monkeypatch.setattr(common, "search", lambda *a, **k: pytest.fail("searched before the file was checked"))
    monkeypatch.setattr(golden_set, "suggest_candidates",
                        lambda *a, **k: pytest.fail("scanned the history before the file was checked"))
    (tmp_path / "r" / ".git").mkdir(parents=True)
    assert golden_set.cmd_add("q") == 1
    assert capsys.readouterr() == ("", f"Error: {_golden_sentence(SHAPE)}\n")
    assert golden_set.cmd_suggest(str(tmp_path / "r")) == 1
    assert capsys.readouterr() == ("", f"Error: {_golden_sentence(SHAPE)}\n")
    assert golden_set.cmd_review() == 1
    assert capsys.readouterr() == ("", f"Error: {_golden_sentence(SHAPE)}\n")


@pytest.mark.anyio
@pytest.mark.parametrize("content", GOLDEN_SHAPES)
@pytest.mark.parametrize("tool,arguments", [
    ("griot_golden_set_list", {}),
    ("griot_golden_set_add", {"query": "q", "must_include": [{"repo": "alpha"}], "confirm": True}),
    ("griot_golden_set_remove", {"index": 1, "confirm": True}),
])
async def test_the_golden_set_tools_raise_the_sentence_for_a_file_of_another_shape(content, tool, arguments):
    _write_golden(content)
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error
    assert _golden_sentence(SHAPE) in result.content[0].text


@pytest.mark.parametrize("content", GOLDEN_SHAPES)
def test_stats_says_the_sentence_for_a_golden_set_of_another_shape(content):
    _write_golden(content)
    state = stats.load_state()
    assert state["golden_set_error"] == SHAPE


@pytest.mark.anyio
@pytest.mark.parametrize("tool,arguments", [
    ("griot_golden_set_add", {"query": "q", "must_include": [{"repo": "alpha"}], "confirm": True}),
    ("griot_golden_set_remove", {"index": 1, "confirm": True}),
])
async def test_the_golden_set_tools_classify_a_permission_error(tool, arguments):
    _write_golden([])
    common.GOLDEN_SET_PATH.chmod(0)
    try:
        async with Client(mcp_server.mcp) as client:
            result = await client.call_tool(tool, arguments)
    finally:
        common.GOLDEN_SET_PATH.chmod(0o600)
    assert result.is_error
    text = result.content[0].text
    assert _golden_sentence("permission denied") in text and "Errno" not in text


# --- a golden set that cannot be run: the case at fault, the same on CLI and MCP ---------------------------

def _unrunnable(number):
    return (f"{common.GOLDEN_SET_PATH} cannot be run: case {number} has no `query` text. Fix that case, or "
            f"remove it:\n  griot golden-set remove {number}")


RUNNABLE = {"query": "fine", "must_include": [{"repo": "alpha"}]}


def test_check_cases_names_the_case_it_refuses():
    with pytest.raises(golden_set.CaseError) as raised:
        golden_set.check_cases([RUNNABLE, {"query": " ", "must_include": [{"repo": "alpha"}]}])
    assert raised.value.number == 2
    assert golden_set.unrunnable_sentence(raised.value) == _unrunnable(2)


def test_check_cases_calls_a_file_that_is_not_a_list_of_another_shape():
    with pytest.raises(common.WrongShape):
        golden_set.check_cases({"query": "x"})


def test_mcp_quality_check_names_the_case_not_corrupted_json():
    _write_golden([RUNNABLE, {"must_include": [{"repo": "alpha"}]}])
    with pytest.raises(mcp_server.UnreadableFile) as raised:
        mcp_server._curated_cases_to_run()
    assert str(raised.value) == _unrunnable(2)


def test_mcp_quality_check_says_the_unreadable_sentence_for_a_corrupted_file():
    common.secure_mkdir(common.GOLDEN_SET_PATH.parent)
    common.GOLDEN_SET_PATH.write_text("{broken")
    with pytest.raises(mcp_server.UnreadableFile) as raised:
        mcp_server._curated_cases_to_run()
    assert str(raised.value) == _golden_sentence("corrupted JSON")


@pytest.mark.parametrize("as_json", [False, True])
def test_quality_check_cli_says_the_case_on_stderr(monkeypatch, capsys, as_json):
    _write_golden([RUNNABLE, {"must_include": [{"repo": "alpha"}]}])
    monkeypatch.setattr(common, "collection_exists", lambda name: True)
    monkeypatch.setattr(quality_check, "run_self_check",
                        lambda *a, **k: pytest.fail("the self-check ran before the file was checked"))
    with pytest.raises(SystemExit) as raised:
        quality_check.main(["--json"] if as_json else [])
    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"Error: {_unrunnable(2)}\n"


@pytest.mark.parametrize("as_json", [False, True])
def test_quality_check_cli_says_the_unreadable_sentence_on_stderr(monkeypatch, capsys, as_json):
    common.secure_mkdir(common.GOLDEN_SET_PATH.parent)
    common.GOLDEN_SET_PATH.write_text("{broken")
    monkeypatch.setattr(common, "collection_exists", lambda name: True)
    monkeypatch.setattr(quality_check, "run_self_check",
                        lambda *a, **k: pytest.fail("the self-check ran before the file was checked"))
    with pytest.raises(SystemExit) as raised:
        quality_check.main(["--json"] if as_json else [])
    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"Error: {_golden_sentence('corrupted JSON')}\n"


def test_a_reason_that_ends_with_a_period_is_not_followed_by_another():
    with pytest.raises(golden_set.CaseError) as raised:
        golden_set.check_cases([{**RUNNABLE, "mode": "bogus"}])
    assert golden_set.unrunnable_sentence(raised.value) == (
        f"{common.GOLDEN_SET_PATH} cannot be run: case 1: `mode` must be one of vector, keyword, hybrid "
        f"(got 'bogus'). Fix that case, or remove it:\n  griot golden-set remove 1")


# --- one case at fault in a list of cases: that case is named, and it can be removed alone ----------------

BROKEN_CASES = [
    pytest.param({"query": "y"}, id="no-must-include"),
    pytest.param({"query": "y", "must_include": "oops"}, id="a-string"),
    pytest.param({"query": "y", "must_include": None}, id="null"),
    pytest.param({"query": "y", "must_include": 5}, id="a-number"),  # not iterable: no TypeError either
    pytest.param({"query": "y", "must_include": [1]}, id="not-objects"),
    pytest.param({"query": "y", "must_include": []}, id="empty"),
]


def _needs_must_include(number):
    # One wording for the one fact, whatever form the missing list takes.
    return (f"{common.GOLDEN_SET_PATH} cannot be run: case {number} needs `must_include`: a list of at least one "
            f"object. Fix that case, or remove it:\n  griot golden-set remove {number}")


@pytest.mark.parametrize("broken", BROKEN_CASES)
def test_a_case_without_a_must_include_list_is_named_not_the_whole_file(broken):
    with pytest.raises(golden_set.CaseError) as raised:
        golden_set.check_cases([RUNNABLE, broken])
    assert raised.value.number == 2
    assert golden_set.unrunnable_sentence(raised.value) == _needs_must_include(2)


@pytest.mark.parametrize("broken", BROKEN_CASES)
def test_the_quality_check_gate_names_the_case_on_both_surfaces(broken, monkeypatch, capsys):
    _write_golden([RUNNABLE, broken])
    with pytest.raises(mcp_server.UnreadableFile) as raised:
        mcp_server._curated_cases_to_run()
    assert str(raised.value) == _needs_must_include(2)
    monkeypatch.setattr(common, "collection_exists", lambda name: True)
    monkeypatch.setattr(quality_check, "run_self_check",
                        lambda *a, **k: pytest.fail("the self-check ran before the file was checked"))
    with pytest.raises(SystemExit):
        quality_check.main([])
    assert capsys.readouterr() == ("", f"Error: {_needs_must_include(2)}\n")


@pytest.mark.parametrize("broken", BROKEN_CASES)
def test_a_file_with_one_broken_case_still_lists_and_that_case_can_be_removed(tmp_path, broken):
    _write_golden([RUNNABLE, broken])

    listed = _run(tmp_path, "golden-set", "list")
    assert (listed.returncode, listed.stderr) == (0, "")
    assert "[1] fine" in listed.stdout and "[2] y" in listed.stdout
    as_json = _run(tmp_path, "golden-set", "list", "--json")
    assert (as_json.returncode, as_json.stderr) == (0, "")
    assert json.loads(as_json.stdout)["count"] == 2
    assert stats.load_state()["golden_set_error"] is None
    assert stats.load_state()["golden_set"]["cases"] == 2

    removed = _run(tmp_path, "golden-set", "remove", "2", "--yes")
    assert (removed.returncode, removed.stderr) == (0, "")
    assert json.loads(common.GOLDEN_SET_PATH.read_text()) == [RUNNABLE]


@pytest.mark.anyio
@pytest.mark.parametrize("broken", BROKEN_CASES)
async def test_the_mcp_tools_list_a_file_with_one_broken_case_and_remove_that_case(broken):
    _write_golden([RUNNABLE, broken])
    async with Client(mcp_server.mcp) as client:
        listed = await client.call_tool("griot_golden_set_list", {})
        removed = await client.call_tool("griot_golden_set_remove", {"index": 2, "confirm": True})
    assert not listed.is_error and listed.structured_content["count"] == 2
    assert not removed.is_error and removed.structured_content["changed"] is True
    assert json.loads(common.GOLDEN_SET_PATH.read_text()) == [RUNNABLE]


# --- from-history: an error goes to stderr in either mode -------------------------------------------------

@pytest.mark.parametrize("unusable_repos", _repos_params(), indirect=True)
@pytest.mark.parametrize("as_json", [False, True])
def test_from_history_says_an_unusable_repos_json_on_stderr(unusable_repos, as_json, capsys):
    import argparse
    with pytest.raises(SystemExit) as raised:
        quality_check.run_from_history(argparse.Namespace(mode="vector", limit=None, json=as_json, repo=None))
    assert raised.value.code == 1
    assert capsys.readouterr() == ("", f"Error: {_repos_sentence(unusable_repos)}\n")


def test_from_history_with_nothing_registered_says_what_repos_list_says(capsys):
    with pytest.raises(quality_check._NotMeasurable) as raised:
        quality_check._history_targets(None)
    assert str(raised.value) == common.no_repository_sentence()


def test_from_history_ends_a_name_it_cannot_find_with_a_command_that_pastes(tmp_path):
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(tmp_path / "alpha")]))
    with pytest.raises(quality_check._NotMeasurable) as raised:
        quality_check._history_targets("nope")
    assert str(raised.value).splitlines()[-1] == "  griot repos list"


# --- griot_repos_remove: a path not in the list ends with what to do, as the CLI does ----------------------

@pytest.mark.anyio
async def test_mcp_repos_remove_of_a_path_not_registered_ends_with_the_list_command(tmp_path, capsys):
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(tmp_path / "alpha")]))
    other = tmp_path / "beta"
    other.mkdir()
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_repos_remove", {"path": str(other), "confirm": True})
    assert result.structured_content == {
        "changed": False,
        "message": f"'{other.resolve()}' not found in {common.REPOS_JSON_PATH}.\n  griot repos list"}
    assert repos.main(["remove", str(other), "--yes"]) == 2
    assert capsys.readouterr().err == f"Error: {result.structured_content['message']}\n"


# --- griot_golden_set_add / remove: nobody is asked about a call that will be refused ----------------------
#
# What is cheap to check (the file can be read, the case exists, the new case
# is one that could fail) is checked BEFORE a person is asked, as the CLI and
# the repos tools do: the answer to a question about a call that is refused
# anyway cannot change the outcome.

ONE_CASE = [{"query": "how is the retry policy chosen", "must_include": [{"repo": "alpha", "file_path": "a.py"}],
             "limit": 5}]
OUT_OF_RANGE = "index 99 out of range (1-1).\n  griot golden-set list"


@pytest.mark.anyio
@pytest.mark.parametrize("content", GOLDEN_SHAPES)
@pytest.mark.parametrize("tool,arguments", [
    ("griot_golden_set_add", {"query": "retry policy", "must_include": [{"repo": "alpha"}]}),
    ("griot_golden_set_remove", {"index": 1}),
])
async def test_the_golden_set_write_tools_ask_nobody_about_an_unreadable_file(content, tool, arguments):
    _write_golden(content)
    async with Client(mcp_server.mcp, elicitation_callback=_never_asked) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error
    assert _golden_sentence(SHAPE) in result.content[0].text
    assert json.loads(common.GOLDEN_SET_PATH.read_text()) == content


@pytest.mark.anyio
@pytest.mark.parametrize("ask", [True, False], ids=["client-can-ask", "client-cannot-ask"])
async def test_mcp_golden_set_remove_out_of_range_is_refused_before_asking_and_says_what_to_run(ask):
    _write_golden(ONE_CASE)
    callback = {"elicitation_callback": _never_asked} if ask else {}
    async with Client(mcp_server.mcp, **callback) as client:
        result = await client.call_tool("griot_golden_set_remove", {"index": 99})
    # Refused for what it is, not offered confirm=true for a call that would fail.
    assert result.structured_content == {"changed": False, "message": OUT_OF_RANGE}
    assert json.loads(common.GOLDEN_SET_PATH.read_text()) == ONE_CASE


def test_cli_golden_set_remove_out_of_range_says_the_same_as_the_tool(capsys):
    _write_golden(ONE_CASE)
    assert golden_set.main(["remove", "99", "--yes"]) == 1
    assert capsys.readouterr() == ("", f"Error: {OUT_OF_RANGE}\n")


@pytest.mark.anyio
@pytest.mark.parametrize("ask", [True, False], ids=["client-can-ask", "client-cannot-ask"])
@pytest.mark.parametrize("must_include,reason", [
    ([], "must_include must not be empty."),
    ([{"repo": None}], "each must_include entry must constrain at least one field"),
])
async def test_mcp_golden_set_add_refuses_a_case_that_could_never_fail_before_asking(ask, must_include, reason):
    _write_golden(ONE_CASE)
    callback = {"elicitation_callback": _never_asked} if ask else {}
    async with Client(mcp_server.mcp, **callback) as client:
        result = await client.call_tool("griot_golden_set_add",
                                        {"query": "retry policy", "must_include": must_include})
    assert result.structured_content["changed"] is False
    assert result.structured_content["message"].startswith(reason)
    assert json.loads(common.GOLDEN_SET_PATH.read_text()) == ONE_CASE
