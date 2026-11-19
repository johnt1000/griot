"""`--json` on the listings an agent or a script reads most: `griot repos
list`, `griot profiles list`, `griot golden-set list` and `griot search`.

One JSON document on stdout and nothing else there, with a warning active;
the same shape as the MCP tool that lists the same thing, built by one
function both call; a file that cannot be read is one sentence on every
surface, never a traceback."""

import hashlib
import json
import os
import random
import subprocess
import sys

import pytest
from mcp.client.client import Client

import griot

from griot import cli, common, doctor, golden_set, harnesses, mcp_server, repos, search_result, stats

OLD_VARIABLE = "RAG_EMBED_PROFILE"
WARNING_ACTIVE = {OLD_VARIABLE: "jina-code"}  # common warns about it on stderr at import
WARNING = f"Warning: {OLD_VARIABLE} was renamed"


def _run(tmp_path, *args, extra_env=None):
    """The real CLI in a subprocess, with the directories the test process
    uses (conftest points both at tmp_path), never the user's."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "XDG_", "NO_COLOR"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               GRIOT_UPDATE_CHECK="false")
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "griot.cli", *args], env=env, capture_output=True, text=True,
                          timeout=180, stdin=subprocess.DEVNULL, start_new_session=True)


def _json(done):
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)  # the whole of stdout: nothing else may be in it


async def _tool(name, arguments=None):
    async with Client(mcp_server.mcp) as client:
        return await client.call_tool(name, arguments or {})


def _write_repos(paths):
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps(paths))


# --- one help for the four flags ---------------------------------------------------------------------------

def _json_help(argv, capsys):
    with pytest.raises(SystemExit):
        cli.main([*argv, "--help"])
    text = " ".join(capsys.readouterr().out.split())
    start = text.index("--json ") + len("--json ")
    return text[start:text.index(")", start) + 1]


@pytest.mark.parametrize("argv,guide", [
    (["repos", "list"], "docs/getting-started.md"),
    (["profiles", "list"], "docs/configuration.md"),
    (["golden-set", "list"], "docs/quality.md"),
    (["search", "q"], "docs/search.md"),
])
def test_the_four_flags_say_the_same_thing_and_name_their_guide(argv, guide, capsys):
    assert _json_help(argv, capsys) == (f"One JSON document on stdout instead of the list; notices and errors "
                                        f"stay on stderr (keys: {guide})")


# --- griot repos list --json -------------------------------------------------------------------------------

def test_repos_list_json_is_the_mcp_shape_with_a_warning_active(tmp_path):
    here = tmp_path / "here"
    (here / ".git").mkdir(parents=True)
    gone = tmp_path / "gone"
    _write_repos([str(here), str(gone)])

    done = _run(tmp_path, "repos", "list", "--json", extra_env=WARNING_ACTIVE)

    assert WARNING in done.stderr
    assert _json(done) == {"repos": [
        {"name": "here", "path": str(here), "exists": True, "is_git": True},
        {"name": "gone", "path": str(gone), "exists": False, "is_git": False},
    ], "count": 2}


@pytest.mark.anyio
async def test_repos_list_json_and_griot_repos_list_are_built_by_one_function(tmp_path, monkeypatch):
    _write_repos([str(tmp_path)])
    marker = {"repos": [{"name": "x", "path": "/Users/you/x", "exists": True, "is_git": False}], "count": 1}
    monkeypatch.setattr(repos, "list_result", lambda: marker)

    result = await _tool("griot_repos_list")

    assert result.structured_content == marker


def test_repos_list_json_empty_is_an_empty_array(tmp_path):
    assert _json(_run(tmp_path, "repos", "list", "--json")) == {"repos": [], "count": 0}


def test_repos_list_text_empty_ends_with_the_command_that_registers_one(tmp_path):
    done = _run(tmp_path, "repos", "list")

    assert done.returncode == 0
    assert done.stdout.rstrip().splitlines()[-1] == "  griot repos add <path>"
    assert "No repository registered" in done.stdout


def test_repos_list_text_names_a_gone_path_as_doctor_does_and_ends_with_its_fix(tmp_path):
    gone = tmp_path / "gone"
    _write_repos([str(gone)])

    done = _run(tmp_path, "repos", "list")

    assert done.returncode == 0
    assert f"registered but no longer on disk  {gone}" in done.stdout
    assert done.stdout.rstrip().splitlines()[-1] == f"  griot repos remove {gone}"
    check = doctor.check_repositories(common)
    assert check["detail"] == f"registered but no longer on disk: {gone}"
    assert check["fix"] == f"griot repos remove {gone}"


@pytest.mark.parametrize("argv", [["repos", "list"], ["repos", "list", "--json"]])
def test_an_unreadable_repos_json_is_one_sentence_exit_1_and_nothing_on_stdout(tmp_path, argv):
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text("[not json")

    done = _run(tmp_path, *argv)

    assert done.returncode == 1
    assert done.stdout == ""
    assert "Traceback" not in done.stderr and "JSONDecodeError" not in done.stderr
    assert done.stderr == (f"Error: {common.REPOS_JSON_PATH} could not be read (corrupted JSON). Fix it, or delete it "
                           f"and register again:\n  griot repos add <path>\n")


@pytest.mark.anyio
async def test_an_unreadable_repos_json_reads_the_same_in_mcp_and_doctor(tmp_path):
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text("[not json")
    sentence = (f"{common.REPOS_JSON_PATH} could not be read (corrupted JSON). Fix it, or delete it and register "
                f"again:\n  griot repos add <path>")

    result = await _tool("griot_repos_list")
    check = doctor.check_repositories(common)

    assert result.is_error
    assert sentence in result.content[0].text
    assert "missing" not in result.content[0].text
    # The two parts of the same sentence, word for word.
    assert f"{check['detail']}:\n  {check['fix']}" == sentence


def test_an_unreadable_cause_is_classified(tmp_path):
    assert common.unreadable_cause(json.JSONDecodeError("x", "doc", 0)) == "corrupted JSON"
    assert common.unreadable_cause(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")) == "corrupted JSON"
    assert common.unreadable_cause(PermissionError(13, "Permission denied")) == "permission denied"
    assert common.unreadable_cause(PermissionError("raised without errno")) == "permission denied"
    assert common.unreadable_cause(IsADirectoryError(21, "Is a directory")) == "it is a directory, not a file"
    assert common.unreadable_cause(OSError(5, "Input/output error")) == "input/output error"
    assert common.unreadable_cause(OSError("no certificate bundle at /Users/you/x")) == \
        "no certificate bundle at /Users/you/x"
    assert common.unreadable_cause(OSError()) == "it could not be opened"


def test_repos_list_json_incorrect_use_is_exit_2_with_nothing_on_stdout(tmp_path):
    done = _run(tmp_path, "repos", "list", "--json", "--bogus")

    assert done.returncode == 2
    assert done.stdout == ""


# --- griot golden-set list --json --------------------------------------------------------------------------

CASES = [
    {"query": "how is the retry policy chosen", "must_include": [{"repo": "alpha", "file_path": "a.py"}],
     "limit": 5},
    {"query": "where is the spend ceiling", "must_include": [{"repo": "alpha", "file_path": "b.py"}],
     "limit": 5, "mode": "keyword"},
]


def _write_cases(cases):
    common.secure_mkdir(common.GOLDEN_SET_PATH.parent)
    common.GOLDEN_SET_PATH.write_text(json.dumps(cases))


def test_golden_set_list_json_is_the_mcp_shape_with_a_warning_active(tmp_path):
    _write_cases(CASES)

    done = _run(tmp_path, "golden-set", "list", "--json", extra_env=WARNING_ACTIVE)

    assert WARNING in done.stderr
    assert _json(done) == {"note": golden_set.GOLDEN_SET_NOTE,
                           "cases": [{**CASES[0], "mode": "vector"}, CASES[1]], "count": 2}


@pytest.mark.anyio
async def test_golden_set_list_json_and_the_tool_give_the_same_document(tmp_path):
    _write_cases(CASES)

    result = await _tool("griot_golden_set_list")

    assert result.structured_content == golden_set.list_result()
    assert mcp_server.GOLDEN_SET_NOTE == golden_set.GOLDEN_SET_NOTE


def test_json_is_one_line_and_keeps_text_as_written(tmp_path):
    """The style `griot stats --json` and `griot config list --json` print:
    one line, not ASCII-escaped, so a query in another language stays
    readable to a script or a person piping it."""
    _write_cases([{"query": "como a política de repetição é escolhida", "must_include": [{"repo": "a"}],
                   "limit": 5}])

    done = _run(tmp_path, "golden-set", "list", "--json")

    assert done.returncode == 0
    assert done.stdout.count("\n") == 1
    assert "política de repetição" in done.stdout


def test_golden_set_list_json_empty_is_an_empty_array(tmp_path):
    assert _json(_run(tmp_path, "golden-set", "list", "--json")) == {
        "note": golden_set.GOLDEN_SET_NOTE, "cases": [], "count": 0}


def test_golden_set_list_text_empty_ends_with_the_command_that_curates(tmp_path, capsys):
    assert golden_set.main(["list"]) == 0

    out = capsys.readouterr().out
    assert out == f"No golden-set cases in {common.GOLDEN_SET_PATH}.\n  griot golden-set suggest <repo-path>\n"


def test_golden_set_list_text_shows_an_agent_written_query_as_printable(capsys):
    _write_cases([{"query": "evil\x1b[31m red‮", "must_include": [{"repo": "a"}], "limit": 5}])

    assert golden_set.main(["list"]) == 0

    out = capsys.readouterr().out
    assert "\x1b" not in out and "‮" not in out
    assert out == "  [1] evil?[31m red? (1 required result, vector search)\n"


@pytest.mark.parametrize("argv", [["golden-set", "list"], ["golden-set", "list", "--json"]])
def test_an_unreadable_golden_set_is_one_sentence_exit_1_and_nothing_on_stdout(tmp_path, argv):
    common.secure_mkdir(common.GOLDEN_SET_PATH.parent)
    common.GOLDEN_SET_PATH.write_text("{broken")

    done = _run(tmp_path, *argv)

    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == (f"Error: {common.GOLDEN_SET_PATH} could not be read (corrupted JSON). Fix it, or delete "
                           f"it and curate again:\n  griot golden-set suggest <repo-path>\n")


@pytest.mark.anyio
async def test_an_unreadable_golden_set_reads_the_same_in_mcp_and_stats(tmp_path):
    common.secure_mkdir(common.GOLDEN_SET_PATH.parent)
    common.GOLDEN_SET_PATH.write_text("{broken")
    sentence = (f"{common.GOLDEN_SET_PATH} could not be read (corrupted JSON). Fix it, or delete it and curate "
                f"again:\n  griot golden-set suggest <repo-path>")

    result = await _tool("griot_golden_set_list")

    assert result.is_error
    assert sentence in result.content[0].text
    state = stats.load_state()
    assert state["golden_set_error"] == "corrupted JSON"
    attention = stats.compute_stats([], [], {"points_count": 1, "embed_profile": "any"}, state=state)["attention"]
    assert sentence.replace(":\n  ", ": ") in attention


def test_golden_set_list_json_incorrect_use_is_exit_2_with_nothing_on_stdout(tmp_path):
    done = _run(tmp_path, "golden-set", "list", "--json", "extra")

    assert done.returncode == 2
    assert done.stdout == ""


# --- griot profiles list --json ----------------------------------------------------------------------------

_MCP_PROFILE_KEYS = {"profile", "is_active", "paid", "credential_configured", "collection"}
_CLI_PROFILE_KEYS = _MCP_PROFILE_KEYS | {"dim", "ram_tier", "rss_max_mb", "fits", "price_per_1m_tokens",
                                         "credential_env", "fix"}


def test_profiles_list_json_with_a_warning_active(tmp_path):
    done = _run(tmp_path, "profiles", "list", "--json", extra_env=WARNING_ACTIVE)

    assert WARNING in done.stderr
    doc = _json(done)
    assert set(doc) == {"profiles", "active", "ram_gb"}
    assert isinstance(doc["ram_gb"], float)
    assert [p["profile"] for p in doc["profiles"]] == sorted(common.EMBED_PROFILES)
    for entry in doc["profiles"]:
        assert set(entry) == _CLI_PROFILE_KEYS
        if entry["paid"]:
            assert entry["fits"] is None and entry["ram_tier"] is None
            assert entry["fix"] == (None if entry["credential_configured"] else entry["fix"])
        else:
            assert isinstance(entry["fits"], bool) and entry["fix"] is None
    assert sum(p["is_active"] for p in doc["profiles"]) == 1


@pytest.mark.anyio
async def test_profiles_list_json_and_the_tool_agree_on_every_shared_field(monkeypatch):
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-test-not-real")

    result = await _tool("griot_profiles_list")
    cli_doc = cli.profiles_list_result(ram_gb=64.0)

    mcp_doc = result.structured_content
    assert mcp_doc["active"] == cli_doc["active"]
    assert mcp_doc["profiles"] == [{k: entry[k] for k in _MCP_PROFILE_KEYS} for entry in cli_doc["profiles"]]
    by_name = {e["profile"]: e for e in cli_doc["profiles"]}
    assert by_name["openai-small"]["credential_configured"] is True and by_name["openai-small"]["fix"] is None


def test_a_missing_credential_names_the_command_that_sets_it(monkeypatch):
    monkeypatch.delenv("GRIOT_OPENAI_API_KEY", raising=False)
    by_name = {e["profile"]: e for e in cli.profiles_list_result(ram_gb=4.0)["profiles"]}

    assert by_name["openai-small"]["credential_configured"] is False
    assert by_name["openai-small"]["fix"] == "griot auth set openai"
    light = next(e for e in by_name.values() if e["ram_tier"] == "light")
    heavy = next(e for e in by_name.values() if e["ram_tier"] == "heavy")
    assert light["fits"] is False and heavy["fits"] is False
    assert all(e["fits"] for e in cli.profiles_list_result(ram_gb=64.0)["profiles"] if not e["paid"])


def test_profiles_list_text_names_a_missing_credential_without_glyphs(monkeypatch, capsys):
    monkeypatch.delenv("GRIOT_OPENAI_API_KEY", raising=False)
    assert cli.main(["profiles", "list"]) == 0

    out = capsys.readouterr().out
    assert "  griot profiles use <name>   # makes it the active profile" in out.splitlines()
    assert "psutil" not in out and "⚠" not in out
    # Worded as `griot doctor` words the same fact.
    assert "its credential (GRIOT_OPENAI_API_KEY) is not configured" in out
    assert "  griot auth set openai   # for openai-small" in out.splitlines()


def test_profiles_list_text_says_which_profiles_a_credential_unlocks(monkeypatch, capsys):
    """Several profiles missing: each command says what it is for, and a
    provider shared by two profiles is one command naming both."""
    def entry(profile, env, fix):
        return {"profile": profile, "is_active": False, "paid": True, "credential_configured": False, "dim": 8,
                "ram_tier": None, "rss_max_mb": None, "fits": None, "price_per_1m_tokens": 0.02,
                "credential_env": env, "fix": fix, "collection": "c"}
    monkeypatch.setattr(cli, "profiles_list_result", lambda: {"ram_gb": 64.0, "active": "x", "profiles": [
        entry("a-small", "A_KEY", "griot auth set a"), entry("b", "B_KEY", "griot auth set b"),
        entry("a-large", "A_KEY", "griot auth set a")]})
    assert cli.main(["profiles", "list"]) == 0

    lines = capsys.readouterr().out.rstrip().splitlines()
    assert lines[-2:] == ["  griot auth set a   # for a-small, a-large", "  griot auth set b   # for b"]


def test_profiles_list_json_incorrect_use_is_exit_2_with_nothing_on_stdout(tmp_path):
    done = _run(tmp_path, "profiles", "list", "--json", "--bogus")

    assert done.returncode == 2
    assert done.stdout == ""


# --- griot search --json -----------------------------------------------------------------------------------

def _vector(text: str, dim: int) -> list[float]:
    rng = random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
    return [rng.uniform(-1, 1) for _ in range(dim)]


DOCS = [
    {"id": "alpha:code:a.py:0", "content": "alpha code about retries\nsecond line",
     "metadata": {"source_type": "code", "repo": "alpha", "file_path": "a.py", "chunk_index": 0}},
    {"id": "alpha:commit:abc", "content": "fix: retries back off",
     "metadata": {"source_type": "commit", "repo": "alpha", "commit_hash": "abcdef1234", "author": "Ada",
                  "date": "2026-03-04T10:00:00+00:00", "chunk_index": 0}},
]


@pytest.fixture
def indexed(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_vector(t, common.EMBED_DIM) for t in texts])
    common.index_documents(DOCS)
    common.release_lock()
    common.release_client()  # the CLI subprocess opens the collection itself


@pytest.mark.anyio
async def test_search_json_and_griot_search_give_the_same_fields_for_the_same_hit(tmp_path, indexed):
    done = _run(tmp_path, "search", "retries", "--json", "--mode", "keyword", extra_env=WARNING_ACTIVE)
    cli_doc = _json(done)
    assert WARNING in done.stderr

    result = await _tool("griot_search", {"query": "retries", "mode": "keyword", "limit": 5})
    common.release_client()
    mcp_doc = result.structured_content

    assert set(cli_doc) == {"note", "results", "mode", "behind"}
    assert cli_doc["mode"] == mcp_doc["mode"] == "keyword"
    assert cli_doc["behind"] == mcp_doc["behind"] == {}
    assert cli_doc["results"], "the keyword search found the indexed text"
    for entry in cli_doc["results"]:
        assert {"source_label", "repo", "source_type", "metadata", "content", "score"} <= set(entry)
    assert cli_doc["results"] == mcp_doc["results"]
    assert cli_doc["note"] == mcp_doc["note"]
    assert cli_doc["note"].startswith(search_result.SEARCH_RESULT_NOTE)


def test_search_json_with_nothing_found_is_an_empty_array_and_the_reason_in_the_note(tmp_path, indexed):
    doc = _json(_run(tmp_path, "search", "retries", "--json", "--mode", "keyword", "--source-type", "tag"))

    assert doc["results"] == []
    assert doc["mode"] == "keyword"
    assert "The search was narrowed to" in doc["note"]


def test_search_json_incorrect_use_is_exit_2_with_nothing_on_stdout(tmp_path, indexed):
    done = _run(tmp_path, "search", "retries", "--json", "--mode", "keyword", "--repo", "nonesuch")

    assert done.returncode == 2
    assert done.stdout == ""
    assert "nonesuch" in done.stderr


def test_search_json_prints_only_the_document_in_process(indexed, capsys):
    assert cli.main(["search", "retries", "--json", "--mode", "keyword"]) == 0

    out = capsys.readouterr().out
    assert "Mode:" not in out and "No results" not in out
    json.loads(out)


def test_the_note_is_built_in_one_place():
    note = search_result.note(behind={}, repos=["a"], source_types=None, results=[], keyword_note="K.",
                              behind_next="griot_index_status")
    assert note.startswith(search_result.SEARCH_RESULT_NOTE)
    assert note.endswith(" K.")
    assert "The search was narrowed to" in note
    assert mcp_server.SEARCH_RESULT_NOTE == search_result.SEARCH_RESULT_NOTE


# --- one is_interactive -------------------------------------------------------------------------------------

def test_harnesses_asks_the_one_check_common_re_exports(monkeypatch, tmp_path):
    """One implementation: common.is_interactive IS griot.is_interactive,
    which `griot assist install` calls without needing common to load."""
    assert not hasattr(harnesses, "_is_interactive")
    assert common.is_interactive is griot.is_interactive
    calls = []
    monkeypatch.setattr(griot, "is_interactive", lambda: calls.append(1) or False)
    harness = next(h for h in harnesses.HARNESSES if h.global_instructions_file is not None)

    assert harnesses.offer_instructions(harness, "global", home=tmp_path) == "not-interactive"
    assert calls


def _interactive_in(use_pty: bool) -> str:
    """griot.is_interactive() in a child whose stdin and stdout are a
    pseudo-terminal, or pipes."""
    code = ["-c", "import griot; print(griot.is_interactive())"]
    if not use_pty:
        return subprocess.run([sys.executable, *code], capture_output=True, text=True, stdin=subprocess.PIPE,
                              timeout=60).stdout.strip()
    import pty
    primary, secondary = pty.openpty()
    child = subprocess.Popen([sys.executable, *code], stdin=secondary, stdout=secondary, stderr=secondary)
    os.close(secondary)
    out = b""
    try:
        while True:
            try:
                chunk = os.read(primary, 1024)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    finally:
        child.wait(timeout=60)
        os.close(primary)
    return out.decode().strip()


def test_the_one_check_sees_a_terminal_and_sees_its_absence():
    assert _interactive_in(use_pty=True) == "True"
    assert _interactive_in(use_pty=False) == "False"


# --- second round: a pasteable fix, one wording for a file of the wrong shape ------------------------------

def test_a_gone_path_with_a_space_is_quoted_so_the_last_line_can_be_pasted(tmp_path):
    import shlex
    gone = tmp_path / "gone dir"
    _write_repos([str(gone)])

    done = _run(tmp_path, "repos", "list")

    last = done.stdout.rstrip().splitlines()[-1].strip()
    assert last == f"griot repos remove '{gone}'"
    assert doctor.check_repositories(common)["fix"] == last
    # Pasted as it is, the line removes the path it names (--yes stands in
    # for the person answering the confirmation, which needs a terminal).
    argv = shlex.split(last)
    assert argv[:3] == ["griot", "repos", "remove"]
    pasted = _run(tmp_path, *argv[1:], "--yes")
    assert pasted.returncode == 0, pasted.stderr
    assert json.loads(common.REPOS_JSON_PATH.read_text()) == []


@pytest.mark.parametrize("harness", ["claude-code", "opencode"])
def test_the_setup_assistant_reads_the_json_listing_not_a_marker(harness):
    """The instructions griot installs for an agent: a marker in the text
    output can be reworded, the `exists` key of the documented JSON cannot."""
    from importlib import resources
    text = (resources.files("griot") / "resources" / "agents" / harness / "griot-setup-assistant.md").read_text()

    assert "doesn't exist" not in text
    assert "griot repos list --json" in text
    assert '"exists": false' in text
    # One form throughout: every mention reads the documented JSON.
    assert text.count("griot repos list") == text.count("griot repos list --json")


def test_the_operations_skill_reads_the_json_listing_not_a_marker():
    from importlib import resources
    text = (resources.files("griot") / "resources" / "skills" / "griot-operations" / "SKILL.md").read_text()

    assert "✓" not in text
    assert text.count("griot repos list") == text.count("griot repos list --json")


WRONG_GOLDEN_SETS = [{"a": 1}, {}, 5, None, [1], [{"query": "q"}], [{"query": "q", "must_include": [1]}],
                     [{"query": "q", "must_include": "a.py"}], [{"query": "q", "must_include": None}],
                     [{"query": "q", "must_include": {}}]]


def _golden_sentence(before_command="\n  "):
    return (f"{common.GOLDEN_SET_PATH} could not be read (not a list of cases, each with must_include). Fix it, or "
            f"delete it and curate again:{before_command}griot golden-set suggest <repo-path>")


@pytest.mark.parametrize("argv", [["golden-set", "list"], ["golden-set", "list", "--json"]])
def test_a_golden_set_of_the_wrong_shape_is_the_one_sentence_exit_1_and_nothing_on_stdout(tmp_path, argv):
    _write_cases({"a": 1})

    done = _run(tmp_path, *argv)

    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == f"Error: {_golden_sentence()}\n"


@pytest.mark.anyio
@pytest.mark.parametrize("cases", WRONG_GOLDEN_SETS)
async def test_a_golden_set_of_the_wrong_shape_reads_the_same_on_every_surface(cases, capsys):
    _write_cases(cases)

    assert golden_set.main(["list"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"Error: {_golden_sentence()}\n"
    result = await _tool("griot_golden_set_list")
    assert result.is_error
    assert _golden_sentence() in result.content[0].text
    state = stats.load_state()
    attention = stats.compute_stats([], [], {"points_count": 1, "embed_profile": "any"}, state=state)["attention"]
    assert _golden_sentence(" ") in attention


WRONG_REPOS = [[1], {"a": 1}, "a", [["/Users/you/a"]]]


def _repos_sentence():
    return (f"{common.REPOS_JSON_PATH} could not be read (not a list of directory paths). Fix it, or delete it "
            f"and register again:\n  griot repos add <path>")


@pytest.mark.parametrize("argv", [["repos", "list"], ["repos", "list", "--json"]])
def test_a_repos_json_of_the_wrong_shape_is_the_one_sentence_exit_1_and_nothing_on_stdout(tmp_path, argv):
    _write_repos([1])

    done = _run(tmp_path, *argv)

    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == f"Error: {_repos_sentence()}\n"


@pytest.mark.anyio
@pytest.mark.parametrize("paths", WRONG_REPOS)
async def test_a_repos_json_of_the_wrong_shape_reads_the_same_on_every_surface(paths, capsys):
    _write_repos(paths)

    assert repos.main(["list"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"Error: {_repos_sentence()}\n"
    result = await _tool("griot_repos_list")
    assert result.is_error
    assert _repos_sentence() in result.content[0].text
    check = doctor.check_repositories(common)
    assert f"{check['detail']}:\n  {check['fix']}" == _repos_sentence()


def test_one_required_result_is_singular(capsys):
    _write_cases([{"query": "a", "must_include": [{"repo": "a"}], "limit": 5},
                  {"query": "b", "must_include": [{"repo": "a"}, {"repo": "b"}], "limit": 5}])

    assert golden_set.main(["list"]) == 0

    assert capsys.readouterr().out == ("  [1] a (1 required result, vector search)\n"
                                       "  [2] b (2 required results, vector search)\n")


_SWITCH = "  griot profiles use <name>   # makes it the active profile"


def test_profiles_list_text_ends_with_the_missing_credential_when_one_is_missing(monkeypatch, capsys):
    monkeypatch.delenv("GRIOT_OPENAI_API_KEY", raising=False)
    assert cli.main(["profiles", "list"]) == 0

    lines = capsys.readouterr().out.rstrip().splitlines()
    assert "  griot auth set openai   # for openai-small" in lines
    assert lines[-1].startswith("  griot auth set ")
    # The more urgent step last: every credential command after the switch.
    auth_lines = [i for i, line in enumerate(lines) if line.startswith("  griot auth set ")]
    assert lines.index(_SWITCH) < min(auth_lines)


def test_profiles_list_text_ends_with_the_switch_when_every_credential_is_set(monkeypatch, capsys):
    monkeypatch.setattr(cli, "profiles_list_result", lambda: {
        "ram_gb": 64.0, "active": "p", "profiles": [
            {"profile": "p", "is_active": True, "paid": True, "credential_configured": True, "dim": 8,
             "ram_tier": None, "rss_max_mb": None, "fits": None, "price_per_1m_tokens": 0.02,
             "credential_env": "X", "fix": None, "collection": "c"}]})
    assert cli.main(["profiles", "list"]) == 0

    assert capsys.readouterr().out.rstrip().splitlines()[-1] == _SWITCH


def test_search_json_names_the_command_that_catches_up_a_repository(indexed, monkeypatch, capsys):
    monkeypatch.setattr(search_result.freshness, "behind_among", lambda names: {"alpha": 3})
    assert cli.main(["search", "retries", "--json", "--mode", "keyword"]) == 0

    doc = json.loads(capsys.readouterr().out)
    assert "alpha (3 commits)" in doc["note"]
    assert "`griot index all --repo alpha`" in doc["note"]


# --- third round: every reader of the golden set, a catch-up command that names the repository -------------

GOLDEN_SHAPES = [{"a": 1}, ["a"], 5]


def _corrupted_sentence():
    return _golden_sentence().replace("not a list of cases, each with must_include", "corrupted JSON")


@pytest.mark.parametrize("cases", GOLDEN_SHAPES)
def test_golden_set_remove_of_a_wrong_shape_is_the_one_sentence(tmp_path, cases):
    """Through the real CLI: `{"a": 1}` was a KeyError traceback here."""
    _write_cases(cases)

    done = _run(tmp_path, "golden-set", "remove", "1", "--yes")

    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == f"Error: {_golden_sentence()}\n"
    assert json.loads(common.GOLDEN_SET_PATH.read_text()) == cases, "the file is left as it was"


def test_golden_set_remove_after_the_question_is_the_one_sentence_too(capsys):
    """The file can change between the question and the removal (edited
    by hand while the person was answering): cmd_remove says it too."""
    _write_cases({"a": 1})

    assert golden_set.cmd_remove(1) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"Error: {_golden_sentence()}\n"


def test_golden_set_remove_of_a_corrupted_file_is_the_one_sentence(tmp_path):
    common.secure_mkdir(common.GOLDEN_SET_PATH.parent)
    common.GOLDEN_SET_PATH.write_text("{broken")

    done = _run(tmp_path, "golden-set", "remove", "1", "--yes")

    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == f"Error: {_corrupted_sentence()}\n"


def test_an_index_out_of_range_is_still_said_as_such(tmp_path):
    _write_cases(CASES)

    done = _run(tmp_path, "golden-set", "remove", "3", "--yes")

    assert done.returncode == 1
    assert done.stderr == "Error: index 3 out of range (1-2).\n"


@pytest.mark.parametrize("cases", GOLDEN_SHAPES)
def test_add_case_refuses_a_file_of_the_wrong_shape_and_leaves_it(cases):
    _write_cases(cases)

    with pytest.raises(common.WrongShape):
        golden_set.add_case("q", [{"repo": "a"}])
    assert json.loads(common.GOLDEN_SET_PATH.read_text()) == cases


@pytest.mark.parametrize("command", ["add", "suggest", "review"])
@pytest.mark.parametrize("cases", [*GOLDEN_SHAPES, "{broken"])
def test_the_curating_commands_say_the_one_sentence_before_any_search(cases, command, tmp_path, monkeypatch,
                                                                       capsys):
    """Checked before the search (which may be paid) and before asking
    about a single candidate: none of them could be saved."""
    if isinstance(cases, str):
        common.secure_mkdir(common.GOLDEN_SET_PATH.parent)
        common.GOLDEN_SET_PATH.write_text(cases)
        sentence = _corrupted_sentence()
    else:
        _write_cases(cases)
        sentence = _golden_sentence()
    monkeypatch.setattr(common, "is_interactive", lambda: True)

    def no_search(*a, **k):
        raise AssertionError("searched although nothing could be saved")

    monkeypatch.setattr(common, "search", no_search)
    monkeypatch.setattr(golden_set, "suggest_candidates", no_search)
    run = {"add": lambda: golden_set.cmd_add("q", mode="vector"),
           "suggest": lambda: golden_set.cmd_suggest(str(tmp_path)),
           "review": lambda: golden_set.cmd_review()}[command]

    assert run() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"Error: {sentence}\n"


@pytest.mark.anyio
@pytest.mark.parametrize("cases", GOLDEN_SHAPES)
async def test_the_mcp_add_and_remove_of_a_wrong_shape_are_the_one_sentence(cases):
    _write_cases(cases)

    added = await _tool("griot_golden_set_add", {"query": "q", "must_include": [{"repo": "a"}], "mode": "vector",
                                                  "confirm": True})
    removed = await _tool("griot_golden_set_remove", {"index": 1, "confirm": True})

    for result in (added, removed):
        assert result.is_error
        assert _golden_sentence() in result.content[0].text
    assert json.loads(common.GOLDEN_SET_PATH.read_text()) == cases


@pytest.mark.parametrize("cases", GOLDEN_SHAPES)
def test_the_quality_check_calls_a_file_of_another_shape_what_every_reader_calls_it(cases):
    """A file `golden-set remove` cannot read either is not answered with a
    command to remove one case: check_cases says the shape, as list does."""
    _write_cases(cases)

    with pytest.raises(common.WrongShape):
        golden_set.check_cases(golden_set.list_cases())


@pytest.mark.parametrize("case", [
    {"must_include": [{"repo": "alpha"}]},
    {"query": " ", "must_include": [{"repo": "alpha"}]},
    {"query": "q", "must_include": []},
    {"query": "q", "must_include": [{"repo": "alpha"}], "limit": 0},
    {"query": "q", "must_include": [{"repo": "alpha"}], "mode": "semantic"},
])
def test_a_case_the_quality_check_names_is_one_golden_set_remove_can_remove(case):
    """The command the refusal ends with works: the file reads for remove."""
    _write_cases([CASES[0], case])

    with pytest.raises(golden_set.CaseError) as raised:
        golden_set.check_cases(golden_set.list_cases())
    assert raised.value.number == 2
    assert golden_set.unrunnable_sentence(raised.value).endswith("\n  griot golden-set remove 2")
    assert golden_set.remove_case(2)["must_include"] == case["must_include"]


def test_the_catch_up_command_names_each_repository_quoted():
    catch_up = search_result.catch_up_commands
    assert catch_up({"real": 1}) == "`griot index all --repo real`"
    assert catch_up({"real": 1, "vanished dir": None}) == (
        "`griot index all --repo real`, `griot index all --repo 'vanished dir'`")
    # A name argparse would read as a flag is glued to its option.
    assert catch_up({"-x": 2}) == "`griot index all --repo=-x`"


def test_suggest_stops_with_the_one_sentence_when_the_file_changes_under_it(tmp_path, monkeypatch, capsys):
    """The file became unreadable after the check at the start (edited by
    hand meanwhile): the run stops at the first approval, says what was
    saved, and ends with the command."""
    _write_cases({"a": 1})
    monkeypatch.setattr(common, "is_interactive", lambda: True)
    monkeypatch.setattr(golden_set, "_refused_unreadable", lambda: None)
    candidate = {"query": "q", "must_include": [{"repo": "r", "file_path": "a.py"}], "limit": 5}
    monkeypatch.setattr(golden_set, "suggest_candidates", lambda *a, **k: {
        "candidates": [candidate, candidate], "problem": None, "too_many_files": 0, "not_indexable": 0,
        "no_message": 0})
    monkeypatch.setattr(golden_set, "_answer", lambda *a, **k: "yes")
    monkeypatch.setattr(golden_set, "case_mode_for", lambda query, mode: ("vector", None))

    assert golden_set.cmd_suggest(str(tmp_path)) == 1

    err = capsys.readouterr().err
    assert err == (f"0 case(s) added to {common.GOLDEN_SET_PATH}.\n"
                   f"Error: {_golden_sentence()}\n")


def test_a_name_that_cannot_be_pasted_gets_no_command():
    assert search_result.catch_up_commands({"bad\nname": 1}) == "`griot index all`"
    assert search_result.catch_up_commands({"bad\nname": 1, "real": 2}) == "`griot index all --repo real`"


def test_a_catch_up_command_pastes_as_the_repository_it_names():
    import shlex
    command = search_result.catch_up_commands({"vanished dir": 3}).strip("`")
    assert shlex.split(command) == ["griot", "index", "all", "--repo", "vanished dir"]


def test_the_behind_sentence_is_last_and_counts_one_commit():
    note = search_result.note(behind={"real": 1, "other": 2}, repos=None, source_types=None, results=[1],
                              keyword_note="K.", behind_next=search_result.catch_up_commands)

    assert "real (1 commit)," in note and "other (2 commits)" in note
    assert note.index(" K.") < note.index("is behind their HEAD")
    assert note.endswith("(see `behind`, and `griot index all --repo real`, `griot index all --repo other`).")


def test_search_json_ends_its_note_with_the_command_that_catches_up_the_repository(indexed, monkeypatch, capsys):
    monkeypatch.setattr(search_result.freshness, "behind_among", lambda names: {"vanished dir": 1})
    assert cli.main(["search", "retries", "--json", "--mode", "keyword"]) == 0

    doc = json.loads(capsys.readouterr().out)
    assert "vanished dir (1 commit)" in doc["note"]
    assert doc["note"].endswith("(see `behind`, and `griot index all --repo 'vanished dir'`).")
    assert "<name>" not in doc["note"]


@pytest.mark.anyio
async def test_griot_search_ends_its_note_with_the_tool_that_says_more(indexed, monkeypatch):
    monkeypatch.setattr(search_result.freshness, "behind_among", lambda names: {"alpha": 1})

    result = await _tool("griot_search", {"query": "retries", "mode": "keyword", "limit": 5})
    common.release_client()

    note = result.structured_content["note"]
    assert "alpha (1 commit)" in note
    assert note.endswith("(see `behind`, and griot_index_status).")
