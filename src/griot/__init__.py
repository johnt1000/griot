__version__ = "0.1.0"


class ConfigurationError(ValueError):
    """A setting griot cannot start with. Defined here, not in common.py: it
    is raised while common.py is being imported, and the CLI has to be able
    to recognise it when that import did not finish."""


class UnknownEmbedProfile(ConfigurationError):
    """GRIOT_EMBED_PROFILE names a profile that does not exist. Carries the
    name and the valid ones, for the one command that takes a profile name
    as an argument and puts it in force before the configuration loads."""

    def __init__(self, message: str, name: str, options: list[str]):
        super().__init__(message)
        self.name, self.options = name, options
