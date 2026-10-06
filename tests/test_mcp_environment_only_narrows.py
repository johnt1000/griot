"""An MCP server obeys its environment only where that narrows.

A server is started by whoever registered it, and a registration can come
with the project: a `.mcp.json` in a cloned repository names a command a
person recognises (`griot mcp`) and an `env` nobody reads. That environment
could turn on indexing through MCP, add directories an agent may index,
raise the spend ceilings, point a platform token at another host or switch
the local embedding profile for one that sends everything to an API, and
the server did as it was told. It could also name a configuration
directory inside the repository itself and bring its own everything, with
the credentials in the keychain still within reach.

So for a server: a value in the environment that WIDENS what the person's
own configuration file says is ignored, and said so; a value that narrows
is obeyed, as before. And the configuration and data directories may not
be inside the project the server was started in.

The command line is not the environment (`griot mcp --profile x` is what a
person approving a registration sees), and the CLI at a terminal obeys the
shell it runs in, as it always did.

Started for real, through both ways a server starts, and asked over stdio:
an import of the module in a test is not a server."""

import json
import os
import subprocess
import sys

import pytest

from griot import common

ENTRIES = {"griot mcp": ["-m", "griot.cli", "mcp"], "python -m griot.mcp_server": ["-m", "griot.mcp_server"]}


class Served:
    def __init__(self, returncode, stdout_lines, stderr, answers):
        self.returncode, self.stdout_lines, self.stderr = returncode, stdout_lines, stderr
        self.started = 2 in answers
        self.tools = [tool["name"] for tool in answers.get(2, {}).get("result", {}).get("tools", [])]
        listed = answers.get(3, {}).get("result", {}).get("structuredContent", {})
        self.settings = {entry["name"]: entry for entry in listed.get("settings", [])}
        self.note = listed.get("note", "")
        self.spend = answers.get(4, {}).get("result", {}).get("structuredContent", {})


def _is_json(line):
    try:
        json.loads(line)
        return True
    except ValueError:
        return False


def _serve(tmp_path, *, file="", entry="griot mcp", cwd=None, args=(), dirs=None, **exported) -> Served:
    """A server started the way a harness starts it, in `cwd` (the project),
    with `file` as the person's own configuration and `exported` as the
    environment of the registration."""
    home = tmp_path / "home"
    project = cwd or tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "XDG_"))}
    if dirs is None:
        env_file = home / "config" / "griot" / ".env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(file)
        env_file.chmod(0o600)
        dirs = {"GRIOT_CONFIG_DIR": str(home / "config"), "GRIOT_DATA_DIR": str(home / "data")}
    env.update(dirs)
    env.update(exported)
    process = subprocess.Popen([sys.executable, *ENTRIES[entry], *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env=env, cwd=project)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "griot_config_list", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "griot_spend_status", "arguments": {}}},
    ]
    lines, answers = [], {}
    try:
        try:
            for message in messages:
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
        except BrokenPipeError:
            pass  # it refused to start: what it said is on stderr
        while not {2, 3, 4} <= set(answers):  # answers come in the order the calls finish
            line = process.stdout.readline()
            if not line:
                break
            lines.append(line)
            if _is_json(line) and "id" in json.loads(line):
                answers[json.loads(line)["id"]] = json.loads(line)
        rest, stderr = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
    return Served(process.returncode, lines + rest.splitlines(keepends=True), stderr, answers)


def _value(served, name):
    return served.settings[name]["value"], served.settings[name]["source"]


# === what widens is ignored =====================================================================


@pytest.mark.parametrize("entry", list(ENTRIES))
def test_the_environment_cannot_turn_on_indexing_through_mcp(tmp_path, entry):
    served = _serve(tmp_path, entry=entry, GRIOT_MCP_ENABLE_INDEX="true")

    assert served.started and "griot_index_repo" not in served.tools
    assert _value(served, "mcp-index") == ("false", "default")


