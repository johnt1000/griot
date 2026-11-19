__version__ = "0.9.0"  # the same number as pyproject.toml: tests/test_release.py


# The spellings a yes/no setting is read with. Here, in one place, because
# `griot config set` (which checks a value before griot's configuration
# loads) and the code that reads the variable must agree: the command took
# yes/no/on/off while the readers knew two spellings each, so a line
# written by hand as `GRIOT_LOG_QUESTIONS=no` kept logging questions.
TRUE_WORDS = ("true", "yes", "on", "1")
FALSE_WORDS = ("false", "no", "off", "0")


# Set by the two ways an MCP server starts (`griot mcp`, and
# `python -m griot.mcp_server`) BEFORE the configuration loads. A server is
# started by whoever registered it, and a registration can come with the
# project it serves (a `.mcp.json` in a cloned repository), so its
# environment is not the person's own word: common.py then obeys a value in
# it only where that narrows what the person's configuration file says, and
# refuses a configuration or data directory inside the project. A command at
# a terminal leaves this alone and obeys the shell it runs in.
ENVIRONMENT_ONLY_NARROWS = False
# The profile given as `--profile` on the command line, when there was one.
# cli.py puts it in the environment for the configuration to read; it is in
# the command a person approves when a server is registered, which the `env`
# beside it is not, so it is not treated as the environment's.
PROFILE_FROM_COMMAND_LINE: str | None = None
# Whether `--quiet` (or -q) was typed, as opposed to GRIOT_QUIET=1 in the
# environment: a command griot rebuilds for a person to paste keeps it.
QUIET_FROM_COMMAND_LINE = False


def is_interactive() -> bool:
    """True when a person can be asked: both ends are a terminal. The one
    check for every command that asks something (common.confirm and the
    others in common.py, `griot assist install`). Here, not in common.py,
    which re-exports it: `griot assist install` must still ask, and still
    finish, when the configuration common.py reads cannot be loaded."""
    import sys
    return sys.stdin.isatty() and sys.stdout.isatty()


class GriotError(Exception):
    """An error griot raises on purpose, with text written for whoever reads
    it: what happened and what to do. The MCP server sends this text to the
    agent (as a ToolError, or a ResourceError for a resource), because from
    mcp 2.2 on the SDK shows a client only those and replaces anything else
    with a bare "Error executing tool <name>". Anything that is not one of
    these is a crash, and keeps that masking: its text can carry internals.

    A mixin beside the builtin each error always was (RuntimeError,
    ValueError), so every `except RuntimeError` written before it still
    catches the same errors. Defined here, not in common.py, for the reason
    ConfigurationError is."""


class UsageError(GriotError, ValueError):
    """The request itself is wrong (an unknown profile, a path that is not a
    directory, a repository not in the list): the CLI exits 2 for it. A plain
    ValueError from the same function is the operation failing: exit 1. A
    GriotError too: the text says what to do, and an MCP tool that raises it
    (a repository path that is not a directory) must hand that to the agent."""


class ConfigurationError(GriotError, ValueError):
    """A setting griot cannot start with, or a configuration file it cannot
    write (a <config>/.env that is a link leading nowhere). The CLI turns it
    into one line with exit status 2. Defined here, not in common.py: it
    is raised while common.py is being imported, and the CLI has to be able
    to recognise it when that import did not finish."""


class UnknownEmbedProfile(ConfigurationError):
    """GRIOT_EMBED_PROFILE names a profile that does not exist. Carries the
    name and the valid ones, for the one command that takes a profile name
    as an argument and puts it in force before the configuration loads."""

    def __init__(self, message: str, name: str, options: list[str], reason: str = ""):
        super().__init__(message)
        # reason: why the name is gone, for a profile griot used to list
        # (common.RETIRED_EMBED_PROFILES), so the callers that word this
        # error themselves (`profiles use`, doctor) say it too.
        self.name, self.options, self.reason = name, options, reason
