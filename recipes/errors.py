"""Recipe-level errors. Library errors (vmkit, configgen, isokit) propagate unchanged."""


class RecipeError(Exception):
    """Base class for failures raised by the recipes themselves."""


class SetupFilesNotFoundError(RecipeError):
    """The base-vm-setup scripts a bake packs onto its disc are missing."""


class BakeTimeoutError(RecipeError):
    """The bake VM did not power itself off within the allowed time."""
