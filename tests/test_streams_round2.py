"""Second round of the streams/--quiet item: consumers that read what
log_and_print says, the private mode of the state file, and the notices that
still went to stdout."""
import io
import json
import os
import subprocess

import pytest

from griot import common, golden_set

_IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def _git(path, *args):
    subprocess.run(["/usr/bin/git", *args], cwd=path, check=True, capture_output=True,
                   env={**os.environ, **_IDENTITY})


@pytest.fixture
def empty_repo(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "nothing here")
    return repo


def test_suggest_carries_why_nothing_was_listed(empty_repo, capsys):
    found = golden_set.suggest_candidates(empty_repo, limit=5, max_commits=10)

    assert found["problem"], "the explanation of the file listing is kept"
    assert "git lists no files" in found["problem"]
    assert "git lists no files" not in capsys.readouterr().err, "and is not printed on top of the result"


def test_suggest_carries_it_under_quiet_and_with_the_mcp_echo_stream(empty_repo, monkeypatch):
    monkeypatch.setenv("GRIOT_QUIET", "1")
    stream = io.StringIO()
    monkeypatch.setattr(common, "_echo_stream", stream)

    found = golden_set.suggest_candidates(empty_repo, limit=5, max_commits=10)

    assert "git lists no files" in (found["problem"] or "")
    assert stream.getvalue() == ""


# --- the state file is private, and doctor stays quiet about it ----------------------------------------------

def test_the_announced_state_file_is_private():
    from griot import notices

    notices.record({notices.KEYWORD_VECTORS_MISSING}, [notices.keyword_vectors_missing("c")])

    path = common.DATA_DIR / "announced.json"
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_a_file_that_was_open_is_closed_when_the_state_changes():
    from griot import notices

    path = common.DATA_DIR / "announced.json"
    common.secure_mkdir(path.parent)
    path.write_text("{}")
    path.chmod(0o644)

    notices.record({notices.KEYWORD_VECTORS_MISSING}, [notices.keyword_vectors_missing("c")])

    assert oct(path.stat().st_mode & 0o777) == "0o600"



def test_adding_a_missing_directory_is_incorrect_use_and_ends_on_how_to_add_one(tmp_path):
    from test_streams_and_quiet import _run
    done = _run(tmp_path, "repos", "add", "/Users/you/not-here")

    assert done.returncode == 2
    assert done.stderr.splitlines()[-1] == "  griot repos add <directory>"


def test_adding_a_registered_directory_is_a_conflict_with_the_list_not_incorrect_use(tmp_path):
    import json
    from test_streams_and_quiet import _run
    here = tmp_path / "proj"
    here.mkdir()
    listing = tmp_path / "config" / "griot" / "repos.json"
    listing.parent.mkdir(parents=True)
    listing.write_text(json.dumps([str(here.resolve())]))

    done = _run(tmp_path, "repos", "add", str(here))

    assert done.returncode == 1, done.stderr
    assert "is already in" in done.stderr
    assert done.stderr.splitlines()[-1] == "  griot repos list"


def test_deleting_a_profile_with_no_collection_ends_on_the_command_that_lists_them(tmp_path):
    from test_streams_and_quiet import _run
    done = _run(tmp_path, "profiles", "delete", "bge-small")

    assert done.returncode == 2, done.stderr
    assert "nothing to delete" in done.stderr
    assert done.stderr.splitlines()[-1] == "  griot profiles list"


def test_a_dash_q_after_the_double_dash_is_data_not_the_flag(tmp_path):
    from test_streams_and_quiet import _run
    # Past `--` a path called -q is a path: it reaches the command, which
    # refuses it by name. Stripped, the command would see no path at all.
    done = _run(tmp_path, "repos", "add", "--", "-q")

    assert done.returncode == 2
    assert "'-q' doesn't exist or isn't a directory" in done.stderr

