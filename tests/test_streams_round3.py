"""Third round of the streams/--quiet item: the flag is in EVERY help, a
repository that cannot be read is never silenced, the two NO_COLOR readers are
one, the echo capture belongs to its own thread, `index all` keeps one stream,
and the refusals end on the step that fixes them."""
import argparse
import contextvars
import json
import os
import subprocess
import sys
import threading

import pytest

from griot import cli, common, index_code, stats

_IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def _run(tmp_path, *args, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "XDG_", "NO_COLOR"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               GRIOT_UPDATE_CHECK="false")
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "griot.cli", *args], env=env, capture_output=True, text=True,
                          timeout=180, stdin=subprocess.DEVNULL,
                          start_new_session=True)  # no controlling terminal: getpass must not reach /dev/tty


# --- --quiet is in the help of every command, leaves included ---------------------------------------------

_HELP_ROOTS = [[], ["index"], ["index", "all"], ["index", "code"], ["index", "commits"], ["index", "tags"],
               ["index", "branches"], ["index", "platform"], ["index", "keywords"], ["search"], ["ask"],
               ["stats"], ["doctor"], ["quality-check"], ["profiles"], ["config"], ["auth"], ["assist"],
               ["repos"], ["update"], ["golden-set"], ["mcp"], ["audit"]]


@pytest.mark.parametrize("root", _HELP_ROOTS, ids=lambda r: " ".join(r) or "griot")
def test_every_parser_that_can_show_help_names_the_quiet_flag(root, monkeypatch, capsys):
    """Walks the parsers the command really builds (leaves under nested
    subparsers included) instead of a list of names that goes stale."""
    built = []
    original = argparse.ArgumentParser.__init__

    def spy(self, *a, **k):
        original(self, *a, **k)
        built.append(self)

    monkeypatch.setattr(argparse.ArgumentParser, "__init__", spy)
    with pytest.raises(SystemExit):
        cli.main([*root, "--help"])
    capsys.readouterr()
    monkeypatch.undo()

    checked = [p for p in built if p.add_help]
    assert checked, "the command built a parser"
    for parser in checked:
        text = parser.format_help()
        assert "--quiet" in text and "GRIOT_QUIET=1" in text, f"{parser.prog} does not name --quiet"
        assert text.count("read by griot itself") == 1, f"{parser.prog} lists the flags twice"


def test_the_leaves_of_nested_parsers_are_among_those_checked(monkeypatch, capsys):
    built = []
    original = argparse.ArgumentParser.__init__

    def spy(self, *a, **k):
        original(self, *a, **k)
        built.append(self.prog)

    monkeypatch.setattr(argparse.ArgumentParser, "__init__", spy)
    with pytest.raises(SystemExit):
        cli.main(["repos", "--help"])
    capsys.readouterr()

    assert any(prog.endswith("repos add") for prog in built)


# --- a repository that cannot be listed is not silenced --------------------------------------------------

def test_a_repository_git_cannot_list_is_said_under_quiet(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("GRIOT_QUIET", "1")
    (repo / ".git" / "HEAD").write_text("not a ref\n")  # a work tree the listing cannot read

    assert index_code.discover_files(repo) == []

    err = capsys.readouterr().err
    assert "git could not list the files" in err
    assert err.rstrip().splitlines()[-1].strip() == f"git -C {repo} status", "ends on the command to paste"


# --- one NO_COLOR reader ------------------------------------------------------------------------------------

def test_the_charts_ask_common_for_no_color(monkeypatch):
    asked = []
    monkeypatch.setattr(common, "no_color", lambda environ=None: asked.append(environ) or True)

    class Tty:
        def isatty(self):
            return True

    assert stats._chart_width(Tty(), {"NO_COLOR": ""}) is None
    assert asked == [{"NO_COLOR": ""}]


def test_no_color_reads_the_mapping_it_is_given():
    assert common.no_color({"NO_COLOR": "1"}) is True
    assert common.no_color({"NO_COLOR": ""}) is False
    assert common.no_color({}) is False


# --- the echo capture is per thread of work -----------------------------------------------------------------

def test_another_thread_is_not_captured(capsys):
    other_done = threading.Event()

    def other():
        common.log_and_print("seen by the person")
        other_done.set()

    with common.capture_echo() as lines:
        worker = threading.Thread(target=other)
        worker.start()
        worker.join()
        common.log_and_print("kept for the caller")

    assert lines == ["kept for the caller"]
    assert "seen by the person" in capsys.readouterr().err


def test_a_context_copy_is_captured_like_its_parent(capsys):
    """asyncio.to_thread runs a tool body in a copy of the context: it keeps
    what the tool captured."""
    with common.capture_echo() as lines:
        contextvars.copy_context().run(common.log_and_print, "from a context copy")

    assert lines == ["from a context copy"]


# --- `index all` keeps one stream ---------------------------------------------------------------------------

@pytest.fixture
def one_repo_without_tags(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["/usr/bin/git", *args], cwd=repo, check=True, capture_output=True,
                       env={**os.environ, **_IDENTITY})
    # Registered by writing the file: `repos add` asks a person first.
    config = tmp_path / "config" / "griot"
    config.mkdir(parents=True)
    (config / "repos.json").write_text(json.dumps([str(repo.resolve())]))
    return repo


def test_index_all_footer_and_nothing_to_index_are_on_stderr(tmp_path, one_repo_without_tags):
    done = _run(tmp_path, "index", "all", "--sources", "tags")

    assert done.returncode == 0, done.stderr
    assert done.stdout == "", done.stdout
    assert "No tags to index." in done.stderr
    assert "all sources completed" in done.stderr


def test_nothing_to_index_is_silenced_by_quiet_but_not_the_run(tmp_path, one_repo_without_tags):
    done = _run(tmp_path, "index", "all", "--sources", "tags", "-q")

    assert done.returncode == 0, done.stderr
    assert done.stdout == "" and done.stderr == ""


# --- refusals end on the step that fixes them ---------------------------------------------------------------

def test_deleting_the_active_profile_ends_on_the_switch(tmp_path):
    done = _run(tmp_path, "profiles", "delete", "jina-code")

    assert done.returncode == 2, done.stderr
    assert done.stderr.rstrip().splitlines()[-1].strip().startswith("griot profiles use "), done.stderr


def test_auth_set_without_a_terminal_is_incorrect_use(tmp_path):
    done = _run(tmp_path, "auth", "set", "openai")

    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "Traceback" not in done.stderr
    assert "Nothing was changed" in done.stderr
    assert not (tmp_path / "config" / "griot" / ".env").read_text().count("OPENAI_API_KEY=sk") \
        if (tmp_path / "config" / "griot" / ".env").exists() else True
