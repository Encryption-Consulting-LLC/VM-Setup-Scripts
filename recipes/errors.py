"""Recipe-level errors. Library errors (vmkit, configgen, isokit) propagate unchanged."""


class RecipeError(Exception):
    """Base class for failures raised by the recipes themselves."""


class SetupFilesNotFoundError(RecipeError):
    """The base-vm-setup scripts a bake packs onto its disc are missing."""


class PowerOffTimeoutError(RecipeError):
    """A VM expected to power itself off when its work is done did not, in time."""


class BakeTimeoutError(PowerOffTimeoutError):
    """The bake VM did not power itself off within the allowed time."""


class InstallTimeoutError(PowerOffTimeoutError):
    """The autoinstall VM did not power itself off within the allowed time."""


class BatchPreflightError(RecipeError):
    """A batch failed its checks before anything started. ``problems`` lists them all."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__(
            f"Batch not started: {len(self.problems)} problem(s):\n"
            + "\n".join(f"  - {p}" for p in self.problems)
        )