def test_it_says_so_where_a_person_and_an_agent_can_read_it(tmp_path):
    served = _serve(tmp_path, GRIOT_MCP_ENABLE_INDEX="true")

    assert "GRIOT_MCP_ENABLE_INDEX" in served.stderr and "ignored" in served.stderr
    ignored = served.settings["mcp-index"]["environment_ignored"]
    assert ignored and "GRIOT_MCP_ENABLE_INDEX" in ignored and "griot config set mcp-index" in ignored
    assert served.settings["spend-ceiling"]["environment_ignored"] is None
    assert "ignored" in served.note


def test_what_it_writes_to_stdout_is_still_only_the_protocol(tmp_path):
    served = _serve(tmp_path, GRIOT_MCP_ENABLE_INDEX="true", GRIOT_SPEND_CEILING_USD="100000")

    assert served.started and [line for line in served.stdout_lines if line.strip() and not _is_json(line)] == []


def test_indexing_the_person_turned_on_in_their_own_file_stays_on(tmp_path):
    served = _serve(tmp_path, file="GRIOT_MCP_ENABLE_INDEX=true\n", GRIOT_MCP_ENABLE_INDEX="true")

    assert "griot_index_repo" in served.tools and served.settings["mcp-index"]["environment_ignored"] is None


def test_the_environment_can_turn_it_off(tmp_path):
    served = _serve(tmp_path, file="GRIOT_MCP_ENABLE_INDEX=true\n", GRIOT_MCP_ENABLE_INDEX="false")

    assert "griot_index_repo" not in served.tools and _value(served, "mcp-index") == ("false", "environment")


@pytest.mark.parametrize("variable,key", [("GRIOT_SPEND_CEILING_USD", "daily_ceiling_usd"),
                                          ("GRIOT_SPEND_VELOCITY_CEILING_USD", "velocity_ceiling_usd")])
def test_the_environment_cannot_raise_a_spend_ceiling(tmp_path, variable, key):
    served = _serve(tmp_path, file=f"{variable}=2.5\n", **{variable: "100000"})

    assert served.spend[key] == 2.5
    assert variable in served.stderr


@pytest.mark.parametrize("variable,key", [("GRIOT_SPEND_CEILING_USD", "daily_ceiling_usd"),
                                          ("GRIOT_SPEND_VELOCITY_CEILING_USD", "velocity_ceiling_usd")])
def test_the_environment_can_lower_one(tmp_path, variable, key):
    served = _serve(tmp_path, file=f"{variable}=2.5\n", **{variable: "0.5"})

    assert served.spend[key] == 0.5 and variable not in served.stderr


def test_a_ceiling_is_measured_against_the_default_when_the_file_has_none(tmp_path):
    raised = _serve(tmp_path, GRIOT_SPEND_CEILING_USD="3.01")
    kept = _serve(tmp_path, GRIOT_SPEND_CEILING_USD="3.0")

    assert raised.spend["daily_ceiling_usd"] == 3.0 and "GRIOT_SPEND_CEILING_USD" in raised.stderr
    assert kept.spend["daily_ceiling_usd"] == 3.0 and "GRIOT_SPEND_CEILING_USD" not in kept.stderr


def test_a_value_that_cannot_be_read_is_not_obeyed_either(tmp_path):
    """It used to stop the server; a server that starts with the person's
    own value is the narrower outcome."""
    served = _serve(tmp_path, GRIOT_SPEND_CEILING_USD="lots")

    assert served.started and served.spend["daily_ceiling_usd"] == 3.0
    assert "GRIOT_SPEND_CEILING_USD" in served.stderr


def test_the_environment_cannot_add_a_directory_an_agent_may_index(tmp_path):
    allowed, other = tmp_path / "code", tmp_path / "elsewhere"
    for directory in (allowed, allowed / "one", other):
        directory.mkdir(parents=True)

    widened = _serve(tmp_path, file=f"GRIOT_MCP_INDEX_ROOTS={allowed}\n", GRIOT_MCP_INDEX_ROOTS=f"{allowed}:{other}")
    narrowed = _serve(tmp_path, file=f"GRIOT_MCP_INDEX_ROOTS={allowed}\n", GRIOT_MCP_INDEX_ROOTS=str(allowed / "one"))
    from_nothing = _serve(tmp_path, GRIOT_MCP_INDEX_ROOTS=str(other))

    assert _value(widened, "mcp-index-roots") == (str(allowed), "file")
    assert _value(narrowed, "mcp-index-roots") == (str(allowed / "one"), "environment")
    assert from_nothing.settings["mcp-index-roots"]["source"] == "default"
    assert not from_nothing.settings["mcp-index-roots"]["value"]


