"""`griot config` reads and changes griot's settings without an editor.

Every setting was a line in `<config>/.env`: to lower the spend ceiling one
had to know where the file is, what the variable is called and what a valid
value looks like, and a wrong one showed up at the next run. The command
names each setting, says what is in force and where it comes from, checks a
value before writing it, and asks a person before a change that widens what
griot may do or spend."""

import json
import os
import subprocess
import sys

import pytest
from dotenv import dotenv_values

from griot import common, config


def _in_file(variable: str) -> str | None:
    return dotenv_values(common.ENV_PATH).get(variable) if common.ENV_PATH.exists() else None


def _fresh(tmp_path, *argv, **extra) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"), **extra)
    return subprocess.run([sys.executable, "-m", "griot.cli", *argv], env=env, capture_output=True, text=True,
                          timeout=120, stdin=subprocess.DEVNULL)


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(common, "is_interactive", lambda: True)

    def answer(text):
        asked = []  # a new list each time: what was asked SINCE this answer was set
        monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or text)
        return asked

    return answer


@pytest.fixture
def no_question(monkeypatch):
    monkeypatch.setattr(common, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(f"asked: {prompt}"))


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(common, "is_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked without a terminal"))


# --- every setting is known to the command -------------------------------------------------


def test_every_setting_of_the_env_template_is_one_the_command_knows():
    """A variable added to the template and not here would be a setting the
    command silently does not show."""
    known = {setting.variable for setting in config.SETTINGS}
    template = {entry[0] for entry in common._ENV_TEMPLATE_SETTINGS}
    assert template <= known, template - known
    assert len({setting.name for setting in config.SETTINGS}) == len(config.SETTINGS), "names are unique"


def test_the_chat_profiles_it_accepts_are_the_ones_griot_has():
    """A copy, because the command works before the configuration loads."""
    assert tuple(config.CHAT_PROFILES) == tuple(common.CHAT_PROFILES)