def test_doctor_directories_stays_ok_after_a_real_run_announced_the_old_variable(tmp_path):
    from test_streams_and_quiet import _run
    env = {"RAG_EMBED_PROFILE": "jina-code"}
    first = _run(tmp_path, "repos", "list", extra_env=env)
    assert "1 notice: griot doctor lists it with what to do.\n  griot doctor" in first.stderr  # the ledger file was written

    done = _run(tmp_path, "doctor", "--json", extra_env=env)

    checks = {c["check"]: c for c in json.loads(done.stdout)}
    # About the ledger file this griot wrote. Not the whole status: doctor
    # asks the harnesses on this machine which server they registered, which
    # can start that server (whatever griot it is) with this test's data
    # directory, and the log it creates there is not this griot's to answer
    # for. The run before used to create that log first, privately, by
    # printing the start-up warning the footer replaced.
    assert "announced.json" not in checks["directories"]["detail"], checks["directories"]
    assert oct((tmp_path / "data" / "griot" / "announced.json").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("env, layout", [
    ([("RAG_EMBED_PROFILE", "GRIOT_EMBED_PROFILE")], []),
    ([], [("/Users/you/old/repos.json", "/Users/you/.config/griot/repos.json")]),
])
def test_the_doctor_detail_is_a_whole_sentence_without_a_dangling_colon(monkeypatch, env, layout):
    from griot import doctor
    monkeypatch.setattr(common, "legacy_env_findings", lambda: env)
    monkeypatch.setattr(common, "legacy_layout_findings", lambda: layout)

    check = doctor.check_old_settings(common)

    assert not check["detail"].rstrip().endswith(":")
    assert "Warning" not in check["detail"]
    assert check["detail"].rstrip().endswith(".")
    assert check["fix"].startswith(("export ", "mv "))


# --- notices and progress that were still printed to stdout ------------------------------------------------

@pytest.fixture
def _fake_embed(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [[0.1] * common.EMBED_DIM for _ in texts])


def _docs(names):
    return [{"id": f"proj:code:{n}:0", "content": f"content of {n}",
             "metadata": {"source_type": "code", "repo": "proj", "file_path": n, "chunk_index": 0}} for n in names]


def _registered(tmp_path):
    import json
    path = tmp_path / "a" / "proj"
    path.mkdir(parents=True)
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(path.resolve())]))
    return path


def test_the_embedding_progress_line_is_on_stderr(_fake_embed, capsys):
    common.index_documents(_docs(["a.py"]))

    seen = capsys.readouterr()
    assert "Embedding 1 of 1 chunk" not in seen.out
    assert "Embedding 1 of 1 chunk" in seen.err


def test_the_embedding_progress_line_is_silenced_by_quiet(_fake_embed, capsys, monkeypatch):
    monkeypatch.setenv("GRIOT_QUIET", "1")
    common.index_documents(_docs(["a.py"]))

    seen = capsys.readouterr()
    assert "Embedding 1 of 1 chunk" not in seen.out + seen.err


def test_removing_stale_points_is_said_on_stderr_and_a_dry_run_result_stays_on_stdout(
        tmp_path, _fake_embed, capsys, monkeypatch):
    path = _registered(tmp_path)
    common.index_documents(_docs(["a.py", "b.py", "c.py"]))
    capsys.readouterr()

    common.prune_orphans(_docs(["a.py"]), source_type="code", repo_paths=[path], dry_run=True)
    dry = capsys.readouterr()
    assert "[dry-run] 2 stale point(s)" in dry.out and "[dry-run]" not in dry.err

    common.prune_orphans(_docs(["a.py"]), source_type="code", repo_paths=[path])
    done = capsys.readouterr()
    assert "Removed 2 stale point(s)" in done.err and done.out == ""

    common.index_documents(_docs(["a.py", "b.py", "c.py"]))
    capsys.readouterr()
    monkeypatch.setenv("GRIOT_QUIET", "1")
    common.prune_orphans(_docs(["a.py"]), source_type="code", repo_paths=[path])
    assert capsys.readouterr().err == "", "removing is progress: --quiet silences it"


def test_stale_points_left_in_place_because_of_a_failure_are_said_even_under_quiet(
        tmp_path, _fake_embed, capsys, monkeypatch):
    path = _registered(tmp_path)
    common.index_documents(_docs(["a.py", "b.py", "c.py"]))
    capsys.readouterr()
    monkeypatch.setenv("GRIOT_QUIET", "1")

    common.prune_orphans(_docs(["a.py", "b.py"]), source_type="code", repo_paths=[path], failed=1)

    seen = capsys.readouterr()
    assert "Stale points were not removed" in seen.err and seen.out == ""


def test_the_redaction_report_is_on_stderr_and_is_never_silenced(capsys, monkeypatch):
    monkeypatch.setenv("GRIOT_QUIET", "1")
    common.take_redactions()
    common.chunk_text("token ghp_" + "a" * 36, where="proj/config.yml")

    common.report_redactions()

    seen = capsys.readouterr()
    assert seen.out == ""
    assert "credential-looking value(s)" in seen.err and "rotate it" in seen.err


def test_a_repository_with_no_supported_file_is_a_note_on_stderr(tmp_path, capsys):
    from griot import index_code
    here = tmp_path / "bare"
    here.mkdir()

    assert index_code.process_repository(here) == []

    seen = capsys.readouterr()
    assert "No supported file found in bare." in seen.err
    assert "No supported file" not in seen.out