def test_the_environment_cannot_send_a_platform_token_somewhere_else(tmp_path):
    served = _serve(tmp_path, GRIOT_GITLAB_API_BASE="https://gitlab.attacker.example/api/v4",
                    GRIOT_GITEA_HOSTS="git.attacker.example")

    assert _value(served, "gitlab-api-base") == ("https://gitlab.com/api/v4", "default")
    assert served.settings["gitea-hosts"]["source"] == "default" and not served.settings["gitea-hosts"]["value"]
    assert "attacker.example" not in json.dumps([entry["value"] for entry in served.settings.values()])


def test_a_host_the_person_set_themselves_may_be_repeated_by_the_environment(tmp_path):
    served = _serve(tmp_path, file="GRIOT_GITLAB_API_BASE=https://gitlab.example.com/api/v4\n",
                    GRIOT_GITLAB_API_BASE="https://gitlab.example.com/api/v4")

    assert served.settings["gitlab-api-base"]["value"] == "https://gitlab.example.com/api/v4"
    assert served.settings["gitlab-api-base"]["environment_ignored"] is None


@pytest.mark.parametrize("entry", list(ENTRIES))
def test_the_environment_cannot_switch_a_local_profile_for_one_that_calls_an_api(tmp_path, entry):
    served = _serve(tmp_path, entry=entry, GRIOT_EMBED_PROFILE="openai-small")

    assert served.spend["embed_profile"] == "jina-code"
    assert "GRIOT_EMBED_PROFILE" in served.stderr


def test_nor_one_api_for_another(tmp_path):
    served = _serve(tmp_path, file="GRIOT_EMBED_PROFILE=gemini\n", GRIOT_EMBED_PROFILE="openai-small")

    assert served.spend["embed_profile"] == "gemini"


def test_the_environment_can_pick_a_local_profile(tmp_path):
    """Nothing leaves the machine with any of them."""
    served = _serve(tmp_path, file="GRIOT_EMBED_PROFILE=openai-small\n", GRIOT_EMBED_PROFILE="bge-small")

    assert served.spend["embed_profile"] == "bge-small" and "GRIOT_EMBED_PROFILE" not in served.stderr


def test_the_profile_the_person_chose_may_be_repeated_by_the_environment(tmp_path):
    served = _serve(tmp_path, file="GRIOT_EMBED_PROFILE=openai-small\n", GRIOT_EMBED_PROFILE="openai-small")

    assert served.spend["embed_profile"] == "openai-small" and "GRIOT_EMBED_PROFILE" not in served.stderr


def test_a_profile_on_the_command_line_is_not_the_environment(tmp_path):
    """`griot mcp --profile x` is in the command a person approves when a
    server is registered; the `env` beside it is not."""
    served = _serve(tmp_path, args=["--profile", "openai-small"])

    assert served.spend["embed_profile"] == "openai-small" and "ignored" not in served.stderr


def test_the_environment_cannot_turn_the_logging_of_questions_back_on(tmp_path):
    on = _serve(tmp_path, file="GRIOT_LOG_QUESTIONS=false\n", GRIOT_LOG_QUESTIONS="true")
    off = _serve(tmp_path, GRIOT_LOG_QUESTIONS="false")

    assert _value(on, "log-questions") == ("false", "file")
    assert _value(off, "log-questions") == ("false", "environment")


def test_the_environment_cannot_turn_the_check_for_a_newer_release_back_on(tmp_path):
    """The doctor's request to PyPI is the person's to allow: a file that
    turns it off stays off whatever the registration's `env` says, in any
    spelling the readers take as on."""
    for word in ("true", "ON", "1"):
        on = _serve(tmp_path, file="GRIOT_UPDATE_CHECK=no\n", GRIOT_UPDATE_CHECK=word)

        assert _value(on, "update-check") == ("no", "file")
        assert on.settings["update-check"]["environment_ignored"]
        assert "GRIOT_UPDATE_CHECK in the environment this server was started with was ignored" in on.stderr


