"""The decisions `griot assist install` takes a person through, and in what order.

It asked about the instructions block first, then the server, then the tools:
the first tells an agent to use tools that only exist once the server is
registered, and the last allows tools of a server that may not be there. It
found out that a server was already registered only when registering failed,
and never looked at what the registration ran. And a bare `griot assist
install` installed into whatever directory one happened to be in.

Now: for every project unless told otherwise; the server first, looked at
before anything is asked; the tools and the instructions only when the
server is there; and a summary of what was done and what comes next."""

from pathlib import Path

import pytest

from griot import common, harnesses

GRIOT = "/opt/tools/griot"
SCOPE_LABEL = {"user": "User config (available in all your projects)",
               "local": "Local config (private to you in this project)",
               "project": "Project config (shared via .mcp.json)"}


class _Cli:
    """The harness's own command line: remembers what is registered and
    answers `mcp get`, `mcp add` and `mcp remove` the way the real one does."""

    def __init__(self, registered=None, garbled=False, args="mcp", fail=()):
        self.calls, self.registered, self.garbled, self.args, self.fail = [], registered, garbled, args, fail

    def __call__(self, argv):
        self.calls.append(list(argv))
        done = lambda out="", rc=0: type("Done", (), {"returncode": rc, "stdout": out, "stderr": ""})()  # noqa: E731
        action = argv[2]
        if action == "get":
            if self.garbled:
                return done("something this version of griot does not understand")
            if self.registered is None:
                return done('No MCP server named "griot". Configured servers: other')
            scope, command = self.registered
            if command is None:  # a server reached over the network: no command at all
                return done(f"griot:\n  Scope: {SCOPE_LABEL[scope]}\n  Status: ✔ Connected\n  Type: http\n"
                            f"  URL: https://example.invalid/mcp\n")
            return done(f"griot:\n  Scope: {SCOPE_LABEL[scope]}\n  Status: ✔ Connected\n  Type: stdio\n"
                        f"  Command: {command}\n  Args: {self.args}\n  Environment:\n")
        if action in self.fail:
            return done("the harness said no", rc=1)
        if action == "add":
            self.registered = (argv[argv.index("--scope") + 1], argv[argv.index("--") + 1])
            return done()
        if action == "remove":
            self.registered = None
            return done()
        raise AssertionError(f"unexpected harness command: {argv}")

    def did(self, action):
        return [call for call in self.calls if call[2] == action]


@pytest.fixture
def claude():
    return next(h for h in harnesses.HARNESSES if h.id == "claude-code")


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A home directory, a griot with a path that stays, and a harness CLI."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    # Like the real one: a bare name is looked up on the PATH, and anything
    # with a directory in it is a file that is not there.
    monkeypatch.setattr(harnesses, "_which", lambda name: None if "/" in name else f"/usr/local/bin/{name}")
    monkeypatch.setattr(harnesses, "_griot_command", lambda: GRIOT)
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "multi")

    def use(cli):
        monkeypatch.setattr(harnesses, "_run_harness_command", cli)
        return cli

    return type("Machine", (), {"home": home, "use": staticmethod(use)})()


@pytest.fixture
def answers(monkeypatch):
    """Answers a person gives, in order; records each question asked."""
    def give(*replies):
        asked, queue = [], list(replies)
        monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(" ".join(prompt.split())) or queue.pop(0))
        return asked
    return give


def _install(*argv):
    return harnesses.main(["install", "--harness", "claude-code", *argv])


# --- where ------------------------------------------------------------------------------------


def test_a_bare_install_is_for_every_project(machine, answers):
    machine.use(_Cli())
    answers("n")
    assert _install() == 0
    assert (machine.home / ".claude" / "skills").is_dir()
    assert not (Path.cwd() / ".claude").exists(), "not into whatever directory one happens to be in"


def test_this_project_only_is_asked_for_by_name(machine, answers):
    machine.use(_Cli())
    answers("n")
    assert _install("--scope", "local") == 0
    assert (Path.cwd() / ".claude" / "skills").is_dir() and not (machine.home / ".claude").exists()


# --- the order, and what depends on what ----------------------------------------------------------


