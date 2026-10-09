import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# SESSION isolation: GRIOT_CONFIG_DIR/GRIOT_DATA_DIR need to point at a
# disposable directory BEFORE the first import of griot.common — the
# constants (CONFIG_DIR, DATA_DIR and all derived ones) are resolved at
# import time, and the import itself runs the migration/warning checks
# (which can write logs to DATA_DIR/logs). tmp_path (fixture) doesn't exist
# yet at this point, so a session tempdir covers the import; the autouse
# fixture below re-isolates per test.
_SESSION_TMP = Path(tempfile.mkdtemp(prefix="griot-tests-"))
os.environ["GRIOT_CONFIG_DIR"] = str(_SESSION_TMP / "config")
os.environ["GRIOT_DATA_DIR"] = str(_SESSION_TMP / "data")

# The code now lives in the griot package (src/griot/), installed editable
# in the venv (pip install -e ".[dev]"). The sys.path insert points at src/
# as a fallback, so the suite works even without the editable install — same
# reason as the old insert (pytest's rootdir/conftest discovery has its own
# rules, this is explicit).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# [security review, real gap] Must be set BEFORE the import below, not only
# in the per-test autouse fixture further down: common.py used to read every
# credential from the keychain at import, and any code run before a fixture
# (a module-level call, a collection-time import) would reach the real
# `keyring` backend if the package happens to be installed in the venv
# running this suite. No monkeypatch fixture exists yet at this point —
# this is a plain, permanent module-level override for the whole session.
sys.modules["keyring"] = None
# The line above covers this process only. A test that runs the real CLI in a
# subprocess imports the real `keyring` there, and on a machine with the
# keychain extra a fake credential reached the user's login keychain
# (2026-10-06). Every subprocess inherits this environment: keyring's `fail`
# backend makes it degrade to the file, as the test process does.
# tests/test_suite_keychain_isolation.py holds it.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"

from griot import common, jobs  # noqa: E402

# Guard: no test (not even the import above) may create the REAL user
# directories (~/.config/griot, ~/.local/share/griot). Records what already
# existed before the suite; the autouse fixture checks again after EACH test.
_REAL_GRIOT_DIRS = (
    Path.home() / ".config" / "griot",
    Path.home() / ".local" / "share" / "griot",
)
_REAL_DIRS_PREEXISTING = {p: p.exists() for p in _REAL_GRIOT_DIRS}