def test_the_module_does_not_load_the_configuration_when_imported():
    done = subprocess.run([sys.executable, "-c", "import sys, griot.config; sys.exit('griot.common' in sys.modules)"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, "it is imported BEFORE the configuration, to be able to repair it"


def test_the_defaults_shown_are_the_ones_the_template_writes():
    template = {entry[0]: entry[1] for entry in common._ENV_TEMPLATE_SETTINGS}
    for setting in config.SETTINGS:
        if setting.variable in template:
            assert config.default_of(setting) == (template[setting.variable] or None), setting.name


# --- list and get -----------------------------------------------------------------------


def test_list_shows_each_setting_with_its_value_and_where_it_comes_from(capsys):
    assert config.main(["list"]) == 0
    out = capsys.readouterr().out
    assert str(common.ENV_PATH) in out
    for setting in config.SETTINGS:
        assert setting.name in out
    line = next(l for l in out.splitlines() if l.strip().startswith("spend-ceiling "))
    assert "3.0" in line and "default" in line


def test_list_says_file_or_environment_when_that_is_where_a_value_comes_from(capsys, monkeypatch, no_question):
    config.main(["set", "log-questions", "false"])
    monkeypatch.setenv("GRIOT_SPEND_VELOCITY_CEILING_USD", "0.25")
    capsys.readouterr()
    config.main(["list"])
    lines = {l.split()[0]: l for l in capsys.readouterr().out.splitlines() if l.strip()}
    assert "false" in lines["log-questions"] and "file" in lines["log-questions"]
    assert "0.25" in lines["spend-velocity-ceiling"] and "environment" in lines["spend-velocity-ceiling"]


def test_list_as_json_is_one_document_with_the_same_facts(capsys):
    assert config.main(["list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    by_name = {row["name"]: row for row in rows}
    assert by_name["spend-ceiling"] == {"name": "spend-ceiling", "variable": "GRIOT_SPEND_CEILING_USD", "value": "3.0",
                                        "source": "default", "default": "3.0",
                                        "description": by_name["spend-ceiling"]["description"]}
    assert by_name["openai-chat-price"]["value"] is None, "not set, and griot never guesses a price"


def test_get_prints_the_value_in_force_and_nothing_else(capsys, no_question):
    config.main(["set", "spend-ceiling", "1.5"])
    capsys.readouterr()
    assert config.main(["get", "spend-ceiling"]) == 0
    assert capsys.readouterr().out == "1.5\n"


def test_a_setting_can_be_named_by_its_variable(capsys):
    assert config.main(["get", "GRIOT_SPEND_CEILING_USD"]) == 0
    assert capsys.readouterr().out == "3.0\n"


@pytest.mark.parametrize("argv", [["get", "no-such-setting"], ["set", "no-such-setting", "1"], ["unset", "no-such-setting"]])
def test_an_unknown_name_is_a_usage_error_that_lists_the_names(argv, capsys):
    assert config.main(argv) == 2
    err = capsys.readouterr().err
    assert "no-such-setting" in err and "spend-ceiling" in err


# --- set: the value is checked before it is written ------------------------------------------


@pytest.mark.parametrize("name,value,variable,written", [
    ("spend-ceiling", "1.5", "GRIOT_SPEND_CEILING_USD", "1.5"),
    ("spend-velocity-ceiling", "0.5", "GRIOT_SPEND_VELOCITY_CEILING_USD", "0.5"),
    ("log-questions", "no", "GRIOT_LOG_QUESTIONS", "false"),
    ("log-questions", "TRUE", "GRIOT_LOG_QUESTIONS", "true"),
    ("max-failed-batches", "3", "GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES", "3"),
    ("mcp-concurrency", "single", "GRIOT_MCP_CONCURRENCY_MODE", "single"),
    ("mcp-idle-release", "60", "GRIOT_MCP_IDLE_RELEASE_SECONDS", "60"),
    ("chat-profile", "groq", "GRIOT_CHAT_PROFILE", "groq"),
    ("openai-chat-model", "gpt-4o", "GRIOT_OPENAI_CHAT_MODEL", "gpt-4o"),
    ("openai-chat-price", "0.6", "GRIOT_OPENAI_CHAT_PRICE_PER_1M_TOKENS", "0.6"),
    ("mcp-index", "false", "GRIOT_MCP_ENABLE_INDEX", "false"),
])
def test_a_valid_value_that_widens_nothing_is_written_without_a_question(no_question, name, value, variable, written):
    assert config.main(["set", name, value]) == 0
    assert _in_file(variable) == written


@pytest.mark.parametrize("name,value", [
    ("spend-ceiling", "nan"), ("spend-ceiling", "-1"), ("spend-ceiling", "abc"), ("spend-ceiling", "inf"),
    ("spend-velocity-ceiling", ""), ("log-questions", "maybe"), ("max-failed-batches", "0"),
    ("max-failed-batches", "2.5"), ("mcp-concurrency", "both"), ("mcp-idle-release", "-5"),
    ("chat-profile", "claude"), ("openai-chat-model", "two words"), ("openai-chat-model", ""),
    ("openai-chat-price", "free"), ("mcp-index", "sometimes"),
    ("gitlab-api-base", "not a url"), ("gitlab-api-base", "ftp://gitlab.example.com/api/v4"),
    ("gitlab-api-base", "https://user:secret@gitlab.example.com/api/v4"),
    ("gitlab-api-base", "http://gitlab.example.com/api/v4"),
    ("gitea-hosts", "git.example.com/path"), ("gitea-hosts", "https://git.example.com"),
    ("mcp-index-roots", "relative/dir"), ("mcp-index-roots", "/"),
])
def test_a_value_that_is_not_valid_for_the_setting_is_refused_and_nothing_is_written(terminal, capsys, name, value):
    terminal("y")
    before = common.ENV_PATH.read_text() if common.ENV_PATH.exists() else None
    assert config.main(["set", name, value]) == 2
    assert capsys.readouterr().err.startswith("Error: ")
    assert (common.ENV_PATH.read_text() if common.ENV_PATH.exists() else None) == before


def test_what_is_written_is_what_the_next_run_reads(tmp_path):
    assert _fresh(tmp_path, "config", "set", "spend-ceiling", "0.75").returncode == 0
    got = _fresh(tmp_path, "config", "get", "spend-ceiling")
    assert got.stdout == "0.75\n"
    check = subprocess.run(
        [sys.executable, "-c", "from griot import common; print(common.SPEND_CEILING_USD)"], capture_output=True,
        text=True, env={**{k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))},
                        "GRIOT_CONFIG_DIR": str(tmp_path / "config"), "GRIOT_DATA_DIR": str(tmp_path / "data")})
    assert check.stdout.strip() == "0.75"


def test_the_rest_of_the_file_is_kept_and_stays_private(no_question):
    config.main(["set", "log-questions", "false"])
    before = dotenv_values(common.ENV_PATH)
    config.main(["set", "spend-ceiling", "2"])
    after = dotenv_values(common.ENV_PATH)
    assert after.pop("GRIOT_SPEND_CEILING_USD") == "2"
    before.pop("GRIOT_SPEND_CEILING_USD", None)
    assert after == before and common.ENV_PATH.stat().st_mode & 0o777 == 0o600


def test_a_caller_inside_a_process_that_loaded_the_file_is_not_told_about_an_environment(no_question, capsys, monkeypatch):
    """Not through the CLI: the file's values are already in the process,
    loaded when the configuration was. They are not exported variables."""
    config.main(["set", "log-questions", "false"])
    monkeypatch.setenv("GRIOT_LOG_QUESTIONS", "false")  # what loading the file leaves behind
    capsys.readouterr()
    config.main(["set", "log-questions", "true"])
    assert "environment" not in capsys.readouterr().out


def test_a_value_already_in_the_file_is_not_written_again(no_question, capsys):
    config.main(["set", "spend-ceiling", "2"])
    stamp = common.ENV_PATH.stat().st_mtime_ns
    capsys.readouterr()
    assert config.main(["set", "spend-ceiling", "2"]) == 0
    assert common.ENV_PATH.stat().st_mtime_ns == stamp and "already" in capsys.readouterr().out


def test_it_says_that_a_running_server_keeps_the_old_value(no_question, capsys):
    config.main(["set", "spend-ceiling", "2"])
    out = " ".join(capsys.readouterr().out.split())
    assert "2" in out and "was 3.0" in out and "restart" in out.lower()


def test_a_variable_exported_in_the_environment_is_pointed_out(tmp_path):
    done = _fresh(tmp_path, "config", "set", "spend-ceiling", "1", GRIOT_SPEND_CEILING_USD="5")
    out = " ".join(done.stdout.split())
    assert done.returncode == 0 and "environment" in out and "wins" in out
    assert "GRIOT_SPEND_CEILING_USD='1'" in (tmp_path / "config" / "griot" / ".env").read_text()


# --- a change that widens something is a person's to make -----------------------------------

WIDENING = [
    ("spend-ceiling", "10", "GRIOT_SPEND_CEILING_USD"),
    ("spend-velocity-ceiling", "5", "GRIOT_SPEND_VELOCITY_CEILING_USD"),
    ("mcp-index", "true", "GRIOT_MCP_ENABLE_INDEX"),
    ("gitlab-api-base", "https://gitlab.example.com/api/v4", "GRIOT_GITLAB_API_BASE"),
    ("gitea-hosts", "git.example.com", "GRIOT_GITEA_HOSTS"),
]


@pytest.mark.parametrize("name,value,variable", WIDENING)
def test_a_widening_change_is_asked_about_and_a_yes_writes_it(terminal, name, value, variable):
    asked = terminal("y")
    assert config.main(["set", name, value]) == 0
    assert len(asked) == 1 and _in_file(variable) == value


@pytest.mark.parametrize("name,value,variable", WIDENING)
def test_a_no_changes_nothing(terminal, name, value, variable):
    terminal("n")
    before = _in_file(variable)
    assert config.main(["set", name, value]) == 1
    assert _in_file(variable) == before


@pytest.mark.parametrize("name,value,variable", WIDENING)
def test_without_a_terminal_a_widening_change_is_refused_and_no_flag_answers(no_terminal, capsys, name, value, variable):
    """An agent runs commands from a shell with no terminal. Raising the
    ceiling, turning on indexing through MCP, or pointing a token at another
    host is not its call."""
    before = _in_file(variable)
    assert config.main(["set", name, value]) == 2
    assert _in_file(variable) == before and "no flag" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        config.main(["set", name, value, "--yes"])


# What a change is measured against is what PERSISTS: the file, else the
# default. The environment wins while it is set, but it is the caller's own
# and gone with the shell: measured against it, exporting a wide value first
# got a wide value written to the file with no question.
EXPORTED_FIRST = [
    ("spend-ceiling", "50", {"GRIOT_SPEND_CEILING_USD": "100"}),
    ("spend-velocity-ceiling", "20", {"GRIOT_SPEND_VELOCITY_CEILING_USD": "100"}),
    ("mcp-index", "true", {"GRIOT_MCP_ENABLE_INDEX": "true"}),
    ("gitea-hosts", "git.example.com", {"GRIOT_GITEA_HOSTS": "git.example.com"}),
    ("gitlab-api-base", "https://gitlab.example.com/api/v4", {"GRIOT_GITLAB_API_BASE": "https://gitlab.example.com/api/v4"}),
]


@pytest.mark.parametrize("name,value,exported", EXPORTED_FIRST)
def test_exporting_the_wide_value_first_does_not_get_it_written_without_a_question(tmp_path, name, value, exported):
    done = _fresh(tmp_path, "config", "set", name, value, **exported)
    assert done.returncode == 2 and "no terminal" in done.stderr, done.stdout
    env_path = tmp_path / "config" / "griot" / ".env"
    assert not env_path.exists() or f"='{value}'" not in env_path.read_text()


def test_a_root_already_in_the_environment_is_still_a_root_being_added_to_the_file(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    done = _fresh(tmp_path, "config", "set", "mcp-index-roots", str(root), GRIOT_MCP_INDEX_ROOTS=str(root))
    assert done.returncode == 2 and "no terminal" in done.stderr


def test_an_unset_is_measured_against_the_file_too(tmp_path):
    """The file says 1, the shell exports 100: removing the line takes the
    persisted ceiling from 1 to the default of 3, which is a raise."""
    assert _fresh(tmp_path, "config", "set", "spend-ceiling", "1").returncode == 0
    done = _fresh(tmp_path, "config", "unset", "spend-ceiling", GRIOT_SPEND_CEILING_USD="100")
    assert done.returncode == 2 and "no terminal" in done.stderr
    assert "GRIOT_SPEND_CEILING_USD='1'" in (tmp_path / "config" / "griot" / ".env").read_text()


@pytest.mark.parametrize("in_file", ["nan", "inf", "lots"])
def test_a_ceiling_in_the_file_that_is_no_amount_does_not_make_every_raise_look_like_none(tmp_path, in_file):
    """Nothing compares as greater than `nan`, and nothing is greater than
    `inf`. The repair is measured against the default instead."""
    _fresh(tmp_path, "config", "set", "log-questions", "false")
    env_path = tmp_path / "config" / "griot" / ".env"
    kept = [line for line in env_path.read_text().splitlines() if not line.startswith("GRIOT_SPEND_CEILING_USD=")]
    env_path.write_text("\n".join(kept + [f"GRIOT_SPEND_CEILING_USD={in_file}"]) + "\n")
    raised = _fresh(tmp_path, "config", "set", "spend-ceiling", "50")
    assert raised.returncode == 2 and "no terminal" in raised.stderr
    assert _fresh(tmp_path, "config", "set", "spend-ceiling", "2").returncode == 0


def test_each_question_says_what_the_change_allows(terminal):
    said = {}
    for name, value, _ in WIDENING:
        asked = terminal("n")
        config.main(["set", name, value])
        said[name] = " ".join(asked[-1].split())
    assert "spent" in said["spend-ceiling"] and "10" in said["spend-ceiling"]
    assert "agent" in said["mcp-index"] and "index" in said["mcp-index"]
    assert "token" in said["gitlab-api-base"] and "gitlab.example.com" in said["gitlab-api-base"]
    assert "token" in said["gitea-hosts"] and "git.example.com" in said["gitea-hosts"]


def test_lowering_a_ceiling_or_turning_indexing_off_is_not_asked_about(terminal, no_question, monkeypatch):
    assert config.main(["set", "spend-ceiling", "1"]) == 0
    assert config.main(["set", "mcp-index", "false"]) == 0
    assert _in_file("GRIOT_SPEND_CEILING_USD") == "1"


def test_raising_is_measured_against_what_is_in_force(terminal):
    """From 1 to 2 is a raise even though 2 is below the default of 3."""
    terminal("y")
    config.main(["set", "spend-ceiling", "1"])
    asked = terminal("n")
    assert config.main(["set", "spend-ceiling", "2"]) == 1 and len(asked) == 1
    assert _in_file("GRIOT_SPEND_CEILING_USD") == "1"


def test_index_roots_are_asked_about_when_a_root_is_added_and_not_when_one_is_removed(terminal, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    asked = terminal("y")
    assert config.main(["set", "mcp-index-roots", f"{one}:{two}"]) == 0
    assert len(asked) == 1 and str(one) in asked[0]
    assert _in_file("GRIOT_MCP_INDEX_ROOTS") == f"{one}:{two}"
    asked = terminal("n")
    assert config.main(["set", "mcp-index-roots", str(one)]) == 0 and asked == [], "fewer roots: nothing to ask"
    assert _in_file("GRIOT_MCP_INDEX_ROOTS") == str(one)


def test_a_directory_inside_a_root_already_allowed_is_not_asked_about(terminal, tmp_path):
    """It widens nothing: everything under it could already be indexed."""
    root = tmp_path / "code"
    (root / "sub").mkdir(parents=True)
    terminal("y")
    config.main(["set", "mcp-index-roots", f"{root}/"])
    asked = terminal("n")
    assert config.main(["set", "mcp-index-roots", f"{root}:{root / 'sub'}"]) == 0 and asked == []


@pytest.mark.parametrize("already", ["TRUE", "1", "true"])
def test_indexing_already_on_in_the_file_under_another_spelling_is_not_asked_about(terminal, already):
    """`TRUE` and `1` turn it on for the reader. Writing `true` over them
    changes nothing."""
    common.ensure_env_template()
    common.env_file_set("GRIOT_MCP_ENABLE_INDEX", already)
    asked = terminal("n")
    assert config.main(["set", "mcp-index", "true"]) == 0 and asked == []


def test_a_relative_root_is_refused_even_when_it_exists(terminal, tmp_path, monkeypatch, capsys):
    """It would mean one directory here and another wherever the server runs."""
    (tmp_path / "code").mkdir()
    monkeypatch.chdir(tmp_path)
    terminal("y")
    assert config.main(["set", "mcp-index-roots", "code"]) == 2
    assert "relative" in capsys.readouterr().err and _in_file("GRIOT_MCP_INDEX_ROOTS") is None


def test_a_host_is_written_the_way_the_reader_compares_it(terminal, monkeypatch):
    """The reader takes the host of a remote in lower case and without a
    port. A host written otherwise was accepted, asked about, and then never
    matched anything."""
    from griot import platforms
    terminal("y")
    assert config.main(["set", "gitea-hosts", "Git.Example.com"]) == 0
    assert _in_file("GRIOT_GITEA_HOSTS") == "git.example.com"
    monkeypatch.setenv("GRIOT_GITEA_HOSTS", _in_file("GRIOT_GITEA_HOSTS"))
    assert platforms._gitea_matches("https://git.example.com/group/project.git") is True


def test_an_address_with_colons_is_not_called_a_port(terminal, capsys):
    terminal("y")
    assert config.main(["set", "gitea-hosts", "::1"]) == 2
    assert "port" not in capsys.readouterr().err


def test_a_host_with_a_port_is_refused(terminal, capsys):
    terminal("y")
    assert config.main(["set", "gitea-hosts", "git.example.com:3000"]) == 2
    assert "port" in capsys.readouterr().err and _in_file("GRIOT_GITEA_HOSTS") in (None, "")


def test_removing_a_host_is_not_asked_about(terminal):
    terminal("y")
    config.main(["set", "gitea-hosts", "git.example.com,forge.example.org"])
    asked = terminal("n")
    assert config.main(["set", "gitea-hosts", "git.example.com"]) == 0 and asked == []


# --- settings that have their own command, or are not this file's -----------------------------


def test_the_embedding_profile_is_shown_and_changed_by_its_own_command(no_question, capsys):
    assert config.main(["get", "embed-profile"]) == 0
    capsys.readouterr()
    assert config.main(["set", "embed-profile", "bge-small"]) == 2
    assert "griot profiles use" in capsys.readouterr().err
    assert _in_file("GRIOT_EMBED_PROFILE") != "bge-small"


def test_the_project_name_is_per_project_and_not_set_here(no_question, capsys):
    assert config.main(["set", "project", "anything"]) == 2
    assert "per project" in capsys.readouterr().err


def test_a_chat_profile_without_a_price_or_a_credential_says_what_is_missing(no_question, capsys, monkeypatch):
    monkeypatch.delenv("GRIOT_OPENAI_API_KEY", raising=False)
    assert config.main(["set", "chat-profile", "openai"]) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "openai-chat-price" in out and "griot auth set openai" in out


# --- unset ------------------------------------------------------------------------------


def test_unset_removes_the_line_and_the_default_is_back(no_question, capsys):
    config.main(["set", "log-questions", "false"])
    capsys.readouterr()
    assert config.main(["unset", "log-questions"]) == 0
    assert _in_file("GRIOT_LOG_QUESTIONS") is None
    assert "default" in capsys.readouterr().out
    config.main(["get", "log-questions"])
    assert capsys.readouterr().out == "true\n"


def test_after_an_unset_nothing_is_said_about_an_environment_that_was_not_set(tmp_path):
    """The file's value is loaded into the process when griot starts. After
    the line is removed that copy is still there, and it was reported as
    "also set in the environment": a variable the user never exported."""
    assert _fresh(tmp_path, "config", "set", "mcp-concurrency", "single").returncode == 0
    done = _fresh(tmp_path, "config", "unset", "mcp-concurrency")
    assert done.returncode == 0 and "environment" not in done.stdout, done.stdout
    assert "environment" not in _fresh(tmp_path, "config", "set", "log-questions", "false").stdout


def test_a_line_that_is_there_and_empty_is_shown_as_empty_and_can_be_removed(no_question, capsys):
    """`VAR=` is not "not set": griot reads it as an empty value. Shown as
    the default, the command described a configuration griot is not using."""
    common.ensure_env_template()
    with open(common.ENV_PATH, "a") as f:
        f.write("GRIOT_CHAT_MODEL=\n")
    config.main(["list"])
    line = next(l for l in capsys.readouterr().out.splitlines() if l.strip().startswith("chat-model "))
    assert "(empty)" in line and "file" in line
    assert config.main(["unset", "chat-model"]) == 0
    assert "GRIOT_CHAT_MODEL=\n" not in common.ENV_PATH.read_text().replace("#GRIOT", "#")


def test_unsetting_what_is_not_in_the_file_says_so(no_question, capsys):
    config.main(["unset", "mcp-concurrency"])
    capsys.readouterr()
    assert config.main(["unset", "mcp-concurrency"]) == 0
    assert "not set" in capsys.readouterr().out


def test_unsetting_back_to_a_default_that_widens_is_asked_about_too(terminal):
    """A ceiling lowered to 1 and then unset is a ceiling raised to 3."""
    terminal("y")
    config.main(["set", "spend-ceiling", "1"])
    asked = terminal("n")
    assert config.main(["unset", "spend-ceiling"]) == 1 and len(asked) == 1
    assert _in_file("GRIOT_SPEND_CEILING_USD") == "1"


# --- a file with a bad value: one line, and this command repairs it ----------------------------


@pytest.mark.parametrize("name,variable,bad,good", [
    ("spend-ceiling", "GRIOT_SPEND_CEILING_USD", "lots", "1"),
    ("chat-profile", "GRIOT_CHAT_PROFILE", "claude", "gemini"),
    ("mcp-concurrency", "GRIOT_MCP_CONCURRENCY_MODE", "both", "multi"),
])
def test_a_bad_value_in_the_file_is_one_line_and_set_repairs_it(tmp_path, name, variable, bad, good):
    assert _fresh(tmp_path, "config", "set", "log-questions", "false").returncode == 0
    env_path = tmp_path / "config" / "griot" / ".env"
    kept = [line for line in env_path.read_text().splitlines() if not line.startswith(f"{variable}=")]
    env_path.write_text("\n".join(kept + [f"{variable}={bad}"]) + "\n")

    broken = _fresh(tmp_path, "stats")
    assert broken.returncode == 2 and broken.stderr.startswith("Error: ") and "Traceback" not in broken.stderr
    assert variable in broken.stderr and "griot config set" in broken.stderr

    assert _fresh(tmp_path, "config", "set", name, good).returncode == 0
    assert _fresh(tmp_path, "stats").returncode == 0


@pytest.mark.parametrize("name,variable,bad", [
    ("openai-chat-price", "GRIOT_OPENAI_CHAT_PRICE_PER_1M_TOKENS", "free"),
    ("chat-profile", "GRIOT_CHAT_PROFILE", "claude"),
    ("max-failed-batches", "GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES", "many"),
    ("mcp-idle-release", "GRIOT_MCP_IDLE_RELEASE_SECONDS", ""),
])
def test_unset_repairs_a_bad_value_too(tmp_path, name, variable, bad):
    """Removing the line is the natural repair, and it was a dead end: the
    bad value stopped griot before the command could run."""
    _fresh(tmp_path, "config", "set", "log-questions", "false")
    env_path = tmp_path / "config" / "griot" / ".env"
    kept = [line for line in env_path.read_text().splitlines() if not line.startswith(f"{variable}=")]
    env_path.write_text("\n".join(kept + [f"{variable}={bad}"]) + "\n")
    broken = _fresh(tmp_path, "stats")
    assert broken.returncode == 2 and "griot config unset" in broken.stderr
    done = _fresh(tmp_path, "config", "unset", name)
    assert done.returncode == 0, done.stderr
    assert f"{variable}=" not in env_path.read_text().replace(f"#{variable}=", "")
    assert _fresh(tmp_path, "stats").returncode == 0


def test_setting_a_bad_value_through_the_command_line_is_the_commands_own_error(tmp_path):
    done = _fresh(tmp_path, "config", "set", "spend-ceiling", "lots")
    assert done.returncode == 2 and done.stderr.startswith("Error: ") and "Traceback" not in done.stderr
    assert _fresh(tmp_path, "stats").returncode == 0, "nothing was written"


# --- the surface -------------------------------------------------------------------------


def test_the_help_lists_the_names_even_when_the_file_is_broken(tmp_path):
    """`list` cannot run then, and the names are what the repair needs."""
    _fresh(tmp_path, "config", "set", "log-questions", "false")
    env_path = tmp_path / "config" / "griot" / ".env"
    env_path.write_text(env_path.read_text() + "GRIOT_MCP_CONCURRENCY_MODE=both\n")
    done = _fresh(tmp_path, "config", "--help")
    out = "".join(done.stdout.split())  # the help is wrapped, and it wraps at hyphens
    assert done.returncode == 0, done.stderr
    assert "spend-ceiling" in out and "mcp-concurrency" in out


def test_griot_help_lists_the_command(tmp_path):
    assert "config" in _fresh(tmp_path, "--help").stdout


@pytest.mark.anyio
async def test_no_mcp_tool_changes_a_setting():
    from mcp.client.client import Client
    from griot import mcp_server
    async with Client(mcp_server.mcp) as client:
        names = [t.name for t in (await client.list_tools()).tools]
    assert not [name for name in names if "config" in name or "setting" in name]