def test_the_server_is_asked_about_first_then_the_tools_then_the_instructions(machine, answers):
    cli = machine.use(_Cli())
    asked = answers("y", "y", "y")
    assert _install() == 0
    assert len(asked) == 3
    assert "Register it?" in asked[0] and "settings.json" in asked[1] and "CLAUDE.md" in asked[2]
    assert cli.did("add") == [["claude", "mcp", "add", "--scope", "user", "griot", "--", GRIOT, "mcp"]]
    assert "griot_search" in (machine.home / ".claude" / "settings.json").read_text()
    assert "griot:begin" in (machine.home / ".claude" / "CLAUDE.md").read_text()


def test_without_the_server_the_tools_and_the_instructions_are_not_offered(machine, answers, capsys):
    """Rules for a server that is not there, and instructions to use tools
    that do not exist, are worse than nothing."""
    machine.use(_Cli())
    asked = answers("n")
    assert _install() == 0
    assert len(asked) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "not offered" in out and "MCP server is not registered" in out
    assert not (machine.home / ".claude" / "settings.json").exists()
    assert not (machine.home / ".claude" / "CLAUDE.md").exists()


def test_without_a_terminal_nothing_is_asked_and_the_command_is_shown(machine, monkeypatch, capsys):
    cli = machine.use(_Cli())
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked without a terminal"))
    assert _install() == 0
    out = " ".join(capsys.readouterr().out.split())
    assert f"claude mcp add --scope user griot -- {GRIOT} mcp" in out and "not offered" in out
    assert cli.did("add") == []


# --- what is already registered is looked at before anything is asked -----------------------------


def test_a_server_already_registered_for_this_griot_is_not_asked_about(machine, answers, capsys):
    cli = machine.use(_Cli(registered=("user", GRIOT)))
    asked = answers("y", "y")
    assert _install() == 0
    assert len(asked) == 2 and "Register it?" not in asked[0], "straight to the tools and the instructions"
    assert cli.did("add") == [] and cli.did("remove") == []
    assert "already registered" in " ".join(capsys.readouterr().out.split())


def test_a_registration_whose_command_is_gone_is_offered_to_be_replaced(machine, answers, capsys):
    cli = machine.use(_Cli(registered=("user", "/gone/with/an/old/venv/griot")))
    asked = answers("y", "n", "n")
    assert _install() == 0
    assert "Replace" in asked[0]
    assert "/gone/with/an/old/venv/griot" in capsys.readouterr().out, "what is being replaced is shown first"
    assert cli.did("remove") == [["claude", "mcp", "remove", "--scope", "user", "griot"]]
    assert cli.did("add") == [["claude", "mcp", "add", "--scope", "user", "griot", "--", GRIOT, "mcp"]]
    assert cli.calls.index(cli.did("remove")[0]) < cli.calls.index(cli.did("add")[0])


def test_declining_the_replacement_leaves_it_and_offers_nothing_more(machine, answers, capsys):
    cli = machine.use(_Cli(registered=("user", "/gone/griot")))
    asked = answers("n")
    assert _install() == 0
    assert len(asked) == 1 and cli.did("remove") == [] and cli.did("add") == []
    assert "not offered" in " ".join(capsys.readouterr().out.split()), "a server that cannot start is no server"


def test_a_registration_that_runs_another_griot_that_exists_is_left_alone_and_said(machine, answers, tmp_path, capsys):
    other = tmp_path / "other-griot"
    other.write_text("#!/bin/sh\n")
    cli = machine.use(_Cli(registered=("user", str(other))))
    asked = answers("n", "n")
    assert _install() == 0
    assert cli.did("add") == [] and cli.did("remove") == []
    out = " ".join(capsys.readouterr().out.split())
    assert str(other) in out and "not this griot" in out
    assert len(asked) == 2, "it is a working server: the tools and the instructions are offered"


def test_a_registration_for_one_project_does_not_count_for_every_project(machine, answers):
    cli = machine.use(_Cli(registered=("local", GRIOT)))
    asked = answers("y", "n", "n")
    assert _install() == 0
    assert "Register it?" in asked[0]
    assert cli.did("add") == [["claude", "mcp", "add", "--scope", "user", "griot", "--", GRIOT, "mcp"]]
    assert cli.did("remove") == [], "the project's own registration is not touched"


