"""`golden-set add`, `suggest` and `review` ask a person, so all three behave
alike around the terminal:

- without one they refuse BEFORE any search or git scan (exit 2, nothing
  changed, the next command last on stderr);
- end of input and Ctrl-C end cleanly, never a traceback;
- one answer vocabulary (numbers pick, y approves a suggestion, s or Enter
  skips, q quits); a bad answer asks again with the choices named instead of
  ending the command and throwing a (possibly paid) search away.

The interactive paths run in a real pseudo-terminal: a child process whose
stdin/stdout/stderr are a pty, so `common.is_interactive()` is true without
being faked.
"""

import json
import os
import pty
import select
import subprocess
import sys

import pytest

from griot import common, golden_set, logdb

# A child process: the search is replaced (no index, no network), everything
# else is the real command.
_DRIVER = """
import sys
from pathlib import Path
from griot import common, golden_set

class Hit:
    def __init__(self, score, payload):
        self.score, self.payload = score, payload

HITS = [
    Hit(0.9, {"repo": "repo-x", "source_type": "code", "file_path": "src/a.py"}),
    Hit(0.8, {"repo": "repo-x", "source_type": "code", "file_path": "src/b.py"}),
]
common.GOLDEN_SET_PATH = Path(sys.argv[1])
common.search = lambda query, limit=5, mode=None, diverse=False: HITS
raise SystemExit(golden_set.main(sys.argv[2:]))
"""


class Hit:
    def __init__(self, score, payload):
        self.score, self.payload = score, payload


HITS = [
    Hit(0.9, {"repo": "repo-x", "source_type": "code", "file_path": "src/a.py"}),
    Hit(0.8, {"repo": "repo-x", "source_type": "code", "file_path": "src/b.py"}),
]


def _env():
    return {**os.environ}


def _in_a_terminal(tmp_path, args, keys: bytes):
    """Runs the driver with a pty as stdin, stdout and stderr, types `keys`
    and returns (exit code, everything it printed, the golden set file's
    cases)."""
    script = tmp_path / "driver.py"
    script.write_text(_DRIVER)
    golden = tmp_path / "golden.json"
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen([sys.executable, str(script), str(golden), *args], stdin=slave, stdout=slave,
                                stderr=slave, env=_env(), close_fds=True)
        os.close(slave)
        os.write(master, keys)
        out = b""
        while True:
            ready, _, _ = select.select([master], [], [], 30)
            if not ready:
                proc.kill()
                pytest.fail(f"timed out; so far: {out!r}")
            try:
                chunk = os.read(master, 4096)
            except OSError:  # the child closed the terminal
                break
            if not chunk:
                break
            out += chunk
        code = proc.wait(timeout=30)
    finally:
        os.close(master)
    cases = json.loads(golden.read_text()) if golden.exists() else []
    return code, out.decode(errors="replace"), cases