def test_the_environment_can_turn_the_check_off(tmp_path):
    off = _serve(tmp_path, GRIOT_UPDATE_CHECK="false")
    again = _serve(tmp_path, file="GRIOT_UPDATE_CHECK=true\n", GRIOT_UPDATE_CHECK="off")
    same = _serve(tmp_path, file="GRIOT_UPDATE_CHECK=off\n", GRIOT_UPDATE_CHECK="false")
    repeated = _serve(tmp_path, GRIOT_UPDATE_CHECK="true")  # on is the default: it widens nothing

    assert _value(off, "update-check") == ("false", "environment")
    assert _value(again, "update-check") == ("false", "environment")
    assert _value(same, "update-check") == ("false", "environment")
    assert _value(repeated, "update-check") == ("true", "environment")
    assert "ignored" not in off.stderr + again.stderr + same.stderr + repeated.stderr


def test_what_widens_nothing_is_obeyed_as_before(tmp_path):
    served = _serve(tmp_path, GRIOT_MCP_CONCURRENCY_MODE="single", GRIOT_PROJECT="a-project",
                    GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES="3")

    assert _value(served, "mcp-concurrency") == ("single", "environment")
    assert _value(served, "project") == ("a-project", "environment")
    assert _value(served, "max-failed-batches") == ("3", "environment")
    assert "ignored" not in served.stderr


def test_the_environment_cannot_let_an_index_run_fail_for_longer(tmp_path):
    """Each batch that fails was a call to the API."""
    served = _serve(tmp_path, GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES="100000")

    assert _value(served, "max-failed-batches") == ("5", "default")


# --- what is obeyed is what was measured ----------------------------------------------------------
# A value was measured in one form (`~` expanded, `..` taken out as text) and
# left in the environment in another, which the code that uses it read its own
# way (no `~`, links followed BEFORE `..`). A value written to look narrow
# could then be wide. What a server obeys is put back in the form it was
# measured in.


def test_a_way_out_through_a_link_and_back_up_is_not_inside_the_directory_it_starts_in(tmp_path):
    work, secret = tmp_path / "R" / "work", tmp_path / "R" / "secret"
    (work / "evil").mkdir(parents=True)
    (secret / "deep").mkdir(parents=True)
    (work / "evil" / "link").symlink_to(secret / "deep")

    served = _serve(tmp_path, file=f"GRIOT_MCP_INDEX_ROOTS={work}\n",
                    GRIOT_MCP_INDEX_ROOTS=f"{work}/evil/link/..")

    assert _value(served, "mcp-index-roots") == (str(work), "file")
    assert "GRIOT_MCP_INDEX_ROOTS" in served.stderr


def test_a_directory_written_with_a_tilde_is_obeyed_as_the_directory_it_names(tmp_path):
    """Left as `~/code/one`, the code that uses it would read a folder named
    `~` in the project."""
    person = tmp_path / "person"
    (person / "code" / "one").mkdir(parents=True)

    served = _serve(tmp_path, file=f"GRIOT_MCP_INDEX_ROOTS={person / 'code'}\n",
                    GRIOT_MCP_INDEX_ROOTS="~/code/one", HOME=str(person))

    assert _value(served, "mcp-index-roots") == (str((person / "code" / "one").resolve()), "environment")


def test_every_value_a_server_obeys_is_in_the_form_it_was_measured_in(tmp_path):
    allowed = tmp_path / "code"
    (allowed / "one").mkdir(parents=True)

    served = _serve(tmp_path, file=(f"GRIOT_MCP_INDEX_ROOTS={allowed}\nGRIOT_GITEA_HOSTS=git.example.com\n"
                                    "GRIOT_SPEND_CEILING_USD=2.5\nGRIOT_MCP_ENABLE_INDEX=true\n"),
                    GRIOT_MCP_INDEX_ROOTS=f"{allowed}/one/", GRIOT_GITEA_HOSTS=" GIT.Example.com ",
                    GRIOT_SPEND_CEILING_USD=" 2 ", GRIOT_MCP_ENABLE_INDEX="OFF")

    assert _value(served, "mcp-index-roots") == (str((allowed / "one").resolve()), "environment")
    assert _value(served, "gitea-hosts") == ("git.example.com", "environment")
    assert _value(served, "spend-ceiling") == ("2", "environment") and served.spend["daily_ceiling_usd"] == 2.0
    assert _value(served, "mcp-index") == ("false", "environment") and "griot_index_repo" not in served.tools