@pytest.fixture(autouse=True)
def _reset_common_globals(monkeypatch, tmp_path):
    """Isolates each test from real user state: never lets a test read/
    write the real qdrant_data, .spend_state.json, .griot.lock, or logs/.
    Two layers: the GRIOT_* env vars (for any code that re-resolves the
    directories, e.g. subprocess or reload) AND common's derived attributes
    (resolved at import time, so the env var alone isn't enough). Runs
    before AND after each test (autouse)."""
    config_dir = tmp_path / "config" / "griot"
    data_dir = tmp_path / "data" / "griot"
    monkeypatch.setenv("GRIOT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("GRIOT_DATA_DIR", str(tmp_path / "data"))

    # [real finding] cli.py's --profile/--chat-profile
    # handling writes GRIOT_EMBED_PROFILE/GRIOT_CHAT_PROFILE into the REAL
    # os.environ directly (cli.main() runs before any test's own
    # monkeypatch registers those keys) — a test exercising cli.main(...,
    # "--profile", ...) without its OWN monkeypatch.delenv/setenv on that
    # var first leaves it permanently set for the rest of the pytest
    # session (found via tests/test_profiles.py:
    # test_profile_flag_equal_syntax/test_profile_flag_repeated_last_wins,
    # neither of which called monkeypatch on GRIOT_EMBED_PROFILE). Touching
    # both vars here, in the autouse fixture that runs for every test,
    # registers them with monkeypatch regardless of whether the specific
    # test remembers to — monkeypatch restores by KEY at teardown, so any
    # direct os.environ[...] = ... write later in the test (by cli.py or
    # anything else) still gets undone, closing the whole class of leak
    # instead of patching it test-by-test.
    monkeypatch.delenv("GRIOT_EMBED_PROFILE", raising=False)
    monkeypatch.delenv("GRIOT_CHAT_PROFILE", raising=False)

    # [real finding] jobs.py's in-process job registry
    # (jobs._registry) is a module-level global dict, populated by
    # start_index_job() and only ever cleaned up lazily (running_index_job()
    # reaps a dead proc.poll() on the NEXT call) — a test whose fake Popen
    # stand-in's poll() always returns None ("still running", the normal
    # way to simulate a live subprocess) leaves a permanent fake "running"
    # entry in the registry for the rest of the pytest session, since
    # nothing ever calls poll() on it again to discover it's fake/dead.
    # The next test that calls start_index_job() then gets an unexpected
    # "already in progress" refusal for a job that isn't real and that
    # test never started. Resetting to a fresh dict per test closes this
    # the same way _client/_embed_model are reset above.
    monkeypatch.setattr(jobs, "_registry", {})
    # And the registry is reloaded from the jobs file in THIS test's data
    # directory on its first read, as a new process would.
    monkeypatch.setattr(jobs, "_loaded", False)

    # [security, real finding] common.keychain_get/_set/_delete lazily
    # `import keyring` inside each call — forcing sys.modules["keyring"] =
    # None makes that import raise ImportError deterministically (a
    # documented Python import-system behavior), so every test sees the
    # exact same "no keychain backend" degrade-gracefully path regardless
    # of whether the machine actually running the suite has a real OS
    # keychain reachable. This is a HARD requirement, not just isolation
    # for its own sake: without it, a real keyring install on the dev/CI
    # machine would make tests actually write fake test credentials into
    # the real OS keychain. tests/test_keychain.py's own tests override
    # this per-test with a fake module to exercise the "available" path.
    monkeypatch.setitem(sys.modules, "keyring", None)
    # What a previous test read from its fake keychain is not this test's.
    monkeypatch.setattr(common, "_keychain_cache", {})
    monkeypatch.setattr(common, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(common, "DATA_DIR", data_dir)
    monkeypatch.setattr(common, "ENV_PATH", config_dir / ".env")
    monkeypatch.setattr(common, "REPOS_JSON_PATH", config_dir / "repos.json")
    monkeypatch.setattr(common, "GOLDEN_SET_PATH", config_dir / "quality_golden_set.json")
    monkeypatch.setattr(common, "_client", None)
    monkeypatch.setattr(common, "_client_last_used_at", None)
    monkeypatch.setattr(common, "_embed_model", None)
    # Process-wide "pruned in the last day" (prune_logs_if_due): without the
    # reset, whether a test's first logged search prunes would depend on
    # which test happened to log one first.
    monkeypatch.setattr(common, "_last_log_prune", None)
    monkeypatch.setattr(common, "QDRANT_PATH", data_dir / "qdrant_data")
    monkeypatch.setattr(common, "SPEND_STATE_PATH", data_dir / ".spend_state.json")
    monkeypatch.setattr(common, "LOCK_PATH", data_dir / ".griot.lock")

    # LOG_DIR alone isn't enough to isolate log_and_print(): the logger's
    # FileHandler stays attached to the file opened on the first log since
    # common.py was imported (found by inspecting the production log — at
    # the time still logs/rag-indexer.log, today logs/griot.log — and
    # seeing test noise mixed in — fake PID from test_lock.py, dimension
    # error from test_indexing.py etc). Temporarily swaps the handlers to
    # tmp_path.
    test_log_dir = data_dir / "logs"
    test_log_dir.mkdir(parents=True)
    monkeypatch.setattr(common, "LOG_DIR", test_log_dir)
    original_handlers = common._logger.handlers[:]
    for h in original_handlers:
        common._logger.removeHandler(h)
    test_handler = logging.FileHandler(test_log_dir / "griot.log")
    test_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    common._logger.addHandler(test_handler)

    yield

    if common._client is not None:
        common._client.close()
    common._logger.removeHandler(test_handler)
    test_handler.close()
    for h in original_handlers:
        common._logger.addHandler(h)

    # Post-test guard: if some test (or code it exercised) escaped isolation
    # and created the real user directories, fail immediately — catches the
    # leak in the culprit test, not silently at the end of the suite.
    for real_dir, preexisting in _REAL_DIRS_PREEXISTING.items():
        assert preexisting or not real_dir.exists(), (
            f"Isolation broken: the test created {real_dir} for real. "
            f"Check GRIOT_CONFIG_DIR/GRIOT_DATA_DIR and conftest's monkeypatches."
        )


@pytest.fixture
def fake_gemini_token(monkeypatch):
    """Fake GEMINI_TOKEN for tests that exercise the direct call path to
    Gemini (embed_texts 'direct' backend, chat_completion) without depending
    on a real credential in the environment — common._http_post is always replaced
    in these tests, the network call never actually goes out."""
    monkeypatch.setattr(common, "GEMINI_TOKEN", "fake-token-for-tests")


def _run_git(repo_path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout


class GitRepo:
    """Helper to build real, disposable git repositories in tmp_path — tests
    git log/tag/branch parsing against real git, without mocking subprocess
    (the format of git's output is what actually matters to test)."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir(exist_ok=True)
        _run_git(path, "init", "-q", "-b", "main")
        _run_git(path, "config", "user.email", "test@example.com")
        _run_git(path, "config", "user.name", "Test")

    def commit(self, message: str, body: str = "", filename: str = "file.txt", content: str = "x") -> str:
        (self.path / filename).write_text(content)
        _run_git(self.path, "add", filename)
        full_message = f"{message}\n\n{body}" if body else message
        _run_git(self.path, "commit", "-q", "-m", full_message)
        return _run_git(self.path, "rev-parse", "HEAD").strip()

    def tag(self, name: str, message: str = "") -> None:
        if message:
            _run_git(self.path, "tag", "-a", name, "-m", message)
        else:
            _run_git(self.path, "tag", name)

    def branch(self, name: str) -> None:
        _run_git(self.path, "branch", name)

    def set_remote_head(self, branch: str = "main") -> None:
        """Creates refs/remotes/origin/* manually (without needing a real
        remote) — the same on-disk state that 'git clone' would leave."""
        for ref in self._local_branches():
            sha = _run_git(self.path, "rev-parse", ref).strip()
            _run_git(self.path, "update-ref", f"refs/remotes/origin/{ref}", sha)
        _run_git(self.path, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")

    def _local_branches(self) -> list[str]:
        out = _run_git(self.path, "branch", "--format=%(refname:short)")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def set_origin_url(self, url: str) -> None:
        _run_git(self.path, "remote", "add", "origin", url)


@pytest.fixture
def git_repo(tmp_path) -> GitRepo:
    return GitRepo(tmp_path / "repo")


# The real factory, for the one test file whose subject is the session itself
# (tests/test_http_connection_reuse.py, which only reaches 127.0.0.1).
REAL_NEW_HTTP_SESSION = common._new_http_session


@pytest.fixture(autouse=True)
def _no_test_leaves_this_process_marked_as_a_server():
    """`griot mcp` marks its process before the configuration loads (see
    griot.ENVIRONMENT_ONLY_NARROWS). A test that runs that command in this
    process must not leave the mark for the tests after it."""
    import griot

    yield
    griot.ENVIRONMENT_ONLY_NARROWS = False
    griot.PROFILE_FROM_COMMAND_LINE = None


@pytest.fixture(autouse=True)
def _the_test_directory_was_not_deleted_from_outside(tmp_path):
    """A test's own temporary directory gone at the end of it was deleted by
    something outside the test: no test removes its tmp_path. On 2026-10-06
    an agent ran `rm -rf` on a scratch path that another agent's full run
    used as --basetemp; one test lost its repository mid-way and failed as
    `assert set() == {'v1', 'v2'}`, which was then chased as a flake. Named
    here, the next such failure says what happened."""
    yield
    if not tmp_path.is_dir():
        pytest.fail(f"{tmp_path} was deleted while the test ran, by something outside it (another process "
                    f"removing pytest's --basetemp?). Its result says nothing about the code: give every run "
                    f"a --basetemp of its own.", pytrace=False)


@pytest.fixture(autouse=True)
def _no_test_opens_a_real_http_session(monkeypatch):
    """The calls to embedding and chat APIs go through common._http_post(),
    over a session the process keeps. Tests stand in for that function; one
    that forgets would send a request to a real API, with whatever
    credential the machine running the suite has. So the session cannot be
    made here, and none is carried from one test to the next."""
    def refuse():
        raise AssertionError("a test tried to open a real HTTP session: stand in for common._http_post")

    common._drop_http_session()
    monkeypatch.setattr(common, "_new_http_session", refuse)
    yield
    common._drop_http_session()


@pytest.fixture(autouse=True)
def _no_test_runs_a_harness_command(monkeypatch):
    """`griot assist install` can now run the harness's own CLI (`claude mcp
    add ...`) to register griot's MCP server. A test that simulates an
    interactive terminal and answers "y" would otherwise change the REAL
    configuration of whoever runs the suite. Tests that exercise that path
    replace this with their own recorder."""
    from griot import harnesses

    def refuse(argv):
        raise AssertionError(f"a test tried to run a harness command for real: {argv}")

    monkeypatch.setattr(harnesses, "_run_harness_command", refuse)


@pytest.fixture(autouse=True)
def _the_harness_config_dir_of_whoever_runs_the_suite_is_not_used(monkeypatch):
    """`griot assist install --scope global` writes where the harness keeps
    its user files: CLAUDE_CONFIG_DIR says where that is for Claude Code,
    XDG_CONFIG_HOME for opencode. Left set, a test that passes its own
    `home=` would still install into the REAL directory of whoever runs the
    suite. Tests that are about a variable set it themselves, to a temporary
    directory. OPENCODE_CONFIG_DIR does not move the install, but a test
    that says so must not depend on the runner's value either, and the
    OPENCODE_DISABLE_* switches decide whether opencode's skills are copied."""
    for variable in ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "OPENCODE_CONFIG_DIR", "OPENCODE_DISABLE_CLAUDE_CODE",
                     "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS", "OPENCODE_DISABLE_EXTERNAL_SKILLS"):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture(autouse=True)
def _no_test_writes_a_settings_file_outside_its_own_directory(monkeypatch, tmp_path_factory):
    """`griot assist install` can add rules to the harness's settings file
    (`~/.claude/settings.json`). A test that simulates a terminal, answers
    "y" and uses the real harness with the real home would otherwise edit
    the settings of whoever runs the suite. Every write of a settings file
    goes through harnesses._write_settings: here it refuses any path outside
    pytest's temporary directory."""
    import os
    from pathlib import Path

    from griot import harnesses

    write = getattr(harnesses, "_write_settings", None)
    if write is None:
        return
    base = Path(os.path.realpath(tmp_path_factory.getbasetemp()))

    def guarded(path, text):
        if base not in Path(os.path.realpath(path)).parents:
            raise AssertionError(f"a test tried to write a settings file outside the test's own directory: {path}")
        return write(path, text)

    monkeypatch.setattr(harnesses, "_write_settings", guarded)


@pytest.fixture(autouse=True)
def _echo_goes_to_stdout_again(monkeypatch):
    """The MCP server's main() points log_and_print() at stderr for the life
    of the process. A test that calls it would change where every later
    test's output goes."""
    from griot import common

    monkeypatch.setattr(common, "_echo_stream", None)


@pytest.fixture(autouse=True)
def _the_mcp_server_has_not_given_the_keyword_fallback_note_yet():
    """griot_search says why a default search fell back to vector once per
    collection and server process (mcp_server._KEYWORD_NOTE_GIVEN_FOR). The
    suite is one process: without this, whether a test sees the note would
    depend on which tests ran before it."""
    import sys

    def forget():
        server = sys.modules.get("griot.mcp_server")
        if server is not None and hasattr(server, "_KEYWORD_NOTE_GIVEN_FOR"):
            server._KEYWORD_NOTE_GIVEN_FOR.clear()

    forget()
    yield
    forget()