def test_a_registration_for_every_project_covers_an_install_into_one(machine, answers):
    cli = machine.use(_Cli(registered=("user", GRIOT)))
    asked = answers("n")
    assert _install("--scope", "local") == 0
    assert cli.did("add") == [] and "Register it?" not in " ".join(asked)


def test_an_answer_griot_cannot_read_falls_back_to_asking(machine, answers):
    cli = machine.use(_Cli(garbled=True))
    asked = answers("y", "n", "n")
    assert _install() == 0
    assert "Register it?" in asked[0] and len(cli.did("add")) == 1


def test_what_the_harness_says_is_read_as_three_different_things(machine, claude):
    """Registered (with its scope and command), not registered, and "could
    not tell": the last is not the same as the second."""
    machine.use(_Cli(registered=("local", "/somewhere/griot")))
    assert harnesses.mcp_registration(claude) == {"scope": "local", "command": "/somewhere/griot", "args": "mcp"}
    machine.use(_Cli())
    assert harnesses.mcp_registration(claude) == {}
    machine.use(_Cli(garbled=True))
    assert harnesses.mcp_registration(claude) is None


def test_a_harness_that_cannot_be_asked_falls_back_to_asking_too(machine, answers, monkeypatch):
    def broken(argv):
        if argv[2] == "get":
            raise OSError("no such file")
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(harnesses, "_run_harness_command", broken)
    asked = answers("y", "n", "n")
    assert _install() == 0 and "Register it?" in asked[0]


# --- the flags ----------------------------------------------------------------------------------


def test_the_flag_that_registers_also_replaces_a_dead_registration_without_asking(machine, monkeypatch):
    cli = machine.use(_Cli(registered=("user", "/gone/griot")))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")  # the two questions that have no such flag
    assert _install("--mcp") == 0
    assert len(cli.did("remove")) == 1 and len(cli.did("add")) == 1


def test_told_not_to_touch_the_server_it_is_not_even_looked_at(machine, answers):
    """Whoever passes --no-mcp looks after the server themselves: the tools
    and the instructions are still offered."""
    cli = machine.use(_Cli())
    asked = answers("n", "n")
    assert _install("--no-mcp") == 0
    assert cli.calls == [] and len(asked) == 2


def test_skills_only_copies_the_files_and_asks_nothing(machine, monkeypatch, capsys):
    cli = machine.use(_Cli())
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked although told only to copy"))
    assert _install("--skills-only") == 0
    assert cli.calls == [] and (machine.home / ".claude" / "skills").is_dir()
    assert not (machine.home / ".claude" / "settings.json").exists()


def test_skills_only_and_the_flag_that_registers_do_not_go_together(machine):
    with pytest.raises(SystemExit):
        _install("--skills-only", "--mcp")


def test_no_harness_on_the_machine_is_a_failed_install(machine, monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "detect_harnesses", lambda: [])
    assert harnesses.main(["install"]) == 1
    assert "No supported harness" in capsys.readouterr().err


# --- the summary --------------------------------------------------------------------------------


def test_it_ends_with_what_was_done_and_what_comes_next(machine, answers, capsys):
    machine.use(_Cli())
    answers("y", "y", "n")
    assert _install() == 0
    out = capsys.readouterr().out
    summary = " ".join(out[out.index("Summary"):].split())
    assert "skills and agent" in summary
    assert "MCP server: registered for every project" in summary
    assert "read-only tools: allowed" in summary
    assert "instructions: not added" in summary
    assert "Restart" in summary, "a session that is already open has neither the server nor the rules"
    assert "griot repos add" in summary, "nothing is registered to index on this machine yet"


def test_the_summary_says_what_was_left_out_and_why(machine, answers, capsys):
    machine.use(_Cli())
    answers("n")
    _install()
    out = capsys.readouterr().out
    summary = " ".join(out[out.index("Summary"):].split())
    assert "MCP server: not registered" in summary and "Restart" not in summary