def test_the_environment_cannot_send_the_token_of_your_own_gitlab_to_the_public_one(tmp_path):
    """Back to the default is another host too."""
    served = _serve(tmp_path, file="GRIOT_GITLAB_API_BASE=https://gitlab.example.com/api/v4\n",
                    GRIOT_GITLAB_API_BASE="https://gitlab.com/api/v4")

    assert _value(served, "gitlab-api-base") == ("https://gitlab.example.com/api/v4", "file")


def test_a_process_the_server_starts_inherits_the_narrowed_values(tmp_path):
    """An index run is another process: it reads the environment it is
    given, and the one it is given is the server's own."""
    code = ("import os, griot; griot.ENVIRONMENT_ONLY_NARROWS = True\n"
            "from griot import common\n"
            "print(os.environ.get('GRIOT_SPEND_CEILING_USD'), os.environ.get('GRIOT_MCP_ENABLE_INDEX'))\n")
    home = tmp_path / "home"
    (home / "config" / "griot").mkdir(parents=True)
    env_file = home / "config" / "griot" / ".env"
    env_file.write_text("GRIOT_SPEND_CEILING_USD=2.5\n")
    env_file.chmod(0o600)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(home / "config"), GRIOT_DATA_DIR=str(home / "data"),
               GRIOT_SPEND_CEILING_USD="100000", GRIOT_MCP_ENABLE_INDEX="true")
    (tmp_path / "project").mkdir()

    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=tmp_path / "project",
                          timeout=120)

    assert done.returncode == 0, done.stderr[-1500:]
    assert done.stdout.split() == ["2.5", "None"]


# === the command line at a terminal is not a server =============================================


def test_a_command_at_a_terminal_obeys_the_shell_it_runs_in(tmp_path):
    home = tmp_path / "home"
    (home / "config" / "griot").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(home / "config"), GRIOT_DATA_DIR=str(home / "data"), GRIOT_SPEND_CEILING_USD="50")

    done = subprocess.run([sys.executable, "-m", "griot.cli", "config", "list", "--json"], env=env, capture_output=True,
                          text=True, timeout=120)

    assert done.returncode == 0, done.stderr[-1500:]
    ceiling = next(entry for entry in json.loads(done.stdout) if entry["name"] == "spend-ceiling")
    assert (ceiling["value"], ceiling["source"]) == ("50", "environment")


# === the directories of a server are not the project's ==========================================


def _git(project):
    (project / ".git").mkdir(parents=True, exist_ok=True)


@pytest.mark.parametrize("entry", list(ENTRIES))
@pytest.mark.parametrize("variable", ["GRIOT_CONFIG_DIR", "GRIOT_DATA_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME"])
def test_a_server_does_not_start_with_a_directory_inside_the_project(tmp_path, entry, variable):
    project = tmp_path / "project"
    _git(project)
    outside = {"GRIOT_CONFIG_DIR": str(tmp_path / "home" / "config"), "GRIOT_DATA_DIR": str(tmp_path / "home" / "data")}
    dirs = {**outside, variable: str(project / ".griot")}
    if variable.startswith("XDG_"):
        del dirs["GRIOT_CONFIG_DIR" if "CONFIG" in variable else "GRIOT_DATA_DIR"]

    served = _serve(tmp_path, entry=entry, cwd=project, dirs=dirs)

    assert not served.started and served.returncode == 2
    assert "inside the project" in served.stderr and str(project) in served.stderr
    assert "Traceback" not in served.stderr, "one line that says what is wrong"
    assert served.stdout_lines == []
    assert not (project / ".griot").exists(), "and nothing was created there"


