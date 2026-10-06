__version__ = "0.2.1"  # the same number as pyproject.toml: tests/test_release.py


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


class ConfigurationError(ValueError):
    """A setting griot cannot start with, or a configuration file it cannot
    write (a <config>/.env that is a link leading nowhere). The CLI turns it
    into one line with exit status 2. Defined here, not in common.py: it
    is raised while common.py is being imported, and the CLI has to be able
    to recognise it when that import did not finish."""


class UnknownEmbedProfile(ConfigurationError):
    """GRIOT_EMBED_PROFILE names a profile that does not exist. Carries the
    name and the valid ones, for the one command that takes a profile name
    as an argument and puts it in force before the configuration loads."""

    def __init__(self, message: str, name: str, options: list[str]):
        super().__init__(message)
        self.name, self.options = name, options