def test_with_repositories_registered_the_next_step_is_to_index(machine, answers, capsys, tmp_path):
    from griot import repos
    (tmp_path / "app").mkdir()
    repos.add_repo(str(tmp_path / "app"))
    machine.use(_Cli(registered=("user", GRIOT)))
    answers("n", "n")
    _install()
    out = capsys.readouterr().out
    summary = " ".join(out[out.index("Summary"):].split())
    assert "griot index all" in summary and "griot repos add" not in summary


# --- what the first review found ---------------------------------------------------------------


@pytest.mark.parametrize("command,args", [
    ("griot", "mcp"),               # found through the PATH by the harness, not a file in the current directory
    ("uv", "run griot mcp"),        # a wrapper
    ("./bin/griot", "mcp"),         # relative to wherever the harness starts it
    ("./griot", "mcp"),             # the same, one level up: not a bare name
    ("~/bin/griot", "mcp"),
])
def test_a_registration_that_works_but_is_not_an_absolute_path_is_not_called_dead(machine, monkeypatch, capsys, command, args):
    """`--mcp` replaced these without asking: a working registration deleted
    because its command was looked for as a file in the current directory."""
    cli = machine.use(_Cli(registered=("user", command), args=args))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert _install("--mcp") == 0
    assert cli.did("remove") == [] and cli.did("add") == []
    assert "left as it is" in " ".join(capsys.readouterr().out.split())


def test_a_bare_command_that_is_nowhere_on_the_path_is_dead(machine, answers, monkeypatch):
    monkeypatch.setattr(harnesses, "_which", lambda name: None if name == "old-griot" else f"/usr/local/bin/{name}")
    cli = machine.use(_Cli(registered=("user", "old-griot")))
    asked = answers("n")
    _install()
    assert "Replace" in asked[0] and cli.did("remove") == []


def test_this_griot_started_with_other_arguments_is_not_this_server(machine, answers, capsys):
    """Same path, but it does not run `griot mcp`: not "running this griot"."""
    cli = machine.use(_Cli(registered=("user", GRIOT), args="--profile x mcp"))
    answers("n", "n")
    _install()
    out = " ".join(capsys.readouterr().out.split())
    assert "running this griot" not in out and "--profile x mcp" in out and "left as it is" in out
    assert cli.did("add") == []


def test_a_server_named_griot_that_is_not_a_command_is_left_to_the_fallback(machine, answers):
    cli = machine.use(_Cli(registered=("user", None), fail=("add",)))
    asked = answers("y")
    assert _install() == 0
    assert "Register it?" in asked[0] and cli.did("remove") == []


def test_replacing_says_what_it_removes_and_where(machine, answers, capsys):
    machine.use(_Cli(registered=("user", "/gone/griot")))
    answers("n")
    _install()
    out = " ".join(capsys.readouterr().out.split())
    assert "claude mcp remove --scope user griot" in out and f"claude mcp add --scope user griot -- {GRIOT} mcp" in out


def test_an_install_into_one_project_does_not_remove_what_is_registered_for_every_project(machine, answers, capsys):
    """A dead registration for every project is replaced by the install for
    every project. Asked for this project only, griot adds this project's
    own and says the other one is broken."""
    cli = machine.use(_Cli(registered=("user", "/gone/griot")))
    asked = answers("y", "n")
    assert _install("--scope", "local") == 0
    assert "Register it?" in asked[0] and cli.did("remove") == []
    assert cli.did("add") == [["claude", "mcp", "add", "--scope", "local", "griot", "--", GRIOT, "mcp"]]
    out = " ".join(capsys.readouterr().out.split())
    assert "/gone/griot" in out and "every project" in out


@pytest.mark.parametrize("scope", ["local", "project"])
def test_a_dead_registration_of_this_project_is_said_to_still_win_here(machine, answers, capsys, scope):
    """Registered for every project, and in THIS one the project's own entry
    still takes precedence and still cannot start."""
    cli = machine.use(_Cli(registered=(scope, "/gone/griot")))
    answers("y", "n", "n")
    assert _install() == 0
    assert cli.did("remove") == [], "griot does not edit a project's own registration from a global install"
    out = " ".join(capsys.readouterr().out.split())
    assert "takes precedence" in out and "/gone/griot" in out
    summary = out[out.index("Summary"):]
    assert "registered for every project" in summary and "in this project" in summary and "still comes first" in summary