def test_a_relative_directory_is_inside_the_project(tmp_path):
    project = tmp_path / "project"
    _git(project)

    served = _serve(tmp_path, cwd=project, dirs={"GRIOT_CONFIG_DIR": ".griot", "GRIOT_DATA_DIR": str(tmp_path / "data")})

    assert not served.started and "inside the project" in served.stderr


def test_the_project_is_the_whole_repository_not_the_folder_the_server_started_in(tmp_path):
    project = tmp_path / "project"
    _git(project)
    (project / "packages" / "one").mkdir(parents=True)

    served = _serve(tmp_path, cwd=project / "packages" / "one",
                    dirs={"GRIOT_CONFIG_DIR": str(project / ".griot"), "GRIOT_DATA_DIR": str(tmp_path / "data")})

    assert not served.started and "inside the project" in served.stderr


def test_a_link_from_outside_into_the_project_is_inside_it(tmp_path):
    project = tmp_path / "project"
    _git(project)
    (project / ".griot").mkdir()
    (tmp_path / "looks-outside").symlink_to(project / ".griot")

    served = _serve(tmp_path, cwd=project,
                    dirs={"GRIOT_CONFIG_DIR": str(tmp_path / "looks-outside"), "GRIOT_DATA_DIR": str(tmp_path / "data")})

    assert not served.started and "inside the project" in served.stderr


def test_a_home_directory_the_environment_names_does_not_make_the_project_home(tmp_path):
    """HOME is part of the environment too: with it pointed at the project,
    griot's default directories would be the project's."""
    project = tmp_path / "project"
    _git(project)

    served = _serve(tmp_path, cwd=project, dirs={"HOME": str(project)})

    assert not served.started and "inside the project" in served.stderr


def test_a_directory_outside_the_project_is_the_person_s_own_business(tmp_path):
    project = tmp_path / "project"
    _git(project)

    served = _serve(tmp_path, cwd=project)

    assert served.started and served.returncode == 0


def test_a_folder_that_is_not_a_repository_is_a_project_too(tmp_path):
    project = tmp_path / "unpacked"
    project.mkdir()

    served = _serve(tmp_path, cwd=project, dirs={"GRIOT_CONFIG_DIR": str(project / "cfg"), "GRIOT_DATA_DIR": str(tmp_path / "d")})

    assert not served.started and "inside the project" in served.stderr


def test_a_command_at_a_terminal_may_keep_its_directories_wherever_it_is_told(tmp_path):
    project = tmp_path / "project"
    _git(project)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(project / ".griot"), GRIOT_DATA_DIR=str(project / ".griot-data"))

    done = subprocess.run([sys.executable, "-m", "griot.cli", "repos", "list"], env=env, capture_output=True, text=True,
                          cwd=project, timeout=120)

    assert done.returncode == 0, done.stderr[-1500:]


# === the rule itself, without a process =========================================================


@pytest.mark.parametrize("root,directory,inside", [
    ("/work/project", "/work/project/.griot", True),
    ("/work/project", "/work/project", True),
    ("/work/project", "/work/project-data", False),
    ("/work/project", "/work", False),
    ("/work/project", "/home/someone/.config/griot", False),
])
def test_inside_means_at_or_below(root, directory, inside):
    from pathlib import Path

    assert common._is_inside(Path(directory), Path(root)) is inside


def test_what_holds_the_person_s_own_home_is_not_a_project(tmp_path):
    """A server started in the home directory, or above it, is not serving
    something somebody handed over: the default directories are in there."""
    home = common._own_home()

    assert common._project_of(home, home) is None and common._project_of(home.parent, home) is None
    assert common._project_of(tmp_path, home) == tmp_path.resolve()


def test_a_home_directory_that_is_a_repository_does_not_make_everything_in_it_home(tmp_path):
    """Dotfiles kept in a repository at the home directory: an unpacked
    folder further down has that `.git` above it and is still a project."""
    home = (tmp_path / "home").resolve()
    (home / ".git").mkdir(parents=True)
    unpacked, cloned = home / "Downloads" / "unpacked", home / "code" / "cloned"
    unpacked.mkdir(parents=True)
    (cloned / ".git").mkdir(parents=True)
    (cloned / "src").mkdir()

    assert common._project_of(unpacked, home) == unpacked
    assert common._project_of(cloned / "src", home) == cloned
    assert common._project_of(home, home) is None


