__version__ = "0.1.0"


class ConfigurationError(ValueError):
    """A setting griot cannot start with. Defined here, not in common.py: it
    is raised while common.py is being imported, and the CLI has to be able
    to recognise it when that import did not finish."""