def test_a_dead_project_file_registration_is_not_said_to_be_replaceable(machine, answers, capsys):
    """An install for every project replaces what is registered for every
    project. It never removes an entry of a project's .mcp.json, so the
    install into one project must not promise that."""
    cli = machine.use(_Cli(registered=("project", "/gone/griot")))
    answers("n")
    _install("--scope", "local")
    out = " ".join(capsys.readouterr().out.split())
    assert cli.did("remove") == [] and "/gone/griot" in out and "offers to replace" not in out


def test_without_a_terminal_a_dead_registration_is_not_summed_up_as_none(machine, monkeypatch, capsys):
    machine.use(_Cli(registered=("user", "/gone/griot")))
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    assert _install() == 0
    out = " ".join(capsys.readouterr().out.split())
    summary = out[out.index("Summary"):]
    assert "claude mcp remove --scope user griot" in out, "both commands are shown"
    assert "its command is gone" in summary and "MCP server: not registered" not in summary


def test_when_the_old_one_is_removed_and_the_new_one_refused_it_says_nothing_is_registered(machine, answers, capsys):
    cli = machine.use(_Cli(registered=("user", "/gone/griot"), fail=("add",)))
    answers("y")
    assert _install() == 0
    out = " ".join(capsys.readouterr().out.split())
    assert len(cli.did("remove")) == 1 and "removed" in out and "not registered now" in out


def test_when_the_old_one_cannot_be_removed_nothing_is_added(machine, answers, capsys):
    cli = machine.use(_Cli(registered=("user", "/gone/griot"), fail=("remove",)))
    answers("y")
    _install()
    assert cli.did("add") == [] and "nothing was changed" in " ".join(capsys.readouterr().out.split())


def test_without_the_harness_command_the_other_steps_are_still_offered(machine, answers, monkeypatch, capsys):
    """Nothing could be looked at, so nothing is known: the server may well
    be registered some other way. Withholding the rest would be a guess."""
    monkeypatch.setattr(harnesses, "_which", lambda name: None)
    asked = answers("n", "n")
    assert _install() == 0
    assert len(asked) == 2
    assert "not offered" not in " ".join(capsys.readouterr().out.split())


def test_a_configuration_griot_cannot_start_with_does_not_break_an_install_that_worked(machine, answers, monkeypatch, capsys):
    """The summary reads griot's configuration for the next steps. By then
    the files are written and the server is registered."""
    import builtins
    real_import = builtins.__import__

    def broken(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "griot" and "common" in (fromlist or ()):
            raise ValueError("GRIOT_SPEND_CEILING_USD must be a finite number")
        return real_import(name, globals, locals, fromlist, level)

    machine.use(_Cli(registered=("user", GRIOT)))
    answers("n", "n")
    monkeypatch.setattr(harnesses, "_model_note", lambda: "")
    monkeypatch.setattr(builtins, "__import__", broken)
    assert _install() == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "Summary" in out and "griot config" in out


@pytest.mark.parametrize("tools,instructions,expected", [
    ("unsafe", "unsafe", ["not touched", "not touched"]),
    ("malformed", "malformed", ["not touched", "not touched"]),
    ("failed", "declined", ["could not be written", "not added (declined)"]),
    ("skipped", "skipped", ["--no-allow-tools", "--no-instructions"]),
])
def test_every_outcome_has_words_in_the_summary(capsys, tools, instructions, expected):
    files = {"harness": "claude-code", "skills_target": "/x/skills", "agents_target": "/x/agents",
             "created": [], "updated": [], "unchanged": ["a"]}
    harnesses._summary([{"files": files, "server": "declined", "tools": tools, "instructions": instructions}], "global")
    lines = capsys.readouterr().out.splitlines()
    assert expected[0] in next(l for l in lines if "read-only tools:" in l)
    assert expected[1] in next(l for l in lines if "instructions:" in l)


def test_nothing_changed_means_nothing_to_restart_for(machine, answers, capsys):
    machine.use(_Cli(registered=("user", GRIOT)))
    answers("n", "n")
    _install()                      # writes the skills
    capsys.readouterr()
    answers("n", "n")
    _install()                      # nothing new this time
    out = capsys.readouterr().out
    assert "Restart" not in out[out.index("Summary"):]