def test_the_place_griot_uses_by_default_is_never_the_project_s(tmp_path):
    """Someone who keeps `~/.config` in a repository and works in it has the
    default directory inside that "project", and it is still their own. A
    project cannot make a directory of its own be that place."""
    home = (tmp_path / "home").resolve()
    dotfiles = home / ".config"
    (dotfiles / ".git").mkdir(parents=True)

    assert common._is_the_project_s(dotfiles / "griot", dotfiles, home) is False
    assert common._is_the_project_s(home / ".local" / "share" / "griot", home / ".local", home) is False
    assert common._is_the_project_s(dotfiles / "other" / "griot", dotfiles, home) is True
    assert common._is_the_project_s(tmp_path / "elsewhere" / "griot", dotfiles, home) is False


def test_a_server_started_in_a_folder_that_is_gone_says_so_in_one_line(tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "XDG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"))

    done = subprocess.run(["sh", "-c", f'cd "{gone}" && rmdir "{gone}" && exec "{sys.executable}" -m griot.cli mcp'],
                          env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)

    assert done.returncode == 2 and "Traceback" not in done.stderr and done.stdout == ""
    assert "working directory" in done.stderr


def test_a_server_started_some_other_way_does_not_serve(tmp_path):
    """Only the two entry points read the configuration as a server does. A
    third way in would serve with the environment taken at its word, and
    say nothing."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "XDG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               GRIOT_SPEND_CEILING_USD="100000")

    done = subprocess.run([sys.executable, "-c", "from griot import mcp_server; mcp_server.main()"], env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)

    assert done.returncode != 0 and done.stdout == ""
    assert "griot mcp" in done.stderr


def test_the_home_directory_is_the_one_on_record_not_the_one_in_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert common._own_home() != tmp_path.resolve()


@pytest.mark.parametrize("word", ["yes", "on", "1", "TRUE"])
def test_indexing_turned_on_in_the_file_in_any_spelling_is_on_already(tmp_path, word):
    served = _serve(tmp_path, file=f"GRIOT_MCP_ENABLE_INDEX={word}\n", GRIOT_MCP_ENABLE_INDEX="true")

    assert "griot_index_repo" in served.tools and "ignored" not in served.stderr


def test_a_link_to_the_whole_filesystem_is_not_a_directory_an_agent_may_index(tmp_path):
    (tmp_path / "everything").symlink_to("/")

    served = _serve(tmp_path, file="GRIOT_MCP_ENABLE_INDEX=true\n", GRIOT_MCP_INDEX_ROOTS=str(tmp_path / "everything"))

    assert served.settings["mcp-index-roots"]["source"] == "default" and not served.settings["mcp-index-roots"]["value"]


@pytest.mark.parametrize("variable,odd", [("GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES", "²"),
                                          ("GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES", "٣"),
                                          ("GRIOT_SPEND_CEILING_USD", "\u0669\u0669")])
def test_a_digit_that_is_not_a_plain_one_is_a_value_the_setting_cannot_take(tmp_path, variable, odd):
    """`"²".isdigit()` is true and `int("²")` is an error: the check that
    was to turn a bad value into "ignored" let this one through to a
    traceback, and the server did not start."""
    served = _serve(tmp_path, **{variable: odd})

    assert served.started and "Traceback" not in served.stderr
    assert variable in served.stderr and "ignored" in served.stderr


def test_the_command_that_sets_a_count_refuses_such_a_digit_too():
    from griot import config

    with pytest.raises(config._NotValid):
        config.normalized(config.find("max-failed-batches"), "²")


def test_whatever_goes_wrong_while_measuring_a_value_the_value_is_not_obeyed(monkeypatch):
    """The rule, not the case that showed it: a value that cannot be
    measured is not known to be narrow."""
    from griot import config

    def broken(setting, raw):
        raise ValueError("something nobody thought of")

    monkeypatch.setattr(config, "normalized", broken)
    reason, value = config.measured_from_environment(config.find("spend-ceiling"), "1")

    assert reason and value == "1"