@pytest.fixture
def no_search(monkeypatch):
    """The search must not run: it can be a paid call."""
    calls = []

    def search(*a, **kw):
        calls.append((a, kw))
        pytest.fail("a search ran before anyone could be asked")

    monkeypatch.setattr(common, "search", search)
    return calls


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(common, "is_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("asked without a terminal"))


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(common, "is_interactive", lambda: True)


def _cases():
    return json.loads(common.GOLDEN_SET_PATH.read_text()) if common.GOLDEN_SET_PATH.exists() else []


# --- without a terminal ------------------------------------------------------


def test_add_without_a_terminal_refuses_before_searching(no_terminal, no_search, capsys):
    assert golden_set.cmd_add("how does login work") == 2

    out, err = capsys.readouterr()
    assert out == ""
    assert "no terminal to ask on" in err and "Nothing was changed." in err
    assert err.rstrip().splitlines()[-1] == "  griot golden-set add 'how does login work'"
    assert _cases() == []


def test_add_refuses_a_bad_limit_before_anything_else(no_terminal, no_search, capsys):
    assert golden_set.cmd_add("q", limit=0) == 2
    assert "--limit must be at least 1" in capsys.readouterr().err


def test_suggest_without_a_terminal_refuses_before_the_git_scan(no_terminal, git_repo, monkeypatch, capsys):
    monkeypatch.setattr(golden_set, "suggest_candidates",
                        lambda *a, **kw: pytest.fail("the git log was scanned before anyone could be asked"))

    assert golden_set.cmd_suggest(str(git_repo.path)) == 2

    out, err = capsys.readouterr()
    assert out == ""
    assert "no terminal to ask on" in err and "Nothing was changed." in err
    assert err.rstrip().splitlines()[-1] == f"  griot golden-set suggest {git_repo.path}"
    assert _cases() == []


def test_suggest_refuses_without_a_terminal_even_when_nothing_would_be_offered(no_terminal, git_repo, capsys):
    """An empty repository, refused the same way as any other."""
    assert golden_set.cmd_suggest(str(git_repo.path)) == 2


def test_review_refusal_has_the_same_shape_and_ends_on_the_command(no_terminal, capsys):
    assert golden_set.cmd_review() == 2

    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("Error: griot golden-set review asks a person which result was the right one, "
                          "and there is no terminal to ask on. Nothing was changed.")
    assert err.rstrip().splitlines()[-2:] == ["Run it in an interactive terminal:", "  griot golden-set review"]


def test_the_refusal_is_one_wording_for_the_three_commands(no_terminal, git_repo, capsys):
    golden_set.cmd_add("q")
    add = capsys.readouterr().err
    golden_set.cmd_suggest(str(git_repo.path))
    suggest = capsys.readouterr().err
    golden_set.cmd_review()
    review = capsys.readouterr().err
    for text in (add, suggest, review):
        assert "there is no terminal to ask on. Nothing was changed.\nRun it in an interactive terminal:\n  griot " in text


def test_main_exit_code_without_a_terminal(tmp_path):
    """The real command, stdin not a terminal: exit 2 and no traceback."""
    script = tmp_path / "driver.py"
    script.write_text(_DRIVER)
    for args in (["add", "q"], ["review"]):
        done = subprocess.run([sys.executable, str(script), str(tmp_path / "g.json"), *args], env=_env(),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True)
        assert done.returncode == 2, done.stderr
        assert "Nothing was changed." in done.stderr and "Traceback" not in done.stderr
        assert done.stdout == ""


# --- add: in a terminal -------------------------------------------------------


def test_add_in_a_terminal_picks_numbers(tmp_path):
    code, out, cases = _in_a_terminal(tmp_path, ["add", "how does login work"], b"1,2\n")

    assert code == 0, out
    assert [c["must_include"] for c in cases] == [[
        {"repo": "repo-x", "source_type": "code", "file_path": "src/a.py"},
        {"repo": "repo-x", "source_type": "code", "file_path": "src/b.py"}]]
    assert "must_include" not in out.split("Case added")[0]  # no jargon in the prompt


def test_add_in_a_terminal_asks_again_after_a_bad_answer(tmp_path):
    code, out, cases = _in_a_terminal(tmp_path, ["add", "how does login work"], b"x\n7\n2\n")

    assert code == 0, out
    assert "Answer with numbers 1-2 (e.g. 1,3) or q." in out
    assert "7 out of range (1-2)." in out
    assert [c["must_include"][0]["file_path"] for c in cases] == ["src/b.py"]


@pytest.mark.parametrize("keys", [b"\n", b"q\n", b"s\n", b"\x04"])
def test_add_quits_with_enter_q_s_and_end_of_input(tmp_path, keys):
    code, out, cases = _in_a_terminal(tmp_path, ["add", "how does login work"], keys)

    assert code == 0, out
    assert cases == []
    assert "Nothing was changed." in out and "Traceback" not in out


def test_add_end_of_input_does_not_print_a_traceback_or_ask_twice(tmp_path):
    code, out, _ = _in_a_terminal(tmp_path, ["add", "q"], b"\x04")

    assert out.count("Which result") == 1


# --- suggest: in a terminal ---------------------------------------------------


def _two_commits(git_repo):
    git_repo.commit("Fix login bug", body="Details.", filename="auth.py")
    git_repo.commit("Add search feature", filename="search.py")


def test_suggest_in_a_terminal_approves_with_y(tmp_path, git_repo):
    _two_commits(git_repo)

    code, out, cases = _in_a_terminal(tmp_path, ["suggest", str(git_repo.path)], b"y\nq\n")

    assert code == 0, out
    assert len(cases) == 1
    assert "y = add it, s or Enter = skip, q = quit" in out


def test_suggest_in_a_terminal_asks_again_after_a_bad_answer(tmp_path, git_repo):
    _two_commits(git_repo)

    code, out, cases = _in_a_terminal(tmp_path, ["suggest", str(git_repo.path)], b"maybe\nyes\nskip\n")

    assert code == 0, out
    assert "Answer with y, s (or n) or q." in out
    assert len(cases) == 1


def test_suggest_end_of_input_quits_and_keeps_what_was_approved(tmp_path, git_repo):
    _two_commits(git_repo)

    code, out, cases = _in_a_terminal(tmp_path, ["suggest", str(git_repo.path)], b"y\n\x04")

    assert code == 0, out
    assert len(cases) == 1
    assert "1 case(s) added to" in out and "Traceback" not in out


# --- Ctrl-C and end of input, in process ---------------------------------------


def _raises(exc):
    def fake(*a):
        raise exc
    return fake


def test_add_ctrl_c_ends_cleanly_with_130(terminal, monkeypatch, capsys):
    monkeypatch.setattr(common, "search", lambda q, limit=5, mode=None, diverse=False: HITS)
    monkeypatch.setattr("builtins.input", _raises(KeyboardInterrupt()))

    assert golden_set.cmd_add("q") == 130

    out, err = capsys.readouterr()
    assert "Nothing was changed." in err and out.count("Traceback") == 0
    assert _cases() == []


def test_suggest_ctrl_c_ends_cleanly_with_130_and_says_what_was_added(terminal, git_repo, monkeypatch, capsys):
    _two_commits(git_repo)
    answers = iter(["y"])

    def fake(prompt=""):
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", fake)

    assert golden_set.cmd_suggest(str(git_repo.path)) == 130

    err = capsys.readouterr().err
    assert f"Stopped. 1 case(s) added to {common.GOLDEN_SET_PATH}." in err
    assert len(_cases()) == 1


def test_suggest_ctrl_c_before_any_approval_says_nothing_was_changed(terminal, git_repo, monkeypatch, capsys):
    _two_commits(git_repo)
    monkeypatch.setattr("builtins.input", _raises(KeyboardInterrupt()))

    assert golden_set.cmd_suggest(str(git_repo.path)) == 130

    assert "Nothing was changed." in capsys.readouterr().err
    assert _cases() == []


def test_end_of_input_in_suggest_stops_at_once(terminal, git_repo, monkeypatch, capsys):
    _two_commits(git_repo)
    asked = []

    def fake(prompt=""):
        asked.append(prompt)
        raise EOFError

    monkeypatch.setattr("builtins.input", fake)

    assert golden_set.cmd_suggest(str(git_repo.path)) == 0
    assert len(asked) == 1 and _cases() == []


def _log_a_search(question):
    """One row of the query log, shaped as the review's own tests write it."""
    from datetime import datetime, timezone

    code = {"repo": "r", "source_type": "code", "file_path": "src/lock.py"}
    logdb.write_query(common.LOG_DIR, {
        "timestamp": datetime.now(timezone.utc).isoformat(), "profile": "p", "collection": "c1", "project": "p1",
        "question": question, "via": "mcp", "limit": 8, "mode": "vector", "num_sources": 1,
        "sources": [common.source_label(code)], "top_score": 0.8, "results": [code]})


def test_review_ctrl_c_goes_to_stderr_with_130(terminal, monkeypatch, capsys):
    for _ in range(2):
        _log_a_search("how is the lock released")
    monkeypatch.setattr("builtins.input", _raises(KeyboardInterrupt()))

    assert golden_set.cmd_review() == 130

    out, err = capsys.readouterr()
    assert "Nothing was changed." in err and "Nothing was changed." not in out


# --- add: no results and the shared vocabulary --------------------------------


def test_add_without_results_says_why_and_ends_on_the_command(terminal, monkeypatch, capsys):
    monkeypatch.setattr(common, "search", lambda q, limit=5, mode=None, diverse=False: [])

    assert golden_set.cmd_add("lock release", mode="vector") == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert 'the vector search for "lock release" returned no results' in err
    assert "Nothing was changed." in err
    assert err.rstrip().splitlines()[-2:] == ["Try:", "  griot golden-set add 'lock release' --mode keyword"]


def test_add_without_results_in_keyword_mode_suggests_vector(terminal, monkeypatch, capsys):
    monkeypatch.setattr(common, "search", lambda q, limit=5, mode=None, diverse=False: [])

    golden_set.cmd_add("lock release", mode="keyword")

    assert capsys.readouterr().err.rstrip().splitlines()[-1] == "  griot golden-set add 'lock release' --mode vector"


def test_a_search_refusal_stays_exit_1(terminal, monkeypatch, capsys):
    def refuse(*a, **kw):
        raise common.SearchFilterError("keyword search needs keyword vectors. Run: griot index keywords")

    monkeypatch.setattr(common, "search", refuse)

    assert golden_set.cmd_add("q", mode="keyword") == 1
    assert "griot index keywords" in capsys.readouterr().err


def test_add_quitting_says_so_on_stderr_not_stdout(terminal, monkeypatch, capsys):
    monkeypatch.setattr(common, "search", lambda q, limit=5, mode=None, diverse=False: HITS)
    monkeypatch.setattr("builtins.input", lambda *a: "")

    assert golden_set.cmd_add("q") == 0

    out, err = capsys.readouterr()
    assert "Nothing was changed." in err and "Nothing was changed." not in out


def test_suggest_enter_skips_and_adds_nothing(terminal, git_repo, monkeypatch, capsys):
    _two_commits(git_repo)
    asked = []

    def fake(prompt=""):
        asked.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", fake)

    assert golden_set.cmd_suggest(str(git_repo.path)) == 0

    assert len(asked) == 2  # Enter skipped each candidate; it did not stop, nor ask again
    assert _cases() == []
    assert "0 case(s) added" in capsys.readouterr().out


def test_the_mcp_add_also_refuses_a_bad_limit_before_asking_or_writing(no_search):
    """Both surfaces validate the cheap thing first (the CLI above, the tool
    here); the tool never searches and never calls the CLI's prompt."""
    import asyncio

    from mcp.client.client import Client

    from griot import mcp_server

    async def call():
        async with Client(mcp_server.mcp) as client:
            result = await client.call_tool("griot_golden_set_add", {
                "query": "q", "must_include": [{"repo": "r", "source_type": "code", "file_path": "a.py"}],
                "limit": 0})
            return result.structured_content or {}

    out = asyncio.run(call())

    assert out.get("changed") is False and "limit must be at least 1" in out.get("message", "")
    assert _cases() == []


def test_the_answer_vocabulary_is_documented_in_one_place():
    doc = golden_set.__doc__ or ""
    assert "ANSWERS" in doc and "q = quit" in doc


# --- round 2: the pasteable command, Ctrl-C in the search, stderr hints ------------


def test_add_refusal_command_is_the_whole_query_not_a_cut_one(no_terminal, no_search, capsys):
    query = "a" * 120 + " two"

    golden_set.cmd_add(query)

    assert capsys.readouterr().err.rstrip().splitlines()[-1] == f"  griot golden-set add '{query}'"


def test_add_refusal_command_keeps_the_flags_the_person_gave(no_terminal, no_search, capsys):
    golden_set.cmd_add("lock release", limit=8, mode="keyword")

    assert capsys.readouterr().err.rstrip().splitlines()[-1] == (
        "  griot golden-set add 'lock release' --mode keyword --limit 8")


def test_add_refusal_quotes_what_the_shell_would_expand(no_terminal, no_search, capsys):
    golden_set.cmd_add("what is $HOME and `id`")

    assert capsys.readouterr().err.rstrip().splitlines()[-1] == "  griot golden-set add 'what is $HOME and `id`'"


@pytest.mark.parametrize("query", ["first line\nsecond line", "tab\there", "esc\x1b[31mred"])
def test_add_refusal_for_a_query_that_cannot_be_pasted_says_so_instead_of_cutting_it(
        no_terminal, no_search, capsys, query):
    golden_set.cmd_add(query)

    last = capsys.readouterr().err.rstrip().splitlines()[-1]
    assert last == "  griot golden-set add '<your query>'"
    assert "first line" not in last and "\x1b" not in last


def test_add_no_results_try_line_is_the_whole_query(terminal, monkeypatch, capsys):
    monkeypatch.setattr(common, "search", lambda q, limit=5, mode=None, diverse=False: [])
    query = "b" * 120 + " end"

    golden_set.cmd_add(query, mode="vector", limit=3)

    assert capsys.readouterr().err.rstrip().splitlines()[-1] == (
        f"  griot golden-set add '{query}' --mode keyword --limit 3")


def test_add_ctrl_c_during_the_search_ends_cleanly_with_130(terminal, monkeypatch, capsys):
    def search(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(common, "search", search)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("asked after Ctrl-C"))

    assert golden_set.cmd_add("q") == 130

    out, err = capsys.readouterr()
    assert out == "" and "Nothing was changed." in err
    assert _cases() == []


def test_add_quitting_and_ctrl_c_say_the_same_sentence(terminal, monkeypatch, capsys):
    monkeypatch.setattr(common, "search", lambda q, limit=5, mode=None, diverse=False: HITS)
    monkeypatch.setattr("builtins.input", lambda *a: "q")
    golden_set.cmd_add("q")
    quit_err = capsys.readouterr().err
    monkeypatch.setattr("builtins.input", _raises(KeyboardInterrupt()))
    golden_set.cmd_add("q")
    interrupted_err = capsys.readouterr().err

    assert "Nothing was changed." in quit_err and "Nothing was changed." in interrupted_err
    assert "Nothing added" not in quit_err


def test_a_bad_answer_hint_goes_to_stderr_in_add(terminal, monkeypatch, capsys):
    monkeypatch.setattr(common, "search", lambda q, limit=5, mode=None, diverse=False: HITS)
    answers = iter(["x", "9", "q"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))

    golden_set.cmd_add("q")

    out, err = capsys.readouterr()
    assert "Answer with numbers 1-2" in err and "9 out of range (1-2)." in err
    assert "Answer with" not in out and "out of range" not in out


def test_a_bad_answer_hint_goes_to_stderr_in_suggest(terminal, git_repo, monkeypatch, capsys):
    _two_commits(git_repo)
    answers = iter(["maybe", "q"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))

    golden_set.cmd_suggest(str(git_repo.path))

    out, err = capsys.readouterr()
    assert "Answer with" in err and "Answer with" not in out


def test_the_suggest_hint_names_every_word_it_accepts(terminal, git_repo, monkeypatch, capsys):
    _two_commits(git_repo)
    answers = iter(["maybe", "q"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))

    golden_set.cmd_suggest(str(git_repo.path))

    assert "Answer with y, s (or n) or q." in capsys.readouterr().err


def test_the_docs_table_lists_n_as_a_skip_in_suggest():
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "quality.md").read_text()
    assert "`n`, `no`" in doc and "(`suggest`" in doc.split("`n`, `no`")[1].splitlines()[0]
    assert "`n`, `no`" in golden_set.__doc__ or "n, no" in golden_set.__doc__


# --- a golden set of another shape: said before the search, at a terminal ---------------------------


def test_add_at_a_terminal_says_the_file_cannot_be_read_before_searching(tmp_path):
    (tmp_path / "golden.json").write_text(json.dumps({"a": 1}))

    code, out, _ = _in_a_terminal(tmp_path, ["add", "how does login work", "--mode", "vector"], b"")

    assert code == 1
    assert "Results for" not in out, "the search ran although its result could not be saved"
    assert "could not be read (not a list of cases, each with must_include)" in out
    assert out.rstrip().splitlines()[-1].strip() == "griot golden-set suggest <repo-path>"
    assert json.loads((tmp_path / "golden.json").read_text()) == {"a": 1}


def test_add_without_a_terminal_still_refuses_first_with_a_file_of_another_shape(no_terminal, no_search, capsys):
    common.secure_mkdir(common.GOLDEN_SET_PATH.parent)
    common.GOLDEN_SET_PATH.write_text(json.dumps({"a": 1}))

    assert golden_set.cmd_add("how does login work") == 2

    out, err = capsys.readouterr()
    assert out == ""
    assert "no terminal to ask on" in err and "Nothing was changed." in err
