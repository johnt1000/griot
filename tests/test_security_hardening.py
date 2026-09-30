"""Three places where a value griot does not control decided more than it
should: a collection name that became a path, an install destination that
was a symbolic link, and a spend ceiling that was not a number."""

import os
import subprocess
import sys

import pytest

from griot import common, harnesses, logdb

# --- a collection name is a name, never a path ------------------------------------------

NOT_A_NAME = ["/etc", "../outside", "a/b", "..", ".", "", "a\\b", "a\x00b", "~", "~/x"]


@pytest.mark.parametrize("name", NOT_A_NAME, ids=repr)
def test_a_collection_name_that_is_not_one_path_component_is_refused(name):
    with pytest.raises(ValueError, match="collection"):
        common._collection_path(name)


@pytest.mark.parametrize("name", ["codebase__jina-code", "codebase__never-indexed"])
def test_an_ordinary_collection_name_stays_under_the_data_directory(name):
    assert common._collection_path(name).parent == common.QDRANT_PATH


def test_status_refuses_a_path_instead_of_opening_it(tmp_path):
    """The name came straight from the agent and was joined to the data
    directory: an absolute path replaced it, `..` walked out of it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / common._EDGE_CONFIG_MARKER).write_text("{}")
    with pytest.raises(ValueError, match="collection"):
        common.get_index_status(str(elsewhere))


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["/etc", "../outside", "codebase__not-a-profile", "anything"])
async def test_the_tool_accepts_only_the_collection_of_a_known_profile(name):
    from mcp.client.client import Client
    from griot import mcp_server
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_index_status", {"collection": name})
    assert result.is_error is True
    assert common.collection_name_for("jina-code") in str(result.content), "the refusal names what is valid"


@pytest.mark.anyio
async def test_the_tool_still_reports_the_collection_of_another_profile():
    from mcp.client.client import Client
    from griot import mcp_server
    other = next(p for p in common.EMBED_PROFILES if p != common.ACTIVE_PROFILE_NAME)
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_index_status", {"collection": common.collection_name_for(other)})
        default = await client.call_tool("griot_index_status", {})
    assert result.is_error is False and result.structured_content["points_count"] == 0
    assert default.is_error is False
    assert not common.collection_exists(common.collection_name_for(other)), "asking must not create it"


# --- the installer does not write through a symbolic link --------------------------------


def _resources(tmp_path, monkeypatch):
    root = tmp_path / "packaged"
    (root / "skills" / "griot-onboarding").mkdir(parents=True)
    (root / "skills" / "griot-onboarding" / "SKILL.md").write_text("# skill\n")
    (root / "skills" / "griot-indexing").mkdir(parents=True)
    (root / "skills" / "griot-indexing" / "SKILL.md").write_text("# another skill\n")
    (root / "agents" / "fake").mkdir(parents=True)
    (root / "agents" / "fake" / "griot-setup-assistant.md").write_text("# agent\n")
    monkeypatch.setattr(harnesses, "_resources_root", lambda: root)


def _harness(name="fake"):
    return harnesses.Harness(
        id=name, display_name=name.title(),
        local_skills_dir=lambda cwd: cwd / f".{name}" / "skills",
        local_agents_dir=lambda cwd: cwd / f".{name}" / "agents",
        global_skills_dir=lambda home: home / f".{name}" / "skills",
        global_agents_dir=lambda home: home / f".{name}" / "agents",
        agent_content_subdir="fake", detect=lambda: True,
    )


def _files_under(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*") if p.is_file() and not p.is_symlink())


@pytest.fixture
def project(tmp_path, monkeypatch):
    _resources(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    return project


def test_a_destination_file_that_is_a_link_is_not_written_through(project, tmp_path):
    """A repository can ship `.claude/skills/<name>/SKILL.md` as a link to any
    file the user can write. Installing into it would overwrite that file."""
    victim = tmp_path / "victim.txt"
    victim.write_text("the user's own file\n")
    dest = project / ".fake" / "skills" / "griot-onboarding"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").symlink_to(victim)

    with pytest.raises(harnesses.UnsafeDestination, match="symbolic link"):
        harnesses.install(_harness(), "local", cwd=project)

    assert victim.read_text() == "the user's own file\n"
    assert _files_under(project) == [], "refused before anything was written, not halfway"


def test_a_link_to_a_file_that_does_not_exist_yet_is_refused_too(project, tmp_path):
    """`exists()` is False for a dangling link, and creating "the file" creates
    whatever the link names."""
    target = tmp_path / "not-there-yet"
    dest = project / ".fake" / "skills" / "griot-onboarding"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").symlink_to(target)

    with pytest.raises(harnesses.UnsafeDestination):
        harnesses.install(_harness(), "local", cwd=project)

    assert not target.exists()


@pytest.mark.parametrize("linked", [".fake", ".fake/skills", ".fake/skills/griot-onboarding"])
def test_a_project_directory_that_links_out_of_the_project_is_refused(project, tmp_path, linked):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = project / linked
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(harnesses.UnsafeDestination, match="outside"):
        harnesses.install(_harness(), "local", cwd=project)

    assert _files_under(outside) == []


def test_a_link_that_stays_inside_the_project_is_followed(project):
    """The rule is where the write lands, not whether a link exists."""
    real = project / "tooling" / "fake"
    real.mkdir(parents=True)
    (project / ".fake").symlink_to(real, target_is_directory=True)

    result = harnesses.install(_harness(), "local", cwd=project)

    assert len(result["created"]) == 3
    assert (real / "skills" / "griot-onboarding" / "SKILL.md").read_text() == "# skill\n"


def test_a_home_directory_kept_in_a_dotfiles_folder_still_installs(tmp_path, monkeypatch):
    """Global scope: `~/.claude` linked to a dotfiles checkout is the user's
    own layout, wherever it points. Only the final file may not be a link."""
    _resources(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    dotfiles = tmp_path / "dotfiles" / "fake"
    dotfiles.mkdir(parents=True)
    (home / ".fake").symlink_to(dotfiles, target_is_directory=True)

    result = harnesses.install(_harness(), "global", home=home)

    assert len(result["created"]) == 3
    assert (dotfiles / "agents" / "griot-setup-assistant.md").read_text() == "# agent\n"


def test_in_the_home_directory_a_linked_file_is_still_refused(tmp_path, monkeypatch):
    _resources(tmp_path, monkeypatch)
    home = tmp_path / "home"
    victim = tmp_path / "victim.txt"
    victim.write_text("kept\n")
    (home / ".fake" / "agents").mkdir(parents=True)
    (home / ".fake" / "agents" / "griot-setup-assistant.md").symlink_to(victim)

    with pytest.raises(harnesses.UnsafeDestination):
        harnesses.install(_harness(), "global", home=home)

    assert victim.read_text() == "kept\n"
    assert _files_under(home) == []


def test_a_destination_that_shares_its_inode_with_another_file_is_replaced_not_written_into(project, tmp_path):
    """A hard link is an ordinary file as far as any check can tell. Writing
    INTO it would change the other name too; replacing it does not."""
    victim = tmp_path / "victim.txt"
    victim.write_text("the user's own file\n")
    victim.chmod(0o644)
    dest = project / ".fake" / "skills" / "griot-onboarding"
    dest.mkdir(parents=True)
    os.link(victim, dest / "SKILL.md")

    result = harnesses.install(_harness(), "local", cwd=project)

    assert "griot-onboarding/SKILL.md" in result["updated"]
    assert (dest / "SKILL.md").read_text() == "# skill\n"
    assert victim.read_text() == "the user's own file\n"
    assert victim.stat().st_mode & 0o777 == 0o644
    assert (dest / "SKILL.md").stat().st_mode & 0o777 == 0o600


def test_the_write_itself_refuses_a_link_that_appeared_after_the_check(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("kept\n")
    link = tmp_path / "SKILL.md"
    link.symlink_to(victim)
    with pytest.raises(harnesses.UnsafeDestination):
        harnesses._write_file(link, b"new")
    assert victim.read_text() == "kept\n"


def test_a_write_that_fails_leaves_no_temporary_file_behind(tmp_path, monkeypatch):
    dest = tmp_path / "skills" / "SKILL.md"

    def fails(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(harnesses.os, "replace", fails)
    with pytest.raises(OSError, match="disk full"):
        harnesses._write_file(dest, b"new")
    assert list((tmp_path / "skills").iterdir()) == []


def test_the_second_install_of_the_same_files_is_still_unchanged(project):
    harnesses.install(_harness(), "local", cwd=project)
    again = harnesses.install(_harness(), "local", cwd=project)
    assert again["created"] == [] and again["updated"] == [] and len(again["unchanged"]) == 3


def test_the_command_says_why_and_fails_without_a_traceback(project, tmp_path, monkeypatch, capsys):
    victim = tmp_path / "victim.txt"
    victim.write_text("kept\n")
    (project / ".fake" / "agents").mkdir(parents=True)
    (project / ".fake" / "agents" / "griot-setup-assistant.md").symlink_to(victim)
    monkeypatch.chdir(project)
    monkeypatch.setattr(harnesses, "HARNESSES", [_harness()])

    rc = harnesses.cmd_install("local", "fake", ask_instructions=False, mcp="no")

    err = capsys.readouterr().err
    assert rc == 1
    assert "symbolic link" in err and "griot-setup-assistant.md" in err
    assert victim.read_text() == "kept\n"


def _second_harness_has_a_linked_file(project, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("kept\n")
    (project / ".second" / "agents").mkdir(parents=True)
    (project / ".second" / "agents" / "griot-setup-assistant.md").symlink_to(victim)
    return [_harness("first"), _harness("second")]


def test_a_refusal_for_one_harness_leaves_none_of_them_installed(project, tmp_path):
    both = _second_harness_has_a_linked_file(project, tmp_path)
    with pytest.raises(harnesses.UnsafeDestination):
        harnesses.install_many(both, "local", cwd=project)
    assert _files_under(project) == []


def test_the_command_checks_every_harness_before_the_first_file(project, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "detect_harnesses", lambda: _second_harness_has_a_linked_file(project, tmp_path))
    monkeypatch.chdir(project)

    rc = harnesses.cmd_install("local", "all", ask_instructions=False, mcp="no")

    assert rc == 1 and "nothing was installed" in capsys.readouterr().err
    assert _files_under(project) == []


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_the_tool_refuses_before_asking_anyone(mode, project, tmp_path, monkeypatch):
    """Through a real client that CAN ask: a question whose answer cannot
    change the outcome teaches people to click through the next one."""
    from mcp.client.client import Client
    from mcp_types import ElicitResult
    from griot import mcp_server
    victim = tmp_path / "victim.txt"
    victim.write_text("kept\n")
    (project / ".fake" / "agents").mkdir(parents=True)
    (project / ".fake" / "agents" / "griot-setup-assistant.md").symlink_to(victim)
    monkeypatch.chdir(project)
    monkeypatch.setattr(harnesses, "HARNESSES", [_harness()])
    asked = []

    async def callback(ctx, params):
        asked.append(params)
        return ElicitResult(action="accept", content={})

    async with Client(mcp_server.mcp, mode=mode, elicitation_callback=callback) as client:
        result = await client.call_tool("griot_assist_install", {"harness": "fake", "scope": "local"})

    out = result.structured_content
    assert asked == [], "nobody should be asked about an install that cannot happen"
    assert out["changed"] is False and "symbolic link" in out["message"]
    assert victim.read_text() == "kept\n"
    assert _files_under(project) == []


@pytest.mark.anyio
async def test_the_tool_installs_through_a_real_client_when_nothing_is_wrong(project, monkeypatch):
    """The other half: the check must not refuse what is fine."""
    from mcp.client.client import Client
    from mcp_types import ElicitResult
    from griot import mcp_server
    monkeypatch.chdir(project)
    monkeypatch.setattr(harnesses, "HARNESSES", [_harness()])

    async def callback(ctx, params):
        return ElicitResult(action="accept", content={})

    async with Client(mcp_server.mcp, elicitation_callback=callback) as client:
        result = await client.call_tool("griot_assist_install", {"harness": "fake", "scope": "local"})

    assert result.structured_content["changed"] is True
    assert len(_files_under(project)) == 3


# --- a ceiling that is not a number is not a ceiling -------------------------------------

NOT_AN_AMOUNT = ["nan", "NaN", "inf", "-inf", "Infinity", "-1", "-0.01", "abc", ""]


@pytest.mark.parametrize("value", NOT_AN_AMOUNT, ids=repr)
def test_an_amount_that_is_not_a_finite_number_from_zero_up_is_refused(monkeypatch, value):
    """`spend >= nan` is False for every spend: the breaker would never trip.
    A negative price makes every call record as free."""
    monkeypatch.setenv("GRIOT_TEST_AMOUNT", value)
    with pytest.raises(ValueError, match="GRIOT_TEST_AMOUNT"):
        common._amount_env("GRIOT_TEST_AMOUNT", "3.0")


@pytest.mark.parametrize("value,expected", [("0", 0.0), ("2.5", 2.5), (" 3 ", 3.0), ("1e-3", 0.001)])
def test_an_ordinary_amount_is_read(monkeypatch, value, expected):
    monkeypatch.setenv("GRIOT_TEST_AMOUNT", value)
    assert common._amount_env("GRIOT_TEST_AMOUNT", "3.0") == expected


def test_an_amount_that_is_not_set_takes_the_default(monkeypatch):
    monkeypatch.delenv("GRIOT_TEST_AMOUNT", raising=False)
    assert common._amount_env("GRIOT_TEST_AMOUNT", "3.0") == 3.0
    assert common._amount_env("GRIOT_TEST_AMOUNT", None) is None


SPEND_VARIABLES = [
    "GRIOT_SPEND_CEILING_USD",
    "GRIOT_SPEND_VELOCITY_CEILING_USD",
    "GRIOT_CHAT_PRICE_PER_1M_TOKENS",
    "GRIOT_OPENAI_CHAT_PRICE_PER_1M_TOKENS",
    "GRIOT_DEEPSEEK_CHAT_PRICE_PER_1M_TOKENS",
    "GRIOT_GROQ_CHAT_PRICE_PER_1M_TOKENS",
]


@pytest.mark.parametrize("variable", SPEND_VARIABLES)
def test_griot_does_not_start_with_a_spend_setting_that_is_not_a_number(variable, tmp_path):
    """In a fresh interpreter, the way a real `griot` starts: every one of
    these is read when the module loads."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"))
    env[variable] = "nan"
    done = subprocess.run([sys.executable, "-c", "import griot.common"], env=env, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode != 0
    assert variable in done.stderr and "finite" in done.stderr


@pytest.mark.parametrize("variable,value", [
    ("GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES", "abc"),
    ("GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES", "2.5"),
    ("GRIOT_MCP_IDLE_RELEASE_SECONDS", "nan"),
    ("GRIOT_MCP_IDLE_RELEASE_SECONDS", "soon"),
])
def test_the_other_numeric_settings_are_named_in_one_line_too(variable, value, tmp_path):
    """Neither is a spend gate. A traceback is still not how a setting
    should be reported, and an idle time of `nan` never releases the index."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"))
    env[variable] = value
    done = subprocess.run([sys.executable, "-m", "griot.cli", "stats"], env=env, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 2
    assert done.stderr.startswith(f"Error: {variable}") and "Traceback" not in done.stderr


def test_the_command_names_the_setting_in_one_line(monkeypatch, capsys):
    """A traceback ending in the message is still a traceback."""
    import importlib
    import griot
    from griot import cli

    def starts_with_a_bad_setting(argv):
        raise griot.ConfigurationError("GRIOT_SPEND_CEILING_USD must be a finite number, zero or more (got 'nan').")

    monkeypatch.setattr(cli, "_main", starts_with_a_bad_setting)
    assert cli.main(["stats"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("Error: GRIOT_SPEND_CEILING_USD") and "Traceback" not in err
    assert issubclass(griot.ConfigurationError, ValueError)
    importlib.import_module("griot.common")  # still importable with the settings of this run


def test_the_real_command_fails_with_that_line(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               GRIOT_SPEND_CEILING_USD="nan")
    done = subprocess.run([sys.executable, "-m", "griot.cli", "stats"], env=env, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 2
    assert done.stderr.startswith("Error: GRIOT_SPEND_CEILING_USD") and "Traceback" not in done.stderr


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), float("-inf"), -0.5], ids=repr)
def test_a_cost_that_is_not_an_amount_stops_the_run_instead_of_being_recorded(cost):
    """Python reads `NaN` and `Infinity` from a JSON body, and a token count
    can be negative: the rule is the one for a price, finite and zero or
    more. A negative cost used to be dropped as if the call had been free."""
    common.record_spend(0.25)
    with pytest.raises(RuntimeError, match="circuit breaker"):
        common.record_spend(cost)
    assert common.get_spend_today() == pytest.approx(0.25)


@pytest.mark.parametrize("stored", [float("nan"), float("inf")], ids=repr)
def test_a_stored_total_that_is_not_a_number_blocks_paid_calls(monkeypatch, stored):
    """The comparison is written so that anything that is not below the
    ceiling counts as having reached it."""
    monkeypatch.setattr(logdb, "read_spend_today", lambda *a, **k: stored)
    with pytest.raises(RuntimeError, match="circuit breaker"):
        common.check_spend_ceiling()
    assert common.get_index_status()["spend_ceiling_exceeded"] is True


def test_a_velocity_that_is_not_a_number_blocks_paid_calls(monkeypatch):
    monkeypatch.setattr(logdb, "read_spend_velocity", lambda *a, **k: float("nan"))
    with pytest.raises(RuntimeError, match="circuit breaker"):
        common.check_spend_ceiling()
